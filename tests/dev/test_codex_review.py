import os
import stat
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/dev/codex_review.sh"


def make_repo(path: Path, *, branch: str = "main"):
    subprocess.run(["git", "init", "-q", "-b", branch, str(path)], check=True)
    (path / "a.txt").write_text("x\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "add", "."], check=True)
    subprocess.run(
        [
            "git", "-C", str(path),
            "-c", "user.email=test@test", "-c", "user.name=test",
            "commit", "-qm", "init",
        ],
        check=True,
    )


def make_codex_stub(path: Path) -> Path:
    """호출 인자를 기록만 하는 가짜 Codex 실행 파일."""
    stub = path / "codex_stub"
    args_file = path / "codex_args.txt"
    stub.write_text(
        "#!/usr/bin/env bash\n"
        f"printf '%s\\n' \"$@\" > {args_file}\n",
        encoding="utf-8",
    )
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
    return stub


def run_review(repo: Path, *args, codex_bin: str):
    env = os.environ | {
        "QWQ_REVIEW_ROOT": str(repo),
        "QWQ_REVIEW_CODEX_BIN": codex_bin,
    }
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def test_refuses_when_codex_missing(tmp_path):
    make_repo(tmp_path)

    result = run_review(tmp_path, codex_bin=str(tmp_path / "no_such_codex"))

    assert result.returncode != 0
    assert "Codex" in result.stderr


def test_refuses_branch_review_on_base_branch(tmp_path):
    make_repo(tmp_path)
    stub = make_codex_stub(tmp_path)

    result = run_review(tmp_path, codex_bin=str(stub))

    assert result.returncode != 0
    assert "기준 브랜치" in result.stderr
    assert not (tmp_path / "codex_args.txt").exists()


def test_branch_mode_runs_read_only_review_against_base(tmp_path):
    make_repo(tmp_path)
    subprocess.run(
        ["git", "-C", str(tmp_path), "switch", "-q", "-c", "feature/x"],
        check=True,
    )
    stub = make_codex_stub(tmp_path)

    result = run_review(tmp_path, codex_bin=str(stub))

    assert result.returncode == 0, result.stdout + result.stderr
    args = (tmp_path / "codex_args.txt").read_text(encoding="utf-8").splitlines()
    assert args[:3] == ["exec", "--sandbox", "read-only"]
    assert "review" in args
    base_idx = args.index("--base")
    assert args[base_idx + 1] == "main"


def test_uncommitted_mode_allows_base_branch(tmp_path):
    make_repo(tmp_path)
    stub = make_codex_stub(tmp_path)

    result = run_review(tmp_path, "--uncommitted", codex_bin=str(stub))

    assert result.returncode == 0, result.stdout + result.stderr
    args = (tmp_path / "codex_args.txt").read_text(encoding="utf-8").splitlines()
    assert "--uncommitted" in args
    assert "--base" not in args


def test_rejects_unknown_option(tmp_path):
    make_repo(tmp_path)
    stub = make_codex_stub(tmp_path)

    result = run_review(tmp_path, "--bogus", codex_bin=str(stub))

    assert result.returncode == 2
    assert "사용법" in result.stdout + result.stderr
