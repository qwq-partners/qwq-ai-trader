"""N1 retained-source request validation at the real owner/SQLite boundary."""
import asyncio
from contextlib import suppress
from copy import deepcopy
from datetime import timedelta
import json

import pytest

from src.execution.safety.application import ApplicationBlocked
from test_execution_risk_input_seal import fixture
from test_execution_runtime import NOW


async def _seal_mode(runtime, mode):
    """Return one legacy and one registered-versioned seal shape."""
    if mode == 'value_only':
        return {'policy_reads': ('protection.config',)}
    await runtime.owner.register_policy_generations(
        'register-retained-' + mode, ('protection.config',))
    return {'versioned_policy_reads': ('protection.config',)}


async def _invalid_retained_request(source, kind):
    if kind == 'missing_operation':
        return ('missing-operation',), ()
    if kind == 'wrong_declared_lane':
        retained = await source.begin('retained-wrong-lane', 'vix_regime')
        assert (await source.complete(retained, 'success', {'value': 14.0})).status == 'accepted'
        return (retained.operation_id,), ('index_trend',)
    if kind == 'pending_terminal':
        retained = await source.begin('retained-pending', 'vix_regime')
        return (retained.operation_id,), ('vix_regime',)
    if kind == 'failed_terminal':
        retained = await source.begin('retained-failed', 'vix_regime')
        assert (await source.complete(retained, 'failed', {})).status == 'failed'
        return (retained.operation_id,), ('vix_regime',)
    if kind == 'stale_terminal':
        retained = await source.begin('retained-stale', 'vix_regime')
        await source.begin('replacement-vix', 'vix_regime')
        assert (await source.complete(retained, 'success', {})).status == 'stale'
        return (retained.operation_id,), ('vix_regime',)
    raise AssertionError('unknown invalid retained request')


@pytest.mark.parametrize('mode', ('value_only', 'versioned'))
@pytest.mark.parametrize('kind', (
    'missing_operation', 'wrong_declared_lane', 'pending_terminal',
    'failed_terminal', 'stale_terminal',
))
def test_first_valid_seal_rejects_invalid_retained_request_without_result_failure(tmp_path, mode, kind):
    """Would fail if an invalid retained read reaches the detached result task."""
    async def scenario():
        _, _, store, runtime, source = await fixture(tmp_path)
        try:
            options = await _seal_mode(runtime, mode)
            retained, lanes = await _invalid_retained_request(source, kind)
            ticket = await source.begin('model-' + mode + '-' + kind, 'llm_regime', require_seal=True)
            before = deepcopy(await store.load())

            with pytest.raises(ValueError, match='invalid_retained_input_source'):
                await source.seal(ticket, retained_sources=retained, source_lanes=lanes,
                                  inputs={'case': kind}, **options)

            assert await store.load() == before
            assert runtime.owner.healthy
            await runtime.restore()
            assert runtime.owner.healthy
            await runtime.shutdown()
        finally:
            with suppress(ApplicationBlocked):
                await runtime.shutdown()
            await store.close()

    asyncio.run(scenario())


@pytest.mark.parametrize('mode', ('value_only', 'versioned'))
def test_outer_command_scope_rejects_malformed_retained_seal_without_result_failure(tmp_path, mode):
    """Would fail if a caller-owned scope let a malformed retained request latch its result task."""
    async def scenario():
        _, _, store, runtime, source = await fixture(tmp_path)
        try:
            options = await _seal_mode(runtime, mode)
            retained = await source.begin('retained-' + mode, 'vix_regime')
            assert (await source.complete(retained, 'success', {'value': 14.0})).status == 'accepted'
            ticket = await source.begin('model-' + mode, 'llm_regime', require_seal=True)
            before = deepcopy(await store.load())

            with runtime.command_scope() as token:
                with pytest.raises(ValueError, match='invalid_retained_input_source'):
                    await source.seal(ticket, retained_sources=(retained.operation_id,),
                                      source_lanes=('index_trend',), inputs={'case': 'outer-scope'},
                                      scope_token=token, **options)

            assert await store.load() == before
            assert runtime.owner.healthy
            await runtime.restore()
            assert runtime.owner.healthy
            await runtime.shutdown()
        finally:
            with suppress(ApplicationBlocked):
                await runtime.shutdown()
            await store.close()

    asyncio.run(scenario())


