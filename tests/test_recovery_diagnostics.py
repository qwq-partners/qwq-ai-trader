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


@pytest.mark.parametrize('field,value', [
    ('mode', 'PRIVATE_SENTINEL'), ('execution_version', True),
    ('captured_at', 'PRIVATE_SENTINEL'), ('findings', (('PRIVATE_SENTINEL', 1),)),
])
def test_dto_rejects_unapproved_output_fields(field, value):
    from src.execution.safety.recovery_diagnostics import RecoverySnapshot
    fields = dict(captured_at=NOW.isoformat())
    fields[field] = value
    with pytest.raises(ValueError, match='invalid_diagnostic_snapshot'):
        RecoverySnapshot(**fields)


@pytest.mark.parametrize('field,value', [
    ('_day_generation', True), ('_reconciler_target_count', -1),
    ('_protection_unattributed_failed', 1), ('_protection_recovery_counts', {'any': True}),
])
def test_malformed_runtime_counter_cannot_look_valid(runtime, field, value):
    setattr(runtime, field, value)
    assert 'evidence_invalid' in codes(report(runtime))


def test_pending_owner_cross_symbol_is_inconsistent(runtime):
    asyncio.run(opened(runtime, 'S1', side='sell'))
    runtime.owner._state['protection']['pending_owners']['OTHER'] = 'S1'
    assert 'protection_link_inconsistent' in codes(report(runtime))


def test_cancel_ack_is_not_parent_finality(runtime):
    from src.execution.safety.lifecycle import CommandKind, CommandResult, CommandStatus

    async def prepare():
        ref = await opened(runtime, 'B1')
        await runtime.lifecycle.prepare('B1', 'C1', 100, '005930', 'buy',
            command=CommandKind.CANCEL, parent_attempt_id='B1', order_ref=ref)
        await runtime.lifecycle.claim('C1', 'cancel-sender')
        await runtime.lifecycle.record_result('C1', 'cancel-sender',
            CommandResult(CommandStatus.ACKNOWLEDGED, 'C1', ref))

    asyncio.run(prepare())
    assert 'cancel_unconfirmed' in codes(report(runtime))
    runtime.owner._state['attempts']['C1']['symbol'] = 'OTHER'
    assert 'attempt_link_inconsistent' in codes(report(runtime))


def test_unapplied_inbox_and_publication_are_independent(runtime):
    runtime.owner._state['inbox'] = {'private': {'status': 'RECEIVED'}}
    runtime.owner._published_version -= 1
    value = report(runtime)
    assert value['snapshot_stable'] is True
    assert value['publication_consistent'] is False
    assert {'publication_inconsistent', 'owner_health_unconfirmed', 'unapplied_inbox'} <= codes(value)


def test_unknown_risk_never_becomes_zero_reservation_evidence(runtime):
    asyncio.run(opened(runtime, 'B1'))
    runtime.owner._state['attempts']['B1'].update(
        reserved_quantity=0, reserved_cash='0', reserved_exposure='0', reserved_planned_risk=None)
    assert 'evidence_invalid' in codes(report(runtime))


def test_external_source_is_not_general_protection(runtime):
    runtime.owner._state['outbox']['private'] = dict(kind='protection_decision',
        status='delivered', symbol='private', intent_id='private', decision=['sell_all', 1, 'x'],
        effect_source='intraday_preemptive')
    assert 'protection_unsubmitted' not in codes(report(runtime))


def test_publication_recovery_latch_is_inconsistent(runtime):
    runtime.owner._publication_recovery_required = True
    assert report(runtime)['publication_consistent'] is False


def test_unsupported_protection_source_is_invalid(runtime):
    runtime.owner._state['outbox']['private'] = dict(kind='protection_decision', effect_source='unknown')
    assert 'evidence_invalid' in codes(report(runtime))


def test_unprotected_position_is_quantity_inconsistent(runtime):
    runtime.owner._state['portfolio']['positions']['private'] = {'quantity': 10}
    assert 'protection_quantity_inconsistent' in codes(report(runtime))


