"""호출 합산, 전체 시간 예산, 송신자 수명 잠금을 검증한다."""

import asyncio
import importlib
import os
import subprocess
import sys

import pytest

from test_toss_client_boundary import api, make_client


class Clock:
    def __init__(self):
        self.now = 100.0
        self.waits = []
    def __call__(self):
        return self.now
    async def sleep(self, seconds):
        self.waits.append(seconds)
        self.now += seconds
        await asyncio.sleep(0)


@pytest.mark.parametrize("value", [0, -1, True, float("nan"), float("inf"), "2"])
def test_budget_rejects_invalid_duration(value):
    with pytest.raises(ValueError):
        api().RequestBudget(value)


def test_budget_deadline_and_retry_page_counters_are_shared():
    mod, clock = api(), Clock()
    budget = mod.RequestBudget(5, clock=clock, max_retries=1, max_pages=2)
    assert budget.deadline == 105
    clock.now = 102
    assert budget.remaining() == 3
    budget.consume_retry()
    with pytest.raises(mod.TossRequestError, match="retry_exhausted"):
        budget.consume_retry()
    budget.consume_page()
    budget.consume_page()
    with pytest.raises(mod.TossRequestError, match="page_exhausted"):
        budget.consume_page()
    clock.now = 105
    with pytest.raises(mod.TossRequestError, match="timeout"):
        budget.remaining()


def limiter(clock, rate=1):
    api()
    mod = importlib.import_module("src.data.providers.toss.rate_limit")
    return mod.GroupRateLimiter(limits={
        "MARKET_DATA": rate, "MARKET_DATA_CHART": rate, "MARKET_INFO": rate},
        clock=clock, sleep=clock.sleep)


def test_shared_bucket_waits_and_other_group_does_not():
    clock, mod = Clock(), api()
    gate = limiter(clock)
    async def run():
        budget = mod.RequestBudget(5, clock=clock)
        await gate.acquire("MARKET_DATA", budget)
        await gate.acquire("MARKET_INFO", budget)
        assert clock.waits == []
        await asyncio.gather(gate.acquire("MARKET_DATA", budget),
                             gate.acquire("MARKET_DATA", budget))
    asyncio.run(run())
    assert clock.waits == [1, 1]


def test_fractional_rate_below_one_is_rejected_instead_of_waiting_forever():
    api()
    mod = importlib.import_module("src.data.providers.toss.rate_limit")
    with pytest.raises(ValueError):
        mod.GroupRateLimiter(limits={"MARKET_DATA": 0.5, "MARKET_DATA_CHART": 1, "MARKET_INFO": 1})


