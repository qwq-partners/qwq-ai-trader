"""실제 임시 SQLite 원장 내보내기의 무변경·완전 검증 경계."""

import asyncio
import hashlib
import os
import sqlite3
from decimal import Decimal
from pathlib import Path

import pytest

import src.execution.execution_ledger as ledger_module
from src.execution.execution_ledger import ExecutionLedger, ExecutionLedgerError


FACTS = {
    "local_id": "o1", "symbol": "005930", "side": "buy", "quantity": 2,
    "order_date": "20261002", "strategy": "gap", "reason": "test", "partial_exit": False,
}


def read(path, scope="scope-test"):
    reader = getattr(ledger_module, "read_execution_ledger", None)
    assert callable(reader), "읽기 전용 export API가 필요합니다"
    return reader(path, scope)


def make_ledger(path: Path, *, full=False, clean=False):
    async def populate():
        ledger = ExecutionLedger(path, "scope-test")
        await ledger.open("s1")
        if full:
            await ledger.intent("s1:o1", FACTS)
            await ledger.accepted("s1:o1", "000123", "001")
            execution_id = await ledger.observe("s1:o1", 2, Decimal("100.5"))
            await ledger.receipt(execution_id, "portfolio_applied")
            await ledger.receipt(execution_id, "handoff_returned")
        if clean:
            assert await ledger.close_session()
    asyncio.run(populate())


def file_snapshot(directory):
    return {p.name: p.read_bytes() for p in directory.iterdir() if p.is_file()}


def test_export_keeps_database_bytes_rows_and_directory_entries_unchanged(tmp_path):
    path = tmp_path / "ledger.sqlite"
    make_ledger(path, full=True, clean=True)
    before = file_snapshot(tmp_path)
    with sqlite3.connect(path) as connection:
        sessions = connection.execute("SELECT * FROM sessions").fetchall()
        events = connection.execute("SELECT * FROM events").fetchall()
        state_digest = connection.execute("SELECT value FROM metadata WHERE name='state_digest'").fetchone()[0]
    result = read(path)
    assert set(result) == {"format", "account_scope", "schema_version", "event_count", "state_digest", "source_sha256", "sessions", "orders"}
    assert result["format"] == "execution-ledger-export-v1"
    assert result["account_scope"] == "scope-test"
    assert result["schema_version"] == 1
    assert result["event_count"] == 7
    assert result["state_digest"] == state_digest
    assert result["source_sha256"] == hashlib.sha256(before[path.name]).hexdigest()
    assert result["sessions"] == {"s1": {"clean": True, "prior_unclean": False}}
    assert result["orders"]["s1:o1"]["facts"] == FACTS
    assert result["orders"]["s1:o1"]["executions"][0]["handoff_returned"] is True
    assert read(path) == result
    assert file_snapshot(tmp_path) == before
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT * FROM sessions").fetchall() == sessions
        assert connection.execute("SELECT * FROM events").fetchall() == events


def test_empty_unclean_session_is_preserved_without_creating_session(tmp_path, monkeypatch):
    path = tmp_path / "ledger.sqlite"
    make_ledger(path)

    def forbidden(*args, **kwargs):
        raise AssertionError("mutable API must not be invoked")

    for name in ("open", "snapshot", "_transaction", "_initialize"):
        monkeypatch.setattr(ExecutionLedger, name, forbidden)
    result = read(path)
    assert result["sessions"] == {"s1": {"clean": False, "prior_unclean": False}}
    assert result["orders"] == {}
    assert result["event_count"] == 1


@pytest.mark.parametrize("name", ["ledger #1?.sqlite", "한글 %25.sqlite"])
def test_uri_quotes_special_filename_characters(tmp_path, name):
    path = tmp_path / name
    make_ledger(path)
    before = file_snapshot(tmp_path)
    assert read(path)["event_count"] == 1
    assert file_snapshot(tmp_path) == before


def test_missing_path_does_not_create_file_or_parents(tmp_path):
    path = tmp_path / "absent" / "ledger.sqlite"
    with pytest.raises(ExecutionLedgerError):
        read(path)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("suffix", ["-wal", "-shm", "-journal"])
def test_even_empty_sidecars_are_rejected_without_changes(tmp_path, suffix):
    path = tmp_path / "ledger.sqlite"
    make_ledger(path)
    Path(str(path) + suffix).touch()
    before = file_snapshot(tmp_path)
    with pytest.raises(ExecutionLedgerError):
        read(path)
    assert file_snapshot(tmp_path) == before


def test_wal_header_without_sidecars_is_rejected_without_creating_shm(tmp_path):
    path = tmp_path / "ledger.sqlite"
    make_ledger(path)
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
    assert list(tmp_path.iterdir()) == [path]
    before = file_snapshot(tmp_path)
    assert before[path.name][18:20] == b"\x02\x02"
    with pytest.raises(ExecutionLedgerError):
        read(path)
    assert file_snapshot(tmp_path) == before


@pytest.mark.parametrize("kind", ["empty", "corrupt", "scope", "schema", "projection", "event"])
def test_invalid_ledger_is_never_repaired(tmp_path, kind):
    path = tmp_path / "ledger.sqlite"
    if kind == "empty":
        path.touch()
    elif kind == "corrupt":
        path.write_bytes(b"not a database")
    else:
        make_ledger(path, full=True)
        with sqlite3.connect(path) as connection:
            if kind == "scope":
                connection.execute("UPDATE metadata SET value='wrong' WHERE name='scope'")
            elif kind == "schema":
                connection.execute("PRAGMA user_version=99")
            elif kind == "projection":
                connection.execute("UPDATE orders SET payload='{}'")
            else:
                connection.execute("DROP TRIGGER events_no_update")
                connection.execute("UPDATE events SET operation='unsupported' WHERE sequence=2")
    before = file_snapshot(tmp_path)
    with pytest.raises(ExecutionLedgerError):
        read(path)
    assert file_snapshot(tmp_path) == before


