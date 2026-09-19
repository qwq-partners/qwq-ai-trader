"""승인 BR2 계약의 독립 실제 refresh/finalizer 수명 인수."""
import asyncio
from copy import deepcopy
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.execution.safety.application import ApplicationBlocked
from test_execution_regime_owner_boundaries_review import install, provider
from test_execution_runtime import NOW


@pytest.fixture(autouse=True)
def synthetic_home(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)


def trace_begins(monkeypatch, writer):
    calls = []
    original = writer.sources.begin
    async def begin(operation_id, kind, **kwargs):
        calls.append((operation_id, kind))
        return await original(operation_id, kind, **kwargs)
    monkeypatch.setattr(writer.sources, 'begin', begin)
    return calls


def refresh_task(writer, feed, experts=None):
    returned = asyncio.Event()
    async def invoke():
        try:
            return await writer.refresh_trend(feed, expert_orchestrator=experts)
        finally:
            returned.set()
    return asyncio.create_task(invoke()), returned


async def cancellation_returned(returned):
    # SQL은 Event로 계속 보류한다. timeout은 취소 반환 계약의 실패 감지 상한이다.
    try:
        await asyncio.wait_for(returned.wait(), 2)
        return True
    except TimeoutError:
        return False


async def cleanup(store, runtime, release, *tasks):
    release.set()
    await asyncio.gather(*(task for task in tasks if task is not None), return_exceptions=True)
    try:
        await asyncio.wait_for(runtime.shutdown(), 2)
    except ApplicationBlocked:
        pass
    await store.close()


def test_actual_expert_begin_cancel_returns_before_sql_and_finalizes_both_lanes(tmp_path, monkeypatch):
    async def scenario():
        _, _, store, runtime, writer = await install(tmp_path, lambda: NOW)
        before = deepcopy(runtime.owner.state['regime_policy'])
        begins = trace_begins(monkeypatch, writer)
        entered, release = asyncio.Event(), asyncio.Event()
        original = store.lookup_commit
        async def lookup(key):
            if key.startswith('command:risk-begin:') and begins[-1][1] == 'expert_regime':
                entered.set()
                await release.wait()
            return await original(key)
        monkeypatch.setattr(store, 'lookup_commit', lookup)
        gets, expert_calls = [], []
        experts = SimpleNamespace(
            aggregate_regime_score=lambda: expert_calls.append('score') or 25,
            bear_consensus=lambda **kwargs: expert_calls.append('consensus') or False)
        task, returned = refresh_task(writer, provider(lambda: NOW, calls=gets), experts)
        try:
            await asyncio.wait_for(entered.wait(), 2)
            task.cancel()
            returned_before_release = await cancellation_returned(returned)
            assert runtime._risk_source_pending
            release.set()
            result = (await asyncio.gather(task, return_exceptions=True))[0]
            await asyncio.wait_for(runtime.shutdown(), 2)
            rows = runtime.owner.state['risk_sources']['records']
            terminals = {kind: (rows[operation]['terminal'] or {}).get('receipt', {}).get('status')
                         for operation, kind in begins}
            assert begins[0][1] == 'index_trend' and begins[1][1] == 'expert_regime' and len(begins) == 2
            assert sorted(gets) == ['0001', '1001'] and expert_calls == []
            assert runtime.owner.state['regime_policy'] == before
            assert not runtime._command_scopes and not runtime._risk_source_pending
            assert runtime.owner.healthy and not runtime._command_results_failed
            assert isinstance(result, asyncio.CancelledError)
            assert (returned_before_release, terminals) == (
                True, {'index_trend': 'cancelled', 'expert_regime': 'cancelled'})
            assert all(rows[operation]['conflict'] is None for operation, _ in begins)
        finally:
            await cleanup(store, runtime, release, task)
    asyncio.run(scenario())


