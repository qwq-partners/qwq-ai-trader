"""
QWQ AI Trader - KR SEPA 트렌드 템플릿 스윙 전략

미너비니(Minervini) 트렌드 템플릿 기반 추세추종 전략.
강한 상승 추세 내에서 눌림목 진입 -> 추세 유지 시 보유.
원본: ai-trader-v2/src/strategies/sepa_trend.py
"""

from decimal import Decimal
from typing import Dict, List, Optional, Any

from loguru import logger

from ..base import BaseStrategy, StrategyConfig
from ...core.types import (
    Signal, Position, OrderSide, SignalStrength, StrategyType
)
from ...utils.sizing import atr_position_multiplier


class SEPATrendStrategy(BaseStrategy):
    """미너비니 SEPA 트렌드 템플릿 스윙 전략 (KR)"""

    def __init__(self, config: Optional[StrategyConfig] = None):
        if config is None:
            config = StrategyConfig(
                name="SEPATrend",
                strategy_type=StrategyType.SEPA_TREND,
                stop_loss_pct=5.0,
                take_profit_pct=8.0,
                min_score=70.0,
            )
        super().__init__(config)

        self.max_holding_days = config.params.get("max_holding_days", 10)

    async def generate_signal(
        self,
        symbol: str,
        current_price: Decimal,
        position: Optional[Position] = None
    ) -> Optional[Signal]:
        """실시간 시그널 생성 (스윙 전략에서는 미사용)"""
        return None

    def calculate_score(self, symbol: str) -> float:
        """신호 점수 계산"""
        return 0.0

    async def generate_batch_signals(self, candidates: List) -> List[Signal]:
        """
        배치 분석 결과 -> Signal 리스트

        Args:
            candidates: SwingCandidate 리스트 (SEPA 조건 통과)

        Returns:
            Signal 리스트
        """
        signals = []
        all_scores: List[tuple] = []
        rr_blocked: List[tuple] = []  # (symbol, stop_pct, target_pct, rr) 추적

        # supply_data_age 기반 min_score 동적 완화
        # T-2 캐시 사용 시 수급 최대점수 20→14점으로 감소 → min_score 보정
        supply_age_sample = 0
        if candidates:
            supply_age_sample = candidates[0].indicators.get("supply_data_age") or 0
        effective_min_score = self.config.min_score
        if supply_age_sample >= 2:
            effective_min_score = max(50.0, self.config.min_score - 10.0)
        elif supply_age_sample >= 1:
            effective_min_score = max(48.0, self.config.min_score - 5.0)
        if effective_min_score != self.config.min_score:
            logger.info(
                f"[SEPA] 수급 T-{supply_age_sample} → min_score 완화: "
                f"{self.config.min_score:.0f} → {effective_min_score:.0f}"
            )

        for candidate in candidates:
            try:
                # 과확장 차단: MA200 대비 +80% 이상 → 후행 추격 방지
                ma200_dist = candidate.indicators.get("ma200_distance_pct")
                if ma200_dist is not None and ma200_dist > 80:
                    logger.debug(f"[SEPA] {candidate.symbol} 과확장 차단: MA200 대비 +{ma200_dist:.0f}%")
                    continue

                score = self._calculate_sepa_score(candidate)
                all_scores.append((score, candidate.symbol, candidate.name))

                if score < effective_min_score:
                    continue

                # ATR 기반 동적 손절/익절
                # ▶ P0-1 수정: ExitManager(ATR×2.0, min=4%, max=7%)와 동일 공식 사용
                #   기존(ATR×1.5, min=2.5%) → R/R 체크가 실제 적용 stop과 달라 필터 무효
                #   수정 후: sepa_trend stop = ExitManager stop → R/R 체크가 실제 R/R 반영
                atr = candidate.indicators.get("atr_14")
                stop_pct = 5.0   # ATR 없을 때 기본값
                target_pct = 10.0
                if atr is not None and atr > 0:
                    # ExitConfig: atr_multiplier=2.0, min_stop_pct=4.0, max_stop_pct=7.0
                    stop_pct = max(4.0, min(7.0, atr * 2.0))
                    # target: stop × 1.5 이상 보장 + 최대 20% (추세 추종 공간 확보)
                    target_pct = max(stop_pct * 1.5, min(20.0, atr * 3.0))
                candidate.stop_price = candidate.entry_price * Decimal(str(1 - stop_pct / 100))
                candidate.target_price = candidate.entry_price * Decimal(str(1 + target_pct / 100))

                # R/R 비율 필터 (min_rr=1.5): 이제 실제 ExitManager stop과 일치
                if not self.check_rr_ratio(
                    candidate.entry_price, candidate.target_price,
                    candidate.stop_price, min_rr=1.5
                ):
                    rr = target_pct / stop_pct if stop_pct > 0 else 0
                    rr_blocked.append((candidate.symbol, candidate.name, stop_pct, target_pct, rr))
                    continue

                if score >= 85:
                    strength = SignalStrength.VERY_STRONG
                elif score >= 75:
                    strength = SignalStrength.STRONG
                else:
                    strength = SignalStrength.NORMAL

                atr_pct_value = candidate.indicators.get("atr_14", 0)
                atr_pct_value = atr_pct_value if atr_pct_value is not None else 0

                # ATR 기반 포지션 사이징 (고변동 → 비중 축소)
                _pos_mult = atr_position_multiplier(atr_pct_value)

                # 고점수(90+) 또는 섹터 RS 우수 → 자본 집중
                # 고확신 시 ATR 축소를 부분 완화하여 최소 0.8배 보장
                if score >= 90:
                    _pos_mult = max(_pos_mult * 1.4, 0.8)
                elif score >= 85 and (candidate.indicators.get("mrs") or 0) > 0:
                    _pos_mult = max(_pos_mult * 1.2, 0.7)

                signal = Signal(
                    symbol=candidate.symbol,
                    side=OrderSide.BUY,
                    strength=strength,
                    strategy=StrategyType.SEPA_TREND,
                    price=candidate.entry_price,
                    target_price=candidate.target_price,
                    stop_price=candidate.stop_price,
                    score=score,
                    confidence=score / 100.0,
                    reason=f"SEPA트렌드: {', '.join(candidate.reasons[:3])}",
                    metadata={
                        "strategy_name": self.name,
                        "candidate_name": candidate.name,
                        "indicators": candidate.indicators,
                        "atr_pct": atr_pct_value,
                        "position_multiplier": _pos_mult,
                    },
                )
                signals.append(signal)

                logger.info(
                    f"[SEPA] 시그널: {candidate.symbol} {candidate.name} "
                    f"점수={score:.0f} MRS={candidate.indicators.get('mrs', 'N/A')} "
                    f"LCI={candidate.indicators.get('lci', 'N/A')}"
                )

            except Exception as e:
                logger.warning(f"[SEPA] {candidate.symbol} 시그널 생성 실패: {e}")

        # R/R 차단 현황 로그 (블랙박스 제거)
        if rr_blocked:
            logger.warning(
                f"[SEPA] R/R 차단: {len(rr_blocked)}개 "
                f"(target cap과 stop 충돌 → R/R<1.5 종목)"
            )
            for sym, name, sp, tp, rr in rr_blocked[:5]:
                logger.warning(f"  ✗ {sym} {name}: stop={sp:.1f}% target={tp:.1f}% R/R={rr:.2f}")

        # 점수 분포 요약 로그
        if all_scores:
            all_scores.sort(reverse=True)
            top = all_scores[:10]
            passed = sum(1 for s, _, _ in all_scores if s >= effective_min_score)
            rr_block_count = len(rr_blocked)
            logger.info(
                f"[SEPA] 점수분포: 전체={len(all_scores)}개, "
                f"통과={passed}개 (min={effective_min_score:.0f}), "
                f"R/R차단={rr_block_count}개, "
                f"평균={sum(s for s,_,_ in all_scores)/len(all_scores):.1f}, "
                f"최고={all_scores[0][0]:.1f}"
            )
            logger.info(f"[SEPA] 상위 10개 점수:")
            for score_val, sym, name in top:
                lci = None
                atr_val = None
                for c in candidates:
                    if c.symbol == sym:
                        lci = c.indicators.get("lci")
                        atr_val = c.indicators.get("atr_14")
                        break
                mark = "v" if score_val >= effective_min_score else " "
                logger.info(
                    f"  {mark} {sym} {name}: {score_val:.1f}pt  "
                    f"LCI={f'{lci:.2f}' if lci is not None else 'None'}  "
                    f"ATR={f'{atr_val:.1f}%' if atr_val is not None else 'N/A'}"
                )

        return signals

    def _calculate_sepa_score(self, candidate) -> float:
        """
        SEPA 트렌드 점수 계산 (0-100)

        - 기술적 (SEPA, MA정렬, 52w위치, MRS, MA5>MA20): 40점
        - 수급 LCI z-score 기반: 20점
        - 재무 (ROE 중심 축소): 10점
        - 거래량 모멘텀: 10점
        - 섹터 모멘텀: 10점
        """
        ind = candidate.indicators
        score = 0.0

        # 1. 기술적 (40점)
        if ind.get("sepa_pass"):
            score += 15

        ma50 = ind.get("ma50")
        ma200 = ind.get("ma200")
        if ma50 is not None and ma200 is not None and ma200 > 0:
            spread = (ma50 - ma200) / ma200 * 100
            if spread > 10:
                score += 7
            elif spread > 5:
                score += 5
            elif spread > 0:
                score += 3

        close = ind.get("close")
        high_52w = ind.get("high_52w")
        if close is not None and high_52w is not None and high_52w > 0:
            from_high = (close - high_52w) / high_52w * 100
            if from_high >= -5:
                score += 7
            elif from_high >= -10:
                score += 5
            elif from_high >= -15:
                score += 3

        mrs = ind.get("mrs")
        mrs_slope = ind.get("mrs_slope", 0)
        if mrs is not None:
            if mrs > 0 and mrs_slope > 0:
                score += 5
            elif mrs > 0:
                score += 3

        if ind.get("ma5_above_ma20", False):
            score += 3

        # MA200 과확장 감점 (60일 급등 후행 추격 방지)
        ma200_dist = ind.get("ma200_distance_pct")
        if ma200_dist is not None:
            if ma200_dist > 50:
                score -= 10
            elif ma200_dist > 30:
                score -= 5

        # 20일 고점 대비 눌림 보너스 / 추격 감점
        high_20d = ind.get("high_20d")
        if high_20d is not None and high_20d > 0 and close is not None and close > 0:
            pullback_pct = (close - high_20d) / high_20d * 100
            if -7 <= pullback_pct <= -3:
                score += 5   # 적정 눌림: 보너스
            elif pullback_pct > 0:
                score -= 5   # 20일 고가 돌파 직후: 추격 감점

        # 2. 수급 LCI z-score 기반 (20점)
        # supply_data_age: 0=당일, 1=전일(T-1), 2=캐시(T-2+)
        supply_age = ind.get("supply_data_age", 0) if ind.get("supply_data_age") is not None else 0
        lci_discount = max(0.7, 1.0 - supply_age * 0.15)  # T-1: 85%, T-2: 70%

        lci = ind.get("lci")
        if lci is not None:
            if lci > 1.5:
                score += int(20 * lci_discount)
            elif lci > 1.0:
                score += int(15 * lci_discount)
            elif lci > 0.5:
                score += int(10 * lci_discount)
            elif lci > 0:
                score += int(5 * lci_discount)
        else:
            foreign_net = ind.get("foreign_net_buy") if ind.get("foreign_net_buy") is not None else 0
            inst_net = ind.get("inst_net_buy") if ind.get("inst_net_buy") is not None else 0
            if foreign_net > 0 or inst_net > 0:
                supply_score = (10 if foreign_net > 0 else 0) + (10 if inst_net > 0 else 0)
                score += int(min(supply_score, 20) * lci_discount)
            else:
                score += 5

        # 3. 재무 (10점)
        per = ind.get("per", 0)
        pbr = ind.get("pbr", 0)
        roe = ind.get("roe", 0)

        if per is not None and 0 < per < 20:
            score += 2
        elif per is not None and 0 < per < 30:
            score += 1

        # 적자 기업 감점: PER < 0 → -5점
        if per is not None and per < 0:
            score -= 5

        if pbr is not None and 0 < pbr < 3:
            score += 2
        elif pbr is not None and 0 < pbr < 5:
            score += 1

        # 고평가 감점: PBR > 10 → -3점
        if pbr is not None and pbr > 10:
            score -= 3

        if roe is not None and roe > 10:
            score += 6
        elif roe is not None and roe > 5:
            score += 3

        # 4. 거래량 모멘텀 (10점)
        vol_ratio = ind.get("vol_ratio")
        if vol_ratio is None:
            vol_ratio = ind.get("volume_ratio")
        if vol_ratio is None:
            vol_ratio = ind.get("vol_inrt")
        if vol_ratio is None:
            vol_ratio = 0
        try:
            vol_ratio = float(vol_ratio)
        except (TypeError, ValueError):
            vol_ratio = 0.0
        if vol_ratio > 2.0:
            score += 10
        elif vol_ratio > 1.5:
            score += 7
        elif vol_ratio > 1.0:
            score += 4

        # 5. 섹터 모멘텀 (10점)
        sm_score = ind.get("sector_momentum_score")
        if sm_score is not None:
            score += max(0.0, min(10.0, float(sm_score)))
        else:
            change_20d = ind.get("change_20d") if ind.get("change_20d") is not None else 0
            try:
                change_20d = float(change_20d)
            except (TypeError, ValueError):
                change_20d = 0.0
            if change_20d > 20:
                score += 10
            elif change_20d > 10:
                score += 7
            elif change_20d > 5:
                score += 4
            elif change_20d > 0:
                score += 2

        # 전략적 오버레이 보너스 (VCP / 전문가패널 / 수급추세) — swing_screener에서 계산
        overlay = candidate.indicators.get("overlay_bonus") if candidate.indicators.get("overlay_bonus") is not None else 0.0
        score += overlay

        return max(0, min(score, 100))
