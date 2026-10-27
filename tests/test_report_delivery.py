from datetime import datetime
from zoneinfo import ZoneInfo
from unittest.mock import AsyncMock, Mock
from concurrent.futures import ThreadPoolExecutor

import pytest

from src.analytics.report_delivery import ReportStore
from src.utils.telegram import TelegramNotifier


def test_claim_is_atomic_across_connections_and_restart(tmp_path):
    path = tmp_path / 'reports.sqlite3'
    a, b = ReportStore(path), ReportStore(path)
    assert a.claim('market', 'midday', '2026-10-27', 'snapshot-a')
    assert not b.claim('market', 'midday', '2026-10-27', 'snapshot-b')
    assert b.receipt('market', 'midday', '2026-10-27')['status'] == 'attempted'
    a.finish('market', 'midday', '2026-10-27', 'accepted')
    assert not ReportStore(path).claim('market', 'midday', '2026-10-27', 'c')
    assert a.claim('engine', 'daily', '2026-10-27', 'a')


def test_concurrent_process_equivalent_connections_claim_once(tmp_path):
    path = tmp_path / 'reports.sqlite3'
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda n: ReportStore(path).claim('market', 'after', '2026-10-27', str(n)), range(8)))
    assert sum(results) == 1


def test_digest_records_replace_same_kind_without_sending(tmp_path):
    store = ReportStore(tmp_path / 'reports.sqlite3')
    now = datetime(2026, 10, 27, 13, 30, tzinfo=ZoneInfo('Asia/Seoul'))
    store.record('batch', 'old', now=now)
    store.record('batch', 'new', now=now)
    assert store.records('2026-10-27') == [{'kind': 'batch', 'text': 'new', 'recorded_at': now.isoformat()}]
    store.record('llm_migration', 'late', now=now.replace(hour=22))
    assert len(store.records('2026-10-27')) == 1
    assert store.records('2026-10-28')[0]['text'] == 'late'


@pytest.mark.asyncio
async def test_audiences_are_separate_and_regular_reports_once(tmp_path):
    notifier = TelegramNotifier(bot_token='fake', chat_id='dm', alert_chat_id='dm', report_chat_id='channel')
    notifier._send_to_chat = AsyncMock(return_value=True)
    store = ReportStore(tmp_path / 'reports.sqlite3')
    args = dict(audience='market', kind='midday', report_date='2026-10-27', source_snapshot_id='s', text='시장', store=store)
    assert await notifier.publish_report(**args) == 'accepted'
    assert await notifier.publish_report(**args) == 'already_consumed'
    notifier._send_to_chat.assert_awaited_once_with('channel', '시장', 'HTML', False)
    args.update(audience='engine', kind='daily', text='엔진')
    assert await notifier.publish_report(**args) == 'accepted'
    assert notifier._send_to_chat.call_args.args[0] == 'dm'


@pytest.mark.asyncio
async def test_missing_target_never_falls_back_or_consumes_other_audience(tmp_path):
    notifier = TelegramNotifier(bot_token='fake', chat_id='dm')
    notifier.report_chat_id = None
    notifier._send_to_chat = AsyncMock()
    store = ReportStore(tmp_path / 'reports.sqlite3')
    assert await notifier.publish_report('market', 'morning', '2026-10-27', 's', 'text', store=store) == 'failed_configuration'
    notifier._send_to_chat.assert_not_awaited()
    assert store.receipt('market', 'morning', '2026-10-27')['status'] == 'failed_configuration'


@pytest.mark.asyncio
async def test_ambiguous_failure_is_not_retried_and_urgent_is_independent(tmp_path):
    notifier = TelegramNotifier(bot_token='fake', chat_id='dm', alert_chat_id='dm')
    notifier._send_to_chat = AsyncMock(side_effect=TimeoutError)
    store = ReportStore(tmp_path / 'reports.sqlite3')
    assert await notifier.publish_report('engine', 'daily', '2026-10-27', 's', 'text', store=store) == 'unknown'
    assert await notifier.publish_report('engine', 'daily', '2026-10-27', 's', 'text', store=store) == 'already_consumed'
    response = AsyncMock()
    response.status = 200
    response.__aenter__.return_value = response
    notifier._session = Mock(closed=False)
    notifier._session.post.return_value = response
    assert await notifier.send_alert('order accepted')
    assert notifier._session.post.call_args.kwargs['json']['chat_id'] == 'dm'
    assert notifier._session.post.call_args.kwargs['json']['text'] == 'order accepted'


@pytest.mark.asyncio
async def test_bad_kind_and_oversize_body_are_rejected_before_transport(tmp_path):
    n = TelegramNotifier(bot_token='fake', chat_id='dm')
    n._send_to_chat = AsyncMock()
    store = ReportStore(tmp_path / 'r.sqlite3')
    with pytest.raises(ValueError):
        await n.publish_report('market', 'daily', '2026-10-27', 's', 'x', store=store)
    with pytest.raises(ValueError):
        await n.publish_report('engine', 'daily', '2026-10-27', 's', 'x' * 1801, store=store)
    n._send_to_chat.assert_not_awaited()
