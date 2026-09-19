"""Owner-issued detached input seals; actual SQLite, no external authority implied."""
import asyncio
from copy import deepcopy
from datetime import timedelta

import pytest

from src.execution.safety.risk_sources import RiskSourceCoordinator, validate_risk_sources
from src.execution.safety.application import ApplicationBlocked
from test_execution_runtime import NOW, setup


async def fixture(tmp_path, *, clock=None, reducer=None):
    engine, exits, store, runtime = await setup(tmp_path, account_scope='scope', clock=clock)
    source = RiskSourceCoordinator(runtime, completion_reducer=reducer)
    return engine, exits, store, runtime, source


def test_required_seal_is_durable_and_cannot_be_bypassed_by_another_facade(tmp_path):
    async def scenario():
        _, _, store, runtime, source = await fixture(tmp_path)
        try:
            ticket = await source.begin('required', 'llm_regime', require_seal=True)
            await runtime.restore()
            other = RiskSourceCoordinator(runtime)
            with pytest.raises(ValueError, match='conflict'):
                await other.begin('required', 'llm_regime')
            result = await other.complete(ticket, 'success', {'regime': 'bull'})
            assert (result.status, result.reason) == ('stale', 'input_seal')
        finally:
            await runtime.shutdown()
            await store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('damage', ['null_root', 'source_version_collision'])
def test_seal_schema_rejects_null_root_and_foreign_commit_version(tmp_path, damage):
    from src.execution.safety.protection_recovery import digest
    async def scenario():
        _, _, store, runtime, source = await fixture(tmp_path)
        try:
            first = await source.begin('model', 'llm_regime', require_seal=True)
            await source.seal(first, inputs={})
            another = await source.begin('another', 'index_trend')
            state = runtime.owner.state
            if damage == 'null_root':
                state['risk_input_seals'] = None
            else:
                row = state['risk_input_seals']['records']['model']['original']
                row['version'] = another.admission_version
                row['seal_digest'] = digest({key: value for key, value in row.items() if key != 'seal_digest'})
            with pytest.raises(ValueError):
                validate_risk_sources(state, runtime.owner.version)
        finally:
            await runtime.shutdown()
            await store.close()
    asyncio.run(scenario())


def test_seal_after_unsealed_terminal_is_rejected_without_poisoning_owner(tmp_path):
    async def scenario():
        _, _, store, runtime, source = await fixture(tmp_path)
        try:
            ticket = await source.begin('legacy', 'llm_regime')
            await source.complete(ticket, 'success', {})
            before = runtime.owner.state
            with pytest.raises(ValueError, match='source_terminal'):
                await source.seal(ticket, inputs={})
            assert runtime.owner.state == before and runtime.owner.healthy
        finally:
            await runtime.shutdown()
            await store.close()
    asyncio.run(scenario())


def test_explicit_previous_day_vix_fact_is_preserved_but_not_reapproved(tmp_path):
    import json
    async def scenario():
        clock = [NOW]
        _, _, store, runtime, source = await fixture(tmp_path, clock=lambda: clock[0])
        try:
            old = await source.begin('yesterday-vix', 'vix_regime')
            await source.complete(old, 'success', {'value': 14.0})
            clock[0] += timedelta(days=1)
            def next_day(state):
                from src.execution.safety.economics import new_risk_state
                state['risk'] = new_risk_state(clock[0].date().isoformat())
                return state
            await runtime.owner.mutate('synthetic-next-day-baseline', next_day)
            latest = await source.begin('vix-failed-today', 'vix_regime')
            await source.complete(latest, 'failed')
            ticket = await source.begin('today-model', 'llm_regime', require_seal=True)
            result = await source.seal(ticket, source_lanes=('vix_regime',),
                                       retained_sources=('yesterday-vix',), inputs={})
            facts = json.loads(result.reads_json)
            assert facts['retained']['yesterday-vix']['ticket']['business_day'] == NOW.date().isoformat()
            assert facts['sources']['vix_regime']['terminal']['receipt']['status'] == 'failed'
            assert source.snapshot().observation_status == 'missing'
            assert (await source.complete(ticket, 'success', {})).status == 'accepted'
        finally:
            await runtime.shutdown()
            await store.close()
    asyncio.run(scenario())


