"""안정 시도 ID의 중복 전송 방지·JSON 응답 복구를 임시 원장으로 검증한다."""
import asyncio
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
import sqlite3

import pytest

from src.analytics.model_comparison_execution import (
    ComparisonAttemptResult, ComparisonCallGuard, ComparisonExecutionPolicy, ComparisonRequest,
)
from src.utils.llm_budget import BudgetBlocked, BudgetStateError, MonthlyBudgetLedger

NOW = datetime(2026, 10, 13, 3, tzinfo=timezone.utc)


def setup(tmp_path):
    ledger = MonthlyBudgetLedger(tmp_path / "budget.db", 100, clock=lambda: NOW, policy_enabled=True)
    policy = ComparisonExecutionPolicy("identity-study", 10, ("gpt-5-mini",), 400, "1", "2026-10-13", "2026-10-31", 10, 16384)
    return ledger, policy, ComparisonCallGuard(ledger=ledger, policy=policy)


def request():
    return ComparisonRequest("gpt-5-mini", "facts", "role", 100)


def test_completed_id_recovers_json_result_and_does_not_consume_another_slot(tmp_path):
    ledger, policy, guard = setup(tmp_path)
    calls = []

    async def dispatch(req):
        calls.append(req)
        return ComparisonAttemptResult({"answer": "approve", "tokens": [1, 2]}, Decimal("0.0001"))

    expected = asyncio.run(guard.call(request(), dispatch=dispatch, attempt_id="decision/bull/current/1"))
    reopened = MonthlyBudgetLedger(ledger.path, 100, clock=lambda: NOW, policy_enabled=True)
    restored = ComparisonCallGuard(ledger=reopened, policy=policy)
    recovered = asyncio.run(restored.call(request(), dispatch=dispatch, attempt_id="decision/bull/current/1"))
    assert recovered == expected
    assert len(calls) == 1
    assert restored.lookup("decision/bull/current/1")["state"] == "known"
    assert reopened.status()["spent_usd"] == 0.0001
    with sqlite3.connect(ledger.path) as db:
        assert db.execute("SELECT COUNT(*) FROM comparison_attempts").fetchone()[0] == 1


def test_concurrent_duplicate_is_rejected_while_first_is_in_provider(tmp_path):
    ledger, policy, guard = setup(tmp_path)
    calls = []

    async def run():
        entered, release = asyncio.Event(), asyncio.Event()

        async def dispatch(req):
            calls.append(req)
            entered.set()
            await release.wait()
            return ComparisonAttemptResult("answer", 0.0001)

        first = asyncio.create_task(guard.call(request(), dispatch=dispatch, attempt_id="same"))
        await entered.wait()
        try:
            with pytest.raises(BudgetBlocked, match="comparison_attempt_pending"):
                await guard.call(request(), dispatch=dispatch, attempt_id="same")
        finally:
            release.set()
        await first

    asyncio.run(run())
    assert len(calls) == 1


@pytest.mark.parametrize("change", [
    {"prompt": "changed"}, {"system": "changed"}, {"max_output_tokens": 101},
    {"reasoning_effort": "low"}, {"temperature": 0.0},
])
def test_same_id_changed_request_controls_never_dispatch(tmp_path, change):
    ledger, policy, guard = setup(tmp_path)
    calls = []

    async def dispatch(req):
        calls.append(req)
        return ComparisonAttemptResult("answer", 0.0001)

    asyncio.run(guard.call(request(), dispatch=dispatch, attempt_id="same"))
    with pytest.raises(BudgetBlocked, match="comparison_attempt_mismatch"):
        asyncio.run(guard.call(replace(request(), **change), dispatch=dispatch, attempt_id="same"))
    assert len(calls) == 1


