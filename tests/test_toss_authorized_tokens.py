"""승인 문맥, bootstrap 소진, 만료 뒤 폐기 영속화 회귀."""
import asyncio
import importlib
from datetime import timedelta

import pytest

from test_toss_live_authority import authority_fixture
from src.data.providers.toss.token import TokenManager
from src.data.providers.toss.token_store import SecureTokenStore, TokenError, TokenRecord


def setup(tmp_path, monkeypatch, *, role="issuer", issue=None):
    approval, kwargs, _, stamp, ticks = authority_fixture(tmp_path, monkeypatch, role=role)
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
