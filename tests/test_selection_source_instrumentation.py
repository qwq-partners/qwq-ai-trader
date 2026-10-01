"""Selection-source capture remains observational and distinguishes empty from failed."""
import asyncio
from types import SimpleNamespace

from src.signals.screener import kr_screener as m
from test_selection_source_basis import screener, stock


def run(s, **kwargs):
    return asyncio.run(s.screen_all(**kwargs))


def _runs(result):
    return {row["source_id"]: row for row in result.selection_sources["runs"]}


def test_capture_is_opt_in_and_preserves_candidates_scores_and_source_calls(monkeypatch):
    calls = {"volume": 0}

    async def volume(*_args, **_kwargs):
        calls["volume"] += 1
        return [stock(score=100)]

    s = screener(monkeypatch, {})
    s.screen_volume_surge = volume
    plain = run(s)
    traced = run(s, capture_selection_sources=True)

    assert calls == {"volume": 2}
    assert [(x.symbol, x.score, x.reasons) for x in plain] == [
        (x.symbol, x.score, x.reasons) for x in traced
    ]
    assert not hasattr(plain, "selection_sources")
    assert set(_runs(traced)) == {
        "premarket_gap", "kis_volume_surge", "kis_institutional_buying", "kis_new_highs",
        "kis_fluctuation_rank", "kis_foreign_buying", "naver_volume_rank", "naver_rise_rank",
        "naver_new_high", "theme_news", "llm_news",
    }
    assert _runs(traced)["kis_volume_surge"]["outcome"] == "unknown_nonempty"
    assert _runs(traced)["kis_new_highs"]["outcome"] == "unknown_empty"


def test_capture_marks_integrated_cache_fallback_without_storing_trace(monkeypatch):
    sources = {"screen_volume_surge": [stock()]}
    s = screener(monkeypatch, sources)
    first = run(s, capture_selection_sources=True)
    assert first.selection_sources["fallback_used"] is False
    sources.clear()

    fallback = run(s, capture_selection_sources=True)

    assert fallback.selection_sources["fallback_used"] is True
    assert fallback[0].score == first[0].score
    assert not hasattr(s._cache["screen_all"][0], "selection_sources")


def test_capture_preserves_theme_precedence_and_sync_call_count(monkeypatch):
    s = screener(monkeypatch, {})
    calls = {"theme": 0, "llm": 0}

    class Theme:
        def get_all_stock_sentiments(self):
            calls["theme"] += 1
            return {"000001": {"direction": "bullish", "impact": 5, "reason": "합성"}}

    async def llm(*_args, **_kwargs):
        calls["llm"] += 1
        return [stock("000002")]

    s.extract_stocks_from_news = llm
    out = run(s, capture_selection_sources=True, theme_detector=Theme(), llm_manager=object(), news_titles=["x"])

    assert calls == {"theme": 3, "llm": 0}
    assert _runs(out)["theme_news"]["returned_count"] == 1
    assert _runs(out)["llm_news"]["outcome"] == "not_called"


def test_provider_cached_inputs_are_distinguished_from_successful_empty_http(monkeypatch):
    from src.data.providers.kis_market_data import KISMarketData
    from src.analytics.selection_source_status import SourceCapture

    provider = object.__new__(KISMarketData)
    provider._cache = {"fluctuation_rank": []}
    provider._cache_ts = {"fluctuation_rank": m.datetime.now()}
    provider._cache_maxsize = 2000
    capture = SourceCapture()
    result = asyncio.run(capture.call("kis_fluctuation_rank", provider.fetch_fluctuation_rank))
    observed = capture.wrap(result)

    assert result == []
    assert _runs(observed)["kis_fluctuation_rank"]["outcome"] == "cached_empty"


class _Response:
    def __init__(self, status, payload):
        self.status, self.payload = status, payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def json(self):
        return self.payload


def test_two_market_kis_source_reports_partial_when_one_market_fails(monkeypatch):
    from src.analytics.selection_source_status import SourceCapture
    from src.data.providers import kis_market_data as provider_module
    from src.data.providers.kis_market_data import KISMarketData

    item = {"mksc_shrn_iscd": "000001", "hts_kor_isnm": "합성", "ntby_qty": "1",
            "acml_vol": "10000", "prdy_vol": "5000", "ntby_tr_pbmn": "1",
            "stck_prpr": "10000", "prdy_ctrt": "1"}
    replies = iter([_Response(500, {"rt_cd": "1"}), _Response(200, {"rt_cd": "0", "output": [item]})])
    provider = object.__new__(KISMarketData)
    provider._cache, provider._cache_ts, provider._cache_maxsize = {}, {}, 2000
    provider._token_manager = SimpleNamespace(base_url="http://synthetic")

    class Session:
        def get(self, *_args, **_kwargs):
            return next(replies)

    async def session():
        return Session()

    async def headers(_tr_id):
        return {}

    async def acquire():
        return None

    provider._get_session, provider._get_headers = session, headers
    monkeypatch.setattr(provider_module.kis_rate_limit, "acquire", acquire)
    s = object.__new__(m.StockScreener)
    s._cache, s._cache_time, s._cache_ttl = {}, {}, 300
    s._kis_market_data, s.min_trading_value, s.max_change_pct = provider, 100_000_000, 15.0
    capture = SourceCapture()
    stocks = asyncio.run(capture.call("kis_institutional_buying", s.screen_institutional_buying))
    observed = capture.wrap(stocks)

    row = _runs(observed)["kis_institutional_buying"]
    assert len(stocks) == 1
    assert (row["outcome"], row["payload_count"], row["failure_count"], row["failure_reason"]) == (
        "partial", 1, 1, "http"
    )
