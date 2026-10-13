"""Committed reservation acknowledgement loss and audited rejection recovery."""
import asyncio
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path
import sqlite3

import pytest

from src.analytics.model_comparison_daily_report import build_daily_report
from src.analytics.model_comparison_execution import ComparisonAttemptResult, ComparisonCallGuard
from src.utils.llm_budget import BudgetBlocked, BudgetStateError, MonthlyBudgetLedger
from src.utils.llm_budget_reconciliation import reconcile_budget
from test_model_comparison_admission_budget import _review
from test_model_comparison_execution import NOW, setup_guard, request


def reconcile_zero(ledger, tmp_path, token):
    evidence = tmp_path / "usage.txt"
    evidence.write_text("synthetic zero-charge provider usage export")
    review = _review(ledger, evidence, [token], cost=0)
    preview = reconcile_budget(ledger.path, 100, review, evidence)
    reconcile_budget(ledger.path, 100, review, evidence, apply=True,
                     expected_state_sha256=preview["state_sha256"])


@pytest.mark.parametrize("committed", [False, True])
def test_reserve_commit_error_preserves_link_only_when_durable(tmp_path, monkeypatch, committed):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    ledger, policy, guard = setup_guard(tmp_path)
    connect = ledger._connect
    injected, calls = [], []

    class Connection:
        def __init__(self, db):
            self.db, self.inserted = db, False

        def __getattr__(self, name):
            return getattr(self.db, name)

        def execute(self, sql, *args):
            if sql == "COMMIT" and self.inserted and not injected:
                injected.append(True)
                if committed:
                    self.db.execute(sql, *args)
                raise sqlite3.OperationalError("injected reservation COMMIT failure")
            result = self.db.execute(sql, *args)
            if sql.startswith("INSERT INTO reservations"):
                self.inserted = True
            return result

    @contextmanager
    def wrapped(*args, **kwargs):
        with connect(*args, **kwargs) as db:
            yield Connection(db)

    async def dispatch(req):
        calls.append(req)
        return ComparisonAttemptResult("approve", Decimal("0.0001"))

    monkeypatch.setattr(ledger, "_connect", wrapped)
    with pytest.raises(BudgetBlocked):
        asyncio.run(guard.call(request(), dispatch=dispatch, attempt_id="commit-error"))
    assert injected and not calls
    with connect(readonly=True) as db:
        reservation = db.execute("SELECT token,state FROM reservations").fetchone()
        attempt = db.execute("SELECT reservation_token FROM comparison_requests").fetchone()
        slot = db.execute("SELECT reservation_token,state FROM comparison_attempts").fetchone()
    if not committed:
        assert reservation is None
        assert attempt["reservation_token"] is None and slot["reservation_token"] is None
        assert slot["state"] == "blocked"
        return
    assert reservation["state"] == "reserved"  # No inferred release or automatic settlement.
    assert attempt["reservation_token"] == slot["reservation_token"] == reservation["token"]
    assert slot["state"] == "unknown"
    ledger.close()
    reopened = MonthlyBudgetLedger(ledger.path, 100, clock=lambda: NOW, policy_enabled=True)
    restarted = ComparisonCallGuard(ledger=reopened, policy=policy)
    with pytest.raises(BudgetBlocked):
        asyncio.run(restarted.call(request(), dispatch=dispatch, attempt_id="commit-error"))
    reconcile_zero(reopened, tmp_path, reservation["token"])
    assert restarted.lookup("commit-error")["state"] == "known_reconciled"
    assert not reopened.status()["blocked"]
    report = build_daily_report([], day_kst=reopened.today().isoformat(),
                                ledger_attempts=restarted.day_attempts(reopened.today().isoformat()))
    assert report["spend"]["total_usd"] == "0"
    with pytest.raises(BudgetBlocked):
        asyncio.run(restarted.call(request(), dispatch=dispatch, attempt_id="commit-error"))
    assert not calls  # A cost audit never authorizes retransmission of a missing response.


def test_rejected_response_settlement_failure_recovers_from_verified_audit(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    ledger, policy, guard = setup_guard(tmp_path)
    calls = []
    original = ComparisonAttemptResult({"success": False, "request_sent": True, "api_cost_usd": "0",
                                       "billing_status": "rejected_before_generation"},
                                      Decimal("0"), "rejected_before_generation")

    async def dispatch(req):
        calls.append(req)
        return original

    def failed_settlement(token, **kwargs):
        ledger.abandon(token)
        raise BudgetStateError("storage_unavailable")

    monkeypatch.setattr(ledger, "settle", failed_settlement)
    with pytest.raises(BudgetBlocked):
        asyncio.run(guard.call(request(), dispatch=dispatch, attempt_id="rejected-settle-failed"))
    token = guard.lookup("rejected-settle-failed")["reservation_token"]
    reconcile_zero(ledger, tmp_path, token)
    record = guard.lookup("rejected-settle-failed")
    assert record["state"] == "known_reconciled"
    assert record["result"] == original and record["original_estimated_cost_usd"] == "0"
    report = build_daily_report([], day_kst=ledger.today().isoformat(),
                                ledger_attempts=guard.day_attempts(ledger.today().isoformat()))
    assert report["spend"]["total_usd"] == "0"
    assert asyncio.run(guard.call(request(), dispatch=dispatch, attempt_id="rejected-settle-failed")) == original
    assert len(calls) == 1


@pytest.mark.parametrize("operation", ["request", "slot"])
@pytest.mark.parametrize("supplied", [None, "different-reservation"])
def test_failure_bookkeeping_cannot_erase_or_replace_durable_reservation(tmp_path, operation, supplied):
    ledger, _, guard = setup_guard(tmp_path)

    async def dispatch(req):
        return ComparisonAttemptResult("approve", Decimal("0.0001"))

    asyncio.run(guard.call(request(), dispatch=dispatch, attempt_id="durable"))
    with ledger._connect(readonly=True) as db:
        row = db.execute("SELECT slot,reservation_token FROM comparison_requests").fetchone()
        token, slot = row["reservation_token"], row["slot"]

    def record():
        if operation == "request":
            guard._mark_failed("durable", supplied, "late_failure")
        else:
            guard._record(slot, supplied, "unknown")

    if supplied is None:
        record()
    else:
        with pytest.raises(BudgetStateError, match="comparison_reservation_mismatch"):
            record()
    with ledger._connect(readonly=True) as db:
        assert db.execute("SELECT reservation_token FROM comparison_requests").fetchone()[0] == token
        assert db.execute("SELECT reservation_token FROM comparison_attempts").fetchone()[0] == token
    assert guard.lookup("durable")["result"].value == "approve"
    assert guard.lookup("durable")["state"] == "known"
