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
        self.observations = []

    def observe_revocation(self, failed_token):
        self.observations.append(failed_token)

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


def frozen_clock():
    """시간이 주제가 아닌 시험용 고정 시계 — 실시간 경과가 예산·deadline 판정에 새지 않는다."""
    return 100.0


def steady_budget(mod, **kwargs):
    """부하 비의존 예산: 고정 주입 시계 + 넉넉한 30초.

    제품은 남은 초를 asyncio 실시간 타이머(asyncio.timeout/wait_for)에도 넘기므로 주입 시계만으로는
    실시간 1초 제한이 남는다. 30초는 만료 원인이 아니라 시험 고착 감시용이다.
    """
    return mod.RequestBudget(30, clock=frozen_clock, **kwargs)


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
                                 budget=steady_budget(mod))
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
                                    budget=steady_budget(mod)) == body
    asyncio.run(run())
    assert transport.closed


class FakeResponse:
    status = 200
    headers = {}
    @property
    def content(self):
        return self
    async def iter_chunked(self, size):
        yield b'{"result":[]}'
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
            assert await client.get(path, params=params, budget=steady_budget(mod)) == body
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
                                 budget=steady_budget(mod))
    asyncio.run(run())


def test_client_injected_exception_chain_never_displays_raw_secret(tmp_path):
    mod = api()
    client, _, transport = make_client(tmp_path, enabled=True, role="sender",
        responses=[RuntimeError("synthetic-private-credential")] * 2)
    async def run():
        async with client:
            try:
                await client.get("/api/v1/prices", params={"symbols": "005930"},
                                 budget=steady_budget(mod))
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
                                 budget=steady_budget(mod))
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
                                 budget=steady_budget(mod))
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
                                 budget=steady_budget(mod))
    asyncio.run(run())
    assert len(transport.requests) == 1
    assert len(tokens.calls) == 1
    assert tokens.observations == []


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


def real_token_stack(tmp_path):
    """통합 인증 회귀는 실제 저장소/관리자와 합성 발급기만 사용한다."""
    from datetime import datetime, timedelta, timezone
    from src.data.providers.toss.token import TokenManager
    from src.data.providers.toss.token_store import SecureTokenStore, TokenRecord
    now = [datetime(2026, 9, 16, tzinfo=timezone.utc)]
    minted = []
    async def issuer():
        minted.append(True)
        return {"access_token": "synthetic-issued-bearer", "expires_in": 7200, "token_type": "Bearer"}
    store = SecureTokenStore(tmp_path / "tokens", "offline-client-boundary")
    store.save(TokenRecord(access_token="synthetic-original-bearer",
        issued_at=now[0] - timedelta(hours=1), expires_at=now[0] + timedelta(hours=2),
        generation=1, client_identity=store.client_identity))
    store.save_state("ready", 1)
    manager = TokenManager(store, enabled=True, role="issuer", issuer=issuer, now=lambda: now[0])
    return store, manager, minted, now


@pytest.mark.parametrize("prefix,max_retries", [([], 0), ([500], 1)])
def test_exhausted_retry_still_persists_revoked_before_poll_restart_and_expiry(tmp_path, prefix, max_retries):
    from datetime import timedelta
    from src.data.providers.toss.token import TokenManager
    from src.data.providers.toss.token_store import TokenError
    mod = api()
    store, manager, minted, now = real_token_stack(tmp_path)
    manager.clock = frozen_clock
    responses = [mod.HttpResponse(status, {}, {}) for status in prefix]
    responses.append(mod.HttpResponse(401, {}, {"error": {"code": "token-revoked"}}))
    client, _, transport = make_client(tmp_path, enabled=True, role="sender", responses=responses)
    client.tokens = manager
    async def run():
        async with client:
            with pytest.raises(mod.TossRequestError):
                await client.get("/api/v1/prices", params={"symbols": "005930"},
                    budget=steady_budget(mod, max_retries=max_retries))
            with pytest.raises(mod.TossRequestError, match="auth_unavailable"):
                await client.get("/api/v1/prices", params={"symbols": "005930"}, budget=steady_budget(mod))
        now[0] += timedelta(hours=3)
        restarted = TokenManager(store, enabled=True, role="issuer", issuer=manager.issuer,
                                 now=lambda: now[0], clock=manager.clock)
        for candidate in (manager, restarted):
            with pytest.raises(TokenError, match="auth_unavailable"):
                await candidate.get_token(deadline=steady_budget(mod).deadline)
            with pytest.raises(TokenError, match="auth_unavailable"):
                await candidate.bootstrap(approved=True, deadline=steady_budget(mod).deadline)
    asyncio.run(run())
    assert len(transport.requests) == len(prefix) + 1
    assert minted == []


