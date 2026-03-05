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

        # 연속 손실 카운터 (US 적응형 사이징)
        self._consecutive_losses: int = 0

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

        # 2. 일일 손실 한도 체크
        if self._is_daily_loss_limit_hit(portfolio, strategy_type):
            equity = portfolio.total_equity
            effective_pnl = portfolio.daily_pnl
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
        if len(portfolio.positions) >= self.config.max_positions:
            return False, f"최대 포지션 수 도달 ({len(portfolio.positions)}/{self.config.max_positions})"

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

        return True, ""

    def _is_daily_loss_limit_hit(self, portfolio: Portfolio, strategy_type: str = "") -> bool:
        """
        일일 손실 한도 도달 여부 (KR: 차등 리스크 관리)
        """
        equity = portfolio.total_equity
        if equity <= 0:
            return False

        # effective_daily_pnl = 실현 + (현재 미실현 - 시작 미실현)
        effective_pnl = getattr(portfolio, 'effective_daily_pnl', portfolio.daily_pnl)
        daily_pnl_pct = float(effective_pnl / equity * 100)

        if self.market == "KR":
            # 차등 리스크 관리
            hard_stop_pct = max(self.config.daily_max_loss_pct * 2.5, 5.0)
            if -hard_stop_pct < daily_pnl_pct <= -self.config.daily_max_loss_pct:
                defensive_strategies = {'mean_reversion', 'defensive', 'value_large_cap'}
                if strategy_type in defensive_strategies:
                    logger.debug(
                        f"[차등리스크] 손실 {daily_pnl_pct:.1f}% -> "
                        f"방어적 전략 '{strategy_type}' 허용"
                    )
                    return False
                else:
                    return True

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
        available = portfolio.cash - min_cash

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
        if position.stop_loss and price <= position.stop_loss:
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
        if position.take_profit and price >= position.take_profit:
            return StopTriggeredEvent(
                source="risk_manager",
                symbol=position.symbol,
                trigger_type="take_profit",
                trigger_price=position.take_profit,
                current_price=price,
                position_side="long"
            )

        # 트레일링 스탑 체크
        if position.trailing_stop_pct and position.highest_price:
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
        return {
            "can_trade": self.metrics.can_trade,
            "daily_loss_pct": self.metrics.daily_loss_pct,
            "daily_trades": self.daily_stats.trades,
            "consecutive_losses": self.daily_stats.consecutive_losses,
            "win_rate": (
                self.daily_stats.wins / max(1, self.daily_stats.wins + self.daily_stats.losses) * 100
                if (self.daily_stats.wins + self.daily_stats.losses) > 0 else 0
            ),
            "total_pnl": float(self.daily_stats.total_pnl),
            "market": self.market,
        }

    def get_risk_metrics(self, portfolio: Portfolio) -> RiskMetrics:
        """Get current risk state (US 호환)"""
        daily_loss_pct = 0.0
        if portfolio.initial_capital > 0:
            daily_loss_pct = float(
                portfolio.effective_daily_pnl / portfolio.initial_capital * 100
            )

        return RiskMetrics(
            daily_loss=portfolio.effective_daily_pnl,
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
