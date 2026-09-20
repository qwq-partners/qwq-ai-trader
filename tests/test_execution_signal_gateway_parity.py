"""S3-6b: attach 모드의 사이징·한도 정합(H2·H3·H6)과 실제 게이트 순서.

S3-6a 의 실큐 시험은 재사용한 `_order_env` 의 스텁(legacy 세션·`engine.can_open_position`·
`_sector_lookup`) 위에 있었다. **이 파일이 그 스텁을 걷어내고 실제 통과 경로를 처음 태운다** —
실제 `UnifiedEngine.can_open_position`, 실제 `KRSession` 표, 실제 `_calculate_position_size`.

owner 는 실제 SQLite·실제 RegimeOwner 이고 증거는 실제 `CrossStrategyValidator` 가 만든다.
전송은 합성 fake HTTP·주입 시계·시험용 합성 startup 허가 위의 결과다.

**S3 는 설치가 아니다** — 제품에 `KRExecutionRuntime` 을 만들거나 `attach()`·
`install_gateway()` 를 부르는 코드는 0건이고 제품의 `trading_ready` 는 계속 False 다.

시계는 넷을 같은 순간으로 맞춘다: legacy `KRSession`(`src.utils.session.datetime` — 여기서
처음 동결)·engine 모듈의 naive 시계(`engine_clock`)·CV 모듈 시계(`freeze`)·runtime 주입 시계.

실행: venv/bin/python -m pytest tests/test_execution_signal_gateway_parity.py -q -p no:cacheprovider
"""
import asyncio
from datetime import datetime
from decimal import Decimal as D
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.core.engine import UnifiedEngine
from src.core.event import ErrorEvent
from src.core.types import TradingConfig
from src.execution.safety.economics import encode_portfolio, new_risk_state
from src.execution.safety.protection import encode_protection
from src.execution.safety.runtime import KRExecutionRuntime
from src.execution.safety.store import ExecutionStateStore
from src.strategies.exit_manager import ExitManager

from test_cross_validator_characterization import freeze  # noqa: F401 — pytest fixture 재사용
from test_execution_decision_facts import unrelated_holding
from test_execution_qualification_publishers import SECTOR, buy, cv
from test_execution_runtime import NOW as RUNTIME_NOW
from test_kr_final_dispatch import Response
from test_execution_signal_gateway import reservations
from test_execution_signal_gateway_wiring import (
    ENGINE_NOW, JUSTIFIED, drive, engine_clock, posts, risk_manager, spy, teardown, wired,
)
from test_risk_sizing import PRICE, SYM, _rm

OTHER = '000100'
# `_rm` 이 심는 인스턴스 스텁 — parity 는 실제 메서드를 태워야 한다. `_get_core_reserve` 는
# 기본 0 스텁을 남겨 두고(코어 예약은 사이징 전체를 함께 움직인다) 전용 시험에서만 걷어낸다.
STUBS = ('_pending_strategy_notional',)


@pytest.fixture(autouse=True)
def synthetic_home(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)


def unstub(obj, *names):
    """하네스가 심은 인스턴스 속성을 걷어내 클래스의 실제 구현을 노출한다."""
    for name in names:
        obj.__dict__.pop(name, None)


def session_clock(monkeypatch, moment):
    """legacy `KRSession` 의 naive 시계 — 세션 표를 실제로 태우는 유일한 입력이다."""
    import src.utils.session as session

    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return moment if tz is None else moment.replace(tzinfo=tz)

    monkeypatch.setattr(session, 'datetime', Frozen)


def gate_log(rm, monkeypatch):
    """`_log_sig` 의 판정만 모은다(원 구현은 fire-and-forget 태스크를 만든다)."""
    records = []
    monkeypatch.setattr(rm, '_log_sig', lambda event, **kw: records.append(kw))
    return records


def blocked_gates(records):
    return [row.get('block_gate') for row in records if row.get('event_type') == 'blocked']


async def parity(tmp_path, monkeypatch, freeze_clock, *, strict=True):
    """S3-6a 의 배선 위에서 스텁을 걷어낸 엔진."""
    f = await wired(tmp_path, monkeypatch, freeze_clock)
    rm = f['rm']
    unstub(rm, *STUBS)
    if strict:
        # 실제 `UnifiedEngine.can_open_position` 이 owner 예약을 받는다(소비 지점 3).
        unstub(f['engine'], 'can_open_position')
    f['gates'] = gate_log(rm, monkeypatch)
    return f


