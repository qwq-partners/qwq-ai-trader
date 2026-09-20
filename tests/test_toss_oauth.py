"""승인/비밀/POST 재송신 금지 경계를 실제 어댑터로 검사한다."""

import asyncio
import importlib
import importlib.util
import json
import traceback

import pytest

from test_toss_http_body import Response, limits


def api():
    name = "src.data.providers.toss.oauth"
    assert importlib.util.find_spec(name) is not None, "OAuth 어댑터 구현 필요"
    return importlib.import_module(name)


class Session:
    def __init__(self, response=None, failure=None):
        self.response = response or Response(json.dumps(dict(access_token="synthetic-token",
            token_type="Bearer", expires_in=86400)).encode())
        self.failure = failure
        self.requests, self.closed = [], False
        self._retry_connection = True

    def request(self, method, url, **kwargs):
        self.requests.append((method, url, kwargs))
        if self.failure:
            raise self.failure
        return self.response

    async def close(self):
        self.closed = True


def make(*, session=None, authorize=None, loader=None, max_issues=1, factory=None):
    mod = api()
    session = session or Session()
    issuer = mod.OAuthIssuer(credential_loader=loader or (lambda: mod.Credentials("synthetic-id", "synthetic-secret")),
        authorize=authorize or (lambda operation, *, deadline: deadline), limits=limits(),
        max_issues=max_issues, session_factory=factory or (lambda: session), clock=lambda: 0)
    return issuer, session


def test_fixed_form_post_and_independent_issue_cap():
    issuer, session = make()
    async def run():
        assert (await issuer.issue(operation="renewal", deadline=10))["access_token"] == "synthetic-token"
        with pytest.raises(api().TossRequestError, match="auth_unavailable"):
            await issuer.issue(operation="renewal", deadline=10)
        await issuer.close()
    asyncio.run(run())
    assert len(session.requests) == 1
    method, url, args = session.requests[0]
    assert (method, url) == ("POST", "https://openapi.tossinvest.com/oauth2/token")
    assert args["data"] == dict(grant_type="client_credentials", client_id="synthetic-id", client_secret="synthetic-secret")
    assert args["headers"] == {"Content-Type": "application/x-www-form-urlencoded", "Accept-Encoding": "identity"}
    assert args["ssl"] is True and args["allow_redirects"] is False
    assert args["auto_decompress"] is False
    assert session._retry_connection is False and session.closed


def test_credentials_are_lazy_and_authority_rechecked_after_factory():
    events = []
    session = Session()
    def authorize(operation, *, deadline):
        events.append("authorize")
        if "factory" in events:
            raise ValueError("synthetic-secret")
        return deadline
    def loader():
        events.append("credentials")
        return api().Credentials("synthetic-id", "synthetic-secret")
    def factory():
        events.append("factory")
        return session
    issuer, _ = make(authorize=authorize, loader=loader, factory=factory)
    assert events == []
    async def run():
        with pytest.raises(api().TossRequestError):
            await issuer.issue(operation="renewal", deadline=10)
        await issuer.close()
    asyncio.run(run())
    assert events == ["authorize", "credentials", "factory", "authorize"]
    assert session.requests == []


@pytest.mark.parametrize("operation,deadline", [("query", 10), ("bootstrap", 0)])
def test_rejected_context_never_loads_keys(operation, deadline):
    calls = []
    issuer, session = make(loader=lambda: calls.append("key"))
    async def run():
        with pytest.raises(api().TossRequestError):
            await issuer.issue(operation=operation, deadline=deadline)
        await issuer.close()
    asyncio.run(run())
    assert calls == session.requests == []


@pytest.mark.parametrize("status", [302, 400, 401, 403, 429, 500])
def test_non_success_never_retries_or_exposes_provider_text(status):
    issuer, session = make(session=Session(Response(b'{"error":"synthetic-secret"}', status=status)))
    async def run():
        with pytest.raises(api().TossRequestError) as error:
            await issuer.issue(operation="bootstrap", deadline=10)
        assert "synthetic-secret" not in "".join(traceback.format_exception(error.value))
        await issuer.close()
    asyncio.run(run())
    assert len(session.requests) == 1


