"""P0-3 S-A: 생산자 배선·BUY 게이트·체결 metadata·설치 창(부품 — 제품 호출자 0건).

여기서 GREEN 인 것은 부품의 계약뿐이다. 실 KIS 일별체결 응답의 철자·거래소 범위·취소
최종성은 계획서 §5 의 스모크 4항이 닫기 전에는 아무것도 증명되지 않았고, 설치기는 그
동안 `execution_query_environment_required` 로 끝난다.

fake 를 남긴 곳은 기존 부품 시험과 같다(브로커 HTTP·시계·수집기 응답). 수집기 자체는
`test_execution_install_factory` 의 결정적 collect 를 그대로 쓴다.
"""
import asyncio
from datetime import timedelta
from decimal import Decimal as D
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture(autouse=True)
def synthetic_home(tmp_path, monkeypatch):
    """설치기 시험 모듈과 같은 보호 — autouse fixture 는 모듈 경계를 넘지 않는다."""
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)

from src.core.types import Order, OrderSide
from src.execution.safety.application import ApplicationBlocked, InboxReceipt
from src.execution.safety.commands import CommandValidationError
from src.execution.safety.guards import EntryOrigin
from src.execution.safety.gateway import SignalGateway
from src.execution.safety.runtime import RECONCILER_STALL_CYCLES
from src.utils.exit_types import classify_exit_type
from test_execution_command_owner import fixture, held
from test_execution_install_factory import (NOW_KST, SCOPE, assert_untouched, live_snapshot,
                                            seed_checkpoint, target)
from test_execution_runtime import opened

# ---------------------------------------------------------------- 분류 함수 동일성


REASONS = ('손절 -5.2%', 'stop loss', '긴급전량청산', 'emergency exit', '트레일링 스탑',
           'trailing stop', '본전 이탈', 'breakeven exit', '횡보 청산', '추세 무효화',
           '익절후 저효율', '보유기간 초과', '코어홀딩 조기경보', 'RSI2 청산', '3차 익절',
           '2차 익절', '1차 익절', '익절 +10%', 'fill_detected', '')


def test_the_pure_function_classifies_exactly_like_the_scheduler():
    """스케줄러 메서드와 새 순수 함수가 같은 입력에서 같은 태그를 낸다(위임은 S-B)."""
    from src.schedulers.kr_scheduler import KRScheduler
    assert len(REASONS) == 20
    for reason in REASONS:
        assert classify_exit_type(reason) == KRScheduler._classify_exit_type(reason)
    # 스케줄러는 reason 을 `reason or ''` 로 받는다 — 문자열이 아닌 입력에서도 같은
    # 결론이어야 한다(falsy 판정 금지 규칙 때문에 순수 함수는 `or` 를 쓸 수 없다).
    for odd in (None, '', 0, False):
        assert classify_exit_type(odd) == KRScheduler._classify_exit_type(odd) == 'manual'
    assert classify_exit_type('손절 -5.2%') == 'stop_loss'


# ---------------------------------------------------------------- ⑤ 체결 metadata


