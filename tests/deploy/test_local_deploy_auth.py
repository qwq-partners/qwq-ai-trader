"""로컬 배포의 인증/롤백 경계: 임시 Git 저장소와 외부 명령 대역만 사용한다."""

import os
import stat
import subprocess
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parents[2] / "scripts/deploy/local_deploy.sh"


def run(command, *, env=None):
    return subprocess.run(
        command, env=env, text=True, capture_output=True,
        stdin=subprocess.DEVNULL, timeout=20, check=False,
    )


def git(repo, *args):
    result = run(["/usr/bin/git", "-C", str(repo), *args])
    assert result.returncode == 0
    return result.stdout.strip()


def executable(path, content):
    path.write_text(content, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


@pytest.fixture
def deployment(tmp_path):
    repo = tmp_path / "repo"
    origin = tmp_path / "origin.git"
    assert run(["/usr/bin/git", "init", "--bare", "-q", str(origin)]).returncode == 0
    assert run(["/usr/bin/git", "init", "-q", "-b", "main", str(repo)]).returncode == 0
    git(repo, "config", "user.name", "Deploy Test")
    git(repo, "config", "user.email", "deploy-test@example.invalid")
    verify = repo / "scripts/dev/verify.sh"
    verify.parent.mkdir(parents=True)
    executable(verify, '#!/bin/bash\nprintf "verify\\n" >> "$QWQ_TEST_EVENTS"\n'
               '[[ ${QWQ_TEST_FAILURE:-} != verify ]]\n')
    (repo / "version.txt").write_text("old\n", encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "old")
    old = git(repo, "rev-parse", "HEAD")
    (repo / "version.txt").write_text("new\n", encoding="utf-8")
    git(repo, "commit", "-qam", "new")
    target = git(repo, "rev-parse", "HEAD")
    git(repo, "remote", "add", "origin", str(origin))
    git(repo, "push", "-q", "origin", "main")
    git(repo, "checkout", "-q", "--detach", old)

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    events = tmp_path / "events"
    events.touch()
    executable(bin_dir / "git", '''#!/bin/bash
[[ $1 == -C && $2 == "$QWQ_DEPLOY_REPO" ]] || exit 91
case $3 in
  fetch) printf 'fetch\n' >> "$QWQ_TEST_EVENTS" ;;
  checkout) printf 'checkout %s\n' "${@: -1}" >> "$QWQ_TEST_EVENTS" ;;
esac
exec /usr/bin/git "$@"
''')
    executable(bin_dir / "sudo", '''#!/bin/bash
# 인증 입력은 절대 기록하지 않는다. 부적절한 인증 방식은 고정 표식만 남긴다.
if [[ "$*" == "-n -l systemctl restart $QWQ_DEPLOY_SERVICE" ]]; then
  printf 'preflight\n' >> "$QWQ_TEST_EVENTS"
  [[ ${QWQ_TEST_FAILURE:-} != preflight ]]
  exit $?
fi
if [[ "$*" != "-n systemctl restart $QWQ_DEPLOY_SERVICE" ]]; then
  /bin/cat >/dev/null
  printf 'invalid-sudo\n' >> "$QWQ_TEST_EVENTS"
  exit 90
fi
version=$(/bin/cat "$QWQ_DEPLOY_REPO/version.txt")
printf 'restart %s\n' "$version" >> "$QWQ_TEST_EVENTS"
[[ ${QWQ_TEST_FAILURE:-} != restart-all ]] || exit 1
[[ ${QWQ_TEST_FAILURE:-} != restart-target || $version != new ]]
''')
    executable(bin_dir / "systemctl", '''#!/bin/bash
[[ "$*" == "is-active --quiet $QWQ_DEPLOY_SERVICE" ]] || exit 92
version=$(/bin/cat "$QWQ_DEPLOY_REPO/version.txt")
printf 'active %s\n' "$version" >> "$QWQ_TEST_EVENTS"
exit 0
''')
    executable(bin_dir / "curl", '''#!/bin/bash
[[ "$*" == "-fsS --max-time 5 $QWQ_DEPLOY_HEALTH_URL" ]] || exit 93
version=$(/bin/cat "$QWQ_DEPLOY_REPO/version.txt")
printf 'health %s\n' "$version" >> "$QWQ_TEST_EVENTS"
[[ ${QWQ_TEST_FAILURE:-} != health-all ]] || exit 1
[[ ${QWQ_TEST_FAILURE:-} != health-target || $version != new ]]
''')
    executable(bin_dir / "sleep", "#!/bin/bash\nexit 0\n")

    env = os.environ | {
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "QWQ_DEPLOY_REPO": str(repo),
        "QWQ_DEPLOY_SERVICE": "deploy-test.service",
        "QWQ_DEPLOY_HEALTH_URL": "http://deployment.invalid/health",
        "QWQ_DEPLOY_LOCK_FILE": str(tmp_path / "override.lock"),
        "QWQ_TEST_EVENTS": str(events),
    }
    return repo, old, target, env, events


def deploy(fixture, failure=""):
    repo, old, target, env, events = fixture
    result = run(["bash", str(SCRIPT), target], env=env | {"QWQ_TEST_FAILURE": failure})
    # 기존 코드의 인증값은 stdout/stderr/단언 메시지에 싣지 않는다.
    return result, events.read_text(encoding="utf-8").splitlines()


def test_permission_denial_leaves_checkout_and_service_untouched(deployment):
    repo, old, _, _, _ = deployment
    result, events = deploy(deployment, "preflight")
    assert result.returncode == 1
    assert git(repo, "rev-parse", "HEAD") == old
    assert events == ["preflight"]
    assert "[완료]" not in result.stdout


def test_authorized_deploy_checks_permission_before_changes(deployment):
    repo, _, target, env, _ = deployment
    result, events = deploy(deployment)
    assert result.returncode == 0
    assert git(repo, "rev-parse", "HEAD") == target
    assert events == ["preflight", "fetch", f"checkout {target}", "verify",
                      "restart new", "active new", "health new"]
    assert Path(env["QWQ_DEPLOY_LOCK_FILE"]).exists()
    assert "[완료]" in result.stdout


@pytest.mark.parametrize("failure", ["verify", "restart-target", "health-target"])
def test_failed_deploy_restores_previous_commit_and_service(deployment, failure):
    repo, old, target, _, _ = deployment
    result, events = deploy(deployment, failure)
    assert result.returncode == 1
    assert git(repo, "rev-parse", "HEAD") == old
    assert events[:4] == ["preflight", "fetch", f"checkout {target}", "verify"]
    assert events[-4:] == [f"checkout {old}", "restart old", "active old", "health old"]
    assert "invalid-sudo" not in events
    assert "[복구]" in result.stderr
    assert "[완료]" not in result.stdout
    if failure == "verify":
        assert "restart new" not in events
    if failure == "restart-target":
        assert "active new" not in events
        assert "health new" not in events


@pytest.mark.parametrize("failure", ["restart-all", "health-all"])
def test_failed_rollback_is_emergency_exit_two(deployment, failure):
    repo, old, _, _, _ = deployment
    result, events = deploy(deployment, failure)
    assert result.returncode == 2
    assert git(repo, "rev-parse", "HEAD") == old
    assert events[0] == "preflight"
    assert "restart old" in events
    assert "invalid-sudo" not in events
    assert "[긴급]" in result.stderr
    assert "[완료]" not in result.stdout
    assert "[복구]" not in result.stderr
    if failure == "restart-all":
        assert "health new" not in events
        assert "health old" not in events
    else:
        assert "health new" in events
        assert "health old" in events
