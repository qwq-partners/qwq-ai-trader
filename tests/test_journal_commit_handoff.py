"""식별 KR 체결의 실제 후처리와 비차단 DB commit 대기 경계."""

import asyncio
from copy import copy
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest

from src.core.event import FillEvent
from src.core.types import Fill, OrderSide
from test_fill_reconciliation import case, process
from test_kis_cancel_fill_recovery import broker, accepted, response, row
from test_trade_storage_execution_postgres import pg_socket, storage_for


@pytest.mark.parametrize("canceled", [False, True])
def test_verified_broker_identity_survives_normal_and_cancel_fill(broker, canceled):
    async def run():
        order = await accepted(broker)
        if canceled:
            await broker.cancel_order(order.id)
        response(broker, [row(3)] if canceled else [row(3, cncl_yn="N", cnc_cfrm_qty="0", rmn_qty="7")])
        fill, = await broker.check_fills()
        assert fill.account_scope == broker._execution_history.ledger.scope
        assert fill.order_date == row()["ord_dt"]
        assert fill.kis_order_no == "001"
        assert fill.execution_id
    asyncio.run(run())


def identified(side=OrderSide.BUY, qty=3, execution="s1:o1:3"):
    fill = Fill(order_id="o1", symbol="005930", side=side, quantity=qty,
                price=Decimal("10000"), execution_id=execution)
    fill.account_scope = "scope-test"
    fill.order_date = "20261002"
    fill.kis_order_no = "000123"
    return fill


class Journal:
    def __init__(self):
        self.entries, self.exits = [], []
        self._trades = {}
        self.status = "pending"

    def record_entry(self, **kwargs):
        self.entries.append(kwargs)
        rec = SimpleNamespace(id=kwargs["trade_id"], market_context={})
        self._trades[rec.id] = rec
        return rec

    def record_exit(self, **kwargs):
        self.exits.append(kwargs)
        return self._trades.get(kwargs["trade_id"])

    def get_execution_receipt(self, scope, execution):
        from src.data.storage.execution_journal import ExecutionWriteReceipt
        return ExecutionWriteReceipt(status=self.status, execution_id=execution, reason="synthetic")

    def get_open_trades(self):
        raise AssertionError("식별 SELL을 종목으로 추정하면 안 됩니다")

    def get_trade(self, key):
        return self._trades.get(key)


def setup(monkeypatch, tmp_path, holdings=0):
    sched, bot = case(monkeypatch, tmp_path, holdings=holdings)
    sched._pending_fill_handoffs = {}
    bot.trade_journal = Journal()
    receipts, acks, failures = [], [], []

    async def receipt(fill, stage):
        receipts.append((fill.execution_id, stage))

    bot.broker.record_execution_receipt = receipt
    bot.broker.acknowledge_fill = lambda *args: acks.append(args)
    bot.broker.record_execution_journal_failure = lambda reason: failures.append(reason)
    return sched, bot, receipts, acks, failures


async def enqueue(sched, bot, fill):
    event = FillEvent.from_fill(fill)
    await bot.engine.emit(event)
    await process(bot.engine)
    assert event.portfolio_applied is True
    sched._pending_fill_handoffs[event.id] = {
        "fill": fill, "event": event, "_sell_pos_snap": event.position_before,
        "_exit_reason_snap": "", "_entry_lot": None, "_order_done": True,
        "_exit_pending_generation": None,
    }
    return event


def test_pending_db_does_not_repeat_side_effects_or_ack_then_commit_finishes(monkeypatch, tmp_path):
    sched, bot, receipts, acks, failures = setup(monkeypatch, tmp_path)

    async def run():
        fill = identified()
        await enqueue(sched, bot, fill)
        await sched._drain_fill_handoffs(wait=False)
        assert len(bot.trade_journal.entries) == 1
        assert receipts == [(fill.execution_id, "portfolio_applied")]
        assert not acks
        assert bot.exit_manager.get_state(fill.symbol).remaining_quantity == 3
        await sched._drain_fill_handoffs(wait=False)
        assert len(bot.trade_journal.entries) == 1
        assert not acks
        bot.trade_journal.status = "committed"
        await sched._drain_fill_handoffs(wait=False)
        assert receipts[-1] == (fill.execution_id, "handoff_returned")
        assert acks == [(fill.order_id, 3)]
        assert not sched._pending_fill_handoffs and not failures
    asyncio.run(run())


