"""
QWQ AI Trader - 핵심 타입 정의 (통합)

KR(ai-trader-v2) + US(ai-trader-us) 도메인 객체 및 열거형 통합.
모든 시장(KRX, NASDAQ, NYSE, AMEX)에서 공통으로 사용합니다.
"""

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum, auto
from typing import Optional, Dict, Any, List
import uuid


# ============================================================
# 열거형 (Enums)
# ============================================================

class Market(str, Enum):
    """거래 시장"""
    KRX = "KRX"           # 한국거래소 (정규장)
    KRX_EXT = "KRX_EXT"   # 한국거래소 (시간외/넥스트)
    NASDAQ = "NASDAQ"     # 나스닥
    NYSE = "NYSE"         # 뉴욕증권거래소
    AMEX = "AMEX"         # 아메리칸증권거래소


class MarketSession(str, Enum):
    """시장 세션"""
    PRE_MARKET = "pre_market"      # KR: 08:00~08:50 / US: 04:00~09:30 ET
    REGULAR = "regular"            # KR: 09:00~15:30 / US: 09:30~16:00 ET
    AFTER_HOURS = "after_hours"    # KR: 15:40~16:00 / US: 16:00~20:00 ET
    NEXT = "next"                  # KR 넥스트장 (15:30~20:00)
    CLOSED = "closed"              # 장 마감


# TradingSession은 MarketSession의 alias (하위호환)
TradingSession = MarketSession


class OrderSide(str, Enum):
    """주문 방향"""
    BUY = "buy"
    SELL = "sell"


class OrderType(str, Enum):
    """주문 유형"""
    MARKET = "market"          # 시장가
    LIMIT = "limit"            # 지정가
    STOP = "stop"              # 스탑
    STOP_LIMIT = "stop_limit"  # 스탑 리밋


class OrderStatus(str, Enum):
    """주문 상태"""
    PENDING = "pending"        # 대기
    SUBMITTED = "submitted"    # 제출됨
    PARTIAL = "partial"        # 부분 체결
    FILLED = "filled"          # 완전 체결
    CANCELLED = "cancelled"    # 취소됨
    REJECTED = "rejected"      # 거부됨
    EXPIRED = "expired"        # 만료됨


class PositionSide(str, Enum):
    """포지션 방향"""
    LONG = "long"
    SHORT = "short"
    FLAT = "flat"


class SignalStrength(str, Enum):
    """신호 강도"""
    VERY_STRONG = "very_strong"  # 매우 강함
    STRONG = "strong"            # 강함
    NORMAL = "normal"            # 보통
    WEAK = "weak"                # 약함


class StrategyType(str, Enum):
    """전략 유형 (KR + US 통합)"""
    # KR 전략
    MOMENTUM_BREAKOUT = "momentum_breakout"    # 20일 고가 돌파 + 거래량 급증
    THEME_CHASING = "theme_chasing"            # 핫 테마 종목 추종
    GAP_AND_GO = "gap_and_go"                  # 갭상승 후 눌림목 매수
    MEAN_REVERSION = "mean_reversion"          # 평균 회귀
    SCALPING = "scalping"                      # 스캘핑
    RSI2_REVERSAL = "rsi2_reversal"            # RSI(2) 역전
    SEPA_TREND = "sepa_trend"                  # SEPA 추세 전략 (스윙)
    CORE_HOLDING = "core_holding"              # 코어홀딩 중장기 전략
    STRATEGIC_SWING = "strategic_swing"        # 전략적 스윙

    # US 전략
    ORB = "orb"                                # Opening Range Breakout
    VWAP_BOUNCE = "vwap_bounce"                # VWAP 반등
    EARNINGS_DRIFT = "earnings_drift"          # Post-Earnings Drift


