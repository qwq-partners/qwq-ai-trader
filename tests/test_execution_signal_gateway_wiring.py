"""S3-6a: 실제 큐의 SIGNAL 이 owner 의 단일 송신로에 닿는 접합(H1·H4·H5·⑮).

S2·S3-5 의 시험은 on_signal 결과를 helper 로 만들어 gateway 에 넣었다 — 여기서는 실제
`UnifiedEngine` 큐에서 `_get_next_event()`→`_process_event()` 로 구동한다. owner 는 실제
SQLite·실제 RegimeOwner 이고, 증거는 실제 `CrossStrategyValidator` 와 실제 사이징이 만든다.
전송은 합성 fake HTTP·주입 시계·시험용 합성 startup 허가 위의 결과다.

**S3 는 설치가 아니다** — 제품에 `KRExecutionRuntime` 을 만들거나 `attach()`·
`install_gateway()` 를 부르는 코드는 0건이고 제품의 `trading_ready` 는 계속 False 다.

**이 파일이 태우지 않는 것(재사용한 `test_t11_entry_plan._order_env` 의 스텁):** legacy 세션
(`is_trading_hours`/`_get_current_session`)뿐 아니라 `engine.can_open_position`(항상 통과)·
`_risk_validator`(None)·`_sector_lookup`(None)도 스텁이다. 그래서 통과 경로에서 실제 일일
한도·섹터·포지션 크기·현금 게이트는 돌지 않고 `_pending_sector_map` 의 자연 writer 도 실행되지
않는다(⑮ 는 수동 seed 로 고정). 실제 게이트 통과 경로는 S3-6b 의 parity 시험이 처음 태운다.

시계는 넷을 같은 순간으로 맞춘다: legacy 세션(스텁 — 실제 `KRSession` 표는 S3-6b)·engine
모듈의 naive 시계(여기서 동결)·CV 모듈 시계(`freeze`)·runtime 주입 시계(`f['clock']`).

실행: venv/bin/python -m pytest tests/test_execution_signal_gateway_wiring.py -q -p no:cacheprovider
"""
import asyncio
from datetime import datetime, timedelta
from decimal import Decimal as D
from pathlib import Path

import pytest

from src.core.engine import UnifiedEngine
from src.core.event import ErrorEvent, EventType, FillEvent, OrderEvent
from src.core.types import Order, OrderSide, OrderType, TradingConfig
from src.execution.safety.lifecycle import CommandStatus

from test_cross_validator_characterization import freeze  # noqa: F401 — pytest fixture 재사용
from test_execution_qualification_publishers import SECTOR, buy, cv
from test_execution_signal_gateway import (
    facts_rows, fixture as gateway_fixture, only_attempt, reservations,
)
from test_risk_sizing import PRICE, SYM, _em, _rm
from test_t11_entry_plan import _order_env


@pytest.fixture(autouse=True)
def synthetic_home(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)


# 네 시계를 맞춘 같은 순간(`test_execution_risk_policy.NOW` 의 naive 표현).
ENGINE_NOW = datetime(2026, 9, 18, 11, 0)
# 고정 fixture 경제(자산 200만·가격 1만·기본 25%)에서 kernel 이 정당화하는 수량.
JUSTIFIED = 47
LEDGERS = ('_pending_orders', '_pending_quantities', '_pending_timestamps', '_pending_sides',
           '_reserved_by_order', '_pending_strategy', '_pending_signal_cache')


