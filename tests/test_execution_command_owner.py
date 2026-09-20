"""실제 SQLite owner/요청/전송 연결. startup 허가는 합성 fixture에만 존재한다."""
import asyncio
from copy import deepcopy
from dataclasses import replace
from decimal import Decimal as D
import importlib

import pytest

from src.core.types import Order, OrderSide, OrderType
from src.execution.safety import risk_policy as p
from src.execution.safety.guards import EntryAuthority, FinalEntryGuard, GuardDecision, RiskSnapshot
from src.execution.safety.lifecycle import CommandStatus
from src.execution.safety.policy_snapshot import PolicyContext
from src.execution.safety.requests import RequestSession
from src.execution.safety.runtime import KRExecutionRuntime
from src.execution.safety.transport import GuardedKISTransport
from src.utils.stop_policy import StopDecision
from test_execution_risk_policy import snapshot, NOW
from test_execution_runtime import setup
from test_kr_prepared_dispatch import fixture as broker_fixture
from test_kr_final_dispatch import Response


def api():
    return importlib.import_module('src.execution.safety.commands')


async def fixture(tmp_path, monkeypatch, *, ready=True, origin='user', policy=None):
    module = api()
    broker, builder, calls = broker_fixture(Response(data={'rt_cd': '0', 'output': {
        'ODNO': '1234567890', 'KRX_FWDG_ORD_ORGNO': '12345'}}))
    clock = [NOW]
    engine, exits, store, runtime = await setup(tmp_path, account_scope='test-scope', clock=lambda: clock[0])
    if ready:
        # 명시 합성 startup 허가. 실제 runtime property는 계속 False다.
        monkeypatch.setattr(KRExecutionRuntime, 'trading_ready', property(lambda self: True))
    authority = EntryAuthority()
    alpha = [RiskSnapshot(1, 1, 'success', NOW, 'normal')]
    session = [GuardDecision(True, 'synthetic_open')]
    stop = [StopDecision(D('5'), 'strategy', False)]
    commands = module.RequestBoundCommands(runtime, builder=builder, authority=authority,
        entry_guard=FinalEntryGuard(authority, lambda: alpha[0], lambda: clock[0]),
        stop_resolver=lambda strategy: stop[0], session_guard=lambda request: session[0])
    ctx = PolicyContext.from_snapshot(snapshot(p))
    ctx = replace(ctx, policy=policy or ctx.policy,
                  versions=replace(ctx.versions, execution=runtime.owner.version))
    await commands.publish_policy_context(ctx, expected_version=runtime.owner.version)
    def request(aid='A', *, symbol='005930', quantity=10, price=D('10000'), side=OrderSide.BUY,
                strategy=None, order_type=OrderType.LIMIT):
        strategy = strategy or ('manual' if origin == 'user' else 'sepa_trend')
        return builder.prepare_submit(Order(symbol=symbol, side=side, quantity=quantity,
            price=price, order_type=order_type, strategy=strategy), intent_id='I-'+aid,
            attempt_id=aid, session=RequestSession(NOW.date().isoformat(), NOW, 'regular'),
            valuation_price=price)
    def entry(req):
        if origin == 'user': return authority.user_order(req.symbol, req.side.value)
        if origin == 'safe_asset': return authority.safe_asset(req.symbol, req.side.value)
        return authority.automatic(req.symbol, req.side.value, req.strategy)
    async def quote(req):
        await commands.observe_entry_quote(req.symbol, req.valuation_price, as_of=clock[0],
            source='synthetic-market', event_id='quote-'+req.attempt_id,
            expected_version=runtime.owner.version)
    async def facts(req, sector=None, **changes):
        """S1 계약: 자동 BUY prepare는 게시된 판단 사실을 요구한다(게시 호출만 추가)."""
        from datetime import timedelta
        from src.execution.safety.decisions import (
            ConsumedSource, EntryDecisionFacts, QualificationFacts)
        risk = ctx.policy.sizing_mode == 'risk' and req.strategy != 'core_holding'
        source = ConsumedSource('trade_memory', 1, clock[0], 'synthetic-memory-digest')
        value = EntryDecisionFacts(intent_id=req.intent_id, symbol=req.symbol,
            side=req.side.value, strategy=req.strategy, origin='automatic', sector=sector,
            config_version=ctx.versions.config, hybrid_enabled=False, decided_at=clock[0],
            expires_at=clock[0] + timedelta(minutes=30), base_pct=0.25,
            strategy_allocation_pct=None, min_position_value=D('200000'),
            strength_multiplier=1.0, position_multiplier=1.0, calendar_multiplier=1.0,
            volatility_multiplier=1.0, conviction_multiplier=1.0, atr_pct=None,
            stop_pct=stop[0].stop_pct if risk else None,
            stop_source=stop[0].source if risk else None,
            stop_crash_capped=stop[0].crash_capped if risk else None,
            qualification=QualificationFacts(72.0, 72.0, 70.0, ('rule_11',), 'allow'),
            sources=(source,), **changes)
        if source.name not in runtime.owner.state.get('qualification_sources', {}):
            await commands.publish_qualification_source(source.name, as_of=source.as_of,
                digest=source.digest, expected_version=runtime.owner.version)
        await commands.publish_decision_facts(value, expected_version=runtime.owner.version)
        return value
    return locals()


