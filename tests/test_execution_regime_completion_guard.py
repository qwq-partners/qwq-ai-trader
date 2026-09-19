"""New typed completion admission and precommit regression boundaries."""
import asyncio
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.execution.safety.application import ApplicationBlocked
from src.execution.safety.regime_owner import RegimeOwner, validate_regime_policy
from src.execution.safety.risk_sources import RiskSourceCoordinator
from test_execution_regime_owner import owned, quote
from test_execution_runtime import NOW


@pytest.fixture(autouse=True)
def synthetic_home(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)


async def close(runtime, store):
    try: await runtime.shutdown()
    except ApplicationBlocked: pass
    await store.close()


@pytest.mark.parametrize('binding', ['foreign', 'noop', 'borrowed', 'owned_none'])
def test_new_index_success_requires_exact_typed_writer_without_durable_change(tmp_path, binding):
    async def scenario():
        _, _, store, runtime, writer, _ = await owned(tmp_path)
        source = RiskSourceCoordinator(runtime, completion_reducer=(
            writer._reduce if binding == 'borrowed' else (lambda state, *args: state) if binding == 'noop' else None))
        if binding == 'owned_none':
            source = writer.sources
            source._completion_reducer = None
        try:
            ticket = await source.begin('unowned', 'index_trend')
            before = await store.load()
            with pytest.raises(ValueError): await source.complete(ticket, 'success', {})
            assert await store.load() == before
            assert runtime.owner.healthy and not runtime._command_results_failed
            assert not runtime._command_result_tasks
        finally: await close(runtime, store)
    asyncio.run(scenario())


def test_malformed_typed_vix_is_rejected_before_task_and_storage(tmp_path):
    async def scenario():
        _, _, store, runtime, _, _ = await owned(tmp_path)
        source = RiskSourceCoordinator(runtime)
        try:
            ticket = await source.begin('invalid-vix', 'vix_regime')
            before = await store.load()
            with pytest.raises(ValueError):
                await source.complete(ticket, 'success', {'value': True, 'fetched_at': NOW.isoformat()})
            assert await store.load() == before
            assert runtime.owner.healthy and not runtime._command_results_failed
        finally: await close(runtime, store)
    asyncio.run(scenario())


@pytest.mark.parametrize('damage', ['projection', 'baseline'])
def test_actual_hook_corruption_fails_before_commit_but_remains_fatal(tmp_path, monkeypatch, damage):
    original = RegimeOwner._reduce
    def corrupt(self, state, ticket, envelope, version):
        result = original(self, state, ticket, envelope, version)
        if ticket.kind == 'index_trend':
            if damage == 'projection': result['regime_policy']['engine_projection']['regime'] = 'bear'
            else: result['regime_policy']['baseline']['supplied']['baseline_id'] = 'corrupted'
        return result
    monkeypatch.setattr(RegimeOwner, '_reduce', corrupt)
    async def scenario():
        _, _, store, runtime, writer, _ = await owned(tmp_path)
        original_commit = store.commit
        bad_commits = []
        async def commit(version, state, key):
            if state['regime_policy']['transitions']: bad_commits.append(key)
            return await original_commit(version, state, key)
        monkeypatch.setattr(store, 'commit', commit)
        async def fetch(code): return quote(code)
        try:
            with pytest.raises((ValueError, ApplicationBlocked)):
                await writer.refresh_trend(SimpleNamespace(fetch_index_price=fetch))
            assert not bad_commits
            version, state = await store.load()
            assert not state['regime_policy']['transitions']
            validate_regime_policy(state, version)
            assert runtime._command_results_failed
        finally: await close(runtime, store)
    asyncio.run(scenario())


@pytest.mark.parametrize('damage', ['boolean', 'ohlc'])
def test_detached_rehashed_transition_cannot_bypass_calculation(tmp_path, damage):
    from src.execution.safety.policy_generations import canonical
    from src.execution.safety.protection_recovery import digest
    import json
    async def scenario():
        _, _, store, runtime, writer, _ = await owned(tmp_path)
        async def fetch(code): return quote(code)
        try:
            receipt = await writer.refresh_trend(SimpleNamespace(fetch_index_price=fetch))
            state = deepcopy(runtime.owner.state)
            op = receipt.operation_id
            terminal = state['risk_sources']['records'][op]['terminal']
            row = state['regime_policy']['transitions'][op]
            seal = state['risk_input_seals']['records'][op]['original']
            if damage == 'boolean': terminal['envelope']['payload']['after']['regime_data']['avg_change'] = False
            else:
                observations = []
                for raw in seal['request']['inputs']['observations']:
                    value = json.loads(raw)
                    value['fields']['low']['value'], value['fields']['high']['value'] = 102.0, 98.0
                    observations.append(canonical(value))
                seal['request']['inputs']['observations'] = observations
                terminal['envelope']['payload']['inputs'] = deepcopy(seal['request']['inputs'])
            seal['request_digest'] = digest(seal['request'])
            seal['seal_digest'] = digest({key: val for key, val in seal.items() if key != 'seal_digest'})
            row['seal_digest'] = seal['seal_digest']
            terminal['receipt']['outcome_digest'] = row['outcome_digest'] = digest(terminal['envelope'])
            with pytest.raises(ValueError): validate_regime_policy(state, runtime.owner.version)
        finally: await close(runtime, store)
    asyncio.run(scenario())


