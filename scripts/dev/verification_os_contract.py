"""로컬 OS process-result와 기존 receipt를 순수하게 결속하는 계약이다."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from scripts.dev.verification_contract import parse_document, validate_receipt


class ProcessEvidenceError(ValueError):
    """엄격한 process-result 문서가 계약을 위반했다."""


_MAX_BYTES = 64 * 1024
_MAX_DEPTH = 8
_MAX_INTEGER_DIGITS = 128
_MAX_RECEIPT_BYTES = 32 * 1024 * 1024
_MAX_STREAM_BYTES = 8 * 1024 * 1024
_HEX64 = re.compile(r"[0-9a-f]{64}\Z")
_PROCESS_SCHEMA = "qwq.verification-process-result/v1"
_DECISION_SCHEMA = "qwq.verification-os-decision/v1"
_SCOPE = "local_os_process_only"
_REASONS = {
    "exited",
    "signaled",
    "timeout",
    "interrupted",
    "startup_error",
    "output_limit",
    "cleanup_error",
    "identity_changed",
    "io_error",
}
_PROCESS_KEYS = {"reason", "returncode", "term_sent", "kill_sent", "leader_reaped",
                 "descendant_survived", "cleanup_complete", "descendants_reaped",
                 "ownership_probe_passed", "guard", "parent_guard"}


def parse_process_result(raw: bytes) -> dict:
    """64KiB process-result을 해석하고 세부 ProcessEvidenceError를 낸다."""
    if type(raw) is not bytes:
        raise ProcessEvidenceError("RAW_NOT_BYTES")
    if len(raw) > _MAX_BYTES:
        raise ProcessEvidenceError("DOCUMENT_TOO_LARGE")
    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_no_duplicate_object,
            parse_constant=_reject_nonfinite,
            parse_int=_bounded_integer,
        )
    except ProcessEvidenceError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError) as exc:
        raise ProcessEvidenceError("INVALID_JSON") from exc
    try:
        if type(value) is not dict:
            raise ProcessEvidenceError("DOCUMENT_NOT_OBJECT")
        if _depth(value) > _MAX_DEPTH:
            raise ProcessEvidenceError("DOCUMENT_TOO_DEEP")
        _validate_unicode(value)
        _validate_process_result(value)
    except ProcessEvidenceError:
        raise
    except (RecursionError, TypeError, ValueError) as exc:
        raise ProcessEvidenceError("INVALID_DOCUMENT") from exc
    return value


def validate_controlled_receipt(
    receipt_raw: bytes, process_raw: bytes, expected: dict
) -> tuple[str, ...]:
    """한 슬롯을 대조하며 process parser 예외는 고정 오류로 정규화한다."""
    errors: set[str] = set()
    try:
        verification, process_identity, launch = _validate_expected(expected)
    except ProcessEvidenceError:
        errors.add("INVALID_EXPECTATION")
        verification = process_identity = launch = None
    try:
        receipt = parse_document(receipt_raw, kind="receipt")
    except Exception:
        errors.add("INVALID_RECEIPT")
        receipt = None
    try:
        process = parse_process_result(process_raw)
    except ProcessEvidenceError:
        errors.add("INVALID_PROCESS_RESULT")
        process = None
    if errors:
        return tuple(sorted(errors))

    assert isinstance(verification, dict)
    assert isinstance(process_identity, dict)
    assert isinstance(launch, dict)
    assert isinstance(receipt, dict)
    assert isinstance(process, dict)
    errors.update(validate_receipt(receipt, verification))

    if process["run"] != receipt["run"]:
        errors.add("PROCESS_RUN_MISMATCH")
    if process["slot"] != receipt["slot"]:
        errors.add("PROCESS_SLOT_MISMATCH")
    if process["identity"] != process_identity:
        errors.add("PROCESS_IDENTITY_MISMATCH")
    if process["launch"] != launch:
        errors.add("PROCESS_LAUNCH_MISMATCH")
    if (
        receipt["run"]["event"] != "local"
        or receipt["slot"]["lane"] != "standard"
        or process["slot"]["lane"] != "standard"
    ):
        errors.add("PROCESS_SCOPE_MISMATCH")

    observed = process["process"]
    if process["identity"]["producer"] != receipt["identity"]["producer"]:
        errors.add("PROCESS_IDENTITY_MISMATCH")
    if process["identity"]["guard"] != receipt["guard"]["sha256"]:
        errors.add("PROCESS_GUARD_MISMATCH")
    if observed["guard"] != receipt["guard"] or observed["parent_guard"] != receipt["guard"]:
        errors.add("PROCESS_GUARD_MISMATCH")
    receipt_fact = process["receipt"]
    if (
        receipt_fact["state"] != "regular"
        or receipt_fact["bytes"] != len(receipt_raw)
        or receipt_fact["sha256"] != hashlib.sha256(receipt_raw).hexdigest()
    ):
        errors.add("PROCESS_RECEIPT_MISMATCH")

    if (
        observed["reason"] != "exited"
        or observed["returncode"] != 0
        or observed["term_sent"]
        or observed["kill_sent"]
    ):
        errors.add("PROCESS_EXIT_REJECTED")
    if not observed["leader_reaped"] or not observed["cleanup_complete"]:
        errors.add("PROCESS_CLEANUP_INCOMPLETE")
    if not observed["ownership_probe_passed"]:
        errors.add("PROCESS_OWNERSHIP_UNPROVEN")
    if observed["descendant_survived"]:
        errors.add("PROCESS_DESCENDANT_SURVIVED")
    if process["streams"]["stdout"]["overflow"] or process["streams"]["stderr"]["overflow"]:
        errors.add("PROCESS_STREAM_OVERFLOW")
    return tuple(sorted(errors))


def evaluate_controlled_slot(receipt_raw: bytes, process_raw: bytes, expected: dict) -> dict:
    """한 슬롯의 OS 결과를 비자격 로컬 결정으로만 반환한다."""
    errors = list(validate_controlled_receipt(receipt_raw, process_raw, expected))
    return {
        "schema": _DECISION_SCHEMA,
        "status": "OS_RESULT_BOUND" if not errors else "REJECTED",
        "errors": errors,
        "scope": _SCOPE,
        "native_qualified": False,
        "ci_provenance_verified": False,
        "production_eligible": False,
    }


def _validate_expected(expected: Any) -> tuple[dict, dict, dict]:
    _require_exact_dict(expected, {"verification", "process_identity", "launch"})
    verification_raw = _canonical_json(expected["verification"])
    try:
        verification = parse_document(verification_raw, kind="expectation")
    except Exception as exc:
        raise ProcessEvidenceError("INVALID_EXPECTATION") from exc
    identity = expected["process_identity"]
    _require_exact_dict(identity, {"controller", "bootstrap", "producer", "guard", "executable"})
    for value in identity.values():
        _require_hash(value)
    launch = expected["launch"]
    _validate_launch(launch)
    return verification, identity, launch


def _validate_process_result(value: dict) -> None:
    _require_exact_dict(value, {"schema", "run", "slot", "scope", "identity", "launch", "process", "streams", "receipt"})
    if value["schema"] != _PROCESS_SCHEMA or value["scope"] != _SCOPE:
        raise ProcessEvidenceError("INVALID_SCHEMA")
    _validate_run_slot(value["run"], value["slot"])
    _require_exact_dict(value["identity"], {"controller", "bootstrap", "producer", "guard", "executable"})
    for item in value["identity"].values():
        _require_hash(item)
    _validate_launch(value["launch"])
    _validate_process(value["process"])
    _validate_streams(value["streams"])
    _validate_receipt_fact(value["receipt"])


def _validate_run_slot(run: Any, slot: Any) -> None:
    """공개 receipt parser만 이용해 기존 run/slot 형식을 재사용한다."""
    nodes = ["node"]
    inventory = hashlib.sha256(b'["node"]').hexdigest()
    sample = {
        "schema": "qwq.verification-receipt/v1",
        "run": run,
        "slot": slot,
        "identity": {"runtime": "0" * 64, "producer": "0" * 64, "inventory": inventory},
        "collected": nodes,
        "results": [{"nodeid": "node", "setup": "passed", "call": "passed", "teardown": "passed"}],
        "session": {"finished": True, "exit_code": 0, "collection_errors": 0, "deselected": 0},
        "guard": {"path": "tests/conftest.py", "sha256": "0" * 64, "module_count": 1, "violations": 0},
    }
    try:
        parse_document(_canonical_json(sample), kind="receipt")
    except Exception as exc:
        raise ProcessEvidenceError("INVALID_RUN_OR_SLOT") from exc


def _validate_launch(launch: Any) -> None:
    _require_exact_dict(launch, {"profile", "process_scope", "timeout_seconds", "selection_sha256"})
    if type(launch["profile"]) is not str or launch["profile"] != "pytest-evidence-bootstrap/v1":
        raise ProcessEvidenceError("INVALID_LAUNCH")
    if type(launch["process_scope"]) is not str or launch["process_scope"] != "linux-subreaper/v1":
        raise ProcessEvidenceError("INVALID_LAUNCH")
    if type(launch["timeout_seconds"]) is not int or not 1 <= launch["timeout_seconds"] <= 900:
        raise ProcessEvidenceError("INVALID_LAUNCH")
    _require_hash(launch["selection_sha256"])


def _validate_process(process: Any, *, b1: bool = False) -> None:
    _require_exact_dict(process, _PROCESS_KEYS)
    reasons = _REASONS | {"lock_timeout"} if b1 else _REASONS
    if type(process["reason"]) is not str or process["reason"] not in reasons:
        raise ProcessEvidenceError("INVALID_REASON")
    returncode = process["returncode"]
    if returncode is not None and (type(returncode) is not int or not -64 <= returncode <= 255):
        raise ProcessEvidenceError("INVALID_RETURN_CODE")
    for key in ("term_sent", "kill_sent", "leader_reaped", "descendant_survived", "cleanup_complete", "ownership_probe_passed"):
        if type(process[key]) is not bool:
            raise ProcessEvidenceError("INVALID_PROCESS_BOOLEAN")
    if type(process["descendants_reaped"]) is not int or not 0 <= process["descendants_reaped"] <= 20_000:
        raise ProcessEvidenceError("INVALID_DESCENDANT_COUNT")
    if process["leader_reaped"] and type(returncode) is not int:
        raise ProcessEvidenceError("MISSING_LEADER_RETURN_CODE")
    if not process["leader_reaped"] and returncode is not None:
        raise ProcessEvidenceError("UNEXPECTED_LEADER_RETURN_CODE")
    _validate_nullable_guard(process["guard"])
    _validate_nullable_guard(process["parent_guard"])


def _validate_nullable_guard(value: Any) -> None:
    if value is None:
        return
    _require_exact_dict(value, {"path", "sha256", "module_count", "violations"})
    if type(value["path"]) is not str or not value["path"] or value["path"].startswith("/") or ".." in value["path"].split("/"):
        raise ProcessEvidenceError("INVALID_GUARD")
    _require_hash(value["sha256"])
    for key in ("module_count", "violations"):
        if type(value[key]) is not int or value[key] < 0:
            raise ProcessEvidenceError("INVALID_GUARD")


def _validate_streams(streams: Any) -> None:
    _require_exact_dict(streams, {"stdout", "stderr"})
    for stream in streams.values():
        _require_exact_dict(stream, {"bytes", "sha256", "overflow"})
        if type(stream["bytes"]) is not int or not 0 <= stream["bytes"] <= _MAX_STREAM_BYTES + 1:
            raise ProcessEvidenceError("INVALID_STREAM_BYTES")
        _require_hash(stream["sha256"])
        if type(stream["overflow"]) is not bool:
            raise ProcessEvidenceError("INVALID_STREAM_OVERFLOW")
        if stream["bytes"] > _MAX_STREAM_BYTES and not stream["overflow"]:
            raise ProcessEvidenceError("MISSING_STREAM_OVERFLOW")


def _validate_receipt_fact(receipt: Any) -> None:
    _require_exact_dict(receipt, {"state", "bytes", "sha256"})
    if receipt["state"] not in {"missing", "regular", "invalid"}:
        raise ProcessEvidenceError("INVALID_RECEIPT_STATE")
    if type(receipt["bytes"]) is not int or not 0 <= receipt["bytes"] <= _MAX_RECEIPT_BYTES:
        raise ProcessEvidenceError("INVALID_RECEIPT_BYTES")
    if receipt["state"] == "regular":
        _require_hash(receipt["sha256"])
    elif receipt["bytes"] != 0 or receipt["sha256"] is not None:
        raise ProcessEvidenceError("INVALID_RECEIPT_FACT")


def _no_duplicate_object(pairs: list[tuple[str, Any]]) -> dict:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ProcessEvidenceError("DUPLICATE_KEY")
        result[key] = value
    return result


def _reject_nonfinite(_: str) -> None:
    raise ProcessEvidenceError("NONFINITE_NUMBER")


def _bounded_integer(value: str) -> int:
    digits = value[1:] if value.startswith("-") else value
    if len(digits) > _MAX_INTEGER_DIGITS:
        raise ProcessEvidenceError("INTEGER_TOO_LARGE")
    return int(value)


def _depth(value: Any) -> int:
    deepest = 0
    pending = [(value, 1)]
    while pending:
        item, depth = pending.pop()
        deepest = max(deepest, depth)
        if deepest > _MAX_DEPTH:
            return deepest
        if type(item) is dict:
            pending.extend((child, depth + 1) for child in item.values())
        elif type(item) is list:
            pending.extend((child, depth + 1) for child in item)
    return deepest


def _validate_unicode(value: Any) -> None:
    if type(value) is str:
        try:
            value.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise ProcessEvidenceError("INVALID_STRING") from exc
    elif type(value) is dict:
        for key, item in value.items():
            _validate_unicode(key)
            _validate_unicode(item)
    elif type(value) is list:
        for item in value:
            _validate_unicode(item)


def _canonical_json(value: Any) -> bytes:
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (RecursionError, TypeError, ValueError, UnicodeEncodeError) as exc:
        raise ProcessEvidenceError("INVALID_JSON_VALUE") from exc


def _require_exact_dict(value: Any, keys: set[str]) -> None:
    if type(value) is not dict or set(value) != keys:
        raise ProcessEvidenceError("INVALID_FIELDS")


def _require_hash(value: Any) -> None:
    if type(value) is not str or not _HEX64.fullmatch(value):
        raise ProcessEvidenceError("INVALID_HASH")


_B1_LAUNCH_LITERALS = {
    "profile": "b1-standard/v1",
    "process_scope": "linux-subreaper/v1",
    "timeout_seconds": 900,
    "budget_profile": "inclusive-lock-cleanup-publication/v1",
    "retained_stream_bytes": 2097152,
    "overflow_observed_bytes": 2097153,
    "pytest_profile": "b1-x-fixed-plugins/v1",
    "environment_profile": "b1-env-i-fake-key/v1",
    "selection_profile": "tests-path-only/v1",
    "lock_profile": "owner-ticket-workload/v1",
}
_B1_COORDINATION_BOOLEANS = {
    "lock_acquired", "lock_identity_stable", "fake_key_absent_before",
    "fake_key_absent_after", "fake_directory_removed",
}


def parse_b1_process_result(raw: bytes) -> dict:
    """v1과 섞이지 않는 strict v2 구조를 읽되 실패 사실도 보존한다."""
    if type(raw) is not bytes:
        raise ProcessEvidenceError("RAW_NOT_BYTES")
    if len(raw) > _MAX_BYTES:
        raise ProcessEvidenceError("DOCUMENT_TOO_LARGE")
    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_no_duplicate_object,
            parse_constant=_reject_nonfinite,
            parse_int=_bounded_integer,
        )
    except ProcessEvidenceError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError) as exc:
        raise ProcessEvidenceError("INVALID_JSON") from exc
    try:
        if type(value) is not dict:
            raise ProcessEvidenceError("DOCUMENT_NOT_OBJECT")
        if _depth(value) > _MAX_DEPTH:
            raise ProcessEvidenceError("DOCUMENT_TOO_DEEP")
        _validate_unicode(value)
        _validate_b1_process_result(value)
    except ProcessEvidenceError:
        raise
    except (RecursionError, TypeError, ValueError) as exc:
        raise ProcessEvidenceError("INVALID_DOCUMENT") from exc
    return value


def validate_b1_process_receipt(
    receipt_raw: bytes, process_raw: bytes, expected: dict, invocation: dict
) -> tuple[str, ...]:
    """프로세스 결속만 검사한다. 원시 호출/시계 출처와 로그는 호출자가 확인한다."""
    errors: set[str] = set()
    try:
        verification, process_identity, launch = _validate_b1_expected(expected)
    except ProcessEvidenceError:
        errors.add("INVALID_EXPECTATION")
        verification = process_identity = launch = None
    try:
        receipt = parse_document(receipt_raw, kind="receipt")
    except Exception:
        errors.add("INVALID_RECEIPT")
        receipt = None
    try:
        process = parse_b1_process_result(process_raw)
    except ProcessEvidenceError:
        errors.add("INVALID_PROCESS_RESULT")
        process = None
    try:
        _validate_b1_invocation(invocation)
    except ProcessEvidenceError:
        errors.add("INVALID_INVOCATION")
    else:
        if invocation["controller_returncode"] != 0:
            errors.add("B1_CONTROLLER_EXIT_REJECTED")
        if invocation["elapsed_ns"] > 900000000000:
            errors.add("B1_TOTAL_BUDGET_EXCEEDED")
    if verification is None or process is None or receipt is None:
        return tuple(sorted(errors))

    # strict expectation은 두 lane×두 timezone을 모두 요구하므로 슬롯은 반드시 있다.
    # v1의 phase 허용 판정을 호출하거나 그 오류를 제거하지 않는다.
    target = next(item for item in verification["slots"] if item["slot"] == receipt["slot"])
    if receipt["run"] != verification["run"]:
        errors.add("RUN_MISMATCH")
    if receipt["identity"] != target["identity"]:
        errors.add("IDENTITY_MISMATCH")
    if sorted(receipt["collected"]) != sorted(target["nodes"]):
        errors.add("NODE_SET_MISMATCH")
    if receipt["guard"] != target["guard"]:
        errors.add("GUARD_MISMATCH")
    session = receipt["session"]
    if not session["finished"]:
        errors.add("SESSION_UNFINISHED")
    if session["exit_code"] != 0:
        errors.add("SESSION_EXIT_NONZERO")
    if session["collection_errors"] != 0:
        errors.add("COLLECTION_ERRORS")
    if session["deselected"] != 0:
        errors.add("DESELECTED")

    if process["run"] != receipt["run"]:
        errors.add("PROCESS_RUN_MISMATCH")
    if process["slot"] != receipt["slot"]:
        errors.add("PROCESS_SLOT_MISMATCH")
    if process["identity"] != process_identity:
        errors.add("PROCESS_IDENTITY_MISMATCH")
    if process["launch"] != launch:
        errors.add("PROCESS_LAUNCH_MISMATCH")
    if (
        receipt["run"]["event"] != "local"
        or process["run"]["event"] != "local"
        or receipt["slot"]["lane"] != "standard"
        or process["slot"]["lane"] != "standard"
    ):
        errors.add("PROCESS_SCOPE_MISMATCH")
    observed = process["process"]
    if process["identity"]["producer"] != receipt["identity"]["producer"]:
        errors.add("PROCESS_IDENTITY_MISMATCH")
    if process["identity"]["guard"] != receipt["guard"]["sha256"]:
        errors.add("PROCESS_GUARD_MISMATCH")
    if observed["guard"] != receipt["guard"] or observed["parent_guard"] != receipt["guard"]:
        errors.add("PROCESS_GUARD_MISMATCH")
    receipt_fact = process["receipt"]
    if (
        receipt_fact["state"] != "regular"
        or receipt_fact["bytes"] != len(receipt_raw)
        or receipt_fact["sha256"] != hashlib.sha256(receipt_raw).hexdigest()
    ):
        errors.add("PROCESS_RECEIPT_MISMATCH")
    if (
        observed["reason"] != "exited"
        or observed["returncode"] != 0
        or observed["term_sent"]
        or observed["kill_sent"]
    ):
        errors.add("PROCESS_EXIT_REJECTED")
    if not observed["leader_reaped"] or not observed["cleanup_complete"]:
        errors.add("PROCESS_CLEANUP_INCOMPLETE")
    if not observed["ownership_probe_passed"]:
        errors.add("PROCESS_OWNERSHIP_UNPROVEN")
    if observed["descendant_survived"]:
        errors.add("PROCESS_DESCENDANT_SURVIVED")
    for stream in process["streams"].values():
        if stream["overflow"]:
            errors.add("PROCESS_STREAM_OVERFLOW")
        elif stream["bytes"] != stream["observed_bytes"]:
            # 관측했으나 저장하지 못한 prefix는 성공 증거가 아니다.
            errors.add("PROCESS_EXIT_REJECTED")
    coordination = process["coordination"]
    if (
        not all(coordination[key] for key in _B1_COORDINATION_BOOLEANS)
        or coordination["fake_key_path_sha256"] is None
    ):
        errors.add("B1_COORDINATION_UNPROVEN")
    return tuple(sorted(errors))


def evaluate_b1_process_slot(
    receipt_raw: bytes, process_raw: bytes, expected: dict, invocation: dict
) -> dict:
    """B1 process-only 결정은 시험 결과 허용/native/CI/운영 자격을 부여하지 않는다."""
    errors = list(validate_b1_process_receipt(receipt_raw, process_raw, expected, invocation))
    return {
        "schema": "qwq.b1-process-decision/v1",
        "status": "B1_PROCESS_BOUND" if not errors else "REJECTED",
        "errors": errors,
        "scope": "local_b1_process_only",
        "outcomes_accepted": False,
        "native_qualified": False,
        "ci_provenance_verified": False,
        "production_eligible": False,
    }


def _validate_b1_expected(expected: Any) -> tuple[dict, dict, dict]:
    _require_exact_dict(expected, {"verification", "process_identity", "launch"})
    try:
        verification = parse_document(_canonical_json(expected["verification"]), kind="expectation")
    except Exception as exc:
        raise ProcessEvidenceError("INVALID_EXPECTATION") from exc
    identity = expected["process_identity"]
    _require_exact_dict(identity, {"controller", "bootstrap", "producer", "guard", "executable"})
    for value in identity.values():
        _require_hash(value)
    _validate_b1_launch(expected["launch"])
    return verification, identity, expected["launch"]


def _validate_b1_launch(launch: Any) -> None:
    _require_exact_dict(launch, set(_B1_LAUNCH_LITERALS) | {"selection_sha256"})
    for key, literal in _B1_LAUNCH_LITERALS.items():
        if type(launch[key]) is not type(literal) or launch[key] != literal:
            raise ProcessEvidenceError("INVALID_LAUNCH")
    _require_hash(launch["selection_sha256"])


def _validate_b1_invocation(invocation: Any) -> None:
    _require_exact_dict(invocation, {"controller_returncode", "elapsed_ns"})
    returncode, elapsed = invocation["controller_returncode"], invocation["elapsed_ns"]
    if (
        type(returncode) is not int
        or type(elapsed) is not int
        or not -(10 ** 128) < returncode < 10 ** 128
        or not 0 <= elapsed < 10 ** 128
    ):
        raise ProcessEvidenceError("INVALID_INVOCATION")


def _validate_b1_process_result(value: dict) -> None:
    _require_exact_dict(value, {
        "schema", "run", "slot", "scope", "identity", "launch", "process", "streams",
        "receipt", "coordination",
    })
    if value["schema"] != "qwq.verification-process-result/v2" or value["scope"] != "local_b1_process_only":
        raise ProcessEvidenceError("INVALID_SCHEMA")
    _validate_run_slot(value["run"], value["slot"])
    _require_exact_dict(value["identity"], {"controller", "bootstrap", "producer", "guard", "executable"})
    for item in value["identity"].values():
        _require_hash(item)
    _validate_b1_launch(value["launch"])
    _validate_process(value["process"], b1=True)
    _validate_b1_streams(value["streams"])
    _validate_receipt_fact(value["receipt"])
    coordination = value["coordination"]
    _require_exact_dict(coordination, _B1_COORDINATION_BOOLEANS | {"fake_key_path_sha256"})
    for key in _B1_COORDINATION_BOOLEANS:
        if type(coordination[key]) is not bool:
            raise ProcessEvidenceError("INVALID_COORDINATION_BOOLEAN")
    if coordination["fake_key_path_sha256"] is not None:
        _require_hash(coordination["fake_key_path_sha256"])


def _validate_b1_streams(streams: Any) -> None:
    _require_exact_dict(streams, {"stdout", "stderr"})
    for stream in streams.values():
        _require_exact_dict(stream, {"bytes", "sha256", "overflow", "observed_bytes"})
        if type(stream["bytes"]) is not int or not 0 <= stream["bytes"] <= 2097152:
            raise ProcessEvidenceError("INVALID_STREAM_BYTES")
        if type(stream["observed_bytes"]) is not int or not 0 <= stream["observed_bytes"] <= 2097153:
            raise ProcessEvidenceError("INVALID_STREAM_BYTES")
        if stream["bytes"] > stream["observed_bytes"]:
            raise ProcessEvidenceError("INVALID_STREAM_BYTES")
        _require_hash(stream["sha256"])
        if type(stream["overflow"]) is not bool or stream["overflow"] != (stream["observed_bytes"] == 2097153):
            raise ProcessEvidenceError("INVALID_STREAM_OVERFLOW")
