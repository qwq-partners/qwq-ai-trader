"""실제 owner에서 시장 원관측의 미완 게시를 주문 근거로 쓰지 않는다."""
import asyncio
from copy import deepcopy
from decimal import Decimal
from dataclasses import replace
from contextlib import suppress

import pytest

from src.execution.safety.lifecycle import CommandStatus
from src.execution.safety.application import ApplicationBlocked
from src.execution.safety.protection_recovery import digest
from test_execution_runtime import setup, opened, observed, queued
from src.execution.safety.transport import GuardedKISTransport
from test_execution_command_owner import fixture


@pytest.mark.parametrize('boundary', ['before_admission', 'after_admission'])
@pytest.mark.parametrize('operation', ['prepare', 'dispatch'])
def test_inflight_market_source_blocks_submit_until_protection_is_applied(
        tmp_path, monkeypatch, boundary, operation):
    async def scenario():
        f = await fixture(tmp_path, monkeypatch)
        runtime, commands = f['runtime'], f['commands']
        reached, release = asyncio.Event(), asyncio.Event()
        task = None
        try:
            request = f['request']()
            await f['quote'](request)
            if operation == 'dispatch':
                await commands.prepare(request, f['entry'](request))
            original = runtime.owner.mutate
            target = 'quote-admit:' if boundary == 'before_admission' else 'quote:'

            async def hold(command_id, reducer):
                if command_id.startswith(target):
                    reached.set()
                    await release.wait()
                return await original(command_id, reducer)

            monkeypatch.setattr(runtime.owner, 'mutate', hold)
            task = asyncio.create_task(runtime.quote(
                request.symbol, request.valuation_price, market_as_of=f['clock'][0],
                source='synthetic-market', source_event_id='inflight-market'))
            await asyncio.wait_for(reached.wait(), 2)
            assert runtime.owner.healthy  # DB 장애가 아니라 실제 미완 source 게시다.
            pending = runtime.owner.state.get('protection_quote_admissions', {})
            assert bool(pending) == (boundary == 'after_admission')
            if operation == 'prepare':
                with pytest.raises(ValueError, match='market_source_pending'):
                    await commands.prepare(request, f['entry'](request))
            else:
                result = await commands.dispatch(request, f['entry'](request),
                    GuardedKISTransport(f['broker'], request_builder=f['builder']))
                assert result.status is CommandStatus.NOT_SENT
            assert not f['broker']._session.posts
            release.set()
            await asyncio.wait_for(task, 2)
            if operation == 'prepare':
                await commands.prepare(request, f['entry'](request))
            result = await commands.dispatch(request, f['entry'](request),
                GuardedKISTransport(f['broker'], request_builder=f['builder']))
            # S3-3: claim 이전 실패는 예약을 남기지 않고 시도를 끝낸다 — 앞서 거부된 dispatch 의
            # 같은 attempt 는 다시 보낼 수 없고, 재송신은 새 prepare 로만 열린다.
            assert result.status is (CommandStatus.ACKNOWLEDGED if operation == 'prepare'
                                     else CommandStatus.NOT_SENT)
            assert len(f['broker']._session.posts) == (1 if operation == 'prepare' else 0)
        finally:
            release.set()
            if task is not None:
                await asyncio.gather(task, return_exceptions=True)
            await runtime.shutdown()
            await f['store'].close()
    asyncio.run(scenario())


async def market_event(monkeypatch, now, *, price='10000', symbol='005930', tr_id='H0STCNT0'):
    """실제 feed에 합성 원문을 넣되 인증/연결 경계는 사용하지 않는다."""
    from src.data.feeds import kis_websocket as ws
    monkeypatch.setattr(ws, 'get_token_manager', lambda: object())
    feed = ws.KISWebSocketFeed(ws.KISWebSocketConfig(ws_url='ws://offline.invalid'), clock=lambda: now)
    events = []
    async def receive(event): events.append(event)
    feed.on_market_data(receive)
    row = ['0'] * 46
    for index, value in {0: symbol, 1: now.strftime('%H%M%S'), 2: price, 3: '3',
                         7: price, 8: price, 9: price, 13: '10', 14: '100000',
                         33: now.strftime('%Y%m%d')}.items():
        row[index] = value
    await feed._handle_message(f'0|{tr_id}|001|' + '^'.join(row))
    assert len(events) == 1
    return events[0]