def test_prepare_atomically_binds_actual_resources_and_posts_once(tmp_path, monkeypatch):
    async def scenario():
        f = await fixture(tmp_path, monkeypatch)
        try:
            req = f['request']()
            await f['quote'](req)
            before = f['runtime'].owner.version
            attempt = await f['commands'].prepare(req, f['entry'](req), sector='반도체')
            assert f['runtime'].owner.version == before + 1
            assert attempt['request_binding']['fingerprint'] == req.fingerprint
            assert attempt['request_binding']['resources']['cash'] == '101500.000'
            assert attempt['reserved_cash'] == '101500.000'
            assert attempt['reserved_exposure'] == '100000'
            assert attempt['reserved_planned_risk'] is None
            result = await f['commands'].dispatch(req, f['entry'](req),
                GuardedKISTransport(f['broker'], request_builder=f['builder']))
            assert result.status is CommandStatus.ACKNOWLEDGED
            assert len(f['broker']._session.posts) == 1
            again = await f['commands'].dispatch(req, f['entry'](req),
                GuardedKISTransport(f['broker'], request_builder=f['builder']))
            assert again.status is CommandStatus.NOT_SENT
            assert len(f['broker']._session.posts) == 1
        finally: await f['store'].close()
    asyncio.run(scenario())


@pytest.mark.parametrize('fault', ['startup', 'forged', 'missing_quote', 'cash', 'legacy_writer'])
def test_prepare_or_dispatch_fails_closed_without_http(tmp_path, monkeypatch, fault):
    async def scenario():
        f = await fixture(tmp_path, monkeypatch, ready=fault != 'startup')
        try:
            req = f['request'](quantity=201 if fault == 'cash' else 10)
            if fault != 'missing_quote': await f['quote'](req)
            context = f['entry'](req)
            if fault == 'forged': context = EntryAuthority().user_order(req.symbol, 'buy')
            if fault == 'legacy_writer': f['engine'].portfolio.cash -= D('1')
            if fault == 'startup':
                await f['commands'].prepare(req, context)
                result = await f['commands'].dispatch(req, context,
                    GuardedKISTransport(f['broker'], request_builder=f['builder']))
                assert result.status is CommandStatus.NOT_SENT
            else:
                with pytest.raises(ValueError): await f['commands'].prepare(req, context)
            assert f['broker']._session.posts == []
        finally: await f['store'].close()
    asyncio.run(scenario())


@pytest.mark.parametrize('dimension', ['cash', 'slot', 'sector', 'new_buys'])
def test_two_prepares_compete_for_current_owner_resources(tmp_path, monkeypatch, dimension):
    async def scenario():
        policy = snapshot(p).policy
        if dimension == 'slot': policy = replace(policy, max_positions=1)
        if dimension == 'sector': policy = replace(policy, max_positions_per_sector=1)
        if dimension == 'new_buys': policy = replace(policy, max_daily_new_buys=1)
        f = await fixture(tmp_path, monkeypatch, origin='user' if dimension == 'cash' else 'automatic', policy=policy)
        try:
            reqs = [f['request']('A', quantity=100 if dimension == 'cash' else 10),
                    f['request']('B', symbol='000660', quantity=100 if dimension == 'cash' else 10)]
            for req in reqs: await f['quote'](req)
            if dimension != 'cash':
                for req in reqs: await f['facts'](req, sector='반도체')
            results = await asyncio.gather(*(f['commands'].prepare(req, f['entry'](req), sector='반도체')
                                            for req in reqs), return_exceptions=True)
            assert sum(isinstance(result, dict) for result in results) == 1
            assert len(f['runtime'].owner.state['attempts']) == 1
        finally: await f['store'].close()
    asyncio.run(scenario())


