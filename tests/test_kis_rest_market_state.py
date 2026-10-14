"""REST 원문 근거의 제한과 과거 호가 연결 회귀."""

import copy
import hashlib
import itertools
import json

import pytest

from src.analytics import kis_rest_market_state as state

SYMBOL = "005930"
START = "2026-10-14T00:00:00+00:00"
END = "2026-10-14T00:00:02+00:00"
QUOTE = "2026-10-14T00:00:05+00:00"
VERIFIED = {"verified": True, "complete": True}


def output(**updates):
    return dict(stck_shrn_iscd=SYMBOL, iscd_stat_cls_code="00", temp_stop_yn="N",
                vi_cls_code="N", ovtm_vi_cls_code="N", **updates)


def snapshot(**updates):
    values = output()
    values.update(updates)
    return state.build_snapshot(SYMBOL, values, request_started_at=START,
                                response_available_at=END)


def record(value=None, observed_at="2026-10-14T00:00:03+00:00"):
    return {"candidate_id": "scan:" + SYMBOL, "observed_at": observed_at,
            "market_state_snapshot": snapshot() if value is None else value}


def associate(records, **kwargs):
    options = dict(symbol=SYMBOL, quote_at=QUOTE, max_age_seconds=10,
                   ledger_status=VERIFIED)
    options.update(kwargs)
    return state.associate_snapshot(records, **options)


def rehash(value):
    value["snapshot_id"] = hashlib.sha256(json.dumps(
        {k: v for k, v in value.items() if k != "snapshot_id"},
        sort_keys=True, separators=(",", ":"), ensure_ascii=True,
        allow_nan=False).encode()).hexdigest()
    return value


def test_preserves_only_qualified_flat_fields_and_never_promotes_state():
    values = output(secret={"not": "serialized"})
    result = state.build_snapshot(SYMBOL, values, request_started_at=START,
                                  response_available_at=END)
    assert set(result) == state.SNAPSHOT_FIELDS
    assert "secret" not in result
    assert result["raw_status"] == "raw_only"
    assert result["source_as_of"] is None
    assert result["source_sequence"] is None
    assert result["state_verdict"] == "UNKNOWN"
    assert result["snapshot_id"] == rehash(copy.copy(result))["snapshot_id"]
    checked = state.validate_snapshot(result, expected_symbol=SYMBOL)
    assert checked == result and checked is not result


@pytest.mark.parametrize("raw,status,stored", [
    (None, "fields_missing", None), ("", "fields_missing", ""),
    (" ", "fields_missing", " "), ("0", "raw_only", "0"),
    (0, "invalid_values", None), (False, "invalid_values", None),
    (float("nan"), "invalid_values", None), ({}, "invalid_values", None),
    ("x" * 17, "invalid_values", None), ("N\n", "invalid_values", None),
    ("N\x85", "invalid_values", None),
])
def test_missing_invalid_and_zero_are_distinguished(raw, status, stored):
    result = snapshot(vi_cls_code=raw)
    assert result["raw_status"] == status
    assert result["vi_cls_code"] == stored
    assert state.validate_snapshot(result, expected_symbol=SYMBOL) == result
    assert associate([record(result)])["raw_available"] is (status == "raw_only")


@pytest.mark.parametrize("code", ["00", "51", "58", "59", "unmapped"])
def test_codes_remain_uninterpreted(code):
    result = associate([record(snapshot(iscd_stat_cls_code=code))])
    assert result["raw_available"] is True
    assert result["state_verdict"] == "UNKNOWN"
    assert result["usable_for_market_state"] is False
    assert result["request_duration_seconds"] == 2.0
    assert result["age_upper_bound_seconds"] == 5.0
    assert len(result["limitations"]) >= 3


@pytest.mark.parametrize("field,value", [
    ("tr_id", "OTHER"), ("market_code", "NX"), ("schema_version", "v0"),
    ("source_sha256", "0" * 64), ("source_as_of", END),
    ("source_sequence", 1), ("state_verdict", "NORMAL"),
    ("clock_basis", "exchange_time"), ("symbol", "000660"),
    ("request_started_at", "2026-10-14T00:00:04+00:00"),
    ("response_available_at", "2026-10-14T00:00:02"),
    ("vi_cls_code", float("nan")), ("raw_status", "invalid_values"),
])
def test_validator_rejects_tampering_even_with_recomputed_hash(field, value):
    changed = snapshot()
    changed[field] = value
    if not isinstance(value, float):
        rehash(changed)
    with pytest.raises(ValueError):
        state.validate_snapshot(changed, expected_symbol=SYMBOL)


