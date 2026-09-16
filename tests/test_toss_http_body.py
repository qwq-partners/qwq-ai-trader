"""실제 파서의 스트림/구조/시간 상한을 합성 자료로 검사한다."""

import asyncio
import importlib
import importlib.util
import json

import pytest


def api():
    name = "src.data.providers.toss.http_body"
    assert importlib.util.find_spec(name) is not None, "bounded HTTP 구현 필요"
    return importlib.import_module(name)


class Response:
    def __init__(self, raw=b'{"ok":true}', *, headers=None, status=200, block=False):
        self.raw, self.headers, self.status = raw, headers or {}, status
        self.content = self
        self.reads = 0
        self.block = block
        self.exited = False

    async def iter_chunked(self, size):
        if self.block:
            await asyncio.Event().wait()
        for i in range(0, len(self.raw), min(size, 3)):
            self.reads += 1
            yield self.raw[i:i + min(size, 3)]

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        self.exited = True


def limits(**changes):
    return api().BodyLimits(**(dict(max_bytes=1024, max_depth=8, max_nodes=50,
                                   max_string=128, parse_timeout_seconds=1) | changes))


def read(response, **changes):
    return asyncio.run(api().read_json_bounded(response, limits=limits(**changes),
                                               deadline=10, clock=lambda: 0))


def test_stream_decodes_utf8_split_chunks_and_never_needs_json_method():
    assert read(Response('{"한글":[1,true,null]}'.encode())) == {"한글": [1, True, None]}


@pytest.mark.parametrize("raw,changes,code", [
    (b'{"a":1,"a":2}', {}, "invalid_json"),
    (b'{"a":NaN}', {}, "invalid_json"),
    (b'[1e999]', {}, "invalid_json"),
    (b'[[[0]]]', {"max_depth": 2}, "body_depth"),
    (b'[1,2,3]', {"max_nodes": 3}, "body_nodes"),
    (b'{"longkey":1}', {"max_string": 3}, "body_string"),
    (b'"longvalue"', {"max_string": 3}, "body_string"),
    (b'"\\ud800"', {}, "invalid_json"),
    (b'not json synthetic-secret', {}, "non_json"),
])
def test_strict_json_and_structural_limits(raw, changes, code):
    with pytest.raises(api().BodyError) as error:
        read(Response(raw), **changes)
    assert error.value.code == code
    assert "synthetic-secret" not in str(error.value)


def test_oversized_chunked_body_stops_at_cap_without_content_length():
    response = Response(b'"' + b'x' * 100 + b'"')
    with pytest.raises(api().BodyError, match="body_bytes"):
        read(response, max_bytes=10)
    assert response.reads == 4


@pytest.mark.parametrize("headers", [{"Content-Encoding": "gzip"},
                                      {"content-encoding": "br"},
                                      {"Content-Length": "10000"}])
def test_unsafe_encoding_and_declared_overflow_reject_before_read(headers):
    response = Response(headers=headers)
    with pytest.raises(api().BodyError):
        read(response)
    assert response.reads == 0


def test_stream_timeout_cancels_read():
    async def run():
        loop = asyncio.get_running_loop()
        with pytest.raises(api().BodyError, match="timeout"):
            await api().read_json_bounded(Response(block=True), limits=limits(),
                                          deadline=loop.time() + .01, clock=loop.time)
    asyncio.run(run())


def test_parse_budget_includes_synchronous_decoder(monkeypatch):
    mod = api()
    now = [0.0]
    original = json.loads
    def slow(*args, **kwargs):
        result = original(*args, **kwargs)
        now[0] = 2
        return result
    monkeypatch.setattr(mod.json, "loads", slow)
    with pytest.raises(mod.BodyError, match="timeout"):
        asyncio.run(mod.read_json_bounded(Response(), limits=limits(), deadline=10,
                                         clock=lambda: now[0]))


@pytest.mark.parametrize("change", [{"max_bytes": True}, {"max_depth": 0},
    {"max_nodes": -1}, {"max_string": 0}, {"parse_timeout_seconds": float("nan")}])
def test_invalid_limits_fail_closed(change):
    with pytest.raises(ValueError):
        limits(**change)


@pytest.mark.parametrize("raw,headers", [(b'"' + b'x' * 100 + b'"', {}),
    (b'{}', {"Content-Encoding": "gzip"}), (b'{"a":1,"a":2}', {})])
