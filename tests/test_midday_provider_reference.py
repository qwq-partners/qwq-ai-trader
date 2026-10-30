from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from src.analytics.audience_reports import MarketSnapshot, current_indices, render_market_report

KST = ZoneInfo('Asia/Seoul')
NOW = datetime(2026, 10, 28, 13, 10, tzinfo=KST)


def row(label='KOSPI', **changes):
    return dict({'label': label, 'price': 3000, 'change_pct': 1.2, 'source': 'kis',
                 'source_observed_at': None, 'received_at': NOW.isoformat()}, **changes)


def test_recent_kis_receipts_are_reference_only_not_current_indices():
    snapshot = MarketSnapshot(indices=(row(), row('KOSDAQ', price=900, change_pct=0)))
    assert current_indices(snapshot, NOW, midday=True) == []
    text = render_market_report('midday', snapshot, NOW)
    assert text is not None
    assert 'KOSPI' in text and 'KOSDAQ' in text and '+0.00%' in text
    assert '수신 13:10' in text and '원천시각 미제공' in text
    assert '전일 기준 거래일 미확인' in text
    assert '확인된 지수' not in text and '전일 종가 대비' not in text

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
import pytest

from src.analytics.report_delivery import ReportStore
from src.data.providers.kis_market_data import KISMarketData
from src.schedulers.report_scheduler import ReportSchedule, market_snapshot
from src.utils.telegram import TelegramNotifier


@pytest.mark.parametrize('changes', [
    {'source_observed_at': 'bad'},
    {'source_observed_at': (NOW - timedelta(minutes=6)).isoformat()},
    {'source_observed_at': (NOW + timedelta(seconds=1)).isoformat()},
    {'received_at': None}, {'received_at': '2026-10-28T13:10:00'},
    {'received_at': (NOW + timedelta(seconds=1)).isoformat()},
    {'received_at': (NOW - timedelta(seconds=301)).isoformat()},
    {'received_at': (NOW - timedelta(days=1)).isoformat()},
    {'source': 'unknown'}, {'label': 'OTHER'},
    {'price': True}, {'price': float('nan')}, {'price': float('inf')}, {'price': 0},
    {'change_pct': False}, {'change_pct': float('nan')}, {'change_pct': float('inf')},
])
def test_invalid_receipt_never_becomes_reference(changes):
    assert render_market_report('midday', MarketSnapshot(indices=(row(**changes),)), NOW) is None


def test_exact_five_minutes_and_utc_conversion():
    received = (NOW - timedelta(seconds=300)).astimezone(timezone.utc).isoformat()
    text = render_market_report('midday', MarketSnapshot(indices=(row(received_at=received),)), NOW)
    assert '수신 13:05' in text
    # UTC date is the prior day, but the converted KST date is current.
    early = NOW.replace(hour=9, minute=0)
    received = (early - timedelta(seconds=1)).astimezone(timezone.utc).isoformat()
    assert render_market_report('midday', MarketSnapshot(indices=(row(received_at=received),)), early)


@pytest.mark.parametrize('now', [NOW.replace(hour=8, minute=59), NOW.replace(hour=15, minute=30),
                                 NOW.replace(day=31)])
def test_reference_is_limited_to_weekday_regular_hours(now):
    assert render_market_report('midday', MarketSnapshot(indices=(row(received_at=now.isoformat()),)), now) is None


def test_identical_rows_are_shown_once():
    text = render_market_report('midday', MarketSnapshot(indices=(row(), row())), NOW)
    assert text is not None and text.count('• KOSPI ') == 1


@pytest.mark.parametrize('second', [row(price=3100), row(source='unknown'),
                                    row(received_at=(NOW - timedelta(seconds=1)).isoformat())])
def test_conflicting_rows_exclude_label(second):
    assert render_market_report('midday', MarketSnapshot(indices=(row(), second)), NOW) is None
    text = render_market_report('midday', MarketSnapshot(indices=(row(), second, row('KOSDAQ'))), NOW)
    assert 'KOSPI' not in text and 'KOSDAQ' in text


def test_verified_indices_take_precedence_and_reference_with_sector_is_honest():
    verified = row(source_observed_at=NOW.isoformat())
    text = render_market_report('midday', MarketSnapshot(indices=(verified, row('KOSDAQ'))), NOW)
    assert '확인된 지수' in text and 'KIS 응답 참고' not in text
    sector = {'name': '반도체', 'r1': 1.5, 'as_of': '2026-10-28', 'source': 'etf'}
    text = render_market_report('midday', MarketSnapshot(indices=(row(),), sectors=(sector,), kr_basis_date='2026-10-28'), NOW)
    assert '반도체' in text and '검증된 시장 방향 자료는 미확보' in text
    assert '확인된 지수' not in text and '전일 종가 대비' not in text
    assert len(text) <= 1800 and len(text.splitlines()) <= 35


@pytest.mark.asyncio
async def test_cached_receipt_through_scheduler_and_real_notifier_once(tmp_path, monkeypatch):
    monkeypatch.setattr('src.schedulers.report_scheduler._render_market_image', lambda text: None)
    provider = KISMarketData(token_manager=Mock())
    provider._cache['index_price_0001'] = {'source': 'kis', 'report_values': {'price': 3000, 'change_pct': 0}, 'received_at': NOW.isoformat()}
    bot = SimpleNamespace(kis_market_data=provider, us_market_data=SimpleNamespace(_cache={}))
    snapshot = market_snapshot(bot, NOW, lambda day: False)
    notifier = TelegramNotifier(bot_token='fake', chat_id='dm', report_chat_id='channel')
    notifier._send_report_once = AsyncMock(return_value='accepted')
    fetch = AsyncMock(return_value=())
    path = tmp_path / 'reports.sqlite3'
    a = ReportSchedule(ReportStore(path), notifier, index_fetcher=fetch)
    b = ReportSchedule(ReportStore(path), notifier, index_fetcher=fetch)
    results = await asyncio.gather(a.tick(NOW, True, snapshot, ''), b.tick(NOW, True, snapshot, ''))
    assert results.count('accepted') == 1
    # The new analysis guard may reject the contender before the delivery claim.
    assert next(result for result in results if result != 'accepted') in {'analysis_collecting', 'already_consumed'}
    assert await ReportSchedule(ReportStore(path), notifier, index_fetcher=fetch).tick(NOW, True, snapshot, '') == 'already_consumed'
    assert notifier._send_report_once.await_count == 1
    args = notifier._send_report_once.call_args.args
    assert args[0] == 'channel' and 'KIS 응답 참고' in args[1]
    fetch.assert_not_awaited()
    holiday = ReportSchedule(ReportStore(tmp_path / 'holiday.sqlite3'), notifier, index_fetcher=fetch)
    assert await holiday.tick(NOW, False, snapshot, '') == 'skipped_holiday'
    assert notifier._send_report_once.await_count == 1
    fetch.assert_not_awaited()


def test_reference_and_many_maximum_length_sectors_fit_message_limits():
    sectors = tuple({'name': '&' * 100, 'r1': value, 'as_of': '2026-10-28', 'source': 'etf'}
                    for value in range(30))
    text = render_market_report('midday', MarketSnapshot(indices=(row(), row('KOSDAQ')),
                                                           sectors=sectors, kr_basis_date='2026-10-28'), NOW)
    assert len(text) <= 1800 and len(text.splitlines()) <= 35
    assert text.count('• &amp;') == 4
