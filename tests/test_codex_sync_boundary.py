"""장외 동기화 수면이 거래일 08:00 주문 접수 경계를 넘지 않는지 검증."""

import asyncio
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from src.core import engine
from src.schedulers import kr_scheduler
from src.schedulers.kr_scheduler import KRScheduler
from src.utils import session


@pytest.mark.parametrize("now, expected", [
    (datetime(2026, 9, 15, 7, 54), 300),
    (datetime(2026, 9, 15, 7, 55), 300),
    (datetime(2026, 9, 15, 7, 59), 60),
    (datetime(2026, 9, 15, 7, 59, 59), 1),
    (datetime(2026, 9, 15, 7, 59, 59, 750000), 0.25),
    (datetime(2026, 9, 15, 8, 0), 30),
    (datetime(2026, 9, 15, 8, 55), 30),
    (datetime(2026, 9, 15, 15, 25), 30),
    (datetime(2026, 9, 15, 21, 0), 300),
    (datetime(2026, 9, 19, 7, 59, 59), 300),  # 토요일
    (datetime(2026, 9, 20, 7, 59, 59), 300),  # 일요일
    (datetime(2026, 9, 24, 7, 59, 59), 300),  # 기존 캘린더 추석 휴일
    (datetime(2026, 9, 28, 7, 59, 59), 1),    # 연휴 뒤 거래일
    (datetime(2026, 9, 15, 7, 59, tzinfo=ZoneInfo("Asia/Seoul")), 60),
])
def test_closed_sync_wait_stops_at_orderable_boundary(now, expected):
    sched = KRScheduler.__new__(KRScheduler)
    assert sched._portfolio_sync_interval(True, True, now) == expected


def test_dynamic_holiday_does_not_shorten_closed_wait(monkeypatch):
    monkeypatch.setattr(engine, "_kr_market_holidays", {date(2026, 9, 15)})
    sched = KRScheduler.__new__(KRScheduler)
    assert sched._portfolio_sync_interval(True, True, datetime(2026, 9, 15, 7, 59)) == 300


@pytest.mark.parametrize("closed, prev_closed", [(True, False), (False, True), (False, False)])
def test_transition_and_open_wait_remain_30_seconds(closed, prev_closed):
    sched = KRScheduler.__new__(KRScheduler)
    assert sched._portfolio_sync_interval(closed, prev_closed, datetime(2026, 9, 15, 7, 59, 59)) == 30


@pytest.mark.parametrize("sync_duration", [0, 59])
def test_loop_resumes_at_0800_without_pending_orders(monkeypatch, sync_duration):
    # 실제 세션 판정과 동기화 루프를 사용하되 원장 I/O와 수면만 가상 시계로 대체한다.
    class Clock(datetime):
        current = datetime(2026, 9, 15, 7, 58)

        @classmethod
        def now(cls, tz=None):
            return cls.current

    sched = KRScheduler.__new__(KRScheduler)
    sched.bot = SimpleNamespace(running=True, risk_manager=None, _pending_orders={})
    sched.bot._get_current_session = lambda: session.KRSession().get_session(Clock.current)
    sync_times = []
    sleeps = []

    async def sync_portfolio():
        sync_times.append(Clock.current)
        if len(sync_times) == 2:
            Clock.current += timedelta(seconds=sync_duration)
        if len(sync_times) == 4:
            sched.bot.running = False

    async def sleep(seconds):
        sleeps.append(seconds)
        Clock.current += timedelta(seconds=seconds)

    sched._sync_portfolio = sync_portfolio
    monkeypatch.setattr(kr_scheduler, "datetime", Clock)
    monkeypatch.setattr(kr_scheduler.asyncio, "sleep", sleep)
    asyncio.run(sched.run_portfolio_sync())

    assert sync_times == [
        datetime(2026, 9, 15, 7, 58, 30),
        datetime(2026, 9, 15, 7, 59),
        datetime(2026, 9, 15, 8, 0),
        datetime(2026, 9, 15, 8, 0, 30),
    ]
    assert sleeps == [30, 30, 60 if sync_duration == 0 else 1, 30, 30]
