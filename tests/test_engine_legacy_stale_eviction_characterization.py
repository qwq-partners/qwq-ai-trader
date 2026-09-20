"""S4-0: runtime 없는 legacy 엔진의 세 경로(90초 SELL 폴백·10분 BUY 정리·eviction) 특성화.

**이 파일은 legacy 불변 대조용이며 옳은 동작의 정의가 아니다.** 아래 현행 결함을 고치지 않고
그대로 고정한다 — S4 가 이관하지 않는 것을 드러내 두는 것이 목적이다(계획 §4 결정 ④·§6):

- 동시호가(15:20~15:30)에는 취소만 보내고 재주문이 없다 — 지정가가 그대로 남는다
- 폴백 상한(2회) 뒤에는 취소도 재주문도 없이 `clear_pending` 만 한다 — 원 지정가 방치
- `submit_order` 예외는 접수 여부를 모른 채 `clear_pending` 한다
- 10분 BUY 정리는 취소 0건도 최종성으로 읽어 예약(`_reserved_by_order`)까지 푼다
- 폴백 루프에는 `exit_exempt` 확인이 없다 — 자동매도 금지 종목도 시장가로 나간다
- 연쇄 축출이 닫혀 있지 않다 — 같은 배치의 고득점 BUY 두 건이 서로 다른 희생자를 연달아 축출한다

구동은 실제 `UnifiedEngine`(runtime 없음) + 실제 inner `RiskManager` + engine 모듈 naive 시계
동결이고, 심는 것은 브로커의 `cancel_all_for_symbol`·`submit_order` 둘뿐이다(본문 monkeypatch 0).

실행: venv/bin/python -m pytest tests/test_engine_legacy_stale_eviction_characterization.py -q
"""
import asyncio
from datetime import timedelta
from decimal import Decimal as D
from pathlib import Path

import pytest

from src.core.engine import UnifiedEngine
from src.core.event import OrderEvent, SignalEvent
from src.core.types import OrderSide, OrderType, Position, TradingConfig

from test_execution_qualification_publishers import buy
from test_execution_signal_gateway_wiring import ENGINE_NOW, drive, engine_clock, risk_manager
from test_risk_sizing import PRICE, SYM


@pytest.fixture(autouse=True)
def synthetic_home(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)


OTHER = '000660'      # 폴백 루프를 구동하는 다른 종목의 SIGNAL
WEAK = '035420'       # 진입 점수 낮은 손실 포지션
WEAKER = '051910'     # 같은 점수에 손실이 더 큰 포지션
CLOSING_AUCTION = ENGINE_NOW.replace(hour=15, minute=25)
LEDGERS = ('_pending_orders', '_pending_quantities', '_pending_timestamps', '_pending_sides',
           '_reserved_by_order', '_pending_strategy', '_pending_signal_cache',
           '_pending_fallback_count')


class Broker:
    """취소·송신 두 지점만 심는 합성 브로커. 값이 `Exception` 이면 그것을 던진다."""

    def __init__(self, *, cancelled=1, submitted=(True, 'ORD-1')):
        self.calls = []
        self._cancelled = cancelled
        self._submitted = submitted

    async def cancel_all_for_symbol(self, symbol):
        self.calls.append(('cancel', symbol))
        if isinstance(self._cancelled, Exception):
            raise self._cancelled
        return self._cancelled

    async def submit_order(self, order):
        self.calls.append(('submit', order))
        if isinstance(self._submitted, Exception):
            raise self._submitted
        return self._submitted


def legacy(monkeypatch, *, broker=None):
    """runtime 없는 실제 엔진 + 실제 inner RiskManager. 시계는 engine 모듈 하나로 민다."""
    engine = UnifiedEngine(TradingConfig(initial_capital=D('2000000')))
    engine.broker = broker
    rm = risk_manager(monkeypatch, engine)
    now = engine_clock(monkeypatch)
    # `_rm` 은 `object.__new__` 라 __init__ 의 교체 상태가 없다 — 제품 기본값으로 채운다.
    rm._REPLACEMENT_LAST_EVICT_TS = {}
    rm._REPLACEMENT_COOLDOWN_SEC = 600
    rm._exit_exempt_ref = set()
    assert engine._execution_runtime is None
    return {'engine': engine, 'rm': rm, 'broker': broker, 'now': now}


