"""Canceled-order execution recovery: real broker, synthetic transport only."""
import asyncio
from datetime import datetime, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from src.core.types import Order, OrderSide, OrderType
from src.execution.broker import kis_kr


@pytest.fixture
def broker(monkeypatch, tmp_path):
    monkeypatch.setattr(kis_kr.order_unknown, "default_path", lambda: tmp_path / "unknown.json")
    monkeypatch.setattr(kis_kr.kill_switch, "check", lambda *a, **k: (True, ""))
    monkeypatch.setattr(kis_kr.audit_log, "record", lambda *a, **k: None)
    monkeypatch.setattr(kis_kr.KISBroker, "is_connected", property(lambda self: True))
    from src.analytics import tca
    monkeypatch.setattr(tca, "_CACHE_DIR", tmp_path)
    b = kis_kr.KISBroker(kis_kr.KISConfig(
        app_key="synthetic", app_secret="synthetic", account_no="synthetic"))
    b._get_current_market_session = lambda: "regular"
    async def hashkey(params):
        return "synthetic"
    async def post(url, tr_id, params, **kwargs):
        return {"rt_cd": "0", "output": {"ODNO": "001", "KRX_FWDG_ORD_ORGNO": "009"}}
    b._get_hashkey, b._api_post = hashkey, post
    return b


async def accepted(b, side=OrderSide.BUY):
    order = Order(symbol="005930", side=side, quantity=10,
                  price=Decimal("10000"), order_type=OrderType.LIMIT,
                  strategy="sepa", reason="original", signal_score=80)
    assert (await b.submit_order(order))[0]
    return order


def row(qty=3, price="10000", **updates):
    result = {"ord_dt": datetime.now(ZoneInfo("Asia/Seoul")).strftime("%Y%m%d"), "odno": "001",
              "orgn_odno": "", "pdno": "005930", "sll_buy_dvsn_cd": "02",
              "ord_qty": "10", "tot_ccld_qty": str(qty), "avg_prvs": price,
              "cncl_yn": "Y", "cnc_cfrm_qty": str(10-qty), "rmn_qty": "0", "rjct_qty": "0"}
    result.update(updates)
    return result


def response(b, rows, complete=True):
    requests = []
    async def get(url, tr_id, params, **kwargs):
        requests.append(dict(params))
        return {"rt_cd": "0", "output1": rows, "_tr_cont": "D" if complete else "F"}
    b._api_get = get
    return requests


@pytest.mark.parametrize("side", [OrderSide.BUY, OrderSide.SELL])
def test_canceled_late_fill_is_observed_once_and_waits_for_receipt(broker, side):
    async def run():
        order = await accepted(broker, side)
        assert await broker.cancel_order(order.id)
        assert await broker.get_open_orders() == []
        assert broker.has_pending_fill_observations()
        assert broker.has_unresolved_cancel(order.symbol)
        assert broker.get_fill_observation_order_ids() == {order.id}
        assert broker.reconciliation_token() is None
        sent = response(broker, [row(sll_buy_dvsn_cd="01" if side == OrderSide.SELL else "02")])
        fills = await broker.check_fills()
        assert [(f.order_id, f.quantity, f.price, f.strategy) for f in fills] == [
            (order.id, 3, Decimal("10000"), "sepa")]
        assert sent[0]["CCLD_DVSN"] == "00"
        assert await broker.check_fills() == []
        assert broker.reconciliation_token() is None
        broker.acknowledge_fill(order.id, 3)
        assert not broker.has_pending_fill_observations()
        assert type(broker.reconciliation_token()) is int
    asyncio.run(run())


def test_partial_before_cancel_preserves_cumulative_quantity_and_price(broker):
    async def run():
        order = await accepted(broker)
        response(broker, [row(1, "10000", cncl_yn="N", cnc_cfrm_qty="0", rmn_qty="9")])
        assert [f.quantity for f in await broker.check_fills()] == [1]
        broker.acknowledge_fill(order.id, 1)
        await broker.cancel_order(order.id)
        response(broker, [row(3, "12000")])
        fills = await broker.check_fills()
        assert [(f.quantity, f.price) for f in fills] == [(2, Decimal("13000"))]
        broker.acknowledge_fill(order.id, 2)
        assert not broker.has_pending_fill_observations()
    asyncio.run(run())


def test_zero_fill_terminal_retires_without_synthetic_fill(broker):
    async def run():
        order = await accepted(broker)
        await broker.cancel_order(order.id)
        response(broker, [row(0, "0")])
        assert await broker.check_fills() == []
        assert not broker.has_pending_fill_observations()
    asyncio.run(run())


