import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

from src.analytics import daily_report as dr
from src.analytics import morning_brief_eval as mbe
from src.analytics.us_report_data import observe_us_quote
from src.data.providers.us_market_data import USMarketData


class _Response:
    status = 200

    def __init__(self, payload):
        self.payload = payload

    async def json(self):
        return self.payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False


class _Session:
    def __init__(self, payload):
        self.payload = payload

    def get(self, *_args, **_kwargs):
        return _Response(self.payload)


def test_v7_keeps_legacy_time_raw_while_exposing_clean_report_time(monkeypatch):
    provider = USMarketData()
    payload = {"quoteResponse": {"result": [{
        "symbol": "^GSPC", "regularMarketPrice": 6000.0,
        "regularMarketChange": 30.0, "regularMarketChangePercent": 0.5,
        "regularMarketTime": True,
    }]}}

    async def fake_session():
        return _Session(payload)

    monkeypatch.setattr(provider, "_get_session", fake_session)
    quote = asyncio.run(provider._fetch_via_v7())["^GSPC"]

    assert quote["market_time"] is True
    assert "market_time_kind" not in quote
    assert quote["report_market_time"] is None
    assert quote["report_market_time_kind"] is None


def test_v8_keeps_legacy_market_time_absent_and_preserves_report_time():
    close = datetime(2026, 10, 21, 20, tzinfo=timezone.utc).timestamp()
    result = {}
    USMarketData._parse_v8_spark_data({
        "SMH": {"close": [101.0], "timestamp": [close], "chartPreviousClose": 99.0},
    }, result)

    quote = result["SMH"]
    assert "market_time" not in quote
    assert "market_time_kind" not in quote
    assert quote["report_market_time"] == close
    assert quote["report_market_time_kind"] == "daily_bar"


def test_reporting_prefers_clean_report_time_over_legacy_market_time(monkeypatch):
    close = datetime(2026, 10, 21, 20, tzinfo=timezone.utc).timestamp()
    quote = {
        "price": 6000.0, "change_pct": 0.5,
        "market_time": True, "report_market_time": close,
    }
    now = datetime(2026, 10, 22, 8, 0, tzinfo=timezone.utc)

    assert observe_us_quote(quote, now=now)["date"] == "2026.10.21"
    monkeypatch.setattr(dr, "_now", lambda: now)
    assert dr.resolve_us_quote_basis({"^GSPC": quote})["as_of"] == "2026.10.21"


def test_collect_brief_actuals_keeps_report_zero_and_omits_report_none(monkeypatch):
    monkeypatch.setattr(dr, "get_telegram_notifier", lambda: SimpleNamespace())
    monkeypatch.setattr(dr, "get_screener", lambda: SimpleNamespace())
    monkeypatch.setattr(dr, "get_theme_detector", lambda: SimpleNamespace())
    monkeypatch.setattr(dr, "NewsCollector", lambda: SimpleNamespace())

    class KMD:
        async def fetch_index_price(self, code):
            return {
                "change_pct": 99.0,
                "report_values": {"price": None, "change": None, "open": None,
                                  "change_pct": 0.0 if code == "0001" else None},
            }

        async def fetch_sector_indices(self):
            return [
                {"name": "제로", "report_values": {"change_pct": 0.0}},
                {"name": "결측", "report_values": {"change_pct": None}},
            ]

    generator = dr.DailyReportGenerator()
    generator._kis_market_data = KMD()
    actual = asyncio.run(generator._collect_brief_actuals())

    assert actual["kospi"] == {"close_change_pct": 0.0}
    assert "kosdaq" not in actual
    assert actual["sectors"] == {"제로": 0.0}


def test_audit_only_brief_is_ledgered_but_not_a_forecast_sample(tmp_path):
    audit = mbe.evaluate(
        {"kr_date": "2026-10-27", "claims": {}, "scope": "us_close_only"},
        {"date": "2026-10-27", "kospi": {"close_change_pct": 1.0}},
        dispatched=True,
    )

    assert audit["prediction_eligible"] is False
    assert audit["evaluated"] is False
    assert "전망 주장 없음" in audit["reason"]
    assert audit["close_direction"]["hit"] is None

    path = tmp_path / "brief.jsonl"
    mbe.append_ledger(path, {
        "date": "2026-10-26", "prediction_eligible": True,
        "open_direction": {"hit": True}, "close_direction": {"hit": True},
        "expert_direction": {"hit": None}, "sectors": [],
    })
    mbe.append_ledger(path, audit)

    summary = mbe.summarize(path)
    assert summary["ledger_rows"] == 2
    assert summary["samples"] == 1
    assert "예측 표본 1건 · 평가원장 2건" in mbe.summary_line(path)
