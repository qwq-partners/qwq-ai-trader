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
from typing import Dict, List, Optional, Tuple
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


@dataclass
class ExitConfig:
    """청산 설정 (KR/US 통합)"""
    # 분할 익절 설정
    enable_partial_exit: bool = True

    # 1차 익절
    first_exit_pct: float = 5.0       # 목표 수익률 (%)
    first_exit_ratio: float = 0.30    # 청산 비율

    # 2차 익절
    second_exit_pct: float = 10.0     # 목표 수익률 (%)
    second_exit_ratio: float = 0.50   # 잔여의 비율

    # 3차 익절
    third_exit_pct: float = 12.0      # 목표 수익률 (%)
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

    # 수수료 포함 계산 (KR=True, US=False)
    include_fees: bool = True

    # 최대 보유 기간 (영업일 기준)
    max_holding_days: int = 10

    # End-of-day close (US day trade 전용)
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
    # ATR 기반 동적 손절
    atr_pct: Optional[float] = None
    dynamic_stop_pct: Optional[float] = None
    # 1R 도달 후 본전 이동 트레일링
    breakeven_activated: bool = False


class ExitManager:
    """
    분할 익절/청산 관리자 (KR/US 통합)

    KR: 수수료를 포함한 순수익 기준으로 분할 익절 관리.
    US: zero-commission 기준 분할 익절 관리.
    """

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
        """현재 모든 포지션의 stage/highest_price를 파일에 저장."""
        data = {}
        for sym, state in self._states.items():
            data[sym] = {
                "stage": state.current_stage.value,
                "highest_price": float(state.highest_price),
                "breakeven_activated": state.breakeven_activated,
            }
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
                    new_high = position.current_price or position.avg_price
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

        # ATR 계산 및 동적 손절 설정
        atr_pct = None
        dynamic_stop = None
        if self.config.enable_dynamic_stop and price_history:
            try:
                highs = price_history.get("high", [])
                lows = price_history.get("low", [])
                closes = price_history.get("close", [])

                if highs and lows and closes:
                    atr_pct = calculate_atr(highs, lows, closes, period=14)
                    if atr_pct:
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

        # 전략별 익절 목표 우선, 없으면 글로벌 기본값
        eff_first = first_exit_pct or self.config.first_exit_pct
        eff_second = second_exit_pct or self.config.second_exit_pct
        eff_third = third_exit_pct or self.config.third_exit_pct

        current_price = position.current_price or position.avg_price
        persisted = self._persisted.get(position.symbol)

        if persisted:
            try:
                initial_stage = ExitStage(persisted["stage"])
                saved_high = Decimal(str(persisted.get("highest_price", float(current_price))))
                breakeven_was = bool(persisted.get("breakeven_activated", False))

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
            atr_pct=atr_pct,
            dynamic_stop_pct=dynamic_stop,
        )

        # 보유기간 체크용 진입 시간 기록
        if position.entry_time:
            self._entry_times[position.symbol] = position.entry_time
        elif position.symbol not in self._entry_times:
            self._entry_times[position.symbol] = datetime.now()

        effective_stop = dynamic_stop or stop_loss_pct or self.config.stop_loss_pct
        eff_1st = first_exit_pct or self.config.first_exit_pct
        logger.debug(
            f"[ExitManager] 포지션 등록: {position.symbol} "
            f"(SL={effective_stop:.2f}%, TP1={eff_1st:.1f}%, "
            f"TS={trailing_stop_pct or self.config.trailing_stop_pct}%, "
            f"stage={initial_stage.value}, market={self.market})"
        )

        self._persist_states()

    def update_price(self, symbol: str, current_price: Decimal) -> Optional[Tuple[str, int, str]]:
        """
        가격 업데이트 및 청산 신호 확인

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

        # 고가 업데이트
        if current_price > state.highest_price:
            state.highest_price = current_price

        # 보유기간 초과 체크 (영업일 기준)
        entry_time = self._entry_times.get(symbol)
        if entry_time and self._max_holding_days > 0:
            biz_days = self._count_business_days(entry_time.date(), date.today())
            if biz_days > self._max_holding_days:
                return self._create_exit(
                    state, "sell_all", state.remaining_quantity,
                    f"보유기간 초과: {biz_days}영업일 (최대 {self._max_holding_days}영업일)"
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

        # 1. 손절 체크
        sl_pct = state.dynamic_stop_pct or state.stop_loss_pct or self.config.stop_loss_pct
        sl_pct = max(sl_pct, self.config.min_stop_pct)
        if net_pnl_pct <= -sl_pct:
            atr_info = f", ATR={state.atr_pct:.2f}%" if state.atr_pct else ""
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
        if state.current_stage != ExitStage.NONE and not state.breakeven_activated:
            one_r = state.dynamic_stop_pct or state.stop_loss_pct or self.config.stop_loss_pct
            if net_pnl_pct >= one_r:
                state.breakeven_activated = True
                self._persist_states()
                logger.info(
                    f"[ExitManager] {symbol} 1R({one_r:.1f}%) 도달 -> 본전 이동 활성화 "
                    f"(현재 +{net_pnl_pct:.2f}%)"
                )

        # 4. 트레일링 스탑
        if state.highest_price <= 0:
            return None

        if state.breakeven_activated:
            atr_trail = (state.atr_pct or 2.0) * 1.5
            base_trail = state.trailing_stop_pct or self.config.trailing_stop_pct
            ts_pct_used = max(atr_trail, base_trail)
            trail_from_high = float((current_price - state.highest_price) / state.highest_price * 100)

            if trail_from_high <= -ts_pct_used:
                # 3차 익절 완료(THIRD/TRAILING) → 전량 매도
                # FIRST/SECOND → 분할 익절이 아직 남아있으므로 고점 리셋 (조기 전량 청산 방지)
                if state.current_stage in (ExitStage.THIRD, ExitStage.TRAILING):
                    return self._create_exit(
                        state, "sell_all", state.remaining_quantity,
                        f"ATR트레일링: 고점 대비 {trail_from_high:.2f}% (한도=-{ts_pct_used:.1f}%)"
                    )
                else:
                    # FIRST/SECOND: 고점을 현재가로 리셋하여 분할 익절 기회 보존
                    logger.info(
                        f"[ExitManager] {symbol} ATR트레일링 도달({trail_from_high:.2f}%) "
                        f"but stage={state.current_stage.value} → 고점 리셋 "
                        f"({state.highest_price:,.0f} → {current_price:,.0f}), 분할 익절 우선"
                    )
                    state.highest_price = current_price
                    self._persist_states()

            # 본전 보호 (1차 익절 완료 후에만 적용 — 분할 수익 확보 전 조기청산 방지)
            if state.current_stage != ExitStage.NONE:  # FIRST 이상 = 1차 익절 완료
                # KR 매도 수수료+세금 ≈ 0.213%, 여유분 포함 0.25%
                sell_fee_buffer = 0.0 if self.market in ("US", "NASDAQ", "NYSE") else 0.25
                if net_pnl_pct <= sell_fee_buffer:
                    return self._create_exit(
                        state, "sell_all", state.remaining_quantity,
                        f"본전 이탈: +{net_pnl_pct:.2f}% "
                        f"(1차 익절 완료 후 수수료 버퍼 {sell_fee_buffer}% 이하)"
                    )

        elif net_pnl_pct >= self.config.trailing_activate_pct:
            trailing_pct = float((current_price - state.highest_price) / state.highest_price * 100)
            ts_pct = state.trailing_stop_pct or self.config.trailing_stop_pct
            if trailing_pct <= -ts_pct:
                return self._create_exit(
                    state, "sell_all", state.remaining_quantity,
                    f"트레일링: 고점 대비 {trailing_pct:.2f}%"
                )

        return None

    def _check_partial_exit(
        self,
        state: PositionExitState,
        current_price: Decimal,
        net_pnl_pct: float
    ) -> Optional[Tuple[str, int, str]]:
        """분할 익절 체크 (3단계, 전략별 목표 우선)"""

        first_pct = state.first_exit_pct if state.first_exit_pct is not None else self.config.first_exit_pct
        second_pct = state.second_exit_pct if state.second_exit_pct is not None else self.config.second_exit_pct
        third_pct = state.third_exit_pct if state.third_exit_pct is not None else self.config.third_exit_pct

        # 1차 익절
        if state.current_stage == ExitStage.NONE:
            if net_pnl_pct >= first_pct:
                # remaining_quantity 기준 (sync 복원 시 original과 괴리 방지)
                exit_qty = max(1, int(state.remaining_quantity * self.config.first_exit_ratio))
                exit_qty = min(exit_qty, state.remaining_quantity)

                state.current_stage = ExitStage.FIRST
                action = "sell_all" if exit_qty >= state.remaining_quantity else "sell_partial"
                return self._create_exit(
                    state, action, exit_qty,
                    f"1차 익절 ({self.config.first_exit_ratio*100:.0f}%): {net_pnl_pct:.2f}% (목표={first_pct:.1f}%)"
                )

        # 2차 익절
        elif state.current_stage == ExitStage.FIRST:
            if net_pnl_pct >= second_pct:
                exit_qty = max(1, int(state.remaining_quantity * self.config.second_exit_ratio))
                exit_qty = min(exit_qty, state.remaining_quantity)

                state.current_stage = ExitStage.SECOND
                action = "sell_all" if exit_qty >= state.remaining_quantity else "sell_partial"
                return self._create_exit(
                    state, action, exit_qty,
                    f"2차 익절 ({self.config.second_exit_ratio*100:.0f}%): {net_pnl_pct:.2f}% (목표={second_pct:.1f}%)"
                )

        # 3차 익절
        elif state.current_stage == ExitStage.SECOND:
            if net_pnl_pct >= third_pct:
                exit_qty = max(1, int(state.remaining_quantity * self.config.third_exit_ratio))
                exit_qty = min(exit_qty, state.remaining_quantity)

                state.current_stage = ExitStage.THIRD
                action = "sell_all" if exit_qty >= state.remaining_quantity else "sell_partial"
                return self._create_exit(
                    state, action, exit_qty,
                    f"3차 익절 ({self.config.third_exit_ratio*100:.0f}%): {net_pnl_pct:.2f}% (목표={third_pct:.1f}%)"
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
        """주문 실패 시 stage를 이전 단계로 되돌림"""
        if symbol not in self._states:
            return False

        state = self._states[symbol]
        prev_stage = state.current_stage

        stage_order = [ExitStage.NONE, ExitStage.FIRST, ExitStage.SECOND,
                       ExitStage.THIRD, ExitStage.TRAILING]
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
            stage_order = [ExitStage.NONE, ExitStage.FIRST, ExitStage.SECOND,
                           ExitStage.THIRD, ExitStage.TRAILING]
            try:
                result[sym] = stage_order.index(state.current_stage)
            except ValueError:
                result[sym] = 0
        return result

    def restore_stages(self, stages: Dict[str, int]):
        """재시작 시 스테이지 복원 (US 호환)
        
        주의: 두 파일(exit_stages_us_*.json과 highest_prices.json)에서 독립적으로
        stage를 복원하므로, 30초 이내 재시작 시 파일 간 시차가 발생할 수 있음.
        → 현재 stage보다 낮은 값으로 다운그레이드하지 않음 (최고값 우선).
        """
        stage_order = [ExitStage.NONE, ExitStage.FIRST, ExitStage.SECOND,
                       ExitStage.THIRD, ExitStage.TRAILING]
        for sym, stage_idx in stages.items():
            if sym in self._states and 0 <= stage_idx < len(stage_order):
                try:
                    current_idx = stage_order.index(self._states[sym].current_stage)
                except ValueError:
                    current_idx = 0
                # 현재보다 높은 단계만 적용 — 낮은 값으로 다운그레이드 금지
                if stage_idx > current_idx:
                    self._states[sym].current_stage = stage_order[stage_idx]
                    logger.debug(
                        f"[ExitManager] {sym} restore_stages 업그레이드: "
                        f"{stage_order[current_idx].value} → {stage_order[stage_idx].value}"
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
