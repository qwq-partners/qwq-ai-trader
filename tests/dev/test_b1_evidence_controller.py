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


class _OutputCase:
    """출력 전용 syscall script. 생성은 stdlib patch보다 먼저 끝난다."""

    def __init__(self, stdout=(), stderr=(), *, budget=True):
        self.module = _load_source("_b1_output_matrix", "pytest_evidence_controller.py")
        self.budget = self.module._B1Budget(0.0) if budget else None
        self.owner = self.module._Owner(budget=self.budget)
        self.owner.leader = 41
        self.pipes = {"stdout": 31, "stderr": 32, "control": 33}
        self.logs = {"stdout": io.BytesIO(), "stderr": io.BytesIO()}
        frame = json.dumps({"schema": "qwq.pytest-guard-ready/v1", "guard": _guard_fact()}).encode() + b"\n"
        self.chunks = {31: list(stdout) + [b""], 32: list(stderr) + [b""], 33: [frame, b""]}
        self.events = []
        self.now = 1.0
        self.times = []
        self.passes = 0
        self.echild = True
        self.stopped = [False]

    def read(self, fd, size):
        assert size == 65536
        self.events.append(("read", self.passes, fd))
        item = self.chunks[fd].pop(0)
        if isinstance(item, BaseException):
            raise item
        assert type(item) is bytes and len(item) <= 65536
        return item

    def waitpid(self, pid, flags):
        assert flags == 1073741825
        self.events.append(("wait", pid))
        if pid == 41:
            return 41, 0
        assert pid == -1
        if self.echild:
            raise ChildProcessError
        return 0, 0

    def monotonic(self):
        self.events.append(("clock",))
        return self.now

    def sleep(self, seconds):
        self.events.append(("sleep", seconds))
        self.passes += 1
        assert self.passes < 300, "고정 output trace 소진"
        if self.times:
            self.now = self.times.pop(0)


def _run_output_case(monkeypatch, case, **options):
    import os
    import time

    targets = [(os, "read", case.read), (os, "waitpid", case.waitpid),
               (os, "close", lambda fd: case.events.append(("close", fd))),
               (time, "monotonic", case.monotonic), (time, "sleep", case.sleep),
               (case.module._Owner, "signal_children", lambda owner, sig: case.events.append(("signal", sig)))]
    originals = [(owner, name, getattr(owner, name)) for owner, name, _ in targets]
    try:
        with monkeypatch.context() as patch:
            for owner, name, replacement in targets:
                patch.setattr(owner, name, replacement)
            return case.module._observe(
                case.owner, case.pipes, case.logs, _guard_fact(), 896.0, case.stopped, **options,
            )
    finally:
        assert all(getattr(owner, name) is original for owner, name, original in originals)


def test_b1_observer_exact_cap_has_observed_bytes(monkeypatch):
    import hashlib

    retained = b"A" * 2097152
    expected_hash = hashlib.sha256(retained).hexdigest()
    case = _OutputCase([b"A" * 65536] * 32)
    complete, guard, streams = _run_output_case(monkeypatch, case, profile="b1-standard/v1")
    assert complete is True and guard == _guard_fact()
    assert case.owner.error is None
    assert case.owner.returncode == 0
    assert case.pipes == {}
    assert case.logs["stdout"].getvalue() == retained
    assert streams == {
        "stdout": {"bytes": 2097152, "sha256": expected_hash, "overflow": False, "observed_bytes": 2097152},
        "stderr": {"bytes": 0, "sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
                   "overflow": False, "observed_bytes": 0},
    }
    assert list(streams["stdout"]) == ["bytes", "sha256", "overflow", "observed_bytes"]
    assert [event[1] for event in case.events if event[0] == "close"] == [32, 33, 31]


def _output_chunks(payload):
    return [payload[index:index + 65536] for index in range(0, len(payload), 65536)]


class _OutputSink:
    def __init__(self, *, at=None, result=None, physical=0):
        self.at = at
        self.result = result
        self.physical = physical
        self.offered = []
        self.data = bytearray()

    def write(self, data):
        self.offered.append(data)
        if len(self.offered) - 1 == self.at:
            if isinstance(self.result, BaseException):
                self.data.extend(data[:self.physical])
                raise self.result
            if type(self.result) is int and 0 <= self.result <= len(data):
                self.data.extend(data[:self.result])
            return self.result
        self.data.extend(data)
        return len(data)


class _OutputAbort(BaseException):
    pass


class _OutputCount(int):
    pass


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
@pytest.mark.parametrize("size", [0, 2097151, 2097152, 2097153, 2097160])
def test_b1_stream_boundaries_keep_patterned_prefix_and_saturate_observation(monkeypatch, stream, size):
    import hashlib

    prefix = b"abcd" * 524288
    payload = (prefix + b"!discarded")[:size]
    case = _OutputCase(**{stream: _output_chunks(payload)})
    sink = _OutputSink()
    case.logs[stream] = sink
    complete, guard, streams = _run_output_case(monkeypatch, case, profile="b1-standard/v1")
    retained = prefix[:size]
    assert complete is True and guard == _guard_fact()
    assert streams[stream] == {
        "bytes": min(size, 2097152), "sha256": hashlib.sha256(retained).hexdigest(),
        "overflow": size > 2097152, "observed_bytes": min(size, 2097153),
    }
    assert bytes(sink.data) == retained
    assert all(sink.offered)
    assert case.owner.error == ("output_limit" if size > 2097152 else None)
    assert set(case.owner.observation) == {"streams", "observed", "failed_logs", "control", "guard", "startup_end", "profile"}
    assert list(case.owner.observation["streams"][stream]) == ["bytes", "sha256", "overflow"]


def test_b1_final_chunk_overflow_writes_only_remaining_three_bytes(monkeypatch):
    import hashlib

    case = _OutputCase([b"A" * 65536] * 31 + [b"A" * 65533, b"XYZ!", b"discarded"])
    sink = _OutputSink()
    case.logs["stdout"] = sink
    complete, _, streams = _run_output_case(monkeypatch, case, profile="b1-standard/v1")
    assert complete is True
    assert case.owner.reaped is True and case.owner.returncode == 0
    assert case.owner.error == "output_limit"
    assert sink.offered[-1] == b"XYZ"
    assert len(sink.offered) == 33
    assert bytes(sink.data) == b"A" * 2097149 + b"XYZ"
    assert streams["stdout"] == {"bytes": 2097152,
        "sha256": hashlib.sha256(b"A" * 2097149 + b"XYZ").hexdigest(),
        "overflow": True, "observed_bytes": 2097153}
    assert case.events.index(("wait", 41)) < case.events.index(("read", 32, 31))
    assert case.pipes == {}


def test_b1_simultaneous_stream_drain_keeps_independent_caps_and_bounded_passes(monkeypatch):
    import hashlib

    case = _OutputCase(_output_chunks(b"A" * 2097152 + b"!"), _output_chunks(b"B" * 2097152 + b"?"))
    complete, guard, streams = _run_output_case(monkeypatch, case, profile="b1-standard/v1")
    assert complete is True and guard == _guard_fact()
    for stream, byte in (("stdout", b"A"), ("stderr", b"B")):
        assert streams[stream] == {"bytes": 2097152, "sha256": hashlib.sha256(byte * 2097152).hexdigest(),
                                   "overflow": True, "observed_bytes": 2097153}
        assert case.logs[stream].getvalue() == byte * 2097152
    reads = [event for event in case.events if event[0] == "read"]
    assert reads[:6] == [("read", 0, 31), ("read", 0, 32), ("read", 0, 33),
                         ("read", 1, 31), ("read", 1, 32), ("read", 1, 33)]
    assert len({(event[1], event[2]) for event in reads}) == len(reads)
    assert case.owner.error == "output_limit"


@pytest.mark.parametrize("reported", [0, 1, 7, 8])
def test_b1_short_write_accounting_disables_only_failed_stream(monkeypatch, reported):
    import hashlib

    case = _OutputCase([b"ABCDEFGH", b"later"], [b"other"])
    sink = _OutputSink(at=0, result=reported)
    case.logs["stdout"] = sink
    complete, _, streams = _run_output_case(monkeypatch, case, profile="b1-standard/v1")
    expected = b"ABCDEFGH" + b"later" if reported == 8 else b"ABCDEFGH"[:reported]
    assert complete is True
    assert streams["stdout"] == {"bytes": len(expected), "sha256": hashlib.sha256(expected).hexdigest(),
                                  "overflow": False, "observed_bytes": 13}
    assert sink.offered == ([b"ABCDEFGH", b"later"] if reported == 8 else [b"ABCDEFGH"])
    assert bytes(sink.data) == expected
    assert case.logs["stderr"].getvalue() == b"other"
    assert case.owner.observation["failed_logs"] == (set() if reported == 8 else {"stdout"})
    assert case.owner.error == (None if reported == 8 else "io_error")


@pytest.mark.parametrize("kind", ["none", "bool", "false", "float", "int_subclass", "negative", "too_large", "oserror", "partial_oserror"])
@pytest.mark.parametrize("crossing", [False, True])
def test_b1_write_failure_still_detects_overflow_and_preserves_first_reason(monkeypatch, kind, crossing):
    import hashlib

    if crossing:
        chunks = [b"A" * 65536] * 31 + [b"A" * 65533, b"XYZ!", b"tail"]
        at, offered, kept, reason = 32, b"XYZ", b"A" * 2097149, "output_limit"
    else:
        chunks = [b"ABCDEFGH"] + _output_chunks(b"A" * 2097152) + [b"tail"]
        at, offered, kept, reason = 0, b"ABCDEFGH", b"", "io_error"
    failure = {"none": None, "bool": True, "false": False, "float": 1.0, "int_subclass": _OutputCount(1), "negative": -1,
               "too_large": len(offered) + 1, "oserror": OSError(5, "write"),
               "partial_oserror": BlockingIOError(11, "partial physical write", 2)}[kind]
    case = _OutputCase(chunks, [b"ok"])
    sink = _OutputSink(at=at, result=failure, physical=2 if kind == "partial_oserror" else 0)
    case.logs["stdout"] = sink
    complete, _, streams = _run_output_case(monkeypatch, case, profile="b1-standard/v1")
    assert complete is True
    assert case.owner.error == reason
    assert streams["stdout"] == {"bytes": len(kept), "sha256": hashlib.sha256(kept).hexdigest(),
                                  "overflow": True, "observed_bytes": 2097153}
    assert len(sink.offered) == at + 1 and sink.offered[-1] == offered
    assert bytes(sink.data) == kept + (offered[:2] if kind == "partial_oserror" else b"")
    assert case.owner.observation["failed_logs"] == {"stdout"}
    assert case.logs["stderr"].getvalue() == b"ok"


