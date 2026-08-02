"""
Research 팀 — Bull과 Bear가 2라운드로 토론한다 (생성-검증 패턴).

`src/core/adversarial_validator.py`는 Bull/Bear가 각자 한 번씩만 말한다.
서로의 논거를 보지 못하므로 진짜 토론이 아니라 독립 투표에 가깝다.

여기서는 라운드를 나눈다:
    R1: Bull과 Bear가 분석가 보고서를 보고 각자 주장 (동시)
    R2: 상대 주장을 읽고 반박 — 이때 입장을 바꿀 수 있다 (동시)

2라운드를 두는 이유는 단순히 한 번 더 묻기 위해서가 아니라,
**상대 논거를 반영한 뒤에도 입장이 유지되는지**를 보기 위해서다.
R1에서 갈렸다가 R2에서 한쪽이 설득되면 그 합의는 R1 만장일치보다 정보량이 많다.

모델을 갈라 쓴다 (Bull=OpenAI, Bear=Gemini). 같은 모델로 양쪽을 돌리면
같은 편향을 공유해 토론이 형식적으로 흐른다.

fail-open: LLM 장애 시 토론 실패로 표시하되 매매를 막지는 않는다.
결정론적 게이트(cross_validator 11규칙)가 뒤에 있으므로,
LLM 문제로 전체가 멈추는 쪽이 더 해롭다.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional

from loguru import logger

from .types import AnalystReport, DebateResult, DebateTurn

# 라운드당 타임아웃 (초)
ROUND_TIMEOUT = 20.0

# gpt-5 계열은 추론 모델이라 이 값이 작으면 본문이 빈 문자열로 온다.
# (src/core/adversarial_validator.py MAX_TOKENS 주석 참조) 줄이지 말 것.
MAX_TOKENS = 400

BULL_SYSTEM = (
    "당신은 매수 측 리서처다. 주어진 종목을 매수해야 하는 근거를 제시한다. "
    "다만 억지로 옹호하지 않는다 — 근거가 부실하면 솔직히 인정하고 반대한다. "
    "결론은 반드시 APPROVE 또는 REJECT로 시작하고, 한국어로 2문장 이내."
)

BEAR_SYSTEM = (
    "당신은 리스크 측 리서처다. 이 매수가 실패할 시나리오를 찾아내는 것이 임무다. "
    "'문제 없음'이나 '리스크가 없다'는 답변은 허용되지 않는다 — "
    "반드시 가장 그럴듯한 실패 경로를 제시하라. "
    "그 리스크가 치명적이면 REJECT, 감수할 만하면 ACCEPT로 시작하고, 한국어로 2문장 이내."
)


def _parse(text: str, positive: str, negative: str) -> Optional[bool]:
    """응답에서 판정 추출 — 먼저 등장한 토큰을 따른다"""
    if not text:
        return None
    u = text.upper()
    p, n = u.find(positive), u.find(negative)
    if p == -1 and n == -1:
        return None
    if p == -1:
        return False
    if n == -1:
        return True
    return p < n


class ResearchTeam:
    """Bull/Bear 다중 라운드 토론"""

    def __init__(self, llm_manager=None, rounds: int = 2):
        self._llm = llm_manager
        self.rounds = max(1, int(rounds))
        self.stats = {"total": 0, "consensus_buy": 0, "consensus_reject": 0,
                      "split": 0, "failed": 0, "flipped": 0, "one_sided": 0}

    def set_llm_manager(self, llm_manager) -> None:
        self._llm = llm_manager

    # ── 프롬프트 ───────────────────────────────────────────
    @staticmethod
    def _context(symbol: str, name: str, reports: List[AnalystReport],
                 market_context: str = "") -> str:
        lines = [f"종목: {name or symbol} ({symbol})"]
        for r in reports:
            if r.ok:
                lines.append(f"- [{r.kind.value}] 점수 {r.score:+d}: {r.summary}")
            else:
                lines.append(f"- [{r.kind.value}] 수집 실패 ({r.error})")
        if market_context:
            lines.append(f"시장 컨텍스트: {market_context}")
        return "\n".join(lines)

    async def _ask(self, prompt: str, system: str, provider) -> str:
        resp = await self._llm.complete_with(
            prompt, provider=provider, weight="light",
            system=system, max_tokens=MAX_TOKENS,
            reasoning_effort="low",
        )
        if not getattr(resp, "success", False):
            return ""
        return (resp.content or "").strip()

    # ── 토론 ───────────────────────────────────────────────
    async def debate(self, symbol: str, name: str,
                     reports: List[AnalystReport],
                     market_context: str = "") -> DebateResult:
        """
        Bull/Bear 토론을 진행한다.

        Returns:
            DebateResult — consensus가 True면 매수 지지, False면 반대,
                           None이면 의견이 갈린 것 (호출측이 보수적으로 처리)
        """
        self.stats["total"] += 1
        result = DebateResult(symbol=symbol)

        if self._llm is None:
            result.failed = True
            result.summary = "LLM 매니저 없음"
            self.stats["failed"] += 1
            return result

        from ..utils.llm import LLMProvider

        base = self._context(symbol, name, reports, market_context)
        bull_text = bear_text = ""
        bull_v = bear_v = None

        for rnd in range(1, self.rounds + 1):
            if rnd == 1:
                bull_prompt = (
                    f"{base}\n\n이 종목을 지금 매수할 만한가? "
                    "APPROVE 또는 REJECT로 시작하고 핵심 근거를 적어라."
                )
                bear_prompt = (
                    f"{base}\n\n이 매수가 실패할 가장 그럴듯한 시나리오는? "
                    "치명적이면 REJECT, 감수 가능하면 ACCEPT로 시작하라."
                )
            else:
                # 상대 논거를 보여주고 반박 기회를 준다 — 입장 변경 허용
                bull_prompt = (
                    f"{base}\n\n[리스크 측 주장]\n{bear_text or '(응답 없음)'}\n\n"
                    "위 반론을 반영해 다시 판단하라. 반론이 타당하면 입장을 바꿔도 된다. "
                    "APPROVE 또는 REJECT로 시작하라."
                )
                bear_prompt = (
                    f"{base}\n\n[매수 측 주장]\n{bull_text or '(응답 없음)'}\n\n"
                    "위 주장을 반영해 다시 판단하라. 리스크가 해소됐다면 ACCEPT로 바꿔도 된다. "
                    "ACCEPT 또는 REJECT로 시작하라."
                )

            try:
                bull_new, bear_new = await asyncio.wait_for(
                    asyncio.gather(
                        self._ask(bull_prompt, BULL_SYSTEM, LLMProvider.OPENAI),
                        self._ask(bear_prompt, BEAR_SYSTEM, LLMProvider.GEMINI),
                        return_exceptions=True,
                    ),
                    timeout=ROUND_TIMEOUT,
                )
            except asyncio.TimeoutError:
                logger.warning(f"[리서치팀] {symbol} R{rnd} 타임아웃")
                break
            except Exception as e:
                logger.warning(f"[리서치팀] {symbol} R{rnd} 예외: {e}")
                break

            if isinstance(bull_new, str) and bull_new:
                bull_text = bull_new
            if isinstance(bear_new, str) and bear_new:
                bear_text = bear_new

            prev_bull, prev_bear = bull_v, bear_v
            bull_v = _parse(bull_text, "APPROVE", "REJECT")
            bear_v = _parse(bear_text, "ACCEPT", "REJECT")

            result.turns.append(DebateTurn(rnd, "bull", bull_v, bull_text))
            result.turns.append(DebateTurn(rnd, "bear", bear_v, bear_text))
            result.rounds_run = rnd

            # 입장 변경 추적 — 토론이 실제로 작동했는지 보는 지표
            if rnd > 1 and (prev_bull != bull_v or prev_bear != bear_v):
                self.stats["flipped"] += 1
                logger.info(
                    f"[리서치팀] {symbol} R{rnd} 입장 변경: "
                    f"bull {prev_bull}→{bull_v}, bear {prev_bear}→{bear_v}"
                )

        result.bull_final = bull_v
        result.bear_final = bear_v

        # ── 판정 ──
        if bull_v is None and bear_v is None:
            result.failed = True
            result.confidence = 0.0
            result.summary = "양측 판정 불가"
            self.stats["failed"] += 1
        elif bull_v is None or bear_v is None:
            # 한쪽만 응답한 경우는 합의로 취급하지 않는다.
            #
            # Bear는 "실패 시나리오를 찾아라"는 역할을 부여받았으므로, Bull이 죽고
            # Bear만 남으면 구조적으로 반대 쪽에 서게 된다 — 토론이 아니라 편향이다.
            # 반대로 Bear의 ACCEPT는 "감수할 만한 리스크"라는 뜻이지 매수 추천이 아니다.
            # 그래서 단독 응답은 비대칭으로 처리한다:
            #   명시적 반대 → 존중 (안전 쪽으로 기운다)
            #   단독 긍정   → 보류 (합의로 승격시키지 않는다)
            single = bull_v if bull_v is not None else bear_v
            side = "Bull" if bull_v is not None else "Bear"
            if single is False:
                result.consensus = False
                result.confidence = 0.5
                verdict_txt = "반대"
            else:
                result.consensus = None
                result.confidence = 0.3
                verdict_txt = "지지(합의 미성립)"
            result.summary = f"{side} 단독 응답 — {verdict_txt} / 상대측 무응답"
            self.stats["one_sided"] += 1
        elif bull_v == bear_v:
            result.consensus = bull_v
            result.confidence = 1.0
            result.summary = (f"만장일치 {'지지' if bull_v else '반대'} "
                              f"— {(bear_text or '')[:100]}")
            self.stats["consensus_buy" if bull_v else "consensus_reject"] += 1
        else:
            result.consensus = None
            result.confidence = 0.5
            result.summary = (f"의견 분열 (Bull={'지지' if bull_v else '반대'}, "
                              f"Bear={'지지' if bear_v else '반대'})")
            self.stats["split"] += 1

        logger.info(
            f"[리서치팀] {symbol} {result.rounds_run}라운드 → {result.summary}"
        )
        return result

    def get_stats(self) -> Dict[str, Any]:
        s: Dict[str, Any] = dict(self.stats)
        t = s.get("total", 0)
        if t:
            s["consensus_rate"] = round(
                (s["consensus_buy"] + s["consensus_reject"]) / t * 100, 1
            )
            s["flip_rate"] = round(s["flipped"] / t * 100, 1)
        return s
