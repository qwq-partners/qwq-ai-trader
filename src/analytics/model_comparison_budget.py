"""동일 입력·동일 역할 모델 비교의 비용 상한 계산 (66차 C, 순수 함수·오프라인).

트레이딩 엔진의 모델 선택 정책(config llm.*)을 다루며 개발 에이전트 라우팅 정책과는 무관하다.
단가는 `llm_pricing.rate_quote`(공표 요율 추정, 청구 보증 아님)를 쓰고, 재시도·폴백·추론 토큰을 호출 수에 반영한다.
비용은 Decimal 문자열로 돌려주며 유료 호출은 실행하지 않는다.
"""
from __future__ import annotations

from decimal import Decimal, ROUND_DOWN
from typing import Any, Dict, List, Optional

from ..utils.llm_pricing import rate_quote

SCHEMA_VERSION = "model-comparison-budget-v1"


def _d(value: Any) -> Decimal:
    return Decimal(str(value))


def call_cost_usd(model: str, input_tokens: int, output_tokens: int) -> Optional[Decimal]:
    """한 호출의 추정 비용. 단가표에 없는 모델은 None(0 으로 대체하지 않음)."""
    quote = rate_quote(model, input_tokens, output_tokens)
    return None if quote is None else _d(quote.cost_usd)


def decision_cost_usd(roles: List[Dict[str, Any]], *, retry_factor: Decimal = Decimal("1.1")) -> Dict[str, Any]:
    """판단 1건(역할별 호출 1회씩) 의 추정 비용. roles: [{role, model, input_tokens, output_tokens}].

    retry_factor 는 빈 응답 재시도(retry_on_empty=1, 실측 6회 중 1회)·폴백 비율의 상한 가정이다.
    단가 없는 역할이 하나라도 있으면 total 은 None 이고 그 역할을 unpriced 에 적는다.
    """
    per_role, unpriced, total = {}, [], Decimal("0")
    for r in roles:
        cost = call_cost_usd(r["model"], int(r["input_tokens"]), int(r["output_tokens"]))
        per_role[r["role"]] = {"model": r["model"], "cost_usd": str(cost) if cost is not None else None}
        if cost is None:
            unpriced.append(r["role"])
        else:
            total += cost
    if unpriced:
        return {"per_role": per_role, "unpriced_roles": unpriced, "total_usd": None, "total_with_retry_usd": None}
    return {"per_role": per_role, "unpriced_roles": [], "total_usd": str(total),
            "total_with_retry_usd": str((total * retry_factor).quantize(Decimal("0.000001")))}


def comparison_budget(*, available_usd: Any, reserve_share: Any, current: Dict[str, Any], candidate: Dict[str, Any],
                      month_days_remaining: int) -> Dict[str, Any]:
    """비교 실험에 쓸 수 있는 판단 수. available_usd 는 원장 status 의 가용액(사용·예약 차감 뒤).

    reserve_share 는 운영 필수 판단(기존 Bull/Bear·레짐 등)을 위해 남겨 두는 비율. 비교 1건 = 현행 판단 비용 +
    후보 판단 비용(둘 다 재시도 상한 반영). 어느 쪽이든 단가 없으면 count 는 None.
    """
    avail = _d(available_usd)
    share = _d(reserve_share)
    if not (Decimal("0") <= share < Decimal("1")) or avail < 0 or int(month_days_remaining) < 0:
        raise ValueError("reserve_share ∈ [0,1), available_usd ≥ 0, month_days_remaining ≥ 0")
    budget = (avail * (Decimal("1") - share)).quantize(Decimal("0.000001"), rounding=ROUND_DOWN)
    cur, cand = current.get("total_with_retry_usd"), candidate.get("total_with_retry_usd")
    if cur is None or cand is None:
        return {"schema_version": SCHEMA_VERSION, "comparison_budget_usd": str(budget), "pair_cost_usd": None,
                "max_comparisons": None, "per_day_cap": None,
                "note": "단가가 없는 역할이 있어 호출 수를 계산할 수 없다 — 0 으로 대체하지 않음"}
    pair = _d(cur) + _d(cand)
    max_cmp = int((budget / pair).to_integral_value(rounding=ROUND_DOWN)) if pair > 0 else None
    per_day = (int(max_cmp // int(month_days_remaining)) if max_cmp is not None and int(month_days_remaining) > 0 else None)
    return {"schema_version": SCHEMA_VERSION, "comparison_budget_usd": str(budget), "pair_cost_usd": str(pair),
            "max_comparisons": max_cmp, "per_day_cap": per_day,
            "note": "공표 요율 추정이며 청구 보증이 아니다. 실제 유료 호출은 이 계산으로 시작되지 않는다"}
