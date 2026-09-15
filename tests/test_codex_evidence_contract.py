"""Codex review fixes: acquisition, validation bonus, and historical clock contracts."""

import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import aiohttp
import pytest

from src.agents import judgment
from src.agents.analysts import AnalystTeam, FundamentalAnalyst
from src.agents.types import AnalystKind, AnalystReport, EvidenceItem
from src.signals.fundamentals.dart_checker import DartChecker


def test_dart_neutral_disclosure_is_marked_fetched(monkeypatch):
    class Response:
        status = 200

        async def json(self):
            return {"status": "000", "list": [{"report_nm": "분기보고서"}]}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

    class Session:
        def get(self, *_args, **_kwargs):
            return Response()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

    monkeypatch.setattr(aiohttp, "ClientSession", lambda *_args, **_kwargs: Session())
    checker = DartChecker.__new__(DartChecker)
    checker._api_key = "synthetic"
    checker.DART_LIST_URL = "http://localhost/not-called"

    result = asyncio.run(checker._fetch_and_analyze("00126380", 7))

    assert result.fetched is True
    assert result.has_risk is False


def _report(*, score=40, validation_pass_bonus=None, risk_clear=None, data_as_of=None):
    return AnalystReport(
        kind=AnalystKind.FUNDAMENTAL,
        symbol="005930",
        score=score,
        confidence=0.9,
        data_as_of=data_as_of or datetime.now(),
        data_status="full",
        evidence=[EvidenceItem(source="test", metric="signal", value=True, dedup_key="signal")],
        validation_pass_bonus=validation_pass_bonus,
        risk_clear=risk_clear,
    )


def test_validation_pass_bonus_is_removed_even_when_dart_risk_clears_risk_clear():
    report = _report(score=10, validation_pass_bonus=10, risk_clear=False)

    assessment = judgment.assess("005930", [report], None)

    assert assessment.merit_score == 0


def test_legacy_report_uses_risk_clear_fallback_for_validation_bonus():
    report = _report(score=10, validation_pass_bonus=None, risk_clear=True)

    assessment = judgment.assess("005930", [report], None)

    assert assessment.merit_score == 0


def test_fundamental_report_preserves_validation_bonus_after_dart_warning():
    async def validated(*_args, **_kwargs):
        return SimpleNamespace(
            approved=True, validated=True, data_status="partial",
            supply_demand_result=SimpleNamespace(
                foreign_net_buying=True, institutional_net_buying=True,
            ),
            short_selling_result=SimpleNamespace(in_top50=False),
        )

    async def warning(*_args, **_kwargs):
        return SimpleNamespace(has_risk=True, risk_disclosures=["전환권행사"])

    report = asyncio.run(FundamentalAnalyst(
        SimpleNamespace(validate=validated), SimpleNamespace(check_disclosures=warning),
    ).analyze("005930", "테스트"))

    assert report.score == 10
    assert report.risk_clear is False
    assert report.validation_pass_bonus == 10
    assert report.to_dict()["validation_pass_bonus"] == 10


def test_historical_clock_controls_age_ttl_decay_and_evidence_expiry():
    decision_time = datetime(2026, 9, 10, 10, 0, tzinfo=timezone(timedelta(hours=9)))
    report = _report(data_as_of=decision_time - timedelta(minutes=30))
    report.evidence[0].valid_until = decision_time + timedelta(minutes=10)

    assert report.age_minutes_at(decision_time) == 30.0
    assert report.freshness_decayed_confidence(now=decision_time) == pytest.approx(0.6364)
    assert AnalystTeam.is_expired(report, now=decision_time) is False
    assessment = judgment.assess("005930", [report], None, now=decision_time)
    assert assessment.merit_score == 40
    assert assessment.evidence_quality["expired"] == 0


def test_aware_report_accepts_naive_historical_clock_in_its_timezone():
    as_of = datetime(2026, 9, 10, 9, 30, tzinfo=timezone(timedelta(hours=9)))
    report = _report(data_as_of=as_of)

    assert report.age_minutes_at(datetime(2026, 9, 10, 10, 0)) == 30.0


def test_naive_kst_now_and_aware_utc_as_of_measure_the_same_instant():
    report = _report(data_as_of=datetime(2026, 9, 10, 0, 30, tzinfo=timezone.utc))

    assert report.age_minutes_at(datetime(2026, 9, 10, 10, 0)) == 30.0


def test_aware_utc_now_and_naive_kst_as_of_measure_the_same_instant():
    report = _report(data_as_of=datetime(2026, 9, 10, 9, 30))

    assert report.age_minutes_at(datetime(2026, 9, 10, 1, 0, tzinfo=timezone.utc)) == 30.0


def test_future_aware_as_of_is_not_treated_as_stale_against_naive_kst_now():
    report = _report(data_as_of=datetime(2026, 9, 10, 1, 30, tzinfo=timezone.utc))

    assert report.age_minutes_at(datetime(2026, 9, 10, 10, 0)) == 0.0


def test_evidence_expiry_accepts_naive_deadline_with_aware_historical_clock():
    decision_time = datetime(2026, 9, 10, 10, 0, tzinfo=timezone(timedelta(hours=9)))
    report = _report(data_as_of=decision_time - timedelta(minutes=5))
    report.evidence[0].valid_until = datetime(2026, 9, 10, 10, 5)

    assessment = judgment.assess("005930", [report], None, now=decision_time)

    assert assessment.merit_score == 40


def test_naive_kst_deadline_expires_against_aware_utc_historical_clock():
    kst = ZoneInfo("Asia/Seoul")
    now = datetime(2026, 9, 10, 1, 0, tzinfo=timezone.utc)  # KST 10:00
    report = _report(data_as_of=datetime(2026, 9, 10, 9, 30, tzinfo=kst))
    report.evidence[0].valid_until = datetime(2026, 9, 10, 9, 55)  # naive KST

    assessment = judgment.assess("005930", [report], None, now=now)

    assert assessment.merit_score is None


def test_missing_data_as_of_remains_fail_closed_and_serializable():
    report = _report()
    report.data_as_of = None

    assert report.age_minutes_at(datetime(2026, 9, 10, 10, 0)) == float("inf")
    assert report.to_dict()["data_as_of"] is None


def test_missing_report_time_serializes_as_json_null_not_infinity():
    report = _report()
    report.data_as_of = None
    restored = json.loads(json.dumps(report.to_dict(), allow_nan=False))
    assert restored["data_as_of"] is None
    assert restored["age_minutes"] is None
