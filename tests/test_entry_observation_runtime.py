"""명시 Runner 관측 수명. 합성 feed/engine 및 임시 파일만 사용."""
import asyncio
from datetime import datetime, timedelta, timezone
import hashlib
import importlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.analytics.entry_observation import capture_scan
from src.analytics.entry_observation_journal import read_observation_journal
from test_received_entry_shadow import received_payload


NOW = datetime(2026, 10, 1, 0, 59, tzinfo=timezone.utc)


def module():
    try:
        return importlib.import_module('src.analytics.entry_observation_runtime')
    except ModuleNotFoundError:
        pytest.fail('명시 관측 실행 연결 미구현')


def fixture_plan(tmp_path, change=None):
    context = received_payload()
    context.pop('opportunities')
    context['as_of'] = (NOW + timedelta(minutes=10)).isoformat()
    context['capture'] = {
        'version': 'runner-first-scan-v1', 'study_ref': 'synthetic-study',
        'start_at': (NOW + timedelta(minutes=1)).isoformat(),
        'admission_end_at': (NOW + timedelta(minutes=2)).isoformat(),
        'end_at': context['as_of'], 'scan_admission_ref': 'synthetic-first',
        'source_version_ref': 'synthetic-source', 'configuration_ref': 'synthetic-config',
        'fee_evidence_ref': 'synthetic-fees', 'capital_evidence_ref': 'synthetic-capital',
        'capacity_evidence_ref': 'synthetic-capacity',
        'journal_path': str(tmp_path / 'capture.jsonl'), 'buffer_capacity': 1000,
        'queue_capacity': 1000, 'batch_size': 10, 'max_bytes': 1000000,
        'max_record_bytes': 10000, 'open_timeout_seconds': 1, 'close_timeout_seconds': 1,
        'channels': {'registration_cap': 41, 'external_reserved': 0,
            'operational_headroom': 10, 'max_candidates': 5, 'lease_seconds': 600,
            'evidence_ref': 'synthetic-session'},
    }
    if change: change(context)
    study = tmp_path / 'study.json'
    study.write_text(json.dumps(context))
    manifest = tmp_path / 'manifest.json'
    manifest.write_text(json.dumps({'study_path': str(study),
        'study_sha256': hashlib.sha256(study.read_bytes()).hexdigest()}))
    return manifest, context


class Feed:
    def __init__(self): self._quote_subscription_owner = None
    def enable_quote_observation(self, observer, **contract):
        from src.data.feeds.quote_subscription import QuoteSubscriptionCoordinator
        self._quote_subscription_owner = QuoteSubscriptionCoordinator(observer, **contract)


def owner():
    return SimpleNamespace(market='kr', dry_run=False, ws_feed=Feed())


def load(tmp_path, change=None):
    path, context = fixture_plan(tmp_path, change)
    return module().CapturePlan.load(path, now=NOW), context


def read(plan):
    return read_observation_journal(plan.settings['journal_path'], max_bytes=1000000,
                                   expected_study_sha256=plan.study_sha256)


@pytest.mark.parametrize('defect', ['unknown', 'relative', 'as_of', 'late', 'reverse',
    'epoch', 'channels', 'lease', 'bool', 'batch', 'timeout', 'hash', 'exists', 'study_changed'])
def test_invalid_plan_rejected_before_installation(tmp_path, defect):
    def change(c):
        s = c['capture']
        if defect == 'unknown': s['auto_restart'] = True
        elif defect == 'relative': s['journal_path'] = 'relative.jsonl'
        elif defect == 'as_of': c['as_of'] = NOW.isoformat()
        elif defect == 'late': s['start_at'] = NOW.isoformat()
        elif defect == 'reverse': s['admission_end_at'] = s['end_at']
        elif defect == 'epoch': c['evaluation_epoch'] = ''
        elif defect == 'channels': s['channels']['registration_cap'] = 42
        elif defect == 'lease': s['channels']['lease_seconds'] = 1
        elif defect == 'bool': s['buffer_capacity'] = True
        elif defect == 'batch': s['batch_size'] = 1001
        elif defect == 'timeout': s['close_timeout_seconds'] = float('nan')
    path, context = fixture_plan(tmp_path, change)
    if defect == 'hash':
        x = json.loads(path.read_text()); x['study_sha256'] = 'a'*64; path.write_text(json.dumps(x))
    if defect == 'exists': Path(context['capture']['journal_path']).write_text('keep')
    if defect == 'study_changed': (tmp_path/'study.json').write_text('{}')
    with pytest.raises((ValueError, FileExistsError)):
        module().CapturePlan.load(path, now=NOW)
    assert list(tmp_path.glob('*.jsonl')) == ([tmp_path/'capture.jsonl'] if defect == 'exists' else [])


