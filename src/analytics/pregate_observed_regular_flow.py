"""Bounded offline regular-flow readout layered on the immutable pregate report."""
from __future__ import annotations

from copy import deepcopy
from datetime import timedelta, timezone
from decimal import Decimal, DecimalException
import math

from .entry_evaluation_bundle import load_json_bytes
from .entry_price_shadow import _timestamp
from .pregate_candidate_readout import build_pregate_candidate_report


POLICY_VERSION = "pregate-observed-regular-flow-v1"
BOOK_TR = "H0STASP0"
CNT_TR = "H0STCNT0"
MKO_TR = "H0STMKO0"
BOOK_SHA = "a22543dd4a1c64c01210eef281203771da0d1d58e2e7d4c08643c35a3fc60ff3"
CNT_SHA = "833b105418e98807d3e24753f97ed008e0f5f6972a0937a1a0f40d252543624d"
MKO_SHA = "4c76c54995ff112312ecf9e98f4f194d6edfe2f0749300b8639c10ff0d40c3f5"
KST = timezone(timedelta(hours=9))
_POLICY_KEYS = {"version", "fixed_at", "preparation_seconds", "state_max_age_seconds",
                "horizon_seconds", "max_markout_delay_seconds"}
_CNT_NOT_NORMAL = {
    "new_mkop_cls_code": {a + b for a in "123478" for b in "012378"} - {"20"},
    "trht_yn": {"Y"}, "hour_cls_code": {"A", "B", "C", "D"}, "market_cls_code": {"1", "3", "5"},
}
_MKO_NOT_NORMAL = {"110", "121", "129", "130", "139", "140", "146", "149", "150", "156", "159",
                   "164", "174", "182", "184"}
def _compact_quote(row):
    if row is None:
        return None
    provenance = row.get("provenance") if isinstance(row.get("provenance"), dict) else {}
    return {"sequence": deepcopy(row.get("sequence")), "quote_id": deepcopy(row.get("quote_id")),
            "symbol": deepcopy(row.get("symbol")), "bid": deepcopy(row.get("bid")), "ask": deepcopy(row.get("ask")),
            "bid_size": deepcopy(row.get("bid_size")), "ask_size": deepcopy(row.get("ask_size")),
            "observed_at": deepcopy(row.get("observed_at")), "received_at": deepcopy(provenance.get("received_at")),
            "provenance": {key: deepcopy(provenance.get(key)) for key in
                           ("tr_id", "connection_id", "generation", "raw_field_count", "market_state_schema",
                            "market_state_source_sha256", "market_state_status", "exchange_time",
                            "hour_class_code", "market_class_code", "message_count", "source_as_of")}}


def _evidence(row):
    # State rows are already bounded by the sealed journal schema.  Preserve the
    # complete raw state record rather than claiming evidence while omitting an
    # accepted MKO field.
    return None if row is None else deepcopy(row)


def _time(row, field):
    return _timestamp(row.get(field), field)


def _quote_times(row):
    p = row.get("provenance")
    if not isinstance(p, dict):
        raise ValueError("quote provenance")
    return _timestamp(p.get("received_at"), "quote received_at"), _time(row, "observed_at")


def _six_digits(value):
    return (type(value) is str and len(value) == 6 and all("0" <= char <= "9" for char in value)
            and 0 <= int(value[:2]) <= 23 and 0 <= int(value[2:4]) <= 59 and 0 <= int(value[4:]) <= 59)


def _book_valid(row):
    try:
        received, observed = _quote_times(row)
        p = row.get("provenance")
        generation = p.get("generation")
        if (row.get("kind") != "ws_quote" or p.get("tr_id") != BOOK_TR or type(p.get("message_count")) is not int or p.get("message_count") != 1
                or p.get("source_as_of") is not None or type(p.get("raw_field_count")) is not int
                or p.get("raw_field_count") != 63 or p.get("market_state_schema") != "H0STASP0-63-v1"
                or p.get("market_state_source_sha256") != BOOK_SHA or p.get("market_state_status") != "raw_observed"
                or p.get("hour_class_code") != "0" or p.get("market_class_code") != "2"
                or not _six_digits(p.get("exchange_time")) or received > observed
                or type(p.get("connection_id")) is not str or not p.get("connection_id")
                or type(generation) is not int or generation < 1):
            return False
        values = []
        for key in ("bid", "ask"):
            if type(row.get(key)) is bool:
                return False
            value = Decimal(str(row.get(key)))
            if not value.is_finite() or value <= 0:
                return False
            values.append(value)
        return values[0] <= values[1] and all(type(row.get(key)) is int and row[key] > 0 for key in ("bid_size", "ask_size"))
    except (ValueError, TypeError, DecimalException):
        return False


