"""Opus I1/Minor2·3 독립 재현: 실제 SQLite와 detached 역사 검증."""
import asyncio
from copy import deepcopy
from decimal import Decimal
import json
from pathlib import Path

import pytest

from src.core.engine import UnifiedEngine
from src.core.types import TradingConfig
from src.execution.safety.application import ApplicationBlocked
from src.execution.safety.policy_generations import canonical, validate_policy_generations
from src.execution.safety.protection_recovery import digest
from src.execution.safety.regime_owner import validate_regime_policy
from src.execution.safety.risk_sources import RiskSourceCoordinator, validate_risk_sources
from src.execution.safety.runtime import KRExecutionRuntime
from src.execution.safety.store import ExecutionStateStore
from src.strategies.exit_manager import ExitManager
from test_execution_regime_owner_boundaries_review import install, provider
from test_execution_runtime import NOW, setup


@pytest.fixture(autouse=True)
def synthetic_home(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)


async def cold_health(path):
    engine = UnifiedEngine(TradingConfig(initial_capital=Decimal('2000000')))
    exits = ExitManager(persist=False, clock=lambda: NOW)
    store = ExecutionStateStore(path)
    runtime = KRExecutionRuntime(store, engine, exits, clock=lambda: NOW, account_scope='scope')
    try:
        try:
            await runtime.restore()
        except Exception as exc:
            return False, str(exc.__cause__ or exc)
        return runtime.owner.healthy, None
    finally:
        await store.close()


async def close_failed_or_healthy(runtime, store):
    try:
        await runtime.shutdown()
    except ApplicationBlocked:
        pass
    await store.close()


@pytest.mark.parametrize('hook', ['none', 'noop', 'wrong_projection'])
def test_foreign_index_success_cannot_durably_poison_installed_regime_root(tmp_path, hook):
    async def scenario():
        _, _, store, runtime, _ = await install(tmp_path, lambda: NOW)
        def wrong(candidate, *_args):
            candidate['regime_policy']['trend_state']['mid_regime'] = 'bull'
            return candidate
        reducer = {'none': None, 'noop': lambda candidate, *_args: candidate,
                   'wrong_projection': wrong}[hook]
        foreign = RiskSourceCoordinator(runtime, completion_reducer=reducer)
        try:
            ticket = await foreign.begin('foreign-success', 'index_trend', require_seal=False)
            before = await store.load()
            assert before[1]['risk_sources']['records'][ticket.operation_id]['terminal'] is None
            error = None
            try:
                await foreign.complete(ticket, 'success', {'synthetic': 'foreign'})
            except Exception as exc:
                error = exc
            after = await store.load()
            durable_terminal = after[1]['risk_sources']['records'][ticket.operation_id]['terminal']
            cold_ok, cold_error = await cold_health(store.path)
            observed = {
                'request_rejected': error is not None,
                'durable_unchanged': after == before,
                'durable_accepted': bool(durable_terminal and durable_terminal['receipt']['status'] == 'accepted'),
                'cold_healthy': cold_ok,
                'cold_error': cold_error,
            }
            expected = {'request_rejected': True, 'durable_unchanged': True,
                        'durable_accepted': False, 'cold_healthy': True, 'cold_error': None}
            if hook == 'none':
                observed.update(owner_healthy=runtime.owner.healthy,
                                result_failed=runtime._command_results_failed)
                expected.update(owner_healthy=True, result_failed=False)
            assert observed == expected, observed
        finally:
            await close_failed_or_healthy(runtime, store)
    asyncio.run(scenario())


@pytest.mark.parametrize('outcome', [None, 'failed', 'missing', 'cancelled'])
def test_foreign_non_success_index_facts_remain_valid_with_regime_root(tmp_path, outcome):
    async def scenario():
        _, _, store, runtime, _ = await install(tmp_path, lambda: NOW)
        foreign = RiskSourceCoordinator(runtime)
        try:
            before = deepcopy(runtime.owner.state['regime_policy'])
            ticket = await foreign.begin('foreign-control', 'index_trend')
            if outcome is not None:
                assert (await foreign.complete(ticket, outcome)).status == outcome
            state = runtime.owner.state
            assert state['regime_policy'] == before
            assert validate_regime_policy(state, runtime.owner.version) == before
            assert runtime.owner.healthy and not runtime._command_results_failed
            assert await cold_health(store.path) == (True, None)
        finally:
            await close_failed_or_healthy(runtime, store)
    asyncio.run(scenario())


def test_actual_regime_writer_success_commits_transition_and_cold_restores(tmp_path):
    async def scenario():
        _, _, store, runtime, writer = await install(tmp_path, lambda: NOW)
        try:
            receipt = await writer.refresh_trend(provider(lambda: NOW))
            assert receipt.status == 'accepted'
            version, state = await store.load()
            row = state['regime_policy']['transitions'][receipt.operation_id]
            assert row['version'] == receipt.committed_version == version
            assert state['risk_sources']['records'][receipt.operation_id]['request']['require_seal'] is True
            assert await cold_health(store.path) == (True, None)
        finally:
            await close_failed_or_healthy(runtime, store)
    asyncio.run(scenario())


