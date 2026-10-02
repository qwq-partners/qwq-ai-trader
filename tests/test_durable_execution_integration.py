"""실제 임시 원장 + 합성 KIS: 재시작 시 과거 체결은 재생하지 않는다."""
import asyncio
from decimal import Decimal

import pytest

from src.core.types import Order, OrderSide, OrderType
from src.execution.broker import kis_kr
from test_kis_cancel_fill_recovery import broker, accepted, response, row
from test_order_post_unknown import ub


def order(side=OrderSide.BUY, partial=False):
    return Order(symbol='005930', side=side, quantity=10, price=Decimal('10000'),
                 order_type=OrderType.LIMIT, partial_exit=partial)


def restart(b):
    fresh = kis_kr.KISBroker(b.config)
    fresh._get_current_market_session = b._get_current_market_session
    fresh._get_hashkey, fresh._api_post = b._get_hashkey, b._api_post
    return fresh


def test_intent_accept_identity_survives_restart_without_replay(broker):
    async def run():
        original = await accepted(broker)
        assert hasattr(broker, 'execution_recovery_status')
        state = broker.execution_recovery_status()
        assert state['status'] == 'ready'
        records = list(state['orders'].values())
        assert records[0]['facts']['local_id'] == original.id
        assert records[0]['odno'] == '001'
        fresh = restart(broker)
        await fresh.initialize_execution_history()
        assert fresh.execution_recovery_status()['status'] == 'recovery_required'
        assert fresh.unknown_buy_hold()
        assert fresh.has_unknown_sell('005930')
        assert await fresh.get_open_orders() == []
        response(fresh, [row(10, cncl_yn='N', cnc_cfrm_qty='0', rmn_qty='0')])
        assert await fresh.check_fills() == []
    asyncio.run(run())


def test_full_fill_records_distinct_receipts_and_clean_restart(broker):
    async def run():
        await accepted(broker)
        response(broker, [row(10, cncl_yn='N', cnc_cfrm_qty='0', rmn_qty='0')])
        fills = await broker.check_fills()
        assert len(fills) == 1
        assert getattr(fills[0], 'execution_id', '')
        await broker.record_execution_receipt(fills[0], 'portfolio_applied')
        await broker.record_execution_receipt(fills[0], 'handoff_returned')
        # Repeated receipt does not add a second durable execution.
        await broker.record_execution_receipt(fills[0], 'handoff_returned')
        broker.acknowledge_fill(fills[0].order_id, fills[0].quantity)
        records = list(broker.execution_recovery_status()['orders'].values())
        assert len(records[0]['executions']) == 1
        assert records[0]['executions'][0]['handoff_returned'] is True
        await broker.disconnect()
        fresh = restart(broker)
        await fresh.initialize_execution_history()
        assert fresh.execution_recovery_status()['status'] == 'ready'
        assert fresh.unknown_buy_hold() is None
    asyncio.run(run())


@pytest.mark.parametrize('stage', [None, 'portfolio_applied', 'handoff_returned'])
def test_abrupt_end_is_not_cleared_by_fully_observed_or_applied_fill(broker, stage):
    async def run():
        await accepted(broker)
        response(broker, [row(10, cncl_yn='N', cnc_cfrm_qty='0', rmn_qty='0')])
        f, = await broker.check_fills()
        assert getattr(f, 'execution_id', '')
        if stage:
            await broker.record_execution_receipt(f, 'portfolio_applied')
        if stage == 'handoff_returned':
            await broker.record_execution_receipt(f, 'handoff_returned')
        fresh = restart(broker)
        await fresh.initialize_execution_history()
        assert fresh.unknown_buy_hold()
        assert fresh.execution_recovery_status()['prior_unclean'] is True
    asyncio.run(run())


def test_fault_blocks_buy_partial_but_preserves_full_protective_sell(broker):
    async def run():
        assert hasattr(broker, 'initialize_execution_history')
        await broker.initialize_execution_history()
        broker._execution_history.fail('synthetic disk failure')
        calls = []
        previous = broker._api_post
        async def post(*args, **kwargs):
            calls.append(1)
            return await previous(*args, **kwargs)
        broker._api_post = post
        assert not (await broker.submit_order(order()))[0]
        assert not (await broker.submit_order(order(OrderSide.SELL, True)))[0]
        assert calls == []
        assert (await broker.submit_order(order(OrderSide.SELL)))[0]
        assert calls == [1]
        assert broker.execution_recovery_status()['status'] == 'storage_fault'
        await broker.disconnect()
        fresh = restart(broker)
        await fresh.initialize_execution_history()
        assert fresh.unknown_buy_hold()
    asyncio.run(run())


