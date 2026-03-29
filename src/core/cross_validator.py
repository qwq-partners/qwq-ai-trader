"""
QWQ AI Trader - 크로스 전략 검증 게이트

다중 전략 신호를 교차 검증하여 맹점을 보완합니다.
각 전략이 독립적으로 시그널을 발행하는 구조에서,
전략 간 모순, 수급-기술 불일치, 시장 체제 부적합 등을 감지합니다.

PRISM-INSIGHT의 "투자전략가" 패턴을 규칙 기반으로 구현.
"""

from datetime import datetime
from typing import Dict, Optional, List, Tuple
from loguru import logger


class CrossStrategyValidator:
    """
    크로스 전략 검증 게이트

    시그널이 엔진(on_signal)에 도달하기 전에 교차 검증을 수행합니다.
    규칙 기반으로 동작하므로 LLM 호출 없이 실시간 성능을 유지합니다.
    """

    def __init__(self, portfolio=None, risk_manager=None, trade_memory=None,
                 llm_manager=None):
        self._portfolio = portfolio
        self._risk_manager = risk_manager
        self._trade_memory = trade_memory
        self._llm_manager = llm_manager  # LLM 종합 판단 (선택적)

        # 오늘 검증 통계
        self._stats = {
            "total": 0,
            "passed": 0,
            "blocked": 0,
            "penalized": 0,
        }
        self._stats_date = None

    def set_portfolio(self, portfolio):
        self._portfolio = portfolio

    def set_risk_manager(self, risk_manager):
        self._risk_manager = risk_manager

    def validate(
        self,
        symbol: str,
        side: str,
        strategy: str,
        score: float,
        metadata: dict,
        market_regime: str = "neutral",
    ) -> Tuple[bool, float, str]:
        """
        시그널 교차 검증

        Args:
            symbol: 종목 코드
            side: "buy" / "sell"
            strategy: 전략명 (sepa_trend, theme_chasing, ...)
            score: 전략 점수
            metadata: 시그널 메타데이터 (indicators, atr_pct, sector 등)
            market_regime: 시장 체제 ("bull", "bear", "sideways", "neutral")

        Returns:
            (통과 여부, 조정된 점수, 사유)
        """
        # 일일 통계 리셋
        today = datetime.now().date()
        if self._stats_date != today:
            self._stats = {"total": 0, "passed": 0, "blocked": 0, "penalized": 0}
            self._stats_date = today

        self._stats["total"] += 1

        # 매도 시그널은 검증 없이 통과 (청산은 항상 허용)
        if side != "buy":
            self._stats["passed"] += 1
            return True, score, ""

        indicators = metadata.get("indicators") or {}
        penalties = []
        adjusted_score = score

        # === 규칙 1: 기술적 과매수 상태에서 추세 전략 매수 ===
        rsi_14 = indicators.get("rsi_14") or indicators.get("rsi")
        if rsi_14 is not None and rsi_14 > 70 and strategy in ("sepa_trend", "momentum_breakout"):
            adjusted_score -= 10
            penalties.append(f"RSI과매수({rsi_14:.0f}>70) -10")

        # === 규칙 2: 기관+외국인 동시 순매도 상태에서 테마/모멘텀 매수 ===
        foreign_net = indicators.get("foreign_net_buy")
        inst_net = indicators.get("inst_net_buy")
        if foreign_net is not None and inst_net is not None:
            if foreign_net < 0 and inst_net < 0 and strategy in ("theme_chasing", "momentum_breakout", "gap_and_go"):
                self._stats["blocked"] += 1
                logger.info(
                    f"[크로스검증] {symbol} 차단: {strategy} 매수 + 기관/외국인 동시 순매도"
                )
                return False, 0, "수급 불일치: 기관+외국인 동시 순매도"

        # === 규칙 3: 약세 체제에서 공격적 전략 차단 ===
        if market_regime == "bear" and strategy in ("theme_chasing", "gap_and_go"):
            self._stats["blocked"] += 1
            logger.info(
                f"[크로스검증] {symbol} 차단: 약세장 + {strategy}"
            )
            return False, 0, f"체제 부적합: 약세장에서 {strategy} 차단"

        # === 규칙 4: 동일 섹터 과집중 ===
        sector = metadata.get("sector")
        if sector and self._portfolio:
            same_sector_count = sum(
                1 for p in self._portfolio.positions.values()
                if getattr(p, 'sector', None) == sector
            )
            if same_sector_count >= 3:
                self._stats["blocked"] += 1
                logger.info(
                    f"[크로스검증] {symbol} 차단: 섹터 과집중 ({sector}: {same_sector_count}종목)"
                )
                return False, 0, f"섹터 집중: {sector} {same_sector_count}종목 보유 중"

        # === 규칙 5: 당일 손절 종목과 동일 섹터 재진입 경고 ===
        # 손절 종목의 섹터 정보를 직접 비교 (섹터 미확인 시 스킵)
        if sector and self._risk_manager:
            exited = getattr(self._risk_manager, '_exited_today', {})
            for sl_symbol, sl_info in exited.items():
                sl_sector = sl_info.get("sector") if isinstance(sl_info, dict) else None
                if sl_sector and sl_sector == sector and sl_symbol != symbol:
                    adjusted_score -= 5
                    penalties.append(f"동일섹터({sector}) 손절종목 존재 -5")
                    break

        # === 규칙 6: ATR 대비 등락률 과다 (추격 매수 감지) ===
        atr_pct = metadata.get("atr_pct") or indicators.get("atr_14")
        rt_change = indicators.get("change_1d") or indicators.get("change_pct") or indicators.get("rt_change_pct")
        if atr_pct is not None and atr_pct > 0 and rt_change is not None and rt_change > 0:
            surge_ratio = rt_change / atr_pct
            if surge_ratio > 1.5:
                adjusted_score -= 15
                penalties.append(f"추격매수(등락/ATR={surge_ratio:.1f}x) -15")

        # === 규칙 7: MA200 하방에서 추세 추종 (SEPA/테마) ===
        ma200 = indicators.get("ma200")
        close = indicators.get("close")
        if ma200 is not None and close is not None and ma200 > 0:
            if close < ma200 and strategy in ("sepa_trend", "theme_chasing"):
                adjusted_score -= 10
                penalties.append(f"MA200하방(-{(1-close/ma200)*100:.1f}%) -10")

        # === 규칙 8: 펀더멘탈 밸류에이션 필터 (PRISM 차용) ===
        # PER 극단 고평가 또는 적자 + 고PBR → 추격 매수 위험
        per = indicators.get("per")
        pbr = indicators.get("pbr")
        if per is not None and pbr is not None:
            # 적자(PER<0) + 고PBR(>5) = 투기적 고평가
            if per < 0 and pbr > 5:
                adjusted_score -= 10
                penalties.append(f"적자+고PBR({pbr:.1f}) -10")
            # PER > 50 = 극단 고평가 (성장주 프리미엄 감안해도 과도)
            elif per > 50:
                adjusted_score -= 5
                penalties.append(f"극단PER({per:.0f}) -5")

        # === 규칙 9: 거래 메모리 기반 점수 보정 ===
        if self._trade_memory:
            memory_adj = self._trade_memory.get_score_adjustment(strategy, sector or "")
            if memory_adj != 0:
                adjusted_score += memory_adj
                penalties.append(f"메모리보정({memory_adj:+d})")

        # 감점 적용 결과
        if penalties:
            self._stats["penalized"] += 1
            penalty_str = ", ".join(penalties)
            logger.info(
                f"[크로스검증] {symbol} 감점: {score:.0f}→{adjusted_score:.0f} ({penalty_str})"
            )

        # 감점 후 최소 점수 미달이면 차단
        if adjusted_score < 50:
            self._stats["blocked"] += 1
            logger.info(
                f"[크로스검증] {symbol} 차단: 감점 후 {adjusted_score:.0f} < 50"
            )
            return False, adjusted_score, f"크로스 감점 후 점수 부족 ({adjusted_score:.0f})"

        self._stats["passed"] += 1
        return True, adjusted_score, ""

    async def llm_second_check(
        self,
        symbol: str,
        strategy: str,
        score: float,
        indicators: dict,
        market_regime: str,
    ) -> bool:
        """
        LLM 종합 판단 — 고점수(85+) + 비강세장에서만 호출 (PRISM 차용)

        비용 최소화: 하루 최대 3~5회, 거부 시 score -20.
        실시간 성능: 타임아웃 10초, 실패 시 통과(fail-open).
        """
        if not self._llm_manager:
            return True

        # 강세장이면 LLM 검증 생략 (속도 우선)
        if market_regime == "bull":
            return True

        # 고점수 시그널만 검증
        if score < 85:
            return True

        try:
            prompt = (
                f"종목 {symbol}, 전략 {strategy}, 점수 {score:.0f}.\n"
                f"시장 체제: {market_regime}.\n"
                f"지표: RSI={indicators.get('rsi_14', 'N/A')}, "
                f"ATR={indicators.get('atr_14', 'N/A')}%, "
                f"MA200거리={indicators.get('ma200_distance_pct', 'N/A')}%, "
                f"PER={indicators.get('per', 'N/A')}, "
                f"수급={'+' if (indicators.get('foreign_net_buy') or 0) > 0 else '-'}.\n\n"
                f"이 매수 시그널을 승인하시겠습니까? "
                f"YES 또는 NO로 답하고, 한 줄 사유를 적어주세요."
            )
            import asyncio
            response = await asyncio.wait_for(
                self._llm_manager.generate(prompt, max_tokens=100),
                timeout=10.0,
            )
            if response and "NO" in response.upper()[:10]:
                logger.info(f"[크로스검증] LLM 거부: {symbol} — {response[:80]}")
                return False
            return True
        except Exception as e:
            logger.debug(f"[크로스검증] LLM 검증 실패 (통과): {e}")
            return True  # fail-open

    def get_stats(self) -> Dict:
        """오늘 검증 통계"""
        return dict(self._stats)
