"""Fixed future preparation with real local runtime/journal; no broker/socket API."""
import asyncio
from copy import deepcopy
import json
from hashlib import sha256
from pathlib import Path
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from src.analytics.entry_observation_runtime import ObservationRuntime
from src.dashboard.kr_api import KRAPIHandler
from test_entry_capture_activate import module, setup
from test_entry_observation_readiness import request
from src.analytics.entry_observation_runtime import CapturePlan
from src.analytics.entry_observation_startup import claim_once_manifest
from test_entry_capture_prepare_20261016 import m as installer_fixture, payloads
from test_market_state_pilot_runtime import bot_with

PROFILE = '20261016-llm-window-v1'
EPOCH = '2026-10-16-engine-llm-window-v1'
NOW = datetime(2026, 10, 16, 0, 10, tzinfo=timezone.utc)


async def installed(tmp_path, *, head='a'*40):
    installer = installer_fixture.__wrapped__()
    installer.ENGINE_STATE = tmp_path / 'journal-state'
    installer.ENGINE_STATE.mkdir(mode=0o700)
    installer.NEW = {**installer.NEW, **{key: tmp_path / name for key, name in
        (('study', 'study.json'), ('engine_manifest', 'manifest.json'), ('once', 'once.json'))}}
    manifest, files = payloads(installer, head)
    installer.validate_payload(manifest, files)
    # 자원 시험의 새 설치만 미래 안전 창으로 이동한다. 고정 installer/profile은 보존한다.
    study = installer.document(files['study'])
    study['as_of'] = '2026-10-16T00:56:00+00:00'
    study['capture'].update(start_at='2026-10-16T00:30:00+00:00',
        admission_end_at='2026-10-16T00:40:00+00:00', end_at=study['as_of'])
    files['study'] = installer.raw(study)
    files['engine_manifest'] = installer.raw(dict(study_path=str(installer.NEW['study']),
        study_sha256=sha256(files['study']).hexdigest()))
    for key in ('study', 'engine_manifest', 'once'):
        installer.NEW[key].write_bytes(files[key])
        installer.NEW[key].chmod(0o600)
    selected = claim_once_manifest(installer.NEW['once'], now=NOW)
    assert selected == str(installer.NEW['engine_manifest'])
    assert claim_once_manifest(installer.NEW['once'], now=NOW) is None
    plan = CapturePlan.load(selected, now=NOW)
    bot = bot_with([f'{100000+i}' for i in range(8)])
    bot.engine = SimpleNamespace(risk_manager=SimpleNamespace(_entry_observation_owner=bot))
    clock = [NOW]
    runtime = await ObservationRuntime.install(bot, plan, now=lambda: clock[0])
    return runtime, bot, clock


async def projection(bot):
    return await KRAPIHandler(SimpleNamespace(bot=bot)).get_entry_observation_readiness(request())


def legacy_profile_resource_check(value):
    # 자원 술어만 과거 profile에 투영한다. 실제 활성화 시험은 원본 projection을 전달한다.
    projection = deepcopy(value)
    if type(projection.get('llm')) is not dict:
        return False
    projection['llm'].update(start_at='2026-10-16T00:15:00+00:00',
        admission_end_at='2026-10-16T00:25:00+00:00', end_at='2026-10-16T00:41:00+00:00')
    return module().capture_prepared(SimpleNamespace(engine_only=True, evaluation_epoch=EPOCH), projection)


@pytest.mark.asyncio
async def test_real_preparation_checks_hook_actual_resources_and_unconnected_capacity(tmp_path):
    r, bot, _ = await installed(tmp_path)
    try:
        response = await projection(bot)
        assert response.status == 200
        data = json.loads(response.text)
        assert legacy_profile_resource_check(data)
        assert not module().capture_prepared(SimpleNamespace(engine_only=True, evaluation_epoch=EPOCH), data)
        assert data['llm']['start_at'] == '2026-10-16T00:30:00+00:00'
        assert data['llm']['admission_end_at'] == '2026-10-16T00:40:00+00:00'
        assert data['llm']['end_at'] == '2026-10-16T00:56:00+00:00'
        assert data['llm']['operational_demand'] == 16
        assert data['llm']['required_new_channels'] == 15
        assert data['llm']['connected'] is False
        assert data['llm']['llm_hook_bound'] is True
        assert data['llm']['rest_market_state_evidence'] is True
        assert not r.buffer.capture_status()['complete']  # No scan before open is expected.
        assert not bot.ws_feed._quote_subscription_owner._leases
    finally:
        await r.close('test_finished')


