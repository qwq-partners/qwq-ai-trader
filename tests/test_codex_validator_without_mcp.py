"""Direct news/DART validation must not revive retired MCP sources (offline)."""

import asyncio
import sys
from types import SimpleNamespace

import pytest

from src.agents.analysts import FundamentalAnalyst
from src.signals.fundamentals.dart_checker import DartCheckResult
from src.signals.fundamentals.news_verifier import NewsCheckResult
from src.signals.fundamentals.stock_validator import StockValidator


def _validator(news=None, dart=None):
    validator = StockValidator()
    validator.news_verifier._enabled = True
    validator.dart_checker._enabled = True
    validator.dart_checker._corp_code_map = {"005930": "00126380"}

    async def check_news(symbol, name):
        assert (symbol, name) == ("005930", "삼성전자")
        return news if news is not None else NewsCheckResult(fetched=True)

    async def check_dart(symbol):
        assert symbol == "005930"
        return dart if dart is not None else DartCheckResult(fetched=True)

    validator.news_verifier.check_news = check_news
    validator.dart_checker.check_disclosures = check_dart
    return validator


def test_initialize_only_initializes_direct_dart_dependency(monkeypatch):
    validator = _validator()
    events = []

    async def ensure_map():
        events.append("dart")

    def get_manager():
        events.append("retired_mcp")
        return SimpleNamespace()

    validator.dart_checker.ensure_corp_code_map = ensure_map
    monkeypatch.setitem(sys.modules, "src.utils.mcp_client", SimpleNamespace(get_mcp_manager=get_manager))

    asyncio.run(validator.initialize())

    assert events == ["dart"]


def test_available_retired_sources_cannot_add_confidence_or_evidence():
    validator = _validator()
    external_calls = []

    async def call_tool(server, _tool, _arguments):
        external_calls.append(server)
        if server == "pykrx":
            payload = '{"data": [{"외국인합계": 1000, "기관합계": 2000}]}'
        else:
            payload = '{"results": [{"data": [' + ','.join(
                '{"ratio": %d}' % ratio for ratio in [10] * 7 + [20] * 7
            ) + ']}]}'
        return SimpleNamespace(content=[SimpleNamespace(text=payload)], isError=False)

    # Model a formerly available source: removal must ignore it, not merely depend
    # on an unavailable SDK/server to suppress the old positive adjustments.
    validator._mcp_manager = SimpleNamespace(is_server_available=lambda _name: True, call_tool=call_tool)
    result = asyncio.run(validator.validate("005930", "삼성전자"))

    assert external_calls == []
    assert result.confidence_adjustment == 0.0
    assert result.supply_demand_result.foreign_net_buying is False
    assert result.supply_demand_result.institutional_net_buying is False
    assert result.supply_demand_result.confidence_adjustment == 0.0
    assert result.short_selling_result.in_top50 is False
    assert result.short_selling_result.confidence_adjustment == 0.0
    assert result.trend_buzz_result.trend_direction == "neutral"
    assert result.trend_buzz_result.confidence_adjustment == 0.0
    assert result.validated is False
    assert result.data_status == "insufficient"


def test_direct_neutral_success_does_not_promote_missing_sources_to_risk_clear():
    validator = _validator()
    result = asyncio.run(validator.validate("005930", "삼성전자"))
    report = asyncio.run(FundamentalAnalyst(stock_validator=validator).analyze("005930", "삼성전자"))

    assert result.approved is True
    assert result.news_result.fetched is True
    assert result.dart_result.fetched is True
    assert result.validated is False
    assert result.data_status == "insufficient"
    assert report.risk_clear is None
    assert report.positive_basis is None
    assert not any(item.metric == "risk_clear" and item.value is True for item in report.evidence)


@pytest.mark.parametrize(("news_adj", "dart_adj", "expected"), [
    (0.10, 0.10, 0.20), (0.20, 0.10, 0.25),
    (-0.20, -0.10, -0.30), (-0.30, -0.10, -0.30),
    (-0.10, 0.10, 0.0),
])
def test_direct_news_dart_adjustments_keep_existing_sum_and_bounds(news_adj, dart_adj, expected):
    validator = _validator(
        NewsCheckResult(fetched=True, confidence_adjustment=news_adj),
        DartCheckResult(fetched=True, confidence_adjustment=dart_adj),
    )
    result = asyncio.run(validator.validate("005930", "삼성전자"))

    assert result.approved is True
    assert result.confidence_adjustment == pytest.approx(expected)
    assert result.validated is False
    assert result.data_status == "insufficient"


@pytest.mark.parametrize("fetched", [False, True])
def test_known_dart_block_survives_missing_sources_and_positive_news(fetched):
    validator = _validator(
        NewsCheckResult(fetched=True, confidence_adjustment=0.10),
        DartCheckResult(fetched=fetched, has_risk=True, risk_level="block",
                        risk_disclosures=["유상증자결정"], confidence_adjustment=-1.0),
    )
    result = asyncio.run(validator.validate("005930", "삼성전자"))

    assert result.approved is False
    assert result.confidence_adjustment == -1.0
    assert "유상증자결정" in result.block_reason
    assert result.validated is True
    assert result.data_status == "partial"


@pytest.mark.parametrize("source", ["news", "dart"])
def test_direct_fetch_exception_remains_unacquired_without_bonus(source):
    validator = _validator()

    async def fail(*_args):
        raise RuntimeError("offline fetch failure")

    if source == "news":
        validator.news_verifier.check_news = fail
    else:
        validator.dart_checker.check_disclosures = fail
    result = asyncio.run(validator.validate("005930", "삼성전자"))

    assert getattr(result, source + "_result").fetched is False
    assert result.approved is True
    assert result.confidence_adjustment == 0.0
    assert result.validated is False
    assert result.data_status == "insufficient"
