"""송신 경계를 실제 클라이언트와 가짜 외부 세션으로 검사한다."""

import asyncio
import importlib
import importlib.util
import traceback

import pytest


def api():
    name = "src.data.providers.toss.client"
    try:
        found = importlib.util.find_spec(name)
    except ModuleNotFoundError:
        found = None
    assert found is not None, "조회 권한 경계 구현이 필요하다"
    return importlib.import_module(name)


class Tokens:
    def __init__(self):
        self.calls = []

    async def get_token(self, *, deadline):
        self.calls.append(("get", deadline))
        return "synthetic-bearer"

    async def recover(self, code, failed_token, *, deadline):
        self.calls.append((code, deadline))
        return "synthetic-replacement"


class Transport:
    def __init__(self, responses=()):
        self.responses = list(responses)
        self.requests = []
        self.closed = False

    async def request(self, method, path, **kwargs):
        self.requests.append((method, path, kwargs))
        result = self.responses.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result

    async def close(self):
        self.closed = True


def make_client(tmp_path, *, responses=(), **kwargs):
    mod = api()
    rates = importlib.import_module("src.data.providers.toss.rate_limit")
    tokens, transport = Tokens(), Transport(responses)
    client = mod.TossClient(
        tokens=tokens, transport=transport,
        limiter=rates.GroupRateLimiter(limits={
            "MARKET_DATA": 100, "MARKET_DATA_CHART": 100, "MARKET_INFO": 100}),
        sender_lock_path=tmp_path / "sender.lock", circuit_failure_threshold=2,
        circuit_open_seconds=5, **kwargs)
    return client, tokens, transport


@pytest.mark.parametrize("settings", [{}, {"enabled": True}, {"role": "sender"}])
def test_off_or_reader_never_touches_credentials_transport_or_lock(tmp_path, settings):
    mod = api()
    client, tokens, transport = make_client(tmp_path, **settings)
    async def run():
        async with client:
            with pytest.raises(mod.TossRequestError):
                await client.get("/api/v1/prices", params={"symbols": "005930"},
                                 budget=mod.RequestBudget(1))
    asyncio.run(run())
    assert tokens.calls == transport.requests == []
    assert not transport.closed
    assert not (tmp_path / "sender.lock").exists()


@pytest.mark.parametrize("method,path,params,headers", [
    ("GET", "/api/v1/" + endpoint, {}, {})
    for endpoint in ("orders", "accounts", "cancel", "conditional-orders")
] + [("POST", "/api/v1/prices", {"symbols": "005930"}, {})] + [
    ("GET", path, {"symbols": "005930"}, {}) for path in (
        "https://evil.invalid/api/v1/prices", "//evil.invalid/api/v1/prices",
        "/api/v1/prices?x=1", "/api/v1/prices#x", "/api/v1/%70rices",
        "/api/v1/../v1/prices", "/api//v1/prices", "/api/v1/prices/")
] + [("GET", "/api/v1/prices", {"symbols": "005930"},
      {"X-Tossinvest-Account": "synthetic-account"})])
def test_disallowed_request_is_rejected_before_auth(tmp_path, method, path, params, headers):
    mod = api()
    client, tokens, transport = make_client(tmp_path, enabled=True, role="sender")
    async def run():
        async with client:
            with pytest.raises(mod.TossRequestError):
                await client.request(method, path, params=params, headers=headers,
                                     budget=mod.RequestBudget(1))
    asyncio.run(run())
    assert tokens.calls == transport.requests == []


@pytest.mark.parametrize("params", [
    {}, {"symbols": ""}, {"symbols": "005930,"}, {"symbols": ["005930"]},
    {"symbols": "005930", "account": "x"}, {"symbols": "A" * 40},
    {"symbols": ",".join(["005930"] * 201)}, {"symbols": "005930\r\n"},
])
def test_bad_parameters_never_reach_auth(tmp_path, params):
    mod = api()
    client, tokens, transport = make_client(tmp_path, enabled=True, role="sender")
    async def run():
        async with client:
            with pytest.raises(mod.TossRequestError, match="invalid_params"):
                await client.get("/api/v1/prices", params=params, budget=mod.RequestBudget(1))
    asyncio.run(run())
    assert tokens.calls == transport.requests == []


