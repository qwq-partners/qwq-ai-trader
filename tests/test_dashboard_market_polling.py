"""전광판 조회 절약이 거래 시세나 미국 지수의 갱신을 막지 않아야 한다."""
import asyncio
import json
from datetime import datetime
from types import SimpleNamespace

import pytest

from src.dashboard.sse import SSEManager
from src.utils.session import KST


def test_no_subscriber_does_not_fetch_market_data(monkeypatch):
    manager = SSEManager()
    calls = []

    async def fetch():
        calls.append("network")
        return [{"label": "KOSPI"}]

    async def stop(_):
        manager._running = False

    monkeypatch.setattr(manager, "_fetch_market_indices", fetch)
    monkeypatch.setattr("src.dashboard.sse.asyncio.sleep", stop)
    asyncio.run(manager.run_broadcast_loop())
    assert calls == []


@pytest.fixture
def clock(monkeypatch):
    from src.dashboard import market_cache

    state = SimpleNamespace(seconds=1000.0, now=datetime(2026, 10, 17, 12, tzinfo=KST))
    monkeypatch.setattr(market_cache.time, "monotonic", lambda: state.seconds)
    monkeypatch.setattr(market_cache, "_now_kst", lambda: state.now)
    return state


def test_closed_cache_expires_and_session_change_refreshes(clock):
    from src.dashboard.market_cache import DashboardMarketCache
    cache = DashboardMarketCache()
    calls = []

    async def fetch():
        calls.append(True)
        return {"price": len(calls) * 100}

    async def run():
        first = await cache.get("quote:005930", fetch)
        first["price"] = -1
        clock.seconds += 299
        cached = await cache.get("quote:005930", fetch)
        assert cached["price"] == 100
        assert cached["fetched_at"] == "2026-10-17T12:00:00+09:00"
        clock.seconds += 1
        assert (await cache.get("quote:005930", fetch))["price"] == 200
        clock.now = datetime(2026, 10, 19, 9, tzinfo=KST)
        assert (await cache.get("quote:005930", fetch))["price"] == 300
        clock.seconds += 9
        assert (await cache.get("quote:005930", fetch))["price"] == 300
        clock.seconds += 1
        assert (await cache.get("quote:005930", fetch))["price"] == 400
    asyncio.run(run())


def test_failed_refresh_preserves_age_and_cools_down(clock):
    from src.dashboard.market_cache import DashboardMarketCache
    cache = DashboardMarketCache()
    calls = []

    async def fetch():
        calls.append(True)
        if len(calls) > 1:
            raise OSError("fake outage")
        return {"price": 100}

    async def run():
        first = await cache.get("quote:005930", fetch)
        clock.seconds += 300
        clock.now = datetime(2026, 10, 17, 12, 5, tzinfo=KST)
        stale = await cache.get("quote:005930", fetch)
        assert stale["price"] == 100 and stale["stale"] is True
        assert stale["fetched_at"] == first["fetched_at"]
        clock.seconds += 10
        assert (await cache.get("quote:005930", fetch))["stale"] is True
        assert len(calls) == 2
    asyncio.run(run())


def test_parallel_requests_share_one_attempt_and_cancellation_recovers(clock):
    from src.dashboard.market_cache import DashboardMarketCache
    cache = DashboardMarketCache()
    calls = []

    async def run():
        started, release = asyncio.Event(), asyncio.Event()

        async def fetch():
            calls.append(True)
            started.set()
            await release.wait()
            return {"price": 100}

        first = asyncio.create_task(cache.get("quote:005930", fetch))
        await started.wait()
        second = asyncio.create_task(cache.get("quote:005930", fetch))
        release.set()
        result = await asyncio.gather(first, second)
        assert [r["price"] for r in result] == [100, 100]
        assert len(calls) == 1

        async def cancel():
            raise asyncio.CancelledError

        with pytest.raises(asyncio.CancelledError):
            await cache.get("quote:000660", cancel)
        clock.seconds += 300
        assert (await cache.get("quote:000660", fetch))["price"] == 100
    asyncio.run(run())