def engine_clock(monkeypatch):
    """engine 모듈의 naive 시계를 동결한다 — 쿨다운·pending 타임스탬프의 유일한 출처다.

    **CV 시계는 여기서 닿지 않는다.** `cross_validator.validate` 는 함수 안에서
    `from datetime import datetime as _dt` 로 다시 import 하므로 모듈 patch 밖이다.
    legacy `on_signal` 을 태우는 시험은 `freeze`(test_cross_validator_characterization)
    를 **같은 순간**으로 함께 불러야 한다 — 빠뜨리면 판정이 실제 벽시계를 탄다
    (09:00~09:29 매수 전량 차단 · 09:30~10:30 -8 · 12:30~13:00 +5). `wired()` 는 이미
    그 쌍을 지킨다.
    """
    import src.core.engine as eng
    holder = [ENGINE_NOW]

    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return holder[0] if tz is None else holder[0].replace(tzinfo=tz)

    monkeypatch.setattr(eng, 'datetime', Frozen)
    return holder


def risk_manager(monkeypatch, engine, *, validator=None, regime='bull'):
    """실제 inner RiskManager 를 실제 엔진에 붙인다(SIGNAL 핸들러 등록 포함)."""
    rm = _rm(monkeypatch, mode='nominal', em=_em())
    rm.engine = engine
    _order_env(monkeypatch, rm)
    if validator is not None:
        rm._cross_validator = validator
    rm._PENDING_TIMEOUT_SECONDS = 600
    engine._market_regime = regime
    engine.risk_manager = rm
    engine.register_handler(EventType.SIGNAL, rm.on_signal)
    return rm


async def wired(tmp_path, monkeypatch, freeze_clock, **changes):
    """attach + gateway 설치가 끝난 실제 엔진. SIGNAL emit 은 이 뒤에만 일어난다."""
    freeze_clock(11, 0, day=18)
    f = await gateway_fixture(tmp_path, monkeypatch, **changes)
    f['rm'] = risk_manager(monkeypatch, f['engine'], validator=cv())
    f['now'] = engine_clock(monkeypatch)
    return f


def spy(f):
    """게시·prepare·dispatch·submit 의 호출 사실만 센다(원 동작은 그대로 통과시킨다)."""
    calls = {}

    def watch(owner, name, key):
        original = getattr(owner, name)
        calls[key] = []

        async def record(*args, **kwargs):
            calls[key].append(args)
            return await original(*args, **kwargs)

        setattr(owner, name, record)

    watch(f['commands'], 'publish_decision_facts', 'publish')
    watch(f['commands'], 'prepare', 'prepare')
    watch(f['commands'], 'dispatch', 'dispatch')
    watch(f['gateway'], 'submit', 'submit')
    return calls


async def drive(engine, event):
    """실큐 한 바퀴 — emit 한 이벤트를 큐에서 다시 꺼내 처리한다(핸들러를 직접 부르지 않는다)."""
    await engine.emit(event)
    popped = await engine._get_next_event()
    assert popped is event
    await engine._process_event(popped)
    return popped


def queued_types(engine):
    return [event.type for event in engine._event_queue]


def posts(f):
    return f['broker']._session.posts


def ledgers(rm):
    return {name: getattr(rm, name) for name in LEDGERS}


async def teardown(f):
    await f['runtime'].shutdown()
    await f['store'].close()


# ── 핵심: 실큐의 SIGNAL 이 한 길로 송신된다 ──────────────────────────────

def test_a_buy_signal_from_the_real_queue_reaches_the_broker_once(tmp_path, monkeypatch, freeze):
    """오늘은 527-533 이 SIGNAL 을 폐기해 POST 0·errors_count +1 이다."""
    async def scenario():
        f = await wired(tmp_path, monkeypatch, freeze)
        try:
            calls = spy(f)
            before = f['engine'].stats.errors_count
            await drive(f['engine'], buy(SYM))
            assert len(posts(f)) == 1
            attempt = only_attempt(f)
            assert attempt['command_status'] == 'acknowledged'
            assert (attempt['quantity'], attempt['sector']) == (JUSTIFIED, SECTOR)
            assert facts_rows(f)[attempt['intent_id']]['symbol'] == SYM
            assert [len(calls[key]) for key in ('submit', 'publish', 'prepare', 'dispatch')] == [1] * 4
            assert f['engine'].stats.errors_count == before
            # 결정 ③: 반환 OrderEvent 를 큐로 되돌리지 않는다(되돌리면 같은 분기가 폐기한다).
            assert queued_types(f['engine']) == []
        finally:
            await teardown(f)
    asyncio.run(scenario())