def test_first_await_mask_is_shared_and_new_lanes_never_supersede_intraday(tmp_path):
    async def scenario():
        _, _, store, runtime, source = await fixture(tmp_path)
        try:
            old = await source.begin('old', 'intraday_5m')
            await source.complete(old, 'success', {'level': 'normal'}, source='synthetic',
                source_event_id='old', received_at=NOW, market_as_of=NOW)
            await runtime.owner._lock.acquire()
            try:
                pending = asyncio.create_task(source.begin('new', 'intraday_5m', require_seal=True))
                await asyncio.sleep(0)
                assert RiskSourceCoordinator(runtime).snapshot().observation_status == 'pending'
            finally:
                runtime.owner._lock.release()
            ticket = await pending
            for kind in ('vix_regime', 'expert_regime', 'llm_morning_diagnosis'):
                other = await source.begin(kind, kind)
                assert other.lane == kind
                await source.complete(other, 'missing')
            await source.seal(ticket, inputs={})
            assert (await source.complete(ticket, 'success', {'level': 'crash'}, source='synthetic',
                source_event_id='new', received_at=NOW, market_as_of=NOW)).status == 'accepted'
            assert source.snapshot().level == 'crash'
            assert (await source.seal(ticket, inputs={'changed': True})).status == 'conflict'
            assert source.snapshot().observation_status == 'conflict'
        finally:
            await runtime.shutdown()
            await store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('when', ['seal', 'complete'])
def test_superseding_same_lane_keeps_late_source_out_of_hook(tmp_path, when):
    async def scenario():
        calls = []
        def hook(state, ticket, envelope, version):
            calls.append(ticket.operation_id)
            return state
        _, _, store, runtime, source = await fixture(tmp_path, reducer=hook)
        try:
            old = await source.begin('old', 'llm_regime', require_seal=True)
            if when == 'complete':
                await source.seal(old, inputs={})
            await source.begin('new', 'llm_regime', require_seal=True)
            if when == 'seal':
                assert (await source.seal(old, inputs={})).status == 'stale'
            result = await source.complete(old, 'success', {})
            assert (result.status, result.reason) == ('stale', 'superseded')
            assert calls == []
        finally:
            await runtime.shutdown()
            await store.close()
    asyncio.run(scenario())


def test_cancelled_seal_waiter_is_drained_and_original_evidence_is_durable(tmp_path, monkeypatch):
    async def scenario():
        _, _, store, runtime, source = await fixture(tmp_path)
        entered, release = asyncio.Event(), asyncio.Event()
        original = store.commit
        try:
            ticket = await source.begin('model', 'llm_regime', require_seal=True)
            async def gate(expected, state, command):
                if 'risk-seal:' in command:
                    entered.set()
                    await release.wait()
                return await original(expected, state, command)
            with monkeypatch.context() as patch:
                patch.setattr(store, 'commit', gate)
                waiter = asyncio.create_task(source.seal(ticket, inputs={'x': 1}))
                await entered.wait()
                waiter.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await waiter
                stopping = asyncio.create_task(runtime.shutdown())
                await asyncio.sleep(0)
                assert not stopping.done()
                release.set()
                await stopping
            state = (await store.load())[1]
            assert state['risk_input_seals']['records']['model']['original']['status'] == 'sealed'
            assert state['risk_sources']['records']['model']['terminal'] is None
        finally:
            release.set()
            await runtime.shutdown()
            await store.close()
    asyncio.run(scenario())


def test_hook_cannot_rewrite_seal_even_with_recomputed_digests(tmp_path):
    async def scenario():
        def hook(state, ticket, envelope, version):
            state.pop('risk_input_seals')
            return state
        _, _, store, runtime, source = await fixture(tmp_path, reducer=hook)
        try:
            # Optional seal still becomes immutable evidence once it exists.
            ticket = await source.begin('model', 'llm_regime')
            await source.seal(ticket, inputs={})
            before = (await store.load())[1]
            with pytest.raises(ValueError, match='completion_reducer_changed'):
                await source.complete(ticket, 'success', {})
            assert (await store.load())[1] == before
        finally:
            with pytest.raises(ApplicationBlocked, match='command_result_drain_failed'):
                await runtime.shutdown()
            await store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('committed', [False, True])
