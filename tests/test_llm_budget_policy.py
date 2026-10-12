"""Policy tests use isolated ledgers and a controllable KST clock only."""
import sqlite3
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from src.utils.llm_budget import BudgetBlocked, MonthlyBudgetLedger

KST = ZoneInfo('Asia/Seoul')


def make(tmp_path):
    clock = [datetime(2026, 10, 12, 23, tzinfo=KST)]
    ledger = MonthlyBudgetLedger(tmp_path / 'ledger.sqlite3', 100,
                                 clock=lambda: clock[0], policy_enabled=True)
    return ledger, clock


def spent(ledger, dollars, month='2026-10'):
    with sqlite3.connect(ledger.path) as db:
        db.execute("INSERT INTO reservations VALUES(?,?,?,0,'settled',?,?,?)",
                   ('seed' + str(dollars) + month, month, 'gpt-5-mini',
                    int(dollars * 1e9), month + '-01', month + '-01'))


def test_upgrade_keeps_activation_and_spending(tmp_path):
    old = MonthlyBudgetLedger(tmp_path / 'ledger.sqlite3', 100)
    spent(old, 3)
    before = old.status()
    new = MonthlyBudgetLedger(old.path, 100, policy_enabled=True)
    after = new.policy_status()
    assert after['activated_at'] == before['activated_at']
    assert after['spent_usd'] == before['spent_usd']
    assert after['policy_mode'] == 'normal'
    assert new.claim_policy_notice() is None


def test_missing_policy_history_is_not_silently_recreated(tmp_path):
    ledger, _ = make(tmp_path)
    with sqlite3.connect(ledger.path) as db:
        db.execute('DROP TABLE policy_months')
    with pytest.raises(BudgetBlocked, match='policy_storage_incomplete'):
        MonthlyBudgetLedger(ledger.path, 100, policy_enabled=True)


def test_failed_policy_upgrade_rolls_back_all_tables_and_keeps_cost(tmp_path, monkeypatch):
    from src.utils.llm_budget_policy import BudgetPolicyMixin
    old = MonthlyBudgetLedger(tmp_path / 'ledger.sqlite3', 100)
    original = BudgetPolicyMixin._initialize_policy
    def fail_mid_upgrade(self, db):
        db.execute('BEGIN IMMEDIATE')
        db.execute('CREATE TABLE policy_meta(id INTEGER)')
        raise sqlite3.OperationalError('synthetic failure')
    monkeypatch.setattr(BudgetPolicyMixin, '_initialize_policy', fail_mid_upgrade)
    with pytest.raises(BudgetBlocked, match='storage_unavailable'):
        MonthlyBudgetLedger(old.path, 100, policy_enabled=True)
    with sqlite3.connect(old.path) as db:
        assert db.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='policy_meta'").fetchone()[0] == 0
    monkeypatch.setattr(BudgetPolicyMixin, '_initialize_policy', original)
    assert MonthlyBudgetLedger(old.path, 100, policy_enabled=True).status()['activated_at'] == old.status()['activated_at']


def test_thresholds_sticky_restart_and_cap(tmp_path):
    ledger, clock = make(tmp_path)
    assert ledger.policy_status()['policy_mode'] == 'normal'
    spent(ledger, 80)
    assert ledger.policy_status()['policy_mode'] == 'economy'
    notice = ledger.claim_policy_notice()
    assert notice['payload']['policy_mode'] == 'economy'
    ledger.finish_policy_notice(notice['event_id'], notice['claim_token'], delivered=True)
    reopened = MonthlyBudgetLedger(ledger.path, 100, clock=lambda: clock[0], policy_enabled=True)
    assert reopened.policy_status()['policy_mode'] == 'economy'
    assert reopened.claim_policy_notice() is None
    spent(ledger, 15)
    assert ledger.policy_status()['policy_mode'] == 'essential'
    with pytest.raises(BudgetBlocked, match='essential_budget_reserved'):
        ledger.reserve('gpt-5-mini', 'p', '', 100)
    token = ledger.reserve('gpt-5-mini', 'p', '', 100, policy_priority='essential')
    ledger.settle(token, outcome='not_sent')
    spent(ledger, 5)
    assert ledger.policy_status()['policy_mode'] == 'paused'
    with pytest.raises(BudgetBlocked, match='monthly_limit_exceeded'):
        ledger.reserve('gpt-5-mini', '', '', 100, policy_priority='essential')


