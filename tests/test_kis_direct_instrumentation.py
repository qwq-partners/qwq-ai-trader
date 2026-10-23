"""실제 공급자 조회의 요청 경계에서 직접 KIS 집계와 기존 동작을 대조한다."""
import asyncio
import json
from types import SimpleNamespace

import aiohttp
import pytest

from src.data.providers import kis_market_data as market_module
from src.data.providers.kis_market_data import KISMarketData
from src.signals.screener import kr_screener as screener_module
from src.signals.screener.kr_screener import StockScreener
from src.utils import kis_direct_metrics as metrics


class _Response:
    def __init__(self, status=200, body=None, error=None, exit_error=None):
        self.status, self.body, self.error, self.exit_error = status, body, error, exit_error
        self.json_calls = 0
        self._raw = json.dumps(body).encode()
        self._offset = 0
        self.content = self

    async def read(self, size):
        chunk = self._raw[self._offset:self._offset + size]
        self._offset += len(chunk)
        return chunk

    def at_eof(self):
        return self._offset == len(self._raw)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        if self.exit_error:
            raise self.exit_error
        return False

    async def json(self):
        self.json_calls += 1
        if self.error:
            raise self.error
        return self.body


class _Session:
    def __init__(self, replies):
        self.replies = iter(replies)
        self.calls = []
        self.closed = False

    def get(self, url, headers=None, params=None):
        self.calls.append((url, dict(headers or {}), dict(params or {})))
        reply = next(self.replies)
        if isinstance(reply, _SyncGetError):
            raise reply.error
        if isinstance(reply, BaseException):
            return _RaisingRequest(reply)
        return reply


class _RaisingRequest:
    """aiohttp 요청 context 진입 중 일어난 전송 실패를 흉내 낸다."""

    def __init__(self, error):
        self.error = error

    async def __aenter__(self):
        raise self.error

    async def __aexit__(self, *_args):
        return False


class _SyncGetError:
    """GET context를 만들기 전 동기적으로 발생한 전송 설정 실패를 흉내 낸다."""

    def __init__(self, error):
        self.error = error


@pytest.fixture(autouse=True)
def isolated_metrics(monkeypatch):
    monkeypatch.setattr(metrics, "_recorder", metrics.DirectKISMetrics())
    monkeypatch.setattr(market_module.kis_rate_limit, "_state",
                        dict.fromkeys(market_module.kis_rate_limit._state, 0))

    async def no_limit():
        return None

    monkeypatch.setattr(market_module.kis_rate_limit, "acquire", no_limit)
    monkeypatch.setattr(screener_module.kis_rate_limit, "acquire", no_limit)


def _market(replies):
    provider = KISMarketData(token_manager=SimpleNamespace(base_url="http://fake"))
    session = _Session(replies)

    async def get_session():
        return session

    async def headers(_tr_id):
        return {}

    provider._get_session, provider._get_headers = get_session, headers
    return provider, session


def _screener(replies):
    screener = object.__new__(StockScreener)
    screener._cache, screener._cache_time, screener._cache_ttl = {}, {}, 300
    screener._token_manager = SimpleNamespace(base_url="http://fake")
    screener.min_volume_ratio, screener.max_change_pct = 2.0, 15.0
    screener.min_trading_value = 100_000_000
    session = _Session(replies)

    async def get_session():
        return session

    async def headers(_tr_id):
        return {}

    screener._get_session, screener._get_headers = get_session, headers
    return screener, session


def _row(operation):
    return next(item for item in metrics.snapshot()["http"] if item["operation"] == operation)


@pytest.mark.parametrize(
    ("factory", "method", "operation", "tr_id", "expected_attempts"),
    [
        (_market, lambda p: p.fetch_holidays("202610"), "holidays", "CTCA0903R", 1),
        (_market, lambda p: p.fetch_sector_indices(), "sector_indices", "FHPUP02140000", 1),
        (_market, lambda p: p.fetch_fluctuation_rank(), "fluctuation_rank", "FHPST01700000", 1),
        (_market, lambda p: p.fetch_foreign_institution(), "foreign_institution", "FHPTJ04400000", 1),
        (_market, lambda p: p.fetch_frgnmem_trade_estimate(), "frgnmem_trade_estimate", "FHKST644100C0", 1),
        (_market, lambda p: p.fetch_stock_investor_daily("000001"), "stock_investor_daily", "FHKST01010900", 1),
        (_market, lambda p: p.fetch_investor_trend_estimate("000001"), "investor_trend_estimate", "HHPTJ04160200", 1),
        (_market, lambda p: p.fetch_stock_valuation("000001"), "stock_valuation", "FHKST01010100", 1),
        (_market, lambda p: p.get_night_futures_quote(symbol="TEST", cache_ttl=0), "night_futures_quote", "FHMIF10000000", 2),
        (_market, lambda p: p.fetch_index_price(), "index_price", "FHPUP02100000", 1),
        (_screener, lambda s: s.screen_volume_surge(), "volume_surge", "FHPST01710000", 1),
        (_screener, lambda s: s.screen_new_highs(), "new_highs", "FHPST01720000", 1),
    ],
)
def test_each_direct_kis_get_reads_non_200_and_feeds_confirmed_rejection_once(
    factory, method, operation, tr_id, expected_attempts
):
    responses = [_Response(status=503, body={"msg_cd": "EGW00201"}) for _ in range(expected_attempts)]
    target, session = factory(responses)
    asyncio.run(method(target))

    row = _row(operation)
    assert (row["component"], row["tr_id"], row["attempts"], row["http_error"], row["api_error"]) == (
        "market_data" if factory is _market else "kr_screener", tr_id, expected_attempts, expected_attempts, 0
    )
    assert [response.json_calls for response in responses] == [0] * expected_attempts
    assert all(response.at_eof() for response in responses)
    assert row["egw00201"] == expected_attempts
    assert market_module.kis_rate_limit._state["rejections"] == expected_attempts
    assert len(session.calls) == expected_attempts


