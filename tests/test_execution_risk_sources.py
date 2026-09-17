"""위험 source 수명: 실제 owner/SQLite, 합성 계좌 기준선, 운영 허가 없음."""
import asyncio
from dataclasses import replace
from datetime import timedelta

import pytest

from test_execution_runtime import NOW, setup
from src.execution.safety.application import ApplicationBlocked
from src.execution.safety.protection_recovery import digest


def api():
    import importlib.util
    assert importlib.util.find_spec('src.execution.safety.risk_sources'), 'risk source lifecycle missing'
    from src.execution.safety.risk_sources import RiskSourceCoordinator, validate_risk_sources
    return RiskSourceCoordinator, validate_risk_sources


async def fixture(tmp_path, monkeypatch, clock=None):
    cls, validate = api()
    engine, exits, store, runtime = await setup(tmp_path, account_scope='scope', clock=clock)
    # 임시 predecode 배선: 실제 runtime 게시에 위임한다. 부모의 정식 hook으로 대체 가능.
    publish = runtime.owner.publisher
    def checked(state, version):
        validate(state, version)
        return publish(state, version)
    monkeypatch.setattr(runtime.owner, 'publisher', checked)
    return store, runtime, cls(runtime)


async def success(source, ticket, level='normal', *, market_time=NOW):
    return await source.complete(ticket, 'success', {'level': level}, source='synthetic-index',
                                 source_event_id=ticket.operation_id, received_at=NOW,
                                 market_as_of=market_time, classified_at=NOW)


def test_begin_terminal_restore_masks_prior_success_and_preserves_policy(tmp_path, monkeypatch):
    async def scenario():
        store, runtime, source = await fixture(tmp_path, monkeypatch)
        try:
            initial = runtime.owner.state
            assert source.snapshot().observation_status == 'missing'
            a = await source.begin('a', 'intraday_5m')
            assert source.snapshot().observation_status == 'pending'
            receipt = await success(source, a)
            assert receipt.status == 'accepted'
            assert source.snapshot().level == 'normal'
            b = await source.begin('b', 'noon_index')
            assert b.sequence == a.sequence + 1 and b.lane == a.lane == 'intraday'
            assert source.snapshot().observation_status == 'pending'
            await runtime.restore()
            assert source.snapshot().observation_status == 'pending'
            await source.complete(b, 'failed')
            assert source.snapshot().observation_status == 'failed'
            assert await success(source, a) == receipt
            assert source.snapshot().observation_status == 'failed'
            for key in ('protection', 'risk', 'portfolio', 'outbox'):
                assert runtime.owner.state[key] == initial[key]
            assert runtime.trading_ready is False
        finally:
            await runtime.shutdown()
            await store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('old_outcome', ['success', 'failed', 'missing', 'cancelled'])
@pytest.mark.parametrize('new_outcome', ['success', 'failed'])
def test_superseded_completion_cannot_change_current_source(tmp_path, monkeypatch, old_outcome, new_outcome):
    async def scenario():
        store, runtime, source = await fixture(tmp_path, monkeypatch)
        try:
            a = await source.begin('old', 'intraday_5m')
            b = await source.begin('new', 'noon_index')
            if new_outcome == 'success':
                await success(source, b, 'severe')
            else:
                await source.complete(b, new_outcome)
            before = source.snapshot()
            result = await (success(source, a) if old_outcome == 'success'
                            else source.complete(a, old_outcome))
            assert result.status == 'stale'
            assert source.snapshot() == before
        finally:
            await runtime.shutdown()
            await store.close()
    asyncio.run(scenario())


def test_missing_market_time_does_not_turn_finite_source_into_entry_authority(tmp_path, monkeypatch):
    async def scenario():
        store, runtime, source = await fixture(tmp_path, monkeypatch)
        try:
            ticket = await source.begin('rest', 'intraday_5m')
            receipt = await success(source, ticket, market_time=None)
            assert receipt.status == 'accepted'
            assert source.snapshot().observation_status == 'missing'
            assert source.snapshot().as_of is None and source.snapshot().level is None
        finally:
            await runtime.shutdown()
            await store.close()
    asyncio.run(scenario())


