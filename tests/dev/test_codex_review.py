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


def run_review(repo: Path, *args, codex_bin: str, extra_env: dict | None = None):
    env = os.environ | {
        "QWQ_REVIEW_ROOT": str(repo),
        "QWQ_REVIEW_CODEX_BIN": codex_bin,
    }
    env.pop("QWQ_REVIEW_SANDBOX", None)   # 호출 셸의 값이 새지 않게 — 테스트가 명시한 것만
    if extra_env:
        env |= extra_env
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
    # 기본은 --sandbox 를 넘기지 않는다 — ~/.codex/config.toml 의 sandbox_mode 를 따른다
    # (이 호스트는 bwrap userns 제한으로 read-only 샌드박스가 뜨지 않아 리뷰가 미검증으로 끝났다)
    assert args[:1] == ["exec"] and "--sandbox" not in args
    prompt = "\n".join(args[1:])
    assert "main...HEAD" in prompt
    assert "P0" in prompt
    assert "읽기 전용" in prompt          # 샌드박스가 없어도 계약은 프롬프트로 유지


def test_uncommitted_mode_allows_base_branch(tmp_path):
    make_repo(tmp_path)
    stub = make_codex_stub(tmp_path)

    result = run_review(tmp_path, "--uncommitted", codex_bin=str(stub))

    assert result.returncode == 0, result.stdout + result.stderr
    args = (tmp_path / "codex_args.txt").read_text(encoding="utf-8").splitlines()
    assert args[:1] == ["exec"] and "--sandbox" not in args
    prompt = "\n".join(args[1:])
    assert "커밋되지 않은 변경" in prompt
    assert "main...HEAD" not in prompt


def test_rejects_unknown_option(tmp_path):
    make_repo(tmp_path)
    stub = make_codex_stub(tmp_path)

    result = run_review(tmp_path, "--bogus", codex_bin=str(stub))

    assert result.returncode == 2
    assert "사용법" in result.stdout + result.stderr


def test_sandbox_env_is_passed_after_successful_probe(tmp_path):
    """QWQ_REVIEW_SANDBOX 를 주면 프로브 뒤 --sandbox 로 전달된다."""
    make_repo(tmp_path)
    subprocess.run(["git", "-C", str(tmp_path), "switch", "-q", "-c", "feature/x"], check=True)
    stub = make_codex_stub(tmp_path)

    result = run_review(tmp_path, codex_bin=str(stub), extra_env={"QWQ_REVIEW_SANDBOX": "read-only"})

    assert result.returncode == 0, result.stdout + result.stderr
    args = (tmp_path / "codex_args.txt").read_text(encoding="utf-8").splitlines()
    assert args[:3] == ["exec", "--sandbox", "read-only"]


def test_sandbox_probe_failure_fails_fast_instead_of_unverified_review(tmp_path):
    """샌드박스가 bwrap 오류로 못 뜨면 리뷰를 돌리지 않고 즉시 실패한다(조용한 '미검증' 방지)."""
    make_repo(tmp_path)
    subprocess.run(["git", "-C", str(tmp_path), "switch", "-q", "-c", "feature/x"], check=True)
    stub = tmp_path / "codex_stub"
    args_file = tmp_path / "codex_args.txt"
    stub.write_text(
        "#!/usr/bin/env bash\n"
        "if [[ \"$*\" == *--sandbox* ]]; then echo 'bwrap: loopback: Failed RTM_NEWADDR: Operation not permitted'; exit 0; fi\n"
        f"printf '%s\\n' \"$@\" > {args_file}\n",
        encoding="utf-8",
    )
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR)

    result = run_review(tmp_path, codex_bin=str(stub), extra_env={"QWQ_REVIEW_SANDBOX": "read-only"})

    assert result.returncode == 1
    assert "샌드박스" in result.stderr and "bwrap" in result.stderr
    assert not args_file.exists(), "리뷰 본 실행이 시작되면 안 된다"
