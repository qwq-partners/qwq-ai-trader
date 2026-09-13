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
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from pykrx import stock as pykrx_stock

from src.utils.fee_calculator import get_fee_calculator
from src.utils.sizing import risk_quantity_cap

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
    # ATR 연동 트레일링 (실제 엔진 exit_manager.py와 동일 공식)
    #   effective_ts = min( max(trailing_stop_pct, ATR% × atr_link_multiplier), atr_link_cap_pct )
    enable_atr_linked_trailing: bool = True
    atr_link_multiplier: float = 1.2
    atr_link_cap_pct: float = 6.0
    # 레짐별 청산 파라미터 적용 (실제 엔진 REGIME_EXIT_PARAMS 대응)
    enable_regime_exit: bool = True
    # 레짐 판단 방식: "ma_stack"(MA20>60>200 정배열) | "ret20" | "ret60" | "ma200"
    regime_mode: str = "ma_stack"
    # ATR 동적 손절
    atr_multiplier: float = 2.0
    min_stop_pct: float = 3.5
    max_stop_pct: float = 6.0
    # 조기 청산
    stale_exit_days: int = 10
    stale_exit_pnl_pct: float = 2.0
    stale_high_days: int = 7
    stale_high_min_pnl_pct: float = 1.0
    # ── A/B 축 (2026-09 청산 정책 리뷰, docs/research/exit-policy-ab-2026-09.md) ──
    #   기본값은 전부 기존 동작(ladder / nominal, 보유 규칙 미변경)을 유지한다 — 게이트 호환.
    exit_policy: str = "ladder"       # ladder(분할익절+본전+트레일링) | channel(ATR 하드스톱+10/20일 저가 채널)
    sizing: str = "nominal"           # nominal(base_position_pct) | risk(equity×risk_per_trade_pct / stop_pct)
    risk_per_trade_pct: float = 0.7
    risk_max_position_pct: float = 18.0
    risk_max_positions: int = 7       # sizing=risk 일 때 단기 전략 동시 보유 상한
    min_holding_days: int = 0         # 이 보유일 전엔 손절(채널 이탈 포함) 외 청산 금지
    # ladder 실엔진 미러 (기본 off — 게이트 기본 동작 보존). A/B 러너가 켠다.
    enable_composite_exit: bool = False   # 1차 익절 후 MA5-0.5% / 전일저가 이탈 → 전량 청산
    post_exit_stale_days: int = 0         # 1차 익절 후 N일 보유 & 0<수익<post_exit_stale_pnl_pct → 청산 (0=off)
    post_exit_stale_pnl_pct: float = 3.0
    # 신규 진입 초기 손절 정책 (2026-09 T5). 기본값은 기존 동작(게이트 호환) — A/B 러너·T6 가 live_policy 를 명시.
    #   atr_dynamic — 진입 ATR×atr_multiplier(min~max 클램프), 이후 매일 당일 ATR 로 손절 재계산 (연구 축)
    #   live_policy — 전략별 고정 SL(sepa/rsi2/core_stop_loss_pct)만, ATR 동적 손절 없음 (실엔진 신규 fill 미러, T2 정합)
    #                 이후 손절 변경은 레짐 전환(BT_REGIME_EXIT_PARAMS, 실엔진 apply_regime_params 미러)뿐
    entry_stop_mode: str = "atr_dynamic"
    # 동시 보유 슬롯 정책 (2026-09-14 T6 parity)
    #   fixed        — 기존 동작: 단순 포지션 수 ≤ max_positions_short(또는 risk_max_positions)
    #   live_weighted— 실엔진 미러: 비코어 포지션의 잔여비율·익절단계 가중 합 ≤ max_positions_weighted
    #                  (src/risk/manager.py::_get_position_weight, docs/risk/risk-and-exit.md)
    slot_policy: str = "fixed"
    max_positions_weighted: float = 8.0
    # 실행 환경 (T7-A). offline=True 면 캐시에 없는 입력을 다운로드하지 않고 데이터 부족으로 종료한다.
    offline: bool = False
    end_date: Optional[str] = None    # "YYYY-MM-DD" (미지정 시 실행 시각)
    # 유효 설정에서 만들어질 때 기록되는 지원 범위 (게이트·manifest 가 그대로 보고)
    supported_scope: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        # 어디서 생성되든(엔진·BTExitManager 직접 생성·dataclasses.replace) 한 곳에서 검증 (2026-09-14 리뷰 P2)
        if self.entry_stop_mode not in ENTRY_STOP_MODES:
            raise ValueError(f"entry_stop_mode 는 {ENTRY_STOP_MODES} 중 하나: {self.entry_stop_mode!r}")
        if self.slot_policy not in SLOT_POLICIES:
            raise ValueError(f"slot_policy 는 {SLOT_POLICIES} 중 하나: {self.slot_policy!r}")


ENTRY_STOP_MODES = ("atr_dynamic", "live_policy")
SLOT_POLICIES = ("fixed", "live_weighted")

# 백테스트 계산기 버전 — 결과 manifest 재현 키. 체결·사이징·청산 계산이 바뀌면 올린다.
CALC_VERSION = "2026-09-14.t6"


class BacktestDataUnavailable(RuntimeError):
    """오프라인 실행에 필요한 입력(OHLCV·레짐 캐시)이 없음 — 다운로드로 자동 전환하지 않는다."""


class UnsupportedBacktestConfig(ValueError):
    """실운영 유효 설정 중 백테스터가 모사할 수 없는 항목 — 운영 게이트는 보류(passed=False)."""


def live_slot_weight(remaining: int, original: int, stage: str) -> float:
    """실엔진 슬롯 가중치 미러 (src/risk/manager.py::_get_position_weight).

    weight = 잔여/원본, TRAILING ×0.5(floor 0.1), SECOND·THIRD ×0.7(floor 0.15), 그 외 floor 0.2.
    """
    orig = max(int(original), 1)
    remain = max(int(remaining), 0)
    weight = remain / orig
    if stage == "trailing":
        return max(0.1, min(1.0, weight * 0.5))
    if stage in ("second", "third"):
        return max(0.15, min(1.0, weight * 0.7))
    return max(0.2, min(1.0, weight))


# 보유(회전) 정책 프리셋 — 리뷰 §2-1 실엔진 값(current) vs 권고 2(extended).
HOLDING_POLICIES: Dict[str, Dict[str, Any]] = {
    "current": dict(stale_exit_days=5, stale_exit_pnl_pct=2.0,
                    stale_high_days=3, stale_high_min_pnl_pct=3.0,
                    sepa_max_holding_days=10, rsi2_max_holding_days=10,
                    min_holding_days=0),
    "extended": dict(stale_exit_days=10, stale_exit_pnl_pct=3.0,
                     stale_high_days=10, stale_high_min_pnl_pct=3.0,
                     sepa_max_holding_days=20, rsi2_max_holding_days=20,
                     min_holding_days=2),
    # 보충 실험용: 보유 규칙 전부 해제 (청산 정책 자체의 효과 격리 — G2 비교와 같은 조건)
    "none": dict(stale_exit_days=999, stale_exit_pnl_pct=0.0,
                 stale_high_days=999, stale_high_min_pnl_pct=0.0,
                 sepa_max_holding_days=999, rsi2_max_holding_days=999,
                 min_holding_days=2),
}


