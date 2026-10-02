"""DB 연결 없는 상태와 JSON실패를 commit으로 오인하지 않는다."""
from datetime import date

import pytest

from src.data.storage.execution_journal import ExecutionIdentity
from src.data.storage.trade_storage import TradeStorage


def record(storage, n="e1", **patch):
    args = dict(trade_id="t1", symbol="005930", name="합성", entry_price=100,
                entry_quantity=2, entry_reason="entry", entry_strategy="gap",
                execution_identity=ExecutionIdentity("scope", date.today(), "0001", n))
    args.update(patch)
    return storage.record_entry(**args)


@pytest.fixture
def storage(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADE_JOURNAL_DIR", str(tmp_path))
    return TradeStorage(db_url="synthetic-unused")


def test_unavailable_db_is_not_commit_and_duplicate_keeps_memory(storage):
    record(storage)
    record(storage)
    assert storage.get_trade("t1").entry_quantity == 2
    assert storage.get_execution_receipt("scope", "e1").status == "unavailable"


def test_json_failure_has_failed_receipt_and_no_queue_job(storage, monkeypatch):
    import os
    monkeypatch.setattr(os, "replace", lambda *a: (_ for _ in ()).throw(OSError("synthetic")))
    with pytest.raises(OSError):
        record(storage)
    assert storage.get_execution_receipt("scope", "e1").status == "failed"
    assert storage.get_trade("t1") is None


@pytest.mark.asyncio
async def test_lookup_never_uses_json_as_commit_evidence(storage):
    record(storage)
    assert (await storage.lookup_execution_receipt("scope", "e1")).status == "unavailable"


@pytest.mark.asyncio
async def test_disconnect_fences_new_execution_enqueue(storage):
    await storage.disconnect()
    record(storage)
    assert storage.get_execution_receipt("scope", "e1").status == "unavailable"


def test_corrupt_returned_journal_batch_is_rejected_before_enqueue(storage, monkeypatch):
    import asyncio
    storage.pool = object()
    storage._db_available = True
    storage._write_queue = asyncio.Queue()
    original = storage._journal.record_entry
    def corrupted_return(**args):
        trade = original(**args)
        next(iter(trade.execution_records.values()))["summary"]["entry_quantity"] = 999
        return trade
    monkeypatch.setattr(storage._journal,"record_entry",corrupted_return)
    with pytest.raises(ValueError):
        record(storage)
    assert storage._write_queue.empty()
    assert storage.get_execution_receipt("scope","e1").status == "failed"