def errors(engine):
    return [event for event in engine._event_queue if type(event) is ErrorEvent]


def attempts(f, side='buy'):
    return [row for row in f['runtime'].owner.state['attempts'].values() if row['side'] == side]


async def again(f, event, *, order_number=None):
    """ErrorEvent 가 큐에 남아 있으면 다음 `drive` 가 그걸 먼저 꺼낸다.

    fake 응답은 주문번호가 고정이라 두 번째 ACK 가 같은 `OrderRef` 를 받는다 — 실제 접수처럼
    새 번호를 돌려주게 갈아 끼운다(`order_number` 를 준 시험만).
    """
    f['engine']._event_queue.clear()
    if order_number is not None:
        f['broker']._session.response = Response(data={'rt_cd': '0', 'output': {
            'ODNO': order_number, 'KRX_FWDG_ORD_ORGNO': '12345'}})
    return await drive(f['engine'], event)


# ── 결정 ⑥: 전략 예산 축 ────────────────────────────────────────────────

def test_the_second_buy_is_sized_against_the_owners_strategy_remaining(tmp_path, monkeypatch,
                                                                       freeze):
    """오늘은 legacy `_pending_strategy_notional` 이 0 이라 B 가 owner 재유도값을 넘는다.

    A 는 ACK 되어 open(예약 유지)이지만 체결 전이라 portfolio 배분은 아직 0 이다. legacy 는
    예약을 보지 못해 B 를 A 와 같은 크기로 만들고, owner 는 `strategy_remaining` 에서
    A 의 예약을 빼므로 `decision_quantity_unjustified` 로 막는다(POST 0).
    """
    async def scenario():
        f = await parity(tmp_path, monkeypatch, freeze)
        try:
            await drive(f['engine'], buy(SYM))
            assert len(posts(f)) == 1
            reserved = f['gateway'].reserved_cash()
            # 전략 배분 40%(=80만) 안에서 A 의 예약이 실제로 잔여를 묶는다.
            budget = f['engine'].portfolio.total_equity * D('0.4')
            assert D('0') < budget - reserved < budget

            before = f['engine'].stats.errors_count
            await again(f, buy(OTHER), order_number='9876543210')
            assert len(posts(f)) == 2
            assert f['engine'].stats.errors_count == before
            sent = [row for row in attempts(f) if row['symbol'] == OTHER]
            assert len(sent) == 1 and sent[0]['command_status'] == 'acknowledged'
            # owner 의 잔여가 legacy 사이징을 실제로 줄였다.
            assert 0 < sent[0]['quantity'] < JUSTIFIED
        finally:
            await teardown(f)
    asyncio.run(scenario())


def test_the_strategy_budget_gate_uses_the_owner_reservation(tmp_path, monkeypatch, freeze):
    """전략 예산 조기 차단(2225-2244)도 owner 예약 기준으로 발화한다 — 게시 0·POST 0."""
    async def scenario():
        f = await parity(tmp_path, monkeypatch, freeze)
        try:
            await drive(f['engine'], buy(SYM))
            assert len(posts(f)) == 1
            # 전략 배분을 A 의 예약 아래로 좁히면 잔여가 없다(설정만 바꾼다).
            reserved = f['gateway'].reserved_cash()
            equity = f['engine'].portfolio.total_equity
            f['rm'].config.strategy_allocation = dict(
                f['rm'].config.strategy_allocation,
                sepa_trend=float(reserved / equity * 100) - 1.0)
            calls = spy(f)
            await again(f, buy(OTHER))
            assert len(posts(f)) == 1
            assert blocked_gates(f['gates']) == ['G5_budget']
            assert [len(calls[key]) for key in ('submit', 'publish', 'prepare')] == [0, 0, 0]
        finally:
            await teardown(f)
    asyncio.run(scenario())


# ── 결정 ⑥: 현금 축 ─────────────────────────────────────────────────────

