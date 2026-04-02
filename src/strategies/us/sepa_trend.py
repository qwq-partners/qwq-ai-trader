"""
QWQ AI Trader - US SEPA/Minervini Trend Template Strategy

Mark Minervini's Specific Entry Point Analysis.
Originally designed for US stocks - this is the "home" version.
Swing trading: hold 5-20 days.
Adapted from ai-trader-us/src/strategies/sepa.py (renamed to sepa_trend.py)
"""

from typing import Dict, Any, Optional
import pandas as pd

from ..base import USBaseStrategy
from ...core.types import Signal, Portfolio, Position, StrategyType, TimeHorizon
from ...indicators.technical import sma
from ...utils.sizing import atr_position_multiplier


class SEPATrendStrategy(USBaseStrategy):
    """SEPA Trend Template: Minervini criteria for US stocks"""

    name = "sepa"
    strategy_type = StrategyType.SEPA_TREND
    time_horizon = TimeHorizon.SWING

    def __init__(self, config: dict = None):
        super().__init__(config)
        self.min_rs_rating = self.config.get('min_rs_rating', 70)
        self.stop_loss_pct = self.config.get('stop_loss_pct', 5.0)
        self.take_profit_pct = self.config.get('take_profit_pct', 15.0)
        self.max_holding_days = self.config.get('max_holding_days', 20)

    def generate_signal(self, symbol: str, indicators: Dict[str, Any],
                        history: pd.DataFrame, portfolio: Portfolio) -> Optional[Signal]:
        close = indicators.get('close', 0)
        if close <= 0 or close < 5.0:
            return None

        ma50 = indicators.get('ma50')
        ma150 = indicators.get('ma150')
        ma200 = indicators.get('ma200')
        high_52w = indicators.get('high_52w')
        low_52w = indicators.get('low_52w')
        vol_ratio = indicators.get('vol_ratio', 1.0)
        rsi = indicators.get('rsi', 50)
        change_20d = indicators.get('change_20d', 0)

        if any(v is None or v <= 0 for v in [ma50, ma150, ma200, high_52w, low_52w]):
            return None

        # === SEPA Template Criteria ===
        sepa_pass = 0
        sepa_total = 6

        # 1. MA50 > MA150 > MA200 (trend alignment)
        if ma50 > ma150 > ma200:
            sepa_pass += 1

        # 2. Price > MA50 (above short-term trend)
        if close > ma50:
            sepa_pass += 1

        # 3. MA200 trending up (check 20-day change proxy)
        if len(history) >= 220:
            ma200_20ago = float(history['close'].iloc[-220:-200].mean())
            if ma200 > ma200_20ago:
                sepa_pass += 1
        else:
            sepa_pass += 1  # 데이터 부족 시 관대하게 통과 처리

        # 4. 52-week low +30% (strong uptrend)
        pct_from_low = (close - low_52w) / low_52w * 100 if low_52w > 0 else 0
        if pct_from_low >= 30:
            sepa_pass += 1

        # 5. Within 25% of 52-week high (near new highs)
        pct_from_high = (close - high_52w) / high_52w * 100 if high_52w > 0 else -100
        if pct_from_high >= -25:
            sepa_pass += 1

        # 6. MA5 > MA20 (short-term momentum)
        ma5 = indicators.get('ma5')
        ma20 = indicators.get('ma20')
        if ma5 is not None and ma20 is not None and ma5 > ma20:
            sepa_pass += 1

        # Must pass at least 5/6 SEPA criteria
        if sepa_pass < 5:
            return None

        # --- Score (0-100) ---
        score = 0

        # SEPA template (25 pts)
        score += (sepa_pass / sepa_total) * 25

        # MA spread quality (15 pts)
        if ma200 > 0:
            ma_spread = (ma50 - ma200) / ma200 * 100
            score += min(15, ma_spread * 1.5)

        # 52w high proximity (15 pts)
        if pct_from_high > -5:
            score += 15
        elif pct_from_high > -10:
            score += 10
        elif pct_from_high > -25:
            score += 5

        # 20-day momentum (15 pts)
        score += min(15, max(0, change_20d * 1.5))

        # Volume (10 pts)
        score += min(10, vol_ratio * 3)

        # RSI sweet spot 50-70 (10 pts)
        if 50 <= rsi <= 70:
            score += 10
        elif 40 <= rsi <= 80:
            score += 5

        # Trend strength (10 pts)
        if close > ma50 > ma150 > ma200:
            score += 10
        elif close > ma50 > ma150:
            score += 5

        # RS Ranking 보너스 (최대 +10)
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

        stop = close * (1 - self.stop_loss_pct / 100)
        target = close * (1 + self.take_profit_pct / 100)

        # R/R 비율 필터
        min_rr = self.config.get('min_rr_ratio', 2.0)
        if not self.check_rr_ratio(close, target, stop, min_rr):
            return None

        reason = (f"SEPA {sepa_pass}/{sepa_total} | "
                  f"52w high {pct_from_high:+.1f}% | "
                  f"vol {vol_ratio:.1f}x")

        # ATR 데이터 품질 가드 + 포지션 사이징
        atr_pct = indicators.get('atr_pct', 0)
        if atr_pct is None or atr_pct <= 0:
            logger.debug(f"[US SEPA] {symbol} ATR 누락/0 차단")
            return None
        _pos_mult = atr_position_multiplier(atr_pct)
        if score >= 85:
            _pos_mult = max(_pos_mult * 1.3, 0.75)

        return self._create_signal(
            symbol=symbol, score=score, reason=reason,
            price=close, stop_price=stop, target_price=target,
            metadata={'atr_pct': atr_pct, 'position_multiplier': _pos_mult},
        )

    def check_exit(self, symbol: str, history: pd.DataFrame,
                   position: Position) -> Optional[str]:
        """Exit if price falls below MA50"""
        if len(history) < 50:
            return None

        ma50_series = sma(history['close'], 50)
        if ma50_series.empty:
            return None

        current = float(history['close'].iloc[-1])
        ma50_val = float(ma50_series.iloc[-1])

        if current < ma50_val:
            return "below_ma50"

        return None
