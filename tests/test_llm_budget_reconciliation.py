"""Operator reconciliation uses synthetic evidence and temporary ledgers only."""
import hashlib
import importlib
import sqlite3

import pytest
from src.utils.llm_budget import MonthlyBudgetLedger, BudgetBlocked


def reconcile(*args, **kwargs):
    try:
        m = importlib.import_module('src.utils.llm_budget_reconciliation')
    except ImportError:
        pytest.fail('evidence-bound budget reconciliation missing')
    return m.reconcile_budget(*args, **kwargs)


def fixture(tmp_path):
    ledger = MonthlyBudgetLedger(tmp_path/'budget.sqlite3',100)
    token = ledger.reserve('gpt-5-mini','synthetic','',100)
    ledger.settle(token,outcome='unknown')
    evidence = tmp_path/'evidence.txt'; evidence.write_text('synthetic provider cost record')
    review = {'version':'llm-budget-reconciliation-v1','activated_at':ledger.status()['activated_at'],
        'reviewer_ref':'synthetic-operator','evidence_sha256':hashlib.sha256(evidence.read_bytes()).hexdigest(),
        'adjustments':[{'token':token,'actual_cost_usd':0.1}], 'clear_bound_breach':False}
    return ledger,evidence,review


def test_dry_run_never_changes_budget_or_creates_audit_table(tmp_path):
    ledger,evidence,review=fixture(tmp_path)
    before=ledger.path.read_bytes()
    out=reconcile(ledger.path,100,review,evidence)
    assert not out['applied'] and out['adjustment_count']==1
    assert out['new_cost_usd']==0.1
    assert ledger.path.read_bytes()==before and ledger.status()['blocked']


def test_apply_requires_exact_dry_run_state_and_preserves_origin(tmp_path):
    ledger,evidence,review=fixture(tmp_path)
    preview=reconcile(ledger.path,100,review,evidence)
    applied=reconcile(ledger.path,100,review,evidence,apply=True,
                      expected_state_sha256=preview['state_sha256'])
    assert applied['applied']
    status=ledger.status()
    assert not status['blocked'] and status['spent_usd']==0.1
    assert status['activated_at']==review['activated_at']
    with sqlite3.connect(ledger.path) as db:
        row=db.execute('SELECT evidence_sha256,reviewer_ref FROM reconciliations').fetchone()
    assert row==(review['evidence_sha256'],'synthetic-operator')
    with pytest.raises(ValueError):
        reconcile(ledger.path,100,review,evidence,apply=True,expected_state_sha256=preview['state_sha256'])


@pytest.mark.parametrize('defect',['hash','origin','duplicate','negative','bool','extra','state'])
def test_invalid_or_stale_adjustment_never_changes_ledger(tmp_path,defect):
    ledger,evidence,review=fixture(tmp_path)
    before=ledger.path.read_bytes()
    if defect=='hash':review['evidence_sha256']='0'*64
    elif defect=='origin':review['activated_at']='wrong'
    elif defect=='duplicate':review['adjustments']*=2
    elif defect=='negative':review['adjustments'][0]['actual_cost_usd']=-1
    elif defect=='bool':review['adjustments'][0]['actual_cost_usd']=True
    elif defect=='extra':review['approve_anything']=True
    with pytest.raises((ValueError,BudgetBlocked)):
        reconcile(ledger.path,100,review,evidence,apply=True,expected_state_sha256='f'*64)
    assert ledger.path.read_bytes()==before


def test_active_reservations_and_known_settlements_cannot_be_rewritten(tmp_path):
    ledger,evidence,review=fixture(tmp_path)
    with sqlite3.connect(ledger.path) as db:
        db.execute("UPDATE reservations SET state='settled',actual_units=100000000")
    token=ledger.reserve('gpt-5-mini','synthetic','',100)
    review['adjustments']=[{'token':token,'actual_cost_usd':0.1}]
    with pytest.raises(ValueError,match='active'):
        reconcile(ledger.path,100,review,evidence)
    ledger.settle(token,outcome='known',actual_cost_usd=0.0001)
    with pytest.raises(ValueError,match='unresolved'):
        reconcile(ledger.path,100,review,evidence)


def test_unknown_cost_above_limit_is_recorded_but_does_not_reopen_budget(tmp_path):
    ledger,evidence,review=fixture(tmp_path)
    review['adjustments'][0]['actual_cost_usd']=101
    preview=reconcile(ledger.path,100,review,evidence)
    reconcile(ledger.path,100,review,evidence,apply=True,expected_state_sha256=preview['state_sha256'])
    assert ledger.status()['spent_usd']==101
    assert ledger.status()['block_reason']=='monthly_limit_exceeded'


