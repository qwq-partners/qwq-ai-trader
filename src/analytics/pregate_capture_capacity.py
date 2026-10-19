"""Offline-only assessment of first-scan-v4 pre-gate capture resources.

This checks declared study arithmetic only.  It neither opens a runtime nor
turns a passing configuration into permission to install one.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import time
from decimal import Decimal
import math
from pathlib import Path
from typing import Any

from .entry_gate_trace import validate_settings as validate_gate_settings
from .entry_markout import MarkoutPolicy
from .entry_observation_journal import MAX_LINE_BYTES
from .entry_price_shadow import KST, _timestamp
from .first_scan_resources import (FIRST_SCAN_RETAINED_MEMORY_LIMIT_BYTES,
                                   first_scan_record_limits, first_scan_record_byte_limits,
                                   first_scan_journal_bound)
from .kis_frame_diagnostics import validate_settings as validate_frame_settings
from .selection_basis import validate_settings as validate_selection_settings


READOUT_DEFAULT_MAX_BYTES = 256 * 1024 * 1024
CHANNELS_PER_CANDIDATE = 3
MAX_CANDIDATES = 5
MARKOUT_SECONDS = 900
MARKOUT_MAX_DELAY_SECONDS = 5
MIN_RECORD_BYTES = 65536
RUNTIME_CHANNEL_CAP = 41

_CHANNEL_FIELDS = frozenset((
    "registration_cap", "external_reserved", "operational_headroom",
    "max_candidates", "lease_seconds", "evidence_ref",
    "market_state_evidence", "market_state_max_records",
))
_RESOURCE_FIELDS = ("buffer_capacity", "queue_capacity", "batch_size",
                    "max_bytes", "max_record_bytes")
_REFERENCE_FIELDS = ("study_ref", "scan_admission_ref", "source_version_ref",
                     "configuration_ref", "fee_evidence_ref", "capital_evidence_ref",
                     "capacity_evidence_ref")
_CAPTURE_FIELDS = frozenset((
    "version", *_REFERENCE_FIELDS, "start_at", "admission_end_at", "end_at",
    "journal_path", *_RESOURCE_FIELDS, "open_timeout_seconds", "close_timeout_seconds",
    "channels", "selection_basis", "entry_gate_trace", "frame_diagnostics",
))
_OPTIONAL_CAPTURE_FIELDS = frozenset(("record_allocations", "record_max_bytes"))
_MARKOUT_FIELDS = frozenset(("version", "policy_ref", "fixed_at", "horizon_seconds",
                             "max_delay_seconds", "anchor"))


def _positive_integer(value: Any, name: str, *, zero: bool = False) -> int:
    if type(value) is not int or value < (0 if zero else 1):
        raise ValueError(f"{name} must be a {'non-negative' if zero else 'positive'} integer")
    return value


def _positive_seconds(value: Any, name: str) -> float:
    if (type(value) not in (int, float) or (type(value) is int and value.bit_length() > 256)
            or not math.isfinite(value) or value <= 0):
        raise ValueError(f"{name} must be a finite positive number")
    return value


def _check(*, passed: bool, **values: Any) -> dict[str, Any]:
    return {"passed": bool(passed), **values}


def _capture(study: Any) -> dict[str, Any]:
    if type(study) is not dict or type(study.get("capture")) is not dict:
        raise ValueError("study.capture must be an object")
    return study["capture"]


def _validate_contract(study: dict[str, Any], capture: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], Any, Any, Any]:
    if (capture.get("version") != "runner-first-scan-v4"
            or not _CAPTURE_FIELDS <= set(capture)
            or set(capture) - _CAPTURE_FIELDS - _OPTIONAL_CAPTURE_FIELDS):
        raise ValueError("pre-gate assessment requires runner-first-scan-v4")
    for field in _REFERENCE_FIELDS:
        if type(capture[field]) is not str or not capture[field].strip() or len(capture[field]) > 200:
            raise ValueError(f"{field} must be bounded non-empty text")
    if (type(capture["journal_path"]) is not str or len(capture["journal_path"]) > 4096
            or not Path(capture["journal_path"]).is_absolute()):
        raise ValueError("journal_path must be an absolute path")
    for field in ("open_timeout_seconds", "close_timeout_seconds"):
        if _positive_seconds(capture[field], field) > 10:
            raise ValueError(f"{field} exceeds runtime maximum")

    start = _timestamp(capture["start_at"], "capture.start_at")
    admission_end = _timestamp(capture["admission_end_at"], "capture.admission_end_at")
    end = _timestamp(capture["end_at"], "capture.end_at")
    if (not start < admission_end < end or end != _timestamp(study.get("as_of"), "study.as_of")):
        raise ValueError("capture timestamps/as_of must be timezone-aware and chronological")
    local_start, local_end = start.astimezone(KST), end.astimezone(KST)
    if (local_start.date() != local_end.date() or local_start.weekday() >= 5
            or not time(9) <= local_start.time() < local_end.time() < time(15, 20)):
        raise ValueError("capture must stay in one KRX weekday session")

    policy_data = study.get("markout_policy")
    if study.get("outcome_basis") != "fixed_horizon_bid_markout" or type(policy_data) is not dict or set(policy_data) != _MARKOUT_FIELDS:
        raise ValueError("exact fixed-horizon markout policy required")
    policy = MarkoutPolicy.from_dict(policy_data)
    if (policy.horizon_seconds != MARKOUT_SECONDS or policy.max_delay_seconds != Decimal(MARKOUT_MAX_DELAY_SECONDS)
            or policy.fixed_at > start or len(policy.policy_ref) > 200):
        raise ValueError("markout policy must be pre-fixed at 900 seconds with five-second delay")

    selection = validate_selection_settings(capture.get("selection_basis"))
    gate = validate_gate_settings(capture.get("entry_gate_trace"))
    validate_frame_settings(capture.get("frame_diagnostics"))
    if selection["max_candidates"] < MAX_CANDIDATES or gate["max_candidates"] < selection["max_candidates"]:
        raise ValueError("first-scan-v4 metadata must retain all returned candidates")
    if (gate["source_version_ref"] != capture.get("source_version_ref")
            or gate["configuration_ref"] != capture.get("configuration_ref")):
        raise ValueError("entry gate source/configuration binding mismatch")

    channels = capture.get("channels")
    if type(channels) is not dict or set(channels) != _CHANNEL_FIELDS:
        raise ValueError("explicit three-channel capture contract required")
    if channels["max_candidates"] != MAX_CANDIDATES:
        raise ValueError("channel contract must reserve exactly five candidates")
    if channels["market_state_evidence"] is not True:
        raise ValueError("three-channel capture requires explicit market-state evidence")
    _positive_integer(channels["market_state_max_records"], "market_state_max_records")
    _positive_integer(channels["registration_cap"], "registration_cap")
    _positive_integer(channels["external_reserved"], "external_reserved", zero=True)
    _positive_integer(channels["operational_headroom"], "operational_headroom", zero=True)
    _positive_seconds(channels["lease_seconds"], "lease_seconds")
    if (channels["registration_cap"] > RUNTIME_CHANNEL_CAP
            or channels["external_reserved"] + channels["operational_headroom"] > channels["registration_cap"]):
        raise ValueError("registration cap/reserve contract is invalid")
    if (type(channels["evidence_ref"]) is not str or not channels["evidence_ref"].strip()
            or len(channels["evidence_ref"]) > 200):
        raise ValueError("channels.evidence_ref must be non-empty text")
    return selection, gate, deepcopy(channels), start, admission_end, end


def assess_pregate_capture_resources(
        study: Any, *, operational_registrations: int, external_registrations: int,
        readout_max_bytes: int = READOUT_DEFAULT_MAX_BYTES) -> dict[str, Any]:
    """Assess declared pre-gate capacity without probing a live process.

    ``operational_registrations`` and ``external_registrations`` are caller
    evidence, not verified runtime demand.  Numeric success is deliberately
    insufficient for installation readiness.
    """
    capture = _capture(study)
    selection, gate, channels, start, admission_end, end = _validate_contract(study, capture)
    operational = _positive_integer(operational_registrations, "operational_registrations", zero=True)
    external = _positive_integer(external_registrations, "external_registrations", zero=True)
    readout_limit = _positive_integer(readout_max_bytes, "readout_max_bytes")
    if readout_limit > READOUT_DEFAULT_MAX_BYTES:
        raise ValueError("readout_max_bytes exceeds the supported 256 MiB reader cap")
    resources = {field: _positive_integer(capture.get(field), field) for field in _RESOURCE_FIELDS}
    if resources["max_record_bytes"] > MAX_LINE_BYTES - 2048:
        raise ValueError("max_record_bytes exceeds the runtime journal row limit")
    guarded_limits = first_scan_record_limits(capture)

    metadata_records = 1 + selection["max_candidates"] + gate["max_candidates"]
    # One fresh state per channel at each of two anchors per candidate.
    # These are only the baseline records for one successful connection, not
    # an upper bound on streaming traffic, reconnects, or operational events.
    minimum_state_records = MAX_CANDIDATES * 2 * 2
    minimum_book_records = MAX_CANDIDATES * 2
    # Connected + desired/allocated per candidate + requested/ACK/observed
    # per channel. Operational channel lifecycle also uses this same journal.
    subscription_baseline = (1 + MAX_CANDIDATES * (2 + 3 * CHANNELS_PER_CANDIDATE)
                             + operational * 3 + int(selection['max_candidates'] > MAX_CANDIDATES))
    minimum_buffer_records = (metadata_records + channels["market_state_max_records"]
                              + minimum_book_records + subscription_baseline)
    candidate_channels = MAX_CANDIDATES * CHANNELS_PER_CANDIDATE
    configured_cap = min(channels["registration_cap"], RUNTIME_CHANNEL_CAP)
    registration_used = (candidate_channels + operational + external
                         + channels["external_reserved"] + channels["operational_headroom"])
    window_seconds = (end - start).total_seconds()
    markout_tail_seconds = (end - admission_end).total_seconds()
    minimum_journal_bytes = (resources["buffer_capacity"]
                             * (resources["max_record_bytes"] + 2048) + 2048)
    if guarded_limits is not None:
        minimum_journal_bytes = first_scan_journal_bound(capture, guarded_limits)

    checks = {
        "registration_capacity": _check(passed=registration_used <= configured_cap,
            candidate_channels=candidate_channels, operational_registrations=operational,
            external_registrations=external, external_reserved=channels["external_reserved"],
            operational_headroom=channels["operational_headroom"], used=registration_used,
            cap=configured_cap),
        "buffer_metadata_and_market_state": _check(
            passed=resources["buffer_capacity"] >= minimum_buffer_records,
            buffer_capacity=resources["buffer_capacity"], required=minimum_buffer_records),
        "state_anchor_coverage": _check(passed=channels['market_state_max_records'] >= minimum_state_records,
            market_state_max_records=channels['market_state_max_records'], required=minimum_state_records),
        "queue_capacity": _check(passed=resources["queue_capacity"] >= resources["buffer_capacity"],
            queue_capacity=resources["queue_capacity"], required=resources["buffer_capacity"]),
        "batch_size": _check(passed=resources["batch_size"] <= resources["queue_capacity"],
            batch_size=resources["batch_size"], queue_capacity=resources["queue_capacity"]),
        "record_size": _check(passed=resources["max_record_bytes"] >= MIN_RECORD_BYTES,
            max_record_bytes=resources["max_record_bytes"], required=MIN_RECORD_BYTES),
        "journal_budget": _check(passed=resources["max_bytes"] >= minimum_journal_bytes,
            max_bytes=resources["max_bytes"], required=minimum_journal_bytes),
        "readout_limit": _check(passed=resources["max_bytes"] <= readout_limit,
            max_bytes=resources["max_bytes"], readout_max_bytes=readout_limit),
        "markout_tail": _check(passed=markout_tail_seconds >= MARKOUT_SECONDS + MARKOUT_MAX_DELAY_SECONDS,
            tail_seconds=markout_tail_seconds, required=MARKOUT_SECONDS + MARKOUT_MAX_DELAY_SECONDS),
        "lease_window": _check(passed=channels["lease_seconds"] >= window_seconds,
            lease_seconds=channels["lease_seconds"], required=window_seconds),
    }
    if guarded_limits is not None:
        guarded_total = sum(guarded_limits.values())
        checks.update({
            "guarded_record_total": _check(
                passed=resources["buffer_capacity"] >= guarded_total,
                buffer_capacity=resources["buffer_capacity"], required=guarded_total),
            "guarded_market_state_anchors": _check(
                passed=guarded_limits["market_state"] >= minimum_state_records,
                market_state_records=guarded_limits["market_state"], required=minimum_state_records),
            "guarded_ws_quote_anchors": _check(
                passed=guarded_limits["ws_quote"] >= minimum_book_records,
                ws_quote_records=guarded_limits["ws_quote"], required=minimum_book_records),
            "guarded_other_lifecycle": _check(
                passed=guarded_limits["other"] >= subscription_baseline,
                other_records=guarded_limits["other"], required=subscription_baseline),
        })
    numeric_configuration_passed = all(row["passed"] for row in checks.values())
    blockers = [
        {"code": "first_scan_runtime_readiness_unavailable", "detail": "first-scan readiness has not been implemented or verified"},
        {"code": "consumed_v5_rotation_unimplemented", "detail": "safe rotation of a consumed v5 capture is unimplemented"},
        {"code": "calendar_unverified", "detail": "this offline check does not verify a trading calendar or installation date"},
        {"code": "live_demand_not_verified", "detail": "caller-supplied registration counts are not verified live demand"},
        {"code": "record_volume_unqualified", "detail": "two-anchor, one-connection arithmetic is not a full-window traffic qualification"},
    ]
    if guarded_limits is None:
        blockers.insert(0, {"code": "runtime_memory_guard_unavailable",
                            "detail": "first-scan-v4 has no dedicated runtime memory guard"})
    else:
        blockers.insert(0, {"code": "runtime_memory_guard_unverified",
                            "detail": "the configured retained-record guard has not been verified in a live runtime"})
    return {
        "requirements": {
            "candidate_limit": MAX_CANDIDATES,
            "channels_per_candidate": CHANNELS_PER_CANDIDATE,
            "candidate_channels": candidate_channels,
            "metadata_records": metadata_records,
            "market_state_records": channels["market_state_max_records"],
            "minimum_buffer_records": minimum_buffer_records,
            "minimum_state_anchor_records": minimum_state_records,
            "minimum_book_anchor_records": minimum_book_records,
            "subscription_baseline_records": subscription_baseline,
            "markout_seconds": MARKOUT_SECONDS,
            "markout_max_delay_seconds": MARKOUT_MAX_DELAY_SECONDS,
            "runtime_channel_cap": RUNTIME_CHANNEL_CAP,
            "guarded_record_allocations": guarded_limits,
            "guarded_record_max_bytes": first_scan_record_byte_limits(capture),
            "guarded_record_total": sum(guarded_limits.values()) if guarded_limits is not None else None,
        },
        "checks": checks,
        "caller_supplied_registrations": {"operational": operational, "external": external},
        "live_demand_verified": False,
        "calendar_verified": False,
        "runtime_memory_safety_verified": False,
        "runtime_memory_guard_configured": guarded_limits is not None,
        "retained_memory_limit_bytes": (FIRST_SCAN_RETAINED_MEMORY_LIMIT_BYTES
                                        if guarded_limits is not None else None),
        "record_volume_qualified": False,
        "first_scan_runtime_readiness_verified": False,
        "consumed_v5_rotation_supported": False,
        "numeric_configuration_passed": numeric_configuration_passed,
        "installation_ready": False,
        "blockers": blockers,
    }
