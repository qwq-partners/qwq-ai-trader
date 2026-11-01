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
    ledger = MonthlyBudgetLedger(tmp_path / "budget.db", limit, clock=clock, policy_enabled=True)
    policy = ComparisonExecutionPolicy(experiment_id="study-1", daily_attempt_cap=cap,
                                       allowed_models=("gpt-5-mini", "gpt-5.6-sol"), max_output_tokens=400,
                                       experiment_usd_ceiling="1", valid_from="2026-10-13",
                                       valid_until="2026-11-30", pairs_per_day=10,
                                       max_common_context_bytes=65536)
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
    reopened = MonthlyBudgetLedger(ledger.path, 100, clock=lambda: NOW, policy_enabled=True)
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
    other = MonthlyBudgetLedger(ledger.path, 100, clock=lambda: NOW, policy_enabled=True)
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
    assert outcomes.count("approve") == 1
    assert set(outcomes) <= {"approve", "comparison_daily_attempt_cap", "comparison_storage_unavailable"}
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
        with pytest.raises(BudgetBlocked, match="comparison_invalid_measured_cost"):
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
    restored = MonthlyBudgetLedger(ledger.path, 100, clock=lambda: NOW, policy_enabled=True)
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
    ("pairs_per_day", 0), ("pairs_per_day", True), ("pairs_per_day", 1.0), ("pairs_per_day", 2 ** 31),
    ("max_common_context_bytes", 0), ("max_common_context_bytes", None), ("max_common_context_bytes", "16384"),
])
def test_invalid_policy_is_rejected_before_storage(field, bad):
    from src.analytics.model_comparison_execution import ComparisonExecutionPolicy
    values = dict(experiment_id="study", daily_attempt_cap=2, allowed_models=("gpt-5-mini",), max_output_tokens=100,
                  experiment_usd_ceiling="1", valid_from="2026-10-13", valid_until="2026-10-31",
                  pairs_per_day=1, max_common_context_bytes=16384)
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
    assert saved["adapter_contract"] == "single-provider-attempt-v2"
    assert saved["schema_version"] == "model-comparison-execution-v2"
    assert saved["max_output_tokens"] == 400
    assert saved["experiment_usd_ceiling"] == "1" and saved["required_policy_mode"] == "normal"
    assert (saved["valid_from"], saved["valid_until"]) == ("2026-10-13", "2026-11-30")
    assert (saved["pairs_per_day"], saved["max_common_context_bytes"]) == (10, 65536)


def test_pair_limits_have_no_default_and_changes_are_policy_mismatch(tmp_path):
    from dataclasses import replace
    from src.analytics.model_comparison_execution import ComparisonCallGuard, ComparisonExecutionPolicy
    ledger, policy, guard = setup_guard(tmp_path)
    with pytest.raises(TypeError):
        ComparisonExecutionPolicy(experiment_id="x", daily_attempt_cap=1, allowed_models=("gpt-5-mini",),
                                  max_output_tokens=1, experiment_usd_ceiling="1", valid_from="2026-10-13",
                                  valid_until="2026-10-31")
    for change in ({"pairs_per_day": 11}, {"max_common_context_bytes": 65535}):
        with pytest.raises(BudgetBlocked, match="comparison_policy_mismatch"):
            ComparisonCallGuard(ledger=ledger, policy=replace(policy, **change))


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


def test_pre_dispatch_forensic_write_contention_cannot_create_unsent_unknown(tmp_path, monkeypatch):
    ledger, policy, guard = setup_guard(tmp_path)
    original_reserve = ledger.reserve
    blocker = [None]
    calls = []

    def reserve_then_contend(*args, **kwargs):
        token = original_reserve(*args, **kwargs)
        blocker[0] = sqlite3.connect(ledger.path, isolation_level=None)
        blocker[0].execute("BEGIN IMMEDIATE")
        return token

    def release():
        if blocker[0] is not None:
            blocker[0].execute("ROLLBACK")
            blocker[0].close()
            blocker[0] = None

    async def transport(req):
        # 예약 뒤 지속되는 잠금은 전송 전 포렌식 기록·보상 정산을 모두 막는다.
        # 전송에 진입할 수 있으면 잠금을 풀고 실제 비용을 정산할 수 있다.
        calls.append(req.model)
        release()
        return measured()

    monkeypatch.setattr(ledger, "reserve", reserve_then_contend)
    failure = None
    try:
        asyncio.run(guard.call(request(), dispatch=transport))
    except BudgetBlocked as exc:
        failure = exc.reason
    finally:
        release()
    assert ledger.status()["unresolved_count"] == 0
    assert failure is None
    assert calls == ["gpt-5-mini"]
    assert ledger.status()["spent_usd"] == 0.0001