def test_a_sell_signal_from_the_real_queue_reaches_the_broker_once(tmp_path, monkeypatch, freeze):
    """청산은 증거를 요구하지 않는다 — 판단 사실 0건으로 POST 1."""
    async def scenario():
        f = await wired(tmp_path, monkeypatch, freeze)
        try:
            from test_execution_command_owner import held
            await held(f)
            f['engine'].portfolio.positions.update(f['runtime'].engine.portfolio.positions)
            event = buy(SYM)
            event.side = OrderSide.SELL
            event.signal.side = OrderSide.SELL
            await drive(f['engine'], event)
            assert len(posts(f)) == 1
            attempt = only_attempt(f)
            assert (attempt['side'], attempt['command_status']) == ('sell', 'acknowledged')
            assert facts_rows(f) == {}
        finally:
            await teardown(f)
    asyncio.run(scenario())


# ── 회귀: 나머지 legacy 거래 이벤트는 그대로 거부된다 ────────────────────

@pytest.mark.parametrize('kind', ['fill', 'order'])
def test_fill_and_order_events_are_still_refused(tmp_path, monkeypatch, freeze, kind):
    async def scenario():
        f = await wired(tmp_path, monkeypatch, freeze)
        try:
            seen = []
            for event_type in (EventType.FILL, EventType.ORDER):
                f['engine']._handlers[event_type] = [lambda event: seen.append(event)]
            event = (FillEvent(symbol=SYM, side=OrderSide.BUY, quantity=1, price=PRICE)
                     if kind == 'fill' else
                     OrderEvent.from_order(Order(symbol=SYM, side=OrderSide.BUY,
                                                 order_type=OrderType.MARKET, quantity=1)))
            before = f['engine'].stats.errors_count
            await drive(f['engine'], event)
            assert f['engine'].stats.errors_count == before + 1
            assert seen == [] and posts(f) == []
        finally:
            await teardown(f)
    asyncio.run(scenario())


def test_a_signal_is_discarded_when_no_gateway_is_installed(tmp_path, monkeypatch, freeze):
    """gateway 미설치 attach 는 fail-closed 유지 — 후보 판단조차 돌지 않는다."""
    async def scenario():
        from test_execution_regime_recheck import fixture as regime_fixture
        freeze(11, 0, day=18)
        f = await regime_fixture(tmp_path, monkeypatch)
        f['engine'].broker = f['broker']
        engine_clock(monkeypatch)
        rm = risk_manager(monkeypatch, f['engine'], validator=cv())
        try:
            assert f['runtime'].gateway is None
            before = f['engine'].stats.errors_count
            await drive(f['engine'], buy(SYM))
            assert f['engine'].stats.errors_count == before + 1
            assert getattr(rm, '_last_qualification_evidence', 'absent') == 'absent'
            assert posts(f) == [] and f['runtime'].owner.state['attempts'] == {}
        finally:
            await teardown(f)
    asyncio.run(scenario())


# ── 결정 ②: 인계점의 예외는 엔진 루프를 내리지 않는다 ────────────────────

