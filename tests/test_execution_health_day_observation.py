"""health 응답 안의 일자 admission 관측은 한 번만 평가한다."""
import asyncio
from copy import deepcopy
from datetime import datetime, timezone

import pytest

from src.execution.safety.economics import encode_portfolio
from src.execution.safety.protection import encode_protection
from tests.test_execution_runtime import setup


class HealthOnlyProducer:
    def __init__(self, events):
        self.events = events

    def health(self):
        self.events.append("producer_health")
        return {"synthetic": True}


class ScriptedClock:
    def __init__(self, values, events=None):
        self.values = iter(values)
        self.calls = 0
        self.events = events

    def __call__(self):
        self.calls += 1
        if self.events is not None:
            self.events.append("clock")
        return next(self.values)


def runtime_snapshot(engine, exits, runtime):
    return (runtime.owner.version, runtime.owner.published_version,
            deepcopy(runtime.owner.state), encode_portfolio(engine.portfolio),
            encode_protection(exits), engine._execution_version)


@pytest.fixture
def install_clock():
    originals = []

    def install(runtime, clock):
        originals.append((runtime, runtime.clock))
        runtime.clock = clock

    yield install

    for runtime, original_clock in reversed(originals):
        runtime.clock = original_clock


def test_health_observes_day_admission_once_then_refreshes_next_request(tmp_path, install_clock):
    async def scenario():
        engine, exits, store, runtime = await setup(tmp_path, account_scope="scope")
        events = []
        scripted = ScriptedClock((
            datetime(2026, 9, 18, 14, 59, 59, tzinfo=timezone.utc),
            datetime(2026, 9, 18, 15, 0, 0, tzinfo=timezone.utc),
            datetime(2026, 9, 18, 15, 0, 1, tzinfo=timezone.utc),
        ), events)
        install_clock(runtime, scripted)
        runtime._protection_producer = HealthOnlyProducer(events)
        before = runtime_snapshot(engine, exits, runtime)
        try:
            first = runtime.health()
            assert (first["day_admission_closed"],
                    first["reconciler"]["day_admission_closed"],
                    scripted.calls) == (False, False, 1)
            assert events == ["producer_health", "clock"]

            second = runtime.health()
            assert (second["day_admission_closed"],
                    second["reconciler"]["day_admission_closed"],
                    scripted.calls) == (True, True, 2)
            assert runtime.day_admission_closed is True
            assert scripted.calls == 3
            assert runtime.trading_ready is False
            assert runtime_snapshot(engine, exits, runtime) == before
        finally:
            await engine._shutdown()
            await store.close()
    asyncio.run(scenario())


def test_health_day_short_circuit_does_not_read_clock_or_write_state(tmp_path, monkeypatch, install_clock):
    async def scenario():
        engine, exits, store, runtime = await setup(tmp_path, account_scope="scope")
        calls = []
        runtime._day_closed = True
        install_clock(runtime, lambda: (_ for _ in ()).throw(AssertionError("clock must short-circuit")))
        runtime._protection_producer = HealthOnlyProducer(calls)
        before = runtime_snapshot(engine, exits, runtime)

        writes = []

        def forbidden(*_args, **_kwargs):
            writes.append("write")
            raise AssertionError("health must not write")

        try:
            monkeypatch.setattr(runtime.owner, "mutate", forbidden)
            monkeypatch.setattr(store, "commit", forbidden)
            health = runtime.health()
            assert health["day_admission_closed"] is True
            assert health["reconciler"]["day_admission_closed"] is True
            assert calls == ["producer_health"]
            assert writes == []
            assert runtime_snapshot(engine, exits, runtime) == before
        finally:
            await engine._shutdown()
            await store.close()
    asyncio.run(scenario())


def test_health_keeps_naive_clock_error_and_producer_before_clock(tmp_path, monkeypatch, install_clock):
    async def scenario():
        engine, exits, store, runtime = await setup(tmp_path, account_scope="scope")
        events = []
        scripted = ScriptedClock((datetime(2026, 9, 18, 23, 59, 59),), events)
        install_clock(runtime, scripted)
        runtime._protection_producer = HealthOnlyProducer(events)
        before = runtime_snapshot(engine, exits, runtime)

        writes = []

        def forbidden(*_args, **_kwargs):
            writes.append("write")
            raise AssertionError("health must not write")

        try:
            monkeypatch.setattr(runtime.owner, "mutate", forbidden)
            monkeypatch.setattr(store, "commit", forbidden)
            with pytest.raises(ValueError, match="^실행 시계는 timezone-aware여야 합니다$"):
                runtime.health()
            assert scripted.calls == 1
            assert events == ["producer_health", "clock"]
            assert writes == []
            assert runtime_snapshot(engine, exits, runtime) == before
        finally:
            await engine._shutdown()
            await store.close()
    asyncio.run(scenario())
