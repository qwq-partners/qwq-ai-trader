"""실제 정책 원장 대비 재생 완전성과 모든 event의 글로벌 정책 보존."""
import asyncio
from copy import deepcopy
from decimal import Decimal
from datetime import timedelta

import pytest

from src.execution.safety import protection_recovery as recovery
from src.execution.safety.protection import decode_protection, encode_protection
from src.strategies.exit_manager import ExitManager
from test_execution_intraday_replay import degraded, refresh, reseal, invariant_roots
from test_execution_intraday_owner import owned
from test_execution_protection_recovery import failed_entry
from test_execution_runtime import NOW, opened, observed, queued
from test_kis_index_observation import provider, raw


@pytest.mark.parametrize('damage', ['fill_before', 'fill_after', 'quote_after'])
def test_each_legacy_event_must_preserve_actual_global_policy(tmp_path, monkeypatch, damage):
    async def scenario():
        _, _, store, runtime, _, _ = await failed_entry(tmp_path, monkeypatch)
        try:
            if damage == 'quote_after':
                await runtime.quote('005930', Decimal('10100'))
            def corrupt(state):
                events = state['protection_replay']['005930']['events']
                if damage == 'fill_before':
                    events[0]['before']['protection']['config']['first_exit_pct'] = 12.0
                elif damage == 'fill_after':
                    events[0]['after']['protection']['config']['first_exit_pct'] = 12.0
                    state['protection']['config']['first_exit_pct'] = 12.0
                else:
                    event = events[-1]
                    event.pop('scope_digest')
                    event['before'] = deepcopy(events[0]['after'])
                    event['after'] = deepcopy(event['before'])
                    event['after']['protection']['config']['first_exit_pct'] = 12.0
                    state['protection']['config']['first_exit_pct'] = 12.0
                reseal(state)
                return state
            await runtime.owner.mutate('synthetic-event-global-forgery', corrupt)
            before = runtime.owner.state
            result = await runtime.repair_protection('event-global', '005930', expected_version=runtime.owner.version)
            assert result.status == 'BLOCKED', damage
            assert runtime.owner.state['protection'] == before['protection']
            invariant_roots(before, runtime.owner.state)
        finally:
            await runtime.shutdown()
            await store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('forge_anchor_version', [False, True])
def test_missing_cycle_is_rejected_even_without_quote_and_equal_final_globals(tmp_path, monkeypatch, forge_anchor_version):
    async def scenario():
        engine, _, store, runtime, _, writer = await owned(tmp_path)
        try:
            def normalize(state):
                manager = decode_protection(state['protection'], clock=lambda: NOW)
                manager.apply_regime_params(manager._current_regime, force=True)
                state['protection'] = encode_protection(manager)
                return state
            await runtime.owner.mutate('synthetic-neutral-baseline', normalize)
            ref = await opened(runtime, 'B1')
            observation = await observed(runtime, ref, 40, '400000')
            def fail(*args, **kwargs): raise ValueError('synthetic registration failure')
            with monkeypatch.context() as patch:
                patch.setattr(ExitManager, 'register_position', fail)
                await queued(engine, observation)
            await refresh(writer, monkeypatch, -3)
            await refresh(writer, monkeypatch, 0)
            def erase(state):
                events = state['protection_replay']['005930']['events']
                assert events[0]['after'] == events[-1]['after']
                if forge_anchor_version:
                    events[0]['source_version'] = events[-1]['source_version']
                events[:] = [events[0]]
                reseal(state)
                return state
            await runtime.owner.mutate('synthetic-erased-cycle', erase)
            before = runtime.owner.state
            result = await runtime.repair_protection('cycle', '005930', expected_version=runtime.owner.version)
            assert result.status == 'BLOCKED'
            assert runtime.owner.state['protection'] == before['protection']
            invariant_roots(before, runtime.owner.state)
        finally:
            await runtime.shutdown()
            await store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('root', ['risk_sources', 'intraday_policy'])