def test_sell_binding_carries_the_exit_tag_and_buy_carries_the_candidate_name():
    buy = Order(symbol='005930', side=OrderSide.BUY, quantity=10, price=D('10000'))
    sell = Order(symbol='005930', side=OrderSide.SELL, quantity=10, price=D('10000'),
                 reason='손절 -5.2% (ATR)')
    event = SimpleNamespace(score=80.0, reason='익절 +10%',
                            metadata={'candidate_name': '삼성전자'})
    assert SignalGateway._fill_metadata(event, sell) == {'exit_type': 'stop_loss'}
    # order.reason 이 없으면 신호가 실어 온 사유를 쓴다(engine 이 싣지 못한 경로).
    sell.reason = None
    assert SignalGateway._fill_metadata(event, sell) == {'exit_type': 'take_profit'}
    # 양쪽 다 없으면 legacy 의 빈 문자열 분류와 같다.
    assert SignalGateway._fill_metadata(SimpleNamespace(score=80.0), sell) == {'exit_type': 'manual'}
    assert SignalGateway._fill_metadata(event, buy) == {'entry_signal_score': 80.0, 'name': '삼성전자'}
    # 이름이 없으면 키 자체가 없다 — 빈 문자열은 `invalid_fill_metadata_value` 이고
    # 종목코드를 이름으로 위조하지도 않는다.
    event.metadata = {'name': ''}
    assert SignalGateway._fill_metadata(event, buy) == {'entry_signal_score': 80.0}
    event.metadata = None
    assert SignalGateway._fill_metadata(event, buy) == {'entry_signal_score': 80.0}
    event.metadata = {'name': '에스케이하이닉스'}
    assert SignalGateway._fill_metadata(event, buy)['name'] == '에스케이하이닉스'
    # 공백은 prepare 의 검사기(`commands._fill_metadata`)와 같은 정규화로 없앤다 — 앞뒤
    # 공백을 그대로 실으면 그 BUY 는 `invalid_fill_metadata_value` 로 통째로 죽는다.
    event.metadata = {'candidate_name': ' 삼성전자 '}
    assert SignalGateway._fill_metadata(event, buy)['name'] == '삼성전자'
    event.metadata = {'candidate_name': '   '}
    assert SignalGateway._fill_metadata(event, buy) == {'entry_signal_score': 80.0}


def test_the_sell_tag_survives_prepare_into_the_stored_binding(tmp_path, monkeypatch):
    """게이트웨이가 만든 태그가 owner 의 `request_binding` 까지 그대로 간다."""
    async def scenario():
        f = await fixture(tmp_path, monkeypatch)
        try:
            await held(f)
            request = f['request']('S1', side=OrderSide.SELL, quantity=5)
            sell = Order(symbol=request.symbol, side=OrderSide.SELL, quantity=5,
                         price=D('10000'), reason='1차 익절 +10%')
            metadata = SignalGateway._fill_metadata(SimpleNamespace(score=None), sell)
            assert metadata == {'exit_type': 'first_take_profit'}
            attempt = await f['commands'].prepare(request, f['entry'](request),
                                                  fill_metadata=metadata)
            assert attempt['request_binding']['fill_metadata'] == {'exit_type': 'first_take_profit'}
        finally:
            await f['store'].close()
    asyncio.run(scenario())


# ---------------------------------------------------------------- ④ BUY 게이트


async def _buy(f, aid='A'):
    request = f['request'](aid)
    await f['quote'](request)
    return await f['commands'].prepare(request, f['entry'](request), sector='반도체')


def test_a_dead_producer_cycle_blocks_the_buy_and_never_the_protective_sell(tmp_path, monkeypatch):
    """주기 task 가 죽으면 새 매수는 멈추고 보호 매도·취소는 계속 나간다(F8 ②)."""
    async def scenario():
        f = await fixture(tmp_path, monkeypatch)
        runtime = f['runtime']
        try:
            async def dead():
                return None

            task = asyncio.create_task(dead())
            await task
            runtime._reconciler_task = task
            runtime._reconciler_started_at = runtime._now()
            assert runtime.reconciler_live(runtime._now()) is False
            request = f['request']()
            await f['quote'](request)
            with pytest.raises(CommandValidationError, match='reconciler_unavailable'):
                await f['commands'].prepare(request, f['entry'](request), sector='반도체')
            # 같은 상태에서 매도는 게이트에 걸리지 않는다(수량 검사까지 간다).
            sell = f['request']('S', side=OrderSide.SELL, quantity=5)
            with pytest.raises(CommandValidationError) as blocked:
                await f['commands'].prepare(sell, f['entry'](sell))
            assert 'reconciler_unavailable' not in str(blocked.value)
        finally:
            await f['store'].close()
    asyncio.run(scenario())


