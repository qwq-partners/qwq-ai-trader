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

    for name in ("open", "close", "dup", "fstat", "stat", "listdir", "rmdir",
                 "mkdir", "unlink", "rename", "read", "write", "chmod", "getuid", "fork"):
        monkeypatch.setattr(syscall, name, forbidden)
    monkeypatch.setattr(temporary, "mkdtemp", forbidden)
    monkeypatch.setattr(clock, "monotonic", forbidden)
    monkeypatch.setattr(clock, "sleep", forbidden)
    monkeypatch.setattr(locking, "flock", forbidden)

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
