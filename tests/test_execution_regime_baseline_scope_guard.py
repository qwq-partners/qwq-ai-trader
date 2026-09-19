"""Typed index admission and append share the strict registered-baseline boundary."""
import asyncio
from copy import deepcopy
from pathlib import Path

import pytest

from src.execution.safety.regime_owner import _validate_regime_append
from test_execution_regime_baseline_scope_review import (
    actual_refresh_envelope, public_complete, public_seal, target,
)
from test_execution_regime_precommit_review import close_failed_or_healthy


@pytest.fixture(autouse=True)
def synthetic_home(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)


@pytest.mark.parametrize('require_seal', [False, True])
def test_bound_prebaseline_success_rejects_before_task_and_preserves_health(tmp_path, require_seal):
    async def scenario():
        envelope = await actual_refresh_envelope(tmp_path / 'oracle')
        store, runtime, writer, _, ticket = await target(tmp_path / 'target',
            before_baseline=True, require_seal=require_seal)
        try:
            await public_seal(writer, ticket, envelope)
            before = await store.load()
            with pytest.raises(ValueError, match='baseline_scope'):
                await public_complete(writer.sources, ticket, envelope)
            assert await store.load() == before
            assert runtime.owner.healthy and not runtime._command_results_failed
            assert not runtime._command_result_tasks and not runtime._command_scopes
            assert runtime.owner.state['risk_sources']['records'][ticket.operation_id]['terminal'] is None
        finally:
            await close_failed_or_healthy(runtime, store)
    asyncio.run(scenario())


@pytest.mark.parametrize('offset', [-1, 0])
def test_detached_append_rejects_admission_at_or_before_baseline(tmp_path, offset):
    async def scenario():
        envelope = await actual_refresh_envelope(tmp_path / 'oracle')
        store, runtime, writer, _, ticket = await target(tmp_path / 'target', before_baseline=False)
        try:
            await public_seal(writer, ticket, envelope)
            before_root = deepcopy(runtime.owner.state['regime_policy'])
            receipt = await public_complete(writer.sources, ticket, envelope)
            assert receipt.status == 'accepted'
            durable = await store.load()
            candidate = deepcopy(durable[1])
            candidate['risk_sources']['records'][ticket.operation_id]['ticket']['admission_version'] = (
                before_root['baseline']['baseline_version'] + offset)
            with pytest.raises(ValueError, match='baseline_scope'):
                _validate_regime_append(candidate, before_root, ticket.operation_id, receipt.committed_version)
            assert await store.load() == durable
        finally:
            await close_failed_or_healthy(runtime, store)
    asyncio.run(scenario())
