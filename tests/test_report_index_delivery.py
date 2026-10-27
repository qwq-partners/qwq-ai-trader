import asyncio
from dataclasses import replace
from datetime import datetime
from threading import Event
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo
import pytest
from src.analytics.audience_reports import MarketSnapshot, render_market_report
from src.analytics.report_delivery import ReportStore
from src.schedulers.report_scheduler import ReportSchedule
from src.utils.telegram import TelegramNotifier

NOW = datetime(2026, 10, 27, 16, 40, tzinfo=ZoneInfo('Asia/Seoul'))
DAY = NOW.date().isoformat()
ROW = dict(code='0001', label='KOSPI', price=5638.35,
           source='KIS/FHKUP03500200', source_observed_at='2026-10-27T15:32:00+09:00',
           received_at=NOW.isoformat(), observation_kind='historical_minute', finality='unverified')
MARKET = MarketSnapshot(sectors=({'name': '반도체', 'r1': 1.5, 'as_of': DAY, 'source': 'etf'},), kr_basis_date=DAY)


def notifier():
    n = TelegramNotifier(bot_token='fake', chat_id='dm', report_chat_id='channel')
    n._send_report_once = AsyncMock(return_value='accepted')
    return n


def test_indicative_price_has_date_but_never_direction_or_daily_pct():
    text = render_market_report('after', replace(MARKET, sectors=(), index_observations=(ROW,)), NOW)
    assert '5,638.35' in text and '10/27 15:32' in text
    assert '확정 종가 아님' in text and '분 단위 참고 관측' in text
    assert '상승 흐름' not in text and '%' not in text
    assert len(text) <= 1800 and len(text.splitlines()) <= 35
    assert render_market_report('midday', replace(MARKET, sectors=(), index_observations=(ROW,)), NOW) is None


@pytest.mark.parametrize('bad', [dict(source_observed_at='2026-10-26T15:32:00+09:00'),
    dict(price=float('nan')), dict(price=True), dict(source='unknown'), dict(finality='confirmed'),
    dict(received_at='2026-10-27T16:40:00'), dict(source_observed_at='2026-10-27T17:00:00+09:00')])
def test_invalid_observations_not_rehabilitated_at_render(bad):
    assert render_market_report('after', MarketSnapshot(kr_basis_date=DAY, index_observations=({**ROW, **bad},)), NOW) is None


