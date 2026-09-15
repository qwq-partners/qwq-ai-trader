"""Immutable, provider-local market-data contracts for Toss read-only data."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import FrozenSet, Optional, Tuple

from src.utils.data_freshness import DataPoint, missing


@dataclass(frozen=True)
class Quote:
    """A price observation; ``fetched_at`` never substitutes for ``observed_at``."""

    symbol: str
    price: Optional[Decimal]
    observed_at: Optional[datetime]
    fetched_at: datetime
    status: str
    missing_fields: FrozenSet[str]
    market_basis: str
    currency: Optional[str]

    def to_data_point(self, max_age_seconds: int) -> DataPoint:
        """Expose quote freshness through the shared utility without changing it."""
        if self.price is None:
            return missing("toss", "price missing")
        return DataPoint(
            value=self.price,
            as_of=self.observed_at,
            source="toss",
            ttl_seconds=max_age_seconds,
            missing_reason=("observed_at missing" if self.observed_at is None else None),
        )


@dataclass(frozen=True)
class Candle:
    """One KST daily bar.  ``complete`` is collection evidence, not elapsed time."""

    bar_date: str
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int
    complete: bool
    adjusted: bool
    market_basis: str


@dataclass(frozen=True)
class CandleSeries:
    """Requested-window result, always chronological and never silently filled."""

    bars: Tuple[Candle, ...]
    complete: bool
    missing_dates: Tuple[str, ...]
    status: str
