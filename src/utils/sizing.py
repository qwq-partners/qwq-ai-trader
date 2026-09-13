"""ATR 기반 포지션 사이징 유틸리티"""

from decimal import Decimal
from typing import Optional

from .fee_calculator import FeeCalculator, get_fee_calculator


def atr_position_multiplier(atr_pct: float) -> float:
    """ATR(%) → 포지션 배율 매핑 (선형 보간)

    ATR이 높을수록 배율 축소. 경계값에서 불연속 점프 없이 연속 함수.

    | ATR   | multiplier | 효과       |
    |-------|-----------|------------|
    | ≤ 2%  | 1.0       | 정상 비중   |
    | 5%    | 0.7       | 30% 축소   |
    | 8%    | 0.4       | 60% 축소   |
    | ≥ 10% | 0.3       | 70% 축소   |
    """
    if atr_pct <= 2.0:
        return 1.0
    elif atr_pct >= 10.0:
        return 0.3
    else:
        # 2% ~ 10% 구간: 선형 보간 (1.0 → 0.3)
        return round(1.0 - (atr_pct - 2.0) * (0.7 / 8.0), 3)


def planned_risk(price: Decimal, quantity: int, stop_pct: Decimal,
                 fee_calc: Optional[FeeCalculator] = None) -> Decimal:
    """계획 위험금액 = entry_cost(q) × stop_pct/100, entry_cost = price×q + 매수수수료 (원 단위 반올림).

    KR 손절 판정이 매수 비용 대비 순손익률(FeeCalculator.calculate_net_pnl)이므로 같은 분모를 쓴다.
    왕복 수수료를 SL% 에 다시 더하지 않는다 (매도 비용은 판정 시 net 에 이미 포함).
    """
    fee_calc = fee_calc if fee_calc is not None else get_fee_calculator("KR")
    cost = price * quantity
    return (cost + fee_calc.calculate_buy_fee(cost)) * stop_pct / 100


def risk_quantity_cap(equity: Decimal, price: Decimal, stop_pct: Decimal, *,
                      risk_per_trade_pct: float, fee_calc: Optional[FeeCalculator] = None) -> int:
    """위험 모드 최종 상한 수량 (2026-09-14 T2): planned_risk(q) ≤ equity × risk_per_trade_pct/100 인 최대 q.

    모든 오버레이·최소금액·최소 3주 보정이 끝난 수량에 마지막으로 적용해 줄이기만 한다 (키우지 않음).
    예) equity 1천만·가격 1만·net SL 5%·위험 0.7% → 139주 (140주는 70,009.85 > 70,000 미세 초과).
    """
    if equity <= 0 or price <= 0 or stop_pct <= 0:
        return 0
    fee_calc = fee_calc if fee_calc is not None else get_fee_calculator("KR")
    budget = equity * Decimal(str(risk_per_trade_pct)) / 100
    rate = fee_calc.config.buy_commission_rate
    qty = int(budget * 100 / (stop_pct * price * (1 + rate)))
    # 수수료 원 단위 반올림 때문에 공식값이 1주 초과할 수 있어 실제 계획 위험으로 확인
    while qty > 0 and planned_risk(price, qty, stop_pct, fee_calc) > budget:
        qty -= 1
    return qty
