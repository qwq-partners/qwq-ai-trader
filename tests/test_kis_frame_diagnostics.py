"""합성 프레임만 사용하는 선택 진단/원장 계약 회귀."""
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import importlib
import json

import pytest

from src.analytics.entry_observation import EntryObservationBuffer
from src.data.feeds.quote_subscription import QuoteSubscriptionCoordinator
from test_quote_subscription import make_feed, book_frame

SETTINGS = {'version': 'kis-frame-diagnostics-v1'}


def module():
    try:
        return importlib.import_module('src.analytics.kis_frame_diagnostics')
    except ModuleNotFoundError:
        pytest.fail('선택 frame diagnostics 모듈 필요')


def buffer(enabled=True, capacity=100):
    return EntryObservationBuffer(evaluation_epoch='synthetic', capacity=capacity,
        **({'frame_diagnostics_settings': SETTINGS} if enabled else {}))


def owner_for(b, clock=None):
    return QuoteSubscriptionCoordinator(b, registration_cap=41, external_reserved=0,
        operational_headroom=0, max_candidates=5, lease_seconds=60,
        evidence_ref='synthetic', request_interval=0,
        **({'monotonic': lambda: clock[0]} if clock is not None else {}))


@pytest.mark.parametrize('frame,reason,tr,count_class,count,encryption,scope', [
    ('0|H0STASP0', 'malformed_frame', 'H0STASP0', 'invalid', None, 'plain', 'candidate_tr_unattributed'),
    ('0|H0STASP0|bad|005930^private', 'invalid_frame_count', 'H0STASP0', 'invalid', None, 'plain', 'candidate_tr_unattributed'),
    (book_frame().replace('|001|','|002|'), 'unsupported_frame_shape', 'H0STASP0', 'multiple', 2, 'plain', 'candidate_tr_unattributed'),
    (book_frame().replace('|001|','|000|'), 'unsupported_frame_shape', 'H0STASP0', 'out_of_range', None, 'plain', 'candidate_tr_unattributed'),
    (book_frame().replace('0|','1|',1), 'unsupported_frame_shape', 'H0STASP0', 'single', 1, 'encrypted', 'candidate_tr_unattributed'),
    ('0|H0STASP0|001|005930^private', 'short_orderbook_frame', 'H0STASP0', 'single', 1, 'plain', 'candidate_lease'),
    (book_frame().replace('10100','private'), 'orderbook_parse_failed', 'H0STASP0', 'single', 1, 'plain', 'candidate_lease'),
    ('0|H0NXCNT0|2|005930^private', 'unsupported_frame_shape', 'H0NXCNT0', 'multiple', 2, 'plain', 'non_candidate_tr'),
    ('0|secret-server-tr|2|005930^private', 'unsupported_frame_shape', 'other', 'multiple', 2, 'plain', 'unknown_tr'),
])
@pytest.mark.asyncio
async def test_actual_feed_adds_one_bounded_diagnostic_without_callbacks(frame,reason,tr,count_class,count,encryption,scope):
    f=make_feed(); b=buffer()
    f.enable_quote_observation(b, registration_cap=41, external_reserved=0, operational_headroom=0,
        max_candidates=5, lease_seconds=60, evidence_ref='synthetic', request_interval=0)
    owner=f._quote_subscription_owner
    await owner.enroll('scan',['005930'])
    await f.connect()
    before=len(b.export()['records']); events=[]
    async def collect(event): events.append(event)
    f.on_quote(collect); f.on_market_data(collect)
    await f._handle_message(frame,socket=f._ws,generation=owner.generation)
    rows=b.export()['records'][before:]
    assert len(rows)==1 and rows[0]['reason']==reason
    detail=rows[0]['frame_diagnostic']
    assert detail==dict(version=SETTINGS['version'],tr_class=tr,count_class=count_class,
        count=count,encryption=encryption,scope=scope)
    assert 'private' not in json.dumps(detail) and '005930' not in json.dumps(detail)
    assert events==[] and b.export()['complete'] is False
    await f.disconnect()