@pytest.mark.parametrize('kind', ['index_trend', 'expert_regime'])
def test_original_begin_sql_failure_during_cancel_is_not_hidden_or_retried(tmp_path, monkeypatch, kind):
    async def scenario():
        _, _, store, runtime, writer = await install(tmp_path, lambda: NOW)
        before = deepcopy(runtime.owner.state['regime_policy'])
        begins = trace_begins(monkeypatch, writer)
        entered, release = asyncio.Event(), asyncio.Event()
        original = store.lookup_commit
        async def lookup(key):
            if key.startswith('command:risk-begin:') and begins[-1][1] == kind:
                entered.set()
                await release.wait()
                raise OSError('independent original begin SQL failure')
            return await original(key)
        monkeypatch.setattr(store, 'lookup_commit', lookup)
        gets = []
        task, returned = refresh_task(writer, provider(lambda: NOW, calls=gets))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            task.cancel()
            returned_before_release = await cancellation_returned(returned)
            release.set()
            result = (await asyncio.gather(task, return_exceptions=True))[0]
            with pytest.raises(ApplicationBlocked, match='command_result_drain_failed'):
                await asyncio.wait_for(runtime.shutdown(), 2)
            assert runtime._command_results_failed and not runtime.owner.healthy
            assert runtime.owner.state['regime_policy'] == before
            assert not runtime._command_scopes and not runtime._risk_source_pending
            assert [item[1] for item in begins] == (['index_trend'] if kind == 'index_trend' else ['index_trend', 'expert_regime'])
            assert begins[-1][0] not in runtime.owner.state['risk_sources']['records']
            assert sorted(gets) == ([] if kind == 'index_trend' else ['0001', '1001'])
            assert returned_before_release and isinstance(result, asyncio.CancelledError), (
                returned_before_release, type(result).__name__, str(result))
        finally:
            await cleanup(store, runtime, release, task)
    asyncio.run(scenario())


@pytest.mark.parametrize('kind', ['index_trend', 'expert_regime'])
@pytest.mark.parametrize('boundary', ['closing', 'day_fence'])
def test_cancel_after_original_sql_commit_uses_original_ticket_across_boundary(tmp_path, monkeypatch, kind, boundary):
    async def scenario():
        current = [NOW]
        _, _, store, runtime, writer = await install(tmp_path, lambda: current[0])
        before = deepcopy(runtime.owner.state['regime_policy'])
        begins = trace_begins(monkeypatch, writer)
        entered, release = asyncio.Event(), asyncio.Event()
        original = store.commit
        durable = {}
        async def commit(expected, state, key):
            version = await original(expected, state, key)
            if key.startswith('command:risk-begin:') and begins[-1][1] == kind:
                operation = begins[-1][0]
                durable.update(version=version, key=key,
                               ticket=deepcopy(state['risk_sources']['records'][operation]['ticket']))
                entered.set()
                await release.wait()
            return version
        monkeypatch.setattr(store, 'commit', commit)
        gets = []
        task, returned = refresh_task(writer, provider(lambda: current[0], calls=gets))
        boundary_task = None
        try:
            await asyncio.wait_for(entered.wait(), 2)
            assert await store.lookup_commit(durable['key']) == durable['version']
            assert durable['ticket']['operation_id'] not in runtime.owner.state['risk_sources']['records']
            task.cancel()
            returned_before_release = await cancellation_returned(returned)
            if boundary == 'closing':
                boundary_task = asyncio.create_task(runtime.shutdown())
            else:
                current[0] += timedelta(days=1)
                boundary_task = asyncio.create_task(runtime.prepare_day_rollover(
                    'cancel-finalizer-day', expected_version=durable['version'],
                    from_day=NOW.date().isoformat(), to_day=current[0].date().isoformat(),
                    valuation_boundary=current[0]))
            boundary_started = asyncio.Event()
            asyncio.get_running_loop().call_soon(boundary_started.set)
            await boundary_started.wait()
            if boundary == 'closing':
                assert runtime._closing
            else:
                assert runtime._day_generation == durable['ticket']['generation'] + 1
                assert runtime._day_fence_id != durable['ticket']['fence_id']
            assert not boundary_task.done()
            release.set()
            result = (await asyncio.gather(task, return_exceptions=True))[0]
            await asyncio.wait_for(boundary_task, 2)
            await asyncio.wait_for(runtime.shutdown(), 2)
            rows = runtime.owner.state['risk_sources']['records']
            target = rows[durable['ticket']['operation_id']]
            assert target['ticket'] == durable['ticket'], 'original day/generation/fence ticket must survive'
            assert [item[1] for item in begins] == (['index_trend'] if kind == 'index_trend' else ['index_trend', 'expert_regime'])
            assert sorted(gets) == ([] if kind == 'index_trend' else ['0001', '1001'])
            assert runtime.owner.state['regime_policy'] == before
            assert not runtime._command_scopes and not runtime._risk_source_pending
            assert runtime.owner.healthy and not runtime._command_results_failed
            assert isinstance(result, asyncio.CancelledError)
            expected_status = 'cancelled' if boundary == 'closing' else 'stale'
            terminal = target['terminal']
            assert (returned_before_release, terminal is not None) == (True, True)
            assert terminal['receipt']['status'] == expected_status
            assert terminal['envelope']['outcome'] == 'cancelled'
            assert terminal['receipt']['reason'] == ('' if boundary == 'closing' else 'day_or_generation')
            assert target['conflict'] is None
        finally:
            await cleanup(store, runtime, release, task, boundary_task)
    asyncio.run(scenario())


