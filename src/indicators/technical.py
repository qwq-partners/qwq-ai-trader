"""
QWQ AI Trader - 기술적 지표 모듈 (통합)

KR: FDR(FinanceDataReader) 일봉 기반 벡터 연산 (TechnicalIndicators 클래스)
US: numpy/pandas 기반 함수형 API (sma, ema, rsi, atr 등)

두 시장 모두 동일 모듈에서 사용할 수 있습니다.

지표 목록:
  MA: 5, 10, 20, 50, 150, 200
  RSI: 2, 14 (Wilder's Smoothing)
  BB: upper, mid, lower (20일, 2 sigma)
  MACD: line, signal, histogram
  ATR: 14일 (%)
  VWAP: volume-weighted average price
  Change: 1d, 5d, 20d, 60d
  52w: high, low
  SEPA: Minervini 트렌드 템플릿 체크
  MRS: Mansfield Relative Strength
"""

from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
from loguru import logger


# ============================================================
# US 함수형 API (pandas/numpy 기반)
# ============================================================

def sma(series: pd.Series, period: int) -> pd.Series:
    """Simple Moving Average"""
    return series.rolling(window=period, min_periods=period).mean()


def ema(series: pd.Series, period: int) -> pd.Series:
    """Exponential Moving Average"""
    return series.ewm(span=period, adjust=False).mean()


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    """RSI (Wilder's smoothing)"""
    delta = series.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = (-delta).where(delta < 0, 0.0)

    avg_gain = gain.ewm(alpha=1/period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1/period, min_periods=period, adjust=False).mean()

    rs = avg_gain / avg_loss.replace(0, np.inf)
    return 100 - (100 / (1 + rs))


def atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    """Average True Range"""
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs()
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1/period, min_periods=period, adjust=False).mean()


def vwap(high: pd.Series, low: pd.Series, close: pd.Series, volume: pd.Series,
         period: int = None) -> pd.Series:
    """Volume Weighted Average Price"""
    tp = (high + low + close) / 3
    if period:
        cum_tp_vol = (tp * volume).rolling(period).sum()
        cum_vol = volume.rolling(period).sum()
    else:
        cum_tp_vol = (tp * volume).cumsum()
        cum_vol = volume.cumsum()
    return cum_tp_vol / cum_vol.replace(0, np.nan)


def bollinger_bands(series: pd.Series, period: int = 20, std_dev: float = 2.0):
    """Bollinger Bands -> (upper, middle, lower)"""
    middle = sma(series, period)
    std = series.rolling(window=period).std()
    upper = middle + std_dev * std
    lower = middle - std_dev * std
    return upper, middle, lower


def macd(series: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9):
    """MACD -> (macd_line, signal_line, histogram)"""
    fast_ema = ema(series, fast)
    slow_ema = ema(series, slow)
    macd_line = fast_ema - slow_ema
    signal_line = ema(macd_line, signal)
    histogram = macd_line - signal_line
    return macd_line, signal_line, histogram


def volume_ratio(volume: pd.Series, period: int = 20) -> pd.Series:
    """Volume relative to N-day average"""
    avg_vol = sma(volume, period)
    return volume / avg_vol.replace(0, np.nan)


def high_low_range(high: pd.Series, low: pd.Series, period: int = 20):
    """N-day high and low"""
    period_high = high.rolling(window=period).max()
    period_low = low.rolling(window=period).min()
    return period_high, period_low


def rs_rating(close: pd.Series, benchmark_close: pd.Series, period: int = 252) -> pd.Series:
    """Relative Strength vs benchmark (0-100 percentile rank)"""
    stock_return = close.pct_change(period)
    bench_return = benchmark_close.pct_change(period)
    relative = stock_return - bench_return
    return relative.rank(pct=True) * 100


