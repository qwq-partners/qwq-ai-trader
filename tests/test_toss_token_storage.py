"""보안 파일 저장의 외부 계약. 운영 경로를 사용하지 않는다."""
import os
import stat

import pytest

from test_toss_token_contract import modules, record


def test_store_permissions_atomic_replacement_and_no_bearer_repr(tmp_path):
    token, storage = modules()
    store = storage.SecureTokenStore(tmp_path / "toss", "offline-client")
    original_umask = os.umask(0o022)
    try:
        store.save(record(storage))
        store.save(record(storage, generation=2, value="synthetic-other"))
    finally:
        os.umask(original_umask)
    assert store.load().access_token == "synthetic-other"
    assert stat.S_IMODE(store.directory.stat().st_mode) == 0o700
    assert stat.S_IMODE((store.directory / "toss_token.json").stat().st_mode) == 0o600
    assert "synthetic-other" not in repr(store.load())
    assert "synthetic" not in repr(token.TokenError("synthetic-other"))


def test_shared_parent_permissions_are_preserved(tmp_path):
    _, storage = modules()
    tmp_path.chmod(0o755)
    storage.SecureTokenStore(tmp_path / "toss", "offline-client").save(record(storage))
    assert stat.S_IMODE(tmp_path.stat().st_mode) == 0o755


@pytest.mark.parametrize("target", ["directory", "token", "ancestor"])
def test_symlink_rejected_without_changing_target(tmp_path, target):
    token, storage = modules()
    destination = tmp_path / "destination"
    destination.mkdir(mode=0o700)
    directory = tmp_path / "toss"
    if target == "directory":
        directory.symlink_to(destination, target_is_directory=True)
    elif target == "ancestor":
        alias = tmp_path / "alias"
        alias.symlink_to(destination, target_is_directory=True)
        directory = alias / "toss"
    else:
        directory.mkdir(mode=0o700)
        (directory / "toss_token.json").symlink_to(destination / "secret")
    with pytest.raises(token.TokenError, match="unsafe_storage"):
        storage.SecureTokenStore(directory, "offline-client").save(record(storage))
    assert list(destination.iterdir()) == []


@pytest.mark.parametrize("field,value", [("client_identity", "other"), ("origin", "https://evil.invalid"),
    ("schema_version", 2), ("schema_version", True), ("generation", 0), ("generation", True),
    ("issued_at", "2026-01-01T00:00:00"), ("expires_at", "not-a-date")])
def test_untrusted_json_is_not_returned(tmp_path, field, value):
    import json
    token, storage = modules()
    store = storage.SecureTokenStore(tmp_path / "toss", "offline-client")
    store.save(record(storage))
    path = store.directory / "toss_token.json"
    data = json.loads(path.read_text())
    data[field] = value
    path.write_text(json.dumps(data))
    with pytest.raises(token.TokenError):
        store.load()


def test_oversize_cache_is_rejected(tmp_path):
    token, storage = modules()
    store = storage.SecureTokenStore(tmp_path / "toss", "offline-client")
    store.save(record(storage))
    (store.directory / "toss_token.json").write_bytes(b" " * 70000)
    with pytest.raises(token.TokenError):
        store.load()


@pytest.mark.parametrize("target", ["directory", "token", "lock"])
def test_wrong_owner_is_rejected(tmp_path, monkeypatch, target):
    from test_toss_token_contract import run, deadline
    token, storage = modules()
    store = storage.SecureTokenStore(tmp_path / "toss", "offline-client")
    store.save(record(storage))
    if target == "lock":
        async def create():
            async with store.lock(deadline=deadline()):
                pass
        run(create())
    path = store.directory if target == "directory" else store.directory / ("toss_token.json" if target == "token" else "toss_token.lock")
    inode = path.stat().st_ino
    original = os.fstat
    def wrong_owner(fd):
        data = original(fd)
        if data.st_ino == inode:
            values = list(data)
            values[4] += 1
            return os.stat_result(values)
        return data
    monkeypatch.setattr(os, "fstat", wrong_owner)
    with pytest.raises(token.TokenError, match="unsafe_storage"):
        if target == "lock":
            run(create())
        else:
            store.load()


