"""S2 독립 지적 회귀: 원 증거 보존, EOD 시세/생성 경계, 관측 분류."""
import asyncio
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal as D

import pytest

from src.core.types import Order, OrderType
from src.execution.safety.lifecycle import OrderEvidence, OrderState
from src.utils import loop_heartbeat as heartbeat
from test_execution_p1_producer import fixture, close, tick, SYM, Producer
from test_execution_p1_gateway import apply_take_profit_fill
from test_execution_p1_resume import fail_quote
from test_execution_market_source import market_event
from test_execution_signal_gateway import synthetic_home  # noqa: F401
from test_cross_validator_characterization import freeze  # noqa: F401


async def strategy(f, name):
    def update(state):
        state['portfolio']['positions'][SYM]['strategy'] = name
        state['risk']['cost_basis_remaining'][SYM] = '1000000'
        state['risk']['buy_fee_remaining'][SYM] = '141'
        return state
    await f['runtime'].owner.mutate('synthetic-producer-strategy', update)


@pytest.mark.parametrize('name', ['manual', 'gap_and_go'])
def test_stale_ws_cannot_become_eod_order(tmp_path, monkeypatch, freeze, name):
    async def scenario():
        freeze(15,10,day=18)
        f = await fixture(tmp_path, monkeypatch)
        try:
            await strategy(f, name)
            f['clock'][0] = f['clock'][0].replace(hour=15,minute=10)
            fresh = await market_event(monkeypatch, f['clock'][0], price='10100')
            await f['producer'].on_market_data(fresh)
            f['clock'][0] += timedelta(seconds=20)
            stale = await market_event(monkeypatch, f['clock'][0]-timedelta(minutes=10), price='9900')
            before = f['runtime'].owner.state
            await f['producer'].on_market_data(stale)
            assert f['runtime'].owner.state == before
            assert f['runtime'].owner.state['attempts'] == {}
            assert f['broker']._session.posts == []
            assert f['producer'].health()['blocked_reason'] == 'stale_market_quote'
        finally: await close(f)
    asyncio.run(scenario())


def test_recovered_nonprefix_full_rejection_survives_recreation_cooldown(tmp_path, monkeypatch, freeze):
    async def scenario():
        freeze(11,0,day=18)
        f = await fixture(tmp_path, monkeypatch)
        try:
            f['broker']._session.response.data = {'rt_cd':'1', 'msg_cd':'synthetic_reject'}
            await fail_quote(f['runtime'], f['store'], monkeypatch, price='9000')
            await f['producer'].sweep()
            assert set(f['runtime'].owner.state['intents']) == {'stop-005930'}
            assert f['runtime'].owner.state['protection']['pending_owners'] == {}
            f['producer'] = Producer(f['runtime'], clock=lambda:f['clock'][0], indicator_source=lambda _: {})
            await f['producer'].on_market_data(tick())
            assert len(f['broker']._session.posts) == 1
            f['clock'][0] += timedelta(seconds=59)
            await f['producer'].on_market_data(tick())
            assert len(f['runtime'].owner.state['intents']) == 1
            f['clock'][0] += timedelta(seconds=1)
            await f['producer'].on_market_data(tick())
            assert len(f['runtime'].owner.state['intents']) == 2
            assert len(f['broker']._session.posts) == 2
        finally: await close(f)
    asyncio.run(scenario())


def test_recreation_preserves_unsubmitted_pending_with_durable_decision(tmp_path, monkeypatch):
    async def scenario():
        f = await fixture(tmp_path, monkeypatch)
        try:
            quote = f['runtime'].quote
            async def close_after_commit(*args, **kwargs):
                decision = await quote(*args, **kwargs)
                f['runtime']._day_closed = True
                return decision
            monkeypatch.setattr(f['runtime'], 'quote', close_after_commit)
            await f['producer'].on_market_data(tick('11200'))
            original = f['runtime'].owner.state['protection']['pending_owners'][SYM]
            f['producer'] = Producer(f['runtime'], clock=lambda:f['clock'][0], indicator_source=lambda _: {})
            await f['producer'].sweep()
            state = f['runtime'].owner.state
            assert state['protection']['pending_owners'][SYM] == original
            assert state['intents'] == state['protection_quote_admissions'] == {}
            recovery = f['producer'].health()['recovery_required'][SYM]
            assert recovery['reason'] == 'unsubmitted_protection_decision'
            assert recovery['intent_ids'] == [original]
            assert recovery['command_ids'] == list(state['outbox'])
            f['runtime']._day_closed = False
            await f['producer'].on_market_data(tick())
            assert f['runtime'].owner.state['protection']['pending_owners'][SYM] == original
            assert f['runtime'].owner.state['attempts'] == {}
            assert f['broker']._session.posts == []
        finally: await close(f)
    asyncio.run(scenario())