def _policy(study):
    value = study.get("observed_regular_flow_policy") if isinstance(study, dict) else None
    if type(value) is not dict or set(value) != _POLICY_KEYS or value.get("version") != POLICY_VERSION:
        raise ValueError("exact observed regular-flow policy required")
    try:
        fixed_at = _timestamp(value["fixed_at"], "observed_regular_flow_policy.fixed_at")
        start = _timestamp(study["capture"]["start_at"], "capture.start_at")
        admission_end = _timestamp(study["capture"]["admission_end_at"], "capture.admission_end_at")
        end = _timestamp(study["capture"]["end_at"], "capture.end_at")
    except (KeyError, ValueError, TypeError) as exc:
        raise ValueError("observed regular-flow policy chronology required") from exc
    expected = {"preparation_seconds": 5, "state_max_age_seconds": 5, "horizon_seconds": 900,
                "max_markout_delay_seconds": 5}
    if fixed_at > start or any(type(value.get(key)) is not int or value[key] != wanted for key, wanted in expected.items()):
        raise ValueError("fixed observed regular-flow policy required")
    if end - admission_end < timedelta(seconds=sum((5, 900, 5))):
        raise ValueError("capture requires regular-flow maturity tail")
    return value, start, end


def _state_times(row):
    return _time(row, "received_at"), _time(row, "observed_at")


def _state_before(row, quote):
    try:
        received, observed = _state_times(row)
        quote_received, _ = _quote_times(quote)
        return received <= observed and received < quote_received and observed < quote_received
    except ValueError:
        return False


def _state_schema_valid(row, tr, capture_day):
    if row.get("kind") != "market_state" or row.get("tr_id") != tr:
        return False
    sha, width = (CNT_SHA, 47) if tr == CNT_TR else (MKO_SHA, 11)
    if (row.get("schema_version") != "kis-market-state-raw-v1" or row.get("market_state_schema") != "kis-public-guide-20261012"
            or row.get("market_state_source_sha256") != sha or row.get("market_state_status") != "raw_only"
            or type(row.get("raw_field_count")) is not int or row.get("raw_field_count") != width):
        return False
    if (type(row.get("connection_id")) is not str or not row.get("connection_id")
            or type(row.get("generation")) is not int or row.get("generation") < 1):
        return False
    if tr == CNT_TR:
        return (row.get("business_date") == capture_day and _six_digits(row.get("exchange_time"))
                and all(type(row.get(k)) is str for k in ("new_mkop_cls_code", "trht_yn", "hour_cls_code", "market_cls_code")))
    return all(type(row.get(k)) is str for k in ("mkop_cls_code", "vi_cls_code", "iscd_stat_cls_code"))


def _cnt_kind(row, capture_day):
    if not _state_schema_valid(row, CNT_TR, capture_day):
        return "unknown"
    values = {key: row.get(key) for key in ("new_mkop_cls_code", "trht_yn", "hour_cls_code", "market_cls_code")}
    if values == {"new_mkop_cls_code": "20", "trht_yn": "N", "hour_cls_code": "0", "market_cls_code": "2"}:
        return "normal"
    if any(values[key] in _CNT_NOT_NORMAL[key] for key in values):
        return "contrary"
    return "unknown"


def _mko_kind(row, capture_day):
    if not _state_schema_valid(row, MKO_TR, capture_day):
        return "unknown"
    if row.get("mkop_cls_code") in _MKO_NOT_NORMAL or row.get("vi_cls_code") == "Y" or row.get("iscd_stat_cls_code") == "58" or row.get("trht_yn") == "Y":
        return "contrary"
    if row.get("mkop_cls_code") == "112" and row.get("vi_cls_code") == "N" and row.get("iscd_stat_cls_code") in {"00", "55", "57"}:
        return "neutral"
    return "unknown"


def _state_rows(records, *, tr, symbol, quote):
    return [row for row in records if row.get("kind") == "market_state" and row.get("tr_id") == tr
            and row.get("symbol") == symbol and type(row.get("sequence")) is int
            and row["sequence"] < quote.get("sequence", 0)]


def _latest(rows):
    """Use causal receive ordering; an untimed or conflicting latest state is unknown."""
    if not rows:
        return None, None
    parsed = []
    for row in rows:
        try:
            received, observed = _state_times(row)
        except ValueError:
            return max(rows, key=lambda item: item["sequence"]), "untimed"
        key = (received,
               row.get("local_frame_index") if type(row.get("local_frame_index")) is int else -1,
               row.get("record_index") if type(row.get("record_index")) is int else -1)
        parsed.append((key, observed, row))
    top_key = max(item[0] for item in parsed)
    top = [item for item in parsed if item[0] == top_key]
    first = top[0][2]
    if any(item[2] != first for item in top[1:]):
        return first, "conflicting"
    return first, None


