"""영구 실행 원장의 임시 SQLite 기반 장애·세션 경계 검증."""

import asyncio
import json
import sqlite3
from decimal import Decimal

import pytest

from src.execution.execution_ledger import ExecutionLedger, ExecutionLedgerError


FACTS = {
    "local_id": "order-1", "symbol": "005930", "side": "buy", "quantity": 10,
    "order_date": "20261002", "strategy": "gap", "reason": "entry",
    "partial_exit": False,
}


async def started(tmp_path, session="s1"):
    ledger = ExecutionLedger(tmp_path / "executions.sqlite", "scope-test")
    await ledger.open(session)
    return ledger


async def accepted(ledger, key="s1:order-1", **facts):
    await ledger.intent(key, {**FACTS, **facts})
    await ledger.accepted(key, "000123", "001")
    return key


@pytest.mark.asyncio
async def test_constructor_has_no_io_and_initial_open_is_idempotent(tmp_path):
    path = tmp_path / "new" / "executions.sqlite"
    ledger = ExecutionLedger(path, "scope-test")
    assert not path.exists()
    snapshot = await ledger.open("s1")
    assert snapshot == {"session_id": "s1", "prior_unclean": False, "orders": {}}
    assert await ledger.open("s1") == snapshot
    assert await ledger.close_session() is True


@pytest.mark.asyncio
async def test_reopen_keeps_immutable_facts_observation_and_receipts(tmp_path):
    ledger = await started(tmp_path)
    key = await accepted(ledger)
    execution_id = await ledger.observe(key, 4, Decimal("100"))
    assert execution_id == "s1:order-1:4"
    assert await ledger.observe(key, 4, Decimal("100.0")) is None
    await ledger.receipt(execution_id, "portfolio_applied")
    await ledger.receipt(execution_id, "portfolio_applied")
    await ledger.receipt(execution_id, "handoff_returned")
    execution_id2 = await ledger.observe(key, 10, Decimal("106"))
    assert execution_id2 == "s1:order-1:10"
    order = (await ledger.snapshot())["orders"][key]
    assert order["observed_quantity"] == 10
    assert order["terminal_quantity"] == 10
    assert order["executions"] == [
        {"execution_id": "s1:order-1:4", "from_quantity": 0, "to_quantity": 4,
         "price": "100", "portfolio_applied": True, "handoff_returned": True},
        {"execution_id": "s1:order-1:10", "from_quantity": 4, "to_quantity": 10,
         "price": "110", "portfolio_applied": False, "handoff_returned": False},
    ]
    assert await ledger.close_session() is False
    reopened = await started(tmp_path, "s2")
    snapshot = await reopened.snapshot()
    assert snapshot["prior_unclean"] is True
    assert snapshot["orders"][key] == order
    with pytest.raises(ExecutionLedgerError):
        await reopened.receipt(execution_id2, "portfolio_applied")


@pytest.mark.asyncio
async def test_complete_handoff_allows_clean_next_start(tmp_path):
    ledger = await started(tmp_path)
    key = await accepted(ledger)
    execution_id = await ledger.observe(key, 10, Decimal("100"))
    await ledger.receipt(execution_id, "portfolio_applied")
    await ledger.receipt(execution_id, "handoff_returned")
    await ledger.receipt(execution_id, "handoff_returned")
    assert await ledger.close_session() is True
    assert await ledger.close_session() is True
    assert (await (await started(tmp_path, "s2")).snapshot())["prior_unclean"] is False


@pytest.mark.asyncio
async def test_empty_unclean_session_remains_hold_across_restarts(tmp_path):
    await started(tmp_path)
    second = await started(tmp_path, "s2")
    assert await second.close_session() is False
    third = await started(tmp_path, "s3")
    assert (await third.snapshot())["prior_unclean"] is True
    assert await third.close_session() is False


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["not_sent", "rejected"])
async def test_terminal_no_execution_outcomes_close_cleanly(tmp_path, status):
    ledger = await started(tmp_path)
    await ledger.intent("key", FACTS)
    await ledger.outcome("key", status)
    await ledger.outcome("key", status)
    assert await ledger.close_session() is True


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["unknown", "modified"])
async def test_ambiguous_outcomes_cannot_be_observed_or_closed(tmp_path, status):
    ledger = await started(tmp_path)
    key = await accepted(ledger)
    await ledger.outcome(key, status)
    assert await ledger.close_session() is False
    with pytest.raises(ExecutionLedgerError):
        await ledger.observe(key, 10, Decimal("100"), terminal=True)


@pytest.mark.asyncio
async def test_canceled_zero_requires_explicit_final_observation(tmp_path):
    ledger = await started(tmp_path)
    key = await accepted(ledger)
    await ledger.outcome(key, "canceled")
    assert await ledger.close_session() is False
    assert await ledger.observe(key, 0, Decimal("0"), terminal=True) is None
    assert (await ledger.snapshot())["orders"][key]["terminal_quantity"] == 0
    assert await ledger.close_session() is True