@pytest.mark.parametrize('mode', ('value_only', 'versioned'))
def test_existing_seal_with_different_invalid_retained_request_records_conflict(tmp_path, mode):
    """Would fail if first-seal validation wrongly bypassed the durable conflict ledger."""
    async def scenario():
        _, _, store, runtime, source = await fixture(tmp_path)
        try:
            options = await _seal_mode(runtime, mode)
            ticket = await source.begin('model-' + mode, 'llm_regime', require_seal=True)
            original = await source.seal(ticket, inputs={'revision': 1}, **options)
            result = await source.seal(ticket, retained_sources=('missing-operation',),
                                       inputs={'revision': 2}, **options)
            row = runtime.owner.state['risk_input_seals']['records'][ticket.operation_id]
            assert (result.status, result.reason) == ('conflict', 'reseal_conflict')
            assert row['original']['seal_digest'] == original.seal_digest
            assert row['conflict']['request']['retained_sources'] == ['missing-operation']
            assert runtime.owner.healthy
            await runtime.shutdown()
        finally:
            with suppress(ApplicationBlocked):
                await runtime.shutdown()
            await store.close()

    asyncio.run(scenario())


@pytest.mark.parametrize('mode', ('value_only', 'versioned'))
@pytest.mark.parametrize('stale_kind', ('superseded', 'day_or_generation'))
def test_already_stale_seal_with_invalid_retained_request_keeps_stale_ledger(tmp_path, mode, stale_kind):
    """Would fail if retained validation ran before an already-stale seal is recorded."""
    async def scenario():
        clock = [NOW]
        _, _, store, runtime, source = await fixture(tmp_path, clock=lambda: clock[0])
        try:
            options = await _seal_mode(runtime, mode)
            ticket = await source.begin('model-' + mode + '-' + stale_kind, 'llm_regime', require_seal=True)
            if stale_kind == 'superseded':
                await source.begin('newest-' + mode, 'llm_regime', require_seal=True)
            else:
                clock[0] += timedelta(days=1)

            receipt = await source.seal(ticket, retained_sources=('missing-operation',),
                                        inputs={'case': stale_kind}, **options)
            record = runtime.owner.state['risk_input_seals']['records'][ticket.operation_id]['original']
            assert (receipt.status, receipt.reason) == ('stale', stale_kind)
            assert record['reads'] == {}
            assert runtime.owner.healthy
            await runtime.shutdown()
        finally:
            with suppress(ApplicationBlocked):
                await runtime.shutdown()
            await store.close()

    asyncio.run(scenario())


@pytest.mark.parametrize('mode', ('value_only', 'versioned'))
def test_same_day_accepted_retained_source_is_valid_control(tmp_path, mode):
    """Would fail if the request guard rejected an accepted retained source."""
    async def scenario():
        _, _, store, runtime, source = await fixture(tmp_path)
        try:
            options = await _seal_mode(runtime, mode)
            retained = await source.begin('retained-' + mode, 'vix_regime')
            assert (await source.complete(retained, 'success', {'value': 14.0})).status == 'accepted'
            ticket = await source.begin('model-' + mode, 'llm_regime', require_seal=True)
            receipt = await source.seal(ticket, retained_sources=(retained.operation_id,),
                                        source_lanes=('vix_regime',), inputs={}, **options)
            facts = json.loads(receipt.reads_json)
            assert facts['retained'][retained.operation_id]['terminal']['receipt']['status'] == 'accepted'
            assert (await source.complete(ticket, 'success', {})).status == 'accepted'
            assert runtime.owner.healthy
            await runtime.shutdown()
        finally:
            with suppress(ApplicationBlocked):
                await runtime.shutdown()
            await store.close()

    asyncio.run(scenario())