@pytest.mark.parametrize("payload", [{"result": {"access_token": "synthetic-token"}},
    {"access_token": "synthetic-token", "token_type": "Bearer", "expires_in": True},
    {"access_token": "synthetic-token", "token_type": "Bearer", "expires_in": 0},
    {"access_token": "synthetic-token\n", "token_type": "Bearer", "expires_in": 30}])
def test_malformed_oauth_success_is_not_returned(payload):
    issuer, session = make(session=Session(Response(json.dumps(payload).encode())))
    async def run():
        with pytest.raises(api().TossRequestError, match="malformed_response"):
            await issuer.issue(operation="renewal", deadline=10)
        await issuer.close()
    asyncio.run(run())
    assert len(session.requests) == 1


def test_credentials_repr_loader_and_transport_errors_are_redacted():
    mod = api()
    assert "synthetic" not in repr(mod.Credentials("synthetic-id", "synthetic-secret"))
    assert "client_secret" not in repr(mod.Credentials("synthetic-id", "synthetic-secret"))
    def fail():
        raise ValueError("synthetic-secret")
    async def run():
        for issuer, session in [make(loader=fail), make(session=Session(failure=ValueError("synthetic-secret")))]:
            with pytest.raises(mod.TossRequestError) as error:
                await issuer.issue(operation="renewal", deadline=10)
            assert "synthetic-secret" not in "".join(traceback.format_exception(error.value))
            assert len(session.requests) <= 1
            await issuer.close()
    asyncio.run(run())


def test_environment_loader_only_reads_supplied_mapping_and_validates():
    assert api().environment_credentials(environ={"TOSS_CLIENT_ID": "synthetic-id", "TOSS_CLIENT_SECRET": "synthetic-secret"}).client_id == "synthetic-id"
    with pytest.raises(api().TossRequestError, match="auth_unavailable"):
        api().environment_credentials(environ={})


def test_cancellation_closes_active_response_without_orphan_request():
    async def run():
        response = Response(block=True)
        issuer, session = make(session=Session(response))
        task = asyncio.create_task(issuer.issue(operation="renewal", deadline=10))
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await issuer.close()
        assert response.exited and session.closed and len(session.requests) == 1
    asyncio.run(run())


def test_spec_example_ttl_is_not_an_undocumented_maximum():
    session = Session(Response(b'{"access_token":"synthetic-token","token_type":"Bearer","expires_in":86401}'))
    issuer, _ = make(session=session)
    async def run():
        assert (await issuer.issue(operation="renewal", deadline=10))["expires_in"] == 86401
        await issuer.close()
    asyncio.run(run())


def test_real_session_configuration_disables_env_cookies_and_decompression(monkeypatch):
    mod = api()
    session, created = Session(), []
    def factory(**kwargs):
        created.append(kwargs)
        return session
    monkeypatch.setattr(mod.aiohttp, "ClientSession", factory)
    issuer = mod.OAuthIssuer(credential_loader=lambda: mod.Credentials("synthetic-id", "synthetic-secret"),
        authorize=lambda operation, *, deadline: deadline, limits=limits(), max_issues=1,
        clock=lambda: 0)
    assert created == []
    async def run():
        await issuer.issue(operation="renewal", deadline=10)
        await issuer.close()
    asyncio.run(run())
    assert created[0]["trust_env"] is False
    assert created[0]["auto_decompress"] is False
    assert isinstance(created[0]["cookie_jar"], mod.aiohttp.DummyCookieJar)