def test_conflicting_duplicate_is_permanently_poisoned(tmp_path, monkeypatch):
    async def scenario():
        store, runtime, source = await fixture(tmp_path, monkeypatch)
        try:
            ticket = await source.begin('same', 'intraday_5m')
            receipt = await success(source, ticket)
            version = runtime.owner.version
            assert await success(source, ticket) == receipt
            assert runtime.owner.version == version
            conflict = await success(source, ticket, 'severe')
            assert conflict.status == 'conflict'
            assert source.snapshot().observation_status == 'conflict'
            assert (await success(source, ticket)).status == 'conflict'
            await runtime.restore()
            assert source.snapshot().observation_status == 'conflict'
        finally:
            await runtime.shutdown()
            await store.close()
    asyncio.run(scenario())


def test_dependency_is_current_source_version_not_execution_counter(tmp_path, monkeypatch):
    async def scenario():
        store, runtime, source = await fixture(tmp_path, monkeypatch)
        try:
            a = await source.begin('index', 'intraday_5m')
            receipt = await success(source, a)
            llm = await source.begin('llm', 'llm_regime', dependencies={'intraday': receipt.committed_version})
            trend = await source.begin('trend', 'index_trend')
            await source.complete(trend, 'success', {'regime': 'bull'})
            assert (await source.complete(llm, 'success', {'regime': 'neutral'})).status == 'accepted'
            llm2 = await source.begin('llm2', 'llm_regime', dependencies={'intraday': receipt.committed_version})
            newer = await source.begin('index2', 'intraday_5m')
            await success(source, newer, 'crash')
            assert (await source.complete(llm2, 'success', {'regime': 'bull'})).status == 'stale'
            with pytest.raises(ValueError, match='dependency'):
                await source.begin('unsupported', 'llm_regime', dependencies={'config': 1})
        finally:
            await runtime.shutdown()
            await store.close()
    asyncio.run(scenario())


def test_first_await_barrier_shared_across_facades_and_independent_lanes(tmp_path, monkeypatch):
    async def scenario():
        store, runtime, source = await fixture(tmp_path, monkeypatch)
        reached, release = asyncio.Event(), asyncio.Event()
        task = None
        try:
            a = await source.begin('first', 'intraday_5m')
            await success(source, a)
            facade = api()[0](runtime)
            original = store.lookup_commit
            async def held(commit_id):
                reached.set()
                await release.wait()
                return await original(commit_id)
            monkeypatch.setattr(store, 'lookup_commit', held)
            task = asyncio.create_task(source.begin('pending', 'noon_index'))
            await asyncio.wait_for(reached.wait(), 2)
            assert runtime.owner.healthy
            assert facade.snapshot().observation_status == 'pending'
            assert len(runtime.owner.state['risk_sources']['records']) == 1
            release.set()
            ticket = await task
            await success(source, ticket)
            await source.begin('unrelated', 'index_trend')
            assert facade.snapshot().observation_status == 'success'
        finally:
            release.set()
            if task:
                await asyncio.gather(task, return_exceptions=True)
            await runtime.shutdown()
            await store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('phase', ['begin', 'complete'])