@pytest.mark.asyncio
async def test_install_same_buffer_fixed_first_window_and_clean_end(tmp_path):
    plan, _ = load(tmp_path); bot = owner(); clock = [NOW]
    runtime = await module().ObservationRuntime.install(bot, plan, now=lambda: clock[0])
    assert bot._entry_price_observer is runtime.buffer is bot.ws_feed._quote_subscription_owner.observer
    assert capture_scan(runtime.buffer, [], 'regular') is None
    clock[0] = plan.start_at
    assert capture_scan(runtime.buffer, [], 'regular')
    assert capture_scan(runtime.buffer, [], 'regular') is None
    clock[0] = plan.end_at
    assert not runtime.buffer.publish({'kind':'ws_quote'})
    await runtime.close()
    assert runtime.result['sealed'] and read(plan)['complete']
    runtime.buffer.mark_incomplete('socket_closed')
    assert read(plan)['complete']
    assert bot._entry_observation_close_result == runtime.result
    assert not bot.ws_feed._quote_subscription_owner._leases


@pytest.mark.asyncio
@pytest.mark.parametrize('reason', ['runner_shutdown', 'runner_cancelled', 'startup_failed'])
async def test_early_close_is_incomplete_and_idempotent(tmp_path, reason):
    plan, _ = load(tmp_path); bot = owner(); clock = [plan.start_at]
    runtime = await module().ObservationRuntime.install(bot, plan, now=lambda: NOW)
    runtime.buffer._now = lambda: clock[0]
    capture_scan(runtime.buffer, [], 'regular')
    result = await runtime.close(reason)
    assert result == await runtime.close()
    data = read(plan)
    assert not data['complete'] and reason in data['incomplete_reasons']


@pytest.mark.asyncio
async def test_enable_failure_rolls_back_without_visible_observer_or_owner(tmp_path):
    plan, _ = load(tmp_path); bot = owner()
    def bad(*a, **k): raise RuntimeError('synthetic install error')
    bot.ws_feed.enable_quote_observation = bad
    with pytest.raises(RuntimeError):
        await module().ObservationRuntime.install(bot, plan, now=lambda: NOW)
    assert getattr(bot, '_entry_price_observer', None) is None
    assert bot.ws_feed._quote_subscription_owner is None
    data = read(plan)
    assert not data['complete'] and 'installation_failed' in data['incomplete_reasons']


@pytest.mark.asyncio
@pytest.mark.parametrize('defect',['dry_run','us','no_feed','already_installed','late'])
async def test_unsupported_runner_rejected_before_journal(tmp_path, defect):
    plan,_ = load(tmp_path); bot = owner()
    if defect == 'dry_run': bot.dry_run = True
    if defect == 'us': bot.market = 'us'
    if defect == 'no_feed': bot.ws_feed = None
    if defect == 'already_installed': bot._entry_price_observer = object()
    now = plan.start_at if defect == 'late' else NOW
    with pytest.raises((ValueError, RuntimeError)):
        await module().ObservationRuntime.install(bot, plan, now=lambda: now)
    assert not Path(plan.settings['journal_path']).exists()