@pytest.mark.parametrize('tr_id', ['H0STCNT0', 'H0NXCNT0'])
def test_real_feed_observation_completes_entry_and_protection_in_same_commit(tmp_path, monkeypatch, tr_id):
    async def scenario():
        f = await fixture(tmp_path, monkeypatch)
        runtime = f['runtime']
        try:
            assert callable(getattr(runtime, 'observe_market', None)), 'market owner publisher missing'
            event = await market_event(monkeypatch, f['clock'][0], tr_id=tr_id)
            before = runtime.owner.version
            result = await runtime.observe_market(event)
            assert result is None  # 보유 없는 정상 시세이며 청산 제안은 없다.
            state = runtime.owner.state
            proof = state['market_sources'][event.symbol]
            assert proof['observation'] == event.observation.to_dict()
            assert proof['admission_version'] == before + 1
            assert proof['completed_version'] == runtime.owner.version == before + 2
            assert state['entry_quotes'][event.symbol]['event_id'] == event.observation.source_event_id
            assert state['latest_explicit_quote'][event.symbol]['source'] == event.observation.source
            assert not state['protection_quote_admissions']
            await runtime.restore()
            assert runtime.owner.state['market_sources'] == state['market_sources']
            request = f['request']()
            await f['commands'].prepare(request, f['entry'](request))
            ack = await f['commands'].dispatch(request, f['entry'](request),
                GuardedKISTransport(f['broker'], request_builder=f['builder']))
            assert ack.status is CommandStatus.ACKNOWLEDGED
            assert len(f['broker']._session.posts) == 1
        finally:
            await runtime.shutdown()
            await f['store'].close()
    asyncio.run(scenario())


@pytest.mark.parametrize('changed', ['symbol', 'close', 'open', 'high', 'low', 'volume', 'value', 'change', 'change_pct', 'source'])
def test_mutated_event_cannot_override_frozen_market_observation(tmp_path, monkeypatch, changed):
    async def scenario():
        f = await fixture(tmp_path, monkeypatch)
        runtime = f['runtime']
        try:
            assert callable(getattr(runtime, 'observe_market', None)), 'market owner publisher missing'
            event = await market_event(monkeypatch, f['clock'][0])
            before = runtime.owner.state
            values = {'symbol': '000660', 'source': 'rest-relabelled', 'volume': 11,
                      'change_pct': 1.0}
            setattr(event, changed, values.get(changed, Decimal('99999')))
            with pytest.raises(ValueError, match='market_event_observation_mismatch'):
                await runtime.observe_market(event)
            assert runtime.owner.state == before
            assert not runtime._protection_tasks and not f['broker']._session.posts
        finally:
            await runtime.shutdown()
            await f['store'].close()
    asyncio.run(scenario())


def test_entry_quote_is_not_published_before_protection_and_caller_cancel_does_not_drop_source(tmp_path, monkeypatch):
    async def scenario():
        f = await fixture(tmp_path, monkeypatch)
        runtime = f['runtime']
        reached, release = asyncio.Event(), asyncio.Event()
        task = None
        try:
            assert callable(getattr(runtime, 'observe_market', None)), 'market owner publisher missing'
            request = f['request']()
            await f['quote'](request)
            previous = deepcopy(runtime.owner.state['entry_quotes'])
            event = await market_event(monkeypatch, f['clock'][0], price='10001')
            original = runtime.owner.mutate
            async def hold(command_id, reducer):
                if command_id.startswith('quote:'):
                    reached.set()
                    await release.wait()
                return await original(command_id, reducer)
            monkeypatch.setattr(runtime.owner, 'mutate', hold)
            task = asyncio.create_task(runtime.observe_market(event))
            await asyncio.wait_for(reached.wait(), 2)
            assert runtime.owner.state['entry_quotes'] == previous
            admission, = runtime.owner.state['protection_quote_admissions'].values()
            assert admission['entry_observation'] == event.observation.to_dict()
            task.cancel()
            with pytest.raises(asyncio.CancelledError): await task
            closing = asyncio.create_task(runtime.shutdown())
            await asyncio.sleep(0)
            assert not closing.done()
            release.set()
            await asyncio.wait_for(closing, 2)
            state = runtime.owner.state
            assert state['entry_quotes'][event.symbol]['price'] == '10001'
            assert state['market_sources'][event.symbol]['completed_version'] == runtime.owner.version
            assert not state['protection_quote_admissions']
            assert not f['broker']._session.posts
        finally:
            release.set()
            if task is not None: await asyncio.gather(task, return_exceptions=True)
            await runtime.shutdown()
            await f['store'].close()
    asyncio.run(scenario())