@pytest.mark.parametrize("reported", [0, 1, 2])
def test_b1_crossing_short_write_latches_output_before_io_error(monkeypatch, reported):
    import hashlib

    case = _OutputCase([b"A" * 65536] * 31 + [b"A" * 65533, b"XYZ!", b"later"])
    sink = _OutputSink(at=32, result=reported)
    case.logs["stdout"] = sink
    complete, _, streams = _run_output_case(monkeypatch, case, profile="b1-standard/v1")
    retained = b"A" * 2097149 + b"XYZ"[:reported]
    assert complete is True and case.owner.error == "output_limit"
    assert streams["stdout"] == {"bytes": 2097149 + reported, "sha256": hashlib.sha256(retained).hexdigest(),
                                  "overflow": True, "observed_bytes": 2097153}
    assert bytes(sink.data) == retained and len(sink.offered) == 33
    assert case.owner.observation["failed_logs"] == {"stdout"}


@pytest.mark.parametrize("error_type", [ValueError, RuntimeError, _OutputAbort])
@pytest.mark.parametrize("earlier", [None, "interrupted"])
def test_b1_non_oserror_write_marks_failed_then_reraises_same_exception_for_retry(monkeypatch, error_type, earlier):
    import hashlib

    failure = error_type("scripted write abort")
    case = _OutputCase([b"AB", b"CD", b"later"], [b"other"])
    if earlier is not None:
        case.owner.reject(earlier)
    sink = _OutputSink(at=1, result=failure)
    case.logs["stdout"] = sink
    with pytest.raises(error_type) as caught:
        _run_output_case(monkeypatch, case, profile="b1-standard/v1")
    assert caught.value is failure
    state = case.owner.observation
    hasher = state["streams"]["stdout"]["sha256"]
    control = state["control"]
    failed_logs, observed = state["failed_logs"], state["observed"]
    assert case.owner.error == (earlier or "io_error")
    assert state["failed_logs"] == {"stdout"}
    assert state["observed"]["stdout"] == 4
    assert (state["startup_end"], case.owner.finish_end, case.budget.cleanup_end, case.budget.term_end) == (11.0, 4.0, 4.0, 2.0)
    complete, guard, streams = _run_output_case(monkeypatch, case, profile="b1-standard/v1")
    assert complete is True and guard == _guard_fact()
    assert case.owner.observation is state and state["streams"]["stdout"]["sha256"] is hasher
    assert state["control"] is control
    assert state["failed_logs"] is failed_logs and state["observed"] is observed
    assert streams["stdout"] == {"bytes": 2, "sha256": hashlib.sha256(b"AB").hexdigest(),
                                  "overflow": False, "observed_bytes": 9}
    assert sink.offered == [b"AB", b"CD"] and bytes(sink.data) == b"AB"
    assert (state["startup_end"], case.owner.finish_end, case.budget.cleanup_end, case.budget.term_end) == (11.0, 4.0, 4.0, 2.0)


class _OutputProfile(str):
    pass


class _OutputEqualProfile:
    def __eq__(self, other):
        return True


@pytest.mark.parametrize("profile", [True, False, 1, "", "other", _OutputProfile("b1-standard/v1"), _OutputEqualProfile()],
                         ids=["true", "false", "int", "empty", "unknown", "str-subclass", "custom-equality"])
def test_b1_observer_profile_contract_rejects_before_budget_sync_or_io(monkeypatch, profile):
    case = _OutputCase([b"unread"])
    case.budget.begin_cleanup(100.0)
    with pytest.raises(ValueError, match="^PROCESS_OBSERVATION_PROFILE$"):
        _run_output_case(monkeypatch, case, profile=profile)
    assert case.events == []
    assert case.owner.observation is None and case.owner.finish_end is None
    assert case.owner.error is None and case.owner.reaped is False
    assert (case.budget.cleanup_end, case.budget.term_end) == (103.0, 101.0)
    assert (case.budget.total_end, case.budget.run_end) == (900.0, 896.0)
    assert case.pipes == {"stdout": 31, "stderr": 32, "control": 33}
    assert case.logs["stdout"].getvalue() == b""


def test_b1_observer_profile_contract_requires_existing_budget_without_consuming_pipes(monkeypatch):
    case = _OutputCase([b"unread"], budget=False)
    with pytest.raises(ValueError, match="^PROCESS_OBSERVATION_PROFILE$"):
        _run_output_case(monkeypatch, case, profile="b1-standard/v1")
    assert case.events == [] and case.owner.observation is None
    assert case.owner.finish_end is None and case.owner.error is None
    assert case.pipes == {"stdout": 31, "stderr": 32, "control": 33}


@pytest.mark.parametrize(("marker", "requested"), [
    ("missing", "b1-standard/v1"), ("b1-standard/v1", None),
    (None, None), (None, "b1-standard/v1"), ("unknown", None),
    (True, "b1-standard/v1"), (_OutputProfile("b1-standard/v1"), "b1-standard/v1"),
    (_OutputEqualProfile(), "b1-standard/v1"),
], ids=["no-promotion", "no-downgrade", "none-not-missing", "none-not-b1", "bad-marker",
        "boolean-marker", "subclass-marker", "equality-marker"])
def test_b1_observer_profile_contract_reentry_marker_is_exact_and_validation_precedes_sync(monkeypatch, marker, requested):
    import hashlib

    case = _OutputCase([b"unread"])
    state = {"streams": {name: {"bytes": 0, "sha256": hashlib.sha256(), "overflow": False}
                         for name in ("stdout", "stderr")},
             "observed": {"stdout": 0, "stderr": 0}, "failed_logs": set(),
             "control": bytearray(b"saved"), "guard": None, "startup_end": 11.0}
    if type(marker) is not str or marker != "missing":
        state["profile"] = marker
    case.owner.observation = state
    case.budget.begin_cleanup(100.0)
    hasher = state["streams"]["stdout"]["sha256"]
    with pytest.raises(ValueError, match="^PROCESS_OBSERVATION_PROFILE$"):
        _run_output_case(monkeypatch, case, profile=requested)
    assert case.events == []
    assert case.owner.observation is state and case.owner.finish_end is None
    assert case.owner.error is None and case.owner.reaped is False
    assert state["control"] == b"saved" and state["startup_end"] == 11.0
    assert state["observed"] == {"stdout": 0, "stderr": 0} and state["failed_logs"] == set()
    assert state["streams"]["stdout"]["sha256"] is hasher
    assert hasher.hexdigest() == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    if type(marker) is str and marker == "missing":
        assert "profile" not in state
    else:
        assert state["profile"] is marker
    assert (case.budget.cleanup_end, case.budget.term_end) == (103.0, 101.0)


@pytest.mark.parametrize("profile", [None, "b1-standard/v1"])
def test_b1_observer_reentry_preserves_output_hash_control_and_deadlines(monkeypatch, profile):
    import hashlib

    interruption = RuntimeError("read boundary")
    case = _OutputCase([b"AB", interruption, b"CD"])
    with pytest.raises(RuntimeError) as caught:
        _run_output_case(monkeypatch, case, profile=profile)
    assert caught.value is interruption
    state = case.owner.observation
    hasher = state["streams"]["stdout"]["sha256"]
    saved_control = bytes(state["control"])
    assert saved_control and state["guard"] is None
    assert case.pipes == {"stdout": 31, "control": 33}
    complete, guard, streams = _run_output_case(monkeypatch, case, profile=profile)
    assert complete is True and guard == _guard_fact()
    assert case.owner.observation is state and state["streams"]["stdout"]["sha256"] is hasher
    assert bytes(state["control"]) == saved_control
    expected = {"bytes": 4, "sha256": hashlib.sha256(b"ABCD").hexdigest(), "overflow": False}
    if profile is not None:
        expected["observed_bytes"] = 4
        assert state["profile"] == "b1-standard/v1"
    else:
        assert "profile" not in state
    assert streams["stdout"] == expected
    assert case.logs["stdout"].getvalue() == b"ABCD"
    assert (state["startup_end"], case.owner.finish_end, case.budget.cleanup_end, case.budget.term_end) == (11.0, 4.0, 4.0, 2.0)
    streams["stdout"]["bytes"] = 999
    streams["stdout"]["sha256"] = "not live hash"
    assert state["streams"]["stdout"]["bytes"] == 4
    assert hasher.hexdigest() == hashlib.sha256(b"ABCD").hexdigest()
    assert state["observed"]["stdout"] == 4


@pytest.mark.parametrize("budget", [False, True])
def test_b1_output_preserves_v1_retained_detection_byte_and_empty_prefix_write(monkeypatch, budget):
    import hashlib

    case = _OutputCase(_output_chunks(b"A" * 8388608 + b"Z!") + [b"later"], budget=budget)
    sink = _OutputSink()
    case.logs["stdout"] = sink
    complete, _, streams = _run_output_case(monkeypatch, case)
    assert complete is True and case.owner.error == "output_limit"
    assert streams["stdout"] == {"bytes": 8388609, "sha256": hashlib.sha256(b"A" * 8388608 + b"Z").hexdigest(), "overflow": True}
    assert bytes(sink.data) == b"A" * 8388608 + b"Z"
    assert sink.offered[-2:] == [b"Z", b""]
    assert "profile" not in case.owner.observation
    assert case.owner.observation["observed"]["stdout"] == 8388609


