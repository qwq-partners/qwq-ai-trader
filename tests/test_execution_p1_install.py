"""P1 설치기: 실제 시세 큐→생산자→엔진→owner→합성 HTTP와 체결 큐 인수."""
import asyncio
from copy import deepcopy
from datetime import timedelta
from decimal import Decimal as D

import pytest

import src.execution.safety.factory as factory
from src.core.engine import StrategyManager
from src.core.event import EventType, MarketDataEvent
from src.execution.safety.application import ApplicationBlocked, FillObservation
from src.execution.safety.lifecycle import OrderEvidence, OrderRef, OrderState
# 동결 fixture 이전에 import하여 제품 datetime 타입 검증을 보존한다.
from src.execution.safety.protection_producer import ProtectionProducer
from src.execution.safety.runtime import KRExecutionRuntime
from src.execution.safety.guards import GuardDecision
from test_execution_install_factory import (
    NOW_KST, SCOPE, _CLOCK, assert_untouched, filled_position_then_quote,
    live_snapshot, mirror_restore, saved_state, synthetic_home, target,
)
from test_execution_p1_resume import fail_quote
from test_execution_runtime import queued
from test_execution_signal_gateway_acceptance import sell_signal

SYM = '005930'


def tick(price='9000'):
    return MarketDataEvent(symbol=SYM, close=D(price), high=D(price), low=D(price), source='rest')


async def holding_target(tmp_path, monkeypatch, *, mutate=None):
    async def holding(runtime):
        await filled_position_then_quote(runtime)
        if mutate is not None:
            await mutate(runtime)
    f = await target(tmp_path, monkeypatch, mutate=holding)
    await mirror_restore(f)
    return f


def test_real_queue_stop_follows_repeated_general_rejections_without_delay(tmp_path, monkeypatch):
    """실제 생산자·큐가 일반 거부의 공통 cooldown 때문에 손절100주를 보류하면 실패한다."""
    async def scenario():
        f = await holding_target(tmp_path, monkeypatch)
        monkeypatch.setattr(KRExecutionRuntime, 'trading_ready', property(lambda self: True))
        try:
            await f['install']()
            for index in range(3):
                if index:
                    _CLOCK['kst'] += timedelta(seconds=31)
                f['commands'].session_guard = lambda request: GuardDecision(False, 'synthetic-general-reject')
                await f['drive'](sell_signal(quantity=10))
                assert len(f['error_events']) == index + 1
                assert f['posts']() == []
            f['commands'].session_guard = lambda request: GuardDecision(True, 'synthetic-open')
            await f['drive'](tick())
            assert len(f['posts']()) == 1
            body = f['posts']()[0][1]['json']
            assert (body['ORD_DVSN'], body['ORD_UNPR'], body['ORD_QTY']) == ('01', '0', '100')
            runtime = f['runtime']
            retained = runtime.health()['protection_producer']['retained_decisions'][SYM]
            sale, = [row for row in runtime.owner.state['attempts'].values() if row['side'] == 'sell']
            assert sale['intent_id'] == retained['intent_id']
            assert sale['quantity'] == sale['reserved_quantity'] == retained['decision'][1] == 100
            original = deepcopy(runtime.owner.state['attempts'])
            _CLOCK['kst'] += timedelta(seconds=31)
            await f['drive'](tick())
            assert len(f['posts']()) == 1
            assert runtime.owner.state['attempts'] == original
            assert f['broker'].direct_calls == []
        finally:
            await f['teardown']()
    asyncio.run(scenario())


@pytest.mark.parametrize('value', [None, {}, 0, 'cache'])
def test_invalid_indicator_source_leaves_live_owner_and_checkpoint_untouched(tmp_path, monkeypatch, value):
    """검사를 restore 뒤로 옮기면 게시본·핸들러·저장본 무변경 계약이 깨진다."""
    async def scenario():
        f = await target(tmp_path, monkeypatch)
        try:
            before, saved = live_snapshot(f), await saved_state(f)
            with pytest.raises(ValueError, match='invalid_protection_indicator_source'):
                await f['install'](indicator_source=value)
            assert_untouched(f, before)
            assert await saved_state(f) == saved
        finally:
            await f['teardown']()
    asyncio.run(scenario())


