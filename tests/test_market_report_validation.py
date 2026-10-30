"""Terminal handling for invalid source boundaries and provider exceptions."""
import asyncio
import json
import sqlite3
from threading import Event
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from src.analytics.audience_reports import render_market_report
from src.analytics.report_delivery import ReportStore
from src.schedulers.report_scheduler import ReportSchedule
from tests.test_market_report_runner import DAY, NOW, MARKET, notifier, response


@pytest.fixture(autouse=True)
def no_image_transport(monkeypatch):
    monkeypatch.setattr('src.schedulers.report_scheduler._render_market_image', lambda text: None)


@pytest.mark.asyncio
@pytest.mark.parametrize('bad', ['malformed', 'over1800', 'over6000', 'many_lines', 'fallback'])
async def test_invalid_source_consumes_delivery_once_without_analysis_claim(tmp_path, monkeypatch, bad):
    from src.analytics import market_report_analysis as content
    source = render_market_report('morning', MARKET, NOW)
    if bad == 'malformed': source = 'not a structured report'
    if bad == 'over1800': source = 'x' * 1801
    if bad == 'over6000': source = 'x' * 6001
    if bad == 'many_lines': source += '\n' * 36
    if bad == 'fallback':
        def invalid(*args, **kwargs): raise ValueError('synthetic boundary failure')
        monkeypatch.setattr(content, 'factual_fallback', invalid)
    monkeypatch.setattr('src.schedulers.report_scheduler.render_market_report', lambda *args: source)
    store = ReportStore(tmp_path/'r.db')
    original_claim = store.claim_analysis
    monkeypatch.setattr(store, 'claim_analysis', Mock(wraps=original_claim))
    factory = Mock(side_effect=AssertionError('must not resolve manager'))
    n = notifier()
    schedule = ReportSchedule(store, n, analysis_manager_factory=factory)
    assert await schedule.tick(NOW, True, MARKET, '') == 'failed_validation'
    assert await schedule.tick(NOW, True, MARKET, '') == 'already_consumed'
    store.claim_analysis.assert_not_called()
    factory.assert_not_called()
    n._send_report_once.assert_not_awaited()
    assert store.analysis_receipt('market', 'morning', DAY) is None
    assert store.receipt('market', 'morning', DAY)['status'] == 'failed_validation'
    quality = store.records(DAY)
    assert len(quality) == 1 and quality[0]['kind'] == 'report_quality'


