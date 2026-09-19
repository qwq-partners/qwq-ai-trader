"""Opus 지적 재현: 요청 거부와 실제 저장 실패의 수명 경계를 구분한다."""
import asyncio
import sqlite3

import pytest

from src.execution.safety.application import ApplicationBlocked
from src.execution.safety.store import StoreError
from test_execution_risk_input_seal import fixture


async def cleanup(store, runtime):
    # RED 단계의 원래 drain 실패가 본문의 실제 assertion을 가리지 않게 정리한다.
    try:
        await runtime.shutdown()
    except ApplicationBlocked:
        pass
    await store.close()


@pytest.mark.parametrize('baseline', ['unregistered', 'other_registered', 'legacy_restored'])
def test_unknown_generation_request_rejects_without_poisoning_runtime(tmp_path, baseline):
    async def scenario():
        _, _, store, runtime, source = await fixture(tmp_path)
        try:
            if baseline == 'other_registered':
                await runtime.owner.register_policy_generations('other', ('protection.current_regime',))
            if baseline == 'legacy_restored':
                old = await source.begin('old', 'llm_regime', require_seal=True)
                await source.seal(old, policy_reads=('protection.config',), inputs={})
                await source.complete(old, 'success', {})
                await runtime.restore()
            ticket = await source.begin('new', 'llm_regime', require_seal=True)
            before = await store.load()
            with pytest.raises(ValueError, match='policy_generation_unregistered'):
                await source.seal(ticket, versioned_policy_reads=('protection.config',), inputs={})
            assert await store.load() == before
            assert runtime.owner.healthy
            assert not runtime.health()['command_results_failed']
            assert runtime.health()['command_results_pending'] == 0
            await runtime.owner.register_policy_generations('correct', ('protection.config',))
            sealed = await source.seal(ticket, versioned_policy_reads=('protection.config',), inputs={})
            assert sealed.status == 'sealed'
            assert (await source.complete(ticket, 'success', {})).status == 'accepted'
            await runtime.restore()
            assert runtime.owner.healthy and not runtime.trading_ready
            await runtime.shutdown()
        finally:
            await cleanup(store, runtime)
    asyncio.run(scenario())


def test_unknown_generation_with_existing_scope_can_shutdown_after_restore(tmp_path):
    async def scenario():
        _, _, store, runtime, source = await fixture(tmp_path)
        try:
            ticket = await source.begin('new', 'llm_regime', require_seal=True)
            before = await store.load()
            with runtime.command_scope() as token:
                with pytest.raises(ValueError, match='policy_generation_unregistered'):
                    await source.seal(ticket, versioned_policy_reads=('protection.config',),
                                      inputs={}, scope_token=token)
            assert await store.load() == before
            await runtime.restore()
            assert runtime.owner.healthy
            await runtime.shutdown()
        finally:
            await cleanup(store, runtime)
    asyncio.run(scenario())


def test_reseal_with_unregistered_selection_still_records_conflict(tmp_path):
    async def scenario():
        _, _, store, runtime, source = await fixture(tmp_path)
        try:
            await runtime.owner.register_policy_generations('other', ('protection.current_regime',))
            ticket = await source.begin('new', 'llm_regime', require_seal=True)
            original = await source.seal(ticket, versioned_policy_reads=('protection.current_regime',), inputs={})
            result = await source.seal(ticket, versioned_policy_reads=('protection.config',), inputs={})
            assert (result.status, result.reason) == ('conflict', 'reseal_conflict')
            assert result.seal_digest == original.seal_digest
            assert runtime.owner.healthy
            assert (await source.complete(ticket, 'success', {})).status == 'stale'
            await runtime.shutdown()
        finally:
            await cleanup(store, runtime)
    asyncio.run(scenario())


def test_superseded_unsealed_request_remains_stale_without_registration(tmp_path):
    async def scenario():
        _, _, store, runtime, source = await fixture(tmp_path)
        try:
            old = await source.begin('old', 'llm_regime', require_seal=True)
            await source.begin('new', 'llm_regime', require_seal=True)
            result = await source.seal(old, versioned_policy_reads=('protection.config',), inputs={})
            assert (result.status, result.reason) == ('stale', 'superseded')
            assert result.reads_json == '{}'
            assert runtime.owner.healthy
            await runtime.shutdown()
        finally:
            await cleanup(store, runtime)
    asyncio.run(scenario())


def test_real_seal_sql_failure_still_blocks_runtime_and_shutdown(tmp_path):
    async def scenario():
        _, _, store, runtime, source = await fixture(tmp_path)
        try:
            await runtime.owner.register_policy_generations('correct', ('protection.config',))
            ticket = await source.begin('new', 'llm_regime', require_seal=True)
            before = await store.load()
            with sqlite3.connect(store.path) as db:
                db.execute("CREATE TRIGGER fail_seal BEFORE UPDATE ON checkpoint BEGIN SELECT RAISE(ABORT, 'synthetic seal failure'); END")
            with pytest.raises(StoreError):
                await source.seal(ticket, versioned_policy_reads=('protection.config',), inputs={})
            assert await store.load() == before
            assert not runtime.owner.healthy
            assert runtime.health()['command_results_failed']
            with sqlite3.connect(store.path) as db:
                db.execute('DROP TRIGGER fail_seal')
            await runtime.restore()
            with pytest.raises(ApplicationBlocked, match='command_result_drain_failed'):
                await runtime.shutdown()
        finally:
            await cleanup(store, runtime)
    asyncio.run(scenario())
