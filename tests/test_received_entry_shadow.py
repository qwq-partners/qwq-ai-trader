"""수신 정보 기준 진단과 주문 생성 직전 정책 관측. 모든 입력은 합성이다."""
from copy import deepcopy
from datetime import datetime
from decimal import Decimal
import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT=Path(__file__).resolve().parents[1]
for p in (ROOT,ROOT/'tests'):
    if str(p) not in sys.path: sys.path.insert(0,str(p))

from test_entry_price_shadow import payload
from src.analytics.entry_price_shadow import build_report
from src.analytics import entry_observation as obs


def received_payload():
    d=payload()
    d['data_basis']='received_snapshot'
    d['received_policy']={'version':'received-v1','max_rest_rtt_seconds':3,'max_target_age_seconds':30}
    r=d['opportunities'][0]
    r['target']={'price':'11000','basis':'received_intraday_high','source':'KIS_FHKST01010100',
                 'source_as_of':None,'requested_at':'2026-09-30T09:59:49+09:00',
                 'received_at':'2026-09-30T09:59:50+09:00'}
    r['quote']={'quote_id':'quote-one','ask':'10000','bid':'9990','ask_size':100,
                'tr_id':'H0STASP0','message_count':1,'source_as_of':None,
                'received_at':'2026-09-30T10:00:01+09:00','observed_at':'2026-09-30T10:00:02+09:00',
                'session':'KRX_REGULAR_CONTINUOUS','session_evidence_ref':'synthetic-session-proof',
                'selection_rule':'first_recorded_krx_after_decision'}
    return d


def test_received_information_is_separate_from_market_clock_without_invented_time():
    d=received_payload(); before=deepcopy(d)
    report=build_report(d); row=report['opportunities'][0]
    assert row['gate_status']=='allow'
    assert row['max_ask_whole_krw']=='10232'
    assert report['data_basis']=='received_snapshot'
    assert report['data_quality']=='received_snapshot_price_proxy'
    assert report['received_policy']['version']=='received-v1'
    assert report['production_eligible'] is False
    assert d==before


@pytest.mark.parametrize('part,field,value',[
    ('target','received_at','2026-09-30T10:00:01+09:00'),
    ('target','requested_at','2026-09-30T09:59:40+09:00'),
    ('target','received_at','2026-09-30T09:59:48+09:00'),
    ('target','source_as_of','2026-09-30T09:59:49+09:00'),
    ('target','basis','intraday_high_at_decision'),
    ('quote','tr_id','H0NXASP0'), ('quote','message_count',2),
    ('quote','received_at','2026-09-30T09:59:59+09:00'),
    ('quote','received_at','2026-09-30T10:00:01'),
    ('quote','observed_at','2026-09-30T10:00:08+09:00'),
    ('quote','session_evidence_ref',None),('quote','session','VI'),
    ('quote','selection_rule','best_later_quote'),('quote','ask_size',99),
])
def test_received_mode_rejects_time_quality_and_selection_violations(part,field,value):
    d=received_payload();d['opportunities'][0][part][field]=value
    r=build_report(d)
    assert r['counts']['unknown']==1
    assert r['summary']['complete_delta_net_pnl'] is None


def test_stale_target_is_not_an_exact_current_high_or_silent_fallback():
    d=received_payload();d['received_policy']['max_target_age_seconds']=5
    assert build_report(d)['counts']['unknown']==1
    d=received_payload();d['data_basis']='market_timestamp'
    assert build_report(d)['counts']['unknown']==1
    d=payload();d['data_basis']='received_snapshot'
    with pytest.raises(ValueError):build_report(d)
    d=received_payload();d['opportunities'][0]['data_basis']='market_timestamp'
    with pytest.raises(ValueError):build_report(d)


def test_order_ready_capture_uses_effective_and_sizing_stops_separately(monkeypatch,tmp_path):
    from src.core.event import SignalEvent
    from src.core.types import Order,OrderSide,StrategyType
    from src.strategies.exit_manager import ExitConfig,ExitManager
    monkeypatch.setattr(Path,'home',classmethod(lambda cls:tmp_path))
    em=ExitManager(ExitConfig(stop_loss_pct=5,min_stop_pct=4))
    em._intraday_crash_level='test-crash'
    import src.strategies.exit_manager as exits
    monkeypatch.setitem(exits.INTRADAY_CRASH_PARAMS,'test-crash',{'stop_loss_pct':2.5})
    buffer=obs.EntryObservationBuffer(evaluation_epoch='test-v1',capacity=10)
    owner=SimpleNamespace(_entry_price_observer=buffer,exit_manager=em,
                          _strategy_exit_params={'gap_and_go':{'stop_loss_pct':3.5}})
    event=SignalEvent(symbol='S',strategy=StrategyType.GAP_AND_GO,source='live_screening')
    order=Order(symbol='S',side=OrderSide.BUY,quantity=100,price=Decimal('10000'),strategy='gap_and_go')
    assert hasattr(obs,'capture_order_ready'),'정책 관측 미구현'
    before=deepcopy(event)
    obs.capture_order_ready(owner,event,order)
    row=buffer.export()['records'][0]
    assert row['kind']=='order_ready' and row['signal_id']==event.id
    assert row['requested_quantity']==100
    assert row['risk_stop_pct']=='3.5' and row['effective_stop_pct']=='2.5'
    assert row['crash_cap_applied'] is True
    assert row['base_stop_source']=='strategy' and row['effective_stop_source']=='crash_cap'
    assert row['stop_basis']=='net_pnl' and row['fill_applied'] is False
    assert row['capital_budget'] is None
    assert row['transport_status']=='not_observed'
    assert event==before and not em.get_state('S')


