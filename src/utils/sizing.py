"""ATR 기반 포지션 사이징 유틸리티"""


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
