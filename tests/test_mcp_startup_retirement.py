"""폐기한 보조 서버를 부팅하지 않고 기존 시장·공시 초기화는 유지한다."""

import asyncio
import sys
from decimal import Decimal
from types import ModuleType, SimpleNamespace

import pytest

from scripts import run_trader
from src.data.providers import kis_market_data
from src.signals.fundamentals import stock_validator
from src.signals.sentiment import kr_theme_detector


def test_kr_startup_keeps_direct_providers_without_starting_auxiliary_servers(monkeypatch):
    """SDK가 우연히 설치돼도 보조 서버를 시작해 신호 가산을 켜지 않는다.

    실제 KR 초기화의 공급자 배선을 실행한다. 외부 I/O만 대체하고 전략 등록 직전
    중단하므로 브로커·주문·스케줄러를 시작하는 전체 봇 실행 테스트는 아니다.
    """
    events = []

    async def holidays(_month):
        events.append("kis_holidays")
        return set()

    async def validator_initialize():
        events.append("direct_validator")

    async def auxiliary_initialize():
        events.append("auxiliary_servers")

    mcp_module = ModuleType("src.utils.mcp_client")
    mcp_module.get_mcp_manager = lambda: SimpleNamespace(initialize=auxiliary_initialize)
    monkeypatch.setitem(sys.modules, "src.utils.mcp_client", mcp_module)
    market_data = SimpleNamespace(fetch_holidays=holidays)
    validator = SimpleNamespace(initialize=validator_initialize)
    monkeypatch.setattr(kis_market_data, "get_kis_market_data", lambda: market_data)
    monkeypatch.setattr(stock_validator, "get_stock_validator", lambda: validator)
    monkeypatch.setattr(kr_theme_detector, "ThemeDetector", lambda **kwargs: SimpleNamespace())
    monkeypatch.setattr(kr_theme_detector, "_theme_detector", None)

    class ReachedStrategies(BaseException):
        pass

    def stop_before_strategy_registration(_engine):
        raise ReachedStrategies

    monkeypatch.setattr(run_trader, "StrategyManager", stop_before_strategy_registration)

    class Config(dict):
        trading = SimpleNamespace(initial_capital=Decimal("10000000"))

    bot = object.__new__(run_trader.UnifiedTradingBot)
    bot.config = Config(kr={"stock_master": {"enabled": False}})
    bot.dry_run = True
    bot.engine = SimpleNamespace(portfolio=SimpleNamespace())
    bot.stock_master = None
    with pytest.raises(ReachedStrategies):
        asyncio.run(bot._initialize_kr())

    assert events == ["kis_holidays", "kis_holidays", "direct_validator"]
    assert bot.kis_market_data is market_data
    assert bot._stock_validator is validator
