#!/usr/bin/env python3
"""
KR 전략 백테스트 엔진
=====================
SEPA, RSI-2, Core Holding 전략의 백테스트
pykrx OHLCV 데이터 기반, 실제 전략 로직 미러링
"""

import argparse
import os
import pickle
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from pykrx import stock as pykrx_stock

# ─── 상수 ───────────────────────────────────────────────────
CACHE_DIR = Path.home() / ".cache" / "ai_trader" / "backtest"
RESULTS_DIR = PROJECT_ROOT / "results"

# KR 수수료 (한투 BanKIS 2026~)
BUY_FEE_RATE = 0.000140527    # 매수 0.0140527%
SELL_FEE_RATE = 0.002130527   # 매도 0.2130527% (수수료+거래세)

# KOSPI/KOSDAQ 시총 상위 대형주 (pykrx 시총API 불안정 대비 하드코딩 폴백)
# 2024~2025 기준 시총 상위 150종목
DEFAULT_UNIVERSE = {
    "KOSPI": [
        "005930", "000660", "373220", "207940", "005935", "006400", "051910",
        "005380", "000270", "068270", "035420", "035720", "105560", "055550",
        "012330", "003670", "066570", "096770", "028260", "032830", "034730",
        "086790", "003550", "015760", "011200", "034020", "259960", "018260",
        "009150", "316140", "033780", "024110", "010130", "009540", "017670",
        "030200", "010950", "036570", "329180", "003490", "011170", "000810",
        "352820", "402340", "302440", "010140", "009830", "361610", "090430",
        "004020", "011790", "138040", "326030", "267250", "000720", "002790",
        "047050", "016360", "047810", "180640", "042700", "021240", "011070",
        "006800", "006260", "051900", "034220", "004170", "000990", "071050",
        "066970", "036460", "007070", "078930", "032640", "028050", "012450",
        "005490", "161390", "003410", "023530", "069500", "004990", "005830",
        "012750", "010120", "009240", "033920", "005940", "011780", "018880",
        "271560", "001570", "004370", "001440", "000080", "139480", "036830",
        "008770", "138930",
    ],
    "KOSDAQ": [
        "247540", "403870", "068760", "196170", "377300", "041510", "145020",
        "263750", "328130", "357780", "086520", "039030", "035900", "383310",
        "336260", "067310", "020150", "112040", "360750", "323410", "141080",
        "397880", "950140", "214150", "257720", "090460", "095340", "036930",
        "028300", "137310", "298380", "058470", "293490", "234080", "060310",
        "217190", "131970", "215600", "011040", "078600", "048410", "240810",
        "226330", "066910", "950160", "054950", "352480", "222160", "041190",
        "253450",
    ],
}


# ─── Enums ──────────────────────────────────────────────────
class ExitStage(Enum):
    NONE = 0
    FIRST = 1
    SECOND = 2
    THIRD = 3
    TRAILING = 4


class RegimeType(Enum):
    BULLISH = "bullish"
    NEUTRAL = "neutral"
    BEARISH = "bearish"


class StrategyType(Enum):
    SEPA = "sepa"
    RSI2 = "rsi2"
    CORE = "core"


# ─── Dataclasses ────────────────────────────────────────────
@dataclass
class BacktestConfig:
    months: int = 6
    initial_capital: int = 10_000_000
    strategies: List[str] = field(default_factory=lambda: ["sepa", "rsi2", "core"])
    universe_size: int = 150
    use_cache: bool = True
    use_t1: bool = True
    # 전략 배분
    allocation: Dict[str, float] = field(default_factory=lambda: {
        "sepa": 0.60, "rsi2": 0.10, "core": 0.30
    })
    # 리스크
    max_positions_short: int = 5
    max_positions_core: int = 3
    base_position_pct: float = 25.0
    max_position_pct: float = 28.0
    min_cash_reserve_pct: float = 5.0
    daily_max_loss_pct: float = 5.0
    min_position_value: int = 200_000
    # SEPA
    sepa_min_score: float = 60.0
    sepa_stop_loss_pct: float = 5.0
    sepa_max_holding_days: int = 10
    # RSI2
    rsi2_min_score: float = 60.0
    rsi2_stop_loss_pct: float = 5.0
    rsi2_max_holding_days: int = 10
    # Core
    core_min_score: float = 70.0
    core_stop_loss_pct: float = 15.0
    core_trailing_stop_pct: float = 8.0
    core_trailing_activate_pct: float = 10.0
    # 분할 익절 (SEPA/RSI2)
    first_exit_pct: float = 5.0
    first_exit_ratio: float = 0.30
    second_exit_pct: float = 15.0
    second_exit_ratio: float = 0.50
    third_exit_pct: float = 25.0
    third_exit_ratio: float = 0.50
    trailing_stop_pct: float = 3.0
    trailing_activate_pct: float = 5.0
    # ATR 동적 손절
    atr_multiplier: float = 2.0
    min_stop_pct: float = 3.5
    max_stop_pct: float = 6.0
    # 조기 청산
    stale_exit_days: int = 10
    stale_exit_pnl_pct: float = 2.0
    stale_high_days: int = 7
    stale_high_min_pnl_pct: float = 1.0


@dataclass
class BTPosition:
    symbol: str
    name: str
    strategy: StrategyType
    entry_date: str
    entry_price: float
    quantity: int
    cost_basis: float
    highest_price: float
    exit_stage: ExitStage = ExitStage.NONE
    remaining_quantity: int = 0
    atr_stop_pct: float = 5.0
    score_at_entry: float = 0.0
    trailing_activated: bool = False
    breakeven_activated: bool = False
    holding_days: int = 0
    days_since_high: int = 0

    def __post_init__(self):
        if self.remaining_quantity == 0:
            self.remaining_quantity = self.quantity


@dataclass
class Trade:
    symbol: str
    name: str
    strategy: str
    side: str
    date: str
    price: float
    quantity: int
    amount: float
    fee: float
    reason: str = ""
    pnl: float = 0.0
    pnl_pct: float = 0.0
    holding_days: int = 0


# ─── 수수료 계산 ────────────────────────────────────────────
class BTFeeCalculator:
    @staticmethod
    def buy_fee(amount: float) -> float:
        return amount * BUY_FEE_RATE

    @staticmethod
    def sell_fee(amount: float) -> float:
        return amount * SELL_FEE_RATE

    @staticmethod
    def net_pnl(buy_price: float, sell_price: float, quantity: int) -> Tuple[float, float]:
        """순손익 → (pnl_amount, pnl_pct)"""
        buy_amount = buy_price * quantity
        sell_amount = sell_price * quantity
        buy_f = buy_amount * BUY_FEE_RATE
        sell_f = sell_amount * SELL_FEE_RATE
        total_cost = buy_amount + buy_f
        net_proceeds = sell_amount - sell_f
        pnl = net_proceeds - total_cost
        pnl_pct = (pnl / total_cost) * 100 if total_cost > 0 else 0.0
        return pnl, pnl_pct

    @staticmethod
    def target_price(entry_price: float, target_pct: float) -> float:
        """수수료 포함 목표가"""
        buy_rate = 1 + BUY_FEE_RATE
        sell_rate = 1 - SELL_FEE_RATE
        multiplier = (1 + target_pct / 100) * buy_rate / sell_rate
        return entry_price * multiplier


