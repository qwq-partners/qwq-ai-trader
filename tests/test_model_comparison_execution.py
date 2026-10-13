"""실제 임시 SQLite 원장·가짜 전송기로 비교 입장 경계를 검증한다."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from decimal import Decimal
import sqlite3

import pytest

from src.utils.llm_budget import BudgetBlocked, MonthlyBudgetLedger

NOW = datetime(2026, 10, 13, 3, tzinfo=timezone.utc)


def setup_guard(tmp_path, *, cap=2, limit=100, clock=lambda: NOW):
    from src.analytics.model_comparison_execution import ComparisonCallGuard, ComparisonExecutionPolicy
    ledger = MonthlyBudgetLedger(tmp_path / "budget.db", limit, clock=clock)
    policy = ComparisonExecutionPolicy(experiment_id="study-1", daily_attempt_cap=cap,
                                       allowed_models=("gpt-5-mini", "gpt-5.6-sol"), max_output_tokens=400)
    return ledger, policy, ComparisonCallGuard(ledger=ledger, policy=policy)


def request(model="gpt-5-mini", **kwargs):
    from src.analytics.model_comparison_execution import ComparisonRequest
    return ComparisonRequest(model=model, prompt="same facts", system="same role",
                             max_output_tokens=kwargs.get("max_output_tokens", 100))


def measured(cost=0.0001):
    from src.analytics.model_comparison_execution import ComparisonAttemptResult
    return ComparisonAttemptResult(value="approve", actual_cost_usd=cost)


async def paid_once(req):
    return measured()


def test_reserves_before_transport_and_counts_retry_fallback_on_restart(tmp_path):
    from src.analytics.model_comparison_execution import ComparisonCallGuard
    ledger, policy, guard = setup_guard(tmp_path)
    calls = []

    async def transport(req):
        # 예약 없이 전송되는 회귀를 검출한다.
        assert ledger.status()["reserved_usd"] > 0
        calls.append(req.model)
        return measured()

    first = asyncio.run(guard.call(request(), dispatch=transport))
    assert first.value == "approve"
    assert ledger.status()["spent_usd"] == 0.0001
    ledger.close()
    reopened = MonthlyBudgetLedger(ledger.path, 100, clock=lambda: NOW)
    restarted = ComparisonCallGuard(ledger=reopened, policy=policy)
    asyncio.run(restarted.call(request("gpt-5.6-sol"), dispatch=paid_once))
    with pytest.raises(BudgetBlocked, match="comparison_daily_attempt_cap"):
        asyncio.run(restarted.call(request(), dispatch=transport))
    assert calls == ["gpt-5-mini"]
    assert reopened.status()["settled_count"] == 2
    assert reopened.status()["spent_usd"] == 0.0002


def test_concurrent_instances_cannot_overrun_one_daily_slot(tmp_path):
    from src.analytics.model_comparison_execution import ComparisonCallGuard
    ledger, policy, guard = setup_guard(tmp_path, cap=1)
    other = MonthlyBudgetLedger(ledger.path, 100, clock=lambda: NOW)
    guards = (guard, ComparisonCallGuard(ledger=other, policy=policy))
    dispatches = []

    async def transport(req):
        dispatches.append(req.model)
        await asyncio.sleep(0.01)
        return measured()

    def run(one):
        try:
            return asyncio.run(one.call(request(), dispatch=transport)).value
        except BudgetBlocked as exc:
            return exc.reason

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(run, guards))
    assert sorted(outcomes) == ["approve", "comparison_daily_attempt_cap"]
    assert dispatches == ["gpt-5-mini"]
    assert ledger.status()["settled_count"] == 1


@pytest.mark.parametrize("cost", [None, -1, True, float("nan"), float("inf"), Decimal("NaN")])
def test_unknown_or_invalid_cost_never_releases_possible_spend(tmp_path, cost):
    ledger, policy, guard = setup_guard(tmp_path, cap=10)
    calls = []

    async def transport(req):
        calls.append(req.model)
        return measured(cost)

    if cost is None:
        assert asyncio.run(guard.call(request(), dispatch=transport)).actual_cost_usd is None
    else:
        with pytest.raises(ValueError, match="actual_cost_usd"):
            asyncio.run(guard.call(request(), dispatch=transport))
    status = ledger.status()
    assert status["unresolved_count"] == 1 and status["spent_usd"] == 0
    with pytest.raises(BudgetBlocked, match="unresolved_usage"):
        asyncio.run(guard.call(request(), dispatch=transport))
    assert calls == ["gpt-5-mini"]


@pytest.mark.parametrize("failure", [asyncio.CancelledError, RuntimeError])
def test_cancel_or_exception_is_unknown_and_blocks_after_month_rollover(tmp_path, failure):
    now = [NOW]
    ledger, policy, guard = setup_guard(tmp_path, clock=lambda: now[0])

    async def transport(req):
        raise failure()

    with pytest.raises(failure):
        asyncio.run(guard.call(request(), dispatch=transport))
    assert ledger.status()["unresolved_count"] == 1
    now[0] = datetime(2026, 11, 1, tzinfo=timezone.utc)
    with pytest.raises(BudgetBlocked, match="unresolved_usage"):
        asyncio.run(guard.call(request(), dispatch=paid_once))


def test_crash_orphan_stays_unknown_without_resetting_daily_cap(tmp_path):
    from src.analytics.model_comparison_execution import ComparisonCallGuard
    ledger, policy, guard = setup_guard(tmp_path)

    async def transport(req):
        # 공급자가 실행 중일 수 있는 상태에서 소유 프로세스 소실을 모사한다.
        ledger.close()
        raise RuntimeError("lost owner")

    with pytest.raises(RuntimeError):
        asyncio.run(guard.call(request(), dispatch=transport))
    restored = MonthlyBudgetLedger(ledger.path, 100, clock=lambda: NOW)
    with pytest.raises(BudgetBlocked, match="unresolved_usage"):
        asyncio.run(ComparisonCallGuard(ledger=restored, policy=policy).call(request(), dispatch=paid_once))
    assert restored.status()["unresolved_count"] == 1


def test_kst_day_month_rollover_and_existing_monthly_baseline_preserved(tmp_path):
    now = [datetime(2026, 10, 31, 14, 59, tzinfo=timezone.utc)]
    ledger = MonthlyBudgetLedger(tmp_path / "budget.db", 100, clock=lambda: now[0])
    token = ledger.reserve("gpt-5-mini", "ordinary production call", "", 400)
    ledger.settle(token, outcome="known", actual_cost_usd=0.0002)
    baseline = ledger.status()
    ledger, policy, guard = setup_guard(tmp_path, cap=1, clock=lambda: now[0])
    asyncio.run(guard.call(request(), dispatch=paid_once))
    assert ledger.status()["spent_usd"] == 0.0003
    with pytest.raises(BudgetBlocked, match="comparison_daily_attempt_cap"):
        asyncio.run(guard.call(request(), dispatch=paid_once))
    now[0] = datetime(2026, 10, 31, 15, 0, tzinfo=timezone.utc)
    asyncio.run(guard.call(request(), dispatch=paid_once))
    status = ledger.status()
    assert status["month"] == "2026-11" and status["spent_usd"] == 0.0001
    assert status["activated_at"] == baseline["activated_at"]
    assert status["baseline_month"] == baseline["baseline_month"] == "2026-10"
    with sqlite3.connect(ledger.path) as db:
        assert db.execute("SELECT SUM(actual_units) FROM reservations WHERE month='2026-10'").fetchone()[0] == 300000


def test_monthly_block_or_policy_request_violation_never_dispatches(tmp_path):
    ledger, policy, guard = setup_guard(tmp_path, limit=0)
    calls = []

    async def transport(req):
        calls.append(req)
        return measured()

    with pytest.raises(BudgetBlocked, match="monthly_limit_exceeded"):
        asyncio.run(guard.call(request(), dispatch=transport))
    with pytest.raises(BudgetBlocked, match="comparison_model_not_allowed"):
        asyncio.run(guard.call(request("unknown"), dispatch=transport))
    with pytest.raises(BudgetBlocked, match="comparison_output_cap"):
        asyncio.run(guard.call(request(max_output_tokens=401), dispatch=transport))
    assert calls == []


def test_explicit_immutable_policy_and_ledger_required(tmp_path):
    from dataclasses import replace
    from src.analytics.model_comparison_execution import ComparisonCallGuard
    ledger, policy, guard = setup_guard(tmp_path)
    for changed in (replace(policy, daily_attempt_cap=3), replace(policy, experiment_id="other"),
                    replace(policy, max_output_tokens=500), replace(policy, allowed_models=("gpt-5-mini",))):
        with pytest.raises(BudgetBlocked, match="comparison_policy_mismatch"):
            ComparisonCallGuard(ledger=ledger, policy=changed)
    for args in (dict(ledger=None, policy=policy), dict(ledger=ledger, policy=None)):
        with pytest.raises(ValueError):
            ComparisonCallGuard(**args)


def test_zero_cap_blocks_and_zero_measured_cost_is_known(tmp_path):
    ledger, policy, guard = setup_guard(tmp_path, cap=0)
    with pytest.raises(BudgetBlocked, match="comparison_daily_attempt_cap"):
        asyncio.run(guard.call(request(), dispatch=paid_once))
    other = tmp_path / "other"
    other.mkdir()
    ledger, policy, guard = setup_guard(other)

    async def free(req):
        return measured(0)

    asyncio.run(guard.call(request(), dispatch=free))
    assert ledger.status()["settled_count"] == 1
    assert ledger.status()["unresolved_count"] == 0


@pytest.mark.parametrize("field,bad", [
    ("daily_attempt_cap", True), ("daily_attempt_cap", -1), ("daily_attempt_cap", 1.5),
    ("daily_attempt_cap", float("inf")), ("max_output_tokens", 0), ("max_output_tokens", True),
    ("allowed_models", ["gpt-5-mini"]), ("allowed_models", ()),
    ("allowed_models", ("gpt-5-mini", "gpt-5-mini")), ("experiment_id", ""),
])
def test_invalid_policy_is_rejected_before_storage(field, bad):
    from src.analytics.model_comparison_execution import ComparisonExecutionPolicy
    values = dict(experiment_id="study", daily_attempt_cap=2, allowed_models=("gpt-5-mini",), max_output_tokens=100)
    values[field] = bad
    with pytest.raises(ValueError):
        ComparisonExecutionPolicy(**values)


def test_actual_task_cancellation_during_transport_retains_unknown_spend(tmp_path):
    ledger, policy, guard = setup_guard(tmp_path)

    async def run():
        entered = asyncio.Event()

        async def transport(req):
            entered.set()
            await asyncio.Future()

        task = asyncio.create_task(guard.call(request(), dispatch=transport))
        await entered.wait()
        assert ledger.status()["reserved_usd"] > 0
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())
    assert ledger.status()["unresolved_count"] == 1
    assert ledger.status()["block_reason"] == "unresolved_usage"
    assert ledger.active_reservations == {}


def test_missing_attempt_history_fails_closed_instead_of_restarting_cap(tmp_path):
    from src.analytics.model_comparison_execution import ComparisonCallGuard
    ledger, policy, guard = setup_guard(tmp_path)
    asyncio.run(guard.call(request(), dispatch=paid_once))
    with sqlite3.connect(ledger.path) as db:
        db.execute("DROP TABLE comparison_attempts")
    with pytest.raises(BudgetBlocked, match="comparison_storage_corrupt"):
        ComparisonCallGuard(ledger=ledger, policy=policy)
    with pytest.raises(BudgetBlocked, match="comparison_storage_unavailable"):
        asyncio.run(guard.call(request(), dispatch=paid_once))


def test_persisted_policy_describes_currency_timezone_and_request_contract(tmp_path):
    import json
    ledger, policy, guard = setup_guard(tmp_path)
    with sqlite3.connect(ledger.path) as db:
        saved = json.loads(db.execute("SELECT policy FROM comparison_policy").fetchone()[0])
    assert saved["currency"] == "USD" and saved["timezone"] == "Asia/Seoul"
    assert saved["adapter_contract"] == "single-provider-attempt-v1"
    assert saved["schema_version"] == "model-comparison-execution-v1"
    assert saved["max_output_tokens"] == 400


def test_clock_crossing_day_during_reservation_does_not_dispatch(tmp_path, monkeypatch):
    now = [NOW]
    ledger, policy, guard = setup_guard(tmp_path, clock=lambda: now[0])
    reserve = ledger.reserve
    calls = []

    def crossing(*args, **kwargs):
        token = reserve(*args, **kwargs)
        now[0] = datetime(2026, 10, 14, 3, tzinfo=timezone.utc)
        return token

    monkeypatch.setattr(ledger, "reserve", crossing)

    async def transport(req):
        calls.append(req)
        return measured()

    with pytest.raises(BudgetBlocked, match="comparison_admission_day_changed"):
        asyncio.run(guard.call(request(), dispatch=transport))
    assert calls == []
    assert ledger.status()["unresolved_count"] == 0
    assert ledger.status()["reserved_usd"] == 0


@pytest.mark.parametrize("cost,expected_nanos", [
    (Decimal("0.0001"), 100000), ("0.0001", 100000),
    (Decimal("0.00010000000000000000001"), 100001),
    ("0.00010000000000000000001", 100001),
    (Decimal("1e-999999999"), 1),
])
def test_decimal_cost_preserves_conservative_nano_usd_settlement(tmp_path, cost, expected_nanos):
    ledger, policy, guard = setup_guard(tmp_path)

    async def transport(req):
        return measured(cost)

    result = asyncio.run(guard.call(request(), dispatch=transport))
    assert result.actual_cost_usd == cost
    with sqlite3.connect(ledger.path) as db:
        assert db.execute("SELECT actual_units FROM reservations").fetchone()[0] == expected_nanos
    assert ledger.status()["unresolved_count"] == 0
