"""임시 PostgreSQL UNIX 소켓에서 실제 DDL/SQL과 core outbox를 인수한다."""
import asyncio
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import tempfile
import time
from uuid import uuid4

import asyncpg
import pytest

from src.data.storage.trade_storage import TradeStorage
from src.execution.safety.journal_delivery import ExecutionEnvelope, OutboxDispatcher
from src.execution.safety.runtime import KRExecutionRuntime
from src.execution.safety.store import ExecutionStateStore
from test_execution_journal_delivery import AckFailStore, filled, immutable_domains
from test_execution_runtime import NOW


PG_BIN = Path("/usr/lib/postgresql/16/bin")
PG_USER = "qwq_journal_synthetic"
PG_DATABASE = "qwq_journal_synthetic"
PG_ENV = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "TZ": "UTC"}


@pytest.fixture(scope="module")
def temporary_postgres():
    if not all((PG_BIN / command).is_file() for command in ("initdb", "postgres")):
        pytest.skip("실제 PostgreSQL 16 initdb/postgres 바이너리가 없는 CI 환경")
    if os.geteuid() == 0:
        pytest.skip("PostgreSQL initdb는 비root 테스트 UID가 필요함; 다른 OS 계정으로 전환하지 않음")
    root = Path(tempfile.mkdtemp(prefix="qwq-journal-pg-", dir="/tmp"))
    root.chmod(0o700)
    data, sockets = root / "data", root / "socket"
    sockets.mkdir(mode=0o700)
    process = None
    output = None
    args = {"host": str(sockets), "port": 25432, "user": PG_USER,
            "password": "SYNTHETIC_UNUSED", "database": PG_DATABASE,
            "ssl": False, "timeout": 5, "command_timeout": 5}
    try:
        initialized = subprocess.run(
            [str(PG_BIN / "initdb"), "-D", str(data), "--username=" + PG_USER,
             "--auth-local=trust", "--auth-host=reject", "--no-locale", "--encoding=UTF8"],
            cwd=root, env=PG_ENV, capture_output=True, text=True, timeout=20,
        )
        assert initialized.returncode == 0, initialized.stderr
        output = (root / "server.log").open("w")
        process = subprocess.Popen(
            [str(PG_BIN / "postgres"), "-D", str(data),
             "-c", "listen_addresses=", "-c", "unix_socket_directories=" + str(sockets),
             "-c", "unix_socket_permissions=0700", "-c", "port=25432"],
            cwd=root, env=PG_ENV, stdout=output, stderr=subprocess.STDOUT,
        )

        async def bootstrap():
            deadline = time.monotonic() + 10
            while True:
                assert process.poll() is None, (root / "server.log").read_text()
                try:
                    connection = await asyncpg.connect(**{**args, "database": "postgres"})
                    break
                except (OSError, asyncpg.CannotConnectNowError):
                    if time.monotonic() >= deadline:
                        raise AssertionError("임시 PostgreSQL 소켓 준비 실패") from None
                    await asyncio.sleep(.05)
            try:
                assert await connection.fetchval("SHOW listen_addresses") == ""
                assert await connection.fetchval("SHOW data_directory") == str(data)
                assert await connection.fetchval("SELECT inet_server_addr()") is None
                await connection.execute("CREATE DATABASE " + PG_DATABASE + " TEMPLATE template0")
            finally:
                await connection.close()

        asyncio.run(bootstrap())
        yield args
    finally:
        if process is not None and process.poll() is None:
            process.send_signal(signal.SIGINT)  # 이 fixture가 직접 spawn한 postmaster만 종료.
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        if output is not None:
            output.close()
        assert root.parent == Path("/tmp") and root.name.startswith("qwq-journal-pg-")
        assert root.stat().st_uid == os.geteuid()
        shutil.rmtree(root)  # 방금 생성한 이 전용 DB 디렉터리만 정리.


@asynccontextmanager
async def journal_pool(arguments, *, initialize=True):
    # 테스트마다 별도 schema. 명시 고정 user/db/password로 .pgpass/DSN 기본값을 사용하지 않는다.
    schema = "execution_test_" + uuid4().hex
    connection = await asyncpg.connect(**arguments)
    try:
        assert await connection.fetchval("SELECT current_database()") == PG_DATABASE
        assert await connection.fetchval("SELECT current_user") == PG_USER
        await connection.execute("CREATE SCHEMA " + schema)
    finally:
        await connection.close()
    pool = await asyncpg.create_pool(**arguments, min_size=1, max_size=4,
                                    server_settings={"search_path": schema})
    try:
        storage = TradeStorage.__new__(TradeStorage)
        storage.pool, storage._db_available = pool, True
        if initialize:
            await storage._ensure_tables()
        yield pool, storage.execution_journal()
    finally:
        await pool.close()


