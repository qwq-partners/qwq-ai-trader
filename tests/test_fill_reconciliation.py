"""Actual queue/portfolio/exit components; only synthetic broker and isolated storage."""
import asyncio
from dataclasses import asdict
from decimal import Decimal as D
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.analytics import position_ledger
from src.core.engine import RiskManager, UnifiedEngine
from src.core.event import EventType, FillEvent
from src.core.types import Fill, OrderSide, TradingConfig
from src.strategies.exit_manager import ExitConfig, ExitManager
from src.core.evolution.trade_journal import TradeJournal
from test_kis_reconciliation_activity import broker  # synthetic actual-adapter fixture
from test_sync_portfolio_characterization import _fill_check_once, _make, _pos

SYM = "005930"


def case(monkeypatch, tmp_path, *, holdings=10, reported=7, cash=129936):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(position_ledger, "get_position_ledger", lambda market:
                        SimpleNamespace(on_buy=lambda *a, **k: None, on_sell=lambda *a, **k: None))
    sched, bot, _ = _make(monkeypatch, bot_positions=[_pos(SYM, qty=holdings)] if holdings else [],
                         balance={"stock_value": reported * 10000, "available_cash": cash},
                         kis_seq=[{SYM: _pos(SYM, qty=reported)}] if reported else [{}])
    engine = UnifiedEngine(TradingConfig())
    engine.portfolio = bot.engine.portfolio
    monkeypatch.setattr(engine, "_save_daily_stats", lambda: None)
    risk = object.__new__(RiskManager)
    risk.engine, risk.config = engine, engine.config.risk
    risk._pending_orders = set()
    for field in ("_pending_quantities", "_pending_sides", "_pending_fallback_count",
                  "_pending_timestamps", "_reserved_by_order", "_pending_strategy",
                  "_pending_cancel_keep", "_pending_exit_reasons", "_pending_signal_cache",
                  "_kis_qty_mismatch_count"):
        setattr(risk, field, {})
    risk._pending_lock = asyncio.Lock()
    risk._zombie_candidate_symbols = set()
    engine.risk_manager = risk
    engine.register_handler(EventType.FILL, risk.on_fill)
    bot.engine = engine
    bot.broker._pending_orders = {}
    bot.broker.generation = 0
    bot.broker.reconciliation_token = lambda: (
        None if bot.broker._pending_orders else bot.broker.generation)
    bot.exit_manager = ExitManager(ExitConfig(enable_dynamic_stop=False))
    if holdings:
        bot.exit_manager.register_position(engine.portfolio.positions[SYM], stop_loss_pct=5)
    return sched, bot


def event(side, qty):
    return FillEvent.from_fill(Fill(order_id="synthetic", symbol=SYM, side=side,
                                    quantity=qty, price=D("10000")))


async def process(engine):
    item = await engine._get_next_event()
    assert item is not None
    await engine._process_event(item)


def test_queued_sell_does_not_double_quantity_cash(monkeypatch, tmp_path):
    sched, bot = case(monkeypatch, tmp_path)
    async def run():
        await bot.engine.emit(event(OrderSide.SELL, 3))
        bot.exit_manager.on_fill(SYM, 3, D("10000"))
        await sched._sync_portfolio()
        await process(bot.engine)
        assert bot.engine.portfolio.positions[SYM].quantity == 7
        assert bot.engine.portfolio.cash == D("129936")
        assert bot.exit_manager.get_state(SYM).remaining_quantity == 7
        # Consumed snapshot was never used; the next idle sync now converges.
        await sched._sync_portfolio()
        assert bot.engine.portfolio.positions[SYM].quantity == 7
        assert bot.engine.portfolio.cash == D("129936")
    asyncio.run(run())


def test_queued_buy_does_not_double_quantity_cash(monkeypatch, tmp_path):
    sched, bot = case(monkeypatch, tmp_path, holdings=0, reported=5, cash=50000)
    async def run():
        await bot.engine.emit(event(OrderSide.BUY, 5))
        await sched._sync_portfolio()
        await process(bot.engine)
        pos = bot.engine.portfolio.positions[SYM]
        bot.exit_manager.register_position(pos, stop_loss_pct=5)
        assert (pos.quantity, bot.engine.portfolio.cash) == (5, D("50000"))
        await sched._sync_portfolio()
        assert bot.exit_manager.get_state(SYM).remaining_quantity == 5
    asyncio.run(run())


