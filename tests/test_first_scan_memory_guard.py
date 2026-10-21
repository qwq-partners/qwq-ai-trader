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


def high_volume_budget(study):
    byte_bounded_study(study)
    s = study['capture']
    s['selection_basis']['max_candidates'] = 100
    s['entry_gate_trace']['max_candidates'] = 100
    s['record_allocations'] = {'ws_quote': 35000, 'market_state': 65000, 'other': 512}
    s['channels']['market_state_max_records'] = 65000
    s['record_max_bytes'].update(ws_quote=2048, market_state=2048)
    s.update(journal_budget_version='bounded-envelope-v1', buffer_capacity=100713,
             queue_capacity=100713, max_bytes=240144543)


def test_explicit_envelope_budget_fits_reader_without_changing_legacy_budget(tmp_path):
    from src.analytics.first_scan_resources import first_scan_journal_bound, first_scan_record_limits
    from src.analytics.pregate_capture_capacity import assess_pregate_capture_resources
    plan, study = load(tmp_path, high_volume_budget)
    assert first_scan_journal_bound(plan.settings, first_scan_record_limits(plan.settings)) == 240144543
    capacity = assess_pregate_capture_resources(study, operational_registrations=2, external_registrations=0)
    assert capacity['numeric_configuration_passed']
    assert capacity['checks']['readout_limit']['passed']
    assert not capacity['installation_ready']
    legacy = deepcopy(study)
    legacy['capture'].pop('journal_budget_version')
    with pytest.raises(ValueError, match='예산'):
        validate_window_contract(legacy)


@pytest.mark.parametrize('defect', ['unknown_version', 'null_version', 'missing_bytes', 'missing_allocations', 'short_budget'])
def test_compact_budget_rejects_unsupported_or_underfunded_contract_before_file(tmp_path, defect):
    def change(study):
        high_volume_budget(study)
        s = study['capture']
        if defect == 'unknown_version': s['journal_budget_version'] = 'future'
        elif defect == 'null_version': s['journal_budget_version'] = None
        elif defect == 'missing_bytes': s.pop('record_max_bytes')
        elif defect == 'missing_allocations': s.pop('record_allocations')
        elif defect == 'short_budget': s['max_bytes'] -= 1
    path, _ = fixture_plan(tmp_path, change)
    with pytest.raises(ValueError):
        module().CapturePlan.load(path, now=NOW)
    assert not list(tmp_path.glob('*.jsonl'))


@pytest.mark.asyncio
async def test_compact_journal_is_legacy_readable_and_preserves_payload(tmp_path):
    from src.analytics.entry_observation_journal import ObservationJournal, read_observation_journal
    plan, _ = load(tmp_path, high_volume_budget)
    b = _WindowBuffer(plan, lambda: plan.start_at)
    j = await ObservationJournal.open(b, plan.settings['journal_path'], study_ref='synthetic',
        study_sha256=plan.study_sha256, queue_capacity=100713, batch_size=10,
        max_bytes=240144543, max_record_bytes=65536, budget_version='bounded-envelope-v1')
    try:
        payload = {'kind':'ws_quote', 'observed_at':plan.start_at.isoformat(), 'quote_id':'raw',
                   'provenance':{'exchange_time':'한\\\"' * 100}}
        assert b.publish(payload)
    finally:
        result = await j.close()
    assert result['sealed'] and result['error'] is None
    loaded = read_observation_journal(plan.settings['journal_path'], max_bytes=256*1024*1024,
                                       expected_study_sha256=plan.study_sha256)
    assert loaded['records'][0] == {**payload, 'sequence':1}


@pytest.mark.asyncio
async def test_compact_control_rows_cannot_silently_overrun_their_reserve(tmp_path):
    from src.analytics.entry_observation_journal import ObservationJournal, read_observation_journal
    plan, _ = load(tmp_path, high_volume_budget)
    b = _WindowBuffer(plan, lambda: plan.start_at)
    j = await ObservationJournal.open(b, plan.settings['journal_path'], study_ref='synthetic',
        study_sha256=plan.study_sha256, queue_capacity=100713, batch_size=10,
        max_bytes=240144543, max_record_bytes=65536, budget_version='bounded-envelope-v1')
    b.mark_incomplete('oversized-reason-' + 'x'*16384)
    result = await j.close()
    assert not result['sealed'] and result['error'] == 'journal_writer_failed'
    loaded = read_observation_journal(plan.settings['journal_path'], max_bytes=256*1024*1024,
                                       expected_study_sha256=plan.study_sha256)
    assert not loaded['complete'] and not loaded['journal']['sealed']


@pytest.mark.asyncio
async def test_compact_oversize_header_rejected_before_creating_journal(tmp_path):
    from src.analytics.entry_observation_journal import ObservationJournal
    plan, _ = load(tmp_path, high_volume_budget)
    b = _WindowBuffer(plan, lambda: plan.start_at)
    b.evaluation_epoch = 'x'*16384
    with pytest.raises(ValueError):
        await ObservationJournal.open(b, plan.settings['journal_path'], study_ref='synthetic',
            study_sha256=plan.study_sha256, queue_capacity=100713, batch_size=10,
            max_bytes=240144543, max_record_bytes=65536, budget_version='bounded-envelope-v1')
    assert not list(tmp_path.glob('*.jsonl'))


