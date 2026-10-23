"""실제 로컬 pipe 자식으로 보고 helper의 경계와 정리를 검증한다."""
import asyncio
from datetime import date, datetime, timedelta
import importlib.util
import sys
from zoneinfo import ZoneInfo

import pytest

AT = datetime(2026, 10, 26, 16, 0, 3, tzinfo=ZoneInfo('Asia/Seoul'))
CHILD = r'''
import json,sys,datetime,time
for line in sys.stdin.buffer:
 r=json.loads(line); c=r['cohort']; t=datetime.datetime.fromisoformat(r['requested_at'])
 q=[dict(symbol=m['symbol'],price='123000',observed_at=t.isoformat(),fetched_at=t.isoformat(),market_basis='unknown',currency='KRW') for m in c['members']]
 result=dict(schema_version=1,request_id=r['request_id'],cohort_id=c['cohort_id'],status='ok',valid_until=(t+datetime.timedelta(seconds=60)).isoformat(),quotes=q)
 print(json.dumps(result),flush=True)
'''


def test_client_is_available():
    assert importlib.util.find_spec('src.data.providers.toss.report_helper') is not None


def cohort(symbol='000660'):
    from src.data.providers.toss.report_protocol import ReportCohort
    return ReportCohort.create(date(2026, 10, 26), AT.replace(hour=8, minute=25), ((1, symbol),))


@pytest.fixture
def module(monkeypatch):
    from src.data.providers.toss import report_helper as m
    monkeypatch.setenv('TOSS_REPORT_API', '1')
    monkeypatch.setattr(m, '_now', lambda: AT)
    monkeypatch.setattr(m, '_ARGV', (sys.executable, '-u', '-c', CHILD))
    return m


@pytest.mark.asyncio
async def test_off_does_not_spawn_or_read_credentials(module, monkeypatch):
    monkeypatch.setenv('TOSS_REPORT_API', '0')
    async def forbidden(*a, **kw): raise AssertionError('spawn while OFF')
    monkeypatch.setattr(module.asyncio, 'create_subprocess_exec', forbidden)
    client = module.ReportHelperClient(enabled=lambda: True)
    assert not await client.prepare_report(cohort())
    assert client.get_report_quote('000660') is None
    await client.close()


@pytest.mark.asyncio
async def test_real_pipe_round_trip_preserves_price_time_and_limits_environment(module, monkeypatch):
    original = asyncio.create_subprocess_exec
    calls = []
    monkeypatch.setenv('KIS_APP_SECRET', 'synthetic-do-not-forward')
    monkeypatch.setenv('OPENAI_API_KEY', 'synthetic-do-not-forward')
    monkeypatch.setenv('TOSS_CLIENT_ID', 'synthetic-id')
    monkeypatch.setenv('TOSS_CLIENT_SECRET', 'synthetic-secret')
    async def spawn(*a, **kw):
        calls.append((a, kw))
        return await original(*a, **kw)
    monkeypatch.setattr(module.asyncio, 'create_subprocess_exec', spawn)
    client = module.ReportHelperClient(enabled=lambda: True)
    try:
        assert await client.prepare_report(cohort())
        q = client.get_report_quote('000660')
        assert str(q.price) == '123000' and q.observed_at == AT and q.market_basis == 'unknown'
        assert set(calls[0][1]['env']) <= {'TOSS_API', 'TOSS_CLIENT_ID', 'TOSS_CLIENT_SECRET', 'PATH', 'LANG'}
        assert calls[0][1]['env']['TOSS_API'] == '1'
        assert not await client.prepare_report(cohort())
        assert len(calls) == 1
    finally:
        await client.close()
    assert client._process.returncode is not None
    assert client.get_report_quote('000660') is None


@pytest.mark.asyncio
@pytest.mark.parametrize('child', [
    CHILD.replace("request_id=r['request_id']", "request_id='b'*32"),
    "import sys,time;sys.stdin.buffer.readline();sys.stdout.write('x'*70000);sys.stdout.flush();time.sleep(10)",
    "import sys;sys.stdin.buffer.readline();print('{',flush=True)",
])
async def test_bad_reply_closes_child_and_never_restarts(module, monkeypatch, child):
    monkeypatch.setattr(module, '_ARGV', (sys.executable, '-u', '-c', child))
    client = module.ReportHelperClient(enabled=lambda: True)
    assert not await client.prepare_report(cohort(), timeout=2)
    first = client._process
    assert first.returncode is not None
    assert not await client.prepare_report(cohort('005930'), timeout=2)
    assert client._process is first
    await client.close()


@pytest.mark.asyncio
async def test_timeout_and_cancellation_reap_actual_child(module, monkeypatch):
    child = 'import sys,time;sys.stdin.buffer.readline();time.sleep(10)'
    monkeypatch.setattr(module, '_ARGV', (sys.executable, '-u', '-c', child))
    client = module.ReportHelperClient(enabled=lambda: True)
    assert not await client.prepare_report(cohort(), timeout=.1)
    assert client._process.returncode is not None
    client2 = module.ReportHelperClient(enabled=lambda: True)
    task = asyncio.create_task(client2.prepare_report(cohort(), timeout=3))
    for _ in range(100):
        if client2._process is not None: break
        await asyncio.sleep(.01)
    assert client2._process is not None
    task.cancel()
    with pytest.raises(asyncio.CancelledError): await task
    assert client2._process.returncode is not None
    await client.close(); await client2.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['wall_backwards', 'monotonic_expired', 'child_dead', 'flag_off'])
