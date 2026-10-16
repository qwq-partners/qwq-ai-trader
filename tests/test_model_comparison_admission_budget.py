"""비교 예약의 원장 내부 입장 조건·실험 상한·대사 연결·쌍 복구를 임시 원장으로 검증한다.

가짜 공급자와 tmp_path 원장만 사용한다. 실제 KIS/LLM/네트워크·운영 경로 접근 없음.
"""
import asyncio
import hashlib
import json
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.utils.llm_budget import BudgetBlocked, MonthlyBudgetLedger

NOW = datetime(2026, 10, 13, 3, tzinfo=timezone.utc)  # KST 12:00


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)


def policy(**changes):
    from src.analytics.model_comparison_execution import ComparisonExecutionPolicy
    values = dict(experiment_id="R1", daily_attempt_cap=50,
                  allowed_models=("gpt-5-mini", "gemini-3.1-flash-lite"), max_output_tokens=400,
                  experiment_usd_ceiling="1", valid_from="2026-10-13", valid_until="2026-10-31",
                  pairs_per_day=10, max_common_context_bytes=65536)
    values.update(changes)
    return ComparisonExecutionPolicy(**values)


def request():
    from src.analytics.model_comparison_execution import ComparisonRequest
    return ComparisonRequest("gpt-5-mini", "same facts", "same role", 100)


def result(cost=0.0001, **value):
    from src.analytics.model_comparison_execution import ComparisonAttemptResult
    return ComparisonAttemptResult({"success": True, "api_cost_usd": None if cost is None else str(cost), **value}, cost)


def request_units(tmp_path):
    """동일 요청의 실제 예약 단위를 별도 임시 원장에서 측정한다(계산식 복제 금지)."""
    probe = tmp_path / "probe"
    probe.mkdir(exist_ok=True)
    ledger = MonthlyBudgetLedger(probe / "probe.db", 100, clock=lambda: NOW)
    req = request()
    token = ledger.reserve(req.model, req.prompt, req.system, req.max_output_tokens)
    with sqlite3.connect(ledger.path) as db:
        units = db.execute("SELECT reserved_units FROM reservations WHERE token=?", (token,)).fetchone()[0]
    ledger.settle(token, outcome="not_sent")
    return units


def prespend(path, limit, units, month="2026-10"):
    """기존 운영 정산 1건을 nano 단위로 정확히 둔다(정책 관측·고점 기록 없이, 임시 원장만)."""
    MonthlyBudgetLedger(path, limit, clock=lambda: NOW).close()
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO reservations VALUES (?,?,?,?,'settled',?,?,?)",
                   ("a" * 32, month, "gpt-5-mini", units, units, NOW.isoformat(), NOW.isoformat()))


def guard_for(path, limit=100, clock=lambda: NOW, policy_enabled=True, **changes):
    from src.analytics.model_comparison_execution import ComparisonCallGuard
    ledger = MonthlyBudgetLedger(path, limit, clock=clock, policy_enabled=policy_enabled)
    return ledger, ComparisonCallGuard(ledger=ledger, policy=policy(**changes))


def frozen(path):
    with sqlite3.connect(path) as db:
        return (db.execute("SELECT activated_at,baseline_month FROM settings").fetchone(),
                db.execute("SELECT token,actual_units FROM reservations WHERE state='settled' "
                           "AND token NOT IN (SELECT token FROM policy_attempts WHERE task='model_comparison') "
                           "ORDER BY token").fetchall()
                if db.execute("SELECT 1 FROM sqlite_master WHERE name='policy_attempts'").fetchone()
                else db.execute("SELECT token,actual_units FROM reservations WHERE state='settled' ORDER BY token").fetchall())


def comparison_reservations(path):
    with sqlite3.connect(path) as db:
        return db.execute("SELECT COUNT(*) FROM reservations r JOIN policy_attempts p ON p.token=r.token "
                          "WHERE p.task='model_comparison'").fetchone()[0]


# --- A(1) 정책 비활성 ---------------------------------------------------------------------------

def test_policy_disabled_ledger_never_reserves_or_dispatches(tmp_path):
    ledger, guard = guard_for(tmp_path / "b.db", policy_enabled=False)
    calls = []

    async def dispatch(req):
        calls.append(req)
        return result()

    with pytest.raises(BudgetBlocked, match="comparison_policy_disabled"):
        asyncio.run(guard.call(request(), dispatch=dispatch, attempt_id="disabled"))
    assert calls == []
    with sqlite3.connect(ledger.path) as db:
        assert db.execute("SELECT COUNT(*) FROM reservations").fetchone()[0] == 0
    record = guard.lookup("disabled")
    assert record["state"] == "blocked" and record["reservation_token"] is None
    assert record["block_reason"] == "comparison_policy_disabled"


# --- A(2) 80/95/100% 경계(이번 예약 포함) --------------------------------------------------------