def test_actual_risk_manager_observes_only_after_order_creation(monkeypatch,tmp_path):
    from test_risk_sizing import _rm,_em,_sig,STRATEGY_EXIT_PARAMS
    from test_entry_risk_lifecycle import _order_path
    monkeypatch.setattr(Path,'home',classmethod(lambda cls:tmp_path))
    em=_em();rm=_rm(monkeypatch,mode='risk',em=em)
    buffer=obs.EntryObservationBuffer(evaluation_epoch='test-v1',capacity=10)
    rm._entry_observation_owner=SimpleNamespace(_entry_price_observer=buffer,exit_manager=em,
                                                _strategy_exit_params=STRATEGY_EXIT_PARAMS)
    event=_sig(2.5);event.source='live_screening'
    orders,_=_order_path(monkeypatch,rm,event)
    rows=buffer.export()['records']
    assert len(rows)==1
    assert rows[0]['requested_quantity']==orders[0].order.quantity==139
    assert rows[0]['signal_id']==event.id
    assert event.symbol in rm._pending_orders
    assert rows[0]['effective_stop_pct']=='5.0'


def observed_bundle():
    context=received_payload();context.pop('opportunities')
    rows=[
        {'kind':'scan','scan_id':'scan','observed_at':'2026-09-30T09:59:45+09:00',
         'candidates':[{'candidate_id':'scan:SYNTH1','symbol':'SYNTH1'},
                       {'candidate_id':'scan:MISSING','symbol':'MISSING'}]},
        {'kind':'rest_quote','candidate_id':'scan:SYNTH1','requested_at':'2026-09-30T09:59:49+09:00',
         'observed_at':'2026-09-30T09:59:50+09:00','source':'KIS_FHKST01010100',
         'source_as_of':None,'quote':{'high':'11000'}},
        {'kind':'signal','candidate_id':'scan:SYNTH1','signal_id':'sig','strategy':'gap_and_go',
         'observed_at':'2026-09-30T09:59:55+09:00'},
        {'kind':'order_ready','signal_id':'sig','symbol':'SYNTH1','strategy':'gap_and_go',
         'observed_at':'2026-09-30T10:00:00+09:00','requested_quantity':100,
         'effective_stop_pct':'5','risk_stop_pct':'5','stop_snapshot_ref':'synthetic-stop',
         'stop_basis':'net_pnl','stop_resolved_at_stage':'order_ready','fill_applied':False,
         'transport_status':'not_observed'},
        {'kind':'ws_quote','quote_id':'quote-one','symbol':'SYNTH1','ask':'10000','bid':'9990','ask_size':100,
         'observed_at':'2026-09-30T10:00:02+09:00','provenance':{
             'tr_id':'H0STASP0','message_count':1,'source_as_of':None,
             'received_at':'2026-09-30T10:00:01+09:00'}},
    ]
    for i,r in enumerate(rows,1):r['sequence']=i
    observations={'schema_version':1,'evaluation_epoch':'test-v1','complete':True,'dropped_records':0,'records':rows}
    inputs=[{'opportunity_id':'scan:SYNTH1','capital_budget':'1100000','capital_budget_ref':'synthetic-budget',
             'exit_policy_ref':'synthetic-fixed-net5','policy_inputs_at':'2026-09-30T09:59:45+09:00',
             'quote_session':{'quote_id':'quote-one','session':'KRX_REGULAR_CONTINUOUS',
                              'evidence_ref':'synthetic-session-proof'}}]
    return context,observations,inputs


def test_native_received_assembly_freezes_quantity_and_keeps_missing_candidates():
    args=observed_bundle();before=deepcopy(args)
    result=obs.prepare_input(*args)
    r=result['payload']['opportunities'][0]
    assert result['ready_opportunities']==1
    assert result['report']['counts']['unknown']==1
    assert r['quantity']==100 and r['stop']['pct']=='5'
    assert r['target']['price']=='11000' and r['quote']['quote_id']=='quote-one'
    assert r['baseline_basis']=='engine_order_ready'
    assert result['report']['summary']['complete_delta_net_pnl'] is None
    assert args==before


@pytest.mark.parametrize('defect',['bad_first','late_target','missing_session','late_budget','overflow',
                                  'no_order','budget_missing','future_quote','bad_first_clock'])
