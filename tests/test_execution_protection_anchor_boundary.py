"""Original fill-proof immutability across actual queue, SQL and delivery boundaries."""
import asyncio
from copy import deepcopy
from decimal import Decimal

import pytest

from src.core.engine import UnifiedEngine
from src.core.types import TradingConfig
from src.execution.safety.application import ApplicationBlocked
from src.execution.safety.journal_delivery import ExecutionEnvelope, OutboxDispatcher, PostgresExecutionJournal
from src.execution.safety.runtime import KRExecutionRuntime
from src.execution.safety.store import ExecutionStateStore
from src.strategies.exit_manager import ExitManager
from test_execution_intraday_replay import invariant_roots
from test_execution_journal_delivery import Pool
from test_execution_protection_recovery import failed_entry
from test_execution_runtime import NOW, setup, opened, observed, queued


async def cold_runtime(store, runtime):
    path = store.path
    await runtime.shutdown()
    await store.close()
    engine = UnifiedEngine(TradingConfig(initial_capital=Decimal('2000000')))
    exits = ExitManager(persist=False, clock=lambda: NOW)
    store = ExecutionStateStore(path)
    runtime = KRExecutionRuntime(store, engine, exits, clock=lambda: NOW)
    await runtime.restore()
    runtime.attach()
    return engine, exits, store, runtime


@pytest.mark.parametrize('bad', ['wrong', 'uppercase', 'space', 'short', 'bool', 'null', 'missing'])
def test_unproven_fill_link_is_not_rebuilt_by_duplicate_restore_or_repair(tmp_path, monkeypatch, bad):
    async def scenario():
        engine, exits, store, runtime, _, observation = await failed_entry(tmp_path, monkeypatch)
        try:
            def corrupt(state):
                row = state['outbox'][observation.observation_id]
                original = state['protection_replay']['005930']['events'][0]['digest']
                values = {'wrong': '0' * 64, 'uppercase': original.upper(), 'space': original + ' ',
                          'short': original[:-1], 'bool': True, 'null': None}
                if bad == 'missing':
                    row.pop('protection_replay_digest', None)
                else:
                    row['protection_replay_digest'] = values[bad]
                return state
            await runtime.owner.mutate('synthetic-old-or-damaged-proof', corrupt)
            before = runtime.owner.state
            assert (await queued(engine, observation)).status == 'ALREADY_APPLIED'
            assert runtime.owner.state['outbox'] == before['outbox']
            engine, exits, store, runtime = await cold_runtime(store, runtime)
            assert runtime.owner.state['outbox'] == before['outbox']
            assert (await queued(engine, observation)).status == 'ALREADY_APPLIED'
            result = await runtime.repair_protection('unproven', '005930', expected_version=runtime.owner.version)
            assert result.status == 'BLOCKED', bad
            assert runtime.owner.state['protection'] == before['protection']
            invariant_roots(before, runtime.owner.state)
            assert (await store.load())[1]['outbox'] == before['outbox']
            assert not runtime.trading_ready
        finally:
            await runtime.shutdown()
            await store.close()
    asyncio.run(scenario())


