"""Independent capture-to-seal acceptance: real owner/SQLite, no provider I/O."""
import asyncio
from copy import deepcopy
import json

import pytest

from src.execution.safety.risk_sources import validate_risk_sources
from src.execution.safety.protection_recovery import digest
from test_execution_policy_generations import cold_restore
from test_execution_risk_input_seal import fixture


READS = ('protection.config',)


def change_config(value):
    def reduce(state):
        state['protection']['config']['first_exit_pct'] = value
        return state
    return reduce


@pytest.mark.parametrize('change', ['policy', 'policy_aba', 'source', 'unrelated'])
def test_captured_reads_cannot_be_relabelled_with_later_owner_facts(tmp_path, change):
    async def scenario():
        calls = []
        def hook(state, ticket, envelope, version):
            calls.append(ticket.operation_id)
            return state
        _, _, store, runtime, source = await fixture(tmp_path, reducer=hook)
        try:
            await runtime.owner.register_policy_generations('capture-reads', READS)
            ticket = await source.begin('calculated', 'llm_regime', require_seal=True)
            expected = source.capture_reads(ticket, source_lanes=('vix_regime',),
                                            versioned_policy_reads=READS)
            original = runtime.owner.state['protection']['config']['first_exit_pct']
            if change.startswith('policy'):
                await runtime.owner.mutate('policy-B', change_config(original + 1))
                if change == 'policy_aba':
                    await runtime.owner.mutate('policy-A', change_config(original))
            elif change == 'source':
                leaf = await source.begin('later-vix', 'vix_regime')
                await source.complete(leaf, 'failed')
            else:
                def unrelated(state):
                    state['capture_review_unrelated'] = 1
                    return state
                await runtime.owner.mutate('unrelated', unrelated)
            seal = await source.seal(ticket, source_lanes=('vix_regime',),
                versioned_policy_reads=READS, inputs={'computed_from': expected},
                expected_reads_json=expected)
            result = await source.complete(ticket, 'success', {'candidate': 'fixed-before-change'})
            changed = change != 'unrelated'
            assert (seal.status, seal.reason) == (
                ('stale', 'captured_reads_changed') if changed else ('sealed', ''))
            assert (result.status, result.reason) == (
                ('stale', 'input_seal') if changed else ('accepted', ''))
            assert calls == ([] if changed else ['calculated'])
            row = runtime.owner.state['risk_input_seals']['records']['calculated']['original']
            assert row['request']['schema'] == 3
            assert row['request']['expected_reads'] == json.loads(expected)
            assert (row['reads'] != json.loads(expected)) is changed
            assert row['reads']
            assert runtime.owner.healthy and not runtime.trading_ready
            validate_risk_sources(runtime.owner.state, runtime.owner.version)
            durable = runtime.owner.state
            store, runtime = await cold_restore(store, runtime)
            assert runtime.owner.state == durable
            assert not runtime.trading_ready
        finally:
            await runtime.shutdown()
            await store.close()
    asyncio.run(scenario())


def test_seal_rechecks_capture_after_waiting_for_owner_lock(tmp_path, monkeypatch):
    async def scenario():
        _, _, store, runtime, source = await fixture(tmp_path)
        entered, release, sealing_started = asyncio.Event(), asyncio.Event(), asyncio.Event()
        changing = sealing = None
        try:
            await runtime.owner.register_policy_generations('capture-reads', READS)
            ticket = await source.begin('waiting-calculation', 'llm_regime', require_seal=True)
            expected = source.capture_reads(ticket, versioned_policy_reads=READS)
            original = store.lookup_commit
            async def hold(command):
                if command == 'command:held-policy-change':
                    entered.set()
                    await release.wait()
                return await original(command)
            monkeypatch.setattr(store, 'lookup_commit', hold)
            old = runtime.owner.state['protection']['config']['first_exit_pct']
            changing = asyncio.create_task(runtime.owner.mutate('held-policy-change', change_config(old + 1)))
            await asyncio.wait_for(entered.wait(), 2)
            async def seal():
                sealing_started.set()
                return await source.seal(ticket, versioned_policy_reads=READS,
                    inputs={}, expected_reads_json=expected)
            sealing = asyncio.create_task(seal())
            await asyncio.wait_for(sealing_started.wait(), 2)
            assert runtime.owner.state['protection']['config']['first_exit_pct'] == old
            release.set()
            await changing
            receipt = await sealing
            assert (receipt.status, receipt.reason) == ('stale', 'captured_reads_changed')
            terminal = await source.complete(ticket, 'success', {})
            assert (terminal.status, terminal.reason) == ('stale', 'input_seal')
            assert runtime.owner.healthy
        finally:
            release.set()
            for task in (changing, sealing):
                if task is not None:
                    await asyncio.gather(task, return_exceptions=True)
            await runtime.shutdown()
            await store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('malformed', ['{', 'null', '[]', '{}', '{"sources": {"wrong": null}}'])
