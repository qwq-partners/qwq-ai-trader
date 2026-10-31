"""Durable report analysis tests use only synthetic providers and temporary DBs."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import hashlib
import json
import sys
from threading import Event
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from zoneinfo import ZoneInfo

import pytest

from src.analytics.audience_reports import MarketSnapshot, render_market_report
from src.analytics.report_delivery import ReportStore
from src.schedulers.report_scheduler import ReportSchedule
from src.utils.telegram import TelegramNotifier

DAY = '2026-10-27'
NOW = datetime(2026, 10, 27, 7, 40, tzinfo=ZoneInfo('Asia/Seoul'))
MARKET = MarketSnapshot(sectors=({'name': '반도체', 'r1': 1.5, 'as_of': '2026-10-26', 'source': 'etf'},), kr_basis_date='2026-10-26')


@pytest.fixture
def content(monkeypatch):
    # Content's independent worker owns its schema. This narrow stub verifies
    # orchestration without duplicating that implementation or touching providers.
    def compose(source, response, **kw):
        if response != 'valid': raise ValueError('synthetic invalid content')
        return source.replace('시장 판단', '시장 판단'), {'synthetic': True}
    module = SimpleNamespace(PROMPT_VERSION='test-v1',
        build_analysis_prompt=Mock(side_effect=lambda source, **kw: (source, 'public-only')),
        compose_analysis=Mock(side_effect=compose),
        factual_fallback=Mock(side_effect=lambda source: source.replace('시장 판단', '시장 판단') + '\n분석 미확보'))
    monkeypatch.setitem(sys.modules, 'src.analytics.market_report_analysis', module)
    return module


def response(**changes):
    return SimpleNamespace(**dict(dict(content='valid', success=True, requested_model='gpt-5-mini',
        resolved_model='provider-version', usage_known=True, finish_reason='stop',
        request_sent=True, refusal_detected=False, budget_reservation_token='b'*32,
        input_tokens=120, output_tokens=50, billing_status='reported_usage',
        budget_block_reason=None, attempt_trail=[{'task':'market_brief', 'role':'primary',
        'requested_model':'gpt-5-mini', 'usage_known':True, 'error':'SECRET_CANARY',
        'raw':'SECRET_CANARY'}]), **changes))


def notifier():
    n = TelegramNotifier(bot_token='fake', chat_id='dm', alert_chat_id='dm', report_chat_id='channel')
    n._send_report_once = AsyncMock(return_value='accepted')
    return n


def test_analysis_claim_is_atomic_and_original_is_immutable(tmp_path):
    path = tmp_path/'r.db'
    def claim(i):
        return ReportStore(path).claim_analysis('market', 'morning', DAY, str(i), 'source'+str(i), 'v1', owner=str(i))
    with ThreadPoolExecutor(max_workers=4) as pool:
        assert sum(pool.map(claim, range(8))) == 1
    store = ReportStore(path)
    row = store.analysis_receipt('market', 'morning', DAY)
    assert row['status'] == 'attempted'
    assert row['owner'] == row['source_snapshot_id']
    assert row['source_sha256'] == hashlib.sha256(row['source_body'].encode()).hexdigest()
    assert not claim(99)
    assert store.analysis_receipt('market', 'morning', DAY) == row


@pytest.mark.asyncio
async def test_concurrent_schedule_does_not_send_fallback_or_start_second_call(tmp_path, content):
    store = ReportStore(tmp_path/'r.db')
    entered, release = asyncio.Event(), asyncio.Event()
    async def complete(*args, **kwargs):
        assert store.analysis_receipt('market', 'morning', DAY)['status'] == 'attempted'
        entered.set()
        await release.wait()
        return response()
    manager = SimpleNamespace(complete_role=AsyncMock(side_effect=complete))
    n = notifier()
    a = ReportSchedule(store, n, analysis_manager=manager)
    b = ReportSchedule(ReportStore(store.path), n, analysis_manager=manager)
    task = asyncio.create_task(a.tick(NOW, True, MARKET, 'PRIVATE_CANARY'))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        assert await b.tick(NOW, True, MARKET, 'PRIVATE_CANARY') == 'analysis_collecting'
        n._send_report_once.assert_not_awaited()
    finally:
        release.set()
    assert await task == 'accepted'
    manager.complete_role.assert_awaited_once()
    assert manager.complete_role.call_args.kwargs['role'] == 'market_brief'
    assert 'PRIVATE_CANARY' not in str(manager.complete_role.call_args)
    row = store.analysis_receipt('market', 'morning', DAY)
    assert row['status'] == 'ready' and row['result'] == {'synthetic':True}
    assert 'SECRET_CANARY' not in json.dumps(row)
    assert await b.tick(NOW, True, MARKET, '') == 'already_consumed'


@pytest.mark.asyncio
async def test_abandoned_analysis_never_calls_manager_and_preserves_source(tmp_path, content):
    store = ReportStore(tmp_path/'r.db')
    source = render_market_report('morning', MARKET, NOW)
    assert store.claim_analysis('market', 'morning', DAY, 'original', source, 'v1', owner='dead')
    manager = SimpleNamespace(complete_role=AsyncMock(return_value=response()))
    n = notifier()
    assert await ReportSchedule(store, n, analysis_manager=manager).tick(NOW.replace(minute=45), True, MarketSnapshot(), '') == 'accepted'
    manager.complete_role.assert_not_awaited()
    row = store.analysis_receipt('market', 'morning', DAY)
    assert row['status'] == 'abandoned' and row['source_body'] == source
    assert '07:40' in n._send_report_once.call_args.args[1]
    assert '분석 미확보' in n._send_report_once.call_args.args[1]


@pytest.mark.asyncio
async def test_ready_analysis_restart_reuses_exact_body_without_calling_or_collecting(tmp_path, content):
    store = ReportStore(tmp_path/'r.db')
    manager = SimpleNamespace(complete_role=AsyncMock(return_value=response()))
    n = notifier()
    n.publish_report = AsyncMock(side_effect=RuntimeError('before delivery claim'))
    with pytest.raises(RuntimeError):
        await ReportSchedule(store, n, analysis_manager=manager).tick(NOW, True, MARKET, '')
    saved = store.analysis_receipt('market', 'morning', DAY)
    assert saved['status'] == 'ready' and store.receipt('market', 'morning', DAY) is None
    n = notifier()
    fetch = AsyncMock()
    assert await ReportSchedule(store, n, analysis_manager=manager, index_fetcher=fetch).tick(NOW.replace(minute=46), True, MarketSnapshot(), '') == 'accepted'
    assert n._send_report_once.call_args.args[1] == saved['final_body']
    manager.complete_role.assert_awaited_once()
    fetch.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('case,expected', [('invalid','failed'), ('refusal','failed'), ('length','failed'), ('budget','failed'), ('timeout','unknown'), ('unavailable','failed')])
async def test_analysis_failures_are_terminal_and_never_retried(tmp_path, content, case, expected):
    updates = {'invalid': {'content':'bad'}, 'refusal': {'refusal_detected':True},
        'length': {'finish_reason':'length'}, 'budget': {'success':False,'request_sent':False,'budget_block_reason':'essential_budget_reserved'},
        'timeout': {'success':False,'usage_known':False}, 'unavailable': {}}[case]
    manager = None if case == 'unavailable' else SimpleNamespace(complete_role=AsyncMock(return_value=response(**updates)))
    store = ReportStore(tmp_path/'r.db')
    n = notifier()
    schedule = ReportSchedule(store, n, analysis_manager=manager)
    assert await schedule.tick(NOW, True, MARKET, '') == 'accepted'
    assert store.analysis_receipt('market', 'morning', DAY)['status'] == expected
    assert '분석 미확보' in n._send_report_once.call_args.args[1]
    assert await schedule.tick(NOW, True, MARKET, '') == 'already_consumed'
    if manager: manager.complete_role.assert_awaited_once()


@pytest.mark.asyncio
async def test_cancel_drains_manager_settlement_before_slot_unlock(tmp_path, content):
    entered, settling, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    async def complete(*args, **kwargs):
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            settling.set()
            await release.wait()
            raise
    manager = SimpleNamespace(complete_role=AsyncMock(side_effect=complete))
    store = ReportStore(tmp_path/'r.db')
    n = notifier()
    task = asyncio.create_task(ReportSchedule(store, n, analysis_manager=manager).tick(NOW, True, MARKET, ''))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        task.cancel()
        await asyncio.wait_for(settling.wait(), 2)
        task.cancel()  # repeated outer cancellation must not cancel settlement again
        contender = ReportSchedule(ReportStore(store.path), n, analysis_manager=manager)
        assert await contender.tick(NOW, True, MARKET, '') == 'analysis_collecting'
        n._send_report_once.assert_not_awaited()
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError): await task
    assert store.analysis_receipt('market', 'morning', DAY)['status'] == 'unknown'
    assert await contender.tick(NOW, True, MARKET, '') == 'accepted'
    manager.complete_role.assert_awaited_once()


@pytest.mark.asyncio
async def test_holiday_missing_basis_and_private_digest_do_not_resolve_manager(tmp_path, content):
    factory = Mock(side_effect=AssertionError('must not initialize'))
    n = notifier()
    schedule = ReportSchedule(ReportStore(tmp_path/'r.db'), n, analysis_manager_factory=factory)
    assert await schedule.tick(NOW, False, MARKET, '') == 'skipped_holiday'
    assert await schedule.tick(NOW.replace(hour=13, minute=10), True, MarketSnapshot(), '') == 'skipped_missing_current_market_basis'
    assert await schedule.tick(NOW.replace(hour=21, minute=0), True, MARKET, 'PRIVATE') == 'accepted'
    factory.assert_not_called()


@pytest.mark.asyncio
async def test_manager_factory_resolves_only_after_durable_claim(tmp_path, content):
    store = ReportStore(tmp_path/'r.db')
    manager = SimpleNamespace(complete_role=AsyncMock(return_value=response()))
    def factory():
        assert store.analysis_receipt('market', 'morning', DAY)['status'] == 'attempted'
        return manager
    getter = Mock(side_effect=factory)
    assert await ReportSchedule(store, notifier(), analysis_manager_factory=getter).tick(NOW, True, MARKET, '') == 'accepted'
    getter.assert_called_once()
    manager.complete_role.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize('stage', ['claim_analysis', 'complete_analysis'])
async def test_cancelled_sqlite_writer_drains_under_slot_lock(tmp_path, content, monkeypatch, stage):
    entered, release = Event(), Event()
    store = ReportStore(tmp_path/'r.db')
    original = getattr(store, stage)
    def paused(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return original(*args, **kwargs)
    monkeypatch.setattr(store, stage, paused)
    manager = SimpleNamespace(complete_role=AsyncMock(return_value=response()))
    n = notifier()
    task = asyncio.create_task(ReportSchedule(store, n, analysis_manager=manager).tick(NOW, True, MARKET, ''))
    contender = ReportSchedule(ReportStore(store.path), n, analysis_manager=manager)
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        task.cancel()
        await asyncio.sleep(0)
        assert await contender.tick(NOW, True, MARKET, '') == 'analysis_collecting'
        task.cancel()
        n._send_report_once.assert_not_awaited()
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError): await task
    before = manager.complete_role.await_count
    assert await contender.tick(NOW, True, MARKET, '') == 'accepted'
    assert manager.complete_role.await_count == before == (0 if stage == 'claim_analysis' else 1)
    assert store.analysis_receipt('market', 'morning', DAY)['status'] == ('abandoned' if stage == 'claim_analysis' else 'ready')


def test_only_claim_owner_can_complete_and_ready_is_immutable(tmp_path):
    store = ReportStore(tmp_path/'r.db')
    assert store.claim_analysis('market', 'morning', DAY, 's', 'original', 'v1', owner='owner')
    data = dict(status='ready', final_body='final', result={'checked':True}, metadata={'usage_known':True})
    assert not store.complete_analysis('market', 'morning', DAY, owner='other', **data)
    assert store.analysis_receipt('market', 'morning', DAY)['status'] == 'attempted'
    assert store.complete_analysis('market', 'morning', DAY, owner='owner', **data)
    assert not store.complete_analysis('market', 'morning', DAY, owner='owner', status='failed', final_body='replacement')
    r = store.analysis_receipt('market', 'morning', DAY)
    assert r['final_body'] == 'final' and r['source_body'] == 'original'
    assert r['result'] == {'checked':True} and r['metadata'] == {'usage_known':True}


@pytest.mark.asyncio
async def test_after_inputs_and_saved_analysis_survive_restart_without_second_fetch(tmp_path, content):
    now = NOW.replace(hour=16, minute=40)
    row = dict(code='0001', label='KOSPI', price=5638.35, source='KIS/FHKUP03500200',
               source_observed_at=DAY+'T15:32:00+09:00', received_at=now.isoformat(),
               observation_kind='historical_minute', finality='unverified')
    store = ReportStore(tmp_path/'r.db')
    fetch = AsyncMock(return_value=(row,))
    manager = SimpleNamespace(complete_role=AsyncMock(return_value=response()))
    n = notifier()
    n.publish_report = AsyncMock(side_effect=RuntimeError('before delivery'))
    with pytest.raises(RuntimeError):
        await ReportSchedule(store, n, index_fetcher=fetch, clock=lambda:now,
                             analysis_manager=manager).tick(now, True, MarketSnapshot(kr_basis_date=DAY), '')
    original = store.analysis_receipt('market', 'after', DAY)
    n = notifier()
    assert await ReportSchedule(store, n, index_fetcher=fetch, analysis_manager=manager).tick(
        now.replace(minute=46), True, MarketSnapshot(), '') == 'accepted'
    fetch.assert_awaited_once_with(now.date())
    manager.complete_role.assert_awaited_once()
    assert n._send_report_once.call_args.args[1] == original['final_body']
    assert '5,638.35' in original['source_body']
    with store.input_guard(DAY) as first:
        assert first
        with store.slot_guard('market', 'after', DAY) as second: assert not second


@pytest.mark.asyncio
async def test_unknown_cost_log_continue_does_not_reopen_analysis(tmp_path, content, monkeypatch):
    import time
    from src.utils.llm import LLMConfig, LLMManager
    from tests.test_llm_transport_contract import Session, Reply
    m = LLMManager(LLMConfig(openai_api_key='synthetic', monthly_budget_usd=100,
        budget_state_path=str(tmp_path/'budget.db'), budget_policy_enabled=True,
        missing_cost_policy='log_and_continue'), audit_dir=tmp_path/'audit')
    reserve = m.monthly_budget.reserve
    def slow_reserve(*args, **kwargs):
        result = reserve(*args, **kwargs)
        time.sleep(0.08)
        return result
    monkeypatch.setattr(m.monthly_budget, 'reserve', slow_reserve)
    complete = AsyncMock(wraps=m.complete_role)
    monkeypatch.setattr(m, 'complete_role', complete)
    class Waiting(Reply):
        async def json(self):
            assert len(session.payloads) == 1
            raise asyncio.TimeoutError('synthetic response-body timeout')
    session = Session(Waiting({}))
    m.openai._session = session
    store = ReportStore(tmp_path/'r.db')
    n = notifier()
    n.publish_report = AsyncMock(side_effect=RuntimeError('before delivery'))
    with pytest.raises(RuntimeError):
        await ReportSchedule(store, n, analysis_manager=m).tick(NOW, True, MARKET, '')
    r = store.analysis_receipt('market', 'morning', DAY)
    assert r['status'] == 'unknown' and r['metadata']['request_sent'] is True
    assert r['metadata']['usage_known'] is False
    assert r['metadata']['billing_status'] == 'unknown'
    assert m.monthly_budget.status()['unresolved_count'] == 1
    assert m._check_budget()  # Existing policy permits other independent calls.
    assert await ReportSchedule(store, notifier(), analysis_manager=m).tick(NOW, True, MARKET, '') == 'accepted'
    assert len(session.payloads) == 1
    complete.assert_awaited_once()
    assert store.analysis_receipt('market', 'morning', DAY) == r
    m.openai._session = None
    await m.close()


@pytest.mark.asyncio
async def test_cancelled_input_reader_drains_before_after_guard_unlock(tmp_path, content, monkeypatch):
    entered, release = Event(), Event()
    store = ReportStore(tmp_path/'r.db')
    original = store.load_inputs
    def paused(day):
        entered.set()
        assert release.wait(5)
        return original(day)
    monkeypatch.setattr(store, 'load_inputs', paused)
    now = NOW.replace(hour=16, minute=40)
    task = asyncio.create_task(ReportSchedule(store, notifier()).tick(now, True, MarketSnapshot(), ''))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        task.cancel()
        await asyncio.sleep(0)
        with ReportStore(store.path).input_guard(DAY) as acquired: assert not acquired
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError): await task


@pytest.mark.asyncio
async def test_exception_attached_settlement_metadata_survives_without_error_text(tmp_path, content):
    error = RuntimeError('SECRET_CANARY')
    error._llm_response = response(success=False, usage_known=False)
    manager = SimpleNamespace(complete_role=AsyncMock(side_effect=error))
    store = ReportStore(tmp_path/'r.db')
    assert await ReportSchedule(store, notifier(), analysis_manager=manager).tick(NOW, True, MARKET, '') == 'accepted'
    record = store.analysis_receipt('market', 'morning', DAY)
    assert record['status'] == 'unknown'
    assert record['metadata']['budget_reservation_token'] == 'b'*32
    assert record['metadata']['usage_known'] is False
    assert 'SECRET_CANARY' not in json.dumps(record)