def test_indicator_source_is_required_and_unwired_health_is_read_only(tmp_path, monkeypatch):
    async def scenario():
        f = await target(tmp_path, monkeypatch)
        try:
            before = live_snapshot(f)
            kwargs = dict(f['kwargs'])
            kwargs.pop('indicator_source')
            with pytest.raises(TypeError):
                await factory.install_attached_runtime(f['runtime'], f['commands'], **kwargs)
            assert f['runtime'].health()['protection_producer'] is None
            assert_untouched(f, before)
        finally:
            await f['teardown']()
    asyncio.run(scenario())


def test_install_runs_one_sweep_after_all_bindings_and_before_return(tmp_path, monkeypatch):
    """기동 sweep 누락·선행·중복 실행은 설치 경계의 관측 순서를 깨뜨린다."""
    async def scenario():
        f = await target(tmp_path, monkeypatch)
        calls = []
        original = ProtectionProducer.sweep
        async def sweep(producer):
            runtime = producer.runtime
            calls.append((runtime.engine._execution_runtime is runtime,
                          runtime.gateway is not None, runtime._reconciler_task is not None,
                          runtime._protection_producer is producer,
                          runtime.engine._handlers[EventType.MARKET_DATA][0] == producer.on_market_data))
            return await original(producer)
        monkeypatch.setattr(ProtectionProducer, 'sweep', sweep)
        try:
            await f['install']()
            assert calls == [(True, True, True, True, True)]
            runtime = f['runtime']
            before = (runtime.owner.version, deepcopy(runtime.owner.state), live_snapshot(f))
            health = runtime.health()
            assert health['protection_producer']['active_tasks'] == 0
            assert health['trading_ready'] is False and runtime.trading_ready is False
            health['protection_producer']['resume_dispositions']['synthetic'] = 9
            assert 'synthetic' not in runtime.health()['protection_producer']['resume_dispositions']
            assert (runtime.owner.version, runtime.owner.state, live_snapshot(f)) == before
            with pytest.raises(ApplicationBlocked, match='execution_runtime_already_restored'):
                await f['install']()
            assert calls == [(True, True, True, True, True)]
            assert live_snapshot(f) == before[2]
        finally:
            await f['teardown']()
    asyncio.run(scenario())


