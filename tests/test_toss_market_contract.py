"""Toss market-data normalization contract (offline-only)."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from src.data.providers.toss.market_data import (
    MarketDataError,
    compose_quote,
    normalize_candle_pages,
    parse_prices,
)
from src.data.providers.toss.market_types import Candle, CandleSeries, Quote
from src.utils.data_freshness import is_fresh


KST = timezone(timedelta(hours=9))
NOW = datetime(2026, 9, 16, 10, 0, tzinfo=KST)
FETCHED = datetime(2026, 9, 16, 10, 0, tzinfo=KST)


def _price_row(symbol="005930", *, price="105", timestamp="2026-09-16T09:59:00+09:00", currency="KRW"):
    return {"symbol": symbol, "lastPrice": price, "timestamp": timestamp, "currency": currency}


def _candle(day, *, close="105", volume=100, timestamp=None):
    return {
        "timestamp": timestamp or f"{day[:4]}-{day[4:6]}-{day[6:]}T00:00:00+09:00",
        "openPrice": "100",
        "highPrice": "250",
        "lowPrice": "1",
        "closePrice": close,
        "volume": volume,
        "currency": "KRW",
    }


def test_parse_prices_preserves_leading_zero_and_alphanumeric_symbols():
    quotes = parse_prices(
        {"result": [_price_row("005930"), _price_row("A1B2", price="7.25")]},
        symbols=["005930", "A1B2"], fetched_at=FETCHED, now=NOW, max_age_seconds=120,
    )

    assert quotes["005930"].symbol == "005930"
    assert quotes["005930"].price == Decimal("105")
    assert quotes["A1B2"].price == Decimal("7.25")
    assert quotes["005930"].status == "ok"
    assert is_fresh(quotes["005930"].to_data_point(120), NOW) is True


def test_parse_prices_marks_null_timestamp_partial_and_never_fresh():
    quotes = parse_prices(
        {"result": [_price_row(timestamp=None)]}, symbols=["005930"],
        fetched_at=FETCHED, now=NOW, max_age_seconds=120,
    )

    quote = quotes["005930"]
    assert quote.price == Decimal("105")
    assert quote.observed_at is None
    assert quote.status == "partial"
    assert "observed_at" in quote.missing_fields
    assert is_fresh(quote.to_data_point(120), NOW) is False


@pytest.mark.parametrize("bad", [True, "NaN", "Infinity", "1e10000", "0", "-1"])
def test_parse_prices_rejects_nonfinite_boolean_and_nonpositive_price(bad):
    quotes = parse_prices(
        {"result": [_price_row(price=bad)]}, symbols=["005930"],
        fetched_at=FETCHED, now=NOW, max_age_seconds=120,
    )

    assert quotes["005930"].price is None
    assert quotes["005930"].status == "invalid"


@pytest.mark.parametrize(
    "rows",
    [
        [_price_row(price="105"), _price_row(price="NaN")],
        [_price_row(price="NaN"), _price_row(price="105")],
        [_price_row(price="105"), _price_row(price="205")],
        [_price_row(price="205"), _price_row(price="105")],
    ],
)
def test_parse_prices_marks_any_duplicate_symbol_invalid_regardless_of_row_order(rows):
    quote = parse_prices(
        {"result": rows}, symbols=["005930"], fetched_at=FETCHED, now=NOW, max_age_seconds=120,
    )["005930"]

    assert quote.price is None
    assert quote.status == "invalid"
    assert is_fresh(quote.to_data_point(120), NOW + timedelta(hours=1)) is False


def test_parse_prices_rejects_wrong_currency_and_keeps_unrequested_symbol_missing():
    quotes = parse_prices(
        {"result": [_price_row(symbol="005930", currency="USD"), _price_row(symbol="OTHER")]},
        symbols=["005930", "A1B2"], fetched_at=FETCHED, now=NOW, max_age_seconds=120,
    )

    assert quotes["005930"].status == "invalid"
    assert quotes["A1B2"].status == "missing"
    assert quotes["A1B2"].price is None


@pytest.mark.parametrize(
    ("timestamp", "status"),
    [
        ("2026-09-16T10:01:00+09:00", "invalid"),
        ("2026-09-16T09:50:00+09:00", "stale"),
        ("2026-09-16T09:59:00", "invalid"),
    ],
)
def test_parse_prices_never_marks_future_stale_or_naive_observation_fresh(timestamp, status):
    quote = parse_prices(
        {"result": [_price_row(timestamp=timestamp)]}, symbols=["005930"],
        fetched_at=FETCHED, now=NOW, max_age_seconds=120,
    )["005930"]

    assert quote.status == status
    assert is_fresh(quote.to_data_point(120), NOW) is False


def test_parse_prices_rejects_naive_or_future_received_time():
    with pytest.raises(MarketDataError, match="invalid_fetched_at"):
        parse_prices({"result": []}, symbols=["005930"], fetched_at=datetime(2026, 9, 16, 10, 0), now=NOW, max_age_seconds=120)
    with pytest.raises(MarketDataError, match="invalid_fetched_at"):
        parse_prices({"result": []}, symbols=["005930"], fetched_at=NOW + timedelta(seconds=1), now=NOW, max_age_seconds=120)


def test_parse_prices_rejects_observation_after_receipt_and_preserves_rejection_in_data_point():
    fetched_at = datetime(2026, 9, 16, 9, 58, tzinfo=KST)
    quote = parse_prices(
        {"result": [_price_row(timestamp="2026-09-16T09:59:00+09:00")]},
        symbols=["005930"], fetched_at=fetched_at, now=NOW, max_age_seconds=120,
    )["005930"]

    assert quote.status == "invalid"
    assert quote.price is None
    assert quote.to_data_point(120).is_missing is True
    assert is_fresh(quote.to_data_point(120), NOW + timedelta(days=1)) is False


def test_parse_prices_ignores_nonstring_row_symbol_without_crashing_batch():
    quotes = parse_prices(
        {"result": [{"symbol": [], "lastPrice": "105", "timestamp": "2026-09-16T09:59:00+09:00", "currency": "KRW"}]},
        symbols=["005930"], fetched_at=FETCHED, now=NOW, max_age_seconds=120,
    )

    assert quotes["005930"].status == "missing"


def test_normalize_candles_sorts_kst_dates_deduplicates_identical_and_keeps_zero_volume():
    series = normalize_candle_pages(
        [{"candles": [_candle("20260915", close="100", volume=0), _candle("20260912", close="90"), _candle("20260915", close="100", volume=0)]}],
        symbol="005930", expected_dates=["20260912", "20260915"],
        fetched_at=FETCHED,
    )

    assert [bar.bar_date for bar in series.bars] == ["20260912", "20260915"]
    assert series.bars[-1].volume == 0
    assert series.complete is True
    assert series.status == "ok"


def test_normalize_candles_rejects_conflicting_duplicate_and_does_not_claim_complete_window():
    series = normalize_candle_pages(
        [{"candles": [_candle("20260915", close="100"), _candle("20260915", close="101")] }],
        symbol="005930", expected_dates=["20260915"], fetched_at=FETCHED,
    )

    assert series.bars == ()
    assert series.complete is False
    assert series.missing_dates == ("20260915",)
    assert series.status == "invalid"


def test_normalize_candles_treats_today_bar_as_partial_even_after_time_passes():
    series = normalize_candle_pages(
        [{"candles": [_candle("20260916")] }], symbol="005930",
        expected_dates=["20260916"], fetched_at=FETCHED,
    )

    assert series.bars[0].complete is False
    assert series.complete is False
    assert series.status == "partial"


def test_normalize_candles_does_not_fill_expected_window_with_outside_dates():
    series = normalize_candle_pages(
        [{"candles": [_candle("20260911"), _candle("20260915")] }], symbol="005930",
        expected_dates=["20260912", "20260915"], fetched_at=FETCHED,
    )

    assert [bar.bar_date for bar in series.bars] == ["20260915"]
    assert series.complete is False
    assert series.missing_dates == ("20260912",)
    assert series.status == "partial"


def test_compose_quote_uses_explicit_previous_trading_day_not_prior_available_bar():
    trade_fetched = datetime(2026, 9, 15, 15, 0, tzinfo=KST)
    quote = Quote("005930", Decimal("105"), trade_fetched - timedelta(seconds=10), trade_fetched, "ok", frozenset(), "krx", "KRW")
    series = normalize_candle_pages(
        [{"candles": [_candle("20260915", close="100"), _candle("20260914", close="100"), _candle("20260912", close="90")] }],
        symbol="005930", expected_dates=["20260912", "20260914", "20260915"], fetched_at=FETCHED,
        market_basis="krx",
    )

    payload = compose_quote(
        quote, series, trading_date="20260915", previous_trading_date="20260914",
        required_fields={"price", "prev_close", "change_pct", "open", "volume"},
    )

    assert payload == {"price": 105.0, "prev_close": 100.0, "change_pct": 5.0, "open": 100.0, "volume": 100}


def test_compose_quote_returns_empty_when_required_today_bar_is_missing_or_basis_unknown():
    quote = Quote("005930", Decimal("105"), NOW - timedelta(seconds=10), FETCHED, "ok", frozenset(), "unknown", "KRW")
    no_today = normalize_candle_pages(
        [{"candles": [_candle("20260915", close="100")] }], symbol="005930",
        expected_dates=["20260915"], fetched_at=FETCHED, market_basis="unknown",
    )

    assert compose_quote(quote, no_today, trading_date="20260916", previous_trading_date="20260915", required_fields={"price", "open", "volume"}) == {}
    assert compose_quote(quote, no_today, trading_date="20260915", previous_trading_date="20260912", required_fields={"price", "prev_close"}) == {}


def test_compose_quote_requires_fresh_price_even_for_price_only_request():
    stale_quote = Quote("005930", Decimal("105"), NOW - timedelta(hours=1), FETCHED, "stale", frozenset(), "krx", "KRW")
    series = normalize_candle_pages([], symbol="005930", expected_dates=[], fetched_at=FETCHED, market_basis="krx")

    assert compose_quote(stale_quote, series, trading_date="20260916", previous_trading_date="20260915", required_fields={"price"}) == {}


def test_compose_quote_needs_quote_on_target_date_and_strictly_earlier_previous_date_but_not_today_ohlcv_for_change():
    fetched = datetime(2026, 9, 16, 10, 0, tzinfo=KST)
    quote = Quote("005930", Decimal("105"), fetched - timedelta(seconds=10), fetched, "ok", frozenset(), "krx", "KRW")
    previous_only = normalize_candle_pages(
        [{"candles": [_candle("20260915", close="100")] }], symbol="005930",
        expected_dates=["20260915"], fetched_at=fetched, market_basis="krx",
    )

    assert compose_quote(quote, previous_only, trading_date="20260916", previous_trading_date="20260915", required_fields={"price", "prev_close", "change_pct"}) == {"price": 105.0, "prev_close": 100.0, "change_pct": 5.0}
    assert compose_quote(quote, previous_only, trading_date="20260915", previous_trading_date="20260914", required_fields={"price", "prev_close", "change_pct"}) == {}
    assert compose_quote(quote, previous_only, trading_date="20260916", previous_trading_date="20260916", required_fields={"price", "prev_close", "change_pct"}) == {}


@pytest.mark.parametrize("basis", ["", "unknown", "unsupported", None])
def test_compose_quote_rejects_empty_unknown_or_unsupported_market_basis(basis):
    quote = Quote("005930", Decimal("105"), NOW - timedelta(seconds=10), FETCHED, "ok", frozenset(), basis, "KRW")
    previous = Candle("20260915", Decimal("100"), Decimal("110"), Decimal("90"), Decimal("100"), 100, True, True, basis)
    series = CandleSeries((previous,), True, (), "ok")

    assert compose_quote(quote, series, trading_date="20260916", previous_trading_date="20260915", required_fields={"price", "prev_close", "change_pct"}) == {}


def test_compose_quote_rejects_mixed_adjusted_history_before_calculating_change():
    quote = Quote("005930", Decimal("105"), NOW - timedelta(seconds=10), FETCHED, "ok", frozenset(), "krx", "KRW")
    today = Candle("20260915", Decimal("100"), Decimal("110"), Decimal("90"), Decimal("100"), 100, True, True, "krx")
    previous = Candle("20260914", Decimal("90"), Decimal("110"), Decimal("80"), Decimal("100"), 100, True, False, "krx")
    mixed = CandleSeries((previous, today), True, (), "ok")

    assert compose_quote(quote, mixed, trading_date="20260915", previous_trading_date="20260914", required_fields={"price", "prev_close", "change_pct"}) == {}
