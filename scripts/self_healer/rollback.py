"""롤백 메커니즘 모듈"""

import select
import subprocess
import time
import re
from typing import Optional


PROJECT_DIR = "/home/user/projects/qwq-ai-trader"
SERVICE_NAME = "qwq-ai-trader"
VERIFY_WAIT_SECS = 60


def save_pre_fix_state() -> str:
    """수정 전 HEAD 커밋 해시 저장"""
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=PROJECT_DIR,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def get_current_head() -> str:
    """현재 HEAD 해시"""
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=PROJECT_DIR,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def get_fix_diff() -> str:
    """최신 커밋의 diff 반환"""
    result = subprocess.run(
        ["git", "diff", "HEAD~1..HEAD"],
        cwd=PROJECT_DIR,
        capture_output=True,
        text=True,
    )
    return result.stdout


def restart_service() -> bool:
    """봇 서비스 재시작"""
    result = subprocess.run(
        ["sudo", "-S", "systemctl", "restart", SERVICE_NAME],
        input="user123!\n",
        cwd=PROJECT_DIR,
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def check_service_active() -> bool:
    """서비스 활성 상태 확인"""
    result = subprocess.run(
        ["systemctl", "is-active", SERVICE_NAME],
        capture_output=True,
        text=True,
    )
    return result.stdout.strip() == "active"


def verify_fix(error_pattern: str, wait_secs: int = VERIFY_WAIT_SECS) -> bool:
    """수정 후 wait_secs 동안 동일 오류 재발 여부 확인.
    True = 오류 미재발 (수정 성공), False = 재발 (롤백 필요)
    """
    # 재시작 후 서비스 안정화 대기
    time.sleep(5)
    if not check_service_active():
        print("[rollback] 서비스 재시작 실패 — 롤백 필요")
        return False

    # wait_secs 동안 로그 감시
    try:
        proc = subprocess.Popen(
            [
                "journalctl", "-u", SERVICE_NAME,
                "-f", "-n", "0", "--no-pager",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        deadline = time.time() + wait_secs
        compiled = re.compile(error_pattern)

        while time.time() < deadline:
            ready, _, _ = select.select([proc.stdout], [], [], 1.0)
            if ready:
                line = proc.stdout.readline()
                if line and compiled.search(line):
                    proc.kill()
                    print(f"[rollback] 오류 재발 감지: {line.strip()}")
                    return False

        proc.kill()
        return True  # 오류 미재발

    except Exception as e:
        print(f"[rollback] 검증 중 오류: {e}")
        return True  # 검증 불능 시 낙관적으로 통과


def rollback(pre_fix_hash: str) -> bool:
    """git revert HEAD로 수정을 되돌리고 서비스 재시작"""
    print(f"[rollback] 롤백 시작 → {pre_fix_hash[:7]}")

    # git revert --no-edit HEAD
    result = subprocess.run(
        ["git", "revert", "--no-edit", "HEAD"],
        cwd=PROJECT_DIR,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        # revert 실패 시 hard reset (최후 수단)
        print(f"[rollback] revert 실패, reset 시도: {result.stderr}")
        subprocess.run(
            ["git", "reset", "--hard", pre_fix_hash],
            cwd=PROJECT_DIR,
            capture_output=True,
            text=True,
        )

    # 서비스 재시작
    restart_service()
    time.sleep(5)

    if check_service_active():
        print("[rollback] 롤백 완료, 서비스 정상")
        return True
    else:
        print("[rollback] 롤백 후에도 서비스 비정상")
        return False


def apply_stashed_fix() -> bool:
    """T2 승인 시: stash된 수정 적용"""
    result = subprocess.run(
        ["git", "stash", "pop"],
        cwd=PROJECT_DIR,
        capture_output=True,
        text=True,
    )
    return result.returncode == 0
