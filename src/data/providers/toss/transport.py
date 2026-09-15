"""고정 origin의 조회 전용 전송 경계. 인증 발급 POST는 제공하지 않는다."""

from __future__ import annotations

import asyncio
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime

import aiohttp


ORIGIN = "https://openapi.tossinvest.com"
ENDPOINT_GROUPS = {
    "/api/v1/prices": "MARKET_DATA",
    "/api/v1/candles": "MARKET_DATA_CHART",
    "/api/v1/market-calendar/KR": "MARKET_INFO",
}
SAFE_CODES = frozenset({
    "disabled", "reader_only", "closed", "not_started", "invalid_request",
    "invalid_params", "malformed_response", "unsupported", "forbidden",
    "http_error", "redirect_rejected", "network_error", "auth_unavailable",
    "timeout", "retry_exhausted", "page_exhausted", "sender_busy", "unsafe_lock",
    "circuit_open", "rate_limited", "server_error", "unknown",
})
AUTH_CODES = frozenset({"invalid-token", "expired-token", "token-revoked", "login-user-not-found"})


class TossRequestError(Exception):
    """임의 원문이 아닌 고정 사유만 외부에 전달한다."""

    def __init__(self, code: str):
        self.code = code if isinstance(code, str) and code in SAFE_CODES else "unknown"
        super().__init__(self.code)


@dataclass(frozen=True)
class HttpResponse:
    status: int
    headers: Mapping = field(repr=False)
    body: object = field(repr=False)


def validate_request(method, path, params):
    """정규화해서 허용하지 않고, 정확히 승인된 경로만 받는다."""
    if method != "GET" or not isinstance(path, str) or path not in ENDPOINT_GROUPS:
        raise TossRequestError("invalid_request")
    if not isinstance(params, Mapping) or any(not isinstance(k, str) for k in params):
        raise TossRequestError("invalid_params")
    result = dict(params)
    if path == "/api/v1/prices":
        if set(result) != {"symbols"} or not isinstance(result["symbols"], str):
            raise TossRequestError("invalid_params")
        symbols = result["symbols"].split(",")
        if not 1 <= len(symbols) <= 200 or any(not _symbol(s) for s in symbols):
            raise TossRequestError("invalid_params")
    elif path == "/api/v1/candles":
        if not {"symbol", "interval", "count", "adjusted"} <= result.keys():
            raise TossRequestError("invalid_params")
        if result.keys() - {"symbol", "interval", "count", "adjusted", "before"}:
            raise TossRequestError("invalid_params")
        if (not _symbol(result["symbol"]) or result["interval"] != "1d"
                or type(result["count"]) is not int or not 1 <= result["count"] <= 200
                or type(result["adjusted"]) is not bool):
            raise TossRequestError("invalid_params")
        if "before" in result:
            cursor = result["before"]
            if (not isinstance(cursor, str) or not 1 <= len(cursor) <= 128
                    or re.fullmatch(r"[A-Za-z0-9:+.\-TZ]+", cursor) is None):
                raise TossRequestError("invalid_params")
            try:
                parsed = datetime.fromisoformat(cursor)
            except ValueError:
                raise TossRequestError("invalid_params") from None
            if "T" not in cursor or parsed.tzinfo is None or parsed.utcoffset() is None:
                raise TossRequestError("invalid_params")
    else:
        if result.keys() - {"date"}:
            raise TossRequestError("invalid_params")
        if "date" in result:
            value = result["date"]
            if not isinstance(value, str) or re.fullmatch(r"\d{4}-\d{2}-\d{2}", value) is None:
                raise TossRequestError("invalid_params")
            try:
                date.fromisoformat(value)
            except ValueError:
                raise TossRequestError("invalid_params") from None
    return result


def _symbol(value):
    return isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9]{1,20}", value) is not None


async def _await_cleanup(task):
    """반복 취소를 받아도 공유 정리를 끝까지 보호한 뒤 호출자 취소를 전파한다."""
    cancelled = False
    while True:
        try:
            result = await asyncio.shield(task)
            break
        except asyncio.CancelledError:
            if task.cancelled():
                raise
            cancelled = True
    if cancelled:
        raise asyncio.CancelledError
    return result


class AiohttpTransport:
    """세션은 첫 승인 조회에서 생성한다. factory는 오프라인 검증용 주입점이다."""

    def __init__(self, *, session_factory=None):
        self._factory = session_factory
        self._session = None
        self._closed = False
        self._closing = None

    async def request(self, method, path, *, params, headers, timeout):
        params = validate_request(method, path, params)
        if (not isinstance(headers, Mapping) or set(headers) != {"Authorization"}
                or not isinstance(headers["Authorization"], str)
                or re.fullmatch(r"Bearer [\x21-\x7e]+", headers["Authorization"]) is None):
            raise TossRequestError("invalid_request")
        if (type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0):
            raise TossRequestError("timeout")
        if self._closed:
            raise TossRequestError("closed")
        # aiohttp는 bool query를 지원하지 않으므로 HTTP 직전에만 직렬화한다.
        query = {k: str(v).lower() if isinstance(v, bool) else v for k, v in params.items()}
        try:
            if self._session is None:
                self._session = (self._factory() if self._factory else
                                 aiohttp.ClientSession(trust_env=False, cookie_jar=aiohttp.DummyCookieJar()))
                # 확인한 aiohttp 3.13.5의 keepalive GET 내부 재시도를 막는다.
                # 이 훅이 없는 버전/세션은 지원을 추측하지 않고 송신 전에 거부한다.
                if not hasattr(self._session, "_retry_connection"):
                    raise TossRequestError("unsupported")
                self._session._retry_connection = False
            if getattr(self._session, "_retry_connection", None) is not False:
                raise TossRequestError("unsupported")
            async with self._session.request(
                method, ORIGIN + path, params=query, headers=dict(headers),
                timeout=aiohttp.ClientTimeout(total=timeout), ssl=True, allow_redirects=False,
            ) as response:
                status, response_headers = response.status, dict(response.headers)
                if 300 <= status < 400:
                    return HttpResponse(status, response_headers, None)
                # redirect/4xx/5xx는 JSON이 아니어도 상태 자체로 분류한다.
                try:
                    body = await response.json()
                except (ValueError, aiohttp.ContentTypeError):
                    if status == 200:
                        raise TossRequestError("malformed_response") from None
                    body = None
                return HttpResponse(status, response_headers, body)
        except asyncio.CancelledError:
            raise
        except TossRequestError:
            raise
        except TimeoutError:
            raise TossRequestError("timeout") from None
        except Exception:
            raise TossRequestError("network_error") from None

    async def close(self):
        self._closed = True
        if (self._closing is None or (self._closing.done() and
                (self._closing.cancelled() or self._closing.exception() is not None))):
            self._closing = asyncio.create_task(self._close())
        await _await_cleanup(self._closing)

    async def _close(self):
        if self._session is not None:
            session = self._session
            try:
                await session.close()
            except asyncio.CancelledError:
                raise
            except Exception:
                raise TossRequestError("network_error") from None
            self._session = None
