"""Lazy assembly and lifecycle ordering with fake in-memory components."""
import asyncio
import threading
from types import SimpleNamespace
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from src.data.providers.toss.runtime_factory import ObservationApp


def fixture_app(monkeypatch, *, cached=True):
    monkeypatch.setenv('TOSS_API', '1')
    calls = []

    def require(operation, *, deadline):
        calls.append('authorize:' + operation)
        return deadline

    authority = SimpleNamespace(
        clock=lambda: 0, require=require, stop=lambda: calls.append('stop'),
        plan=SimpleNamespace(document={'limits': {'job_timeout_seconds': 1}}),
        grant=SimpleNamespace(capabilities={'bootstrap': True}),
    )

    async def client_start(): calls.append('sender_acquired')
    async def client_close(): calls.append('sender_released')
    async def oauth_close(): calls.append('oauth_closed')
    async def token_get(*, deadline):
        calls.append('cached_query')
        if not cached:
            from src.data.providers.toss.token_store import TokenError
            raise TokenError('auth_unavailable')
        return 'synthetic-only'
    async def bootstrap(*, deadline): calls.append('bootstrap')

    app = ObservationApp(
        authority, threading.Event(),
        SimpleNamespace(start=client_start, close=client_close),
        SimpleNamespace(close=oauth_close),
        SimpleNamespace(bootstrap=bootstrap, get_token=token_get),
        SimpleNamespace(open=lambda: calls.append('ledger_open'),
                        configure_plan=lambda policy: calls.append('plan_durable'),
                        close=lambda: calls.append('ledger_close')),
        None,
    )
    return app, calls


def test_restart_with_existing_cache_does_not_spend_bootstrap_again(monkeypatch):
    app, calls = fixture_app(monkeypatch)
    asyncio.run(app.start())
    assert (calls.index('sender_acquired') < calls.index('ledger_open')
            < calls.index('plan_durable') < calls.index('cached_query'))
    assert 'bootstrap' not in calls
    asyncio.run(app.close())
    assert calls[-4:] == ['stop', 'oauth_closed', 'sender_released', 'ledger_close']


def test_initial_missing_cache_uses_separate_bootstrap_grant(monkeypatch):
    app, calls = fixture_app(monkeypatch, cached=False)
    asyncio.run(app.start())
    assert calls.index('cached_query') < calls.index('bootstrap')


def test_factory_construction_does_not_touch_tokens_files_or_keys(monkeypatch):
    app, calls = fixture_app(monkeypatch)
    assert calls == []
    app.stop_event.set()
    try:
        asyncio.run(app.start())
    except Exception as error:
        assert str(error) == 'stopping'
    else:
        raise AssertionError('stopped app started')
    assert calls == []


def test_price_send_deadline_cannot_escape_fixed_slot_or_session():
    from src.data.providers.toss.runtime_factory import command_deadline
    from src.data.providers.toss.runtime import WorkerCommand, RuntimeUnavailable
    policy = {'dates': ('2026-09-16',), 'calendar_time': '08:00',
              'sessions': ({'name': 'regular', 'start': '09:00', 'end': '09:12'},),
              'limits': {'job_timeout_seconds': 300}}
    kst = ZoneInfo('Asia/Seoul')
    command = WorkerCommand('prices', '2026-09-16T09:10:00+09:00', {})
    assert command_deadline(command, policy, now=datetime(2026, 9, 16, 9, 11, tzinfo=kst), clock=lambda: 100) == 160
    with pytest.raises(RuntimeUnavailable):
        command_deadline(command, policy, now=datetime(2026, 9, 16, 9, 12, tzinfo=kst), clock=lambda: 100)
    with pytest.raises(RuntimeUnavailable):
        command_deadline(command, policy, now=datetime(2026, 9, 17, 9, 11, tzinfo=kst), clock=lambda: 100)


def test_calendar_query_allowed_before_regular_session_not_next_day():
    from src.data.providers.toss.runtime_factory import command_deadline
    from src.data.providers.toss.runtime import WorkerCommand, RuntimeUnavailable
    policy = {'dates': ('2026-09-16',), 'calendar_time': '08:00',
              'sessions': ({'name': 'regular', 'start': '09:00', 'end': '15:30'},),
              'limits': {'job_timeout_seconds': 300}}
    kst = ZoneInfo('Asia/Seoul')
    command = WorkerCommand('calendar', '2026-09-16T08:00:00+09:00', requested_date='2026-09-16')
    assert command_deadline(command, policy, now=datetime(2026, 9, 16, 8, 1, tzinfo=kst), clock=lambda: 100) == 400
    with pytest.raises(RuntimeUnavailable):
        command_deadline(command, policy, now=datetime(2026, 9, 17, 0, 1, tzinfo=kst), clock=lambda: 100)


