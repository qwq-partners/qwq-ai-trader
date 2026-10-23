"""새 목적 승인에서만 완결된 관측을 보고서로 전달한다. 외부 접속 없음."""
import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from tests.test_toss_live_authority import authority_fixture, plan_document, raw
from src.data.providers.toss.approval import ApprovalError, ObservationPlan
from src.data.providers.toss.market_types import Quote
from src.data.providers.toss.observation import ObservationResult, ObservationRunner, select_snapshot
from src.data.providers.toss.observation_ledger import ObservationLedger
from src.data.providers.toss.runtime import WorkerCommand


def approved(tmp_path, monkeypatch, *, version=3, report_capability=True):
    mod, kwargs, grant, stamp, ticks = authority_fixture(tmp_path, monkeypatch)
    p = plan_document()
    if version == 3:
        p.update(schema_version=3, report_display=dict(purpose='daily_report_current_price', max_age_seconds=60))
        grant.update(schema_version=3)
        grant['capabilities']['report_display'] = report_capability
    plan = ObservationPlan.from_bytes(raw(p))
    grant.update(plan_raw_hash=plan.raw_hash, plan_canonical_hash=plan.canonical_hash,
                 expires_at=(stamp[0] + timedelta(hours=2)).isoformat())
    kwargs['plan_path'].write_bytes(raw(p))
    kwargs['registry_path'].write_bytes(raw(dict(schema_version=1, grants=[grant])))
    stamp[0] += timedelta(hours=1)
    return mod.load_authority(**kwargs), stamp, ticks


def test_new_plan_binds_display_purpose_and_requires_new_grant(tmp_path, monkeypatch):
    authority, _, _ = approved(tmp_path, monkeypatch)
    assert authority.require('report_display', deadline=101) == 101
    assert authority.plan.document['report_display']['purpose'] == 'daily_report_current_price'


@pytest.mark.parametrize('field,value', [('purpose', 'trading'), ('max_age_seconds', 0),
                                          ('max_age_seconds', 301), ('max_age_seconds', True)])
def test_invalid_display_policy_rejected(field, value):
    p = plan_document()
    p.update(schema_version=3, report_display=dict(purpose='daily_report_current_price', max_age_seconds=60))
    p['report_display'][field] = value
    with pytest.raises(ApprovalError):
        ObservationPlan.from_bytes(raw(p))


def test_runner_returns_original_quotes_only_after_terminal_persistence(tmp_path):
    now = datetime(2026, 9, 16, 1, tzinfo=timezone.utc)
    p = plan_document()
    class Client:
        async def get(self, *args, **kwargs):
            return {'result': [dict(symbol='005930', lastPrice='101', currency='KRW',
                                    timestamp='2026-09-16T09:59:55+09:00')]}
    ledger = ObservationLedger(tmp_path / 'ledger', plan_hash='a' * 64, max_bytes=100_000)
    ledger.open()
    snapshot = select_snapshot(candidates=(('005930', 1),), holdings=(), source_success_at=now,
                               now=now, policy=p, kis_quotes={})
    runner = ObservationRunner(client=Client(), ledger=ledger, policy=p, now=lambda: now)
    result = asyncio.run(runner.prices(slot_id='slot', snapshot=snapshot))
    assert result.quotes[0].price == Decimal('101')
    assert result.quotes[0].observed_at.isoformat() == '2026-09-16T09:59:55+09:00'
    assert ledger.summary()['terminal_attempts'] == 1
    assert asyncio.run(runner.prices(slot_id='slot', snapshot=snapshot)).quotes == ()
    ledger.close()


def sample(authority):
    now = authority.now()
    quote = Quote('005930', Decimal('101'), now - timedelta(seconds=5), now, 'ok',
                  frozenset(), 'unknown', 'KRW')
    result = ObservationResult('success', True, 1, 0, True, 'ok', quotes=(quote,))
    command = WorkerCommand('prices', now.astimezone(timezone(timedelta(hours=9))).isoformat(),
                            {'symbols': ('005930',)})
    return quote, result, command


def test_provider_preserves_unknown_basis_and_original_times_and_expires(tmp_path, monkeypatch):
    from src.data.providers.toss.report_quotes import ReportQuotes
    authority, stamp, _ = approved(tmp_path, monkeypatch)
    cache = ReportQuotes(authority, enabled=lambda: True)
    quote, result, command = sample(authority)
    assert cache.publish(result, command=command, deadline=110)
    assert cache.get_quote('005930') is quote
    stamp[0] += timedelta(seconds=56)
    assert cache.get_quote('005930') is None