def apply_holding_policy(cfg: "BacktestConfig", name: str) -> "BacktestConfig":
    for k, v in HOLDING_POLICIES[name].items():
        setattr(cfg, k, v)
    return cfg


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
    atr_stop_pct: float = 5.0     # 진입 시 확정한 초기 손절폭(%) — R 분모, 체결 후 불변 (매일 ATR 재계산으로 소급 변경 금지)
    atr_pct: float = 0.0          # 진입 시점 ATR%(원본, 체결일 이전 확정 봉) — ATR 연동 트레일링 계산용
    initial_risk: float = 0.0     # 초기 위험금액(원) = cost_basis × atr_stop_pct/100, 체결 후 불변
    live_stop_pct: float = 0.0    # live_policy: 레짐 전환으로 바뀐 현재 손절폭 (0 = 진입 손절 그대로)
    regime_seen: Optional[RegimeType] = None   # live_policy: 마지막으로 관측한 레짐 (전환 감지용)
    score_at_entry: float = 0.0
    trailing_activated: bool = False
    breakeven_activated: bool = False
    holding_days: int = 0
    days_since_high: int = 0
    stop_price: float = 0.0       # channel: 하드스톱 → 채널 승급 (진입 시 초기화)
    runner: bool = False          # channel: +30% 도달 후 20일 채널

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
    stop_pct: float = 0.0
    initial_risk: float = 0.0     # BUY: 진입 시 확정한 위험금액(원)


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

        # 채널 청산용 선행 N일 저가(당일 제외) + 전일 저가(복합 청산 미러)
        df['low10'] = low.rolling(10).min().shift(1)
        df['low20'] = low.rolling(20).min().shift(1)
        df['prev_low'] = low.shift(1)

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
    def __init__(self, offline: bool = False):
        self.kospi_data: Optional[pd.DataFrame] = None
        self.offline = offline
        self.cache_files: List[Path] = []

    def load(self, start: str, end: str):
        """KOSPI 지수로 레짐 판단 (pykrx → FDR → 개별주 대리 순 폴백).

        offline=True 면 캐시(regime_{start}_{end}.pkl)만 쓰고, 없으면 데이터 부족으로 종료한다
        (레짐 미로드는 NEUTRAL 고정으로 결과를 조용히 바꾸므로 오프라인에선 허용하지 않는다).
        """
        print("  시장 레짐 지표 로드 중...")
        cache_file = CACHE_DIR / f"regime_{start}_{end}.pkl"
        if cache_file.exists():
            try:
                with open(cache_file, "rb") as f:
                    self.kospi_data = pickle.load(f)
                self.cache_files.append(cache_file)
                print(f"  레짐 지표 캐시 {len(self.kospi_data)}일 사용")
                return
            except Exception:
                pass
        if self.offline:
            raise BacktestDataUnavailable(
                f"레짐 지표 캐시 없음: {cache_file} — offline 실행 종료 (다운로드 금지)")
        df = None
        # 1차: KOSPI 지수 (pykrx) — KRX 인증이 없으면 자주 실패한다
        try:
            df = pykrx_stock.get_index_ohlcv_by_date(start, end, "1001")
        except Exception:
            pass

        # 2차: FinanceDataReader KOSPI (KS11) — 인증 불필요
        #   pykrx 지수 조회는 KRX_ID/KRX_PW가 없으면 실패하므로 이 경로가 사실상 주력이다.
        if df is None or len(df) < 20:
            try:
                import FinanceDataReader as fdr
                s = f"{start[:4]}-{start[4:6]}-{start[6:]}"
                e = f"{end[:4]}-{end[4:6]}-{end[6:]}"
                fdf = fdr.DataReader("KS11", s, e)
                if fdf is not None and not fdf.empty:
                    df = fdf.rename(columns={"Close": "종가", "Open": "시가",
                                             "High": "고가", "Low": "저가",
                                             "Volume": "거래량"})
                    print("  KOSPI 지수(pykrx) 실패 → FDR KS11 사용")
            except Exception:
                pass

        # 3차: 삼성전자 OHLCV (최후 폴백)
        #   ⚠️ 개별주는 지수와 크게 괴리될 수 있다. 실제로 2026-05~08 구간에서
        #      KOSPI -23%인데 삼성전자 +393%라 레짐이 100% bullish로 왜곡된 사례가 있었다.
        #      이 경로로 떨어지면 레짐 기반 판단을 신뢰하지 말 것.
        if df is None or len(df) < 20:
            try:
                df = pykrx_stock.get_market_ohlcv_by_date(
                    start, end, "005930")
                if df is not None and len(df) > 0:
                    print("  ⚠️ KOSPI 지수·FDR 모두 실패 → 삼성전자 대리 사용 "
                          "(레짐 판단 신뢰도 낮음)")
            except Exception:
                pass

        if df is not None and len(df) > 0:
            c = df['종가'].astype(float)
            df['ma20'] = c.rolling(20).mean()
            df['ma60'] = c.rolling(60).mean()
            df['ma200'] = c.rolling(200).mean()
            self.kospi_data = df
            try:
                CACHE_DIR.mkdir(parents=True, exist_ok=True)
                with open(cache_file, "wb") as f:
                    pickle.dump(df, f)
                self.cache_files.append(cache_file)
            except Exception:
                pass
            print(f"  레짐 지표 {len(df)}일 로드 완료")
        else:
            print("  레짐 지표 로드 실패 — NEUTRAL 고정")

    def get_regime(self, date: str, mode: str = "ma_stack") -> RegimeType:
        """
        시장 레짐 판단.

        mode:
          ma_stack — MA20>MA60>MA200 정배열 (중장기 추세, 기본)
          ma200    — 종가의 MA200 상/하 (가장 단순·느림)
          ret20    — 최근 20영업일 수익률 (단기)
          ret60    — 최근 60영업일 수익률 (중기)

        기간이 짧을수록 반응은 빠르지만 whipsaw(잦은 전환)가 늘어난다.
        어느 쪽이 나은지는 시장에 따라 다르므로 백테스트로 고른다.
        """
        if self.kospi_data is None:
            return RegimeType.NEUTRAL

        if mode in ("ret20", "ret60"):
            window = 20 if mode == "ret20" else 60
            try:
                mask = self.kospi_data.index <= pd.Timestamp(date)
                hist = self.kospi_data.loc[mask, '종가'].astype(float)
                if len(hist) < window + 1:
                    return RegimeType.NEUTRAL
                ret = (hist.iloc[-1] - hist.iloc[-window - 1]) / hist.iloc[-window - 1] * 100
                # ±5% 밴드 — 이보다 작은 움직임은 추세로 보지 않는다
                if ret >= 5.0:
                    return RegimeType.BULLISH
                if ret <= -5.0:
                    return RegimeType.BEARISH
                return RegimeType.NEUTRAL
            except (KeyError, IndexError):
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

        if mode == "ma200":
            return RegimeType.BULLISH if close > ma200 else RegimeType.BEARISH

        if close > ma20 > ma60 > ma200:
            return RegimeType.BULLISH
        elif close < ma200 and ma20 < ma60:
            return RegimeType.BEARISH
        return RegimeType.NEUTRAL


