"""컨트롤러 출력/상수를 사용하지 않는 B1 순수 결속 계약의 독립 입력이다."""

import hashlib
import json

import pytest

from scripts.dev import verification_os_contract as contract


def _raw(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _expected():
    """실행 결과를 받지 않고 사전에 정한 네 슬롯 기대값을 구성한다."""
    return {
        "verification": {
            "schema": "qwq.verification-expectation/v1",
            "run": {
                "event": "local", "sha": "a" * 40, "tree": "b" * 40,
                "contract": "c" * 64, "run_id": "b1-contract-literal-1", "attempt": 1,
            },
            "slots": [
                {
                    "slot": {"lane": lane, "timezone": timezone},
                    "identity": {
                        "runtime": runtime,
                        "producer": "d" * 64,
                        "inventory": "a5e8106f94a7a50488ce35ca029dfe23e3b0c93bf5fbfd1f920e5693c1d3006c",
                    },
                    "nodes": ["tests/dev/test_standard.py::test_ok"],
                    "allowed_outcomes": {},
                    "guard": {
                        "path": "tests/conftest.py", "sha256": "e" * 64,
                        "module_count": 1, "violations": 0,
                    },
                }
                for lane, timezone, runtime in (
                    ("standard", "UTC", "1" * 64),
                    ("standard", "Asia/Seoul", "2" * 64),
                    ("source-proof", "UTC", "3" * 64),
                    ("source-proof", "Asia/Seoul", "4" * 64),
                )
            ],
        },
        "process_identity": {
            "controller": "5" * 64, "bootstrap": "6" * 64,
            "producer": "d" * 64, "guard": "e" * 64, "executable": "8" * 64,
        },
        "launch": {
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
            "selection_sha256": "9" * 64,
        },
    }


def _receipt():
    """기대값과 별개로 작성한 관측 receipt; 기대값으로 역산하지 않는다."""
    return {
        "schema": "qwq.verification-receipt/v1",
        "run": {
            "event": "local", "sha": "a" * 40, "tree": "b" * 40,
            "contract": "c" * 64, "run_id": "b1-contract-literal-1", "attempt": 1,
        },
        "slot": {"lane": "standard", "timezone": "UTC"},
        "identity": {
            "runtime": "1" * 64, "producer": "d" * 64,
            "inventory": "a5e8106f94a7a50488ce35ca029dfe23e3b0c93bf5fbfd1f920e5693c1d3006c",
        },
        "collected": ["tests/dev/test_standard.py::test_ok"],
        "results": [{
            "nodeid": "tests/dev/test_standard.py::test_ok",
            "setup": "passed", "call": "passed", "teardown": "passed",
        }],
        "session": {"finished": True, "exit_code": 0, "collection_errors": 0, "deselected": 0},
        "guard": {
            "path": "tests/conftest.py", "sha256": "e" * 64,
            "module_count": 1, "violations": 0,
        },
    }


def _process(receipt_raw):
    """오직 receipt의 원시 바이트 결속만 계산하며 다른 사실은 독립 리터럴이다."""
    return {
        "schema": "qwq.verification-process-result/v2",
        "scope": "local_b1_process_only",
        "run": {
            "event": "local", "sha": "a" * 40, "tree": "b" * 40,
            "contract": "c" * 64, "run_id": "b1-contract-literal-1", "attempt": 1,
        },
        "slot": {"lane": "standard", "timezone": "UTC"},
        "identity": {
            "controller": "5" * 64, "bootstrap": "6" * 64,
            "producer": "d" * 64, "guard": "e" * 64, "executable": "8" * 64,
        },
        "launch": {
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
            "selection_sha256": "9" * 64,
        },
        "process": {
            "reason": "exited", "returncode": 0, "term_sent": False,
            "kill_sent": False, "leader_reaped": True, "descendant_survived": False,
            "cleanup_complete": True, "descendants_reaped": 0, "ownership_probe_passed": True,
            "guard": {
                "path": "tests/conftest.py", "sha256": "e" * 64,
                "module_count": 1, "violations": 0,
            },
            "parent_guard": {
                "path": "tests/conftest.py", "sha256": "e" * 64,
                "module_count": 1, "violations": 0,
            },
        },
        "streams": {
            "stdout": {
                "bytes": 0, "observed_bytes": 0, "overflow": False,
                "sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
            },
            "stderr": {
                "bytes": 0, "observed_bytes": 0, "overflow": False,
                "sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
            },
        },
        "receipt": {
            "state": "regular", "bytes": len(receipt_raw),
            "sha256": hashlib.sha256(receipt_raw).hexdigest(),
        },
        "coordination": {
            "lock_acquired": True, "lock_identity_stable": True,
            "fake_key_absent_before": True, "fake_key_absent_after": True,
            "fake_directory_removed": True, "fake_key_path_sha256": "f" * 64,
        },
    }


def test_b1_binds_independent_literal_evidence_without_accepting_outcomes():
    """정상 결속 누락 또는 의미/native/CI/운영 승격 버그를 잡는다."""
    receipt_raw = _raw(_receipt())
    process_raw = _raw(_process(receipt_raw))
    invocation = {"controller_returncode": 0, "elapsed_ns": 900000000000}

    assert contract.validate_b1_process_receipt(
        receipt_raw, process_raw, _expected(), invocation
    ) == ()
    assert contract.evaluate_b1_process_slot(
        receipt_raw, process_raw, _expected(), invocation
    ) == {
        "schema": "qwq.b1-process-decision/v1",
        "status": "B1_PROCESS_BOUND",
        "errors": [],
        "scope": "local_b1_process_only",
        "outcomes_accepted": False,
        "native_qualified": False,
        "ci_provenance_verified": False,
        "production_eligible": False,
    }


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("profile", "pytest-evidence-bootstrap/v1"),
        ("process_scope", "process-group/v1"),
        ("timeout_seconds", 899),
        ("timeout_seconds", 901),
        ("timeout_seconds", True),
        ("timeout_seconds", 900.0),
        ("retained_stream_bytes", 2097151),
        ("retained_stream_bytes", 2097153),
        ("retained_stream_bytes", 8388608),
        ("retained_stream_bytes", True),
        ("overflow_observed_bytes", 2097152),
        ("overflow_observed_bytes", True),
        ("budget_profile", "workload-only/v1"),
        ("pytest_profile", "pytest-evidence-bootstrap/v1"),
        ("environment_profile", "inherit-parent/v1"),
        ("selection_profile", "arbitrary-options/v1"),
        ("lock_profile", "worktree-local/v1"),
    ],
)
def test_b1_schema_profile_confusion_rejected(field, value):
    """expected/result의 위조 상수가 같아도 고정 B1 계약을 우회하지 못한다."""
    receipt_raw = _raw(_receipt())
    process = _process(receipt_raw)
    expected = _expected()
    process["launch"][field] = value
    expected["launch"][field] = value

    with pytest.raises(contract.ProcessEvidenceError):
        contract.parse_b1_process_result(_raw(process))
    assert contract.validate_b1_process_receipt(
        receipt_raw, _raw(process), expected,
        {"controller_returncode": 0, "elapsed_ns": 1000000000},
    ) == ("INVALID_EXPECTATION", "INVALID_PROCESS_RESULT")