def test_old_authority_cannot_construct_consumer(tmp_path, monkeypatch):
    from src.data.providers.toss.report_quotes import ReportQuotes
    authority, _, _ = approved(tmp_path, monkeypatch, version=1)
    with pytest.raises(ApprovalError):
        ReportQuotes(authority, enabled=lambda: True)


@pytest.mark.parametrize('change', [dict(ledger_complete=False), dict(outcome='failure'),
                                   dict(outcome='duplicate'), dict(provider_failures=1), dict(budget_skips=1)])
def test_bad_completion_clears_previous_quote(tmp_path, monkeypatch, change):
    from src.data.providers.toss.report_quotes import ReportQuotes
    authority, _, _ = approved(tmp_path, monkeypatch)
    cache = ReportQuotes(authority, enabled=lambda: True)
    _, result, command = sample(authority)
    cache.publish(result, command=command, deadline=110)
    assert not cache.publish(replace(result, **change), command=command, deadline=110)
    assert cache.get_quote('005930') is None


@pytest.mark.parametrize('reason', ['disabled', 'stopped', 'expired', 'late', 'session_end'])
def test_stop_and_late_outcome_cannot_refresh(tmp_path, monkeypatch, reason):
    from src.data.providers.toss.report_quotes import ReportQuotes
    authority, stamp, ticks = approved(tmp_path, monkeypatch)
    enabled = [True]
    cache = ReportQuotes(authority, enabled=lambda: enabled[0])
    _, result, command = sample(authority)
    cache.publish(result, command=command, deadline=110)
    if reason == 'disabled': enabled[0] = False
    elif reason == 'stopped': authority.stop()
    elif reason == 'expired': stamp[0] += timedelta(hours=3)
    elif reason == 'late': ticks[0] = 110
    else: stamp[0] += timedelta(hours=6)
    assert not cache.publish(result, command=command, deadline=110)
    assert cache.get_quote('005930') is None


@pytest.mark.parametrize('change', [dict(price=Decimal('NaN')), dict(currency='USD'),
    dict(observed_at=None), dict(status='stale'), dict(symbol='other'),
    dict(observed_at=datetime(2027, 1, 1, tzinfo=timezone.utc))])
def test_malformed_quote_is_not_published(tmp_path, monkeypatch, change):
    from src.data.providers.toss.report_quotes import ReportQuotes
    authority, _, _ = approved(tmp_path, monkeypatch)
    cache = ReportQuotes(authority, enabled=lambda: True)
    quote, result, command = sample(authority)
    assert not cache.publish(replace(result, quotes=(replace(quote, **change),)), command=command, deadline=110)
    assert cache.get_quote('005930') is None


def test_daily_report_consumes_real_observation_without_fallback(tmp_path, monkeypatch):
    from src.analytics import daily_report as dr
    from src.data.providers.toss.report_quotes import ReportQuotes
    authority, _, _ = approved(tmp_path, monkeypatch)
    cache = ReportQuotes(authority, enabled=lambda: True)
    quote, result, command = sample(authority)
    assert cache.publish(result, command=command, deadline=110)
    generator = dr.DailyReportGenerator.__new__(dr.DailyReportGenerator)
    generator.toss_quote_provider = cache.get_quote
    async def forbidden(*args, **kwargs):
        pytest.fail('fresh Toss quote must skip fallback requests')
    generator._bot = SimpleNamespace(broker=SimpleNamespace(get_quote=forbidden))
    monkeypatch.setattr(asyncio, 'to_thread', forbidden)
    rec = dr.RecommendedStock(symbol='005930', name='합성', rank=1, investment_thesis='', catalyst='', reference_price=100)
    generator._today_recommendations = [rec]
    asyncio.run(generator._update_results())
    assert rec.result_price == 101
    assert rec.result_source == 'Toss 현재가 (시장 기준 미확인)'
    assert rec.result_as_of == quote.observed_at.isoformat()
    assert rec.result_fetched_at == quote.fetched_at.isoformat()
    assert rec.result_market_basis == 'unknown'
    assert rec.result_pct is None
    text = generator._format_evening_report([rec], authority.now().date())
    assert "101원" in text and "가격 미확인 0" in text and "시장 기준 비교 불가" in text


