"""Strict reporting-only parser for KIS index minute bars.

This adapter deliberately never derives a reporting observation from a current
quote summary: only a dated, timed row from the documented historical output2
is eligible.
"""
from datetime import date, datetime, time, timezone
import math
from typing import Any, Optional
from zoneinfo import ZoneInfo


KST = ZoneInfo("Asia/Seoul")
_LABELS = {"0001": "KOSPI", "1001": "KOSDAQ"}
_DTO_KEYS = frozenset({
    "code", "label", "price", "source_observed_at", "received_at", "source",
    "observation_kind", "finality",
})


def _number(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if math.isfinite(parsed) and parsed > 0 else None


def _observed_at(row: Any, report_date: date) -> Optional[datetime]:
    if not isinstance(row, dict):
        return None
    day, clock = row.get("stck_bsop_date"), row.get("stck_cntg_hour")
    if type(day) is not str or type(clock) is not str or len(day) != 8 or len(clock) != 6:
        return None
    if not (day.isascii() and day.isdigit() and clock.isascii() and clock.isdigit()):
        return None
    try:
        observed_day = datetime.strptime(day, "%Y%m%d").date()
        observed_time = time(int(clock[:2]), int(clock[2:4]), int(clock[4:]))
    except ValueError:
        return None
    if observed_day != report_date or observed_time < time(15, 30):
        return None
    return datetime.combine(observed_day, observed_time, KST)


def parse_index_observation(
    payload: Any, *, index_code: str, report_date: date, received_at: datetime,
) -> Optional[dict]:
    """Return one conservative reporting DTO from a KIS time-index response.

    The KIS ``output1`` current summary is intentionally ignored.  A report is
    eligible only when a same-day output2 minute row has a real valid clock,
    positive finite price, and an observation time no later than receipt.
    """
    if (index_code not in _LABELS or type(report_date) is not date
            or not isinstance(received_at, datetime) or received_at.tzinfo is None
            or received_at.utcoffset() is None or not isinstance(payload, dict)
            or payload.get("rt_cd") != "0"):
        return None
    received_kst = received_at.astimezone(KST)
    if received_kst.date() != report_date:
        return None
    rows = payload.get("output2")
    if not isinstance(rows, list):
        return None

    latest_at: Optional[datetime] = None
    latest_prices: set[float] = set()
    for row in rows:
        observed_at = _observed_at(row, report_date)
        price = _number(row.get("bstp_nmix_prpr")) if isinstance(row, dict) else None
        if observed_at is None or price is None or observed_at > received_kst:
            continue
        if latest_at is None or observed_at > latest_at:
            latest_at, latest_prices = observed_at, {price}
        elif observed_at == latest_at:
            latest_prices.add(price)

    if latest_at is None or len(latest_prices) != 1:
        return None
    price = latest_prices.pop()
    result = {
        "code": index_code,
        "label": _LABELS[index_code],
        "price": price,
        "source_observed_at": latest_at.isoformat(),
        "received_at": received_at.astimezone(timezone.utc).isoformat(),
        "source": "KIS/FHKUP03500200",
        "observation_kind": "historical_minute",
        "finality": "unverified",
    }
    # Keep this explicit so an accidental future reporting field is detected at
    # this narrow provider boundary rather than silently published.
    return result if set(result) == _DTO_KEYS else None