def test_seal_sql_failure_and_cold_restore_use_only_actual_commit(tmp_path, monkeypatch, committed):
    async def scenario():
        _, _, store, runtime, source = await fixture(tmp_path)
        try:
            ticket = await source.begin('model', 'llm_regime', require_seal=True)
            original = store.commit
            async def fail(expected, state, command):
                if 'risk-seal:' in command:
                    if committed:
                        await original(expected, state, command)
                    raise OSError('synthetic input seal SQL failure')
                return await original(expected, state, command)
            with monkeypatch.context() as patch:
                patch.setattr(store, 'commit', fail)
                with pytest.raises(OSError, match='synthetic input seal SQL failure'):
                    await source.seal(ticket, source_lanes=('index_trend',), inputs={'x': 1})
            durable = (await store.load())[1]
            assert ('risk_input_seals' in durable) is committed
            # Failed accepted result tasks must drain fail-closed before store close.
            with pytest.raises(ApplicationBlocked, match='command_result_drain_failed'):
                await runtime.shutdown()
            path = store.path
            await store.close()
            from src.core.engine import UnifiedEngine
            from src.core.types import TradingConfig
            from src.strategies.exit_manager import ExitManager
            from src.execution.safety.store import ExecutionStateStore
            from src.execution.safety.runtime import KRExecutionRuntime
            from decimal import Decimal
            engine = UnifiedEngine(TradingConfig(initial_capital=Decimal('2000000')))
            exits = ExitManager(persist=False, clock=lambda: NOW)
            store = ExecutionStateStore(path)
            runtime = KRExecutionRuntime(store, engine, exits, account_scope='scope', clock=lambda: NOW)
            await runtime.restore()
            runtime.attach()
            assert runtime.owner.state == durable
            source = RiskSourceCoordinator(runtime)
            receipt = await source.seal(ticket, source_lanes=('index_trend',), inputs={'x': 1})
            assert receipt.status == 'sealed'
            assert receipt.ticket_digest == ticket.request_digest
            if committed:
                assert runtime.owner.state == durable
            assert (await source.complete(ticket, 'success', {})).status == 'accepted'
        finally:
            await runtime.shutdown()
            await store.close()
    asyncio.run(scenario())


def test_accepted_hook_can_change_its_declared_policy_without_invalidating_history(tmp_path):
    async def scenario():
        def hook(state, ticket, envelope, version):
            state['protection']['config']['first_exit_pct'] = 12.0
            return state
        _, _, store, runtime, source = await fixture(tmp_path, reducer=hook)
        try:
            ticket = await source.begin('model', 'llm_regime', require_seal=True)
            await source.seal(ticket, policy_reads=('protection.config',), inputs={})
            seals = runtime.owner.state['risk_input_seals']
            receipt = await source.complete(ticket, 'success', {})
            assert receipt.status == 'accepted'
            await runtime.restore()
            assert runtime.owner.state['protection']['config']['first_exit_pct'] == 12.0
            assert runtime.owner.state['risk_input_seals'] == seals
            assert await source.complete(ticket, 'success', {}) == receipt
        finally:
            await runtime.shutdown()
            await store.close()
    asyncio.run(scenario())