def test_the_cash_gate_blocks_before_any_publication(tmp_path, monkeypatch, freeze):
    """owner 예약이 가용 현금을 넘으면 G5_cash 에서 막힌다 — 게시 0·prepare 0·POST 0."""
    async def scenario():
        f = await parity(tmp_path, monkeypatch, freeze)
        try:
            await drive(f['engine'], buy(SYM))
            assert len(posts(f)) == 1
            reserved = f['gateway'].reserved_cash()
            # 무관 보유로 현금만 줄인다(자산 불변) — 남은 가용 현금이 A 의 예약보다 적어진다.
            await unrelated_holding(f, symbol='000660', quantity=150)
            available = f['engine'].get_available_cash()
            assert D('0') < available < reserved

            calls = spy(f)
            await again(f, buy(OTHER))
            assert len(posts(f)) == 1
            assert blocked_gates(f['gates']) == ['G5_cash']
            assert [len(calls[key]) for key in ('submit', 'publish', 'prepare')] == [0, 0, 0]
            assert len(attempts(f)) == 1
        finally:
            await teardown(f)
    asyncio.run(scenario())


def test_the_core_reserve_is_added_once_on_top_of_the_owner_reservation(tmp_path, monkeypatch,
                                                                        freeze):
    """이중 차감·누락 확인: owner 는 `pending + core_reserve`, engine 도 같은 합이어야 한다.

    `gateway.reserved_cash()` 는 core 예약을 더하지 않는다(risk_policy 의 `evaluate_entry_policy`
    가 non-core 에서 따로 더한다). engine 은 `_get_core_reserve()` 를 따로 빼므로 합이 같다.
    """
    async def scenario():
        f = await parity(tmp_path, monkeypatch, freeze)
        try:
            await drive(f['engine'], buy(SYM))
            assert len(posts(f)) == 1
            unstub(f['rm'], '_get_core_reserve')     # 실제 코어 예약(배분 30%·보유 0)
            pending = f['gateway'].reserved_cash()
            core = f['rm']._get_core_reserve()
            assert core > D('0')                     # 코어 배분 30%·코어 보유 0
            assert f['gateway'].reserved_cash() == pending   # core 는 포함되지 않는다

            seen = []
            original = f['engine'].can_open_position

            def record(*args, **kwargs):
                seen.append(kwargs.get('reserved_cash'))
                return original(*args, **kwargs)

            f['engine'].can_open_position = record
            await again(f, buy(OTHER), order_number='9876543210')
            assert len(seen) == 1
            assert seen[0] == pending + core
        finally:
            await teardown(f)
    asyncio.run(scenario())


# ── fail-closed: 예약 읽기 실패는 0 이 아니다 ────────────────────────────

def test_an_unreadable_reservation_refuses_the_signal_instead_of_reporting_zero(tmp_path,
                                                                                monkeypatch,
                                                                                freeze):
    """helper 는 snapshot 을 못 만들면 예외를 낸다 — H1 이 흡수해 그 SIGNAL 만 거부된다."""
    async def scenario():
        f = await parity(tmp_path, monkeypatch, freeze)
        try:
            engine = f['engine']
            original_cash = engine.portfolio.cash
            # 게시본과 어긋난 legacy portfolio — owner 는 복구가 필요한 상태다.
            engine.portfolio.cash = original_cash + D('1')
            calls = spy(f)
            before = engine.stats.errors_count
            await drive(engine, buy(SYM))
            assert engine.stats.errors_count == before + 1
            assert 'legacy_portfolio_writer_conflict' in errors(engine)[0].message
            assert posts(f) == []
            assert [len(calls[key]) for key in ('submit', 'publish', 'prepare')] == [0, 0, 0]

            # 루프는 살아 있다 — 상태를 되돌리면 같은 종목이 정상 송신된다.
            engine.portfolio.cash = original_cash
            f['now'][0] = ENGINE_NOW.replace(minute=5)
            await again(f, buy(SYM))
            assert len(posts(f)) == 1
        finally:
            await teardown(f)
    asyncio.run(scenario())


# ── H6: legacy 장부가 남은 채로는 설치하지 않는다 ────────────────────────

