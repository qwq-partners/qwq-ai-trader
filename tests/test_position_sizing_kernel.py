"""Pure 10B1 sizing phases; no legacy engine or external provider calls."""

from dataclasses import FrozenInstanceError
from decimal import Decimal, InvalidOperation
from types import SimpleNamespace

import pytest

from src.utils.position_sizing_kernel import (
    FinalizeSizingInput,
    NominalSizingInput,
    RiskSizingInput,
    SizingPhase,
    apply_daily_loss,
    apply_overlay,
    apply_strategy_remaining,
    capture_pre_multiplier,
    compose_sizing,
    finalize_quantity,
    nominal_initial,
    override_risk_initial,
    should_skip_atr_multiplier,
)


EQUITY = Decimal("10000000")
PRICE = Decimal("10000")
RATE = Decimal("0.000140527")


def _nominal():
    return NominalSizingInput(
        equity=EQUITY, pool_equity=Decimal("7000000"), base_pct=0.25,
        strength_multiplier=1.0, max_pct=0.28, global_max_position_pct=28.0,
        available=Decimal("7000000"),
    )


def test_nominal_phase_preserves_float_to_decimal_order_and_global_cap():
    phase = nominal_initial(_nominal())

    assert (phase.position_value, phase.max_value, phase.available) == (
        Decimal("1750000.00"), Decimal("2800000.0"), Decimal("7000000"),
    )
    with pytest.raises(FrozenInstanceError):
        phase.position_value = Decimal("0")


def test_risk_override_replaces_nominal_value_and_records_pre_overlay_risk_fact():
    phase = override_risk_initial(
        nominal_initial(_nominal()),
        RiskSizingInput(equity=EQUITY, available=Decimal("7000000"),
                        global_max_position_pct=28.0, risk_per_trade_pct=0.7,
                        risk_max_position_pct=18.0, stop_pct=Decimal("5")),
    )

    assert (phase.position_value, phase.max_value, phase.risk_before_overlays, phase.risk_applied) == (
        Decimal("1400000.0"), Decimal("1800000.0"), Decimal("1400000.0"), True,
    )


def test_strategy_remaining_daily_loss_and_pre_multiplier_are_separate_phases():
    phase = apply_strategy_remaining(nominal_initial(_nominal()), Decimal("1000000"))
    phase = apply_daily_loss(phase, effective_daily_pnl=Decimal("-250000"), equity=EQUITY,
                             daily_max_loss_pct=5.0)
    phase = capture_pre_multiplier(phase)

    assert phase.position_value == Decimal("500000.0")
    assert phase.strategy_remaining == Decimal("1000000")
    assert phase.pre_multiplier_value == Decimal("500000.0")


def test_each_overlay_is_one_ordered_phase_with_legacy_clamp_rules():
    phase = override_risk_initial(
        nominal_initial(_nominal()),
        RiskSizingInput(EQUITY, Decimal("7000000"), 28.0, 0.7, 18.0, Decimal("5")),
    )
    phase = capture_pre_multiplier(phase)
    phase = apply_overlay(phase, kind="position", multiplier=1.3)
    phase = apply_overlay(phase, kind="calendar", multiplier=1.1)
    phase = apply_overlay(phase, kind="volatility", multiplier=0.5)
    phase = apply_overlay(phase, kind="conviction", multiplier=1.2)

    assert phase.position_value == Decimal("1080000.00")
    assert phase.pre_multiplier_value == Decimal("1400000.0")
    assert apply_overlay(phase, kind="volatility", multiplier=1.1) is phase
    assert apply_overlay(phase, kind="conviction", multiplier=0.9) is phase


def test_invalid_overlay_is_not_normalized_and_atr_skip_reuses_existing_mapping():
    phase = nominal_initial(_nominal())

    with pytest.raises(InvalidOperation):
        apply_overlay(phase, kind="calendar", multiplier=None)
    assert should_skip_atr_multiplier(True, 0.65, 6.0) is True
    assert should_skip_atr_multiplier(True, 0.64, 6.0) is False
    assert should_skip_atr_multiplier(False, 0.65, 6.0) is False