@pytest.mark.parametrize('boundary', ['connect', 'token', 'hashkey', 'limiter'])
@pytest.mark.parametrize('change', ['cash', 'quote', 'stop', 'severe', 'session', 'day', 'claim', 'protection'])
def test_current_state_after_every_network_await_blocks_post(tmp_path, monkeypatch, boundary, change):
    async def scenario():
        f = await fixture(tmp_path, monkeypatch, origin='automatic')
        try:
            req = f['request']()
            await f['quote'](req)
            await f['facts'](req)
            await f['commands'].prepare(req, f['entry'](req))
            reached, release = asyncio.Event(), asyncio.Event()
            async def pause(*args):
                reached.set()
                await release.wait()
                f['broker']._session.closed = False
                f['broker']._token = 'synthetic-token'
                return 'synthetic-hash' if boundary == 'hashkey' else True
            if boundary == 'connect': f['broker']._session.closed = True
            if boundary == 'token': f['broker']._token = None
            setattr(f['broker'], {'connect': 'connect', 'token': '_ensure_token',
                'hashkey': '_get_hashkey', 'limiter': '_rate_limit'}[boundary], pause)
            task = asyncio.create_task(f['commands'].dispatch(req, f['entry'](req),
                GuardedKISTransport(f['broker'], request_builder=f['builder'])))
            await asyncio.wait_for(reached.wait(), 2)
            if change == 'cash':
                def reduce(state):
                    state['portfolio']['cash'] = '100'
                    return state
                await f['runtime'].owner.mutate('synthetic-cash-change', reduce)
            elif change == 'quote':
                await f['commands'].observe_entry_quote(req.symbol, D('10001'), as_of=NOW,
                    source='synthetic-market', event_id='new-quote', expected_version=f['runtime'].owner.version)
            elif change == 'stop': f['stop'][0] = StopDecision(D('6'), 'strategy', False)
            elif change == 'severe': f['alpha'][0] = replace(f['alpha'][0], level='severe')
            elif change == 'session': f['session'][0] = GuardDecision(False, 'synthetic-kill-or-session')
            elif change == 'day':
                from datetime import timedelta
                f['clock'][0] += timedelta(days=1)
            elif change == 'claim':
                def reduce(state):
                    state['attempts']['A']['claim_id'] = 'different-sender'
                    return state
                await f['runtime'].owner.mutate('synthetic-claim-change', reduce)
            elif change == 'protection': f['exits']._intraday_crash_level = 'severe'
            release.set()
            result = await task
            assert result.status in (CommandStatus.NOT_SENT, CommandStatus.UNKNOWN)
            assert f['broker']._session.posts == []
        finally: await f['store'].close()
    asyncio.run(scenario())


@pytest.mark.parametrize('point', ['unsent', 'response', 'result'])
def test_caller_cancellation_never_starts_an_unsent_post_and_retains_result_task(tmp_path, monkeypatch, point):
    async def scenario():
        f = await fixture(tmp_path, monkeypatch)
        try:
            req = f['request']()
            await f['quote'](req)
            await f['commands'].prepare(req, f['entry'](req))
            reached, release = asyncio.Event(), asyncio.Event()
            async def wait(*args):
                reached.set()
                await release.wait()
                return {'rt_cd': '0', 'output': {'ODNO': '1234567890', 'KRX_FWDG_ORD_ORGNO': '12345'}}
            if point == 'unsent': f['broker']._rate_limit = wait
            elif point == 'response': f['broker']._session.response.json = wait
            else:
                original = f['runtime'].lifecycle.record_result
                async def record(*args, **kwargs):
                    await wait()
                    return await original(*args, **kwargs)
                monkeypatch.setattr(f['runtime'].lifecycle, 'record_result', record)
            task = asyncio.create_task(f['commands'].dispatch(req, f['entry'](req),
                GuardedKISTransport(f['broker'], request_builder=f['builder'])))
            await asyncio.wait_for(reached.wait(), 2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError): await task
            if point == 'result':
                assert len(f['commands']._result_tasks) == 1
                pending = tuple(f['commands']._result_tasks)
                release.set()
                await asyncio.gather(*pending)
            assert len(f['broker']._session.posts) == (0 if point == 'unsent' else 1)
            attempt = f['runtime'].owner.state['attempts']['A']
            assert attempt['claim_id'] and attempt['reserved_cash'] == '101500.000'
            assert attempt['command_status'] == ('acknowledged' if point == 'result' else 'unknown')
            again = await f['commands'].dispatch(req, f['entry'](req),
                GuardedKISTransport(f['broker'], request_builder=f['builder']))
            assert again.status is CommandStatus.NOT_SENT
            assert len(f['broker']._session.posts) == (0 if point == 'unsent' else 1)
        finally: await f['store'].close()
    asyncio.run(scenario())


@pytest.mark.parametrize('response,status', [
    ({'rt_cd': '0'}, CommandStatus.UNKNOWN),
    ({'rt_cd': '0', 'output': {'ODNO': 'TEMP_123', 'KRX_FWDG_ORD_ORGNO': '123'}}, CommandStatus.UNKNOWN),
    ({'rt_cd': '1', 'msg_cd': 'SYNTHETIC_REJECT'}, CommandStatus.REJECTED),
])
def test_ack_identity_unknown_and_definitive_rejection_resource_release(tmp_path, monkeypatch, response, status):
    async def scenario():
        f = await fixture(tmp_path, monkeypatch, origin='automatic')
        try:
            req = f['request']()
            await f['quote'](req)
            await f['facts'](req, sector='반도체')
            await f['commands'].prepare(req, f['entry'](req), sector='반도체')
            f['broker']._session.response.data = response
            result = await f['commands'].dispatch(req, f['entry'](req),
                GuardedKISTransport(f['broker'], request_builder=f['builder']))
            assert result.status is status
            attempt = f['runtime'].owner.state['attempts']['A']
            released = status is CommandStatus.REJECTED
            assert (D(attempt['reserved_exposure']) == 0) == released
            assert (D(attempt['reserved_planned_risk']) == 0) == released
            assert (req.symbol not in f['runtime'].owner.state['entry_policy_effects']['pending_sectors']) == released
            assert len(f['broker']._session.posts) == 1
        finally: await f['store'].close()
    asyncio.run(scenario())