# ─── 기술 지표 계산 ─────────────────────────────────────────
class BTIndicators:
    @staticmethod
    def compute(df: pd.DataFrame) -> pd.DataFrame:
        """OHLCV DataFrame에 기술 지표 추가"""
        if len(df) < 10:
            return df

        c = df['종가'].astype(float)
        h = df['고가'].astype(float)
        low = df['저가'].astype(float)
        v = df['거래량'].astype(float)

        # 이동평균
        for p in [5, 10, 20, 50, 150, 200]:
            df[f'ma{p}'] = c.rolling(p).mean()

        # RSI (Wilder's smoothing)
        for period in [2, 14]:
            delta = c.diff()
            gain = delta.clip(lower=0)
            loss = (-delta).clip(lower=0)
            avg_gain = gain.ewm(alpha=1 / period, min_periods=period).mean()
            avg_loss = loss.ewm(alpha=1 / period, min_periods=period).mean()
            rs = avg_gain / avg_loss.replace(0, np.nan)
            df[f'rsi_{period}'] = 100 - (100 / (1 + rs))

        # Bollinger Bands
        df['bb_mid'] = c.rolling(20).mean()
        bb_std = c.rolling(20).std()
        df['bb_upper'] = df['bb_mid'] + 2.0 * bb_std
        df['bb_lower'] = df['bb_mid'] - 2.0 * bb_std

        # ATR (Wilder's)
        tr = pd.concat([
            h - low,
            (h - c.shift(1)).abs(),
            (low - c.shift(1)).abs()
        ], axis=1).max(axis=1)
        df['atr_14'] = tr.ewm(alpha=1 / 14, min_periods=14).mean()
        df['atr_pct'] = (df['atr_14'] / c * 100).replace([np.inf, -np.inf], np.nan)

        # MACD
        ema12 = c.ewm(span=12).mean()
        ema26 = c.ewm(span=26).mean()
        df['macd'] = ema12 - ema26
        df['macd_signal'] = df['macd'].ewm(span=9).mean()
        df['macd_hist'] = df['macd'] - df['macd_signal']

        # 거래량 비율
        df['vol_ma20'] = v.rolling(20).mean()
        df['vol_ratio'] = (v / df['vol_ma20']).replace([np.inf, -np.inf], np.nan)

        # 52주 고저
        df['high_52w'] = h.rolling(252, min_periods=60).max()
        df['low_52w'] = low.rolling(252, min_periods=60).min()

        # 변화율
        df['change_5d'] = c.pct_change(5) * 100
        df['change_20d'] = c.pct_change(20) * 100
        df['change_60d'] = c.pct_change(60) * 100
        df['change_120d'] = c.pct_change(120) * 100

        # MA 관계
        df['ma5_above_ma20'] = df['ma5'] > df['ma20']

        # SEPA 미너비니 템플릿
        df['sepa_pass'] = (
            (df['ma50'] > df['ma150']) &
            (df['ma150'] > df['ma200']) &
            (c > df['ma50']) &
            (df['change_60d'] > 0) &
            (c >= df['low_52w'] * 1.20) &
            (c >= df['high_52w'] * 0.70)
        )

        # 거래대금 (pykrx 버전에 따라 없을 수 있음 → 추정)
        if '거래대금' not in df.columns:
            # 거래대금 ≈ 거래량 × (시가+고가+저가+종가)/4
            avg_price = (df['시가'].astype(float) + h + low + c) / 4
            df['거래대금'] = v * avg_price

        return df


# ─── 시장 레짐 판단 ─────────────────────────────────────────
class MarketRegime:
    def __init__(self):
        self.kospi_data: Optional[pd.DataFrame] = None

    def load(self, start: str, end: str):
        """KOSPI 지수 또는 삼성전자 대리 지표로 레짐 판단"""
        print("  시장 레짐 지표 로드 중...")
        df = None
        # 1차: KOSPI 지수
        try:
            df = pykrx_stock.get_index_ohlcv_by_date(start, end, "1001")
        except Exception:
            pass

        # 2차: 삼성전자 OHLCV (대리 지표)
        if df is None or len(df) < 20:
            try:
                df = pykrx_stock.get_market_ohlcv_by_date(
                    start, end, "005930")
                if df is not None and len(df) > 0:
                    print("  KOSPI 지수 실패 → 삼성전자 OHLCV 대리 사용")
            except Exception:
                pass

        if df is not None and len(df) > 0:
            c = df['종가'].astype(float)
            df['ma20'] = c.rolling(20).mean()
            df['ma60'] = c.rolling(60).mean()
            df['ma200'] = c.rolling(200).mean()
            self.kospi_data = df
            print(f"  레짐 지표 {len(df)}일 로드 완료")
        else:
            print("  레짐 지표 로드 실패 — NEUTRAL 고정")

    def get_regime(self, date: str) -> RegimeType:
        if self.kospi_data is None:
            return RegimeType.NEUTRAL
        try:
            date_ts = pd.Timestamp(date)
            mask = self.kospi_data.index <= date_ts
            if not mask.any():
                return RegimeType.NEUTRAL
            row = self.kospi_data.loc[mask].iloc[-1]
        except (KeyError, IndexError):
            return RegimeType.NEUTRAL

        close = float(row['종가'])
        ma20 = row.get('ma20')
        ma60 = row.get('ma60')
        ma200 = row.get('ma200')

        if pd.isna(ma200):
            return RegimeType.NEUTRAL

        ma20 = float(ma20) if not pd.isna(ma20) else close
        ma60 = float(ma60) if not pd.isna(ma60) else close
        ma200 = float(ma200)

        if close > ma20 > ma60 > ma200:
            return RegimeType.BULLISH
        elif close < ma200 and ma20 < ma60:
            return RegimeType.BEARISH
        return RegimeType.NEUTRAL


