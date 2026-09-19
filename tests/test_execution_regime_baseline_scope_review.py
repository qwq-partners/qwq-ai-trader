"""Independent baseline-scope adjudication using real public source/seal APIs.

The lower-level completion payload is captured, unchanged, from an actual
RegimeOwner.refresh_trend on a separate synthetic runtime. No accepted fact,
seal, regime root, or SQLite checkpoint is fabricated in the target runtime.
"""
import asyncio
from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path

import pytest

from src.execution.safety.regime_owner import POLICY_READS, RegimeBaseline, RegimeOwner
from src.execution.safety.risk_input_seal import input_seal_current
from src.execution.safety.risk_sources import RiskSourceCoordinator
from test_execution_regime_owner import baseline_json
from test_execution_regime_owner_boundaries_review import install, prerequisites, provider
from test_execution_regime_precommit_review import cold_health, close_failed_or_healthy
from test_execution_runtime import NOW


@pytest.fixture(autouse=True)
def synthetic_home(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)


async def actual_refresh_envelope(tmp_path):
    _, _, store, runtime, writer = await install(tmp_path, lambda: NOW, vix=None)
    try:
        receipt = await writer.refresh_trend(provider(lambda: NOW))
        assert receipt.status == 'accepted'
        envelope = deepcopy(runtime.owner.state['risk_sources']['records'][receipt.operation_id]['terminal']['envelope'])
        assert envelope['payload']['after']['source_refs'] == {'trend': None, 'vix': None, 'expert': None}
        return envelope
    finally:
        await close_failed_or_healthy(runtime, store)


async def target(tmp_path, *, before_baseline, require_seal=True):
    engine, _, store, runtime = await prerequisites(tmp_path, lambda: NOW)
    foreign = RiskSourceCoordinator(runtime)
    ticket = None
    if before_baseline:
        ticket = await foreign.begin('baseline-crossing-index', 'index_trend', require_seal=require_seal)
    await RegimeOwner.register_baseline(runtime, RegimeBaseline.from_dict(baseline_json(runtime)),
        expected_version=runtime.owner.version)
    await runtime.owner.register_policy_generations('baseline-scope-reads', POLICY_READS)
    async def no_vix():
        return None
    writer = RegimeOwner(runtime, adapter=engine._regime_adapter, sidecar=runtime.risk_manager,
        vix_fetcher=no_vix)
    if ticket is None:
        ticket = await writer.sources.begin('baseline-crossing-index', 'index_trend', require_seal=require_seal)
    return store, runtime, writer, foreign, ticket


async def public_seal(writer, ticket, envelope):
    expected = writer.sources.capture_reads(ticket, source_lanes=('vix_regime', 'expert_regime'),
        versioned_policy_reads=POLICY_READS)
    receipt = await writer.sources.seal(ticket, source_lanes=('vix_regime', 'expert_regime'),
        versioned_policy_reads=POLICY_READS, inputs=envelope['payload']['inputs'], expected_reads_json=expected)
    assert receipt.status == 'sealed'
    assert input_seal_current(writer.runtime.owner.state, ticket)


async def public_complete(source, ticket, envelope):
    timestamps = {key: None if envelope[key] is None else datetime.fromisoformat(envelope[key])
        for key in ('received_at', 'market_as_of', 'classified_at', 'recovery_until')}
    return await source.complete(ticket, envelope['outcome'], envelope['payload'],
        source=envelope['source'], source_event_id=envelope['source_event_id'], **timestamps)


