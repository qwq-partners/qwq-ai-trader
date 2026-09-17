"""실제 core 큐→SQLite→Portfolio/ExitManager 완료 경계 (외부 I/O 없음)."""
import asyncio
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from src.core.engine import UnifiedEngine
from src.core.event import FillEvent, HeartbeatEvent
from src.core.types import Fill, OrderSide, TradingConfig, RiskConfig
from src.execution.safety.application import ApplicationBlocked, FillObservation
from src.execution.safety.economics import encode_portfolio, new_risk_state
from src.execution.safety.lifecycle import (
    CommandResult, CommandStatus, OrderEvidence, OrderRef, OrderState,
)
from src.execution.safety.protection import encode_protection
from src.execution.safety.runtime import KRExecutionRuntime
from src.execution.safety.store import ExecutionStateStore
from src.strategies.exit_manager import ExitManager, ExitStage


NOW = datetime(2026, 9, 18, 10, tzinfo=ZoneInfo("Asia/Seoul"))


async def setup(tmp_path, *, store_type=ExecutionStateStore, risk_manager=None, account_scope=None, clock=None):
    clock = clock or (lambda: NOW)
    engine = UnifiedEngine(TradingConfig(initial_capital=Decimal("2000000")))
    exits = ExitManager(persist=False, clock=clock)
    store = store_type(tmp_path / "execution" / "state.sqlite3")
    # 합성 기준선일 뿐: 실계좌 인계/시작 대사 증거를 생성하는 API는 아니다.
    state = {
        "portfolio": encode_portfolio(engine.portfolio),
        "protection": encode_protection(exits),
        "risk": new_risk_state(NOW.date().isoformat()),
        "lots": {}, "outbox": {}, "intents": {}, "attempts": {},
        "startup_reconciliation": {"status": "blocked", "reason": "synthetic_baseline"},
    }
    await store.commit(0, state, "synthetic-baseline")
    runtime = KRExecutionRuntime(store, engine, exits, clock=clock, risk_manager=risk_manager, account_scope=account_scope)
    await runtime.restore()
    runtime.attach()
    return engine, exits, store, runtime


async def opened(runtime, order_id, side="buy", quantity=100, intent=None):
    ref = OrderRef("scope", "KR", "2026-09-18", "KRX", order_id)
    await runtime.lifecycle.prepare(intent or order_id, order_id, quantity, "005930", side,
                                    strategy="sepa_trend", reserved_cash="1000200" if side == "buy" else "0")
    await runtime.lifecycle.claim(order_id, "sender")
    await runtime.lifecycle.record_result(order_id, "sender", CommandResult(
        CommandStatus.ACKNOWLEDGED, order_id, ref))
    return ref


async def observed(runtime, ref, qty, amount, *, side="buy", total=100, metadata=None):
    evidence = OrderEvidence(
        ref, "005930", side, total, qty, Decimal(amount), total - qty, 0,
        OrderState.PARTIAL if qty < total else OrderState.FINAL_FILLED,
        complete=True, supported_finality=qty == total, source_contract="synthetic-test",
        observed_at=NOW, request_started_at=NOW - timedelta(seconds=1),
        query_scope={"account_scope": "scope", "market": "KR", "exchange": "KRX",
                     "start_date": "2026-09-18", "end_date": "2026-09-18",
                     "tr_id": "TTTC0081R", "query_kind": "all", "session": "regular"},
    )
    assert await runtime.lifecycle.reconcile(ref.order_no, evidence)
    return FillObservation("scope", "KR", "2026-09-18", "KRX", ref.order_no,
                           "005930", side.upper(), qty, Decimal(amount), metadata=metadata or {})


async def wait_queued(engine, caller):
    async def condition():
        while not engine._event_queue:
            if caller.done():
                return await caller
            await asyncio.sleep(0)
    await asyncio.wait_for(condition(), 2)


