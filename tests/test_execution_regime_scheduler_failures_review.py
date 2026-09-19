"""Independent C2 Opus disposition: installed scheduler and seal-drain boundaries."""
import asyncio
from types import SimpleNamespace

import pytest

from src.core.engine import UnifiedEngine
from src.core.types import TradingConfig
from src.execution.safety.application import ApplicationBlocked
from src.execution.safety.risk_sources import RiskSourceCoordinator
from src.execution.safety.runtime import KRExecutionRuntime
from src.execution.safety.store import ExecutionStateStore
from src.strategies.exit_manager import ExitManager
from test_execution_runtime import NOW, setup


class _Heartbeat:
    def __init__(self):
        self.attempts, self.successes, self.failures = [], [], []

    def record_attempt(self, name): self.attempts.append(name)
    def record_success(self, name): self.successes.append(name)
    def record_failure(self, name, reason): self.failures.append((name, reason))
    def record_idle(self, *_args): raise AssertionError('regular session must not be idle')


class _LegacyRiskManager:
    def __init__(self): self.legacy_updates = []
    def update_market_trend(self, *args): self.legacy_updates.append(args)


class _KIS:
    def __init__(self): self.calls = []
    async def fetch_index_price(self, code):
        self.calls.append(code)
        raise AssertionError('unbound/misbound installed loop must not fetch legacy index data')


async def _run_once(scheduler, bot, monkeypatch, heartbeat):
    import src.schedulers.kr_scheduler as scheduler_module
    real_sleep, sleeps = asyncio.sleep, []

    async def sleep(seconds):
        sleeps.append(seconds)
        if seconds == 120:
            bot.running = False
        await real_sleep(0)

    monkeypatch.setattr(scheduler_module, '_hb', heartbeat)
    monkeypatch.setattr(scheduler_module.asyncio, 'sleep', sleep)
    await scheduler.run_market_trend_monitor()
    return sleeps


@pytest.mark.parametrize('case', ('baseline_missing', 'binding_conflict'))
def test_actual_installed_loop_records_failure_without_legacy_fetch_or_setter(tmp_path, monkeypatch, case):
    """The installed-runtime branch must fail in-loop, rather than fall back to legacy setters."""
    async def scenario():
        from src.schedulers.kr_scheduler import KRScheduler, MarketSession
        legacy, kis, heartbeat = _LegacyRiskManager(), _KIS(), _Heartbeat()
        if case == 'baseline_missing':
            # The runtime keeps its real publisher-compatible risk manager.  The
            # legacy object is deliberately only the scheduler's fallback target.
            engine, _, store, runtime = await setup(tmp_path, account_scope='scope')
        else:
            import src.risk.manager as risk_module
            from test_execution_regime_owner import owned
            monkeypatch.setattr(risk_module.Path, 'home', lambda: tmp_path)
            engine, _, store, runtime, writer, _ = await owned(tmp_path)
            assert runtime._regime_writer is writer and writer.sidecar is not legacy
        bot = SimpleNamespace(running=True, engine=engine, risk_manager=legacy, kis_market_data=kis,
            expert_orchestrator=None, _get_current_session=lambda: MarketSession.REGULAR)
        scheduler = object.__new__(KRScheduler); scheduler.bot = bot
        try:
            assert await _run_once(scheduler, bot, monkeypatch, heartbeat) == [60, 120]
            assert heartbeat.attempts == ['kr_market_trend']
            assert len(heartbeat.failures) == 1 and heartbeat.successes == []
            assert kis.calls == [] and legacy.legacy_updates == []
            assert runtime.owner.healthy
        finally:
            await runtime.shutdown(); await store.close()
    asyncio.run(scenario())


async def _cold_source(store, runtime):
    path = store.path
    await store.close()
    engine = UnifiedEngine(TradingConfig(initial_capital=__import__('decimal').Decimal('2000000')))
    exits = ExitManager(persist=False, clock=lambda: NOW)
    reopened = ExecutionStateStore(path)
    restored = KRExecutionRuntime(reopened, engine, exits, clock=lambda: NOW, account_scope='scope')
    await restored.restore(); restored.attach()
    return reopened, restored


@pytest.mark.parametrize('phase', ('before_write', 'after_write'))
def test_cancelled_seal_drain_persists_only_real_sql_progress_and_cold_terminalizes(tmp_path, monkeypatch, phase):
    """Cancelling the waiter cannot cancel a submitted seal; no SQL error is expected here."""
    async def scenario():
        _, _, store, runtime = await setup(tmp_path, account_scope='scope')
        source = RiskSourceCoordinator(runtime)
        entered, release = asyncio.Event(), asyncio.Event()
        original, waiter, closing = store.commit, None, None
        try:
            ticket = await source.begin('seal-drain-' + phase, 'llm_regime', require_seal=True)

            async def gate(expected, state, command):
                if 'risk-seal:' not in command:
                    return await original(expected, state, command)
                if phase == 'before_write':
                    entered.set(); await release.wait()
                    return await original(expected, state, command)
                result = await original(expected, state, command)
                entered.set(); await release.wait()
                return result

            monkeypatch.setattr(store, 'commit', gate)
            waiter = asyncio.create_task(source.seal(ticket, inputs={'phase': phase}))
            await asyncio.wait_for(entered.wait(), 2)
            waiter.cancel()
            with pytest.raises(asyncio.CancelledError):
                await waiter
            closing = asyncio.create_task(runtime.shutdown())
            await asyncio.sleep(0)
            assert not closing.done()
            release.set(); await asyncio.wait_for(closing, 2)
            durable = (await store.load())[1]
            assert durable['risk_input_seals']['records'][ticket.operation_id]['original']['status'] == 'sealed'
            assert runtime.owner.healthy and not runtime._command_results_failed
            store, restored = await _cold_source(store, runtime)
            runtime = None
            receipt = await RiskSourceCoordinator(restored).complete(ticket, 'success', {})
            assert receipt.status == 'accepted' and restored.owner.healthy
            assert restored.owner.state['risk_sources']['records'][ticket.operation_id]['terminal']['receipt']['status'] == 'accepted'
        finally:
            release.set()
            await asyncio.gather(*(task for task in (waiter, closing) if task), return_exceptions=True)
            if runtime is not None and not runtime._closing:
                await runtime.shutdown()
            if 'restored' in locals() and not restored._closing:
                await restored.shutdown()
            await store.close()
    asyncio.run(scenario())


def test_genuine_seal_sql_error_keeps_failure_latch(tmp_path, monkeypatch):
    """A real storage exception is not normalized into the healthy cancellation control above."""
    async def scenario():
        _, _, store, runtime = await setup(tmp_path, account_scope='scope')
        source = RiskSourceCoordinator(runtime)
        original = store.commit
        try:
            ticket = await source.begin('seal-sql-error', 'llm_regime', require_seal=True)

            async def fail(expected, state, command):
                if 'risk-seal:' in command:
                    raise OSError('independent seal SQL fault')
                return await original(expected, state, command)

            monkeypatch.setattr(store, 'commit', fail)
            with pytest.raises(OSError, match='independent seal SQL fault'):
                await source.seal(ticket, inputs={})
            assert not runtime.owner.healthy and runtime._command_results_failed
            with pytest.raises(ApplicationBlocked, match='command_result_drain_failed'):
                await runtime.shutdown()
        finally:
            await store.close()
    asyncio.run(scenario())
