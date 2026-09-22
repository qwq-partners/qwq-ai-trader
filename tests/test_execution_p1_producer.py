"""P1 S2 생산자: 실제 SQLite/owner/gateway, 엔진 인수만 S3 경계 어댑터."""
import asyncio
from copy import deepcopy
from datetime import timedelta, timezone
from decimal import Decimal as D

import pytest

from src.core.event import MarketDataEvent
from src.core.types import Order, OrderSide, OrderType
from src.execution.safety.application import ApplicationBlocked
from src.execution.safety.lifecycle import CommandResult, CommandStatus
from src.execution.safety.protection_producer import ProtectionProducer as Producer
from test_execution_command_owner import fixture as command_fixture, held
from test_execution_market_source import market_event
from test_execution_p1_gateway import apply_take_profit_fill
from test_execution_p1_resume import fail_quote
from test_execution_signal_gateway import install, synthetic_home  # noqa: F401
from test_cross_validator_characterization import freeze  # noqa: F401

SYM = '005930'


async def fixture(tmp_path, monkeypatch, *, quantity=100, indicators=None):
    f = await install(await command_fixture(tmp_path, monkeypatch), monkeypatch)
    if quantity > 0:
        await held(f, quantity=quantity)
    f['events'], f['results'] = [], []
    async def submit(event):
        # 실제 엔진은 항상 None을 반환한다. S3의 주문 인수 경계만 명시적으로 번역한다.
        f['events'].append(event)
        typ = OrderType(event.metadata['order_type'])
        order = Order(symbol=event.symbol, side=event.side, order_type=typ,
            quantity=event.metadata['quantity'], price=event.price if typ is OrderType.LIMIT else None,
            strategy=event.strategy.value, reason=event.reason)
        f['results'].append(await f['gateway'].submit(event, order, None))
    monkeypatch.setattr(f['engine'], '_submit_signal', submit)
    f['producer'] = Producer(f['runtime'], clock=lambda: f['clock'][0],
        indicator_source=lambda symbol: {} if indicators is None else indicators)
    return f


def tick(price='9000', symbol=SYM):
    return MarketDataEvent(symbol=symbol, close=D(price), high=D(price), low=D(price), source='rest')


async def close(f):
    await f['runtime'].shutdown()
    await f['store'].close()


def test_stop_is_consumed_synchronously_with_original_episode_and_market_body(tmp_path, monkeypatch, freeze):
    async def scenario():
        freeze(11, 0, day=18)
        f = await fixture(tmp_path, monkeypatch)
        try:
            await f['producer'].on_market_data(tick())
            state = f['runtime'].owner.state
            assert len(state['attempts']) == 1
            attempt = next(iter(state['attempts'].values()))
            signal = f['events'][0]
            assert attempt['intent_id'] == signal.metadata['protection_intent_id']
            assert attempt['quantity'] == attempt['reserved_quantity'] == 100
            assert signal.metadata['order_type'] == 'market'
            assert signal.metadata['exit_action'] == 'sell_all'
            assert attempt['request_binding']['fill_metadata']['exit_type'] == 'stop_loss'
            assert f['broker']._session.posts[0][1]['json']['ORD_DVSN'] == '01'
            assert f['broker']._session.posts[0][1]['json']['SLL_TYPE'] == '01'
            await f['producer'].on_market_data(tick())
            assert len(f['broker']._session.posts) == 1
        finally: await close(f)
    asyncio.run(scenario())


def test_rest_provenance_is_absent_and_throttle_cannot_delay_stop(tmp_path, monkeypatch, freeze):
    async def scenario():
        freeze(11, 0, day=18)
        f = await fixture(tmp_path, monkeypatch)
        try:
            await f['producer'].on_market_data(tick('10000'))
            before = f['runtime'].owner.version
            f['clock'][0] += timedelta(seconds=1)
            await f['producer'].on_market_data(tick('10100'))
            assert f['runtime'].owner.version == before
            f['clock'][0] += timedelta(seconds=1)
            await f['producer'].on_market_data(tick())
            state = f['runtime'].owner.state
            assert len(state['attempts']) == 1
            assert f['producer'].health()['throttled'] == 1
            row = state['quote_price_views'][SYM]
            assert (row['market_as_of'], row['source'], row['source_event_id']) == (None, None, None)
            assert 'market_sources' not in state
        finally: await close(f)
    asyncio.run(scenario())


@pytest.mark.parametrize('strategy,price,want', [
    ('gap_and_go', '9900', '갭EOD'), ('gap_and_go', '10000', None),
    ('theme_chasing', '10050', '테마EOD'), ('theme_chasing', '10100', None),
    ('theme_chasing', '10150', None), ('gap_and_go', '9000', '갭EOD')])
