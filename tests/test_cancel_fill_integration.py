"""Canceled order increments must retain their own accounting identity."""
import asyncio
from datetime import date, datetime, timedelta
from decimal import Decimal as D
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.core.event import FillEvent, OrderEvent
from src.core.types import Fill, Order, OrderSide
from src.strategies.exit_manager import ExitStage
from test_fill_reconciliation import SYM, case, process
from test_sync_portfolio_characterization import _fill_check_once
from test_stale_sell_cancel_failure import SellBroker, _engine, _scheduler, _sweep, NOW
from test_kis_cancel_fill_recovery import broker, accepted, response, row
from test_order_post_unknown import ub, _RecSession, _FakeResp, OK_BODY


def confirmed_journal(bot):
    """취소 테스트에서도 명시 trade ID와 실제 저장 완료 계약을 충족한다."""
    from test_journal_commit_handoff import Journal
    bot.trade_journal = Journal()
    bot.trade_journal.status = "committed"
    pos = bot.engine.portfolio.positions[SYM]
    pos.trade_id = "synthetic-original-position"
    bot.trade_journal._trades[pos.trade_id] = SimpleNamespace(
        id=pos.trade_id, market_context={})


def pending(risk, side, oid="replacement", qty=5):
    risk._pending_order_ids = {SYM: oid}
    risk._pending_orders.add(SYM)
    risk._pending_quantities[SYM] = qty
    risk._pending_sides[SYM] = side
    risk._pending_timestamps[SYM] = datetime.now()
    risk._pending_signal_cache[SYM] = {"score": 99, "reason": "new signal"}
    risk._reserved_by_order[SYM] = D("50000")


def fill(side, oid="canceled", qty=3):
    return Fill(order_id=oid, symbol=SYM, side=side, quantity=qty,
                price=D("10000"), reason="old order")


@pytest.mark.parametrize("side", [OrderSide.BUY, OrderSide.SELL])
def test_old_fill_applies_cash_quantity_without_consuming_new_pending(monkeypatch, tmp_path, side):
    sched, bot = case(monkeypatch, tmp_path)
    risk = bot.engine.risk_manager
    pending(risk, side)
    item = FillEvent.from_fill(fill(side))
    asyncio.run(risk.on_fill(item))
    assert item.portfolio_applied is True
    assert risk._pending_quantities[SYM] == 5
    assert risk._reserved_by_order[SYM] == D("50000")
    assert risk._pending_signal_cache[SYM]["score"] == 99
    assert bot.engine.portfolio.positions[SYM].quantity == (13 if side == OrderSide.BUY else 7)
    assert getattr(bot.engine.portfolio.positions[SYM], "entry_signal_score", 0) != 99


def test_matching_partial_fill_consumes_only_owned_reservation(monkeypatch, tmp_path):
    _, bot = case(monkeypatch, tmp_path)
    risk = bot.engine.risk_manager
    pending(risk, OrderSide.BUY)
    asyncio.run(risk.on_fill(FillEvent.from_fill(fill(OrderSide.BUY, "replacement"))))
    assert risk._pending_quantities[SYM] == 2
    assert risk._reserved_by_order[SYM] == D("20000")


def test_late_fill_does_not_promote_new_exit_stage(monkeypatch, tmp_path):
    sched, bot = case(monkeypatch, tmp_path)
    pending(bot.engine.risk_manager, OrderSide.SELL)
    state = bot.exit_manager.get_state(SYM)
    state.pending_stage = ExitStage.FIRST
    state.pending_target_qty = 3
    state.pending_filled_qty = 0
    stage = state.current_stage
    bot._exit_pending_symbols.add(SYM)
    bot._exit_reasons[SYM] = "replacement reason"
    bot.broker.fills_seq = [[fill(OrderSide.SELL)]]
    _fill_check_once(monkeypatch, sched, [])
    asyncio.run(process(bot.engine))
    _fill_check_once(monkeypatch, sched, [])
    assert state.remaining_quantity == 7
    assert state.current_stage == stage
    assert state.pending_filled_qty == 0
    assert SYM in bot._exit_pending_symbols
    assert bot._exit_reasons[SYM] == "replacement reason"


