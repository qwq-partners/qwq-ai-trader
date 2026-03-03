"""
QWQ AI Trader - MarketContext (시장별 컨텍스트 번들)

각 시장(KR, US)의 핵심 컴포넌트를 하나의 객체로 묶어
UnifiedEngine이 시장별 작업을 일관되게 수행할 수 있게 합니다.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .types import Market, Portfolio


@dataclass
class MarketContext:
    """시장별 컨텍스트 번들

    각 시장(KR/US)의 핵심 컴포넌트를 하나의 객체로 묶습니다.
    UnifiedEngine.contexts["KR"] / UnifiedEngine.contexts["US"]로 접근합니다.

    Attributes:
        market: 시장 식별자 (Market.KRX, Market.NASDAQ 등)
        broker: 브로커 인스턴스 (KISBroker 또는 KISUSBroker)
        session: 세션 관리자 (KRSession 또는 USSession)
        portfolio: 포트폴리오 (현금, 포지션 등)
        risk_mgr: 리스크 매니저 (시장별 독립 관리)
        exit_mgr: 분할 익절/청산 관리자
        strategies: 활성 전략 목록
        screener: 종목 스크리너 (시장별)
        config: 시장별 설정 딕셔너리
        enabled: 이 시장이 활성화되었는지 여부
    """

    # 시장 식별
    market: Market = Market.KRX

    # 핵심 컴포넌트 (초기화 시 주입)
    broker: Any = None           # BaseBroker 구현체
    session: Any = None          # KRSession 또는 USSession
    portfolio: Optional[Portfolio] = None

    # 리스크 & 청산 관리
    risk_mgr: Any = None         # RiskManager (placeholder)
    exit_mgr: Any = None         # ExitManager (placeholder)

    # 전략
    strategies: List[Any] = field(default_factory=list)   # BaseStrategy 목록
    strategy_manager: Any = None  # StrategyManager (KR) 또는 전략 리스트 관리자

    # 스크리너
    screener: Any = None          # StockScreener (시장별)

    # 데이터 소스
    data_feed: Any = None         # WebSocket 또는 REST 피드
    market_data: Any = None       # KISMarketData / YFinanceProvider 등

    # 설정
    config: Dict[str, Any] = field(default_factory=dict)

    # 활성화 여부
    enabled: bool = True

    # 추가 컴포넌트 (시장별)
    extras: Dict[str, Any] = field(default_factory=dict)

    @property
    def name(self) -> str:
        """시장 이름 (로깅용)"""
        return self.market.value

    @property
    def is_kr(self) -> bool:
        """한국 시장 여부"""
        return self.market in (Market.KRX, Market.KRX_EXT)

    @property
    def is_us(self) -> bool:
        """미국 시장 여부"""
        return self.market in (Market.NASDAQ, Market.NYSE, Market.AMEX)
