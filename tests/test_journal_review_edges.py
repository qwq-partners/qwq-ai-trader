"""리뷰 후속: legacy DB 보강 단조성·오염 분류·복구 I/O 수명."""
import asyncio
from copy import deepcopy
from datetime import timedelta
import json
import time

import asyncpg
import pytest

import src.core.evolution.trade_journal as journal_module
from src.core.evolution.trade_journal import TradeJournal
from src.data.storage.execution_journal import ExecutionPayloadError
from test_trade_recovery_integrity import legacy, save_day, sell


@pytest.mark.asyncio
@pytest.mark.parametrize("db_quantity", [3, 5, 7])
async def test_legacy_db_sync_only_advances_exit_quantity_and_day_remains_writable(
        tmp_path, monkeypatch, db_quantity):
    journal = TradeJournal(str(tmp_path))
    trade = legacy()
    trade.exit_quantity, trade.exit_price = 5, 123.456
    trade.exit_time = trade.entry_time + timedelta(days=1)
    trade.pnl, trade.pnl_pct = 117.123456, 11.7123456
    journal._trades[trade.id] = trade
    path = save_day(journal, trade, legacy("peer", "000660"))
    before = deepcopy(trade.to_dict())
    db = deepcopy(trade)
    db.exit_quantity, db.exit_price, db.pnl, db.pnl_pct = db_quantity, 124, 166, 16.6
    db.exit_time += timedelta(hours=1)
    row = {name: getattr(db, name) for name in db.__dataclass_fields__}
    class Pool:
        async def fetch(self, *args):
            return [row]
        async def close(self):
            pass
    async def pool(*args, **kwargs):
        return Pool()
    monkeypatch.setenv("DATABASE_URL", "synthetic-unused")
    monkeypatch.setattr(asyncpg, "create_pool", pool)
    await journal.sync_from_db()
    if db_quantity <= 5:
        assert journal.get_trade(trade.id).to_dict() == before
    else:
        advanced = journal.get_trade(trade.id)
        assert advanced.exit_quantity == 7 and advanced.pnl == 166
        assert advanced.exit_time == db.exit_time
        assert trade.exit_quantity == 5  # 게시 전 원본 객체는 변경하지 않는다.
    assert journal.update_market_context(trade.id, {"after_sync": True})
    rows = {r["id"]: r for r in json.loads(path.read_text())["trades"]}
    assert set(rows) == {"old1", "peer"}
    assert rows["old1"]["exit_quantity"] == max(5, db_quantity)


@pytest.mark.parametrize("field", ["entry_quantity", "entry_price"])
def test_sparse_legacy_accounting_fails_with_classified_error(tmp_path, field):
    journal = TradeJournal(str(tmp_path))
    original = legacy()
    journal._trades[original.id] = original
    path = save_day(journal, original)
    payload = json.loads(path.read_text())
    del payload["trades"][0][field]
    path.write_text(json.dumps(payload))
    before = path.read_bytes()
    with pytest.raises(ExecutionPayloadError):
        sell(journal)
    assert journal.get_trade(original.id) is original
    assert original.exit_quantity == 0
    assert path.read_bytes() == before


@pytest.mark.parametrize("raw", ["NaN", "Infinity", "-Infinity", "1e999"])
@pytest.mark.parametrize("field", ["pnl", "market_context"])
def test_nonfinite_existing_day_is_classified_without_overwriting(tmp_path, raw, field):
    journal = TradeJournal(str(tmp_path))
    trade = legacy()
    journal._trades[trade.id] = trade
    peer = legacy("peer", "000660")
    path = save_day(journal, trade, peer)
    payload = json.loads(path.read_text())
    payload["trades"][1][field] = "NONFINITE_MARKER" if field == "pnl" else {"value": "NONFINITE_MARKER"}
    path.write_text(json.dumps(payload).replace('"NONFINITE_MARKER"', raw))
    before = path.read_bytes()
    with pytest.raises(ExecutionPayloadError, match="daily_trade_payload_corrupt"):
        sell(journal)
    assert path.read_bytes() == before
    assert trade.exit_quantity == 0 and journal.get_trade(trade.id) is trade
    assert list(tmp_path.glob(".execution-*")) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("stalled", ["connect", "fetch", "close"])
