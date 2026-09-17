"""A2b cold restart 및 원관측과 보조 지표 경계의 실제 클래스 회귀."""
import asyncio
from contextlib import suppress
from decimal import Decimal as D

import pytest

from src.core.engine import UnifiedEngine
from src.core.types import TradingConfig
from src.execution.safety.application import ApplicationBlocked
from src.execution.safety.runtime import KRExecutionRuntime
from src.execution.safety.store import ExecutionStateStore
from src.strategies.exit_manager import ExitManager
from test_execution_market_source import market_event
from test_execution_runtime import setup, opened, observed, queued


async def cold_runtime(runtime):
    await runtime.shutdown()
    await runtime.owner.store.close()
    engine = UnifiedEngine(TradingConfig(initial_capital=D('2000000')))
    fresh = KRExecutionRuntime(ExecutionStateStore(runtime.owner.store.path), engine,
        ExitManager(persist=False, clock=runtime.clock), clock=runtime.clock,
        account_scope=runtime.account_scope)
    await fresh.restore()
    fresh.attach()
    return fresh


@pytest.mark.parametrize('kind', ['bound', 'explicit', 'unknown', 'bound_then_unknown'])
@pytest.mark.parametrize('price', ['9600', '10500'])
def test_accepted_price_view_is_durable_not_process_cache(tmp_path, monkeypatch, kind, price):
    async def scenario():
        engine, exits, store, runtime = await setup(tmp_path, account_scope='scope')
        fresh = None
        try:
            ref = await opened(runtime, 'B1')
            await queued(engine, await observed(runtime, ref, 100, '1000000'))
            if kind in ('bound', 'bound_then_unknown'):
                event = await market_event(monkeypatch, runtime._now(), price=price)
                await runtime.observe_market(event)
            elif kind == 'explicit':
                await runtime.quote('005930', D(price), market_as_of=runtime._now(),
                                    source='synthetic', source_event_id='one')
            else:
                await runtime.quote('005930', D(price))
            expected_price = D(price)
            if kind == 'bound_then_unknown':
                expected_price += 1
                await runtime.quote('005930', expected_price)
            expected = (engine.portfolio.total_equity, engine.portfolio.effective_daily_pnl)
            assert engine.portfolio.positions['005930'].current_price == expected_price
            state = runtime.owner.state
            fresh = await cold_runtime(runtime)
            assert fresh.owner.state == state
            assert fresh.engine.portfolio.positions['005930'].current_price == expected_price
            assert (fresh.engine.portfolio.total_equity, fresh.engine.portfolio.effective_daily_pnl) == expected
            assert not fresh.trading_ready
        finally:
            with suppress(ApplicationBlocked): await runtime.shutdown()
            await store.close()
            if fresh is not None:
                await fresh.shutdown()
                await fresh.owner.store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('field', ['low', 'high', 'open', 'close'])
def test_supplemental_ohlc_conflict_is_rejected_before_admission(tmp_path, monkeypatch, field):
    async def scenario():
        _, _, store, runtime = await setup(tmp_path, account_scope='scope')
        try:
            event = await market_event(monkeypatch, runtime._now())
            state, version = runtime.owner.state, runtime.owner.version
            with pytest.raises(ValueError, match='market.*(conflict|mismatch)'):
                await runtime.observe_market(event, market_data={field: '9999'})
            assert runtime.owner.state == state and runtime.owner.version == version
            assert not runtime._protection_tasks and not runtime._protection_failed
        finally:
            await runtime.shutdown()
            await store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('include_low', [False, True])
