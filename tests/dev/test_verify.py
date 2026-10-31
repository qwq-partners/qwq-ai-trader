import os
import shlex
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/dev/verify.sh"


def init_repo(path: Path):
    subprocess.run(["git", "init", "-q", str(path)], check=True)


def track_all(path: Path):
    subprocess.run(["git", "-C", str(path), "add", "."], check=True)


def run_verify(repo: Path, *, skip_tests: bool = True):
    env = os.environ | {
        "QWQ_VERIFY_ROOT": str(repo),
        "QWQ_VERIFY_PYTHON": sys.executable,
        "QWQ_VERIFY_SKIP_TESTS": "1" if skip_tests else "0",
    }
    return subprocess.run(
        ["bash", str(SCRIPT)],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def test_verify_passes_clean_source(tmp_path):
    init_repo(tmp_path)
    (tmp_path / "ok.py").write_text("VALUE = 1\n", encoding="utf-8")
    track_all(tmp_path)

    result = run_verify(tmp_path)

    assert result.returncode == 0, result.stdout + result.stderr


def test_verify_rejects_python_syntax_error(tmp_path):
    init_repo(tmp_path)
    (tmp_path / "bad.py").write_text("def broken(:\n", encoding="utf-8")
    track_all(tmp_path)

    result = run_verify(tmp_path)

    assert result.returncode != 0
    assert "문법 검사" in result.stdout


def test_verify_propagates_pytest_failure(tmp_path):
    init_repo(tmp_path)
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_failure.py").write_text(
        "def test_failure():\n    assert False\n",
        encoding="utf-8",
    )
    track_all(tmp_path)

    result = run_verify(tmp_path, skip_tests=False)

    assert result.returncode != 0
    assert "테스트" in result.stdout


def test_verify_runs_pytest_from_relative_target_root_outside_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    init_repo(repo)
    tests = repo / "tests"
    tests.mkdir()
    (tests / "test_context.py").write_text(
        "from pathlib import Path\n\n"
        "def test_pytest_uses_repository_context(pytestconfig):\n"
        "    root = Path(__file__).resolve().parents[1]\n"
        "    assert Path.cwd().resolve() == root\n"
        "    assert Path(str(pytestconfig.rootpath)).resolve() == root\n",
        encoding="utf-8",
    )
    track_all(repo)
    caller = tmp_path / "caller"
    caller.mkdir()
    python = caller / "python"
    python.write_text(
        f"#!/usr/bin/env bash\nexec {shlex.quote(sys.executable)} \"$@\"\n",
        encoding="utf-8",
    )
    python.chmod(0o755)
    env = os.environ | {
        "QWQ_VERIFY_ROOT": "../repo",
        "QWQ_VERIFY_PYTHON": "python",
        "QWQ_VERIFY_SKIP_TESTS": "0",
    }

    result = subprocess.run(
        ["bash", str(SCRIPT)],
        cwd=caller,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr


def test_verify_rejects_private_key(tmp_path):
    init_repo(tmp_path)
    (tmp_path / "leak.txt").write_text(
        "-----BEGIN " + "PRIVATE KEY-----\nsecret\n",
        encoding="utf-8",
    )
    track_all(tmp_path)

    result = run_verify(tmp_path)

    assert result.returncode != 0
    assert "비밀정보" in result.stdout


def test_verify_does_not_echo_detected_secret_value(tmp_path):
    init_repo(tmp_path)
    token = "AKIA" + "ABCDEFGHIJKLMNOP"
    (tmp_path / "leak.txt").write_text(f"credential={token}\n", encoding="utf-8")
    track_all(tmp_path)

    result = run_verify(tmp_path)

    assert result.returncode != 0
    assert "leak.txt" in result.stdout
    assert token not in result.stdout


def _repo_with_two_tests(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "tests").mkdir(parents=True)
    (repo / "tests" / "test_a.py").write_text("def test_ok():\n    assert True\n")
    (repo / "tests" / "test_b.py").write_text("def test_bad():\n    assert False\n")
    init_repo(repo)
    track_all(repo)
    return repo


def _run(repo: Path, **env_extra):
    env = {"PATH": os.environ["PATH"], "QWQ_VERIFY_ROOT": str(repo), "QWQ_VERIFY_PYTHON": sys.executable,
           "QWQ_VERIFY_SKIP_TESTS": "0", **env_extra}
    return subprocess.run(["bash", str(SCRIPT)], env=env, capture_output=True, text=True, timeout=120)


def test_verify_tests_override_runs_only_that_path(tmp_path):
    repo = _repo_with_two_tests(tmp_path)
    assert _run(repo).returncode != 0                                          # default: whole tests dir, test_b fails
    subset = _run(repo, QWQ_VERIFY_TESTS="tests/test_a.py")                    # relative to the repo root
    assert subset.returncode == 0, subset.stdout + subset.stderr
    assert "1 passed" in subset.stdout
    missing = _run(repo, QWQ_VERIFY_TESTS="tests/nope.py")
    assert missing.returncode != 0 and "테스트 경로를 찾지 못했습니다" in missing.stderr


def test_verify_parallel_requires_xdist_and_a_valid_worker_count(tmp_path, monkeypatch):
    repo = _repo_with_two_tests(tmp_path)
    try:
        import xdist  # noqa: F401
        has_xdist = True
    except ImportError:
        has_xdist = False
    bad = _run(repo, QWQ_VERIFY_PARALLEL="0", QWQ_VERIFY_TESTS="tests/test_a.py")
    assert bad.returncode != 0
    if has_xdist:
        assert "auto 또는 양의 정수" in bad.stderr
        ok = _run(repo, QWQ_VERIFY_PARALLEL="2", QWQ_VERIFY_TESTS="tests/test_a.py")
        assert ok.returncode == 0 and "병렬 2" in ok.stdout
    else:
        assert "pytest-xdist 가 필요합니다" in bad.stderr
        assert "pytest" not in bad.stdout                                       # fails before any test run