@pytest.mark.parametrize('kind', ['entry_only', 'bare_explicit', 'bare_without_time'])
def test_bound_source_cannot_be_replaced_by_a_legacy_price_writer(tmp_path, monkeypatch, kind):
    async def scenario():
        f = await fixture(tmp_path, monkeypatch)
        runtime, commands = f['runtime'], f['commands']
        try:
            event = await market_event(monkeypatch, f['clock'][0])
            await runtime.observe_market(event)
            request = f['request']()
            if kind == 'entry_only':
                with pytest.raises(ValueError, match='bound_market_source_required'):
                    await commands.observe_entry_quote(event.symbol, event.close,
                        as_of=event.observation.market_as_of, source='fake-rest', event_id='fake',
                        expected_version=runtime.owner.version)
            else:
                params = (dict(market_as_of=f['clock'][0], source='legacy', source_event_id='same-price')
                          if kind == 'bare_explicit' else {})
                await runtime.quote(event.symbol, event.close, **params)
                await runtime.restore()
                with pytest.raises(ValueError, match='current_market_source_required'):
                    await commands.prepare(request, f['entry'](request))
                assert not f['broker']._session.posts
                # 별도 원관측이 두 projection을 다시 완료한 뒤에만 정상 복구한다.
                event = await market_event(monkeypatch, f['clock'][0])
                await runtime.observe_market(event)
            await commands.prepare(request, f['entry'](request))
            ack = await commands.dispatch(request, f['entry'](request),
                GuardedKISTransport(f['broker'], request_builder=f['builder']))
            assert ack.status is CommandStatus.ACKNOWLEDGED
        finally:
            await runtime.shutdown()
            await f['store'].close()
    asyncio.run(scenario())


@pytest.mark.parametrize('fault', [
    'bool_version', 'future_version', 'equal_version', 'missing_entry', 'entry_price',
    'request_digest', 'request_price', 'request_naive', 'missing_observation',
    'decision_shape', 'decision_without_outbox', 'negative_invalidation',
])
def test_restore_validates_market_completion_proof_even_for_unheld_symbol(tmp_path, monkeypatch, fault):
    async def scenario():
        f = await fixture(tmp_path, monkeypatch)
        runtime = f['runtime']
        try:
            event = await market_event(monkeypatch, f['clock'][0])
            await runtime.observe_market(event)
            bad = runtime.owner.state
            proof = bad['market_sources'][event.symbol]
            if fault == 'bool_version': proof['admission_version'] = True
            elif fault == 'future_version': proof['completed_version'] = runtime.owner.version + 2
            elif fault == 'equal_version': proof['admission_version'] = proof['completed_version']
            elif fault == 'missing_entry': del bad['entry_quotes'][event.symbol]
            elif fault == 'entry_price': bad['entry_quotes'][event.symbol]['price'] = '10001'
            elif fault == 'request_digest': proof['request_digest'] = 'bad'
            elif fault == 'request_price':
                proof['request']['price'] = '10001'
                proof['request_digest'] = digest(proof['request'])
            elif fault == 'request_naive':
                proof['request']['observed_at'] = f['clock'][0].replace(tzinfo=None).isoformat()
                proof['request_digest'] = digest(proof['request'])
            elif fault == 'missing_observation': del proof['request']['entry_observation']
            elif fault == 'decision_shape': proof['decision'] = ['fake']
            elif fault == 'decision_without_outbox': proof['decision'] = ['sell_all', 1, 'fake']
            else: proof['invalidated_at_version'] = -1
            await f['store'].commit(runtime.owner.version, bad, 'synthetic-corrupt-market-proof')
            with pytest.raises(ApplicationBlocked): await runtime.restore()
            assert not runtime.owner.healthy
            assert not f['broker']._session.posts
        finally: await f['store'].close()
    asyncio.run(scenario())