def test_eod_uses_tick_price_original_basis_and_precedes_quote(tmp_path, monkeypatch, freeze, strategy, price, want):
    async def scenario():
        freeze(15, 10, day=18)
        f = await fixture(tmp_path, monkeypatch)
        try:
            f['clock'][0] = f['clock'][0].replace(hour=15, minute=10)
            def strategy_update(state):
                state['portfolio']['positions'][SYM]['strategy'] = strategy
                return state
            await f['runtime'].owner.mutate('synthetic-strategy', strategy_update)
            await f['producer'].on_market_data(tick(price))
            if want is not None:
                assert len(f['runtime'].owner.state['attempts']) == 1
                assert f['events'][0].reason.startswith(want)
                assert f['events'][0].metadata['quantity'] == 100
                assert f['runtime'].owner.state.get('quote_price_views', {}) == {}
                await f['producer'].on_market_data(tick(price))
                assert len(f['broker']._session.posts) == 1
            else:
                assert f['runtime'].owner.state['attempts'] == {}
        finally: await close(f)
    asyncio.run(scenario())


@pytest.mark.parametrize('hour,minute,want', [(8,0,None),(8,50,None),(15,25,'limit'),
    (15,35,None),(16,0,None),(21,0,None)])
def test_sessions_use_kst_even_for_utc_clock(tmp_path, monkeypatch, freeze, hour, minute, want):
    async def scenario():
        freeze(hour, minute, day=18)
        f = await fixture(tmp_path, monkeypatch)
        try:
            local = f['clock'][0].replace(hour=hour, minute=minute)
            f['clock'][0] = local
            f['producer'].clock = lambda: local.astimezone(timezone.utc)
            before = f['runtime'].owner.version
            await f['producer'].on_market_data(tick())
            if want is None:
                assert f['runtime'].owner.version == before
                assert f['producer'].health()['blocked_reason'].startswith('unsupported_session:')
            else:
                assert f['events'][0].metadata['order_type'] == want
                assert len(f['runtime'].owner.state['attempts']) == 1
        finally: await close(f)
    asyncio.run(scenario())


def test_closed_admission_resume_retains_original_decision_across_sweeps(tmp_path, monkeypatch, freeze):
    async def scenario():
        freeze(11, 0, day=18)
        f = await fixture(tmp_path, monkeypatch)
        try:
            await fail_quote(f['runtime'], f['store'], monkeypatch, price='11200')
            f['runtime']._day_closed = True
            await f['producer'].sweep()
            await f['producer'].sweep()
            state = f['runtime'].owner.state
            assert state['protection_quote_admissions'] == {}
            assert state['protection']['pending_owners'][SYM] == 'stop-005930'
            assert len(f['producer'].health()['retained_decisions']) == 1
            assert state['attempts'] == {}
            f['runtime']._day_closed = False
            await f['producer'].on_market_data(tick('9000'))
            signal = f['events'][0]
            assert signal.metadata['protection_intent_id'] == 'stop-005930'
            assert signal.metadata['quantity'] == 10
            assert signal.price == D('11200')
        finally: await close(f)
    asyncio.run(scenario())


@pytest.mark.parametrize('indicator', ['ma5', 'prev_low'])
def test_ws_original_observation_and_cached_indicator_trigger_composite(tmp_path, monkeypatch, freeze, indicator):
    async def scenario():
        freeze(11, 0, day=18)
        f = await fixture(tmp_path, monkeypatch, indicators={indicator: D('10500')})
        try:
            def first_stage(state):
                state['protection']['states'][SYM]['current_stage'] = 'first'
                return state
            await f['runtime'].owner.mutate('synthetic-first-stage', first_stage)
            event = await market_event(monkeypatch, f['clock'][0], price='10300')
            await f['producer'].on_market_data(event)
            state = f['runtime'].owner.state
            assert len(state['attempts']) == 1
            assert '복합트레일링' in f['events'][0].reason
            proof = state['market_sources'][SYM]
            assert proof['observation']['source_event_id'] == event.observation.source_event_id
            assert state['quote_price_views'][SYM]['market_as_of'] == event.observation.market_as_of.isoformat()
            assert f['events'][0].metadata['quantity'] == 100
        finally: await close(f)
    asyncio.run(scenario())