def test_oauth_close_survives_repeated_cancellation():
    async def run():
        entered, release = asyncio.Event(), asyncio.Event()
        class SlowSession(Session):
            async def close(self):
                entered.set()
                await release.wait()
                self.closed = True
        issuer, session = make(session=SlowSession())
        await issuer.issue(operation="renewal", deadline=10)
        task = asyncio.create_task(issuer.close())
        await entered.wait()
        for _ in range(2):
            task.cancel()
            await asyncio.sleep(0)
        retry = asyncio.create_task(issuer.close())
        release.set()
        results = await asyncio.gather(task, retry, return_exceptions=True)
        assert isinstance(results[0], asyncio.CancelledError) and results[1] is None
        assert session.closed
    asyncio.run(run())


def test_first_authority_denial_does_not_load_credentials_or_create_session():
    calls = []
    def deny(operation, *, deadline):
        raise ValueError("synthetic-secret")
    issuer, _ = make(authorize=deny, loader=lambda: calls.append("credentials"),
                     factory=lambda: calls.append("session"))
    async def run():
        with pytest.raises(api().TossRequestError, match="auth_unavailable"):
            await issuer.issue(operation="bootstrap", deadline=10)
        await issuer.close()
    asyncio.run(run())
    assert calls == []


@pytest.mark.parametrize("failure", ["timeout", "save", "cancel"])
def test_manager_preserves_unknown_after_adapter_or_storage_failure(tmp_path, monkeypatch, failure):
    from src.data.providers.toss.token import TokenManager
    from src.data.providers.toss.token_store import SecureTokenStore, TokenError
    import time
    async def run():
        session = Session(Response(block=True)) if failure == "cancel" else Session(
            failure=TimeoutError("synthetic-secret") if failure == "timeout" else None)
        issuer, _ = make(session=session, max_issues=10)
        store = SecureTokenStore(tmp_path / "toss", "synthetic-client")
        async def issue():
            return await issuer.issue(operation="bootstrap", deadline=10)
        manager = TokenManager(store, role="issuer", issuer=issue, enabled=True)
        if failure == "save":
            def broken_save(record):
                raise OSError("synthetic-secret")
            monkeypatch.setattr(store, "save", broken_save)
        # 30초는 만료 원인이 아니라 고착 감시용 — 부하로 상태 기록 전에 만료되면 issuance_unknown 이 안 남는다.
        task = asyncio.create_task(manager.bootstrap(approved=True, deadline=time.monotonic() + 30))
        if failure == "cancel":
            while not session.requests:
                await asyncio.sleep(0)
            task.cancel()
        with pytest.raises(asyncio.CancelledError if failure == "cancel" else TokenError):
            await task
        assert store.load_state()["kind"] == "issuance_unknown"
        restarted = TokenManager(store, role="issuer", issuer=issue, enabled=True)
        with pytest.raises(TokenError):
            await restarted.bootstrap(approved=True, deadline=time.monotonic() + 30)
        assert len(session.requests) == 1
        await issuer.close()
    asyncio.run(run())


def test_missing_library_retry_control_cannot_send_even_on_second_call():
    session = Session()
    del session._retry_connection
    issuer, _ = make(session=session, max_issues=2)
    async def run():
        for _ in range(2):
            with pytest.raises(api().TossRequestError, match="unsupported"):
                await issuer.issue(operation="renewal", deadline=10)
        await issuer.close()
    asyncio.run(run())
    assert session.requests == []


def test_issue_budget_probe_is_pure_and_false_after_possible_post_or_close():
    calls = []
    issuer, session = make(loader=lambda: calls.append("credentials"))
    assert issuer.can_issue() is True
    assert calls == [] and session.requests == []
    asyncio.run(issuer.close())
    assert issuer.can_issue() is False
    assert calls == [] and session.requests == []
    issuer, session = make(session=Session(failure=TimeoutError("synthetic-failure")))
    async def scenario():
        assert issuer.can_issue() is True
        with pytest.raises(api().TossRequestError):
            await issuer.issue(operation="renewal", deadline=10)
        assert issuer.can_issue() is False
        await issuer.close()
    asyncio.run(scenario())
    assert len(session.requests) == 1