@pytest.mark.asyncio
async def test_deadline_task_closes_capture_without_stopping_feed(tmp_path):
    plan,_ = load(tmp_path); bot = owner(); clock=[NOW]
    runtime = await module().ObservationRuntime.install(bot, plan, now=lambda: clock[0])
    clock[0] = plan.start_at
    capture_scan(runtime.buffer, [], 'regular')
    clock[0] = plan.end_at
    runtime.start()
    await asyncio.wait_for(runtime.task, 2)
    assert read(plan)['complete'] and runtime.result['sealed']
    assert bot.ws_feed._quote_subscription_owner is not None


def test_duplicate_or_large_manifest_is_rejected(tmp_path):
    path=tmp_path/'bad.json'
    for text in ('{"study_path":"a","study_path":"b"}', ' '*300000):
        path.write_text(text)
        with pytest.raises(ValueError): module().CapturePlan.load(path, now=NOW)


@pytest.mark.asyncio
async def test_ended_lease_cannot_be_restored_by_waiting_enrollment(tmp_path):
    plan,_=load(tmp_path); bot=owner()
    runtime=await module().ObservationRuntime.install(bot,plan,now=lambda:NOW)
    q=bot.ws_feed._quote_subscription_owner
    await q._lock.acquire()
    q.submit('scan',['900001'])
    waiting=list(q._tasks)
    q.end_observation()
    q._lock.release()
    await asyncio.gather(*waiting)
    q.submit('later',['900002'])
    assert q.snapshot()['leases']==0
    await runtime.close('test_end')


@pytest.mark.asyncio
async def test_end_is_latched_when_clock_moves_back(tmp_path):
    plan,_=load(tmp_path); bot=owner(); clock=[NOW]
    runtime=await module().ObservationRuntime.install(bot,plan,now=lambda:clock[0])
    clock[0]=plan.end_at
    assert not runtime.buffer.publish({'kind':'ws_quote'})
    clock[0]=plan.start_at
    assert capture_scan(runtime.buffer,[],'regular') is None
    await runtime.close()
    assert not read(plan)['complete']


@pytest.mark.asyncio
async def test_interruption_before_deadline_stays_incomplete_after_deadline(tmp_path):
    plan,_=load(tmp_path); bot=owner(); clock=[NOW]
    runtime=await module().ObservationRuntime.install(bot,plan,now=lambda:clock[0])
    clock[0]=plan.start_at
    capture_scan(runtime.buffer,[],'regular')
    runtime.interrupt('runner_stop_requested')
    clock[0]=plan.end_at+timedelta(seconds=1)
    await runtime.close()
    assert 'runner_stop_requested' in read(plan)['incomplete_reasons']


def runner():
    return importlib.import_module('scripts.run_trader')


def fake_bot():
    m=runner()
    b=m.UnifiedTradingBot.__new__(m.UnifiedTradingBot)
    b.market='kr'; b.dry_run=False; b.dashboard=None; b.ws_feed=None
    b._entry_observation_plan=None; b._entry_observation_runtime=None
    b.broker=None; b._us_engine=None; b._token_manager=None
    b.running=False
    b.engine=SimpleNamespace(get_context=lambda market:None,stop=lambda:None)
    return b


@pytest.mark.asyncio
async def test_runner_default_off_does_not_install_or_create_capture(tmp_path,monkeypatch):
    b=fake_bot(); events=[]
    async def initialize(): events.append('initialize');return True
    async def engine_run(): events.append('engine');b.stop()
    async def forbidden(*a,**k): pytest.fail('default off attempted installation')
    b.initialize=initialize;b.engine.run=engine_run
    monkeypatch.setattr(module().ObservationRuntime,'install',forbidden)
    await b.run()
    assert events==['initialize','engine'] and not list(tmp_path.iterdir())