def test_rest_ws_disconnect_reconnect_keeps_original_source_gates(tmp_path, monkeypatch, freeze):
    async def scenario():
        freeze(11, 0, day=18)
        f = await fixture(tmp_path, monkeypatch)
        try:
            await f['producer'].on_market_data(tick('10000'))
            f['clock'][0] += timedelta(seconds=20)
            ws1 = await market_event(monkeypatch, f['clock'][0], price='10010')
            await f['producer'].on_market_data(ws1)
            state = f['runtime'].owner.state
            assert state['market_sources'][SYM]['invalidated_at_version'] == 0
            f['clock'][0] += timedelta(seconds=20)
            await f['producer'].on_market_data(tick('10020'))
            state = f['runtime'].owner.state
            assert state['market_sources'][SYM]['invalidated_at_version'] > 0
            assert state['quote_price_views'][SYM]['market_as_of'] is None
            f['clock'][0] += timedelta(seconds=20)
            ws2 = await market_event(monkeypatch, f['clock'][0], price='10030')
            await f['producer'].on_market_data(ws2)
            state = f['runtime'].owner.state
            assert state['market_sources'][SYM]['invalidated_at_version'] == 0
            assert state['market_sources'][SYM]['observation']['source_event_id'] == ws2.observation.source_event_id
            assert f['producer'].health()['source_transitions'] == 3
            f['clock'][0] += timedelta(seconds=20)
            version = f['runtime'].owner.version
            with pytest.raises(ValueError, match='market_source_event_conflict'):
                await f['producer'].on_market_data(ws2)
            assert f['runtime'].owner.version == version
            f['clock'][0] += timedelta(seconds=20)
            await f['producer'].on_market_data(ws1)
            assert f['runtime'].owner.version == version
            assert f['producer'].health()['blocked_reason'] is not None
        finally: await close(f)
    asyncio.run(scenario())


@pytest.mark.parametrize('when', ['before', 'after_quote', 'eod'])
def test_published_exemption_blocks_every_submission_boundary(tmp_path, monkeypatch, freeze, when):
    async def scenario():
        freeze(15, 10, day=18)
        f = await fixture(tmp_path, monkeypatch)
        try:
            if when == 'eod':
                f['clock'][0] = f['clock'][0].replace(hour=15, minute=10)
                def gap(state):
                    state['portfolio']['positions'][SYM]['strategy'] = 'gap_and_go'
                    return state
                await f['runtime'].owner.mutate('synthetic-gap', gap)
            if when != 'after_quote':
                f['exits'].add_exit_exempt(SYM)
            else:
                quote = f['runtime'].quote
                async def exempt_after_quote(*args, **kwargs):
                    result = await quote(*args, **kwargs)
                    f['exits'].add_exit_exempt(SYM)
                    return result
                monkeypatch.setattr(f['runtime'], 'quote', exempt_after_quote)
            await f['producer'].on_market_data(tick())
            assert f['runtime'].owner.state['attempts'] == {}
            assert f['broker']._session.posts == []
            if when == 'after_quote':
                assert f['producer'].health()['retained_decisions'][SYM]['reason'] == 'exit_exempt'
        finally: await close(f)
    asyncio.run(scenario())


def test_degraded_tick_does_not_touch_durable_quote_or_evidence(tmp_path, monkeypatch):
    async def scenario():
        f = await fixture(tmp_path, monkeypatch)
        try:
            def degrade(state):
                state['protection']['degraded'][SYM] = {'quantity':100, 'reason':'protection_calculation_failed'}
                return state
            await f['runtime'].owner.mutate('synthetic-degraded', degrade)
            before, version = f['runtime'].owner.state, f['runtime'].owner.version
            await f['producer'].on_market_data(tick())
            assert f['runtime'].owner.state == before
            assert f['runtime'].owner.version == version
            assert f['producer'].health()['blocked_reason'] == 'protection_degraded'
        finally: await close(f)
    asyncio.run(scenario())


def test_open_buy_is_observed_without_owner_ready_and_keeps_stop_decision(tmp_path, monkeypatch):
    async def scenario():
        f = await fixture(tmp_path, monkeypatch)
        try:
            await f['runtime'].lifecycle.prepare('entry', 'entry-a', 10, SYM, 'buy')
            await f['producer'].on_market_data(tick())
            assert len(f['runtime'].owner.state['attempts']) == 1
            health = f['producer'].health()
            assert health['blocked_by_open_entry'] == [SYM]
            assert health['retained_decisions'][SYM]['decision'][:2] == ['sell_all', 100]
            assert health['retained_decisions'][SYM]['reason'] == 'blocked_by_open_entry'
            assert f['events'] == []
        finally: await close(f)
    asyncio.run(scenario())


@pytest.mark.parametrize('fault', ['blocked', 'type_error', 'no_evidence'])
def test_submission_none_and_expected_block_are_not_invented_success(tmp_path, monkeypatch, fault):
    async def scenario():
        f = await fixture(tmp_path, monkeypatch)
        try:
            async def stop(event):
                if fault == 'blocked': raise ApplicationBlocked('합성 게이트 차단')
                if fault == 'type_error': raise TypeError('합성 프로그래밍 오류')
            monkeypatch.setattr(f['engine'], '_submit_signal', stop)
            if fault == 'type_error':
                with pytest.raises(TypeError, match='합성 프로그래밍 오류'):
                    await f['producer'].on_market_data(tick('11200'))
                assert f['producer'].health()['last_error'] == 'TypeError'
            else:
                await f['producer'].on_market_data(tick('11200'))
            retained = deepcopy(f['producer'].health()['retained_decisions'][SYM])
            assert retained['intent_id'] == f['runtime'].owner.state['protection']['pending_owners'][SYM]
            assert f['runtime'].owner.state['attempts'] == {}
            if fault != 'type_error':
                await f['producer'].on_market_data(tick())
                assert f['producer'].health()['retained_decisions'][SYM] == retained
        finally: await close(f)
    asyncio.run(scenario())


