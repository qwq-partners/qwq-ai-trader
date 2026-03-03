"""
AI Trader US - Stock Screener

Scans universe for trading opportunities based on technical conditions:
- Volume surge (2x+ average)
- 52-week high proximity
- Momentum score ranking
- Earnings approaching
"""

import json
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import List, Dict, Optional
import pandas as pd
import numpy as np
from loguru import logger

from src.data.providers.yfinance import YFinanceProvider
from src.data.providers.finviz import FinvizProvider
from src.data.store import DataStore


@dataclass
class ScreenResult:
    """Single screened stock result"""
    symbol: str
    close: float = 0.0
    change_1d: float = 0.0
    change_5d: float = 0.0
    change_20d: float = 0.0
    volume: int = 0
    avg_volume: int = 0
    vol_ratio: float = 0.0
    rsi: float = 50.0
    pct_from_52w_high: float = 0.0
    atr_pct: float = 0.0
    score: float = 0.0              # 기술적 점수 (0~100)
    finviz_bonus: float = 0.0       # Finviz 수급/펀더멘털 보너스
    finviz_meta: dict = field(default_factory=dict)  # inst_trans, short_float 등
    flags: List[str] = field(default_factory=list)

    @property
    def total_score(self) -> float:
        """기술점수 + Finviz 보너스 합산 점수"""
        return self.score + self.finviz_bonus

    @property
    def flag_str(self) -> str:
        return " ".join(self.flags)


@dataclass
class ScreenerResult:
    """Screener output"""
    results: List[ScreenResult] = field(default_factory=list)
    scan_date: date = None
    total_scanned: int = 0

    @property
    def volume_surge(self) -> List[ScreenResult]:
        return [r for r in self.results if "VOL_SURGE" in r.flags]

    @property
    def new_highs(self) -> List[ScreenResult]:
        return [r for r in self.results if "52W_HIGH" in r.flags]

    @property
    def momentum_leaders(self) -> List[ScreenResult]:
        return sorted(self.results, key=lambda r: r.score, reverse=True)[:20]

    @property
    def breakouts(self) -> List[ScreenResult]:
        return [r for r in self.results if "BREAKOUT" in r.flags]


