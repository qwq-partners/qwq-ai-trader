"""실제 owner 명령/늦은 결과 저장의 종료 인수. 운영 설치·실 HTTP는 아니다."""
import asyncio
from dataclasses import replace
from decimal import Decimal

import pytest

from src.execution.safety.application import ApplicationBlocked
from src.execution.safety.lifecycle import CommandStatus
from src.execution.safety.policy_snapshot import PolicyContext
from src.execution.safety.store import StoreError
from src.execution.safety.transport import GuardedKISTransport
from test_execution_command_owner import fixture


@pytest.mark.parametrize('boundary', ['response', 'result', 'cancelled_caller_result'])
def test_shutdown_drains_command_and_result_created_after_shutdown_begins(tmp_path, monkeypatch, boundary):
    async def scenario():
        f = await fixture(tmp_path, monkeypatch)
        runtime, commands = f['runtime'], f['commands']
        reached, release = asyncio.Event(), asyncio.Event()
        tasks = []
        try:
            req = f['request']()
            await f['quote'](req)
            await commands.prepare(req, f['entry'](req))
            if boundary == 'response':
                original = f['broker']._session.response.json
                async def hold():
                    reached.set()
                    await release.wait()
                    return await original()
                f['broker']._session.response.json = hold
            else:
                original = runtime.lifecycle.record_result
                async def hold(*args, **kwargs):
                    reached.set()
                    await release.wait()
                    return await original(*args, **kwargs)
                monkeypatch.setattr(runtime.lifecycle, 'record_result', hold)
            send = asyncio.create_task(commands.dispatch(req, f['entry'](req),
                GuardedKISTransport(f['broker'], request_builder=f['builder'])))
            tasks.append(send)
            await asyncio.wait_for(reached.wait(), 2)
            if boundary == 'cancelled_caller_result':
                send.cancel()
                with pytest.raises(asyncio.CancelledError): await send
                assert commands._result_tasks
            closing = asyncio.create_task(runtime.shutdown())
            tasks.append(closing)
            await asyncio.sleep(0)  # 종료 coroutine을 정확히 한 번 스케줄한다.
            assert runtime._closing
            assert not closing.done(), 'accepted command/result must drain before shutdown returns'
            release.set()
            await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 2)
            assert not closing.cancelled() and closing.exception() is None
            row = runtime.owner.state['attempts']['A']
            assert row['command_status'] == 'acknowledged'
            assert row['reserved_cash'] == '101500.000'
            assert not commands._result_tasks
            assert len(f['broker']._session.posts) == 1
            await runtime.shutdown()  # 멱등 종료
        finally:
            release.set()
            if tasks: await asyncio.gather(*tasks, return_exceptions=True)
            if commands._result_tasks:
                await asyncio.gather(*tuple(commands._result_tasks), return_exceptions=True)
            await f['store'].close()
    asyncio.run(scenario())


@pytest.mark.parametrize('operation', ['prepare', 'quote', 'policy'])
def test_shutdown_drains_already_started_owner_commit(tmp_path, monkeypatch, operation):
    async def scenario():
        f = await fixture(tmp_path, monkeypatch)
        runtime, commands = f['runtime'], f['commands']
        reached, release = asyncio.Event(), asyncio.Event()
        tasks = []
        try:
            req = f['request']()
            await f['quote'](req)
            original = f['store'].commit
            async def hold(*args, **kwargs):
                reached.set()
                await release.wait()
                return await original(*args, **kwargs)
            monkeypatch.setattr(f['store'], 'commit', hold)
            if operation == 'prepare':
                call = commands.prepare(req, f['entry'](req))
            elif operation == 'quote':
                call = commands.observe_entry_quote(req.symbol, Decimal('10001'), as_of=f['clock'][0],
                    source='synthetic', event_id='next', expected_version=runtime.owner.version)
            else:
                context = PolicyContext.from_dict(runtime.owner.state['entry_policy_context'])
                context = replace(context, versions=replace(context.versions, execution=runtime.owner.version))
                call = commands.publish_policy_context(context, expected_version=runtime.owner.version)
            operation_task = asyncio.create_task(call)
            tasks.append(operation_task)
            await asyncio.wait_for(reached.wait(), 2)
            closing = asyncio.create_task(runtime.shutdown())
            tasks.append(closing)
            await asyncio.sleep(0)
            assert not closing.done(), 'in-flight owner write is not yet drained'
            release.set()
            await asyncio.wait_for(asyncio.gather(*tasks), 2)
            assert runtime.owner.healthy
            assert runtime.owner.version == runtime.engine._execution_version
            assert not f['broker']._session.posts
        finally:
            release.set()
            if tasks: await asyncio.gather(*tasks, return_exceptions=True)
            await f['store'].close()
    asyncio.run(scenario())


