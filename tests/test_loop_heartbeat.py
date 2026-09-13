"""루프 하트비트 레지스트리·정체 판정 테스트 (2026-09-13)

실행: venv/bin/python -m pytest tests/test_loop_heartbeat.py -q
네트워크·캐시 무접촉 — 시각과 휴장일 판정을 주입한다.
"""

import sys
from datetime import datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.utils import loop_heartbeat as hb  # noqa: E402

T0 = 1_000_000.0
WEEKEND = lambda d: d.weekday() >= 5  # noqa: E731


@pytest.fixture(autouse=True)
def _fresh_registry(monkeypatch):
    monkeypatch.setattr(hb, "_beats", {})
    monkeypatch.setattr(hb, "_started", T0)


def test_beat_and_snapshot(monkeypatch):
    monkeypatch.setattr(hb.time, "time", lambda: T0 + 10)
    hb.beat("kr_fill_checker")
    snap = hb.snapshot(now=T0 + 40)
    assert snap["kr_fill_checker"] == 30
    assert snap["kr_screener"] == 40          # beat 없음 → 프로세스 시작 기산
    assert set(snap) == set(hb.PERIODS) | set(hb.DAILY)


def test_stale_threshold_and_floor():
    hb._beats["a"] = T0
    assert hb.stale(T0 + 100, {"a": 45}) == {"a": 100}
    assert hb.stale(T0 + 100, {"a": 120}) == {}
    # floor(장 시작)가 beat보다 뒤면 그 시각부터 센다
    assert hb.stale(T0 + 100, {"a": 45}, floor=T0 + 80) == {}
    # beat 없는 루프는 _started 기산
    assert hb.stale(T0 + 300, {"b": 120}) == {"b": 300}


def test_check_intraday_only_in_regular_session():
    tue_10 = datetime(2026, 9, 15, 10, 0)             # 화요일 정규장
    open_ts = tue_10.replace(hour=9, minute=0).timestamp()
    # 밤새 쉰 루프: 09:00 기산 → 3600초 경과 → 전부 정체
    stale = hb.check(tue_10, is_holiday=WEEKEND)
    assert set(hb.PERIODS) <= set(stale)
    assert stale["kr_fill_checker"] == pytest.approx(3600)
    # 개장 직후엔 기산점 리셋으로 오탐 없음 (09:01, 나이 60초 < 최소 120초)
    assert not set(hb.PERIODS) & set(hb.check(tue_10.replace(hour=9, minute=1), is_holiday=WEEKEND))
    # beat 후엔 해제
    for n in hb.PERIODS:
        hb._beats[n] = open_ts + 3500
    assert not set(hb.PERIODS) & set(hb.check(tue_10, is_holiday=WEEKEND))
    # 장외(16:00)엔 장중 루프 미점검
    hb._beats.clear()
    assert not set(hb.PERIODS) & set(hb.check(tue_10.replace(hour=16), is_holiday=WEEKEND))
    # 휴장일엔 아무것도 점검하지 않음
    assert hb.check(datetime(2026, 9, 13, 10, 0), is_holiday=WEEKEND) == {}


def test_check_daily_jobs_skip_weekend_gap():
    fri_run = datetime(2026, 9, 11, 8, 40).timestamp()   # 금요일 수확 shadow 실행
    hb._beats["kr_harvest_shadow"] = fri_run
    # 월요일: 직전 거래일(금) 자정 이후 beat 있음 → 정상 (72h 경과지만 오탐 아님)
    assert "kr_harvest_shadow" not in hb.check(datetime(2026, 9, 14, 10, 0), is_holiday=WEEKEND)
    # 화요일: 월요일 실행이 없었으면 정체
    stale = hb.check(datetime(2026, 9, 15, 10, 0), is_holiday=WEEKEND)
    assert "kr_harvest_shadow" in stale
    assert stale["kr_harvest_shadow"] == pytest.approx(datetime(2026, 9, 15, 10, 0).timestamp() - fri_run)
    # 월요일 실행했으면 화요일 정상
    hb._beats["kr_harvest_shadow"] = datetime(2026, 9, 14, 8, 40).timestamp()
    assert "kr_harvest_shadow" not in hb.check(datetime(2026, 9, 15, 10, 0), is_holiday=WEEKEND)


def test_check_daily_jobs_respect_holiday():
    holiday = lambda d: d.weekday() >= 5 or d == datetime(2026, 9, 16).date()  # 수요일 공휴일  # noqa: E731
    hb._beats["kr_evolution_scheduler"] = datetime(2026, 9, 15, 20, 30).timestamp()   # 화요일 실행
    # 목요일: 직전 거래일은 화요일 → 정상
    assert "kr_evolution_scheduler" not in hb.check(datetime(2026, 9, 17, 10, 0), is_holiday=holiday)
    # 금요일: 직전 거래일 목요일에 실행 없음 → 정체
    assert "kr_evolution_scheduler" in hb.check(datetime(2026, 9, 18, 10, 0), is_holiday=holiday)


def test_check_after_restart_uses_process_start():
    # 화요일 12:00 재시작 — 아직 beat 없어도 수요일까지는 정상, 목요일엔 정체
    hb._started = datetime(2026, 9, 15, 12, 0).timestamp()
    assert "kr_vol_targeting" not in hb.check(datetime(2026, 9, 16, 10, 0), is_holiday=WEEKEND)
    assert "kr_vol_targeting" in hb.check(datetime(2026, 9, 17, 10, 0), is_holiday=WEEKEND)
