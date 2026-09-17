"""Pure KIS index provenance normalization; no owner, adapter I/O, or policy publication."""

from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone
import json
from zoneinfo import ZoneInfo

import pytest

from src.data.providers.kis_index_observation import build_index_observation
from src.execution.safety.index_risk_input import normalize_index_risk


KST = ZoneInfo("Asia/Seoul")
NOW = datetime(2026, 9, 18, 10, 0, tzinfo=KST)
BUSINESS_DAY = "2026-09-18"


def _raw(*, change_pct="-1.5"):
    return {
        "bstp_nmix_prpr": "3000.12", "bstp_nmix_oprc": "3020.0",
        "bstp_nmix_hgpr": "3040.0", "bstp_nmix_lwpr": "2980.0",
        "bstp_nmix_prdy_vrss": "-45.0", "bstp_nmix_prdy_ctrt": change_pct,
    }


def _quote(*, change_pct=-1.5, received_at=None):
    observation = build_index_observation(
        _raw(change_pct=str(change_pct)), "0001", received_at=received_at or NOW - timedelta(seconds=1),
    )
    return {"change_pct": change_pct, "_observation": observation, "untrusted": "do-not-store"}


def test_actual_producer_metadata_becomes_a_frozen_canonical_success_input():
    quote = _quote(change_pct=-1.5)

    result = normalize_index_risk(quote, now=NOW, business_day=BUSINESS_DAY)

    assert result.outcome == "success"
    assert result.change_pct == -1.5
    assert result.level == "caution"
    assert result.source == "kis:FHPUP02100000:index0001"
    assert result.source_event_id == quote["_observation"]["observation_id"]
    assert result.received_at == NOW - timedelta(seconds=1)
    assert result.market_as_of is None
    assert json.loads(result.observation_json) == quote["_observation"]
    assert "do-not-store" not in result.observation_json
    with pytest.raises(FrozenInstanceError):
        result.level = "normal"


@pytest.mark.parametrize("field", ["price", "open", "high", "low", "change"])
def test_change_percent_can_succeed_when_other_index_fields_are_missing(field):
    quote = _quote()
    quote["_observation"]["fields"][field] = {"status": "missing", "value": None}

    result = normalize_index_risk(quote, now=NOW, business_day=BUSINESS_DAY)

    assert result.outcome == "success"
    assert result.change_pct == -1.5


def test_verified_raw_zero_is_a_normal_observation_not_legacy_missing_zero():
    quote = _quote(change_pct=0.0)

    result = normalize_index_risk(quote, now=NOW, business_day=BUSINESS_DAY)

    assert (result.outcome, result.change_pct, result.level) == ("success", 0.0, "normal")


@pytest.mark.parametrize("status,value", [
    ("missing", None), ("invalid", None), ("valid", False), ("valid", float("nan")),
])
def test_missing_or_invalid_raw_percent_never_promotes_legacy_outer_zero_to_normal(status, value):
    quote = _quote(change_pct=0.0)
    quote["_observation"]["fields"]["change_pct"] = {"status": status, "value": value}

    result = normalize_index_risk(quote, now=NOW, business_day=BUSINESS_DAY)

    assert (result.outcome, result.change_pct, result.level) == ("missing", None, None)


@pytest.mark.parametrize("outer", [True, float("nan"), float("inf"), "-1.5", -1.49])
def test_outer_percent_must_be_finite_numeric_and_equal_to_metadata(outer):
    result = normalize_index_risk(
        {**_quote(), "change_pct": outer}, now=NOW, business_day=BUSINESS_DAY,
    )

    assert (result.outcome, result.change_pct, result.level) == ("missing", None, None)


@pytest.mark.parametrize("mutation", [
    lambda observation: observation.update(schema_version=True),
    lambda observation: observation.update(source="other"),
    lambda observation: observation.update(source_tr="OTHER"),
    lambda observation: observation.update(index_code="1001"),
    lambda observation: observation.update(observation_id="not-a-uuid"),
    lambda observation: observation.update(market_as_of="2026-09-18T10:00:00+09:00"),
    lambda observation: observation.update(received_at=(NOW + timedelta(seconds=1)).isoformat()),
    lambda observation: observation.update(received_at=(NOW - timedelta(days=1)).isoformat()),
])
def test_untrusted_or_unproven_producer_metadata_is_missing(mutation):
    quote = _quote()
    mutation(quote["_observation"])

    result = normalize_index_risk(quote, now=NOW, business_day=BUSINESS_DAY)

    assert (result.outcome, result.change_pct, result.level) == ("missing", None, None)
    assert result.observation_json == "{}"


def test_utc_now_and_receipt_are_checked_against_the_kst_business_day():
    now = datetime(2026, 9, 18, 1, 0, tzinfo=timezone.utc)
    quote = _quote(received_at=datetime(2026, 9, 17, 16, 0, tzinfo=timezone.utc))

    result = normalize_index_risk(quote, now=now, business_day=BUSINESS_DAY)

    assert result.outcome == "success"
    assert result.received_at == datetime(2026, 9, 17, 16, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize("field", ["change_pct", "high"])
@pytest.mark.parametrize("status", [[], {}, ["valid"]])
def test_json_serializable_collection_status_is_missing_not_an_exception(field, status):
    quote = _quote()
    quote["_observation"]["fields"][field]["status"] = status

    result = normalize_index_risk(quote, now=NOW, business_day=BUSINESS_DAY)

    assert (result.outcome, result.observation_json) == ("missing", "{}")


@pytest.mark.parametrize("location", ["outer", "change_pct", "high"])
@pytest.mark.parametrize("number", [10**400, -(10**400)])
def test_json_integer_outside_float_range_is_missing_not_an_exception(location, number):
    quote = _quote()
    if location == "outer":
        quote["change_pct"] = number
    else:
        quote["_observation"]["fields"][location]["value"] = number

    result = normalize_index_risk(quote, now=NOW, business_day=BUSINESS_DAY)

    assert (result.outcome, result.observation_json) == ("missing", "{}")


@pytest.mark.parametrize("receipt", [
    "0001-01-01T00:00:00+14:00", "9999-12-31T23:59:59-12:00",
])
def test_extreme_canonical_receipt_is_missing_not_an_exception(receipt):
    quote = _quote()
    quote["_observation"]["received_at"] = receipt

    result = normalize_index_risk(quote, now=NOW, business_day=BUSINESS_DAY)

    assert result.outcome == "missing"


@pytest.mark.parametrize("business_day", ["2026-9-18", "2026-09-31", 20260918])
def test_business_day_must_be_an_exact_iso_date(business_day):
    with pytest.raises(ValueError):
        normalize_index_risk(_quote(), now=NOW, business_day=business_day)