def test_every_partial_buy_is_journaled_with_same_trade_identity(monkeypatch, tmp_path):
    sched, bot, receipts, acks, failures = setup(monkeypatch, tmp_path)

    async def run():
        await enqueue(sched, bot, identified(qty=3))
        await enqueue(sched, bot, identified(qty=7, execution="s1:o1:10"))
        await sched._drain_fill_handoffs(wait=False)
        calls = bot.trade_journal.entries
        assert len(calls) == 2
        assert calls[0]["trade_id"] == calls[1]["trade_id"]
        assert [row["entry_quantity"] for row in calls] == [3, 7]
        assert calls[0]["execution_identity"].order_date == date(2026, 10, 2)
        assert calls[1]["execution_identity"].execution_id == "s1:o1:10"
        assert calls[0]["execution_time"].tzinfo is not None
        assert bot.exit_manager.get_state("005930").remaining_quantity == 10
        assert not acks and not failures
    asyncio.run(run())


def test_pending_buy_journal_does_not_delay_protective_sell(monkeypatch, tmp_path):
    sched, bot, receipts, acks, failures = setup(monkeypatch, tmp_path)

    async def run():
        await enqueue(sched, bot, identified(qty=10))
        await sched._drain_fill_handoffs(wait=False)
        await enqueue(sched, bot, identified(OrderSide.SELL, 4, "s1:sell:4"))
        await sched._drain_fill_handoffs(wait=False)
        assert len(bot.trade_journal.entries) == 1
        assert len(bot.trade_journal.exits) == 1
        assert bot.exit_manager.get_state("005930").remaining_quantity == 6
        assert not acks and not failures
    asyncio.run(run())


@pytest.mark.parametrize("status", ["unavailable", "failed", "unknown"])
def test_unconfirmed_storage_preserves_handoff_and_latches_recovery(monkeypatch, tmp_path, status):
    sched, bot, receipts, acks, failures = setup(monkeypatch, tmp_path)
    bot.trade_journal.status = status

    async def run():
        await enqueue(sched, bot, identified())
        await sched._drain_fill_handoffs(wait=False)
        await sched._drain_fill_handoffs(wait=False)
        assert failures and not acks
        assert sched._pending_fill_handoffs
        assert len(bot.trade_journal.entries) == 1
        assert all(stage != "handoff_returned" for _, stage in receipts)
        assert bot.exit_manager.get_state("005930").remaining_quantity == 3
    asyncio.run(run())


def test_identified_sell_without_trade_id_never_guesses_by_symbol(monkeypatch, tmp_path):
    sched, bot, receipts, acks, failures = setup(monkeypatch, tmp_path, holdings=10)
    bot.trade_journal.status = "unavailable"

    async def run():
        await enqueue(sched, bot, identified(OrderSide.SELL, 3))
        await sched._drain_fill_handoffs(wait=False)
        assert not bot.trade_journal.exits
        assert failures and not acks
        assert bot.exit_manager.get_state("005930").remaining_quantity == 7
    asyncio.run(run())


def test_changed_execution_account_is_not_accepted_as_an_equal_duplicate(monkeypatch, tmp_path):
    _, bot = case(monkeypatch, tmp_path, holdings=0)

    async def run():
        fill = identified()
        first = FillEvent.from_fill(fill)
        await bot.engine.risk_manager.on_fill(first)
        changed = copy(fill)
        changed.account_scope = "other-scope"
        second = FillEvent.from_fill(changed)
        await bot.engine.risk_manager.on_fill(second)
        assert second.portfolio_applied is False
        assert not second.duplicate_execution
        assert bot.engine.portfolio.positions[fill.symbol].quantity == 3
    asyncio.run(run())