@pytest.mark.parametrize("budget", [False, True])
def test_b1_output_preserves_v1_write_before_overflow_error_order(monkeypatch, budget):
    import hashlib

    case = _OutputCase([b"A" * 65536] * 127 + [b"A" * 65535, b"XYZ"], budget=budget)
    sink = _OutputSink(at=128, result=OSError(5, "write"))
    case.logs["stdout"] = sink
    complete, _, streams = _run_output_case(monkeypatch, case, profile=None)
    assert complete is True and case.owner.error == "io_error"
    assert streams["stdout"] == {"bytes": 8388607, "sha256": hashlib.sha256(b"A" * 8388607).hexdigest(), "overflow": True}
    assert sink.offered[-1] == b"XY"
    assert case.owner.observation["observed"]["stdout"] == 8388609


@pytest.mark.parametrize("reason", ["interrupted", "timeout", "cleanup_error"])
def test_b1_output_cannot_replace_an_earlier_reason(monkeypatch, reason):
    case = _OutputCase([b"first"] + _output_chunks(b"A" * 2097153))
    case.owner.reject(reason)
    case.logs["stdout"] = _OutputSink(at=0, result=OSError(5, "write"))
    complete, _, streams = _run_output_case(monkeypatch, case, profile="b1-standard/v1")
    assert complete is True and case.owner.error == reason
    assert streams["stdout"]["bytes"] == 0
    assert streams["stdout"]["observed_bytes"] == 2097153
    assert streams["stdout"]["overflow"] is True


@pytest.mark.parametrize("stderr_first", [False, True])
def test_b1_same_pass_stream_errors_follow_pipe_order(monkeypatch, stderr_first):
    case = _OutputCase([b"A" * 65536] * 31 + [b"A" * 65533, b"XYZ!"],
                       [BlockingIOError()] * 32 + [b"oops"])
    case.logs["stderr"] = _OutputSink(at=0, result=OSError(5, "write"))
    if stderr_first:
        case.pipes = {"stderr": 32, "stdout": 31, "control": 33}
    complete, _, streams = _run_output_case(monkeypatch, case, profile="b1-standard/v1")
    assert complete is True
    assert case.owner.error == ("io_error" if stderr_first else "output_limit")
    assert streams["stdout"]["observed_bytes"] == 2097153
    assert streams["stderr"]["observed_bytes"] == 4 and streams["stderr"]["bytes"] == 0


@pytest.mark.parametrize("error_type", [BlockingIOError, InterruptedError, OSError])
def test_b1_read_errors_and_eof_do_not_increment_stream_observation(monkeypatch, error_type):
    case = _OutputCase([error_type(), b"X"])
    complete, _, streams = _run_output_case(monkeypatch, case, profile="b1-standard/v1")
    assert complete is True
    assert streams["stdout"]["observed_bytes"] == 1 and streams["stdout"]["bytes"] == 1
    assert case.owner.error == ("io_error" if error_type is OSError else None)


@pytest.mark.parametrize("missing", ["eof", "echild"])
def test_b1_stream_completion_still_requires_eof_and_echild(monkeypatch, missing):
    case = _OutputCase([BlockingIOError(), BlockingIOError()] if missing == "eof" else [])
    case.echild = missing != "echild"
    case.times = [4.0]
    complete, _, streams = _run_output_case(monkeypatch, case, profile="b1-standard/v1")
    assert complete is False and case.owner.error == "cleanup_error"
    assert case.pipes == {}
    assert streams["stdout"]["bytes"] == 0 and streams["stdout"]["observed_bytes"] == 0


@pytest.mark.parametrize("budget", [False, True])
def test_b1_output_preserves_v1_unexpected_write_exception_state_and_retry(monkeypatch, budget):
    import hashlib

    failure = RuntimeError("legacy write failure")
    case = _OutputCase([b"first", b"next"], budget=budget)
    sink = _OutputSink(at=0, result=failure)
    case.logs["stdout"] = sink
    with pytest.raises(RuntimeError) as caught:
        _run_output_case(monkeypatch, case)
    assert caught.value is failure
    assert case.owner.error is None
    assert case.owner.observation["failed_logs"] == set()
    complete, _, streams = _run_output_case(monkeypatch, case)
    assert complete is True and case.owner.error is None
    assert streams["stdout"] == {"bytes": 4, "sha256": hashlib.sha256(b"next").hexdigest(), "overflow": False}
    assert case.owner.observation["observed"]["stdout"] == 9
    assert sink.offered == [b"first", b"next"] and bytes(sink.data) == b"next"


def test_b1_guard_failure_does_not_count_control_bytes_as_stream_output(monkeypatch):
    case = _OutputCase([b"X"])
    case.chunks[33] = [b"invalid guard\n", b""]
    complete, guard, streams = _run_output_case(monkeypatch, case, profile="b1-standard/v1")
    assert complete is True and guard is None
    assert case.owner.error == "startup_error"
    assert streams["stdout"]["bytes"] == 1 and streams["stdout"]["observed_bytes"] == 1
    assert streams["stderr"]["bytes"] == 0 and streams["stderr"]["observed_bytes"] == 0


@pytest.mark.parametrize("initial", [None, "b1-standard/v1"])
def test_b1_observer_profile_switch_after_bytes_rejects_without_consuming_or_resetting(monkeypatch, initial):
    import hashlib

    case = _OutputCase([b"AB", RuntimeError("pause"), b"CD"])
    with pytest.raises(RuntimeError):
        _run_output_case(monkeypatch, case, profile=initial)
    state = case.owner.observation
    before = len(case.events)
    pending = dict(case.pipes)
    requested = "b1-standard/v1" if initial is None else None
    with pytest.raises(ValueError, match="^PROCESS_OBSERVATION_PROFILE$"):
        _run_output_case(monkeypatch, case, profile=requested)
    assert len(case.events) == before and case.pipes == pending
    assert case.owner.observation is state
    assert state["observed"] == {"stdout": 2, "stderr": 0}
    assert state["streams"]["stdout"]["bytes"] == 2
    assert state["streams"]["stdout"]["sha256"].hexdigest() == hashlib.sha256(b"AB").hexdigest()
    assert case.logs["stdout"].getvalue() == b"AB"


def test_b1_crossing_baseexception_keeps_output_limit_before_marked_write_failure(monkeypatch):
    failure = _OutputAbort("crossing write")
    case = _OutputCase([b"A" * 65536] * 31 + [b"A" * 65533, b"XYZ!", b"later"])
    sink = _OutputSink(at=32, result=failure)
    case.logs["stdout"] = sink
    with pytest.raises(_OutputAbort) as caught:
        _run_output_case(monkeypatch, case, profile="b1-standard/v1")
    assert caught.value is failure
    assert case.owner.error == "output_limit"
    assert case.owner.observation["failed_logs"] == {"stdout"}
    assert case.owner.observation["observed"]["stdout"] == 2097153
    assert case.owner.observation["streams"]["stdout"]["overflow"] is True
    complete, _, streams = _run_output_case(monkeypatch, case, profile="b1-standard/v1")
    assert complete is True and case.owner.error == "output_limit"
    assert streams["stdout"]["bytes"] == 2097149
    assert streams["stdout"]["observed_bytes"] == 2097153
    assert len(sink.offered) == 33


def test_b1_probe_child_systemexit_close_still_reaches_terminal_125(monkeypatch):
    module = _load_source("_b1_probe_child_terminal_prerequisite", "pytest_evidence_controller.py")
    import os
    import time

    budget = module._B1Budget(0.0)
    events = []
    close_failure = SystemExit(0)
    escaped = None

    def fork():
        events.append(("fork", 0))
        return 0

    def close(fd):
        events.append(("close", fd))
        if fd == 71:
            raise close_failure

    def terminal(code):
        events.append(("exit", code))
        raise _ProbeExit(code)

    def forbidden(*args, **kwargs):
        raise AssertionError("probe child가 parent finalization/unlock/workload에 진입함")

    targets = [
        (os, "fork", fork), (os, "close", close), (os, "_exit", terminal),
        (time, "monotonic", lambda: 1.0), (fcntl, "flock", forbidden),
        (module, "_emergency_cleanup", forbidden), (module, "_RawPopen", forbidden),
        (module._BoundPaths, "close", forbidden),
        (module._B1Coordination, "close", forbidden),
        (module._B1Coordination, "finish_fake_key", forbidden),
    ]
    originals = [(owner, name, getattr(owner, name)) for owner, name, _ in targets]
    try:
        with monkeypatch.context() as patch:
            for owner, name, replacement in targets:
                patch.setattr(owner, name, replacement)
            try:
                module._probe(896.0, [False], budget=budget, controller_fds=(71, 72, 73))
            except BaseException as error:
                escaped = error
    finally:
        assert all(getattr(owner, name) is original for owner, name, original in originals)

    assert isinstance(escaped, _ProbeExit)
    assert escaped is not close_failure and escaped.code == 125
    assert events == [("fork", 0), ("close", 71), ("close", 72), ("close", 73), ("exit", 125)]
    assert (budget.cleanup_end, budget.term_end) == (None, None)


def _prerequisite_forbidden(*args, **kwargs):
    """명시적 금지 adapter marker: scoped caller가 태그 원장부터 기록한다."""
    raise AssertionError("prerequisite forbidden boundary")


