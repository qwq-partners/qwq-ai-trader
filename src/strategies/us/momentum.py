"""
QWQ AI Trader - US Momentum Breakout Strategy

20-day high breakout with volume confirmation.
Adapted from ai-trader-us/src/strategies/momentum.py for US markets.
"""

from typing import Dict, Any, Optional
import pandas as pd
from loguru import logger

from ..base import USBaseStrategy
from ...core.types import Signal, Portfolio, StrategyType, TimeHorizon


class MomentumBreakoutStrategy(USBaseStrategy):
    """US Momentum Breakout: 20-day high breakout + volume surge"""

    name = "momentum"
    strategy_type = StrategyType.MOMENTUM_BREAKOUT
    time_horizon = TimeHorizon.SWING

    def __init__(self, config: dict = None):
        super().__init__(config)
        self.breakout_period = self.config.get('breakout_period', 20)
        self.min_breakout_pct = self.config.get('min_breakout_pct', 0.8)
        self.volume_surge_ratio = self.config.get('volume_surge_ratio', 2.0)
        self.stop_loss_pct = self.config.get('stop_loss_pct', 4.0)
        self.take_profit_pct = self.config.get('take_profit_pct', 10.0)

    def generate_signal(self, symbol: str, indicators: Dict[str, Any],
                        history: pd.DataFrame, portfolio: Portfolio) -> Optional[Signal]:
        close = indicators.get('close', 0)
        if close <= 0 or close < 5.0:
            return None

        # Use PREVIOUS day's 20-day high for breakout detection
        prev_high_20d = indicators.get('prev_high_20d', 0)
        vol_ratio = indicators.get('vol_ratio', 0)
        rsi = indicators.get('rsi', 50)
        ma5 = indicators.get('ma5', 0)
        ma20 = indicators.get('ma20', 0)
        ma50 = indicators.get('ma50', 0)
        vwap = indicators.get('vwap', 0)
        atr_pct = indicators.get('atr_pct', 0)
        change_1d = indicators.get('change_1d', 0)
        change_5d = indicators.get('change_5d', 0)
        change_20d = indicators.get('change_20d', 0)

        # --- Filters ---
        # Must be above PREVIOUS 20-day high by min breakout %
        if prev_high_20d <= 0:
            return None
        breakout_pct = (close - prev_high_20d) / prev_high_20d * 100
        if breakout_pct < self.min_breakout_pct:
            return None

        # Volume confirmation
        if vol_ratio < self.volume_surge_ratio:
            return None

        # Overheat filter
        if rsi > 80:
            return None

        # Extreme volatility filter (>8% daily ATR)
        if atr_pct > 8:
            return None

        # MA trend filter
        if ma5 <= 0 or ma20 <= 0:
            return None
        if close < ma20:
            return None

        # --- Score Calculation (0-100) ---
        score = 0

        # Price momentum (30 pts) - positive changes only
        score += min(12, max(0, change_1d) * 2.4)
        score += min(10, max(0, change_5d) * 1.4)
        score += min(8, max(0, change_20d) * 0.8)

        # Volume momentum (20 pts)
        score += min(20, vol_ratio * 4)

        # Proximity to 52w high (20 pts)
        pct_from_high = indicators.get('pct_from_52w_high', -50)
        if pct_from_high is not None:
            if pct_from_high > -5:
                score += 20
            elif pct_from_high > -10:
                score += 15
            elif pct_from_high > -20:
                score += 10

        # Trend quality (20 pts)
        if ma5 > ma20 and ma20 > ma50 and ma50 > 0:
            score += 12
        elif ma5 > ma20:
            score += 6
        if vwap > 0 and close > vwap:
            score += 4
        if close > ma20:
            score += 4

        # RSI penalty (up to -15 pts)
        if rsi > 75:
            score -= 15
        elif rsi > 70:
            score -= 8

        # ORB 확인 보너스: 전일 고가 돌파 + 당일 시가 > 전일 종가 (갭업 ORB)
        if len(history) >= 2:
            prev_high = float(history['high'].iloc[-2])
            prev_close_val = float(history['close'].iloc[-2])
            today_open = float(history['open'].iloc[-1]) if 'open' in history else close
            if today_open > prev_close_val and close > prev_high:
                score += 5  # ORB 돌파 보너스

        # RS Ranking 보너스/감점 (최대 +10, 감점 -5) — min_score 체크 전 적용
        rs_val = indicators.get('rs_rating')
        if rs_val is not None:
            if rs_val >= 80:
                score += 10
            elif rs_val >= 70:
                score += 5
            elif rs_val < 30:
                score -= 5

        score = max(0, min(100, score))

        if score < self.min_score:
            return None

        # Stop / Target
        stop = close * (1 - self.stop_loss_pct / 100)
        target = close * (1 + self.take_profit_pct / 100)

        # R/R 비율 필터
        min_rr = self.config.get('min_rr_ratio', 2.0)
        if not self.check_rr_ratio(close, target, stop, min_rr):
            return None

        reason = (f"20d breakout +{breakout_pct:.1f}% | "
                  f"vol {vol_ratio:.1f}x | RSI {rsi:.0f} | "
                  f"1d {change_1d:+.1f}%")

        return self._create_signal(
            symbol=symbol, score=score, reason=reason,
            price=close, stop_price=stop, target_price=target,
        )