def test_first_stage_composite_uses_original_low_with_independent_previous_low(tmp_path, monkeypatch, include_low):
    async def scenario():
        engine, exits, store, runtime = await setup(tmp_path, account_scope='scope')
        try:
            ref = await opened(runtime, 'B1')
            await queued(engine, await observed(runtime, ref, 100, '1000000'))
            take = await market_event(monkeypatch, runtime._now(), price='11200')
            decision = await runtime.observe_market(take, intent_id='first')
            assert decision[:2] == ('sell_partial', 10)
            sell = await opened(runtime, 'S1', side='sell', quantity=10, intent='first')
            await queued(engine, await observed(runtime, sell, 10, '112000', side='sell', total=10))
            assert exits.get_state('005930').current_stage.value == 'first'
            assert exits.config.composite_trail_min_stage == 'first'
            event = await market_event(monkeypatch, runtime._now(), price='11100')
            supplemental = {'prev_low': 11150, 'ma5': None}
            if include_low:
                supplemental['low'] = 11100
            decision = await runtime.observe_market(event, intent_id='composite', market_data=supplemental)
            assert decision is not None and decision[:2] == ('sell_all', 90)
            assert '복합트레일링' in decision[2]
        finally:
            await runtime.shutdown()
            await store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('fault', ['missing', 'bool_version', 'future_version', 'wrong_price',
    'naive_receipt', 'future_market', 'unknown_source', 'source_conflict', 'missing_low', 'wrong_low'])
def test_restore_rejects_incoherent_price_view_or_source_decision_input(tmp_path, monkeypatch, fault):
    async def scenario():
        from src.execution.safety.market_source import digest
        _, _, store, runtime = await setup(tmp_path, account_scope='scope')
        try:
            event = await market_event(monkeypatch, runtime._now())
            await runtime.observe_market(event)
            state = runtime.owner.state
            view = state['quote_price_views'][event.symbol]
            if fault == 'missing': del state['quote_price_views'][event.symbol]
            elif fault == 'bool_version': view['source_version'] = True
            elif fault == 'future_version': view['source_version'] = runtime.owner.version + 10
            elif fault == 'wrong_price': view['price'] = '9999'
            elif fault == 'naive_receipt': view['received_at'] = runtime._now().replace(tzinfo=None).isoformat()
            elif fault == 'future_market': view['market_as_of'] = runtime._now().replace(hour=15).isoformat()
            elif fault == 'unknown_source': view['market_as_of'] = None
            elif fault == 'source_conflict': view['source'] = 'rest-relabelled'
            else:
                proof = state['market_sources'][event.symbol]
                data = proof['request']['market_data']
                if fault == 'missing_low': del data['low']
                else: data['low'] = '9999'
                proof['request_digest'] = digest(proof['request'])
            await store.commit(runtime.owner.version, state, 'synthetic-corruption-'+fault)
            with pytest.raises(ApplicationBlocked): await runtime.restore()
            assert not runtime.owner.healthy and not runtime.trading_ready
        finally:
            with suppress(ApplicationBlocked): await runtime.shutdown()
            await store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('new_position', [False, True])
def test_old_market_duplicate_cannot_revalue_later_fill_or_new_position(tmp_path, monkeypatch, new_position):
    async def scenario():
        engine, exits, store, runtime = await setup(tmp_path, account_scope='scope')
        fresh = None
        try:
            buy = await opened(runtime, 'B1')
            await queued(engine, await observed(runtime, buy, 100, '1000000'))
            event = await market_event(monkeypatch, runtime._now(), price='10500')
            assert await runtime.observe_market(event) is None
            qty = 100 if new_position else 10
            sell = await opened(runtime, 'S1', side='sell', quantity=qty)
            await queued(engine, await observed(runtime, sell, qty, str(qty * 10400), side='sell', total=qty))
            expected = D('10400')
            if new_position:
                buy2 = await opened(runtime, 'B2')
                await queued(engine, await observed(runtime, buy2, 100, '990000'))
                expected = D('9900')
            assert engine.portfolio.positions['005930'].current_price == expected
            fresh = await cold_runtime(runtime)
            before, version = fresh.owner.state, fresh.owner.version
            assert await fresh.observe_market(event) is None
            assert fresh.owner.state == before and fresh.owner.version == version
            assert fresh.engine.portfolio.positions['005930'].current_price == expected
        finally:
            with suppress(ApplicationBlocked): await runtime.shutdown()
            await store.close()
            if fresh is not None:
                await fresh.shutdown()
                await fresh.owner.store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('bound', [False, True])
