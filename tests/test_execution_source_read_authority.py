"""Post-completion source-read authority regressions using the real owner and SQLite."""
import asyncio
from dataclasses import FrozenInstanceError
import json
from decimal import Decimal

import pytest

from src.core.engine import UnifiedEngine
from src.core.types import TradingConfig
from src.execution.safety.risk_sources import RiskSourceCoordinator
from src.execution.safety.runtime import KRExecutionRuntime
from src.execution.safety.store import ExecutionStateStore
from src.strategies.exit_manager import ExitManager
from test_execution_runtime import NOW, setup


async def fixture(tmp_path, *, reducer=None):
    engine, exits, store, runtime = await setup(tmp_path, account_scope='scope')
    return store, runtime, RiskSourceCoordinator(runtime, completion_reducer=reducer)


async def accepted(source, ticket, payload=None):
    """Use explicit synthetic intraday provenance so snapshot reaches authority checks."""
    payload = {'level': 'normal'} if payload is None else payload
    if ticket.kind == 'intraday_5m':
        return await source.complete(
            ticket, 'success', payload, source='synthetic-index',
            source_event_id=ticket.operation_id, received_at=NOW,
            market_as_of=NOW, classified_at=NOW,
        )
    return await source.complete(ticket, 'success', payload)


async def sealed_intraday(source, *, operation='intraday', read_lanes=('index_trend',), dependencies=None):
    ticket = await source.begin(operation, 'intraday_5m', dependencies=dependencies or {}, require_seal=True)
    await source.seal(ticket, source_lanes=read_lanes, inputs={'synthetic': operation})
    receipt = await accepted(source, ticket)
    assert receipt.status == 'accepted'
    return ticket, receipt


def test_completed_intraday_snapshot_loses_success_when_declared_source_is_replaced(tmp_path):
    """Would fail if snapshot forgot a sealed source read after terminal acceptance."""
    async def scenario():
        store, runtime, source = await fixture(tmp_path)
        try:
            trend = await source.begin('trend-original', 'index_trend')
            await accepted(source, trend, {'regime': 'bull'})
            _, receipt = await sealed_intraday(source)
            assert source.snapshot().observation_status == 'success'

            changed = await source.begin('trend-replaced', 'index_trend')
            await source.complete(changed, 'failed')

            # History remains an accepted fact, but it is no longer current authority.
            assert runtime.owner.state['risk_sources']['records']['intraday']['terminal']['receipt']['status'] == 'accepted'
            assert receipt.status == 'accepted'
            assert source.snapshot().observation_status != 'success'
        finally:
            await runtime.shutdown()
            await store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('outcome', ['failed', 'missing', 'cancelled'])
def test_read_source_preserves_failure_fact_and_detaches_envelope(tmp_path, outcome):
    """실패를 성공으로 승격하거나 caller alias로 원 사실을 바꾸면 실패한다."""
    async def scenario():
        store, runtime, source = await fixture(tmp_path)
        try:
            assert source.read_source('vix_regime').authority_status == 'missing'
            ticket = await source.begin('failure', 'vix_regime')
            assert source.read_source('vix_regime').authority_status == 'pending'
            payload = {'values': [None, 0]}
            receipt = await source.complete(ticket, outcome, payload)
            read = source.read_source('vix_regime', expected_version=receipt.committed_version)
            assert (read.authority_status, read.terminal_status) == ('current', outcome)
            assert read.operation_id == 'failure'
            payload['values'].append(99)
            decoded = json.loads(read.envelope_json)
            decoded['payload']['values'].append(88)
            assert json.loads(source.read_source('vix_regime').envelope_json)['payload'] == {'values': [None, 0]}
            with pytest.raises(FrozenInstanceError):
                read.reason = 'forged'
            assert source.read_source('vix_regime', expected_version=receipt.committed_version + 1).authority_status == 'stale'
            for bad in (True, 0, -1, '1'):
                with pytest.raises(ValueError):
                    source.read_source('vix_regime', expected_version=bad)
            with pytest.raises(ValueError):
                source.read_source('unknown')
        finally:
            await runtime.shutdown()
            await store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('change', ['begin', 'outcome_conflict', 'reseal_conflict'])