def _prerequisite_call(monkeypatch, targets, operation):
    """예외도 patch 안에서 포착하되 결과 검사는 stdlib 복원 뒤 호출자가 한다."""
    originals = [(owner, name, getattr(owner, name)) for owner, name, _ in targets]
    forbidden_attempts = []
    result, escaped = None, None
    try:
        with monkeypatch.context() as patch:
            for owner, name, replacement in targets:
                if replacement is _prerequisite_forbidden:
                    def recorded_forbidden(*args, _tag=name, **kwargs):
                        forbidden_attempts.append((_tag, args, kwargs))
                        return _prerequisite_forbidden(*args, **kwargs)
                    replacement = recorded_forbidden
                patch.setattr(owner, name, replacement)
            try:
                result = operation(patch)
            except BaseException as error:
                escaped = error
    finally:
        for owner, name, original in originals:
            restored = getattr(owner, name)
            assert restored is original or (
                hasattr(original, "__func__") and getattr(restored, "__func__", None) is original.__func__
                and getattr(restored, "__self__", None) is original.__self__
            )
    if forbidden_attempts:
        raise AssertionError(("forbidden attempts survived candidate catch", forbidden_attempts))
    return result, escaped


@pytest.mark.parametrize("failed_fd", [None, 71, 72])
@pytest.mark.parametrize("error_type", [SystemExit, KeyboardInterrupt, RuntimeError])
@pytest.mark.parametrize("b1", [False, True])
def test_b1_prerequisite_child_terminal_and_v1_exception_identity(monkeypatch, failed_fd, error_type, b1):
    module = _load_source("_b1_child_baseexceptions", "pytest_evidence_controller.py")
    import os
    import time

    events = []
    failure = error_type(0)
    def close(fd):
        events.append(("close", fd))
        if fd == failed_fd:
            raise failure
    def terminal(code):
        events.append(("exit", code))
        raise _ProbeExit(code)
    forbidden = _prerequisite_forbidden
    targets = [(os, "fork", lambda: 0), (os, "close", close), (os, "_exit", terminal),
               (os, "waitpid", forbidden), (time, "monotonic", lambda: 100.0),
               (time, "sleep", forbidden), (fcntl, "flock", forbidden),
               (os, "pidfd_open", forbidden), (module.signal, "pidfd_send_signal", forbidden),
               (module, "_emergency_cleanup", forbidden)]
    budget = module._B1Budget(0.0) if b1 else None
    _, escaped = _prerequisite_call(monkeypatch, targets,
        lambda patch: module._probe(896.0, [False], budget=budget, controller_fds=(71, 72, 73)))
    if failed_fd is None:
        assert isinstance(escaped, _ProbeExit) and escaped.code == 23
        assert events == [("close", 71), ("close", 72), ("close", 73), ("exit", 23)]
    elif b1:
        assert isinstance(escaped, _ProbeExit) and escaped.code == 125
        assert events == [("close", 71), ("close", 72), ("close", 73), ("exit", 125)]
    else:
        assert escaped is failure
        assert events == ([("close", 71)] if failed_fd == 71 else [("close", 71), ("close", 72)])


@pytest.mark.parametrize("stage", ["prefork_clock", "fork", "clock", "clock_no_fresh", "reap", "sleep", "ordinary_cleanup"])
@pytest.mark.parametrize("emergency_raises", [False, True])
def test_b1_prerequisite_probe_keeps_one_owner_and_first_exception(monkeypatch, stage, emergency_raises):
    module = _load_source("_b1_probe_exception_owner", "pytest_evidence_controller.py")
    import os
    import time

    budget = module._B1Budget(0.0)
    original_owner = module._Owner
    owners, events, emergencies, obligations = [], [], [], []
    first, second = RuntimeError("probe first"), SystemExit(0)
    state = {"forked": False, "clock_failed": False, "now": 100.0}
    def owner_factory(*, budget=None):
        owner = original_owner(budget=budget)
        owners.append(owner)
        return owner
    def clock():
        if stage == "prefork_clock":
            raise first
        if state["forked"] and stage in ("clock", "clock_no_fresh"):
            if not state["clock_failed"] or stage == "clock_no_fresh":
                obligations.append(owners[0].leader)
                state["clock_failed"] = True
                raise first
            return 101.0
        return state["now"]
    def fork():
        events.append("fork")
        state["forked"] = True
        if stage == "fork":
            raise first
        return 41
    def reap(owner):
        events.append("reap")
        if stage == "reap" or (stage == "ordinary_cleanup" and state["now"] == 101.0):
            if stage == "ordinary_cleanup":
                state["now"] = 102.0
            raise first
        return False
    def sleep(seconds):
        events.append("sleep")
        if stage == "sleep":
            raise first
        state["now"] = 101.0
    def emergency(owner, pipes, *, b1=False):
        emergencies.append((owner, dict(pipes), b1, owner.finish_end, budget.cleanup_end, budget.term_end))
        if emergency_raises:
            raise second
        return False
    forbidden = _prerequisite_forbidden
    targets = [(module, "_Owner", owner_factory), (os, "fork", fork), (os, "waitpid", forbidden),
               (os, "close", forbidden), (time, "monotonic", clock), (time, "sleep", sleep),
               (os, "_exit", forbidden), (os, "pidfd_open", forbidden),
               (module.signal, "pidfd_send_signal", forbidden), (module, "_RawPopen", forbidden),
               (original_owner, "reap", reap), (original_owner, "signal_children", forbidden),
               (module, "_emergency_cleanup", emergency)]
    _, escaped = _prerequisite_call(monkeypatch, targets,
        lambda patch: module._probe(896.0, [False], budget=budget, controller_fds=(71,)))
    assert escaped is first
    if stage == "prefork_clock":
        assert len(owners) <= 1
        assert events == [] and emergencies == []
        assert (budget.cleanup_end, budget.term_end) == (None, None)
    else:
        assert len(owners) == 1 and owners[0].returncode is None
        assert events.count("fork") == 1 and len(emergencies) == 1
        expected = (104.0, 102.0) if stage in ("clock", "ordinary_cleanup") else (103.0, 101.0)
        assert emergencies[0] == (owners[0], {}, True, expected[0], expected[0], expected[1])
        assert owners[0].leader == (None if stage == "fork" else 41)
        if stage in ("clock", "clock_no_fresh"):
            assert obligations and all(pid == 41 for pid in obligations)


@pytest.mark.parametrize("b1", [False, True])
@pytest.mark.parametrize("failed_fd", [73, 72])
@pytest.mark.parametrize("earlier", [None, "timeout"])
def test_b1_prerequisite_descriptor_batch_exhausts_before_first_exception(monkeypatch, b1, failed_fd, earlier):
    module = _load_source("_b1_descriptor_batch", "pytest_evidence_controller.py")
    import os

    owner = module._Owner(budget=module._B1Budget(0.0))
    if earlier is not None:
        owner.reject(earlier)
    descriptors, closed = [71, 72, 73], []
    first, second = SystemExit(0), RuntimeError("later close")
    def close(fd):
        assert fd not in descriptors
        closed.append(fd)
        if fd == failed_fd:
            raise first
        if fd == 71:
            raise second
    kwargs = {"b1": True} if b1 else {}
    _, escaped = _prerequisite_call(monkeypatch, [(os, "close", close)],
        lambda patch: module._close_descriptors(descriptors, owner, **kwargs))
    assert escaped is first and owner.error == (earlier or ("io_error" if b1 else None))
    if b1:
        assert closed == [73, 72, 71] and descriptors == []
    else:
        assert closed == ([73] if failed_fd == 73 else [73, 72])
        assert descriptors == ([71, 72] if failed_fd == 73 else [71])


@pytest.mark.parametrize("b1", [False, True])
@pytest.mark.parametrize("failing", [False, True])
def test_b1_prerequisite_descriptor_status_is_per_call_not_owner_reason(monkeypatch, b1, failing):
    module = _load_source("_b1_descriptor_status", "pytest_evidence_controller.py")
    import os

    owner = module._Owner(budget=module._B1Budget(0.0))
    owner.reject("timeout")
    descriptors, closed = [71, 72], []
    def close(fd):
        closed.append(fd)
        if failing and fd == 72:
            raise OSError(4, "EINTR")
    kwargs = {"b1": True} if b1 else {}
    result, escaped = _prerequisite_call(monkeypatch, [(os, "close", close)],
        lambda patch: module._close_descriptors(descriptors, owner, **kwargs))
    assert escaped is None and closed == [72, 71] and descriptors == []
    assert result is ((not failing) if b1 else None)
    assert owner.error == "timeout"
    empty, escaped = _prerequisite_call(monkeypatch, [(os, "close", close)],
        lambda patch: module._close_descriptors(descriptors, owner, **kwargs))
    assert escaped is None and empty is (True if b1 else None) and closed == [72, 71]


@pytest.mark.parametrize("stage", ["clock", "clock_after_reap", "reap", "signal", "sleep", "none"])
@pytest.mark.parametrize("batch_failure", [False, True])
def test_b1_prerequisite_emergency_process_exception_wins_after_complete_fd_batch(monkeypatch, stage, batch_failure):
    module = _load_source("_b1_emergency_exceptions", "pytest_evidence_controller.py")
    import os
    import time

    budget = module._B1Budget(0.0)
    owner = module._Owner(budget=budget)
    owner._begin_cleanup(100.0)
    owner.reject("timeout")
    pipes = {"stdout": 71, "stderr": 72, "control": 73}
    events = []
    first, second = RuntimeError("process first"), SystemExit(0)
    reaped = [False]
    def clock():
        events.append(("clock",))
        if stage == "clock" or (stage == "clock_after_reap" and reaped[0]):
            raise first
        return 100.0
    def reap():
        events.append(("reap",))
        reaped[0] = True
        if stage == "reap":
            raise first
        return stage == "none"
    def signal_children(sig):
        events.append(("signal", sig))
        if stage == "signal":
            raise first
    def sleep(seconds):
        events.append(("sleep",))
        raise first
    def close(fd):
        assert pipes == {}
        events.append(("close", fd))
        if batch_failure and fd == 72:
            raise second
    forbidden = _prerequisite_forbidden
    targets = [(time, "monotonic", clock), (time, "sleep", sleep), (owner, "reap", reap),
               (owner, "signal_children", signal_children), (os, "close", close),
               (os, "waitpid", forbidden), (os, "fork", forbidden), (os, "pidfd_open", forbidden),
               (module.signal, "pidfd_send_signal", forbidden)]
    result, escaped = _prerequisite_call(monkeypatch, targets,
        lambda patch: module._emergency_cleanup(owner, pipes, b1=True))
    assert escaped is (first if stage != "none" else second if batch_failure else None)
    if stage == "none" and not batch_failure:
        assert result is True
    assert [event for event in events if event[0] == "close"] == [("close", 73), ("close", 72), ("close", 71)]
    assert events.count(("reap",)) == 1 and pipes == {}
    if stage in ("clock", "clock_after_reap", "reap"):
        assert not any(event[0] in ("signal", "sleep") for event in events)
    assert (owner.finish_end, budget.cleanup_end, budget.term_end) == (103.0, 103.0, 101.0)
    assert owner.error == "timeout" and owner.returncode is None