def test_first_resumed_decision_survives_later_admission_failure(tmp_path, monkeypatch):
    async def scenario():
        f = await fixture(tmp_path, monkeypatch)
        try:
            first = await fail_quote(f['runtime'], f['store'], monkeypatch, price='11200')
            second = await fail_quote(f['runtime'], f['store'], monkeypatch, symbol='000660')
            commit = f['store'].commit
            async def break_second(expected, state, command):
                if command == 'command:' + second: raise OSError('합성 두번째 저장 오류')
                return await commit(expected, state, command)
            with monkeypatch.context() as patch:
                patch.setattr(f['store'], 'commit', break_second)
                with pytest.raises(OSError, match='두번째'):
                    await f['producer'].sweep()
            retained = f['producer'].health()['retained_decisions'][SYM]
            assert retained['intent_id'] == 'stop-005930'
            assert retained['command_id'] == first
            assert retained['decision'][:2] == ['sell_partial', 10]
            await f['runtime'].restore()
            f['runtime']._day_closed = True
            await f['producer'].sweep()
            assert f['runtime'].owner.state['protection']['pending_owners'][SYM] == 'stop-005930'
        finally: await close(f)
    asyncio.run(scenario())


def test_shared_lock_preserves_quote_prepare_window_under_sweep_and_caller_cancel(tmp_path, monkeypatch, freeze):
    async def scenario():
        freeze(11,0,day=18)
        f = await fixture(tmp_path, monkeypatch)
        entered, release = asyncio.Event(), asyncio.Event()
        tasks = []
        try:
            submit = f['engine']._submit_signal
            async def pause(event):
                entered.set()
                await release.wait()
                await submit(event)
            monkeypatch.setattr(f['engine'], '_submit_signal', pause)
            caller = asyncio.create_task(f['producer'].on_market_data(tick('11200')))
            tasks.append(caller)
            await asyncio.wait_for(entered.wait(), 2)
            original = f['runtime'].owner.state['protection']['pending_owners'][SYM]
            concurrent = asyncio.create_task(f['producer'].sweep())
            tasks.append(concurrent)
            await asyncio.sleep(0)
            caller.cancel()
            with pytest.raises(asyncio.CancelledError): await caller
            assert not concurrent.done()
            assert f['runtime'].owner.state['protection']['pending_owners'][SYM] == original
            assert f['runtime'].health()['command_operations_pending'] >= 1
            release.set()
            await asyncio.wait_for(concurrent, 2)
            assert f['runtime'].owner.state['intents'][original]['target_quantity'] == 10
            assert len(f['broker']._session.posts) == 1
        finally:
            release.set()
            await asyncio.gather(*tasks, return_exceptions=True)
            await close(f)
    asyncio.run(scenario())


def test_shutdown_drains_shielded_submit_and_refuses_new_ticks(tmp_path, monkeypatch):
    async def scenario():
        f = await fixture(tmp_path, monkeypatch)
        entered, release = asyncio.Event(), asyncio.Event()
        tasks = []
        try:
            async def pause(event):
                entered.set()
                await release.wait()
            monkeypatch.setattr(f['engine'], '_submit_signal', pause)
            caller = asyncio.create_task(f['producer'].on_market_data(tick('11200')))
            tasks.append(caller)
            await asyncio.wait_for(entered.wait(), 2)
            caller.cancel()
            with pytest.raises(asyncio.CancelledError): await caller
            shutdown = asyncio.create_task(f['runtime'].shutdown())
            tasks.append(shutdown)
            await asyncio.sleep(0)
            assert not shutdown.done()
            await f['producer'].on_market_data(tick())
            assert f['producer'].health()['blocked_reason'] == 'command_admission_closed'
            release.set()
            await asyncio.wait_for(shutdown, 2)
            assert f['producer'].health()['active_tasks'] == 0
            assert f['producer'].health()['closing']
        finally:
            release.set()
            await asyncio.gather(*tasks, return_exceptions=True)
            await close(f)
    asyncio.run(scenario())