def _state_context(records, quote, capture_day, policy):
    base = {"flow_context": "unknown", "vi_state": "unknown", "tradability": "not_evaluated",
            "fillability": "not_evaluated", "reasons": [], "raw_cnt_evidence": None,
            "raw_mko_evidence": None, "raw_mko_count": 0}
    if quote is None or type(quote.get("sequence")) is not int or type(quote.get("symbol")) is not str:
        base["reasons"].append("book_unavailable_or_invalid")
        return base
    cnt_rows = _state_rows(records, tr=CNT_TR, symbol=quote["symbol"], quote=quote)
    mko_rows = _state_rows(records, tr=MKO_TR, symbol=quote["symbol"], quote=quote)
    cnt, cnt_issue = _latest(cnt_rows)
    mko, mko_issue = _latest(mko_rows)
    base["raw_cnt_evidence"], base["raw_mko_evidence"], base["raw_mko_count"] = _evidence(cnt), _evidence(mko), len(mko_rows)
    if not _book_valid(quote):
        base["reasons"].append("book_unavailable_or_invalid")
        return base
    p = quote["provenance"]
    connection, generation = p.get("connection_id"), p.get("generation")
    if type(connection) is not str or not connection or type(generation) is not int or type(generation) is bool:
        base["reasons"].append("book_connection_or_generation_invalid")
        return base
    cnt_kind = "unknown"
    if cnt is None:
        base["reasons"].append("missing_prior_cnt")
    elif cnt_issue:
        base["reasons"].append("latest_cnt_" + cnt_issue)
    elif cnt.get("connection_id") != connection or cnt.get("generation") != generation:
        base["reasons"].append("latest_cnt_connection_or_generation_mismatch")
    elif not _state_before(cnt, quote):
        base["reasons"].append("latest_cnt_not_strictly_causal")
    else:
        try:
            if _quote_times(quote)[0] - _state_times(cnt)[0] > timedelta(seconds=policy["state_max_age_seconds"]):
                base["reasons"].append("latest_cnt_stale")
            else:
                cnt_kind = _cnt_kind(cnt, capture_day)
                if cnt_kind == "contrary":
                    base["reasons"].append("contrary_cnt")
                elif cnt_kind != "normal":
                    base["reasons"].append("latest_cnt_unqualified")
        except ValueError:
            base["reasons"].append("latest_cnt_time_invalid")
    mko_kind = "none"
    if mko is not None:
        if mko_issue:
            base["reasons"].append("latest_mko_" + mko_issue)
            mko_kind = "unknown"
        elif (mko.get("connection_id") != connection or mko.get("generation") != generation or not _state_before(mko, quote)):
            base["reasons"].append("latest_mko_unqualified")
            mko_kind = "unknown"
        else:
            mko_kind = _mko_kind(mko, capture_day)
            if mko_kind == "contrary":
                base["reasons"].append("contrary_mko")
            elif mko_kind == "unknown":
                base["reasons"].append("latest_mko_unknown")
    if cnt_kind == "contrary" or mko_kind == "contrary":
        base["flow_context"] = "contrary_observed"
    elif cnt_kind == "normal" and mko_kind in {"none", "neutral"}:
        base["flow_context"] = "observed_regular_flow"
    return base


def _allocation(records, candidate_id, symbol, scan, capture_start, scan_at):
    values = [row for row in records if row.get("kind") == "quote_subscription" and row.get("candidate_id") == candidate_id
              and row.get("symbol") == symbol and row.get("tr_id") == BOOK_TR and row.get("status") in {"allocated", "shared_operational"}]
    values = [row for row in values if type(row.get("sequence")) is int and row["sequence"] > scan.get("sequence", 0)]
    return min(values, key=lambda row: row["sequence"], default=None)


def _allocation_time_in_window(row, capture_start, scan_at):
    try:
        return _time(row, "observed_at") >= capture_start and _time(row, "observed_at") >= scan_at
    except ValueError:
        return False


def _normal_cnt_after(records, allocation, symbol, allocation_at, deadline, capture_day):
    for row in records:
        if row.get("tr_id") != CNT_TR or row.get("symbol") != symbol or row.get("kind") != "market_state" or row.get("sequence", 0) <= allocation.get("sequence", 0):
            continue
        try:
            received, observed = _state_times(row)
        except ValueError:
            continue
        if received < allocation_at or observed < allocation_at or received > deadline or observed > deadline:
            continue
        if received <= observed and _cnt_kind(row, capture_day) == "normal":
            return row
    return None