@pytest.mark.asyncio
@pytest.mark.parametrize('field,value', [
    ('retained_memory_limit_bytes', None),
    ('retained_memory_limit_bytes', 512 * 1024 * 1024 + 1),
    ('retained_memory_bytes', -1),
    ('retained_memory_bytes', True),
    ('retained_memory_bytes', 512 * 1024 * 1024 + 1),
    ('memory_exhausted', True),
    ('memory_exhausted', None),
])
async def test_activation_rejects_missing_unbounded_or_exhausted_memory(tmp_path, field, value):
    r, bot, _ = await installed(tmp_path)
    try:
        data = json.loads((await projection(bot)).text)
        data['llm'].update(retained_memory_limit_bytes=512 * 1024 * 1024,
                           retained_memory_bytes=0, memory_exhausted=False)
        assert legacy_profile_resource_check(data)
        data['llm'][field] = value
        assert not legacy_profile_resource_check(data)
    finally:
        await r.close('test_finished')


@pytest.mark.asyncio
@pytest.mark.parametrize('defect', ['hook', 'engine_hook', 'rest', 'state', 'version', 'owner',
    'uninitialized', 'capacity', 'queue', 'buffer', 'allocation', 'bytes', 'record_bytes',
    'drop', 'gap', 'scan_gap', 'disconnected_generation', 'connected_unready', 'end', 'writer', 'projection'])
async def test_prepared_never_promotes_missing_or_mutated_runtime(tmp_path, defect):
    r, bot, _ = await installed(tmp_path)
    q = bot.ws_feed._quote_subscription_owner
    try:
        if defect == 'hook': r.buffer.llm_quote_submit = None
        elif defect == 'engine_hook': bot.engine.risk_manager._entry_observation_owner = None
        elif defect == 'rest': r.buffer.rest_market_state_evidence = False
        elif defect == 'state': q.market_state_evidence = False
        elif defect == 'version': r.buffer.llm_admission = False
        elif defect == 'owner': q.observer = object()
        elif defect == 'uninitialized': q._operational_initialized = False
        elif defect == 'capacity': q._operational.append(('H0STASP0', '900001'))
        elif defect == 'queue': r.journal._queue.maxsize -= 1
        elif defect == 'buffer': r.buffer.capacity -= 1
        elif defect == 'allocation': r.buffer._record_limits['other'] -= 1
        elif defect == 'bytes': r.journal._max_bytes -= 1
        elif defect == 'record_bytes': r.journal._max_record_bytes -= 1
        elif defect == 'drop': r.buffer._dropped = 1
        elif defect == 'gap': r.buffer.mark_incomplete('synthetic_gap')
        elif defect == 'scan_gap': r.buffer.mark_incomplete('window_scan_not_recorded')
        elif defect == 'disconnected_generation': q.generation = 1
        elif defect == 'connected_unready': q._send = lambda *a: None
        elif defect == 'end': r.buffer.ended = True
        elif defect == 'writer': r.journal._fail('synthetic_writer_failed')
        elif defect == 'projection': r.buffer.selection_basis_settings = None
        response = await projection(bot)
        assert response.status == 503 or not legacy_profile_resource_check(json.loads(response.text))
    finally:
        q._send = None
        await r.close('test_finished')


@pytest.mark.asyncio
async def test_connected_operational_acks_suffice_without_future_candidate_acks(tmp_path):
    from test_quote_subscription import ack
    r, bot, _ = await installed(tmp_path)
    q = bot.ws_feed._quote_subscription_owner
    q.interval = 0
    try:
        assert await bot.ws_feed.connect()
        assert not legacy_profile_resource_check(json.loads((await projection(bot)).text))
        for key in list(q._states): await q.handle_ack(ack(key), q.generation)
        assert legacy_profile_resource_check(json.loads((await projection(bot)).text))
        assert not q._leases
    finally:
        await bot.ws_feed.disconnect()
        await r.close('test_finished')


@pytest.mark.asyncio
async def test_closing_actual_feed_cannot_reuse_old_owner_acks(tmp_path):
    from test_quote_subscription import ack
    r, bot, _ = await installed(tmp_path)
    feed = bot.ws_feed
    q = feed._quote_subscription_owner
    q.interval = 0
    closing = None
    try:
        assert await feed.connect()
        for key in list(q._states): await q.handle_ack(ack(key), q.generation)
        assert legacy_profile_resource_check(json.loads((await projection(bot)).text))
        await q._lock.acquire()
        closing = asyncio.create_task(feed._close_managed_socket(feed._ws, q.generation))
        await asyncio.sleep(0)
        assert feed._connected is False and q._send is not None
        assert not legacy_profile_resource_check(json.loads((await projection(bot)).text))
    finally:
        if q._lock.locked(): q._lock.release()
        if closing is not None: await closing
        await feed.disconnect()
        await r.close('test_finished')


