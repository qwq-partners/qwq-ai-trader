"""Black-box tests for the bounded offline verification decision CLI."""

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
    """Hand-derive all known child identities before producing any receipt."""
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
    # The copied file is exactly the real isolation guard, loaded before product imports.
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
    # This happens before receipts are generated; it never derives expected from receipt bytes.
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


def test_failing_receipt_is_not_turned_into_success_by_existing_performance_exception_ledger(actual_artifacts):
    expected, receipts = actual_artifacts
    assert (ROOT / "docs/reviews/recovery-performance-exceptions-2026-09-27.json").is_file()
    failed = json.loads(receipts[0].read_text(encoding="utf-8"))
    failed["results"][0]["call"] = "failed"
    failed_path = receipts[0].with_name("failing-receipt.json")
    failed_path.write_bytes(_canonical(failed))
    receipts[0] = failed_path

    result = _run_cli(expected, receipts)

    assert result.returncode == 1
    assert "CALL_FAILED" in _decision(result)["errors"]


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
