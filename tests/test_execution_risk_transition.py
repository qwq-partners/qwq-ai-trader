"""Pure A2b3 intraday-risk candidate tests; no owner, emit, or I/O wiring."""

import asyncio
from copy import deepcopy
from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from src.core.batch_analyzer import BatchAnalyzer
from src.core.types import Position
from src.execution.safety.protection import decode_protection, encode_protection
from src.execution.safety.risk_transition import (
    IntradayPolicyState,
    transition_intraday,
)
from src.strategies.exit_manager import ExitManager


KST = ZoneInfo("Asia/Seoul")
NOW = datetime(2026, 9, 18, 10, 0, tzinfo=KST)
LEVEL_PERCENT = {"normal": -1.49, "caution": -1.5, "crash": -2.5, "severe": -3.5}


def _manager(tmp_path):
    """Use the actual memory-only ExitManager and its checkpoint codec."""
    manager = ExitManager(persist=False, state_dir=tmp_path, clock=lambda: NOW)
    manager.apply_regime_params("trending_bull")
    manager.register_position(Position("005930", quantity=10, avg_price=Decimal("10000"),
                                       current_price=Decimal("10000"), entry_time=NOW))
    manager.register_position(Position("000660", quantity=10, avg_price=Decimal("10000"),
                                       current_price=Decimal("10000"), entry_time=NOW), is_core=True)
    manager.register_position(Position("035420", quantity=10, avg_price=Decimal("10000"),
                                       current_price=Decimal("10000"), entry_time=NOW))
    manager._exit_exempt.add("035420")
    manager.get_state("005930").stop_loss_pct = None
    manager.get_state("005930").trailing_stop_pct = None
    return manager


def _previous(level, *, updated_at=NOW, recovery_until=None):
    return IntradayPolicyState(
        level=level,
        kospi_pct=LEVEL_PERCENT[level],
        updated_at=updated_at,
        recovery_until=recovery_until,
    )


def _actual_batch_transition(monkeypatch, dto, previous, *, change_pct, classified_at):
    """Characterize the unchanged BatchAnalyzer branch with real ExitManager calls."""
    import src.core.batch_analyzer as batch_module

    actual_manager = decode_protection(deepcopy(dto), clock=lambda: classified_at)
    analyzer = object.__new__(BatchAnalyzer)
    analyzer._intraday_state = previous.level
    analyzer._intraday_kospi_pct = previous.kospi_pct
    analyzer._intraday_updated_at = previous.updated_at
    analyzer._intraday_recovery_until = previous.recovery_until
    analyzer._exit_manager = actual_manager
    preemptive = []

    async def fake_preemptive():
        preemptive.append("called")

    analyzer._preemptive_stale_exit_on_bear = fake_preemptive
    monkeypatch.setattr(batch_module, "datetime", SimpleNamespace(now=lambda: classified_at))
    level = asyncio.run(analyzer.update_intraday_state(change_pct))
    return analyzer, encode_protection(actual_manager), preemptive, level


@pytest.mark.parametrize("previous_level", ["normal", "caution", "crash", "severe"])
@pytest.mark.parametrize("next_level", ["normal", "caution", "crash", "severe"])
def test_transition_matches_actual_batch_branch_and_memory_exit_manager(
    monkeypatch, tmp_path, previous_level, next_level
):
    dto = encode_protection(_manager(tmp_path))
    previous = _previous(previous_level)
    classified_at = NOW + timedelta(minutes=1)

    actual, expected_protection, preemptive, level = _actual_batch_transition(
        monkeypatch, dto, previous, change_pct=LEVEL_PERCENT[next_level], classified_at=classified_at
    )
    candidate = transition_intraday(
        previous, dto, change_pct=LEVEL_PERCENT[next_level], classified_at=classified_at
    )

    assert candidate.status == "applied"
    assert level == candidate.state.level == next_level
    assert candidate.state.kospi_pct == LEVEL_PERCENT[next_level]
    assert candidate.state.updated_at == classified_at
    assert candidate.state.recovery_until == actual._intraday_recovery_until
    assert candidate.protection_dto == expected_protection
    assert candidate.preemptive_stale_required is (preemptive == ["called"])