# ─── 유니버스 관리 ──────────────────────────────────────────
class UniverseManager:
    def __init__(self, size: int = 150, use_cache: bool = True, offline: bool = False):
        self.size = size
        self.use_cache = use_cache
        self.offline = offline          # True면 네트워크 호출 없이 캐시·하드코딩 유니버스만 사용
        self.tickers: List[str] = []
        self.names: Dict[str, str] = {}
        self.ohlcv: Dict[str, pd.DataFrame] = {}
        self.cache_files: List[Path] = []

    def build_universe(self, ref_date: str):
        print(f"\n유니버스 구성 (기준일: {ref_date})...")
        kospi_n = int(self.size * 2 / 3)
        kosdaq_n = self.size - kospi_n
        tickers = []

        if self.offline:
            # 오프라인: 고정 유니버스(하드코딩)만 사용 — 시총 조회·종목명 조회 모두 네트워크다
            self.tickers = (DEFAULT_UNIVERSE["KOSPI"][:kospi_n]
                            + DEFAULT_UNIVERSE["KOSDAQ"][:kosdaq_n])[:self.size]
            self.names = {t: t for t in self.tickers}
            print(f"  offline: 고정 유니버스 {len(self.tickers)}종목")
            return

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
                        self.cache_files.append(cache_file)
                        cached += 1
                        continue
                except Exception:
                    pass

            if self.offline:
                failed += 1
                continue

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

        if self.offline and cached == 0:
            raise BacktestDataUnavailable(
                f"offline 실행에 쓸 OHLCV 캐시 없음 ({CACHE_DIR}, {start}~{end}, "
                f"{len(self.tickers)}종목) — 다운로드로 전환하지 않고 종료")

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

    def get_last_bar_before(self, ticker: str, date: str) -> Optional[pd.Series]:
        """date 보다 앞선 마지막 확정 봉 (당일 제외 — 시가 체결 시점에 알려진 정보)"""
        df = self.ohlcv.get(ticker)
        if df is None:
            return None
        mask = df.index < pd.Timestamp(date)
        if not mask.any():
            return None
        return df.loc[mask].iloc[-1]

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
# 레짐별 청산 파라미터 — src/strategies/exit_manager.py REGIME_EXIT_PARAMS와 동일해야 한다.
# 백테스트 RegimeType(3종)을 실제 엔진 레짐(5종)에 매핑:
#   BULLISH → trending_bull / NEUTRAL → neutral / BEARISH → trending_bear
# ⚠️ 한쪽을 고치면 반드시 다른 쪽도 고칠 것 (게이트 판정이 어긋난다).
BT_REGIME_EXIT_PARAMS: Dict[RegimeType, Dict] = {
    RegimeType.BULLISH: {   # ↔ trending_bull
        "first_exit_pct": 10.0, "second_exit_pct": 15.0, "third_exit_pct": 25.0,
        "trailing_stop_pct": 4.0, "stop_loss_pct": 5.0,
    },
    RegimeType.NEUTRAL: {   # ↔ neutral
        "first_exit_pct": 10.0, "second_exit_pct": 12.0, "third_exit_pct": 20.0,
        "trailing_stop_pct": 3.0, "stop_loss_pct": 4.0,
    },
    RegimeType.BEARISH: {   # ↔ trending_bear
        "first_exit_pct": 5.0, "second_exit_pct": 8.0, "third_exit_pct": 14.0,
        "trailing_stop_pct": 2.0, "stop_loss_pct": 3.5,
    },
}


def channel_exit(pos: BTPosition, row: pd.Series) -> Optional[Tuple[float, str]]:
    """채널 청산 1봉 판정 — src/strategies/harvest_shadow.exit_step 과 동일 의미 (EOD 근사).

    하드스톱 pos.stop_price 는 진입 시 ATR×2(min/max_stop_pct 클램프)로 고정. 갭 관통이면 시가,
    저가 관통이면 스탑가 체결. +30% 도달 후엔 20일 저가 채널로 승격(runner). 채널(선행 N일 저가,
    당일 제외)을 종가가 하향 이탈하면 종가 청산, 아니면 채널×0.999로 스탑 승급.
    분할 익절·본전보호·복합 MA5·고정 트레일링 없음. 반환: (청산가, 사유) 또는 None.
    """
    entry = pos.entry_price
    stop = pos.stop_price
    o, h, lo, c = (float(row['시가']), float(row['고가']),
                   float(row['저가']), float(row['종가']))
    if o <= stop:
        return o, "손절(갭관통)"
    if lo <= stop:
        return stop, "손절"
    if h >= entry * 1.30:
        pos.runner = True
    ch = row.get('low20' if pos.runner else 'low10')
    if ch is not None and not pd.isna(ch):
        ch = max(float(ch), stop)
        if c < ch:
            return c, f"채널이탈 ({20 if pos.runner else 10}일 저가 {ch:,.0f})"
        pos.stop_price = max(stop, ch * 0.999)
    return None


