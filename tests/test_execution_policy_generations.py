"""정책 변경 이력: 실제 owner/SQLite 경계, 외부 I/O 없음."""
import asyncio
from copy import deepcopy
import json

import pytest

from test_execution_risk_input_seal import fixture
from test_execution_runtime import NOW


def test_registration_same_id_requires_exact_original_selection(tmp_path):
    async def scenario():
        _, _, store, runtime, _ = await fixture(tmp_path)
        try:
            await runtime.owner.register_policy_generations('register', ('protection.config',))
            before = await store.load()
            with pytest.raises(ValueError, match='registration.*conflict'):
                await runtime.owner.register_policy_generations('register', ('protection.current_regime',))
            assert await store.load() == before
            store, runtime = await cold_restore(store, runtime)
            with pytest.raises(ValueError, match='registration.*conflict'):
                await runtime.owner.register_policy_generations('register', ('protection.current_regime',))
        finally:
            await runtime.shutdown()
            await store.close()
    asyncio.run(scenario())


async def cold_restore(store, runtime, *, unclean=False):
    from decimal import Decimal
    from src.core.engine import UnifiedEngine
    from src.core.types import TradingConfig
    from src.strategies.exit_manager import ExitManager
    from src.execution.safety.store import ExecutionStateStore
    from src.execution.safety.runtime import KRExecutionRuntime
    if unclean:
        from src.execution.safety.application import ApplicationBlocked
        with pytest.raises(ApplicationBlocked, match='command_state_drain_failed'):
            await runtime.shutdown()
        assert not runtime.owner.healthy
        assert runtime.health()['command_operations_pending'] == 0
        assert runtime.health()['command_results_pending'] == 0
    else:
        await runtime.shutdown()
    path = store.path
    await store.close()
    store = ExecutionStateStore(path)
    runtime = KRExecutionRuntime(store, UnifiedEngine(TradingConfig(initial_capital=Decimal('2000000'))),
        ExitManager(persist=False, clock=lambda: NOW), account_scope='scope', clock=lambda: NOW)
    await runtime.restore()
    runtime.attach()
    return store, runtime


def test_actual_owner_config_aba_is_stale_before_completion_hook(tmp_path):
    async def scenario():
        calls = []
        def hook(state, ticket, envelope, version):
            calls.append(ticket.operation_id)
            return state
        _, _, store, runtime, source = await fixture(tmp_path, reducer=hook)
        try:
            await runtime.owner.register_policy_generations('register', ('protection.config',))
            ticket = await source.begin('model', 'llm_regime', require_seal=True)
            original = runtime.owner.state['protection']['config']['first_exit_pct']
            await source.seal(ticket, versioned_policy_reads=('protection.config',), inputs={})
            def change(value):
                def reduce(state):
                    state['protection']['config']['first_exit_pct'] = value
                    return state
                return reduce
            await runtime.owner.mutate('A-B', change(original + 1))
            await runtime.owner.mutate('B-A', change(original))
            result = await source.complete(ticket, 'success', {})
            assert (result.status, result.reason) == ('stale', 'input_seal')
            assert calls == []
        finally:
            await runtime.shutdown()
            await store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('fault', ['sql_before_commit', 'committed_ack_loss', 'publisher'])
