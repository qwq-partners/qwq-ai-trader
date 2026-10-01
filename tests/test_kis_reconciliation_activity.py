"""Local broker activity barriers; synthetic transport only, no exchange guarantees."""
import asyncio
import time
from decimal import Decimal

import pytest

from src.core.types import Order, OrderSide, OrderType
from src.execution.broker import kis_kr


@pytest.fixture
def broker(monkeypatch, tmp_path):
    monkeypatch.setattr(kis_kr.order_unknown, "default_path", lambda: tmp_path / "unknown.json")
    monkeypatch.setattr(kis_kr.kill_switch, "check", lambda *a, **k: (True, ""))
    monkeypatch.setattr(kis_kr.audit_log, "record", lambda *a, **k: None)
    monkeypatch.setattr(kis_kr.audit_log, "record_blocked", lambda *a, **k: None)
    monkeypatch.setattr(kis_kr.KISBroker, "is_connected", property(lambda self: True))
    b = kis_kr.KISBroker(kis_kr.KISConfig(
        app_key="synthetic", app_secret="synthetic", account_no="synthetic"))
    b._get_current_market_session = lambda: "regular"
    return b


def order():
    return Order(symbol="005930", side=OrderSide.SELL, quantity=3,
                 price=Decimal("10000"), order_type=OrderType.LIMIT)


@pytest.mark.parametrize("operation", ["submit", "cancel", "modify", "fills"])
def test_inflight_operations_hide_token_and_completed_cycle_changes_it(broker, operation):
    async def run():
        before = broker.reconciliation_token()
        assert type(before) is int
        entered, release = asyncio.Event(), asyncio.Event()
        current = order()
        if operation in {"cancel", "modify"}:
            broker._pending_orders[current.id] = current
            broker._order_id_to_kis_no[current.id] = "synthetic-order"

        async def wait(*args, **kwargs):
            entered.set()
            await release.wait()
            return [] if operation == "fills" else None

        broker._get_hashkey = wait
        broker._query_daily_fills = wait
        calls = {"submit": lambda: broker.submit_order(current),
                 "cancel": lambda: broker.cancel_order(current.id),
                 "modify": lambda: broker.modify_order(current.id, new_quantity=2),
                 "fills": broker.check_fills}
        task = asyncio.create_task(calls[operation]())
        await entered.wait()
        # Pending can disappear concurrently; the operation itself must stay visible.
        broker._pending_orders.clear()
        assert broker.reconciliation_token() is None
        release.set()
        await task
        assert type(broker.reconciliation_token()) is int
        assert broker.reconciliation_token() > before
    asyncio.run(run())


def test_pending_orders_hide_token_without_changing_order_tracking(broker):
    current = order()
    broker._pending_orders[current.id] = current
    assert broker.reconciliation_token() is None
    assert broker._pending_orders[current.id] is current


def test_uninitialized_adapter_never_claims_idle():
    broker = object.__new__(kis_kr.KISBroker)
    broker._pending_orders = {}
    assert broker.reconciliation_token() is None


@pytest.mark.parametrize("outcome", ["rejection", "exception", "cancellation"])
def test_fill_query_outcomes_release_activity_and_invalidate_snapshot(broker, outcome):
    async def run():
        before = broker.reconciliation_token()
        entered, release = asyncio.Event(), asyncio.Event()
        async def query():
            entered.set()
            await release.wait()
            if outcome == "exception":
                raise RuntimeError("synthetic query failure")
            return []
        broker._query_daily_fills = query
        task = asyncio.create_task(broker.check_fills())
        await entered.wait()
        assert broker.reconciliation_token() is None
        if outcome == "cancellation":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            release.set()
            assert await task == []
        assert broker.reconciliation_token() > before
    asyncio.run(run())


def test_nested_and_overlapping_operations_remain_busy_until_all_complete(broker):
    async def run():
        before = broker.reconciliation_token()
        entered, release = asyncio.Event(), asyncio.Event()
        async def query():
            await broker.cancel_order("missing")
            entered.set()
            await release.wait()
            return []
        broker._query_daily_fills = query
        task = asyncio.create_task(broker.check_fills())
        await entered.wait()
        await broker.modify_order("missing")
        assert broker.reconciliation_token() is None
        release.set()
        await task
        assert broker.reconciliation_token() > before
    asyncio.run(run())


@pytest.mark.parametrize("response", [
    {"rt_cd": "1", "msg1": "synthetic failure"},
    {"rt_cd": "0", "output2": []},
    RuntimeError("synthetic transport failure"),
])
def test_new_balance_attempt_discards_old_positions_even_on_failure(broker, response):
    async def run():
        broker._balance_snapshot = (time.monotonic(), [{"pdno": "OLD"}])
        async def get(*args, **kwargs):
            # Old positions must be invalidated before the first network await.
            assert broker._balance_snapshot is None
            if isinstance(response, Exception):
                raise response
            return response
        broker._api_get = get
        assert await broker.get_account_balance() == {}
        assert broker._balance_snapshot is None
    asyncio.run(run())


def test_paginated_balance_never_reuses_previous_single_page_snapshot(broker):
    async def run():
        broker._balance_snapshot = (time.monotonic(), [{"pdno": "OLD"}])
        async def get(url, tr_id, params, **kwargs):
            assert broker._balance_snapshot is None
            if tr_id == "TTTC8908R":
                return {"rt_cd": "0", "output": {"nrcvb_buy_amt": "0"}}
            return {"rt_cd": "0", "_tr_cont": "F", "ctx_area_fk100": "next",
                    "output1": [], "output2": [{"dnca_tot_amt": "10"}]}
        broker._api_get = get
        assert (await broker.get_account_balance())["available_cash"] == 0
        assert broker._balance_snapshot is None
    asyncio.run(run())
