"""조건부 진입계획(EntryPlan) 주문 직전 검증기 — T11 계약 2.3 (2026-09-15).

정본은 `src.core.batch_analyzer.PendingSignal` 이다(별도 주문 파이프라인·복사 캐시를 만들지
않는다). 이 모듈은 계획과 최신 호가·시장 상태를 받아 허용/대기/거절과 사유 코드를 내는
**순수 함수**만 제공한다. 이번 단계에서는 **shadow 전용** — 결과로 기존 실주문을 허용하거나
차단하지 않는다(엔진은 기록만 한다).

원칙:
- 종목 적합성(팀 판단)과 즉시 진입 가능성(이 검증기)은 분리한다.
- setup 별 필수 조건(sepa_pullback 밴드, vcp_breakout 트리거, gap_vwap 의 VWAP)을 공통 틀
  안에서 적용하되 무조건 같은 규칙으로 합치지 않는다.
- 계획이 없으면 검증하지 않는다 — 현재가로 그럴듯한 계획을 자동 생성하지 않는다.
- 손절·계획 위험은 기존 ExitManager/stop_policy/entry_risk 해석을 따른다(여기서 목표가·손절가를
  바꾸지 않는다).
- 시장가 주문 직전 재조회만으로 가격 상한을 보장한다고 주장하지 않는다(사유 코드 PRICE_ABOVE_CAP
  는 "검증 시점" 판정이다).
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Any, Dict, List, Optional

PLAN_CHECK_STATUS = ("allow", "wait", "reject")

# 사유 코드 (계약 2.3) — 접두사 뒤에 ':<이름>' 이 붙을 수 있다
REASON_CODES = (
    "PLAN_EXPIRED", "PRICE_ABOVE_CAP", "PRICE_BELOW_BAND", "TRIGGER_NOT_MET",
    "QUOTE_MISSING", "QUOTE_STALE", "INPUT_MISSING", "INTRADAY_BLOCK", "INVALIDATED",
    "RISK_BUDGET", "SLOT_FULL", "DAILY_LIMIT", "COST_RR_LOW", "CHECKER_ERROR",
    "CHECKER_NOT_IMPLEMENTED",
)

# 호가 신선도 상한(초) — 이보다 오래된 호가로는 판정하지 않는다 (정책값: 장중 5분 루프 주기)
QUOTE_MAX_AGE_SEC = 300


@dataclass
class PlanCheck:
    """주문 직전 shadow 검증 결과"""

    status: str                                   # allow | wait | reject
    reasons: List[str] = field(default_factory=list)
    expected_fill_price: Optional[float] = None
    cost_adjusted_rr: Optional[float] = None      # (목표-예상체결-비용)/(예상체결-손절+비용); 계산 불가면 None
    checked_at: Optional[datetime] = None
    quote_as_of: Optional[datetime] = None
    missing_inputs: List[str] = field(default_factory=list)
    plan_id: str = ""
    setup: str = ""

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        for k in ("checked_at", "quote_as_of"):
            v = getattr(self, k)
            d[k] = v.isoformat(timespec="seconds") if isinstance(v, datetime) else None
        return d


def check_entry_plan(
    plan: Any,
    quote: Optional[Dict[str, Any]],
    now: datetime,
    *,
    intraday_level: Optional[str] = None,
    risk_ctx: Optional[Dict[str, Any]] = None,
    setup_rules: Optional[Dict[str, Any]] = None,
) -> PlanCheck:
    """계획(PendingSignal 또는 그 dict)과 최신 호가로 허용/대기/거절을 판정한다 (순수 함수).

    Args:
        plan: `PendingSignal` 인스턴스 또는 `to_dict()` 결과.
        quote: {"price", "as_of"(datetime|ISO), "vwap"(선택), "bid"/"ask"(선택)} — None 이면 QUOTE_MISSING.
        now: 판정 시각(주입).
        intraday_level: 급락 감지기 수준 normal|caution|crash|severe|None.
        risk_ctx: {"risk_budget_ok": bool|None, "slots_available": int|None, "daily_buys_left": int|None,
                   "fee_bps": float|None, "slippage_bps": float|None} — 없으면 해당 항목은 판정하지 않는다.
        setup_rules: setup 별 필수 조건 덮어쓰기(테스트용).

    계약 커밋 단계의 본문은 보수적 placeholder 이며(항상 wait + CHECKER_NOT_IMPLEMENTED),
    담당 C 가 구현한다. 어떤 경우에도 예외를 밖으로 내지 않는다(CHECKER_ERROR).
    """
    try:
        pid = getattr(plan, "plan_id", None) if not isinstance(plan, dict) else plan.get("plan_id", "")
        setup = getattr(plan, "setup", None) if not isinstance(plan, dict) else plan.get("setup", "")
        return PlanCheck(
            status="wait", reasons=["CHECKER_NOT_IMPLEMENTED"], checked_at=now,
            plan_id=str(pid or ""), setup=str(setup or ""),
        )
    except Exception as e:  # pragma: no cover — placeholder
        return PlanCheck(status="wait", reasons=[f"CHECKER_ERROR:{type(e).__name__}"], checked_at=now)
