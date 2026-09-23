"""호출자 태그가 실제 async 진입점에서 브로커까지 전달되는지 검증."""

import asyncio
from types import SimpleNamespace

from src.utils import kis_request_metrics as metrics
from test_sync_portfolio_characterization import _make, _pos, _fill_check_once


def test_sync_and_consistency_retry_sources_are_distinct(monkeypatch):
    sched, bot, _ = _make(monkeypatch, bot_positions=[_pos("005930")],
                         balance={"stock_value": 100000}, kis_seq=[])
    seen = []

    async def balance():
        seen.append(("balance", metrics._source.get()))
        return {"stock_value": 100000}

    async def positions():
        seen.append(("positions", metrics._source.get()))
        return {}

    bot.broker.get_account_balance = balance
    bot.broker.get_positions = positions
    asyncio.run(sched._sync_portfolio())
    assert seen == [("balance", "portfolio_sync"), ("positions", "portfolio_sync"),
                    ("positions", "portfolio_sync_consistency_retry")]
    assert metrics._source.get() == "unknown"


def test_fill_loop_source_is_not_sync(monkeypatch):
    sched, bot, sleeps = _make(monkeypatch, bot_positions=[], balance={}, kis_seq=[])
    seen = []

    async def fills():
        seen.append(metrics._source.get())
        return []

    bot.broker.fills_seq = [["pending"]]
    bot.broker.check_fills = fills
    _fill_check_once(monkeypatch, sched, sleeps)
    assert seen == ["fill_check"]
    assert metrics._source.get() == "unknown"


def test_batch_guard_tags_only_portfolio_probe():
    from src.core.batch_analyzer import BatchAnalyzer
    seen = []

    async def positions():
        seen.append(metrics._source.get())
        return {}

    analyzer = object.__new__(BatchAnalyzer)
    analyzer._engine = SimpleNamespace(portfolio=SimpleNamespace(positions={}))
    analyzer._broker = SimpleNamespace(get_positions=positions)
    analyzer._load_json = lambda: []
    asyncio.run(analyzer.execute_pending_signals())
    assert seen == ["batch_guard"]
    assert metrics._source.get() == "unknown"


def test_startup_position_load_keeps_scope_without_starting_bot():
    from scripts.run_trader import UnifiedTradingBot
    seen = []

    async def positions():
        seen.append(metrics._source.get())
        return {}

    bot = object.__new__(UnifiedTradingBot)
    bot.broker = SimpleNamespace(get_positions=positions)
    asyncio.run(bot._load_existing_positions())
    assert seen == ["startup"]
    assert metrics._source.get() == "unknown"