@pytest.mark.parametrize('boundary', ['connect', 'token', 'hashkey', 'limiter'])
def test_inflight_source_is_rechecked_after_each_real_transport_await(tmp_path, monkeypatch, boundary):
    async def scenario():
        f = await fixture(tmp_path, monkeypatch)
        runtime, commands = f['runtime'], f['commands']
        reached, release = asyncio.Event(), asyncio.Event()
        price_task = None
        try:
            request = f['request']()
            await f['quote'](request)
            await commands.prepare(request, f['entry'](request))
            mutate = runtime.owner.mutate
            async def hold(command_id, reducer):
                if command_id.startswith('quote:'):
                    reached.set()
                    await release.wait()
                return await mutate(command_id, reducer)
            monkeypatch.setattr(runtime.owner, 'mutate', hold)
            async def change():
                nonlocal price_task
                price_task = asyncio.create_task(runtime.quote(request.symbol, request.valuation_price,
                    market_as_of=f['clock'][0], source='synthetic', source_event_id='network-gap'))
                await asyncio.wait_for(reached.wait(), 2)
                assert runtime.owner.healthy
            if boundary == 'connect':
                f['broker']._session.closed = True
                original = f['broker'].connect
                async def hook(*args, **kwargs):
                    result = await original(*args, **kwargs)
                    await change()
                    return result
                f['broker'].connect = hook
            else:
                if boundary == 'token': f['broker']._token = None
                name = {'token': '_ensure_token', 'hashkey': '_get_hashkey', 'limiter': '_rate_limit'}[boundary]
                original = getattr(f['broker'], name)
                async def hook(*args, **kwargs):
                    result = await original(*args, **kwargs)
                    await change()
                    return result
                monkeypatch.setattr(f['broker'], name, hook)
            result = await commands.dispatch(request, f['entry'](request),
                GuardedKISTransport(f['broker'], request_builder=f['builder']))
            assert result.status is CommandStatus.NOT_SENT
            assert not f['broker']._session.posts
        finally:
            release.set()
            if price_task is not None: await asyncio.gather(price_task, return_exceptions=True)
            await runtime.shutdown()
            await f['store'].close()
    asyncio.run(scenario())


@pytest.mark.parametrize('held', [False, True])
def test_duplicate_source_reads_durable_decision_without_reapplying_price(tmp_path, monkeypatch, held):
    async def scenario():
        engine, exits, store, runtime = await setup(tmp_path, account_scope='scope')
        try:
            if held:
                ref = await opened(runtime, 'B1')
                await queued(engine, await observed(runtime, ref, 100, '1000000'))
            event = await market_event(monkeypatch, runtime._now(), price='9000')
            decision = await runtime.observe_market(event, intent_id='owned-stop')
            if held:
                assert decision[0] == 'sell_all' and decision[1] == 100
            else: assert decision is None
            before, version = runtime.owner.state, runtime.owner.version
            assert before['entry_quotes'][event.symbol]['price'] == '9000'
            await runtime.restore()
            assert await runtime.observe_market(event, intent_id='owned-stop') == decision
            assert runtime.owner.state == before and runtime.owner.version == version
            assert not runtime.trading_ready
        finally:
            await runtime.shutdown()
            await store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('fault', ['apply_commit', 'apply_publication'])
def test_market_apply_failure_reopens_with_exact_pending_or_completed_evidence(tmp_path, monkeypatch, fault):
    async def scenario():
        from src.core.engine import UnifiedEngine
        from src.core.types import TradingConfig
        from src.strategies.exit_manager import ExitManager
        from src.execution.safety.store import ExecutionStateStore, StoreError
        from src.execution.safety.runtime import KRExecutionRuntime
        f = await fixture(tmp_path, monkeypatch)
        runtime, store = f['runtime'], f['store']
        fresh = None
        try:
            request = f['request']()
            await f['quote'](request)
            event = await market_event(monkeypatch, f['clock'][0])
            if fault == 'apply_commit':
                commit = store.commit
                async def broken(expected, payload, commit_id):
                    if commit_id.startswith('command:quote:'):
                        raise StoreError('synthetic-market-apply-failure')
                    return await commit(expected, payload, commit_id)
                monkeypatch.setattr(store, 'commit', broken)
            else:
                publisher = runtime.owner.publisher
                def broken(state, version):
                    if state.get('market_sources'):
                        raise ValueError('synthetic-market-publish-failure')
                    return publisher(state, version)
                monkeypatch.setattr(runtime.owner, 'publisher', broken)
            with pytest.raises((StoreError, ApplicationBlocked)):
                await runtime.observe_market(event)
            assert not runtime.owner.healthy
            with suppress(ApplicationBlocked): await runtime.shutdown()
            await store.close()
            engine = UnifiedEngine(TradingConfig(initial_capital=Decimal('2000000')))
            exits = ExitManager(persist=False, clock=lambda: f['clock'][0])
            fresh_store = ExecutionStateStore(store.path)
            fresh = KRExecutionRuntime(fresh_store, engine, exits, clock=lambda: f['clock'][0],
                                       account_scope='test-scope')
            monkeypatch.setattr(KRExecutionRuntime, 'trading_ready', property(lambda self: False))
            await fresh.restore()
            fresh.attach()
            assert fresh.owner.healthy and not fresh.trading_ready
            state = fresh.owner.state
            if fault == 'apply_commit':
                admission, = state['protection_quote_admissions'].values()
                assert admission['entry_observation'] == event.observation.to_dict()
                assert not state.get('market_sources')
                assert fresh.market_source_pending(state)
                newer = await market_event(monkeypatch, f['clock'][0], price='10400')
                with pytest.raises(ApplicationBlocked): await fresh.observe_market(newer)
                assert fresh.owner.state == state  # 미해결 원 입력을 높은 후속 가격이 덮지 못한다.
            else:
                assert not state['protection_quote_admissions']
                assert state['market_sources'][event.symbol]['observation'] == event.observation.to_dict()
                version = fresh.owner.version
                assert await fresh.observe_market(event) is None
                assert fresh.owner.version == version
            assert not f['broker']._session.posts
        finally:
            with suppress(ApplicationBlocked): await runtime.shutdown()
            await store.close()
            if fresh is not None:
                with suppress(ApplicationBlocked): await fresh.shutdown()
                await fresh.owner.store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('fault', ['yesterday', 'future_market', 'future_receipt', 'missing_observation'])