class TimeHorizon(str, Enum):
    """전략 타임 호라이즌 (보유 기간)"""
    DAY = "day"                # 데이 트레이딩 (1일 이내)
    SHORT_TERM = "short_term"  # 단기 (2-5일)
    SWING = "swing"            # 스윙 (5-20일)
    MEDIUM_TERM = "medium_term"  # 중장기 (20일+, 코어홀딩)


# ============================================================
# 데이터 클래스 (Data Classes)
# ============================================================

@dataclass
class Symbol:
    """종목 정보"""
    code: str                     # 종목코드 (예: 005930, AAPL)
    name: str                     # 종목명 (예: 삼성전자, Apple Inc.)
    market: Market = Market.KRX   # 시장
    sector: Optional[str] = None  # 섹터

    @property
    def full_code(self) -> str:
        """전체 코드 (시장 포함)"""
        return f"{self.market.value}:{self.code}"


@dataclass
class Price:
    """가격 정보 (OHLCV)"""
    symbol: str                   # 종목코드
    timestamp: datetime           # 시간
    open: Decimal                 # 시가
    high: Decimal                 # 고가
    low: Decimal                  # 저가
    close: Decimal                # 종가 (현재가)
    volume: int                   # 거래량
    value: Optional[Decimal] = None  # 거래대금 (KRW) / Dollar volume (USD)

    @property
    def typical_price(self) -> Decimal:
        """대표가격 (HLC 평균)"""
        return (self.high + self.low + self.close) / 3


@dataclass
class Quote:
    """호가 정보"""
    symbol: str
    timestamp: datetime
    bid_price: Decimal            # 매수 호가
    bid_size: int                 # 매수 잔량
    ask_price: Decimal            # 매도 호가
    ask_size: int                 # 매도 잔량

    @property
    def spread(self) -> Decimal:
        """스프레드"""
        return self.ask_price - self.bid_price

    @property
    def mid_price(self) -> Decimal:
        """중간가"""
        return (self.bid_price + self.ask_price) / 2


@dataclass
class Order:
    """주문"""
    id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    symbol: str = ""
    side: OrderSide = OrderSide.BUY
    order_type: OrderType = OrderType.LIMIT
    quantity: int = 0
    price: Optional[Decimal] = None        # 지정가 (시장가면 None)
    stop_price: Optional[Decimal] = None   # 스탑 가격

    status: OrderStatus = OrderStatus.PENDING
    filled_quantity: int = 0
    filled_price: Optional[Decimal] = None

    strategy: Optional[str] = None         # 전략명
    reason: Optional[str] = None           # 주문 사유
    signal_score: Optional[float] = None   # 신호 점수

    created_at: datetime = field(default_factory=datetime.now)
    updated_at: Optional[datetime] = None
    broker_order_id: Optional[str] = None  # 브로커 주문번호

    @property
    def is_active(self) -> bool:
        """활성 주문 여부"""
        return self.status in (OrderStatus.PENDING, OrderStatus.SUBMITTED, OrderStatus.PARTIAL)

    @property
    def remaining_quantity(self) -> int:
        """미체결 수량"""
        return self.quantity - self.filled_quantity


@dataclass
class Fill:
    """체결 정보"""
    order_id: str
    symbol: str
    side: OrderSide
    quantity: int
    price: Decimal
    commission: Decimal = Decimal("0")
    timestamp: datetime = field(default_factory=datetime.now)

    # 추가 메타데이터 (거래 저널용)
    strategy: Optional[str] = None         # 전략명
    reason: Optional[str] = None           # 체결 사유
    signal_score: Optional[float] = None   # 신호 점수

    @property
    def total_value(self) -> Decimal:
        """총 체결금액"""
        return self.price * self.quantity

    @property
    def total_cost(self) -> Decimal:
        """총 비용 (수수료 포함)"""
        return self.total_value + self.commission


