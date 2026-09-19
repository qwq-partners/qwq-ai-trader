"""Real SQLite owner regressions; external index/VIX I/O only is synthetic."""
import asyncio
from copy import deepcopy
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest

from src.core.market_regime import MarketRegimeAdapter
from src.core.types import RiskConfig
from src.risk.manager import RiskManager
from src.execution.safety.application import ApplicationBlocked
from src.execution.safety.risk_sources import RiskSourceCoordinator
from src.execution.safety.risk_transition import IntradayPolicyState
from test_execution_runtime import NOW, setup


@pytest.fixture(autouse=True)
def synthetic_home(tmp_path, monkeypatch):
    from pathlib import Path
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)


def baseline_json(runtime):
    return {'schema': 1, 'baseline_id': 'known-1', 'account_scope': 'scope',
        'business_day': NOW.date().isoformat(), 'generation': runtime._day_generation,
        'fence_id': runtime._day_fence_id,
        'evidence': {'source': 'synthetic', 'event_id': 'known-1', 'observed_at': NOW.isoformat()},
        'sidecar_active': True,
        'intraday': {'baseline_version': runtime.owner.state['intraday_policy']['baseline_version'],
                     'current': runtime.owner.state['intraday_policy']['current']},
        'trend_state': {'mid_regime': 'sideways',
            'mid_pending': {'present': False, 'target': None, 'since': None},
            'expert_pending': {'present': False, 'target': None, 'since': None},
            'market_trend': {'present': True, 'kospi_pct': -1.0, 'kosdaq_pct': -1.0,
                'avg_pct': -1.0, 'vs_open_pct': -0.5, 'position_pct': 20.0,
                'recovering': False, 'classified_at': NOW.isoformat()},
            'regime_data': {}, 'last_update': NOW.isoformat(),
            'source_refs': {'trend': None, 'vix': None, 'expert': None}},
        'engine_regime': 'sideways'}


async def owned(tmp_path, *, fetch_vix=None, clock=None):
    from src.execution.safety.regime_owner import RegimeBaseline, RegimeOwner, POLICY_READS
    sidecar = RiskManager(RiskConfig(), Decimal('2000000'))
    engine, exits, store, runtime = await setup(tmp_path, account_scope='scope', risk_manager=sidecar, clock=clock)
    adapter = MarketRegimeAdapter()
    engine._regime_adapter = adapter
    def seed(state):
        value = IntradayPolicyState('normal', 0.0, None, None).to_dict()
        state['intraday_policy'] = {'schema': 1, 'baseline': value,
            'baseline_version': runtime.owner.version + 1, 'current': value.copy(), 'transitions': {}}
        state['entry_policy_effects'] = {'pending_sectors': {}, 'sidecar_active': True}
        return state
    await runtime.owner.mutate('known-prerequisite', seed)
    supplied = baseline_json(runtime)
    await RegimeOwner.register_baseline(runtime, RegimeBaseline.from_dict(supplied), expected_version=runtime.owner.version)
    await runtime.owner.register_policy_generations('known-reads', POLICY_READS)
    async def missing_vix(): return None
    writer = RegimeOwner(runtime, adapter=adapter, sidecar=sidecar, vix_fetcher=fetch_vix or missing_vix)
    # Explicit accepted fresh VIX fact prevents unrelated background work in ordinary tests.
    ticket = await writer.sources.begin('fresh-vix', 'vix_regime')
    await writer.sources.complete(ticket, 'success', {'value': 20.0, 'fetched_at': NOW.isoformat()},
        source='synthetic-vix', source_event_id='fresh-vix', received_at=NOW)
    return engine, exits, store, runtime, writer, supplied


def quote(code, *, pct=0.0, high=102.0):
    values = {'price': 100.0, 'open': 100.0, 'high': high, 'low': 98.0, 'change': 0.0, 'change_pct': pct}
    return {**values, '_observation': {'schema_version': 1, 'source': 'kis',
        'source_tr': 'FHPUP02100000', 'index_code': code, 'observation_id': str(uuid4()),
        'received_at': NOW.isoformat(), 'market_as_of': None,
        'fields': {k: {'status': 'valid', 'value': v} for k, v in values.items()}}}