@pytest.mark.asyncio
async def test_terminal_flag_on_duplicate_partial_observation(tmp_path):
    ledger = await started(tmp_path)
    key = await accepted(ledger)
    execution_id = await ledger.observe(key, 4, Decimal("100"))
    await ledger.outcome(key, "canceled")
    assert await ledger.observe(key, 4, Decimal("100"), terminal=True) is None
    await ledger.receipt(execution_id, "portfolio_applied")
    await ledger.receipt(execution_id, "handoff_returned")
    assert await ledger.close_session() is True


@pytest.mark.asyncio
async def test_duplicate_intent_and_identity_idempotent_but_conflicts_fail(tmp_path):
    ledger = await started(tmp_path)
    key = await accepted(ledger)
    await ledger.intent(key, dict(FACTS))
    await ledger.accepted(key, "000123", "001")
    before = await ledger.snapshot()
    with pytest.raises(ExecutionLedgerError):
        await ledger.accepted(key, "000124", "001")
    assert await ledger.snapshot() == before


@pytest.mark.asyncio
async def test_same_date_broker_id_cannot_belong_to_two_keys(tmp_path):
    ledger = await started(tmp_path)
    await accepted(ledger)
    await ledger.intent("other", {**FACTS, "local_id": "other"})
    with pytest.raises(ExecutionLedgerError):
        await ledger.accepted("other", "000123", "002")


@pytest.mark.asyncio
@pytest.mark.parametrize("quantity,average", [
    (3, "100"), (4, "101"), (11, "100"), (5, "70"),
    (-1, "100"), (True, "100"), (5, "NaN"), (5, "Infinity"), (5, "0"),
])
async def test_bad_cumulative_evidence_cannot_change_projection(tmp_path, quantity, average):
    ledger = await started(tmp_path)
    key = await accepted(ledger)
    await ledger.observe(key, 4, Decimal("100"))
    before = await ledger.snapshot()
    with pytest.raises(ExecutionLedgerError):
        await ledger.observe(key, quantity, Decimal(average))
    assert await ledger.snapshot() == before


@pytest.mark.asyncio
async def test_receipt_rejects_return_before_application(tmp_path):
    ledger = await started(tmp_path)
    execution_id = await ledger.observe(await accepted(ledger), 1, Decimal("100"))
    with pytest.raises(ExecutionLedgerError):
        await ledger.receipt(execution_id, "handoff_returned")
    assert (await ledger.snapshot())["orders"]["s1:order-1"]["executions"][0]["handoff_returned"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [
    {"quantity": True}, {"quantity": 0}, {"side": "BUY"}, {"order_date": "20260230"},
    {"partial_exit": 1}, {"token": "forbidden"}, {"symbol": ""},
])
async def test_invalid_or_extra_fact_fields_are_rejected(tmp_path, change):
    ledger = await started(tmp_path)
    with pytest.raises(ExecutionLedgerError):
        await ledger.intent("key", {**FACTS, **change})
    assert (await ledger.snapshot())["orders"] == {}


@pytest.mark.asyncio
async def test_failed_transaction_leaves_neither_event_nor_projection(tmp_path):
    ledger = await started(tmp_path)
    path = tmp_path / "executions.sqlite"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TRIGGER deny_order BEFORE INSERT ON orders BEGIN SELECT RAISE(ABORT, 'disk failure simulation'); END")
        before = connection.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    with pytest.raises(ExecutionLedgerError):
        await ledger.intent("key", FACTS)
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM events").fetchone()[0] == before
        assert connection.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0
    assert await ledger.close_session() is False


@pytest.mark.asyncio
async def test_wrong_scope_schema_and_corrupt_database_fail_closed(tmp_path):
    ledger = await started(tmp_path)
    assert await ledger.close_session() is True
    with pytest.raises(ExecutionLedgerError):
        await ExecutionLedger(tmp_path / "executions.sqlite", "other-scope").open("s2")
    with sqlite3.connect(tmp_path / "executions.sqlite") as connection:
        connection.execute("PRAGMA user_version = 999")
    with pytest.raises(ExecutionLedgerError):
        await started(tmp_path, "s3")
    corrupt = tmp_path / "corrupt.sqlite"
    corrupt.write_bytes(b"not a sqlite database")
    with pytest.raises(ExecutionLedgerError):
        await ExecutionLedger(corrupt, "scope-test").open("s4")
    assert corrupt.read_bytes() == b"not a sqlite database"


@pytest.mark.asyncio
async def test_projection_tampering_is_rejected_on_load(tmp_path):
    ledger = await started(tmp_path)
    await accepted(ledger)
    with sqlite3.connect(tmp_path / "executions.sqlite") as connection:
        row = connection.execute("SELECT payload FROM orders").fetchone()[0]
        projection = json.loads(row)
        projection["facts"]["quantity"] = 100
        connection.execute("UPDATE orders SET payload = ?", (json.dumps(projection),))
    with pytest.raises(ExecutionLedgerError):
        await started(tmp_path, "s2")


@pytest.mark.asyncio
async def test_second_session_fences_previous_writer_and_reused_session_ids(tmp_path):
    ledger = await started(tmp_path)
    await started(tmp_path, "s2")
    with pytest.raises(ExecutionLedgerError):
        await ledger.intent("key", FACTS)
    with pytest.raises(ExecutionLedgerError):
        await started(tmp_path, "s1")


