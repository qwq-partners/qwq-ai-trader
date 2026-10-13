"""동일 입력·동일 역할 모델 비교의 비용 상한 계산 (66차 C, 순수 함수·오프라인).

트레이딩 엔진의 모델 선택 정책(config llm.*)을 다루며 개발 에이전트 라우팅 정책과는 무관하다.
단가는 `llm_pricing.rate_quote`(공표 요율 추정, 청구 보증 아님)를 쓰고, 재시도·폴백·추론 토큰을 호출 수에 반영한다.
비용은 Decimal 문자열로 돌려주며 유료 호출은 실행하지 않는다.
"""
from __future__ import annotations

from decimal import Decimal, ROUND_DOWN, ROUND_UP
from typing import Any, Dict, List, Optional

from ..utils.llm_pricing import rate_quote

SCHEMA_VERSION = "model-comparison-budget-v1"


def _d(value: Any) -> Decimal:
    return Decimal(str(value))


_MICRO = Decimal("0.000000001")


def call_cost_usd(model: str, input_tokens: int, output_tokens: int) -> Optional[Decimal]:
    """한 호출의 추정 비용 — 공표 요율을 Decimal 로 직접 계산하고 1e-9 단위로 올림(보수적). 단가표에 없으면 None."""
    quote = rate_quote(model, input_tokens, output_tokens)          # 모델 존재·장문 구간 판정은 기존 단가표 로직을 따른다
    if quote is None:
        return None
    cost = (_d(input_tokens) * _d(quote.input_usd_per_million) + _d(output_tokens) * _d(quote.output_usd_per_million)) / Decimal(1_000_000)
    return cost.quantize(_MICRO, rounding=ROUND_UP)


def decision_cost_usd(roles: List[Dict[str, Any]], *, retry_factor: Decimal = Decimal("1.1"),
                      max_attempts_per_role: int = 2,
                      fallback: Optional[Any] = None) -> Dict[str, Any]:
    """판단 1건의 비용. roles: [{role, model, input_tokens, output_tokens}].

    - expected: 역할별 1회 × retry_factor(실측 빈 응답 재시도율 상한 가정) — 평균 비용 가정.
    - max: 역할별 max_attempts_per_role(retry_on_empty=1 → 2회) + 단일 폴백 **체인 전체**(dict 하나 또는 list:
      `LLMManager.complete(STRATEGY_ANALYSIS)` 는 primary 실패 뒤 다른 공급자 heavy 를 추가 호출할 수 있고 실패 응답에
      사용량이 있으면 둘 다 과금된다) — 경로상 최대 비용(상한).
    단가 없는 역할이 있으면 둘 다 None 이고 unpriced_roles 에 적는다.
    """
    per_role, unpriced, total = {}, [], Decimal("0")
    for r in roles:
        cost = call_cost_usd(r["model"], int(r["input_tokens"]), int(r["output_tokens"]))
        per_role[r["role"]] = {"model": r["model"], "cost_usd": str(cost) if cost is not None else None}
        if cost is None:
            unpriced.append(r["role"])
        else:
            total += cost
    fb_cost = None
    if fallback is not None:
        chain = fallback if isinstance(fallback, list) else [fallback]
        fb_cost = Decimal("0")
        for i, step in enumerate(chain):
            cost = call_cost_usd(step["model"], int(step["input_tokens"]), int(step["output_tokens"]))
            per_role[f"fallback_{i}"] = {"model": step["model"], "cost_usd": str(cost) if cost is not None else None}
            if cost is None:
                unpriced.append(f"fallback_{i}")
            else:
                fb_cost += cost
    if unpriced:
        return {"per_role": per_role, "unpriced_roles": unpriced, "total_usd": None,
                "total_with_retry_usd": None, "max_usd": None}
    maximum = total * _d(max_attempts_per_role) + (fb_cost if fb_cost is not None else Decimal("0"))
    return {"per_role": per_role, "unpriced_roles": [], "total_usd": str(total),
            "total_with_retry_usd": str((total * retry_factor).quantize(_MICRO, rounding=ROUND_UP)),
            "max_usd": str(maximum.quantize(_MICRO, rounding=ROUND_UP))}


def comparison_budget(*, available_usd: Any, reserve_share: Any, current: Dict[str, Any], candidate: Dict[str, Any],
                      month_days_remaining: int, basis: str = "max") -> Dict[str, Any]:
    """비교 실험에 쓸 수 있는 판단 수. available_usd 는 원장 status 의 가용액(사용·예약 차감 뒤).

    basis="max"(기본) 는 경로상 최대 비용으로 상한을 내고, "expected" 는 평균 가정이다 — 둘을 섞지 않는다.
    """
    avail = _d(available_usd)
    share = _d(reserve_share)
    if not (Decimal("0") <= share < Decimal("1")) or avail < 0 or int(month_days_remaining) < 0:
        raise ValueError("reserve_share ∈ [0,1), available_usd ≥ 0, month_days_remaining ≥ 0")
    if basis not in ("max", "expected"):
        raise ValueError("basis 는 max 또는 expected")
    key = "max_usd" if basis == "max" else "total_with_retry_usd"
    budget = (avail * (Decimal("1") - share)).quantize(Decimal("0.000001"), rounding=ROUND_DOWN)
    cur, cand = current.get(key), candidate.get(key)
    if cur is None or cand is None:
        return {"schema_version": SCHEMA_VERSION, "basis": basis, "comparison_budget_usd": str(budget), "pair_cost_usd": None,
                "max_comparisons": None, "per_day_cap": None,
                "note": "단가가 없는 역할이 있어 호출 수를 계산할 수 없다 — 0 으로 대체하지 않음"}
    pair = _d(cur) + _d(cand)
    max_cmp = int((budget / pair).to_integral_value(rounding=ROUND_DOWN)) if pair > 0 else None
    per_day = (int(max_cmp // int(month_days_remaining)) if max_cmp is not None and int(month_days_remaining) > 0 else None)
    return {"schema_version": SCHEMA_VERSION, "basis": basis, "comparison_budget_usd": str(budget), "pair_cost_usd": str(pair),
            "max_comparisons": max_cmp, "per_day_cap": per_day,
            "note": "공표 요율 추정(올림)이며 청구 보증이 아니다. 실제 유료 호출은 이 계산으로 시작되지 않는다"}
