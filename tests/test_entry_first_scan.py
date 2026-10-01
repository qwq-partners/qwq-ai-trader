"""첫 스캔 모집단 고정. 주문/네트워크 없이 실제 관측·구독·복원 경로를 검사한다."""
import asyncio
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.analytics import entry_observation as obs
from src.analytics.entry_observation_journal import ObservationJournal, read_observation_journal
from src.core.event import SignalEvent
from src.core.types import StrategyType
from src.data.feeds.quote_subscription import QuoteSubscriptionCoordinator, observe_screen_candidates


def first_buffer(capacity=100):
    return obs.EntryObservationBuffer(evaluation_epoch="test-v1", capacity=capacity,
                                      scan_scope="first", scan_admission_ref="synthetic-first-scan-v1")


def stocks(*symbols):
    return [SimpleNamespace(symbol=s, score=80, price=10000) for s in symbols]


def test_first_scan_preserves_all_candidates_and_ignores_later_empty_or_better_scans():
    b=first_buffer()
    original=stocks("900001","900002","900003")
    sid=obs.capture_scan(b,original,"regular")
    assert sid
    assert obs.capture_scan(b,[],"regular") is None
    assert obs.capture_scan(b,stocks("900004"),"regular") is None
    rows=b.export()['records']
    assert len(rows)==1 and len(rows[0]['candidates'])==3
    assert rows[0]['population_scope']=='first_returned_scan_candidates'
    assert rows[0]['scan_admission_ref']=='synthetic-first-scan-v1'
    assert b.export()['complete'] is True and b.export()['dropped_records']==0
    assert [s.symbol for s in original]==["900001","900002","900003"]


def test_empty_first_scan_is_not_replaced_by_later_opportunities():
    b=first_buffer()
    assert obs.capture_scan(b,[],"regular")
    assert obs.capture_scan(b,stocks("900001"),"regular") is None
    assert b.export()['records'][0]['candidates']==[]


@pytest.mark.parametrize('failure',['copy','capacity'])
def test_failed_first_scan_does_not_choose_a_more_convenient_later_scan(failure):
    b=first_buffer(capacity=1)
    if failure=='capacity': b.publish({'kind':'quote_subscription','status':'connected'})
    class Bad:
        @property
        def symbol(self): raise RuntimeError('synthetic copy failure')
    assert obs.capture_scan(b,[Bad()] if failure=='copy' else stocks("900001"),'regular') is None
    assert obs.capture_scan(b,stocks("900002"),'regular') is None
    assert b.export()['complete'] is False
    assert not any(r['kind']=='scan' for r in b.export()['records'])


def test_noncohort_order_does_not_read_stop_or_create_an_orphan_record():
    b=first_buffer(); obs.capture_scan(b,stocks("900001"),'regular')
    class Trap:
        def resolve_stop(self,**kwargs): raise AssertionError('outside cohort stop read')
    owner=SimpleNamespace(_entry_price_observer=b,exit_manager=Trap())
    event=SignalEvent(symbol='900002',strategy=StrategyType.GAP_AND_GO,source='live_screening')
    obs.capture_order_ready(owner,event,SimpleNamespace(side=SimpleNamespace(value='buy')))
    assert [r['kind'] for r in b.export()['records']]==['scan']
    assert b.export()['complete'] is True


@pytest.mark.asyncio
async def test_later_signals_still_reach_original_emit_but_never_enter_cohort():
    b=first_buffer(); first=obs.capture_scan(b,stocks('900001'),'regular')
    later=obs.capture_scan(b,stocks('900002'),'regular')
    seen=[]
    class Engine:
        async def emit(self,event): seen.append(event); return 'original-result'
    first_event=SignalEvent(symbol='900001',strategy=StrategyType.GAP_AND_GO,source='live_screening')
    later_event=SignalEvent(symbol='900002',strategy=StrategyType.GAP_AND_GO,source='live_screening')
    for event,sid in ((first_event,first),(later_event,later)):
        before=deepcopy(event)
        assert await obs.emit_with_observation(Engine(),event,b,sid)=='original-result'
        assert event==before
    assert seen==[first_event,later_event]
    rows=b.export()['records']
    assert [r['signal_id'] for r in rows if r['kind']=='signal']==[first_event.id]
    assert b.export()['complete'] is True


@pytest.mark.asyncio
async def test_first_scan_alone_enrolls_even_when_lease_capacity_has_spare_slots():
    b=first_buffer(); sent=[]
    owner=QuoteSubscriptionCoordinator(b,registration_cap=41,external_reserved=0,
        operational_headroom=0,max_candidates=5,lease_seconds=600,
        evidence_ref='synthetic-session',request_interval=0)
    async def send(action,key): sent.append((action,key))
    await owner.start(send)
    feed=SimpleNamespace(_quote_subscription_owner=owner)
    for candidates in (stocks('900001'),stocks('900002')):
        sid=obs.capture_scan(b,candidates,'regular')
        observe_screen_candidates(feed,b,sid,candidates)
        if owner._tasks: await asyncio.gather(*list(owner._tasks))
    assert sent==[('subscribe',('H0STASP0','900001'))]
    assert owner.snapshot()['leases']==1
    assert len([r for r in b.export()['records'] if r['kind']=='scan'])==1


@pytest.mark.parametrize('kwargs',[
    {'scan_scope':'best'}, {'scan_scope':'first'},
    {'scan_scope':'first','scan_admission_ref':''},
    {'scan_scope':'first','scan_admission_ref':'x'*201},
    {'scan_scope':'all','scan_admission_ref':'misleading'},
])
def test_invalid_or_unreferenced_scope_is_rejected(kwargs):
    with pytest.raises(ValueError): obs.EntryObservationBuffer(evaluation_epoch='x',capacity=1,**kwargs)