@pytest.mark.parametrize("response", ["malformed", measured("NaN"), measured("1e500")])
def test_adapter_contract_violation_is_budget_blocked_with_unknown_spend(tmp_path, response):
    ledger, policy, guard = setup_guard(tmp_path)

    async def transport(req):
        return response

    with pytest.raises(BudgetBlocked, match="comparison_invalid_"):
        asyncio.run(guard.call(request(), dispatch=transport))
    assert ledger.status()["unresolved_count"] == 1


# ── 전송 채널 소유권: 구독 전송은 일 슬롯만 소비하고 공유 월 원장을 예약·정산하지 않는다 ──────────

def subscription_request():
    from src.analytics.model_comparison_execution import ComparisonRequest
    return ComparisonRequest(model="gpt-5-mini", prompt="same facts", system="same role",
                             max_output_tokens=100, billing_channel="subscription")


def declared(fn, channel="subscription"):
    """구독 요청은 채널을 명시 선언한 어댑터만 전송할 수 있다 — 검사용 선언 래퍼."""
    class _Declared:
        billing_channel = channel

        async def __call__(self, req):
            return await fn(req)
    return _Declared()


def subscription_result(estimate=0.0123):
    from src.analytics.model_comparison_execution import ComparisonAttemptResult
    return ComparisonAttemptResult(value={"request_sent": True, "subscription_estimated_cost_usd": estimate},
                                   actual_cost_usd=None, settlement="subscription")


def test_request_contract_names_billing_channel_and_rejects_unknown_channel():
    from src.analytics.model_comparison_execution import ComparisonRequest, _result_json
    assert request().billing_channel == "api"
    with pytest.raises(ValueError, match="billing_channel"):
        ComparisonRequest(model="gpt-5-mini", prompt="p", system="s", max_output_tokens=1, billing_channel="free")
    # 구독 정산은 월 원장 USD 를 실을 수 없다 — 저장 직렬화에서 거절한다.
    from src.analytics.model_comparison_execution import ComparisonAttemptResult
    with pytest.raises(ValueError, match="subscription"):
        _result_json(ComparisonAttemptResult(value={}, actual_cost_usd=0.01, settlement="subscription"))


def test_subscription_attempt_consumes_slot_without_monthly_reservation(tmp_path):
    ledger, policy, guard = setup_guard(tmp_path, cap=1)
    seen = []

    async def transport(req):
        status = ledger.status()
        seen.append((req.billing_channel, status["reserved_usd"], status["settled_count"], status["unresolved_count"]))
        return subscription_result()

    result = asyncio.run(guard.call(subscription_request(), dispatch=declared(transport), attempt_id="sub-1"))
    assert result.settlement == "subscription" and result.actual_cost_usd is None
    assert seen == [("subscription", 0, 0, 0)]          # 전송 중에도 월 예약이 없다
    status = ledger.status()
    assert (status["settled_count"], status["unresolved_count"], status["spent_usd"], status["reserved_usd"]) == (0, 0, 0, 0)
    record = guard.lookup("sub-1")
    assert (record["state"], record["reservation_token"], record["block_reason"]) == ("known", None, None)
    assert record["original_estimated_cost_usd"] is None   # API 추정비 없음 — 구독 추정비는 value 에만 있다
    # 같은 ID 는 재전송 없이 저장 응답을 그대로 복구한다.
    assert asyncio.run(guard.call(subscription_request(), dispatch=declared(transport), attempt_id="sub-1")) == result
    assert len(seen) == 1
    # 일 슬롯은 유료 시도와 같은 상한을 소비한다(cap=1).
    with pytest.raises(BudgetBlocked, match="comparison_daily_attempt_cap"):
        asyncio.run(guard.call(request(), dispatch=paid_once))
    rows = guard.day_attempts(ledger.today().isoformat())
    assert [(r["attempt_id"], r["billing_channel"], r["state"], r["cost_usd"], r["subscription_estimated_cost_usd"])
            for r in rows if r["block_reason"] is None] == [("sub-1", "subscription", "known", None, 0.0123)]


@pytest.mark.parametrize("response", [measured(0.0001), measured(None),
                                      __import__("src.analytics.model_comparison_execution", fromlist=["x"])
                                      .ComparisonAttemptResult(value="x", actual_cost_usd=0.0001,
                                                               settlement="rejected_before_generation")])
