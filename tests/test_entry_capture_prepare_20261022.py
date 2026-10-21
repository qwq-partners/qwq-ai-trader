"""October 22 bounded-envelope candidate; all host inputs are synthetic."""
import importlib.util
from datetime import datetime, timezone
from pathlib import Path

import pytest
import test_entry_capture_prepare_20261016 as historical
import test_entry_capture_prepare_20261021 as oct21_transaction


SCRIPT = Path(__file__).parents[1] / 'scripts/ops/entry_capture_prepare_20261022.py'
PROFILE = '20261022-pregate-v1'
EPOCH = '2026-10-22-engine-pregate-v1'
NOW = datetime(2026, 10, 22, 0, 25, tzinfo=timezone.utc)


def test_oct22_installer_is_available():
    assert SCRIPT.exists(), 'October 22 bounded-envelope installer is missing'


@pytest.fixture
def m():
    assert SCRIPT.exists(), 'October 22 bounded-envelope installer is missing'
    spec = importlib.util.spec_from_file_location('prepare_oct22', SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def payloads(m, head='a' * 40):
    """Fresh, repinned Oct. 22 inputs; historical files remain unchanged."""
    doc, files = historical.payloads(m, head)
    for key in ('once', 'activation'):
        value = m.document(files[key])
        value.update(not_before='2026-10-22T00:25:00+00:00',
                     latest_start_at='2026-10-22T00:27:00+00:00')
        files[key] = m.raw(value)
    study = m.document(files['study'])
    study['as_of'] = '2026-10-22T00:51:00+00:00'
    capture = study['capture']
    for key in ('max_scans', 'quote_admission', 'rest_market_state_evidence'):
        capture.pop(key, None)
    capture.update(version='runner-first-scan-v4',
        start_at='2026-10-22T00:30:00+00:00',
        admission_end_at='2026-10-22T00:35:00+00:00', end_at=study['as_of'],
        buffer_capacity=100713, queue_capacity=100713, batch_size=100,
        max_bytes=240144543, open_timeout_seconds=5, close_timeout_seconds=10,
        journal_budget_version='bounded-envelope-v1',
        record_allocations=dict(ws_quote=35000, market_state=65000, other=512),
        record_max_bytes=dict(scan_metadata=65536, ws_quote=2048,
                              market_state=2048, other=4096))
    capture['channels'].update(lease_seconds=1262, market_state_max_records=65000)
    capture['selection_basis'] = dict(version='selection-basis-v4', max_candidates=100,
        max_source_terms=16, max_modifier_stages=24)
    study['observed_regular_flow_policy'] = dict(version='pregate-observed-regular-flow-v1',
        fixed_at='2026-10-21T12:00:00+00:00', preparation_seconds=5,
        state_max_age_seconds=5, horizon_seconds=900, max_markout_delay_seconds=5)
    historical.repin_study(m, doc, files, study)
    return doc, files


def test_oct22_payload_binds_date_epoch_budget_and_consumed_oct21(m):
    doc, files = payloads(m)
    m.validate_payload(doc, files)
    assert m.PROFILE == PROFILE and m.EPOCH == EPOCH and m.NOT_BEFORE == NOW
    assert str(m.OLD_ONCE).endswith('/20261021-pregate-v1/once.json')
    assert str(m.OLD_RECEIPT).endswith('/20261021-pregate-v1/startup.receipt')
    assert str(m.OLD_STATUS).endswith('/20261021-pregate-v1/status.json')
    study = m.document(files['study'])['capture']
    assert study['journal_budget_version'] == 'bounded-envelope-v1'
    assert study['record_allocations'] == {'ws_quote': 35000, 'market_state': 65000, 'other': 512}
    assert study['record_max_bytes'] == {'scan_metadata': 65536, 'ws_quote': 2048,
                                         'market_state': 2048, 'other': 4096}
    assert (study['buffer_capacity'], study['queue_capacity'], study['batch_size'],
            study['max_bytes']) == (100713, 100713, 100, 240144543)
    assert (study['open_timeout_seconds'], study['close_timeout_seconds']) == (5, 10)
    assert b'OnCalendar=2026-10-22 09:25:00 Asia/Seoul\n' in files['activation_timer']
    assert b'activate-20261022.py --profile 20261022-pregate-v1' in files['activation_service']


@pytest.mark.parametrize('path,value', [
    (('capture', 'journal_budget_version'), 'bounded-envelope-v2'),
    (('capture', 'journal_budget_version'), None),
    (('capture', 'start_at'), '2026-10-21T00:30:00+00:00'),
    (('capture', 'end_at'), '2026-10-22T00:51:01+00:00'),
    (('capture', 'buffer_capacity'), 40713),
    (('capture', 'queue_capacity'), 40713),
    (('capture', 'max_bytes'), 262492160),
    (('capture', 'close_timeout_seconds'), 5),
    (('capture', 'close_timeout_seconds'), 11),
    (('capture', 'record_allocations'), {'ws_quote': 21000, 'market_state': 19000, 'other': 512}),
    (('capture', 'record_max_bytes'), {'scan_metadata': 65536, 'ws_quote': 4096,
                                        'market_state': 4096, 'other': 4096}),
    (('capture', 'channels', 'lease_seconds'), 1260),
    (('capture', 'channels', 'market_state_max_records'), 19000),
])
def test_rehashed_oct22_rejects_old_date_or_budget_contract(m, path, value):
    doc, files = payloads(m)
    study = m.document(files['study'])
    target = study
    for key in path[:-1]:
        target = target[key]
    if value is None:
        target.pop(path[-1])
    else:
        target[path[-1]] = value
    historical.repin_study(m, doc, files, study)
    with pytest.raises(m.GuardError):
        m.validate_payload(doc, files)


@pytest.mark.parametrize('fixed_at', [
    '2026-10-22T00:25:00+00:00', '2026-10-22T00:25:01+00:00',
    '2026-10-21T21:00:00+09:00', '2026-10-21T12:00:00',
])
def test_observed_flow_policy_must_be_exact_utc_and_pre_activation(m, fixed_at):
    doc, files = payloads(m)
    study = m.document(files['study'])
    study['observed_regular_flow_policy']['fixed_at'] = fixed_at
    historical.repin_study(m, doc, files, study)
    with pytest.raises(m.GuardError):
        m.validate_payload(doc, files)


def test_only_exactly_consumed_oct21_evidence_can_be_retired(m):
    once = dict(version='entry-observation-once-v1',
        receipt_path=str(m.OLD_RECEIPT).replace('20261021', '20261020'),
        manifest_path=str(m.OLD_ONCE.with_name('manifest.json')))
    originals = {m.OLD_ONCE: m.raw(once),
        m.OLD_RECEIPT: m.raw({'version': 'entry-observation-once-receipt-v1', 'status': 'attempt_consumed'}),
        m.OLD_STATUS: m.raw({'status': 'complete', 'restart_attempted': True}),
        m.DROPIN: m.dropin(m.OLD_ONCE)}
    with pytest.raises(m.GuardError, match='old_attempt_not_exactly_consumed'):
        m.verify_consumed(originals)


@pytest.mark.parametrize('instant', ['2026-10-22T00:20:00+00:00', '2026-10-22T00:25:00+00:00'])
def test_install_deadline_is_0920_and_prevents_host_access(m, monkeypatch, instant):
    class Clock:
        @staticmethod
        def now(tz): return datetime.fromisoformat(instant)
    monkeypatch.setattr(m, 'datetime', Clock)
    monkeypatch.setattr(m, 'load_manifest', lambda _: pytest.fail('deadline reached host input'))
    with pytest.raises(m.GuardError, match='installation_deadline_passed'):
        m.prepare('a' * 64)


@pytest.mark.parametrize('value,accepted', [
    ('Thu 2026-10-22 09:25:00 KST', True), ('Thu 2026-10-22 00:25:00 UTC', True),
    ('Wed 2026-10-21 09:25:00 KST', False), ('Thu 2026-10-22 09:27:00 KST', False),
    ('n/a', False),
])
def test_oct22_next_elapse_requires_exact_date_and_time(m, value, accepted):
    if accepted:
        m.verify_next_elapse(value)
    else:
        with pytest.raises(m.GuardError, match='timer_next_elapse_mismatch'):
            m.verify_next_elapse(value)


@pytest.mark.parametrize('failure', [None, 'check', 'static', 'enable', 'post_enable',
    'backup_write', 'disk_before', 'disk_locked', 'disk_arm', 'disarm', 'partial_disable'])
def test_apply_preserves_exact_oct21_predecessor_and_disarms_its_partial_timer(m, tmp_path, monkeypatch, failure):
    """Reuse the mature transaction fault matrix with the Oct. 22 timer identity."""
    expected_timer = 'Thu 2026-10-22 09:25:00 KST'
    original = m.verify_next_elapse
    monkeypatch.setattr(m, 'verify_next_elapse', lambda _: original(expected_timer))
    oct21_transaction.test_offline_transaction_preserves_old_data_and_disarms_partial_failure(
        m, tmp_path, monkeypatch, failure)