def test_bad_expected_reads_are_request_errors_not_result_task_failures(tmp_path, malformed):
    async def scenario():
        _, _, store, runtime, source = await fixture(tmp_path)
        try:
            await runtime.owner.register_policy_generations('capture-reads', READS)
            ticket = await source.begin('invalid-expected', 'llm_regime', require_seal=True)
            before = await store.load()
            with pytest.raises(ValueError):
                await source.seal(ticket, versioned_policy_reads=READS,
                                  inputs={}, expected_reads_json=malformed)
            assert await store.load() == before
            assert runtime.owner.healthy
            assert not runtime._command_results_failed
            expected = source.capture_reads(ticket, versioned_policy_reads=READS)
            seal = await source.seal(ticket, versioned_policy_reads=READS,
                                    inputs={}, expected_reads_json=expected)
            assert seal.status == 'sealed'
            assert (await source.complete(ticket, 'success', {})).status == 'accepted'
        finally:
            await runtime.shutdown()
            await store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('tamper', ['hide_stale', 'invent_stale', 'actual_generation'])
def test_schema3_historical_validation_rejects_rehashed_mismatches(tmp_path, tamper):
    async def scenario():
        _, _, store, runtime, source = await fixture(tmp_path)
        try:
            await runtime.owner.register_policy_generations('capture-reads', READS)
            ticket = await source.begin('history', 'llm_regime', require_seal=True)
            expected = source.capture_reads(ticket, versioned_policy_reads=READS)
            if tamper == 'hide_stale':
                old = runtime.owner.state['protection']['config']['first_exit_pct']
                await runtime.owner.mutate('changed', change_config(old + 1))
            await source.seal(ticket, versioned_policy_reads=READS, inputs={}, expected_reads_json=expected)
            candidate = deepcopy(runtime.owner.state)
            row = candidate['risk_input_seals']['records']['history']['original']
            if tamper == 'hide_stale':
                row['status'], row['reason'] = 'sealed', ''
            elif tamper == 'invent_stale':
                row['status'], row['reason'] = 'stale', 'captured_reads_changed'
            else:
                # Both copies agree, but no longer crosslink to the actual policy history.
                row['reads']['versioned_policies']['protection.config']['value']['first_exit_pct'] += 1
                row['request']['expected_reads'] = deepcopy(row['reads'])
                row['request_digest'] = digest(row['request'])
            row['seal_digest'] = digest({key: value for key, value in row.items() if key != 'seal_digest'})
            with pytest.raises(ValueError):
                validate_risk_sources(candidate, runtime.owner.version)
            assert runtime.owner.healthy
        finally:
            await runtime.shutdown()
            await store.close()
    asyncio.run(scenario())


def test_captured_source_fact_preserves_json_boolean_number_distinction(tmp_path):
    """Python True == 1 must not authenticate a changed captured source payload."""
    async def scenario():
        _, _, store, runtime, source = await fixture(tmp_path)
        try:
            leaf = await source.begin('vix-number', 'vix_regime')
            await source.complete(leaf, 'success', {'value': 1})
            ticket = await source.begin('typed-capture', 'llm_regime', require_seal=True)
            expected = json.loads(source.capture_reads(ticket, source_lanes=('vix_regime',)))
            expected['sources']['vix_regime']['terminal']['envelope']['payload']['value'] = True
            before = await store.load()
            with pytest.raises(ValueError):
                await source.seal(ticket, source_lanes=('vix_regime',), inputs={},
                                  expected_reads_json=json.dumps(expected))
            assert await store.load() == before
            assert runtime.owner.healthy and not runtime._command_results_failed
        finally:
            await runtime.shutdown()
            await store.close()
    asyncio.run(scenario())


def test_cancel_during_admitted_trend_completion_does_not_fabricate_outcome_conflict(tmp_path, monkeypatch):
    """Cancellation is not a second contradictory provider response after SQL admission."""
    from pathlib import Path
    from types import SimpleNamespace
    from test_execution_regime_owner import owned, quote
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)

    async def scenario():
        _, _, store, runtime, writer, _ = await owned(tmp_path)
        entered, release = asyncio.Event(), asyncio.Event()
        original = store.lookup_commit
        calls = []
        async def fetch(code):
            calls.append(code)
            return quote(code)
        async def hold(command):
            root = runtime.owner.state.get('risk_sources', {})
            operation = root.get('latest', {}).get('index_trend')
            if operation:
                ticket = root['records'][operation]['ticket']
                if command.startswith('command:risk-complete:' + ticket['request_digest'] + ':'):
                    entered.set()
                    await release.wait()
            return await original(command)
        monkeypatch.setattr(store, 'lookup_commit', hold)
        task = asyncio.create_task(writer.refresh_trend(SimpleNamespace(fetch_index_price=fetch)))
        try:
            await asyncio.wait_for(entered.wait(), 3)
            task.cancel()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            await runtime.shutdown()
            state = runtime.owner.state
            operation = state['risk_sources']['latest']['index_trend']
            row = state['risk_sources']['records'][operation]
            assert row['terminal']['receipt']['status'] == 'accepted'
            assert row['conflict'] is None
            assert list(state['regime_policy']['transitions']) == [operation]
            assert runtime.owner.healthy and not runtime._command_results_failed
            assert calls == ['0001', '1001']
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)
            await runtime.shutdown()
            await store.close()
    asyncio.run(scenario())