def _first_book_after(records, after, symbol):
    values = [row for row in records if row.get("kind") == "ws_quote" and row.get("symbol") == symbol
              and row.get("provenance", {}).get("tr_id") == BOOK_TR and row.get("sequence", 0) > after.get("sequence", 0)]
    return min(values, key=lambda row: row.get("sequence", 0), default=None)


def _endpoint(records, anchor, symbol, end, as_of, policy):
    if anchor is None:
        return None, "anchor_missing"
    try:
        target = _quote_times(anchor)[1] + timedelta(seconds=policy["horizon_seconds"])
    except ValueError:
        return None, "anchor_time_invalid"
    if target + timedelta(seconds=policy["max_markout_delay_seconds"]) > end:
        return None, "markout_window_not_mature"
    try:
        anchor_received, anchor_observed = _quote_times(anchor)
        anchor_p = anchor["provenance"]
    except (ValueError, KeyError, TypeError):
        return None, "anchor_time_invalid"
    previous_received, previous_observed = anchor_received, anchor_observed
    for row in records:
        if row.get("kind") != "ws_quote" or row.get("symbol") != symbol or row.get("provenance", {}).get("tr_id") != BOOK_TR or row.get("sequence", 0) <= anchor.get("sequence", 0):
            continue
        try:
            received, observed = _quote_times(row)
        except ValueError:
            return row, "first_endpoint_time_invalid"
        if not previous_received <= received <= observed or observed < previous_observed:
            return row, "first_endpoint_clock_regression"
        if received > end or observed > end or received > as_of or observed > as_of:
            return row, "first_endpoint_outside_capture_or_analysis"
        if received >= target or observed >= target:
            if received < target:
                return row, "first_endpoint_publication_delayed"
            if (received > target + timedelta(seconds=policy["max_markout_delay_seconds"])
                    or observed > target + timedelta(seconds=policy["max_markout_delay_seconds"])):
                return row, "first_endpoint_too_late"
            p = row.get("provenance")
            if not isinstance(p, dict) or p.get("connection_id") != anchor_p.get("connection_id") or p.get("generation") != anchor_p.get("generation"):
                return row, "endpoint_connection_or_generation_mismatch"
            return row, None
        previous_received, previous_observed = received, observed
    return None, "endpoint_missing"


def _move(anchor, endpoint, anchor_state, endpoint_state):
    if (anchor_state["flow_context"] != "observed_regular_flow" or endpoint_state["flow_context"] != "observed_regular_flow"
            or not _book_valid(anchor) or not _book_valid(endpoint)):
        return None
    try:
        value = float((Decimal(str(endpoint["bid"])) / Decimal(str(anchor["ask"])) - 1) * 100)
        return value if math.isfinite(value) else None
    except (DecimalException, ValueError, TypeError):
        return None