def test_real_postgres_delivery_lookup_and_duplicate_row_count(temporary_postgres, tmp_path):
    async def scenario():
        _, _, store, runtime, _, obs = await filled(tmp_path)
        try:
            before = immutable_domains(runtime.owner.state)
            event = ExecutionEnvelope.from_outbox(obs.observation_id, runtime.owner.state["outbox"][obs.observation_id])
            async with journal_pool(temporary_postgres) as (pool, sink):
                result = await OutboxDispatcher(runtime.owner, sink).drain()
                assert result.delivered == 1 and result.pending == 0, result
                receipt = await sink.lookup(event.execution_key)
                assert receipt.payload_digest == event.payload_digest
                assert await sink.apply_once(event) == receipt
                async with pool.acquire() as connection:
                    assert await connection.fetchval("SELECT count(*) FROM execution_journal") == 1
                    payload = await connection.fetchval("SELECT payload FROM execution_journal WHERE execution_key=$1", event.execution_key)
                    assert json.loads(payload) == json.loads(event.payload_json)
                assert not runtime.owner.state["cursors"][obs.order_key]["journal_pending"]
                assert immutable_domains(runtime.owner.state) == before
                assert not runtime.trading_ready
        finally:
            await store.close()
    asyncio.run(scenario())


def test_real_postgres_two_dispatchers_and_direct_concurrent_insert_are_unique(temporary_postgres, tmp_path):
    async def scenario():
        _, _, store, runtime, _, obs = await filled(tmp_path)
        try:
            event = ExecutionEnvelope.from_outbox(obs.observation_id, runtime.owner.state["outbox"][obs.observation_id])
            async with journal_pool(temporary_postgres) as (pool, sink):
                outcomes = await asyncio.gather(
                    OutboxDispatcher(runtime.owner, sink).drain(),
                    OutboxDispatcher(runtime.owner, sink).drain(),
                )
                assert sum(item.delivered for item in outcomes) == 1
                acknowledgments = await asyncio.gather(*(sink.apply_once(event) for _ in range(4)))
                assert all(item == acknowledgments[0] for item in acknowledgments)
                async with pool.acquire() as connection:
                    assert await connection.fetchval("SELECT count(*) FROM execution_journal") == 1
        finally:
            await store.close()
    asyncio.run(scenario())


def test_real_postgres_same_key_different_payload_rolls_back(temporary_postgres, tmp_path):
    async def scenario():
        _, _, store, runtime, _, obs = await filled(tmp_path)
        try:
            row = runtime.owner.state["outbox"][obs.observation_id]
            event = ExecutionEnvelope.from_outbox(obs.observation_id, row)
            row["realized_pnl"] = "123"
            conflict = ExecutionEnvelope.from_outbox(obs.observation_id, row)
            async with journal_pool(temporary_postgres) as (pool, sink):
                original = await sink.apply_once(event)
                with pytest.raises(ValueError, match="different payload"):
                    await sink.apply_once(conflict)
                assert await sink.lookup(event.execution_key) == original
                async with pool.acquire() as connection:
                    assert await connection.fetchval("SELECT count(*) FROM execution_journal") == 1
                    assert await connection.fetchval("SELECT payload->>'realized_pnl' FROM execution_journal") == "0"
        finally:
            await store.close()
    asyncio.run(scenario())


def test_real_postgres_commit_owner_ack_failure_and_core_reopen(temporary_postgres, tmp_path):
    async def scenario():
        engine, exits, store, runtime, _, obs = await filled(tmp_path, store_type=AckFailStore)
        before = immutable_domains(runtime.owner.state)
        async with journal_pool(temporary_postgres) as (pool, sink):
            try:
                store.fail_ack = True
                result = await OutboxDispatcher(runtime.owner, sink).drain()
                assert result.delivered == 0 and result.reason == "owner_ack_unconfirmed"
                assert not runtime.owner.healthy
                async with pool.acquire() as connection:
                    assert await connection.fetchval("SELECT count(*) FROM execution_journal") == 1
                assert (await store.load())[1]["outbox"][obs.observation_id]["status"] == "pending"
            finally:
                await store.close()
            reopened = ExecutionStateStore(tmp_path / "execution" / "state.sqlite3")
            again = KRExecutionRuntime(reopened, engine, exits, clock=lambda: NOW)
            try:
                await again.restore()
                assert (await OutboxDispatcher(again.owner, sink).drain()).delivered == 1
                assert immutable_domains(again.owner.state) == before
                assert not again.owner.state["cursors"][obs.order_key]["journal_pending"]
                async with pool.acquire() as connection:
                    assert await connection.fetchval("SELECT count(*) FROM execution_journal") == 1
            finally:
                await reopened.close()
    asyncio.run(scenario())


def test_real_postgres_missing_schema_cannot_ack_or_fall_back(temporary_postgres, tmp_path):
    async def scenario():
        _, _, store, runtime, _, obs = await filled(tmp_path)
        try:
            async with journal_pool(temporary_postgres, initialize=False) as (_, sink):
                result = await OutboxDispatcher(runtime.owner, sink).drain()
                assert result.delivered == 0 and result.reason == "sink_unconfirmed"
                assert runtime.owner.state["outbox"][obs.observation_id]["status"] == "pending"
                assert runtime.owner.state["cursors"][obs.order_key]["journal_pending"]
        finally:
            await store.close()
    asyncio.run(scenario())