def test_sql_and_publication_failures_only_restore_actual_committed_history(tmp_path, monkeypatch, fault):
    import sqlite3
    from src.execution.safety.application import ApplicationBlocked
    from src.execution.safety.store import StoreError
    async def scenario():
        _, exits, store, runtime, _ = await fixture(tmp_path)
        try:
            await runtime.owner.register_policy_generations('register', ('protection.config',))
            before_version, before = await store.load()
            def change(state):
                state['protection']['config']['first_exit_pct'] += 1
                return state
            original_commit, original_publish = store.commit, runtime.owner.publisher
            if fault == 'sql_before_commit':
                with sqlite3.connect(store.path) as db:
                    db.execute("CREATE TRIGGER generation_fail BEFORE UPDATE ON checkpoint BEGIN SELECT RAISE(ABORT, 'synthetic precommit'); END")
            async def lose_ack(expected, state, command):
                await original_commit(expected, state, command)
                raise OSError('synthetic committed ACK loss')
            def fail_publish(state, version):
                raise ValueError('synthetic publisher failure')
            with monkeypatch.context() as patch:
                if fault == 'committed_ack_loss':
                    patch.setattr(store, 'commit', lose_ack)
                if fault == 'publisher':
                    patch.setattr(runtime.owner, 'publisher', fail_publish)
                with pytest.raises((StoreError, OSError, ApplicationBlocked)):
                    await runtime.owner.mutate('change', change)
            assert not runtime.owner.healthy
            durable_version, durable = await store.load()
            committed = fault != 'sql_before_commit'
            assert durable_version == before_version + int(committed)
            history = durable['policy_generations']['selectors']['protection.config']['history']
            assert len(history) == 1 + int(committed)
            assert runtime.owner.published_version == before_version
            if fault == 'sql_before_commit':
                with sqlite3.connect(store.path) as db:
                    db.execute('DROP TRIGGER generation_fail')
                assert durable == before
            await runtime.restore()
            store, runtime = await cold_restore(store, runtime)
            await runtime.owner.mutate('change', change)
            history = runtime.owner.state['policy_generations']['selectors']['protection.config']['history']
            assert len(history) == 2
            assert runtime.owner.version == before_version + 1
            assert not runtime.trading_ready
        finally:
            await runtime.shutdown()
            await store.close()
    asyncio.run(scenario())


def test_cancel_during_real_sql_does_not_publish_provisional_generation(tmp_path, monkeypatch):
    import threading
    async def scenario():
        _, _, store, runtime, _ = await fixture(tmp_path)
        entered, release = threading.Event(), threading.Event()
        try:
            await runtime.owner.register_policy_generations('register', ('protection.config',))
            before_version, before = await store.load()
            original = store._commit
            def gate(expected, payload, command):
                entered.set()
                assert release.wait(5)
                return original(expected, payload, command)
            def change(state):
                state['protection']['config']['first_exit_pct'] += 1
                return state
            with monkeypatch.context() as patch:
                patch.setattr(store, '_commit', gate)
                task = asyncio.create_task(runtime.owner.mutate('change', change))
                assert await asyncio.to_thread(entered.wait, 5)
                assert runtime.owner.state == before
                assert runtime.owner.published_version == before_version
                task.cancel()
                release.set()
                with pytest.raises(asyncio.CancelledError):
                    await task
            assert runtime.owner.state == before and not runtime.owner.healthy
            durable_version, durable = await store.load()
            assert durable_version == before_version + 1
            assert len(durable['policy_generations']['selectors']['protection.config']['history']) == 2
            store, runtime = await cold_restore(store, runtime, unclean=True)
            assert runtime.owner.state == durable
            await runtime.owner.mutate('change', change)
            assert runtime.owner.state == durable
        finally:
            release.set()
            await runtime.shutdown()
            await store.close()
    asyncio.run(scenario())


