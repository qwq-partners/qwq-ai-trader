"""미장 보고서 본문과 이미지가 공유하는 외부 접근 없는 가격 검증."""

import math
from datetime import datetime, timezone
from typing import Any, Dict, Optional
from zoneinfo import ZoneInfo


def _finite_number(value: Any, *, positive: bool = False) -> Optional[float]:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(number) or (positive and number <= 0):
        return None
    return number


def observe_us_quote(
    quote: Any, *, now: Optional[datetime] = None,
) -> Optional[Dict[str, Any]]:
    """유한 가격·등락과 뉴욕 시장일을 반환하며 부적합한 행은 ``None``이다.

    시장 시각이 없으면 유효한 수치와 기준일 미확인을 유지한다. 명시된 시각은
    유한 양수·현재 이하이어야 한다. 주입한 naive 시계는 한국 시각이며
    기본 시계는 timezone이 있는 UTC다.
    """
    if not isinstance(quote, dict):
        return None
    price = _finite_number(quote.get("price"), positive=True)
    pct = _finite_number(quote.get("change_pct"))
    if price is None or pct is None:
        return None

    market_time = quote.get("market_time")
    if market_time is None:
        return {"price": price, "pct": pct, "date": None, "date_label": "기준일 미확인"}

    stamp = _finite_number(market_time, positive=True)
    if stamp is None:
        return None
    reference = now if now is not None else datetime.now(timezone.utc)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=ZoneInfo("Asia/Seoul"))
    if stamp > reference.timestamp():
        return None
    try:
        market_day = datetime.fromtimestamp(
            stamp, tz=timezone.utc,
        ).astimezone(ZoneInfo("America/New_York"))
    except (OSError, OverflowError, ValueError):
        return None
    return {
        "price": price,
        "pct": pct,
        "date": market_day.strftime("%Y.%m.%d"),
        "date_label": market_day.strftime("%m/%d"),
    }