def test_decoded_api_error_invalid_json_timeout_and_cancellation_keep_existing_provider_flow(monkeypatch):
    provider, _ = _market([_Response(body={"rt_cd": "EGW00215"})])
    assert asyncio.run(provider.fetch_sector_indices()) == []
    api = _row("sector_indices")
    assert (api["api_error"], api["egw00215"]) == (1, 1)

    provider, _ = _market([_Response(error=ValueError("synthetic"))])
    assert asyncio.run(provider.fetch_sector_indices()) == []
    assert _row("sector_indices")["invalid_json"] == 1

    provider, _ = _market([asyncio.TimeoutError("synthetic")])
    assert asyncio.run(provider.fetch_sector_indices()) == []
    assert _row("sector_indices")["network_error"] == 1

    provider, _ = _market([asyncio.CancelledError()])
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(provider.fetch_sector_indices())
    assert _row("sector_indices")["cancelled"] == 1

    provider, _ = _market([_SyncGetError(asyncio.TimeoutError("sync setup"))])
    assert asyncio.run(provider.fetch_sector_indices()) == []
    assert _row("sector_indices")["network_error"] == 2

    async def cancel_limiter():
        raise asyncio.CancelledError()

    monkeypatch.setattr(market_module.kis_rate_limit, "acquire", cancel_limiter)
    provider, _ = _market([])
    attempts_before = _row("sector_indices")["attempts"]
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(provider.fetch_sector_indices())
    assert _row("sector_indices")["attempts"] == attempts_before


def test_response_exit_failure_after_decoded_success_does_not_reclassify_attempt():
    provider, _ = _market([_Response(
        body={"rt_cd": "0", "output": []},
        exit_error=aiohttp.ClientPayloadError("close failure"),
    )])
    assert asyncio.run(provider.fetch_sector_indices()) == []
    row = _row("sector_indices")
    assert (row["success"], row["network_error"], row["other"]) == (1, 0, 0)


def test_cache_cursor_and_night_fallback_preserve_real_get_counts():
    provider, session = _market([
        _Response(body={"rt_cd": "0", "output": [{"bass_dt": "20261015", "opnd_yn": "N"}]}),
        _Response(body={"rt_cd": "0", "output": [{"bass_dt": "20261031", "opnd_yn": "Y"}]}),
    ])
    holidays = asyncio.run(provider.fetch_holidays("202610"))
    assert len(holidays) == 1
    assert (_row("holidays")["attempts"], _row("holidays")["success"], len(session.calls)) == (2, 2, 2)

    provider, session = _market([
        _Response(body={"rt_cd": "0", "output1": {}}),
        _Response(body={"rt_cd": "0", "output1": {"futs_prpr": "410", "futs_sdpr": "400"}}),
    ])
    assert asyncio.run(provider.get_night_futures_quote(symbol="TEST", cache_ttl=0))["session"] == "day"
    assert (_row("night_futures_quote")["attempts"], _row("night_futures_quote")["success"], len(session.calls)) == (2, 2, 2)

    provider, session = _market([_Response(body={"rt_cd": "0", "output": {"bstp_nmix_prpr": "10"}})])
    assert asyncio.run(provider.fetch_index_price()) is not None
    assert asyncio.run(provider.fetch_index_price()) is not None
    assert (_row("index_price")["attempts"], _row("index_price")["success"], len(session.calls)) == (1, 1, 1)


def test_naver_request_is_excluded_and_recorder_failure_changes_no_return(monkeypatch):
    screener, _ = _screener([_Response(status=500)])
    assert asyncio.run(screener._naver_crawl("http://naver.fake")) is None
    assert metrics.snapshot()["http"] == []

    provider, _ = _market([_Response(body={"rt_cd": "0", "output": []})])

    def broken(*_args):
        raise RuntimeError("synthetic recorder failure")

    monkeypatch.setattr(metrics._recorder, "increment", broken)
    assert asyncio.run(provider.fetch_sector_indices()) == []
    assert metrics.snapshot()["available"] is False