def test_registration_tracks_only_selected_canonical_changes_and_is_idempotent(tmp_path):
    async def scenario():
        _, _, store, runtime, source = await fixture(tmp_path)
        try:
            before = runtime.owner.state
            registered = await runtime.owner.register_policy_generations('register', (
                'protection.config', 'entry_policy_effects.sidecar_active'))
            state = runtime.owner.state
            registry = state.pop('policy_generations')
            assert state == before
            assert not runtime.trading_ready and source.snapshot().observation_status == 'missing'
            selectors = registry['selectors']
            sidecar = selectors['entry_policy_effects.sidecar_active']
            assert sidecar['registered_version'] == registered
            assert sidecar['history'][0]['present'] is False
            assert sidecar['history'][0]['value'] is None
            for index, value in enumerate([None, False, 0, True, False]):
                def change(state):
                    state.setdefault('entry_policy_effects', {})['sidecar_active'] = value
                    return state
                version = await runtime.owner.mutate('change-' + str(index), change)
                history = runtime.owner.state['policy_generations']['selectors']['entry_policy_effects.sidecar_active']['history']
                assert len(history) == index + 2
                assert history[-1]['version'] == version
                assert history[-1]['present'] is True
                assert type(history[-1]['value']) is type(value)
            last = runtime.owner.state['policy_generations']
            await runtime.owner.mutate('no-op', lambda s: s)
            await runtime.owner.register_policy_generations('register-again', tuple(selectors))
            assert await runtime.owner.register_policy_generations('register', tuple(selectors)) == registered
            assert runtime.owner.state['policy_generations']['selectors'] == last['selectors']
            assert last['selectors']['protection.config'] == selectors['protection.config']
            def absent(state):
                del state['entry_policy_effects']['sidecar_active']
                return state
            await runtime.owner.mutate('absent', absent)
            assert runtime.owner.state['policy_generations']['selectors']['entry_policy_effects.sidecar_active']['history'][-1]['present'] is False
        finally:
            await runtime.shutdown()
            await store.close()
    asyncio.run(scenario())


def test_own_completion_changes_policy_and_history_uses_strict_cutoff(tmp_path):
    async def scenario():
        calls = []
        def hook(state, ticket, envelope, version):
            calls.append(ticket.operation_id)
            state['protection']['config']['first_exit_pct'] += 1
            state['protection']['current_regime'] = 'trending_bear'
            return state
        _, _, store, runtime, source = await fixture(tmp_path, reducer=hook)
        try:
            registration = await runtime.owner.register_policy_generations('register', (
                'protection.config', 'protection.current_regime'))
            ticket = await source.begin('model', 'llm_regime', require_seal=True)
            seal = await source.seal(ticket, versioned_policy_reads=('protection.config',), inputs={})
            result = await source.complete(ticket, 'success', {})
            assert result.status == 'accepted'
            assert (await source.complete(ticket, 'success', {})) == result
            assert calls == ['model']
            for selector in runtime.owner.state['policy_generations']['selectors'].values():
                assert [event['version'] for event in selector['history']] == [registration, result.committed_version]
            await runtime.restore()
            next_ticket = await source.begin('next-model', 'llm_regime', require_seal=True)
            next_seal = await source.seal(next_ticket, versioned_policy_reads=('protection.config',), inputs={})
            assert json.loads(seal.reads_json)['versioned_policies']['protection.config']['generation'] == registration
            assert json.loads(next_seal.reads_json)['versioned_policies']['protection.config']['generation'] == result.committed_version
            def later(state):
                state['protection']['config']['first_exit_pct'] += 2
                return state
            await runtime.owner.mutate('later', later)
            store, runtime = await cold_restore(store, runtime)
            assert runtime.owner.state['risk_sources']['records']['model']['terminal']['receipt']['status'] == 'accepted'
        finally:
            await runtime.shutdown()
            await store.close()
    asyncio.run(scenario())


