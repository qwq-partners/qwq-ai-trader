"""L01 Task 5: 월 상한 명시 이관(D2) — 설정만 바꾸면 종전대로 mismatch, 이관 뒤 사용액·예약·정책 행 보존. 외부 IO 없음."""
import json
import sqlite3
from datetime import datetime, timezone

import pytest

from src.utils.llm_budget import BudgetBlocked, MonthlyBudgetLedger, migrate_monthly_limit

CLOCK = lambda: datetime(2026, 11, 12, 1, tzinfo=timezone.utc)   # KST 11/12 10:00


def seeded_ledger(tmp_path, *, limit=100, policy=True):
    ledger = MonthlyBudgetLedger(tmp_path / 'budget.sqlite3', limit, clock=CLOCK, policy_enabled=policy,
                                 missing_cost_policy='log_and_continue')
    t1 = ledger.reserve('gpt-5-mini', 'p', '', 100, task='theme_detection', role='primary')
    ledger.settle(t1, outcome='known', actual_cost_usd=0.0001)   # 예약 추정 이하(bound_breach 아님)
    t2 = ledger.reserve('claude-opus-5-5', 'p', '', 400, task='complete_with', role='primary')
    ledger.settle(t2, outcome='unknown')
    return ledger


def rows(path):
    with sqlite3.connect(path) as db:
        db.row_factory = sqlite3.Row
        return {'reservations': [tuple(r) for r in db.execute('SELECT * FROM reservations ORDER BY token')],
                'attempts': [tuple(r) for r in db.execute('SELECT * FROM policy_attempts ORDER BY token')],
                'settings': tuple(db.execute('SELECT * FROM settings').fetchone())}


def test_config_only_change_still_blocks_and_migration_preserves_rows(tmp_path):
    ledger = seeded_ledger(tmp_path); path = ledger.path
    before = rows(path); spent_before = ledger.status()['spent_usd']
    ledger.close()
    with pytest.raises(BudgetBlocked, match='budget_configuration_mismatch'):
        MonthlyBudgetLedger(path, 200, clock=CLOCK)
    receipt = migrate_monthly_limit(path, current_limit_usd=100, new_limit_usd=200,
                                    note='2026-11-08 사용자 D2: Ultra 크레딧 200, 이전 뒤 상한', clock=CLOCK)
    assert receipt['old_limit_usd'] == 100 and receipt['new_limit_usd'] == 200
    assert receipt['reservations_rows'] == 2 and receipt['spent_usd'] == pytest.approx(0.0001)
    after = rows(path)
    assert after['reservations'] == before['reservations'] and after['attempts'] == before['attempts']
    assert after['settings'][2] == 200 * 10 ** 9 and after['settings'][:2] == before['settings'][:2]
    assert after['settings'][3:] == before['settings'][3:]           # activated_at·baseline·bound_breach 불변
    with pytest.raises(BudgetBlocked, match='budget_configuration_mismatch'):
        MonthlyBudgetLedger(path, 100, clock=CLOCK)                   # 옛 상한 설정은 이제 거부
    reopened = MonthlyBudgetLedger(path, 200, clock=CLOCK, policy_enabled=True, missing_cost_policy='log_and_continue')
    try:
        status = reopened.status()
        assert status['monthly_limit_usd'] == 200 and status['spent_usd'] == spent_before == pytest.approx(0.0001)
        assert status['unresolved_count'] == 1 and status['settled_count'] == 1
        assert status['activated_at'] == before['settings'][3]
    finally:
        reopened.close()
    with sqlite3.connect(path) as db:
        mig = db.execute('SELECT old_units,new_units,note,reservations_rows,spent_units FROM limit_migrations').fetchall()
    assert mig == [(100 * 10 ** 9, 200 * 10 ** 9, '2026-11-08 사용자 D2: Ultra 크레딧 200, 이전 뒤 상한', 2, 100_000)]
    assert receipt['policy_level_previous'] in (0, None) and receipt['policy_level_new'] in (0, None)