def test_existing_token_begin_drains_shutdown_during_lookup(tmp_path, monkeypatch):
    async def scenario():
        _, _, store, runtime = await setup(tmp_path, account_scope='scope')
        source = RiskSourceCoordinator(runtime)
        reached, release = asyncio.Event(), asyncio.Event()
        original = store.lookup_commit
        async def lookup(*args, **kwargs):
            reached.set(); await release.wait(); return await original(*args, **kwargs)
        monkeypatch.setattr(store, 'lookup_commit', lookup)
        async def worker():
            with runtime.command_scope() as token:
                ticket = await source.begin('scoped', 'vix_regime', scope_token=token)
                return await source.complete(ticket, 'missing', scope_token=token)
        task = asyncio.create_task(worker())
        try:
            done, _ = await asyncio.wait((task, asyncio.create_task(reached.wait())), timeout=1,
                                         return_when=asyncio.FIRST_COMPLETED)
            if task in done: await task
            closing = asyncio.create_task(runtime.shutdown())
            await asyncio.sleep(0); release.set()
            assert (await task).status == 'missing'
            await closing
            assert runtime.owner.healthy and not runtime._command_results_failed
        finally:
            release.set(); await asyncio.gather(task, return_exceptions=True); await store.close()
    asyncio.run(scenario())


def test_foreign_begin_token_has_no_pending_or_durable_write(tmp_path):
    async def scenario():
        _, _, store, runtime = await setup(tmp_path, account_scope='scope')
        source = RiskSourceCoordinator(runtime)
        try:
            before = runtime.owner.state
            with runtime.command_scope() as token:
                async def foreign():
                    with pytest.raises(ApplicationBlocked, match='command_scope_required'):
                        await source.begin('foreign', 'vix_regime', scope_token=token)
                await asyncio.create_task(foreign())
            assert runtime.owner.state == before and not runtime._risk_source_pending
        finally: await runtime.shutdown(); await store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('bad', [False, True])
def test_refresh_publishes_owned_policy_or_preserves_it_for_invalid_index(tmp_path, bad):
    async def scenario():
        engine, exits, store, runtime, writer, _ = await owned(tmp_path)
        before, protection = runtime.owner.state['regime_policy'], runtime.owner.state['protection']
        calls = []
        async def fetch(code):
            calls.append(code)
            return quote(code, high=90.0 if bad and code == '1001' else 102.0)
        try:
            receipt = await writer.refresh_trend(SimpleNamespace(fetch_index_price=fetch))
            assert calls == ['0001', '1001']
            assert receipt.status == ('missing' if bad else 'accepted')
            state = runtime.owner.state
            assert state['protection'] == protection and not state['outbox']
            if bad:
                assert state['regime_policy'] == before and writer.sidecar._sidecar_active
            else:
                assert not writer.sidecar._sidecar_active
                assert writer.sidecar._market_trend['recovering'] is True
                assert state['regime_policy']['transitions'][receipt.operation_id]['version'] == receipt.committed_version
                assert engine._market_regime == 'sideways'
                assert (await store.load())[1] == state
                await runtime.restore()
                assert writer.sidecar._market_trend['recovering'] is True
            assert not runtime.trading_ready
        finally: await runtime.shutdown(); await store.close()
    asyncio.run(scenario())


def test_baseline_is_detached_future_rejected_at_registration_and_raw_writers_block(tmp_path):
    async def scenario():
        from src.execution.safety.regime_owner import RegimeBaseline, RegimeOwner
        _, _, store, runtime, writer, supplied = await owned(tmp_path)
        try:
            dto = RegimeBaseline.from_dict(supplied)
            supplied['trend_state']['mid_regime'] = 'bull'
            assert dto.to_dict()['trend_state']['mid_regime'] == 'sideways'
            future = dto.to_dict(); future['evidence']['observed_at'] = (NOW + timedelta(days=1)).isoformat()
            decoded = RegimeBaseline.from_dict(future)
            with pytest.raises(ValueError, match='future'):
                await RegimeOwner.register_baseline(runtime, decoded, expected_version=runtime.owner.version)
            with pytest.raises(ApplicationBlocked): writer.adapter.update_regime({}, {})
            with pytest.raises(ApplicationBlocked): writer.adapter.apply_expert_adjustment(None)
            with pytest.raises(ApplicationBlocked): writer.sidecar.update_market_trend({}, {})
        finally: await runtime.shutdown(); await store.close()
    asyncio.run(scenario())


def test_owned_snapshot_ignores_optimistic_context_and_rejects_latest_failure(tmp_path):
    async def scenario():
        from dataclasses import replace
        from src.execution.safety.policy_snapshot import PolicyContext, build_owned_snapshot
        from src.execution.safety import risk_policy
        from test_execution_risk_policy import snapshot
        _, _, store, runtime, writer, _ = await owned(tmp_path)
        async def fetch(code): return quote(code, pct=-2)
        try:
            receipt = await writer.refresh_trend(SimpleNamespace(fetch_index_price=fetch))
            context = PolicyContext.from_snapshot(snapshot(risk_policy))
            context = replace(context, business_day=NOW.date(), observed_at=NOW,
                trend=risk_policy.MarketTrendPolicySnapshot(True, True, False))
            result = build_owned_snapshot(runtime.owner.state, context=context, version=runtime.owner.version, now=NOW, prices={})
            assert result.trend.recovering is False and result.trend.sidecar_active is True
            assert result.versions.regime == receipt.committed_version
            ticket = await writer.sources.begin('latest-failure', 'index_trend')
            await writer.sources.complete(ticket, 'failed')
            with pytest.raises(ApplicationBlocked, match='regime_source_not_current'):
                build_owned_snapshot(runtime.owner.state, context=context, version=runtime.owner.version, now=NOW, prices={})
        finally: await runtime.shutdown(); await store.close()
    asyncio.run(scenario())