@pytest.mark.parametrize("failure", ["write", "replace", "file_fsync", "directory_fsync"])
def test_publish_failure_is_reported_and_existing_token_survives_when_possible(tmp_path, monkeypatch, failure):
    token, storage = modules()
    store = storage.SecureTokenStore(tmp_path / "toss", "offline-client")
    store.save(record(storage))
    original_fsync = os.fsync
    def fail(*args, **kwargs):
        raise OSError("FAKE_STORAGE_SECRET")
    def fsync(fd):
        is_directory = stat.S_ISDIR(os.fstat(fd).st_mode)
        if is_directory == (failure == "directory_fsync"):
            fail()
        original_fsync(fd)
    with monkeypatch.context() as patch:
        if failure in {"write", "replace"}:
            patch.setattr(os, failure, fail)
        else:
            patch.setattr(os, "fsync", fsync)
        with pytest.raises(token.TokenError, match="storage_failed") as caught:
            store.save(record(storage, value="synthetic-replacement", generation=2))
    assert "FAKE_STORAGE_SECRET" not in repr(caught.value)
    expected = "synthetic-replacement" if failure == "directory_fsync" else "synthetic-old-bearer"
    assert store.load().access_token == expected
    assert not list(store.directory.glob(".token-*"))


def test_temporary_file_is_private_before_first_write(tmp_path, monkeypatch):
    _, storage = modules()
    store = storage.SecureTokenStore(tmp_path / "toss", "offline-client")
    observed = []
    original = os.write
    def observe(fd, payload):
        observed.append(stat.S_IMODE(os.fstat(fd).st_mode))
        return original(fd, payload)
    monkeypatch.setattr(os, "write", observe)
    store.save(record(storage))
    assert observed and set(observed) == {0o600}


def test_lock_inode_remains_fixed_across_token_replacement(tmp_path):
    from test_toss_token_contract import run, deadline
    _, storage = modules()
    store = storage.SecureTokenStore(tmp_path / "toss", "offline-client")
    async def scenario():
        async with store.lock(deadline=deadline()):
            before = (store.directory / "toss_token.lock").stat()
            store.save(record(storage))
            store.save(record(storage, generation=2))
        async with store.lock(deadline=deadline()):
            after = (store.directory / "toss_token.lock").stat()
        assert before.st_ino == after.st_ino
        assert stat.S_IMODE(after.st_mode) == 0o600
    run(scenario())


@pytest.mark.parametrize("phase", ["intent", "cache", "ready"])
def test_issuance_publish_failures_never_duplicate_issuance(tmp_path, monkeypatch, phase):
    from test_toss_token_contract import setup, run, deadline
    token, store, manager, issued = setup(tmp_path)
    original = store._write
    def fail(name, data):
        if ((phase == "intent" and data.get("kind") == "issuance_unknown")
                or (phase == "cache" and name == "toss_token.json")
                or (phase == "ready" and data.get("kind") == "ready")):
            raise token.TokenError("storage_failed")
        return original(name, data)
    monkeypatch.setattr(store, "_write", fail)
    with pytest.raises(token.TokenError):
        run(manager.bootstrap(approved=True, deadline=deadline()))
    if phase == "intent":
        assert issued == []
        return
    restarted = token.TokenManager(store, role="issuer", issuer=manager.issuer, enabled=True)
    with pytest.raises(token.TokenError, match="issuance_unknown"):
        run(restarted.get_token(deadline=deadline()))
    assert issued == ["issued"]


