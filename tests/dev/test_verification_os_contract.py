import copy
import hashlib
import json

import pytest

from scripts.dev.verification_os_contract import (
    ProcessEvidenceError,
    evaluate_controlled_slot,
    parse_process_result,
    validate_controlled_receipt,
)


SHA40 = "a" * 40
TREE40 = "b" * 40
CONTRACT64 = "c" * 64
INVENTORY64 = "a5e8106f94a7a50488ce35ca029dfe23e3b0c93bf5fbfd1f920e5693c1d3006c"
GUARD64 = "e" * 64


def _verification():
    """한 슬롯만 소비하되 기존 네 슬롯 expectation 형식을 보존한다."""
    slots = []
    for lane, timezone, runtime in (
        ("standard", "UTC", "1" * 64),
        ("standard", "Asia/Seoul", "2" * 64),
        ("source-proof", "UTC", "3" * 64),
        ("source-proof", "Asia/Seoul", "4" * 64),
    ):
        slots.append(
            {
                "slot": {"lane": lane, "timezone": timezone},
                "identity": {
                    "runtime": runtime,
                    "producer": "d" * 64,
                    "inventory": INVENTORY64,
                },
                "nodes": ["tests/dev/test_standard.py::test_ok"],
                "allowed_outcomes": {},
                "guard": {
                    "path": "tests/conftest.py",
                    "sha256": GUARD64,
                    "module_count": 1,
                    "violations": 0,
                },
            }
        )
    return {
        "schema": "qwq.verification-expectation/v1",
        "run": {
            "event": "local",
            "sha": SHA40,
            "tree": TREE40,
            "contract": CONTRACT64,
            "run_id": "os-process-contract-1",
            "attempt": 1,
        },
        "slots": slots,
    }


def _expected():
    return {
        "verification": _verification(),
        "process_identity": {
            "controller": "5" * 64,
            "bootstrap": "6" * 64,
            "producer": "7" * 64,
            "guard": GUARD64,
            "executable": "8" * 64,
        },
        "launch": {
            "profile": "pytest-evidence-bootstrap/v1",
            "process_scope": "linux-subreaper/v1",
            "timeout_seconds": 30,
            "selection_sha256": "9" * 64,
        },
    }


def _receipt(expected):
    slot = expected["verification"]["slots"][0]
    return {
        "schema": "qwq.verification-receipt/v1",
        "run": copy.deepcopy(expected["verification"]["run"]),
        "slot": copy.deepcopy(slot["slot"]),
        "identity": copy.deepcopy(slot["identity"]),
        "collected": list(slot["nodes"]),
        "results": [
            {
                "nodeid": "tests/dev/test_standard.py::test_ok",
                "setup": "passed",
                "call": "passed",
                "teardown": "passed",
            }
        ],
        "session": {
            "finished": True,
            "exit_code": 0,
            "collection_errors": 0,
            "deselected": 0,
        },
        "guard": copy.deepcopy(slot["guard"]),
    }


