"""History must retain requested session gaps and never relabel old snapshots."""
import importlib
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
import sys
from types import ModuleType

import pytest


@pytest.fixture
def history(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    module = importlib.import_module("src.data.providers.supply_score")
    monkeypatch.setattr(module, "CACHE_DIR", tmp_path)
    dates = ["20261016", "20261015", "20261014", "20261013", "20261012"]
    monkeypatch.setattr(module, "_get_trading_dates", lambda n: dates[:n])
    return module, module.SupplyScoreProvider(), dates, tmp_path


@pytest.mark.asyncio
async def test_old_intraday_snapshot_cannot_fill_five_missing_days(history, monkeypatch):
    module, provider, dates, root = history
    old = root / "supply_demand_20261012.json"
    old.write_text(json.dumps({"005930": {"foreign_net_buy": 100, "inst_net_buy": 100}}))
    before = old.read_bytes()
    monkeypatch.setattr(provider, "_fetch_pykrx_day", lambda day: {})
    assert await provider.ensure_loaded() is False
    assert provider._daily == {}
    assert provider.get_score("005930") == 0
    assert old.read_bytes() == before


def test_legacy_daily_cache_not_promoted_to_dated_evidence(history):
    _, provider, dates, root = history
    (root / f"supply_daily_{dates[0]}.json").write_text('{"005930":{"foreign":100,"inst":100}}')
    assert provider._load_day(dates[0]) is None


@pytest.mark.asyncio
async def test_missing_session_breaks_streak_and_prevents_five_day_claim(history, monkeypatch):
    _, provider, dates, _ = history
    monkeypatch.setattr(provider, "_fetch_pykrx_day", lambda day: {} if day == dates[1]
                        else {"005930": {"foreign": 100, "inst": 100}})
    await provider.ensure_loaded()
    meta = provider.get_meta("005930")
    assert meta["foreign_streak"] == 1
    assert meta["inst_streak"] == 1
    assert meta["is_accelerating"] is False
    assert meta["foreign_5d"] is None
    assert meta["total_5d"] is None
    assert meta["days_observed"] == {"foreign": 4, "inst": 4}


@pytest.mark.asyncio
async def test_full_dated_history_preserves_scoring_and_roundtrips(history, monkeypatch):
    module, provider, dates, _ = history
    monkeypatch.setattr(provider, "_fetch_pykrx_day", lambda day: {"005930": {"foreign": 100, "inst": 0}})
    await provider.ensure_loaded()
    assert provider.get_meta("005930")["foreign_5d"] == 500
    assert provider.get_meta("005930")["foreign_streak"] == 5
    fresh = module.SupplyScoreProvider()
    monkeypatch.setattr(fresh, "_fetch_pykrx_day", lambda day: pytest.fail("verified date cache should avoid fetch"))
    assert await fresh.ensure_loaded()
    assert fresh.get_score("005930") == provider.get_score("005930")
    envelope = json.loads(provider._cache_path(dates[0]).read_text())
    assert envelope["source_date"] == dates[0]
    envelope["source_date"] = dates[1]
    provider._cache_path(dates[0]).write_text(json.dumps(envelope))
    assert fresh._load_day(dates[0]) is None


@pytest.mark.asyncio
async def test_new_window_without_data_clears_old_readiness(history, monkeypatch):
    module, provider, dates, _ = history
    monkeypatch.setattr(provider, "_fetch_pykrx_day", lambda day: {"005930": {"foreign": 100, "inst": 100}})
    await provider.ensure_loaded()
    assert provider.get_score("005930") > 0
    monkeypatch.setattr(module, "_get_trading_dates", lambda n: ["20261102"])
    monkeypatch.setattr(provider, "_fetch_pykrx_day", lambda day: {})
    assert await provider.ensure_loaded() is False
    assert not provider.is_ready
    assert provider.get_score("005930") == 0


@pytest.mark.asyncio
async def test_kis_missing_side_not_normalized_zero_and_wrong_date_rejected(history, monkeypatch):
    module, provider, dates, _ = history
    monkeypatch.setattr(module, "_get_trading_dates", lambda n: dates[:1])
    stamp = datetime.now(timezone.utc).isoformat()
    def evidence(value, day="2026-10-16"):
        return {"value": value, "source": "FHKST01010900", "source_date": day,
                "captured_at": stamp, "status": "observed"}
    broker = SimpleNamespace(fetch_batch_investor_daily=AsyncMock(return_value={
        "005930": {"foreign_net_buy": 10, "inst_net_buy": 0,
                   "_supply_evidence": {"foreign": evidence(10), "institution": None}},
        "000660": {"foreign_net_buy": 999, "inst_net_buy": 999,
                   "_supply_evidence": {"foreign": evidence(999, "2026-10-15"), "institution": None}},
    }))
    monkeypatch.setattr(provider, "_fetch_pykrx_day", lambda day: {})
    await provider.ensure_loaded_from_kis(broker, ["005930", "000660"])
    assert provider._daily[dates[0]]["005930"]["inst"] is None
    assert "000660" not in provider._daily[dates[0]]
    assert provider.get_meta("005930")["inst_5d"] is None


@pytest.mark.parametrize("date", ["../bad", "20261301", "20260230", "", None])
def test_invalid_cache_date_rejected(history, date):
    _, provider, _, _ = history
    with pytest.raises(ValueError):
        provider._cache_path(date)


@pytest.mark.asyncio
async def test_partial_candidate_does_not_inherit_other_symbols_coverage(history, monkeypatch):
    _, provider, dates, _ = history
    monkeypatch.setattr(provider, "_fetch_pykrx_day", lambda day: {
        "000660": {"foreign": 10, "inst": 10},
        **({"005930": {"foreign": 0, "inst": -2}} if day == dates[0] else {}),
    })
    await provider.ensure_loaded()
    meta = provider.get_meta("005930")
    assert meta["days_loaded"] == 5
    assert meta["days_observed"] == {"foreign": 1, "inst": 1}
    assert meta["foreign_5d"] is None
    assert meta["is_accelerating"] is False
    assert provider.get_score("005930") == 0


@pytest.mark.parametrize("raw", [True, 1.5, float("nan"), "", "bad", 10**13])
def test_invalid_history_quantity_stays_unknown(history, raw):
    _, provider, _, _ = history
    assert provider._valid_day({"005930": {"foreign": raw, "inst": -5}}) == {
        "005930": {"foreign": None, "inst": -5}}


def test_kis_history_cannot_precede_its_source_date(history):
    _, provider, dates, _ = history
    proof = {"value": 10, "source": "FHKST01010900", "source_date": "2026-10-16",
             "captured_at": "2026-10-14T00:00:00+00:00", "status": "observed"}
    rows = {"005930": {"foreign_net_buy": 10, "inst_net_buy": 0,
                       "_supply_evidence": {"foreign": proof, "institution": None}}}
    assert provider._kis_day(rows, dates[0]) == {}


def test_pykrx_float_column_preserves_integral_values_without_filling_nan(history, monkeypatch):
    import pandas as pd
    _, provider, dates, _ = history
    stock = ModuleType("pykrx.stock")
    def query(start, end, market, investor):
        assert start == end == dates[0]
        if market == "KOSDAQ":
            return pd.DataFrame()
        return pd.DataFrame({"순매수거래량": [100.0, float("nan"), -200.0, 3.25]},
                            index=["000001", "000002", "000003", "000004"])
    stock.get_market_net_purchases_of_equities = query
    package = ModuleType("pykrx")
    package.stock = stock
    monkeypatch.setitem(sys.modules, "pykrx", package)
    monkeypatch.setitem(sys.modules, "pykrx.stock", stock)
    data = provider._valid_day(provider._fetch_pykrx_day(dates[0]))
    assert data == {"000001": {"foreign": 100, "inst": 100},
                    "000003": {"foreign": -200, "inst": -200}}


@pytest.mark.asyncio
async def test_fallback_preserves_window_when_kst_date_rolls_over(history, monkeypatch):
    module, provider, dates, _ = history
    calls = []
    def window(n):
        calls.append(n)
        return dates if len(calls) == 1 else ["20261102"]
    monkeypatch.setattr(module, "_get_trading_dates", window)
    monkeypatch.setattr(provider, "_fetch_pykrx_day", lambda day: {"005930": {"foreign": 1, "inst": 1}})
    broker = SimpleNamespace(fetch_batch_investor_daily=AsyncMock(return_value={}))
    assert await provider.ensure_loaded_from_kis(broker, ["005930"])
    assert provider._expected_dates == dates
    assert len(calls) == 1
