"""KR 브로커 GET 관측. 프로세스 수명 누적, 고정 차원, I/O·대기·추가 task 없음.

리미터/주문 제어에 사용하지 않는다. 계좌·종목·본문·URL·예외 문자열은 받지 않는다.
asyncio 단일 루프에서 카운터 갱신 중 await가 없으며 context는 task별로 격리된다.
"""
import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
from aiohttp import ClientError

SOURCES = frozenset({"startup", "portfolio_sync", "portfolio_sync_consistency_retry",
                     "fill_check", "batch_guard", "dashboard_external_accounts",
                     "dashboard_settlement", "unknown"})
OPERATIONS = frozenset({"account_summary", "positions", "external_positions",
                        "daily_fills", "open_orders", "orderable_cash", "other"})
TR_IDS = frozenset({"TTTC8434R", "TTTC8908R", "TTTC8001R", "TTTC8036R",
                    "TTTC0081R", "TTTC0084R", "TTTS3012R", "VTTS3012R", "other"})
OUTCOMES = frozenset({"success", "http_error", "api_error", "network_error",
                     "cancelled", "invalid_json", "other"})
_source = ContextVar("kis_request_source", default="unknown")
_operation = ContextVar("kis_request_operation", default="other")
_page = ContextVar("kis_request_page", default=1)


def _label(value, allowed, default):
    return value if type(value) is str and value in allowed else default


class RequestMetrics:
    """고정 allowlist 조합만 저장하며 snapshot은 독립된 dict를 반환한다."""
    def __init__(self):
        self.logical = {}
        self.http = {}
        self.available = True

    def increment(self, table, key, field):
        rows = self.logical if table == "logical" else self.http
        if key not in rows:
            fields = ("calls", "cache_hits", "cache_misses") if table == "logical" else (
                "attempts", "retries", "egw00215", *sorted(OUTCOMES))
            rows[key] = dict.fromkeys(fields, 0)
        rows[key][field] += 1

    def snapshot(self):
        return {
            "available": self.available, "scope": "kr_broker_get", "window": "process_lifetime",
            "logical": [dict(source=k[0], operation=k[1], **v) for k, v in sorted(self.logical.items())],
            "http": [dict(source=k[0], operation=k[1], tr_id=k[2], page=k[3], **v)
                     for k, v in sorted(self.http.items())],
        }


_recorder = RequestMetrics()


def _increment(table, key, field):
    try:
        _recorder.increment(table, key, field)
    except Exception:
        # 원문 예외를 저장/출력하지 않는다. 부분 계수는 정상 0과 구별한다.
        try:
            _recorder.available = False
        except Exception:
            pass


@contextmanager
def request_source(source):
    token = _source.set(_label(source, SOURCES, "unknown"))
    try:
        yield
    finally:
        _source.reset(token)


def with_request_source(source):
    def decorate(func):
        @wraps(func)
        async def wrapped(*args, **kwargs):
            with request_source(source):
                return await func(*args, **kwargs)
        return wrapped
    return decorate


@contextmanager
def logical_operation(operation):
    token = _operation.set(_label(operation, OPERATIONS, "other"))
    page_token = _page.set(1)
    try:
        _increment("logical", (_source.get(), _operation.get()), "calls")
        yield
    finally:
        _page.reset(page_token)
        _operation.reset(token)


def observe_operation(operation):
    def decorate(func):
        @wraps(func)
        async def wrapped(*args, **kwargs):
            with logical_operation(operation):
                return await func(*args, **kwargs)
        return wrapped
    return decorate


@contextmanager
def request_page(page):
    token = _page.set(page if type(page) is int and 1 <= page <= 10 else 0)
    try:
        yield
    finally:
        _page.reset(token)


def record_cache(result):
    if result in ("hit", "miss"):
        _increment("logical", (_source.get(), _operation.get()),
                   "cache_hits" if result == "hit" else "cache_misses")


def _http_key(tr_id):
    return (_source.get(), _operation.get(), _label(tr_id, TR_IDS, "other"), _page.get())


def record_http_attempt(tr_id, attempt):
    key = _http_key(tr_id)
    _increment("http", key, "attempts")
    if attempt > 0:
        _increment("http", key, "retries")


def record_http_result(tr_id, outcome, egw00215=False):
    key = _http_key(tr_id)
    _increment("http", key, _label(outcome, OUTCOMES, "other"))
    if egw00215:
        _increment("http", key, "egw00215")


@contextmanager
def http_attempt(tr_id, attempt):
    """전송 직전부터 응답까지 계수. 응답 후 재시도 대기의 취소는 중복 계수하지 않는다."""
    record_http_attempt(tr_id, attempt)
    finished = False

    def finish(outcome, rejection=False):
        nonlocal finished
        if not finished:
            finished = True
            record_http_result(tr_id, outcome, rejection)

    try:
        yield finish
    except asyncio.CancelledError:
        finish("cancelled")
        raise
    except (ClientError, OSError, asyncio.TimeoutError):
        finish("network_error")
        raise
    except Exception:
        finish("other")
        raise
    finally:
        finish("other")


def snapshot():
    try:
        return _recorder.snapshot()
    except Exception:
        return {"available": False, "scope": "kr_broker_get", "window": "process_lifetime",
                "logical": [], "http": []}


def rate_limit_calls_last_sec():
    """공용 리미터 최근 송신수를 읽기만 한다. 획득/만료 정리 등 상태 변경 없음."""
    try:
        from . import kis_rate_limit
        now = kis_rate_limit.time.monotonic()
        return sum(0 <= now - stamp <= 1.0 for stamp in kis_rate_limit._calls)
    except Exception:
        return None