def test_real_worker_to_supervisor_to_report_and_stop(tmp_path, monkeypatch):
    """합성 HTTP 외에는 실제 승인/worker/parser/원장/수신/소비 경로를 실행한다."""
    import json
    from src.analytics import daily_report as dr
    from src.schedulers import toss_shadow as shadow
    from src.data.providers.toss import oauth, transport
    from tests.test_toss_http_body import Response
    from src.utils import loop_heartbeat as hb
    for name in ('_beats', '_states', '_observers', '_observer_status'):
        monkeypatch.setattr(hb, name, {})
    monkeypatch.setenv('TOSS_API', '1')
    authority, stamp, _ = approved(tmp_path, monkeypatch)
    requests = []
    monkeypatch.setattr(oauth, 'environment_credentials',
                        lambda: oauth.Credentials('synthetic-client', 'synthetic-secret'))
    class Session:
        def __init__(self, **kwargs): self._retry_connection = True
        def request(self, method, url, **kwargs):
            requests.append((method, url))
            body = (dict(access_token='synthetic-token', token_type='Bearer', expires_in=86400)
                    if method == 'POST' else {'result': [dict(symbol='005930', lastPrice='101',
                        currency='KRW', timestamp=stamp[0].isoformat())]})
            return Response(json.dumps(body).encode())
        async def close(self): pass
    monkeypatch.setattr(transport.aiohttp, 'ClientSession', Session)
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None): return stamp[0].astimezone(tz)
    monkeypatch.setattr(shadow, 'datetime', Clock)
    slot = stamp[0].astimezone(timezone(timedelta(hours=9))).isoformat()
    monkeypatch.setattr(shadow, 'due_slots', lambda *_: [shadow.DueSlot('prices', slot, False, '2026-09-16')])
    bot = SimpleNamespace(running=True, engine=SimpleNamespace(portfolio=SimpleNamespace(
        positions={'005930': SimpleNamespace(current_price=Decimal('100'))})))
    deployment = SimpleNamespace(preflight_timeout_seconds=2, report_display=True, load=lambda: authority)
    supervisor = shadow.TossShadowSupervisor(bot, deployment)
    bot._toss_shadow_supervisor = supervisor
    generator = dr.DailyReportGenerator.__new__(dr.DailyReportGenerator)
    # 실제 scheduler의 동적 주입 계약: observer 초기화보다 보고 객체 생성이 빨라도 안전하다.
    generator.toss_quote_provider = lambda symbol: getattr(getattr(bot, '_toss_shadow_supervisor', None),
                                                          'get_report_quote', lambda _: None)(symbol)
    assert generator.toss_quote_provider('005930') is None
    rec = dr.RecommendedStock(symbol='005930', name='합성', rank=1, investment_thesis='', catalyst='', reference_price=100)
    generator._today_recommendations = [rec]
    async def forbidden(*args, **kwargs): pytest.fail('fallback must not be called')
    generator._bot = SimpleNamespace(broker=SimpleNamespace(get_quote=forbidden))
    consumed = []
    async def consume():
        await generator._update_results()
        consumed.append(rec.result_price)
        bot.running = False
    def observed(*args, **kwargs):
        asyncio.create_task(consume())
    monkeypatch.setattr(shadow, 'update_observation_health', observed)
    asyncio.run(asyncio.wait_for(supervisor.run(), timeout=5))
    assert consumed == [101]
    assert [method for method, _ in requests] == ['POST', 'GET']
    assert rec.result_source == 'Toss 현재가 (시장 기준 미확인)'
    assert rec.result_pct is None
    assert supervisor.get_report_quote('005930') is None
    assert supervisor.state == 'closed'


def test_monotonic_age_prevents_wall_clock_rollback_refresh(tmp_path, monkeypatch):
    from src.data.providers.toss.report_quotes import ReportQuotes
    authority, stamp, ticks = approved(tmp_path, monkeypatch)
    cache = ReportQuotes(authority, enabled=lambda: True)
    _, result, command = sample(authority)
    cache.publish(result, command=command, deadline=110)
    ticks[0] += 61
    # 원천시각 이후에서 벽시계가 멈추거나 뒤로 이동해도 경과 TTL은 유지한다.
    stamp[0] += timedelta(seconds=1)
    assert cache.get_quote('005930') is None


def test_ledger_failure_never_returns_handoff_quotes(tmp_path, monkeypatch):
    from src.data.providers.toss.observation_ledger import LedgerError
    from tests.test_toss_observation import setup_runner
    runner, ledger, snapshot = setup_runner(tmp_path)
    original = ledger.finish
    def failed(*args, **kwargs):
        original(*args, **kwargs)
        raise LedgerError()
    monkeypatch.setattr(ledger, 'finish', failed)
    result = asyncio.run(runner.prices(slot_id='slot', snapshot=snapshot))
    assert result.ledger_complete is False
    assert result.quotes == ()
    ledger.close()


def test_new_plan_without_display_capability_stays_unavailable(tmp_path, monkeypatch):
    from src.data.providers.toss.report_quotes import ReportQuotes
    authority, _, _ = approved(tmp_path, monkeypatch, report_capability=False)
    with pytest.raises(ApprovalError):
        ReportQuotes(authority, enabled=lambda: True)