def test_recreation_holds_unsubmitted_full_audit_without_creating_new_order(tmp_path, monkeypatch, freeze):
    async def scenario():
        freeze(11,0,day=18)
        f = await fixture(tmp_path, monkeypatch)
        try:
            quote = f['runtime'].quote
            async def close_after_commit(*args, **kwargs):
                decision = await quote(*args, **kwargs)
                f['runtime']._day_closed = True
                return decision
            monkeypatch.setattr(f['runtime'], 'quote', close_after_commit)
            await f['producer'].on_market_data(tick())
            original = f['producer'].health()['retained_decisions'][SYM]['intent_id']
            assert f['runtime'].owner.state['protection']['pending_owners'] == {}
            f['producer'] = Producer(f['runtime'], clock=lambda:f['clock'][0], indicator_source=lambda _: {})
            f['runtime']._day_closed = False
            monkeypatch.setattr(f['runtime'], 'quote', quote)
            before = f['runtime'].owner.version
            await f['producer'].on_market_data(tick())
            assert f['runtime'].owner.version == before
            assert f['runtime'].owner.state['attempts'] == {}
            assert f['producer'].health()['recovery_required'][SYM]['intent_ids'] == [original]
            assert f['broker']._session.posts == []
        finally: await close(f)
    asyncio.run(scenario())


def test_existing_partial_sell_defers_new_eod_then_fresh_tick_sells_ninety(tmp_path, monkeypatch, freeze):
    async def scenario():
        freeze(11,0,day=18)
        f = await fixture(tmp_path, monkeypatch)
        try:
            await strategy(f, 'theme_chasing')
            await f['producer'].on_market_data(tick('11200'))
            first = f['results'][0]
            f['producer'] = Producer(f['runtime'], clock=lambda:f['clock'][0], indicator_source=lambda _: {})
            f['clock'][0] = f['clock'][0].replace(hour=15,minute=10)
            freeze(15,10,day=18)
            await f['producer'].on_market_data(tick('10050'))
            assert f['producer'].health()['retained_decisions'] == {}
            assert len(f['runtime'].owner.state['attempts']) == 1
            await apply_take_profit_fill(f, first)
            f['broker']._session.response.data['output']['ODNO'] = '1234567891'
            await f['producer'].on_market_data(tick('9000'))
            assert len(f['runtime'].owner.state['attempts']) == 2
            assert f['events'][1].metadata['quantity'] == 90
            assert f['broker']._session.posts[1][1]['json']['ORD_QTY'] == '90'
            assert f['events'][1].reason.startswith('테마EOD')
        finally: await close(f)
    asyncio.run(scenario())


def test_actual_gateway_quantity_must_match_retained_full_decision(tmp_path, monkeypatch, freeze):
    async def scenario():
        freeze(11,0,day=18)
        monkeypatch.setattr(heartbeat, '_states', {})
        f = await fixture(tmp_path, monkeypatch)
        try:
            async def mistranslated(event):
                f['events'].append(event)
                order = Order(symbol=event.symbol, side=event.side, order_type=OrderType.MARKET,
                    quantity=90, price=None, strategy=event.strategy.value, reason=event.reason)
                f['results'].append(await f['gateway'].submit(event, order, None))
            monkeypatch.setattr(f['engine'], '_submit_signal', mistranslated)
            await f['producer'].on_market_data(tick())
            identity = f['events'][0].metadata['protection_intent_id']
            assert f['runtime'].owner.state['intents'][identity]['target_quantity'] == 90
            assert f['broker']._session.posts[0][1]['json']['ORD_QTY'] == '90'
            retained = f['producer'].health()['retained_decisions'][SYM]
            assert retained['decision'][:2] == ['sell_all', 100]
            assert retained['reason'] == 'episode_evidence_mismatch'
            assert heartbeat._states['kr_protection_producer'].last_success is None
            assert heartbeat._states['kr_protection_producer'].last_failure is not None
        finally: await close(f)
    asyncio.run(scenario())


def test_acknowledged_pending_is_idle_without_failure_heartbeat(tmp_path, monkeypatch, freeze):
    async def scenario():
        freeze(11,0,day=18)
        monkeypatch.setattr(heartbeat, '_states', {})
        f = await fixture(tmp_path, monkeypatch)
        try:
            await f['producer'].on_market_data(tick())
            success = heartbeat._states['kr_protection_producer'].last_success
            await f['producer'].on_market_data(tick())
            beat = heartbeat._states['kr_protection_producer']
            assert beat.last_failure is None
            assert beat.last_success == success
            assert beat.idle_reason is not None
            assert f['producer'].health()['pending_reasons'][SYM] == 'episode_acknowledged_pending'
            assert len(f['broker']._session.posts) == 1
        finally: await close(f)
    asyncio.run(scenario())


def test_resume_invariant_failure_is_observable_with_since_and_original_ids(tmp_path, monkeypatch):
    async def scenario():
        f = await fixture(tmp_path, monkeypatch)
        try:
            command = await fail_quote(f['runtime'], f['store'], monkeypatch)
            def corrupt(state):
                state['protection_quote_admissions'][command]['payload_digest'] = 'wrong'
                return state
            await f['runtime'].owner.mutate('synthetic-invalid-admission', corrupt)
            await f['producer'].sweep()
            health = f['producer'].health()
            assert health['invariant_violation']['reason'] == 'protection_resume_blocked'
            assert health['invariant_violation']['since'] == f['clock'][0].isoformat()
            assert health['invariant_violation']['command_ids'] == [command]
            assert command in f['runtime'].owner.state['protection_quote_admissions']
            assert f['runtime'].owner.state['attempts'] == {}
        finally: await close(f)
    asyncio.run(scenario())