def test_a_started_producer_passes_while_it_completes_cycles_and_stops_when_it_goes_stale(
        tmp_path, monkeypatch):
    """기동한 주기: 오늘 완료 조회가 신선하면 통과, 낡았는데 기다리는 대상이 있으면 거부.

    여기서 죽는 것은 **조회** 신선도 절 하나다 — 주기 완주 시각은 내내 신선하게 둔다
    (주기는 돌지만 조회만 죽은 상태를 고립시킨다).
    """
    async def scenario():
        f = await fixture(tmp_path, monkeypatch)
        runtime, clock = f['runtime'], f['clock']
        try:
            async def idle():
                await asyncio.sleep(3600)

            task = asyncio.create_task(idle())
            runtime._reconciler_task = task
            runtime._reconciler_started_at = clock[0]
            runtime._reconciler_cycle_completed_at = clock[0]
            runtime._reconciler_complete_at = clock[0]
            assert runtime.reconciler_live(clock[0]) is True
            # 접수까지 간 미해결 주문 = 주기가 기다리는 대상이 있다.
            await opened(runtime, 'p03-open')
            assert runtime.reconciler_live(clock[0]) is True
            # 완료 조회가 낡으면 같은 상태에서 거부다. **진전은 방금 있었다** — B1 재접수는
            # 조회 없이도 진전을 만들므로 `blocked_reason` 은 None 이고, 조회가 통째로 죽은
            # 이 상태를 잡는 것은 조회 완료 시각 하나뿐이다.
            stale = RECONCILER_STALL_CYCLES * runtime._reconciler_interval + 1
            clock[0] = clock[0] + timedelta(seconds=stale)
            runtime._reconciler_progress_at = clock[0]
            runtime._reconciler_cycle_completed_at = clock[0]
            assert runtime.reconciler_blocked_reason() is None
            assert runtime.reconciler_live(clock[0]) is False
            second = f['request']('B', symbol='000660')
            await f['quote'](second)
            with pytest.raises(CommandValidationError, match='reconciler_unavailable'):
                await f['commands'].prepare(second, f['entry'](second), sector='반도체')
            # 주기가 한 번 더 돌아 조회를 완료하고 진전을 만들면 다시 통과한다.
            runtime._reconciler_complete_at = clock[0]
            runtime._reconciler_progress_at = clock[0]
            assert runtime.reconciler_live(clock[0]) is True
            task.cancel()
        finally:
            await f['store'].close()
    asyncio.run(scenario())


async def _until(predicate, limit=5.0):
    """조건이 설 때까지 실제 루프를 돌린다(주입 시계와 무관한 실시간 대기)."""
    async def wait():
        while not predicate():
            await asyncio.sleep(0.005)
    await asyncio.wait_for(wait(), limit)