def test_shutdown_waits_for_original_scope_seal_and_late_completion(tmp_path):
    async def scenario():
        _, _, store, runtime, source = await fixture(tmp_path)
        fetched, finish = asyncio.Event(), asyncio.Event()
        async def caller():
            with runtime.command_scope() as token:
                ticket = await source.begin('model', 'llm_regime', require_seal=True)
                fetched.set()
                await finish.wait()
                await source.seal(ticket, inputs={}, scope_token=token)
                return await source.complete(ticket, 'success', {}, scope_token=token)
        try:
            pending = asyncio.create_task(caller())
            await fetched.wait()
            stopping = asyncio.create_task(runtime.shutdown())
            await asyncio.sleep(0)
            assert not stopping.done()
            with pytest.raises(ApplicationBlocked):
                await source.begin('late', 'expert_regime')
            finish.set()
            assert (await pending).status == 'accepted'
            await stopping
        finally:
            finish.set()
            await runtime.shutdown()
            await store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('damage', ['source_root', 'seal_root', 'record_alias', 'source_fact', 'policy_digest'])
def test_restore_validator_rejects_removed_or_aliased_seal_evidence(tmp_path, damage):
    async def scenario():
        _, _, store, runtime, source = await fixture(tmp_path)
        try:
            ticket = await source.begin('model', 'llm_regime', require_seal=True)
            await source.seal(ticket, source_lanes=('index_trend',),
                              policy_reads=('protection.config',), inputs={})
            await source.complete(ticket, 'success', {})
            state = runtime.owner.state
            if damage == 'source_root': del state['risk_sources']
            elif damage == 'seal_root': del state['risk_input_seals']
            elif damage == 'record_alias':
                state['risk_input_seals']['records']['another'] = state['risk_input_seals']['records'].pop('model')
            elif damage == 'source_fact':
                state['risk_input_seals']['records']['model']['original']['reads']['sources']['index_trend'] = {}
            else:
                state['risk_input_seals']['records']['model']['original']['reads']['policies']['protection.config']['digest'] = '0' * 64
            # Actual store, then real restore/publication validator; never turn the guard off.
            await store.commit(runtime.owner.version, state, 'synthetic-corrupt-checkpoint')
            with pytest.raises(ApplicationBlocked, match='게시 실패'):
                await runtime.restore()
            assert not runtime.owner.healthy
        finally:
            await store.close()
    asyncio.run(scenario())


def test_optional_seal_conflict_after_completion_cannot_be_new_dependency(tmp_path):
    async def scenario():
        _, _, store, runtime, source = await fixture(tmp_path)
        try:
            ticket = await source.begin('index', 'index_trend')
            await source.seal(ticket, inputs={'x': 1})
            receipt = await source.complete(ticket, 'success', {'regime': 'bull'})
            assert (await source.seal(ticket, inputs={'x': 2})).status == 'conflict'
            with pytest.raises(ValueError, match='dependency'):
                await source.begin('model', 'llm_regime', dependencies={'index_trend': receipt.committed_version})
        finally:
            await runtime.shutdown()
            await store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('outcome', ['missing', 'failed', 'cancelled'])
def test_required_unsealed_failure_is_terminal_and_needs_no_seal(tmp_path, outcome):
    async def scenario():
        _, _, store, runtime, source = await fixture(tmp_path)
        try:
            ticket = await source.begin('failed-input', 'llm_regime', require_seal=True)
            receipt = await source.complete(ticket, outcome)
            assert receipt.status == outcome
            await runtime.restore()
            assert await source.complete(ticket, outcome) == receipt
        finally:
            await runtime.shutdown()
            await store.close()
    asyncio.run(scenario())


def test_reseal_is_idempotent_and_conflict_is_permanent_without_overwrite(tmp_path):
    async def scenario():
        _, _, store, runtime, source = await fixture(tmp_path)
        try:
            ticket = await source.begin('model', 'llm_regime', require_seal=True)
            first = await source.seal(ticket, inputs={'value': 1})
            version = runtime.owner.version
            assert await source.seal(ticket, inputs={'value': 1}) == first
            assert runtime.owner.version == version
            original = runtime.owner.state['risk_input_seals']['records']['model']['original']
            conflict = await source.seal(ticket, inputs={'value': 2})
            assert conflict.status == 'conflict'
            await runtime.restore()
            assert await source.seal(ticket, inputs={'value': 1}) == conflict
            assert runtime.owner.state['risk_input_seals']['records']['model']['original'] == original
            result = await source.complete(ticket, 'success', {'level': 'normal'})
            assert (result.status, result.reason) == ('stale', 'input_seal')
        finally:
            await runtime.shutdown()
            await store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('bad', ['self_lane', 'self_operation', 'jsonpath', 'future_policy', 'dict_key', 'nan', 'duplicate'])
