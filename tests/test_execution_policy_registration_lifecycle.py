"""등록도 runtime 일자/종료 수명에 속한다. 실제 SQLite와 owner lock 사용."""
import asyncio

import pytest

from src.execution.safety.application import ApplicationBlocked
from test_execution_day_recovery import prepare, valued
from test_execution_risk_input_seal import fixture
from test_execution_runtime import NOW

SELECTOR = 'protection.config'


def test_prepared_registration_rejects_and_original_rollover_id_remains_usable(tmp_path):
    async def scenario():
        times = [NOW]
        _, _, store, runtime, _ = await fixture(tmp_path, clock=lambda: times[0])
        try:
            fence = await prepare(runtime, times)
            assert fence.status == 'PREPARED'
            evidence_id = await valued(runtime, fence, times)
            before = await store.load()
            with pytest.raises(ApplicationBlocked, match='admission_closed'):
                await runtime.owner.register_policy_generations('closed-day', (SELECTOR,))
            assert await store.load() == before
            assert runtime.owner.healthy and not runtime.health()['command_results_failed']
            receipt = await runtime.rollover_day('same-roll', expected_version=before[0],
                fence_id=fence.fence_id, valuation_evidence_id=evidence_id)
            assert receipt.status == 'APPLIED'
        finally:
            await runtime.shutdown()
            await store.close()
    asyncio.run(scenario())


def test_shutdown_drains_registration_already_waiting_for_owner_lock(tmp_path):
    async def scenario():
        _, _, store, runtime, _ = await fixture(tmp_path)
        await runtime.owner._lock.acquire()
        registration = asyncio.create_task(runtime.owner.register_policy_generations('in-flight', (SELECTOR,)))
        stopping = None
        try:
            await asyncio.sleep(0)
            stopping = asyncio.create_task(runtime.shutdown())
            await asyncio.sleep(0)
            returned_early = stopping.done() and not registration.done()
            runtime.owner._lock.release()
            committed = await registration
            await stopping
            assert committed == 2 and runtime._closing
            assert not returned_early, 'shutdown returned before admitted registration committed'
            assert runtime.owner.healthy and runtime.owner.published_version == committed
        finally:
            if runtime.owner._lock.locked():
                runtime.owner._lock.release()
            await asyncio.gather(registration, return_exceptions=True)
            if stopping is not None:
                await asyncio.gather(stopping, return_exceptions=True)
            await runtime.shutdown()
            await store.close()
    asyncio.run(scenario())


def test_registration_after_shutdown_rejects_without_commit(tmp_path):
    async def scenario():
        _, _, store, runtime, _ = await fixture(tmp_path)
        try:
            before = await store.load()
            await runtime.shutdown()
            with pytest.raises(ApplicationBlocked, match='admission_closed'):
                await runtime.owner.register_policy_generations('after-close', (SELECTOR,))
            assert await store.load() == before
            assert runtime.owner.healthy
        finally:
            await store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('pause', ['lock', 'lookup'])
def test_registration_rechecks_day_after_admission_and_lookup(tmp_path, monkeypatch, pause):
    async def scenario():
        times = [NOW]
        _, _, store, runtime, _ = await fixture(tmp_path, clock=lambda: times[0])
        entered, release = asyncio.Event(), asyncio.Event()
        original = store.lookup_commit

        async def gated_lookup(command):
            result = await original(command)
            if command == 'policy-registration:racing':
                entered.set()
                await release.wait()
            return result

        if pause == 'lock':
            await runtime.owner._lock.acquire()
        else:
            monkeypatch.setattr(store, 'lookup_commit', gated_lookup)
        task = asyncio.create_task(runtime.owner.register_policy_generations('racing', (SELECTOR,)))
        fence_task = None
        try:
            if pause == 'lookup':
                await asyncio.wait_for(entered.wait(), 5)
            else:
                await asyncio.sleep(0)
            fence_task = asyncio.create_task(prepare(runtime, times))
            await asyncio.sleep(0)
            assert runtime.day_admission_closed
            release.set()
            if pause == 'lock':
                runtime.owner._lock.release()
            with pytest.raises(ApplicationBlocked, match='admission_closed'):
                await task
            assert (await fence_task).status == 'PREPARED'
            assert await store.lookup_commit('policy-registration:racing') is None
            assert 'policy_generations' not in runtime.owner.state
            assert runtime.owner.healthy and not runtime.health()['command_results_failed']
            assert runtime.health()['command_operations_pending'] == 0
        finally:
            release.set()
            if pause == 'lock' and runtime.owner._lock.locked():
                runtime.owner._lock.release()
            await asyncio.gather(task, return_exceptions=True)
            if fence_task is not None:
                await asyncio.gather(fence_task, return_exceptions=True)
            await runtime.shutdown()
            await store.close()
    asyncio.run(scenario())
