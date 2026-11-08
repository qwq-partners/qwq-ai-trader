"""L01 Task 1 — 테마 탐지 루프는 거래일 06:30~16:45 KST 창에서만 LLM 탐지를 호출한다.

11/8 월 예산 원장: gpt-5-mini 테마 탐지가 24시간·주말 포함 10분마다 돌아 OpenAI 청구의
절반을 차지했다. 창 밖에서는 다음 창 시작까지 대기하고(상한 1시간, 휴장일 갱신 재평가),
소비자(스크리너 뉴스 심리·보고·대시보드)는 마지막 성공 결과를 그대로 본다.
"""
from datetime import date, datetime
from types import SimpleNamespace

import pytest

from src.schedulers import kr_scheduler as module

# 2026-11-07 토 · 11-09 월(거래일) · 11-10 화를 합성 휴장일로 지정
_HOLIDAY = date(2026, 11, 10)


@pytest.fixture
def holidays(monkeypatch):
    monkeypatch.setattr(module, "is_kr_market_holiday",
                        lambda d: d.weekday() >= 5 or d == _HOLIDAY)


@pytest.mark.parametrize("now, expected", [
    (datetime(2026, 11, 7, 3, 0), 2 * 86400 + 3 * 3600 + 30 * 60),   # 토 새벽 → 월 06:30
    (datetime(2026, 11, 9, 2, 0), 4 * 3600 + 30 * 60),                # 거래일 새벽 → 당일 06:30
    (datetime(2026, 11, 9, 6, 30), 0),                                # 창 시작 경계 포함
    (datetime(2026, 11, 9, 10, 0), 0),                                # 장중
    (datetime(2026, 11, 9, 16, 44, 59), 0),                           # 창 끝 직전
    (datetime(2026, 11, 9, 16, 45), 37 * 3600 + 45 * 60),             # 창 끝(배타) → 화 휴장 건너 수 06:30
    (datetime(2026, 11, 10, 10, 0), 20 * 3600 + 30 * 60),             # 휴장일 장중 → 익일 06:30
])
def test_theme_window_wait_seconds(holidays, now, expected):
    assert module._theme_window_wait_seconds(now) == pytest.approx(expected)


def test_theme_window_wait_is_capped_when_no_trading_day_found(monkeypatch):
    monkeypatch.setattr(module, "is_kr_market_holiday", lambda d: True)
    assert module._theme_window_wait_seconds(datetime(2026, 11, 9, 10, 0)) == 3600.0


def _scheduler(monkeypatch, now, detect_calls, sleeps):
    detector = SimpleNamespace(detection_interval_minutes=10,
                               get_all_stock_sentiments=lambda: {})

    async def detect(force=False):
        detect_calls.append(force)
        return []

    detector.detect_themes = detect
    bot = SimpleNamespace(theme_detector=detector, running=True,
                          engine=SimpleNamespace(emit=None),
                          config=SimpleNamespace(get=lambda *_: {}))
    scheduler = object.__new__(module.KRScheduler)
    scheduler.bot = bot
    scheduler._trim_watch_symbols = lambda: None
    monkeypatch.setattr(module, "_now_kst", lambda: now)

    async def fake_sleep(seconds):
        sleeps.append(seconds)
        bot.running = False  # 한 바퀴만 돈다

    monkeypatch.setattr(module.asyncio, "sleep", fake_sleep)
    return scheduler


@pytest.mark.asyncio
async def test_loop_waits_outside_window_without_calling_llm(monkeypatch, holidays):
    detect_calls, sleeps = [], []
    scheduler = _scheduler(monkeypatch, datetime(2026, 11, 7, 3, 0), detect_calls, sleeps)
    await scheduler.run_theme_detection()
    assert detect_calls == []
    assert sleeps == [3600.0]  # 남은 185,400초를 1시간 상한으로 쪼개 대기


@pytest.mark.asyncio
async def test_loop_waits_exact_remaining_when_under_cap(monkeypatch, holidays):
    detect_calls, sleeps = [], []
    scheduler = _scheduler(monkeypatch, datetime(2026, 11, 9, 6, 0), detect_calls, sleeps)
    await scheduler.run_theme_detection()
    assert detect_calls == []
    assert sleeps == [1800.0]


@pytest.mark.asyncio
async def test_loop_detects_inside_window_then_sleeps_interval(monkeypatch, holidays):
    detect_calls, sleeps = [], []
    scheduler = _scheduler(monkeypatch, datetime(2026, 11, 9, 10, 0), detect_calls, sleeps)
    await scheduler.run_theme_detection()
    assert detect_calls == [True]
    assert sleeps == [600]