@pytest.mark.parametrize('next_price,eod', [('9000', False), ('10050', True)])
def test_profit_fill_ten_then_new_protection_episode_sends_ninety(tmp_path, monkeypatch, freeze, next_price, eod):
    async def scenario():
        freeze(11,0,day=18)
        f = await fixture(tmp_path, monkeypatch)
        try:
            def basis(state):
                state['risk']['cost_basis_remaining'][SYM] = '1000000'
                state['risk']['buy_fee_remaining'][SYM] = '141'
                if eod: state['portfolio']['positions'][SYM]['strategy'] = 'theme_chasing'
                return state
            await f['runtime'].owner.mutate('synthetic-basis', basis)
            await f['producer'].on_market_data(tick('11200'))
            first = f['events'][0].metadata['protection_intent_id']
            assert f['runtime'].owner.state['protection']['pending_owners'][SYM] == first
            await apply_take_profit_fill(f, f['results'][0])
            if eod:
                freeze(15,10,day=18)
                f['clock'][0] = f['clock'][0].replace(hour=15,minute=10)
            f['broker']._session.response.data['output']['ODNO'] = '1234567891'
            await f['producer'].on_market_data(tick(next_price))
            assert len(f['runtime'].owner.state['attempts']) == 2
            second = f['events'][1].metadata['protection_intent_id']
            assert second != first
            assert f['events'][1].metadata['quantity'] == 90
            assert f['runtime'].owner.state['intents'][second]['target_quantity'] == 90
            assert f['runtime'].owner.state['intents'][first]['target_quantity'] == 10
        finally: await close(f)
    asyncio.run(scenario())


@pytest.mark.parametrize('status', [CommandStatus.NOT_SENT, CommandStatus.REJECTED, CommandStatus.UNKNOWN])
def test_only_confirmed_unfilled_rejection_can_start_new_episode_after_sixty_seconds(tmp_path, monkeypatch, freeze, status):
    async def scenario():
        freeze(11,0,day=18)
        f = await fixture(tmp_path, monkeypatch)
        try:
            if status is CommandStatus.NOT_SENT:
                f['ready'][0] = False
            else:
                f['broker']._session.response.data = ({'rt_cd':'1', 'msg_cd':'synthetic_reject'}
                    if status is CommandStatus.REJECTED else {'rt_cd':'unknown'})
            await f['producer'].on_market_data(tick('11200'))
            assert f['results'][0].status is status
            first = next(iter(f['runtime'].owner.state['intents']))
            f['clock'][0] += timedelta(seconds=59)
            await f['producer'].on_market_data(tick('11200'))
            assert len(f['runtime'].owner.state['intents']) == 1
            f['clock'][0] += timedelta(seconds=1)
            await f['producer'].on_market_data(tick('11200'))
            state = f['runtime'].owner.state
            if status is CommandStatus.UNKNOWN:
                assert len(state['intents']) == 1
                assert state['protection']['pending_owners'][SYM] == first
            else:
                assert len(state['intents']) == 2
                assert state['protection']['pending_owners'][SYM] != first
                assert f['producer'].health()['reemissions'] == 1
        finally: await close(f)
    asyncio.run(scenario())


@pytest.mark.parametrize('quantity', [0, -1, True, 10.0, 101, 90])
def test_invalid_or_inconsistent_full_quantity_never_reaches_submission(tmp_path, monkeypatch, quantity):
    async def scenario():
        f = await fixture(tmp_path, monkeypatch)
        try:
            quote = f['runtime'].quote
            async def corrupt_return(*args, **kwargs):
                decision = await quote(*args, **kwargs)
                return (decision[0], quantity, decision[2])
            monkeypatch.setattr(f['runtime'], 'quote', corrupt_return)
            await f['producer'].on_market_data(tick())
            assert f['runtime'].owner.state['attempts'] == {}
            assert f['events'] == []
            assert f['producer'].health()['blocked_reason'] == 'protection_quantity_mismatch'
        finally: await close(f)
    asyncio.run(scenario())


def test_other_symbol_unapplied_inbox_does_not_prevent_stop_calculation(tmp_path, monkeypatch, freeze):
    async def scenario():
        from src.execution.safety.application import FillObservation
        freeze(11,0,day=18)
        f = await fixture(tmp_path, monkeypatch)
        try:
            observation = FillObservation('test-scope', 'KR', '2026-09-18', 'KRX',
                'synthetic-other-fill', '000660', 'BUY', 1, D('10000'), org_no='12345')
            await f['runtime'].owner.receive(observation, ingress_context=f['runtime'].ingress_context(1))
            submit = f['engine']._submit_signal
            async def engine_exception_contract(event):
                try: await submit(event)
                except ValueError: pass
            monkeypatch.setattr(f['engine'], '_submit_signal', engine_exception_contract)
            await f['producer'].on_market_data(tick())
            state = f['runtime'].owner.state
            decisions = [row for row in state['outbox'].values() if row['kind'] == 'protection_decision']
            assert len(decisions) == 1
            assert decisions[0]['decision'][:2] == ['sell_all', 100]
            assert state['attempts'] == {}
            assert f['producer'].health()['retained_decisions'][SYM]['reason'] == 'submit_without_owner_evidence'
        finally: await close(f)
    asyncio.run(scenario())


