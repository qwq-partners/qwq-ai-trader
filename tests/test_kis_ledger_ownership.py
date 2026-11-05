"""실제 브로커 GET·리미터의 소유권 경계. HTTP·시간만 가짜이며 운영 I/O는 없다."""

import asyncio
import collections
from types import SimpleNamespace

import aiohttp
import pytest

from src.execution.broker import kis_kr
from src.utils import kis_rate_limit as limiter


async def _settle():
    # 시간은 전진시키지 않고 준비된 작업만 유한 번 실행한다.
    for _ in range(8):
        await asyncio.sleep(0)


class _Clock:
    def __init__(self):
        self.now = 100.0
        self.sleepers = []

    async def sleep(self, delay):
        future = asyncio.get_running_loop().create_future()
        self.sleepers.append((self.now + delay, future))
        await future

    async def advance(self, now):
        assert now >= self.now
        self.now = now
        pending, self.sleepers = self.sleepers, []
        for deadline, future in pending:
            if future.done():
                continue
            if deadline <= now:
                future.set_result(None)
            else:
                self.sleepers.append((deadline, future))
        await _settle()


class _Response:
    def __init__(self, *, status=200, body=None, ready=False):
        self.status = status
        self.headers = {"tr_cont": "D"}
        self.body = body if body is not None else {"rt_cd": "0"}
        self.headers_ready = asyncio.Event()
        self.body_ready = asyncio.Event()
        self.body_ready.set()
        self.enter_error = None
        self.body_error = None
        if ready:
            self.headers_ready.set()

    async def __aenter__(self):
        await self.headers_ready.wait()
        if self.enter_error:
            raise self.enter_error
        return self

    async def __aexit__(self, *_args):
        return False

    async def json(self):
        await self.body_ready.wait()
        if self.body_error:
            raise self.body_error
        return dict(self.body)


class _Harness:
    def __init__(self, clock):
        self.clock = clock
        self.sent = []
        self.responses = {}
        self.tasks = []
        self.broker = object.__new__(kis_kr.KISBroker)
        self.broker._token = "synthetic-token"
        self.broker._token_mgr = SimpleNamespace(_access_token="synthetic-token")
        self.broker.config = SimpleNamespace(app_key="synthetic", app_secret="synthetic")
        self.broker._session = SimpleNamespace(closed=False, get=self.get)

    def get(self, url, *, headers, params):
        name = params["case"]
        self.sent.append((name, self.clock.now, headers["tr_id"]))
        return self.responses[name].pop(0)

    async def start(self, name, *responses, tr_id="TTTC8001R"):
        self.responses[name] = list(responses) or [_Response()]
        task = asyncio.create_task(self.broker._api_get(
            "http://synthetic.invalid/ledger", tr_id, {"case": name}))
        self.tasks.append(task)
        await _settle()
        return task

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        for task in self.tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)