def test_real_market_handler_protects_before_strategy_and_profit_fill_then_stop_sends_ninety(tmp_path, monkeypatch):
    """핸들러 후순위·실엔진 경로 누락·수량 전량 보정은 wire와 전략 관측을 깨뜨린다."""
    async def scenario():
        f = await holding_target(tmp_path, monkeypatch)
        seen = []
        class Strategy:
            async def on_market_data(self, event, *, position):
                seen.append((event.close, len(f['posts']()), position.quantity))
                return None
        StrategyManager(f['engine']).register_strategy('설치기인수', Strategy())
        # 기존 실제 gateway 인수와 같은 합성 startup 허가. 제품 health는 계속 False다.
        monkeypatch.setattr(KRExecutionRuntime, 'trading_ready', property(lambda self: True))
        try:
            await f['install']()
            await f['drive'](tick('11200'))
            assert seen == [(D('11200'), 1, 100)]
            runtime = f['runtime']
            sales = [(key, row) for key, row in runtime.owner.state['attempts'].items() if row['side'] == 'sell']
            assert len(sales) == 1
            first_id, first = sales[0]
            assert first['quantity'] == first['reserved_quantity'] == 10
            ref = OrderRef.from_dict(first['order_ref'])
            now = _CLOCK['kst']
            day = now.date().isoformat()
            evidence = OrderEvidence(ref, SYM, 'sell', 10, 10, D('112000'), 0, 0,
                OrderState.FINAL_FILLED, complete=True, supported_finality=True,
                source_contract='synthetic-install', observed_at=now,
                request_started_at=now - timedelta(seconds=1), query_scope={
                    'account_scope': SCOPE, 'market': 'KR', 'exchange': 'KRX',
                    'start_date': day, 'end_date': day, 'tr_id': 'TTTC0081R',
                    'query_kind': 'all', 'session': 'regular'})
            assert await runtime.lifecycle.reconcile(first_id, evidence)
            # 시계가 만료되어도 관측10주가 경제에 적용되기 전에는 원 pending을 보존한다.
            _CLOCK['kst'] += timedelta(seconds=31)
            await f['drive'](tick())
            observed = runtime.owner.state
            assert len(f['posts']()) == 1
            assert observed['attempts'][first_id]['observed_quantity'] == 10
            assert observed['attempts'][first_id]['applied_quantity'] == 0
            assert observed['protection']['pending_owners'][SYM] == first['intent_id']
            assert observed['protection']['states'][SYM]['pending_target_qty'] == 10
            observation = FillObservation(SCOPE, 'KR', day, 'KRX', ref.order_no, SYM,
                'SELL', 10, D('112000'), org_no=ref.org_no)
            receipt = await queued(f['engine'], observation)
            assert (receipt.status, receipt.protection_status) == ('APPLIED', 'ready')
            assert f['engine'].portfolio.positions[SYM].quantity == 90
            # 매수 1,000,000+수수료141, 매도 112,000-수수료/세금239.
            assert f['engine'].portfolio.cash == D('1111620')
            assert f['exits'].get_state(SYM).remaining_quantity == 90
            assert runtime.owner.state['attempts'][first_id]['applied_quantity'] == 10
            # 엔진의 정상 30초 cooldown을 우회하지 않고 주입 시각을 전진시킨다.
            _CLOCK['kst'] += timedelta(seconds=31)
            await f['drive'](tick())
            bodies = [row[1]['json'] for row in f['posts']()]
            assert [(row['ORD_DVSN'], row['ORD_UNPR'], row['ORD_QTY']) for row in bodies] == [
                ('01', '0', '10'), ('01', '0', '90')]
            state = runtime.owner.state
            sales = [row for row in state['attempts'].values() if row['side'] == 'sell']
            assert len(sales) == 2
            second = next(row for row in sales if row['intent_id'] != first['intent_id'])
            assert state['intents'][first['intent_id']]['target_quantity'] == 10
            assert state['intents'][second['intent_id']]['target_quantity'] == 90
            assert seen[-1] == (D('9000'), 2, 90)
            assert f['engine'].stats.errors_count == 0 and f['error_events'] == []
            assert f['broker'].direct_calls == []
            assert runtime.health()['protection_producer']['last_tick_at'] == _CLOCK['kst'].isoformat()
        finally:
            await f['teardown']()
    asyncio.run(scenario())


def test_startup_sweep_recovers_original_admission_and_retains_unsent_decision(tmp_path, monkeypatch):
    async def scenario():
        async def admission(runtime):
            await fail_quote(runtime, runtime.owner.store, monkeypatch, price='11200')
        f = await holding_target(tmp_path, monkeypatch, mutate=admission)
        try:
            await f['install']()
            runtime = f['runtime']
            state = runtime.owner.state
            assert state['protection_quote_admissions'] == {}
            assert state['protection']['pending_owners'][SYM] == 'stop-' + SYM
            health = runtime.health()['protection_producer']
            assert health['resume_dispositions'] == {'protection_admission_resumed': 1}
            assert health['retained_decisions'][SYM]['intent_id'] == 'stop-' + SYM
            assert f['posts']() == []
            assert runtime.trading_ready is False
        finally:
            await f['teardown']()
    asyncio.run(scenario())