def test_scheduled_but_not_started_producer_fails_closed_after_shutdown(tmp_path, monkeypatch):
    async def scenario():
        f = await fixture(tmp_path, monkeypatch)
        entered, release = asyncio.Event(), asyncio.Event()
        caller = None
        try:
            original = f['producer']._run
            async def not_started(event):
                entered.set()
                await release.wait()
                await original(event)
            monkeypatch.setattr(f['producer'], '_run', not_started)
            before = f['runtime'].owner.version
            caller = asyncio.create_task(f['producer'].on_market_data(tick()))
            await asyncio.wait_for(entered.wait(), 2)
            await f['runtime'].shutdown()
            release.set()
            await caller
            await asyncio.sleep(0)
            assert f['runtime'].owner.version == before
            assert f['broker']._session.posts == []
            assert f['producer'].health()['active_tasks'] == 0
            assert f['producer'].health()['blocked_reason'] == 'command_admission_closed'
        finally:
            release.set()
            if caller is not None: await asyncio.gather(caller, return_exceptions=True)
            await close(f)
    asyncio.run(scenario())


@pytest.mark.parametrize('value', [D('10500'), 10500, 10500.5, None])
def test_cached_indicator_domain_normalizes_supported_values(tmp_path, monkeypatch, value):
    async def scenario():
        f = await fixture(tmp_path, monkeypatch, indicators={'ma5':value, 'prev_low':value})
        try:
            event = await market_event(monkeypatch, f['clock'][0])
            await f['producer'].on_market_data(event)
            data = f['runtime'].owner.state['market_sources'][SYM]['request']['market_data']
            expected = None if value is None else str(value)
            assert data == {'ma5':expected, 'prev_low':expected, 'low':'10000'}
        finally: await close(f)
    asyncio.run(scenario())


@pytest.mark.parametrize('value', [True, float('nan'), float('inf'), D('NaN'), D('Infinity'), 0, -1, '10500', {}])
@pytest.mark.parametrize('field', ['ma5', 'rest_high', 'rest_low'])
def test_invalid_cached_or_rest_price_domain_is_rejected_without_owner_write(tmp_path, monkeypatch, value, field):
    async def scenario():
        f = await fixture(tmp_path, monkeypatch, indicators={'ma5':value} if field == 'ma5' else {})
        try:
            event = tick('10000')
            if field != 'ma5': setattr(event, field.removeprefix('rest_'), value)
            before = f['runtime'].owner.version
            with pytest.raises(ValueError, match='보호 시세 값'):
                await f['producer'].on_market_data(event)
            assert f['runtime'].owner.version == before
            assert f['runtime'].owner.state['attempts'] == {}
        finally: await close(f)
    asyncio.run(scenario())


def evidence(f, result, *, symbol=SYM, quantity=100, side='sell', amount='0', observed=0):
    now = f['clock'][0]
    return OrderEvidence(result.order_ref, symbol, side, quantity, observed, D(amount),
        quantity-observed, 0, OrderState.FINAL_FILLED if observed == quantity else OrderState.PARTIAL,
        complete=True, supported_finality=observed == quantity, source_contract='synthetic-test',
        observed_at=now, request_started_at=now-timedelta(seconds=1), query_scope={
            'account_scope':'test-scope', 'market':'KR', 'exchange':'KRX',
            'start_date':'2026-09-18', 'end_date':'2026-09-18',
            'tr_id':'TTTC0081R', 'query_kind':'all', 'session':'regular'})


def test_conflicting_actual_evidence_after_handoff_is_failure(tmp_path, monkeypatch, freeze):
    async def scenario():
        freeze(11,0,day=18)
        monkeypatch.setattr(heartbeat, '_states', {})
        f = await fixture(tmp_path, monkeypatch)
        try:
            submit = f['engine']._submit_signal
            async def conflict_after_ack(event):
                await submit(event)
                result = f['results'][0]
                # 실제 lifecycle가 관측0/금액1의 모순을 저장한다.
                assert not await f['runtime'].lifecycle.reconcile(result.attempt_id,
                    evidence(f, result, amount='1'))
            monkeypatch.setattr(f['engine'], '_submit_signal', conflict_after_ack)
            await f['producer'].on_market_data(tick())
            assert next(iter(f['runtime'].owner.state['attempts'].values()))['evidence_conflict']
            assert heartbeat._states['kr_protection_producer'].last_success is None
            assert heartbeat._states['kr_protection_producer'].last_failure is not None
            assert f['producer'].health()['blocked_reason'] == 'episode_conflict'
        finally: await close(f)
    asyncio.run(scenario())