def test_generic_index_success_without_regime_root_keeps_existing_contract(tmp_path):
    async def scenario():
        _, _, store, runtime = await setup(tmp_path, account_scope='scope')
        source = RiskSourceCoordinator(runtime)
        try:
            ticket = await source.begin('generic-index', 'index_trend')
            assert (await source.complete(ticket, 'success', {})).status == 'accepted'
            assert 'regime_policy' not in runtime.owner.state
            assert runtime.owner.healthy and not runtime._command_results_failed
            assert await cold_health(store.path) == (True, None)
        finally:
            await close_failed_or_healthy(runtime, store)
    asyncio.run(scenario())


def test_foreign_malformed_vix_success_cannot_persist_unrestorable_checkpoint(tmp_path):
    async def scenario():
        _, _, store, runtime, _ = await install(tmp_path, lambda: NOW)
        foreign = RiskSourceCoordinator(runtime)
        try:
            ticket = await foreign.begin('foreign-invalid-vix', 'vix_regime')
            before = await store.load()
            error = None
            try:
                await foreign.complete(ticket, 'success',
                    {'value': 'not-a-number', 'fetched_at': NOW.isoformat()},
                    source='synthetic', source_event_id='foreign-invalid-vix', received_at=NOW)
            except Exception as exc:
                error = exc
            after = await store.load()
            cold_ok, cold_error = await cold_health(store.path)
            observed = {'rejected': error is not None, 'durable_unchanged': after == before,
                        'cold_healthy': cold_ok, 'cold_error': cold_error,
                        'owner_healthy': runtime.owner.healthy,
                        'result_failed': runtime._command_results_failed}
            assert observed == {'rejected': True, 'durable_unchanged': True,
                                'cold_healthy': True, 'cold_error': None,
                                'owner_healthy': True, 'result_failed': False}, observed
        finally:
            await close_failed_or_healthy(runtime, store)
    asyncio.run(scenario())


def rehash_accepted(state, operation):
    """수학/형태 validator를 시험하도록 관련 해시만 맞춘 detached 공격 입력."""
    row = state['regime_policy']['transitions'][operation]
    terminal = state['risk_sources']['records'][operation]['terminal']
    seal = state['risk_input_seals']['records'][operation]['original']
    seal['request_digest'] = digest(seal['request'])
    seal['seal_digest'] = digest({key: value for key, value in seal.items() if key != 'seal_digest'})
    row['seal_digest'] = seal['seal_digest']
    terminal['receipt']['outcome_digest'] = row['outcome_digest'] = digest(terminal['envelope'])


def test_rehashed_historical_inverted_ohlc_cannot_pass_real_regime_validator(tmp_path):
    async def scenario():
        _, _, store, runtime, writer = await install(tmp_path, lambda: NOW)
        try:
            receipt = await writer.refresh_trend(provider(lambda: NOW))
            assert receipt.status == 'accepted'
            original = await store.load()
            state = deepcopy(original[1])
            seal = state['risk_input_seals']['records'][receipt.operation_id]['original']
            terminal = state['risk_sources']['records'][receipt.operation_id]['terminal']
            observations = []
            for text in seal['request']['inputs']['observations']:
                observation = json.loads(text)
                assert observation['fields']['low']['value'] == 98.0
                assert observation['fields']['high']['value'] == 102.0
                observation['fields']['low']['value'] = 102.0
                observation['fields']['high']['value'] = 98.0
                observations.append(canonical(observation))
            seal['request']['inputs']['observations'] = observations
            terminal['envelope']['payload']['inputs'] = deepcopy(seal['request']['inputs'])
            rehash_accepted(state, receipt.operation_id)
            validate_policy_generations(state, original[0])
            validate_risk_sources(state, original[0])
            assert await store.load() == original, 'adversarial history must remain detached from SQLite'
            with pytest.raises(ValueError):
                validate_regime_policy(state, original[0])
        finally:
            await close_failed_or_healthy(runtime, store)
    asyncio.run(scenario())


def test_rehashed_boolean_payload_cannot_equal_numeric_regime_transition(tmp_path):
    async def scenario():
        _, _, store, runtime, writer = await install(tmp_path, lambda: NOW)
        try:
            receipt = await writer.refresh_trend(provider(lambda: NOW))
            assert receipt.status == 'accepted'
            original = await store.load()
            state = deepcopy(original[1])
            terminal = state['risk_sources']['records'][receipt.operation_id]['terminal']
            data = terminal['envelope']['payload']['after']['regime_data']
            assert type(data['avg_change']) is float and data['avg_change'] == 0.0
            data['avg_change'] = False
            # root와 transition의 계산값은 원 float0을 유지한다. bool payload만 해시를 맞춘다.
            row = state['regime_policy']['transitions'][receipt.operation_id]
            assert type(row['after']['regime_data']['avg_change']) is float
            rehash_accepted(state, receipt.operation_id)
            validate_policy_generations(state, original[0])
            validate_risk_sources(state, original[0])
            assert await store.load() == original
            with pytest.raises(ValueError):
                validate_regime_policy(state, original[0])
        finally:
            await close_failed_or_healthy(runtime, store)
    asyncio.run(scenario())
