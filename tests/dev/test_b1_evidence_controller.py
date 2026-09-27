"""B1 실행 경계의 독립 리터럴 시험. 이 파일은 실제 자식을 만들지 않는다."""

import fcntl
import importlib.util
import io
import json
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[2]


def _load_source(name, filename):
    """기존 시험과 같은 소스 loader를 쓰며 수집 시 제품 모듈을 읽지 않는다."""
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts/dev" / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _parent_env():
    return {
        "PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "TZ": "UTC",
        "PYTHONDONTWRITEBYTECODE": "1", "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
    }


def _guard_fact():
    return {
        "path": "tests/conftest.py",
        "sha256": "7b7b26940309a2a165d9bdba7611b6730e83b90ae9ea5f9e725764ee4c0a23f7",
        "module_count": 1, "violations": 0,
    }


def _argument_case(tmp_path, monkeypatch):
    module = _load_source("_b1_argument_candidate", "pytest_evidence_controller.py")
    repo = tmp_path / "repo"
    (repo / "tests/proofs").mkdir(parents=True)
    (repo / "tests/test_tiny.py").write_text("def test_ok():\n    assert True\n")
    (repo / "context.json").write_text("{}")
    monkeypatch.setattr(module, "ROOT", repo)
    monkeypatch.chdir(repo)
    monkeypatch.setattr(module.os, "environ", _parent_env())
    return module, repo


def _args(*, profile=("--profile", "b1-standard/v1"), timeout="900", selected=("tests/test_tiny.py",)):
    return [
        "--verification-context", "context.json", "--verification-output", "receipt.json",
        "--process-output", "process.json", "--timeout-seconds", timeout,
        *profile, "--", *selected,
    ]


def test_b1_arguments_accept_exact_profile_without_changing_six_result_shape(tmp_path, monkeypatch):
    """첫 RED: 새 profile 미지원은 기존 parser의 실제 호출에서 드러난다."""
    module, repo = _argument_case(tmp_path, monkeypatch)

    options, context, receipt, output, logs, selected = module._arguments(_args())

    assert options.profile == "b1-standard/v1"
    assert options.timeout_seconds == 900
    assert context == repo / "context.json"
    assert receipt == repo / "receipt.json"
    assert output == repo / "process.json"
    assert logs == [repo / "process.json.stdout.log", repo / "process.json.stderr.log"]
    assert selected == ["tests/test_tiny.py"]


@pytest.mark.parametrize("timezone", ["UTC", "Asia/Seoul"])
def test_b1_parser_accepts_exact_parent_environment_in_each_timezone(tmp_path, monkeypatch, timezone):
    module, _ = _argument_case(tmp_path, monkeypatch)
    monkeypatch.setattr(module.os, "environ", {
        "PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "TZ": timezone,
        "PYTHONDONTWRITEBYTECODE": "1", "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
    })
    options, _, _, _, _, selected = module._arguments(_args())
    assert options.profile == "b1-standard/v1"
    assert options.timeout_seconds == 900
    assert selected == ["tests/test_tiny.py"]


@pytest.mark.parametrize("timeout", ["1", "30", "900"])
def test_legacy_arguments_keep_six_results_and_original_timeout_range(tmp_path, monkeypatch, timeout):
    module, repo = _argument_case(tmp_path, monkeypatch)
    module.os.environ["QWQ_TEST_ONLY"] = "legacy-extra-key"
    options, context, receipt, output, logs, selected = module._arguments(_args(profile=(), timeout=timeout))
    assert options.timeout_seconds == int(timeout)
    assert context == repo / "context.json"
    assert receipt == repo / "receipt.json"
    assert output == repo / "process.json"
    assert logs == [repo / "process.json.stdout.log", repo / "process.json.stderr.log"]
    assert selected == ["tests/test_tiny.py"]


def test_b1_profile_equals_syntax_is_accepted_once(tmp_path, monkeypatch):
    module, _ = _argument_case(tmp_path, monkeypatch)
    options, _, _, _, _, selected = module._arguments(_args(profile=("--profile=b1-standard/v1",)))
    assert options.profile == "b1-standard/v1"
    assert options.timeout_seconds == 900
    assert selected == ["tests/test_tiny.py"]


@pytest.mark.parametrize(("selection", "expected"), [
    ("tests", ["tests"]),
    ("./tests/test_tiny.py", ["tests/test_tiny.py"]),
])
def test_b1_full_suite_and_normalized_literal_path_remain_allowed(tmp_path, monkeypatch, selection, expected):
    module, _ = _argument_case(tmp_path, monkeypatch)
    _, _, _, _, _, selected = module._arguments(_args(selected=(selection,)))
    assert selected == expected


@pytest.mark.parametrize("profile", [
    ("--profile", "other/v1"),
    ("--profile", "pytest-evidence-bootstrap/v1"),
    ("--profile", "b1-standard/v1", "--profile", "b1-standard/v1"),
    ("--profile=b1-standard/v1", "--profile=b1-standard/v1"),
    ("--profile", "b1-standard/v1", "--profile=b1-standard/v1"),
    ("--prof", "b1-standard/v1"),
    ("--profile",),
])
def test_b1_unknown_repeated_or_abbreviated_profile_rejected(tmp_path, monkeypatch, profile):
    module, _ = _argument_case(tmp_path, monkeypatch)
    with pytest.raises(ValueError):
        module._arguments(_args(profile=profile))


@pytest.mark.parametrize("timeout", ["0", "1", "3", "899", "901", "900.0", "+900", "９００", "true"])
def test_b1_only_literal_integer_900_timeout_is_allowed(tmp_path, monkeypatch, timeout):
    module, _ = _argument_case(tmp_path, monkeypatch)
    with pytest.raises(ValueError):
        module._arguments(_args(timeout=timeout))


@pytest.mark.parametrize("extra", [
    ("--retained-stream-bytes", "8388608"),
    ("--lock-path", "fixture.lock"),
    ("--env", "QWQ_TEST_ONLY=1"),
    ("--executable", "/usr/bin/false"),
    ("--command", "true"),
    ("--timeout-seconds", "900"),
])
def test_b1_has_no_caller_policy_or_command_extension(tmp_path, monkeypatch, extra):
    module, _ = _argument_case(tmp_path, monkeypatch)
    with pytest.raises(ValueError):
        module._arguments(_args(profile=("--profile", "b1-standard/v1", *extra)))


@pytest.mark.parametrize("selected", [
    (), ("../outside",), ("tests/test_tiny.py::test_ok",), ("tests/*",),
    ("tests/?.py",), ("tests/[a].py",), ("-s",), ("--b1-standard-v1",),
    ("tests/test_tiny.py", "./tests/test_tiny.py"), ("tests/proofs",), ("context.json",),
])
def test_b1_selectors_remain_normalized_unique_test_paths(tmp_path, monkeypatch, selected):
    module, _ = _argument_case(tmp_path, monkeypatch)
    with pytest.raises(ValueError):
        module._arguments(_args(selected=selected))


def test_b1_selector_symlinks_and_existing_outputs_reject(tmp_path, monkeypatch):
    module, repo = _argument_case(tmp_path, monkeypatch)
    (repo / "tests/link.py").symlink_to(repo / "tests/test_tiny.py")
    with pytest.raises(ValueError):
        module._arguments(_args(selected=("tests/link.py",)))
    (repo / "process.json").write_text("preserve-me")
    with pytest.raises(ValueError):
        module._arguments(_args())
    assert (repo / "process.json").read_text() == "preserve-me"