def test_real_command_sidecar_effect_aba_invalidates_seal(tmp_path):
    """실제 effect applicator; prepare/dispatch 또는 scheduler 연결 인수는 아니다."""
    from types import SimpleNamespace
    from dataclasses import replace
    from decimal import Decimal
    from src.execution.safety.commands import RequestBoundCommands
    from src.execution.safety import risk_policy as p
    from test_execution_risk_policy import snapshot
    async def scenario():
        calls = []
        def hook(state, ticket, envelope, version):
            calls.append(ticket.operation_id)
            return state
        _, _, store, runtime, source = await fixture(tmp_path, reducer=hook)
        try:
            # 합성 초기 인계 값; 이후 변경은 실제 정책 평가/effect 적용 경로다.
            await runtime.owner.mutate('seed-sidecar', lambda s: dict(s,
                entry_policy_effects={'sidecar_active': False, 'pending_sectors': {}}))
            await runtime.owner.register_policy_generations('register', ('entry_policy_effects.sidecar_active',))
            ticket = await source.begin('model', 'llm_regime', require_seal=True)
            await source.seal(ticket, versioned_policy_reads=('entry_policy_effects.sidecar_active',), inputs={})
            for index, recovering in enumerate((False, True)):
                base = snapshot(p)
                policy = replace(base, portfolio=replace(base.portfolio, effective_daily_pnl=Decimal('-400000')),
                    trend=p.MarketTrendPolicySnapshot(True, recovering, bool(index)))
                decision = p.evaluate_daily_loss('sepa_trend', policy)
                def reduce(state):
                    RequestBoundCommands._effects(state, SimpleNamespace(symbol='005930'), decision, commit=True)
                    return state
                await runtime.owner.mutate('command-effect-' + str(index), reduce)
            assert runtime.owner.state['entry_policy_effects']['sidecar_active'] is False
            assert len(runtime.owner.state['policy_generations']['selectors']['entry_policy_effects.sidecar_active']['history']) == 3
            assert (await source.complete(ticket, 'success', {})).status == 'stale'
            assert calls == []
        finally:
            await runtime.shutdown()
            await store.close()
    asyncio.run(scenario())


def test_actual_intraday_changes_only_affected_selectors(tmp_path, monkeypatch):
    from datetime import timedelta
    from test_execution_intraday_owner import owned
    from test_kis_index_observation import provider, raw
    from src.execution.safety.risk_sources import RiskSourceCoordinator
    async def scenario():
        clock = [NOW]
        _, _, store, runtime, _, writer = await owned(tmp_path, clock=lambda: clock[0])
        try:
            await runtime.owner.register_policy_generations('register', (
                'intraday_policy.current', 'protection.intraday_crash_level', 'protection.config'))
            source = RiskSourceCoordinator(runtime)
            ticket = await source.begin('model', 'llm_regime', require_seal=True)
            await source.seal(ticket, versioned_policy_reads=('intraday_policy.current',), inputs={})
            output = raw()
            output['bstp_nmix_prdy_ctrt'] = '-3'
            md, _ = await provider(monkeypatch, output)
            first = await writer.refresh(md)
            original = runtime.owner.state['policy_generations']
            assert original['selectors']['intraday_policy.current']['history'][-1]['version'] == first.committed_version
            await writer.refresh(md)
            assert runtime.owner.state['policy_generations'] == original
            clock[0] += timedelta(seconds=1)
            output['bstp_nmix_prdy_ctrt'] = '-3.1'
            md, _ = await provider(monkeypatch, output)
            monkeypatch.setattr(md, '_index_clock', lambda: clock[0])
            await writer.refresh(md)
            newer = runtime.owner.state['policy_generations']['selectors']
            assert len(newer['intraday_policy.current']['history']) == 3
            assert newer['protection.intraday_crash_level'] == original['selectors']['protection.intraday_crash_level']
            assert newer['protection.config'] == original['selectors']['protection.config']
            assert (await source.complete(ticket, 'success', {})).status == 'stale'
            store, runtime = await cold_restore(store, runtime)
            assert runtime.owner.state['policy_generations']['selectors'] == newer
        finally:
            await runtime.shutdown()
            await store.close()
    asyncio.run(scenario())


