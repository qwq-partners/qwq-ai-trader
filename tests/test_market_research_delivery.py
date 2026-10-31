"""Public research is frozen with the paid attempt, never recollected on reuse."""
import asyncio
from copy import deepcopy
import hashlib
import json
import sqlite3
import sys
from types import SimpleNamespace
from threading import Event
from unittest.mock import AsyncMock, Mock

import pytest

from src.analytics.report_delivery import ReportStore
from src.analytics.market_report_html import with_web_link
from src.schedulers.report_scheduler import ReportSchedule
from tests.test_market_report_runner import DAY, NOW, MARKET, content, notifier, response


def bundle():
    return {'version': 'public-research-v1', 'captured_at': NOW.isoformat(),
            'coverage': [], 'records': [{'id': 'E1', 'kind': 'news',
            'title': '합성 공개 사건', 'excerpt': '공개 자료', 'source': 'synthetic',
            'url': 'https://example.com/public', 'published_at': None,
            'publication_status': 'unverified', 'collected_at': NOW.isoformat(),
            'observed_at': None, 'event_date': None, 'content_kind': 'article_excerpt',
            'limitations': []}]}


@pytest.fixture
def sources(monkeypatch):
    # The source worker owns extraction/validation. This stub is only a
    # deterministic boundary for storage and cancellation fault injection.
    module = SimpleNamespace(normalize_public_research=Mock(side_effect=deepcopy),
        empty_public_research=lambda now, status: {
            'version': 'public-research-v1', 'captured_at': now.isoformat(),
            'coverage': [{'source': 'research', 'status': status, 'detail': ''}], 'records': []})
    monkeypatch.setitem(sys.modules, 'src.analytics.market_research_sources', module)
    return module


