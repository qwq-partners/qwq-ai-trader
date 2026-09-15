"""replay의 혼합 시간대·명시 관측 시각·검증 오류 분류 회귀."""

from copy import deepcopy
from datetime import datetime

import pytest

from scripts import team_policy_ab as replay
from src.execution.entry_plan import check_entry_plan


EXPIRIES = ["2026-08-04T15:30:00", "2026-08-04T15:30:00+09:00", "2026-08-04T06:30:00+00:00"]


def _candidate():
    return replay.Candidate(
        date="2026-08-03", symbol="005930",
        plan={"plan_id": "p", "score": 80, "stop_price": 95, "decided_at": "2026-08-03T10:00:00"},
        votes={"r1": {"bull": True, "bear": True}, "r2": {"bull": True, "bear": True}},
        prices=[{"date": f"2026-08-{day:02d}", "open": 100, "high": 101, "low": 99, "close": 100}
                for day in range(3, 7)],
        evidence=[{
            "kind": "technical", "score": 30, "confidence": 0.8,
            "data_status": "full", "data_as_of": "2026-08-03T09:50:00",
            "positive_basis": True, "observed_at": None,
            "evidence": [{"source": "ohlcv", "metric": "breakout", "value": 1,
                          "status": "full", "kind": "fact", "observed_at": None}],
        }],
    )


@pytest.mark.parametrize("expiry", EXPIRIES)
@pytest.mark.parametrize("location", ["plan", "invalidation"])
def test_same_expiry_in_naive_kst_aware_kst_and_utc_fills_same_replay_bar(expiry, location):
    c = _candidate()
    if location == "plan":
        c.plan["expires_at"] = expiry
    else:
        c.plan["invalidation"] = {"expires_at": expiry}
    assert replay._entry_plan_fill(c, 1) == (1, 100.0, None)


@pytest.mark.parametrize("now", ["2026-08-04T15:30:00", "2026-08-04T06:30:00+00:00"])
@pytest.mark.parametrize(("expiry", "expected"), [
    ("2026-08-04T06:29:59+00:00", "reject"),
    ("2026-08-04T15:30:00", "allow"),
    ("2026-08-04T15:30:01+09:00", "allow"),
])
def test_expiry_boundary_compares_instants_and_keeps_audit_time(now, expiry, expected):
    reference = datetime.fromisoformat(now)
    quote = {"price": 100, "as_of": "2026-08-04T15:30:00+09:00"}
    check = check_entry_plan({"expires_at": expiry}, quote, reference)
    assert check.status == expected
    assert check.reasons == (["PLAN_EXPIRED"] if expected == "reject" else [])
    assert check.checked_at == reference
    assert check.to_dict()["checked_at"] == now
    assert check.to_dict()["quote_as_of"] == "2026-08-04T15:30:00+09:00"


@pytest.mark.parametrize("now", ["2026-08-04T15:30:00", "2026-08-04T06:30:00+00:00"])
@pytest.mark.parametrize(("as_of", "expected"), [
    ("2026-08-04T06:25:00+00:00", "allow"),
    ("2026-08-04T15:25:00", "allow"),
    ("2026-08-04T15:24:59+09:00", "wait"),
    ("2026-08-04T06:30:01+00:00", "wait"),
])
def test_quote_age_boundary_accepts_same_instant_across_timezones(now, as_of, expected):
    check = check_entry_plan({"plan_id": "p"}, {"price": 100, "as_of": as_of}, datetime.fromisoformat(now))
    assert check.status == expected
    assert check.reasons == (["QUOTE_STALE"] if expected == "wait" else [])


def test_real_checker_error_is_returned_instead_of_normal_no_fill():
    c = _candidate()
    c.plan["required_inputs"] = 1  # 잘못된 목록: 실제 checker가 TypeError를 CHECKER_ERROR로 반환
    assert replay._entry_plan_fill(c, 1) == (None, None, "CHECKER_ERROR:TypeError")


def test_timing_report_separates_checker_errors_from_unfilled_and_opportunity_cost():
    c = _candidate()
    c.plan["required_inputs"] = 1
    result = replay.run_timing_experiment([c], fixed_policy="A", max_new=5)["entry_plan"]
    assert result["checker_errors"] == 1
    assert result["checker_error_reasons"] == {"CHECKER_ERROR:TypeError": 1}
    assert result["candidates_selected"] == 1
    assert result["timing_candidates"] == 1
    assert result["evaluated_candidates"] == 0
    assert result["unfilled"] == 0
    assert result["fill_rate"] is None
    assert result["unfilled_opportunity_cost_median_r"] is None