@pytest.mark.parametrize("where", ["balance", "positions", "metadata"])
def test_fill_during_snapshot_cannot_restore_stale_quantity_cash(monkeypatch, tmp_path, where):
    sched, bot = case(monkeypatch, tmp_path, reported=10, cash=100000)
    async def apply():
        await bot.engine.emit(event(OrderSide.SELL, 3))
        await process(bot.engine)
        bot.exit_manager.on_fill(SYM, 3, D("10000"))
    if where == "metadata":
        bot.broker.positions_seq = [{SYM: _pos(SYM), "000660": _pos("000660")}]
        async def restore(positions): await apply()
        bot._restore_position_metadata = restore
    else:
        name = "get_account_balance" if where == "balance" else "get_positions"
        original = getattr(bot.broker, name)
        async def query():
            await apply()
            return await original()
        setattr(bot.broker, name, query)
    asyncio.run(sched._sync_portfolio())
    assert bot.engine.portfolio.positions[SYM].quantity == 7
    assert bot.engine.portfolio.cash == D("129936")
    assert "000660" not in bot.engine.portfolio.positions


def test_popped_fill_still_prevents_snapshot(monkeypatch, tmp_path):
    sched, bot = case(monkeypatch, tmp_path)
    async def run():
        await bot.engine.emit(event(OrderSide.SELL, 3))
        popped = await bot.engine._get_next_event()
        assert not bot.engine._event_queue
        await sched._sync_portfolio()
        assert bot.engine.portfolio.positions[SYM].quantity == 10
        await bot.engine._process_event(popped)
        assert bot.engine.portfolio.positions[SYM].quantity == 7
    asyncio.run(run())


@pytest.mark.parametrize("cash", [0, None, "NaN", "Infinity", -1])
def test_cash_zero_is_valid_and_invalid_cash_preserves_snapshot(monkeypatch, tmp_path, cash):
    sched, bot = case(monkeypatch, tmp_path, reported=7, cash=cash)
    asyncio.run(sched._sync_portfolio())
    assert bot.engine.portfolio.cash == (D(0) if cash == 0 else D("100000"))
    assert bot.engine.portfolio.positions[SYM].quantity == (7 if cash == 0 else 10)


def test_pending_order_deferral_preserves_exit_state_and_stop(monkeypatch, tmp_path):
    sched, bot = case(monkeypatch, tmp_path)
    state = bot.exit_manager.get_state(SYM)
    before = asdict(state)
    bot.broker._pending_orders["order"] = SimpleNamespace(symbol=SYM)
    asyncio.run(sched._sync_portfolio())
    assert asdict(state) == before
    assert bot.engine.portfolio.positions[SYM].quantity == 10
    assert bot.engine.portfolio.cash == D("100000")
    assert bot.exit_manager.update_price(SYM, D("9000"))[:2] == ("sell_all", 10)


@pytest.mark.parametrize("kind", ["broker", "handoff"])
def test_idle_aba_generation_discards_snapshot(monkeypatch, tmp_path, kind):
    sched, bot = case(monkeypatch, tmp_path)
    original = bot.broker.get_positions
    async def changed():
        if kind == "broker": bot.broker.generation += 2
        else: sched._fill_handoff_generation = 2
        return await original()
    bot.broker.get_positions = changed
    asyncio.run(sched._sync_portfolio())
    assert bot.engine.portfolio.positions[SYM].quantity == 10
    assert bot.engine.portfolio.cash == D("100000")


@pytest.mark.parametrize("mode", ["exception", "missing_sell", "oversell"])
def test_failed_application_is_not_acknowledged_or_reapplied(monkeypatch, tmp_path, mode):
    sched, bot = case(monkeypatch, tmp_path, holdings=0 if mode == "missing_sell" else 10)
    item = event(OrderSide.SELL, 11 if mode == "oversell" else 3)
    if mode == "exception":
        def fail(fill): raise ValueError("synthetic application failure")
        monkeypatch.setattr(bot.engine, "update_position", fail)
    async def run():
        await bot.engine.emit(item)
        await process(bot.engine)
        assert item.portfolio_applied is False
        assert bot.engine.reconciliation_token() is None
        await bot.engine.risk_manager.on_fill(item)
        await sched._sync_portfolio()
        assert bot.engine.portfolio.cash == D("100000")
    asyncio.run(run())


def test_receipt_is_per_increment_and_same_event_is_applied_once(monkeypatch, tmp_path):
    sched, bot = case(monkeypatch, tmp_path)
    a, b = event(OrderSide.BUY, 2), event(OrderSide.BUY, 3)
    assert a.order_id == b.order_id and a.id != b.id
    async def run():
        await bot.engine.emit_many([a, b])
        await process(bot.engine)
        assert a.portfolio_applied is True and b.portfolio_applied is None
        assert bot.engine.reconciliation_token() is None
        await bot.engine.risk_manager.on_fill(a)
        await process(bot.engine)
        assert bot.engine.portfolio.positions[SYM].quantity == 15
        assert bot.engine.portfolio.cash == D("50000")
        assert bot.engine.reconciliation_token() is not None
    asyncio.run(run())