def test_startup_unsubmitted_audit_is_observable_recovery_hold_not_install_exception(tmp_path, monkeypatch):
    async def scenario():
        async def decision(runtime):
            assert (await runtime.quote(SYM, D('11200'), intent_id='original-profit'))[:2] == ('sell_partial', 10)
        f = await holding_target(tmp_path, monkeypatch, mutate=decision)
        try:
            await f['install']()
            runtime = f['runtime']
            health = runtime.health()['protection_producer']
            assert health['recovery_required'][SYM]['reason'] == 'unsubmitted_protection_decision'
            assert health['recovery_required'][SYM]['intent_ids'] == ['original-profit']
            assert runtime.owner.state['protection']['pending_owners'][SYM] == 'original-profit'
            assert runtime.trading_ready is False and f['posts']() == []
        finally:
            await f['teardown']()
    asyncio.run(scenario())


def test_startup_sweep_releases_true_orphan_without_touching_position(tmp_path, monkeypatch):
    async def scenario():
        async def orphan(runtime):
            await runtime.quote(SYM, D('11200'), intent_id='synthetic-orphan')
            def without_audit(state):
                state['outbox'] = {}
                return state
            await runtime.owner.mutate('synthetic-orphan-without-audit', without_audit)
        f = await holding_target(tmp_path, monkeypatch, mutate=orphan)
        try:
            await f['install']()
            runtime = f['runtime']
            assert runtime.owner.state['protection']['pending_owners'] == {}
            assert f['exits'].get_state(SYM).remaining_quantity == 100
            health = runtime.health()['protection_producer']
            assert health['pending_released'] == 1 and health['recovery_required'] == {}
            assert f['posts']() == []
        finally:
            await f['teardown']()
    asyncio.run(scenario())


@pytest.mark.parametrize('failure', ['constructor', 'sweep'])
def test_phase_two_producer_failure_keeps_attached_runtime_and_gateway(tmp_path, monkeypatch, failure):
    async def scenario():
        f = await target(tmp_path, monkeypatch)
        def broken_init(self, *args, **kwargs):
            raise TypeError('합성 생산자 생성 실패')
        async def broken_sweep(self):
            raise TypeError('합성 생산자 복구 실패')
        monkeypatch.setattr(ProtectionProducer, '__init__' if failure == 'constructor' else 'sweep',
                            broken_init if failure == 'constructor' else broken_sweep)
        try:
            with pytest.raises(TypeError, match='합성 생산자'):
                await f['install']()
            runtime = f['runtime']
            assert f['engine']._execution_runtime is runtime
            assert runtime.gateway is not None and runtime._reconciler_task is not None
            assert runtime.owner.version > 0 and 'entry_policy_context' in runtime.owner.state
            with pytest.raises(ApplicationBlocked, match='execution_runtime_already_restored'):
                await f['install']()
            assert f['posts']() == []
        finally:
            await f['teardown']()
    asyncio.run(scenario())


def test_cancelled_install_sweep_is_drained_by_existing_runtime_shutdown(tmp_path, monkeypatch):
    """설치 caller 취소 뒤 shield 작업이 기존 command_scope drain에 남아 있어야 한다."""
    async def scenario():
        f = await target(tmp_path, monkeypatch)
        entered, release = asyncio.Event(), asyncio.Event()
        tasks = []
        original = ProtectionProducer._sweep
        async def gated(self, *args, **kwargs):
            entered.set()
            await release.wait()
            return await original(self, *args, **kwargs)
        monkeypatch.setattr(ProtectionProducer, '_sweep', gated)
        try:
            install = asyncio.create_task(f['install']())
            tasks.append(install)
            await asyncio.wait_for(entered.wait(), 2)
            install.cancel()
            with pytest.raises(asyncio.CancelledError):
                await install
            shutdown = asyncio.create_task(f['runtime'].shutdown())
            tasks.append(shutdown)
            await asyncio.sleep(0)
            assert not shutdown.done()
            assert f['runtime'].health()['command_operations_pending'] >= 1
            release.set()
            await asyncio.wait_for(shutdown, 2)
            assert f['runtime'].health()['protection_producer']['active_tasks'] == 0
            assert f['runtime'].health()['protection_producer']['closing'] is True
            assert f['posts']() == []
        finally:
            release.set()
            await asyncio.gather(*tasks, return_exceptions=True)
            await f['teardown']()
    asyncio.run(scenario())