@pytest.mark.parametrize("status,body,code", [
    (200, [], "malformed_response"), (200, {}, "malformed_response"),
    (200, {"result": None}, "malformed_response"),
    (200, {"result": {}}, "malformed_response"),
    (200, {"error": {"code": "unsupported"}}, "unsupported"),
    (403, {"error": {"code": "synthetic-secret"}}, "forbidden"),
    (400, {"error": {"code": "synthetic-secret"}}, "http_error"),
    (302, {}, "redirect_rejected"),
])
def test_structural_and_permanent_errors_are_not_retried(tmp_path, status, body, code):
    mod = api()
    client, _, transport = make_client(tmp_path, enabled=True, role="sender",
        responses=[mod.HttpResponse(status, {}, body)])
    async def run():
        async with client:
            with pytest.raises(mod.TossRequestError, match=code):
                await client.get("/api/v1/prices", params={"symbols": "005930"},
                                 budget=mod.RequestBudget(1))
    asyncio.run(run())
    assert len(transport.requests) == 1


def test_success_returns_envelope_and_redacts_response_repr(tmp_path):
    mod = api()
    body = {"result": [{"symbol": "005930", "lastPrice": "123"}]}
    response = mod.HttpResponse(200, {"secret": "synthetic-secret"}, body)
    assert "synthetic-secret" not in repr(response)
    assert "lastPrice" not in repr(response)
    client, _, transport = make_client(tmp_path, responses=[response], enabled=True, role="sender")
    async def run():
        async with client:
            assert await client.get("/api/v1/prices", params={"symbols": "005930"},
                                    budget=mod.RequestBudget(1)) == body
    asyncio.run(run())
    assert transport.closed


class FakeResponse:
    status = 200
    headers = {}
    async def __aenter__(self):
        return self
    async def __aexit__(self, *args):
        return False
    async def json(self):
        return {"result": []}


class FakeSession:
    def __init__(self, fail=False):
        self.calls, self.closed, self.fail = [], False, fail
        self._retry_connection = True
    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if self.fail:
            raise RuntimeError("synthetic-secret raw bearer")
        return FakeResponse()
    async def close(self):
        self.closed = True


def test_transport_is_lazy_pins_origin_tls_redirect_and_timeout():
    api()
    mod = importlib.import_module("src.data.providers.toss.transport")
    session, created = FakeSession(), []
    def factory():
        created.append(True)
        return session
    transport = mod.AiohttpTransport(session_factory=factory)
    assert created == []
    async def run():
        await transport.request("GET", "/api/v1/prices", params={"symbols": "005930"},
                                headers={"Authorization": "Bearer synthetic-bearer"}, timeout=2)
        await transport.close()
    asyncio.run(run())
    method, url, args = session.calls[0]
    assert method == "GET" and url == "https://openapi.tossinvest.com/api/v1/prices"
    assert args["ssl"] is True and args["allow_redirects"] is False
    assert args["timeout"].total == 2
    assert session.closed
    assert session._retry_connection is False


def test_transport_exception_display_does_not_expose_raw_secret():
    error = api().TossRequestError
    mod = importlib.import_module("src.data.providers.toss.transport")
    session = FakeSession(fail=True)
    async def run():
        transport = mod.AiohttpTransport(session_factory=lambda: session)
        try:
            await transport.request("GET", "/api/v1/prices", params={"symbols": "005930"},
                                    headers={"Authorization": "Bearer synthetic-bearer"}, timeout=2)
        except error as exc:
            rendered = "".join(traceback.format_exception(exc))
            assert "synthetic-secret" not in rendered
            assert exc.code == "network_error"
        else:
            pytest.fail("전송 오류를 성공으로 숨겼다")
        finally:
            await transport.close()
    asyncio.run(run())