@pytest.mark.parametrize("max_retries", [0, 1])
def test_revoked_can_adopt_new_cache_without_exceeding_http_retry_budget(tmp_path, max_retries):
    from dataclasses import replace
    mod = api()
    store, manager, minted, _ = real_token_stack(tmp_path)
    manager.clock = frozen_clock
    client, _, transport = make_client(tmp_path, enabled=True, role="sender")
    client.tokens = manager
    sent = []
    async def respond(*args, **kwargs):
        sent.append(kwargs["headers"]["Authorization"])
        if len(sent) == 1:
            store.save(replace(store.load(), access_token="synthetic-other-bearer", generation=2))
            return mod.HttpResponse(401, {}, {"error": {"code": "token-revoked"}})
        return mod.HttpResponse(200, {}, {"result": []})
    transport.request = respond
    async def run():
        async with client:
            action = client.get("/api/v1/prices", params={"symbols": "005930"},
                                budget=steady_budget(mod, max_retries=max_retries))
            if max_retries == 0:
                with pytest.raises(mod.TossRequestError, match="retry_exhausted"):
                    await action
            else:
                assert await action == {"result": []}
            assert len(sent) == 1 + max_retries
            assert await manager.get_token(deadline=steady_budget(mod).deadline) == "synthetic-other-bearer"
            assert await client.get("/api/v1/prices", params={"symbols": "005930"},
                                    budget=steady_budget(mod)) == {"result": []}
    asyncio.run(run())
    assert sent == ["Bearer synthetic-original-bearer"] + ["Bearer synthetic-other-bearer"] * (1 + max_retries)
    assert minted == []


@pytest.mark.parametrize("prefix,max_retries", [([], 0), ([500], 1), ([], 1)])
def test_expired_token_issuance_still_requires_remaining_retry(tmp_path, prefix, max_retries):
    mod = api()
    _, manager, minted, _ = real_token_stack(tmp_path)
    manager.clock = frozen_clock
    responses = [mod.HttpResponse(status, {}, {}) for status in prefix]
    responses += [mod.HttpResponse(401, {}, {"error": {"code": "expired-token"}}),
                  mod.HttpResponse(200, {}, {"result": []})]
    client, _, transport = make_client(tmp_path, enabled=True, role="sender", responses=responses)
    client.tokens = manager
    can_retry = max_retries > len(prefix)
    async def run():
        async with client:
            action = client.get("/api/v1/prices", params={"symbols": "005930"},
                                budget=steady_budget(mod, max_retries=max_retries))
            if can_retry:
                assert await action == {"result": []}
            else:
                with pytest.raises(mod.TossRequestError, match="retry_exhausted"):
                    await action
    asyncio.run(run())
    assert len(transport.requests) == len(prefix) + 1 + int(can_retry)
    assert len(minted) == int(can_retry)


@pytest.mark.parametrize("stop", ["deadline", "cancel"])
def test_revoked_observation_does_not_escape_deadline_or_cancellation(tmp_path, stop):
    """동기 폐기 관측 후 토큰 잠금 대기만 기존 예산/취소에 종속된다."""
    from contextlib import asynccontextmanager
    mod = api()
    store, manager, minted, now = real_token_stack(tmp_path)
    clock_value = [100.0]
    clock = lambda: clock_value[0]
    manager.clock = clock
    client, _, transport = make_client(tmp_path, enabled=True, role="sender", clock=clock)
    client.tokens = manager
    observed, recovery_waiting = asyncio.Event(), asyncio.Event()
    held, sent = [], []
    real_lock = store.lock
    @asynccontextmanager
    async def tracked_lock(*, deadline, clock):
        if held:
            recovery_waiting.set()
        async with real_lock(deadline=deadline, clock=clock):
            yield
    store.lock = tracked_lock
    async def respond(*args, **kwargs):
        lock = store.lock(deadline=clock() + 60, clock=clock)
        await lock.__aenter__()
        held.append(lock)
        sent.append(True)
        observed.set()
        return mod.HttpResponse(401, {}, {"error": {"code": "token-revoked"}})
    transport.request = respond
    async def run():
        async with client:
            task = asyncio.create_task(client.get("/api/v1/prices", params={"symbols": "005930"},
                budget=mod.RequestBudget(60, clock=clock, max_retries=0)))
            try:
                # wall-clock 제한은 테스트 고착 감시용이며 요청 만료의 원인이 아니다.
                await asyncio.wait_for(observed.wait(), 5)
                await asyncio.wait_for(recovery_waiting.wait(), 5)
                if stop == "cancel":
                    task.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await task
                else:
                    clock_value[0] += 61
                    with pytest.raises(mod.TossRequestError):
                        await asyncio.wait_for(task, 5)
            finally:
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                for lock in held:
                    await lock.__aexit__(None, None, None)
        await assert_revoked_after_restart_and_expiry(store, manager, now)
    asyncio.run(run())
    assert sent == [True] and minted == []