def test_pending_sector_cleanup_keeps_other_unreleased_risk_and_strict_quantity():
    from src.execution.safety.lifecycle import clear_settled_pending_sector
    base = {'attempts': {'A': {'kind': 'submit', 'symbol': 'S', 'state': 'final_filled',
        'reserved_quantity': 0, 'reserved_cash': '0', 'reserved_exposure': '0', 'reserved_planned_risk': None}},
        'entry_policy_effects': {'pending_sectors': {'S': 'sector', 'OTHER': 'other'}, 'sidecar_active': True}}
    for changes in ({'reserved_exposure': '1'}, {'reserved_planned_risk': '1'}, {'reserved_quantity': False}):
        state = deepcopy(base)
        state['attempts']['B'] = state['attempts']['A'] | changes
        if changes.get('reserved_quantity') is False:
            with pytest.raises(ValueError): clear_settled_pending_sector(state, 'A')
        else:
            clear_settled_pending_sector(state, 'A')
        assert state['entry_policy_effects'] == base['entry_policy_effects']
    state = deepcopy(base)
    clear_settled_pending_sector(state, 'A')
    assert state['entry_policy_effects'] == {'pending_sectors': {'OTHER': 'other'}, 'sidecar_active': True}


@pytest.mark.parametrize('quantity,allowed', [(139, True), (140, False)])
def test_real_risk_resource_cap_139_140_uses_current_equity(tmp_path, monkeypatch, quantity, allowed):
    async def scenario():
        f = await fixture(tmp_path, monkeypatch, origin='automatic')
        try:
            def fund(state):
                state['portfolio']['cash'] = '10000000'
                return state
            await f['runtime'].owner.mutate('synthetic-equity', fund)
            req = f['request'](quantity=quantity)
            await f['quote'](req)
            await f['facts'](req)
            if allowed:
                attempt = await f['commands'].prepare(req, f['entry'](req))
                assert D(attempt['reserved_planned_risk']) <= D('70000')
            else:
                with pytest.raises(ValueError): await f['commands'].prepare(req, f['entry'](req))
                assert f['runtime'].owner.state['attempts'] == {}
        finally: await f['store'].close()
    asyncio.run(scenario())


async def held(f, quantity=100):
    from src.core.types import Position, PositionSide
    from src.execution.safety.economics import encode_portfolio, decode_portfolio
    from src.execution.safety.protection import encode_protection, decode_protection
    def reduce(state):
        pf = decode_portfolio(state['portfolio'])
        position = Position('005930', side=PositionSide.LONG, quantity=quantity,
            avg_price=D('10000'), current_price=D('10000'), strategy='manual', entry_time=NOW)
        pf.positions['005930'] = position
        exits = decode_protection(state['protection'], clock=lambda: NOW)
        exits.register_position(position)
        state['portfolio'], state['protection'] = encode_portfolio(pf), encode_protection(exits)
        return state
    await f['runtime'].owner.mutate('synthetic-held:'+str(quantity), reduce)


def test_concurrent_sell_claims_cannot_double_reserve_recognized_holdings(tmp_path, monkeypatch):
    async def scenario():
        f = await fixture(tmp_path, monkeypatch)
        try:
            await held(f)
            reqs = [f['request'](aid, quantity=60, side=OrderSide.SELL) for aid in ('A', 'B')]
            results = await asyncio.gather(*(f['commands'].prepare(req, f['entry'](req)) for req in reqs),
                                            return_exceptions=True)
            assert sum(isinstance(result, dict) for result in results) == 1
            assert sum(a['reserved_quantity'] for a in f['runtime'].owner.state['attempts'].values()) == 60
        finally: await f['store'].close()
    asyncio.run(scenario())


@pytest.mark.parametrize('change', ['none', 'version', 'cash', 'quantity'])
def test_cancel_binds_current_parent_and_never_releases_parent_on_ack(tmp_path, monkeypatch, change):
    async def scenario():
        from src.execution.safety.requests import CancelParent
        from src.execution.safety.lifecycle import OrderRef
        f = await fixture(tmp_path, monkeypatch)
        try:
            req = f['request']()
            await f['quote'](req)
            await f['commands'].prepare(req, f['entry'](req))
            await f['commands'].dispatch(req, f['entry'](req), GuardedKISTransport(f['broker'], request_builder=f['builder']))
            parent = f['runtime'].owner.state['attempts']['A']
            cancel_parent = CancelParent(req.intent_id, req.attempt_id, parent['version'],
                OrderRef.from_dict(parent['order_ref']), req.symbol, req.side, req.order_type,
                parent['reserved_quantity'], req.valuation_price, req.strategy)
            cancel = f['builder'].prepare_cancel(intent_id=req.intent_id, attempt_id='C',
                session=req.session, parent=cancel_parent)
            if change != 'none':
                def mutate(state):
                    a = state['attempts']['A']
                    if change == 'version': a['version'] += 1
                    elif change == 'cash': a['reserved_cash'] = '0'
                    else: a['reserved_quantity'] -= 1
                    return state
                await f['runtime'].owner.mutate('synthetic-parent-change', mutate)
                with pytest.raises(ValueError): await f['commands'].prepare(cancel, f['entry'](cancel))
                assert len(f['broker']._session.posts) == 1
            else:
                child = await f['commands'].prepare(cancel, f['entry'](cancel))
                assert child['reserved_quantity'] == 0 and D(child['reserved_exposure']) == 0
                assert child['reserved_planned_risk'] is None
                result = await f['commands'].dispatch(cancel, f['entry'](cancel),
                    GuardedKISTransport(f['broker'], request_builder=f['builder']))
                assert result.status is CommandStatus.ACKNOWLEDGED
                assert f['runtime'].owner.state['attempts']['A'] == parent
                assert len(f['broker']._session.posts) == 2
        finally: await f['store'].close()
    asyncio.run(scenario())