@pytest.mark.parametrize("b1", [False, True])
def test_b1_prerequisite_emergency_oserror_status_does_not_follow_budget_presence(monkeypatch, b1):
    module = _load_source("_b1_emergency_status", "pytest_evidence_controller.py")
    import os
    import time

    budget = module._B1Budget(0.0)
    budget.begin_cleanup(100.0)
    owner = module._Owner(budget=budget)
    owner.reject("timeout")
    pipes, closed = {"stdout": 71, "stderr": 72}, []
    def close(fd):
        closed.append(fd)
        if fd == 72:
            raise OSError(5, "close")
    forbidden = _prerequisite_forbidden
    targets = [(time, "monotonic", lambda: 100.0), (owner, "reap", lambda: True), (os, "close", close),
               (time, "sleep", forbidden), (owner, "signal_children", forbidden),
               (os, "waitpid", forbidden), (os, "fork", forbidden), (os, "pidfd_open", forbidden),
               (module.signal, "pidfd_send_signal", forbidden)]
    kwargs = {"b1": True} if b1 else {}
    result, escaped = _prerequisite_call(monkeypatch, targets,
        lambda patch: module._emergency_cleanup(owner, pipes, **kwargs))
    assert escaped is None and result is (False if b1 else None)
    assert closed == [72, 71] and pipes == {} and owner.error == "timeout"


@pytest.mark.parametrize("which", ["batch", "emergency", "bounds"])
@pytest.mark.parametrize("value", [None, 0, 1, "true", []])
def test_b1_prerequisite_exact_bool_modes_reject_before_any_effect(monkeypatch, which, value):
    module = _load_source("_b1_prerequisite_invalid_mode", "pytest_evidence_controller.py")
    import os
    import time

    owner = module._Owner(budget=module._B1Budget(0.0))
    descriptors, pipes, events = [71], {"stdout": 72}, []
    def forbidden(*args, **kwargs):
        events.append(("invalid-mode-effect", args, kwargs))
        raise AssertionError("invalid b1 이전 부작용")
    targets = [(os, "open", forbidden), (os, "close", forbidden), (time, "monotonic", forbidden),
               (owner, "reap", forbidden), (owner, "signal_children", forbidden),
               (os, "waitpid", forbidden), (os, "fork", forbidden), (os, "pidfd_open", forbidden),
               (module.signal, "pidfd_send_signal", forbidden), (time, "sleep", forbidden)]
    def invoke(patch):
        if which == "batch":
            return module._close_descriptors(descriptors, owner, b1=value)
        if which == "emergency":
            return module._emergency_cleanup(owner, pipes, b1=value)
        return module._BoundPaths([], b1=value)
    _, escaped = _prerequisite_call(monkeypatch, targets, invoke)
    assert isinstance(escaped, ValueError)
    assert events == [] and descriptors == [71] and pipes == {"stdout": 72}
    assert owner.error is None and owner.finish_end is None


@pytest.mark.parametrize("precondition", ["no_budget", "no_interval", "no_term", "owner_none", "owner_different"])
def test_b1_prerequisite_emergency_requires_caller_initialized_interval(monkeypatch, precondition):
    module = _load_source("_b1_emergency_preconditions", "pytest_evidence_controller.py")
    import os
    import time

    budget = None if precondition == "no_budget" else module._B1Budget(0.0)
    if precondition == "no_term":
        budget.cleanup_end = 103.0
    owner = module._Owner(budget=budget)
    if precondition in ("owner_none", "owner_different"):
        budget.begin_cleanup(100.0)
        if precondition == "owner_different":
            owner.finish_end = 102.0
    pipes, events = {"stdout": 71}, []
    def forbidden(*args, **kwargs):
        events.append(("uninitialized-cleanup-effect", args, kwargs))
        raise AssertionError("uninitialized cleanup 부작용")
    before = owner.finish_end
    before_budget = None if budget is None else (budget.cleanup_end, budget.term_end)
    _, escaped = _prerequisite_call(monkeypatch,
        [(time, "monotonic", forbidden), (os, "close", forbidden), (owner, "reap", forbidden),
         (owner, "signal_children", forbidden), (os, "waitpid", forbidden), (os, "fork", forbidden),
         (os, "pidfd_open", forbidden), (module.signal, "pidfd_send_signal", forbidden),
         (time, "sleep", forbidden)],
        lambda patch: module._emergency_cleanup(owner, pipes, b1=True))
    assert isinstance(escaped, ValueError)
    assert events == [] and pipes == {"stdout": 71} and owner.error is None
    assert owner.finish_end == before
    assert (None if budget is None else (budget.cleanup_end, budget.term_end)) == before_budget


def _prerequisite_fs_call(monkeypatch, operation, *, bounds=False):
    module = _load_source("_b1_prerequisite_filesystem", "pytest_evidence_controller.py")
    import os
    import time
    import tempfile
    import subprocess

    names = ("open", "close", "fstat", "stat", "listdir", "rmdir", "dup", "fdopen", "getuid",
             "read", "write", "unlink", "remove", "rename", "chmod", "mkdir", "fork", "_exit",
             "lstat", "scandir", "pidfd_open", "getenv")
    targets = [(os, name, getattr(os, name)) for name in names]
    targets += [(time, "monotonic", time.monotonic), (time, "sleep", time.sleep),
                (tempfile, "mkdtemp", tempfile.mkdtemp), (fcntl, "flock", fcntl.flock),
                (subprocess, "Popen", subprocess.Popen)]
    ledgers = []
    def invoke(patch):
        calls = _CoordinationSyscalls(patch, bound_paths=bounds)
        calls.close_attempts = []
        calls.close_violations = []
        ledgers.append(calls)
        delegated_close = calls.close
        def recorded_close(fd):
            calls.close_attempts.append(fd)
            if type(fd) is not int or fd not in calls.live:
                calls.close_violations.append(("unowned-or-consumed", fd))
                raise AssertionError("invalid prerequisite close ownership")
            return delegated_close(fd)
        patch.setattr(os, "close", recorded_close)
        if bounds:
            patch.setattr(module, "ROOT", Path("/fixture"))
            calls.metadata["/fixture/a/output"] = SimpleNamespace(st_dev=7, st_ino=900,
                st_mode=0o100600, st_uid=1001, st_nlink=1)
        return operation(module, calls)
    result, escaped = _prerequisite_call(monkeypatch, targets, invoke)
    for calls in ledgers:
        assert calls.close_violations == [], ("close ownership violations", calls.close_violations)
        assert calls.close_attempts == [event[1] for event in calls.events if event[0] == "close"]
    return result, escaped


@pytest.mark.parametrize("b1", [False, True])
@pytest.mark.parametrize("strict", [False, True])
@pytest.mark.parametrize("failed_fd", [72, 74])
@pytest.mark.parametrize("error_type", [SystemExit, KeyboardInterrupt, RuntimeError])
def test_b1_prerequisite_bounds_retained_batch_and_literal_default_escape(monkeypatch, b1, strict, failed_fd, error_type):
    data = {}
    failure = error_type(0)
    def script(module, calls):
        kwargs = {"b1": True} if b1 else {}
        bounds = module._BoundPaths([Path("/fixture/a/one"), Path("/fixture/a/two")], **kwargs)
        data.update(bounds=bounds, calls=calls, start=len(calls.events))
        calls.errors[("close", failed_fd)] = failure
        result = bounds.close(strict=strict)
        data["end"] = len(calls.events)
        data["repeat"] = bounds.close(strict=True)
        return result
    result, escaped = _prerequisite_fs_call(monkeypatch, script, bounds=True)
    bounds, calls = data["bounds"], data["calls"]
    closes = [event[1] for event in calls.events[data["start"]:] if event[0] == "close"]
    assert bounds.controller_fds() == ()
    if b1:
        assert escaped is None and result is (False if strict else None)
        assert closes == [72, 74, 70] and calls.live == {}
        assert bounds.close_failed is True and data["repeat"] is False
        assert len(calls.events) == data["end"]
    else:
        assert escaped is failure and bounds.close_failed is False
        assert closes == ([72] if failed_fd == 72 else [72, 74])
        assert sorted(calls.live) == ([70, 74] if failed_fd == 72 else [70])


@pytest.mark.parametrize("b1", [False, True])
@pytest.mark.parametrize("temporary_failure", [False, True])
def test_b1_prerequisite_bounds_constructor_preserves_received_exception_after_owned_cleanup(monkeypatch, b1, temporary_failure):
    data = {}
    operation, temporary, retained = FileNotFoundError(2, "component A"), SystemExit(0), RuntimeError("retained C")
    def script(module, calls):
        observed = []
        class Observed(module._BoundPaths):
            def close(self, *, strict=False):
                observed.append(self)
                return super().close(strict=strict)
        calls.errors[("open", "/fixture/b")] = operation
        calls.errors[("close", 72)] = retained
        if temporary_failure:
            calls.errors[("close", 73)] = temporary
        data.update(calls=calls, observed=observed)
        kwargs = {"b1": True} if b1 else {}
        return Observed([Path("/fixture/a/one"), Path("/fixture/b/two")], **kwargs)
    _, escaped = _prerequisite_fs_call(monkeypatch, script, bounds=True)
    calls, bounds = data["calls"], data["observed"][0]
    assert escaped is ((temporary if temporary_failure else operation) if b1 else retained)
    assert bounds.controller_fds() == ()
    assert [event[1] for event in calls.events if event[0] == "close"] == ([71, 73, 72, 70] if b1 else [71, 73, 72])
    assert bounds.close_failed is b1
    assert sorted(calls.live) == ([] if b1 else [70])