def test_market_time_is_original_not_relabelled_to_now(tmp_path, monkeypatch, fault):
    async def scenario():
        from datetime import timedelta
        f = await fixture(tmp_path, monkeypatch)
        runtime = f['runtime']
        try:
            event = await market_event(monkeypatch, f['clock'][0])
            obs = event.observation
            if fault == 'yesterday':
                event.observation = replace(obs, raw_date='20260917')
            elif fault == 'future_market': event.observation = replace(obs, raw_time='110001')
            elif fault == 'future_receipt':
                event.observation = replace(obs, received_at=f['clock'][0] + timedelta(seconds=1))
            else: event.observation = None
            before = runtime.owner.state
            with pytest.raises(ValueError): await runtime.observe_market(event)
            assert runtime.owner.state == before and not runtime._protection_tasks
            assert not f['broker']._session.posts
        finally:
            await runtime.shutdown()
            await f['store'].close()
    asyncio.run(scenario())


def test_market_pending_does_not_add_a_new_alpha_barrier_to_cancel(tmp_path, monkeypatch):
    async def scenario():
        from src.execution.safety.requests import CancelParent
        from src.execution.safety.lifecycle import OrderRef
        f = await fixture(tmp_path, monkeypatch)
        runtime, commands = f['runtime'], f['commands']
        reached, release = asyncio.Event(), asyncio.Event()
        task = None
        try:
            request = f['request']()
            await f['quote'](request)
            await commands.prepare(request, f['entry'](request))
            transport = GuardedKISTransport(f['broker'], request_builder=f['builder'])
            assert (await commands.dispatch(request, f['entry'](request), transport)).status is CommandStatus.ACKNOWLEDGED
            parent = runtime.owner.state['attempts']['A']
            ref = CancelParent(request.intent_id, request.attempt_id, parent['version'],
                OrderRef.from_dict(parent['order_ref']), request.symbol, request.side,
                request.order_type, parent['reserved_quantity'], request.valuation_price, request.strategy)
            cancel = f['builder'].prepare_cancel(intent_id=request.intent_id, attempt_id='C',
                                                 session=request.session, parent=ref)
            mutate = runtime.owner.mutate
            async def hold(command_id, reducer):
                if command_id.startswith('quote:'):
                    reached.set()
                    await release.wait()
                return await mutate(command_id, reducer)
            monkeypatch.setattr(runtime.owner, 'mutate', hold)
            task = asyncio.create_task(runtime.quote(request.symbol, request.valuation_price,
                market_as_of=f['clock'][0], source='synthetic', source_event_id='pending'))
            await asyncio.wait_for(reached.wait(), 2)
            assert runtime.market_source_pending(runtime.owner.state)
            await commands.prepare(cancel, f['entry'](cancel))
            result = await commands.dispatch(cancel, f['entry'](cancel), transport)
            assert result.status is CommandStatus.ACKNOWLEDGED
            assert runtime.owner.state['attempts']['A'] == parent  # 취소 ACK는 부모 예약 해제가 아니다.
            assert len(f['broker']._session.posts) == 2
        finally:
            release.set()
            if task is not None: await asyncio.gather(task, return_exceptions=True)
            await runtime.shutdown()
            await f['store'].close()
    asyncio.run(scenario())
