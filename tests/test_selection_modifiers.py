"""Raw supply and ordered score evidence stay outside trading decisions."""
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import pytest
from test_selection_source_basis import screener, stock, run
from src.analytics.selection_basis import validate_settings, validate_basis

SETTINGS = dict(version='selection-basis-v3', max_candidates=100, max_source_terms=16, max_modifier_stages=24)

def test_v3_is_explicit_and_historical_settings_do_not_accept_extra_keys():
    assert validate_settings(SETTINGS) == SETTINGS
    for version in ('selection-basis-v1','selection-basis-v2'):
        with pytest.raises(ValueError): validate_settings({**SETTINGS, 'version': version})


def test_ordered_news_merge_and_modifier_accounting_preserves_scores(monkeypatch):
    s = screener(monkeypatch, {'screen_volume_surge':[stock()]})
    class Theme:
        def get_all_stock_sentiments(self):
            return {'000001':dict(direction='bullish',impact=5,reason='synthetic')}
    plain=run(s,theme_detector=Theme())
    out=run(s,theme_detector=Theme(),capture_selection=True,capture_selection_sources=True,capture_selection_modifiers=True)
    a,b=asdict(plain[0]),asdict(out[0]);a.pop('selection_basis');b.pop('selection_basis')
    assert a==b
    basis=out[0].selection_basis
    assert basis['version']=='kr-selection-basis-v2'
    assert basis['modifier_sum']==15
    news=next(x for x in basis['modifier_stages'] if x['stage_id']=='theme_or_llm_news_bonus')
    assert (news['before'],news['after'],news['merge_delta'],news['delta'])==(50,89,24,15)
    assert basis['pre_normalization_score']==99
    validate_basis(basis,score=out[0].score,rank=1,observed_at=datetime.now(timezone.utc).isoformat())


def test_corrected_unknown_absence_does_not_claim_historical_shadow_comparison(monkeypatch):
    from src.signals.screener.kr_screener import StockScreener
    s=screener(monkeypatch,{'screen_volume_surge':[stock(score=30,change_pct=5)]})
    s._apply_spdi_filter=StockScreener._apply_spdi_filter.__get__(s)
    out=run(s,capture_selection=True,capture_selection_modifiers=True)
    assert out[0].score==15
    shadow=out[0].selection_basis['same_input_shadow']
    assert shadow['status']=='inputs_incomplete'
    assert shadow['actual_after'] is shadow['shadow_after'] is None
    assert shadow['final_score_delta'] is shadow['gate_delta'] is shadow['economic_effect'] is None


def test_v3_buffer_report_declares_missing_modifiers_and_never_upgrades_v1(monkeypatch):
    from src.analytics.entry_observation import EntryObservationBuffer,capture_scan
    from src.analytics.selection_basis import build_selection_report
    b=EntryObservationBuffer(evaluation_epoch='synthetic',capacity=200,selection_basis_settings=SETTINGS)
    out=run(screener(monkeypatch,{'screen_volume_surge':[stock()]}),capture_selection=True,capture_selection_modifiers=True)
    capture_scan(b,out,'regular')
    report=build_selection_report(b.export())
    assert report['modifier_evidence']['candidates']==1
    assert report['modifier_evidence']['supply_states']=={'positive':0,'observed_nonpositive':0,'unobserved':2}
    assert report['modifier_evidence']['removed_candidate_details_captured'] is False
    assert b.selection_modifiers_capture_enabled is True


