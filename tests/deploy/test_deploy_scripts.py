"""WSL entry point deploy_lightsail.sh: read-only check, fixed-SHA deploy via the server's local_deploy.sh, postcheck."""
import os
import stat
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/deploy/deploy_lightsail.sh"
SHA = "a" * 40


def executable(path: Path, content: str):
    path.write_text(content, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def run_script(tmp_path, *args, **env_extra):
    """Fake ssh appends its arguments and stdin to a log; nothing reaches a real host."""
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir(exist_ok=True)
    log = tmp_path / "ssh.log"
    executable(fake_bin / "ssh", "#!/usr/bin/env bash\n"
               'printf -- "ARGS: %s\\n" "$*" >> "$QWQ_TEST_SSH_LOG"\n'
               'if [[ ! -t 0 ]]; then sed "s/^/STDIN: /" >> "$QWQ_TEST_SSH_LOG"; fi\n'
               'exit "${QWQ_TEST_SSH_EXIT:-0}"\n')
    env = os.environ | {"PATH": f"{fake_bin}:{os.environ['PATH']}", "QWQ_DEPLOY_ROOT": str(ROOT),
                        "QWQ_DEPLOY_SKIP_LOCAL_GIT_CHECKS": "1", "QWQ_DEPLOY_TARGET_SHA": SHA,
                        "QWQ_DEPLOY_SSH_HOST": "ubuntu@test.invalid", "QWQ_DEPLOY_SSH_KEY": str(tmp_path / "missing-key"),
                        "QWQ_DEPLOY_NOW": "2026-10-31 21:00", "QWQ_TEST_SSH_LOG": str(log), **env_extra}
    result = subprocess.run(["bash", str(SCRIPT), *args], cwd=ROOT, env=env, text=True, capture_output=True, check=False)
    return result, (log.read_text(encoding="utf-8") if log.exists() else "")


def test_default_is_read_only_check_with_server_precheck_only(tmp_path):
    result, log = run_script(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "읽기 전용" in result.stdout and log.count("ARGS:") == 1
    assert "ubuntu@test.invalid bash -s -- /home/ubuntu/projects/qwq-ai-trader" in log
    assert "STDIN: " in log and "api/orders/pending" in log and "evolved_overrides" in log
    assert "local_deploy.sh" not in log                                   # check never deploys


def test_deploy_runs_precheck_then_the_servers_local_deploy_with_the_fixed_sha(tmp_path):
    result, log = run_script(tmp_path, "--deploy")
    assert result.returncode == 0, result.stdout + result.stderr
    assert log.count("ARGS:") == 2
    assert f"bash scripts/deploy/local_deploy.sh '{SHA}'" in log
    assert "git reset" not in log                                         # never rewrites the production tree itself
    explicit, log2 = run_script(tmp_path, "--deploy", "b" * 40)
    assert explicit.returncode == 0 and ("b" * 40) in log2
    bad, _ = run_script(tmp_path, "--deploy", "abc")
    assert bad.returncode != 0 and "40자 전체 SHA" in bad.stderr


def test_deploy_propagates_server_exit_code_and_stops_after_failed_precheck(tmp_path):
    result, log = run_script(tmp_path, "--deploy", QWQ_TEST_SSH_EXIT="1")
    assert result.returncode == 1
    assert log.count("ARGS:") == 1                                        # precheck failed → local_deploy never called


def test_market_hours_are_refused_unless_explicitly_allowed(tmp_path):
    refused, log = run_script(tmp_path, "--deploy", QWQ_DEPLOY_NOW="2026-11-02 10:30")   # Monday, intraday
    assert refused.returncode != 0 and "장중" in refused.stderr and log == ""
    weekend, _ = run_script(tmp_path, "--check", QWQ_DEPLOY_NOW="2026-11-01 10:30")      # Sunday
    assert weekend.returncode == 0
    after_close, _ = run_script(tmp_path, "--check", QWQ_DEPLOY_NOW="2026-11-02 15:30")  # close boundary
    assert after_close.returncode == 0
    allowed, log3 = run_script(tmp_path, "--deploy", QWQ_DEPLOY_NOW="2026-11-02 10:30", QWQ_DEPLOY_ALLOW_MARKET_HOURS="1")
    assert allowed.returncode == 0 and "local_deploy.sh" in log3


def test_postcheck_runs_ops_check_only(tmp_path):
    result, log = run_script(tmp_path, "--postcheck", "22:05", QWQ_DEPLOY_NOW="2026-11-02 10:30")  # allowed intraday: read-only
    assert result.returncode == 0, result.stdout + result.stderr
    assert log.count("ARGS:") == 1 and "ops_check.sh '22:05'" in log and "local_deploy" not in log
    usage = subprocess.run(["bash", str(SCRIPT), "--bogus"], text=True, capture_output=True)
    assert usage.returncode == 2
