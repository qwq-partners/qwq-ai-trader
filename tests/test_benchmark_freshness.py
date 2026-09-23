"""오래된/손상된 KOSPI 자료를 실제 레짐·MRS에 쓰는 회귀를 잡는다."""

import asyncio
from datetime import date, datetime, timezone

import pandas as pd
import pytest

from src.indicators.technical import TechnicalIndicators
from src.signals.screener import swing_screener as mod


class Clock(datetime):
    current = datetime(2026, 9, 23, 12)

    @classmethod
    def now(cls, tz=None):
        if tz is None:
            return cls.current
        from zoneinfo import ZoneInfo
        return cls.current.replace(tzinfo=ZoneInfo("Asia/Seoul")).astimezone(tz)


@pytest.fixture
def screener(monkeypatch):
    # 생성자의 수급 공급자는 import 시 운영 캐시를 생성하므로 순수 계산 상태만 구성.
    monkeypatch.setattr(mod, "datetime", Clock)
    Clock.current = datetime(2026, 9, 23, 12)
    obj = mod.SwingScreener.__new__(mod.SwingScreener)
    obj._broker = None
    obj._kospi_closes = []
    obj._kospi_loaded_at = None
    obj._kospi_last_bar_date = None
    obj._indicators = TechnicalIndicators()
    return obj


def frame(last="2026-09-22", *, rising=True):
    values = [100.0 + i for i in range(60)]
    if not rising:
        values.reverse()
    return pd.DataFrame({"Close": values}, index=pd.bdate_range(end=last, periods=60))


def load(monkeypatch, screener, primary, fallback=None):
    def fetch(symbol, start_date):
        if symbol == "KS11":
            return primary
        if symbol == "YAHOO:^KS11":
            return fallback
        raise AssertionError(f"예상 밖 데이터 소스: {symbol}")
    monkeypatch.setattr(screener, "_fetch_fdr_data", fetch)
    asyncio.run(screener._load_benchmark_index())


@pytest.mark.parametrize("rising", [True, False])
def test_stale_trend_is_not_a_current_regime(monkeypatch, screener, rising):
    load(monkeypatch, screener, frame("2026-09-17", rising=rising))
    assert screener.get_market_regime() == "neutral"
    assert screener.get_kospi_change() == {"c5": 0.0, "c20": 0.0, "level": 0.0}
    assert screener.get_benchmark_status()["status"] == "stale"


@pytest.mark.parametrize("now,last", [
    (datetime(2026, 9, 23, 8), "2026-09-22"),
    (datetime(2026, 9, 23, 12), "2026-09-23"),
    (datetime(2026, 9, 21, 8), "2026-09-18"),
    (datetime(2026, 9, 27, 12), "2026-09-23"),
    (datetime(2026, 9, 28, 8), "2026-09-23"),
])
def test_today_or_previous_kr_session_is_usable(monkeypatch, screener, now, last):
    Clock.current = now
    load(monkeypatch, screener, frame(last))
    status = screener.get_benchmark_status()
    assert status["status"] == "fresh"
    assert status["source"] == "FDR:KS11"
    assert status["last_bar_date"] == date.fromisoformat(last)
    assert status["loaded_at"] == now
    assert status["loaded_at"].tzinfo is None
    assert screener.get_market_regime() == "bull"
    assert screener.get_kospi_change() == {"c5": 3.25, "c20": 14.39, "level": 159.0}


@pytest.mark.parametrize("bad_price", [0, -1, float("nan"), float("inf"), -float("inf"), "bad"])
def test_invalid_prices_are_not_consumed(monkeypatch, screener, bad_price):
    data = frame().astype(object)
    data.iloc[20, 0] = bad_price
    load(monkeypatch, screener, data)
    assert screener.get_market_regime() == "neutral"
    assert screener.get_kospi_change()["level"] == 0
    assert screener.get_benchmark_status()["status"] == "unknown"


@pytest.mark.parametrize("kind", ["reversed", "duplicate", "undated", "nat", "short"])
def test_untrustworthy_history_is_not_consumed(monkeypatch, screener, kind):
    data = frame()
    if kind == "reversed":
        data = data.iloc[::-1]
    elif kind == "duplicate":
        data.index = list(data.index[:-1]) + [data.index[-2]]
    elif kind == "undated":
        data.index = range(len(data))
    elif kind == "nat":
        data.index = list(data.index[:-1]) + [pd.NaT]
    else:
        data = data.iloc[-20:]
    load(monkeypatch, screener, data)
    assert screener.get_market_regime() == "neutral"
    assert screener.get_benchmark_status()["status"] == "unknown"


