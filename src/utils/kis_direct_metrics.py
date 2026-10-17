"""직접 KIS 시장 데이터 GET의 메모리 내 고정 차원 관측.

직접 공급자 호출은 브로커 요청 출처를 갖지 않으므로 기존 브로커 관측과 분리한다.
"""
import asyncio
from typing import Any

import aiohttp


COMPONENTS = frozenset({"market_data", "kr_screener", "other"})
OPERATIONS = frozenset({
    "holidays", "sector_indices", "fluctuation_rank", "foreign_institution",
    "frgnmem_trade_estimate", "stock_investor_daily", "investor_trend_estimate",
    "stock_valuation", "night_futures_quote", "index_price", "volume_surge",
    "new_highs", "other",
})
TR_IDS = frozenset({
    "CTCA0903R", "FHPUP02140000", "FHPST01700000", "FHPTJ04400000",
    "FHKST644100C0", "FHKST01010900", "HHPTJ04160200", "FHKST01010100",
    "FHMIF10000000", "FHPUP02100000", "FHPST01710000", "FHPST01720000", "other",
})
OUTCOMES = ("success", "http_error", "api_error", "network_error", "cancelled",
            "invalid_json", "other")
_FIELDS = ("attempts", *OUTCOMES, "egw00201", "egw00215")


def _label(value: Any, allowed: frozenset[str]) -> str:
    return value if type(value) is str and value in allowed else "other"


class DirectKISMetrics:
    """고정 라벨 조합만 보존하며 스냅샷은 독립 dict를 반환한다."""

    def __init__(self):
        self.http: dict[tuple[str, str, str], dict[str, int]] = {}
        self.available = True

    def increment(self, key: tuple[str, str, str], field: str) -> None:
        if key not in self.http:
            self.http[key] = dict.fromkeys(_FIELDS, 0)
        self.http[key][field] += 1

    def snapshot(self) -> dict:
        return {
            "available": self.available,
            "scope": "kr_market_data_and_screener_get",
            "window": "process_lifetime",
            "http": [dict(component=key[0], operation=key[1], tr_id=key[2], **values)
                     for key, values in sorted(self.http.items())],
        }


_recorder = DirectKISMetrics()


def _increment(key: tuple[str, str, str], field: str) -> None:
    """계측 실패는 공급자 호출의 반환이나 예외를 바꾸지 않는다."""
    try:
        _recorder.increment(key, field)
    except Exception:
        try:
            _recorder.available = False
        except Exception:
            pass


class _Observation:
    def __init__(self, component: str, operation: str, tr_id: str):
        self.key = (_label(component, COMPONENTS), _label(operation, OPERATIONS),
                    _label(tr_id, TR_IDS))
        self.finished = False

    def __enter__(self):
        return self.start()

    def start(self):
        """기존 리미터 직후 실제 GET 직전에 한 번만 시도 횟수를 올린다."""
        _increment(self.key, "attempts")
        return self

    async def __aenter__(self):
        return self.start()

    async def __aexit__(self, exc_type, exc, traceback):
        return self.__exit__(exc_type, exc, traceback)

    def __exit__(self, exc_type, exc, _traceback):
        if exc_type is None:
            self._finish("other")
        elif issubclass(exc_type, asyncio.CancelledError):
            self._finish("cancelled")
        elif issubclass(exc_type, (aiohttp.ClientError, OSError, asyncio.TimeoutError)):
            self._finish("network_error")
        else:
            self._finish("other")
        return False

    def _finish(self, outcome: str, envelope: Any = None) -> None:
        if self.finished:
            return
        self.finished = True
        _increment(self.key, outcome if outcome in OUTCOMES else "other")
        if outcome == "api_error" and isinstance(envelope, dict):
            for field in ("egw00201", "egw00215"):
                code = field.upper()
                if envelope.get("msg_cd") == code or envelope.get("rt_cd") == code:
                    _increment(self.key, field)

    def observe_response(self, response: Any) -> bool:
        """non-200 응답 본문을 읽지 않고 시도를 닫는다."""
        if response.status != 200:
            self._finish("http_error")
            return False
        return True

    async def json(self, response: Any) -> Any:
        """기존 본문을 한 번만 읽고 응답 봉투의 결과만 분류한다."""
        try:
            data = await response.json()
        except asyncio.CancelledError:
            self._finish("cancelled")
            raise
        except aiohttp.ContentTypeError:
            self._finish("invalid_json")
            raise
        except (aiohttp.ClientError, OSError, asyncio.TimeoutError):
            self._finish("network_error")
            raise
        except (ValueError, TypeError):
            self._finish("invalid_json")
            raise
        self.api_envelope(data)
        return data

    def api_envelope(self, data: Any) -> None:
        if not isinstance(data, dict):
            self._finish("other")
        elif data.get("rt_cd") == "0":
            self._finish("success")
        else:
            self._finish("api_error", data)


def observe_get(component: str, operation: str, tr_id: str) -> _Observation:
    """기존 리미터를 통과한 직접 GET 시도 한 건을 관측한다."""
    return _Observation(component, operation, tr_id)


def snapshot() -> dict:
    try:
        return _recorder.snapshot()
    except Exception:
        return {"available": False, "scope": "kr_market_data_and_screener_get",
                "window": "process_lifetime", "http": []}