@pytest.mark.parametrize("params", [
    {"symbol": "005930", "interval": "1m", "count": 1, "adjusted": True},
    {"symbol": "005930", "interval": "1d", "count": True, "adjusted": True},
    {"symbol": "005930", "interval": "1d", "count": 201, "adjusted": True},
    {"symbol": "005930", "interval": "1d", "count": 1, "adjusted": "true"},
    {"symbol": "005930", "interval": "1d", "count": 1, "adjusted": True, "before": "x\n"},
    {"symbol": "005930", "interval": "1d", "count": 1, "adjusted": True, "before": "x" * 129},
])
def test_candle_parameter_contract_precedes_auth(tmp_path, params):
    mod = api()
    client, tokens, transport = make_client(tmp_path, enabled=True, role="sender")
    async def run():
        async with client:
            with pytest.raises(mod.TossRequestError, match="invalid_params"):
                await client.get("/api/v1/candles", params=params, budget=mod.RequestBudget(1))
    asyncio.run(run())
    assert tokens.calls == transport.requests == []


@pytest.mark.parametrize("path,params,body", [
    ("/api/v1/candles", {"symbol": "A005930", "interval": "1d", "count": 200,
                         "adjusted": True, "before": "2026-09-15T00:00:00+09:00"},
     {"result": {"candles": [], "nextBefore": None}}),
    ("/api/v1/market-calendar/KR", {"date": "2026-09-16"},
     {"result": {"today": {}, "previousBusinessDay": {}, "nextBusinessDay": {}}}),
])
def test_other_allowed_endpoints_preserve_contract(tmp_path, path, params, body):
    mod = api()
    client, _, transport = make_client(tmp_path, enabled=True, role="sender",
                                      responses=[mod.HttpResponse(200, {}, body)])
    async def run():
        async with client:
            assert await client.get(path, params=params, budget=mod.RequestBudget(1)) == body
    asyncio.run(run())
    assert transport.requests[0][2]["params"] == params


def test_result_and_error_cannot_both_be_present_even_with_null_error(tmp_path):
    mod = api()
    client, _, _ = make_client(tmp_path, enabled=True, role="sender",
        responses=[mod.HttpResponse(200, {}, {"result": [], "error": None})])
    async def run():
        async with client:
            with pytest.raises(mod.TossRequestError, match="malformed_response"):
                await client.get("/api/v1/prices", params={"symbols": "005930"},
                                 budget=mod.RequestBudget(1))
    asyncio.run(run())


def test_client_injected_exception_chain_never_displays_raw_secret(tmp_path):
    mod = api()
    client, _, transport = make_client(tmp_path, enabled=True, role="sender",
        responses=[RuntimeError("synthetic-private-credential")] * 2)
    async def run():
        async with client:
            try:
                await client.get("/api/v1/prices", params={"symbols": "005930"},
                                 budget=mod.RequestBudget(1))
            except mod.TossRequestError as exc:
                assert "synthetic-private-credential" not in "".join(traceback.format_exception(exc))
                assert exc.code == "retry_exhausted"
            else:
                pytest.fail("네트워크 실패를 성공으로 숨겼다")
    asyncio.run(run())
    assert len(transport.requests) == 2


def test_transport_rejects_redirect_without_following_or_parsing_it():
    mod = api()
    transport_mod = importlib.import_module("src.data.providers.toss.transport")
    class Redirect(FakeResponse):
        status = 302
        async def json(self):
            raise AssertionError("redirect 본문 파싱 불필요")
    class Session(FakeSession):
        def request(self, *args, **kwargs):
            return Redirect()
    async def run():
        transport = transport_mod.AiohttpTransport(session_factory=Session)
        try:
            result = await transport.request("GET", "/api/v1/prices", params={"symbols": "005930"},
                headers={"Authorization": "Bearer synthetic-bearer"}, timeout=1)
            assert result.status == 302 and result.body is None
        finally:
            await transport.close()
    asyncio.run(run())


