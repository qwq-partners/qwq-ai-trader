from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from unittest.mock import AsyncMock
import pytest

from src.analytics.audience_reports import MarketSnapshot, render_market_report, render_engine_digest
from src.analytics.report_delivery import ReportStore
from src.schedulers.report_scheduler import ReportSchedule

NOW = datetime(2026, 10, 27, 13, 12, tzinfo=ZoneInfo('Asia/Seoul'))


def quote(**updates):
    return dict(dict(label='KOSPI', price=3000, change_pct=1.2, source='kis',
                     source_observed_at=NOW.isoformat(), received_at=NOW.isoformat()), **updates)


def test_midday_requires_same_day_source_time_not_just_receipt():
    assert render_market_report('midday', MarketSnapshot(indices=(quote(source_observed_at=None),)), NOW) is None
    assert render_market_report('midday', MarketSnapshot(indices=(quote(source_observed_at=(NOW-timedelta(minutes=6)).isoformat()),)), NOW) is None
    assert render_market_report('midday', MarketSnapshot(indices=(quote(change_pct=float('nan')),)), NOW) is None
    text = render_market_report('midday', MarketSnapshot(indices=(quote(),)), NOW)
    assert 'KOSPI' in text and '+1.20%' in text
    assert 'KOSDAQ' not in text
    assert '전일 종가 대비' in text


def test_market_body_only_consumes_market_fields_and_stays_mobile():
    q = quote(engine_cash='PRIVATE_CANARY', label='<KOSPI>')
    text = render_market_report('midday', MarketSnapshot(indices=(q,)), NOW)
    assert 'PRIVATE_CANARY' not in text
    assert '<KOSPI>' not in text
    assert len(text) <= 1800 and len(text.splitlines()) <= 35


def test_engine_digest_distinguishes_no_records_from_no_trades():
    text = render_engine_digest([], '현재 대응: 신규 매수 중지', NOW)
    assert '신규 매수 중지' in text and '기록 없음' in text
    assert '체결 0' not in text


def test_digest_escaped_content_still_fits_one_message():
    rows = [{'kind': f'topic{i}', 'text': '&' * 500} for i in range(20)]
    text = render_engine_digest(rows, ('&' * 500+'\n') * 3, NOW)
    assert len(text) <= 1800 and len(text.splitlines()) <= 35
    assert not text.endswith('&amp')


def test_after_close_rejects_morning_price_of_same_day():
    now = NOW.replace(hour=16, minute=42)
    assert render_market_report('after', MarketSnapshot(indices=(quote(),), kr_basis_date='2026-10-27'), now) is None


@pytest.mark.asyncio
async def test_scheduler_consumes_missing_slot_and_reports_once_after_restart(tmp_path):
    store = ReportStore(tmp_path/'r.sqlite3')
    notifier = AsyncMock()
    schedule = ReportSchedule(store, notifier)
    assert await schedule.tick(NOW, True, MarketSnapshot(), 'context') == 'skipped_missing_current_market_basis'
    assert await ReportSchedule(store, notifier).tick(NOW, True, MarketSnapshot(indices=(quote(),)), 'context') == 'already_consumed'
    notifier.publish_report.assert_not_awaited()
    assert store.records(NOW.date().isoformat())[0]['kind'] == 'report_quality'


@pytest.mark.asyncio
async def test_holiday_and_outside_window_dont_send_market_but_digest_runs(tmp_path):
    store = ReportStore(tmp_path/'r.sqlite3')
    notifier = AsyncMock()
    notifier.publish_report.return_value = 'accepted'
    schedule = ReportSchedule(store, notifier)
    assert await schedule.tick(NOW, False, MarketSnapshot(indices=(quote(),)), '') == 'skipped_holiday'
    assert await schedule.tick(NOW.replace(minute=25), True, MarketSnapshot(indices=(quote(),)), '') is None
    assert await schedule.tick(NOW.replace(hour=21, minute=0), False, MarketSnapshot(), 'engine') == 'accepted'
    args = notifier.publish_report.call_args.args
    assert args[:2] == ('engine', 'daily')
