"""Independent actual SQLite cancellation/snapshot controls after B2/B4 repair."""
import asyncio
import sqlite3
import threading

import pytest

from test_execution_risk_input_seal import fixture
from test_execution_policy_generations import cold_restore
from src.execution.safety.application import ApplicationBlocked
from src.execution.safety.store import StoreError, encode_state

SELECTOR = 'protection.config'


@pytest.mark.parametrize('position', ['before_sql', 'after_sql'])
@pytest.mark.parametrize('cancel_shutdown', [False, True])
def test_registration_repeated_cancellation_drains_sql_then_requires_restore(tmp_path, monkeypatch, position, cancel_shutdown):
    async def scenario():
        _, _, store, runtime, _ = await fixture(tmp_path)
        entered, release = asyncio.Event(), threading.Event()
        loop = asyncio.get_running_loop()
        original = store._commit
        def gated(expected, payload, command):
            if position == 'after_sql':
                result = original(expected, payload, command)
            loop.call_soon_threadsafe(entered.set)
            if not release.wait(5):
                raise RuntimeError('probe gate timeout')
            return result if position == 'after_sql' else original(expected, payload, command)
        monkeypatch.setattr(store, '_commit', gated)
        task = asyncio.create_task(runtime.owner.register_policy_generations('cancelled-register', (SELECTOR,)))
        stopping = None
        try:
            await asyncio.wait_for(entered.wait(), 5)
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
            stopping = asyncio.create_task(runtime.shutdown())
            await asyncio.sleep(0)
            assert not task.done() and not stopping.done()
            assert runtime.health()['command_operations_pending'] == 1
            if cancel_shutdown:
                stopping.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await stopping
                stopping = asyncio.create_task(runtime.shutdown())
                await asyncio.sleep(0)
                assert not stopping.done() and not task.done()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            with pytest.raises(ApplicationBlocked, match='command_state_drain_failed'):
                await stopping
            assert not runtime.owner.healthy
            assert not runtime.health()['command_results_failed']
            assert runtime.health()['command_operations_pending'] == 0
            assert await store.lookup_commit('policy-registration:cancelled-register') == 2
            await runtime.restore()
            await runtime.shutdown()
            store, runtime = await cold_restore(store, runtime)
            assert runtime.owner.healthy
            assert await runtime.owner.register_policy_generations('cancelled-register', (SELECTOR,)) == 2
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)
            if stopping:
                await asyncio.gather(stopping, return_exceptions=True)
            try:
                await runtime.shutdown()
            except ApplicationBlocked:
                pass
            await store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('failure', ['sqlite', 'value', 'store'])
def test_receipt_read_error_rolls_back_and_same_connection_recovers(tmp_path, monkeypatch, failure):
    async def scenario():
        _, _, store, runtime, _ = await fixture(tmp_path)
        await runtime.owner.register_policy_generations('register', (SELECTOR,))
        original = store._load
        def broken():
            original()
            assert store._connection.in_transaction
            if failure == 'sqlite':
                store._connection.execute('SELECT * FROM synthetic_missing_table')
            elif failure == 'value':
                raise ValueError('synthetic decoding failure')
            else:
                raise StoreError('synthetic store failure')
        try:
            with monkeypatch.context() as patch:
                patch.setattr(store, '_load', broken)
                with pytest.raises(StoreError):
                    await runtime.restore()
            assert not runtime.owner.healthy
            assert not await store._run(lambda: store._connection.in_transaction)
            assert await runtime.restore() == 2
            assert runtime.owner.healthy
            assert await runtime.owner.register_policy_generations('next', ('protection.current_regime',)) == 3
        finally:
            await runtime.shutdown()
            await store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('action', ['concurrent_writer', 'cancel_restore'])
def test_receipt_read_is_one_snapshot_and_cancellation_closes_transaction(tmp_path, monkeypatch, action):
    async def scenario():
        _, _, store, runtime, _ = await fixture(tmp_path)
        await runtime.owner.register_policy_generations('register', (SELECTOR,))
        original = store._load
        entered, release = asyncio.Event(), threading.Event()
        loop = asyncio.get_running_loop()
        def gated():
            result = original()
            assert store._connection.in_transaction
            loop.call_soon_threadsafe(entered.set)
            if not release.wait(5):
                raise RuntimeError('probe gate timeout')
            return result
        task = None
        try:
            with monkeypatch.context() as patch:
                patch.setattr(store, '_load', gated)
                task = asyncio.create_task(runtime.restore())
                await asyncio.wait_for(entered.wait(), 5)
                if action == 'concurrent_writer':
                    # Separate real SQLite writer updates both sides coherently while the
                    # store read is paused between checkpoint SELECT and receipt SELECT.
                    state = runtime.owner.state
                    del state['policy_generations']
                    with sqlite3.connect(store.path) as db:
                        db.execute('UPDATE checkpoint SET version=3, state=? WHERE id=1', (encode_state(state),))
                        db.execute("DELETE FROM commits WHERE commit_id='policy-registration:register'")
                    release.set()
                    assert await task == 2
                    assert runtime.owner.state['policy_generations']['registrations']['register']['version'] == 2
                else:
                    task.cancel()
                    await asyncio.sleep(0)
                    task.cancel()
                    await asyncio.sleep(0)
                    assert not task.done()
                    release.set()
                    with pytest.raises(asyncio.CancelledError):
                        await task
                    assert not runtime.owner.healthy
            assert not await store._run(lambda: store._connection.in_transaction)
            assert await runtime.restore() == (3 if action == 'concurrent_writer' else 2)
            assert runtime.owner.healthy
            if action == 'concurrent_writer':
                assert 'policy_generations' not in runtime.owner.state
        finally:
            release.set()
            if task:
                await asyncio.gather(task, return_exceptions=True)
            await runtime.shutdown()
            await store.close()
    asyncio.run(scenario())
