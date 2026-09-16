"""토스 액세스 토큰 단일 캐시 (설계 §4.2)

토스는 **클라이언트당 유효 토큰이 1개**다. 재발급하면 이전 토큰이 `token-revoked` 로
즉시 무효화되므로, 봇·대시보드·스크립트가 각자 발급하면 서로를 끊는다. 그래서:

- 캐시 파일 1개(`~/.cache/ai_trader/toss_token.json`, mode 0600)를 모든 프로세스가 공유
- 만료 30분 전 선제 갱신
- 프로세스 간 경합은 `fcntl.flock` 으로 직렬화하고 **락 안에서 다시 읽어**(double-check)
  다른 프로세스가 이미 발급했으면 발급하지 않는다
- `token-revoked` 는 "남이 새로 발급했다"는 신호이므로 **재발급하지 않고 캐시를 다시 읽는** 것이
  먼저다. 캐시 토큰이 방금 실패한 토큰과 같을 때만 락을 잡고 1회 발급한다
- 실패한 토큰은 절대 다시 반환하지 않는다 (서버·로컬 만료 시각 불일치 시 무한 루프 방지)
- 캐시 파일은 어떤 경우에도 **삭제하지 않는다** (삭제 = 모든 프로세스가 동시에 발급 경쟁)

경로는 전부 생성자 인자다 — 테스트는 tmp_path 를 주입한다(운영 캐시 접근 금지).
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import json
import os
import time
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, Optional

import aiohttp
from loguru import logger

from src.utils.atomic_io import atomic_write_json

DEFAULT_CACHE_PATH = Path.home() / ".cache" / "ai_trader" / "toss_token.json"
DEFAULT_BASE_URL = "https://openapi.tossinvest.com"

REFRESH_MARGIN_SEC = 1800.0   # 만료 30분 전 선제 갱신
LOCK_TIMEOUT_SEC = 15.0       # 파일 락 대기 상한 — 초과 시 발급하지 않고 캐시 재사용
LOCK_POLL_SEC = 0.05

# 재발급이 필요한 401 에러 코드 (본문 code 기준 — HTTP 401 만으로 분기하지 않는다)
TOKEN_ERROR_CODES = frozenset({"expired-token", "invalid-token", "token-revoked"})
REVOKED_CODE = "token-revoked"

# 토큰 발급 콜러블 계약: (client_id, client_secret) -> {"access_token": str, "expires_in": int}
TokenFetcher = Callable[[str, str], Awaitable[Dict[str, Any]]]


class TossLockTimeout(TimeoutError):
    """토큰 파일 락 대기 초과 — 네트워크 타임아웃과 구분해서 잡는다"""


class TossTokenError(RuntimeError):
    """토큰 발급·조회 실패"""


def _mask(token: str) -> str:
    """로그·원장에 토큰 원문이 남지 않게 마스킹 (설계 §6.5)"""
    if not token:
        return "<empty>"
    return f"{token[:6]}…{token[-4:]}({len(token)}자)"


async def default_token_fetcher(
    client_id: str,
    client_secret: str,
    *,
    base_url: str = DEFAULT_BASE_URL,
    timeout_sec: int = 10,
) -> Dict[str, Any]:
    """`POST /oauth2/token` (client_credentials) — 기본 발급 콜러블

    실패 응답은 OAuth2 표준 포맷(`{"error": ...}`)이라 공통 envelope 과 다르다.
    """
    from .rate_limit import acquire  # 순환 import 회피 (rate_limit 은 token 을 모른다)

    await acquire("AUTH")
    payload = {
        "grant_type": "client_credentials",
        "client_id": client_id,
        "client_secret": client_secret,
    }
    timeout = aiohttp.ClientTimeout(total=timeout_sec)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(f"{base_url}/oauth2/token", data=payload) as resp:
            body = await resp.json(content_type=None)
            if resp.status != 200:
                err = (body or {}).get("error") if isinstance(body, dict) else None
                raise TossTokenError(f"토큰 발급 실패 HTTP {resp.status} error={err}")
            return body if isinstance(body, dict) else {}


class TossTokenCache:
    """프로세스 간 공유 토큰 캐시"""

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        *,
        cache_path: Optional[Path] = None,
        lock_path: Optional[Path] = None,
        token_fetcher: Optional[TokenFetcher] = None,
        refresh_margin_sec: float = REFRESH_MARGIN_SEC,
        lock_timeout_sec: float = LOCK_TIMEOUT_SEC,
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._cache_path = Path(cache_path) if cache_path is not None else DEFAULT_CACHE_PATH
        self._lock_path = (
            Path(lock_path) if lock_path is not None
            else self._cache_path.with_suffix(self._cache_path.suffix + ".lock")
        )
        self._fetcher: TokenFetcher = token_fetcher if token_fetcher is not None else default_token_fetcher
        self._refresh_margin = refresh_margin_sec
        self._lock_timeout = lock_timeout_sec
        self._inproc_lock = asyncio.Lock()   # 같은 프로세스 내 동시 요청 직렬화
        self.issue_count = 0                 # 계측용 (테스트에서 발급 횟수 확인)

    # ── 캐시 파일 ────────────────────────────────────────────────────────────
    def _read(self) -> Optional[Dict[str, Any]]:
        """캐시 읽기 — 파손·부재는 None (예외 전파 금지)"""
        try:
            with open(self._cache_path, "r", encoding="utf-8") as f:
                entry = json.load(f)
        except (OSError, ValueError):
            return None
        if not isinstance(entry, dict):
            return None
        if not entry.get("access_token") or entry.get("expires_at") is None:
            return None
        return entry

    def _write(self, entry: Dict[str, Any]) -> None:
        atomic_write_json(self._cache_path, entry)
        # ponytail: replace 후 chmod — tmp 가 umask 모드로 만들어지므로 짧은 순간 0644 다.
        # 단일 사용자 서버라 여기까지만 한다. 다중 사용자면 tmp 생성 시점에 0600 을 줘야 한다.
        with contextlib.suppress(OSError):
            os.chmod(self._cache_path, 0o600)

    def _usable(self, entry: Optional[Dict[str, Any]], failed_token: Optional[str]) -> Optional[str]:
        """캐시 항목이 지금 쓸 수 있는 토큰이면 그 토큰을 반환

        - 만료 `refresh_margin` 이내면 쓰지 않는다 (선제 갱신)
        - 방금 실패한 토큰은 만료 시각이 남아 있어도 절대 반환하지 않는다
        """
        if entry is None:
            return None
        token = entry.get("access_token")
        if not token:
            return None
        if failed_token is not None and token == failed_token:
            return None
        try:
            expires_at = float(entry["expires_at"])
        except (TypeError, ValueError, KeyError):
            return None
        if expires_at - time.time() <= self._refresh_margin:
            return None
        return str(token)

    # ── 파일 락 ──────────────────────────────────────────────────────────────
    @contextlib.asynccontextmanager
    async def _file_lock(self):
        """프로세스 간 배타 락 — 이벤트 루프를 막지 않도록 논블로킹 폴링"""
        self._lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self._lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            deadline = time.monotonic() + self._lock_timeout
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise TossLockTimeout("토스 토큰 파일 락 획득 실패")
                    await asyncio.sleep(LOCK_POLL_SEC)
            try:
                yield
            finally:
                with contextlib.suppress(OSError):
                    fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    # ── 발급 ─────────────────────────────────────────────────────────────────
    async def _issue(self) -> str:
        try:
            body = await self._fetcher(self._client_id, self._client_secret)
        except TossTokenError:
            raise
        except Exception as e:   # aiohttp/asyncio 예외가 그대로 새면 서킷 계수·원장 기록을 놓친다
            raise TossTokenError(f"토큰 발급 전송 실패: {type(e).__name__}") from e
        token = (body or {}).get("access_token")
        if not token:
            raise TossTokenError("토큰 발급 응답에 access_token 이 없음")
        try:
            expires_in = float(body.get("expires_in"))
        except (TypeError, ValueError):
            expires_in = 0.0
        if expires_in <= 0:
            raise TossTokenError(f"토큰 만료시간이 비정상: expires_in={body.get('expires_in')!r}")
        if expires_in <= self._refresh_margin:
            # 마진보다 짧게 발급되면 캐시가 절대 '사용 가능'이 못 돼 호출마다 재발급 → 상호 무효화 루프.
            # 마진을 만료의 절반으로 줄여 캐시 히트를 보장한다(설계 §4.2).
            logger.warning(
                f"[토스] expires_in={expires_in:.0f}s ≤ 갱신마진 {self._refresh_margin:.0f}s — 마진을 {expires_in / 2:.0f}s 로 축소"
            )
            self._refresh_margin = expires_in / 2
        now = time.time()
        self._write({
            "access_token": str(token),
            "issued_at": now,
            "expires_at": now + expires_in,
        })
        self.issue_count += 1
        logger.info(
            f"[토스] 토큰 발급 #{self.issue_count} {_mask(str(token))} "
            f"만료 {expires_in / 3600:.1f}시간 뒤"
        )
        return str(token)

    async def _acquire_locked(self, failed_token: Optional[str]) -> str:
        """파일 락 안에서 double-check 후 필요할 때만 1회 발급"""
        try:
            async with self._file_lock():
                cached = self._usable(self._read(), failed_token)
                if cached is not None:
                    logger.info("[토스] 락 안 재확인 — 다른 프로세스가 이미 갱신, 발급 생략")
                    return cached
                return await self._issue()
        except TossLockTimeout:
            # 설계 §4.2: 락을 못 잡으면 발급하지 않고 캐시를 다시 읽는다
            cached = self._usable(self._read(), failed_token)
            if cached is not None:
                logger.warning("[토스] 토큰 락 대기 초과 — 캐시 토큰 재사용")
                return cached
            raise TossTokenError("토큰 락 대기 초과 + 쓸 수 있는 캐시 토큰 없음")

    # ── 공개 API ─────────────────────────────────────────────────────────────
    async def get_token(self) -> str:
        """유효 토큰 반환 (만료 30분 전이면 갱신)"""
        cached = self._usable(self._read(), None)
        if cached is not None:
            return cached
        async with self._inproc_lock:
            cached = self._usable(self._read(), None)   # 같은 프로세스 double-check
            if cached is not None:
                return cached
            return await self._acquire_locked(None)

    async def refresh_after_error(self, failed_token: str, code: str) -> str:
        """401 토큰 에러 후 새 토큰 확보

        `token-revoked` 는 다른 프로세스가 새 토큰을 발급했다는 뜻이므로 **재발급이 아니라
        캐시 재읽기**가 먼저다. 캐시가 실패한 토큰과 같을 때만 락을 잡고 1회 발급한다.
        `expired-token` / `invalid-token` 도 같은 순서를 지키면 손해가 없다
        (그 사이 남이 갱신했으면 재발급이 남의 토큰을 끊는다).
        """
        async with self._inproc_lock:
            cached = self._usable(self._read(), failed_token)
            if cached is not None:
                logger.info(f"[토스] {code} — 캐시에 다른 토큰 존재, 재발급 없이 재시도")
                return cached
            if code == REVOKED_CODE:
                logger.warning(f"[토스] {code} — 캐시 토큰이 실패 토큰과 동일, 락 획득 후 1회 발급")
            else:
                logger.info(f"[토스] {code} — 토큰 재발급 경로 진입")
            return await self._acquire_locked(failed_token)
