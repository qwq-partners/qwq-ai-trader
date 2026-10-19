"""명시 첫 스캔의 자원 보호. 합성 입력과 임시 원장만 사용한다."""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from src.analytics import entry_observation as obs
from src.analytics.entry_observation_runtime import _WindowBuffer, validate_window_contract
from test_entry_observation_runtime import load, NOW, fixture_plan, module
from test_pregate_capture_flow import study_change


def guarded_study(study):
    study_change(study)
    s = study['capture']
    s['record_allocations'] = {'ws_quote': 100, 'market_state': 100, 'other': 100}


def make_buffer(tmp_path):
    plan, study = load(tmp_path, guarded_study)
    clock = [plan.start_at]
    return _WindowBuffer(plan, lambda: clock[0]), plan, study


def test_explicit_first_scan_enables_retained_memory_and_per_kind_limits(tmp_path):
    b, _, _ = make_buffer(tmp_path)
    assert b.retained_memory_limit_bytes == 512 * 1024 * 1024
    assert b._record_limits == {'scan_metadata': 21, 'ws_quote': 100, 'market_state': 100, 'other': 100}
    assert b.llm_admission is b.order_ready_admission is False
    assert b.scan_scope == 'first' and b.max_scans is None


@pytest.mark.parametrize('kind,category', [('ws_quote', 'ws_quote'), ('market_state', 'market_state'), ('quote_subscription', 'other')])
def test_category_exhaustion_keeps_prior_records_and_other_categories(tmp_path, kind, category):
    b, plan, _ = make_buffer(tmp_path)
    b._record_limits[category] = 1
    row = {'kind': kind, 'observed_at': plan.start_at.isoformat()}
    assert b.publish(row)
    old = deepcopy(b._records)
    charge = b.retained_memory_bytes
    assert not b.publish(row)
    assert b._records == old and b.retained_memory_bytes == charge
    assert b._record_counts[category] == 1
    assert b._dropped == 1
    assert category + '_record_limit' in b.capture_status()['incomplete_reasons']
    other = 'ws_quote' if category != 'ws_quote' else 'quote_subscription'
    assert b.publish({'kind': other, 'observed_at': plan.start_at.isoformat()})


def test_memory_failure_does_not_copy_enroll_or_offer_scan(tmp_path, monkeypatch):
    b, plan, _ = make_buffer(tmp_path)
    b.retained_memory_limit_bytes = 1
    offered = []
    b._journal_sink = SimpleNamespace(offer=offered.append, failure_reason=None)
    monkeypatch.setattr(obs, 'deepcopy', lambda _: pytest.fail('복사 전에 메모리 거부 필요'))
    stock = SimpleNamespace(symbol='900001', score=90)
    assert obs.capture_scan(b, [stock], 'regular') is None
    assert not b._records and not b._cohort_candidates and not offered
    assert b.retained_memory_bytes == 0 and b.memory_exhausted
    assert not b._record_counts and not b.capture_status()['complete']
    assert obs.capture_scan(b, [stock], 'regular') is None  # 첫 실패를 후속 스캔으로 대체하지 않는다.


def test_oversize_first_scan_record_rejected_before_copy_and_journal(tmp_path, monkeypatch):
    b, plan, _ = make_buffer(tmp_path)
    offered = []
    b._journal_sink = SimpleNamespace(offer=offered.append, failure_reason=None)
    monkeypatch.setattr(obs, 'deepcopy', lambda _: pytest.fail('거부한 입력 복사 금지'))
    assert not b.publish({'kind': 'ws_quote', 'observed_at': plan.start_at.isoformat(),
                          'provenance': {'exchange_time': 'x' * 65537}})
    assert not b._records and not offered and not b._record_counts
    assert not b.capture_status()['complete']


@pytest.mark.parametrize('defect', ['missing_kind', 'extra_kind', 'bool', 'state_binding', 'state_off', 'buffer', 'queue', 'journal', 'selection', 'gate'])
def test_invalid_explicit_allocations_rejected_before_journal(tmp_path, defect):
    def change(study):
        guarded_study(study)
        s = study['capture']
        if defect == 'missing_kind': del s['record_allocations']['other']
        elif defect == 'extra_kind': s['record_allocations']['metadata'] = 1
        elif defect == 'bool': s['record_allocations']['other'] = True
        elif defect == 'state_binding': s['record_allocations']['market_state'] += 1
        elif defect == 'state_off': s['channels']['market_state_evidence'] = False
        elif defect == 'buffer': s['buffer_capacity'] = 320
        elif defect == 'queue': s['queue_capacity'] = 999
        elif defect == 'journal': s['max_bytes'] -= 1
        elif defect == 'selection': s['selection_basis']['max_candidates'] = True
        elif defect == 'gate': s['entry_gate_trace']['max_candidates'] = 9
    path, _ = fixture_plan(tmp_path, change)
    with pytest.raises(ValueError):
        module().CapturePlan.load(path, now=NOW)
    assert not list(tmp_path.glob('*.jsonl'))


def test_replay_enforces_explicit_allocation_even_without_window_scans(tmp_path):
    _, _, study = make_buffer(tmp_path)
    records = [{'kind': 'ws_quote'} for _ in range(101)]
    with pytest.raises(ValueError, match='allocation|예산|한도'):
        validate_window_contract(study, {'records': records})


def test_legacy_v4_remains_unguarded_and_replay_supported(tmp_path):
    plan, study = load(tmp_path, study_change)
    b = _WindowBuffer(plan, lambda: plan.start_at)
    assert b.retained_memory_limit_bytes is None and b._record_limits == {}
    validate_window_contract(study, {'records': []})