def test_profile_race_and_projected_reserve_rejected_before_http(tmp_path):
    ledger, _ = make(tmp_path)
    spent(ledger, 80)
    with pytest.raises(BudgetBlocked, match='budget_profile_changed'):
        ledger.reserve('gpt-5-mini', '', '', 10, expected_policy_mode='normal')
    spent(ledger, 14.99999)
    with pytest.raises(BudgetBlocked, match='essential_budget_reserved'):
        ledger.reserve('gpt-5-mini', '', '', 100)
    assert ledger.status()['reserved_usd'] == 0


def test_economy_summary_admission_enforces_configured_light_models(tmp_path):
    ledger, clock = make(tmp_path)
    ledger = MonthlyBudgetLedger(ledger.path, 100, policy_enabled=True, clock=lambda: clock[0],
                                 economy_summary_models=('gpt-5-mini', 'gemini-3.1-flash-lite-preview'))
    spent(ledger, 80)
    with pytest.raises(BudgetBlocked, match='economy_profile_required'):
        ledger.reserve('gpt-5.6-sol', '', '', 100, task='report_summary', expected_policy_mode='economy')
    for model, priority in [('gpt-5-mini', 'standard'), ('gpt-5.6-sol', 'essential')]:
        token = ledger.reserve(model, '', '', 100, task='report_summary',
                               expected_policy_mode='economy', policy_priority=priority)
        ledger.settle(token, outcome='not_sent')


def test_notice_sequence_same_timestamp_survives_reopen_and_failure(tmp_path):
    ledger, clock = make(tmp_path)
    spent(ledger, 80)
    ledger.policy_status()
    spent(ledger, 15)
    ledger.policy_status()
    first = ledger.claim_policy_notice()
    ledger.finish_policy_notice(first['event_id'], first['claim_token'], delivered=False)
    assert ledger.claim_policy_notice() is None  # later essential notice cannot overtake
    clock[0] += timedelta(hours=1)
    again = ledger.claim_policy_notice()
    assert (again['sequence'], again['event_id']) == (first['sequence'], first['event_id'])
    ledger.finish_policy_notice(again['event_id'], again['claim_token'], delivered=True)
    ledger = MonthlyBudgetLedger(ledger.path, 100, clock=lambda: clock[0], policy_enabled=True)
    second = ledger.claim_policy_notice()
    assert second['sequence'] == first['sequence'] + 1
    assert second['payload']['sequence'] == second['sequence']


@pytest.mark.parametrize('fault', ['clock_regression', 'policy_write_error'])
def test_policy_failure_cannot_erase_known_settlement(tmp_path, monkeypatch, fault):
    ledger, clock = make(tmp_path)
    token = ledger.reserve('gpt-5-mini', '', '', 100, policy_priority='essential')
    observe = ledger._observe_policy
    if fault == 'clock_regression':
        clock[0] = datetime(2026, 11, 1, tzinfo=KST)
        ledger.policy_status()
        clock[0] = datetime(2026, 10, 31, tzinfo=KST)
    else:
        def fail(*a, **kw):
            raise sqlite3.OperationalError('synthetic policy write failure')
        monkeypatch.setattr(ledger, '_observe_policy', fail)
    ledger.settle(token, outcome='known', actual_cost_usd=0.00001)
    with sqlite3.connect(ledger.path) as db:
        assert db.execute('SELECT state,actual_units,month FROM reservations WHERE token=?', (token,)).fetchone() == (
            'settled', 10000, '2026-10')
    assert token not in ledger.active_reservations
    with pytest.raises(BudgetBlocked):
        ledger.reserve('gpt-5-mini', '', '', 100, policy_priority='essential')
    monkeypatch.setattr(ledger, '_observe_policy', observe)
    clock[0] = datetime(2026, 11, 1, tzinfo=KST)
    assert ledger.policy_status()['unresolved_count'] == 0