def test_actual_fill_inbox_quote_journal_ack_keep_policy_history(tmp_path):
    from decimal import Decimal
    from src.execution.safety.journal_delivery import OutboxDispatcher, PostgresExecutionJournal
    from test_execution_journal_delivery import Pool
    from test_execution_runtime import opened, observed, queued
    async def scenario():
        engine, _, store, runtime, source = await fixture(tmp_path)
        try:
            await runtime.owner.register_policy_generations('register', (
                'protection.config', 'protection.current_regime', 'protection.intraday_crash_level',
                'entry_policy_effects.sidecar_active', 'intraday_policy.current'))
            before = runtime.owner.state['policy_generations']
            ticket = await source.begin('model', 'llm_regime', require_seal=True)
            await source.seal(ticket, versioned_policy_reads=tuple(before['selectors']), inputs={})
            ref = await opened(runtime, 'B1')
            observation = await observed(runtime, ref, 40, '400000')
            assert (await queued(engine, observation)).status == 'APPLIED'
            assert (await queued(engine, observation)).status == 'ALREADY_APPLIED'
            await runtime.quote('005930', Decimal('10100'))
            assert (await OutboxDispatcher(runtime.owner, PostgresExecutionJournal(Pool())).drain()).delivered == 1
            assert runtime.owner.state['policy_generations'] == before
            assert (await source.complete(ticket, 'success', {})).status == 'accepted'
            economic = {name: runtime.owner.state[name] for name in ('portfolio', 'lots', 'outbox', 'risk', 'attempts')}
            store, runtime = await cold_restore(store, runtime)
            assert runtime.owner.state['policy_generations'] == before
            assert {name: runtime.owner.state[name] for name in economic} == economic
        finally:
            await runtime.shutdown()
            await store.close()
    asyncio.run(scenario())


def test_actual_prepare_and_pending_sector_release_leave_selected_history_unchanged(tmp_path, monkeypatch):
    from test_execution_command_owner import fixture as command_fixture
    from src.execution.safety.lifecycle import CommandResult, CommandStatus
    from src.execution.safety.risk_sources import RiskSourceCoordinator
    async def scenario():
        f = await command_fixture(tmp_path, monkeypatch, ready=False, origin='automatic')
        runtime, store = f['runtime'], f['store']
        try:
            await runtime.owner.register_policy_generations('register', ('entry_policy_effects.sidecar_active', 'protection.config'))
            source = RiskSourceCoordinator(runtime)
            ticket = await source.begin('model', 'llm_regime', require_seal=True)
            await source.seal(ticket, versioned_policy_reads=('entry_policy_effects.sidecar_active', 'protection.config'), inputs={})
            before = runtime.owner.state['policy_generations']
            request = f['request']()
            await f['quote'](request)
            await f['facts'](request, sector='반도체')
            await f['commands'].prepare(request, f['entry'](request), sector='반도체')
            assert runtime.owner.state['entry_policy_effects']['pending_sectors'] == {'005930': '반도체'}
            await runtime.lifecycle.claim(request.attempt_id, 'sender')
            assert await runtime.lifecycle.record_result(request.attempt_id, 'sender',
                CommandResult(CommandStatus.NOT_SENT, request.attempt_id),
                expected_attempt_version=runtime.owner.state['attempts'][request.attempt_id]['version'])
            assert runtime.owner.state['entry_policy_effects']['pending_sectors'] == {}
            assert runtime.owner.state['policy_generations'] == before
            assert (await source.complete(ticket, 'success', {})).status == 'accepted'
            assert not runtime.trading_ready and f['broker']._session.posts == []
        finally:
            await runtime.shutdown()
            await store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('fault', ['ack_loss', 'publisher'])