@pytest.mark.parametrize("b1", [False, True])
@pytest.mark.parametrize("method", ["walk", "open", "create"])
def test_b1_prerequisite_bounds_temporary_close_keeps_exception_replacement_priority(monkeypatch, b1, method):
    data = {}
    operation, closing = OSError(5, "operation A"), KeyboardInterrupt("close B")
    def script(module, calls):
        kwargs = {"b1": True} if b1 else {}
        bounds = module._BoundPaths([Path("/fixture/a/output")], **kwargs)
        data.update(calls=calls, bounds=bounds)
        if method == "walk":
            calls.errors[("open", "/fixture/a")] = operation
            calls.errors[("close", 73)] = closing
            invoke = lambda: bounds._walk(Path("/fixture/a"))
        elif method == "open":
            calls.errors[("fstat", "/fixture/a")] = operation
            calls.errors[("close", 74)] = closing
            invoke = lambda: bounds.open(Path("/fixture/a/output"), 0)
        else:
            calls.errors[("fdopen", 75)] = operation
            calls.errors[("close", 75)] = closing
            invoke = lambda: bounds.create(Path("/fixture/a/output"))
        try:
            invoke()
        except BaseException as error:
            data["error"] = error
        data["sticky"] = bounds.close_failed
        data["snapshot"] = bounds.controller_fds()
        data["strict"] = bounds.close(strict=True)
    _, escaped = _prerequisite_fs_call(monkeypatch, script, bounds=True)
    assert escaped is None and data["error"] is closing
    assert data["sticky"] is b1 and data["strict"] is (not b1)
    assert data["snapshot"] == (70, 72) and data["calls"].live == {}
    closes = [event[1] for event in data["calls"].events if event[0] == "close"]
    assert len(closes) == len(set(closes))


def test_b1_prerequisite_bounds_root_open_escape_has_no_invented_close(monkeypatch):
    failure = SystemExit(0)
    data = {}
    def script(module, calls):
        data["calls"] = calls
        calls.errors[("open", "/fixture")] = failure
        return module._BoundPaths([], b1=True)
    _, escaped = _prerequisite_fs_call(monkeypatch, script, bounds=True)
    assert escaped is failure
    assert data["calls"].live == {}
    assert not any(event[0] == "close" for event in data["calls"].events)


@pytest.mark.parametrize("where", ["first_directory", "middle_directory", "unlock", "lock_close"])
@pytest.mark.parametrize("error_type", [SystemExit, KeyboardInterrupt, RuntimeError])
@pytest.mark.parametrize("prelatched", [False, True])
def test_b1_prerequisite_coordination_baseexception_batch_is_sticky_and_lock_last(monkeypatch, where, error_type, prelatched):
    failure = error_type(0)
    data = {}
    def script(module, calls):
        coordination = module._B1Coordination()
        coordination.acquire_lock([False], module._B1Budget(0.0))
        coordination.prepare_fake_key()
        if prelatched:
            coordination.acquire_lock([False], module._B1Budget(0.0))
        owned = tuple(sorted(calls.live))
        snapshot = coordination.controller_fds()
        lock = next(fd for fd in owned if calls.live[fd][0] == _LOCK_LITERAL)
        directories = sorted(set(owned) - {lock})
        target = lock if where in ("unlock", "lock_close") else directories[0 if where == "first_directory" else 1]
        calls.errors[("flock" if where == "unlock" else "close", target)] = failure
        detach_snapshots = []
        def detached(event):
            if event[0] in ("flock", "close"):
                detach_snapshots.append((event[:2], coordination.controller_fds()))
        calls.on_event = detached
        data.update(calls=calls, coordination=coordination, owned=owned, snapshot=snapshot,
                    detach_snapshots=detach_snapshots, lock=lock, start=len(calls.events),
                    attempt_start=len(calls.close_attempts))
        data["result"] = coordination.close()
        data["end"] = len(calls.events)
        data["repeat"] = coordination.close()
    _, escaped = _prerequisite_fs_call(monkeypatch, script)
    assert escaped is None and data["result"] is False and data["repeat"] is False
    coordination, calls = data["coordination"], data["calls"]
    events = calls.events[data["start"]:]
    assert data["snapshot"] == data["owned"]
    assert data["detach_snapshots"] == [(event[:2], ()) for event in events if event[0] in ("flock", "close")]
    assert sorted(calls.close_attempts[data["attempt_start"]:]) == list(data["owned"])
    assert calls.close_attempts[-1] == data["lock"]
    assert sorted(event[1] for event in events if event[0] == "close") == list(data["owned"])
    assert events[-2:] == [("flock", data["lock"], 8), ("close", data["lock"], _LOCK_LITERAL)]
    assert coordination.close_failed is True and coordination.error == ("startup_error" if prelatched else "io_error")
    assert len(calls.events) == data["end"] and calls.live == {}
    assert not any(event[0] in ("stat", "rmdir", "mkdtemp", "open") for event in events)


@pytest.mark.parametrize("phase", ["acquire", "check"])
def test_b1_prerequisite_coordination_temporary_baseexception_records_and_reraises(monkeypatch, phase):
    failure = SystemExit(0)
    data = {}
    def script(module, calls):
        coordination = module._B1Coordination()
        budget = module._B1Budget(0.0)
        if phase == "check":
            coordination.acquire_lock([False], budget)
        failed = []
        def fail_once(event):
            if event[0] == "close" and not failed:
                failed.append(event[1])
                raise failure
        calls.on_event = fail_once
        try:
            coordination.acquire_lock([False], budget) if phase == "acquire" else coordination.check_lock_identity()
        except BaseException as error:
            data["error"] = error
        calls.on_event = lambda event: None
        data.update(coordination=coordination, calls=calls, failed=failed,
                    snapshot=coordination.controller_fds(), result=coordination.close())
    _, escaped = _prerequisite_fs_call(monkeypatch, script)
    assert escaped is None and data["error"] is failure
    assert data["coordination"].close_failed is True and data["coordination"].error == "io_error"
    assert data["result"] is False and data["calls"].live == {}
    assert len(data["failed"]) == 1 and data["failed"][0] not in data["snapshot"]
    assert len([event for event in data["calls"].events if event[:2] == ("close", data["failed"][0])]) == 1


@pytest.mark.parametrize("stage", ["reap", "sleep", "close"])
def test_b1_prerequisite_default_emergency_keeps_immediate_exception_and_pipe_ownership(monkeypatch, stage):
    module = _load_source("_b1_default_emergency_literal", "pytest_evidence_controller.py")
    import os
    import time

    budget = module._B1Budget(0.0)
    budget.begin_cleanup(100.0)
    owner = module._Owner(budget=budget)
    pipes, events = {"stdout": 71, "stderr": 72}, []
    failure = RuntimeError("legacy immediate")
    def reap():
        events.append("reap")
        if stage == "reap":
            raise failure
        return stage == "close"
    def sleep(seconds):
        events.append("sleep")
        raise failure
    def close(fd):
        events.append(("close", fd))
        raise failure
    forbidden = _prerequisite_forbidden
    targets = [(time, "monotonic", lambda: 100.0), (time, "sleep", sleep),
               (owner, "reap", reap), (owner, "signal_children", lambda sig: events.append(("signal", sig))),
               (os, "close", close), (os, "waitpid", forbidden), (os, "fork", forbidden),
               (os, "pidfd_open", forbidden), (module.signal, "pidfd_send_signal", forbidden)]
    _, escaped = _prerequisite_call(monkeypatch, targets, lambda patch: module._emergency_cleanup(owner, pipes))
    assert escaped is failure
    assert pipes == ({} if stage == "close" else {"stdout": 71, "stderr": 72})
    assert [event for event in events if type(event) is tuple and event[0] == "close"] == ([("close", 72)] if stage == "close" else [])
    assert (owner.finish_end, budget.cleanup_end, budget.term_end) == (103.0, 103.0, 101.0)


def test_b1_prerequisite_default_probe_parent_exception_does_not_gain_emergency(monkeypatch):
    module = _load_source("_b1_default_probe_parent", "pytest_evidence_controller.py")
    import os
    import time

    failure = RuntimeError("legacy reap")
    events = []
    def reap(owner):
        events.append("reap")
        raise failure
    forbidden = _prerequisite_forbidden
    targets = [(os, "fork", lambda: 41), (time, "monotonic", lambda: 100.0),
               (module._Owner, "reap", reap), (module, "_emergency_cleanup", forbidden),
               (os, "waitpid", forbidden), (os, "pidfd_open", forbidden),
               (module.signal, "pidfd_send_signal", forbidden), (time, "sleep", forbidden)]
    _, escaped = _prerequisite_call(monkeypatch, targets, lambda patch: module._probe(896.0, [False]))
    assert escaped is failure and events == ["reap"]


def test_b1_prerequisite_emergency_at_fixed_end_only_closes_owned_pipes(monkeypatch):
    module = _load_source("_b1_emergency_expired_interval", "pytest_evidence_controller.py")
    import os
    import time

    budget = module._B1Budget(0.0)
    owner = module._Owner(budget=budget)
    owner._begin_cleanup(100.0)
    pipes, closed = {"stdout": 71, "stderr": 72}, []
    forbidden = _prerequisite_forbidden
    targets = [(time, "monotonic", lambda: 103.0), (time, "sleep", forbidden),
               (owner, "reap", forbidden), (owner, "signal_children", forbidden),
               (os, "waitpid", forbidden), (os, "pidfd_open", forbidden),
               (module.signal, "pidfd_send_signal", forbidden), (os, "close", closed.append)]
    result, escaped = _prerequisite_call(monkeypatch, targets,
        lambda patch: module._emergency_cleanup(owner, pipes, b1=True))
    assert escaped is None and result is True
    assert closed == [72, 71] and pipes == {}
    assert owner.error == "cleanup_error" and owner.returncode is None
    assert (owner.finish_end, budget.cleanup_end, budget.term_end) == (103.0, 103.0, 101.0)


