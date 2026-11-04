"""공유 임시 원장의 명시 등록·전역/실험 상한·과거 바이트 보존 검사."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import hashlib
import importlib
import importlib.util
import json

import pytest

from src.analytics.model_comparison_execution import ComparisonCallGuard, read_day_attempts
from src.utils.llm_budget import BudgetBlocked, MonthlyBudgetLedger
from tests.test_model_comparison_execution import NOW, paid_once, request, setup_guard


def registry():
    assert importlib.util.find_spec('src.analytics.model_comparison_policy_registry'), 'explicit policy registry missing'
    return importlib.import_module('src.analytics.model_comparison_policy_registry')


def register(ledger, policy, **kw):
    return registry().register_policy(ledger=ledger, policy=policy, approved_by=kw.get('approved_by', 'reviewer'),
                                      approved_plan_sha256=kw.get('approved_plan_sha256', 'a' * 64))


def snapshot(ledger):
    with ledger._connect(readonly=True) as db:
        return {r[0]: [tuple(row) for row in db.execute('SELECT * FROM "' + r[0] + '"')]
                for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def call(guard, name):
    return asyncio.run(guard.call(request(), dispatch=paid_once, attempt_id=name))


def test_registration_preserves_history_and_both_guards_recover(tmp_path):
    ledger, old, guard = setup_guard(tmp_path, cap=4)
    original = call(guard, 'old-1')
    before = snapshot(ledger)
    new = replace(old, experiment_id='new', daily_attempt_cap=2)
    with pytest.raises(BudgetBlocked, match='comparison_policy_mismatch'):
        ComparisonCallGuard(ledger=ledger, policy=new)
    register(ledger, new)
    assert all(snapshot(ledger)[table] == rows for table, rows in before.items())
    other = ComparisonCallGuard(ledger=ledger, policy=new)
    call(other, 'new-1')
    assert call(guard, 'old-1') == original
    assert other.lookup('old-1')['result'] == original
    assert len(read_day_attempts(ledger.path, '2026-10-13')) == 2
    ledger.close()
    reopened = MonthlyBudgetLedger(ledger.path, 100, clock=lambda: NOW, policy_enabled=True)
    assert call(ComparisonCallGuard(ledger=reopened, policy=new), 'new-1').value == 'approve'
    assert call(ComparisonCallGuard(ledger=reopened, policy=old), 'old-1') == original


def test_global_cap_survives_multiple_experiment_ids_and_local_cap_is_narrower(tmp_path):
    ledger, old, guard = setup_guard(tmp_path, cap=4)
    new = replace(old, experiment_id='new', daily_attempt_cap=1)
    third = replace(old, experiment_id='third', daily_attempt_cap=10)
    register(ledger, new)
    register(ledger, third)
    second = ComparisonCallGuard(ledger=ledger, policy=new)
    last = ComparisonCallGuard(ledger=ledger, policy=third)
    call(guard, 'old-1')
    call(second, 'new-1')
    with pytest.raises(BudgetBlocked, match='comparison_daily_attempt_cap'): call(second, 'new-2')
    call(last, 'third-1')
    call(last, 'third-2')
    with pytest.raises(BudgetBlocked, match='comparison_daily_attempt_cap'): call(last, 'third-3')
    assert len(guard.day_attempts('2026-10-13')) == 4


def test_same_id_registration_approval_and_policy_are_immutable(tmp_path):
    ledger, old, guard = setup_guard(tmp_path)
    new = replace(old, experiment_id='new')
    register(ledger, new)
    before = snapshot(ledger)
    register(ledger, new)
    for changed in (replace(new, daily_attempt_cap=3), replace(new, valid_until='2026-12-01'),
                    replace(old, daily_attempt_cap=3)):
        with pytest.raises(BudgetBlocked): register(ledger, changed)
    for kw in ({'approved_by': ''}, {'approved_plan_sha256': 'bad'}, {'approved_by': 'other'},
               {'approved_plan_sha256': 'b' * 64}):
        with pytest.raises((BudgetBlocked, ValueError)): register(ledger, new, **kw)
    assert snapshot(ledger) == before


def test_cross_policy_attempt_id_collision_and_concurrent_global_cap(tmp_path):
    ledger, old, guard = setup_guard(tmp_path, cap=2)
    new = replace(old, experiment_id='new')
    register(ledger, new)
    other = ComparisonCallGuard(ledger=ledger, policy=new)
    call(guard, 'shared')
    with pytest.raises(BudgetBlocked, match='comparison_attempt_mismatch'): call(other, 'shared')
    def run(item):
        g, name = item
        try:
            call(g, name)
            return 'sent'
        except BudgetBlocked as exc:
            return exc.reason
    with ThreadPoolExecutor(2) as pool:
        outcomes = list(pool.map(run, [(guard, 'old-2'), (other, 'new-2')]))
    assert sorted(outcomes) == ['comparison_daily_attempt_cap', 'sent']
    assert len(guard.day_attempts('2026-10-13')) == 2


@pytest.mark.parametrize('damage', ['partial', 'version', 'digest', 'anchor', 'identity', 'contract', 'request'])
def test_damaged_registry_or_history_fails_before_new_admission(tmp_path, damage):
    ledger, old, guard = setup_guard(tmp_path, cap=4)
    new = replace(old, experiment_id='new')
    register(ledger, new)
    call(guard, 'old-1')
    with ledger._connect() as db:
        if damage == 'partial': db.execute('DROP TABLE comparison_policy_registry')
        if damage == 'version': db.execute('UPDATE comparison_policy_lifecycle SET version=99')
        if damage == 'digest': db.execute("UPDATE comparison_policy_registry SET policy_sha256='bad'")
        if damage == 'anchor': db.execute("UPDATE comparison_policy_lifecycle SET anchor_sha256='bad'")
        if damage == 'identity': db.execute('UPDATE comparison_policy SET identity_version=99')
        if damage == 'contract':
            value = json.loads(old.serialized()); value['adapter_contract'] = 'unknown'
            db.execute('UPDATE comparison_policy SET policy=?', (json.dumps(value),))
        if damage == 'request':
            value = json.dumps({'policy': replace(old, experiment_id='unregistered').serialized(), 'request': {}})
            db.execute('UPDATE comparison_requests SET request_json=?,request_sha256=?',
                       (value, hashlib.sha256(value.encode()).hexdigest()))
    before = snapshot(ledger)
    with pytest.raises(BudgetBlocked): call(guard, 'must-not-send')
    with pytest.raises(BudgetBlocked): register(ledger, replace(old, experiment_id='third'))
    assert snapshot(ledger) == before


def test_registration_rolls_back_schema_on_unknown_historical_owner(tmp_path):
    ledger, old, guard = setup_guard(tmp_path)
    call(guard, 'old-1')
    new = replace(old, experiment_id='new')
    with ledger._connect() as db:
        db.execute("UPDATE policy_attempts SET role='new' WHERE task='model_comparison'")
    before = snapshot(ledger)
    with pytest.raises(BudgetBlocked): register(ledger, new)
    assert snapshot(ledger) == before
    assert 'comparison_policy_registry' not in before


def test_old_orphan_slot_counts_toward_global_cap(tmp_path):
    ledger, old, guard = setup_guard(tmp_path, cap=1)
    with ledger._connect() as db:
        db.execute("INSERT INTO comparison_attempts VALUES ('legacy-orphan','2026-10-13',NULL,'blocked')")
    new = replace(old, experiment_id='new')
    register(ledger, new)
    with pytest.raises(BudgetBlocked, match='comparison_daily_attempt_cap'):
        call(ComparisonCallGuard(ledger=ledger, policy=new), 'new-1')


def test_pair_global_local_caps_and_guard_revalidation(tmp_path):
    from tests.test_model_comparison_runner import setup, NOW as PAIR_NOW
    m, ledger, old_runner, spec, _, _ = setup(tmp_path, pairs_per_day=3)
    new = replace(old_runner.guard.policy, experiment_id='new', pairs_per_day=1)
    register(ledger, new)
    runner = m.PairedComparisonRunner(guard=ComparisonCallGuard(ledger=ledger, policy=new),
                                     adapter=old_runner.adapter, clock=lambda: PAIR_NOW)
    old_id, _ = old_runner._claim(spec, PAIR_NOW)
    new_id, _ = runner._claim(spec, PAIR_NOW)
    assert old_runner.admitted_pair_ids('2026-10-13') == {old_id}
    assert runner.admitted_pair_ids('2026-10-13') == {new_id}
    with pytest.raises(BudgetBlocked, match='comparison_daily_pair_cap'):
        runner._claim(replace(spec, decision_id='second'), PAIR_NOW)
    old_runner._claim(replace(spec, decision_id='second'), PAIR_NOW)
    with pytest.raises(BudgetBlocked, match='comparison_daily_pair_cap'):
        old_runner._claim(replace(spec, decision_id='third'), PAIR_NOW)
    with ledger._connect() as db:
        db.execute('UPDATE comparison_policy_lifecycle SET version=99')
    before = snapshot(ledger)
    with pytest.raises(BudgetBlocked): runner._claim(replace(spec, decision_id='third'), PAIR_NOW)
    assert snapshot(ledger) == before


def test_failed_registration_rolls_back_new_tables(tmp_path, monkeypatch):
    import sqlite3
    from contextlib import contextmanager
    ledger, old, _ = setup_guard(tmp_path)
    before = snapshot(ledger)
    connect = ledger._connect
    @contextmanager
    def denied_insert(**kw):
        with connect(**kw) as db:
            db.set_authorizer(lambda action, table, *rest: sqlite3.SQLITE_DENY
                              if action == sqlite3.SQLITE_INSERT and table == 'comparison_policy_registry'
                              else sqlite3.SQLITE_OK)
            yield db
    monkeypatch.setattr(ledger, '_connect', denied_insert)
    with pytest.raises(BudgetBlocked): register(ledger, replace(old, experiment_id='new'))
    monkeypatch.setattr(ledger, '_connect', connect)
    assert snapshot(ledger) == before


def test_registration_preserves_unknown_and_monthly_configuration(tmp_path):
    from src.analytics.model_comparison_execution import ComparisonAttemptResult
    ledger, old, guard = setup_guard(tmp_path, cap=4)
    async def unknown(req): return ComparisonAttemptResult('uncertain', None)
    asyncio.run(guard.call(request(), dispatch=unknown, attempt_id='old-unknown'))
    before = snapshot(ledger)
    marker = ledger.path.with_name(ledger.path.name + '.activated').read_bytes()
    new = replace(old, experiment_id='new')
    register(ledger, new)
    assert all(snapshot(ledger)[table] == rows for table, rows in before.items())
    assert ledger.path.with_name(ledger.path.name + '.activated').read_bytes() == marker
    with pytest.raises(BudgetBlocked, match='unresolved_usage'):
        call(ComparisonCallGuard(ledger=ledger, policy=new), 'new-1')
    assert guard.lookup('old-unknown')['state'] == 'unknown'
    assert ledger.status()['unresolved_count'] == 1


def test_experiment_spend_survives_registration_restart_and_month_change(tmp_path):
    from datetime import datetime, timezone
    from tests.test_model_comparison_admission_budget import request_units
    units = request_units(tmp_path)
    now = [datetime(2026, 10, 31, 14, 59, tzinfo=timezone.utc)]
    ledger, old, guard = setup_guard(tmp_path, cap=4, clock=lambda: now[0])
    new = replace(old, experiment_id='new', experiment_usd_ceiling=str(units / 1_000_000_000))
    register(ledger, new)
    other = ComparisonCallGuard(ledger=ledger, policy=new)
    call(other, 'new-1')
    ledger.close()
    now[0] = datetime(2026, 11, 1, 3, tzinfo=timezone.utc)
    reopened = MonthlyBudgetLedger(ledger.path, 100, clock=lambda: now[0], policy_enabled=True)
    restarted = ComparisonCallGuard(ledger=reopened, policy=new)
    with pytest.raises(BudgetBlocked, match='comparison_experiment_ceiling'): call(restarted, 'new-2')
    call(ComparisonCallGuard(ledger=reopened, policy=old), 'old-next-month')
    assert restarted.lookup('new-1')['state'] == 'known'


def test_old_new_concurrent_reservations_share_monthly_eighty_percent(tmp_path):
    import threading
    from tests.test_model_comparison_admission_budget import request_units, prespend
    units = request_units(tmp_path)
    path = tmp_path / 'budget.db'
    prespend(path, 1, 800_000_000 - units)
    ledger, old, guard = setup_guard(tmp_path, cap=4, limit=1)
    new = replace(old, experiment_id='new')
    register(ledger, new)
    other = ComparisonCallGuard(ledger=ledger, policy=new)
    barrier, loser_done = threading.Barrier(2), threading.Event()
    async def held(req):
        assert loser_done.wait(timeout=5)
        return await paid_once(req)
    def run(item):
        g, name = item
        barrier.wait(timeout=5)
        try:
            asyncio.run(g.call(request(), dispatch=held, attempt_id=name))
            return 'sent'
        except BudgetBlocked as exc:
            loser_done.set()
            return exc.reason
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(run, [(guard, 'old'), (other, 'new')]))
    assert results.count('sent') == 1
    assert next(r for r in results if r != 'sent') in ('comparison_economy_threshold', 'comparison_policy_mode_not_normal')
    assert ledger.status()['reserved_usd'] == 0


def test_diagnostic_registration_failure_preserves_both_consumption_markers(tmp_path, monkeypatch):
    from tests.test_bear_api_cap_diagnostic import setup_run
    m, out, ledger, client, factories, kw = setup_run(tmp_path, monkeypatch)
    def fail(**args):
        assert (out / 'consumed.json').is_file()
        assert len(list(tmp_path.glob('budget.db.bear-api-cap-*.consumed.json'))) == 1
        raise BudgetBlocked('comparison_policy_mismatch')
    monkeypatch.setattr(m, 'register_policy', fail)
    receipt = asyncio.run(m.run_diagnostic(**kw))
    assert receipt['status'] == 'stopped' and factories == [] and client.calls == []
    assert (out / 'consumed.json').is_file()
    assert len(list(tmp_path.glob('budget.db.bear-api-cap-*.consumed.json'))) == 1
    assert 'comparison_policy_registry' not in snapshot(ledger)


def test_diagnostic_without_approval_does_not_register(tmp_path, monkeypatch):
    from tests.test_bear_api_cap_diagnostic import setup_run
    m, out, ledger, client, factories, kw = setup_run(tmp_path, monkeypatch)
    before = snapshot(ledger)
    kw['approved_by'] = ''
    with pytest.raises(ValueError, match='approval_missing'): asyncio.run(m.run_diagnostic(**kw))
    assert snapshot(ledger) == before
    assert factories == [] and client.calls == []
    assert not list(tmp_path.glob('budget.db.bear-api-cap-*.consumed.json'))


@pytest.mark.parametrize('table', ['comparison_requests', 'comparison_policy_registry'])
def test_incomplete_table_constraints_are_rejected(tmp_path, table):
    ledger, old, guard = setup_guard(tmp_path)
    new = replace(old, experiment_id='new')
    register(ledger, new)
    with ledger._connect() as db:
        sql = db.execute('SELECT sql FROM sqlite_master WHERE name=?', (table,)).fetchone()[0]
        rows = db.execute('SELECT * FROM ' + table).fetchall()
        db.execute('DROP TABLE ' + table)
        db.execute(sql.replace('PRIMARY KEY', '').replace('NOT NULL', ''))
        for row in rows:
            db.execute('INSERT INTO ' + table + ' VALUES (' + ','.join('?' for _ in row) + ')', tuple(row))
    before = snapshot(ledger)
    with pytest.raises(BudgetBlocked): call(guard, 'never-send')
    assert snapshot(ledger) == before


def test_empty_registry_is_partial_state_not_permission_to_register_again(tmp_path):
    ledger, old, guard = setup_guard(tmp_path)
    register(ledger, replace(old, experiment_id='new'))
    with ledger._connect() as db: db.execute('DELETE FROM comparison_policy_registry')
    before = snapshot(ledger)
    with pytest.raises(BudgetBlocked): register(ledger, replace(old, experiment_id='new'))
    assert snapshot(ledger) == before


def test_missing_admitted_slot_cannot_reset_global_count(tmp_path):
    ledger, old, guard = setup_guard(tmp_path, cap=1)
    call(guard, 'old-1')
    with ledger._connect() as db: db.execute('DELETE FROM comparison_attempts')
    before = snapshot(ledger)
    with pytest.raises(BudgetBlocked): register(ledger, replace(old, experiment_id='new'))
    with pytest.raises(BudgetBlocked): call(guard, 'old-2')
    assert snapshot(ledger) == before


def test_readonly_preflight_distinguishes_same_id_conflict_from_corrupt_contract(tmp_path, monkeypatch):
    from tests.test_bear_api_cap_diagnostic import setup_run
    m, out, ledger, _, _, _ = setup_run(tmp_path, monkeypatch)
    plan = json.loads((out / 'plan.json').read_text())
    policy = m._policy(plan)
    ComparisonCallGuard(ledger=ledger, policy=replace(policy, max_output_tokens=400))
    before = snapshot(ledger)
    assert m.check_ledger_policy(ledger.path, plan) == {'ready': False, 'reason': 'existing_policy_conflict'}
    assert snapshot(ledger) == before
    with ledger._connect() as db:
        value = json.loads(policy.serialized()); value['schema_version'] = 'unknown'
        db.execute('UPDATE comparison_policy SET policy=?', (json.dumps(value),))
    before = snapshot(ledger)
    assert m.check_ledger_policy(ledger.path, plan) == {'ready': False, 'reason': 'ledger_policy_corrupt'}
    assert snapshot(ledger) == before


@pytest.mark.parametrize('damage', ['other_owner', 'missing_mapping', 'wrong_task', 'missing_reservation',
                                    'slot_token', 'request_token', 'both_tokens'])
def test_paid_history_links_cannot_reduce_experiment_spend(tmp_path, damage):
    from tests.test_model_comparison_admission_budget import request_units
    units = request_units(tmp_path)
    ledger, old, _ = setup_guard(tmp_path, cap=10)
    policy = replace(old, experiment_id='limited', experiment_usd_ceiling=str(units / 1_000_000_000))
    register(ledger, policy)
    guard = ComparisonCallGuard(ledger=ledger, policy=policy)
    call(guard, 'paid')
    with ledger._connect() as db:
        if damage == 'other_owner': db.execute("UPDATE policy_attempts SET role='study-1' WHERE task='model_comparison'")
        if damage == 'missing_mapping': db.execute("DELETE FROM policy_attempts WHERE task='model_comparison'")
        if damage == 'wrong_task': db.execute("UPDATE policy_attempts SET task='ordinary' WHERE task='model_comparison'")
        if damage == 'missing_reservation': db.execute('DELETE FROM reservations')
        if damage == 'slot_token': db.execute('UPDATE comparison_attempts SET reservation_token=NULL')
        if damage == 'request_token': db.execute('UPDATE comparison_requests SET reservation_token=NULL')
        if damage == 'both_tokens':
            db.execute('UPDATE comparison_requests SET reservation_token=NULL')
            db.execute('UPDATE comparison_attempts SET reservation_token=NULL')
    before = snapshot(ledger)
    with pytest.raises(BudgetBlocked): register(ledger, replace(old, experiment_id='third'))
    sent = []
    async def dispatch(req):
        sent.append(req)
        return await paid_once(req)
    with pytest.raises(BudgetBlocked): asyncio.run(guard.call(request(), dispatch=dispatch, attempt_id='next'))
    assert sent == [] and snapshot(ledger) == before


@pytest.mark.parametrize('damage', ['null_day', 'missing_column', 'missing_pk'])
def test_pair_admission_damage_cannot_revive_global_cap(tmp_path, damage):
    from tests.test_model_comparison_runner import setup, NOW as PAIR_NOW
    m, ledger, old_runner, spec, op, ge = setup(tmp_path, pairs_per_day=1)
    new = replace(old_runner.guard.policy, experiment_id='new')
    register(ledger, new)
    guard = ComparisonCallGuard(ledger=ledger, policy=new)
    runner = m.PairedComparisonRunner(guard=guard, adapter=old_runner.adapter, clock=lambda: PAIR_NOW)
    old_runner._claim(spec, PAIR_NOW)
    with ledger._connect() as db:
        if damage == 'null_day': db.execute('UPDATE comparison_pairs SET admission_day=NULL')
        if damage == 'missing_column': db.execute('ALTER TABLE comparison_pairs DROP COLUMN admission_day')
        if damage == 'missing_pk':
            rows = db.execute('SELECT * FROM comparison_pairs').fetchall()
            db.execute('DROP TABLE comparison_pairs')
            db.execute('CREATE TABLE comparison_pairs (pair_id TEXT, manifest TEXT NOT NULL, '
                       'requested_at TEXT NOT NULL, result TEXT, admission_day TEXT)')
            db.executemany('INSERT INTO comparison_pairs VALUES (?,?,?,?,?)', [tuple(r) for r in rows])
    before = snapshot(ledger)
    with pytest.raises(BudgetBlocked): register(ledger, replace(new, experiment_id='third'))
    with pytest.raises(BudgetBlocked): asyncio.run(runner.run(replace(spec, decision_id='new-pair')))
    with pytest.raises(BudgetBlocked): old_runner.admitted_pair_ids('2026-10-13')
    assert snapshot(ledger) == before and op.calls == [] and ge.calls == []


@pytest.mark.parametrize('damage', ['missing_slot', 'missing_table', 'registry_version_empty_day'])
def test_standalone_report_validates_before_empty_join(tmp_path, damage):
    ledger, old, guard = setup_guard(tmp_path)
    register(ledger, replace(old, experiment_id='new'))
    call(guard, 'paid')
    day = '2026-10-13'
    with ledger._connect() as db:
        if damage == 'missing_slot': db.execute('DELETE FROM comparison_attempts')
        if damage == 'missing_table': db.execute('DROP TABLE comparison_attempts')
        if damage == 'registry_version_empty_day':
            db.execute('UPDATE comparison_policy_lifecycle SET version=99')
            day = '2026-10-14'
    before = snapshot(ledger)
    with pytest.raises(BudgetBlocked): read_day_attempts(ledger.path, day)
    assert snapshot(ledger) == before


def test_unused_comparison_report_is_empty_without_schema_writes(tmp_path):
    ledger = MonthlyBudgetLedger(tmp_path / 'budget.db', 100, clock=lambda: NOW, policy_enabled=True)
    before = snapshot(ledger)
    assert read_day_attempts(ledger.path, '2026-10-13') == []
    assert snapshot(ledger) == before


def test_preidentity_paid_slot_keeps_anchor_cost_and_history(tmp_path):
    ledger, old, guard = setup_guard(tmp_path, cap=1)
    call(guard, 'old')
    # 신원 테이블 도입 이전 이력처럼 요청만 없고 슬롯·예약·비용 소유권은 남는다.
    with ledger._connect() as db: db.execute('DELETE FROM comparison_requests')
    before = snapshot(ledger)
    new = replace(old, experiment_id='new')
    register(ledger, new)
    assert all(snapshot(ledger)[table] == rows for table, rows in before.items())
    with pytest.raises(BudgetBlocked, match='comparison_daily_attempt_cap'):
        call(ComparisonCallGuard(ledger=ledger, policy=new), 'new')
    assert ledger.status()['spent_usd'] == 0.0001


def test_valid_pair_block_marker_survives_new_registration_and_report(tmp_path):
    from tests.test_model_comparison_runner import setup, NOW as PAIR_NOW
    m, ledger, runner, spec, _, _ = setup(tmp_path, pairs_per_day=2, max_common_context_bytes=10)
    with pytest.raises(BudgetBlocked, match='comparison_input_too_large'): runner._claim(spec, PAIR_NOW)
    before = snapshot(ledger)
    register(ledger, replace(runner.guard.policy, experiment_id='new'))
    assert all(snapshot(ledger)[table] == rows for table, rows in before.items())
    assert runner.admitted_pair_ids('2026-10-13') == set()
    assert read_day_attempts(ledger.path, '2026-10-13') == []


def test_malformed_pair_block_timestamp_is_not_an_unadmitted_marker(tmp_path):
    from tests.test_model_comparison_runner import setup, NOW as PAIR_NOW
    _, ledger, runner, spec, _, _ = setup(tmp_path, max_common_context_bytes=10)
    with pytest.raises(BudgetBlocked): runner._claim(spec, PAIR_NOW)
    with ledger._connect() as db:
        value = json.loads(db.execute('SELECT result FROM comparison_pairs').fetchone()[0])
        value['blocked_at'] = 'invalid'
        db.execute('UPDATE comparison_pairs SET requested_at=?,result=?', ('invalid', json.dumps(value)))
    before = snapshot(ledger)
    with pytest.raises(BudgetBlocked): register(ledger, replace(runner.guard.policy, experiment_id='new'))
    assert snapshot(ledger) == before