def test_independent_roots_are_validated_when_no_policy_event_remains(tmp_path, monkeypatch, root):
    async def scenario():
        _, _, store, runtime, writer, _ = await degraded(tmp_path, monkeypatch)
        try:
            await refresh(writer, monkeypatch, 0)  # normal→normal: no policy event is correct.
            state = runtime.owner.state
            assert [event['kind'] for event in state['protection_replay']['005930']['events']] == ['fill']
            state[root]['schema'] = 999
            before = deepcopy(state)
            result = recovery.reduce_repair(state, 'root-corrupt', '005930',
                expected_version=runtime.owner.version, state_version=runtime.owner.version)
            assert result['recovery_receipts']['root-corrupt']['status'] == 'BLOCKED'
            assert result['protection'] == before['protection']
            invariant_roots(before, result)
        finally:
            await runtime.shutdown()
            await store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('kind', ['normal_noop', 'same_level', 'duplicate', 'missing', 'older'])
def test_nonchanging_source_attempts_do_not_require_policy_replay_events(tmp_path, monkeypatch, kind):
    async def scenario():
        _, exits, store, runtime, writer, _ = await degraded(tmp_path, monkeypatch)
        try:
            if kind == 'normal_noop':
                await refresh(writer, monkeypatch, 0)
            elif kind == 'missing':
                await writer.refresh(None)
            elif kind == 'same_level':
                await refresh(writer, monkeypatch, -3)
                await refresh(writer, monkeypatch, -3.2)
            elif kind == 'older':
                await refresh(writer, monkeypatch, -3)
                await refresh(writer, monkeypatch, 0, at=NOW - timedelta(seconds=1))
            else:
                md, _ = await provider(monkeypatch, {**raw(), 'bstp_nmix_prdy_ctrt': '-3'})
                await writer.refresh(md)
                await writer.refresh(md)
            before = runtime.owner.state
            result = await runtime.repair_protection('no-extra-policy', '005930', expected_version=runtime.owner.version)
            assert result.status == 'APPLIED', result.reason
            assert exits.get_state('005930').remaining_quantity == 40
            invariant_roots(before, runtime.owner.state)
        finally:
            await runtime.shutdown()
            await store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('prior_cycle', [False, True])
def test_policy_changes_before_latest_healthy_anchor_do_not_require_old_events(tmp_path, monkeypatch, prior_cycle):
    async def scenario():
        engine, exits, store, runtime, _, writer = await owned(tmp_path)
        try:
            ref = await opened(runtime, 'B1')
            await queued(engine, await observed(runtime, ref, 40, '400000'))
            await refresh(writer, monkeypatch, -3)
            if prior_cycle:
                await refresh(writer, monkeypatch, 0)
            assert not runtime.owner.state.get('protection_replay')
            next_fill = await observed(runtime, ref, 100, '1000000')
            def fail(*args, **kwargs): raise ValueError('synthetic later fill failure')
            with monkeypatch.context() as patch:
                patch.setattr('src.execution.safety.protection._registration', fail)
                await queued(engine, next_fill)
            await refresh(writer, monkeypatch, -4)
            before = runtime.owner.state
            result = await runtime.repair_protection('latest-anchor', '005930', expected_version=runtime.owner.version)
            assert result.status == 'APPLIED', result.reason
            assert exits.get_state('005930').stop_loss_pct == 2.0
            assert exits.get_state('005930').remaining_quantity == 100
            invariant_roots(before, runtime.owner.state)
        finally:
            await runtime.shutdown()
            await store.close()
    asyncio.run(scenario())


def test_closed_healthy_lifecycle_cycles_do_not_belong_to_new_entry_repair(tmp_path, monkeypatch):
    async def scenario():
        engine, exits, store, runtime, _, writer = await owned(tmp_path)
        try:
            buy = await opened(runtime, 'OLD', quantity=40)
            await queued(engine, await observed(runtime, buy, 40, '400000', total=40))
            await refresh(writer, monkeypatch, -3)
            await refresh(writer, monkeypatch, 0)
            sale = await opened(runtime, 'SELL', 'sell', quantity=40)
            await queued(engine, await observed(runtime, sale, 40, '400000', side='sell', total=40))
            new_buy = await opened(runtime, 'NEW', quantity=10)
            new_fill = await observed(runtime, new_buy, 10, '100000', total=10)
            def fail(*args, **kwargs): raise ValueError('synthetic new lifecycle failure')
            with monkeypatch.context() as patch:
                patch.setattr(ExitManager, 'register_position', fail)
                assert (await queued(engine, new_fill)).protection_status == 'degraded'
            before = runtime.owner.state
            result = await runtime.repair_protection('new-lifecycle', '005930', expected_version=runtime.owner.version)
            assert result.status == 'APPLIED', result.reason
            assert exits.get_state('005930').remaining_quantity == 10
            invariant_roots(before, runtime.owner.state)
        finally:
            await runtime.shutdown()
            await store.close()
    asyncio.run(scenario())