@pytest.mark.parametrize(
    ("invocation", "error"),
    [
        (None, "INVALID_INVOCATION"),
        ({}, "INVALID_INVOCATION"),
        ({"controller_returncode": 0}, "INVALID_INVOCATION"),
        ({"controller_returncode": False, "elapsed_ns": 1}, "INVALID_INVOCATION"),
        ({"controller_returncode": 0, "elapsed_ns": True}, "INVALID_INVOCATION"),
        ({"controller_returncode": 0, "elapsed_ns": -1}, "INVALID_INVOCATION"),
        ({"controller_returncode": 0, "elapsed_ns": "1"}, "INVALID_INVOCATION"),
        ({"controller_returncode": 0, "elapsed_ns": 1.0}, "INVALID_INVOCATION"),
        ({"controller_returncode": 0.0, "elapsed_ns": 1}, "INVALID_INVOCATION"),
        ({"controller_returncode": "0", "elapsed_ns": 1}, "INVALID_INVOCATION"),
        ({"controller_returncode": 0, "elapsed_ns": 10 ** 128}, "INVALID_INVOCATION"),
        ({"controller_returncode": 10 ** 128, "elapsed_ns": 1}, "INVALID_INVOCATION"),
        ({"controller_returncode": -(10 ** 128), "elapsed_ns": 1}, "INVALID_INVOCATION"),
        ({"controller_returncode": 0, "elapsed_ns": 1, "trusted": True}, "INVALID_INVOCATION"),
        ([0, 1], "INVALID_INVOCATION"),
        ({"controller_returncode": 125, "elapsed_ns": 1}, "B1_CONTROLLER_EXIT_REJECTED"),
        ({"controller_returncode": -9, "elapsed_ns": 1}, "B1_CONTROLLER_EXIT_REJECTED"),
        ({"controller_returncode": 0, "elapsed_ns": 900000000001}, "B1_TOTAL_BUDGET_EXCEEDED"),
    ],
)
def test_b1_candidate_publication_does_not_prove_exit(invocation, error):
    """완전한 성공 JSON도 독립 호출자의 종료/총시간 증거를 대신하지 못한다."""
    receipt_raw = _raw(_receipt())
    assert contract.validate_b1_process_receipt(
        receipt_raw, _raw(_process(receipt_raw)), _expected(), invocation
    ) == (error,)