def test_failed_execution_receipt_does_not_skip_already_applied_protective_work(monkeypatch, tmp_path):
    sched, bot, receipts, acks, failures = setup(monkeypatch, tmp_path)
    completed = []

    async def broken_receipt(fill, stage):
        raise OSError("synthetic ledger failure")

    async def complete(**kwargs):
        completed.append(kwargs["fill"].execution_id)
        return True

    bot.broker.record_execution_receipt = broken_receipt
    sched._complete_fill_handoff = complete

    async def run():
        await enqueue(sched, bot, identified())
        await sched._drain_fill_handoffs(wait=False)
        await sched._drain_fill_handoffs(wait=False)
        assert completed == ["s1:o1:3"]
        assert failures and not acks
    asyncio.run(run())


def test_applied_sell_preserves_its_original_position_owner_after_full_close(monkeypatch, tmp_path):
    sched, bot, *_ = setup(monkeypatch, tmp_path)

    async def run():
        buy = await enqueue(sched, bot, identified(qty=10))
        sell = await enqueue(sched, bot, identified(OrderSide.SELL, 10, "s1:sell:10"))
        new_buy = await enqueue(sched, bot, identified(qty=3, execution="s1:new:3"))
        assert sell.position_before_owner is buy.position_owner
        assert sell.position_before_owner is not new_buy.position_owner
    asyncio.run(run())


@pytest.mark.parametrize("new_life", [False, True])
def test_delayed_buy_sell_batch_uses_original_trade_life_without_symbol_guess(monkeypatch, tmp_path, new_life):
    sched, bot, receipts, acks, failures = setup(monkeypatch, tmp_path)
    bot.trade_journal.status = "committed"

    async def run():
        await enqueue(sched, bot, identified(qty=10))
        await enqueue(sched, bot, identified(OrderSide.SELL, 10, "s1:sell:10"))
        if new_life:
            await enqueue(sched, bot, identified(qty=3, execution="s1:new:3"))
        await sched._drain_fill_handoffs(wait=False)
        entries, exits = bot.trade_journal.entries, bot.trade_journal.exits
        assert len(exits) == 1 and not failures
        assert exits[0]["trade_id"] == entries[0]["trade_id"]
        if new_life:
            assert entries[1]["trade_id"] != entries[0]["trade_id"]
            assert bot.engine.portfolio.positions["005930"].trade_id == entries[1]["trade_id"]
        assert not sched._pending_fill_handoffs
    asyncio.run(run())


def test_observed_execution_holds_new_risk_until_commit_handoff_receipt(broker):
    async def run():
        await accepted(broker)
        response(broker, [row(10, cncl_yn="N", cnc_cfrm_qty="0", rmn_qty="0")])
        fill, = await broker.check_fills()
        assert broker.unknown_buy_hold()
        assert broker.has_unknown_sell(fill.symbol)
        assert not broker.has_unknown_sell("000660")
        assert broker.execution_recovery_status()["journal_pending_count"] == 1
        await broker.record_execution_receipt(fill, "portfolio_applied")
        assert broker.unknown_buy_hold()
        await broker.record_execution_receipt(fill, "handoff_returned")
        assert broker.unknown_buy_hold() is None
        assert broker.has_unknown_sell(fill.symbol) is False
    asyncio.run(run())


def test_pending_journal_blocks_new_risk_but_allows_full_protective_sell(broker):
    from test_durable_execution_integration import order

    async def run():
        await accepted(broker)
        response(broker, [row(10, cncl_yn="N", cnc_cfrm_qty="0", rmn_qty="0")])
        fill, = await broker.check_fills()
        calls = []
        original = broker._api_post

        async def post(*args, **kwargs):
            calls.append(1)
            return await original(*args, **kwargs)

        broker._api_post = post
        assert not (await broker.submit_order(order()))[0]
        assert not (await broker.submit_order(order(OrderSide.SELL, True)))[0]
        assert calls == []
        assert (await broker.submit_order(order(OrderSide.SELL)))[0]
        assert calls == [1]
        assert broker.execution_recovery_status()["journal_pending_count"] == 1
        assert broker.unknown_buy_hold()

    asyncio.run(run())


