"""
캘린더 시즈널리티 오버레이 — 월말월초(turn-of-month) · US 옵션만기주 사이징 부스트

awesome-systematic-trading 재현 백테스트에서 확인된 저변동 캘린더 이상현상을
독립 전략이 아닌 **기존 전략의 사이징 배율**로만 반영한다 (2026-08-03 도입).

  - Turn of the Month (Sharpe 0.305, 변동성 7.2%): 월 마지막 2거래일 + 익월 첫 3거래일
    → KR/US 공통 부스트 1.10
  - Option-Expiration Week (Sharpe 0.452, 변동성 5.0%): 3번째 금요일이 낀 주 대형주 강세
    → **US 전용** 부스트 1.05 (논문 근거가 미국 시장. KR 옵션만기주는 수급 변동성만
      커서 근거 없이 적용하지 않는다)

설계 원칙:
  - 부스트만 있고 차단/축소는 없다 — 근거 없는 방어 규칙은 만들지 않는다
  - 배율 적용 후에도 max_position_pct 상한은 호출측에서 재적용해야 한다
  - 환경변수 CALENDAR_SEASONALITY=0 으로 전체 비활성화
"""

import os
from datetime import date, timedelta
from typing import List, Tuple

from loguru import logger

from .session import is_kr_market_holiday

TOM_MULT = 1.10          # turn-of-month 부스트
OPEX_MULT = 1.05         # US 옵션만기주 부스트
TOM_LAST_DAYS = 2        # 월말 마지막 N거래일
TOM_FIRST_DAYS = 3       # 월초 첫 N거래일

# 하루 한 번만 INFO 로그 (사이징은 신호마다 호출된다)
_logged: set = set()


def _enabled() -> bool:
    return os.getenv("CALENDAR_SEASONALITY", "1") != "0"


def _kr_is_trading_day(d: date) -> bool:
    return not is_kr_market_holiday(d)


def _us_is_trading_day(d: date) -> bool:
    """US 거래일 근사 — 주말만 제외 (공휴일 미스는 5일 윈도우 판정에서 ±1일 오차뿐)"""
    return d.weekday() < 5


def _month_trading_days(year: int, month: int, is_trading_day) -> List[date]:
    d = date(year, month, 1)
    days = []
    while d.month == month:
        if is_trading_day(d):
            days.append(d)
        d += timedelta(days=1)
    return days


def _is_turn_of_month(d: date, is_trading_day) -> bool:
    """월 마지막 TOM_LAST_DAYS거래일 또는 첫 TOM_FIRST_DAYS거래일인가"""
    days = _month_trading_days(d.year, d.month, is_trading_day)
    if not days or d not in days:
        return False
    idx = days.index(d)
    return idx < TOM_FIRST_DAYS or idx >= len(days) - TOM_LAST_DAYS


def _is_us_opex_week(d: date) -> bool:
    """3번째 금요일이 포함된 주(월~금)인가"""
    fridays = [
        dd for dd in _month_trading_days(d.year, d.month, lambda x: x.weekday() == 4)
    ]
    if len(fridays) < 3:
        return False
    opex = fridays[2]
    week_start = opex - timedelta(days=opex.weekday())  # 해당 주 월요일
    return week_start <= d <= opex


def calendar_multiplier(d: date, market: str = "kr") -> Tuple[float, str]:
    """
    캘린더 시즈널리티 사이징 배율.

    Returns:
        (배율, 사유 태그) — 해당 없음이면 (1.0, "")
    """
    if not _enabled():
        return 1.0, ""

    market = market.lower()
    is_td = _kr_is_trading_day if market == "kr" else _us_is_trading_day

    mult = 1.0
    tags = []

    if _is_turn_of_month(d, is_td):
        mult *= TOM_MULT
        tags.append("월말월초")

    if market == "us" and _is_us_opex_week(d):
        mult *= OPEX_MULT
        tags.append("옵션만기주")

    tag = "+".join(tags)
    if mult != 1.0:
        key = (d.isoformat(), market)
        if key not in _logged:
            _logged.add(key)
            if len(_logged) > 64:  # 무한 성장 방지
                _logged.clear()
                _logged.add(key)
            logger.info(
                f"[시즈널리티] {market.upper()} {d} {tag} → 사이징 ×{mult:.2f}"
            )
    return mult, tag
