"""Missing-cost opt-in retains evidence and all known financial constraints."""
import logging
import sqlite3
from datetime import datetime, timezone

import pytest

from src.utils.llm_budget import BudgetBlocked, BudgetStateError, MonthlyBudgetLedger

MODEL = 'gemini-3.1-flash-lite'
CONTRACT = 'gemini-flash-lite-standard-text-v1'
NOW = datetime(2026, 10, 17, tzinfo=timezone.utc)


def ledger_at(tmp_path, **kwargs):
    kwargs.setdefault('clock', lambda: NOW)
    return MonthlyBudgetLedger(tmp_path / 'budget.sqlite3', 1, **kwargs)


def reserve(ledger, **kwargs):
    return ledger.reserve('gpt-5-mini', 'private synthetic prompt', '', 100, **kwargs)


def row_for(ledger, token):
    with sqlite3.connect(ledger.path) as db:
        return db.execute('SELECT * FROM reservations WHERE token=?', (token,)).fetchone()


@pytest.mark.parametrize('bad', ['', 'ignore', 'LOG_AND_CONTINUE', None, True, [], {}])
def test_invalid_policy_cannot_initialize_any_storage(tmp_path, bad):
    with pytest.raises(BudgetStateError, match='invalid_missing_cost_policy'):
        ledger_at(tmp_path, missing_cost_policy=bad)
    assert list(tmp_path.iterdir()) == []


def test_default_block_preserves_unknown_gate_and_reports_incomplete_cost(tmp_path):
    ledger = ledger_at(tmp_path)
    ledger.settle(reserve(ledger), outcome='unknown')
    status = ledger.status()
    assert status['missing_cost_policy'] == 'block'
    assert status['skipped_unknown_count'] == 0
    assert status['cost_accounting_complete'] is False
    with pytest.raises(BudgetBlocked, match='unresolved_usage'):
        reserve(ledger)


@pytest.mark.parametrize('month', [10, 11])
def test_legacy_unknown_survives_opt_in_restart_month_and_switch_back(tmp_path, month):
    old = ledger_at(tmp_path)
    original = reserve(old)
    old.settle(original, outcome='unknown')
    before = row_for(old, original)
    old.close()
    ledger = ledger_at(tmp_path, missing_cost_policy='log_and_continue',
                       clock=lambda: datetime(2026, month, 17, tzinfo=timezone.utc))
    snapshot_bytes = ledger.path.read_bytes()
    status = ledger.status()
    assert ledger.path.read_bytes() == snapshot_bytes
    assert status['missing_cost_policy'] == 'log_and_continue'
    assert status['unbounded_unresolved_count'] == status['skipped_unknown_count'] == 1
    assert status['unresolved_count'] == 1
    assert status['cost_accounting_complete'] is False
    assert status['blocked'] is False
    assert status['spent_usd'] == status['committed_usd'] == 0
    assert status['available_usd'] == 1
    new = reserve(ledger)
    ledger.settle(new, outcome='not_sent')
    assert row_for(ledger, original) == before
    assert before[4:6] == ('unknown', None)
    ledger.close()
    strict = ledger_at(tmp_path, missing_cost_policy='block')
    with pytest.raises(BudgetBlocked, match='unresolved_usage'):
        reserve(strict)
    assert row_for(strict, original) == before


@pytest.mark.parametrize('kind', ['unknown', 'timeout', 'orphan', 'certified_unknown'])
def test_each_unbounded_missing_usage_can_be_skipped_without_forged_settlement(tmp_path, kind):
    ledger = ledger_at(tmp_path, missing_cost_policy='log_and_continue')
    token = (ledger.reserve(MODEL, 'synthetic', '', 512, timeout_hold_contract=CONTRACT)
             if kind == 'certified_unknown' else reserve(ledger))
    estimate = row_for(ledger, token)[3]
    if kind == 'orphan':
        ledger.abandon(token)
    else:
        ledger.settle(token, outcome='timeout' if kind == 'timeout' else 'unknown')
    new = reserve(ledger)
    ledger.settle(new, outcome='not_sent')
    row = row_for(ledger, token)
    assert row[3:6] == (estimate, 'unknown', None)
    assert ledger.status()['skipped_unknown_count'] == 1


def test_skipped_missing_usage_logs_once_per_new_observed_occurrence(tmp_path, caplog):
    ledger = ledger_at(tmp_path, missing_cost_policy='log_and_continue')
    with caplog.at_level(logging.WARNING, logger='src.utils.llm_budget'):
        first = reserve(ledger)
        ledger.settle(first, outcome='unknown')
        for _ in range(3):
            ledger.status()
        messages = [r.getMessage() for r in caplog.records if r.name == 'src.utils.llm_budget']
        assert len(messages) == 1
        second = reserve(ledger)
        ledger.settle(second, outcome='unknown')
        for _ in range(3):
            ledger.status()
        messages = [r.getMessage() for r in caplog.records if r.name == 'src.utils.llm_budget']
        assert len(messages) == 2
    assert 'log_and_continue' in messages[0]
    assert '1' in messages[0] and '2' in messages[1]
    assert first not in caplog.text and second not in caplog.text
    assert 'private synthetic prompt' not in caplog.text