def build_observed_regular_flow_report(study_bytes, observations, *, study_sha256, analysis_as_of):
    """Add observed-only regular-flow pairs while preserving the v1 result verbatim."""
    if type(study_bytes) is not bytes:
        raise ValueError("study bytes required")
    study = load_json_bytes(study_bytes)
    policy, capture_start, capture_end = _policy(study)
    as_of = _timestamp(analysis_as_of, "analysis_as_of")
    strict = build_pregate_candidate_report(study_bytes, observations, study_sha256=study_sha256, analysis_as_of=analysis_as_of)
    records = observations.get("records") if isinstance(observations, dict) else None
    if not isinstance(records, list):
        raise ValueError("bounded journal records required")
    capture_day = capture_start.astimezone(KST).strftime("%Y%m%d")
    scan = next(row for row in records if row.get("kind") == "scan")
    scan_at = _time(scan, "observed_at")
    rows, eligible = [], 0
    for strict_row in strict["candidates"]:
        candidate_id, symbol, rank = strict_row["candidate_id"], strict_row["symbol"], strict_row["rank"]
        base = {"candidate_id": candidate_id, "symbol": symbol, "rank": rank,
                "allocation_status": strict_row["allocation_status"],
                "original_entry_quote": deepcopy(strict_row.get("entry_quote")), "anchor_quote": None,
                "markout_quote": None, "anchor_delay_from_first_book": None, "gross_quote_move_pct": None,
                "anchor_state": {"flow_context": "unknown", "vi_state": "unknown", "tradability": "not_evaluated", "fillability": "not_evaluated", "reasons": ["not_evaluated"], "raw_cnt_evidence": None, "raw_mko_evidence": None, "raw_mko_count": 0},
                "markout_state": {"flow_context": "unknown", "vi_state": "unknown", "tradability": "not_evaluated", "fillability": "not_evaluated", "reasons": ["not_evaluated"], "raw_cnt_evidence": None, "raw_mko_evidence": None, "raw_mko_count": 0}, "reasons": []}
        if rank > 5:
            base["anchor_state"]["reasons"] = ["not_sampled"]
            base["markout_state"]["reasons"] = ["not_sampled"]
            rows.append(base); continue
        allocation = _allocation(records, candidate_id, symbol, scan, capture_start, scan_at)
        if allocation is None:
            base["reasons"].append("allocation_missing")
            rows.append(base); continue
        try:
            allocation_at = _time(allocation, "observed_at")
        except ValueError:
            base["reasons"].append("allocation_time_invalid")
            rows.append(base); continue
        if allocation_at < capture_start or allocation_at < scan_at:
            base["reasons"].append("allocation_before_capture_or_scan")
            rows.append(base); continue
        deadline = allocation_at + timedelta(seconds=policy["preparation_seconds"])
        if allocation_at + timedelta(seconds=policy["preparation_seconds"] + policy["horizon_seconds"] + policy["max_markout_delay_seconds"]) > capture_end:
            base["reasons"].append("allocation_not_mature")
            rows.append(base); continue
        cnt = _normal_cnt_after(records, allocation, symbol, allocation_at, deadline, capture_day)
        if cnt is None:
            base["reasons"].append("qualifying_cnt_missing_in_preparation_window")
            rows.append(base); continue
        anchor = _first_book_after(records, cnt, symbol)
        base["anchor_quote"] = _compact_quote(anchor)
        if anchor is None:
            base["reasons"].append("anchor_book_missing")
            rows.append(base); continue
        try:
            anchor_received, anchor_observed = _quote_times(anchor)
            if (anchor_received < allocation_at or anchor_observed < allocation_at or anchor_received > deadline
                    or anchor_observed > deadline or anchor_received > capture_end or anchor_observed > capture_end
                    or anchor_received > as_of or anchor_observed > as_of):
                base["reasons"].append("anchor_outside_preparation_window")
                rows.append(base); continue
        except ValueError:
            base["reasons"].append("anchor_time_invalid")
            rows.append(base); continue
        anchor_p = anchor.get("provenance") if isinstance(anchor.get("provenance"), dict) else {}
        if cnt.get("connection_id") != anchor_p.get("connection_id") or cnt.get("generation") != anchor_p.get("generation"):
            base["reasons"].append("qualifying_cnt_connection_or_generation_mismatch")
            rows.append(base); continue
        anchor_state = _state_context(records, anchor, capture_day, policy)
        base["anchor_state"] = anchor_state
        try:
            original = strict_row.get("entry_quote")
            if original is not None:
                base["anchor_delay_from_first_book"] = (_quote_times(anchor)[1] - _timestamp(original.get("observed_at"), "original observed_at")).total_seconds()
        except ValueError:
            base["reasons"].append("anchor_or_original_time_invalid")
        endpoint, endpoint_reason = _endpoint(records, anchor, symbol, capture_end, as_of, policy)
        base["markout_quote"] = _compact_quote(endpoint)
        endpoint_state = _state_context(records, endpoint, capture_day, policy)
        base["markout_state"] = endpoint_state
        if endpoint_reason:
            base["reasons"].append(endpoint_reason)
        base["gross_quote_move_pct"] = None if endpoint_reason else _move(anchor, endpoint, anchor_state, endpoint_state)
        if base["gross_quote_move_pct"] is not None:
            eligible += 1
        rows.append(base)
    return {"schema_version": POLICY_VERSION, "evaluation_epoch": strict["evaluation_epoch"],
            "analysis_as_of": strict["analysis_as_of"], "study_sha256": strict["study_sha256"],
            "observed_regular_flow_policy": deepcopy(policy), "strict_report": deepcopy(strict),
            "denominator": deepcopy(strict["denominator"]), "counts": {"denominator": len(rows), "eligible_pairs": eligible},
            "candidates": rows, "account_return": None, "net_pnl": None, "profitability_pass": False,
            "production_eligible": False, "source_authenticity_verified": False,
            "limitations": ["Observed regular flow is not a tradability, fillability, order, fill, or P&L claim.",
                            "The declared fixed_at is hash-bound input, not independently authenticated."]}