async def queued(engine, observation):
    pending = asyncio.create_task(engine.apply_execution_observation(observation))
    # 실제 우선순위 힙 적재/핸들러를 사용하고 emit을 mock하지 않는다.
    await wait_queued(engine, pending)
    assert not pending.done()
    event = await engine._get_next_event()
    assert event is not None
    await engine._process_event(event)
    return await pending


def test_real_queue_buy40_60_then_sell40_60_applies_cash_and_protection_once(tmp_path):
    async def scenario():
        engine, exits, store, runtime = await setup(tmp_path)
        try:
            ref = await opened(runtime, "B1")
            first = await observed(runtime, ref, 40, "400000")
            receipt = await queued(engine, first)
            assert (receipt.status, receipt.protection_status) == ("APPLIED", "ready")
            assert receipt.journal_pending
            assert engine.portfolio.cash == Decimal("1599944")  # fee(400000)=56
            assert exits.get_state("005930").remaining_quantity == 40
            second = await observed(runtime, ref, 100, "1000000")
            await queued(engine, second)
            assert engine.portfolio.positions["005930"].quantity == 100
            assert exits.get_state("005930").remaining_quantity == 100
            assert engine.portfolio.cash == Decimal("999859")  # cumulative fee=141
            assert engine.portfolio.daily_trades == 1
            assert (await queued(engine, first)).status == "ALREADY_APPLIED"
            sell = await opened(runtime, "S1", "sell")
            sale40 = await observed(runtime, sell, 40, "440000", side="sell")
            await queued(engine, sale40)
            assert engine.portfolio.positions["005930"].quantity == 60
            assert exits.get_state("005930").remaining_quantity == 60
            assert engine.portfolio.cash == Decimal("1438922")  # sell fee=937
            sale100 = await observed(runtime, sell, 100, "1100000", side="sell")
            await queued(engine, sale100)
            assert "005930" not in engine.portfolio.positions
            assert exits.get_state("005930") is None
            assert engine.portfolio.cash == Decimal("2097515")  # 2M+100000-141-2344
            assert engine.portfolio.daily_pnl == Decimal("97515")
            assert engine.portfolio.daily_trades == 1
            assert runtime.owner.state["attempts"]["S1"]["applied_quantity"] == 100
            assert len(runtime.owner.state["outbox"]) == 4
            # baseline이 blocked인 동안 체결 사실은 반영해도 거래 허가는 없다.
            assert not runtime.trading_ready
        finally:
            await store.close()
    asyncio.run(scenario())


def test_receipt_waiter_cancellation_does_not_cancel_queued_fill(tmp_path):
    async def scenario():
        engine, exits, store, runtime = await setup(tmp_path)
        try:
            ref = await opened(runtime, "B1")
            obs = await observed(runtime, ref, 100, "1000000")
            caller = asyncio.create_task(engine.apply_execution_observation(obs))
            await wait_queued(engine, caller)
            caller.cancel()
            with pytest.raises(asyncio.CancelledError):
                await caller
            await engine._process_event(await engine._get_next_event())
            assert engine.portfolio.positions["005930"].quantity == 100
            assert (await queued(engine, obs)).status == "ALREADY_APPLIED"
        finally:
            await store.close()
    asyncio.run(scenario())


def test_queue_saturation_keeps_execution_fill_and_shutdown_fails_waiter(tmp_path):
    async def scenario():
        engine, _, store, runtime = await setup(tmp_path)
        try:
            ref = await opened(runtime, "B1")
            obs = await observed(runtime, ref, 100, "1000000")
            engine._MAX_QUEUE_SIZE = 1
            caller = asyncio.create_task(engine.apply_execution_observation(obs))
            await wait_queued(engine, caller)
            await engine.emit_many([HeartbeatEvent(), HeartbeatEvent()])
            assert not caller.done()
            await engine._shutdown()
            with pytest.raises(ApplicationBlocked):
                await caller
            with pytest.raises(ApplicationBlocked):
                await asyncio.wait_for(engine.apply_execution_observation(obs), 0.1)
            assert engine.portfolio.positions == {}
            assert runtime.owner.state["attempts"]["B1"]["applied_quantity"] == 0
        finally:
            await store.close()
    asyncio.run(scenario())


