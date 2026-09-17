"""Installed actual BatchAnalyzer risk writer must use the durable source owner."""
import asyncio
from datetime import timedelta
from types import SimpleNamespace
import pytest

from src.core.batch_analyzer import BatchAnalyzer
from src.execution.safety.application import ApplicationBlocked
from test_execution_runtime import NOW, setup
from test_kis_index_observation import provider, raw


async def owned(tmp_path, *, clock=None):
    from src.execution.safety.intraday_owner import IntradayRiskOwner
    from src.execution.safety.risk_transition import IntradayPolicyState
    engine, exits, store, runtime = await setup(tmp_path, account_scope='scope', clock=clock)
    # Synthetic explicit handoff only. Production must not synthesize this baseline.
    def seed(state):
        baseline = IntradayPolicyState('normal', 0.0, None, None).to_dict()
        state['intraday_policy'] = {'schema': 1, 'baseline': baseline,
            'baseline_version': runtime.owner.version + 1,
            'current': baseline.copy(), 'transitions': {}}
        return state
    await runtime.owner.mutate('synthetic-risk-baseline', seed)
    batch = batch_for(engine, exits)
    writer = IntradayRiskOwner(runtime, batch)
    return engine, exits, store, runtime, batch, writer


async def stale_position(runtime):
    from decimal import Decimal
    from src.core.types import Position
    from src.execution.safety.economics import decode_portfolio, encode_portfolio
    from src.execution.safety.protection import decode_protection, encode_protection
    def seed(state):
        portfolio = decode_portfolio(state['portfolio'])
        position = Position(symbol='005930', quantity=10, avg_price=Decimal('10000'),
            current_price=Decimal('10000'), strategy='sepa_trend', entry_time=NOW - timedelta(days=10))
        portfolio.positions[position.symbol] = position
        exits = decode_protection(state['protection'], clock=lambda: NOW)
        exits.register_position(position)
        state['portfolio'], state['protection'] = encode_portfolio(portfolio), encode_protection(exits)
        return state
    await runtime.owner.mutate('synthetic-stale-position', seed)


def batch_for(engine, exits):
    analyzer = object.__new__(BatchAnalyzer)
    analyzer._engine = engine
    analyzer._exit_manager = exits
    analyzer._intraday_state = 'normal'
    analyzer._intraday_kospi_pct = 0.0
    analyzer._intraday_updated_at = None
    analyzer._intraday_recovery_until = None
    return analyzer


def test_installed_batch_cannot_mutate_owned_protection_without_source_ticket(tmp_path):
    async def scenario():
        engine, exits, store, runtime = await setup(tmp_path, account_scope='scope')
        batch = batch_for(engine, exits)
        try:
            version, state = runtime.owner.version, runtime.owner.state
            with pytest.raises(ApplicationBlocked, match='risk_source_ticket_required'):
                await batch.update_intraday_state(-3.0)
            assert runtime.owner.version == version and runtime.owner.state == state
            assert batch._intraday_state == exits._intraday_crash_level == 'normal'
        finally:
            await runtime.shutdown()
            await store.close()
    asyncio.run(scenario())


def test_actual_scheduler_and_kis_adapter_commit_risk_and_protection_together(tmp_path, monkeypatch):
    async def scenario():
        from src.schedulers.kr_scheduler import KRScheduler
        engine, exits, store, runtime, batch, writer = await owned(tmp_path)
        output = raw(); output['bstp_nmix_prdy_ctrt'] = '-3'
        md, calls = await provider(monkeypatch, output)
        scheduler = object.__new__(KRScheduler)
        scheduler.bot = SimpleNamespace(engine=engine, batch_analyzer=batch, kis_market_data=md)
        try:
            receipt = await scheduler._refresh_intraday_risk()
            assert receipt.status == 'accepted'
            state = runtime.owner.state
            assert state['intraday_policy']['current']['level'] == 'crash'
            assert batch._intraday_state == exits._intraday_crash_level == 'crash'
            assert batch._intraday_updated_at == NOW
            assert calls == {'get': 1, 'limiter': 1}
            assert (await store.load())[1] == state
            assert writer.snapshot().observation_status == 'missing'
            assert not runtime.trading_ready
        finally:
            await runtime.shutdown(); await store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('newer', [-3.0, None])