@pytest.mark.asyncio
async def test_runner_close_precedes_feed_cancellation_and_disconnect(monkeypatch):
    b=fake_bot(); events=[]; started=asyncio.Event()
    class Runtime:
        task=None
        def start(self): events.append('start_capture')
        def interrupt(self,reason): events.append('interrupt')
        def watch_tasks(self,tasks):pass
        async def close(self,reason=None): events.append('close_capture')
    runtime=Runtime()
    async def init():return True
    async def install(bot):
        events.append('install');bot._entry_observation_runtime=runtime;runtime.start()
    async def close(reason): await runtime.close(reason)
    async def feed_run():
        started.set()
        try: await asyncio.Future()
        finally: events.append('cancel_feed')
    async def disconnect(): events.append('disconnect')
    async def engine_run(): await started.wait(); b.stop(); await asyncio.Future()
    b.initialize=init;b.engine.run=engine_run
    b.ws_feed=SimpleNamespace(run=feed_run,disconnect=disconnect)
    # Installation helper is replaced; run/stop/shutdown ordering remains real.
    b._install_entry_observation=lambda:install(b)
    b._close_entry_observation=close
    await b.run()
    assert events.index('install')<events.index('start_capture')
    assert events.index('interrupt')<events.index('close_capture')<events.index('cancel_feed')<events.index('disconnect')


@pytest.mark.asyncio
async def test_invalid_explicit_plan_fails_before_initialize(tmp_path):
    plan,_=load(tmp_path);b=fake_bot();b._entry_observation_plan=plan
    b.dry_run=True
    async def forbidden():pytest.fail('invalid plan initialized API components')
    b.initialize=forbidden
    with pytest.raises(ValueError): await b.run()


def test_cli_none_by_default_and_invalid_manifest_precedes_config(monkeypatch,tmp_path):
    m=runner()
    monkeypatch.setattr('sys.argv',['run_trader.py'])
    assert m.parse_args().entry_observation_plan is None
    monkeypatch.setattr('sys.argv',['run_trader.py','--entry-observation-plan',str(tmp_path/'absent.json')])
    # Failed file read must occur before logger/config/cache/singleton paths.
    with pytest.raises(FileNotFoundError): asyncio.run(m.main())


@pytest.mark.asyncio
async def test_partial_feed_install_restores_stopped_state(tmp_path):
    plan,_=load(tmp_path); bot=owner()
    bot.ws_feed._subscribed_symbols={'synthetic-original'}
    bot.ws_feed._pending_subscriptions={'synthetic-pending'}
    def bad(observer,**contract):
        bot.ws_feed._quote_subscription_owner=object()
        bot.ws_feed._subscribed_symbols.clear()
        bot.ws_feed._pending_subscriptions.clear()
        raise RuntimeError('synthetic partial installation')
    bot.ws_feed.enable_quote_observation=bad
    with pytest.raises(RuntimeError): await module().ObservationRuntime.install(bot,plan,now=lambda:NOW)
    assert bot.ws_feed._quote_subscription_owner is None
    assert bot.ws_feed._subscribed_symbols=={'synthetic-original'}
    assert bot.ws_feed._pending_subscriptions=={'synthetic-pending'}
    assert not read(plan)['complete']


@pytest.mark.asyncio
async def test_inflight_reconcile_does_not_start_more_candidate_sends_after_end():
    from test_quote_subscription import setup_owner
    q,_,_,sent,_=setup_owner()
    entered=asyncio.Event(); release=asyncio.Event()
    async def send(action,key):
        sent.append((action,key)); entered.set(); await release.wait()
    await q.start(send)
    task=asyncio.create_task(q.enroll('scan',['900001','900002']))
    await entered.wait()
    q.end_observation();release.set();await task
    assert sent==[('subscribe',('H0STASP0','900001'))]
    assert q.snapshot()['occupied']==1 # 송신 중이었던 슬롯을 해제 완료로 보지 않는다.


@pytest.mark.asyncio
async def test_stop_during_install_never_starts_trading_tasks():
    b=fake_bot(); seen=[]
    async def initialize():return True
    async def install():b.stop();seen.append('install-stopped')
    async def forbidden():seen.append('unexpected-trading-task')
    b.initialize=initialize;b._install_entry_observation=install;b.engine.run=forbidden
    await b.run()
    assert seen==['install-stopped']


@pytest.mark.parametrize('value',[False,None,{},'',0])
def test_empty_wrong_type_opportunities_rejected(tmp_path,value):
    with pytest.raises(ValueError):load(tmp_path,lambda c:c.update(opportunities=value))