@pytest.fixture
def harness(monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(limiter, "_calls", collections.deque(maxlen=10))
    monkeypatch.setattr(limiter, "_state", dict(limiter._state))
    limiter.reset()
    monkeypatch.setattr(limiter, "time", SimpleNamespace(monotonic=lambda: clock.now))
    monkeypatch.setattr(limiter, "asyncio", SimpleNamespace(
        sleep=clock.sleep, current_task=asyncio.current_task))
    monkeypatch.setattr(kis_kr, "asyncio", SimpleNamespace(
        sleep=clock.sleep, TimeoutError=asyncio.TimeoutError))
    return _Harness(clock)


def test_stale_ledger_rechecks_global_gap_after_quote(harness):
    async def run():
        async with harness as h:
            await h.start("A")
            await h.clock.advance(110.01)
            quote = await h.start("quote", _Response(ready=True), tr_id="FHKST01010100")
            assert quote.result()["rt_cd"] == "0"
            await h.start("B")
            assert [row[0] for row in h.sent] == ["A", "quote"]
            await h.clock.advance(110.12)
            assert h.sent[-1] == ("B", 110.12, "TTTC8001R")
    asyncio.run(run())


@pytest.mark.parametrize("ending", ["response", "cancel"])
def test_late_stale_response_cannot_release_successor(harness, ending):
    async def run():
        async with harness as h:
            old_response = _Response()
            old = await h.start("A", old_response)
            await h.clock.advance(110.01)
            active_response = _Response()
            active = await h.start("B", active_response)
            await h.clock.advance(110.02)
            if ending == "cancel":
                old.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await old
            else:
                old_response.headers_ready.set()
                await _settle()
                assert old.result()["rt_cd"] == "0"
            assert limiter._state["ledger_busy_since"] == 110.01
            assert limiter._state["ledger_last"] == 110.01
            await h.clock.advance(111.08)
            await h.start("C")
            assert [row[0] for row in h.sent] == ["A", "B"]
            active_response.headers_ready.set()
            await active
            await h.clock.advance(112.12)
            assert [row[0] for row in h.sent] == ["A", "B"]
            await h.clock.advance(112.14)
            assert h.sent[-1] == ("C", 112.14, "TTTC8001R")
    asyncio.run(run())


def test_stale_ledger_retains_rejection_interval(harness):
    async def run():
        async with harness as h:
            await h.start("A")
            await h.clock.advance(110.01)
            limiter.note_ledger_rejection("TTTC8001R")
            await h.start("B")
            assert len(h.sent) == 1
            await h.clock.advance(112.0)
            assert len(h.sent) == 1
            await h.clock.advance(112.02)
            assert h.sent[-1] == ("B", 112.02, "TTTC8001R")
    asyncio.run(run())


def test_reset_invalidates_owner_and_release_timestamp(harness):
    async def run():
        await limiter.acquire("TTTC8001R")
        limiter.reset()
        await harness.clock.advance(101.0)
        limiter.release_ledger()
        assert limiter._state["ledger_busy_since"] == 0.0
        assert limiter._state["ledger_last"] == 0.0
    asyncio.run(run())


def test_release_outside_event_loop_preserves_orphaned_owner(harness):
    asyncio.run(limiter.acquire("TTTC8001R"))
    assert limiter._state["ledger_busy_since"] == 100.0
    before = dict(limiter._state)
    harness.clock.now = 101.0
    limiter.release_ledger()
    limiter.stamp_ledger()
    assert limiter._state == before


def test_cancelled_owner_releases_before_stale_timeout(harness):
    async def run():
        async with harness as h:
            owner = await h.start("A")
            await h.clock.advance(100.2)
            owner.cancel()
            with pytest.raises(asyncio.CancelledError):
                await owner
            assert limiter._state["ledger_busy_since"] == 0.0
            await h.start("B")
            await h.clock.advance(101.24)
            assert len(h.sent) == 1
            await h.clock.advance(101.26)
            assert h.sent[-1] == ("B", 101.26, "TTTC8001R")
    asyncio.run(run())


def test_cancelled_waiter_preserves_active_owner(harness):
    async def run():
        async with harness as h:
            active_response = _Response()
            active = await h.start("A", active_response)
            await h.clock.advance(100.2)
            waiter = await h.start("B")
            waiter.cancel()
            with pytest.raises(asyncio.CancelledError):
                await waiter
            assert limiter._state["ledger_busy_since"] == 100.0
            assert limiter._state["ledger_last"] == 100.0
            await h.clock.advance(101.26)
            await h.start("C")
            assert [row[0] for row in h.sent] == ["A"]
            active_response.headers_ready.set()
            await active
            await h.clock.advance(102.30)
            assert [row[0] for row in h.sent] == ["A"]
            await h.clock.advance(102.32)
            assert h.sent[-1] == ("C", 102.32, "TTTC8001R")
    asyncio.run(run())


@pytest.mark.parametrize("ending", ["success", "body_error", "cancel"])
def test_headers_release_early_and_body_exit_preserves_successor(harness, ending):
    async def run():
        async with harness as h:
            response = _Response(ready=True)
            response.body_ready.clear()
            old = await h.start("A", response)
            await h.clock.advance(101.04)
            await h.start("B")
            assert len(h.sent) == 1
            await h.clock.advance(101.06)
            assert h.sent[-1][0] == "B"
            assert not old.done()
            await h.clock.advance(101.07)
            if ending == "cancel":
                old.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await old
            else:
                if ending == "body_error":
                    response.body_error = aiohttp.ClientError("synthetic body failure")
                response.body_ready.set()
                await _settle()
                assert old.result()["rt_cd"] == ("-1" if ending == "body_error" else "0")
            assert limiter._state["ledger_busy_since"] == 101.06
            assert limiter._state["ledger_last"] == 101.06
    asyncio.run(run())


def test_duplicate_release_does_not_delay_next_admission(harness):
    async def run():
        await limiter.acquire("TTTC8001R")
        limiter.stamp_ledger()
        await harness.clock.advance(101.0)
        limiter.release_ledger()
        assert limiter._state["ledger_last"] == 100.0
        async with harness as h:
            await h.clock.advance(101.06)
            await h.start("B")
            assert h.sent == [("B", 101.06, "TTTC8001R")]
    asyncio.run(run())


def test_unexpected_header_error_releases_owner(harness, monkeypatch):
    async def run():
        async with harness as h:
            def broken_headers(_tr_id):
                raise RuntimeError("synthetic header failure")
            monkeypatch.setattr(h.broker, "_get_headers", broken_headers)
            task = await h.start("A")
            with pytest.raises(RuntimeError, match="synthetic header failure"):
                await task
            assert limiter._state["ledger_busy_since"] == 0.0
            assert h.sent == []
    asyncio.run(run())


@pytest.mark.parametrize("failure", ["network", "EGW00201", "EGW00215"])
def test_retry_releases_before_wait_and_retains_backoff(harness, failure):
    async def run():
        async with harness as h:
            if failure == "network":
                response = _Response(ready=True)
                response.enter_error = aiohttp.ClientError("synthetic network failure")
            else:
                response = _Response(status=500, body={"rt_cd": "1", "msg_cd": failure}, ready=True)
            await h.start("A", response, _Response())
            assert limiter._state["ledger_busy_since"] == 0.0
            # 일반/네트워크는 응답 후1.05초, 원장 거절은2초 뒤 재입장.
            before, after = (101.99, 102.01) if failure == "EGW00215" else (101.04, 101.06)
            await h.clock.advance(before)
            assert len(h.sent) == 1
            await h.clock.advance(after)
            assert len(h.sent) == 2
            assert limiter._state["ledger_busy_since"] == after
    asyncio.run(run())
