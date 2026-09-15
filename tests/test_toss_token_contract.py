"""토큰 발급 금지·지속 회로 계약: 실제 저장소, 합성 발급기만 사용."""
import asyncio
import importlib
import time
from datetime import datetime, timedelta, timezone

import pytest


def modules():
    try:
        token = importlib.import_module("src.data.providers.toss.token")
        storage = importlib.import_module("src.data.providers.toss.token_store")
    except ModuleNotFoundError:
        pytest.fail("Toss token implementation is missing", pytrace=False)
    return token, storage


def setup(tmp_path, *, role="issuer", enabled=True, ttl=86400):
    token, storage = modules()
    store = storage.SecureTokenStore(tmp_path / "toss", "offline-client")
    issued = []

    async def issuer():
        issued.append("issued")
        return {"access_token": "synthetic-new-bearer", "expires_in": ttl, "token_type": "Bearer"}

    manager = token.TokenManager(store, role=role, issuer=issuer, enabled=enabled)
    return token, store, manager, issued


def run(awaitable):
    return asyncio.run(awaitable)


def deadline():
    return time.monotonic() + 2


def record(storage, *, value="synthetic-old-bearer", generation=1, expired=False):
    now = datetime.now(timezone.utc)
    return storage.TokenRecord(access_token=value, issued_at=now - timedelta(hours=1),
        expires_at=now + timedelta(seconds=-1 if expired else 7200), generation=generation,
        client_identity="offline-client", origin="https://openapi.tossinvest.com", schema_version=1)


def test_disabled_has_no_store_or_issuer_side_effect(tmp_path):
    token, store, manager, issued = setup(tmp_path, enabled=False)
    for action in (manager.get_token(deadline=deadline()),
                   manager.recover("token-revoked", "synthetic-old-bearer", deadline=deadline()),
                   manager.bootstrap(approved=True, deadline=deadline())):
        with pytest.raises(token.TokenError, match="disabled"):
            run(action)
    assert not store.directory.exists()
    assert issued == []


@pytest.mark.parametrize("cache", ["same", "missing", "corrupt"])
def test_revoked_cannot_mint_even_after_restart_and_expiry(tmp_path, cache):
    token, store, manager, issued = setup(tmp_path)
    _, storage = modules()
    if cache != "missing":
        store.save(record(storage, expired=True))
        if cache == "corrupt":
            (store.directory / "toss_token.json").write_text("broken")
    with pytest.raises(token.TokenError, match="auth_unavailable"):
        run(manager.recover("token-revoked", "synthetic-old-bearer", deadline=deadline()))
    for candidate in (manager, token.TokenManager(store, role="issuer", issuer=manager.issuer, enabled=True)):
        with pytest.raises(token.TokenError, match="auth_unavailable"):
            run(candidate.get_token(deadline=deadline()))
        with pytest.raises(token.TokenError, match="auth_unavailable"):
            run(candidate.bootstrap(approved=True, deadline=deadline()))
    assert issued == []
    assert "synthetic-old-bearer" not in repr(manager)


def test_revoked_recovers_only_newer_different_valid_cache(tmp_path):
    token, store, manager, issued = setup(tmp_path)
    _, storage = modules()
    store.save(record(storage))
    with pytest.raises(token.TokenError, match="auth_unavailable"):
        run(manager.recover("token-revoked", "synthetic-old-bearer", deadline=deadline()))
    store.save(record(storage, value="synthetic-other", generation=1))
    with pytest.raises(token.TokenError, match="auth_unavailable"):
        run(manager.get_token(deadline=deadline()))
    store.save(record(storage, value="synthetic-other", generation=2))
    assert run(manager.get_token(deadline=deadline())) == "synthetic-other"
    assert issued == []


@pytest.mark.parametrize("error", ["token-revoked", "expired-token", "invalid-token", "403", "401"])
@pytest.mark.parametrize("cache", ["missing", "expired", "corrupt"])
def test_reader_never_issues(tmp_path, error, cache):
    token, store, manager, issued = setup(tmp_path, role="reader")
    _, storage = modules()
    if cache != "missing":
        store.save(record(storage, expired=True))
        if cache == "corrupt":
            (store.directory / "toss_token.json").write_text("broken")
    with pytest.raises(token.TokenError):
        run(manager.recover(error, "synthetic-old-bearer", deadline=deadline()))
    with pytest.raises(token.TokenError):
        run(manager.bootstrap(approved=True, deadline=deadline()))
    assert issued == []


def test_bootstrap_requires_approval_and_missing_cache(tmp_path):
    token, store, manager, issued = setup(tmp_path)
    with pytest.raises(token.TokenError, match="auth_unavailable"):
        run(manager.get_token(deadline=deadline()))
    with pytest.raises(token.TokenError, match="approval_required"):
        run(manager.bootstrap(approved=False, deadline=deadline()))
    assert issued == []
    assert run(manager.bootstrap(approved=True, deadline=deadline())) == "synthetic-new-bearer"
    assert run(manager.bootstrap(approved=True, deadline=deadline())) == "synthetic-new-bearer"
    assert issued == ["issued"]