def test_rejected_order_closes_intent_and_allows_clean_restart(broker):
    async def run():
        async def reject(*args, **kwargs):
            return {'rt_cd': '1', 'msg_cd': 'SYNTHETIC', 'msg1': '거절'}
        broker._api_post = reject
        assert not (await broker.submit_order(order()))[0]
        assert hasattr(broker, 'execution_recovery_status')
        record, = broker.execution_recovery_status()['orders'].values()
        assert record['status'] == 'rejected'
        await broker.disconnect()
        fresh = restart(broker)
        await fresh.initialize_execution_history()
        assert fresh.unknown_buy_hold() is None
    asyncio.run(run())


def test_unknown_response_remains_unresolved_after_disconnect(broker):
    async def run():
        async def unknown(*args, **kwargs):
            return {'_unknown': True, 'msg1': '응답 유실'}
        broker._api_post = unknown
        assert not (await broker.submit_order(order()))[0]
        assert hasattr(broker, 'execution_recovery_status')
        record, = broker.execution_recovery_status()['orders'].values()
        assert record['status'] == 'unknown'
        await broker.disconnect()
        fresh = restart(broker)
        await fresh.initialize_execution_history()
        assert fresh.execution_recovery_status()['status'] == 'recovery_required'
    asyncio.run(run())


def test_cancel_zero_terminal_allows_clean_close(broker):
    async def run():
        o = await accepted(broker)
        assert await broker.cancel_order(o.id)
        response(broker, [row(0, '0')])
        assert await broker.check_fills() == []
        assert hasattr(broker, 'execution_recovery_status')
        record, = broker.execution_recovery_status()['orders'].values()
        assert record['terminal_quantity'] == 0
        await broker.disconnect()
        fresh = restart(broker)
        await fresh.initialize_execution_history()
        assert fresh.execution_recovery_status()['status'] == 'ready'
    asyncio.run(run())


def test_same_execution_in_different_events_applies_portfolio_only_once(monkeypatch, tmp_path):
    from test_fill_reconciliation import case
    from src.core.event import FillEvent
    from src.core.types import Fill
    async def run():
        _, bot = case(monkeypatch, tmp_path, holdings=10)
        f = Fill(order_id='local', symbol='005930', side=OrderSide.BUY,
                 quantity=3, price=Decimal('10000'))
        # Set explicitly so RED is the double-count assertion on the existing engine.
        f.execution_id = 'session:local:3'
        first, duplicate = FillEvent.from_fill(f), FillEvent.from_fill(f)
        await bot.engine.risk_manager.on_fill(first)
        after = bot.engine.portfolio.cash
        await bot.engine.risk_manager.on_fill(duplicate)
        assert bot.engine.portfolio.positions['005930'].quantity == 13
        assert bot.engine.portfolio.cash == after
    asyncio.run(run())


def test_shutdown_fences_new_full_sell_even_if_history_close_is_waiting(broker):
    async def run():
        await broker.initialize_execution_history()
        entered, release = asyncio.Event(), asyncio.Event()
        original = broker._execution_history.close
        async def paused_close():
            entered.set()
            await release.wait()
            return await original()
        broker._execution_history.close = paused_close
        closing = asyncio.create_task(broker.disconnect())
        await asyncio.wait_for(entered.wait(), 2)
        try:
            assert not (await broker.submit_order(order(OrderSide.SELL)))[0]
        finally:
            release.set()
            await closing
        assert not (await broker.submit_order(order(OrderSide.SELL)))[0]
        assert broker._pending_orders == {}
    asyncio.run(run())


