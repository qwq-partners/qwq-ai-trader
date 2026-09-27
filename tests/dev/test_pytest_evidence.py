"""실제 작은 pytest 자식으로 evidence의 종료·격리 경계를 검증한다."""

import hashlib
import json
from pathlib import Path
import platform
import subprocess
import sys
import sysconfig
import textwrap

import pytest


ROOT = Path(__file__).resolve().parents[2]
RUN = {
    "event": "local", "sha": "a" * 40, "tree": "b" * 40,
    "contract": "c" * 64, "run_id": "tiny-run", "attempt": 1,
}


def run_case(tmp_path, source="def test_ok():\n    pass\n", *, hooks="",
             args=(), timezone="UTC", context_timezone="UTC", env_extra=None,
             bootstrap="", existing=False, outside=False, context_raw=None):
    repo = tmp_path / "repo"
    repo.mkdir()
    tests = repo / "tests"
    tests.mkdir()
    (tests / "conftest.py").write_bytes((ROOT / "tests/conftest.py").read_bytes())
    (tests / "test_case.py").write_text(textwrap.dedent(source), encoding="utf-8")
    (repo / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    if hooks:
        (repo / "conftest.py").write_text(textwrap.dedent(hooks), encoding="utf-8")
    context = repo / "context.json"
    context.write_text(context_raw if context_raw is not None else json.dumps({
        "run": RUN, "slot": {"lane": "standard", "timezone": context_timezone},
    }), encoding="utf-8")
    output = (tmp_path if outside else repo) / "receipt.json"
    if existing:
        output.write_text("do not overwrite", encoding="utf-8")
    pytest_args = ["-c", str(repo / "pytest.ini"), "--rootdir", str(repo),
                   "-q", "-p", "no:cacheprovider", "--tb=short", *args, str(tests)]
    # 모든 자식은 실제 저장소 guard부터 설치한다. HOME/운영 환경은 전달하지 않는다.
    code = f"""
import importlib.util, sys, runpy
from pathlib import Path
sys.path.insert(0, {str(ROOT)!r})
spec = importlib.util.spec_from_file_location('_bootstrap_guard', {str(ROOT / 'tests/conftest.py')!r})
guard = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = guard
spec.loader.exec_module(guard)
{textwrap.dedent(bootstrap)}
sys.argv = ['pytest_evidence', '--verification-context', {str(context)!r}, '--verification-output', {str(output)!r}, '--', *{pytest_args!r}]
runpy.run_module('scripts.dev.pytest_evidence', run_name='__main__')
"""
    env = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8",
           "PYTHONDONTWRITEBYTECODE": "1", "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"}
    if timezone is not None:
        env["TZ"] = timezone
    env.update(env_extra or {})
    with subprocess.Popen([sys.executable, "-c", code], cwd=repo, env=env,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) as child:
        try:
            stdout, stderr = child.communicate(timeout=30)
        except subprocess.TimeoutExpired:
            child.kill()
            stdout, stderr = child.communicate()
            pytest.fail(f"tiny child timeout; stdout={stdout!r}; stderr={stderr!r}")
    receipt = json.loads(output.read_text()) if output.exists() and not existing else None
    return child.returncode, receipt, stdout, stderr, repo


