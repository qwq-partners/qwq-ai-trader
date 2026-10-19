"""Read a sealed first-scan journal without making an entry or P&L claim."""
from __future__ import annotations

from copy import deepcopy
from datetime import timedelta
from decimal import Decimal, DecimalException
import hashlib

from .entry_evaluation_bundle import load_json_bytes
from .entry_gate_report import build_gate_report
from .entry_markout import MarkoutPolicy, select_markout_quote
from .entry_observation_runtime import validate_window_contract
from .entry_price_shadow import _timestamp
from .kis_frame_diagnostics import build_frame_report, validate_study_binding
from .model_comparison_admission import classify_market_state
from .selection_basis import build_selection_report


PROFILE = "runner-first-scan-v4"
TOP_CANDIDATES = 5
HORIZON_SECONDS = 900
MAX_MARKOUT_DELAY_SECONDS = 5
MARKET_STATE_MAX_AGE_SECONDS = 5
BOOK_TR = "H0STASP0"


def _sha(value):
    if type(value) is not str or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
        raise ValueError("lowercase study SHA-256 required")
    return value


def _strict_complete(observations, sha):
    if type(observations) is not dict or observations.get("schema_version") != 1:
        raise ValueError("observation schema required")
    journal = observations.get("journal")
    if (type(journal) is not dict or journal.get("study_sha256") != sha
            or journal.get("sealed") is not True or journal.get("structurally_valid") is not True
            or observations.get("complete") is not True or observations.get("dropped_records") != 0
            or journal.get("persistence_dropped_records") != 0
            or observations.get("incomplete_reasons") != []):
        raise ValueError("sealed complete undropped journal required")
    records = observations.get("records")
    if type(records) is not list or len(records) > 1_000_000:
        raise ValueError("bounded journal records required")
    for sequence, record in enumerate(records, 1):
        if type(record) is not dict or record.get("sequence") != sequence:
            raise ValueError("journal sequence mismatch")


def _quote_time(record):
    try:
        provenance = record["provenance"]
        if type(provenance) is not dict:
            raise ValueError
        return _timestamp(provenance["received_at"], "quote received_at"), _timestamp(record["observed_at"], "quote observed_at")
    except (KeyError, TypeError, ValueError):
        raise ValueError("invalid quote time")


def _book_valid(record):
    try:
        received, observed = _quote_time(record)
        p = record["provenance"]
        if (record.get("kind") != "ws_quote" or p.get("tr_id") != BOOK_TR
                or type(p.get("message_count")) is not int or p.get("message_count") != 1
                or p.get("source_as_of") is not None
                or received > observed):
            return False
        for key in ("bid", "ask"):
            value = record.get(key)
            if type(value) is bool or not isinstance(value, (str, int, float, Decimal)):
                return False
            try:
                if not Decimal(str(value)).is_finite() or Decimal(str(value)) <= 0:
                    return False
            except (DecimalException, ValueError):
                return False
        if Decimal(str(record["bid"])) > Decimal(str(record["ask"])):
            return False
        if any(type(record.get(key)) is not int or record[key] <= 0 for key in ("bid_size", "ask_size")):
            return False
        return True
    except ValueError:
        return False


def _compact_quote(record):
    if record is None:
        return None
    fields = ("sequence", "quote_id", "symbol", "bid", "ask", "bid_size", "ask_size", "observed_at")
    out = {key: deepcopy(record.get(key)) for key in fields}
    provenance = record.get("provenance", {})
    out["received_at"] = deepcopy(provenance.get("received_at"))
    out["provenance"] = {key: deepcopy(provenance.get(key)) for key in
                         ("tr_id", "connection_id", "generation", "exchange_time", "hour_class_code",
                          "market_class_code", "raw_field_count", "message_count", "source_as_of")}
    return out


def _subscription_events(records, candidate_id, symbol):
    events = []
    for row in records:
        if (row.get("kind") == "quote_subscription" and row.get("tr_id") == BOOK_TR
                and row.get("symbol") == symbol
                and row.get("candidate_id") in (candidate_id, None)):
            events.append(row)
    return events


def _allocation(records, candidate_id, symbol, scan_sequence):
    events = [r for r in _subscription_events(records, candidate_id, symbol) if r["sequence"] > scan_sequence]
    history = [{key: deepcopy(row.get(key)) for key in ("sequence", "observed_at", "status", "reason", "connection_id", "generation")}
               for row in events]
    allocated = next((row for row in events if row.get("candidate_id") == candidate_id
                      and row.get("status") in ("allocated", "shared_operational")), None)
    if allocated is not None:
        return allocated, allocated["status"], history
    if events:
        last = events[-1]
        reason = last.get("reason") or last.get("status") or "unallocated"
        return None, str(reason), history
    return None, "not_recorded", history


