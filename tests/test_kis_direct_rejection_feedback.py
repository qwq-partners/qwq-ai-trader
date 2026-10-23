"""직접 GET 거절이 공유 리미터의 다음 실제 acquire를 늦추는지 검증한다."""
import asyncio
import collections
import json
from types import SimpleNamespace

import aiohttp
import pytest
from loguru import logger

from src.utils import kis_direct_metrics as metrics, kis_rate_limit as limiter
from test_kis_direct_instrumentation import _market, _screener, _Session
from test_kis_pagination_protocol import broker


class _Stream:
    def __init__(self, body, *, chunk_size=7, error=None, stall=False):
        self.body, self.chunk_size = body, chunk_size
        self.error, self.stall = error, stall
        self.offset = 0
        self.read_sizes = []
        self.cancelled = False

    async def read(self, size):
        self.read_sizes.append(size)
        if self.error:
            raise self.error
        if self.stall:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled = True
                raise
        chunk = self.body[self.offset:self.offset + min(size, self.chunk_size)]
        self.offset += len(chunk)
        return chunk

    def at_eof(self):
        return self.offset == len(self.body)


class _Response:
    def __init__(self, status=500, body=None, *, raw=None, **stream_kwargs):
        self.status, self.body = status, body
        self.content = _Stream(raw if raw is not None else json.dumps(body).encode(), **stream_kwargs)
        self.json_calls = 0
        self.headers = {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def json(self):
        self.json_calls += 1
        return self.body


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    monkeypatch.setattr(metrics, "_recorder", metrics.DirectKISMetrics())
    monkeypatch.setattr(limiter, "_state", dict.fromkeys(limiter._state, 0))
    monkeypatch.setattr(limiter, "_calls", collections.deque(maxlen=10))
    clock = SimpleNamespace(now=100.0, sleeps=[])

    async def advance(delay):
        clock.sleeps.append(delay)
        clock.now += delay

    monkeypatch.setattr(limiter, "time", SimpleNamespace(monotonic=lambda: clock.now))
    monkeypatch.setattr(limiter, "asyncio", SimpleNamespace(sleep=advance))
    return clock


@pytest.mark.parametrize("factory,method", [
    (_market, lambda target: target.fetch_sector_indices()),
    (_screener, lambda target: target.screen_volume_surge()),
])
@pytest.mark.parametrize("status", [500, 200])
def test_confirmed_direct_rejection_holds_next_real_acquire_once(factory, method, status, isolated):
    response = _Response(status, {"rt_cd": "1", "msg_cd": "EGW00201"})
    target, session = factory([response])

    async def run():
        assert await method(target) == []
        assert limiter._state["rejections"] == 1
        await limiter.acquire("FHKST01010100")

    asyncio.run(run())
    assert isolated.now == 101.0
    assert isolated.sleeps == [1.0]
    assert len(session.calls) == 1
    row = metrics.snapshot()["http"][0]
    assert row["egw00201"] == row["attempts"] == 1
    assert row["http_error" if status == 500 else "api_error"] == 1
    assert response.json_calls == (1 if status == 200 else 0)


@pytest.mark.parametrize("raw", [
    b'<html>EGW00201</html>', b'{"msg1":"EGW00201"}', b'{"rt_cd":"EGW00201"}',
    b'{"msg_cd":" EGW00201"}', b'{"msg_cd":["EGW00201"]}', b'[]',
    b'{"msg_cd":"EGW00201",', b'\xff',
    b'{"msg_cd":"EGW00201","padding":"' + b'x' * 17000 + b'"}',
    b'[' * 1200 + b']' * 1200,
])
def test_unknown_http_errors_never_hold_or_retain_raw_body(raw):
    response = _Response(raw=raw, chunk_size=4096)
    target, session = _market([response])
    assert asyncio.run(target.fetch_sector_indices()) == []
    assert limiter._state["rejections"] == 0
    row = metrics.snapshot()["http"][0]
    assert row["http_error"] == 1
    assert row["egw00201"] == 0
    assert len(session.calls) == 1
    assert response.content.offset <= 16384


def test_non_200_body_deadline_preserves_http_failure():
    response = _Response(stall=True)
    target, _ = _market([response])

    async def run():
        assert await asyncio.wait_for(target.fetch_sector_indices(), timeout=0.8) == []

    asyncio.run(run())
    assert response.content.cancelled
    assert limiter._state["rejections"] == 0
    assert metrics.snapshot()["http"][0]["http_error"] == 1


def test_cancel_during_non_200_body_read_propagates():
    response = _Response(error=asyncio.CancelledError())
    target, _ = _screener([response])
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(target.screen_volume_surge())
    assert limiter._state["rejections"] == 0
    assert metrics.snapshot()["http"][0]["cancelled"] == 1


def test_read_failure_remains_unknown_http_error():
    response = _Response(error=aiohttp.ClientPayloadError("private body"))
    target, _ = _market([response])
    assert asyncio.run(target.fetch_sector_indices()) == []
    assert limiter._state["rejections"] == 0
    row = metrics.snapshot()["http"][0]
    assert (row["http_error"], row["network_error"]) == (1, 0)


def test_repeated_observation_is_terminal_and_logs_only_sanitized_code():
    private = "private-account-and-token"
    response = _Response(body={"msg_cd": "EGW00201", "msg1": private})
    logs = []
    sink = logger.add(lambda message: logs.append(str(message)))
    try:
        async def run():
            with metrics.observe_get("market_data", "sector_indices", "FHPUP02140000") as observed:
                assert await observed.inspect_response(response) is False
                assert await observed.inspect_response(response) is False
                observed.api_envelope(response.body)
        asyncio.run(run())
    finally:
        logger.remove(sink)
    assert limiter._state["rejections"] == 1
    assert metrics.snapshot()["http"][0]["egw00201"] == 1
    assert private not in str(logs) + str(metrics.snapshot())


def test_metrics_failure_does_not_disable_rejection_feedback(monkeypatch):
    def broken(*_args):
        raise RuntimeError("metrics only")
    monkeypatch.setattr(metrics._recorder, "increment", broken)
    target, _ = _market([_Response(body={"msg_cd": "EGW00201"})])
    assert asyncio.run(target.fetch_sector_indices()) == []
    assert limiter._state["rejections"] == 1
    assert metrics.snapshot()["available"] is False


def test_control_failure_is_not_silently_swallowed_as_metrics(monkeypatch):
    def broken(_tr_id):
        raise RuntimeError("control failed")
    monkeypatch.setattr(limiter, "note_rejection", broken)
    async def run():
        with metrics.observe_get("market_data", "sector_indices", "FHPUP02140000") as observed:
            await observed.inspect_response(_Response(body={"msg_cd": "EGW00201"}))
    with pytest.raises(RuntimeError, match="control failed"):
        asyncio.run(run())


def test_success_body_consumed_once_without_rejection():
    response = _Response(200, {"rt_cd": "0", "output": []})
    target, _ = _market([response])
    assert asyncio.run(target.fetch_sector_indices()) == []
    assert response.json_calls == 1
    assert response.content.read_sizes == []
    assert limiter._state["rejections"] == 0


def test_existing_broker_rejection_is_not_double_counted(broker, monkeypatch):
    response = _Response(body={"rt_cd": "1", "msg_cd": "EGW00201"})
    broker._session = _Session([response, _Response(200, {"rt_cd": "0"})])
    async def no_wait(_delay):
        return None
    monkeypatch.setattr("src.execution.broker.kis_kr.asyncio.sleep", no_wait)
    assert asyncio.run(broker._api_get("http://fake", "FHKST01010100", {}))["rt_cd"] == "0"
    assert limiter._state["rejections"] == 1
    assert metrics.snapshot()["http"] == []


def test_exact_limit_complete_aiohttp_stream_is_inspected():
    async def run():
        protocol = aiohttp.base_protocol.BaseProtocol(asyncio.get_running_loop())
        stream = aiohttp.StreamReader(protocol, limit=65536)
        prefix = b'{"msg_cd":"EGW00201","padding":"'
        body = prefix + b'x' * (16384 - len(prefix) - 2) + b'"}'
        stream.feed_data(body)
        stream.feed_eof()
        response = _Response()
        response.content = stream
        target, _ = _market([response])
        assert await target.fetch_sector_indices() == []
    asyncio.run(run())
    assert limiter._state["rejections"] == 1
    assert metrics.snapshot()["http"][0]["http_error"] == 1


def test_200_api_envelope_reinspection_does_not_repeat_feedback():
    response = _Response(200, {"rt_cd": "1", "msg_cd": "EGW00201"})
    async def run():
        with metrics.observe_get("market_data", "sector_indices", "FHPUP02140000") as observed:
            assert await observed.inspect_response(response)
            body = await observed.json(response)
            observed.api_envelope(body)
            observed.api_envelope(body)
    asyncio.run(run())
    assert limiter._state["rejections"] == 1
    assert metrics.snapshot()["http"][0]["api_error"] == 1
    assert response.json_calls == 1


@pytest.mark.parametrize("body", [
    {"rt_cd": "EGW00201"},
    {"rt_cd": "1", "msg1": "EGW00201"},
    {"rt_cd": "1", "msg_cd": "EGW00215"},
    {"rt_cd": "0", "output": []},
])
def test_200_without_confirmed_rate_rejection_never_holds(body):
    target, _ = _market([_Response(200, body)])
    assert asyncio.run(target.fetch_sector_indices()) == []
    assert limiter._state["rejections"] == 0
    assert limiter._state["ledger_rejections"] == 0
