"""Offline gate explanation never promotes missing evidence to a trade or profit."""
from copy import deepcopy
import importlib
from types import SimpleNamespace

import pytest
from test_entry_gate_trace import AT, setup_trace
from src.analytics import entry_observation as observation


def module():
    try:return importlib.import_module('src.analytics.entry_gate_report')
    except ModuleNotFoundError:pytest.fail('gate report missing')


def sample(monkeypatch):
    from src.analytics.entry_gate_trace import STAGES
    _,b,_,t=setup_trace(monkeypatch,count=3)
    for stage in STAGES[:7]:t.note(stage,'pass')
    t.note('score','fail',symbol='000000',value=74.8,threshold=75)
    for stage in STAGES[7:]:
        if stage=='signal_created':t.signal('000001','synthetic-event');break
        t.note(stage,'pass',symbol='000001')
    t.stop('000002','candidate_loop_limit_8',stage='batch_limit')
    t.finish()
    return b.export()


def test_whole_denominator_first_failure_and_later_not_reached(monkeypatch):
    r=module().build_gate_report(sample(monkeypatch),as_of=AT)
    assert r['counts']==dict(total=3,recorded=3,blocked=1,signal_created=1,not_reached=1,unknown=0,unavailable=0)
    a,b,c=r['candidates']
    assert a['first_observed_blocking_stage']=='score' and a['first_blocking_stage']=='score'
    assert a['stages']['scan_change']['status']=='not_reached'
    assert b['outcome']=='signal_created' and 'emitted' not in b
    assert c['outcome']=='not_reached'
    assert r['profit_comparison_available'] is False and r['production_eligible'] is False


def test_legacy_and_missing_records_are_not_zero_return(monkeypatch):
    m=module();b=observation.EntryObservationBuffer(evaluation_epoch='legacy',capacity=10)
    monkeypatch.setattr(observation,'_now',lambda:AT)
    observation.capture_scan(b,[SimpleNamespace(symbol='000001')],'regular')
    r=m.build_gate_report(b.export(),as_of=AT)
    assert r['counts']['unavailable']==1 and r['candidates'][0]['first_blocking_stage'] is None
    data=sample(monkeypatch);data['records'].pop()
    r=m.build_gate_report(data,as_of=AT)
    assert r['counts']['total']==3 and r['counts']['unknown']==1
    assert r['trace_coverage_complete'] is False


def test_unknown_before_known_failure_is_not_claimed_first_actual_gate(monkeypatch):
    data=sample(monkeypatch)
    data['records'][1]['steps'][5].update(status='unknown',reason='lookup_error')
    r=module().build_gate_report(data,as_of=AT)
    assert r['candidates'][0]['first_observed_blocking_stage']=='score'
    assert r['candidates'][0]['first_blocking_stage'] is None
    assert r['candidates'][0]['prior_evidence_complete'] is False


@pytest.mark.parametrize('stage',['batch_limit','signal_limit'])
@pytest.mark.parametrize('gap',[False,True])
def test_explicit_limit_accounts_for_stop_only_with_complete_prefix(monkeypatch,stage,gap):
    from src.analytics.entry_gate_trace import STAGES
    _,b,_,t=setup_trace(monkeypatch,count=1)
    for previous in STAGES[:STAGES.index(stage)]:
        if gap and previous=='regime':continue
        t.note(previous,'pass')
    t.stop('000000',stage,stage=stage);t.finish()
    data=b.export();before=deepcopy(data)
    r=module().build_gate_report(data,as_of=AT);c=r['candidates'][0]
    assert r['trace_coverage_complete'] is (not gap)
    assert c['prior_evidence_complete'] is (not gap)
    assert c['stages'][stage]['status']=='not_reached'
    assert c['first_blocking_stage'] is None and c['first_observed_blocking_stage'] is None
    assert data==before