async def test_published_quote_invalidated_when_runtime_or_original_lease_ends(module, monkeypatch, change):
    client = module.ReportHelperClient(enabled=lambda: True)
    try:
        assert await client.prepare_report(cohort())
        if change == 'wall_backwards': monkeypatch.setattr(module, '_now', lambda: AT - timedelta(seconds=1))
        elif change == 'monotonic_expired':
            current = module._clock()
            monkeypatch.setattr(module, '_clock', lambda: current + 61)
        elif change == 'child_dead':
            client._process.kill(); await client._process.wait()
        else: monkeypatch.setenv('TOSS_REPORT_API', '0')
        assert client.get_report_quote('000660') is None
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_flag_off_during_pending_request_stops_helper_without_waiting_for_request_deadline(module, monkeypatch):
    child = 'import sys,time;sys.stdin.buffer.readline();time.sleep(10)'
    monkeypatch.setattr(module, '_ARGV', (sys.executable, '-u', '-c', child))
    client = module.ReportHelperClient(enabled=lambda: True)
    task = asyncio.create_task(client.prepare_report(cohort(), timeout=20))
    try:
        for _ in range(100):
            if client._process is not None: break
            await asyncio.sleep(.01)
        assert client._process is not None
        monkeypatch.setenv('TOSS_REPORT_API', '0')
        assert await asyncio.wait_for(asyncio.shield(task), 3) is False
        assert client._process.returncode is not None
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await client.close()


@pytest.mark.asyncio
async def test_cancelled_spawn_late_completion_is_owned_and_reaped(module, monkeypatch):
    original = asyncio.create_subprocess_exec
    created, release = asyncio.Event(), asyncio.Event()
    processes = []
    async def slow_spawn(*args, **kwargs):
        process = await original(*args, **kwargs)
        processes.append(process)
        created.set()
        await release.wait()
        return process
    monkeypatch.setattr(module.asyncio, 'create_subprocess_exec', slow_spawn)
    client = module.ReportHelperClient(enabled=lambda: True)
    task = asyncio.create_task(client.prepare_report(cohort(), timeout=20))
    await created.wait()
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError): await task
    assert len(processes) == 1 and processes[0].returncode is not None
    assert not await client.prepare_report(cohort('005930'))


@pytest.mark.asyncio
async def test_stdout_flood_with_ignored_terminate_is_reaped_within_cleanup_bound(module, monkeypatch):
    child = "import sys,signal;signal.signal(signal.SIGTERM,signal.SIG_IGN);sys.stdin.buffer.readline();sys.stdout.buffer.write(b'x'*10000000);sys.stdout.buffer.flush()"
    monkeypatch.setattr(module, '_ARGV', (sys.executable, '-I', '-S', '-u', '-c', child))
    client = module.ReportHelperClient(enabled=lambda: True)
    task = asyncio.create_task(client.prepare_report(cohort(), timeout=.25))
    try:
        done, _ = await asyncio.wait((task,), timeout=7)
        assert task in done, 'stdout backpressure must not block killed-child cleanup'
        assert task.result() is False
        assert client._process.returncode is not None
    finally:
        if not task.done():
            client._process.stdout._transport.close()
        await asyncio.wait_for(task, 2)
        await client.close()


@pytest.mark.asyncio
async def test_wall_rollback_during_response_does_not_extend_original_monotonic_ttl(module, monkeypatch):
    from decimal import Decimal
    from types import SimpleNamespace
    from src.data.providers.toss.market_types import Quote
    from src.data.providers.toss.report_protocol import make_result
    clock, wall = [0.0], [AT]
    monkeypatch.setattr(module, '_clock', lambda: clock[0])
    monkeypatch.setattr(module, '_now', lambda: wall[0])
    client = module.ReportHelperClient(enabled=lambda: True)
    client._process = SimpleNamespace(returncode=None)
    async def exchange(request, deadline):
        clock[0], wall[0] = 44.0, AT + timedelta(seconds=1)
        quote = Quote('000660', Decimal('123000'), AT, AT, 'ok', frozenset({'market_basis'}), 'unknown', 'KRW')
        return make_result(request, quotes=(quote,), valid_until=AT + timedelta(seconds=60))
    monkeypatch.setattr(client, '_bounded_exchange', exchange)
    assert await client.prepare_report(cohort())
    clock[0], wall[0] = 61.0, AT + timedelta(seconds=18)
    assert client.get_report_quote('000660') is None


@pytest.mark.asyncio
async def test_quote_cannot_be_reused_by_another_report_cohort(module):
    from src.data.providers.toss.report_protocol import ReportCohort
    current = cohort()
    other = ReportCohort.create(current.report_date, AT.replace(hour=8, minute=26), current.members)
    client = module.ReportHelperClient(enabled=lambda: True)
    try:
        assert await client.prepare_report(current)
        assert client.get_report_quote('000660', cohort=current) is not None
        assert client.get_report_quote('000660', cohort=other) is None
    finally:
        await client.close()
