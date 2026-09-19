"""C2a acceptance: real SQLite owner/lifecycle/core queue; no external I/O."""
import asyncio
import json
import threading
from copy import deepcopy
from datetime import timedelta
from decimal import Decimal

import pytest

from src.execution.safety.application import FillObservation
from src.execution.safety.lifecycle import OrderEvidence, OrderState
from src.execution.safety.risk_sources import RiskSourceCoordinator
from src.execution.safety.transport import GuardedKISTransport
from test_execution_policy_generations import cold_restore
from test_execution_risk_input_seal import fixture
from test_execution_runtime import NOW, queued


def test_v1_actual_queued_fill_releases_pending_sector_without_advancing_sidecar_generation(tmp_path, monkeypatch):
    """Would fail if a settled fill rewrote sidecar_active or generation history."""
    from test_execution_command_owner import fixture as command_fixture

    async def scenario():
        f = await command_fixture(tmp_path, monkeypatch, origin='automatic')
        runtime, store, engine = f['runtime'], f['store'], f['engine']
        try:
            await runtime.owner.register_policy_generations(
                'register-sidecar', ('entry_policy_effects.sidecar_active',))
            source = RiskSourceCoordinator(runtime)
            ticket = await source.begin('model', 'llm_regime', require_seal=True)
            await source.seal(ticket, versioned_policy_reads=(
                'entry_policy_effects.sidecar_active',), inputs={})
            before = deepcopy(runtime.owner.state['policy_generations'])

            request = f['request'](quantity=10)
            await f['quote'](request)
            await f['commands'].prepare(request, f['entry'](request), sector='반도체')
            assert runtime.owner.state['entry_policy_effects']['pending_sectors'] == {'005930': '반도체'}
            result = await f['commands'].dispatch(
                request, f['entry'](request), GuardedKISTransport(f['broker'], request_builder=f['builder']))
            assert result.order_ref is not None
            ref = result.order_ref
            evidence = OrderEvidence(
                ref, '005930', 'buy', 10, 10, Decimal('100000'), 0, 0,
                OrderState.FINAL_FILLED, complete=True, supported_finality=True,
                source_contract='synthetic-test', observed_at=NOW,
                request_started_at=NOW - timedelta(seconds=1),
                query_scope={'account_scope': ref.account_scope, 'market': 'KR',
                             'exchange': 'KRX', 'start_date': '2026-09-18',
                             'end_date': '2026-09-18', 'tr_id': 'TTTC0081R',
                             'query_kind': 'all', 'session': 'regular'},
            )
            assert await runtime.lifecycle.reconcile(request.attempt_id, evidence)
            observation = FillObservation(
                ref.account_scope, ref.market, ref.order_date, ref.exchange, ref.order_no,
                '005930', 'BUY', 10, Decimal('100000'), org_no=ref.org_no,
                parent_order_no=ref.parent_order_no)
            assert (await queued(engine, observation)).status == 'APPLIED'
            assert runtime.owner.state['entry_policy_effects']['pending_sectors'] == {}
            assert runtime.owner.state['policy_generations'] == before
            assert (await source.complete(ticket, 'success', {})).status == 'accepted'
        finally:
            await runtime.shutdown()
            await store.close()

    asyncio.run(scenario())


def test_v3_partial_then_superset_registration_keeps_each_selector_first_version(tmp_path):
    """Would fail if later superset registration reset an existing selector's origin."""
    async def scenario():
        _, _, store, runtime, source = await fixture(tmp_path)
        try:
            first = await runtime.owner.register_policy_generations('register-a', ('protection.config',))
            second = await runtime.owner.register_policy_generations(
                'register-a-b', ('protection.config', 'protection.current_regime'))
            assert first < second
            selectors = runtime.owner.state['policy_generations']['selectors']
            assert selectors['protection.config']['registered_version'] == first
            assert selectors['protection.current_regime']['registered_version'] == second
            ticket = await source.begin('model', 'llm_regime', require_seal=True)
            receipt = await source.seal(ticket, versioned_policy_reads=(
                'protection.config', 'protection.current_regime'), inputs={})
            reads = json.loads(receipt.reads_json)['versioned_policies']
            assert reads['protection.config']['registered_version'] == first
            assert reads['protection.config']['generation'] == first
            assert reads['protection.current_regime']['registered_version'] == second
            assert reads['protection.current_regime']['generation'] == second
        finally:
            await runtime.shutdown()
            await store.close()

    asyncio.run(scenario())


def test_v4_legacy_and_distinct_versioned_reads_seal_but_duplicate_selector_is_rejected(tmp_path):
    """Would fail if schema-2 mixed reads lost legacy data or allowed duplicate authority."""
    async def scenario():
        _, _, store, runtime, source = await fixture(tmp_path)
        try:
            version = await runtime.owner.register_policy_generations(
                'register-regime', ('protection.current_regime',))
            ticket = await source.begin('mixed', 'llm_regime', require_seal=True)
            receipt = await source.seal(ticket, policy_reads=('protection.config',),
                versioned_policy_reads=('protection.current_regime',), inputs={})
            reads = json.loads(receipt.reads_json)
            assert reads['policies']['protection.config']['present'] is True
            assert reads['versioned_policies']['protection.current_regime']['generation'] == version
            duplicate = await source.begin('duplicate', 'index_trend', require_seal=True)
            before = await store.load()
            with pytest.raises(ValueError, match='unsupported_input_seal_selection'):
                await source.seal(duplicate, policy_reads=('protection.config',),
                    versioned_policy_reads=('protection.config',), inputs={})
            assert await store.load() == before
            assert runtime.owner.healthy
        finally:
            await runtime.shutdown()
            await store.close()

    asyncio.run(scenario())


def test_v5_cancelled_registration_sql_commit_cold_restores_and_same_request_is_idempotent(tmp_path, monkeypatch):
    """Would fail if cancellation published a provisional registry or lost the durable receipt."""
    async def scenario():
        from src.execution.safety.application import ApplicationBlocked
        _, _, store, runtime, _ = await fixture(tmp_path)
        entered, release = threading.Event(), threading.Event()
        try:
            before_version, before = await store.load()
            original = store._commit

            def gate(expected, payload, command):
                if command == 'policy-registration:register-config':
                    entered.set()
                    assert release.wait(5)
                return original(expected, payload, command)

            with monkeypatch.context() as patch:
                patch.setattr(store, '_commit', gate)
                task = asyncio.create_task(runtime.owner.register_policy_generations(
                    'register-config', ('protection.config',)))
                assert await asyncio.to_thread(entered.wait, 5)
                task.cancel()
                release.set()
                with pytest.raises(asyncio.CancelledError):
                    await task

            durable_version, durable = await store.load()
            assert runtime.owner.state == before
            assert durable_version == before_version + 1
            assert durable['policy_generations']['registrations']['register-config'] == {
                'selectors': ['protection.config'], 'version': durable_version}
            # 등록도 명령 결과 drain에 속하므로, 취소한 caller가 stale owner를
            # 정상 종료로 숨길 수 없다. restore가 durable 사실을 다시 게시한 뒤에만 닫힌다.
            with pytest.raises(ApplicationBlocked, match='command_state_drain_failed'):
                await runtime.shutdown()
            await runtime.restore()
            assert runtime.owner.state == durable
            await runtime.shutdown()
            store, runtime = await cold_restore(store, runtime)
            assert runtime.owner.state == durable
            assert await runtime.owner.register_policy_generations(
                'register-config', ('protection.config',)) == durable_version
            assert runtime.owner.state == durable
        finally:
            release.set()
            await runtime.shutdown()
            await store.close()

    asyncio.run(scenario())