def test_invalid_order_state_cannot_be_pending_sell(runtime):
    asyncio.run(opened(runtime, 'S1', side='sell'))
    runtime.owner._state['attempts']['S1']['state'] = 'arbitrary'
    value = report(runtime)
    assert 'evidence_invalid' in codes(value)
    assert 'pending_sell' not in codes(value)


def test_unsupported_type_metaclass_is_not_compared(runtime):
    class Meta(type):
        def __eq__(cls, other):
            raise AssertionError('metaclass comparison executed')
    class Hostile(metaclass=Meta):
        pass
    runtime.owner._state['hostile'] = Hostile()
    assert 'evidence_invalid' in codes(report(runtime))


@pytest.mark.parametrize('adapted', ['episode', 'ingress', 'lock'])
def test_exact_adapter_rejects_dict_subclass_without_hooks(runtime, adapted):
    from decimal import Decimal
    from src.execution.safety.application import IngressContext
    from src.execution.safety.protection_producer import ProtectionProducer, _Episode

    class HostileDict(dict):
        def __getitem__(self, key):
            raise AssertionError('mapping hook executed')

    if adapted == 'episode':
        producer = ProtectionProducer(runtime, clock=lambda: NOW, indicator_source=None)
        runtime._protection_producer = producer
        obj = _Episode('intent', ('sell_all', 1, 'reason'), Decimal(1), 'ws')
        producer._episodes['symbol'] = obj
    elif adapted == 'ingress':
        obj = IngressContext(1, 0, NOW)
        runtime.engine._execution_ingress[1] = {'context': obj}
    else:
        obj = runtime.owner._lock
    original = obj.__dict__
    object.__setattr__(obj, '__dict__', HostileDict(original))
    try:
        assert 'evidence_invalid' in codes(report(runtime))
    finally:
        object.__setattr__(obj, '__dict__', original)


@pytest.mark.parametrize('field,value', [
    ('mode', 'PRIVATE_SENTINEL'), ('captured_at', 'PRIVATE_SENTINEL'),
    ('findings', (('PRIVATE_SENTINEL', 1),)),
    ('findings', (('unknown_buy', 1), ('unknown_buy', 2))),
])
def test_builder_revalidates_bypassed_snapshot(field, value):
    from src.execution.safety.recovery_diagnostics import RecoverySnapshot
    snapshot = RecoverySnapshot(NOW.isoformat())
    object.__setattr__(snapshot, field, value)
    result = build_recovery_diagnostic(snapshot)
    assert {'snapshot_unavailable', 'evidence_invalid'} <= codes(result)
    assert 'PRIVATE_SENTINEL' not in json.dumps(result)


@pytest.mark.parametrize('field,value', [
    ('account_scope', 'other'), ('market', 'US'), ('order_date', '2026-09-19'),
    ('exchange', 'NXT'), ('parent_order_no', 'OTHER'), ('whole', None), ('whole', 'bad'),
])
def test_cancel_command_reference_does_not_inherit_unrelated_parent_finality(runtime, field, value):
    from copy import deepcopy
    asyncio.run(opened(runtime, 'B1'))
    parent = runtime.owner._state['attempts']['B1']
    parent.update(state='final_cancelled', reserved_quantity=0, reserved_cash='0')
    child = deepcopy(parent)
    command_ref = dict(parent['order_ref'], order_no='C1', parent_order_no='B1')
    if field == 'whole':
        command_ref = value
    else:
        command_ref[field] = value
    child.update(attempt_id='C1', kind='cancel', parent_attempt_id='B1',
                 command_ref=command_ref, state='cancel_requested')
    runtime.owner._state['attempts']['C1'] = child
    runtime.owner._state['intents']['B1']['attempt_ids'].append('C1')
    result = report(runtime)
    assert 'cancel_unconfirmed' in codes(result)
    if command_ref is not None:
        assert 'attempt_link_inconsistent' in codes(result)


