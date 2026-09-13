"""루프 하트비트 레지스트리·정체 판정 테스트 (2026-09-13, 2026-09-14 리뷰 T4 갱신)

실행: venv/bin/python -m pytest tests/test_loop_heartbeat.py -q
네트워크·캐시 무접촉 — 시각과 휴장일 판정을 주입한다.

2026-09-14: F5("조회 전에 beat"·"예외를 삼킨 뒤 beat") 재발 방지를 위해 record_attempt/
record_success/record_failure/record_idle/set_enabled API와 예정시각+60분 grace 기반
일일 잡 판정(기존 "직전 거래일 자정 이후" 기준은 예정시각 전에도 오탐하고 실패를 하루 늦게 잡았다)을 추가.
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
_DEFAULT_DAILY_SCHEDULE = dict(hb.DAILY_SCHEDULE)


@pytest.fixture(autouse=True)
def _fresh_registry(monkeypatch):
    monkeypatch.setattr(hb, "_beats", {})
    monkeypatch.setattr(hb, "_started", T0)
    monkeypatch.setattr(hb, "_states", {})
    monkeypatch.setattr(hb, "DAILY_SCHEDULE", dict(_DEFAULT_DAILY_SCHEDULE))


# ── 기존 필드 호환 (beat/snapshot/stale/PERIODS/DAILY) ──────────────────────

def test_beat_and_snapshot(monkeypatch):
    monkeypatch.setattr(hb.time, "time", lambda: T0 + 10)
    hb.beat("kr_fill_checker")
    snap = hb.snapshot(now=T0 + 40)
    assert snap["kr_fill_checker"] == 30
    assert snap["kr_screener"] == 40          # beat 없음 → 프로세스 시작 기산
    assert set(snap) == set(hb.PERIODS) | set(hb.DAILY)


def test_beat_is_alias_for_record_success(monkeypatch):
    monkeypatch.setattr(hb.time, "time", lambda: T0)
    hb.beat("kr_fill_checker")
    st = hb.loop_status()["kr_fill_checker"]
    assert st["last_success"] == T0
    assert st["consecutive_failures"] == 0


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


# ── 일일 잡: 예정시각 + 60분 grace (2026-09-14 리뷰 F5 — 기존 "자정 이후" 기준 대체) ──

def test_check_daily_grace_window_before_deadline_never_stale():
    # 성공 기록이 아예 없어도(전날조차 실행 안 함), 오늘 예정시각+60분 전까지는 정체가 아니다.
    before_sched = datetime(2026, 9, 15, 8, 0)     # 화요일, harvest 08:40 전
    within_grace = datetime(2026, 9, 15, 9, 30)    # 08:40~09:40 grace 이내
    assert "kr_harvest_shadow" not in hb.check(before_sched, is_holiday=WEEKEND)
    assert "kr_harvest_shadow" not in hb.check(within_grace, is_holiday=WEEKEND)


def test_check_daily_deadline_past_without_todays_success_is_stale():
    after_deadline = datetime(2026, 9, 15, 9, 41)  # 08:40 + 60분 = 09:40 그 이후
    stale = hb.check(after_deadline, is_holiday=WEEKEND)
    assert "kr_harvest_shadow" in stale
    # 오늘 예정시각 이후 실제 성공(또는 유휴)이 있으면 정체 아님
    hb._beats["kr_harvest_shadow"] = datetime(2026, 9, 15, 8, 41).timestamp()
    assert "kr_harvest_shadow" not in hb.check(after_deadline, is_holiday=WEEKEND)


def test_check_daily_yesterdays_success_insufficient_after_todays_deadline():
    fri_run = datetime(2026, 9, 11, 8, 40).timestamp()   # 금요일 수확 shadow 실행
    hb._beats["kr_harvest_shadow"] = fri_run
    # 월요일 데드라인(09:40) 전까지는 금요일 성공으로 충분 (주말은 점검 자체가 없음)
    assert "kr_harvest_shadow" not in hb.check(datetime(2026, 9, 14, 9, 30), is_holiday=WEEKEND)
    # 월요일 데드라인 이후엔 "오늘" 실행이 없으므로 정체 — 기존 버그는 화요일까지 이를 못 잡았다
    stale = hb.check(datetime(2026, 9, 14, 10, 0), is_holiday=WEEKEND)
    assert "kr_harvest_shadow" in stale
    assert stale["kr_harvest_shadow"] == pytest.approx(
        datetime(2026, 9, 14, 10, 0).timestamp() - fri_run
    )
    # 월요일 실행했으면 화요일 데드라인 전까지 정상
    hb._beats["kr_harvest_shadow"] = datetime(2026, 9, 14, 8, 40).timestamp()
    assert "kr_harvest_shadow" not in hb.check(datetime(2026, 9, 15, 9, 0), is_holiday=WEEKEND)


def test_check_daily_jobs_respect_holiday():
    holiday = lambda d: d.weekday() >= 5 or d == datetime(2026, 9, 16).date()  # 수요일 공휴일  # noqa: E731
    # 휴장일 자체는 점검을 하지 않는다
    assert hb.check(datetime(2026, 9, 16, 21, 0), is_holiday=holiday) == {}
    hb._beats["kr_evolution_scheduler"] = datetime(2026, 9, 15, 20, 30).timestamp()   # 화요일 실행
    # 목요일 데드라인(21:30) 전까지는 화요일 성공(수요일 휴장 통과)으로 충분
    assert "kr_evolution_scheduler" not in hb.check(datetime(2026, 9, 17, 21, 0), is_holiday=holiday)
    # 목요일 데드라인 이후엔 정체 (휴장일 하루를 건너뛰어도 "오늘" 몫은 채워지지 않는다)
    assert "kr_evolution_scheduler" in hb.check(datetime(2026, 9, 17, 21, 31), is_holiday=holiday)


def test_check_after_restart_uses_process_start():
    # 화요일 12:00 재시작(그날 데드라인 09:30은 이미 지남) — beat 없이도 재시작 시각 자체가
    # fallback 기산점이라 당일 남은 시간·다음날 데드라인 전까지는 정체 아님
    hb._started = datetime(2026, 9, 15, 12, 0).timestamp()
    assert "kr_vol_targeting" not in hb.check(datetime(2026, 9, 15, 13, 0), is_holiday=WEEKEND)
    # 다음날 데드라인(09:30) 이후에도 beat 가 없으면 재시작 시각(전날 12:00)이 sched 이전이라 정체
    assert "kr_vol_targeting" in hb.check(datetime(2026, 9, 16, 10, 0), is_holiday=WEEKEND)
    # 재시작이 그날 데드라인 전이면, 데드라인까지는 정체 아님
    hb._started = datetime(2026, 9, 16, 8, 0).timestamp()
    assert "kr_vol_targeting" not in hb.check(datetime(2026, 9, 16, 9, 0), is_holiday=WEEKEND)
    assert "kr_vol_targeting" in hb.check(datetime(2026, 9, 16, 9, 31), is_holiday=WEEKEND)


# ── record_attempt/success/failure/idle/set_enabled ─────────────────────────

def test_record_failure_does_not_update_staleness_but_counts():
    hb.record_attempt("kr_fill_checker")
    hb.record_failure("kr_fill_checker", "네트워크 오류")
    hb.record_failure("kr_fill_checker", "네트워크 오류")
    st = hb.loop_status()["kr_fill_checker"]
    assert st["consecutive_failures"] == 2
    assert st["last_success"] is None
    assert st["failure_reason"] == "네트워크 오류"
    assert "kr_fill_checker" not in hb._beats   # 실패는 정체 기준을 갱신하지 않는다

    hb.record_success("kr_fill_checker")
    st = hb.loop_status()["kr_fill_checker"]
    assert st["consecutive_failures"] == 0
    assert st["last_success"] is not None
    assert "kr_fill_checker" in hb._beats


def test_record_idle_is_not_a_failure_and_clears_stale_age():
    hb.record_idle("kr_screener", "장외 세션")
    st = hb.loop_status()["kr_screener"]
    assert st["idle_reason"] == "장외 세션"
    assert st["consecutive_failures"] == 0
    assert "kr_screener" in hb._beats   # 유휴는 정체가 아니다 — 기준을 갱신한다


def test_record_idle_does_not_update_last_success():
    """성공 시각(last_success)은 record_success만 갱신한다(계획서 T4 계약).

    유휴는 정체 기준(_beats)은 갱신하되 '실제로 완수'와 구분되어야 한다 — 그렇지 않으면
    유휴 이후 실제 실패가 이어져도 last_success가 '정상 동작한 적 있음'처럼 보인다
    (리뷰 blocking #1).
    """
    hb.record_idle("kr_screener", "장외 세션")
    st = hb.loop_status()["kr_screener"]
    assert st["last_success"] is None
    assert "kr_screener" in hb._beats   # 정체 기준은 여전히 갱신된다

    hb.record_success("kr_screener")
    assert hb.loop_status()["kr_screener"]["last_success"] is not None


def test_record_success_clears_idle_reason():
    hb.record_idle("kr_screener", "장외 세션")
    hb.record_success("kr_screener")
    assert hb.loop_status()["kr_screener"]["idle_reason"] is None


def test_record_success_note_field_for_degraded():
    hb.record_success("kr_dart_alert", note="1/5 실패")
    assert hb.loop_status()["kr_dart_alert"]["note"] == "1/5 실패"
    hb.record_success("kr_dart_alert")   # 다음 성공엔 note 없음 → 사라진다
    assert "note" not in hb.loop_status()["kr_dart_alert"]


def test_set_enabled_false_excludes_from_stale_and_status():
    hb.set_enabled("kr_dart_alert", False, "DART_API_KEY 미설정")
    tue_10 = datetime(2026, 9, 15, 10, 0)
    assert "kr_dart_alert" not in hb.check(tue_10, is_holiday=WEEKEND)
    status = hb.loop_status()["kr_dart_alert"]
    assert status["enabled"] is False
    assert status["idle_reason"] == "DART_API_KEY 미설정"


def test_uninitialized_feature_not_disguised_as_disabled():
    # 활성 기능의 초기화 실패(예: DART corp_code 맵 로드 실패)는 record_* 호출 없이
    # 함수가 그냥 return 한다 — enabled 는 기본 True 로 남고, beat 가 없으니 결국 정체로 드러나야 한다.
    tue_10 = datetime(2026, 9, 15, 10, 0)
    assert hb.loop_status()["kr_dart_alert"]["enabled"] is True
    assert "kr_dart_alert" in hb.check(tue_10, is_holiday=WEEKEND)


def test_loop_status_shape_and_next_due():
    early = datetime(2026, 9, 15, 8, 0)   # harvest 08:40 전
    status = hb.loop_status(early)
    assert set(status) == set(hb.PERIODS) | set(hb.DAILY)
    row = status["kr_harvest_shadow"]
    assert {"enabled", "idle_reason", "last_attempt", "last_success",
            "consecutive_failures", "next_due"} <= set(row)
    assert row["enabled"] is True
    assert row["consecutive_failures"] == 0
    assert row["next_due"] == "2026-09-15 08:40"

    late = hb.loop_status(datetime(2026, 9, 15, 9, 0))   # 08:40 지남 → 다음날
    assert late["kr_harvest_shadow"]["next_due"] == "2026-09-16 08:40"
    # 장중 루프는 next_due 가 없다 (일일 잡 전용 필드)
    assert late["kr_fill_checker"]["next_due"] is None
