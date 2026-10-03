"""복구 취소·지연과 부분 날짜 캐시의 합성 무결성 회귀."""
import asyncio
from copy import deepcopy
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
import json
import threading

import asyncpg
import pytest

from test_trade_storage_execution_postgres import pg_socket

from src.core.evolution.trade_journal import TradeJournal, TradeRecord
from src.data.storage.execution_journal import ExecutionIdentity, ExecutionPayloadError
from src.data.storage.trade_storage import TradeStorage


def test_legacy_partial_exit_recovers_at_database_numeric_precision(tmp_path, monkeypatch):
    journal = TradeJournal(str(tmp_path))
    trade = legacy("rounded")
    trade.entry_price, trade.entry_quantity = 10000, 3
    journal._trades[trade.id] = trade
    updated = journal.record_exit(trade_id=trade.id, exit_price=11000, exit_quantity=1,
        exit_reason="합성", exit_type="take_profit", avg_entry_price=10000)
    original = updated.to_dict()
    db = deepcopy(updated)
    # legacy UPDATE는 pnl을 정수 round 후, pct를 NUMERIC(8,4)에 저장한다.
    db.pnl = round(float(db.pnl))
    db.pnl_pct = float(Decimal(str(db.pnl_pct)).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP))
    assert original["pnl_pct"] != db.pnl_pct
    fresh = TradeJournal(str(tmp_path))
    monkeypatch.setattr(fresh, "_recover_trade_from_db_sync", lambda _: db)
    assert fresh.recover_trade(trade.id)
    assert fresh.get_trade(trade.id).to_dict() == original


def legacy(trade_id="old1", symbol="005930"):
    return TradeRecord(id=trade_id, symbol=symbol, name="합성",
        entry_time=datetime.now().replace(microsecond=0) - timedelta(days=45),
        entry_price=100, entry_quantity=10, entry_strategy="gap")


def save_day(journal, *trades):
    path = journal._get_file_path(trades[0].entry_time.date())
    path.write_text(json.dumps(dict(date=trades[0].entry_time.date().isoformat(),
        count=len(trades), trades=[t.to_dict() for t in trades], audit={"source": "synthetic"})))
    return path


def sell(journal, trade_id="old1", execution_id="sell1"):
    return journal.record_exit(trade_id=trade_id, exit_price=120, exit_quantity=1,
        exit_reason="합성", exit_type="take_profit", avg_entry_price=100,
        execution_identity=ExecutionIdentity("synthetic", datetime.now().date(), "001", execution_id))


def test_old_day_partial_recovery_preserves_peer_and_metadata(tmp_path, monkeypatch):
    journal = TradeJournal(str(tmp_path))
    first, peer = legacy(), legacy("old2", "000660")
    first.market_context = {"preserved": "entry evidence"}
    first.review_notes = "기존 복기"
    peer.market_context = {"peer": "evidence"}
    path = save_day(journal, first, peer)
    db = deepcopy(first)
    db.market_context, db.review_notes = {}, ""
    monkeypatch.setattr(journal, "_recover_trade_from_db_sync", lambda _: db)
    assert journal.recover_trade("old1")
    sell(journal)
    payload = json.loads(path.read_text())
    rows = {r["id"]: r for r in payload["trades"]}
    assert set(rows) == {"old1", "old2"}
    assert rows["old2"] == peer.to_dict()
    assert rows["old1"]["market_context"] == {"preserved": "entry evidence"}
    assert rows["old1"]["review_notes"] == "기존 복기"
    assert payload["audit"] == {"source": "synthetic"}
    assert payload["count"] == 2
    assert journal.update_market_context("old1", {"new": "context"})
    assert len(json.loads(path.read_text())["trades"]) == 2