def test_scheduler_helper_uses_bound_owner_and_missing_owner_blocks(tmp_path):
    async def scenario():
        from src.schedulers.kr_scheduler import KRScheduler
        engine, _, store, runtime, writer, _ = await owned(tmp_path)
        scheduler = object.__new__(KRScheduler)
        async def fetch(code): return quote(code)
        scheduler.bot = SimpleNamespace(engine=engine, risk_manager=writer.sidecar,
            kis_market_data=SimpleNamespace(fetch_index_price=fetch), expert_orchestrator=None)
        try:
            assert (await scheduler._refresh_market_trend()).status == 'accepted'
            runtime._regime_writer = None
            with pytest.raises(ApplicationBlocked, match='regime_owner_not_installed'):
                await scheduler._refresh_market_trend()
        finally: await runtime.shutdown(); await store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('change', ['aba', 'vix', 'unrelated'])
def test_captured_reads_detect_seal_lookup_races_without_global_version_rejection(tmp_path, monkeypatch, change):
    async def scenario():
        _, _, store, runtime, writer, _ = await owned(tmp_path)
        source = writer.sources
        ticket = await source.begin('capture-race', 'llm_regime', require_seal=True)
        selected = dict(source_lanes=('vix_regime',), versioned_policy_reads=('entry_policy_effects.sidecar_active',))
        capture = source.capture_reads(ticket, **selected)
        reached, release = asyncio.Event(), asyncio.Event()
        original = runtime.owner.mutate
        async def lookup(command, reducer):
            if command.startswith('risk-seal:'):
                reached.set(); await release.wait()
            return await original(command, reducer)
        monkeypatch.setattr(runtime.owner, 'mutate', lookup)
        task = asyncio.create_task(source.seal(ticket, **selected, inputs={}, expected_reads_json=capture))
        try:
            await asyncio.wait_for(reached.wait(), 2)
            if change == 'vix':
                newer = await source.begin('replacement', 'vix_regime')
                await source.complete(newer, 'failed')
            else:
                def mutate(value):
                    def reduce(state):
                        state['entry_policy_effects']['sidecar_active'] = value
                        return state
                    return reduce
                if change == 'aba':
                    await runtime.owner.mutate('off', mutate(False))
                    await runtime.owner.mutate('on', mutate(True))
                else: await runtime.owner.mutate('unrelated', lambda state: state)
            release.set(); seal = await task
            receipt = await source.complete(ticket, 'success', {})
            if change == 'unrelated': assert seal.status == 'sealed' and receipt.status == 'accepted'
            else:
                assert (seal.status, seal.reason) == ('stale', 'captured_reads_changed')
                assert (receipt.status, receipt.reason) == ('stale', 'input_seal')
            await runtime.restore()
            assert runtime.owner.healthy and not runtime._command_results_failed
        finally:
            release.set(); await asyncio.gather(task, return_exceptions=True)
            await runtime.shutdown(); await store.close()
    asyncio.run(scenario())


def test_malformed_capture_rejected_before_result_admission(tmp_path):
    async def scenario():
        _, _, store, runtime, writer, _ = await owned(tmp_path)
        try:
            ticket = await writer.sources.begin('bad-capture', 'llm_regime')
            before = runtime.owner.state
            for value in ('{}', '[]', 'not json'):
                with pytest.raises(ValueError):
                    await writer.sources.seal(ticket, inputs={}, expected_reads_json=value)
            assert runtime.owner.state == before and not runtime._command_results_failed
        finally: await runtime.shutdown(); await store.close()
    asyncio.run(scenario())


def test_technical_calculation_precedes_expert_calls(tmp_path, monkeypatch):
    async def scenario():
        from src.utils import regime_transition
        _, _, store, runtime, writer, _ = await owned(tmp_path)
        order = []
        original = regime_transition.complete_mid_regime_transition
        def technical(*args):
            order.append('technical'); return original(*args)
        monkeypatch.setattr(regime_transition, 'complete_mid_regime_transition', technical)
        class Experts:
            def aggregate_regime_score(self): order.append('score'); return 0
            def bear_consensus(self, **kwargs):
                assert kwargs == {'threshold_confidence': .7, 'min_count': 2}
                order.append('consensus'); return False
        async def fetch(code): return quote(code)
        try:
            assert (await writer.refresh_trend(SimpleNamespace(fetch_index_price=fetch), expert_orchestrator=Experts())).status == 'accepted'
            # Publisher/restore may replay pure arithmetic; provider calls remain once.
            assert order.index('technical') < order.index('score') < order.index('consensus')
            assert order.count('score') == order.count('consensus') == 1
        finally: await runtime.shutdown(); await store.close()
    asyncio.run(scenario())