def test_short_lifetime_does_not_rotate_on_every_read(tmp_path):
    _, _, manager, issued = setup(tmp_path, ttl=120)
    assert run(manager.bootstrap(approved=True, deadline=deadline())) == "synthetic-new-bearer"
    for _ in range(4):
        assert run(manager.get_token(deadline=deadline())) == "synthetic-new-bearer"
    assert len(issued) == 1


@pytest.mark.parametrize("ttl", [True, False, 0, -1, float("nan"), float("inf"), "3600"])
def test_bad_issuance_response_persists_unknown(tmp_path, ttl):
    token, store, manager, issued = setup(tmp_path, ttl=ttl)
    with pytest.raises(token.TokenError, match="issuance_unknown"):
        run(manager.bootstrap(approved=True, deadline=deadline()))
    restarted = token.TokenManager(store, role="issuer", issuer=manager.issuer, enabled=True)
    for action in (restarted.get_token(deadline=deadline()), restarted.bootstrap(approved=True, deadline=deadline())):
        with pytest.raises(token.TokenError, match="issuance_unknown"):
            run(action)
    assert len(issued) == 1


def test_cancellation_persists_intent_and_releases_lock(tmp_path):
    token, store, _, issued = setup(tmp_path)

    async def scenario():
        entered = asyncio.Event()
        async def issuer():
            issued.append("issued")
            entered.set()
            await asyncio.Event().wait()
        manager = token.TokenManager(store, role="issuer", issuer=issuer, enabled=True)
        task = asyncio.create_task(manager.bootstrap(approved=True, deadline=deadline()))
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        with pytest.raises(token.TokenError, match="issuance_unknown"):
            await manager.get_token(deadline=deadline())
    run(scenario())
    assert issued == ["issued"]


def test_future_dated_cache_never_triggers_issuance(tmp_path):
    from dataclasses import replace
    token, store, manager, issued = setup(tmp_path)
    _, storage = modules()
    future = datetime.now(timezone.utc) + timedelta(days=2)
    store.save(replace(record(storage), issued_at=future, expires_at=future + timedelta(hours=1)))
    with pytest.raises(token.TokenError, match="auth_unavailable"):
        run(manager.get_token(deadline=deadline()))
    assert issued == []


def test_server_expiry_renews_aged_same_token(tmp_path):
    _, store, manager, issued = setup(tmp_path)
    _, storage = modules()
    store.save(record(storage))
    assert run(manager.recover("expired-token", "synthetic-old-bearer", deadline=deadline())) == "synthetic-new-bearer"
    assert issued == ["issued"]


@pytest.mark.parametrize("error", ["invalid-token", "403", "401", "unexpected"])
def test_issuer_never_mints_on_nonrenewable_error(tmp_path, error):
    token, store, manager, issued = setup(tmp_path)
    _, storage = modules()
    store.save(record(storage, expired=True))
    with pytest.raises(token.TokenError, match="auth_unavailable"):
        run(manager.recover(error, "synthetic-old-bearer", deadline=deadline()))
    assert issued == []


def test_timeout_persists_unknown_without_exception_secret(tmp_path):
    import traceback
    token, store, _, issued = setup(tmp_path)
    async def issuer():
        issued.append("issued")
        await asyncio.sleep(2)
    manager = token.TokenManager(store, role="issuer", issuer=issuer, enabled=True)
    with pytest.raises(token.TokenError, match="issuance_unknown"):
        run(manager.bootstrap(approved=True, deadline=time.monotonic() + 0.05))
    restarted = token.TokenManager(store, role="issuer", issuer=issuer, enabled=True)
    with pytest.raises(token.TokenError, match="issuance_unknown") as caught:
        run(restarted.get_token(deadline=deadline()))
    assert "synthetic" not in "".join(traceback.format_exception(caught.value))
    assert issued == ["issued"]


def test_issuer_exception_is_sanitized_and_persistent(tmp_path, caplog):
    import traceback
    token, store, _, issued = setup(tmp_path)
    secret = "FAKE_ISSUER_EXCEPTION_BEARER"
    async def issuer():
        issued.append("issued")
        raise RuntimeError(secret)
    manager = token.TokenManager(store, role="issuer", issuer=issuer, enabled=True)
    with pytest.raises(token.TokenError, match="issuance_unknown") as caught:
        run(manager.bootstrap(approved=True, deadline=deadline()))
    assert secret not in "".join(traceback.format_exception(caught.value)) + caplog.text + repr(manager)
    with pytest.raises(token.TokenError, match="issuance_unknown"):
        run(token.TokenManager(store, role="issuer", issuer=issuer, enabled=True).get_token(deadline=deadline()))
    assert issued == ["issued"]