def compute_indicators(df: pd.DataFrame) -> Dict[str, Any]:
    """Compute all standard indicators for a DataFrame with OHLCV columns.

    Returns dict of indicator values (latest bar).
    Used by US strategies.
    """
    if len(df) < 20:
        return {}

    c = df['close']
    h = df['high']
    l = df['low']
    v = df['volume']

    indicators = {}

    # Moving averages
    for p in [5, 10, 20, 50, 150, 200]:
        ma = sma(c, p)
        if not ma.empty and not pd.isna(ma.iloc[-1]):
            indicators[f'ma{p}'] = float(ma.iloc[-1])

    # RSI
    rsi_val = rsi(c, 14)
    if not rsi_val.empty and not pd.isna(rsi_val.iloc[-1]):
        indicators['rsi'] = float(rsi_val.iloc[-1])

    # RSI-2 (for mean reversion)
    rsi2_val = rsi(c, 2)
    if not rsi2_val.empty and not pd.isna(rsi2_val.iloc[-1]):
        indicators['rsi2'] = float(rsi2_val.iloc[-1])

    # ATR
    atr_val = atr(h, l, c, 14)
    if not atr_val.empty and not pd.isna(atr_val.iloc[-1]):
        indicators['atr'] = float(atr_val.iloc[-1])
        if c.iloc[-1] > 0:
            indicators['atr_pct'] = float(atr_val.iloc[-1] / c.iloc[-1] * 100)

    # VWAP (20-bar)
    vwap_val = vwap(h, l, c, v, period=20)
    if not vwap_val.empty and not pd.isna(vwap_val.iloc[-1]):
        indicators['vwap'] = float(vwap_val.iloc[-1])

    # Volume ratio
    vr = volume_ratio(v, 20)
    if not vr.empty and not pd.isna(vr.iloc[-1]):
        indicators['vol_ratio'] = float(vr.iloc[-1])

    # Highs/Lows
    h20, l20 = high_low_range(h, l, 20)
    if not pd.isna(h20.iloc[-1]):
        indicators['high_20d'] = float(h20.iloc[-1])
        indicators['low_20d'] = float(l20.iloc[-1])

    # Previous day's 20-day high/low (for breakout detection)
    if len(h20) >= 2 and not pd.isna(h20.iloc[-2]):
        indicators['prev_high_20d'] = float(h20.iloc[-2])
        indicators['prev_low_20d'] = float(l20.iloc[-2])

    if len(df) >= 252:
        h52, l52 = high_low_range(h, l, 252)
        if not pd.isna(h52.iloc[-1]):
            indicators['high_52w'] = float(h52.iloc[-1])
            indicators['low_52w'] = float(l52.iloc[-1])
            if h52.iloc[-1] > 0:
                indicators['pct_from_52w_high'] = float(
                    (c.iloc[-1] - h52.iloc[-1]) / h52.iloc[-1] * 100
                )
            if l52.iloc[-1] > 0:
                indicators['pct_from_52w_low'] = float(
                    (c.iloc[-1] - l52.iloc[-1]) / l52.iloc[-1] * 100
                )

    # Price momentum
    for days in [1, 5, 20]:
        if len(df) > days:
            change = float((c.iloc[-1] - c.iloc[-1 - days]) / c.iloc[-1 - days] * 100)
            indicators[f'change_{days}d'] = change

    # Current price
    indicators['close'] = float(c.iloc[-1])
    indicators['volume'] = int(v.iloc[-1])

    return indicators


def compute_indicators_all(df: pd.DataFrame) -> pd.DataFrame:
    """Pre-compute all indicators for every row in the DataFrame.

    Much faster than calling compute_indicators() per-row.
    Used by BacktestEngine for bulk pre-computation.

    Returns DataFrame with indicator columns, same index as input.
    """
    if len(df) < 20:
        return pd.DataFrame(index=df.index)

    c = df['close'].astype(float)
    h = df['high'].astype(float)
    l = df['low'].astype(float)
    v = df['volume'].astype(float)

    result = pd.DataFrame(index=df.index)
    result['close'] = c
    result['volume'] = v

    # Moving averages
    for p in [5, 10, 20, 50, 150, 200]:
        result[f'ma{p}'] = sma(c, p)

    # RSI
    result['rsi'] = rsi(c, 14)
    result['rsi2'] = rsi(c, 2)

    # ATR
    atr_series = atr(h, l, c, 14)
    result['atr'] = atr_series
    result['atr_pct'] = atr_series / c * 100

    # VWAP (20-bar)
    result['vwap'] = vwap(h, l, c, v, period=20)

    # Volume ratio
    result['vol_ratio'] = volume_ratio(v, 20)

    # 20-day highs/lows
    h20, l20 = high_low_range(h, l, 20)
    result['high_20d'] = h20
    result['low_20d'] = l20
    result['prev_high_20d'] = h20.shift(1)
    result['prev_low_20d'] = l20.shift(1)

    # 52-week highs/lows
    h52, l52 = high_low_range(h, l, 252)
    result['high_52w'] = h52
    result['low_52w'] = l52
    result['pct_from_52w_high'] = (c - h52) / h52 * 100
    result['pct_from_52w_low'] = (c - l52) / l52 * 100

    # Price momentum
    for days in [1, 5, 20]:
        result[f'change_{days}d'] = c.pct_change(days) * 100

    return result