def test_policy_thresholds_follow_migrated_limit(tmp_path):
    ledger = MonthlyBudgetLedger(tmp_path / 'budget.sqlite3', 100, clock=CLOCK, policy_enabled=True)
    month = ledger.status()['month']
    with sqlite3.connect(ledger.path) as db:    # 이달 확정 사용액 85 USD 를 정산 행으로 심는다(예약 상한 위반 없이)
        db.execute("INSERT INTO reservations VALUES(?,?,?,?,'settled',?,?,?)",
                   ('seed', month, 'gpt-6-astra', 85 * 10 ** 9, 85 * 10 ** 9, month + '-01', month + '-01'))
    assert ledger.status()['policy_mode'] == 'economy'              # 85/100 ≥ 80%
    ledger.close()
    migrate_monthly_limit(ledger.path, current_limit_usd=100, new_limit_usd=200, note='test', clock=CLOCK)
    reopened = MonthlyBudgetLedger(ledger.path, 200, clock=CLOCK, policy_enabled=True)
    try:
        assert reopened.status()['policy_mode'] == 'normal'          # 85/200 < 80%: 160/190/200 문턱
        assert reopened.status()['available_usd'] == pytest.approx(115.0)
    finally:
        reopened.close()


@pytest.mark.parametrize('kwargs,reason', [
    (dict(current_limit_usd=150, new_limit_usd=200, note='n'), 'budget_configuration_mismatch'),
    (dict(current_limit_usd=100, new_limit_usd=100, note='n'), 'migration_noop'),
    (dict(current_limit_usd=100, new_limit_usd=200, note='  '), 'migration_note_required'),
])
def test_migration_refuses_without_changing_anything(tmp_path, kwargs, reason):
    ledger = seeded_ledger(tmp_path); path = ledger.path; before = rows(path); ledger.close()
    with pytest.raises(BudgetBlocked, match=reason):
        migrate_monthly_limit(path, clock=CLOCK, **kwargs)
    assert rows(path) == before
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT 1 FROM sqlite_master WHERE name='limit_migrations'").fetchone() is None


def test_migration_refuses_while_a_reservation_is_live_and_never_creates_a_ledger(tmp_path):
    ledger = MonthlyBudgetLedger(tmp_path / 'budget.sqlite3', 100, clock=CLOCK)
    token = ledger.reserve('gpt-5-mini', 'p', '', 100)
    try:
        with pytest.raises(BudgetBlocked, match='active_reservations_present'):
            migrate_monthly_limit(ledger.path, current_limit_usd=100, new_limit_usd=200, note='n', clock=CLOCK)
    finally:
        ledger.settle(token, outcome='not_sent'); ledger.close()
    missing = tmp_path / 'absent.sqlite3'
    with pytest.raises(BudgetBlocked, match='storage_unavailable'):
        migrate_monthly_limit(missing, current_limit_usd=100, new_limit_usd=200, note='n', clock=CLOCK)
    assert not missing.exists()


