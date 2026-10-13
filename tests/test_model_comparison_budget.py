"""66차 C — 모델 비교 비용 상한: 단가표·재시도 상한·예비 비율·단가 없음 처리 (오프라인)."""
from decimal import Decimal

import pytest

from src.analytics.model_comparison_budget import call_cost_usd, comparison_budget, decision_cost_usd

CURRENT = [{"role": "bull", "model": "gpt-5-mini", "input_tokens": 900, "output_tokens": 400},
           {"role": "bear", "model": "gemini-3.1-flash-lite-preview", "input_tokens": 900, "output_tokens": 400}]


def test_call_cost_uses_published_rates_and_none_for_unknown():
    assert call_cost_usd("gpt-5-mini", 900, 400) == Decimal("0.001025")      # 900×0.25 + 400×2.0 per 1M
    assert call_cost_usd("gpt-5.6-sol", 900, 400) == Decimal("0.0116")
    assert call_cost_usd("unknown-model", 1, 1) is None


def test_decision_cost_sums_roles_with_retry_factor():
    d = decision_cost_usd(CURRENT)
    assert d["total_usd"] == "0.001850" or Decimal(d["total_usd"]) == Decimal("0.00185")
    assert Decimal(d["total_with_retry_usd"]) == Decimal("0.002035")           # ×1.1
    bad = decision_cost_usd(CURRENT + [{"role": "x", "model": "nope", "input_tokens": 1, "output_tokens": 1}])
    assert bad["total_usd"] is None and bad["unpriced_roles"] == ["x"]


def test_comparison_budget_counts_pairs_and_keeps_reserve():
    cur = decision_cost_usd(CURRENT)
    cand = decision_cost_usd([{"role": "bull", "model": "gpt-5.6-sol", "input_tokens": 900, "output_tokens": 400},
                              {"role": "bear", "model": "gemini-3.1-pro-preview", "input_tokens": 900, "output_tokens": 400}])
    b = comparison_budget(available_usd="99.703917", reserve_share="0.5", current=cur, candidate=cand, month_days_remaining=18)
    assert Decimal(b["comparison_budget_usd"]) == Decimal("49.851958")
    pair = Decimal(cur["total_with_retry_usd"]) + Decimal(cand["total_with_retry_usd"])
    assert Decimal(b["pair_cost_usd"]) == pair and b["max_comparisons"] == int(Decimal("49.851958") // pair)
    assert b["per_day_cap"] == b["max_comparisons"] // 18
    none = comparison_budget(available_usd="10", reserve_share="0.5", current=cur,
                             candidate=decision_cost_usd([{"role": "bull", "model": "nope", "input_tokens": 1, "output_tokens": 1}]),
                             month_days_remaining=18)
    assert none["max_comparisons"] is None
    with pytest.raises(ValueError):
        comparison_budget(available_usd="10", reserve_share="1", current=cur, candidate=cand, month_days_remaining=1)