def test_failed_result_drain_is_not_reported_as_successful_shutdown(tmp_path, monkeypatch):
    async def scenario():
        f = await fixture(tmp_path, monkeypatch)
        runtime = f['runtime']
        try:
            req = f['request']()
            await f['quote'](req)
            await f['commands'].prepare(req, f['entry'](req))
            async def fail(*args, **kwargs): raise StoreError('synthetic-result-storage-failure')
            monkeypatch.setattr(runtime.lifecycle, 'record_result', fail)
            result = await f['commands'].dispatch(req, f['entry'](req),
                GuardedKISTransport(f['broker'], request_builder=f['builder']))
            assert result.status is CommandStatus.UNKNOWN
            assert not runtime.owner.healthy
            with pytest.raises(ApplicationBlocked, match='command_result_drain_failed'):
                await runtime.shutdown()
            assert runtime.owner.state['attempts']['A']['reserved_cash'] == '101500.000'
            assert len(f['broker']._session.posts) == 1
        finally: await f['store'].close()
    asyncio.run(scenario())


@pytest.mark.parametrize('boundary', ['limiter', 'response', 'result'])
def test_cancelled_shutdown_does_not_cancel_accepted_command_or_late_result(tmp_path, monkeypatch, boundary):
    async def scenario():
        f = await fixture(tmp_path, monkeypatch)
        runtime = f['runtime']
        reached, release = asyncio.Event(), asyncio.Event()
        tasks = []
        try:
            req = f['request']()
            await f['quote'](req)
            await f['commands'].prepare(req, f['entry'](req))
            if boundary == 'limiter':
                original = f['broker']._rate_limit
            elif boundary == 'response':
                original = f['broker']._session.response.json
            else:
                original = runtime.lifecycle.record_result
            async def hold(*args, **kwargs):
                reached.set()
                await release.wait()
                return await original(*args, **kwargs)
            if boundary == 'limiter': f['broker']._rate_limit = hold
            elif boundary == 'response': f['broker']._session.response.json = hold
            else: monkeypatch.setattr(runtime.lifecycle, 'record_result', hold)
            send = asyncio.create_task(f['commands'].dispatch(req, f['entry'](req),
                GuardedKISTransport(f['broker'], request_builder=f['builder'])))
            tasks.append(send)
            await asyncio.wait_for(reached.wait(), 2)
            closing = asyncio.create_task(runtime.shutdown())
            tasks.append(closing)
            await asyncio.sleep(0)
            closing.cancel()
            with pytest.raises(asyncio.CancelledError): await closing
            assert not send.done()
            assert not any(task.cancelled() for task in f['commands']._result_tasks)
            another_close = asyncio.create_task(runtime.shutdown())
            tasks.append(another_close)
            await asyncio.sleep(0)
            assert not another_close.done()
            release.set()
            result = await asyncio.wait_for(send, 2)
            await asyncio.wait_for(another_close, 2)
            assert result.status is (CommandStatus.NOT_SENT if boundary == 'limiter' else CommandStatus.ACKNOWLEDGED)
            assert len(f['broker']._session.posts) == (0 if boundary == 'limiter' else 1)
            assert runtime.health()['command_operations_pending'] == 0
            assert runtime.health()['command_results_pending'] == 0
            assert not runtime.health()['command_results_failed']
        finally:
            release.set()
            if tasks: await asyncio.gather(*tasks, return_exceptions=True)
            await f['store'].close()
    asyncio.run(scenario())


@pytest.mark.parametrize('operation', ['prepare', 'quote', 'policy', 'dispatch'])
def test_closed_runtime_does_not_admit_new_commands_or_publishers(tmp_path, monkeypatch, operation):
    async def scenario():
        f = await fixture(tmp_path, monkeypatch)
        try:
            req = f['request']()
            await f['quote'](req)
            await f['commands'].prepare(req, f['entry'](req))
            await f['runtime'].shutdown()
            before = f['runtime'].owner.state
            version = f['runtime'].owner.version
            if operation == 'dispatch':
                result = await f['commands'].dispatch(req, f['entry'](req),
                    GuardedKISTransport(f['broker'], request_builder=f['builder']))
                assert result.status is CommandStatus.NOT_SENT
            else:
                with pytest.raises(ApplicationBlocked):
                    if operation == 'prepare': await f['commands'].prepare(req, f['entry'](req))
                    elif operation == 'quote': await f['quote'](req)
                    else:
                        await f['commands'].publish_policy_context(f['ctx'], expected_version=version)
            assert f['runtime'].owner.version == version
            assert f['runtime'].owner.state == before
            assert not f['broker']._session.posts
        finally: await f['store'].close()
    asyncio.run(scenario())


