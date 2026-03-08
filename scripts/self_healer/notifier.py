"""텔레그램 알림 + T2 승인 대기 모듈"""

import asyncio
import os
import sys
import time
from typing import Optional

import aiohttp

# 프로젝트 루트를 path에 추가
PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, PROJECT_DIR)

from dotenv import load_dotenv

load_dotenv(os.path.join(PROJECT_DIR, ".env"))

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
APPROVAL_TIMEOUT_SECS = 300  # 5분


class SelfHealerNotifier:
    """자가수정 에이전트 전용 텔레그램 알림"""

    def __init__(self, bot_token: str = "", chat_id: str = ""):
        self.bot_token = bot_token or BOT_TOKEN
        self.chat_id = chat_id or CHAT_ID
        self.base_url = f"https://api.telegram.org/bot{self.bot_token}"
        self._last_update_id: int = 0

    async def send(self, text: str, parse_mode: str = "HTML") -> bool:
        """텔레그램 메시지 전송"""
        if not self.bot_token or not self.chat_id:
            print(f"[notifier] 텔레그램 미설정, 콘솔 출력:\n{text}")
            return False

        # 4096자 제한
        if len(text) > 4000:
            text = text[:4000] + "\n...(잘림)"

        try:
            timeout = aiohttp.ClientTimeout(total=30)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(
                    f"{self.base_url}/sendMessage",
                    json={
                        "chat_id": self.chat_id,
                        "text": text,
                        "parse_mode": parse_mode,
                    },
                ) as resp:
                    if resp.status == 200:
                        return True
                    body = await resp.text()
                    print(f"[notifier] 전송 실패: HTTP {resp.status} — {body}")
                    return False
        except Exception as e:
            print(f"[notifier] 전송 오류: {e}")
            return False

    async def notify_t1(
        self,
        error_type: str,
        error_message: str,
        file_path: str,
        fix_summary: str,
        commit_hash: str,
        elapsed_secs: float,
    ) -> None:
        """T1 자동수정 완료 알림"""
        text = (
            f"<b>자가수정 완료 [T1]</b>\n"
            f"오류: {_escape(error_type)}: {_escape(error_message[:200])}\n"
            f"파일: <code>{_escape(file_path or 'unknown')}</code>\n"
            f"수정: {_escape(fix_summary)}\n"
            f"커밋: <code>{commit_hash[:7]}</code>\n"
            f"소요: {elapsed_secs:.0f}초"
        )
        await self.send(text)

    async def notify_t2_request(
        self,
        error_type: str,
        error_message: str,
        file_path: str,
        diff_text: str,
        fix_summary: str,
    ) -> None:
        """T2 승인 요청"""
        diff_preview = diff_text[:1500] if diff_text else "(diff 없음)"
        text = (
            f"<b>자가수정 승인 요청 [T2]</b>\n"
            f"오류: {_escape(error_type)}: {_escape(error_message[:200])}\n"
            f"파일: <code>{_escape(file_path or 'unknown')}</code>\n"
            f"수정: {_escape(fix_summary)}\n\n"
            f"<pre>{_escape(diff_preview)}</pre>\n\n"
            f"<b>/approve</b> — 적용  |  <b>/deny</b> — 거부  (5분 타임아웃→자동 거부)"
        )
        await self.send(text)

    async def notify_t3(
        self,
        error_type: str,
        error_message: str,
        analysis: str,
    ) -> None:
        """T3 에스컬레이션"""
        text = (
            f"<b>수동 확인 필요 [T3]</b>\n"
            f"오류: {_escape(error_type)}: {_escape(error_message[:300])}\n\n"
            f"분석:\n{_escape(analysis[:2000])}"
        )
        await self.send(text)

    async def notify_rollback(self, reason: str, commit_hash: str) -> None:
        """롤백 알림"""
        text = (
            f"<b>자가수정 롤백</b>\n"
            f"사유: {_escape(reason)}\n"
            f"복원: <code>{commit_hash[:7]}</code>"
        )
        await self.send(text)

    async def notify_error(self, message: str) -> None:
        """자가수정 에이전트 내부 오류 알림"""
        text = f"<b>[self-healer 오류]</b>\n{_escape(message[:2000])}"
        await self.send(text)

    async def wait_for_approval(self, timeout: int = APPROVAL_TIMEOUT_SECS) -> bool:
        """텔레그램에서 /approve 또는 /deny 응답 대기. 타임아웃 시 False."""
        if not self.bot_token:
            print("[notifier] 텔레그램 미설정, 자동 거부")
            return False

        # 현재 update_id 갱신 (이전 메시지 무시)
        await self._flush_updates()

        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                reply = await self._poll_reply(timeout=10)
                if reply is None:
                    continue
                reply_lower = reply.strip().lower()
                if reply_lower in ("/approve", "approve", "승인"):
                    return True
                if reply_lower in ("/deny", "deny", "거부"):
                    return False
            except Exception as e:
                print(f"[notifier] 폴링 오류: {e}")
                await asyncio.sleep(5)
        return False  # 타임아웃 → 자동 거부

    async def _flush_updates(self) -> None:
        """현재까지의 업데이트를 소비하여 offset 갱신"""
        try:
            timeout = aiohttp.ClientTimeout(total=10)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(
                    f"{self.base_url}/getUpdates",
                    params={"offset": -1, "limit": 1},
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        results = data.get("result", [])
                        if results:
                            self._last_update_id = results[-1]["update_id"] + 1
        except Exception:
            pass

    async def _poll_reply(self, timeout: int = 10) -> Optional[str]:
        """getUpdates로 새 메시지 폴링"""
        try:
            req_timeout = aiohttp.ClientTimeout(total=timeout + 5)
            async with aiohttp.ClientSession(timeout=req_timeout) as session:
                async with session.get(
                    f"{self.base_url}/getUpdates",
                    params={
                        "offset": self._last_update_id,
                        "timeout": timeout,
                        "allowed_updates": '["message"]',
                    },
                ) as resp:
                    if resp.status != 200:
                        return None
                    data = await resp.json()
                    for update in data.get("result", []):
                        self._last_update_id = update["update_id"] + 1
                        msg = update.get("message", {})
                        text = msg.get("text", "")
                        chat_id = str(msg.get("chat", {}).get("id", ""))
                        if chat_id == self.chat_id and text:
                            return text
        except Exception:
            pass
        return None


def _escape(text: str) -> str:
    """HTML 이스케이프"""
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )
