"""크기 제한 오프라인 verification decision CLI의 블랙박스 시험이다."""

import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import sysconfig
import textwrap

import pytest


ROOT = Path(__file__).resolve().parents[2]
CLI = "scripts.dev.check_verification_evidence"
RUN = {
    "event": "local",
    "sha": "a" * 40,
    "tree": "b" * 40,
    "contract": "c" * 64,
    "run_id": "offline-contract",
    "attempt": 1,
}
NODEIDS = ["tests/test_case.py::test_ok"]
SYNTHETIC_PERFORMANCE_CASES = [
    (
        "tests/test_recovery_projection.py::test_every_real_advance_call_including_finalization_stays_below_5ms[controlled_work-5000]",
        "038aa599f043e62d200df048152fa292167fea72",
    ),
    (
        "tests/test_recovery_projection.py::test_every_real_advance_call_including_finalization_stays_below_5ms[controlled_work-100000]",
        "7208acc8358e1c08bd7725d5d64d5059c24cd65b",
    ),
]
DECISION = {
    "schema": "qwq.verification-decision/v1",
    "status": "EVIDENCE_CONSISTENT",
    "errors": [],
    "scope": "offline_evidence_only",
    "production_eligible": False,
}


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _expected_document() -> dict:
    """영수증을 만들기 전에 알려진 자식 identity를 독립적으로 계산한다."""
    inventory = hashlib.sha256(_canonical(NODEIDS)).hexdigest()
    runtime = {
        "implementation": sys.implementation.name,
        "version": sys.version,
        "soabi": sysconfig.get_config_var("SOABI"),
        "machine": platform.machine(),
        "executable_sha256": _sha256_file(Path(sys.executable)),
    }
    identity = {
        "runtime": hashlib.sha256(_canonical(runtime)).hexdigest(),
        "producer": _sha256_file(ROOT / "scripts/dev/pytest_evidence.py"),
        "inventory": inventory,
    }
    guard = {
        "path": "tests/conftest.py",
        "sha256": _sha256_file(ROOT / "tests/conftest.py"),
        "module_count": 1,
        "violations": 0,
    }
    return {
        "schema": "qwq.verification-expectation/v1",
        "run": RUN,
        "slots": [
            {
                "slot": {"lane": lane, "timezone": timezone},
                "identity": identity.copy(),
                "nodes": NODEIDS.copy(),
                "allowed_outcomes": {},
                "guard": guard.copy(),
            }
            for lane in ("standard", "source-proof")
            for timezone in ("UTC", "Asia/Seoul")
        ],
    }


def _safe_env(timezone: str) -> dict[str, str]:
    return {
        "PATH": "/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "TZ": timezone,
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
    }


def _synthetic_performance_documents(nodeid: str, candidate_head: str) -> tuple[dict, list[dict]]:
    """실행하지 않은 성능 사례의 consumer 입력만 독립적으로 구성한다."""
    nodes = [nodeid]
    inventory = hashlib.sha256(_canonical(nodes)).hexdigest()
    run = RUN | {
        "sha": candidate_head,
        "tree": candidate_head,
        "run_id": "synthetic-performance-consumer",
    }
    expected = {
        "schema": "qwq.verification-expectation/v1",
        "run": run,
        "slots": [
            {
                "slot": {"lane": lane, "timezone": timezone},
                "identity": {
                    "runtime": hashlib.sha256(
                        f"synthetic-runtime-{lane}-{timezone}".encode("utf-8")
                    ).hexdigest(),
                    "producer": hashlib.sha256(b"synthetic-consumer-producer").hexdigest(),
                    "inventory": inventory,
                },
                "nodes": nodes,
                "allowed_outcomes": {},
                "guard": {
                    "path": "tests/conftest.py",
                    "sha256": "e" * 64,
                    "module_count": 1,
                    "violations": 0,
                },
            }
            for lane in ("standard", "source-proof")
            for timezone in ("UTC", "Asia/Seoul")
        ],
    }
    receipts = []
    for target in expected["slots"]:
        receipts.append(
            {
                "schema": "qwq.verification-receipt/v1",
                "run": run.copy(),
                "slot": target["slot"].copy(),
                "identity": target["identity"].copy(),
                "collected": nodes.copy(),
                "results": [
                    {"nodeid": nodeid, "setup": "passed", "call": "failed", "teardown": "passed"}
                ],
                "session": {
                    "finished": True,
                    "exit_code": 1,
                    "collection_errors": 0,
                    "deselected": 0,
                },
                "guard": target["guard"].copy(),
            }
        )
    return expected, receipts