def test_refresh_cancelled_after_provider_still_records_terminal_and_drains(tmp_path, monkeypatch):
    async def scenario():
        _, _, store, runtime, writer, _ = await owned(tmp_path)
        reached, release = asyncio.Event(), asyncio.Event()
        original = writer.sources.seal
        async def seal(*args, **kwargs):
            reached.set(); await release.wait(); return await original(*args, **kwargs)
        monkeypatch.setattr(writer.sources, 'seal', seal)
        async def fetch(code): return quote(code)
        task = asyncio.create_task(writer.refresh_trend(SimpleNamespace(fetch_index_price=fetch)))
        try:
            await asyncio.wait_for(reached.wait(), 2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError): await task
            records = runtime.owner.state['risk_sources']
            row = records['records'][records['latest']['index_trend']]
            assert row['terminal']['receipt']['status'] == 'cancelled'
            await runtime.shutdown()
            assert not runtime._command_results_failed
        finally:
            release.set(); await asyncio.gather(task, return_exceptions=True); await store.close()
    asyncio.run(scenario())


def test_vix_runner_closing_before_start_skips_and_retains_owner_health(tmp_path):
    async def scenario():
        calls = []
        async def fetch(): calls.append(True); return 20.0
        _, _, store, runtime, writer, _ = await owned(tmp_path, fetch_vix=fetch)
        try:
            before = runtime.owner.state
            with runtime.command_scope() as token:
                writer._schedule_vix(token)
                runtime._closing = True
            await runtime.shutdown()
            assert calls == [] and runtime.owner.state == before
            assert runtime.owner.healthy and not runtime._command_results_failed
        finally: await store.close()
    asyncio.run(scenario())


def test_cancelled_begin_scope_does_not_reject_already_accepted_sql(tmp_path, monkeypatch):
    async def scenario():
        _, _, store, runtime = await setup(tmp_path, account_scope='scope')
        source = RiskSourceCoordinator(runtime)
        reached, release = asyncio.Event(), asyncio.Event()
        original = store.lookup_commit
        async def lookup(*args):
            reached.set(); await release.wait(); return await original(*args)
        monkeypatch.setattr(store, 'lookup_commit', lookup)
        async def worker():
            with runtime.command_scope() as token:
                return await source.begin('cancelled-begin', 'vix_regime', scope_token=token)
        task = asyncio.create_task(worker())
        try:
            await asyncio.wait_for(reached.wait(), 2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError): await task
            assert not runtime._command_scopes and runtime._risk_source_pending
            shutdown = asyncio.create_task(runtime.shutdown())
            await asyncio.sleep(0); assert not shutdown.done()
            release.set(); await shutdown
            assert not runtime._risk_source_pending and runtime.owner.healthy
            assert 'cancelled-begin' in runtime.owner.state['risk_sources']['records']
        finally: release.set(); await asyncio.gather(task, return_exceptions=True); await store.close()
    asyncio.run(scenario())


def test_coordinated_after_and_payload_rewrite_cannot_change_pure_arithmetic(tmp_path):
    async def scenario():
        from src.execution.safety.regime_owner import validate_regime_policy
        from src.execution.safety.protection_recovery import digest
        _, _, store, runtime, writer, _ = await owned(tmp_path)
        async def fetch(code): return quote(code)
        try:
            receipt = await writer.refresh_trend(SimpleNamespace(fetch_index_price=fetch))
            state = runtime.owner.state
            root = state['regime_policy']; row = root['transitions'][receipt.operation_id]
            terminal = state['risk_sources']['records'][receipt.operation_id]['terminal']
            for trend in (root['trend_state'], row['after'], terminal['envelope']['payload']['after']):
                trend['mid_regime'] = 'bear'
            terminal['receipt']['outcome_digest'] = row['outcome_digest'] = digest(terminal['envelope'])
            with pytest.raises(ValueError, match='calculation'):
                validate_regime_policy(state, runtime.owner.version)
        finally: await runtime.shutdown(); await store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('bound', [True, False])
