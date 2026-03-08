#!/usr/bin/env python3
"""
qwq-ai-trader 자가수정 에이전트 — 메인 데몬

journalctl 로그를 실시간 감시하며 심각한 오류 발생 시
Claude Code를 호출해 코드 분석·수정·재배포를 수행한다.

실행: python scripts/self_healer/error_watcher.py
systemd: qwq-self-healer.service
"""

import asyncio
import fcntl
import json
import os
import signal
import sys
import time
from collections import deque
from datetime import date

# 프로젝트 루트
PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(PROJECT_DIR, "scripts", "self_healer"))

from error_classifier import ErrorClassifier, ErrorContext, Tier
from healer_agent import call_claude_code, build_prompt, HealResult
from notifier import SelfHealerNotifier
from rollback import (
    save_pre_fix_state,
    restart_service,
    verify_fix,
    rollback,
    get_fix_diff,
    get_current_head,
)

# ─── 설정 ───
SERVICE_NAME = "qwq-ai-trader"
LOCK_FILE = "/tmp/self_healer.lock"
STATE_FILE = os.path.join(os.path.dirname(__file__), "state.json")
HISTORY_FILE = os.path.expanduser("~/.cache/ai_trader/self_healer_history.json")

MAX_FIXES_PER_DAY = 3
MIN_COOLDOWN_SECS = 300        # 수정 간 최소 5분
FIX_VERIFY_WAIT_SECS = 60     # 수정 후 60초 모니터링
DEBOUNCE_SECS = 30            # 동일 오류 디바운싱
LOG_BUFFER_SIZE = 100          # 로그 버퍼 크기

# ─── 글로벌 ───
_shutdown = False


def _signal_handler(signum, frame):
    global _shutdown
    print(f"[self_healer] 시그널 {signum} 수신, 종료 중...")
    _shutdown = True


signal.signal(signal.SIGTERM, _signal_handler)
signal.signal(signal.SIGINT, _signal_handler)


class StateManager:
    """일일 수정 횟수, 쿨다운 상태 관리"""

    def __init__(self, state_path: str = STATE_FILE):
        self.state_path = state_path
        self._state = self._load()

    def _load(self) -> dict:
        try:
            with open(self.state_path, "r") as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return {"date": "", "fixes_today": 0, "last_fix_timestamp": 0, "history": []}

    def _save(self) -> None:
        with open(self.state_path, "w") as f:
            json.dump(self._state, f, indent=2, ensure_ascii=False)

    def _reset_if_new_day(self) -> None:
        today = date.today().isoformat()
        if self._state.get("date") != today:
            self._state["date"] = today
            self._state["fixes_today"] = 0

    def can_fix(self) -> tuple[bool, str]:
        """수정 가능 여부 및 사유"""
        self._reset_if_new_day()

        if self._state["fixes_today"] >= MAX_FIXES_PER_DAY:
            return False, f"일일 수정 한도 도달 ({MAX_FIXES_PER_DAY}회)"

        elapsed = time.time() - self._state.get("last_fix_timestamp", 0)
        if elapsed < MIN_COOLDOWN_SECS:
            remaining = int(MIN_COOLDOWN_SECS - elapsed)
            return False, f"쿨다운 중 ({remaining}초 남음)"

        return True, ""

    def record_fix(self, error_key: str, summary: str, commit_hash: str) -> None:
        self._reset_if_new_day()
        self._state["fixes_today"] += 1
        self._state["last_fix_timestamp"] = time.time()
        self._state.setdefault("history", []).append({
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "error_key": error_key,
            "summary": summary,
            "commit_hash": commit_hash,
        })
        # 히스토리 최대 50건
        self._state["history"] = self._state["history"][-50:]
        self._save()

    def save_history(self, entry: dict) -> None:
        """영속 히스토리에 기록"""
        os.makedirs(os.path.dirname(HISTORY_FILE), exist_ok=True)
        try:
            with open(HISTORY_FILE, "r") as f:
                history = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            history = []
        history.append(entry)
        history = history[-200:]  # 최대 200건
        with open(HISTORY_FILE, "w") as f:
            json.dump(history, f, indent=2, ensure_ascii=False)