@pytest.mark.parametrize('failure', ['gateway', 'candidate'])
def test_a_failure_at_the_handoff_is_absorbed_like_the_handler_loop(tmp_path, monkeypatch,
                                                                    freeze, failure):
    """인계점은 핸들러 루프 밖이다 — 여기서 새는 예외 한 건이 run 루프를 끝낸다(사실 2)."""
    async def scenario():
        f = await wired(tmp_path, monkeypatch, freeze)
        try:
            async def broken(*args, **kwargs):
                raise RuntimeError('synthetic-handoff-failure')

            target, name = ((f['gateway'], 'submit') if failure == 'gateway'
                            else (f['rm'], 'on_signal'))
            original = getattr(target, name)
            setattr(target, name, broken)
            before = f['engine'].stats.errors_count
            await drive(f['engine'], buy(SYM))
            assert f['engine'].stats.errors_count == before + 1
            errors = [event for event in f['engine']._event_queue if type(event) is ErrorEvent]
            assert len(errors) == 1 and errors[0].message == 'synthetic-handoff-failure'
            assert errors[0].recoverable and posts(f) == []

            # 루프가 살아 있다 — 다음 SIGNAL 은 정상적으로 송신된다.
            setattr(target, name, original)
            f['engine']._event_queue.clear()
            f['now'][0] = ENGINE_NOW + timedelta(seconds=31)
            await drive(f['engine'], buy(SYM))
            assert len(posts(f)) == 1
        finally:
            await teardown(f)
    asyncio.run(scenario())


def test_a_cancellation_at_the_handoff_is_not_swallowed(tmp_path, monkeypatch, freeze):
    """종료 신호를 '핸들러 오류'로 삼키면 엔진이 멈추지 않는다."""
    async def scenario():
        f = await wired(tmp_path, monkeypatch, freeze)
        try:
            async def cancelled(*args, **kwargs):
                raise asyncio.CancelledError()

            monkeypatch.setattr(f['gateway'], 'submit', cancelled)
            before = f['engine'].stats.errors_count
            with pytest.raises(asyncio.CancelledError):
                await drive(f['engine'], buy(SYM))
            assert f['engine'].stats.errors_count == before
            assert f['engine']._pending_sector_map == {}
        finally:
            await teardown(f)
    asyncio.run(scenario())


# ── 결정 ④: 이중 장부 없이 쿨다운만 남는다 ──────────────────────────────

def test_the_signal_cooldown_is_armed_without_the_legacy_ledgers(tmp_path, monkeypatch, freeze):
    """`_last_signal_time` 을 지우면 성공 송신 뒤 30초 쿨다운이 영원히 무장되지 않는다."""
    async def scenario():
        f = await wired(tmp_path, monkeypatch, freeze)
        try:
            rm, calls = f['rm'], spy(f)
            await drive(f['engine'], buy(SYM))
            assert len(posts(f)) == 1
            assert ledgers(rm) == {'_pending_orders': set(), '_pending_quantities': {},
                                   '_pending_timestamps': {}, '_pending_sides': {},
                                   '_reserved_by_order': {}, '_pending_strategy': {},
                                   '_pending_signal_cache': {}}
            assert rm._last_signal_time[SYM] == ENGINE_NOW

            # 10초 뒤 같은 종목: 쿨다운이 owner 에 닿기 전에 막는다.
            sent = {key: len(value) for key, value in calls.items()}
            assert sent == {'submit': 1, 'publish': 1, 'prepare': 1, 'dispatch': 1}
            f['now'][0] = ENGINE_NOW + timedelta(seconds=10)
            await drive(f['engine'], buy(SYM))
            assert {key: len(value) for key, value in calls.items()} == sent
            assert len(posts(f)) == 1
        finally:
            await teardown(f)
    asyncio.run(scenario())


def test_after_the_cooldown_the_owner_refuses_the_duplicate(tmp_path, monkeypatch, freeze):
    """attach 에서 legacy 중복 재검사(2376-2379)는 항상 비어 통과한다 — 실제로 막는 것은 owner 다."""
    async def scenario():
        f = await wired(tmp_path, monkeypatch, freeze)
        try:
            await drive(f['engine'], buy(SYM))
            assert len(posts(f)) == 1
            f['now'][0] = ENGINE_NOW + timedelta(seconds=31)
            before = f['engine'].stats.errors_count
            await drive(f['engine'], buy(SYM))
            assert len(posts(f)) == 1
            assert f['engine'].stats.errors_count == before + 1
            errors = [event for event in f['engine']._event_queue if type(event) is ErrorEvent]
            assert 'unresolved_symbol_attempt' in errors[0].message
        finally:
            await teardown(f)
    asyncio.run(scenario())