@pytest.mark.parametrize("updates", [
    {"ord_dt": "19990101"}, {"odno": "002", "orgn_odno": "001"},
    {"pdno": "000660"}, {"sll_buy_dvsn_cd": "01"}, {"ord_qty": "11"},
    {"rjct_qty": "1"}, {"rmn_qty": "1"}, {"cncl_yn": "N"},
    {"cnc_cfrm_qty": "6"}, {"cnc_cfrm_qty": None}, {"rjct_qty": ""},
    {"tot_ccld_qty": "-1"}, {"tot_ccld_qty": "3.0"}, {"avg_prvs": "NaN"},
])
def test_unverified_rows_never_release_observation(broker, updates):
    async def run():
        order = await accepted(broker)
        await broker.cancel_order(order.id)
        response(broker, [row(**updates)])
        fills = await broker.check_fills()
        for fill in fills:
            broker.acknowledge_fill(fill.order_id, fill.quantity)
        assert broker.has_unresolved_cancel(order.symbol)
        assert broker.reconciliation_token() is None
    asyncio.run(run())


@pytest.mark.parametrize("case", ["incomplete", "conflicting", "empty"])
def test_failed_or_ambiguous_query_does_not_consume_delta(broker, case):
    async def run():
        order = await accepted(broker)
        await broker.cancel_order(order.id)
        rows = [] if case == "empty" else [row()]
        if case == "conflicting":
            rows.append(row(4))
        response(broker, rows, complete=case != "incomplete")
        assert await broker.check_fills() == []
        assert broker.has_pending_fill_observations()
        response(broker, [row(), row()])
        assert [f.quantity for f in await broker.check_fills()] == [3]
    asyncio.run(run())


def test_modified_order_cannot_be_declared_terminal(broker):
    async def run():
        order = await accepted(broker)
        assert await broker.modify_order(order.id, new_price=Decimal("11000"))
        assert await broker.cancel_order(order.id)
        response(broker, [row(0, "0")])
        assert await broker.check_fills() == []
        assert broker.has_pending_fill_observations()
    asyncio.run(run())


def test_cancel_during_fill_query_keeps_late_quantity(broker):
    async def run():
        order = await accepted(broker)
        entered, release = asyncio.Event(), asyncio.Event()
        async def get(url, tr_id, params, **kwargs):
            entered.set()
            await release.wait()
            return {"rt_cd": "0", "_tr_cont": "D", "output1": [row()]}
        broker._api_get = get
        checking = asyncio.create_task(broker.check_fills())
        await entered.wait()
        await broker.cancel_order(order.id)
        release.set()
        first = await checking
        response(broker, [row()])
        second = await broker.check_fills()
        assert sum(f.quantity for f in first + second) == 3
        assert broker.has_pending_fill_observations()
        broker.acknowledge_fill(order.id, 3)
        assert not broker.has_pending_fill_observations()
    asyncio.run(run())


def test_full_fill_during_cancel_does_not_resurrect_order(broker):
    async def run():
        order = await accepted(broker)
        entered, release = asyncio.Event(), asyncio.Event()
        async def post(*args, **kwargs):
            entered.set()
            await release.wait()
            return {"rt_cd": "0"}
        broker._api_post = post
        cancelling = asyncio.create_task(broker.cancel_order(order.id))
        await entered.wait()
        response(broker, [row(10, cncl_yn="N")])
        assert [f.quantity for f in await broker.check_fills()] == [10]
        release.set()
        assert await cancelling
        assert await broker.get_open_orders() == []
        assert not broker.has_pending_fill_observations()
    asyncio.run(run())


def test_previously_delivered_partial_requires_its_own_acknowledgement(broker):
    async def run():
        order = await accepted(broker)
        response(broker, [row(1, cncl_yn="N", cnc_cfrm_qty="0", rmn_qty="9")])
        assert [f.quantity for f in await broker.check_fills()] == [1]
        await broker.cancel_order(order.id)
        response(broker, [row(3)])
        assert [f.quantity for f in await broker.check_fills()] == [2]
        broker.acknowledge_fill(order.id, 2)
        assert broker.has_pending_fill_observations()
        broker.acknowledge_fill(order.id, 99)
        assert broker.has_pending_fill_observations()
        broker.acknowledge_fill(order.id, 1)
        assert not broker.has_pending_fill_observations()
    asyncio.run(run())


