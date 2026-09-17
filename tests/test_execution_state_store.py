"""실행 checkpoint의 원자성·재시작·충돌 계약. 실제 tmp SQLite만 사용한다."""
import asyncio
import sqlite3
from pathlib import Path

import pytest

from src.execution.safety.store import (
    ExecutionStateStore, StoreConflict, StoreError,
)


def test_reopen_and_idempotent_commit_preserve_atomic_checkpoint(tmp_path):
    async def scenario():
        path = tmp_path / "state" / "execution.sqlite3"
        store = ExecutionStateStore(path)
        assert await store.load() == (0, {})
        assert await store.commit(0, {"cash": "100"}, "baseline") == 1
        assert await store.commit(0, {"cash": "100"}, "baseline") == 1
        assert await store.lookup_commit("baseline") == 1
        assert await store.lookup_commit("absent") is None
        await store.close()
        reopened = ExecutionStateStore(path)
        assert await reopened.load() == (1, {"cash": "100"})
        await reopened.close()
    asyncio.run(scenario())


def test_conflicting_version_or_commit_id_cannot_overwrite_money(tmp_path):
    async def scenario():
        store = ExecutionStateStore(tmp_path / "state" / "execution.sqlite3")
        await store.commit(0, {"cash": "100"}, "one")
        for version, state, key in [
            (0, {"cash": "200"}, "two"),
            (0, {"cash": "200"}, "one"),
            (1, {"cash": "100"}, "one"),
        ]:
            with pytest.raises(StoreConflict):
                await store.commit(version, state, key)
        assert await store.load() == (1, {"cash": "100"})
        await store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("kind", ["corrupt", "future_schema", "foreign_schema"])
def test_existing_invalid_database_is_rejected_not_recreated(tmp_path, kind):
    path = tmp_path / "execution.sqlite3"
    if kind == "corrupt":
        path.write_bytes(b"not a sqlite database")
    else:
        with sqlite3.connect(path) as conn:
            conn.execute("CREATE TABLE unrelated(value TEXT)")
            if kind == "future_schema":
                conn.execute("PRAGMA user_version=999")
    original = path.read_bytes()
    async def scenario():
        store = ExecutionStateStore(path)
        with pytest.raises(StoreError):
            await store.load()
        await store.close()
    asyncio.run(scenario())
    assert path.read_bytes() == original


def test_database_and_sidecars_are_private_and_durable(tmp_path):
    async def scenario():
        path = tmp_path / "state" / "execution.sqlite3"
        store = ExecutionStateStore(path)
        await store.commit(0, {"cash": "100"}, "one")
        assert path.parent.stat().st_mode & 0o777 == 0o700
        for file in [path, Path(str(path) + "-wal"), Path(str(path) + "-shm")]:
            assert file.exists()
            assert file.stat().st_mode & 0o777 == 0o600
        with sqlite3.connect(path) as conn:
            assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        await store.close()
    asyncio.run(scenario())


def test_transaction_failure_leaves_checkpoint_and_commit_marker_unchanged(tmp_path):
    async def scenario():
        path = tmp_path / "state" / "execution.sqlite3"
        store = ExecutionStateStore(path)
        await store.commit(0, {"cash": "100"}, "one")
        with sqlite3.connect(path) as conn:
            conn.execute("""CREATE TRIGGER fail_checkpoint BEFORE UPDATE ON checkpoint
                            BEGIN SELECT RAISE(ABORT, 'injected storage failure'); END""")
        with pytest.raises(StoreError):
            await store.commit(1, {"cash": "200"}, "two")
        assert await store.lookup_commit("two") is None
        assert await store.load() == (1, {"cash": "100"})
        await store.close()
    asyncio.run(scenario())


def test_caller_mutation_and_nonfinite_json_never_change_stored_snapshot(tmp_path):
    async def scenario():
        store = ExecutionStateStore(tmp_path / "state" / "execution.sqlite3")
        original = {"nested": {"quantity": 40}}
        await store.commit(0, original, "one")
        original["nested"]["quantity"] = 999
        with pytest.raises((ValueError, StoreError)):
            await store.commit(1, {"price": float("nan")}, "two")
        assert await store.load() == (1, {"nested": {"quantity": 40}})
        await store.close()
    asyncio.run(scenario())


def test_symlink_database_is_rejected_before_target_mutation(tmp_path):
    target = tmp_path / "target"
    target.write_text("preserve")
    path = tmp_path / "execution.sqlite3"
    path.symlink_to(target)
    async def scenario():
        store = ExecutionStateStore(path)
        with pytest.raises(StoreError):
            await store.load()
        await store.close()
    asyncio.run(scenario())
    assert target.read_text() == "preserve"


def test_commit_dedup_history_does_not_retain_every_full_checkpoint(tmp_path):
    async def scenario():
        path = tmp_path / "state" / "execution.sqlite3"
        store = ExecutionStateStore(path)
        for version in range(3):
            await store.commit(version, {"inbox": "x" * 10000, "version": version}, str(version))
        # commit 이력에 전체 checkpoint를 반복 보관하면 관측량에 제곱 비례한다.
        with sqlite3.connect(path) as conn:
            rows = conn.execute("SELECT * FROM commits").fetchall()
        assert sum(len(str(value)) for row in rows for value in row) < 1024
        assert (await store.load())[1]["inbox"] == "x" * 10000
        await store.close()
    asyncio.run(scenario())