# ─── 유니버스 관리 ──────────────────────────────────────────
class UniverseManager:
    def __init__(self, size: int = 150, use_cache: bool = True):
        self.size = size
        self.use_cache = use_cache
        self.tickers: List[str] = []
        self.names: Dict[str, str] = {}
        self.ohlcv: Dict[str, pd.DataFrame] = {}

    def build_universe(self, ref_date: str):
        print(f"\n유니버스 구성 (기준일: {ref_date})...")
        kospi_n = int(self.size * 2 / 3)
        kosdaq_n = self.size - kospi_n
        tickers = []

        # 1차: pykrx 시총 기반
        for market, n in [("KOSPI", kospi_n), ("KOSDAQ", kosdaq_n)]:
            try:
                cap_df = pykrx_stock.get_market_cap_by_ticker(
                    ref_date, market=market)
                if cap_df is not None and len(cap_df) > 0:
                    top = cap_df.nlargest(n, '시가총액')
                    tickers.extend(top.index.tolist())
                    print(f"  {market}: {len(top)}종목 (시총 기반)")
            except Exception:
                pass
            time.sleep(0.3)

        # 2차: 폴백 — 하드코딩 유니버스
        if len(tickers) < 10:
            print("  pykrx 시총 API 실패 → 하드코딩 유니버스 사용")
            tickers = (DEFAULT_UNIVERSE["KOSPI"][:kospi_n]
                       + DEFAULT_UNIVERSE["KOSDAQ"][:kosdaq_n])

        self.tickers = tickers[:self.size]

        # 종목명 조회
        for t in self.tickers:
            try:
                name = pykrx_stock.get_market_ticker_name(t)
                self.names[t] = name if name else t
            except Exception:
                self.names[t] = t
            time.sleep(0.05)
        print(f"  총 {len(self.tickers)}종목 유니버스 구성 완료")

    def load_ohlcv(self, start: str, end: str):
        print(f"\nOHLCV 데이터 로드 ({start} ~ {end})...")
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        loaded, cached, failed = 0, 0, 0

        for i, ticker in enumerate(self.tickers):
            cache_file = CACHE_DIR / f"ohlcv_{ticker}_{start}_{end}.pkl"

            if self.use_cache and cache_file.exists():
                try:
                    with open(cache_file, 'rb') as f:
                        df = pickle.load(f)
                    if df is not None and len(df) > 20:
                        self.ohlcv[ticker] = BTIndicators.compute(df)
                        cached += 1
                        continue
                except Exception:
                    pass

            try:
                df = pykrx_stock.get_market_ohlcv_by_date(start, end, ticker)
                if df is not None and len(df) > 20:
                    with open(cache_file, 'wb') as f:
                        pickle.dump(df, f)
                    self.ohlcv[ticker] = BTIndicators.compute(df)
                    loaded += 1
                else:
                    failed += 1
            except Exception:
                failed += 1

            if (i + 1) % 10 == 0:
                print(f"  진행: {i+1}/{len(self.tickers)} "
                      f"(신규:{loaded} 캐시:{cached} 실패:{failed})")
            time.sleep(0.3)

        print(f"  완료: 신규 {loaded} + 캐시 {cached} = "
              f"{loaded + cached}종목 (실패 {failed})")

    def get_data(self, ticker: str, date: str) -> Optional[pd.Series]:
        df = self.ohlcv.get(ticker)
        if df is None:
            return None
        try:
            date_ts = pd.Timestamp(date)
            mask = df.index <= date_ts
            if not mask.any():
                return None
            return df.loc[mask].iloc[-1]
        except (KeyError, IndexError):
            return None

    def get_row_on_date(self, ticker: str, date: str) -> Optional[pd.Series]:
        """정확히 해당 날짜의 데이터 (거래일이 아니면 None)"""
        df = self.ohlcv.get(ticker)
        if df is None:
            return None
        try:
            date_ts = pd.Timestamp(date)
            if date_ts in df.index:
                return df.loc[date_ts]
        except (KeyError, IndexError):
            pass
        return None


# ─── 전략 스코어링 ─────────────────────────────────────────
class StrategyScorer:
    def __init__(self, config: BacktestConfig):
        self.config = config

    def score_sepa(self, data: pd.Series) -> float:
        """SEPA 전략 100점 스코어링"""
        score = 0.0
        close = float(data.get('종가', 0))
        if close < 1000:
            return 0.0

        amount = float(data.get('거래대금', 0))
        if amount < 500_000_000:
            return 0.0

        ma200 = data.get('ma200')
        if pd.isna(ma200) or ma200 is None:
            return 0.0

        # ── 기술적 지표 (40점) ──
        if data.get('sepa_pass', False):
            score += 15.0

        ma50 = data.get('ma50')
        if not pd.isna(ma50) and float(ma200) > 0:
            spread = (float(ma50) - float(ma200)) / float(ma200) * 100
            if spread > 10:
                score += 7
            elif spread > 5:
                score += 5
            elif spread > 2:
                score += 3

        high_52w = data.get('high_52w')
        if not pd.isna(high_52w) and float(high_52w) > 0:
            pct_from_high = (close - float(high_52w)) / float(high_52w) * 100
            if pct_from_high >= -5:
                score += 7
            elif pct_from_high >= -10:
                score += 5
            elif pct_from_high >= -15:
                score += 3
            elif pct_from_high >= -25:
                score += 1

        change_120d = data.get('change_120d', 0)
        change_60d = data.get('change_60d', 0)
        if not pd.isna(change_120d) and not pd.isna(change_60d):
            if float(change_120d) > 0 and float(change_60d) > float(change_120d) * 0.5:
                score += 5
            elif float(change_120d) > 0:
                score += 3

        if data.get('ma5_above_ma20', False):
            score += 3

        # ── 수급 (20점) → 중립 10점 ──
        score += 10.0

        # ── 펀더멘탈 (10점) → 중립 5점 ──
        score += 5.0

        # ── 거래량 모멘텀 (10점) ──
        vol_ratio = data.get('vol_ratio', 1.0)
        if not pd.isna(vol_ratio):
            vr = float(vol_ratio)
            if vr > 2.0:
                score += 10
            elif vr > 1.5:
                score += 7
            elif vr > 1.0:
                score += 4

        # ── 섹터 (10점) → 중립 5점 ──
        score += 5.0

        # ── 오버레이 보너스 (최대 10점) ──
        bb_lower = data.get('bb_lower')
        if not pd.isna(bb_lower) and close < float(bb_lower) * 1.02:
            score += 3

        macd_hist = data.get('macd_hist')
        if not pd.isna(macd_hist) and float(macd_hist) > 0:
            score += 3

        open_price = data.get('시가', close)
        if (not pd.isna(vol_ratio) and float(vol_ratio) > 2.0
                and close > float(open_price)):
            score += 4

        return min(score, 100.0)

    def score_rsi2(self, data: pd.Series) -> float:
        """RSI-2 역추세 전략 100점 스코어링"""
        score = 0.0
        close = float(data.get('종가', 0))
        if close < 1000:
            return 0.0

        ma200 = data.get('ma200')
        if pd.isna(ma200) or float(ma200) <= 0:
            return 0.0
        if close <= float(ma200):
            return 0.0

        rsi2 = data.get('rsi_2')
        if pd.isna(rsi2):
            return 0.0
        rsi2 = float(rsi2)
        if rsi2 >= 30:
            return 0.0

        # ── RSI 포지션 (30점) ──
        if rsi2 < 5:
            score += 30
        elif rsi2 < 10:
            score += 22
        elif rsi2 < 15:
            score += 15
        elif rsi2 < 20:
            score += 10
        else:
            score += 5

        # ── MA200 거리 (15점) ──
        ma200_dist = (close - float(ma200)) / float(ma200) * 100
        if ma200_dist > 20:
            score += 15
        elif ma200_dist > 10:
            score += 11
        elif ma200_dist > 0:
            score += 7

        # ── BB 하단 이탈 (15점) ──
        bb_lower = data.get('bb_lower')
        if not pd.isna(bb_lower):
            bb = float(bb_lower)
            if close < bb * 0.98:
                score += 15
            elif close < bb:
                score += 10
            elif close < bb * 1.01:
                score += 5

        # ── 수급 (20점) → 중립 10점 ──
        score += 10.0

        # ── MRS (5점) → 중립 2.5점 ──
        score += 2.5

        # ── 5일 하락 (10점) ──
        change_5d = data.get('change_5d', 0)
        if not pd.isna(change_5d):
            c5 = float(change_5d)
            if c5 < -5:
                score += 10
            elif c5 < -3:
                score += 5

        # ── 거래대금 증가 (5점) ──
        vol_ratio = data.get('vol_ratio', 1.0)
        if not pd.isna(vol_ratio):
            vr = float(vol_ratio)
            if vr > 1.5:
                score += 5
            elif vr > 1.0:
                score += 3

        return min(score, 100.0)

    def score_core(self, data: pd.Series) -> float:
        """Core Holding 중장기 전략 100점 스코어링"""
        score = 0.0
        close = float(data.get('종가', 0))
        if close < 5000:
            return 0.0

        ma200 = data.get('ma200')
        if pd.isna(ma200) or float(ma200) <= 0:
            return 0.0

        # ── 추세 안정성 (30점) ──
        ma10 = data.get('ma10')
        ma20 = data.get('ma20')
        ma50 = data.get('ma50')
        ma_vals = [ma10, ma20, ma50, ma200]
        if all(not pd.isna(v) for v in ma_vals):
            vals = [float(v) for v in ma_vals]
            if vals[0] > vals[1] > vals[2] > vals[3]:
                score += 10
            elif vals[2] > vals[3] and close > vals[2]:
                score += 5

        if close > float(ma200):
            score += 5

        high_52w = data.get('high_52w')
        if not pd.isna(high_52w) and float(high_52w) > 0:
            pct = (close - float(high_52w)) / float(high_52w) * 100
            if pct >= -10:
                score += 5
            elif pct >= -20:
                score += 3

        change_120d = data.get('change_120d', 0)
        if not pd.isna(change_120d):
            c120 = float(change_120d)
            if c120 > 20:
                score += 5
            elif c120 > 10:
                score += 3
            elif c120 > 0:
                score += 1

        atr_pct = data.get('atr_pct')
        if not pd.isna(atr_pct) and float(atr_pct) < 3.0:
            score += 5

        # ── 펀더멘탈 (30점) → 중립 15점 ──
        score += 15.0

        # ── 수급 (20점) → 중립 10점 ──
        score += 10.0

        # ── 모멘텀 품질 (20점) ──
        if not pd.isna(change_120d) and float(change_120d) > 10:
            score += 5
        elif not pd.isna(change_120d) and float(change_120d) > 0:
            score += 3

        change_20d = data.get('change_20d', 0)
        if not pd.isna(change_20d) and float(change_20d) > 5:
            score += 5
        elif not pd.isna(change_20d) and float(change_20d) > 0:
            score += 3

        change_60d = data.get('change_60d', 0)
        if not pd.isna(change_60d) and float(change_60d) > 10:
            score += 5
        elif not pd.isna(change_60d) and float(change_60d) > 0:
            score += 2

        if data.get('ma5_above_ma20', False):
            score += 5

        return min(score, 100.0)