@pytest.mark.asyncio
@pytest.mark.parametrize('source', ['malformed archive', 'x'*1801, 'line\n'*36])
async def test_invalid_orphan_becomes_abandoned_and_quality_terminal(tmp_path, source):
    store = ReportStore(tmp_path/'r.db')
    assert store.claim_analysis('market', 'morning', DAY, 'original', source, 'old', owner='dead')
    factory = Mock(side_effect=AssertionError('must not resolve manager'))
    n = notifier()
    schedule = ReportSchedule(store, n, analysis_manager_factory=factory)
    assert await schedule.tick(NOW, True, MARKET, '') == 'failed_validation'
    analysis = store.analysis_receipt('market', 'morning', DAY)
    assert analysis['status'] == 'abandoned'
    assert analysis['metadata']['reason'] == 'invalid_source'
    assert analysis['source_body'] == source and analysis['final_body']
    assert await ReportSchedule(ReportStore(store.path), n, analysis_manager_factory=factory).tick(NOW, True, MARKET, '') == 'already_consumed'
    factory.assert_not_called()
    n._send_report_once.assert_not_awaited()
    assert len(store.records(DAY)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('bad', ['prompt_value', 'prompt_type', 'prompt_large', 'profile'])
async def test_analysis_preparation_failure_sends_safe_fallback_once(tmp_path, monkeypatch, bad):
    from src.analytics import market_report_analysis as content, market_report_runner as runner
    if bad == 'profile':
        def unavailable(*args): raise ValueError('profile unavailable')
        monkeypatch.setattr(runner, 'get_role_profile', unavailable)
    elif bad == 'prompt_large':
        monkeypatch.setattr(content, 'build_analysis_prompt', lambda *args, **kw: ('x'*65536, 'system'))
    else:
        def unavailable(*args, **kwargs):
            raise ValueError('prompt unavailable') if bad == 'prompt_value' else TypeError('prompt unavailable')
        monkeypatch.setattr(content, 'build_analysis_prompt', unavailable)
    factory = Mock(side_effect=AssertionError('must not resolve manager'))
    store = ReportStore(tmp_path/'r.db')
    n = notifier()
    schedule = ReportSchedule(store, n, analysis_manager_factory=factory)
    assert await schedule.tick(NOW, True, MARKET, '') == 'accepted'
    assert await schedule.tick(NOW, True, MARKET, '') == 'already_consumed'
    row = store.analysis_receipt('market', 'morning', DAY)
    assert row['status'] == 'failed' and row['metadata']['reason'] == 'prompt_unavailable'
    assert '분석 미확보' in row['final_body']
    factory.assert_not_called()
    n._send_report_once.assert_awaited_once()
    assert store.records(DAY) == []


@pytest.mark.asyncio
async def test_valid_orphan_never_rebuilds_prompt_or_profile(tmp_path, monkeypatch):
    from src.analytics import market_report_analysis as content, market_report_runner as runner
    source = render_market_report('morning', MARKET, NOW)
    prompt = Mock(side_effect=AssertionError('unused prompt'))
    profile = Mock(side_effect=AssertionError('unused profile'))
    monkeypatch.setattr(content, 'build_analysis_prompt', prompt)
    monkeypatch.setattr(runner, 'get_role_profile', profile)
    store = ReportStore(tmp_path/'r.db')
    store.claim_analysis('market', 'morning', DAY, 'original', source, 'old', owner='dead')
    n = notifier()
    assert await ReportSchedule(store, n).tick(NOW, True, MARKET, '') == 'accepted'
    assert store.analysis_receipt('market', 'morning', DAY)['status'] == 'abandoned'
    prompt.assert_not_called()
    profile.assert_not_called()
    n._send_report_once.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize('orphan', [False, True])
async def test_unexpected_fallback_exception_is_terminal(tmp_path, monkeypatch, orphan):
    from src.analytics import market_report_analysis as content
    source = render_market_report('morning', MARKET, NOW)
    store = ReportStore(tmp_path/'r.db')
    if orphan: store.claim_analysis('market', 'morning', DAY, 'original', source, 'v1', owner='dead')
    monkeypatch.setattr(content, 'factual_fallback', Mock(side_effect=KeyError('synthetic bug')))
    n = notifier()
    assert await ReportSchedule(store, n).tick(NOW, True, MARKET, '') == 'failed_validation'
    assert await ReportSchedule(store, n).tick(NOW, True, MARKET, '') == 'already_consumed'
    assert len(store.records(DAY)) == 1
    n._send_report_once.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('fault', ['refused', 'incomplete', 'invalid', 'compose_exception'])
@pytest.mark.parametrize('known', [True, False])
async def test_response_failure_status_consistently_preserves_unknown_usage(tmp_path, monkeypatch, fault, known):
    from src.analytics import market_report_analysis as content
    changes = {'refused': {'refusal_detected':True}, 'incomplete': {'finish_reason':'length'},
               'invalid': {'content':'not json'}, 'compose_exception': {}}[fault]
    if fault == 'compose_exception':
        monkeypatch.setattr(content, 'compose_analysis', Mock(side_effect=TypeError('synthetic composition failure')))
    manager = SimpleNamespace(complete_role=AsyncMock(return_value=response(usage_known=known, **changes)))
    store = ReportStore(tmp_path/'r.db')
    assert await ReportSchedule(store, notifier(), analysis_manager=manager).tick(NOW, True, MARKET, '') == 'accepted'
    row = store.analysis_receipt('market', 'morning', DAY)
    assert row['status'] == ('failed' if known else 'unknown')
    assert row['metadata']['reason'] == {'refused':'refused', 'incomplete':'incomplete',
                                       'invalid':'invalid_output', 'compose_exception':'invalid_output'}[fault]
    manager.complete_role.assert_awaited_once()


@pytest.mark.asyncio
async def test_after_invalid_orphan_preserves_original_snapshot_and_never_fetches(tmp_path):
    store = ReportStore(tmp_path/'r.db')
    store.claim_analysis('market', 'after', DAY, 'original-snapshot', 'invalid archive', 'v1', owner='dead')
    fetch = AsyncMock(side_effect=AssertionError('must not spend another GET'))
    factory = Mock(side_effect=AssertionError('must not resolve manager'))
    n = notifier()
    schedule = ReportSchedule(store, n, index_fetcher=fetch, analysis_manager_factory=factory)
    now = NOW.replace(hour=16, minute=40)
    assert await schedule.tick(now, True, MARKET, '') == 'failed_validation'
    assert await schedule.tick(now, True, MARKET, '') == 'already_consumed'
    receipt = store.receipt('market', 'after', DAY)
    assert receipt['source_snapshot_id'] == 'original-snapshot'
    assert store.analysis_receipt('market', 'after', DAY)['source_snapshot_id'] == 'original-snapshot'
    assert len(store.records(DAY)) == 1
    fetch.assert_not_awaited()
    factory.assert_not_called()
    n._send_report_once.assert_not_awaited()


@pytest.mark.asyncio
async def test_provider_exception_with_known_settlement_is_failed_and_keeps_usage(tmp_path):
    error = RuntimeError('synthetic post-settlement error')
    error._llm_response = response(success=False, request_sent=True, usage_known=True)
    manager = SimpleNamespace(complete_role=AsyncMock(side_effect=error))
    store = ReportStore(tmp_path/'r.db')
    assert await ReportSchedule(store, notifier(), analysis_manager=manager).tick(NOW, True, MARKET, '') == 'accepted'
    row = store.analysis_receipt('market', 'morning', DAY)
    assert row['status'] == 'failed'
    assert row['metadata']['reason'] == 'provider_exception'
    assert row['metadata']['usage_known'] is True
    assert row['metadata']['input_tokens'] == 120 and row['metadata']['output_tokens'] == 50
    assert row['metadata']['budget_reservation_token'] == 'b'*32


@pytest.mark.asyncio
@pytest.mark.parametrize('factory_error', [False, True])
@pytest.mark.parametrize('error_type', [ValueError, json.JSONDecodeError])
async def test_provider_value_errors_keep_unknown_settlement_metadata(tmp_path, factory_error, error_type):
    error = error_type('SECRET_CANARY', 'x', 0) if error_type is json.JSONDecodeError else error_type('SECRET_CANARY')
    error._llm_response = response(success=False, usage_known=False)
    manager = SimpleNamespace(complete_role=AsyncMock(side_effect=error))
    factory = Mock(side_effect=error) if factory_error else None
    store = ReportStore(tmp_path/'r.db')
    n = notifier()
    n._send_report_photo_once = AsyncMock(return_value='accepted')
    schedule = ReportSchedule(store, n, analysis_manager=None if factory_error else manager,
                              analysis_manager_factory=factory)
    assert await schedule.tick(NOW, True, MARKET, '') == 'accepted'
    record = store.analysis_receipt('market', 'morning', DAY)
    assert record['status'] == 'unknown'
    assert record['metadata']['reason'] != 'invalid_output'
    assert record['metadata']['budget_reservation_token'] == 'b'*32
    assert record['metadata']['usage_known'] is False
    assert 'SECRET_CANARY' not in json.dumps(record)


@pytest.mark.asyncio
async def test_proven_unsent_provider_value_error_is_failed_not_invalid_output(tmp_path, monkeypatch):
    monkeypatch.setattr('src.schedulers.report_scheduler._render_market_image', lambda text: None)
    error = ValueError('local controls')
    error._llm_response = response(success=False, request_sent=False, usage_known=False)
    manager = SimpleNamespace(complete_role=AsyncMock(side_effect=error))
    store = ReportStore(tmp_path/'r.db')
    assert await ReportSchedule(store, notifier(), analysis_manager=manager).tick(NOW, True, MARKET, '') == 'accepted'
    row = store.analysis_receipt('market', 'morning', DAY)
    assert row['status'] == 'failed' and row['metadata']['reason'] != 'invalid_output'


def test_failed_validation_slot_and_quality_are_one_atomic_write(tmp_path):
    store = ReportStore(tmp_path/'r.db')
    store.receipt('market', 'morning', DAY)
    with sqlite3.connect(store.path) as db:
        db.execute("CREATE TRIGGER refuse_quality BEFORE INSERT ON report_events BEGIN SELECT RAISE(ABORT, 'synthetic'); END")
    with pytest.raises(sqlite3.IntegrityError):
        store.record_validation_failure('market', 'morning', DAY, 'source', NOW)
    assert store.receipt('market', 'morning', DAY) is None
    assert store.records(DAY) == []
    with sqlite3.connect(store.path) as db: db.execute('DROP TRIGGER refuse_quality')
    assert store.record_validation_failure('market', 'morning', DAY, 'source', NOW)
    assert not store.record_validation_failure('market', 'morning', DAY, 'source', NOW)
    assert len(store.records(DAY)) == 1


@pytest.mark.asyncio
async def test_invalid_orphan_restart_never_sends_diagnostic_body(tmp_path, monkeypatch):
    store = ReportStore(tmp_path/'r.db')
    store.claim_analysis('market', 'morning', DAY, 'original', 'invalid archive', 'v1', owner='dead')
    original = store.record_validation_failure
    monkeypatch.setattr(store, 'record_validation_failure', Mock(side_effect=RuntimeError('before failure commit')))
    n = notifier()
    with pytest.raises(RuntimeError): await ReportSchedule(store, n).tick(NOW, True, MARKET, '')
    assert store.analysis_receipt('market', 'morning', DAY)['status'] == 'abandoned'
    assert store.receipt('market', 'morning', DAY) is None
    monkeypatch.setattr(store, 'record_validation_failure', original)
    assert await ReportSchedule(store, n).tick(NOW, True, MARKET, '') == 'failed_validation'
    n._send_report_once.assert_not_awaited()
    assert len(store.records(DAY)) == 1


@pytest.mark.asyncio
async def test_cancellation_drains_atomic_validation_failure_under_guard(tmp_path, monkeypatch):
    store = ReportStore(tmp_path/'r.db')
    monkeypatch.setattr('src.schedulers.report_scheduler.render_market_report', lambda *args: 'invalid')
    entered, release = Event(), Event()
    original = store.record_validation_failure
    def paused(*args):
        entered.set()
        assert release.wait(5)
        return original(*args)
    monkeypatch.setattr(store, 'record_validation_failure', paused)
    n = notifier()
    task = asyncio.create_task(ReportSchedule(store, n).tick(NOW, True, MARKET, ''))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        task.cancel()
        await asyncio.sleep(0)
        assert await ReportSchedule(ReportStore(store.path), n).tick(NOW, True, MARKET, '') == 'analysis_collecting'
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError): await task
    assert await ReportSchedule(ReportStore(store.path), n).tick(NOW, True, MARKET, '') == 'already_consumed'
    assert store.analysis_receipt('market', 'morning', DAY) is None
    assert store.receipt('market', 'morning', DAY)['status'] == 'failed_validation'
    assert len(store.records(DAY)) == 1