def test_missing_retry_control_fails_closed_before_session_request():
    error = api().TossRequestError
    transport_mod = importlib.import_module("src.data.providers.toss.transport")
    session = FakeSession()
    del session._retry_connection
    async def run():
        transport = transport_mod.AiohttpTransport(session_factory=lambda: session)
        try:
            for _ in range(2):
                with pytest.raises(error, match="unsupported"):
                    await transport.request("GET", "/api/v1/prices", params={"symbols": "005930"},
                        headers={"Authorization": "Bearer synthetic-bearer"}, timeout=1)
        finally:
            await transport.close()
    asyncio.run(run())
    assert session.calls == []
    assert session.closed


def test_none_response_is_structural_failure_without_retry(tmp_path):
    mod = api()
    client, _, transport = make_client(tmp_path, enabled=True, role="sender", responses=[None])
    async def run():
        async with client:
            with pytest.raises(mod.TossRequestError, match="malformed_response"):
                await client.get("/api/v1/prices", params={"symbols": "005930"},
                                 budget=mod.RequestBudget(1))
    asyncio.run(run())
    assert len(transport.requests) == 1


@pytest.mark.parametrize("value", [None, "", "x\r\nInjected: yes"])
def test_malformed_token_cannot_reach_injected_transport(tmp_path, value):
    mod = api()
    client, _, transport = make_client(tmp_path, enabled=True, role="sender")
    async def token(**kwargs):
        return value
    client.tokens.get_token = token
    async def run():
        async with client:
            with pytest.raises(mod.TossRequestError, match="auth_unavailable"):
                await client.get("/api/v1/prices", params={"symbols": "005930"},
                                 budget=mod.RequestBudget(1))
    asyncio.run(run())
    assert transport.requests == []


def test_401_with_result_and_error_is_not_recovered(tmp_path):
    mod = api()
    client, tokens, transport = make_client(tmp_path, enabled=True, role="sender",
        responses=[mod.HttpResponse(401, {}, {"result": [], "error": {"code": "token-revoked"}})])
    async def run():
        async with client:
            with pytest.raises(mod.TossRequestError, match="malformed_response"):
                await client.get("/api/v1/prices", params={"symbols": "005930"},
                                 budget=mod.RequestBudget(1))
    asyncio.run(run())
    assert len(transport.requests) == 1
    assert len(tokens.calls) == 1


@pytest.mark.parametrize("cursor", [
    "2026-09-15", "2026-09-15T00:00:00", "2026-02-30T00:00:00+09:00", "junk",
])
def test_before_requires_valid_aware_datetime_before_auth(tmp_path, cursor):
    mod = api()
    client, tokens, transport = make_client(tmp_path, enabled=True, role="sender")
    async def run():
        async with client:
            with pytest.raises(mod.TossRequestError, match="invalid_params"):
                await client.get("/api/v1/candles", params={"symbol": "005930", "interval": "1d",
                    "count": 200, "adjusted": True, "before": cursor}, budget=mod.RequestBudget(1))
    asyncio.run(run())
    assert tokens.calls == transport.requests == []


def test_transport_close_survives_repeated_cancellation_and_shared_retry():
    api()
    transport_mod = importlib.import_module("src.data.providers.toss.transport")
    entered, release = asyncio.Event(), asyncio.Event()
    class Session(FakeSession):
        async def close(self):
            entered.set()
            await release.wait()
            self.closed = True
    session = Session()
    async def run():
        transport = transport_mod.AiohttpTransport(session_factory=lambda: session)
        await transport.request("GET", "/api/v1/prices", params={"symbols": "005930"},
            headers={"Authorization": "Bearer synthetic-bearer"}, timeout=1)
        task = asyncio.create_task(transport.close())
        await entered.wait()
        for _ in range(2):
            task.cancel()
            await asyncio.sleep(0)
        retry = asyncio.create_task(transport.close())
        await asyncio.sleep(0)
        release.set()
        results = await asyncio.gather(task, retry, return_exceptions=True)
        assert isinstance(results[0], asyncio.CancelledError) and results[1] is None
        assert session.closed
    asyncio.run(run())