class BTExitManager:
    def __init__(self, config: BacktestConfig):
        self.config = config
        self.fee = BTFeeCalculator()

    def _holding_rules(self, pos: BTPosition, close: float,
                       actions: List[Tuple[str, int, float, str]]):
        """보유(회전) 규칙 — 최대 보유일 / 횡보 / 신고가 실패. ladder·channel 공통."""
        entry = pos.entry_price
        # ── 5. 최대 보유일 (SEPA/RSI2) ──
        max_days = (self.config.sepa_max_holding_days
                    if pos.strategy == StrategyType.SEPA
                    else self.config.rsi2_max_holding_days)
        if pos.holding_days >= max_days:
            actions.append(("SELL", pos.remaining_quantity, close,
                            f"보유기간 초과 ({pos.holding_days}일)"))
            pos.remaining_quantity = 0
            return actions

        # ── 6. 횡보 청산 ──
        if pos.holding_days >= self.config.stale_exit_days:
            _, pnl_pct = self.fee.net_pnl(entry, close, 1)
            if abs(pnl_pct) < self.config.stale_exit_pnl_pct:
                actions.append(("SELL", pos.remaining_quantity, close,
                                f"횡보 청산 ({pos.holding_days}일)"))
                pos.remaining_quantity = 0
                return actions

        # ── 7. 추세 무효화 (신고가 갱신 실패) ──
        if pos.days_since_high >= self.config.stale_high_days:
            _, pnl_pct = self.fee.net_pnl(entry, close, 1)
            if pnl_pct < self.config.stale_high_min_pnl_pct:
                actions.append(("SELL", pos.remaining_quantity, close,
                                f"추세 무효화 ({pos.days_since_high}일 "
                                f"신고가 없음)"))
                pos.remaining_quantity = 0
                return actions
        return actions

    def check_exit(
        self, pos: BTPosition, row: pd.Series,
        regime: Optional["RegimeType"] = None,
    ) -> List[Tuple[str, int, float, str]]:
        """일봉 데이터로 청산 체크 → [(action, qty, price, reason)]

        Args:
            regime: 당일 시장 레짐. 실제 엔진의 REGIME_EXIT_PARAMS와 동일하게
                    익절/트레일링/손절 목표를 레짐별로 바꾼다 (None이면 config 기본값).

        체결 순서 규칙 (일봉으로는 장중 순서를 알 수 없으므로 보수적으로 고정, 전 셀 동일 적용):
          ① 손절 우선 — 같은 봉에서 익절 목표와 손절가를 모두 접촉하면 손절로 전량 청산.
          ② 갭 관통 — 시가 ≤ 손절가면 손절가가 아닌 **시가** 체결 (channel_exit 와 동일).
             그 외 저가 ≤ 손절가면 손절가 체결.
          ③ 손절 미접촉 시 본전보호 → 분할 익절(봉당 한 단계) → 트레일링 → 복합 청산 →
             익절후 저효율 → 보유 규칙(최대 보유일·횡보·추세 무효화) 순, 모두 종가 체결.
          ④ 손절폭: entry_stop_mode=atr_dynamic 은 당일 ATR 로 재계산(연구 축), live_policy 는
             진입 시 확정한 고정 SL(레짐 전환 시에만 상태 전이). 어느 쪽도 pos.atr_stop_pct
             (진입 초기 손절·R 분모)를 다시 쓰지 않는다.
        """
        actions = []
        open_ = float(row['시가'])
        high = float(row['고가'])
        low = float(row['저가'])
        close = float(row['종가'])

        if pos.remaining_quantity <= 0:
            return actions

        entry = pos.entry_price
        is_core = pos.strategy == StrategyType.CORE

        # 레짐별 청산 파라미터 (실제 exit_manager.REGIME_EXIT_PARAMS와 동일 값)
        # 코어홀딩은 실제 엔진과 마찬가지로 레짐 보정을 받지 않는다.
        rp = None
        if regime is not None and not is_core and self.config.enable_regime_exit:
            rp = BT_REGIME_EXIT_PARAMS.get(regime)

        # 최고가 갱신
        if high > pos.highest_price:
            pos.highest_price = high
            pos.days_since_high = 0
        else:
            pos.days_since_high += 1

        pos.holding_days += 1

        # ── channel 정책: 하드스톱→채널 트레일링만 (사다리·본전·복합·고정 트레일링 없음) ──
        if self.config.exit_policy == "channel" and not is_core:
            hit = channel_exit(pos, row)
            if hit is not None:
                actions.append(("SELL", pos.remaining_quantity, hit[0], hit[1]))
                pos.remaining_quantity = 0
                return actions
            if pos.holding_days < self.config.min_holding_days:
                return actions
            return self._holding_rules(pos, close, actions)

        # ── 1. 손절 (저가 기준) ──
        if is_core:
            stop_pct = self.config.core_stop_loss_pct
        elif self.config.entry_stop_mode == "live_policy":
            # 실엔진 신규 fill 미러: 진입 시 확정한 고정 SL. 이후 변경은 레짐 전환 상태 전이뿐
            # (apply_regime_params — 레짐이 바뀌면 기존 포지션 SL 을 레짐값으로 덮어씀).
            if rp is not None and regime != pos.regime_seen:
                if pos.regime_seen is not None:
                    pos.live_stop_pct = rp["stop_loss_pct"]
                pos.regime_seen = regime
            stop_pct = pos.live_stop_pct if pos.live_stop_pct > 0 else pos.atr_stop_pct
        else:
            # atr_dynamic(연구 축): 당일 ATR 로 재계산. 진입 초기 손절(pos.atr_stop_pct)은 다시 쓰지 않는다.
            atr_pct = row.get('atr_pct')
            if not pd.isna(atr_pct) and float(atr_pct) > 0:
                stop_pct = max(
                    self.config.min_stop_pct,
                    min(self.config.max_stop_pct,
                        float(atr_pct) * self.config.atr_multiplier)
                )
            elif rp:
                stop_pct = rp["stop_loss_pct"]
            else:
                stop_pct = (self.config.sepa_stop_loss_pct
                            if pos.strategy == StrategyType.SEPA
                            else self.config.rsi2_stop_loss_pct)

        stop_price = entry * (1 - stop_pct / 100)
        if low <= stop_price:
            # 갭 관통(시가 ≤ 손절가)이면 시가 체결 — 시장에 없던 손절가 체결 금지 (보수 규칙 ②)
            sell_price = min(open_, stop_price) if open_ > 0 else stop_price
            actions.append(("SELL", pos.remaining_quantity, sell_price,
                            f"손절 -{stop_pct:.1f}%"))
            pos.remaining_quantity = 0
            return actions

        # 손절 외 청산 금지 구간 (holding_policy=extended: 2일 전 청산 금지)
        if pos.holding_days < self.config.min_holding_days:
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

            # 레짐 보정된 익절 목표 (rp가 None이면 config 기본값)
            first_pct = rp["first_exit_pct"] if rp else self.config.first_exit_pct
            second_pct = rp["second_exit_pct"] if rp else self.config.second_exit_pct
            third_pct = rp["third_exit_pct"] if rp else self.config.third_exit_pct

            if (pos.exit_stage == ExitStage.NONE
                    and high_pnl >= first_pct):
                qty = max(1, int(pos.remaining_quantity
                                 * self.config.first_exit_ratio))
                actions.append(("SELL", qty, close,
                                f"1차 익절 +{first_pct}%"))
                pos.remaining_quantity -= qty
                pos.exit_stage = ExitStage.FIRST
                pos.breakeven_activated = True

            elif (pos.exit_stage == ExitStage.FIRST
                  and high_pnl >= second_pct):
                qty = max(1, int(pos.remaining_quantity
                                 * self.config.second_exit_ratio))
                actions.append(("SELL", qty, close,
                                f"2차 익절 +{second_pct}%"))
                pos.remaining_quantity -= qty
                pos.exit_stage = ExitStage.SECOND

            elif (pos.exit_stage == ExitStage.SECOND
                  and high_pnl >= third_pct):
                qty = max(1, int(pos.remaining_quantity
                                 * self.config.third_exit_ratio))
                actions.append(("SELL", qty, close,
                                f"3차 익절 +{third_pct}%"))
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
            # 레짐 보정 트레일링 (실제 엔진은 REGIME_EXIT_PARAMS가 config_ts를 대체)
            trail_pct = rp["trailing_stop_pct"] if rp else self.config.trailing_stop_pct
            trail_activate = self.config.trailing_activate_pct

            # ATR 연동 트레일링 — 실제 엔진(exit_manager.py:490~512)과 동일한 공식.
            #   effective_ts = min( max(config_ts, ATR% × mult), cap )
            # 고정 트레일링만 쓰면 변동성 큰 종목이 노이즈에 털려
            # 백테스트가 실제보다 비관적으로 나온다 (코어홀딩은 실제와 동일하게 제외).
            if self.config.enable_atr_linked_trailing and pos.atr_pct > 0:
                atr_based = pos.atr_pct * self.config.atr_link_multiplier
                trail_pct = min(max(trail_pct, atr_based),
                                self.config.atr_link_cap_pct)

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

        if is_core:
            return actions

        # ── 4b. 복합 청산 (실엔진 _check_composite_trailing 미러, 1차 익절 이후) ──
        if (self.config.enable_composite_exit
                and pos.exit_stage != ExitStage.NONE
                and pos.remaining_quantity > 0):
            ma5 = row.get('ma5')
            prev_low = row.get('prev_low')
            reason = None
            if ma5 is not None and not pd.isna(ma5) and close < float(ma5) * 0.995:
                reason = f"복합청산 MA5({float(ma5):,.0f})-0.5% 이탈"
            elif (prev_low is not None and not pd.isna(prev_low)
                  and low < float(prev_low) and close < float(prev_low)):
                reason = f"복합청산 전일저가({float(prev_low):,.0f}) 이탈"
            if reason:
                actions.append(("SELL", pos.remaining_quantity, close, reason))
                pos.remaining_quantity = 0
                return actions

        # ── 4c. 익절 후 저효율 (실엔진: 1차 익절 후 N일 & 0<수익<X% & 신고가 3일 실패) ──
        if (self.config.post_exit_stale_days > 0
                and pos.exit_stage in (ExitStage.FIRST, ExitStage.SECOND)
                and pos.holding_days >= self.config.post_exit_stale_days
                and pos.days_since_high >= 3):
            _, pnl_pct = self.fee.net_pnl(entry, close, 1)
            if 0 < pnl_pct < self.config.post_exit_stale_pnl_pct:
                actions.append(("SELL", pos.remaining_quantity, close,
                                f"익절후 저효율 ({pos.holding_days}일, {pnl_pct:+.1f}%)"))
                pos.remaining_quantity = 0
                return actions

        return self._holding_rules(pos, close, actions)