def test_research_claim_freezes_exact_json_hash_and_rolls_back_together(tmp_path):
    store = ReportStore(tmp_path/'report.db')
    data = bundle()
    assert store.claim_analysis('market', 'morning', DAY, 'original', 'facts', 'v2',
                                owner='owner', public_evidence=data)
    data['records'][0]['title'] = 'mutated'
    row = store.analysis_receipt('market', 'morning', DAY)
    assert row['public_evidence'] == bundle()
    encoded = json.dumps(bundle(), ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
    assert row['evidence_sha256'] == hashlib.sha256(encoded.encode()).hexdigest()
    with sqlite3.connect(store.path) as db:
        db.execute("CREATE TRIGGER reject_research BEFORE INSERT ON report_analysis_sources BEGIN SELECT RAISE(ABORT, 'synthetic'); END")
    with pytest.raises(sqlite3.IntegrityError):
        store.claim_analysis('market', 'midday', DAY, 'other', 'facts', 'v2',
                             owner='owner', public_evidence=bundle())
    assert store.analysis_receipt('market', 'midday', DAY) is None


@pytest.mark.asyncio
async def test_factory_runs_under_guard_after_source_checks_and_exact_bundle_is_frozen(tmp_path, content, sources):
    store = ReportStore(tmp_path/'report.db')
    data = bundle()
    async def capture(*, now):
        assert now == NOW
        with ReportStore(store.path).slot_guard('market', 'morning', DAY) as acquired:
            assert not acquired
        return data
    factory = AsyncMock(side_effect=capture)
    async def complete(*a, **kw):
        saved = store.analysis_receipt('market', 'morning', DAY)
        assert saved['public_evidence'] == bundle() and saved['status'] == 'attempted'
        data['records'].clear()
        assert 'PRIVATE_CANARY' not in str((a, kw))
        return response()
    manager = SimpleNamespace(complete_role=AsyncMock(side_effect=complete))
    schedule = ReportSchedule(store, notifier(), analysis_manager=manager, public_evidence_factory=factory)
    assert await schedule.tick(NOW, True, MARKET, 'PRIVATE_CANARY') == 'accepted'
    assert content.build_analysis_prompt.call_args.kwargs['public_evidence'] == bundle()
    assert content.compose_analysis.call_args.kwargs['public_evidence'] == bundle()
    assert await schedule.tick(NOW, True, MARKET, '') == 'already_consumed'
    factory.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize('state', ['attempted', 'ready'])
async def test_saved_research_never_reads_new_sources(tmp_path, content, sources, state):
    from src.analytics.audience_reports import render_market_report, MarketSnapshot
    store = ReportStore(tmp_path/'report.db')
    source = render_market_report('morning', MARKET, NOW)
    store.claim_analysis('market', 'morning', DAY, 'original', source, 'v2', owner='old', public_evidence=bundle())
    if state == 'ready':
        store.complete_analysis('market', 'morning', DAY, owner='old', status='ready', final_body=source, result={})
    factory = AsyncMock(side_effect=AssertionError('must never recollect'))
    manager = SimpleNamespace(complete_role=AsyncMock())
    n = notifier()
    assert await ReportSchedule(store, n, analysis_manager=manager, public_evidence_factory=factory).tick(
        NOW.replace(minute=45), True, MarketSnapshot(), '') == 'accepted'
    row = store.analysis_receipt('market', 'morning', DAY)
    assert row['public_evidence'] == bundle() and row['source_snapshot_id'] == 'original'
    assert row['status'] == ('abandoned' if state == 'attempted' else 'ready')
    factory.assert_not_awaited()
    manager.complete_role.assert_not_awaited()


@pytest.mark.asyncio
async def test_bad_source_holiday_private_and_missing_market_never_read_research(tmp_path, content, sources, monkeypatch):
    from src.analytics.audience_reports import MarketSnapshot
    factory = AsyncMock(side_effect=AssertionError('not eligible'))
    store = ReportStore(tmp_path/'report.db')
    schedule = ReportSchedule(store, notifier(), public_evidence_factory=factory)
    assert await schedule.tick(NOW, False, MARKET, '') == 'skipped_holiday'
    assert await schedule.tick(NOW.replace(hour=13, minute=10), True, MarketSnapshot(), '') == 'skipped_missing_current_market_basis'
    assert await schedule.tick(NOW.replace(hour=21, minute=0), True, MARKET, '') == 'accepted'
    monkeypatch.setattr('src.schedulers.report_scheduler.render_market_report', lambda *a: 'x'*1801)
    assert await schedule.tick(NOW.replace(day=28), True, MARKET, '') == 'failed_validation'
    factory.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancelled_capture_leaves_no_paid_claim_and_keeps_guard_until_cleanup(tmp_path, content, sources):
    entered, settling, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    async def capture(**kw):
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            settling.set()
            await release.wait()
            raise
    store = ReportStore(tmp_path/'report.db')
    manager = SimpleNamespace(complete_role=AsyncMock())
    n = notifier()
    schedule = ReportSchedule(store, n, analysis_manager=manager, public_evidence_factory=capture)
    task = asyncio.create_task(schedule.tick(NOW, True, MARKET, ''))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        task.cancel()
        await asyncio.wait_for(settling.wait(), 2)
        task.cancel()
        assert await schedule.tick(NOW, True, MARKET, '') == 'analysis_collecting'
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError): await task
    assert store.analysis_receipt('market', 'morning', DAY) is None
    manager.complete_role.assert_not_awaited()
    n._send_report_once.assert_not_awaited()


@pytest.mark.asyncio
async def test_concurrent_capture_and_analysis_are_each_once(tmp_path, content, sources):
    entered, release = asyncio.Event(), asyncio.Event()
    async def capture(**kw):
        entered.set()
        await release.wait()
        return bundle()
    factory = AsyncMock(side_effect=capture)
    store = ReportStore(tmp_path/'r.db')
    manager = SimpleNamespace(complete_role=AsyncMock(return_value=response()))
    n = notifier()
    schedule = ReportSchedule(store, n, analysis_manager=manager, public_evidence_factory=factory)
    task = asyncio.create_task(schedule.tick(NOW, True, MARKET, ''))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        assert await schedule.tick(NOW, True, MARKET, '') == 'analysis_collecting'
        n._send_report_once.assert_not_awaited()
    finally:
        release.set()
    assert await task == 'accepted'
    factory.assert_awaited_once()
    manager.complete_role.assert_awaited_once()