def test_b1_prerequisite_coordination_unheld_leaf_never_unlocks_after_baseexception(monkeypatch):
    data = {}
    failure = SystemExit(0)
    def script(module, calls):
        coordination = module._B1Coordination()
        calls.flocks = [OSError(5, "acquisition failed")]
        coordination.acquire_lock([False], module._B1Budget(0.0))
        owned = tuple(sorted(calls.live))
        snapshot = coordination.controller_fds()
        lock = next(fd for fd in owned if calls.live[fd][0] == _LOCK_LITERAL)
        calls.errors[("close", lock)] = failure
        data.update(calls=calls, coordination=coordination, owned=owned, snapshot=snapshot,
                    start=len(calls.events))
        return coordination.close()
    result, escaped = _prerequisite_fs_call(monkeypatch, script)
    assert escaped is None and result is False
    events = data["calls"].events[data["start"]:]
    assert data["snapshot"] == data["owned"]
    assert not any(event[0] == "flock" for event in events)
    assert sorted(event[1] for event in events if event[0] == "close") == list(data["owned"])
    assert data["calls"].live == {} and data["coordination"].close_failed is True
    assert data["coordination"].error == "startup_error"


def test_b1_prerequisite_default_emergency_syncs_later_initialized_shared_budget(monkeypatch):
    module = _load_source("_b1_default_late_budget", "pytest_evidence_controller.py")
    import os
    import time

    budget = module._B1Budget(0.0)
    owner = module._Owner(budget=budget)
    budget.begin_cleanup(100.0)
    assert owner.finish_end is None and (budget.cleanup_end, budget.term_end) == (103.0, 101.0)
    pipes, events = {"stdout": 71}, []
    forbidden = _prerequisite_forbidden
    targets = [(time, "monotonic", lambda: 102.0), (owner, "reap", lambda: True),
               (os, "close", lambda fd: events.append(("close", fd))), (time, "sleep", forbidden),
               (owner, "signal_children", forbidden), (os, "waitpid", forbidden), (os, "fork", forbidden),
               (os, "pidfd_open", forbidden), (module.signal, "pidfd_send_signal", forbidden)]
    result, escaped = _prerequisite_call(monkeypatch, targets,
        lambda patch: module._emergency_cleanup(owner, pipes))
    assert escaped is None and result is None and pipes == {} and events == [("close", 71)]
    assert (owner.finish_end, budget.cleanup_end, budget.term_end) == (103.0, 103.0, 101.0)


def test_b1_prerequisite_emergency_failed_first_clock_keeps_priority_over_fallback_reap(monkeypatch):
    module = _load_source("_b1_emergency_clock_and_reap", "pytest_evidence_controller.py")
    import os
    import time

    owner = module._Owner(budget=module._B1Budget(0.0))
    owner._begin_cleanup(100.0)
    first, second = RuntimeError("first clock"), SystemExit(0)
    pipes, events = {"stdout": 71, "stderr": 72}, []
    def clock():
        events.append("clock")
        raise first
    def reap():
        events.append("reap")
        raise second
    forbidden = _prerequisite_forbidden
    targets = [(time, "monotonic", clock), (owner, "reap", reap),
               (os, "close", lambda fd: events.append(("close", fd))), (time, "sleep", forbidden),
               (owner, "signal_children", forbidden), (os, "waitpid", forbidden), (os, "fork", forbidden),
               (os, "pidfd_open", forbidden), (module.signal, "pidfd_send_signal", forbidden)]
    _, escaped = _prerequisite_call(monkeypatch, targets,
        lambda patch: module._emergency_cleanup(owner, pipes, b1=True))
    assert escaped is first and events == ["clock", "reap", ("close", 72), ("close", 71)]
    assert pipes == {} and owner.finish_end == 103.0 and owner._budget.term_end == 101.0


@pytest.mark.parametrize("invalid", ["duplicate", "unknown", "bool", "noninteger"])
def test_b1_prerequisite_oracle_detects_swallowed_close_ownership_violation(monkeypatch, invalid):
    import os

    original_close = os.close
    caught, observations = [], {}
    injected = SystemExit(0)
    def operation(module, calls):
        fd = calls.allocate("/fixture", calls.metadata["/fixture"])
        calls.errors[("close", fd)] = injected
        bad_fd = {"duplicate": fd, "unknown": 999, "bool": True, "noninteger": "70"}[invalid]
        for attempted in (fd, bad_fd):
            try:
                os.close(attempted)
            except BaseException as error:
                caught.append(error)
        observations.update(attempts=list(calls.close_attempts), violations=list(calls.close_violations))
    with pytest.raises(AssertionError, match="close ownership violations"):
        _prerequisite_fs_call(monkeypatch, operation, bounds=True)
    assert os.close is original_close
    assert caught[0] is injected and isinstance(caught[1], AssertionError)
    expected_bad = {"duplicate": 70, "unknown": 999, "bool": True, "noninteger": "70"}[invalid]
    assert observations == {"attempts": [70, expected_bad],
                            "violations": [("unowned-or-consumed", expected_bad)]}


def test_b1_prerequisite_oracle_detects_swallowed_forbidden_attempt(monkeypatch):
    original = lambda: None
    boundary = SimpleNamespace(waitpid=original)
    caught = []
    def operation(patch):
        try:
            boundary.waitpid(41, 1)
        except BaseException as error:
            caught.append(error)
        return False
    with pytest.raises(AssertionError, match="forbidden attempts survived candidate catch") as failure:
        _prerequisite_call(monkeypatch, [(boundary, "waitpid", _prerequisite_forbidden)], operation)
    assert boundary.waitpid is original
    assert len(caught) == 1 and isinstance(caught[0], AssertionError)
    assert failure.value.args[0] == ("forbidden attempts survived candidate catch", [("waitpid", (41, 1), {})])


class _ValidatedBodyCase:
    """독립 입력과 in-memory 사건 원장; 실제 FD/프로세스를 만들지 않는다."""

    def __init__(self):
        import hashlib

        self.events, self.violations, self.writes, self.spawn = [], [], [], []
        self.held = False
        self.live = set()
        self.pipe_pairs = iter(((80, 81), (82, 83), (84, 85)))
        self.context = {"run": {"event": "local", "sha": "a" * 40, "tree": "b" * 40,
            "contract": "c" * 64, "run_id": "b1-main-literal", "attempt": 1},
            "slot": {"lane": "standard", "timezone": "UTC"}}
        self.guard = {"path": "tests/conftest.py",
            "sha256": "7b7b26940309a2a165d9bdba7611b6730e83b90ae9ea5f9e725764ee4c0a23f7",
            "module_count": 1, "violations": 0}
        self.hashes = {"/fixture/scripts/dev/pytest_evidence_controller.py": "1" * 64,
            "/fixture/scripts/dev/pytest_evidence_bootstrap.py": "2" * 64,
            "/fixture/scripts/dev/pytest_evidence.py": "3" * 64,
            "/fixture/tests/conftest.py": self.guard["sha256"], "/fixture/python": "5" * 64}
        self.fake_path = "/tmp/qwq-b1-independent/deploy-key"
        self.facts = {"lock_acquired": False, "lock_identity_stable": False,
            "fake_key_absent_before": False, "fake_key_absent_after": False,
            "fake_directory_removed": False, "fake_key_path_sha256": None}
        self.key_hash = hashlib.sha256(b"/tmp/qwq-b1-independent/deploy-key").hexdigest()
        self.receipt = {"state": "regular", "bytes": 2,
            "sha256": "44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a"}
        self.streams = {"stdout": {"bytes": 3, "sha256": hashlib.sha256(b"abc").hexdigest(),
            "overflow": False, "observed_bytes": 3},
            "stderr": {"bytes": 2, "sha256": hashlib.sha256(b"de").hexdigest(),
            "overflow": False, "observed_bytes": 2}}
        self.logs = {}

    def event(self, name, *args):
        self.events.append((name, self.held, *args))

    def clock(self):
        self.event("clock")
        return 105.0

    def pipe(self):
        pair = next(self.pipe_pairs)
        self.event("pipe", pair)
        self.live.update(pair)
        return pair

    def close(self, fd):
        self.event("fd-close", fd)
        if fd not in self.live:
            self.violations.append(("unowned-close", fd))
            raise AssertionError("fake FD ownership")
        self.live.remove(fd)

    def bootstrap(self):
        self.event("bootstrap")
        def guard(root, *, install=False):
            self.event("guard", root, install)
            return dict(self.guard)
        def read_context(path):
            self.event("context", path)
            return json.loads(json.dumps(self.context)), json.dumps(self.context).encode()
        def load(name, path):
            self.event("producer", name, path)
            return SimpleNamespace(_read_context=read_context)
        return SimpleNamespace(_guard=guard, _load=load)

    def hash(self, path):
        self.event("hash", str(path))
        return self.hashes[str(path)]

    def bounds(self, paths, *, b1=False):
        self.event("bounds", tuple(paths), b1)
        case = self
        class Bounds:
            close_failed = False
            def controller_fds(self):
                return (70, 72, 74)
            def create(self, path, *, buffering=-1):
                case.event("create", path, buffering)
                name = "result" if path == Path("/fixture/result.json") else (
                    "stdout" if path == Path("/fixture/stdout.log") else "stderr")
                stream = case.stream(name)
                if name != "result":
                    case.logs[name] = stream
                return stream
            def close(self, *, strict=False):
                case.event("bounds-close", strict)
                return True if strict else None
        return Bounds()

    def stream(self, name):
        case = self
        class Stream:
            closed = False
            def write(self, raw):
                case.event("stream-write", name, bytes(raw))
                if name == "result":
                    case.writes.append(bytes(raw))
                return len(raw)
            def close(self):
                case.event("stream-close", name)
                if self.closed:
                    case.violations.append(("duplicate-stream-close", name))
                self.closed = True
            def __enter__(self):
                return self
            def __exit__(self, *args):
                self.close()
        return Stream()

    def coordination(self):
        self.event("coordination")
        case = self
        class Coordination:
            error = None
            close_failed = False
            fake_key_path = None
            def acquire_lock(self, stopped, budget):
                case.event("acquire", tuple(stopped), budget.total_end, budget.run_end)
                case.held = True
                case.facts["lock_acquired"] = True
                case.facts["lock_identity_stable"] = True
                return True
            def prepare_fake_key(self):
                case.event("prepare")
                self.fake_key_path = case.fake_path
                case.facts["fake_key_absent_before"] = True
                case.facts["fake_key_path_sha256"] = case.key_hash
                return case.fake_path
            def controller_fds(self):
                return (71, 73)
            def finish_fake_key(self):
                case.event("finish")
                case.facts["fake_key_absent_after"] = True
                case.facts["fake_directory_removed"] = True
                return True
            def check_lock_identity(self):
                case.event("lock-check")
                return True
            def facts(self):
                case.event("facts")
                return dict(case.facts)
            def close(self):
                case.event("coord-close")
                case.held = False
                return True
        return Coordination()

    def probe(self, deadline, stopped, *, budget=None, controller_fds=()):
        self.event("probe", deadline, tuple(stopped), budget.total_end, budget.run_end, controller_fds)
        return True

    def popen(self, command, **kwargs):
        self.event("spawn")
        self.spawn.append((list(command), kwargs))
        return SimpleNamespace(pid=41)

    def observe(self, owner, pipes, logs, parent_guard, deadline, stopped, *, profile=None):
        import hashlib

        self.event("observe", owner.leader, deadline, owner.startup_end, profile, tuple(pipes.items()),
                   owner._budget.total_end, owner._budget.run_end, dict(parent_guard))
        owner.returncode, owner.reaped = 0, True
        owner._begin_cleanup(105.0)
        owner.observation = {"profile": "b1-standard/v1", "streams": {
            "stdout": {"bytes": 3, "sha256": hashlib.sha256(b"abc"), "overflow": False},
            "stderr": {"bytes": 2, "sha256": hashlib.sha256(b"de"), "overflow": False}},
            "observed": {"stdout": 3, "stderr": 2}, "failed_logs": set(),
            "control": bytearray(), "guard": dict(self.guard), "startup_end": 115.0}
        logs["stdout"].write(b"abc")
        logs["stderr"].write(b"de")
        for fd in list(pipes.values()):
            self.close(fd)
        pipes.clear()
        return True, dict(self.guard), {name: dict(item) for name, item in self.streams.items()}


