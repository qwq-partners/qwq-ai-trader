"""Paging contract for Toss daily candles (offline-only)."""

import asyncio
from datetime import datetime, timedelta, timezone

from src.data.providers.toss.market_data import fetch_daily_candles


KST = timezone(timedelta(hours=9))
FETCHED = datetime(2026, 9, 16, 10, 0, tzinfo=KST)


def _candle(day, close="100"):
    return {
        "timestamp": f"{day[:4]}-{day[4:6]}-{day[6:]}T00:00:00+09:00",
        "openPrice": "95", "highPrice": "250", "lowPrice": "1",
        "closePrice": close, "volume": 100, "currency": "KRW",
    }


class _Budget:
    def __init__(self, *, pages=4, remaining=60.0):
        self.max_pages = pages
        self._remaining = remaining
        self.pages_used = 0

    def remaining(self):
        return self._remaining

    def consume_page(self):
        if self.pages_used >= self.max_pages:
            return False
        self.pages_used += 1
        return True


class _Client:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    async def get(self, path, *, params, budget):
        # The real Task2 client owns one page-budget consumption per logical get.
        # This fake preserves that public boundary; the collector must not do it.
        if budget.remaining() <= 0 or budget.consume_page() is False:
            raise RuntimeError("budget exhausted")
        self.calls.append((path, params, budget))
        outcome = next(self.responses)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def test_fetch_daily_candles_passes_next_before_verbatim_and_collects_complete_window():
    client = _Client([
        {"candles": [_candle("20260915"), _candle("20260912")], "nextBefore": "2026-09-11T00:00:00+09:00"},
        {"candles": [_candle("20260911")], "nextBefore": None},
    ])
    budget = _Budget()

    series = asyncio.run(fetch_daily_candles(
        client, symbol="005930", expected_dates=["20260911", "20260912", "20260915"],
        fetched_at=FETCHED, budget=budget, market_basis="krx",
    ))

    assert series.complete is True
    assert [bar.bar_date for bar in series.bars] == ["20260911", "20260912", "20260915"]
    assert client.calls[0][0] == "/api/v1/candles"
    assert client.calls[0][1] == {"symbol": "005930", "interval": "1d", "count": 200, "adjusted": True}
    assert client.calls[1][1]["before"] == "2026-09-11T00:00:00+09:00"
    assert budget.pages_used == 2


def test_fetch_daily_candles_deduplicates_inclusive_page_boundary_without_losing_201_to_250_high():
    expected = [f"2025{month:02d}{day:02d}" for month, day in [(1, 2), (1, 3), (1, 6)]]
    # A10의 핵심은 200개 뒤쪽(두 번째 페이지)에만 있는 고점도 요구 창에 포함되면 보존하는 것이다.
    client = _Client([
        {"candles": [_candle("20250106", "110"), _candle("20250103", "120")], "nextBefore": "2025-01-03T00:00:00+09:00"},
        {"candles": [_candle("20250103", "120"), _candle("20250102", "200")], "nextBefore": None},
    ])

    series = asyncio.run(fetch_daily_candles(
        client, symbol="005930", expected_dates=expected, fetched_at=FETCHED,
        budget=_Budget(), market_basis="krx",
    ))

    assert series.complete is True
    assert [str(bar.close) for bar in series.bars] == ["200", "120", "110"]


def test_fetch_daily_candles_stops_repeated_cursor_and_returns_partial_not_false_success():
    client = _Client([
        {"candles": [_candle("20260915")], "nextBefore": "cursor-a"},
        {"candles": [_candle("20260912")], "nextBefore": "cursor-a"},
    ])

    series = asyncio.run(fetch_daily_candles(
        client, symbol="005930", expected_dates=["20260911", "20260912", "20260915"],
        fetched_at=FETCHED, budget=_Budget(), market_basis="krx",
    ))

    assert series.complete is False
    assert series.missing_dates == ("20260911",)
    assert series.status == "partial"
    assert len(client.calls) == 2


def test_fetch_daily_candles_second_page_error_returns_partial_without_collector_retry():
    client = _Client([
        {"candles": [_candle("20260915")], "nextBefore": "cursor-a"},
        RuntimeError("offline fake failure"),
    ])
    budget = _Budget()

    series = asyncio.run(fetch_daily_candles(
        client, symbol="005930", expected_dates=["20260912", "20260915"],
        fetched_at=FETCHED, budget=budget, market_basis="krx",
    ))

    assert series.complete is False
    assert series.missing_dates == ("20260912",)
    assert series.status == "partial"
    assert len(client.calls) == 2
    assert budget.pages_used == 2


def test_fetch_daily_candles_honors_page_cap_and_deadline_before_a_new_request():
    capped_client = _Client([{"candles": [_candle("20260915")], "nextBefore": "cursor-a"}])
    capped = asyncio.run(fetch_daily_candles(
        capped_client, symbol="005930", expected_dates=["20260912", "20260915"],
        fetched_at=FETCHED, budget=_Budget(pages=1), market_basis="krx",
    ))
    expired_client = _Client([])
    expired = asyncio.run(fetch_daily_candles(
        expired_client, symbol="005930", expected_dates=["20260915"],
        fetched_at=FETCHED, budget=_Budget(remaining=0), market_basis="krx",
    ))

    assert capped.complete is False
    assert capped.missing_dates == ("20260912",)
    assert len(capped_client.calls) == 1
    assert expired.complete is False
    assert expired.missing_dates == ("20260915",)
    assert expired.status == "partial"
    assert expired_client.calls == []