def test_binding_rejects_legacy_fill_and_direct_economic_writer(tmp_path):
    async def scenario():
        engine, _, store, runtime = await setup(tmp_path)
        try:
            old_fill = Fill("old", "005930", OrderSide.BUY, 100, Decimal("10000"))
            with pytest.raises(ApplicationBlocked):
                engine.update_position(old_fill)
            called = []
            async def old_handler(event):
                called.append(event)
            engine.register_handler(FillEvent().type, old_handler)
            await engine._process_event(FillEvent(symbol="005930", quantity=100))
            assert not called
            assert not engine.portfolio.positions
            assert engine.stats.errors_count == 1
        finally:
            await store.close()
    asyncio.run(scenario())


def test_unknown_order_fails_with_durable_inbox_not_cash_application(tmp_path):
    async def scenario():
        engine, _, store, runtime = await setup(tmp_path)
        try:
            obs = FillObservation("scope", "KR", "2026-09-18", "KRX", "unidentified",
                                  "005930", "BUY", 100, Decimal("1000000"))
            receipt = await queued(engine, obs)
            assert receipt.status == "FAILED"
            assert engine.portfolio.cash == Decimal("2000000")
            assert not runtime.owner.state.get("cursors")
            assert runtime.owner.state["inbox"][obs.observation_id]["status"] == "RECEIVED"
        finally:
            await store.close()
    asyncio.run(scenario())


def test_empty_database_does_not_replace_known_live_portfolio(tmp_path):
    async def scenario():
        engine = UnifiedEngine(TradingConfig(initial_capital=Decimal("1000")))
        exits = ExitManager(persist=False)
        store = ExecutionStateStore(tmp_path / "empty" / "state.sqlite3")
        try:
            runtime = KRExecutionRuntime(store, engine, exits, clock=lambda: NOW)
            with pytest.raises(ApplicationBlocked):
                await runtime.restore()
            assert engine.portfolio.cash == Decimal("1000")
            assert not runtime.owner.healthy
            assert not runtime.trading_ready
        finally:
            await store.close()
    asyncio.run(scenario())


def test_protection_failure_commits_economics_once_and_stays_degraded(tmp_path, monkeypatch):
    async def scenario():
        engine, exits, store, runtime = await setup(tmp_path)
        try:
            ref = await opened(runtime, "B1")
            obs = await observed(runtime, ref, 40, "400000")
            def failing_registration(*args, **kwargs):
                raise ValueError("synthetic protection failure")
            monkeypatch.setattr(ExitManager, "register_position", failing_registration)
            receipt = await queued(engine, obs)
            assert (receipt.status, receipt.protection_status) == ("APPLIED", "degraded")
            assert engine.portfolio.positions["005930"].quantity == 40
            assert engine.portfolio.cash == Decimal("1599944")
            assert (await queued(engine, obs)).status == "ALREADY_APPLIED"
            obs100 = await observed(runtime, ref, 100, "1000000")
            assert (await queued(engine, obs100)).protection_status == "degraded"
            assert engine.portfolio.positions["005930"].quantity == 100
            assert runtime.owner.state["protection"]["degraded"]["005930"]["quantity"] == 100
            assert runtime.health()["protection_degraded"] == 1
        finally:
            await store.close()
    asyncio.run(scenario())


