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
            "producer": "d" * 64,
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


def _receipt(expected, position=0):
    slot = expected["verification"]["slots"][position]
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


def _process(expected, receipt_raw, position=0):
    slot = expected["verification"]["slots"][position]
    guard = slot["guard"]
    return {
        "schema": "qwq.verification-process-result/v1",
        "run": copy.deepcopy(expected["verification"]["run"]),
        "slot": copy.deepcopy(slot["slot"]),
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


def _coherent_valid_raws():
    """receipt와 process가 같은 producer/guard 파일을 가리키는 정상 증거다."""
    return _valid_raws()


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


def test_validate_rejects_process_producer_identity_not_bound_to_receipt():
    expected, receipt_raw, process_raw = _coherent_valid_raws()
    process = json.loads(process_raw)
    process["identity"]["producer"] = "f" * 64
    expected["process_identity"]["producer"] = "f" * 64
    assert validate_controlled_receipt(receipt_raw, _raw(process), expected) == (
        "PROCESS_IDENTITY_MISMATCH",
    )


def test_validate_rejects_process_guard_identity_not_bound_to_receipt():
    expected, receipt_raw, process_raw = _coherent_valid_raws()
    process = json.loads(process_raw)
    process["identity"]["guard"] = "f" * 64
    expected["process_identity"]["guard"] = "f" * 64
    assert validate_controlled_receipt(receipt_raw, _raw(process), expected) == (
        "PROCESS_GUARD_MISMATCH",
    )


def test_validate_normalizes_deep_direct_expectation_to_fixed_error():
    expected, receipt_raw, process_raw = _coherent_valid_raws()
    deep = 0
    for _ in range(10_000):
        deep = [deep]
    expected["verification"] = deep
    assert validate_controlled_receipt(receipt_raw, process_raw, expected) == (
        "INVALID_EXPECTATION",
    )
    assert evaluate_controlled_slot(receipt_raw, process_raw, expected)["errors"] == [
        "INVALID_EXPECTATION"
    ]


def test_validate_rejects_direct_launch_equality_object():
    class EqualToProfile:
        def __eq__(self, other):
            return other == "pytest-evidence-bootstrap/v1"

    expected, receipt_raw, process_raw = _coherent_valid_raws()
    expected["launch"]["profile"] = EqualToProfile()
    assert validate_controlled_receipt(receipt_raw, process_raw, expected) == (
        "INVALID_EXPECTATION",
    )


def test_validator_normalizes_multiple_bad_inputs_to_sorted_fixed_errors():
    assert validate_controlled_receipt(b"{", b"{}", {}) == (
        "INVALID_EXPECTATION",
        "INVALID_PROCESS_RESULT",
        "INVALID_RECEIPT",
    )


def test_process_parser_accepts_exact_byte_limit_and_reaches_depth_guard_before_schema():
    expected, receipt_raw, _ = _coherent_valid_raws()
    raw = _raw(_process(expected, receipt_raw))
    assert len(raw) < 64 * 1024
    padded = raw + b" " * (64 * 1024 - len(raw))
    assert parse_process_result(padded)["schema"] == "qwq.verification-process-result/v1"

    nested = 0
    for _ in range(8):
        nested = {"nested": nested}
    with pytest.raises(ProcessEvidenceError, match="DOCUMENT_TOO_DEEP"):
        parse_process_result(_raw(nested))


@pytest.mark.parametrize(
    "mutate",
    [
        lambda process: process["process"].__setitem__("returncode", True),
        lambda process: process["process"].__setitem__("returncode", -65),
        lambda process: process["process"].__setitem__("returncode", 256),
        lambda process: process["process"].update(leader_reaped=True, returncode=None),
        lambda process: process["process"].update(leader_reaped=False, returncode=0),
        lambda process: process["process"]["guard"].__setitem__("violations", True),
        lambda process: process["process"].__setitem__("descendants_reaped", 20_001),
    ],
)
def test_process_parser_rejects_terminal_type_and_range_incoherence(mutate):
    expected, receipt_raw, _ = _coherent_valid_raws()
    process = _process(expected, receipt_raw)
    mutate(process)
    with pytest.raises(ProcessEvidenceError):
        parse_process_result(_raw(process))


@pytest.mark.parametrize(
    ("stream_name", "bytes_count", "overflow", "parses"),
    [
        ("stdout", 8 * 1024 * 1024, False, True),
        ("stderr", 8 * 1024 * 1024, False, True),
        ("stdout", 8 * 1024 * 1024 + 1, True, True),
        ("stderr", 8 * 1024 * 1024 + 1, False, False),
    ],
)
def test_stream_limits_are_strict_and_any_overflow_cannot_bind(stream_name, bytes_count, overflow, parses):
    expected, receipt_raw, _ = _coherent_valid_raws()
    process = _process(expected, receipt_raw)
    process["streams"][stream_name].update(bytes=bytes_count, overflow=overflow)
    if not parses:
        with pytest.raises(ProcessEvidenceError):
            parse_process_result(_raw(process))
        return
    assert parse_process_result(_raw(process))["streams"][stream_name]["bytes"] == bytes_count
    if overflow:
        assert "PROCESS_STREAM_OVERFLOW" in validate_controlled_receipt(
            receipt_raw, _raw(process), expected
        )


@pytest.mark.parametrize("state", ["missing", "invalid"])
def test_nonregular_receipt_fact_and_absent_guard_never_bind(state):
    expected, receipt_raw, _ = _coherent_valid_raws()
    process = _process(expected, receipt_raw)
    process["receipt"] = {"state": state, "bytes": 0, "sha256": None}
    assert "PROCESS_RECEIPT_MISMATCH" in validate_controlled_receipt(
        receipt_raw, _raw(process), expected
    )

    process = _process(expected, receipt_raw)
    process["process"]["parent_guard"] = None
    assert "PROCESS_GUARD_MISMATCH" in validate_controlled_receipt(
        receipt_raw, _raw(process), expected
    )


@pytest.mark.parametrize("outcome", ["skipped", "xfailed", "xpassed"])
def test_all_unsupported_existing_receipt_outcomes_remain_rejected(outcome):
    expected, receipt_raw, _ = _coherent_valid_raws()
    receipt = _receipt(expected)
    receipt["results"][0]["call"] = outcome
    receipt_raw = _raw(receipt)
    assert "UNSUPPORTED_OUTCOME" in validate_controlled_receipt(
        receipt_raw, _raw(_process(expected, receipt_raw)), expected
    )


def test_matching_source_proof_or_nonlocal_receipt_is_rejected_by_controlled_scope():
    expected = _expected()
    receipt_raw = _raw(_receipt(expected, 2))
    assert "PROCESS_SCOPE_MISMATCH" in validate_controlled_receipt(
        receipt_raw, _raw(_process(expected, receipt_raw, 2)), expected
    )

    expected = _expected()
    expected["verification"]["run"]["event"] = "push"
    receipt_raw = _raw(_receipt(expected))
    assert "PROCESS_SCOPE_MISMATCH" in validate_controlled_receipt(
        receipt_raw, _raw(_process(expected, receipt_raw)), expected
    )


@pytest.mark.parametrize(
    "mutate",
    [
        lambda expected: expected.__setitem__("unknown", 1),
        lambda expected: expected.__delitem__("verification"),
        lambda expected: [],
        lambda expected: expected["process_identity"].__setitem__("controller", "A" * 64),
        lambda expected: expected["process_identity"].__setitem__("controller", "a" * 63),
        lambda expected: expected["process_identity"].__setitem__("controller", 1),
        lambda expected: expected["launch"].__setitem__("unknown", 1),
        lambda expected: expected["launch"].__setitem__("profile", "other/v1"),
    ],
)
def test_direct_expected_wrapper_hash_and_literal_boundaries_are_fixed_errors(mutate):
    expected, receipt_raw, process_raw = _coherent_valid_raws()
    candidate = mutate(expected)
    if candidate is not None:
        expected = candidate
    assert validate_controlled_receipt(receipt_raw, process_raw, expected) == (
        "INVALID_EXPECTATION",
    )


@pytest.mark.parametrize("timeout_seconds", [1, 900])
def test_timeout_launch_endpoints_bind_when_expected_and_process_agree(timeout_seconds):
    expected = _expected()
    expected["launch"]["timeout_seconds"] = timeout_seconds
    receipt_raw = _raw(_receipt(expected))
    process_raw = _raw(_process(expected, receipt_raw))
    assert evaluate_controlled_slot(receipt_raw, process_raw, expected)["status"] == "OS_RESULT_BOUND"


@pytest.mark.parametrize("timeout_seconds", [0, 901, True])
def test_timeout_launch_outside_range_or_bool_is_rejected_on_each_input_boundary(timeout_seconds):
    expected, receipt_raw, process_raw = _coherent_valid_raws()
    expected["launch"]["timeout_seconds"] = timeout_seconds
    assert validate_controlled_receipt(receipt_raw, process_raw, expected) == (
        "INVALID_EXPECTATION",
    )

    expected, receipt_raw, process_raw = _coherent_valid_raws()
    process = json.loads(process_raw)
    process["launch"]["timeout_seconds"] = timeout_seconds
    assert validate_controlled_receipt(receipt_raw, _raw(process), expected) == (
        "INVALID_PROCESS_RESULT",
    )


@pytest.mark.parametrize("factory", [dict, list])
def test_direct_cyclic_expected_verification_is_normalized(factory):
    expected, receipt_raw, process_raw = _coherent_valid_raws()
    cycle = factory()
    if type(cycle) is dict:
        cycle["self"] = cycle
    else:
        cycle.append(cycle)
    expected["verification"] = cycle
    assert validate_controlled_receipt(receipt_raw, process_raw, expected) == (
        "INVALID_EXPECTATION",
    )


def test_parser_distinguishes_depth_eight_schema_rejection_from_depth_nine_limit():
    depth_eight = 0
    for _ in range(7):
        depth_eight = {"nested": depth_eight}
    with pytest.raises(ProcessEvidenceError, match="INVALID_FIELDS"):
        parse_process_result(_raw(depth_eight))

    depth_nine = {"nested": depth_eight}
    with pytest.raises(ProcessEvidenceError, match="DOCUMENT_TOO_DEEP"):
        parse_process_result(_raw(depth_nine))


def test_regular_receipt_declared_bytes_must_match_raw_receipt_alone():
    expected, receipt_raw, _ = _coherent_valid_raws()
    process = _process(expected, receipt_raw)
    process["receipt"]["bytes"] += 1
    assert validate_controlled_receipt(receipt_raw, _raw(process), expected) == (
        "PROCESS_RECEIPT_MISMATCH",
    )


def test_stderr_overflow_is_a_legal_failure_document_but_never_binds():
    expected, receipt_raw, _ = _coherent_valid_raws()
    process = _process(expected, receipt_raw)
    process["streams"]["stderr"].update(bytes=8 * 1024 * 1024 + 1, overflow=True)
    assert parse_process_result(_raw(process))["streams"]["stderr"]["overflow"] is True
    assert validate_controlled_receipt(receipt_raw, _raw(process), expected) == (
        "PROCESS_STREAM_OVERFLOW",
    )


def test_missing_child_or_different_parent_guard_is_a_single_guard_mismatch():
    expected, receipt_raw, _ = _coherent_valid_raws()
    process = _process(expected, receipt_raw)
    process["process"]["guard"] = None
    assert validate_controlled_receipt(receipt_raw, _raw(process), expected) == (
        "PROCESS_GUARD_MISMATCH",
    )

    process = _process(expected, receipt_raw)
    process["process"]["parent_guard"]["sha256"] = "f" * 64
    assert validate_controlled_receipt(receipt_raw, _raw(process), expected) == (
        "PROCESS_GUARD_MISMATCH",
    )


def test_rejected_decision_has_exact_local_nonqualification_shape():
    expected, receipt_raw, _ = _coherent_valid_raws()
    process = _process(expected, receipt_raw)
    process["receipt"]["bytes"] += 1
    assert evaluate_controlled_slot(receipt_raw, _raw(process), expected) == {
        "schema": "qwq.verification-os-decision/v1",
        "status": "REJECTED",
        "errors": ["PROCESS_RECEIPT_MISMATCH"],
        "scope": "local_os_process_only",
        "native_qualified": False,
        "ci_provenance_verified": False,
        "production_eligible": False,
    }


@pytest.mark.parametrize(
    ("reason", "returncode", "term_sent", "kill_sent"),
    [
        ("signaled", -9, False, False),
        ("timeout", 124, False, False),
        ("interrupted", -15, False, False),
        ("startup_error", None, False, False),
        ("output_limit", None, False, False),
        ("cleanup_error", None, False, False),
        ("identity_changed", None, False, False),
        ("io_error", None, False, False),
        ("exited", 1, False, False),
        ("exited", 0, True, False),
        ("exited", 0, False, True),
    ],
)
def test_each_nonqualifying_reason_or_terminal_fact_is_rejected(reason, returncode, term_sent, kill_sent):
    expected, receipt_raw, _ = _coherent_valid_raws()
    process = _process(expected, receipt_raw)
    process["process"].update(
        reason=reason,
        returncode=returncode,
        term_sent=term_sent,
        kill_sent=kill_sent,
        leader_reaped=returncode is not None,
    )
    assert "PROCESS_EXIT_REJECTED" in validate_controlled_receipt(
        receipt_raw, _raw(process), expected
    )


@pytest.mark.parametrize(
    "raw",
    [
        b'{"schema":"qwq.verification-process-result/v1","schema":"duplicate"}',
        b'{"schema":"qwq.verification-process-result/v1","number":NaN}',
        b"\xff",
        b"{}",
        pytest.param(b" " * (64 * 1024 + 1), id="document-too-large"),
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
