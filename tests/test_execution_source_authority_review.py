"""Independent C2b review reproductions: real owner, SQLite, and public reads."""
import asyncio
from copy import deepcopy

import pytest

from src.execution.safety.risk_sources import RiskSourceCoordinator
from test_execution_source_read_authority import accepted, fixture, sealed_intraday


def test_unsealed_completion_ignores_same_lane_predurable_begin_but_public_read_stays_pending(tmp_path, monkeypatch):
    """Would fail if completion treats its own lane as an input read while another facade is pending."""
    async def scenario():
        store, runtime, source = await fixture(tmp_path)
        entered, release = asyncio.Event(), asyncio.Event()
        beginning_started = asyncio.Event()
        completing = beginning = None
        try:
            first = await source.begin('first-index', 'index_trend')
            original = store.lookup_commit
            original_start = runtime.start_command_result

            def observe_begin(token, operation):
                task = original_start(token, operation)
                if runtime._risk_source_pending.get(token) == 'index_trend':
                    beginning_started.set()
                return task

            async def held(commit_id):
                entered.set()
                await release.wait()
                return await original(commit_id)

            monkeypatch.setattr(runtime, 'start_command_result', observe_begin)
            monkeypatch.setattr(store, 'lookup_commit', held)
            completing = asyncio.create_task(accepted(source, first, {'regime': 'bull'}))
            await asyncio.wait_for(entered.wait(), 2)
            beginning = asyncio.create_task(RiskSourceCoordinator(runtime).begin('second-index', 'index_trend'))
            await asyncio.wait_for(beginning_started.wait(), 2)
            assert 'index_trend' in runtime._risk_source_pending.values()
            # The public first-await barrier remains even though the older row is still latest.
            assert source.read_source('index_trend').authority_status == 'pending'

            release.set()
            first_receipt = await completing
            second = await beginning

            assert (first_receipt.status, first_receipt.reason) == ('accepted', '')
            assert runtime.owner.state['risk_sources']['latest']['index_trend'] == second.operation_id
            assert runtime.owner.state['risk_sources']['records'][first.operation_id]['terminal']['receipt'] == {
                'operation_id': first.operation_id,
                'sequence': first.sequence,
                'status': 'accepted',
                'reason': '',
                'committed_version': first_receipt.committed_version,
                'outcome_digest': first_receipt.outcome_digest,
            }
            assert source.read_source('index_trend').authority_status == 'pending'
            assert runtime.owner.healthy
        finally:
            release.set()
            if completing is not None:
                await asyncio.gather(completing, return_exceptions=True)
            if beginning is not None:
                await asyncio.gather(beginning, return_exceptions=True)
            await runtime.shutdown()
            await store.close()

    asyncio.run(scenario())


@pytest.mark.parametrize('conflict_kind', ('outcome', 'reseal'))
def test_conflicted_source_read_rejects_hard_dependency_without_rewriting_accepted_receipt(tmp_path, conflict_kind):
    """Would fail if a conflict became current or changed the original accepted terminal receipt."""
    async def scenario():
        store, runtime, source = await fixture(tmp_path)
        try:
            ticket = await source.begin('accepted-index-' + conflict_kind, 'index_trend', require_seal=True)
            await source.seal(ticket, inputs={'revision': 1})
            receipt = await accepted(source, ticket, {'regime': 'bull'})
            original_terminal = deepcopy(runtime.owner.state['risk_sources']['records'][ticket.operation_id]['terminal'])
            if conflict_kind == 'outcome':
                conflict = await source.complete(ticket, 'failed', {'regime': 'bear'})
                assert (conflict.status, conflict.reason) == ('conflict', 'outcome_conflict')
            else:
                conflict = await source.seal(ticket, inputs={'revision': 2})
                assert (conflict.status, conflict.reason) == ('conflict', 'reseal_conflict')

            read = source.read_source('index_trend')
            assert (read.authority_status, read.operation_id) == ('conflict', ticket.operation_id)
            assert runtime.owner.state['risk_sources']['records'][ticket.operation_id]['terminal'] == original_terminal
            assert receipt.status == 'accepted'
            with pytest.raises(ValueError, match='risk_source_dependency_not_current'):
                await source.begin('hard-dependent-' + conflict_kind, 'llm_regime',
                                   dependencies={'index_trend': receipt.committed_version})
            assert runtime.owner.healthy
        finally:
            await runtime.shutdown()
            await store.close()

    asyncio.run(scenario())


def test_dependency_reducer_race_exposes_exact_value_error_not_private_marker(tmp_path, monkeypatch):
    """Would fail if the public begin API leaked its internal dependency-race exception type."""
    async def scenario():
        store, runtime, source = await fixture(tmp_path)
        entered, changing_started, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        waiting = changing = None
        try:
            leaf = await source.begin('leaf', 'index_trend')
            await accepted(source, leaf)
            _, receipt = await sealed_intraday(source)
            original = store.lookup_commit

            async def held(command):
                if not entered.is_set():
                    entered.set()
                    await release.wait()
                return await original(command)

            monkeypatch.setattr(store, 'lookup_commit', held)
            waiting = asyncio.create_task(source.begin(
                'consumer', 'llm_morning_diagnosis', dependencies={'intraday': receipt.committed_version}))
            await asyncio.wait_for(entered.wait(), 2)

            async def change():
                changing_started.set()
                return await RiskSourceCoordinator(runtime).begin('next-leaf', 'index_trend')

            changing = asyncio.create_task(change())
            await asyncio.wait_for(changing_started.wait(), 2)
            assert source.read_source('intraday').authority_status == 'pending'
            release.set()
            with pytest.raises(ValueError) as raised:
                await waiting
            assert type(raised.value) is ValueError
            assert str(raised.value) == 'risk_source_dependency_not_current'
            assert (await changing).operation_id == 'next-leaf'
            assert runtime.owner.healthy
            assert 'consumer' not in runtime.owner.state['risk_sources']['records']
        finally:
            release.set()
            for task in (waiting, changing):
                if task is not None:
                    await asyncio.gather(task, return_exceptions=True)
            await runtime.shutdown()
            await store.close()

    asyncio.run(scenario())


def test_legacy_policy_hook_keeps_completed_source_current(tmp_path):
    """Would fail if post-hook policy changes were still rechecked as source authority reads."""
    async def scenario():
        def hook(state, ticket, envelope, version):
            state['protection']['config']['first_exit_pct'] = 12.0
            return state

        store, runtime, source = await fixture(tmp_path, reducer=hook)
        try:
            ticket = await source.begin('policy-hook-model', 'llm_regime', require_seal=True)
            await source.seal(ticket, policy_reads=('protection.config',), inputs={})
            receipt = await accepted(source, ticket, {'regime': 'bull'})
            read = source.read_source('llm_regime', expected_version=receipt.committed_version)
            assert (read.authority_status, read.terminal_status) == ('current', 'accepted')
            assert runtime.owner.state['protection']['config']['first_exit_pct'] == 12.0
        finally:
            await runtime.shutdown()
            await store.close()

    asyncio.run(scenario())