@pytest.mark.asyncio
async def test_finished_runner_task_makes_active_capture_incomplete(tmp_path):
    plan,_=load(tmp_path);bot=owner();clock=[NOW]
    rt=await module().ObservationRuntime.install(bot,plan,now=lambda:clock[0])
    clock[0]=plan.start_at;capture_scan(rt.buffer,[],'regular')
    async def short():return None
    task=asyncio.create_task(short(),name='kr_screener')
    rt.watch_tasks([task]);rt.start()
    await task
    await asyncio.wait_for(rt.task,2)
    assert 'runner_task_ended' in read(plan)['incomplete_reasons']


@pytest.mark.asyncio
async def test_cancelled_close_caller_does_not_cancel_shared_seal(tmp_path,monkeypatch):
    plan,_=load(tmp_path);bot=owner()
    rt=await module().ObservationRuntime.install(bot,plan,now=lambda:NOW)
    original=rt.journal.close;entered=asyncio.Event();release=asyncio.Event()
    async def delayed(**kw):entered.set();await release.wait();return await original(**kw)
    monkeypatch.setattr(rt.journal,'close',delayed)
    task=asyncio.create_task(rt.close('interrupted'))
    await entered.wait();task.cancel()
    with pytest.raises(asyncio.CancelledError):await task
    release.set()
    assert (await rt.close())['sealed'] and not read(plan)['complete']


@pytest.mark.asyncio
@pytest.mark.parametrize('failure',['study_changed','start_passed','cancelled'])
async def test_journal_preparation_race_leaves_incomplete_uninstalled_prefix(tmp_path,monkeypatch,failure):
    plan,_=load(tmp_path);bot=owner();clock=[NOW]
    m=module();original=m.ObservationJournal.open
    async def raced(*args,**kwargs):
        journal=await original(*args,**kwargs)
        if failure=='study_changed':plan.study_path.write_text('{}')
        elif failure=='start_passed':clock[0]=plan.start_at
        else:asyncio.current_task().cancel()
        return journal
    monkeypatch.setattr(m.ObservationJournal,'open',raced)
    if failure=='cancelled':
        task=asyncio.create_task(m.ObservationRuntime.install(bot,plan,now=lambda:clock[0]))
        with pytest.raises(asyncio.CancelledError):await task
    else:
        with pytest.raises(ValueError):await m.ObservationRuntime.install(bot,plan,now=lambda:clock[0])
    assert getattr(bot,'_entry_price_observer',None) is None
    assert bot.ws_feed._quote_subscription_owner is None
    assert not read(plan)['complete']


@pytest.mark.asyncio
async def test_cancelled_observation_timer_does_not_skip_runner_cleanup(tmp_path):
    plan,_=load(tmp_path);b=fake_bot();b.ws_feed=Feed();events=[]
    rt=await module().ObservationRuntime.install(b,plan,now=lambda:NOW)
    rt.start();await asyncio.sleep(0);rt.task.cancel()
    with pytest.raises(asyncio.CancelledError):await rt.task
    assert rt.result['sealed']
    async def disconnect():events.append('disconnect')
    b.broker=SimpleNamespace(disconnect=disconnect);b.ws_feed.disconnect=disconnect
    await b.shutdown()
    assert events==['disconnect','disconnect']


@pytest.mark.asyncio
@pytest.mark.parametrize('name',['kr_dart_alert','kr_manual_buy'])
async def test_normal_auxiliary_task_completion_does_not_end_capture(tmp_path,name):
    plan,_=load(tmp_path);b=owner();clock=[NOW]
    rt=await module().ObservationRuntime.install(b,plan,now=lambda:clock[0])
    clock[0]=plan.start_at;capture_scan(rt.buffer,[],'regular')
    async def short():return None
    task=asyncio.create_task(short(),name=name)
    rt.watch_tasks([task]);await task;await asyncio.sleep(0)
    active=not rt.buffer.ended
    await rt.close('test_cleanup')
    assert active


@pytest.mark.asyncio
async def test_observation_close_exception_does_not_skip_broker_disconnect():
    b=fake_bot();events=[]
    async def close(reason):raise RuntimeError('synthetic observation close failure')
    async def disconnect():events.append('disconnect')
    b._entry_observation_runtime=SimpleNamespace(close=close,task=None)
    b.broker=SimpleNamespace(disconnect=disconnect)
    await b.shutdown()
    assert events==['disconnect']


