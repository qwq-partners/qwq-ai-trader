"""지연된 장부 복구와 독립적인 실제 포트폴리오·보호 후처리 진행."""
import asyncio
from decimal import Decimal

import pytest

from src.data.storage.execution_journal import ExecutionWriteReceipt
from test_journal_commit_handoff import _unattributed, enqueue, identified, setup


@pytest.mark.parametrize("wait", [False, True])
def test_stalled_receipt_does_not_block_other_symbol_protection(monkeypatch, tmp_path, wait):
    sched, bot, receipts, acks, failures = setup(monkeypatch, tmp_path)
    bot.trade_journal.status = "unknown"

    async def run():
        entered, release = asyncio.Event(), asyncio.Event()
        calls = []

        async def resolve(scope, execution_id):
            calls.append(execution_id)
            entered.set()
            await release.wait()
            raise OSError("synthetic database outage")

        bot.trade_journal.resolve_execution_receipt = resolve
        first, second = identified(), identified(execution="s2:o2:3")
        second.symbol, second.order_id = "000660", "o2"
        await enqueue(sched, bot, first)
        await enqueue(sched, bot, second)
        drain = asyncio.create_task(sched._drain_fill_handoffs(wait=wait))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            await asyncio.wait_for(asyncio.shield(drain), 0.1)
            assert bot.exit_manager.get_state("000660").remaining_quantity == 3
            assert bot.engine.portfolio.positions["000660"].quantity == 3
            assert bot.exit_manager.update_price("000660", second.price * Decimal("0.8"))[0] == "sell_all"
            await sched._drain_fill_handoffs(wait=False)
            assert calls.count(first.execution_id) == 1
            assert len(bot.trade_journal.entries) == 2 and not acks and not failures
        finally:
            drain.cancel()
            await asyncio.gather(drain, return_exceptions=True)
            release.set()
            cleanup = getattr(sched, "_cancel_journal_resolvers", None)
            if cleanup:
                await cleanup()

    asyncio.run(run())


async def settle(sched):
    tasks = list(getattr(sched, "_journal_resolver_tasks", ()))
    if tasks:
        await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 1)


@pytest.mark.parametrize("mode", ["timeout", "exception", "cancel"])
def test_resolver_failure_exhausts_two_attempts_without_repeating_protection(monkeypatch, tmp_path, mode):
    sched, bot, receipts, acks, failures = setup(monkeypatch, tmp_path)
    bot.trade_journal.status = "unknown"
    sched.JOURNAL_RESOLVE_TIMEOUT_SEC = 0.01
    calls, canceled = [], []
    bot.broker.mark_unattributed_execution = lambda *args: None

    async def resolve(scope, execution_id):
        calls.append(execution_id)
        if mode == "exception":
            raise OSError("synthetic failure")
        if mode == "cancel":
            raise asyncio.CancelledError
        try:
            await asyncio.Event().wait()
        finally:
            canceled.append(execution_id)

    bot.trade_journal.resolve_execution_receipt = resolve

    async def run():
        fill = identified()
        await enqueue(sched, bot, fill)
        for _ in range(3):
            await sched._drain_fill_handoffs(wait=False)
            await settle(sched)
        await sched._drain_fill_handoffs(wait=False)
        assert calls == [fill.execution_id, fill.execution_id]
        assert len(bot.trade_journal.entries) == 1
        assert bot.exit_manager.get_state(fill.symbol).remaining_quantity == 3
        assert acks == [(fill.order_id, 3)] and not failures
        assert len(_unattributed(tmp_path)) == 1
        assert not sched._pending_fill_handoffs and not sched._journal_resolver_tasks
        if mode == "timeout":
            assert canceled == [fill.execution_id, fill.execution_id]

    asyncio.run(run())


@pytest.mark.parametrize("cancel_loop", [False, True])
def test_fill_loop_shutdown_cancels_and_reaps_outstanding_resolver(monkeypatch, tmp_path, cancel_loop):
    sched, bot, receipts, acks, failures = setup(monkeypatch, tmp_path)
    bot.trade_journal.status = "unknown"

    async def run():
        entered, stopped, tick = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def resolve(*args):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()

        async def sleep(_):
            await entered.wait()
            tick.set()
            if cancel_loop:
                await asyncio.Event().wait()
            bot.running = False

        bot.trade_journal.resolve_execution_receipt = resolve
        monkeypatch.setattr(asyncio, "sleep", sleep)
        await enqueue(sched, bot, identified())
        bot.running = True
        loop = asyncio.create_task(sched.run_fill_check())
        await asyncio.wait_for(tick.wait(), 1)
        if cancel_loop:
            loop.cancel()
        await asyncio.wait_for(loop, 1)
        assert stopped.is_set() and not sched._journal_resolver_tasks
        assert len(sched._pending_fill_handoffs) == 1
        assert not acks and not failures and _unattributed(tmp_path) == []
        handoff, = sched._pending_fill_handoffs.values()
        assert handoff["postprocessing_done"] and "journal_resolver_task" not in handoff

    asyncio.run(run())


