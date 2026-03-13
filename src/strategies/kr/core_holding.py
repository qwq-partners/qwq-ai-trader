"""
QWQ AI Trader - KR 코어홀딩 중장기 전략

전체 자본의 30% 예산으로 최대 3종목 장기 보유.
월 1회 리밸런싱, 우상향 대형주 중심.
분할 익절 비활성화, 손절 -15%, 트레일링 고점 -8%.
"""

from decimal import Decimal
from typing import Dict, List, Optional, Any

from loguru import logger

from ..base import BaseStrategy, StrategyConfig
from ...core.types import (
    Signal, Position, OrderSide, SignalStrength, StrategyType
)


class CoreHoldingStrategy(BaseStrategy):
    """코어홀딩 중장기 전략 (KR)

    스코어링 (100점 만점):
        추세 안정성  30점: MA정배열, MA200위, 52주고점근접, 6개월수익률, 저변동성
        펀더멘탈    30점: ROE, 영업이익률, PER, PBR, 시총, 배당
        수급 추세   20점: 외인순매수5일, 기관순매수5일
        모멘텀 품질 20점: MRS, 20일수익률, 60일수익률, MA5>MA20
    """

    def __init__(self, config: Optional[StrategyConfig] = None):
        if config is None:
            config = StrategyConfig(
                name="CoreHolding",
                strategy_type=StrategyType.CORE_HOLDING,
                stop_loss_pct=15.0,
                take_profit_pct=50.0,
                min_score=70.0,
            )
        super().__init__(config)

    async def generate_signal(
        self,
        symbol: str,
        current_price: Decimal,
        position: Optional[Position] = None
    ) -> Optional[Signal]:
        """실시간 시그널 생성 (코어홀딩은 배치 전용, 미사용)"""
        return None

    def calculate_score(self, symbol: str) -> float:
        """신호 점수 계산 (배치용 래퍼)"""
        return 0.0

    async def generate_batch_signals(self, candidates: List) -> List[Signal]:
        """
        배치 분석 결과 -> Signal 리스트

        Args:
            candidates: CoreScreener에서 스코어링 완료된 후보 리스트
                        각 후보는 .symbol, .name, .entry_price, .score,
                        .indicators, .reasons 속성 필요

        Returns:
            Signal 리스트
        """
        signals = []
        min_score = self.config.min_score

        for candidate in candidates:
            try:
                score = candidate.score if hasattr(candidate, 'score') else 0.0

                if score < min_score:
                    continue

                if score >= 85:
                    strength = SignalStrength.VERY_STRONG
                elif score >= 75:
                    strength = SignalStrength.STRONG
                else:
                    strength = SignalStrength.NORMAL

                # 코어홀딩: 손절 -15%, 목표가 없음 (장기 보유)
                entry_price = candidate.entry_price
                stop_price = entry_price * Decimal(str(1 - 15.0 / 100))

                reasons = candidate.reasons if hasattr(candidate, 'reasons') else []

                signal = Signal(
                    symbol=candidate.symbol,
                    side=OrderSide.BUY,
                    strength=strength,
                    strategy=StrategyType.CORE_HOLDING,
                    price=entry_price,
                    target_price=None,
                    stop_price=stop_price,
                    score=score,
                    confidence=score / 100.0,
                    reason=f"코어홀딩: {', '.join(reasons[:3])}",
                    metadata={
                        "strategy_name": self.name,
                        "candidate_name": getattr(candidate, 'name', ''),
                        "indicators": getattr(candidate, 'indicators', {}),
                        "is_core": True,
                    },
                )
                signals.append(signal)

                logger.info(
                    f"[코어홀딩] 시그널: {candidate.symbol} "
                    f"{getattr(candidate, 'name', '')} "
                    f"점수={score:.0f}"
                )

            except Exception as e:
                sym = getattr(candidate, 'symbol', '?')
                logger.warning(f"[코어홀딩] {sym} 시그널 생성 실패: {e}")

        if signals:
            logger.info(f"[코어홀딩] 총 {len(signals)}개 시그널 생성")

        return signals