def test_a_quiet_account_is_live_only_while_its_cycles_keep_completing(tmp_path, monkeypatch):
    """대상이 0 이어도 생존은 **주기 완주 시각**으로 잰다(P0-3 S-A 처분 2).

    예전 술어에는 "기다리는 대상이 하나도 없으면 통과" 예외가 있었다 — 미해결 주문이
    없는 조용한 아침에 주기가 통째로 멈춰도 첫 자동 매수가 증거 생산자 없이 나갔다.
    여기서 멈추는 것은 가짜 collect 가 아니라 **주기 자신**이다(대상이 0 이라 collect 는
    아예 불리지 않는다). 주기가 다시 한 바퀴를 끝내면 같은 상태에서 다시 통과한다.
    """
    async def scenario():
        f = await fixture(tmp_path, monkeypatch)
        runtime, clock = f['runtime'], f['clock']
        gate, cycles = asyncio.Event(), []
        original = runtime._reconcile_cycle

        async def cycle(collect, deadline):
            cycles.append(1)
            if len(cycles) > 1:
                await gate.wait()   # 두 번째 주기부터 끝나지 않는다.
            return await original(collect, deadline)

        monkeypatch.setattr(runtime, '_reconcile_cycle', cycle)

        async def collect(**kwargs):
            raise AssertionError('대상이 0 인데 원장 TR 이 나갔다')

        try:
            runtime.start_reconciler(collect, interval=0.01, cycle_timeout=30.0)
            await _until(lambda: runtime._reconciler_cycle_completed_at is not None)
            assert runtime.reconciler_live(clock[0]) is True
            await _until(lambda: len(cycles) > 1)
            clock[0] = clock[0] + timedelta(
                seconds=RECONCILER_STALL_CYCLES * runtime._reconciler_interval + 1)
            # 주기 task 는 살아 있다 — 잡히는 것은 완주 시각뿐이다.
            assert runtime._reconciler_task.done() is False
            assert runtime.reconciler_blocked_reason() is None
            assert runtime.reconciler_live(clock[0]) is False
            request = f['request']()
            await f['quote'](request)
            with pytest.raises(CommandValidationError, match='reconciler_unavailable'):
                await f['commands'].prepare(request, f['entry'](request), sector='반도체')
            # 같은 상태에서 보호 매도는 이 게이트에 걸리지 않는다.
            sell = f['request']('S', side=OrderSide.SELL, quantity=5)
            with pytest.raises(CommandValidationError) as blocked:
                await f['commands'].prepare(sell, f['entry'](sell))
            assert 'reconciler_unavailable' not in str(blocked.value)
            gate.set()
            await _until(lambda: runtime._reconciler_cycle_completed_at == clock[0])
            assert runtime.reconciler_live(clock[0]) is True
        finally:
            gate.set()
            await runtime.shutdown()
            await f['store'].close()
    asyncio.run(scenario())


def test_a_manual_buy_is_stopped_by_the_same_gate(tmp_path, monkeypatch):
    """결정(P0-3 S-A 처분 6): 수동 BUY 도 `reconciler_unavailable` 로 막는다.

    생산자가 죽어 있으면 **수동** 매수의 체결도 관측되지 않아 owner 의 수량·예약이
    그만큼 틀어진다. 예외는 청산(SELL·CANCEL)뿐이다 — 증거가 없을수록 청산은 나가야 한다.
    """
    async def scenario():
        f = await fixture(tmp_path, monkeypatch)
        runtime = f['runtime']
        try:
            async def dead():
                return None

            task = asyncio.create_task(dead())
            await task
            runtime._reconciler_task = task
            runtime._reconciler_started_at = runtime._now()
            request = f['request']()
            context = f['entry'](request)
            assert context.origin is EntryOrigin.USER   # 코드의 이름은 USER, 전략은 'manual'
            await f['quote'](request)
            with pytest.raises(CommandValidationError, match='reconciler_unavailable'):
                await f['commands'].prepare(request, context, sector='반도체')
        finally:
            await f['store'].close()
    asyncio.run(scenario())


def test_an_unwired_producer_is_not_reported_as_an_unavailable_one(tmp_path, monkeypatch):
    """기록된 차이(Q-2 와 다른 곳): 생산자가 배선되지 않은 runtime 은 이 술어를 통과한다.

    그 구멍은 설치기가 닫는다 — `collect` 는 필수 인자이고 attach 마지막에 무조건
    `start_reconciler` 가 돈다. 여기에 걸면 수집기를 세우지 않는 기존 owner 인수가 전부
    `reconciler_unavailable` 이 된다(P0-3 보고서의 변경 요청 1건).
    """
    async def scenario():
        f = await fixture(tmp_path, monkeypatch)
        try:
            assert f['runtime']._reconciler_started_at is None
            assert f['runtime'].reconciler_live(f['clock'][0]) is True
            await _buy(f, 'A')
        finally:
            await f['store'].close()
    asyncio.run(scenario())


# ---------------------------------------------------------------- ⑥⑦ 설치~run 창


def _observation(runtime):
    from src.execution.safety.application import observation_from_evidence
    from src.execution.safety.lifecycle import OrderEvidence, OrderRef
    ref = OrderRef('test-scope', 'KR', '2026-09-18', 'KRX', 'p03-1')
    evidence = OrderEvidence(ref, '005930', 'buy', 100, 40, D('400000'))
    return observation_from_evidence(evidence, trading_day='2026-09-18', metadata=None)