@pytest.mark.asyncio
async def test_scope_uses_current_lease_not_ack_and_does_not_expire_state():
    b=buffer(); clock=[0.]; o=owner_for(b,clock)
    await o.enroll('scan',['005930'])
    o.frame_gap('short_orderbook_frame',tr_id='H0STASP0',count=1,encrypted='0',data='005930^x')
    assert b.export()['records'][-1]['frame_diagnostic']['scope']=='candidate_lease'
    clock[0]=60
    o.frame_gap('short_orderbook_frame',tr_id='H0STASP0',count=1,encrypted='0',data='005930^x')
    assert b.export()['records'][-1]['frame_diagnostic']['scope']=='unregistered'
    assert len(o._leases)==1  # 진단이 만료 처리/구독 상태를 변경하지 않는다.


@pytest.mark.asyncio
async def test_disabled_and_diagnostic_failure_preserve_original_gap(monkeypatch):
    old=buffer(False); owner_for(old).frame_gap('malformed_frame',parts=['private'])
    assert 'frame_diagnostic' not in old.export()['records'][-1]
    b=buffer(); o=owner_for(b)
    def fail(*args,**kwargs): raise ValueError('private')
    monkeypatch.setattr(module(),'classify_frame',fail)
    o.frame_gap('malformed_frame',parts=['private'])
    assert len(b.export()['records'])==1
    assert b.export()['incomplete_reasons']==['frame_diagnostic_failed','malformed_frame']
    assert 'private' not in json.dumps(b.export())


def v4_change(c):
    from test_entry_gate_trace import gate_plan_change
    gate_plan_change(c)
    c['capture'].update(version='runner-first-scan-v4',frame_diagnostics=deepcopy(SETTINGS))


def test_v4_plan_requires_explicit_diagnostics_and_preserves_legacy(tmp_path):
    from test_entry_observation_runtime import load
    plan,_=load(tmp_path,v4_change)
    assert plan.settings['frame_diagnostics']==SETTINGS


@pytest.mark.parametrize('defect',['missing','extra','version','v3'])
def test_manifest_rejects_implicit_or_invalid_opt_in(tmp_path,defect):
    from test_entry_observation_runtime import load
    def change(c):
        v4_change(c); s=c['capture']
        if defect=='missing':s.pop('frame_diagnostics')
        if defect=='extra':s['frame_diagnostics']['raw']=True
        if defect=='version':s['frame_diagnostics']['version']='other'
        if defect=='v3':s['version']='runner-first-scan-v3'
    with pytest.raises(ValueError):load(tmp_path,change)


@pytest.mark.asyncio
async def test_journal_v2_roundtrip_and_offline_binding(tmp_path,capsys):
    from src.analytics.entry_observation_journal import ObservationJournal,read_observation_journal
    script=importlib.import_module('scripts.report_kis_frame_diagnostics')
    from test_entry_gate_trace import settings
    study={'evaluation_epoch':'synthetic','capture':{'version':'runner-first-scan-v4','frame_diagnostics':SETTINGS,
        'selection_basis':{'version':'selection-basis-v2','max_candidates':2,'max_source_terms':16},
        'entry_gate_trace':settings(),'source_version_ref':'synthetic-source-v3','configuration_ref':'synthetic-config'}}
    raw=json.dumps(study).encode(); p=tmp_path/'study.json';p.write_bytes(raw)
    sha=hashlib.sha256(raw).hexdigest(); b=buffer();path=tmp_path/'journal.jsonl'
    journal=await ObservationJournal.open(b,path,study_ref='synthetic',study_sha256=sha,
        queue_capacity=100,batch_size=10,max_bytes=1000000,max_record_bytes=10000)
    owner_for(b).frame_gap('invalid_frame_count',parts=['0','H0STASP0','bad','private'])
    assert (await journal.close())['sealed']
    obs=read_observation_journal(path,max_bytes=1000000,expected_study_sha256=sha)
    assert obs['frame_diagnostics']==SETTINGS
    assert obs['journal']['format']=='entry-observation-journal-v2'
    args=['--journal',str(path),'--study',str(p),'--as-of',datetime.now(timezone.utc).isoformat(),'--max-journal-bytes','1000000']
    assert script.main(args)==0
    out=json.loads(capsys.readouterr().out)
    assert out['counts']=={'connection_gaps':1,'eligible_frame_gaps':1,'available':1,'unavailable':0}
    assert out['stream_complete'] is False and out['study_binding_validated']
    assert path.read_bytes().startswith(b'{')


