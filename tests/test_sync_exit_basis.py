"""Synthetic balance reconciliation, isolated from accounts and persisted state."""
from copy import deepcopy
from dataclasses import asdict
from decimal import Decimal as D
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.core.engine import UnifiedEngine
from src.core.types import Fill, OrderSide, TradingConfig
from src.strategies.exit_manager import ExitConfig, ExitManager, ExitStage
from test_sync_portfolio_characterization import _make, _pos, _run


def setup_case(monkeypatch, tmp_path, *, qty=10, avg="12000", initial="10000"):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    sched, bot, _ = _make(monkeypatch, bot_positions=[_pos("005930", avg=initial)],
                         balance={"stock_value": 110000, "available_cash": 100000},
                         kis_seq=[{"005930": _pos("005930", qty=qty, avg=avg, cur="11000")}])
    bot.exit_manager = ExitManager(ExitConfig(enable_dynamic_stop=False))
    bot.exit_manager.register_position(bot.engine.portfolio.positions["005930"], stop_loss_pct=5)
    bot.engine._position_update_generation = 0
    bot.engine.risk_manager._pending_orders = set()
    bot.engine.risk_manager._pending_quantities = {}
    bot.broker._pending_orders = {}
    return sched, bot, bot.exit_manager.get_state("005930")


def test_same_quantity_avg_correction_restores_net_stop(monkeypatch, tmp_path):
    sched, bot, state = setup_case(monkeypatch, tmp_path)
    before = asdict(state)
    _run(sched)
    assert state.entry_price == D("12000")
    assert bot.engine.portfolio.positions["005930"].avg_price == D("12000")
    after = asdict(state)
    assert {k: v for k, v in after.items() if k != "entry_price"} == {
        k: v for k, v in before.items() if k != "entry_price"}
    signal = bot.exit_manager.update_price("005930", D("11000"))
    assert signal[:2] == ("sell_all", 10)


def test_lower_corrected_basis_avoids_false_stop(monkeypatch, tmp_path):
    sched, bot, state = setup_case(monkeypatch, tmp_path, initial="12000", avg="10000")
    _run(sched)
    assert state.entry_price == D("10000")
    assert bot.exit_manager.update_price("005930", D("10000")) is None


@pytest.mark.parametrize("qty", [7, 12])
def test_quantity_change_does_not_rewrite_exit_state_or_block_stop(monkeypatch, tmp_path, qty):
    sched, bot, state = setup_case(monkeypatch, tmp_path, qty=qty)
    before = asdict(state)
    _run(sched)
    assert asdict(state) == before
    assert bot.engine.portfolio.positions["005930"].quantity == qty
    assert bot.exit_manager.update_price("005930", D("9000"))[:2] == ("sell_all", 10)


@pytest.mark.parametrize("pending", ["broker_buy", "broker_sell", "risk", "quantity", "exit", "stage"])
def test_pending_paths_do_not_rebase_or_clear_pending(monkeypatch, tmp_path, pending):
    sched, bot, state = setup_case(monkeypatch, tmp_path)
    if pending.startswith("broker"):
        bot.broker._pending_orders["order"] = SimpleNamespace(symbol="005930", side=pending)
    elif pending == "risk": bot.engine.risk_manager._pending_orders.add("005930")
    elif pending == "quantity": bot.engine.risk_manager._pending_quantities["005930"] = 1
    elif pending == "exit": bot._exit_pending_symbols.add("005930")
    else:
        state.pending_stage = ExitStage.FIRST
        state.pending_target_qty = 3
        state.pending_filled_qty = 1
    before = asdict(state)
    _run(sched)
    assert asdict(state) == before


@pytest.mark.parametrize("value", ["0", "-1", "NaN", "Infinity", "-Infinity"])
def test_invalid_basis_never_applied(monkeypatch, tmp_path, value):
    sched, bot, state = setup_case(monkeypatch, tmp_path, avg=value)
    _run(sched)
    assert state.entry_price == D("10000")
    assert bot.engine.portfolio.positions["005930"].avg_price == D("10000")
    assert bot.risk_manager.sync_status[-1] is True