def test_committed_fill_with_failed_publication_recovers_without_double_cash(tmp_path):
    async def scenario():
        engine, _, store, runtime = await setup(tmp_path)
        try:
            ref = await opened(runtime, "B1")
            obs = await observed(runtime, ref, 100, "1000000")
            publish = runtime.owner.publisher
            failed = False
            def fail_once(state, version):
                nonlocal failed
                if state.get("cursors") and not failed:
                    failed = True
                    raise ValueError("synthetic publication failure")
                publish(state, version)
            runtime.owner.publisher = fail_once
            with pytest.raises(ApplicationBlocked):
                await queued(engine, obs)
            assert not runtime.owner.healthy
            assert runtime.owner.publication_recovery_required
            assert (await store.load())[1]["cursors"][obs.order_key]["quantity"] == 100
            with pytest.raises(ApplicationBlocked):
                await queued(engine, obs)
            await runtime.restore()
            assert runtime.owner.healthy
            assert engine.portfolio.positions["005930"].quantity == 100
            assert engine.portfolio.cash == Decimal("999859")
            assert (await queued(engine, obs)).status == "ALREADY_APPLIED"
            assert engine.portfolio.cash == Decimal("999859")
        finally:
            await store.close()
    asyncio.run(scenario())


class GatedStore(ExecutionStateStore):
    def __init__(self, path):
        super().__init__(path)
        self.armed = False
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def commit(self, expected_version, state, commit_id):
        if self.armed and commit_id.startswith("fill:"):
            self.armed = False
            self.entered.set()
            await self.release.wait()
        return await super().commit(expected_version, state, commit_id)


def test_quote_during_fill_commit_preserves_current_view_and_serializes_high_be(tmp_path):
    async def scenario():
        engine, exits, store, runtime = await setup(tmp_path, store_type=GatedStore)
        try:
            ref = await opened(runtime, "B1")
            await queued(engine, await observed(runtime, ref, 40, "400000"))
            def stage(state):
                # 합성 이전 익절을 명시 snapshot으로 주입한다. 등록 mock은 아니다.
                state["protection"]["states"]["005930"]["current_stage"] = "second"
                return state
            await runtime.owner.mutate("synthetic-stage", stage)
            obs = await observed(runtime, ref, 100, "1000000")
            store.armed = True
            caller = asyncio.create_task(engine.apply_execution_observation(obs))
            await wait_queued(engine, caller)
            process = asyncio.create_task(engine._process_event(await engine._get_next_event()))
            await store.entered.wait()
            price = asyncio.create_task(runtime.quote("005930", Decimal("11000")))
            await asyncio.sleep(0)
            # durable quote admission 전에는 이전 view를 유지한다.
            assert engine.portfolio.positions["005930"].current_price == Decimal("10000")
            assert not price.done() and not caller.done()
            assert not runtime.owner.healthy
            store.release.set()
            await process
            assert (await caller).status == "APPLIED"
            assert await price is None
            assert engine.portfolio.positions["005930"].current_price == Decimal("11000")
            guard = exits.get_state("005930")
            assert guard.current_stage is ExitStage.SECOND
            assert guard.breakeven_activated
            assert guard.highest_price == Decimal("11000")
            assert guard.remaining_quantity == 100
            await runtime.restore()
            assert exits.get_state("005930").breakeven_activated
            assert exits.get_state("005930").highest_price == Decimal("11000")
        finally:
            store.release.set()
            await store.close()
    asyncio.run(scenario())