def observations():
    return {'schema_version':1,'evaluation_epoch':'synthetic','complete':False,
        'incomplete_reasons':['invalid_frame_count'],'dropped_records':0,
        'frame_diagnostics':deepcopy(SETTINGS), 'records':[{
            'kind':'quote_subscription','sequence':1,'observed_at':'2026-10-02T00:00:00+00:00',
            'status':'connection_gap','reason':'invalid_frame_count',
            'frame_diagnostic':dict(version=SETTINGS['version'],tr_class='H0STASP0',count_class='invalid',
                count=None,encryption='plain',scope='candidate_tr_unattributed')} ]}


@pytest.mark.parametrize('defect',['sequence','future','after_seal','bad_scope','bad_count','count_bool',
    'reason','extra','missing_optin','wrong_kind','negative_drop','false_complete','seal_future'])
def test_strict_report_rejects_contradictory_or_unbound_evidence(defect):
    obs=observations();r=obs['records'][0];d=r['frame_diagnostic']
    if defect=='sequence':r['sequence']=2
    elif defect=='future':r['observed_at']='2026-10-03T00:00:00Z'
    elif defect=='after_seal':obs['journal']={'capture_closed_at':'2026-10-01T23:59:59Z'}
    elif defect=='bad_scope':d['scope']='candidate_lease'
    elif defect=='bad_count':d['count']=1
    elif defect=='count_bool':d.update(count=True,count_class='single')
    elif defect=='reason':r['reason']='socket_closed'
    elif defect=='extra':d['raw']='private'
    elif defect=='missing_optin':obs.pop('frame_diagnostics')
    elif defect=='wrong_kind':r['kind']='scan'
    elif defect=='negative_drop':obs['dropped_records']=-1
    elif defect=='false_complete':obs.update(complete=True,incomplete_reasons=[])
    elif defect=='seal_future':obs['journal']={'capture_closed_at':'2026-10-03T00:00:00Z'}
    with pytest.raises(ValueError):module().build_frame_report(obs,as_of='2026-10-02T01:00:00Z')


def test_legacy_gaps_remain_unavailable_and_never_complete():
    obs=observations();obs.pop('frame_diagnostics');obs['records'][0].pop('frame_diagnostic')
    out=module().build_frame_report(obs,as_of='2026-10-02T01:00:00Z')
    assert out['counts']['unavailable']==1 and out['stream_complete'] is False


@pytest.mark.asyncio
async def test_direct_book_helper_does_not_invent_plain_frame():
    f=make_feed(); b=buffer(); o=owner_for(b); f._quote_subscription_owner=o
    await o.enroll('scan',['005930'])
    await f._handle_orderbook_data('005930^broken',tr_id='H0STASP0')
    d=b.export()['records'][-1]['frame_diagnostic']
    assert d['encryption']=='unknown' and d['scope']=='candidate_tr_unattributed'


@pytest.mark.asyncio
async def test_registered_non_candidate_and_closed_lease_are_not_candidates():
    b=buffer();o=owner_for(b)
    async def send(action,key):pass
    await o.start(send)
    await o.set_operational(['005930'],'H0STCNT0','H0STASP0')
    o.frame_gap('short_orderbook_frame',tr_id='H0STASP0',count=1,encrypted='0',data='005930^x')
    assert b.export()['records'][-1]['frame_diagnostic']['scope']=='registered_non_candidate'
    await o.enroll('scan',['005930']);o.end_observation()
    o.frame_gap('short_orderbook_frame',tr_id='H0STASP0',count=1,encrypted='0',data='005930^x')
    assert b.export()['records'][-1]['frame_diagnostic']['scope']=='registered_non_candidate'


def test_long_numeric_count_is_out_of_range_not_invalid():
    o=owner_for(buffer())
    d=module().classify_frame(owner=o,parts=['0','H0STASP0','9'*129,'005930^x'])
    assert d['count_class']=='out_of_range' and d['count'] is None


