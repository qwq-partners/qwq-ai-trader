"""독립 임시 PostgreSQL/Unix socket으로 실제 commit과 rollback을 확인한다."""
import asyncio
import os
import shutil
import subprocess
import tempfile
from datetime import date
from pathlib import Path

import asyncpg
import pytest

from src.data.storage.execution_journal import ExecutionIdentity
from src.data.storage.trade_storage import TradeStorage


@pytest.fixture(scope="module")
def pg_socket():
    binaries = Path("/usr/lib/postgresql/16/bin")
    if not (binaries / "postgres").exists():
        pytest.skip("PostgreSQL16 binary unavailable; real DB was NOT verified")
    with tempfile.TemporaryDirectory(prefix="qwq-journal-pg-") as directory:
        root = Path(directory)
        data, socket = root / "data", root / "socket"
        socket.mkdir(mode=0o700)
        subprocess.run([str(binaries / "initdb"), "-D", str(data), "-A", "trust", "--no-locale", "-E", "UTF8", "--username", "qwq_test"], check=True, capture_output=True, env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"})
        # 외부/운영 설정을 전달하지 않으며 TCP를 열지 않는다. fsync 기본 ON 유지.
        process = subprocess.Popen([str(binaries / "postgres"), "-D", str(data), "-k", str(socket),
                                    "-c", "listen_addresses=", "-c", "unix_socket_permissions=0700"],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"})
        try:
            async def ready():
                for _ in range(100):
                    try:
                        conn = await asyncpg.connect(host=str(socket), database="postgres", user="qwq_test", password="synthetic-unused", port=5432, ssl=False)
                        assert await conn.fetchval("SHOW fsync") == "on"
                        assert await conn.fetchval("SHOW listen_addresses") == ""
                        await conn.close()
                        return
                    except (OSError, asyncpg.PostgresError):
                        await asyncio.sleep(.05)
                raise AssertionError("isolated PostgreSQL startup failed")
            asyncio.run(ready())
            yield str(socket)
        finally:
            process.terminate()
            process.wait(timeout=15)
            assert process.returncode == 0
            assert not (data / "postmaster.pid").exists()
            print("isolated PostgreSQL stopped; owned PID", process.pid)


async def storage_for(pg_socket, tmp_path, monkeypatch):
    monkeypatch.setenv("TRADE_JOURNAL_DIR", str(tmp_path / "journal"))
    pool = await asyncpg.create_pool(host=pg_socket, database="postgres", user="qwq_test", password="synthetic-unused", port=5432, ssl=False, min_size=1, max_size=3)
    async with pool.acquire() as conn:
        await conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
    storage = TradeStorage(db_url="synthetic-unused")
    storage.pool = pool
    await storage._ensure_tables()
    await storage._ensure_tables()
    storage._db_available = True
    storage._write_queue = asyncio.Queue()
    storage._writer_task = asyncio.create_task(storage._db_writer())
    return storage


def buy(storage, execution="e1", **patch):
    args = dict(trade_id="t1", symbol="005930", name="합성", entry_price=100,
                entry_quantity=2, entry_reason="entry", entry_strategy="gap",
                execution_identity=ExecutionIdentity("scope", date.today(), "0001", execution))
    args.update(patch)
    return storage.record_entry(**args)


def sell(storage, execution="s1", **patch):
    args = dict(trade_id="t1", exit_price=120, exit_quantity=1, exit_reason="exit",
                exit_type="take_profit", avg_entry_price=100, execution_identity=ExecutionIdentity("scope", date.today(), "0002", execution))
    args.update(patch)
    return storage.record_exit(**args)


@pytest.mark.asyncio
async def test_real_postgres_delta_events_receipts_retries_and_lookup(pg_socket,tmp_path,monkeypatch):
    storage = await storage_for(pg_socket,tmp_path,monkeypatch)
    try:
        buy(storage)
        assert storage.get_execution_receipt("scope","e1").status == "pending"
        buy(storage,"e2",entry_price=130,entry_quantity=1)
        sell(storage)
        await storage._write_queue.join()
        assert storage.get_execution_receipt("scope","s1").status == "committed"
        assert await storage.pool.fetchval("SELECT COUNT(*) FROM trade_events") == 3
        summary = await storage.pool.fetchrow("SELECT entry_quantity,entry_price,exit_quantity FROM trades")
        assert tuple(summary) == (3,110,1)
        sell(storage,"s2")
        await storage._write_queue.join()
        sell(storage)
        await storage._write_queue.join()
        assert await storage.pool.fetchval("SELECT exit_quantity FROM trades") == 2
        assert await storage.pool.fetchval("SELECT COUNT(*) FROM trade_events") == 4
        storage._execution_receipts.clear()
        assert (await storage.lookup_execution_receipt("scope","e1")).status == "committed"
    finally:
        await storage.disconnect()


@pytest.mark.asyncio
async def test_batch_event_failure_rolls_back_parent_and_blocks_following_summary(pg_socket,tmp_path,monkeypatch):
    storage = await storage_for(pg_socket,tmp_path,monkeypatch)
    try:
        await storage.pool.execute("ALTER TABLE trade_events ADD CONSTRAINT synthetic_failure CHECK (quantity < 2)")
        buy(storage)
        buy(storage,"e2",entry_quantity=1)
        await storage._write_queue.join()
        assert await storage.pool.fetchval("SELECT COUNT(*) FROM trades") == 0
        assert await storage.pool.fetchval("SELECT COUNT(*) FROM trade_events") == 0
        assert storage.get_execution_receipt("scope","e1").status != "committed"
        assert storage.get_execution_receipt("scope","e2").status != "committed"
    finally:
        await storage.disconnect()


@pytest.mark.asyncio
async def test_concurrent_same_execution_commits_one_event(pg_socket,tmp_path,monkeypatch):
    storage = await storage_for(pg_socket,tmp_path,monkeypatch)
    try:
        buy(storage)
        job = storage._write_queue.get_nowait()
        storage._write_queue.task_done()
        await asyncio.gather(storage._commit_execution_batch(job),storage._commit_execution_batch(job))
        assert await storage.pool.fetchval("SELECT COUNT(*) FROM trade_events") == 1
        assert await storage.pool.fetchval("SELECT entry_quantity FROM trades") == 2
    finally:
        await storage.disconnect()


@pytest.mark.asyncio
async def test_commit_response_loss_remains_unknown_until_independent_lookup(pg_socket,tmp_path,monkeypatch):
    storage = await storage_for(pg_socket,tmp_path,monkeypatch)
    original = storage._commit_execution_batch
    async def response_lost(job):
        await original(job)
        raise ConnectionError("synthetic response loss")
    monkeypatch.setattr(storage,"_commit_execution_batch",response_lost)
    try:
        buy(storage)
        await storage._write_queue.join()
        assert storage.get_execution_receipt("scope","e1").status == "unknown"
        assert await storage.pool.fetchval("SELECT COUNT(*) FROM trade_events") == 1
        assert (await storage.lookup_execution_receipt("scope","e1")).status == "committed"
        monkeypatch.setattr(storage,"_commit_execution_batch",original)
        buy(storage)
        await storage._write_queue.join()
        assert await storage.pool.fetchval("SELECT COUNT(*) FROM trade_events") == 1
    finally:
        await storage.disconnect()


@pytest.mark.asyncio
async def test_disconnect_drains_commits_before_closing_pool(pg_socket,tmp_path,monkeypatch):
    storage = await storage_for(pg_socket,tmp_path,monkeypatch)
    buy(storage)
    await storage.disconnect()
    assert storage.get_execution_receipt("scope","e1").status == "committed"
    assert storage._write_queue.empty()


@pytest.mark.asyncio
async def test_new_instance_db_conflict_never_updates_summary(pg_socket,tmp_path,monkeypatch):
    storage = await storage_for(pg_socket,tmp_path,monkeypatch)
    try:
        buy(storage)
        await storage._write_queue.join()
        monkeypatch.setenv("TRADE_JOURNAL_DIR", str(tmp_path / "other-journal"))
        other = TradeStorage(db_url="synthetic-unused")
        other.pool = storage.pool
        other._db_available = True
        other._write_queue = asyncio.Queue()
        buy(other, entry_price=101)
        job = other._write_queue.get_nowait()
        with pytest.raises(ValueError):
            await other._commit_execution_batch(job)
        other._write_queue.task_done()
        assert await storage.pool.fetchval("SELECT entry_price FROM trades") == 100
        assert await storage.pool.fetchval("SELECT COUNT(*) FROM trade_events") == 1
    finally:
        await storage.disconnect()


@pytest.mark.asyncio
async def test_legacy_schema_migration_preserves_null_identity_and_rolls_back_errors(pg_socket,tmp_path,monkeypatch):
    from src.data.storage import trade_storage as module
    pool = await asyncpg.create_pool(host=pg_socket,database="postgres",user="qwq_test", password="synthetic-unused", port=5432, ssl=False,min_size=1,max_size=2)
    monkeypatch.setenv("TRADE_JOURNAL_DIR", str(tmp_path / "journal"))
    storage = TradeStorage(db_url="synthetic-unused")
    storage.pool = pool
    try:
        await pool.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
        await pool.execute(module.SCHEMA_SQL)
        await pool.execute("INSERT INTO trades(id,symbol,entry_time,entry_price,entry_quantity) VALUES ('old','005930',CURRENT_TIMESTAMP,100,2)")
        await pool.execute("INSERT INTO trade_events(trade_id,symbol,event_type,event_time,price,quantity) VALUES ('old','005930','BUY',CURRENT_TIMESTAMP,100,2)")
        with monkeypatch.context() as patch:
            patch.setattr(module,"EXECUTION_SCHEMA_SQL",module.EXECUTION_SCHEMA_SQL+" SELECT missing_column FROM trades;")
            with pytest.raises(asyncpg.UndefinedColumnError):
                await storage._ensure_tables()
        assert await pool.fetchval("SELECT count(*) FROM information_schema.columns WHERE table_name='trade_events' AND column_name='execution_id'") == 0
        await storage._ensure_tables()
        await storage._ensure_tables()
        assert tuple(await pool.fetchrow("SELECT execution_id,account_scope,order_date,kis_order_no FROM trade_events")) == (None,None,None,None)
        assert await pool.fetchval("SELECT count(*) FROM trade_events") == 1
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_restart_json_retries_original_batch_without_later_summary(pg_socket,tmp_path,monkeypatch):
    storage = await storage_for(pg_socket,tmp_path,monkeypatch)
    try:
        buy(storage)
        buy(storage,"e2",entry_quantity=1,entry_price=130)
        await storage._write_queue.join()
        other = TradeStorage(db_url="synthetic-unused")
        other.pool = storage.pool
        other._db_available = True
        other._write_queue = asyncio.Queue()
        buy(other)
        job = other._write_queue.get_nowait()
        await other._commit_execution_batch(job)
        other._write_queue.task_done()
        assert other.get_trade("t1").entry_quantity == 3
        assert await storage.pool.fetchval("SELECT entry_quantity FROM trades") == 3
        assert await storage.pool.fetchval("SELECT COUNT(*) FROM trade_events") == 2
    finally:
        await storage.disconnect()


@pytest.mark.asyncio
async def test_retry_failed_predecessor_then_successor_is_exactly_once(pg_socket,tmp_path,monkeypatch):
    storage = await storage_for(pg_socket,tmp_path,monkeypatch)
    try:
        await storage.pool.execute("ALTER TABLE trade_events ADD CONSTRAINT retry_failure CHECK (quantity < 2)")
        buy(storage)
        buy(storage,"e2",entry_quantity=1,entry_price=130)
        await storage._write_queue.join()
        await storage.pool.execute("ALTER TABLE trade_events DROP CONSTRAINT retry_failure")
        buy(storage)
        buy(storage,"e2",entry_quantity=1,entry_price=130)
        await storage._write_queue.join()
        assert storage.get_trade("t1").entry_quantity == 3
        assert await storage.pool.fetchval("SELECT entry_quantity FROM trades") == 3
        assert await storage.pool.fetchval("SELECT COUNT(*) FROM trade_events") == 2
        assert storage.get_execution_receipt("scope","e2").status == "committed"
    finally:
        await storage.disconnect()


@pytest.mark.asyncio
async def test_corrupt_json_event_payload_cannot_claim_original_signature(pg_socket,tmp_path,monkeypatch):
    import json
    from dataclasses import replace
    storage = await storage_for(pg_socket,tmp_path,monkeypatch)
    try:
        buy(storage)
        job = storage._write_queue.get_nowait()
        storage._write_queue.task_done()
        payload = json.loads(job.payload)
        payload["event"]["quantity"] = 99
        corrupt = replace(job,payload=json.dumps(payload))
        with pytest.raises(ValueError):
            await storage._commit_execution_batch(corrupt)
        assert await storage.pool.fetchval("SELECT COUNT(*) FROM trades") == 0
    finally:
        await storage.disconnect()


@pytest.mark.asyncio
@pytest.mark.parametrize("section,field,value", [
    ("summary","entry_quantity",999), ("summary","entry_price",999),
    ("summary","pnl",777), ("event","pnl",888), ("event","status","forged"),
    ("event","event_time","2000-01-01T00:00:00"),
])
async def test_full_payload_corruption_never_commits_to_postgres(pg_socket,tmp_path,monkeypatch,section,field,value):
    import json
    from dataclasses import replace
    storage = await storage_for(pg_socket,tmp_path,monkeypatch)
    try:
        buy(storage)
        job = storage._write_queue.get_nowait()
        storage._write_queue.task_done()
        data = json.loads(job.payload)
        data[section][field] = value
        with pytest.raises(ValueError):
            await storage._commit_execution_batch(replace(job,payload=json.dumps(data)))
        assert await storage.pool.fetchval("SELECT COUNT(*) FROM trades") == 0
        assert await storage.pool.fetchval("SELECT COUNT(*) FROM trade_events") == 0
    finally:
        await storage.disconnect()


@pytest.mark.asyncio
async def test_database_duplicate_and_lookup_require_same_full_batch_digest(pg_socket,tmp_path,monkeypatch):
    import json
    from dataclasses import replace
    from src.data.storage.execution_journal import execution_key, execution_payload_digest
    storage = await storage_for(pg_socket,tmp_path,monkeypatch)
    try:
        buy(storage)
        await storage._write_queue.join()
        key = execution_key("scope","e1")
        job = storage._execution_batches[key]
        data = json.loads(job.payload)
        data["summary"]["entry_quantity"] = 999
        data["payload_digest"] = execution_payload_digest(data)
        different = replace(job,payload=json.dumps(data))
        with pytest.raises(ValueError):
            await storage._commit_execution_batch(different)
        storage._execution_batches[key] = different
        assert (await storage.lookup_execution_receipt("scope","e1")).status == "failed"
        assert await storage.pool.fetchval("SELECT entry_quantity FROM trades") == 2
        assert await storage.pool.fetchval("SELECT COUNT(*) FROM trade_events") == 1
    finally:
        await storage.disconnect()


@pytest.mark.asyncio
@pytest.mark.parametrize('source',['json','live-new','live-duplicate'])
async def test_outer_accounting_corruption_never_commits(pg_socket,tmp_path,monkeypatch,source):
    import json
    from src.core.evolution.trade_journal import TradeJournal
    from src.data.storage.execution_journal import ExecutionPayloadError
    storage = await storage_for(pg_socket,tmp_path,monkeypatch)
    try:
        buy(storage)
        await storage._write_queue.join()
        before = [dict(r) for r in await storage.pool.fetch('SELECT * FROM trades')]
        if source == 'json':
            path = next((tmp_path/'journal').glob('trades_*.json'))
            data = json.loads(path.read_text())
            data['trades'][0]['entry_quantity'] = 999
            path.write_text(json.dumps(data))
            with pytest.raises(ExecutionPayloadError):
                storage._journal = TradeJournal(str(tmp_path/'journal'))
        else:
            storage._journal.get_trade('t1').entry_quantity = 999
            with pytest.raises(ExecutionPayloadError):
                buy(storage,'e1' if source == 'live-duplicate' else 'e2',entry_quantity=1)
        await storage._write_queue.join()
        assert [dict(r) for r in await storage.pool.fetch('SELECT * FROM trades')] == before
        assert await storage.pool.fetchval('SELECT count(*) FROM trade_events') == 1
    finally:
        await storage.disconnect()


@pytest.mark.asyncio
async def test_legacy_kr_reconcile_preserves_identified_rows_and_memory(pg_socket,tmp_path,monkeypatch):
    storage = await storage_for(pg_socket,tmp_path,monkeypatch)
    try:
        buy(storage)
        sell(storage,exit_quantity=2)
        await storage._write_queue.join()
        before = [dict(r) for r in await storage.pool.fetch('SELECT * FROM trades')]
        cached = storage._journal.get_trade('t1').to_dict()
        await storage._reconcile_pnl(date.today(),{'005930':[{'tot_ccld_qty':'2','avg_prvs':'900'}]})
        assert [dict(r) for r in await storage.pool.fetch('SELECT * FROM trades')] == before
        assert storage._journal.get_trade('t1').to_dict() == cached
    finally:
        await storage.disconnect()


@pytest.mark.asyncio
async def test_identified_buy_preserves_signal_score_in_sealed_event_and_db(pg_socket,tmp_path,monkeypatch):
    storage = await storage_for(pg_socket,tmp_path,monkeypatch)
    try:
        trade = buy(storage,signal_score=77)
        record = next(iter(trade.execution_records.values()))
        assert record['event']['signal_score'] == 77
        await storage._write_queue.join()
        assert storage.get_execution_receipt('scope','e1').status == 'committed'
        assert await storage.pool.fetchval('SELECT signal_score FROM trade_events') == 77
    finally:
        await storage.disconnect()


@pytest.mark.asyncio
async def test_legacy_db_restore_does_not_strip_identified_registry(pg_socket,tmp_path,monkeypatch):
    from src.core.evolution.trade_journal import TradeJournal
    storage = await storage_for(pg_socket,tmp_path,monkeypatch)
    try:
        buy(storage)
        sell(storage,exit_quantity=2)
        await storage._write_queue.join()
        original_connect = asyncpg.connect
        original_create_pool = asyncpg.create_pool
        async def isolated_connect(*args,**kwargs):
            return await original_connect(host=pg_socket,database='postgres',user='qwq_test',
                                          password='synthetic-unused',port=5432,ssl=False)
        async def isolated_pool(*args,**kwargs):
            return await original_create_pool(host=pg_socket,database='postgres',user='qwq_test',
                                              password='synthetic-unused',port=5432,ssl=False,min_size=1,max_size=2)
        monkeypatch.setattr(asyncpg,'connect',isolated_connect)
        monkeypatch.setattr(asyncpg,'create_pool',isolated_pool)
        monkeypatch.setenv('DATABASE_URL','synthetic-unused')
        empty = TradeJournal(str(tmp_path/'empty-journal'))
        assert await empty._async_fetch_trade('synthetic-unused','t1') is None
        await empty.sync_from_db()
        assert empty.get_trade('t1') is None
    finally:
        await storage.disconnect()