@pytest.mark.parametrize("case,expected", [
    ("exactly_80", None),                       # 이번 예약 포함 정확히 80%: 넘지 않으므로 허용
    ("one_nano_over_80", "comparison_economy_threshold"),
    ("economy_mode", "comparison_policy_mode_not_normal"),
    ("one_nano_over_95", "essential_budget_reserved"),
    ("essential_mode", "essential_budget_reserved"),
    ("limit", "monthly_limit_exceeded"),
])
def test_budget_thresholds_include_this_reservation(tmp_path, case, expected):
    units = request_units(tmp_path)
    limit_units = 1_000_000_000
    spent = {"exactly_80": 800_000_000 - units, "one_nano_over_80": 800_000_000 - units + 1,
             "economy_mode": 850_000_000, "one_nano_over_95": 950_000_000 - units + 1,
             "essential_mode": 960_000_000, "limit": limit_units}[case]
    path = tmp_path / "b.db"
    prespend(path, 1, spent)
    before = frozen(path)
    ledger, guard = guard_for(path, limit=1)
    calls = []

    async def dispatch(req):
        calls.append(req)
        return result(units / 1_000_000_000)

    if expected is None:
        asyncio.run(guard.call(request(), dispatch=dispatch, attempt_id=case))
        assert calls and guard.lookup(case)["state"] == "known"
        # 80%에 도달한 뒤 다음 연구 예약은 절약 단계라 거부된다.
        with pytest.raises(BudgetBlocked, match="comparison_policy_mode_not_normal"):
            asyncio.run(guard.call(request(), dispatch=dispatch, attempt_id=case + "-next"))
        assert len(calls) == 1
    else:
        with pytest.raises(BudgetBlocked, match=expected):
            asyncio.run(guard.call(request(), dispatch=dispatch, attempt_id=case))
        assert calls == [] and comparison_reservations(path) == 0
        assert ledger.status()["spent_usd"] == spent / 1_000_000_000
    assert frozen(path) == before
    assert ledger.status()["unresolved_count"] == 0


def test_economy_rejection_keeps_policy_observation(tmp_path):
    path = tmp_path / "b.db"
    prespend(path, 1, 850_000_000)
    ledger, guard = guard_for(path, limit=1)
    with pytest.raises(BudgetBlocked, match="comparison_policy_mode_not_normal"):
        asyncio.run(guard.call(request(), dispatch=lambda r: None, attempt_id="economy"))
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT mode FROM policy_cursor").fetchone()[0] == "economy"


# --- 실험 상한·유효 구간·ID 변경·재시작·월 전환 ----------------------------------------------------

