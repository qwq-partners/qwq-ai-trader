"""S4-2: 만석 시 교체(eviction)가 attach 에서 owner 의 한 길로 나간다.

**attach 에서 실제 SELL POST 를 새로 여는 유일한 단계다**(계획 §4 "S4-2"·결정 ⑥⑦⑧⑨).
세 hunk 를 고정한다 — H5(호출부의 attach 차단 해제) · H9(후보 제외 집합이 attach 에서는
owner 의 미해결 종목) · H10(attach 에서만 쿨다운 안 전역 1건 상한). 보호 가드(exit_exempt·
core_holding·승자·+5점)는 한 줄도 바뀌지 않았다는 것을 attach 경로에서 다시 고정한다.

**S4 는 설치가 아니다** — 제품에 `attach()`·`install_gateway()` 호출자는 0건이고 송신은
합성 fake HTTP·주입 시계·시험용 합성 startup 허가 위의 결과다. attach 모드에는 계속
미체결 SELL 의 시장가 에스컬레이션도 미체결 BUY 의 타임아웃 취소도 없다(§0).

legacy(runtime 없음)의 현행 동작은 `test_engine_legacy_stale_eviction_characterization.py`
가 정본이다 — 여기서는 H9 가 같은 장부 객체를 본다는 것과 H10 이 legacy 에 걸리지 않는다는
두 축만 대조한다.

실행: venv/bin/python -m pytest tests/test_execution_signal_gateway_eviction.py -q
"""
import asyncio
from datetime import timedelta
from decimal import Decimal as D
from pathlib import Path

import pytest

from src.core.engine import RiskManager, UnifiedEngine
from src.core.event import ErrorEvent, OrderEvent, SignalEvent
from src.core.types import OrderSide, OrderType, TradingConfig

from test_cross_validator_characterization import freeze  # noqa: F401 — pytest fixture 재사용
from test_engine_legacy_stale_eviction_characterization import full_book, sells
from test_execution_qualification_publishers import buy, cv
from test_execution_risk_policy import NOW
from test_execution_signal_gateway import reservations
from test_execution_signal_gateway_wiring import (
    ENGINE_NOW, drive, engine_clock, posts, risk_manager, teardown, wired,
)
from test_risk_sizing import PRICE, SYM


@pytest.fixture(autouse=True)
def synthetic_home(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)


WEAK = '035420'       # 진입 점수 낮은 손실 포지션
WEAKER = '051910'     # 같은 점수에 손실이 더 큰 포지션 — 정렬 1순위 희생자
OTHER = '000660'      # 두 번째 고득점 BUY 의 종목(보유하지 않는다)
# (종목, 수량, 현재가, 전략, 진입 점수) — 수량은 작게: 전략 예산 게이트가 `_risk_validator`
# 보다 앞이라 큰 포지션은 만석 경로에 닿기 전에 막힌다(S4-0 의 구동 전제).
BOOK = ((WEAK, 5, D('9900'), 'sepa_trend', 50), (WEAKER, 5, D('9000'), 'sepa_trend', 50))


class Recorder(set):
    """`__contains__` 호출을 기록하는 legacy 장부 — H9 가 어느 집합을 보는지 관측한다."""

    def __init__(self, *args):
        super().__init__(*args)
        self.asked = []

    def __contains__(self, item):
        self.asked.append(item)
        return super().__contains__(item)


def evictable(rm):
    """`_rm` 은 `object.__new__` 라 __init__ 의 교체 상태가 없다 — 제품 기본값으로 채운다."""
    full_book(rm)
    rm._REPLACEMENT_LAST_EVICT_TS = {}
    rm._REPLACEMENT_COOLDOWN_SEC = 600
    rm._exit_exempt_ref = set()
    return rm


async def stocked(f, rows=BOOK):
    """owner state 에 포지션·보호를 심고 게시로 engine.portfolio 까지 내린다(legacy writer 0)."""
    from src.core.types import Position, PositionSide
    from src.execution.safety.economics import decode_portfolio, encode_portfolio
    from src.execution.safety.protection import decode_protection, encode_protection

    def reduce(state):
        pf = decode_portfolio(state['portfolio'])
        exits = decode_protection(state['protection'], clock=lambda: NOW)
        for symbol, quantity, price, strategy, score in rows:
            position = Position(symbol, side=PositionSide.LONG, quantity=quantity,
                                avg_price=PRICE, current_price=price, strategy=strategy,
                                entry_time=NOW)
            position.entry_signal_score = score
            pf.positions[symbol] = position
            exits.register_position(position)
        state['portfolio'], state['protection'] = encode_portfolio(pf), encode_protection(exits)
        return state

    await f['runtime'].owner.mutate('synthetic-holdings', reduce)


