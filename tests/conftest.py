"""테스트 격리 가드 (2026-09-15, T10 §0 안전 경계)

이 저장소의 테스트는 운영 봇과 같은 호스트에서 돈다. 모듈 상수 30여 개가
``Path.home()/.cache/ai_trader`` (운영 캐시·원장·상태) 를 가리키고, 공급자는
KIS/Yahoo/LLM 으로 나간다. worktree 격리만으로는 부족하므로 conftest 가 import
시점부터 다음을 강제한다.

1. 네트워크 — 루프백(127.0.0.1/::1/localhost)·UNIX 소켓 외 모든 connect/getaddrinfo 차단 +
   curl_cffi(yfinance 백엔드, C 레벨 curl 이라 socket 패치를 우회) 의 perform/request 차단.
2. 운영 상태 파일 — ``~/.cache/ai_trader``, ``~/.cache/ai_trader_us``, ``~/.gh_token``,
   운영 체크아웃의 ``.env``/``logs``/``results`` 에 대한 open/stat/scandir/mkdir/
   rename/unlink 등을 차단 (``PermissionError`` 로 크게 실패).
3. HOME 등 시스템 환경변수는 **덮어쓰지 않는다** — 테스트가 tmp_path 를 명시 주입한다.

차단 시도는 모두 기록해 세션 요약에 출력한다 (프로덕션 코드가 삼킨 예외도 보이게).
해제 스위치는 두지 않는다 — 실제 상태가 필요한 검증은 pytest 밖에서 한다.
"""

from __future__ import annotations

import builtins
import errno
import io
import os
import socket
from pathlib import Path
from typing import List

# ── 차단 대상 (import 시점에 실제 HOME 으로 계산 — 이후 monkeypatch 무관) ──────────
_HOME = Path.home()
_PROD_ROOT = Path(os.environ.get("QWQ_PROD_ROOT", "/home/ubuntu/projects/qwq-ai-trader"))
BLOCKED_PATH_PREFIXES = tuple(
    str(p) for p in (
        _HOME / ".cache" / "ai_trader",
        _HOME / ".cache" / "ai_trader_us",
        _HOME / ".gh_token",
        _PROD_ROOT / ".env",
        _PROD_ROOT / "logs",
        _PROD_ROOT / "results",
    )
)
_LOOPBACK_HOSTS = {"", "localhost", "127.0.0.1", "::1", "0.0.0.0"}

VIOLATIONS: List[str] = []
_CURRENT_TEST = ["<collection>"]


class IsolationViolation(PermissionError):
    """운영 상태 파일 접근 시도 — 테스트는 전용 임시 경로를 주입해야 한다."""


class NetworkBlocked(ConnectionRefusedError):
    """테스트 중 외부 네트워크 접근 시도."""


def _blocked_path(p) -> str | None:
    if isinstance(p, int):
        return None
    try:
        s = os.fspath(p)
    except TypeError:
        return None
    if isinstance(s, bytes):
        s = s.decode(errors="ignore")
    if not s:
        return None
    a = os.path.abspath(s)
    for prefix in BLOCKED_PATH_PREFIXES:
        if a == prefix or a.startswith(prefix + os.sep):
            return a
    return None


def _guard_fs(mod, name, path_arg_index=0):
    original = getattr(mod, name)

    def guarded(*args, **kwargs):
        target = args[path_arg_index] if len(args) > path_arg_index else kwargs.get("path")
        hit = _blocked_path(target)
        if hit is not None:
            VIOLATIONS.append(f"{_CURRENT_TEST[0]} :: {mod.__name__}.{name}({hit})")
            raise IsolationViolation(
                errno.EACCES, f"[테스트 격리] 운영 상태 파일 접근 차단 ({name})", hit
            )
        return original(*args, **kwargs)

    guarded.__wrapped__ = original  # type: ignore[attr-defined]
    setattr(mod, name, guarded)


def _guard_fs_two(mod, name):
    """rename/replace 처럼 src·dst 둘 다 검사."""
    original = getattr(mod, name)

    def guarded(src, dst, *args, **kwargs):
        for t in (src, dst):
            hit = _blocked_path(t)
            if hit is not None:
                VIOLATIONS.append(f"{_CURRENT_TEST[0]} :: {mod.__name__}.{name}({hit})")
                raise IsolationViolation(
                    errno.EACCES, f"[테스트 격리] 운영 상태 파일 접근 차단 ({name})", hit
                )
        return original(src, dst, *args, **kwargs)

    guarded.__wrapped__ = original  # type: ignore[attr-defined]
    setattr(mod, name, guarded)


