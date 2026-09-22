"""P1 S3: 실제 엔진·owner·gateway를 거치는 보호 매도 인수."""
from __future__ import annotations

import asyncio
from copy import deepcopy
from decimal import Decimal as D
from pathlib import Path

import pytest

import src.utils.session as session_module
from src.core.engine import StrategyManager
from src.core.event import MarketDataEvent
from src.core.types import OrderType
from src.execution.safety.economics import encode_portfolio
from src.execution.safety.protection import encode_protection
from test_execution_signal_gateway_acceptance import (
    NOW_KST, _CLOCK, fixture, seed_position, sell_signal,
)


@pytest.fixture(autouse=True)
def synthetic_home(tmp_path, monkeypatch):
    """재사용 하네스의 모듈별 autouse 범위 밖이므로 HOME을 별도로 격리한다."""
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    _CLOCK['kst'] = NOW_KST
    yield
    _CLOCK['kst'] = NOW_KST


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


@pytest.mark.parametrize('start,end', [((15, 19), (15, 20)), ((15, 29), (15, 30))])
def test_session_change_during_bid_lookup_does_not_rebind_old_decision(tmp_path, monkeypatch, start, end):
    """호가 await 뒤 세션 재확인을 제거하면 이전 세션의 결정을 새 요청으로 송신한다."""
    async def scenario():
        f = await fixture(tmp_path, monkeypatch)
        try:
            await seed_position(f)
            _CLOCK['kst'] = NOW_KST.replace(hour=start[0], minute=start[1])
            bids = []

            async def bid(symbol):
                bids.append(symbol)
                await asyncio.sleep(0)
                _CLOCK['kst'] = NOW_KST.replace(hour=end[0], minute=end[1])
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
            if holiday == 'weekend':
                _CLOCK['kst'] = NOW_KST.replace(day=19)
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


@pytest.mark.parametrize('change', ['quantity', 'date', 'cooldown'])
def test_bid_await_rechecks_current_holdings_date_and_signal_competition(tmp_path, monkeypatch, change):
    """가격 대기 중 보유 축소·날짜 이월·경쟁 신호가 생기면 이전 결정은 발행하지 않는다."""
    async def scenario():
        f = await fixture(tmp_path, monkeypatch)
        try:
            await seed_position(f)
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
                    _CLOCK['kst'] = NOW_KST.replace(day=21)
                else:
                    f['rm']._last_signal_time[symbol] = NOW_KST.replace(tzinfo=None)
                after_change.append(snapshot(f))
                return D('9800')

            f['broker'].get_best_bid = bid
            await f['drive'](protective_signal(order_type='limit'))
            assert len(after_change) == 1
            assert f['posts']() == []
            assert f['prepared'] == []
            assert snapshot(f) == after_change[0]
            assert f['engine'].stats.errors_count == 0
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
            await f['drive'](protective_signal())
            assert len(f['posts']()) == 1
            assert f['rm']._last_signal_time['005930'] == NOW_KST.replace(tzinfo=None)
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


@pytest.mark.parametrize('intent', ['', None, 123])
def test_nonprotective_sell_keeps_legacy_limit_and_quantity_coercion(tmp_path, monkeypatch, intent):
    """비어있거나 문자열 아닌 태그는 보호 분기를 열지 않는다."""
    async def scenario():
        f = await fixture(tmp_path, monkeypatch)
        try:
            await seed_position(f)
            f['broker'].best_bid = D('9800')
            event = protective_signal(quantity='90')
            event.metadata['protection_intent_id'] = intent
            await f['drive'](event)
            assert len(f['posts']()) == 1
            body = f['posts']()[0][1]['json']
            assert (body['ORD_DVSN'], body['ORD_UNPR'], body['ORD_QTY']) == ('00', '9800', '90')
            assert f['engine'].stats.errors_count == 0
        finally:
            await f['teardown']()
    asyncio.run(scenario())
