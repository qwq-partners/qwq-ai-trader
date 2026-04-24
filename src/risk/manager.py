"""
QWQ AI Trader - 리스크 관리자 (통합)

KR/US 통합 리스크 관리자.
포지션 크기 계산, 손절/익절 관리, 일일 손실 제한.

KR 특화: 대형주 손절 완화, 당일 재진입 금지, 차등 리스크 관리
US 특화: 적응형 사이징 (연속 손실 축소), 섹터 제한
"""

from dataclasses import dataclass, field
from datetime import datetime, date
from decimal import Decimal
from typing import Dict, List, Optional, Tuple, Any
from loguru import logger
import json
from pathlib import Path

from ..core.types import (
    Order, Position, Portfolio, RiskMetrics, RiskConfig,
    OrderSide, SignalStrength, Signal
)
from ..core.event import (
    SignalEvent, FillEvent, RiskAlertEvent, StopTriggeredEvent,
    Event, EventType
)


@dataclass
class DailyStats:
    """일일 거래 통계"""
    date: date = field(default_factory=date.today)
    trades: int = 0
    wins: int = 0
    losses: int = 0
    total_pnl: Decimal = Decimal("0")
    max_drawdown: Decimal = Decimal("0")
    consecutive_losses: int = 0
    peak_equity: Decimal = Decimal("0")