def test_invalid_vix_projection_fails_before_first_live_write(tmp_path, bound):
    async def scenario():
        from src.execution.safety.protection_recovery import digest
        engine, _, store, runtime, writer, _ = await owned(tmp_path)
        try:
            before = engine.portfolio.cash
            state = runtime.owner.state
            state['portfolio']['cash'] = '123456'
            terminal = state['risk_sources']['records']['fresh-vix']['terminal']
            terminal['envelope']['payload'] = {'value': 'bad', 'fetched_at': NOW.isoformat()}
            terminal['receipt']['outcome_digest'] = digest(terminal['envelope'])
            if not bound: runtime._regime_writer = None
            with pytest.raises(ValueError): runtime._publish(state, runtime.owner.version)
            assert engine.portfolio.cash == before
        finally: await runtime.shutdown(); await store.close()
    asyncio.run(scenario())


def test_captured_source_json_distinguishes_boolean_from_number(tmp_path):
    async def scenario():
        import json
        _, _, store, runtime = await setup(tmp_path, account_scope='scope')
        source = RiskSourceCoordinator(runtime)
        try:
            vix = await source.begin('vix', 'vix_regime')
            await source.complete(vix, 'success', {'value': 1})
            ticket = await source.begin('consumer', 'llm_regime')
            captured = json.loads(source.capture_reads(ticket, source_lanes=('vix_regime',)))
            captured['sources']['vix_regime']['terminal']['envelope']['payload']['value'] = True
            before = runtime.owner.state
            with pytest.raises(ValueError):
                await source.seal(ticket, source_lanes=('vix_regime',), inputs={}, expected_reads_json=json.dumps(captured))
            assert runtime.owner.state == before and not runtime._command_results_failed
        finally: await runtime.shutdown(); await store.close()
    asyncio.run(scenario())


def test_installed_other_raw_regime_writers_are_blocked(tmp_path):
    async def scenario():
        _, _, store, runtime, writer, _ = await owned(tmp_path)
        try:
            with pytest.raises(ApplicationBlocked): writer.adapter.set_intraday_risk('crash', -3.0, NOW)
            with pytest.raises(ApplicationBlocked): writer.adapter.set_open_expectation('bull', NOW)
        finally: await runtime.shutdown(); await store.close()
    asyncio.run(scenario())


def test_installed_sidecar_without_regime_owner_cannot_fall_through(tmp_path):
    async def scenario():
        sidecar = RiskManager(RiskConfig(), Decimal('2000000'))
        _, _, store, runtime = await setup(tmp_path, account_scope='scope', risk_manager=sidecar)
        try:
            with pytest.raises(ApplicationBlocked): sidecar.update_market_trend({}, {})
        finally: await runtime.shutdown(); await store.close()
    asyncio.run(scenario())


def test_reverse_refresh_completion_is_stale_without_extra_gets(tmp_path):
    async def scenario():
        _, _, store, runtime, writer, _ = await owned(tmp_path)
        reached, release = asyncio.Event(), asyncio.Event()
        calls = []
        async def old_fetch(code):
            calls.append(code); reached.set(); await release.wait(); return quote(code)
        async def fresh_fetch(code): return quote(code)
        task = asyncio.create_task(writer.refresh_trend(SimpleNamespace(fetch_index_price=old_fetch)))
        try:
            await asyncio.wait_for(reached.wait(), 2)
            assert (await writer.refresh_trend(SimpleNamespace(fetch_index_price=fresh_fetch))).status == 'accepted'
            before = runtime.owner.state['regime_policy']
            release.set()
            assert (await task).status == 'stale'
            assert runtime.owner.state['regime_policy'] == before and calls == ['0001', '1001']
        finally:
            release.set(); await asyncio.gather(task, return_exceptions=True)
            await runtime.shutdown(); await store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('seam', ['lookup_commit', 'commit'])
def test_scoped_begin_sql_fault_retains_failed_latch(tmp_path, monkeypatch, seam):
    async def scenario():
        _, _, store, runtime = await setup(tmp_path, account_scope='scope')
        source = RiskSourceCoordinator(runtime)
        async def fault(*args): raise RuntimeError('synthetic storage fault')
        monkeypatch.setattr(store, seam, fault)
        try:
            with runtime.command_scope() as token:
                with pytest.raises(RuntimeError, match='synthetic storage fault'):
                    await source.begin('fault', 'vix_regime', scope_token=token)
            with pytest.raises(ApplicationBlocked, match='command_result_drain_failed'): await runtime.shutdown()
            assert not runtime.owner.healthy and runtime._command_results_failed
        finally: await store.close()
    asyncio.run(scenario())