def test_actual_refresh_reverse_completion_cannot_publish_old_normal(tmp_path, monkeypatch, newer):
    async def scenario():
        engine, exits, store, runtime, batch, writer = await owned(tmp_path)
        started, release = asyncio.Event(), asyncio.Event()
        md, _ = await provider(monkeypatch, raw())
        first = await md.fetch_index_price('0001')
        first['change_pct'] = first['_observation']['fields']['change_pct']['value'] = 0.0
        async def old_fetch(code):
            started.set(); await release.wait(); return first
        task = asyncio.create_task(writer.refresh(SimpleNamespace(fetch_index_price=old_fetch)))
        await asyncio.wait_for(started.wait(), 2)
        async def fresh_fetch(code):
            if newer is None: return None
            from copy import deepcopy
            value = deepcopy(first)
            from uuid import uuid4
            value['_observation']['observation_id'] = str(uuid4())
            value['change_pct'] = value['_observation']['fields']['change_pct']['value'] = newer
            return value
        try:
            new_receipt = await writer.refresh(SimpleNamespace(fetch_index_price=fresh_fetch))
            before = runtime.owner.state['intraday_policy']
            release.set()
            assert (await task).status == 'stale'
            assert runtime.owner.state['intraday_policy'] == before
            expected = 'crash' if newer is not None else 'normal'
            assert batch._intraday_state == exits._intraday_crash_level == expected
            assert new_receipt.status == ('accepted' if newer is not None else 'missing')
        finally:
            release.set(); await asyncio.gather(task, return_exceptions=True)
            await runtime.shutdown(); await store.close()
    asyncio.run(scenario())


def test_missing_original_percent_never_recovers_crash_as_outer_zero(tmp_path, monkeypatch):
    async def scenario():
        _, exits, store, runtime, batch, writer = await owned(tmp_path)
        output = raw(); output['bstp_nmix_prdy_ctrt'] = '-3'
        md, _ = await provider(monkeypatch, output)
        try:
            await writer.refresh(md)
            del output['bstp_nmix_prdy_ctrt']
            md, _ = await provider(monkeypatch, output)
            assert (await writer.refresh(md)).status == 'missing'
            assert batch._intraday_state == exits._intraday_crash_level == 'crash'
            assert batch._intraday_recovery_until is None
        finally:
            await runtime.shutdown(); await store.close()
    asyncio.run(scenario())


def test_owned_risk_requires_explicit_baseline_not_live_normal_defaults(tmp_path):
    async def scenario():
        from src.execution.safety.intraday_owner import IntradayRiskOwner
        engine, exits, store, runtime = await setup(tmp_path, account_scope='scope')
        try:
            with pytest.raises(ApplicationBlocked, match='baseline'):
                IntradayRiskOwner(runtime, batch_for(engine, exits))
            assert getattr(runtime, '_intraday_writer', None) is None
        finally:
            await runtime.shutdown(); await store.close()
    asyncio.run(scenario())


