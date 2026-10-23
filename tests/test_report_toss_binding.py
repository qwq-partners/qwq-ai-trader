"""관측 supervisor가 보고 스케줄러보다 늦게 준비돼도 현재 공급자를 읽는다."""
from types import SimpleNamespace

import pytest


@pytest.mark.asyncio
async def test_report_binding_follows_late_supervisor_and_removal(tmp_path, monkeypatch):
    from src.schedulers import kr_scheduler as module
    reporter = SimpleNamespace()
    bot = SimpleNamespace(report_generator=reporter, running=False,
                          config=SimpleNamespace(get=lambda *_: {}))
    scheduler = object.__new__(module.KRScheduler)
    scheduler.bot = bot
    monkeypatch.setattr(module, "Path", SimpleNamespace(home=lambda: tmp_path))
    await scheduler.run_daily_report_scheduler()
    assert callable(getattr(reporter, "toss_quote_provider", None))
    assert reporter.toss_quote_provider("005930") is None
    received = []

    def read(symbol):
        received.append(symbol)
        return "synthetic quote"

    bot._toss_shadow_supervisor = SimpleNamespace(get_report_quote=read)
    assert reporter.toss_quote_provider("005930") == "synthetic quote"
    assert received == ["005930"]
    del bot._toss_shadow_supervisor
    assert reporter.toss_quote_provider("005930") is None