def test_exact_guarded_total_capacity_boundary(tmp_path):
    def change(study):
        guarded_study(study)
        s = study['capture']
        s.update(buffer_capacity=321, queue_capacity=321,
                 max_bytes=321 * (65536 + 2048) + 2048)
    plan, study = load(tmp_path, change)
    b = _WindowBuffer(plan, lambda: plan.start_at)
    assert b.capacity == sum(b._record_limits.values()) == 321
    validate_window_contract(study, {'records': [{'kind': 'ws_quote'}] * 100})


def test_memory_rejection_is_sticky_with_one_drop_per_public_attempt(tmp_path):
    b, plan, _ = make_buffer(tmp_path)
    b.retained_memory_limit_bytes = 1
    row = {'kind': 'ws_quote', 'observed_at': plan.start_at.isoformat()}
    for expected in range(1, 6):
        assert b.publish(row) is False
        assert b._dropped == expected
    assert b.memory_exhausted and not b._records and not b._record_counts


@pytest.mark.parametrize('version', ['runner-first-scan-v1', 'runner-first-scan-v2', 'runner-first-scan-v3'])
def test_record_allocation_option_does_not_extend_older_schemas(tmp_path, version):
    def change(study):
        study['capture']['version'] = version
        if version != 'runner-first-scan-v1':
            from test_kis_frame_diagnostics import v4_change
            v4_change(study)
            study['capture']['version'] = version
            study['capture'].pop('frame_diagnostics')
            if version == 'runner-first-scan-v2':
                study['capture'].pop('entry_gate_trace')
        study['capture']['record_allocations'] = {'ws_quote': 100, 'market_state': 100, 'other': 100}
    path, _ = fixture_plan(tmp_path, change)
    with pytest.raises(ValueError, match='계약/필드'):
        module().CapturePlan.load(path, now=NOW)
    assert not list(tmp_path.glob('*.jsonl'))


def byte_bounded_study(study):
    guarded_study(study)
    s = study['capture']
    s['record_max_bytes'] = {'scan_metadata': 65536, 'ws_quote': 4096, 'market_state': 4096, 'other': 4096}
    s['max_bytes'] = 21 * (65536 + 2048) + 300 * (4096 + 2048) + 2048


def test_kind_byte_budgets_accept_weighted_journal_bound(tmp_path):
    plan, study = load(tmp_path, byte_bounded_study)
    b = _WindowBuffer(plan, lambda: plan.start_at)
    assert b.publish({'kind': 'ws_quote', 'observed_at': plan.start_at.isoformat()})
    before = b.retained_memory_bytes
    assert not b.publish({'kind': 'ws_quote', 'observed_at': plan.start_at.isoformat(),
                          'provenance': {'exchange_time': 'x' * 4096}})
    assert b.retained_memory_bytes == before and b._record_counts['ws_quote'] == 1
    assert b._dropped == 1 and 'record_byte_limit' in b.capture_status()['incomplete_reasons']
    validate_window_contract(study, {'records': b._records})


@pytest.mark.parametrize('defect', ['without_allocations', 'missing_kind', 'unknown_kind', 'bool', 'oversize', 'metadata_shrunk', 'journal_short'])
def test_invalid_category_byte_contract_rejected(tmp_path, defect):
    def change(study):
        byte_bounded_study(study)
        s = study['capture']
        if defect == 'without_allocations': del s['record_allocations']
        elif defect == 'missing_kind': del s['record_max_bytes']['other']
        elif defect == 'unknown_kind': s['record_max_bytes']['unknown'] = 4096
        elif defect == 'bool': s['record_max_bytes']['other'] = True
        elif defect == 'oversize': s['record_max_bytes']['other'] = 65537
        elif defect == 'metadata_shrunk': s['record_max_bytes']['scan_metadata'] = 4096
        elif defect == 'journal_short': s['max_bytes'] -= 1
    path, _ = fixture_plan(tmp_path, change)
    with pytest.raises(ValueError):
        module().CapturePlan.load(path, now=NOW)
    assert not list(tmp_path.glob('*.jsonl'))


def test_replay_rejects_a_record_exceeding_category_byte_bound(tmp_path):
    _, study = load(tmp_path, byte_bounded_study)
    with pytest.raises(ValueError, match='byte|크기'):
        validate_window_contract(study, {'records': [{'kind': 'ws_quote', 'provenance': {'exchange_time': 'x' * 4096}}]})


def test_utf8_byte_limit_includes_sequence_and_rejects_next_byte_before_copy(tmp_path, monkeypatch):
    from src.analytics.entry_observation_journal import _json
    plan, _ = load(tmp_path, byte_bounded_study)
    b = _WindowBuffer(plan, lambda: plan.start_at)
    row = {'kind': 'ws_quote', 'observed_at': plan.start_at.isoformat(),
           'provenance': {'exchange_time': '가'}}
    size = len(_json({**row, 'sequence': 1}))
    row['provenance']['exchange_time'] += 'x' * (4096 - size)
    assert len(_json({**row, 'sequence': 1})) == 4096
    assert b.publish(row)
    before = b.retained_memory_bytes
    row['provenance']['exchange_time'] += 'x'
    monkeypatch.setattr(obs, 'deepcopy', lambda _: pytest.fail('한도 초과 입력 복사 금지'))
    assert not b.publish(row)
    assert b._record_counts['ws_quote'] == 1 and b.retained_memory_bytes == before and b._dropped == 1