def test_post_settlement_forensic_record_failure_does_not_lose_response(tmp_path, monkeypatch):
    ledger, policy, guard = setup(tmp_path)
    calls = []

    def failed_record(*args):
        raise BudgetStateError("comparison_storage_unavailable")

    async def dispatch(req):
        calls.append(req)
        return ComparisonAttemptResult({"answer": "approve"}, "0.0001")

    monkeypatch.setattr(guard, "_record", failed_record)
    result = asyncio.run(guard.call(request(), dispatch=dispatch, attempt_id="paid"))
    assert result.value == {"answer": "approve"}
    assert asyncio.run(guard.call(request(), dispatch=dispatch, attempt_id="paid")) == result
    assert len(calls) == 1
    assert ledger.status()["settled_count"] == 1


def test_unknown_completed_result_is_recoverable_but_new_ids_remain_blocked(tmp_path):
    ledger, policy, guard = setup(tmp_path)
    calls = []

    async def dispatch(req):
        calls.append(req)
        return ComparisonAttemptResult({"answer": "unverified"}, None)

    result = asyncio.run(guard.call(request(), dispatch=dispatch, attempt_id="unknown"))
    assert asyncio.run(guard.call(request(), dispatch=dispatch, attempt_id="unknown")) == result
    with pytest.raises(BudgetBlocked, match="unresolved_usage"):
        asyncio.run(guard.call(request(), dispatch=dispatch, attempt_id="next"))
    assert len(calls) == 1 and ledger.status()["unresolved_count"] == 1


def test_cancellation_and_restart_do_not_retry_interrupted_id(tmp_path):
    ledger, policy, guard = setup(tmp_path)
    calls = []

    async def dispatch(req):
        calls.append(req)
        raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(guard.call(request(), dispatch=dispatch, attempt_id="cancelled"))
    restored = ComparisonCallGuard(ledger=MonthlyBudgetLedger(ledger.path, 100, clock=lambda: NOW, policy_enabled=True), policy=policy)
    with pytest.raises(BudgetBlocked):
        asyncio.run(restored.call(request(), dispatch=dispatch, attempt_id="cancelled"))
    assert len(calls) == 1
    assert ledger.status()["unresolved_count"] == 1


def test_settlement_failure_is_pending_reconciliation_without_blind_retry(tmp_path, monkeypatch):
    ledger, policy, guard = setup(tmp_path)
    calls, settlements = [], []

    async def dispatch(req):
        calls.append(req)
        return ComparisonAttemptResult("answer", Decimal("0.0001"))

    def failed_settlement(token, **kwargs):
        settlements.append(token)
        ledger.abandon(token)
        raise BudgetStateError("storage_unavailable")

    monkeypatch.setattr(ledger, "settle", failed_settlement)
    with pytest.raises(BudgetBlocked):
        asyncio.run(guard.call(request(), dispatch=dispatch, attempt_id="pending"))
    assert guard.lookup("pending")["state"] == "pending_reconciliation"
    assert guard.lookup("pending")["result"].value == "answer"
    with pytest.raises(BudgetBlocked, match="comparison_pending_reconciliation"):
        asyncio.run(guard.call(request(), dispatch=dispatch, attempt_id="pending"))
    assert len(calls) == len(settlements) == 1
    assert ledger.status()["unresolved_count"] == 1


@pytest.mark.parametrize("bad", [0, -1, True, float("nan"), float("inf"), "12"])
def test_timeout_is_positive_finite_and_bound_to_attempt(tmp_path, bad):
    with pytest.raises(ValueError, match="timeout_seconds"):
        replace(request(), timeout_seconds=bad)


def test_same_id_timeout_change_is_rejected(tmp_path):
    ledger, policy, guard = setup(tmp_path)
    calls = []

    async def dispatch(req):
        calls.append(req)
        return ComparisonAttemptResult("answer", 0.0001)

    asyncio.run(guard.call(request(), dispatch=dispatch, attempt_id="timeout"))
    with pytest.raises(BudgetBlocked, match="comparison_attempt_mismatch"):
        asyncio.run(guard.call(replace(request(), timeout_seconds=1.0), dispatch=dispatch, attempt_id="timeout"))
    assert len(calls) == 1


