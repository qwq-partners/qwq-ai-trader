"""
AI Trading Bot v2 - LLM 전략가 (LLM Strategist)

LLM을 활용하여 거래 복기 결과를 분석하고 전략 개선안을 도출합니다.
"""

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, date, timedelta
from typing import Dict, List, Optional, Any
from loguru import logger

from .trade_journal import TradeJournal, get_trade_journal
from .trade_reviewer import TradeReviewer, ReviewResult, get_trade_reviewer
from ...utils.llm import LLMManager, LLMTask, get_llm_manager


@dataclass
class ParameterAdjustment:
    """파라미터 조정 제안"""
    parameter: str           # 파라미터 이름
    current_value: Any       # 현재 값
    suggested_value: Any     # 제안 값
    reason: str              # 변경 이유
    confidence: float        # 신뢰도 (0~1)
    expected_impact: str     # 예상 영향


@dataclass
class StrategyAdvice:
    """전략 조언"""
    # 분석 기간
    analysis_date: datetime
    period_days: int

    # 전체 평가
    overall_assessment: str      # 전반적 평가 (good/fair/poor)
    confidence_score: float      # 분석 신뢰도 (0~1)

    # 핵심 인사이트
    key_insights: List[str] = field(default_factory=list)

    # 파라미터 조정 제안
    parameter_adjustments: List[ParameterAdjustment] = field(default_factory=list)

    # 전략별 권고
    strategy_recommendations: Dict[str, str] = field(default_factory=dict)

    # 새로운 규칙 제안
    new_rules: List[Dict] = field(default_factory=list)

    # 피해야 할 상황
    avoid_situations: List[str] = field(default_factory=list)

    # 집중해야 할 기회
    focus_opportunities: List[str] = field(default_factory=list)

    # 다음 주 전망
    next_week_outlook: str = ""

    # 원본 LLM 응답
    raw_response: str = ""

    def to_dict(self) -> Dict:
        """딕셔너리로 변환"""
        return {
            "analysis_date": self.analysis_date.isoformat(),
            "period_days": self.period_days,
            "overall_assessment": self.overall_assessment,
            "confidence_score": self.confidence_score,
            "key_insights": self.key_insights,
            "parameter_adjustments": [
                {
                    "parameter": p.parameter,
                    "current_value": p.current_value,
                    "suggested_value": p.suggested_value,
                    "reason": p.reason,
                    "confidence": p.confidence,
                    "expected_impact": p.expected_impact,
                }
                for p in self.parameter_adjustments
            ],
            "strategy_recommendations": self.strategy_recommendations,
            "new_rules": self.new_rules,
            "avoid_situations": self.avoid_situations,
            "focus_opportunities": self.focus_opportunities,
            "next_week_outlook": self.next_week_outlook,
        }