def test_subscription_attempt_rejects_api_settlement_without_touching_ledger(tmp_path, response):
    ledger, policy, guard = setup_guard(tmp_path, cap=5)
    calls = []

    async def transport(req):
        calls.append(req.billing_channel)
        return response

    with pytest.raises(BudgetBlocked, match="comparison_billing_channel_mismatch"):
        asyncio.run(guard.call(subscription_request(), dispatch=declared(transport), attempt_id="sub-bad"))
    status = ledger.status()
    assert (status["settled_count"], status["unresolved_count"], status["reserved_usd"], status["spent_usd"]) == (0, 0, 0, 0)
    record = guard.lookup("sub-bad")
    assert (record["state"], record["reservation_token"]) == ("blocked", None)
    # 다른 채널로 바꿔 복구하지 않고 같은 ID 의 재전송도 막는다.
    with pytest.raises(BudgetBlocked, match="comparison_billing_channel_mismatch"):
        asyncio.run(guard.call(subscription_request(), dispatch=declared(transport), attempt_id="sub-bad"))
    assert calls == ["subscription"]


def test_subscription_result_with_ledger_cost_is_invalid_measured_cost(tmp_path):
    from src.analytics.model_comparison_execution import ComparisonAttemptResult
    ledger, policy, guard = setup_guard(tmp_path, cap=5)

    async def transport(req):
        return ComparisonAttemptResult(value={}, actual_cost_usd=0.0001, settlement="subscription")

    with pytest.raises(BudgetBlocked, match="comparison_invalid_measured_cost"):
        asyncio.run(guard.call(subscription_request(), dispatch=declared(transport), attempt_id="sub-cost"))
    assert ledger.status()["unresolved_count"] == 0 and guard.lookup("sub-cost")["state"] == "blocked"


def test_api_attempt_returning_subscription_settlement_keeps_reservation_unknown(tmp_path):
    ledger, policy, guard = setup_guard(tmp_path, cap=5)

    async def transport(req):
        assert req.billing_channel == "api"
        return subscription_result()

    with pytest.raises(BudgetBlocked, match="comparison_billing_channel_mismatch"):
        asyncio.run(guard.call(request(), dispatch=transport, attempt_id="api-sub"))
    status = ledger.status()
    assert status["unresolved_count"] == 1 and status["spent_usd"] == 0     # 과금 여부 불명 — 해제하지 않는다
    assert guard.lookup("api-sub")["state"] == "blocked"


def test_subscription_attempt_exception_is_blocked_without_ledger_rows(tmp_path):
    ledger, policy, guard = setup_guard(tmp_path, cap=5)

    async def transport(req):
        raise RuntimeError("cli crashed")

    with pytest.raises(RuntimeError):
        asyncio.run(guard.call(subscription_request(), dispatch=declared(transport), attempt_id="sub-crash"))
    assert ledger.status()["unresolved_count"] == 0 and ledger.status()["settled_count"] == 0
    record = guard.lookup("sub-crash")
    assert (record["state"], record["reservation_token"], record["block_reason"]) == (
        "blocked", None, "comparison_attempt_interrupted")
    with pytest.raises(BudgetBlocked, match="comparison_attempt_interrupted"):
        asyncio.run(guard.call(subscription_request(), dispatch=declared(paid_once), attempt_id="sub-crash"))


def test_subscription_admission_still_requires_experiment_window_and_policy(tmp_path):
    from src.analytics.model_comparison_execution import ComparisonCallGuard
    calls = []

    async def transport(req):
        calls.append(req.billing_channel)
        return subscription_result()

    expired = datetime(2026, 12, 1, 3, tzinfo=timezone.utc)
    ledger, policy, guard = setup_guard(tmp_path, cap=5, clock=lambda: expired)
    with pytest.raises(BudgetBlocked, match="comparison_experiment_expired"):
        asyncio.run(guard.call(subscription_request(), dispatch=declared(transport), attempt_id="sub-late"))
    ledger.close()
    disabled = MonthlyBudgetLedger(tmp_path / "plain.db", 100, clock=lambda: NOW, policy_enabled=False)
    other = ComparisonCallGuard(ledger=disabled, policy=policy)
    with pytest.raises(BudgetBlocked, match="comparison_policy_disabled"):
        asyncio.run(other.call(subscription_request(), dispatch=declared(transport), attempt_id="sub-plain"))
    assert calls == []
    # 차단 ID 는 고정돼 다음 날 자동 재시도가 되지 않는다.
    assert guard.lookup("sub-late")["state"] == "blocked" and other.lookup("sub-plain")["state"] == "blocked"