@pytest.mark.parametrize('change', ['scope', 'day', 'market', 'exchange', 'org', 'version'])
def test_bound_lifecycle_result_cannot_record_wrong_identity_or_unversioned_result(tmp_path, monkeypatch, change):
    async def scenario():
        from src.execution.safety.lifecycle import CommandResult, OrderRef
        f = await fixture(tmp_path, monkeypatch)
        try:
            req = f['request']()
            await f['quote'](req)
            await f['commands'].prepare(req, f['entry'](req))
            assert await f['runtime'].lifecycle.claim(req.attempt_id, 'generic-claim')
            ref = OrderRef('test-scope', 'KR', NOW.date().isoformat(), 'KRX', '12345', '12345')
            if change != 'version':
                ref = replace(ref, **{{'scope': 'account_scope', 'day': 'order_date', 'market': 'market',
                    'exchange': 'exchange', 'org': 'org_no'}[change]: '2026-09-17' if change == 'day' else '' if change == 'org' else 'WRONG'})
            version = f['runtime'].owner.state['attempts']['A']['version']
            accepted = await f['runtime'].lifecycle.record_result('A', 'generic-claim',
                CommandResult(CommandStatus.ACKNOWLEDGED, 'A', ref),
                **({} if change == 'version' else {'expected_attempt_version': version}))
            assert accepted is False
            assert f['runtime'].owner.state['attempts']['A']['order_ref'] is None
            result = await f['commands'].dispatch(req, f['entry'](req),
                GuardedKISTransport(f['broker'], request_builder=f['builder']))
            assert result.status is CommandStatus.NOT_SENT and f['broker']._session.posts == []
        finally: await f['store'].close()
    asyncio.run(scenario())


@pytest.mark.parametrize('field', ['quantity', 'reserved_quantity', 'binding_scope', 'binding_quantity', 'binding_resources'])
def test_current_stored_request_identity_and_exact_integer_reservations_checked(tmp_path, monkeypatch, field):
    async def scenario():
        f = await fixture(tmp_path, monkeypatch)
        try:
            req = f['request'](quantity=1)
            await f['quote'](req)
            await f['commands'].prepare(req, f['entry'](req))
            def mutate(state):
                attempt = state['attempts']['A']
                if field == 'binding_scope': attempt['request_binding']['account_scope'] = 'WRONG'
                elif field == 'binding_quantity': attempt['request_binding']['quantity'] = True
                elif field == 'binding_resources': attempt['request_binding']['resources']['quantity'] = True
                else: attempt[field] = True
                return state
            await f['runtime'].owner.mutate('synthetic-binding-conflict', mutate)
            result = await f['commands'].dispatch(req, f['entry'](req),
                GuardedKISTransport(f['broker'], request_builder=f['builder']))
            assert result.status is CommandStatus.NOT_SENT
            assert f['broker']._session.posts == []
        finally: await f['store'].close()
    asyncio.run(scenario())


@pytest.mark.parametrize('stage', ['prepare_commit', 'prepare_publish', 'claim_commit', 'result_commit'])
def test_store_and_publication_failures_never_manufacture_post_or_release(tmp_path, monkeypatch, stage):
    async def scenario():
        f = await fixture(tmp_path, monkeypatch)
        try:
            req = f['request']()
            await f['quote'](req)
            async def fail(*args, **kwargs): raise OSError('synthetic-store-failure')
            if stage == 'prepare_commit': monkeypatch.setattr(f['store'], 'commit', fail)
            if stage == 'prepare_publish':
                def publish(*args): raise RuntimeError('synthetic-publication-failure')
                monkeypatch.setattr(f['runtime'].owner, 'publisher', publish)
            if stage.startswith('prepare'):
                with pytest.raises(Exception): await f['commands'].prepare(req, f['entry'](req))
                assert not f['runtime'].owner.healthy
                assert f['broker']._session.posts == []
            else:
                await f['commands'].prepare(req, f['entry'](req))
                if stage == 'claim_commit': monkeypatch.setattr(f['store'], 'commit', fail)
                else:
                    original = f['broker']._session.response.json
                    async def after_post():
                        monkeypatch.setattr(f['store'], 'commit', fail)
                        return await original()
                    f['broker']._session.response.json = after_post
                result = await f['commands'].dispatch(req, f['entry'](req),
                    GuardedKISTransport(f['broker'], request_builder=f['builder']))
                assert result.status is (CommandStatus.NOT_SENT if stage == 'claim_commit' else CommandStatus.UNKNOWN)
                assert len(f['broker']._session.posts) == (0 if stage == 'claim_commit' else 1)
                assert not f['runtime'].owner.healthy
                assert D(f['runtime'].owner.state['attempts']['A']['reserved_exposure']) > 0
        finally: await f['store'].close()
    asyncio.run(scenario())