@pytest.mark.parametrize('prefix',['empty','partial','complete'])
def test_emit_break_requires_actual_prior_batch_evidence(monkeypatch,prefix):
    from src.analytics.entry_gate_trace import STAGES
    _,b,_,t=setup_trace(monkeypatch,count=1)
    for stage in (STAGES[:STAGES.index('batch_limit')+1] if prefix=='complete'
                  else STAGES[:1] if prefix=='partial' else []):
        t.note(stage,'pass')
    t.stop('000000','emit_break');t.finish()
    r=module().build_gate_report(b.export(),as_of=AT)
    assert r['trace_coverage_complete'] is (prefix=='complete')
    assert r['candidates'][0]['prior_evidence_complete'] is (prefix=='complete')


def test_observations_after_journal_seal_are_rejected(monkeypatch):
    _,b,_,t=setup_trace(monkeypatch,count=1)
    t.note('enabled','fail');t.finish()
    data=b.export();data['journal']={'capture_closed_at':'2026-10-02T00:16:00+00:00'}
    with pytest.raises(ValueError):module().build_gate_report(data,as_of=AT)


def test_observations_exactly_at_journal_seal_are_valid(monkeypatch):
    _,b,_,t=setup_trace(monkeypatch,count=1)
    t.note('enabled','fail');t.finish()
    data=b.export();data['journal']={'capture_closed_at':AT}
    assert module().build_gate_report(data,as_of=AT)['trace_coverage_complete'] is True


@pytest.mark.parametrize('defect',['future','step_future','reverse','duplicate_step','orphan',
    'duplicate_candidate','duplicate_trace','bad_version','bad_stage','terminal','signal',
    'post_fail','extra_field','extra_step_field','nan','missing_marker','false_marker','sequence',
    'seal_future','time_naive','missing_step_field','reason_secret','wrong_terminal_reason'])
def test_contradictory_or_noncausal_trace_rejected(monkeypatch,defect):
    data=sample(monkeypatch);r=data['records'][1]
    if defect=='future':r['observed_at']='2099-01-01T00:00:00+00:00'
    elif defect=='step_future':r['steps'][0]['observed_at']='2099-01-01T00:00:00+00:00'
    elif defect=='reverse':r['steps'][:2]=r['steps'][1::-1]
    elif defect=='duplicate_step':r['steps'].insert(1,deepcopy(r['steps'][0]))
    elif defect=='orphan':r['candidate_id']='unknown:000000'
    elif defect=='duplicate_candidate':data['records'][0]['candidates'].append(deepcopy(data['records'][0]['candidates'][0]))
    elif defect=='duplicate_trace':data['records'].append(deepcopy(r));data['records'][-1]['sequence']=len(data['records'])
    elif defect=='bad_version':r['trace_version']='other'
    elif defect=='bad_stage':r['steps'][0]['stage']='invented'
    elif defect=='terminal':r['outcome']='signal_created'
    elif defect=='signal':r['signal_id']='unexpected-event'
    elif defect=='post_fail':r['steps'].append({**r['steps'][-1],'stage':'scan_change','status':'pass'})
    elif defect=='extra_field':r['raw_news']='not allowed'
    elif defect=='extra_step_field':r['steps'][0]['api_key']='synthetic'
    elif defect=='nan':r['steps'][0]['value']=float('nan')
    elif defect=='missing_marker':data['records'][0].pop('entry_gate_trace_expected')
    elif defect=='false_marker':data['records'][0]['entry_gate_trace_expected']=False
    elif defect=='sequence':r['sequence']=99
    elif defect=='seal_future':data['journal']={'capture_closed_at':'2099-01-01T00:00:00+00:00'}
    elif defect=='time_naive':r['observed_at']='2026-10-02T00:17:19'
    elif defect=='missing_step_field':r['steps'][0].pop('threshold')
    elif defect=='reason_secret':r['steps'][0]['reason']='arbitrary free text'
    elif defect=='wrong_terminal_reason':r['terminal_reason']='different_gate'
    with pytest.raises(ValueError):module().build_gate_report(data,as_of=AT)


def test_report_preserves_gaps_and_input_immutability(monkeypatch):
    data=sample(monkeypatch);data['complete']=False;data['incomplete_reasons']=['synthetic_gap']
    before=deepcopy(data);r=module().build_gate_report(data,as_of=AT)
    assert data==before and r['source_complete'] is False
    assert r['incomplete_reasons']==['synthetic_gap']


