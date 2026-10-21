"""A short volume lookup must not suppress a later candidate's ATR check."""
from datetime import date, timedelta
from types import SimpleNamespace

import pytest

from src.execution.broker.kis_kr import KISBroker
from src.signals.screener.kr_screener import ScreenedStock, StockScreener
from src.utils import kis_request_metrics


def history(count=30, *, high=11200, low=10000):
    rows = []
    day = date.today() - timedelta(days=1)
    while len(rows) < count:
        if day.weekday() < 5:
            rows.append({"date": day.strftime("%Y%m%d"), "open": 10000,
                         "high": high, "low": low, "close": 10000,
                         "volume": 1000, "value": 10000000})
        day -= timedelta(days=1)
    return list(reversed(rows))


def screener_with_http(monkeypatch, available):
    # Keep the real broker's request pagination and days slicing. Only HTTP is fake.
    broker = object.__new__(KISBroker)
    broker.config = SimpleNamespace(base_url="https://fixture.invalid")
    monkeypatch.setattr(KISBroker, "is_connected", property(lambda self: True))
    monkeypatch.setattr(kis_request_metrics, "_recorder", kis_request_metrics.RequestMetrics())
    sent = []

    async def api_get(url, tr_id, params):
        sent.append(dict(params))
        rows = [row for row in reversed(available)
                if params["fid_input_date_1"] <= row["date"] <= params["fid_input_date_2"]]
        return {"rt_cd": "0", "output2": [
            {"stck_bsop_date": row["date"], "stck_oprc": str(row["open"]),
             "stck_hgpr": str(row["high"]), "stck_lwpr": str(row["low"]),
             "stck_clpr": str(row["close"]), "acml_vol": str(row["volume"]),
             "acml_tr_pbmn": str(row["value"])} for row in rows[:100]
        ]}

    broker._api_get = api_get
    screener = object.__new__(StockScreener)
    screener._broker = broker
    return screener, sent


@pytest.mark.asyncio
async def test_volume_cache_keeps_atr_for_candidate_promoted_into_top_twenty(monkeypatch):
    screener, sent = screener_with_http(monkeypatch, history())
    stocks = {f"{n:06d}": ScreenedStock(f"{n:06d}", score=80, volume=2000)
              for n in range(1, 21)}
    candidate = ScreenedStock("000021", score=79, volume=2000)
    stocks[candidate.symbol] = candidate
    # Momentum already fetched only its original top 20 candidates.
    cache = {symbol: history(high=10100, low=9900) for symbol in stocks if symbol != candidate.symbol}

    await screener._apply_volume_ratio(stocks, daily_cache=cache)
    # A later sector adjustment moves the previously 21st candidate into the ATR set.
    candidate.score += 10
    await screener._apply_volatility_filter(stocks, daily_cache=cache)

    assert candidate.volume_ratio == 2.0
    assert candidate.atr_pct == pytest.approx(12.0)
    assert candidate.score == 74  # Existing >8% ATR rule deducts 15 points.
    assert any("초고변동" in reason for reason in candidate.reasons)
    assert len(sent) == 1  # 30 available rows still fit in the same single HTTP page.


@pytest.mark.asyncio
async def test_existing_full_daily_cache_serves_both_checks_without_http(monkeypatch):
    screener, sent = screener_with_http(monkeypatch, history())
    candidate = ScreenedStock("000021", score=80, volume=2000)
    stocks = {candidate.symbol: candidate}
    cache = {candidate.symbol: history()}

    await screener._apply_volume_ratio(stocks, daily_cache=cache)
    await screener._apply_volatility_filter(stocks, daily_cache=cache)

    assert candidate.volume_ratio == 2.0
    assert candidate.atr_pct == pytest.approx(12.0)
    assert candidate.score == 65
    assert sent == []


@pytest.mark.asyncio
async def test_insufficient_actual_history_remains_unknown(monkeypatch):
    screener, _ = screener_with_http(monkeypatch, history(count=5))
    candidate = ScreenedStock("000021", score=80, volume=2000)
    stocks = {candidate.symbol: candidate}
    cache = {}

    await screener._apply_volume_ratio(stocks, daily_cache=cache)
    await screener._apply_volatility_filter(stocks, daily_cache=cache)

    assert candidate.volume_ratio == 2.0
    assert candidate.atr_pct is None
    assert candidate.score == 80
    assert len(cache[candidate.symbol]) == 5
