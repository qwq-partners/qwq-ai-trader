"""Pure Toss price/candle normalization and bounded daily-candle collection.

This module has no token, retry, cache, or network implementation.  It accepts
the read-only client boundary and its shared RequestBudget as injected protocols.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Mapping, Sequence
from zoneinfo import ZoneInfo

from src.data.providers.toss.market_types import Candle, CandleSeries, Quote
from src.data.providers.toss.transport import TossRequestError


KST = ZoneInfo("Asia/Seoul")
_STATUSES = frozenset({"ok", "partial", "missing", "invalid", "stale"})
_SUPPORTED_PAYLOAD_FIELDS = frozenset({
    "price", "open", "high", "low", "close", "volume", "prev_close", "change_pct",
})
# These are internal explicit labels only.  A Toss source remains ``unknown``
# until its concrete market-basis evidence is supplied by an upper boundary.
_SUPPORTED_MARKET_BASES = frozenset({"krx", "krx_nxt"})


class MarketDataError(Exception):
    """A safe normalization error.  It deliberately contains no response body."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _require_aware_not_future(value: datetime, now: datetime, *, code: str) -> None:
    if (
        now.tzinfo is None
        or now.utcoffset() is None
        or value.tzinfo is None
        or value.utcoffset() is None
        or value > now
    ):
        raise MarketDataError(code)


def _as_decimal(value: Any, *, positive: bool = True) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise ValueError("not a numeric value")
    if isinstance(value, str) and len(value) > 30:
        raise ValueError("numeric string too long")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError("not a decimal") from None
    if not number.is_finite() or (positive and number <= 0):
        raise ValueError("non-finite or out-of-range")
    # Reject values that can exhaust Decimal work or are not representable market ticks.
    if abs(number.adjusted()) > 15 or abs(number.as_tuple().exponent) > 12:
        raise ValueError("decimal exponent out of range")
    return number


def _as_volume(value: Any) -> int:
    if isinstance(value, bool) or value is None:
        raise ValueError("not a volume")
    if isinstance(value, str) and len(value) > 30:
        raise ValueError("numeric string too long")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError("not a volume") from None
    if not number.is_finite() or number < 0 or number != number.to_integral_value():
        raise ValueError("invalid volume")
    if number.adjusted() > 18:
        raise ValueError("volume out of range")
    return int(number)


def _parse_timestamp(value: Any) -> datetime:
    if not isinstance(value, str):
        raise ValueError("timestamp missing")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("timestamp invalid") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp naive")
    return parsed


def _missing_quote(symbol: str, fetched_at: datetime, *, status: str = "missing") -> Quote:
    return Quote(
        symbol=symbol, price=None, observed_at=None, fetched_at=fetched_at,
        status=status, missing_fields=frozenset({"price", "observed_at", "currency"}),
        market_basis="unknown", currency=None,
    )


def _price_result_rows(body: object) -> Iterable[object]:
    if isinstance(body, Mapping):
        result = body.get("result", body)
    else:
        result = body
    return result if isinstance(result, list) else ()


def parse_prices(
    body: object,
    *,
    symbols: Sequence[str],
    fetched_at: datetime,
    now: datetime,
    max_age_seconds: int,
) -> dict[str, Quote]:
    """Validate a ``/prices`` result without inventing missing market metadata."""
    _require_aware_not_future(fetched_at, now, code="invalid_fetched_at")
    if isinstance(max_age_seconds, bool) or not isinstance(max_age_seconds, int) or max_age_seconds <= 0:
        raise MarketDataError("invalid_max_age")

    requested = tuple(symbols)
    if any(not isinstance(symbol, str) or not symbol for symbol in requested):
        raise MarketDataError("invalid_symbol")
    quotes = {symbol: _missing_quote(symbol, fetched_at) for symbol in requested}
    seen_symbols: set[str] = set()

    for row in _price_result_rows(body):
        if not isinstance(row, Mapping):
            continue
        symbol = row.get("symbol")
        if not isinstance(symbol, str):
            continue
        if symbol not in quotes:
            continue
        # Every duplicate is ambiguous as a single point-in-time observation.
        # Fail closed permanently, independent of validity or response ordering.
        if symbol in seen_symbols:
            quotes[symbol] = _missing_quote(symbol, fetched_at, status="invalid")
            continue
        seen_symbols.add(symbol)
        currency = row.get("currency")
        if currency != "KRW":
            quotes[symbol] = _missing_quote(symbol, fetched_at, status="invalid")
            continue
        try:
            price = _as_decimal(row.get("lastPrice"))
        except ValueError:
            quotes[symbol] = _missing_quote(symbol, fetched_at, status="invalid")
            continue

        raw_timestamp = row.get("timestamp")
        if raw_timestamp is None:
            quotes[symbol] = Quote(symbol, price, None, fetched_at, "partial", frozenset({"observed_at"}), "unknown", "KRW")
            continue
        try:
            observed_at = _parse_timestamp(raw_timestamp)
        except ValueError:
            quotes[symbol] = Quote(symbol, price, None, fetched_at, "invalid", frozenset({"observed_at"}), "unknown", "KRW")
            continue

        if observed_at > fetched_at:
            quotes[symbol] = _missing_quote(symbol, fetched_at, status="invalid")
            continue
        age_seconds = (now - observed_at).total_seconds()
        if age_seconds < 0 or age_seconds > max_age_seconds:
            quotes[symbol] = Quote(symbol, price, observed_at, fetched_at, "stale", frozenset(), "unknown", "KRW")
            continue
        quotes[symbol] = Quote(symbol, price, observed_at, fetched_at, "ok", frozenset(), "unknown", "KRW")

    return quotes


