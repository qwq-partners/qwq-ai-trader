"""L01 Task 1 — 테마 탐지 루프는 거래일 06:30~16:45 KST 창에서만 LLM 탐지를 호출한다.

11/8 월 예산 원장: gpt-5-mini 테마 탐지가 24시간·주말 포함 10분마다 돌아 OpenAI 청구의
절반을 차지했다. 창 밖에서는 다음 창 시작까지 대기하고(상한 1시간, 휴장일 갱신 재평가),
소비자(스크리너 뉴스 심리·보고·대시보드)는 마지막 성공 결과를 그대로 본다.
"""
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from types import SimpleNamespace
from unittest.mock import AsyncMock

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


def test_same_instant_in_utc_and_kst_gives_same_verdict(holidays):
    kst = datetime(2026, 11, 9, 9, 30, tzinfo=ZoneInfo("Asia/Seoul"))   # 장중
    assert module._theme_window_wait_seconds(kst) == 0
    assert module._theme_window_wait_seconds(kst.astimezone(timezone.utc)) == 0  # UTC 00:30 로 읽어도 장중
    late = datetime(2026, 11, 9, 23, 0, tzinfo=ZoneInfo("Asia/Seoul"))    # 월 23:00 → 화 휴장 → 수 06:30
    expected = module._theme_window_wait_seconds(late)
    assert expected == pytest.approx(31 * 3600 + 30 * 60)
    assert module._theme_window_wait_seconds(late.astimezone(timezone.utc)) == pytest.approx(expected)


def test_gate_clock_is_kst_aware():
    now = module._theme_gate_now()
    assert now.tzinfo is not None and now.utcoffset().total_seconds() == 9 * 3600


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
    monkeypatch.setattr(module, "_theme_gate_now", lambda: now)

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


# ── 독립 리뷰 P1: 창 밖 대기 뒤 첫 탐지에서 만료 캐시가 새 결과를 가리면 안 된다 ──────────
def _real_detector(monkeypatch, llm_result, sentiments, themes):
    import src.signals.sentiment.kr_theme_detector as kd
    news = SimpleNamespace(get_market_news=AsyncMock(return_value=[kd.NewsArticle(
        title='synthetic semiconductor demand shock', content='synthetic', url='u', published_at=datetime.now())]))
    monkeypatch.setattr(kd, 'NewsCollector', lambda: news)
    llm = SimpleNamespace(complete_json=AsyncMock(return_value=llm_result))
    d = kd.ThemeDetector(llm_manager=llm)
    d._storage_initialized = True
    # 점수 보정 단계는 전역 KIS/US 시세 싱글톤을 조회하므로 스텁(격리 가드: 네트워크 0)
    d._adjust_scores_by_sector = AsyncMock()
    d._adjust_scores_by_us_market = AsyncMock()
    d._stock_sentiments = sentiments
    d._themes = themes
    return d, kd


_NEW_BEARISH = {"themes": [{"theme": "AI/반도체", "news_count": 1, "score": 80, "reason": "s", "stocks": []}],
                "stock_impacts": [{"symbol": "000001", "name": "s", "impact": -7, "direction": "bearish",
                                   "catalyst_phase": "unknown", "reason": "synthetic bad news"}]}


@pytest.mark.asyncio
async def test_resume_after_gap_replaces_expired_sentiment_with_new_lower_abs(monkeypatch):
    stale_at = datetime.now() - timedelta(hours=2)
    d, kd = _real_detector(monkeypatch, _NEW_BEARISH,
                           {"000001": {"impact": 8, "abs_impact": 8, "updated_at": stale_at}},
                           {})
    d._themes = {"old": kd.ThemeInfo(name="old", last_updated=stale_at)}
    await d.detect_themes(force=True)
    assert d._stock_sentiments["000001"]["impact"] == -7      # 만료된 8 이 새 -7 을 가리지 않는다
    assert "old" not in d._themes and "AI/반도체" in d._themes  # 만료 테마는 정리, 새 테마는 등록


@pytest.mark.asyncio
async def test_fresh_higher_abs_still_wins_within_hour(monkeypatch):
    fresh_at = datetime.now() - timedelta(minutes=10)
    d, _ = _real_detector(monkeypatch, _NEW_BEARISH,
                          {"000001": {"impact": 8, "abs_impact": 8, "updated_at": fresh_at}}, {})
    await d.detect_themes(force=True)
    assert d._stock_sentiments["000001"]["impact"] == 8       # 1시간 안의 더 큰 절댓값 유지(기존 계약)


# ── 독립 리뷰 2차 P2: 탐지가 쉬는 동안 대시보드 /api/themes 에 만료 테마가 남으면 안 된다 ──────
def test_dashboard_themes_drop_expired_without_detection(monkeypatch):
    import src.signals.sentiment.kr_theme_detector as kd
    from src.dashboard import data_collector as dc
    detector = kd.ThemeDetector(llm_manager=SimpleNamespace())  # 전역 LLM 매니저 미사용
    detector._themes = {
        "stale": kd.ThemeInfo(name="stale", last_updated=datetime.now() - timedelta(hours=2)),
        "fresh": kd.ThemeInfo(name="fresh", last_updated=datetime.now() - timedelta(minutes=5)),
    }
    collector = object.__new__(dc.DashboardDataCollector)
    collector.bot = SimpleNamespace(theme_detector=detector)
    collector._build_name_cache = lambda: {}
    names = [row["name"] for row in collector.get_themes()]
    assert names == ["fresh"]
    assert "stale" not in detector._themes