def test_later_acknowledged_symbol_cannot_clear_earlier_sweep_failure(tmp_path, monkeypatch, freeze):
    async def scenario():
        freeze(11,0,day=18)
        monkeypatch.setattr(heartbeat, '_states', {})
        f = await fixture(tmp_path, monkeypatch)
        try:
            other = '000660'
            def second_holding(state):
                for key, rows_name in [('portfolio','positions'), ('protection','states')]:
                    rows = state[key][rows_name]
                    rows[other] = deepcopy(rows[SYM])
                    rows[other]['symbol'] = other
                state['protection']['entry_times'][other] = state['protection']['entry_times'][SYM]
                return state
            await f['runtime'].owner.mutate('synthetic-second-holding', second_holding)
            await f['producer'].on_market_data(tick())
            f['broker']._session.response.data['output']['ODNO'] = '1234567891'
            await f['producer'].on_market_data(tick(symbol=other))
            result = f['results'][0]
            assert not await f['runtime'].lifecycle.reconcile(result.attempt_id, evidence(f, result, amount='1'))
            await f['producer'].sweep()
            health = f['producer'].health()
            assert health['blocked_reason'] == 'episode_conflict'
            assert health['pending_reasons'][other] == 'episode_acknowledged_pending'
            assert heartbeat._states['kr_protection_producer'].consecutive_failures == 1
        finally: await close(f)
    asyncio.run(scenario())


def test_terminal_buy_with_unapplied_reservation_remains_visible(tmp_path, monkeypatch, freeze):
    async def scenario():
        from src.execution.safety.transport import GuardedKISTransport
        freeze(11,0,day=18)
        f = await fixture(tmp_path, monkeypatch)
        try:
            request = f['request']('unapplied-buy')
            await f['quote'](request)
            await f['commands'].prepare(request, f['entry'](request))
            result = await f['commands'].dispatch(request, f['entry'](request),
                GuardedKISTransport(f['broker'], request_builder=f['builder']))
            assert await f['runtime'].lifecycle.reconcile(result.attempt_id,
                evidence(f, result, quantity=10, side='buy', amount='100000', observed=10))
            row = f['runtime'].owner.state['attempts'][result.attempt_id]
            assert row['state'] == 'final_filled' and row['reserved_quantity'] == 10
            assert f['producer'].health()['blocked_by_open_entry'] == [SYM]
        finally: await close(f)
    asyncio.run(scenario())


def test_true_orphan_without_audit_is_still_released(tmp_path, monkeypatch):
    async def scenario():
        f = await fixture(tmp_path, monkeypatch)
        try:
            await f['runtime'].quote(SYM, D('11200'), intent_id='synthetic-orphan')
            def no_audit(state):
                state['outbox'] = {}
                return state
            await f['runtime'].owner.mutate('synthetic-orphan-without-audit', no_audit)
            await f['producer'].sweep()
            assert f['runtime'].owner.state['protection']['pending_owners'] == {}
            assert f['producer'].health()['pending_released'] == 1
            assert f['producer'].health()['recovery_required'] == {}
        finally: await close(f)
    asyncio.run(scenario())


def test_valid_original_ws_data_shape_allows_eod_without_durable_quote(tmp_path, monkeypatch, freeze):
    async def scenario():
        freeze(15,9,day=18)
        f = await fixture(tmp_path, monkeypatch, indicators={'ma5':10000.5, 'prev_low':None})
        try:
            await strategy(f, 'gap_and_go')
            f['clock'][0] = f['clock'][0].replace(hour=15,minute=9)
            event = await market_event(monkeypatch, f['clock'][0], price='9900')
            await f['producer'].on_market_data(event)
            before = deepcopy(f['runtime'].owner.state['latest_explicit_quote'])
            f['clock'][0] += timedelta(minutes=1)
            freeze(15,10,day=18)
            await f['producer'].on_market_data(event)
            assert len(f['broker']._session.posts) == 1
            assert f['runtime'].owner.state['latest_explicit_quote'] == before
            assert f['runtime'].owner.state['outbox'] == {}
            assert f['events'][0].reason.startswith('갭EOD')
        finally: await close(f)
    asyncio.run(scenario())


@pytest.mark.parametrize('damage', ['future_receipt', 'event_mismatch', 'event_conflict'])
def test_eod_validates_original_ws_binding_and_canonical_time(tmp_path, monkeypatch, freeze, damage):
    async def scenario():
        freeze(15,10,day=18)
        f = await fixture(tmp_path, monkeypatch)
        try:
            await strategy(f, 'gap_and_go')
            f['clock'][0] = f['clock'][0].replace(hour=15,minute=10)
            event = await market_event(monkeypatch, f['clock'][0], price='9900')
            if damage == 'future_receipt':
                event.observation = replace(event.observation, received_at=f['clock'][0]+timedelta(seconds=1))
            elif damage == 'event_mismatch':
                event.close = D('9800')
            else:
                await f['runtime'].observe_market(event, intent_id='original-market')
                event.observation = replace(event.observation, price=D('9800'))
                event.close = D('9800')
            before = f['runtime'].owner.version
            if damage == 'event_conflict':
                await f['producer'].on_market_data(event)
                assert f['producer'].health()['blocked_reason'] == 'market_quote_event_conflict'
            else:
                with pytest.raises(ValueError): await f['producer'].on_market_data(event)
            assert f['runtime'].owner.version == before
            assert f['broker']._session.posts == []
        finally: await close(f)
    asyncio.run(scenario())