@pytest.mark.parametrize("field", [
    "lock_acquired", "lock_identity_stable", "fake_key_absent_before",
    "fake_key_absent_after", "fake_directory_removed", "fake_key_path_sha256",
])
def test_b1_missing_coordination_cannot_bind(field):
    """어느 조정 사실 하나라도 미입증이면 성공 결속이 열리지 않는다."""
    receipt_raw = _raw(_receipt())
    process = _process(receipt_raw)
    process["coordination"][field] = None if field == "fake_key_path_sha256" else False
    errors = contract.validate_b1_process_receipt(
        receipt_raw, _raw(process), _expected(),
        {"controller_returncode": 0, "elapsed_ns": 1000000000},
    )
    assert errors == ("B1_COORDINATION_UNPROVEN",)


def _v1_expected():
    expected = _expected()
    expected["launch"] = {
        "profile": "pytest-evidence-bootstrap/v1", "process_scope": "linux-subreaper/v1",
        "timeout_seconds": 30, "selection_sha256": "9" * 64,
    }
    return expected


def _v1_process(receipt_raw):
    """시험 입력만 변환한다. 제품 parser/controller 출력은 사용하지 않는다."""
    process = _process(receipt_raw)
    process["schema"] = "qwq.verification-process-result/v1"
    process["scope"] = "local_os_process_only"
    process["launch"] = {
        "profile": "pytest-evidence-bootstrap/v1", "process_scope": "linux-subreaper/v1",
        "timeout_seconds": 30, "selection_sha256": "9" * 64,
    }
    del process["coordination"]
    del process["streams"]["stdout"]["observed_bytes"]
    del process["streams"]["stderr"]["observed_bytes"]
    return process


def _bind(receipt_raw, process, expected=None, invocation=None):
    return contract.validate_b1_process_receipt(
        receipt_raw, _raw(process), _expected() if expected is None else expected,
        {"controller_returncode": 0, "elapsed_ns": 1} if invocation is None else invocation,
    )


def _set(document, path, value):
    target = document
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value


@pytest.mark.parametrize("elapsed_ns", [0, 1, 899999999999, 900000000000])
def test_b1_inclusive_supplied_invocation_bound_accepts_endpoints(elapsed_ns):
    receipt_raw = _raw(_receipt())
    assert _bind(
        receipt_raw, _process(receipt_raw),
        invocation={"controller_returncode": 0, "elapsed_ns": elapsed_ns},
    ) == ()


@pytest.mark.parametrize(
    ("pre_ns", "post_ns", "reported_waits_ns"),
    [
        pytest.param(1000000000000, 1901000000000, [1000000000, 1000000000], id="yielded-session-gap"),
        pytest.param(2000000000000, 2900000000001, [899000000000], id="after-publication-delay"),
    ],
)
def test_b1_total_bracket_cannot_be_shortened_by_tool_waits(pre_ns, post_ns, reported_waits_ns):
    """합성 시각 차이는 호출 간 공백을 포함하며 provenance 검증을 흉내내지 않는다."""
    assert sum(reported_waits_ns) < 900000000000
    receipt_raw = _raw(_receipt())
    assert _bind(
        receipt_raw, _process(receipt_raw),
        invocation={"controller_returncode": 0, "elapsed_ns": post_ns - pre_ns},
    ) == ("B1_TOTAL_BUDGET_EXCEEDED",)


