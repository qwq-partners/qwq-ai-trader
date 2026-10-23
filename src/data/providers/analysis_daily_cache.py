"""분석 소비자만 공유하는 짧은 일봉 캐시. 브로커/청산 메서드는 변경하지 않는다."""
import asyncio
from collections import OrderedDict
from contextlib import suppress
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
import math
import time
from weakref import WeakKeyDictionary
from zoneinfo import ZoneInfo

from src.utils.session import KRSession

_clock = time.monotonic
_caches = WeakKeyDictionary()
_TTL = 60.0
_CAPACITY = 64


def _now():
    return datetime.now(ZoneInfo("Asia/Seoul"))


def _epoch():
    now = _now()
    return now.strftime("%Y%m%d"), KRSession().get_session(now).value


def _cacheable(rows, days, day):
    if not isinstance(rows, list) or not days <= len(rows) <= 500:
        return False
    previous = ""
    for row in rows:
        if not isinstance(row, dict):
            return False
        stamp = row.get("date")
        if not isinstance(stamp, str) or len(stamp) != 8 or not previous < stamp <= day:
            return False
        try:
            datetime.strptime(stamp, "%Y%m%d")
            for field in ("open", "high", "low", "close", "volume"):
                value = row.get(field)
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                    return False
                if value < 0 or (field != "volume" and value == 0):
                    return False
            if row["low"] > min(row["open"], row["close"]) or row["high"] < max(row["open"], row["close"]):
                return False
        except (ValueError, OverflowError):
            return False
        previous = stamp
    return True


@dataclass
class _Flight:
    task: asyncio.Task
    waiters: int = 0


class _AnalysisCache:
    def __init__(self):
        self.loop = asyncio.get_running_loop()
        self.entries = OrderedDict()
        self.flights = {}
        self.counts = dict(requests=0, cache_hits=0, coalesced=0, provider_calls=0)

    async def read(self, broker, symbol, days):
        self.counts["requests"] += 1
        epoch, started = _epoch(), _clock()
        key = (epoch, symbol)
        cached = self.entries.get(key)
        if cached is not None:
            at, data = cached
            if 0 <= started - at < _TTL:
                if len(data) >= days:
                    self.counts["cache_hits"] += 1
                    self.entries.move_to_end(key)
                    return deepcopy(data[-days:])
                # 짧아도 최신 자료를 남겨 이전 시각의 진행 요청이 덮지 못하게 한다.
            else:
                self.entries.pop(key, None)
        # 진행 요청은 같은 깊이만 합친다. 짧거나 부분인 큰 요청의 의미를 추정하지 않는다.
        flight_key = key + (days,)
        flight = self.flights.get(flight_key)
        if flight is None:
            async def fetch():
                self.counts["provider_calls"] += 1
                data = await broker.get_daily_prices(symbol, days=days)
                if (_epoch() == epoch and 0 <= _clock() - started < _TTL
                        and _cacheable(data, days, epoch[0])):
                    # 최신 요청을 우선하고, 시작 시각도 같으면 깊은 자료를 보존한다.
                    existing = self.entries.get(key)
                    if existing is None or started > existing[0] or (
                            started == existing[0] and len(data) >= len(existing[1])):
                        self.entries[key] = (started, deepcopy(data))
                        self.entries.move_to_end(key)
                    while len(self.entries) > _CAPACITY:
                        self.entries.popitem(last=False)
                return data

            flight = _Flight(asyncio.create_task(fetch(), name="analysis_daily_prices"))
            self.flights[flight_key] = flight
        else:
            self.counts["coalesced"] += 1
        flight.waiters += 1
        try:
            return deepcopy(await asyncio.shield(flight.task))
        finally:
            flight.waiters -= 1
            if flight.waiters == 0:
                if self.flights.get(flight_key) is flight:
                    self.flights.pop(flight_key)
                if not flight.task.done():
                    flight.task.cancel()
                    with suppress(asyncio.CancelledError, Exception):
                        await flight.task


async def get_analysis_daily_prices(broker, symbol, *, days=60):
    """같은 브로커의 분석 요청만 공유한다. 실패/불완전 자료는 캐시하지 않는다.

    반환 형식과 공급자는 원래 브로커와 같다. 요청 시작부터 60초 뒤, KST 날짜나
    세션 변경 시 다시 조회하며 당일 봉의 확정 여부를 새로 부여하지 않는다.
    """
    if type(days) is not int or not 1 <= days <= 500 or not isinstance(symbol, str):
        return await broker.get_daily_prices(symbol, days=days)
    try:
        cache = _caches.get(broker)
    except TypeError:  # 약한 참조/해시를 지원하지 않는 외부 브로커는 기존 경로 유지.
        return await broker.get_daily_prices(symbol, days=days)
    if cache is None or cache.loop is not asyncio.get_running_loop():
        cache = _AnalysisCache()
        _caches[broker] = cache
    return await cache.read(broker, symbol, days)


def analysis_daily_snapshot(broker):
    """조회 없이 고정 차원만 반환한다. provider_calls는 HTTP 페이지 수가 아니다."""
    try:
        cache = _caches.get(broker)
    except TypeError:
        cache = None
    counts = dict(requests=0, cache_hits=0, coalesced=0, provider_calls=0)
    if cache is not None:
        counts.update(cache.counts)
    return dict(scope="analysis_daily_prices", window="broker_event_loop_lifetime",
                **counts, entries=len(cache.entries) if cache else 0)