def test_expert_terminal_replaced_before_capture_cannot_publish_other_fact(tmp_path, monkeypatch):
    async def scenario():
        _, _, store, runtime, writer, _ = await owned(tmp_path)
        source = RiskSourceCoordinator(runtime)
        original = writer.sources.complete
        async def complete(ticket, *args, **kwargs):
            result = await original(ticket, *args, **kwargs)
            if ticket.kind == 'expert_regime':
                other = await source.begin('other-expert', 'expert_regime')
                await source.complete(other, 'success', {'score': -30, 'bear_consensus': True})
            return result
        monkeypatch.setattr(writer.sources, 'complete', complete)
        async def fetch(code): return quote(code)
        experts = SimpleNamespace(aggregate_regime_score=lambda: 0, bear_consensus=lambda **kwargs: False)
        try:
            before = runtime.owner.state['regime_policy']
            assert (await writer.refresh_trend(SimpleNamespace(fetch_index_price=fetch), expert_orchestrator=experts)).status == 'stale'
            assert runtime.owner.state['regime_policy'] == before and runtime.owner.healthy
        finally:
            # A failing RED publication deliberately leaves the runtime blocked.
            if runtime.owner.healthy: await runtime.shutdown()
            await store.close()
    asyncio.run(scenario())


def test_cancel_during_terminal_sql_drains_original_without_conflicting_outcome(tmp_path, monkeypatch):
    async def scenario():
        _, _, store, runtime, writer, _ = await owned(tmp_path)
        reached, release = asyncio.Event(), asyncio.Event()
        original = store.commit
        async def commit(version, state, command):
            if state.get('regime_policy', {}).get('transitions'):
                reached.set(); await release.wait()
            return await original(version, state, command)
        monkeypatch.setattr(store, 'commit', commit)
        async def fetch(code): return quote(code)
        task = asyncio.create_task(writer.refresh_trend(SimpleNamespace(fetch_index_price=fetch)))
        try:
            await asyncio.wait_for(reached.wait(), 2)
            task.cancel(); await asyncio.sleep(0); release.set()
            with pytest.raises(asyncio.CancelledError): await task
            await runtime.shutdown()
            root = runtime.owner.state['risk_sources']
            row = root['records'][root['latest']['index_trend']]
            assert row['terminal']['receipt']['status'] == 'accepted' and row['conflict'] is None
        finally:
            release.set(); await asyncio.gather(task, return_exceptions=True); await store.close()
    asyncio.run(scenario())


async def registration_fixture(tmp_path, clock):
    _, _, store, runtime = await setup(tmp_path, account_scope='scope', clock=clock)
    def seed(state):
        value = IntradayPolicyState('normal', 0.0, None, None).to_dict()
        state['intraday_policy'] = {'schema': 1, 'baseline': value,
            'baseline_version': runtime.owner.version + 1, 'current': value.copy(), 'transitions': {}}
        state['entry_policy_effects'] = {'pending_sectors': {}, 'sidecar_active': True}
        return state
    await runtime.owner.mutate('registration-prerequisites', seed)
    return store, runtime


@pytest.mark.parametrize('race', ['version', 'day', 'different_baseline'])
def test_registration_race_rejects_without_poisoning_durable_results(tmp_path, monkeypatch, race):
    async def scenario():
        from src.execution.safety.regime_owner import RegimeBaseline, RegimeOwner
        current = [NOW]
        store, runtime = await registration_fixture(tmp_path, lambda: current[0])
        supplied = baseline_json(runtime); expected = runtime.owner.version
        reached, release = asyncio.Event(), asyncio.Event()
        original = runtime.owner.mutate
        held = False
        async def mutate(command, reducer):
            nonlocal held
            if command.startswith('regime-baseline:') and not held:
                held = True; reached.set(); await release.wait()
            return await original(command, reducer)
        monkeypatch.setattr(runtime.owner, 'mutate', mutate)
        task = asyncio.create_task(RegimeOwner.register_baseline(runtime, RegimeBaseline.from_dict(supplied), expected_version=expected))
        try:
            await asyncio.wait_for(reached.wait(), 2)
            if race == 'version': await runtime.owner.mutate('unrelated', lambda state: state)
            elif race == 'day': current[0] += timedelta(days=1)
            else:
                other = deepcopy(supplied); other['baseline_id'] = 'other-known'
                await RegimeOwner.register_baseline(runtime, RegimeBaseline.from_dict(other), expected_version=expected)
            release.set()
            with pytest.raises((ValueError, ApplicationBlocked)): await task
            assert ('regime_policy' in runtime.owner.state) is (race == 'different_baseline')
            assert runtime.owner.healthy and not runtime._command_results_failed
            current[0] = NOW
            await runtime.shutdown()
        finally:
            release.set(); await asyncio.gather(task, return_exceptions=True); await store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('kind', ['index_trend', 'expert_regime'])
