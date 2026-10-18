"""기존 KRX REST 응답의 제한된 원문 보존과 호가 시점 연결.

공식 문서의 필드 존재만 사용하며 상태 코드의 의미는 판정하지 않는다.
호출 구간에는 리미터 대기/재시도가 포함되고 HTTP 전송 또는 거래소 시각이 아니다.
지문은 원문 출처 인증이 아닌 저장 무결성 보조다. REST 자료만으로 정상 거래,
체결 가능성, WS 연결 세대의 연속성 또는 순수익을 인증할 수 없다.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from datetime import datetime, timedelta

SCHEMA_VERSION = "kis-rest-market-state-raw-v1"
SOURCE_SHA256 = "cb8da9262a405b865f2f3d61261f76bfbd16c053a4cf72e1abe47108940fbbe4"
SOURCE_URL = (
    "https://apiportal.koreainvestment.com/api/apis/public/detail?accessUrl="
    "%2Fuapi%2Fdomestic-stock%2Fv1%2Fquotations%2Finquire-price"
)
_RAW_FIELDS = (
    "response_symbol", "iscd_stat_cls_code", "temp_stop_yn", "vi_cls_code",
    "ovtm_vi_cls_code",
)
_CONSTANTS = {
    "schema_version": SCHEMA_VERSION,
    "tr_id": "FHKST01010100",
    "market_code": "J",
    "source_as_of": None,
    "source_sequence": None,
    "clock_basis": "logical_call_including_retries",
    "source_sha256": SOURCE_SHA256,
    "state_verdict": "UNKNOWN",
}
SNAPSHOT_FIELDS = frozenset(_CONSTANTS) | frozenset(_RAW_FIELDS) | frozenset({
    "symbol", "request_started_at", "response_available_at", "raw_status",
    "snapshot_id",
})
_LIMITATIONS = (
    "rest_field_semantics_unqualified",
    "exchange_as_of_and_sequence_unavailable",
    "websocket_connection_generation_unbound",
)


def _symbol(value: object) -> str:
    if type(value) is not str or re.fullmatch(r"[0-9]{6}", value) is None:
        raise ValueError("normalized six-digit symbol required")
    return value


def _time(value: object) -> datetime:
    if (type(value) is not str or len(value) > 40
            or re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,6})?(?:Z|\+00:00)", value) is None):
        raise ValueError("bounded UTC-aware timestamp required")
    try:
        result = datetime.fromisoformat(value)
    except ValueError:
        raise ValueError("invalid UTC timestamp") from None
    if result.utcoffset() != timedelta(0):
        raise ValueError("UTC timestamp required")
    return result


def _raw_valid(value: object) -> bool:
    return type(value) is str and len(value) <= 16 and all(
        not unicodedata.category(char).startswith("C") for char in value
    )


def _missing(value: str | None) -> bool:
    return value is None or value.strip() == ""


def _digest(snapshot: dict) -> str:
    return hashlib.sha256(json.dumps(
        {key: value for key, value in snapshot.items() if key != "snapshot_id"},
        sort_keys=True, separators=(",", ":"), ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")).hexdigest()


def build_snapshot(symbol: str, output: dict, *, request_started_at: str,
                   response_available_at: str) -> dict:
    """허용한 다섯 원문 값만 복사한다. 잘못된 원문은 None과 상태로 남긴다."""
    _symbol(symbol)
    if _time(request_started_at) > _time(response_available_at):
        raise ValueError("logical call clock reversed")
    if type(output) is not dict:
        raise ValueError("response output must be a dict")
    snapshot = dict(_CONSTANTS, symbol=symbol, request_started_at=request_started_at,
                    response_available_at=response_available_at)
    invalid = False
    for field in _RAW_FIELDS:
        value = output.get("stck_shrn_iscd" if field == "response_symbol" else field)
        if value is not None and not _raw_valid(value):
            value = None
            invalid = True
        snapshot[field] = value
    snapshot["raw_status"] = (
        "invalid_values" if invalid else
        "fields_missing" if any(_missing(snapshot[key]) for key in _RAW_FIELDS) else
        "raw_only"
    )
    snapshot["snapshot_id"] = _digest(snapshot)
    return snapshot


def validate_snapshot(snapshot: dict, *, expected_symbol: str) -> dict:
    """정확한 키/상수/UTC 인과/유한 원문/지문을 검증하고 독립 사본을 반환한다."""
    _symbol(expected_symbol)
    if type(snapshot) is not dict or snapshot.keys() != SNAPSHOT_FIELDS:
        raise ValueError("unexpected snapshot fields")
    for key, value in _CONSTANTS.items():
        if type(snapshot[key]) is not type(value) or snapshot[key] != value:
            raise ValueError("snapshot constants mismatch")
    if _symbol(snapshot["symbol"]) != expected_symbol:
        raise ValueError("requested symbol mismatch")
    if _time(snapshot["request_started_at"]) > _time(snapshot["response_available_at"]):
        raise ValueError("logical call clock reversed")
    for field in _RAW_FIELDS:
        if snapshot[field] is not None and not _raw_valid(snapshot[field]):
            raise ValueError("invalid bounded raw value")
    status = snapshot["raw_status"]
    missing = any(_missing(snapshot[key]) for key in _RAW_FIELDS)
    if type(status) is not str or status not in {"raw_only", "fields_missing", "invalid_values"}:
        raise ValueError("invalid raw status")
    if ((status == "raw_only" and missing)
            or (status == "fields_missing" and not missing)
            or (status == "invalid_values" and not any(snapshot[key] is None for key in _RAW_FIELDS))):
        raise ValueError("inconsistent raw status")
    if type(snapshot["snapshot_id"]) is not str or snapshot["snapshot_id"] != _digest(snapshot):
        raise ValueError("snapshot digest mismatch")
    return dict(snapshot)


def associate_snapshot(rest_records: list[dict], *, symbol: str, quote_at: str,
                       max_age_seconds: float, ledger_status: dict | None) -> dict:
    """검증된 원장에서 호가 이전의 최신 종목별 기록을 연결한다.

    최신 순서는 observed_at이며, 보존된 response_available_at 순서도 엄격히
    증가해야 한다. 입력 목록 순서를 신뢰하지 않는다. 동률(중복 포함), 두 시계의
    역전, 손상 시각은 UNKNOWN이다. 응답 <= 발행 <= 호가 경계는 허용한다.
    최신 행의 증거 결측/손상 때 이전 값을 사용하지 않는다. 호가 이후 발행한
    행은 당시 사용할 수 없었던 자료라 제외한다. 신선도 상한은 요청 시작부터다.
    raw_available은 이 조건을 만족하는 raw 자료이며 정상 시장 판정이 아니다.
    """
    result = {
        "schema_version": "kis-rest-market-state-association-v1",
        "raw_available": False,
        "state_verdict": "UNKNOWN",
        "usable_for_market_state": False,
        "reasons": [],
        "snapshot": None,
        "request_duration_seconds": None,
        "age_upper_bound_seconds": None,
        "limitations": list(_LIMITATIONS),
    }

    def fail(reason: str) -> dict:
        result["reasons"].append(reason)
        return result

    if (type(ledger_status) is not dict or ledger_status.get("verified") is not True
            or ledger_status.get("complete") is not True):
        return fail("ledger_unverified_or_incomplete")
    try:
        _symbol(symbol)
        quote_time = _time(quote_at)
    except ValueError:
        return fail("invalid_association_identity_or_time")
    if (type(max_age_seconds) not in (int, float) or max_age_seconds < 0):
        return fail("invalid_max_age_seconds")
    try:
        if not math.isfinite(max_age_seconds):
            return fail("invalid_max_age_seconds")
    except OverflowError:
        return fail("invalid_max_age_seconds")
    if type(rest_records) is not list:
        return fail("invalid_rest_records")
    candidates = []
    for record in rest_records:
        if type(record) is not dict:
            return fail("invalid_rest_records")
        candidate_id = record.get("candidate_id")
        if (type(candidate_id) is not str or ":" not in candidate_id
                or candidate_id.rsplit(":", 1)[1] != symbol):
            continue
        try:
            observed = _time(record.get("observed_at"))
        except ValueError:
            return fail("invalid_record_time")
        if observed <= quote_time:
            candidates.append((observed, record))
    if not candidates:
        return fail("no_record_before_quote")
    candidates.sort(key=lambda item: item[0])
    if any(left[0] == right[0] for left, right in zip(candidates, candidates[1:])):
        return fail("ambiguous_record_time")
    previous_response = None
    for observed, record in candidates:
        evidence = record.get("market_state_snapshot")
        if evidence is None:
            continue
        # 손상 시각은 최신 선정을 신뢰할 수 없게 하므로 종목 전체를 미확정으로 둔다.
        if type(evidence) is dict:
            try:
                response_time = _time(evidence.get("response_available_at"))
                request_time = _time(evidence.get("request_started_at"))
            except ValueError:
                return fail("invalid_snapshot_time")
            if request_time > response_time or response_time > observed:
                return fail("snapshot_clock_order_invalid")
            if previous_response is not None and response_time <= previous_response:
                return fail("ambiguous_response_order")
            previous_response = response_time
    latest = candidates[-1][1].get("market_state_snapshot")
    if latest is None:
        return fail("latest_snapshot_missing")
    try:
        checked = validate_snapshot(latest, expected_symbol=symbol)
    except ValueError:
        return fail("latest_snapshot_invalid")
    result["snapshot"] = checked
    requested = _time(checked["request_started_at"])
    available = _time(checked["response_available_at"])
    result["request_duration_seconds"] = (available - requested).total_seconds()
    result["age_upper_bound_seconds"] = (quote_time - requested).total_seconds()
    if checked["response_symbol"] != symbol:
        result["reasons"].append("response_symbol_missing_or_mismatch")
    if checked["raw_status"] != "raw_only":
        result["reasons"].append("latest_snapshot_" + checked["raw_status"])
    if result["age_upper_bound_seconds"] > max_age_seconds:
        result["reasons"].append("snapshot_stale")
    result["raw_available"] = not result["reasons"]
    return result