def _count_trading_days(start: date, end: date) -> int:
    """실제 영업일 수 계산 (주말 + 공휴일 제외)"""
    try:
        from ..engine import is_kr_market_holiday
        count = 0
        d = start
        while d <= end:
            if d.weekday() < 5 and not is_kr_market_holiday(d):
                count += 1
            d += timedelta(days=1)
        return max(count, 1)
    except ImportError:
        # fallback: 주말만 제외
        period_days = (end - start).days or 1
        return max(period_days * 5 // 7, 1)


class LLMStrategist:
    """
    LLM 전략가

    거래 복기 결과를 LLM에 제공하고:
    1. 성과 분석 및 평가
    2. 전략 파라미터 최적화 제안
    3. 새로운 규칙 제안
    4. 피해야 할 상황 식별
    """

    # 시스템 프롬프트
    SYSTEM_PROMPT = """당신은 한국 주식 전략의 증거를 검토하는 분석가입니다.
목표는 위험·자본 한도 안에서 비용 후 기대수익과 자본 효율을 개선하는 것입니다.
고정 일수익이나 거래 횟수 할당량을 만들지 말고, 거래하지 않는 선택도 평가하세요.

분석 원칙:
1. 어떤 종목을 고르는가와 어느 시점·가격에 진입하는가를 분리하세요.
2. 거래별 수익률의 합계/평균은 계좌 수익률이 아닙니다. NAV·입출금·비용 근거가
   없으면 계좌 일수익/TWR/벤치마크 초과수익은 미측정이며 추정으로 채우지 마세요.
3. 수수료·세금·슬리피지·API 비용과 LLM 응답 이후 실제 진입 가능 가격을 반영하세요.
   잘못 거절한 기회, 회피한 손실, 현금 보유의 기회비용도 함께 비교하세요.
4. 엔진 버전·설정·시장 국면별로 구분하고, 전체 KODEX200과 사전 정의된 대칭적
   반도체 제외 비교를 자료가 있을 때만 병기하세요. 사후 승자 제외는 금지합니다.
5. 승률이나 손익비만으로 손절 축소/거래 확대를 정당화하지 마세요. 동일 진입의
   청산 대조 등 원인별 비교가 필요합니다. 과거 패턴은 인과관계의 증거가 아닙니다.
6. 근거 부족 시 기권하고 누락 자료를 명시하세요. 검증 가능한 변경 가설은 최대 하나로
   제한하고 관측 밖 검증·중단 조건을 쓰세요. 모델 신뢰도는 적용 승인이나 성공 확률이 아닙니다.

JSON으로 응답하세요. good/fair/poor는 관측된 거래 자료에 대한 정성 평가이며
계좌 성과 인증이 아닙니다. 증거 부족이면 fair와 낮은 confidence_score를 사용하고
parameter_adjustments/new_rules는 비워 두세요. 가설의 비용 후 효과를 확인하기 전에는
확정 수익 개선치를 제시하지 마세요."""

    def __init__(
        self,
        llm_manager: LLMManager = None,
        reviewer: TradeReviewer = None,
    ):
        self.llm = llm_manager or get_llm_manager()
        self.reviewer = reviewer or get_trade_reviewer()

        # 현재 전략 파라미터 (외부에서 설정)
        self._current_params: Dict[str, Dict] = {}

    def set_current_params(self, strategy_name: str, params: Dict):
        """현재 전략 파라미터 설정"""
        self._current_params[strategy_name] = params

    async def analyze_and_advise(
        self,
        days: int = 7,
        include_parameter_suggestions: bool = True,
        extra_context: str = "",
    ) -> StrategyAdvice:
        """
        거래 분석 및 조언 생성

        1. 복기 시스템으로 데이터 분석
        2. LLM에 분석 결과 전달
        3. 전략 개선안 수신 및 파싱
        """
        logger.info(f"[LLM 전략가] 최근 {days}일 거래 분석 시작")

        # 1. 복기 실행
        review = self.reviewer.review_period(days)

        if review.total_trades == 0:
            logger.warning("[LLM 전략가] 분석할 거래 없음")
            return StrategyAdvice(
                analysis_date=datetime.now(),
                period_days=days,
                overall_assessment="no_data",
                confidence_score=0,
                key_insights=["분석할 거래 데이터가 없습니다."],
            )

        # 2. 매크로 컨텍스트 수집 (환율/금리)
        market_context = None
        try:
            from ...signals.strategic.data_collector import StrategicDataCollector
            collector = StrategicDataCollector()  # FDR만 사용하므로 의존성 주입 불필요
            market_context = await collector.collect_macro_context()
        except Exception as e:
            logger.debug(f"[LLM 전략가] 매크로 컨텍스트 수집 실패: {e}")

        # 3. LLM 프롬프트 구성
        prompt = self._build_analysis_prompt(review, include_parameter_suggestions, market_context,
                                             extra_context=extra_context)

        # 4. LLM 호출
        try:
            llm_response = await self.llm.complete(
                prompt,
                budget_priority="research",
                task=LLMTask.STRATEGY_ANALYSIS,
                system=self.SYSTEM_PROMPT,
            )

            if not llm_response.success or not llm_response.content:
                raise ValueError(llm_response.error or "LLM 응답 없음")

            # 5. 응답 파싱
            advice = self._parse_llm_response(llm_response.content, days)

            logger.info(
                f"[LLM 전략가] 분석 완료: 평가={advice.overall_assessment}, "
                f"인사이트 {len(advice.key_insights)}개, "
                f"파라미터 조정 {len(advice.parameter_adjustments)}개"
            )

            return advice

        except Exception as e:
            logger.error(f"[LLM 전략가] 분석 실패: {e}")

            # 폴백: 기본 분석 결과 반환
            return self._create_fallback_advice(review, days)

    def _build_analysis_prompt(
        self,
        review: ReviewResult,
        include_params: bool,
        market_context: Optional[Dict] = None,
        extra_context: str = "",
    ) -> str:
        """LLM 분석 프롬프트 구성"""
        # ReviewResult에는 계좌 NAV/입출금/비용 완결성이 없다.
        period_days = (review.period_end - review.period_start).days or 1
        trading_days = _count_trading_days(review.period_start.date(), review.period_end.date())

        prompt_parts = [
            "# 거래 복기 분석 요청",
            "",
            "## 목적: 위험 한도 안에서 비용 후 기대수익 개선",
            f"- 분석 기간: {period_days}일 (영업일 {trading_days}일)",
            f"- 기록된 거래 수: {review.total_trades}건",
            f"- 기록된 거래 손익 합계: {review.total_pnl:+,.0f}원 (비용 반영 완결성 미확인)",
            f"- 거래별 수익률 단순 평균: {review.avg_pnl_pct:+.2f}% (계좌 수익률 아님)",
            "- 계좌 일수익률/TWR/벤치마크 초과수익: 미측정",
            "- NAV·입출금·보유 평가손익·모든 비용·동일 기간 벤치마크 근거가 필요합니다.",
            "",
            "## 과거 거래 요약 (관측 기술이며 인과관계/적용 근거가 아님)",
            review.summary_for_llm,
            "",
        ]

        # 현재 파라미터 정보
        if include_params and self._current_params:
            prompt_parts.extend([
                "## 현재 전략 파라미터",
                "",
            ])
            for strategy, params in self._current_params.items():
                prompt_parts.append(f"### {strategy}")
                for key, value in params.items():
                    prompt_parts.append(f"- {key}: {value}")
                prompt_parts.append("")

        # 전략별 성과
        if review.strategy_performance:
            prompt_parts.extend([
                "## 전략별 거래 표본 (비용 완결성 미확인)",
                "",
            ])
            for strategy, perf in review.strategy_performance.items():
                prompt_parts.append(
                    f"- {strategy}: 거래 {perf['trades']}회, "
                    f"승률 {perf.get('win_rate', 0):.1f}%, "
                    f"거래 수익률 평균 {perf.get('avg_pnl_pct', 0):+.2f}%"
                )
            prompt_parts.append("")

        # 시간대 분석
        if review.best_entry_hours or review.worst_entry_hours:
            prompt_parts.extend([
                "## 관측 표본의 진입 시간대 (최적 시각의 인과 증거 아님)",
                f"- 성과 상위 시간: {review.best_entry_hours}",
                f"- 성과 하위 시간: {review.worst_entry_hours}",
                "",
            ])

        # 시장 매크로 컨텍스트
        if market_context:
            prompt_parts.extend(["## 시장 매크로 컨텍스트", ""])
            exchange = market_context.get("exchange_rate")
            if exchange:
                prompt_parts.append(
                    f"- USD/KRW: {exchange.get('current', '?')}원 "
                    f"(1개월 {exchange.get('change_1m_pct', 0):+.1f}%)"
                )
            rates = market_context.get("interest_rates")
            if rates:
                kr = rates.get("KR_3Y")
                us = rates.get("US_10Y")
                if kr:
                    prompt_parts.append(f"- 한국 국채3년: {kr['current']:.2f}%")
                if us:
                    prompt_parts.append(f"- 미국 국채10년: {us['current']:.2f}%")
                spread = rates.get("spread_kr_us")
                if spread is not None:
                    prompt_parts.append(f"- 한미 스프레드: {spread:+.2f}%p")
            prompt_parts.append("")

        # 분석 요청
        prompt_parts.extend([
            "## 분석 요청",
            "",
            "자료 충분성부터 평가하고 종목 선택·진입·청산·비용의 원인을 구분하세요.",
            "비교 근거가 없으면 기권하고 필요한 추가 자료와 검증 방법만 제시하세요.",
            "",
            "다음을 JSON 형식으로 응답해주세요:",
            "",
            "```json",
            "{",
            '  "overall_assessment": "good/fair/poor 중 하나 (증거 부족은 fair)",',
            '  "confidence_score": 0.0~1.0,',
            '  "key_insights": ["인사이트1", "인사이트2", ...],',
            '  "parameter_adjustments": [',
            '    {',
            '      "parameter": "전략명.파라미터명",',
            '      "current_value": 현재값,',
            '      "suggested_value": 제안값,',
            '      "reason": "대조 근거·자료 공백·관측 밖 검증 및 중단 조건",',
            '      "confidence": 0.0~1.0,',
            '      "expected_impact": "비용 후 검증할 효과 (미측정은 미측정)"',
            '    }',
            '  ],',
            '  "strategy_recommendations": {',
            '    "전략명": "자료에 근거한 검증 권고 또는 기권"',
            '  },',
            '  "new_rules": [',
            '    {"condition": "조건", "action": "행동", "reason": "이유"}',
            '  ],',
            '  "avoid_situations": ["상황1", "상황2", ...],',
            '  "focus_opportunities": ["기회1", "기회2", ...],',
            '  "next_week_outlook": "버전을 고정한 다음 검증과 필요한 자료"',
            "}",
            "```",
        ])

        # 2026-09-13 WikiSkill 정렬 — 검증된 교훈(lesson_store)과 기각·롤백 이력은 제안자에게만 제공
        try:
            from .lesson_store import get_lesson_store
            _lessons = get_lesson_store().format_context(max_items=5)
            if _lessons:
                prompt_parts.extend(["## 검증된 교훈 (lesson_store)", _lessons, ""])
        except Exception:
            pass
        if extra_context:
            prompt_parts.extend([extra_context, ""])

        return "\n".join(prompt_parts)

    def _parse_llm_response(self, response: str, days: int) -> StrategyAdvice:
        """LLM 응답 파싱"""
        # JSON 추출
        json_start = response.find("{")
        json_end = response.rfind("}") + 1

        if json_start == -1 or json_end == 0:
            raise ValueError("JSON 형식 응답 없음")

        json_str = response[json_start:json_end]
        try:
            data = json.loads(json_str)
        except json.JSONDecodeError as e:
            raise ValueError(f"LLM 응답 JSON 파싱 실패: {e}")

        # ParameterAdjustment 변환
        param_adjustments = []
        for p in data.get("parameter_adjustments", []):
            param_adjustments.append(ParameterAdjustment(
                parameter=p.get("parameter", ""),
                current_value=p.get("current_value"),
                suggested_value=p.get("suggested_value"),
                reason=p.get("reason", ""),
                confidence=float(p.get("confidence", 0.5)),
                expected_impact=p.get("expected_impact", ""),
            ))

        return StrategyAdvice(
            analysis_date=datetime.now(),
            period_days=days,
            overall_assessment=data.get("overall_assessment", "fair"),
            confidence_score=float(data.get("confidence_score", 0.5)),
            key_insights=data.get("key_insights", []),
            parameter_adjustments=param_adjustments,
            strategy_recommendations=data.get("strategy_recommendations", {}),
            new_rules=data.get("new_rules", []),
            avoid_situations=data.get("avoid_situations", []),
            focus_opportunities=data.get("focus_opportunities", []),
            next_week_outlook=data.get("next_week_outlook", ""),
            raw_response=response,
        )

    def _get_current_param(self, param_name: str, default: Any = None) -> Any:
        """현재 전략 파라미터에서 값 조회 (모든 전략에서 검색)"""
        for strategy_params in self._current_params.values():
            if param_name in strategy_params:
                return strategy_params[param_name]
        return default

    def _create_fallback_advice(self, review: ReviewResult, days: int) -> StrategyAdvice:
        """LLM 실패는 전략 변경 근거가 아니다. 관측 통계만 반환한다."""
        return StrategyAdvice(
            analysis_date=datetime.now(),
            period_days=days,
            overall_assessment="fair",  # 기존 스키마의 중립값, 성과 적합 판정 아님
            confidence_score=0.0,
            key_insights=[
                f"기록된 거래 {review.total_trades}건, 손익 합계 {review.total_pnl:+,.0f}원 "
                "(비용 반영 완결성 미확인)",
                f"거래별 수익률 단순 평균 {review.avg_pnl_pct:+.2f}%, "
                f"승률 {review.win_rate:.1f}%, profit factor {review.profit_factor:.2f}",
                "계좌 일수익률/TWR/벤치마크 초과수익: 미측정. NAV·입출금·비용 근거 필요.",
                "LLM 분석 실패: 현재 자료만으로 파라미터 변경 효과를 확인할 수 없음.",
            ],
            parameter_adjustments=[],
            strategy_recommendations={},
            new_rules=[],
            avoid_situations=[],
            focus_opportunities=[],
            next_week_outlook="분석 기권. 입력 완결성·호출 실패 원인을 점검하고 동일 자료로 재검증.",
            raw_response="",
        )

    async def get_realtime_advice(
        self,
        symbol: str,
        current_price: float,
        indicators: Dict[str, float],
        position: Dict = None,
    ) -> str:
        """
        실시간 매매 조언

        현재 상황에서 어떻게 해야 할지 LLM에게 물어봅니다.
        """
        prompt = f"""
# 실시간 매매 조언 요청

## 종목 정보
- 종목: {symbol}
- 현재가: {current_price:,.0f}원

## 기술적 지표
"""
        for key, value in indicators.items():
            prompt += f"- {key}: {value:.2f}\n"

        if position:
            prompt += f"""
## 현재 포지션
- 보유 수량: {position.get('quantity', 0)}주
- 평균 단가: {position.get('avg_price', 0):,.0f}원
- 현재 손익: {position.get('pnl_pct', 0):+.1f}%
"""

        prompt += """
## 질문
현재 상황에서 어떤 행동을 취해야 할까요?
간단하게 한 줄로 답해주세요. (매수/매도/관망/손절/익절 중 하나와 이유)
"""

        try:
            llm_response = await self.llm.complete(
                prompt,
                task=LLMTask.QUICK_ANALYSIS,
                max_tokens=100,
            )
            if llm_response.success and llm_response.content:
                return llm_response.content.strip()
            return "분석 불가"

        except Exception as e:
            logger.error(f"실시간 조언 실패: {e}")
            return "분석 불가"


# 싱글톤 인스턴스
_llm_strategist: Optional[LLMStrategist] = None


def get_llm_strategist() -> LLMStrategist:
    """LLMStrategist 인스턴스 반환"""
    global _llm_strategist
    if _llm_strategist is None:
        _llm_strategist = LLMStrategist()
    return _llm_strategist