@pytest.mark.asyncio
async def test_v3_journal_roundtrip_keeps_nested_evidence(tmp_path,monkeypatch):
    from src.analytics.entry_observation import EntryObservationBuffer,capture_scan
    from src.analytics.entry_observation_journal import ObservationJournal,read_observation_journal
    b=EntryObservationBuffer(evaluation_epoch='synthetic',capacity=250,selection_basis_settings=SETTINGS)
    path=tmp_path/'v3.jsonl'
    j=await ObservationJournal.open(b,path,study_ref='synthetic',study_sha256='a'*64,
        queue_capacity=250,batch_size=25,max_bytes=8000000,max_record_bytes=65536)
    stocks=await screener(monkeypatch,{'screen_volume_surge':[stock(f'{i+1:06d}') for i in range(100)]}).screen_all(capture_selection=True,capture_selection_modifiers=True)
    capture_scan(b,stocks,'regular')
    closed=await j.close()
    assert closed['sealed'] and closed['fsync_confirmed']
    loaded=read_observation_journal(path,max_bytes=8000000,expected_study_sha256='a'*64)
    assert loaded['complete'] and len(loaded['records'])==101
    assert loaded['records'][1]['modifier_sum']==0


def provider_fixture(monkeypatch, raw, *, cached=False):
    from src.data.providers import kis_market_data as mod
    from test_selection_source_instrumentation import _Response
    p=object.__new__(mod.KISMarketData)
    p._cache,p._cache_ts,p._cache_maxsize={},{},2000
    p._token_manager=SimpleNamespace(base_url='http://synthetic')
    row=dict(mksc_shrn_iscd='000001',hts_kor_isnm='synthetic',acml_vol='100',prdy_vol='100',stck_prpr='10000')
    if raw!='MISSING':row['ntby_qty']=raw
    calls=[]
    class Session:
        def get(self,*a,**kw):calls.append(kw['params']);return _Response(200,{'rt_cd':'0','output':[row]})
    async def session():return Session()
    async def headers(*a):return {}
    async def acquire():return None
    p._get_session,p._get_headers=session,headers
    monkeypatch.setattr(mod.kis_rate_limit,'acquire',acquire)
    if cached:
        p._cache={'foreign_inst_0001_1':[dict(symbol='000001',net_buy_qty=0)]}
        p._cache_ts={'foreign_inst_0001_1':datetime.now()}
    return p,calls


@pytest.mark.parametrize('raw,want,value',[('1','positive',1),('0','observed_nonpositive',0),('-1','observed_nonpositive',-1),('MISSING','unobserved',None),('','unobserved',None),('x','unobserved',None),(True,'unobserved',None),('1000000000001','unobserved',None)])
def test_raw_presence_not_normalized_default_establishes_supply(monkeypatch,raw,want,value):
    p,calls=provider_fixture(monkeypatch,raw)
    s=screener(monkeypatch,{'screen_volume_surge':[stock()]})
    async def foreign(*a,**kw):await p.fetch_foreign_institution(market='0001',investor='1');return []
    s.screen_foreign_buying=foreign
    out=run(s,capture_selection=True,capture_selection_modifiers=True)
    evidence=out[0].selection_basis['supply_evidence']['foreign']
    assert (evidence['state'],evidence['value'])==(want,value)
    assert len(calls)==1
    assert evidence['source_as_of'] is None


def test_provider_normalized_cache_zero_is_unobserved_and_no_extra_request(monkeypatch):
    p,calls=provider_fixture(monkeypatch,'0',cached=True)
    s=screener(monkeypatch,{'screen_volume_surge':[stock()]})
    async def foreign(*a,**kw):await p.fetch_foreign_institution(market='0001',investor='1');return []
    s.screen_foreign_buying=foreign
    out=run(s,capture_selection=True,capture_selection_modifiers=True)
    assert out[0].selection_basis['supply_evidence']['foreign']['state']=='unobserved'
    assert calls==[]


def test_v3_cached_trace_never_leaks_into_historical_contract(monkeypatch):
    sources={'screen_volume_surge':[stock()]};s=screener(monkeypatch,sources)
    first=run(s,capture_selection=True,capture_selection_modifiers=True)
    sources.clear()
    old=run(s,capture_selection=True)
    assert old[0].selection_basis['version']=='kr-selection-basis-v1'
    assert 'modifier_stages' not in old[0].selection_basis
    again=run(s,capture_selection=True,capture_selection_modifiers=True)
    assert again[0].selection_basis['supply_evidence']==first[0].selection_basis['supply_evidence']
    assert again[0].selection_basis['basis_id']==first[0].selection_basis['basis_id']
    assert again[0].selection_basis['cache_fallback'] is True