async def unattached(tmp_path):
    """restore 까지만 끝낸 runtime — attach 는 시험이 직접 부른다(`setup` 은 attach 한다)."""
    engine = UnifiedEngine(TradingConfig(initial_capital=D('2000000')))
    exits = ExitManager(persist=False, clock=lambda: RUNTIME_NOW)
    store = ExecutionStateStore(tmp_path / 'execution' / 'state.sqlite3')
    await store.commit(0, {
        'portfolio': encode_portfolio(engine.portfolio),
        'protection': encode_protection(exits),
        'risk': new_risk_state(RUNTIME_NOW.date().isoformat()),
        'lots': {}, 'outbox': {}, 'intents': {}, 'attempts': {},
        'startup_reconciliation': {'status': 'blocked', 'reason': 'synthetic_baseline'},
    }, 'synthetic-baseline')
    runtime = KRExecutionRuntime(store, engine, exits, clock=lambda: RUNTIME_NOW)
    await runtime.restore()
    return engine, store, runtime


def legacy_books(**changes):
    books = {'_pending_orders': set(), '_pending_timestamps': {}, '_reserved_by_order': {}}
    books.update(changes)
    return SimpleNamespace(**books)


@pytest.mark.parametrize('ledger', ['_pending_orders', '_pending_timestamps',
                                    '_reserved_by_order'])
def test_bind_refuses_a_runtime_while_the_legacy_ledgers_are_not_empty(tmp_path, ledger):
    """남은 행이 있으면 진입부의 90초 stale SELL 루프가 owner 를 건너뛰고 직접 POST 한다."""
    async def scenario():
        engine, store, runtime = await unattached(tmp_path)
        try:
            row = {'_pending_orders': {SYM}}.get(ledger, {SYM: RUNTIME_NOW})
            engine.risk_manager = legacy_books(**{ledger: row})
            with pytest.raises(RuntimeError):
                runtime.attach()
            assert engine._execution_runtime is None
        finally:
            await store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('books', ['empty', 'absent'])
def test_bind_succeeds_when_no_legacy_order_is_outstanding(tmp_path, books):
    async def scenario():
        engine, store, runtime = await unattached(tmp_path)
        try:
            engine.risk_manager = None if books == 'absent' else legacy_books()
            runtime.attach()
            assert engine._execution_runtime is runtime
        finally:
            await store.close()
    asyncio.run(scenario())


# ── 결정 ⑭: 섹터 조회 실패는 attach 에서 거부다 ─────────────────────────

@pytest.mark.parametrize('lookup', ['raises', 'returns_none'])
def test_a_sector_lookup_failure_refuses_the_buy_in_attach_mode(tmp_path, monkeypatch, freeze,
                                                                lookup):
    """owner 는 sector None 에서 섹터 한도를 건너뛴다(사실 16) — 소비 지점에서 막는다."""
    async def scenario():
        f = await parity(tmp_path, monkeypatch, freeze)
        try:
            async def sector(symbol):
                if lookup == 'raises':
                    raise RuntimeError('synthetic-sector-outage')
                return None

            f['rm']._sector_lookup = sector
            calls = spy(f)
            await drive(f['engine'], buy(SYM, score=82.0, sector=None))
            if lookup == 'raises':
                assert posts(f) == []
                assert blocked_gates(f['gates']) == ['G3_sector']
                assert [len(calls[key]) for key in ('submit', 'publish', 'prepare')] == [0, 0, 0]
                assert f['runtime'].owner.state['attempts'] == {}
            else:
                # 정상적으로 모른다고 답한 경우는 종전대로 진행한다.
                assert len(posts(f)) == 1
                assert blocked_gates(f['gates']) == []
        finally:
            await teardown(f)
    asyncio.run(scenario())


# ── 계약 2: 실제 KRSession 표가 owner 의 허용 구간을 덮는다 ──────────────

@pytest.mark.parametrize('moment,sent', [((8, 55), 0), ((15, 30), 0), ((11, 0), 1)])
def test_the_legacy_session_table_closes_the_owners_allowed_gaps(tmp_path, monkeypatch, freeze,
                                                                 moment, sent):
    """owner 세션 표는 08:50-09:00·15:20-15:40 을 허용하지만 legacy 는 CLOSED 다(계약 2)."""
    async def scenario():
        f = await parity(tmp_path, monkeypatch, freeze)
        try:
            # 세션 스텁을 걷어내 실제 `KRSession` 표를 태운다.
            unstub(f['engine'], 'is_trading_hours', '_get_current_session')
            when = ENGINE_NOW.replace(hour=moment[0], minute=moment[1])
            session_clock(monkeypatch, when)
            f['now'][0] = when
            calls = spy(f)
            await drive(f['engine'], buy(SYM))
            assert len(posts(f)) == sent
            assert [len(calls[key]) for key in ('submit', 'publish', 'prepare')] == [sent] * 3
        finally:
            await teardown(f)
    asyncio.run(scenario())