@pytest.mark.parametrize('require_seal', [False, True])
def test_prebaseline_ticket_public_bound_completion_cannot_commit_unrestorable_transition(tmp_path, require_seal):
    async def scenario():
        envelope = await actual_refresh_envelope(tmp_path / 'oracle')
        store, runtime, writer, _, ticket = await target(tmp_path / 'target',
            before_baseline=True, require_seal=require_seal)
        try:
            baseline = runtime.owner.state['regime_policy']['baseline']['baseline_version']
            assert ticket.admission_version < baseline
            await public_seal(writer, ticket, envelope)
            before = await store.load()
            error = None
            try:
                await public_complete(writer.sources, ticket, envelope)
            except Exception as exc:
                error = exc
            after = await store.load()
            terminal = after[1]['risk_sources']['records'][ticket.operation_id]['terminal']
            cold_ok, cold_error = await cold_health(store.path)
            observed = {
                'rejected': error is not None,
                'durable_unchanged': before == after,
                'durable_accepted': bool(terminal and terminal['receipt']['status'] == 'accepted'),
                'durable_transition': ticket.operation_id in after[1]['regime_policy']['transitions'],
                'cold_healthy': cold_ok, 'cold_error': cold_error,
            }
            assert observed == {'rejected': True, 'durable_unchanged': True,
                'durable_accepted': False, 'durable_transition': False,
                'cold_healthy': True, 'cold_error': None}, json.dumps({
                    **observed, 'admission_version': ticket.admission_version,
                    'baseline_version': baseline, 'version_before': before[0], 'version_after': after[0],
                    'error_type': type(error).__name__, 'error_cause': str(error.__cause__ or error),
                    'owner_healthy': runtime.owner.healthy, 'result_failed': runtime._command_results_failed}, sort_keys=True)
        finally:
            await close_failed_or_healthy(runtime, store)
    asyncio.run(scenario())


def test_same_actual_payload_and_public_seal_work_for_postbaseline_ticket(tmp_path):
    async def scenario():
        envelope = await actual_refresh_envelope(tmp_path / 'oracle')
        store, runtime, writer, _, ticket = await target(tmp_path / 'target', before_baseline=False)
        try:
            assert ticket.admission_version > runtime.owner.state['regime_policy']['baseline']['baseline_version']
            await public_seal(writer, ticket, envelope)
            receipt = await public_complete(writer.sources, ticket, envelope)
            assert receipt.status == 'accepted'
            assert runtime.owner.healthy and not runtime._command_results_failed
            assert await cold_health(store.path) == (True, None)
        finally:
            await close_failed_or_healthy(runtime, store)
    asyncio.run(scenario())


def test_foreign_facade_cannot_complete_same_prebaseline_ticket_after_valid_seal(tmp_path):
    async def scenario():
        envelope = await actual_refresh_envelope(tmp_path / 'oracle')
        store, runtime, writer, foreign, ticket = await target(tmp_path / 'target', before_baseline=True)
        try:
            await public_seal(writer, ticket, envelope)
            before = await store.load()
            with pytest.raises(ValueError, match='writer_required'):
                await public_complete(foreign, ticket, envelope)
            assert await store.load() == before
            assert runtime.owner.healthy and not runtime._command_results_failed
            assert await cold_health(store.path) == (True, None)
        finally:
            await close_failed_or_healthy(runtime, store)
    asyncio.run(scenario())


def test_normal_refresh_begins_new_ticket_and_supersedes_prebaseline_ticket(tmp_path):
    async def scenario():
        store, runtime, writer, _, ticket = await target(tmp_path, before_baseline=True)
        try:
            receipt = await writer.refresh_trend(provider(lambda: NOW))
            assert receipt.status == 'accepted' and receipt.operation_id != ticket.operation_id
            state = runtime.owner.state
            assert state['risk_sources']['records'][receipt.operation_id]['ticket']['admission_version'] > state['regime_policy']['baseline']['baseline_version']
            stale = await writer.sources.complete(ticket, 'success', {})
            assert (stale.status, stale.reason) == ('stale', 'superseded')
            assert runtime.owner.healthy and not runtime._command_results_failed
            assert await cold_health(store.path) == (True, None)
        finally:
            await close_failed_or_healthy(runtime, store)
    asyncio.run(scenario())