def test_committed_actual_fill_failure_cold_restore_does_not_repeat_economics(tmp_path, monkeypatch, fault):
    from src.execution.safety.application import ApplicationBlocked
    from test_execution_runtime import opened, observed, queued
    async def scenario():
        engine, _, store, runtime, _ = await fixture(tmp_path)
        try:
            await runtime.owner.register_policy_generations('register', ('protection.config',))
            registry = runtime.owner.state['policy_generations']
            ref = await opened(runtime, 'B1')
            observation = await observed(runtime, ref, 100, '1000000')
            original_commit, original_publish = store.commit, runtime.owner.publisher
            async def lose_ack(expected, state, command):
                result = await original_commit(expected, state, command)
                if command.startswith('fill:'):
                    raise OSError('synthetic fill ACK loss')
                return result
            def fail_publish(state, version):
                if state.get('cursors'):
                    raise ValueError('synthetic fill publish')
                return original_publish(state, version)
            with monkeypatch.context() as patch:
                if fault == 'ack_loss':
                    patch.setattr(store, 'commit', lose_ack)
                else:
                    patch.setattr(runtime.owner, 'publisher', fail_publish)
                with pytest.raises((OSError, ApplicationBlocked)):
                    await queued(engine, observation)
            durable = (await store.load())[1]
            assert durable['cursors'][observation.order_key]['quantity'] == 100
            assert durable['policy_generations'] == registry
            assert not runtime.owner.healthy
            # 기존 runtime의 실패 ingress와 새 cold owner를 분리한다.
            store, runtime = await cold_restore(store, runtime, unclean=True)
            assert runtime.owner.state == durable
            economic = {key: durable[key] for key in ('portfolio', 'lots', 'risk', 'outbox', 'attempts')}
            assert (await queued(runtime.engine, observation)).status == 'ALREADY_APPLIED'
            assert {key: runtime.owner.state[key] for key in economic} == economic
            assert runtime.owner.state['policy_generations'] == registry
        finally:
            await runtime.shutdown()
            await store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('attack', ['reorder', 'bool_version', 'missing_event', 'tail', 'seal_generation', 'registration', 'null'])
def test_cold_restore_rejects_history_and_resealed_crosslink_tampering(tmp_path, attack):
    from src.execution.safety.application import ApplicationBlocked
    from src.execution.safety.protection_recovery import digest
    async def scenario():
        _, _, store, runtime, source = await fixture(tmp_path)
        try:
            await runtime.owner.register_policy_generations('register', ('protection.config',))
            ticket = await source.begin('model', 'llm_regime', require_seal=True)
            await source.seal(ticket, versioned_policy_reads=('protection.config',), inputs={})
            await source.complete(ticket, 'success', {})
            def change(state):
                state['protection']['config']['first_exit_pct'] += 1
                return state
            await runtime.owner.mutate('change', change)
            bad = runtime.owner.state
            row = bad['policy_generations']['selectors']['protection.config']
            if attack == 'reorder':
                row['history'].reverse()
            elif attack == 'bool_version':
                row['history'][0]['version'] = True
            elif attack == 'missing_event':
                row['history'].pop(0)
                row['registered_version'] = row['history'][0]['version']
            elif attack == 'tail':
                row['history'][-1]['value']['first_exit_pct'] += 2
                row['history'][-1]['digest'] = digest({k: row['history'][-1][k] for k in ('present', 'value')})
            elif attack == 'registration':
                row['registered_version'] += 1
            elif attack == 'null':
                bad['policy_generations'] = None
            else:
                original = bad['risk_input_seals']['records']['model']['original']
                original['reads']['versioned_policies']['protection.config']['generation'] += 1
                original['seal_digest'] = digest({k: v for k, v in original.items() if k != 'seal_digest'})
            await store.commit(runtime.owner.version, bad, 'synthetic-tamper')
            await runtime.shutdown()
            from src.execution.safety.runtime import KRExecutionRuntime
            from src.execution.safety.store import ExecutionStateStore
            from src.core.engine import UnifiedEngine
            from src.core.types import TradingConfig
            from src.strategies.exit_manager import ExitManager
            path = store.path
            await store.close()
            store = ExecutionStateStore(path)
            runtime = KRExecutionRuntime(store, UnifiedEngine(TradingConfig()),
                ExitManager(persist=False, clock=lambda: NOW), account_scope='scope', clock=lambda: NOW)
            with pytest.raises(ApplicationBlocked):
                await runtime.restore()
            assert not runtime.owner.healthy and not runtime.trading_ready
        finally:
            await runtime.shutdown()
            await store.close()
    asyncio.run(scenario())


