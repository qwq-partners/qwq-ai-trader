"""실시간 host-local 시계와 과거 replay의 KST 시계 계약을 함께 보존한다.

TZ=UTC 및 TZ=Asia/Seoul 환경에서 동일 테스트를 실행한다. 테스트가 프로세스의
시간대를 바꾸지 않으므로 실제 datetime.now 기본 생성자 경로까지 검증한다.
"""

import math
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from src.agents.analysts import AnalystTeam
from src.agents.types import AnalystKind, AnalystReport


def test_default_report_is_fresh_on_the_host_clock():
    # naive 생성 시각을 무조건 KST로 해석하면 UTC 호스트에서 540분으로 오판한다.
    report = AnalystReport(AnalystKind.TECHNICAL, "005930", 40, confidence=0.8)

    assert 0 <= report.age_minutes < 1
    assert 0 <= report.age_minutes_at(now=None) < 1
    assert report.to_dict()["age_minutes"] == 0.0
    assert report.freshness_decayed_confidence() == pytest.approx(0.8, abs=0.001)
    assert not AnalystTeam.is_expired(report)
    assert AnalystTeam.aggregate_score([report]) == 40


@pytest.mark.parametrize("age", [0, 15, 30, 60])
def test_explicit_naive_live_report_preserves_elapsed_host_minutes(age):
    report = AnalystReport(
        AnalystKind.TECHNICAL, "005930", 40,
        data_as_of=datetime.now() - timedelta(minutes=age),
    )

    assert report.age_minutes_at() == pytest.approx(age, abs=0.01)


@pytest.mark.parametrize("report_zone", [timezone.utc, ZoneInfo("Asia/Seoul")])
def test_aware_live_report_uses_the_current_instant(report_zone):
    # aware 시각을 host-local naive 시각과 직접 빼거나 offset을 버리면 실패한다.
    report = AnalystReport(
        AnalystKind.TECHNICAL, "005930", 40,
        data_as_of=datetime.now(report_zone) - timedelta(minutes=15),
    )

    assert report.age_minutes_at() == pytest.approx(15, abs=0.01)


@pytest.mark.parametrize(("as_of", "now"), [
    ("2026-08-03T09:00:00", "2026-08-03T10:00:00"),
    ("2026-08-03T09:00:00", "2026-08-03T01:00:00+00:00"),
    ("2026-08-03T00:00:00+00:00", "2026-08-03T10:00:00"),
    ("2026-08-03T00:00:00+00:00", "2026-08-03T10:00:00+09:00"),
    ("2026-08-03T09:00:00+09:00", "2026-08-03T01:00:00+00:00"),
])
def test_injected_historical_clock_keeps_naive_kst_contract(as_of, now):
    # 명시적 평가 시각에는 host-local 해석을 적용하면 안 된다: 모두 정확히 60분.
    report = AnalystReport(
        AnalystKind.TECHNICAL, "005930", 40, confidence=0.8,
        data_as_of=datetime.fromisoformat(as_of),
    )
    reference = datetime.fromisoformat(now)

    assert report.age_minutes_at(reference) == 60
    assert report.freshness_decayed_confidence(now=reference) == 0.4
    assert AnalystTeam.is_expired(report, now=reference)


@pytest.mark.parametrize("now", [None, datetime(2026, 8, 3, 10)])
def test_unknown_report_time_remains_unusable(now):
    report = AnalystReport(AnalystKind.TECHNICAL, "005930", 40, data_as_of=None)

    assert math.isinf(report.age_minutes_at(now))
    assert report.freshness_decayed_confidence(now=now) == 0
    assert AnalystTeam.is_expired(report, now=now)