def test_decision_free_ids_are_discarded_before_later_partial_episode(tmp_path, monkeypatch, freeze):
    async def scenario():
        freeze(11,0,day=18)
        f = await fixture(tmp_path, monkeypatch)
        try:
            ids = []
            quote = f['runtime'].quote
            async def observe_id(*args, **kwargs):
                ids.append(kwargs['intent_id'])
                return await quote(*args, **kwargs)
            monkeypatch.setattr(f['runtime'], 'quote', observe_id)
            await f['producer'].on_market_data(tick('10000'))
            assert f['producer'].health()['retained_decisions'] == {}
            await f['producer'].on_market_data(tick('11200'))
            assert len(ids) == 2 and ids[0] != ids[1]
            assert all(value.startswith('pp-i-') for value in ids)
            assert f['runtime'].owner.state['protection']['pending_owners'][SYM] == ids[1]
            assert f['runtime'].owner.state['intents'][ids[1]]['target_quantity'] == 10
            assert ids[0] not in f['runtime'].owner.state['intents']
        finally: await close(f)
    asyncio.run(scenario())


def test_heartbeat_separates_empty_idle_success_and_failure(tmp_path, monkeypatch):
    async def scenario():
        from src.utils import loop_heartbeat as hb
        monkeypatch.setattr(hb, '_states', {})
        monkeypatch.setattr(hb, '_beats', {})
        f = await fixture(tmp_path, monkeypatch, quantity=0)
        try:
            await f['producer'].on_market_data(tick())
            state = hb._states['kr_protection_producer']
            assert state.last_attempt is not None and state.idle_reason == '보유 종목 없음'
            assert state.last_success is None and state.last_failure is None
            await held(f)
            await f['producer'].on_market_data(tick('10000'))
            success = state.last_success
            assert success is not None and state.last_failure is None
            f['runtime']._day_closed = True
            await f['producer'].on_market_data(tick())
            assert state.last_failure is not None and state.last_success == success
        finally: await close(f)
    asyncio.run(scenario())


def test_cancelled_waiter_programming_error_is_retrieved_and_health_records_it(tmp_path, monkeypatch):
    async def scenario():
        f = await fixture(tmp_path, monkeypatch)
        entered, release = asyncio.Event(), asyncio.Event()
        errors = []
        loop = asyncio.get_running_loop()
        previous = loop.get_exception_handler()
        loop.set_exception_handler(lambda loop, context: errors.append(context))
        caller = None
        try:
            async def broken(event):
                entered.set()
                await release.wait()
                raise TypeError('합성 취소 후 프로그래밍 오류')
            monkeypatch.setattr(f['engine'], '_submit_signal', broken)
            caller = asyncio.create_task(f['producer'].on_market_data(tick()))
            await asyncio.wait_for(entered.wait(), 2)
            caller.cancel()
            with pytest.raises(asyncio.CancelledError): await caller
            release.set()
            await f['runtime'].shutdown()
            await asyncio.sleep(0)
            assert f['producer'].health()['last_error'] == 'TypeError'
            assert f['producer'].health()['active_tasks'] == 0
            assert errors == []
        finally:
            release.set()
            if caller is not None: await asyncio.gather(caller, return_exceptions=True)
            loop.set_exception_handler(previous)
            await close(f)
    asyncio.run(scenario())


def test_eight_symbol_synthetic_write_latency_and_high_watermark_comparison(tmp_path, monkeypatch):
    async def run(directory, interval):
        from time import perf_counter
        import src.execution.safety.protection_producer as module
        f = await fixture(directory, monkeypatch)
        symbols = ['005930', '000660', '035420', '035720', '005380', '051910', '006400', '068270']
        try:
            def eight(state):
                for symbol in symbols[1:]:
                    for key in ('portfolio', 'protection'):
                        rows = state[key]['positions' if key == 'portfolio' else 'states']
                        rows[symbol] = deepcopy(rows[SYM])
                        rows[symbol]['symbol'] = symbol
                    state['protection']['entry_times'][symbol] = state['protection']['entry_times'][SYM]
                return state
            await f['runtime'].owner.mutate('synthetic-eight-holdings', eight)
            timings = []
            commit = f['store'].commit
            async def timed(expected, state, command):
                started = perf_counter()
                result = await commit(expected, state, command)
                timings.append((perf_counter() - started) * 1000)
                return result
            with monkeypatch.context() as patch:
                patch.setattr(module, 'PROTECTION_QUOTE_INTERVAL', interval)
                patch.setattr(f['store'], 'commit', timed)
                start = perf_counter()
                for offset, price in [(0, '10000'), (1, '10100'), (20, '10050')]:
                    f['clock'][0] = f['clock'][0].replace(second=offset)
                    for symbol in symbols:
                        await f['producer'].on_market_data(tick(price, symbol))
                elapsed = (perf_counter() - start) * 1000
            highs = [D(f['runtime'].owner.state['protection']['states'][symbol]['highest_price']) for symbol in symbols]
            return {'writes':len(timings), 'write_mean_ms':sum(timings)/len(timings),
                'write_max_ms':max(timings), 'tick_mean_ms':elapsed/24, 'highs':highs,
                'throttled':f['producer'].health()['throttled']}
        finally: await close(f)
    async def scenario():
        throttled = await run(tmp_path / 'throttled', 20)
        control = await run(tmp_path / 'control', 0)
        assert throttled['writes'] == 32 and control['writes'] == 48
        assert throttled['throttled'] == 8
        assert throttled['highs'] == [D('10050')] * 8
        assert control['highs'] == [D('10100')] * 8
        print('합성 8종목/24틱 측정:', {'20초':throttled, '미스로틀':control})
    asyncio.run(scenario())