# ─── 청산 관리 ──────────────────────────────────────────────
class BTExitManager:
    def __init__(self, config: BacktestConfig):
        self.config = config
        self.fee = BTFeeCalculator()

    def check_exit(
        self, pos: BTPosition, row: pd.Series
    ) -> List[Tuple[str, int, float, str]]:
        """일봉 데이터로 청산 체크 → [(action, qty, price, reason)]"""
        actions = []
        high = float(row['고가'])
        low = float(row['저가'])
        close = float(row['종가'])

        if pos.remaining_quantity <= 0:
            return actions

        entry = pos.entry_price
        is_core = pos.strategy == StrategyType.CORE

        # 최고가 갱신
        if high > pos.highest_price:
            pos.highest_price = high
            pos.days_since_high = 0
        else:
            pos.days_since_high += 1

        pos.holding_days += 1

        # ── 1. 손절 (저가 기준) ──
        if is_core:
            stop_pct = self.config.core_stop_loss_pct
        else:
            atr_pct = row.get('atr_pct')
            if not pd.isna(atr_pct) and float(atr_pct) > 0:
                stop_pct = max(
                    self.config.min_stop_pct,
                    min(self.config.max_stop_pct,
                        float(atr_pct) * self.config.atr_multiplier)
                )
            else:
                stop_pct = (self.config.sepa_stop_loss_pct
                            if pos.strategy == StrategyType.SEPA
                            else self.config.rsi2_stop_loss_pct)
            pos.atr_stop_pct = stop_pct

        stop_price = entry * (1 - stop_pct / 100)
        if low <= stop_price:
            sell_price = stop_price
            actions.append(("SELL", pos.remaining_quantity, sell_price,
                            f"손절 -{stop_pct:.1f}%"))
            pos.remaining_quantity = 0
            return actions

        # ── 2. 본전 보호 ──
        if pos.breakeven_activated:
            _, net_pnl_pct = self.fee.net_pnl(entry, close, 1)
            if net_pnl_pct <= -0.25:
                actions.append(("SELL", pos.remaining_quantity, close,
                                "본전보호 청산"))
                pos.remaining_quantity = 0
                return actions

        # ── 3. 분할 익절 (SEPA/RSI2만) ──
        if not is_core:
            _, high_pnl = self.fee.net_pnl(entry, high, 1)

            if (pos.exit_stage == ExitStage.NONE
                    and high_pnl >= self.config.first_exit_pct):
                qty = max(1, int(pos.remaining_quantity
                                 * self.config.first_exit_ratio))
                actions.append(("SELL", qty, close,
                                f"1차 익절 +{self.config.first_exit_pct}%"))
                pos.remaining_quantity -= qty
                pos.exit_stage = ExitStage.FIRST
                pos.breakeven_activated = True

            elif (pos.exit_stage == ExitStage.FIRST
                  and high_pnl >= self.config.second_exit_pct):
                qty = max(1, int(pos.remaining_quantity
                                 * self.config.second_exit_ratio))
                actions.append(("SELL", qty, close,
                                f"2차 익절 +{self.config.second_exit_pct}%"))
                pos.remaining_quantity -= qty
                pos.exit_stage = ExitStage.SECOND

            elif (pos.exit_stage == ExitStage.SECOND
                  and high_pnl >= self.config.third_exit_pct):
                qty = max(1, int(pos.remaining_quantity
                                 * self.config.third_exit_ratio))
                actions.append(("SELL", qty, close,
                                f"3차 익절 +{self.config.third_exit_pct}%"))
                pos.remaining_quantity -= qty
                pos.exit_stage = ExitStage.THIRD

            if pos.exit_stage == ExitStage.THIRD:
                pos.exit_stage = ExitStage.TRAILING
                pos.trailing_activated = True

        # ── 4. 트레일링 스탑 ──
        if is_core:
            trail_pct = self.config.core_trailing_stop_pct
            trail_activate = self.config.core_trailing_activate_pct
        else:
            trail_pct = self.config.trailing_stop_pct
            trail_activate = self.config.trailing_activate_pct

        _, peak_pnl = self.fee.net_pnl(entry, pos.highest_price, 1)
        if peak_pnl >= trail_activate:
            pos.trailing_activated = True

        if pos.trailing_activated and pos.remaining_quantity > 0:
            trail_price = pos.highest_price * (1 - trail_pct / 100)
            if close <= trail_price:
                actions.append(("SELL", pos.remaining_quantity, close,
                                f"트레일링 (고점 {pos.highest_price:,.0f} "
                                f"대비 -{trail_pct}%)"))
                pos.remaining_quantity = 0
                return actions

        # ── 5. 최대 보유일 (SEPA/RSI2) ──
        if not is_core:
            max_days = (self.config.sepa_max_holding_days
                        if pos.strategy == StrategyType.SEPA
                        else self.config.rsi2_max_holding_days)
            if pos.holding_days >= max_days:
                actions.append(("SELL", pos.remaining_quantity, close,
                                f"보유기간 초과 ({pos.holding_days}일)"))
                pos.remaining_quantity = 0
                return actions

        # ── 6. 횡보 청산 ──
        if not is_core and pos.holding_days >= self.config.stale_exit_days:
            _, pnl_pct = self.fee.net_pnl(entry, close, 1)
            if abs(pnl_pct) < self.config.stale_exit_pnl_pct:
                actions.append(("SELL", pos.remaining_quantity, close,
                                f"횡보 청산 ({pos.holding_days}일)"))
                pos.remaining_quantity = 0
                return actions

        # ── 7. 추세 무효화 (신고가 갱신 실패) ──
        if (not is_core
                and pos.days_since_high >= self.config.stale_high_days):
            _, pnl_pct = self.fee.net_pnl(entry, close, 1)
            if pnl_pct < self.config.stale_high_min_pnl_pct:
                actions.append(("SELL", pos.remaining_quantity, close,
                                f"추세 무효화 ({pos.days_since_high}일 "
                                f"신고가 없음)"))
                pos.remaining_quantity = 0
                return actions

        return actions


