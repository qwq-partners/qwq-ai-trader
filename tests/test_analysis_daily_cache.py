"""분석 소비자가 같은 일봉을 재조회하거나 다른 시점 자료를 재사용하는 회귀."""
import asyncio
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest


def rows(count=30):
    start = datetime(2026, 8, 1)
    return [{"date": (start + timedelta(days=i)).strftime("%Y%m%d"),
             "open": 100, "high": 110, "low": 90, "close": 105,
             "volume": 0, "value": 0} for i in range(count)]


class Broker:
    def __init__(self):
        self.calls = []
        self.result = rows()
        self.started = asyncio.Event()
        self.release = None
        self.cancelled = False

    async def get_daily_prices(self, symbol, days=60):
        self.calls.append((symbol, days))
        self.started.set()
        try:
            if self.release is not None:
                await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        if isinstance(self.result, Exception):
            raise self.result
        return [dict(row) for row in self.result[-days:]]


@pytest.fixture
def cache(monkeypatch):
    from src.data.providers import analysis_daily_cache as module
    stamp = [datetime(2026, 10, 23, 10, tzinfo=ZoneInfo("Asia/Seoul"))]
    ticks = [100.0]
    monkeypatch.setattr(module, "_now", lambda: stamp[0])
    monkeypatch.setattr(module, "_clock", lambda: ticks[0])
    return module, stamp, ticks


@pytest.mark.asyncio
async def test_reuses_sufficient_history_without_mutable_aliases(cache):
    module, _, _ = cache
    broker = Broker()
    first = await module.get_analysis_daily_prices(broker, "005930", days=30)
    first[-1]["close"] = 999
    second = await module.get_analysis_daily_prices(broker, "005930", days=25)
    assert len(second) == 25 and second[-1]["close"] == 105
    assert broker.calls == [("005930", 30)]
    second.clear()
    assert len(await module.get_analysis_daily_prices(broker, "005930", days=30)) == 30
    stats = module.analysis_daily_snapshot(broker)
    assert (stats["requests"], stats["cache_hits"], stats["provider_calls"]) == (3, 2, 1)
    stats["cache_hits"] = -1
    assert module.analysis_daily_snapshot(broker)["cache_hits"] == 2


@pytest.mark.asyncio
async def test_deeper_waiter_does_not_discard_newer_shallow_history(cache):
    module, _, ticks = cache
    started, release = asyncio.Event(), asyncio.Event()

    class OverlappingBroker:
        calls = 0

        async def get_daily_prices(self, symbol, days=60):
            self.calls += 1
            data = rows()[-days:]
            if self.calls == 1:
                started.set()
                await release.wait()
            else:
                data[-1]["close"] = 106
            return data

    broker = OverlappingBroker()
    old = asyncio.create_task(module.get_analysis_daily_prices(broker, "005930", days=30))
    await started.wait()
    ticks[0] = 101
    fresh = await module.get_analysis_daily_prices(broker, "005930", days=25)
    assert fresh[-1]["close"] == 106
    ticks[0] = 102
    joined = asyncio.create_task(module.get_analysis_daily_prices(broker, "005930", days=30))
    await asyncio.sleep(0)
    release.set()
    assert all(data[-1]["close"] == 105 for data in await asyncio.gather(old, joined))
    newest = await module.get_analysis_daily_prices(broker, "005930", days=25)
    assert newest[-1]["close"] == 106
    assert broker.calls == 2


@pytest.mark.asyncio
async def test_depth_and_broker_are_not_interchangeable(cache):
    module, _, _ = cache
    broker, other = Broker(), Broker()
    await module.get_analysis_daily_prices(broker, "005930", days=5)
    await module.get_analysis_daily_prices(broker, "005930", days=30)
    await module.get_analysis_daily_prices(other, "005930", days=30)
    assert broker.calls == [("005930", 5), ("005930", 30)]
    assert len(other.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["ttl", "backwards", "date", "session"])
async def test_refreshes_at_age_or_market_boundary(cache, change):
    module, stamp, ticks = cache
    broker = Broker()
    await module.get_analysis_daily_prices(broker, "005930", days=30)
    if change == "ttl":
        ticks[0] += 60
    elif change == "backwards":
        ticks[0] -= 1
    elif change == "date":
        stamp[0] += timedelta(days=1)
    else:
        stamp[0] = stamp[0].replace(hour=16)
    broker.result[-1]["close"] = 106
    result = await module.get_analysis_daily_prices(broker, "005930", days=30)
    assert result[-1]["close"] == 106 and len(broker.calls) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["empty", "short", "nan", "duplicate", "future"])
