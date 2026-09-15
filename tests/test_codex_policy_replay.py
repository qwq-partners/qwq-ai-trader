"""오프라인 정책 재현: 관측 가능한 갭 체결과 스냅샷 기준 시계."""

from datetime import datetime, timezone, timedelta

import pytest

from scripts import team_policy_ab as replay
from src.agents import types as agent_types


def _bar(day, opening, high, low, close):
    return dict(date=f"2026-08-{day:02d}", open=opening, high=high, low=low, close=close)


def test_fixed_stop_gap_fills_at_open_even_when_day_recovers_above_take_profit():
    # 1,000주: 매수 10,000,000+1,405원, 시가 매도 9,000,000-19,175원.
    # 고가가 회복해도 시가에 먼저 도달한 손절을 취소할 수 없다.
    for high in (9200, 12000):
        result = replay.simulate_exit([
            _bar(1, 10000, 10100, 9900, 10000),
            _bar(2, 9000, high, 8800, 9000),
        ], 0, 10000, 9500)
        assert result["exit_reason"] == "stop_loss"
        assert result["holding_days"] == 1
        assert result["net_pct"] == pytest.approx(-10.2043662865)


def test_previously_armed_trailing_gap_exits_before_new_high_and_take_profits():
    # 전일 고점 10,600 → 기존 트레일 10,282. 다음 시가 10,100으로 전량 청산.
    result = replay.simulate_exit([
        _bar(1, 10000, 10100, 9900, 10000),
        _bar(2, 10400, 10600, 10350, 10500),
        _bar(3, 10100, 12000, 10000, 11500),
    ], 0, 10000, 9500)
    assert result["exit_reason"] == "trailing"
    assert result["holding_days"] == 2
    assert result["net_pct"] == pytest.approx(77077 / 10001405 * 100)


def test_partial_take_profit_then_trailing_gap_values_only_remaining_position_at_open():
    # 10%는 11,000(순매각 10,976,564), 잔여 90%는 갭 시가 10,300(10,278,056).
    result = replay.simulate_exit([
        _bar(1, 10000, 10100, 9900, 10000),
        _bar(2, 10800, 11000, 10800, 10900),
        _bar(3, 10300, 12000, 10200, 11500),
    ], 0, 10000, 9500)
    assert result["exit_reason"] == "trailing"
    assert result["net_pct"] == pytest.approx((0.1 * 975159 + 0.9 * 276651) / 10001405 * 100)


def test_intraday_stop_and_profit_ambiguity_remains_stop_first():
    result = replay.simulate_exit([_bar(1, 10000, 12000, 9400, 11500)], 0, 10000, 9500)
    assert result["exit_reason"] == "stop_loss"
    assert result["net_pct"] == pytest.approx(-5.2157171917)


def _candidate(*, decided_at="2026-08-03T10:00:00+09:00", age=10):
    return replay.Candidate(
        date="2026-08-03", symbol="005930",
        plan={"decided_at": decided_at, "score": 80},
        votes={"r1": {"bull": True, "bear": True}, "r2": {"bull": True, "bear": True}},
        evidence=[{
            "kind": "technical", "score": 30, "confidence": 0.8,
            "data_status": "full", "age_minutes": age, "positive_basis": True,
            "evidence": [{"source": "ohlcv", "metric": "breakout", "value": 1,
                          "kind": "fact", "status": "full",
                          "observed_at": "2026-08-03T09:50:00+09:00",
                          "valid_until": "2026-08-03T10:30:00+09:00"}],
        }],
    )


@pytest.mark.parametrize("wall_year", [2026, 2030])
def test_historical_snapshot_gates_and_confidence_are_independent_of_execution_date(monkeypatch, wall_year):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            frozen = cls(wall_year, 9, 15, 12, tzinfo=timezone(timedelta(hours=9)))
            return frozen.astimezone(tz) if tz else frozen.replace(tzinfo=None)

    monkeypatch.setattr(replay, "datetime", Clock)
    monkeypatch.setattr(agent_types, "datetime", Clock)
    candidate = _candidate()
    assessment = replay._assess(candidate, 1)
    assert replay.gate_b(candidate) is True
    assert replay.gate_c(candidate) is True
    assert assessment.merit_score == 30
    assert assessment.evidence_quality["weight"] == 0.713