def test_overlap_canceled_queries_emit_one_delta(broker):
    async def run():
        order = await accepted(broker)
        await broker.cancel_order(order.id)
        both, release = asyncio.Event(), asyncio.Event()
        calls = 0
        async def get(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                both.set()
            await release.wait()
            return {"rt_cd": "0", "_tr_cont": "D", "output1": [row()]}
        broker._api_get = get
        tasks = [asyncio.create_task(broker.check_fills()) for _ in range(2)]
        await both.wait()
        release.set()
        results = await asyncio.gather(*tasks)
        assert sum(fill.quantity for result in results for fill in result) == 3
        broker.acknowledge_fill(order.id, 3)
        assert not broker.has_pending_fill_observations()
    asyncio.run(run())


def test_post_ack_query_is_required_even_when_prior_query_has_terminal_row(broker):
    async def run():
        order = await accepted(broker)
        entered, release = asyncio.Event(), asyncio.Event()
        queries = []
        async def get(url, tr_id, params, **kwargs):
            queries.append(dict(params))
            if len(queries) == 1:
                entered.set()
                await release.wait()
                return {"rt_cd": "0", "_tr_cont": "D", "output1": [row(0, "0")]}
            return {"rt_cd": "1", "output1": []}
        broker._api_get = get
        checking = asyncio.create_task(broker.check_fills())
        await entered.wait()
        await broker.cancel_order(order.id)
        release.set()
        assert await checking == []
        assert broker.has_pending_fill_observations()
        assert len(queries) == 2
        response(broker, [row(0, "0")])
        assert await broker.check_fills() == []
        assert not broker.has_pending_fill_observations()
    asyncio.run(run())


def test_submission_day_survives_rollover_and_stale_order_creation_date(broker, monkeypatch):
    class Clock(datetime):
        current = datetime(2026, 10, 2, 14)
        @classmethod
        def now(cls, tz=None):
            return cls.current
    monkeypatch.setattr(kis_kr, "datetime", Clock)
    async def run():
        order = await accepted(broker)
        order.created_at = datetime(2026, 9, 30)  # Creation is not submission evidence.
        await broker.cancel_order(order.id)
        Clock.current = datetime(2026, 10, 3, 9)
        requests = response(broker, [row(0, "0", ord_dt="20261002")])
        assert await broker.check_fills() == []
        assert requests[0]["INQR_STRT_DT"] == "20261002"
        assert requests[0]["INQR_END_DT"] == "20261002"
        assert not broker.has_pending_fill_observations()
    asyncio.run(run())


@pytest.mark.parametrize("newrow", [row(2), row(3, "9999"), row(4, "1")])
def test_cumulative_regression_or_impossible_incremental_price_is_not_fabricated(broker, newrow):
    async def run():
        order = await accepted(broker)
        await broker.cancel_order(order.id)
        response(broker, [row(3, cncl_yn="N", cnc_cfrm_qty="0", rmn_qty="7")])
        assert [f.quantity for f in await broker.check_fills()] == [3]
        broker.acknowledge_fill(order.id, 3)
        response(broker, [newrow])
        assert await broker.check_fills() == []
        assert broker.has_pending_fill_observations()
        assert order.filled_quantity == 3
        assert order.filled_price == Decimal("10000")
    asyncio.run(run())


def test_ambiguous_modification_and_cancel_failure_preserve_observation(broker):
    async def run():
        order = await accepted(broker)
        async def unknown(*args, **kwargs):
            return {"_unknown": True, "msg1": "synthetic lost response"}
        broker._api_post = unknown
        assert not await broker.modify_order(order.id, new_price=Decimal("11000"))
        assert not await broker.cancel_order(order.id)
        assert await broker.get_open_orders() == [order]
        async def ok(*args, **kwargs):
            return {"rt_cd": "0"}
        broker._api_post = ok
        assert await broker.cancel_order(order.id)
        response(broker, [row(0, "0")])
        assert await broker.check_fills() == []
        assert broker.has_pending_fill_observations()
    asyncio.run(run())


def test_explicitly_rejected_modification_does_not_prevent_terminal_evidence(broker):
    async def run():
        order = await accepted(broker)
        async def rejected(*args, **kwargs):
            return {"rt_cd": "1", "msg_cd": "REJECTED", "msg1": "synthetic rejection"}
        broker._api_post = rejected
        assert not await broker.modify_order(order.id, new_price=Decimal("11000"))
        async def ok(*args, **kwargs):
            return {"rt_cd": "0"}
        broker._api_post = ok
        assert await broker.cancel_order(order.id)
        response(broker, [row(0, "0")])
        assert await broker.check_fills() == []
        assert not broker.has_pending_fill_observations()
    asyncio.run(run())


def test_submission_identity_uses_korean_date_on_utc_host(broker, monkeypatch):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            instant = datetime(2026, 10, 2, 16, tzinfo=timezone.utc)
            return instant.astimezone(tz) if tz else instant.replace(tzinfo=None)
    monkeypatch.setattr(kis_kr, "datetime", Clock)
    async def run():
        order = await accepted(broker)
        await broker.cancel_order(order.id)
        requests = response(broker, [row(0, "0", ord_dt="20261003")])
        assert await broker.check_fills() == []
        assert requests[0]["INQR_STRT_DT"] == "20261003"
        assert not broker.has_pending_fill_observations()
    asyncio.run(run())


def test_cancel_terminal_cannot_retire_during_inflight_modification(broker):
    async def run():
        order = await accepted(broker)
        entered, release = asyncio.Event(), asyncio.Event()
        async def post(url, tr_id, params, **kwargs):
            if params['RVSE_CNCL_DVSN_CD'] == '01':
                entered.set()
                await release.wait()
                return {"rt_cd": "1", "msg_cd": "REJECTED"}
            return {"rt_cd": "0"}
        broker._api_post = post
        modification = asyncio.create_task(broker.modify_order(order.id, new_price=Decimal('11000')))
        await entered.wait()
        assert await broker.cancel_order(order.id)
        response(broker, [row(0, "0")])
        assert await broker.check_fills() == []
        assert broker.has_pending_fill_observations()
        release.set()
        assert not await modification
        assert await broker.check_fills() == []
        assert not broker.has_pending_fill_observations()
    asyncio.run(run())