def test_ready_directory_fsync_failure_still_blocks_restart(tmp_path, monkeypatch):
    from test_toss_token_contract import setup, run, deadline
    token, store, manager, issued = setup(tmp_path)
    original_replace, original_fsync = os.replace, os.fsync
    states = []
    def replace(source, destination, **kwargs):
        original_replace(source, destination, **kwargs)
        if destination == "toss_auth_state.json":
            states.append(destination)
    def fsync(fd):
        if len(states) >= 2 and stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError("FAKE_POST_REPLACE_FAILURE")
        original_fsync(fd)
    with monkeypatch.context() as patch:
        patch.setattr(os, "replace", replace)
        patch.setattr(os, "fsync", fsync)
        with pytest.raises(token.TokenError, match="issuance_unknown"):
            run(manager.bootstrap(approved=True, deadline=deadline()))
    restarted = token.TokenManager(store, role="issuer", issuer=manager.issuer, enabled=True)
    with pytest.raises(token.TokenError, match="issuance_unknown"):
        run(restarted.get_token(deadline=deadline()))
    assert issued == ["issued"]


@pytest.mark.parametrize("target", ["token", "lock"])
def test_insecure_modes_and_hard_links_are_rejected(tmp_path, target):
    from test_toss_token_contract import run, deadline
    token, storage = modules()
    store = storage.SecureTokenStore(tmp_path / "toss", "offline-client")
    store.save(record(storage))
    async def lock():
        async with store.lock(deadline=deadline()):
            pass
    if target == "lock":
        run(lock())
    path = store.directory / ("toss_token.json" if target == "token" else "toss_token.lock")
    path.chmod(0o644)
    with pytest.raises(token.TokenError, match="unsafe_storage"):
        store.load() if target == "token" else run(lock())
    path.chmod(0o600)
    os.link(path, store.directory / "alias")
    with pytest.raises(token.TokenError, match="unsafe_storage"):
        store.load() if target == "token" else run(lock())


def test_lock_symlink_does_not_touch_target(tmp_path):
    from test_toss_token_contract import run, deadline
    token, storage = modules()
    store = storage.SecureTokenStore(tmp_path / "toss", "offline-client")
    store.save(record(storage))
    target = tmp_path / "untouched"
    (store.directory / "toss_token.lock").symlink_to(target)
    async def scenario():
        async with store.lock(deadline=deadline()):
            pytest.fail("unsafe lock acquired")
    with pytest.raises(token.TokenError, match="unsafe_storage"):
        run(scenario())
    assert not target.exists()


def test_issuance_records_pid_as_audit_metadata(tmp_path):
    import json
    from test_toss_token_contract import setup, run, deadline
    _, store, manager, _ = setup(tmp_path)
    run(manager.bootstrap(approved=True, deadline=deadline()))
    data = json.loads((store.directory / "toss_token.json").read_text())
    assert data.get("issuer_pid") == os.getpid()
    assert store.load().issuer_pid == os.getpid()


@pytest.mark.parametrize("pid", [None, True, False, 0, -1, 1.5, "123"])
def test_invalid_issuer_pid_is_rejected(tmp_path, pid):
    import json
    token, storage = modules()
    store = storage.SecureTokenStore(tmp_path / "toss", "offline-client")
    store.save(record(storage))
    path = store.directory / "toss_token.json"
    data = json.loads(path.read_text())
    if pid is None:
        data.pop("issuer_pid", None)
    else:
        data["issuer_pid"] = pid
    path.write_text(json.dumps(data))
    with pytest.raises(token.TokenError, match="invalid_cache"):
        store.load()


def test_matching_pid_cannot_grant_reader_issuance_authority(tmp_path):
    import json
    from test_toss_token_contract import setup, run, deadline
    token, store, manager, issued = setup(tmp_path, role="reader")
    _, storage = modules()
    store.save(record(storage, expired=True))
    path = store.directory / "toss_token.json"
    data = json.loads(path.read_text())
    data["issuer_pid"] = os.getpid()
    path.write_text(json.dumps(data))
    with pytest.raises(token.TokenError, match="auth_unavailable"):
        run(manager.get_token(deadline=deadline()))
    assert issued == []