@pytest.mark.asyncio
async def test_cancelled_atomic_claim_keeps_frozen_research_for_orphan(tmp_path, content, sources, monkeypatch):
    entered, release = Event(), Event()
    store = ReportStore(tmp_path/'r.db')
    original = store.claim_analysis
    def delayed(*a, **kw):
        entered.set()
        assert release.wait(5)
        return original(*a, **kw)
    monkeypatch.setattr(store, 'claim_analysis', delayed)
    manager = SimpleNamespace(complete_role=AsyncMock())
    factory = AsyncMock(return_value=bundle())
    schedule = ReportSchedule(store, notifier(), analysis_manager=manager, public_evidence_factory=factory)
    task = asyncio.create_task(schedule.tick(NOW, True, MARKET, ''))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        task.cancel()
        await asyncio.sleep(0)
        with ReportStore(store.path).slot_guard('market', 'morning', DAY) as acquired: assert not acquired
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError): await task
    assert store.analysis_receipt('market', 'morning', DAY)['public_evidence'] == bundle()
    assert await schedule.tick(NOW, True, MARKET, '') == 'accepted'
    assert store.analysis_receipt('market', 'morning', DAY)['status'] == 'abandoned'
    factory.assert_awaited_once()
    manager.complete_role.assert_not_awaited()


@pytest.mark.asyncio
async def test_broken_normalizer_is_optional_and_exception_text_is_not_persisted(tmp_path, content, sources):
    sources.normalize_public_research.side_effect = ValueError('PRIVATE_CANARY')
    store = ReportStore(tmp_path/'r.db')
    manager = SimpleNamespace(complete_role=AsyncMock())
    assert await ReportSchedule(store, notifier(), analysis_manager=manager,
        public_evidence_factory=AsyncMock(return_value=bundle())).tick(NOW, True, MARKET, '') == 'accepted'
    row = store.analysis_receipt('market', 'morning', DAY)
    assert row['metadata']['reason'] == 'research_capture_failed'
    assert 'PRIVATE_CANARY' not in json.dumps(row)
    manager.complete_role.assert_not_awaited()


@pytest.mark.asyncio
async def test_optional_capture_failure_preserves_fact_delivery(tmp_path, content, sources):
    store = ReportStore(tmp_path/'report.db')
    n = notifier()
    factory = AsyncMock(side_effect=ValueError('SECRET_CANARY'))
    assert await ReportSchedule(store, n, public_evidence_factory=factory).tick(NOW, True, MARKET, '') == 'accepted'
    row = store.analysis_receipt('market', 'morning', DAY)
    assert row['public_evidence']['records'] == []
    assert 'SECRET_CANARY' not in json.dumps(row)
    assert '분석 미확보' in row['final_body']


@pytest.mark.asyncio
@pytest.mark.parametrize('case', ['empty', 'macro_only', 'future_capture', 'invalid_schema'])
async def test_unusable_optional_research_never_spends_a_model_call(tmp_path, content, sources, case):
    data = bundle()
    if case == 'empty': data['records'] = []
    if case == 'macro_only': data['records'][0]['kind'] = 'macro'
    if case == 'future_capture': data['captured_at'] = NOW.replace(minute=41).isoformat()
    if case == 'invalid_schema': data = {'unexpected': 'PRIVATE_CANARY'}
    store = ReportStore(tmp_path/'report.db')
    manager = SimpleNamespace(complete_role=AsyncMock())
    assert await ReportSchedule(store, notifier(), analysis_manager=manager,
        public_evidence_factory=AsyncMock(return_value=data)).tick(NOW, True, MARKET, '') == 'accepted'
    row = store.analysis_receipt('market', 'morning', DAY)
    reason = 'research_capture_failed' if case in {'future_capture', 'invalid_schema'} else 'research_evidence_missing'
    assert row['status'] == 'failed' and row['metadata']['reason'] == reason
    manager.complete_role.assert_not_awaited()
    assert 'PRIVATE_CANARY' not in json.dumps(row)


@pytest.mark.asyncio
async def test_real_normalizer_excludes_private_fields_before_prompt_and_storage(tmp_path, content):
    from src.analytics.market_research_sources import normalize_public_research
    data = bundle()
    data['account'] = 'PRIVATE_CANARY'
    data['records'][0]['holdings'] = 'PRIVATE_CANARY'
    expected = normalize_public_research(data)
    store = ReportStore(tmp_path/'report.db')
    manager = SimpleNamespace(complete_role=AsyncMock(return_value=response()))
    assert await ReportSchedule(store, notifier(), analysis_manager=manager,
        public_evidence_factory=AsyncMock(return_value=data)).tick(NOW, True, MARKET, 'PRIVATE_CANARY') == 'accepted'
    saved = store.analysis_receipt('market', 'morning', DAY)
    assert saved['public_evidence'] == expected
    assert content.build_analysis_prompt.call_args.kwargs['public_evidence'] == expected
    assert 'PRIVATE_CANARY' not in json.dumps(saved)


