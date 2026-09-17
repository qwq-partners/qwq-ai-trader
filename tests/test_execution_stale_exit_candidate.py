"""Parity tests for pure preemptive-stale exit candidate selection."""

import asyncio
from copy import deepcopy
from dataclasses import FrozenInstanceError
from datetime import date, datetime, timedelta, timezone
import datetime as datetime_module
from decimal import Decimal
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from src.core.batch_analyzer import BatchAnalyzer
from src.core.types import Portfolio, Position
from src.execution.safety.economics import encode_portfolio
from src.execution.safety.protection import encode_protection
from src.execution.safety.stale_exit_candidate import select_preemptive_stale
from src.strategies.exit_manager import ExitManager


KST = ZoneInfo("Asia/Seoul")
TODAY = date(2026, 9, 18)
NOW = datetime(2026, 9, 18, 10, tzinfo=KST)


def _business_days(today, entry, holidays=frozenset()):
    return sum(
        1 for offset in range((today - entry).days)
        if (entry + timedelta(days=offset + 1)).weekday() < 5
        and (entry + timedelta(days=offset + 1)) not in holidays
    )


def _entry_for(today, business_days, holidays=frozenset()):
    for offset in range(0, 32):
        candidate = today - timedelta(days=offset)
        if _business_days(today, candidate, holidays) == business_days:
            return candidate
    raise AssertionError("entry date fixture could not be built")


def _position(symbol, *, entry_time, current="10000", avg="10000", strategy="sepa_trend", quantity=10):
    return Position(
        symbol=symbol,
        quantity=quantity,
        avg_price=Decimal(avg),
        current_price=Decimal(current),
        strategy=strategy,
        entry_time=entry_time,
    )


def _protection(tmp_path, *, exempt=()):
    manager = ExitManager(persist=False, state_dir=tmp_path, clock=lambda: NOW)
    for symbol in exempt:
        manager.add_exit_exempt(symbol)
    return encode_protection(manager), manager


def _actual_candidates(monkeypatch, portfolio, manager, holidays, *, today=TODAY):
    """Run the unchanged BatchAnalyzer method; only emit/calendar boundaries are fake."""
    import src.utils.session as session

    class FrozenDate(date):
        @classmethod
        def today(cls):
            return today

    emitted = []

    async def emit(event):
        emitted.append(event)

    analyzer = object.__new__(BatchAnalyzer)
    analyzer._engine = SimpleNamespace(portfolio=portfolio, emit=emit)
    analyzer._exit_manager = manager
    with monkeypatch.context() as patch:
        patch.setattr(datetime_module, "date", FrozenDate)
        patch.setattr(session, "is_kr_market_holiday", lambda day: day in holidays)
        asyncio.run(analyzer._preemptive_stale_exit_on_bear())
    return emitted


def test_pure_candidates_match_actual_batch_method_with_real_portfolio_and_exit_manager(monkeypatch, tmp_path):
    today = TODAY
    holidays = frozenset({today - timedelta(days=2)})
    eligible_entry = _entry_for(today, 5, holidays)
    four_day_entry = _entry_for(today, 4, holidays)
    positions = {
        "005930": _position("005930", entry_time=datetime.combine(eligible_entry, datetime.min.time(), KST)),
        "000660": _position("000660", entry_time=datetime.combine(eligible_entry, datetime.min.time(), KST), strategy="core_holding"),
        "035420": _position("035420", entry_time=datetime.combine(eligible_entry, datetime.min.time(), KST)),
        "051910": _position("051910", entry_time=datetime.combine(four_day_entry, datetime.min.time(), KST)),
        "068270": _position("068270", entry_time=datetime.combine(eligible_entry, datetime.min.time(), KST), strategy="unknown_strategy"),
        "207940": _position("207940", entry_time=datetime.combine(eligible_entry, datetime.min.time(), KST), current="10100"),
        "373220": _position("373220", entry_time=None),
    }
    portfolio = Portfolio(cash=Decimal("0"), positions=positions, initial_capital=Decimal("1000000"))
    protection_dto, manager = _protection(tmp_path, exempt=("035420",))
    prices = {symbol: position.current_price for symbol, position in positions.items()}

    emitted = _actual_candidates(monkeypatch, portfolio, manager, holidays)
    candidates = select_preemptive_stale(
        encode_portfolio(portfolio), protection_dto, today=today, prices=prices, holidays=holidays
    )

    assert [candidate.symbol for candidate in candidates] == [event.symbol for event in emitted]
    assert [candidate.quantity for candidate in candidates] == [event.metadata["quantity"] for event in emitted]
    assert [candidate.price for candidate in candidates] == [event.price for event in emitted]
    assert [candidate.strategy for candidate in candidates] == [event.strategy.value for event in emitted]
    assert [candidate.reason for candidate in candidates] == [event.reason for event in emitted]
    assert all(event.strength.value == "strong" and event.score == 100.0 and event.confidence == 1.0
               and event.source == "preemptive_stale_bear" for event in emitted)
    assert {candidate.symbol for candidate in candidates} == {"005930", "068270"}
    assert candidates[1].strategy == "sepa_trend"