@dataclass
class Position:
    """포지션 (KR/US 통합)"""
    symbol: str
    name: str = ""
    side: PositionSide = PositionSide.FLAT
    quantity: int = 0
    avg_price: Decimal = Decimal("0")
    current_price: Decimal = Decimal("0")

    # 시장/통화
    market: Market = Market.KRX
    currency: str = "KRW"

    # 리스크 관리
    stop_loss: Optional[Decimal] = None
    take_profit: Optional[Decimal] = None
    trailing_stop_pct: Optional[float] = None
    highest_price: Optional[Decimal] = None   # 트레일링용

    # 메타데이터
    strategy: Optional[str] = None
    entry_time: Optional[datetime] = None
    sector: Optional[str] = None
    time_horizon: Optional[TimeHorizon] = None  # US에서 추가
    trade_id: Optional[str] = None              # US에서 추가

    @property
    def market_value(self) -> Decimal:
        """시장가치"""
        return self.current_price * Decimal(str(self.quantity))

    @property
    def cost_basis(self) -> Decimal:
        """취득원가"""
        return self.avg_price * Decimal(str(self.quantity))

    @property
    def unrealized_pnl(self) -> Decimal:
        """미실현 손익 (수수료 미포함 -- 매입가 대비 단순 평가차익)"""
        if self.quantity == 0:
            return Decimal("0")
        return self.market_value - self.cost_basis

    @property
    def unrealized_pnl_net(self) -> Decimal:
        """미실현 순손익 (지금 매도했을 때 실제로 손에 쥐는 금액)

        = (현재가 - 평균단가) x 수량 - 매수수수료 - 매도수수료 - 거래세
        매수수수료는 fill.total_cost에 이미 포함됐으므로 cost_basis에서 빼지 않고
        별도 추산해서 차감합니다.
        수수료 상수는 FeeConfig 기본값 기준 (fee_calculator.py와 동일).
        KR 전용 -- US는 zero-commission이므로 unrealized_pnl과 동일.
        """
        if self.quantity == 0:
            return Decimal("0")
        # US 시장은 zero-commission 기본
        if self.currency == "USD":
            return self.unrealized_pnl
        from ..utils.fee_calculator import FeeConfig
        _fc = FeeConfig()
        buy_fee = self.cost_basis * _fc.buy_commission_rate
        sell_fee = self.market_value * _fc.total_sell_rate
        return self.unrealized_pnl - buy_fee - sell_fee

    @property
    def unrealized_pnl_net_pct(self) -> float:
        """미실현 순손익률 (수수료 포함, cost_basis + 매수수수료 대비)"""
        if self.cost_basis == 0:
            return 0.0
        # US 시장은 zero-commission 기본
        if self.currency == "USD":
            return self.unrealized_pnl_pct
        from ..utils.fee_calculator import FeeConfig
        total_cost = self.cost_basis * (1 + FeeConfig().buy_commission_rate)
        return float(self.unrealized_pnl_net / total_cost * 100)

    @property
    def unrealized_pnl_pct(self) -> float:
        """미실현 손익률 (수수료 미포함, %)"""
        if self.cost_basis == 0:
            return 0.0
        return float(self.unrealized_pnl / self.cost_basis * 100)

    @property
    def is_profit(self) -> bool:
        """수익 상태 (KR: 수수료 포함, US: gross 기준)"""
        if self.currency == "USD":
            return self.unrealized_pnl > 0
        return self.unrealized_pnl_net > 0