def test_cli_check_is_read_only_and_migrate_holds_bot_lock(tmp_path, monkeypatch, capsys):
    import importlib, sys
    sys.path.insert(0, 'scripts/ops')
    cli = importlib.import_module('llm_budget_migrate')
    ledger = seeded_ledger(tmp_path); path = ledger.path; before = rows(path); ledger.close()
    assert cli.main(['--check', '--path', str(path)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out['limit_usd'] == 100 and out['reservation_states'] == {'settled': 1, 'unknown': 1} and out['migrations'] == 0
    assert rows(path) == before
    held = []
    monkeypatch.setattr(cli, 'hold_or_exit', lambda tool: held.append(tool))
    assert cli.main(['--path', str(path), '--from', '100', '--to', '200', '--note', 'D2']) == 0
    assert held == ['llm_budget_migrate'] and json.loads(capsys.readouterr().out)['new_limit_usd'] == 200
    assert cli.main(['--path', str(path), '--from', '100', '--to', '300', '--note', 'again']) == 2
    assert json.loads(capsys.readouterr().err)['error'] == 'budget_configuration_mismatch'
    def refuse(tool):
        raise SystemExit(2)
    monkeypatch.setattr(cli, 'hold_or_exit', refuse)
    with pytest.raises(SystemExit):
        cli.main(['--path', str(path), '--from', '200', '--to', '300', '--note', 'locked'])
    assert rows(path)['settings'][2] == 200 * 10 ** 9


def test_latched_essential_level_is_recomputed_under_new_limit_and_transition_is_observed(tmp_path):
    # 독립 리뷰 1차 P1: policy_months.level 은 월 고수위라 95/100 에서 essential 이 저장되면 상한만 올려도 남는다.
    ledger = MonthlyBudgetLedger(tmp_path / 'budget.sqlite3', 100, clock=CLOCK, policy_enabled=True)
    month = ledger.status()['month']
    with sqlite3.connect(ledger.path) as db:
        db.execute("INSERT INTO reservations VALUES(?,?,?,?,'settled',?,?,?)",
                   ('seed', month, 'gpt-6-astra', 96 * 10 ** 9, 96 * 10 ** 9, month + '-01', month + '-01'))
    assert ledger.policy_status()['policy_mode'] == 'essential'    # 관측이 level=2 를 영속화한다
    ledger.close()
    with sqlite3.connect(ledger.path) as db:
        assert db.execute('SELECT level FROM policy_months WHERE month=?', (month,)).fetchone() == (2,)
    receipt = migrate_monthly_limit(ledger.path, current_limit_usd=100, new_limit_usd=200, note='D2', clock=CLOCK)
    assert (receipt['policy_level_previous'], receipt['policy_level_new']) == (2, 0)    # 96/200 = 48%
    reopened = MonthlyBudgetLedger(ledger.path, 200, clock=CLOCK, policy_enabled=True)
    try:
        status = reopened.policy_status()
        assert status['policy_mode'] == 'normal' and status['spent_usd'] == pytest.approx(96.0)
        first = reopened.claim_policy_notice()                        # 1번: 이관 전 normal → essential 전환(기존 알림)
        assert first['payload']['policy_mode'] == 'essential'
        reopened.finish_policy_notice(first['event_id'], first['claim_token'], delivered=True)
        notice = reopened.claim_policy_notice()                       # 2번: 이관 뒤 essential → normal 전환이 종전 알림 경로로 남는다
        assert notice is not None and notice['payload']['policy_mode'] == 'normal' and notice['payload']['kind'] == 'transition'
        with sqlite3.connect(ledger.path) as db:
            assert db.execute('SELECT previous_level,new_level FROM limit_migrations').fetchone() == (2, 0)
    finally:
        reopened.close()


def test_migration_to_a_limit_still_within_thresholds_keeps_level(tmp_path):
    ledger = MonthlyBudgetLedger(tmp_path / 'budget.sqlite3', 100, clock=CLOCK, policy_enabled=True)
    month = ledger.status()['month']
    with sqlite3.connect(ledger.path) as db:
        db.execute("INSERT INTO reservations VALUES(?,?,?,?,'settled',?,?,?)",
                   ('seed', month, 'gpt-6-astra', 96 * 10 ** 9, 96 * 10 ** 9, month + '-01', month + '-01'))
    ledger.policy_status(); ledger.close()
    receipt = migrate_monthly_limit(ledger.path, current_limit_usd=100, new_limit_usd=110, note='small', clock=CLOCK)
    assert (receipt['policy_level_previous'], receipt['policy_level_new']) == (2, 1)    # 96/110 = 87% → economy


def test_orphaned_reservation_of_a_dead_process_does_not_block_migration(tmp_path):
    # 독립 리뷰 1차 P2: 종료된 프로세스의 reserved 행은 잠금이 없다 → 고아. 행은 보존하고 이관은 진행한다.
    import subprocess, sys
    path = tmp_path / 'budget.sqlite3'
    code = ("from src.utils.llm_budget import MonthlyBudgetLedger; import os,sys; "
            "ledger=MonthlyBudgetLedger(sys.argv[1],100); ledger.reserve('gpt-5-mini','p','',100); os._exit(0)")
    subprocess.run([sys.executable, '-c', code, str(path)], check=True, timeout=10)
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT COUNT(*) FROM reservations WHERE state='reserved'").fetchone()[0] == 1
    receipt = migrate_monthly_limit(path, current_limit_usd=100, new_limit_usd=200, note='orphan ok', clock=CLOCK)
    assert receipt['reservations_rows'] == 1
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT state FROM reservations").fetchall() == [('reserved',)]   # 행 보존(다음 기동이 불명 처리)
    reopened = MonthlyBudgetLedger(path, 200, clock=CLOCK)
    try:
        assert reopened.status()['unresolved_count'] == 1 and reopened.status()['monthly_limit_usd'] == 200
    finally:
        reopened.close()


def test_invalid_clock_is_rejected_not_defaulted(tmp_path):
    ledger = seeded_ledger(tmp_path); path = ledger.path; ledger.close()
    for bad in (0, 'now', object()):
        with pytest.raises(BudgetBlocked, match='invalid_clock'):
            migrate_monthly_limit(path, current_limit_usd=100, new_limit_usd=200, note='n', clock=bad)