def test_cancelled_owner_begin_returns_promptly_then_terminalizes_on_drain(tmp_path, monkeypatch, kind):
    async def scenario():
        _, _, store, runtime, writer, _ = await owned(tmp_path)
        reached, release = asyncio.Event(), asyncio.Event()
        original = store.lookup_commit
        calls = []
        async def lookup(command):
            pending = runtime._risk_source_pending.values()
            if command.startswith('command:risk-begin:') and kind in pending:
                reached.set(); await release.wait()
            return await original(command)
        monkeypatch.setattr(store, 'lookup_commit', lookup)
        async def fetch(code): calls.append(code); return quote(code)
        task = asyncio.create_task(writer.refresh_trend(SimpleNamespace(fetch_index_price=fetch)))
        try:
            await asyncio.wait_for(reached.wait(), 2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError): await asyncio.wait_for(task, 2)
            assert runtime._risk_source_pending
            closing = asyncio.create_task(runtime.shutdown())
            await asyncio.sleep(0); assert not closing.done()
            release.set(); await asyncio.wait_for(closing, 2)
            rows = [row for row in runtime.owner.state['risk_sources']['records'].values()
                    if row['ticket']['lane'] in ('index_trend', 'expert_regime')]
            assert rows and all(row['terminal'] is not None for row in rows)
            assert all(row['terminal']['receipt']['status'] == 'cancelled' for row in rows)
            assert calls == ([] if kind == 'index_trend' else ['0001', '1001'])
            assert runtime.owner.healthy and not runtime._command_results_failed
        finally:
            release.set(); await asyncio.gather(task, return_exceptions=True); await store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('failure', ['prestart_cancel', 'task_error'])
def test_finalizer_releases_child_scope_but_preserves_failure_latch(tmp_path, failure):
    async def scenario():
        _, _, store, runtime = await setup(tmp_path, account_scope='scope')
        seen = []
        async def operation(child):
            seen.append(child)
            assert runtime._command_scopes[child] is asyncio.current_task()
            raise RuntimeError('synthetic finalizer fault')
        try:
            with runtime.command_scope() as parent:
                task = runtime._start_command_finalizer(parent, operation)
                assert len(runtime._command_scopes) == 2
                if failure == 'prestart_cancel': task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            assert not runtime._command_scopes
            assert bool(seen) is (failure == 'task_error')
            with pytest.raises(ApplicationBlocked, match='command_result_drain_failed'): await runtime.shutdown()
            assert runtime._command_results_failed and not runtime.owner.healthy
        finally: await store.close()
    asyncio.run(scenario())


def test_begin_observer_receives_original_registered_task_before_first_await(tmp_path, monkeypatch):
    async def scenario():
        _, _, store, runtime = await setup(tmp_path, account_scope='scope')
        source = RiskSourceCoordinator(runtime)
        seen = []
        original = store.lookup_commit
        async def lookup(command):
            assert len(seen) == 1 and seen[0] in runtime._command_result_tasks
            return await original(command)
        monkeypatch.setattr(store, 'lookup_commit', lookup)
        try:
            ticket = await source.begin('observed', 'index_trend', _admitted_task_observer=seen.append)
            assert ticket.operation_id == 'observed' and seen[0].result() is True
        finally: await runtime.shutdown(); await store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('kind', ['index_trend', 'expert_regime'])
def test_cancelled_begin_original_sql_failure_is_not_normalized(tmp_path, monkeypatch, kind):
    async def scenario():
        _, _, store, runtime, writer, _ = await owned(tmp_path)
        reached, release = asyncio.Event(), asyncio.Event()
        original = store.lookup_commit; failed = []
        async def lookup(command):
            if command.startswith('command:risk-begin:') and kind in runtime._risk_source_pending.values():
                failed.append(command); reached.set(); await release.wait()
                raise OSError('synthetic original lookup fault')
            return await original(command)
        monkeypatch.setattr(store, 'lookup_commit', lookup)
        async def fetch(code): return quote(code)
        task = asyncio.create_task(writer.refresh_trend(SimpleNamespace(fetch_index_price=fetch)))
        try:
            await asyncio.wait_for(reached.wait(), 2); task.cancel()
            with pytest.raises(asyncio.CancelledError): await asyncio.wait_for(task, 2)
            release.set()
            with pytest.raises(ApplicationBlocked, match='command_result_drain_failed'):
                await asyncio.wait_for(runtime.shutdown(), 2)
            assert len(failed) == 1 and runtime._command_results_failed and not runtime.owner.healthy
            assert not runtime._command_scopes and not runtime._risk_source_pending
        finally: release.set(); await asyncio.gather(task, return_exceptions=True); await store.close()
    asyncio.run(scenario())