async def full(tmp_path, monkeypatch, freeze_clock, rows=BOOK):
    """attach + gateway + 만석 `_risk_validator` + 보유 포지션까지 끝난 엔진."""
    f = await wired(tmp_path, monkeypatch, freeze_clock)
    evictable(f['rm'])
    f['rm']._pending_orders = Recorder()
    await stocked(f, rows)
    return f


async def pump(engine, limit=4):
    """BUY 처리 중에 큐에 실린 축출 SELL 을 차례로 꺼내 처리한다(`drive` 는 못 쓴다).

    `drive` 는 "emit 한 그 이벤트가 곧 다음 pop"을 단언하므로 재진입 발행에는 쓸 수 없다.
    """
    seen = []
    while len(engine._event_queue) > 0 and len(seen) < limit:
        event = await engine._get_next_event()
        seen.append(event)
        await engine._process_event(event)
    return seen


def attempts(f):
    return list(f['runtime'].owner.state['attempts'].values())


def sold(f):
    """owner 에 남은 SELL attempt 의 (종목, 수량, 송신 상태)."""
    return [(row['symbol'], row['quantity'], row['command_status'])
            for row in attempts(f) if row['side'] == 'sell']


def renumber(f, order_no):
    """합성 세션은 같은 ODNO 를 무한히 돌려준다 — 두 번째 송신은 다른 주문번호를 받게 한다."""
    from test_kr_final_dispatch import Response
    f['broker']._session.response = Response(data={'rt_cd': '0', 'output': {
        'ODNO': order_no, 'KRX_FWDG_ORD_ORGNO': '12345'}})


def errors(engine):
    return [event for event in engine._event_queue if type(event) is ErrorEvent]


def direct(f, monkeypatch):
    """legacy 직접 송신 경로(브로커 메서드)를 감시한다 — attach 에서는 0 이어야 한다."""
    calls = []
    for name in ('submit_order', 'cancel_all_for_symbol'):
        async def record(*args, _name=name, **kwargs):
            calls.append(_name)
            return True
        monkeypatch.setattr(f['broker'], name, record, raising=False)
    return calls


# ── 핵심: 축출 SELL 이 owner 의 한 길로 나간다 ──────────────────────────

def test_a_full_book_evicts_the_weakest_position_through_the_owner_path(tmp_path, monkeypatch,
                                                                        freeze):
    """attach 의 만석 BUY 는 최약 포지션 SELL 을 owner 의 한 길로 정확히 1건 내보낸다 — 브로커 직접 호출 0."""
    async def scenario():
        f = await full(tmp_path, monkeypatch, freeze)
        try:
            calls = direct(f, monkeypatch)
            await drive(f['engine'], buy(SYM, score=99.0))
            emitted = sells(f['engine'])
            assert len(emitted) == 1
            assert (emitted[0].symbol, emitted[0].source) == (WEAKER, 'replacement')
            assert emitted[0].metadata['quantity'] == 5
            # attach 에서 legacy 장부를 본 곳은 on_signal 의 중복 검사뿐이다 — 축출 루프는 0.
            assert f['rm']._pending_orders.asked == [SYM]
            await pump(f['engine'])
            assert len(posts(f)) == 1
            assert sold(f) == [(WEAKER, 5, 'acknowledged')]
            # 새 송신 경로를 만들지 않는다 — 브로커 직접 호출은 한 건도 없다.
            assert calls == []
            assert f['rm']._REPLACEMENT_LAST_EVICT_TS == {WEAKER: ENGINE_NOW}
        finally:
            await teardown(f)
    asyncio.run(scenario())


def test_the_original_buy_is_still_refused_in_the_same_cycle(tmp_path, monkeypatch, freeze):
    """축출이 성립해도 원 BUY 는 그 사이클에 G3_risk 거부다 — POST 는 SELL 1건뿐."""
    async def scenario():
        f = await full(tmp_path, monkeypatch, freeze)
        try:
            await drive(f['engine'], buy(SYM, score=99.0))
            await pump(f['engine'])
            assert len(posts(f)) == 1
            assert [row['symbol'] for row in attempts(f)] == [WEAKER]
            assert [event for event in f['engine']._event_queue
                    if type(event) is OrderEvent] == []
            assert SYM not in f['rm']._last_signal_time
        finally:
            await teardown(f)
    asyncio.run(scenario())