def test_the_install_to_run_window_applies_nothing_and_names_the_skip(tmp_path, monkeypatch):
    """engine.run 전에는 apply 를 부르지 않는다 — 새 ingress 행 0, skip 사유는 명명된다."""
    async def scenario():
        f = await fixture(tmp_path, monkeypatch)
        runtime, engine = f['runtime'], f['engine']
        try:
            assert engine.running is False
            calls = []

            async def apply(observation):
                calls.append(observation)
                return InboxReceipt(observation.observation_id, 'RECEIVED', 1, True)

            engine.apply_execution_observation = apply
            observation = _observation(runtime)
            before = len(engine._execution_ingress)
            for _ in range(3):
                assert await runtime._reapply(observation) is False
            assert calls == []
            assert len(engine._execution_ingress) == before
            assert runtime.health()['reconciler']['skipped']['engine_not_running'] == 3
            assert runtime.health()['reconciler']['last_reason'] == 'engine_not_running'
            # run 이 시작하면 같은 행을 다음 주기가 처리한다.
            engine.running = True
            assert await runtime._reapply(observation) is True
            assert len(calls) == 1
        finally:
            engine.running = False
            await f['store'].close()
    asyncio.run(scenario())


def test_the_apply_window_is_measured_inside_the_owner_task(tmp_path, monkeypatch):
    """적용 경과 2키. 대기자가 취소돼도 task 본문이 이미 기록했다."""
    async def scenario():
        f = await fixture(tmp_path, monkeypatch)
        runtime, engine = f['runtime'], f['engine']
        try:
            engine.running = True
            reported = runtime.health()['reconciler']
            assert reported['apply_seconds_last'] is None and reported['apply_seconds_max'] is None

            async def apply(observation):
                await asyncio.sleep(0.02)
                return InboxReceipt(observation.observation_id, 'RECEIVED', 1, True)

            engine.apply_execution_observation = apply
            observation = _observation(runtime)
            assert await runtime._reapply(observation) is True
            first = runtime.health()['reconciler']
            assert first['apply_seconds_last'] >= 0.02
            assert first['apply_seconds_max'] == first['apply_seconds_last']

            async def quick(observation):
                return InboxReceipt(observation.observation_id, 'RECEIVED', 1, True)

            engine.apply_execution_observation = quick
            assert await runtime._reapply(observation) is True
            second = runtime.health()['reconciler']
            assert second['apply_seconds_last'] < first['apply_seconds_last']
            assert second['apply_seconds_max'] == first['apply_seconds_max']

            # 실패한 적용도 창에서 사라지지 않는다 — 앞선 성공의 값이 남은 것으로 읽히지
            # 않게 마지막 값을 지운 뒤, 실패가 **자기 값**을 다시 쓰는지를 본다.
            async def raises(observation):
                await asyncio.sleep(0.02)
                raise RuntimeError('p03')

            engine.apply_execution_observation = raises
            runtime._reconciler_apply_seconds_last = None
            assert await runtime._reapply(observation) is False
            assert runtime.health()['reconciler']['apply_seconds_last'] >= 0.02
        finally:
            engine.running = False
            await f['store'].close()
    asyncio.run(scenario())


# ---------------------------------------------------------------- ①②③ 설치기 배선


async def _acknowledged(runtime):
    """설치 뒤에도 남는 미해결 SUBMIT(ACK 까지 간 주문 = 주기의 조회 대상) 하나."""
    from src.execution.safety.lifecycle import CommandResult, CommandStatus, OrderRef
    ref = OrderRef(SCOPE, 'KR', NOW_KST.date().isoformat(), 'KRX', '9100003')
    await runtime.lifecycle.prepare('i-p03', 'a-p03', 10, '005930', 'buy',
                                    strategy='sepa_trend', reserved_cash='100000')
    await runtime.lifecycle.claim('a-p03', 'sender')
    await runtime.lifecycle.record_result('a-p03', 'sender', CommandResult(
        CommandStatus.ACKNOWLEDGED, 'a-p03', ref))