def test_initial_open_failure_sends_full_protective_sell_but_blocks_buy_and_partial(broker):
    """2026-10-02 47차 리뷰 P1-2 로 35차 결정을 번복: 시작 기록 실패는 신규 위험(BUY·분할 SELL)만 막는다.

    35차는 "기록 없는 주문이 다음 시작에서 사라질 수 있다"는 이유로 전량 SELL 도 막았다. 번복 근거: 막힌 손절은
    상한 없는 손실이고, 미기록 전량 SELL 은 KIS 매도가능수량이 이중 매도를 막으며 체결은 잔고 동기화·기동 시
    거래소 미체결 대사로 반영되는 유한한 대사 공백이다. 전송은 ERROR 로그를 남기고 세션은 clean close 불가로 남는다.
    """
    async def run():
        async def broken(*args):
            raise OSError('synthetic open failure')
        broker._execution_history.ledger.open = broken
        calls = []
        async def post(*args, **kwargs):
            calls.append(kwargs.get('cancel_guard'))
            return {'rt_cd': '0', 'output': {'ODNO': '001'}}
        broker._api_post = post
        assert not (await broker.submit_order(order(OrderSide.BUY)))[0]
        partial = order(OrderSide.SELL)
        partial.partial_exit = True
        assert not (await broker.submit_order(partial))[0]
        assert calls == []
        assert (await broker.submit_order(order(OrderSide.SELL)))[0]
        assert len(calls) == 1 and calls[0][1] == OrderSide.SELL and calls[0][2] is False
        assert broker._execution_history.fault == 'open_failed'
        assert broker.execution_recovery_status()['status'] == 'storage_fault'
    asyncio.run(run())


def test_opened_session_fault_still_tracks_current_protective_fill(broker):
    async def run():
        await broker.initialize_execution_history()
        broker._execution_history.fail('synthetic disk failure')
        o = order(OrderSide.SELL)
        assert (await broker.submit_order(o))[0]
        response(broker, [row(10, sll_buy_dvsn_cd='01', cncl_yn='N', cnc_cfrm_qty='0', rmn_qty='0')])
        fills = await broker.check_fills()
        assert [f.quantity for f in fills] == [10]
        assert fills[0].execution_id == ''  # 기록된 체결이라고 표시하지 않는다.
        assert await broker.check_fills() == []
        assert broker.execution_recovery_status()['status'] == 'storage_fault'
        await broker.disconnect()
        fresh = restart(broker)
        await fresh.initialize_execution_history()
        assert fresh.unknown_buy_hold()
    asyncio.run(run())


def test_normal_same_cumulative_price_conflict_latches_recovery_hold(broker):
    async def run():
        await accepted(broker)
        response(broker, [row(3, cncl_yn='N', cnc_cfrm_qty='0', rmn_qty='7')])
        assert [f.quantity for f in await broker.check_fills()] == [3]
        response(broker, [row(3, '11000', cncl_yn='N', cnc_cfrm_qty='0', rmn_qty='7')])
        assert await broker.check_fills() == []
        assert broker.unknown_buy_hold()
    asyncio.run(run())


def test_real_scheduler_keeps_missing_journal_unconfirmed_after_protective_handler(monkeypatch, tmp_path, broker):
    from test_fill_reconciliation import case, process
    from test_cancel_fill_integration import pending
    from test_sync_portfolio_characterization import _fill_check_once
    sched, bot = case(monkeypatch, tmp_path, holdings=100)
    bot.broker = bot.engine.broker = broker
    o = asyncio.run(accepted(broker, OrderSide.SELL))
    pending(bot.engine.risk_manager, OrderSide.SELL, o.id, 10)
    response(broker, [row(10, sll_buy_dvsn_cd='01', cncl_yn='N', cnc_cfrm_qty='0', rmn_qty='0')])
    _fill_check_once(monkeypatch, sched, [])
    record, = broker.execution_recovery_status()['orders'].values()
    assert record['executions'][0]['portfolio_applied'] is False
    asyncio.run(process(bot.engine))
    _fill_check_once(monkeypatch, sched, [])
    record, = broker.execution_recovery_status()['orders'].values()
    assert record['executions'][0]['portfolio_applied'] is True
    assert record['executions'][0]['handoff_returned'] is False
    assert broker.execution_recovery_status()['status'] == 'storage_fault'
    assert broker.unknown_buy_hold()
    assert bot.engine.portfolio.positions['005930'].quantity == 90
    assert bot.exit_manager.get_state('005930').remaining_quantity == 90


