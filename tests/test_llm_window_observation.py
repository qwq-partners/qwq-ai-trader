"""Offline LLM-boundary observation: no provider, account or socket calls."""
import asyncio
from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace

import pytest

from src.analytics import entry_observation as obs
from src.analytics.entry_observation_runtime import _WindowBuffer, validate_window_contract
from src.data.feeds.quote_subscription import QuoteSubscriptionCoordinator
from test_entry_observation_runtime import load
from test_signal_window_observation import window_plan, stock, event
from test_risk_sizing import home  # temporary state directory fixture


def llm_plan(c):
    window_plan(c)
    s = c['capture']
    s.update(version='runner-llm-window-v1', quote_admission='llm_check_started',
             record_allocations={'ws_quote': 100, 'market_state': 100, 'other': 500},
             max_bytes=1000 * (65536 + 2048) + 2048)
    s['channels'].update(market_state_evidence=True, market_state_max_records=100)


def setup(tmp_path, monkeypatch):
    plan, context = load(tmp_path, llm_plan)
    clock = [plan.start_at]
    monkeypatch.setattr(obs, '_now', lambda: clock[0].isoformat())
    b = _WindowBuffer(plan, lambda: clock[0])
    q = QuoteSubscriptionCoordinator(b, **plan.settings['channels'])
    b.llm_quote_submit = q.submit_llm_check
    return plan, context, b, q, clock


def enter(b, scan, sid, symbol):
    e = event(sid, symbol); e.source = 'live_screening'
    obs.capture_signal(b, scan, e)
    obs.capture_llm_check_started(SimpleNamespace(_entry_price_observer=b), e)


@pytest.mark.asyncio
async def test_five_bypass_signals_leave_room_for_llm_and_repeat_symbol_reuses_slot(tmp_path, monkeypatch):
    _, context, b, q, _ = setup(tmp_path, monkeypatch)
    symbols = [str(900001+i) for i in range(11)]
    scan = obs.capture_scan(b, [stock(s) for s in symbols], 'regular')
    for i, symbol in enumerate(symbols[:5]):
        obs.capture_signal(b, scan, event(f'bypass{i}', symbol))
    assert not q._tasks and not q._leases
    for i, symbol in enumerate(symbols[5:]):
        enter(b, scan, f'eligible{i}', symbol)
    scan2 = obs.capture_scan(b, [stock(symbols[5])], 'regular')
    enter(b, scan2, 'repeat', symbols[5])
    await asyncio.gather(*q._tasks)
    assert len(q._leases) == 5
    assert {v.symbol for v in q._leases.values()} == set(symbols[5:10])
    rows = [r for r in b.export()['records'] if r['kind'] == 'llm_check_admission']
    assert [r['reason'] for r in rows] == ['llm_check_admitted']*5 + ['llm_check_limit','llm_check_reused']
    assert rows[-1]['allocation_signal_id'] == 'eligible0'
    assert sum(r['kind'] == 'signal' for r in b.export()['records']) == 12
    validate_window_contract(context, b.export())


@pytest.mark.parametrize('field,value', [('ws_quote', None), ('ws_quote', True), ('ws_quote', 0),
    ('market_state', 1.5), ('other', float('inf')), ('other', -1)])
def test_explicit_integer_record_allocations_required(tmp_path, field, value):
    def change(c):
        llm_plan(c); c['capture']['record_allocations'][field] = value
    with pytest.raises(ValueError): load(tmp_path, change)


@pytest.mark.parametrize('field', ['buffer_capacity', 'queue_capacity', 'max_bytes'])
def test_capacity_sum_includes_quotes_states_other_and_scan_metadata(tmp_path, field):
    def change(c):
        llm_plan(c)
        s=c['capture']; s[field] = 700 if field != 'max_bytes' else 700*(65536+2048)+2048
    with pytest.raises(ValueError): load(tmp_path, change)