def _first_book_after(records, *, symbol, after_sequence, scan_at, allocation_at, capture_end, as_of):
    for row in records:
        if row.get("sequence", 0) <= after_sequence or row.get("kind") != "ws_quote" or row.get("symbol") != symbol:
            continue
        if row.get("provenance", {}).get("tr_id") != BOOK_TR:
            continue
        try:
            received, observed = _quote_time(row)
        except ValueError:
            return row, "first_krx_book_invalid"
        if received > observed:
            return row, "first_krx_book_invalid"
        if received < scan_at or received < allocation_at:
            return row, "first_krx_book_before_scan_or_allocation"
        if received > capture_end or received > as_of or observed > capture_end or observed > as_of:
            return row, "first_krx_book_outside_window"
        return row, None if _book_valid(row) else "first_krx_book_invalid"
    return None, "first_krx_book_missing"


def _market_state(records, quote, as_of):
    if quote is None or not _book_valid(quote):
        return {"verdict": "unknown", "basis": ["entry_quote_unavailable"], "source_refs": []}
    p = quote["provenance"]
    connection, generation = p.get("connection_id"), p.get("generation")
    if type(connection) is not str or not connection or type(generation) is not int or type(generation) is bool:
        return {"verdict": "unknown", "basis": ["entry_connection_or_generation_missing"], "source_refs": []}
    received, _ = _quote_time(quote)
    state_rows = []
    equal_time_channels = set()
    for row in records:
        if (row.get("kind") != "market_state" or row.get("symbol") != quote.get("symbol")
                or row.get("sequence", 0) >= quote["sequence"]):
            continue
        state_rows.append(row)
        try:
            state_received = _timestamp(row.get("received_at"), "state received_at")
        except ValueError:
            continue
        if state_received == received:
            equal_time_channels.add(row.get("tr_id"))
    # Keep malformed and other-generation records in the classifier input so it
    # can reject a latest bad record rather than silently revive an older good one.
    result = classify_market_state(state_rows, now=received.isoformat(), max_age_seconds=MARKET_STATE_MAX_AGE_SECONDS,
                                   expected_connection_id=connection, expected_generation=generation)
    # The classifier intentionally permits equality and event-channel records do not age out.
    # This report's contract is stricter: both latest channel records must precede the BOOK and be fresh.
    latest = {}
    for row in state_rows:
        tr = row.get("tr_id")
        if tr not in ("H0STCNT0", "H0STMKO0"):
            continue
        try:
            at = _timestamp(row.get("received_at"), "state received_at")
        except ValueError:
            continue
        if not (at < received and at <= as_of):
            continue
        key = (at, row.get("local_frame_index", -1) if type(row.get("local_frame_index")) is int else -1,
               row.get("record_index", -1) if type(row.get("record_index")) is int else -1)
        if tr not in latest or key > latest[tr][0]:
            latest[tr] = (key, row)
    stale = [tr for tr in ("H0STCNT0", "H0STMKO0")
             if tr not in latest or received - latest[tr][0][0] > timedelta(seconds=MARKET_STATE_MAX_AGE_SECONDS)]
    if stale:
        result = deepcopy(result)
        result["verdict"] = "unknown"
        result["basis"] = list(result.get("basis", [])) + ["missing_or_stale_prior:" + tr for tr in stale]
    if equal_time_channels & {"H0STCNT0", "H0STMKO0"}:
        result = deepcopy(result)
        result["verdict"] = "unknown"
        result["basis"] = list(result.get("basis", [])) + ["state_not_strictly_prior:" + tr
                                                               for tr in sorted(equal_time_channels & {"H0STCNT0", "H0STMKO0"})]
    result = deepcopy(result)
    result["raw_channel_evidence"] = {tr: deepcopy(latest[tr][1]) if tr in latest else None
                                      for tr in ("H0STCNT0", "H0STMKO0")}
    return result