class ErrorWatcher:
    """journalctl 실시간 감시 + 오류 처리 메인 루프"""

    def __init__(self):
        self.classifier = ErrorClassifier()
        self.notifier = SelfHealerNotifier()
        self.state = StateManager()
        self.log_buffer: deque[str] = deque(maxlen=LOG_BUFFER_SIZE)
        self.error_timestamps: dict[str, list[float]] = {}  # 오류별 발생 시각
        self.last_processed: dict[str, float] = {}  # 디바운싱용

    async def _async_tail_journal(self):
        """journalctl -f 로 실시간 로그 스트림 (비동기, 이벤트 루프 블로킹 방지)"""
        proc = await asyncio.create_subprocess_exec(
            "journalctl", "-u", SERVICE_NAME,
            "-f", "-n", "0", "--no-pager",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            while not _shutdown:
                line_bytes = await proc.stdout.readline()
                if not line_bytes:
                    break
                yield line_bytes.decode("utf-8", errors="replace").rstrip()
        finally:
            proc.kill()
            await proc.wait()

    async def run(self) -> None:
        """메인 이벤트 루프"""
        print(f"[self_healer] 시작 — PID {os.getpid()}")
        print(f"[self_healer] 서비스 감시: {SERVICE_NAME}")
        print(f"[self_healer] 일일 수정 한도: {MAX_FIXES_PER_DAY}회")
        print(f"[self_healer] 쿨다운: {MIN_COOLDOWN_SECS}초")

        async for line in self._async_tail_journal():
            if _shutdown:
                break

            self.log_buffer.append(line)

            # 분류
            ctx = self.classifier.classify(line, list(self.log_buffer))
            if ctx is None:
                continue

            # 디바운싱: 동일 오류 30초 내 무시
            err_key = ErrorClassifier.error_key(ctx)
            now = time.time()
            if now - self.last_processed.get(err_key, 0) < DEBOUNCE_SECS:
                continue
            self.last_processed[err_key] = now

            # 반복 횟수 기록
            self.error_timestamps.setdefault(err_key, []).append(now)
            # 30분 이전 기록 정리
            self.error_timestamps[err_key] = [
                t for t in self.error_timestamps[err_key] if now - t < 1800
            ]

            # T2 승격: 30분 내 3회 이상 반복 시 T1 → T2
            if ctx.tier == Tier.T1 and len(self.error_timestamps.get(err_key, [])) >= 3:
                print(f"[self_healer] T1→T2 승격 (반복 {len(self.error_timestamps[err_key])}회): {err_key}")
                ctx.tier = Tier.T2

            print(f"[self_healer] 오류 감지 [{ctx.tier.value.upper()}]: {ctx.error_type} — {ctx.error_message[:100]}")

            # 비동기로 처리
            try:
                await self._handle_error(ctx, err_key)
            except Exception as e:
                print(f"[self_healer] 처리 중 내부 오류: {e}")
                await self.notifier.notify_error(f"오류 처리 실패: {e}\n원본: {ctx.error_message[:200]}")

    async def _handle_error(self, ctx: ErrorContext, err_key: str) -> None:
        """티어별 오류 처리"""
        if ctx.tier == Tier.T1:
            await self._handle_t1(ctx, err_key)
        elif ctx.tier == Tier.T2:
            await self._handle_t2(ctx, err_key)
        elif ctx.tier == Tier.T3:
            await self._handle_t3(ctx)

    async def _handle_t1(self, ctx: ErrorContext, err_key: str) -> None:
        """T1: 자동 수정 → 커밋 → 재시작 → 60초 검증"""
        can_fix, reason = self.state.can_fix()
        if not can_fix:
            print(f"[self_healer] T1 수정 불가: {reason}")
            await self.notifier.send(
                f"<b>[self-healer]</b> T1 수정 불가: {reason}\n오류: {ctx.error_message[:200]}"
            )
            return

        pre_hash = save_pre_fix_state()
        prompt = build_prompt(ctx)

        print(f"[self_healer] Claude Code 호출 중 (T1)...")
        result = await asyncio.to_thread(call_claude_code, prompt)

        if not result.success:
            reason_text = result.cannot_fix_reason or result.summary
            print(f"[self_healer] 수정 실패: {reason_text}")
            await self.notifier.send(
                f"<b>[self-healer T1]</b> 수정 실패\n오류: {ctx.error_type}\n사유: {reason_text[:500]}"
            )
            return

        # 서비스 재시작
        print("[self_healer] 서비스 재시작 중...")
        restart_service()

        # 60초 검증
        print(f"[self_healer] {FIX_VERIFY_WAIT_SECS}초 검증 중...")
        fix_ok = await asyncio.to_thread(
            verify_fix, ctx.error_type, FIX_VERIFY_WAIT_SECS
        )

        if not fix_ok:
            print("[self_healer] 오류 재발 → 롤백")
            await asyncio.to_thread(rollback, pre_hash)
            await self.notifier.notify_rollback(
                f"T1 수정 후 오류 재발: {ctx.error_type}", pre_hash
            )
            return

        # 성공
        commit_hash = get_current_head()
        self.state.record_fix(err_key, result.summary, commit_hash)
        self.state.save_history({
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "tier": "T1",
            "error_key": err_key,
            "error_type": ctx.error_type,
            "summary": result.summary,
            "commit_hash": commit_hash,
            "elapsed_secs": result.elapsed_secs,
        })

        await self.notifier.notify_t1(
            error_type=ctx.error_type,
            error_message=ctx.error_message,
            file_path=ctx.file_path or "unknown",
            fix_summary=result.summary,
            commit_hash=commit_hash,
            elapsed_secs=result.elapsed_secs,
        )
        print(f"[self_healer] T1 수정 완료: {result.summary}")

    async def _handle_t2(self, ctx: ErrorContext, err_key: str) -> None:
        """T2: 수정 → 텔레그램 승인 → 배포"""
        can_fix, reason = self.state.can_fix()
        if not can_fix:
            print(f"[self_healer] T2 수정 불가: {reason}")
            await self.notifier.send(
                f"<b>[self-healer]</b> T2 수정 불가: {reason}\n오류: {ctx.error_message[:200]}"
            )
            return

        pre_hash = save_pre_fix_state()
        prompt = build_prompt(ctx)

        print(f"[self_healer] Claude Code 호출 중 (T2)...")
        result = await asyncio.to_thread(call_claude_code, prompt)

        if not result.success:
            reason_text = result.cannot_fix_reason or result.summary
            print(f"[self_healer] 수정 실패: {reason_text}")
            await self.notifier.send(
                f"<b>[self-healer T2]</b> 수정 실패\n오류: {ctx.error_type}\n사유: {reason_text[:500]}"
            )
            return

        # diff 추출
        diff_text = get_fix_diff()

        # 텔레그램 승인 요청
        await self.notifier.notify_t2_request(
            error_type=ctx.error_type,
            error_message=ctx.error_message,
            file_path=ctx.file_path or "unknown",
            diff_text=diff_text,
            fix_summary=result.summary,
        )

        print("[self_healer] T2 승인 대기 (5분)...")
        approved = await self.notifier.wait_for_approval()

        if not approved:
            # 거부 → 롤백
            print("[self_healer] T2 거부/타임아웃 → 롤백")
            await asyncio.to_thread(rollback, pre_hash)
            await self.notifier.send("<b>[self-healer]</b> T2 수정 거부됨, 롤백 완료")
            return

        # 승인 → 서비스 재시작
        print("[self_healer] T2 승인됨, 서비스 재시작 중...")
        restart_service()

        # 60초 검증
        fix_ok = await asyncio.to_thread(
            verify_fix, ctx.error_type, FIX_VERIFY_WAIT_SECS
        )

        if not fix_ok:
            print("[self_healer] T2 수정 후 오류 재발 → 롤백")
            await asyncio.to_thread(rollback, pre_hash)
            await self.notifier.notify_rollback(
                f"T2 승인 후에도 오류 재발: {ctx.error_type}", pre_hash
            )
            return

        # 성공
        commit_hash = get_current_head()
        self.state.record_fix(err_key, result.summary, commit_hash)
        self.state.save_history({
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "tier": "T2",
            "error_key": err_key,
            "error_type": ctx.error_type,
            "summary": result.summary,
            "commit_hash": commit_hash,
            "elapsed_secs": result.elapsed_secs,
            "approved": True,
        })

        await self.notifier.send(
            f"<b>T2 수정 적용 완료</b>\n"
            f"수정: {result.summary}\n"
            f"커밋: <code>{commit_hash[:7]}</code>"
        )
        print(f"[self_healer] T2 수정 완료: {result.summary}")

    async def _handle_t3(self, ctx: ErrorContext) -> None:
        """T3: 분석만 → 텔레그램 보고 (일일 한도 공유)"""
        can_fix, reason = self.state.can_fix()
        if not can_fix:
            print(f"[self_healer] T3 분석 불가 (한도): {reason}")
            await self.notifier.send(
                f"<b>[self-healer]</b> T3 분석 불가: {reason}\n오류: {ctx.error_message[:200]}"
            )
            return

        prompt = build_prompt(ctx)

        print(f"[self_healer] Claude Code 호출 중 (T3 분석)...")
        result = await asyncio.to_thread(call_claude_code, prompt)

        analysis = result.summary if result.success else f"분석 실패: {result.summary}"

        await self.notifier.notify_t3(
            error_type=ctx.error_type,
            error_message=ctx.error_message,
            analysis=analysis,
        )
        print(f"[self_healer] T3 분석 전송: {ctx.error_type}")


def acquire_lock() -> int:
    """프로세스 락 획득. 실패 시 종료."""
    try:
        fd = os.open(LOCK_FILE, os.O_CREAT | os.O_WRONLY)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        os.write(fd, str(os.getpid()).encode())
        os.fsync(fd)
        return fd
    except (OSError, IOError):
        print("[self_healer] 이미 실행 중 (락 파일 충돌). 종료.")
        sys.exit(1)


def release_lock(fd: int) -> None:
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
        os.unlink(LOCK_FILE)
    except OSError:
        pass


def main():
    lock_fd = acquire_lock()
    try:
        watcher = ErrorWatcher()
        asyncio.run(watcher.run())
    except KeyboardInterrupt:
        print("\n[self_healer] 키보드 인터럽트, 종료")
    finally:
        release_lock(lock_fd)
        print("[self_healer] 종료 완료")


if __name__ == "__main__":
    main()