def test_b1_and_v1_documents_cannot_substitute_for_each_other():
    receipt_raw = _raw(_receipt())
    with pytest.raises(contract.ProcessEvidenceError):
        contract.parse_b1_process_result(_raw(_v1_process(receipt_raw)))
    with pytest.raises(contract.ProcessEvidenceError):
        contract.parse_process_result(_raw(_process(receipt_raw)))
    assert _bind(receipt_raw, _process(receipt_raw), expected=_v1_expected()) == ("INVALID_EXPECTATION",)
    assert contract.validate_controlled_receipt(
        receipt_raw, _raw(_v1_process(receipt_raw)), _expected()
    ) == ("INVALID_EXPECTATION",)


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("schema",), "qwq.verification-process-result/v1"),
        (("scope",), "local_os_process_only"),
        (("schema",), True),
        (("scope",), 1),
        (("identity", "controller"), "A" * 64),
        (("identity", "controller"), "a" * 63),
        (("launch", "selection_sha256"), "x" * 64),
        (("run", "attempt"), True),
        (("process", "returncode"), True),
        (("process", "returncode"), -65),
        (("process", "returncode"), 256),
        (("process", "returncode"), None),
        (("process", "leader_reaped"), False),
        (("process", "term_sent"), 0),
        (("process", "kill_sent"), 0),
        (("process", "descendant_survived"), 0),
        (("process", "cleanup_complete"), 1),
        (("process", "ownership_probe_passed"), 1),
        (("process", "descendants_reaped"), True),
        (("process", "descendants_reaped"), -1),
        (("process", "descendants_reaped"), 20001),
        (("process", "guard", "violations"), True),
        (("process", "parent_guard", "module_count"), True),
        (("process", "guard", "path"), "../conftest.py"),
        (("process", "reason"), "unknown"),
        (("receipt", "state"), []),
        (("receipt", "bytes"), True),
        (("receipt", "bytes"), 33554433),
        (("receipt", "sha256"), None),
        (("coordination", "fake_key_path_sha256"), "f" * 63),
    ],
)
def test_b1_parser_rejects_malformed_typed_facts(path, value):
    process = _process(_raw(_receipt()))
    _set(process, path, value)
    with pytest.raises(contract.ProcessEvidenceError):
        contract.parse_b1_process_result(_raw(process))


@pytest.mark.parametrize("field", [
    "lock_acquired", "lock_identity_stable", "fake_key_absent_before",
    "fake_key_absent_after", "fake_directory_removed",
])
@pytest.mark.parametrize("value", [0, 1, None, "true"])
def test_b1_coordination_requires_exact_booleans(field, value):
    process = _process(_raw(_receipt()))
    process["coordination"][field] = value
    with pytest.raises(contract.ProcessEvidenceError):
        contract.parse_b1_process_result(_raw(process))


@pytest.mark.parametrize("path", [
    (), ("run",), ("slot",), ("identity",), ("launch",), ("process",),
    ("process", "guard"), ("process", "parent_guard"), ("streams",),
    ("streams", "stdout"), ("streams", "stderr"), ("receipt",), ("coordination",),
])
@pytest.mark.parametrize("change", ["extra", "missing"])
def test_b1_every_object_has_exact_keys(path, change):
    process = _process(_raw(_receipt()))
    target = process
    for key in path:
        target = target[key]
    if change == "extra":
        target["unknown"] = 0
    else:
        del target[next(iter(target))]
    with pytest.raises(contract.ProcessEvidenceError):
        contract.parse_b1_process_result(_raw(process))


@pytest.mark.parametrize(("raw", "error"), [
    pytest.param(None, "RAW_NOT_BYTES", id="none"),
    pytest.param("{}", "RAW_NOT_BYTES", id="text"),
    pytest.param(bytearray(b"{}"), "RAW_NOT_BYTES", id="bytearray"),
    pytest.param(b"[]", "DOCUMENT_NOT_OBJECT", id="array"),
    pytest.param(b"null", "DOCUMENT_NOT_OBJECT", id="null"),
    pytest.param(b"\xff", "INVALID_JSON", id="utf8"),
    pytest.param(b"{", "INVALID_JSON", id="truncated"),
    pytest.param(b"{}", "INVALID_FIELDS", id="empty"),
    pytest.param(b'{"value":NaN}', "NONFINITE_NUMBER", id="nan"),
    pytest.param(b'{"value":Infinity}', "NONFINITE_NUMBER", id="infinity"),
    pytest.param(b'{"value":-Infinity}', "NONFINITE_NUMBER", id="negative-infinity"),
    pytest.param(b'{"value":' + b"9" * 129 + b"}", "INTEGER_TOO_LARGE", id="integer-129-digits"),
    pytest.param(b'{"value":"\\ud800"}', "INVALID_STRING", id="surrogate"),
    pytest.param(b" " * 65537, "DOCUMENT_TOO_LARGE", id="oversize"),
])
def test_b1_strict_json_rejects_ambiguous_or_unbounded_input(raw, error):
    with pytest.raises(contract.ProcessEvidenceError, match=error):
        contract.parse_b1_process_result(raw)


@pytest.mark.parametrize("fragment", [
    b'"schema":"qwq.verification-process-result/v2"',
    b'"timeout_seconds":900',
    b'"observed_bytes":0',
])
def test_b1_repeated_keys_rejected_in_otherwise_valid_document(fragment):
    raw = _raw(_process(_raw(_receipt())))
    repeated = raw.replace(fragment, fragment + b"," + fragment, 1)
    with pytest.raises(contract.ProcessEvidenceError, match="DUPLICATE_KEY"):
        contract.parse_b1_process_result(repeated)


