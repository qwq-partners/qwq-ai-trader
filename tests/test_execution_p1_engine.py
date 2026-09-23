"""P1 S3: 실제 엔진·owner·gateway를 거치는 보호 매도 인수."""
from __future__ import annotations

import asyncio
from copy import deepcopy
from decimal import Decimal as D
from pathlib import Path

import pytest
from loguru import logger

import src.utils.session as session_module
from src.core.engine import StrategyManager
from src.core.event import MarketDataEvent
from src.core.types import OrderType
from src.execution.safety.economics import encode_portfolio
from src.execution.safety.guards import GuardDecision
from src.execution.safety.lifecycle import CommandStatus
from src.execution.safety.protection import encode_protection
from test_execution_signal_gateway_acceptance import (
    NOW_KST, _CLOCK, advance, fixture, seed_position, sell_signal,
)


@pytest.fixture(autouse=True)
def synthetic_home(tmp_path, monkeypatch):
    """재사용 하네스의 모듈별 autouse 범위 밖이므로 HOME을 별도로 격리한다."""
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    _CLOCK['kst'] = NOW_KST
    yield
    _CLOCK['kst'] = NOW_KST


@pytest.fixture
def protection_logs():
    """실제 loguru 출력에서 보호 거부 사유의 관측 가능성을 확인한다."""
    messages = []
    sink = logger.add(lambda message: messages.append(message.record['message']))
    try:
        yield messages
    finally:
        logger.remove(sink)


def protective_signal(*, quantity=90, order_type='market', price=D('9700')):
    event = sell_signal(price=price, quantity=quantity)
    event.metadata.update(protection_intent_id='pp-i-engine-test',
                          order_type=order_type, exit_action='sell', source='protection')
    return event


def snapshot(f):
    """직접 writer가 owner와 live 게시본 양쪽을 건드리지 않는지 대조한다."""
    return (f['runtime'].owner.version, deepcopy(f['runtime'].owner.state),
            encode_portfolio(f['engine'].portfolio), encode_protection(f['exits']))


def test_market_data_reaches_strategy_without_mutating_owner_publication(tmp_path, monkeypatch):
    """가격 writer 예외 재도입은 전략 도달과 errors_count를 함께 깨뜨린다."""
    async def scenario():
        f = await fixture(tmp_path, monkeypatch)
        try:
            await seed_position(f)
            received = []

            class Strategy:
                async def on_market_data(self, event, *, position):
                    received.append((event.close, position.quantity))
                    return None

            manager = StrategyManager(f['engine'])
            manager.register_strategy('보호가격독립전략', Strategy())
            before = snapshot(f)
            await f['drive'](MarketDataEvent(symbol='005930', close=D('12000')))
            assert received == [(D('12000'), 100)]
            assert f['engine'].stats.errors_count == 0
            assert snapshot(f) == before
            assert f['posts']() == []
        finally:
            await f['teardown']()
    asyncio.run(scenario())


def test_regular_protection_posts_market_exact_quantity_without_bid_lookup(tmp_path, monkeypatch):
    """시장가를 지정가로 바꾸거나 수량을 전량으로 보정하면 실제 wire가 달라진다."""
    async def scenario():
        f = await fixture(tmp_path, monkeypatch)
        try:
            await seed_position(f)
            bids = []

            async def bid(symbol):
                bids.append(symbol)
                return D('9800')

            f['broker'].get_best_bid = bid
            submitted = []
            original_submit = f['gateway'].submit

            async def submit(event, order, evidence):
                submitted.append(order)
                return await original_submit(event, order, evidence)

            f['gateway'].submit = submit
            await f['drive'](protective_signal())
            assert len(f['posts']()) == 1
            body = f['posts']()[0][1]['json']
            assert (body['ORD_DVSN'], body['ORD_UNPR'], body['ORD_QTY']) == ('01', '0', '90')
            assert bids == []
            assert submitted[0].price is None
            assert f['prepared'][0].order_type == OrderType.MARKET
            assert f['prepared'][0].limit_price is None
            assert f['prepared'][0].intent_id == 'pp-i-engine-test'
            assert f['engine'].stats.errors_count == 0
        finally:
            await f['teardown']()
    asyncio.run(scenario())