def test_claimed_checkpoint_cannot_issue_new_process_permit_after_restore(tmp_path, monkeypatch):
    async def scenario():
        f = await fixture(tmp_path, monkeypatch)
        try:
            req = f['request']()
            await f['quote'](req)
            await f['commands'].prepare(req, f['entry'](req))
            assert await f['runtime'].lifecycle.claim('A', 'previous-process')
            await f['runtime'].restore()
            fresh = api().RequestBoundCommands(f['runtime'], builder=f['builder'], authority=f['authority'],
                entry_guard=f['commands'].entry_guard, stop_resolver=f['commands'].stop_resolver,
                session_guard=f['commands'].session_guard)
            result = await fresh.dispatch(req, f['entry'](req),
                GuardedKISTransport(f['broker'], request_builder=f['builder']))
            assert result.status is CommandStatus.NOT_SENT
            assert f['broker']._session.posts == [] and fresh._permits == {}
            assert f['runtime'].owner.state['attempts']['A']['claim_id'] == 'previous-process'
        finally: await f['store'].close()
    asyncio.run(scenario())


@pytest.mark.parametrize('change', ['quote_conflict', 'quote_stale', 'quote_future', 'policy_old_version', 'policy_yesterday'])
def test_external_context_observation_provenance_is_not_relabelled(tmp_path, monkeypatch, change):
    async def scenario():
        from datetime import timedelta
        f = await fixture(tmp_path, monkeypatch)
        try:
            req = f['request']()
            await f['quote'](req)
            before = f['runtime'].owner.state
            with pytest.raises(ValueError):
                if change.startswith('quote'):
                    await f['commands'].observe_entry_quote(req.symbol, D('10001'),
                        as_of=NOW - timedelta(seconds=1) if change == 'quote_stale' else
                              NOW + timedelta(seconds=1) if change == 'quote_future' else NOW,
                        source='synthetic-market', event_id='quote-A' if change == 'quote_conflict' else 'new',
                        expected_version=f['runtime'].owner.version)
                else:
                    ctx = f['ctx']
                    if change == 'policy_yesterday':
                        ctx = replace(ctx, business_day=ctx.business_day - timedelta(days=1),
                            observed_at=ctx.observed_at - timedelta(days=1),
                            versions=replace(ctx.versions, execution=f['runtime'].owner.version))
                    await f['commands'].publish_policy_context(ctx, expected_version=f['runtime'].owner.version)
            assert f['runtime'].owner.state == before
        finally: await f['store'].close()
    asyncio.run(scenario())


def test_unapplied_durable_observation_is_a_common_barrier(tmp_path, monkeypatch):
    async def scenario():
        from src.execution.safety.application import FillObservation
        f = await fixture(tmp_path, monkeypatch)
        try:
            req = f['request']()
            await f['quote'](req)
            await f['runtime'].owner.receive(FillObservation('test-scope', 'KR', NOW.date().isoformat(),
                'KRX', 'unknown-order', '000660', 'BUY', 1, D('10000')),
                ingress_context=f['runtime'].ingress_context(1))
            with pytest.raises(ValueError): await f['commands'].prepare(req, f['entry'](req))
            assert f['runtime'].owner.state['attempts'] == {}
        finally: await f['store'].close()
    asyncio.run(scenario())


def test_original_cancellation_propagates_even_when_unknown_result_cannot_be_saved(tmp_path, monkeypatch):
    async def scenario():
        f = await fixture(tmp_path, monkeypatch)
        try:
            req = f['request']()
            await f['quote'](req)
            await f['commands'].prepare(req, f['entry'](req))
            reached = asyncio.Event()
            async def wait(*args):
                reached.set()
                await asyncio.Event().wait()
            f['broker']._rate_limit = wait
            task = asyncio.create_task(f['commands'].dispatch(req, f['entry'](req),
                GuardedKISTransport(f['broker'], request_builder=f['builder'])))
            await asyncio.wait_for(reached.wait(), 2)
            async def fail(*args, **kwargs): raise OSError('synthetic-store-failure')
            monkeypatch.setattr(f['store'], 'commit', fail)
            task.cancel()
            with pytest.raises(asyncio.CancelledError): await task
            assert not f['runtime'].owner.healthy
            assert f['broker']._session.posts == []
            assert D(f['runtime'].owner.state['attempts']['A']['reserved_cash']) > 0
        finally: await f['store'].close()
    asyncio.run(scenario())


