"""Opt-in actual gate records: no trading or external state."""
from copy import deepcopy
from types import SimpleNamespace
import importlib

import pytest

from src.analytics import entry_observation as observation

AT = '2026-10-02T00:17:19+00:00'


def module():
    try:
        return importlib.import_module('src.analytics.entry_gate_trace')
    except ModuleNotFoundError:
        pytest.fail('gate trace module missing')


def settings():
    return dict(version='entry-gate-trace-v1', policy_ref='kr-live-screening-gates-v1',
                max_candidates=100, source_version_ref='synthetic-source-v3',
                configuration_ref='synthetic-config')


def setup_trace(monkeypatch, *, count=2, capacity=1000):
    m=module()
    monkeypatch.setattr(m, '_now', lambda: AT)
    monkeypatch.setattr(observation, '_now', lambda: AT)
    b=observation.EntryObservationBuffer(evaluation_epoch='synthetic-gates-v3',capacity=capacity,
        scan_scope='first',scan_admission_ref='synthetic-first',entry_gate_trace_settings=settings())
    stocks=[SimpleNamespace(symbol=f'{i:06}',score=70+i,change_pct=6) for i in range(count)]
    sid=observation.capture_scan(b,stocks,'regular')
    return m,b,sid,m.begin_gate_trace(b,sid,stocks)


def rows(b):
    return [r for r in b.export()['records'] if r['kind']=='entry_gate_trace']


def test_disabled_trace_never_reads_stocks_and_preserves_result_identity():
    m=module()
    class Unreadable:
        def __iter__(self): raise AssertionError('must not inspect')
    t=m.begin_gate_trace(None,None,Unreadable())
    marker=object()
    assert t.check('score',marker) is marker
    t.note('score','fail');t.stop('x','synthetic');t.signal('x','s');t.finish()


def test_first_actual_failure_preserved_with_original_denominator(monkeypatch):
    m,b,sid,t=setup_trace(monkeypatch)
    t.note('enabled','pass')
    t.note('score','fail',symbol='000000',value=70,threshold=75)
    t.note('score','pass',symbol='000001',value=90,threshold=75)
    t.note('scan_change','fail',symbol='000001',value=6,threshold=5)
    t.note('strategy','pass')
    t.finish();t.finish()
    out=rows(b)
    assert len(out)==2
    assert [r['candidate_id'] for r in out]==[f'{sid}:000000',f'{sid}:000001']
    assert [r['outcome'] for r in out]==['blocked','blocked']
    assert [[s['stage'] for s in r['steps']] for r in out]==[
        ['enabled','score'],['enabled','score','scan_change']]
    assert b.export()['records'][0]['entry_gate_trace_expected'] is True


def test_partial_unknown_and_signal_creation_not_emission(monkeypatch):
    _,b,_,t=setup_trace(monkeypatch,count=3)
    t.note('regime','unknown',reason='lookup_error')
    t.note('score','fail',symbol='000000',value=74.8,threshold=75)
    t.signal('000001','synthetic-event')
    t.note('quote_fetch','unknown',symbol='000002',reason='quote_error')
    t.finish('cancelled')
    a,c,u=rows(b)
    assert a['outcome']=='blocked'
    assert c['outcome']=='signal_created' and c['signal_id']=='synthetic-event'
    assert u['outcome']=='unknown' and u['terminal_reason']=='cancelled'
    assert 'emitted' not in c


def test_limit_is_not_a_failed_policy_condition(monkeypatch):
    _,b,_,t=setup_trace(monkeypatch)
    t.stop('000000','candidate_loop_limit_8',stage='batch_limit')
    t.stop('000001','quote_error',stage='quote_fetch',outcome='unknown')
    t.finish()
    a,b=rows(b)
    assert a['outcome']=='not_reached' and not any(s['status']=='fail' for s in a['steps'])
    assert b['outcome']=='unknown'


def test_invalid_duplicate_step_marks_incomplete_without_escaping(monkeypatch):
    _,b,_,t=setup_trace(monkeypatch)
    t.note('score','pass',symbol='000000')
    t.note('score','pass',symbol='000000')
    t.note('enabled','pass',symbol='000001',value={'secret':'never copied'})
    t.finish()
    assert b.capture_status()['complete'] is False
    assert 'secret' not in str(rows(b))


def test_publish_failure_and_throwing_facade_do_not_escape(monkeypatch):
    m,b,_,t=setup_trace(monkeypatch)
    def broken(*a,**kw): raise RuntimeError('synthetic observer failure')
    monkeypatch.setattr(b,'publish',broken)
    t.finish()
    assert b.capture_status()['complete'] is False
    assert m.safe_trace_call(SimpleNamespace(note=broken),'note','score','pass') is None


def test_only_first_cohort_once_and_no_closed_capture(monkeypatch):
    m,b,sid,t=setup_trace(monkeypatch)
    assert m.begin_gate_trace(b,sid,[]) is m.NULL_TRACE
    b._capture_closed=True
    t.finish()
    assert not rows(b)
    assert m.begin_gate_trace(b,sid,[]) is m.NULL_TRACE


def test_overflow_keeps_missing_trace_visible(monkeypatch):
    _,b,_,t=setup_trace(monkeypatch,capacity=2)
    t.note('enabled','fail');t.finish()
    assert len(rows(b))==1 and b.capture_status()['dropped_records']==1
    assert b.capture_status()['complete'] is False


