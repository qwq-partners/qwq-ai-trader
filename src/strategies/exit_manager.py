"""
QWQ AI Trader - 분할 익절/청산 관리자 (통합)

KR/US 통합 ExitManager.
KR: 수수료(FeeCalculator) 포함 순수익 기준 분할 익절.
US: zero-commission 기준 분할 익절.

분할 익절 전략 (3단계):
1. 1차 익절 → 일정 비율 매도 (빠른 수익 확보)
2. 2차 익절 → 잔여의 일정 비율 매도 (중간 목표)
3. 3차 익절 → 잔여의 일정 비율 매도
4. 나머지 → 트레일링 스탑으로 수익 극대화

ATR 기반 동적 손절:
- 변동성 낮음(ATR 1%) → min_stop_pct 손절
- 변동성 높음(ATR 3%+) → max_stop_pct 손절 (상한)
"""

import json
from dataclasses import dataclass, field
from datetime import datetime, date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from enum import Enum
from loguru import logger

from ..core.types import Position, OrderSide, Signal, SignalStrength
from ..utils.fee_calculator import FeeCalculator, get_fee_calculator
from ..indicators.atr import calculate_atr, calculate_dynamic_stop_loss


class ExitStage(Enum):
    """익절 단계"""
    NONE = "none"               # 익절 전
    FIRST = "first"             # 1차 익절
    SECOND = "second"           # 2차 익절
    THIRD = "third"             # 3차 익절
    TRAILING = "trailing"       # 트레일링


# ──────────────────────────────────────────────────────────────────────────────
# 시장 레짐별 청산 파라미터 테이블
# LLM 레짐 분류기(trending_bull / ranging / trending_bear / turning_point / neutral)
# 결과에 따라 ExitManager.apply_regime_params() 가 기존 포지션에 실시간 반영.
#
# 설계 원칙:
#   - stop_loss / trailing / stale_high_days : 모든 stage에 즉시 적용
#   - first_exit_pct  : stage=NONE 에만 (FIRST 이상은 이미 1차 완료)
#   - second_exit_pct : stage ≤ FIRST 에만
#   - third_exit_pct  : stage ≤ SECOND 에만
# ──────────────────────────────────────────────────────────────────────────────
# ▶ P0-4 수정: 분할 익절 구조를 추세 추종에 맞게 조정
#
# 기존 문제: TP1=5% TP2=10% TP3=12% — 타겟이 너무 촘촘하고 낮음.
#   SEPA 종목 기대 움직임 +20~50%인데 +12%에서 82.5% 포지션 이미 청산.
#   first_exit_ratio=30% → 너무 이른 수익 확정 + 추세 소멸 후 남은 포지션만 홀딩.
#
# 수정:
#   first_exit_ratio: 30% → 20% (포지션 더 오래 유지)
#   TP2: 10% → 15% (추세 중간 목표)
#   TP3: 12% → 25% (추세 후반 목표, SEPA 기대값과 정렬)
#   trailing: 범위별 3~5% (ATR 대비 현실적 범위로 상향)
#
# 기대 효과 (종목 +40% 달성 기준):
#   변경 전: 30%×5% + 35%×10% + 17.5%×12% + 17.5%×37% = 13.6%
#   변경 후: 20%×5% + 40%×15% + 20%×25% + 20%×37%     = 19.4%  (+42%)
REGIME_EXIT_PARAMS: Dict[str, Dict] = {
    "trending_bull": {
        "first_exit_pct":    5.0,
        "second_exit_pct":  15.0,   # 10 → 15
        "third_exit_pct":   25.0,   # 12 → 25
        "trailing_stop_pct":  4.0,  #  3 → 4 (KR ATR 5~7% 대비)
        "stop_loss_pct":      5.0,
        "stale_high_days":    7,
    },
    "neutral": {
        "first_exit_pct":    5.0,
        "second_exit_pct":  12.0,   #  8 → 12
        "third_exit_pct":   20.0,   # 10 → 20
        "trailing_stop_pct":  3.0,  # 2.5 → 3
        "stop_loss_pct":      4.0,
        "stale_high_days":    5,
    },
    "ranging": {
        "first_exit_pct":    4.0,
        "second_exit_pct":   8.0,   #  7 → 8
        "third_exit_pct":   14.0,   #  9 → 14
        "trailing_stop_pct":  2.5,
        "stop_loss_pct":      4.0,
        "stale_high_days":    4,
    },
    "turning_point": {          # 바닥 전환점 — 중간값
        "first_exit_pct":    4.0,
        "second_exit_pct":  10.0,   #  7 → 10
        "third_exit_pct":   18.0,   #  9 → 18
        "trailing_stop_pct":  3.0,  # 2.5 → 3
        "stop_loss_pct":      4.0,
        "stale_high_days":    5,
    },
    "trending_bear": {
        "first_exit_pct":    3.0,
        "second_exit_pct":   8.0,   #  6 → 8 (bear에서도 추세 공간 확보)
        "third_exit_pct":   14.0,   #  8 → 14
        "trailing_stop_pct":  2.0,  # 1.5 → 2
        "stop_loss_pct":      3.5,
        "stale_high_days":    3,
    },
}

# ──────────────────────────────────────────────────────────────────────────────
# 장중 급락 단계별 손절/트레일링 오버라이드 파라미터
# KOSPI 당일 등락률 기준:
#   caution : -1.5% ~ -2.5%  → 손절/트레일링 소폭 강화
#   crash   : -2.5% ~ -3.5%  → trending_bear 수준으로 압축
#   severe  : -3.5% 이하      → 신규 진입 전면 차단 + 최단 손절
#
# * TP 목표치는 레짐 파라미터 유지 (추세 흐름에서 복구 여지 남김)
# * 코어홀딩 포지션은 장중 급락 오버라이드에서도 제외 (is_core=True)
# ──────────────────────────────────────────────────────────────────────────────
INTRADAY_CRASH_PARAMS: Dict[str, Dict] = {
    "caution": {
        "stop_loss_pct":      3.0,   # neutral 4% → 3%
        "trailing_stop_pct":  2.5,   # neutral 3% → 2.5%
    },
    "crash": {
        "stop_loss_pct":      2.5,   # trending_bear 3.5% → 2.5%
        "trailing_stop_pct":  2.0,   # trending_bear 2% 유지
    },
    "severe": {
        "stop_loss_pct":      2.0,   # 최단 손절 — severe일 땐 진입도 차단
        "trailing_stop_pct":  1.5,
    },
}


@dataclass
class ExitConfig:
    """청산 설정 (KR/US 통합)"""
    # 분할 익절 설정
    enable_partial_exit: bool = True

    # 1차 익절
    first_exit_pct: float = 5.0       # 목표 수익률 (%)
    first_exit_ratio: float = 0.30    # 청산 비율 (evolved_overrides에서 0.2로 오버라이드)

    # 2차 익절
    second_exit_pct: float = 15.0     # 목표 수익률 (%) (10 → 15: P0-4)
    second_exit_ratio: float = 0.50   # 잔여의 비율

    # 3차 익절
    third_exit_pct: float = 25.0      # 목표 수익률 (%) (12 → 25: P0-4)
    third_exit_ratio: float = 0.50    # 잔여의 비율

    # 손절
    stop_loss_pct: float = 5.0        # 기본 손실률 (%)
    enable_dynamic_stop: bool = True  # ATR 기반 동적 손절 활성화
    atr_multiplier: float = 2.0       # ATR 배수
    min_stop_pct: float = 4.0         # 최소 손절폭 (%)
    max_stop_pct: float = 7.0         # 최대 손절폭 (%)

    # 트레일링 스탑
    trailing_stop_pct: float = 3.0    # 고점 대비 하락률 (%)
    trailing_activate_pct: float = 5.0  # 트레일링 활성화 수익률 (%)
    atr_trailing_multiplier: float = 1.5  # ATR 트레일링 승수 (breakeven 모드에서 사용)

    # ATR-linked 트레일링 (고정 트레일링을 ATR에 연동하여 과민 청산 방지)
    # effective_ts = min( max(config_trailing_stop_pct, ATR_pct × atr_link_multiplier), atr_link_cap_pct )
    # - 하한: REGIME 기반 trailing_stop_pct 존중 (너무 작아지지 않음)
    # - 상한: atr_link_cap_pct (트레일링이 너무 커져 손실 확대 방지)
    enable_atr_linked_trailing: bool = True   # ATR 연동 트레일링 활성화
    atr_link_multiplier: float = 1.2          # ATR_pct × 배수 = 제안 트레일링
    atr_link_cap_pct: float = 6.0             # 상한선 (%)

    # 수수료 포함 계산 (KR=True, US=False)
    include_fees: bool = True

    # 최대 보유 기간 (영업일 기준)
    max_holding_days: int = 10

    # 횡보 조기 청산: N영업일 이상 보유 & |수익률| < X% → 전량 청산
    stale_exit_days: int = 5          # 횡보 판단 시작 영업일
    stale_exit_pnl_pct: float = 2.0   # 횡보 판단 수익률 범위 (±%)

    # 신고가 실패 무효화: N영업일 신고가 갱신 없음 & PnL < X% → 전량 청산 (추세 소멸)
    stale_high_days: int = 0          # 0 = 비활성화 (전략별 override 사용)
    stale_high_min_pnl_pct: float = 3.0  # 이 수익률 미만일 때만 적용

    # 익절 후 횡보 청산: stage >= FIRST & N영업일 보유 & 수익률 < X% → 저효율 포지션 정리
    post_exit_stale_days: int = 5            # 1차 익절 후 횡보 판단 영업일
    post_exit_stale_pnl_pct: float = 3.0     # 이 수익률 미만이면 저효율로 판단 (%)

    # 복합 트레일링 (MA5 + 전일저가 기반)
    enable_composite_trailing: bool = True       # 복합 트레일링 활성화
    composite_trail_min_stage: str = "first"     # 최소 적용 단계 (1차 익절 완료 이후)
    composite_ma5_buffer_pct: float = 0.5        # MA5 아래 버퍼 (%) — MA5 - 0.5% 이탈 시 청산
    composite_prev_low_enabled: bool = True      # 전일 저가 기준 활성화

    # End-of-day close (US day trade 전용 — ExitManager 내부 미사용, us_scheduler가 직접 처리)
    eod_close: bool = False