@dataclass
class Portfolio:
    """포트폴리오 (KR/US 통합)"""
    cash: Decimal = Decimal("0")
    positions: Dict[str, Position] = field(default_factory=dict)
    initial_capital: Decimal = Decimal("0")

    # 시장/통화
    market: Market = Market.KRX
    currency: str = "KRW"

    # 일일 통계
    daily_pnl: Decimal = Decimal("0")
    daily_trades: int = 0
    daily_start_unrealized_pnl: Decimal = Decimal("0")  # 당일 시작 시점 미실현 손익

    @property
    def total_position_value(self) -> Decimal:
        """총 포지션 가치"""
        return sum(p.market_value for p in self.positions.values())

    @property
    def total_equity(self) -> Decimal:
        """총 자산"""
        return self.cash + self.total_position_value

    @property
    def total_pnl(self) -> Decimal:
        """총 손익"""
        return self.total_equity - self.initial_capital

    @property
    def total_pnl_pct(self) -> float:
        """총 손익률 (%)"""
        if self.initial_capital == 0:
            return 0.0
        return float(self.total_pnl / self.initial_capital * 100)

    @property
    def total_unrealized_pnl(self) -> Decimal:
        """총 미실현 손익"""
        return sum(p.unrealized_pnl for p in self.positions.values())

    @property
    def effective_daily_pnl(self) -> Decimal:
        """실효 일일 손익 (실현 + 금일 미실현 변동분)

        전일부터 보유 중인 포지션의 미실현 손익 중 '오늘 발생한 변동분'만 반영합니다.
        daily_start_unrealized_pnl은 장 시작 시 reset_daily()에서 세팅됩니다.
        """
        return self.daily_pnl + (self.total_unrealized_pnl - self.daily_start_unrealized_pnl)

    @property
    def cash_ratio(self) -> float:
        """현금 비율"""
        if self.total_equity == 0:
            return 1.0
        return float(self.cash / self.total_equity)

    def reset_daily(self):
        """일일 통계 초기화 (장 시작 시 호출)"""
        self.daily_pnl = Decimal("0")
        self.daily_trades = 0
        self.daily_start_unrealized_pnl = self.total_unrealized_pnl

    def get_strategy_allocation(self, strategy: str) -> Decimal:
        """특정 전략의 현재 총 배분 금액 (보유 포지션 시장가치 합계)"""
        return sum(
            (p.market_value for p in self.positions.values()
             if p.strategy == strategy),
            Decimal("0"),
        )

    def get_all_strategy_allocations(self) -> Dict[str, Decimal]:
        """전략별 현재 배분 금액"""
        allocs: Dict[str, Decimal] = {}
        for pos in self.positions.values():
            key = pos.strategy or "unknown"
            allocs[key] = allocs.get(key, Decimal("0")) + pos.market_value
        return allocs


@dataclass
class Signal:
    """매매 신호"""
    symbol: str
    side: OrderSide
    strength: SignalStrength
    strategy: StrategyType

    # 시장
    market: Market = Market.KRX

    price: Optional[Decimal] = None
    target_price: Optional[Decimal] = None
    stop_price: Optional[Decimal] = None

    score: float = 0.0              # 신호 점수 (0~100)
    confidence: float = 0.0         # 신뢰도 (0~1)

    reason: str = ""                # 신호 생성 사유 (요약 문자열 — 하위호환)
    reasons: List[str] = field(default_factory=list)          # 구조화 진입 근거 (최소 2개 권장, 2026-04-21 도입)
    score_breakdown: Dict[str, float] = field(default_factory=dict)   # 전략별 핵심 메트릭
    context_snapshot: Dict[str, Any] = field(default_factory=dict)    # 시장 체제, 섹터 강도 등
    metadata: Dict[str, Any] = field(default_factory=dict)

    timestamp: datetime = field(default_factory=datetime.now)
    expires_at: Optional[datetime] = None  # 신호 만료 시간

    @property
    def is_buy(self) -> bool:
        return self.side == OrderSide.BUY

    @property
    def is_expired(self) -> bool:
        if self.expires_at is None:
            return False
        return datetime.now() > self.expires_at

    def effective_reasons(self) -> List[str]:
        """구조화 reasons가 비어있으면 reason 문자열을 쉼표/세미콜론 분리하여 폴백"""
        if self.reasons:
            return list(self.reasons)
        if self.reason:
            parts = [p.strip() for p in self.reason.replace(";", ",").split(",") if p.strip()]
            return parts if parts else [self.reason.strip()]
        return []


