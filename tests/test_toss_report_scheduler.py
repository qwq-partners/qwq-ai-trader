"""The scheduler owns the report helper lifetime and callback binding."""
import asyncio
import os
import sys
from types import ModuleType, SimpleNamespace

import pytest


@pytest.mark.asyncio
async def test_enabled_report_helper_binds_callbacks_and_closes(monkeypatch, tmp_path):
    from src.schedulers import kr_scheduler as module

    created = []

    class Helper:
        def __init__(self, *, enabled):
            self.enabled = enabled
            self.closed = False
            created.append(self)

        async def prepare_report(self, cohort):
            return True

        def get_report_quote(self, symbol, *, cohort=None):
            return cohort

        async def close(self):
            self.closed = True

    fake_helper = ModuleType("src.data.providers.toss.report_helper")
    fake_helper.ReportHelperClient = Helper
    monkeypatch.setitem(sys.modules, fake_helper.__name__, fake_helper)
    monkeypatch.setenv("TOSS_REPORT_API", "1")
    monkeypatch.setattr(module, "Path", SimpleNamespace(home=lambda: tmp_path))
    reporter = SimpleNamespace()
    bot = SimpleNamespace(report_generator=reporter, running=False,
                          config=SimpleNamespace(get=lambda *_: {}))
    scheduler = object.__new__(module.KRScheduler)
    scheduler.bot = bot

    await scheduler.run_daily_report_scheduler()
    await scheduler.run_daily_report_scheduler()

    assert len(created) == 1
    assert created[0].enabled() is False
    assert reporter.toss_report_prepare.__self__ is created[0]
    assert bot._toss_report_helper_client is created[0]
    assert created[0].closed is True


@pytest.mark.asyncio
async def test_enabled_helper_reads_only_the_reporters_current_valid_cohort(monkeypatch, tmp_path):
    from src.schedulers import kr_scheduler as module

    calls = []

    class Helper:
        def __init__(self, *, enabled):
            pass

        async def prepare_report(self, cohort):
            return True

        def get_report_quote(self, symbol, *, cohort=None):
            calls.append((symbol, cohort))
            return cohort

        async def close(self):
            pass

    class Reporter:
        def __init__(self):
            self.current = object()

        def _valid_report_cohort(self, _day):
            return self.current

    fake_helper = ModuleType("src.data.providers.toss.report_helper")
    fake_helper.ReportHelperClient = Helper
    monkeypatch.setitem(sys.modules, fake_helper.__name__, fake_helper)
    monkeypatch.setenv("TOSS_REPORT_API", "1")
    monkeypatch.setattr(module, "Path", SimpleNamespace(home=lambda: tmp_path))
    reporter = Reporter()
    bot = SimpleNamespace(report_generator=reporter, running=False,
                          config=SimpleNamespace(get=lambda *_: {}))
    scheduler = object.__new__(module.KRScheduler)
    scheduler.bot = bot

    await scheduler.run_daily_report_scheduler()
    valid = reporter.current
    assert reporter.toss_quote_provider("005930") is valid
    reporter.current = None
    assert reporter.toss_quote_provider("005930") is None
    assert calls == [("005930", valid)]


@pytest.mark.asyncio
async def test_disabled_report_helper_keeps_legacy_supervisor_binding(monkeypatch, tmp_path):
    from src.schedulers import kr_scheduler as module

    monkeypatch.delenv("TOSS_REPORT_API", raising=False)
    monkeypatch.setattr(module, "Path", SimpleNamespace(home=lambda: tmp_path))
    reporter = SimpleNamespace()
    bot = SimpleNamespace(report_generator=reporter, running=False,
                          config=SimpleNamespace(get=lambda *_: {}),
                          _toss_shadow_supervisor=SimpleNamespace(
                              get_report_quote=lambda symbol: "legacy:" + symbol))
    scheduler = object.__new__(module.KRScheduler)
    scheduler.bot = bot

    await scheduler.run_daily_report_scheduler()
    assert not hasattr(reporter, "toss_report_prepare")
    assert reporter.toss_quote_provider("005930") == "legacy:005930"


@pytest.mark.asyncio
async def test_off_after_helper_attempt_never_restores_legacy_periodic_quote(monkeypatch, tmp_path):
    from src.schedulers import kr_scheduler as module

    class ClosedHelper:
        def __init__(self):
            self.close_calls = 0

        async def close(self):
            self.close_calls += 1

    helper = ClosedHelper()
    monkeypatch.setenv("TOSS_REPORT_API", "0")
    monkeypatch.setattr(module, "Path", SimpleNamespace(home=lambda: tmp_path))
    reporter = SimpleNamespace(toss_report_prepare=object())
    bot = SimpleNamespace(
        report_generator=reporter,
        running=False,
        config=SimpleNamespace(get=lambda *_: {}),
        _toss_report_helper_client=helper,
        _toss_shadow_supervisor=SimpleNamespace(get_report_quote=lambda symbol: "old-periodic:" + symbol),
    )
    scheduler = object.__new__(module.KRScheduler)
    scheduler.bot = bot

    await scheduler.run_daily_report_scheduler()
    assert reporter.toss_report_prepare is None
    assert reporter.toss_quote_provider("005930") is None
    assert helper.close_calls == 1