def test_month_reset_history_and_unknown_carry(tmp_path):
    ledger, clock = make(tmp_path)
    spent(ledger, 80)
    ledger.policy_status()
    notice = ledger.claim_policy_notice()
    ledger.finish_policy_notice(notice['event_id'], notice['claim_token'], delivered=True)
    clock[0] = datetime(2026, 11, 1, tzinfo=KST)
    assert ledger.policy_status()['policy_mode'] == 'normal'
    notice = ledger.claim_policy_notice()
    assert notice['payload']['kind'] == 'month_reset'
    assert notice['payload']['previous_month']['spent_usd'] == 80
    ledger.finish_policy_notice(notice['event_id'], notice['claim_token'], delivered=True)
    token = ledger.reserve('gpt-5-mini', 'p', '', 100)
    ledger.settle(token, outcome='unknown')
    clock[0] = datetime(2026, 12, 1, tzinfo=KST)
    assert ledger.policy_status()['policy_mode'] == 'blocked'
    assert ledger.policy_status()['block_reason'] == 'unresolved_usage'


def test_outbox_claim_lease_retry_and_stale_ack(tmp_path):
    ledger, clock = make(tmp_path)
    spent(ledger, 80)
    ledger.policy_status()
    first = ledger.claim_policy_notice()
    assert ledger.claim_policy_notice() is None

    clock[0] += timedelta(seconds=121)
    second = ledger.claim_policy_notice()
    assert second['event_id'] == first['event_id']
    assert second['claim_token'] != first['claim_token']
    ledger.finish_policy_notice(first['event_id'], first['claim_token'], delivered=True)
    ledger.finish_policy_notice(second['event_id'], second['claim_token'], delivered=False)
    assert ledger.claim_policy_notice() is None
    clock[0] += timedelta(hours=1)
    third = ledger.claim_policy_notice()
    ledger.finish_policy_notice(third['event_id'], third['claim_token'], delivered=True)
    assert ledger.claim_policy_notice() is None


@pytest.mark.parametrize('start,amount,priority,during,after', [
    (79, 1, 'standard', 'economy', 'economy'),
    (94, 1, 'essential', 'essential', 'essential'),
    (99, 1, 'essential', 'paused', 'essential'),
])
def test_reservation_crossing_sticks_only_80_and_95(tmp_path, monkeypatch, start, amount, priority, during, after):
    from types import SimpleNamespace
    from src.utils import llm_budget
    ledger, _ = make(tmp_path)
    spent(ledger, start)
    monkeypatch.setattr(llm_budget, 'rate_quote', lambda *a, **kw: SimpleNamespace(cost_usd=amount))
    token = ledger.reserve('gpt-5-mini', '', '', 100, policy_priority=priority)
    assert ledger.policy_status()['policy_mode'] == during
    ledger.settle(token, outcome='not_sent')
    assert ledger.policy_status()['policy_mode'] == after


def test_concurrent_projected_95_admission_is_serialized(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from types import SimpleNamespace
    from src.utils import llm_budget
    ledger, clock = make(tmp_path)
    spent(ledger, 94)
    peer = MonthlyBudgetLedger(ledger.path, 100, clock=lambda: clock[0], policy_enabled=True)
    monkeypatch.setattr(llm_budget, 'rate_quote', lambda *a, **kw: SimpleNamespace(cost_usd=0.6))
    def reserve(subject):
        try:
            return subject.reserve('gpt-5-mini', '', '', 100)
        except BudgetBlocked as exc:
            return exc.reason
    with ThreadPoolExecutor(2) as pool:
        answers = list(pool.map(reserve, (ledger, peer)))
    assert answers.count('essential_budget_reserved') == 1
    assert ledger.status()['reserved_usd'] == 0.6
    for subject, answer in zip((ledger, peer), answers):
        if answer != 'essential_budget_reserved':
            subject.settle(answer, outcome='not_sent')


def test_rollover_snapshot_provisional_prior_inflight_stays_prior_month(tmp_path):
    ledger, clock = make(tmp_path)
    ledger.policy_status()
    token = ledger.reserve('gpt-5-mini', '', '', 100, policy_priority='essential')
    clock[0] = datetime(2026, 11, 1, tzinfo=KST)
    ledger.policy_status()
    notice = ledger.claim_policy_notice()
    assert notice['payload']['previous_month']['reserved_usd'] > 0
    ledger.settle(token, outcome='known', actual_cost_usd=0.000001)
    assert ledger.status()['spent_usd'] == 0
    with sqlite3.connect(ledger.path) as db:
        assert db.execute('SELECT month FROM reservations WHERE token=?', (token,)).fetchone()[0] == '2026-10'
    clock[0] = datetime(2026, 10, 31, tzinfo=KST)
    with pytest.raises(BudgetBlocked, match='policy_clock_regression'):
        ledger.policy_status()