def test_nested_scope_and_result_registration_do_not_release_parent_early(tmp_path, monkeypatch):
    async def scenario():
        f = await fixture(tmp_path, monkeypatch)
        runtime = f['runtime']
        made = []
        def operation():
            made.append(True)
            async def result(): return True
            return result()
        try:
            with pytest.raises(ApplicationBlocked, match='command_scope_required'):
                runtime.start_command_result(object(), operation)
            with runtime.command_scope() as outer:
                with runtime.command_scope() as inner:
                    assert runtime.health()['command_operations_pending'] == 2
                assert runtime.health()['command_operations_pending'] == 1
                with pytest.raises(ApplicationBlocked, match='command_scope_required'):
                    runtime.start_command_result(inner, operation)
                async def foreign_task():
                    with pytest.raises(ApplicationBlocked, match='command_scope_required'):
                        runtime.start_command_result(outer, operation)
                await asyncio.create_task(foreign_task())
                assert not made
                with pytest.raises(ApplicationBlocked, match='command_shutdown_self_wait'):
                    await runtime.shutdown()
                assert await runtime.start_command_result(outer, operation) is True
            assert made == [True]
            await runtime.shutdown()
            assert runtime.health()['command_operations_pending'] == 0
        finally: await f['store'].close()
    asyncio.run(scenario())


@pytest.mark.parametrize('operation', ['prepare', 'quote', 'policy', 'dispatch'])
@pytest.mark.parametrize('boundary', ['commit', 'publication'])
def test_failed_command_write_or_publication_cannot_finish_shutdown_successfully(tmp_path, monkeypatch, operation, boundary):
    async def scenario():
        f = await fixture(tmp_path, monkeypatch)
        runtime, commands = f['runtime'], f['commands']
        reached, release = asyncio.Event(), asyncio.Event()
        tasks = []
        try:
            req = f['request']()
            await f['quote'](req)
            if operation == 'dispatch': await commands.prepare(req, f['entry'](req))
            original = f['store'].commit
            async def hold(*args, **kwargs):
                reached.set()
                await release.wait()
                if boundary == 'commit': raise StoreError('synthetic-command-commit-failure')
                return await original(*args, **kwargs)
            monkeypatch.setattr(f['store'], 'commit', hold)
            if boundary == 'publication':
                def broken(*args): raise ValueError('synthetic-command-publication-failure')
                monkeypatch.setattr(runtime.owner, 'publisher', broken)
            if operation == 'prepare': call = commands.prepare(req, f['entry'](req))
            elif operation == 'dispatch':
                call = commands.dispatch(req, f['entry'](req),
                    GuardedKISTransport(f['broker'], request_builder=f['builder']))
            elif operation == 'quote':
                call = commands.observe_entry_quote(req.symbol, Decimal('10001'), as_of=f['clock'][0],
                    source='synthetic', event_id='next', expected_version=runtime.owner.version)
            else:
                context = PolicyContext.from_dict(runtime.owner.state['entry_policy_context'])
                context = replace(context, versions=replace(context.versions, execution=runtime.owner.version))
                call = commands.publish_policy_context(context, expected_version=runtime.owner.version)
            running = asyncio.create_task(call)
            tasks.append(running)
            await asyncio.wait_for(reached.wait(), 2)
            closing = asyncio.create_task(runtime.shutdown())
            tasks.append(closing)
            await asyncio.sleep(0)
            assert not closing.done()
            release.set()
            result = await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 2)
            assert not runtime.owner.healthy and not f['broker']._session.posts
            assert isinstance(result[1], ApplicationBlocked)
            assert str(result[1]) == 'command_state_drain_failed'
            if operation == 'dispatch': assert result[0].status is CommandStatus.NOT_SENT
        finally:
            release.set()
            if tasks: await asyncio.gather(*tasks, return_exceptions=True)
            await f['store'].close()
    asyncio.run(scenario())


@pytest.mark.parametrize('operation', ['prepare', 'dispatch'])
def test_rejected_command_during_other_successful_commit_is_not_sticky_storage_failure(tmp_path, monkeypatch, operation):
    async def scenario():
        f = await fixture(tmp_path, monkeypatch)
        runtime, commands = f['runtime'], f['commands']
        reached, release = asyncio.Event(), asyncio.Event()
        tasks = []
        try:
            req = f['request']()
            await f['quote'](req)
            original = f['store'].commit
            async def hold(*args, **kwargs):
                reached.set()
                await release.wait()
                return await original(*args, **kwargs)
            monkeypatch.setattr(f['store'], 'commit', hold)
            running = asyncio.create_task(commands.prepare(req, f['entry'](req)))
            tasks.append(running)
            await asyncio.wait_for(reached.wait(), 2)
            assert not runtime.owner.healthy  # 정상 commit 진행 중의 일시 장벽
            with pytest.raises(ValueError, match='untrusted_entry_context'):
                if operation == 'prepare': await commands.prepare(req, object())
                else:
                    await commands.dispatch(req, object(),
                        GuardedKISTransport(f['broker'], request_builder=f['builder']))
            closing = asyncio.create_task(runtime.shutdown())
            tasks.append(closing)
            await asyncio.sleep(0)
            assert not closing.done()
            release.set()
            await asyncio.wait_for(asyncio.gather(*tasks), 2)
            assert runtime.owner.healthy
            assert not runtime.health()['command_results_failed']
            assert not f['broker']._session.posts
        finally:
            release.set()
            if tasks: await asyncio.gather(*tasks, return_exceptions=True)
            await f['store'].close()
    asyncio.run(scenario())