def _markout(records, entry, as_of, study, capture_end):
    if entry is None or not _book_valid(entry):
        return None, "entry_quote_unavailable"
    _, entry_observed = _quote_time(entry)
    target = entry_observed + timedelta(seconds=HORIZON_SECONDS)
    if target + timedelta(seconds=MAX_MARKOUT_DELAY_SECONDS) > capture_end:
        return None, "missing_mature_markout_window"
    if target > as_of:
        return None, "markout_not_matured"
    quotes = [row for row in records if (row.get("kind") == "ws_quote" and row.get("symbol") == entry.get("symbol")
                                         and row.get("sequence", 0) > entry.get("sequence", 0)
                                         and row.get("provenance", {}).get("tr_id") == BOOK_TR)]
    try:
        policy = MarkoutPolicy.from_dict(study.get("markout_policy"))
        start = _timestamp(study["capture"]["start_at"], "capture start_at")
        if (policy.horizon_seconds != HORIZON_SECONDS or policy.max_delay_seconds != Decimal(MAX_MARKOUT_DELAY_SECONDS)
                or policy.fixed_at > start):
            raise ValueError("incompatible pregate markout policy")
        selected = select_markout_quote(entry, quotes, policy)
    except (ValueError, TypeError, KeyError):
        return None, "markout_stream_invalid"
    if selected is not None:
        try:
            selected_received, selected_observed = _quote_time(selected)
        except ValueError:
            return selected, "first_markout_book_invalid"
        if selected_received > capture_end or selected_observed > capture_end or selected_received > as_of or selected_observed > as_of:
            return None, "markout_book_missing"
        if selected_received - target > timedelta(seconds=MAX_MARKOUT_DELAY_SECONDS):
            return selected, "first_markout_book_too_late"
        if selected_observed - target > timedelta(seconds=MAX_MARKOUT_DELAY_SECONDS):
            return selected, "first_markout_book_publication_delayed"
        return selected, None if _book_valid(selected) else "first_markout_book_invalid"
    return None, "markout_book_missing"


