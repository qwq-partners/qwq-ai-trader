"""Paging contract for Toss daily candles (offline-only)."""

import asyncio
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from src.data.providers.toss.market_data import fetch_daily_candles
from src.data.providers.toss.rate_limit import RequestBudget


KST = timezone(timedelta(hours=9))
FETCHED = datetime(2026, 9, 16, 10, 0, tzinfo=KST)


def _candle(day, close="100", high=None):
    high = high or close
    return {
        "timestamp": f"{day[:4]}-{day[4:6]}-{day[6:]}T00:00:00+09:00",
        "openPrice": "95", "highPrice": high, "lowPrice": "1",
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
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class _Clock:
    def __init__(self, now=0.0):
        self.now = now

    def __call__(self):
        return self.now


class _ActualBudgetClient:
    """Offline client boundary that mirrors TossClient's page consumption order."""

    def __init__(self, responses, *, clock=None, expire_after_first=False):
        self.responses = iter(responses)
        self.calls = []
        self._clock = clock
        self._expire_after_first = expire_after_first

    async def get(self, path, *, params, budget):
        budget.consume_page()
        self.calls.append((path, params, budget))
        outcome = next(self.responses)
        if self._expire_after_first and len(self.calls) == 1:
            self._clock.now = budget.deadline
        if isinstance(outcome, BaseException):
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


def _weekday_dates(count):
    result = []
    cursor = date(2025, 1, 2)
    while len(result) < count:
        if cursor.weekday() < 5:
            result.append(cursor.strftime("%Y%m%d"))
        cursor += timedelta(days=1)
    return result


def test_fetch_daily_candles_preserves_actual_250_day_window_and_older_page_only_high():
    expected = _weekday_dates(250)
    newer_page = list(reversed(expected[50:]))
    older_page = [expected[50], *reversed(expected[:50])]
    # A10: first page has 200 distinct dates, the older page contributes the
    # remaining 50, and only that older segment contains the 200 high.
    client = _Client([
        {"candles": [_candle(day, "110", high="120") for day in newer_page], "nextBefore": f"{expected[50][:4]}-{expected[50][4:6]}-{expected[50][6:]}T00:00:00+09:00"},
        {"candles": [_candle(day, "110", high=("200" if day == expected[0] else "120")) for day in older_page], "nextBefore": None},
    ])

    series = asyncio.run(fetch_daily_candles(
        client, symbol="005930", expected_dates=expected, fetched_at=FETCHED,
        budget=_Budget(), market_basis="krx",
    ))

    assert series.complete is True
    assert len(series.bars) == 250
    assert len({bar.bar_date for bar in series.bars}) == 250
    assert max(bar.high for bar in series.bars) == Decimal("200")
    assert float((Decimal("110") / max(bar.high for bar in series.bars) - Decimal("1")) * Decimal("100")) == -45.0


def test_fetch_daily_candles_stops_repeated_cursor_and_returns_partial_not_false_success():
    client = _Client([
        {"candles": [_candle("20260915")], "nextBefore": "2026-09-12T00:00:00+09:00"},
        {"candles": [_candle("20260912")], "nextBefore": "2026-09-12T00:00:00+09:00"},
    ])

    series = asyncio.run(fetch_daily_candles(
        client, symbol="005930", expected_dates=["20260911", "20260912", "20260915"],
        fetched_at=FETCHED, budget=_Budget(), market_basis="krx",
    ))

    assert series.complete is False
    assert series.missing_dates == ("20260911",)
    assert series.status == "partial"
    assert len(client.calls) == 2


@pytest.mark.parametrize("next_before", [[], "not-an-iso-cursor"])
def test_fetch_daily_candles_stops_malformed_cursor_safely_with_partial_result(next_before):
    client = _Client([{"candles": [_candle("20260915")], "nextBefore": next_before}])

    series = asyncio.run(fetch_daily_candles(
        client, symbol="005930", expected_dates=["20260912", "20260915"],
        fetched_at=FETCHED, budget=_Budget(), market_basis="krx",
    ))

    assert series.complete is False
    assert series.missing_dates == ("20260912",)
    assert len(client.calls) == 1


def test_fetch_daily_candles_stops_forward_cursor_without_a_third_request():
    client = _Client([
        {"candles": [_candle("20260915")], "nextBefore": "2026-09-15T00:00:00+09:00"},
        {"candles": [_candle("20260912")], "nextBefore": "2026-09-16T00:00:00+09:00"},
    ])

    series = asyncio.run(fetch_daily_candles(
        client, symbol="005930", expected_dates=["20260911", "20260912", "20260915"],
        fetched_at=FETCHED, budget=_Budget(), market_basis="krx",
    ))

    assert series.complete is False
    assert series.missing_dates == ("20260911",)
    assert len(client.calls) == 2


def test_fetch_daily_candles_stops_initial_cursor_later_than_received_time():
    client = _Client([{"candles": [_candle("20260915")], "nextBefore": "2026-09-16T10:01:00+09:00"}])

    series = asyncio.run(fetch_daily_candles(
        client, symbol="005930", expected_dates=["20260912", "20260915"],
        fetched_at=FETCHED, budget=_Budget(), market_basis="krx",
    ))

    assert series.complete is False
    assert series.missing_dates == ("20260912",)
    assert len(client.calls) == 1


def test_fetch_daily_candles_second_page_error_returns_partial_without_collector_retry():
    client = _Client([
        {"candles": [_candle("20260915")], "nextBefore": "2026-09-12T00:00:00+09:00"},
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


def test_fetch_daily_candles_actual_budget_timeout_before_first_send_returns_partial():
    clock = _Clock()
    budget = RequestBudget(1, clock=clock)
    clock.now = budget.deadline
    client = _ActualBudgetClient([])

    series = asyncio.run(fetch_daily_candles(
        client, symbol="005930", expected_dates=["20260915"],
        fetched_at=FETCHED, budget=budget, market_basis="krx",
    ))

    assert client.calls == []
    assert series.complete is False
    assert series.missing_dates == ("20260915",)
    assert series.status == "partial"


def test_fetch_daily_candles_actual_budget_expiry_between_pages_keeps_first_bar_partial():
    clock = _Clock()
    budget = RequestBudget(1, clock=clock)
    client = _ActualBudgetClient(
        [{"candles": [_candle("20260915")], "nextBefore": "2026-09-12T00:00:00+09:00"}],
        clock=clock, expire_after_first=True,
    )

    series = asyncio.run(fetch_daily_candles(
        client, symbol="005930", expected_dates=["20260912", "20260915"],
        fetched_at=FETCHED, budget=budget, market_basis="krx",
    ))

    assert len(client.calls) == 1
    assert [bar.bar_date for bar in series.bars] == ["20260915"]
    assert series.complete is False
    assert series.missing_dates == ("20260912",)
    assert series.status == "partial"


def test_fetch_daily_candles_actual_page_cap_prevents_second_send_and_higher_cap_completes():
    responses = [
        {"candles": [_candle("20260915")], "nextBefore": "2026-09-12T00:00:00+09:00"},
        {"candles": [_candle("20260912")], "nextBefore": None},
    ]
    cap_one_client = _ActualBudgetClient(responses)
    cap_one = asyncio.run(fetch_daily_candles(
        cap_one_client, symbol="005930", expected_dates=["20260912", "20260915"],
        fetched_at=FETCHED, budget=RequestBudget(5, clock=_Clock(), max_pages=1), market_basis="krx",
    ))
    cap_two_client = _ActualBudgetClient(responses)
    cap_two = asyncio.run(fetch_daily_candles(
        cap_two_client, symbol="005930", expected_dates=["20260912", "20260915"],
        fetched_at=FETCHED, budget=RequestBudget(5, clock=_Clock(), max_pages=2), market_basis="krx",
    ))

    assert len(cap_one_client.calls) == 1
    assert [bar.bar_date for bar in cap_one.bars] == ["20260915"]
    assert cap_one.complete is False
    assert cap_one.missing_dates == ("20260912",)
    assert len(cap_two_client.calls) == 2
    assert cap_two.complete is True
    assert [bar.bar_date for bar in cap_two.bars] == ["20260912", "20260915"]


def test_fetch_daily_candles_propagates_cancellation_instead_of_converting_it_to_partial():
    client = _ActualBudgetClient([asyncio.CancelledError()])

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(fetch_daily_candles(
            client, symbol="005930", expected_dates=["20260915"],
            fetched_at=FETCHED, budget=RequestBudget(5, clock=_Clock()), market_basis="krx",
        ))