def test_late_sync_recovery_preserves_newest_object_and_registry(tmp_path, monkeypatch):
    journal = TradeJournal(str(tmp_path))
    stale = legacy()
    entered, release = threading.Event(), threading.Event()
    errors = []
    def slow(_):
        entered.set()
        assert release.wait(5)
        return stale
    monkeypatch.setattr(journal, "_recover_trade_from_db_sync", slow)
    def recover():
        try:
            assert journal.recover_trade("old1")
        except BaseException as error:
            errors.append(error)
    worker = threading.Thread(target=recover)
    worker.start()
    try:
        assert entered.wait(5)
        journal._trades["old1"] = deepcopy(stale)
        newest = sell(journal)
        path = journal._get_file_path(stale.entry_time.date())
        before = path.read_bytes()
    finally:
        release.set()
        worker.join(5)
    assert not worker.is_alive() and not errors
    assert journal.get_trade("old1") is newest
    assert newest.exit_quantity == 1 and len(newest.execution_records) == 1
    assert path.read_bytes() == before


@pytest.mark.asyncio
@pytest.mark.parametrize("facade", ["journal", "storage"])
@pytest.mark.parametrize("cancel", [True, False])
async def test_async_recovery_cancellation_and_late_result_never_rewind(tmp_path, monkeypatch, facade, cancel):
    monkeypatch.setenv("TRADE_JOURNAL_DIR", str(tmp_path))
    owner = TradeJournal(str(tmp_path)) if facade == "journal" else TradeStorage(db_url="synthetic-unused")
    journal = owner if facade == "journal" else owner._journal
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    stale = legacy()
    def slow(_):
        entered.set()
        try:
            assert release.wait(5)
            return deepcopy(stale)
        finally:
            finished.set()
    monkeypatch.setattr(journal, "_recover_trade_from_db_sync", slow)
    assert hasattr(owner, "recover_trade_async"), "복구 I/O와 캐시 게시를 분리한 async 경로 필요"
    task = asyncio.create_task(owner.recover_trade_async("old1"))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        if cancel:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        journal._trades["old1"] = deepcopy(stale)
        newest = sell(journal)
        path = journal._get_file_path(stale.entry_time.date())
        before = path.read_bytes()
    finally:
        release.set()
    assert await asyncio.to_thread(finished.wait, 5)
    if not cancel:
        assert await task
    await asyncio.sleep(0)
    assert journal.get_trade("old1") is newest
    assert newest.exit_quantity == 1 and len(newest.execution_records) == 1
    assert path.read_bytes() == before


@pytest.mark.parametrize("damage", ["json", "shape", "duplicate", "identity", "registry"])
def test_existing_day_corruption_or_conflict_fails_closed(tmp_path, monkeypatch, damage):
    journal = TradeJournal(str(tmp_path))
    original = legacy()
    journal._trades["old1"] = original
    path = save_day(journal, original, legacy("old2", "000660"))
    data = json.loads(path.read_text())
    if damage == "json":
        path.write_text("{")
    elif damage == "shape":
        path.write_text(json.dumps({"trades": {}}))
    else:
        if damage == "duplicate":
            data["trades"].append(deepcopy(data["trades"][0]))
        elif damage == "identity":
            data["trades"][0]["symbol"] = "999999"
        else:
            data["trades"][1]["execution_records"] = {"broken": {}}
        path.write_text(json.dumps(data))
    before = path.read_bytes()
    with pytest.raises((ExecutionPayloadError, ValueError)):
        sell(journal)
    assert journal.get_trade("old1") is original
    assert original.exit_quantity == 0 and original.execution_records == {}
    assert path.read_bytes() == before


def test_disk_legacy_accounting_conflict_cannot_be_overwritten(tmp_path):
    journal = TradeJournal(str(tmp_path))
    cached = legacy()
    journal._trades[cached.id] = cached
    conflicting = deepcopy(cached)
    conflicting.entry_quantity = 20
    path = save_day(journal, conflicting)
    before = path.read_bytes()
    with pytest.raises(ExecutionPayloadError):
        sell(journal)
    assert journal.get_trade(cached.id) is cached
    assert path.read_bytes() == before