def test_observed_only_chain_loses_authority_without_rewriting_receipts(tmp_path, change):
    """hard edge 없는 observed 연쇄도 leaf 변경을 전파해야 한다."""
    async def scenario():
        store, runtime, source = await fixture(tmp_path)
        try:
            leaf = await source.begin('leaf', 'index_trend', require_seal=True)
            await source.seal(leaf, inputs={'original': True})
            await accepted(source, leaf)
            model = await source.begin('model', 'llm_regime', require_seal=True)
            await source.seal(model, source_lanes=('index_trend',), inputs={})
            await accepted(source, model)
            _, receipt = await sealed_intraday(source, read_lanes=('llm_regime',))
            assert source.snapshot().observation_status == 'success'
            if change == 'begin':
                await source.begin('new-leaf', 'index_trend')
            elif change == 'outcome_conflict':
                await source.complete(leaf, 'failed')
            else:
                await source.seal(leaf, inputs={'different': True})
            assert source.snapshot().observation_status != 'success'
            assert source.read_source('intraday').authority_status == 'stale'
            assert runtime.owner.state['risk_sources']['records']['intraday']['terminal']['receipt']['status'] == 'accepted'
            with pytest.raises(ValueError, match='dependency'):
                await source.begin('dependent', 'llm_morning_diagnosis', dependencies={'intraday': receipt.committed_version})
        finally:
            await runtime.shutdown()
            await store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('fact', ['pending', 'failed', 'missing', 'cancelled', 'conflict', 'seal_conflict'])
def test_unchanged_optional_facts_are_leaves_and_terminal_change_invalidates(tmp_path, fact):
    """optional 관측을 accepted-only hard edge로 바꾸는 회귀를 잡는다."""
    async def scenario():
        store, runtime, source = await fixture(tmp_path)
        try:
            leaf = await source.begin('optional', 'vix_regime', require_seal=True)
            await source.seal(leaf, inputs={})
            if fact != 'pending':
                await source.complete(leaf, 'success' if 'conflict' in fact else fact)
            if fact == 'conflict':
                await source.complete(leaf, 'failed')
            elif fact == 'seal_conflict':
                await source.seal(leaf, inputs={'changed': True})
            _, receipt = await sealed_intraday(source, read_lanes=('vix_regime',))
            assert source.snapshot().observation_status == 'success'
            assert source.read_source('intraday').authority_status == 'current'
            await source.begin('optional-reader', 'llm_regime', dependencies={'intraday': receipt.committed_version})
            if fact == 'pending':
                await source.complete(leaf, 'failed')
            else:
                await source.begin('new-optional', 'vix_regime')
            assert source.snapshot().observation_status != 'success'
        finally:
            await runtime.shutdown()
            await store.close()
    asyncio.run(scenario())


async def cold_source(store, runtime, *, clock=lambda: NOW, unclean=False):
    from src.execution.safety.application import ApplicationBlocked
    if unclean:
        with pytest.raises(ApplicationBlocked, match='command_.*drain_failed'):
            await runtime.shutdown()
    else:
        await runtime.shutdown()
    path = store.path
    await store.close()
    engine = UnifiedEngine(TradingConfig(initial_capital=Decimal('2000000')))
    exits = ExitManager(persist=False, clock=clock)
    store = ExecutionStateStore(path)
    runtime = KRExecutionRuntime(store, engine, exits, account_scope='scope', clock=clock)
    await runtime.restore()
    runtime.attach()
    return store, runtime, RiskSourceCoordinator(runtime)


@pytest.mark.parametrize('leaf_fact', ['absent', 'failed', 'accepted_chain', 'unrelated'])
def test_cancelled_first_await_keeps_shared_selected_closure_until_durable_drain(tmp_path, monkeypatch, leaf_fact):
    """caller 취소가 selected/전이 lane의 predurable 장벽을 지우면 실패한다."""
    async def scenario():
        store, runtime, source = await fixture(tmp_path)
        entered, release = asyncio.Event(), asyncio.Event()
        waiter = None
        try:
            if leaf_fact in {'failed', 'accepted_chain'}:
                leaf = await source.begin('leaf', 'expert_regime')
                await source.complete(leaf, 'failed' if leaf_fact == 'failed' else 'success')
            if leaf_fact == 'accepted_chain':
                model = await source.begin('model', 'llm_regime', require_seal=True)
                await source.seal(model, source_lanes=('expert_regime',), inputs={})
                await accepted(source, model)
            _, receipt = await sealed_intraday(source, read_lanes=(
                'llm_regime' if leaf_fact == 'accepted_chain' else 'expert_regime',))
            original = store.lookup_commit
            async def hold(command):
                entered.set()
                await release.wait()
                return await original(command)
            monkeypatch.setattr(store, 'lookup_commit', hold)
            lane = 'vix_regime' if leaf_fact == 'unrelated' else 'expert_regime'
            waiter = asyncio.create_task(RiskSourceCoordinator(runtime).begin('next-leaf', lane))
            await asyncio.wait_for(entered.wait(), 2)
            assert runtime.owner.healthy
            assert 'next-leaf' not in runtime.owner.state['risk_sources']['records']
            expected = 'current' if leaf_fact == 'unrelated' else 'pending'
            assert source.read_source('intraday').authority_status == expected
            if leaf_fact != 'unrelated':
                with pytest.raises(ValueError, match='dependency'):
                    await source.begin('blocked-dependent', 'llm_morning_diagnosis',
                                       dependencies={'intraday': receipt.committed_version})
                assert runtime.owner.healthy and not runtime.health()['command_results_failed']
            waiter.cancel()
            with pytest.raises(asyncio.CancelledError):
                await waiter
            assert source.read_source('intraday').authority_status == expected
            release.set()
            await asyncio.gather(*tuple(runtime._command_result_tasks))
            assert not runtime._risk_source_pending
            assert source.read_source('intraday').authority_status == ('current' if leaf_fact == 'unrelated' else 'stale')
        finally:
            release.set()
            if waiter:
                await asyncio.gather(waiter, return_exceptions=True)
            await runtime.shutdown()
            await store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('fault', ['sql_before', 'ack_after', 'publish_after'])