def test_throwing_observer_hook_cannot_change_score(monkeypatch):
    from src.signals.screener import kr_screener as m
    s=screener(monkeypatch,{'screen_volume_surge':[stock()]})
    def fail(*a):raise RuntimeError('observer failure')
    monkeypatch.setattr(m,'_capture_modifier',fail,raising=False)
    out=run(s,capture_selection=True,capture_selection_modifiers=True)
    assert out[0].score==50
    assert out[0].selection_basis['version']=='kr-selection-basis-v1'


@pytest.mark.parametrize('defect',['unknown_field','missing','duplicate','order','nan','delta','sum','unit','semantics','raw_missing','future','shadow_no_native','shadow_final','shadow_floor','oversized','historical_extra'])
def test_strict_nested_tampering_is_rejected(monkeypatch,defect):
    from src.signals.screener.kr_screener import StockScreener
    s=screener(monkeypatch,{'screen_volume_surge':[stock(change_pct=5)]})
    s._apply_spdi_filter=StockScreener._apply_spdi_filter.__get__(s)
    basis=deepcopy(run(s,capture_selection=True,capture_selection_modifiers=True)[0].selection_basis)
    if defect=='unknown_field':basis['supply_evidence']['foreign']['raw_payload']='no'
    elif defect=='missing':basis.pop('modifier_sum')
    elif defect=='duplicate':basis['modifier_stages'][1]=deepcopy(basis['modifier_stages'][0])
    elif defect=='order':basis['modifier_stages'].reverse()
    elif defect=='nan':basis['modifier_stages'][0]['before']=float('nan')
    elif defect=='delta':basis['modifier_stages'][0]['delta']=1
    elif defect=='sum':basis['modifier_sum']=1
    elif defect=='unit':basis['supply_evidence']['foreign']['unit']='money'
    elif defect=='semantics':basis['supply_evidence']['foreign']['semantics']='estimated'
    elif defect=='raw_missing':basis['supply_evidence']['foreign'].update(state='observed_nonpositive',value=0)
    elif defect=='future':basis['supply_evidence']['foreign'].update(state='positive',value=1,origin='current_raw_row',market_scope='0001',captured_at='2099-01-01T00:00:00+00:00')
    elif defect=='shadow_no_native':basis['same_input_shadow']['native_evaluated']=False
    elif defect=='shadow_final':basis['same_input_shadow']['final_score_delta']=20
    elif defect=='shadow_floor':basis['same_input_shadow']['shadow_after']=100
    elif defect=='oversized':basis['modifier_stages']*=100
    else:basis['version']='kr-selection-basis-v1'
    with pytest.raises(ValueError):validate_basis(basis,score=15,rank=1,observed_at=datetime.now(timezone.utc).isoformat())


def test_noop_spdi_does_not_claim_actual_execution(monkeypatch):
    s=screener(monkeypatch,{'screen_volume_surge':[stock(change_pct=5)]})
    out=run(s,capture_selection=True,capture_selection_modifiers=True)
    basis=out[0].selection_basis
    assert basis['same_input_shadow']['status']=='inputs_incomplete'
    assert next(r for r in basis['modifier_stages'] if r['stage_id']=='spdi')['status']=='not_evaluated'