@pytest.mark.parametrize('defect',['missing_gate','missing_selection','source_mismatch','configuration_mismatch','legacy_journal','legacy_study'])
def test_v4_study_binding_checks_inherited_contract(defect):
    from test_entry_gate_trace import settings
    capture={'version':'runner-first-scan-v4','frame_diagnostics':SETTINGS,
        'selection_basis':{'version':'selection-basis-v2','max_candidates':2,'max_source_terms':16},
        'entry_gate_trace':settings(),'source_version_ref':'synthetic-source-v3','configuration_ref':'synthetic-config'}
    obs=observations();obs['journal']={'format':'entry-observation-journal-v2','frame_diagnostics':SETTINGS}
    if defect=='missing_gate':capture.pop('entry_gate_trace')
    elif defect=='missing_selection':capture.pop('selection_basis')
    elif defect=='source_mismatch':capture['source_version_ref']='other'
    elif defect=='configuration_mismatch':capture['configuration_ref']='other'
    elif defect=='legacy_journal':obs['journal'].pop('format')
    elif defect=='legacy_study':capture['version']='runner-first-scan-v3'
    with pytest.raises(ValueError):module().validate_study_binding(obs,{'evaluation_epoch':'synthetic','capture':capture})


def test_mixed_legacy_gap_reasons_and_loss_counts_are_preserved():
    obs=observations();obs.update(dropped_records=3)
    obs['journal']={'persistence_dropped_records':2,'sealed':True}
    obs['records'].append(dict(kind='quote_subscription',sequence=2,status='connection_gap',
        observed_at='2026-10-02T00:00:01Z',reason='socket_closed'))
    out=module().build_frame_report(obs,as_of='2026-10-02T01:00:00Z')
    assert out['counts']['connection_gaps']==2 and out['counts']['eligible_frame_gaps']==1
    assert len(out['gaps'])==2 and out['gaps'][1]['reason']=='socket_closed'
    assert out['dropped_records']==3 and out['persistence_dropped_records']==2


@pytest.mark.asyncio
async def test_full_buffer_and_closed_buffer_do_not_change_gap_semantics():
    b=buffer(capacity=1);o=owner_for(b)
    o.frame_gap('malformed_frame',parts=[]);o.frame_gap('malformed_frame',parts=[])
    assert len(b.export()['records'])==1 and b.export()['dropped_records']==1
    b._capture_closed=True;o.frame_gap('malformed_frame',parts=[])
    assert len(b.export()['records'])==1 and b.export()['dropped_records']==1


@pytest.mark.asyncio
async def test_reader_rejects_v1_header_with_diagnostic_and_v2_missing_optin(tmp_path):
    from src.analytics.entry_observation_journal import ObservationJournal,read_observation_journal,_json,_digest,ZERO_HASH
    b=buffer();path=tmp_path/'source.jsonl'
    j=await ObservationJournal.open(b,path,study_ref='synthetic',study_sha256='a'*64,
        queue_capacity=10,batch_size=1,max_bytes=1000000,max_record_bytes=10000)
    owner_for(b).frame_gap('malformed_frame',parts=[]);await j.close()
    original=[json.loads(line) for line in path.read_text().splitlines()]
    for case in ('v1','missing'):
        rows=deepcopy(original); rows[0]['payload'].pop('frame_diagnostics')
        if case=='v1':rows[0]['payload']['format']='entry-observation-journal-v1'
        previous=ZERO_HASH;raw=[]
        for row in rows:
            row.pop('hash');row['previous_hash']=previous;previous=_digest(row)
            raw.append(_json({**row,'hash':previous})+b'\n')
        forged=tmp_path/f'{case}.jsonl';forged.write_bytes(b''.join(raw))
        with pytest.raises(ValueError):read_observation_journal(forged,max_bytes=1000000,expected_study_sha256='a'*64)


def test_price_input_checks_diagnostic_contract_and_keeps_gap_incomplete():
    from src.analytics.entry_observation import prepare_input
    from test_received_entry_shadow import observed_bundle
    context,obs,inputs=observed_bundle()
    context['capture']={'source_version_ref':'synthetic-source','configuration_ref':'synthetic-config'}
    v4_change(context)
    obs['journal']={'format':'entry-observation-journal-v2','frame_diagnostics':deepcopy(SETTINGS)}
    obs['frame_diagnostics']=deepcopy(SETTINGS)
    row=observations()['records'][0];row['sequence']=len(obs['records'])+1
    row['observed_at']=obs['records'][-1]['observed_at'];obs['records'].append(row)
    obs.update(complete=False,incomplete_reasons=['invalid_frame_count'])
    out=prepare_input(context,obs,inputs)
    assert not out['capture_complete'] and out['ready_opportunities']==0
    row['frame_diagnostic']['scope']='candidate_lease'
    with pytest.raises(ValueError):prepare_input(context,obs,inputs)


