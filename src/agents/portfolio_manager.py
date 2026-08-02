"""
Portfolio Manager — 최종 승인/거부 (생성-검증 패턴의 마지막 게이트).

PM은 두 가지 일을 한다:
  1) 리스크 게이트를 통과한 제안을 한 번 더 거를 수 있다 (거부 권한)
  2) 제한된 조건에서 게이트 차단을 뒤집을 수 있다 (오버라이드 권한)

■ 오버라이드를 제한하는 이유

2026-08-02 게이트 성능 실측에서 **모든 게이트가 유효**했다.
차단된 신호의 20영업일 사후 수익률은 게이트별로 -3.7% ~ -13.2%였고,
특히 G2_cross는 -13.21%(회피성공 74%)로 가장 정확했다.
즉 게이트를 뚫는 행위는 통계적으로 손해 보는 쪽에 가깝다.

그럼에도 오버라이드를 여는 이유는, 게이트가 **개별 종목의 맥락을 모르기** 때문이다.
팀 전체가 만장일치로 강하게 지지하는 건은 게이트가 놓친 정보를 담고 있을 수 있다.

따라서 다음 조건을 **모두** 만족할 때만 허용한다:
  - 토론이 만장일치 지지 (confidence 1.0)
  - Trader 확신도가 임계값 이상
  - 차단 게이트가 오버라이드 가능 목록에 있음
  - 일일 오버라이드 한도 미소진

■ 절대 오버라이드 불가 (하드 가드)

    킬스위치 / 일일 손실 한도 / 현금 부족 / 예산 초과 / 중복 보유

이들은 계좌 생존과 직결된다. 팀이 아무리 확신해도 뚫을 수 없다.
"오늘은 확신이 있으니 손실 한도를 넘겨보자"가 계좌를 끝내는 전형적인 경로다.

모든 오버라이드는 감사 원장에 남고 텔레그램으로 알린다.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Dict, List, Optional, Set

from loguru import logger

from .types import PMDecision, Stance, TradeProposal

# ── 오버라이드 정책 ────────────────────────────────────────
# 절대 뚫을 수 없는 게이트 (계좌 생존 관련)
HARD_GATES: Set[str] = {
    "killswitch", "kill_switch",
    "G5_cash", "G5_budget",        # 현금·예산 부족
    "daily_loss", "G3_daily_loss", # 일일 손실 한도
    "duplicate", "already_holding", # 중복 보유
    "exit_exempt",                  # 자동매도 금지 종목
}

# 오버라이드 가능한 게이트 (맥락 판단 여지가 있는 것)
SOFT_GATES: Set[str] = {
    "G1_regime",      # 시장 레짐 — 종목이 레짐과 무관하게 강할 수 있다
    "G2_cross",       # 크로스 검증 감점
    "G4_llm",         # LLM 2차 검증
    "G_intraday",     # 진입 시간대 — 실측상 효과가 가장 불명확했다
}

# 오버라이드 최소 조건
MIN_CONVICTION = 0.75          # Trader 확신도
DAILY_OVERRIDE_LIMIT = 2       # 하루 최대 오버라이드 횟수


class PortfolioManager:
    """최종 의사결정자"""

    name = "portfolio_manager"

    def __init__(self, allow_override: bool = True,
                 daily_limit: int = DAILY_OVERRIDE_LIMIT,
                 min_conviction: float = MIN_CONVICTION):
        self.allow_override = allow_override
        self.daily_limit = daily_limit
        self.min_conviction = min_conviction
        self._override_count = 0
        self._count_date = date.today()
        self.stats = {"approved": 0, "rejected": 0, "overrode": 0}

    def _reset_if_new_day(self) -> None:
        today = date.today()
        if self._count_date != today:
            self._override_count = 0
            self._count_date = today

    @property
    def overrides_left(self) -> int:
        self._reset_if_new_day()
        return max(0, self.daily_limit - self._override_count)

    def decide(
        self,
        proposal: TradeProposal,
        gate_passed: bool,
        blocked_gates: Optional[List[str]] = None,
        gate_reason: str = "",
        exit_exempt_symbols: Optional[Set[str]] = None,
    ) -> PMDecision:
        """
        최종 결정.

        Args:
            proposal: Trader 제안
            gate_passed: 리스크 게이트 통과 여부 (매수 제안일 때만 의미 있음)
            blocked_gates: 차단한 게이트 이름들 (통과 시 빈 리스트)
            gate_reason: 게이트 차단 사유 원문
            exit_exempt_symbols: 자동매도 금지 종목 (SELL 제안을 무력화한다)
        """
        self._reset_if_new_day()
        blocked = list(blocked_gates or [])
        symbol = proposal.symbol

        # ── 0) 자동매도 금지 종목은 SELL 제안을 통과시키지 않는다 ──
        # config `kr.no_auto_exit_symbols`로 지정된 종목(예: 087010 펩트론)은
        # 손절·트레일링을 포함한 모든 자동 청산 경로에서 면제된다.
        # 팀이 아무리 부정적으로 판단해도 PM이 매도를 승인해선 안 된다 — 수동 판단 전용이다.
        if proposal.stance == Stance.SELL and exit_exempt_symbols:
            if symbol in exit_exempt_symbols:
                self.stats["rejected"] += 1
                logger.info(f"[PM] {symbol} SELL 제안 무효화 — 자동매도 금지 종목")
                return PMDecision(
                    symbol=symbol, approved=False, stance=Stance.HOLD,
                    reason="자동매도 금지 종목(exit_exempt) — SELL 제안 무효화, 수동 판단 필요",
                    proposal=proposal.to_dict(),
                )

        # ── 1) 매수가 아니면 게이트 검사 없이 전달 (게이트는 매수 전용) ──
        if proposal.stance != Stance.BUY:
            self.stats["approved"] += 1
            return PMDecision(
                symbol=symbol, approved=True, stance=proposal.stance,
                size_multiplier=proposal.size_multiplier,
                reason=f"{proposal.stance.value} 제안 승인 — {proposal.rationale[:80]}",
                proposal=proposal.to_dict(),
            )

        # ── 2) 게이트 통과 — PM이 거부할 수 있다 ──
        if gate_passed:
            # 팀이 반대하는데 게이트만 통과한 경우는 PM이 막는다
            debate = proposal.debate or {}
            if debate.get("consensus") is False:
                self.stats["rejected"] += 1
                return PMDecision(
                    symbol=symbol, approved=False, stance=Stance.HOLD,
                    reason=f"게이트는 통과했으나 팀 만장일치 반대 — {debate.get('summary', '')[:80]}",
                    proposal=proposal.to_dict(),
                )
            self.stats["approved"] += 1
            return PMDecision(
                symbol=symbol, approved=True, stance=Stance.BUY,
                size_multiplier=proposal.size_multiplier,
                reason=f"승인 — {proposal.rationale[:100]}",
                proposal=proposal.to_dict(),
            )

        # ── 3) 게이트 차단 — 오버라이드 검토 ──
        def reject(why: str) -> PMDecision:
            return PMDecision(
                symbol=symbol, approved=False, stance=Stance.HOLD,
                reason=why, proposal=proposal.to_dict(),
            )

        if not self.allow_override:
            self.stats["rejected"] += 1
            return reject(f"게이트 차단 (오버라이드 비활성): {gate_reason[:80]}")

        # ⚠️ 차단 근거가 비어 있으면 오버라이드를 검토하지 않는다 (fail-closed).
        #   gate_passed=False인데 blocked_gates가 비었다면 게이트 결과와 근거가
        #   불일치하는 상태다. 이때 아래 검사들은 빈 컬렉션이라 전부 통과해버려
        #   하드 게이트 가드와 화이트리스트가 무력화되고 PM이 게이트를 그냥 뚫는다.
        #   무엇이 막았는지 모르는 차단은 절대 뒤집지 않는다.
        if not blocked:
            self.stats["rejected"] += 1
            logger.warning(
                f"[PM] {symbol} 차단됐으나 게이트 목록이 비어 있음 "
                f"— 오버라이드 불가 (fail-closed). 사유: {gate_reason[:80]}"
            )
            return reject(
                f"게이트 차단 근거 불명 (blocked_gates 비어 있음) — 보수적 거부: "
                f"{gate_reason[:60]}"
            )

        # 하드 게이트가 하나라도 걸리면 즉시 거부
        hard_hit = [g for g in blocked if g in HARD_GATES]
        if hard_hit:
            self.stats["rejected"] += 1
            return reject(f"오버라이드 불가 게이트 {hard_hit}: {gate_reason[:60]}")

        # 알 수 없는 게이트도 보수적으로 거부 (화이트리스트 방식)
        unknown = [g for g in blocked if g not in SOFT_GATES]
        if unknown:
            self.stats["rejected"] += 1
            return reject(f"미분류 게이트 {unknown} — 보수적 거부")

        if self.overrides_left <= 0:
            self.stats["rejected"] += 1
            return reject(f"일일 오버라이드 한도 소진 ({self.daily_limit}회)")

        debate = proposal.debate or {}
        if debate.get("consensus") is not True or float(debate.get("confidence", 0)) < 1.0:
            self.stats["rejected"] += 1
            return reject("오버라이드 조건 미충족 — 토론 만장일치 지지 아님")

        if proposal.conviction < self.min_conviction:
            self.stats["rejected"] += 1
            return reject(
                f"오버라이드 조건 미충족 — 확신도 {proposal.conviction:.2f} "
                f"< {self.min_conviction}"
            )

        # ── 오버라이드 승인 ──
        # 게이트를 뚫는 것이므로 사이징을 낮춘다 (통계적으로 불리한 베팅이다)
        self._override_count += 1
        self.stats["overrode"] += 1
        self.stats["approved"] += 1
        size = min(proposal.size_multiplier, 1.0) * 0.7

        logger.warning(
            f"[PM] {symbol} 게이트 오버라이드 승인 "
            f"({self._override_count}/{self.daily_limit}) — "
            f"차단={blocked}, 확신={proposal.conviction:.2f}, 사이징 ×{size:.2f}"
        )
        return PMDecision(
            symbol=symbol, approved=True, stance=Stance.BUY,
            size_multiplier=round(size, 2),
            reason=(f"게이트 오버라이드 승인 (만장일치+확신 {proposal.conviction:.2f}) "
                    f"— 차단 사유: {gate_reason[:60]}"),
            overrode_gate=True, overridden_gates=blocked,
            proposal=proposal.to_dict(),
        )

    def get_stats(self) -> Dict[str, Any]:
        self._reset_if_new_day()
        s = dict(self.stats)
        s["overrides_left_today"] = self.overrides_left
        return s