@pytest.mark.asyncio
async def test_closed_window_and_pending_registration_do_not_restore_leases(tmp_path, monkeypatch):
    plan, context, b, q, clock = setup(tmp_path, monkeypatch)
    scan = obs.capture_scan(b, [stock()], 'regular')
    await q._lock.acquire()
    enter(b, scan, 'queued', '900001')
    assert len(q._tasks)==1 and not q._leases
    clock[0] = b.admission_end
    enter(b, scan, 'late', '900001')
    assert b.export()['records'][-1]['reason']=='llm_check_admission_closed'
    validate_window_contract(context,b.export())
    clock[0]=plan.end_at
    q._lock.release(); await asyncio.gather(*q._tasks)
    assert not q._leases
    assert not b.capture_status()['complete']


@pytest.mark.asyncio
@pytest.mark.parametrize('defect', ['missing','signal','candidate','allocation','reason','lease','old_version'])
async def test_replay_rejects_forged_admission_and_old_contract(tmp_path, monkeypatch, defect):
    _, context,b,q,_=setup(tmp_path,monkeypatch)
    scan=obs.capture_scan(b,[stock()],'regular'); enter(b,scan,'s','900001')
    await asyncio.gather(*q._tasks)
    data=b.export(); row=next(r for r in data['records'] if r['kind']=='llm_check_admission')
    if defect=='missing': data['records'].remove(row)
    elif defect=='old_version': context['capture'].update(version='runner-signal-window-v1',quote_admission='signal_created')
    elif defect=='lease': next(r for r in data['records'] if r.get('status')=='desired')['signal_id']='foreign'
    else: row[{'signal':'signal_id','candidate':'candidate_id','allocation':'allocation_signal_id','reason':'reason'}[defect]]='foreign'
    with pytest.raises(ValueError): validate_window_contract(context,data)


@pytest.mark.parametrize('kind,limit', [('ws_quote',100), ('market_state',100), ('other',500)])
def test_record_allocation_overflow_marks_whole_ledger_incomplete(tmp_path,monkeypatch,kind,limit):
    _,_,b,_,_=setup(tmp_path,monkeypatch)
    for _ in range(limit): assert b.publish(dict(kind=kind,observed_at=obs._now()))
    assert not b.publish(dict(kind=kind,observed_at=obs._now()))
    assert not b.capture_status()['complete']
    assert f'{kind}_record_limit' in b.capture_status()['incomplete_reasons']


def test_disabled_hook_never_reads_event():
    class Bad:
        def __getattribute__(self,name): raise AssertionError('disabled observation touched event')
    obs.capture_llm_check_started(None, Bad())


@pytest.mark.asyncio
async def test_real_install_seals_new_records_and_strictly_replays(tmp_path, monkeypatch):
    import src.analytics.entry_observation_journal as journal
    from src.analytics.entry_observation_runtime import ObservationRuntime
    from src.analytics.kis_frame_diagnostics import validate_study_binding
    from test_entry_observation_runtime import NOW, owner, read
    plan,context=load(tmp_path,llm_plan); clock=[NOW]
    monkeypatch.setattr(obs,'_now',lambda:clock[0].isoformat())
    monkeypatch.setattr(journal,'_timestamp',lambda:clock[0].isoformat())
    bot=owner(); runtime=await ObservationRuntime.install(bot,plan,now=lambda:clock[0])
    clock[0]=plan.start_at
    scan=obs.capture_scan(runtime.buffer,[stock()],'regular')
    enter(runtime.buffer,scan,'sealed','900001')
    await asyncio.gather(*bot.ws_feed._quote_subscription_owner._tasks)
    clock[0]=plan.end_at; await runtime.close()
    data=read(plan)
    assert runtime.result['sealed'] and data['complete'], data['incomplete_reasons']
    validate_study_binding(data,context)


