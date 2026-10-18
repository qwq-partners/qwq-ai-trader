"""MRS must compare the same completed sessions, never list positions."""
from datetime import date, timedelta

import pytest

from src.indicators.technical import TechnicalIndicators


def bars(n=35):
    start = date(2026, 8, 1)
    return [(start + timedelta(days=i), float(100 + i)) for i in range(n)]


def calc(stock, index, **kwargs):
    return TechnicalIndicators.calculate_mrs_dated(stock, index, **kwargs)


def test_equal_length_different_dates_join_before_calculation():
    index = bars()
    # Different missing sessions; common-session ratios are exactly two.
    stock = [(d, p * 2) for d, p in index if d != index[10][0]]
    index = [row for i, row in enumerate(index) if i != 15]
    assert calc(stock, index, before=date(2026, 10, 1)) == {"mrs": 0.0, "mrs_slope": 0.0}
    positional = TechnicalIndicators.calculate_mrs([p for _, p in stock], [p for _, p in index])
    assert positional != {"mrs": 0.0, "mrs_slope": 0.0}


def test_partial_and_future_sessions_are_excluded():
    index = bars()
    stock = [(d, p * 2) for d, p in index]
    cutoff = index[-2][0]
    stock[-2:] = [(cutoff, 999999.0), (index[-1][0], 0.01)]
    assert calc(stock, index, before=cutoff) == {"mrs": 0.0, "mrs_slope": 0.0}


def test_missing_latest_completed_stock_session_is_not_current_mrs():
    assert calc(bars()[:-1], bars(), before=date(2026, 10, 1)) is None


@pytest.mark.parametrize("bad", [0, -1, float("nan"), float("inf"), True, "bad"])
def test_invalid_price_is_not_imputed(bad):
    stock = bars()
    stock[4] = (stock[4][0], bad)
    assert calc(stock, bars(), before=date(2026, 10, 1)) is None


@pytest.mark.parametrize("kind", ["duplicate", "reverse", "undated", "short"])
def test_unusable_dates_or_insufficient_common_history(kind):
    stock = bars()
    if kind == "duplicate":
        stock[4] = stock[3]
    elif kind == "reverse":
        stock.reverse()
    elif kind == "undated":
        stock[4] = (None, 104)
    else:
        stock = stock[-24:]
    assert calc(stock, bars(), before=date(2026, 10, 1)) is None


@pytest.mark.parametrize("scan_day,hour", [("2026-09-23", 12), ("2026-09-23", 17),
                                         ("2026-09-21", 9), ("2026-09-28", 9)])
def test_live_screener_uses_dates_and_excludes_partial_today(monkeypatch, scan_day, hour):
    import asyncio
    from datetime import datetime
    import pandas as pd
    from src.signals.screener import swing_screener as mod
    from tests.test_benchmark_freshness import Clock
    from src.utils.session import is_kr_market_holiday

    monkeypatch.setattr(mod, "datetime", Clock)
    Clock.current = datetime.fromisoformat(scan_day).replace(hour=hour)
    dates = pd.bdate_range(end=scan_day, periods=70)
    dates = dates[[not is_kr_market_holiday(day.date()) for day in dates]]
    index = pd.DataFrame({"Close": [100.0 + i for i in range(len(dates))]}, index=dates)
    stock = index.copy() * 2
    index = index.drop(dates[40])
    stock = stock.drop(dates[45])
    stock.loc[dates[-1], "Close"] = 100000  # unfinished current bar
    stock["Open"] = stock["Close"]
    stock["High"] = stock["Close"] + 1
    stock["Low"] = stock["Close"] - 1
    stock["Volume"] = 100_000_000
    screener = mod.SwingScreener.__new__(mod.SwingScreener)
    screener._indicators = TechnicalIndicators()
    monkeypatch.setattr(screener, "_fetch_fdr_data", lambda symbol, start: index if symbol == "YAHOO:^KS11" else stock)
    asyncio.run(screener._load_benchmark_index())
    results = asyncio.run(screener._calculate_all_indicators([{"symbol": "TEST", "name": "Synthetic"}]))
    assert results[0]["indicators"]["mrs"] == 0.0
    assert results[0]["indicators"]["mrs_slope"] == 0.0
    screener._kospi_dates = []  # legacy undated cache cannot fall back to positions
    results = asyncio.run(screener._calculate_all_indicators([{"symbol": "TEST", "name": "Synthetic"}]))
    assert "mrs" not in results[0]["indicators"]
    # A partial bar today must not hide a missing previous completed session.
    screener._kospi_dates = [day.date() for day in index.index if day != dates[-2]]
    screener._kospi_closes = [float(row.Close) for day, row in index.iterrows() if day != dates[-2]]
    results = asyncio.run(screener._calculate_all_indicators([{"symbol": "TEST", "name": "Synthetic"}]))
    assert "mrs" not in results[0]["indicators"]