def test_the_installer_starts_the_producer_and_shutdown_drains_it(tmp_path, monkeypatch):
    """설치 성공 뒤 주기 task 1건, `shutdown()` 뒤 0건(미배수 task 경고 0).

    설치기가 넘긴 collect 의 **인자**까지 고정한다: 미해결 ACK 를 하나 심은 checkpoint 로
    설치해 조회가 실제로 나가게 하고, 그 한 번이 주기 계약(당일·ALL·3페이지) 그대로인지
    본다. 대상이 0 이면 조회 자체가 없으므로 `collected` 만 보는 단언은 항진이다.
    """
    async def scenario():
        path = await seed_checkpoint(tmp_path, mutate=_acknowledged)
        f = await target(tmp_path, monkeypatch, seed=False, path=path)
        runtime = f['runtime']
        try:
            assert runtime._reconciler_tasks == set()
            gateway = await f['install']()
            assert runtime.gateway is gateway
            assert len(runtime._reconciler_tasks) == 1
            assert runtime.health()['reconciler']['reconciler_running'] is True
            day = NOW_KST.date().isoformat()
            await _until(lambda: f['collected'] != [])
            assert f['collected'][0] == {'start_date': day, 'end_date': day,
                                         'exchange_scope': 'ALL', 'max_pages': 3}
            await runtime.shutdown()
            assert runtime._reconciler_tasks == set()
        finally:
            await f['store'].close()
    asyncio.run(scenario())


@pytest.mark.parametrize('wired', ['running', 'closing'])
def test_an_already_wired_producer_refuses_the_install_before_anything_is_touched(
        tmp_path, monkeypatch, wired):
    """설치기는 자기가 세울 생산자가 이미 있는 runtime 에 손을 대지 않는다(처분 5).

    `start_reconciler` 도 같은 두 상태를 거부하지만 그것은 attach **뒤**다 — live 를 바꾼
    뒤에 거부하면 호출자는 legacy 로 돌아갈 수 없다.
    """
    async def scenario():
        f = await target(tmp_path, monkeypatch)
        runtime = f['runtime']
        try:
            if wired == 'running':
                runtime.start_reconciler(f['collect'], interval=100.0, cycle_timeout=5.0)
            else:
                runtime._closing = True
            before = live_snapshot(f)
            with pytest.raises(ApplicationBlocked, match='execution_producer_already_wired'):
                await f['install']()
            assert_untouched(f, before)
        finally:
            runtime._closing = False
            await f['teardown']()
    asyncio.run(scenario())


def test_a_non_production_query_environment_is_refused_before_anything_is_touched(
        tmp_path, monkeypatch):
    async def scenario():
        f = await target(tmp_path, monkeypatch)
        try:
            f['broker'].config.env = 'dev'
            before = live_snapshot(f)
            with pytest.raises(ApplicationBlocked, match='execution_query_environment_required'):
                await f['install']()
            assert_untouched(f, before)
        finally:
            await f['teardown']()
    asyncio.run(scenario())


def test_an_uncallable_collector_is_refused_before_anything_is_touched(tmp_path, monkeypatch):
    async def scenario():
        f = await target(tmp_path, monkeypatch)
        try:
            before = live_snapshot(f)
            with pytest.raises(ValueError, match='invalid_execution_collector'):
                await f['install'](collect=None)
            assert_untouched(f, before)
        finally:
            await f['teardown']()
    asyncio.run(scenario())


def test_the_shutdown_contract_is_written_down_for_the_caller():
    """Q-7: 호출자가 설치와 run task 생성을 한 try/finally 로 묶어야 한다는 계약."""
    from src.execution.safety.factory import install_attached_runtime
    doc = install_attached_runtime.__doc__
    assert 'try/finally' in doc and '_shutdown()' in doc
