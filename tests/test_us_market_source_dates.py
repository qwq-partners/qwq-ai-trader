import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

from src.analytics import daily_report as dr
from src.analytics.us_report_data import observe_us_quote
from src.data.providers.us_market_data import USMarketData


def test_v8_flat_close_uses_its_matching_timestamp_only():
    earlier = datetime(2026, 10, 20, 20, tzinfo=timezone.utc).timestamp()
    close = datetime(2026, 10, 21, 20, tzinfo=timezone.utc).timestamp()
    result = {}

    USMarketData._parse_v8_spark_data({
        "SMH": {
            "close": [100.0, 101.0],
            "timestamp": [earlier, close],
            "chartPreviousClose": 99.0,
        },
    }, result)

    assert result["SMH"]["price"] == 101.0
    assert result["SMH"]["report_market_time"] == close
    assert result["SMH"]["report_market_time_kind"] == "daily_bar"


def test_v8_never_attaches_a_bar_time_to_nested_regular_market_price():
    bar_time = datetime(2026, 10, 21, 20, tzinfo=timezone.utc).timestamp()
    result = {}

    USMarketData._parse_v8_spark_data({
        "spark": {"result": [{
            "symbol": "SMH",
            "response": [{
                "timestamp": [bar_time],
                "meta": {
                    "regularMarketPrice": 101.0,
                    "chartPreviousClose": 99.0,
                },
            }],
        }]},
    }, result)

    assert result["SMH"]["price"] == 101.0
    assert result["SMH"]["report_market_time"] is None
    assert result["SMH"]["report_market_time_kind"] is None


def test_v8_nested_regular_market_price_keeps_its_explicit_regular_time():
    market_time = datetime(2026, 10, 21, 20, tzinfo=timezone.utc).timestamp()
    result = {}

    USMarketData._parse_v8_spark_data({
        "spark": {"result": [{
            "symbol": "SMH",
            "response": [{"meta": {
                "regularMarketPrice": 101.0,
                "chartPreviousClose": 99.0,
                "regularMarketTime": market_time,
            }}],
        }]},
    }, result)

    assert result["SMH"]["report_market_time"] == market_time
    assert result["SMH"]["report_market_time_kind"] == "regular_market"


def test_v8_rejects_unpaired_or_invalid_times_from_date_evidence():
    close = datetime(2026, 10, 21, 20, tzinfo=timezone.utc).timestamp()
    future = datetime(2026, 10, 23, 20, tzinfo=timezone.utc).timestamp()
    result = {}

    USMarketData._parse_v8_spark_data({
        "MISMATCH": {"close": [101.0], "timestamp": [close, future], "chartPreviousClose": 99.0},
        "BOOL": {"close": [101.0], "timestamp": [True], "chartPreviousClose": 99.0},
        "NAN": {"close": [101.0], "timestamp": [float("nan")], "chartPreviousClose": 99.0},
        "FUTURE": {"close": [101.0], "timestamp": [future], "chartPreviousClose": 99.0},
    }, result)

    for symbol in ("MISMATCH", "BOOL", "NAN"):
        assert result[symbol]["report_market_time"] is None
        assert result[symbol]["report_market_time_kind"] is None
    assert observe_us_quote(
        result["FUTURE"], now=datetime(2026, 10, 22, 8, 0, tzinfo=timezone.utc),
    ) is None


def test_morning_brief_uses_only_each_groups_complete_same_day_evidence(monkeypatch):
    now = datetime(2026, 10, 22, 8, 0, tzinfo=timezone.utc)
    close = datetime(2026, 10, 21, 20, tzinfo=timezone.utc).timestamp()
    older = datetime(2026, 10, 20, 20, tzinfo=timezone.utc).timestamp()
    monkeypatch.setattr(dr, "_now", lambda: now)
    monkeypatch.setattr(dr, "get_telegram_notifier", lambda: SimpleNamespace())
    monkeypatch.setattr(dr, "get_screener", lambda: SimpleNamespace())
    monkeypatch.setattr(dr, "get_theme_detector", lambda: SimpleNamespace())
    monkeypatch.setattr(dr, "NewsCollector", lambda: SimpleNamespace())
    generator = dr.DailyReportGenerator()

    record = asyncio.run(generator.build_morning_brief(
        quotes={
            "^GSPC": {"price": 6888.0, "change_pct": 0.55, "report_market_time": close},
            "^IXIC": {"price": 23100.0, "change_pct": 0.91, "report_market_time": close},
            "^DJI": {"price": 47100.0, "change_pct": 0.12, "report_market_time": close},
            "^SOX": {"price": 7100.0, "change_pct": 1.24, "report_market_time": close},
            "^VIX": {"price": 18.4, "change_pct": -2.1, "report_market_time": close},
            "NVDA": {"price": 205.0, "change_pct": 2.0},
            "AAPL": {"price": 265.0, "change_pct": -0.3, "report_market_time": close},
            "MSFT": {"price": 510.0, "change_pct": 0.2, "report_market_time": close},
            "GOOG": {"price": 250.0, "change_pct": 0.1, "report_market_time": close},
            "META": {"price": 610.0, "change_pct": 0.4, "report_market_time": close},
            "AMZN": {"price": 245.0, "change_pct": 0.3, "report_market_time": close},
            "TSLA": {"price": 440.0, "change_pct": -0.2, "report_market_time": close},
            "SMH": {"price": 350.0, "change_pct": 1.8, "report_market_time": close},
            "XLK": {"price": 290.0, "change_pct": 0.8, "report_market_time": older},
            "XLV": {"price": 150.0, "change_pct": 0.3, "report_market_time": close},
            "XBI": {"price": 90.0, "change_pct": -0.2, "report_market_time": older},
            "XLE": {"price": 85.0, "change_pct": 0.5, "report_market_time": close},
            "URNM": {"price": 70.0, "change_pct": -0.1, "report_market_time": older},
            "LIT": {"price": 45.0, "change_pct": 0.2, "report_market_time": close},
            "ITA": {"price": 190.0, "change_pct": 0.1, "report_market_time": older},
        },
        sector_signals={}, avg_pct=0.0, us_date_str="2026.10.21", mood="보합",
    ))

    groups = {entry["name"]: entry for entry in record["inputs"][:3]}
    assert groups["미국 지수"]["as_of"] == "2026.10.21"
    assert groups["미국 지수"]["valid"] is True
    assert groups["빅테크"]["as_of"] is None
    assert groups["빅테크"]["valid"] is False
    assert groups["주요 ETF"]["as_of"] is None
    assert groups["주요 ETF"]["valid"] is False