@pytest.mark.parametrize('field', ['symbol', 'quantity', 'intent'])
def test_mismatched_audit_link_cannot_bypass_recovered_rejection_hold(tmp_path, monkeypatch, freeze, field):
    async def scenario():
        freeze(11,0,day=18)
        f = await fixture(tmp_path, monkeypatch)
        try:
            f['broker']._session.response.data = {'rt_cd':'1', 'msg_cd':'synthetic_reject'}
            await fail_quote(f['runtime'], f['store'], monkeypatch)
            await f['producer'].sweep()
            def corrupt(state):
                row = next(row for row in state['outbox'].values() if row['kind'] == 'protection_decision')
                if field == 'symbol': row['symbol'] = '000660'
                elif field == 'quantity': row['decision'][1] = 90
                else: row['intent_id'] = 'unregistered-other-intent'
                return state
            await f['runtime'].owner.mutate('synthetic-audit-link-mismatch', corrupt)
            f['producer'] = Producer(f['runtime'], clock=lambda:f['clock'][0], indicator_source=lambda _: {})
            await f['producer'].on_market_data(tick())
            assert len(f['runtime'].owner.state['intents']) == 1
            assert len(f['broker']._session.posts) == 1
            assert SYM in f['producer'].health()['recovery_required']
        finally: await close(f)
    asyncio.run(scenario())


@pytest.mark.parametrize('damage', ['observed_unapplied', 'reservation_inconsistent', 'unknown'])
@pytest.mark.parametrize('recreate', [False, True])
def test_ack_alone_does_not_hide_unapplied_or_inconsistent_state(tmp_path, monkeypatch, freeze, damage, recreate):
    async def scenario():
        freeze(11,0,day=18)
        monkeypatch.setattr(heartbeat, '_states', {})
        f = await fixture(tmp_path, monkeypatch)
        try:
            await strategy(f, 'theme_chasing')
            await f['producer'].on_market_data(tick('11200'))
            result = f['results'][0]
            if damage == 'observed_unapplied':
                assert await f['runtime'].lifecycle.reconcile(result.attempt_id,
                    evidence(f, result, quantity=10, observed=10, amount='112000'))
            else:
                def corrupt(state):
                    row = state['attempts'][result.attempt_id]
                    row['state'] = 'blocked_unknown' if damage == 'unknown' else 'final_cancelled'
                    return state
                await f['runtime'].owner.mutate('synthetic-inconsistent-attempt', corrupt)
            if recreate:
                f['producer'] = Producer(f['runtime'], clock=lambda:f['clock'][0], indicator_source=lambda _: {})
                f['clock'][0] = f['clock'][0].replace(hour=15,minute=10)
                freeze(15,10,day=18)
            await f['producer'].on_market_data(tick('10050'))
            health = f['producer'].health()
            assert health['blocked_reason'] == ('blocked_by_open_exit' if recreate else 'episode_'+damage)
            assert health['pending_reasons'] == {}
            assert heartbeat._states['kr_protection_producer'].last_failure is not None
            assert len(f['broker']._session.posts) == 1
        finally: await close(f)
    asyncio.run(scenario())


def test_unrelated_partial_sell_prevents_stale_full_stop_then_new_tick_sells_ninety(tmp_path, monkeypatch, freeze):
    async def scenario():
        from src.core.types import OrderSide
        from test_execution_qualification_publishers import buy
        from test_execution_signal_gateway import order
        freeze(11,0,day=18)
        f = await fixture(tmp_path, monkeypatch)
        try:
            await strategy(f, 'manual')
            first = await f['gateway'].submit(buy(SYM),
                replace(order(side=OrderSide.SELL, price=D('11200')), reason='익절'), None)
            assert f['runtime'].owner.state['protection']['pending_owners'] == {}
            await f['producer'].on_market_data(tick())
            assert f['producer'].health()['retained_decisions'] == {}
            assert f['runtime'].owner.state['outbox'] == {}
            assert len(f['broker']._session.posts) == 1
            await apply_take_profit_fill(f, first)
            f['broker']._session.response.data['output']['ODNO'] = '1234567891'
            await f['producer'].on_market_data(tick())
            assert f['events'][0].metadata['quantity'] == 90
            assert f['broker']._session.posts[1][1]['json']['ORD_QTY'] == '90'
            assert len(f['runtime'].owner.state['attempts']) == 2
        finally: await close(f)
    asyncio.run(scenario())


def test_unrelated_rejected_sell_without_audit_does_not_start_protection_cooldown(tmp_path, monkeypatch, freeze):
    async def scenario():
        from src.core.types import OrderSide
        from test_execution_qualification_publishers import buy
        from test_execution_signal_gateway import order
        freeze(11,0,day=18)
        f = await fixture(tmp_path, monkeypatch)
        try:
            f['broker']._session.response.data = {'rt_cd':'1', 'msg_cd':'synthetic_reject'}
            await f['gateway'].submit(buy(SYM), order(side=OrderSide.SELL), None)
            assert f['runtime'].owner.state['outbox'] == {}
            f['producer'] = Producer(f['runtime'], clock=lambda:f['clock'][0], indicator_source=lambda _: {})
            await f['producer'].on_market_data(tick())
            assert len(f['runtime'].owner.state['intents']) == 2
            assert len(f['broker']._session.posts) == 2
            assert f['producer'].health()['restart_retries'] == {}
        finally: await close(f)
    asyncio.run(scenario())