def test_old_buy_does_not_capture_replacement_signal(monkeypatch, tmp_path):
    sched, bot = case(monkeypatch, tmp_path)
    pending(bot.engine.risk_manager, OrderSide.BUY)
    lot = sched._capture_entry_fill(fill(OrderSide.BUY))
    assert lot["signal"].get("score") != 99
    assert lot["signal"].get("reason") != "new signal"


def test_no_open_order_still_polls_cancel_and_acks_after_handoff(monkeypatch, tmp_path):
    sched, bot = case(monkeypatch, tmp_path)
    async def no_open(): return []
    bot.broker.get_open_orders = no_open
    bot.broker.fills_seq = [[fill(OrderSide.SELL)]]
    bot.broker.has_pending_fill_observations = lambda: True
    bot.broker.get_fill_observation_order_ids = lambda: {"canceled"}
    acks = []
    bot.broker.acknowledge_fill = lambda oid, qty: acks.append((oid, qty))
    _fill_check_once(monkeypatch, sched, [])
    assert len(bot.engine._event_queue) == 1
    assert acks == []
    asyncio.run(process(bot.engine))
    _fill_check_once(monkeypatch, sched, [])
    assert acks == [("canceled", 3)]
    assert bot.exit_manager.get_state(SYM).remaining_quantity == 7


@pytest.mark.parametrize("partial", [True, False])
def test_cancel_unknown_fill_defers_partial_replacement_but_not_full_exit(monkeypatch, partial):
    broker = SellBroker(cancelled=1, tracked=[])
    broker.has_unresolved_cancel = lambda symbol: True
    risk = _engine(monkeypatch, broker, partial=partial)
    asyncio.run(risk._fallback_stale_sell(SYM, NOW))
    assert len(broker.orders) == (0 if partial else 1)
    if partial:
        assert risk._pending_quantities[SYM] == 10
        assert SYM in risk._pending_cancel_keep