@pytest.mark.asyncio
async def test_research_identity_includes_bundle_and_ready_long_body_is_reused(tmp_path, content, sources):
    content.compose_analysis.side_effect = lambda *a, **kw: ('가'*2500, {'research': True})
    records = []
    for i in range(2):
        store = ReportStore(tmp_path/f'{i}.db')
        data = bundle()
        data['records'][0]['title'] += str(i)
        n = notifier()
        n.publish_report = AsyncMock(side_effect=RuntimeError('before delivery'))
        manager = SimpleNamespace(complete_role=AsyncMock(return_value=response()))
        with pytest.raises(RuntimeError):
            await ReportSchedule(store, n, analysis_manager=manager,
                public_evidence_factory=AsyncMock(return_value=data)).tick(NOW, True, MARKET, '')
        row = store.analysis_receipt('market', 'morning', DAY)
        records.append(row)
        assert row['status'] == 'ready' and len(row['final_body']) == 2500
        n = notifier()
        factory = AsyncMock(side_effect=AssertionError('no fresh sources'))
        assert await ReportSchedule(store, n, public_evidence_factory=factory).tick(NOW, True, MARKET, '') == 'accepted'
        assert n._send_report_once.call_args.args[1] == with_web_link(row['final_body'], DAY, 'morning')
        factory.assert_not_awaited()
    assert records[0]['source_sha256'] == records[1]['source_sha256']
    assert records[0]['source_snapshot_id'] != records[1]['source_snapshot_id']


@pytest.mark.asyncio
async def test_after_research_uses_post_fetch_cutoff_and_never_fetches_on_restart(tmp_path, content, sources):
    from src.analytics.audience_reports import MarketSnapshot
    now = NOW.replace(hour=16, minute=40)
    cutoff = now.replace(minute=41)
    observation = dict(code='0001', label='KOSPI', price=1234.5, source='KIS/FHKUP03500200',
        source_observed_at=DAY+'T15:32:00+09:00', received_at=now.isoformat(),
        observation_kind='historical_minute', finality='unverified')
    fetch = AsyncMock(return_value=(observation,))
    data = bundle()
    data['captured_at'] = cutoff.isoformat()
    factory = AsyncMock(return_value=data)
    store = ReportStore(tmp_path/'r.db')
    n = notifier()
    n.publish_report = AsyncMock(side_effect=RuntimeError('before transport'))
    manager = SimpleNamespace(complete_role=AsyncMock(return_value=response()))
    with pytest.raises(RuntimeError):
        await ReportSchedule(store, n, index_fetcher=fetch, clock=lambda: cutoff,
            analysis_manager=manager, public_evidence_factory=factory).tick(now, True, MarketSnapshot(kr_basis_date=DAY), '')
    factory.assert_awaited_once_with(now=cutoff)
    assert await ReportSchedule(store, notifier(), index_fetcher=fetch,
        public_evidence_factory=factory).tick(now.replace(minute=46), True, MarketSnapshot(), '') == 'accepted'
    fetch.assert_awaited_once_with(now.date())
    factory.assert_awaited_once()
    manager.complete_role.assert_awaited_once()


def test_legacy_analysis_receipt_has_no_research_and_remains_unchanged(tmp_path):
    store = ReportStore(tmp_path/'r.db')
    store.claim_analysis('market', 'morning', DAY, 'legacy', 'original', 'v1', owner='old')
    row = store.analysis_receipt('market', 'morning', DAY)
    assert row['public_evidence'] is None and row['evidence_sha256'] is None
    assert row['source_snapshot_id'] == 'legacy'


@pytest.mark.asyncio
async def test_research_hash_cannot_mask_missing_market_snapshot_identity(tmp_path, content, sources):
    from src.analytics.audience_reports import render_market_report
    from src.analytics.market_report_runner import prepare_market_report
    store = ReportStore(tmp_path/'r.db')
    with pytest.raises(ValueError):
        await prepare_market_report(store, kind='morning', day=DAY,
            source_body=render_market_report('morning', MARKET, NOW),
            public_evidence=bundle(), now=NOW)
    assert store.analysis_receipt('market', 'morning', DAY) is None