def _run_child(command: list[str], *, cwd: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    with subprocess.Popen(
        command,
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    ) as child:
        try:
            stdout, stderr = child.communicate(timeout=30)
        except subprocess.TimeoutExpired:
            child.kill()
            stdout, stderr = child.communicate()
            pytest.fail("tiny child timeout")
    return subprocess.CompletedProcess(command, child.returncode, stdout, stderr)


def _produce_receipt(parent: Path, *, lane: str, timezone: str) -> Path:
    repo = parent / f"{lane}-{timezone.replace('/', '-') }"
    tests = repo / "tests"
    tests.mkdir(parents=True)
    # 복사본은 제품 import 전에 로드하는 실제 격리 guard와 정확히 같다.
    (tests / "conftest.py").write_bytes((ROOT / "tests/conftest.py").read_bytes())
    (tests / "test_case.py").write_text("def test_ok():\n    pass\n", encoding="utf-8")
    (repo / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    context = repo / "context.json"
    context.write_bytes(_canonical({"run": RUN, "slot": {"lane": lane, "timezone": timezone}}))
    output = repo / "receipt.json"
    pytest_args = [
        "-c", str(repo / "pytest.ini"), "--rootdir", str(repo), "-q", "-p", "no:cacheprovider",
        "--tb=short", str(tests),
    ]
    code = f"""
import importlib.util
import runpy
import sys
sys.path.insert(0, {str(ROOT)!r})
spec = importlib.util.spec_from_file_location('_bootstrap_guard', {str(ROOT / 'tests/conftest.py')!r})
guard = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = guard
spec.loader.exec_module(guard)
sys.argv = ['pytest_evidence', '--verification-context', {str(context)!r}, '--verification-output', {str(output)!r}, '--', *{pytest_args!r}]
runpy.run_module('scripts.dev.pytest_evidence', run_name='__main__')
"""
    result = _run_child([sys.executable, "-c", textwrap.dedent(code)], cwd=repo, env=_safe_env(timezone))
    assert result.returncode == 0, result.stdout + result.stderr
    assert output.is_file()
    return output


@pytest.fixture
def actual_artifacts(tmp_path):
    # 영수증 생성 전 실행하며 expected를 영수증 bytes에서 파생하지 않는다.
    expected = _expected_document()
    expected_path = tmp_path / "expected.json"
    expected_path.write_bytes(_canonical(expected))
    receipts = [
        _produce_receipt(tmp_path, lane=lane, timezone=timezone)
        for lane in ("standard", "source-proof")
        for timezone in ("UTC", "Asia/Seoul")
    ]
    return expected_path, receipts


def _run_cli(expected: Path, receipts: list[Path]) -> subprocess.CompletedProcess[str]:
    command = [sys.executable, "-m", CLI, "--expected", str(expected)]
    for receipt in receipts:
        command.extend(("--receipt", str(receipt)))
    return _run_child(command, cwd=ROOT, env=_safe_env("UTC"))


def _decision(result: subprocess.CompletedProcess[str]) -> dict:
    assert result.stderr == ""
    assert result.stdout.count("\n") == 1
    return json.loads(result.stdout)


def test_cli_accepts_four_real_pytest_receipts_for_two_lanes_and_timezones(actual_artifacts):
    expected, receipts = actual_artifacts

    result = _run_cli(expected, receipts)

    assert result.returncode == 0
    assert _decision(result) == DECISION


@pytest.mark.parametrize(
    ("case", "expected_rc"),
    [("missing", 2), ("duplicate", 1), ("other-run", 1), ("truncated", 1), ("oversize", 2), ("nonregular", 2)],
)
def test_cli_rejects_missing_duplicate_invalid_or_unreadable_inputs(actual_artifacts, case, expected_rc):
    expected, receipts = actual_artifacts
    selected = receipts.copy()
    if case == "missing":
        selected.pop()
    elif case == "duplicate":
        selected[-1] = selected[0]
    elif case == "other-run":
        payload = json.loads(selected[0].read_text(encoding="utf-8"))
        payload["run"]["attempt"] = 2
        changed = selected[0].with_name("other-run.json")
        changed.write_bytes(_canonical(payload))
        selected[0] = changed
    elif case == "truncated":
        changed = selected[0].with_name("truncated.json")
        changed.write_bytes(b'{"schema":')
        selected[0] = changed
    elif case == "oversize":
        changed = selected[0].with_name("oversize.json")
        changed.write_bytes(b"x" * (32 * 1024 * 1024 + 1))
        selected[0] = changed
    else:
        changed = selected[0].with_name("fifo-input")
        os.mkfifo(changed)
        selected[0] = changed

    result = _run_cli(expected, selected)

    assert result.returncode == expected_rc
    decision = _decision(result)
    assert decision["status"] == "REJECTED"
    assert decision["production_eligible"] is False
    assert all(str(path) not in result.stdout for path in [expected, *selected])


@pytest.mark.parametrize(("nodeid", "candidate_head"), SYNTHETIC_PERFORMANCE_CASES)
def test_synthetic_ledger_identified_failure_remains_rejected_by_consumer(tmp_path, nodeid, candidate_head):
    expected, receipts = _synthetic_performance_documents(nodeid, candidate_head)
    expected_path = tmp_path / "synthetic-expected.json"
    expected_path.write_bytes(_canonical(expected))
    receipt_paths = []
    for index, receipt in enumerate(receipts):
        path = tmp_path / f"synthetic-receipt-{index}.json"
        path.write_bytes(_canonical(receipt))
        receipt_paths.append(path)

    result = _run_cli(expected_path, receipt_paths)

    assert result.returncode == 1
    assert _decision(result)["errors"] == ["CALL_FAILED", "SESSION_EXIT_NONZERO"]


def test_direct_main_rejects_embedded_nul_path_with_fixed_decision(capsys):
    from scripts.dev.check_verification_evidence import main

    rc = main(["--expected", "bad\0path", *sum((["--receipt", "unused"] for _ in range(4)), [])])

    captured = capsys.readouterr()
    assert rc == 2
    assert captured.err == ""
    assert json.loads(captured.out) == {
        "schema": "qwq.verification-decision/v1",
        "status": "REJECTED",
        "errors": ["INPUT_OPEN_ERROR"],
        "scope": "offline_evidence_only",
        "production_eligible": False,
    }