def test_conflicting_signal_and_blocked_trace_rejected(monkeypatch):
    data=sample(monkeypatch);cid=data['records'][1]['candidate_id']
    data['records'].append(dict(kind='signal',sequence=len(data['records'])+1,
        candidate_id=cid,signal_id='contradiction',observed_at=AT))
    with pytest.raises(ValueError):module().build_gate_report(data,as_of=AT)


@pytest.mark.asyncio
@pytest.mark.parametrize('binding',['valid','epoch','version','configuration','candidate_bound'])
async def test_explicit_journal_study_cli_round_trip_and_binding(tmp_path,monkeypatch,capsys,binding):
    import hashlib,json
    from src.analytics.entry_gate_trace import begin_gate_trace
    from src.analytics.entry_observation_journal import ObservationJournal
    from src.analytics import entry_observation_journal as journal_module
    from test_entry_gate_trace import settings
    try:cli=importlib.import_module('scripts.report_entry_gate_trace')
    except ModuleNotFoundError:pytest.fail('gate report CLI missing')
    monkeypatch.setattr(journal_module,'_timestamp',lambda:AT)
    monkeypatch.setattr(observation,'_now',lambda:AT)
    monkeypatch.setattr(importlib.import_module('src.analytics.entry_gate_trace'),'_now',lambda:AT)
    study=dict(evaluation_epoch='synthetic-gates-v3',as_of=AT,capture=dict(version='runner-first-scan-v3',
        entry_gate_trace=settings(),source_version_ref='synthetic-source-v3',configuration_ref='synthetic-config'))
    if binding=='epoch':study['evaluation_epoch']='wrong-epoch'
    if binding=='version':study['capture']['version']='runner-first-scan-v2'
    if binding=='configuration':study['capture']['configuration_ref']='other'
    if binding=='candidate_bound':study['capture']['entry_gate_trace']['max_candidates']=1
    raw=json.dumps(study).encode();study_path=tmp_path/'study.json';study_path.write_bytes(raw)
    path=tmp_path/'gates.jsonl'
    b=observation.EntryObservationBuffer(evaluation_epoch='synthetic-gates-v3',capacity=100,
        scan_scope='first',scan_admission_ref='synthetic',entry_gate_trace_settings=settings())
    j=await ObservationJournal.open(b,path,study_ref='synthetic',study_sha256=hashlib.sha256(raw).hexdigest(),
        queue_capacity=100,batch_size=10,max_bytes=1000000,max_record_bytes=65536)
    sid=observation.capture_scan(b,[SimpleNamespace(symbol='000001',score=74.8)] +
        ([SimpleNamespace(symbol='000002',score=70)] if binding=='candidate_bound' else []),'regular')
    t=begin_gate_trace(b,sid,[]);t.note('score','fail',symbol='000001',value=74.8,threshold=75);t.finish()
    await j.close();original=path.read_bytes()
    args=['--journal',str(path),'--study',str(study_path),'--as-of',AT,'--max-journal-bytes','1000000']
    if binding!='valid':
        assert cli.main(args)==2
        assert path.read_bytes()==original and study_path.read_bytes()==raw
        return
    assert cli.main(args)==0
    out=json.loads(capsys.readouterr().out)
    assert out['counts']['total']==1 and out['counts']['blocked']==1
    assert out['study_sha256']==hashlib.sha256(raw).hexdigest()
    assert out['source_authenticity_verified'] is False and out['profit_comparison_available'] is False
    assert path.read_bytes()==original and study_path.read_bytes()==raw
    study_path.write_bytes(raw+b' ')
    assert cli.main(args)==2


def test_price_assembly_accepts_valid_gate_trace_without_inferring_eligibility(monkeypatch):
    from src.analytics.entry_gate_trace import begin_gate_trace
    from src.analytics.received_entry_input import prepare_received_input
    from test_received_entry_shadow import received_payload
    _,b,_,t=setup_trace(monkeypatch,count=1)
    t.note('score','fail',symbol='000000',value=74.8,threshold=75);t.finish()
    c=received_payload();c.pop('opportunities');c['evaluation_epoch']='synthetic-gates-v3';c['as_of']=AT
    out=prepare_received_input(c,b.export(),[])
    assert out['ready_opportunities']==0 and len(out['payload']['opportunities'])==1