def test_unrelated_open_sell_allows_decisionless_quote_observation(tmp_path, monkeypatch, freeze):
    async def scenario():
        from src.core.types import OrderSide
        from test_execution_qualification_publishers import buy
        from test_execution_signal_gateway import order
        freeze(11,0,day=18)
        f = await fixture(tmp_path, monkeypatch)
        try:
            await f['gateway'].submit(buy(SYM), order(side=OrderSide.SELL), None)
            await f['producer'].on_market_data(tick('10100'))
            assert f['runtime'].owner.state['quote_price_views'][SYM]['price'] == '10100'
            assert f['producer'].health()['retained_decisions'] == {}
            assert len(f['broker']._session.posts) == 1
        finally: await close(f)
    asyncio.run(scenario())


@pytest.mark.parametrize('field', ['symbol', 'quantity'])
def test_audit_must_match_current_retained_unsubmitted_episode(tmp_path, monkeypatch, freeze, field):
    async def scenario():
        freeze(11,0,day=18)
        f = await fixture(tmp_path, monkeypatch)
        try:
            quote = f['runtime'].quote
            async def close_after_commit(*args, **kwargs):
                decision = await quote(*args, **kwargs)
                f['runtime']._day_closed = True
                return decision
            monkeypatch.setattr(f['runtime'], 'quote', close_after_commit)
            await f['producer'].on_market_data(tick())
            def corrupt(state):
                row = next(row for row in state['outbox'].values() if row['kind'] == 'protection_decision')
                if field == 'symbol': row['symbol'] = '000660'
                else: row['decision'][1] = 90
                return state
            await f['runtime'].owner.mutate('synthetic-retained-audit-mismatch', corrupt)
            f['runtime']._day_closed = False
            await f['producer'].sweep()
            assert f['broker']._session.posts == []
            assert f['producer'].health()['recovery_required'][SYM]['reason'] == 'protection_decision_evidence_mismatch'
        finally: await close(f)
    asyncio.run(scenario())


def test_current_resume_invariant_clears_only_after_successful_recovery(tmp_path, monkeypatch, freeze):
    async def scenario():
        freeze(11,0,day=18)
        monkeypatch.setattr(heartbeat, '_states', {})
        f = await fixture(tmp_path, monkeypatch)
        try:
            command = await fail_quote(f['runtime'], f['store'], monkeypatch)
            original = deepcopy(f['runtime'].owner.state['protection_quote_admissions'][command])
            def corrupt(state):
                state['protection_quote_admissions'][command]['payload_digest'] = 'wrong'
                return state
            await f['runtime'].owner.mutate('synthetic-invariant-corruption', corrupt)
            await f['producer'].sweep()
            violation = f['producer'].health()['invariant_violation']
            assert violation == dict(reason='protection_resume_blocked',
                since=f['clock'][0].isoformat(), command_ids=[command])
            f['clock'][0] += timedelta(seconds=1)
            await f['producer'].sweep()
            assert f['producer'].health()['invariant_violation'] == violation
            assert f['broker']._session.posts == []
            # 시험에서만 원래 정확한 접수 증거를 복원한다. 제품 복구 API를 만들지 않는다.
            def restore(state):
                state['protection_quote_admissions'][command] = deepcopy(original)
                return state
            await f['runtime'].owner.mutate('synthetic-invariant-proof-restoration', restore)
            await f['producer'].sweep()
            assert f['runtime'].owner.state['protection_quote_admissions'] == {}
            assert len(f['broker']._session.posts) == 1
            assert f['producer'].health()['invariant_violation'] is None
            assert f['runtime'].health()['protection_updates_failed'] is False
            assert f['producer'].health()['blocked_reason'] is None
            assert heartbeat._states['kr_protection_producer'].last_success is not None
        finally: await close(f)
    asyncio.run(scenario())