@pytest.mark.parametrize('side,partial,expected', [(OrderSide.BUY, False, False),
                                                  (OrderSide.SELL, True, False),
                                                  (OrderSide.SELL, False, True)])
def test_recovery_gate_after_rate_limit_wait(ub, tmp_path, side, partial, expected):
    from src.execution.execution_history import ExecutionHistory
    async def run():
        ub._execution_history = ExecutionHistory(tmp_path / 'late.sqlite', 'synthetic')
        await ub.initialize_execution_history()
        async def rate_limit(*args):
            ub._execution_history.fail('synthetic late fault')
        ub._rate_limit = rate_limit
        result = await ub.submit_order(order(side, partial))
        assert result[0] is expected
        assert len(ub._session.sent) == int(expected)
    asyncio.run(run())


@pytest.mark.parametrize('failure', ['write', 'malformed'])
def test_later_row_failure_does_not_strand_already_emitted_full_order(broker, failure):
    async def run():
        first = await accepted(broker, OrderSide.SELL)
        async def second_post(*args, **kwargs):
            return {'rt_cd': '0', 'output': {'ODNO': '002', 'KRX_FWDG_ORD_ORGNO': '009'}}
        broker._api_post = second_post
        second = order(OrderSide.SELL)
        second.symbol = '000660'
        assert (await broker.submit_order(second))[0]
        a = row(10, sll_buy_dvsn_cd='01', cncl_yn='N', cnc_cfrm_qty='0', rmn_qty='0')
        b = row(10, odno='002', pdno='000660', sll_buy_dvsn_cd='01',
                cncl_yn='N', cnc_cfrm_qty='0', rmn_qty='0')
        if failure == 'write':
            observe = broker._execution_history.ledger.observe
            async def broken(key, *args, **kwargs):
                if key.endswith(second.id):
                    raise OSError('synthetic second-order write error')
                return await observe(key, *args, **kwargs)
            broker._execution_history.ledger.observe = broken
        response(broker, [a, dict(b, tot_ccld_qty='bad') if failure == 'malformed' else b])
        assert [f.order_id for f in await broker.check_fills()] == [first.id]
        assert first.id not in {o.id for o in await broker.get_open_orders()}
        response(broker, [a, b])
        assert [f.order_id for f in await broker.check_fills()] == [second.id]
        assert await broker.get_open_orders() == []
    asyncio.run(run())


@pytest.mark.parametrize('updates', [dict(ord_dt='20250101'), dict(pdno='000660'),
                                    dict(sll_buy_dvsn_cd='02'), dict(ord_qty='11'),
                                    dict(tot_ccld_qty='11'), dict(orgn_odno='999')])
def test_fault_memory_fill_requires_exact_original_order(broker, updates):
    async def run():
        await broker.initialize_execution_history()
        broker._execution_history.fail('synthetic disk failure')
        o = order(OrderSide.SELL)
        assert (await broker.submit_order(o))[0]
        evidence = row(10, sll_buy_dvsn_cd='01', cncl_yn='N', cnc_cfrm_qty='0', rmn_qty='0')
        evidence.update(updates)
        response(broker, [evidence])
        assert await broker.check_fills() == []
        assert o.filled_quantity == 0
        assert broker.unknown_buy_hold()
    asyncio.run(run())


def test_conflicting_execution_id_never_changes_portfolio_twice(monkeypatch, tmp_path):
    from test_fill_reconciliation import case
    from src.core.event import FillEvent
    from src.core.types import Fill
    async def run():
        _, bot = case(monkeypatch, tmp_path, holdings=10)
        first = Fill(order_id='local', symbol='005930', side=OrderSide.BUY,
                     quantity=3, price=Decimal('10000'), execution_id='session:local:3')
        conflict = Fill(order_id='local', symbol='005930', side=OrderSide.BUY,
                        quantity=4, price=Decimal('10000'), execution_id=first.execution_id)
        await bot.engine.risk_manager.on_fill(FillEvent.from_fill(first))
        bad = FillEvent.from_fill(conflict)
        await bot.engine.risk_manager.on_fill(bad)
        assert bot.engine.portfolio.positions['005930'].quantity == 13
        assert bad.portfolio_applied is False
        assert bot.engine.reconciliation_token() is None
    asyncio.run(run())
