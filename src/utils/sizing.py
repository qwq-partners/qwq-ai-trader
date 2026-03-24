"""ATR 기반 포지션 사이징 유틸리티"""


def atr_position_multiplier(atr_pct: float) -> float:
    """ATR(%) → 포지션 배율 매핑

    | ATR   | multiplier | 효과       |
    |-------|-----------|------------|
    | ≤ 3%  | 1.0       | 정상 비중   |
    | 3~5%  | 0.8       | 20% 축소   |
    | 5~8%  | 0.6       | 40% 축소   |
    | > 8%  | 0.4       | 60% 축소   |
    """
    if atr_pct <= 3:
        return 1.0
    elif atr_pct <= 5:
        return 0.8
    elif atr_pct <= 8:
        return 0.6
    else:
        return 0.4