def test_get_transport_uses_bounded_stream_for_success_and_errors(raw, headers):
    from src.data.providers.toss.transport import AiohttpTransport, TossRequestError
    from test_toss_oauth import Session
    async def run():
        for status in (200, 401):
            session = Session(Response(raw, headers=headers, status=status))
            transport = AiohttpTransport(session_factory=lambda: session, body_limits=limits(max_bytes=20))
            try:
                with pytest.raises(TossRequestError, match="malformed_response"):
                    await transport.request("GET", "/api/v1/prices", params={"symbols": "005930"},
                        headers={"Authorization": "Bearer synthetic-token"}, timeout=1)
            finally:
                await transport.close()
            assert session.requests[0][2]["auto_decompress"] is False
    asyncio.run(run())


def test_get_authorization_is_after_session_creation_and_bounds_send_deadline():
    from src.data.providers.toss.transport import AiohttpTransport, TossRequestError
    from test_toss_oauth import Session
    events = []
    session = Session(Response(b'{"result":[]}'))
    def factory():
        events.append("factory")
        return session
    def authorize(*, deadline):
        events.append("authorize")
        return 2
    async def run():
        transport = AiohttpTransport(session_factory=factory, body_limits=limits(),
                                    authorize=authorize, clock=lambda: 0)
        result = await transport.request("GET", "/api/v1/prices", params={"symbols": "005930"},
            headers={"Authorization": "Bearer synthetic-token"}, timeout=10)
        assert result.body == {"result": []}
        await transport.close()
    asyncio.run(run())
    assert events == ["factory", "authorize"]
    assert session.requests[0][2]["timeout"].total == 2


def test_get_authority_denial_never_sends_or_leaks_error():
    from src.data.providers.toss.transport import AiohttpTransport, TossRequestError
    from test_toss_oauth import Session
    session = Session()
    def deny(*, deadline):
        raise ValueError("synthetic-secret")
    async def run():
        transport = AiohttpTransport(session_factory=lambda: session, authorize=deny)
        try:
            with pytest.raises(TossRequestError):
                await transport.request("GET", "/api/v1/prices", params={"symbols": "005930"},
                    headers={"Authorization": "Bearer synthetic-token"}, timeout=1)
        finally:
            await transport.close()
    asyncio.run(run())
    assert session.requests == []


def test_get_non_json_error_preserves_http_status():
    from src.data.providers.toss.transport import AiohttpTransport
    from test_toss_oauth import Session
    async def run():
        session = Session(Response(b'upstream failed', status=503))
        transport = AiohttpTransport(session_factory=lambda: session)
        result = await transport.request("GET", "/api/v1/prices", params={"symbols": "005930"},
            headers={"Authorization": "Bearer synthetic-token"}, timeout=1)
        assert result.status == 503 and result.body is None
        assert session.requests[0][2]["headers"]["Accept-Encoding"] == "identity"
        await transport.close()
    asyncio.run(run())


def test_real_aiohttp_connection_wait_cannot_send_after_approved_deadline(monkeypatch):
    """aiohttp의 5초 이상 timeout 올림이 짧은 승인 기한을 늘리지 않는다."""
    import math
    import time

    import aiohttp
    from src.data.providers.toss.transport import AiohttpTransport, TossRequestError

    async def run():
        expiry = math.ceil(time.monotonic()) + 5.1
        sends = []

        class Protocol:
            def set_response_params(self, **kwargs):
                pass

        class Connection:
            protocol = Protocol()

            def close(self):
                pass

        async def connect(self, req, *, traces, timeout):
            await asyncio.sleep(expiry + .08 - time.monotonic())
            return Connection()

        async def send(self, connection):
            sends.append(time.monotonic())
            raise aiohttp.ClientError("synthetic stop before socket send")

        def authorize(*, deadline):
            return min(deadline, expiry)

        monkeypatch.setattr(aiohttp.TCPConnector, "connect", connect)
        monkeypatch.setattr(aiohttp.ClientRequest, "send", send)
        transport = AiohttpTransport(authorize=authorize, body_limits=limits())
        try:
            with pytest.raises(TossRequestError) as error:
                await transport.request("GET", "/api/v1/prices", params={"symbols": "005930"},
                    headers={"Authorization": "Bearer synthetic-token"}, timeout=30)
            assert sends == [], "승인 기한 후 실제 aiohttp send 경계에 도달했다"
            assert error.value.code == "timeout"
        finally:
            await transport.close()
    asyncio.run(run())