# ─── 백테스트 엔진 ─────────────────────────────────────────
class BacktestEngine:
    def __init__(self, config: BacktestConfig):
        self.config = config
        self.universe = UniverseManager(config.universe_size, config.use_cache)
        self.scorer = StrategyScorer(config)
        self.exit_mgr = BTExitManager(config)
        self.regime = MarketRegime()
        self.fee = BTFeeCalculator()

        self.cash: float = float(config.initial_capital)
        self.positions: Dict[str, BTPosition] = {}
        self.trades: List[Trade] = []
        self.equity_curve: List[Tuple[str, float]] = []
        self.pending_buys: List[dict] = []
        self.daily_loss_locked: bool = False
        self.peak_equity: float = float(config.initial_capital)

    def run(self):
        end_date = datetime.now()
        start_date = end_date - timedelta(days=self.config.months * 30)
        warmup_date = start_date - timedelta(days=400)

        end_str = end_date.strftime("%Y%m%d")
        start_str = start_date.strftime("%Y%m%d")
        warmup_str = warmup_date.strftime("%Y%m%d")

        print("=" * 60)
        print("KR 전략 백테스트 엔진")
        print(f"기간: {start_str} ~ {end_str} ({self.config.months}개월)")
        print(f"초기 자본: \u20a9{self.config.initial_capital:,.0f}")
        print(f"전략: {', '.join(self.config.strategies)}")
        print("=" * 60)

        # 1. 유니버스 (시작 2개월 전 시총 기준)
        ref_dt = start_date - timedelta(days=60)
        self.universe.build_universe(ref_dt.strftime("%Y%m%d"))

        # 2. OHLCV (워밍업 포함)
        self.universe.load_ohlcv(warmup_str, end_str)

        # 3. KOSPI 지수 (레짐)
        self.regime.load(warmup_str, end_str)

        # 4. 거래일
        trading_days = self._get_trading_days(start_str, end_str)
        if not trading_days:
            print("거래일 데이터 없음")
            return

        print(f"\n시뮬레이션: {len(trading_days)}일")
        print("-" * 60)

        # 5. 일별 루프
        prev_equity = self.cash
        for i, day in enumerate(trading_days):
            day_str = day.strftime("%Y-%m-%d")
            self.daily_loss_locked = False

            # T+1 주문 실행
            self._execute_pending_buys(day_str)

            # 청산 체크
            self._check_exits(day_str)

            # 시그널 생성
            if not self.daily_loss_locked:
                self._generate_signals(day_str)

            # T+0 모드: 당일 시그널 즉시 실행
            if not self.config.use_t1 and self.pending_buys:
                self._execute_pending_buys(day_str, use_close=True)

            # 자산 기록
            equity = self._calc_equity(day_str)
            self.equity_curve.append((day_str, equity))

            daily_ret = ((equity - prev_equity) / prev_equity * 100
                         if prev_equity > 0 else 0)
            if daily_ret <= -self.config.daily_max_loss_pct:
                self.daily_loss_locked = True

            if equity > self.peak_equity:
                self.peak_equity = equity
            prev_equity = equity

            if (i + 1) % 20 == 0 or i == len(trading_days) - 1:
                n_pos = len([p for p in self.positions.values()
                             if p.remaining_quantity > 0])
                print(f"  [{day_str}] 자산 \u20a9{equity:,.0f} | "
                      f"포지션 {n_pos}개 | 거래 {len(self.trades)}건")

        # 잔여 포지션 청산
        self._close_all(trading_days[-1].strftime("%Y-%m-%d"))

        # 결과
        analyzer = ResultAnalyzer(self.config, self.trades, self.equity_curve)
        analyzer.print_results()
        analyzer.save_csv()

    def _get_trading_days(self, start: str, end: str) -> List[pd.Timestamp]:
        sample = self.universe.tickers[0] if self.universe.tickers else "005930"
        df = self.universe.ohlcv.get(sample)
        if df is not None:
            mask = ((df.index >= pd.Timestamp(start))
                    & (df.index <= pd.Timestamp(end)))
            return sorted(df.index[mask].tolist())
        if self.regime.kospi_data is not None:
            mask = ((self.regime.kospi_data.index >= pd.Timestamp(start))
                    & (self.regime.kospi_data.index <= pd.Timestamp(end)))
            return sorted(self.regime.kospi_data.index[mask].tolist())
        return []

    def _execute_pending_buys(self, day_str: str, use_close: bool = False):
        """T+1 대기 주문 실행"""
        if not self.pending_buys:
            return

        # 포지션 수 카운트
        short_count = len([
            p for p in self.positions.values()
            if p.strategy in (StrategyType.SEPA, StrategyType.RSI2)
            and p.remaining_quantity > 0
        ])
        core_count = len([
            p for p in self.positions.values()
            if p.strategy == StrategyType.CORE
            and p.remaining_quantity > 0
        ])

        # 점수 높은 순 정렬
        orders = sorted(self.pending_buys, key=lambda x: x.get('score', 0),
                        reverse=True)
        self.pending_buys = []

        for order in orders:
            symbol = order['symbol']
            strategy = order['strategy']

            # 포지션 수 제한
            if strategy in (StrategyType.SEPA, StrategyType.RSI2):
                if short_count >= self.config.max_positions_short:
                    continue
            elif strategy == StrategyType.CORE:
                if core_count >= self.config.max_positions_core:
                    continue

            # 이미 보유 중
            if (symbol in self.positions
                    and self.positions[symbol].remaining_quantity > 0):
                continue

            data = self.universe.get_row_on_date(symbol, day_str)
            if data is None:
                data = self.universe.get_data(symbol, day_str)
            if data is None:
                continue

            if use_close:
                exec_price = float(data.get('종가', 0))
            else:
                exec_price = float(data.get('시가', 0))
            if exec_price <= 0:
                continue

            # 갭다운 필터
            prev_close = order.get('signal_close', exec_price)
            if prev_close > 0 and not use_close:
                gap = (exec_price - prev_close) / prev_close * 100
                if gap < -3.5:
                    continue

            # 사이징
            equity = self._calc_equity(day_str)
            pos_value = self._calc_position_size(equity, strategy)
            if pos_value < self.config.min_position_value:
                continue

            quantity = int(pos_value / exec_price)
            if quantity <= 0:
                continue

            buy_amount = exec_price * quantity
            buy_fee = self.fee.buy_fee(buy_amount)
            total_cost = buy_amount + buy_fee

            if total_cost > self.cash:
                quantity = int(
                    (self.cash * 0.99) / (exec_price * (1 + BUY_FEE_RATE)))
                if quantity <= 0:
                    continue
                buy_amount = exec_price * quantity
                buy_fee = self.fee.buy_fee(buy_amount)
                total_cost = buy_amount + buy_fee

            # 최소 현금 보유
            min_reserve = equity * self.config.min_cash_reserve_pct / 100
            if (self.cash - total_cost) < min_reserve:
                continue

            # 매수 체결
            self.cash -= total_cost
            name = self.universe.names.get(symbol, symbol)
            pos = BTPosition(
                symbol=symbol, name=name, strategy=strategy,
                entry_date=day_str, entry_price=exec_price,
                quantity=quantity, cost_basis=total_cost,
                highest_price=float(data.get('고가', exec_price)),
                score_at_entry=order.get('score', 0),
            )

            atr_pct = data.get('atr_pct')
            if not pd.isna(atr_pct) and float(atr_pct) > 0:
                pos.atr_stop_pct = max(
                    self.config.min_stop_pct,
                    min(self.config.max_stop_pct,
                        float(atr_pct) * self.config.atr_multiplier))

            self.positions[symbol] = pos
            self.trades.append(Trade(
                symbol=symbol, name=name, strategy=strategy.value,
                side="BUY", date=day_str, price=exec_price,
                quantity=quantity, amount=buy_amount, fee=buy_fee,
                reason=f"진입 (점수 {order.get('score', 0):.0f})"
            ))

            if strategy in (StrategyType.SEPA, StrategyType.RSI2):
                short_count += 1
            else:
                core_count += 1

    def _check_exits(self, day_str: str):
        to_remove = []
        for symbol, pos in list(self.positions.items()):
            if pos.remaining_quantity <= 0:
                to_remove.append(symbol)
                continue

            data = self.universe.get_row_on_date(symbol, day_str)
            if data is None:
                continue

            actions = self.exit_mgr.check_exit(pos, data)
            for _, qty, price, reason in actions:
                if qty <= 0:
                    continue
                sell_amount = price * qty
                sell_fee = self.fee.sell_fee(sell_amount)
                self.cash += sell_amount - sell_fee
                pnl, pnl_pct = self.fee.net_pnl(pos.entry_price, price, qty)
                self.trades.append(Trade(
                    symbol=symbol, name=pos.name,
                    strategy=pos.strategy.value,
                    side="SELL", date=day_str, price=price,
                    quantity=qty, amount=sell_amount, fee=sell_fee,
                    reason=reason, pnl=pnl, pnl_pct=pnl_pct,
                    holding_days=pos.holding_days
                ))
                self.daily_loss_locked = (
                    self.daily_loss_locked or pnl < 0
                )

            if pos.remaining_quantity <= 0:
                to_remove.append(symbol)

        for sym in set(to_remove):
            if (sym in self.positions
                    and self.positions[sym].remaining_quantity <= 0):
                del self.positions[sym]

    def _generate_signals(self, day_str: str):
        short_pos = len([
            p for p in self.positions.values()
            if p.strategy in (StrategyType.SEPA, StrategyType.RSI2)
            and p.remaining_quantity > 0
        ])
        core_pos = len([
            p for p in self.positions.values()
            if p.strategy == StrategyType.CORE
            and p.remaining_quantity > 0
        ])

        pending_syms = {o['symbol'] for o in self.pending_buys}
        held_syms = {s for s, p in self.positions.items()
                     if p.remaining_quantity > 0}
        skip = pending_syms | held_syms

        regime = self.regime.get_regime(day_str)

        # SEPA / RSI2
        if short_pos < self.config.max_positions_short:
            signals = []
            for ticker in self.universe.tickers:
                if ticker in skip:
                    continue
                data = self.universe.get_data(ticker, day_str)
                if data is None:
                    continue

                if ("sepa" in self.config.strategies
                        and regime != RegimeType.BEARISH):
                    s = self.scorer.score_sepa(data)
                    if s >= self.config.sepa_min_score:
                        signals.append({
                            'symbol': ticker,
                            'strategy': StrategyType.SEPA,
                            'score': s,
                            'signal_close': float(data.get('종가', 0))
                        })

                if "rsi2" in self.config.strategies:
                    s = self.scorer.score_rsi2(data)
                    if s >= self.config.rsi2_min_score:
                        signals.append({
                            'symbol': ticker,
                            'strategy': StrategyType.RSI2,
                            'score': s,
                            'signal_close': float(data.get('종가', 0))
                        })

            signals.sort(key=lambda x: x['score'], reverse=True)
            available = self.config.max_positions_short - short_pos
            seen = set()
            for sig in signals:
                if sig['symbol'] not in seen and available > 0:
                    self.pending_buys.append(sig)
                    seen.add(sig['symbol'])
                    available -= 1

        # Core: 월 첫 영업일
        if ("core" in self.config.strategies
                and core_pos < self.config.max_positions_core):
            if self._is_month_start(day_str):
                core_sigs = []
                for ticker in self.universe.tickers:
                    if ticker in skip:
                        continue
                    data = self.universe.get_data(ticker, day_str)
                    if data is None:
                        continue
                    s = self.scorer.score_core(data)
                    if s >= self.config.core_min_score:
                        core_sigs.append({
                            'symbol': ticker,
                            'strategy': StrategyType.CORE,
                            'score': s,
                            'signal_close': float(data.get('종가', 0))
                        })
                core_sigs.sort(key=lambda x: x['score'], reverse=True)
                avail = self.config.max_positions_core - core_pos
                for sig in core_sigs[:avail]:
                    self.pending_buys.append(sig)

    def _is_month_start(self, day_str: str) -> bool:
        day_dt = pd.Timestamp(day_str)
        if day_dt.day > 5:
            return False
        sample = self.universe.tickers[0] if self.universe.tickers else None
        if sample and sample in self.universe.ohlcv:
            df = self.universe.ohlcv[sample]
            mask = df.index < day_dt
            if mask.any():
                return df.index[mask][-1].month != day_dt.month
        return day_dt.day <= 3

    def _calc_position_size(self, equity: float,
                            strategy: StrategyType) -> float:
        base = equity * self.config.base_position_pct / 100
        max_sz = equity * self.config.max_position_pct / 100
        min_reserve = equity * self.config.min_cash_reserve_pct / 100
        available = max(0, self.cash - min_reserve)
        pos_val = min(base, max_sz, available)

        if strategy == StrategyType.CORE:
            core_budget = equity * self.config.allocation.get("core", 0.30)
            core_per = core_budget / max(self.config.max_positions_core, 1)
            pos_val = min(core_per, available)

        return pos_val

    def _calc_equity(self, day_str: str) -> float:
        equity = self.cash
        for pos in self.positions.values():
            if pos.remaining_quantity <= 0:
                continue
            data = self.universe.get_data(pos.symbol, day_str)
            if data is not None:
                close = float(data.get('종가', pos.entry_price))
                equity += close * pos.remaining_quantity
            else:
                equity += pos.entry_price * pos.remaining_quantity
        return equity

    def _close_all(self, day_str: str):
        for symbol, pos in list(self.positions.items()):
            if pos.remaining_quantity <= 0:
                continue
            data = self.universe.get_data(symbol, day_str)
            close = (float(data.get('종가', pos.entry_price))
                     if data is not None else pos.entry_price)
            qty = pos.remaining_quantity
            sell_amount = close * qty
            sell_fee = self.fee.sell_fee(sell_amount)
            self.cash += sell_amount - sell_fee
            pnl, pnl_pct = self.fee.net_pnl(pos.entry_price, close, qty)
            self.trades.append(Trade(
                symbol=symbol, name=pos.name,
                strategy=pos.strategy.value,
                side="SELL", date=day_str, price=close,
                quantity=qty, amount=sell_amount, fee=sell_fee,
                reason="백테스트 종료 청산",
                pnl=pnl, pnl_pct=pnl_pct,
                holding_days=pos.holding_days
            ))
            pos.remaining_quantity = 0