def test_other_pid_does_not_prevent_authorized_issuer_renewal(tmp_path):
    import json
    from test_toss_token_contract import setup, run, deadline
    _, store, manager, issued = setup(tmp_path)
    _, storage = modules()
    store.save(record(storage, expired=True))
    path = store.directory / "toss_token.json"
    data = json.loads(path.read_text())
    data["issuer_pid"] = os.getpid() + 100000
    path.write_text(json.dumps(data))
    assert run(manager.get_token(deadline=deadline())) == "synthetic-new-bearer"
    assert issued == ["issued"]


@pytest.mark.parametrize("field", ["origin", "schema_version"])
def test_required_disk_schema_field_cannot_use_constructor_default(tmp_path, field):
    import json
    token, storage = modules()
    store = storage.SecureTokenStore(tmp_path / "toss", "offline-client")
    store.save(record(storage))
    path = store.directory / "toss_token.json"
    data = json.loads(path.read_text())
    data.pop(field)
    path.write_text(json.dumps(data))
    with pytest.raises(token.TokenError, match="invalid_cache"):
        store.load()


@pytest.mark.parametrize("field,value", [("kind", []), ("generation", True),
    ("failed_digest", "bad"), ("origin", "https://evil.invalid"), ("schema_version", True)])
def test_malformed_auth_state_fails_closed_with_safe_error(tmp_path, field, value):
    import json
    from test_toss_token_contract import setup, run, deadline
    token, store, manager, issued = setup(tmp_path)
    store.save_state("issuance_unknown", 1)
    path = store.directory / "toss_auth_state.json"
    data = json.loads(path.read_text())
    data[field] = value
    path.write_text(json.dumps(data))
    with pytest.raises(token.TokenError):
        run(manager.bootstrap(approved=True, deadline=deadline()))
    assert issued == []


@pytest.mark.parametrize("digest", ["", "f" * 63, "g" * 64])
def test_revoked_state_requires_valid_digest_across_restart_and_expiry(tmp_path, digest):
    import json
    from dataclasses import replace
    from datetime import datetime, timedelta, timezone
    from test_toss_token_contract import setup, run, deadline
    token, store, manager, issued = setup(tmp_path)
    _, storage = modules()
    now = datetime(2026, 9, 16, tzinfo=timezone.utc)
    manager.now = lambda: now
    first = replace(record(storage), issued_at=now - timedelta(hours=1), expires_at=now + timedelta(hours=1))
    store.save(first)
    with pytest.raises(token.TokenError, match="auth_unavailable"):
        run(manager.recover("token-revoked", first.access_token, deadline=deadline()))
    # 같은 bearer에 세대만 높여도 정상 지문이 있으면 절대 해제되지 않는다.
    store.save(replace(first, generation=2))
    with pytest.raises(token.TokenError, match="auth_unavailable"):
        run(manager.get_token(deadline=deadline()))
    path = store.directory / "toss_auth_state.json"
    data = json.loads(path.read_text())
    data["failed_digest"] = digest
    path.write_text(json.dumps(data))
    restarted = token.TokenManager(store, role="issuer", issuer=manager.issuer, enabled=True, now=lambda: now)
    for candidate in (manager, restarted):
        with pytest.raises(token.TokenError, match="auth_unavailable"):
            run(candidate.get_token(deadline=deadline()))
    now += timedelta(hours=2)
    for candidate in (manager, restarted):
        with pytest.raises(token.TokenError, match="auth_unavailable"):
            run(candidate.get_token(deadline=deadline()))
    assert issued == []
    assert json.loads(path.read_text())["kind"] == "auth_unavailable"


@pytest.mark.parametrize("kind", ["ready", "issuance_unknown"])
def test_nonrevoked_state_rejects_unexpected_failed_digest(tmp_path, kind):
    import json
    token, storage = modules()
    store = storage.SecureTokenStore(tmp_path / "toss", "offline-client")
    store.save_state(kind, 1)
    assert store.load_state()["kind"] == kind
    path = store.directory / "toss_auth_state.json"
    data = json.loads(path.read_text())
    data["failed_digest"] = "a" * 64
    path.write_text(json.dumps(data))
    with pytest.raises(token.TokenError, match="auth_unavailable"):
        store.load_state()