def test_busy_lock_deadline_and_cancellation_leave_event_loop_running(tmp_path):
    token, store, manager, issued = setup(tmp_path)
    async def scenario():
        ticks = []
        async with store.lock(deadline=deadline()):
            task = asyncio.create_task(manager.bootstrap(approved=True, deadline=time.monotonic() + 0.06))
            for _ in range(3):
                await asyncio.sleep(0.005)
                ticks.append(1)
            with pytest.raises(token.TokenError, match="deadline_exceeded"):
                await task
            task = asyncio.create_task(manager.bootstrap(approved=True, deadline=deadline()))
            await asyncio.sleep(0.02)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert ticks == [1, 1, 1]
        assert await manager.bootstrap(approved=True, deadline=deadline()) == "synthetic-new-bearer"
    run(scenario())
    assert issued == ["issued"]


def _process_renew(directory, barrier, results):
    # spawn 자식에도 운영 경로/외부 네트워크 격리 가드를 설치한다.
    import conftest  # noqa: F401
    from pathlib import Path
    token, storage = modules()
    store = storage.SecureTokenStore(Path(directory), "offline-client")
    async def issuer():
        results.put("issued")
        await asyncio.sleep(0.1)
        return {"access_token": "synthetic-process-bearer", "expires_in": 86400, "token_type": "Bearer"}
    manager = token.TokenManager(store, role="issuer", issuer=issuer, enabled=True)
    barrier.wait(timeout=3)
    results.put(run(manager.get_token(deadline=time.monotonic() + 3)))


def test_two_processes_share_one_renewal(tmp_path):
    import multiprocessing
    _, store, _, _ = setup(tmp_path)
    _, storage = modules()
    store.save(record(storage, expired=True))
    ctx = multiprocessing.get_context("spawn")
    barrier = ctx.Barrier(2)
    results = ctx.Queue()
    processes = [ctx.Process(target=_process_renew, args=(str(store.directory), barrier, results)) for _ in range(2)]
    try:
        for process in processes:
            process.start()
        for process in processes:
            process.join(timeout=5)
            assert process.exitcode == 0
        output = [results.get(timeout=1) for _ in range(3)]
        assert output.count("issued") == 1
        assert output.count("synthetic-process-bearer") == 2
        assert store.load().generation == 2
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(timeout=2)
        results.close()
        results.join_thread()


def _process_crash(directory):
    import conftest  # noqa: F401
    import os
    from pathlib import Path
    token, storage = modules()
    async def issuer():
        os._exit(17)
    run(token.TokenManager(storage.SecureTokenStore(Path(directory), "offline-client"),
        role="issuer", issuer=issuer, enabled=True).bootstrap(approved=True, deadline=deadline()))


def test_process_death_after_intent_never_automatically_reissues(tmp_path):
    import multiprocessing
    token, store, manager, issued = setup(tmp_path)
    process = multiprocessing.get_context("spawn").Process(target=_process_crash, args=(str(store.directory),))
    try:
        process.start()
        process.join(timeout=5)
        assert process.exitcode == 17
        with pytest.raises(token.TokenError, match="issuance_unknown"):
            run(manager.bootstrap(approved=True, deadline=deadline()))
        assert issued == []
    finally:
        if process.is_alive():
            process.terminate()
            process.join(timeout=2)


def test_ready_generation_rejects_rolled_back_cache(tmp_path):
    token, store, manager, issued = setup(tmp_path)
    _, storage = modules()
    store.save(record(storage, generation=4))
    store.save_state("ready", 5)
    with pytest.raises(token.TokenError, match="auth_unavailable"):
        run(manager.get_token(deadline=deadline()))
    assert issued == []


def test_fresh_server_expired_token_cannot_loop_issuance(tmp_path):
    token, _, manager, issued = setup(tmp_path, ttl=120)
    run(manager.bootstrap(approved=True, deadline=deadline()))
    with pytest.raises(token.TokenError, match="auth_unavailable"):
        run(manager.recover("expired-token", "synthetic-new-bearer", deadline=deadline()))
    assert issued == ["issued"]


def test_injected_time_renews_once_at_threshold(tmp_path):
    from dataclasses import replace
    token, store, manager, issued = setup(tmp_path)
    _, storage = modules()
    now = datetime(2026, 9, 16, tzinfo=timezone.utc)
    store.save(replace(record(storage), issued_at=now - timedelta(hours=22),
        expires_at=now + timedelta(minutes=30)))
    manager.now = lambda: now
    assert run(manager.get_token(deadline=deadline())) == "synthetic-new-bearer"
    assert run(manager.get_token(deadline=deadline())) == "synthetic-new-bearer"
    assert issued == ["issued"]


def test_very_short_expiry_cannot_bypass_minimum_refresh_interval(tmp_path):
    token, _, manager, issued = setup(tmp_path, ttl=2)
    now = datetime(2026, 9, 16, tzinfo=timezone.utc)
    manager.now = lambda: now
    run(manager.bootstrap(approved=True, deadline=deadline()))
    now += timedelta(seconds=3)
    with pytest.raises(token.TokenError, match="auth_unavailable"):
        run(manager.get_token(deadline=deadline()))
    assert issued == ["issued"]
    now += timedelta(seconds=57)
    assert run(manager.get_token(deadline=deadline())) == "synthetic-new-bearer"
    assert issued == ["issued", "issued"]
