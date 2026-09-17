"""Actual scheduler/HTTP adapter/core queue → policy history → repair; no hook mocks.

The explicit owned() baseline is synthetic, not permission for production startup.
"""
import asyncio
from copy import deepcopy
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from src.core.engine import UnifiedEngine
from src.core.types import TradingConfig
from src.execution.safety.application import ApplicationBlocked
from src.execution.safety.intraday_owner import IntradayRiskOwner
from src.execution.safety.runtime import KRExecutionRuntime
from src.execution.safety.store import ExecutionStateStore
from src.schedulers.kr_scheduler import KRScheduler
from src.strategies.exit_manager import ExitManager
from test_execution_intraday_owner import owned, batch_for
from test_execution_runtime import NOW, opened, observed, queued
from test_kis_index_observation import provider, raw


async def failed_fill(engine, runtime, monkeypatch):
    ref = await opened(runtime, 'B1')
    observation = await observed(runtime, ref, 40, '400000')

    def fail(*args, **kwargs):
        raise ValueError('synthetic initial registration failure')

    with monkeypatch.context() as patch:
        patch.setattr(ExitManager, 'register_position', fail)
        assert (await queued(engine, observation)).protection_status == 'degraded'
    return ref


async def refresh(engine, batch, monkeypatch, pct, clock):
    output = raw()
    if pct is None:
        output.pop('bstp_nmix_prdy_ctrt')
    else:
        output['bstp_nmix_prdy_ctrt'] = str(pct)
    market_data, calls = await provider(monkeypatch, output)
    monkeypatch.setattr(market_data, '_index_clock', lambda: clock[0])
    scheduler = object.__new__(KRScheduler)
    scheduler.bot = SimpleNamespace(engine=engine, batch_analyzer=batch, kis_market_data=market_data)
    receipt = await scheduler._refresh_intraday_risk()
    assert calls == {'get': 1, 'limiter': 1}
    return receipt


async def reopen(engine, exits, store, runtime, clock, *, failed_drain=False):
    path = store.path
    if failed_drain:
        with pytest.raises(ApplicationBlocked, match='command_result_drain_failed'):
            await runtime.shutdown()
    else:
        await runtime.shutdown()
    await store.close()
    engine = UnifiedEngine(TradingConfig(initial_capital=Decimal('2000000')))
    exits = ExitManager(persist=False, clock=lambda: clock[0])
    store = ExecutionStateStore(path)
    runtime = KRExecutionRuntime(store, engine, exits, account_scope='scope', clock=lambda: clock[0])
    await runtime.restore()
    runtime.attach()
    batch = batch_for(engine, exits)
    IntradayRiskOwner(runtime, batch)
    return engine, exits, store, runtime, batch


def unchanged_economics_and_sources(before, after):
    for key in ('portfolio', 'risk', 'intents', 'attempts', 'lots', 'outbox',
                'inbox', 'startup_reconciliation', 'risk_sources', 'intraday_policy'):
        assert after.get(key) == before.get(key), key
    for key in ('config', 'current_regime', 'intraday_crash_level'):
        assert after['protection'][key] == before['protection'][key], key


@pytest.mark.parametrize('pct', [-1.5, -3.0, -4.0])
@pytest.mark.parametrize('cold', [False, True])
def test_real_scheduler_policy_is_repairable_after_degraded_core_fill(tmp_path, monkeypatch, pct, cold):
    async def scenario():
        clock = [NOW]
        engine, exits, store, runtime, batch, _ = await owned(tmp_path, clock=lambda: clock[0])
        try:
            await failed_fill(engine, runtime, monkeypatch)
            clock[0] += timedelta(seconds=1)
            receipt = await refresh(engine, batch, monkeypatch, pct, clock)
            assert receipt.status == 'accepted'
            if cold:
                engine, exits, store, runtime, batch = await reopen(engine, exits, store, runtime, clock)
            before = runtime.owner.state
            version = runtime.owner.version
            repaired = await runtime.repair_protection('actual-policy-repair', '005930', expected_version=version)
            assert repaired.status == 'APPLIED', repaired.reason
            assert exits.get_state('005930').remaining_quantity == 40
            assert exits.get_state('005930').entry_price == Decimal('10000')
            unchanged_economics_and_sources(before, runtime.owner.state)
            assert (await store.load())[1] == runtime.owner.state
            assert not runtime.trading_ready
            assert not engine._event_queue
            repeated = await runtime.repair_protection('actual-policy-repair', '005930', expected_version=version)
            assert repeated.status == 'ALREADY_APPLIED'
        finally:
            await runtime.shutdown()
            await store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('quote_first', [False, True])
