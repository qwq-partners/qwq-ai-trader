"""Malformed public morning date DTOs fail with the documented ValueError."""
import pytest

from src.execution.safety.regime_morning import RegimeMorningBaseline


@pytest.mark.parametrize("field", ["business_day", "assessment_day"])
def test_non_string_morning_date_is_a_domain_validation_error(field):
    value = {
        "schema": 1, "baseline_id": "known", "account_scope": "scope",
        "business_day": "2026-09-18", "generation": 0, "fence_id": None,
        "evidence": {"source": "synthetic", "event_id": "known",
                     "observed_at": "2026-09-18T08:48:00+09:00"},
        "regime_baseline_version": 1, "horizon_baseline_version": 2,
        "morning": {"knowledge": "known", "assessment": None, "assessment_day": None,
                    "open_expectation": None, "open_expectation_as_of": None},
    }
    if field == "business_day":
        value["business_day"] = 7
    else:
        value["morning"].update(assessment="known", assessment_day=7)
    with pytest.raises(ValueError, match="morning_day"):
        RegimeMorningBaseline.from_dict(value)