def _raw(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _process(expected, receipt_raw):
    guard = expected["verification"]["slots"][0]["guard"]
    return {
        "schema": "qwq.verification-process-result/v1",
        "run": copy.deepcopy(expected["verification"]["run"]),
        "slot": {"lane": "standard", "timezone": "UTC"},
        "scope": "local_os_process_only",
        "identity": copy.deepcopy(expected["process_identity"]),
        "launch": copy.deepcopy(expected["launch"]),
        "process": {
            "reason": "exited",
            "returncode": 0,
            "term_sent": False,
            "kill_sent": False,
            "leader_reaped": True,
            "descendant_survived": False,
            "cleanup_complete": True,
            "descendants_reaped": 0,
            "ownership_probe_passed": True,
            "guard": copy.deepcopy(guard),
            "parent_guard": copy.deepcopy(guard),
        },
        "streams": {
            "stdout": {"bytes": 0, "sha256": "0" * 64, "overflow": False},
            "stderr": {"bytes": 0, "sha256": "0" * 64, "overflow": False},
        },
        "receipt": {
            "state": "regular",
            "bytes": len(receipt_raw),
            "sha256": hashlib.sha256(receipt_raw).hexdigest(),
        },
    }


def _valid_raws():
    expected = _expected()
    receipt_raw = _raw(_receipt(expected))
    return expected, receipt_raw, _raw(_process(expected, receipt_raw))


def test_evaluate_binds_literal_standard_local_evidence_without_claiming_native_or_ci():
    expected, receipt_raw, process_raw = _valid_raws()

    assert validate_controlled_receipt(receipt_raw, process_raw, expected) == ()
    assert evaluate_controlled_slot(receipt_raw, process_raw, expected) == {
        "schema": "qwq.verification-os-decision/v1",
        "status": "OS_RESULT_BOUND",
        "errors": [],
        "scope": "local_os_process_only",
        "native_qualified": False,
        "ci_provenance_verified": False,
        "production_eligible": False,
    }


@pytest.mark.parametrize(
    "raw",
    [
        b'{"schema":"qwq.verification-process-result/v1","schema":"duplicate"}',
        b'{"schema":"qwq.verification-process-result/v1","number":NaN}',
        b"\xff",
        b"{}",
        b" " * (64 * 1024 + 1),
    ],
)
def test_parse_process_result_rejects_noncanonical_or_oversize_bytes(raw):
    with pytest.raises(ProcessEvidenceError):
        parse_process_result(raw)


def test_parse_process_result_rejects_unknown_fields_bool_counts_and_depth_nine():
    expected, receipt_raw, _ = _valid_raws()
    process = _process(expected, receipt_raw)
    process["unknown"] = 1
    with pytest.raises(ProcessEvidenceError):
        parse_process_result(_raw(process))

    process = _process(expected, receipt_raw)
    process["process"]["descendants_reaped"] = True
    with pytest.raises(ProcessEvidenceError):
        parse_process_result(_raw(process))

    nested = "0"
    for _ in range(9):
        nested = "[" + nested + "]"
    with pytest.raises(ProcessEvidenceError):
        parse_process_result(nested.encode())


@pytest.mark.parametrize(
    ("path", "value", "expected_error"),
    [
        (("process", "reason"), "timeout", "PROCESS_EXIT_REJECTED"),
        (("process", "cleanup_complete"), False, "PROCESS_CLEANUP_INCOMPLETE"),
        (("process", "ownership_probe_passed"), False, "PROCESS_OWNERSHIP_UNPROVEN"),
        (("process", "descendant_survived"), True, "PROCESS_DESCENDANT_SURVIVED"),
        (("streams", "stdout", "overflow"), True, "PROCESS_STREAM_OVERFLOW"),
    ],
)
def test_evaluate_rejects_unapproved_process_statuses(path, value, expected_error):
    expected, receipt_raw, _ = _valid_raws()
    process = _process(expected, receipt_raw)
    target = process
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value

    decision = evaluate_controlled_slot(receipt_raw, _raw(process), expected)
    assert decision["status"] == "REJECTED"
    assert expected_error in decision["errors"]


@pytest.mark.parametrize(
    ("mutate", "error"),
    [
        (lambda process, receipt: process["run"].__setitem__("attempt", 2), "PROCESS_RUN_MISMATCH"),
        (lambda process, receipt: process["slot"].__setitem__("timezone", "Asia/Seoul"), "PROCESS_SLOT_MISMATCH"),
        (lambda process, receipt: process["identity"].__setitem__("controller", "f" * 64), "PROCESS_IDENTITY_MISMATCH"),
        (lambda process, receipt: process["launch"].__setitem__("timeout_seconds", 31), "PROCESS_LAUNCH_MISMATCH"),
        (lambda process, receipt: process["process"]["guard"].__setitem__("violations", 1), "PROCESS_GUARD_MISMATCH"),
        (lambda process, receipt: process["receipt"].__setitem__("sha256", "f" * 64), "PROCESS_RECEIPT_MISMATCH"),
    ],
)
def test_validate_rejects_mismatched_process_facts(mutate, error):
    expected, receipt_raw, _ = _valid_raws()
    process = _process(expected, receipt_raw)
    mutate(process, receipt_raw)

    assert error in validate_controlled_receipt(receipt_raw, _raw(process), expected)


def test_validate_preserves_existing_skip_rejection_and_rejects_source_proof_slot():
    expected, receipt_raw, _ = _valid_raws()
    receipt = _receipt(expected)
    receipt["results"][0]["call"] = "xfailed"
    receipt_raw = _raw(receipt)
    process = _process(expected, receipt_raw)
    assert "UNSUPPORTED_OUTCOME" in validate_controlled_receipt(receipt_raw, _raw(process), expected)

    source = _process(expected, receipt_raw)
    source["slot"] = {"lane": "source-proof", "timezone": "UTC"}
    assert "PROCESS_SCOPE_MISMATCH" in validate_controlled_receipt(receipt_raw, _raw(source), expected)
