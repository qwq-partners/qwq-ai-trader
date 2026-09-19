"""Pure legacy arithmetic for the regime adapter and risk-manager sidecar.

These helpers deliberately accept explicit legacy state and explicit caller-observed
time.  They do not validate source authority, read clocks/files/providers, log, or
apply protection.  In particular, the partial-index fallback is retained only so
the existing callers keep their historical behavior; it is not an approved input
contract for a future owner.
"""

from dataclasses import dataclass, replace
from datetime import datetime
from types import MappingProxyType
from typing import Mapping, Optional


@dataclass(frozen=True)
class SidecarState:
    market_trend: Mapping[str, object]
    sidecar_active: bool


@dataclass(frozen=True)
class SidecarTrendTransition:
    updated: bool
    market_trend: Optional[Mapping[str, object]]
    sidecar_active: bool


@dataclass(frozen=True)
class MidRegimeState:
    current_regime: str
    pending_present: bool
    pending_regime: Optional[str]
    pending_since: Optional[datetime]


@dataclass(frozen=True)
class MidRegimeFacts:
    kospi_change: object
    kosdaq_change: object
    avg_change: object
    avg_vs_open: object


@dataclass(frozen=True)
class MidRegimePlan:
    state: MidRegimeState
    kospi_change: object
    kosdaq_change: object
    avg_change: object
    avg_vs_open: object
    clock_stage: str
    pending_target: Optional[str]
    confirm_delay_sec: Optional[int]
    vix_state: str
    vix_value: Optional[float]


@dataclass(frozen=True)
class MidRegimeTransition:
    state: MidRegimeState
    kospi_change: object
    kosdaq_change: object
    avg_change: object
    avg_vs_open: object
    fear_demoted: bool


@dataclass(frozen=True)
class ExpertState:
    current_regime: str
    pending_present: bool
    pending_regime: Optional[str]
    pending_since: Optional[datetime]


@dataclass(frozen=True)
class ExpertPlan:
    state: ExpertState
    proposed: Optional[str]
    reason: str
    clock_stage: str


@dataclass(frozen=True)
class ExpertTransition:
    state: ExpertState
    proposed: Optional[str]
    reason: str
    confirmed: bool


def transition_sidecar_trend(
    kospi: Mapping[str, object],
    kosdaq: Mapping[str, object],
    state: SidecarState,
) -> SidecarTrendTransition:
    """Return the legacy sidecar calculation without modifying either input mapping."""
    if not kospi.get("price") and not kosdaq.get("price"):
        return SidecarTrendTransition(False, None, state.sidecar_active)

    def _calc_trend(index: Mapping[str, object]) -> dict:
        price = index.get("price", 0)
        open_price = index.get("open", 0)
        high_price = index.get("high", 0)
        low_price = index.get("low", 0)
        change_pct = index.get("change_pct", 0)
        vs_open = ((price - open_price) / open_price * 100) if open_price > 0 else 0
        intraday_range = high_price - low_price
        position_pct = ((price - low_price) / intraday_range * 100) if intraday_range > 0 else 50
        return {
            "change_pct": change_pct,
            "vs_open_pct": round(vs_open, 2),
            "position_pct": round(position_pct, 1),
        }

    ki = _calc_trend(kospi) if kospi.get("price") else {
        "change_pct": 0, "vs_open_pct": 0, "position_pct": 50,
    }
    kq = _calc_trend(kosdaq) if kosdaq.get("price") else {
        "change_pct": 0, "vs_open_pct": 0, "position_pct": 50,
    }
    avg_change = (ki["change_pct"] + kq["change_pct"]) / 2
    avg_vs_open = (ki["vs_open_pct"] + kq["vs_open_pct"]) / 2
    avg_position = (ki["position_pct"] + kq["position_pct"]) / 2
    if avg_change >= -0.5 and (avg_vs_open >= 0 or avg_position >= 50):
        recovering = True
    elif avg_change < -0.5 and avg_vs_open < 0 and avg_position < 30:
        recovering = False
    else:
        recovering = state.market_trend.get("recovering", True)
    trend = MappingProxyType({
        "kospi_pct": ki["change_pct"],
        "kosdaq_pct": kq["change_pct"],
        "avg_pct": avg_change,
        "vs_open_pct": avg_vs_open,
        "position_pct": avg_position,
        "recovering": recovering,
    })
    return SidecarTrendTransition(True, trend, False if state.sidecar_active and recovering else state.sidecar_active)