def test_original_link_and_journal_payload_survive_ack_duplicate_repair_and_cold_restore(tmp_path, monkeypatch):
    async def scenario():
        engine, exits, store, runtime, _, observation = await failed_entry(tmp_path, monkeypatch)
        try:
            original = runtime.owner.state
            row = original['outbox'][observation.observation_id]
            event = original['protection_replay']['005930']['events'][0]
            assert row.get('protection_replay_digest') == event['digest']
            envelope = ExecutionEnvelope.from_outbox(observation.observation_id, row)
            pool = Pool()
            dispatcher = OutboxDispatcher(runtime.owner, PostgresExecutionJournal(pool))
            assert (await dispatcher.drain()).delivered == 1
            delivered = runtime.owner.state
            assert ExecutionEnvelope.from_outbox(observation.observation_id,
                delivered['outbox'][observation.observation_id]) == envelope
            assert (await queued(engine, observation)).status == 'ALREADY_APPLIED'
            assert runtime.owner.state['outbox'] == delivered['outbox']
            version = runtime.owner.version
            result = await runtime.repair_protection('repair-once', '005930', expected_version=version)
            assert result.status == 'APPLIED', result.reason
            invariant_roots(delivered, runtime.owner.state)
            engine, exits, store, runtime = await cold_runtime(store, runtime)
            assert (await queued(engine, observation)).status == 'ALREADY_APPLIED'
            assert (await runtime.repair_protection('repair-once', '005930', expected_version=version)).status == 'ALREADY_APPLIED'
            assert runtime.owner.state['outbox'] == delivered['outbox']
            assert runtime.owner.state['protection_replay'] == original['protection_replay']
            assert (await OutboxDispatcher(runtime.owner, PostgresExecutionJournal(pool)).drain()).delivered == 0
            assert pool.insertions == 1
            assert not runtime.trading_ready
        finally:
            await runtime.shutdown()
            await store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('committed', [False, True])
def test_original_link_is_atomic_with_fill_sql_commit_and_lost_ack(tmp_path, monkeypatch, committed):
    async def scenario():
        engine, exits, store, runtime = await setup(tmp_path)
        try:
            ref = await opened(runtime, 'B1')
            observation = await observed(runtime, ref, 40, '400000')
            original_commit = store.commit
            captured = []
            async def fail_commit(expected, state, command):
                if command.startswith('fill:'):
                    captured.append(deepcopy(state))
                    if committed:
                        await original_commit(expected, state, command)
                    raise OSError('synthetic fill SQL failure')
                return await original_commit(expected, state, command)
            def fail_registration(*args, **kwargs):
                raise ValueError('synthetic protection registration failure')
            with monkeypatch.context() as patch:
                patch.setattr(store, 'commit', fail_commit)
                patch.setattr(ExitManager, 'register_position', fail_registration)
                # Core queue intentionally wraps the SQL failure in its fail-closed receipt.
                with pytest.raises(ApplicationBlocked, match='체결 적용 실패'):
                    await queued(engine, observation)
            assert not runtime.owner.healthy
            assert len(captured) == 1
            proposed = captured[0]
            event = proposed['protection_replay']['005930']['events'][0]
            assert proposed['outbox'][observation.observation_id].get('protection_replay_digest') == event['digest']
            persisted = (await store.load())[1]
            if committed:
                assert persisted == proposed
            else:
                assert observation.observation_id not in persisted['outbox']
                assert 'protection_replay' not in persisted
                assert persisted['portfolio']['positions'] == {}
            engine, exits, store, runtime = await cold_runtime(store, runtime)
            assert runtime.owner.state == persisted
            if committed:
                receipt = await queued(engine, observation)
                assert receipt.status == 'ALREADY_APPLIED'
                assert runtime.owner.state['outbox'] == proposed['outbox']
                assert runtime.owner.state['protection_replay'] == proposed['protection_replay']
            else:
                # This is a new first successful commit, not a duplicate or migration.
                with monkeypatch.context() as patch:
                    patch.setattr(ExitManager, 'register_position', fail_registration)
                    assert (await queued(engine, observation)).status == 'APPLIED'
            before = runtime.owner.state
            event = before['protection_replay']['005930']['events'][0]
            assert before['outbox'][observation.observation_id]['protection_replay_digest'] == event['digest']
            result = await runtime.repair_protection('after-sql', '005930', expected_version=runtime.owner.version)
            assert result.status == 'APPLIED', result.reason
            invariant_roots(before, runtime.owner.state)
            assert len(runtime.owner.state['outbox']) == 1
            assert len(runtime.owner.state['protection_replay']['005930']['events']) == 1
            assert engine.portfolio.positions['005930'].quantity == 40
            assert not runtime.trading_ready
        finally:
            await runtime.shutdown()
            await store.close()
    asyncio.run(scenario())