@pytest.mark.parametrize('protected', ['core', 'exit_exempt', 'winner'])
def test_the_protected_positions_are_never_evicted_in_attach_mode(tmp_path, monkeypatch, freeze,
                                                                  protected):
    """유일한 후보가 보호 대상이면 attach 에서도 SELL SIGNAL 0·POST 0 이다(결정 ⑧)."""
    async def scenario():
        strategy = 'core_holding' if protected == 'core' else 'sepa_trend'
        price = PRICE + 1 if protected == 'winner' else D('9000')
        f = await full(tmp_path, monkeypatch, freeze,
                       rows=((WEAK, 5, price, strategy, 10),))
        try:
            if protected == 'exit_exempt':
                f['rm']._exit_exempt_ref = {WEAK}
            await drive(f['engine'], buy(SYM, score=99.0))
            assert sells(f['engine']) == []
            await pump(f['engine'])
            assert posts(f) == [] and attempts(f) == []
        finally:
            await teardown(f)
    asyncio.run(scenario())


def test_an_edge_below_five_points_skips_the_replacement_in_attach_mode(tmp_path, monkeypatch,
                                                                        freeze):
    """신규 점수가 희생 후보 +5 미만이면 attach 에서도 축출하지 않는다."""
    async def scenario():
        f = await full(tmp_path, monkeypatch, freeze,
                       rows=((WEAK, 5, D('9000'), 'sepa_trend', 99),))
        try:
            await drive(f['engine'], buy(SYM, score=99.0))
            assert sells(f['engine']) == []
            await pump(f['engine'])
            assert posts(f) == [] and f['rm']._REPLACEMENT_LAST_EVICT_TS == {}
        finally:
            await teardown(f)
    asyncio.run(scenario())


# ── H9: 후보 제외 집합의 정본이 owner 다 ────────────────────────────────

def test_a_symbol_with_an_unresolved_owner_sell_is_not_a_candidate(tmp_path, monkeypatch, freeze):
    """legacy `_pending_orders` 로 두면 attach 에서 그 집합은 비어 있어 같은 희생자를 다시 고른다."""
    async def scenario():
        f = await full(tmp_path, monkeypatch, freeze)
        try:
            await drive(f['engine'], buy(SYM, score=99.0))
            await pump(f['engine'])
            assert sold(f) == [(WEAKER, 5, 'acknowledged')]

            # 전역 상한·종목 쿨다운·신호 쿨다운을 모두 지난 뒤의 두 번째 고득점 BUY.
            renumber(f, '1234567891')
            f['now'][0] = ENGINE_NOW + timedelta(seconds=601)
            await drive(f['engine'], buy(OTHER, score=99.0))
            await pump(f['engine'])
            # WEAKER 는 owner 미해결이라 후보에서 빠지고 차순위 WEAK 가 축출된다.
            assert sorted(sold(f)) == [(WEAK, 5, 'acknowledged'), (WEAKER, 5, 'acknowledged')]
            assert len(posts(f)) == 2
        finally:
            await teardown(f)
    asyncio.run(scenario())


def test_a_broken_owner_helper_ends_the_buy_signal_with_an_error(tmp_path, monkeypatch, freeze):
    """helper 는 fail-closed 다 — 예외를 흡수하면 축출만 건너뛴 채 SIGNAL 이 통과한다."""
    async def scenario():
        f = await full(tmp_path, monkeypatch, freeze)
        try:
            def broken():
                raise RuntimeError('synthetic-owner-unready')

            monkeypatch.setattr(f['gateway'], 'unresolved_symbols', broken)
            before = f['engine'].stats.errors_count
            await drive(f['engine'], buy(SYM, score=99.0))
            assert f['engine'].stats.errors_count == before + 1
            assert [event.message for event in errors(f['engine'])] == \
                   ['synthetic-owner-unready']
            assert sells(f['engine']) == []
            assert posts(f) == [] and attempts(f) == []
        finally:
            await teardown(f)
    asyncio.run(scenario())


# ── H10: 쿨다운 안에는 전역 1건 (결정 ⑦) ────────────────────────────────