def test_exact_five_business_days_qualifies_but_four_days_and_one_percent_profit_do_not(tmp_path):
    today = date(2026, 9, 18)
    five = _entry_for(today, 5)
    four = _entry_for(today, 4)
    positions = {
        "005930": _position("005930", entry_time=datetime.combine(five, datetime.min.time(), KST)),
        "000660": _position("000660", entry_time=datetime.combine(four, datetime.min.time(), KST)),
        "035420": _position("035420", entry_time=datetime.combine(five, datetime.min.time(), KST), current="10100"),
    }
    dto, _ = _protection(tmp_path)

    candidates = select_preemptive_stale(
        encode_portfolio(Portfolio(positions=positions)), dto, today=today,
        prices={symbol: position.current_price for symbol, position in positions.items()}, holidays=frozenset(),
    )

    assert [candidate.symbol for candidate in candidates] == ["005930"]
    assert candidates[0].business_days == 5
    assert candidates[0].pnl_pct == 0.0


def test_weekends_and_explicit_holidays_reduce_business_days(tmp_path):
    today = date(2026, 9, 18)
    entry = today - timedelta(days=7)
    holidays = frozenset({date(2026, 9, 16)})
    assert _business_days(today, entry, holidays) == 4
    position = _position("005930", entry_time=datetime.combine(entry, datetime.min.time(), KST))
    dto, _ = _protection(tmp_path)

    candidates = select_preemptive_stale(
        encode_portfolio(Portfolio(positions={"005930": position})), dto, today=today,
        prices={"005930": Decimal("10000")}, holidays=holidays,
    )

    assert candidates == ()


def test_current_zero_falls_back_to_average_price_and_inputs_remain_unchanged(tmp_path):
    today = date(2026, 9, 18)
    entry = _entry_for(today, 5)
    position = _position("005930", entry_time=datetime.combine(entry, datetime.min.time(), timezone.utc), current="0", avg="10000")
    portfolio_dto = encode_portfolio(Portfolio(positions={"005930": position}))
    protection_dto, _ = _protection(tmp_path)
    before_portfolio, before_protection = deepcopy(portfolio_dto), deepcopy(protection_dto)

    candidates = select_preemptive_stale(
        portfolio_dto, protection_dto, today=today, prices={}, holidays=frozenset(),
    )

    assert candidates[0].price == Decimal("10000")
    assert portfolio_dto == before_portfolio and protection_dto == before_protection
    with pytest.raises(FrozenInstanceError):
        candidates[0].quantity = 1


def test_supplied_current_price_is_used_for_the_candidate(tmp_path):
    today = date(2026, 9, 18)
    entry = _entry_for(today, 5)
    position = _position("005930", entry_time=datetime.combine(entry, datetime.min.time(), KST))
    dto, _ = _protection(tmp_path)

    candidates = select_preemptive_stale(
        encode_portfolio(Portfolio(positions={"005930": position})), dto, today=today,
        prices={"005930": Decimal("9999")}, holidays=frozenset(),
    )

    assert candidates[0].price == Decimal("9999")


@pytest.mark.parametrize("retained,view_price", [
    ("10000", "10100"),
    ("10200", "10000"),
    ("10000", "10099"),
    ("10000", "10000"),
    ("0", "0"),
    ("10200", "0"),
])
def test_supplied_price_drives_actual_pnl_threshold_reason_and_price(
    monkeypatch, tmp_path, retained, view_price,
):
    """The copied position must use the owner's current view before legacy selection."""
    entry = _entry_for(TODAY, 5)
    retained_position = _position(
        "005930", entry_time=datetime.combine(entry, datetime.min.time(), KST), current=retained,
    )
    retained_portfolio = Portfolio(positions={"005930": retained_position})
    protection_dto, manager = _protection(tmp_path)
    live_portfolio = deepcopy(retained_portfolio)
    live_portfolio.positions["005930"].current_price = Decimal(view_price)

    emitted = _actual_candidates(monkeypatch, live_portfolio, manager, frozenset())
    candidates = select_preemptive_stale(
        encode_portfolio(retained_portfolio), protection_dto, today=TODAY,
        prices={"005930": Decimal(view_price)}, holidays=frozenset(),
    )

    assert [(candidate.symbol, candidate.price, candidate.pnl_pct, candidate.reason) for candidate in candidates] == [
        (event.symbol, event.price,
         float(live_portfolio.positions[event.symbol].unrealized_pnl_pct or 0), event.reason)
        for event in emitted
    ]


@pytest.mark.parametrize("price", ["NaN", "sNaN", "Infinity", "-Infinity", "-1"])
def test_invalid_supplied_price_is_rejected_before_candidate_construction(tmp_path, price):
    entry = _entry_for(TODAY, 5)
    position = _position("005930", entry_time=datetime.combine(entry, datetime.min.time(), KST))
    protection_dto, _ = _protection(tmp_path)

    with pytest.raises(ValueError):
        select_preemptive_stale(
            encode_portfolio(Portfolio(positions={"005930": position})), protection_dto,
            today=TODAY, prices={"005930": Decimal(price)}, holidays=frozenset(),
        )


def test_utc_entry_time_uses_existing_entry_date_semantics(tmp_path):
    today = date(2026, 9, 18)
    entry = _entry_for(today, 5)
    position = _position("005930", entry_time=datetime.combine(entry, datetime.min.time(), timezone.utc))
    dto, _ = _protection(tmp_path)

    candidates = select_preemptive_stale(
        encode_portfolio(Portfolio(positions={"005930": position})), dto, today=today,
        prices={"005930": Decimal("10000")}, holidays=frozenset(),
    )

    assert candidates[0].business_days == 5
