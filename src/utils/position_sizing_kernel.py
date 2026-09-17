"""Pure position-sizing arithmetic extracted in phases from the legacy wrapper.

Resolver, calendar/volatility/conviction providers, metadata writes and entry-risk
snapshots remain in the caller.  This module only consumes already-selected facts.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Literal

from .fee_calculator import FeeCalculator, FeeConfig
from .sizing import atr_position_multiplier, risk_quantity_cap


OverlayKind = Literal["position", "calendar", "volatility", "conviction"]


@dataclass(frozen=True)
class NominalSizingInput:
    equity: Decimal
    pool_equity: Decimal
    base_pct: float
    strength_multiplier: float
    max_pct: float
    global_max_position_pct: float
    available: Decimal


@dataclass(frozen=True)
class RiskSizingInput:
    equity: Decimal
    available: Decimal
    global_max_position_pct: float
    risk_per_trade_pct: float
    risk_max_position_pct: float
    stop_pct: Decimal


@dataclass(frozen=True)
class SizingPhase:
    position_value: Decimal
    max_value: Decimal
    available: Decimal
    strategy_remaining: Decimal | None
    pre_multiplier_value: Decimal | None
    risk_before_overlays: Decimal | None
    risk_applied: bool
    reason: str | None = None


@dataclass(frozen=True)
class FinalizeSizingInput:
    price: Decimal
    min_position_value: Decimal
    buy_commission_rate: Decimal
    stop_pct: Decimal | None
    equity: Decimal
    risk_per_trade_pct: float


@dataclass(frozen=True)
class PositionSizingResult:
    quantity: int
    reason: str | None
    minimum_floor_applied: bool
    risk_cap_quantity: int | None
    quantity_before_risk_cap: int | None
    risk_before_overlays: Decimal | None
    position_value: Decimal
    pre_multiplier_value: Decimal | None


def nominal_initial(value: NominalSizingInput) -> SizingPhase:
    """Legacy nominal base/pool/strength/global-cap calculation."""
    position_pct = min(value.base_pct * value.strength_multiplier, value.max_pct)
    pct_value = value.pool_equity * Decimal(str(position_pct))
    max_value = value.equity * Decimal(str(value.global_max_position_pct / 100))
    return SizingPhase(
        position_value=min(pct_value, max_value, value.available),
        max_value=max_value,
        available=value.available,
        strategy_remaining=None,
        pre_multiplier_value=None,
        risk_before_overlays=None,
        risk_applied=False,
    )


def override_risk_initial(phase: SizingPhase, value: RiskSizingInput) -> SizingPhase:
    """Replace the nominal initial amount after the caller has resolved its stop."""
    risk_value = value.equity * Decimal(str(value.risk_per_trade_pct)) / value.stop_pct
    global_cap = value.equity * Decimal(str(value.global_max_position_pct / 100))
    risk_cap = value.equity * Decimal(str(value.risk_max_position_pct / 100))
    max_value = min(global_cap, risk_cap)
    return replace(
        phase,
        position_value=min(risk_value, max_value, value.available),
        max_value=max_value,
        available=value.available,
        risk_before_overlays=risk_value,
        risk_applied=True,
    )


def apply_strategy_remaining(phase: SizingPhase, remaining: Decimal | None) -> SizingPhase:
    """Apply the already-computed strategy budget remainder without reading holdings."""
    if remaining is None:
        return phase
    if remaining <= 0:
        return replace(phase, strategy_remaining=remaining, reason="strategy_exhausted")
    return replace(
        phase,
        position_value=min(phase.position_value, remaining),
        strategy_remaining=remaining,
    )


def apply_daily_loss(
    phase: SizingPhase,
    *,
    effective_daily_pnl: Decimal,
    equity: Decimal,
    daily_max_loss_pct: float,
) -> SizingPhase:
    """Preserve the legacy half-loss threshold and Decimal half-size operation."""
    daily_pnl_pct = float(effective_daily_pnl / equity * 100) if equity > 0 else 0.0
    if daily_pnl_pct <= -daily_max_loss_pct / 2:
        return replace(phase, position_value=phase.position_value * Decimal("0.5"))
    return phase


def capture_pre_multiplier(phase: SizingPhase) -> SizingPhase:
    """Record the legacy value used by the later minimum-value floor decision."""
    return replace(phase, pre_multiplier_value=phase.position_value)


def should_skip_atr_multiplier(risk_applied: bool, position_multiplier: float, atr_pct: float | None) -> bool:
    """Keep the risk-mode ATR multiplier exception using the shared ATR mapping."""
    return (
        risk_applied
        and atr_pct is not None
        and position_multiplier != 1.0
        and abs(position_multiplier - atr_position_multiplier(atr_pct)) < 1e-6
    )


def apply_overlay(phase: SizingPhase, *, kind: OverlayKind, multiplier: float) -> SizingPhase:
    """Apply exactly one provider result; caller-owned try boundaries remain outside."""
    if kind in {"position", "calendar"}:
        if multiplier != 1.0:
            return replace(
                phase,
                position_value=min(phase.position_value * Decimal(str(multiplier)), phase.max_value),
            )
        return phase
    if kind == "volatility":
        if multiplier < 1.0:
            return replace(phase, position_value=phase.position_value * Decimal(str(multiplier)))
        return phase
    if kind == "conviction":
        if multiplier > 1.0:
            return replace(
                phase,
                position_value=min(phase.position_value * Decimal(str(multiplier)), phase.max_value),
            )
        return phase
    raise ValueError("unsupported sizing overlay")


def finalize_quantity(phase: SizingPhase, value: FinalizeSizingInput) -> PositionSizingResult:
    """Apply final strategy clamp, floor, market affordability, 3-share and fee-risk caps."""
    result = pre_fee_quantity(phase, price=value.price, min_position_value=value.min_position_value)
    return apply_fee_risk_cap(result, value)


def pre_fee_quantity(
    phase: SizingPhase, *, price: Decimal, min_position_value: Decimal,
) -> PositionSizingResult:
    """Quantity/rejection phase before the caller needs any fee configuration."""
    if phase.reason is not None:
        return _result(0, phase.reason, False, None, None, phase)
    position_value = phase.position_value
    if phase.strategy_remaining is not None and position_value > phase.strategy_remaining:
        position_value = phase.strategy_remaining

    floor_applied = False
    if position_value < min_position_value:
        if (phase.pre_multiplier_value is not None
                and phase.pre_multiplier_value >= min_position_value
                and min_position_value <= phase.available
                and min_position_value <= phase.max_value):
            position_value = min_position_value
            floor_applied = True
        else:
            return _result(0, "min_position_value", floor_applied, None, None, phase, position_value)

    quantity = int(position_value / price)
    market_cap = int(phase.available / (price * Decimal("1.3")))
    if market_cap < quantity:
        quantity = market_cap

    if quantity < 3:
        cost_for_min = price * 3 * Decimal("1.001")
        if (cost_for_min <= phase.available and cost_for_min <= phase.max_value
                and (phase.strategy_remaining is None or cost_for_min <= phase.strategy_remaining)):
            quantity = 3
        elif quantity < 1:
            return _result(0, "minimum_quantity", floor_applied, None, None, phase, position_value)

    return _result(quantity, None, floor_applied, None, None, phase, position_value)


def apply_fee_risk_cap(result: PositionSizingResult, value: FinalizeSizingInput) -> PositionSizingResult:
    """Explicit fee-dependent phase, reached only after pre-fee rejection checks."""
    if result.reason is not None or value.stop_pct is None or result.quantity <= 0:
        return result
    calculator = FeeCalculator(FeeConfig(buy_commission_rate=value.buy_commission_rate))
    cap = risk_quantity_cap(value.equity, value.price, value.stop_pct,
                           risk_per_trade_pct=value.risk_per_trade_pct, fee_calc=calculator)
    quantity = min(result.quantity, cap)
    reason = None
    if result.quantity > cap and quantity * value.price < value.min_position_value:
        quantity, reason = 0, "risk_cap_min_position_value"
    return replace(result, quantity=max(quantity, 0), reason=reason,
                   risk_cap_quantity=cap, quantity_before_risk_cap=result.quantity)


def compose_sizing(
    nominal: NominalSizingInput,
    final: FinalizeSizingInput,
    *,
    risk: RiskSizingInput | None = None,
    strategy_remaining: Decimal | None = None,
    daily_loss: tuple[Decimal, Decimal, float] | None = None,
    overlays: tuple[tuple[OverlayKind, float], ...] = (),
) -> PositionSizingResult:
    """Offline/revalidation composition for explicitly supplied facts only."""
    phase = nominal_initial(nominal)
    if risk is not None:
        phase = override_risk_initial(phase, risk)
    phase = apply_strategy_remaining(phase, strategy_remaining)
    if daily_loss is not None:
        pnl, equity, limit = daily_loss
        phase = apply_daily_loss(phase, effective_daily_pnl=pnl, equity=equity, daily_max_loss_pct=limit)
    phase = capture_pre_multiplier(phase)
    for kind, multiplier in overlays:
        phase = apply_overlay(phase, kind=kind, multiplier=multiplier)
    return finalize_quantity(phase, final)


def _result(
    quantity: int,
    reason: str | None,
    floor_applied: bool,
    risk_cap_quantity: int | None,
    quantity_before_risk_cap: int | None,
    phase: SizingPhase,
    position_value: Decimal | None = None,
) -> PositionSizingResult:
    return PositionSizingResult(
        quantity=max(quantity, 0),
        reason=reason,
        minimum_floor_applied=floor_applied,
        risk_cap_quantity=risk_cap_quantity,
        quantity_before_risk_cap=quantity_before_risk_cap,
        risk_before_overlays=phase.risk_before_overlays,
        position_value=phase.position_value if position_value is None else position_value,
        pre_multiplier_value=phase.pre_multiplier_value,
    )
