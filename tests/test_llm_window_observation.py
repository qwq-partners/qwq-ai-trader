"""Offline LLM-boundary observation: no provider, account or socket calls."""
import asyncio
from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace

import pytest

from src.analytics import entry_observation as obs
from src.analytics.entry_observation_runtime import _WindowBuffer, validate_window_contract
from src.analytics.entry_price_shadow import KST
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


def _llm_capture_times(c, *, start, admission, end):
    llm_plan(c)
    c['capture'].update(start_at=start, admission_end_at=admission, end_at=end)
    c['capture']['channels']['lease_seconds'] = 2000
    c['as_of'] = end


@pytest.mark.parametrize('start,admission,end,local_start', [
    ('2026-10-02T09:30:00+09:00', '2026-10-02T09:31:00+09:00', '2026-10-02T09:32:06+09:00', '09:30:00'),
    ('2026-10-02T00:30:00+00:00', '2026-10-02T00:31:00+00:00', '2026-10-02T00:32:06+00:00', '09:30:00'),
    ('2026-10-02T15:00:00+09:00', '2026-10-02T15:01:00+09:00', '2026-10-02T15:02:06+09:00', '15:00:00'),
])
def test_llm_install_accepts_same_kst_entry_window_in_kst_or_utc(
        tmp_path, start, admission, end, local_start):
    plan, _ = load(tmp_path, lambda c: _llm_capture_times(
        c, start=start, admission=admission, end=end))
    assert plan.start_at.astimezone(KST).time().isoformat() == local_start


@pytest.mark.parametrize('start,admission,end,error', [
    # Any overlap is insufficient: early scans can consume the finite cohort.
    ('2026-10-02T09:00:00+09:00', '2026-10-02T09:20:00+09:00', '2026-10-02T09:21:06+09:00', 'LLM 수집 입장 창'),
    # A 09:14 scan could not have passed the common live_screening entry-time gate.
    ('2026-10-02T09:14:00+09:00', '2026-10-02T09:16:00+09:00', '2026-10-02T09:17:06+09:00', 'LLM 수집 입장 창'),
    # 비코어 KR BUY는 공통 entry_time 통과 뒤에도 교차검증에서 09:30 전 차단된다.
    ('2026-10-02T09:15:00+09:00', '2026-10-02T09:25:00+09:00', '2026-10-02T09:41:00+09:00', 'LLM 수집 입장 창'),
    ('2026-10-02T09:29:59.999999+09:00', '2026-10-02T09:31:00+09:00', '2026-10-02T09:32:06+09:00', 'LLM 수집 입장 창'),
    ('2026-10-02T00:29:59.999999+00:00', '2026-10-02T00:31:00+00:00', '2026-10-02T00:32:06+00:00', 'LLM 수집 입장 창'),
    # The half-open 15:01 bound permits equality but excludes the next instant.
    ('2026-10-02T15:00:00+09:00', '2026-10-02T15:01:00.000001+09:00', '2026-10-02T15:03:00+09:00', 'LLM 수집 입장 창'),
    # Equal tail is insufficient because the markout deadline is half-open too.
    ('2026-10-02T09:30:00+09:00', '2026-10-02T09:31:00+09:00', '2026-10-02T09:32:05+09:00', 'LLM 수집 종료'),
    ('2026-10-02T15:00:00+09:00', '2026-10-02T15:01:00+09:00', '2026-10-02T15:02:05+09:00', 'LLM 수집 종료'),
])
def test_llm_install_rejects_windows_outside_entry_time_or_without_markout_tail(
        tmp_path, start, admission, end, error):
    with pytest.raises(ValueError, match=error):
        load(tmp_path, lambda c: _llm_capture_times(c, start=start, admission=admission, end=end))


def test_historical_llm_readout_keeps_original_0900_window_contract(tmp_path):
    from test_entry_observation_runtime import fixture_plan
    _, context = fixture_plan(tmp_path, lambda c: _llm_capture_times(
        c, start='2026-10-02T09:00:00+09:00', admission='2026-10-02T09:10:00+09:00',
        end='2026-10-02T09:26:00+09:00'))
    context['capture']['channels']['lease_seconds'] = 1560
    # Historical replay never invokes CapturePlan.validate, so its recorded study
    # remains readable even though a new installation of this window is rejected.
    validate_window_contract(context)


