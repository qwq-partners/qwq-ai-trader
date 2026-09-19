"""Pure arithmetic contracts for the legacy regime transition sidecars."""

from datetime import datetime, timedelta

from src.utils.regime_transition import (
    calculate_mid_regime_facts,
    ExpertState,
    MidRegimeState,
    SidecarState,
    complete_expert_transition,
    complete_mid_regime_transition,
    plan_expert_transition,
    plan_mid_regime_transition,
    transition_sidecar_trend,
)


def test_sidecar_transition_preserves_legacy_partial_index_fallback_without_mutating_inputs():
    """A one-index payload still uses the original zero/default arithmetic."""
    kospi = {"price": 98.0, "open": 100.0, "high": 102.0, "low": 94.0, "change_pct": -1.0}
    kosdaq = {}

    result = transition_sidecar_trend(
        kospi,
        kosdaq,
        SidecarState(market_trend={"recovering": False}, sidecar_active=True),
    )

    assert result.updated is True
    assert result.sidecar_active is False
    assert dict(result.market_trend) == {
        "kospi_pct": -1.0,
        "kosdaq_pct": 0,
        "avg_pct": -0.5,
        "vs_open_pct": -1.0,
        "position_pct": 50.0,
        "recovering": True,
    }
    assert kospi == {"price": 98.0, "open": 100.0, "high": 102.0, "low": 94.0, "change_pct": -1.0}
    assert kosdaq == {}


def test_mid_regime_plan_keeps_strict_thresholds_pending_delays_and_nonnull_vix_fear():
    """Changing a strict boundary, delay, or null fear behavior must fail this test."""
    state = MidRegimeState("sideways", False, None, None)
    bull = {"change_pct": 1.1, "open": 100.0, "price": 100.4}

    open_neutral = plan_mid_regime_transition(
        state, calculate_mid_regime_facts(bull, bull), "09:00", "normal", None
    )
    assert open_neutral.clock_stage == "none"
    assert complete_mid_regime_transition(open_neutral).state.current_regime == "neutral"
    pending_at = datetime(2026, 9, 18, 8, 59)
    open_with_pending = plan_mid_regime_transition(
        MidRegimeState("bull", True, "bear", pending_at),
        calculate_mid_regime_facts(bull, bull), "09:30", "normal", None,
    )
    assert complete_mid_regime_transition(open_with_pending).state == MidRegimeState(
        "neutral", True, "bear", pending_at,
    )
    boundary = {"change_pct": 1.0, "open": 100.0, "price": 100.4}
    strict_boundary = plan_mid_regime_transition(
        state, calculate_mid_regime_facts(boundary, boundary), "10:00", "normal", None
    )
    assert complete_mid_regime_transition(strict_boundary).state.current_regime == "sideways"

    bull_facts = calculate_mid_regime_facts(bull, bull)
    first = plan_mid_regime_transition(state, bull_facts, "10:00", "complacency", 14.0)
    assert first.clock_stage == "start"
    pending_at = datetime(2026, 9, 18, 10, 0)
    pending = complete_mid_regime_transition(first, pending_at)
    assert pending.state == MidRegimeState("sideways", True, "bull", pending_at)

    confirmed = complete_mid_regime_transition(
        plan_mid_regime_transition(pending.state, bull_facts, "10:10", "complacency", 14.0),
        pending_at + timedelta(seconds=600),
    )
    assert confirmed.state == MidRegimeState("bull", True, None, pending_at)

    fear_without_value = complete_mid_regime_transition(
        plan_mid_regime_transition(state, bull_facts, "10:00", "fear", None), pending_at
    )
    assert fear_without_value.state.current_regime == "sideways"
    assert fear_without_value.fear_demoted is False

    fear_with_value = complete_mid_regime_transition(
        plan_mid_regime_transition(
            MidRegimeState("bull", True, None, pending_at), bull_facts, "10:00", "fear", 30.0
        )
    )
    assert fear_with_value.state.current_regime == "sideways"
    assert fear_with_value.fear_demoted is True


def test_expert_transition_keeps_pending_attribute_presence_and_600_second_confirmation():
    """The expert path must retain its separate pending state and never touch last-update."""
    state = ExpertState("sideways", False, None, None)
    first = plan_expert_transition(state, score=25, bear_consensus=False)
    assert first.proposed == "bull"
    assert first.clock_stage == "start"
    pending_at = datetime(2026, 9, 18, 10, 0)
    pending = complete_expert_transition(first, pending_at)
    assert pending.state == ExpertState("sideways", True, "bull", pending_at)

    confirmed = complete_expert_transition(
        plan_expert_transition(pending.state, score=25, bear_consensus=False),
        pending_at + timedelta(seconds=600),
    )
    assert confirmed.state == ExpertState("bull", True, None, pending_at)

    cleared = complete_expert_transition(
        plan_expert_transition(confirmed.state, score=0, bear_consensus=False)
    )
    assert cleared.state == ExpertState("bull", True, None, pending_at)