@pytest.mark.parametrize("after_commit", [False, True])
def test_result_storage_failure_never_redispatches_and_preserves_known_charge(tmp_path, monkeypatch, after_commit):
    ledger, policy, guard = setup(tmp_path)
    original = guard._store_result
    calls = []

    def fail_store(*args):
        if after_commit:
            original(*args)
        raise BudgetStateError("comparison_storage_unavailable")

    async def dispatch(req):
        calls.append(req)
        return ComparisonAttemptResult({"responded_at": "2026-10-13T03:00:00+00:00"}, 0.0001)

    monkeypatch.setattr(guard, "_store_result", fail_store)
    with pytest.raises(BudgetBlocked):
        asyncio.run(guard.call(request(), dispatch=dispatch, attempt_id="store-error"))
    assert ledger.status()["spent_usd"] == 0.0001
    restored = ComparisonCallGuard(ledger=MonthlyBudgetLedger(ledger.path, 100, clock=lambda: NOW, policy_enabled=True), policy=policy)
    if after_commit:
        result = asyncio.run(restored.call(request(), dispatch=dispatch, attempt_id="store-error"))
        assert result.value["responded_at"] == "2026-10-13T03:00:00+00:00"
    else:
        with pytest.raises(BudgetBlocked):
            asyncio.run(restored.call(request(), dispatch=dispatch, attempt_id="store-error"))
    assert len(calls) == 1


def test_settlement_commit_ack_loss_recovers_from_durable_money_evidence(tmp_path, monkeypatch):
    ledger, policy, guard = setup(tmp_path)
    original = ledger.settle
    calls, settlements = [], []

    async def dispatch(req):
        calls.append(req)
        return ComparisonAttemptResult({"original_time": "fixed"}, 0.0001)

    def settle_then_fail(*args, **kwargs):
        settlements.append(args)
        original(*args, **kwargs)
        raise BudgetStateError("storage_unavailable")

    monkeypatch.setattr(ledger, "settle", settle_then_fail)
    with pytest.raises(BudgetBlocked):
        asyncio.run(guard.call(request(), dispatch=dispatch, attempt_id="commit-ack"))
    result = asyncio.run(guard.call(request(), dispatch=dispatch, attempt_id="commit-ack"))
    assert result.value == {"original_time": "fixed"}
    assert guard.lookup("commit-ack")["state"] == "known"
    assert len(calls) == len(settlements) == 1


def _crash_in_provider(path, policy):
    import os
    ledger = MonthlyBudgetLedger(path, 100, clock=lambda: NOW, policy_enabled=True)
    guard = ComparisonCallGuard(ledger=ledger, policy=policy)

    async def dispatch(req):
        os._exit(0)

    asyncio.run(guard.call(request(), dispatch=dispatch, attempt_id="process-lost"))


def test_real_process_loss_rejects_id_after_restart_and_preserves_unknown(tmp_path):
    import multiprocessing
    ledger, policy, guard = setup(tmp_path)
    process = multiprocessing.get_context("fork").Process(target=_crash_in_provider, args=(ledger.path, policy))
    process.start()
    process.join(5)
    if process.is_alive():
        process.terminate()
        process.join(5)
        pytest.fail("격리된 자식 프로세스가 종료되지 않았다")
    assert process.exitcode == 0
    assert ledger.status()["unresolved_count"] == 1
    calls = []

    async def dispatch(req):
        calls.append(req)
        return ComparisonAttemptResult("answer", 0)

    with pytest.raises(BudgetBlocked, match="comparison_attempt_pending"):
        asyncio.run(guard.call(request(), dispatch=dispatch, attempt_id="process-lost"))
    assert calls == []


