"""승인 문맥, bootstrap 소진, 만료 뒤 폐기 영속화 회귀."""
import asyncio
import importlib
from datetime import timedelta

import pytest

from test_toss_live_authority import authority_fixture, plan_document, raw
from src.data.providers.toss.token import TokenManager
from src.data.providers.toss.token_store import SecureTokenStore, TokenError, TokenRecord


def setup(tmp_path, monkeypatch, *, role="issuer", issue=None, capabilities=None):
    approval, kwargs, _, stamp, ticks = authority_fixture(tmp_path, monkeypatch, role=role, capabilities=capabilities)
    authority = approval.load_authority(**kwargs)
    try:
        mod = importlib.import_module("src.data.providers.toss.authorized_tokens")
    except ModuleNotFoundError:
        pytest.fail("승인 토큰 wrapper 구현 없음", pytrace=False)
    sent = []
    async def fake_issue(*, operation, deadline):
        sent.append((operation, deadline))
        return dict(access_token="synthetic-new", token_type="Bearer", expires_in=86400)
    store = SecureTokenStore(tmp_path / "tokens", "synthetic-client")
    issuer = mod.make_authorized_issuer(authority, issue or fake_issue)
    manager = TokenManager(store, role=role, issuer=issuer, enabled=True, clock=lambda: ticks[0], now=lambda: stamp[0])
    provider = mod.AuthorizedTokenProvider(manager, authority)
    return mod, approval, authority, store, manager, provider, sent, stamp, ticks


def save_old(store, stamp, *, ttl=100):
    store.save(TokenRecord(access_token="synthetic-old", issued_at=stamp[0] - timedelta(hours=1),
        expires_at=stamp[0] + timedelta(seconds=ttl), generation=1, client_identity="synthetic-client"))


def test_reader_uses_cache_without_renewal_and_issuer_renews(tmp_path, monkeypatch):
    _, _, _, store, _, provider, sent, stamp, _ = setup(tmp_path, monkeypatch, role="reader")
    save_old(store, stamp)
    assert asyncio.run(provider.get_token(deadline=130)) == "synthetic-old"
    assert sent == []


def test_normal_get_and_expired_recovery_bind_renewal(tmp_path, monkeypatch):
    _, _, _, store, _, provider, sent, stamp, _ = setup(tmp_path, monkeypatch)
    save_old(store, stamp)
    assert asyncio.run(provider.get_token(deadline=130)) == "synthetic-new"
    assert sent == [("renewal", 130)]
    save_old(store, stamp, ttl=-1)
    # 이전 발급의 ready generation을 보존하므로 별도 토큰으로 재발급하지 않는다.
    with pytest.raises(TokenError):
        asyncio.run(provider.recover("expired-token", "synthetic-old", deadline=130))
    assert len(sent) == 1


def test_bootstrap_is_durably_consumed_across_wrapper_restart(tmp_path, monkeypatch):
    mod, approval, authority, store, manager, provider, sent, _, _ = setup(tmp_path, monkeypatch)
    assert asyncio.run(provider.bootstrap(deadline=130)) == "synthetic-new"
    restarted_manager = TokenManager(SecureTokenStore(store.directory, store.client_identity),
        role="issuer", issuer=manager.issuer, enabled=True, clock=manager.clock, now=manager.now)
    restarted = mod.AuthorizedTokenProvider(restarted_manager, authority)
    # 캐시/state를 지운 뒤에도 별도 승인 소진 기록을 우회하지 못한다.
    (store.directory / "toss_token.json").unlink()
    (store.directory / "toss_auth_state.json").unlink()
    with pytest.raises((TokenError, approval.ApprovalError)):
        asyncio.run(restarted.bootstrap(deadline=130))
    assert sent == [("bootstrap", 130)]
    assert (store.directory / "toss_bootstrap_used.json").stat().st_mode & 0o777 == 0o600


def test_bootstrap_failure_remains_unknown_and_no_second_issue(tmp_path, monkeypatch):
    sent = []
    async def fail(**kwargs):
        sent.append(kwargs)
        raise RuntimeError("synthetic-secret")
    mod, approval, authority, store, manager, provider, _, _, _ = setup(tmp_path, monkeypatch, issue=fail)
    with pytest.raises(TokenError, match="issuance_unknown") as caught:
        asyncio.run(provider.bootstrap(deadline=130))
    assert "synthetic-secret" not in str(caught.value)
    with pytest.raises((TokenError, approval.ApprovalError)):
        asyncio.run(mod.AuthorizedTokenProvider(manager, authority).bootstrap(deadline=130))
    assert len(sent) == 1
    assert store.load_state()["kind"] == "issuance_unknown"