@pytest.mark.parametrize('method',['__init__','begin','end','merge','raw','finish','spdi'])
def test_observer_internal_failures_leave_business_result_unchanged(monkeypatch,method):
    from src.analytics.selection_modifiers import ModifierCapture
    from src.signals.screener.kr_screener import StockScreener
    p,calls=provider_fixture(monkeypatch,'0')
    s=screener(monkeypatch,{'screen_volume_surge':[stock(change_pct=5)]})
    s._apply_spdi_filter=StockScreener._apply_spdi_filter.__get__(s)
    async def foreign(*a,**kw):await p.fetch_foreign_institution(market='0001',investor='1');return []
    s.screen_foreign_buying=foreign
    def fail(*a):raise RuntimeError('synthetic observer failure')
    monkeypatch.setattr(ModifierCapture,method,fail)
    out=run(s,capture_selection=True,capture_selection_modifiers=True)
    assert out[0].score==50 and len(calls)==1
    assert out[0].selection_basis['version']=='kr-selection-basis-v1'


@pytest.mark.asyncio
async def test_runtime_readiness_exposes_v3_limits_only_when_opted_in(tmp_path):
    from test_llm_capture_readiness import fixed_llm
    from test_entry_observation_runtime import load
    from test_market_state_pilot_runtime import bot_with
    from src.analytics.entry_observation_runtime import ObservationRuntime
    def change(c):
        fixed_llm(c)
        c['capture']['selection_basis']=SETTINGS
    plan,_=load(tmp_path,change)
    bot=bot_with([])
    bot.engine=SimpleNamespace(risk_manager=SimpleNamespace(_entry_observation_owner=bot))
    r=await ObservationRuntime.install(bot,plan,now=lambda:plan.start_at-timedelta(seconds=1))
    try:
        readiness=r.llm_readiness()
        assert readiness['selection_basis_version']=='selection-basis-v3'
        assert readiness['selection_max_modifier_stages']==24
    finally:await r.close('test_finished')


@pytest.mark.asyncio
async def test_context_scope_resets_after_business_exception_and_cancellation(monkeypatch):
    import asyncio
    from src.analytics import selection_modifiers as mod
    s=screener(monkeypatch,{'screen_volume_surge':[stock()]})
    async def failed(*a):raise RuntimeError('business failure')
    s._apply_valuation_bonus=failed
    with pytest.raises(RuntimeError,match='business failure'):
        await s.screen_all(capture_selection=True,capture_selection_modifiers=True)
    assert mod._ACTIVE.get() is None and mod._SOURCE.get() is None
    async def canceled(*a):raise asyncio.CancelledError()
    s._apply_valuation_bonus=canceled
    with pytest.raises(asyncio.CancelledError):
        await s.screen_all(capture_selection=True,capture_selection_modifiers=True)
    assert mod._ACTIVE.get() is None and mod._SOURCE.get() is None


@pytest.mark.parametrize('foreign,inst,change,base,want_actual,want_shadow',[
    (False,False,5,100,None,None),(False,False,5,10,None,None),
    (True,False,2,100,62,62),(True,True,2,100,80,80),
    (True,False,8,10,0,0),(False,False,2,100,50,50)])
def test_spdi_shadow_uses_actual_stage_floor_and_legacy_flags(monkeypatch,foreign,inst,change,base,want_actual,want_shadow):
    from src.signals.screener.kr_screener import StockScreener
    s=screener(monkeypatch,{'screen_volume_surge':[stock(score=base,change_pct=change,has_foreign_buying=foreign,has_inst_buying=inst)]})
    s._apply_spdi_filter=StockScreener._apply_spdi_filter.__get__(s)
    result=run(s,capture_selection=True,capture_selection_modifiers=True)
    b=result[0].selection_basis;shadow=b['same_input_shadow']
    assert shadow['actual_after']==want_actual and shadow['shadow_after']==want_shadow
    validate_basis(b,score=result[0].score,rank=1,observed_at=datetime.now(timezone.utc).isoformat())