def test_buy_existing_position_waits_for_own_receipt_before_registration(monkeypatch, tmp_path):
    sched, bot = case(monkeypatch, tmp_path, reported=15, cash=50000)
    registrations = []
    original = bot.exit_manager.register_position
    def register(pos, **kwargs):
        registrations.append(pos.quantity)
        return original(pos, **kwargs)
    bot.exit_manager.register_position = register
    bot.broker.fills_seq = [[event(OrderSide.BUY, 5).fill]]
    _fill_check_once(monkeypatch, sched, [])
    assert registrations == []  # existing 10-share position is not an application receipt
    assert bot.engine.portfolio.positions[SYM].quantity == 10
    _fill_check_once(monkeypatch, sched, [])
    assert registrations == []  # generic registration retry also must await the receipt
    asyncio.run(process(bot.engine))
    _fill_check_once(monkeypatch, sched, [])
    assert registrations == [15]
    assert bot.exit_manager.get_state(SYM).remaining_quantity == 15
    assert not sched._pending_fill_handoffs
    assert sched._reconciliation_token() is not None


def test_broker_consumed_before_emit_gap_is_guarded(monkeypatch, tmp_path):
    sched, bot = case(monkeypatch, tmp_path)
    bot.broker.fills_seq = [[event(OrderSide.SELL, 3).fill]]
    original = bot.broker.get_open_orders
    calls = []
    async def open_orders():
        calls.append(1)
        if len(calls) == 2:
            assert not bot.broker._pending_orders
            assert not bot.engine._event_queue
            await sched._sync_portfolio()
            assert bot.engine.portfolio.positions[SYM].quantity == 10
            assert bot.broker.get_positions_calls == 0
        return await original()
    bot.broker.get_open_orders = open_orders
    _fill_check_once(monkeypatch, sched, [])
    assert len(calls) == 2
    assert bot.engine.portfolio.positions[SYM].quantity == 10
    asyncio.run(process(bot.engine))
    assert bot.engine.portfolio.positions[SYM].quantity == 7
    assert bot.engine.portfolio.cash == D("129936")


def test_delayed_buy_completes_journal_owner_and_subscription_once(monkeypatch, tmp_path):
    sched, bot = case(monkeypatch, tmp_path, reported=15)
    bot.trade_journal = TradeJournal(str(tmp_path / "journal"))
    subscriptions = []
    async def subscribe(symbols): subscriptions.append(symbols)
    bot.ws_feed = SimpleNamespace(set_priority_symbols=lambda syms: None, subscribe=subscribe)
    bot.broker.fills_seq = [[event(OrderSide.BUY, 5).fill]]
    _fill_check_once(monkeypatch, sched, [])
    assert not bot.trade_journal._trades and not subscriptions
    assert sched._entry_fill_lots  # risk denominator survives the delayed receipt
    asyncio.run(process(bot.engine))
    _fill_check_once(monkeypatch, sched, [])
    assert len(bot.trade_journal._trades) == 1
    trade = next(iter(bot.trade_journal._trades.values()))
    assert trade.entry_quantity == 5
    assert bot.engine.portfolio.positions[SYM].trade_id == trade.id
    assert subscriptions == [[SYM]]
    assert bot.exit_manager.get_state(SYM).initial_risk_amount is not None
    _fill_check_once(monkeypatch, sched, [])
    assert len(bot.trade_journal._trades) == 1 and subscriptions == [[SYM]]


@pytest.mark.parametrize("sell_qty", [3, 15])
def test_delayed_buy_then_sell_preserves_event_order_without_resurrection(monkeypatch, tmp_path, sell_qty):
    sched, bot = case(monkeypatch, tmp_path)
    bot.trade_journal = TradeJournal(str(tmp_path / "journal"))
    bot.broker.fills_seq = [[event(OrderSide.BUY, 5).fill, event(OrderSide.SELL, sell_qty).fill]]
    _fill_check_once(monkeypatch, sched, [])
    async def apply_both():
        await process(bot.engine)
        await process(bot.engine)
    asyncio.run(apply_both())
    _fill_check_once(monkeypatch, sched, [])
    if sell_qty == 15:
        assert SYM not in bot.engine.portfolio.positions
        assert bot.exit_manager.get_state(SYM) is None
    else:
        assert bot.engine.portfolio.positions[SYM].quantity == 12
        assert bot.exit_manager.get_state(SYM).remaining_quantity == 12
    assert len(bot.trade_journal._trades) == 1
    assert not sched._pending_fill_handoffs