def full_book(rm):
    """`_risk_validator` 가 '최대 포지션 수 도달' 을 돌려줘야 eviction 호출부에 닿는다."""
    rm._risk_validator = type('V', (), {'can_open_position': staticmethod(
        lambda *args, **kwargs: (False, '최대 포지션 수 도달'))})()


def hold(f, symbol, *, quantity=100, price=PRICE, strategy='sepa_trend', score=None):
    pos = Position(symbol=symbol, quantity=quantity, avg_price=PRICE, current_price=price,
                   strategy=strategy)
    if score is not None:
        pos.entry_signal_score = score
    f['engine'].portfolio.positions[symbol] = pos
    return pos


def stale(rm, symbol, *, side, now, seconds=200, quantity=None, fallback=0):
    rm._pending_orders.add(symbol)
    rm._pending_sides[symbol] = side
    rm._pending_timestamps[symbol] = now - timedelta(seconds=seconds)
    rm._pending_fallback_count[symbol] = fallback
    if quantity is not None:
        rm._pending_quantities[symbol] = quantity


def held_in(rm, symbol):
    """그 종목이 아직 남아 있는 legacy 장부 이름들(구동용 SIGNAL 이 쌓는 행과 섞이지 않는다)."""
    return {name for name in LEDGERS if symbol in getattr(rm, name)}


def submitted(broker):
    return [order for kind, order in broker.calls if kind == 'submit']


def kinds(broker):
    return [kind for kind, _ in broker.calls]


def sells(engine):
    return [event for event in engine._event_queue
            if type(event) is SignalEvent and event.side == OrderSide.SELL]


# ── 90초 SELL 폴백 ───────────────────────────────────────────────────────

def test_a_stale_limit_sell_is_cancelled_then_resubmitted_at_market(monkeypatch):
    """취소 → 시장가 재주문이 이 순서로 1회씩. 성공하면 세 장부가 갱신된다."""
    async def scenario():
        f = legacy(monkeypatch, broker=Broker())
        hold(f, SYM)
        stale(f['rm'], SYM, side=OrderSide.SELL, now=ENGINE_NOW, quantity=30)
        await drive(f['engine'], buy(OTHER))
        assert kinds(f['broker']) == ['cancel', 'submit']
        order = submitted(f['broker'])[0]
        assert (order.symbol, order.side, order.order_type) == (SYM, OrderSide.SELL,
                                                                OrderType.MARKET)
        rm = f['rm']
        assert rm._pending_timestamps[SYM] == ENGINE_NOW
        assert rm._pending_sides[SYM] == OrderSide.SELL
        assert rm._pending_fallback_count[SYM] == 1
    asyncio.run(scenario())


@pytest.mark.parametrize('pending_qty, expected', [(30, 30), (None, 100)])
def test_the_fallback_quantity_is_the_pending_quantity_not_the_whole_position(
        monkeypatch, pending_qty, expected):
    """2026-08-04 P0 — 분할 익절 미체결이 전량 시장가로 번지던 버그의 수정을 고정한다."""
    async def scenario():
        f = legacy(monkeypatch, broker=Broker())
        hold(f, SYM)
        stale(f['rm'], SYM, side=OrderSide.SELL, now=ENGINE_NOW, quantity=pending_qty)
        await drive(f['engine'], buy(OTHER))
        assert submitted(f['broker'])[0].quantity == expected
    asyncio.run(scenario())


def test_the_third_fallback_only_clears_the_pending_and_leaves_the_limit_order(monkeypatch):
    """현행 결함: 상한 뒤에는 취소조차 보내지 않아 원 지정가가 거래소에 남는다."""
    async def scenario():
        f = legacy(monkeypatch, broker=Broker())
        hold(f, SYM)
        stale(f['rm'], SYM, side=OrderSide.SELL, now=ENGINE_NOW, quantity=30, fallback=2)
        await drive(f['engine'], buy(OTHER))
        assert f['broker'].calls == []
        assert held_in(f['rm'], SYM) == set()
    asyncio.run(scenario())


