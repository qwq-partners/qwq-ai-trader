"""약세장 진입 때의 선제 stale 청산 후보를 순수하게 고른다.

이 모듈은 기존 ``BatchAnalyzer._preemptive_stale_exit_on_bear``의 선택 조건만
재현한다. 주문 신호, outbox, 효과 ID와 실행 권한은 호출자 소유이며 여기서는
만들지 않는다. ``prices``는 동일 관측 묶음에서 호출자가 넘긴 현재가일 뿐
가격 출처 증명이나 거래 허가가 아니다.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from ...core.types import StrategyType
from .economics import decode_portfolio
from .protection import decode_protection


_KST = ZoneInfo("Asia/Seoul")
_KNOWN_STRATEGIES = frozenset(strategy.value for strategy in StrategyType)


@dataclass(frozen=True)
class StaleExitCandidate:
    """효과를 아직 만들지 않은, 선제 stale 청산의 불변 입력값."""

    symbol: str
    quantity: int
    price: Decimal
    strategy: str
    business_days: int
    pnl_pct: float
    reason: str


def _business_days(entry_day: date, today: date, holidays: frozenset[date]) -> int:
    """기존 BatchAnalyzer와 같은, 진입 다음 날부터 오늘까지의 영업일 수."""
    return sum(
        1
        for offset in range((today - entry_day).days)
        if (day := entry_day + timedelta(days=offset + 1)).weekday() < 5 and day not in holidays
    )


def select_preemptive_stale(
    portfolio_dto: dict,
    protection_dto: dict,
    *,
    today: date,
    prices: dict[str, Decimal],
    holidays: frozenset[date],
) -> tuple[StaleExitCandidate, ...]:
    """기존 약세장 stale 청산 조건에 맞는 후보만 반환한다.

    DTO는 실제 ``Portfolio``/``ExitManager``와 동일한 엄격한 복호화 경로를
    거친다. 복호화 계산기는 ``persist=False``이고, 그 clock도 인계된 ``today``로
    고정된다. 입력 DTO나 live 객체를 수정하지 않는다.
    """
    if type(today) is not date:
        raise ValueError("today는 date여야 합니다")
    if type(holidays) is not frozenset or any(type(day) is not date for day in holidays):
        raise ValueError("holidays는 date frozenset이어야 합니다")
    if type(prices) is not dict or any(
        type(symbol) is not str or type(price) is not Decimal
        or not price.is_finite() or price < 0
        for symbol, price in prices.items()
    ):
        raise ValueError("prices는 Decimal 현재가 mapping이어야 합니다")

    portfolio = decode_portfolio(deepcopy(portfolio_dto))
    for symbol, position in portfolio.positions.items():
        if symbol in prices:
            position.current_price = prices[symbol]
    fixed_clock = lambda: datetime.combine(today, time.min, tzinfo=_KST)
    protection = decode_protection(deepcopy(protection_dto), clock=fixed_clock)
    candidates = []

    for symbol, position in portfolio.positions.items():
        if protection.is_exit_exempt(symbol) or position.strategy == "core_holding":
            continue
        entry_time = position.entry_time
        if entry_time is None:
            continue
        entry_day = entry_time.date() if hasattr(entry_time, "date") else entry_time
        business_days = _business_days(entry_day, today, holidays)
        if business_days < 5:
            continue
        pnl_pct = float(position.unrealized_pnl_pct or 0)
        if pnl_pct >= 1.0:
            continue

        # Explicit 0 remains the legacy -100% PnL observation; only the proposed
        # order price falls back to the average price.
        price = position.current_price or position.avg_price
        strategy = position.strategy if position.strategy in _KNOWN_STRATEGIES else StrategyType.SEPA_TREND.value
        reason = f"P2-C 선제 stale 청산: {business_days}일 보유, PnL {pnl_pct:+.1f}%, 약세장 진입"
        candidates.append(StaleExitCandidate(
            symbol=symbol,
            quantity=position.quantity,
            price=price,
            strategy=strategy,
            business_days=business_days,
            pnl_pct=pnl_pct,
            reason=reason,
        ))

    return tuple(candidates)