@pytest.mark.parametrize("field,bad", [
    ("reasoning_effort", True), ("reasoning_effort", " "), ("temperature", True),
    ("temperature", float("nan")), ("temperature", 2.1), ("temperature", -0.1),
    ("seed", True), ("seed", 1.5), ("seed", 2 ** 63), ("seed", -(2 ** 63) - 1),
])
def test_request_controls_are_strict(field, bad):
    with pytest.raises(ValueError, match=field):
        replace(request(), **{field: bad})


def test_seed_is_bound_to_stable_id(tmp_path):
    ledger, policy, guard = setup(tmp_path)
    calls = []

    async def dispatch(req):
        calls.append(req)
        return ComparisonAttemptResult("answer", 0.0001)

    seeded = replace(request(), seed=20260803)
    asyncio.run(guard.call(seeded, dispatch=dispatch, attempt_id="seed"))
    with pytest.raises(BudgetBlocked, match="comparison_attempt_mismatch"):
        asyncio.run(guard.call(replace(seeded, seed=20260804), dispatch=dispatch, attempt_id="seed"))
    assert len(calls) == 1


def test_daily_blocked_id_is_not_automatically_retried_on_next_day(tmp_path):
    now = [NOW]
    ledger = MonthlyBudgetLedger(tmp_path / "budget.db", 100, clock=lambda: now[0], policy_enabled=True)
    policy = ComparisonExecutionPolicy("one-per-day", 1, ("gpt-5-mini",), 400, "1", "2026-10-13", "2026-10-31", 10, 16384)
    guard = ComparisonCallGuard(ledger=ledger, policy=policy)
    calls = []

    async def dispatch(req):
        calls.append(req)
        return ComparisonAttemptResult("answer", 0.0001)

    asyncio.run(guard.call(request(), dispatch=dispatch, attempt_id="first"))
    with pytest.raises(BudgetBlocked, match="comparison_daily_attempt_cap"):
        asyncio.run(guard.call(request(), dispatch=dispatch, attempt_id="blocked"))
    now[0] = datetime(2026, 10, 14, 3, tzinfo=timezone.utc)
    with pytest.raises(BudgetBlocked, match="comparison_daily_attempt_cap"):
        asyncio.run(guard.call(request(), dispatch=dispatch, attempt_id="blocked"))
    assert len(calls) == 1
    assert guard.lookup("blocked")["state"] == "blocked"


def test_real_200ms_result_write_contention_preserves_paid_response(tmp_path):
    import threading
    import time
    ledger, policy, guard = setup(tmp_path)
    locked = threading.Event()
    calls = []

    def hold_write_lock():
        with sqlite3.connect(ledger.path, isolation_level=None) as db:
            db.execute("BEGIN IMMEDIATE")
            locked.set()
            time.sleep(0.2)
            db.execute("ROLLBACK")

    worker = threading.Thread(target=hold_write_lock)

    async def dispatch(req):
        calls.append(req)
        worker.start()
        assert locked.wait(2)
        return ComparisonAttemptResult({"response": "paid", "responded_at": "original"}, "0.0001")

    try:
        result = asyncio.run(guard.call(request(), dispatch=dispatch, attempt_id="busy-response"))
    finally:
        worker.join(2)
    assert not worker.is_alive()
    assert result.value == {"response": "paid", "responded_at": "original"}
    assert asyncio.run(guard.call(request(), dispatch=dispatch, attempt_id="busy-response")) == result
    assert len(calls) == 1
    assert ledger.status()["spent_usd"] == 0.0001
    assert guard.lookup("busy-response")["state"] == "known"