@pytest.mark.parametrize('quantity', [None, 0, -1, 101, True, '90', D('90')])
def test_invalid_protection_quantity_never_defaults_to_full_position(tmp_path, monkeypatch, quantity):
    """정수 변환 또는 전량 fallback이 잘못된 보호 요청을 실주문으로 바꾸지 못한다."""
    async def scenario():
        f = await fixture(tmp_path, monkeypatch)
        try:
            await seed_position(f)
            before = snapshot(f)
            await f['drive'](protective_signal(quantity=quantity))
            assert f['posts']() == []
            assert f['prepared'] == []
            assert snapshot(f) == before
            assert f['engine'].stats.errors_count == 0
        finally:
            await f['teardown']()
    asyncio.run(scenario())


@pytest.mark.parametrize('bid,price,want', [(D('9800'), D('9700'), '9800'),
                                         (None, D('9700'), '9700'),
                                         (None, None, None), (D('0'), D('0'), None)])
def test_closing_protection_is_limit_or_unsent(tmp_path, monkeypatch, bid, price, want):
    """15:25는 legacy CLOSED지만 보호 지정가가 가능하며 가격 결측은 시장가가 아니다."""
    async def scenario():
        f = await fixture(tmp_path, monkeypatch)
        try:
            await seed_position(f)
            _CLOCK['kst'] = NOW_KST.replace(hour=15, minute=25)
            f['broker'].best_bid = bid
            await f['drive'](protective_signal(order_type='limit', price=price))
            if want is None:
                assert f['posts']() == []
                assert f['prepared'] == []
            else:
                assert len(f['posts']()) == 1
                body = f['posts']()[0][1]['json']
                assert (body['ORD_DVSN'], body['ORD_UNPR'], body['ORD_QTY']) == ('00', want, '90')
            assert f['engine'].stats.errors_count == 0
        finally:
            await f['teardown']()
    asyncio.run(scenario())


@pytest.mark.parametrize('hour,minute', [(8, 30), (8, 55), (15, 35), (15, 45), (20, 0)])
def test_protection_outside_regular_and_closing_never_posts(tmp_path, monkeypatch, hour, minute):
    """장전·장후 legacy 허용 여부와 무관하게 보호 생산자 세션만 허용한다."""
    async def scenario():
        f = await fixture(tmp_path, monkeypatch)
        try:
            await seed_position(f)
            _CLOCK['kst'] = NOW_KST.replace(hour=hour, minute=minute)
            before = snapshot(f)
            await f['drive'](protective_signal())
            assert f['posts']() == []
            assert f['prepared'] == []
            assert snapshot(f) == before
            assert f['engine'].stats.errors_count == 0
        finally:
            await f['teardown']()
    asyncio.run(scenario())


def test_session_change_during_bid_lookup_does_not_rebind_old_decision(tmp_path, monkeypatch):
    """호가 await 뒤 세션 재확인을 제거하면 이전 세션의 결정을 새 요청으로 송신한다."""
    async def scenario():
        f = await fixture(tmp_path, monkeypatch)
        try:
            await seed_position(f)
            _CLOCK['kst'] = NOW_KST.replace(hour=15, minute=29)
            bids = []

            async def bid(symbol):
                bids.append(symbol)
                await asyncio.sleep(0)
                _CLOCK['kst'] = NOW_KST.replace(hour=15, minute=30)
                return D('9800')

            f['broker'].get_best_bid = bid
            before = snapshot(f)
            await f['drive'](protective_signal(order_type='limit'))
            assert bids == ['005930']
            assert f['posts']() == []
            assert f['prepared'] == []
            assert snapshot(f) == before
            assert f['engine'].stats.errors_count == 0
        finally:
            await f['teardown']()
    asyncio.run(scenario())


