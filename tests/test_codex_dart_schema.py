"""DART disclosure-list acquisition schema regressions (offline only)."""

import asyncio
from datetime import timedelta
from types import SimpleNamespace

import aiohttp
import pytest

from src.agents import judgment
from src.agents.analysts import FundamentalAnalyst
from src.agents.types import DebateResult
from src.signals.fundamentals.dart_checker import DartChecker
from src.signals.fundamentals.news_verifier import NewsCheckResult
from src.signals.fundamentals.stock_validator import (
    ShortSellingResult,
    StockValidator,
    SupplyDemandResult,
    TrendBuzzResult,
)


class _Response:
    status = 200

    def __init__(self, payload):
        self._payload = payload

    async def json(self):
        return self._payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False


class _Session:
    def __init__(self, payload):
        self._payload = payload

    def get(self, *_args, **_kwargs):
        return _Response(self._payload)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False


def _checker(payload, monkeypatch):
    monkeypatch.setattr(aiohttp, "ClientSession", lambda *_args, **_kwargs: _Session(payload))
    checker = DartChecker.__new__(DartChecker)
    checker._api_key = "synthetic"
    checker.DART_LIST_URL = "http://localhost/dart"
    return checker


def _configured_checker(payload, monkeypatch):
    checker = _checker(payload, monkeypatch)
    checker._enabled = True
    checker._corp_code_map = {"005930": "00126380"}
    checker._cache = {}
    checker._cache_ttl = timedelta(minutes=30)
    return checker


def test_status_000_row_without_report_name_is_not_a_fetched_neutral(monkeypatch):
    """Removing the required title must not turn malformed data into risk-clear evidence."""
    checker = _checker({"status": "000", "list": [{}]}, monkeypatch)

    result = asyncio.run(checker._fetch_and_analyze("00126380", 7))

    assert result.fetched is False


@pytest.mark.parametrize(
    "payload",
    [
        {"status": "000"},
        {"status": "000", "list": None},
        {"status": "000", "list": {"report_nm": "분기보고서"}},
        {"status": "000", "list": [None]},
        {"status": "000", "list": [{"report_nm": ""}]},
        {"status": "000", "list": [{"report_nm": "   "}]},
        {"status": "000", "list": [{"report_nm": 123}]},
    ],
)
def test_status_000_malformed_disclosure_list_is_not_fetched(monkeypatch, payload):
    checker = _checker(payload, monkeypatch)

    result = asyncio.run(checker._fetch_and_analyze("00126380", 7))

    assert result.fetched is False
    assert result.has_risk is False


@pytest.mark.parametrize(
    ("payload", "has_risk", "risk_level", "positive"),
    [
        ({"status": "013"}, False, "none", []),
        ({"status": "000", "list": []}, False, "none", []),
        ({"status": "000", "list": [{"report_nm": "분기보고서"}]}, False, "none", []),
        ({"status": "000", "list": [{"report_nm": "자기주식취득결정"}]}, False, "none", ["자기주식취득결정"]),
        ({"status": "000", "list": [{"report_nm": "전환권행사"}]}, True, "warning", []),
        ({"status": "000", "list": [{"report_nm": "유상증자결정"}]}, True, "block", []),
    ],
)
def test_known_empty_neutral_and_classified_disclosures_remain_fetched(
    monkeypatch, payload, has_risk, risk_level, positive,
):
    checker = _checker(payload, monkeypatch)

    result = asyncio.run(checker._fetch_and_analyze("00126380", 7))

    assert result.fetched is True
    assert result.has_risk is has_risk
    assert result.risk_level == risk_level
    assert result.positive_disclosures == positive


def test_mixed_malformed_list_keeps_known_risk_but_is_not_fetched_or_cached(monkeypatch):
    checker = _configured_checker(
        {"status": "000", "list": [
            {"report_nm": "유상증자결정"},
            {"report_nm": " "},
        ]},
        monkeypatch,
    )

    result = asyncio.run(checker.check_disclosures("005930", use_cache=False))

    assert result.fetched is False
    assert result.has_risk is True
    assert result.risk_level == "block"
    assert result.risk_disclosures == ["유상증자결정"]
    assert checker._cache == {}


async def _async_result(value):
    return value


class _Validator:
    def __init__(self, result):
        self._result = result

    async def validate(self, *_args):
        return self._result


def test_malformed_dart_does_not_create_risk_clear_or_buy_candidate(monkeypatch):
    """A malformed neutral-looking list must stay insufficient through judgment v2."""
    checker = _configured_checker({"status": "000", "list": [{"report_nm": " "}]}, monkeypatch)
    validator = StockValidator.__new__(StockValidator)
    validator.dart_checker = checker
    validator._mcp_manager = SimpleNamespace(is_server_available=lambda _server: True)
    validator._safe_check_news = lambda *_args: _async_result((NewsCheckResult(fetched=True), True))
    validator._safe_check_supply_demand = lambda *_args: _async_result((SupplyDemandResult(), True))
    validator._safe_check_short_selling = lambda *_args: _async_result((ShortSellingResult(), True))
    validator._safe_check_trend_buzz = lambda *_args: _async_result((TrendBuzzResult(), True))

    validation = asyncio.run(validator.validate("005930", "테스트"))
    report = asyncio.run(FundamentalAnalyst(_Validator(validation)).analyze("005930", "테스트"))
    assessment = judgment.assess(
        "005930", [report],
        DebateResult(symbol="005930", bull_final=True, bear_final=True, consensus=True, confidence=1.0),
        entry_check={"status": "allow"},
    )

    assert validation.validated is False
    assert validation.data_status == "insufficient"
    assert report.risk_clear is None
    assert report.positive_basis is None
    assert assessment.merit_status == "abstain"
    assert assessment.stance_v2 != "buy_candidate"
