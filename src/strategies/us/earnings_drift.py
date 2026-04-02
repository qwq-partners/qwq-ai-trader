"""
QWQ AI Trader - US Post-Earnings Announcement Drift (PEAD) Strategy

Trades the tendency for stocks to continue drifting after earnings surprises.
US market unique strategy - earnings seasons are major catalysts.
Swing trading: hold up to 20 days.
Adapted from ai-trader-us/src/strategies/earnings_drift.py
"""

from typing import Dict, Any, Optional
import pandas as pd

from ..base import USBaseStrategy
from ...core.types import Signal, Portfolio, StrategyType, TimeHorizon
from ...utils.sizing import atr_position_multiplier


class EarningsDriftStrategy(USBaseStrategy):
    """Post-Earnings Drift: buy strong earnings gap that holds"""

    name = "earnings_drift"
    strategy_type = StrategyType.EARNINGS_DRIFT
    time_horizon = TimeHorizon.SWING

    def __init__(self, config: dict = None):
        super().__init__(config)
        self.min_gap_pct = self.config.get('min_gap_pct', 5.0)
        self.min_eps_surprise_pct = self.config.get('min_eps_surprise_pct', 10)
        self.min_revenue_growth_pct = self.config.get('min_revenue_growth_pct', 15)
        self.stop_loss_pct = self.config.get('stop_loss_pct', 8.0)
        self.max_holding_days = self.config.get('max_holding_days', 20)

    def generate_signal(self, symbol: str, indicators: Dict[str, Any],
                        history: pd.DataFrame, portfolio: Portfolio) -> Optional[Signal]:
        """
        Daily backtesting approximation:
        Detect earnings gap-up patterns from price data.

        A large gap-up (+5%+) with very high volume suggests earnings reaction.
        The strategy confirms the gap held (close near high) before entering.
        """
        close = indicators.get('close', 0)
        if close <= 0 or close < 5.0 or len(history) < 5:
            return None

        vol_ratio = indicators.get('vol_ratio', 0)
        rsi = indicators.get('rsi', 50)
        ma20 = indicators.get('ma20', 0)
        ma50 = indicators.get('ma50', 0)

        # Look at today's bar
        today = history.iloc[-1]
        yesterday = history.iloc[-2]

        today_open = float(today['open'])
        today_close = float(today['close'])
        today_high = float(today['high'])
        today_low = float(today['low'])
        today_volume = int(today['volume'])
        prev_close = float(yesterday['close'])

        if prev_close <= 0 or today_open <= 0:
            return None

        # --- Gap Detection ---
        gap_pct = (today_open - prev_close) / prev_close * 100

        # Must be a significant gap up
        if gap_pct < self.min_gap_pct:
            return None

        # --- Volume Surge (earnings days typically 3x+) ---
        if vol_ratio < 2.5:
            return None

        # --- Gap Hold Confirmation ---
        # Close should be above open (didn't sell off)
        if today_close < today_open:
            return None

        # Close should be in upper 50% of day's range
        if today_high > today_low:
            close_position = (today_close - today_low) / (today_high - today_low)
            if close_position < 0.5:
                return None
        else:
            close_position = 0.5

        # --- Score (0-100) ---
        score = 0

        # Gap size (25 pts) - proxy for earnings surprise
        score += min(25, gap_pct * 2.5)

        # Volume surge (20 pts) - institutional participation
        score += min(20, vol_ratio * 3)

        # Gap held (20 pts) - close near high
        score += close_position * 20

        # Day gain beyond gap (15 pts)
        intraday_gain = (today_close - today_open) / today_open * 100
        score += min(15, max(0, intraday_gain * 5))

        # Trend context (10 pts)
        if ma20 > 0 and close > ma20:
            score += 5
        if ma50 > 0 and close > ma50:
            score += 5

        # Pre-earnings trend (10 pts)
        change_5d_pre = 0
        if len(history) >= 7:
            pre_close = float(history['close'].iloc[-6])
            if pre_close > 0:
                change_5d_pre = (prev_close - pre_close) / pre_close * 100

        if change_5d_pre > 0:
            score += min(10, change_5d_pre * 2)

        score = max(0, min(100, score))

        if score < self.min_score:
            return None

        # Stop at earnings day low (or stop_loss_pct, whichever is tighter)
        earnings_day_stop = today_low * 0.995  # Slight buffer below day low
        pct_stop = close * (1 - self.stop_loss_pct / 100)
        stop = max(earnings_day_stop, pct_stop)

        # Target: 20-day trend following (use 15% as default target)
        target = close * 1.15

        # R/R 비율 필터
        min_rr = self.config.get('min_rr_ratio', 2.0)
        if not self.check_rr_ratio(close, target, stop, min_rr):
            return None

        reason = (f"Earnings gap +{gap_pct:.1f}% | vol {vol_ratio:.1f}x | "
                  f"held {close_position:.0%}")

        # ATR 포지션 사이징 (어닝 드리프트는 고변동 허용 → 가드만)
        atr_pct = indicators.get('atr_pct', 0)
        _pos_mult = atr_position_multiplier(atr_pct) if atr_pct is not None and atr_pct > 0 else 0.8

        return self._create_signal(
            symbol=symbol, score=score, reason=reason,
            price=close, stop_price=stop, target_price=target,
            metadata={'gap_pct': gap_pct, 'vol_ratio': vol_ratio,
                      'atr_pct': atr_pct, 'position_multiplier': _pos_mult},
        )
