"""Contract tests on scheduled production entry points, without running loops."""
import ast
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from src.schedulers.kr_scheduler import KRScheduler


@pytest.mark.asyncio
async def test_expert_collection_stores_quality_without_channel_or_dm(monkeypatch):
    from src.utils import telegram
    notifier = SimpleNamespace(record_report=AsyncMock(return_value=True), send_report=AsyncMock(), send_message=AsyncMock(), send_alert=AsyncMock())
    monkeypatch.setattr(telegram, 'get_telegram_notifier', lambda: notifier)
    scheduler = object.__new__(KRScheduler)
    scheduler.bot = SimpleNamespace()
    await scheduler._record_expert_briefing('morning', {}, 0)
    notifier.record_report.assert_awaited_once()
    notifier.send_report.assert_not_awaited()
    notifier.send_message.assert_not_awaited()
    notifier.send_alert.assert_not_awaited()


def test_scheduled_producers_do_not_directly_send_candidate_or_us_reports():
    path = Path(__file__).parents[1]/'src/schedulers/kr_scheduler.py'
    tree = ast.parse(path.read_text())
    method = next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == 'run_daily_report_scheduler')
    calls = [n for n in ast.walk(method) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr in {'generate_morning_report','generate_evening_report','generate_us_market_report'}]
    assert len(calls) == 3
    for call in calls:
        value = next(k.value for k in call.keywords if k.arg == 'send_telegram')
        assert isinstance(value, ast.Constant) and value.value is False