def test_binding_change_during_lookup_is_expected_rejection_not_failed_task(tmp_path, monkeypatch):
    async def scenario():
        _, _, store, runtime, writer, _ = await owned(tmp_path)
        ticket = await writer.sources.begin('binding-race', 'index_trend')
        reached, release = asyncio.Event(), asyncio.Event()
        original = store.lookup_commit
        async def lookup(key):
            if key.startswith('command:risk-complete:'):
                reached.set(); await release.wait()
            return await original(key)
        monkeypatch.setattr(store, 'lookup_commit', lookup)
        before = await store.load()
        task = asyncio.create_task(writer.sources.complete(ticket, 'success', {}))
        try:
            await asyncio.wait_for(reached.wait(), 2)
            writer.sources._completion_reducer = None
            release.set()
            with pytest.raises(ValueError, match='writer_required'): await task
            assert await store.load() == before
            assert runtime.owner.healthy and not runtime._command_results_failed
        finally: release.set(); await close(runtime, store)
    asyncio.run(scenario())


@pytest.mark.parametrize('kind,huge', [('index_trend', False), ('vix_regime', False), ('vix_regime', True)])
def test_baseline_introduced_after_preflight_rejects_before_sql_without_latch(tmp_path, monkeypatch, kind, huge):
    from src.execution.safety.regime_owner import RegimeBaseline
    from test_execution_regime_owner import registration_fixture, baseline_json
    async def scenario():
        store, runtime = await registration_fixture(tmp_path, lambda: NOW)
        source = RiskSourceCoordinator(runtime)
        ticket = await source.begin('root-race', kind)
        reached, release = asyncio.Event(), asyncio.Event()
        original = runtime.owner.mutate
        async def mutate(key, reducer):
            if key.startswith('risk-complete:'):
                reached.set(); await release.wait()
            return await original(key, reducer)
        monkeypatch.setattr(runtime.owner, 'mutate', mutate)
        payload = {} if kind == 'index_trend' else {'value': 10 ** 400 if huge else True, 'fetched_at': NOW.isoformat()}
        task = asyncio.create_task(source.complete(ticket, 'success', payload))
        try:
            await asyncio.wait_for(reached.wait(), 2)
            await RegimeOwner.register_baseline(runtime, RegimeBaseline.from_dict(baseline_json(runtime)),
                expected_version=runtime.owner.version)
            before = await store.load()
            release.set()
            with pytest.raises(ValueError): await task
            assert await store.load() == before
            assert runtime.owner.healthy and not runtime._command_results_failed
        finally: release.set(); await close(runtime, store)
    asyncio.run(scenario())


@pytest.mark.parametrize('control', ['stale', 'missing', 'conflict', 'retry'])
def test_foreign_nonaccepted_or_existing_facts_keep_previous_contract(tmp_path, control):
    async def scenario():
        _, _, store, runtime, writer, _ = await owned(tmp_path)
        source = RiskSourceCoordinator(runtime)
        try:
            ticket = await source.begin('control', 'index_trend')
            if control == 'stale':
                await source.begin('newer', 'index_trend')
                assert (await source.complete(ticket, 'success', {})).status == 'stale'
            elif control == 'missing':
                assert (await source.complete(ticket, 'missing')).status == 'missing'
            else:
                first = await source.complete(ticket, 'missing')
                before = await store.load()
                if control == 'conflict':
                    assert (await source.complete(ticket, 'success', {})).status == 'conflict'
                else:
                    assert await source.complete(ticket, 'missing') == first
                    assert await store.load() == before
            assert runtime.owner.healthy and not runtime._command_results_failed
        finally: await close(runtime, store)
    asyncio.run(scenario())


@pytest.mark.parametrize('error_type', [ValueError, OSError])
def test_typed_completion_storage_fault_remains_fatal(tmp_path, monkeypatch, error_type):
    async def scenario():
        _, _, store, runtime, writer, _ = await owned(tmp_path)
        ticket = await writer.sources.begin('storage-control', 'vix_regime')
        before = await store.load()
        async def lookup(key): raise error_type('genuine lookup fault')
        monkeypatch.setattr(store, 'lookup_commit', lookup)
        try:
            with pytest.raises(error_type, match='genuine lookup fault'):
                await writer.sources.complete(ticket, 'success', {'value': 20.0, 'fetched_at': NOW.isoformat()})
            assert await store.load() == before
            assert runtime._command_results_failed and not runtime.owner.healthy
        finally: await close(runtime, store)
    asyncio.run(scenario())