def test_normal_recovery_uses_classification_time_for_five_minute_cooldown_and_utc(tmp_path):
    dto = encode_protection(_manager(tmp_path))
    classified_at = datetime(2026, 9, 18, 1, 0, tzinfo=timezone.utc)

    result = transition_intraday(
        _previous("crash"), dto, change_pct=0.0, classified_at=classified_at
    )

    assert result.state.level == "normal"
    assert result.state.updated_at == classified_at
    assert result.state.recovery_until == classified_at + timedelta(minutes=5)
    restored = decode_protection(result.protection_dto, clock=lambda: classified_at)
    assert restored._intraday_crash_level == "normal"
    assert restored._current_regime == "trending_bull"


def test_same_level_keeps_protection_and_existing_expired_cooldown(tmp_path):
    dto = encode_protection(_manager(tmp_path))
    expired = NOW - timedelta(minutes=1)
    previous = _previous("normal", recovery_until=expired)

    result = transition_intraday(previous, dto, change_pct=0.0, classified_at=NOW)

    assert result.protection_dto == dto
    assert result.state.recovery_until == expired
    assert result.preemptive_stale_required is False


def test_crash_candidate_preserves_core_but_tightens_exit_exempt_and_none_stops(tmp_path):
    dto = encode_protection(_manager(tmp_path))
    before = decode_protection(dto, clock=lambda: NOW)
    core_before = deepcopy(before.get_state("000660"))

    result = transition_intraday(_previous("normal"), dto, change_pct=-2.5, classified_at=NOW)
    after = decode_protection(result.protection_dto, clock=lambda: NOW)

    assert after.get_state("005930").stop_loss_pct == 2.5
    assert after.get_state("005930").trailing_stop_pct == 2.0
    assert after.get_state("000660") == core_before
    assert after.get_state("035420").stop_loss_pct == 2.5
    assert result.preemptive_stale_required is True


@pytest.mark.parametrize("invalid", [None, True, False, float("nan"), float("inf"), float("-inf"), Decimal("NaN")])
def test_missing_inputs_do_not_rebrand_baseline_or_relax_protection(tmp_path, invalid):
    dto = encode_protection(_manager(tmp_path))
    baseline = IntradayPolicyState(level="normal", kospi_pct=0.0, updated_at=None, recovery_until=None)

    result = transition_intraday(baseline, dto, change_pct=invalid, classified_at=NOW)

    assert result.status == "missing"
    assert result.state is baseline
    assert result.protection_dto == dto
    assert result.preemptive_stale_required is False


def test_state_is_frozen_and_canonical_round_trips_without_turning_baseline_into_source_evidence():
    baseline = IntradayPolicyState(level="normal", kospi_pct=0.0, updated_at=None, recovery_until=None)

    payload = baseline.to_dict()
    restored = IntradayPolicyState.from_dict(payload)

    assert restored == baseline
    assert restored.updated_at is None
    with pytest.raises(FrozenInstanceError):
        restored.level = "crash"
    with pytest.raises(ValueError):
        IntradayPolicyState.from_dict({**payload, "updated_at": NOW.isoformat() + "x"})


@pytest.mark.parametrize(
    ("change_pct", "level"),
    [(-1.49, "normal"), (-1.5, "caution"), (-2.49, "caution"),
     (-2.5, "crash"), (-3.49, "crash"), (-3.5, "severe"), (0.0, "normal")],
)
def test_finite_classifier_boundaries_are_the_existing_shared_thresholds(tmp_path, change_pct, level):
    result = transition_intraday(
        _previous("normal"), encode_protection(_manager(tmp_path)),
        change_pct=change_pct, classified_at=NOW,
    )

    assert result.status == "applied"
    assert result.state.level == level


def test_zero_position_protection_clone_still_tightens_global_severe_policy_without_mutating_input(tmp_path):
    manager = ExitManager(persist=False, state_dir=tmp_path, clock=lambda: NOW)
    dto = encode_protection(manager)
    before = deepcopy(dto)

    result = transition_intraday(_previous("normal"), dto, change_pct=-3.5, classified_at=NOW)
    restored = decode_protection(result.protection_dto, clock=lambda: NOW)

    assert dto == before
    assert restored.get_all_states() == {}
    assert restored.config.stop_loss_pct == 2.0
    assert restored.config.trailing_stop_pct == 1.5
    assert result.preemptive_stale_required is True
    with pytest.raises(FrozenInstanceError):
        result.status = "missing"


def test_classification_time_must_be_aware_and_is_not_a_market_as_of(tmp_path):
    with pytest.raises(ValueError, match="classified_at"):
        transition_intraday(
            _previous("normal"), encode_protection(_manager(tmp_path)),
            change_pct=-2.5, classified_at=NOW.replace(tzinfo=None),
        )
