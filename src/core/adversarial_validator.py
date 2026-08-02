"""
적대적 검증 — 매수 신호에 반대 논거를 강제로 생성시키고, 서로 다른 모델의 합의를 본다.

기존 llm_second_check의 프롬프트는 "이 매수 시그널을 승인하시겠습니까?"였다.
이 질문은 승인을 기본값으로 깔고 시작하므로 확증 편향을 유도한다.
게다가 단일 모델의 판단이라, 그 모델이 틀리면 걸러낼 방법이 없다.

여기서는 두 가지를 바꾼다:

1) **역할 분리 (Bull / Bear)**
   - Bull: 이 매수를 지지하는 근거를 찾는다
   - Bear: 이 매수가 실패할 시나리오를 찾는다 — "문제 없음"이라고 답하는 것을 금지한다
   Bear에게 반증 책임을 지우면, 승인 쪽으로 기울던 판단이 균형을 찾는다.

2) **서로 다른 provider (교차 검증)**
   Bull은 OpenAI, Bear는 Gemini로 돌린다. 두 모델이 같은 결론이면 신뢰도가 높고,
   갈리면 그 자체가 "불확실한 신호"라는 정보다.

판정:
    둘 다 승인            → 통과 (confidence 1.0)
    둘 다 거부            → 차단 (confidence 1.0)
    불일치                → STRICT 모드면 차단, 아니면 통과하되 confidence 0.5로 표시
                            (호출측이 점수를 감점하는 데 쓸 수 있다)

fail-open: LLM 장애·타임아웃·예산 소진 시에는 통과시킨다.
결정론적 규칙 9개가 이미 앞단에서 걸러내므로, LLM 장애로 매매가 멈추는 쪽이 더 해롭다.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from loguru import logger

# 불일치 시 차단할지 여부 (True면 보수적)
STRICT_ON_DISAGREEMENT = False

# 개별 LLM 호출 타임아웃 (초) — 실시간 매매 경로이므로 짧게
LLM_TIMEOUT = 12.0

# 응답 토큰 상한.
# ⚠️ gpt-5 계열은 추론 모델이라 이 값이 작으면 추론 토큰만 쓰고 본문이 빈 문자열로 온다
#    (success=True인데 content='' — 실측: 120이면 빈 응답, 400이면 정상).
#    줄이지 말 것.
MAX_TOKENS = 400

# 재현성 — gpt-5는 temperature 커스텀이 막혀 있어 seed로만 판정을 고정할 수 있다.
# (실측: seed 없이 6회 중 1회 반전, seed 고정 시 6회 일치)
VERDICT_SEED = 20260803

BULL_SYSTEM = (
    "당신은 한국 주식 트레이딩 심사역이다. "
    "주어진 매수 후보의 **지지 근거**를 냉정하게 평가한다. "
    "근거가 부실하면 주저 없이 반대한다. 답변은 한국어로 간결하게."
)

BEAR_SYSTEM = (
    "당신은 리스크 담당자다. 당신의 임무는 이 매수가 **실패할 이유를 찾아내는 것**이다. "
    "'문제 없음' 또는 '리스크가 없다'는 답변은 허용되지 않는다. "
    "반드시 가장 그럴듯한 실패 시나리오를 최소 하나 제시하라. "
    "그 시나리오가 치명적(손실 -5% 이상 가능)이면 REJECT, "
    "감수할 만한 수준이면 ACCEPT로 판정하라. 답변은 한국어로 간결하게."
)


@dataclass
class AdversarialResult:
    """적대적 검증 결과"""
    approved: bool
    confidence: float               # 1.0=만장일치, 0.5=불일치, 0.0=검증 실패
    reason: str = ""
    bull_verdict: Optional[bool] = None
    bear_verdict: Optional[bool] = None
    bull_text: str = ""
    bear_text: str = ""
    disagreed: bool = False
    failed: bool = False            # LLM 장애로 검증 못 함 (fail-open 통과)
    meta: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "approved": self.approved,
            "confidence": self.confidence,
            "reason": self.reason,
            "bull": self.bull_verdict,
            "bear": self.bear_verdict,
            "disagreed": self.disagreed,
            "failed": self.failed,
        }


def _parse_verdict(text: str, positive_token: str, negative_token: str) -> Optional[bool]:
    """
    응답에서 판정을 추출한다.

    앞부분 우선으로 보되, 토큰이 뒤에 나오는 경우도 처리한다.
    둘 다 없으면 None (판정 불가).
    """
    if not text:
        return None
    upper = text.upper()

    pos_at = upper.find(positive_token)
    neg_at = upper.find(negative_token)

    if pos_at == -1 and neg_at == -1:
        return None
    if pos_at == -1:
        return False
    if neg_at == -1:
        return True
    # 먼저 등장한 토큰을 판정으로 본다
    return pos_at < neg_at


class AdversarialValidator:
    """Bull/Bear 교차 검증기"""

    def __init__(self, llm_manager=None, strict_on_disagreement: bool = STRICT_ON_DISAGREEMENT):
        self._llm = llm_manager
        self.strict = strict_on_disagreement
        self.stats = {"total": 0, "approved": 0, "blocked": 0,
                      "disagreed": 0, "failed": 0}

    def set_llm_manager(self, llm_manager) -> None:
        self._llm = llm_manager

    # ── 프롬프트 ───────────────────────────────────────────
    def _build_context(self, symbol: str, strategy: str, score: float,
                       indicators: dict, market_regime: str, sector: str,
                       extra_context: str = "") -> str:
        ind = indicators or {}
        foreign = ind.get("foreign_net_buy")
        foreign_sign = "+" if (foreign if foreign is not None else 0) > 0 else "-"
        return (
            f"종목 {symbol}, 전략 {strategy}, 전략점수 {score:.0f}/100.\n"
            f"시장 체제: {market_regime}."
            + (f" 섹터: {sector}." if sector else "") + "\n"
            f"지표: RSI={ind.get('rsi_14', 'N/A')}, "
            f"ATR={ind.get('atr_14', 'N/A')}%, "
            f"MA200거리={ind.get('ma200_distance_pct', 'N/A')}%, "
            f"PER={ind.get('per', 'N/A')}, "
            f"수급={foreign_sign}.\n"
            + (f"{extra_context}\n" if extra_context else "")
        )

    async def _ask_bull(self, context: str):
        from ..utils.llm import LLMProvider
        prompt = (
            context
            + "\n이 매수 후보의 지지 근거를 평가하라. "
            "매수할 만하면 APPROVE, 아니면 REJECT로 시작하고 한 줄 사유를 적어라."
        )
        return await self._llm.complete_with(
            prompt, provider=LLMProvider.OPENAI, weight="light",
            system=BULL_SYSTEM, max_tokens=MAX_TOKENS,
            # 판정만 필요하므로 추론을 최소화한다. 토큰을 덜 쓸수록 빈 응답 여지도 준다.
            reasoning_effort="minimal",
            retry_on_empty=1,   # 200 + 빈 content가 실측으로 확인됨 (6회 중 1회)
            seed=VERDICT_SEED,
        )

    async def _ask_bear(self, context: str):
        from ..utils.llm import LLMProvider
        prompt = (
            context
            + "\n이 매수가 실패할 가장 그럴듯한 시나리오를 하나 제시하라. "
            "그 시나리오가 치명적이면 REJECT, 감수 가능하면 ACCEPT로 시작하고 "
            "시나리오를 한 줄로 적어라."
        )
        return await self._llm.complete_with(
            prompt, provider=LLMProvider.GEMINI, weight="light",
            system=BEAR_SYSTEM, max_tokens=MAX_TOKENS,
            retry_on_empty=1,
            temperature=0.0,
        )

    # ── 검증 ───────────────────────────────────────────────
    async def validate(
        self,
        symbol: str,
        strategy: str,
        score: float,
        indicators: dict,
        market_regime: str,
        sector: str = "",
        extra_context: str = "",
    ) -> AdversarialResult:
        """
        Bull/Bear 교차 검증을 수행한다.

        Returns:
            AdversarialResult — approved=False면 매수를 차단해야 한다.
        """
        self.stats["total"] += 1

        if not self._llm:
            self.stats["failed"] += 1
            return AdversarialResult(True, 0.0, "LLM 매니저 없음 — 검증 생략", failed=True)

        context = self._build_context(symbol, strategy, score, indicators,
                                      market_regime, sector, extra_context)

        try:
            bull_resp, bear_resp = await asyncio.wait_for(
                asyncio.gather(
                    self._ask_bull(context),
                    self._ask_bear(context),
                    return_exceptions=True,
                ),
                timeout=LLM_TIMEOUT,
            )
        except asyncio.TimeoutError:
            self.stats["failed"] += 1
            logger.warning(f"[적대검증] {symbol} 타임아웃 ({LLM_TIMEOUT}s) — fail-open 통과")
            return AdversarialResult(True, 0.0, "타임아웃 — fail-open", failed=True)
        except Exception as e:
            self.stats["failed"] += 1
            logger.warning(f"[적대검증] {symbol} 예외 — fail-open 통과: {e}")
            return AdversarialResult(True, 0.0, f"예외 ({e}) — fail-open", failed=True)

        bull_text = ""
        bear_text = ""
        if not isinstance(bull_resp, Exception) and getattr(bull_resp, "success", False):
            bull_text = (bull_resp.content or "").strip()
            if not bull_text:
                # 성공 응답인데 본문이 비었다 = 추론 토큰 소진 (MAX_TOKENS 주석 참조)
                logger.warning(f"[적대검증] {symbol} Bull 빈 응답 — MAX_TOKENS({MAX_TOKENS}) 확인 필요")
        if not isinstance(bear_resp, Exception) and getattr(bear_resp, "success", False):
            bear_text = (bear_resp.content or "").strip()
            if not bear_text:
                logger.warning(f"[적대검증] {symbol} Bear 빈 응답 — MAX_TOKENS({MAX_TOKENS}) 확인 필요")

        bull_verdict = _parse_verdict(bull_text, "APPROVE", "REJECT")
        bear_verdict = _parse_verdict(bear_text, "ACCEPT", "REJECT")

        # 둘 다 판정 불가 → 검증 실패 (fail-open)
        if bull_verdict is None and bear_verdict is None:
            self.stats["failed"] += 1
            logger.warning(f"[적대검증] {symbol} 양쪽 판정 불가 — fail-open 통과")
            return AdversarialResult(
                True, 0.0, "양쪽 판정 불가 — fail-open",
                bull_text=bull_text[:120], bear_text=bear_text[:120], failed=True,
            )

        # 한쪽만 응답 → 그 판정을 따르되 confidence를 낮춘다
        if bull_verdict is None or bear_verdict is None:
            single = bull_verdict if bull_verdict is not None else bear_verdict
            side = "Bull" if bull_verdict is not None else "Bear"
            result = AdversarialResult(
                approved=bool(single),
                confidence=0.5,
                reason=f"{side} 단독 판정 ({'승인' if single else '거부'})",
                bull_verdict=bull_verdict, bear_verdict=bear_verdict,
                bull_text=bull_text[:120], bear_text=bear_text[:120],
            )
            self.stats["approved" if result.approved else "blocked"] += 1
            logger.info(f"[적대검증] {symbol} {result.reason} (편측 응답)")
            return result

        # 양측 판정 확보
        if bull_verdict and bear_verdict:
            self.stats["approved"] += 1
            logger.info(f"[적대검증] {symbol} 만장일치 승인 (Bull✓ Bear✓)")
            return AdversarialResult(
                True, 1.0, "만장일치 승인",
                bull_verdict=True, bear_verdict=True,
                bull_text=bull_text[:120], bear_text=bear_text[:120],
            )

        if not bull_verdict and not bear_verdict:
            self.stats["blocked"] += 1
            reason = f"만장일치 거부 — {bear_text[:80]}"
            logger.info(f"[적대검증] {symbol} 만장일치 거부: {bear_text[:80]}")
            return AdversarialResult(
                False, 1.0, reason,
                bull_verdict=False, bear_verdict=False,
                bull_text=bull_text[:120], bear_text=bear_text[:120],
            )

        # 불일치
        self.stats["disagreed"] += 1
        approved = not self.strict
        reason = (
            f"모델 불일치 (Bull={'승인' if bull_verdict else '거부'}, "
            f"Bear={'승인' if bear_verdict else '거부'}) — "
            f"{'차단(strict)' if self.strict else '통과하되 신뢰도 하향'}"
        )
        if approved:
            self.stats["approved"] += 1
        else:
            self.stats["blocked"] += 1
        logger.info(f"[적대검증] {symbol} {reason} | Bear: {bear_text[:60]}")
        return AdversarialResult(
            approved, 0.5, reason,
            bull_verdict=bull_verdict, bear_verdict=bear_verdict,
            bull_text=bull_text[:120], bear_text=bear_text[:120],
            disagreed=True,
        )

    def get_stats(self) -> Dict[str, Any]:
        s = dict(self.stats)
        total = s.get("total", 0)
        if total:
            s["agreement_rate"] = round(
                (total - s.get("disagreed", 0) - s.get("failed", 0)) / total * 100, 1
            )
        return s


_validator: Optional[AdversarialValidator] = None


def get_adversarial_validator(llm_manager=None) -> AdversarialValidator:
    global _validator
    if _validator is None:
        _validator = AdversarialValidator(llm_manager=llm_manager)
    elif llm_manager is not None and _validator._llm is None:
        _validator.set_llm_manager(llm_manager)
    return _validator