def test_future_bar_is_not_consumed(monkeypatch, screener):
    load(monkeypatch, screener, frame("2026-09-28"))
    assert screener.get_market_regime() == "neutral"
    assert screener.get_benchmark_status()["status"] == "future"


def test_holiday_bar_is_not_a_market_session(monkeypatch, screener):
    Clock.current = datetime(2026, 9, 25, 12)
    load(monkeypatch, screener, frame("2026-09-24"))
    assert screener.get_market_regime() == "neutral"
    assert screener.get_benchmark_status()["status"] == "unknown"


def test_stale_primary_uses_valid_yahoo_history(monkeypatch, screener):
    load(monkeypatch, screener, frame("2026-09-17"), frame(rising=False))
    assert screener.get_market_regime() == "bear"
    status = screener.get_benchmark_status()
    assert status["status"] == "fresh"
    assert status["source"] == "FDR:YAHOO:^KS11"
    assert status["last_bar_date"] == date(2026, 9, 22)
    assert screener.get_kospi_change()["level"] == 100


def test_all_sources_fail_without_using_stock_0001(monkeypatch, screener):
    stock_requests = []

    class StockBroker:
        async def get_daily_prices(self, *args, **kwargs):
            stock_requests.append(args)
            raise AssertionError("주식 itemchart를 지수 자료로 쓰면 안 됨")
    screener._broker = StockBroker()
    load(monkeypatch, screener, frame())
    load(monkeypatch, screener, None)
    assert screener._kospi_closes == []
    assert screener.get_benchmark_status()["status"] == "missing"
    assert screener.get_market_regime() == "neutral"
    assert stock_requests == []


@pytest.mark.parametrize("fallback", ["stale", "future", "invalid"])
def test_yahoo_fallback_must_pass_same_validation(monkeypatch, screener, fallback):
    data = frame("2026-09-17" if fallback == "stale" else "2026-09-28")
    if fallback == "invalid":
        data = frame()
        data.iloc[-1, 0] = float("nan")
    load(monkeypatch, screener, None, data)
    assert screener._kospi_closes == []
    assert screener.get_market_regime() == "neutral"
    assert screener.get_benchmark_status()["status"] == {
        "stale": "stale", "future": "future", "invalid": "unknown",
    }[fallback]


def test_fetch_errors_clear_previously_usable_history(monkeypatch, screener):
    load(monkeypatch, screener, frame())

    def unavailable(symbol, start_date):
        raise RuntimeError("합성 공급자 장애")

    monkeypatch.setattr(screener, "_fetch_fdr_data", unavailable)
    asyncio.run(screener._load_benchmark_index())
    assert screener.get_benchmark_status()["status"] == "missing"
    assert screener._kospi_closes == []
    assert screener.get_kospi_change()["level"] == 0


def test_undated_in_memory_history_is_unknown(screener):
    screener._kospi_closes = [100.0 + i for i in range(60)]
    assert screener.get_benchmark_status()["status"] == "unknown"
    assert screener.get_market_regime() == "neutral"


def test_consumers_recheck_age_and_kst_date(monkeypatch, screener):
    load(monkeypatch, screener, frame())
    assert screener.get_benchmark_status(datetime(2026, 9, 22, 16, tzinfo=timezone.utc))["status"] == "fresh"
    Clock.current = datetime(2026, 9, 28, 8)
    assert screener.get_benchmark_status()["status"] == "stale"
    assert screener.get_market_regime() == "neutral"
    assert screener.get_kospi_change()["level"] == 0


@pytest.mark.parametrize("aged", [False, True])
def test_mrs_uses_only_fresh_benchmark(monkeypatch, screener, aged):
    load(monkeypatch, screener, frame())
    if aged:
        Clock.current = datetime(2026, 9, 28, 8)
    data = frame()
    data["Open"] = data["Close"]
    data["High"] = data["Close"] + 1
    data["Low"] = data["Close"] - 1
    data["Volume"] = 100_000_000
    monkeypatch.setattr(screener, "_fetch_fdr_data", lambda symbol, start: data)
    results = asyncio.run(screener._calculate_all_indicators([{"symbol": "005930", "name": "합성"}]))
    assert len(results) == 1
    assert ("mrs" in results[0]["indicators"]) is (not aged)


def test_fdr_yahoo_symbol_routes_to_index_reader_without_network(monkeypatch):
    import FinanceDataReader.data as fdr_data
    captured = {}
    expected = frame()

    class Reader:
        def __init__(self, symbol, start, end):
            captured["symbol"] = symbol

        def read(self):
            return expected

    monkeypatch.setattr(fdr_data, "YahooDailyReader", Reader)
    actual = mod.SwingScreener._fetch_fdr_data("YAHOO:^KS11", "2025-09-23")
    assert actual is expected
    assert captured["symbol"] == "^KS11"