@pytest.mark.parametrize('setup', [PROFILE], indirect=True)
@pytest.mark.asyncio
@pytest.mark.parametrize('defect', [None, 'old_start', 'old_admission', 'old_end', 'epoch', 'study', 'llm'])
async def test_retired_profile_rejects_future_safe_runtime_projection_without_checkout(setup, tmp_path, defect):
    s = setup
    object.__setattr__(s.cfg, 'old_head', s.cfg.new_head)
    s.source.write_bytes(b'new\n')
    r, bot, _ = await installed(tmp_path, head=s.cfg.new_head)
    # 합성 새 설치의 실제 지문을 연결하되 날짜 투영을 변조하지 않는다.
    # 고정 과거 profile은 새로운 안전 창을 활성화할 수 없어야 한다.
    once = tmp_path / 'once.json'
    object.__setattr__(s.cfg, 'once_path', once)
    object.__setattr__(s.cfg, 'study_sha256', r.plan.study_sha256)
    s.cfg.input_hashes.clear()
    s.cfg.input_hashes.update({str(tmp_path / name): sha256((tmp_path / name).read_bytes()).hexdigest()
                               for name in ('once.json', 'manifest.json', 'study.json')})
    s.staged.write_text(s.m.dropin_content(str(once)))
    object.__setattr__(s.cfg, 'dropin_sha256', sha256(s.staged.read_bytes()).hexdigest())
    try:
        readiness = json.loads((await projection(bot)).text)
        assert readiness['study_sha256'] == s.cfg.study_sha256
        assert readiness['evaluation_epoch'] == EPOCH
        if defect == 'old_start': readiness['llm']['start_at'] = '2026-10-16T00:00:00+00:00'
        elif defect == 'old_admission': readiness['llm']['admission_end_at'] = '2026-10-16T00:10:00+00:00'
        elif defect == 'old_end': readiness['llm']['end_at'] = '2026-10-16T00:26:00+00:00'
        elif defect == 'epoch': readiness['evaluation_epoch'] = '2026-10-15-engine-llm-window-v1'
        elif defect == 'study': readiness['study_sha256'] = 'f'*64
        elif defect == 'llm': readiness.pop('llm')
        s.anchors.update(readiness)
        result = s.run()
        assert result['status'] == 'failed' and not result['toss_attempted']
        assert result['restart_attempted'] is True and result['rollback'] == 'not_needed'
        assert sum('restart' in e for e in s.events) == 1
        assert not any('checkout' in e for e in s.events)
        count = len(s.events)
        assert s.run()['status'] == 'rejected' and len(s.events) == count
    finally:
        await r.close('test_finished')


@pytest.mark.parametrize('setup', [PROFILE], indirect=True)
def test_new_profile_rejects_deployment_before_any_side_effect(setup):
    result = setup.run()
    assert result['status'] == 'rejected'
    assert not setup.events
    assert not (setup.state / 'activation.receipt').exists()


def test_new_profile_date_and_engine_only_are_fixed():
    p = module().activation_profile(PROFILE)
    assert p.not_before == NOW and p.latest_start_at == NOW.replace(minute=12)
    assert p.engine_only and p.allow_predeployed and p.evaluation_epoch == EPOCH


@pytest.mark.parametrize('setup', [PROFILE], indirect=True)
@pytest.mark.parametrize('defect', [None, 'deployment', 'time', 'study', 'old_profile', 'extra_input'])
def test_new_profile_config_pins_sha_window_and_inputs(setup, monkeypatch, defect):
    s = setup
    installer = installer_fixture.__wrapped__()
    _, files = payloads(installer)
    raw = installer.document(files['activation'])
    raw['old_head'] = raw['new_head']
    if defect == 'deployment': raw['old_head'] = 'f'*40
    elif defect == 'time': raw['latest_start_at'] = '2026-10-16T00:13:00+00:00'
    elif defect == 'study': raw['study_sha256'] = 'f'*64
    elif defect == 'old_profile': raw['evaluation_epoch'] = '2026-10-15-engine-llm-window-v1'
    elif defect == 'extra_input': raw['input_hashes']['/etc/qwq-toss-observer/plan.json'] = 'f'*64
    monkeypatch.setattr(s.m.os, 'geteuid', lambda: 0)
    monkeypatch.setattr(s.m, 'trusted_dir', lambda *a: None)
    monkeypatch.setattr(s.m, 'read_file', lambda *a, **k: json.dumps(raw).encode())
    if defect:
        with pytest.raises(s.m.GuardError): s.m.load_config(PROFILE)
    else:
        cfg = s.m.load_config(PROFILE)
        assert cfg.engine_only and cfg.old_head == cfg.new_head


