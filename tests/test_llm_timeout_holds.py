"""Offline timeout liability accounting using only temporary SQLite ledgers."""
import hashlib
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import pytest

from src.utils.llm_budget import BudgetBlocked, MonthlyBudgetLedger
from src.utils.llm_budget_reconciliation import reconcile_budget

CONTRACT = 'gemini-flash-lite-standard-text-v1'
MODEL = 'gemini-3.1-flash-lite'
CEILING = 0.262912  # Full 1,048,576 input tokens + 512 output/thinking tokens.


def ledger_at(tmp_path, limit=1, **kwargs):
    return MonthlyBudgetLedger(tmp_path / 'budget.sqlite3', limit, **kwargs)


def certified(ledger, cap=512, **kwargs):
    return ledger.reserve(MODEL, 'synthetic', '', cap, timeout_hold_contract=CONTRACT, **kwargs)


def test_timeout_retains_null_actual_and_full_bound_and_admits_independent_fallback(tmp_path):
    ledger = ledger_at(tmp_path)
    token = certified(ledger)
    assert ledger.status()['reserved_usd'] == CEILING
    assert ledger.settle(token, outcome='timeout') == CEILING
    with sqlite3.connect(ledger.path) as db:
        assert db.execute('SELECT state,actual_units,reserved_units FROM reservations WHERE token=?',
                          (token,)).fetchone() == ('unknown', None, 262912000)
    status = ledger.status()
    assert status['spent_usd'] == status['reserved_usd'] == 0
    assert status['uncertain_cost_ceiling_usd'] == CEILING
    assert status['bounded_unresolved_count'] == status['unresolved_count'] == 1
    assert status['unbounded_unresolved_count'] == 0
    assert status['available_usd'] == pytest.approx(1 - CEILING)
    fallback = ledger.reserve('gpt-5-mini', 'synthetic', '', 100)
    assert fallback != token
    ledger.settle(fallback, outcome='not_sent')


@pytest.mark.parametrize('contract,model,cap', [('invalid', MODEL, 512), (CONTRACT, 'gpt-5-mini', 512),
    (CONTRACT, MODEL, 65537), (CONTRACT, MODEL, True), (CONTRACT, MODEL, 0)])
def test_invalid_contract_cannot_create_a_reservation(tmp_path, contract, model, cap):
    ledger = ledger_at(tmp_path)
    with pytest.raises(BudgetBlocked):
        ledger.reserve(model, 'p', '', cap, timeout_hold_contract=contract)
    assert ledger.status()['reserved_usd'] == 0


def test_retired_preview_cannot_be_newly_admitted(tmp_path):
    ledger = ledger_at(tmp_path)
    with pytest.raises(BudgetBlocked, match='retired_model'):
        ledger.reserve('gemini-3.1-flash-lite-preview', 'p', '', 10)
    with sqlite3.connect(ledger.path) as db:
        assert db.execute('SELECT count(*) FROM reservations').fetchone()[0] == 0


@pytest.mark.parametrize('kind', ['legacy', 'uncertified_timeout', 'certified_unknown', 'orphan'])
def test_non_timeout_or_uncertified_uncertainty_still_blocks(tmp_path, kind):
    ledger = ledger_at(tmp_path)
    token = certified(ledger) if kind in {'certified_unknown', 'orphan'} else ledger.reserve(MODEL, 'p', '', 512)
    if kind == 'orphan':
        ledger.abandon(token)
    else:
        assert ledger.settle(token, outcome='timeout' if kind == 'uncertified_timeout' else 'unknown') is None
    status = ledger.status()
    assert status['unresolved_count'] == status['unbounded_unresolved_count'] == 1
    assert status['bounded_unresolved_count'] == 0
    assert status['available_usd'] is None
    with pytest.raises(BudgetBlocked, match='unresolved_usage'):
        ledger.reserve('gpt-5-mini', 'p', '', 100)


def test_hold_carries_across_restart_and_month_and_status_is_readonly(tmp_path):
    now = [datetime(2026, 10, 31, 14, 59, tzinfo=timezone.utc)]
    ledger = ledger_at(tmp_path, clock=lambda: now[0])
    token = certified(ledger)
    ledger.settle(token, outcome='timeout')
    ledger.close()
    now[0] = datetime(2027, 1, 1, tzinfo=timezone.utc)
    reopened = ledger_at(tmp_path, clock=lambda: now[0])
    before = reopened.path.read_bytes()
    status = reopened.status()
    assert status['month'] == '2027-01'
    assert status['uncertain_cost_ceiling_usd'] == CEILING
    assert status['available_usd'] == pytest.approx(1 - CEILING)
    assert before == reopened.path.read_bytes()