def test_regular_market_waiting_for_pending_lock_is_refused_after_closing(tmp_path, monkeypatch):
    """실제 pending lock 대기 동안 regular→closing이 되면 MARKET 결정을 폐기한다."""
    async def scenario():
        f = await fixture(tmp_path, monkeypatch)
        task = None
        try:
            await seed_position(f)
            _CLOCK['kst'] = NOW_KST.replace(hour=15, minute=19, second=59)
            before = snapshot(f)
            lock = f['rm']._pending_lock
            await lock.acquire()
            try:
                task = asyncio.create_task(f['drive'](protective_signal()))
                await asyncio.sleep(0)
                assert not task.done()
                assert lock.locked()
                assert lock._waiters is not None and len(lock._waiters) == 1
                assert not lock._waiters[0].done()
                _CLOCK['kst'] = NOW_KST.replace(hour=15, minute=20)
            finally:
                lock.release()
            await task
            assert f['posts']() == []
            assert f['prepared'] == []
            assert snapshot(f) == before
            assert f['engine'].stats.errors_count == 0
        finally:
            if task is not None and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            await f['teardown']()
    asyncio.run(scenario())


def test_regular_rejects_limit_protection_before_bid_lookup(tmp_path, monkeypatch):
    """정규장 보호 LIMIT은 호가 조회 전에 거부하며 시장가로 자동 보정하지 않는다."""
    async def scenario():
        f = await fixture(tmp_path, monkeypatch)
        try:
            await seed_position(f)
            bids = []

            async def bid(symbol):
                bids.append(symbol)
                return D('9800')

            f['broker'].get_best_bid = bid
            before = snapshot(f)
            await f['drive'](protective_signal(order_type='limit'))
            assert f['posts']() == []
            assert f['prepared'] == []
            assert bids == []
            assert snapshot(f) == before
            assert f['engine'].stats.errors_count == 0
        finally:
            await f['teardown']()
    asyncio.run(scenario())


def test_closing_rejects_market_metadata_before_price_lookup(tmp_path, monkeypatch):
    async def scenario():
        f = await fixture(tmp_path, monkeypatch)
        try:
            await seed_position(f)
            _CLOCK['kst'] = NOW_KST.replace(hour=15, minute=21)
            await f['drive'](protective_signal())
            assert f['posts']() == []
            assert f['prepared'] == []
            assert f['engine'].stats.errors_count == 0
        finally:
            await f['teardown']()
    asyncio.run(scenario())


@pytest.mark.parametrize('holiday', ['weekend', 'registered'])
def test_protection_preserves_holiday_guard_before_bid_lookup(tmp_path, monkeypatch, holiday):
    """시간대만 검사하는 우회는 주말·기존 등록 휴장일에도 호가와 주문을 만든다."""
    async def scenario():
        f = await fixture(tmp_path, monkeypatch)
        try:
            await seed_position(f)
            _CLOCK['kst'] = NOW_KST.replace(hour=15, minute=25)
            if holiday == 'weekend':
                _CLOCK['kst'] = _CLOCK['kst'].replace(day=19)
            else:
                monkeypatch.setattr(session_module, '_kr_market_holidays', {NOW_KST.date()})
            bids = []

            async def bid(symbol):
                bids.append(symbol)
                return D('9800')

            f['broker'].get_best_bid = bid
            before = snapshot(f)
            result = await f['rm'].on_signal(protective_signal(order_type='limit'))
            assert result is None
            assert bids == []
            assert snapshot(f) == before
        finally:
            await f['teardown']()
    asyncio.run(scenario())


def test_holiday_update_during_bid_wait_refuses_order(tmp_path, monkeypatch):
    """날짜·세션이 같아도 호가 대기 중 갱신된 휴장은 송신 전에 다시 검사한다."""
    async def scenario():
        f = await fixture(tmp_path, monkeypatch)
        try:
            await seed_position(f)
            monkeypatch.setattr(session_module, '_kr_market_holidays', set())
            _CLOCK['kst'] = NOW_KST.replace(hour=15, minute=25)
            assert session_module.is_kr_market_holiday(NOW_KST.date()) is False
            bids = []

            async def bid(symbol):
                bids.append(symbol)
                await asyncio.sleep(0)
                session_module.set_kr_holidays({NOW_KST.date()})
                return D('9800')

            f['broker'].get_best_bid = bid
            before = snapshot(f)
            await f['drive'](protective_signal(order_type='limit'))
            assert bids == ['005930']
            assert session_module.is_kr_market_holiday(_CLOCK['kst'].date()) is True
            assert f['engine'].stats.errors_count == 0
            assert f['posts']() == []
            assert f['prepared'] == []
            assert snapshot(f) == before
        finally:
            await f['teardown']()
    asyncio.run(scenario())