@pytest.mark.parametrize('change',[
    dict(max_candidates=True),dict(max_candidates=101),dict(version='wrong'),
    dict(policy_ref='wrong'),dict(source_version_ref=''),dict(configuration_ref='x'*201),dict(extra=True),
])
def test_settings_require_fixed_explicit_bounded_contract(change):
    m=module()
    with pytest.raises(ValueError):m.validate_settings({**settings(),**change})


def test_settings_and_records_are_copied(monkeypatch):
    m,b,_,t=setup_trace(monkeypatch)
    s=settings();normalized=m.validate_settings(s);s['max_candidates']=1
    assert normalized['max_candidates']==100
    t.note('score','fail',symbol='000000',value=float('nan'),threshold=75)
    t.finish();out=rows(b)
    assert out[0]['steps'][0]['value'] is None
    out[0]['steps'].clear()
    assert rows(b)[0]['steps']


def test_begin_is_safe_even_when_observer_settings_access_raises():
    m=module()
    class Broken(observation.EntryObservationBuffer):
        @property
        def entry_gate_trace_settings(self): raise RuntimeError('synthetic')
    assert m.begin_gate_trace(object.__new__(Broken),'scan',[]) is m.NULL_TRACE


@pytest.mark.asyncio
async def test_gate_records_survive_actual_journal_allowlist(tmp_path,monkeypatch):
    from src.analytics.entry_observation_journal import ObservationJournal,read_observation_journal
    m=module();monkeypatch.setattr(m,'_now',lambda:AT);monkeypatch.setattr(observation,'_now',lambda:AT)
    b=observation.EntryObservationBuffer(evaluation_epoch='synthetic-gates-v3',capacity=100,
        scan_scope='first',scan_admission_ref='synthetic',entry_gate_trace_settings=settings())
    path=tmp_path/'gates.jsonl'
    j=await ObservationJournal.open(b,path,study_ref='synthetic',study_sha256='a'*64,
        queue_capacity=100,batch_size=10,max_bytes=1000000,max_record_bytes=65536)
    sid=observation.capture_scan(b,[SimpleNamespace(symbol='000001',score=74.8)],'regular')
    t=m.begin_gate_trace(b,sid,[]);t.note('score','fail',symbol='000001',value=74.8,threshold=75);t.finish()
    close=await j.close()
    assert close['sealed'] and close['fsync_confirmed']
    out=read_observation_journal(path,max_bytes=1000000,expected_study_sha256='a'*64)
    assert out['complete'] and out['records'][1]['steps'][0]['threshold']==75


def gate_plan_change(c):
    c['capture'].update(version='runner-first-scan-v3',
        selection_basis={'version':'selection-basis-v2','max_candidates':2,'max_source_terms':16},
        entry_gate_trace={**settings(),'max_candidates':2,
            'source_version_ref':c['capture']['source_version_ref'],
            'configuration_ref':c['capture']['configuration_ref']},
        max_record_bytes=65536,max_bytes=1000000)


def test_v3_plan_explicit_contract_and_old_versions_stay_disabled(tmp_path):
    from test_entry_observation_runtime import load
    plan,_=load(tmp_path,gate_plan_change)
    assert plan.settings['entry_gate_trace']['version']==module().VERSION


@pytest.mark.parametrize('defect',['source','configuration','budget','record','candidate_bound','v2_extra'])
def test_v3_plan_rejects_unbound_or_underfunded_gate_trace(tmp_path,defect):
    from test_entry_observation_runtime import load
    def change(c):
        gate_plan_change(c);s=c['capture']
        if defect=='source':s['entry_gate_trace']['source_version_ref']='other'
        if defect=='configuration':s['entry_gate_trace']['configuration_ref']='other'
        if defect=='budget':s['queue_capacity']=4;s['batch_size']=4
        if defect=='record':s['max_record_bytes']=32768
        if defect=='candidate_bound':s['entry_gate_trace']['max_candidates']=1
        if defect=='v2_extra':s['version']='runner-first-scan-v2'
    with pytest.raises(ValueError):load(tmp_path,change)


@pytest.mark.asyncio
async def test_v3_runtime_installs_gate_contract_and_persists_it(tmp_path,monkeypatch):
    from test_entry_observation_runtime import load,owner,NOW,read,module as runtime_module
    m=module();plan,_=load(tmp_path,gate_plan_change);clock=[NOW]
    monkeypatch.setattr(observation,'_now',lambda:clock[0].isoformat())
    monkeypatch.setattr(m,'_now',lambda:clock[0].isoformat())
    runtime=await runtime_module().ObservationRuntime.install(owner(),plan,now=lambda:clock[0])
    clock[0]=plan.start_at
    sid=observation.capture_scan(runtime.buffer,[SimpleNamespace(symbol='000001',score=74.8)],'regular')
    t=m.begin_gate_trace(runtime.buffer,sid,[])
    t.note('score','fail',symbol='000001',value=74.8,threshold=75);t.finish()
    clock[0]=plan.end_at
    await runtime.close()
    out=read(plan)
    assert out['records'][0]['entry_gate_source_version_ref']==plan.settings['source_version_ref']
    assert len([r for r in out['records'] if r['kind']=='entry_gate_trace'])==1


def test_candidate_limit_failure_does_not_silently_shrink_cohort(monkeypatch):
    from src.analytics.entry_gate_report import build_gate_report
    m,b,_,trace=setup_trace(monkeypatch,count=101)
    assert trace is m.NULL_TRACE
    out=build_gate_report(b.export(),as_of=AT)
    assert out['counts']['total']==101 and out['counts']['unknown']==101
    assert out['source_complete'] is False
