"""장중 돌파 RS 정렬의 KOSPI 기준값 (2026-09-28).

없는 키 'c1' 대신 5일 변화율 c5 를 당일 등락처럼 쓰던 결함 — 종목 change_pct(당일)와 같은 좌표인
risk/manager 의 당일 추세 캐시(kospi_pct, 2분 주기 갱신)를 3분 이내일 때만 쓴다.
"""
from datetime import datetime, timedelta

from src.schedulers.kr_scheduler import _intraday_kospi_change

NOW = datetime(2026, 9, 28, 10, 30)


def test_fresh_trend_returns_today_kospi_change():
    trend = {"kospi_pct": 1.25, "avg_pct": 0.4, "ts": NOW - timedelta(seconds=90)}
    assert _intraday_kospi_change(trend, NOW) == 1.25  # avg_pct 가 아니라 KOSPI 당일 값


def test_stale_or_missing_trend_skips_rs_sort():
    assert _intraday_kospi_change({"kospi_pct": 1.25, "ts": NOW - timedelta(seconds=180)}, NOW) == 0.0
    assert _intraday_kospi_change({}, NOW) == 0.0
    assert _intraday_kospi_change(None, NOW) == 0.0
    assert _intraday_kospi_change({"kospi_pct": None, "ts": NOW}, NOW) == 0.0


def test_intraday_breakout_call_site_uses_risk_trend_cache():
    # 호출부는 거대한 장중 루프 안이라 실행 시험 대신 소스 구조로 고정한다 — 되돌림 변이(c5 조회,
    # bot.engine.risk_manager 참조: 그쪽엔 _market_trend 가 없어 늘 빈 dict)를 잡는다.
    import inspect
    from src.schedulers import kr_scheduler
    src = inspect.getsource(kr_scheduler)
    start = src.index("# ── Step3: KOSPI 오늘 등락 조회 (RS 정렬 기준) ──")
    block = src[start:src.index("# ATR 기반 동적 변동률 상한 계산", start)]
    assert '_intraday_kospi_change(' in block
    assert 'getattr(bot.risk_manager, "_market_trend", None)' in block
    assert "get_kospi_change" not in block and "engine.risk_manager" not in block


def test_negative_change_is_kept_not_treated_as_missing():
    # 하락장 값도 그대로 돌려준다(호출부가 > 0.3 일 때만 정렬 보정) — 0 과 결측을 섞지 않는다
    assert _intraday_kospi_change({"kospi_pct": -0.8, "ts": NOW}, NOW) == -0.8
