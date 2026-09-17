"""Pure decoder for documented KIS KRX/NXT 46-column market-price frames."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
import hashlib
import math
from typing import Tuple
from zoneinfo import ZoneInfo


KST = ZoneInfo("Asia/Seoul")
PRICE_TR_EXCHANGES = {
    "H0STCNT0": "KRX",
    "H0NXCNT0": "NXT",
}
PRICE_RECORD_FIELD_COUNT = 46


class MarketFrameError(ValueError):
    """A deliberately non-raw diagnostic for a rejected market-price frame."""


@dataclass(frozen=True)
class ParsedKISMarketRecord:
    """Validated source facts; receipt time and local identity are added by the feed."""

    symbol: str
    price: Decimal
    open: Decimal
    high: Decimal
    low: Decimal
    volume: int
    value: Decimal
    change_sign: str
    change: Decimal
    change_pct: Decimal
    tr_id: str
    exchange: str
    raw_date: str
    raw_time: str
    record_digest: str


def parse_market_price_frame(frame: str) -> Tuple[ParsedKISMarketRecord, ...]:
    """Parse one plaintext KIS price frame without clock, I/O, or callbacks.

    Supported symbols are nonempty printable ASCII identifiers with no whitespace
    or frame delimiters.  They are normalized with the feed's established
    ``zfill(6)`` behavior; this intentionally does not impose a numeric-only
    universe policy.  Accepted entry-price OHLC values are strictly positive;
    this is a data-validity boundary, not a symbol-universe decision.
    """
    if not isinstance(frame, str):
        raise MarketFrameError("market frame must be text")
    parts = frame.split("|")
    if len(parts) != 4:
        raise MarketFrameError("market price frame must have exactly four parts")
    marker, tr_id, raw_count, raw_data = parts
    if marker != "0":
        raise MarketFrameError("encrypted market price frames are unsupported")
    exchange = PRICE_TR_EXCHANGES.get(tr_id)
    if exchange is None:
        raise MarketFrameError("unsupported market price TR")
    count = _positive_ascii_count(raw_count)
    fields = raw_data.split("^")
    if len(fields) != PRICE_RECORD_FIELD_COUNT * count:
        raise MarketFrameError("market price record count does not match fields")

    records = []
    for offset in range(0, len(fields), PRICE_RECORD_FIELD_COUNT):
        record_fields = fields[offset:offset + PRICE_RECORD_FIELD_COUNT]
        records.append(_parse_record(record_fields, tr_id=tr_id, exchange=exchange))
    return tuple(records)


def _positive_ascii_count(value: str) -> int:
    if not isinstance(value, str) or not value or not value.isascii() or not value.isdecimal():
        raise MarketFrameError("market price count must be positive ASCII digits")
    count = int(value)
    if count < 1:
        raise MarketFrameError("market price count must be positive")
    return count


def _parse_record(fields: list[str], *, tr_id: str, exchange: str) -> ParsedKISMarketRecord:
    symbol = _symbol(fields[0]).zfill(6)
    raw_time = fields[1]
    price = _positive_decimal_integer(fields[2], "price")
    change_sign = fields[3]
    if change_sign not in {"1", "2", "3", "4", "5"}:
        raise MarketFrameError("invalid market price change sign")
    raw_change = _nonnegative_decimal_integer(fields[4], "change")
    raw_change_pct = _finite_decimal(fields[5], "change percent")
    open_price = _positive_decimal_integer(fields[7], "open")
    high = _positive_decimal_integer(fields[8], "high")
    low = _positive_decimal_integer(fields[9], "low")
    volume = _nonnegative_integer(fields[13], "volume")
    value = _nonnegative_decimal_integer(fields[14], "value")
    raw_date = fields[33]
    _validate_source_datetime(raw_date, raw_time)

    # Preserve the legacy event's only established sign behavior for this slice.
    # Unary Decimal negation applies the ambient context, unlike copy_negate().
    if change_sign == "5":
        # Legacy ``-int(0)`` remains positive zero while ``-float(0.0)`` is
        # signed negative zero.  Keep both observable contracts without rounding.
        change = raw_change if raw_change.is_zero() else raw_change.copy_negate()
        change_pct = raw_change_pct.copy_negate()
    else:
        change = raw_change
        change_pct = raw_change_pct
    digest = hashlib.sha256("^".join(fields).encode("utf-8")).hexdigest()
    return ParsedKISMarketRecord(
        symbol=symbol,
        price=price,
        open=open_price,
        high=high,
        low=low,
        volume=volume,
        value=value,
        change_sign=change_sign,
        change=change,
        change_pct=change_pct,
        tr_id=tr_id,
        exchange=exchange,
        raw_date=raw_date,
        raw_time=raw_time,
        record_digest=digest,
    )


def _symbol(value: str) -> str:
    if not isinstance(value, str) or not value or not value.isascii():
        raise MarketFrameError("invalid market price symbol")
    if any(char.isspace() or not char.isprintable() or char in "^|" for char in value):
        raise MarketFrameError("invalid market price symbol")
    return value


def _nonnegative_integer(value: str, name: str) -> int:
    if not isinstance(value, str) or not value or not value.isascii() or not value.isdecimal():
        raise MarketFrameError(f"invalid market price {name}")
    return int(value)


def _positive_decimal_integer(value: str, name: str) -> Decimal:
    parsed = _nonnegative_decimal_integer(value, name)
    if parsed <= 0:
        raise MarketFrameError(f"invalid market price {name}")
    return parsed


def _nonnegative_decimal_integer(value: str, name: str) -> Decimal:
    if not isinstance(value, str) or not value or not value.isascii() or not value.isdecimal():
        raise MarketFrameError(f"invalid market price {name}")
    return Decimal(value)


def _finite_decimal(value: str, name: str) -> Decimal:
    if not isinstance(value, str):
        raise MarketFrameError(f"invalid market price {name}")
    try:
        # Keep the legacy float lexical acceptance boundary, then retain the
        # original Decimal text rather than round-tripping through float.
        legacy_float = float(value)
        parsed = Decimal(value)
    except (InvalidOperation, ValueError, OverflowError) as exc:
        raise MarketFrameError(f"invalid market price {name}") from exc
    if not parsed.is_finite() or not math.isfinite(legacy_float):
        raise MarketFrameError(f"invalid market price {name}")
    return parsed


def _validate_source_datetime(raw_date: str, raw_time: str) -> None:
    if (
        not isinstance(raw_date, str)
        or len(raw_date) != 8
        or not raw_date.isascii()
        or not raw_date.isdecimal()
        or not isinstance(raw_time, str)
        or len(raw_time) != 6
        or not raw_time.isascii()
        or not raw_time.isdecimal()
    ):
        raise MarketFrameError("invalid market price source date/time")
    try:
        datetime(
            int(raw_date[:4]), int(raw_date[4:6]), int(raw_date[6:8]),
            int(raw_time[:2]), int(raw_time[2:4]), int(raw_time[4:6]), tzinfo=KST,
        )
    except ValueError as exc:
        raise MarketFrameError("invalid market price source date/time") from exc