def test_the_entry_stale_loops_have_nothing_to_sweep_in_attach_mode(tmp_path, monkeypatch, freeze):
    """S4 경계: 90초 매도 폴백·10분 매수 취소는 `_pending_timestamps` 를 순회한다(사실 6)."""
    async def scenario():
        f = await wired(tmp_path, monkeypatch, freeze)
        try:
            broker_calls = []
            for name in ('cancel_all_for_symbol', 'submit_order'):
                async def record(*args, _name=name, **kwargs):
                    broker_calls.append(_name)
                    return True
                monkeypatch.setattr(f['broker'], name, record, raising=False)
            cleared = []
            monkeypatch.setattr(f['rm'], 'clear_pending',
                                lambda symbol, amount=D('0'): cleared.append(symbol))
            await drive(f['engine'], buy(SYM))
            assert len(posts(f)) == 1
            f['now'][0] = ENGINE_NOW + timedelta(minutes=15)
            await drive(f['engine'], buy('000660'))
            assert broker_calls == [] and cleared == []
            assert f['rm']._pending_timestamps == {}
        finally:
            await teardown(f)
    asyncio.run(scenario())


# ── S4-2 결정 ⑥: eviction 은 attach 에서도 호출된다 ─────────────────────

def test_eviction_is_reached_in_attach_mode(tmp_path, monkeypatch, freeze):
    """S4-2 가 H5 를 복원했다 — 호출부까지 닿는다(실제 SELL 은 eviction 시험 파일이 고정한다)."""
    async def scenario():
        f = await wired(tmp_path, monkeypatch, freeze)
        try:
            evicted = []

            async def evict(**kwargs):
                evicted.append(kwargs)
                return '000660'

            monkeypatch.setattr(f['rm'], '_try_evict_weakest_position', evict)
            f['rm']._risk_validator = type('V', (), {'can_open_position': staticmethod(
                lambda *args, **kwargs: (False, '최대 포지션 수 도달'))})()
            calls = spy(f)
            await drive(f['engine'], buy(SYM, score=99.0))
            assert len(evicted) == 1 and evicted[0]['new_symbol'] == SYM
            assert queued_types(f['engine']) == []
            assert [len(calls[key]) for key in ('submit', 'publish', 'prepare')] == [0, 0, 0]
            assert posts(f) == [] and f['runtime'].owner.state['attempts'] == {}
        finally:
            await teardown(f)
    asyncio.run(scenario())


# ── 결정 ⑮·계약 3 ───────────────────────────────────────────────────────

@pytest.mark.parametrize('outcome', ['sent', 'refused', 'raised'])
def test_the_pending_sector_map_is_empty_after_every_signal(tmp_path, monkeypatch, freeze,
                                                            outcome):
    """성공 경로의 유일한 pop(`update_position` 763)이 attach 에서 막혀 있다(결정 ⑮)."""
    async def scenario():
        f = await wired(tmp_path, monkeypatch, freeze)
        try:
            if outcome == 'refused':
                f['ready'][0] = False
            if outcome == 'raised':
                # 게시~prepare 실패는 예외로 올라온다 — 이 경로의 정상적인 실패 채널이다.
                async def refuse(*args, **kwargs):
                    raise ValueError('unresolved_symbol_attempt')
                f['gateway'].submit = refuse
            before = f['engine'].stats.errors_count
            f['engine']._pending_sector_map[SYM] = SECTOR
            await drive(f['engine'], buy(SYM))
            assert f['engine']._pending_sector_map == {}
            assert len(posts(f)) == (1 if outcome == 'sent' else 0)
            assert f['engine'].stats.errors_count == before + (1 if outcome == 'raised' else 0)
        finally:
            await teardown(f)
    asyncio.run(scenario())