def test_source_begin_failure_cold_restore_uses_only_durable_facts(tmp_path, monkeypatch, fault):
    """SQL 실패/응답 유실을 지워 옛 source를 승인하거나 phantom pending을 남기면 실패한다."""
    import sqlite3
    from src.execution.safety.store import StoreError
    from src.execution.safety.application import ApplicationBlocked
    async def scenario():
        store, runtime, source = await fixture(tmp_path)
        try:
            await sealed_intraday(source, read_lanes=('expert_regime',))
            if fault == 'sql_before':
                with sqlite3.connect(store.path) as db:
                    db.execute("CREATE TRIGGER fail_begin BEFORE UPDATE ON checkpoint BEGIN SELECT RAISE(ABORT, 'source failure'); END")
            elif fault == 'ack_after':
                original = store.commit
                async def lost(*args, **kwargs):
                    await original(*args, **kwargs)
                    raise OSError('synthetic_ack_loss')
                monkeypatch.setattr(store, 'commit', lost)
            else:
                def lost_publish(*args):
                    raise OSError('synthetic_publish_failure')
                monkeypatch.setattr(runtime.owner, 'publisher', lost_publish)
            with pytest.raises((StoreError, OSError, ApplicationBlocked)):
                await source.begin('new-expert', 'expert_regime')
            assert source.read_source('intraday').authority_status == 'unavailable'
            assert not runtime.owner.healthy
            if fault == 'sql_before':
                with sqlite3.connect(store.path) as db:
                    db.execute('DROP TRIGGER fail_begin')
            store, runtime, source = await cold_source(store, runtime, unclean=True)
            assert source.read_source('intraday').authority_status == ('current' if fault == 'sql_before' else 'stale')
            assert ('new-expert' in runtime.owner.state['risk_sources']['records']) == (fault != 'sql_before')
            assert not runtime._risk_source_pending
        finally:
            await runtime.shutdown()
            await store.close()
    asyncio.run(scenario())


def test_observed_dependent_completion_rechecks_transitive_source_before_hook(tmp_path):
    """직접 seal row가 같아도 accepted observed의 하위 source 변화는 hook을 막는다."""
    async def scenario():
        calls = []
        def hook(state, ticket, envelope, version):
            calls.append(ticket.operation_id)
            return state
        store, runtime, source = await fixture(tmp_path, reducer=hook)
        try:
            leaf = await source.begin('leaf', 'index_trend')
            await source.complete(leaf, 'success')
            model = await source.begin('model', 'llm_regime', require_seal=True)
            await source.seal(model, source_lanes=('index_trend',), inputs={})
            await source.complete(model, 'success')
            consumer = await source.begin('consumer', 'llm_morning_diagnosis', require_seal=True)
            await source.seal(consumer, source_lanes=('llm_regime',), inputs={})
            calls.clear()
            await source.begin('changed-leaf', 'index_trend')
            receipt = await source.complete(consumer, 'success')
            assert (receipt.status, receipt.reason) == ('stale', 'input_seal')
            assert calls == []
        finally:
            await runtime.shutdown()
            await store.close()
    asyncio.run(scenario())


def test_day_fence_first_await_cancellation_and_cold_restore_keep_reads_unavailable(tmp_path, monkeypatch):
    """day caller 취소/복원이 닫힌 fence를 열어 기존 source를 승인하면 실패한다."""
    from test_execution_day_recovery import day_setup, prepare
    async def scenario():
        _, _, store, runtime, times = await day_setup(tmp_path)
        source = RiskSourceCoordinator(runtime)
        reached, release = asyncio.Event(), asyncio.Event()
        waiter = None
        try:
            await sealed_intraday(source, read_lanes=())
            assert source.read_source('intraday').authority_status == 'current'
            original = store.lookup_commit
            async def held(command):
                reached.set()
                await release.wait()
                return await original(command)
            monkeypatch.setattr(store, 'lookup_commit', held)
            waiter = asyncio.create_task(prepare(runtime, times))
            await asyncio.wait_for(reached.wait(), 2)
            assert 'day_transition' not in runtime.owner.state
            assert runtime.day_admission_closed
            assert source.read_source('intraday').authority_status == 'unavailable'
            waiter.cancel()
            with pytest.raises(asyncio.CancelledError):
                await waiter
            assert source.snapshot().observation_status == 'missing'
            release.set()
            await asyncio.gather(*tuple(runtime._day_tasks))
            store, runtime, source = await cold_source(store, runtime, clock=lambda: times[0])
            assert runtime.owner.state['day_transition']['phase'] == 'PREPARED'
            assert source.read_source('intraday').authority_status == 'unavailable'
        finally:
            release.set()
            if waiter:
                await asyncio.gather(waiter, return_exceptions=True)
            await runtime.shutdown()
            await store.close()
    asyncio.run(scenario())