@pytest.mark.asyncio
@pytest.mark.parametrize('capacity,envelope_bound', [(8,195), (9,195), (98,196), (99,196)])
async def test_compact_writer_envelope_covers_row_digit_boundaries(tmp_path, capacity, envelope_bound):
    import json
    from src.analytics.entry_observation_journal import ObservationJournal, _json, journal_record_envelope_bytes
    b = obs.EntryObservationBuffer(evaluation_epoch='합성'*100, capacity=capacity)
    path = tmp_path / 'digits.jsonl'
    j = await ObservationJournal.open(b, path, study_ref='한'*200, study_sha256='a'*64,
        queue_capacity=capacity, batch_size=1, max_bytes=32768+capacity*(512+envelope_bound),
        max_record_bytes=512, budget_version='bounded-envelope-v1')
    try:
        for n in range(capacity):
            assert b.publish({'kind':'ws_quote', 'quote_id':str(n), 'symbol':'900001',
                              'observed_at':'2026-10-21T00:30:00+00:00'})
    finally:
        result = await j.close()
    assert result['sealed'] and result['persisted_records'] == capacity
    assert journal_record_envelope_bytes(capacity) == envelope_bound
    rows = path.read_bytes().splitlines(keepends=True)
    assert len(rows) == capacity+2
    for line in rows[1:-1]:
        payload = json.loads(line)['payload']
        assert len(line)-len(_json(payload)) <= envelope_bound
    assert len(rows[0]) <= 16384 and len(rows[-1]) <= 16384


@pytest.mark.asyncio
async def test_compact_byte_guard_preserves_full_state_and_rejects_large_book_without_blocking_feed(tmp_path, monkeypatch):
    import asyncio
    from src.analytics.entry_observation_runtime import ObservationRuntime
    from src.analytics.entry_observation_journal import read_observation_journal, _json
    from src.data.feeds.quote_subscription import observe_screen_candidates
    from test_market_state_capture import advancing_capture_clock, fields, PRICE, BOOK, STATUS, receive
    from test_quote_subscription import make_feed, ack
    plan, _ = load(tmp_path, high_volume_budget)
    clock = advancing_capture_clock(monkeypatch); clock[0] = NOW
    feed = make_feed()
    bot = SimpleNamespace(market='kr', dry_run=False, ws_feed=feed)
    runtime = await ObservationRuntime.install(bot, plan, now=lambda: clock[0])
    assert runtime.first_scan_readiness()['journal_budget_version'] == 'bounded-envelope-v1'
    owner = feed._quote_subscription_owner
    owner.interval = 0
    delivered = []

    async def capture(event):
        obs.capture_quote(runtime.buffer, event)

    async def operational_quote(event):
        delivered.append(event)

    feed.on_quote(capture)
    feed.on_quote(operational_quote)
    try:
        assert await feed.connect()
        await owner.set_operational(['900001'], PRICE, BOOK)
        clock[0] = plan.start_at
        candidates = [SimpleNamespace(symbol='900001', score=90)]
        scan = obs.capture_scan(runtime.buffer, candidates, 'regular')
        observe_screen_candidates(feed, runtime.buffer, scan, candidates)
        await asyncio.gather(*list(owner._tasks))
        for key in list(owner._states): await owner.handle_ack(ack(key), owner.generation)
        # Documented state fields accept at most16 codepoints. Four-byte UTF-8
        # and a large frame counter must survive whole, not be cut to fit.
        raw = fields(STATUS, '900001')
        for index in (1,3,4,5,6,7,8,9,10): raw[index] = '😀'*16
        feed._message_count = 999999999999
        await receive(feed, owner, '0|'+STATUS+'|001|'+'^'.join(raw))
        state = next(r for r in runtime.buffer._records if r['kind']=='market_state')
        assert state['trht_yn'] == state['vi_cls_code'] == '😀'*16
        assert state['local_frame_index'] == 1000000000000
        assert len(_json(state)) <= 2048
        before = len(runtime.buffer._records)
        large_book = fields(BOOK, '900001'); large_book[3] = '9'*2048
        await receive(feed, owner, '0|'+BOOK+'|001|'+'^'.join(large_book))
        assert len(delivered) == 1 and str(delivered[0].ask_price) == '9'*2048
        assert not any(r['kind']=='ws_quote' for r in runtime.buffer._records[before:])
        assert runtime.buffer._dropped == 1
        assert 'record_byte_limit' in runtime.buffer.capture_status()['incomplete_reasons']
    finally:
        clock[0] = plan.end_at
        await runtime.close()
        await feed.disconnect()
    assert runtime.result['sealed']
    result = read_observation_journal(plan.settings['journal_path'], max_bytes=256*1024*1024,
                                      expected_study_sha256=plan.study_sha256)
    assert not result['complete'] and result['dropped_records'] == 1
    assert next(r for r in result['records'] if r['kind']=='market_state') == state


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