async def test_each_recovery_db_phase_has_finite_lifetime(tmp_path, monkeypatch, stalled):
    journal = TradeJournal(str(tmp_path))
    monkeypatch.setattr(journal_module, "_DB_RECOVERY_IO_TIMEOUT", .02, raising=False)
    closed, terminated = [], []
    class Connection:
        async def fetchrow(self, *args, **kwargs):
            if stalled == "fetch":
                await asyncio.Future()
            return None
        async def close(self, **kwargs):
            closed.append(True)
            if stalled == "close":
                await asyncio.Future()
        def terminate(self):
            terminated.append(True)
    async def connect(*args, **kwargs):
        if stalled == "connect":
            await asyncio.Future()
        return Connection()
    monkeypatch.setattr(asyncpg, "connect", connect)
    started = time.monotonic()
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(journal._async_fetch_trade("synthetic-unused", "old1"), .5)
    assert time.monotonic() - started < .3, "복구 내부 시한이 아닌 외부 시험 시한까지 잔존"
    if stalled != "connect":
        assert closed
    if stalled == "close":
        assert terminated
    assert journal._trades == {}


@pytest.mark.asyncio
async def test_canceled_recovery_releases_underlying_worker_after_db_deadline(tmp_path, monkeypatch):
    import threading
    journal = TradeJournal(str(tmp_path))
    monkeypatch.setattr(journal_module, "_DB_RECOVERY_IO_TIMEOUT", .02)
    monkeypatch.setenv("DATABASE_URL", "synthetic-unused")
    entered, finished = threading.Event(), threading.Event()
    async def connect(*args, **kwargs):
        entered.set()
        await asyncio.Future()
    monkeypatch.setattr(asyncpg, "connect", connect)
    recover = journal._recover_trade_from_db_sync
    def tracked_recover(trade_id):
        try:
            return recover(trade_id)
        finally:
            finished.set()
    monkeypatch.setattr(journal, "_recover_trade_from_db_sync", tracked_recover)
    task = asyncio.create_task(journal.recover_trade_async("old1"))
    assert await asyncio.to_thread(entered.wait, 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await asyncio.to_thread(finished.wait, .3), "취소 후 DB 작업 스레드가 종료되지 않음"
    assert journal._trades == {}
    assert list(tmp_path.glob("trades_*.json")) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("cached", [True, False])
@pytest.mark.parametrize("fail_write", [False, True])
async def test_forward_db_sync_persists_before_direct_identified_sell(
        tmp_path, monkeypatch, cached, fail_write):
    from datetime import datetime
    import os
    journal = TradeJournal(str(tmp_path))
    trade = legacy()
    trade.entry_time = datetime.now().replace(microsecond=0) - timedelta(days=2)
    trade.exit_quantity, trade.exit_price = 5, 120
    trade.exit_time = trade.entry_time + timedelta(hours=1)
    if cached:
        journal._trades[trade.id] = trade
    path = save_day(journal, trade)
    before = path.read_bytes()
    db = deepcopy(trade)
    db.exit_quantity, db.pnl, db.pnl_pct = 7, 138, 13.8
    row = {name: getattr(db, name) for name in db.__dataclass_fields__}
    class Pool:
        async def fetch(self, *args): return [row]
        async def close(self): pass
    async def pool(*args, **kwargs): return Pool()
    monkeypatch.setenv("DATABASE_URL", "synthetic-unused")
    monkeypatch.setattr(asyncpg, "create_pool", pool)
    with monkeypatch.context() as patch:
        if fail_write:
            def fail_replace(*args): raise OSError("synthetic replace failure")
            patch.setattr(os, "replace", fail_replace)
        await journal.sync_from_db()
    if fail_write:
        assert journal.get_trade(trade.id) is (trade if cached else None)
        assert trade.exit_quantity == 5
        assert path.read_bytes() == before
    else:
        # 다른 metadata 저장이 개입하기 전에 디스크도 전진해야 한다.
        assert json.loads(path.read_text())["trades"][0]["exit_quantity"] == 7
        final = sell(journal)
        assert final.exit_quantity == 8
        assert json.loads(path.read_text())["trades"][0]["exit_quantity"] == 8


@pytest.mark.asyncio
async def test_new_db_summary_cannot_overwrite_uncached_disk_registry(tmp_path, monkeypatch):
    journal = TradeJournal(str(tmp_path))
    original = legacy()
    journal._trades[original.id] = original
    sell(journal)
    path = journal._get_file_path(original.entry_time.date())
    before = path.read_bytes()
    journal._trades.clear()
    db = deepcopy(original)
    db.exit_quantity = 2
    row = {name: getattr(db, name) for name in db.__dataclass_fields__}
    class Pool:
        async def fetch(self, *args): return [row]
        async def close(self): pass
    async def pool(*args, **kwargs): return Pool()
    monkeypatch.setenv("DATABASE_URL", "synthetic-unused")
    monkeypatch.setattr(asyncpg, "create_pool", pool)
    await journal.sync_from_db(days=60)
    assert journal.get_trade(db.id) is None
    assert path.read_bytes() == before

@pytest.mark.asyncio
@pytest.mark.parametrize('db_quantity', [8, 10])
async def test_legacy_sync_loads_uncached_equal_or_older_db_from_verified_disk(tmp_path, monkeypatch, db_quantity):
    journal = TradeJournal(str(tmp_path))
    trade = legacy()
    trade.exit_quantity = 10
    trade.exit_time = trade.entry_time + timedelta(days=1)
    trade.review_notes = 'preserve disk review'
    path = save_day(journal, trade)
    before = path.read_bytes()
    db = deepcopy(trade)
    db.exit_quantity = db_quantity
    db.review_notes = ''
    row = {name: getattr(db, name) for name in db.__dataclass_fields__}
    class Pool:
        async def fetch(self, *args): return [row]
        async def close(self): pass
    async def pool(*args, **kwargs): return Pool()
    monkeypatch.setenv('DATABASE_URL', 'synthetic-unused')
    monkeypatch.setattr(asyncpg, 'create_pool', pool)
    await journal.sync_from_db(days=60)
    loaded = journal.get_trade(trade.id)
    assert loaded is not None
    assert loaded.to_dict() == trade.to_dict()
    assert path.read_bytes() == before

@pytest.mark.asyncio
@pytest.mark.parametrize('missing_key', [False, True])
async def test_corrupt_day_does_not_block_unrelated_db_sync_day(tmp_path, monkeypatch, missing_key):
    journal = TradeJournal(str(tmp_path))
    broken, healthy = legacy('broken'), legacy('healthy')
    healthy.entry_time += timedelta(days=1)
    rows=[]
    for trade in (broken, healthy):
        trade.exit_quantity = 3
        trade.exit_time = trade.entry_time + timedelta(hours=1)
        rows.append({name: getattr(trade, name) for name in trade.__dataclass_fields__})
    if missing_key:
        rows[0].pop('id')
    path=journal._get_file_path(broken.entry_time.date())
    path.write_text('not JSON')
    class Pool:
        async def fetch(self, *args): return rows
        async def close(self): pass
    async def pool(*args, **kwargs): return Pool()
    monkeypatch.setenv('DATABASE_URL', 'synthetic-unused')
    monkeypatch.setattr(asyncpg, 'create_pool', pool)
    await journal.sync_from_db(days=60)
    assert journal.get_trade('broken') is None
    assert path.read_text() == 'not JSON'
    assert journal.get_trade('healthy').exit_quantity == 3
    assert journal._get_file_path(healthy.entry_time.date()).exists()

@pytest.mark.asyncio
async def test_directory_fsync_failure_never_publishes_success_and_retry_repairs(tmp_path, monkeypatch):
    import os
    import stat
    journal = TradeJournal(str(tmp_path))
    trade = legacy()
    trade.exit_quantity = 5
    trade.exit_time = trade.entry_time + timedelta(hours=1)
    journal._trades[trade.id] = trade
    path = save_day(journal, trade)
    db = deepcopy(trade)
    db.exit_quantity = 7
    row = {name: getattr(db, name) for name in db.__dataclass_fields__}
    fsync = os.fsync
    def fail_directory(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError('synthetic durability failure after replace')
        fsync(fd)
    with monkeypatch.context() as patch:
        patch.setattr(os, 'fsync', fail_directory)
        with pytest.raises(OSError):
            journal._merge_legacy_db_trade(row)
    assert journal.get_trade(trade.id) is trade
    assert trade.exit_quantity == 5
    # replace succeeded, but durability was not confirmed: this is not a committed write.
    assert json.loads(path.read_text())['trades'][0]['exit_quantity'] == 7
    with pytest.raises(ExecutionPayloadError):
        sell(journal)
    assert journal._merge_legacy_db_trade(row)
    assert sell(journal).exit_quantity == 8