def test_deployment_rejects_missing_startup_attestation_before_file_loader(tmp_path):
    from src.data.providers.toss.runtime_factory import Deployment
    deployment = Deployment(tmp_path / 'registry.json', tmp_path / 'plan.json', 'test',
                            object(), object(), 1)
    with pytest.raises(Exception) as error:
        deployment.load()
    assert str(error.value) == 'approval_unavailable'
    assert list(tmp_path.iterdir()) == []


def test_startup_attestation_checked_against_actual_identity(monkeypatch):
    from src.data.providers.toss.runtime_factory import StartupAttestation, validate_attestation
    identity = SimpleNamespace(release_id='fixed-release', config_hash='a' * 64,
                               host_identity='synthetic-host', service_uid=1234)
    attestation = StartupAttestation('fixed-release', 'a' * 64, 'b' * 64, 'synthetic-host', 1234)
    monkeypatch.setattr('os.geteuid', lambda: 1234)
    monkeypatch.setattr('socket.gethostname', lambda: 'synthetic-host')
    validate_attestation(attestation, identity, expected_artifact_sha256='b' * 64)
    monkeypatch.setattr('os.geteuid', lambda: 1235)
    with pytest.raises(Exception, match='approval_unavailable'):
        validate_attestation(attestation, identity, expected_artifact_sha256='b' * 64)


def test_factory_off_refuses_before_loading_auth_ledger_or_credentials(monkeypatch):
    from src.data.providers.toss.runtime_factory import build_app
    monkeypatch.setenv('TOSS_API', '0')
    with pytest.raises(Exception, match='stopping'):
        build_app(object(), threading.Event())


@pytest.mark.parametrize('operation,wall_seconds,mono_seconds', [
    ('renewal', 2, 2), ('query', 61, 0), ('query', -1, 2),
])
def test_factory_send_gates_preserve_slot_cutoff_and_recheck_wall_clock(
        tmp_path, monkeypatch, operation, wall_seconds, mono_seconds):
    import sys
    from datetime import timedelta
    from types import ModuleType
    from test_toss_live_authority import authority_fixture, plan_document, raw
    from src.data.providers.toss import oauth, transport
    from src.data.providers.toss.runtime import WorkerCommand, RuntimeUnavailable
    from src.data.providers.toss.runtime_factory import build_app
    mod, kwargs, grant, stamp, ticks = authority_fixture(tmp_path, monkeypatch)
    policy = plan_document()
    policy['sessions'][0]['end'] = '09:12'
    policy['limits']['job_timeout_seconds'] = 300
    plan = mod.ObservationPlan.from_bytes(raw(policy))
    grant.update(plan_raw_hash=plan.raw_hash, plan_canonical_hash=plan.canonical_hash,
                 expires_at=(stamp[0] + timedelta(hours=1)).isoformat())
    kwargs['plan_path'].write_bytes(raw(policy))
    kwargs['registry_path'].write_bytes(raw(dict(schema_version=1, grants=[grant])))
    stamp[0] += timedelta(minutes=11, seconds=59)
    authority = mod.load_authority(**kwargs)
    callbacks = {}
    class OAuth:
        def __init__(self, **options): callbacks['renewal'] = options['authorize']
        async def issue(self, **kwargs): raise AssertionError('no issue in boundary test')
        def can_issue(self): return True
    class Transport:
        def __init__(self, **options): callbacks['query'] = options['authorize']
    class Runner:
        def __init__(self, **options): pass
        async def prices(self, **options):
            stamp[0] += timedelta(seconds=wall_seconds)
            ticks[0] += mono_seconds
            if operation == 'renewal':
                return callbacks[operation](operation, deadline=1000)
            return callbacks[operation](deadline=1000)
    observation = ModuleType('src.data.providers.toss.observation')
    observation.ObservationRunner = Runner
    observation.ObservationResult = SimpleNamespace
    ledger = ModuleType('src.data.providers.toss.observation_ledger')
    ledger.ObservationLedger = lambda *args, **kwargs: None
    monkeypatch.setitem(sys.modules, observation.__name__, observation)
    monkeypatch.setitem(sys.modules, ledger.__name__, ledger)
    monkeypatch.setattr(oauth, 'OAuthIssuer', OAuth)
    monkeypatch.setattr(transport, 'AiohttpTransport', Transport)
    monkeypatch.setenv('TOSS_API', '1')
    app = build_app(authority, threading.Event())
    with pytest.raises((RuntimeUnavailable, mod.ApprovalError)) as failure:
        asyncio.run(app.run(WorkerCommand('prices', '2026-09-16T09:10:00+09:00', {})))
    assert failure.value.code in {'invalid_command', 'approval_expired'}
    assert app.query_scope.get() is None