def test_two_high_score_buys_in_one_batch_evict_only_one_victim(tmp_path, monkeypatch, freeze):
    """S4-0 은 legacy 가 서로 다른 희생자 둘을 연달아 축출하는 것을 고정한다 — attach 는 1건."""
    async def scenario():
        f = await full(tmp_path, monkeypatch, freeze)
        try:
            engine = f['engine']
            await engine.emit(buy(SYM, score=99.0))
            await engine.emit(buy(OTHER, score=99.0))
            for expected in (SYM, OTHER):
                popped = await engine._get_next_event()
                assert popped.symbol == expected
                await engine._process_event(popped)
            await pump(engine)
            assert sold(f) == [(WEAKER, 5, 'acknowledged')]
            assert len(posts(f)) == 1
            assert set(f['rm']._REPLACEMENT_LAST_EVICT_TS) == {WEAKER}
        finally:
            await teardown(f)
    asyncio.run(scenario())


@pytest.mark.parametrize('elapsed, evicts', [(599, False), (600, True)])
def test_the_global_cap_reuses_the_existing_cooldown_threshold(tmp_path, monkeypatch, freeze,
                                                               elapsed, evicts):
    """상한 임계는 기존 `_REPLACEMENT_COOLDOWN_SEC` 다 — 새 상수를 만들지 않는다.

    기록은 보유하지 않은 종목의 것이다(종목별 쿨다운으로는 이 후보를 막을 수 없다).
    """
    async def scenario():
        f = await full(tmp_path, monkeypatch, freeze)
        try:
            f['rm']._REPLACEMENT_LAST_EVICT_TS[OTHER] = ENGINE_NOW - timedelta(seconds=elapsed)
            await drive(f['engine'], buy(SYM, score=99.0))
            await pump(f['engine'])
            assert len(posts(f)) == (1 if evicts else 0)
            assert sold(f) == ([(WEAKER, 5, 'acknowledged')] if evicts else [])
        finally:
            await teardown(f)
    asyncio.run(scenario())


# ── owner 의 최종 방어선 ─────────────────────────────────────────────────

def test_a_forced_second_eviction_sell_is_refused_by_the_owner(tmp_path, monkeypatch, freeze):
    """상한을 넘겨 같은 종목의 SELL 을 강제로 구동해도 owner 가 사유 코드로 끝낸다."""
    async def scenario():
        f = await full(tmp_path, monkeypatch, freeze)
        try:
            await drive(f['engine'], buy(SYM, score=99.0))
            processed = await pump(f['engine'])
            sell_event = next(event for event in processed if type(event) is SignalEvent
                              and event.side == OrderSide.SELL)
            assert len(posts(f)) == 1

            f['now'][0] = ENGINE_NOW + timedelta(seconds=31)
            before = f['engine'].stats.errors_count
            await drive(f['engine'], sell_event)
            assert len(posts(f)) == 1
            assert f['engine'].stats.errors_count == before + 1
            assert 'unresolved_symbol_attempt' in errors(f['engine'])[0].message
            assert sold(f) == [(WEAKER, 5, 'acknowledged')]
        finally:
            await teardown(f)
    asyncio.run(scenario())


def test_the_replacement_metadata_source_is_not_an_authority(tmp_path, monkeypatch, freeze):
    """`metadata['source']` 는 소비자 0건이다 — 지우거나 바꿔도 송신 가능/불가가 안 바뀐다."""
    async def scenario():
        f = await full(tmp_path, monkeypatch, freeze)
        try:
            await drive(f['engine'], buy(SYM, score=99.0))
            event = sells(f['engine'])[0]
            event.metadata.pop('source')
            event.metadata['exit_action'] = 'synthetic-unknown-action'
            event.source = 'synthetic-unknown-source'
            await pump(f['engine'])
            assert len(posts(f)) == 1
            assert sold(f) == [(WEAKER, 5, 'acknowledged')]
        finally:
            await teardown(f)
    asyncio.run(scenario())


def test_without_the_synthetic_permit_the_eviction_sell_is_not_sent(tmp_path, monkeypatch, freeze):
    """합성 허가가 없으면 SELL SIGNAL 은 발행되지만 POST 0·예약 0 이다."""
    async def scenario():
        f = await full(tmp_path, monkeypatch, freeze)
        try:
            f['ready'][0] = False
            await drive(f['engine'], buy(SYM, score=99.0))
            assert [event.symbol for event in sells(f['engine'])] == [WEAKER]
            await pump(f['engine'])
            assert posts(f) == []
            row = attempts(f)[0]
            assert (row['symbol'], row['state']) == (WEAKER, 'final_rejected')
            assert reservations(row) == (0, D('0'), D('0'), None)
        finally:
            await teardown(f)
    asyncio.run(scenario())


# ── legacy 불변 (§5 의 행동 대조) ───────────────────────────────────────