@pytest.mark.parametrize('after_boundary', [False, True])
def test_cold_price_view_respects_day_valuation_admission_order(tmp_path, monkeypatch, bound, after_boundary):
    async def scenario():
        from datetime import timedelta
        from src.execution.safety.day_recovery import ValuationPrice
        from test_execution_day_recovery import day_setup, prepare, valued
        engine, exits, store, runtime, times = await day_setup(tmp_path)
        fresh = None
        async def quote():
            if bound:
                event = await market_event(monkeypatch, runtime._now(), price='10400')
                await runtime.observe_market(event)
            else:
                await runtime.quote('005930', D('10400'))
        try:
            buy = await opened(runtime, 'B1')
            await queued(engine, await observed(runtime, buy, 100, '1000000'))
            if not after_boundary: await quote()
            fence = await prepare(runtime, times)
            eid = await valued(runtime, fence, times, [ValuationPrice('005930', D('10500'),
                                 times[0], 'synthetic', 'boundary')])
            assert (await runtime.rollover_day('roll', expected_version=runtime.owner.version,
                fence_id=fence.fence_id, valuation_evidence_id=eid)).status == 'APPLIED'
            assert (await runtime.resume_after_rollover('resume', expected_version=runtime.owner.version,
                fence_id=fence.fence_id)).status == 'APPLIED'
            if after_boundary:
                times[0] += timedelta(minutes=1)
                await quote()
            expected = D('10400' if after_boundary else '10500')
            assert engine.portfolio.positions['005930'].current_price == expected
            fresh = await cold_runtime(runtime)
            assert fresh.engine.portfolio.positions['005930'].current_price == expected
            assert not fresh.trading_ready
        finally:
            with suppress(ApplicationBlocked): await runtime.shutdown()
            await store.close()
            if fresh is not None:
                await fresh.shutdown()
                await fresh.owner.store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('corrupt', [False, True])
def test_newer_completed_price_cannot_reactivate_old_source(tmp_path, monkeypatch, corrupt):
    async def scenario():
        from src.execution.safety.market_source import source_is_current
        _, _, store, runtime = await setup(tmp_path, account_scope='scope')
        try:
            event = await market_event(monkeypatch, runtime._now())
            await runtime.observe_market(event)
            await runtime.quote(event.symbol, D('10100'))
            state = runtime.owner.state
            if corrupt:
                state['market_sources'][event.symbol]['invalidated_at_version'] = 0
            assert not source_is_current(state, event.symbol)
            if corrupt:
                await store.commit(runtime.owner.version, state, 'lost-invalidation')
                with pytest.raises(ApplicationBlocked): await runtime.restore()
                assert not runtime.owner.healthy
            else:
                await runtime.restore()
                assert runtime.owner.healthy
        finally:
            with suppress(ApplicationBlocked): await runtime.shutdown()
            await store.close()
    asyncio.run(scenario())


def test_newer_pending_price_restores_without_authorizing_old_source(tmp_path, monkeypatch):
    async def scenario():
        from src.execution.safety.market_source import source_is_current
        _, _, store, runtime = await setup(tmp_path, account_scope='scope')
        fresh = None
        try:
            event = await market_event(monkeypatch, runtime._now())
            await runtime.observe_market(event)
            commit = store.commit
            async def fail_apply(version, state, command_id):
                if command_id.startswith('command:quote:'):
                    raise OSError('pending bare quote')
                return await commit(version, state, command_id)
            monkeypatch.setattr(store, 'commit', fail_apply)
            with pytest.raises(OSError): await runtime.quote(event.symbol, D('10100'))
            fresh = await cold_runtime(runtime)
            state = fresh.owner.state
            assert fresh.owner.healthy and fresh.market_source_pending(state)
            assert state['market_sources'][event.symbol]['invalidated_at_version'] == 0
            assert not source_is_current(state, event.symbol)
        finally:
            with suppress(ApplicationBlocked): await runtime.shutdown()
            await store.close()
            if fresh is not None:
                await fresh.shutdown()
                await fresh.owner.store.close()
    asyncio.run(scenario())