def test_identical_response_write_is_idempotent_but_changed_payload_is_rejected(tmp_path):
    ledger, policy, guard = setup(tmp_path)

    async def dispatch(req):
        return ComparisonAttemptResult("paid", 0.0001)

    asyncio.run(guard.call(request(), dispatch=dispatch, attempt_id="write-twice"))
    with sqlite3.connect(ledger.path) as db:
        token, payload = db.execute("SELECT reservation_token,result_json FROM comparison_requests").fetchone()
    guard._store_result("write-twice", token, payload)
    with pytest.raises(BudgetBlocked, match="comparison_result_mismatch"):
        guard._store_result("write-twice", token, payload.replace('paid', 'changed'))
    assert guard.lookup("write-twice")["result"].value == "paid"


@pytest.mark.parametrize("committed", [False, True])
def test_nonbusy_write_error_is_not_retried_and_commit_ack_is_read_back(tmp_path, monkeypatch, committed):
    ledger, policy, guard = setup(tmp_path)
    original = guard._write_result_once
    writes = []
    calls = []

    def fail_write(*args):
        writes.append(args)
        if committed:
            original(*args)
        error = sqlite3.OperationalError("simulated I/O acknowledgement failure")
        error.sqlite_errorcode = sqlite3.SQLITE_IOERR
        raise error

    async def dispatch(req):
        calls.append(req)
        return ComparisonAttemptResult({"response": "paid"}, 0.0001)

    monkeypatch.setattr(guard, "_write_result_once", fail_write)
    if committed:
        result = asyncio.run(guard.call(request(), dispatch=dispatch, attempt_id="io-error"))
        assert result.value == {"response": "paid"}
        assert guard.lookup("io-error")["state"] == "known"
    else:
        with pytest.raises(BudgetBlocked, match="comparison_storage_unavailable"):
            asyncio.run(guard.call(request(), dispatch=dispatch, attempt_id="io-error"))
        assert guard.lookup("io-error")["state"] == "blocked"
    assert len(writes) == len(calls) == 1
    assert ledger.status()["spent_usd"] == 0.0001


def test_long_writer_contention_stops_at_bounded_attempts_without_redispatch(tmp_path, monkeypatch):
    import threading
    import time
    ledger, policy, guard = setup(tmp_path)
    locked, release = threading.Event(), threading.Event()
    original = guard._write_result_once
    writes, calls = [], []

    def hold_write_lock():
        with sqlite3.connect(ledger.path, isolation_level=None) as db:
            db.execute("BEGIN IMMEDIATE")
            locked.set()
            release.wait(5)
            db.execute("ROLLBACK")

    def observed_write(*args):
        writes.append(args)
        return original(*args)

    worker = threading.Thread(target=hold_write_lock)

    async def dispatch(req):
        calls.append(req)
        worker.start()
        assert locked.wait(2)
        return ComparisonAttemptResult("paid", 0.0001)

    monkeypatch.setattr(guard, "_write_result_once", observed_write)
    started = time.monotonic()
    try:
        with pytest.raises(BudgetBlocked, match="comparison_storage_unavailable"):
            asyncio.run(guard.call(request(), dispatch=dispatch, attempt_id="long-busy"))
    finally:
        elapsed = time.monotonic() - started
        release.set()
        worker.join(2)
    assert 1 <= len(writes) <= 10
    assert elapsed < 2.5  # 응답 쓰기 약1초와 기존 정산/실패기록 각100ms를 포함한 여유.
    assert ledger.status()["unresolved_count"] == 1
    with pytest.raises(BudgetBlocked):
        asyncio.run(guard.call(request(), dispatch=dispatch, attempt_id="long-busy"))
    assert len(calls) == 1


def test_generic_connection_error_is_not_evidence_of_zero_spend(tmp_path):
    ledger, policy, guard = setup(tmp_path)

    async def dispatch(req):
        raise ConnectionError("no verified not-sent evidence")

    with pytest.raises(ConnectionError):
        asyncio.run(guard.call(request(), dispatch=dispatch, attempt_id="connection-error"))
    assert ledger.status()["unresolved_count"] == 1
    assert ledger.status()["block_reason"] == "unresolved_usage"