def test_a_candidate_refused_by_the_legacy_gates_never_reaches_the_gateway(tmp_path, monkeypatch,
                                                                          freeze):
    """on_signal 이 None 을 돌려주면 속성에 증거가 남아 있어도 게시하지 않는다(계약 3)."""
    async def scenario():
        f = await wired(tmp_path, monkeypatch, freeze)
        try:
            f['rm'].engine.can_open_position = lambda *args, **kwargs: (False, '합성 거부')
            calls = spy(f)
            await drive(f['engine'], buy(SYM))
            assert f['rm']._last_qualification_evidence is not None
            assert [len(calls[key]) for key in ('submit', 'publish', 'prepare')] == [0, 0, 0]
            assert posts(f) == [] and f['runtime'].owner.state['attempts'] == {}
            assert f['engine']._pending_sector_map == {}
        finally:
            await teardown(f)
    asyncio.run(scenario())


def test_the_signal_event_is_not_mutated_by_the_handoff(tmp_path, monkeypatch, freeze):
    async def scenario():
        f = await wired(tmp_path, monkeypatch, freeze)
        try:
            seen = []
            original = f['gateway'].submit

            async def record(event, order, evidence):
                seen.append((event, event.score, event.metadata['position_multiplier'],
                             event.price))
                return await original(event, order, evidence)

            f['gateway'].submit = record
            event = buy(SYM)
            await drive(f['engine'], event)
            assert len(posts(f)) == 1
            # 인계점이 받은 그 객체가 송신 뒤에도 같은 값이다(CV 의 점수 조정은 인계 이전이다).
            assert len(seen) == 1 and seen[0][0] is event
            assert seen[0][1:] == (event.score, event.metadata['position_multiplier'], event.price)
        finally:
            await teardown(f)
    asyncio.run(scenario())


# ── 합성 허가가 없으면 예약도 남지 않는다 ────────────────────────────────

def test_without_the_synthetic_permit_nothing_is_sent_and_nothing_is_reserved(tmp_path,
                                                                             monkeypatch, freeze):
    async def scenario():
        f = await wired(tmp_path, monkeypatch, freeze)
        try:
            f['ready'][0] = False
            before = f['engine'].stats.errors_count
            await drive(f['engine'], buy(SYM))
            assert posts(f) == []
            attempt = only_attempt(f)
            assert attempt['state'] == 'final_rejected'
            assert reservations(attempt) == (0, D('0'), D('0'), None)
            assert f['engine'].stats.errors_count == before
            assert f['engine']._pending_sector_map == {}
        finally:
            await teardown(f)
    asyncio.run(scenario())


# ── legacy 불변 대조 (§5 의 행동 증명) ──────────────────────────────────

def test_the_same_signal_keeps_the_legacy_path_unchanged_without_a_runtime(tmp_path, monkeypatch,
                                                                          freeze):
    """attach 가드를 '항상 참'으로 바꾸는 변이는 이 쌍에서 죽는다."""
    async def scenario():
        f = await wired(tmp_path, monkeypatch, freeze)
        try:
            calls = spy(f)
            legacy = UnifiedEngine(TradingConfig(initial_capital=D('2000000')))
            legacy.broker = f['broker']
            rm = risk_manager(monkeypatch, legacy, validator=cv())
            engine_clock(monkeypatch)
            assert legacy._execution_runtime is None
            before = legacy.stats.errors_count
            await drive(legacy, buy(SYM))

            orders = [event for event in legacy._event_queue if type(event) is OrderEvent]
            assert len(orders) == 1
            order = orders[0].order
            assert (order.symbol, order.side, order.quantity) == (SYM, OrderSide.BUY, JUSTIFIED)
            assert (order.price, order.strategy) == (PRICE, 'sepa_trend')
            assert rm._pending_orders == {SYM}
            assert rm._pending_quantities[SYM] == JUSTIFIED
            assert rm._pending_timestamps[SYM] == ENGINE_NOW
            assert rm._reserved_by_order[SYM] == PRICE * JUSTIFIED * D('1.015')
            assert rm._pending_strategy[SYM] == 'sepa_trend'
            assert rm._pending_signal_cache[SYM]['strategy'] == 'sepa_trend'
            assert rm._last_signal_time[SYM] == ENGINE_NOW
            assert legacy.stats.errors_count == before
            assert all(calls[key] == [] for key in calls)
            assert posts(f) == []
        finally:
            await teardown(f)
    asyncio.run(scenario())