def test_in_the_closing_auction_the_cancel_is_sent_and_nothing_is_reordered(monkeypatch):
    """현행 결함: 15:20~15:30 에는 취소만 나가고 재주문이 없다 — pending 은 그대로 남는다."""
    async def scenario():
        f = legacy(monkeypatch, broker=Broker())
        f['now'][0] = CLOSING_AUCTION
        hold(f, SYM)
        stale(f['rm'], SYM, side=OrderSide.SELL, now=CLOSING_AUCTION, quantity=30)
        await drive(f['engine'], buy(OTHER))
        assert kinds(f['broker']) == ['cancel']
        rm = f['rm']
        assert rm._pending_timestamps[SYM] == CLOSING_AUCTION - timedelta(seconds=200)
        assert rm._pending_fallback_count[SYM] == 0
    asyncio.run(scenario())


@pytest.mark.parametrize('fallback, cleared', [(0, False), (1, True)])
def test_a_rejected_fallback_counts_up_and_gives_up_at_the_limit(monkeypatch, fallback, cleared):
    """반환 False 는 카운트만 올린다 — 그 카운트가 상한에 닿는 순간 청산 시그널을 포기한다."""
    async def scenario():
        f = legacy(monkeypatch, broker=Broker(submitted=(False, '거부')))
        hold(f, SYM)
        stale(f['rm'], SYM, side=OrderSide.SELL, now=ENGINE_NOW, quantity=30,
              fallback=fallback)
        await drive(f['engine'], buy(OTHER))
        assert kinds(f['broker']) == ['cancel', 'submit']
        rm = f['rm']
        if cleared:
            assert held_in(rm, SYM) == set()
        else:
            assert rm._pending_fallback_count[SYM] == fallback + 1
            assert rm._pending_timestamps[SYM] == ENGINE_NOW - timedelta(seconds=200)
    asyncio.run(scenario())


def test_a_submit_exception_clears_the_pending_without_knowing_it_was_accepted(monkeypatch):
    """현행 결함: 예외는 '미접수' 가 아니다 — 접수됐을 수도 있는데 장부를 비운다."""
    async def scenario():
        f = legacy(monkeypatch, broker=Broker(submitted=RuntimeError('합성 송신 장애')))
        hold(f, SYM)
        stale(f['rm'], SYM, side=OrderSide.SELL, now=ENGINE_NOW, quantity=30)
        await drive(f['engine'], buy(OTHER))
        assert kinds(f['broker']) == ['cancel', 'submit']
        assert held_in(f['rm'], SYM) == set()
    asyncio.run(scenario())


def test_a_cancel_exception_skips_the_reorder_and_keeps_the_pending(monkeypatch):
    """취소는 반환값이 아니라 예외만 의미한다 — 예외면 재주문을 건너뛰고 다음 주기에 재감지."""
    async def scenario():
        f = legacy(monkeypatch, broker=Broker(cancelled=RuntimeError('합성 취소 장애')))
        hold(f, SYM)
        stale(f['rm'], SYM, side=OrderSide.SELL, now=ENGINE_NOW, quantity=30)
        await drive(f['engine'], buy(OTHER))
        assert kinds(f['broker']) == ['cancel']
        assert f['rm']._pending_timestamps[SYM] == ENGINE_NOW - timedelta(seconds=200)
        assert f['rm']._pending_fallback_count[SYM] == 0
    asyncio.run(scenario())


def test_an_exit_exempt_symbol_is_still_market_sold_by_the_fallback_loop(monkeypatch):
    """현행 결함: 7개 청산 가드 중 이 루프에는 `_exit_exempt_ref` 확인이 없다."""
    async def scenario():
        f = legacy(monkeypatch, broker=Broker())
        f['rm']._exit_exempt_ref = {SYM}
        hold(f, SYM)
        stale(f['rm'], SYM, side=OrderSide.SELL, now=ENGINE_NOW, quantity=30)
        await drive(f['engine'], buy(OTHER))
        assert kinds(f['broker']) == ['cancel', 'submit']
        assert submitted(f['broker'])[0].symbol == SYM
    asyncio.run(scenario())


