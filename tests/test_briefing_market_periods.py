"""Market-brief period and source evidence regressions; all data is synthetic."""

import asyncio
import sys
import types
from datetime import datetime, timedelta, timezone

import pandas as pd
from src.experts.types import ExpertConfig
from src.experts.us_market_expert import USMarketExpert
from src.experts.weekend_signal_expert import WeekendSignalExpert


def _us_expert(monkeypatch, sectors):
    expert = USMarketExpert(ExpertConfig())

    async def _indices():
        return {"SPY": {"vs_ma50_pct": 0.0, "vs_ma200_pct": 0.0}}

    async def _sectors():
        return sectors

    async def _vix():
        return 18.0

    async def _earnings():
        return {}

    async def _semis():
        return {"sox_5d_pct": 0.0}

    monkeypatch.setattr(expert, "_fetch_index_states", _indices)
    monkeypatch.setattr(expert, "_fetch_sector_rs", _sectors)
    monkeypatch.setattr(expert, "_fetch_vix", _vix)
    monkeypatch.setattr(expert, "_fetch_earnings_season", _earnings)
    monkeypatch.setattr(expert, "_fetch_semis_state", _semis)
    return expert


def test_us_sector_returns_publish_actual_session_period_and_provenance(monkeypatch):
    expert = _us_expert(monkeypatch, (
        {"XLE": 2.5},
        {"XLE": {"as_of": "2026-10-20", "source": "yfinance:XLE", "start": "2026-09-23", "session_count": 19}},
    ))

    opinion = asyncio.run(expert._analyze())

    assert opinion.raw_evidence["sector_returns"] == {"XLE": 2.5}
    assert opinion.raw_evidence["sector_periods"]["XLE"]["session_count"] == 19
    assert "19세션" in opinion.key_findings[-1]


def test_us_sector_mock_without_metadata_is_explicitly_unknown_period(monkeypatch):
    expert = _us_expert(monkeypatch, {"XLE": 2.5})

    opinion = asyncio.run(expert._analyze())

    assert opinion.raw_evidence["sector_periods"]["XLE"]["label"] == "구간 미확인"
    assert "구간 미확인" in opinion.key_findings[-1]


def test_weekend_kr_signal_finding_is_factual_not_gap_prediction(monkeypatch):
    expert = WeekendSignalExpert(ExpertConfig())

    async def _signals():
        return {
            "kr_futures_pct": 2.0,
            "kr_futures_source": "KIS:A01609",
            "kr_futures_session": "night",
            "kr_futures_as_of": "2026-10-20T06:00:00",
            "kr_futures_as_of_ttl_seconds": 99999999,
        }

    monkeypatch.setattr(expert, "_fetch_all_signals", _signals)
    opinion = asyncio.run(expert._analyze())

    kr_finding = next(f for f in opinion.key_findings if "KIS:A01609" in f)
    assert "직전 야간세션 등락" in kr_finding
    assert "갭업" not in kr_finding and "갭다운" not in kr_finding


def test_sox_five_day_return_is_unavailable_with_fewer_than_six_bars(monkeypatch):
    """A three-bar fallback must never be presented as a five-session SOX return."""
    fake_yf = types.ModuleType("yfinance")

    class _Ticker:
        def __init__(self, ticker):
            self.ticker = ticker

        def history(self, **kwargs):
            return pd.DataFrame({"Close": [100.0, 102.0, 101.0]})

    fake_yf.Ticker = _Ticker
    monkeypatch.setitem(sys.modules, "yfinance", fake_yf)
    expert = USMarketExpert(ExpertConfig())

    state = asyncio.run(expert._fetch_semis_state())

    assert state["sox_5d_pct"] is None


def test_real_sector_fetch_counts_selected_window_not_all_received_bars(monkeypatch):
    fake_yf = types.ModuleType("yfinance")
    class Ticker:
        def __init__(self, ticker):
            pass
        def history(self, **kwargs):
            return pd.DataFrame({"Close": list(range(100, 125))},
                                index=pd.bdate_range("2026-09-01", periods=25))
    fake_yf.Ticker = Ticker
    monkeypatch.setitem(sys.modules, "yfinance", fake_yf)
    values, periods = asyncio.run(USMarketExpert(ExpertConfig())._fetch_sector_rs())
    assert periods["XLE"]["session_count"] == 19
    assert values["XLE"] == round((124 / 105 - 1) * 100, 2)


def test_common_period_is_unknown_when_one_of_top_sectors_has_no_metadata(monkeypatch):
    expert = _us_expert(monkeypatch, (
        {"XLE": 2.5, "XLV": 2.0}, {"XLE": {"session_count": 19}},
    ))
    opinion = asyncio.run(expert._analyze())
    assert "구간 미확인" in opinion.key_findings[-1]


def test_nikkei_proxy_is_not_described_as_observed_kr_night_session(monkeypatch):
    expert = WeekendSignalExpert(ExpertConfig())
    async def signals():
        return {"kr_futures_pct": 2.0, "kr_futures_source": "NKD_proxy",
                "kr_futures_session": "yfinance_1h_proxy"}
    monkeypatch.setattr(expert, "_fetch_all_signals", signals)
    opinion = asyncio.run(expert._analyze())
    finding = next(f for f in opinion.key_findings if "NKD_proxy" in f)
    assert "대체지표" in finding and "야간세션" not in finding


def test_macro_actual_two_bars_preserve_rate_decrease_and_source_date(monkeypatch):
    from src.experts.macro_economist import MacroEconomist
    fake_yf = types.ModuleType("yfinance")
    class Ticker:
        def __init__(self, ticker):
            self.ticker = ticker
        def history(self, **kwargs):
            if self.ticker != "^TNX":
                return pd.DataFrame()
            return pd.DataFrame({"Close": [5.15, 5.12]},
                                index=pd.to_datetime(["2026-10-19", "2026-10-20"]))
    fake_yf.Ticker = Ticker
    monkeypatch.setitem(sys.modules, "yfinance", fake_yf)
    expert = MacroEconomist(ExpertConfig())
    values = asyncio.run(expert._fetch_yfinance_indicators())
    evidence = expert._indicator_evidence["us10y"]
    assert values["us10y"] == 5.12
    assert evidence["change_bp"] == -3.0
    assert evidence["source"] == "yfinance:^TNX"
    assert evidence["as_of"].startswith("2026-10-20")


def test_nkd_fallback_preserves_timezone_and_analyzes_all_signals(monkeypatch):
    from src.data.providers import kis_market_data
    fake_yf = types.ModuleType("yfinance")
    stamp = datetime.now(timezone.utc) - timedelta(minutes=5)
    class Ticker:
        def __init__(self, ticker):
            pass
        def history(self, **kwargs):
            return pd.DataFrame({"Close": [100., 104.]},
                                index=pd.DatetimeIndex([stamp - timedelta(hours=1), stamp]))
    class NoKISQuote:
        async def get_night_futures_quote(self):
            return None
    fake_yf.Ticker = Ticker
    monkeypatch.setitem(sys.modules, "yfinance", fake_yf)
    monkeypatch.setattr(kis_market_data, "get_kis_market_data", lambda: NoKISQuote())
    expert = WeekendSignalExpert(ExpertConfig())
    opinion = asyncio.run(expert._analyze())
    signals = opinion.raw_evidence["signals"]
    assert signals["kr_futures_as_of"] == stamp.isoformat()
    assert signals["kr_futures_source"] == "NKD=F(proxy×0.7)"
    assert opinion.error is None and opinion.data_status == "ok"
    assert any("대체지표" in f for f in opinion.key_findings)