# ─── 백테스트 엔진 ─────────────────────────────────────────
class BacktestEngine:
    def __init__(self, config: BacktestConfig):
        self.config = config
        self.universe = UniverseManager(config.universe_size, config.use_cache,
                                        offline=config.offline)
        self.scorer = StrategyScorer(config)
        self.exit_mgr = BTExitManager(config)
        self.regime = MarketRegime(offline=config.offline)
        self.fee = BTFeeCalculator()
        # entry_stop_mode·slot_policy 검증은 BacktestConfig.__post_init__ (한 곳)
        # fixed: sizing=risk 는 종목당 위험이 작아 동시 보유 상한을 별도로 둔다 (리뷰 권고 3)
        # live_weighted: 실엔진과 같은 잔여비율 가중 슬롯 (사이징 방식과 무관하게 동일 상한)
        self.max_short: float = (
            config.max_positions_weighted if config.slot_policy == "live_weighted"
            else (config.risk_max_positions if config.sizing == "risk"
                  else config.max_positions_short))

        self.cash: float = float(config.initial_capital)
        self.positions: Dict[str, BTPosition] = {}
        self.trades: List[Trade] = []
        self.equity_curve: List[Tuple[str, float]] = []
        self.pending_buys: List[dict] = []
        self.daily_loss_locked: bool = False
        self.peak_equity: float = float(config.initial_capital)

    def run(self, save_results: bool = True) -> Dict[str, Any]:
        """
        백테스트 실행.

        Args:
            save_results: results/ 에 CSV 저장 (게이트 호출 시 False)

        Returns:
            ResultAnalyzer.metrics() dict (데이터 없으면 빈 dict)

        stdout 출력을 억제하려면 호출측에서 contextlib.redirect_stdout을 쓴다
        (진화 게이트가 그렇게 사용).
        """
        end_date = (datetime.strptime(self.config.end_date, "%Y-%m-%d")
                    if self.config.end_date else datetime.now())
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
            return {}

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
        if save_results:
            analyzer.save_csv()
        return analyzer.metrics()

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
        """T+1 대기 주문 실행 (시가 체결). use_close=True 는 T+0 당일 종가 체결.

        정보 시점 규칙 (2026-09 T5, F2·F8):
          - 시가 체결에 쓰는 정보는 체결일 시가까지 알려진 것만: 진입 ATR 은 체결일 **이전** 확정 봉
            (`_entry_atr`), 사이징 equity 는 시가 평가(`_calc_equity(phase="open")`).
          - 체결일 봉이 없는 종목(거래정지·누락봉)은 체결하지 않는다 (이전 봉 시가 대체 금지).
          - 주문의 signal_date·indicator_asof 는 체결일보다 앞서야 한다 (T+0 종가 체결은 당일 허용).
          - 진입 시 초기 손절·위험금액(R 분모)을 포지션에 고정한다.
        """
        if not self.pending_buys:
            return

        # 포지션 수 카운트 (live_weighted 는 잔여비율·익절단계 가중 합 — 실엔진 미러)
        short_count = self._short_slot_usage()
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
                if short_count >= self.max_short:
                    continue
            elif strategy == StrategyType.CORE:
                if core_count >= self.config.max_positions_core:
                    continue

            # 이미 보유 중
            if (symbol in self.positions
                    and self.positions[symbol].remaining_quantity > 0):
                continue

            # 정보 시점 검사 — 신호·지표 날짜는 체결일보다 앞서야 한다 (휴일·거래정지·누락봉 포함)
            if not use_close:
                for tag in ("signal_date", "indicator_asof"):
                    d = order.get(tag)
                    if d is not None and str(d) >= day_str:
                        raise ValueError(f"[백테스트] {symbol} {tag}={d} 가 체결일 {day_str} 보다 "
                                         f"앞서지 않음 — 정보 시점 위반")

            # 체결일 봉이 없으면(거래정지·누락봉) 체결 없음 — 이전 봉 시가로 대체하지 않는다
            data = self.universe.get_row_on_date(symbol, day_str)
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

            # 진입 손절폭 — 위험 기반 사이징·채널 하드스톱·R 계산의 기준.
            # ATR 은 체결 시점까지 확정된 봉만 (T+1 시가: 전일까지 / T+0 종가: 당일 포함) — F2
            atr_pct = self._entry_atr(symbol, day_str, include_day=use_close)
            stop_pct = self._entry_stop_pct(strategy, atr_pct)

            # 사이징 — 시가 체결은 시가 평가 equity (당일 종가 미래정보 배제) — F8
            equity = self._calc_equity(day_str, phase="close" if use_close else "open")
            pos_value = self._calc_position_size(equity, strategy, stop_pct)
            if pos_value < self.config.min_position_value:
                continue

            quantity = int(pos_value / exec_price)
            if quantity <= 0:
                continue

            # 위험 모드 최종 상한 — 실엔진과 같은 함수(src.utils.sizing.risk_quantity_cap):
            # (가격×수량 + 매수수수료) × net SL% ≤ equity × 위험% (2026-09-14 T6 parity).
            # 수수료를 뺀 근사(equity×r%/SL%)보다 1주가량 작다 — 실거래 주문과 같은 수량을 만든다.
            if (self.config.sizing == "risk" and strategy != StrategyType.CORE
                    and stop_pct > 0):
                cap = risk_quantity_cap(
                    Decimal(str(equity)), Decimal(str(exec_price)), Decimal(str(stop_pct)),
                    risk_per_trade_pct=self.config.risk_per_trade_pct)
                if cap < quantity:
                    quantity = cap
                if quantity <= 0 or exec_price * quantity < self.config.min_position_value:
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
                # T+1 시가 체결의 최고가는 시가로 초기화 — 체결일 고가는 시가 시점에 모르는 정보 (2026-09-14 리뷰 P2)
                highest_price=float(data.get('고가', exec_price)) if use_close else exec_price,
                score_at_entry=order.get('score', 0),
            )

            if atr_pct is not None:
                # 원본 ATR%도 보관(트레일링 연동용) — atr_stop_pct는 clamp되므로 역산이 불가능하다
                pos.atr_pct = atr_pct
            # 초기 손절·위험금액(R 분모) 고정 — 이후 ATR 재계산·레짐 전환으로 소급 변경하지 않는다
            pos.atr_stop_pct = stop_pct
            pos.initial_risk = total_cost * stop_pct / 100
            pos.stop_price = exec_price * (1 - stop_pct / 100)
            # live_policy 레짐 전환 감지 기준 — 시가 시점에 알려진 레짐(전일 종가까지)
            asof = (day_str if use_close
                    else (pd.Timestamp(day_str) - pd.Timedelta(days=1)).strftime("%Y-%m-%d"))
            pos.regime_seen = self.regime.get_regime(asof, self.config.regime_mode)

            self.positions[symbol] = pos
            self.trades.append(Trade(
                symbol=symbol, name=name, strategy=strategy.value,
                side="BUY", date=day_str, price=exec_price,
                quantity=quantity, amount=buy_amount, fee=buy_fee,
                reason=f"진입 (점수 {order.get('score', 0):.0f})",
                stop_pct=stop_pct, initial_risk=pos.initial_risk,
            ))

            if strategy in (StrategyType.SEPA, StrategyType.RSI2):
                short_count += 1
            else:
                core_count += 1

    def _short_slot_usage(self) -> float:
        """단기(비코어) 슬롯 사용량. fixed=포지션 수, live_weighted=잔여비율·단계 가중 합."""
        shorts = [p for p in self.positions.values()
                  if p.strategy in (StrategyType.SEPA, StrategyType.RSI2)
                  and p.remaining_quantity > 0]
        if self.config.slot_policy != "live_weighted":
            return float(len(shorts))
        return sum(live_slot_weight(p.remaining_quantity, p.quantity,
                                    p.exit_stage.name.lower()) for p in shorts)

    def _check_exits(self, day_str: str):
        to_remove = []
        for symbol, pos in list(self.positions.items()):
            if pos.remaining_quantity <= 0:
                to_remove.append(symbol)
                continue

            data = self.universe.get_row_on_date(symbol, day_str)
            if data is None:
                continue

            day_regime = self.regime.get_regime(day_str, self.config.regime_mode)
            actions = self.exit_mgr.check_exit(pos, data, regime=day_regime)
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

        regime = self.regime.get_regime(day_str, self.config.regime_mode)

        # SEPA / RSI2
        if short_pos < self.max_short:
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
                        signals.append(self._signal(ticker, StrategyType.SEPA, s, data, day_str))

                if "rsi2" in self.config.strategies:
                    s = self.scorer.score_rsi2(data)
                    if s >= self.config.rsi2_min_score:
                        signals.append(self._signal(ticker, StrategyType.RSI2, s, data, day_str))

            signals.sort(key=lambda x: x['score'], reverse=True)
            available = self.max_short - short_pos
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
                        core_sigs.append(self._signal(ticker, StrategyType.CORE, s, data, day_str))
                core_sigs.sort(key=lambda x: x['score'], reverse=True)
                avail = self.config.max_positions_core - core_pos
                for sig in core_sigs[:avail]:
                    self.pending_buys.append(sig)

    @staticmethod
    def _signal(ticker: str, strategy: StrategyType, score: float,
                data: pd.Series, day_str: str) -> dict:
        """pending_buys 항목. signal_date=신호 생성일(종가 이후), indicator_asof=지표에 쓴 마지막 봉 날짜
        (거래정지·누락봉이면 신호일보다 앞설 수 있다) — 체결 시 체결일보다 앞서는지 검사한다."""
        asof = data.name
        return {
            'symbol': ticker, 'strategy': strategy, 'score': score,
            'signal_close': float(data.get('종가', 0)),
            'signal_date': day_str,
            'indicator_asof': (asof.strftime("%Y-%m-%d") if isinstance(asof, pd.Timestamp)
                               else str(asof)),
        }

    def _entry_atr(self, symbol: str, day_str: str, *, include_day: bool = False) -> Optional[float]:
        """진입 ATR%(원본). 체결 시점까지 확정된 마지막 거래봉만 사용 — F2.
        include_day=False(T+1 시가 체결): day_str **이전** 봉 / True(T+0 종가 체결): 당일 봉 포함.
        미산출(워밍업)·비양수·봉 없음이면 None."""
        row = (self.universe.get_data(symbol, day_str) if include_day
               else self.universe.get_last_bar_before(symbol, day_str))
        if row is None:
            return None
        atr_pct = row.get('atr_pct')
        if atr_pct is None or pd.isna(atr_pct) or float(atr_pct) <= 0:
            return None
        return float(atr_pct)

    def _entry_stop_pct(self, strategy: StrategyType, atr_pct: Optional[float]) -> float:
        """신규 진입 초기 손절폭(%) — entry_stop_mode 참조 (BacktestConfig 주석).
        live_policy: 전략별 고정 SL (ATR 무관). atr_dynamic: ATR×배수 클램프, ATR 없으면 5.0(기존 동작)."""
        if self.config.entry_stop_mode == "live_policy":
            return {StrategyType.SEPA: self.config.sepa_stop_loss_pct,
                    StrategyType.RSI2: self.config.rsi2_stop_loss_pct,
                    StrategyType.CORE: self.config.core_stop_loss_pct}[strategy]
        if atr_pct is None:
            return 5.0
        return max(self.config.min_stop_pct,
                   min(self.config.max_stop_pct, atr_pct * self.config.atr_multiplier))

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
                            strategy: StrategyType,
                            stop_pct: Optional[float] = None) -> float:
        base = equity * self.config.base_position_pct / 100
        max_sz = equity * self.config.max_position_pct / 100
        min_reserve = equity * self.config.min_cash_reserve_pct / 100
        available = max(0, self.cash - min_reserve)
        pos_val = min(base, max_sz, available)

        # 위험 기반 사이징: 건당 자본 위험 = risk_per_trade_pct (equity×r% / stop% → % 약분)
        if (self.config.sizing == "risk" and strategy != StrategyType.CORE
                and stop_pct is not None and stop_pct > 0):
            risk_val = equity * self.config.risk_per_trade_pct / stop_pct
            pos_val = min(risk_val,
                          equity * self.config.risk_max_position_pct / 100,
                          available)

        if strategy == StrategyType.CORE:
            core_budget = equity * self.config.allocation.get("core", 0.30)
            core_per = core_budget / max(self.config.max_positions_core, 1)
            pos_val = min(core_per, available)

        return pos_val

    def _calc_equity(self, day_str: str, *, phase: str = "close") -> float:
        """평가 자산. phase="close": 당일 종가(없으면 마지막 봉 종가) — EOD 성과 기록.
        phase="open": 당일 시가만 사용, 당일 봉이 없는 보유 종목은 마지막 확정(전일 이전) 종가 —
        시가 체결 사이징용 (당일 종가 미래정보 배제, F8)."""
        if phase not in ("open", "close"):
            raise ValueError(f"phase 는 open|close: {phase!r}")
        equity = self.cash
        for pos in self.positions.values():
            if pos.remaining_quantity <= 0:
                continue
            price = None
            if phase == "open":
                row = self.universe.get_row_on_date(pos.symbol, day_str)
                if row is not None and not pd.isna(row.get('시가')) and float(row['시가']) > 0:
                    price = float(row['시가'])
                else:
                    prev = self.universe.get_last_bar_before(pos.symbol, day_str)
                    if prev is not None:
                        price = float(prev.get('종가', pos.entry_price))
            else:
                data = self.universe.get_data(pos.symbol, day_str)
                if data is not None:
                    price = float(data.get('종가', pos.entry_price))
            equity += (price if price is not None else pos.entry_price) * pos.remaining_quantity
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

    def metrics(self) -> Dict[str, Any]:
        """
        결과 지표를 dict로 반환 (출력 없음).

        진화 백테스트 게이트(`src/core/evolution/backtest_gate.py`)가
        A/B 비교에 사용하므로, 표시용 print_results와 분리해 둔다.
        """
        if not self.equity_curve:
            return {}

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

        return {
            "initial": initial,
            "final": final,
            "total_return_pct": total_ret,
            "cagr_pct": cagr,
            "mdd_pct": mdd,
            "mdd_start": mdd_s,
            "mdd_end": mdd_e,
            "sharpe": sharpe,
            "win_rate": wr,
            "profit_factor": pf,
            "total_trades": len(sells),
            "wins": len(wins),
            "losses": len(losses),
            "total_fees": total_fees,
            "start_date": start_d,
            "end_date": end_d,
            "avg_holding_days": (float(np.mean([t.holding_days for t in sells]))
                                 if sells else 0.0),
            "position_level": self.position_metrics(),
        }

    # ── 포지션(왕복) 단위 ─────────────────────────────────
    def positions(self) -> List[Dict[str, Any]]:
        """이벤트(부분 매도) 단위 trades → 포지션(왕복) 단위로 합산.

        리뷰 §6 "이벤트 승률 지표 사용 금지 → 포지션 단위 R" 대응. 한 종목은 동시에 한
        포지션만 가지므로 BUY 를 구분자로 삼는다. R = 순손익% / 진입 손절폭%.
        """
        open_: Dict[str, dict] = {}
        out: List[Dict[str, Any]] = []
        for t in self.trades:
            if t.side == "BUY":
                open_[t.symbol] = dict(
                    symbol=t.symbol, name=t.name, strategy=t.strategy,
                    entry_date=t.date, exit_date=t.date, entry_price=t.price,
                    quantity=t.quantity, sold=0, cost=t.amount + t.fee,
                    proceeds=0.0, fees=t.fee, notional=t.amount,
                    stop_pct=t.stop_pct, initial_risk=t.initial_risk,
                    holding_days=0, reasons=[])
                continue
            p = open_.get(t.symbol)
            if p is None:
                continue
            p['proceeds'] += t.amount - t.fee
            p['fees'] += t.fee
            p['notional'] += t.amount
            p['sold'] += t.quantity
            p['exit_date'] = t.date
            p['holding_days'] = t.holding_days
            p['reasons'].append(t.reason)
            if p['sold'] >= p['quantity']:
                p['pnl'] = p['proceeds'] - p['cost']
                p['pnl_pct'] = p['pnl'] / p['cost'] * 100 if p['cost'] > 0 else 0.0
                # 포지션 R = 순손익 ÷ 최초 확정 위험금액 (T0 회계 단위). 구 기록(initial_risk 없음)은 pnl%/stop% 폴백
                _ir = p.get('initial_risk')
                if _ir is not None and _ir > 0:
                    p['r'] = p['pnl'] / _ir
                else:
                    p['r'] = p['pnl_pct'] / p['stop_pct'] if p['stop_pct'] > 0 else 0.0
                out.append(open_.pop(t.symbol))
        return out

    def position_metrics(self, positions: Optional[List[Dict[str, Any]]] = None
                         ) -> Dict[str, Any]:
        """포지션 단위 지표 (A/B 비교용). positions 를 주면 그 부분집합만 집계."""
        ps = self.positions() if positions is None else positions
        eqs = [e for _, e in self.equity_curve] or [float(self.config.initial_capital)]
        avg_eq = float(np.mean(eqs))
        days = ((pd.Timestamp(self.equity_curve[-1][0]) - pd.Timestamp(self.equity_curve[0][0])).days
                if len(self.equity_curve) > 1 else 0)
        wins = [p for p in ps if p['pnl'] > 0]
        losses = [p for p in ps if p['pnl'] <= 0]
        avg_w = float(np.mean([p['pnl_pct'] for p in wins])) if wins else 0.0
        avg_l = float(np.mean([p['pnl_pct'] for p in losses])) if losses else 0.0
        gross_w = sum(p['pnl'] for p in wins)
        gross_l = abs(sum(p['pnl'] for p in losses))
        streak = worst = 0
        for p in sorted(ps, key=lambda x: (x['exit_date'], x['entry_date'])):
            streak = streak + 1 if p['pnl'] <= 0 else 0
            worst = max(worst, streak)
        reasons: Dict[str, int] = {}
        for p in ps:
            for r in p['reasons']:
                key = r.split(' (')[0].split(' +')[0].split(' -')[0]
                reasons[key] = reasons.get(key, 0) + 1
        notional = sum(p['notional'] for p in ps)
        return {
            "trades": len(ps),
            "win_rate": len(wins) / len(ps) * 100 if ps else 0.0,
            "avg_win_pct": avg_w,
            "avg_loss_pct": avg_l,
            "payoff": (avg_w / abs(avg_l)) if avg_l < 0 else (float('inf') if wins else 0.0),
            "expectancy_pct": float(np.mean([p['pnl_pct'] for p in ps])) if ps else 0.0,
            "expectancy_r": float(np.mean([p['r'] for p in ps])) if ps else 0.0,
            "profit_factor": (gross_w / gross_l) if gross_l > 0 else (float('inf') if gross_w > 0 else 0.0),
            "net_pnl": sum(p['pnl'] for p in ps),
            "max_consec_losses": worst,
            "median_holding_days": float(np.median([p['holding_days'] for p in ps])) if ps else 0.0,
            "turnover": notional / avg_eq if avg_eq > 0 else 0.0,
            "turnover_annual": (notional / avg_eq * 365 / days) if (avg_eq > 0 and days > 0) else 0.0,
            "fee_drag_pct": sum(p['fees'] for p in ps) / avg_eq * 100 if avg_eq > 0 else 0.0,
            "exit_reasons": dict(sorted(reasons.items(), key=lambda kv: -kv[1])),
        }

    def print_results(self):
        m = self.metrics()
        if not m:
            print("데이터 없음")
            return

        initial = m["initial"]
        final = m["final"]
        total_ret = m["total_return_pct"]
        start_d = m["start_date"]
        end_d = m["end_date"]
        cagr = m["cagr_pct"]
        mdd, mdd_s, mdd_e = m["mdd_pct"], m["mdd_start"], m["mdd_end"]
        wr = m["win_rate"]
        pf = m["profit_factor"]
        sharpe = m["sharpe"]
        total_fees = m["total_fees"]

        sells = [t for t in self.trades if t.side == "SELL"]
        wins = [t for t in sells if t.pnl > 0]

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
    """유효 설정(default.yml + evolved_overrides.yml) — 병합은 src/utils/config 단일 출처."""
    from src.utils.config import load_effective_config
    return load_effective_config()