@pytest.mark.parametrize('price', ['9000', '11200'])
def test_recreated_producer_waits_sixty_seconds_for_confirmed_unfilled_episode(tmp_path, monkeypatch, freeze, price):
    async def scenario():
        freeze(11,0,day=18)
        f = await fixture(tmp_path, monkeypatch)
        try:
            f['broker']._session.response.data = {'rt_cd':'1', 'msg_cd':'synthetic_reject'}
            await f['producer'].on_market_data(tick(price))
            assert f['results'][0].status is CommandStatus.REJECTED
            original = next(iter(f['runtime'].owner.state['intents']))
            f['producer'] = Producer(f['runtime'], clock=lambda: f['clock'][0], indicator_source=lambda _: {})
            await f['producer'].on_market_data(tick(price))
            assert len(f['runtime'].owner.state['intents']) == 1
            assert f['producer'].health()['blocked_reason'] == 'restart_reemission_cooldown'
            f['clock'][0] += timedelta(seconds=59)
            await f['producer'].on_market_data(tick(price))
            assert len(f['runtime'].owner.state['intents']) == 1
            f['clock'][0] += timedelta(seconds=1)
            await f['producer'].on_market_data(tick(price))
            assert len(f['runtime'].owner.state['intents']) == 2
            assert f['producer'].health()['retained_decisions'][SYM]['intent_id'] != original
        finally: await close(f)
    asyncio.run(scenario())


def test_recreated_producer_does_not_delay_stop_after_applied_old_profit(tmp_path, monkeypatch, freeze):
    async def scenario():
        freeze(11,0,day=18)
        f = await fixture(tmp_path, monkeypatch)
        try:
            def basis(state):
                state['risk']['cost_basis_remaining'][SYM] = '1000000'
                state['risk']['buy_fee_remaining'][SYM] = '141'
                return state
            await f['runtime'].owner.mutate('synthetic-restart-basis', basis)
            await f['producer'].on_market_data(tick('11200'))
            await apply_take_profit_fill(f, f['results'][0])
            f['producer'] = Producer(f['runtime'], clock=lambda: f['clock'][0], indicator_source=lambda _: {})
            f['broker']._session.response.data['output']['ODNO'] = '1234567891'
            await f['producer'].on_market_data(tick())
            assert len(f['runtime'].owner.state['attempts']) == 2
            assert f['events'][1].metadata['quantity'] == 90
        finally: await close(f)
    asyncio.run(scenario())


def test_rejection_cooldown_starts_when_result_is_observed_not_when_tick_started(tmp_path, monkeypatch, freeze):
    async def scenario():
        freeze(11,0,day=18)
        f = await fixture(tmp_path, monkeypatch)
        try:
            async def slow_reject():
                f['clock'][0] += timedelta(seconds=61)
                return {'rt_cd':'1', 'msg_cd':'synthetic_reject'}
            monkeypatch.setattr(f['broker']._session.response, 'json', slow_reject)
            await f['producer'].on_market_data(tick())
            assert f['results'][0].status is CommandStatus.REJECTED
            await f['producer'].on_market_data(tick())
            assert len(f['runtime'].owner.state['intents']) == 1
        finally: await close(f)
    asyncio.run(scenario())


def test_session_is_rechecked_after_recovery_await_before_fresh_quote(tmp_path, monkeypatch):
    async def scenario():
        f = await fixture(tmp_path, monkeypatch)
        try:
            await fail_quote(f['runtime'], f['store'], monkeypatch, price='10000')
            resume = f['runtime'].resume_protection_admission
            async def crosses_session():
                result = await resume()
                f['clock'][0] = f['clock'][0].replace(hour=15, minute=35)
                return result
            monkeypatch.setattr(f['runtime'], 'resume_protection_admission', crosses_session)
            await f['producer'].on_market_data(tick())
            state = f['runtime'].owner.state
            assert state['quote_price_views'][SYM]['price'] == '10000'
            assert state['outbox'] == {}
            assert f['producer'].health()['blocked_reason'] == 'unsupported_session:break'
        finally: await close(f)
    asyncio.run(scenario())