def test_experiment_ceiling_counts_settled_spend_across_restart_and_month(tmp_path):
    units = request_units(tmp_path)
    now = [datetime(2026, 10, 31, 3, tzinfo=timezone.utc)]
    ceiling = str((units * 3 // 2) / 1_000_000_000)
    path = tmp_path / "b.db"
    ledger, guard = guard_for(path, clock=lambda: now[0], experiment_usd_ceiling=ceiling,
                              valid_from="2026-10-31", valid_until="2026-11-01")
    calls = []

    async def dispatch(req):
        calls.append(req)
        return result(units / 1_000_000_000)

    asyncio.run(guard.call(request(), dispatch=dispatch, attempt_id="october"))
    before = frozen(path)
    activated = ledger.status()["activated_at"]
    ledger.close()
    # 프로세스 재시작 + KST 월 전환: 월 집계는 새 달이지만 실험 상한은 그대로다.
    now[0] = datetime(2026, 10, 31, 15, 30, tzinfo=timezone.utc)
    ledger, guard = guard_for(path, clock=lambda: now[0], experiment_usd_ceiling=ceiling,
                              valid_from="2026-10-31", valid_until="2026-11-01")
    status = ledger.status()
    assert status["month"] == "2026-11" and status["spent_usd"] == 0
    with pytest.raises(BudgetBlocked, match="comparison_experiment_ceiling"):
        asyncio.run(guard.call(request(), dispatch=dispatch, attempt_id="november"))
    # 유효 구간 밖은 상한과 무관하게 만료다.
    now[0] = datetime(2026, 11, 1, 15, 30, tzinfo=timezone.utc)
    with pytest.raises(BudgetBlocked, match="comparison_experiment_expired"):
        asyncio.run(guard.call(request(), dispatch=dispatch, attempt_id="after-window"))
    assert len(calls) == 1 and comparison_reservations(path) == 1
    assert ledger.status()["activated_at"] == activated and frozen(path) == before


@pytest.mark.parametrize("clock,window", [
    (datetime(2026, 10, 13, 15, 0, tzinfo=timezone.utc), ("2026-10-13", "2026-10-13")),  # KST 10/14 00:00
    (datetime(2026, 10, 13, 3, tzinfo=timezone.utc), ("2026-10-14", "2026-10-20")),      # 시작 전
])
def test_kst_validity_window_is_inclusive_and_day_change_expires(tmp_path, clock, window):
    ledger, guard = guard_for(tmp_path / "b.db", clock=lambda: clock, valid_from=window[0], valid_until=window[1])
    with pytest.raises(BudgetBlocked, match="comparison_experiment_expired"):
        asyncio.run(guard.call(request(), dispatch=lambda r: None, attempt_id="expired"))
    assert comparison_reservations(ledger.path) == 0


def test_last_valid_kst_day_is_admitted(tmp_path):
    clock = datetime(2026, 10, 13, 14, 59, tzinfo=timezone.utc)  # KST 10/13 23:59
    ledger, guard = guard_for(tmp_path / "b.db", clock=lambda: clock, valid_from="2026-10-13", valid_until="2026-10-13")

    async def dispatch(req):
        return result()

    asyncio.run(guard.call(request(), dispatch=dispatch, attempt_id="last-day"))
    assert guard.lookup("last-day")["state"] == "known"


def test_changed_experiment_id_or_ceiling_cannot_replace_stored_policy(tmp_path):
    from src.analytics.model_comparison_execution import ComparisonCallGuard
    ledger, guard = guard_for(tmp_path / "b.db")
    for changes in ({"experiment_id": "R2"}, {"experiment_usd_ceiling": "2"},
                    {"valid_until": "2026-12-31"}, {"valid_from": "2026-10-01"}):
        with pytest.raises(BudgetBlocked, match="comparison_policy_mismatch"):
            ComparisonCallGuard(ledger=ledger, policy=policy(**changes))


def test_stored_v1_policy_is_rejected_without_migration(tmp_path):
    from src.analytics.model_comparison_execution import ComparisonCallGuard
    ledger = MonthlyBudgetLedger(tmp_path / "b.db", 100, clock=lambda: NOW, policy_enabled=True)
    v1 = json.dumps({"experiment_id": "R1", "daily_attempt_cap": 50, "allowed_models": ["gpt-5-mini"],
                     "max_output_tokens": 400, "schema_version": "model-comparison-execution-v1",
                     "currency": "USD", "timezone": "Asia/Seoul", "adapter_contract": "single-provider-attempt-v1"},
                    sort_keys=True, separators=(",", ":"))
    with sqlite3.connect(ledger.path) as db:
        db.execute("CREATE TABLE comparison_policy (id INTEGER PRIMARY KEY CHECK(id=1), policy TEXT NOT NULL)")
        db.execute("INSERT INTO comparison_policy VALUES (1,?)", (v1,))
        db.execute("CREATE TABLE comparison_attempts (slot TEXT PRIMARY KEY, admission_day TEXT NOT NULL, "
                   "reservation_token TEXT, state TEXT NOT NULL)")
    with pytest.raises(BudgetBlocked, match="comparison_policy_mismatch"):
        ComparisonCallGuard(ledger=ledger, policy=policy())
    with sqlite3.connect(ledger.path) as db:
        assert db.execute("SELECT policy FROM comparison_policy").fetchone()[0] == v1


@pytest.mark.parametrize("field,bad", [
    ("experiment_usd_ceiling", 1), ("experiment_usd_ceiling", "0"), ("experiment_usd_ceiling", "-1"),
    ("experiment_usd_ceiling", "NaN"), ("experiment_usd_ceiling", "abc"), ("experiment_usd_ceiling", "1e30"),
    ("valid_from", "20261013"), ("valid_from", "2026-13-01"), ("valid_until", "2026-10-12"),
    ("valid_until", None), ("required_policy_mode", "economy"),
])
def test_invalid_v2_policy_fields_are_rejected(field, bad):
    with pytest.raises(ValueError):
        policy(**{field: bad})


def test_inflight_reservation_counts_toward_experiment_ceiling(tmp_path):
    units = request_units(tmp_path)
    ceiling = str((units * 3 // 2) / 1_000_000_000)
    path = tmp_path / "b.db"
    first_ledger, first = guard_for(path, experiment_usd_ceiling=ceiling)
    second_ledger, second = guard_for(path, experiment_usd_ceiling=ceiling)
    observed = []

    async def nested(req):
        # 첫 예약이 reserved 인 동안 다른 원장 객체(다른 프로세스 모사)가 입장을 시도한다.
        try:
            await second.call(request(), dispatch=nested_never, attempt_id="second")
        except BudgetBlocked as exc:
            observed.append(exc.reason)
        return result(units / 1_000_000_000)

    async def nested_never(req):
        raise AssertionError("상한 초과 시도는 전송되면 안 된다")

    asyncio.run(first.call(request(), dispatch=nested, attempt_id="first"))
    assert observed == ["comparison_experiment_ceiling"]
    assert comparison_reservations(path) == 1


def test_concurrent_guards_cannot_cross_experiment_ceiling(tmp_path):
    units = request_units(tmp_path)
    ceiling = str((units * 3 // 2) / 1_000_000_000)
    path = tmp_path / "b.db"
    guards = [guard_for(path, experiment_usd_ceiling=ceiling)[1] for _ in range(2)]
    barrier = threading.Barrier(2)
    dispatched = []

    async def dispatch(req):
        dispatched.append(req)
        await asyncio.sleep(0.05)
        return result(units / 1_000_000_000)

    outcomes = [None, None]

    def run(index):
        barrier.wait()
        try:
            asyncio.run(guards[index].call(request(), dispatch=dispatch, attempt_id=f"race-{index}"))
            outcomes[index] = "admitted"
        except BudgetBlocked as exc:
            outcomes[index] = exc.reason

    threads = [threading.Thread(target=run, args=(i,)) for i in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
    assert outcomes.count("admitted") == 1, outcomes
    assert set(outcomes) <= {"admitted", "comparison_experiment_ceiling", "storage_unavailable",
                             "comparison_storage_unavailable"}
    assert len(dispatched) == 1 and comparison_reservations(path) == 1


# --- D 대사 연결 ---------------------------------------------------------------------------------

def _review(ledger, evidence, tokens, cost=0.0002):
    return {"version": "llm-budget-reconciliation-v1", "activated_at": ledger.status()["activated_at"],
            "reviewer_ref": "synthetic-operator", "evidence_sha256": hashlib.sha256(evidence.read_bytes()).hexdigest(),
            "adjustments": [{"token": token, "actual_cost_usd": cost} for token in tokens],
            "clear_bound_breach": False}


def _unknown_attempt(tmp_path):
    path = tmp_path / "b.db"
    prespend(path, 100, 300_000)
    ledger, guard = guard_for(path)
    calls = []

    async def dispatch(req):
        calls.append(req)
        return result(None, success=False, responded_at="2026-10-13T03:00:01+00:00")

    original = asyncio.run(guard.call(request(), dispatch=dispatch, attempt_id="unknown-cost"))
    evidence = tmp_path / "evidence.txt"
    evidence.write_text("synthetic provider usage export")
    token = guard.lookup("unknown-cost")["reservation_token"]
    return path, ledger, guard, calls, original, evidence, token


def test_unknown_cost_reconciled_by_existing_cli_is_known_reconciled_without_rewriting(tmp_path):
    from src.utils.llm_budget_reconciliation import reconcile_budget
    path, ledger, guard, calls, original, evidence, token = _unknown_attempt(tmp_path)
    before = frozen(path)
    assert guard.lookup("unknown-cost")["state"] == "unknown"
    assert guard.lookup("unknown-cost")["reconciled"] is None  # reconciliations 테이블 없음
    review = _review(ledger, evidence, [token])
    preview = reconcile_budget(path, 100, review, evidence)
    assert guard.lookup("unknown-cost")["state"] == "unknown"  # 미리보기는 읽기 전용
    applied = reconcile_budget(path, 100, review, evidence, apply=True,
                               expected_state_sha256=preview["state_sha256"])
    assert applied["applied"]
    record = guard.lookup("unknown-cost")
    assert record["state"] == "known_reconciled"
    assert record["result"] == original and record["result"].value["success"] is False
    assert record["original_estimated_cost_usd"] is None
    reconciled = record["reconciled"]
    assert reconciled["actual_cost_usd"] == "0.0002" and isinstance(reconciled["reconciliation_id"], int)
    assert reconciled["reviewer_ref"] == "synthetic-operator"
    assert reconciled["evidence_sha256"] == review["evidence_sha256"] and reconciled["applied_at"]
    # 같은 ID 재호출은 원래 응답만 돌려주고 재전송·재과금하지 않는다.
    assert asyncio.run(guard.call(request(), dispatch=lambda r: None, attempt_id="unknown-cost")) == original
    assert len(calls) == 1
    status = ledger.status()
    assert not status["blocked"] and status["unresolved_count"] == 0
    assert frozen(path) == before
    # 중복 적용은 거부되고 감사 행은 하나뿐이다.
    with pytest.raises(ValueError):
        reconcile_budget(path, 100, review, evidence, apply=True, expected_state_sha256=preview["state_sha256"])
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT COUNT(*) FROM reconciliations").fetchone()[0] == 1
    assert guard.lookup("unknown-cost")["reconciled"] == reconciled


def test_stale_preview_and_tampered_evidence_never_apply(tmp_path):
    from src.utils.llm_budget_reconciliation import reconcile_budget
    path, ledger, guard, calls, original, evidence, token = _unknown_attempt(tmp_path)
    review = _review(ledger, evidence, [token])
    preview = reconcile_budget(path, 100, review, evidence)
    with pytest.raises(ValueError, match="dry-run state changed"):
        reconcile_budget(path, 100, review, evidence, apply=True, expected_state_sha256="0" * 64)
    evidence.write_text("edited after review")
    with pytest.raises(ValueError, match="evidence hash mismatch"):
        reconcile_budget(path, 100, review, evidence, apply=True, expected_state_sha256=preview["state_sha256"])
    record = guard.lookup("unknown-cost")
    assert record["state"] == "unknown" and record["reconciled"] is None
    assert ledger.status()["unresolved_count"] == 1


def test_settlement_failure_with_measured_cost_reconciles_without_hiding_original_estimate(tmp_path, monkeypatch):
    from src.utils.llm_budget import BudgetStateError
    from src.utils.llm_budget_reconciliation import reconcile_budget
    path = tmp_path / "b.db"
    ledger, guard = guard_for(path)

    def failed_settlement(token, **kwargs):
        ledger.abandon(token)
        raise BudgetStateError("storage_unavailable")

    async def dispatch(req):
        return result(0.0001)

    monkeypatch.setattr(ledger, "settle", failed_settlement)
    with pytest.raises(BudgetBlocked):
        asyncio.run(guard.call(request(), dispatch=dispatch, attempt_id="settle-failed"))
    assert guard.lookup("settle-failed")["state"] == "pending_reconciliation"
    evidence = tmp_path / "evidence.txt"
    evidence.write_text("synthetic invoice line")
    token = guard.lookup("settle-failed")["reservation_token"]
    review = _review(ledger, evidence, [token], cost=0.00015)
    preview = reconcile_budget(path, 100, review, evidence)
    reconcile_budget(path, 100, review, evidence, apply=True, expected_state_sha256=preview["state_sha256"])
    record = guard.lookup("settle-failed")
    assert record["state"] == "known_reconciled"
    assert record["original_estimated_cost_usd"] == "0.0001"
    assert record["reconciled"]["actual_cost_usd"] == "0.00015"


# --- E 쌍 검사·복구 ------------------------------------------------------------------------------

def _runner(path, *, unknown=False, crash=False):
    from src.analytics import model_comparison_runner as m
    from src.utils.llm import GeminiClient, LLMConfig, LLMProvider, LLMResponse, OpenAIClient

    class FakeOpenAI(OpenAIClient):
        def __init__(self):
            super().__init__(LLMConfig(openai_api_key="", gemini_api_key=""))
            self.calls = []

        async def complete(self, prompt, system="", model=None, **kw):
            if crash:
                import os
                os._exit(0)
            self.calls.append(model)
            await asyncio.sleep(0)
            return LLMResponse("APPROVE" if kw.get("reasoning_effort") == "low" else "REJECT", model,
                               LLMProvider.OPENAI, input_tokens=100, output_tokens=10, requested_model=model,
                               resolved_model=model, usage_known=not unknown,
                               billing_status="unknown" if unknown else "reported_usage")

    class FakeGemini(GeminiClient):
        def __init__(self):
            super().__init__(LLMConfig(openai_api_key="", gemini_api_key=""))
            self.calls = []

        async def complete(self, prompt, system="", model=None, **kw):
            self.calls.append(model)
            return LLMResponse("ACCEPT", model, LLMProvider.GEMINI, input_tokens=100, output_tokens=10,
                               requested_model=model, resolved_model=model, usage_known=True)

    ledger, guard = guard_for(path)
    openai, gemini = FakeOpenAI(), FakeGemini()
    adapter = m.SingleAttemptAdapter(openai=openai, gemini=gemini, clock=lambda: NOW)
    runner = m.PairedComparisonRunner(guard=guard, adapter=adapter, clock=lambda: NOW)
    spec = m.PairSpec(decision_id="decision-1", signal_id="signal-1", symbol="005490",
                      common_context="same immutable facts", input_available_at=NOW.isoformat(),
                      current=m.RoleControls("gpt-5-mini", 400, "minimal", 0.3, 20260803),
                      candidate=m.RoleControls("gpt-5-mini", 400, "low", 0.3, 20260803),
                      bear=m.RoleControls("gemini-3.1-flash-lite", 400, None, 0.0),
                      neutral_sizing=None, pricing_basis="offline_replay",
                      fixed_at=NOW.isoformat(), strict_on_disagreement=True, fallback_chain_timeout_seconds=10.0)
    return m, ledger, guard, runner, spec, openai, gemini


def _failing_final_write(path):
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE IF NOT EXISTS comparison_pairs (pair_id TEXT PRIMARY KEY, manifest TEXT NOT NULL, "
                   "requested_at TEXT NOT NULL, result TEXT)")
        db.execute("CREATE TRIGGER fail_pair_write BEFORE UPDATE ON comparison_pairs "
                   "BEGIN SELECT RAISE(ABORT, 'simulated final write failure'); END")


def _allow_final_write(path):
    with sqlite3.connect(path) as db:
        db.execute("DROP TRIGGER fail_pair_write")


def _stored_pair(path, pair_id):
    with sqlite3.connect(path) as db:
        return db.execute("SELECT manifest,requested_at,result FROM comparison_pairs WHERE pair_id=?",
                          (pair_id,)).fetchone()


def _journal(path):
    return path.parent / "incomplete-2026-10.jsonl"


def _write_evidence(path, row):
    """작업자가 저장 실패 때 쓰는 incomplete 저널 레코드와 같은 형식(row_sha256 = 정규화 JSON)."""
    from src.analytics.model_comparison_worker import JOURNAL_VERSION, row_sha256
    record = {"schema_version": JOURNAL_VERSION, "kind": "incomplete_result", "recorded_at": NOW.isoformat(),
              "decision_id": row["decision_id"], "pair_id": row["pair_id"], "row_sha256": row_sha256(row), "row": row}
    with open(_journal(path), "a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")


def _unpersisted_pair(tmp_path, **kwargs):
    path = tmp_path / "b.db"
    m, ledger, guard, runner, spec, openai, gemini = _runner(path, **kwargs)
    _failing_final_write(path)
    with pytest.raises(m.PairResultPersistenceError) as raised:
        asyncio.run(runner.run(spec))
    _allow_final_write(path)
    row = raised.value.result
    _write_evidence(path, row)
    return m, path, ledger, guard, runner, spec, openai, gemini, row


def test_unpersisted_pair_is_restored_only_from_original_row(tmp_path):
    from copy import deepcopy
    from src.analytics import model_comparison_recovery as recovery
    m, path, ledger, guard, runner, spec, openai, gemini, row = _unpersisted_pair(tmp_path)
    pair_id = row["pair_id"]
    manifest, requested_at, stored = _stored_pair(path, pair_id)
    assert stored is None
    inspected = recovery.inspect_pair(guard, pair_id)
    assert inspected["found"] and not inspected["complete"] and inspected["requested_at"] == requested_at
    assert {a["attempt_id"] for a in inspected["attempts"]} == {
        a["attempt_id"] for arm in ("current", "candidate") for a in row[arm]["attempts"]}
    assert all(a["state"] == "known" for a in inspected["attempts"])
    diagnostic = recovery.export_incomplete_pair(guard, pair_id)
    assert diagnostic["diagnostic_only"] is True and diagnostic["complete"] is False

    rejected = []
    tampered = deepcopy(row)
    tampered["current"]["attempts"][0]["content"] = "APPROVE (edited)"
    rejected.append(tampered)
    shifted = deepcopy(row)
    shifted["requested_at"] = "2026-10-13T03:00:05+00:00"   # 현재/임의 시각으로 완료 시각을 만들지 않는다.
    rejected.append(shifted)
    fabricated = deepcopy(row)
    fabricated["candidate"]["attempts"].append(dict(fabricated["candidate"]["attempts"][0],
                                                    attempt_id=pair_id + ":bull_candidate:9"))
    rejected.append(fabricated)
    token = deepcopy(row)
    token["current"]["attempts"][0]["reservation_token"] = "f" * 32
    rejected.append(token)
    hidden = deepcopy(row)
    hidden["current"]["attempts"] = [a for a in hidden["current"]["attempts"] if not a["role"].startswith("bull")]
    hidden["candidate"]["attempts"] = [a for a in hidden["candidate"]["attempts"] if not a["role"].startswith("bull_c")]
    rejected.append(hidden)
    for bad in rejected:
        with pytest.raises(ValueError):
            recovery.restore_pair_result(guard, pair_id, bad, manifest_json=manifest, evidence_path=_journal(path))
    with pytest.raises(ValueError):
        recovery.restore_pair_result(guard, pair_id, row, manifest_json=manifest.replace("decision-1", "decision-2"), evidence_path=_journal(path))
    assert _stored_pair(path, pair_id)[2] is None

    restored = recovery.restore_pair_result(guard, pair_id, row, manifest_json=manifest, evidence_path=_journal(path))
    assert restored["restored"] is True
    assert json.loads(_stored_pair(path, pair_id)[2]) == row
    assert recovery.restore_pair_result(guard, pair_id, row, manifest_json=manifest, evidence_path=_journal(path))["restored"] is False
    with pytest.raises(ValueError):
        recovery.restore_pair_result(guard, pair_id, tampered, manifest_json=manifest, evidence_path=_journal(path))
    calls = (len(openai.calls), len(gemini.calls))
    assert asyncio.run(runner.run(spec)) == row
    assert (len(openai.calls), len(gemini.calls)) == calls
    assert recovery.inspect_pair(guard, pair_id)["complete"] is True
    with pytest.raises(ValueError):
        recovery.export_incomplete_pair(guard, pair_id)


def _mutations(row, pair_id):
    """원래 행에서 한 값씩 바꾼 반례. 모두 복구가 거부해야 한다(P1 #2)."""
    from copy import deepcopy
    out = {}

    def case(name, fn):
        bad = deepcopy(row)
        fn(bad)
        out[name] = bad
    case("decision_available_at", lambda r: r["current"].__setitem__("decision_available_at", "2026-10-13T03:00:09+00:00"))
    case("attempt_available_at_unrecorded", lambda r: r["candidate"]["attempts"][0].__setitem__(
        "available_at", "2026-10-13T03:00:09+00:00"))
    case("total_cost", lambda r: r.__setitem__("total_comparison_cost_usd", "0"))
    case("arm_cost", lambda r: r["current"].__setitem__("api_cost_usd", "0"))
    case("attempt_counts", lambda r: r.__setitem__("actual_provider_attempts", 0))
    case("unknown_count", lambda r: r.__setitem__("unknown_transmission_attempts", 1))
    case("admitted_count", lambda r: r.__setitem__("admitted_attempts", 9))
    case("economic", lambda r: r.update(economic_evaluable=True, net_delta_krw=12345))
    case("actual_execution", lambda r: r.__setitem__("actual_execution", True))
    case("evaluation_at", lambda r: r.__setitem__("evaluation_at", "2026-10-13T04:00:00+00:00"))
    case("evaluation_anchor", lambda r: r.__setitem__("evaluation_anchor", "input_available_at"))
    case("queue_delay", lambda r: r["current"].__setitem__("queue_delay_ms", 1.0))
    case("policy", lambda r: r["policy"].__setitem__("horizon_seconds", 60))
    # error 키를 붙이면 검사를 건너뛰던 결함: 내용·성공·비용을 바꾼 성공 시도
    case("error_key_skip", lambda r: r["current"]["attempts"][0].update(error="x", content="APPROVE", success=True))
    case("error_key_only", lambda r: r["current"]["attempts"][0].__setitem__("error", "x"))
    case("extra_key", lambda r: r["current"]["attempts"][0].__setitem__("injected", True))
    case("role", lambda r: r["current"]["attempts"][0].__setitem__("role", "bear"))
    return out


@pytest.mark.parametrize("name", ["decision_available_at", "attempt_available_at_unrecorded", "total_cost", "arm_cost",
                                  "attempt_counts", "unknown_count", "admitted_count", "economic", "actual_execution",
                                  "evaluation_at", "evaluation_anchor", "queue_delay", "policy", "error_key_skip",
                                  "error_key_only", "extra_key", "role"])
def test_restore_rejects_each_mutated_row_even_when_mutation_is_in_evidence(tmp_path, name):
    """행 수준 재계산·시도 원본 비교·증거 일치 중 하나라도 맞지 않으면 쓰지 않는다."""
    from src.analytics import model_comparison_recovery as recovery
    m, path, ledger, guard, runner, spec, openai, gemini, row = _unpersisted_pair(tmp_path)
    manifest = _stored_pair(path, row["pair_id"])[0]
    bad = _mutations(row, row["pair_id"])[name]
    with pytest.raises(ValueError, match="result_row_not_in_incomplete_evidence"):
        recovery.restore_pair_result(guard, row["pair_id"], bad, manifest_json=manifest, evidence_path=_journal(path))
    _write_evidence(path, bad)          # 증거 파일까지 바꾼 경우에도 재계산 가능한 값은 막는다
    if name not in ("decision_available_at", "attempt_available_at_unrecorded"):
        with pytest.raises(ValueError):
            recovery.restore_pair_result(guard, row["pair_id"], bad, manifest_json=manifest,
                                         evidence_path=_journal(path))
    assert _stored_pair(path, row["pair_id"])[2] is None or name in (
        "decision_available_at", "attempt_available_at_unrecorded")


def test_restore_requires_journal_evidence_and_then_serves_cache_without_provider_calls(tmp_path):
    from src.analytics import model_comparison_recovery as recovery
    m, path, ledger, guard, runner, spec, openai, gemini, row = _unpersisted_pair(tmp_path)
    manifest = _stored_pair(path, row["pair_id"])[0]
    empty = tmp_path / "empty.jsonl"
    empty.write_text('{"not json\n')
    for evidence in (tmp_path / "missing.jsonl", empty):
        with pytest.raises(ValueError, match="result_row_not_in_incomplete_evidence"):
            recovery.restore_pair_result(guard, row["pair_id"], row, manifest_json=manifest, evidence_path=evidence)
    with pytest.raises(TypeError):
        recovery.restore_pair_result(guard, row["pair_id"], row, manifest_json=manifest)   # evidence_path 필수
    assert recovery.restore_pair_result(guard, row["pair_id"], row, manifest_json=manifest,
                                        evidence_path=_journal(path))["restored"] is True
    calls = (len(openai.calls), len(gemini.calls))
    assert asyncio.run(runner.run(spec)) == row
    assert (len(openai.calls), len(gemini.calls)) == calls


def test_failed_attempt_row_restores_only_with_recomputed_error_fields(tmp_path):
    """저장 응답이 있는 실패 시도(정산 실패)는 러너의 같은 실패 함수로 재계산해 비교한다 — 건너뛰지 않는다."""
    from copy import deepcopy
    from src.analytics import model_comparison_recovery as recovery
    from src.utils.llm_budget import BudgetStateError
    path = tmp_path / "b.db"
    m, ledger, guard, runner, spec, openai, gemini = _runner(path)
    original = ledger.settle
    first = []

    def fail_once(token, **kwargs):
        if not first:
            first.append(token)
            ledger.abandon(token)
            raise BudgetStateError("storage_unavailable")
        return original(token, **kwargs)
    ledger.settle = fail_once
    _failing_final_write(path)
    with pytest.raises(m.PairResultPersistenceError) as raised:
        asyncio.run(runner.run(spec))
    _allow_final_write(path)
    row = raised.value.result
    attempts = [a for arm in ("current", "candidate") for a in row[arm]["attempts"]]
    failed = [a for a in attempts if a["ledger_state"] == "pending_reconciliation"]
    assert failed and failed[0]["error"] and failed[0]["provider_success"] is True
    # 같은 행의 뒤 시도는 미해결 예약 때문에 전송 전 차단(저장 응답 없음) — 그 경로도 복구 검사를 거친다.
    assert any(a["ledger_state"] == "blocked" and a["reservation_token"] is None and a["api_cost_usd"] == "0"
               for a in attempts)
    _write_evidence(path, row)
    manifest = _stored_pair(path, row["pair_id"])[0]
    for change in ({"success": True}, {"content": "edited"}, {"api_cost_usd": "0"}, {"provider_success": False}):
        bad = deepcopy(row)
        for arm in ("current", "candidate"):
            for attempt in bad[arm]["attempts"]:
                if attempt["attempt_id"] == failed[0]["attempt_id"]:
                    attempt.update(change)
        for arm in ("current", "candidate"):
            bad[arm]["api_cost_usd"] = m._sum_cost(bad[arm]["attempts"])
        bad["total_comparison_cost_usd"] = m._sum_cost(list({a["attempt_id"]: a for arm in ("current", "candidate")
                                                             for a in bad[arm]["attempts"]}.values()))
        _write_evidence(path, bad)
        with pytest.raises(ValueError, match="result_row_response_mismatch"):
            recovery.restore_pair_result(guard, row["pair_id"], bad, manifest_json=manifest, evidence_path=_journal(path))
    # 저장 응답이 없는 시도: 원장 차단 사유·상태와 행의 error·ledger_state 가 정확히 같아야 한다.
    blocked_id = next(a["attempt_id"] for a in attempts if a["ledger_state"] == "blocked")
    for change, reason in (({"error": "comparison_daily_attempt_cap"}, "result_row_error_mismatch"),
                           ({"error": "TimeoutError"}, "result_row_error_mismatch"),
                           ({"ledger_state": "known"}, "result_row_ledger_state_mismatch"),
                           ({"request_sent": None}, "result_row_(derived|response)_mismatch")):
        bad = deepcopy(row)
        for arm in ("current", "candidate"):
            for attempt in bad[arm]["attempts"]:
                if attempt["attempt_id"] == blocked_id:
                    attempt.update(change)
        _write_evidence(path, bad)
        with pytest.raises(ValueError, match=reason):
            recovery.restore_pair_result(guard, row["pair_id"], bad, manifest_json=manifest, evidence_path=_journal(path))
    assert recovery.restore_pair_result(guard, row["pair_id"], row, manifest_json=manifest,
                                        evidence_path=_journal(path))["restored"] is True


def test_unknown_pair_cost_reconciled_then_restored_keeps_original_states(tmp_path):
    from src.analytics import model_comparison_recovery as recovery
    from src.utils.llm_budget_reconciliation import reconcile_budget
    m, path, ledger, guard, runner, spec, openai, gemini, row = _unpersisted_pair(tmp_path, unknown=True)
    before = ledger.status()["activated_at"]
    with sqlite3.connect(path) as db:
        unknown = [r[0] for r in db.execute("SELECT token FROM reservations WHERE state='unknown'")]
    assert unknown
    evidence = tmp_path / "evidence.txt"
    evidence.write_text("synthetic usage export")
    review = _review(ledger, evidence, unknown)
    preview = reconcile_budget(path, 100, review, evidence)
    reconcile_budget(path, 100, review, evidence, apply=True, expected_state_sha256=preview["state_sha256"])
    states = {a["attempt_id"]: a["state"] for a in recovery.inspect_pair(guard, row["pair_id"])["attempts"]}
    assert "known_reconciled" in states.values()
    manifest = _stored_pair(path, row["pair_id"])[0]
    assert recovery.restore_pair_result(guard, row["pair_id"], row, manifest_json=manifest, evidence_path=_journal(path))["restored"]
    restored = json.loads(_stored_pair(path, row["pair_id"])[2])
    # 복구는 당시 상태를 보존한다. 대사 결과는 inspect 로 별도 확인한다.
    assert "known_reconciled" not in {a["ledger_state"] for arm in ("current", "candidate")
                                      for a in restored[arm]["attempts"]}
    assert ledger.status()["activated_at"] == before


def test_restore_retries_short_lock_but_gives_up_on_long_lock_without_writing(tmp_path):
    from src.analytics import model_comparison_recovery as recovery
    m, path, ledger, guard, runner, spec, openai, gemini, row = _unpersisted_pair(tmp_path)
    manifest = _stored_pair(path, row["pair_id"])[0]
    locked, release = threading.Event(), threading.Event()

    def hold(seconds):
        with sqlite3.connect(path, isolation_level=None) as db:
            db.execute("BEGIN IMMEDIATE")
            locked.set()
            release.wait(seconds)
            db.execute("ROLLBACK")

    worker = threading.Thread(target=hold, args=(5,))
    worker.start()
    assert locked.wait(2)
    started = time.monotonic()
    try:
        with pytest.raises(BudgetBlocked, match="comparison_pair_storage_unavailable"):
            recovery.restore_pair_result(guard, row["pair_id"], row, manifest_json=manifest, evidence_path=_journal(path))
    finally:
        elapsed = time.monotonic() - started
        release.set()
        worker.join(2)
    assert elapsed < 2.5 and _stored_pair(path, row["pair_id"])[2] is None

    locked.clear()
    release.clear()
    worker = threading.Thread(target=hold, args=(0.2,))
    worker.start()
    assert locked.wait(2)
    try:
        assert recovery.restore_pair_result(guard, row["pair_id"], row, manifest_json=manifest, evidence_path=_journal(path))["restored"]
    finally:
        worker.join(2)
    assert json.loads(_stored_pair(path, row["pair_id"])[2]) == row


def _crash_pair(path):
    m, ledger, guard, runner, spec, openai, gemini = _runner(path, crash=True)
    asyncio.run(runner.run(spec))


def test_process_loss_leaves_inspectable_incomplete_pair_without_retry(tmp_path):
    import multiprocessing
    from src.analytics import model_comparison_recovery as recovery
    path = tmp_path / "b.db"
    MonthlyBudgetLedger(path, 100, clock=lambda: NOW, policy_enabled=True).close()
    activated = MonthlyBudgetLedger(path, 100).status()["activated_at"]
    process = multiprocessing.get_context("fork").Process(target=_crash_pair, args=(path,))
    process.start()
    process.join(10)
    if process.is_alive():
        process.terminate()
        process.join(5)
        pytest.fail("격리된 자식 프로세스가 종료되지 않았다")
    m, ledger, guard, runner, spec, openai, gemini = _runner(path)
    with sqlite3.connect(path) as db:
        pair_id = db.execute("SELECT pair_id FROM comparison_pairs").fetchone()[0]
    inspected = recovery.inspect_pair(guard, pair_id)
    assert not inspected["complete"] and "pending" in {a["state"] for a in inspected["attempts"]}
    diagnostic = recovery.export_incomplete_pair(guard, pair_id)
    assert diagnostic["diagnostic_only"] and any(r.endswith(":pending") for r in diagnostic["reasons"])
    with pytest.raises(BudgetBlocked, match="comparison_pair_incomplete_no_automatic_retry"):
        asyncio.run(runner.run(spec))
    assert openai.calls == [] and gemini.calls == []
    assert ledger.status()["unresolved_count"] >= 1
    assert ledger.status()["activated_at"] == activated
    assert recovery.inspect_attempt(guard, "missing")["found"] is False