class StockScreener:
    """Scan universe for trading opportunities"""

    _CACHE_PATH = Path.home() / ".cache" / "ai_trader_us" / "screener_result.json"

    def __init__(self, provider: YFinanceProvider = None,
                 finviz: FinvizProvider = None):
        self._provider = provider or YFinanceProvider()
        self._store = DataStore()
        self._finviz: Optional[FinvizProvider] = finviz

    def save_cache(self, result: ScreenerResult):
        """스크리너 결과를 JSON 캐시로 저장"""
        data = {
            "scan_date": result.scan_date.isoformat(),
            "total_scanned": result.total_scanned,
            "saved_at": datetime.now().isoformat(),
            "results": [
                {
                    "symbol": r.symbol, "close": r.close,
                    "change_1d": r.change_1d, "change_5d": r.change_5d,
                    "change_20d": r.change_20d,
                    "volume": r.volume, "avg_volume": r.avg_volume,
                    "vol_ratio": r.vol_ratio, "rsi": r.rsi,
                    "pct_from_52w_high": r.pct_from_52w_high,
                    "atr_pct": r.atr_pct, "score": r.score,
                    "finviz_bonus": r.finviz_bonus,
                    "finviz_meta": r.finviz_meta or {},
                    "flags": r.flags or [],
                }
                for r in result.results
            ],
        }
        self._CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        self._CACHE_PATH.write_text(json.dumps(data, ensure_ascii=False))

    def load_cache(self) -> Optional[ScreenerResult]:
        """캐시에서 스크리너 결과 로드 (T-0 또는 T-1 유효)"""
        if not self._CACHE_PATH.exists():
            return None
        try:
            data = json.loads(self._CACHE_PATH.read_text())
            scan_date = date.fromisoformat(data["scan_date"])
            from zoneinfo import ZoneInfo
            today = datetime.now(ZoneInfo("America/New_York")).date()
            if (today - scan_date).days > 1:
                return None
            results = [
                ScreenResult(
                    symbol=r["symbol"], close=r["close"],
                    change_1d=r["change_1d"], change_5d=r["change_5d"],
                    change_20d=r["change_20d"],
                    volume=r["volume"], avg_volume=r["avg_volume"],
                    vol_ratio=r["vol_ratio"], rsi=r["rsi"],
                    pct_from_52w_high=r["pct_from_52w_high"],
                    atr_pct=r["atr_pct"], score=r["score"],
                    finviz_bonus=r.get("finviz_bonus", 0),
                    finviz_meta=r.get("finviz_meta", {}),
                    flags=r.get("flags", []),
                )
                for r in data["results"]
            ]
            return ScreenerResult(results=results, scan_date=scan_date,
                                  total_scanned=data["total_scanned"])
        except Exception:
            return None

    def set_finviz(self, finviz: FinvizProvider):
        """Finviz 프로바이더 주입 (live_engine에서 호출)"""
        self._finviz = finviz

    def scan(
        self,
        symbols: List[str],
        min_price: float = 5.0,
        min_avg_volume: int = 500_000,
        vol_surge_threshold: float = 2.0,
        lookback_days: int = 252,
        min_dollar_volume: float = 5_000_000,
        min_atr_pct: float = 2.0,
    ) -> ScreenerResult:
        """
        Scan symbols and compute screening metrics.

        Args:
            symbols: List of tickers to scan
            min_price: Minimum stock price filter
            min_avg_volume: Minimum 20-day average volume
            vol_surge_threshold: Volume surge multiplier threshold
            lookback_days: Days of history needed
        """
        # ET 기준 날짜 사용 (서버 KST와 불일치 방지)
        from zoneinfo import ZoneInfo
        today = datetime.now(ZoneInfo("America/New_York")).date()
        start = today - timedelta(days=lookback_days + 50)  # Extra buffer

        screener_result = ScreenerResult(scan_date=today, total_scanned=len(symbols))

        for i, symbol in enumerate(symbols):
            if (i + 1) % 50 == 0:
                logger.info(f"  Scanning {i+1}/{len(symbols)}...")

            try:
                result = self._analyze_symbol(
                    symbol, start, today, min_price, min_avg_volume,
                    vol_surge_threshold, min_dollar_volume, min_atr_pct,
                )
                if result:
                    screener_result.results.append(result)
            except Exception as e:
                logger.debug(f"[스크리너] {symbol} 분석 실패: {e}")

        # Finviz 보너스 적용 (FinvizProvider가 준비된 경우)
        if self._finviz and self._finviz.is_ready:
            bonus_count = 0
            for result in screener_result.results:
                bonus = self._finviz.get_bonus_score(result.symbol)
                meta = self._finviz.get_meta(result.symbol)
                if bonus != 0 or meta:
                    result.finviz_bonus = bonus
                    result.finviz_meta = meta
                    bonus_count += 1
            logger.info(
                f"[Finviz] 보너스 적용: {bonus_count}/{len(screener_result.results)}종목"
            )

        # total_score(기술 + Finviz) 기준 정렬
        screener_result.results.sort(key=lambda r: r.total_score, reverse=True)

        finviz_info = (
            f" | Finviz 커버리지: {self._finviz.coverage()}종목"
            if self._finviz and self._finviz.is_ready else ""
        )
        logger.info(
            f"Screener: {len(screener_result.results)}/{len(symbols)} passed"
            f"{finviz_info}"
        )

        return screener_result

    def _analyze_symbol(
        self,
        symbol: str,
        start: date,
        end: date,
        min_price: float,
        min_avg_volume: int,
        vol_surge_threshold: float,
        min_dollar_volume: float = 5_000_000,
        min_atr_pct: float = 2.0,
    ) -> Optional[ScreenResult]:
        """Analyze single symbol"""
        # Load data
        df = self._store.load(symbol, 'daily')
        if df is None or df.empty:
            df = self._provider.get_daily_bars(symbol, start, end)
            if not df.empty:
                self._store.save(symbol, df, 'daily')

        if df is None or df.empty or len(df) < 50:
            return None

        # Get latest data
        close = float(df['close'].iloc[-1])
        volume = int(df['volume'].iloc[-1])

        # Price filter
        if close < min_price:
            return None

        # Volume filter (shares + dollar volume)
        avg_vol_20 = int(df['volume'].tail(20).mean())
        if avg_vol_20 < min_avg_volume:
            return None
        avg_dollar_vol = float((df['volume'] * df['close']).tail(20).mean())
        if avg_dollar_vol < min_dollar_volume:
            return None

        # Compute metrics
        vol_ratio = volume / avg_vol_20 if avg_vol_20 > 0 else 0

        # Returns
        change_1d = (close / float(df['close'].iloc[-2]) - 1) * 100 if len(df) >= 2 else 0
        change_5d = (close / float(df['close'].iloc[-6]) - 1) * 100 if len(df) >= 6 else 0
        change_20d = (close / float(df['close'].iloc[-21]) - 1) * 100 if len(df) >= 21 else 0

        # RSI
        delta = df['close'].diff().tail(15)
        gain = delta.where(delta > 0, 0).mean()
        loss = (-delta.where(delta < 0, 0)).mean()
        rs = float(gain / loss) if loss > 0 else 100
        rsi = 100 - (100 / (1 + rs))

        # 52-week high
        if len(df) >= 252:
            high_52w = float(df['high'].tail(252).max())
        else:
            high_52w = float(df['high'].max())
        pct_from_high = (close - high_52w) / high_52w * 100

        # ATR%
        high = df['high'].tail(15)
        low = df['low'].tail(15)
        prev_close = df['close'].shift(1).tail(15)
        tr = pd.concat([
            high - low,
            (high - prev_close).abs(),
            (low - prev_close).abs()
        ], axis=1).max(axis=1)
        atr = float(tr.mean())
        atr_pct = (atr / close * 100) if close > 0 else 0

        # ATR% 하한 필터 (변동성 부족 종목 제외)
        if atr_pct < min_atr_pct:
            return None

        # 20-day high breakout
        prev_high_20d = float(df['high'].iloc[-21:-1].max()) if len(df) >= 21 else 0

        # Flags
        flags = []
        if vol_ratio >= vol_surge_threshold:
            flags.append("VOL_SURGE")
        if pct_from_high >= -2:
            flags.append("52W_HIGH")
        if prev_high_20d > 0 and close > prev_high_20d:
            flags.append("BREAKOUT")
        if change_1d > 3 and vol_ratio > 1.5:
            flags.append("MOMENTUM")
        if rsi < 30:
            flags.append("OVERSOLD")
        if rsi > 70:
            flags.append("OVERBOUGHT")

        # Composite screening score (0-100)
        score = 0

        # Momentum (30 pts)
        score += min(10, max(0, change_1d) * 2)
        score += min(10, max(0, change_5d) * 1)
        score += min(10, max(0, change_20d) * 0.5)

        # Volume (25 pts)
        score += min(25, vol_ratio * 5)

        # Trend (25 pts)
        ma50 = float(df['close'].tail(50).mean()) if len(df) >= 50 else close
        ma200 = float(df['close'].tail(200).mean()) if len(df) >= 200 else close
        if close > ma50:
            score += 10
        if close > ma200:
            score += 10
        if ma50 > ma200:
            score += 5

        # Proximity to high (20 pts)
        if pct_from_high >= -2:
            score += 20
        elif pct_from_high >= -5:
            score += 15
        elif pct_from_high >= -10:
            score += 10
        elif pct_from_high >= -20:
            score += 5

        score = max(0, min(100, score))

        return ScreenResult(
            symbol=symbol,
            close=close,
            change_1d=change_1d,
            change_5d=change_5d,
            change_20d=change_20d,
            volume=volume,
            avg_volume=avg_vol_20,
            vol_ratio=vol_ratio,
            rsi=rsi,
            pct_from_52w_high=pct_from_high,
            atr_pct=atr_pct,
            score=score,
            flags=flags,
        )