def test_finalizer_preserves_minimum_floor_and_three_share_correction():
    floor = SizingPhase(Decimal("175000"), Decimal("2800000"), Decimal("7000000"),
                        None, Decimal("1750000"), None, False)
    three = SizingPhase(Decimal("200000"), Decimal("2800000"), Decimal("7000000"),
                        None, Decimal("200000"), None, False)

    floor_result = finalize_quantity(
        floor, FinalizeSizingInput(PRICE, Decimal("500000"), RATE, None, EQUITY, 0.7)
    )
    three_result = finalize_quantity(
        three, FinalizeSizingInput(Decimal("100000"), Decimal("100000"), RATE, None, EQUITY, 0.7)
    )

    assert (floor_result.quantity, floor_result.minimum_floor_applied, floor_result.reason) == (50, True, None)
    assert (three_result.quantity, three_result.reason) == (3, None)


def test_finalizer_uses_explicit_fee_rate_for_139_cap_without_global_fee(monkeypatch):
    import src.utils.sizing as legacy_sizing

    monkeypatch.setattr(legacy_sizing, "get_fee_calculator", lambda *_args, **_kwargs: pytest.fail("global fee"))
    phase = SizingPhase(Decimal("1400000"), Decimal("1800000"), Decimal("7000000"),
                        None, Decimal("1400000"), Decimal("1400000"), True)

    result = finalize_quantity(
        phase, FinalizeSizingInput(PRICE, Decimal("200000"), RATE, Decimal("5"), EQUITY, 0.7)
    )

    assert (
        result.quantity,
        result.risk_cap_quantity,
        result.quantity_before_risk_cap,
        result.risk_before_overlays,
    ) == (139, 139, 140, Decimal("1400000"))


def test_finalizer_preserves_market_130_affordability_before_risk_cap():
    phase = SizingPhase(Decimal("1300000"), Decimal("2800000"), Decimal("1300000"),
                        None, Decimal("1300000"), None, False)

    result = finalize_quantity(
        phase, FinalizeSizingInput(PRICE, Decimal("200000"), RATE, None, EQUITY, 0.7)
    )

    assert result.quantity == 100


def test_offline_composition_matches_actual_risk_wrapper_fee_boundary(monkeypatch):
    """The real wrapper is the oracle; expected arithmetic is not copied here."""
    from src.core.engine import RiskManager
    from src.core.event import SignalEvent
    from src.core.types import OrderSide, Portfolio, RiskConfig, Signal, SignalStrength, StrategyType
    from src.utils.stop_policy import StopDecision

    monkeypatch.setattr("src.utils.calendar_seasonality.calendar_multiplier", lambda *_args: (1.0, "test"))
    monkeypatch.setattr("src.utils.volatility_targeting.vol_targeting_multiplier", lambda *_args: (1.0, "test"))
    monkeypatch.setattr("src.utils.team_conviction.team_conviction_multiplier", lambda *_args: (1.0, "test"))
    config = RiskConfig(
        sizing_mode="risk", base_position_pct=25.0, max_position_pct=28.0,
        min_position_value=200000, daily_max_loss_pct=5.0,
        risk_per_trade_pct=0.7, risk_max_position_pct=18.0,
        strategy_allocation={"core_holding": 30.0, "sepa_trend": 42.0},
    )
    manager = object.__new__(RiskManager)
    manager.config = config
    manager.engine = SimpleNamespace(
        portfolio=Portfolio(cash=EQUITY, initial_capital=EQUITY),
        get_available_cash=lambda: Decimal("7000000"),
    )
    manager._reserved_by_order = {}
    manager._pending_strategy = {}
    manager._entry_risk_cfg_hash = "position-sizing-kernel-test"
    manager._resolve_entry_stop = lambda _strategy: StopDecision(Decimal("5"), "strategy", False)
    event = SignalEvent.from_signal(Signal(
        symbol="005930", side=OrderSide.BUY, strength=SignalStrength.NORMAL,
        strategy=StrategyType.SEPA_TREND, price=PRICE,
        metadata={"atr_pct": 6.0, "position_multiplier": 0.65},
    ), source="kernel-oracle")

    legacy_quantity = manager._calculate_position_size(event)
    result = compose_sizing(
        _nominal(),
        FinalizeSizingInput(PRICE, Decimal("200000"), RATE, Decimal("5"), EQUITY, 0.7),
        risk=RiskSizingInput(EQUITY, Decimal("7000000"), 28.0, 0.7, 18.0, Decimal("5")),
        strategy_remaining=Decimal("4200000"),
        daily_loss=(Decimal("0"), EQUITY, 5.0),
    )

    assert legacy_quantity == result.quantity == 139
    assert result.quantity_before_risk_cap == 140