def test_revocation_callback_precedes_limiter_observe_await(tmp_path):
    mod = api()
    client, tokens, transport = make_client(tmp_path, enabled=True, role="sender",
        responses=[mod.HttpResponse(401, {}, {"error": {"code": "token-revoked"}})])
    order = []
    original_observer = tokens.observe_revocation
    def observe(token):
        original_observer(token)
        order.append("revoked")
    async def observe_rate(*args, **kwargs):
        order.append("limiter")
    tokens.observe_revocation = observe
    client.limiter.observe = observe_rate
    async def run():
        async with client:
            with pytest.raises(mod.TossRequestError):
                await client.get("/api/v1/prices", params={"symbols": "005930"},
                                 budget=steady_budget(mod, max_retries=0))
    asyncio.run(run())
    assert order == ["revoked", "limiter"]
    assert tokens.observations == ["synthetic-bearer"]
    assert len(transport.requests) == 1


@pytest.mark.parametrize("async_callback", [False, True])
def test_missing_sync_observation_protocol_fails_before_auth_and_http(tmp_path, async_callback):
    mod = api()
    client, tokens, transport = make_client(tmp_path, enabled=True, role="sender",
        responses=[mod.HttpResponse(200, {}, {"result": []})])
    async def wrong_callback(failed_token):
        return None
    tokens.observe_revocation = wrong_callback if async_callback else None
    async def run():
        async with client:
            with pytest.raises(mod.TossRequestError, match="auth_unavailable"):
                await client.get("/api/v1/prices", params={"symbols": "005930"}, budget=steady_budget(mod))
    asyncio.run(run())
    assert tokens.calls == transport.requests == []


async def assert_revoked_after_restart_and_expiry(store, manager, now):
    from datetime import timedelta
    from src.data.providers.toss.token import TokenManager
    from src.data.providers.toss.token_store import TokenError
    with pytest.raises(TokenError, match="auth_unavailable"):
        await manager.get_token(deadline=manager.clock() + 1)
    now[0] += timedelta(hours=3)
    restarted = TokenManager(store, enabled=True, role="issuer", issuer=manager.issuer,
                             now=lambda: now[0], clock=manager.clock)
    for candidate in (manager, restarted):
        with pytest.raises(TokenError, match="auth_unavailable"):
            await candidate.get_token(deadline=manager.clock() + 1)
        with pytest.raises(TokenError, match="auth_unavailable"):
            await candidate.bootstrap(approved=True, deadline=manager.clock() + 1)


def test_revoked_response_after_deadline_still_prevents_restart_mint(tmp_path):
    import time
    mod = api()
    store, manager, minted, now = real_token_stack(tmp_path)
    clock_value = [time.monotonic()]
    clock = lambda: clock_value[0]
    manager.clock = clock
    client, _, transport = make_client(tmp_path, enabled=True, role="sender", clock=clock)
    client.tokens = manager
    sent = []
    async def respond(*args, **kwargs):
        sent.append(True)
        clock_value[0] += 2
        return mod.HttpResponse(401, {}, {"error": {"code": "token-revoked"}})
    transport.request = respond
    async def run():
        async with client:
            with pytest.raises(mod.TossRequestError):
                await client.get("/api/v1/prices", params={"symbols": "005930"},
                                 budget=mod.RequestBudget(1, clock=clock))
        await assert_revoked_after_restart_and_expiry(store, manager, now)
    asyncio.run(run())
    assert sent == [True] and minted == []


def test_cancelled_rate_observation_still_prevents_restart_mint(tmp_path):
    mod = api()
    store, manager, minted, now = real_token_stack(tmp_path)
    manager.clock = frozen_clock
    client, _, transport = make_client(tmp_path, enabled=True, role="sender")
    client.tokens = manager
    received = asyncio.Event()
    sent = []
    async def respond(*args, **kwargs):
        await client.limiter._lock.acquire()
        sent.append(True)
        received.set()
        return mod.HttpResponse(401, {}, {"error": {"code": "token-revoked"}})
    transport.request = respond
    async def run():
        async with client:
            task = asyncio.create_task(client.get("/api/v1/prices", params={"symbols": "005930"},
                                                 budget=mod.RequestBudget(60, clock=frozen_clock)))
            try:
                await asyncio.wait_for(received.wait(), 5)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            finally:
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                if client.limiter._lock.locked():
                    client.limiter._lock.release()
        await assert_revoked_after_restart_and_expiry(store, manager, now)
    asyncio.run(run())
    assert sent == [True] and minted == []


def test_disabled_client_with_real_token_manager_never_creates_storage(tmp_path):
    from src.data.providers.toss.token import TokenManager
    from src.data.providers.toss.token_store import SecureTokenStore
    mod = api()
    store = SecureTokenStore(tmp_path / "unused-token-store", "offline-disabled")
    manager = TokenManager(store, enabled=False)
    client, _, transport = make_client(tmp_path, enabled=False, role="sender")
    client.tokens = manager
    async def run():
        async with client:
            with pytest.raises(mod.TossRequestError, match="disabled"):
                await client.get("/api/v1/prices", params={"symbols": "005930"}, budget=mod.RequestBudget(1))
    asyncio.run(run())
    assert not store.directory.exists()
    assert transport.requests == [] and not transport.closed