def test_failed_sell_application_does_not_change_exit_or_journal(monkeypatch, tmp_path):
    sched, bot = case(monkeypatch, tmp_path)
    before = asdict(bot.exit_manager.get_state(SYM))
    bot.trade_journal = TradeJournal(str(tmp_path / "journal"))
    bot.broker.fills_seq = [[event(OrderSide.SELL, 11).fill]]
    _fill_check_once(monkeypatch, sched, [])
    asyncio.run(process(bot.engine))
    _fill_check_once(monkeypatch, sched, [])
    assert asdict(bot.exit_manager.get_state(SYM)) == before
    assert not bot.trade_journal._trades
    assert bot.engine.portfolio.positions[SYM].quantity == 10
    assert sched._reconciliation_token() is None


def test_delayed_buy_registration_failure_keeps_initial_risk_for_retry(monkeypatch, tmp_path):
    sched, bot = case(monkeypatch, tmp_path)
    bot.broker.fills_seq = [[event(OrderSide.BUY, 5).fill]]
    _fill_check_once(monkeypatch, sched, [])
    asyncio.run(process(bot.engine))
    original = bot.exit_manager.register_position
    attempts = []
    def flaky(*a, **kw):
        attempts.append(1)
        if len(attempts) == 1: raise ValueError("synthetic registration failure")
        return original(*a, **kw)
    bot.exit_manager.register_position = flaky
    _fill_check_once(monkeypatch, sched, [])
    _fill_check_once(monkeypatch, sched, [])
    assert len(attempts) == 2
    assert bot.exit_manager.get_state(SYM).initial_risk_amount is not None


def test_idle_fill_poll_does_not_invalidate_balance_snapshot(monkeypatch, tmp_path):
    sched, bot = case(monkeypatch, tmp_path)
    original = bot.broker.get_positions
    async def poll_during_query():
        async def stop(sec): bot.running = False
        monkeypatch.setattr(asyncio, "sleep", stop)
        bot.running = True
        await sched.run_fill_check()
        return await original()
    bot.broker.get_positions = poll_during_query
    asyncio.run(sched._sync_portfolio())
    assert bot.engine.portfolio.positions[SYM].quantity == 7


@pytest.mark.parametrize("value", [None, "", "absent", "0"])
def test_actual_cash_adapter_does_not_confuse_missing_with_zero(monkeypatch, tmp_path, broker, value):
    sched, bot = case(monkeypatch, tmp_path)
    async def get(url, tr_id, params, **kw):
        if tr_id == "TTTC8908R":
            return {"rt_cd": "0", "output": {} if value == "absent" else {"nrcvb_buy_amt": value}}
        return {"rt_cd": "0", "_tr_cont": "D", "output1": [],
                "output2": [{"dnca_tot_amt": "100000", "scts_evlu_amt": "70000"}]}
    broker._api_get = get
    bot.broker.get_account_balance = broker.get_account_balance
    asyncio.run(sched._sync_portfolio())
    assert bot.engine.portfolio.cash == (D(0) if value == "0" else D("100000"))
    assert bot.engine.portfolio.positions[SYM].quantity == (7 if value == "0" else 10)


@pytest.mark.parametrize("failure", [ValueError, asyncio.CancelledError])
def test_failed_postprocessing_is_not_replayed(monkeypatch, tmp_path, failure):
    sched, bot = case(monkeypatch, tmp_path)
    bot.broker.fills_seq = [[event(OrderSide.BUY, 5).fill]]
    _fill_check_once(monkeypatch, sched, [])
    asyncio.run(process(bot.engine))
    calls = []
    async def partial(**kwargs):
        calls.append(1)
        raise failure("synthetic interrupted side effect")
    sched._complete_fill_handoff = partial
    _fill_check_once(monkeypatch, sched, [])
    _fill_check_once(monkeypatch, sched, [])
    assert calls == [1]
    assert sched._reconciliation_token() is None


def test_delayed_partial_buys_share_position_journal_identity(monkeypatch, tmp_path):
    sched, bot = case(monkeypatch, tmp_path)
    bot.trade_journal = TradeJournal(str(tmp_path / "journal"))
    bot.broker.fills_seq = [[event(OrderSide.BUY, 2).fill, event(OrderSide.BUY, 3).fill]]
    _fill_check_once(monkeypatch, sched, [])
    async def apply():
        await process(bot.engine)
        await process(bot.engine)
    asyncio.run(apply())
    _fill_check_once(monkeypatch, sched, [])
    assert len(bot.trade_journal._trades) == 1
    assert bot.engine.portfolio.positions[SYM].trade_id == next(iter(bot.trade_journal._trades))
    assert bot.exit_manager.get_state(SYM).remaining_quantity == 15