def test_public_closing_read_blocks_but_admitted_schema2_completion_drains(tmp_path):
    """public read의 closing guard가 이미 접수된 completion을 금지하면 실패한다."""
    async def scenario():
        store, runtime, source = await fixture(tmp_path)
        reached, release = asyncio.Event(), asyncio.Event()
        caller = closing = None
        try:
            await runtime.owner.register_policy_generations('register', ('protection.config',))
            async def fetch():
                with runtime.command_scope() as token:
                    ticket = await source.begin('fetch', 'llm_regime', require_seal=True)
                    await source.seal(ticket, versioned_policy_reads=('protection.config',), inputs={}, scope_token=token)
                    reached.set()
                    await release.wait()
                    return await source.complete(ticket, 'success', scope_token=token)
            caller = asyncio.create_task(fetch())
            await asyncio.wait_for(reached.wait(), 2)
            closing = asyncio.create_task(runtime.shutdown())
            await asyncio.sleep(0)  # 종료 task가 admission을 닫을 한 event-loop turn
            assert runtime._closing and not closing.done()
            assert source.read_source('llm_regime').authority_status == 'unavailable'
            release.set()
            assert (await caller).status == 'accepted'
            await closing
            assert not runtime.health()['command_results_failed']
        finally:
            release.set()
            if caller:
                await asyncio.gather(caller, return_exceptions=True)
            if closing:
                await asyncio.gather(closing, return_exceptions=True)
            await runtime.shutdown()
            await store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('cancel_caller', [False, True])
@pytest.mark.parametrize('consumer_kind', ['llm_morning_diagnosis', 'index_trend'])
def test_dependency_change_while_admission_waits_rejects_without_poisoning_owner(tmp_path, monkeypatch, cancel_caller, consumer_kind):
    """정상 pending 경합의 reducer 거부를 SQL/게시 실패로 확대하면 실패한다."""
    from src.execution.safety.application import ApplicationBlocked
    async def scenario():
        store, runtime, source = await fixture(tmp_path)
        entered, started, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        waiting = changing = None
        consumer_commit = []
        try:
            leaf = await source.begin('leaf', 'index_trend')
            await accepted(source, leaf)
            _, receipt = await sealed_intraday(source)
            original = store.lookup_commit
            async def held(command):
                if not entered.is_set():
                    consumer_commit.append(command)
                    entered.set()
                    await release.wait()
                return await original(command)
            monkeypatch.setattr(store, 'lookup_commit', held)
            waiting = asyncio.create_task(source.begin('consumer', consumer_kind,
                dependencies={'intraday': receipt.committed_version}))
            await asyncio.wait_for(entered.wait(), 2)
            async def change():
                started.set()
                return await RiskSourceCoordinator(runtime).begin('next-leaf', 'index_trend')
            changing = asyncio.create_task(change())
            await asyncio.wait_for(started.wait(), 2)
            assert source.read_source('intraday').authority_status == 'pending'
            if cancel_caller:
                waiting.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await waiting
                assert 'index_trend' in runtime._risk_source_pending.values()
            release.set()
            if not cancel_caller:
                with pytest.raises(ValueError, match='dependency'):
                    await waiting
            results = await asyncio.gather(changing, return_exceptions=True)
            assert not isinstance(results[0], BaseException)
            assert 'consumer' not in runtime.owner.state['risk_sources']['records']
            assert await original(consumer_commit[0]) is None
            assert runtime.owner.healthy
            assert not runtime.health()['command_results_failed']
            await accepted(source, results[0])
            _, fresh = await sealed_intraday(source, operation='new-intraday')
            retry = await source.begin('consumer', consumer_kind,
                dependencies={'intraday': fresh.committed_version})
            assert retry.operation_id == 'consumer'
            await runtime.shutdown()
        finally:
            release.set()
            for task in (waiting, changing):
                if task:
                    await asyncio.gather(task, return_exceptions=True)
            try:
                await runtime.shutdown()
            except ApplicationBlocked:
                pass  # RED 본문 assertion을 cleanup 실패로 가리지 않는다.
            await store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('guard', ['engine_version', 'unhealthy', 'generation', 'fence', 'scope'])