@pytest.mark.parametrize('failure_first', [False, True])
@pytest.mark.parametrize('damage', ['conflict', 'unknown', 'observed_unapplied', 'terminal_reservation', 'ack_only'])
def test_same_symbol_sell_failure_has_priority_over_ack_in_any_order(tmp_path, monkeypatch, freeze, damage, failure_first):
    async def scenario():
        from src.core.types import OrderSide
        from test_execution_qualification_publishers import buy
        from test_execution_signal_gateway import order
        freeze(11,0,day=18)
        monkeypatch.setattr(heartbeat, '_states', {})
        f = await fixture(tmp_path, monkeypatch)
        try:
            result = await f['gateway'].submit(buy(SYM), order(side=OrderSide.SELL), None)
            def damaged_snapshot(state):
                normal = deepcopy(state['attempts'][result.attempt_id])
                broken = deepcopy(normal)
                if damage == 'conflict': broken['evidence_conflict'] = True
                elif damage == 'unknown': broken['state'] = 'blocked_unknown'
                elif damage == 'observed_unapplied': broken['observed_quantity'] = 1
                elif damage == 'terminal_reservation': broken['state'] = 'final_cancelled'
                # 정상 owner가 막는 다중 미종결 행을 손상 snapshot으로만 합성한다.
                rows = [broken, normal] if failure_first else [normal, broken]
                state['attempts'] = {}
                for identity, row in zip(('a-first', 'z-last'), rows):
                    row['attempt_id'] = identity
                    state['attempts'][identity] = row
                state['intents'][normal['intent_id']]['attempt_ids'] = ['a-first', 'z-last']
                return state
            await f['runtime'].owner.mutate('synthetic-multiple-open-sells', damaged_snapshot)
            before = f['runtime'].owner.state
            await f['producer'].on_market_data(tick())
            health = f['producer'].health()
            if damage == 'ack_only':
                assert health['blocked_reason'] is None
                assert health['pending_reasons'] == {SYM:'blocked_by_open_exit'}
                assert heartbeat._states['kr_protection_producer'].last_failure is None
                assert heartbeat._states['kr_protection_producer'].idle_reason is not None
            else:
                assert health['blocked_reason'] == 'blocked_by_open_exit'
                assert health['pending_reasons'] == {}
                assert heartbeat._states['kr_protection_producer'].last_failure is not None
            assert heartbeat._states['kr_protection_producer'].last_success is None
            assert health['retained_decisions'] == {}
            assert f['runtime'].owner.state == before
            assert len(f['broker']._session.posts) == 1
        finally: await close(f)
    asyncio.run(scenario())


@pytest.mark.parametrize('damaged_symbol', [None, '', 12345, ' 005930', '000000'])
def test_unknown_audit_symbol_blocks_even_proven_retained_order_globally(tmp_path, monkeypatch, freeze, damaged_symbol):
    async def scenario():
        freeze(11,0,day=18)
        monkeypatch.setattr(heartbeat, '_states', {})
        f = await fixture(tmp_path, monkeypatch)
        try:
            quote = f['runtime'].quote
            async def close_after_commit(*args, **kwargs):
                decision = await quote(*args, **kwargs)
                f['runtime']._day_closed = True
                return decision
            monkeypatch.setattr(f['runtime'], 'quote', close_after_commit)
            await f['producer'].on_market_data(tick())
            original = deepcopy(f['producer'].health()['retained_decisions'])
            def corrupt(state):
                state['outbox']['unknown-symbol-audit'] = dict(kind='protection_decision',
                    symbol=damaged_symbol, intent_id='unknown-symbol-intent', decision=['sell_all', 100, '손절'])
                return state
            await f['runtime'].owner.mutate('synthetic-unknown-audit-symbol', corrupt)
            f['runtime']._day_closed = False
            before = f['runtime'].owner.state
            monkeypatch.setattr(heartbeat, '_states', {})
            await f['producer'].on_market_data(tick())
            assert f['runtime'].owner.state == before
            assert f['broker']._session.posts == []
            health = f['producer'].health()
            assert health['blocked_reason'] == 'protection_decision_symbol_required'
            assert health['retained_decisions'] == original
            assert heartbeat._states['kr_protection_producer'].last_failure is not None
            assert heartbeat._states['kr_protection_producer'].consecutive_failures == 1
            assert heartbeat._states['kr_protection_producer'].last_success is None
        finally: await close(f)
    asyncio.run(scenario())


def test_ws_partial_profit_commits_matching_audit_pending_and_intent_link(tmp_path, monkeypatch, freeze):
    async def scenario():
        freeze(11,0,day=18)
        f = await fixture(tmp_path, monkeypatch, indicators={'ma5':D('10500'), 'prev_low':None})
        try:
            event = await market_event(monkeypatch, f['clock'][0], price='11200')
            await f['producer'].on_market_data(event)
            state = f['runtime'].owner.state
            identity = f['events'][0].metadata['protection_intent_id']
            assert state['market_sources'][SYM]['request']['market_data'] == {
                'ma5':'10500', 'prev_low':None, 'low':'11200'}
            audits = [row for row in state['outbox'].values() if row['kind'] == 'protection_decision']
            assert len(audits) == 1
            assert type(audits[0]['decision']) is list
            assert audits[0]['decision'][:2] == ['sell_partial', 10]
            assert audits[0]['symbol'] == SYM and audits[0]['intent_id'] == identity
            assert audits[0].get('effect_source') is None
            assert state['protection']['pending_owners'][SYM] == identity
            assert state['protection']['states'][SYM]['pending_target_qty'] == 10
            assert state['intents'][identity]['target_quantity'] == 10
            assert len(f['broker']._session.posts) == 1
        finally: await close(f)
    asyncio.run(scenario())