def _normalize_expected_dates(expected_dates: Sequence[str]) -> tuple[str, ...]:
    normalized: list[str] = []
    for item in expected_dates:
        if not isinstance(item, str) or len(item) != 8 or not item.isdigit():
            raise MarketDataError("invalid_expected_date")
        try:
            datetime.strptime(item, "%Y%m%d")
        except ValueError:
            raise MarketDataError("invalid_expected_date") from None
        if item not in normalized:
            normalized.append(item)
    return tuple(normalized)


def _page_payload(page: object) -> Mapping[str, Any] | None:
    if not isinstance(page, Mapping):
        return None
    result = page.get("result", page)
    return result if isinstance(result, Mapping) else None


def _parse_candle(
    row: Mapping[str, Any],
    *,
    fetched_day: str,
    market_basis: str,
    adjusted: bool,
) -> Candle:
    timestamp = _parse_timestamp(row.get("timestamp"))
    bar_date = timestamp.astimezone(KST).strftime("%Y%m%d")
    if bar_date > fetched_day:
        raise ValueError("future candle")
    if row.get("currency") != "KRW":
        raise ValueError("currency mismatch")
    open_price = _as_decimal(row.get("openPrice"))
    high = _as_decimal(row.get("highPrice"))
    low = _as_decimal(row.get("lowPrice"))
    close = _as_decimal(row.get("closePrice"))
    volume = _as_volume(row.get("volume"))
    if high < max(open_price, close) or low > min(open_price, close) or high < low:
        raise ValueError("inconsistent OHLC")
    # A current-day bar has no completion proof in this endpoint contract.  Do not
    # change this merely because a later caller happens to run after market close.
    return Candle(
        bar_date=bar_date, open=open_price, high=high, low=low, close=close,
        volume=volume, complete=(bar_date < fetched_day), adjusted=adjusted,
        market_basis=market_basis,
    )


def normalize_candle_pages(
    pages: Iterable[object],
    *,
    symbol: str,
    expected_dates: Sequence[str],
    fetched_at: datetime,
    market_basis: str = "unknown",
    adjusted: bool = True,
) -> CandleSeries:
    """Return only the caller's requested confirmed dates, in chronological order."""
    if fetched_at.tzinfo is None or fetched_at.utcoffset() is None:
        raise MarketDataError("invalid_fetched_at")
    if not isinstance(symbol, str) or not symbol:
        raise MarketDataError("invalid_symbol")
    if not isinstance(adjusted, bool):
        raise MarketDataError("invalid_adjusted")
    expected = _normalize_expected_dates(expected_dates)
    expected_set = set(expected)
    fetched_day = fetched_at.astimezone(KST).strftime("%Y%m%d")
    accepted: dict[str, Candle] = {}
    conflicted: set[str] = set()
    invalid_expected = False

    for page in pages:
        payload = _page_payload(page)
        if payload is None:
            invalid_expected = invalid_expected or bool(expected)
            continue
        candles = payload.get("candles")
        if not isinstance(candles, list):
            invalid_expected = invalid_expected or bool(expected)
            continue
        for row in candles:
            if not isinstance(row, Mapping):
                invalid_expected = invalid_expected or bool(expected)
                continue
            try:
                candle = _parse_candle(
                    row, fetched_day=fetched_day, market_basis=market_basis, adjusted=adjusted,
                )
            except ValueError:
                # A malformed row without a safely-derived date cannot prove any
                # requested date.  Keep a complete window from being claimed.
                invalid_expected = invalid_expected or bool(expected)
                continue
            if candle.bar_date not in expected_set:
                continue
            if candle.bar_date in conflicted:
                continue
            prior = accepted.get(candle.bar_date)
            if prior is None:
                accepted[candle.bar_date] = candle
            elif prior != candle:
                accepted.pop(candle.bar_date, None)
                conflicted.add(candle.bar_date)

    bars = tuple(sorted(accepted.values(), key=lambda bar: bar.bar_date))
    missing_dates = tuple(sorted(expected_set - set(accepted)))
    has_partial = any(not bar.complete for bar in bars)
    complete = not missing_dates and not has_partial and not invalid_expected
    if complete:
        status = "ok"
    elif invalid_expected or conflicted:
        status = "invalid"
    elif not bars and not expected:
        status = "ok"
    else:
        status = "partial"
    return CandleSeries(bars=bars, complete=complete, missing_dates=missing_dates, status=status)