def test_cancel_child_order_number_can_differ_from_parent(runtime):
    from copy import deepcopy
    asyncio.run(opened(runtime, 'B1'))
    parent = runtime.owner._state['attempts']['B1']
    parent.update(state='final_cancelled', reserved_quantity=0, reserved_cash='0')
    child = deepcopy(parent)
    child.update(attempt_id='C1', kind='cancel', parent_attempt_id='B1', state='cancel_requested',
                 command_ref=dict(parent['order_ref'], order_no='CHILD', parent_order_no='B1'))
    runtime.owner._state['attempts']['C1'] = child
    runtime.owner._state['intents']['B1']['attempt_ids'].append('C1')
    assert 'attempt_link_inconsistent' not in codes(report(runtime))


@pytest.mark.parametrize('before,after', [(1, True), (NOW, NOW.isoformat()), (1, 1.0)])
def test_two_sample_comparison_preserves_scalar_types(runtime, monkeypatch, before, after):
    from src.execution.safety import recovery_capture as module
    runtime.owner._state['diagnostic_test_metadata'] = before
    original = module._read_sample
    calls = []

    def sample(value):
        result = original(value)
        if not calls:
            value.owner._state['diagnostic_test_metadata'] = after
        calls.append(1)
        return result

    monkeypatch.setattr(module, '_read_sample', sample)
    value = report(runtime)
    assert value['snapshot_stable'] is False
    assert 'snapshot_volatile' in codes(value)


def test_task_codec_cannot_collide_with_user_tuple(runtime, monkeypatch):
    from src.execution.safety import recovery_capture as module
    original = module._read_sample

    async def scenario():
        task = asyncio.create_task(asyncio.sleep(0))
        runtime.owner._state['diagnostic_test_metadata'] = task
        calls = []

        def sample(value):
            result = original(value)
            if not calls:
                value.owner._state['diagnostic_test_metadata'] = ('task', id(task), False, False)
            calls.append(1)
            return result

        monkeypatch.setattr(module, '_read_sample', sample)
        try:
            value = report(runtime)
            assert value['snapshot_stable'] is False
        finally:
            await task

    asyncio.run(scenario())


def synthetic_zone(key):
    from io import BytesIO
    import struct
    from zoneinfo import ZoneInfo
    data = (b'TZif\0' + bytes(15) + struct.pack('>6l', 0, 0, 0, 0, 1, 4)
            + struct.pack('>lBB', 0, 0, 0) + b'UTC\0')
    return ZoneInfo.from_file(BytesIO(data), key=key)


@pytest.mark.parametrize('kind', ['hostile', 'oversized'])
def test_zoneinfo_metadata_never_bypasses_exact_type_or_size(runtime, kind):
    from src.execution.safety.recovery_capture import MAX_TEXT

    class Hostile:
        def __eq__(self, other):
            raise AssertionError('zone key equality hook')
        def __str__(self):
            raise AssertionError('zone key string hook')
        def __repr__(self):
            raise AssertionError('zone key repr hook')

    key = Hostile() if kind == 'hostile' else 'x' * (MAX_TEXT + 1)
    runtime._reconciler_started_at = NOW.replace(tzinfo=synthetic_zone(key))
    value = report(runtime)
    assert value['snapshot_stable'] is None
    assert 'evidence_invalid' in codes(value)


def test_inflight_mutation_marks_counts_incomplete(runtime):
    async def scenario():
        await runtime.owner._lock.acquire()
        try:
            runtime.owner._block()
            value = report(runtime)
            assert value['mutation_in_flight'] is True
            assert value['counts_complete'] is False
            assert value['snapshot_stable'] is True
            assert value['publication_consistent'] is False
        finally:
            runtime.owner._lock.release()

    asyncio.run(scenario())


def test_clean_capture_counts_are_complete_within_supported_scope(runtime):
    value = report(runtime)
    assert value['mutation_in_flight'] is False
    assert value['counts_complete'] is True


