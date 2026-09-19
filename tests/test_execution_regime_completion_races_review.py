"""Independent C2 race review: real owner cancellation and unbound completion."""
import asyncio
from copy import deepcopy
from decimal import Decimal

import pytest

from src.core.market_regime import MarketRegimeAdapter
from src.execution.safety.application import ApplicationBlocked
from src.execution.safety.regime_owner import RegimeBaseline, RegimeOwner
from src.execution.safety.risk_sources import RiskSourceCoordinator, _ticket
from src.execution.safety.risk_transition import IntradayPolicyState
from src.risk.manager import RiskManager
from test_execution_runtime import NOW
from test_execution_two_minute_regime_owner import (
    _actual_kis,
    _baseline,
    _bullish_output,
    _installed,
    _reopen_installed,
)
from test_regime_transition_parity import _risk_config
from test_execution_runtime import setup


@pytest.mark.parametrize('phase', ('before_write', 'after_write'))
def test_actual_refresh_cancelled_during_index_seal_sql_drains_cancelled_terminal(tmp_path, monkeypatch, phase):
    """Fails if a caller cancel can strand the real index/expert source lifecycle."""
    async def scenario():
        _, _, store, runtime, _, _, owner = await _installed(tmp_path, monkeypatch)
        provider, _ = await _actual_kis(monkeypatch,
            {'0001': _bullish_output(), '1001': _bullish_output()})
        entered, release = asyncio.Event(), asyncio.Event()
        original, task, closing, restored = store.commit, None, None, None
        try:
            async def hold(version, state, command):
                if 'risk-seal:' not in command:
                    return await original(version, state, command)
                if phase == 'before_write':
                    entered.set()
                    await release.wait()
                    return await original(version, state, command)
                result = await original(version, state, command)
                entered.set()
                await release.wait()
                return result

            monkeypatch.setattr(store, 'commit', hold)
            task = asyncio.create_task(owner.refresh_trend(provider, expert_orchestrator=None))
            await asyncio.wait_for(entered.wait(), 2)
            index_id = runtime.owner.state['risk_sources']['latest']['index_trend']
            expert_id = runtime.owner.state['risk_sources']['latest']['expert_regime']
            expert_before = deepcopy(runtime.owner.state['risk_sources']['records'][expert_id]['terminal'])
            task.cancel()
            await asyncio.sleep(0)
            closing = asyncio.create_task(runtime.shutdown())
            await asyncio.sleep(0)
            assert not closing.done()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 2)
            await asyncio.wait_for(closing, 2)
            row = runtime.owner.state['risk_sources']['records'][index_id]
            assert row['terminal']['receipt']['status'] == 'cancelled'
            assert runtime.owner.state['risk_sources']['records'][expert_id]['terminal'] == expert_before
            assert runtime.owner.healthy and not runtime.health()['command_results_failed']

            _, _, store, restored, _, _, _ = await _reopen_installed(
                tmp_path, monkeypatch, store, runtime, clock=lambda: NOW)
            runtime = None
            row = restored.owner.state['risk_sources']['records'][index_id]
            assert row['terminal']['receipt']['status'] == 'cancelled'
            assert restored.owner.state['risk_sources']['records'][expert_id]['terminal'] == expert_before
            assert restored.owner.healthy and not restored.health()['command_results_failed']
        finally:
            release.set()
            await asyncio.gather(*(item for item in (task, closing) if item), return_exceptions=True)
            if runtime is not None and not runtime._closing:
                await runtime.shutdown()
            if restored is not None and not restored._closing:
                await restored.shutdown()
            await store.close()
    asyncio.run(scenario())


async def _prebaseline_runtime(tmp_path, monkeypatch):
    """Real prerequisites, intentionally before the one allowed baseline registration."""
    import src.risk.manager as risk_manager_module
    monkeypatch.setattr(risk_manager_module.Path, 'home', lambda: tmp_path)
    sidecar = RiskManager(_risk_config(), Decimal('2000000'))
    engine, _, store, runtime = await setup(tmp_path, account_scope='scope', risk_manager=sidecar)
    engine._regime_adapter = MarketRegimeAdapter()

    def seed(state):
        intraday = IntradayPolicyState('normal', 0.0, None, None).to_dict()
        state['intraday_policy'] = {'schema': 1, 'baseline': intraday,
            'baseline_version': runtime.owner.version + 1,
            'current': intraday.copy(), 'transitions': {}}
        state['entry_policy_effects'] = {'pending_sectors': {}, 'sidecar_active': True}
        return state

    await runtime.owner.mutate('race-review-prerequisites', seed)
    baseline = RegimeBaseline.from_dict(_baseline(runtime,
        intraday_version=runtime.owner.state['intraday_policy']['baseline_version']))
    return store, runtime, baseline


def test_foreign_index_success_held_before_owner_lock_rejects_after_baseline_registration(tmp_path, monkeypatch):
    """Fails if a pre-baseline foreign completion can poison a subsequently installed root."""
    async def scenario():
        store, runtime, baseline = await _prebaseline_runtime(tmp_path, monkeypatch)
        foreign = RiskSourceCoordinator(runtime)
        entered, release = asyncio.Event(), asyncio.Event()
        original, completion = runtime.owner.mutate, None
        try:
            ticket = await foreign.begin('foreign-before-baseline', 'index_trend')

            async def hold(command, reducer):
                if command.startswith('risk-complete:'):
                    entered.set()
                    await release.wait()
                return await original(command, reducer)

            monkeypatch.setattr(runtime.owner, 'mutate', hold)
            completion = asyncio.create_task(foreign.complete(ticket, 'success', {}))
            await asyncio.wait_for(entered.wait(), 2)
            await RegimeOwner.register_baseline(runtime, baseline, expected_version=runtime.owner.version)
            before = deepcopy(runtime.owner.state)
            release.set()
            with pytest.raises(ValueError):
                await asyncio.wait_for(completion, 2)
            assert runtime.owner.state == before
            assert (await store.load())[1] == before
            assert runtime.owner.state['risk_sources']['records'][ticket.operation_id]['terminal'] is None
            assert runtime.owner.healthy and not runtime.health()['command_results_failed']
        finally:
            release.set()
            await asyncio.gather(*(item for item in (completion,) if item), return_exceptions=True)
            if runtime.owner.healthy:
                await runtime.shutdown()
            await store.close()
    asyncio.run(scenario())