def test_nothing_is_swept_while_no_signal_arrives(monkeypatch):
    """두 루프는 on_signal 본문 안에만 있다 — 주기 태스크가 아니다(사실 7)."""
    async def scenario():
        f = legacy(monkeypatch, broker=Broker())
        hold(f, SYM)
        stale(f['rm'], SYM, side=OrderSide.SELL, now=ENGINE_NOW, quantity=30, seconds=3600)
        f['now'][0] = ENGINE_NOW + timedelta(hours=1)
        await asyncio.sleep(0)
        assert f['broker'].calls == []
        assert f['rm']._pending_orders == {SYM}
    asyncio.run(scenario())


# ── 10분 BUY 정리 ────────────────────────────────────────────────────────

@pytest.mark.parametrize('cancelled', [0, 3])
def test_a_stale_buy_is_released_whether_the_cancel_matched_anything_or_not(monkeypatch,
                                                                            cancelled):
    """2026-08-04 P1 의 의도된 완화 — 취소 0건과 취소 ACK 를 똑같이 최종성으로 읽는다."""
    async def scenario():
        f = legacy(monkeypatch, broker=Broker(cancelled=cancelled))
        rm = f['rm']
        stale(rm, SYM, side=OrderSide.BUY, now=ENGINE_NOW, seconds=700, quantity=47)
        rm._reserved_by_order[SYM] = PRICE * 47 * D('1.015')
        rm._pending_signal_cache[SYM] = {'strategy': 'sepa_trend'}
        rm._pending_strategy[SYM] = 'sepa_trend'
        await drive(f['engine'], buy(OTHER))
        assert kinds(f['broker']) == ['cancel']
        # 예약·전략·시그널 캐시까지 전부 풀린다 — 취소 0건도 ACK 와 똑같이 읽는다.
        assert held_in(rm, SYM) == set()
    asyncio.run(scenario())


def test_a_cancel_exception_keeps_the_stale_buy_pending(monkeypatch):
    """API 예외만 유지-재시도다."""
    async def scenario():
        f = legacy(monkeypatch, broker=Broker(cancelled=RuntimeError('합성 취소 장애')))
        rm = f['rm']
        stale(rm, SYM, side=OrderSide.BUY, now=ENGINE_NOW, seconds=700, quantity=47)
        rm._reserved_by_order[SYM] = PRICE * 47 * D('1.015')
        await drive(f['engine'], buy(OTHER))
        assert kinds(f['broker']) == ['cancel']
        assert SYM in rm._pending_orders
        assert rm._reserved_by_order[SYM] == PRICE * 47 * D('1.015')
    asyncio.run(scenario())


@pytest.mark.parametrize('broker', ['absent', 'without_cancel'])
def test_without_a_usable_cancel_method_the_stale_buy_is_released_anyway(monkeypatch, broker):
    """취소를 한 번도 보내지 않아도 해제한다."""
    async def scenario():
        instance = None if broker == 'absent' else type('B', (), {})()
        f = legacy(monkeypatch, broker=instance)
        rm = f['rm']
        stale(rm, SYM, side=OrderSide.BUY, now=ENGINE_NOW, seconds=700, quantity=47)
        rm._reserved_by_order[SYM] = PRICE * 47 * D('1.015')
        await drive(f['engine'], buy(OTHER))
        assert held_in(rm, SYM) == set()
    asyncio.run(scenario())


# ── eviction ────────────────────────────────────────────────────────────