def test_breach_acknowledgment_keeps_known_charge_and_requires_no_unresolved(tmp_path):
    ledger,evidence,review=fixture(tmp_path)
    with sqlite3.connect(ledger.path) as db:
        db.execute('UPDATE settings SET bound_breach=1')
    review['clear_bound_breach']=True
    preview=reconcile(ledger.path,100,review,evidence)
    reconcile(ledger.path,100,review,evidence,apply=True,expected_state_sha256=preview['state_sha256'])
    assert not ledger.status()['bound_breach'] and ledger.status()['spent_usd']==0.1


def test_missing_ledger_is_never_initialized_by_reconciliation(tmp_path):
    path=tmp_path/'missing.sqlite3'
    evidence=tmp_path/'evidence';evidence.write_text('synthetic')
    with pytest.raises((ValueError,FileNotFoundError)):
        reconcile(path,100,{},evidence)
    assert not path.exists()


def test_breach_acknowledgment_cannot_reduce_already_known_cost(tmp_path):
    ledger=MonthlyBudgetLedger(tmp_path/'budget.sqlite3',100)
    token=ledger.reserve('gpt-5-mini','synthetic','',100)
    ledger.settle(token,outcome='known',actual_cost_usd=1)
    evidence=tmp_path/'evidence';evidence.write_text('synthetic verified known charge')
    review={'version':'llm-budget-reconciliation-v1','activated_at':ledger.status()['activated_at'],
        'reviewer_ref':'synthetic-operator','evidence_sha256':hashlib.sha256(evidence.read_bytes()).hexdigest(),
        'adjustments':[], 'clear_bound_breach':True}
    before=ledger.status()
    preview=reconcile(ledger.path,100,review,evidence)
    reconcile(ledger.path,100,review,evidence,apply=True,expected_state_sha256=preview['state_sha256'])
    assert ledger.status()['spent_usd']==before['spent_usd']==1
    assert not ledger.status()['blocked']


def test_cli_defaults_to_preview_and_requires_state_for_apply(tmp_path):
    import json,subprocess,sys
    from pathlib import Path
    ledger,evidence,review=fixture(tmp_path)
    review_path=tmp_path/'review.json';review_path.write_text(json.dumps(review))
    script=Path(__file__).resolve().parents[1]/'scripts/reconcile_llm_budget.py'
    args=[sys.executable,str(script),'--ledger',str(ledger.path),'--limit-usd','100',
          '--review',str(review_path),'--evidence',str(evidence)]
    before=ledger.path.read_bytes()
    completed=subprocess.run(args,capture_output=True,text=True,check=True)
    assert json.loads(completed.stdout)['applied'] is False
    refused=subprocess.run(args+['--apply'],capture_output=True,text=True)
    assert refused.returncode!=0 and 'expected-state-sha256' in refused.stderr
    assert ledger.path.read_bytes()==before


def test_failed_second_adjustment_rolls_back_all_costs_and_audit(tmp_path):
    ledger=MonthlyBudgetLedger(tmp_path/'budget.sqlite3',100)
    a=ledger.reserve('gpt-5-mini','synthetic-a','',100)
    b=ledger.reserve('gpt-5-mini','synthetic-b','',100)
    ledger.settle(a,outcome='unknown');ledger.settle(b,outcome='unknown')
    evidence=tmp_path/'evidence';evidence.write_text('synthetic two-charge evidence')
    review={'version':'llm-budget-reconciliation-v1','activated_at':ledger.status()['activated_at'],
        'reviewer_ref':'synthetic-operator','evidence_sha256':hashlib.sha256(evidence.read_bytes()).hexdigest(),
        'adjustments':[{'token':a,'actual_cost_usd':0.1},{'token':b,'actual_cost_usd':0.2}],
        'clear_bound_breach':False}
    with sqlite3.connect(ledger.path) as db:
        db.execute("CREATE TRIGGER synthetic_failure BEFORE UPDATE ON reservations WHEN NEW.token='"+b+
                   "' BEGIN SELECT RAISE(ABORT,'synthetic write failure'); END")
    preview=reconcile(ledger.path,100,review,evidence)
    with pytest.raises(sqlite3.IntegrityError):
        reconcile(ledger.path,100,review,evidence,apply=True,expected_state_sha256=preview['state_sha256'])
    assert ledger.status()['unresolved_count']==2 and ledger.status()['spent_usd']==0
    with sqlite3.connect(ledger.path) as db:
        assert db.execute("SELECT name FROM sqlite_master WHERE name='reconciliations'").fetchall()==[]