def test_observation_is_idempotent_private_and_preserves_auth_state(tmp_path):
    from test_toss_token_contract import setup
    _, store, manager, _ = setup(tmp_path)
    store.save_state("ready", 1)
    previous = (store.directory / "toss_auth_state.json").read_bytes()
    for _ in range(3):
        manager.observe_revocation("synthetic-observed-bearer")
    paths = list(store.directory.glob("toss_revoked_slot_*.json"))
    assert len(paths) == 1
    assert stat.S_IMODE(paths[0].stat().st_mode) == 0o600
    assert paths[0].stat().st_size <= 1024
    assert "synthetic-observed-bearer" not in paths[0].read_text()
    assert (store.directory / "toss_auth_state.json").read_bytes() == previous
    assert not list(store.directory.glob(".token-*"))


@pytest.mark.parametrize("damage", ["json", "large", "symlink", "mode", "owner"])
@pytest.mark.parametrize("surface", ["observation", "resolution"])
def test_observation_and_resolution_damage_blocks_restart(tmp_path, monkeypatch, damage, surface):
    from test_toss_token_contract import setup, run, deadline
    token, store, manager, issued = setup(tmp_path)
    _, storage = modules()
    manager.observe_revocation("synthetic-old-bearer")
    store.save(record(storage, value="synthetic-different-bearer", generation=2))
    assert run(manager.get_token(deadline=deadline())) == "synthetic-different-bearer"
    path = (next(store.directory.glob("toss_revoked_slot_*.json")) if surface == "observation"
            else store.directory / "toss_revoked_resolutions.json")
    if damage == "json":
        path.write_text("broken")
    elif damage == "large":
        path.write_bytes(b" " * (1025 if surface == "observation" else 65537))
    elif damage == "symlink":
        target = tmp_path / "untouched"
        path.unlink()
        path.symlink_to(target)
    elif damage == "mode":
        path.chmod(0o644)
    else:
        inode = path.stat().st_ino
        original = os.fstat
        def wrong_owner(fd):
            data = original(fd)
            if data.st_ino == inode:
                values = list(data)
                values[4] += 1
                return os.stat_result(values)
            return data
        monkeypatch.setattr(os, "fstat", wrong_owner)
    restarted = token.TokenManager(store, role="issuer", issuer=manager.issuer, enabled=True)
    with pytest.raises(token.TokenError, match="auth_unavailable"):
        run(restarted.get_token(deadline=deadline()))
    assert issued == []


def test_revocation_slots_are_bounded_and_overflow_blocks_restart(tmp_path):
    from test_toss_token_contract import setup, run, deadline
    token, store, manager, issued = setup(tmp_path)
    _, storage = modules()
    for index in range(256):
        manager.observe_revocation(f"synthetic-distinct-{index}")
    assert len(list(store.directory.glob("toss_revoked_slot_*.json"))) == 256
    with pytest.raises(token.TokenError, match="auth_unavailable"):
        manager.observe_revocation("synthetic-overflow")
    assert (store.directory / "toss_revoked_overflow.json").exists()
    assert len(list(store.directory.glob("toss_revoked_slot_*.json"))) == 256
    store.save(record(storage, value="synthetic-different-bearer", generation=300, expired=True))
    restarted = token.TokenManager(store, role="issuer", issuer=manager.issuer, enabled=True)
    with pytest.raises(token.TokenError, match="auth_unavailable"):
        run(restarted.get_token(deadline=deadline()))
    assert issued == []