class YahooSession:
    closed = False

    def __init__(self, *args, **kwargs):
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    def get(self, url):
        self.calls.append(url)
        return YahooResponse()


class YahooResponse:
    status = 200

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def json(self, **kwargs):
        return {"chart": {"result": [{"meta": {"regularMarketPrice": 120},
                                     "indicators": {"quote": [{"close": [100, 120]}]}}]}}


def test_sse_http_share_closed_quotes_but_us_updates_and_trading_bypasses(clock, monkeypatch, tmp_path):
    from src.dashboard import kr_api, office_api
    from src.dashboard.server import DashboardServer
    sources = []
    indices = []

    async def quote(symbol):
        from src.utils.kis_request_metrics import _source
        sources.append((symbol, _source.get()))
        return {"price": 12300, "change": 100, "change_pct": 0.82}

    async def index(code):
        indices.append(code)
        return {"label": {"0001": "KOSPI", "1001": "KOSDAQ"}[code],
                "kind": "index_kr", "price": 2500, "change": 0, "change_pct": 0}

    dc = SimpleNamespace(bot=SimpleNamespace(
        broker=SimpleNamespace(get_quote=quote),
        kis_market_data=SimpleNamespace(fetch_index_price=index)))
    server = DashboardServer(kr_bot=dc.bot)
    manager = server.sse_manager
    yahoo = YahooSession()
    manager._http_session = yahoo
    monkeypatch.setattr(kr_api, "_indices_cache", {"data": None, "ts": 0})
    monkeypatch.setattr(kr_api.aiohttp_client, "ClientSession", YahooSession)
    monkeypatch.setattr(office_api, "STATE_FILE", tmp_path / "office_status.json")
    app = server._create_app()
    http_handler = next(route.handler for route in app.router.routes()
                        if route.method == "GET" and route.resource.canonical == "/api/market/indices")

    async def run():
        first = await manager._fetch_market_indices()
        clock.seconds += 60
        second = await manager._fetch_market_indices()
        assert len(first) == len(second) == 8
        assert len(sources) == 3 and indices == ["0001", "1001"]
        assert len(yahoo.calls) == 6  # US 지수3개는 두 번 모두 갱신
        kr_api._indices_cache["ts"] = 0  # HTTP 본 경로도 같은 시세 캐시 사용
        response = await http_handler(None)
        assert len(json.loads(response.text)) == 8
        assert len(sources) == 3
        # 거래용 직접 호출은 표시용 캐시를 통과하지 않는다.
        await dc.bot.broker.get_quote("005930")
        assert len(sources) == 4
        assert sources[-1][1] == "unknown"
        clock.seconds += 300
        kr_api._indices_cache["ts"] = 0
        await http_handler(None)
        assert [s for _, s in sources[-3:]] == ["dashboard_http_indices"] * 3
        assert [s for _, s in sources[:3]] == ["dashboard_sse_indices"] * 3
    asyncio.run(run())


def test_empty_result_is_cooled_down_without_fabricated_price(clock):
    from src.dashboard.market_cache import DashboardMarketCache
    cache = DashboardMarketCache()
    calls = []

    async def empty():
        calls.append(True)
        return None

    async def run():
        assert await cache.get("quote:005930", empty) is None
        clock.seconds += 60
        assert await cache.get("quote:005930", empty) is None
        assert len(calls) == 1
    asyncio.run(run())


def test_closing_auction_is_not_throttled_as_a_holiday(clock):
    from src.dashboard.market_cache import DashboardMarketCache
    cache = DashboardMarketCache()
    clock.now = datetime(2026, 10, 19, 15, 25, tzinfo=KST)
    calls = []

    async def fetch():
        calls.append(True)
        return {"price": len(calls) * 100}

    async def run():
        assert (await cache.get("quote:005930", fetch))["price"] == 100
        clock.seconds += 10
        assert (await cache.get("quote:005930", fetch))["price"] == 200
    asyncio.run(run())