@pytest.mark.parametrize('reason', [1, None, True])
def test_session_guard_requires_exact_reason_string(tmp_path, monkeypatch, reason):
    async def scenario():
        f = await fixture(tmp_path, monkeypatch)
        try:
            req = f['request']()
            await f['quote'](req)
            f['session'][0] = GuardDecision(True, reason)
            with pytest.raises(ValueError): await f['commands'].prepare(req, f['entry'](req))
        finally: await f['store'].close()
    asyncio.run(scenario())


def test_quote_storage_is_latest_only_and_does_not_concatenate_identity_parts(tmp_path, monkeypatch):
    async def scenario():
        f = await fixture(tmp_path, monkeypatch)
        try:
            commands = f['commands']
            for source, event_id, price in [('a:b', 'c', D('1')), ('a', 'b:c', D('2'))]:
                await commands.observe_entry_quote('005930', price, as_of=NOW, source=source,
                    event_id=event_id, expected_version=f['runtime'].owner.version)
            state = f['runtime'].owner.state
            assert state['entry_quotes']['005930']['price'] == '2'
            assert 'entry_quote_observations' not in state
            assert len(state['entry_quotes']) == 1
        finally: await f['store'].close()
    asyncio.run(scenario())


@pytest.mark.parametrize('field', ['reserved_cash', 'reserved_exposure', 'reserved_planned_risk'])
def test_terminal_attempt_with_remaining_resource_never_disappears_from_admission(tmp_path, monkeypatch, field):
    async def scenario():
        f = await fixture(tmp_path, monkeypatch)
        try:
            first, req = f['request'](), f['request']('B', symbol='000660')
            await f['quote'](first)
            await f['quote'](req)
            await f['commands'].prepare(first, f['entry'](first))
            def mutate(state):
                a = state['attempts']['A']
                a.update(state='final_rejected', status='final_rejected', reserved_quantity=0,
                         reserved_cash='0', reserved_exposure='0', reserved_planned_risk=None)
                a[field] = '1'
                return state
            await f['runtime'].owner.mutate('synthetic-terminal-resource', mutate)
            with pytest.raises(ValueError): await f['commands'].prepare(req, f['entry'](req))
        finally: await f['store'].close()
    asyncio.run(scenario())


@pytest.mark.parametrize('kind', ['market', 'same_intent_increase', 'modify', 'caller_tamper'])
def test_market_is_supported_but_request_and_intent_cannot_be_reinterpreted(tmp_path, monkeypatch, kind):
    async def scenario():
        from src.execution.safety.lifecycle import CommandKind
        f = await fixture(tmp_path, monkeypatch)
        try:
            req = f['request'](order_type=OrderType.MARKET if kind == 'market' else OrderType.LIMIT)
            await f['quote'](req)
            if kind == 'modify':
                req = replace(req, command=CommandKind.MODIFY)
                with pytest.raises(ValueError): await f['commands'].prepare(req, f['entry'](req))
                assert f['broker']._session.posts == []
            elif kind == 'caller_tamper':
                await f['commands'].prepare(req, f['entry'](req))
                object.__setattr__(req, 'quantity', 11)
                with pytest.raises(ValueError):
                    await f['commands'].dispatch(req, f['entry'](req),
                        GuardedKISTransport(f['broker'], request_builder=f['builder']))
                assert f['broker']._session.posts == []
            else:
                await f['commands'].prepare(req, f['entry'](req))
                if kind == 'same_intent_increase':
                    f['broker']._session.response.data = {'rt_cd': '1', 'msg_cd': 'SYNTHETIC_REJECT'}
                result = await f['commands'].dispatch(req, f['entry'](req),
                    GuardedKISTransport(f['broker'], request_builder=f['builder']))
                if kind == 'market':
                    assert result.status is CommandStatus.ACKNOWLEDGED
                    body = f['broker']._session.posts[0][1]['json']
                    assert body['ORD_UNPR'] == '0' and body['ORD_QTY'] == '10'
                    assert D(f['runtime'].owner.state['attempts']['A']['reserved_cash']) > 0
                else:
                    second = f['builder'].prepare_submit(Order(symbol=req.symbol, side=req.side,
                        quantity=11, price=D('10000'), order_type=OrderType.LIMIT, strategy='manual'),
                        intent_id=req.intent_id, attempt_id='B', session=req.session, valuation_price=D('10000'))
                    with pytest.raises(ValueError): await f['commands'].prepare(second, f['entry'](second))
                    assert len(f['broker']._session.posts) == 1
        finally: await f['store'].close()
    asyncio.run(scenario())