async def fetch_daily_candles(
    client: Any,
    *,
    symbol: str,
    expected_dates: Sequence[str],
    fetched_at: datetime,
    budget: Any,
    market_basis: str = "unknown",
    adjusted: bool = True,
) -> CandleSeries:
    """Collect daily pages under the *one* client-owned request budget.

    The collector never retries: a client-level retry already consumes the shared
    budget, and a failed later page returns a partial series rather than success.
    """
    expected = _normalize_expected_dates(expected_dates)
    pages: list[object] = []
    cursor: str | None = None
    cursor_time: datetime | None = None
    seen_cursors: set[str] = set()

    while True:
        try:
            if budget.remaining() <= 0:
                break
        except TossRequestError:
            break
        params: dict[str, Any] = {
            "symbol": symbol, "interval": "1d", "count": 200, "adjusted": adjusted,
        }
        if cursor is not None:
            params["before"] = cursor
        try:
            page = await client.get("/api/v1/candles", params=params, budget=budget)
        except Exception:
            break
        pages.append(page)
        series = normalize_candle_pages(
            pages, symbol=symbol, expected_dates=expected, fetched_at=fetched_at,
            market_basis=market_basis, adjusted=adjusted,
        )
        if series.complete:
            return series
        payload = _page_payload(page)
        next_before = payload.get("nextBefore") if payload is not None else None
        if next_before is None or not isinstance(next_before, str):
            break
        try:
            next_time = _parse_timestamp(next_before)
        except ValueError:
            break
        if (
            next_time > fetched_at
            or next_before in seen_cursors
            or (cursor_time is not None and next_time >= cursor_time)
        ):
            break
        seen_cursors.add(next_before)
        cursor = next_before
        cursor_time = next_time

    return normalize_candle_pages(
        pages, symbol=symbol, expected_dates=expected, fetched_at=fetched_at,
        market_basis=market_basis, adjusted=adjusted,
    )


def compose_quote(
    quote: Quote,
    series: CandleSeries,
    *,
    trading_date: str,
    previous_trading_date: str,
    required_fields: set[str],
) -> dict[str, float | int]:
    """Build a legacy numeric payload only when every requested field is proven."""
    if not required_fields or not required_fields <= _SUPPORTED_PAYLOAD_FIELDS:
        return {}
    if quote.status != "ok" or quote.price is None or quote.observed_at is None:
        return {}
    if quote.observed_at.tzinfo is None or quote.observed_at.utcoffset() is None:
        return {}
    if quote.fetched_at.tzinfo is None or quote.fetched_at.utcoffset() is None:
        return {}
    if quote.observed_at > quote.fetched_at:
        return {}
    try:
        _normalize_expected_dates([trading_date, previous_trading_date])
    except MarketDataError:
        return {}
    target_date = trading_date
    previous_date = previous_trading_date
    if previous_date >= target_date:
        return {}
    if quote.observed_at.astimezone(KST).strftime("%Y%m%d") != target_date:
        return {}

    payload: dict[str, float | int] = {"price": float(quote.price)} if "price" in required_fields else {}
    candle_fields = required_fields - {"price"}
    if not candle_fields:
        return payload
    if (
        not series.complete
        or series.status != "ok"
        or not isinstance(quote.market_basis, str)
        or quote.market_basis not in _SUPPORTED_MARKET_BASES
    ):
        return {}
    by_date = {bar.bar_date: bar for bar in series.bars}
    today = by_date.get(trading_date)
    previous = by_date.get(previous_trading_date)
    if any(field in candle_fields for field in {"open", "high", "low", "close", "volume"}):
        if today is None or not today.complete:
            return {}
    if any(field in candle_fields for field in {"prev_close", "change_pct"}):
        if previous is None or not previous.complete:
            return {}
    relevant = [bar for bar in (today, previous) if bar is not None]
    if any(
        not isinstance(bar.market_basis, str)
        or bar.market_basis not in _SUPPORTED_MARKET_BASES
        or bar.market_basis != quote.market_basis
        for bar in relevant
    ):
        return {}
    # A Quote has no adjusted flag because a live last price is not itself an
    # adjusted history.  The historical legs still must share one explicit
    # adjusted basis; a hand-assembled mixed series cannot be composed.
    if len({bar.adjusted for bar in relevant}) > 1:
        return {}

    if "open" in required_fields:
        payload["open"] = float(today.open)  # type: ignore[union-attr]
    if "high" in required_fields:
        payload["high"] = float(today.high)  # type: ignore[union-attr]
    if "low" in required_fields:
        payload["low"] = float(today.low)  # type: ignore[union-attr]
    if "close" in required_fields:
        payload["close"] = float(today.close)  # type: ignore[union-attr]
    if "volume" in required_fields:
        payload["volume"] = today.volume  # type: ignore[union-attr]
    if "prev_close" in required_fields:
        payload["prev_close"] = float(previous.close)  # type: ignore[union-attr]
    if "change_pct" in required_fields:
        payload["change_pct"] = float((quote.price / previous.close - Decimal("1")) * Decimal("100"))  # type: ignore[union-attr]
    return payload