def test_observer_only_rows_cannot_become_scoring_inputs(monkeypatch):
    from src.analytics.selection_modifiers import mark_candidate_supply
    from src.signals.screener.kr_screener import StockScreener
    s=screener(monkeypatch,{'screen_volume_surge':[stock(change_pct=5)]})
    s._apply_spdi_filter=StockScreener._apply_spdi_filter.__get__(s)
    async def foreign(*a,**kw):mark_candidate_supply('1','0001','000001','0');return []
    async def inst(*a,**kw):mark_candidate_supply('2','0001','000001','-1');return []
    s.screen_foreign_buying,s.screen_institutional_buying=foreign,inst
    out=run(s,capture_selection=True,capture_selection_modifiers=True)
    shadow=out[0].selection_basis['same_input_shadow']
    assert out[0].score == 50
    assert shadow['status'] == 'inputs_incomplete'
    assert shadow['actual_after'] is shadow['shadow_after'] is None


def test_conflicting_markets_and_wrong_source_binding_stay_unknown(monkeypatch):
    from src.analytics.selection_modifiers import mark_candidate_supply
    from src.signals.screener.kr_screener import StockScreener
    s=screener(monkeypatch,{'screen_volume_surge':[stock(change_pct=5)]})
    s._apply_spdi_filter=StockScreener._apply_spdi_filter.__get__(s)
    async def foreign(*a,**kw):
        mark_candidate_supply('1','0001','000001','0')
        mark_candidate_supply('1','0002','000001','1')
        mark_candidate_supply('2','0001','000001','0')  # investor/source mismatch
        return []
    s.screen_foreign_buying=foreign
    result=run(s,capture_selection=True,capture_selection_modifiers=True)
    b=result[0].selection_basis
    assert b['supply_evidence']['foreign']['origin']=='conflict'
    assert b['supply_evidence']['institution']['state']=='unobserved'
    assert b['modifier_trace_complete'] is False
    assert b['same_input_shadow']['status']=='inputs_incomplete'
    validate_basis(b,score=result[0].score,rank=1,observed_at=datetime.now(timezone.utc).isoformat())


def test_partial_source_keeps_valid_row_but_not_absent_candidate(monkeypatch):
    from src.analytics.selection_modifiers import mark_candidate_supply
    from src.analytics.selection_source_status import mark_source
    s=screener(monkeypatch,{'screen_volume_surge':[stock(),stock('000002')]})
    async def foreign(*a,**kw):
        mark_source('payload',count=1)
        mark_candidate_supply('1','0001','000001','0')
        mark_source('error',reason='http')
        return [stock(has_foreign_buying=True)]
    s.screen_foreign_buying=foreign
    out=run(s,capture_selection=True,capture_selection_sources=True,capture_selection_modifiers=True)
    assert next(r for r in out.selection_sources['runs'] if r['source_id']=='kis_foreign_buying')['outcome']=='partial'
    assert [x.selection_basis['supply_evidence']['foreign']['state'] for x in out]==['observed_nonpositive','unobserved']


def test_naver_late_birth_affine_normalization_and_llm_merge_reconcile(monkeypatch):
    s=screener(monkeypatch,{'screen_volume_surge':[stock(score=240)],'naver_volume_rank':[stock('000002',score=20)]})
    async def high(*a,**kw):return []
    async def news(*a,**kw):return [stock('000003',score=10)]
    s.naver_new_high=high;s.extract_stocks_from_news=news
    out=run(s,use_naver=True,llm_manager=object(),news_titles=['synthetic'],capture_selection=True,capture_selection_modifiers=True)
    assert [x.symbol for x in out]==['000001','000003','000002']
    assert out[0].score==100 and out[-1].score==30
    for rank,x in enumerate(out,1):
        validate_basis(x.selection_basis,score=x.score,rank=rank,observed_at=datetime.now(timezone.utc).isoformat())
    assert out[1].selection_basis['modifier_sum']==15
    assert out[-1].selection_basis['modifier_sum']==0


