"""MCP 제거 후에도 직접 전략 데이터 수집과 기존 폴백은 유지된다."""

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

from src.signals.strategic.data_collector import StrategicDataCollector
from src.signals.strategic.supply_trend import SupplyTrendDetector


def test_collect_all_keeps_removed_sector_keys_empty_without_calling_collectors(monkeypatch):
    collector = StrategicDataCollector()
    retired_calls = []

    async def value(name):
        return {"source": name}

    for name in (
        "_collect_market_indices", "_collect_exchange_rate", "_collect_interest_rates",
        "_collect_top_foreign_buys", "_collect_top_inst_buys", "_collect_recent_themes",
        "_collect_news_summary",
    ):
        monkeypatch.setattr(collector, name, lambda name=name: value(name))

    async def forbidden():
        retired_calls.append("sector")
        raise AssertionError("폐기한 MCP 업종 조회 실행")

    monkeypatch.setattr(collector, "_collect_sector_flows", forbidden, raising=False)
    monkeypatch.setattr(collector, "_collect_sector_valuations", forbidden, raising=False)

    result = asyncio.run(collector.collect_all())

    assert retired_calls == []
    assert result["sector_flows"] == {}
    assert result["sector_valuations"] == {}
    assert result["market_indices"] == {"source": "_collect_market_indices"}
    assert result["news_summary"] == {"source": "_collect_news_summary"}


def test_supply_trend_without_kis_uses_existing_daily_fallback_without_mcp(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: tmp_path))
    detector = SupplyTrendDetector(kis_market_data=None)

    async def universe():
        return {"005930": "삼성전자"}

    detector._build_universe = universe
    monkeypatch.setitem(
        sys.modules,
        "src.utils.mcp_client",
        SimpleNamespace(get_mcp_manager=lambda: (_ for _ in ()).throw(AssertionError("MCP 호출"))),
    )

    stocks = asyncio.run(detector.detect_accumulation())

    assert [(stock.symbol, stock.score, stock.reasons) for stock in stocks] == [
        ("005930", 55, ["당일 수급 상위 (연속 데이터 미확인)"]),
    ]


def test_supply_trend_keeps_kis_investor_daily_analysis(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: tmp_path))

    class KisMarketData:
        async def fetch_stock_investor_daily(self, symbol, days):
            assert (symbol, days) == ("005930", 30)
            return {
                f"2026-09-{day:02d}": {
                    "foreign_net_buy": 100_000,
                    "inst_net_buy": 100_000,
                }
                for day in range(1, 7)
            }

    detector = SupplyTrendDetector(kis_market_data=KisMarketData())

    async def universe():
        return {"005930": "삼성전자"}

    detector._build_universe = universe

    stocks = asyncio.run(detector.detect_accumulation())

    assert len(stocks) == 1
    assert stocks[0].symbol == "005930"
    assert stocks[0].score >= 50
    assert stocks[0].foreign_streak == stocks[0].inst_streak == 6


def test_supply_trend_keeps_existing_empty_result_when_kis_daily_fetch_fails(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: tmp_path))
    fallback_calls = []

    class FailingKisMarketData:
        async def fetch_stock_investor_daily(self, symbol, days):
            raise RuntimeError("KIS 일별 수급 일시 오류")

    detector = SupplyTrendDetector(kis_market_data=FailingKisMarketData())

    async def universe():
        return {"005930": "삼성전자"}

    def unexpected_daily_fallback(universe):
        fallback_calls.append(universe)
        return []

    detector._build_universe = universe
    monkeypatch.setattr(
        detector,
        "_fallback_daily_only",
        unexpected_daily_fallback,
    )

    assert asyncio.run(detector.detect_accumulation()) == []
    assert fallback_calls == []