def test_stale_cleanup_preserves_stage_but_reopens_protective_exit(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    broker = SellBroker(cancelled=1, tracked=[])
    broker.has_unresolved_cancel = lambda symbol: True
    sched, bot, state, cleared = _scheduler(monkeypatch, broker)
    _sweep(sched)
    assert state.pending_stage == ExitStage.FIRST
    assert SYM not in bot._exit_pending_symbols
    assert cleared == [SYM]
    assert bot.exit_manager.update_price(SYM, D("5000"))[:2] == ("sell_all", 100)


@pytest.mark.parametrize("status", ["incomplete", "unsupported", "ambiguous", "error"])
def test_failed_journal_comparison_does_not_consume_daily_retry(status):
    from src.data.storage.trade_storage import KISSyncResult, sync_kis_journal
    calls = []
    async def compare(broker, engine):
        calls.append(1)
        return KISSyncResult(status if len(calls) == 1 else "reconciled", "synthetic")
    owner = SimpleNamespace(broker=object(), engine=object(),
                            trade_journal=SimpleNamespace(sync_from_kis=compare))
    async def run():
        await sync_kis_journal(owner, close_day=True)
        assert not hasattr(owner, "_last_kis_sync_date")
        assert owner._kis_journal_sync_result.complete is False
        await sync_kis_journal(owner, close_day=True)
        assert owner._last_kis_sync_date == date.today()
    asyncio.run(run())


@pytest.mark.parametrize("outcome", [None, RuntimeError("synthetic"), "empty"])
def test_startup_result_never_suppresses_end_of_day_check(outcome):
    from src.data.storage.trade_storage import KISSyncResult, sync_kis_journal
    async def compare(broker, engine):
        if isinstance(outcome, Exception):
            raise outcome
        return KISSyncResult("verified_empty", "synthetic") if outcome == "empty" else None
    owner = SimpleNamespace(broker=object(), engine=object(),
                            trade_journal=SimpleNamespace(sync_from_kis=compare))
    result = asyncio.run(sync_kis_journal(owner))
    assert not hasattr(owner, "_last_kis_sync_date")
    assert result.complete is (outcome == "empty")


@pytest.mark.parametrize("applied", [True, False])
def test_actual_broker_cancel_through_engine_and_exit_receipt(monkeypatch, tmp_path, broker, applied):
    sched, bot = case(monkeypatch, tmp_path, holdings=100)
    confirmed_journal(bot)
    bot.broker = bot.engine.broker = broker
    async def submit_cancel():
        order = await accepted(broker, OrderSide.SELL)
        await broker.cancel_order(order.id)
        return order
    order = asyncio.run(submit_cancel())
    pending(bot.engine.risk_manager, OrderSide.SELL, order.id, 10)
    response(broker, [row(sll_buy_dvsn_cd="01")])
    if not applied:
        monkeypatch.setattr(bot.engine, "update_position", lambda fill: False)
    _fill_check_once(monkeypatch, sched, [])
    assert broker.has_unresolved_cancel(SYM)
    assert bot.engine.portfolio.positions[SYM].quantity == 100
    asyncio.run(process(bot.engine))
    _fill_check_once(monkeypatch, sched, [])
    assert broker.has_unresolved_cancel(SYM) is not applied
    assert bot.engine.portfolio.positions[SYM].quantity == (97 if applied else 100)
    assert bot.exit_manager.get_state(SYM).remaining_quantity == (97 if applied else 100)
    assert bot.engine.risk_manager._pending_quantities[SYM] == (7 if applied else 10)
    _fill_check_once(monkeypatch, sched, [])
    assert bot.engine.portfolio.positions[SYM].quantity == (97 if applied else 100)


@pytest.mark.parametrize("quantity,new_generation", [(10, False), (3, False), (3, True)])
def test_late_fill_after_stale_cleanup_keeps_original_stage_and_remainder(
        monkeypatch, tmp_path, broker, quantity, new_generation):
    sched, bot = case(monkeypatch, tmp_path, holdings=100)
    confirmed_journal(bot)
    bot.broker = bot.engine.broker = broker
    async def submit_cancel():
        order = await accepted(broker, OrderSide.SELL)
        await broker.cancel_order(order.id)
        return order
    order = asyncio.run(submit_cancel())
    pending(bot.engine.risk_manager, OrderSide.SELL, order.id, 10)
    old_time = datetime.now() - timedelta(minutes=6)
    state = bot.exit_manager.get_state(SYM)
    state.pending_stage, state.pending_target_qty = ExitStage.FIRST, 10
    state.pending_since = old_time
    bot._exit_pending_symbols.add(SYM)
    bot._exit_pending_timestamps[SYM] = old_time
    sched._partial_exit_pending = {SYM: old_time}
    asyncio.run(sched._cleanup_stale_pending())
    assert SYM not in bot.engine.risk_manager._pending_orders
    if new_generation:
        state.pending_generation += 1
        state.pending_target_qty = 5
        pending(bot.engine.risk_manager, OrderSide.SELL, "replacement", 5)
        bot._exit_pending_symbols.add(SYM)
        bot._exit_pending_timestamps[SYM] = datetime.now()
    response(broker, [row(quantity, sll_buy_dvsn_cd="01")])
    _fill_check_once(monkeypatch, sched, [])
    asyncio.run(process(bot.engine))
    _fill_check_once(monkeypatch, sched, [])
    assert state.remaining_quantity == 100 - quantity
    assert not broker.has_unresolved_cancel(SYM)
    assert not sched._canceled_exit_stage_owners
    if new_generation:
        assert state.pending_filled_qty == 0
        assert state.pending_target_qty == 5
    elif quantity == 10:
        assert state.current_stage == ExitStage.FIRST
        assert state.pending_stage is None
    else:
        assert state.pending_filled_qty == 3
        state.pending_since = datetime.now() - timedelta(minutes=6)
        async def absent(symbol): return False
        bot.exit_manager.set_pending_verifier(absent)
        asyncio.run(bot.exit_manager.maybe_expire_pending(SYM))
        retry = bot.exit_manager._check_partial_exit(state, D("12000"), 20)
        assert retry[:2] == ("sell_partial", 7)
        bot.exit_manager.on_fill(SYM, 2, D("12000"))
        bot.exit_manager.rollback_stage(SYM)
        assert bot.exit_manager._check_partial_exit(state, D("12000"), 20)[:2] == ("sell_partial", 5)


@pytest.mark.parametrize("qty", [3, 4])
def test_actual_checked_broker_conflicting_duplicates_cannot_complete_journal(monkeypatch, tmp_path, broker, qty):
    from test_trade_storage_sync_result import Pool
    from src.data.storage.trade_storage import TradeStorage
    monkeypatch.setenv("TRADE_JOURNAL_DIR", str(tmp_path / "journal"))
    storage = TradeStorage(db_url="synthetic")
    storage._db_available = True
    storage.pool = Pool([dict(trade_id="known", symbol=SYM, event_type="BUY",
                              kis_order_no="001", quantity=3, price=D("10000"))])
    response(broker, [row(3), row(qty)])
    result = asyncio.run(storage.sync_from_kis(broker))
    assert result.complete is (qty == 3)
    assert storage._trades == {}


def test_empty_broker_query_does_not_hide_persisted_events(monkeypatch, tmp_path, broker):
    from test_trade_storage_sync_result import Pool
    from src.data.storage.trade_storage import TradeStorage, sync_kis_journal
    monkeypatch.setenv("TRADE_JOURNAL_DIR", str(tmp_path / "journal"))
    storage = TradeStorage(db_url="synthetic")
    storage._db_available = True
    storage.pool = Pool([dict(trade_id="known", symbol=SYM, event_type="BUY",
                              kis_order_no="001", quantity=3, price=D("10000"))])
    response(broker, [])
    owner = SimpleNamespace(broker=broker, trade_journal=storage)
    result = asyncio.run(sync_kis_journal(owner, close_day=True))
    assert not result.complete
    assert not hasattr(owner, "_last_kis_sync_date")


@pytest.mark.parametrize("side,partial,submitted", [
    (OrderSide.BUY, False, False), (OrderSide.SELL, True, False), (OrderSide.SELL, False, True)])
def test_cancel_observed_while_order_event_queued_is_rechecked(monkeypatch, tmp_path, side, partial, submitted):
    _, bot = case(monkeypatch, tmp_path)
    risk = bot.engine.risk_manager
    pending(risk, side)
    risk.engine._pending_sector_map = {}
    if partial:
        risk._pending_signal_cache[SYM]['sell_partial_intent'] = True
    sent = []
    async def submit(order):
        sent.append(order)
        return True, "synthetic"
    risk.engine.broker = SimpleNamespace(has_unresolved_cancel=lambda sym: True, submit_order=submit)
    order = Order(id="replacement", symbol=SYM, side=side, quantity=5, price=D("10000"))
    asyncio.run(risk.on_order(OrderEvent.from_order(order)))
    assert bool(sent) is submitted


def test_stage_retry_cap_survives_state_roundtrip(monkeypatch, tmp_path):
    from src.strategies.exit_manager import ExitManager, ExitConfig
    _, bot = case(monkeypatch, tmp_path, holdings=100)
    em = bot.exit_manager
    state = em.get_state(SYM)
    state.pending_stage, state.pending_target_qty = ExitStage.FIRST, 10
    state.pending_since = datetime.now()
    em.on_fill(SYM, 3, D("12000"))
    em.rollback_stage(SYM)
    pos = bot.engine.portfolio.positions[SYM]
    pos.quantity = 97
    restored = ExitManager(ExitConfig(enable_dynamic_stop=False))
    restored.register_position(pos, stop_loss_pct=5)
    s = restored.get_state(SYM)
    assert s.retry_stage == ExitStage.FIRST and s.retry_quantity == 7
    assert restored._check_partial_exit(s, D("12000"), 20)[:2] == ("sell_partial", 7)


@pytest.mark.parametrize("where", ["hashkey", "rate_limit"])
@pytest.mark.parametrize("side,partial,allowed", [
    (OrderSide.BUY, False, False), (OrderSide.SELL, True, False), (OrderSide.SELL, False, True)])
def test_last_post_guard_preserves_original_partial_intent(ub, where, side, partial, allowed):
    ub._session = _RecSession(_FakeResp(200, OK_BODY))
    unresolved = [False]
    ub.has_unresolved_cancel = lambda sym: unresolved[0]
    async def changed(*args, **kwargs):
        unresolved[0] = True
        return "synthetic-hash"
    setattr(ub, "_get_hashkey" if where == "hashkey" else "_rate_limit", changed)
    order = Order(symbol=SYM, side=side, quantity=5, price=D("10000"), partial_exit=partial)
    ok, _ = asyncio.run(ub.submit_order(order))
    assert ok is allowed
    assert len(ub._session.sent) == (1 if allowed else 0)