@pytest.mark.parametrize('token_case', ['forged', 'expired', 'foreign'])
def test_finalizer_rejects_non_owned_token_without_child_scope(tmp_path, token_case):
    async def scenario():
        _, _, store, runtime, _ = await install(tmp_path, lambda: NOW)
        called = []
        async def operation(child_token):
            called.append(child_token)
            return True
        try:
            helper = runtime._start_command_finalizer
            before = runtime.owner.state
            if token_case == 'forged':
                with pytest.raises(ApplicationBlocked, match='command_scope_required'):
                    helper(asyncio.get_running_loop().create_future(), operation)
            elif token_case == 'expired':
                with runtime.command_scope() as token:
                    pass
                with pytest.raises(ApplicationBlocked, match='command_scope_required'):
                    helper(token, operation)
            else:
                with runtime.command_scope() as token:
                    async def foreign():
                        with pytest.raises(ApplicationBlocked, match='command_scope_required'):
                            helper(token, operation)
                    await asyncio.create_task(foreign())
            assert not called and not runtime._command_scopes and not runtime._command_result_tasks
            assert runtime.owner.state == before and not runtime._command_results_failed
        finally:
            await runtime.shutdown()
            await store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('failure', ['prestart_cancel', 'exception'])
def test_finalizer_failure_has_no_scope_leak_and_keeps_failure_latch(tmp_path, failure):
    async def scenario():
        _, _, store, runtime, _ = await install(tmp_path, lambda: NOW)
        entered = []
        async def operation(child):
            entered.append(child)
            raise OSError('independent finalizer failure')
        try:
            helper = runtime._start_command_finalizer
            with runtime.command_scope() as parent:
                task = helper(parent, operation)
                assert len(runtime._command_scopes) == 2
                child = next(token for token in runtime._command_scopes if token is not parent)
                assert runtime._command_scopes[child] is task
                if failure == 'prestart_cancel':
                    task.cancel()
            result = (await asyncio.gather(task, return_exceptions=True))[0]
            with pytest.raises(ApplicationBlocked, match='command_result_drain_failed'):
                await asyncio.wait_for(runtime.shutdown(), 2)
            assert not runtime._command_scopes and child.done()
            assert runtime._command_results_failed and not runtime.owner.healthy
            if failure == 'prestart_cancel':
                assert entered == [] and isinstance(result, asyncio.CancelledError)
            else:
                assert entered == [child] and isinstance(result, OSError)
        finally:
            try:
                await runtime.shutdown()
            except ApplicationBlocked:
                pass
            await store.close()
    asyncio.run(scenario())


def test_finalizer_owns_distinct_token_and_drains_closing_admitted_parent(tmp_path):
    async def scenario():
        _, _, store, runtime, writer = await install(tmp_path, lambda: NOW)
        entered, release = asyncio.Event(), asyncio.Event()
        children = []
        async def operation(child):
            children.append(child)
            assert runtime._command_scopes[child] is asyncio.current_task()
            entered.set()
            await release.wait()
            ticket = await writer.sources.begin('finalizer-owned-begin', 'vix_regime', scope_token=child)
            assert (await writer.sources.complete(ticket, 'missing', scope_token=child)).status == 'missing'
            return True
        task = shutdown = None
        try:
            helper = runtime._start_command_finalizer
            with runtime.command_scope() as parent:
                # 실제 shutdown은 다른 task에서 시작하고 첫 await가 넘었다는 Event로 확인한다.
                shutdown = asyncio.create_task(runtime.shutdown())
                closing_started = asyncio.Event()
                asyncio.get_running_loop().call_soon(closing_started.set)
                await closing_started.wait()
                assert runtime._closing
                task = helper(parent, operation)
                child = next(token for token in runtime._command_scopes if token is not parent)
                assert child is not parent and runtime._command_scopes[child] is task
            await asyncio.wait_for(entered.wait(), 2)
            assert not shutdown.done() and children == [child]
            release.set()
            assert await task is True
            await asyncio.wait_for(shutdown, 2)
            assert not runtime._command_scopes and not runtime._command_result_tasks
            assert runtime.owner.healthy and not runtime._command_results_failed
        finally:
            await cleanup(store, runtime, release, task, shutdown)
    asyncio.run(scenario())