@pytest.mark.asyncio
async def test_historical_0915_sealed_journal_remains_readable_without_install(tmp_path, monkeypatch):
    import hashlib
    import json
    from pathlib import Path
    from src.analytics.capture_readiness import build_capture_readiness
    import src.analytics.entry_observation_journal as journal_module
    from src.analytics.entry_observation_journal import ObservationJournal, read_observation_journal
    from src.analytics.entry_observation_runtime import CapturePlan
    from test_entry_observation_runtime import fixture_plan

    manifest, context = fixture_plan(tmp_path, lambda c: _llm_capture_times(
        c, start='2026-10-02T09:15:00+09:00', admission='2026-10-02T09:25:00+09:00',
        end='2026-10-02T09:41:00+09:00'))
    study = Path(json.loads(manifest.read_text())['study_path'])
    raw = study.read_bytes()
    # 과거 합성 원장은 설치 검사를 호출하지 않고 기록된 계약으로 재생한다.
    plan = CapturePlan(study, hashlib.sha256(raw).hexdigest(), raw)
    clock = [plan.start_at]
    monkeypatch.setattr(obs, '_now', lambda: clock[0].isoformat())
    monkeypatch.setattr(journal_module, '_timestamp', lambda: clock[0].isoformat())
    buffer = _WindowBuffer(plan, lambda: clock[0])
    writer = await ObservationJournal.open(buffer, plan.settings['journal_path'],
        study_ref=plan.settings['study_ref'], study_sha256=plan.study_sha256,
        queue_capacity=1000, batch_size=10, max_bytes=plan.settings['max_bytes'],
        max_record_bytes=plan.settings['max_record_bytes'])
    try:
        scan = obs.capture_scan(buffer, [stock()], 'regular')
        obs.capture_signal(buffer, scan, event('historical-blocked'))
    finally:
        clock[0] = plan.end_at
        sealed = await writer.close()
    assert sealed['sealed']
    journal = Path(plan.settings['journal_path'])
    before = raw, journal.read_bytes()
    observations = read_observation_journal(journal, max_bytes=plan.settings['max_bytes'],
        expected_study_sha256=plan.study_sha256)
    validate_window_contract(context, observations)
    report = build_capture_readiness(study, journal)
    assert report['status'] == 'no_llm_checks'
    assert report['llm_started_count'] == 0
    assert report['economics']['profitability_pass'] is False
    assert (study.read_bytes(), journal.read_bytes()) == before