@pytest.mark.parametrize('reason,changes',[
    ('unsupported_frame_shape',{}),
    ('malformed_frame',{'count_class':'single','count':1,'scope':'candidate_lease'}),
])
def test_report_rejects_diagnostic_impossible_at_original_branch(reason,changes):
    obs=observations(); row=obs['records'][0];row['reason']=reason;row['frame_diagnostic'].update(changes)
    with pytest.raises(ValueError):module().build_frame_report(obs,as_of='2026-10-02T01:00:00Z')


@pytest.mark.asyncio
async def test_v4_runtime_passes_opt_in_to_buffer_and_journal(tmp_path):
    from test_entry_observation_runtime import load,owner,NOW,module as runtime_module
    from src.analytics.entry_observation_journal import read_observation_journal
    plan,_=load(tmp_path,v4_change)
    runtime=await runtime_module().ObservationRuntime.install(owner(),plan,now=lambda:NOW)
    assert runtime.buffer.frame_diagnostics_settings==SETTINGS
    await runtime.close()
    obs=read_observation_journal(plan.settings['journal_path'],max_bytes=1000000,expected_study_sha256=plan.study_sha256)
    assert obs['journal']['format']=='entry-observation-journal-v2' and obs['frame_diagnostics']==SETTINGS


@pytest.mark.asyncio
async def test_normal_frames_and_callbacks_remain_identical_when_enabled():
    from test_quote_subscription import price_frame
    received=[]
    for enabled in (False,True):
        f=make_feed(); b=buffer(enabled)
        f.enable_quote_observation(b,registration_cap=41,external_reserved=0,operational_headroom=0,
            max_candidates=5,lease_seconds=60,evidence_ref='synthetic',request_interval=0)
        events=[]
        async def quote(event):events.append(('book',event.symbol,str(event.ask_price),str(event.bid_price)))
        async def price(event):events.append(('price',event.symbol,str(event.close)))
        f.on_quote(quote);f.on_market_data(price)
        await f.subscribe(['005930']);await f.connect()
        await f._handle_message(book_frame(),socket=f._ws,generation=f._quote_subscription_owner.generation)
        await f._handle_message(price_frame(),socket=f._ws,generation=f._quote_subscription_owner.generation)
        received.append(events)
        assert not any('frame_diagnostic' in r for r in b.export()['records'])
        await f.disconnect()
    assert received==[[('book','005930','10100','10000'),('price','005930','10000')]]*2


@pytest.mark.parametrize('direction',['v4_study_legacy_journal','legacy_study_v2_journal','legacy_study_v2_header_only'])
def test_price_bundle_rejects_mismatched_frame_contract(direction):
    from test_entry_evaluation_bundle import fixture,build
    from test_entry_gate_trace import settings
    context,obs,_=fixture();capture=context.setdefault('capture',{})
    capture.update(source_version_ref=context['capital_policy']['source_version_ref'],
        configuration_ref=context['capital_policy']['configuration_ref'])
    if direction=='v4_study_legacy_journal':
        capture.update(version='runner-first-scan-v4',frame_diagnostics=deepcopy(SETTINGS),
            selection_basis={'version':'selection-basis-v2','max_candidates':2,'max_source_terms':16},
            entry_gate_trace=settings())
        capture['entry_gate_trace'].update(source_version_ref=capture['source_version_ref'],
            configuration_ref=capture['configuration_ref'])
    else:
        capture['version']='runner-first-scan-v3'
        if direction=='legacy_study_v2_journal':obs['frame_diagnostics']=deepcopy(SETTINGS)
        obs.setdefault('incomplete_reasons',[])
        obs['journal'].update(format='entry-observation-journal-v2',frame_diagnostics=deepcopy(SETTINGS))
    raw=(json.dumps(context,ensure_ascii=False,sort_keys=True,separators=(',',':'))+'\n').encode()
    sha=hashlib.sha256(raw).hexdigest();obs['journal']['study_sha256']=sha
    with pytest.raises(ValueError, match="진단|v4"):build(context,obs,sha)
