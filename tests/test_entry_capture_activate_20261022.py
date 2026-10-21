"""October 22 profile acceptance is narrow and cannot accept reverse-era readiness."""
from datetime import datetime, timezone
from types import SimpleNamespace
import json

import pytest
import test_entry_capture_activate as historical
from test_entry_capture_activate import module
from test_entry_capture_prepare_20261022 import m as installer_fixture, payloads


PROFILE = '20261022-pregate-v1'
EPOCH = '2026-10-22-engine-pregate-v1'
NOW = datetime(2026, 10, 22, 0, 25, tzinfo=timezone.utc)


@pytest.fixture
def setup(tmp_path, request):
    s = historical.setup.__wrapped__(tmp_path, request)
    original_runner = s.runner
    s.retirement_checks = []
    def runner(argv, *, timeout):
        if ('show' in argv and argv[-1] in ('qwq-entry-capture-20261021.timer',
                                            'qwq-entry-capture-20261021.service')):
            s.retirement_checks.append(tuple(argv))
            prop = next(a.split('=', 1)[1] for a in argv if a.startswith('--property='))
            value = 'disabled' if prop == 'UnitFileState' else 'inactive'
            if s.behavior.get('old_live') or (s.behavior.get('old_live_before_restart') and s.dropin.exists()):
                value = 'enabled' if prop == 'UnitFileState' else 'active'
            return SimpleNamespace(returncode=0, stdout=value)
        return original_runner(argv, timeout=timeout)
    s.runner = runner
    s.run = lambda: s.m.activate(s.cfg, runner=runner, clock=lambda: s.current[0], fetch=s.fetch,
        sleep=lambda seconds: s.current.__setitem__(0, s.current[0] + __import__('datetime').timedelta(seconds=seconds)),
        trusted_uid=__import__('os').getuid(), lock_uid=__import__('os').getuid(),
        engine_input_uid=__import__('os').getuid())
    return s


def projection():
    return {'scan_scope': 'first', 'capture_closed': False, 'first_scan': {
        'version': 'runner-first-scan-v4', 'quote_admission': 'first_scan', 'scan_scope': 'first',
        'observer_bound': True, 'rest_market_state_evidence': False, 'market_state_evidence': True,
        'market_state_max_records': 65000, 'operational_initialized': True,
        'start_at': '2026-10-22T00:30:00+00:00', 'admission_end_at': '2026-10-22T00:35:00+00:00',
        'end_at': '2026-10-22T00:51:00+00:00', 'max_scans': 1,
        'selection_basis_version': 'selection-basis-v4', 'selection_max_modifier_stages': 24,
        'selection_max_candidates': 100, 'gate_max_candidates': 100, 'max_candidates': 5,
        'registration_cap': 41, 'external_reserved': 0, 'operational_headroom': 10,
        'required_new_channels': 15, 'buffer_capacity': 100713, 'queue_capacity': 100713,
        'batch_size': 100, 'max_record_bytes': 65536, 'max_bytes': 240144543,
        'journal_budget_version': 'bounded-envelope-v1', 'dropped_records': 0,
        'retained_memory_limit_bytes': 512 * 1024 * 1024, 'retained_memory_bytes': 0,
        'memory_exhausted': False,
        'record_allocations': {'scan_metadata': 201, 'ws_quote': 35000, 'market_state': 65000, 'other': 512},
        'record_max_bytes': {'scan_metadata': 65536, 'ws_quote': 2048, 'market_state': 2048, 'other': 4096},
        'record_counts': {'scan_metadata': 0, 'ws_quote': 0, 'market_state': 0, 'other': 0},
        'incomplete_reasons': [], 'lease_seconds': 1262, 'operational_demand': 16, 'generation': 0,
        'connected': False, 'connection_pristine': True, 'operational_stream_ready': False}}


def test_oct22_activation_profile_is_available_and_fixed():
    p = module().activation_profile(PROFILE)
    assert p.not_before == NOW and p.latest_start_at == NOW.replace(minute=27)
    assert p.engine_only and p.allow_predeployed and p.evaluation_epoch == EPOCH


