"""
포트폴리오 배분기 — 종목별 심의 결과를 **한 번에 모아** 최종 승인한다.

■ 왜 필요한가

팀 심의는 종목을 하나씩 독립적으로 본다. 각 종목이 개별적으로 안전해도
그 결과를 그대로 전부 주문하면 포트폴리오는 안전하지 않다:

    - 후보 5개가 전부 반도체여도 각각은 "통과"다
    - 동시에 승인된 건들의 합계 익스포저를 아무도 보지 않는다
    - 기존 보유분과의 상관·집중은 종목별 심의로는 계산할 수 없다

`cross_validator`의 섹터 규칙은 **이미 보유 중인** 포지션만 센다.
같은 배치에서 동시에 승인된 후보들끼리는 서로를 보지 못한다.
그래서 심의 이후, 주문 이전에 전체를 한 번에 보는 계층이 필요하다.

■ 설계 원칙

1. **한도는 새로 만들지 않는다.** `RiskConfig`(max_positions, max_position_pct,
   min_cash_reserve_pct, max_daily_new_buys, max_positions_per_sector)를 그대로 쓴다.
   여기서 별도 숫자를 정의하면 두 소스가 갈라져 언젠가 어긋난다.
2. **원자적으로 적용한다.** 후보를 확신도 순으로 정렬해 하나씩 배정하며,
   배정할 때마다 누적 상태(섹터 카운트, 현금, 포지션 수)를 갱신한다.
3. **오버라이드 불가.** allocator가 거부하면 그것으로 끝이다.
   PM 오버라이드는 게이트 단계의 개념이고, 포트폴리오 제약은 계좌 생존에 직결된다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

from loguru import logger

from .types import PMDecision, Stance, TeamVerdict


@dataclass
class AllocationResult:
    """한 종목에 대한 배분 판정"""
    symbol: str
    name: str = ""
    approved: bool = False
    sector: Optional[str] = None
    size_multiplier: float = 0.0     # 최종 사이징 (원래 제안에서 축소될 수 있음)
    budget_krw: Decimal = Decimal("0")
    reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol, "name": self.name,
            "approved": self.approved, "sector": self.sector,
            "size_multiplier": round(self.size_multiplier, 2),
            "budget_krw": float(self.budget_krw),
            "reason": self.reason,
        }


@dataclass
class AllocationPlan:
    """배분 전체 결과"""
    approved: List[AllocationResult] = field(default_factory=list)
    rejected: List[AllocationResult] = field(default_factory=list)
    total_budget_krw: Decimal = Decimal("0")
    notes: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "approved": [a.to_dict() for a in self.approved],
            "rejected": [r.to_dict() for r in self.rejected],
            "approved_count": len(self.approved),
            "rejected_count": len(self.rejected),
            "total_budget_krw": float(self.total_budget_krw),
            "notes": self.notes,
        }


class PortfolioAllocator:
    """승인된 매수 후보들을 포트폴리오 제약 아래 한 번에 배분"""

    name = "portfolio_allocator"

    def __init__(self, risk_manager=None, portfolio=None):
        self._risk = risk_manager
        self._portfolio = portfolio

    def set_context(self, risk_manager=None, portfolio=None) -> None:
        if risk_manager is not None:
            self._risk = risk_manager
        if portfolio is not None:
            self._portfolio = portfolio

    # ── 한도 조회 (기존 RiskConfig 재사용) ──────────────────
    def _limits(self) -> Dict[str, Any]:
        cfg = getattr(self._risk, "config", None)
        def g(attr, default):
            v = getattr(cfg, attr, None) if cfg is not None else None
            return default if v is None else v
        return {
            "max_positions": int(g("max_positions", 8)),
            "max_position_pct": float(g("max_position_pct", 28.0)),
            "base_position_pct": float(g("base_position_pct", 25.0)),
            "min_cash_reserve_pct": float(g("min_cash_reserve_pct", 5.0)),
            "max_daily_new_buys": int(g("max_daily_new_buys", 5)),
            "max_positions_per_sector": int(g("max_positions_per_sector", 2)),
            "min_position_value": int(g("min_position_value", 200_000)),
        }

    def _current_state(self) -> Tuple[int, Dict[str, int], Decimal, Decimal]:
        """(보유 종목 수, 섹터별 카운트, 가용 현금, 총자산)"""
        pf = self._portfolio
        if pf is None:
            return 0, {}, Decimal("0"), Decimal("0")

        positions = getattr(pf, "positions", {}) or {}
        held = len(positions)

        sector_count: Dict[str, int] = {}
        for pos in positions.values():
            sec = getattr(pos, "sector", None)
            if sec:
                sector_count[sec] = sector_count.get(sec, 0) + 1

        try:
            equity = Decimal(str(pf.total_equity))
        except (TypeError, AttributeError):
            equity = Decimal("0")

        # 가용 현금은 RiskManager 계산을 그대로 쓴다 (예비금 정의가 한 곳에만 있어야 한다)
        cash = Decimal("0")
        if self._risk is not None and hasattr(self._risk, "_get_available_cash"):
            try:
                cash = Decimal(str(self._risk._get_available_cash(pf)))
            except Exception as e:
                logger.debug(f"[배분] 가용현금 조회 실패, 폴백: {e}")
                cash = Decimal("0")
        if cash <= 0:
            try:
                reserve = equity * Decimal(str(self._limits()["min_cash_reserve_pct"] / 100))
                cash = max(Decimal(str(pf.cash)) - reserve, Decimal("0"))
            except (TypeError, AttributeError):
                cash = Decimal("0")

        return held, sector_count, cash, equity

    # ── 배분 ───────────────────────────────────────────────
    def allocate(
        self,
        verdicts: List[TeamVerdict],
        daily_buys_used: int = 0,
    ) -> AllocationPlan:
        """
        팀이 승인한 BUY 후보들을 포트폴리오 제약 아래 배분한다.

        Args:
            verdicts: 팀 심의 결과 (BUY 승인 건만 대상으로 삼는다)
            daily_buys_used: 오늘 이미 집행한 신규 매수 건수

        Returns:
            AllocationPlan — approved에 남은 것만 주문 대상이다.
        """
        plan = AllocationPlan()
        lim = self._limits()
        held, sector_count, cash, equity = self._current_state()

        # 대상: PM이 승인한 BUY만
        candidates = [
            v for v in verdicts
            if v.decision and v.decision.approved
            and v.decision.stance == Stance.BUY
        ]
        if not candidates:
            plan.notes.append("배분 대상 없음 (승인된 BUY 후보 없음)")
            return plan

        if equity <= 0:
            for v in candidates:
                plan.rejected.append(AllocationResult(
                    v.symbol, v.name, False, self._sector_of(v),
                    reason="총자산 조회 실패 — 배분 보류",
                ))
            plan.notes.append("총자산 0 — 전량 보류")
            return plan

        # 확신도 높은 순 — 한정된 예산을 좋은 후보부터 채운다
        candidates.sort(
            key=lambda v: ((v.proposal.conviction if v.proposal else 0.0),
                           (v.decision.size_multiplier if v.decision else 0.0)),
            reverse=True,
        )

        slots_left = max(0, lim["max_positions"] - held)
        buys_left = max(0, lim["max_daily_new_buys"] - daily_buys_used)
        base_pct = Decimal(str(lim["base_position_pct"] / 100))
        max_pct = Decimal(str(lim["max_position_pct"] / 100))
        min_value = Decimal(str(lim["min_position_value"]))

        plan.notes.append(
            f"보유 {held}/{lim['max_positions']}, 잔여슬롯 {slots_left}, "
            f"오늘 매수 {daily_buys_used}/{lim['max_daily_new_buys']}, "
            f"가용현금 {cash:,.0f}"
        )

        for v in candidates:
            sector = self._sector_of(v)
            size_mult = float(v.decision.size_multiplier) if v.decision else 1.0
            res = AllocationResult(v.symbol, v.name, sector=sector,
                                   size_multiplier=size_mult)

            # ① 포지션 슬롯
            if slots_left <= 0:
                res.reason = f"최대 포지션 수 도달 ({lim['max_positions']}개)"
                plan.rejected.append(res)
                continue

            # ② 일일 신규 매수 한도
            if buys_left <= 0:
                res.reason = f"일일 신규 매수 한도 소진 ({lim['max_daily_new_buys']}건)"
                plan.rejected.append(res)
                continue

            # ③ 섹터 집중 — 이 배치에서 앞서 배정된 건도 함께 센다.
            #    cross_validator는 '이미 보유 중'만 세므로 동시 승인 건을 못 막는다.
            if sector and lim["max_positions_per_sector"] > 0:
                cur = sector_count.get(sector, 0)
                if cur >= lim["max_positions_per_sector"]:
                    res.reason = (f"섹터 집중 한도 ({sector} {cur}/"
                                  f"{lim['max_positions_per_sector']}) — 동시 승인분 포함")
                    plan.rejected.append(res)
                    continue

            # ④ 금액 산정 — 단일 종목 상한 적용
            budget = equity * base_pct * Decimal(str(size_mult))
            cap = equity * max_pct
            if budget > cap:
                budget = cap
                res.reason = f"단일 종목 상한 적용 ({lim['max_position_pct']}%)"

            # ⑤ 남은 현금 안에서만
            if budget > cash:
                budget = cash
                res.reason = (res.reason + " / " if res.reason else "") + "잔여 현금 한도"

            # ⑥ 최소 주문 금액 미달이면 배정하지 않는다 (수수료 대비 비효율)
            if budget < min_value:
                res.reason = (f"최소 포지션 금액 미달 ({budget:,.0f} < {min_value:,.0f})")
                plan.rejected.append(res)
                continue

            # ── 배정 확정: 누적 상태를 즉시 갱신 (원자적 적용) ──
            res.approved = True
            res.budget_krw = budget
            res.size_multiplier = float(budget / (equity * base_pct)) if equity > 0 else 0.0
            plan.approved.append(res)

            cash -= budget
            slots_left -= 1
            buys_left -= 1
            if sector:
                sector_count[sector] = sector_count.get(sector, 0) + 1
            plan.total_budget_krw += budget

        logger.info(
            f"[배분] 후보 {len(candidates)}건 → 승인 {len(plan.approved)}건 / "
            f"거부 {len(plan.rejected)}건, 총 예산 {plan.total_budget_krw:,.0f}원"
        )
        for r in plan.rejected:
            logger.info(f"[배분] 거부 {r.name or r.symbol}: {r.reason}")

        return plan

    @staticmethod
    def _sector_of(verdict: TeamVerdict) -> Optional[str]:
        """심의 결과에서 섹터 추출 (분석가 metrics 또는 verdict 메타)"""
        for r in verdict.reports or []:
            sec = (r.metrics or {}).get("sector")
            if sec:
                return str(sec)
        return None


_allocator: Optional[PortfolioAllocator] = None


def get_allocator(risk_manager=None, portfolio=None) -> PortfolioAllocator:
    global _allocator
    if _allocator is None:
        _allocator = PortfolioAllocator(risk_manager, portfolio)
    else:
        _allocator.set_context(risk_manager, portfolio)
    return _allocator
