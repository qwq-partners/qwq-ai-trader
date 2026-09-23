"""복구 진단은 증거를 읽고 고정된 비식별 사실만 반환한다."""
import asyncio
import json
from dataclasses import FrozenInstanceError
from datetime import datetime, timezone

import pytest

from src.execution.safety.recovery_capture import capture_recovery_snapshot
from src.execution.safety.recovery_diagnostics import build_recovery_diagnostic
from test_execution_runtime import setup, opened

NOW = datetime(2026, 9, 23, tzinfo=timezone.utc)


def report(runtime):
    return build_recovery_diagnostic(capture_recovery_snapshot(runtime, captured_at=NOW))


def codes(result):
    return {row['code'] for row in result['findings']}


@pytest.fixture
def runtime(tmp_path):
    _, _, store, value = asyncio.run(setup(tmp_path))
    yield value
    asyncio.run(store.close())


def test_missing_runtime_does_not_authenticate_legacy():
    value = report(None)
    assert value['mode'] == 'unknown'
    assert value['snapshot_stable'] is None
    assert value['trading_ready'] is False
    assert 'snapshot_unavailable' in codes(value)


def test_supported_capture_is_frozen_detached_and_always_unverified(runtime):
    snapshot = capture_recovery_snapshot(runtime, captured_at=NOW)
    value = build_recovery_diagnostic(snapshot)
    assert value['mode'] == 'partial_install'
    assert value['snapshot_stable'] is True
    assert value['publication_consistent'] is True
    assert value['read_only'] is True
    assert value['automatic_action_allowed'] is False
    assert value['installation_verified'] is False
    assert 'disposition_not_durable' in codes(value)
    with pytest.raises(FrozenInstanceError):
        snapshot.mode = 'unknown'
    value['findings'][0]['code'] = 'changed'
    assert 'changed' not in codes(build_recovery_diagnostic(snapshot))


@pytest.mark.parametrize('source', [None, 'absent'])
def test_historical_delivered_protection_decision_without_link(runtime, source):
    row = dict(kind='protection_decision', status='delivered', symbol='PRIVATE_SENTINEL',
               intent_id='PRIVATE_SENTINEL', decision=['sell_all', 100, 'PRIVATE_SENTINEL'])
    if source != 'absent':
        row['effect_source'] = source
    runtime.owner._state['outbox']['PRIVATE_SENTINEL'] = row
    snapshot = capture_recovery_snapshot(runtime, captured_at=NOW)
    value = build_recovery_diagnostic(snapshot)
    assert 'protection_unsubmitted' in codes(value)
    assert 'PRIVATE_SENTINEL' not in json.dumps(value)
    assert 'PRIVATE_SENTINEL' not in repr(snapshot)


@pytest.mark.parametrize('field,value,expected', [
    ('command_status', 'unknown', 'unknown_buy'),
    ('observed_quantity', 1, 'observation_not_applied'),
    ('reserved_quantity', True, 'evidence_invalid'),
    ('reserved_quantity', -1, 'evidence_invalid'),
    ('reserved_cash', 'NaN', 'evidence_invalid'),
    ('reserved_cash', 'Infinity', 'evidence_invalid'),
    ('reserved_planned_risk', None, 'evidence_invalid'),
])
def test_attempt_evidence(runtime, field, value, expected):
    asyncio.run(opened(runtime, 'B1'))
    runtime.owner._state['attempts']['B1'][field] = value
    assert expected in codes(report(runtime))


def test_cash_only_terminal_reservation(runtime):
    asyncio.run(opened(runtime, 'B1'))
    row = runtime.owner._state['attempts']['B1']
    row.update(state='final_cancelled', reserved_quantity=0, reserved_cash='1')
    assert {'remaining_reservation', 'terminal_reservation'} <= codes(report(runtime))


def test_volatile_ram_without_version_change(runtime, monkeypatch):
    from src.execution.safety import recovery_capture as module
    original = module._read_sample
    calls = []

    def sample(value):
        result = original(value)
        if not calls:
            value._protection_recovery_counts['PRIVATE_SENTINEL'] = 1
        calls.append(1)
        return result

    monkeypatch.setattr(module, '_read_sample', sample)
    value = report(runtime)
    assert value['snapshot_stable'] is False
    assert 'snapshot_volatile' in codes(value)


def test_untrusted_hooks_never_run(runtime):
    class Hostile:
        def __deepcopy__(self, memo):
            raise AssertionError('hook called')
        def __repr__(self):
            raise AssertionError('hook called')
        def __eq__(self, other):
            raise AssertionError('hook called')
        def __str__(self):
            raise AssertionError('hook called')
        def __bool__(self):
            raise AssertionError('hook called')
    runtime.owner._state['hostile'] = Hostile()
    assert 'evidence_invalid' in codes(report(runtime))


def test_cycles_are_bounded(runtime):
    runtime.owner._state['cycle'] = runtime.owner._state
    assert 'evidence_invalid' in codes(report(runtime))


@pytest.mark.parametrize('stamp', [None, 'PRIVATE_SENTINEL', datetime(2026, 1, 1)])
def test_invalid_time_is_unavailable(stamp):
    value = build_recovery_diagnostic(capture_recovery_snapshot(None, captured_at=stamp))
    assert value['captured_at'] is None
    assert 'evidence_invalid' in codes(value)
