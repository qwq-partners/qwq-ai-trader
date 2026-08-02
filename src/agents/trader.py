"""
Trader 에이전트 — 분석가 보고서와 토론 결과를 종합해 매매를 제안한다 (감독자 패턴).

LLM을 쓰지 않는다. 이 단계에서 필요한 것은 새로운 통찰이 아니라
**앞 단계 결론들을 일관된 규칙으로 합치는 일**이기 때문이다.
같은 입력에 같은 출력이 나와야 백테스트·감사·회귀 테스트가 가능하다.
판단의 창의적인 부분은 이미 Bull/Bear 토론에서 LLM이 담당했다.

산출물은 제안(TradeProposal)일 뿐이며, 실행 권한은 없다.
리스크 게이트와 PM 승인을 거쳐야 주문이 나간다.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from loguru import logger

from .analysts import AnalystTeam
from .types import AnalystReport, DebateResult, Stance, TradeProposal

# 종합 점수 임계값 (-100 ~ +100)
BUY_THRESHOLD = 20        # 이상이면 매수 후보
SELL_THRESHOLD = -30      # 이하면 청산 검토

# 사이징 배수 한계 — 리스크 매니저의 기본 포지션 비율에 곱해진다
MIN_SIZE_MULT = 0.5
MAX_SIZE_MULT = 1.5


class TraderAgent:
    """분석·토론 결과 → 매매 제안"""

    name = "trader"

    def propose(
        self,
        symbol: str,
        name: str,
        reports: List[AnalystReport],
        debate: Optional[DebateResult],
        holding: bool = False,
        unrealized_pnl_pct: Optional[float] = None,
    ) -> TradeProposal:
        """
        Args:
            holding: 이미 보유 중인 종목인지 (보유면 HOLD/SELL, 아니면 BUY/HOLD)
            unrealized_pnl_pct: 보유 시 현재 평가손익률
        """
        analyst_score = AnalystTeam.aggregate_score(reports)
        scores = {r.kind.value: r.score for r in reports if r.ok}

        # ── 근거 충분성 검사 (fail-closed) ──────────────────────
        # 가중평균은 상대 비중만 보므로 "근거가 얼마나 남았는가"를 반영하지 못한다.
        # 모든 근거가 TTL을 넘겼는데도 종합 점수가 그대로 나오는 상황을 막는다.
        # 근거가 부족하면 신규 매수는 금지하고, 보유분은 현상 유지한다
        # (정보가 없다고 팔 이유는 없지만, 정보 없이 살 이유도 없다).
        evidence_ok, evidence_reason, evidence_w = AnalystTeam.evidence_quality(reports)

        # 토론 결과를 점수에 반영
        #   만장일치 지지  → +20 / 만장일치 반대 → -40 (반대에 더 큰 가중)
        #   의견 분열      → -10 (불확실성 자체를 비용으로 본다)
        debate_adj = 0
        conviction = 0.5
        debate_ok = debate is not None and not debate.failed
        if debate_ok and debate is not None:
            if debate.consensus is True:
                debate_adj = int(20 * debate.confidence)
                conviction = 0.5 + 0.4 * debate.confidence
            elif debate.consensus is False:
                debate_adj = int(-40 * debate.confidence)
                conviction = 0.2
            else:
                # 합의 미성립 (의견 분열 또는 단독 응답) — 불확실성 자체를 비용으로 본다
                debate_adj = -10
                conviction = 0.35
        else:
            # 토론 실패(LLM 장애 등) — 아래에서 신규 매수는 차단된다.
            # 보유 종목 판단에는 계속 쓰이므로 확신도만 낮춰 둔다.
            conviction = 0.3

        total = max(-100, min(100, analyst_score + debate_adj))

        # ── 방향 결정 ──
        if holding:
            if total <= SELL_THRESHOLD:
                stance = Stance.SELL
            else:
                stance = Stance.HOLD
            # 손실 중인데 팀 판단도 부정적이면 청산 쪽으로 기운다
            if (unrealized_pnl_pct is not None and unrealized_pnl_pct < -5.0
                    and total < 0):
                stance = Stance.SELL
        else:
            stance = Stance.BUY if total >= BUY_THRESHOLD else Stance.HOLD

        # ── 신규 매수 차단 조건 (fail-closed) ────────────────────
        # 팀 심의는 "검증 계층"으로 도입했다. 검증이 성립하지 않았는데 매수를 허용하면
        # 계층을 둔 의미가 없다. 두 경우 모두 BUY를 HOLD로 내린다.
        block_reason = ""
        if stance == Stance.BUY:
            if not evidence_ok:
                stance = Stance.HOLD
                block_reason = f"근거 부족 — {evidence_reason}"
            elif not debate_ok:
                # 토론 실패 = 반대 논거 검증이 아예 이뤄지지 않은 상태.
                # 예전에는 사이징만 줄여 통과시켰으나(fail-open), 그러면
                # "안전 검증 계층"이라는 도입 목적과 모순된다.
                stance = Stance.HOLD
                block_reason = "토론 검증 실패 — 반대 논거 미검증 상태로 매수 불가"

        if block_reason:
            logger.info(f"[트레이더] {name or symbol} 매수 보류: {block_reason}")

        # ── 사이징 ──
        # 종합 점수와 확신도를 함께 반영하되, 상·하한을 둬서 한 종목에 몰리지 않게 한다.
        size_mult = 1.0
        if stance == Stance.BUY:
            size_mult = 0.5 + (total / 100.0) * 0.7 + (conviction - 0.5) * 0.6
            size_mult = max(MIN_SIZE_MULT, min(MAX_SIZE_MULT, round(size_mult, 2)))

        rationale_parts = [f"분석가 종합 {analyst_score:+d}(근거 {evidence_w:.2f})"]
        if block_reason:
            rationale_parts.append(block_reason)
        if debate_ok and debate is not None:
            rationale_parts.append(f"토론 {debate.summary[:60]}")
        else:
            rationale_parts.append("토론 실패")
        if debate_adj:
            rationale_parts.append(f"토론 보정 {debate_adj:+d}")
        rationale_parts.append(f"최종 {total:+d}")

        proposal = TradeProposal(
            symbol=symbol,
            stance=stance,
            conviction=round(conviction, 2),
            size_multiplier=size_mult,
            rationale=" | ".join(rationale_parts),
            analyst_scores=scores,
            debate=debate.to_dict() if debate else None,
        )

        logger.info(
            f"[트레이더] {name or symbol} → {stance.value.upper()} "
            f"(종합 {total:+d}, 확신 {conviction:.2f}, 사이징 ×{size_mult})"
        )
        return proposal