# 백테스터가 모사하는 전략 ↔ 실운영 전략 키
BT_STRATEGY_KEYS = {"sepa": "sepa_trend", "rsi2": "rsi2_reversal", "core": "core_holding"}


def build_backtest_config_from_effective(
    effective_config: Dict[str, Any], *, months: int, strategies: List[str],
    **overrides: Any,
) -> BacktestConfig:
    """유효 설정(default.yml + evolved_overrides.yml 병합) → BacktestConfig (2026-09-14 T6).

    운영 게이트(backtest_gate)와 A/B 러너가 **같은 builder**를 써서, 백테스트 기준군이
    실운영 설정과 갈라지지 않게 한다 (F7: 기본 nominal·TP1 5% 기준군에서 risk 변경을 판정하던 결함).

    매핑:
      사이징    risk.sizing_mode → sizing, risk_per_trade_pct, risk_max_position_pct,
                base_position_pct, max_position_pct
      전략·배분 risk.strategy_allocation → allocation (0% 전략은 제외), strategies
      초기 손절 entry_stop_mode="live_policy" + 전략별 stop_loss_pct (실엔진 신규 fill 미러)
      청산      exit_manager.* (분할익절·트레일링·ATR·stale) + 복합 청산·익절후 저효율 ON,
                stale_high 는 실엔진 전략 파라미터(run_trader._strategy_exit_params sepa=3일)
      현금·슬롯 min_cash_reserve_pct, min_position_value, risk.max_positions(가중 슬롯)
      수수료    FeeCalculator 단일 출처와 동일해야 한다 (다르면 UnsupportedBacktestConfig)

    Raises:
        UnsupportedBacktestConfig — 백테스터가 모사할 수 없는 유효 설정 (미지원 사이징 모드,
        수수료 모델 불일치, 모사 가능한 활성 전략 없음). 게이트는 이를 보류(passed=False)로 다룬다.
    """
    kr = effective_config.get("kr", {}) or {}
    risk = kr.get("risk", {}) or {}
    em = kr.get("exit_manager", {}) or {}
    strats = kr.get("strategies", {}) or {}
    alloc_raw = risk.get("strategy_allocation", {}) or {}

    # 수수료: 백테스터 상수 = FeeCalculator(실엔진 단일 출처) 여야 한다
    fee = get_fee_calculator("KR")
    if (abs(float(fee.config.buy_commission_rate) - BUY_FEE_RATE) > 1e-12
            or abs(float(fee.config.total_sell_rate) - SELL_FEE_RATE) > 1e-12):
        raise UnsupportedBacktestConfig(
            f"수수료 모델 불일치: 백테스터 {BUY_FEE_RATE}/{SELL_FEE_RATE} vs "
            f"FeeCalculator {float(fee.config.buy_commission_rate)}/"
            f"{float(fee.config.total_sell_rate)}")

    sizing = str(risk.get("sizing_mode", "nominal"))
    if sizing not in ("nominal", "risk"):
        raise UnsupportedBacktestConfig(f"백테스터 미지원 사이징 모드: {sizing!r}")

    # 전략 활성·배분 — 배분 0%(또는 미설정)는 제외, 백테스터 미지원 라인은 범위로 기록
    requested = [s for s in strategies]
    unknown = [s for s in requested if s not in BT_STRATEGY_KEYS]
    if unknown:
        raise UnsupportedBacktestConfig(f"백테스터 미지원 전략: {unknown}")
    zero = [s for s in requested if float(alloc_raw.get(BT_STRATEGY_KEYS[s], 0) or 0) <= 0]
    active = [s for s in requested if s not in zero]
    if not active:
        raise UnsupportedBacktestConfig(
            f"모사 가능한 활성 전략 없음 — 요청 {requested} 의 배분이 모두 0% "
            f"(유효 배분: { {k: v for k, v in alloc_raw.items() if v} })")
    total_a = sum(float(alloc_raw[BT_STRATEGY_KEYS[s]]) for s in active)
    alloc = {s: float(alloc_raw[BT_STRATEGY_KEYS[s]]) / total_a for s in active}

    unsupported_alloc = {k: float(v) for k, v in alloc_raw.items()
                         if float(v or 0) > 0 and k not in BT_STRATEGY_KEYS.values()}
    alloc_total = sum(float(v or 0) for v in alloc_raw.values())
    scope = {
        "strategies_requested": requested,
        "strategies_simulated": active,
        "excluded_zero_allocation": zero,
        "unsupported_allocated": unsupported_alloc,
        "allocation_covered_pct": round(total_a / alloc_total * 100, 1) if alloc_total > 0 else 0.0,
        "entry_stop_mode": "live_policy",
        "slot_policy": "live_weighted",
        "calculator_version": CALC_VERSION,
    }

    sepa = strats.get("sepa_trend", {}) or {}
    rsi2 = strats.get("rsi2_reversal", {}) or {}
    core = strats.get("core_holding", {}) or {}

    cfg = BacktestConfig(
        months=months,
        strategies=active,
        allocation=alloc,
        supported_scope=scope,
        # 사이징
        sizing=sizing,
        risk_per_trade_pct=float(risk.get("risk_per_trade_pct", 0.7)),
        risk_max_position_pct=float(risk.get("risk_max_position_pct", 18.0)),
        base_position_pct=float(risk.get("base_position_pct", 25.0)),
        max_position_pct=float(risk.get("max_position_pct", 28.0)),
        # 슬롯 — 실엔진은 코어 제외 가중 합 ≤ max_positions
        slot_policy="live_weighted",
        max_positions_weighted=float(risk.get("max_positions", 8)),
        max_positions_short=int(risk.get("max_positions", 8)) - int(core.get("max_positions", 3)),
        max_positions_core=int(core.get("max_positions", 3)),
        # 현금·최소금액·일일 손실
        min_cash_reserve_pct=float(risk.get("min_cash_reserve_pct", 5.0)),
        min_position_value=int(risk.get("min_position_value", 200_000)),
        daily_max_loss_pct=float(risk.get("daily_max_loss_pct", 5.0)),
        # 초기 손절 — 실엔진 신규 fill 은 전략별 고정 SL (ATR 동적 손절 없음)
        entry_stop_mode="live_policy",
        sepa_min_score=float(sepa.get("min_score", 60.0)),
        sepa_stop_loss_pct=float(sepa.get("stop_loss_pct", 5.0)),
        sepa_max_holding_days=int(sepa.get("max_holding_days", 10)),
        rsi2_min_score=float(rsi2.get("min_score", 60.0)),
        rsi2_stop_loss_pct=float(rsi2.get("stop_loss_pct", 5.0)),
        rsi2_max_holding_days=int(rsi2.get("max_holding_days", 10)),
        core_min_score=float(core.get("min_score", 70.0)),
        core_stop_loss_pct=float(core.get("stop_loss_pct", 15.0)),
        core_trailing_stop_pct=float(core.get("trailing_stop_pct", 8.0)),
        core_trailing_activate_pct=float(core.get("trailing_activate_pct", 10.0)),
        # 청산 사다리·트레일링·ATR
        first_exit_pct=float(em.get("first_exit_pct", 10.0)),
        first_exit_ratio=float(em.get("first_exit_ratio", 0.10)),
        second_exit_pct=float(em.get("second_exit_pct", 15.0)),
        second_exit_ratio=float(em.get("second_exit_ratio", 0.50)),
        third_exit_pct=float(em.get("third_exit_pct", 25.0)),
        third_exit_ratio=float(em.get("third_exit_ratio", 0.50)),
        trailing_stop_pct=float(em.get("trailing_stop_pct", 3.0)),
        trailing_activate_pct=float(em.get("trailing_activate_pct", 5.0)),
        atr_multiplier=float(em.get("atr_multiplier", 2.0)),
        min_stop_pct=float(em.get("min_stop_pct", 4.0)),
        max_stop_pct=float(em.get("max_stop_pct", 8.0)),
        # 복합 청산·익절후 저효율·정체 (실엔진 값)
        enable_composite_exit=bool(em.get("enable_composite_trailing", True)),
        post_exit_stale_days=int(em.get("post_exit_stale_days", 5)),
        post_exit_stale_pnl_pct=float(em.get("post_exit_stale_pnl_pct", 3.0)),
        stale_exit_days=int(em.get("stale_exit_days", 5)),
        stale_exit_pnl_pct=float(em.get("stale_exit_pnl_pct", 2.0)),
        # 신고가 실패(추세 무효화)는 실엔진에서 전략 파라미터 — sepa/vcp 3영업일
        stale_high_days=int(sepa.get("stale_high_days", 3)),
        stale_high_min_pnl_pct=float(em.get("stale_high_min_pnl_pct", 3.0)),
    )

    for key, value in overrides.items():
        if not hasattr(cfg, key):
            raise UnsupportedBacktestConfig(f"알 수 없는 BacktestConfig 필드: {key}")
        setattr(cfg, key, value)
    cfg.__post_init__()          # 오버라이드된 모드 값도 한 곳에서 검증
    return cfg


