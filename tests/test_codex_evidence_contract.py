"""Codex review fixes: acquisition, validation bonus, and historical clock contracts."""

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import aiohttp
import pytest

from src.agents import judgment
from src.agents.analysts import AnalystTeam, FundamentalAnalyst
from src.agents.types import AnalystKind, AnalystReport, EvidenceItem
from src.signals.fundamentals.dart_checker import DartChecker
from src.signals.fundamentals.stock_validator import StockValidator


class _McpResponse:
    def __init__(self, text: str = "", *, is_error: bool = False):
        self.content = [SimpleNamespace(text=text)] if text else []
        self.isError = is_error


def _validator_with_mcp(call_tool):
    validator = StockValidator.__new__(StockValidator)
    validator._mcp_manager = SimpleNamespace(
        is_server_available=lambda _server: True,
        call_tool=call_tool,
    )
    validator._supply_demand_cache = {}
    validator._trend_buzz_cache = {}
    return validator


@pytest.mark.parametrize(
    "response",
    [None, _McpResponse(is_error=True), _McpResponse("not-json"), _McpResponse('{"unexpected": 1}')],
)
def test_supply_demand_mcp_failures_are_not_acquired_or_cached(response):
    async def call_tool(*_args, **_kwargs):
        return response

    validator = _validator_with_mcp(call_tool)
    result, acquired = asyncio.run(validator._safe_check_supply_demand("005930"))

    assert acquired is False
    assert result.foreign_net_buying is False
    assert validator._supply_demand_cache == {}


@pytest.mark.parametrize(
    "response",
    [None, _McpResponse(is_error=True), _McpResponse("not-json"), _McpResponse('{"unexpected": 1}')],
)
def test_trend_buzz_mcp_failures_are_not_acquired_or_cached(response):
    async def call_tool(*_args, **_kwargs):
        return response

    validator = _validator_with_mcp(call_tool)
    result, acquired = asyncio.run(validator._safe_check_trend_buzz("삼성전자"))

    assert acquired is False
    assert result.trend_direction == "neutral"
    assert validator._trend_buzz_cache == {}


def test_zero_supply_response_is_acquired_and_cached_as_valid_neutral():
    async def call_tool(*_args, **_kwargs):
        return _McpResponse('{"data": [{"외국인합계": 0, "기관합계": 0}]}')

    validator = _validator_with_mcp(call_tool)
    result, acquired = asyncio.run(validator._safe_check_supply_demand("005930"))

    assert acquired is True
    assert result.confidence_adjustment == 0.0
    assert "005930" in validator._supply_demand_cache


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


def test_evidence_expiry_accepts_naive_deadline_with_aware_historical_clock():
    decision_time = datetime(2026, 9, 10, 10, 0, tzinfo=timezone(timedelta(hours=9)))
    report = _report(data_as_of=decision_time - timedelta(minutes=5))
    report.evidence[0].valid_until = datetime(2026, 9, 10, 10, 5)

    assessment = judgment.assess("005930", [report], None, now=decision_time)

    assert assessment.merit_score == 40


def test_missing_data_as_of_remains_fail_closed_and_serializable():
    report = _report()
    report.data_as_of = None

    assert report.age_minutes_at(datetime(2026, 9, 10, 10, 0)) == float("inf")
    assert report.to_dict()["data_as_of"] is None