def test_public_read_runtime_context_cannot_grant_current_authority(tmp_path, monkeypatch, guard):
    """원 source가 같아도 runtime 게시/일자 context 불일치는 현재 권한이 아니다."""
    async def scenario():
        store, runtime, source = await fixture(tmp_path)
        try:
            await sealed_intraday(source, read_lanes=())
            assert source.read_source('intraday').authority_status == 'current'
            with monkeypatch.context() as patch:
                if guard == 'engine_version':
                    patch.setattr(runtime.engine, '_execution_version', runtime.owner.version + 1)
                elif guard == 'unhealthy':
                    patch.setattr(runtime.owner, '_healthy', False)
                elif guard == 'generation':
                    patch.setattr(runtime, '_day_generation', runtime._day_generation + 1)
                elif guard == 'fence':
                    patch.setattr(runtime, '_day_fence_id', 'different-fence')
                else:
                    patch.setattr(runtime, 'account_scope', 'different-scope')
                assert source.read_source('intraday').authority_status != 'current'
                assert source.snapshot().observation_status != 'success'
            assert source.read_source('intraday').authority_status == 'current'
        finally:
            await runtime.shutdown()
            await store.close()
    asyncio.run(scenario())


def test_graph_cycle_fails_closed_without_mutating_actual_owner_snapshot(tmp_path):
    """현재 writer의 version DAG 밖인 손상 그래프도 evaluator 재귀를 무한 반복하지 않는다."""
    from src.execution.safety.risk_sources import _SourceAuthority
    async def scenario():
        store, runtime, source = await fixture(tmp_path)
        try:
            ticket = await source.begin('leaf', 'index_trend')
            receipt = await source.complete(ticket, 'success')
            detached = runtime.owner.state
            detached['risk_sources']['records']['leaf']['ticket']['dependencies'] = [['index_trend', receipt.committed_version]]
            status, _, lanes = _SourceAuthority(detached, NOW.date().isoformat()).dependency('index_trend', receipt.committed_version)
            assert status == 'stale' and lanes == frozenset({'index_trend'})
            assert runtime.owner.state['risk_sources']['records']['leaf']['ticket']['dependencies'] == []
            assert source.read_source('index_trend').authority_status == 'current'
        finally:
            await runtime.shutdown()
            await store.close()
    asyncio.run(scenario())


def test_same_lane_hard_dependency_keeps_legacy_admission_but_completion_is_stale(tmp_path):
    """자기 begin token을 경쟁 갱신으로 오인하여 기존 합법 접수를 거부하면 실패한다."""
    async def scenario():
        store, runtime, source = await fixture(tmp_path)
        try:
            old = await source.begin('old', 'index_trend')
            receipt = await source.complete(old, 'success')
            new = await source.begin('new', 'index_trend', dependencies={'index_trend': receipt.committed_version})
            assert new.operation_id == 'new'
            result = await source.complete(new, 'success')
            assert (result.status, result.reason) == ('stale', 'dependency')
            assert runtime.owner.state['risk_sources']['records']['old']['terminal']['receipt']['status'] == 'accepted'
            assert runtime.owner.healthy and not runtime.health()['command_results_failed']
        finally:
            await runtime.shutdown()
            await store.close()
    asyncio.run(scenario())


def test_completion_waiting_in_lookup_sees_other_facade_predurable_pending_before_hook(tmp_path, monkeypatch):
    """완료 reducer가 durable row만 보면 직전 await 동안 접수된 갱신을 놓친다."""
    async def scenario():
        calls = []
        def hook(state, ticket, envelope, version):
            calls.append(ticket.operation_id)
            return state
        store, runtime, source = await fixture(tmp_path, reducer=hook)
        entered, started, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        completing = changing = None
        try:
            consumer = await source.begin('consumer', 'llm_regime', require_seal=True)
            await source.seal(consumer, source_lanes=('expert_regime',), inputs={})
            original = store.lookup_commit
            async def held(command):
                if not entered.is_set():
                    entered.set()
                    await release.wait()
                return await original(command)
            monkeypatch.setattr(store, 'lookup_commit', held)
            completing = asyncio.create_task(source.complete(consumer, 'success'))
            await asyncio.wait_for(entered.wait(), 2)
            async def change():
                started.set()
                return await RiskSourceCoordinator(runtime).begin('expert', 'expert_regime')
            changing = asyncio.create_task(change())
            await asyncio.wait_for(started.wait(), 2)
            assert 'expert' not in runtime.owner.state['risk_sources']['records']
            release.set()
            receipt = await completing
            await changing
            assert (receipt.status, receipt.reason) == ('stale', 'input_seal')
            assert calls == []
            assert runtime.owner.healthy and not runtime.health()['command_results_failed']
        finally:
            release.set()
            for task in (completing, changing):
                if task:
                    await asyncio.gather(task, return_exceptions=True)
            await runtime.shutdown()
            await store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('conflict', ['outcome', 'seal'])