def test_claim_lookup_await_rechecks_current_quote_inside_serial_owner(tmp_path, monkeypatch):
    async def scenario():
        f = await fixture(tmp_path, monkeypatch)
        try:
            req = f['request']()
            await f['quote'](req)
            await f['commands'].prepare(req, f['entry'](req))
            original = f['store'].lookup_commit
            async def lookup(key):
                if 'claim:' in key:
                    f['clock'][0] = NOW.replace(hour=15, minute=25)
                return await original(key)
            monkeypatch.setattr(f['store'], 'lookup_commit', lookup)
            result = await f['commands'].dispatch(req, f['entry'](req),
                GuardedKISTransport(f['broker'], request_builder=f['builder']))
            assert result.status is CommandStatus.NOT_SENT
            assert f['broker']._session.posts == []
            assert f['runtime'].owner.state['attempts']['A']['claim_id'] is None
        finally: await f['store'].close()
    asyncio.run(scenario())


@pytest.mark.parametrize('bad', ['NaN', 'Infinity', '-1', True])
def test_lifecycle_must_not_erase_malformed_resource_before_validating_it(tmp_path, monkeypatch, bad):
    async def scenario():
        from src.execution.safety.lifecycle import CommandResult
        f = await fixture(tmp_path, monkeypatch)
        try:
            req = f['request']()
            await f['quote'](req)
            await f['commands'].prepare(req, f['entry'](req))
            assert await f['runtime'].lifecycle.claim('A', 'generic-claim')
            def mutate(state):
                state['attempts']['A']['reserved_exposure'] = bad
                return state
            await f['runtime'].owner.mutate('synthetic-corrupt-resource', mutate)
            version = f['runtime'].owner.state['attempts']['A']['version']
            with pytest.raises(ValueError):
                await f['runtime'].lifecycle.record_result('A', 'generic-claim',
                    CommandResult(CommandStatus.NOT_SENT, 'A'), expected_attempt_version=version)
            assert f['runtime'].owner.state['attempts']['A']['reserved_exposure'] == bad
        finally: await f['store'].close()
    asyncio.run(scenario())


def test_sell_available_quantity_is_recomputed_after_last_limiter_await(tmp_path, monkeypatch):
    async def scenario():
        f = await fixture(tmp_path, monkeypatch)
        try:
            await held(f)
            req = f['request'](quantity=100, side=OrderSide.SELL)
            await f['commands'].prepare(req, f['entry'](req))
            async def reduce_holdings(*args): await held(f, quantity=50)
            f['broker']._rate_limit = reduce_holdings
            result = await f['commands'].dispatch(req, f['entry'](req),
                GuardedKISTransport(f['broker'], request_builder=f['builder']))
            assert result.status is CommandStatus.NOT_SENT
            assert f['broker']._session.posts == []
            assert f['runtime'].owner.state['attempts']['A']['reserved_quantity'] == 0
            assert f['engine'].portfolio.positions[req.symbol].quantity == 50
        finally: await f['store'].close()
    asyncio.run(scenario())


@pytest.mark.parametrize('route', ['user', 'safe_asset', 'core', 'sell'])
def test_stop_resolver_failure_does_not_extend_cap_to_exempt_routes(tmp_path, monkeypatch, route):
    async def scenario():
        f = await fixture(tmp_path, monkeypatch,
            origin='automatic' if route in ('core', 'sell') else route)
        try:
            def fail(strategy): raise AssertionError('cap-exempt resolver must not be called')
            f['commands'].stop_resolver = fail
            if route == 'sell': await held(f)
            req = f['request'](side=OrderSide.SELL if route == 'sell' else OrderSide.BUY,
                strategy='core_holding' if route == 'core' else 'safe_asset' if route == 'safe_asset' else None)
            if route != 'sell': await f['quote'](req)
            if route == 'core': await f['facts'](req)
            attempt = await f['commands'].prepare(req, f['entry'](req))
            assert attempt['reserved_planned_risk'] is None
            result = await f['commands'].dispatch(req, f['entry'](req),
                GuardedKISTransport(f['broker'], request_builder=f['builder']))
            assert result.status is CommandStatus.ACKNOWLEDGED
            assert len(f['broker']._session.posts) == 1
        finally: await f['store'].close()
    asyncio.run(scenario())


@pytest.mark.parametrize('change', [
    {'reserved_quantity': 1}, {'reserved_cash': '1'}, {'reserved_exposure': '1'},
    {'reserved_planned_risk': '1'}, {'reserved_quantity': False}, {'reserved_exposure': 'NaN'}, {},
])
def test_generic_replacement_capacity_requires_all_reservations_settled(tmp_path, monkeypatch, change):
    async def scenario():
        f = await fixture(tmp_path, monkeypatch)
        try:
            req = f['request']()
            await f['quote'](req)
            await f['commands'].prepare(req, f['entry'](req))
            def terminal(state):
                state['attempts']['A'].update(state='final_rejected', status='final_rejected',
                    reserved_quantity=0, reserved_cash='0', reserved_exposure='0', reserved_planned_risk=None)
                state['attempts']['A'].update(change)
                return state
            await f['runtime'].owner.mutate('synthetic-capacity-terminal', terminal)
            assert f['runtime'].lifecycle.replacement_quantity(req.intent_id) == (0 if change else 10)
        finally: await f['store'].close()
    asyncio.run(scenario())
