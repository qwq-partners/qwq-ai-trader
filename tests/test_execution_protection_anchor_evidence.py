"""A healthy replay anchor needs the original fill's independent evidence."""
import asyncio
from copy import deepcopy
from decimal import Decimal

import pytest

from src.execution.safety import protection_recovery as recovery
from src.execution.safety.economics import _decode_position
from src.execution.safety.protection import decode_protection, encode_protection
from test_execution_intraday_replay import degraded, refresh, reseal, invariant_roots
from test_execution_intraday_replay_integration import reopen
from test_execution_runtime import NOW, observed, queued


def test_capture_binds_original_fill_event_to_independent_outbox(tmp_path, monkeypatch):
    async def scenario():
        _, _, store, runtime, _, _ = await degraded(tmp_path, monkeypatch)
        try:
            state = runtime.owner.state
            event = state['protection_replay']['005930']['events'][0]
            assert state['outbox'][event['observation_id']]['protection_replay_digest'] == event['digest']
        finally:
            await runtime.shutdown()
            await store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('cold', [False, True])
def test_real_later_fill_does_not_prove_a_changed_healthy_before_scope(tmp_path, monkeypatch, cold):
    async def scenario():
        engine, exits, store, runtime, writer, ref = await degraded(tmp_path, monkeypatch)
        try:
            await refresh(writer, monkeypatch, -3)
            await runtime.quote('005930', Decimal('9700'))
            await refresh(writer, monkeypatch, 0)
            assert (await queued(engine, await observed(runtime, ref, 100, '1000000'))).protection_status == 'degraded'
            original = runtime.owner.state

            def change_history_only(state):
                event = state['protection_replay']['005930']['events'][-1]
                before = event['before']
                manager = decode_protection(before['protection'], clock=lambda: NOW)
                manager._execution_protection['degraded'].pop('005930')
                manager.register_position(_decode_position(before['position']))
                manager._execution_protection['orders'][ref.key] = {
                    'symbol': '005930', 'side': 'BUY', 'intent_id': event['intent_id'],
                    'kind': 'initial_entry', 'base_quantity': 0,
                    'cumulative_quantity': 40, 'reset_applied': False,
                }
                before['protection'] = encode_protection(manager)
                reseal(state)
                return state

            await runtime.owner.mutate('synthetic-altered-fill-before', change_history_only)
            if cold:
                engine, exits, store, runtime, _ = await reopen(engine, exits, store, runtime, [NOW])
            before = runtime.owner.state
            for key in original.keys() - {'protection_replay'}:
                assert before[key] == original[key], key
            receipt = await runtime.repair_protection('anchor-proof', '005930', expected_version=runtime.owner.version)
            assert receipt.status == 'BLOCKED', receipt.reason
            assert runtime.owner.state['protection'] == before['protection']
            invariant_roots(before, runtime.owner.state)
            assert runtime.trading_ready is False and engine._event_queue == []
        finally:
            await runtime.shutdown()
            await store.close()
    asyncio.run(scenario())


def test_missing_independent_fill_proof_is_not_backfilled_from_history(tmp_path, monkeypatch):
    async def scenario():
        _, _, store, runtime, _, _ = await degraded(tmp_path, monkeypatch)
        try:
            state = runtime.owner.state
            for event in state['protection_replay']['005930']['events']:
                state['outbox'][event['observation_id']].pop('protection_replay_digest', None)
            before = deepcopy(state)
            result = recovery.reduce_repair(state, 'old-unproven-anchor', '005930',
                expected_version=runtime.owner.version, state_version=runtime.owner.version)
            assert result['recovery_receipts']['old-unproven-anchor']['status'] == 'BLOCKED'
            assert result['protection'] == before['protection']
            invariant_roots(before, result)
        finally:
            await runtime.shutdown()
            await store.close()
    asyncio.run(scenario())


def test_actual_successful_repair_then_new_degradation_keeps_real_healthy_anchor(tmp_path, monkeypatch):
    async def scenario():
        engine, exits, store, runtime, writer, ref = await degraded(tmp_path, monkeypatch)
        try:
            await refresh(writer, monkeypatch, -3)
            first = await runtime.repair_protection('real-first-repair', '005930', expected_version=runtime.owner.version)
            assert first.status == 'APPLIED', first.reason
            await runtime.quote('005930', Decimal('10100'))
            next_fill = await observed(runtime, ref, 100, '1000000')

            def fail(*_args, **_kwargs):
                raise ValueError('synthetic next registration failure')

            with monkeypatch.context() as patch:
                patch.setattr('src.execution.safety.protection._registration', fail)
                assert (await queued(engine, next_fill)).protection_status == 'degraded'
            before = runtime.owner.state
            second = await runtime.repair_protection('real-second-repair', '005930', expected_version=runtime.owner.version)
            assert second.status == 'APPLIED', second.reason
            assert exits.get_state('005930').remaining_quantity == 100
            invariant_roots(before, runtime.owner.state)
            assert runtime.trading_ready is False
        finally:
            await runtime.shutdown()
            await store.close()
    asyncio.run(scenario())