def test_actual_day_roll_retains_yesterday_identity_but_later_conflict_invalidates(tmp_path, conflict):
    """전일 retained를 오늘 latest/의존 freshness로 재승인하면 실패한다."""
    from test_execution_day_recovery import day_setup, prepare, valued
    async def scenario():
        _, _, store, runtime, times = await day_setup(tmp_path)
        source = RiskSourceCoordinator(runtime)
        try:
            old_trend = await source.begin('yesterday-trend', 'index_trend')
            old_trend_receipt = await source.complete(old_trend, 'success')
            old = await source.begin('yesterday-vix', 'vix_regime', require_seal=True,
                                     dependencies={'index_trend': old_trend_receipt.committed_version})
            await source.seal(old, source_lanes=('index_trend',), inputs={})
            await source.complete(old, 'success', {'value': 12.0})
            original_day = old.business_day
            fence = await prepare(runtime, times)
            assert source.read_source('vix_regime').authority_status == 'unavailable'
            evidence = await valued(runtime, fence, times)
            assert (await runtime.rollover_day('roll', expected_version=runtime.owner.version,
                fence_id=fence.fence_id, valuation_evidence_id=evidence)).status == 'APPLIED'
            assert (await runtime.resume_after_rollover('resume', expected_version=runtime.owner.version,
                fence_id=fence.fence_id)).status == 'APPLIED'
            assert source.read_source('vix_regime').authority_status == 'stale'
            changed = await source.begin('today-trend', 'index_trend')
            await source.complete(changed, 'success')
            today = await source.begin('today-vix', 'vix_regime')
            await source.complete(today, 'failed')
            await runtime.owner.register_policy_generations('after-roll', ('protection.config',))
            model = await source.begin('retained-model', 'llm_regime', require_seal=True)
            seal = await source.seal(model, source_lanes=('vix_regime',), retained_sources=('yesterday-vix',),
                versioned_policy_reads=('protection.config',), inputs={'vix': None})
            assert (await source.complete(model, 'success')).status == 'accepted'
            retained = json.loads(seal.reads_json)['retained']['yesterday-vix']
            assert retained['ticket']['business_day'] == original_day != model.business_day
            assert retained['terminal']['envelope']['payload'] == {'value': 12.0}
            assert source.read_source('llm_regime').authority_status == 'current'
            store, runtime, source = await cold_source(store, runtime, clock=lambda: times[0])
            assert source.read_source('llm_regime').authority_status == 'current'
            if conflict == 'outcome':
                await source.complete(old, 'failed')
            else:
                await source.seal(old, inputs={'different': True})
            assert source.read_source('llm_regime').authority_status == 'stale'
            assert runtime.owner.state['risk_sources']['records']['retained-model']['terminal']['receipt']['status'] == 'accepted'
            store, runtime, source = await cold_source(store, runtime, clock=lambda: times[0])
            assert source.read_source('llm_regime').authority_status == 'stale'
        finally:
            await runtime.shutdown()
            await store.close()
    asyncio.run(scenario())


def test_schema2_own_hook_and_actual_fill_ack_keep_current_authority_after_cold_restore(tmp_path):
    """완료 전 정책을 완료 후 다시 비교하거나 전역 version으로 stale시키면 실패한다."""
    from src.execution.safety.journal_delivery import OutboxDispatcher, PostgresExecutionJournal
    from test_execution_journal_delivery import Pool
    from test_execution_runtime import opened, observed, queued
    async def scenario():
        def hook(state, ticket, envelope, version):
            state['protection']['config']['first_exit_pct'] = 12.0
            return state
        store, runtime, source = await fixture(tmp_path, reducer=hook)
        try:
            await runtime.owner.register_policy_generations('register', ('protection.config',))
            ticket = await source.begin('policy-model', 'llm_regime', require_seal=True)
            await source.seal(ticket, versioned_policy_reads=('protection.config',), inputs={})
            receipt = await accepted(source, ticket)
            assert source.read_source('llm_regime').authority_status == 'current'
            ref = await opened(runtime, 'B1')
            await queued(runtime.engine, await observed(runtime, ref, 40, '400000'))
            assert (await OutboxDispatcher(runtime.owner, PostgresExecutionJournal(Pool())).drain()).delivered == 1
            assert source.read_source('llm_regime').authority_status == 'current'
            store, runtime, source = await cold_source(store, runtime)
            assert source.read_source('llm_regime').authority_status == 'current'
            await source.begin('consumer', 'llm_morning_diagnosis', dependencies={'llm_regime': receipt.committed_version})
            assert not runtime.trading_ready
        finally:
            await runtime.shutdown()
            await store.close()
    asyncio.run(scenario())


