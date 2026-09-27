"""실제 producer→OS controller→순수 consumer 결합 인수 시험."""

import hashlib
import importlib.util
import json
from pathlib import Path
import platform
import sys
import sysconfig

from scripts.dev.verification_os_contract import evaluate_controlled_slot
from scripts.dev.verification_contract import parse_document, validate_receipt


ROOT = Path(__file__).resolve().parents[2]
HELPER_PATH = ROOT / "tests/dev/test_pytest_evidence_controller.py"
NODE = "tests/test_tiny.py::test_ok"
SELECTION = ["tests/test_tiny.py"]
RUN = {
    "event": "local",
    "sha": "1" * 40,
    "tree": "2" * 40,
    "contract": "3" * 64,
    "run_id": "os-fixture",
    "attempt": 1,
}


def _load_controller_test_helpers():
    """시험 함수는 수집하지 않고 안정된 두 helper만 모듈 객체로 재사용한다."""
    spec = importlib.util.spec_from_file_location("_controlled_os_test_helpers", HELPER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.ROOT == ROOT
    return module


HELPERS = _load_controller_test_helpers()


def _canonical(value, *, sort_keys=False):
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=sort_keys,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _file_digest(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _runtime_digest(executable):
    """producer helper를 호출하지 않고 공개 필드의 v1 알고리즘을 독립 계산한다."""
    runtime = {
        "implementation": sys.implementation.name,
        "version": sys.version,
        "soabi": sysconfig.get_config_var("SOABI"),
        "machine": platform.machine(),
        "executable_sha256": _file_digest(executable),
    }
    return hashlib.sha256(_canonical(runtime, sort_keys=True)).hexdigest()


def _expected_before_run(repo, run):
    """실행 전 literal node/context와 고정 파일 bytes로 기대 문서를 만든다."""
    executable = Path(sys.executable)
    inventory = hashlib.sha256(_canonical([NODE], sort_keys=True)).hexdigest()
    producer = _file_digest(repo / "scripts/dev/pytest_evidence.py")
    guard_hash = _file_digest(repo / "tests/conftest.py")
    identity = {
        "runtime": _runtime_digest(executable),
        "producer": producer,
        "inventory": inventory,
    }
    guard = {
        "path": "tests/conftest.py",
        "sha256": guard_hash,
        "module_count": 1,
        "violations": 0,
    }
    verification = {
        "schema": "qwq.verification-expectation/v1",
        "run": run.copy(),
        "slots": [
            {
                "slot": {"lane": lane, "timezone": slot_timezone},
                "identity": identity.copy(),
                "nodes": [NODE],
                "allowed_outcomes": {},
                "guard": guard.copy(),
            }
            for lane in ("standard", "source-proof")
            for slot_timezone in ("UTC", "Asia/Seoul")
        ],
    }
    return {
        "verification": verification,
        "process_identity": {
            "controller": _file_digest(repo / "scripts/dev/pytest_evidence_controller.py"),
            "bootstrap": _file_digest(repo / "scripts/dev/pytest_evidence_bootstrap.py"),
            "producer": producer,
            "guard": guard_hash,
            "executable": _file_digest(executable),
        },
        "launch": {
            "profile": "pytest-evidence-bootstrap/v1",
            "process_scope": "linux-subreaper/v1",
            "timeout_seconds": 3,
            "selection_sha256": hashlib.sha256(_canonical(SELECTION)).hexdigest(),
        },
    }


def _prepare_case(tmp_path, *, body="def test_ok():\n    assert True\n", timezone="UTC", run=None):
    repo = HELPERS.make_repo(tmp_path, body)
    literal_run = (RUN if run is None else run).copy()
    context = {"run": literal_run.copy(), "slot": {"lane": "standard", "timezone": timezone}}
    (repo / "context.json").write_bytes(_canonical(context))
    expected = _expected_before_run(repo, literal_run)
    return repo, expected


def _observed(repo):
    receipt_raw = (repo / "receipt.json").read_bytes()
    process_raw = (repo / "process.json").read_bytes()
    process = json.loads(process_raw)
    for name in ("stdout", "stderr"):
        log = (repo / f"process.json.{name}.log").read_bytes()
        assert process["streams"][name] == {
            "bytes": len(log),
            "sha256": hashlib.sha256(log).hexdigest(),
            "overflow": False,
        }
    assert process["receipt"] == {
        "state": "regular",
        "bytes": len(receipt_raw),
        "sha256": hashlib.sha256(receipt_raw).hexdigest(),
    }
    return receipt_raw, process_raw, process


def _decision(repo, expected):
    receipt_raw, process_raw, process = _observed(repo)
    return evaluate_controlled_slot(receipt_raw, process_raw, expected), process


def test_actual_tiny_utc_and_kst_bind_without_native_ci_or_production_authority(tmp_path):
    for timezone in ("UTC", "Asia/Seoul"):
        repo, expected = _prepare_case(tmp_path / timezone.replace("/", "-"), timezone=timezone)

        assert HELPERS.run_case(repo)["rc"] == 0
        decision, process = _decision(repo, expected)

        assert decision == {
            "schema": "qwq.verification-os-decision/v1",
            "status": "OS_RESULT_BOUND",
            "errors": [],
            "scope": "local_os_process_only",
            "native_qualified": False,
            "ci_provenance_verified": False,
            "production_eligible": False,
        }
        assert process["slot"] == {"lane": "standard", "timezone": timezone}
        assert process["process"]["reason"] == "exited"
        assert process["process"]["returncode"] == 0


def test_actual_pytest_failure_and_postreceipt_os_faults_are_rejected(tmp_path):
    cases = (
        ("failure", "def test_ok():\n    assert False\n", 1, "exited", 1, False),
        ("exit-nine", "import atexit, os\ndef test_ok():\n    atexit.register(lambda: os._exit(9))\n", 9, "exited", 9, False),
        ("signal", "import atexit, signal\ndef test_ok():\n    atexit.register(lambda: signal.raise_signal(signal.SIGTERM))\n", 143, "signaled", -15, False),
        ("hang", "import atexit, time\ndef test_ok():\n    atexit.register(lambda: time.sleep(7))\n", 124, "timeout", -15, False),
        (
            "setsid-descendant",
            """import atexit, os, signal, time
def spawn():
    signal.signal(signal.SIGALRM, signal.SIG_DFL)
    signal.alarm(8)
    if os.fork() == 0:
        signal.alarm(8)
        os.setsid()
        time.sleep(7)
        os._exit(0)
def test_ok():
    atexit.register(spawn)
""",
            125,
            "cleanup_error",
            0,
            True,
        ),
    )
    for name, body, harness_rc, reason, returncode, descendant_survived in cases:
        repo, expected = _prepare_case(tmp_path / name, body=body)

        result = HELPERS.run_case(repo)
        receipt_raw, process_raw, process = _observed(repo)
        decision = evaluate_controlled_slot(receipt_raw, process_raw, expected)

        assert result["rc"] == harness_rc
        assert decision["status"] == "REJECTED"
        assert "PROCESS_EXIT_REJECTED" in decision["errors"]
        assert process["process"]["reason"] == reason
        assert process["process"]["returncode"] == returncode
        assert process["process"]["descendant_survived"] is descendant_survived
        assert process["process"]["cleanup_complete"] is True
        receipt = parse_document(receipt_raw, kind="receipt")
        if name == "failure":
            assert receipt["collected"] == [NODE]
            assert receipt["results"] == [
                {"nodeid": NODE, "setup": "passed", "call": "failed", "teardown": "passed"}
            ]
            assert validate_receipt(receipt, expected["verification"]) == (
                "CALL_FAILED",
                "SESSION_EXIT_NONZERO",
            )
            assert decision["errors"] == [
                "CALL_FAILED",
                "PROCESS_EXIT_REJECTED",
                "SESSION_EXIT_NONZERO",
            ]
        else:
            assert validate_receipt(receipt, expected["verification"]) == ()
        if descendant_survived:
            assert "PROCESS_DESCENDANT_SURVIVED" in decision["errors"]
            assert result["complete"] is True


def test_actual_unsupported_outcomes_and_crossrun_expectation_remain_rejected(tmp_path):
    outcome_cases = (
        ("skip", "import pytest\n@pytest.mark.skip\ndef test_ok():\n    pass\n"),
        ("strict-xfail", "import pytest\n@pytest.mark.xfail(strict=True)\ndef test_ok():\n    assert False\n"),
    )
    for name, body in outcome_cases:
        repo, expected = _prepare_case(tmp_path / name, body=body)

        assert HELPERS.run_case(repo)["rc"] == 0
        decision, process = _decision(repo, expected)

        assert decision["status"] == "REJECTED"
        assert decision["errors"] == ["UNSUPPORTED_OUTCOME"]
        assert process["process"]["returncode"] == 0

    run_a = RUN | {"run_id": "os-fixture-crossrun-a"}
    run_b = RUN | {"run_id": "os-fixture-crossrun-b", "attempt": 2}
    repo_a, expected_a = _prepare_case(tmp_path / "crossrun-a", run=run_a)
    repo_b, expected_b = _prepare_case(tmp_path / "crossrun-b", run=run_b)

    assert HELPERS.run_case(repo_a)["rc"] == 0
    assert HELPERS.run_case(repo_b)["rc"] == 0
    receipt_a, process_a, document_a = _observed(repo_a)
    receipt_b, process_b, document_b = _observed(repo_b)
    assert evaluate_controlled_slot(receipt_a, process_a, expected_a)["status"] == "OS_RESULT_BOUND"
    assert evaluate_controlled_slot(receipt_b, process_b, expected_b)["status"] == "OS_RESULT_BOUND"

    crossrun = evaluate_controlled_slot(receipt_a, process_b, expected_a)
    assert document_a["run"] == run_a
    assert document_b["run"] == run_b
    assert crossrun["status"] == "REJECTED"
    assert crossrun["errors"] == ["PROCESS_RECEIPT_MISMATCH", "PROCESS_RUN_MISMATCH"]