def legacy(monkeypatch):
    """runtime 없는 실제 엔진 + 실제 inner RiskManager. 보유는 legacy 장부에 직접 심는다."""
    from src.core.types import Position
    engine = UnifiedEngine(TradingConfig(initial_capital=D('2000000')))
    rm = evictable(risk_manager(monkeypatch, engine, validator=cv()))
    rm._pending_orders = Recorder()
    now = engine_clock(monkeypatch)
    for symbol, quantity, price, strategy, score in BOOK:
        position = Position(symbol=symbol, quantity=quantity, avg_price=PRICE,
                            current_price=price, strategy=strategy)
        position.entry_signal_score = score
        engine.portfolio.positions[symbol] = position
    assert engine._execution_runtime is None
    return {'engine': engine, 'rm': rm, 'now': now}


def test_the_legacy_path_still_reads_its_own_pending_ledger(tmp_path, monkeypatch, freeze):
    """H9 의 legacy 분기는 `self._pending_orders` **그 객체 그대로**를 본다."""
    async def scenario():
        f = legacy(monkeypatch)
        await drive(f['engine'], buy(SYM, score=99.0))
        # on_signal 의 중복 검사(SYM) 뒤에 축출 루프가 같은 객체로 후보를 걸렀다.
        assert f['rm']._pending_orders.asked == [SYM, WEAK, WEAKER]
        assert [event.symbol for event in sells(f['engine'])] == [WEAKER]
    asyncio.run(scenario())


def test_the_global_cap_does_not_apply_to_the_legacy_path(tmp_path, monkeypatch, freeze):
    """H10 은 attach 전용이다 — 쿨다운 안 기록이 있어도 legacy 는 그대로 축출한다."""
    async def scenario():
        f = legacy(monkeypatch)
        f['rm']._REPLACEMENT_LAST_EVICT_TS[OTHER] = ENGINE_NOW - timedelta(seconds=10)
        await drive(f['engine'], buy(SYM, score=99.0))
        assert [event.symbol for event in sells(f['engine'])] == [WEAKER]
    asyncio.run(scenario())


def test_a_partially_constructed_risk_manager_still_returns_none(tmp_path, monkeypatch, freeze):
    """`engine` 속성 없는 부분 생성 인스턴스에서 새 helper 가 종전 값으로 떨어진다(S3 wave 5)."""
    async def scenario():
        rm = object.__new__(RiskManager)
        assert await rm._try_evict_weakest_position(
            new_symbol=SYM, new_score=99.0, new_reason='합성') is None
    asyncio.run(scenario())


# ── 독립 재현이 찾은 공백 (S4 wave 2) ─────────────────────────────────────

def test_the_owner_helper_itself_refuses_a_state_it_cannot_read(tmp_path, monkeypatch, freeze):
    """`unresolved_symbols()` 자신이 fail-closed 다 — 흡수해 빈 집합을 돌려주면 미해결 SELL 이
    있는 종목이 다시 축출 후보가 된다(위 시험은 helper 를 스텁으로 갈아끼워 engine 쪽 전파만 본다).
    """
    async def scenario():
        f = await full(tmp_path, monkeypatch, freeze)
        try:
            assert f['gateway'].unresolved_symbols() == frozenset()
            f['runtime'].owner._healthy = False
            try:
                with pytest.raises(Exception, match='store_or_publication_unhealthy'):
                    f['gateway'].unresolved_symbols()
            finally:
                f['runtime'].owner._healthy = True
        finally:
            await teardown(f)
    asyncio.run(scenario())


@pytest.mark.parametrize('victim_score, evicted', [(81, True), (82, False)])
def test_the_five_point_edge_is_a_boundary_in_attach_mode(tmp_path, monkeypatch, freeze,
                                                          victim_score, evicted):
    """attach 하네스의 CV 는 99 점을 86 점으로 깎는다 — 실효 점수 기준으로 +5 경계 양쪽을 고정한다.

    86 >= 81 + 5 는 축출, 86 < 82 + 5 는 스킵. 여유가 큰 표본만으로는 +1~+4 로의 완화가 안 보인다.
    """
    async def scenario():
        f = await full(tmp_path, monkeypatch, freeze,
                       rows=((WEAK, 5, D('9000'), 'sepa_trend', victim_score),))
        try:
            await drive(f['engine'], buy(SYM, score=99.0))
            assert [event.symbol for event in sells(f['engine'])] == ([WEAK] if evicted else [])
        finally:
            await teardown(f)
    asyncio.run(scenario())