def test_full_engine_loop_delivers_receipt_and_reopen_replays_without_reapplying(tmp_path):
    async def scenario():
        engine, _, store, runtime = await setup(tmp_path)
        ref = await opened(runtime, "B1")
        obs = await observed(runtime, ref, 100, "1000000")
        runner = asyncio.create_task(engine.run())
        try:
            receipt = await asyncio.wait_for(engine.apply_execution_observation(obs), 2)
            assert receipt.status == "APPLIED"
        finally:
            runner.cancel()
            with pytest.raises(asyncio.CancelledError):
                await runner
            await store.close()
        reopened = ExecutionStateStore(tmp_path / "execution" / "state.sqlite3")
        new_engine = UnifiedEngine(TradingConfig(initial_capital=Decimal("0")))
        new_exits = ExitManager(persist=False, clock=lambda: NOW)
        new_runtime = KRExecutionRuntime(reopened, new_engine, new_exits, clock=lambda: NOW)
        try:
            await new_runtime.restore()
            new_runtime.attach()
            assert (await queued(new_engine, obs)).status == "ALREADY_APPLIED"
            assert new_engine.portfolio.cash == Decimal("999859")
            assert new_engine.portfolio.daily_trades == 1
            assert new_exits.get_state("005930").remaining_quantity == 100
        finally:
            await reopened.close()
    asyncio.run(scenario())


def test_real_risk_manager_receives_persisted_loss_intent_and_one_use_state(tmp_path, monkeypatch):
    import src.risk.manager as policy

    class TestPath(type(Path())):
        @classmethod
        def home(cls):
            return tmp_path / "risk-home"

    # 실제 정책 객체. 파일 위치만 명시 임시경로로 대체하며 정책 메서드는 mock하지 않는다.
    monkeypatch.setattr(policy, "Path", TestPath)
    risk = policy.RiskManager(RiskConfig(), Decimal("2000000"), "KR")

    async def scenario():
        engine, _, store, runtime = await setup(tmp_path, risk_manager=risk)
        try:
            buy = await opened(runtime, "B1")
            await queued(engine, await observed(runtime, buy, 100, "1000000"))
            sell = await opened(runtime, "S1", "sell")
            metadata = {"exit_type": "stop_loss"}
            first = await observed(runtime, sell, 40, "360000", side="sell", metadata=metadata)
            await queued(engine, first)
            await queued(engine, first)
            assert risk._daily_exit_count == 1
            assert risk._stop_loss_today == {"005930"}
            assert risk._exited_today == {}
            await queued(engine, await observed(runtime, sell, 100, "900000", side="sell", metadata=metadata))
            assert risk._daily_exit_count == 1
            assert risk._exited_today["005930"]["price"] == 9000.0
            next_buy = await opened(runtime, "B2")
            rebuy = await observed(runtime, next_buy, 40, "400000")
            await queued(engine, rebuy)
            assert risk._stop_loss_rebound_used == {"005930"}
            assert risk.daily_stats.trades == engine.portfolio.daily_trades == 2
            await runtime.restore()
            await queued(engine, rebuy)
            assert risk._daily_exit_count == 1
            assert risk._stop_loss_rebound_used == {"005930"}
            assert risk.daily_stats.trades == 2
        finally:
            await store.close()
    asyncio.run(scenario())


def test_engine_cancelled_during_startup_closes_queued_receipt(tmp_path):
    async def scenario():
        engine, _, store, runtime = await setup(tmp_path)
        ref = await opened(runtime, "B1")
        obs = await observed(runtime, ref, 100, "1000000")
        caller = asyncio.create_task(engine.apply_execution_observation(obs))
        await wait_queued(engine, caller)
        await engine._queue_lock.acquire()
        runner = asyncio.create_task(engine.run())
        try:
            await asyncio.sleep(0)  # startup SessionEvent 적재가 실제 queue lock에서 대기
            runner.cancel()
            engine._queue_lock.release()
            with pytest.raises(asyncio.CancelledError):
                await runner
            assert not engine.running
            assert not engine._execution_accepting
            with pytest.raises(ApplicationBlocked):
                await caller
        finally:
            if engine._queue_lock.locked():
                engine._queue_lock.release()
            await engine._shutdown()
            await asyncio.gather(caller, return_exceptions=True)
            await store.close()
    asyncio.run(scenario())