# ─── 결과 분석 ─────────────────────────────────────────────
class ResultAnalyzer:
    def __init__(self, config: BacktestConfig, trades: List[Trade],
                 equity_curve: List[Tuple[str, float]]):
        self.config = config
        self.trades = trades
        self.equity_curve = equity_curve

    def print_results(self):
        if not self.equity_curve:
            print("데이터 없음")
            return

        initial = float(self.config.initial_capital)
        final = self.equity_curve[-1][1]
        total_ret = (final - initial) / initial * 100
        start_d = self.equity_curve[0][0]
        end_d = self.equity_curve[-1][0]

        days = (pd.Timestamp(end_d) - pd.Timestamp(start_d)).days
        years = days / 365.25 if days > 0 else 1
        cagr = ((final / initial) ** (1 / years) - 1) * 100

        mdd, mdd_s, mdd_e = self._calc_mdd()

        sells = [t for t in self.trades if t.side == "SELL"]
        wins = [t for t in sells if t.pnl > 0]
        losses = [t for t in sells if t.pnl <= 0]
        wr = len(wins) / len(sells) * 100 if sells else 0
        avg_w = np.mean([t.pnl_pct for t in wins]) if wins else 0
        avg_l = abs(np.mean([t.pnl_pct for t in losses])) if losses else 1
        pf = avg_w / avg_l if avg_l > 0 else float('inf')
        sharpe = self._calc_sharpe()
        total_fees = sum(t.fee for t in self.trades)

        print("\n" + "=" * 60)
        print(f" 백테스트 결과 ({start_d} ~ {end_d})")
        print("=" * 60)
        print(f"  초기 자본:    \u20a9{initial:>15,.0f}")
        print(f"  최종 자본:    \u20a9{final:>15,.0f}")
        print(f"  총 수익률:    {total_ret:>+14.2f}%")
        print(f"  CAGR:         {cagr:>14.1f}%")
        print(f"  MDD:          {mdd:>14.2f}%  ({mdd_s} ~ {mdd_e})")
        print(f"  Sharpe Ratio: {sharpe:>14.2f}")
        print(f"  승률:         {wr:>13.1f}% ({len(wins)}/{len(sells)})")
        print(f"  손익비:       {pf:>14.2f}")
        print(f"  총 거래 수:   {len(sells):>14d}")
        print(f"  총 수수료:    \u20a9{total_fees:>15,.0f}")

        print("\n-- 전략별 --")
        for strat in ["sepa", "rsi2", "core"]:
            st = [t for t in sells if t.strategy == strat]
            if not st:
                continue
            sw = [t for t in st if t.pnl > 0]
            s_pnl = sum(t.pnl for t in st)
            s_ret = s_pnl / initial * 100
            s_wr = len(sw) / len(st) * 100
            s_days = np.mean([t.holding_days for t in st])
            label = {"sepa": "SEPA", "rsi2": "RSI-2", "core": "Core"}[strat]
            print(f"  {label:>6}: 수익 {s_ret:>+6.1f}%, 승률 {s_wr:>4.0f}%, "
                  f"거래 {len(st):>3d}건, 평균 보유 {s_days:>.1f}일")

        print("\n-- 월별 수익률 --")
        for month, ret in self._monthly_returns().items():
            bar = "#" * min(int(abs(ret)), 30)
            print(f"  {month}: {ret:>+6.2f}% {bar}")

        print("=" * 60)

    def _calc_mdd(self) -> Tuple[float, str, str]:
        if not self.equity_curve:
            return 0.0, "", ""
        peak = self.equity_curve[0][1]
        mdd, mdd_s, mdd_e = 0.0, "", ""
        peak_date = self.equity_curve[0][0]
        for date, eq in self.equity_curve:
            if eq > peak:
                peak = eq
                peak_date = date
            dd = (eq - peak) / peak * 100
            if dd < mdd:
                mdd = dd
                mdd_s = peak_date
                mdd_e = date
        return mdd, mdd_s, mdd_e

    def _calc_sharpe(self, rf: float = 3.5) -> float:
        if len(self.equity_curve) < 2:
            return 0.0
        eqs = [e for _, e in self.equity_curve]
        rets = pd.Series(eqs).pct_change().dropna()
        if rets.std() == 0:
            return 0.0
        daily_rf = (1 + rf / 100) ** (1 / 252) - 1
        return (rets.mean() - daily_rf) / rets.std() * np.sqrt(252)

    def _monthly_returns(self) -> Dict[str, float]:
        if not self.equity_curve:
            return {}
        monthly = {}
        m_start = self.equity_curve[0][1]
        cur_m = self.equity_curve[0][0][:7]
        for date, eq in self.equity_curve:
            m = date[:7]
            if m != cur_m:
                monthly[cur_m] = (eq - m_start) / m_start * 100
                m_start = eq
                cur_m = m
        final = self.equity_curve[-1][1]
        monthly[cur_m] = (final - m_start) / m_start * 100
        return monthly

    def save_csv(self):
        RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")

        if self.trades:
            rows = [{
                '날짜': t.date, '종목코드': t.symbol, '종목명': t.name,
                '전략': t.strategy, '구분': t.side, '가격': t.price,
                '수량': t.quantity, '금액': t.amount, '수수료': round(t.fee, 0),
                '사유': t.reason, '손익': round(t.pnl, 0),
                '손익률': round(t.pnl_pct, 2), '보유일': t.holding_days
            } for t in self.trades]
            tf = RESULTS_DIR / f"trades_{ts}.csv"
            pd.DataFrame(rows).to_csv(tf, index=False, encoding='utf-8-sig')
            print(f"\n거래 내역: {tf}")

        if self.equity_curve:
            ef = RESULTS_DIR / f"equity_{ts}.csv"
            pd.DataFrame(self.equity_curve, columns=['날짜', '자산']).to_csv(
                ef, index=False, encoding='utf-8-sig')
            print(f"자산 추이: {ef}")

        sf = RESULTS_DIR / f"summary_{ts}.txt"
        with open(sf, 'w', encoding='utf-8') as f:
            f.write(f"백테스트 요약 ({ts})\n")
            f.write(f"기간: {self.config.months}개월\n")
            f.write(f"초기 자본: {self.config.initial_capital:,}\n")
            f.write(f"전략: {', '.join(self.config.strategies)}\n")
            f.write(f"유니버스: {self.config.universe_size}종목\n")
            sells = [t for t in self.trades if t.side == "SELL"]
            wins = [t for t in sells if t.pnl > 0]
            f.write(f"총 거래: {len(sells)}건\n")
            if sells:
                f.write(f"승률: {len(wins)/len(sells)*100:.1f}%\n")
            if self.equity_curve:
                final = self.equity_curve[-1][1]
                ret = (final - self.config.initial_capital)
                f.write(f"최종 자본: {final:,.0f}\n")
                f.write(f"총 수익률: {ret/self.config.initial_capital*100:+.2f}%\n")
        print(f"요약: {sf}")