def test_a_full_book_emits_a_replacement_sell_for_the_weakest_position(monkeypatch):
    """정렬은 (진입 점수, 손익률) 오름차순 — 점수가 같으면 손실이 큰 쪽이 희생자다."""
    async def scenario():
        f = legacy(monkeypatch)
        full_book(f['rm'])
        # 수량은 작게 — 전략 예산 게이트가 `_risk_validator` 보다 앞이라 만석을 못 태운다.
        hold(f, WEAK, quantity=5, price=D('9900'), score=50)    # 같은 점수, 손실 -1%
        hold(f, WEAKER, quantity=5, price=D('9000'), score=50)  # 같은 점수, 손실 -10%
        hold(f, OTHER, quantity=5, price=D('8000'), score=70)   # 손실은 더 크지만 점수가 높다
        await drive(f['engine'], buy(SYM, score=99.0))
        emitted = sells(f['engine'])
        assert len(emitted) == 1
        event = emitted[0]
        assert (event.symbol, event.source) == (WEAKER, 'replacement')
        assert event.metadata['quantity'] == 5
        assert event.metadata['source'] == 'replacement'
        assert event.metadata['exit_action'] == 'replacement_exit'
        # 원 BUY 는 같은 사이클에 G3_risk 로 거부되고 쿨다운도 무장되지 않는다.
        assert [event for event in f['engine']._event_queue
                if type(event) is OrderEvent] == []
        assert SYM not in f['rm']._last_signal_time
        assert f['rm']._REPLACEMENT_LAST_EVICT_TS == {WEAKER: ENGINE_NOW}
    asyncio.run(scenario())


@pytest.mark.parametrize('excluded', ['core', 'pending', 'exit_exempt', 'winner', 'cooldown'])
def test_the_protected_positions_are_never_evicted(monkeypatch, excluded):
    """유일한 후보가 제외 대상이면 축출 SELL 은 한 건도 발행되지 않는다."""
    async def scenario():
        f = legacy(monkeypatch)
        rm = f['rm']
        full_book(rm)
        strategy = 'core_holding' if excluded == 'core' else 'sepa_trend'
        price = PRICE + 1 if excluded == 'winner' else D('9000')
        hold(f, WEAK, quantity=5, price=price, strategy=strategy, score=10)
        if excluded == 'pending':
            rm._pending_orders.add(WEAK)
        if excluded == 'exit_exempt':
            rm._exit_exempt_ref = {WEAK}
        if excluded == 'cooldown':
            rm._REPLACEMENT_LAST_EVICT_TS[WEAK] = ENGINE_NOW - timedelta(seconds=599)
        await drive(f['engine'], buy(SYM, score=99.0))
        assert sells(f['engine']) == []
    asyncio.run(scenario())


def test_an_edge_below_five_points_skips_the_replacement(monkeypatch):
    """신규 점수가 희생 후보 +5 미만이면 축출하지 않는다."""
    async def scenario():
        f = legacy(monkeypatch)
        full_book(f['rm'])
        hold(f, WEAK, quantity=5, price=D('9000'), score=99)
        await drive(f['engine'], buy(SYM, score=99.0))
        assert sells(f['engine']) == []
        assert f['rm']._REPLACEMENT_LAST_EVICT_TS == {}
    asyncio.run(scenario())


def test_two_high_score_buys_in_one_batch_evict_two_different_victims(monkeypatch):
    """현행 결함(사실 9): 종목별 600초 쿨다운뿐이라 연쇄 축출이 닫혀 있지 않다.

    축출 SELL 은 같은 priority 의 뒤에 실리므로 **이미 큐에 있던 BUY#2 뒤**에 처리된다 —
    BUY#2 평가 시점에 첫 희생자만 쿨다운이라 두 번째 희생자가 연달아 축출된다.
    """
    async def scenario():
        f = legacy(monkeypatch)
        engine = f['engine']
        full_book(f['rm'])
        hold(f, WEAK, quantity=5, price=D('9900'), score=50)
        hold(f, WEAKER, quantity=5, price=D('9000'), score=50)
        await engine.emit(buy(SYM, score=99.0))
        await engine.emit(buy(OTHER, score=99.0))
        for expected in (SYM, OTHER):
            popped = await engine._get_next_event()
            assert popped.symbol == expected
            await engine._process_event(popped)
        assert [event.symbol for event in sells(engine)] == [WEAKER, WEAK]
        assert set(f['rm']._REPLACEMENT_LAST_EVICT_TS) == {WEAK, WEAKER}
    asyncio.run(scenario())