def test_multiple_rejected_full_histories_without_ordering_are_observable_and_held(tmp_path, monkeypatch):
    async def scenario():
        f = await fixture(tmp_path, monkeypatch)
        try:
            for suffix in ('first', 'second'):
                identity, aid = 'pp-i-' + suffix, 'historical-' + suffix
                await f['runtime'].lifecycle.prepare(identity, aid, 100, SYM, 'sell')
                await f['runtime'].lifecycle.claim(aid, 'synthetic-sender')
                await f['runtime'].lifecycle.record_result(aid, 'synthetic-sender',
                    CommandResult(CommandStatus.REJECTED, aid))
            await f['producer'].on_market_data(tick())
            f['clock'][0] += timedelta(seconds=120)
            await f['producer'].on_market_data(tick())
            assert len(f['runtime'].owner.state['intents']) == 2
            health = f['producer'].health()
            assert health['blocked_reason'] == 'restart_episode_order_ambiguous'
            assert set(health['restart_retries'][SYM]['intent_ids']) == {'pp-i-first', 'pp-i-second'}
        finally: await close(f)
    asyncio.run(scenario())


def test_observed_profit_fill_keeps_original_pending_until_economic_application(tmp_path, monkeypatch, freeze):
    async def scenario():
        from src.execution.safety.lifecycle import OrderEvidence, OrderState
        freeze(11,0,day=18)
        f = await fixture(tmp_path, monkeypatch)
        try:
            await f['producer'].on_market_data(tick('11200'))
            result, now = f['results'][0], f['clock'][0]
            identity = f['events'][0].metadata['protection_intent_id']
            evidence = OrderEvidence(result.order_ref, SYM, 'sell', 10, 10, D('112000'), 0, 0,
                OrderState.FINAL_FILLED, complete=True, supported_finality=True,
                source_contract='synthetic-test', observed_at=now,
                request_started_at=now-timedelta(seconds=1), query_scope={
                    'account_scope':'test-scope', 'market':'KR', 'exchange':'KRX',
                    'start_date':'2026-09-18', 'end_date':'2026-09-18',
                    'tr_id':'TTTC0081R', 'query_kind':'all', 'session':'regular'})
            assert await f['runtime'].lifecycle.reconcile(result.attempt_id, evidence)
            f['clock'][0] += timedelta(seconds=61)
            await f['producer'].on_market_data(tick())
            state = f['runtime'].owner.state
            assert state['attempts'][result.attempt_id]['observed_quantity'] == 10
            assert state['attempts'][result.attempt_id]['applied_quantity'] == 0
            assert state['protection']['pending_owners'][SYM] == identity
            assert state['protection']['states'][SYM]['pending_target_qty'] == 10
            assert len(f['broker']._session.posts) == 1
        finally: await close(f)
    asyncio.run(scenario())


def test_gap_eod_begins_at_1510_and_not_one_minute_before(tmp_path, monkeypatch, freeze):
    async def scenario():
        freeze(15,9,day=18)
        f = await fixture(tmp_path, monkeypatch)
        try:
            def gap(state):
                state['portfolio']['positions'][SYM]['strategy'] = 'gap_and_go'
                return state
            await f['runtime'].owner.mutate('synthetic-gap-boundary', gap)
            f['clock'][0] = f['clock'][0].replace(hour=15,minute=9)
            await f['producer'].on_market_data(tick('9900'))
            assert f['runtime'].owner.state['attempts'] == {}
            f['clock'][0] += timedelta(minutes=1)
            freeze(15,10,day=18)
            await f['producer'].on_market_data(tick('9900'))
            assert len(f['runtime'].owner.state['attempts']) == 1
            assert f['events'][0].reason.startswith('갭EOD')
        finally: await close(f)
    asyncio.run(scenario())


def test_unknown_broker_response_is_failure_not_successful_handoff_heartbeat(tmp_path, monkeypatch, freeze):
    async def scenario():
        from src.utils import loop_heartbeat as hb
        freeze(11,0,day=18)
        monkeypatch.setattr(hb, '_states', {})
        f = await fixture(tmp_path, monkeypatch)
        try:
            f['broker']._session.response.data = {'rt_cd':'unknown'}
            await f['producer'].on_market_data(tick())
            assert f['results'][0].status is CommandStatus.UNKNOWN
            beat = hb._states['kr_protection_producer']
            assert beat.last_failure is not None and beat.last_success is None
            assert f['producer'].health()['retained_decisions'][SYM]['reason'] == 'episode_unknown'
        finally: await close(f)
    asyncio.run(scenario())