def test_known_spending_active_reservations_and_certified_hold_still_count(tmp_path):
    ledger = ledger_at(tmp_path, missing_cost_policy='log_and_continue')
    assert ledger.status()['cost_accounting_complete'] is True
    known = reserve(ledger)
    ledger.settle(known, outcome='known', actual_cost_usd=0.0001)
    ledger.settle(reserve(ledger), outcome='unknown')
    held = ledger.reserve(MODEL, 'synthetic', '', 512, timeout_hold_contract=CONTRACT)
    ledger.settle(held, outcome='timeout')
    live = ledger.reserve(MODEL, 'synthetic', '', 512, timeout_hold_contract=CONTRACT)
    status = ledger.status()
    assert status['spent_usd'] == 0.0001
    assert status['reserved_usd'] == status['uncertain_cost_ceiling_usd'] == 0.262912
    assert status['committed_usd'] == 0.525924
    assert status['available_usd'] == 0.474076
    assert status['unresolved_count'] == 2
    assert status['bounded_unresolved_count'] == status['skipped_unknown_count'] == 1
    assert status['cost_accounting_complete'] is False
    ledger.settle(live, outcome='not_sent')


@pytest.mark.parametrize('spent,mode,reason', [
    (0.799, 'normal', None), (0.8, 'economy', None),
    (0.95, 'essential', 'essential_budget_reserved'), (1.0, 'paused', 'monthly_limit_exceeded'),
])
def test_missing_cost_does_not_bypass_policy_thresholds(tmp_path, spent, mode, reason):
    ledger = ledger_at(tmp_path, missing_cost_policy='log_and_continue', policy_enabled=True)
    ledger.settle(reserve(ledger), outcome='unknown')
    # Historical settled charges are supplied explicitly, independently of pricing.
    with sqlite3.connect(ledger.path) as db:
        db.execute("INSERT INTO reservations VALUES(?,?,'gpt-5-mini',?,'settled',?,?,?)",
                   ('a' * 32, '2026-10', int(spent * 1e9), int(spent * 1e9),
                    NOW.isoformat(), NOW.isoformat()))
    status = ledger.policy_status()
    assert status['policy_mode'] == mode
    assert status['spent_usd'] == spent
    assert status['skipped_unknown_count'] == 1
    if reason:
        with pytest.raises(BudgetBlocked, match=reason):
            reserve(ledger)
    else:
        ledger.settle(reserve(ledger), outcome='not_sent')
    if mode == 'essential':
        ledger.settle(reserve(ledger, policy_priority='essential'), outcome='not_sent')
    elif mode == 'paused':
        with pytest.raises(BudgetBlocked, match='monthly_limit_exceeded'):
            reserve(ledger, policy_priority='essential')


def test_certified_holds_keep_thresholds_after_restart_and_month(tmp_path):
    ledger = MonthlyBudgetLedger(tmp_path / 'budget.sqlite3', 0.32, clock=lambda: NOW,
                                missing_cost_policy='log_and_continue', policy_enabled=True)
    ledger.settle(reserve(ledger), outcome='unknown')
    held = ledger.reserve(MODEL, 'p', '', 512, timeout_hold_contract=CONTRACT)
    ledger.settle(held, outcome='timeout')
    ledger.close()
    reopened = MonthlyBudgetLedger(ledger.path, 0.32, missing_cost_policy='log_and_continue',
        policy_enabled=True, clock=lambda: datetime(2026, 11, 17, tzinfo=timezone.utc))
    status = reopened.policy_status()
    assert status['policy_mode'] == 'economy'
    assert status['uncertain_cost_ceiling_usd'] == 0.262912
    assert status['skipped_unknown_count'] == 1
    with pytest.raises(BudgetBlocked, match='essential_budget_reserved'):
        reopened.reserve(MODEL, 'p', '', 512, timeout_hold_contract=CONTRACT)


def test_missing_cost_opt_in_does_not_waive_bound_breach(tmp_path):
    ledger = ledger_at(tmp_path, missing_cost_policy='log_and_continue')
    ledger.settle(reserve(ledger), outcome='unknown')
    ledger.settle(reserve(ledger), outcome='known', actual_cost_usd=0.5)
    assert ledger.status()['block_reason'] == 'reservation_bound_breach'
    with pytest.raises(BudgetBlocked, match='reservation_bound_breach'):
        reserve(ledger)


@pytest.mark.parametrize('damage', ['certificate', 'ceiling', 'table', 'unknown_actual'])
def test_missing_cost_opt_in_never_skips_storage_corruption(tmp_path, damage):
    ledger = ledger_at(tmp_path, missing_cost_policy='log_and_continue')
    held = ledger.reserve(MODEL, 'p', '', 512, timeout_hold_contract=CONTRACT)
    ledger.settle(held, outcome='timeout')
    unknown = reserve(ledger)
    ledger.settle(unknown, outcome='unknown')
    with sqlite3.connect(ledger.path) as db:
        if damage == 'certificate':
            db.execute("UPDATE timeout_bounds SET certificate='{}'")
        elif damage == 'ceiling':
            db.execute('UPDATE timeout_bounds SET ceiling_units=1')
        elif damage == 'table':
            db.execute('DROP TABLE timeout_bounds')
        else:
            db.execute('UPDATE reservations SET actual_units=1 WHERE token=?', (unknown,))
    with pytest.raises(BudgetBlocked):
        reserve(ledger)


@pytest.mark.parametrize('model,contract,reason', [
    ('gemini-3.1-flash-lite-preview', None, 'retired_model'),
    (MODEL, 'invalid', 'invalid_timeout_hold_contract'),
])
def test_skip_policy_preserves_model_and_contract_admission_guards(tmp_path, model, contract, reason):
    ledger = ledger_at(tmp_path, missing_cost_policy='log_and_continue')
    ledger.settle(reserve(ledger), outcome='unknown')
    with pytest.raises(BudgetBlocked, match=reason):
        ledger.reserve(model, 'p', '', 512, timeout_hold_contract=contract)