def test_new_hard_dependency_rejects_completed_intraday_with_replaced_declared_source(tmp_path):
    """Would fail if begin checks only a dependency's terminal version, not its sealed reads."""
    async def scenario():
        store, runtime, source = await fixture(tmp_path)
        try:
            trend = await source.begin('trend-original', 'index_trend')
            await accepted(source, trend, {'regime': 'bull'})
            _, receipt = await sealed_intraday(source)
            changed = await source.begin('trend-replaced', 'index_trend')
            await source.complete(changed, 'failed')

            with pytest.raises(ValueError, match='dependency'):
                await source.begin('new-model', 'llm_regime',
                                   dependencies={'intraday': receipt.committed_version})
        finally:
            await runtime.shutdown()
            await store.close()
    asyncio.run(scenario())


def test_transitive_sealed_and_hard_graph_blocks_snapshot_and_new_consumer(tmp_path):
    """Would fail if authority traversal stops at a hard dependency instead of its sealed reads."""
    async def scenario():
        store, runtime, source = await fixture(tmp_path)
        try:
            trend = await source.begin('trend-original', 'index_trend')
            await accepted(source, trend, {'regime': 'bull'})
            model = await source.begin('sealed-model', 'llm_regime', require_seal=True)
            await source.seal(model, source_lanes=('index_trend',), inputs={'model': 'synthetic'})
            model_receipt = await accepted(source, model, {'regime': 'neutral'})
            _, intraday_receipt = await sealed_intraday(
                source, operation='intraday-through-model', read_lanes=('llm_regime',),
                dependencies={'llm_regime': model_receipt.committed_version},
            )
            assert source.snapshot().observation_status == 'success'

            changed = await source.begin('trend-replaced', 'index_trend')
            await source.complete(changed, 'failed')

            assert source.snapshot().observation_status != 'success'
            with pytest.raises(ValueError, match='dependency'):
                await source.begin('after-transitive-change', 'llm_morning_diagnosis',
                                   dependencies={'intraday': intraday_receipt.committed_version})
        finally:
            await runtime.shutdown()
            await store.close()
    asyncio.run(scenario())


def test_first_await_pending_declared_source_masks_completed_intraday_from_another_facade(tmp_path, monkeypatch):
    """Would fail if the shared first-await barrier ignores sealed source lanes."""
    async def scenario():
        store, runtime, source = await fixture(tmp_path)
        reached, release = asyncio.Event(), asyncio.Event()
        waiter = None
        try:
            trend = await source.begin('trend-original', 'index_trend')
            await accepted(source, trend, {'regime': 'bull'})
            await sealed_intraday(source)
            facade = RiskSourceCoordinator(runtime)
            original = store.lookup_commit

            async def held(commit_id):
                reached.set()
                await release.wait()
                return await original(commit_id)

            monkeypatch.setattr(store, 'lookup_commit', held)
            waiter = asyncio.create_task(facade.begin('trend-pending', 'index_trend'))
            await asyncio.wait_for(reached.wait(), 2)
            assert runtime.owner.healthy
            assert len(runtime.owner.state['risk_sources']['records']) == 2
            assert source.snapshot().observation_status == 'pending'
        finally:
            release.set()
            if waiter is not None:
                await asyncio.gather(waiter, return_exceptions=True)
            await runtime.shutdown()
            await store.close()
    asyncio.run(scenario())


def test_absent_declared_source_becoming_failed_invalidates_completed_intraday(tmp_path):
    """Would fail if an absent sealed read is omitted from post-completion authority checks."""
    async def scenario():
        store, runtime, source = await fixture(tmp_path)
        try:
            _, receipt = await sealed_intraday(source, read_lanes=('expert_regime',))
            assert source.snapshot().observation_status == 'success'

            expert = await source.begin('expert-now-failed', 'expert_regime')
            await source.complete(expert, 'failed')

            assert receipt.status == 'accepted'
            assert source.snapshot().observation_status != 'success'
        finally:
            await runtime.shutdown()
            await store.close()
    asyncio.run(scenario())


def test_transitive_change_blocks_already_admitted_hard_dependent_completion_hook(tmp_path):
    """Would fail if complete rechecks terminal dependency versions but not source-read authority."""
    async def scenario():
        hook_calls = []

        def hook(state, ticket, envelope, version):
            hook_calls.append(ticket.operation_id)
            return state

        store, runtime, source = await fixture(tmp_path, reducer=hook)
        try:
            trend = await source.begin('trend-original', 'index_trend')
            await accepted(source, trend, {'regime': 'bull'})
            model = await source.begin('sealed-model', 'llm_regime', require_seal=True)
            await source.seal(model, source_lanes=('index_trend',), inputs={'model': 'synthetic'})
            model_receipt = await accepted(source, model, {'regime': 'neutral'})
            _, intraday_receipt = await sealed_intraday(
                source, operation='intraday-through-model', read_lanes=('llm_regime',),
                dependencies={'llm_regime': model_receipt.committed_version},
            )
            followup = await source.begin('already-admitted-followup', 'llm_morning_diagnosis',
                                          dependencies={'intraday': intraday_receipt.committed_version})
            hook_calls.clear()

            changed = await source.begin('trend-replaced', 'index_trend')
            await source.complete(changed, 'failed')
            result = await accepted(source, followup, {'regime': 'bear'})

            assert (result.status, result.reason) == ('stale', 'dependency')
            assert hook_calls == []
        finally:
            await runtime.shutdown()
            await store.close()
    asyncio.run(scenario())


