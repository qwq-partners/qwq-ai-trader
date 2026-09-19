"""Two-index trend inputs: actual metadata, external HTTP/clock boundary fake only."""
import asyncio
from copy import deepcopy
from dataclasses import FrozenInstanceError, replace
from datetime import timedelta, timezone
import json

import pytest

from src.data.providers.kis_index_observation import build_index_observation
from test_execution_index_risk_input import NOW, BUSINESS_DAY, _raw


def normalize(quote, *, index_code="0001", now=NOW):
    from src.execution.safety.index_risk_input import normalize_index_trend
    return normalize_index_trend(quote, index_code=index_code, now=now, business_day=BUSINESS_DAY)


def quote(code="0001", *, received_at=NOW):
    observation = build_index_observation(_raw(), code, received_at=received_at)
    return {**{k: v["value"] for k, v in observation["fields"].items()},
            "_observation": observation, "untrusted": "not-preserved"}


@pytest.mark.parametrize("code", ["0001", "1001"])
def test_complete_original_fields_are_detached_but_never_market_freshness(code):
    original = quote(code)
    before = deepcopy(original)
    fact = normalize(original, index_code=code)
    assert fact.outcome == "success"
    assert fact.index_code == code and fact.market_as_of is None
    assert fact.source == "kis:FHPUP02100000:index" + code
    assert fact.source_event_id == original["_observation"]["observation_id"]
    assert fact.received_at == NOW
    values = dict(fact.values)
    assert values == {k: original[k] for k in ("price", "open", "high", "low", "change_pct")}
    assert json.loads(fact.observation_json) == original["_observation"]
    assert original == before and "not-preserved" not in fact.observation_json
    original["_observation"]["fields"]["open"]["value"] = 0
    values["open"] = 0
    assert dict(fact.values)["open"] == 3020.0
    with pytest.raises(FrozenInstanceError):
        fact.outcome = "missing"


@pytest.mark.parametrize("field,value", [
    ("values", []), ("values", (("price", True),)), ("source", "caller"),
    ("observation_json", "{}"), ("market_as_of", NOW),
])
def test_direct_frozen_dto_cannot_hide_mutability_or_conflicting_provenance(field, value):
    fact = normalize(quote())
    with pytest.raises(ValueError, match="trend"):
        replace(fact, **{field: value})


@pytest.mark.parametrize("field", ["price", "open", "high", "low", "change_pct"])
@pytest.mark.parametrize("status", ["missing", "invalid"])
def test_each_required_raw_field_missing_never_uses_numeric_fallback(field, status):
    row = quote()
    row[field] = 0.0
    row["_observation"]["fields"][field] = {"status": status, "value": None}
    fact = normalize(row)
    assert (fact.outcome, fact.values, fact.observation_json) == ("missing", (), "{}")


@pytest.mark.parametrize("field", ["price", "open", "high", "low", "change_pct"])
@pytest.mark.parametrize("value", [False, "1.0", float("nan"), float("inf"), 10**400],
                         ids=["bool", "text", "nan", "infinity", "huge_int"])
def test_outer_invalid_fields_do_not_throw_or_become_valid(field, value):
    row = quote()
    row[field] = value
    assert normalize(row).outcome == "missing"


def test_price_mismatch_and_wrong_index_are_not_accepted():
    row = quote("1001")
    assert normalize(row).outcome == "missing"
    row["price"] += 0.01
    assert normalize(row, index_code="1001").outcome == "missing"


@pytest.mark.parametrize("field", ["price", "open", "high", "low"])
def test_zero_or_negative_price_field_is_missing_not_recovery(field):
    for value in (0.0, -1.0):
        row = quote()
        row[field] = row["_observation"]["fields"][field]["value"] = value
        assert normalize(row).outcome == "missing"


def test_unused_change_amount_can_be_missing_and_verified_zero_change_pct_is_valid():
    row = quote()
    row["change"] = 0.0
    row["_observation"]["fields"]["change"] = {"status": "missing", "value": None}
    row["change_pct"] = row["_observation"]["fields"]["change_pct"]["value"] = 0.0
    assert normalize(row).outcome == "success"


@pytest.mark.parametrize("receipt", [NOW + timedelta(seconds=1), NOW - timedelta(days=1)])
def test_future_or_previous_business_day_receipt_is_not_current_input(receipt):
    assert normalize(quote(received_at=receipt)).outcome == "missing"


def test_utc_receipt_uses_kst_business_day_not_host_date():
    at = (NOW - timedelta(hours=9)).astimezone(timezone.utc)
    assert at.date().isoformat() != BUSINESS_DAY
    assert normalize(quote("1001", received_at=at), index_code="1001",
                     now=NOW.astimezone(timezone.utc)).outcome == "success"


@pytest.mark.parametrize("code", ["2001", "", None, False])
def test_unknown_index_contract_is_not_invented(code):
    with pytest.raises(ValueError, match="index"):
        normalize(quote(), index_code=code)


def test_real_adapter_two_indices_and_cache_keep_call_counts(monkeypatch):
    from src.data.providers.kis_market_data import KISMarketData
    from types import SimpleNamespace
    async def scenario():
        calls = {"get": [], "limiter": 0}
        class Response:
            status = 200
            async def __aenter__(self): return self
            async def __aexit__(self, *args): return False
            async def json(self): return {"rt_cd": "0", "output": _raw()}
        class Session:
            closed = False
            def get(self, url, *, headers, params):
                assert headers["tr_id"] == "FHPUP02100000"
                calls["get"].append(params["FID_INPUT_ISCD"])
                return Response()
        async def headers(tr): return {"tr_id": tr}
        async def acquire(): calls["limiter"] += 1
        md = KISMarketData(token_manager=SimpleNamespace(base_url="https://synthetic.invalid"))
        md._session = Session()
        monkeypatch.setattr(md, "_get_headers", headers)
        monkeypatch.setattr(md, "_index_clock", lambda: NOW, raising=False)
        monkeypatch.setattr("src.data.providers.kis_market_data.kis_rate_limit.acquire", acquire)
        originals = await asyncio.gather(*(md.fetch_index_price(code) for code in ("0001", "1001")))
        facts = [normalize(row, index_code=code) for code, row in zip(("0001", "1001"), originals)]
        assert all(f.outcome == "success" and f.market_as_of is None for f in facts)
        monkeypatch.setattr(md, "_index_clock", lambda: NOW + timedelta(seconds=1))
        again = await asyncio.gather(*(md.fetch_index_price(code) for code in ("0001", "1001")))
        assert [row["_observation"] for row in originals] == [row["_observation"] for row in again]
        assert calls == {"get": ["0001", "1001"], "limiter": 2}
    asyncio.run(scenario())