def test_memory_commit_cancels_late_lookup_without_duplicate_ack(monkeypatch, tmp_path):
    sched, bot, receipts, acks, failures = setup(monkeypatch, tmp_path)
    bot.trade_journal.status = "unknown"

    async def run():
        entered, canceled = asyncio.Event(), asyncio.Event()

        async def resolve(*args):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                canceled.set()

        bot.trade_journal.resolve_execution_receipt = resolve
        fill = identified()
        await enqueue(sched, bot, fill)
        await sched._drain_fill_handoffs(wait=False)
        await asyncio.wait_for(entered.wait(), 1)
        bot.trade_journal.status = "committed"
        await sched._drain_fill_handoffs(wait=False)
        await settle(sched)
        await sched._drain_fill_handoffs(wait=False)
        assert canceled.is_set() and acks == [(fill.order_id, 3)]
        assert not sched._journal_resolver_tasks and not failures
        assert len(bot.trade_journal.entries) == 1 and _unattributed(tmp_path) == []

    asyncio.run(run())


def test_resolver_return_alone_cannot_fabricate_commit(monkeypatch, tmp_path):
    sched, bot, receipts, acks, failures = setup(monkeypatch, tmp_path)
    bot.trade_journal.status = "unknown"
    bot.broker.mark_unattributed_execution = lambda *args: None

    async def resolve(scope, execution_id):
        return ExecutionWriteReceipt("committed", execution_id, "synthetic")

    bot.trade_journal.resolve_execution_receipt = resolve

    async def run():
        await enqueue(sched, bot, identified())
        for _ in range(4):
            await sched._drain_fill_handoffs(wait=False)
            await settle(sched)
            assert not acks
        await sched._drain_fill_handoffs(wait=False)
        assert len(_unattributed(tmp_path)) == 1 and len(acks) == 1

    asyncio.run(run())


def test_stalled_receipt_allows_next_broker_fill_and_portfolio_application(monkeypatch, tmp_path):
    sched, bot, receipts, acks, failures = setup(monkeypatch, tmp_path)
    bot.trade_journal.status = "unknown"

    async def run():
        entered = asyncio.Event()

        async def resolve(*args):
            entered.set()
            await asyncio.Event().wait()

        async def sleep(_):
            bot.running = False

        original_emit = bot.engine.emit

        async def emit_and_process(event):
            from test_fill_reconciliation import process
            await original_emit(event)
            await process(bot.engine)

        bot.trade_journal.resolve_execution_receipt = resolve
        await enqueue(sched, bot, identified())
        await sched._drain_fill_handoffs(wait=False)
        await asyncio.wait_for(entered.wait(), 1)
        second = identified(execution="s2:o2:3")
        second.symbol, second.order_id = "000660", "o2"
        bot.broker.fills_seq = [[second]]
        bot.engine.emit = emit_and_process
        monkeypatch.setattr(asyncio, "sleep", sleep)
        bot.running = True
        await asyncio.wait_for(sched.run_fill_check(), 1)
        assert bot.broker.fills_seq == []
        assert bot.engine.portfolio.positions[second.symbol].quantity == 3
        assert bot.exit_manager.get_state(second.symbol).remaining_quantity == 3
        assert len(bot.trade_journal.entries) == 2 and not failures and not acks
        assert not sched._journal_resolver_tasks

    asyncio.run(run())


@pytest.mark.parametrize("verified, fence_changed", [(True, False), (False, False), (True, True)])
def test_cash_confirmation_only_after_verified_cash_passes_final_fence(monkeypatch, verified, fence_changed):
    from test_sync_portfolio_characterization import _make, _pos
    sched, bot, _ = _make(
        monkeypatch, bot_positions=[_pos("005930")],
        balance={"stock_value": 105000, "available_cash": 0, "available_cash_verified": verified},
        kis_seq=[{"005930": _pos("005930")}], cash="123456")
    confirmed = []

    def confirm(cash):
        confirmed.append((cash, bot.engine.portfolio.cash))

    bot._confirm_kr_cash = confirm
    if fence_changed:
        get_positions = bot.broker.get_positions

        async def changed():
            positions = await get_positions()
            bot.engine._position_update_generation += 1
            return positions

        bot.broker.get_positions = changed
    asyncio.run(sched._sync_portfolio())
    assert confirmed == ([(Decimal("0"), Decimal("0"))] if verified and not fence_changed else [])


def test_identified_sell_never_falls_back_to_sync_trade_recovery(monkeypatch, tmp_path):
    from src.core.types import OrderSide
    sched, bot, receipts, acks, failures = setup(monkeypatch, tmp_path, holdings=10)
    bot.engine.portfolio.positions["005930"].trade_id = "T-old"
    sync_calls = []

    def sync_recover(trade_id):
        sync_calls.append(trade_id)
        raise AssertionError("식별 SELL 경로에 동기 복구 금지")

    bot.trade_journal.recover_trade = sync_recover

    async def run():
        fill = identified(OrderSide.SELL, 3)
        await enqueue(sched, bot, fill)
        await sched._drain_fill_handoffs(wait=False)
        assert sync_calls == []
        assert len(_unattributed(tmp_path)) == 1
        assert bot.exit_manager.get_state(fill.symbol).remaining_quantity == 7
        assert acks == [(fill.order_id, 3)] and not failures

    asyncio.run(run())
