"""발급 판단/복구를 하지 않는 승인된 OAuth 단일 POST 어댑터."""

from __future__ import annotations

import asyncio
import math
import os
import re
import time
from dataclasses import dataclass, field

import aiohttp

from .http_body import BodyError, BodyLimits, read_json_bounded
from .token_store import MAX_LIFETIME
from .transport import ORIGIN, TossRequestError, _await_cleanup


@dataclass(frozen=True, repr=False)
class Credentials:
    client_id: str = field(repr=False)
    client_secret: str = field(repr=False)

    def __post_init__(self):
        for value in (self.client_id, self.client_secret):
            if not isinstance(value, str) or re.fullmatch(r"[\x21-\x7e]{1,4096}", value) is None:
                raise TossRequestError("auth_unavailable")

    def __repr__(self):
        return "Credentials(<redacted>)"


def environment_credentials(*, environ=None):
    """호출 시에만 키를 읽는다. 파일/.env 로딩 및 생성자 부작용은 없다."""
    try:
        source = os.environ if environ is None else environ
        return Credentials(source["TOSS_CLIENT_ID"], source["TOSS_CLIENT_SECRET"])
    except Exception:
        raise TossRequestError("auth_unavailable") from None


class OAuthIssuer:
    def __init__(self, *, credential_loader, authorize, limits, max_issues,
                 session_factory=None, clock=time.monotonic):
        if (not callable(credential_loader) or not callable(authorize)
                or not isinstance(limits, BodyLimits)
                or type(max_issues) is not int or max_issues < 1):
            raise TossRequestError("invalid_request")
        self._loader, self._authorize = credential_loader, authorize
        self._limits, self._max_issues = limits, max_issues
        self._factory, self._clock = session_factory, clock
        self._session = self._credentials = self._closing = None
        self._closed, self._issues = False, 0

    def can_issue(self):
        """이 worker 수명의 남은 발급 예산. I/O·예약·승인 검사는 하지 않는다."""
        return not self._closed and self._issues < self._max_issues

    def _check(self, deadline):
        if type(deadline) not in (int, float) or not math.isfinite(deadline):
            raise TossRequestError("timeout")
        remaining = deadline - self._clock()
        if remaining <= 0:
            raise TossRequestError("timeout")
        return remaining

    def _permit(self, operation, deadline):
        self._check(deadline)
        try:
            bounded = self._authorize(operation, deadline=deadline)
            self._check(bounded)
            return min(deadline, bounded)
        except Exception:
            raise TossRequestError("auth_unavailable") from None

    async def issue(self, *, operation, deadline):
        if self._closed:
            raise TossRequestError("closed")
        if operation not in {"renewal", "bootstrap"} or self._issues >= self._max_issues:
            raise TossRequestError("auth_unavailable")
        deadline = self._permit(operation, deadline)
        try:
            if self._credentials is None:
                self._credentials = self._loader()
                if not isinstance(self._credentials, Credentials):
                    raise TossRequestError("auth_unavailable")
            if self._session is None:
                self._session = self._factory() if self._factory else aiohttp.ClientSession(
                    trust_env=False, cookie_jar=aiohttp.DummyCookieJar(), auto_decompress=False)
                if not hasattr(self._session, "_retry_connection"):
                    raise TossRequestError("unsupported")
                self._session._retry_connection = False
            if getattr(self._session, "_retry_connection", None) is not False:
                raise TossRequestError("unsupported")
            data = dict(grant_type="client_credentials", client_id=self._credentials.client_id,
                        client_secret=self._credentials.client_secret)
            deadline = self._permit(operation, deadline)
            remaining = self._check(deadline)
            self._issues += 1
            # 재전송/백그라운드 발급 없이 현재 task가 POST와 취소를 소유한다.
            async with asyncio.timeout(remaining):
                async with self._session.request("POST", ORIGIN + "/oauth2/token", data=data,
                    headers={"Content-Type": "application/x-www-form-urlencoded", "Accept-Encoding": "identity"},
                    timeout=aiohttp.ClientTimeout(total=remaining), ssl=True,
                    allow_redirects=False, auto_decompress=False) as response:
                    if response.status != 200:
                        raise TossRequestError("redirect_rejected" if 300 <= response.status < 400 else "auth_unavailable")
                    result = await read_json_bounded(response, limits=self._limits,
                                                     deadline=deadline, clock=self._clock)
            if not isinstance(result, dict) or set(result) != {"access_token", "token_type", "expires_in"}:
                raise TossRequestError("malformed_response")
            if (not isinstance(result["access_token"], str)
                    or re.fullmatch(r"[\x21-\x7e]{1,16384}", result["access_token"]) is None
                    or result["token_type"] != "Bearer"
                    or type(result["expires_in"]) is not int or not 0 < result["expires_in"] <= MAX_LIFETIME):
                raise TossRequestError("malformed_response")
            self._check(deadline)
            return result
        except asyncio.CancelledError:
            raise
        except TossRequestError as exc:
            raise TossRequestError(exc.code) from None
        except BodyError as exc:
            raise TossRequestError("timeout" if exc.code == "timeout" else "malformed_response") from None
        except TimeoutError:
            raise TossRequestError("timeout") from None
        except Exception:
            raise TossRequestError("auth_unavailable") from None

    async def close(self):
        self._closed = True
        if (self._closing is None or (self._closing.done() and
                (self._closing.cancelled() or self._closing.exception() is not None))):
            self._closing = asyncio.create_task(self._close())
        await _await_cleanup(self._closing)

    async def _close(self):
        self._credentials = None
        if self._session is not None:
            try:
                await self._session.close()
            except asyncio.CancelledError:
                raise
            except Exception:
                raise TossRequestError("network_error") from None
            self._session = None