def print_screen_results(result: ScreenerResult, mode: str = "all", limit: int = 30):
    """Pretty print screener results"""
    print(f"\n{'='*100}")
    print(f"STOCK SCREENER | {result.scan_date} | "
          f"Passed: {len(result.results)}/{result.total_scanned}")
    print(f"{'='*100}")

    if mode == "volume":
        items = result.volume_surge
        title = "VOLUME SURGE (2x+ avg)"
    elif mode == "highs":
        items = result.new_highs
        title = "52-WEEK HIGH PROXIMITY (within -2%)"
    elif mode == "breakout":
        items = result.breakouts
        title = "20-DAY BREAKOUT"
    elif mode == "momentum":
        items = result.momentum_leaders
        title = f"TOP MOMENTUM (top {limit})"
    else:
        items = result.results
        title = "ALL RESULTS"

    items = items[:limit]

    print(f"\n{title} ({len(items)} stocks)")
    print(f"{'':>4s} {'Symbol':>7s} {'Close':>8s} {'1D%':>6s} {'5D%':>6s} "
          f"{'20D%':>6s} {'VolR':>5s} {'RSI':>5s} {'52wH%':>6s} "
          f"{'Score':>5s} {'Flags'}")
    print("-" * 95)

    for i, r in enumerate(items):
        print(f"  {i+1:>2d}. {r.symbol:>6s} ${r.close:>7.2f} "
              f"{r.change_1d:>+5.1f}% {r.change_5d:>+5.1f}% "
              f"{r.change_20d:>+5.1f}% {r.vol_ratio:>4.1f}x "
              f"{r.rsi:>4.0f} {r.pct_from_52w_high:>+5.1f}% "
              f"{r.score:>4.0f}  {r.flag_str}")

    print(f"{'='*100}")