def test_oct22_runtime_readiness_accepts_only_exact_live_contract():
    cfg = SimpleNamespace(engine_only=True, evaluation_epoch=EPOCH)
    assert module().capture_prepared(cfg, projection())
    live = projection(); s = live['first_scan']
    s.update(connected=True, connection_pristine=False, operational_stream_ready=True, generation=1)
    assert module().capture_prepared(cfg, live)


@pytest.mark.parametrize('key,value', [
    ('journal_budget_version', None), ('journal_budget_version', 'bounded-envelope-v2'),
    ('start_at', '2026-10-21T00:30:00+00:00'), ('end_at', '2026-10-22T00:51:01+00:00'),
    ('market_state_max_records', 19000), ('buffer_capacity', 40713),
    ('queue_capacity', 40713), ('max_bytes', 262492160), ('lease_seconds', 1260),
    ('record_allocations', {'scan_metadata': 201, 'ws_quote': 21000, 'market_state': 19000, 'other': 512}),
    ('record_max_bytes', {'scan_metadata': 65536, 'ws_quote': 4096, 'market_state': 4096, 'other': 4096}),
])
def test_oct22_rejects_old_or_incomplete_runtime_contract(key, value):
    data = projection(); data['first_scan'][key] = value
    assert not module().capture_prepared(SimpleNamespace(engine_only=True, evaluation_epoch=EPOCH), data)


def test_oct22_does_not_accept_oct21_readiness_and_oct21_does_not_accept_oct22():
    m = module()
    assert not m.capture_prepared(SimpleNamespace(engine_only=True,
        evaluation_epoch='2026-10-21-engine-pregate-v1'), projection())
    old = projection(); old['first_scan'].update(start_at='2026-10-21T00:30:00+00:00',
        admission_end_at='2026-10-21T00:35:00+00:00', end_at='2026-10-21T00:51:00+00:00',
        market_state_max_records=19000, buffer_capacity=40713, queue_capacity=40713,
        max_bytes=262492160, journal_budget_version='bounded-envelope-v1',
        record_allocations={'scan_metadata': 201, 'ws_quote': 21000, 'market_state': 19000, 'other': 512},
        record_max_bytes={'scan_metadata': 65536, 'ws_quote': 4096, 'market_state': 4096, 'other': 4096})
    assert not m.capture_prepared(SimpleNamespace(engine_only=True, evaluation_epoch=EPOCH), old)


@pytest.mark.parametrize('setup', [PROFILE], indirect=True)
def test_same_sha_restarts_once_and_checks_consumed_oct21_twice(setup):
    setup.source.write_bytes(b'new\n')
    object.__setattr__(setup.cfg, 'old_head', setup.cfg.new_head)
    setup.anchors.update(projection())
    result = setup.run()
    assert result['status'] == 'complete' and result['restart_attempted'] is True
    assert sum('restart' in event for event in setup.events) == 1
    expected = [
        ('/usr/bin/systemctl', 'show', '--property=ActiveState', '--value', 'qwq-entry-capture-20261021.timer'),
        ('/usr/bin/systemctl', 'show', '--property=UnitFileState', '--value', 'qwq-entry-capture-20261021.timer'),
        ('/usr/bin/systemctl', 'show', '--property=ActiveState', '--value', 'qwq-entry-capture-20261021.service'),
    ]
    assert setup.retirement_checks == expected * 2


@pytest.mark.parametrize('setup', [PROFILE], indirect=True)
@pytest.mark.parametrize('late', [False, True])
def test_rearmed_oct21_timer_blocks_before_or_after_receipt(setup, late):
    object.__setattr__(setup.cfg, 'old_head', setup.cfg.new_head)
    setup.source.write_bytes(b'new\n')
    setup.anchors.update(projection())
    setup.behavior['old_live_before_restart' if late else 'old_live'] = True
    result = setup.run()
    assert result['status'] != 'complete' and not result['restart_attempted']
    assert (setup.state / 'activation.receipt').exists() is late
    assert not setup.dropin.exists()


