"""Bounded preservation of qualified KIS market-state fields.

This module deliberately records only documented raw fields.  It does not
infer a market session, trading availability, or a date from the receive time.
"""

from __future__ import annotations


_SCHEMA_VERSION = "kis-market-state-raw-v1"
_GUIDE_SCHEMA = "kis-public-guide-20261012"
_UNKNOWN = "UNKNOWN"

_TR_SPECS = {
    "H0STCNT0": {
        "width": 47,
        "source_sha256": "833b105418e98807d3e24753f97ed008e0f5f6972a0937a1a0f40d252543624d",
        "indexes": {
            "business_date": 33,
            "exchange_time": 1,
            "new_mkop_cls_code": 34,
            "trht_yn": 35,
            "hour_cls_code": 43,
            "market_cls_code": 46,
        },
    },
    "H0STMKO0": {
        "width": 11,
        "source_sha256": "4c76c54995ff112312ecf9e98f4f194d6edfe2f0749300b8639c10ff0d40c3f5",
        "indexes": {
            "trht_yn": 1,
            "mkop_cls_code": 3,
            "antc_mkop_cls_code": 4,
            "mrkt_trtm_cls_code": 5,
            "divi_app_cls_code": 6,
            "iscd_stat_cls_code": 7,
            "vi_cls_code": 8,
            "ovtm_vi_cls_code": 9,
            "exch_cls_code": 10,
        },
    },
}

_RAW_FIELDS = (
    "business_date",
    "exchange_time",
    "new_mkop_cls_code",
    "trht_yn",
    "hour_cls_code",
    "market_cls_code",
    "mkop_cls_code",
    "antc_mkop_cls_code",
    "mrkt_trtm_cls_code",
    "divi_app_cls_code",
    "iscd_stat_cls_code",
    "vi_cls_code",
    "ovtm_vi_cls_code",
    "exch_cls_code",
)


def _is_bounded_raw_value(value: object) -> bool:
    """Accept short strings without ASCII control characters only."""
    return (
        isinstance(value, str)
        and len(value) <= 16
        and not any(ord(char) < 32 or ord(char) == 127 for char in value)
    )


def parse_market_state(tr_id: str, fields: list[str]) -> dict | None:
    """Return qualified raw market-state evidence, or ``None`` for other TRs."""
    spec = _TR_SPECS.get(tr_id)
    if spec is None:
        return None

    result = {
        "schema_version": _SCHEMA_VERSION,
        "tr_id": tr_id,
        "raw_field_count": len(fields),
        "market_state_schema": _GUIDE_SCHEMA,
        "market_state_source_sha256": spec["source_sha256"],
        "market_state_status": "unsupported_layout",
        "source_sequence": None,
        "state_verdict": _UNKNOWN,
        **{name: None for name in _RAW_FIELDS},
    }
    if len(fields) != spec["width"]:
        return result

    selected = {name: fields[index] for name, index in spec["indexes"].items()}
    if not all(_is_bounded_raw_value(value) for value in selected.values()):
        result["market_state_status"] = "invalid_values"
        return result

    result.update(selected)
    result["market_state_status"] = "raw_only"
    return result