def test_b1_integer_digit_limit_allows_128_but_not_129():
    process = _process(_raw(_receipt()))
    process["run"]["attempt"] = 10 ** 127
    assert contract.parse_b1_process_result(_raw(process))["run"]["attempt"] == 10 ** 127
    process["run"]["attempt"] = 10 ** 128
    with pytest.raises(contract.ProcessEvidenceError, match="INTEGER_TOO_LARGE"):
        contract.parse_b1_process_result(_raw(process))


def test_b1_exact_json_size_and_depth_limits():
    raw = _raw(_process(_raw(_receipt())))
    assert contract.parse_b1_process_result(raw + b" " * (65536 - len(raw)))["schema"] == "qwq.verification-process-result/v2"
    with pytest.raises(contract.ProcessEvidenceError, match="DOCUMENT_TOO_LARGE"):
        contract.parse_b1_process_result(raw + b" " * (65537 - len(raw)))
    nested = 0
    for _ in range(7):
        nested = {"nested": nested}
    with pytest.raises(contract.ProcessEvidenceError, match="INVALID_FIELDS"):
        contract.parse_b1_process_result(_raw(nested))
    with pytest.raises(contract.ProcessEvidenceError, match="DOCUMENT_TOO_DEEP"):
        contract.parse_b1_process_result(_raw({"nested": nested}))


@pytest.mark.parametrize("stream_name", ["stdout", "stderr"])
@pytest.mark.parametrize(
    ("retained", "observed", "overflow", "reason", "parses", "errors"),
    [
        (2097151, 2097151, False, "exited", True, ()),
        (2097152, 2097152, False, "exited", True, ()),
        (2097152, 2097153, True, "output_limit", True, ("PROCESS_EXIT_REJECTED", "PROCESS_STREAM_OVERFLOW")),
        (0, 2097153, True, "io_error", True, ("PROCESS_EXIT_REJECTED", "PROCESS_STREAM_OVERFLOW")),
        (7, 2097153, True, "io_error", True, ("PROCESS_EXIT_REJECTED", "PROCESS_STREAM_OVERFLOW")),
        (7, 8, False, "io_error", True, ("PROCESS_EXIT_REJECTED",)),
        (7, 8, False, "exited", True, ("PROCESS_EXIT_REJECTED",)),
        (2097152, 2097153, True, "exited", True, ("PROCESS_STREAM_OVERFLOW",)),
        (2097152, 2097153, False, "output_limit", False, None),
        (2097153, 2097153, True, "output_limit", False, None),
        (8388608, 2097153, True, "output_limit", False, None),
        (0, 2097154, True, "output_limit", False, None),
        (0, 0, True, "output_limit", False, None),
        (8, 7, False, "io_error", False, None),
        (-1, 0, False, "io_error", False, None),
        (0, -1, False, "io_error", False, None),
        (True, 1, False, "exited", False, None),
        (0, True, False, "exited", False, None),
        (0, 0, 0, "exited", False, None),
    ],
)
def test_b1_prefix_observation_contract(stream_name, retained, observed, overflow, reason, parses, errors):
    """순수 구조/결속 시험이며 실제 보관 로그의 hash/size 대조는 별도 인수다."""
    receipt_raw = _raw(_receipt())
    process = _process(receipt_raw)
    process["process"]["reason"] = reason
    process["streams"][stream_name].update(bytes=retained, observed_bytes=observed, overflow=overflow)
    if not parses:
        with pytest.raises(contract.ProcessEvidenceError):
            contract.parse_b1_process_result(_raw(process))
        return
    parsed = contract.parse_b1_process_result(_raw(process))
    assert parsed["streams"][stream_name]["bytes"] == retained
    assert parsed["streams"][stream_name]["observed_bytes"] == observed
    assert parsed["streams"][stream_name]["overflow"] is overflow
    assert _bind(receipt_raw, process) == errors


@pytest.mark.parametrize("stream_name", ["stdout", "stderr"])
def test_v1_eight_mib_and_overflow_detection_byte_remain_unchanged(stream_name):
    receipt_raw = _raw(_receipt())
    process = _v1_process(receipt_raw)
    process["streams"][stream_name].update(bytes=8388608, overflow=False)
    assert contract.validate_controlled_receipt(receipt_raw, _raw(process), _v1_expected()) == ()
    process["streams"][stream_name].update(bytes=8388609, overflow=True)
    assert contract.parse_process_result(_raw(process))["streams"][stream_name]["bytes"] == 8388609
    assert contract.validate_controlled_receipt(
        receipt_raw, _raw(process), _v1_expected()
    ) == ("PROCESS_STREAM_OVERFLOW",)


