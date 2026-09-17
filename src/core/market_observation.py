"""Immutable, source-time provenance for an accepted market observation.

This module deliberately has no feed, clock, network, or trading dependencies.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
import math
from typing import Any, ClassVar, Dict
from uuid import UUID
from zoneinfo import ZoneInfo


KST = ZoneInfo("Asia/Seoul")


@dataclass(frozen=True)
class MarketObservation:
    """Canonical facts from one validated KIS market-price record.

    ``market_as_of`` and ``source_event_id`` are derived facts: callers provide
    the raw KIS date/time and local record coordinates, never a replacement
    timestamp or identity.  ``received_at`` is intentionally separate from
    the exchange's source time.
    """

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
    source: str
    tr_id: str
    exchange: str
    raw_date: str
    raw_time: str
    received_at: datetime
    connection_id: str
    frame_sequence: int
    record_index: int
    record_digest: str
    market_as_of: datetime = field(init=False)
    source_event_id: str = field(init=False)

    _SCHEMA_VERSION: ClassVar[int] = 1
    _TR_EXCHANGES: ClassVar[Dict[str, str]] = {
        "H0STCNT0": "KRX",
        "H0NXCNT0": "NXT",
    }

    def __post_init__(self) -> None:
        self._require_clean_symbol(self.symbol)
        expected_exchange = self._TR_EXCHANGES.get(self.tr_id)
        if expected_exchange is None:
            raise ValueError("unsupported market observation TR")
        if self.exchange != expected_exchange:
            raise ValueError("market observation exchange does not match TR")
        if self.source != f"kis_websocket:{self.tr_id}":
            raise ValueError("market observation source does not match TR")
        if self.change_sign not in {"1", "2", "3", "4", "5"}:
            raise ValueError("invalid market observation change sign")

        for name in ("price", "open", "high", "low"):
            self._require_decimal(getattr(self, name), name, positive=True)
        for name in ("value", "change", "change_pct"):
            self._require_decimal(getattr(self, name), name)
        if not math.isfinite(float(self.change_pct)):
            raise ValueError("invalid market observation change_pct")
        if not isinstance(self.volume, int) or isinstance(self.volume, bool) or self.volume < 0:
            raise ValueError("invalid market observation volume")
        if self.value < 0:
            raise ValueError("invalid market observation value")
        if not isinstance(self.received_at, datetime) or self.received_at.tzinfo is None:
            raise ValueError("market observation received_at must be timezone-aware")
        if self.received_at.utcoffset() is None:
            raise ValueError("market observation received_at must be timezone-aware")
        self._require_connection_uuid(self.connection_id)
        if not isinstance(self.frame_sequence, int) or isinstance(self.frame_sequence, bool) or self.frame_sequence < 1:
            raise ValueError("invalid market observation frame sequence")
        if not isinstance(self.record_index, int) or isinstance(self.record_index, bool) or self.record_index < 1:
            raise ValueError("invalid market observation record index")
        if not isinstance(self.record_digest, str) or len(self.record_digest) != 64:
            raise ValueError("invalid market observation digest")
        if any(char not in "0123456789abcdef" for char in self.record_digest):
            raise ValueError("invalid market observation digest")

        market_as_of = self._derive_market_as_of(self.raw_date, self.raw_time)
        object.__setattr__(self, "market_as_of", market_as_of)
        object.__setattr__(
            self,
            "source_event_id",
            f"{self.connection_id}:{self.frame_sequence}:{self.record_index}",
        )

    @staticmethod
    def _require_clean_text(value: str, name: str) -> None:
        if not isinstance(value, str) or not value:
            raise ValueError(f"invalid market observation {name}")
        if not value.isascii() or any(char.isspace() or not char.isprintable() for char in value):
            raise ValueError(f"invalid market observation {name}")

    @classmethod
    def _require_clean_symbol(cls, symbol: str) -> None:
        cls._require_clean_text(symbol, "symbol")
        if "^" in symbol or "|" in symbol:
            raise ValueError("invalid market observation symbol")

    @classmethod
    def _require_connection_uuid(cls, value: str) -> None:
        cls._require_clean_text(value, "connection id")
        try:
            parsed = UUID(value)
        except (ValueError, AttributeError) as exc:
            raise ValueError("invalid market observation connection id") from exc
        if str(parsed) != value:
            raise ValueError("invalid market observation connection id")

    @staticmethod
    def _require_decimal(value: Decimal, name: str, *, positive: bool = False) -> None:
        if not isinstance(value, Decimal) or not value.is_finite():
            raise ValueError(f"invalid market observation {name}")
        if positive and value <= 0:
            raise ValueError(f"invalid market observation {name}")

    @staticmethod
    def _derive_market_as_of(raw_date: str, raw_time: str) -> datetime:
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
            raise ValueError("invalid market observation source date/time")
        try:
            return datetime(
                int(raw_date[:4]),
                int(raw_date[4:6]),
                int(raw_date[6:8]),
                int(raw_time[:2]),
                int(raw_time[2:4]),
                int(raw_time[4:6]),
                tzinfo=KST,
            )
        except ValueError as exc:
            raise ValueError("invalid market observation source date/time") from exc

    def to_dict(self) -> Dict[str, Any]:
        """Return the exact JSON-safe canonical representation for persistence."""
        return {
            "schema_version": self._SCHEMA_VERSION,
            "symbol": self.symbol,
            "price": str(self.price),
            "open": str(self.open),
            "high": str(self.high),
            "low": str(self.low),
            "volume": self.volume,
            "value": str(self.value),
            "change_sign": self.change_sign,
            "change": str(self.change),
            "change_pct": str(self.change_pct),
            "source": self.source,
            "tr_id": self.tr_id,
            "exchange": self.exchange,
            "raw_date": self.raw_date,
            "raw_time": self.raw_time,
            "market_as_of": self.market_as_of.isoformat(),
            "received_at": self.received_at.isoformat(),
            "connection_id": self.connection_id,
            "frame_sequence": self.frame_sequence,
            "record_index": self.record_index,
            "source_event_id": self.source_event_id,
            "record_digest": self.record_digest,
        }

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "MarketObservation":
        """Restore only an exact canonical payload and reject derived conflicts."""
        expected = {
            "schema_version", "symbol", "price", "open", "high", "low", "volume", "value",
            "change_sign", "change", "change_pct", "source", "tr_id", "exchange", "raw_date",
            "raw_time", "market_as_of", "received_at", "connection_id", "frame_sequence",
            "record_index", "source_event_id", "record_digest",
        }
        if not isinstance(payload, dict) or set(payload) != expected:
            raise ValueError("invalid market observation payload fields")
        if type(payload["schema_version"]) is not int or payload["schema_version"] != cls._SCHEMA_VERSION:
            raise ValueError("unsupported market observation payload version")
        for key in ("symbol", "change_sign", "source", "tr_id", "exchange", "raw_date", "raw_time", "connection_id", "record_digest"):
            if not isinstance(payload[key], str) or payload[key].strip() != payload[key]:
                raise ValueError("invalid market observation payload text")
        decimals = {
            key: cls._decimal_from_canonical(payload[key], key)
            for key in ("price", "open", "high", "low", "value", "change", "change_pct")
        }
        received_at = cls._datetime_from_canonical(payload["received_at"], "received_at")
        observation = cls(
            symbol=payload["symbol"],
            price=decimals["price"],
            open=decimals["open"],
            high=decimals["high"],
            low=decimals["low"],
            volume=payload["volume"],
            value=decimals["value"],
            change_sign=payload["change_sign"],
            change=decimals["change"],
            change_pct=decimals["change_pct"],
            source=payload["source"],
            tr_id=payload["tr_id"],
            exchange=payload["exchange"],
            raw_date=payload["raw_date"],
            raw_time=payload["raw_time"],
            received_at=received_at,
            connection_id=payload["connection_id"],
            frame_sequence=payload["frame_sequence"],
            record_index=payload["record_index"],
            record_digest=payload["record_digest"],
        )
        if payload["market_as_of"] != observation.market_as_of.isoformat():
            raise ValueError("market observation market_as_of conflicts with source date/time")
        if payload["source_event_id"] != observation.source_event_id:
            raise ValueError("market observation source_event_id conflicts with coordinates")
        return observation

    @staticmethod
    def _decimal_from_canonical(value: Any, name: str) -> Decimal:
        if not isinstance(value, str) or not value or value.strip() != value:
            raise ValueError(f"invalid market observation {name}")
        try:
            decimal = Decimal(value)
        except Exception as exc:
            raise ValueError(f"invalid market observation {name}") from exc
        if not decimal.is_finite() or str(decimal) != value:
            raise ValueError(f"invalid market observation {name}")
        return decimal

    @staticmethod
    def _datetime_from_canonical(value: Any, name: str) -> datetime:
        if not isinstance(value, str) or not value or value.strip() != value:
            raise ValueError(f"invalid market observation {name}")
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError as exc:
            raise ValueError(f"invalid market observation {name}") from exc
        if parsed.tzinfo is None or parsed.utcoffset() is None or parsed.isoformat() != value:
            raise ValueError(f"invalid market observation {name}")
        return parsed