def test_received_assembly_never_repairs_missing_or_bad_first_data(defect):
    c,o,i=observed_bundle();rows=o['records']
    if defect in ('bad_first','bad_first_clock'):
        later=deepcopy(rows[-1]);later['quote_id']='better-later';later['sequence']=6
        later['observed_at']='2026-09-30T10:00:03+09:00'
        rows.append(later)
        if defect=='bad_first':rows[-2]['ask_size']=1
        else:rows[-2]['provenance']['received_at']='not-a-time'
    elif defect=='late_target':rows[1]['observed_at']='2026-09-30T09:59:56+09:00'
    elif defect=='missing_session':i[0].pop('quote_session')
    elif defect=='late_budget':i[0]['policy_inputs_at']='2026-09-30T10:00:01+09:00'
    elif defect=='overflow':o.update(complete=False,dropped_records=1)
    elif defect=='no_order':rows.pop(3)
    elif defect=='budget_missing':i[0].pop('capital_budget')
    elif defect=='future_quote':rows[-1]['observed_at']='2026-09-30T16:00:01+09:00'
    for seq,row in enumerate(rows,1):row['sequence']=seq
    result=obs.prepare_input(c,o,i)
    assert result['ready_opportunities']==0
    assert result['report']['summary']['complete_delta_net_pnl'] is None


@pytest.mark.parametrize('defect',['sequence','duplicate_quote','orphan_signal','overwrite_quantity'])
def test_received_assembly_rejects_ambiguous_join_or_policy_overrides(defect):
    c,o,i=observed_bundle()
    if defect=='sequence':o['records'][-1]['sequence']=40
    if defect=='duplicate_quote':
        row=deepcopy(o['records'][-1]);row['sequence']=6;o['records'].append(row)
    if defect=='orphan_signal':o['records'][3]['signal_id']='unknown'
    if defect=='overwrite_quantity':i[0]['quantity']=999
    with pytest.raises(ValueError):obs.prepare_input(c,o,i)


@pytest.mark.parametrize('mode',['normal','broken','overflow','missing_exit'])
def test_real_order_and_pending_state_are_identical_when_observation_fails(monkeypatch,tmp_path,mode):
    from test_risk_sizing import _rm,_em,_sig,STRATEGY_EXIT_PARAMS
    from test_entry_risk_lifecycle import _order_path
    monkeypatch.setattr(Path,'home',classmethod(lambda cls:tmp_path))
    class Broken(obs.EntryObservationBuffer):
        def publish(self,record):raise RuntimeError('synthetic observer failure')
    buffer=(Broken if mode=='broken' else obs.EntryObservationBuffer)(evaluation_epoch='test-v1',capacity=1)
    if mode=='overflow':buffer.publish({'kind':'occupied'})
    fingerprints=[]
    template=_sig(2.5);template.source='live_screening'
    for observed in (False,True):
        em=_em();rm=_rm(monkeypatch,mode='risk',em=em)
        if observed:
            rm._entry_observation_owner=SimpleNamespace(
                _entry_price_observer=buffer,exit_manager=None if mode=='missing_exit' else em,
                _strategy_exit_params=STRATEGY_EXIT_PARAMS)
        event=deepcopy(template)
        events,_=_order_path(monkeypatch,rm,event)
        assert len(events)==1
        order=events[0].order
        fingerprints.append((order.symbol,order.side,order.order_type,order.price,order.quantity,order.strategy,
                             rm._pending_orders,rm._pending_quantities,rm._reserved_by_order,
                             rm._pending_strategy,rm._pending_signal_cache))
    assert fingerprints[0]==fingerprints[1]
    assert buffer.export()['complete'] is (mode=='normal')


@pytest.mark.parametrize('gate',['G3','pending_race'])
def test_rejected_or_racing_order_never_becomes_observed_approval(monkeypatch,tmp_path,gate):
    from test_risk_sizing import _rm,_em,_sig,STRATEGY_EXIT_PARAMS
    from test_entry_risk_lifecycle import _order_path
    monkeypatch.setattr(Path,'home',classmethod(lambda cls:tmp_path))
    em=_em();rm=_rm(monkeypatch,mode='risk',em=em)
    buffer=obs.EntryObservationBuffer(evaluation_epoch='test-v1',capacity=10)
    rm._entry_observation_owner=SimpleNamespace(_entry_price_observer=buffer,exit_manager=em,
                                                _strategy_exit_params=STRATEGY_EXIT_PARAMS)
    calculate=rm._calculate_position_size
    def sizing(signal):
        quantity=calculate(signal)
        def final_gate(*args,**kwargs):
            if gate=='pending_race':rm._pending_orders.add(signal.symbol)
            return (gate!='G3','synthetic rejection')
        rm.engine.can_open_position=final_gate
        return quantity
    rm._calculate_position_size=sizing
    event=_sig(2.5);event.source='live_screening'
    orders,_=_order_path(monkeypatch,rm,event)
    assert orders is None and buffer.export()['records']==[]