@pytest.mark.parametrize('change', ['quantity', 'date', 'general_cooldown', 'protection_cooldown'])
def test_bid_await_rechecks_current_holdings_date_and_signal_competition(
        tmp_path, monkeypatch, change, protection_logs):
    """일반 신호의 시계만 보호를 막지 않으며 보유·일자·보호 경합은 재검사한다."""
    async def scenario():
        f = await fixture(tmp_path, monkeypatch)
        try:
            await seed_position(f)
            _CLOCK['kst'] = NOW_KST.replace(hour=15, minute=25)
            after_change = []

            async def bid(symbol):
                await asyncio.sleep(0)
                if change == 'quantity':
                    def reduce(state):
                        state['portfolio']['positions'][symbol]['quantity'] = 80
                        state['protection']['states'][symbol]['remaining_quantity'] = 80
                        return state

                    await f['runtime'].owner.mutate('보유축소-호가대기', reduce)
                    assert f['engine'].portfolio.positions[symbol].quantity == 80
                elif change == 'date':
                    _CLOCK['kst'] = _CLOCK['kst'].replace(day=21)
                elif change == 'general_cooldown':
                    f['rm']._last_signal_time[symbol] = _CLOCK['kst'].replace(tzinfo=None)
                else:
                    f['rm']._last_protection_signal_time[symbol] = _CLOCK['kst'].replace(tzinfo=None)
                after_change.append(snapshot(f))
                return D('9800')

            f['broker'].get_best_bid = bid
            await f['drive'](protective_signal(order_type='limit'))
            assert len(after_change) == 1
            if change == 'general_cooldown':
                assert len(f['posts']()) == 1
                assert f['posts']()[0][1]['json']['ORD_QTY'] == '90'
                assert f['prepared'][0].intent_id == 'pp-i-engine-test'
                assert f['engine'].stats.errors_count == 0
                return
            assert f['posts']() == []
            assert f['prepared'] == []
            assert snapshot(f) == after_change[0]
            assert f['engine'].stats.errors_count == 0
            if change == 'protection_cooldown':
                assert any(message.startswith('[리스크] 보호 매도 최종 주문 진행·신호 쿨다운 차단:')
                           for message in protection_logs)
        finally:
            await f['teardown']()
    asyncio.run(scenario())


@pytest.mark.parametrize('failure', [TimeoutError, TypeError])
def test_bid_transient_failure_falls_back_but_programming_error_is_reported(tmp_path, monkeypatch, failure):
    """넓은 except를 복사하면 프로그래밍 오류까지 성공 주문으로 바뀐다."""
    async def scenario():
        f = await fixture(tmp_path, monkeypatch)
        try:
            await seed_position(f)
            _CLOCK['kst'] = NOW_KST.replace(hour=15, minute=25)
            f['broker'].best_bid_error = failure('합성 호가 오류')
            await f['drive'](protective_signal(order_type='limit'))
            if failure is TimeoutError:
                assert len(f['posts']()) == 1
                body = f['posts']()[0][1]['json']
                assert (body['ORD_DVSN'], body['ORD_UNPR']) == ('00', '9700')
                assert f['engine'].stats.errors_count == 0
            else:
                assert f['posts']() == []
                assert f['prepared'] == []
                assert f['engine'].stats.errors_count == 1
                assert f['errors']()[0].error_type == 'TypeError'
        finally:
            await f['teardown']()
    asyncio.run(scenario())


def test_protection_keeps_cooldown_record_and_does_not_query_on_duplicate(tmp_path, monkeypatch):
    """early-return이 기존 신호 cooldown 기록을 빠뜨리는 회귀를 잡는다."""
    async def scenario():
        f = await fixture(tmp_path, monkeypatch)
        try:
            await seed_position(f)
            _CLOCK['kst'] = NOW_KST.replace(hour=15, minute=25)
            await f['drive'](protective_signal(order_type='limit'))
            assert len(f['posts']()) == 1
            assert f['rm']._last_signal_time['005930'] == _CLOCK['kst'].replace(tzinfo=None)
            bids = []

            async def bid(symbol):
                bids.append(symbol)
                return D('9800')

            f['broker'].get_best_bid = bid
            await f['drive'](protective_signal(order_type='limit'))
            assert len(f['posts']()) == 1
            assert bids == []
            assert f['engine'].stats.errors_count == 0
        finally:
            await f['teardown']()
    asyncio.run(scenario())