@pytest.mark.parametrize('setup', [PROFILE], indirect=True)
@pytest.mark.parametrize('instant', ['2026-10-16T00:09:59+00:00', '2026-10-16T00:12:00+00:00',
    '2026-10-16T09:09:59+09:00', '2026-10-16T09:12:00+09:00'])
def test_new_activation_rejects_outside_window_without_receipt(setup, instant):
    s = setup
    object.__setattr__(s.cfg, 'old_head', s.cfg.new_head)
    s.current[0] = datetime.fromisoformat(instant)
    assert s.run()['status'] == 'rejected'
    assert not s.events and not (s.state / 'activation.receipt').exists()


@pytest.mark.asyncio
async def test_fixed_date_first_five_repeat_sixth_and_finite_seal(tmp_path, monkeypatch):
    from src.analytics import entry_observation as obs
    from src.analytics.entry_observation_journal import read_observation_journal
    from test_llm_window_observation import enter
    from test_signal_window_observation import stock
    r, bot, clock = await installed(tmp_path)
    q = bot.ws_feed._quote_subscription_owner
    monkeypatch.setattr(obs, '_now', lambda: clock[0].isoformat())
    try:
        clock[0] = r.plan.start_at + timedelta(minutes=6)  # Delayed first screen.
        symbols = [f'{900001+i}' for i in range(6)]
        scan = obs.capture_scan(r.buffer, [stock(s) for s in symbols], 'regular')
        for i, symbol in enumerate(symbols): enter(r.buffer, scan, f'first-{i}', symbol)
        enter(r.buffer, scan, 'repeat', symbols[0])
        await asyncio.gather(*q._tasks)
        rows = [x for x in r.buffer._records if x['kind'] == 'llm_check_admission']
        assert [x['reason'] for x in rows] == ['llm_check_admitted']*5 + ['llm_check_limit', 'llm_check_reused']
        assert len(q._leases) == 5
        clock[0] = r.buffer.admission_end
        enter(r.buffer, scan, 'closed', symbols[0])
        assert r.buffer._records[-1]['reason'] == 'llm_check_admission_closed'
        clock[0] = r.plan.end_at
        r.start()
        await asyncio.wait_for(r.task, 2)
        assert r.result['sealed'] and not q._leases
        result = read_observation_journal(r.plan.settings['journal_path'],
            max_bytes=r.plan.settings['max_bytes'], expected_study_sha256=r.plan.study_sha256)
        assert result['complete'] and len(result['records']) > 10
        # This is structural capture completeness, without ACK/quotes/market validity.
        assert not any(x['kind'] in ('ws_quote', 'market_state') for x in result['records'])
    finally:
        await r.close('test_finished')


def test_new_llm_epoch_cannot_use_generic_window_readiness_fallback():
    cfg = SimpleNamespace(engine_only=True, evaluation_epoch=EPOCH)
    assert not module().capture_prepared(cfg, {'scan_scope': 'window'})


@pytest.mark.parametrize('argv,accepted', [
    (['--profile', PROFILE],True),
    (['--profile','20261017-llm-window-v1'],False),
    (['--profile',PROFILE,'--date','2026-10-17'],False),
    (['--profile',PROFILE,'--not-before','2026-10-16T00:00:00+00:00'],False),
])
def test_cli_accepts_only_explicit_reviewed_profile(monkeypatch,capsys,argv,accepted):
    m=module()
    selected=[]
    def load(profile):
        selected.append(profile)
        return profile
    monkeypatch.setattr(m.sys,'argv',[str(Path(m.__file__)),*argv])
    monkeypatch.setattr(m,'load_config',load)
    monkeypatch.setattr(m,'activate',lambda cfg:{'status':'complete'})
    assert m.main()==(0 if accepted else 1)
    result=json.loads(capsys.readouterr().out)
    assert result['status']==('complete' if accepted else 'rejected')
    assert selected==([PROFILE] if accepted else [])