def test_fresh_legacy_history_without_source_registry_remains_supported(tmp_path, monkeypatch):
    async def scenario():
        _, _, store, runtime, _, _ = await failed_entry(tmp_path, monkeypatch)
        try:
            assert 'risk_sources' not in runtime.owner.state and 'intraday_policy' not in runtime.owner.state
            result = await runtime.repair_protection('legacy', '005930', expected_version=runtime.owner.version)
            assert result.status == 'APPLIED', result.reason
        finally:
            await runtime.shutdown()
            await store.close()
    asyncio.run(scenario())


def test_retyped_policy_quote_cannot_become_new_healthy_anchor(tmp_path, monkeypatch):
    async def scenario():
        from src.execution.safety.economics import _decode_position
        _, _, store, runtime, writer, _ = await degraded(tmp_path, monkeypatch)
        try:
            await refresh(writer, monkeypatch, -3)
            await runtime.quote('005930', Decimal('9700'))
            await refresh(writer, monkeypatch, 0)
            def forge(state):
                event = state['protection_replay']['005930']['events'][-1]
                # 독립 source/current protection은 그대로 두고 history만 조작한다.
                fake_before = deepcopy(event['after'])
                manager = decode_protection(fake_before['protection'], clock=lambda: NOW)
                manager._execution_protection['degraded'].pop('005930')
                manager.register_position(_decode_position(fake_before['position']))
                fake_before['protection'] = encode_protection(manager)
                event['before'] = fake_before
                event.pop('policy_input')
                event.update(kind='quote', price='10000', market_data=None, intent_id=None,
                             command_id='synthetic-fake-anchor', decision=None, provenance=None)
                reseal(state)
                return state
            await runtime.owner.mutate('synthetic-healthy-quote-anchor', forge)
            before = runtime.owner.state
            result = await runtime.repair_protection('fake-anchor', '005930', expected_version=runtime.owner.version)
            assert result.status == 'BLOCKED'
            assert runtime.owner.state['protection'] == before['protection']
            invariant_roots(before, runtime.owner.state)
        finally:
            await runtime.shutdown()
            await store.close()
    asyncio.run(scenario())


def test_actual_delivery_ack_preserves_fill_anchor_and_repair_outbox(tmp_path, monkeypatch):
    from src.execution.safety.journal_delivery import (
        ExecutionEnvelope, OutboxDispatcher, PostgresExecutionJournal,
    )
    from test_execution_journal_delivery import Pool
    async def scenario():
        _, _, store, runtime, writer, ref = await degraded(tmp_path, monkeypatch)
        try:
            key, row = next(iter(runtime.owner.state['outbox'].items()))
            original = ExecutionEnvelope.from_outbox(key, row)
            pool = Pool()  # DB sink boundary only; actual dispatcher/ACK/SQLite remain in use.
            dispatcher = OutboxDispatcher(runtime.owner, PostgresExecutionJournal(pool))
            assert (await dispatcher.drain()).delivered == 1
            await refresh(writer, monkeypatch, -3)
            before = runtime.owner.state
            assert before['outbox'][key]['status'] == 'delivered'
            assert before['outbox'][key]['delivery']['payload_digest'] == original.payload_digest
            assert ExecutionEnvelope.from_outbox(key, before['outbox'][key]) == original
            assert not before['cursors'][ref.key]['journal_pending']
            result = await runtime.repair_protection('after-ack', '005930', expected_version=runtime.owner.version)
            assert result.status == 'APPLIED', result.reason
            invariant_roots(before, runtime.owner.state)
            assert runtime.owner.state['outbox'] == before['outbox']
            assert not runtime.owner.state['cursors'][ref.key]['journal_pending']
            assert (await dispatcher.drain()).delivered == 0
            assert pool.insertions == 1
        finally:
            await runtime.shutdown()
            await store.close()
    asyncio.run(scenario())