def build_config(yaml_cfg: dict, args: argparse.Namespace) -> BacktestConfig:
    """CLI 경로 설정 빌더 — 기존 기본 동작(atr_dynamic·fixed 슬롯) 유지."""
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
def build_parser() -> argparse.ArgumentParser:
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
    parser.add_argument('--exit-policy', choices=['ladder', 'channel'], default='ladder',
                        help='청산 정책 (기본 ladder=현행 사다리)')
    parser.add_argument('--holding-policy', choices=list(HOLDING_POLICIES), default=None,
                        help='보유 규칙 프리셋 (미지정 시 YAML 값 그대로)')
    parser.add_argument('--sizing', choices=['nominal', 'risk'], default='nominal',
                        help='사이징 (기본 nominal=25%%)')
    parser.add_argument('--entry-stop-mode', choices=list(ENTRY_STOP_MODES), default='atr_dynamic',
                        help='신규 진입 초기 손절 (기본 atr_dynamic=기존 동작, live_policy=전략 고정 SL·실엔진 미러)')
    return parser


def main():
    args = build_parser().parse_args()

    yaml_cfg = load_config_from_yaml()
    config = build_config(yaml_cfg, args)
    config.exit_policy = args.exit_policy
    config.sizing = args.sizing
    config.entry_stop_mode = args.entry_stop_mode
    if args.holding_policy:
        apply_holding_policy(config, args.holding_policy)

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