def test_unchanged_failed_vix_and_explicit_retained_vix_keep_sealed_consumer_current(tmp_path):
    """Control: optional failed/absent facts are authority facts, not implicit success requirements."""
    async def scenario():
        store, runtime, source = await fixture(tmp_path)
        try:
            old = await source.begin('vix-old', 'vix_regime')
            await accepted(source, old, {'value': 12.0})
            latest = await source.begin('vix-failed', 'vix_regime')
            await source.complete(latest, 'failed')
            model = await source.begin('optional-vix-model', 'llm_regime', require_seal=True)
            await source.seal(model, source_lanes=('vix_regime', 'expert_regime'),
                              retained_sources=('vix-old',), inputs={'vix': None})
            model_receipt = await accepted(source, model, {'regime': 'neutral'})

            consumer = await source.begin('optional-vix-consumer', 'llm_morning_diagnosis',
                                          dependencies={'llm_regime': model_receipt.committed_version})
            assert consumer.operation_id == 'optional-vix-consumer'
            assert runtime.owner.state['risk_sources']['records']['vix-failed']['terminal']['receipt']['status'] == 'failed'
            retained = runtime.owner.state['risk_input_seals']['records']['optional-vix-model']['original']['reads']['retained']['vix-old']
            assert retained['ticket']['business_day'] == NOW.date().isoformat()
            assert retained['terminal']['envelope']['payload'] == {'value': 12.0}
            assert runtime.owner.state['risk_input_seals']['records']['optional-vix-model']['original']['reads']['sources']['expert_regime'] is None
        finally:
            await runtime.shutdown()
            await store.close()
    asyncio.run(scenario())


def test_completion_hook_may_change_its_own_policy_read_without_losing_source_authority(tmp_path):
    """Control: post-completion source authority must not reuse the pre-hook policy comparison."""
    async def scenario():
        def hook(state, ticket, envelope, version):
            state['protection']['config']['first_exit_pct'] = 12.0
            return state

        store, runtime, source = await fixture(tmp_path, reducer=hook)
        try:
            ticket = await source.begin('policy-owning-model', 'llm_regime', require_seal=True)
            await source.seal(ticket, policy_reads=('protection.config',), inputs={'policy': 'before'})
            receipt = await accepted(source, ticket, {'regime': 'bull'})
            assert receipt.status == 'accepted'
            assert runtime.owner.state['protection']['config']['first_exit_pct'] == 12.0
            assert (await source.complete(ticket, 'success', {'regime': 'bull'})) == receipt
        finally:
            await runtime.shutdown()
            await store.close()
    asyncio.run(scenario())


def test_cold_restore_preserves_accepted_history_but_rejects_changed_declared_authority(tmp_path):
    """Would fail if invalidation lives only in the in-memory pending map or rewrites history."""
    async def scenario():
        store, runtime, source = await fixture(tmp_path)
        replacement = None
        restored_store = None
        try:
            trend = await source.begin('trend-original', 'index_trend')
            await accepted(source, trend, {'regime': 'bull'})
            _, receipt = await sealed_intraday(source)
            changed = await source.begin('trend-replaced', 'index_trend')
            await source.complete(changed, 'failed')
            path = store.path
            await runtime.shutdown()
            await store.close()

            engine = UnifiedEngine(TradingConfig(initial_capital=Decimal('2000000')))
            exits = ExitManager(persist=False, clock=lambda: NOW)
            restored_store = ExecutionStateStore(path)
            replacement = KRExecutionRuntime(restored_store, engine, exits, account_scope='scope', clock=lambda: NOW)
            await replacement.restore()
            replacement.attach()
            restored = RiskSourceCoordinator(replacement)
            history = replacement.owner.state['risk_sources']['records']['intraday']['terminal']['receipt']
            assert (history['status'], history['committed_version']) == ('accepted', receipt.committed_version)
            assert restored.snapshot().observation_status != 'success'
        finally:
            if replacement is not None:
                await replacement.shutdown()
                await restored_store.close()
            elif not store._closed:
                await runtime.shutdown()
                await store.close()
    asyncio.run(scenario())