def build_pregate_candidate_report(study_bytes, observations, *, study_sha256, analysis_as_of):
    """Return a bounded price/state readout; never infer a signal, order, or return."""
    sha = _sha(study_sha256)
    if type(study_bytes) is not bytes or len(study_bytes) > 4 * 1024 * 1024 or hashlib.sha256(study_bytes).hexdigest() != sha:
        raise ValueError("original study bytes/hash mismatch")
    study = load_json_bytes(study_bytes)
    if type(study) is not dict or study.get("capture", {}).get("version") != PROFILE:
        raise ValueError("runner-first-scan-v4 study required")
    capture = study["capture"]
    channels = capture.get("channels")
    if (type(channels) is not dict or channels.get("max_candidates") != TOP_CANDIDATES
            or type(channels.get("max_candidates")) is not int
            or channels.get("market_state_evidence") is not True):
        raise ValueError("pregate requires fixed five BOOK/state channel contract")
    capture_start = _timestamp(capture.get("start_at"), "capture start_at")
    capture_admission_end = _timestamp(capture.get("admission_end_at"), "capture admission_end_at")
    capture_end = _timestamp(capture.get("end_at"), "capture end_at")
    if (not capture_start < capture_admission_end < capture_end
            or _timestamp(study.get('as_of'), 'study as_of') != capture_end):
        raise ValueError('capture chronology/as_of mismatch')
    if (capture_end - capture_admission_end).total_seconds() < HORIZON_SECONDS + MAX_MARKOUT_DELAY_SECONDS:
        raise ValueError('capture requires 905-second markout tail')
    policy = MarkoutPolicy.from_dict(study.get('markout_policy'))
    if (study.get('outcome_basis') != 'fixed_horizon_bid_markout'
            or policy.horizon_seconds != HORIZON_SECONDS
            or policy.max_delay_seconds != MAX_MARKOUT_DELAY_SECONDS
            or policy.fixed_at > capture_start):
        raise ValueError('pre-fixed 900-second/5-second policy required')
    as_of = _timestamp(analysis_as_of, "analysis_as_of")
    _strict_complete(observations, sha)
    if observations.get("evaluation_epoch") != study.get("evaluation_epoch"):
        raise ValueError("study epoch mismatch")
    validate_window_contract(study, observations)
    validate_study_binding(observations, study)
    frame = build_frame_report(observations, as_of=as_of.isoformat())
    selection = build_selection_report(observations)
    gates = build_gate_report(observations, as_of=as_of.isoformat())
    records = observations["records"]
    scans = [row for row in records if row.get("kind") == "scan"]
    if len(scans) != 1:
        raise ValueError("exactly one first scan required")
    scan = scans[0]
    if (scan.get("population_scope") != "first_returned_scan_candidates"
            or scan.get("scan_admission_ref") != capture.get("scan_admission_ref")
            or type(scan.get("candidates")) is not list):
        raise ValueError("first returned scan binding required")
    scan_at = _timestamp(scan.get("observed_at"), "first scan observed_at")
    if not capture_start <= scan_at < capture_admission_end:
        raise ValueError("first scan outside capture admission window")
    candidates = scan["candidates"]
    selection_settings = capture['selection_basis']
    gate_settings = capture['entry_gate_trace']
    if (scan.get('selection_basis_expected') is not True
            or scan.get('selection_basis_max_candidates') != selection_settings['max_candidates']
            or scan.get('selection_basis_max_terms') != selection_settings['max_source_terms']
            or len(candidates) > min(selection_settings['max_candidates'], gate_settings['max_candidates'])):
        raise ValueError('selection study/scan limits mismatch')
    modifiers = selection_settings['version'] in ('selection-basis-v3', 'selection-basis-v4')
    if (scan.get('selection_modifiers_expected', False) != modifiers
            or scan.get('selection_modifier_max_stages') != selection_settings.get('max_modifier_stages')
            or scan.get('selection_basis_version') != ('selection-basis-v4' if selection_settings['version'] == 'selection-basis-v4' else None)):
        raise ValueError('selection modifier/version binding mismatch')
    references = gates.get('references') or {}
    for key, marker in (('source_version_ref', 'entry_gate_source_version_ref'),
                        ('configuration_ref', 'entry_gate_configuration_ref')):
        if references.get(marker) != capture[key]:
            raise ValueError('gate study/scan source binding mismatch')
    seen = set()
    rows = []
    for rank, candidate in enumerate(candidates, 1):
        if type(candidate) is not dict or type(candidate.get("symbol")) is not str or type(candidate.get("candidate_id")) is not str:
            raise ValueError("candidate identity required")
        cid, symbol = candidate["candidate_id"], candidate["symbol"]
        if cid != f"{scan.get('scan_id')}:{symbol}" or cid in seen:
            raise ValueError("candidate identity mismatch")
        seen.add(cid)
        if rank > TOP_CANDIDATES:
            rows.append({"candidate_id": cid, "symbol": symbol, "rank": rank,
                         "allocation_status": "not_subscribed_by_fixed_top_n", "allocation_history": [],
                         "entry_quote": None, "markout_quote": None,
                         "entry_market_state": {"verdict": "unknown", "basis": ["not_subscribed_by_fixed_top_n"], "source_refs": []},
                         "markout_market_state": {"verdict": "unknown", "basis": ["not_subscribed_by_fixed_top_n"], "source_refs": []},
                         "reasons": ["not_subscribed_by_fixed_top_n"]})
            continue
        allocation, allocation_status, history = _allocation(records, cid, symbol, scan["sequence"])
        try:
            allocation_at = _timestamp(allocation["observed_at"], "allocation observed_at") if allocation else None
        except ValueError:
            allocation_at = None
        entry, entry_reason = (None, "allocation_not_recorded") if allocation is None or allocation_at is None else _first_book_after(
            records, symbol=symbol, after_sequence=allocation["sequence"], scan_at=scan_at, allocation_at=allocation_at,
            capture_end=capture_end, as_of=as_of)
        markout, markout_reason = _markout(records, entry, as_of, study, capture_end)
        reasons = [reason for reason in (entry_reason, markout_reason) if reason is not None]
        rows.append({"candidate_id": cid, "symbol": symbol, "rank": rank,
                     "allocation_status": allocation_status, "allocation_history": history,
                     # Preserve the first raw BOOK even when it is unusable.  Hiding it
                     # would make a later valid quote look like the baseline.
                     "entry_quote": _compact_quote(entry), "markout_quote": _compact_quote(markout),
                     "entry_market_state": _market_state(records, entry, as_of),
                     "markout_market_state": _market_state(records, markout, as_of), "reasons": reasons})
    return {"schema_version": "pregate-candidate-readout-v1", "evaluation_epoch": observations["evaluation_epoch"],
            "analysis_as_of": as_of.isoformat(), "study_sha256": sha,
            "price_observation_policy": {"anchor": "first_krx_book_after_candidate_allocation",
                "horizon_anchor": "entry_quote_observed_at", "horizon_seconds": HORIZON_SECONDS,
                "max_markout_delay_seconds": MAX_MARKOUT_DELAY_SECONDS,
                "market_state_max_age_seconds": MARKET_STATE_MAX_AGE_SECONDS},
            "denominator": {"population_scope": "first_returned_scan_candidates", "returned_candidates": len(candidates),
                            "sampled_candidates": min(TOP_CANDIDATES, len(candidates)), "sampling_limit": TOP_CANDIDATES},
            "candidates": rows, "validation": {"frame": frame, "selection": selection, "gates": gates},
            "account_return": None, "profitability_pass": False, "production_eligible": False,
            "source_authenticity_verified": False,
            "limitations": ["Prices and states are observations, not orders, fills, capital allocation, or P&L.",
                            "Only the returned first scan is the denominator; ranks after five are intentionally unsubscribed."]}