def test_real_scheduler_policy_replays_in_original_quote_order(tmp_path, monkeypatch, quote_first):
    async def scenario():
        clock = [NOW]
        engine, exits, store, runtime, batch, _ = await owned(tmp_path, clock=lambda: clock[0])
        try:
            await failed_fill(engine, runtime, monkeypatch)
            if quote_first:
                await runtime.quote('005930', Decimal('9600'))
            clock[0] += timedelta(seconds=1)
            await refresh(engine, batch, monkeypatch, -4, clock)
            if not quote_first:
                await runtime.quote('005930', Decimal('9600'))
            before = runtime.owner.state
            result = await runtime.repair_protection('ordered-repair', '005930', expected_version=runtime.owner.version)
            assert result.status == ('APPLIED' if quote_first else 'BLOCKED'), result.reason
            if not quote_first:
                assert result.reason == 'unrecorded_historical_decision'
            unchanged_economics_and_sources(before, runtime.owner.state)
            assert not runtime.trading_ready
        finally:
            await runtime.shutdown()
            await store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('committed', [False, True])
def test_policy_history_has_same_sql_commit_as_actual_risk_completion(tmp_path, monkeypatch, committed):
    async def scenario():
        clock = [NOW]
        engine, exits, store, runtime, batch, _ = await owned(tmp_path, clock=lambda: clock[0])
        await failed_fill(engine, runtime, monkeypatch)
        original = store.commit

        async def fail(expected, state, command):
            if 'risk-complete:' in command:
                if committed:
                    await original(expected, state, command)
                raise OSError('synthetic policy commit failure')
            return await original(expected, state, command)

        try:
            with monkeypatch.context() as patch:
                patch.setattr(store, 'commit', fail)
                with pytest.raises(OSError, match='synthetic policy commit failure'):
                    await refresh(engine, batch, monkeypatch, -3, clock)
            assert not runtime.owner.healthy
            # Cold restore uses only what the failed call actually committed.
            engine, exits, store, runtime, batch = await reopen(
                engine, exits, store, runtime, clock, failed_drain=True)
            before = runtime.owner.state
            assert before['protection']['intraday_crash_level'] == ('crash' if committed else 'normal')
            result = await runtime.repair_protection('after-sql-failure', '005930', expected_version=runtime.owner.version)
            assert result.status == 'APPLIED', result.reason
            unchanged_economics_and_sources(before, runtime.owner.state)
            assert not runtime.trading_ready
        finally:
            await runtime.shutdown()
            await store.close()
    asyncio.run(scenario())


def test_missing_index_after_policy_does_not_invent_recovery_during_repair(tmp_path, monkeypatch):
    async def scenario():
        clock = [NOW]
        engine, exits, store, runtime, batch, _ = await owned(tmp_path, clock=lambda: clock[0])
        try:
            await failed_fill(engine, runtime, monkeypatch)
            await refresh(engine, batch, monkeypatch, -3, clock)
            history = deepcopy(runtime.owner.state['protection_replay'])
            clock[0] += timedelta(seconds=1)
            assert (await refresh(engine, batch, monkeypatch, None, clock)).status == 'missing'
            assert runtime.owner.state['protection_replay'] == history
            before = runtime.owner.state
            result = await runtime.repair_protection('missing-after-crash', '005930', expected_version=runtime.owner.version)
            assert result.status == 'APPLIED', result.reason
            assert exits._intraday_crash_level == batch._intraday_state == 'crash'
            assert batch._intraday_recovery_until is None
            unchanged_economics_and_sources(before, runtime.owner.state)
        finally:
            await runtime.shutdown()
            await store.close()
    asyncio.run(scenario())