def test_b1_validated_body_holds_lock_through_result_close(monkeypatch):
    module = _load_source("_b1_validated_body_first", "pytest_evidence_controller.py")
    import builtins
    import hashlib
    import os
    import signal
    import subprocess
    import time

    case = _ValidatedBodyCase()
    context, receipt, output = Path("/fixture/context.json"), Path("/fixture/receipt.json"), Path("/fixture/result.json")
    logs, selected = [Path("/fixture/stdout.log"), Path("/fixture/stderr.log")], ["tests/test_tiny.py"]
    options = SimpleNamespace(profile="b1-standard/v1", timeout_seconds=900)
    def receipt_fact(path, bounds):
        case.event("receipt", path)
        return dict(case.receipt)
    targets = [(module, "ROOT", Path("/fixture")),
        (module, "__file__", "/fixture/scripts/dev/pytest_evidence_controller.py"),
        (sys, "executable", "/fixture/python"), (module, "_load_bootstrap", case.bootstrap),
        (module, "_hash", case.hash), (module, "_BoundPaths", case.bounds),
        (module, "_B1Coordination", case.coordination), (module, "_probe", case.probe),
        (module, "_preflight", lambda: case.event("preflight")), (module, "_RawPopen", case.popen),
        (module, "_observe", case.observe), (module, "_receipt", receipt_fact),
        (time, "monotonic", case.clock), (os, "pipe", case.pipe), (os, "close", case.close),
        (os, "set_blocking", lambda fd, flag: case.event("nonblocking", fd, flag)),
        (signal, "signal", lambda sig, handler: case.event("handler", sig)),
        (Path, "resolve", lambda path, **kwargs: path), (os, "environ", _parent_env())]
    targets += [(owner, name, _prerequisite_forbidden) for owner, name in (
        (builtins, "open"), (Path, "open"), (Path, "read_bytes"), (os, "open"), (os, "fork"),
        (os, "waitpid"), (os, "pidfd_open"), (os, "read"), (os, "write"), (os, "unlink"),
        (os, "rmdir"), (signal, "pidfd_send_signal"), (time, "sleep"), (fcntl, "flock"),
        (subprocess, "Popen"), (module, "_emergency_cleanup"))]
    result, escaped = _prerequisite_call(monkeypatch, targets,
        lambda patch: module._run_validated(100.0, options, context, receipt, output, logs, selected))
    if escaped is not None:
        raise escaped
    assert result == 0 and type(result) is int
    assert case.violations == [] and case.live == set() and not case.held
    assert case.spawn == [(["/fixture/python", "-I", "-B",
        "/fixture/scripts/dev/pytest_evidence_bootstrap.py", "--b1-standard-v1", "85",
        "/fixture/context.json", "/fixture/receipt.json", "tests/test_tiny.py"], {
        "cwd": Path("/fixture"), "env": {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "TZ": "UTC",
        "PYTHONDONTWRITEBYTECODE": "1", "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        "QWQ_DEPLOY_SSH_KEY": "/tmp/qwq-b1-independent/deploy-key"}, "close_fds": True,
        "start_new_session": True, "pass_fds": (85,), "stdin": -3, "stdout": 81, "stderr": 83})]
    expected = {"schema": "qwq.verification-process-result/v2", **case.context,
        "scope": "local_b1_process_only", "identity": {"controller": "1" * 64, "bootstrap": "2" * 64,
        "producer": "3" * 64, "guard": "7b7b26940309a2a165d9bdba7611b6730e83b90ae9ea5f9e725764ee4c0a23f7",
        "executable": "5" * 64}, "launch": {"profile": "b1-standard/v1", "process_scope": "linux-subreaper/v1",
        "timeout_seconds": 900, "budget_profile": "inclusive-lock-cleanup-publication/v1",
        "retained_stream_bytes": 2097152, "overflow_observed_bytes": 2097153,
        "pytest_profile": "b1-x-fixed-plugins/v1", "environment_profile": "b1-env-i-fake-key/v1",
        "selection_profile": "tests-path-only/v1", "lock_profile": "owner-ticket-workload/v1",
        "selection_sha256": hashlib.sha256(b'["tests/test_tiny.py"]').hexdigest()},
        "process": {"reason": "exited", "returncode": 0, "term_sent": False, "kill_sent": False,
        "leader_reaped": True, "descendant_survived": False, "cleanup_complete": True,
        "descendants_reaped": 0, "ownership_probe_passed": True, "guard": case.guard, "parent_guard": case.guard},
        "streams": case.streams, "receipt": case.receipt,
        "coordination": {"lock_acquired": True, "lock_identity_stable": True, "fake_key_absent_before": True,
        "fake_key_absent_after": True, "fake_directory_removed": True,
        "fake_key_path_sha256": hashlib.sha256(b"/tmp/qwq-b1-independent/deploy-key").hexdigest()}}
    assert case.writes == [json.dumps(expected, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()]
    assert [event for event in case.events if event[0] == "probe"] == [
        ("probe", True, 106.0, (False,), 1000.0, 996.0, (70, 71, 72, 73, 74))]
    assert [event for event in case.events if event[0] == "observe"] == [
        ("observe", True, 41, 996.0, 115.0, "b1-standard/v1",
         (("stdout", 80), ("stderr", 82), ("control", 84)), 1000.0, 996.0, case.guard)]
    named = [event[0] for event in case.events]
    result_close = case.events.index(("stream-close", True, "result"))
    bounds_close = case.events.index(("bounds-close", True, True))
    coord_close = case.events.index(("coord-close", True))
    assert result_close < bounds_close < coord_close < len(case.events) - 1
    assert case.events[coord_close + 1:] == [("clock", False)]
    assert named.count("acquire") == named.count("prepare") == named.count("finish") == named.count("lock-check") == 1
    assert named.index("finish") < named.index("receipt") < named.index("lock-check") < result_close
    assert all(event[1] for event in case.events[named.index("prepare"):coord_close + 1])
    assert named.index("handler") < named.index("bootstrap") < named.index("acquire")
    assert [event for event in case.events if event[0] == "handler"] == [
        ("handler", False, 15), ("handler", False, 2)]
    assert [event for event in case.events if event[0] == "bounds"] == [
        ("bounds", False, (receipt, output, *logs), True)]
    assert [event for event in case.events if event[0] == "create"] == [
        ("create", True, logs[0], 0), ("create", True, logs[1], 0), ("create", True, output, 0)]
    assert named.count("bounds-close") == named.count("coord-close") == 1
    assert case.logs["stdout"].closed and case.logs["stderr"].closed
    assert case.events.index(("stream-close", True, "stdout")) < named.index("finish")
    assert case.events.index(("stream-close", True, "stderr")) < named.index("finish")
    assert [event for event in case.events if event[0] == "fd-close"] == [
        ("fd-close", True, 85), ("fd-close", True, 83), ("fd-close", True, 81),
        ("fd-close", True, 80), ("fd-close", True, 82), ("fd-close", True, 84)]