@pytest.mark.parametrize("outcome", ["skipped", "xfailed", "xpassed", "not_run", "failed"])
@pytest.mark.parametrize("phase", ["setup", "call", "teardown"])
def test_b1_outcomes_never_auto_accepted(outcome, phase):
    receipt = _receipt()
    receipt["results"][0][phase] = outcome
    receipt_raw = _raw(receipt)
    decision = contract.evaluate_b1_process_slot(
        receipt_raw, _raw(_process(receipt_raw)), _expected(),
        {"controller_returncode": 0, "elapsed_ns": 1},
    )
    assert decision == {
        "schema": "qwq.b1-process-decision/v1", "status": "B1_PROCESS_BOUND", "errors": [],
        "scope": "local_b1_process_only", "outcomes_accepted": False,
        "native_qualified": False, "ci_provenance_verified": False, "production_eligible": False,
    }
    legacy = contract.evaluate_controlled_slot(receipt_raw, _raw(_v1_process(receipt_raw)), _v1_expected())
    assert legacy["status"] == "REJECTED"
    if outcome != "failed":
        assert legacy["errors"] == ["UNSUPPORTED_OUTCOME"]
    else:
        assert legacy["errors"] == [phase.upper() + "_FAILED"]


@pytest.mark.parametrize(
    ("field", "value", "error"),
    [
        ("finished", False, "SESSION_UNFINISHED"),
        ("exit_code", 1, "SESSION_EXIT_NONZERO"),
        ("collection_errors", 1, "COLLECTION_ERRORS"),
        ("deselected", 1, "DESELECTED"),
    ],
)
def test_b1_session_failures_cannot_bind_even_with_matching_receipt_digest(field, value, error):
    receipt = _receipt()
    receipt["session"][field] = value
    receipt_raw = _raw(receipt)
    assert _bind(receipt_raw, _process(receipt_raw)) == (error,)


@pytest.mark.parametrize(
    ("path", "value", "error"),
    [
        (("run", "attempt"), 2, "PROCESS_RUN_MISMATCH"),
        (("slot", "timezone"), "Asia/Seoul", "PROCESS_SLOT_MISMATCH"),
        (("identity", "controller"), "f" * 64, "PROCESS_IDENTITY_MISMATCH"),
        (("identity", "bootstrap"), "f" * 64, "PROCESS_IDENTITY_MISMATCH"),
        (("identity", "executable"), "f" * 64, "PROCESS_IDENTITY_MISMATCH"),
        (("launch", "selection_sha256"), "f" * 64, "PROCESS_LAUNCH_MISMATCH"),
        (("process", "guard"), None, "PROCESS_GUARD_MISMATCH"),
        (("process", "parent_guard"), None, "PROCESS_GUARD_MISMATCH"),
        (("process", "guard", "violations"), 1, "PROCESS_GUARD_MISMATCH"),
        (("process", "parent_guard", "module_count"), 2, "PROCESS_GUARD_MISMATCH"),
        (("receipt", "bytes"), 0, "PROCESS_RECEIPT_MISMATCH"),
        (("receipt", "sha256"), "f" * 64, "PROCESS_RECEIPT_MISMATCH"),
        (("process", "returncode"), 1, "PROCESS_EXIT_REJECTED"),
        (("process", "returncode"), -9, "PROCESS_EXIT_REJECTED"),
        (("process", "term_sent"), True, "PROCESS_EXIT_REJECTED"),
        (("process", "kill_sent"), True, "PROCESS_EXIT_REJECTED"),
        (("process", "cleanup_complete"), False, "PROCESS_CLEANUP_INCOMPLETE"),
        (("process", "ownership_probe_passed"), False, "PROCESS_OWNERSHIP_UNPROVEN"),
        (("process", "descendant_survived"), True, "PROCESS_DESCENDANT_SURVIVED"),
    ],
)
def test_b1_process_facts_are_bound_to_independent_expectation(path, value, error):
    receipt_raw = _raw(_receipt())
    process = _process(receipt_raw)
    _set(process, path, value)
    assert _bind(receipt_raw, process) == (error,)


@pytest.mark.parametrize("key", ["sha", "tree", "contract", "run_id", "attempt"])
def test_b1_receipt_and_process_agreement_cannot_replace_expected_run(key):
    receipt = _receipt()
    value = {"sha": "f" * 40, "tree": "f" * 40, "contract": "f" * 64, "run_id": "other", "attempt": 2}[key]
    receipt["run"][key] = value
    receipt_raw = _raw(receipt)
    process = _process(receipt_raw)
    process["run"][key] = value
    assert _bind(receipt_raw, process) == ("RUN_MISMATCH",)