def plan_mid_regime_transition(
    state: MidRegimeState,
    facts: MidRegimeFacts,
    now_hm: str,
    vix_state: str,
    vix_value: Optional[float],
) -> MidRegimePlan:
    """Plan one legacy mid-regime transition; time completion is caller-owned."""
    avg_change = facts.avg_change
    avg_vs_open = facts.avg_vs_open
    confirm_delay = 600 if vix_state == "complacency" else 1800

    next_state = state
    stage = "none"
    target = None
    delay = None
    if "09:00" <= now_hm < "10:00":
        next_state = replace(state, current_regime="neutral")
    elif avg_change > 1.0 and avg_vs_open > 0.3:
        if state.current_regime != "bull":
            if not state.pending_present or state.pending_regime != "bull":
                stage, target = "start", "bull"
            else:
                stage, target, delay = "confirm", "bull", confirm_delay
        else:
            next_state = replace(state, current_regime="bull", pending_present=True, pending_regime=None)
    elif avg_change < -1.0 and avg_vs_open < -0.3:
        if state.current_regime != "bear":
            if not state.pending_present or state.pending_regime != "bear":
                stage, target = "start", "bear"
            else:
                stage, target, delay = "confirm", "bear", 1800
        else:
            next_state = replace(state, current_regime="bear", pending_present=True, pending_regime=None)
    else:
        next_state = replace(state, current_regime="sideways", pending_present=True, pending_regime=None)
    return MidRegimePlan(
        next_state, facts.kospi_change, facts.kosdaq_change, avg_change, avg_vs_open,
        stage, target, delay, vix_state, vix_value,
    )


def calculate_mid_regime_facts(
    kospi_data: Mapping[str, object], kosdaq_data: Mapping[str, object],
) -> MidRegimeFacts:
    """Calculate legacy index facts before the caller performs its opening clock read."""
    kospi_change = kospi_data.get("change_pct", 0)
    kosdaq_change = kosdaq_data.get("change_pct", 0)
    avg_change = (kospi_change + kosdaq_change) / 2
    kospi_vs_open = 0
    if kospi_data.get("open", 0) > 0 and kospi_data.get("price", 0) > 0:
        kospi_vs_open = (kospi_data["price"] - kospi_data["open"]) / kospi_data["open"] * 100
    kosdaq_vs_open = 0
    if kosdaq_data.get("open", 0) > 0 and kosdaq_data.get("price", 0) > 0:
        kosdaq_vs_open = (kosdaq_data["price"] - kosdaq_data["open"]) / kosdaq_data["open"] * 100
    avg_vs_open = (kospi_vs_open + kosdaq_vs_open) / 2
    return MidRegimeFacts(kospi_change, kosdaq_change, avg_change, avg_vs_open)


def complete_mid_regime_transition(
    plan: MidRegimePlan, now: Optional[datetime] = None,
) -> MidRegimeTransition:
    """Complete a plan using only a caller-supplied conditional clock value."""
    state = plan.state
    if plan.clock_stage == "start":
        state = replace(state, pending_present=True, pending_regime=plan.pending_target, pending_since=now)
    elif plan.clock_stage == "confirm":
        if (now - state.pending_since).total_seconds() >= plan.confirm_delay_sec:
            state = replace(state, current_regime=plan.pending_target, pending_present=True, pending_regime=None)
    fear_demoted = False
    if plan.vix_value is not None and plan.vix_state == "fear" and state.current_regime == "bull":
        state = replace(state, current_regime="sideways")
        fear_demoted = True
    return MidRegimeTransition(
        state, plan.kospi_change, plan.kosdaq_change, plan.avg_change, plan.avg_vs_open, fear_demoted,
    )


def plan_expert_transition(state: ExpertState, score: object, bear_consensus: object) -> ExpertPlan:
    """Plan the legacy expert proposal without calling the orchestrator or a clock."""
    proposed = None
    reason = ""
    if bear_consensus and state.current_regime in ("bull", "sideways", "neutral"):
        proposed, reason = "bear", f"전문가 BEAR 합의 (score={score})"
    elif score >= 20 and state.current_regime in ("sideways", "neutral"):
        proposed, reason = "bull", f"전문가 BULL 강한 합의 (score={score})"
    elif score <= -20 and state.current_regime in ("bull", "sideways"):
        proposed, reason = "sideways", f"전문가 BEAR (score={score})"
    elif score >= 10 and not bear_consensus and state.current_regime == "bear":
        proposed, reason = "sideways", f"전문가 BEAR 합의 해소 (score={score})"
    if proposed is None:
        return ExpertPlan(replace(state, pending_regime=None) if state.pending_present else state, None, reason, "none")
    if not state.pending_present or state.pending_regime != proposed:
        return ExpertPlan(state, proposed, reason, "start")
    return ExpertPlan(state, proposed, reason, "confirm")


def complete_expert_transition(plan: ExpertPlan, now: Optional[datetime] = None) -> ExpertTransition:
    """Complete the legacy 600-second expert confirmation with explicit time."""
    state = plan.state
    confirmed = False
    if plan.clock_stage == "start":
        state = replace(state, pending_present=True, pending_regime=plan.proposed, pending_since=now)
    elif plan.clock_stage == "confirm" and (now - state.pending_since).total_seconds() >= 600:
        state = replace(state, current_regime=plan.proposed, pending_present=True, pending_regime=None)
        confirmed = True
    return ExpertTransition(state, plan.proposed, plan.reason, confirmed)
