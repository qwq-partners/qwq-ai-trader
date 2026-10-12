"""Build reviewable H1 observation inputs without writing or installing them."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, time, timezone
from decimal import Decimal
import hashlib
import json
import math
from pathlib import PurePath
from typing import Any, Mapping

from .entry_capital_limits import CapitalPolicy
from .entry_markout import MarkoutPolicy
from .entry_observation_runtime import READY_VERSION, validate_window_contract
from .entry_price_shadow import PriceGatePolicy, _fees, _number, build_report
from .entry_window_evaluation import KST, _aware, _effective_policy, _iso_date
from .h1_multiday_evaluation import _protocol


_REQUEST_FIELDS = {"protocol_ref", "study_template", "calendar", "input_root", "state_root"}
_REGIME = {
    "source": "entry-gate-trace-v1/regime",
    "version": "h1-gate-regime-policy-v1",
    "categories": ["downside_gate_passed", "downside_gate_blocked", "unknown"],
    "missing": "unknown",
}
_UNRESOLVED_CHECKS = [
    "Positive contemporaneous KRX regular-continuous session-state evidence pilot is required.",
    "Account fee applicability and product scope remain externally unverified.",
    "Actual channel and memory inventory remains externally unverified.",
    "Engine in-memory configuration remains externally unverified.",
    "Exchange calendar exceptions remain externally unverified.",
    "Reviewed installer and input-rotation procedure remains required before installation.",
    "Runtime preflight after materialization remains required.",
]


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n"


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"invalid {name}")
    return value.strip()


def _root(value: Any, name: str) -> PurePath:
    if (not isinstance(value, str) or not value or value != value.strip()
            or any(ord(char) < 32 or ord(char) == 127 for char in value)):
        raise ValueError(f"invalid {name}")
    text = value
    path = PurePath(text)
    if not path.is_absolute() or str(path) != text or any(part in {"", ".", ".."} for part in path.parts[1:]):
        raise ValueError(f"canonical absolute {name} required")
    return path


def _dates(calendar: Any, prepared_at: datetime) -> tuple[dict, list[str]]:
    if not isinstance(calendar, Mapping) or set(calendar) != {"source_ref", "source_sha256", "scheduled_dates"}:
        raise ValueError("strict calendar required")
    _text(calendar["source_ref"], "calendar.source_ref")
    sha = calendar["source_sha256"]
    if not isinstance(sha, str) or len(sha) != 64 or any(c not in "0123456789abcdef" for c in sha):
        raise ValueError("invalid calendar.source_sha256")
    values = calendar["scheduled_dates"]
    if not isinstance(values, list) or len(values) != 20:
        raise ValueError("exactly twenty dates required")
    parsed = [_iso_date(item, "calendar.scheduled_date") for item in values]
    if values != sorted(values) or len(set(values)) != 20 or any(item.weekday() >= 5 for item in parsed):
        raise ValueError("sorted unique weekdays required")
    if prepared_at.astimezone(KST) >= datetime.combine(parsed[0], time.min, tzinfo=KST):
        raise ValueError("preparation must precede first-date midnight KST")
    return deepcopy(dict(calendar)), list(values)


def _regime(context: Mapping[str, Any]) -> None:
    value = context.get("regime_policy")
    if not isinstance(value, Mapping):
        raise ValueError("explicit regime policy required")
    if any(value.get(key) != expected for key, expected in _REGIME.items()):
        raise ValueError("strict H1 risk-gate regime policy required")


def _h1_contract(context: Mapping[str, Any]) -> None:
    if context.get("dataset_kind") != "observed":
        raise ValueError("observed H1 study template required")
    capture = context.get("capture")
    if not isinstance(capture, Mapping) or capture.get("version") != READY_VERSION:
        raise ValueError("H1 order-ready capture profile required")
    resources = {"max_scans": 14, "buffer_capacity": 400000, "queue_capacity": 8192,
                 "batch_size": 256, "max_bytes": 512 * 1024 * 1024, "max_record_bytes": 65536}
    if (capture.get("quote_admission") != "order_ready"
            or any(type(capture.get(name)) is not int or capture[name] != expected
                   for name, expected in resources.items())):
        raise ValueError("qualified H1 capture capacity required")
    if capture.get("selection_basis") != {"version": "selection-basis-v2", "max_candidates": 100, "max_source_terms": 16}:
        raise ValueError("qualified H1 selection profile required")
    gate = capture.get("entry_gate_trace")
    if (not isinstance(gate, Mapping) or gate.get("max_candidates") != 100
            or any(gate.get(key) != capture.get(key)
                   for key in ("source_version_ref", "configuration_ref"))):
        raise ValueError("H1 gate population/source/configuration binding required")
    if capture.get("frame_diagnostics") != {"version": "kis-frame-diagnostics-v1"}:
        raise ValueError("qualified H1 frame diagnostics required")
    channels = capture.get("channels")
    if not isinstance(channels, Mapping):
        raise ValueError("qualified H1 channel profile required")
    expected_channels = {
        "registration_cap": 41, "external_reserved": 0, "operational_headroom": 10,
        "max_candidates": 5, "lease_seconds": 5100, "evidence_ref": channels.get("evidence_ref"),
    }
    if (channels != expected_channels
            or any(type(channels[name]) is not int for name in expected_channels if name != "evidence_ref")
            or not _text(channels["evidence_ref"], "channels.evidence_ref")):
        raise ValueError("qualified H1 channel profile required")
    policy = PriceGatePolicy.from_dict(context.get("policy"))
    received = context.get("received_policy")
    if not isinstance(received, Mapping) or received.get("version") != "received-v1":
        raise ValueError("received policy required")
    if (policy.max_quote_age_seconds != Decimal(1) or policy.max_decision_delay_seconds != Decimal(5)
            or policy.entry_slippage_bps != 0 or policy.exit_slippage_bps != 0
            or _number(received.get("max_rest_rtt_seconds"), "received.max_rest_rtt_seconds", zero=True) != 3
            or _number(received.get("max_target_age_seconds"), "received.max_target_age_seconds", zero=True) != 30):
        raise ValueError("fixed H1 freshness policy required")
    markout = MarkoutPolicy.from_dict(context.get("markout_policy"))
    if markout.horizon_seconds != 900 or markout.max_delay_seconds != Decimal(5):
        raise ValueError("fixed H1 markout policy required")
    fees = _fees(context.get("fees"))
    if any(value <= 0 for value in (fees.config.buy_commission_rate, fees.config.sell_commission_rate,
                                    fees.config.sell_tax_rate)):
        raise ValueError("declared nonzero observed fees required")
    _regime(context)


def _capture_template_contract(context: Mapping[str, Any], first_day: str) -> None:
    capture = context.get("capture")
    expected = {"version", "study_ref", "start_at", "admission_end_at", "end_at", "scan_admission_ref",
                "source_version_ref", "configuration_ref", "fee_evidence_ref", "capital_evidence_ref",
                "capacity_evidence_ref", "journal_path", "buffer_capacity", "queue_capacity", "batch_size",
                "max_bytes", "max_record_bytes", "open_timeout_seconds", "close_timeout_seconds", "channels",
                "selection_basis", "entry_gate_trace", "frame_diagnostics", "max_scans", "quote_admission"}
    if not isinstance(capture, Mapping) or set(capture) != expected:
        raise ValueError("strict H1 capture fields required")
    for name in ("study_ref", "scan_admission_ref", "source_version_ref", "configuration_ref", "fee_evidence_ref",
                 "capital_evidence_ref", "capacity_evidence_ref"):
        if not isinstance(capture[name], str) or len(capture[name]) > 200 or not _text(capture[name], "capture." + name):
            raise ValueError("bounded capture reference required")
    for name in ("open_timeout_seconds", "close_timeout_seconds"):
        value = capture[name]
        if type(value) not in (int, float) or not math.isfinite(value) or not 0 < value <= 10:
            raise ValueError("bounded capture timeout required")
    for name, expected_time in (("start_at", time(9)), ("admission_end_at", time(10, 5)), ("end_at", time(10, 21))):
        value = _aware(capture[name], "capture." + name)
        if value.date().isoformat() != first_day or value.timetz().replace(tzinfo=None) != expected_time:
            raise ValueError("template must use exact first-date H1 schedule")
    if context.get("data_basis") != "received_snapshot" or context.get("outcome_basis") != "fixed_horizon_bid_markout":
        raise ValueError("received fixed-horizon H1 study required")
    if "opportunities" in context and (type(context["opportunities"]) is not list or context["opportunities"]):
        raise ValueError("template cannot contain prior opportunities")


def _day_time(day: str, hour: int, minute: int) -> str:
    return datetime.combine(_iso_date(day, "day"), time(hour, minute), tzinfo=KST).isoformat()


def _study(template: Mapping[str, Any], day: str, prepared_at: datetime, input_root: PurePath,
           state_root: PurePath) -> tuple[dict, dict]:
    context = deepcopy(dict(template))
    input_dir, state_dir = input_root / day, state_root / day
    capture = context["capture"]
    capture.update(start_at=_day_time(day, 9, 0), admission_end_at=_day_time(day, 10, 5),
                   end_at=_day_time(day, 10, 21), journal_path=str(state_dir / "engine.jsonl"))
    context["as_of"] = capture["end_at"]
    if not isinstance(context.get("capital_policy"), Mapping) or not isinstance(context.get("markout_policy"), Mapping):
        raise ValueError("capital and markout policies required")
    fixed = prepared_at.isoformat()
    context["capital_policy"] = dict(context["capital_policy"], fixed_at=fixed)
    context["markout_policy"] = dict(context["markout_policy"], fixed_at=fixed)
    _h1_contract(context)
    CapitalPolicy.from_dict(context["capital_policy"])
    validate_window_contract(context)
    build_report({**context, "opportunities": []})
    study_path = str(input_dir / "study.json")
    manifest_path = str(input_dir / "manifest.json")
    once_path = str(input_dir / "once.json")
    receipt_path = str(state_dir / "startup.receipt")
    raw = _canonical(context)
    digest = hashlib.sha256(raw.encode()).hexdigest()
    manifest = {"study_path": study_path, "study_sha256": digest}
    once = {
        "version": "entry-observation-once-v1", "manifest_path": manifest_path,
        "receipt_path": receipt_path, "not_before": _day_time(day, 8, 55),
        "latest_start_at": _day_time(day, 8, 57),
    }
    return dict(context=context, study_path=study_path, manifest_path=manifest_path, once_path=once_path,
                journal_path=str(state_dir / "engine.jsonl"), receipt_path=receipt_path, study=raw,
                manifest=_canonical(manifest), once=_canonical(once), policy_sha256=None), manifest


def _file(path: str, utf8: str) -> dict:
    return {"path": path, "utf8": utf8, "sha256": hashlib.sha256(utf8.encode()).hexdigest()}


def build_preparation(request, *, prepared_at: datetime) -> dict:
    """Return deterministic JSON payloads only; the caller materializes them later."""
    if not isinstance(request, Mapping) or set(request) != _REQUEST_FIELDS:
        raise ValueError("strict preparation request required")
    if not isinstance(prepared_at, datetime) or prepared_at.tzinfo is None or prepared_at.utcoffset() is None:
        raise ValueError("aware preparation time required")
    prepared_at = prepared_at.astimezone(timezone.utc)
    protocol_ref = _text(request["protocol_ref"], "protocol_ref")
    template = request["study_template"]
    if not isinstance(template, Mapping):
        raise ValueError("study_template object required")
    input_root, state_root = _root(request["input_root"], "input_root"), _root(request["state_root"], "state_root")
    if input_root == state_root or input_root in state_root.parents or state_root in input_root.parents:
        raise ValueError("input and state roots must not overlap")
    calendar, days = _dates(request["calendar"], prepared_at)
    _capture_template_contract(template, days[0])
    entries = [_study(template, day, prepared_at, input_root, state_root) for day in days]
    policy_hashes = []
    for entry, _ in entries:
        identity = _effective_policy(entry["context"], prepared_at, entry["context"]["as_of"][:10],
                                     _aware(entry["context"]["as_of"], "study.as_of"))
        entry["policy_sha256"] = hashlib.sha256(identity.encode()).hexdigest()
        policy_hashes.append(entry["policy_sha256"])
    if len(set(policy_hashes)) != 1:
        raise ValueError("all H1 study policies must normalize to one fingerprint")
    context = entries[0][0]["context"]
    protocol = {
        "version": "h1-multiday-protocol-v1", "protocol_ref": protocol_ref,
        "dataset_kind": context["dataset_kind"], "fixed_at": prepared_at.isoformat(),
        "evaluation_epoch": context["evaluation_epoch"], "calendar": calendar,
        "study_policy_sha256": policy_hashes[0], "capital_model": "bounded-shadow-sleeve-v1",
        "seed_rule": "first_admitted_nonnegative_capacity", "pending_reservation_multiplier": "1.015",
        "min_net_rr": "1.5", "slippage_bps_each": [0, 10, 30],
    }
    _protocol(protocol)
    planned_days, files = [], []
    for entry, _ in entries:
        planned_days.append({key: entry[key] for key in ("study_path", "manifest_path", "once_path", "journal_path",
                             "receipt_path", "policy_sha256")} | {
                                 "date": entry["context"]["as_of"][:10], "start_at": entry["context"]["capture"]["start_at"],
                                 "admission_end_at": entry["context"]["capture"]["admission_end_at"], "end_at": entry["context"]["as_of"],
                                 "not_before": json.loads(entry["once"])["not_before"],
                                 "latest_start_at": json.loads(entry["once"])["latest_start_at"],
                             })
        files.extend([_file(entry["study_path"], entry["study"]), _file(entry["manifest_path"], entry["manifest"]),
                      _file(entry["once_path"], entry["once"])])
    return {
        "status": "preparation_only", "installation_ready": False, "source_authenticity_verified": False,
        "proposed_protocol": protocol, "planned_days": planned_days, "files": files,
        "evaluation_manifest": {"protocol": deepcopy(protocol), "days": [], "as_of": prepared_at.isoformat()},
        "unresolved_external_checks": list(_UNRESOLVED_CHECKS),
    }
