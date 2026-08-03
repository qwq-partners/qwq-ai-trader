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

from .reproducibility import get_ledger, snapshot_reports
from .types import AnalystReport, DebateResult, DebateTurn

# 라운드당 타임아웃 (초)
ROUND_TIMEOUT = 20.0

# gpt-5 계열은 추론 모델이라 이 값이 작으면 본문이 빈 문자열로 온다.
# (src/core/adversarial_validator.py MAX_TOKENS 주석 참조) 줄이지 말 것.
MAX_TOKENS = 400

# 추론 강도 — 2026-08-03 "low" → "minimal".
#   토론은 APPROVE/REJECT 판정과 두 문장 근거가 전부라 깊은 추론이 필요 없다.
#   실측(각 5회): low는 reasoning 128~320토큰을 소비하고 총 236~437토큰,
#   minimal은 reasoning 0에 총 112~156토큰. 응답 품질 차이는 없었고
#   토큰을 덜 쓸수록 본문이 잘려 빈 응답이 될 여지도 줄어든다.
REASONING_EFFORT = "minimal"

# 빈 응답 재시도 — gpt-5 계열은 200 응답에 content만 비어 오는 경우가 있다.
#   실측: LLMManager 경유 6회 중 1회 발생. 판정 호출에서 빈 응답은 곧 판단 불가이고,
#   그게 재현성 지표(동일 입력 일치율)를 직접 깎는다. 1회 재시도로 회복한다.
EMPTY_RETRY = 1

# 샘플링 시드 — 재현성의 핵심.
#   gpt-5 계열은 temperature 커스텀이 막혀 있어 seed 말고는 판정을 고정할 방법이 없다.
#   실측: seed 없이 동일 입력 6회 → 1회 반전(불일치), seed 고정 → 6회 전부 일치.
#   값 자체에 의미는 없다. 입력이 다르면 출력도 달라진다 — 같은 입력의 재현만 보장한다.
DEBATE_SEED = 20260803
# Gemini는 temperature로 결정성을 확보한다 (0.3에서도 안정적이었지만 0으로 고정)
DEBATE_TEMPERATURE = 0.0

BULL_SYSTEM = (
    "당신은 매수 측 리서처다. 주어진 종목을 매수해야 하는 근거를 제시한다. "
    "다만 억지로 옹호하지 않는다 — 근거가 부실하면 솔직히 인정하고 반대한다. "
    "결론은 반드시 APPROVE 또는 REJECT로 시작하고, 한국어로 2문장 이내."
)

