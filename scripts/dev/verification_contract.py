"""Strict, offline-only verification receipt contract (v1)."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any


class EvidenceError(ValueError):
    """A bounded document violated the offline evidence contract."""


_MAX_DOCUMENT_BYTES = 32 * 1024 * 1024
_MAX_DEPTH = 12
_MAX_NODES = 20_000
_MAX_NODEID_BYTES = 2_048
_MAX_INTEGER_DIGITS = 128
_HEX40 = re.compile(r"[0-9a-f]{40}\Z")
_HEX64 = re.compile(r"[0-9a-f]{64}\Z")
_RUN_ID = re.compile(r"[A-Za-z0-9_.:-]{1,128}\Z")
_EVENTS = {"local", "pull_request", "push", "merge_group", "workflow_dispatch"}
_LANES = {"standard", "source-proof"}
_TIMEZONES = {"UTC", "Asia/Seoul"}
_PHASES = {"passed", "failed", "skipped", "xfailed", "xpassed", "not_run"}
_RECEIPT_SCHEMA = "qwq.verification-receipt/v1"
_EXPECTATION_SCHEMA = "qwq.verification-expectation/v1"
_DECISION_SCHEMA = "qwq.verification-decision/v1"


def parse_document(raw: bytes, *, kind: str) -> dict:
    """Decode and validate one bounded receipt or expectation document."""
    if type(raw) is not bytes:
        raise EvidenceError("RAW_NOT_BYTES")
    if len(raw) > _MAX_DOCUMENT_BYTES:
        raise EvidenceError("DOCUMENT_TOO_LARGE")
    if kind not in {"receipt", "expectation"}:
        raise EvidenceError("INVALID_KIND")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise EvidenceError("INVALID_UTF8") from exc
    try:
        value = json.loads(
            text,
            object_pairs_hook=_no_duplicate_object,
            parse_constant=_reject_nonfinite,
            parse_int=_bounded_integer,
        )
    except EvidenceError:
        raise
    except (json.JSONDecodeError, RecursionError, ValueError) as exc:
        raise EvidenceError("INVALID_JSON") from exc
    if not isinstance(value, dict):
        raise EvidenceError("DOCUMENT_NOT_OBJECT")
    try:
        if _depth(value) > _MAX_DEPTH:
            raise EvidenceError("DOCUMENT_TOO_DEEP")
        if kind == "receipt":
            _validate_receipt_document(value)
        else:
            _validate_expectation_document(value)
    except EvidenceError:
        raise
    except (RecursionError, UnicodeError, ValueError, TypeError) as exc:
        raise EvidenceError("INVALID_DOCUMENT") from exc
    return value


def validate_receipt(receipt: dict, expected: dict) -> tuple[str, ...]:
    """Return fixed reason codes for a receipt against a complete expectation."""
    errors: set[str] = set()
    try:
        _validate_expectation_document(expected)
    except EvidenceError:
        errors.add("INVALID_EXPECTATION")
    try:
        _validate_receipt_document(receipt)
    except EvidenceError:
        errors.add("INVALID_RECEIPT")
    if errors:
        return tuple(sorted(errors))

    target = _find_slot(expected, receipt["slot"])
    if target is None:
        errors.add("SLOT_MISMATCH")
        return tuple(sorted(errors))
    if receipt["run"] != expected["run"]:
        errors.add("RUN_MISMATCH")
    if receipt["identity"] != target["identity"]:
        errors.add("IDENTITY_MISMATCH")
    if _canonical_nodes(receipt["collected"]) != _canonical_nodes(target["nodes"]):
        errors.add("NODE_SET_MISMATCH")
    if receipt["guard"] != target["guard"]:
        errors.add("GUARD_MISMATCH")

    session = receipt["session"]
    if session["finished"] is not True:
        errors.add("SESSION_UNFINISHED")
    if session["exit_code"] != 0:
        errors.add("SESSION_EXIT_NONZERO")
    if session["collection_errors"] != 0:
        errors.add("COLLECTION_ERRORS")
    if session["deselected"] != 0:
        errors.add("DESELECTED")

    for result in receipt["results"]:
        if result["setup"] != "passed":
            errors.add(_phase_error("setup", result["setup"]))
        if result["call"] != "passed":
            errors.add(_phase_error("call", result["call"]))
        if result["teardown"] != "passed":
            errors.add(_phase_error("teardown", result["teardown"]))
    return tuple(sorted(errors))


def evaluate_bundle(receipts: list[dict], expected: dict) -> dict:
    """Decide whether exactly the four expected offline receipts agree."""
    errors: set[str] = set()
    if not isinstance(receipts, list):
        errors.add("INVALID_RECEIPT_BUNDLE")
        receipts = []
    try:
        _validate_expectation_document(expected)
    except EvidenceError:
        errors.add("INVALID_EXPECTATION")
        expected_slots: list[dict] = []
    else:
        expected_slots = expected["slots"]

    seen_slots: set[tuple[str, str]] = set()
    for receipt in receipts:
        validation = validate_receipt(receipt, expected)
        errors.update(validation)
        if isinstance(receipt, dict) and isinstance(receipt.get("slot"), dict):
            lane = receipt["slot"].get("lane")
            timezone = receipt["slot"].get("timezone")
            if isinstance(lane, str) and isinstance(timezone, str):
                key = (lane, timezone)
                if key in seen_slots:
                    errors.add("DUPLICATE_SLOT")
                seen_slots.add(key)

    required_slots = {(item["slot"]["lane"], item["slot"]["timezone"]) for item in expected_slots}
    if expected_slots:
        if len(receipts) != 4:
            errors.add("SLOT_COUNT_MISMATCH")
        if seen_slots != required_slots:
            errors.add("SLOT_SET_MISMATCH")
    ordered_errors = sorted(errors)
    return {
        "schema": _DECISION_SCHEMA,
        "status": "EVIDENCE_CONSISTENT" if not ordered_errors else "REJECTED",
        "errors": ordered_errors,
        "scope": "offline_evidence_only",
        "production_eligible": False,
    }


def _no_duplicate_object(pairs: list[tuple[str, Any]]) -> dict:
    document: dict[str, Any] = {}
    for key, value in pairs:
        if key in document:
            raise EvidenceError("DUPLICATE_KEY")
        document[key] = value
    return document


def _reject_nonfinite(_: str) -> None:
    raise EvidenceError("NONFINITE_NUMBER")


def _bounded_integer(value: str) -> int:
    digits = value[1:] if value.startswith("-") else value
    if len(digits) > _MAX_INTEGER_DIGITS:
        raise EvidenceError("INTEGER_TOO_LARGE")
    return int(value)


def _depth(value: Any) -> int:
    deepest = 0
    pending = [(value, 1)]
    while pending:
        item, depth = pending.pop()
        if isinstance(item, (dict, list)):
            deepest = max(deepest, depth)
            if deepest > _MAX_DEPTH:
                return deepest
            children = item.values() if isinstance(item, dict) else item
            pending.extend((child, depth + 1) for child in children)
    return deepest


def _validate_receipt_document(receipt: Any) -> None:
    _require_exact_dict(receipt, {"schema", "run", "slot", "identity", "collected", "results", "session", "guard"})
    _require_equal(receipt["schema"], _RECEIPT_SCHEMA)
    _validate_run(receipt["run"])
    _validate_slot(receipt["slot"])
    _validate_identity(receipt["identity"])
    _validate_nodes(receipt["collected"])
    _validate_results(receipt["results"], receipt["collected"])
    _validate_session(receipt["session"])
    _validate_guard(receipt["guard"])
    if receipt["identity"]["inventory"] != _inventory(receipt["collected"]):
        raise EvidenceError("INVENTORY_MISMATCH")


def _validate_expectation_document(expected: Any) -> None:
    _require_exact_dict(expected, {"schema", "run", "slots"})
    _require_equal(expected["schema"], _EXPECTATION_SCHEMA)
    _validate_run(expected["run"])
    slots = expected["slots"]
    if not isinstance(slots, list) or len(slots) != 4:
        raise EvidenceError("INVALID_SLOTS")
    seen: set[tuple[str, str]] = set()
    for item in slots:
        _require_exact_dict(item, {"slot", "identity", "nodes", "allowed_outcomes", "guard"})
        _validate_slot(item["slot"])
        key = (item["slot"]["lane"], item["slot"]["timezone"])
        if key in seen:
            raise EvidenceError("DUPLICATE_SLOT")
        seen.add(key)
        _validate_identity(item["identity"])
        _validate_nodes(item["nodes"])
        if item["identity"]["inventory"] != _inventory(item["nodes"]):
            raise EvidenceError("INVENTORY_MISMATCH")
        if type(item["allowed_outcomes"]) is not dict or item["allowed_outcomes"]:
            raise EvidenceError("INVALID_ALLOWED_OUTCOMES")
        _validate_guard(item["guard"])
        if item["guard"]["module_count"] != 1 or item["guard"]["violations"] != 0:
            raise EvidenceError("INVALID_EXPECTED_GUARD")
    if seen != {(lane, timezone) for lane in _LANES for timezone in _TIMEZONES}:
        raise EvidenceError("INVALID_SLOT_SET")


def _validate_run(run: Any) -> None:
    _require_exact_dict(run, {"event", "sha", "tree", "contract", "run_id", "attempt"})
    if not isinstance(run["event"], str) or run["event"] not in _EVENTS:
        raise EvidenceError("INVALID_EVENT")
    _require_hex(run["sha"], 40)
    _require_hex(run["tree"], 40)
    _require_hex(run["contract"], 64)
    if not isinstance(run["run_id"], str) or not _RUN_ID.fullmatch(run["run_id"]):
        raise EvidenceError("INVALID_RUN_ID")
    if type(run["attempt"]) is not int or run["attempt"] < 1:
        raise EvidenceError("INVALID_ATTEMPT")


def _validate_slot(slot: Any) -> None:
    _require_exact_dict(slot, {"lane", "timezone"})
    if (
        not isinstance(slot["lane"], str)
        or not isinstance(slot["timezone"], str)
        or slot["lane"] not in _LANES
        or slot["timezone"] not in _TIMEZONES
    ):
        raise EvidenceError("INVALID_SLOT")


def _validate_identity(identity: Any) -> None:
    _require_exact_dict(identity, {"runtime", "producer", "inventory"})
    for value in identity.values():
        _require_hex(value, 64)


def _validate_nodes(nodes: Any) -> None:
    if not isinstance(nodes, list) or not 1 <= len(nodes) <= _MAX_NODES:
        raise EvidenceError("INVALID_NODES")
    _canonical_nodes(nodes)


def _validate_results(results: Any, collected: list[str]) -> None:
    if not isinstance(results, list) or len(results) > _MAX_NODES:
        raise EvidenceError("INVALID_RESULTS")
    result_nodes: list[str] = []
    for result in results:
        _require_exact_dict(result, {"nodeid", "setup", "call", "teardown"})
        _validate_nodeid(result["nodeid"])
        result_nodes.append(result["nodeid"])
        for phase in ("setup", "call", "teardown"):
            if not isinstance(result[phase], str) or result[phase] not in _PHASES:
                raise EvidenceError("INVALID_PHASE")
    if _canonical_nodes(result_nodes) != _canonical_nodes(collected):
        raise EvidenceError("RESULT_NODE_MISMATCH")


def _validate_session(session: Any) -> None:
    _require_exact_dict(session, {"finished", "exit_code", "collection_errors", "deselected"})
    if type(session["finished"]) is not bool:
        raise EvidenceError("INVALID_FINISHED")
    exit_code = session["exit_code"]
    if exit_code is not None and type(exit_code) is not int:
        raise EvidenceError("INVALID_EXIT_CODE")
    for key in ("collection_errors", "deselected"):
        if type(session[key]) is not int or session[key] < 0:
            raise EvidenceError("INVALID_SESSION_COUNT")


def _validate_guard(guard: Any) -> None:
    _require_exact_dict(guard, {"path", "sha256", "module_count", "violations"})
    path = guard["path"]
    if not isinstance(path, str) or not path or path.startswith("/") or ".." in path.split("/"):
        raise EvidenceError("INVALID_GUARD_PATH")
    try:
        path.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise EvidenceError("INVALID_GUARD_PATH") from exc
    _require_hex(guard["sha256"], 64)
    for key in ("module_count", "violations"):
        if type(guard[key]) is not int or guard[key] < 0:
            raise EvidenceError("INVALID_GUARD_COUNT")


def _require_exact_dict(value: Any, keys: set[str]) -> None:
    if type(value) is not dict or set(value) != keys:
        raise EvidenceError("INVALID_FIELDS")


def _require_equal(value: Any, expected: str) -> None:
    if value != expected:
        raise EvidenceError("INVALID_SCHEMA")


def _require_hex(value: Any, length: int) -> None:
    matcher = _HEX40 if length == 40 else _HEX64
    if not isinstance(value, str) or not matcher.fullmatch(value):
        raise EvidenceError("INVALID_HASH")


def _validate_nodeid(value: Any) -> None:
    if not isinstance(value, str) or not value:
        raise EvidenceError("INVALID_NODEID")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise EvidenceError("INVALID_NODEID") from exc
    if len(encoded) > _MAX_NODEID_BYTES:
        raise EvidenceError("INVALID_NODEID")


def _canonical_nodes(nodes: list[str]) -> list[str]:
    canonical: list[str] = []
    for node in nodes:
        _validate_nodeid(node)
        canonical.append(node)
    canonical.sort()
    if len(set(canonical)) != len(canonical):
        raise EvidenceError("DUPLICATE_NODE")
    return canonical


def _inventory(nodes: list[str]) -> str:
    encoded = json.dumps(
        _canonical_nodes(nodes), ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _find_slot(expected: dict, slot: dict) -> dict | None:
    for item in expected["slots"]:
        if item["slot"] == slot:
            return item
    return None


def _phase_error(phase: str, value: str) -> str:
    if value in {"skipped", "xfailed", "xpassed", "not_run"}:
        return "UNSUPPORTED_OUTCOME"
    return f"{phase.upper()}_FAILED"