def test_cancel_finalizer_uses_original_ticket_after_day_fence_closes(tmp_path, monkeypatch):
    async def scenario():
        _, _, store, runtime, writer, _ = await owned(tmp_path)
        reached, release = asyncio.Event(), asyncio.Event()
        original = store.commit
        begun = []
        async def commit(version, state, command):
            result = await original(version, state, command)
            if command.startswith('command:risk-begin:'):
                begun.append(command); reached.set(); await release.wait()
            return result
        monkeypatch.setattr(store, 'commit', commit)
        async def fetch(code): pytest.fail('provider must not start after cancelled begin')
        task = asyncio.create_task(writer.refresh_trend(SimpleNamespace(fetch_index_price=fetch)))
        try:
            await asyncio.wait_for(reached.wait(), 2); task.cancel()
            with pytest.raises(asyncio.CancelledError): await asyncio.wait_for(task, 2)
            # Isolate the post-commit fence boundary; independent review uses actual prepare_day.
            runtime._day_closed = True
            runtime._day_generation += 1
            runtime._day_fence_id = 'synthetic-next-fence'
            release.set(); await asyncio.wait_for(runtime.shutdown(), 2)
            root = runtime.owner.state['risk_sources']
            row = root['records'][root['latest']['index_trend']]
            assert len(begun) == 1 and row['ticket']['generation'] == 0
            assert row['terminal']['envelope']['outcome'] == 'cancelled'
            assert (row['terminal']['receipt']['status'], row['terminal']['receipt']['reason']) == ('stale', 'day_or_generation')
            assert runtime.owner.healthy and not runtime._command_results_failed
        finally: release.set(); await asyncio.gather(task, return_exceptions=True); await store.close()
    asyncio.run(scenario())


def test_finalizer_rejects_foreign_token_and_bad_begin_observer_before_admission(tmp_path):
    async def scenario():
        _, _, store, runtime = await setup(tmp_path, account_scope='scope')
        source = RiskSourceCoordinator(runtime)
        async def operation(child): return True
        try:
            before = runtime.owner.state
            with runtime.command_scope() as parent:
                async def foreign():
                    with pytest.raises(ApplicationBlocked, match='command_scope_required'):
                        runtime._start_command_finalizer(parent, operation)
                await asyncio.create_task(foreign())
                with pytest.raises(TypeError, match='admitted_task_observer_required'):
                    await source.begin('bad-observer', 'index_trend', scope_token=parent, _admitted_task_observer=3)
            assert runtime.owner.state == before and not runtime._command_result_tasks and not runtime._risk_source_pending
        finally: await runtime.shutdown(); await store.close()
    asyncio.run(scenario())


def test_cancelled_vix_begin_worker_transfers_terminal_without_provider_or_latch(tmp_path, monkeypatch):
    async def scenario():
        calls = []
        async def fetch(): calls.append('vix'); return 20.0
        _, _, store, runtime, writer, _ = await owned(tmp_path, fetch_vix=fetch)
        reached, release = asyncio.Event(), asyncio.Event()
        original = store.lookup_commit
        async def lookup(command):
            if command.startswith('command:risk-begin:'):
                reached.set(); await release.wait()
            return await original(command)
        monkeypatch.setattr(store, 'lookup_commit', lookup)
        try:
            with runtime.command_scope() as token:
                writer._schedule_vix(token)
            await asyncio.wait_for(reached.wait(), 2)
            worker = writer._vix_refresh_task
            worker.cancel()
            assert await asyncio.wait_for(worker, 2) is True
            release.set(); await asyncio.wait_for(runtime.shutdown(), 2)
            root = runtime.owner.state['risk_sources']
            row = root['records'][root['latest']['vix_regime']]
            assert row['terminal']['receipt']['status'] == 'cancelled' and row['conflict'] is None
            assert not calls and not runtime._command_scopes and not runtime._risk_source_pending
            assert runtime.owner.healthy and not runtime._command_results_failed
        finally: release.set(); await store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('error_type', [OSError, ValueError])
def test_baseline_genuine_storage_exception_is_not_expected_rejection(tmp_path, monkeypatch, error_type):
    async def scenario():
        from src.execution.safety.regime_owner import RegimeBaseline, RegimeOwner
        store, runtime = await registration_fixture(tmp_path, lambda: NOW)
        supplied = baseline_json(runtime)
        async def lookup(command): raise error_type('genuine storage fault')
        monkeypatch.setattr(store, 'lookup_commit', lookup)
        try:
            with pytest.raises(error_type, match='genuine storage fault'):
                await RegimeOwner.register_baseline(runtime, RegimeBaseline.from_dict(supplied),
                    expected_version=runtime.owner.version)
            with pytest.raises(ApplicationBlocked, match='command_result_drain_failed'):
                await runtime.shutdown()
            assert runtime._command_results_failed and not runtime.owner.healthy
        finally: await store.close()
    asyncio.run(scenario())