def test_legacy_plan_cannot_gain_display_by_extra_field():
    p = plan_document()
    p['report_display'] = dict(purpose='daily_report_current_price', max_age_seconds=60)
    with pytest.raises(ApprovalError):
        ObservationPlan.from_bytes(raw(p))


@pytest.mark.parametrize('version,opt_in,allowed', [(1, False, True), (1, True, False),
    (3, False, True), (3, True, True), (3, '1', False)])
def test_deployment_requires_explicit_new_purpose_for_opt_in(tmp_path, monkeypatch, version, opt_in, allowed):
    from src.data.providers.toss import approval, runtime_factory
    authority, _, _ = approved(tmp_path, monkeypatch, version=version)
    monkeypatch.setattr(runtime_factory, 'validate_attestation', lambda *a, **kw: None)
    monkeypatch.setattr(approval, 'load_authority', lambda **kw: authority)
    deployment = runtime_factory.Deployment(tmp_path / 'registry', tmp_path / 'plan', 'synthetic',
        object(), object(), 1, report_display=opt_in)
    if allowed:
        assert deployment.load() is authority
    else:
        with pytest.raises(ApprovalError):
            deployment.load()


def test_stale_provider_preserves_existing_kis_fallback(tmp_path, monkeypatch):
    from src.analytics import daily_report as dr
    from src.data.providers.toss.report_quotes import ReportQuotes
    authority, stamp, _ = approved(tmp_path, monkeypatch)
    cache = ReportQuotes(authority, enabled=lambda: True)
    _, result, command = sample(authority)
    cache.publish(result, command=command, deadline=110)
    stamp[0] += timedelta(seconds=61)
    generator = dr.DailyReportGenerator.__new__(dr.DailyReportGenerator)
    generator.toss_quote_provider = cache.get_quote
    calls = []
    async def kis(symbol):
        calls.append(symbol)
        return dict(price=102, as_of='synthetic-kis-time')
    generator._bot = SimpleNamespace(broker=SimpleNamespace(get_quote=kis))
    rec = dr.RecommendedStock(rank=1, symbol='005930', name='합성', investment_thesis='', catalyst='', reference_price=100)
    generator._today_recommendations = [rec]
    asyncio.run(generator._update_results())
    assert calls == ['005930'] and rec.result_source == 'KIS 현재가'
    assert rec.result_price == 102 and rec.result_pct == 2
    assert rec.result_market_basis == rec.result_fetched_at == ''


def test_supervisor_preserves_initial_slot_deadline_when_wall_clock_stalls(tmp_path, monkeypatch):
    """슬롯 종료 1초 전 제출 → 2초 후 응답은 벽시계가 같아도 게시 금지."""
    import concurrent.futures
    from src.schedulers import toss_shadow as shadow
    from src.utils import loop_heartbeat as hb
    for name in ('_beats', '_states', '_observers', '_observer_status'):
        monkeypatch.setattr(hb, name, {})
    monkeypatch.setenv('TOSS_API', '1')
    authority, stamp, ticks = approved(tmp_path, monkeypatch)
    stamp[0] += timedelta(minutes=4, seconds=59)
    _, result, _ = sample(authority)
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None): return stamp[0].astimezone(tz)
    monkeypatch.setattr(shadow, 'datetime', Clock)
    monkeypatch.setattr(shadow, 'due_slots', lambda *_: [
        shadow.DueSlot('prices', '2026-09-16T10:00:00+09:00', False, '2026-09-16')])
    class Worker:
        def __init__(self, *args, **kwargs): pass
        async def start(self, **kwargs): pass
        def submit(self, command):
            ticks[0] += 2
            future = concurrent.futures.Future()
            future.set_result(result)
            return future
        async def stop(self, **kwargs): return 'closed'
    monkeypatch.setattr(shadow, 'TossWorker', Worker)
    bot = SimpleNamespace(running=True, engine=SimpleNamespace(portfolio=SimpleNamespace(
        positions={'005930': SimpleNamespace(current_price=100)})))
    supervisor = shadow.TossShadowSupervisor(bot, SimpleNamespace(
        preflight_timeout_seconds=2, report_display=True, load=lambda: authority))
    read = []
    def observed(*args, **kwargs):
        read.append(supervisor.get_report_quote('005930'))
        bot.running = False
    monkeypatch.setattr(shadow, 'update_observation_health', observed)
    asyncio.run(asyncio.wait_for(supervisor.run(), timeout=5))
    assert read == [None]
