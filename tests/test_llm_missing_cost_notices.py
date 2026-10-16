"""Notification coverage for opt-in continuation after unknown LLM costs."""


def _payload(**overrides):
    payload = {
        "kind": "transition",
        "event_at": "2026-10-17T02:45:00+09:00",
        "month": "2026-10",
        "spent_usd": 79.25,
        "reserved_usd": 1.0,
        "monthly_limit_usd": 100.0,
        "policy_mode": "economy",
        "policy_reason": "threshold_80",
        "policy_version": "budget-economy-v1",
        "missing_cost_policy": "log_and_continue",
        "skipped_unknown_count": 2,
        "cost_accounting_complete": False,
    }
    payload.update(overrides)
    return payload


def test_notice_reports_opted_in_unknown_costs_excluded_from_current_total():
    """Catch silently omitting a positive continuation skip from a notice."""
    from src.utils.llm_budget_monitor import format_budget_policy_notice

    text = format_budget_policy_notice(_payload())

    assert "비용 미확인 2건은 합계에서 제외하고 계속 진행합니다." in text
    assert "비용 미확인 2건이 $0" not in text
    assert "공급자 청구서 확정액이 아닙니다" in text


def test_notice_reports_invalid_unknown_cost_count_as_unknown_and_escapes_text():
    """Catch treating an explicit malformed count as zero or emitting raw HTML."""
    from src.utils.llm_budget_monitor import format_budget_policy_notice

    text = format_budget_policy_notice(_payload(
        month='<script>alert("month")</script>',
        skipped_unknown_count="<img src=x onerror=alert(1)>",
    ), event_id="evt-<unsafe>")

    assert "비용 미확인 건수: 미확인" in text
    assert "비용 미확인 0건은 합계에서 제외하고 계속 진행합니다." not in text
    assert "<script>" not in text
    assert "<img" not in text
    assert "evt-&lt;unsafe&gt;" in text


def test_month_reset_reports_previous_month_unknown_costs_excluded_from_total():
    """Catch dropping prior-month continuation skips from a reset notice."""
    from src.utils.llm_budget_monitor import format_budget_policy_notice

    text = format_budget_policy_notice(_payload(
        kind="month_reset",
        month="2026-11",
        spent_usd=0.0,
        reserved_usd=0.0,
        policy_mode="normal",
        policy_reason="kst_month_reset",
        skipped_unknown_count=0,
        cost_accounting_complete=True,
        previous_month={
            "month": "2026-10",
            "spent_usd": 94.0,
            "reserved_usd": 0.5,
            "missing_cost_policy": "log_and_continue",
            "skipped_unknown_count": 3,
            "cost_accounting_complete": False,
        },
    ))

    assert "이전 월 비용 미확인 3건은 합계에서 제외하고 계속 진행했습니다." in text
    assert "새 월 비용 미확인" not in text


def test_legacy_payload_without_unknown_cost_fields_keeps_zero_skip_behavior():
    """Catch inventing skipped costs for durable events written by older versions."""
    from src.utils.llm_budget_monitor import format_budget_policy_notice

    legacy = _payload()
    for key in ("missing_cost_policy", "skipped_unknown_count", "cost_accounting_complete"):
        legacy.pop(key)

    text = format_budget_policy_notice(legacy)

    assert "비용 미확인" not in text