@pytest.mark.parametrize("field,value", [("entry_quantity", 20), ("entry_price", 200), ("exit_quantity", 3)])
def test_legacy_recovery_refuses_conflicting_disk_accounting(tmp_path, monkeypatch, field, value):
    journal = TradeJournal(str(tmp_path))
    db, disk = legacy(), None
    disk = deepcopy(db)
    setattr(disk, field, value)
    path = save_day(journal, disk)
    before = path.read_bytes()
    monkeypatch.setattr(journal, "_recover_trade_from_db_sync", lambda _: db)
    with pytest.raises(ExecutionPayloadError):
        journal.recover_trade(db.id)
    assert journal.get_trade(db.id) is None
    assert path.read_bytes() == before


def test_stale_disk_prefix_keeps_newer_memory_and_peer_registry(tmp_path):
    journal = TradeJournal(str(tmp_path))
    first, peer = legacy(), legacy("old2", "000660")
    journal._trades.update({first.id: first, peer.id: peer})
    peer = sell(journal, "old2", "peer-sell")
    sell(journal)
    path = journal._get_file_path(first.entry_time.date())
    stale = path.read_bytes()
    sell(journal, execution_id="sell2")
    newest = journal.get_trade("old1")
    path.write_bytes(stale)
    sell(journal, execution_id="sell3")
    rows = {r["id"]: r for r in json.loads(path.read_text())["trades"]}
    assert newest.exit_quantity == 2
    assert rows["old1"]["exit_quantity"] == 3
    assert len(rows["old1"]["execution_records"]) == 3
    assert rows["old2"] == peer.to_dict()


@pytest.mark.asyncio
async def test_timeout_without_replacement_does_not_publish_after_db_returns(tmp_path, monkeypatch):
    journal = TradeJournal(str(tmp_path))
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    def slow(_):
        entered.set()
        try:
            assert release.wait(5)
            return legacy()
        finally:
            finished.set()
    monkeypatch.setattr(journal, "_recover_trade_from_db_sync", slow)
    task = asyncio.create_task(journal.recover_trade_async("old1"))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(task, 0.01)
    finally:
        release.set()
    assert await asyncio.to_thread(finished.wait, 5)
    await asyncio.sleep(0)
    assert journal._trades == {}
    assert list(tmp_path.glob("trades_*.json")) == []


def test_valid_but_divergent_execution_history_is_not_merged(tmp_path):
    journal = TradeJournal(str(tmp_path / "main"))
    other = TradeJournal(str(tmp_path / "other"))
    original = legacy()
    journal._trades[original.id] = deepcopy(original)
    other._trades[original.id] = deepcopy(original)
    newest = sell(journal, execution_id="memory-execution")
    sell(other, execution_id="disk-execution")
    path = journal._get_file_path(original.entry_time.date())
    path.write_bytes(other._get_file_path(original.entry_time.date()).read_bytes())
    before = path.read_bytes()
    with pytest.raises(ExecutionPayloadError):
        sell(journal, execution_id="next-execution")
    assert journal.get_trade(original.id) is newest
    assert newest.exit_quantity == 1
    assert path.read_bytes() == before


@pytest.mark.asyncio
async def test_async_recovery_preserves_disk_registry_and_duplicate_sell(tmp_path, monkeypatch):
    journal = TradeJournal(str(tmp_path))
    db = legacy()
    journal._trades[db.id] = deepcopy(db)
    original = sell(journal)
    path = journal._get_file_path(db.entry_time.date())
    before = path.read_bytes()
    reopened = TradeJournal(str(tmp_path))
    assert reopened.get_trade(db.id) is None  # 30일 창 밖
    monkeypatch.setattr(reopened, "_recover_trade_from_db_sync", lambda _: deepcopy(db))
    assert await reopened.recover_trade_async(db.id)
    assert reopened.get_trade(db.id).to_dict() == original.to_dict()
    repeated = sell(reopened)
    assert repeated.exit_quantity == 1 and len(repeated.execution_records) == 1
    assert path.read_bytes() == before