# ─── 설정 로드 ─────────────────────────────────────────────
def load_config_from_yaml() -> dict:
    config_dir = PROJECT_ROOT / "config"
    result = {}
    default_f = config_dir / "default.yml"
    if default_f.exists():
        with open(default_f, 'r', encoding='utf-8') as f:
            result = yaml.safe_load(f) or {}

    override_f = config_dir / "evolved_overrides.yml"
    if override_f.exists():
        with open(override_f, 'r', encoding='utf-8') as f:
            overrides = yaml.safe_load(f) or {}
        if 'exit_manager' in overrides:
            em = result.get('kr', {}).get('exit_manager', {})
            em.update(overrides['exit_manager'])
        if 'risk_config' in overrides:
            rk = result.get('kr', {}).get('risk', {})
            rk.update(overrides['risk_config'])
        for strat in ['sepa_trend', 'rsi2_reversal']:
            if strat in overrides:
                st = result.get('kr', {}).get('strategies', {}).get(strat, {})
                st.update(overrides[strat])
    return result


def build_config(yaml_cfg: dict, args: argparse.Namespace) -> BacktestConfig:
    kr = yaml_cfg.get('kr', {})
    risk = kr.get('risk', {})
    em = kr.get('exit_manager', {})
    strats = kr.get('strategies', {})
    alloc_raw = risk.get('strategy_allocation', {})

    active = args.strategies.split(',')
    key_map = {'sepa': 'sepa_trend', 'rsi2': 'rsi2_reversal',
               'core': 'core_holding'}
    total_a = sum(alloc_raw.get(key_map.get(s, s), 25) for s in active)
    alloc = {}
    for s in active:
        alloc[s] = (alloc_raw.get(key_map.get(s, s), 25) / total_a
                    if total_a > 0 else 1 / len(active))

    sepa = strats.get('sepa_trend', {})
    rsi2 = strats.get('rsi2_reversal', {})
    core = strats.get('core_holding', {})

    return BacktestConfig(
        months=args.months,
        initial_capital=args.initial_capital,
        strategies=active,
        universe_size=args.universe_size,
        use_cache=not args.no_cache,
        use_t1=not args.no_t1,
        allocation=alloc,
        max_positions_short=(risk.get('max_positions', 8)
                             - core.get('max_positions', 3)),
        max_positions_core=core.get('max_positions', 3),
        base_position_pct=risk.get('base_position_pct', 25.0),
        max_position_pct=risk.get('max_position_pct', 28.0),
        min_cash_reserve_pct=risk.get('min_cash_reserve_pct', 5.0),
        daily_max_loss_pct=risk.get('daily_max_loss_pct', 5.0),
        min_position_value=risk.get('min_position_value', 200_000),
        sepa_min_score=sepa.get('min_score', 60.0),
        sepa_stop_loss_pct=sepa.get('stop_loss_pct', 5.0),
        sepa_max_holding_days=sepa.get('max_holding_days', 10),
        rsi2_min_score=rsi2.get('min_score', 60.0),
        rsi2_stop_loss_pct=rsi2.get('stop_loss_pct', 5.0),
        rsi2_max_holding_days=rsi2.get('max_holding_days', 10),
        core_min_score=core.get('min_score', 70.0),
        core_stop_loss_pct=core.get('stop_loss_pct', 15.0),
        core_trailing_stop_pct=core.get('trailing_stop_pct', 8.0),
        core_trailing_activate_pct=core.get('trailing_activate_pct', 10.0),
        first_exit_pct=em.get('first_exit_pct', 5.0),
        first_exit_ratio=em.get('first_exit_ratio', 0.30),
        second_exit_pct=em.get('second_exit_pct', 15.0),
        second_exit_ratio=em.get('second_exit_ratio', 0.50),
        third_exit_pct=em.get('third_exit_pct', 25.0),
        third_exit_ratio=em.get('third_exit_ratio', 0.50),
        trailing_stop_pct=em.get('trailing_stop_pct', 3.0),
        trailing_activate_pct=em.get('trailing_activate_pct', 5.0),
        atr_multiplier=em.get('atr_multiplier', 2.0),
        min_stop_pct=em.get('min_stop_pct', 3.5),
        max_stop_pct=em.get('max_stop_pct', 6.0),
        stale_exit_days=em.get('stale_exit_days', 10),
        stale_exit_pnl_pct=em.get('stale_exit_pnl_pct', 2.0),
        stale_high_days=em.get('stale_high_days', 7),
        stale_high_min_pnl_pct=em.get('stale_high_min_pnl_pct', 1.0),
    )