@pytest.mark.parametrize('disposition', ['stale', 'abandoned'])
def test_runtime_unresolved_recovery_remains_visible_on_sweep_and_other_tick(tmp_path, monkeypatch, disposition):
    async def scenario():
        from src.strategies.exit_manager import ExitManager
        f = await fixture(tmp_path, monkeypatch)
        try:
            command = await fail_quote(f['runtime'], f['store'], monkeypatch)
            original = deepcopy(f['runtime'].owner.state['protection_quote_admissions'][command])
            def corrupt(state):
                state['protection_quote_admissions'][command]['payload_digest'] = 'wrong'
                return state
            await f['runtime'].owner.mutate('synthetic-unresolved-bad-admission', corrupt)
            await f['producer'].sweep()
            assert f['producer'].health()['invariant_violation'] is not None
            def restore(state):
                state['protection_quote_admissions'][command] = deepcopy(original)
                return state
            await f['runtime'].owner.mutate('synthetic-unresolved-original-proof', restore)
            with monkeypatch.context() as patch:
                if disposition == 'stale': f['clock'][0] += timedelta(seconds=61)
                else:
                    def broken(manager, symbol, *args, **kwargs):
                        raise ValueError('합성 보호 계산 포기')
                    patch.setattr(ExitManager, 'update_price', broken)
                monkeypatch.setattr(heartbeat, '_states', {})
                await f['producer'].sweep()
            assert f['runtime'].owner.state['protection_quote_admissions'] == {}
            assert f['runtime'].health()['protection_updates_failed'] is True
            assert (SYM in f['runtime'].owner.state['protection']['degraded']) is (disposition == 'abandoned')
            assert f['producer'].health()['invariant_violation'] is None
            assert f['producer'].health()['blocked_reason'] == 'runtime_protection_recovery_required'
            assert heartbeat._states['kr_protection_producer'].last_success is None
            assert heartbeat._states['kr_protection_producer'].last_failure is not None
            # 현재 실패는 과거 disposition 카운터가 아니라 runtime 래치에서 다시 관측된다.
            for other_tick in (False, True):
                monkeypatch.setattr(heartbeat, '_states', {})
                if other_tick: await f['producer'].on_market_data(tick('10000', symbol='000660'))
                else: await f['producer'].sweep()
                assert f['producer'].health()['blocked_reason'] == 'runtime_protection_recovery_required'
                assert heartbeat._states['kr_protection_producer'].last_success is None
                assert heartbeat._states['kr_protection_producer'].last_failure is not None
                assert f['runtime'].health()['protection_updates_failed'] is True
            assert f['runtime'].owner.state['attempts'] == {}
            assert f['broker']._session.posts == []
        finally: await close(f)
    asyncio.run(scenario())


def test_current_degraded_without_failure_latch_is_observed_on_empty_sweep(tmp_path, monkeypatch):
    async def scenario():
        f = await fixture(tmp_path, monkeypatch)
        try:
            def degraded(state):
                del state['protection']['states'][SYM]
                state['protection']['degraded'][SYM] = dict(quantity=100, reason='합성 보호 상태 결손')
                return state
            await f['runtime'].owner.mutate('synthetic-degraded-observation', degraded)
            assert f['runtime'].health()['protection_updates_failed'] is False
            monkeypatch.setattr(heartbeat, '_states', {})
            before = f['runtime'].owner.state
            await f['producer'].sweep()
            assert f['producer'].health()['blocked_reason'] == 'runtime_protection_recovery_required'
            assert heartbeat._states['kr_protection_producer'].last_success is None
            assert heartbeat._states['kr_protection_producer'].last_failure is not None
            assert f['runtime'].owner.state == before
        finally: await close(f)
    asyncio.run(scenario())


def test_matching_historical_full_audit_without_position_is_not_phantom(tmp_path, monkeypatch, freeze):
    async def scenario():
        from src.execution.safety.application import FillObservation
        from test_execution_runtime import queued
        freeze(11,0,day=18)
        f = await fixture(tmp_path, monkeypatch)
        try:
            await strategy(f, 'manual')
            await f['producer'].on_market_data(tick())
            result = f['results'][0]
            assert await f['runtime'].lifecycle.reconcile(result.attempt_id,
                evidence(f, result, quantity=100, observed=100, amount='900000'))
            ref = result.order_ref
            receipt = await queued(f['engine'], FillObservation('test-scope', 'KR', ref.order_date,
                'KRX', ref.order_no, SYM, 'SELL', 100, D('900000'), org_no=ref.org_no))
            assert receipt.status == 'APPLIED'
            state = f['runtime'].owner.state
            assert SYM not in state['portfolio']['positions']
            assert SYM not in state['protection']['states']
            assert len(state['outbox']) > 0 and len(state['intents']) == 1
            f['producer'] = Producer(f['runtime'], clock=lambda:f['clock'][0], indicator_source=lambda _: {})
            monkeypatch.setattr(heartbeat, '_states', {})
            await f['producer'].sweep()
            assert f['producer'].health()['blocked_reason'] is None
            assert f['producer'].health()['recovery_required'] == {}
            assert heartbeat._states['kr_protection_producer'].idle_reason == '보유 종목 없음'
            assert heartbeat._states['kr_protection_producer'].last_failure is None
            assert len(f['broker']._session.posts) == 1
        finally: await close(f)
    asyncio.run(scenario())