@pytest.mark.asyncio
async def test_actual_broker_engine_storage_commit_handoff(pg_socket, broker, monkeypatch, tmp_path):
    """합성 KIS→실제 엔진→임시 PostgreSQL→SQLite 완료 receipt를 함께 검증."""
    sched, bot = case(monkeypatch, tmp_path, holdings=0)
    sched._pending_fill_handoffs = {}
    bot.broker = bot.engine.broker = broker
    storage = await storage_for(pg_socket, tmp_path, monkeypatch)
    bot.trade_journal = storage
    try:
        await accepted(broker)
        for quantity in (3, 10):
            response(broker, [row(quantity, cncl_yn="N", cnc_cfrm_qty="0", rmn_qty=str(10-quantity))])
            fill, = await broker.check_fills()
            await enqueue(sched, bot, fill)
        await sched._drain_fill_handoffs(wait=False)
        assert len(sched._pending_fill_handoffs) == 2
        assert broker.execution_recovery_status()["journal_pending_count"] == 2
        assert bot.exit_manager.get_state(fill.symbol).remaining_quantity == 10
        await storage._write_queue.join()
        await sched._drain_fill_handoffs(wait=False)
        assert not sched._pending_fill_handoffs
        assert broker.execution_recovery_status()["journal_pending_count"] == 0
        assert broker.unknown_buy_hold() is None
        events = await storage.pool.fetch("SELECT quantity,trade_id,account_scope,order_date,kis_order_no,execution_id FROM trade_events ORDER BY id")
        assert [event["quantity"] for event in events] == [3, 7]
        assert len({event["trade_id"] for event in events}) == 1
        assert all(event["account_scope"] == fill.account_scope for event in events)
        assert all(event["kis_order_no"] == "001" for event in events)
        assert all(event["execution_id"] for event in events)
        assert await storage.pool.fetchval("SELECT entry_quantity FROM trades") == 10
    finally:
        await storage.disconnect()


@pytest.mark.asyncio
async def test_actual_storage_preserves_buy_close_reentry_trade_lifetimes(pg_socket, monkeypatch, tmp_path):
    sched, bot, receipts, acks, failures = setup(monkeypatch, tmp_path)
    storage = await storage_for(pg_socket, tmp_path, monkeypatch)
    bot.trade_journal = storage
    try:
        await enqueue(sched, bot, identified(qty=10))
        await enqueue(sched, bot, identified(OrderSide.SELL, 10, "s1:sell:10"))
        await enqueue(sched, bot, identified(qty=3, execution="s1:new:3"))
        await sched._drain_fill_handoffs(wait=False)
        assert not acks and not failures
        assert bot.exit_manager.get_state("005930").remaining_quantity == 3
        await storage._write_queue.join()
        await sched._drain_fill_handoffs(wait=False)
        rows = await storage.pool.fetch("SELECT id,entry_quantity,exit_quantity FROM trades ORDER BY entry_quantity DESC")
        assert [(r["entry_quantity"], r["exit_quantity"]) for r in rows] == [(10, 10), (3, 0)]
        events = await storage.pool.fetch("SELECT trade_id,event_type FROM trade_events ORDER BY id")
        assert [r["event_type"] for r in events] == ["BUY", "SELL", "BUY"]
        assert events[0]["trade_id"] == events[1]["trade_id"] == rows[0]["id"]
        assert events[2]["trade_id"] == rows[1]["id"] != rows[0]["id"]
        assert len(acks) == 3 and not failures and not sched._pending_fill_handoffs
    finally:
        await storage.disconnect()