@pytest.mark.parametrize("decided_at", ["2026-08-03T10:00:00", "2026-08-03T01:00:00+00:00"])
def test_aware_and_naive_decision_times_use_same_kst_instant(decided_at):
    assert replay.gate_b(_candidate(decided_at=decided_at)) is True


def test_evidence_expiry_is_checked_at_decision_and_original_observation_is_preserved():
    candidate = _candidate()
    candidate.evidence[0]["evidence"][0]["valid_until"] = "2026-08-03T09:59:59+09:00"
    assert replay.gate_b(candidate) is False
    report = replay._to_analyst_report(candidate.evidence[0], now=datetime(2026, 8, 3, 10))
    assert report.evidence[0].observed_at.isoformat() == "2026-08-03T09:50:00+09:00"
    assert report.evidence[0].valid_until.isoformat() == "2026-08-03T09:59:59+09:00"


def test_report_stale_at_decision_is_excluded_despite_unexpired_evidence():
    assert replay.gate_b(_candidate(age=46)) is False  # technical HARD_TTL 45분


def test_date_only_legacy_candidate_uses_kst_midnight_and_restores_age():
    candidate = _candidate()
    candidate.plan.pop("decided_at")
    candidate.evidence[0]["evidence"][0]["observed_at"] = "2026-08-02T23:50:00+09:00"
    candidate.evidence[0]["evidence"][0]["valid_until"] = "2026-08-03T00:01:00+09:00"
    assert replay.gate_b(candidate) is True
    assert replay._decision_time(candidate).isoformat() == "2026-08-03T00:00:00+09:00"


@pytest.mark.parametrize("bad_time", ["not-a-time", "2026-08-03", "", None])
def test_explicit_invalid_decision_time_does_not_fall_back_to_date(bad_time):
    candidate = _candidate(decided_at=bad_time)
    assert replay.gate_b(candidate) is False
    assert "decision_time" in replay._assess(candidate, 1).abstain_reason


@pytest.mark.parametrize("bad_age", [None, -1, float("nan"), float("inf"), 1e100, "10", True])
def test_missing_or_invalid_report_time_never_invents_freshness(bad_age):
    candidate = _candidate(age=bad_age)
    assert replay.gate_b(candidate) is False


def test_original_report_data_as_of_takes_precedence_over_serialized_age():
    candidate = _candidate(age=0)
    candidate.evidence[0]["data_as_of"] = "2026-08-03T09:00:00+09:00"
    assert replay.gate_b(candidate) is False
    candidate.evidence[0]["data_as_of"] = "2026-08-03T09:50:00+09:00"
    assert replay.gate_b(candidate) is True


@pytest.mark.parametrize("as_of", [None, "garbled", "2026-08-03T10:01:00+09:00"])
def test_invalid_or_future_original_report_time_does_not_fall_back_to_fresh_age(as_of):
    candidate = _candidate(age=0)
    candidate.evidence[0]["data_as_of"] = as_of
    assert replay.gate_b(candidate) is False


def test_legacy_candidate_without_any_decision_date_abstains():
    candidate = _candidate()
    candidate.plan.pop("decided_at")
    candidate.date = ""
    assert replay.gate_b(candidate) is False


def test_malformed_expiry_is_not_restored_as_unlimited_validity():
    candidate = _candidate()
    candidate.evidence[0]["evidence"][0]["valid_until"] = "garbled"
    assert replay._to_evidence_item(candidate.evidence[0]["evidence"][0]).usable is False
    assert replay.gate_b(candidate) is False


def test_entry_plan_invalid_bar_date_does_not_use_execution_clock():
    candidate = _candidate()
    candidate.prices = [dict(date="garbled", close=10000)]
    assert replay._entry_plan_fill(candidate, 0) == (None, None, "invalid_bar_time")


def test_snapshot_restores_explicit_validation_bonus_without_assuming_risk_clear_is_ten():
    candidate = _candidate()
    candidate.evidence[0].update(risk_clear=True, validation_pass_bonus=0)
    assert replay._assess(candidate, 1).merit_score == 30