def test_default_buffer_still_records_every_scan():
    b=obs.EntryObservationBuffer(evaluation_epoch='x',capacity=10)
    assert obs.capture_scan(b,stocks('900001'),'regular')
    assert obs.capture_scan(b,stocks('900002'),'regular')
    assert len(b.export()['records'])==2


@pytest.mark.asyncio
async def test_first_scan_roundtrip_and_received_report_keep_explicit_scope(tmp_path,monkeypatch):
    from test_received_entry_shadow import observed_bundle
    context,observations,inputs=observed_bundle()
    monkeypatch.setattr(obs,'_now',lambda:'2026-09-30T09:59:45+09:00')
    b=first_buffer(); raw=json.dumps(context).encode(); sha=hashlib.sha256(raw).hexdigest()
    path=tmp_path/'first.jsonl'
    journal=await ObservationJournal.open(b,path,study_ref='synthetic',study_sha256=sha,
        queue_capacity=50,batch_size=5,max_bytes=100000,max_record_bytes=10000)
    sid=obs.capture_scan(b,stocks('SYNTH1','MISSING'),'regular')
    for record in observations['records'][1:]:
        record=deepcopy(record); record.pop('sequence')
        if 'candidate_id' in record: record['candidate_id']=record['candidate_id'].replace('scan:',sid+':')
        b.publish(record)
    obs.capture_scan(b,stocks('LATER'),'regular')
    inputs[0]['opportunity_id']=inputs[0]['opportunity_id'].replace('scan:',sid+':')
    result=await journal.close()
    assert result['sealed'] and result['fsync_confirmed']
    loaded=read_observation_journal(path,max_bytes=100000,expected_study_sha256=sha)
    assert loaded['records']==b.export()['records']
    report=obs.prepare_input(context,loaded,inputs)
    assert report['population_scope']=='first_returned_scan_candidates'
    assert report['scan_admission_ref']=='synthetic-first-scan-v1'
    assert report['report']['counts']['total']==2
    assert report['ready_opportunities']==1 and report['report']['counts']['unknown']==1
    assert report['report']['summary']['complete_delta_net_pnl'] is None


@pytest.mark.parametrize('basis',['received_snapshot','market_timestamp'])
@pytest.mark.parametrize('defect',['mixed','two_first','missing_ref'])
def test_input_cannot_claim_one_scan_scope_for_mixed_or_unreferenced_population(basis,defect):
    from test_received_entry_shadow import observed_bundle
    from test_entry_price_shadow import payload
    context,observations,_=observed_bundle()
    if basis=='market_timestamp': context=payload();context.pop('opportunities')
    first=observations['records'][0]
    first.update(population_scope='first_returned_scan_candidates',scan_admission_ref='synthetic-ref')
    if defect=='missing_ref': first.pop('scan_admission_ref')
    else:
        second=deepcopy(first);second['scan_id']='next';second['candidates']=[]
        second['sequence']=len(observations['records'])+1
        if defect=='mixed': second['population_scope']='returned_screen_candidates';second.pop('scan_admission_ref')
        observations['records'].append(second)
    with pytest.raises(ValueError):obs.prepare_input(context,observations,[])


def test_no_scan_yet_is_not_a_complete_empty_cohort():
    b=first_buffer()
    assert b.export()['complete'] is False
    assert 'first_scan_not_recorded' in b.export()['incomplete_reasons']
    obs.capture_scan(b,[],'regular')
    assert b.export()['complete'] is True


def test_same_symbol_later_signal_cannot_join_via_candidate_or_order_id():
    b=first_buffer();sid=obs.capture_scan(b,stocks('900001'),'regular')
    first=SignalEvent(symbol='900001',strategy=StrategyType.GAP_AND_GO,source='live_screening')
    obs.capture_signal(b,sid,first)
    before=b.export()
    assert b.publish({'kind':'signal','candidate_id':'later:900001','signal_id':'later-signal'}) is False
    assert b.publish({'kind':'order_ready','signal_id':'later-signal','symbol':'900001'}) is False
    assert b.export()==before
    assert b.accepts_order_signal(first.id) is True


@pytest.mark.parametrize('admitted',[True,False])
def test_real_order_creation_stays_identical_with_first_scan_scope(monkeypatch,tmp_path,admitted):
    from test_risk_sizing import _rm,_em,_sig,STRATEGY_EXIT_PARAMS
    from test_entry_risk_lifecycle import _order_path
    monkeypatch.setattr(Path,'home',classmethod(lambda cls:tmp_path))
    event=_sig(2.5);event.source='live_screening'
    fingerprints=[]; b=first_buffer()
    sid=obs.capture_scan(b,stocks(event.symbol),'regular')
    if admitted: obs.capture_signal(b,sid,event)
    for observed in (False,True):
        em=_em();rm=_rm(monkeypatch,mode='risk',em=em)
        if observed:
            rm._entry_observation_owner=SimpleNamespace(_entry_price_observer=b,exit_manager=em,
                _strategy_exit_params=STRATEGY_EXIT_PARAMS)
        events,_=_order_path(monkeypatch,rm,deepcopy(event))
        order=events[0].order
        fingerprints.append((order.symbol,order.side,order.order_type,order.price,order.quantity,order.strategy,
            rm._pending_orders,rm._pending_quantities,rm._reserved_by_order,rm._pending_strategy,rm._pending_signal_cache))
    assert fingerprints[0]==fingerprints[1]
    assert len([r for r in b.export()['records'] if r['kind']=='order_ready'])==int(admitted)
    assert b.export()['complete'] is True