def test_nonprotective_sell_keeps_legacy_limit_and_quantity_coercion(tmp_path, monkeypatch):
    """보호 ID 키가 없는 일반 SELL은 기존 LIMIT·문자열 수량 변환을 유지한다."""
    async def scenario():
        f = await fixture(tmp_path, monkeypatch)
        try:
            await seed_position(f)
            f['broker'].best_bid = D('9800')
            event = protective_signal(quantity='90')
            del event.metadata['protection_intent_id']
            await f['drive'](event)
            assert len(f['posts']()) == 1
            body = f['posts']()[0][1]['json']
            assert (body['ORD_DVSN'], body['ORD_UNPR'], body['ORD_QTY']) == ('00', '9800', '90')
            assert f['engine'].stats.errors_count == 0
        finally:
            await f['teardown']()
    asyncio.run(scenario())


@pytest.mark.parametrize('intent', [None, '', 123, True, ' ', ' pp-i-test', 'pp-i-test '])
def test_explicit_invalid_protection_identity_is_refused(tmp_path, monkeypatch, intent, protection_logs):
    """보호 키가 명시된 손상 요청이 일반 SELL 수량 변환·지정가 경로로 흐르지 않는다."""
    async def scenario():
        f = await fixture(tmp_path, monkeypatch)
        try:
            await seed_position(f)
            event = protective_signal()
            event.metadata['protection_intent_id'] = intent
            before = snapshot(f)
            await f['drive'](event)
            assert f['posts']() == []
            assert f['prepared'] == []
            assert snapshot(f) == before
            assert f['engine'].stats.errors_count == 0
            assert any(message.startswith('[리스크] 보호 매도 식별자 거부:')
                       for message in protection_logs)
        finally:
            await f['teardown']()
    asyncio.run(scenario())


def test_dict_subclass_with_explicit_protection_identity_is_refused(tmp_path, monkeypatch, protection_logs):
    """dict 하위형의 표시 숨김 구현도 실제 저장된 보호 키를 우회할 수 없다."""
    class Metadata(dict):
        def __contains__(self, key):
            return False

    async def scenario():
        f = await fixture(tmp_path, monkeypatch)
        try:
            await seed_position(f)
            event = protective_signal(quantity='90')
            event.metadata = Metadata(event.metadata)
            before = snapshot(f)
            await f['drive'](event)
            assert f['posts']() == []
            assert f['prepared'] == []
            assert snapshot(f) == before
            assert f['engine'].stats.errors_count == 0
            assert any(message.startswith('[리스크] 보호 매도 식별자 거부:')
                       for message in protection_logs)
        finally:
            await f['teardown']()
    asyncio.run(scenario())


@pytest.mark.parametrize('waves', [1, 3])
def test_repeated_general_rejection_cannot_delay_protection(tmp_path, monkeypatch, waves):
    """일반 거부가 매번 공통 시계를 갱신해도 바로 뒤 보호90주가 송신되어야 한다."""
    async def scenario():
        f = await fixture(tmp_path, monkeypatch)
        try:
            await seed_position(f)
            retained = protective_signal()
            for index in range(waves):
                if index:
                    advance(31)
                f['session'][0] = GuardDecision(False, '합성-일반신호-최종거부')
                await f['drive'](sell_signal(quantity=10))
                assert f['posts']() == []
                assert f['prepared'] == []
                assert len(f['errors']()) == index + 1
                assert '합성-일반신호-최종거부' in f['errors']()[-1].message
                assert f['rm']._last_signal_time['005930'] == _CLOCK['kst'].replace(tzinfo=None)
            f['session'][0] = GuardDecision(True, '합성-세션허용')
            await f['drive'](retained)
            assert len(f['posts']()) == 1
            assert f['prepared'][0].intent_id == 'pp-i-engine-test'
            assert f['posts']()[0][1]['json']['ORD_QTY'] == '90'
            assert len(f['errors']()) == waves
        finally:
            await f['teardown']()
    asyncio.run(scenario())