@pytest.mark.parametrize("stopped", [False, True])
def test_revocation_persists_before_expired_or_stopped_authority_check(tmp_path, monkeypatch, stopped):
    _, approval, authority, store, _, provider, sent, _, ticks = setup(tmp_path, monkeypatch)
    if stopped:
        authority.stop()
    else:
        ticks[0] = 200
    with pytest.raises(approval.ApprovalError):
        asyncio.run(provider.recover("token-revoked", "synthetic-old", deadline=300))
    assert len(store.load_revocations()) == 1
    provider.observe_revocation("synthetic-second")
    assert len(store.load_revocations()) == 2
    assert sent == []


def test_context_absent_and_child_task_inheritance_cannot_issue(tmp_path, monkeypatch):
    mod, approval, authority, _, manager, provider, sent, _, _ = setup(tmp_path, monkeypatch)
    async def scenario():
        with pytest.raises(approval.ApprovalError):
            await manager.issuer()
        original = manager.get_token
        async def leaking_get(*, deadline):
            async def child():
                return await manager.issuer()
            with pytest.raises(approval.ApprovalError):
                await asyncio.create_task(child())
            raise TokenError("auth_unavailable")
        manager.get_token = leaking_get
        with pytest.raises(TokenError):
            await provider.get_token(deadline=130)
        manager.get_token = original
        with pytest.raises(approval.ApprovalError):
            await manager.issuer()
    asyncio.run(scenario())
    assert sent == []


def test_cancel_resets_context_and_preserves_unknown(tmp_path, monkeypatch):
    async def scenario():
        entered = asyncio.Event()
        async def issue(**kwargs):
            entered.set()
            await asyncio.Event().wait()
        _, approval, _, store, manager, provider, _, _, _ = setup(tmp_path, monkeypatch, issue=issue)
        task = asyncio.create_task(provider.bootstrap(deadline=130))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        with pytest.raises(approval.ApprovalError):
            await manager.issuer()
        assert store.load_state()["kind"] == "issuance_unknown"
    asyncio.run(scenario())


def test_expiry_while_waiting_for_token_lock_never_sends(tmp_path, monkeypatch):
    _, approval, _, store, _, provider, sent, stamp, ticks = setup(tmp_path, monkeypatch)
    save_old(store, stamp)
    async def scenario():
        async with store.lock(deadline=130, clock=lambda: ticks[0]):
            task = asyncio.create_task(provider.get_token(deadline=130))
            await asyncio.sleep(0)
            stamp[0] += timedelta(seconds=61)
        with pytest.raises((TokenError, approval.ApprovalError)):
            await task
    asyncio.run(scenario())
    assert sent == []


def test_revoked_same_token_never_mints(tmp_path, monkeypatch):
    _, _, _, store, _, provider, sent, stamp, _ = setup(tmp_path, monkeypatch)
    save_old(store, stamp)
    with pytest.raises(TokenError):
        asyncio.run(provider.recover("token-revoked", "synthetic-old", deadline=130))
    assert sent == []
    assert store.load_revocations()


def test_expired_recovery_uses_renewal_context(tmp_path, monkeypatch):
    _, _, _, store, _, provider, sent, stamp, _ = setup(tmp_path, monkeypatch)
    save_old(store, stamp, ttl=-1)
    assert asyncio.run(provider.recover("expired-token", "synthetic-old", deadline=130)) == "synthetic-new"
    assert sent == [("renewal", 130)]


def test_expiry_after_intent_preserves_unknown_without_sending(tmp_path, monkeypatch):
    _, _, _, store, _, provider, sent, stamp, _ = setup(tmp_path, monkeypatch)
    original = store.save_state
    def expire(kind, *args, **kwargs):
        original(kind, *args, **kwargs)
        if kind == "issuance_unknown":
            stamp[0] += timedelta(seconds=61)
    monkeypatch.setattr(store, "save_state", expire)
    with pytest.raises(TokenError, match="issuance_unknown"):
        asyncio.run(provider.bootstrap(deadline=130))
    assert sent == []
    assert store.load_state()["kind"] == "issuance_unknown"


