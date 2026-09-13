"""ATR 기반 포지션 사이징 유틸리티"""

from decimal import Decimal
from typing import Optional, Tuple

from ..indicators.atr import calculate_dynamic_stop_loss


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


def risk_position_value(equity: Decimal, atr_pct: Optional[float], *,
                        risk_per_trade_pct: float, max_position_pct: float,
                        stop_params: Tuple[float, float, float] = (2.0, 4.0, 8.0),
                        fallback_stop_pct: float = 4.0) -> Tuple[Decimal, float]:
    """위험 기반 포지션 금액 (2026-09-13 리뷰 권고 ③ — 백테스트 A/B ladder/current/risk)

    건당 자본 위험 = equity × risk_per_trade_pct. 손절폭은 ExitManager와 같은 규칙
    (ATR × 배수, min~max 클램프 = stop_params)으로 계산해 사이징↔손절이 정합.
    ATR이 없거나 0이면 fallback_stop_pct. 결과는 equity × max_position_pct 로 상한.
    반환: (포지션 금액, 적용 손절폭 %)  — 예) 위험 0.7%·손절 5% → equity의 14%
    """
    mult, lo, hi = stop_params
    if atr_pct is not None and atr_pct > 0:
        stop_pct = calculate_dynamic_stop_loss(atr_pct, min_stop=lo, max_stop=hi, multiplier=mult)
    else:
        stop_pct = fallback_stop_pct
    value = equity * Decimal(str(risk_per_trade_pct)) / Decimal(str(stop_pct))
    cap = equity * Decimal(str(max_position_pct / 100))
    return min(value, cap), float(stop_pct)