def test_old_day_atomic_write_failure_preserves_file_and_cached_trade(tmp_path, monkeypatch):
    import os
    journal = TradeJournal(str(tmp_path))
    first, peer = legacy(), legacy("old2", "000660")
    path = save_day(journal, first, peer)
    journal._trades[first.id] = first
    before = path.read_bytes()
    def fail_replace(*args):
        raise OSError("synthetic replace failure")
    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(OSError):
        sell(journal)
    assert journal.get_trade(first.id) is first
    assert first.exit_quantity == 0 and first.execution_records == {}
    assert path.read_bytes() == before
    assert list(tmp_path.glob(".execution-*")) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("pnl,pct,stored_pnl,stored_pct", [
    (976.5, 1.23445, "976.00", "1.2345"),
    (977.5, 3.253333333, "978.00", "3.2533"),
    (-976.5, -1.23445, "-976.00", "-1.2345"),
])
async def test_legacy_recovery_matches_real_numeric_codec_without_changing_json(
        pg_socket, tmp_path, monkeypatch, pnl, pct, stored_pnl, stored_pct):
    journal = TradeJournal(str(tmp_path))
    original = legacy()
    original.entry_price, original.exit_price = 100.005, 110.015
    original.exit_quantity, original.pnl, original.pnl_pct = 1, pnl, pct
    path = save_day(journal, original)
    before = path.read_bytes()
    conn = await asyncpg.connect(host=pg_socket, database="postgres", user="qwq_test",
        password="synthetic-unused", port=5432, ssl=False)
    try:
        # 실제 legacy writer의 float 인자 및 PnL 선행 round를 그대로 보낸다.
        row = await conn.fetchrow("""SELECT $1::NUMERIC(12,2) AS entry_price,
            $2::NUMERIC(12,2) AS exit_price, $3::NUMERIC(14,2) AS pnl,
            $4::NUMERIC(8,4) AS pnl_pct""",
            original.entry_price, original.exit_price, round(float(pnl)), pct)
    finally:
        await conn.close()
    assert tuple(row) == (Decimal("100.00"), Decimal("110.02"),
                          Decimal(stored_pnl), Decimal(stored_pct))
    db = deepcopy(original)
    for field, value in row.items():
        setattr(db, field, float(value))
    monkeypatch.setattr(journal, "_recover_trade_from_db_sync", lambda _: db)
    assert await journal.recover_trade_async(original.id)
    assert journal.get_trade(original.id).to_dict() == original.to_dict()
    assert path.read_bytes() == before


@pytest.mark.parametrize("field,wrong", [
    ("entry_price", 100.01), ("exit_price", 110.03), ("pnl", 977),
    ("pnl_pct", 1.2344), ("entry_quantity", 11), ("symbol", "000660"),
])
def test_numeric_normalization_still_rejects_distinct_stored_value(
        tmp_path, monkeypatch, field, wrong):
    journal = TradeJournal(str(tmp_path))
    original = legacy()
    original.entry_price, original.exit_price = 100.005, 110.015
    original.exit_quantity, original.pnl, original.pnl_pct = 1, 976.5, 1.23445
    path = save_day(journal, original)
    before = path.read_bytes()
    db = deepcopy(original)
    db.entry_price, db.exit_price, db.pnl, db.pnl_pct = 100, 110.02, 976, 1.2345
    setattr(db, field, wrong)
    monkeypatch.setattr(journal, "_recover_trade_from_db_sync", lambda _: db)
    with pytest.raises(ExecutionPayloadError):
        journal.recover_trade(original.id)
    assert journal.get_trade(original.id) is None
    assert path.read_bytes() == before