def test_mismatched_authority_cannot_consume_issue_context(tmp_path, monkeypatch):
    mod, approval, authority, _, manager, provider, sent, _, _ = setup(tmp_path, monkeypatch)
    _, kwargs, *_ = authority_fixture(tmp_path, monkeypatch)
    other = approval.load_authority(**kwargs)
    async def issue(**kwargs):
        sent.append(kwargs)
    manager.issuer = mod.make_authorized_issuer(other, issue)
    with pytest.raises(TokenError, match="issuance_unknown"):
        asyncio.run(provider.bootstrap(deadline=130))
    assert sent == []


def test_marker_write_failure_prevents_issue(tmp_path, monkeypatch):
    _, _, _, store, _, provider, sent, _, _ = setup(tmp_path, monkeypatch)
    def fail(*args, **kwargs):
        raise TokenError("storage_failed")
    monkeypatch.setattr(store, "_publish_immutable", fail)
    with pytest.raises(TokenError, match="storage_failed"):
        asyncio.run(provider.bootstrap(deadline=130))
    assert sent == []


def test_direct_self_authored_authority_without_registry_never_stores_or_issues(tmp_path, monkeypatch):
    approval, kwargs, grant, _, _ = authority_fixture(tmp_path, monkeypatch)
    kwargs["registry_path"].unlink()
    mod = importlib.import_module("src.data.providers.toss.authorized_tokens")
    sent = []
    async def issue(**arguments):
        sent.append(arguments)
        return dict(access_token="synthetic-token", token_type="Bearer", expires_in=86400)
    store = SecureTokenStore(tmp_path / "tokens", "synthetic-client")
    with pytest.raises(approval.ApprovalError):
        authority = approval.ApprovedAuthority(
            approval.ObservationPlan.from_bytes(raw(plan_document())),
            approval.LiveObservationGrant.from_document(grant), clock=kwargs["clock"], now=kwargs["now"])
        manager = TokenManager(store, role="issuer", enabled=True,
            issuer=mod.make_authorized_issuer(authority, issue), clock=kwargs["clock"], now=kwargs["now"])
        asyncio.run(mod.AuthorizedTokenProvider(manager, authority).bootstrap(deadline=130))
    assert sent == []
    assert not store.directory.exists()


def test_query_only_issuer_uses_due_valid_cache_without_intent(tmp_path, monkeypatch):
    _, _, _, store, manager, provider, sent, stamp, _ = setup(tmp_path, monkeypatch,
        capabilities=dict(query=True, renewal=False, bootstrap=False))
    save_old(store, stamp)
    store.save_state("ready", 1)
    before = (store.directory / "toss_auth_state.json").read_bytes()
    assert asyncio.run(provider.get_token(deadline=130)) == "synthetic-old"
    assert (store.directory / "toss_auth_state.json").read_bytes() == before
    assert store.load().access_token == "synthetic-old"
    assert manager.role == "issuer"
    assert sent == []


@pytest.mark.parametrize("action", ["get", "recover"])
@pytest.mark.parametrize("ttl", [-1, 100])
def test_query_only_issuer_denial_never_creates_unknown(tmp_path, monkeypatch, action, ttl):
    _, approval, _, store, _, provider, sent, stamp, _ = setup(tmp_path, monkeypatch,
        capabilities=dict(query=True, renewal=False, bootstrap=False))
    save_old(store, stamp, ttl=ttl)
    store.save_state("ready", 1)
    before = (store.directory / "toss_auth_state.json").read_bytes()
    async def scenario():
        if action == "get":
            return await provider.get_token(deadline=130)
        return await provider.recover("expired-token", "synthetic-old", deadline=130)
    if action == "get" and ttl > 0:
        assert asyncio.run(scenario()) == "synthetic-old"
    else:
        with pytest.raises((TokenError, approval.ApprovalError)):
            asyncio.run(scenario())
    assert (store.directory / "toss_auth_state.json").read_bytes() == before
    assert store.load().access_token == "synthetic-old"
    assert sent == []