def test_b1_receipt_runtime_is_bound_to_matching_timezone_slot():
    receipt = _receipt()
    receipt["slot"]["timezone"] = "Asia/Seoul"
    receipt_raw = _raw(receipt)
    process = _process(receipt_raw)
    process["slot"]["timezone"] = "Asia/Seoul"
    assert _bind(receipt_raw, process) == ("IDENTITY_MISMATCH",)
    receipt["identity"]["runtime"] = "2" * 64
    receipt_raw = _raw(receipt)
    process = _process(receipt_raw)
    process["slot"]["timezone"] = "Asia/Seoul"
    assert _bind(receipt_raw, process) == ()


def test_b1_receipt_inventory_and_collected_nodes_must_match_expected():
    receipt = _receipt()
    receipt["collected"] = ["tests/dev/test_other.py::test_other"]
    receipt["results"][0]["nodeid"] = "tests/dev/test_other.py::test_other"
    receipt["identity"]["inventory"] = hashlib.sha256(b'["tests/dev/test_other.py::test_other"]').hexdigest()
    receipt_raw = _raw(receipt)
    assert _bind(receipt_raw, _process(receipt_raw)) == ("IDENTITY_MISMATCH", "NODE_SET_MISMATCH")
    receipt["identity"]["inventory"] = "f" * 64
    receipt_raw = _raw(receipt)
    assert _bind(receipt_raw, _process(receipt_raw)) == ("INVALID_RECEIPT",)


@pytest.mark.parametrize("key", ["producer", "guard"])
def test_b1_forged_matching_process_identity_still_binds_receipt(key):
    receipt_raw = _raw(_receipt())
    process = _process(receipt_raw)
    expected = _expected()
    process["identity"][key] = "f" * 64
    expected["process_identity"][key] = "f" * 64
    error = {"producer": "PROCESS_IDENTITY_MISMATCH", "guard": "PROCESS_GUARD_MISMATCH"}[key]
    assert _bind(receipt_raw, process, expected) == (error,)


@pytest.mark.parametrize("change", ["producer", "guard"])
def test_b1_all_observations_agreeing_still_require_independent_receipt_identity(change):
    receipt = _receipt()
    if change == "producer":
        receipt["identity"]["producer"] = "f" * 64
    else:
        receipt["guard"]["sha256"] = "f" * 64
    receipt_raw = _raw(receipt)
    process = _process(receipt_raw)
    expected = _expected()
    if change == "producer":
        process["identity"]["producer"] = "f" * 64
        expected["process_identity"]["producer"] = "f" * 64
    else:
        process["identity"]["guard"] = "f" * 64
        expected["process_identity"]["guard"] = "f" * 64
        process["process"]["guard"]["sha256"] = "f" * 64
        process["process"]["parent_guard"]["sha256"] = "f" * 64
    assert _bind(receipt_raw, process, expected) == ({"producer": "IDENTITY_MISMATCH", "guard": "GUARD_MISMATCH"}[change],)


@pytest.mark.parametrize("change", ["source-proof", "push"])
def test_b1_scope_is_never_expanded_by_matching_expected(change):
    receipt = _receipt()
    expected = _expected()
    if change == "source-proof":
        receipt["slot"]["lane"] = "source-proof"
        receipt["identity"]["runtime"] = "3" * 64
    else:
        receipt["run"]["event"] = "push"
        expected["verification"]["run"]["event"] = "push"
    receipt_raw = _raw(receipt)
    process = _process(receipt_raw)
    if change == "source-proof":
        process["slot"]["lane"] = "source-proof"
    else:
        process["run"]["event"] = "push"
    assert _bind(receipt_raw, process, expected) == ("PROCESS_SCOPE_MISMATCH",)


@pytest.mark.parametrize("reason", [
    "signaled", "timeout", "interrupted", "startup_error", "output_limit",
    "cleanup_error", "identity_changed", "io_error", "lock_timeout",
])
def test_b1_failure_reasons_remain_parseable_but_never_bind(reason):
    receipt_raw = _raw(_receipt())
    process = _process(receipt_raw)
    process["process"]["reason"] = reason
    assert contract.parse_b1_process_result(_raw(process))["process"]["reason"] == reason
    assert _bind(receipt_raw, process) == ("PROCESS_EXIT_REJECTED",)