@pytest.mark.parametrize("mutation", ["fill_generation", "position", "state", "pending"])
def test_change_during_broker_await_defers_basis(monkeypatch, tmp_path, mutation):
    sched, bot, state = setup_case(monkeypatch, tmp_path)
    original = bot.broker.get_positions
    async def changed():
        if mutation == "fill_generation": bot.engine._position_update_generation += 2
        elif mutation == "position":
            bot.engine.portfolio.positions["005930"] = deepcopy(bot.engine.portfolio.positions["005930"])
        elif mutation == "state": bot.exit_manager._states["005930"] = deepcopy(state)
        else: bot.engine.risk_manager._pending_orders.add("005930")
        return await original()
    bot.broker.get_positions = changed
    _run(sched)
    assert bot.exit_manager.get_state("005930").entry_price == D("10000")


def test_partial_exit_basis_preserves_slot_risk_and_later_fill(monkeypatch, tmp_path):
    sched, bot, state = setup_case(monkeypatch, tmp_path, qty=7)
    bot.engine.portfolio.positions["005930"].quantity = 7
    state.remaining_quantity = 7
    state.current_stage = ExitStage.FIRST
    state.initial_risk_amount = D("5000")
    state.actual_stop_pct = D("5")
    state.breakeven_activated = True
    state.effective_trailing_stop_pct = 4.2
    before = asdict(state)
    _run(sched)
    assert state.entry_price == D("12000")
    assert {k: v for k, v in asdict(state).items() if k != "entry_price"} == {
        k: v for k, v in before.items() if k != "entry_price"}
    bot.exit_manager.on_fill("005930", 2, D("13000"))
    assert state.remaining_quantity == 5 and state.original_quantity == 10
    assert state.total_realized_pnl == bot.exit_manager.fee_calc.calculate_net_pnl(D("12000"), D("13000"), 2)[0]


def test_unknown_pending_adapter_defers_basis(monkeypatch, tmp_path):
    sched, bot, state = setup_case(monkeypatch, tmp_path)
    del bot.broker._pending_orders
    _run(sched)
    assert state.entry_price == D("10000")


def test_preexisting_exit_quantity_mismatch_is_not_rebased(monkeypatch, tmp_path):
    sched, bot, state = setup_case(monkeypatch, tmp_path)
    state.remaining_quantity = 7
    _run(sched)
    assert state.entry_price == D("10000") and state.remaining_quantity == 7


def test_actual_buy_sell_aba_during_snapshot_defers_basis(monkeypatch, tmp_path):
    from src.analytics import position_ledger
    sched, bot, state = setup_case(monkeypatch, tmp_path)
    engine = UnifiedEngine(TradingConfig())
    engine.portfolio = bot.engine.portfolio
    engine.risk_manager = bot.engine.risk_manager
    bot.engine = engine
    monkeypatch.setattr(engine, '_save_daily_stats', lambda: None)
    monkeypatch.setattr(position_ledger, 'get_position_ledger', lambda market:
                        SimpleNamespace(on_buy=lambda *a, **k: None, on_sell=lambda *a, **k: None))
    original = bot.broker.get_positions
    async def fills_during_snapshot():
        for side in (OrderSide.BUY, OrderSide.SELL):
            engine.update_position(Fill(order_id="synthetic-" + side.value, symbol="005930",
                                        side=side, quantity=1, price=D("10000")))
        assert engine.portfolio.positions["005930"].quantity == 10
        assert engine.portfolio.positions["005930"].avg_price == D("10000")
        return await original()
    bot.broker.get_positions = fills_during_snapshot
    _run(sched)
    assert engine._position_update_generation == 2
    assert state.entry_price == D("10000")


def test_engine_fill_generation_detects_even_ignored_fill():
    engine = object.__new__(UnifiedEngine)
    engine.portfolio = SimpleNamespace(positions={})
    engine._position_update_generation = 3
    engine.update_position(Fill(order_id="synthetic", symbol="005930", side=OrderSide.SELL,
                                quantity=1, price=D("10000")))
    assert engine._position_update_generation == 4