def test_bound_index_completion_unbound_between_preflight_and_reducer_rejects_before_sql(tmp_path, monkeypatch):
    """Fails if a mutable callback binding can become an unguarded accepted index transition."""
    async def scenario():
        _, _, store, runtime, _, _, owner = await _installed(tmp_path, monkeypatch)
        provider, _ = await _actual_kis(monkeypatch,
            {'0001': _bullish_output(), '1001': _bullish_output()})
        armed, unbound = False, asyncio.Event()
        original_commit, original_lookup = store.commit, store.lookup_commit
        try:
            async def arm(version, state, command):
                nonlocal armed
                result = await original_commit(version, state, command)
                if 'risk-seal:' in command:
                    armed = True
                return result

            async def unbind(command):
                if armed and command.startswith('command:risk-complete:') and not unbound.is_set():
                    owner.sources._completion_reducer = None
                    unbound.set()
                return await original_lookup(command)

            monkeypatch.setattr(store, 'commit', arm)
            monkeypatch.setattr(store, 'lookup_commit', unbind)
            before_policy = deepcopy(runtime.owner.state['regime_policy'])
            with pytest.raises(ValueError):
                await owner.refresh_trend(provider, expert_orchestrator=None)
            assert unbound.is_set()
            state = runtime.owner.state
            index_id = state['risk_sources']['latest']['index_trend']
            assert state['risk_sources']['records'][index_id]['terminal'] is None
            assert state['regime_policy'] == before_policy
            assert runtime.owner.healthy and not runtime.health()['command_results_failed']
        finally:
            if runtime.owner.healthy:
                await runtime.shutdown()
            await store.close()
    asyncio.run(scenario())


def test_actual_index_seal_sql_fault_remains_fail_closed(tmp_path, monkeypatch):
    """Cancellation controls must not normalize a real owner seal storage fault."""
    async def scenario():
        _, _, store, runtime, _, _, owner = await _installed(tmp_path, monkeypatch)
        provider, _ = await _actual_kis(monkeypatch,
            {'0001': _bullish_output(), '1001': _bullish_output()})
        original = store.commit
        try:
            async def fail(version, state, command):
                if 'risk-seal:' in command:
                    raise OSError('actual index seal storage fault')
                return await original(version, state, command)

            monkeypatch.setattr(store, 'commit', fail)
            with pytest.raises(OSError, match='actual index seal storage fault'):
                await owner.refresh_trend(provider, expert_orchestrator=None)
            assert not runtime.owner.healthy and runtime.health()['command_results_failed']
            with pytest.raises(ApplicationBlocked, match='command_result_drain_failed'):
                await runtime.shutdown()
        finally:
            await store.close()
    asyncio.run(scenario())


def test_foreign_retry_conflict_and_superseded_old_ticket_preserve_accepted_regime_fact(tmp_path, monkeypatch):
    """Controls that the I1 guard rejects only unsafe new foreign success, not history facts."""
    async def scenario():
        _, _, store, runtime, _, _, owner = await _installed(tmp_path, monkeypatch)
        provider, _ = await _actual_kis(monkeypatch,
            {'0001': _bullish_output(), '1001': _bullish_output()})
        foreign = RiskSourceCoordinator(runtime)
        try:
            old = await foreign.begin('foreign-old-index', 'index_trend')
            accepted = await owner.refresh_trend(provider, expert_orchestrator=None)
            row = runtime.owner.state['risk_sources']['records'][accepted.operation_id]
            original_terminal = deepcopy(row['terminal'])
            envelope = original_terminal['envelope']
            retry = await foreign.complete(_ticket(row), envelope['outcome'], envelope['payload'],
                source=envelope['source'], source_event_id=envelope['source_event_id'],
                received_at=None if envelope['received_at'] is None else __import__('datetime').datetime.fromisoformat(envelope['received_at']),
                market_as_of=None if envelope['market_as_of'] is None else __import__('datetime').datetime.fromisoformat(envelope['market_as_of']),
                classified_at=None if envelope['classified_at'] is None else __import__('datetime').datetime.fromisoformat(envelope['classified_at']),
                recovery_until=None if envelope['recovery_until'] is None else __import__('datetime').datetime.fromisoformat(envelope['recovery_until']))
            assert retry.status == 'accepted' and row['terminal'] == original_terminal
            conflict = await foreign.complete(_ticket(row), 'failed')
            assert conflict.status == 'conflict'
            row = runtime.owner.state['risk_sources']['records'][accepted.operation_id]
            assert row['terminal'] == original_terminal and row['conflict']['receipt']['status'] == 'conflict'
            stale = await foreign.complete(old, 'success', {})
            assert (stale.status, stale.reason) == ('stale', 'superseded')
            assert runtime.owner.healthy and not runtime.health()['command_results_failed']
        finally:
            if runtime.owner.healthy:
                await runtime.shutdown()
            await store.close()
    asyncio.run(scenario())
