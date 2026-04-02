"""
US 시장 체제 판단 — SPY/QQQ 기반

KR의 MarketRegimeAdapter와 동일 개념:
bull/bear/sideways/neutral 4단계 → 전략 파라미터 동적 조정
"""

from datetime import datetime, date
from typing import Dict, Any, Optional

from loguru import logger


# 체제별 파라미터 조정
REGIME_PARAMS = {
    "bull": {
        "min_score_adj": -5,
        "max_daily_new_buys": 3,
        "position_mult_boost": 1.1,
        "description": "강세장 — 적극 진입",
    },
    "bear": {
        "min_score_adj": +10,
        "max_daily_new_buys": 1,
        "position_mult_boost": 0.7,
        "description": "약세장 — 방어적",
    },
    "sideways": {
        "min_score_adj": +3,
        "max_daily_new_buys": 2,
        "position_mult_boost": 0.9,
        "description": "횡보장 — 선별적",
    },
    "neutral": {
        "min_score_adj": 0,
        "max_daily_new_buys": 3,
        "position_mult_boost": 1.0,
        "description": "중립 — 기본",
    },
}


class USMarketRegimeAdapter:
    """SPY/QQQ 기반 US 시장 체제 판단"""

    def __init__(self):
        self._regime: str = "neutral"
        self._last_update: Optional[datetime] = None
        self._data: Dict[str, float] = {}

    @property
    def regime(self) -> str:
        return self._regime

    @property
    def params(self) -> Dict[str, Any]:
        return REGIME_PARAMS.get(self._regime, REGIME_PARAMS["neutral"])

    def update_regime(self, spy_change_pct: float, qqq_change_pct: float,
                      spy_vs_open: float = 0.0, qqq_vs_open: float = 0.0):
        """
        SPY/QQQ 등락률 기반 체제 판단

        Args:
            spy_change_pct: SPY 전일비 등락률 (%)
            qqq_change_pct: QQQ 전일비 등락률 (%)
            spy_vs_open: SPY 시가 대비 변화 (%)
            qqq_vs_open: QQQ 시가 대비 변화 (%)
        """
        # SPY 60% + QQQ 40% 가중 평균 (US 시장은 SPY 중심)
        avg_change = spy_change_pct * 0.6 + qqq_change_pct * 0.4
        avg_vs_open = spy_vs_open * 0.6 + qqq_vs_open * 0.4

        self._data = {
            "spy_change": spy_change_pct,
            "qqq_change": qqq_change_pct,
            "avg_change": round(avg_change, 2),
            "avg_vs_open": round(avg_vs_open, 2),
        }

        prev = self._regime

        # US 시장은 KR보다 변동폭이 작음 → 임계값 하향
        # avg_change 기본 판단 + vs_open 보조 (vs_open 미전달 시에도 동작)
        if avg_change > 0.7:
            self._regime = "bull"
        elif avg_change < -0.7:
            self._regime = "bear"
        else:
            self._regime = "sideways"

        # vs_open 역방향이면 혼조 → sideways로 완화
        if avg_vs_open != 0.0:
            if self._regime == "bull" and avg_vs_open < -0.3:
                self._regime = "sideways"
            elif self._regime == "bear" and avg_vs_open > 0.3:
                self._regime = "sideways"

        self._last_update = datetime.now()

        if self._regime != prev:
            logger.info(
                f"[US 시장체제] {prev} → {self._regime} "
                f"(SPY {spy_change_pct:+.2f}%, QQQ {qqq_change_pct:+.2f}%)"
            )

    def get_summary(self) -> Dict[str, Any]:
        """대시보드용 요약"""
        return {
            "regime": self._regime,
            "params": self.params,
            "data": self._data,
            "last_update": self._last_update.isoformat() if self._last_update else None,
        }