@pytest.mark.parametrize('setup', [PROFILE], indirect=True)
@pytest.mark.parametrize('replace_inode', [False, True])
def test_changed_dropin_never_restarts_and_preserves_replaced_inode(setup, replace_inode):
    object.__setattr__(setup.cfg, 'old_head', setup.cfg.new_head)
    setup.source.write_bytes(b'new\n')
    setup.anchors.update(projection())
    changed = False
    def replace(argv):
        nonlocal changed
        if 'daemon-reload' in argv and not changed:
            changed = True
            if replace_inode:
                setup.dropin.rename(setup.dropin.with_suffix('.original'))
            setup.dropin.write_bytes(b'other-owner')
    setup.behavior['hook'] = replace
    result = setup.run()
    assert result['status'] == 'failed' and not result['restart_attempted']
    if replace_inode:
        assert setup.dropin.read_bytes() == b'other-owner' and result['rollback'] == 'failed'
    else:
        assert not setup.dropin.exists() and result['rollback'] == 'complete'


@pytest.mark.parametrize('setup', [PROFILE], indirect=True)
def test_restart_failure_never_retries_or_rolls_back(setup):
    setup.anchors.update(projection())
    historical.test_predeployed_restart_failure_never_rolls_back_or_retries(setup)


@pytest.mark.asyncio
async def test_krapi_runtime_readiness_accepts_exact_oct22_after_existing_book(tmp_path):
    """Readiness must come from the live runtime, including an operational BOOK."""
    from src.analytics.entry_observation_runtime import CapturePlan, ObservationRuntime
    from src.analytics.entry_observation_startup import claim_once_manifest
    from src.dashboard.kr_api import KRAPIHandler
    from test_entry_observation_readiness import request
    from test_market_state_pilot_runtime import bot_with
    from test_quote_subscription import ack
    from test_market_state_capture import BOOK, frame, receive
    from src.analytics.entry_observation import capture_quote
    installer = installer_fixture.__wrapped__()
    installer.ENGINE_STATE = tmp_path / 'state'
    installer.ENGINE_STATE.mkdir(mode=0o700)
    installer.NEW = {**installer.NEW, **{key: tmp_path / name for key, name in
        (('study', 'study.json'), ('engine_manifest', 'manifest.json'), ('once', 'once.json'))}}
    document, files = payloads(installer)
    installer.validate_payload(document, files)
    for key in ('study', 'engine_manifest', 'once'):
        installer.NEW[key].write_bytes(files[key])
        installer.NEW[key].chmod(0o600)
    selected = claim_once_manifest(installer.NEW['once'], now=NOW)
    assert selected == str(installer.NEW['engine_manifest'])
    plan = CapturePlan.load(selected, now=NOW)
    bot = bot_with([f'{100000 + i}' for i in range(8)])
    bot.engine = SimpleNamespace(risk_manager=SimpleNamespace(_entry_observation_owner=bot))
    runtime = await ObservationRuntime.install(bot, plan, now=lambda: NOW)
    try:
        feed, owner = bot.ws_feed, bot.ws_feed._quote_subscription_owner
        owner.interval = 0
        async def quote(event):
            capture_quote(runtime.buffer, event)
        feed.on_quote(quote)
        assert await feed.connect()
        for key in list(owner._states):
            await owner.handle_ack(ack(key), owner.generation)
        await receive(feed, owner, frame(BOOK, '100000'))
        data = json.loads((await KRAPIHandler(SimpleNamespace(bot=bot)).get_entry_observation_readiness(request())).text)
        first = data['first_scan']
        assert data['evaluation_epoch'] == EPOCH and data['study_sha256'] == plan.study_sha256
        assert first['record_counts']['ws_quote'] == 1 and first['incomplete_reasons'] == []
        assert first['operational_stream_ready'] is True
        assert module().capture_prepared(SimpleNamespace(engine_only=True, evaluation_epoch=EPOCH), data)
    finally:
        await bot.ws_feed.disconnect()
        await runtime.close('test_finished')
