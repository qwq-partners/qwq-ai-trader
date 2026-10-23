"""보고·시장자료 HTTP 세션은 외부 요청 없이 실제 객체로 종료를 검증한다."""
import asyncio
from types import SimpleNamespace

import aiohttp
import pytest

from scripts.run_trader import UnifiedTradingBot
from src.analytics import llm_decision_record
from src.data.providers import us_market_data
from src.data.providers.kis_market_data import KISMarketData
from src.experts.news_curator import NewsCurator
from src.experts.orchestrator import ExpertOrchestrator
from src.signals.screener.kr_screener import StockScreener
from src.signals.sentiment.kr_theme_detector import NewsCollector, ThemeDetector
from src.utils import llm, telegram


@pytest.fixture
def bot(monkeypatch):
    bot = object.__new__(UnifiedTradingBot)
    bot.broker = bot._us_engine = bot.dashboard = bot.ws_feed = None
    bot._token_manager = None
    bot._entry_observation_runtime = None

    async def no_llm():
        return None

    async def no_recorder(*, timeout):
        return True

    monkeypatch.setattr(llm, "close_llm_manager", no_llm)
    monkeypatch.setattr(llm_decision_record, "close_decision_recorder", no_recorder)
    monkeypatch.setattr(telegram, "_notifier", None)
    monkeypatch.setattr(us_market_data, "_us_market_data", None)
    return bot


def attach_owner(bot, owner_name, session, monkeypatch):
    classes = {
        "screener": StockScreener,
        "kis_market_data": KISMarketData,
        "theme_news": NewsCollector,
        "expert_news": NewsCurator,
        "us_market_data": us_market_data.USMarketData,
        "telegram": telegram.TelegramNotifier,
    }
    owner = object.__new__(classes[owner_name])
    owner._session = session
    if owner_name == "theme_news":
        bot.theme_detector = object.__new__(ThemeDetector)
        bot.theme_detector.news_collector = owner
    elif owner_name == "expert_news":
        bot.expert_orchestrator = object.__new__(ExpertOrchestrator)
        bot.expert_orchestrator.agents = {"news_curator": owner}
    elif owner_name == "us_market_data":
        monkeypatch.setattr(us_market_data, "_us_market_data", owner)
    elif owner_name == "telegram":
        monkeypatch.setattr(telegram, "_notifier", owner)
    else:
        setattr(bot, owner_name, owner)
    return owner


@pytest.mark.asyncio
@pytest.mark.parametrize("owner_name", [
    "screener", "kis_market_data", "theme_news", "expert_news",
    "us_market_data", "telegram",
])
async def test_shutdown_closes_existing_http_session(bot, monkeypatch, owner_name):
    session = aiohttp.ClientSession()
    attach_owner(bot, owner_name, session, monkeypatch)
    try:
        await bot.shutdown()
        assert session.closed, f"{owner_name}의 기존 HTTP 세션이 종료되지 않음"
        await bot.shutdown()  # 이미 닫힌 세션·부분 초기화도 안전하다.
        assert session.closed
    finally:
        await session.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["raises", "timeout"])
@pytest.mark.parametrize("failing_owner", [
    "screener", "kis_market_data", "theme_news", "expert_news",
    "us_market_data", "telegram",
])
async def test_one_failed_close_does_not_skip_other_resources(
    bot, monkeypatch, failure, failing_owner,
):
    names = ["screener", "kis_market_data", "theme_news", "expert_news",
             "us_market_data", "telegram"]
    sessions = {name: aiohttp.ClientSession() for name in names}
    owners = {name: attach_owner(bot, name, session, monkeypatch)
              for name, session in sessions.items()}
    attempted = asyncio.Event()

    async def broken_close():
        attempted.set()
        if failure == "raises":
            raise RuntimeError("synthetic close failure")
        await asyncio.Event().wait()

    monkeypatch.setattr(owners[failing_owner], "close", broken_close)
    real_wait_for = asyncio.wait_for

    async def shortened_wait_for(awaitable, timeout):
        return await real_wait_for(awaitable, 0.01 if timeout == 2.0 else timeout)

    monkeypatch.setattr(asyncio, "wait_for", shortened_wait_for)
    token_closed = []

    async def close_token():
        token_closed.append(True)

    bot._token_manager = SimpleNamespace(close=close_token)
    try:
        await real_wait_for(bot.shutdown(), timeout=1.0)
        assert attempted.is_set()
        assert token_closed == [True]
        assert all(session.closed for name, session in sessions.items()
                   if name != failing_owner)
    finally:
        for session in sessions.values():
            await session.close()


@pytest.mark.asyncio
async def test_shutdown_does_not_create_unused_singletons(bot, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("종료 중 새 HTTP 소유자를 생성함")

    monkeypatch.setattr(telegram, "TelegramNotifier", forbidden)
    monkeypatch.setattr(us_market_data, "USMarketData", forbidden)
    await bot.shutdown()
    assert telegram._notifier is None
    assert us_market_data._us_market_data is None


@pytest.mark.asyncio
async def test_report_sessions_close_after_last_llm_record_before_token(bot, monkeypatch):
    session = aiohttp.ClientSession()
    attach_owner(bot, "telegram", session, monkeypatch)
    events = []

    async def drain_llm():
        assert not session.closed
        events.append("llm")

    async def drain_recorder(*, timeout):
        assert events == ["llm"] and not session.closed
        events.append("recorder")
        return True

    async def close_token():
        events.append("token")
        assert session.closed

    monkeypatch.setattr(llm, "close_llm_manager", drain_llm)
    monkeypatch.setattr(llm_decision_record, "close_decision_recorder", drain_recorder)
    bot._token_manager = SimpleNamespace(close=close_token)
    try:
        await bot.shutdown()
        assert events == ["llm", "recorder", "token"]
        assert session.closed
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_run_stop_drains_task_before_closing_report_session(bot, monkeypatch):
    session = aiohttp.ClientSession()
    attach_owner(bot, "telegram", session, monkeypatch)
    entered = asyncio.Event()
    drained = []

    async def engine_run():
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            drained.append(not session.closed)

    async def initialized():
        return True

    bot.market = "kr"
    bot.dry_run = False
    bot._entry_observation_plan = None
    bot.engine = SimpleNamespace(run=engine_run, stop=lambda: None,
                                 get_context=lambda market: None)
    bot.initialize = initialized
    monkeypatch.setattr(llm, "get_budget_monitor_manager", lambda: None)
    task = asyncio.create_task(bot.run())
    try:
        await asyncio.wait_for(entered.wait(), timeout=1.0)
        bot.stop()
        await asyncio.wait_for(task, timeout=1.0)
        assert drained == [True]
        assert session.closed
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await session.close()