# open 계열 (pathlib.Path.open/read_text/write_text 는 io.open 을 쓴다)
_guard_fs(io, "open")
builtins.open = io.open
_guard_fs(os, "open")
# 메타데이터·디렉터리
for _n in ("stat", "lstat", "listdir", "scandir", "mkdir", "makedirs",
           "remove", "unlink", "rmdir", "utime", "access", "truncate"):
    if hasattr(os, _n):
        _guard_fs(os, _n)
for _n in ("rename", "replace", "link", "symlink"):
    if hasattr(os, _n):
        _guard_fs_two(os, _n)


# ── 네트워크 ────────────────────────────────────────────────────────────────
def _host_of(address) -> str | None:
    if isinstance(address, (str, bytes)):          # AF_UNIX
        return None
    if isinstance(address, tuple) and address:
        return str(address[0])
    return "?"


def _deny_net(what: str, host: str):
    VIOLATIONS.append(f"{_CURRENT_TEST[0]} :: net.{what}({host})")
    raise NetworkBlocked(errno.ECONNREFUSED, f"[테스트 격리] 외부 네트워크 차단 ({what} → {host})")


_orig_connect = socket.socket.connect
_orig_connect_ex = socket.socket.connect_ex
_orig_create_connection = socket.create_connection
_orig_getaddrinfo = socket.getaddrinfo


def _connect(self, address):
    host = _host_of(address)
    if host is not None and host not in _LOOPBACK_HOSTS:
        _deny_net("connect", host)
    return _orig_connect(self, address)


def _connect_ex(self, address):
    host = _host_of(address)
    if host is not None and host not in _LOOPBACK_HOSTS:
        _deny_net("connect_ex", host)
    return _orig_connect_ex(self, address)


def _create_connection(address, *args, **kwargs):
    host = _host_of(address)
    if host is not None and host not in _LOOPBACK_HOSTS:
        _deny_net("create_connection", host)
    return _orig_create_connection(address, *args, **kwargs)


def _getaddrinfo(host, *args, **kwargs):
    h = "" if host is None else (host.decode() if isinstance(host, bytes) else str(host))
    if h not in _LOOPBACK_HOSTS:
        _deny_net("getaddrinfo", h)
    return _orig_getaddrinfo(host, *args, **kwargs)


socket.socket.connect = _connect            # type: ignore[assignment]
socket.socket.connect_ex = _connect_ex      # type: ignore[assignment]
socket.create_connection = _create_connection
socket.getaddrinfo = _getaddrinfo


def pytest_runtest_protocol(item, nextitem):
    _CURRENT_TEST[0] = item.nodeid
    return None


# ── curl_cffi (yfinance 백엔드) — C 레벨 curl 이라 socket 패치를 우회한다 (D 재현 중 실측 발견, 2026-09-15)
try:
    import curl_cffi.curl as _curl_mod
    from curl_cffi import requests as _curl_requests
except Exception:  # 미설치 환경
    _curl_mod = None
    _curl_requests = None

if _curl_mod is not None:
    def _curl_perform(self, *args, **kwargs):
        _deny_net("curl_cffi.perform", "?")

    _curl_mod.Curl.perform = _curl_perform  # type: ignore[assignment]
    if hasattr(_curl_mod, "AsyncCurl"):
        _orig_add_handle = getattr(_curl_mod.AsyncCurl, "add_handle", None)
        if _orig_add_handle is not None:
            def _curl_add_handle(self, curl, *args, **kwargs):
                _deny_net("curl_cffi.AsyncCurl", "?")
            _curl_mod.AsyncCurl.add_handle = _curl_add_handle  # type: ignore[assignment]
    if _curl_requests is not None:
        def _curl_request(self, method, url, *args, **kwargs):
            _deny_net("curl_cffi.request", str(url))

        _curl_requests.Session.request = _curl_request  # type: ignore[assignment]
        if hasattr(_curl_requests, "AsyncSession"):
            async def _curl_arequest(self, method, url, *args, **kwargs):
                _deny_net("curl_cffi.arequest", str(url))
            _curl_requests.AsyncSession.request = _curl_arequest  # type: ignore[assignment]


# ── 리포트 ───────────────────────────────────────────────────────────────────
def pytest_terminal_summary(terminalreporter, exitstatus, config):
    if not VIOLATIONS:
        terminalreporter.write_line("[테스트 격리] 운영 상태·외부 네트워크 접근 시도 0건")
        return
    terminalreporter.write_line(
        f"[테스트 격리] 차단된 접근 시도 {len(VIOLATIONS)}건 — 테스트가 임시 경로/가짜 공급자를 주입하지 않았다:"
    )
    seen = set()
    for v in VIOLATIONS:
        if v in seen:
            continue
        seen.add(v)
        terminalreporter.write_line(f"  - {v}")