# ─── CLI ───────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="KR 전략 백테스트 엔진")
    parser.add_argument('--months', type=int, default=6,
                        help='백테스트 기간 (월, 기본 6)')
    parser.add_argument('--initial-capital', type=int, default=10_000_000,
                        help='초기 자본 (원, 기본 1000만)')
    parser.add_argument('--strategies', type=str, default='sepa,rsi2,core',
                        help='실행 전략 (sepa,rsi2,core)')
    parser.add_argument('--universe-size', type=int, default=150,
                        help='유니버스 종목 수 (기본 150)')
    parser.add_argument('--no-cache', action='store_true',
                        help='OHLCV 캐시 무시')
    parser.add_argument('--no-t1', action='store_true',
                        help='T+1 비활성화 (당일 종가 체결)')
    args = parser.parse_args()

    yaml_cfg = load_config_from_yaml()
    config = build_config(yaml_cfg, args)

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    if not config.use_cache:
        import shutil
        shutil.rmtree(CACHE_DIR, ignore_errors=True)
        CACHE_DIR.mkdir(parents=True, exist_ok=True)

    engine = BacktestEngine(config)
    t0 = time.time()
    engine.run()
    print(f"\n실행 시간: {time.time() - t0:.1f}초")


if __name__ == "__main__":
    main()