def test_caller_cancelled_result_is_durable_and_shutdown_drains(tmp_path, monkeypatch, phase):
    async def scenario():
        store, runtime, source = await fixture(tmp_path, monkeypatch)
        reached, release = asyncio.Event(), asyncio.Event()
        task = shutdown = None
        try:
            ticket = await source.begin('initial', 'intraday_5m')
            original = store.commit
            async def held(*args, **kwargs):
                reached.set()
                await release.wait()
                return await original(*args, **kwargs)
            monkeypatch.setattr(store, 'commit', held)
            task = asyncio.create_task(source.begin('next', 'noon_index') if phase == 'begin'
                                       else success(source, ticket))
            await asyncio.wait_for(reached.wait(), 2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            shutdown = asyncio.create_task(runtime.shutdown())
            await asyncio.sleep(0)
            assert not shutdown.done()
            release.set()
            await asyncio.wait_for(shutdown, 2)
            records = runtime.owner.state['risk_sources']['records']
            assert records['next']['terminal'] is None if phase == 'begin' else records['initial']['terminal']['receipt']['status'] == 'accepted'
            assert not runtime._risk_source_pending and not runtime._command_results_failed
        finally:
            release.set()
            await asyncio.gather(*(t for t in (task, shutdown) if t), return_exceptions=True)
            await store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('phase', ['begin', 'complete'])
def test_sql_failure_keeps_source_unknown_and_shutdown_fails(tmp_path, monkeypatch, phase):
    async def scenario():
        store, runtime, source = await fixture(tmp_path, monkeypatch)
        try:
            ticket = await source.begin('initial', 'intraday_5m')
            await success(source, ticket)
            if phase == 'complete':
                ticket = await source.begin('next', 'intraday_5m')
            original = runtime.owner.state
            async def reject(*args, **kwargs):
                raise OSError('synthetic SQL failure')
            monkeypatch.setattr(store, 'commit', reject)
            with pytest.raises(OSError, match='synthetic SQL failure'):
                await (source.begin('rejected', 'intraday_5m') if phase == 'begin' else success(source, ticket))
            assert source.snapshot().observation_status != 'success'
            assert runtime.owner.state == original
            with pytest.raises(ApplicationBlocked, match='drain_failed'):
                await runtime.shutdown()
        finally:
            await store.close()
    asyncio.run(scenario())


def test_old_day_completion_is_stale_and_new_begin_rejected(tmp_path, monkeypatch):
    async def scenario():
        clock = [NOW]
        store, runtime, source = await fixture(tmp_path, monkeypatch, lambda: clock[0])
        try:
            ticket = await source.begin('yesterday', 'intraday_5m')
            clock[0] += timedelta(days=1)
            assert (await success(source, ticket)).status == 'stale'
            with pytest.raises(ApplicationBlocked, match='day_transition'):
                await source.begin('today', 'intraday_5m')
            assert source.snapshot().observation_status == 'missing'
        finally:
            await runtime.shutdown()
            await store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('mutation', ['latest', 'sequence', 'admission', 'digest', 'dependency',
                                      'status', 'future', 'bool_generation', 'phantom_success'])
def test_restore_validator_rejects_corrupted_crosslinks(tmp_path, monkeypatch, mutation):
    async def scenario():
        store, runtime, source = await fixture(tmp_path, monkeypatch)
        try:
            a = await source.begin('a', 'intraday_5m')
            await success(source, a)
            b = await source.begin('b', 'intraday_5m')
            await source.complete(b, 'failed')
            state = runtime.owner.state
            registry = state['risk_sources']
            row = registry['records']['b']
            if mutation == 'latest':
                registry['latest']['intraday'] = 'a'
            elif mutation == 'sequence':
                row['ticket']['sequence'] = 1
            elif mutation == 'admission':
                row['ticket']['admission_version'] = runtime.owner.version + 1
            elif mutation == 'digest':
                row['terminal']['receipt']['outcome_digest'] = '0' * 64
            elif mutation == 'dependency':
                row['request']['dependencies'] = {'intraday': 999}
                row['ticket']['dependencies'] = [['intraday', 999]]
                row['ticket']['request_digest'] = digest(row['request'])
            elif mutation == 'status':
                row['terminal']['receipt']['status'] = 'accepted'
            elif mutation == 'future':
                row['terminal']['envelope']['classified_at'] = (NOW + timedelta(days=1)).isoformat()
                row['terminal']['receipt']['outcome_digest'] = digest(row['terminal']['envelope'])
            elif mutation == 'bool_generation':
                row['request']['generation'] = True
                row['ticket']['generation'] = True
                row['ticket']['request_digest'] = digest(row['request'])
            elif mutation == 'phantom_success':
                # Earlier ticket cannot be accepted after a newer begin, even with coherent digest.
                old = registry['records']['a']
                old['terminal']['receipt']['committed_version'] = runtime.owner.version
            with pytest.raises(ValueError):
                api()[1](state, runtime.owner.version)
        finally:
            await runtime.shutdown()
            await store.close()
    asyncio.run(scenario())


def test_forged_ticket_and_bad_payload_rejected_without_live_changes(tmp_path, monkeypatch):
    async def scenario():
        store, runtime, source = await fixture(tmp_path, monkeypatch)
        try:
            ticket = await source.begin('a', 'intraday_5m')
            before = runtime.owner.state
            with pytest.raises(ValueError, match='ticket'):
                await success(source, replace(ticket, sequence=99))
            for bad in (True, 'nonsense', 1):
                with pytest.raises(ValueError, match='level'):
                    await source.complete(ticket, 'success', {'level': bad})
            with pytest.raises(ValueError):
                await source.complete(ticket, 'success', {'change_pct': float('nan')})
            assert runtime.owner.state == before
            assert source.snapshot().observation_status == 'pending'
        finally:
            await runtime.shutdown()
            await store.close()
    asyncio.run(scenario())


def test_trusted_completion_reducer_is_atomic_and_runs_once_for_latest_success(tmp_path, monkeypatch):
    async def scenario():
        store, runtime, _ = await fixture(tmp_path, monkeypatch)
        def reduce(state, ticket, envelope, version):
            state.setdefault('synthetic_effects', []).append({'id': ticket.operation_id, 'version': version})
            return state
        source = api()[0](runtime, completion_reducer=reduce)
        try:
            a = await source.begin('a', 'intraday_5m')
            b = await source.begin('b', 'intraday_5m')
            result = await success(source, b)
            await success(source, a)
            await success(source, b)
            await success(source, b, 'severe')
            c = await source.begin('c', 'intraday_5m')
            await source.complete(c, 'missing')
            assert runtime.owner.state['synthetic_effects'] == [{'id': 'b', 'version': result.committed_version}]
            _, durable = await store.load()
            assert durable['synthetic_effects'] == runtime.owner.state['synthetic_effects']
        finally:
            await runtime.shutdown()
            await store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('bad_hook', ['throws', 'async', 'sources'])
def test_bad_completion_reducer_cannot_commit_or_publish(tmp_path, monkeypatch, bad_hook):
    async def scenario():
        store, runtime, _ = await fixture(tmp_path, monkeypatch)
        def reduce(state, ticket, envelope, version):
            state['synthetic_effects'] = ['uncommitted']
            if bad_hook == 'throws':
                raise ValueError('synthetic reduction error')
            state['risk_sources']['latest'] = {}
            return state
        async def async_reduce(*args):
            return args[0]
        source = api()[0](runtime, completion_reducer=async_reduce if bad_hook == 'async' else reduce)
        try:
            a = await source.begin('a', 'intraday_5m')
            before = runtime.owner.state
            with pytest.raises((ValueError, TypeError)):
                await success(source, a)
            assert runtime.owner.state == before
            _, durable = await store.load()
            assert 'synthetic_effects' not in durable
            with pytest.raises(ApplicationBlocked, match='drain_failed'):
                await runtime.shutdown()
        finally:
            await store.close()
    asyncio.run(scenario())


def test_transitive_dependency_cannot_reuse_invalidated_source(tmp_path, monkeypatch):
    async def scenario():
        store, runtime, source = await fixture(tmp_path, monkeypatch)
        try:
            index = await source.begin('index', 'intraday_5m')
            index_receipt = await success(source, index)
            trend = await source.begin('trend', 'index_trend', dependencies={'intraday': index_receipt.committed_version})
            trend_receipt = await source.complete(trend, 'success', {'regime': 'bull'})
            await source.begin('new-index', 'intraday_5m')
            with pytest.raises(ValueError, match='dependency'):
                await source.begin('llm', 'llm_regime', dependencies={'index_trend': trend_receipt.committed_version})
        finally:
            await runtime.shutdown()
            await store.close()
    asyncio.run(scenario())


def test_predurable_pending_explicit_dependency_masks_snapshot(tmp_path, monkeypatch):
    async def scenario():
        store, runtime, source = await fixture(tmp_path, monkeypatch)
        reached, release = asyncio.Event(), asyncio.Event()
        task = None
        try:
            trend = await source.begin('trend', 'index_trend')
            receipt = await source.complete(trend, 'success', {'regime': 'bull'})
            index = await source.begin('index', 'intraday_5m', dependencies={'index_trend': receipt.committed_version})
            await success(source, index)
            original = store.lookup_commit
            async def held(commit_id):
                reached.set()
                await release.wait()
                return await original(commit_id)
            monkeypatch.setattr(store, 'lookup_commit', held)
            task = asyncio.create_task(source.begin('new-trend', 'index_trend'))
            await asyncio.wait_for(reached.wait(), 2)
            assert runtime.owner.healthy
            assert source.snapshot().observation_status == 'pending'
        finally:
            release.set()
            if task:
                await asyncio.gather(task, return_exceptions=True)
            await runtime.shutdown()
            await store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('outcome', ['success', 'missing'])
def test_corrupt_protection_never_publishes_even_no_policy_change(tmp_path, monkeypatch, outcome):
    async def scenario():
        store, runtime, source = await fixture(tmp_path, monkeypatch)
        try:
            ticket = await source.begin('index', 'intraday_5m')
            await (success(source, ticket) if outcome == 'success' else source.complete(ticket, outcome))
            state = runtime.owner.state
            old_cash = runtime.engine.portfolio.cash
            state['portfolio']['cash'] = '999'
            state['protection']['config'] = {}
            await store.commit(runtime.owner.version, state, 'synthetic-corrupt-protection')
            with pytest.raises(ApplicationBlocked):
                await runtime.restore()
            assert runtime.engine.portfolio.cash == old_cash
            assert source.snapshot().observation_status != 'success'
        finally:
            await store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('field', ['sequence', 'generation', 'admission_version'])
def test_bool_ticket_integers_are_not_canonical(tmp_path, monkeypatch, field):
    async def scenario():
        store, runtime, source = await fixture(tmp_path, monkeypatch)
        try:
            ticket = await source.begin('index', 'intraday_5m')
            state = runtime.owner.state
            raw = state['risk_sources']['records']['index']['ticket']
            if field == 'admission_version':
                # Choose False only to exercise exact integer semantics; all remain invalid.
                raw[field] = False
            else:
                raw[field] = bool(raw[field])
            with pytest.raises(ValueError):
                api()[1](state, runtime.owner.version)
        finally:
            await runtime.shutdown()
            await store.close()
    asyncio.run(scenario())


def test_bool_receipt_sequence_cannot_alias_integer_on_restore(tmp_path, monkeypatch):
    async def scenario():
        store, runtime, source = await fixture(tmp_path, monkeypatch)
        try:
            ticket = await source.begin('index', 'intraday_5m')
            await success(source, ticket)
            state = runtime.owner.state
            row = state['risk_sources']['records']['index']
            row['terminal']['receipt']['sequence'] = True
            with pytest.raises(ValueError):
                api()[1](state, runtime.owner.version)
        finally:
            await runtime.shutdown()
            await store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('dependencies', [False, [], '', 0])
def test_invalid_empty_dependency_input_is_not_absence(tmp_path, monkeypatch, dependencies):
    async def scenario():
        store, runtime, source = await fixture(tmp_path, monkeypatch)
        try:
            with pytest.raises(ValueError, match='dependency'):
                await source.begin('index', 'intraday_5m', dependencies=dependencies)
            assert 'risk_sources' not in runtime.owner.state
        finally:
            await runtime.shutdown()
            await store.close()
    asyncio.run(scenario())


def test_accepted_outer_fetch_scope_can_store_result_after_shutdown_started(tmp_path, monkeypatch):
    async def scenario():
        store, runtime, source = await fixture(tmp_path, monkeypatch)
        reached, release = asyncio.Event(), asyncio.Event()
        async def caller():
            with runtime.command_scope() as token:
                ticket = await source.begin('fetch', 'intraday_5m')
                reached.set()
                await release.wait()  # 외부 collector 대기 경계
                return await source.complete(ticket, 'success', {'level': 'normal'}, scope_token=token)
        task = asyncio.create_task(caller())
        await asyncio.wait_for(reached.wait(), 2)
        closing = asyncio.create_task(runtime.shutdown())
        await asyncio.sleep(0)
        assert not closing.done()
        release.set()
        try:
            receipt = await asyncio.wait_for(task, 2)
            await asyncio.wait_for(closing, 2)
            assert receipt.status == 'accepted'
            assert runtime.owner.state['risk_sources']['records']['fetch']['terminal']['receipt']['status'] == 'accepted'
            assert not runtime._command_results_failed
        finally:
            await asyncio.gather(task, closing, return_exceptions=True)
            await store.close()
    asyncio.run(scenario())


def test_another_task_cannot_borrow_completion_scope_token(tmp_path, monkeypatch):
    async def scenario():
        store, runtime, source = await fixture(tmp_path, monkeypatch)
        try:
            ticket = await source.begin('fetch', 'intraday_5m')
            with runtime.command_scope() as token:
                task = asyncio.create_task(source.complete(ticket, 'missing', scope_token=token))
                with pytest.raises(ApplicationBlocked, match='command_scope_required'):
                    await task
            assert runtime.owner.state['risk_sources']['records']['fetch']['terminal'] is None
        finally:
            await runtime.shutdown()
            await store.close()
    asyncio.run(scenario())