# 2026-08-03 캘리브레이션: ACCEPT의 판정 기준을 명시했다.
#   기존 프롬프트는 "실패 경로 제시 필수 + 치명적이면 REJECT"만 있고 ACCEPT가
#   무엇인지 정의하지 않았다. 모델은 지시대로 생생한 실패 서사를 쓴 뒤 자기 서사에
#   설득돼 REJECT로 기울었다 — 실측 19/19 REJECT. 근거 최상 케이스(분석가 종합 52,
#   어닝서프라이즈+동반순매수+기술적 건전)에서도 REJECT였다.
#   Bear가 ACCEPT를 낼 수 없으면 만장일치 지지(+20) 경로와 PM 오버라이드
#   (conviction≥0.75)가 전부 죽은 조항이 되고, shadow 관측은 "매수 판정 품질"을
#   측정할 표본 자체를 얻지 못한다.
#   실패 시나리오 제시 의무는 그대로 둔다 — 그것이 Bear의 가치다. 바뀐 것은
#   "그 시나리오를 어떻게 판정하는가"의 기준뿐이다.
BEAR_SYSTEM = (
    "당신은 리스크 측 리서처다. 이 매수가 실패할 시나리오를 찾아내는 것이 임무다. "
    "'문제 없음'이나 '리스크가 없다'는 답변은 허용되지 않는다 — "
    "반드시 가장 그럴듯한 실패 경로를 제시하라. "
    "단, ACCEPT는 매수 추천이 아니다 — '실패 경로는 존재하지만 제시된 근거 대비 "
    "감수 가능한 수준'이라는 평가다. 판정 기준: 당신의 실패 시나리오가 구체적 촉매나 "
    "데이터 없이 일반론(차익 실현 가능성, 시장 변동성, 선반영 우려 등)에만 기대면 "
    "ACCEPT, 근거의 핵심을 직접 무너뜨리는 구체적 사실이 있으면 REJECT다. "
    "모든 건에 REJECT를 내는 것은 리스크 평가가 아니라 평가 회피다. "
    "REJECT 또는 ACCEPT로 시작하고, 한국어로 2문장 이내."
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
        # 재현성 원장 — 모든 토론 호출을 append-only로 남긴다
        self._ledger = get_ledger()

    def set_llm_manager(self, llm_manager) -> None:
        self._llm = llm_manager

    # ── 프롬프트 ───────────────────────────────────────────
    @staticmethod
    def _context(symbol: str, name: str, reports: List[AnalystReport],
                 market_context: str = "") -> str:
        """
        토론 프롬프트용 컨텍스트.

        각 근거의 **나이**를 함께 적는다. 30분 전 수급과 방금 계산한 지표를
        구분 없이 제시하면 모델이 모두 현재 정보로 취급한다.
        """
        lines = [f"종목: {name or symbol} ({symbol})"]
        for r in reports:
            if r.ok:
                age = r.age_minutes
                stamp = "실시간" if age < 1 else f"{age:.0f}분 전"
                lines.append(
                    f"- [{r.kind.value}] 점수 {r.score:+d} ({stamp}): {r.summary}"
                )
            else:
                lines.append(f"- [{r.kind.value}] 수집 실패 ({r.error})")
        if market_context:
            lines.append(f"시장 컨텍스트: {market_context}")
        lines.append(
            "※ 괄호 안은 근거 데이터의 나이다. 오래된 근거는 그만큼 할인해서 판단하라."
        )
        return "\n".join(lines)

    async def _ask(self, prompt: str, system: str, provider) -> Dict[str, Any]:
        """
        LLM 1회 호출.

        문자열만 반환하면 **실제 응답 모델 ID와 지연이 유실**된다.
        재현성 원장은 "무슨 모델이 답했는가"가 핵심이므로 메타를 함께 돌려준다
        (폴백으로 모델이 바뀌었을 수 있어 요청 모델과 응답 모델이 다를 수 있다).
        """
        resp = await self._llm.complete_with(
            prompt, provider=provider, weight="light",
            system=system, max_tokens=MAX_TOKENS,
            reasoning_effort=REASONING_EFFORT,
            retry_on_empty=EMPTY_RETRY,
            seed=DEBATE_SEED,
            temperature=DEBATE_TEMPERATURE,
        )
        ok = bool(getattr(resp, "success", False))
        return {
            "text": (resp.content or "").strip() if ok else "",
            "model": getattr(resp, "model", "") or "",
            "provider": getattr(getattr(resp, "provider", None), "value",
                                str(getattr(resp, "provider", ""))),
            "latency_ms": float(getattr(resp, "latency_ms", 0.0) or 0.0),
            "success": ok,
            "error": getattr(resp, "error", None),
        }

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
        # 입력 스냅샷 — 재실행 시 "같은 입력이었나"를 판정하는 기준.
        # 나이는 호출마다 변하므로 제외한다 (snapshot_reports 참조).
        input_snapshot = snapshot_reports(reports)
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
                # "해소됐다면"은 불가능한 문턱이다 — 리스크는 결코 완전히 해소되지 않는다.
                # R1과 같은 기준(치명적 vs 감수 가능)으로 재판정하게 한다.
                bear_prompt = (
                    f"{base}\n\n[매수 측 주장]\n{bull_text or '(응답 없음)'}\n\n"
                    "위 주장을 반영해 다시 판단하라. 당신의 리스크가 여전히 근거의 핵심을 "
                    "무너뜨리면 REJECT, 감수 가능한 수준이면 ACCEPT다. "
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

            # 이번 라운드의 **원시 응답**을 분리해서 다룬다.
            #   직전 라운드 텍스트를 그대로 이번 turn에 기록하면, 응답이 비었던 라운드가
            #   "같은 말을 반복한 것"처럼 감사 로그에 남아 토론 이력이 오염된다.
            bull_meta = bull_new if isinstance(bull_new, dict) else {}
            bear_meta = bear_new if isinstance(bear_new, dict) else {}
            round_bull = bull_meta.get("text") or ""
            round_bear = bear_meta.get("text") or ""

            round_bull_v = _parse(round_bull, "APPROVE", "REJECT") if round_bull else None
            round_bear_v = _parse(round_bear, "ACCEPT", "REJECT") if round_bear else None

            # ── 재현성 원장 기록 (append-only) ──
            # 판단 근거를 사후에 재현·비교할 수 있어야 shadow 성과를 신뢰할 수 있다.
            for _role, _meta, _p, _sys, _v in (
                ("bull", bull_meta, bull_prompt, BULL_SYSTEM, round_bull_v),
                ("bear", bear_meta, bear_prompt, BEAR_SYSTEM, round_bear_v),
            ):
                try:
                    self._ledger.record(
                        symbol=symbol, role=_role, round_no=rnd,
                        prompt=_p, system=_sys,
                        response=_meta.get("text", ""),
                        provider=_meta.get("provider", ""),
                        model=_meta.get("model", ""),
                        params={"max_tokens": MAX_TOKENS,
                                "reasoning_effort": "low", "weight": "light"},
                        verdict=_v,
                        input_snapshot=input_snapshot,
                        latency_ms=_meta.get("latency_ms", 0.0),
                        success=bool(_meta.get("success")),
                        error=_meta.get("error"),
                    )
                except Exception as _le:
                    logger.debug(f"[리서치팀] 원장 기록 실패: {_le}")

            # 이력에는 이번 라운드에 실제로 나온 것만 남긴다 (모델 ID 포함)
            result.turns.append(DebateTurn(
                rnd, "bull", round_bull_v, round_bull or "(응답 없음)",
                model=bull_meta.get("model", ""),
                provider=bull_meta.get("provider", "")))
            result.turns.append(DebateTurn(
                rnd, "bear", round_bear_v, round_bear or "(응답 없음)",
                model=bear_meta.get("model", ""),
                provider=bear_meta.get("provider", "")))
            result.rounds_run = rnd

            # 다음 라운드 프롬프트와 최종 판정에는 마지막으로 확보된 응답을 이어 쓴다
            # (한쪽이 한 라운드 실패했다고 그 관점을 통째로 버리지는 않는다)
            prev_bull, prev_bear = bull_v, bear_v
            if round_bull:
                bull_text, bull_v = round_bull, round_bull_v
            if round_bear:
                bear_text, bear_v = round_bear, round_bear_v

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