class _DeclaredAdapter:
    """채널을 선언하는 가짜 어댑터 — 가드는 전송 전에 요청 채널과 대조해야 한다."""
    def __init__(self, channel, result):
        self.billing_channel, self._result, self.calls = channel, result, 0

    async def __call__(self, req):
        self.calls += 1
        return self._result


def test_adapter_declared_channel_is_checked_before_any_dispatch(tmp_path):
    ledger, policy, guard = setup_guard(tmp_path, cap=5)
    api_adapter = _DeclaredAdapter("api", measured())
    # 구독 요청 → 유료 API 어댑터: 월 예약 없는 과금이 되므로 공급자 호출 0 으로 차단한다.
    with pytest.raises(BudgetBlocked, match="comparison_billing_channel_mismatch"):
        asyncio.run(guard.call(subscription_request(), dispatch=api_adapter, attempt_id="sub-to-api"))
    assert api_adapter.calls == 0
    assert guard.lookup("sub-to-api")["state"] == "blocked"
    # 유료 요청 → 구독 어댑터: 미전송이므로 예약은 not_sent 로 해제되고 불명이 남지 않는다.
    sub_adapter = _DeclaredAdapter("subscription", subscription_result())
    with pytest.raises(BudgetBlocked, match="comparison_billing_channel_mismatch"):
        asyncio.run(guard.call(request(), dispatch=sub_adapter, attempt_id="api-to-sub"))
    assert sub_adapter.calls == 0
    status = ledger.status()
    assert (status["unresolved_count"], status["spent_usd"], status["reserved_usd"]) == (0, 0, 0)
    assert guard.lookup("api-to-sub")["state"] == "blocked"
    # 채널이 맞는 선언 어댑터는 그대로 전송된다.
    ok = _DeclaredAdapter("subscription", subscription_result())
    assert asyncio.run(guard.call(subscription_request(), dispatch=ok, attempt_id="sub-ok")).settlement == "subscription"
    assert ok.calls == 1 and ledger.status()["settled_count"] == 0


def test_stored_subscription_result_with_token_or_api_request_is_settlement_mismatch(tmp_path):
    from src.utils.llm_budget import BudgetStateError
    ledger, policy, guard = setup_guard(tmp_path, cap=5)

    async def transport(req):
        return subscription_result()

    asyncio.run(guard.call(subscription_request(), dispatch=declared(transport), attempt_id="sub-1"))
    with sqlite3.connect(ledger.path) as db:
        db.execute("UPDATE comparison_requests SET reservation_token='forged' WHERE attempt_id='sub-1'")
    with pytest.raises(BudgetStateError, match="comparison_settlement_mismatch"):
        guard.lookup("sub-1")
    # 같은 ID 복구도 정상으로 돌려주지 않는다.
    with pytest.raises(BudgetStateError, match="comparison_settlement_mismatch"):
        asyncio.run(guard.call(subscription_request(), dispatch=declared(transport), attempt_id="sub-1"))
    # 유료 요청 행에 구독 정산 응답을 끼워 넣은 저장도 거절한다.
    asyncio.run(guard.call(request(), dispatch=paid_once, attempt_id="api-1"))
    from src.analytics.model_comparison_execution import _result_json
    with sqlite3.connect(ledger.path) as db:
        db.execute("UPDATE comparison_requests SET result_json=? WHERE attempt_id='api-1'",
                   (_result_json(subscription_result()),))
    with pytest.raises(BudgetStateError, match="comparison_settlement_mismatch"):
        guard.lookup("api-1")


def test_subscription_request_requires_declared_subscription_adapter(tmp_path):
    """미선언 콜백은 유료 API 일 수 있다 — 구독 요청은 전송 전에 거절하고 공급자 호출 0 이어야 한다(재리뷰 P1)."""
    ledger, policy, guard = setup_guard(tmp_path, cap=5)
    calls = []

    async def undeclared(req):
        calls.append(req.billing_channel)
        return measured()

    with pytest.raises(BudgetBlocked, match="comparison_billing_channel_mismatch"):
        asyncio.run(guard.call(subscription_request(), dispatch=undeclared, attempt_id="sub-undeclared"))
    assert calls == []
    record = guard.lookup("sub-undeclared")
    assert (record["state"], record["reservation_token"], record["billing_channel"]) == ("blocked", None, "subscription")
    with sqlite3.connect(ledger.path) as db:
        assert db.execute("SELECT COUNT(*) FROM reservations").fetchone()[0] == 0
    # 유료 요청의 미선언 콜백은 기존 계약대로 허용된다.
    assert asyncio.run(guard.call(request(), dispatch=undeclared, attempt_id="api-undeclared")).value == "approve"
    assert calls == ["api"]