def test_untrusted_selectors_and_noncanonical_bundle_are_rejected_without_commit(tmp_path, bad):
    async def scenario():
        _, _, store, runtime, source = await fixture(tmp_path)
        try:
            ticket = await source.begin('model', 'llm_regime', require_seal=True)
            args = {'inputs': {}}
            if bad == 'self_lane': args['source_lanes'] = ('llm_regime',)
            elif bad == 'self_operation': args['retained_sources'] = ('model',)
            elif bad == 'jsonpath': args['policy_reads'] = ('protection.*',)
            elif bad == 'future_policy': args['policy_reads'] = ('regime_policy.current',)
            elif bad == 'dict_key': args['inputs'] = {1: 'coerced'}
            elif bad == 'nan': args['inputs'] = {'x': float('nan')}
            else: args['source_lanes'] = ('index_trend', 'index_trend')
            before, version = runtime.owner.state, runtime.owner.version
            with pytest.raises(ValueError):
                await source.seal(ticket, **args)
            assert runtime.owner.state == before and runtime.owner.version == version
        finally:
            await runtime.shutdown()
            await store.close()
    asyncio.run(scenario())


def test_actual_fill_and_ack_do_not_starve_a_sealed_source(tmp_path):
    from src.execution.safety.journal_delivery import OutboxDispatcher, PostgresExecutionJournal
    from test_execution_journal_delivery import Pool
    from test_execution_runtime import opened, observed, queued
    async def scenario():
        engine, _, store, runtime, source = await fixture(tmp_path)
        try:
            ticket = await source.begin('model', 'llm_regime', require_seal=True)
            seal = await source.seal(ticket, policy_reads=('protection.config',), inputs={})
            ref = await opened(runtime, 'B1')
            await queued(engine, await observed(runtime, ref, 40, '400000'))
            assert (await OutboxDispatcher(runtime.owner, PostgresExecutionJournal(Pool())).drain()).delivered == 1
            before = runtime.owner.state
            assert runtime.owner.version > seal.committed_version
            assert (await source.complete(ticket, 'success', {'regime': 'bull'})).status == 'accepted'
            for key in ('portfolio', 'risk', 'outbox', 'lots', 'attempts', 'protection'):
                assert runtime.owner.state[key] == before[key]
        finally:
            await runtime.shutdown()
            await store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('moment', ['before_seal', 'before_complete'])
def test_day_change_is_stale_without_reinterpreting_input_time(tmp_path, moment):
    async def scenario():
        clock = [NOW]
        _, _, store, runtime, source = await fixture(tmp_path, clock=lambda: clock[0])
        try:
            ticket = await source.begin('model', 'llm_regime', require_seal=True)
            if moment == 'before_complete':
                await source.seal(ticket, inputs={})
            clock[0] += timedelta(days=1)
            if moment == 'before_seal':
                result = await source.seal(ticket, inputs={})
                assert (result.status, result.reason) == ('stale', 'day_or_generation')
            result = await source.complete(ticket, 'success', {})
            assert (result.status, result.reason) == ('stale', 'day_or_generation')
        finally:
            await runtime.shutdown()
            await store.close()
    asyncio.run(scenario())