def test_eviction_still_runs_on_the_legacy_path(tmp_path, monkeypatch, freeze):
    """H5 의 가드를 '항상 참'(= 항상 건너뜀)으로 바꾸면 여기서 죽는다."""
    async def scenario():
        # CV 시간 가드는 engine_clock 이 닿지 않는 축이다 — 같은 순간으로 함께 민다.
        freeze(ENGINE_NOW.hour, ENGINE_NOW.minute, day=ENGINE_NOW.day)
        legacy = UnifiedEngine(TradingConfig(initial_capital=D('2000000')))
        rm = risk_manager(monkeypatch, legacy, validator=cv())
        engine_clock(monkeypatch)
        evicted = []

        async def evict(**kwargs):
            evicted.append(kwargs)
            return '000660'

        monkeypatch.setattr(rm, '_try_evict_weakest_position', evict)
        rm._risk_validator = type('V', (), {'can_open_position': staticmethod(
            lambda *args, **kwargs: (False, '최대 포지션 수 도달'))})()
        await drive(legacy, buy(SYM, score=99.0))
        assert len(evicted) == 1 and evicted[0]['new_symbol'] == SYM
    asyncio.run(scenario())


def test_the_evidence_is_taken_before_anything_else_can_replace_it(tmp_path, monkeypatch, freeze):
    """계약 3: 증거는 on_signal 반환 직후의 지역값이다.

    `_last_qualification_evidence` 는 공유 RiskManager 의 가변 속성이다. 반환과 submit 사이에
    무엇이든 끼어들어 속성을 바꾸면, 늦게 읽는 구현은 **다른 요청의 증거**를 이번 intent 로 게시한다.
    """
    async def scenario():
        f = await wired(tmp_path, monkeypatch, freeze)
        try:
            rm, sentinel, seen = f['rm'], object(), []
            original_signal, original_submit = rm.on_signal, f['gateway'].submit

            class Trap:
                """`.order` 를 읽는 순간 속성을 바꿔 치운다 — 읽기 순서를 관측 가능하게 만든다."""
                def __init__(self, inner):
                    self._inner = inner

                @property
                def order(self):
                    rm._last_qualification_evidence = sentinel
                    return self._inner.order

            async def trapped(event):
                result = await original_signal(event)
                return [Trap(result[0])] if result else result

            async def record(event, order, evidence):
                seen.append(evidence)
                return await original_submit(event, order, evidence)

            rm.on_signal = trapped
            f['gateway'].submit = record
            await drive(f['engine'], buy(SYM))
            assert len(seen) == 1 and seen[0] is not sentinel and seen[0] is not None
            assert len(posts(f)) == 1
        finally:
            await teardown(f)
    asyncio.run(scenario())


# ── Codex 교차 리뷰 3차 ───────────────────────────────────────────────────