@pytest.mark.asyncio
async def test_shared_channel_precedence_reconnect_and_stale_generation(tmp_path,monkeypatch):
    _,context,b,q,_=setup(tmp_path,monkeypatch)
    q.interval=0
    sent=[]
    async def send(action,key): sent.append((action,key))
    await q.set_operational(['900001'], 'H0STCNT0', 'H0STASP0')
    old=await q.start(send)
    scan=obs.capture_scan(b,[stock()],'regular'); enter(b,scan,'s','900001')
    await asyncio.gather(*q._tasks)
    assert sent[:2]==[('subscribe',('H0STCNT0','900001')),('subscribe',('H0STASP0','900001'))]
    assert len(sent)==3
    assert q.accepts_market_state(('H0STMKO0','900001'),old)
    await q.stop(); new=await q.start(send)
    assert new!=old and not q.accepts_market_state(('H0STMKO0','900001'),old)
    assert q.accepts_market_state(('H0STMKO0','900001'),new)
    assert len(q._leases)==1
    assert not b.capture_status()['complete']
    validate_window_contract(context,b.export())


@pytest.mark.asyncio
async def test_channel_pressure_keeps_operational_demand_and_marks_incomplete(tmp_path,monkeypatch):
    _,context,b,q,_=setup(tmp_path,monkeypatch)
    q._operational=[('H0STCNT0',str(901000+i)) for i in range(29)]
    scan=obs.capture_scan(b,[stock()],'regular'); enter(b,scan,'s','900001')
    await asyncio.gather(*q._tasks)
    assert q._desired()==q._operational
    assert not b.capture_status()['complete']
    validate_window_contract(context,b.export())


@pytest.mark.asyncio
@pytest.mark.parametrize('score,regime,eligible',[(95,'neutral',False),(90,'bull',False),(84,'neutral',False),(90,'neutral',True)])
async def test_engine_real_boundary_matches_existing_llm_gate(tmp_path,monkeypatch,home,score,regime,eligible):
    from test_risk_sizing import _em,_rm,_sig
    from test_t11_entry_plan import _order_env
    _,context,b,q,_=setup(tmp_path,monkeypatch)
    rm=_rm(monkeypatch,mode='nominal',em=_em()); _order_env(monkeypatch,rm)
    rm.engine._market_regime=regime
    rm._entry_observation_owner=SimpleNamespace(_entry_price_observer=b)
    e=_sig(2.5); e.source='live_screening'; e.score=score
    scan=obs.capture_scan(b,[stock(e.symbol)],'regular'); obs.capture_signal(b,scan,e)
    async def check(**kwargs):
        assert b.export()['records'][-1]['kind']=='llm_check_admission'
        assert b.export()['records'][-1]['signal_id']==e.id
        assert not q._leases  # caller has not yielded to enrollment or awaited ACK
        rm._cross_validator.last_llm_budget_status={'status':'unavailable','reason':'synthetic_budget_denied'}
        return True
    rm._cross_validator.llm_second_check=check
    assert await rm.on_signal(e)
    await asyncio.gather(*q._tasks)
    assert len(q._leases)==int(eligible)
    if eligible:
        assert e.metadata['llm_budget_validation']['status']=='unavailable'
        assert 'paid_call' not in b.export()['records'][-1]
    validate_window_contract(context,b.export())


def test_replay_rejects_more_records_than_declared_allocation(tmp_path,monkeypatch):
    _,context,b,_,_=setup(tmp_path,monkeypatch)
    for _ in range(100): assert b.publish(dict(kind='ws_quote',observed_at=obs._now()))
    data=b.export(); data['records'].append(dict(kind='ws_quote',observed_at=obs._now(),sequence=101))
    with pytest.raises(ValueError): validate_window_contract(context,data)


@pytest.mark.asyncio
async def test_enrollment_failure_keeps_boundary_record_and_failclosed_status(tmp_path,monkeypatch):
    _,_,b,q,_=setup(tmp_path,monkeypatch)
    def broken(_record): raise RuntimeError('synthetic')
    b.llm_quote_submit=broken
    scan=obs.capture_scan(b,[stock()],'regular'); enter(b,scan,'s','900001')
    assert not b.capture_status()['complete']
    assert b.export()['records'][-1]['reason']=='llm_check_admitted'
    assert not q._tasks