def test_certified_inflight_request_keeps_room_for_timeout_across_month_boundary(tmp_path):
    now = [datetime(2026, 10, 31, 14, 59, tzinfo=timezone.utc)]
    ledger = ledger_at(tmp_path, limit=0.5, clock=lambda: now[0])
    original = certified(ledger)
    now[0] = datetime(2026, 10, 31, 15, 0, tzinfo=timezone.utc)
    # The first request can still become an all-month uncertain hold. Reusing
    # that room at midnight would admit two 0.262912 liabilities into USD0.5.
    with pytest.raises(BudgetBlocked, match='monthly_limit_exceeded'):
        certified(ledger)
    ledger.settle(original, outcome='timeout')
    assert ledger.status()['committed_usd'] == CEILING


def test_full_ceiling_must_fit_even_when_prompt_tiny(tmp_path):
    ledger = ledger_at(tmp_path, limit=0.26)
    with pytest.raises(BudgetBlocked, match='monthly_limit_exceeded'):
        certified(ledger)


def test_concurrent_admissions_cannot_spend_existing_hold_twice(tmp_path):
    a = ledger_at(tmp_path, limit=0.6)
    a.settle(certified(a), outcome='timeout')
    b = ledger_at(tmp_path, limit=0.6)
    def attempt(ledger):
        try:
            return certified(ledger)
        except BudgetBlocked as exc:
            return exc
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, [a, b]))
    assert sum(isinstance(result, str) for result in results) == 1
    assert a.status()['available_usd'] == pytest.approx(0.6 - 2 * CEILING)
    a.close()
    b.close()


@pytest.mark.parametrize('actual', [0.01, 0.3])
def test_evidence_reconciliation_replaces_hold_and_preserves_breach(tmp_path, actual):
    ledger = ledger_at(tmp_path)
    token = certified(ledger)
    ledger.settle(token, outcome='timeout')
    evidence = tmp_path / 'synthetic-evidence.txt'
    evidence.write_text('synthetic provider usage evidence')
    review = {'version': 'llm-budget-reconciliation-v1', 'activated_at': ledger.status()['activated_at'],
              'reviewer_ref': 'synthetic-operator', 'evidence_sha256': hashlib.sha256(evidence.read_bytes()).hexdigest(),
              'adjustments': [{'token': token, 'actual_cost_usd': actual}], 'clear_bound_breach': False}
    preview = reconcile_budget(ledger.path, 1, review, evidence)
    reconcile_budget(ledger.path, 1, review, evidence, apply=True, expected_state_sha256=preview['state_sha256'])
    status = ledger.status()
    assert status['unresolved_count'] == 0
    assert status['uncertain_cost_ceiling_usd'] == 0
    assert status['spent_usd'] == actual
    assert status['bound_breach'] == (actual > CEILING)
    if actual < CEILING:
        assert status['available_usd'] == 1 - actual


@pytest.mark.parametrize('outcome', ['known', 'not_sent', 'rejected'])
def test_normal_settlement_never_creates_timeout_hold(tmp_path, outcome):
    ledger = ledger_at(tmp_path)
    token = certified(ledger)
    assert ledger.settle(token, outcome=outcome, actual_cost_usd=0.01) is None
    status = ledger.status()
    assert status['unresolved_count'] == status['uncertain_cost_ceiling_usd'] == 0
    assert status['spent_usd'] == (0.01 if outcome == 'known' else 0)


def test_hold_occupancy_crosses_policy_economy_threshold(tmp_path):
    ledger = ledger_at(tmp_path, limit=0.32, policy_enabled=True)
    ledger.settle(certified(ledger), outcome='timeout')
    assert ledger.status()['policy_mode'] == 'economy'


@pytest.mark.parametrize('damage', ['certificate', 'ceiling', 'held', 'table'])
def test_corrupt_hold_never_enables_admission(tmp_path, damage):
    ledger = ledger_at(tmp_path)
    ledger.settle(certified(ledger), outcome='timeout')
    with sqlite3.connect(ledger.path) as db:
        if damage == 'certificate':
            db.execute("UPDATE timeout_bounds SET certificate='{}'")
        elif damage == 'ceiling':
            db.execute('UPDATE timeout_bounds SET ceiling_units=1')
        elif damage == 'held':
            db.execute('UPDATE timeout_bounds SET held=0')
        else:
            db.execute('ALTER TABLE timeout_bounds RENAME TO corrupt_bounds')
            db.execute('CREATE TABLE timeout_bounds (token TEXT)')
    with pytest.raises(BudgetBlocked):
        ledger.reserve('gpt-5-mini', 'p', '', 100)


def test_legacy_schema_unknown_stays_blocked_on_migration(tmp_path):
    ledger = ledger_at(tmp_path)
    ledger.settle(ledger.reserve('gpt-5-mini', 'p', '', 100), outcome='unknown')
    with sqlite3.connect(ledger.path) as db:
        db.execute('DROP TABLE IF EXISTS timeout_bounds')
    reopened = ledger_at(tmp_path)
    assert reopened.status()['block_reason'] == 'unresolved_usage'
    with sqlite3.connect(ledger.path) as db:
        assert len(db.execute('PRAGMA table_info(reservations)').fetchall()) == 8