def test_a_risk_manager_with_legacy_orders_connected_after_attach_cannot_post_directly(
        tmp_path, monkeypatch, freeze):
    """H6 은 attach **시점**만 본다 — 그 뒤에 장부가 남은 관리자가 연결되면 진입부 stale 루프가
    owner 를 거치지 않고 직접 취소·재주문한다(계약 1 위반). 위험 지점에서 막는다.
    """
    async def scenario():
        f = await wired(tmp_path, monkeypatch, freeze)
        try:
            broker_calls = []
            for name in ('cancel_all_for_symbol', 'submit_order'):
                async def record(*args, _name=name, **kwargs):
                    broker_calls.append(_name)
                    return True
                monkeypatch.setattr(f['broker'], name, record, raising=False)
            rm = f['rm']
            # attach 이후에 남아 있는 legacy 미체결 매도(90초 경과, 정규장).
            rm._pending_orders.add('000660')
            rm._pending_sides['000660'] = OrderSide.SELL
            rm._pending_quantities['000660'] = 10
            rm._pending_timestamps['000660'] = ENGINE_NOW - timedelta(seconds=200)
            calls = spy(f)
            before = f['engine'].stats.errors_count
            await drive(f['engine'], buy(SYM))
            assert broker_calls == []
            assert f['engine'].stats.errors_count == before + 1
            assert [len(calls[key]) for key in ('submit', 'publish', 'prepare')] == [0, 0, 0]
            assert posts(f) == []
        finally:
            await teardown(f)
    asyncio.run(scenario())


def test_a_signal_typed_event_without_a_symbol_does_not_stop_the_loop(tmp_path, monkeypatch,
                                                                     freeze):
    """정리(`finally`)가 다시 던지면 흡수한 예외를 덮고 루프 밖으로 샌다."""
    from src.core.event import Event

    async def scenario():
        f = await wired(tmp_path, monkeypatch, freeze)
        try:
            before = f['engine'].stats.errors_count
            await drive(f['engine'], Event(type=EventType.SIGNAL))
            assert f['engine'].stats.errors_count == before + 1
            f['engine']._event_queue.clear()
            await drive(f['engine'], buy(SYM))
            assert len(posts(f)) == 1
        finally:
            await teardown(f)
    asyncio.run(scenario())


# ── P0-4 S-C(C3): 접수 불가 세션의 소멸은 **모양이 다르다** ──────────────

@pytest.mark.parametrize('moment, session', [((15, 31), 'break'), ((20, 1), 'closed')])
def test_a_session_that_cannot_accept_a_submit_leaves_an_error_event_not_a_result(
        tmp_path, monkeypatch, freeze, moment, session):
    """`break`·`closed` 는 요청 객체를 **만들 수조차** 없다(`requests.py:243-244`).

    그래서 소멸의 모양이 `CommandResult(NOT_SENT)` 가 아니라 예외다 — `gateway._bind` 에서
    올라와 `gateway.submit` 밖으로 새고 engine 이 `errors_count` 를 올리며 ErrorEvent 를
    낸다. 같은 순간 legacy 도 보내지 못하지만(`kis_kr.py:511-514`) 매 틱 재감지로 의도를
    살린다 — attach 에는 그 루프가 없다(차단 사유 21). 재준비의 소유자는 생산자다(P1).
    """
    async def scenario():
        f = await wired(tmp_path, monkeypatch, freeze)
        try:
            from src.execution.safety.requests import _session_at
            hour, minute = moment
            stamp = freeze(hour, minute, day=18)
            f['now'][0] = stamp
            f['clock'][0] = f['clock'][0].replace(hour=hour, minute=minute)
            assert _session_at(f['clock'][0]) == session

            calls = spy(f)
            before = f['engine'].stats.errors_count
            await drive(f['engine'], buy(SYM))
            assert f['engine'].stats.errors_count == before + 1
            errors = [event for event in f['engine']._event_queue if type(event) is ErrorEvent]
            assert len(errors) == 1
            assert 'unsupported_submit_session' in errors[0].message
            assert posts(f) == []
            assert f['runtime'].owner.state['attempts'] == {}
            assert [len(calls[key]) for key in ('submit', 'prepare', 'dispatch')] == [1, 0, 0]
        finally:
            await teardown(f)
    asyncio.run(scenario())
