import os
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