def test_query_only_issuer_keeps_revocation_generation_and_newer_cache_gate(tmp_path, monkeypatch):
    _, _, _, store, _, provider, sent, stamp, _ = setup(tmp_path, monkeypatch,
        capabilities=dict(query=True, renewal=False, bootstrap=False))
    store.save(TokenRecord(access_token="synthetic-newer", issued_at=stamp[0] - timedelta(hours=1),
        expires_at=stamp[0] + timedelta(seconds=100), generation=2, client_identity="synthetic-client"))
    assert asyncio.run(provider.get_token(deadline=130)) == "synthetic-newer"
    save_old(store, stamp)
    with pytest.raises(TokenError):
        asyncio.run(provider.recover("token-revoked", "synthetic-newer", deadline=130))
    assert {item["generation"] for item in store.load_revocations().values()} == {2}
    assert sent == []


def test_query_only_issuer_never_bypasses_unknown(tmp_path, monkeypatch):
    _, _, _, store, _, provider, sent, stamp, _ = setup(tmp_path, monkeypatch,
        capabilities=dict(query=True, renewal=False, bootstrap=False))
    save_old(store, stamp)
    store.save_state("issuance_unknown", 2)
    with pytest.raises(TokenError, match="issuance_unknown"):
        asyncio.run(provider.get_token(deadline=130))
    assert store.load_state()["kind"] == "issuance_unknown"
    assert sent == []


def test_query_only_issuer_recovers_with_different_valid_cache(tmp_path, monkeypatch):
    _, _, _, store, _, provider, sent, stamp, _ = setup(tmp_path, monkeypatch,
        capabilities=dict(query=True, renewal=False, bootstrap=False))
    save_old(store, stamp)
    assert asyncio.run(provider.recover("expired-token", "synthetic-other", deadline=130)) == "synthetic-old"
    assert store.load_state() is None
    assert sent == []


def test_query_only_view_shares_bootstrap_failure_block(tmp_path, monkeypatch):
    async def fail(**kwargs):
        raise RuntimeError("synthetic-failure")
    _, _, _, store, manager, provider, _, _, _ = setup(tmp_path, monkeypatch, issue=fail,
        capabilities=dict(query=True, renewal=False, bootstrap=True))
    with pytest.raises(TokenError, match="issuance_unknown"):
        asyncio.run(provider.bootstrap(deadline=130))
    assert manager._blocked == "issuance_unknown"
    assert provider._query_manager._blocked == "issuance_unknown"
    with pytest.raises(TokenError, match="issuance_unknown"):
        asyncio.run(provider.get_token(deadline=130))
    assert store.load_state()["kind"] == "issuance_unknown"


def oauth_budget_setup(tmp_path, monkeypatch, *, failure=None):
    from src.data.providers.toss.oauth import OAuthIssuer, Credentials
    from test_toss_oauth import Session
    from test_toss_http_body import Response, limits
    approval, kwargs, grant, stamp, ticks = authority_fixture(tmp_path, monkeypatch)
    grant["expires_at"] = (stamp[0] + timedelta(days=1)).isoformat()
    kwargs["registry_path"].write_bytes(raw(dict(schema_version=1, grants=[grant])))
    authority = approval.load_authority(**kwargs)
    session = Session(Response(b'{"access_token":"synthetic-budget-token","token_type":"Bearer","expires_in":120}'), failure=failure)
    oauth = OAuthIssuer(credential_loader=lambda: Credentials("synthetic-id", "synthetic-secret"),
        authorize=authority.require, limits=limits(), max_issues=1,
        session_factory=lambda: session, clock=lambda: ticks[0])
    mod = importlib.import_module("src.data.providers.toss.authorized_tokens")
    store = SecureTokenStore(tmp_path / "tokens", "synthetic-client")
    manager = TokenManager(store, role="issuer", issuer=mod.make_authorized_issuer(authority, oauth.issue),
        enabled=True, clock=lambda: ticks[0], now=lambda: stamp[0])
    provider = mod.AuthorizedTokenProvider(manager, authority, can_issue=oauth.can_issue)
    return oauth, session, store, provider, stamp, ticks