def test_crash_effect_and_policy_survive_recovery_and_cache_duplicate_without_emit(tmp_path, monkeypatch):
    async def scenario():
        clock = [NOW]
        engine, exits, store, runtime, batch, writer = await owned(tmp_path, clock=lambda: clock[0])
        await stale_position(runtime)
        output = raw(); output['bstp_nmix_prdy_ctrt'] = '-3'
        md, calls = await provider(monkeypatch, output)
        try:
            receipt = await writer.refresh(md)
            effects = {k: v for k, v in runtime.owner.state['outbox'].items()
                       if v.get('effect_source') == 'intraday_preemptive'}
            assert len(effects) == 1
            row = next(iter(effects.values()))
            assert row['source_version'] == receipt.committed_version
            assert row['candidate']['quantity'] == 10 and row['candidate']['price'] == '10000'
            assert engine._event_queue == []  # effect is durable, not yet a broker/Signal permission
            clock[0] += timedelta(seconds=1)
            await writer.refresh(md)
            assert calls == {'get': 1, 'limiter': 1}
            assert batch._intraday_updated_at == NOW  # cache read is not a fresh observation
            assert len(runtime.owner.state['outbox']) == 1
            output['bstp_nmix_prdy_ctrt'] = '0'
            md, _ = await provider(monkeypatch, output)
            monkeypatch.setattr(md, '_index_clock', lambda: clock[0])
            await writer.refresh(md)
            assert batch._intraday_state == exits._intraday_crash_level == 'normal'
            assert batch._intraday_recovery_until == clock[0] + timedelta(minutes=5)
            assert runtime.owner.state['outbox'] == effects
            await runtime.restore()
            assert batch._intraday_recovery_until == clock[0] + timedelta(minutes=5)
        finally:
            await runtime.shutdown(); await store.close()
    asyncio.run(scenario())


def test_current_owned_prices_win_over_old_portfolio_dto_when_creating_stale_effect(tmp_path, monkeypatch):
    async def scenario():
        from decimal import Decimal
        _, exits, store, runtime, _, writer = await owned(tmp_path)
        await stale_position(runtime)
        # Same accepted owned quote view used by live publication and candidate calculation.
        await runtime.quote('005930', Decimal('10200'))
        output = raw(); output['bstp_nmix_prdy_ctrt'] = '-3'
        md, _ = await provider(monkeypatch, output)
        try:
            await writer.refresh(md)
            assert not any(v.get('effect_source') == 'intraday_preemptive'
                           for v in runtime.owner.state['outbox'].values())
            assert exits._intraday_crash_level == 'crash'
        finally:
            await runtime.shutdown(); await store.close()
    asyncio.run(scenario())


def test_fetch_cancellation_terminal_and_shutdown_wait_for_started_refresh(tmp_path, monkeypatch):
    async def scenario():
        _, _, store, runtime, _, writer = await owned(tmp_path)
        reached, release = asyncio.Event(), asyncio.Event()
        async def fetch(code):
            reached.set(); await release.wait(); return None
        task = asyncio.create_task(writer.refresh(SimpleNamespace(fetch_index_price=fetch)))
        await asyncio.wait_for(reached.wait(), 2)
        closing = asyncio.create_task(runtime.shutdown())
        await asyncio.sleep(0)
        assert not closing.done()
        task.cancel()
        try:
            with pytest.raises(asyncio.CancelledError): await task
            await asyncio.wait_for(closing, 2)
            row = next(iter(runtime.owner.state['risk_sources']['records'].values()))
            assert row['terminal']['receipt']['status'] == 'cancelled'
            assert runtime._command_results_failed is False
        finally:
            release.set(); await asyncio.gather(task, closing, return_exceptions=True)
            await store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('corruption', ['current', 'version', 'effect', 'source'])
def test_actual_restore_rejects_policy_source_effect_corruption_before_live_publication(tmp_path, monkeypatch, corruption):
    async def scenario():
        engine, exits, store, runtime, batch, writer = await owned(tmp_path)
        await stale_position(runtime)
        output = raw(); output['bstp_nmix_prdy_ctrt'] = '-3'
        md, _ = await provider(monkeypatch, output)
        await writer.refresh(md)
        state = runtime.owner.state
        prior_cash = engine.portfolio.cash
        state['portfolio']['cash'] = '1'
        if corruption == 'current': state['intraday_policy']['current']['level'] = 'normal'
        elif corruption == 'version':
            next(iter(state['intraday_policy']['transitions'].values()))['version'] = 1
        elif corruption == 'effect': state['outbox'].clear()
        else: state['risk_sources']['latest'].clear()
        try:
            await store.commit(runtime.owner.version, state, 'synthetic-corrupted-checkpoint')
            with pytest.raises(ApplicationBlocked): await runtime.restore()
            assert engine.portfolio.cash == prior_cash
            assert batch._intraday_state == exits._intraday_crash_level == 'crash'
            assert not runtime.owner.healthy
        finally:
            await store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('offset', [-1, 0, 1])
