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
from src.signals.fundamentals import dart_checker as dart_module
from src.signals.fundamentals.news_verifier import NewsCheckResult
from src.signals.fundamentals.stock_validator import (
    ShortSellingResult,
    StockValidator,
    SupplyDemandResult,
    TrendBuzzResult,
)
from src.signals.screener.kr_screener import ScreenedStock, StockScreener


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


def _stock_validator(checker):
    validator = StockValidator.__new__(StockValidator)
    validator.dart_checker = checker
    validator._mcp_manager = SimpleNamespace(is_server_available=lambda _server: True)
    validator._safe_check_news = lambda *_args: _async_result((NewsCheckResult(fetched=True), True))
    validator._safe_check_supply_demand = lambda *_args: _async_result((SupplyDemandResult(), True))
    validator._safe_check_short_selling = lambda *_args: _async_result((ShortSellingResult(), True))
    validator._safe_check_trend_buzz = lambda *_args: _async_result((TrendBuzzResult(), True))
    return validator


def test_malformed_dart_does_not_create_risk_clear_or_buy_candidate(monkeypatch):
    """A malformed neutral-looking list must stay insufficient through judgment v2."""
    checker = _configured_checker({"status": "000", "list": [{"report_nm": " "}]}, monkeypatch)
    validator = _stock_validator(checker)

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


def _run_live_dart_consumers(disclosures, monkeypatch):
    """HTTP만 합성하고 실제 검증기 합산·스크리너 촉매 점수 분기를 함께 실행한다."""
    checker = _configured_checker({"status": "000", "list": disclosures}, monkeypatch)
    validator = _stock_validator(checker)
    # 스크리너의 로컬 생성만 주입: ensure_corp_code_map/check_disclosures는 실제 메서드.
    monkeypatch.setattr(dart_module, "DartChecker", lambda: checker)
    stocks = {"005930": ScreenedStock(symbol="005930", name="테스트", score=50)}
    screener = StockScreener.__new__(StockScreener)

    async def run():
        validation = await validator.validate("005930", "테스트")
        await screener._apply_dart_catalyst(stocks)
        return validation

    return asyncio.run(run()), stocks, checker


@pytest.mark.parametrize("malformed", [None, {"report_nm": 123}, {"report_nm": "   "}])
@pytest.mark.parametrize("malformed_first", [False, True])
def test_incomplete_positive_list_cannot_boost_live_validation_or_screener(
    monkeypatch, malformed, malformed_first,
):
    rows = [{"report_nm": "자기주식취득결정"}, malformed]
    if malformed_first:
        rows.reverse()
    validation, stocks, checker = _run_live_dart_consumers(rows, monkeypatch)

    assert validation.dart_result.fetched is False
    assert (validation.confidence_adjustment, stocks["005930"].score) == (0.0, 50)
    assert validation.dart_result.positive_disclosures == []
    assert validation.approved is True
    assert validation.validated is False
    assert validation.data_status == "insufficient"
    assert stocks["005930"].score == 50
    assert stocks["005930"].reasons == []
    assert checker._cache == {}


def test_complete_positive_list_keeps_existing_live_bonuses(monkeypatch):
    validation, stocks, checker = _run_live_dart_consumers(
        [{"report_nm": "자기주식취득결정"}], monkeypatch,
    )
    assert validation.dart_result.fetched is True
    assert validation.dart_result.positive_disclosures == ["자기주식취득결정"]
    assert validation.confidence_adjustment == 0.10
    assert validation.validated is True
    assert stocks["005930"].score == 65
    assert "005930" in checker._cache


@pytest.mark.parametrize(("risk_title", "level", "adjustment", "score"), [
    ("유상증자결정", "block", -1.0, None),
    ("전환권행사", "warning", -0.10, 40),
])
def test_incomplete_positive_and_risk_list_preserves_live_risk_controls(
    monkeypatch, risk_title, level, adjustment, score,
):
    validation, stocks, checker = _run_live_dart_consumers([
        {"report_nm": "자기주식취득결정"}, None, {"report_nm": risk_title},
    ], monkeypatch)
    assert validation.dart_result.fetched is False
    assert validation.dart_result.risk_disclosures == [risk_title]
    assert validation.dart_result.has_risk is True
    assert validation.dart_result.risk_level == level
    assert validation.dart_result.positive_disclosures == []
    assert validation.confidence_adjustment == adjustment
    assert validation.approved is (level != "block")
    if score is None:
        assert stocks == {}
    else:
        assert stocks["005930"].score == score
    assert checker._cache == {}
