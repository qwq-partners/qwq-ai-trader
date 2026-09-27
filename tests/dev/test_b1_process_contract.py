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
        ("timeout_seconds", 899),
        ("timeout_seconds", True),
        ("retained_stream_bytes", 2097151),
        ("retained_stream_bytes", 2097153),
        ("retained_stream_bytes", 8388608),
        ("retained_stream_bytes", True),
        ("overflow_observed_bytes", 2097152),
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
