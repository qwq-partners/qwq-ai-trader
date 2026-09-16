"""토스 Open API HTTP 클라이언트 (설계 §4.2·§4.3·§6.6)

토큰 주입 → 그룹 리미터 → 429/401 처리 → 에러 envelope 파싱 → 서킷 브레이커.
읽기 전용 GET 만 제공한다. **주문·계좌 엔드포인트는 이 클라이언트로 부르지 않는다**
(설계 §8: 제공되지만 쓰지 않는다).
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import Any, Dict, Mapping, Optional

import aiohttp
from loguru import logger

from . import rate_limit
from .token import DEFAULT_BASE_URL, TOKEN_ERROR_CODES, TossTokenCache, TossTokenError

DEFAULT_TIMEOUT_SEC = 5
CIRCUIT_FAIL_THRESHOLD = 5    # 연속 실패 N회 → 개방
CIRCUIT_OPEN_SEC = 60.0       # 개방 유지 시간 (이 동안 호출 자체를 건너뛴다)


class TossAPIError(RuntimeError):
    """토스 API 호출 실패 (HTTP 오류·전송 오류·서킷 개방)"""

    def __init__(
        self,
        message: str,
        *,
        status: Optional[int] = None,
        code: Optional[str] = None,
        request_id: Optional[str] = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.request_id = request_id


class TossCircuitOpen(TossAPIError):
    """서킷 개방 — 호출하지 않고 즉시 실패"""


def _error_fields(body: Any) -> Dict[str, Optional[str]]:
    """에러 envelope `{"error": {"code", "message", "requestId"}}` 파싱"""
    if not isinstance(body, dict):
        return {"code": None, "message": None, "request_id": None}
    err = body.get("error")
    if isinstance(err, str):          # OAuth2 표준 포맷 (토큰 엔드포인트)
        return {"code": err, "message": body.get("error_description"), "request_id": None}
    if not isinstance(err, dict):
        return {"code": None, "message": None, "request_id": None}
    return {
        "code": err.get("code"),
        "message": err.get("message"),
        "request_id": err.get("requestId"),
    }


def _query(params: Optional[Mapping[str, Any]]) -> Dict[str, str]:
    """aiohttp 쿼리 값은 문자열이어야 한다 — bool 은 토스 규약대로 true/false"""
    out: Dict[str, str] = {}
    for key, value in (params or {}).items():
        if value is None:
            continue
        if isinstance(value, bool):
            out[key] = "true" if value else "false"
        else:
            out[key] = str(value)
    return out


class TossClient:
    def __init__(
        self,
        client_id: str,
        client_secret: str,
        *,
        token_cache: Optional[TossTokenCache] = None,
        session: Optional[aiohttp.ClientSession] = None,
        timeout_sec: int = DEFAULT_TIMEOUT_SEC,
        base_url: str = DEFAULT_BASE_URL,
        fail_threshold: int = CIRCUIT_FAIL_THRESHOLD,
        circuit_open_sec: float = CIRCUIT_OPEN_SEC,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout_sec = timeout_sec
        self._session = session
        self._owns_session = session is None
        self._token_cache = token_cache if token_cache is not None else TossTokenCache(
            client_id, client_secret
        )
        self._fail_threshold = fail_threshold
        self._circuit_open_sec = circuit_open_sec
        self._consecutive_failures = 0
        self._open_until = 0.0
        self._half_open_trial = False   # 반열림 중 시험 호출 1건만 통과

    # ── 세션 ────────────────────────────────────────────────────────────────
    def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self._timeout_sec)
            )
            self._owns_session = True
        return self._session

    async def close(self) -> None:
        if self._session is not None and self._owns_session and not self._session.closed:
            await self._session.close()
        self._session = None

    # ── 서킷 브레이커 (설계 §6.6) ────────────────────────────────────────────
    def circuit_state(self) -> Dict[str, Any]:
        now = time.monotonic()
        is_open = now < self._open_until
        return {
            "open": is_open,
            "failures": self._consecutive_failures,
            "remaining_sec": round(self._open_until - now, 2) if is_open else 0.0,
        }

    def _note_success(self) -> None:
        self._consecutive_failures = 0
        self._open_until = 0.0
        self._half_open_trial = False

    def _note_failure(self, reason: str) -> None:
        self._consecutive_failures += 1
        self._half_open_trial = False
        if self._consecutive_failures >= self._fail_threshold and self._open_until <= time.monotonic():
            self._open_until = time.monotonic() + self._circuit_open_sec
            logger.warning(
                f"[토스] 서킷 개방 — 연속 실패 {self._consecutive_failures}회 ({reason}), "
                f"{self._circuit_open_sec:.0f}초간 호출 중단"
            )

    def _check_circuit(self, path: str) -> None:
        if time.monotonic() < self._open_until:
            raise TossCircuitOpen(f"토스 서킷 개방 중 — 호출 생략: {path}")
        if self._open_until and time.monotonic() >= self._open_until:
            # 개방 시간이 지나면 반열림: 시험 호출 1건만 통과시키고 나머지는 계속 거부
            if self._half_open_trial:
                raise TossCircuitOpen(f"토스 서킷 반열림 — 시험 호출 진행 중, 생략: {path}")
            self._half_open_trial = True
            self._open_until = 0.0
            self._consecutive_failures = self._fail_threshold - 1
            logger.info("[토스] 서킷 반열림 — 1회 시험 호출")

    # ── 조회 ────────────────────────────────────────────────────────────────
    async def get(
        self,
        path: str,
        params: Optional[Mapping[str, Any]] = None,
        *,
        group: str,
    ) -> Dict[str, Any]:
        """GET 호출 — 성공 응답 envelope(`{"result": ...}`) 전체를 반환

        실패는 `TossAPIError` 로 올린다 (Phase 1 은 실패를 기록해야 하므로 조용히 삼키지 않는다).
        """
        self._check_circuit(path)
        url = f"{self._base_url}{path}"
        query = _query(params)
        token_retried = False

        for attempt in range(rate_limit.MAX_RETRIES + 1):
            try:
                token = await self._token_cache.get_token()
            except TossTokenError as exc:
                self._note_failure("토큰 발급 실패")
                raise TossAPIError(f"토스 토큰 확보 실패: {exc}") from exc
            await rate_limit.acquire(group)   # 토큰 확보(최대 락 대기 15초) 뒤에 슬롯을 소비해야 전송 시각과 맞는다

            headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
            try:
                async with self._get_session().get(url, params=query, headers=headers) as resp:
                    status = resp.status
                    resp_headers = resp.headers
                    try:
                        body = await resp.json(content_type=None)
                    except ValueError:   # 게이트웨이 HTML 5xx 등 비-JSON — status 분기가 그대로 동작하게
                        body = None
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                self._note_failure(type(exc).__name__)
                raise TossAPIError(f"토스 전송 실패 {path}: {exc}") from exc

            rate_limit.note_limit_header(group, resp_headers)

            if status == 200:
                self._note_success()
                return body if isinstance(body, dict) else {"result": body}

            fields = _error_fields(body)
            code = fields["code"]

            if status == 429 and attempt < rate_limit.MAX_RETRIES:
                delay = rate_limit.retry_delay(attempt, rate_limit.parse_retry_after(resp_headers))
                logger.warning(f"[토스] 429 {code or ''} {path} — {delay:.2f}초 뒤 재시도")
                await asyncio.sleep(delay)
                continue

            if status == 401 and code in TOKEN_ERROR_CODES and not token_retried:
                token_retried = True
                await self._token_cache.refresh_after_error(token, str(code))
                continue

            if status >= 500:
                self._note_failure(f"HTTP {status}")
            raise TossAPIError(
                f"토스 API 실패 {path}: HTTP {status} code={code} msg={fields['message']}",
                status=status,
                code=code,
                request_id=fields["request_id"],
            )

        self._note_failure("재시도 소진")
        raise TossAPIError(f"토스 API 재시도 소진 {path}", status=429)


def create_toss_client(
    *,
    session: Optional[aiohttp.ClientSession] = None,
    token_cache: Optional[TossTokenCache] = None,
    env: Optional[Mapping[str, str]] = None,
    **kwargs: Any,
) -> Optional[TossClient]:
    """환경변수 기반 팩토리 — `TOSS_API=0` 이거나 자격증명이 없으면 None

    None 반환은 "토스 비활성"을 뜻한다. 호출부는 None 을 정상 상태로 다뤄야 한다.
    """
    env = os.environ if env is None else env
    if str(env.get("TOSS_API", "1")).strip() == "0":
        logger.info("[토스] TOSS_API=0 — 비활성")
        return None
    client_id = (env.get("TOSS_CLIENT_ID") or "").strip()
    client_secret = (env.get("TOSS_CLIENT_SECRET") or "").strip()
    if not client_id or not client_secret:
        logger.warning("[토스] TOSS_CLIENT_ID/TOSS_CLIENT_SECRET 미설정 — 비활성")
        return None
    return TossClient(
        client_id, client_secret, session=session, token_cache=token_cache, **kwargs
    )