@dataclass
class PositionExitState:
    """포지션별 청산 상태"""
    symbol: str
    entry_price: Decimal
    original_quantity: int
    remaining_quantity: int
    current_stage: ExitStage = ExitStage.NONE
    highest_price: Decimal = Decimal("0")
    total_realized_pnl: Decimal = Decimal("0")
    exit_history: List[Dict] = field(default_factory=list)
    # 전략별 청산 파라미터 (None이면 글로벌 ExitConfig 사용)
    stop_loss_pct: Optional[float] = None
    trailing_stop_pct: Optional[float] = None
    # 전략별 익절 목표 (None이면 글로벌 ExitConfig 사용)
    first_exit_pct: Optional[float] = None
    second_exit_pct: Optional[float] = None
    third_exit_pct: Optional[float] = None
    # 전략별 익절 비율 (None이면 글로벌 ExitConfig 사용, 코어+트레이더 구조용)
    first_exit_ratio: Optional[float] = None
    second_exit_ratio: Optional[float] = None
    third_exit_ratio: Optional[float] = None
    # ATR 기반 동적 손절
    atr_pct: Optional[float] = None
    dynamic_stop_pct: Optional[float] = None
    # ATR 연동 트레일링: register 시 계산된 실효 트레일링 한도 (%)
    # - None: ATR 미전달 → 기존 방식 (state.trailing_stop_pct 또는 config 사용)
    # - 숫자: ATR-linked 방식 (max(config_ts, ATR×mult), capped by atr_link_cap_pct)
    effective_trailing_stop_pct: Optional[float] = None
    # 1R 도달 후 본전 이동 트레일링
    breakeven_activated: bool = False
    # 신고가 실패 무효화 (추세 소멸 감지)
    last_new_high_date: Optional[date] = None
    stale_high_days: Optional[int] = None           # None이면 글로벌 ExitConfig 사용
    # 분할 익절 신호 발행됨, fill 대기 중 (재시작 시 None → current_stage 유지, 재발행 방지)
    # 파일에 저장 안 함: 재시작 시 None → current_stage=NONE이면 자동 재발행
    pending_stage: Optional[ExitStage] = None
    # 포지션 최초 진입 수량 (부분 매도 후에도 유지 — 재시작 정합성 검증용)
    # 파일의 initial_qty에서 복원, 없으면 original_quantity로 초기화
    initial_quantity: int = 0
    # 코어홀딩 전용: 레짐 오버라이드 제외, 포지션별 max_holding_days 우선 적용
    is_core: bool = False
    max_holding_days: Optional[int] = None  # None이면 글로벌 ExitConfig 사용, 0이면 무제한
    trailing_activate_pct: Optional[float] = None  # None이면 글로벌 ExitConfig 사용