def test_spent_oauth_budget_keeps_valid_due_cache_ready(tmp_path, monkeypatch):
    oauth, session, store, provider, stamp, _ = oauth_budget_setup(tmp_path, monkeypatch)
    save_old(store, stamp)
    async def scenario():
        assert await provider.get_token(deadline=130) == "synthetic-budget-token"
        ready = (store.directory / "toss_auth_state.json").read_bytes()
        stamp[0] += timedelta(seconds=61)
        assert await provider.get_token(deadline=130) == "synthetic-budget-token"
        assert (store.directory / "toss_auth_state.json").read_bytes() == ready
        assert store.load().generation == 2
        await oauth.close()
    asyncio.run(scenario())
    assert len(session.requests) == 1


@pytest.mark.parametrize("action", ["get", "recover", "bootstrap"])
def test_spent_oauth_budget_denies_without_unknown_or_bootstrap_marker(tmp_path, monkeypatch, action):
    oauth, session, store, provider, stamp, _ = oauth_budget_setup(tmp_path, monkeypatch)
    save_old(store, stamp)
    async def scenario():
        await provider.get_token(deadline=130)
        ready = (store.directory / "toss_auth_state.json").read_bytes()
        stamp[0] += timedelta(seconds=121)
        with pytest.raises(TokenError):
            if action == "get":
                await provider.get_token(deadline=130)
            elif action == "recover":
                await provider.recover("expired-token", "synthetic-budget-token", deadline=130)
            else:
                await provider.bootstrap(deadline=130)
        assert (store.directory / "toss_auth_state.json").read_bytes() == ready
        assert not (store.directory / "toss_bootstrap_used.json").exists()
        await oauth.close()
    asyncio.run(scenario())
    assert len(session.requests) == 1


def test_oauth_budget_is_rechecked_after_waiting_for_issuer_lock(tmp_path, monkeypatch):
    oauth, session, store, provider, stamp, ticks = oauth_budget_setup(tmp_path, monkeypatch)
    save_old(store, stamp)
    async def scenario():
        async with store.lock(deadline=130, clock=lambda: ticks[0]):
            waiting = asyncio.create_task(provider.get_token(deadline=130))
            await asyncio.sleep(0)
            # 동일 worker의 다른 허용 발급이 마지막 슬롯을 소비한 상황을 재현.
            await oauth.issue(operation="renewal", deadline=130)
        assert await waiting == "synthetic-old"
        assert store.load_state() is None
        await oauth.close()
    asyncio.run(scenario())
    assert len(session.requests) == 1


def test_possible_post_failure_keeps_unknown_with_budget_guard(tmp_path, monkeypatch):
    oauth, session, store, provider, stamp, _ = oauth_budget_setup(tmp_path, monkeypatch,
        failure=TimeoutError("synthetic-failure"))
    save_old(store, stamp)
    async def scenario():
        with pytest.raises(TokenError, match="issuance_unknown"):
            await provider.get_token(deadline=130)
        with pytest.raises(TokenError, match="issuance_unknown"):
            await provider.get_token(deadline=130)
        assert store.load_state()["kind"] == "issuance_unknown"
        await oauth.close()
    asyncio.run(scenario())
    assert len(session.requests) == 1


def test_exhausted_bootstrap_budget_does_not_create_store(tmp_path, monkeypatch):
    oauth, session, store, provider, _, _ = oauth_budget_setup(tmp_path, monkeypatch)
    async def scenario():
        await oauth.issue(operation="renewal", deadline=130)
        with pytest.raises(TokenError):
            await provider.bootstrap(deadline=130)
        assert not store.directory.exists()
        await oauth.close()
    asyncio.run(scenario())
    assert len(session.requests) == 1


def test_budget_guard_does_not_bypass_stop_or_revocation_safety(tmp_path, monkeypatch):
    from src.data.providers.toss.approval import ApprovalError
    oauth, session, store, provider, stamp, _ = oauth_budget_setup(tmp_path, monkeypatch)
    save_old(store, stamp)
    async def scenario():
        await provider.get_token(deadline=130)
        provider.authority.stop()
        with pytest.raises(ApprovalError):
            await provider.get_token(deadline=130)
        with pytest.raises(ApprovalError):
            await provider.recover("token-revoked", "synthetic-budget-token", deadline=130)
        assert {item["generation"] for item in store.load_revocations().values()} == {2}
        await oauth.close()
    asyncio.run(scenario())
    assert len(session.requests) == 1
