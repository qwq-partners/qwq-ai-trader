"""Offline contracts for startup composite settings and active ATR trailing."""

from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest

from src.core.types import Position
from src.strategies.exit_manager import ExitConfig, ExitManager, ExitStage


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(ExitManager, "_count_business_days", lambda self, start, end: 0)


def registered(config, *, core=False, atr=8.0):
    manager = ExitManager(config)
    manager.register_position(
        Position(symbol="000001", quantity=100, avg_price=Decimal("10000"),
                 current_price=Decimal("10000"), entry_time=datetime.now()),
        is_core=core, atr_pct_hint=atr,
        trailing_stop_pct=12.0 if core else 3.0,
    )
    return manager, manager.get_state("000001")


@pytest.mark.parametrize("enabled,stage,buffer,prev_enabled,data,want_exit", [
    (False, "first", 0.5, True, {"ma5": 11000, "prev_low": 11000, "low": 10900}, False),
    (True, "second", 0.5, True, {"ma5": 11000}, False),
    (True, "first", 2.0, True, {"ma5": 11000}, False),
    (True, "first", 0.5, False, {"prev_low": 11000, "low": 10900}, False),
    (True, "first", 0.5, True, {"ma5": 11000}, True),
    (True, "first", 0.5, True, {"prev_low": 11000, "low": 10900}, True),
])
def test_startup_composite_settings_control_real_exit(
        enabled, stage, buffer, prev_enabled, data, want_exit):
    from scripts.run_trader import build_kr_exit_config

    config = build_kr_exit_config({
        "enable_partial_exit": False, "include_fees": False,
        "enable_composite_trailing": enabled,
        "composite_trail_min_stage": stage,
        "composite_ma5_buffer_pct": buffer,
        "composite_prev_low_enabled": prev_enabled,
    })
    manager, state = registered(config)
    state.current_stage = ExitStage.FIRST
    result = manager.update_price(state.symbol, Decimal("10900"), market_data=data)
    if want_exit:
        assert result[:2] == ("sell_all", 100)
        assert result[2].startswith("복합트레일링:")
    else:
        assert result is None


@pytest.mark.parametrize("stage", [ExitStage.FIRST, ExitStage.SECOND, ExitStage.THIRD, ExitStage.TRAILING])
@pytest.mark.parametrize("cap,price", [(6.0, "11200"), (4.0, "11400")])
def test_active_post_breakeven_atr_cap_keeps_stage_reset_or_sell(stage, cap, price):
    manager, state = registered(ExitConfig(
        enable_partial_exit=False, include_fees=False,
        enable_composite_trailing=False, atr_link_cap_pct=cap,
    ))
    assert state.effective_trailing_stop_pct == cap
    state.current_stage = stage
    state.highest_price = Decimal("12000")
    state.breakeven_activated = True

    # ATR*1.5=12% exceeds both caps. Drawdowns 6.67%/5% must use the cap.
    result = manager.update_price(state.symbol, Decimal(price))
    if stage in (ExitStage.FIRST, ExitStage.SECOND):
        assert result is None
        assert state.highest_price == Decimal(price)
    else:
        assert result[:2] == ("sell_all", 100)
        assert f"한도=-{cap:.1f}%" in result[2]


@pytest.mark.parametrize("core,enabled", [(True, True), (False, False)])
def test_active_cap_preserves_core_and_disabled_policy(core, enabled):
    manager, state = registered(ExitConfig(
        enable_partial_exit=False, include_fees=False,
        enable_composite_trailing=False, enable_atr_linked_trailing=enabled,
        atr_link_cap_pct=4.0,
    ), core=core)
    state.current_stage = ExitStage.THIRD
    state.highest_price = Decimal("12000")
    state.breakeven_activated = True
    assert manager.update_price(state.symbol, Decimal("11200")) is None
    assert state.highest_price == Decimal("12000")
    result = manager.update_price(state.symbol, Decimal("10400"))
    assert result[:2] == ("sell_all", 100)
    assert "한도=-12.0%" in result[2]


def test_cap_does_not_activate_breakeven_before_first_fill():
    manager, state = registered(ExitConfig(include_fees=False, enable_composite_trailing=False))
    assert manager.update_price(state.symbol, Decimal("10600")) is None
    assert state.current_stage is ExitStage.NONE
    assert not state.breakeven_activated
    result = manager.update_price(state.symbol, Decimal("11000"))
    assert result[:2] == ("sell_partial", 10)
    assert state.current_stage is ExitStage.NONE
    assert not state.breakeven_activated
    manager.on_fill(state.symbol, 10, Decimal("11000"))
    assert state.current_stage is ExitStage.FIRST
    assert manager.update_price(state.symbol, Decimal("11000")) is None
    assert state.breakeven_activated


@pytest.mark.parametrize("stage", ["bogus", "FIRST", "", None, 1])
def test_invalid_composite_stage_is_rejected(stage):
    with pytest.raises(ValueError, match="composite_trail_min_stage"):
        ExitConfig(composite_trail_min_stage=stage)


@pytest.mark.parametrize("buffer", [-0.1, 100, float("nan"), float("inf"), None, True, "bad"])
def test_invalid_composite_buffer_is_rejected(buffer):
    with pytest.raises(ValueError, match="composite_ma5_buffer_pct"):
        ExitConfig(composite_ma5_buffer_pct=buffer)


@pytest.mark.parametrize("cap", [0, -1, float("nan"), float("inf"), None, True, "bad"])
def test_enabled_atr_policy_rejects_invalid_cap(cap):
    with pytest.raises(ValueError, match="atr_link_cap_pct"):
        ExitConfig(atr_link_cap_pct=cap)


def test_disabled_atr_policy_does_not_require_unused_cap():
    ExitConfig(enable_atr_linked_trailing=False, atr_link_cap_pct=0)
