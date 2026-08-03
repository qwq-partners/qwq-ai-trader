"""
QWQ AI Trader - US Earnings Announcement Reversal Strategy (2026-08-03)

논문: Reversal During Earnings-Announcements (재현 Sharpe 0.785, 변동성 25.7%)
발표 직전 낙폭과대 종목이 발표를 계기로 반등하는 경향을 포착한다.
EarningsDrift(발표 후 갭업 추종)의 거울 전략 — 같은 어닝 캘린더 인프라 사용.

동작 조건:
  - 스케줄러가 eng._earnings_upcoming(오늘~+2일 발표 예정)에 있는 종목만 평가시킨다.
    캘린더가 비어 있으면 전략은 발화하지 않는다 (fail-closed — 발표일을 모르면
    "발표 전 매수"라는 전제 자체가 성립하지 않는다).
  - 발표를 보유한 채 넘기는 갭 리스크가 있으므로 position_multiplier 0.5 고정 축소.

quick_backtest.py --idea earnings_reversal 검증을 통과하기 전까지 config
enabled: false 유지 (2026-08-03 기준 finnhub 과거 캘린더 제약으로 표본 확보 중).
"""

from typing import Dict, Any, Optional
import pandas as pd

from ..base import USBaseStrategy
from ...core.types import Signal, Portfolio, StrategyType, TimeHorizon
from ...utils.sizing import atr_position_multiplier


class EarningsReversalStrategy(USBaseStrategy):
    """발표 전 낙폭과대 매수 → 발표 후 단기 반등 청산"""

    name = "earnings_reversal"
    # 전용 타입 필수 — EARNINGS_DRIFT 재사용 시 재시작 복원(strategy_type.value 매칭)과
    # 전략별 청산 설정 조회가 드리프트(max_holding 20일)로 오귀속된다
    strategy_type = StrategyType.EARNINGS_REVERSAL
    time_horizon = TimeHorizon.SWING

    # 발표 관통 갭 리스크 — 사이징 절반 고정
    RISK_SIZE_MULT = 0.5

    def __init__(self, config: dict = None):
        super().__init__(config)
        self.min_pre_drop_pct = self.config.get('min_pre_drop_pct', 5.0)
        self.max_day_drop_pct = self.config.get('max_day_drop_pct', 8.0)
        self.stop_loss_pct = self.config.get('stop_loss_pct', 5.0)
        self.take_profit_pct = self.config.get('take_profit_pct', 6.0)
        self.max_holding_days = self.config.get('max_holding_days', 3)

    def generate_signal(self, symbol: str, indicators: Dict[str, Any],
                        history: pd.DataFrame, portfolio: Portfolio) -> Optional[Signal]:
        close = indicators.get('close', 0)
        if close <= 0 or close < 5.0 or len(history) < 30:
            return None

        closes = history['close'].astype(float)
        prev5 = float(closes.iloc[-6])
        if prev5 <= 0:
            return None

        # ── 발표 전 낙폭 (5거래일) ──
        pre_drop = (float(closes.iloc[-1]) / prev5 - 1) * 100
        if pre_drop > -self.min_pre_drop_pct:
            return None

        # ── 떨어지는 칼 회피: 당일 폭락 중이면 진입 금지 ──
        day_ret = 0.0
        prev_close = float(closes.iloc[-2])
        if prev_close > 0:
            day_ret = (float(closes.iloc[-1]) / prev_close - 1) * 100
        if day_ret <= -self.max_day_drop_pct:
            return None

        rsi = indicators.get('rsi')
        ma200 = indicators.get('ma200', 0)

        # ── Score (0-100) ──
        score = 55.0
        # 낙폭 깊이 (최대 20): -5%에서 0점, -12% 이상에서 만점
        score += min(20.0, max(0.0, (-pre_drop - self.min_pre_drop_pct) * (20.0 / 7.0)))
        # RSI 과매도 (최대 15)
        if rsi is not None and rsi < 40:
            score += min(15.0, (40 - rsi) * 0.75)
        # 장기 추세 위 (10): 구조적 하락이 아닌 일시 낙폭일 가능성
        if ma200 is not None and ma200 > 0 and close > ma200:
            score += 10.0
        score = max(0.0, min(100.0, score))

        if score < self.min_score:
            return None

        # ── 손절/목표 ──
        recent_low = float(history['low'].astype(float).iloc[-5:].min())
        pct_stop = close * (1 - self.stop_loss_pct / 100)
        stop = max(recent_low * 0.99, pct_stop)
        target = close * (1 + self.take_profit_pct / 100)

        min_rr = self.config.get('min_rr_ratio', 1.2)
        if not self.check_rr_ratio(close, target, stop, min_rr):
            return None

        # ── 사이징: 발표 관통 리스크 절반 + ATR ──
        atr_pct = indicators.get('atr_pct')
        if atr_pct is not None and atr_pct > 0:
            _pos_mult = atr_position_multiplier(atr_pct) * self.RISK_SIZE_MULT
        else:
            _pos_mult = 0.5 * self.RISK_SIZE_MULT

        reason = (f"Earnings reversal pre5d {pre_drop:+.1f}% | "
                  f"RSI {rsi:.0f}" if rsi is not None else
                  f"Earnings reversal pre5d {pre_drop:+.1f}%")

        return self._create_signal(
            symbol=symbol, score=score, reason=reason,
            price=close, stop_price=stop, target_price=target,
            metadata={'pre_drop_pct': pre_drop, 'atr_pct': atr_pct,
                      'position_multiplier': _pos_mult},
        )