def test_unregistered_versioned_selector_fails_without_legacy_promotion(tmp_path):
    async def scenario():
        _, _, store, runtime, source = await fixture(tmp_path)
        try:
            legacy = await source.begin('legacy', 'llm_regime', require_seal=True)
            await source.seal(legacy, policy_reads=('protection.config',), inputs={})
            await source.complete(legacy, 'success', {})
            original = runtime.owner.state['risk_input_seals']
            await runtime.restore()
            assert 'policy_generations' not in runtime.owner.state
            await runtime.owner.register_policy_generations('register', ('protection.current_regime',))
            assert runtime.owner.state['risk_input_seals'] == original
            ticket = await source.begin('new', 'llm_regime', require_seal=True)
            before = await store.load()
            with pytest.raises(ValueError, match='unregistered'):
                await source.seal(ticket, versioned_policy_reads=('protection.config',), inputs={})
            assert await store.load() == before
            assert runtime.owner.healthy
            assert not runtime.health()['command_results_failed']
        finally:
            await runtime.shutdown()
            await store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('attack', ['insert', 'rewrite', 'delete'])
def test_actual_fill_write_set_rejects_metadata_attacks(tmp_path, attack):
    from src.execution.safety.application import FillReduction
    from test_execution_runtime import opened, observed, queued
    async def scenario():
        engine, _, store, runtime, _ = await fixture(tmp_path)
        try:
            if attack != 'insert':
                await runtime.owner.register_policy_generations('register', ('protection.config',))
            ref = await opened(runtime, 'B1')
            observation = await observed(runtime, ref, 40, '400000')
            original = runtime.owner.reducer
            def corrupt(state, observation, delta):
                result = original(state, observation, delta)
                if attack == 'insert':
                    result.state['policy_generations'] = {'schema': 1, 'selectors': {}}
                elif attack == 'delete':
                    del result.state['policy_generations']
                else:
                    result.state['policy_generations']['selectors']['protection.config']['history'][0]['version'] += 1
                return FillReduction(result.state, result.protection_status, result.journal_pending)
            runtime.owner.reducer = corrupt
            before = runtime.owner.state
            assert (await queued(engine, observation)).status == 'FAILED'
            after = (await store.load())[1]
            assert after.get('policy_generations') == before.get('policy_generations')
            assert after['portfolio'] == before['portfolio']
            assert not after['outbox']
            runtime.owner.reducer = original
            assert (await queued(engine, observation)).status == 'APPLIED'
        finally:
            await runtime.shutdown()
            await store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('attack', ['insert', 'delete', 'rewrite', 'append', 'schema', 'parent'])
def test_reducer_cannot_forge_generation_metadata_before_sql(tmp_path, attack):
    async def scenario():
        _, _, store, runtime, _ = await fixture(tmp_path)
        try:
            if attack != 'insert':
                await runtime.owner.register_policy_generations('register', ('protection.config',))
            before = await store.load()
            def corrupt(state):
                if attack == 'insert':
                    state['policy_generations'] = {'schema': 1, 'selectors': {}}
                elif attack == 'delete':
                    del state['policy_generations']
                elif attack == 'parent':
                    state['protection'] = None
                elif attack == 'schema':
                    state['policy_generations']['schema'] = 2
                else:
                    history = state['policy_generations']['selectors']['protection.config']['history']
                    if attack == 'rewrite':
                        history[0]['version'] += 1
                    else:
                        history.append(deepcopy(history[0]))
                return state
            with pytest.raises(ValueError):
                await runtime.owner.mutate('attack', corrupt)
            assert await store.load() == before
            assert runtime.owner.healthy
        finally:
            await runtime.shutdown()
            await store.close()
    asyncio.run(scenario())