def test_symlink_file_and_parent_are_rejected(tmp_path):
    actual = tmp_path / "actual"
    actual.mkdir()
    path = actual / "ledger.sqlite"
    make_ledger(path)
    alias = tmp_path / "alias.sqlite"
    alias.symlink_to(path)
    directory_alias = tmp_path / "linked"
    directory_alias.symlink_to(actual, target_is_directory=True)
    for candidate in (alias, directory_alias / path.name):
        with pytest.raises(ExecutionLedgerError):
            read(candidate)


def test_nonregular_inputs_are_rejected_without_blocking(tmp_path):
    fifo = tmp_path / "pipe"
    os.mkfifo(fifo)
    for candidate in (tmp_path, fifo):
        with pytest.raises(ExecutionLedgerError):
            read(candidate)


def test_file_size_limit_precedes_sqlite_parsing(tmp_path):
    path = tmp_path / "oversized.sqlite"
    with path.open("wb") as stream:
        stream.truncate(128 * 1024 * 1024 + 1)
    with pytest.raises(ExecutionLedgerError):
        read(path)
    assert path.stat().st_size == 128 * 1024 * 1024 + 1


def test_event_limit_is_enforced_before_history_replay(tmp_path, monkeypatch):
    path = tmp_path / "ledger.sqlite"
    make_ledger(path, full=True)
    monkeypatch.setattr(ledger_module, "_READ_MAX_EVENTS", 2, raising=False)
    with pytest.raises(ExecutionLedgerError):
        read(path)


def test_reader_connection_cannot_write_even_if_query_only_is_disabled(tmp_path, monkeypatch):
    path = tmp_path / "ledger.sqlite"
    make_ledger(path)
    original_load = ExecutionLedger._load

    def check_read_only(self, connection):
        assert connection.in_transaction
        assert connection.execute("PRAGMA query_only").fetchone() == (1,)
        connection.execute("PRAGMA query_only=OFF")
        with pytest.raises(sqlite3.OperationalError):
            connection.execute("INSERT INTO metadata VALUES ('forbidden', 'write')")
        connection.execute("PRAGMA query_only=ON")
        return original_load(self, connection)

    monkeypatch.setattr(ExecutionLedger, "_load", check_read_only)
    before = file_snapshot(tmp_path)
    assert read(path)["event_count"] == 1
    assert file_snapshot(tmp_path) == before


def test_source_change_during_validation_is_rejected(tmp_path, monkeypatch):
    path = tmp_path / "ledger.sqlite"
    make_ledger(path)
    original_load = ExecutionLedger._load

    def mutate_after_load(self, connection):
        state = original_load(self, connection)
        with path.open("ab") as stream:
            stream.write(b"changed")
        return state

    monkeypatch.setattr(ExecutionLedger, "_load", mutate_after_load)
    with pytest.raises(ExecutionLedgerError):
        read(path)


def test_sidecar_appearing_during_validation_is_rejected(tmp_path, monkeypatch):
    path = tmp_path / "ledger.sqlite"
    make_ledger(path)
    original_load = ExecutionLedger._load

    def sidecar_after_load(self, connection):
        state = original_load(self, connection)
        Path(str(path) + "-journal").touch()
        return state

    monkeypatch.setattr(ExecutionLedger, "_load", sidecar_after_load)
    with pytest.raises(ExecutionLedgerError):
        read(path)


def test_symlink_parent_cannot_be_hidden_by_dot_dot_normalization(tmp_path):
    path = tmp_path / "ledger.sqlite"
    make_ledger(path)
    target = tmp_path / "real_directory"
    target.mkdir()
    alias = tmp_path / "linked"
    alias.symlink_to(target, target_is_directory=True)
    with pytest.raises(ExecutionLedgerError):
        read(alias / ".." / path.name)


def test_wal_transition_between_header_check_and_connect_creates_no_shm(tmp_path, monkeypatch):
    path = tmp_path / "ledger.sqlite"
    make_ledger(path)
    original_connect = sqlite3.connect

    def switch_before_reader_connect(*args, **kwargs):
        with path.open("r+b") as stream:
            stream.seek(18)
            stream.write(b"\x02\x02")
        return original_connect(*args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", switch_before_reader_connect)
    with pytest.raises(ExecutionLedgerError):
        read(path)
    assert set(tmp_path.iterdir()) == {path}


def test_same_size_source_change_during_validation_is_rejected(tmp_path, monkeypatch):
    path = tmp_path / "ledger.sqlite"
    make_ledger(path)
    original_load = ExecutionLedger._load

    def mutate_after_load(self, connection):
        state = original_load(self, connection)
        with path.open("r+b") as stream:
            stream.seek(-1, os.SEEK_END)
            old = stream.read(1)
            stream.seek(-1, os.SEEK_END)
            stream.write(bytes([old[0] ^ 1]))
        return state

    monkeypatch.setattr(ExecutionLedger, "_load", mutate_after_load)
    with pytest.raises(ExecutionLedgerError):
        read(path)