class ExitManager:
    """
    분할 익절/청산 관리자 (KR/US 통합)

    KR: 수수료를 포함한 순수익 기준으로 분할 익절 관리.
    US: zero-commission 기준 분할 익절 관리.
    """

    # 클래스 상수: stage 순서 (모든 메서드에서 통일 사용)
    STAGE_ORDER = [ExitStage.NONE, ExitStage.FIRST, ExitStage.SECOND, ExitStage.THIRD, ExitStage.TRAILING]
    _COMPOSITE_STAGE_MAP = {
        "none": ExitStage.NONE,
        "first": ExitStage.FIRST,
        "second": ExitStage.SECOND,
        "third": ExitStage.THIRD,
        "trailing": ExitStage.TRAILING,
    }

    def __init__(self, config: Optional[ExitConfig] = None, market: str = "KR"):
        self.config = config or ExitConfig()
        self.market = market.upper()

        # 수수료 계산기 (KR: 수수료 포함, US: zero-commission)
        self.fee_calc = get_fee_calculator(self.market)

        # US인 경우 기본 수수료 비포함
        if self.market in ("US", "NASDAQ", "NYSE", "AMEX"):
            self.config.include_fees = False

        # 포지션별 청산 상태
        self._states: Dict[str, PositionExitState] = {}

        # 청산 예외 종목 (수동 매수 등)
        self._exit_exempt: set = set()

        # 보유기간 체크용 (포지션별 진입 시간)
        self._entry_times: Dict[str, datetime] = {}
        self._max_holding_days: int = self.config.max_holding_days

        # stage 영속화: 재시작 후 정확한 stage 복원
        _cache_dir = Path.home() / ".cache" / "ai_trader"
        _cache_dir.mkdir(parents=True, exist_ok=True)
        market_suffix = f"_{self.market.lower()}" if self.market != "KR" else ""
        self._stage_file = _cache_dir / f"exit_stages{market_suffix}_{date.today().isoformat()}.json"
        self._persisted: Dict[str, Dict] = self._load_persisted_states()

        # 현재 적용된 레짐 (apply_regime_params 호출 시 갱신)
        self._current_regime: str = "neutral"
        # 장중 급락 오버라이드 상태 ("normal" | "caution" | "crash" | "severe")
        self._intraday_crash_level: str = "normal"

    # ------------------------------------------------------------------ #
    # Stage 영속화                                                        #
    # ------------------------------------------------------------------ #

    def _load_persisted_states(self) -> Dict[str, Dict]:
        """당일 stage 파일 로드. 없으면 최근 7일 파일에서 폴백."""
        _cache_dir = self._stage_file.parent
        market_suffix = f"_{self.market.lower()}" if self.market != "KR" else ""
        for delta in range(0, 7):
            candidate = _cache_dir / f"exit_stages{market_suffix}_{(date.today() - timedelta(days=delta)).isoformat()}.json"
            if not candidate.exists():
                continue
            try:
                with open(candidate, "r") as f:
                    data = json.load(f)
                if data:
                    logger.info(
                        f"[ExitManager] stage 복원 파일 로드: {len(data)}종목 "
                        f"({candidate.name})"
                    )
                    return data
            except Exception as e:
                logger.warning(f"[ExitManager] stage 파일 로드 실패({candidate.name}): {e}")
        return {}

    def _persist_states(self) -> None:
        """현재 모든 포지션의 stage/highest_price를 파일에 저장.
        
        initial_qty: 포지션 최초 진입 수량 (부분 매도 후에도 유지).
          재시작 시 KIS 실제 잔고와 비교해 익절 미실행 여부를 검증하는 데 사용.
        """
        data = {}
        for sym, state in self._states.items():
            entry: Dict = {
                "stage": state.current_stage.value,
                "highest_price": str(state.highest_price),
                "breakeven_activated": state.breakeven_activated,
                "is_core": state.is_core,
            }
            if state.max_holding_days is not None:
                entry["max_holding_days"] = state.max_holding_days
            if state.trailing_activate_pct is not None:
                entry["trailing_activate_pct"] = state.trailing_activate_pct
            # ATR-linked trailing: 재시작 시 소실 방지 (sync 재등록에서 원본 atr_pct를 못 받음)
            if state.effective_trailing_stop_pct is not None:
                entry["effective_trailing_stop_pct"] = state.effective_trailing_stop_pct
            if state.atr_pct is not None:
                entry["atr_pct"] = state.atr_pct
            # 코어 포지션의 전용 파라미터 영속화 (재시작 시 strategy=None 폴백 방어)
            if state.is_core:
                if state.stop_loss_pct is not None:
                    entry["stop_loss_pct"] = state.stop_loss_pct
                if state.trailing_stop_pct is not None:
                    entry["trailing_stop_pct"] = state.trailing_stop_pct
                if state.stale_high_days is not None:
                    entry["stale_high_days"] = state.stale_high_days
                # 분할 익절 비활성화 ratio도 영속화 (재시작 시 글로벌 기본값 오적용 방어)
                entry["first_exit_ratio"] = state.first_exit_ratio if state.first_exit_ratio is not None else 0.0
                entry["second_exit_ratio"] = state.second_exit_ratio if state.second_exit_ratio is not None else 0.0
                entry["third_exit_ratio"] = state.third_exit_ratio if state.third_exit_ratio is not None else 0.0
            # ★ initial_qty: 최초 진입 수량 (부분 매도 후 덮어쓰기 방지)
            # - stage=NONE(신규/무매도): 현재 수량으로 갱신
            # - stage>NONE(익절 진행 중): 파일의 기존 initial_qty 보존
            #   (재시작 시 post-sell qty로 덮어쓰면 정합성 검증이 false positive 유발)
            if state.current_stage == ExitStage.NONE:
                entry["initial_qty"] = (
                    int(state.initial_quantity) if state.initial_quantity is not None
                    else int(state.original_quantity)
                )
            else:
                existing = self._persisted.get(sym, {}).get("initial_qty")
                if existing is not None:
                    entry["initial_qty"] = existing
            data[sym] = entry
        try:
            with open(self._stage_file, "w") as f:
                json.dump(data, f)
        except Exception as e:
            logger.warning(f"[ExitManager] stage 파일 저장 실패: {e}")

    def register_position(
        self,
        position: Position,
        stop_loss_pct: Optional[float] = None,
        trailing_stop_pct: Optional[float] = None,
        price_history: Optional[Dict[str, List[Decimal]]] = None,
        first_exit_pct: Optional[float] = None,
        second_exit_pct: Optional[float] = None,
        third_exit_pct: Optional[float] = None,
        first_exit_ratio: Optional[float] = None,
        second_exit_ratio: Optional[float] = None,
        third_exit_ratio: Optional[float] = None,
        stale_high_days: Optional[int] = None,
        is_core: bool = False,
        max_holding_days: Optional[int] = None,
        trailing_activate_pct: Optional[float] = None,
        atr_pct_hint: Optional[float] = None,
    ):
        """포지션 등록"""
        if position.symbol in self._states:
            state = self._states[position.symbol]
            if position.quantity != state.remaining_quantity:
                old_qty = state.remaining_quantity
                state.entry_price = position.avg_price
                state.original_quantity = position.quantity
                state.remaining_quantity = position.quantity
                if position.quantity > old_qty:
                    added = position.quantity - old_qty
                    pct_added = added / max(old_qty, 1)
                    new_high = position.current_price if position.current_price is not None and position.current_price > 0 else position.avg_price
                    state.highest_price = max(state.highest_price, new_high)
                    # 10% 이상 추가매수만 stage 리셋 (소량 sync 오차는 stage 유지)
                    if pct_added >= 0.10:
                        state.current_stage = ExitStage.NONE
                        state.breakeven_activated = False
                        logger.info(
                            f"[ExitManager] 추가매수 확인: {position.symbol} "
                            f"{old_qty}→{position.quantity}주 (+{pct_added*100:.0f}%), stage→NONE, BE→False"
                        )
                    else:
                        logger.debug(
                            f"[ExitManager] 수량 소폭 증가(sync): {position.symbol} "
                            f"{old_qty}→{position.quantity}주 (+{added}주, "
                            f"{pct_added*100:.1f}%), stage={state.current_stage.value} 유지"
                        )
                else:
                    logger.debug(
                        f"[ExitManager] 포지션 업데이트(부분매도): {position.symbol} "
                        f"{old_qty}주 -> {position.quantity}주, stage={state.current_stage.value} 유지"
                    )
            return

        # ATR 계산 및 동적 손절 설정 (코어홀딩은 ATR 동적 손절 비활성화 — 고정 SL 우선)
        atr_pct: Optional[float] = None
        dynamic_stop: Optional[float] = None
        if self.config.enable_dynamic_stop and price_history and not is_core:
            try:
                highs = price_history.get("high", [])
                lows = price_history.get("low", [])
                closes = price_history.get("close", [])

                if highs and lows and closes:
                    atr_pct = calculate_atr(highs, lows, closes, period=14)
                    if atr_pct is not None:
                        dynamic_stop = calculate_dynamic_stop_loss(
                            atr_pct,
                            min_stop=self.config.min_stop_pct,
                            max_stop=self.config.max_stop_pct,
                            multiplier=self.config.atr_multiplier
                        )
                        logger.info(
                            f"[ExitManager] {position.symbol} ATR 기반 손절: "
                            f"ATR={atr_pct:.2f}% -> 손절={dynamic_stop:.2f}%"
                        )
            except Exception as e:
                logger.warning(f"[ExitManager] {position.symbol} ATR 계산 실패: {e}")

        # price_history로 ATR 계산에 실패했거나 코어홀딩인 경우, 외부에서 전달된 hint를 사용
        if atr_pct is None and atr_pct_hint is not None and atr_pct_hint > 0:
            atr_pct = float(atr_pct_hint)

        # ── ATR 연동 트레일링 계산 ─────────────────────────────────────
        # effective_ts = min( max(config_ts, ATR × mult), atr_link_cap_pct )
        #   - config_ts : 전략별 trailing_stop_pct (없으면 글로벌 기본값)
        #   - ATR×mult  : 변동성 연동 트레일링 (매크로 노이즈 흡수)
        #   - cap_pct   : 상한선 (너무 커져 손실 확대 방지)
        # ATR이 없으면 None 저장 → 기존 방식 fallback
        effective_ts_pct: Optional[float] = None
        # 영속화된 상태에서 atr_pct/effective_ts 복원 (sync 재등록 + 재시작 경로)
        persisted_check = self._persisted.get(position.symbol, {})
        if atr_pct is None and persisted_check.get("atr_pct") is not None:
            atr_pct = float(persisted_check["atr_pct"])
        if (
            self.config.enable_atr_linked_trailing
            and atr_pct is not None
            and atr_pct > 0
            and not is_core  # 코어홀딩은 별도 고정 트레일링 우선
        ):
            base_ts = trailing_stop_pct if trailing_stop_pct is not None else self.config.trailing_stop_pct
            atr_based = float(atr_pct) * float(self.config.atr_link_multiplier)
            # 하한: config 기반 최소값 존중
            candidate = max(float(base_ts), atr_based)
            # 상한: cap_pct 초과 금지
            effective_ts_pct = min(candidate, float(self.config.atr_link_cap_pct))
            logger.info(
                f"[ExitManager] {position.symbol} ATR-linked 트레일링: "
                f"ATR={atr_pct:.2f}% × {self.config.atr_link_multiplier} = {atr_based:.2f}% / "
                f"config_ts={float(base_ts):.2f}% → effective={effective_ts_pct:.2f}% "
                f"(cap={self.config.atr_link_cap_pct}%)"
            )
        elif persisted_check.get("effective_trailing_stop_pct") is not None:
            # ATR 미전달이지만 영속화된 effective_ts가 있으면 복원 (재시작 경로)
            effective_ts_pct = float(persisted_check["effective_trailing_stop_pct"])
            logger.debug(
                f"[ExitManager] {position.symbol} effective_ts 복원: {effective_ts_pct:.2f}%"
            )

        # 전략별 익절 목표 우선, 없으면 글로벌 기본값
        eff_first = first_exit_pct if first_exit_pct is not None else self.config.first_exit_pct
        eff_second = second_exit_pct if second_exit_pct is not None else self.config.second_exit_pct
        eff_third = third_exit_pct if third_exit_pct is not None else self.config.third_exit_pct

        current_price = position.current_price if position.current_price is not None and position.current_price > 0 else position.avg_price
        persisted = self._persisted.get(position.symbol)

        saved_initial_qty: int = 0
        if persisted:
            try:
                initial_stage = ExitStage(persisted["stage"])
                saved_high = Decimal(str(persisted.get("highest_price", float(current_price))))
                breakeven_was = bool(persisted.get("breakeven_activated", False))
                saved_initial_qty = int(persisted.get("initial_qty", 0))
                # 코어홀딩 플래그/파라미터 복원 (파일에 저장된 값 우선)
                if persisted.get("is_core", False):
                    is_core = True
                if "max_holding_days" in persisted:
                    max_holding_days = persisted["max_holding_days"]
                if "trailing_activate_pct" in persisted:
                    trailing_activate_pct = persisted["trailing_activate_pct"]
                # 코어 포지션 전용 파라미터 복원 (strategy=None 재시작 방어)
                if is_core:
                    if "stop_loss_pct" in persisted and stop_loss_pct is None:
                        stop_loss_pct = persisted["stop_loss_pct"]
                    if "trailing_stop_pct" in persisted and trailing_stop_pct is None:
                        trailing_stop_pct = persisted["trailing_stop_pct"]
                    if "stale_high_days" in persisted and stale_high_days is None:
                        stale_high_days = persisted["stale_high_days"]
                    # 분할 익절 ratio 복원 (코어: 0.0으로 비활성화)
                    if "first_exit_ratio" in persisted and first_exit_ratio is None:
                        first_exit_ratio = persisted["first_exit_ratio"]
                    if "second_exit_ratio" in persisted and second_exit_ratio is None:
                        second_exit_ratio = persisted["second_exit_ratio"]
                    if "third_exit_ratio" in persisted and third_exit_ratio is None:
                        third_exit_ratio = persisted["third_exit_ratio"]

                # 재시작 시 고점 보정: 저장된 고점이 현재가보다 5% 초과 높으면
                # 즉시 트레일링 발동 위험 → 현재가로 리셋
                if current_price > 0 and saved_high > current_price:
                    gap_pct = float((saved_high - current_price) / current_price * 100)
                    if gap_pct > 5.0:
                        logger.warning(
                            f"[ExitManager] {position.symbol} 재시작 고점 보정: "
                            f"{saved_high:,.0f} → {current_price:,.0f} "
                            f"(괴리 {gap_pct:.1f}% > 5%, 즉시 트레일링 방지)"
                        )
                        saved_high = current_price

                # ★ 재시작 정합성 검증 (initial_qty가 파일에 있을 때만)
                # stage=FIRST/SECOND/THIRD인데 KIS 실제 잔고가 1차 익절 이후 예상 잔고보다 많으면
                # = 해당 stage의 매도가 실행되지 않은 것 → NONE으로 리셋해 재발행
                if (
                    saved_initial_qty > 0
                    and initial_stage not in (ExitStage.NONE, ExitStage.TRAILING)
                ):
                    eff_first_ratio = first_exit_ratio if first_exit_ratio is not None else self.config.first_exit_ratio
                    expected_after_first = saved_initial_qty - max(
                        1, int(saved_initial_qty * eff_first_ratio)
                    )
                    # 부분체결 허용: 5% 버퍼 (KIS 부분체결 시 수량 소폭 불일치 허용)
                    tolerance_qty = max(1, int(saved_initial_qty * 0.05))
                    if position.quantity > expected_after_first + tolerance_qty:
                        logger.warning(
                            f"[ExitManager] {position.symbol} 재시작 정합성 실패: "
                            f"KIS qty={position.quantity} > expected_after_1st={expected_after_first} "
                            f"(initial_qty={saved_initial_qty}, stage={initial_stage.value}) "
                            f"→ NONE 리셋 (익절 미실행 감지, 재발행 예정)"
                        )
                        initial_stage = ExitStage.NONE
                        breakeven_was = False

                logger.info(
                    f"[ExitManager] {position.symbol} stage 파일 복원: "
                    f"stage={initial_stage.value} / 고점={saved_high:,.0f} / BE={breakeven_was}"
                )
            except Exception as e:
                logger.warning(f"[ExitManager] {position.symbol} stage 복원 실패({e}), 추정으로 폴백")
                initial_stage = ExitStage.NONE
                saved_high = current_price
                breakeven_was = False
        else:
            saved_high = current_price
            breakeven_was = False
            initial_stage = ExitStage.NONE
            # 주의: persisted 없는 신규 포지션은 항상 NONE에서 시작
            # stage 추정 점프는 실제 매도 없이 stage만 올려 익절 누락 유발

        # initial_quantity: 파일에 저장된 최초 진입 수량 우선, 없으면 현재 KIS 잔고
        initial_qty_for_state = saved_initial_qty if saved_initial_qty > 0 else position.quantity

        self._states[position.symbol] = PositionExitState(
            symbol=position.symbol,
            entry_price=position.avg_price,
            original_quantity=position.quantity,
            remaining_quantity=position.quantity,
            current_stage=initial_stage,
            highest_price=saved_high,
            breakeven_activated=breakeven_was,
            stop_loss_pct=stop_loss_pct,
            trailing_stop_pct=trailing_stop_pct,
            first_exit_pct=first_exit_pct,
            second_exit_pct=second_exit_pct,
            third_exit_pct=third_exit_pct,
            first_exit_ratio=first_exit_ratio,
            second_exit_ratio=second_exit_ratio,
            third_exit_ratio=third_exit_ratio,
            atr_pct=atr_pct,
            dynamic_stop_pct=dynamic_stop,
            effective_trailing_stop_pct=effective_ts_pct,
            last_new_high_date=date.today(),
            stale_high_days=stale_high_days,
            initial_quantity=initial_qty_for_state,
            is_core=is_core,
            max_holding_days=max_holding_days,
            trailing_activate_pct=trailing_activate_pct,
        )

        # ── 장중 급락 active 시: 신규 포지션에도 즉시 crash SL/TS 적용 ──
        # apply_intraday_crash_params()는 중복 레벨 시 스킵되므로, 신규 등록 포지션은
        # 별도로 확인해야 함. 코어홀딩은 제외.
        if not is_core and self._intraday_crash_level != "normal":
            crash_p = INTRADAY_CRASH_PARAMS.get(self._intraday_crash_level, {})
            if crash_p:
                c_sl = crash_p["stop_loss_pct"]
                c_ts = crash_p["trailing_stop_pct"]
                state = self._states[position.symbol]
                tightened = []
                if state.stop_loss_pct is None or state.stop_loss_pct > c_sl:
                    state.stop_loss_pct = c_sl
                    tightened.append(f"SL={c_sl}%")
                if state.trailing_stop_pct is None or state.trailing_stop_pct > c_ts:
                    state.trailing_stop_pct = c_ts
                    tightened.append(f"TS={c_ts}%")
                if tightened:
                    logger.warning(
                        f"[장중급락] 신규 포지션 {position.symbol} "
                        f"crash({self._intraday_crash_level}) 즉시 적용: "
                        f"{', '.join(tightened)}"
                    )
        # ──────────────────────────────────────────────────────────────

        # 보유기간 체크용 진입 시간 기록
        if position.entry_time:
            self._entry_times[position.symbol] = position.entry_time
        elif position.symbol not in self._entry_times:
            self._entry_times[position.symbol] = datetime.now()

        effective_stop = dynamic_stop if dynamic_stop is not None else (stop_loss_pct if stop_loss_pct is not None else self.config.stop_loss_pct)
        eff_1st = first_exit_pct if first_exit_pct is not None else self.config.first_exit_pct
        eff_ts = trailing_stop_pct if trailing_stop_pct is not None else self.config.trailing_stop_pct
        _ts_label = (
            f"TS={effective_ts_pct:.2f}% (ATR-linked, base={eff_ts}%)"
            if effective_ts_pct is not None
            else f"TS={eff_ts}%"
        )
        logger.debug(
            f"[ExitManager] 포지션 등록: {position.symbol} "
            f"(SL={effective_stop:.2f}%, TP1={eff_1st:.1f}%, "
            f"{_ts_label}, "
            f"stage={initial_stage.value}, market={self.market})"
        )

        self._persist_states()

    def update_price(self, symbol: str, current_price: Decimal,
                     market_data: Optional[Dict[str, Any]] = None) -> Optional[Tuple[str, int, str]]:
        """
        가격 업데이트 및 청산 신호 확인

        Args:
            market_data: 복합 트레일링용 시장 데이터 (선택)
                - ma5: 5일 이동평균
                - prev_low: 전일 저가
                - high: 당일 고가
                - low: 당일 저가

        Returns:
            (action, quantity, reason) 또는 None
            action: "sell_partial" | "sell_all" | None
        """
        if symbol not in self._states:
            return None

        # 청산 예외 종목 스킵
        if symbol in self._exit_exempt:
            return None

        state = self._states[symbol]

        if state.remaining_quantity <= 0:
            return None

        if state.entry_price <= 0:
            return None

        # 고가 업데이트 + 신고가 일자 추적
        if current_price > state.highest_price:
            state.highest_price = current_price
            state.last_new_high_date = date.today()

        # 보유기간 계산 (영업일 기준)
        entry_time = self._entry_times.get(symbol)
        biz_days = 0
        if entry_time:
            biz_days = self._count_business_days(entry_time.date(), date.today())

        # 보유기간 초과 체크 (포지션별 max_holding_days 우선 적용)
        eff_max_holding = state.max_holding_days if state.max_holding_days is not None else self._max_holding_days
        if biz_days > 0 and eff_max_holding > 0:
            if biz_days > eff_max_holding:
                return self._create_exit(
                    state, "sell_all", state.remaining_quantity,
                    f"보유기간 초과: {biz_days}영업일 (최대 {eff_max_holding}영업일)"
                )

        # 순손익률 계산
        if self.config.include_fees:
            _, net_pnl_pct = self.fee_calc.calculate_net_pnl(
                state.entry_price, current_price, state.remaining_quantity
            )
            net_pnl_pct = float(net_pnl_pct)
        else:
            # US: zero-commission
            net_pnl_pct = float((current_price - state.entry_price) / state.entry_price * 100)

        # 횡보 조기 청산: N영업일 이상 보유 & |수익률| < X% & 1차 익절 전 (코어홀딩 제외)
        if (not state.is_core
            and biz_days >= self.config.stale_exit_days > 0
            and abs(net_pnl_pct) < self.config.stale_exit_pnl_pct
            and state.current_stage == ExitStage.NONE):
            return self._create_exit(
                state, "sell_all", state.remaining_quantity,
                f"횡보 청산: {biz_days}영업일 보유, "
                f"수익률 {net_pnl_pct:+.2f}% (±{self.config.stale_exit_pnl_pct}% 이내)"
            )

        # 익절 후 저효율 청산: 1차 익절 완료 이후 N영업일 보유 & 수익률 < X%
        # 이미 수익 확보(분할 매도)했으나 잔여 물량이 추세 없이 체류 → 기회비용 손실
        if (not state.is_core
            and state.current_stage not in (ExitStage.NONE, ExitStage.TRAILING)
            and self.config.post_exit_stale_days > 0
            and biz_days >= self.config.post_exit_stale_days
            and 0 < net_pnl_pct < self.config.post_exit_stale_pnl_pct):
            # 신고가 갱신 여부 체크: 최근 갱신 중이면 아직 추세 진행 → 스킵
            _days_since_high = 0
            if state.last_new_high_date is not None:
                _days_since_high = self._count_business_days(state.last_new_high_date, date.today())
            if _days_since_high >= 3:  # 3영업일 이상 신고가 미갱신 시에만 발동
                return self._create_exit(
                    state, "sell_all", state.remaining_quantity,
                    f"익절후 저효율: {biz_days}영업일 보유, stage={state.current_stage.value}, "
                    f"수익률 {net_pnl_pct:+.2f}% (< {self.config.post_exit_stale_pnl_pct}%), "
                    f"신고가 {_days_since_high}일 전"
                )

        # 신고가 실패 무효화 (추세 소멸): N영업일 신고가 갱신 없음 & PnL 미달 & 1차 익절 전 (코어홀딩 제외)
        eff_stale_high = state.stale_high_days if state.stale_high_days is not None else self.config.stale_high_days
        if (not state.is_core
            and eff_stale_high > 0
            and state.last_new_high_date is not None
            and state.current_stage == ExitStage.NONE):
            days_since_high = self._count_business_days(state.last_new_high_date, date.today())
            if (days_since_high >= eff_stale_high
                and net_pnl_pct < self.config.stale_high_min_pnl_pct):
                return self._create_exit(
                    state, "sell_all", state.remaining_quantity,
                    f"추세 무효화: {days_since_high}영업일 신고가 실패, "
                    f"수익률 {net_pnl_pct:+.2f}% (< {self.config.stale_high_min_pnl_pct}%)"
                )

        # 1. 손절 체크
        sl_pct = state.dynamic_stop_pct if state.dynamic_stop_pct is not None else (state.stop_loss_pct if state.stop_loss_pct is not None else self.config.stop_loss_pct)
        sl_pct = max(sl_pct, self.config.min_stop_pct)
        if net_pnl_pct <= -sl_pct:
            atr_info = f", ATR={state.atr_pct:.2f}%" if state.atr_pct is not None else ""
            return self._create_exit(
                state, "sell_all", state.remaining_quantity,
                f"손절: {net_pnl_pct:.2f}% (SL={sl_pct:.2f}%{atr_info})"
            )

        # 2. 분할 익절
        if self.config.enable_partial_exit:
            exit_signal = self._check_partial_exit(state, current_price, net_pnl_pct)
            if exit_signal:
                return exit_signal

        # 3. 1R 본전 이동 체크
        # 코어홀딩: 분할익절 비활성(ratio=0) → stage가 NONE에서 안 올라감
        #   → trailing_activate_pct 도달 시 직접 breakeven 활성화
        _be_eligible = (state.current_stage != ExitStage.NONE) or state.is_core
        if _be_eligible and not state.breakeven_activated:
            if state.is_core:
                # 코어: trailing_activate_pct(10%) 도달 시 본전 보호 활성화
                _be_threshold = state.trailing_activate_pct if state.trailing_activate_pct is not None else self.config.trailing_activate_pct
            else:
                # 일반: 1R(손절폭) 도달 시 본전 보호 활성화
                _be_threshold = state.dynamic_stop_pct if state.dynamic_stop_pct is not None else (state.stop_loss_pct if state.stop_loss_pct is not None else self.config.stop_loss_pct)
            if net_pnl_pct >= _be_threshold:
                state.breakeven_activated = True
                # 코어: breakeven 활성화 시점에서 trailing 기준점(고점)을 현재가로 리셋
                # → 활성화 직후 고점 괴리에 의한 즉시 트레일링 발동 방지
                if state.is_core and current_price < state.highest_price:
                    logger.info(
                        f"[ExitManager] {symbol} 코어 고점 리셋: "
                        f"{state.highest_price:,.0f} → {current_price:,.0f} (BE 활성화 시점)"
                    )
                    state.highest_price = current_price
                self._persist_states()
                logger.info(
                    f"[ExitManager] {symbol} 본전보호 활성화: 임계값 {_be_threshold:.1f}% 도달 "
                    f"(현재 +{net_pnl_pct:.2f}%, core={state.is_core})"
                )

        # 4. 트레일링 스탑
        if state.highest_price <= 0:
            return None

        if state.breakeven_activated:
            atr_trail = (state.atr_pct if state.atr_pct is not None else 2.0) * self.config.atr_trailing_multiplier
            base_trail = state.trailing_stop_pct if state.trailing_stop_pct is not None else self.config.trailing_stop_pct
            # ATR-linked effective_trailing 존재 시 우선 반영 (register 시 계산된 하한+상한 적용값)
            if state.effective_trailing_stop_pct is not None and state.effective_trailing_stop_pct > 0:
                ts_pct_used = max(atr_trail, float(state.effective_trailing_stop_pct))
                _trail_src = "ATR-linked trailing"
            else:
                ts_pct_used = max(atr_trail, float(base_trail))
                _trail_src = "ATR트레일링"
            trail_from_high = float((current_price - state.highest_price) / state.highest_price * 100)

            if trail_from_high <= -ts_pct_used:
                # 3차 익절 완료(THIRD/TRAILING) 또는 코어홀딩(분할익절 없음) → 전량 매도
                # FIRST/SECOND → 분할 익절이 아직 남아있으므로 고점 리셋 (조기 전량 청산 방지)
                if state.current_stage in (ExitStage.THIRD, ExitStage.TRAILING) or state.is_core:
                    return self._create_exit(
                        state, "sell_all", state.remaining_quantity,
                        f"{_trail_src}: 고점 대비 {trail_from_high:.2f}% (한도=-{ts_pct_used:.1f}%)"
                    )
                else:
                    # FIRST/SECOND: 고점을 현재가로 리셋하여 분할 익절 기회 보존
                    logger.info(
                        f"[ExitManager] {symbol} {_trail_src} 도달({trail_from_high:.2f}%) "
                        f"but stage={state.current_stage.value} → 고점 리셋 "
                        f"({state.highest_price:,.0f} → {current_price:,.0f}), 분할 익절 우선"
                    )
                    state.highest_price = current_price
                    self._persist_states()

            # 본전 보호 (1차 익절 완료 또는 코어홀딩 — 분할 수익 확보 전 조기청산 방지)
            # Stage별 차등 버퍼: 초기 stage에서는 추세 공간 확보, 후기 stage에서는 수익 보호
            if state.current_stage != ExitStage.NONE or state.is_core:
                if state.is_core:
                    # 코어: 장기 보유 → 본전 보호 버퍼를 넓게 (-2% 허용)
                    sell_fee_buffer = -2.0
                elif state.current_stage == ExitStage.FIRST:
                    # 1차 익절 완료: 20% 이미 수익 확보 → 추세 추종 여유 부여
                    # ATR/MA5 트레일링이 기술적 청산 담당, 본전보호는 최소 수익 보호
                    # -0.5%: first_exit_ratio=0.2 기준 1차 익절 수익(5%×20%=1%) > 잔여 손실(0.5%×80%=0.4%)
                    sell_fee_buffer = -0.5
                elif state.current_stage == ExitStage.SECOND:
                    # 2차 익절 완료: 추가 수익 확보 → 버퍼 축소
                    sell_fee_buffer = -0.5
                else:
                    # THIRD/TRAILING: 수수료 보호 (KR 0.25%, US 0%)
                    sell_fee_buffer = 0.0 if self.market in ("US", "NASDAQ", "NYSE") else 0.25
                if net_pnl_pct <= sell_fee_buffer:
                    _stage_label = f"stage={state.current_stage.value}" if not state.is_core else "코어"
                    return self._create_exit(
                        state, "sell_all", state.remaining_quantity,
                        f"본전 이탈: {net_pnl_pct:+.2f}% ({_stage_label}, 버퍼={sell_fee_buffer}%)"
                    )

        elif net_pnl_pct >= (state.trailing_activate_pct if state.trailing_activate_pct is not None else self.config.trailing_activate_pct):
            trailing_pct = float((current_price - state.highest_price) / state.highest_price * 100)
            # ATR-linked effective_trailing이 있으면 우선 사용 (매크로 노이즈 흡수)
            if state.effective_trailing_stop_pct is not None and state.effective_trailing_stop_pct > 0:
                ts_pct = float(state.effective_trailing_stop_pct)
                _trail_src = "ATR-linked trailing"
            else:
                ts_pct = float(state.trailing_stop_pct if state.trailing_stop_pct is not None else self.config.trailing_stop_pct)
                _trail_src = "트레일링"
            if trailing_pct <= -ts_pct:
                return self._create_exit(
                    state, "sell_all", state.remaining_quantity,
                    f"{_trail_src}: 고점 대비 {trailing_pct:.2f}% (한도=-{ts_pct:.1f}%)"
                )

        # 5. 복합 트레일링 (MA5 + 전일저가 기반, breakeven 여부 무관, stage >= min_stage)
        # breakeven 블록과 독립 — 1차 익절 직후 가격 하락으로 breakeven 미활성 시에도 작동
        composite_exit = self._check_composite_trailing(state, symbol, current_price, market_data)
        if composite_exit:
            return composite_exit

        return None

    def _check_composite_trailing(
        self,
        state: PositionExitState,
        symbol: str,
        current_price: Decimal,
        market_data: Optional[Dict[str, Any]],
    ) -> Optional[Tuple[str, int, str]]:
        """복합 트레일링: MA5/전일저가 기반 기술적 지지선 청산"""
        if not self.config.enable_composite_trailing:
            return None
        if state.is_core:
            return None
        if market_data is None:
            return None

        # 최소 적용 단계 체크
        min_stage = self._COMPOSITE_STAGE_MAP.get(
            self.config.composite_trail_min_stage, ExitStage.FIRST
        )
        cur_idx = self.STAGE_ORDER.index(state.current_stage) if state.current_stage in self.STAGE_ORDER else 0
        min_idx = self.STAGE_ORDER.index(min_stage) if min_stage in self.STAGE_ORDER else 1
        if cur_idx < min_idx:
            return None

        price_f = float(current_price)

        # 조건 1: MA5 이탈
        ma5 = market_data.get("ma5")
        if ma5 is not None:
            ma5_f = float(ma5)
            if ma5_f > 0:
                buffer = self.config.composite_ma5_buffer_pct
                threshold = ma5_f * (1 - buffer / 100)
                if price_f < threshold:
                    logger.info(
                        f"[ExitManager] {symbol} 복합트레일링 MA5 이탈: "
                        f"현재가 {price_f:,.0f} < MA5({ma5_f:,.0f}) - {buffer}% = {threshold:,.0f}"
                    )
                    return self._create_exit(
                        state, "sell_all", state.remaining_quantity,
                        f"복합트레일링: MA5({ma5_f:,.0f}) - {buffer}% 이탈"
                    )

        # 조건 2: 전일저가 이탈
        if self.config.composite_prev_low_enabled:
            prev_low = market_data.get("prev_low")
            day_low = market_data.get("low")
            if prev_low is not None and day_low is not None:
                prev_low_f = float(prev_low)
                day_low_f = float(day_low)
                if prev_low_f > 0 and day_low_f > 0 and day_low_f < prev_low_f:
                    if price_f < prev_low_f:
                        logger.info(
                            f"[ExitManager] {symbol} 복합트레일링 전일저가 이탈: "
                            f"현재가 {price_f:,.0f} < 전일저가 {prev_low_f:,.0f} "
                            f"(당일저가 {day_low_f:,.0f})"
                        )
                        return self._create_exit(
                            state, "sell_all", state.remaining_quantity,
                            f"복합트레일링: 전일저가({prev_low_f:,.0f}) 이탈"
                        )

        return None

    def _check_partial_exit(
        self,
        state: PositionExitState,
        current_price: Decimal,
        net_pnl_pct: float
    ) -> Optional[Tuple[str, int, str]]:
        """분할 익절 체크 (3단계, 전략별 목표 우선)"""
        # 코어홀딩은 분할 익절 비활성화 (ratio 복원 실패 시에도 안전)
        if state.is_core:
            return None

        first_pct = state.first_exit_pct if state.first_exit_pct is not None else self.config.first_exit_pct
        second_pct = state.second_exit_pct if state.second_exit_pct is not None else self.config.second_exit_pct
        third_pct = state.third_exit_pct if state.third_exit_pct is not None else self.config.third_exit_pct

        # 전략별 분할 비율 (코어+트레이더 구조 지원)
        first_ratio = state.first_exit_ratio if state.first_exit_ratio is not None else self.config.first_exit_ratio
        second_ratio = state.second_exit_ratio if state.second_exit_ratio is not None else self.config.second_exit_ratio
        third_ratio = state.third_exit_ratio if state.third_exit_ratio is not None else self.config.third_exit_ratio

        # ratio=0이면 해당 단계 분할 익절 비활성화 (코어홀딩 등)
        if first_ratio <= 0 and second_ratio <= 0 and third_ratio <= 0:
            return None

        # 1차 익절
        # pending_stage가 이미 있으면 fill 대기 중 → 중복 신호 방지
        if state.current_stage == ExitStage.NONE and state.pending_stage is None:
            if first_ratio <= 0:
                pass  # 분할 익절 비활성화 → 스킵
            elif net_pnl_pct >= first_pct:
                # remaining_quantity 기준 (sync 복원 시 original과 괴리 방지)
                exit_qty = max(1, int(state.remaining_quantity * first_ratio))
                exit_qty = min(exit_qty, state.remaining_quantity)

                # ★ stage는 fill 확인 후(on_fill)에만 advance
                # 재시작 시 pending_stage=None → current_stage=NONE → 자동 재발행 보장
                state.pending_stage = ExitStage.FIRST
                action = "sell_all" if exit_qty >= state.remaining_quantity else "sell_partial"
                return self._create_exit(
                    state, action, exit_qty,
                    f"1차 익절 ({first_ratio*100:.0f}%): {net_pnl_pct:.2f}% (목표={first_pct:.1f}%)"
                )

        # 2차 익절
        elif state.current_stage == ExitStage.FIRST and state.pending_stage is None:
            if second_ratio <= 0:
                pass  # 분할 익절 비활성화
            elif net_pnl_pct >= second_pct:
                exit_qty = max(1, int(state.remaining_quantity * second_ratio))
                exit_qty = min(exit_qty, state.remaining_quantity)

                state.pending_stage = ExitStage.SECOND
                action = "sell_all" if exit_qty >= state.remaining_quantity else "sell_partial"
                return self._create_exit(
                    state, action, exit_qty,
                    f"2차 익절 ({second_ratio*100:.0f}%): {net_pnl_pct:.2f}% (목표={second_pct:.1f}%)"
                )

        # 3차 익절
        elif state.current_stage == ExitStage.SECOND and state.pending_stage is None:
            if third_ratio <= 0:
                pass  # 분할 익절 비활성화
            elif net_pnl_pct >= third_pct:
                exit_qty = max(1, int(state.remaining_quantity * third_ratio))
                exit_qty = min(exit_qty, state.remaining_quantity)

                state.pending_stage = ExitStage.THIRD
                action = "sell_all" if exit_qty >= state.remaining_quantity else "sell_partial"
                return self._create_exit(
                    state, action, exit_qty,
                    f"3차 익절 ({third_ratio*100:.0f}%): {net_pnl_pct:.2f}% (목표={third_pct:.1f}%)"
                )

        # 3차 익절 완료 후 트레일링으로 전환
        elif state.current_stage == ExitStage.THIRD:
            if net_pnl_pct >= third_pct + 1.0:
                state.current_stage = ExitStage.TRAILING
                self._persist_states()
                logger.info(
                    f"[ExitManager] {state.symbol} 트레일링 단계 진입 "
                    f"(+{net_pnl_pct:.2f}%, 고점={state.highest_price:,.0f})"
                )

        return None

    def _create_exit(
        self,
        state: PositionExitState,
        action: str,
        quantity: int,
        reason: str
    ) -> Tuple[str, int, str]:
        """청산 신호 생성"""
        state.exit_history.append({
            "timestamp": datetime.now().isoformat(),
            "action": action,
            "quantity": quantity,
            "reason": reason,
            "remaining_before": state.remaining_quantity,
        })

        # stage 변경 즉시 영속화 (재시작 시 복원용)
        self._persist_states()

        return (action, quantity, reason)

    def rollback_stage(self, symbol: str) -> bool:
        """주문 실패/타임아웃 시 stage 롤백
        - pending_stage 있으면: pending만 클리어 (current_stage는 그대로 — fill 전이므로 정상)
        - pending_stage 없으면: current_stage를 이전 단계로 되돌림 (레거시 호환)
        """
        if symbol not in self._states:
            return False

        state = self._states[symbol]

        # ★ pending_stage가 있으면 fill 전이므로 pending만 클리어
        # current_stage는 변경하지 않음 (이미 NONE/FIRST/... 그대로)
        if state.pending_stage is not None:
            prev_pending = state.pending_stage
            state.pending_stage = None
            self._persist_states()
            logger.warning(
                f"[ExitManager] {symbol} pending_stage 클리어: {prev_pending.value} "
                f"(주문 실패/타임아웃, fill 미수신 → current_stage={state.current_stage.value} 유지)"
            )
            return True

        # pending 없는 레거시 케이스: current_stage 한 단계 롤백
        prev_stage = state.current_stage
        stage_order = self.STAGE_ORDER
        try:
            idx = stage_order.index(state.current_stage)
        except ValueError:
            return False

        if idx > 0:
            state.current_stage = stage_order[idx - 1]
            self._persist_states()
            logger.warning(
                f"[ExitManager] {symbol} stage 롤백: {prev_stage.value} -> {state.current_stage.value} "
                f"(주문 실패로 복원, 영속화 완료)"
            )
            return True
        return False

    def apply_regime_params(self, regime: str, *, force: bool = False) -> int:
        """시장 레짐에 따라 기존 포지션의 청산 파라미터를 실시간 갱신.

        Args:
            regime: LLM 레짐 분류 결과
                    (trending_bull / neutral / ranging / turning_point / trending_bear)
            force:  True이면 레짐 변경 없어도 강제 재적용 (장중 급락 해제 복원 등)

        Returns:
            갱신된 포지션 수

        적용 규칙 (stage별 차등):
            stop_loss_pct, trailing_stop_pct, stale_high_days — 모든 stage 즉시 적용
            first_exit_pct  — stage=NONE 만 (FIRST+ 이상은 이미 1차 익절 완료)
            second_exit_pct — stage ≤ FIRST
            third_exit_pct  — stage ≤ SECOND

        새 포지션에도 반영되도록 ExitConfig 글로벌 기본값도 함께 갱신.
        """
        params = REGIME_EXIT_PARAMS.get(regime)
        if not params:
            logger.warning(f"[레짐파라미터] 알 수 없는 레짐: {regime!r} → 적용 생략")
            return 0

        if not force and regime == self._current_regime and self._states:
            logger.debug(f"[레짐파라미터] 레짐 변경 없음 ({regime}) → 스킵")
            return 0

        prev_regime = self._current_regime
        self._current_regime = regime

        # ── 글로벌 ExitConfig 갱신 (신규 포지션 기본값) ──────────
        self.config.stop_loss_pct     = params["stop_loss_pct"]
        self.config.trailing_stop_pct = params["trailing_stop_pct"]
        self.config.first_exit_pct    = params["first_exit_pct"]
        self.config.second_exit_pct   = params["second_exit_pct"]
        self.config.third_exit_pct    = params["third_exit_pct"]
        self.config.stale_high_days   = params["stale_high_days"]

        # ── 기존 포지션 개별 갱신 ─────────────────────────────────
        stage_order = self.STAGE_ORDER  # noqa: 하위 호환 변수명 유지
        updated = 0

        for sym, state in self._states.items():
            # 코어홀딩 포지션은 레짐 오버라이드에서 제외
            if state.is_core:
                logger.debug(f"[레짐파라미터] {sym} 코어홀딩 → 레짐 오버라이드 스킵")
                continue

            changed: List[str] = []

            # 항상 적용: 손절 / 트레일링 / stale_high_days
            if state.stop_loss_pct != params["stop_loss_pct"]:
                state.stop_loss_pct = params["stop_loss_pct"]
                changed.append(f"SL={state.stop_loss_pct}%")

            if state.trailing_stop_pct != params["trailing_stop_pct"]:
                state.trailing_stop_pct = params["trailing_stop_pct"]
                changed.append(f"TS={state.trailing_stop_pct}%")

            if state.stale_high_days != params["stale_high_days"]:
                state.stale_high_days = params["stale_high_days"]
                changed.append(f"stale={state.stale_high_days}d")

            # stage별 조건부 익절 목표 갱신
            cur_idx = stage_order.index(state.current_stage) if state.current_stage in stage_order else 0

            if cur_idx <= 0:  # NONE → 1차 익절 목표 갱신 가능
                if state.first_exit_pct != params["first_exit_pct"]:
                    state.first_exit_pct = params["first_exit_pct"]
                    changed.append(f"TP1={state.first_exit_pct}%")

            if cur_idx <= 1:  # NONE / FIRST → 2차 익절 목표 갱신 가능
                if state.second_exit_pct != params["second_exit_pct"]:
                    state.second_exit_pct = params["second_exit_pct"]
                    changed.append(f"TP2={state.second_exit_pct}%")

            if cur_idx <= 2:  # NONE / FIRST / SECOND → 3차 익절 목표 갱신 가능
                if state.third_exit_pct != params["third_exit_pct"]:
                    state.third_exit_pct = params["third_exit_pct"]
                    changed.append(f"TP3={state.third_exit_pct}%")

            if changed:
                updated += 1
                logger.info(
                    f"[레짐파라미터] {sym} ({state.current_stage.value}) "
                    f"← {prev_regime}→{regime}: {', '.join(changed)}"
                )

        if updated:
            self._persist_states()

        logger.info(
            f"[레짐파라미터] {prev_regime} → {regime} 적용 완료: "
            f"{updated}/{len(self._states)}개 포지션 갱신 "
            f"(SL={params['stop_loss_pct']}%, TS={params['trailing_stop_pct']}%, "
            f"TP1/2/3={params['first_exit_pct']}/{params['second_exit_pct']}/{params['third_exit_pct']}%)"
        )

        # ── 장중 급락 override 재적용 ──────────────────────────────────
        # 레짐 재분류(12:00 LLM, 30분 sync)가 intraday crash 설정을 덮어쓰는 버그 방지.
        # crash가 active이면 SL/TS를 다시 조임 (TP는 레짐값 유지).
        if self._intraday_crash_level != "normal":
            crash_p = INTRADAY_CRASH_PARAMS.get(self._intraday_crash_level, {})
            if crash_p:
                c_sl = crash_p["stop_loss_pct"]
                c_ts = crash_p["trailing_stop_pct"]
                if self.config.stop_loss_pct > c_sl:
                    self.config.stop_loss_pct = c_sl
                if self.config.trailing_stop_pct > c_ts:
                    self.config.trailing_stop_pct = c_ts
                for sym2, state2 in self._states.items():
                    if not state2.is_core:
                        if state2.stop_loss_pct > c_sl:
                            state2.stop_loss_pct = c_sl
                        if state2.trailing_stop_pct > c_ts:
                            state2.trailing_stop_pct = c_ts
                logger.warning(
                    f"[장중급락] 레짐({regime}) 적용 후 crash({self._intraday_crash_level}) "
                    f"재적용 → SL ≤ {c_sl}%, TS ≤ {c_ts}%"
                )
        # ─────────────────────────────────────────────────────────────────
        return updated

    def apply_intraday_crash_params(self, crash_level: str) -> int:
        """장중 급락 단계에 따라 SL/trailing 즉시 조임.

        TP 목표치는 변경하지 않음 — 레짐 파라미터 유지.
        코어홀딩 포지션(is_core=True) 제외.

        Args:
            crash_level: "caution" | "crash" | "severe"

        Returns:
            갱신된 포지션 수
        """
        params = INTRADAY_CRASH_PARAMS.get(crash_level)
        if not params:
            logger.warning(f"[장중급락] 알 수 없는 crash_level: {crash_level!r}")
            return 0

        if crash_level == self._intraday_crash_level:
            return 0  # 변경 없음

        prev_level = self._intraday_crash_level
        self._intraday_crash_level = crash_level

        # 글로벌 ExitConfig도 갱신 (신규 포지션에 적용)
        new_sl  = params["stop_loss_pct"]
        new_ts  = params["trailing_stop_pct"]
        self.config.stop_loss_pct     = new_sl
        self.config.trailing_stop_pct = new_ts

        updated = 0
        for sym, state in self._states.items():
            if state.is_core:
                continue  # 코어홀딩 제외
            changed: List[str] = []
            if state.stop_loss_pct > new_sl:   # 더 타이트할 때만 덮어씀
                state.stop_loss_pct = new_sl
                changed.append(f"SL={new_sl}%")
            if state.trailing_stop_pct > new_ts:
                state.trailing_stop_pct = new_ts
                changed.append(f"TS={new_ts}%")
            if changed:
                updated += 1
                logger.warning(
                    f"[장중급락] {sym} SL/TS 강화: {', '.join(changed)}"
                )

        if updated:
            self._persist_states()

        logger.warning(
            f"[장중급락] {prev_level} → {crash_level} 파라미터 적용: "
            f"{updated}/{len(self._states)}개 포지션 (SL={new_sl}%, TS={new_ts}%)"
        )
        return updated

    def recover_from_intraday_crash(self) -> None:
        """장중 급락 해제 — 현재 레짐 파라미터로 복원.

        intraday_crash_level이 normal이 아닌 상태에서 KOSPI가 회복될 때 호출.
        _current_regime 기준으로 apply_regime_params() 재실행.
        """
        if self._intraday_crash_level == "normal":
            return
        prev_level = self._intraday_crash_level
        self._intraday_crash_level = "normal"
        # force=True: 레짐 변경 없어도 강제 재적용 (crash로 조인 SL/TS 복원)
        self.apply_regime_params(self._current_regime, force=True)
        logger.info(f"[장중급락] {prev_level} → normal 해제: 레짐 {self._current_regime!r} 복원")

    def on_fill(self, symbol: str, sold_quantity: int, fill_price: Decimal):
        """체결 후 상태 업데이트"""
        if symbol not in self._states:
            return

        state = self._states[symbol]

        if sold_quantity > state.remaining_quantity:
            logger.warning(
                f"[ExitManager] {symbol} 매도수량({sold_quantity}) > 보유수량({state.remaining_quantity}), 보정"
            )
            sold_quantity = state.remaining_quantity

        # 실현 손익 계산
        pnl, _ = self.fee_calc.calculate_net_pnl(
            state.entry_price, fill_price, sold_quantity
        )
        state.total_realized_pnl += pnl
        state.remaining_quantity -= sold_quantity

        # ★ fill 확인 후 pending_stage → current_stage 승격
        # 이 시점이 stage가 실제로 advance되는 유일한 지점
        if state.pending_stage is not None:
            prev_stage = state.current_stage
            state.current_stage = state.pending_stage
            state.pending_stage = None
            logger.info(
                f"[ExitManager] {symbol} fill 확인 → stage 승격: "
                f"{prev_stage.value} → {state.current_stage.value}"
            )

        logger.info(
            f"[ExitManager] {symbol} 청산: {sold_quantity}주 @ {fill_price:,.0f}, "
            f"실현손익: {pnl:+,.0f}, 남은 수량: {state.remaining_quantity}주"
        )

        if state.remaining_quantity <= 0:
            total_pnl = state.total_realized_pnl
            del self._states[symbol]
            self._entry_times.pop(symbol, None)
            self._persisted.pop(symbol, None)
            logger.info(f"[ExitManager] {symbol} 완전 청산, 총 실현손익: {total_pnl:+,.0f}")

        self._persist_states()

    def get_state(self, symbol: str) -> Optional[PositionExitState]:
        """포지션 청산 상태 조회"""
        return self._states.get(symbol)

    def get_all_states(self) -> Dict[str, PositionExitState]:
        """모든 포지션 상태 조회"""
        return self._states.copy()

    def remove_position(self, symbol: str) -> bool:
        """포지션 상태 제거 (유령 포지션 정리용)"""
        if symbol in self._states:
            del self._states[symbol]
            self._entry_times.pop(symbol, None)
            self._persisted.pop(symbol, None)
            self._persist_states()
            logger.debug(f"[ExitManager] 포지션 상태 제거 및 영속화: {symbol}")
            return True
        return False

    def _count_business_days(self, start_date: date, end_date: date) -> int:
        """start_date ~ end_date 사이의 영업일 수 (주말 제외, 공휴일은 KR만)"""
        if start_date >= end_date:
            return 0
        count = 0
        current = start_date + timedelta(days=1)
        is_kr = self.market == "KR"
        # KR: is_kr_market_holiday 사용 (주말+공휴일)
        # US: 주말만 제외 (exchange_calendars 의존성 회피)
        if is_kr:
            try:
                from ..utils.session import is_kr_market_holiday
                while current <= end_date:
                    if not is_kr_market_holiday(current):
                        count += 1
                    current += timedelta(days=1)
                return count
            except ImportError:
                pass
        # US 또는 KR 폴백: 주말만 제외
        while current <= end_date:
            if current.weekday() < 5:
                count += 1
            current += timedelta(days=1)
        return count

    def add_exit_exempt(self, symbol: str, reason: str = ""):
        """청산 예외 종목 추가 (익절/손절 비활성화)"""
        self._exit_exempt.add(symbol)
        logger.info(f"[ExitManager] 청산 예외 추가: {symbol} ({reason or '수동'})")

    def remove_exit_exempt(self, symbol: str):
        """청산 예외 종목 제거"""
        self._exit_exempt.discard(symbol)
        logger.info(f"[ExitManager] 청산 예외 해제: {symbol}")

    def is_exit_exempt(self, symbol: str) -> bool:
        """청산 예외 종목 여부"""
        return symbol in self._exit_exempt

    # US 호환 메서드
    def on_position_closed(self, symbol: str):
        """Clean up when position is fully closed (US 호환)"""
        self.remove_position(symbol)

    def get_stages(self) -> Dict[str, int]:
        """영속화용 스테이지 상태 반환 (US 호환)"""
        result = {}
        for sym, state in self._states.items():
            try:
                result[sym] = self.STAGE_ORDER.index(state.current_stage)
            except ValueError:
                result[sym] = 0
        return result

    def restore_stages(self, stages: Dict[str, int]):
        """재시작 시 스테이지 복원 (US 호환)
        
        주의: 두 파일(exit_stages_us_*.json과 highest_prices.json)에서 독립적으로
        stage를 복원하므로, 30초 이내 재시작 시 파일 간 시차가 발생할 수 있음.
        → 현재 stage보다 낮은 값으로 다운그레이드하지 않음 (최고값 우선).
        """
        for sym, stage_idx in stages.items():
            if sym in self._states and 0 <= stage_idx < len(self.STAGE_ORDER):
                try:
                    current_idx = self.STAGE_ORDER.index(self._states[sym].current_stage)
                except ValueError:
                    current_idx = 0
                # 현재보다 높은 단계만 적용 — 낮은 값으로 다운그레이드 금지
                if stage_idx > current_idx:
                    self._states[sym].current_stage = self.STAGE_ORDER[stage_idx]
                    logger.debug(
                        f"[ExitManager] {sym} restore_stages 업그레이드: "
                        f"{self.STAGE_ORDER[current_idx].value} → {self.STAGE_ORDER[stage_idx].value}"
                    )


# 전역 인스턴스
_exit_manager: Optional[ExitManager] = None
_us_exit_manager: Optional[ExitManager] = None


def get_exit_manager(market: str = "KR") -> ExitManager:
    """전역 청산 관리자"""
    global _exit_manager, _us_exit_manager
    if market.upper() in ("US", "NASDAQ", "NYSE", "AMEX"):
        if _us_exit_manager is None:
            _us_exit_manager = ExitManager(market="US")
        return _us_exit_manager
    else:
        if _exit_manager is None:
            _exit_manager = ExitManager(market="KR")
        return _exit_manager
