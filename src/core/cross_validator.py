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

    # 감점 후 최소 통과 점수 (이하면 차단)
    _MIN_PASS_SCORE: int = 50

    # 2026-04-23 추가: 튜닝 가능 상수 (매직넘버 정리)
    # 지표 결손 감점: 결손 지표 1개당 -N점, 최대 -M점
    _MISSING_IND_PENALTY_STEP: int = 2   # 지표 1개당 감점
    _MISSING_IND_PENALTY_CAP: int = 8    # 최대 감점 (지표 4개 이상 결손 시 적용)

    def __init__(self, portfolio=None, risk_manager=None, trade_memory=None,
                 llm_manager=None, market: str = "KR", trade_wiki=None,
                 max_sector_positions: int = 2,
                 # 2026-04-23 추가: YAML 토글 가능 튜닝 파라미터
                 min_pass_score: Optional[int] = None,
                 missing_indicator_penalty_step: Optional[int] = None,
                 missing_indicator_penalty_cap: Optional[int] = None,
                 llm_daily_max: Optional[int] = None):
        self._portfolio = portfolio
        self._risk_manager = risk_manager
        self._trade_memory = trade_memory
        self._llm_manager = llm_manager  # LLM 종합 판단 (선택적)
        self._market = market  # "KR" 또는 "US"
        self._trade_wiki = trade_wiki  # 거래 위키 (교훈 컨텍스트)
        self._max_sector_positions = max_sector_positions  # 동일 섹터 최대 포지션 수 (설정 참조)

        # YAML 토글 적용 (None이면 클래스 상수 사용)
        if min_pass_score is not None:
            self._MIN_PASS_SCORE = int(min_pass_score)
        if missing_indicator_penalty_step is not None:
            self._MISSING_IND_PENALTY_STEP = int(missing_indicator_penalty_step)
        if missing_indicator_penalty_cap is not None:
            self._MISSING_IND_PENALTY_CAP = int(missing_indicator_penalty_cap)

        # 오늘 검증 통계
        self._stats = {
            "total": 0,
            "passed": 0,
            "blocked": 0,
            "penalized": 0,
        }
        self._stats_date = None

        # LLM 이중 검증 일일 한도 (비용 폭발 방지)
        self._daily_llm_count: int = 0
        self._daily_llm_count_date = None
        self._daily_llm_max: int = int(llm_daily_max) if llm_daily_max is not None else 10

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

        # 2026-04-25 추가: 진입 시간대 가드 (KR 전용)
        # 30일 데이터: 09시 -440k(승률 26.7%) / 10시 +181k / 12시 +1.08M(승률 70%)
        # → 09:00~09:29 하드 차단, 09:30~10:30 -8점 페널티, 12:30~13:00 +5 보너스
        # 청산 시그널은 시간 가드 미적용 (sell은 통과)
        if self._market == "KR" and side == "buy":
            from datetime import datetime as _dt
            now = _dt.now()
            now_hm = now.hour * 100 + now.minute  # 930 = 09:30
            # 09:00~09:29 하드 차단 (장초반 30분 모든 매수 신호 차단)
            # 단, core_holding은 별도 배치(09:30 execute)로 처리되므로 영향 없음
            if 900 <= now_hm < 930 and strategy != "core_holding":
                self._stats["blocked"] += 1
                logger.info(
                    f"[크로스검증] {symbol} 차단: 09:00~09:29 장초반 변동성 회피 "
                    f"({strategy}, KR 30일 -440k 패턴)"
                )
                return False, 0, "장초반 30분 진입 차단 (09:00~09:29)"
            # 09:30~10:30 -8점 페널티 (변동성 방향 미확정 구간)
            # core_holding/strategic_swing은 배치(T+1) 전략 — 09:30 시작이 정상 동작이므로 예외.
            # 주의: strategic_swing이 미래에 장중 진입 추가 시 이 면제 재검토 필수.
            if 930 <= now_hm < 1030 and strategy not in ("core_holding", "strategic_swing"):
                adjusted_score -= 8
                penalties.append("장초반 변동성 -8 (09:30~10:30)")
            # 12:30~13:00 +5 보너스 (점심 batch 추세 확정 후 진입 sweet spot)
            if 1230 <= now_hm <= 1300:
                adjusted_score += 5
                penalties.append("점심 sweet spot +5 (12:30~13:00)")

        # 2026-04-25 추가: sepa_trend 고점수 추격매수 페널티
        # 90일 데이터 역설: 80~90점 승률 31.8% / -636k, 60~70점 승률 82.4% / +937k
        # 고점수가 오히려 추격매수 함정 → 90+ -10점 페널티
        if side == "buy" and strategy == "sepa_trend" and score >= 90:
            adjusted_score -= 10
            penalties.append("SEPA 90+ 추격매수 -10")

        # 2026-04-23 추가: 지표 결손 시 보수적 감점 (risk-auditor 감사 결과)
        # atr_pct 78.6%, PER/PBR 87.7%, 수급 78.6% 결손 → R6/R7/R8/R2 사실상 비활성.
        # 단기 임시 방편: 지표 None이면 "불확실 = 의심" 원칙으로 -2점 × 결손 지표 수.
        # 2026-04-23 수정: metadata 상위 키(atr_pct, sector 등)와 indicators dict 둘 다 체크.
        #   스크리너는 top-level metadata, 전략은 indicators dict로 공급하는 구조 혼재.
        _missing_key_indicators = []
        _has_atr = (
            indicators.get("atr_pct") is not None
            or indicators.get("atr_14") is not None
            or metadata.get("atr_pct") is not None
        )
        if not _has_atr:
            _missing_key_indicators.append("ATR")
        if indicators.get("per") is None and metadata.get("per") is None:
            _missing_key_indicators.append("PER")
        if indicators.get("pbr") is None and metadata.get("pbr") is None:
            _missing_key_indicators.append("PBR")
        # 수급은 KR 전용
        if self._market == "KR":
            _has_supply = (
                indicators.get("foreign_net_buy") is not None
                or indicators.get("inst_net_buy") is not None
                or metadata.get("foreign_net_buy") is not None
                or metadata.get("inst_net_buy") is not None
            )
            if not _has_supply:
                _missing_key_indicators.append("수급")
        if _missing_key_indicators:
            _missing_penalty = min(
                len(_missing_key_indicators) * self._MISSING_IND_PENALTY_STEP,
                self._MISSING_IND_PENALTY_CAP,
            )
            adjusted_score -= _missing_penalty
            penalties.append(f"지표결손({','.join(_missing_key_indicators)}) -{_missing_penalty}")

        # === 규칙 1: 기술적 과매수 상태에서 추세 전략 매수 ===
        # 중복 감점 주의: kr_screener._apply_momentum_filter가 이미 RSI>75 시 -10, >70 시 -5 적용.
        # 여기서는 크로스검증 단계(체제/수급 종합 판단) 보정이므로 감점 규모를 -5로 축소해
        # 이중 감점 폭을 -20 → -15로 완화. bull 체제는 RSI 70~80이 모멘텀의 sweet spot이므로
        # bull에서는 감점 생략.
        rsi_14 = indicators.get("rsi_14")
        if rsi_14 is None:
            rsi_14 = indicators.get("rsi")
        if (rsi_14 is not None and rsi_14 > 70
                and market_regime != "bull"
                and strategy in ("sepa_trend", "momentum_breakout")):
            adjusted_score -= 5
            penalties.append(f"RSI과매수({rsi_14:.0f}>70) -5")

        # === 규칙 2: 기관+외국인 동시 순매도 (KR 전용 — US는 수급 데이터 없음) ===
        if self._market == "KR":
            foreign_net = indicators.get("foreign_net_buy")
            inst_net = indicators.get("inst_net_buy")
            if foreign_net is not None and inst_net is not None:
                if foreign_net < 0 and inst_net < 0:
                    if strategy in ("theme_chasing", "momentum_breakout", "gap_and_go"):
                        self._stats["blocked"] += 1
                        logger.info(
                            f"[크로스검증] {symbol} 차단: {strategy} 매수 + 기관/외국인 동시 순매도"
                        )
                        return False, 0, "수급 불일치: 기관+외국인 동시 순매도"
                    # sepa_trend: 배치(T+1) 특성상 완전 차단 대신 감점 처리
                    if strategy == "sepa_trend":
                        adjusted_score -= 10
                        penalties.append(f"[규칙2] 기관+외국인 동시 순매도 — SEPA 감점 -10")

        # === 규칙 3: 약세 체제에서 공격적/역추세 전략 차단 ===
        # KR: rsi2_reversal 추가 — Connors 원전 규칙(지수 약세 시 RSI(2) 진입 금지) 준수,
        #     "칼떨어지는 칼" 진입 방지. momentum_breakout도 약세장 추격 매수 위험으로 차단.
        # US: earnings_drift는 어닝 서프라이즈 기반 → bear에서도 허용
        _bear_block = ("theme_chasing", "gap_and_go", "rsi2_reversal", "momentum_breakout")
        if self._market == "US":
            _bear_block = ("momentum_breakout",)  # US bear: 모멘텀만 차단, SEPA/어닝은 허용
        if market_regime == "bear" and strategy in _bear_block:
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
            if same_sector_count >= self._max_sector_positions:
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

        # === 규칙 5-2 (2026-04-25 추가): 외국인 5일 누적 매수 상위 섹터 보너스 ===
        # supply_score_provider 또는 supply_daily_*.json 5일 누적 합계 기준,
        # 외국인 매수 강한 섹터 종목에 +5 overlay (수급 모멘텀 추적)
        if sector and self._market == "KR":
            top_sectors = metadata.get("foreign_top_sectors") or []
            if isinstance(top_sectors, (list, tuple)) and sector in top_sectors:
                adjusted_score += 5
                penalties.append(f"외국인 매수 상위 섹터({sector}) +5")

        # === 규칙 6: ATR 대비 등락률 과다 (추격 매수 감지) ===
        atr_pct = metadata.get("atr_pct")
        if atr_pct is None:
            atr_pct = indicators.get("atr_14")
        rt_change = indicators.get("change_1d")
        if rt_change is None:
            rt_change = indicators.get("change_pct")
        if rt_change is None:
            rt_change = indicators.get("rt_change_pct")
        if atr_pct is not None and atr_pct > 0 and rt_change is not None and rt_change > 0:
            surge_ratio = rt_change / atr_pct
            if surge_ratio > 1.5:
                adjusted_score -= 15
                penalties.append(f"추격매수(등락/ATR={surge_ratio:.1f}x) -15")

        # === 규칙 7: MA200 하방에서 추세 추종 (SEPA/테마) ===
        # 중복 감점 주의: swing_screener._base_technical_score에서도 MA200 상방이 보너스 조건.
        # SEPA 후보가 스크리너에서 이미 감점된 상태로 올라오므로 여기서는 -5로 축소해
        # 중복 폭을 -20 → -15로 완화.
        ma200 = indicators.get("ma200")
        close = indicators.get("close")
        if ma200 is not None and close is not None and ma200 > 0:
            if close < ma200 and strategy in ("sepa_trend", "theme_chasing"):
                adjusted_score -= 5
                penalties.append(f"MA200하방(-{(1-close/ma200)*100:.1f}%) -5")

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

        # === 누적 감점 cap (2026-05-03 P0-1) ===
        # 시간대 -8 + 지표결손 -8 + MA200 -5 + 극단PER -5 = 최대 -26 누적 가능
        # 60-70점대 종목이 자동 차단되는 역설(이전 분석: 91.7% 승률 영역) 방지
        # 추격매수(-15)/RSI과매수(-5)/적자+고PBR(-10)는 hard block 의도라 캡 예외
        TOTAL_PENALTY_CAP = 15
        _HARD_BLOCK_TAGS = ("추격매수", "RSI과매수", "적자+고PBR")
        has_hard_block = any(any(tag in p for tag in _HARD_BLOCK_TAGS) for p in penalties)
        if not has_hard_block:
            total_penalty = score - adjusted_score
            if total_penalty > TOTAL_PENALTY_CAP:
                penalties.append(
                    f"누적감점캡({total_penalty:.0f}→{TOTAL_PENALTY_CAP})"
                )
                adjusted_score = score - TOTAL_PENALTY_CAP

        # 감점 적용 결과
        if penalties:
            self._stats["penalized"] += 1
            penalty_str = ", ".join(penalties)
            logger.info(
                f"[크로스검증] {symbol} 감점: {score:.0f}→{adjusted_score:.0f} ({penalty_str})"
            )

        # 감점 후 최소 점수 미달이면 차단
        if adjusted_score < self._MIN_PASS_SCORE:
            self._stats["blocked"] += 1
            logger.info(
                f"[크로스검증] {symbol} 차단: 감점 후 {adjusted_score:.0f} < {self._MIN_PASS_SCORE}"
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
        sector: str = "",
    ) -> bool:
        """
        LLM 종합 판단 — 고점수(85+) + 비강세장에서만 호출 (PRISM 차용)

        비용 최소화: 하루 최대 10회 (_daily_llm_max), 거부 시 주문 차단.
        실시간 성능: 타임아웃 10초, 실패 시 통과(fail-open).

        fail-open 정책 의도: LLM 장애나 한도 소진 시 매수 차단보다 기회 손실 방지를 우선.
        규칙1~9는 이미 결정론적 게이트로 작동하므로 LLM은 추가 안전장치 역할.
        엄격 모드가 필요하면 return True → return False 로 변경 (정책 결정 사안).
        """
        if not self._llm_manager:
            return True

        # 강세장이면 LLM 검증 생략 (속도 우선)
        if market_regime == "bull":
            return True

        # 고점수 시그널만 검증
        if score < 85:
            return True

        # 일일 한도 체크 (비용 제어)
        today = datetime.now().date()
        if self._daily_llm_count_date != today:
            self._daily_llm_count = 0
            self._daily_llm_count_date = today
        if self._daily_llm_count >= self._daily_llm_max:
            # 2026-04-23 수정: 한도 소진 시 경고 로그 승격 + 통과 유지
            # 기존엔 debug 로그라 LLM 보호 상실 인지 지연 → warning으로 즉시 알림
            # 상위 호출자(engine.py:1389)가 변동성 큰 날 본 로그를 보고 수동 조치 가능
            logger.warning(
                f"[크로스검증] LLM 이중검증 일일 한도 소진 "
                f"({self._daily_llm_max}회) → fail-open 통과. "
                f"고변동 날엔 수동 모니터링 필요."
            )
            return True
        self._daily_llm_count += 1
        logger.info(
            f"[크로스검증] LLM 이중검증 #{self._daily_llm_count}/{self._daily_llm_max}: "
            f"{symbol} 점수={score:.0f} 체제={market_regime}"
        )

        try:
            import asyncio
            # 거래 메모리 컨텍스트 (최근 유사 패턴)
            mem_context = ""
            if self._trade_memory and hasattr(self._trade_memory, 'get_context_for_signal'):
                mem_context = self._trade_memory.get_context_for_signal(strategy, sector)

            # 위키 컨텍스트 (전략/섹터/체제별 축적 교훈)
            wiki_context = ""
            if self._trade_wiki:
                wiki_context = self._trade_wiki.query(strategy, sector, market_regime)

            prompt = (
                f"종목 {symbol}, 전략 {strategy}, 점수 {score:.0f}.\n"
                f"시장 체제: {market_regime}."
                + (f" 섹터: {sector}." if sector else "") + "\n"
                f"지표: RSI={indicators.get('rsi_14', 'N/A')}, "
                f"ATR={indicators.get('atr_14', 'N/A')}%, "
                f"MA200거리={indicators.get('ma200_distance_pct', 'N/A')}%, "
                f"PER={indicators.get('per', 'N/A')}, "
                f"수급={'+' if (indicators.get('foreign_net_buy') if indicators.get('foreign_net_buy') is not None else 0) > 0 else '-'}.\n"
                + (f"최근 유사 거래 기억: {mem_context}\n" if mem_context else "")
                + (f"위키 교훈: {wiki_context}\n" if wiki_context else "")
                + "\n이 매수 시그널을 승인하시겠습니까? "
                "YES 또는 NO로 답하고, 한 줄 사유를 적어주세요."
            )
            # GPT-5.4 (STRATEGY_ANALYSIS) — 추론 필요한 매수 판단
            from ..utils.llm import LLMTask
            resp = await asyncio.wait_for(
                self._llm_manager.complete(prompt, task=LLMTask.STRATEGY_ANALYSIS, max_tokens=100),
                timeout=10.0,
            )
            if not resp.success:
                logger.debug(f"[크로스검증] LLM 응답 실패 (통과): {resp.error}")
                return True  # fail-open
            content = (resp.content or "").strip()
            if content and "NO" in content.upper()[:10]:
                logger.info(f"[크로스검증] LLM 거부: {symbol} — {content[:80]}")
                return False
            return True
        except Exception as e:
            logger.debug(f"[크로스검증] LLM 검증 실패 (통과): {e}")
            return True  # fail-open

    def get_stats(self) -> Dict:
        """오늘 검증 통계"""
        return dict(self._stats)