def test_external_close_cancels_request_without_deadlocking_context_exit(tmp_path):
    mod = api()
    client, _, transport = make_client(tmp_path, enabled=True, role="sender")
    entered = asyncio.Event()
    async def blocked(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()
    transport.request = blocked
    async def run():
        async def worker():
            async with client:
                await client.get("/api/v1/prices", params={"symbols": "005930"},
                                 budget=mod.RequestBudget(3))
        task = asyncio.create_task(worker())
        await entered.wait()
        await asyncio.wait_for(asyncio.shield(client.close()), 0.1)
        with pytest.raises(asyncio.CancelledError):
            await task
    asyncio.run(run())
    assert transport.closed


def test_second_process_cannot_start_sender_while_lifetime_lock_is_held(tmp_path):
    mod = api()
    client, _, _ = make_client(tmp_path, enabled=True, role="sender")
    script = """
import asyncio, sys
from pathlib import Path
from src.data.providers.toss.client import TossClient, TossRequestError
client = TossClient(transport=None, tokens=None, limiter=None, enabled=True, role='sender',
    sender_lock_path=Path(sys.argv[1]), circuit_failure_threshold=2, circuit_open_seconds=5)
try:
    asyncio.run(client.start())
except TossRequestError as exc:
    print(exc.code)
else:
    raise SystemExit('unexpected_sender')
"""
    async def run():
        async with client:
            result = await asyncio.to_thread(subprocess.run, [sys.executable, "-c", script,
                str(tmp_path / "sender.lock")], capture_output=True, text=True, timeout=5)
            assert result.returncode == 0 and result.stdout.strip() == "sender_busy"
    asyncio.run(run())


def test_shared_budget_consumes_each_page_once_but_retries_only_once(tmp_path):
    mod = api()
    client, _, transport = make_client(tmp_path, enabled=True, role="sender", responses=[
        mod.HttpResponse(500, {}, {}), mod.HttpResponse(200, {}, {"result": []}),
        mod.HttpResponse(200, {}, {"result": []})])
    async def run():
        async with client:
            budget = mod.RequestBudget(1, max_pages=2)
            for _ in range(2):
                await client.get("/api/v1/prices", params={"symbols": "005930"}, budget=budget)
            with pytest.raises(mod.TossRequestError, match="page_exhausted"):
                await client.get("/api/v1/prices", params={"symbols": "005930"}, budget=budget)
    asyncio.run(run())
    assert len(transport.requests) == 3


def test_sender_rejects_wrong_owner_without_touching_tokens(tmp_path, monkeypatch):
    mod = api()
    client, tokens, transport = make_client(tmp_path, enabled=True, role="sender")
    monkeypatch.setattr(os, "getuid", lambda: 999999)
    with pytest.raises(mod.TossRequestError, match="unsafe_lock"):
        asyncio.run(client.start())
    assert tokens.calls == transport.requests == []


def test_reset_is_relative_seconds_and_retry_after_wins():
    clock, mod = Clock(), api()
    gate = limiter(clock, rate=4)
    async def run():
        await gate.observe("MARKET_DATA", {"X-RateLimit-Remaining": "0",
            "X-RateLimit-Reset": "1"}, status=200)
        await gate.acquire("MARKET_DATA", mod.RequestBudget(5, clock=clock))
        assert clock.now == 101
        await gate.observe("MARKET_DATA", {"Retry-After": "2",
            "X-RateLimit-Reset": "9"}, status=429)
        await gate.acquire("MARKET_DATA", mod.RequestBudget(5, clock=clock))
        assert clock.now == 103
    asyncio.run(run())


def test_limit_only_decreases_and_huge_hold_fails_without_early_retry():
    clock, mod = Clock(), api()
    gate = limiter(clock, rate=4)
    async def run():
        await gate.observe("MARKET_DATA", {"X-RateLimit-Limit": "1"}, status=200)
        await gate.observe("MARKET_DATA", {"X-RateLimit-Limit": "999"}, status=200)
        await gate.observe("MARKET_DATA", {"X-RateLimit-Limit": "nan"}, status=200)
        await gate.acquire("MARKET_DATA", mod.RequestBudget(5, clock=clock))
        await gate.acquire("MARKET_DATA", mod.RequestBudget(5, clock=clock))
        assert clock.waits == [1]
        await gate.observe("MARKET_DATA", {"Retry-After": "3600"}, status=429)
        with pytest.raises(mod.TossRequestError, match="timeout"):
            await gate.acquire("MARKET_DATA", mod.RequestBudget(1, clock=clock))
        await gate.acquire("MARKET_INFO", mod.RequestBudget(1, clock=clock))
    asyncio.run(run())
    assert clock.waits == [1]


def test_401_then_429_cannot_get_a_second_retry(tmp_path):
    mod = api()
    client, tokens, transport = make_client(tmp_path, enabled=True, role="sender", responses=[
        mod.HttpResponse(401, {}, {"error": {"code": "token-revoked"}}),
        mod.HttpResponse(429, {"Retry-After": "3600"}, {"error": {"code": "rate-limit"}})])
    async def run():
        async with client:
            with pytest.raises(mod.TossRequestError, match="retry_exhausted"):
                await client.get("/api/v1/prices", params={"symbols": "005930"},
                                 budget=mod.RequestBudget(1))
    asyncio.run(run())
    assert len(transport.requests) == 2
    assert [call[0] for call in tokens.calls] == ["get", "token-revoked"]


def test_sender_lock_blocks_second_client_and_releases_without_unlink(tmp_path):
    mod = api()
    first, _, _ = make_client(tmp_path, enabled=True, role="sender")
    second, tokens, transport = make_client(tmp_path, enabled=True, role="sender")
    async def run():
        async with first:
            inode = (tmp_path / "sender.lock").stat().st_ino
            with pytest.raises(mod.TossRequestError, match="sender_busy"):
                await second.start()
            assert tokens.calls == transport.requests == []
        async with second:
            assert (tmp_path / "sender.lock").stat().st_ino == inode
    asyncio.run(run())
    assert (tmp_path / "sender.lock").stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("unsafe", ["mode", "symlink"])
def test_unsafe_sender_file_is_rejected(tmp_path, unsafe):
    mod = api()
    path = tmp_path / "sender.lock"
    if unsafe == "mode":
        path.touch(mode=0o644)
    else:
        (tmp_path / "other").touch(mode=0o600)
        path.symlink_to(tmp_path / "other")
    client, tokens, transport = make_client(tmp_path, enabled=True, role="sender")
    with pytest.raises(mod.TossRequestError, match="unsafe_lock"):
        asyncio.run(client.start())
    assert tokens.calls == transport.requests == []


def test_token_wait_is_bounded_by_total_timeout_and_loop_keeps_running(tmp_path):
    mod = api()
    client, _, transport = make_client(tmp_path, enabled=True, role="sender")
    ticks = []
    async def blocked(**kwargs):
        await asyncio.Event().wait()
    client.tokens.get_token = blocked
    async def run():
        async def beat():
            await asyncio.sleep(0.001)
            ticks.append(True)
        async with client:
            beat_task = asyncio.create_task(beat())
            with pytest.raises(mod.TossRequestError, match="timeout"):
                await client.get("/api/v1/prices", params={"symbols": "005930"},
                                 budget=mod.RequestBudget(0.02))
            await beat_task
    asyncio.run(run())
    assert ticks and transport.requests == [] and transport.closed


def test_cancellation_closes_and_releases_sender_lock(tmp_path):
    mod = api()
    client, _, transport = make_client(tmp_path, enabled=True, role="sender")
    ready = asyncio.Event()
    async def blocked(*args, **kwargs):
        ready.set()
        await asyncio.Event().wait()
    transport.request = blocked
    async def run():
        async def worker():
            async with client:
                await client.get("/api/v1/prices", params={"symbols": "005930"},
                                 budget=mod.RequestBudget(1))
        task = asyncio.create_task(worker())
        await ready.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        replacement, _, _ = make_client(tmp_path, enabled=True, role="sender")
        async with replacement:
            pass
    asyncio.run(run())
    assert transport.closed


def test_circuit_opens_per_group_and_allows_one_half_open_probe(tmp_path):
    mod, clock = api(), Clock()
    client, _, transport = make_client(tmp_path, enabled=True, role="sender", clock=clock,
        responses=[mod.HttpResponse(500, {}, {})] * 2)
    async def fetch():
        return await client.get("/api/v1/prices", params={"symbols": "005930"},
                                budget=mod.RequestBudget(2, clock=clock, max_retries=0))
    async def run():
        async with client:
            for _ in range(2):
                with pytest.raises(mod.TossRequestError):
                    await fetch()
            with pytest.raises(mod.TossRequestError, match="circuit_open"):
                await fetch()
            assert len(transport.requests) == 2
            clock.now += 5
            ready, release = asyncio.Event(), asyncio.Event()
            async def probe(*args, **kwargs):
                ready.set()
                await release.wait()
                return mod.HttpResponse(200, {}, {"result": []})
            transport.request = probe
            task = asyncio.create_task(fetch())
            await ready.wait()
            with pytest.raises(mod.TossRequestError, match="circuit_open"):
                await fetch()
            release.set()
            await task
            assert client.health["MARKET_DATA"]["last_success"] == 105
            assert client.health["MARKET_DATA"]["failures"] == 0
    asyncio.run(run())


def test_reset_releases_only_one_token_not_full_bucket():
    mod, clock = api(), Clock()
    gate = limiter(clock, rate=4)
    async def run():
        await gate.observe("MARKET_DATA", {"X-RateLimit-Remaining": "0",
            "X-RateLimit-Reset": "1"}, status=200)
        budget = mod.RequestBudget(5, clock=clock)
        await gate.acquire("MARKET_DATA", budget)
        await gate.acquire("MARKET_DATA", budget)
    asyncio.run(run())
    assert clock.waits == [1, 0.25]


@pytest.mark.parametrize("headers", [{}, {"Retry-After": "nan", "X-RateLimit-Reset": "bad"}])
def test_missing_or_corrupt_429_headers_use_bounded_backoff(headers):
    mod, clock = api(), Clock()
    gate = limiter(clock, rate=4)
    async def run():
        await gate.observe("MARKET_DATA", headers, status=429)
        await gate.acquire("MARKET_DATA", mod.RequestBudget(5, clock=clock))
    asyncio.run(run())
    assert 0.25 <= sum(clock.waits) <= 0.500001


@pytest.mark.parametrize("half_open,old_result", [
    (False, "success"), (True, "success"), (True, "failure"), (True, "cancel"),
])
def test_old_inflight_result_cannot_change_new_circuit_epoch(tmp_path, half_open, old_result):
    mod, clock = api(), Clock()
    client, _, transport = make_client(tmp_path, enabled=True, role="sender", clock=clock)
    entered, old_release, probe_entered, probe_release = [asyncio.Event() for _ in range(4)]
    calls = []
    async def response(*args, **kwargs):
        index = len(calls)
        calls.append(index)
        if index == 0:
            entered.set()
            await old_release.wait()
            status = 500 if old_result == "failure" else 200
            return mod.HttpResponse(status, {}, {"result": []})
        if index in (1, 2):
            return mod.HttpResponse(500, {}, {})
        if index == 3 and half_open:
            probe_entered.set()
            await probe_release.wait()
        return mod.HttpResponse(200, {}, {"result": []})
    transport.request = response
    async def fetch():
        return await client.get("/api/v1/prices", params={"symbols": "005930"},
                               budget=mod.RequestBudget(30, clock=clock, max_retries=0))
    async def run():
        async with client:
            old = asyncio.create_task(fetch())
            await entered.wait()
            for _ in range(2):
                with pytest.raises(mod.TossRequestError):
                    await fetch()
            probe = None
            if half_open:
                clock.now += 5
                probe = asyncio.create_task(fetch())
                await probe_entered.wait()
            try:
                if old_result == "cancel":
                    old.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await old
                else:
                    old_release.set()
                    if old_result == "failure":
                        with pytest.raises(mod.TossRequestError):
                            await old
                    else:
                        await old
                assert client.health["MARKET_DATA"]["failures"] == 2
                with pytest.raises(mod.TossRequestError, match="circuit_open"):
                    await fetch()
                assert len(calls) == (4 if half_open else 3)
            finally:
                probe_release.set()
                if probe is not None:
                    await probe
            if half_open:
                assert client.health["MARKET_DATA"]["failures"] == 0
                await fetch()
                assert len(calls) == 5
    asyncio.run(run())


def test_repeated_close_cancellation_keeps_session_and_lock_until_cleanup(tmp_path):
    from test_toss_client_boundary import FakeSession
    mod = api()
    transport_mod = importlib.import_module("src.data.providers.toss.transport")
    close_entered, close_release = asyncio.Event(), asyncio.Event()
    class Session(FakeSession):
        async def close(self):
            close_entered.set()
            await close_release.wait()
            self.closed = True
    session = Session()
    client, _, _ = make_client(tmp_path, enabled=True, role="sender")
    client.transport = transport_mod.AiohttpTransport(session_factory=lambda: session)
    async def run():
        await client.get("/api/v1/prices", params={"symbols": "005930"}, budget=mod.RequestBudget(1))
        closing = asyncio.create_task(client.close())
        await close_entered.wait()
        retry = None
        try:
            for _ in range(2):
                closing.cancel()
                await asyncio.sleep(0)
            await asyncio.sleep(0)
            retry = asyncio.create_task(client.close())
            await asyncio.sleep(0)
            other, _, _ = make_client(tmp_path, enabled=True, role="sender")
            with pytest.raises(mod.TossRequestError, match="sender_busy"):
                await other.start()
            assert not session.closed
        finally:
            close_release.set()
            await asyncio.gather(closing, *([retry] if retry is not None else []), return_exceptions=True)
        assert session.closed
        assert closing.cancelled()
        assert retry is not None and retry.exception() is None
        async with other:
            pass
    asyncio.run(run())


def test_failed_session_close_retains_sender_ownership_until_successful_retry(tmp_path):
    from test_toss_client_boundary import FakeSession
    mod = api()
    transport_mod = importlib.import_module("src.data.providers.toss.transport")
    class Session(FakeSession):
        attempts = 0
        async def close(self):
            self.attempts += 1
            if self.attempts == 1:
                raise RuntimeError("synthetic-close-error")
            self.closed = True
    session = Session()
    client, _, _ = make_client(tmp_path, enabled=True, role="sender")
    client.transport = transport_mod.AiohttpTransport(session_factory=lambda: session)
    async def run():
        await client.get("/api/v1/prices", params={"symbols": "005930"}, budget=mod.RequestBudget(1))
        try:
            with pytest.raises(mod.TossRequestError, match="network_error"):
                await client.close()
            other, _, _ = make_client(tmp_path, enabled=True, role="sender")
            with pytest.raises(mod.TossRequestError, match="sender_busy"):
                await other.start()
            assert not session.closed
        finally:
            await client.close()
        assert session.closed and session.attempts == 2
        async with other:
            pass
    asyncio.run(run())