@pytest.mark.parametrize("failure", ["write", "link", "file_fsync", "directory_fsync"])
def test_failed_observation_publication_is_fail_closed(tmp_path, monkeypatch, failure):
    from test_toss_token_contract import setup, run, deadline
    token, store, manager, issued = setup(tmp_path)
    _, storage = modules()
    store.save(record(storage, expired=True))
    original_fsync = os.fsync
    failures = []
    def fail_once(*args, **kwargs):
        failures.append(1)
        if len(failures) == 1:
            raise OSError("FAKE_OBSERVATION_STORAGE_SECRET")
        return original_operation(*args, **kwargs)
    def fsync(fd):
        is_directory = stat.S_ISDIR(os.fstat(fd).st_mode)
        if is_directory == (failure == "directory_fsync") and not failures:
            failures.append(1)
            raise OSError("FAKE_OBSERVATION_STORAGE_SECRET")
        return original_fsync(fd)
    with monkeypatch.context() as patch:
        if failure in {"write", "link"}:
            original_operation = getattr(os, failure)
            patch.setattr(os, failure, fail_once)
        else:
            patch.setattr(os, "fsync", fsync)
        with pytest.raises(token.TokenError, match="auth_unavailable"):
            manager.observe_revocation("synthetic-old-bearer")
    restarted = token.TokenManager(store, role="issuer", issuer=manager.issuer, enabled=True)
    with pytest.raises(token.TokenError, match="auth_unavailable"):
        run(restarted.get_token(deadline=deadline()))
    assert issued == []


def test_new_observation_outside_resolution_snapshot_is_not_acknowledged(tmp_path, monkeypatch):
    from test_toss_token_contract import setup, run, deadline
    token, store, manager, issued = setup(tmp_path)
    _, storage = modules()
    store.save(record(storage, value="synthetic-current-bearer", generation=2))
    manager.observe_revocation("synthetic-old-bearer")
    original = store.load_revocations
    def append_after_snapshot():
        snapshot = original()
        manager.observe_revocation("synthetic-current-bearer")
        return snapshot
    with monkeypatch.context() as patch:
        patch.setattr(store, "load_revocations", append_after_snapshot)
        assert run(manager.get_token(deadline=deadline())) == "synthetic-current-bearer"
    assert len(store.load_revocations()) == 2
    assert len(store.load_revocation_resolutions()) == 1
    restarted = token.TokenManager(store, role="issuer", issuer=manager.issuer, enabled=True)
    with pytest.raises(token.TokenError, match="auth_unavailable"):
        run(restarted.get_token(deadline=deadline()))
    assert issued == []


def _observe_process(directory, digest, barrier, results):
    import conftest  # noqa: F401
    from pathlib import Path
    _, storage = modules()
    store = storage.SecureTokenStore(Path(directory), "offline-client")
    barrier.wait(timeout=3)
    store.observe_revocation(digest, 1)
    store.observe_revocation(digest, 1)
    results.put("observed")


@pytest.mark.parametrize("same", [True, False])
def test_two_process_observations_are_idempotent_and_never_clobber(tmp_path, same):
    import hashlib
    import multiprocessing
    _, storage = modules()
    store = storage.SecureTokenStore(tmp_path / "toss", "offline-client")
    first = hashlib.sha256(b"synthetic-first").hexdigest()
    slot = int(hashlib.sha256(f"{first}:1".encode()).hexdigest()[:8], 16) % 256
    second = first
    if not same:
        for index in range(10000):
            candidate = hashlib.sha256(f"synthetic-collision-{index}".encode()).hexdigest()
            if int(hashlib.sha256(f"{candidate}:1".encode()).hexdigest()[:8], 16) % 256 == slot:
                second = candidate
                break
        assert second != first
    ctx = multiprocessing.get_context("spawn")
    barrier, results = ctx.Barrier(2), ctx.Queue()
    processes = [ctx.Process(target=_observe_process, args=(str(store.directory), digest, barrier, results))
                 for digest in (first, second)]
    try:
        for process in processes:
            process.start()
        for process in processes:
            process.join(timeout=5)
            assert process.exitcode == 0
        assert [results.get(timeout=1) for _ in range(2)] == ["observed", "observed"]
        observations = store.load_revocations()
        assert len(observations) == (1 if same else 2)
        assert {item["failed_digest"] for item in observations.values()} == {first, second}
        assert not list(store.directory.glob(".token-*"))
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(timeout=2)
        results.close()
        results.join_thread()