@pytest.mark.parametrize(("key", "value"), [
    ("HOME", "/not-used"), ("PYTHONPATH", "/not-used"),
    ("PYTEST_ADDOPTS", ""), ("PYTEST_PLUGINS", ""),
    ("QWQ_DEPLOY_SSH_KEY", "/not-used/nonexistent-key"),
    ("QWQ_VERIFY_SKIP_TESTS", "1"), ("QWQ_B1_RAW_MUTANT", "not-used"),
    ("ARBITRARY_EXTRA_KEY", "1"),
    ("PATH", "/usr/local/bin"), ("LANG", "C"), ("TZ", "Europe/Paris"),
    ("PYTHONDONTWRITEBYTECODE", "0"), ("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "0"),
])
def test_b1_parent_environment_rejected_before_dependency_or_guard_loading(tmp_path, monkeypatch, key, value):
    module, _ = _argument_case(tmp_path, monkeypatch)
    module.os.environ[key] = value

    def forbidden(*args, **kwargs):
        pytest.fail("잘못된 B1 부모 환경이 의존성 또는 프로세스 경계에 도달함")

    monkeypatch.setattr(module, "_load_bootstrap", forbidden)
    monkeypatch.setattr(module, "_preflight", forbidden)
    monkeypatch.setattr(module, "_probe", forbidden)
    monkeypatch.setattr(module, "_RawPopen", forbidden)
    monkeypatch.setattr(module.time, "monotonic", lambda: 0.0)
    with pytest.raises(ValueError):
        module._arguments(_args())
    assert module.main(_args()) == 125


@pytest.mark.parametrize("key", [
    "PATH", "LANG", "TZ", "PYTHONDONTWRITEBYTECODE", "PYTEST_DISABLE_PLUGIN_AUTOLOAD",
])
def test_b1_parent_environment_requires_every_fixed_key(tmp_path, monkeypatch, key):
    module, _ = _argument_case(tmp_path, monkeypatch)
    del module.os.environ[key]

    def forbidden(*args, **kwargs):
        pytest.fail("누락된 B1 부모 환경이 의존성 경계에 도달함")

    monkeypatch.setattr(module, "_load_bootstrap", forbidden)
    monkeypatch.setattr(module, "_preflight", forbidden)
    monkeypatch.setattr(module, "_probe", forbidden)
    monkeypatch.setattr(module, "_RawPopen", forbidden)
    monkeypatch.setattr(module.time, "monotonic", lambda: 0.0)
    with pytest.raises(ValueError):
        module._arguments(_args())
    assert module.main(_args()) == 125


def _bootstrap_case(tmp_path, monkeypatch, returncode=0):
    module = _load_source("_b1_bootstrap_candidate", "pytest_evidence_bootstrap.py")
    repo = tmp_path / "bootstrap-repo"
    (repo / "scripts/dev").mkdir(parents=True)
    monkeypatch.chdir(repo)
    monkeypatch.setattr(module, "__file__", str(repo / "scripts/dev/pytest_evidence_bootstrap.py"))
    monkeypatch.setattr(module.sys, "path", list(sys.path))
    calls = {"guard": [], "frames": [], "closed": [], "producer": [], "loads": []}

    def guard(root, *, install=False):
        assert root == repo
        calls["guard"].append(install)
        return _guard_fact()

    def produce(argv, *, context_path, output_path):
        calls["producer"].append((argv, context_path, output_path))
        return returncode

    def load(name, path, source=None):
        calls["loads"].append((name, path))
        assert source is None
        return SimpleNamespace(run_with_evidence=produce)

    def write(fd, raw):
        calls["frames"].append((fd, raw))
        return len(raw)

    monkeypatch.setattr(module, "_guard", guard)
    monkeypatch.setattr(module, "_load", load)
    monkeypatch.setattr(module.os, "write", write)
    monkeypatch.setattr(module.os, "close", calls["closed"].append)
    return module, repo, calls


@pytest.mark.parametrize("returncode", [0, 9])
def test_b1_bootstrap_marker_sets_exact_pytest_argv_and_preserves_producer_exit(tmp_path, monkeypatch, returncode):
    module, repo, calls = _bootstrap_case(tmp_path, monkeypatch, returncode)
    assert module.main([
        "--b1-standard-v1", "71", "context.json", "receipt.json", "tests/test_tiny.py",
    ]) == returncode
    assert calls["producer"] == [([
        "-x", "-q", "-p", "no:cacheprovider", "-p", "pytest_asyncio.plugin",
        "-p", "pytest_cov.plugin", "-p", "anyio.pytest_plugin", "--tb=short", "tests/test_tiny.py",
    ], Path("context.json"), Path("receipt.json"))]
    assert calls["loads"] == [("_qwq_evidence_producer", repo / "scripts/dev/pytest_evidence.py")]
    assert calls["guard"] == [True, False]
    assert calls["closed"] == [71]
    assert len(calls["frames"]) == 1
    fd, raw = calls["frames"][0]
    assert fd == 71
    assert raw.endswith(b"\n")
    assert json.loads(raw) == {"schema": "qwq.pytest-guard-ready/v1", "guard": _guard_fact()}


def test_legacy_bootstrap_without_marker_keeps_original_fixed_argv(tmp_path, monkeypatch):
    module, _, calls = _bootstrap_case(tmp_path, monkeypatch)
    assert module.main(["71", "context.json", "receipt.json", "tests/test_tiny.py"]) == 0
    assert calls["producer"] == [([
        "-q", "-p", "no:cacheprovider", "-p", "pytest_asyncio.plugin",
        "-p", "pytest_cov.plugin", "-p", "anyio.pytest_plugin", "--tb=short", "tests/test_tiny.py",
    ], Path("context.json"), Path("receipt.json"))]


@pytest.mark.parametrize("args", [
    ["--b1-standard-v2", "71", "context.json", "receipt.json", "tests/test_tiny.py"],
    ["--b1-standard-v1", "--b1-standard-v1", "71", "context.json", "receipt.json", "tests/test_tiny.py"],
    ["--b1-standard-v1", "71", "context.json", "receipt.json", "--b1-standard-v2"],
    ["--b1-standard-v1", "71", "context.json", "receipt.json", "tests/test_tiny.py", "--b1-standard-v1"],
])
def test_b1_unknown_or_repeated_private_marker_never_reaches_producer(tmp_path, monkeypatch, args):
    module, _, calls = _bootstrap_case(tmp_path, monkeypatch)
    assert module.main(args) == 125
    assert calls["producer"] == []


def test_b1_bootstrap_guard_recheck_cannot_be_skipped(tmp_path, monkeypatch):
    module, _, calls = _bootstrap_case(tmp_path, monkeypatch)

    def guard(root, *, install=False):
        observed = _guard_fact()
        if not install:
            observed["violations"] = 1
        return observed

    monkeypatch.setattr(module, "_guard", guard)
    assert module.main([
        "--b1-standard-v1", "71", "context.json", "receipt.json", "tests/test_tiny.py",
    ]) == 125
    assert len(calls["producer"]) == 1


def test_bootstrap_guard_accepts_pinned_bytes_and_rejects_modified_bytes(tmp_path, monkeypatch):
    """제품 상수를 expected로 읽지 않고 실제 guard 판정의 고정 hash 경계를 검사한다."""
    module = _load_source("_b1_guard_candidate", "pytest_evidence_bootstrap.py")
    repo = tmp_path / "guard-repo"
    (repo / "tests").mkdir(parents=True)
    guard_path = repo / "tests/conftest.py"
    source = (ROOT / "tests/conftest.py").read_bytes()
    guard_path.write_bytes(source)
    actual_module = ModuleType("conftest")
    actual_module.__file__ = str(guard_path)
    actual_module.VIOLATIONS = []
    with monkeypatch.context() as patch:
        patch.setattr(module.sys, "modules", {"conftest": actual_module})
        assert module._guard(repo) == {
            "path": "tests/conftest.py",
            "sha256": "7b7b26940309a2a165d9bdba7611b6730e83b90ae9ea5f9e725764ee4c0a23f7",
            "module_count": 1, "violations": 0,
        }
        guard_path.write_bytes(source + b"\n")
        with pytest.raises(ValueError, match="PROCESS_GUARD"):
            module._guard(repo)


def test_b1_budget_keeps_one_inclusive_deadline_and_first_cleanup_window():
    """첫 RED: cleanup 재진입이 전체 예산이나 최초 TERM/종료 끝을 연장하면 안 된다."""
    module = _load_source("_b1_budget_candidate", "pytest_evidence_controller.py")
    budget = module._B1Budget(100.0)

    assert budget.total_end == 1000.0
    assert budget.run_end == 996.0
    assert budget.cleanup_end is None
    assert budget.term_end is None

    budget.begin_cleanup(200.0)
    assert budget.cleanup_end == 203.0
    assert budget.term_end == 201.0

    budget.begin_cleanup(250.0)
    assert budget.cleanup_end == 203.0
    assert budget.term_end == 201.0
    assert (budget.total_end, budget.run_end) == (1000.0, 996.0)


def _clock_trace(module, monkeypatch, points):
    """시계 읽기 횟수와 무관하게 sleep 경계에서만 독립 리터럴 시각으로 진행한다."""
    now = [points[0]]
    remaining = list(points[1:])

    def sleep(_seconds):
        assert remaining, "고정 시각 trace를 넘겨 계속 대기함"
        now[0] = remaining.pop(0)

    monkeypatch.setattr(module.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(module.time, "sleep", sleep)
    return now


class _ProbeExit(BaseException):
    def __init__(self, code):
        self.code = code


def _probe_adapters(module, monkeypatch, now, *, child=False):
    """fork/wait/close/exit/signals는 합성 경계만 사용하며 /proc도 읽지 않는다."""
    events = []

    def fork():
        events.append(("fork",))
        return 0 if child else 41

    def exit_child(code):
        events.append(("exit", code))
        raise _ProbeExit(code)

    def forbidden(*args, **kwargs):
        pytest.fail("지정하지 않은 probe syscall 또는 unlock에 도달함")

    monkeypatch.setattr(module.os, "fork", fork)
    monkeypatch.setattr(module.os, "_exit", exit_child)
    monkeypatch.setattr(module.os, "close", lambda fd: events.append(("close", fd)))
    monkeypatch.setattr(module.os, "waitpid", forbidden)
    monkeypatch.setattr(module.os, "pidfd_open", forbidden)
    monkeypatch.setattr(module.signal, "pidfd_send_signal", forbidden)
    monkeypatch.setattr(module._Owner, "signal_children", lambda self, sig: events.append(("signal", now[0], sig)))
    monkeypatch.setattr(fcntl, "flock", forbidden)
    return events


@pytest.mark.parametrize(("cleanup_at", "cleanup_end", "term_end"), [
    (200.0, 203.0, 201.0),
    (995.0, 998.0, 996.0),
    (996.0, 999.0, 997.0),
    (997.0, 999.0, 998.0),
    (998.0, 999.0, 999.0),
    (998.5, 999.0, 999.0),
    (999.0, 999.0, 999.0),
    (1000.0, 999.0, 999.0),
    (1001.0, 999.0, 999.0),
])
def test_b1_budget_clamps_cleanup_and_term_without_extending_total(cleanup_at, cleanup_end, term_end):
    module = _load_source("_b1_budget_boundaries", "pytest_evidence_controller.py")
    budget = module._B1Budget(100.0)
    budget.begin_cleanup(cleanup_at)
    assert (budget.total_end, budget.run_end) == (1000.0, 996.0)
    assert (budget.cleanup_end, budget.term_end) == (cleanup_end, term_end)
    budget.begin_cleanup(1005.0)
    assert (budget.cleanup_end, budget.term_end) == (cleanup_end, term_end)


@pytest.mark.parametrize("snapshot", [None, [71, 72], {71, 72}, (71, 71), (True,), (-1,), (71.0,), ("71",)])
def test_b1_probe_invalid_fd_snapshot_rejected_before_any_fork_or_close(monkeypatch, snapshot):
    module = _load_source("_b1_probe_bad_fds", "pytest_evidence_controller.py")
    now = _clock_trace(module, monkeypatch, (0.0,))
    events = _probe_adapters(module, monkeypatch, now)
    budget = module._B1Budget(0.0)
    try:
        result = module._probe(896.0, [False], budget=budget, controller_fds=snapshot)
    except ValueError:
        result = False
    assert result is False
    assert events == []


def test_b1_probe_fd_snapshot_rejects_tuple_and_integer_subclasses(monkeypatch):
    class Descriptor(int):
        pass

    class Snapshot(tuple):
        pass

    module = _load_source("_b1_probe_fd_types", "pytest_evidence_controller.py")
    now = _clock_trace(module, monkeypatch, (0.0,))
    events = _probe_adapters(module, monkeypatch, now)
    for snapshot in (Snapshot((71,)), (Descriptor(71),)):
        try:
            result = module._probe(896.0, [False], budget=module._B1Budget(0.0), controller_fds=snapshot)
        except ValueError:
            result = False
        assert result is False
    assert events == []


@pytest.mark.parametrize(("snapshot", "expected_events"), [
    ((), [("fork",), ("exit", 23)]),
    ((71, 72), [("fork",), ("close", 71), ("close", 72), ("exit", 23)]),
    ((0, 72), [("fork",), ("close", 0), ("close", 72), ("exit", 23)]),
])
def test_b1_probe_child_closes_exact_owned_snapshot_before_exit_without_unlock(monkeypatch, snapshot, expected_events):
    module = _load_source("_b1_probe_child", "pytest_evidence_controller.py")
    now = _clock_trace(module, monkeypatch, (0.0,))
    events = _probe_adapters(module, monkeypatch, now, child=True)
    budget = module._B1Budget(0.0)
    with pytest.raises(_ProbeExit) as exited:
        module._probe(896.0, [False], budget=budget, controller_fds=snapshot)
    assert exited.value.code == 23
    assert events == expected_events
    assert budget.cleanup_end is None
    assert budget.term_end is None


@pytest.mark.parametrize("failed_fd", [71, 72])
def test_b1_probe_child_close_failure_still_closes_remaining_fds_and_exits_125(monkeypatch, failed_fd):
    module = _load_source("_b1_probe_close_error", "pytest_evidence_controller.py")
    now = _clock_trace(module, monkeypatch, (0.0,))
    events = _probe_adapters(module, monkeypatch, now, child=True)

    def close(fd):
        events.append(("close", fd))
        if fd == failed_fd:
            raise OSError("합성 close 실패")

    monkeypatch.setattr(module.os, "close", close)
    with pytest.raises(_ProbeExit) as exited:
        module._probe(896.0, [False], budget=module._B1Budget(0.0), controller_fds=(71, 72, 73))
    assert exited.value.code == 125
    assert events == [("fork",), ("close", 71), ("close", 72), ("close", 73), ("exit", 125)]


@pytest.mark.parametrize(("now_value", "stopped"), [(0.0, True), (896.0, False), (897.0, False)])
def test_b1_probe_never_forks_when_stopped_or_at_run_end(monkeypatch, now_value, stopped):
    module = _load_source("_b1_probe_prestart", "pytest_evidence_controller.py")
    now = _clock_trace(module, monkeypatch, (now_value,))
    events = _probe_adapters(module, monkeypatch, now)
    assert module._probe(896.0, [stopped], budget=module._B1Budget(0.0), controller_fds=(71,)) is False
    assert events == []


@pytest.mark.parametrize("b1", [False, True])
def test_b1_probe_normal_23_and_echild_preserve_parent_fds_and_cleanup_budget(monkeypatch, b1):
    module = _load_source("_b1_probe_parent", "pytest_evidence_controller.py")
    now = _clock_trace(module, monkeypatch, (895.5,))
    events = _probe_adapters(module, monkeypatch, now)
    budget = module._B1Budget(0.0) if b1 else None

    def wait(pid, flags):
        assert flags == 0x40000001
        events.append(("wait", pid))
        if pid == 41:
            return 41, 5888
        raise ChildProcessError

    monkeypatch.setattr(module.os, "waitpid", wait)
    if b1:
        assert module._probe(896.0, [False], budget=budget, controller_fds=(71, 72)) is True
    else:
        assert module._probe(896.0, [False]) is True
    assert events == [("fork",), ("wait", 41), ("wait", -1)]
    if b1:
        assert budget.cleanup_end is None
        assert budget.term_end is None
        assert (budget.total_end, budget.run_end) == (900.0, 896.0)


@pytest.mark.parametrize("raw_status", [0, 2304, 32000, 9])
def test_b1_probe_non_23_terminal_status_rejects_and_uses_shared_cleanup(monkeypatch, raw_status):
    module = _load_source("_b1_probe_bad_status", "pytest_evidence_controller.py")
    now = _clock_trace(module, monkeypatch, (995.0,))
    events = _probe_adapters(module, monkeypatch, now)
    budget = module._B1Budget(100.0)

    def wait(pid, flags):
        if pid == 41:
            return 41, raw_status
        raise ChildProcessError

    monkeypatch.setattr(module.os, "waitpid", wait)
    assert module._probe(996.0, [False], budget=budget, controller_fds=(71,)) is False
    assert events == [("fork",)]
    assert (budget.cleanup_end, budget.term_end) == (998.0, 996.0)


def test_b1_probe_23_without_final_echild_cannot_qualify(monkeypatch):
    module = _load_source("_b1_probe_no_echild", "pytest_evidence_controller.py")
    now = _clock_trace(module, monkeypatch, (0.0, 1.0, 2.0, 3.0, 4.0))
    events = _probe_adapters(module, monkeypatch, now)
    budget = module._B1Budget(0.0)

    def wait(pid, flags):
        return (41, 5888) if pid == 41 else (0, 0)

    monkeypatch.setattr(module.os, "waitpid", wait)
    assert module._probe(896.0, [False], budget=budget) is False
    assert budget.cleanup_end is not None
    assert budget.cleanup_end <= 4.0
    assert budget.term_end <= 2.0
    assert events[0] == ("fork",)


@pytest.mark.parametrize("missing_status", [False, True])
def test_b1_probe_failure_after_clock_gap_clamps_cleanup_to_original_total(monkeypatch, missing_status):
    module = _load_source("_b1_probe_late_failure", "pytest_evidence_controller.py")
    now = _clock_trace(module, monkeypatch, (995.5, 998.5, 999.0))
    events = _probe_adapters(module, monkeypatch, now)
    budget = module._B1Budget(100.0)

    def wait(pid, flags):
        if missing_status:
            raise ChildProcessError
        return 0, 0

    monkeypatch.setattr(module.os, "waitpid", wait)
    assert module._probe(996.0, [False], budget=budget, controller_fds=(71,)) is False
    assert budget.cleanup_end <= 999.0
    assert budget.term_end <= 999.0
    if not missing_status:
        assert (budget.cleanup_end, budget.term_end) == (999.0, 999.0)
        assert events == [("fork",), ("signal", 998.5, 15)]
    assert not any(event[0] == "close" for event in events)


def test_v1_probe_default_empty_fd_snapshot_keeps_child_exit_23(monkeypatch):
    module = _load_source("_b1_probe_legacy_child", "pytest_evidence_controller.py")
    now = _clock_trace(module, monkeypatch, (0.0,))
    events = _probe_adapters(module, monkeypatch, now, child=True)
    with pytest.raises(_ProbeExit) as exited:
        module._probe(10.0, [False])
    assert exited.value.code == 23
    assert events == [("fork",), ("exit", 23)]


def _pending_owner(module, monkeypatch, now, *, budget):
    owner = module._Owner() if budget is None else module._Owner(budget=budget)
    events = []

    def pending_read(fd, size):
        raise BlockingIOError

    def forbidden(*args, **kwargs):
        pytest.fail("관측기 합성 시험에서 실제 프로세스 작업을 시도함")

    monkeypatch.setattr(module.os, "fork", forbidden)
    monkeypatch.setattr(module.os, "_exit", forbidden)
    monkeypatch.setattr(module.os, "pidfd_open", forbidden)
    monkeypatch.setattr(module.signal, "pidfd_send_signal", forbidden)
    monkeypatch.setattr(fcntl, "flock", forbidden)
    monkeypatch.setattr(module.os, "waitpid", lambda *args: (0, 0))
    monkeypatch.setattr(module.os, "read", pending_read)
    monkeypatch.setattr(module.os, "close", lambda fd: events.append(("close", fd)))
    monkeypatch.setattr(owner, "signal_children", lambda sig: events.append(("signal", now[0], sig)))
    return owner, events


@pytest.mark.parametrize(("b1", "points", "deadline", "finish", "signals"), [
    (True, (998.5, 999.0), 996.0, 999.0, [("signal", 998.5, 15)]),
    (False, (998.5, 999.0, 999.5, 1001.5), 2000.0, 1001.5,
     [("signal", 998.5, 15), ("signal", 999.0, 15), ("signal", 999.5, 9)]),
])
def test_b1_observe_clamped_term_window_and_v1_literal_deadlines(monkeypatch, b1, points, deadline, finish, signals):
    module = _load_source("_b1_observe_deadline", "pytest_evidence_controller.py")
    now = _clock_trace(module, monkeypatch, points)
    budget = module._B1Budget(100.0) if b1 else None
    owner, events = _pending_owner(module, monkeypatch, now, budget=budget)
    owner.reject("io_error")
    pipes = {"stdout": 31}
    complete, _, streams = module._observe(
        owner, pipes, {"stdout": io.BytesIO(), "stderr": io.BytesIO()}, _guard_fact(), deadline, [False],
    )
    assert complete is False
    assert owner.error == "io_error"
    assert owner.finish_end == finish
    assert events == signals + [("close", 31)]
    assert pipes == {}
    assert streams["stdout"]["bytes"] == 0
    if b1:
        assert (budget.cleanup_end, budget.term_end) == (999.0, 999.0)


@pytest.mark.parametrize(("b1", "points", "finish", "signals"), [
    (True, (998.5, 999.0), 999.0, [("signal", 998.5, 15)]),
    (False, (998.5, 999.0, 999.5, 1001.5), 1001.5,
     [("signal", 998.5, 15), ("signal", 999.0, 15), ("signal", 999.5, 9)]),
])
def test_b1_emergency_clamped_term_window_and_v1_literal_deadlines(monkeypatch, b1, points, finish, signals):
    module = _load_source("_b1_emergency_deadline", "pytest_evidence_controller.py")
    now = _clock_trace(module, monkeypatch, points)
    budget = module._B1Budget(100.0) if b1 else None
    owner, events = _pending_owner(module, monkeypatch, now, budget=budget)
    pipes = {"stdout": 31}
    module._emergency_cleanup(owner, pipes)
    assert owner.error == "cleanup_error"
    assert owner.finish_end == finish
    assert events == signals + [("close", 31)]
    assert pipes == {}
    if b1:
        assert (budget.cleanup_end, budget.term_end) == (999.0, 999.0)


@pytest.mark.parametrize("which", ["observe", "emergency"])
def test_b1_cleanup_uses_term_equality_for_kill_and_never_signals_at_cleanup_end(monkeypatch, which):
    module = _load_source("_b1_cleanup_equality", "pytest_evidence_controller.py")
    now = _clock_trace(module, monkeypatch, (100.0, 101.0, 102.0, 103.0))
    budget = module._B1Budget(0.0)
    owner, events = _pending_owner(module, monkeypatch, now, budget=budget)
    if which == "observe":
        owner.reject("io_error")
        complete, _, _ = module._observe(owner, {}, {}, _guard_fact(), 896.0, [False])
        assert complete is False
    else:
        module._emergency_cleanup(owner, {})
    assert (owner.finish_end, budget.cleanup_end, budget.term_end) == (103.0, 103.0, 101.0)
    assert events == [("signal", 100.0, 15), ("signal", 101.0, 9), ("signal", 102.0, 9)]


@pytest.mark.parametrize(("finish_at", "expected_complete"), [(103.0, True), (103.25, False)])
def test_b1_observe_retry_late_adoption_and_emergency_share_first_cleanup(monkeypatch, finish_at, expected_complete):
    module = _load_source("_b1_cleanup_reentry", "pytest_evidence_controller.py")
    now = _clock_trace(module, monkeypatch, (100.0, finish_at))
    budget = module._B1Budget(0.0)
    owner = module._Owner(budget=budget)
    owner.leader = 41
    owner.record(41, 0)
    owner.startup_end = 500.0
    closed = []

    def empty_wait(*args):
        raise ChildProcessError

    def interrupted_read(fd, size):
        if fd == 31:
            return b""
        raise RuntimeError("독립 합성 관측기 중단")

    monkeypatch.setattr(module.os, "waitpid", empty_wait)
    monkeypatch.setattr(module.os, "read", interrupted_read)
    monkeypatch.setattr(module.os, "close", closed.append)
    monkeypatch.setattr(owner, "signal_children", lambda sig: pytest.fail("ECHILD 뒤 signal 시도"))
    pipes = {"stdout": 31, "stderr": 32, "control": 33}
    logs = {"stdout": io.BytesIO(), "stderr": io.BytesIO()}
    with pytest.raises(RuntimeError, match="독립 합성"):
        module._observe(owner, pipes, logs, _guard_fact(), 896.0, [False])
    assert (owner.finish_end, budget.cleanup_end, budget.term_end) == (103.0, 103.0, 101.0)
    assert closed == [31]

    now[0] = 102.0
    owner.record(42, 0)
    owner.reject("io_error")
    frame = json.dumps({"schema": "qwq.pytest-guard-ready/v1", "guard": _guard_fact()}).encode() + b"\n"
    chunks = {32: [b""], 33: [frame, b""]}
    monkeypatch.setattr(module.os, "read", lambda fd, size: chunks[fd].pop(0))
    complete, guard, _ = module._observe(owner, pipes, logs, _guard_fact(), 896.0, [False])
    assert complete is expected_complete
    assert guard == _guard_fact()
    assert (owner.finish_end, budget.cleanup_end, budget.term_end) == (103.0, 103.0, 101.0)
    assert owner.survived is True
    assert owner.returncode == 0
    assert owner.error == "io_error"
    assert closed == [31, 32, 33]

    now[0] = 104.0
    leftover = {"stdout": 51}
    module._emergency_cleanup(owner, leftover)
    assert leftover == {}
    assert closed == [31, 32, 33, 51]
    assert (owner.finish_end, budget.cleanup_end, budget.term_end) == (103.0, 103.0, 101.0)
    assert owner.error == "io_error"


def test_b1_emergency_on_new_owner_consumes_already_started_budget(monkeypatch):
    module = _load_source("_b1_shared_cleanup", "pytest_evidence_controller.py")
    now = _clock_trace(module, monkeypatch, (102.0, 103.0))
    budget = module._B1Budget(0.0)
    budget.begin_cleanup(100.0)
    owner, events = _pending_owner(module, monkeypatch, now, budget=budget)
    module._emergency_cleanup(owner, {})
    assert (owner.finish_end, budget.cleanup_end, budget.term_end) == (103.0, 103.0, 101.0)
    assert events == [("signal", 102.0, 9)]


def test_b1_probe_fork_oserror_clamps_shared_budget_without_retry_or_parent_close(monkeypatch):
    """fork 실패를 재시도하거나 새 cleanup 예산을 허용하는 회귀를 잡는다."""
    module = _load_source("_b1_probe_fork_failure", "pytest_evidence_controller.py")
    now = _clock_trace(module, monkeypatch, (995.5,))
    events = _probe_adapters(module, monkeypatch, now)
    budget = module._B1Budget(100.0)

    def failed_fork():
        assert events == [], "실패한 fork를 재시도함"
        events.append(("fork",))
        now[0] = 998.5
        raise OSError("합성 fork 실패")

    monkeypatch.setattr(module.os, "fork", failed_fork)
    assert module._probe(996.0, [False], budget=budget, controller_fds=(71, 72)) is False
    assert events == [("fork",)]
    assert (budget.total_end, budget.run_end) == (1000.0, 996.0)
    assert (budget.cleanup_end, budget.term_end) == (999.0, 999.0)


def test_v1_probe_fork_oserror_preserves_exception_identity_without_retry_or_parent_close(monkeypatch):
    """B1 예외 처리가 legacy 예외 전파까지 바꾸는 회귀를 잡는다."""
    module = _load_source("_v1_probe_fork_failure", "pytest_evidence_controller.py")
    now = _clock_trace(module, monkeypatch, (995.5,))
    events = _probe_adapters(module, monkeypatch, now)
    failure = OSError("합성 legacy fork 실패")

    def failed_fork():
        assert events == [], "실패한 fork를 재시도함"
        events.append(("fork",))
        raise failure

    monkeypatch.setattr(module.os, "fork", failed_fork)
    with pytest.raises(OSError) as caught:
        module._probe(996.0, [False], controller_fds=(71, 72))
    assert caught.value is failure
    assert events == [("fork",)]


def test_b1_probe_already_started_cleanup_blocks_fork_and_preserves_first_ends(monkeypatch):
    """run_end 전이어도 이미 소비한 cleanup을 새 probe가 재사용하지 못한다."""
    module = _load_source("_b1_probe_used_cleanup", "pytest_evidence_controller.py")
    now = _clock_trace(module, monkeypatch, (101.0,))
    events = _probe_adapters(module, monkeypatch, now)
    budget = module._B1Budget(100.0)
    budget.begin_cleanup(100.0)
    assert (budget.cleanup_end, budget.term_end) == (103.0, 101.0)

    assert module._probe(996.0, [False], budget=budget, controller_fds=(71, 72)) is False
    assert events == []
    assert (budget.total_end, budget.run_end) == (1000.0, 996.0)
    assert (budget.cleanup_end, budget.term_end) == (103.0, 101.0)


def test_b1_coordination_constructor_is_inert(monkeypatch):
    module = _load_source("_b1_coordination_constructor", "pytest_evidence_controller.py")
    import os as syscall
    import tempfile as temporary
    import time as clock
    import fcntl as locking

    def forbidden(*args, **kwargs):
        pytest.fail("coordination 생성자/snapshot에서 I/O 또는 clock 호출")

    targets = [(syscall, name) for name in (
        "open", "close", "dup", "fstat", "stat", "listdir", "rmdir", "mkdir",
        "unlink", "rename", "read", "write", "chmod", "getuid", "fork",
    )] + [(temporary, "mkdtemp"), (clock, "monotonic"), (clock, "sleep"), (locking, "flock")]
    originals = [(owner, name, getattr(owner, name)) for owner, name in targets]
    try:
        with monkeypatch.context() as patch:
            for owner, name in targets:
                patch.setattr(owner, name, forbidden)
            assert all(getattr(owner, name) is forbidden for owner, name in targets)
            coordination = module._B1Coordination()
            assert coordination.error is None
            assert coordination.close_failed is False
            assert coordination.fake_key_path is None
            assert type(coordination.controller_fds()) is tuple
            assert coordination.controller_fds() == ()
            expected = {
                "lock_acquired": False,
                "lock_identity_stable": False,
                "fake_key_absent_before": False,
                "fake_key_absent_after": False,
                "fake_directory_removed": False,
                "fake_key_path_sha256": None,
            }
            facts = coordination.facts()
            assert type(facts) is dict
            assert facts == expected
            assert list(facts) == list(expected)
            facts["lock_acquired"] = True
            facts["unexpected"] = "not object state"
            assert coordination.facts() == expected
            assert coordination.facts() is not facts
    finally:
        assert all(getattr(owner, name) is original for owner, name, original in originals)


_LOCK_LITERAL = "/home/ubuntu/projects/qwq-ai-trader/.claude/worktrees/owner-ticket-gate-20260926/.superpowers/sdd/2026-09-27-runtime-admission-contract/test-workload.lock"
_FAKE_DIRECTORY = "/tmp/qwq-b1-runner-fixed"
_FAKE_PATH = "/tmp/qwq-b1-runner-fixed/nonexistent-key"
_FAKE_HASH = "d70c45e8937aa7851510aebf49a37d0a82cf1f372c325c97fd59a340c3490c88"


def _scoped_stdlib_adapters(test):
    """본문 예외도 pytest 보고 전에 모든 전역 adapter를 반드시 복원한다."""
    from functools import wraps

    @wraps(test)
    def scoped(*args, **kwargs):
        with kwargs["monkeypatch"].context() as patch:
            kwargs["monkeypatch"] = patch
            return test(*args, **kwargs)

    return scoped


class _CoordinationSyscalls:
    """제품의 저장 구조를 모르는 독립 경로/FD/metadata ledger."""

    def __init__(self, monkeypatch, *, bound_paths=False):
        import os
        import time
        import tempfile
        import subprocess

        self.events = []
        self.live = {}
        self.next_fd = 70
        self.now = 0.0
        self.polls = []
        self.flocks = []
        self.errors = {}
        self.metadata = {}
        self.contents = []
        self.created_path = _FAKE_DIRECTORY
        self.on_event = lambda event: None
        self.bound_paths = bound_paths
        directories = ["/", "/tmp", _FAKE_DIRECTORY, "/fixture", "/fixture/a"]
        directories += [str(path) for path in Path(_LOCK_LITERAL).parents]
        for index, path in enumerate(dict.fromkeys(directories)):
            self.metadata[path] = SimpleNamespace(st_dev=7, st_ino=100 + index,
                                                  st_mode=0o40700, st_uid=1001, st_nlink=1)
        self.metadata[_LOCK_LITERAL] = SimpleNamespace(st_dev=7, st_ino=501,
                                                       st_mode=0o100600, st_uid=1001, st_nlink=1)
        for name in ("open", "close", "fstat", "stat", "listdir", "rmdir", "dup", "fdopen"):
            monkeypatch.setattr(os, name, getattr(self, name))
        monkeypatch.setattr(os, "getuid", lambda: 1001)
        monkeypatch.setattr(time, "monotonic", lambda: self.now)
        monkeypatch.setattr(time, "sleep", self.sleep)
        monkeypatch.setattr(tempfile, "mkdtemp", self.mkdtemp)
        monkeypatch.setattr(fcntl, "flock", self.flock)
        monkeypatch.setattr(subprocess, "Popen", self.forbidden)
        for name in ("read", "write", "unlink", "remove", "rename", "chmod", "mkdir",
                     "fork", "_exit", "lstat", "scandir", "pidfd_open", "getenv"):
            monkeypatch.setattr(os, name, self.forbidden)

    def forbidden(self, *args, **kwargs):
        pytest.fail("coordination 허용 밖의 syscall")

    def event(self, *event):
        self.events.append(event)
        self.on_event(event)
        failure = self.errors.get((event[0], event[1] if len(event) > 1 else None))
        if failure is not None:
            raise failure

    def path(self, path, dir_fd=None):
        value = str(path)
        if dir_fd is not None:
            assert not value.startswith("/")
            return str(Path(self.live[dir_fd][0]) / value)
        assert value.startswith("/")
        return value

    def allocate(self, path, metadata):
        fd = self.next_fd
        self.next_fd += 1
        self.live[fd] = (path, metadata)
        return fd

    def open(self, path, flags, mode=0o777, *, dir_fd=None):
        original_name = str(path)
        path = self.path(path, dir_fd)
        self.event("open", path, flags, dir_fd)
        assert path != _FAKE_PATH
        if not self.bound_paths:
            assert flags == (657408 if path == _LOCK_LITERAL else 720896)
            if dir_fd is None:
                assert original_name in ("/", "/tmp")
            else:
                assert "/" not in original_name and original_name not in (".", "..")
        metadata = self.metadata[path]
        if flags & 131072 and metadata.st_mode & 0o170000 == 0o120000:
            raise OSError(40, "NOFOLLOW symlink")
        if flags & 65536 and metadata.st_mode & 0o170000 != 0o40000:
            raise NotADirectoryError(20, "DIRECTORY component")
        return self.allocate(path, metadata)

    def close(self, fd):
        assert fd in self.live, "소유하지 않거나 이미 소비한 FD를 close함"
        path, _ = self.live.pop(fd)
        self.event("close", fd, path)

    def fstat(self, fd):
        path, metadata = self.live[fd]
        self.event("fstat", path, fd)
        current = self.metadata.get(path)
        if current is not None and (current.st_dev, current.st_ino) == (metadata.st_dev, metadata.st_ino):
            return current
        return metadata

    def stat(self, path, *, dir_fd=None, follow_symlinks=True):
        path = self.path(path, dir_fd)
        self.event("stat", path, dir_fd, follow_symlinks)
        assert follow_symlinks is False
        if path not in self.metadata:
            raise FileNotFoundError(2, "독립 ENOENT")
        return self.metadata[path]

    def listdir(self, fd):
        assert type(fd) is int
        assert self.live[fd][0] == _FAKE_DIRECTORY
        self.event("listdir", fd)
        return list(self.contents)

    def rmdir(self, name, *, dir_fd):
        assert name == "qwq-b1-runner-fixed"
        assert self.live[dir_fd][0] == "/tmp"
        self.event("rmdir", name, dir_fd)

    def mkdtemp(self, *, prefix, dir):
        assert (prefix, dir) == ("qwq-b1-runner-", "/tmp")
        self.event("mkdtemp", prefix, dir)
        return self.created_path

    def flock(self, fd, flags):
        assert self.live[fd][0] == _LOCK_LITERAL
        assert flags in (6, 8)
        self.event("flock", fd, flags)
        if flags == 6 and self.flocks:
            action = self.flocks.pop(0)
            if isinstance(action, BaseException):
                raise action
            action()

    def sleep(self, seconds):
        assert 0 < seconds <= 0.005
        self.event("sleep", seconds)
        assert self.polls, "독립 sleep trace 소진"
        self.now = self.polls.pop(0)

    def dup(self, fd):
        assert self.bound_paths
        self.event("dup", fd)
        return self.allocate(*self.live[fd])

    def fdopen(self, fd, mode, *, buffering=-1):
        self.event("fdopen", fd, mode, buffering)
        pytest.fail("이 행렬에서 성공 fdopen은 필요하지 않음")


def _coordination_case(monkeypatch):
    module = _load_source("_b1_coordination_matrix", "pytest_evidence_controller.py")
    calls = _CoordinationSyscalls(monkeypatch)
    return module, calls, module._B1Coordination(), module._B1Budget(0.0)


def _assert_close_once(calls, coordination):
    before = set(calls.live)
    lock = {fd for fd, item in calls.live.items() if item[0] == _LOCK_LITERAL}
    start = len(calls.events)
    result = coordination.close()
    closes = [event[1] for event in calls.events[start:] if event[0] == "close"]
    assert sorted(closes) == sorted(before)
    assert len(closes) == len(set(closes))
    if lock:
        assert closes[-1] in lock
    assert coordination.controller_fds() == ()
    assert calls.live == {}
    end = len(calls.events)
    assert coordination.close() is result
    assert len(calls.events) == end
    return result


@_scoped_stdlib_adapters
def test_b1_coordination_safe_lock_fake_path_and_lock_last_close(monkeypatch):
    module, calls, coordination, budget = _coordination_case(monkeypatch)
    assert coordination.acquire_lock([False], budget) is True
    assert coordination.prepare_fake_key() == _FAKE_PATH
    snapshot = coordination.controller_fds()
    assert type(snapshot) is tuple
    assert coordination.controller_fds() is not snapshot
    assert snapshot == tuple(sorted(calls.live))
    assert len(snapshot) == 4
    assert {calls.live[fd][0] for fd in snapshot} == {
        str(Path(_LOCK_LITERAL).parent), _LOCK_LITERAL, "/tmp", _FAKE_DIRECTORY,
    }
    assert coordination.fake_key_path == _FAKE_PATH
    assert coordination.facts() == {
        "lock_acquired": True, "lock_identity_stable": False,
        "fake_key_absent_before": True, "fake_key_absent_after": False,
        "fake_directory_removed": False, "fake_key_path_sha256": _FAKE_HASH,
    }
    assert coordination.finish_fake_key() is True
    assert coordination.check_lock_identity() is True
    assert coordination.facts() == {
        "lock_acquired": True, "lock_identity_stable": True,
        "fake_key_absent_before": True, "fake_key_absent_after": True,
        "fake_directory_removed": True, "fake_key_path_sha256": _FAKE_HASH,
    }
    assert _assert_close_once(calls, coordination) is True
    assert len(snapshot) == 4
    assert calls.events[-2][0] == "flock" and calls.events[-2][2] == 8
    assert calls.events[-1][0] == "close"


@pytest.mark.parametrize(("field", "value"), [
    ("st_mode", 0o10600), ("st_mode", 0o40600), ("st_mode", 0o20600),
    ("st_mode", 0o140600), ("st_mode", 0o120600), ("st_uid", 1002), ("st_nlink", 2),
])
@_scoped_stdlib_adapters
def test_b1_coordination_unsafe_lock_leaf_never_flocks(monkeypatch, field, value):
    module, calls, coordination, budget = _coordination_case(monkeypatch)
    setattr(calls.metadata[_LOCK_LITERAL], field, value)
    assert coordination.acquire_lock([False], budget) is False
    assert coordination.error == "startup_error"
    assert not any(event[0] == "flock" for event in calls.events)
    assert _assert_close_once(calls, coordination) is True


@pytest.mark.parametrize(("entry", "stopped", "reason"), [
    (0.0, True, "interrupted"), (896.0, False, "timeout"), (896.0, True, "interrupted"),
])
@_scoped_stdlib_adapters
def test_b1_coordination_entry_rejection_has_no_path_work(monkeypatch, entry, stopped, reason):
    module, calls, coordination, budget = _coordination_case(monkeypatch)
    calls.now = entry
    assert coordination.acquire_lock([stopped], budget) is False
    assert coordination.error == reason
    assert calls.events == []


@pytest.mark.parametrize(("entry", "end", "reason", "failure"), [
    (0.0, 240.0, "lock_timeout", BlockingIOError(11, "busy")),
    (0.0, 240.0, "lock_timeout", OSError(13, "busy")),
    (700.0, 896.0, "timeout", BlockingIOError(11, "busy")),
    (0.0, 240.0, "startup_error", InterruptedError(4, "interrupted")),
])
@_scoped_stdlib_adapters
def test_b1_coordination_lock_deadline_is_not_renewed(monkeypatch, entry, end, reason, failure):
    module, calls, coordination, budget = _coordination_case(monkeypatch)
    calls.now = entry
    calls.polls = [end]
    calls.flocks = [failure]
    if isinstance(failure, InterruptedError):
        calls.flocks = [lambda: (setattr(calls, "now", end), (_ for _ in ()).throw(failure))]
    assert coordination.acquire_lock([False], budget) is False
    assert coordination.error == reason
    assert len([event for event in calls.events if event[0] == "flock"]) == 1
    assert _assert_close_once(calls, coordination) is True


@pytest.mark.parametrize("phase", ["setup", "flock", "recheck"])
@_scoped_stdlib_adapters
def test_b1_coordination_time_consumed_at_boundaries_prevents_success(monkeypatch, phase):
    module, calls, coordination, budget = _coordination_case(monkeypatch)
    acquired = [False]

    def advance(event):
        if event[0] == "flock" and event[2] == 6:
            acquired[0] = True
            if phase == "flock":
                calls.now = 896.0
        if phase == "setup" and event[:2] == ("open", _LOCK_LITERAL):
            calls.now = 240.0
        if phase == "recheck" and acquired[0] and event[0] == "stat":
            calls.now = 896.0

    calls.on_event = advance
    assert coordination.acquire_lock([False], budget) is False
    assert coordination.error == ("startup_error" if phase == "setup" else "timeout")
    assert coordination.facts()["lock_acquired"] is (phase != "setup")
    assert not any(event[0] == "flock" and event[2] == 8 for event in calls.events)
    assert _assert_close_once(calls, coordination) is True


@pytest.mark.parametrize("stage", ["prepare", "finish"])
@pytest.mark.parametrize("bad", ["uid", "mode", "special_mode", "type", "identity", "leaf", "symlink", "entry"])
@_scoped_stdlib_adapters
def test_b1_coordination_unsafe_fake_directory_is_never_removed(monkeypatch, stage, bad):
    module, calls, coordination, budget = _coordination_case(monkeypatch)
    assert coordination.acquire_lock([False], budget) is True
    if stage == "finish":
        assert coordination.prepare_fake_key() == _FAKE_PATH
    metadata = dict(vars(calls.metadata[_FAKE_DIRECTORY]))
    changes = {"uid": ("st_uid", 1002), "mode": ("st_mode", 0o40755),
               "special_mode": ("st_mode", 0o44700), "type": ("st_mode", 0o100700),
               "identity": ("st_ino", 999)}
    if bad in changes:
        name, value = changes[bad]
        metadata[name] = value
        if bad == "identity" and stage == "prepare":
            calls.on_event = lambda event: calls.metadata.__setitem__(
                _FAKE_DIRECTORY, SimpleNamespace(**metadata)) if event[:2] == ("fstat", _FAKE_DIRECTORY) else None
        else:
            calls.metadata[_FAKE_DIRECTORY] = SimpleNamespace(**metadata)
    elif bad in ("leaf", "symlink"):
        calls.metadata[_FAKE_PATH] = SimpleNamespace(st_dev=7, st_ino=800,
            st_mode=0o100600 if bad == "leaf" else 0o120777, st_uid=1001, st_nlink=1)
    else:
        calls.contents = ["unexpected"]
    if stage == "prepare":
        assert coordination.prepare_fake_key() is None
        assert coordination.error == "startup_error"
        assert coordination.fake_key_path == _FAKE_PATH
        assert coordination.facts()["fake_key_path_sha256"] == _FAKE_HASH
        assert coordination.facts()["fake_key_absent_before"] is False
    else:
        assert coordination.finish_fake_key() is False
        assert coordination.error == "identity_changed"
        count = len(calls.events)
        assert coordination.finish_fake_key() is False
        assert len(calls.events) == count
    assert not any(event[0] == "rmdir" for event in calls.events)
    assert coordination.facts()["fake_directory_removed"] is False
    assert _assert_close_once(calls, coordination) is True


def _quiet_coordination_call(monkeypatch, calls, operation, expected):
    import os
    import time

    count = len(calls.events)
    with monkeypatch.context() as patch:
        patch.setattr(time, "monotonic", calls.forbidden)
        patch.setattr(os, "getuid", calls.forbidden)
        assert operation() is expected
    assert len(calls.events) == count


@pytest.mark.parametrize(("state", "finished", "reason"), [
    ("new", True, None), ("acquire_failed", True, "interrupted"),
    ("held", True, None), ("creation_failed", False, "startup_error"),
    ("unbound", False, "startup_error"), ("bound_failed", True, "startup_error"),
    ("prepared", True, None),
])
@_scoped_stdlib_adapters
def test_b1_coordination_first_finish_is_terminal_and_cached(monkeypatch, state, finished, reason):
    module, calls, coordination, budget = _coordination_case(monkeypatch)
    if state == "acquire_failed":
        assert coordination.acquire_lock([True], budget) is False
    elif state != "new":
        assert coordination.acquire_lock([False], budget) is True
    if state == "creation_failed":
        calls.errors[("mkdtemp", "qwq-b1-runner-")] = OSError(5, "creation")
        assert coordination.prepare_fake_key() is None
    elif state == "unbound":
        calls.metadata[_FAKE_DIRECTORY].st_uid = 1002
        assert coordination.prepare_fake_key() is None
    elif state == "bound_failed":
        calls.contents = ["unexpected"]
        assert coordination.prepare_fake_key() is None
        calls.contents = []
    elif state == "prepared":
        assert coordination.prepare_fake_key() == _FAKE_PATH
    if state == "creation_failed":
        assert coordination.fake_key_path is None
        assert coordination.facts()["fake_key_path_sha256"] is None
    if state == "unbound":
        assert coordination.fake_key_path == _FAKE_PATH
        assert coordination.facts()["fake_key_path_sha256"] == _FAKE_HASH
    if state in ("bound_failed", "prepared"):
        assert coordination.finish_fake_key() is finished
        assert coordination.facts()["fake_directory_removed"] is True
    else:
        _quiet_coordination_call(monkeypatch, calls, coordination.finish_fake_key, finished)
        assert coordination.facts()["fake_directory_removed"] is False
    assert coordination.error == reason
    observed = coordination.facts()
    snapshot = coordination.controller_fds()
    _quiet_coordination_call(monkeypatch, calls, coordination.finish_fake_key, finished)
    _quiet_coordination_call(monkeypatch, calls, lambda: coordination.acquire_lock([False], budget), False)
    _quiet_coordination_call(monkeypatch, calls, coordination.prepare_fake_key, None)
    assert coordination.error == (reason or "startup_error")
    assert coordination.facts() == observed
    assert coordination.controller_fds() == snapshot
    assert _assert_close_once(calls, coordination) is True
    _quiet_coordination_call(monkeypatch, calls, coordination.finish_fake_key, finished)


@pytest.mark.parametrize("prepared", [False, True])
@_scoped_stdlib_adapters
def test_b1_coordination_first_finish_after_close_cannot_remove_or_restart(monkeypatch, prepared):
    module, calls, coordination, budget = _coordination_case(monkeypatch)
    if prepared:
        assert coordination.acquire_lock([False], budget) is True
        assert coordination.prepare_fake_key() == _FAKE_PATH
        assert coordination.check_lock_identity() is True
    observed = coordination.facts()
    assert _assert_close_once(calls, coordination) is True
    assert coordination.facts() == observed
    _quiet_coordination_call(monkeypatch, calls, coordination.finish_fake_key, False)
    _quiet_coordination_call(monkeypatch, calls, coordination.prepare_fake_key, None)
    _quiet_coordination_call(monkeypatch, calls, lambda: coordination.acquire_lock([False], budget), False)
    assert coordination.error == "startup_error"
    assert coordination.facts() == observed
    _quiet_coordination_call(monkeypatch, calls, coordination.check_lock_identity, False)
    assert coordination.facts()["lock_identity_stable"] is False
    assert coordination.facts()["fake_directory_removed"] is False
    assert not any(event[0] == "rmdir" for event in calls.events)


@pytest.mark.parametrize("failed_action", ["unlock", "close", "lock_close", "both"])
@_scoped_stdlib_adapters
def test_b1_coordination_finalization_failure_is_sticky_and_detaches_before_syscall(monkeypatch, failed_action):
    module, calls, coordination, budget = _coordination_case(monkeypatch)
    assert coordination.acquire_lock([False], budget) is True
    assert coordination.prepare_fake_key() == _FAKE_PATH
    assert coordination.finish_fake_key() is True
    owned = coordination.controller_fds()
    lock_fd = next(fd for fd in owned if calls.live[fd][0] == _LOCK_LITERAL)
    if failed_action in ("unlock", "both"):
        calls.errors[("flock", lock_fd)] = OSError(5, "unlock")
    if failed_action == "close":
        calls.errors[("close", owned[0])] = OSError(4, "close EINTR")
    if failed_action in ("lock_close", "both"):
        calls.errors[("close", lock_fd)] = OSError(9, "lock close EBADF")

    def already_detached(event):
        if event[0] in ("close", "flock"):
            assert coordination.controller_fds() == ()

    calls.on_event = already_detached
    assert _assert_close_once(calls, coordination) is False
    assert coordination.close_failed is True
    assert coordination.error == "io_error"
    _quiet_coordination_call(monkeypatch, calls, coordination.finish_fake_key, True)
    assert coordination.error == "io_error"


def _bound_paths_case(monkeypatch):
    module = _load_source("_b1_bound_paths_matrix", "pytest_evidence_controller.py")
    calls = _CoordinationSyscalls(monkeypatch, bound_paths=True)
    monkeypatch.setattr(module, "ROOT", Path("/fixture"))
    path = Path("/fixture/a/output")
    calls.metadata[str(path)] = SimpleNamespace(st_dev=7, st_ino=900,
        st_mode=0o100600, st_uid=1001, st_nlink=1)
    return module, calls, path


@pytest.mark.parametrize(("first_strict", "fail_fd"), [(False, None), (True, None), (False, 70), (True, 72)])
@_scoped_stdlib_adapters
def test_b1_bound_paths_strict_close_preserves_default_and_sticky_result(monkeypatch, first_strict, fail_fd):
    module, calls, path = _bound_paths_case(monkeypatch)
    bounds = module._BoundPaths([path])
    snapshot = bounds.controller_fds()
    assert type(snapshot) is tuple and snapshot == (70, 72)
    assert bounds.controller_fds() is not snapshot
    assert bounds.close_failed is False
    if fail_fd is not None:
        calls.errors[("close", fail_fd)] = OSError(4, "EINTR close")
    start = len(calls.events)

    def detached(event):
        if event[0] == "close":
            assert bounds.controller_fds() == ()

    calls.on_event = detached
    if first_strict:
        assert bounds.close(strict=True) is (fail_fd is None)
    else:
        assert bounds.close() is None
    assert sorted(event[1] for event in calls.events[start:] if event[0] == "close") == [70, 72]
    assert bounds.close_failed is (fail_fd is not None)
    assert calls.live == {}
    assert snapshot == (70, 72)
    end = len(calls.events)
    assert bounds.close(strict=True) is (fail_fd is None)
    assert bounds.close() is None
    assert len(calls.events) == end


@pytest.mark.parametrize("case", ["operation_only", "operation_then_close", "previous_close", "previous_then_current_close"])
@_scoped_stdlib_adapters
def test_b1_bound_paths_walk_exception_identity_and_close_once(monkeypatch, case):
    module, calls, path = _bound_paths_case(monkeypatch)
    bounds = module._BoundPaths([path])
    operation = FileNotFoundError(2, "component A")
    previous = OSError(5, "close B")
    current = OSError(9, "close C")
    expected = operation
    if case.startswith("operation"):
        calls.errors[("open", "/fixture/a")] = operation
    if case in ("operation_then_close", "previous_close", "previous_then_current_close"):
        calls.errors[("close", 73)] = previous
        expected = previous
    if case == "previous_then_current_close":
        calls.errors[("close", 74)] = current
        expected = current
    start = len(calls.events)
    with pytest.raises(OSError) as caught:
        bounds._walk(Path("/fixture/a"))
    assert caught.value is expected
    assert caught.value.errno == (2 if case == "operation_only" else 9 if case == "previous_then_current_close" else 5)
    closes = [event[1] for event in calls.events[start:] if event[0] == "close"]
    assert closes == ([73] if case.startswith("operation") else [73, 74])
    assert bounds.close_failed is (case != "operation_only")
    assert bounds.controller_fds() == (70, 72)
    assert bounds.close(strict=True) is (case == "operation_only")
    assert calls.live == {}


@pytest.mark.parametrize(("operation_fails", "close_fails"), [(True, False), (True, True), (False, True)])
@_scoped_stdlib_adapters
def test_b1_bound_paths_open_preserves_finally_exception_precedence(monkeypatch, operation_fails, close_fails):
    module, calls, path = _bound_paths_case(monkeypatch)
    bounds = module._BoundPaths([path])
    operation = OSError(13, "identity A")
    closing = OSError(5, "close B")
    if operation_fails:
        calls.errors[("fstat", "/fixture/a")] = operation
    if close_fails:
        calls.errors[("close", 74)] = closing
    start = len(calls.events)
    with pytest.raises(OSError) as caught:
        bounds.open(path, 0)
    assert caught.value is (closing if close_fails else operation)
    assert caught.value.errno == (5 if close_fails else 13)
    assert [event[1] for event in calls.events[start:] if event[0] == "close"] == [73, 74]
    assert not any(event[:2] == ("open", "/fixture/a/output") for event in calls.events)
    assert bounds.close_failed is close_fails
    assert bounds.close(strict=True) is (not close_fails)
    assert calls.live == {}


@pytest.mark.parametrize("close_fails", [False, True])
@_scoped_stdlib_adapters
def test_b1_bound_paths_create_preserves_fdopen_cleanup_exception_precedence(monkeypatch, close_fails):
    module, calls, path = _bound_paths_case(monkeypatch)
    bounds = module._BoundPaths([path])
    operation = OSError(12, "fdopen A")
    closing = OSError(5, "close B")
    calls.errors[("fdopen", 75)] = operation
    if close_fails:
        calls.errors[("close", 75)] = closing
    start = len(calls.events)
    with pytest.raises(OSError) as caught:
        bounds.create(path)
    assert caught.value is (closing if close_fails else operation)
    assert caught.value.errno == (5 if close_fails else 12)
    assert [event[1] for event in calls.events[start:] if event[0] == "close"] == [73, 74, 75]
    assert len([event for event in calls.events[start:] if event[0] == "fdopen"]) == 1
    assert bounds.close_failed is close_fails
    assert bounds.close(strict=True) is (not close_fails)
    assert calls.live == {}


@_scoped_stdlib_adapters
def test_b1_bound_paths_constructor_keeps_operation_error_after_default_close_failure(monkeypatch):
    module, calls, path = _bound_paths_case(monkeypatch)
    operation = FileNotFoundError(2, "parent A")
    closing = OSError(5, "root close B")
    calls.errors[("open", "/fixture/a")] = operation
    calls.errors[("close", 70)] = closing
    observed = []

    class ObservedBounds(module._BoundPaths):
        def close(self, *, strict=False):
            observed.append(self)
            return super().close(strict=strict)

    with pytest.raises(FileNotFoundError) as caught:
        ObservedBounds([path])
    assert caught.value is operation and caught.value.errno == 2
    assert len(observed) == 1
    bounds = observed[0]
    assert bounds.close_failed is True
    assert bounds.controller_fds() == ()
    assert [event[1] for event in calls.events if event[0] == "close"] == [71, 70]
    end = len(calls.events)
    assert bounds.close(strict=True) is False
    assert len(calls.events) == end
    assert calls.live == {}


@_scoped_stdlib_adapters
def test_b1_bound_paths_receipt_missing_component_then_close_eio_stays_invalid(monkeypatch):
    module, calls, path = _bound_paths_case(monkeypatch)
    bounds = module._BoundPaths([path])
    calls.errors[("open", "/fixture/a")] = FileNotFoundError(2, "missing component")
    calls.errors[("close", 73)] = OSError(5, "cleanup EIO")
    assert module._receipt(path, bounds) == {"state": "invalid", "bytes": 0, "sha256": None}
    assert bounds.close_failed is True
    assert bounds.close(strict=True) is False
    assert calls.live == {}


@pytest.mark.parametrize("phase", ["acquire", "check"])
@pytest.mark.parametrize("prelatched", [False, True])
@_scoped_stdlib_adapters
def test_b1_coordination_transient_close_failure_vetoes_operation(monkeypatch, phase, prelatched):
    module, calls, coordination, budget = _coordination_case(monkeypatch)
    if phase == "check":
        assert coordination.acquire_lock([False], budget) is True
        if prelatched:
            _quiet_coordination_call(monkeypatch, calls, lambda: coordination.acquire_lock([False], budget), False)
    elif prelatched:
        # acquire는 기존 error가 있으면 새 I/O 자체가 금지된다.
        assert coordination.acquire_lock([True], budget) is False
        _quiet_coordination_call(monkeypatch, calls, lambda: coordination.acquire_lock([False], budget), False)
        assert coordination.error == "interrupted"
        assert coordination.close_failed is False
        return
    failed = []

    def fail_once(event):
        if event[0] == "close" and not failed:
            failed.append(event[1])
            raise OSError(5, "transient close")

    calls.on_event = fail_once
    start = len(calls.events)
    result = coordination.acquire_lock([False], budget) if phase == "acquire" else coordination.check_lock_identity()
    assert result is False
    assert len(failed) == 1
    assert coordination.close_failed is True
    assert coordination.error == ("startup_error" if prelatched else "io_error")
    assert coordination.facts()["lock_identity_stable"] is False
    assert not any(event[0] == "flock" for event in calls.events[start:])
    assert failed[0] not in coordination.controller_fds()
    calls.on_event = lambda event: None
    assert _assert_close_once(calls, coordination) is False
    assert len([event for event in calls.events if event[:2] == ("close", failed[0])]) == 1


@pytest.mark.parametrize(("bad", "path"), [
    ("missing", _LOCK_LITERAL), ("open_error", "/home"),
    ("symlink", "/home"), ("not_directory", "/home"),
    ("parent_replaced", str(Path(_LOCK_LITERAL).parent)), ("leaf_replaced", _LOCK_LITERAL),
])
@_scoped_stdlib_adapters
def test_b1_coordination_failed_lock_binding_never_reaches_flock(monkeypatch, bad, path):
    module, calls, coordination, budget = _coordination_case(monkeypatch)
    if bad in ("missing", "open_error"):
        calls.errors[("open", path)] = OSError(2 if bad == "missing" else 13, "binding")
    elif bad in ("symlink", "not_directory"):
        calls.metadata[path].st_mode = 0o120777 if bad == "symlink" else 0o100600
    else:
        changed = dict(vars(calls.metadata[path]), st_ino=999)

        def replace(event):
            trigger = ("open", _LOCK_LITERAL) if bad == "parent_replaced" else ("fstat", _LOCK_LITERAL)
            if event[:2] == trigger:
                calls.metadata[path] = SimpleNamespace(**changed)

        calls.on_event = replace
    assert coordination.acquire_lock([False], budget) is False
    assert coordination.error == "startup_error"
    assert not any(event[0] == "flock" for event in calls.events)
    assert _assert_close_once(calls, coordination) is True


@pytest.mark.parametrize("bad", ["leaf_identity", "parent_identity", "mode", "uid", "nlink", "stat_error"])
@_scoped_stdlib_adapters
def test_b1_coordination_explicit_recheck_invalidates_stability_without_unlock(monkeypatch, bad):
    module, calls, coordination, budget = _coordination_case(monkeypatch)
    assert coordination.acquire_lock([False], budget) is True
    assert coordination.check_lock_identity() is True
    if bad == "stat_error":
        calls.errors[("stat", _LOCK_LITERAL)] = OSError(5, "stat")
    else:
        path = str(Path(_LOCK_LITERAL).parent) if bad == "parent_identity" else _LOCK_LITERAL
        metadata = dict(vars(calls.metadata[path]))
        key, value = {"leaf_identity": ("st_ino", 999), "parent_identity": ("st_ino", 999),
                      "mode": ("st_mode", 0o10600), "uid": ("st_uid", 1002), "nlink": ("st_nlink", 2)}[bad]
        metadata[key] = value
        calls.metadata[path] = SimpleNamespace(**metadata)
    start = len(calls.events)
    assert coordination.check_lock_identity() is False
    assert coordination.error == "identity_changed"
    assert coordination.facts()["lock_identity_stable"] is False
    assert coordination.facts()["lock_acquired"] is True
    assert not any(event[0] == "flock" for event in calls.events[start:])
    assert _assert_close_once(calls, coordination) is True


@pytest.mark.parametrize("boundary", ["poll", "success", "interrupted"])
@_scoped_stdlib_adapters
def test_b1_coordination_stopped_wins_at_post_syscall_boundaries(monkeypatch, boundary):
    module, calls, coordination, budget = _coordination_case(monkeypatch)
    stopped = [False]
    calls.polls = [896.0]
    if boundary == "poll":
        calls.flocks = [BlockingIOError(11, "busy")]
        calls.on_event = lambda event: stopped.__setitem__(0, True) if event[0] == "sleep" else None
    else:
        def stop():
            stopped[0] = True
            calls.now = 896.0
            if boundary == "interrupted":
                raise InterruptedError(4, "interrupted")
        calls.flocks = [stop]
    assert coordination.acquire_lock(stopped, budget) is False
    assert coordination.error == "interrupted"
    assert coordination.facts()["lock_acquired"] is (boundary == "success")
    assert _assert_close_once(calls, coordination) is True


@_scoped_stdlib_adapters
def test_b1_coordination_non_contention_flock_error_is_startup_error(monkeypatch):
    module, calls, coordination, budget = _coordination_case(monkeypatch)
    calls.flocks = [OSError(5, "flock EIO")]
    assert coordination.acquire_lock([False], budget) is False
    assert coordination.error == "startup_error"
    assert not any(event[0] == "sleep" for event in calls.events)
    assert _assert_close_once(calls, coordination) is True
    assert not any(event[0] == "flock" and event[2] == 8 for event in calls.events)


@pytest.mark.parametrize("returned", ["relative", "/var/qwq-b1-runner-x", "/tmp/qwq-b1-runner-",
                                       "/tmp/other", "/tmp/qwq-b1-runner-x/child"])
@_scoped_stdlib_adapters
def test_b1_coordination_invalid_returned_fake_name_never_confers_removal(monkeypatch, returned):
    module, calls, coordination, budget = _coordination_case(monkeypatch)
    assert coordination.acquire_lock([False], budget) is True
    calls.created_path = returned
    assert coordination.prepare_fake_key() is None
    assert coordination.error == "startup_error"
    assert coordination.fake_key_path is None
    assert coordination.facts()["fake_key_path_sha256"] is None
    _quiet_coordination_call(monkeypatch, calls, coordination.finish_fake_key, False)
    assert not any(event[0] == "rmdir" for event in calls.events)
    assert _assert_close_once(calls, coordination) is True


@pytest.mark.parametrize(("phase", "bad"), [
    ("prepare", "parent"), ("prepare", "leaf_stat"), ("prepare", "listing"),
    ("prepare", "dir_open"), ("prepare", "dir_fstat"), ("prepare", "dir_stat"),
    ("finish", "parent"), ("finish", "leaf_stat"), ("finish", "listing"), ("finish", "rmdir"),
    ("finish", "dir_fstat"), ("finish", "dir_stat"),
])
@_scoped_stdlib_adapters
def test_b1_coordination_fake_finalization_failures_preserve_unexpected_paths(monkeypatch, phase, bad):
    module, calls, coordination, budget = _coordination_case(monkeypatch)
    assert coordination.acquire_lock([False], budget) is True
    if phase == "finish":
        assert coordination.prepare_fake_key() == _FAKE_PATH
    if bad == "parent":
        replacement = SimpleNamespace(**dict(vars(calls.metadata["/tmp"]), st_ino=999))
        if phase == "prepare":
            calls.on_event = lambda event: calls.metadata.__setitem__("/tmp", replacement) if event[:2] == ("open", _FAKE_DIRECTORY) else None
        else:
            calls.metadata["/tmp"] = replacement
    elif bad == "leaf_stat":
        calls.errors[("stat", _FAKE_PATH)] = PermissionError(13, "not absence")
    elif bad == "rmdir":
        calls.errors[("rmdir", "qwq-b1-runner-fixed")] = OSError(39, "late entry")
    elif bad.startswith("dir_"):
        operation = {"dir_open": "open", "dir_fstat": "fstat", "dir_stat": "stat"}[bad]
        calls.errors[(operation, _FAKE_DIRECTORY)] = OSError(5, "directory metadata")
    else:
        calls.on_event = lambda event: (_ for _ in ()).throw(OSError(5, "listing")) if event[0] == "listdir" else None
    if phase == "prepare":
        assert coordination.prepare_fake_key() is None
        assert coordination.error == "startup_error"
    else:
        assert coordination.finish_fake_key() is False
        assert coordination.error == ("identity_changed" if bad == "parent" else "io_error")
        _quiet_coordination_call(monkeypatch, calls, coordination.finish_fake_key, False)
        assert coordination.facts()["fake_key_absent_after"] is (bad == "rmdir")
    assert coordination.facts()["fake_directory_removed"] is False
    assert len([event for event in calls.events if event[0] == "rmdir"]) == (1 if bad == "rmdir" else 0)
    assert _assert_close_once(calls, coordination) is True


@pytest.mark.parametrize("state", ["new", "failed_lock", "rejected_held_lock", "repeated_prepare"])
@_scoped_stdlib_adapters
def test_b1_coordination_prepare_guard_never_creates_or_retries(monkeypatch, state):
    module, calls, coordination, budget = _coordination_case(monkeypatch)
    reason = "startup_error"
    if state == "failed_lock":
        assert coordination.acquire_lock([True], budget) is False
        reason = "interrupted"
    elif state == "rejected_held_lock":
        calls.flocks = [lambda: setattr(calls, "now", 896.0)]
        assert coordination.acquire_lock([False], budget) is False
        reason = "timeout"
    elif state == "repeated_prepare":
        assert coordination.acquire_lock([False], budget) is True
        assert coordination.prepare_fake_key() == _FAKE_PATH
    observed = coordination.facts()
    snapshot = coordination.controller_fds()
    path = coordination.fake_key_path
    _quiet_coordination_call(monkeypatch, calls, coordination.prepare_fake_key, None)
    assert coordination.error == reason
    assert coordination.facts() == observed
    assert coordination.controller_fds() == snapshot
    assert coordination.fake_key_path == path
    assert _assert_close_once(calls, coordination) is True
    assert not any(event[0] == "rmdir" for event in calls.events)


@pytest.mark.parametrize("failed_lock", [False, True])
@_scoped_stdlib_adapters
def test_b1_coordination_missing_held_lock_check_is_quiet(monkeypatch, failed_lock):
    module, calls, coordination, budget = _coordination_case(monkeypatch)
    if failed_lock:
        assert coordination.acquire_lock([True], budget) is False
    _quiet_coordination_call(monkeypatch, calls, coordination.check_lock_identity, False)
    assert coordination.error == ("interrupted" if failed_lock else "startup_error")
    assert coordination.facts()["lock_identity_stable"] is False
    assert coordination.controller_fds() == ()


@_scoped_stdlib_adapters
def test_b1_coordination_close_failure_cannot_replace_prior_timeout(monkeypatch):
    module, calls, coordination, budget = _coordination_case(monkeypatch)
    calls.flocks = [lambda: setattr(calls, "now", 896.0)]
    assert coordination.acquire_lock([False], budget) is False
    assert coordination.error == "timeout"
    lock_fd = next(fd for fd, item in calls.live.items() if item[0] == _LOCK_LITERAL)
    calls.errors[("close", lock_fd)] = OSError(5, "close")
    assert _assert_close_once(calls, coordination) is False
    assert coordination.error == "timeout"
    assert coordination.close_failed is True


@_scoped_stdlib_adapters
def test_b1_coordination_owned_snapshots_are_pure_copies(monkeypatch):
    import os
    import time

    module, calls, coordination, budget = _coordination_case(monkeypatch)
    assert coordination.acquire_lock([False], budget) is True
    assert coordination.prepare_fake_key() == _FAKE_PATH
    expected_fds = tuple(sorted(calls.live))
    with monkeypatch.context() as patch:
        for name in ("open", "close", "dup", "fstat", "stat", "listdir", "getuid"):
            patch.setattr(os, name, calls.forbidden)
        patch.setattr(time, "monotonic", calls.forbidden)
        patch.setattr(fcntl, "flock", calls.forbidden)
        first = coordination.controller_fds()
        assert first == expected_fds and type(first) is tuple
        assert coordination.controller_fds() is not first
        facts = coordination.facts()
        facts["fake_key_absent_before"] = False
        assert coordination.facts()["fake_key_absent_before"] is True
    assert _assert_close_once(calls, coordination) is True
    assert first == expected_fds


@_scoped_stdlib_adapters
def test_b1_coordination_poll_sleep_is_capped_by_remaining_window(monkeypatch):
    module, calls, coordination, budget = _coordination_case(monkeypatch)
    calls.polls = [240.0]

    def busy_near_end():
        calls.now = 239.999
        raise BlockingIOError(11, "busy")

    calls.flocks = [busy_near_end]
    assert coordination.acquire_lock([False], budget) is False
    assert coordination.error == "lock_timeout"
    sleeps = [event[1] for event in calls.events if event[0] == "sleep"]
    assert sleeps and all(0 < seconds <= 0.001001 for seconds in sleeps)
    assert _assert_close_once(calls, coordination) is True


@_scoped_stdlib_adapters
def test_b1_bound_paths_create_temporary_close_failure_never_reaches_leaf_or_fdopen(monkeypatch):
    module, calls, path = _bound_paths_case(monkeypatch)
    bounds = module._BoundPaths([path])
    closing = OSError(5, "walk close")
    calls.errors[("close", 73)] = closing
    start = len(calls.events)
    with pytest.raises(OSError) as caught:
        bounds.create(path)
    assert caught.value is closing and caught.value.errno == 5
    assert [event[1] for event in calls.events[start:] if event[0] == "close"] == [73, 74]
    assert not any(event[0] == "fdopen" or event[:2] == ("open", "/fixture/a/output") for event in calls.events)
    assert bounds.close_failed is True
    assert bounds.close(strict=True) is False
    assert calls.live == {}


@pytest.mark.parametrize("path", [_LOCK_LITERAL, str(Path(_LOCK_LITERAL).parent)])
@_scoped_stdlib_adapters
def test_b1_coordination_post_flock_replacement_preserves_acquisition_but_rejects(monkeypatch, path):
    module, calls, coordination, budget = _coordination_case(monkeypatch)
    replacement = SimpleNamespace(**dict(vars(calls.metadata[path]), st_ino=999))
    calls.flocks = [lambda: calls.metadata.__setitem__(path, replacement)]
    assert coordination.acquire_lock([False], budget) is False
    assert coordination.error == "startup_error"
    assert coordination.facts()["lock_acquired"] is True
    assert not any(event[0] == "flock" and event[2] == 8 for event in calls.events)
    _quiet_coordination_call(monkeypatch, calls, coordination.prepare_fake_key, None)
    assert _assert_close_once(calls, coordination) is True


@pytest.mark.parametrize("stage", ["before_flock", "after_flock"])
@_scoped_stdlib_adapters
def test_b1_coordination_lock_fstat_error_never_claims_success(monkeypatch, stage):
    module, calls, coordination, budget = _coordination_case(monkeypatch)
    failure = OSError(5, "fstat")
    if stage == "before_flock":
        calls.errors[("fstat", _LOCK_LITERAL)] = failure
    else:
        calls.flocks = [lambda: calls.errors.__setitem__(("fstat", _LOCK_LITERAL), failure)]
    assert coordination.acquire_lock([False], budget) is False
    assert coordination.error == "startup_error"
    assert coordination.facts()["lock_acquired"] is (stage == "after_flock")
    assert _assert_close_once(calls, coordination) is True