def test_paused_engine_applies_confirmed_fill_without_processing_other_events(tmp_path):
    async def scenario():
        engine, exits, store, runtime = await setup(tmp_path)
        ref = await opened(runtime, "B1")
        obs = await observed(runtime, ref, 100, "1000000")
        engine.paused = True
        called = []
        async def heartbeat(event):
            called.append(event)
        engine.register_handler(HeartbeatEvent().type, heartbeat)
        await engine.emit(HeartbeatEvent(priority=0))
        runner = asyncio.create_task(engine.run())
        try:
            receipt = await asyncio.wait_for(engine.apply_execution_observation(obs), 0.5)
            assert receipt.status == "APPLIED"
            assert exits.get_state("005930").remaining_quantity == 100
            assert not called  # 거래 중지 의미는 유지, 이미 체결된 사실만 적용
        finally:
            runner.cancel()
            with pytest.raises(asyncio.CancelledError):
                await runner
            await store.close()
    asyncio.run(scenario())


def test_cancelled_quote_waiter_does_not_drop_intermediate_high_or_be(tmp_path):
    async def scenario():
        engine, exits, store, runtime = await setup(tmp_path)
        try:
            ref = await opened(runtime, "B1")
            await queued(engine, await observed(runtime, ref, 100, "1000000"))
            def stage(state):
                state["protection"]["states"]["005930"]["current_stage"] = "second"
                return state
            await runtime.owner.mutate("synthetic-stage", stage)
            await runtime.owner._lock.acquire()
            caller = asyncio.create_task(runtime.quote("005930", Decimal("11000")))
            await asyncio.sleep(0)
            caller.cancel()
            with pytest.raises(asyncio.CancelledError):
                await caller
            runtime.owner._lock.release()
            await runtime.quote("005930", Decimal("10900"))
            assert engine.portfolio.positions["005930"].current_price == Decimal("10900")
            assert exits.get_state("005930").highest_price == Decimal("11000")
            assert exits.get_state("005930").breakeven_activated
        finally:
            if runtime.owner._lock.locked():
                runtime.owner._lock.release()
            await engine._shutdown()
            await store.close()
    asyncio.run(scenario())


def test_shutdown_drains_accepted_quote_after_waiter_cancel_and_refuses_new_quotes(tmp_path):
    async def scenario():
        engine, exits, store, runtime = await setup(tmp_path)
        try:
            ref = await opened(runtime, "B1")
            await queued(engine, await observed(runtime, ref, 100, "1000000"))
            await runtime.owner._lock.acquire()
            caller = asyncio.create_task(runtime.quote("005930", Decimal("10100")))
            await asyncio.sleep(0)
            caller.cancel()
            with pytest.raises(asyncio.CancelledError):
                await caller
            assert runtime.health()["protection_updates_pending"] == 1
            ending = asyncio.create_task(engine._shutdown())
            await asyncio.sleep(0)
            assert not ending.done()
            runtime.owner._lock.release()
            await ending
            assert exits.get_state("005930").highest_price == Decimal("10100")
            assert runtime.health()["protection_updates_pending"] == 0
            with pytest.raises(ApplicationBlocked):
                await runtime.quote("005930", Decimal("10200"))
        finally:
            if runtime.owner._lock.locked():
                runtime.owner._lock.release()
            await runtime.shutdown()
            await store.close()
    asyncio.run(scenario())


def test_failed_accepted_protection_command_is_visible_in_health(tmp_path):
    async def scenario():
        engine, _, store, runtime = await setup(tmp_path)
        try:
            ref = await opened(runtime, "B1")
            await queued(engine, await observed(runtime, ref, 100, "1000000"))
            # 익절 접촉은 owner 없는 pending을 새로 만들 수 없어 명시 실패한다.
            with pytest.raises(ValueError):
                await runtime.quote("005930", Decimal("12000"))
            assert runtime.health()["protection_updates_failed"]
            assert runtime.health()["protection_updates_pending"] == 0
            assert not runtime.trading_ready
        finally:
            await runtime.shutdown()
            await store.close()
    asyncio.run(scenario())