def test_validator_rejects_extra_missing_keys_and_changed_digest():
    for mutate in (lambda x: x.update(extra="x"), lambda x: x.pop("vi_cls_code"),
                   lambda x: x.update(vi_cls_code="Y")):
        changed = snapshot()
        mutate(changed)
        with pytest.raises(ValueError):
            state.validate_snapshot(changed, expected_symbol=SYMBOL)


@pytest.mark.parametrize("symbol", ["5930", " 005930", "１２３４５６", 5930, None])
def test_request_symbol_must_be_normalized(symbol):
    with pytest.raises(ValueError):
        state.build_snapshot(symbol, output(), request_started_at=START,
                             response_available_at=END)


@pytest.mark.parametrize("start,end", [(END, START), ("bad", END),
    (START, "2026-10-14T00:00:02"), (START, float("nan"))])
def test_build_rejects_unknown_or_reversed_clock(start, end):
    with pytest.raises(ValueError):
        state.build_snapshot(SYMBOL, output(), request_started_at=start,
                             response_available_at=end)


@pytest.mark.parametrize("response_symbol", [None, "", "000660"])
def test_response_symbol_is_preserved_but_must_match_for_association(response_symbol):
    result = snapshot(stck_shrn_iscd=response_symbol)
    assert state.validate_snapshot(result, expected_symbol=SYMBOL) == result
    associated = associate([record(result)])
    assert associated["raw_available"] is False
    assert associated["snapshot"]["response_symbol"] == response_symbol


@pytest.mark.parametrize("ledger", [None, {}, {"verified": True},
    {"verified": 1, "complete": True}, {"verified": True, "complete": False}])
def test_unverified_or_incomplete_ledger_never_supplies_raw(ledger):
    assert associate([record()], ledger_status=ledger)["raw_available"] is False


def test_latest_missing_or_invalid_record_blocks_older_value_in_all_orders():
    old = record(observed_at=END)
    missing = {"candidate_id": "next:" + SYMBOL, "observed_at": QUOTE}
    invalid = record({"vi_cls_code": "N"}, observed_at=QUOTE)
    for newest in (missing, invalid, record(snapshot(vi_cls_code=""), QUOTE)):
        for records in itertools.permutations([old, newest]):
            assert associate(list(records))["raw_available"] is False


def test_future_record_cannot_backfill_old_quote():
    future = record(observed_at="2026-10-14T00:00:06+00:00")
    assert associate([future])["raw_available"] is False
    assert associate([future, record()])["raw_available"] is True
    invalid = record(observed_at="2026-10-14T00:00:01+00:00")
    assert associate([invalid])["raw_available"] is False


def test_age_counts_full_logical_call_and_accepts_exact_boundary():
    assert associate([record()], max_age_seconds=5)["raw_available"] is True
    result = associate([record()], max_age_seconds=4)
    assert result["raw_available"] is False
    assert result["age_upper_bound_seconds"] == 5.0
    assert result["snapshot"] is not None


@pytest.mark.parametrize("limit", [-1, float("nan"), float("inf"), True, "5", None])
def test_invalid_freshness_bound_fails_closed(limit):
    assert associate([record()], max_age_seconds=limit)["raw_available"] is False


def test_ties_clock_inversion_and_corrupt_latest_are_permutation_invariant():
    later_response = state.build_snapshot(SYMBOL, output(), request_started_at=START,
                                         response_available_at="2026-10-14T00:00:04+00:00")
    cases = [
        [record(), record(snapshot(vi_cls_code="Y"))],
        [record(), record(observed_at="2026-10-14T00:00:04+00:00")],
        [record(later_response, "2026-10-14T00:00:04+00:00"), record(snapshot(), QUOTE)],
        [record(), record(observed_at=float("nan"))],
    ]
    for records in cases:
        results = [associate(list(order)) for order in itertools.permutations(records)]
        assert all(not result["raw_available"] for result in results)
        assert results[0] == results[1]


def test_other_symbol_cannot_contaminate_and_outputs_do_not_alias():
    original = record()
    other = {"candidate_id": "scan:000660", "observed_at": "bad"}
    result = associate([other, original])
    assert result["raw_available"] is True
    result["snapshot"]["vi_cls_code"] = "changed"
    assert original["market_state_snapshot"]["vi_cls_code"] == "N"
    json.dumps(result, allow_nan=False)