def test_real_pass_and_failure_keep_pytest_exit_codes(tmp_path):
    for name, body, expected in [("pass", "pass", 0), ("fail", "assert False", 1)]:
        case = tmp_path / name
        case.mkdir()
        rc, receipt, out, err, repo = run_case(case, f"def test_ok():\n    {body}\n")
        assert rc == expected, out + err
        assert receipt is not None, err
        assert receipt["session"] == {"finished": True, "exit_code": expected,
                                      "collection_errors": 0, "deselected": 0}
        assert receipt["results"] == [{"nodeid": "tests/test_case.py::test_ok",
            "setup": "passed", "call": "passed" if expected == 0 else "failed", "teardown": "passed"}]
        assert receipt["guard"] == {"path": "tests/conftest.py", "module_count": 1,
            "violations": 0, "sha256": hashlib.sha256((repo / "tests/conftest.py").read_bytes()).hexdigest()}
        assert receipt["run"] == RUN
        assert set(receipt) == {"schema", "run", "slot", "identity", "collected", "results", "session", "guard"}
        assert receipt["schema"] == "qwq.verification-receipt/v1"
        assert receipt["identity"]["inventory"] == hashlib.sha256(b'["tests/test_case.py::test_ok"]').hexdigest()
        assert receipt["identity"]["producer"] == hashlib.sha256((ROOT / "scripts/dev/pytest_evidence.py").read_bytes()).hexdigest()
        runtime = {"implementation": sys.implementation.name, "version": sys.version,
                   "soabi": sysconfig.get_config_var("SOABI"), "machine": platform.machine(),
                   "executable_sha256": hashlib.sha256(Path(sys.executable).read_bytes()).hexdigest()}
        assert receipt["identity"]["runtime"] == hashlib.sha256(json.dumps(
            runtime, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")).hexdigest()
        assert "접근 시도 0건" in out


@pytest.mark.parametrize(("source", "phase", "outcome", "rc"), [
    ("import pytest\n@pytest.fixture(autouse=True)\ndef bad():\n    assert False\ndef test_ok():\n    pass\n", "setup", "failed", 1),
    ("import pytest\n@pytest.fixture(autouse=True)\ndef bad():\n    yield\n    assert False\ndef test_ok():\n    pass\n", "teardown", "failed", 1),
    ("import pytest\n@pytest.mark.xfail(strict=True)\ndef test_ok():\n    assert False\n", "call", "xfailed", 0),
    ("import pytest\n@pytest.mark.xfail(strict=True)\ndef test_ok():\n    pass\n", "call", "xpassed", 1),
    ("import pytest\n@pytest.mark.xfail\ndef test_ok():\n    pass\n", "call", "xpassed", 0),
    ("import pytest\n@pytest.mark.skip\ndef test_ok():\n    pass\n", "setup", "skipped", 0),
])
def test_records_actual_phases(tmp_path, source, phase, outcome, rc):
    actual, receipt, out, err, _ = run_case(tmp_path, source)
    assert actual == rc, out + err
    assert receipt["results"][0][phase] == outcome
    if phase == "setup":
        assert receipt["results"][0]["call"] == "not_run"


def test_collection_errors_are_visible(tmp_path):
    rc, receipt, out, err, _ = run_case(tmp_path, "raise ValueError('synthetic collection error')\n")
    assert rc == 2, out + err
    assert receipt["session"]["collection_errors"] == 1
    assert receipt["session"]["exit_code"] == 2


def test_deselection_is_visible(tmp_path):
    rc, receipt, out, err, _ = run_case(tmp_path, args=("-k", "missing"))
    assert rc == 5, out + err
    assert receipt["collected"] == []
    assert receipt["session"]["deselected"] == 1


def test_mid_session_exit_keeps_unrun_phases(tmp_path):
    rc, receipt, out, err, _ = run_case(tmp_path, "import pytest\ndef test_ok():\n    pytest.exit('synthetic stop', returncode=7)\n")
    assert rc == 7, out + err
    assert receipt["results"][0]["call"] == "not_run"
    assert receipt["session"]["exit_code"] == 7


def test_swallowed_guard_violation_is_recorded(tmp_path):
    rc, receipt, out, err, _ = run_case(tmp_path, "import conftest, sys\ndef test_ok():\n    conftest.VIOLATIONS.append('synthetic-only')\n    assert sys.modules['_bootstrap_guard'].VIOLATIONS == []\n")
    assert rc == 0, out + err
    assert receipt["guard"]["violations"] == 1
    assert "synthetic-only" not in json.dumps(receipt)


@pytest.mark.parametrize(("mode", "count"), [("missing", 0), ("wrong", 0), ("alias", 1), ("duplicate", 2)])
def test_guard_identity_uses_exact_file_and_distinct_objects(tmp_path, mode, count):
    source = "def test_ok():\n    pass\n"
    args = ("--noconftest",) if mode in {"missing", "wrong"} else ()
    bootstrap = ""
    if mode == "wrong":
        bootstrap = "sys.modules['conftest'] = guard"
    if mode in {"alias", "duplicate"}:
        source = "import conftest, sys, types\ndef test_ok():\n    sys.modules['synthetic_guard_alias'] = " + (
            "conftest\n" if mode == "alias" else "types.SimpleNamespace(__file__=conftest.__file__, VIOLATIONS=[])\n")
    rc, receipt, out, err, _ = run_case(tmp_path, source, args=args, bootstrap=bootstrap)
    assert rc == 0, out + err
    assert receipt["guard"]["module_count"] == count


def test_existing_output_is_never_overwritten(tmp_path):
    rc, receipt, out, err, repo = run_case(tmp_path, existing=True)
    assert rc == 2, out + err
    assert (repo / "receipt.json").read_text() == "do not overwrite"
    assert receipt is None
    assert "EVIDENCE_OUTPUT" in err


@pytest.mark.parametrize(("body", "rc"), [("pass", 2), ("assert False", 1)])
def test_outside_output_rejected_preserving_pytest_failure(tmp_path, body, rc):
    actual, receipt, out, err, _ = run_case(tmp_path, f"def test_ok():\n    {body}\n", outside=True)
    assert actual == rc, out + err
    assert receipt is None
    assert "EVIDENCE_OUTPUT" in err


@pytest.mark.parametrize(("timezone", "context_timezone"), [("UTC", "Asia/Seoul"), ("Asia/Seoul", "UTC"), (None, "UTC")])
def test_timezone_mismatch_rejected_before_pytest(tmp_path, timezone, context_timezone):
    rc, receipt, out, err, _ = run_case(tmp_path, timezone=timezone, context_timezone=context_timezone)
    assert rc == 2, out + err
    assert receipt is None
    assert "EVIDENCE_TIMEZONE" in err
    assert "passed" not in out


def test_seoul_actual_timezone_is_supported(tmp_path):
    rc, receipt, out, err, _ = run_case(tmp_path, timezone="Asia/Seoul", context_timezone="Asia/Seoul")
    assert rc == 0, out + err
    assert receipt["slot"]["timezone"] == "Asia/Seoul"


@pytest.mark.parametrize("late", ["sessionfinish", "unconfigure"])
def test_final_timezone_tampering_prevents_receipt(tmp_path, late):
    hooks = f"import os\ndef pytest_{late}({'session, exitstatus' if late == 'sessionfinish' else 'config'}):\n    os.environ['TZ'] = 'Asia/Seoul'\n"
    rc, receipt, out, err, _ = run_case(tmp_path, hooks=hooks)
    assert rc == 2, out + err
    assert receipt is None
    assert "EVIDENCE_TIMEZONE" in err


def test_receipt_waits_for_late_sessionfinish_and_unconfigure(tmp_path):
    hooks = """
        import pytest, sys
        from pathlib import Path
        @pytest.hookimpl(wrapper=True, tryfirst=True)
        def pytest_sessionfinish(session, exitstatus):
            yield
            assert not Path('receipt.json').exists()
            session.exitstatus = 6
            sys.modules['conftest'].VIOLATIONS.append('late-only')
        def pytest_unconfigure(config):
            assert not Path('receipt.json').exists()
            sys.modules['conftest'].VIOLATIONS.append('unconfigure-only')
    """
    rc, receipt, out, err, _ = run_case(tmp_path, hooks=hooks)
    assert rc == 6, out + err
    assert receipt["session"]["exit_code"] == 6
    assert receipt["guard"]["violations"] == 2


def test_unconfigure_exception_never_leaves_completed_receipt(tmp_path):
    rc, receipt, out, err, _ = run_case(tmp_path, hooks="def pytest_unconfigure(config):\n    raise RuntimeError('private-message')\n")
    assert rc != 0, out + err
    assert receipt is None
    assert "private-message" not in err
    assert "EVIDENCE_PYTEST_EXCEPTION" in err


@pytest.mark.parametrize("name", ["PYTEST_ADDOPTS", "PYTEST_PLUGINS"])
def test_ambient_pytest_injection_is_rejected(tmp_path, name):
    rc, receipt, out, err, _ = run_case(tmp_path, env_extra={name: "synthetic-untrusted"})
    assert rc == 2, out + err
    assert receipt is None
    assert "EVIDENCE_ARGUMENTS" in err
    assert "synthetic-untrusted" not in err


@pytest.mark.parametrize("arg", ["--verification-context=other", "--verification-output=other"])
def test_owned_options_cannot_be_reinjected(tmp_path, arg):
    rc, receipt, out, err, _ = run_case(tmp_path, args=(arg,))
    assert rc == 2, out + err
    assert receipt is None
    assert "EVIDENCE_ARGUMENTS" in err


@pytest.mark.parametrize("raw", ['{"run": {}, "slot": {}, "results": []}', '{"run": {}, "run": {}, "slot": {}}', '{"run": NaN, "slot": {}}'])
def test_context_has_strict_run_slot_schema(tmp_path, raw):
    rc, receipt, out, err, _ = run_case(tmp_path, context_raw=raw)
    assert rc == 2, out + err
    assert receipt is None
    assert "EVIDENCE_CONTEXT" in err


def test_context_changed_during_pytest_is_rejected(tmp_path):
    source = "from pathlib import Path\ndef test_ok():\n    Path('context.json').write_text('{}')\n"
    rc, receipt, out, err, _ = run_case(tmp_path, source)
    assert rc == 2, out + err
    assert receipt is None
    assert "EVIDENCE_CONTEXT" in err


def test_forced_exit_does_not_publish_early_receipt(tmp_path):
    rc, receipt, out, err, _ = run_case(tmp_path, "import os\ndef test_ok():\n    os._exit(9)\n")
    assert rc == 9, out + err
    assert receipt is None


@pytest.mark.parametrize("bad", [True, 0, -1, "1"])
def test_context_attempt_is_a_positive_integer(tmp_path, bad):
    raw = json.dumps({"run": RUN | {"attempt": bad}, "slot": {"lane": "standard", "timezone": "UTC"}})
    rc, receipt, out, err, _ = run_case(tmp_path, context_raw=raw)
    assert rc == 2, out + err
    assert receipt is None
    assert "EVIDENCE_CONTEXT" in err


def test_final_context_failure_preserves_raw_nonzero_rc(tmp_path):
    source = "from pathlib import Path\ndef test_ok():\n    Path('context.json').write_text('{}')\n    assert False\n"
    rc, receipt, out, err, _ = run_case(tmp_path, source)
    assert rc == 1, out + err
    assert receipt is None
    assert "EVIDENCE_CONTEXT" in err


def test_final_timezone_failure_preserves_raw_nonzero_rc(tmp_path):
    hooks = "import os\ndef pytest_unconfigure(config):\n    os.environ['TZ'] = 'Asia/Seoul'\n"
    rc, receipt, out, err, _ = run_case(tmp_path, "def test_ok():\n    assert False\n", hooks=hooks)
    assert rc == 1, out + err
    assert receipt is None
    assert "EVIDENCE_TIMEZONE" in err


def test_oversize_nodeid_prevents_receipt_without_changing_test(tmp_path):
    hooks = "def pytest_collection_modifyitems(items):\n    items[0]._nodeid = 'x' * 2049\n"
    rc, receipt, out, err, _ = run_case(tmp_path, hooks=hooks)
    assert rc == 2, out + err
    assert "1 passed" in out
    assert receipt is None
    assert "EVIDENCE_SESSION" in err


def test_publication_cannot_overwrite_context_or_escape_via_symlink(tmp_path):
    from scripts.dev.pytest_evidence import _EvidenceError, _publish
    root = tmp_path / "root"
    root.mkdir()
    context = root / "context.json"
    context.write_text("original")
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "link").symlink_to(outside, target_is_directory=True)
    with pytest.raises(_EvidenceError, match="EVIDENCE_OUTPUT"):
        _publish(context, context, root, {})
    with pytest.raises(_EvidenceError, match="EVIDENCE_OUTPUT"):
        _publish(root / "link" / "receipt.json", context, root, {})
    assert context.read_text() == "original"
    assert not (outside / "receipt.json").exists()


def test_publication_refuses_more_than_32_mib_before_creating_file(tmp_path):
    from scripts.dev.pytest_evidence import _EvidenceError, _publish
    output = tmp_path / "receipt.json"
    with pytest.raises(_EvidenceError, match="EVIDENCE_OUTPUT"):
        _publish(output, tmp_path / "context.json", tmp_path, {"data": "x" * (32 * 1024 * 1024)})
    assert not output.exists()


def test_offset_must_match_real_timezone_even_when_tz_label_matches(tmp_path):
    bootstrap = "import time\nfrom types import SimpleNamespace\ntime.localtime = lambda epoch: SimpleNamespace(tm_gmtoff=1234)"
    rc, receipt, out, err, _ = run_case(tmp_path, bootstrap=bootstrap)
    assert rc == 2, out + err
    assert receipt is None
    assert "EVIDENCE_TIMEZONE" in err


def test_late_hook_exception_never_leaves_completed_receipt(tmp_path):
    hooks = "import pytest\n@pytest.hookimpl(wrapper=True, tryfirst=True)\ndef pytest_sessionfinish(session, exitstatus):\n    yield\n    raise RuntimeError('late-private-message')\n"
    rc, receipt, out, err, _ = run_case(tmp_path, hooks=hooks)
    assert rc == 2, out + err
    assert receipt is None
    assert "EVIDENCE_PYTEST_EXCEPTION" in err
    assert "late-private-message" not in err


@pytest.mark.parametrize(("count", "invalid"), [(20_000, False), (20_001, True)])
def test_observer_bounds_inventory_without_running_large_workload(count, invalid):
    from types import SimpleNamespace
    from scripts.dev.pytest_evidence import _Observer
    observer = _Observer()
    observer.pytest_collection_finish(SimpleNamespace(items=[
        SimpleNamespace(nodeid=f"tests/test_tiny.py::test_{index}") for index in range(count)
    ]))
    assert observer.invalid is invalid


def test_duplicate_actual_nodeids_are_not_silently_deduplicated(tmp_path):
    hooks = "def pytest_collection_modifyitems(items):\n    items.append(items[0])\n"
    rc, receipt, out, err, _ = run_case(tmp_path, hooks=hooks)
    assert rc == 2, out + err
    assert "2 passed" in out
    assert receipt is None
    assert "EVIDENCE_SESSION" in err