@dataclass
class Theme:
    """테마 정보"""
    name: str                       # 테마명 (예: AI/반도체)
    keywords: List[str]             # 관련 키워드
    symbols: List[str]              # 관련 종목
    score: float = 0.0              # 테마 강도 (0~100)

    news_count: int = 0             # 관련 뉴스 수
    price_momentum: float = 0.0     # 관련 종목 평균 모멘텀

    detected_at: datetime = field(default_factory=datetime.now)

    @property
    def is_hot(self) -> bool:
        """핫 테마 여부"""
        return self.score > 70


@dataclass
class TradeResult:
    """거래 결과"""
    symbol: str
    side: OrderSide
    entry_price: Decimal
    exit_price: Decimal
    quantity: int

    entry_time: datetime
    exit_time: datetime

    strategy: str
    reason: str = ""
    commission: Decimal = Decimal("0")  # US에서 추가 (명시적 수수료)

    @property
    def pnl(self) -> Decimal:
        """손익 (수수료 차감)"""
        if self.side == OrderSide.BUY:
            gross = (self.exit_price - self.entry_price) * self.quantity
        else:
            gross = (self.entry_price - self.exit_price) * self.quantity
        return gross - self.commission

    @property
    def pnl_pct(self) -> float:
        """손익률 (%)"""
        if self.entry_price is None or self.entry_price == 0:
            return 0.0
        return float(Decimal(str(self.exit_price - self.entry_price)) / Decimal(str(self.entry_price)) * 100)

    @property
    def holding_time(self) -> float:
        """보유 시간 (분) — 음수 방지 (sync 기록 시 exit < entry 가능)"""
        delta = (self.exit_time - self.entry_time).total_seconds() / 60
        return max(0.0, delta)

    # holding_minutes는 holding_time의 alias (US 호환)
    @property
    def holding_minutes(self) -> float:
        """보유 시간 (분) -- holding_time alias"""
        return self.holding_time

    @property
    def is_win(self) -> bool:
        """승리 여부"""
        return self.pnl > 0


@dataclass
class RiskMetrics:
    """리스크 지표"""
    # 일일 제한
    daily_loss: Decimal = Decimal("0")
    daily_loss_pct: float = 0.0
    daily_trades: int = 0

    # 포지션 제한
    max_position_value: Decimal = Decimal("0")
    total_exposure: float = 0.0

    # 상태
    is_daily_loss_limit_hit: bool = False
    is_max_trades_hit: bool = False
    can_trade: bool = True

    # 연속 손실
    consecutive_losses: int = 0


# ============================================================
# 설정 타입
# ============================================================

@dataclass
class HybridConfig:
    """하이브리드 전략 설정 (타임 호라이즌별 자금 배분)"""
    enabled: bool = False                 # 하이브리드 모드 활성화
    day_trading_pct: float = 20.0        # 데이 트레이딩 자금 비율 (%)
    short_term_pct: float = 30.0         # 단기 전략 자금 비율 (%)
    swing_pct: float = 50.0              # 스윙 전략 자금 비율 (%)

    # 타임 호라이즌별 포지션 설정
    day_base_position_pct: float = 10.0      # 데이: 작은 포지션 (빠른 회전)
    short_term_base_position_pct: float = 15.0  # 단기: 중간 포지션
    swing_base_position_pct: float = 20.0    # 스윙: 큰 포지션 (집중 투자)

    day_max_position_pct: float = 20.0
    short_term_max_position_pct: float = 30.0
    swing_max_position_pct: float = 40.0


@dataclass
class CommissionConfig:
    """수수료 구조 (US 시장)"""
    type: str = "zero"              # zero, per_share, percentage
    rate: float = 0.0               # $0.005/share or 0.1%
    min_commission: float = 0.0


@dataclass
class SlippageConfig:
    """슬리피지 모델 (US 시장)"""
    model: str = "percentage"       # fixed, percentage, volume_impact
    rate: float = 0.05              # 0.05%