@pytest.mark.asyncio
@pytest.mark.parametrize('events', [True, False])
async def test_real_content_and_sources_produce_research_or_explicit_missing(tmp_path, monkeypatch, events):
    from tests.test_market_research_content import NOW as research_now, market, evidence, analysis
    data = evidence()
    if not events: data['records'] = []
    monkeypatch.setattr('src.schedulers.report_scheduler.render_market_report', lambda *a: market())
    manager = SimpleNamespace(complete_role=AsyncMock(return_value=response(
        content=json.dumps(analysis(), ensure_ascii=False))))
    store = ReportStore(tmp_path/'r.db')
    n = notifier()
    assert await ReportSchedule(store, n, analysis_manager=manager,
        public_evidence_factory=AsyncMock(return_value=data)).tick(research_now, True, MARKET, '') == 'accepted'
    row = store.analysis_receipt('market', 'morning', research_now.date().isoformat())
    assert row['status'] == ('ready' if events else 'failed')
    assert ('핵심 이슈와 영향' if events else '시장 지표 요약') in row['final_body']
    assert n._send_report_once.call_args.args[1] == with_web_link(row['final_body'], research_now.date().isoformat(), 'morning')
    assert manager.complete_role.await_count == (2 if events else 0)   # Telegram brief + web report; neither when no events


@pytest.mark.asyncio
@pytest.mark.parametrize('body,accepted', [('가'*2500, True), ('가\n'*59+'나', True), ('가'*3001, False), ('가\n'*60+'나', False), ('😀'*2200, False)], ids=['long-valid', 'lines-valid', 'too-long', 'too-many-lines', 'utf16'])
async def test_research_body_bounds_and_telegram_utf16(tmp_path, body, accepted):
    store = ReportStore(tmp_path/'report.db')
    n = notifier()
    result = await n.publish_report('market', 'morning', DAY, 'source', body, store=store)
    assert result == ('accepted' if accepted else 'failed_validation')
    assert n._send_report_once.await_count == int(accepted)


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['timeout', 'internal_cancel'])
async def test_capture_deadline_and_internal_cancel_preserve_facts(tmp_path, content, sources, monkeypatch, failure):
    from src.analytics import market_report_runner as runner
    monkeypatch.setattr(runner, 'CAPTURE_TIMEOUT_SECONDS', .02, raising=False)
    cleaned = asyncio.Event()
    async def capture(**kw):
        try:
            if failure == 'internal_cancel': raise asyncio.CancelledError
            await asyncio.Event().wait()
        finally:
            cleaned.set()
    store = ReportStore(tmp_path/'r.db')
    manager = SimpleNamespace(complete_role=AsyncMock())
    n = notifier()
    schedule = ReportSchedule(store, n, analysis_manager=manager, public_evidence_factory=capture)
    assert await asyncio.wait_for(schedule.tick(NOW, True, MARKET, ''), 2) == 'accepted'
    assert cleaned.is_set()
    row = store.analysis_receipt('market', 'morning', DAY)
    assert row['status'] == 'failed' and row['metadata']['reason'] == 'research_capture_failed'
    assert row['public_evidence']['records'] == []
    assert '분석 미확보' in row['final_body']
    manager.complete_role.assert_not_awaited()
    n._send_report_once.assert_awaited_once()


@pytest.mark.asyncio
async def test_external_cancel_during_timeout_cleanup_propagates_after_drain(tmp_path, content, sources, monkeypatch):
    from src.analytics import market_report_runner as runner
    monkeypatch.setattr(runner, 'CAPTURE_TIMEOUT_SECONDS', .02, raising=False)
    draining, release = asyncio.Event(), asyncio.Event()
    async def capture(**kw):
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            draining.set()
            await release.wait()
            raise
    store = ReportStore(tmp_path/'r.db')
    manager = SimpleNamespace(complete_role=AsyncMock())
    n = notifier()
    schedule = ReportSchedule(store, n, analysis_manager=manager, public_evidence_factory=capture)
    task = asyncio.create_task(schedule.tick(NOW, True, MARKET, ''))
    try:
        await asyncio.wait_for(draining.wait(), 2)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        assert await schedule.tick(NOW, True, MARKET, '') == 'analysis_collecting'
        assert store.analysis_receipt('market', 'morning', DAY) is None
    finally:
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError): await task
    assert store.analysis_receipt('market', 'morning', DAY) is None
    manager.complete_role.assert_not_awaited()
    n._send_report_once.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('stage', ['capture', 'normalize', 'import'])
