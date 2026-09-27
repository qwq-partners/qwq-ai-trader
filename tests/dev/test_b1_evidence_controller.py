"""B1 실행 경계의 독립 리터럴 시험. 이 파일은 실제 자식을 만들지 않는다."""

import importlib.util
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