def test_seal_captures_absent_and_failed_sources_and_original_accepted_vix(tmp_path):
    async def scenario():
        _, _, store, runtime, source = await fixture(tmp_path)
        try:
            vix = await source.begin('vix-old', 'vix_regime')
            await source.complete(vix, 'success', {'value': 12.0})
            bad = await source.begin('vix-new', 'vix_regime')
            await source.complete(bad, 'failed')
            ticket = await source.begin('model', 'llm_regime', require_seal=True)
            inputs = {'optional': None, 'prices': [1.0, 2.0]}
            receipt = await source.seal(ticket, source_lanes=('vix_regime', 'expert_regime'),
                retained_sources=('vix-old',), policy_reads=('protection.config',), inputs=inputs)
            assert receipt.status == 'sealed'
            inputs['prices'].append(3.0)
            import json
            assert json.loads(receipt.inputs_json) == {'optional': None, 'prices': [1.0, 2.0]}
            facts = json.loads(receipt.reads_json)
            assert facts['sources']['expert_regime'] is None
            assert facts['sources']['vix_regime']['terminal']['receipt']['status'] == 'failed'
            assert facts['retained']['vix-old']['terminal']['envelope']['payload'] == {'value': 12.0}
            before = runtime.owner.state
            result = await source.complete(ticket, 'success', {'regime': 'neutral'})
            assert result.status == 'accepted'
            assert runtime.owner.state['risk_input_seals'] == before['risk_input_seals']
            await runtime.restore()
            validate_risk_sources(runtime.owner.state, runtime.owner.version)
        finally:
            await runtime.shutdown()
            await store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('change', ['absent_begin', 'pending_failure', 'accepted_conflict', 'accepted_seal_conflict', 'policy'])
def test_changed_declared_reads_stop_completion_before_hook(tmp_path, change):
    async def scenario():
        calls = []
        def reducer(state, ticket, envelope, version):
            calls.append(ticket.operation_id)
            return state
        _, _, store, runtime, source = await fixture(tmp_path, reducer=reducer)
        try:
            dependency = None
            if change in {'pending_failure', 'accepted_conflict', 'accepted_seal_conflict'}:
                dependency = await source.begin('dep', 'index_trend')
                if change == 'accepted_seal_conflict':
                    await source.seal(dependency, inputs={'original': True})
                if change in {'accepted_conflict', 'accepted_seal_conflict'}:
                    await source.complete(dependency, 'success', {'regime': 'bull'})
            ticket = await source.begin('model', 'llm_regime', require_seal=True)
            await source.seal(ticket, source_lanes=('index_trend',),
                policy_reads=('protection.config',), inputs={})
            if change == 'absent_begin':
                await source.begin('dep', 'index_trend')
            elif change == 'pending_failure':
                await source.complete(dependency, 'failed')
            elif change == 'accepted_conflict':
                await source.complete(dependency, 'success', {'regime': 'bear'})
            elif change == 'accepted_seal_conflict':
                await source.seal(dependency, inputs={'original': False})
            else:
                def mutate(state):
                    state['protection']['config']['first_exit_pct'] = 12.0
                    return state
                await runtime.owner.mutate('synthetic-policy', mutate)
            calls.clear()
            result = await source.complete(ticket, 'success', {'regime': 'bull'})
            assert (result.status, result.reason) == ('stale', 'input_seal')
            assert calls == []
        finally:
            await runtime.shutdown()
            await store.close()
    asyncio.run(scenario())


def test_known_limit_value_only_policy_read_cannot_detect_actual_owner_aba(tmp_path):
    """Characterization, not approval: a future policy generation must close this gap."""
    async def scenario():
        _, _, store, runtime, source = await fixture(tmp_path)
        try:
            ticket = await source.begin('value-only-model', 'llm_regime', require_seal=True)
            original = runtime.owner.state['protection']['config']['first_exit_pct']
            seal = await source.seal(ticket, policy_reads=('protection.config',), inputs={})
            def changed(state):
                state['protection']['config']['first_exit_pct'] = original + 1
                return state
            def back(state):
                state['protection']['config']['first_exit_pct'] = original
                return state
            await runtime.owner.mutate('synthetic-policy-A-to-B', changed)
            await runtime.owner.mutate('synthetic-policy-B-to-A', back)
            assert runtime.owner.version == seal.committed_version + 2
            assert (await store.load())[1]['protection']['config']['first_exit_pct'] == original
            # The known limitation remains visible; no global counter is substituted.
            result = await source.complete(ticket, 'success', {'regime': 'bull'})
            assert result.status == 'accepted'
            assert not runtime.trading_ready
        finally:
            await runtime.shutdown()
            await store.close()
    asyncio.run(scenario())