@pytest.mark.parametrize('failure', ['invalid', 'volatile'])
def test_unconfirmed_sample_has_unknown_mode_and_incomplete_counts(runtime, monkeypatch, failure):
    from src.execution.safety import recovery_capture as module
    if failure == 'invalid':
        runtime.owner._state['invalid'] = object()
    else:
        original = module._read_sample

        def sample(value):
            result = original(value)
            value._day_generation += 1
            return result

        monkeypatch.setattr(module, '_read_sample', sample)
    value = report(runtime)
    assert value['mode'] == 'unknown'
    assert value['mutation_in_flight'] is None
    assert value['counts_complete'] is False


def test_absent_market_handler_key_is_partial_wiring(runtime):
    from src.core.event import EventType
    runtime.engine._handlers.pop(EventType.MARKET_DATA)
    value = report(runtime)
    assert value['mode'] == 'partial_install'
    assert value['snapshot_stable'] is True


@pytest.mark.parametrize('kind', ['utc', 'kst', 'zone_without_key', 'naive', 'subclass', 'unknown_tz'])
def test_known_ram_datetime_types_are_explicit(runtime, kind):
    from datetime import tzinfo
    from zoneinfo import ZoneInfo

    class DateSubclass(datetime):
        pass

    class UnknownTimezone(tzinfo):
        def utcoffset(self, value):
            raise AssertionError('unknown timezone callback')

    stamps = dict(utc=NOW, kst=NOW.astimezone(ZoneInfo('Asia/Seoul')),
                  zone_without_key=NOW.replace(tzinfo=synthetic_zone(None)),
                  naive=NOW.replace(tzinfo=None),
                  subclass=DateSubclass(2026, 9, 23, tzinfo=timezone.utc),
                  unknown_tz=NOW.replace(tzinfo=UnknownTimezone()))
    runtime._reconciler_started_at = stamps[kind]
    value = report(runtime)
    if kind in ('utc', 'kst', 'zone_without_key'):
        assert value['snapshot_stable'] is True
    else:
        assert 'evidence_invalid' in codes(value)


@pytest.mark.parametrize('budget', ['MAX_DEPTH', 'MAX_NODES', 'MAX_TEXT', 'MAX_TOTAL_TEXT', 'MAX_INTEGER_BITS'])
def test_capture_budget_rejection_is_fixed_and_unavailable(runtime, monkeypatch, budget):
    from src.execution.safety import recovery_capture as module
    monkeypatch.setattr(module, budget, 1)
    value = report(runtime)
    assert value['snapshot_stable'] is None
    assert 'evidence_invalid' in codes(value)


@pytest.mark.parametrize('budget', ['text', 'integer', 'depth', 'nodes', 'aggregate'])
def test_copier_real_budget_edges(budget):
    from src.execution.safety import recovery_capture as module
    if budget == 'text':
        accepted, rejected = 'a' * module.MAX_TEXT, 'a' * (module.MAX_TEXT + 1)
    elif budget == 'integer':
        accepted, rejected = (1 << module.MAX_INTEGER_BITS) - 1, 1 << module.MAX_INTEGER_BITS
    elif budget == 'depth':
        accepted = None
        for _ in range(module.MAX_DEPTH):
            accepted = [accepted]
        rejected = [accepted]
    elif budget == 'nodes':
        accepted, rejected = [None] * (module.MAX_NODES - 1), [None] * module.MAX_NODES
    else:
        count, remain = divmod(module.MAX_TOTAL_TEXT, module.MAX_TEXT)
        accepted = ['a' * module.MAX_TEXT] * count + ['b' * remain]
        rejected = accepted + ['x']
    module._Copier().copy(accepted)
    with pytest.raises(ValueError, match='invalid_capture_evidence'):
        module._Copier().copy(rejected)


@pytest.mark.parametrize('kind', ['decimal', 'datetime'])
def test_derived_scalar_evidence_consumes_aggregate_budget(monkeypatch, kind):
    from decimal import Decimal
    from src.execution.safety import recovery_capture as module
    monkeypatch.setattr(module, 'MAX_TOTAL_TEXT', 4)
    value = Decimal('12345') if kind == 'decimal' else NOW
    with pytest.raises(ValueError, match='invalid_capture_evidence'):
        module._Copier().copy(value)