def test_mixed_checker_error_and_success_exposes_original_and_valid_denominators():
    bad = _candidate()
    bad.plan["required_inputs"] = 1
    valid = _candidate()
    valid.symbol = "000001"
    result = replay.run_timing_experiment([bad, valid], fixed_policy="A", max_new=5)["entry_plan"]
    assert result["candidates_selected"] == 2
    assert result["timing_candidates"] == 2
    assert result["checker_errors"] == 1
    assert result["evaluated_candidates"] == 1
    assert result["completed_positions"] == 1
    assert result["unfilled"] == 0
    assert result["fill_rate"] == 1.0


def test_normal_wait_still_counts_as_unfilled():
    c = _candidate()
    c.plan["max_entry_price"] = 90
    assert replay._entry_plan_fill(c, 1) == (None, None, "no_fill_in_window")
    result = replay.run_timing_experiment([c], fixed_policy="A", max_new=5)["entry_plan"]
    assert result["checker_errors"] == 0
    assert result["timing_candidates"] == 1
    assert result["evaluated_candidates"] == 1
    assert result["unfilled"] == 1
    assert result["fill_rate"] == 0.0
    assert result["unfilled_opportunity_cost_median_r"] is not None


@pytest.mark.parametrize("policy", ["B", "C"])
@pytest.mark.parametrize("observed", ["2026-08-03T10:01:00", "2026-08-03T10:01:00+09:00", "2026-08-03T01:01:00+00:00"])
def test_top_level_future_observation_excludes_report_despite_old_data_as_of(policy, observed):
    c = _candidate()
    c.evidence[0]["observed_at"] = observed
    assert replay.select_candidates([c], policy, 5) == []
    report = replay._to_analyst_report(c.evidence[0], now=datetime(2026, 8, 3, 10))
    assert report.ok is False
    assert report.observed_at == datetime.fromisoformat(observed)


@pytest.mark.parametrize("location", ["top", "nested"])
@pytest.mark.parametrize("observed", ["garbled", "", "   ", True, 123, [], {}])
def test_explicit_invalid_observation_excludes_the_whole_report(location, observed):
    c = _candidate()
    source = c.evidence[0] if location == "top" else c.evidence[0]["evidence"][0]
    source["observed_at"] = observed
    assert replay.gate_b(c) is False
    assert replay.gate_c(c) is False
    report = replay._to_analyst_report(c.evidence[0], now=datetime(2026, 8, 3, 10))
    assert report.ok is False
    if location == "nested":
        assert report.evidence[0].usable is False


@pytest.mark.parametrize("observed", [None, "2026-08-03T09:59:59", "2026-08-03T10:00:00+09:00", "2026-08-03T01:00:00+00:00"])
@pytest.mark.parametrize("decided", ["2026-08-03T10:00:00", "2026-08-03T01:00:00+00:00"])
def test_missing_or_nonfuture_observation_preserves_valid_report(observed, decided):
    c = _candidate()
    c.plan["decided_at"] = decided
    c.evidence[0]["observed_at"] = observed
    assert replay.gate_b(c) is True
    assert replay.gate_c(c) is True
    assert replay._assess(c, 1).merit_score == 30


@pytest.mark.parametrize("bad_observed", ["2026-08-03T10:01:00", "garbled"])
def test_bad_report_does_not_remove_separate_historical_report(bad_observed):
    c = _candidate()
    historical = deepcopy(c.evidence[0])
    c.evidence[0].update(score=100, observed_at=bad_observed)
    c.evidence.append(historical)
    assert replay.gate_b(c) is True
    assert replay.gate_c(c) is True
    assert replay._assess(c, 1).merit_score == 30
    assert replay._assess(c, 2).merit_score == 30


def test_absent_observation_key_preserves_legacy_report():
    c = _candidate()
    c.evidence[0].pop("observed_at")
    c.evidence[0]["evidence"][0].pop("observed_at")
    assert replay.gate_b(c) is True