@pytest.mark.asyncio
async def test_concurrent_schedule_cannot_publish_fallback_before_winner(tmp_path):
    entered, release = asyncio.Event(), asyncio.Event()
    async def fetch(day):
        assert day == NOW.date()
        entered.set()
        await release.wait()
        return (ROW,)
    path = tmp_path/'reports.db'
    n = notifier()
    a = ReportSchedule(ReportStore(path), n, index_fetcher=fetch, clock=lambda: NOW)
    other_fetch = AsyncMock(return_value=())
    b = ReportSchedule(ReportStore(path), n, index_fetcher=other_fetch, clock=lambda: NOW)
    task = asyncio.create_task(a.tick(NOW, True, MARKET, ''))
    await asyncio.wait_for(entered.wait(), 2)
    assert await b.tick(NOW, True, MARKET, '') == 'input_collecting'
    n._send_report_once.assert_not_awaited()
    release.set()
    assert await task == 'accepted'
    assert '5,638.35' in n._send_report_once.call_args.args[1]
    assert await b.tick(NOW, True, MARKET, '') == 'already_consumed'
    other_fetch.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancelled_fetch_is_spent_and_restart_falls_back(tmp_path):
    entered = asyncio.Event()
    async def fetch(day):
        entered.set()
        await asyncio.Event().wait()
    path = tmp_path/'reports.db'
    n = notifier()
    a = ReportSchedule(ReportStore(path), n, index_fetcher=fetch, clock=lambda: NOW)
    task = asyncio.create_task(a.tick(NOW, True, MARKET, ''))
    await asyncio.wait_for(entered.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    resumed_fetch = AsyncMock()
    b = ReportSchedule(ReportStore(path), n, index_fetcher=resumed_fetch, clock=lambda: NOW)
    assert await b.tick(NOW, True, MARKET, '') == 'accepted'
    resumed_fetch.assert_not_awaited()
    assert '5,638.35' not in n._send_report_once.call_args.args[1]


@pytest.mark.asyncio
async def test_completed_inputs_survive_failure_before_publish(tmp_path):
    path = tmp_path/'reports.db'
    n = notifier()
    n.publish_report = AsyncMock(side_effect=RuntimeError('before claim'))
    fetch = AsyncMock(return_value=(ROW,))
    with pytest.raises(RuntimeError):
        await ReportSchedule(ReportStore(path), n, index_fetcher=fetch, clock=lambda: NOW).tick(NOW, True, MARKET, '')
    restarted = notifier()
    assert await ReportSchedule(ReportStore(path), restarted, index_fetcher=fetch, clock=lambda: NOW).tick(NOW, True, MARKET, '') == 'accepted'
    fetch.assert_awaited_once()
    assert '5,638.35' in restarted._send_report_once.call_args.args[1]


@pytest.mark.asyncio
async def test_holiday_and_other_slots_never_fetch(tmp_path):
    fetch = AsyncMock()
    n = notifier()
    s = ReportSchedule(ReportStore(tmp_path/'r.db'), n, index_fetcher=fetch, clock=lambda: NOW)
    assert await s.tick(NOW, False, MARKET, '') == 'skipped_holiday'
    await s.tick(NOW.replace(hour=13, minute=10), True, MARKET, '')
    await s.tick(NOW.replace(hour=7, minute=40), True, MARKET, '')
    fetch.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancel_during_input_commit_keeps_lock_until_thread_drained(tmp_path, monkeypatch):
    path = tmp_path/'r.db'
    store = ReportStore(path)
    entered, release = Event(), Event()
    real_claim = store.claim_inputs
    def paused(*args):
        entered.set()
        assert release.wait(3)
        return real_claim(*args)
    monkeypatch.setattr(store, 'claim_inputs', paused)
    fetch = AsyncMock(return_value=(ROW,))
    task = asyncio.create_task(ReportSchedule(store, notifier(), index_fetcher=fetch, clock=lambda: NOW).tick(NOW, True, MARKET, ''))
    assert await asyncio.to_thread(entered.wait, 2)
    task.cancel()
    await asyncio.sleep(0)
    b = ReportSchedule(ReportStore(path), notifier(), index_fetcher=fetch, clock=lambda: NOW)
    assert await b.tick(NOW, True, MARKET, '') == 'input_collecting'
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await b.tick(NOW, True, MARKET, '') == 'accepted'
    fetch.assert_not_awaited()


@pytest.mark.asyncio
async def test_receipt_after_tick_start_uses_post_fetch_clock(tmp_path):
    later = NOW.replace(second=2)
    row = {**ROW, 'received_at': later.isoformat()}
    n = notifier()
    schedule = ReportSchedule(ReportStore(tmp_path/'r.db'), n,
                              index_fetcher=AsyncMock(return_value=(row,)), clock=lambda: later)
    assert await schedule.tick(NOW, True, MARKET, '') == 'accepted'
    assert '5,638.35' in n._send_report_once.call_args.args[1]


def test_full_after_mobile_budget():
    sectors = tuple({'name': '가'*24, 'r1': n, 'as_of': DAY, 'source': 'etf'} for n in range(11))
    second = {**ROW, 'code': '1001', 'label': 'KOSDAQ'}
    report = render_market_report('after', replace(MARKET, sectors=sectors, index_observations=(ROW, second)), NOW)
    assert len(report) <= 1800 and len(report.splitlines()) <= 35


@pytest.mark.asyncio
async def test_fetcherless_contender_also_waits_for_live_input_owner(tmp_path):
    entered, release = asyncio.Event(), asyncio.Event()
    async def fetch(day):
        entered.set()
        await release.wait()
        return (ROW,)
    path = tmp_path/'r.db'
    n = notifier()
    a = ReportSchedule(ReportStore(path), n, index_fetcher=fetch, clock=lambda: NOW)
    task = asyncio.create_task(a.tick(NOW, True, MARKET, ''))
    await asyncio.wait_for(entered.wait(), 2)
    try:
        assert await ReportSchedule(ReportStore(path), n, clock=lambda: NOW).tick(NOW, True, MARKET, '') == 'input_collecting'
        n._send_report_once.assert_not_awaited()
    finally:
        release.set()
        await task