async def test_research_failures_have_distinct_reason_and_class_only_warning(tmp_path, content, sources, monkeypatch, stage):
    from src.analytics import market_report_runner as runner
    warning = Mock()
    monkeypatch.setattr(runner, 'logger', SimpleNamespace(warning=warning), raising=False)
    factory = AsyncMock(return_value=bundle())
    if stage == 'capture': factory.side_effect = ValueError('PRIVATE_CANARY')
    if stage == 'normalize': sources.normalize_public_research.side_effect = ValueError('PRIVATE_CANARY')
    if stage == 'import': monkeypatch.setitem(sys.modules, 'src.analytics.market_research_sources', None)
    store = ReportStore(tmp_path/'r.db')
    manager = SimpleNamespace(complete_role=AsyncMock())
    assert await ReportSchedule(store, notifier(), analysis_manager=manager,
        public_evidence_factory=factory).tick(NOW, True, MARKET, '') == 'accepted'
    saved = store.analysis_receipt('market', 'morning', DAY)
    assert saved['metadata']['reason'] == 'research_capture_failed'
    assert warning.called and stage in str(warning.call_args_list)
    assert ('ModuleNotFoundError' if stage == 'import' else 'ValueError') in str(warning.call_args_list)
    assert 'PRIVATE_CANARY' not in str(warning.call_args_list) + json.dumps(saved)
    manager.complete_role.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('settled', [False, True])
async def test_internal_model_cancellation_sends_facts_once_without_cancelling_scheduler(tmp_path, content, sources, settled):
    cancelled = asyncio.CancelledError('PRIVATE_CANARY')
    if settled:
        cancelled._llm_response = response(success=False, usage_known=True)
    manager = SimpleNamespace(complete_role=AsyncMock(side_effect=cancelled))
    factory = AsyncMock(return_value=bundle())
    store = ReportStore(tmp_path/'r.db')
    n = notifier()
    schedule = ReportSchedule(store, n, analysis_manager=manager, public_evidence_factory=factory)
    assert await schedule.tick(NOW, True, MARKET, '') == 'accepted'
    row = store.analysis_receipt('market', 'morning', DAY)
    assert row['status'] == ('failed' if settled else 'unknown')
    assert row['metadata']['reason'] == 'provider_exception'
    assert '분석 미확보' in row['final_body']
    assert 'PRIVATE_CANARY' not in json.dumps(row)
    if settled:
        assert row['metadata']['usage_known'] is True
        assert row['metadata']['budget_reservation_token'] == 'b'*32
    assert await schedule.tick(NOW, True, MARKET, '') == 'already_consumed'
    factory.assert_awaited_once()
    manager.complete_role.assert_awaited_once()
    n._send_report_once.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize('settlement', ['returned', 'raised'])
async def test_external_model_cancellation_drains_and_keeps_settlement(tmp_path, content, sources, settlement):
    entered, settling, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    async def complete(*a, **kw):
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError as cancelled:
            settling.set()
            await release.wait()
            received = response(success=False, usage_known=True)
            if settlement == 'raised':
                cancelled._llm_response = received
                raise
            return received
    manager = SimpleNamespace(complete_role=AsyncMock(side_effect=complete))
    store = ReportStore(tmp_path/'r.db')
    n = notifier()
    schedule = ReportSchedule(store, n, analysis_manager=manager,
                              public_evidence_factory=AsyncMock(return_value=bundle()))
    task = asyncio.create_task(schedule.tick(NOW, True, MARKET, ''))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        task.cancel()
        await asyncio.wait_for(settling.wait(), 2)
        task.cancel()
        assert await schedule.tick(NOW, True, MARKET, '') == 'analysis_collecting'
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError): await task
    row = store.analysis_receipt('market', 'morning', DAY)
    assert row['status'] == 'unknown' and row['metadata']['reason'] == 'cancelled'
    assert row['metadata']['usage_known'] is True
    assert row['metadata']['budget_reservation_token'] == 'b'*32
    manager.complete_role.assert_awaited_once()
    n._send_report_once.assert_not_awaited()