@pytest.mark.asyncio
async def test_concurrent_duplicate_observations_create_one_execution(tmp_path):
    ledger = await started(tmp_path)
    key = await accepted(ledger)
    results = await asyncio.gather(*(ledger.observe(key, 4, Decimal("100")) for _ in range(8)))
    assert results.count("s1:order-1:4") == 1
    assert results.count(None) == 7
    assert len((await ledger.snapshot())["orders"][key]["executions"]) == 1


@pytest.mark.asyncio
async def test_first_creation_directory_sync_failure_is_not_success(tmp_path, monkeypatch):
    import os

    def fail_sync(descriptor):
        raise OSError("directory persistence failure")

    ledger = ExecutionLedger(tmp_path / "nested" / "ledger.sqlite", "scope-test")
    with monkeypatch.context() as patch:
        patch.setattr(os, "fsync", fail_sync)
        with pytest.raises(ExecutionLedgerError):
            await ledger.open("s1")
    assert await ledger.close_session() is False
    reopened = ExecutionLedger(tmp_path / "nested" / "ledger.sqlite", "scope-test")
    assert (await reopened.open("s2"))["prior_unclean"] is True


@pytest.mark.asyncio
async def test_empty_existing_file_is_not_a_new_database(tmp_path):
    path = tmp_path / "executions.sqlite"
    path.touch()
    with pytest.raises(ExecutionLedgerError):
        await started(tmp_path)
    assert path.stat().st_size == 0


@pytest.mark.asyncio
async def test_unknown_schema_table_is_rejected(tmp_path):
    await started(tmp_path)
    with sqlite3.connect(tmp_path / "executions.sqlite") as connection:
        connection.execute("CREATE TABLE unsupported_extension (value TEXT)")
    with pytest.raises(ExecutionLedgerError):
        await started(tmp_path, "s2")


@pytest.mark.asyncio
async def test_cancellation_drains_physical_transaction_before_unlock(tmp_path, monkeypatch):
    import threading

    ledger = await started(tmp_path)
    entered, release = threading.Event(), threading.Event()
    save = ledger._save_projection

    def delayed_save(connection, state):
        entered.set()
        assert release.wait(5)
        return save(connection, state)

    monkeypatch.setattr(ledger, "_save_projection", delayed_save)
    pending = asyncio.create_task(ledger.intent("key", FACTS))
    assert await asyncio.to_thread(entered.wait, 5)
    pending.cancel()
    await asyncio.sleep(0)
    pending.cancel()
    queued = asyncio.create_task(ledger.snapshot())
    await asyncio.sleep(0)
    assert not pending.done()
    assert not queued.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert "key" in (await queued)["orders"]
    assert await ledger.close_session() is False
    with pytest.raises(ExecutionLedgerError):
        await ledger.intent("later", {**FACTS, "local_id": "later"})


@pytest.mark.asyncio
async def test_empty_orgno_and_late_canceled_preserve_terminal_evidence(tmp_path):
    ledger = await started(tmp_path)
    await ledger.intent("key", FACTS)
    await ledger.accepted("key", "000123", "")
    execution_id = await ledger.observe("key", 10, Decimal("100"))
    await ledger.outcome("key", "canceled")
    await ledger.receipt(execution_id, "portfolio_applied")
    await ledger.receipt(execution_id, "handoff_returned")
    assert await ledger.close_session() is True


@pytest.mark.asyncio
async def test_open_instance_detects_projection_corruption_before_mutating(tmp_path):
    ledger = await started(tmp_path)
    key = await accepted(ledger)
    with sqlite3.connect(tmp_path / "executions.sqlite") as connection:
        connection.execute("UPDATE orders SET payload = '{}' WHERE order_key = ?", (key,))
    with pytest.raises(ExecutionLedgerError):
        await ledger.observe(key, 10, Decimal("100"))
    assert await ledger.close_session() is False


@pytest.mark.asyncio
async def test_new_order_does_not_rewrite_unchanged_prior_order(tmp_path):
    ledger = await started(tmp_path)
    await accepted(ledger)
    with sqlite3.connect(tmp_path / "executions.sqlite") as connection:
        connection.execute("CREATE TRIGGER no_unchanged_update BEFORE UPDATE ON orders WHEN OLD.payload = NEW.payload BEGIN SELECT RAISE(ABORT, 'unchanged historical order rewrite'); END")
    await ledger.intent("second", {**FACTS, "local_id": "second"})
    assert set((await ledger.snapshot())["orders"]) == {"s1:order-1", "second"}


@pytest.mark.asyncio
async def test_extreme_decimal_arithmetic_failure_is_ledger_error(tmp_path):
    ledger = await started(tmp_path)
    key = await accepted(ledger)
    with pytest.raises(ExecutionLedgerError):
        await ledger.observe(key, 10, Decimal("1E+999999999"))
    assert (await ledger.snapshot())["orders"][key]["executions"] == []