@pytest.mark.asyncio
@pytest.mark.parametrize('stop_branch',[True,False])
async def test_external_cancel_during_observation_close_still_cleans_runner(stop_branch):
    b=fake_bot();events=[];entered=asyncio.Event();counter=[0]
    async def init():return True
    async def engine():
        if stop_branch:
            b.stop();await asyncio.Future()
    async def close(reason):
        counter[0]+=1
        if counter[0]==1:entered.set();await asyncio.Future()
    async def disconnect():events.append('disconnect')
    b.initialize=init;b.engine.run=engine;b.broker=SimpleNamespace(disconnect=disconnect)
    b._entry_observation_runtime=SimpleNamespace(close=close,task=None,interrupt=lambda reason:None,watch_tasks=lambda tasks:None)
    run=asyncio.create_task(b.run())
    await entered.wait();run.cancel()
    with pytest.raises(asyncio.CancelledError):await run
    assert events==['disconnect']


def test_once_startup_cli_exclusive_and_default(monkeypatch):
    m = runner()
    monkeypatch.setattr('sys.argv', ['run_trader.py'])
    args = m.parse_args()
    assert args.entry_observation_once is None
    assert m.load_entry_observation_plan(args) is None
    monkeypatch.setattr('sys.argv', ['run_trader.py', '--entry-observation-plan', '/one', '--entry-observation-once', '/two'])
    with pytest.raises(SystemExit): m.parse_args()


@pytest.mark.parametrize('selected', [None, '/synthetic/manifest.json'])
def test_once_selection_before_plan_load(monkeypatch, selected):
    m = runner()
    from src.analytics import entry_observation_startup as startup
    events = []
    def claim(path):
        events.append(('claim', path))
        return selected
    def load_manifest(path):
        events.append(('load', path))
        return 'synthetic-plan'
    monkeypatch.setattr(startup, 'claim_once_manifest', claim)
    monkeypatch.setattr(module().CapturePlan, 'load', load_manifest)
    args = SimpleNamespace(entry_observation_plan=None, entry_observation_once='/synthetic/once.json', market='kr', dry_run=False)
    assert m.load_entry_observation_plan(args) == ('synthetic-plan' if selected else None)
    assert events == [('claim', args.entry_observation_once)] + ([('load', selected)] if selected else [])


@pytest.mark.parametrize('market,dry_run', [('us', False), ('kr', True)])
def test_once_invalid_mode_does_not_consume_attempt(monkeypatch, market, dry_run):
    m = runner()
    from src.analytics import entry_observation_startup as startup
    monkeypatch.setattr(startup, 'claim_once_manifest', lambda _: pytest.fail('invalid mode consumed receipt'))
    args = SimpleNamespace(entry_observation_plan=None, entry_observation_once='/synthetic/once.json', market=market, dry_run=dry_run)
    with pytest.raises(ValueError): m.load_entry_observation_plan(args)


def test_once_bad_manifest_consumed_before_any_bot_initialization(tmp_path, monkeypatch):
    m = runner()
    from src.analytics import entry_observation_startup as startup
    moment = datetime.now(timezone.utc)
    tmp_path.chmod(0o700)
    receipt = tmp_path / 'once.receipt'
    request = tmp_path / 'once.json'
    request.write_text(json.dumps(dict(version='entry-observation-once-v1',
        manifest_path=str(tmp_path / 'absent.json'), receipt_path=str(receipt),
        not_before=(moment-timedelta(seconds=10)).isoformat(),
        latest_start_at=(moment+timedelta(minutes=2)).isoformat())))
    request.chmod(0o600)
    monkeypatch.setattr('sys.argv', ['run_trader.py', '--entry-observation-once', str(request)])
    with pytest.raises(FileNotFoundError): asyncio.run(m.main())
    assert receipt.is_file()
    assert startup.claim_once_manifest(request, now=moment) is None
