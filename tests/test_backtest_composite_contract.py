"""Composite config must reach the daily evaluator; full tick parity is separate."""
import copy

import pandas as pd
import pytest

from tests.test_backtest_gate_config import bt, EFFECTIVE


def effective(**settings):
    result = copy.deepcopy(EFFECTIVE)
    result["kr"]["exit_manager"].update(settings)
    return result


@pytest.mark.parametrize("settings,expected", [
    ({"composite_trail_min_stage": "second"}, ("second", 0.5, True)),
    ({"composite_ma5_buffer_pct": 2.0}, ("first", 2.0, True)),
    ({"composite_prev_low_enabled": False}, ("first", 0.5, False)),
])
def test_effective_builder_preserves_composite_contract(settings, expected):
    cfg = bt.build_backtest_config_from_effective(effective(**settings), strategies=["sepa"], months=6)
    assert (cfg.composite_trail_min_stage, cfg.composite_ma5_buffer_pct,
            cfg.composite_prev_low_enabled) == expected


@pytest.mark.parametrize("settings,row,want_exit", [
    ({"composite_trail_min_stage": "second"}, {"ma5": 11000}, False),
    ({"composite_ma5_buffer_pct": 2.0}, {"ma5": 11000}, False),
    ({"composite_prev_low_enabled": False}, {"prev_low": 11000}, False),
    ({}, {"ma5": 11000}, True),
    ({}, {"prev_low": 11000}, True),
])
def test_daily_composite_uses_requested_settings(settings, row, want_exit):
    assert_daily_composite(settings, row, want_exit, bt.ExitStage.FIRST)


def assert_daily_composite(settings, row, want_exit, stage):
    cfg = bt.BacktestConfig(enable_composite_exit=True, first_exit_pct=10, second_exit_pct=15)
    for key, value in settings.items():
        setattr(cfg, key, value)
    pos = bt.BTPosition(symbol="X", name="X", strategy=bt.StrategyType.SEPA,
                        entry_date="2026-10-16", entry_price=10000, quantity=100,
                        cost_basis=1000000, highest_price=11000)
    pos.exit_stage = stage
    pos.remaining_quantity = 90
    result = bt.BTExitManager(cfg).check_exit(pos, pd.Series({
        "시가": 10900, "고가": 10900, "저가": 10900, "종가": 10900, **row}))
    assert bool(result) is want_exit
    if want_exit:
        assert "복합청산" in result[0][3]


def test_gate_scope_does_not_claim_full_live_exit_parity():
    cfg = bt.build_backtest_config_from_effective(EFFECTIVE, strategies=["sepa"], months=6)
    assert cfg.supported_scope["exit_evaluation"] == "daily_ohlc_approximation"
    assert cfg.supported_scope["live_exit_parity"] is False
    assert "first_second_trailing_reset" in cfg.supported_scope["exit_parity_gaps"]
    assert "third_to_trailing_promotion" in cfg.supported_scope["exit_parity_gaps"]
    assert "unset_exit_defaults" in cfg.supported_scope["exit_parity_gaps"]


def test_effective_builder_preserves_atr_contract():
    cfg = bt.build_backtest_config_from_effective(effective(
        atr_link_multiplier=1.1, atr_link_cap_pct=4.0,
        enable_atr_linked_trailing=False), strategies=["sepa"], months=6)
    assert (cfg.enable_atr_linked_trailing, cfg.atr_link_multiplier, cfg.atr_link_cap_pct) == (False, 1.1, 4.0)


@pytest.mark.parametrize("minimum", list(bt.ExitStage))
@pytest.mark.parametrize("stage", list(bt.ExitStage))
def test_composite_minimum_stage_uses_enum_order(minimum, stage):
    # The existing daily evaluator promotes THIRD to TRAILING before this check.
    evaluated_stage = bt.ExitStage.TRAILING if stage is bt.ExitStage.THIRD else stage
    assert_daily_composite({"composite_trail_min_stage": minimum.name.lower()},
                          {"ma5": 11000}, evaluated_stage.value >= minimum.value, stage)