@dataclass
class RiskConfig:
    """리스크 설정 (KR + US 통합)"""
    # 일일 한도
    daily_max_loss_pct: float = 3.0
    daily_max_trades: int = 15
    max_daily_new_buys: int = 5        # 일일 신규 매수 한도 (적응형 사이징 슬롯)

    # 포지션 관리
    base_position_pct: float = 15.0    # 기본 포지션 비율 (15% - 집중 투자)
    max_position_pct: float = 35.0     # 최대 포지션 비율 (35%)
    max_positions: int = 5             # 최대 동시 포지션 수 (소수 정예)
    min_cash_reserve_pct: float = 15.0 # 최소 현금 예비 (안전마진)
    min_position_value: float = 500000 # 최소 포지션 금액 (KR: 50만원 / US: $50)
    dynamic_max_positions: bool = True # 자산 규모에 따라 max_positions 동적 조정
    flex_extra_positions: int = 2            # 여유자금 시 추가 허용 슬롯 수 (0=비활성화)
    flex_cash_threshold_pct: float = 10.0    # 가용현금이 총자산의 N% 이상이면 슬롯 추가
    max_positions_per_sector: int = 3        # 동일 섹터 최대 포지션 수 (0=제한없음)

    # 손절/익절
    default_stop_loss_pct: float = 2.5
    default_take_profit_pct: float = 5.0
    trailing_stop_pct: float = 1.5

    # 특별 상황 (KR)
    hot_theme_position_pct: float = 50.0
    momentum_multiplier: float = 1.5

    # 연속 손실 적응형 사이징 (US)
    consecutive_loss_threshold: int = 3
    consecutive_loss_size_factor: float = 0.5

    # 당일 청산 누적 쿨다운 (D+1 분리)
    # 청산 당일 신규 매수를 차단해 "저점 청산 후 즉시 재진입 → 반등 미스" 패턴 방지
    # 4/14 -8.42% 사고 대응: 같은 날 다수 청산 + 다수 신규 매수 동시 발생 방지
    # threshold=0 이면 규칙 비활성 (안전장치)
    daily_exit_cooldown_threshold: int = 3

    # 코어홀딩 (KR)
    max_core_positions: int = 3            # 코어홀딩 최대 동시 보유 수

    # 하이브리드 전략 (KR)
    hybrid: HybridConfig = field(default_factory=HybridConfig)

    # 전략별 총 예산 배분 (% of total_equity, 0=제한없음)
    strategy_allocation: Dict[str, float] = field(default_factory=lambda: {
        "core_holding": 30.0,
        "sepa_trend": 42.0,
        "rsi2_reversal": 17.5,
        "momentum_breakout": 0.0,
        "theme_chasing": 7.0,
        "gap_and_go": 3.5,
        "strategic_swing": 0.0,
    })


@dataclass
class TradingConfig:
    """트레이딩 설정 (KR + US 통합)"""
    # 기본 설정
    initial_capital: Decimal = Decimal("10000000")  # fallback (실제값은 KIS API에서 동기화)
    market: Market = Market.KRX

    # 수수료 - KR (2026년 한투 BanKIS 기준)
    buy_fee_rate: float = 0.000140527   # 매수 0.0140527%
    sell_fee_rate: float = 0.002130527  # 매도 0.0130527% + 증권거래세 0.20%

    # 수수료 - US (구조화된 커미션 설정)
    commission: CommissionConfig = field(default_factory=CommissionConfig)

    # 슬리피지
    expected_slippage_ticks: int = 1
    max_slippage_ticks: int = 3
    slippage: SlippageConfig = field(default_factory=SlippageConfig)

    # 시간대
    enable_pre_market: bool = True
    enable_next_market: bool = True       # KR 넥스트장
    enable_after_hours: bool = False      # US 시간외
    pre_market_slippage_buffer_pct: float = 3.0  # 프리장 익절 슬리피지 버퍼 (%)

    # 리스크
    risk: RiskConfig = field(default_factory=RiskConfig)