def test_raw_candidate_cap_makes_evidence_unavailable_without_truncating_results(monkeypatch):
    from src.analytics.selection_modifiers import mark_candidate_supply
    s=screener(monkeypatch,{'screen_volume_surge':[stock()]})
    async def foreign(*a,**kw):
        for i in range(513):mark_candidate_supply('1','0001',f'{i+1:06d}','0')
        return []
    s.screen_foreign_buying=foreign
    out=run(s,capture_selection=True,capture_selection_modifiers=True)
    assert len(out)==1 and out[0].score==50
    assert out[0].selection_basis['version']=='kr-selection-basis-v1'


def test_journal_shape_rejects_missing_nested_fields_before_write(monkeypatch):
    from src.analytics.entry_observation_journal import _record_shape
    s=screener(monkeypatch,{'screen_volume_surge':[stock()]})
    basis=run(s,capture_selection=True,capture_selection_modifiers=True)[0].selection_basis
    row=dict(kind='selection_basis',sequence=1,candidate_id='synthetic:000001',symbol='000001',
             observed_at=datetime.now(timezone.utc).isoformat(),basis_status='observed',**basis)
    row['supply_evidence']['foreign'].pop('legacy_flag')
    with pytest.raises(ValueError):_record_shape(row,65536)


def test_report_rejects_supply_timestamp_outside_source_call(monkeypatch):
    from src.analytics.entry_observation import EntryObservationBuffer,capture_scan
    from src.analytics.selection_basis import build_selection_report
    p,calls=provider_fixture(monkeypatch,'0')
    s=screener(monkeypatch,{'screen_volume_surge':[stock()]})
    async def foreign(*a,**kw):await p.fetch_foreign_institution(market='0001',investor='1');return []
    s.screen_foreign_buying=foreign
    out=run(s,capture_selection=True,capture_selection_sources=True,capture_selection_modifiers=True)
    b=EntryObservationBuffer(evaluation_epoch='synthetic',capacity=200,selection_basis_settings=SETTINGS)
    capture_scan(b,out,'regular')
    observation=b.export()
    observation['records'][1]['supply_evidence']['foreign']['captured_at']='2000-01-01T00:00:00+00:00'
    with pytest.raises(ValueError):build_selection_report(observation)


def test_v3_observer_failure_publishes_readable_unavailable_basis(monkeypatch):
    from src.analytics.entry_observation import EntryObservationBuffer,capture_scan
    from src.analytics.selection_basis import build_selection_report
    from src.analytics.selection_modifiers import ModifierCapture
    def fail(*a):raise RuntimeError('synthetic')
    monkeypatch.setattr(ModifierCapture,'begin',fail)
    stocks=run(screener(monkeypatch,{'screen_volume_surge':[stock()]}),capture_selection=True,capture_selection_modifiers=True)
    b=EntryObservationBuffer(evaluation_epoch='synthetic',capacity=200,selection_basis_settings=SETTINGS)
    capture_scan(b,stocks,'regular')
    observations=b.export();report=build_selection_report(observations)
    assert observations['complete'] is False
    assert observations['records'][1]['basis_status']=='unavailable'
    assert report['counts']['unavailable']==1
    assert report['modifier_evidence']['candidates']==0
    assert report['capture_complete'] is False


def test_screener_cache_legacy_positive_flag_is_not_numeric_evidence(monkeypatch):
    s=screener(monkeypatch,{'screen_foreign_buying':[stock(has_foreign_buying=True)]})
    stocks=run(s,capture_selection=True,capture_selection_modifiers=True)
    row=stocks[0].selection_basis['supply_evidence']['foreign']
    assert row['legacy_flag'] is True and row['state']=='unobserved' and row['value'] is None


def test_supply_marker_rejects_zero_symbol(monkeypatch):
    from src.analytics.selection_modifiers import ModifierCapture
    c=ModifierCapture()
    c.raw('1','0001','000000','0','kis_foreign_buying')
    assert c.rows=={}