@pytest.mark.parametrize('local_time,expected_pass,expected_score', [
    ('09:15:00', False, 0),
    ('09:29:59.999999', False, 0),
    ('09:30:00', True, 62),
])
def test_real_cross_validator_blocks_noncore_kr_buy_until_0930(
        home, monkeypatch, local_time, expected_pass, expected_score):
    import datetime as datetime_module
    import src.core.cross_validator as cv_module

    fixed = datetime_module.datetime.fromisoformat('2026-10-02T' + local_time)

    class Frozen(datetime_module.datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed if tz is None else fixed.replace(tzinfo=tz)

    monkeypatch.setattr(datetime_module, 'datetime', Frozen)
    monkeypatch.setattr(cv_module, 'datetime', Frozen)
    validator = cv_module.CrossStrategyValidator(market='KR')
    # 패널 로더만 격리하며 시간 차단·점수 계산은 실제 검증기를 실행한다.
    monkeypatch.setattr(validator, '_load_panel_outlook', lambda: None)
    passed, score, reason = validator.validate(
        symbol='900001', side='buy', strategy='sepa_trend', score=70,
        metadata={'atr_pct': 3.0, 'per': 10.0, 'pbr': 1.0, 'foreign_net_buy': 1.0},
        market_regime='bull')
    assert passed is expected_pass
    assert score == expected_score
    assert ('장초반 30분 진입 차단' in reason) is (not expected_pass)


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
    record_kind = 'quote_subscription' if kind == 'other' else kind
    for _ in range(limit): assert b.publish(dict(kind=record_kind,observed_at=obs._now()))
    assert not b.publish(dict(kind=record_kind,observed_at=obs._now()))
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


@pytest.mark.parametrize('defect', ['invalid_symbol', 'signal_time', 'last_other_slot'])
def test_failed_boundary_keeps_replayable_incomplete_prefix(tmp_path, monkeypatch, defect):
    _, context, b, _, clock = setup(tmp_path, monkeypatch)
    b.llm_quote_submit = lambda record: None
    symbol = 'BADSYM' if defect == 'invalid_symbol' else '900001'
    scan = obs.capture_scan(b, [stock(symbol)], 'regular')
    e = event('boundary', symbol); e.source = 'live_screening'
    obs.capture_signal(b, scan, e)
    if defect == 'signal_time':
        # The callback timestamp regressed without moving the capture clock.
        monkeypatch.setattr(obs, '_now', lambda: (clock[0] - timedelta(seconds=1)).isoformat())
    elif defect == 'last_other_slot':
        while b._record_counts['other'] < b._record_limits['other'] - 1:
            assert b.publish(dict(kind='quote_subscription', observed_at=clock[0].isoformat(), status='connected'))
    obs.capture_llm_check_started(SimpleNamespace(_entry_price_observer=b), e)
    assert not any(r['kind'] == 'llm_check_started' for r in b.export()['records'])
    assert not b.capture_status()['complete']
    validate_window_contract(context, b.export())


def test_started_and_admission_share_one_capture_deadline_check(tmp_path, monkeypatch):
    plan, context, b, _, clock = setup(tmp_path, monkeypatch)
    b.llm_quote_submit = lambda record: None
    scan = obs.capture_scan(b, [stock()], 'regular')
    e = event('deadline'); e.source = 'live_screening'
    obs.capture_signal(b, scan, e)
    # A second clock read would close the window between the two records.
    before = plan.end_at - timedelta(microseconds=1)
    calls = []
    def boundary_clock():
        calls.append(None)
        return before if len(calls) == 1 else plan.end_at
    b._now = boundary_clock
    monkeypatch.setattr(obs, '_now', lambda: before.isoformat())
    obs.capture_llm_check_started(SimpleNamespace(_entry_price_observer=b), e)
    records = b.export()['records']
    assert [r['kind'] for r in records[-2:]] == ['llm_check_started', 'llm_check_admission']
    validate_window_contract(context, b.export())


@pytest.mark.parametrize('value', [4, 6, 5.0, True])
def test_llm_contract_already_binds_exactly_five_distinct_symbols(tmp_path, value):
    def change(context):
        llm_plan(context)
        context['capture']['channels']['max_candidates'] = value
    with pytest.raises(ValueError):
        load(tmp_path, change)


def test_none_strategy_never_enters_captured_signal_cohort(tmp_path, monkeypatch):
    _, context, b, q, _ = setup(tmp_path, monkeypatch)
    scan = obs.capture_scan(b, [stock()], 'regular')
    e = event('no-strategy'); e.strategy = None; e.source = 'live_screening'
    obs.capture_signal(b, scan, e)
    assert not b.accepts_order_signal(e.id)
    obs.capture_llm_check_started(SimpleNamespace(_entry_price_observer=b), e)
    assert not q._tasks
    assert not b.capture_status()['complete']
    validate_window_contract(context, b.export())


def test_llm_keeps_order_ready_population_and_legacy_capacity_reason(tmp_path, monkeypatch):
    _, context, b, q, _ = setup(tmp_path, monkeypatch)
    b.llm_quote_submit = lambda record: None
    scan = obs.capture_scan(b, [stock()], 'regular'); enter(b, scan, 's', '900001')
    assert b.publish(dict(kind='order_ready', signal_id='s', order_id='o', symbol='900001',
                          observed_at=obs._now(), requested_quantity=1, strategy='gap_and_go'))
    q._gap('order_ready_channel_capacity')
    assert any(r['kind'] == 'order_ready' for r in b.export()['records'])
    assert 'order_ready_channel_capacity' in b.capture_status()['incomplete_reasons']
    validate_window_contract(context, b.export())


@pytest.mark.asyncio
@pytest.mark.parametrize('defect', ['queue_pair_capacity', 'record_shape', 'admission_shape'])
async def test_pair_rejection_preserves_sealed_replayable_prefix(tmp_path, monkeypatch, defect):
    import src.analytics.entry_observation_journal as journal
    from src.analytics.entry_observation_runtime import ObservationRuntime
    from src.analytics.kis_frame_diagnostics import validate_study_binding
    from test_entry_observation_runtime import NOW, owner, read
    plan, context = load(tmp_path, llm_plan); clock = [NOW]
    monkeypatch.setattr(obs, '_now', lambda: clock[0].isoformat())
    monkeypatch.setattr(journal, '_timestamp', lambda: clock[0].isoformat())
    bot = owner(); runtime = await ObservationRuntime.install(bot, plan, now=lambda: clock[0])
    b = runtime.buffer; clock[0] = plan.start_at
    scan = obs.capture_scan(b, [stock()], 'regular')
    e = event('pair'); e.source = 'live_screening'
    obs.capture_signal(b, scan, e)
    # Let the real writer persist prior records, then test bounded queue admission.
    expected_prefix = len(b.export()['records'])
    await journal._wait_until(lambda: runtime.journal._persisted == expected_prefix, 1)
    if defect == 'queue_pair_capacity':
        runtime.journal._queue.maxsize = 1
    elif defect == 'record_shape':
        e.source = 'x' * (plan.settings['max_record_bytes'] + 1)
    elif defect == 'admission_shape':
        # The start fits, but the longer admission must fail before either is queued.
        runtime.journal._max_record_bytes = 250
        journal._record_shape(dict(kind='llm_check_started', signal_id=e.id, symbol=e.symbol,
            strategy=e.strategy.value, source=e.source, observed_at=obs._now(),
            sequence=expected_prefix + 1), 250)
    obs.capture_llm_check_started(bot, e)
    assert not any(r['kind'] == 'llm_check_started' for r in b.export()['records'])
    assert not b.capture_status()['complete']
    clock[0] = plan.end_at
    await runtime.close()
    data = read(plan)
    assert runtime.result['sealed'] and not data['complete']
    validate_study_binding(data, context)


@pytest.mark.asyncio
async def test_closed_journal_excludes_whole_boundary_without_changing_seal(tmp_path, monkeypatch):
    import src.analytics.entry_observation_journal as journal
    from src.analytics.entry_observation_runtime import ObservationRuntime
    from src.analytics.kis_frame_diagnostics import validate_study_binding
    from test_entry_observation_runtime import NOW, owner, read
    plan, context = load(tmp_path, llm_plan); clock = [NOW]
    monkeypatch.setattr(obs, '_now', lambda: clock[0].isoformat())
    monkeypatch.setattr(journal, '_timestamp', lambda: clock[0].isoformat())
    bot = owner(); runtime = await ObservationRuntime.install(bot, plan, now=lambda: clock[0])
    b = runtime.buffer; clock[0] = plan.start_at
    scan = obs.capture_scan(b, [stock()], 'regular')
    e = event('after-seal'); e.source = 'live_screening'
    obs.capture_signal(b, scan, e)
    result = await runtime.journal.close()
    before = b.export()
    obs.capture_llm_check_started(bot, e)
    assert result['sealed'] and b.export() == before
    assert not bot.ws_feed._quote_subscription_owner._tasks
    validate_study_binding(read(plan), context)


def test_enrollment_callback_cannot_mutate_recorded_admission_pair(tmp_path, monkeypatch):
    _, context, b, _, _ = setup(tmp_path, monkeypatch)
    def change_callback_copy(admission):
        admission['signal_id'] = 'mutated-after-recording'
    b.llm_quote_submit = change_callback_copy
    scan = obs.capture_scan(b, [stock()], 'regular'); enter(b, scan, 'original', '900001')
    assert b.export()['records'][-1]['signal_id'] == 'original'
    validate_window_contract(context, b.export())
