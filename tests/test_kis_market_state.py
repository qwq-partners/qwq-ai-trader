"""Pure, bounded parsing of KIS market-state evidence."""

import pytest

from src.analytics.kis_market_state import parse_market_state


CNT0_SHA = "833b105418e98807d3e24753f97ed008e0f5f6972a0937a1a0f40d252543624d"
MKO0_SHA = "4c76c54995ff112312ecf9e98f4f194d6edfe2f0749300b8639c10ff0d40c3f5"

UNION_FIELDS = {
    "business_date", "exchange_time", "new_mkop_cls_code", "trht_yn",
    "hour_cls_code", "market_cls_code", "mkop_cls_code",
    "antc_mkop_cls_code", "mrkt_trtm_cls_code", "divi_app_cls_code",
    "iscd_stat_cls_code", "vi_cls_code", "ovtm_vi_cls_code", "exch_cls_code",
}


def fields(length, updates):
    values = ["unused"] * length
    for index, value in updates.items():
        values[index] = value
    return values


def test_cnt0_exact_layout_preserves_only_documented_raw_fields():
    """Catches an off-by-one index or invented normal-market verdict."""
    result = parse_market_state("H0STCNT0", fields(47, {
        1: "091530", 33: "20261013", 34: "20", 35: "Y", 43: "C", 46: "1",
    }))

    assert result == {
        "schema_version": "kis-market-state-raw-v1",
        "tr_id": "H0STCNT0",
        "raw_field_count": 47,
        "market_state_schema": "kis-public-guide-20261012",
        "market_state_source_sha256": CNT0_SHA,
        "market_state_status": "raw_only",
        "source_sequence": None,
        "state_verdict": "UNKNOWN",
        "business_date": "20261013",
        "exchange_time": "091530",
        "new_mkop_cls_code": "20",
        "trht_yn": "Y",
        "hour_cls_code": "C",
        "market_cls_code": "1",
        "mkop_cls_code": None,
        "antc_mkop_cls_code": None,
        "mrkt_trtm_cls_code": None,
        "divi_app_cls_code": None,
        "iscd_stat_cls_code": None,
        "vi_cls_code": None,
        "ovtm_vi_cls_code": None,
        "exch_cls_code": None,
    }


def test_mko0_exact_layout_preserves_its_raw_codes_without_normal_assertion():
    """Catches adding semantic classification to a raw-evidence parser."""
    result = parse_market_state("H0STMKO0", fields(11, {
        1: "N", 3: "112", 4: "121", 5: "A", 6: "D", 7: "58", 8: "V", 9: "O", 10: "K",
    }))

    assert result["market_state_status"] == "raw_only"
    assert result["market_state_source_sha256"] == MKO0_SHA
    assert result["state_verdict"] == "UNKNOWN"
    assert result["business_date"] is None
    assert result["exchange_time"] is None
    assert {key: result[key] for key in UNION_FIELDS} == {
        "business_date": None, "exchange_time": None, "new_mkop_cls_code": None,
        "trht_yn": "N", "hour_cls_code": None, "market_cls_code": None,
        "mkop_cls_code": "112", "antc_mkop_cls_code": "121",
        "mrkt_trtm_cls_code": "A", "divi_app_cls_code": "D",
        "iscd_stat_cls_code": "58", "vi_cls_code": "V", "ovtm_vi_cls_code": "O", "exch_cls_code": "K",
    }


@pytest.mark.parametrize(("tr_id", "length", "source_sha"), [
    ("H0STCNT0", 46, CNT0_SHA),
    ("H0STCNT0", 48, CNT0_SHA),
    ("H0STMKO0", 10, MKO0_SHA),
])
def test_unsupported_layout_never_guesses_dates_or_codes(tr_id, length, source_sha):
    """Catches accepting a historical width and assigning unverified offsets."""
    result = parse_market_state(tr_id, ["20261013"] * length)

    assert result["raw_field_count"] == length
    assert result["market_state_source_sha256"] == source_sha
    assert result["market_state_status"] == "unsupported_layout"
    assert result["state_verdict"] == "UNKNOWN"
    assert {key: result[key] for key in UNION_FIELDS} == {key: None for key in UNION_FIELDS}


def test_unknown_transaction_returns_none():
    """Catches parsing fields from a transaction without a qualified guide."""
    assert parse_market_state("H0NXCNT0", ["x"] * 47) is None


@pytest.mark.parametrize("bad_value", ["x" * 17, "bad\x00code", 7])
def test_invalid_selected_value_is_bounded_and_not_preserved(bad_value):
    """Catches retaining oversized, control, or non-string selected fields."""
    result = parse_market_state("H0STCNT0", fields(47, {1: bad_value}))

    assert result["market_state_status"] == "invalid_values"
    assert result["raw_field_count"] == 47
    assert result["market_state_source_sha256"] == CNT0_SHA
    assert {key: result[key] for key in UNION_FIELDS} == {key: None for key in UNION_FIELDS}