# ── 합성 허가가 없으면 예약도 남지 않는다 ────────────────────────────────

def test_without_the_synthetic_permit_the_strategy_scenario_reserves_nothing(tmp_path,
                                                                             monkeypatch, freeze):
    async def scenario():
        f = await parity(tmp_path, monkeypatch, freeze)
        try:
            f['ready'][0] = False
            await drive(f['engine'], buy(SYM))
            await again(f, buy(OTHER))
            assert posts(f) == []
            rows = attempts(f)
            assert len(rows) == 2
            for row in rows:
                assert row['state'] == 'final_rejected'
                assert reservations(row) == (0, D('0'), D('0'), None)
            assert f['gateway'].reserved_cash() == D('0')
        finally:
            await teardown(f)
    asyncio.run(scenario())


# ── legacy 불변 대조 (§5 의 행동 증명) ──────────────────────────────────

def test_without_a_runtime_the_reservation_reads_stay_on_the_legacy_ledgers(monkeypatch):
    """attach 가드를 '항상 참'으로 바꾸는 변이는 여기서 죽는다(runtime 없음 = gateway 없음)."""
    rm = _rm(monkeypatch, mode='nominal')
    unstub(rm, *STUBS)
    rm._pending_strategy = {}
    assert getattr(rm.engine, '_execution_runtime', None) is None
    assert rm._reserved_cash == D('0')
    assert rm._pending_strategy_notional('sepa_trend') == D('0')

    rm._reserved_by_order = {SYM: D('101500'), OTHER: D('203000')}
    rm._pending_strategy = {SYM: 'sepa_trend', OTHER: 'gap_and_go'}
    assert rm._reserved_cash == D('304500')
    assert rm._pending_strategy_notional('sepa_trend') == D('101500')
    assert rm._pending_strategy_notional('gap_and_go') == D('203000')
    assert rm._pending_strategy_notional('vcp_breakout') == D('0')


def test_without_a_runtime_a_sector_lookup_failure_still_falls_back_to_none(tmp_path, monkeypatch,
                                                                            freeze):
    """H3 의 attach 가드를 '항상 참'으로 바꾸면 legacy 매수가 섹터 장애마다 죽는다."""
    async def scenario():
        legacy = UnifiedEngine(TradingConfig(initial_capital=D('2000000')))
        rm = risk_manager(monkeypatch, legacy, validator=cv())
        engine_clock(monkeypatch)
        unstub(rm, *STUBS)
        records = gate_log(rm, monkeypatch)

        async def sector(symbol):
            raise RuntimeError('synthetic-sector-outage')

        rm._sector_lookup = sector
        assert legacy._execution_runtime is None
        result = await rm.on_signal(buy(SYM, score=82.0, sector=None))
        assert result is not None and len(result) == 1
        assert result[0].order.symbol == SYM
        assert blocked_gates(records) == []
        # 섹터를 모른 채 진행한 종전 동작 그대로다.
        assert rm._pending_orders == {SYM}
    asyncio.run(scenario())


def test_the_pending_sector_map_is_still_cleared_when_the_sector_lookup_fails(tmp_path,
                                                                             monkeypatch, freeze):
    """⑮: 거부 경로에서도 legacy 섹터 집계에 유령 항목이 남지 않는다."""
    async def scenario():
        f = await parity(tmp_path, monkeypatch, freeze)
        try:
            async def sector(symbol):
                raise RuntimeError('synthetic-sector-outage')

            f['rm']._sector_lookup = sector
            f['engine']._pending_sector_map[SYM] = SECTOR
            await drive(f['engine'], buy(SYM, score=82.0, sector=None))
            assert f['engine']._pending_sector_map == {}
            assert posts(f) == []
        finally:
            await teardown(f)
    asyncio.run(scenario())
