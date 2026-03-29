"""
QWQ AI Trader - 시장 체제 사전 적응

시장 체제(bull/bear/sideways)를 판단하고,
전략 파라미터를 사전에 조정합니다.

기존 스마트 사이드카(사후 방어)와 상호 보완:
- MarketRegimeAdapter: 장 시작 시 사전 조정 (공격/방어 모드)
- SmartSidecar: 장중 손실 발생 시 사후 차단 (안전망)
"""

from datetime import datetime
from typing import Dict, Optional
from loguru import logger


class MarketRegimeAdapter:
    """시장 체제별 전략 파라미터 동적 조정"""

    # 체제별 파라미터 기본값
    REGIME_PARAMS = {
        "bull": {
            "sepa_min_score_adj": -5,       # 기본 min_score에서 완화
            "theme_max_change_adj": 2.0,    # 기본 max_change에 추가
            "max_daily_new_buys": 5,
            "position_mult_boost": 1.1,
            "description": "강세장: 적극적 진입",
        },
        "bear": {
            "sepa_min_score_adj": +10,      # 기본 min_score에서 강화
            "theme_max_change_adj": -2.0,   # 기본 max_change에서 축소
            "max_daily_new_buys": 2,
            "position_mult_boost": 0.7,
            "description": "약세장: 보수적 진입",
        },
        "sideways": {
            "sepa_min_score_adj": +3,
            "theme_max_change_adj": 0.0,
            "max_daily_new_buys": 3,
            "position_mult_boost": 0.9,
            "description": "횡보장: 선별적 진입",
        },
        "neutral": {
            "sepa_min_score_adj": 0,
            "theme_max_change_adj": 0.0,
            "max_daily_new_buys": 4,
            "position_mult_boost": 1.0,
            "description": "중립: 기본 기준",
        },
    }

    def __init__(self):
        self._current_regime: str = "neutral"
        self._regime_data: Dict = {}
        self._last_update: Optional[datetime] = None

    def update_regime(self, kospi_data: dict, kosdaq_data: dict):
        """
        시장 체제 판단 (KOSPI/KOSDAQ OHLCV 기반)

        판단 기준:
        - bull:     평균 등락률 > +1% AND 시가대비 상승
        - bear:     평균 등락률 < -1% AND 시가대비 하락
        - sideways: 그 외
        """
        kospi_change = kospi_data.get("change_pct", 0)
        kosdaq_change = kosdaq_data.get("change_pct", 0)
        avg_change = (kospi_change + kosdaq_change) / 2

        kospi_vs_open = 0
        if kospi_data.get("open", 0) > 0 and kospi_data.get("price", 0) > 0:
            kospi_vs_open = (kospi_data["price"] - kospi_data["open"]) / kospi_data["open"] * 100
        kosdaq_vs_open = 0
        if kosdaq_data.get("open", 0) > 0 and kosdaq_data.get("price", 0) > 0:
            kosdaq_vs_open = (kosdaq_data["price"] - kosdaq_data["open"]) / kosdaq_data["open"] * 100
        avg_vs_open = (kospi_vs_open + kosdaq_vs_open) / 2

        prev_regime = self._current_regime

        if avg_change > 1.0 and avg_vs_open > 0.3:
            self._current_regime = "bull"
        elif avg_change < -1.0 and avg_vs_open < -0.3:
            self._current_regime = "bear"
        elif abs(avg_change) <= 1.0:
            self._current_regime = "sideways"
        else:
            # 혼조: 이전 상태 유지 (과도한 전환 방지)
            pass

        self._regime_data = {
            "kospi_change": kospi_change,
            "kosdaq_change": kosdaq_change,
            "avg_change": avg_change,
            "avg_vs_open": avg_vs_open,
        }
        self._last_update = datetime.now()

        if prev_regime != self._current_regime:
            params = self.REGIME_PARAMS[self._current_regime]
            logger.info(
                f"[시장체제] {prev_regime} → {self._current_regime}: "
                f"{params['description']} "
                f"(전일비 {avg_change:+.1f}%, 시가비 {avg_vs_open:+.1f}%)"
            )

    @property
    def regime(self) -> str:
        return self._current_regime

    @property
    def params(self) -> Dict:
        return self.REGIME_PARAMS.get(self._current_regime, self.REGIME_PARAMS["neutral"])

    def get_adjusted_min_score(self, base_min_score: float) -> float:
        """체제 반영 min_score"""
        adj = self.params.get("sepa_min_score_adj", 0)
        return base_min_score + adj

    def get_position_boost(self) -> float:
        """체제 반영 포지션 배율"""
        return self.params.get("position_mult_boost", 1.0)

    def get_summary(self) -> Dict:
        """현재 체제 요약"""
        return {
            "regime": self._current_regime,
            "params": self.params,
            "data": self._regime_data,
            "last_update": self._last_update.isoformat() if self._last_update else None,
        }