class RiskManager:
    """
    리스크 관리자 (KR/US 통합)

    주요 기능:
    - 포지션 크기 계산
    - 손절/익절 가격 계산
    - 일일 손실 제한 체크
    - 최대 포지션 수 제한
    - 연속 손실 관리
    - 당일 재진입 금지 (KR)
    - 차등 리스크 관리 (KR)
    - 적응형 사이징 (US)
    """

    def __init__(self, config: RiskConfig, initial_capital: Decimal, market: str = "KR"):
        self.config = config
        self.initial_capital = initial_capital
        self.market = market.upper()

        # 일일 손익 저장 경로
        cache_dir = Path.home() / ".cache" / "ai_trader"
        cache_dir.mkdir(parents=True, exist_ok=True)
        market_suffix = f"_{self.market.lower()}" if self.market != "KR" else ""
        self._daily_stats_path = cache_dir / f"daily_stats{market_suffix}.json"

        # 리스크 메트릭스
        self.metrics = RiskMetrics()

        # 일일 통계
        self.daily_stats = DailyStats(peak_equity=initial_capital)

        # 일일 손익 로드 (프로세스 재시작 시)
        self._load_daily_stats()

        # 경고 임계값 (동적 계산 -- 하드코딩 금지)
        self._warn_threshold_pct = config.daily_max_loss_pct * 0.7

        # 당일 재진입 금지 (KR 전용): 손절된 종목
        self._stop_loss_today_path = cache_dir / f"stop_loss_today{market_suffix}.json"
        self._stop_loss_today: set = self._load_stop_loss_today()

        # 당일 청산 종목 재진입 제한 (KR): 눌림/재돌파 확인 필요
        self._exited_today_path = cache_dir / f"exited_today{market_suffix}.json"
        self._exited_today: Dict[str, Dict] = self._load_exited_today()

        # 연속 손실 카운터 (US 적응형 사이징)
        self._consecutive_losses: int = 0

        # 시장 추세 연동 사이드카 (KR 전용)
        # 스케줄러가 장중 주기적으로 갱신 → _is_daily_loss_limit_hit에서 참조
        self._market_trend: dict = {}  # {"kospi_pct": float, "kosdaq_pct": float, "recovering": bool, "ts": datetime}
        self._sidecar_active: bool = False  # 사이드카 활성 상태

        # 포트폴리오 동기화 장애 프로토콜 (연속 3회 실패 시 매수 차단)
        # trading_lock: sync_healthy=False 상태에서 신규 매수 진입 차단
        self._sync_healthy: bool = True
        self._sync_fail_count: int = 0
        self._sync_fail_threshold: int = 3
        # 차단 시작 시각 (타임아웃 안전장치용)
        self._sync_unhealthy_since: Optional[datetime] = None
        # 차단 지속 한도 (분): 초과 시 강제 해제 + CRITICAL 경고
        self._sync_timeout_minutes: int = 10
        # 차단 로그 스팸 방지 쿨다운
        self._last_sync_block_log: Dict[str, datetime] = {}

        # 당일 청산 누적 쿨다운 (D+1 분리)
        # 4/14 -8.42% 사고 대응: 같은 날 다수 청산 + 다수 신규 매수 동시 발생 방지
        # 3건 이상 청산 발생 시 신규 매수 차단 → 다음 거래일에 재개
        self._daily_exit_count: int = 0
        self._daily_exit_count_date: Optional[date] = date.today()
        # 차단 로그 스팸 방지 쿨다운 (심볼별 60초)
        self._last_exit_cooldown_log: Dict[str, datetime] = {}

        logger.info(
            f"RiskManager 초기화 ({self.market}): "
            f"일일손실한도={config.daily_max_loss_pct}%, "
            f"최대포지션={config.max_positions}개, "
            f"최대비율={config.max_position_pct}%, "
            f"최소금액={config.min_position_value:,}"
        )

    def _get_available_cash(self, portfolio: Portfolio) -> Decimal:
        """가용 현금 (최소 예비금 제외)"""
        min_reserve = portfolio.total_equity * Decimal(str(self.config.min_cash_reserve_pct / 100))
        return max(portfolio.cash - min_reserve, Decimal("0"))

    # ============================================================
    # 손절/익절 가격 계산
    # ============================================================

    def calculate_stop_loss(
        self,
        entry_price: Decimal,
        side: OrderSide,
        volatility: Optional[float] = None,
        symbol: Optional[str] = None,
        market_cap: Optional[float] = None
    ) -> Decimal:
        """
        손절 가격 계산 (KR: 대형주 손절 완화 포함)
        """
        stop_pct = self.config.default_stop_loss_pct / 100

        # KR 대형주 손절 완화
        if self.market == "KR":
            is_large_cap = False
            if symbol:
                large_caps = {
                    '005930', '000660', '373220', '207940', '005380',
                    '000270', '051910', '006400', '035420', '035720',
                    '068270', '028260', '105560', '055550', '086790', '316140',
                }
                is_large_cap = symbol in large_caps

            if market_cap is not None and market_cap >= 10000:
                is_large_cap = True

            if is_large_cap:
                stop_pct = max(stop_pct, 0.035)
                logger.debug(f"[손절완화] {symbol} 대형주 -> 손절폭 {stop_pct*100:.1f}%")

        # 변동성 기반 조정 (선택적)
        if volatility is not None and volatility > 5:
            stop_pct = min(stop_pct * 1.5, 0.05)

        if side == OrderSide.BUY:
            return entry_price * (1 - Decimal(str(stop_pct)))
        else:
            return entry_price * (1 + Decimal(str(stop_pct)))

    def calculate_take_profit(
        self,
        entry_price: Decimal,
        side: OrderSide,
        signal_strength: SignalStrength = SignalStrength.NORMAL
    ) -> Decimal:
        """익절 가격 계산"""
        base_pct = self.config.default_take_profit_pct / 100

        if signal_strength == SignalStrength.VERY_STRONG:
            target_pct = base_pct * 1.5
        elif signal_strength == SignalStrength.WEAK:
            target_pct = base_pct * 0.7
        else:
            target_pct = base_pct

        if side == OrderSide.BUY:
            return entry_price * (1 + Decimal(str(target_pct)))
        else:
            return entry_price * (1 - Decimal(str(target_pct)))

    def calculate_trailing_stop(
        self,
        highest_price: Decimal,
        side: OrderSide
    ) -> Decimal:
        """트레일링 스탑 가격 계산"""
        trail_pct = self.config.trailing_stop_pct / 100

        if side == OrderSide.BUY:
            return highest_price * (1 - Decimal(str(trail_pct)))
        else:
            return highest_price * (1 + Decimal(str(trail_pct)))

    # ============================================================
    # 거래 가능 여부 체크
    # ============================================================

    def can_open_position(
        self,
        symbol: str,
        side: OrderSide,
        quantity: int,
        price: Decimal,
        portfolio: Portfolio,
        strategy_type: str = "",
        signal: Optional[Signal] = None,
        sector: Optional[str] = None,
    ) -> Tuple[bool, str]:
        """
        포지션 오픈 가능 여부 체크 (KR/US 통합)

        Returns:
            (가능 여부, 거부 사유)
        """
        # 1. 당일 재진입 금지 체크 (KR 전용)
        if self.market == "KR" and symbol in self._stop_loss_today:
            return False, "당일 손절 종목 재진입 금지"

        # 1.2. 동일 종목 재진입 제한 (KR): 눌림/재돌파 확인형
        if self.market == "KR" and symbol in self._exited_today:
            can_re, reason = self.check_reentry_condition(symbol, float(price))
            if not can_re:
                return False, f"재진입 제한: {reason}"

        # 1.5. 포트폴리오 동기화 장애 체크 (trading_lock)
        # 대형 손실 이력: KIS API 일시 응답 지연 → 복구 과정에서 비정상 상태 진입
        # → 03-27 DB손해보험 -14%, SK하이닉스 -11.89% 등 10건 중 7건이 이 패턴
        if not self._sync_healthy:
            # 타임아웃 안전장치: 10분 이상 차단 지속 시 강제 해제
            # (블로킹 영구화 방지 — 운영 연속성 보장)
            now = datetime.now()
            if self._sync_unhealthy_since is not None:
                elapsed_min = (now - self._sync_unhealthy_since).total_seconds() / 60
                if elapsed_min >= self._sync_timeout_minutes:
                    logger.critical(
                        f"[리스크] 동기화 차단 타임아웃 강제 해제 "
                        f"({elapsed_min:.1f}분 경과, 한도 {self._sync_timeout_minutes}분) "
                        f"— 장기 스턱 방지용 자동 복구. 동기화 상태 즉시 점검 필요!"
                    )
                    self._sync_healthy = True
                    self._sync_fail_count = 0
                    self._sync_unhealthy_since = None
                    self._last_sync_block_log.clear()
                    # 강제 해제 → 이번 매수는 통과 (이후 체크 계속)
                else:
                    # 차단 유지 — 심볼별 로그 쿨다운 (60초)
                    _last = self._last_sync_block_log.get(symbol)
                    if _last is None or (now - _last).total_seconds() >= 60:
                        logger.warning(
                            f"[리스크] 동기화 복구 중 신규 매수 차단 ({symbol}) "
                            f"— 연속 {self._sync_fail_count}회 실패, "
                            f"{elapsed_min:.1f}분 경과 (타임아웃 {self._sync_timeout_minutes}분)"
                        )
                        self._last_sync_block_log[symbol] = now
                    return False, (
                        f"동기화 복구 중 매수 차단 "
                        f"(연속 {self._sync_fail_count}회 실패, {elapsed_min:.1f}분)"
                    )
            else:
                # 타임스탬프 누락(방어적) — 현재 시각으로 초기화 후 차단
                self._sync_unhealthy_since = now
                logger.warning(
                    f"[리스크] 동기화 복구 중 신규 매수 차단 ({symbol}) "
                    f"— 연속 {self._sync_fail_count}회 실패"
                )
                return False, (
                    f"동기화 복구 중 매수 차단 (연속 {self._sync_fail_count}회 실패)"
                )

        # 2. 일일 손실 한도 체크
        if self._is_daily_loss_limit_hit(portfolio, strategy_type):
            equity = portfolio.total_equity
            effective_pnl = getattr(portfolio, 'effective_daily_pnl', portfolio.daily_pnl)
            daily_pnl_pct = float(effective_pnl / equity * 100) if equity > 0 else 0.0

            if self.market == "KR":
                hard_stop_pct = max(self.config.daily_max_loss_pct * 2.5, 5.0)
                if daily_pnl_pct <= -hard_stop_pct:
                    return False, f"일일 손실 한도 초과 ({daily_pnl_pct:.1f}%) - 전면 차단"
                else:
                    return False, f"일일 손실 한도 도달 ({daily_pnl_pct:.1f}%) - 방어적 전략만 허용"
            else:
                return False, f"Daily loss limit reached ({daily_pnl_pct:.1f}%)"

        # 3. 최대 포지션 수 제한 (KR + US 공통)
        # 코어홀딩 포지션은 별도 슬롯으로 관리 (max_positions에서 제외)
        core_count = sum(1 for p in portfolio.positions.values() if p.strategy == "core_holding")
        non_core_count = len(portfolio.positions) - core_count
        is_core_signal = (strategy_type == "core_holding")
        if is_core_signal:
            # 코어 진입: 최대 N개 상한 (execute_core_rebalance + 여기서 이중 검증)
            max_core = getattr(self.config, 'max_core_positions', 3)
            if core_count >= max_core:
                return False, f"코어홀딩 상한 도달 ({core_count}/{max_core}개)"
        elif non_core_count >= self.config.max_positions:
            return False, f"최대 포지션 수 도달 ({non_core_count}/{self.config.max_positions}, 코어 {core_count}개 제외)"

        # 4. 최소 현금 예비 (KR + US 공통)
        min_cash = portfolio.total_equity * Decimal(str(self.config.min_cash_reserve_pct / 100))
        if portfolio.cash < min_cash:
            return False, f"최소 현금 보유 미달 ({portfolio.cash:,.0f} < {min_cash:,.0f})"

        # 5. 포지션 크기 체크
        position_value = price * quantity
        max_value = portfolio.total_equity * Decimal(str(self.config.max_position_pct / 100))
        if position_value > max_value:
            return False, f"포지션 크기 초과 ({position_value:,.0f} > {max_value:,.0f})"

        # 6. 현금 체크 (매수 시)
        if side == OrderSide.BUY:
            required = position_value * Decimal("1.001")
            available = self._get_available_cash(portfolio)
            if required > available:
                return False, f"현금 부족 ({available:,.0f} < {required:,.0f})"

        # 7. 섹터 제한 (US)
        if sector and self.config.max_positions_per_sector > 0:
            sector_count = sum(
                1 for p in portfolio.positions.values()
                if p.sector == sector
            )
            if sector_count >= self.config.max_positions_per_sector:
                return False, f"Sector limit reached for {sector}"

        # 8. 당일 청산 누적 쿨다운 (D+1 분리)
        # 4/14 -8.42% 사고 대응: 같은 날 다수 청산 + 다수 신규 매수 방지
        # threshold=0 이면 비활성 (안전장치)
        #
        # 2026-04-24 개선: 수익 상태에선 차단 해제 (오탐 제거)
        #   기존: 청산 횟수 ≥ 3 → 무조건 차단 (수익 익절 3건에도 발동)
        #   개선: (손실청산 ≥ 3) AND (당일 PnL < -1%) 둘 다일 때만 차단
        #   - record_exit에서 손실 청산(stop_loss/breakeven)만 카운트하도록 변경
        #   - 여기선 추가로 당일 PnL 음수 조건 체크 → 수익 중이면 프리패스
        threshold = int(getattr(self.config, 'daily_exit_cooldown_threshold', 0) or 0)
        if threshold > 0:
            # 날짜 롤오버 방어 (외부에서 record_exit 안 불린 상태로 날짜 바뀔 때)
            today = date.today()
            if self._daily_exit_count_date != today:
                self._daily_exit_count = 0
                self._daily_exit_count_date = today
                self._last_exit_cooldown_log.clear()

            if self._daily_exit_count >= threshold:
                # 당일 PnL < -1% 일 때만 차단 (수익 중에는 기회 허용)
                _daily_pnl_pct = 0.0
                try:
                    _eq = float(portfolio.total_equity)
                    _eff_pnl = float(getattr(portfolio, 'effective_daily_pnl', portfolio.daily_pnl))
                    _daily_pnl_pct = (_eff_pnl / _eq * 100) if _eq > 0 else 0.0
                except Exception:
                    pass

                _NET_LOSS_GATE = -1.0  # 당일 손익 < -1% 일 때만 차단 트리거
                if _daily_pnl_pct < _NET_LOSS_GATE:
                    # 심볼별 60초 로그 쿨다운 (스팸 방지)
                    now = datetime.now()
                    _last = self._last_exit_cooldown_log.get(symbol)
                    if _last is None or (now - _last).total_seconds() >= 60:
                        logger.warning(
                            f"[리스크] 당일 손실청산 {self._daily_exit_count}건 + "
                            f"PnL {_daily_pnl_pct:+.1f}% — 신규 매수 차단 ({symbol})"
                        )
                        self._last_exit_cooldown_log[symbol] = now
                    return False, (
                        f"당일 손실청산 {self._daily_exit_count}건 누적 + "
                        f"PnL {_daily_pnl_pct:+.1f}% < {_NET_LOSS_GATE:.0f}% → 차단"
                    )
                else:
                    # 쿨다운 임계 도달했지만 수익 중 — 허용 (로그는 60초 쿨다운)
                    now = datetime.now()
                    _last_key = f"__bypass__{symbol}"
                    _last = self._last_exit_cooldown_log.get(_last_key)
                    if _last is None or (now - _last).total_seconds() >= 60:
                        logger.info(
                            f"[리스크] 손실청산 {self._daily_exit_count}건 도달했으나 "
                            f"수익 상태(PnL {_daily_pnl_pct:+.1f}% ≥ {_NET_LOSS_GATE:.0f}%) → "
                            f"{symbol} 매수 허용"
                        )
                        self._last_exit_cooldown_log[_last_key] = now

        return True, ""

    # 2026-04-24: 손절성 청산 타입 (익절/트레일링/stale은 카운트 제외)
    #   기존엔 청산 종류 무관 전부 카운트 → 수익 청산 3건으로도 쿨다운 발동(오탐).
    #   4/14 사고(대부분 손절 연쇄) 방지는 유지하되, 손실성 청산만 카운트.
    _LOSS_EXIT_TYPES = {"stop_loss", "breakeven"}

    def record_exit(self, symbol: str, exit_price: float, sector: str = "",
                    is_full_exit: bool = True, exit_type: str = ""):
        """청산 종목 기록 (당일 재진입 조건 체크 + 크로스 검증 섹터 비교용)

        분할 매도 시 최초 청산가를 기준으로 유지 (덮어쓰기 방지).
        당일 청산 카운터는 **심볼별 최초 청산 1회 + 손실성 청산만** 증가.
        is_full_exit=False: 부분 청산 — 재진입 기록만, 카운터 미증가.
        exit_type: stop_loss/breakeven만 카운트 (take_profit/trailing/stale 제외).
        """
        # 당일 청산 카운터 — 날짜 롤오버 시 리셋
        today = date.today()
        if self._daily_exit_count_date != today:
            logger.debug(
                f"[리스크] 당일 청산 카운터 날짜 롤오버: "
                f"{self._daily_exit_count_date} -> {today} "
                f"(이전 카운터 {self._daily_exit_count} → 0)"
            )
            self._daily_exit_count = 0
            self._daily_exit_count_date = today
            self._last_exit_cooldown_log.clear()

        # 심볼별 최초 청산 1회 + **손실성 청산(손절/본전이탈)**만 카운트
        # 익절/트레일링/stale은 심리적 복구매수 유발 패턴이 아니므로 제외
        is_new_exit = symbol not in self._exited_today
        is_loss_exit = exit_type in self._LOSS_EXIT_TYPES
        if is_new_exit and is_loss_exit:
            self._daily_exit_count += 1
            threshold = int(getattr(self.config, 'daily_exit_cooldown_threshold', 0) or 0)
            if threshold > 0:
                logger.info(
                    f"[리스크] 당일 손실청산 누적: {self._daily_exit_count}/{threshold} "
                    f"({symbol} @ {exit_price}, type={exit_type})"
                )
            else:
                logger.debug(
                    f"[리스크] 당일 손실청산 카운터: {self._daily_exit_count} "
                    f"(쿨다운 비활성, threshold=0)"
                )
        elif is_new_exit:
            # 익절/트레일링 등 비손실 청산은 디버그 로그만
            logger.debug(
                f"[리스크] 비손실 청산 카운트 제외: {symbol} "
                f"(type={exit_type or 'unknown'}, 누적 {self._daily_exit_count} 유지)"
            )

        if self.market == "KR":
            if symbol not in self._exited_today:
                self._exited_today[symbol] = {
                    "price": exit_price,
                    "time": datetime.now().isoformat(),
                    "sector": sector,
                }
                self._save_exited_today()

    def check_reentry_condition(self, symbol: str, current_price: float) -> tuple:
        """동일 종목 재진입 조건 체크 (눌림/재돌파 확인형)

        Returns:
            (허용 여부, 사유)
        """
        if self.market != "KR":
            return True, ""

        exit_info = self._exited_today.get(symbol)
        if exit_info is None:
            return True, ""  # 당일 청산 이력 없음 → 자유 진입

        exit_price = exit_info["price"]
        exit_time_raw = exit_info["time"]
        if isinstance(exit_time_raw, str):
            exit_time = datetime.fromisoformat(exit_time_raw)
        else:
            exit_time = exit_time_raw
        elapsed_min = (datetime.now() - exit_time).total_seconds() / 60

        # 최소 쿨다운 30분
        if elapsed_min < 30:
            return False, f"당일 청산 후 쿨다운 ({elapsed_min:.0f}분/30분)"

        # 눌림/재돌파 확인: 청산가 대비 가격 위치로 판단
        if exit_price > 0 and current_price > 0:
            from_exit = (current_price - exit_price) / exit_price * 100
            # 청산가 대비 -3%~+3% = 눌림~소폭반등 구간 → 재진입 허용
            if -3 <= from_exit <= 3:
                return True, f"눌림/횡보 확인 (청산가 대비 {from_exit:+.1f}%)"
            # 청산가 대비 +3% 초과 = 재돌파 → 재진입 허용
            if from_exit > 3:
                return True, f"재돌파 확인 (청산가 대비 {from_exit:+.1f}%)"
            # -3% 미만 = 급락 중 → 차단 (추격 방지)
            return False, f"급락 중 재진입 차단 (청산가 대비 {from_exit:+.1f}%)"

        return True, ""

    def set_sync_status(self, healthy: bool):
        """포트폴리오 동기화 상태 갱신 (trading_lock 제어)

        연속 sync_fail_threshold회 실패 시 매수 차단.
        성공 1회로 즉시 복구.
        차단 지속 시간은 can_open_position에서 타임아웃 감시.
        """
        if healthy:
            if not self._sync_healthy:
                elapsed_min = 0.0
                if self._sync_unhealthy_since is not None:
                    elapsed_min = (datetime.now() - self._sync_unhealthy_since).total_seconds() / 60
                logger.info(
                    f"[리스크] 포트폴리오 동기화 복구 → 매수 차단 해제 "
                    f"(차단 지속 {elapsed_min:.1f}분)"
                )
            self._sync_fail_count = 0
            self._sync_healthy = True
            self._sync_unhealthy_since = None
            self._last_sync_block_log.clear()
        else:
            self._sync_fail_count += 1
            if self._sync_fail_count >= self._sync_fail_threshold and self._sync_healthy:
                self._sync_healthy = False
                self._sync_unhealthy_since = datetime.now()
                logger.warning(
                    f"[리스크] 포트폴리오 동기화 장애 (연속 {self._sync_fail_count}회 실패) → "
                    f"매수 차단 (타임아웃 {self._sync_timeout_minutes}분 후 자동 해제)"
                )

    def update_market_trend(self, kospi: dict, kosdaq: dict):
        """스케줄러에서 호출: KOSPI/KOSDAQ 장중 OHLC 기반 추세 갱신

        추세 판단 3가지 지표:
        1. 전일대비 등락률 (change_pct) — 전일 종가 대비 현재
        2. 시가대비 방향 — 장 시작 후 상승/하락
        3. 장중 위치 — (현재가 - 저가) / (고가 - 저가) → 0%=저가, 100%=고가

        recovering 판단:
        - 전일대비 등락률 평균 >= -0.5% 이고 시가대비 상승이면 → 회복세
        - 장중 위치 50% 이상 (고가 쪽에 가까움) → 회복세 보강
        - 전일대비 등락률 평균 < -0.5% 이고 시가대비 하락이면 → 하락세
        """
        from datetime import datetime

        def _calc_trend(idx: dict) -> dict:
            price = idx.get("price", 0)
            open_p = idx.get("open", 0)
            high_p = idx.get("high", 0)
            low_p = idx.get("low", 0)
            change_pct = idx.get("change_pct", 0)
            # 시가대비 방향
            vs_open = ((price - open_p) / open_p * 100) if open_p > 0 else 0
            # 장중 위치 (0%=저가, 100%=고가)
            intraday_range = high_p - low_p
            position_pct = ((price - low_p) / intraday_range * 100) if intraday_range > 0 else 50
            return {
                "change_pct": change_pct,
                "vs_open_pct": round(vs_open, 2),
                "position_pct": round(position_pct, 1),
            }

        # 양쪽 모두 유효한 데이터가 있어야 추세 판단 (빈 dict 방어)
        if not kospi.get("price") and not kosdaq.get("price"):
            return
        ki = _calc_trend(kospi) if kospi.get("price") else {"change_pct": 0, "vs_open_pct": 0, "position_pct": 50}
        kq = _calc_trend(kosdaq) if kosdaq.get("price") else {"change_pct": 0, "vs_open_pct": 0, "position_pct": 50}

        avg_change = (ki["change_pct"] + kq["change_pct"]) / 2
        avg_vs_open = (ki["vs_open_pct"] + kq["vs_open_pct"]) / 2
        avg_position = (ki["position_pct"] + kq["position_pct"]) / 2

        # 회복세 판단: 전일대비 + 시가대비 + 장중위치 종합
        # - 전일대비 양호(-0.5% 이상) AND (시가대비 양호 OR 장중위치 50% 이상) → 회복
        # - 전일대비 약세(-0.5% 미만) AND 시가대비 하락 AND 장중위치 30% 미만 → 하락
        if avg_change >= -0.5 and (avg_vs_open >= 0 or avg_position >= 50):
            recovering = True
        elif avg_change < -0.5 and avg_vs_open < 0 and avg_position < 30:
            recovering = False
        else:
            # 혼조세: 이전 상태 유지 (너무 자주 전환 방지)
            recovering = self._market_trend.get("recovering", True)

        prev_state = self._sidecar_active
        self._market_trend = {
            "kospi_pct": ki["change_pct"],
            "kosdaq_pct": kq["change_pct"],
            "avg_pct": avg_change,
            "vs_open_pct": avg_vs_open,
            "position_pct": avg_position,
            "recovering": recovering,
            "ts": datetime.now(),
        }

        # 사이드카 전환 로그 (상태 변경 시에만)
        if self._sidecar_active and recovering:
            self._sidecar_active = False
            logger.info(
                f"[리스크] 사이드카 해제: 시장 회복세 "
                f"(전일대비 {avg_change:+.1f}%, 시가대비 {avg_vs_open:+.1f}%, 장중위치 {avg_position:.0f}%)"
            )

        if prev_state != self._sidecar_active:
            logger.info(f"[리스크] 사이드카 상태: {'ON' if self._sidecar_active else 'OFF'}")

    def _is_daily_loss_limit_hit(self, portfolio: Portfolio, strategy_type: str = "") -> bool:
        """
        일일 손실 한도 도달 여부 (KR: 시장 추세 연동 스마트 사이드카)

        KR 로직 (daily_max_loss_pct=5% 기준):
        - 손실 < 경고 시작(-3.5%) → 허용
        - 경고 구간(-3.5% ~ -5%):
            → 시장 하락세 → 사이드카 ON (전면 차단)
            → 시장 회복세 → 허용 (특정 종목 문제, 시장은 OK)
            → 추세 정보 없음 → 방어적 전략만 허용
        - 한도 초과(-5% ~ -12.5%):
            → 시장 회복세 → 방어적 전략(RSI2/코어/SEPA)만 허용
            → 시장 하락세 또는 추세 없음 → 전면 차단
        - 하드 스탑(-12.5%+) → 무조건 전면 차단
        """
        equity = portfolio.total_equity
        if equity <= 0:
            return True  # equity 0 이하 → 거래 차단 (안전 장치)

        # effective_daily_pnl = 실현 + (현재 미실현 - 시작 미실현)
        # 분모 통일: initial_capital 기준 — 대시보드(data_collector/equity_tracker)와
        # 동일 분모 사용해 "표시값 -4.9%인데 차단된다" 같은 의사결정 혼선 방지.
        # initial_capital이 0/음수면 fallback으로 equity 사용 (안전 장치).
        effective_pnl = getattr(portfolio, 'effective_daily_pnl', portfolio.daily_pnl)
        _denom = self.initial_capital if self.initial_capital and self.initial_capital > 0 else equity
        daily_pnl_pct = float(effective_pnl / _denom * 100)

        if self.market == "KR":
            warn_start_pct = self.config.daily_max_loss_pct * 0.7  # 3.5% (경고 시작)
            warn_pct = self.config.daily_max_loss_pct               # 5% (한도)
            hard_stop_pct = max(warn_pct * 2.5, 5.0)                # 12.5%

            # 경고 구간: -warn_start_pct ~ -warn_pct (예: -3.5% ~ -5%)
            if -warn_pct < daily_pnl_pct <= -warn_start_pct:
                # 시장 추세 연동: 회복세면 특정 종목 손실 → 매수 허용
                trend = self._market_trend
                # 경고 구간: 시장 회복세면 전면 허용, 하락세면 차단
                if trend and trend.get("recovering"):
                    self._sidecar_active = False
                    logger.debug(
                        f"[스마트사이드카] 경고구간 {daily_pnl_pct:.1f}% but 시장 회복세 "
                        f"(전일비 {trend.get('avg_pct', 0):+.1f}%, "
                        f"시가비 {trend.get('vs_open_pct', 0):+.1f}%, "
                        f"장중위치 {trend.get('position_pct', 50):.0f}%) → 매수 허용"
                    )
                    return False  # 시장 OK → 전면 허용
                elif trend and not trend.get("recovering"):
                    if not self._sidecar_active:
                        self._sidecar_active = True
                        logger.warning(
                            f"[스마트사이드카] 사이드카 ON: 손실 {daily_pnl_pct:.1f}% + 시장 하락세 "
                            f"(전일비 {trend.get('avg_pct', 0):+.1f}%, "
                            f"시가비 {trend.get('vs_open_pct', 0):+.1f}%, "
                            f"장중위치 {trend.get('position_pct', 50):.0f}%)"
                        )
                    return True  # 시장도 하락 → 차단
                else:
                    return False  # 추세 정보 없음(장 초반 등) → 허용 (아직 경고 구간)

            # 한도 초과 구간: -warn_pct ~ -hard_stop_pct (예: -5% ~ -12.5%)
            if -hard_stop_pct < daily_pnl_pct <= -warn_pct:
                trend = self._market_trend
                if trend and trend.get("recovering"):
                    self._sidecar_active = False
                    # 시장 회복세 → 방어적 전략만 허용
                    defensive_strategies = {'rsi2_reversal', 'core_holding', 'sepa_trend'}
                    if strategy_type in defensive_strategies:
                        logger.debug(
                            f"[스마트사이드카] 한도초과 {daily_pnl_pct:.1f}% but 시장 회복세 "
                            f"→ 방어적 전략 '{strategy_type}' 허용"
                        )
                        return False
                    return True
                else:
                    # 시장 하락세 또는 추세 없음 → 전면 차단
                    if not self._sidecar_active and trend:
                        self._sidecar_active = True
                    return True

            # 하드 스탑: 무조건 차단
            if daily_pnl_pct <= -hard_stop_pct:
                return True
        else:
            # US: 단순 한도 체크
            if daily_pnl_pct <= -self.config.daily_max_loss_pct:
                return True

        return False

    # ============================================================
    # 포지션 사이징 (US)
    # ============================================================

    def calculate_position_size(self, portfolio: Portfolio,
                                price: Decimal,
                                allow_min_one: bool = False) -> int:
        """
        포지션 크기 계산 (주 수) -- US 전용

        Args:
            portfolio: 현재 포트폴리오
            price: 주가
            allow_min_one: True이면 예산 부족이어도 1주 강제 허용
        """
        if price <= 0:
            return 0

        equity = portfolio.total_equity
        min_cash = equity * Decimal(str(self.config.min_cash_reserve_pct / 100))
        available = max(Decimal("0"), portfolio.cash - min_cash)

        if available < Decimal(str(self.config.min_position_value)):
            if allow_min_one and portfolio.cash >= price:
                max_value = equity * Decimal(str(self.config.max_position_pct / 100))
                if price <= max_value:
                    return 1
            return 0

        base_value = equity * Decimal(str(self.config.base_position_pct / 100))
        max_value = equity * Decimal(str(self.config.max_position_pct / 100))

        position_value = min(base_value, available, max_value)

        # 연속 손실 적응형 사이징
        if self._consecutive_losses >= self.config.consecutive_loss_threshold:
            position_value *= Decimal(str(self.config.consecutive_loss_size_factor))
            logger.info(f"Size reduced by {self.config.consecutive_loss_size_factor}x "
                       f"(consecutive losses: {self._consecutive_losses})")

        quantity = int(position_value / price)

        if quantity == 0 and allow_min_one and portfolio.cash >= price:
            max_val = equity * Decimal(str(self.config.max_position_pct / 100))
            if price <= max_val:
                quantity = 1

        return max(0, quantity)

    # ============================================================
    # 이벤트 처리
    # ============================================================

    def on_fill(self, fill_event: FillEvent, portfolio: Portfolio) -> List[Event]:
        """체결 이벤트 처리"""
        events = []

        if fill_event.side == OrderSide.BUY:
            self.daily_stats.trades += 1

        equity = portfolio.total_equity
        effective_pnl = getattr(portfolio, 'effective_daily_pnl', portfolio.daily_pnl)
        daily_pnl_pct = float(effective_pnl / equity * 100) if equity > 0 else 0.0

        # 경고 임계값 체크
        if daily_pnl_pct <= -self._warn_threshold_pct:
            events.append(RiskAlertEvent(
                source="risk_manager",
                alert_type="daily_loss_warning",
                message=f"일일 손실 경고: {daily_pnl_pct:.1f}%",
                current_value=daily_pnl_pct,
                threshold=-self._warn_threshold_pct,
                action="warn"
            ))

        # 한도 도달 체크
        if daily_pnl_pct <= -self.config.daily_max_loss_pct:
            events.append(RiskAlertEvent(
                source="risk_manager",
                alert_type="daily_loss_limit",
                message=f"일일 손실 한도 도달: {daily_pnl_pct:.1f}%",
                current_value=daily_pnl_pct,
                threshold=-self.config.daily_max_loss_pct,
                action="block"
            ))
            self.metrics.is_daily_loss_limit_hit = True
            self.metrics.can_trade = False

        # 메트릭스 업데이트
        self.metrics.daily_loss = portfolio.daily_pnl
        self.metrics.daily_loss_pct = daily_pnl_pct
        self.metrics.daily_trades = self.daily_stats.trades

        return events

    def check_position_stops(
        self,
        position: Position,
        current_price: Decimal
    ) -> Optional[StopTriggeredEvent]:
        """포지션 손절/익절 체크"""
        if position.quantity <= 0:
            return None

        price = current_price
        entry_price = position.avg_price

        if entry_price <= 0:
            return None

        # 손절 체크
        if position.stop_loss is not None and price <= position.stop_loss:
            if self.market == "KR":
                self._stop_loss_today.add(position.symbol)
                self._save_stop_loss_today()
                logger.info(f"[재진입금지] {position.symbol} 손절 기록 (당일 재진입 차단)")

            return StopTriggeredEvent(
                source="risk_manager",
                symbol=position.symbol,
                trigger_type="stop_loss",
                trigger_price=position.stop_loss,
                current_price=price,
                position_side="long"
            )

        # 익절 체크
        if position.take_profit is not None and price >= position.take_profit:
            return StopTriggeredEvent(
                source="risk_manager",
                symbol=position.symbol,
                trigger_type="take_profit",
                trigger_price=position.take_profit,
                current_price=price,
                position_side="long"
            )

        # 트레일링 스탑 체크
        if position.trailing_stop_pct is not None and position.highest_price is not None:
            trailing_stop = self.calculate_trailing_stop(
                position.highest_price, OrderSide.BUY
            )
            if price <= trailing_stop:
                return StopTriggeredEvent(
                    source="risk_manager",
                    symbol=position.symbol,
                    trigger_type="trailing_stop",
                    trigger_price=trailing_stop,
                    current_price=price,
                    position_side="long"
                )

        return None

    # ============================================================
    # 유틸리티
    # ============================================================

    def reset_daily_stats(self):
        """일일 통계 초기화 (날짜 변경 시에만)"""
        today = date.today()

        if self.daily_stats.date == today:
            logger.debug(f"일일 통계 유지 (같은 날: {today})")
            return

        logger.info(f"날짜 변경: {self.daily_stats.date} -> {today}, 일일 통계 초기화")
        self.daily_stats = DailyStats(peak_equity=self.initial_capital)
        self.metrics = RiskMetrics()
        self.metrics.can_trade = True
        self._stop_loss_today.clear()
        self._exited_today.clear()
        # 연속 손실 카운터 일일 리셋 — 전일 4연패가 익일 첫 거래 전 사이징 50% 축소를
        # 잘못 끌고 가지 않도록. (record_trade_result의 승리 시 즉시 리셋과 별개로 날짜 경계 보강)
        self._consecutive_losses = 0
        # 당일 청산 카운터도 날짜 경계에서 리셋 (D+1 분리 쿨다운 해제)
        self._daily_exit_count = 0
        self._daily_exit_count_date = today
        self._last_exit_cooldown_log.clear()
        self._save_exited_today()

        self._save_daily_stats()

    def record_trade_result(self, pnl: Decimal = None, is_win: bool = None):
        """거래 결과 기록 (KR/US 통합)

        Args:
            pnl: KR에서 사용 -- Decimal 손익
            is_win: US에서 사용 -- 승리 여부 (bool)
        """
        if pnl is not None:
            # KR 스타일
            if pnl > 0:
                self.daily_stats.wins += 1
                self.daily_stats.consecutive_losses = 0
                self._consecutive_losses = 0
            elif pnl < 0:
                self.daily_stats.losses += 1
                self.daily_stats.consecutive_losses += 1
                self._consecutive_losses += 1
            self.daily_stats.total_pnl += pnl
        elif is_win is not None:
            # US 스타일
            if is_win:
                self._consecutive_losses = 0
                self.daily_stats.wins += 1
                self.daily_stats.consecutive_losses = 0
            else:
                self._consecutive_losses += 1
                self.daily_stats.losses += 1
                self.daily_stats.consecutive_losses += 1

        self.metrics.consecutive_losses = self.daily_stats.consecutive_losses
        self._save_daily_stats()

    def get_risk_summary(self) -> Dict[str, Any]:
        """리스크 요약"""
        _wins = self.daily_stats.wins
        _losses = self.daily_stats.losses
        _total = _wins + _losses
        return {
            "can_trade": self.metrics.can_trade,
            "daily_loss_pct": self.metrics.daily_loss_pct,
            "daily_trades": self.daily_stats.trades,
            "wins": _wins,
            "losses": _losses,
            "consecutive_losses": self.daily_stats.consecutive_losses,
            "win_rate": (_wins / max(1, _total) * 100 if _total > 0 else 0),
            "total_pnl": float(self.daily_stats.total_pnl),
            "market": self.market,
        }

    def get_risk_metrics(self, portfolio: Portfolio) -> RiskMetrics:
        """Get current risk state (US 호환)"""
        daily_loss_pct = 0.0
        effective_pnl = getattr(portfolio, 'effective_daily_pnl', portfolio.daily_pnl)
        # _is_daily_loss_limit_hit와 동일 기준(total_equity) 사용
        equity = portfolio.total_equity
        if equity > 0:
            daily_loss_pct = float(
                effective_pnl / equity * 100
            )

        return RiskMetrics(
            daily_loss=effective_pnl,
            daily_loss_pct=daily_loss_pct,
            daily_trades=portfolio.daily_trades,
            total_exposure=float(1 - portfolio.cash_ratio) * 100,
            is_daily_loss_limit_hit=self._is_daily_loss_limit_hit(portfolio),
            can_trade=not self._is_daily_loss_limit_hit(portfolio),
            consecutive_losses=self._consecutive_losses,
        )

    # ============================================================
    # 일일 손익 영속화
    # ============================================================

    def _save_daily_stats(self):
        """일일 손익을 파일에 저장"""
        try:
            data = {
                "date": self.daily_stats.date.isoformat(),
                "trades": self.daily_stats.trades,
                "wins": self.daily_stats.wins,
                "losses": self.daily_stats.losses,
                "total_pnl": str(self.daily_stats.total_pnl),
                "consecutive_losses": self.daily_stats.consecutive_losses,
                "peak_equity": str(self.daily_stats.peak_equity),
            }
            with open(self._daily_stats_path, 'w') as f:
                json.dump(data, f, indent=2)
            logger.debug(f"일일 손익 저장 완료: {data['total_pnl']}")
        except Exception as e:
            logger.error(f"일일 손익 저장 실패: {e}")

    def _load_daily_stats(self):
        """일일 손익을 파일에서 로드"""
        try:
            if not self._daily_stats_path.exists():
                logger.debug("일일 손익 파일 없음 (신규 시작)")
                return

            with open(self._daily_stats_path, 'r') as f:
                data = json.load(f)

            saved_date = date.fromisoformat(data["date"])
            today = date.today()

            if saved_date != today:
                logger.info(f"날짜 변경 감지: {saved_date} -> {today}, 일일 손익 리셋")
                return

            if "trades" not in data:
                logger.warning("일일 손익 파일 포맷 불일치 -> 무시하고 재생성")
                self._save_daily_stats()
                return

            self.daily_stats.trades = data["trades"]
            self.daily_stats.wins = data.get("wins", 0)
            self.daily_stats.losses = data.get("losses", 0)
            self.daily_stats.total_pnl = Decimal(data.get("total_pnl", "0"))
            self.daily_stats.consecutive_losses = data.get("consecutive_losses", 0)
            self._consecutive_losses = self.daily_stats.consecutive_losses
            self.daily_stats.peak_equity = Decimal(data.get("peak_equity", str(self.initial_capital)))

            logger.info(
                f"일일 손익 복원: {self.daily_stats.total_pnl:,.0f} "
                f"(거래 {self.daily_stats.trades}회, 승 {self.daily_stats.wins} / 패 {self.daily_stats.losses})"
            )
        except Exception as e:
            logger.error(f"일일 손익 로드 실패: {e}")

    def _load_stop_loss_today(self) -> set:
        """손절 종목 목록을 파일에서 복원"""
        try:
            if not self._stop_loss_today_path.exists():
                return set()
            with open(self._stop_loss_today_path, 'r') as f:
                data = json.load(f)
            saved_date = data.get("date", "")
            if saved_date != date.today().isoformat():
                return set()
            symbols = set(data.get("symbols", []))
            if symbols:
                logger.info(f"[리스크] 손절 종목 복원: {symbols}")
            return symbols
        except Exception as e:
            logger.error(f"손절 종목 로드 실패: {e}")
            return set()

    def _save_stop_loss_today(self):
        """손절 종목 목록을 파일에 저장"""
        try:
            data = {
                "date": date.today().isoformat(),
                "symbols": list(self._stop_loss_today)
            }
            with open(self._stop_loss_today_path, 'w') as f:
                json.dump(data, f)
        except Exception as e:
            logger.error(f"손절 종목 저장 실패: {e}")

    def _load_exited_today(self) -> Dict[str, Dict]:
        """당일 청산 종목을 파일에서 복원"""
        try:
            if not self._exited_today_path.exists():
                return {}
            with open(self._exited_today_path, 'r') as f:
                data = json.load(f)
            if data.get("date") != date.today().isoformat():
                return {}
            entries = data.get("entries", {})
            if entries:
                logger.info(f"[리스크] 청산 종목 복원: {list(entries.keys())} (재진입 제한)")
            return entries
        except Exception as e:
            logger.error(f"청산 종목 로드 실패: {e}")
            return {}

    def _save_exited_today(self):
        """당일 청산 종목을 파일에 저장"""
        try:
            data = {
                "date": date.today().isoformat(),
                "entries": self._exited_today,
            }
            with open(self._exited_today_path, 'w') as f:
                json.dump(data, f)
        except Exception as e:
            logger.error(f"청산 종목 저장 실패: {e}")