# ============================================================
# KR 클래스 기반 API (일봉 데이터 리스트 기반)
# ============================================================

class TechnicalIndicators:
    """
    일봉 기반 기술적 지표 계산기

    캐시 TTL 24시간: 장 마감 후 1회 계산, 익일 장중 재사용.
    """

    def __init__(self):
        self._cache: Dict[str, Dict[str, Any]] = {}
        self._cache_ts: Dict[str, datetime] = {}
        self.CACHE_TTL = 86400  # 24시간

    def calculate_all(self, symbol: str, daily_data: List[Dict]) -> Dict[str, Any]:
        """
        일봉 데이터 -> 전체 지표 계산

        Args:
            symbol: 종목코드
            daily_data: [{"date","open","high","low","close","volume"}, ...]
                        날짜 오름차순 (오래된 것 먼저)

        Returns:
            지표 딕셔너리
        """
        # 캐시 체크
        now = datetime.now()
        if symbol in self._cache_ts:
            elapsed = (now - self._cache_ts[symbol]).total_seconds()
            if elapsed < self.CACHE_TTL and symbol in self._cache:
                return self._cache[symbol]

        if not daily_data or len(daily_data) < 5:
            return {}

        closes = [float(d["close"]) for d in daily_data]
        highs = [float(d["high"]) for d in daily_data]
        lows = [float(d["low"]) for d in daily_data]
        volumes = [int(d.get("volume", 0)) for d in daily_data]

        indicators: Dict[str, Any] = {}

        # 이동평균
        for period in [5, 20, 50, 150, 200]:
            key = f"ma{period}"
            indicators[key] = self._sma(closes, period)

        # RSI
        indicators["rsi_2"] = self._rsi(closes, 2)
        indicators["rsi_14"] = self._rsi(closes, 14)

        # 볼린저밴드
        bb = self._bollinger(closes, 20, 2.0)
        if bb:
            indicators["bb_upper"], indicators["bb_mid"], indicators["bb_lower"] = bb

        # MACD
        macd_result = self._macd(closes, 12, 26, 9)
        if macd_result:
            indicators["macd"], indicators["macd_signal"], indicators["macd_hist"] = macd_result

        # ATR (% 및 절대값)
        atr_result = self._atr(highs, lows, closes, 14)
        if atr_result:
            indicators["atr_14"] = atr_result[0]       # % 단위
            indicators["atr_14_abs"] = atr_result[1]    # 원/달러 단위 절대값
        else:
            indicators["atr_14"] = None
            indicators["atr_14_abs"] = None

        # 변화율
        if len(closes) >= 6:
            indicators["change_5d"] = (closes[-1] - closes[-6]) / closes[-6] * 100 if closes[-6] > 0 else 0
        if len(closes) >= 21:
            indicators["change_20d"] = (closes[-1] - closes[-21]) / closes[-21] * 100 if closes[-21] > 0 else 0
        if len(closes) >= 61:
            indicators["change_60d"] = (closes[-1] - closes[-61]) / closes[-61] * 100 if closes[-61] > 0 else 0

        # 52주 고저 (약 250거래일)
        lookback_52w = min(len(highs), 250)
        indicators["high_52w"] = max(highs[-lookback_52w:])
        indicators["low_52w"] = min(lows[-lookback_52w:])

        # 현재가
        indicators["close"] = closes[-1]
        indicators["volume"] = volumes[-1] if volumes else 0

        # 거래량 평균 (20일)
        if len(volumes) >= 20:
            indicators["vol_ma20"] = sum(volumes[-20:]) / 20
            indicators["vol_ratio"] = volumes[-1] / indicators["vol_ma20"] if indicators["vol_ma20"] > 0 else 0

        # MA5 > MA20 정렬 플래그
        indicators["ma5_above_ma20"] = bool(
            indicators.get("ma5") and indicators.get("ma20")
            and indicators["ma5"] > indicators["ma20"]
        )

        # SEPA 체크
        sepa_pass, sepa_reasons = self.check_sepa(indicators)
        indicators["sepa_pass"] = sepa_pass
        indicators["sepa_reasons"] = sepa_reasons

        # 캐시 저장
        self._cache[symbol] = indicators
        self._cache_ts[symbol] = now

        return indicators

    # --- 핵심 지표 ---

    @staticmethod
    def _sma(closes: List[float], period: int) -> Optional[float]:
        """단순이동평균"""
        if len(closes) < period:
            return None
        return sum(closes[-period:]) / period

    @staticmethod
    def _rsi(closes: List[float], period: int) -> Optional[float]:
        """RSI (Wilder's Smoothing)"""
        if len(closes) < period + 1:
            return None

        changes = [closes[i] - closes[i - 1] for i in range(1, len(closes))]

        # 첫 번째 평균 (SMA)
        gains = [max(c, 0) for c in changes[:period]]
        losses = [max(-c, 0) for c in changes[:period]]

        avg_gain = sum(gains) / period
        avg_loss = sum(losses) / period

        # Wilder's Smoothing
        for c in changes[period:]:
            gain = max(c, 0)
            loss = max(-c, 0)
            avg_gain = (avg_gain * (period - 1) + gain) / period
            avg_loss = (avg_loss * (period - 1) + loss) / period

        if avg_gain == 0 and avg_loss == 0:
            return 50.0
        if avg_loss == 0:
            return 100.0

        rs = avg_gain / avg_loss
        return 100 - (100 / (1 + rs))

    @staticmethod
    def _bollinger(closes: List[float], period: int = 20, std: float = 2.0) -> Optional[Tuple[float, float, float]]:
        """볼린저밴드 -> (upper, mid, lower)"""
        if len(closes) < period:
            return None

        data = closes[-period:]
        mid = sum(data) / period
        variance = sum((x - mid) ** 2 for x in data) / period
        sd = variance ** 0.5

        upper = mid + std * sd
        lower = mid - std * sd

        return (upper, mid, lower)

    @staticmethod
    def _macd(closes: List[float], fast: int = 12, slow: int = 26, sig: int = 9) -> Optional[Tuple[float, float, float]]:
        """MACD -> (line, signal, histogram)"""
        if len(closes) < slow + sig:
            return None

        def _ema(data: List[float], period: int) -> List[float]:
            multiplier = 2 / (period + 1)
            result = [data[0]]
            for i in range(1, len(data)):
                result.append(data[i] * multiplier + result[-1] * (1 - multiplier))
            return result

        ema_fast = _ema(closes, fast)
        ema_slow = _ema(closes, slow)

        # MACD line = EMA(fast) - EMA(slow)
        macd_line = [ema_fast[i] - ema_slow[i] for i in range(len(closes))]

        # Signal line = EMA(MACD, sig)
        signal_line = _ema(macd_line, sig)

        line_val = macd_line[-1]
        signal_val = signal_line[-1]
        hist_val = line_val - signal_val

        return (line_val, signal_val, hist_val)

    @staticmethod
    def _atr(highs: List[float], lows: List[float], closes: List[float], period: int = 14) -> Optional[Tuple[float, float]]:
        """ATR -> (%, 절대값) 튜플 반환"""
        if len(highs) < period + 1:
            return None

        true_ranges = []
        for i in range(1, len(highs)):
            high = highs[i]
            low = lows[i]
            prev_close = closes[i - 1]

            tr = max(
                high - low,
                abs(high - prev_close),
                abs(low - prev_close),
            )
            true_ranges.append(tr)

        if len(true_ranges) < period:
            return None

        atr_val = sum(true_ranges[-period:]) / period
        current_price = closes[-1]

        if current_price <= 0:
            return None

        atr_pct = (atr_val / current_price) * 100
        return (atr_pct, atr_val)

    # --- 스윙 전용 ---

    @staticmethod
    def check_sepa(indicators: Dict[str, Any]) -> Tuple[bool, List[str]]:
        """
        미너비니 SEPA 트렌드 템플릿 조건 체크

        조건:
        1. MA50 > MA150 > MA200
        2. 가격 > MA50
        3. MA200 상승 추세 (close > ma200이면 간접 확인)
        4. 52주 저점 대비 +20% 이상 (KRX 완화)
        5. 52주 고점 대비 -30% 이내 (KRX 완화)

        Returns:
            (pass: bool, reasons: List[str])
        """
        reasons = []
        close = indicators.get("close")
        ma50 = indicators.get("ma50")
        ma150 = indicators.get("ma150")
        ma200 = indicators.get("ma200")
        high_52w = indicators.get("high_52w")
        low_52w = indicators.get("low_52w")

        if not all([close, ma50, ma150, ma200, high_52w, low_52w]):
            return False, ["데이터 부족"]

        # 1. MA 정렬: MA50 > MA150 > MA200
        if ma50 > ma150 > ma200:
            reasons.append("MA정렬 OK (50>150>200)")
        else:
            return False, ["MA정렬 실패"]

        # 2. 가격 > MA50
        if close > ma50:
            reasons.append(f"가격>MA50 ({close:,.0f}>{ma50:,.0f})")
        else:
            return False, ["가격<MA50"]

        # 3. MA200 상승 (change_60d > 0 으로 간접 확인)
        change_60d = indicators.get("change_60d", 0)
        if change_60d is not None and change_60d > 0:
            reasons.append(f"60일 상승 +{change_60d:.1f}%")

        # 4. 52주 저점 대비 +20% 이상 (KRX 시장 규모 고려, 30%->20% 완화)
        if low_52w > 0:
            from_low = (close - low_52w) / low_52w * 100
            if from_low >= 20:
                reasons.append(f"52w저점 대비 +{from_low:.0f}%")
            else:
                return False, [f"52w저점 대비 +{from_low:.0f}% (<20%)"]

        # 5. 52주 고점 대비 -30% 이내 (KRX 시장 규모 고려, -25%->-30% 완화)
        if high_52w > 0:
            from_high = (close - high_52w) / high_52w * 100
            if from_high >= -30:
                reasons.append(f"52w고점 대비 {from_high:.0f}%")
            else:
                return False, [f"52w고점 대비 {from_high:.0f}% (<-30%)"]

        return True, reasons

    @staticmethod
    def check_rsi2_entry(indicators: Dict[str, Any]) -> Tuple[bool, float, str]:
        """
        RSI-2 역추세 진입 조건 체크

        조건:
        1. RSI(2) < 10
        2. 가격 > MA200 (장기 상승 추세)
        3. 가격 < 볼린저밴드 하단

        Returns:
            (pass: bool, rsi_value: float, reason: str)
        """
        rsi_2 = indicators.get("rsi_2")
        close = indicators.get("close")
        ma200 = indicators.get("ma200")
        bb_lower = indicators.get("bb_lower")

        if rsi_2 is None or close is None or ma200 is None:
            return False, 0.0, "데이터 부족"

        if rsi_2 >= 10:
            return False, rsi_2, f"RSI(2)={rsi_2:.1f} (>10)"

        if close <= ma200:
            return False, rsi_2, f"가격({close:,.0f})<MA200({ma200:,.0f})"

        # BB 하단 체크 (없으면 RSI+MA 조건만으로 통과)
        reason = f"RSI(2)={rsi_2:.1f}, 가격>MA200"
        if bb_lower is not None and close < bb_lower:
            reason += f", BB하단 이탈"

        return True, rsi_2, reason

    @staticmethod
    def calculate_mrs(stock_closes: List[float], index_closes: List[float],
                      period: int = 20) -> Optional[Dict[str, float]]:
        """
        맨스필드 상대강도 (Mansfield Relative Strength)

        RS = stock / index
        MRS = ((RS / SMA(RS, period)) - 1) * 100

        Returns:
            {"mrs": float, "mrs_slope": float} or None
        """
        min_len = min(len(stock_closes), len(index_closes))
        if min_len < period + 5:
            return None

        # 길이 맞추기 (뒤에서부터)
        sc = stock_closes[-min_len:]
        ic = index_closes[-min_len:]

        # RS 계산
        rs = []
        for i in range(min_len):
            if ic[i] > 0:
                rs.append(sc[i] / ic[i])
            else:
                rs.append(0)

        if len(rs) < period:
            return None

        # SMA(RS, period)
        sma_rs = sum(rs[-period:]) / period
        if sma_rs <= 0:
            return None

        # MRS = ((RS / SMA(RS)) - 1) * 100
        mrs = (rs[-1] / sma_rs - 1) * 100

        # 5일 기울기: 5일 전 MRS와 현재 MRS의 차이
        if len(rs) >= period + 5:
            sma_rs_5ago = sum(rs[-(period + 5):-5]) / period
            if sma_rs_5ago > 0:
                mrs_5ago = (rs[-6] / sma_rs_5ago - 1) * 100
            else:
                mrs_5ago = 0.0
            mrs_slope = mrs - mrs_5ago
        else:
            mrs_slope = 0.0

        return {"mrs": round(mrs, 3), "mrs_slope": round(mrs_slope, 3)}

    def invalidate_cache(self, symbol: Optional[str] = None):
        """캐시 무효화"""
        if symbol:
            self._cache.pop(symbol, None)
            self._cache_ts.pop(symbol, None)
        else:
            self._cache.clear()
            self._cache_ts.clear()