async def test_unsuitable_result_does_not_poison_next_request(cache, bad):
    module, _, _ = cache
    broker = Broker()
    if bad == "empty":
        broker.result = []
    elif bad == "short":
        broker.result = rows(5)
    elif bad == "nan":
        broker.result[-1]["close"] = float("nan")
    elif bad == "duplicate":
        broker.result[-1]["date"] = broker.result[-2]["date"]
    else:
        broker.result[-1]["date"] = "20261024"
    await module.get_analysis_daily_prices(broker, "005930", days=30)
    broker.result = rows()
    result = await module.get_analysis_daily_prices(broker, "005930", days=30)
    assert result[-1]["close"] == 105 and len(broker.calls) == 2


@pytest.mark.asyncio
async def test_same_inflight_request_survives_one_waiter_cancellation(cache):
    module, _, _ = cache
    broker = Broker()
    broker.release = asyncio.Event()
    one = asyncio.create_task(module.get_analysis_daily_prices(broker, "005930", days=30))
    await broker.started.wait()
    two = asyncio.create_task(module.get_analysis_daily_prices(broker, "005930", days=30))
    await asyncio.sleep(0)
    one.cancel()
    with pytest.raises(asyncio.CancelledError):
        await one
    assert not broker.cancelled
    broker.release.set()
    assert len(await two) == 30 and len(broker.calls) == 1
    stats = module.analysis_daily_snapshot(broker)
    assert (stats["requests"], stats["coalesced"], stats["provider_calls"]) == (2, 1, 1)


@pytest.mark.asyncio
async def test_last_cancelled_waiter_reaps_sender_and_next_call_retries(cache):
    module, _, _ = cache
    broker = Broker()
    broker.release = asyncio.Event()
    task = asyncio.create_task(module.get_analysis_daily_prices(broker, "005930", days=30))
    await broker.started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert broker.cancelled
    broker.release.set()
    assert len(await module.get_analysis_daily_prices(broker, "005930", days=30)) == 30
    assert len(broker.calls) == 2


@pytest.mark.asyncio
async def test_slow_request_does_not_get_new_sixty_second_lifetime(cache):
    module, _, ticks = cache
    broker = Broker()
    broker.release = asyncio.Event()
    task = asyncio.create_task(module.get_analysis_daily_prices(broker, "005930", days=30))
    await broker.started.wait()
    ticks[0] += 61
    broker.release.set()
    await task
    await module.get_analysis_daily_prices(broker, "005930", days=30)
    assert len(broker.calls) == 2


@pytest.mark.asyncio
async def test_failed_request_is_retried_and_capacity_evicts_old_entry(cache):
    module, _, _ = cache
    broker = Broker()
    broker.result = RuntimeError("synthetic")
    with pytest.raises(RuntimeError):
        await module.get_analysis_daily_prices(broker, "005930", days=30)
    broker.result = rows()
    for number in range(65):
        await module.get_analysis_daily_prices(broker, f"{number:06d}", days=30)
    await module.get_analysis_daily_prices(broker, "000000", days=30)
    assert len(broker.calls) == 67


@pytest.mark.asyncio
async def test_real_screener_reuses_http_bars_with_identical_candidate_scores(monkeypatch):
    from test_screener_daily_cache_depth import history, screener_with_http
    from src.signals.screener.kr_screener import ScreenedStock
    screener, sent = screener_with_http(monkeypatch, history())
    one = ScreenedStock("005930", score=80, volume=2000)
    two = ScreenedStock("005930", score=80, volume=2000)
    for candidate in (one, two):
        local = {}
        await screener._apply_momentum_filter({candidate.symbol: candidate}, daily_cache=local)
        await screener._apply_volume_ratio({candidate.symbol: candidate}, daily_cache=local)
        await screener._apply_volatility_filter({candidate.symbol: candidate}, daily_cache=local)
    assert (one.score, one.atr_pct, one.volume_ratio) == (two.score, two.atr_pct, two.volume_ratio)
    assert len(sent) == 1


@pytest.mark.asyncio
async def test_sector_analysis_reuses_real_broker_http(monkeypatch):
    from test_screener_daily_cache_depth import history, screener_with_http
    from src.data.providers.sector_momentum import SectorMomentumProvider
    screener, sent = screener_with_http(monkeypatch, history())
    provider = object.__new__(SectorMomentumProvider)
    provider._broker = screener._broker
    first = await provider._calc_etf_returns("091160")
    second = await provider._calc_etf_returns("091160")
    assert first == second and first is not None
    assert len(sent) == 1