def test_general_zero_size_cannot_delay_newly_held_protection(tmp_path, monkeypatch):
    """보유 없는 일반 SELL의 사이징0 기록이 뒤이어 게시된 보유의 손절을 막지 않는다."""
    async def scenario():
        f = await fixture(tmp_path, monkeypatch)
        try:
            await f['drive'](sell_signal())
            assert f['posts']() == [] and f['prepared'] == []
            assert f['rm']._last_signal_time['005930'] == _CLOCK['kst'].replace(tzinfo=None)
            await seed_position(f)
            await f['drive'](protective_signal())
            assert len(f['posts']()) == 1
            assert f['posts']()[0][1]['json']['ORD_QTY'] == '90'
            assert f['engine'].stats.errors_count == 0
        finally:
            await f['teardown']()
    asyncio.run(scenario())


def test_protection_thirty_second_boundary_survives_general_rejection(tmp_path, monkeypatch):
    """보호 후보가 두 cooldown을 무장하고 일반 거부는 보호의30초 만료를 연장하지 않는다."""
    async def scenario():
        f = await fixture(tmp_path, monkeypatch)
        try:
            await seed_position(f)
            # 후보만 생성해 원장 미해결 장벽과 cooldown을 독립적으로 검증한다.
            assert await f['rm'].on_signal(protective_signal())
            assert await f['rm'].on_signal(sell_signal(quantity=10)) is None
            advance(29)
            assert await f['rm'].on_signal(protective_signal()) is None
            advance(1)
            f['session'][0] = GuardDecision(False, '합성-일반신호-최종거부')
            await f['drive'](sell_signal(quantity=10))
            assert len(f['errors']()) == 1 and f['prepared'] == []
            f['session'][0] = GuardDecision(True, '합성-세션허용')
            await f['drive'](protective_signal())
            assert len(f['posts']()) == 1
            assert f['posts']()[0][1]['json']['ORD_QTY'] == '90'
        finally:
            await f['teardown']()
    asyncio.run(scenario())


def test_concurrent_closing_protection_reserves_and_posts_only_once(tmp_path, monkeypatch):
    """두 요청이 모두 호가 await를 넘어도 최종 보호 시계 검사가 중복90주 예약을 막는다."""
    async def scenario():
        f = await fixture(tmp_path, monkeypatch)
        try:
            await seed_position(f)
            _CLOCK['kst'] = NOW_KST.replace(hour=15, minute=25)
            both_waiting, release = asyncio.Event(), asyncio.Event()
            waiting = []
            async def bid(symbol):
                waiting.append(symbol)
                if len(waiting) == 2:
                    both_waiting.set()
                await release.wait()
                return D('9800')
            f['broker'].get_best_bid = bid
            tasks = [asyncio.create_task(f['engine']._submit_signal(
                protective_signal(order_type='limit'))) for _ in range(2)]
            try:
                await asyncio.wait_for(both_waiting.wait(), 2)
            finally:
                release.set()
                await asyncio.gather(*tasks)
            assert len(f['posts']()) == len(f['prepared']) == 1
            attempt, = f['runtime'].owner.state['attempts'].values()
            assert attempt['quantity'] == attempt['reserved_quantity'] == 90
            assert f['engine'].stats.errors_count == 0
        finally:
            await f['teardown']()
    asyncio.run(scenario())


@pytest.mark.parametrize('unknown', [False, True])
def test_expired_protection_cooldown_keeps_open_or_unknown_reservation(tmp_path, monkeypatch, unknown):
    """보호 시계 만료가 owner의 ACK/UNKNOWN90주 예약을 지우거나 중복 매도를 열면 실패한다."""
    async def scenario():
        f = await fixture(tmp_path, monkeypatch)
        try:
            await seed_position(f)
            if unknown:
                f['broker'].ack_output = None
            await f['drive'](protective_signal())
            assert len(f['posts']()) == 1
            original = deepcopy(f['runtime'].owner.state['attempts'])
            attempt, = original.values()
            assert attempt['reserved_quantity'] == 90
            assert attempt['command_status'] == ('unknown' if unknown else 'acknowledged')
            advance(31)
            second = protective_signal()
            second.metadata['protection_intent_id'] = 'pp-i-must-not-replace-open'
            await f['drive'](second)
            assert len(f['posts']()) == 1
            assert f['runtime'].owner.state['attempts'] == original
            assert 'pp-i-must-not-replace-open' not in f['runtime'].owner.state['intents']
            assert len(f['errors']()) == 1
            assert ('unresolved_execution_evidence' if unknown else 'unresolved_symbol_attempt') in f['errors']()[0].message
        finally:
            await f['teardown']()
    asyncio.run(scenario())