def test_actual_pending_signal_gate_uses_owned_aware_cooldown(tmp_path, monkeypatch, offset):
    class PassedCooldown(Exception): pass
    class StopAfterGate:
        def get(self, *args): raise PassedCooldown
    async def scenario():
        clock = [NOW]
        _, _, store, runtime, batch, writer = await owned(tmp_path, clock=lambda: clock[0])
        await stale_position(runtime)
        output = raw(); output['bstp_nmix_prdy_ctrt'] = '-3'
        md, _ = await provider(monkeypatch, output)
        await writer.refresh(md)
        output['bstp_nmix_prdy_ctrt'] = '0'
        md, _ = await provider(monkeypatch, output)
        await writer.refresh(md)
        async def symbols(): return []
        async def revalidate(signals, symbols): return signals
        batch._broker = SimpleNamespace(get_nxt_symbols=symbols)
        batch._load_json = lambda: [object()]
        batch._premarket_revalidate = revalidate
        batch._config = StopAfterGate()  # no strategy/quote/order code after tested gate
        clock[0] = batch._intraday_recovery_until + timedelta(seconds=offset)
        try:
            if offset < 0:
                await batch.execute_pending_signals()
            else:
                with pytest.raises(PassedCooldown): await batch.execute_pending_signals()
        finally:
            await runtime.shutdown(); await store.close()
    asyncio.run(scenario())


def test_actual_batch_scheduler_loop_reaches_owned_refresh_without_legacy_risk_writer(tmp_path, monkeypatch):
    async def scenario():
        import src.schedulers.kr_scheduler as module
        from pathlib import Path
        engine, exits, store, runtime, batch, writer = await owned(tmp_path)
        output = raw(); output['bstp_nmix_prdy_ctrt'] = '-3'
        md, calls = await provider(monkeypatch, output)
        bot = SimpleNamespace(engine=engine, batch_analyzer=batch, kis_market_data=md,
            config={'kr': {'llm_ops': {'regime_classifier_enabled': False},
                           'batch': {'lunchtime_scan_enabled': False}}}, running=True)
        scheduler = object.__new__(module.KRScheduler); scheduler.bot = bot
        class TestPath(type(Path())):
            @classmethod
            def home(cls): return cls(tmp_path / 'synthetic-home')
        monkeypatch.setattr(module, 'Path', TestPath)
        flag = TestPath.home() / '.cache' / 'ai_trader' / 'executed_2026-09-18.flag'
        flag.parent.mkdir(parents=True); flag.touch()
        async def no_regime_sync(): pass  # other writer, explicitly outside this bounded loop test
        async def monitor(): pass
        monkeypatch.setattr(scheduler, '_apply_regime_to_exit_manager', no_regime_sync)
        monkeypatch.setattr(batch, 'monitor_positions', monitor, raising=False)
        # Isolate only loop sleep; owner futures and real HTTP fake still execute normally.
        async def stop_after_iteration(seconds):
            assert seconds == 30
            bot.running = False
        monkeypatch.setattr(module, 'asyncio', SimpleNamespace(sleep=stop_after_iteration, CancelledError=asyncio.CancelledError))
        try:
            await scheduler.run_batch_scheduler()
            assert calls == {'get': 1, 'limiter': 1}
            assert batch._intraday_state == exits._intraday_crash_level == 'crash'
            assert len(runtime.owner.state['intraday_policy']['transitions']) == 1
            assert writer.snapshot().observation_status == 'missing'
        finally:
            await runtime.shutdown(); await store.close()
    asyncio.run(scenario())


def test_installed_raw_preemptive_writer_cannot_emit_outside_owned_effect(tmp_path):
    async def scenario():
        engine, _, store, runtime, batch, _ = await owned(tmp_path)
        await stale_position(runtime)
        try:
            with pytest.raises(ApplicationBlocked, match='risk_source_ticket_required'):
                await batch._preemptive_stale_exit_on_bear()
            assert engine._event_queue == []
        finally:
            await runtime.shutdown(); await store.close()
    asyncio.run(scenario())
