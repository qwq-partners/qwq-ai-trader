"""기본 OFF 조회 client. sender 역할은 토큰 issuer/reader 역할과 독립이다."""

from __future__ import annotations

import asyncio
import fcntl
import inspect
import os
import re
import stat
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Protocol

from .rate_limit import RequestBudget, positive_number
from .transport import (
    AUTH_CODES, ENDPOINT_GROUPS, HttpResponse, TossRequestError, validate_request,
    _await_cleanup,
)


class TokenProvider(Protocol):
    """폐기 관측은 동기·지속·발급0이며, 조회/복구와 함께 필수 계약이다."""

    def observe_revocation(self, failed_token: str) -> None: ...

    async def get_token(self, *, deadline: float) -> str: ...

    async def recover(self, error_code: str, failed_token: str, *, deadline: float) -> str: ...


class TossClient:
    def __init__(self, *, transport, tokens: TokenProvider, limiter, enabled=False, role="reader",
                 sender_lock_path: Path, circuit_failure_threshold: int,
                 circuit_open_seconds: float, clock=time.monotonic):
        if type(enabled) is not bool or role not in ("sender", "reader"):
            raise ValueError("invalid_role")
        if (type(circuit_failure_threshold) is not int or circuit_failure_threshold < 1
                or not positive_number(circuit_open_seconds)):
            raise ValueError("invalid_circuit_policy")
        self.transport, self.tokens, self.limiter = transport, tokens, limiter
        self.enabled, self.role = enabled, role
        self._path = Path(sender_lock_path)
        self._threshold, self._open_seconds = circuit_failure_threshold, circuit_open_seconds
        self._clock, self._fd = clock, None
        self._closed = False
        self._active = set()
        self._closing = None
        self._states = {group: dict(failures=0, open_until=None, probe=False,
                                   last_success=None, reason=None)
                        for group in ENDPOINT_GROUPS.values()}
        self._epochs = dict.fromkeys(self._states, 0)
        self._probes = dict.fromkeys(self._states)

    @property
    def health(self):
        return {group: dict(state) for group, state in self._states.items()}

    async def __aenter__(self):
        await self.start()
        return self

    async def __aexit__(self, *args):
        await self.close()

    async def start(self):
        if not self.enabled or self.role == "reader":
            return
        if self._closed:
            raise TossRequestError("closed")
        if self._fd is not None:
            return
        fd = None
        try:
            # 상위 symlink도 거부한다. 기존 디렉터리나 파일의 권한은 변경하지 않는다.
            for parent in self._path.absolute().parents:
                if parent.is_symlink():
                    raise TossRequestError("unsafe_lock")
            fd = os.open(self._path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
                raise TossRequestError("unsafe_lock")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise TossRequestError("sender_busy") from None
            visible = self._path.lstat()
            if (visible.st_dev, visible.st_ino) != (info.st_dev, info.st_ino):
                raise TossRequestError("unsafe_lock")
            self._fd, fd = fd, None
        except TossRequestError:
            raise
        except OSError:
            raise TossRequestError("unsafe_lock") from None
        finally:
            if fd is not None:
                os.close(fd)

    async def close(self):
        if not self.enabled or self.role == "reader":
            return
        if (self._closing is None or (self._closing.done() and
                (self._closing.cancelled() or self._closing.exception() is not None))):
            self._closed = True
            self._closing = asyncio.create_task(self._close())
        await _await_cleanup(self._closing)

    async def _close(self):
        try:
            active = tuple(self._active)
            for task in active:
                task.cancel()
            if active:
                await asyncio.gather(*active, return_exceptions=True)
            await self.transport.close()
        except asyncio.CancelledError:
            raise
        except Exception:
            raise TossRequestError("network_error") from None
        # 실패/취소 시에는 세션 정리가 완료되지 않았으므로 송신 소유권을 유지한다.
        if self._fd is not None:
            fd, self._fd = self._fd, None
            os.close(fd)

    async def get(self, path, *, params, budget):
        return await self.request("GET", path, params=params, budget=budget)

    async def request(self, method, path, *, params, budget, headers=None):
        if not self.enabled:
            raise TossRequestError("disabled")
        if self.role != "sender":
            raise TossRequestError("reader_only")
        params = validate_request(method, path, params)
        if headers:
            raise TossRequestError("invalid_request")
        if self._closed:
            raise TossRequestError("closed")
        group = ENDPOINT_GROUPS[path]
        budget.consume_page()
        task = asyncio.create_task(self._execute(path, params, budget, group))
        self._active.add(task)
        try:
            return await task
        finally:
            self._active.discard(task)

    async def _execute(self, path, params, budget, group):
        probe, epoch = None, None
        try:
            async with asyncio.timeout(budget.remaining()):
                await self.start()
                state = self._states[group]
                if state["open_until"] is not None:
                    if self._clock() < state["open_until"] or state["probe"]:
                        raise TossRequestError("circuit_open")
                    probe = object()
                    self._probes[group] = probe
                    state["probe"] = True
                    self._epochs[group] += 1
                epoch = self._epochs[group]
                body = await self._get(path, params, budget, group)
                # 회로가 열린 뒤 도착한 이전 요청은 새 회로/검사 요청을 갱신하지 못한다.
                if self._epochs[group] == epoch:
                    state.update(failures=0, open_until=None, last_success=self._clock(), reason=None)
                    if probe is not None:
                        self._epochs[group] += 1
                return body
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            self._failure(group, "timeout", epoch)
            raise TossRequestError("timeout") from None
        except TossRequestError as exc:
            if exc.code not in ("circuit_open", "sender_busy", "unsafe_lock"):
                self._failure(group, exc.code, epoch)
            raise TossRequestError(exc.code) from None
        except Exception:
            self._failure(group, "auth_unavailable", epoch)
            raise TossRequestError("auth_unavailable") from None
        finally:
            if probe is not None and self._probes[group] is probe:
                self._probes[group] = None
                self._states[group]["probe"] = False

    def _failure(self, group, reason, epoch):
        if epoch is None or self._epochs[group] != epoch:
            return
        state = self._states[group]
        state["failures"] += 1
        state["reason"] = reason
        if state["failures"] >= self._threshold:
            state["open_until"] = self._clock() + self._open_seconds
            self._epochs[group] += 1

    async def _get(self, path, params, budget, group):
        observe_revocation = getattr(self.tokens, "observe_revocation", None)
        if not callable(observe_revocation) or inspect.iscoroutinefunction(observe_revocation):
            raise TossRequestError("auth_unavailable")
        token = await self.tokens.get_token(deadline=budget.deadline)
        while True:
            if (not isinstance(token, str) or len(token) > 16384
                    or re.fullmatch(r"[\x21-\x7e]+", token) is None):
                raise TossRequestError("auth_unavailable")
            await self.limiter.acquire(group, budget)
            network_failure = False
            try:
                response = await self.transport.request(
                    "GET", path, params=params, headers={"Authorization": "Bearer " + token},
                    timeout=budget.remaining())
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if isinstance(exc, TossRequestError) and exc.code not in ("network_error", "timeout"):
                    raise TossRequestError(exc.code) from None
                network_failure = True
            if network_failure:
                budget.consume_retry()
                continue
            if not isinstance(response, HttpResponse) or type(response.status) is not int:
                raise TossRequestError("malformed_response")
            if not isinstance(response.headers, Mapping):
                raise TossRequestError("malformed_response")
            status, body = response.status, response.body
            if isinstance(body, Mapping) and "result" in body and "error" in body:
                raise TossRequestError("malformed_response")
            if status == 401 and _error_code(body) == "token-revoked":
                # 응답을 확인한 뒤 첫 await/deadline 검사 전에 폐기 관측을 지속 게시한다.
                observe_revocation(token)
            await self.limiter.observe(group, response.headers, status=status)
            if 300 <= status < 400:
                raise TossRequestError("redirect_rejected")
            if status == 403:
                raise TossRequestError("forbidden")
            if status == 401:
                code = _error_code(body)
                if code not in ("expired-token", "token-revoked"):
                    raise TossRequestError("auth_unavailable")
                if code == "token-revoked":
                    # 폐기 관측의 지속 처리는 추가 HTTP 송신 허가와 독립이다.
                    # recover는 동일 deadline 안에서 발급 없이 새 캐시 확인/차단만 한다.
                    token = await self.tokens.recover(code, token, deadline=budget.deadline)
                    budget.consume_retry()
                else:
                    # expired 복구는 발급할 수 있으므로 먼저 재시도 허가를 확보한다.
                    budget.consume_retry()
                    token = await self.tokens.recover(code, token, deadline=budget.deadline)
                continue
            if status == 429 or 500 <= status < 600:
                budget.consume_retry()
                continue
            if status != 200:
                raise TossRequestError("http_error")
            _validate_body(path, body)
            return body


def _error_code(body):
    if isinstance(body, Mapping) and isinstance(body.get("error"), Mapping):
        code = body["error"].get("code")
        if isinstance(code, str) and code in AUTH_CODES | {"unsupported"}:
            return code
    return "unknown"


def _validate_body(path, body):
    if not isinstance(body, Mapping):
        raise TossRequestError("malformed_response")
    if "error" in body and "result" in body:
        raise TossRequestError("malformed_response")
    if "error" in body:
        code = _error_code(body)
        raise TossRequestError("auth_unavailable" if code in AUTH_CODES else
                               "unsupported" if code == "unsupported" else "http_error")
    result = body.get("result")
    if path == "/api/v1/prices":
        valid = isinstance(result, list)
    elif path == "/api/v1/candles":
        valid = (isinstance(result, Mapping) and isinstance(result.get("candles"), list)
                 and (result.get("nextBefore") is None or isinstance(result["nextBefore"], str)))
    else:
        valid = isinstance(result, Mapping) and {
            "today", "previousBusinessDay", "nextBusinessDay"} <= result.keys()
    if not valid:
        raise TossRequestError("malformed_response")