@pytest.mark.parametrize('boundary', ['before_bind', 'after_limiter'])
def test_gateway_existing_session_guards_refuse_regular_market_after_closing(
        tmp_path, monkeypatch, boundary):
    """엔진 반환 뒤 builder와 마지막 limiter await 뒤 transport가 각각 세션을 막는다."""
    async def scenario():
        f = await fixture(tmp_path, monkeypatch)
        try:
            await seed_position(f)
            _CLOCK['kst'] = NOW_KST.replace(hour=15, minute=19, second=59)
            reached = []
            if boundary == 'before_bind':
                original_submit = f['gateway'].submit

                async def submit(event, order, evidence):
                    assert order.order_type == OrderType.MARKET
                    assert order.price is None
                    reached.append('gateway')
                    _CLOCK['kst'] = NOW_KST.replace(hour=15, minute=20)
                    return await original_submit(event, order, evidence)

                f['gateway'].submit = submit
            else:
                original_limiter = f['broker']._rate_limit

                async def limiter(tr_id):
                    reached.append(tr_id)
                    await asyncio.sleep(0)
                    _CLOCK['kst'] = NOW_KST.replace(hour=15, minute=20)

                f['broker']._rate_limit = limiter

            await f['drive'](protective_signal())
            assert f['posts']() == []
            if boundary == 'before_bind':
                assert reached == ['gateway']
                assert f['prepared'] == []
                assert f['runtime'].owner.state['attempts'] == {}
                assert f['engine'].stats.errors_count == 1
                assert 'unsupported_submit_session' in f['errors']()[0].message
            else:
                assert reached == ['TTTC0801U']
                assert len(f['prepared']) == 1
                assert f['outcomes'][0].status == CommandStatus.NOT_SENT
                assert f['outcomes'][0].reason_code == 'current_request_guard_rejected'
                attempt = next(iter(f['runtime'].owner.state['attempts'].values()))
                assert attempt['state'] == 'final_rejected'
                assert attempt['reserved_quantity'] == 0
                assert f['engine'].stats.errors_count == 0
                original_attempts = deepcopy(f['runtime'].owner.state['attempts'])
                original_intent = deepcopy(f['runtime'].owner.state['intents']['pp-i-engine-test'])
                f['broker']._rate_limit = original_limiter
                advance(31)
                assert (_CLOCK['kst'].hour, _CLOCK['kst'].minute, _CLOCK['kst'].second) == (15, 20, 31)
                f['broker'].best_bid = D('9800')
                replacement = protective_signal(order_type='limit')
                replacement.metadata['protection_intent_id'] = 'pp-i-engine-after-session'
                await f['drive'](replacement)
                assert len(f['posts']()) == 1
                body = f['posts']()[0][1]['json']
                assert (body['ORD_DVSN'], body['ORD_UNPR'], body['ORD_QTY']) == ('00', '9800', '90')
                assert len(f['prepared']) == 2
                assert f['prepared'][-1].intent_id == 'pp-i-engine-after-session'
                attempts = f['runtime'].owner.state['attempts']
                assert len(attempts) == 2
                for attempt_id, original in original_attempts.items():
                    assert attempts[attempt_id] == original
                assert f['runtime'].owner.state['intents']['pp-i-engine-test'] == original_intent
                assert len(set(attempts) - set(original_attempts)) == 1
                assert len(f['runtime'].owner.state['intents']) == 2
                assert f['outcomes'][-1].status == CommandStatus.ACKNOWLEDGED
                assert f['engine'].stats.errors_count == 0
        finally:
            await f['teardown']()
    asyncio.run(scenario())