def test_b1_lock_timeout_without_child_or_key_preserves_failure_facts():
    receipt_raw = _raw(_receipt())
    process = _process(receipt_raw)
    process["process"].update(
        reason="lock_timeout", returncode=None, leader_reaped=False, ownership_probe_passed=False,
        guard=None, parent_guard=None,
    )
    process["coordination"] = {
        "lock_acquired": False, "lock_identity_stable": False,
        "fake_key_absent_before": False, "fake_key_absent_after": False,
        "fake_directory_removed": False, "fake_key_path_sha256": None,
    }
    process["receipt"] = {"state": "missing", "bytes": 0, "sha256": None}
    assert contract.parse_b1_process_result(_raw(process))["process"]["returncode"] is None
    assert _bind(receipt_raw, process) == (
        "B1_COORDINATION_UNPROVEN", "PROCESS_CLEANUP_INCOMPLETE", "PROCESS_EXIT_REJECTED",
        "PROCESS_GUARD_MISMATCH", "PROCESS_OWNERSHIP_UNPROVEN", "PROCESS_RECEIPT_MISMATCH",
    )
    legacy = _v1_process(receipt_raw)
    legacy["process"]["reason"] = "lock_timeout"
    with pytest.raises(contract.ProcessEvidenceError):
        contract.parse_process_result(_raw(legacy))


@pytest.mark.parametrize("state", ["missing", "invalid"])
def test_b1_nonregular_receipt_fact_never_binds(state):
    receipt_raw = _raw(_receipt())
    process = _process(receipt_raw)
    process["receipt"] = {"state": state, "bytes": 0, "sha256": None}
    assert _bind(receipt_raw, process) == ("PROCESS_RECEIPT_MISMATCH",)


@pytest.mark.parametrize("path", [(), ("process_identity",), ("launch",)])
def test_b1_direct_expectation_unknown_fields_rejected(path):
    expected = _expected()
    target = expected
    for key in path:
        target = target[key]
    target["unknown"] = 0
    receipt_raw = _raw(_receipt())
    assert _bind(receipt_raw, _process(receipt_raw), expected) == ("INVALID_EXPECTATION",)


def test_b1_direct_expectation_does_not_trust_custom_equality_or_integer_subclasses():
    class EqualToProfile:
        def __eq__(self, other):
            return other == "b1-standard/v1"

    class Counter(int):
        pass

    receipt_raw = _raw(_receipt())
    for field, value in (("profile", EqualToProfile()), ("timeout_seconds", Counter(900))):
        expected = _expected()
        expected["launch"][field] = value
        assert _bind(receipt_raw, _process(receipt_raw), expected) == ("INVALID_EXPECTATION",)
    assert contract.validate_b1_process_receipt(
        receipt_raw, _raw(_process(receipt_raw)), _expected(),
        {"controller_returncode": Counter(0), "elapsed_ns": 1},
    ) == ("INVALID_INVOCATION",)


@pytest.mark.parametrize("value", [None, [], {}, "invalid"])
def test_b1_invalid_direct_expectation_normalized(value):
    receipt_raw = _raw(_receipt())
    assert contract.validate_b1_process_receipt(
        receipt_raw, _raw(_process(receipt_raw)), value,
        {"controller_returncode": 0, "elapsed_ns": 1},
    ) == ("INVALID_EXPECTATION",)


def test_b1_deep_or_cyclic_direct_expectation_normalized():
    receipt_raw = _raw(_receipt())
    cycle = {}
    cycle["self"] = cycle
    deep = 0
    for _ in range(10000):
        deep = [deep]
    for value in (cycle, deep):
        expected = _expected()
        expected["verification"] = value
        assert _bind(receipt_raw, _process(receipt_raw), expected) == ("INVALID_EXPECTATION",)


def test_b1_rejected_decision_sorts_deduplicates_errors_and_never_promotes():
    receipt_raw = _raw(_receipt())
    process = _process(receipt_raw)
    process["process"]["cleanup_complete"] = False
    process["process"]["term_sent"] = True
    process["process"]["kill_sent"] = True
    process["coordination"]["lock_acquired"] = False
    assert contract.evaluate_b1_process_slot(
        receipt_raw, _raw(process), _expected(),
        {"controller_returncode": 125, "elapsed_ns": 900000000001},
    ) == {
        "schema": "qwq.b1-process-decision/v1", "status": "REJECTED",
        "errors": [
            "B1_CONTROLLER_EXIT_REJECTED", "B1_COORDINATION_UNPROVEN", "B1_TOTAL_BUDGET_EXCEEDED",
            "PROCESS_CLEANUP_INCOMPLETE", "PROCESS_EXIT_REJECTED",
        ],
        "scope": "local_b1_process_only", "outcomes_accepted": False,
        "native_qualified": False, "ci_provenance_verified": False, "production_eligible": False,
    }


def test_b1_multiple_malformed_inputs_report_fixed_sorted_classes():
    assert contract.validate_b1_process_receipt(b"{", b"{}", {}, None) == (
        "INVALID_EXPECTATION", "INVALID_INVOCATION", "INVALID_PROCESS_RESULT", "INVALID_RECEIPT",
    )
