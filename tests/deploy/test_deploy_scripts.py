import os
import stat
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
LOCAL_SCRIPT = ROOT / "scripts/deploy/deploy_lightsail.sh"
REMOTE_SCRIPT = ROOT / "scripts/deploy/remote_deploy.sh"


def run(command, *, cwd=None, env=None):
    return subprocess.run(
        command,
        cwd=cwd,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def git(path, *args):
    result = run(["git", "-C", str(path), *args])
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout.strip()


def executable(path: Path, content: str):
    path.write_text(content, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def make_remote_fixture(tmp_path):
    bare = tmp_path / "origin.git"
    repo = tmp_path / "production"
    run(["git", "init", "--bare", "-q", str(bare)])
    run(["git", "init", "-q", "-b", "main", str(repo)])
    git(repo, "config", "user.email", "deploy-test@example.invalid")
    git(repo, "config", "user.name", "Deploy Test")
    (repo / "version.txt").write_text("old\n", encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "old")
    old_sha = git(repo, "rev-parse", "HEAD")
    git(repo, "remote", "add", "origin", str(bare))
    git(repo, "push", "-qu", "origin", "main")
    (repo / "version.txt").write_text("new\n", encoding="utf-8")
    git(repo, "commit", "-qam", "new")
    target_sha = git(repo, "rev-parse", "HEAD")
    git(repo, "push", "-q", "origin", "main")
    git(repo, "reset", "--hard", old_sha)
    return repo, old_sha, target_sha


def remote_env(tmp_path, repo, target_sha, *, fail_target_health=False):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    executable(
        bin_dir / "sudo",
        "#!/usr/bin/env bash\n"
        "if [[ $1 == -n ]]; then shift; fi\n"
        "if [[ $1 == systemctl && $2 == is-active ]]; then exit 0; fi\n"
        "exit 0\n",
    )
    curl_body = "exit 0"
    if fail_target_health:
        curl_body = (
            'current=$(git -C "$QWQ_DEPLOY_REPO" rev-parse HEAD)\n'
            '[[ "$current" != "$QWQ_TEST_TARGET_SHA" ]]'
        )
    executable(bin_dir / "curl", f"#!/usr/bin/env bash\n{curl_body}\n")
    return os.environ | {
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "QWQ_DEPLOY_REPO": str(repo),
        "QWQ_DEPLOY_LOCK_FILE": str(tmp_path / "deploy.lock"),
        "QWQ_DEPLOY_SKIP_INSTALL": "1",
        "QWQ_DEPLOY_VERIFY_COMMAND": "true",
        "QWQ_DEPLOY_HEALTH_ATTEMPTS": "2",
        "QWQ_DEPLOY_HEALTH_INTERVAL": "0",
        "QWQ_TEST_TARGET_SHA": target_sha,
    }


def test_local_script_defaults_to_read_only_check(tmp_path):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    ssh_log = tmp_path / "ssh.log"
    executable(
        fake_bin / "ssh",
        "#!/usr/bin/env bash\n"
        'printf "%s\\n" "$*" > "$QWQ_TEST_SSH_LOG"\n'
        "cat >/dev/null\n",
    )
    env = os.environ | {
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "QWQ_DEPLOY_ROOT": str(ROOT),
        "QWQ_DEPLOY_SKIP_LOCAL_GIT_CHECKS": "1",
        "QWQ_DEPLOY_TARGET_SHA": "a" * 40,
        "QWQ_TEST_SSH_LOG": str(ssh_log),
    }

    result = run(["bash", str(LOCAL_SCRIPT)], cwd=ROOT, env=env)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "--check" in ssh_log.read_text(encoding="utf-8")
    assert "읽기 전용" in result.stdout


def test_remote_deploy_moves_to_target_and_passes_health_check(tmp_path):
    repo, _old_sha, target_sha = make_remote_fixture(tmp_path)
    env = remote_env(tmp_path, repo, target_sha)

    result = run(["bash", str(REMOTE_SCRIPT), "--deploy", target_sha], env=env)

    assert result.returncode == 0, result.stdout + result.stderr
    assert git(repo, "rev-parse", "HEAD") == target_sha
    assert "배포가 완료" in result.stdout


def test_remote_deploy_rolls_back_when_target_health_fails(tmp_path):
    repo, old_sha, target_sha = make_remote_fixture(tmp_path)
    env = remote_env(tmp_path, repo, target_sha, fail_target_health=True)

    result = run(["bash", str(REMOTE_SCRIPT), "--deploy", target_sha], env=env)

    assert result.returncode != 0
    assert git(repo, "rev-parse", "HEAD") == old_sha
    assert "롤백 완료" in result.stdout + result.stderr
