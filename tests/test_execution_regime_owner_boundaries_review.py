"""독립 경계 재현: 실제 owner/SQLite, 외부 I/O는 합성 주입만 사용."""
import asyncio
from copy import deepcopy
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.core.market_regime import MarketRegimeAdapter
from src.core.types import RiskConfig
from src.execution.safety.application import ApplicationBlocked
from src.execution.safety.regime_owner import POLICY_READS, RegimeBaseline, RegimeOwner
from src.execution.safety.risk_transition import IntradayPolicyState
from src.risk.manager import RiskManager
from test_execution_regime_owner import baseline_json, quote
from test_execution_runtime import NOW, setup


@pytest.fixture(autouse=True)
def synthetic_home(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)


async def prerequisites(tmp_path, clock=None):
    sidecar = RiskManager(RiskConfig(), Decimal('2000000'))
    engine, exits, store, runtime = await setup(
        tmp_path, account_scope='scope', risk_manager=sidecar, clock=clock)
    engine._regime_adapter = MarketRegimeAdapter()

    def seed(state):
        value = IntradayPolicyState('normal', 0.0, None, None).to_dict()
        state['intraday_policy'] = {'schema': 1, 'baseline': value,
            'baseline_version': runtime.owner.version + 1,
            'current': value.copy(), 'transitions': {}}
        state['entry_policy_effects'] = {'pending_sectors': {}, 'sidecar_active': True}
        return state

    await runtime.owner.mutate('boundary-known-prerequisites', seed)
    return engine, exits, store, runtime


async def install(tmp_path, clock, *, modify=None, vix=20.0, vix_time=None, fetcher=None):
    engine, exits, store, runtime = await prerequisites(tmp_path, clock)
    supplied = baseline_json(runtime)
    when = clock().isoformat()
    supplied['evidence']['observed_at'] = when
    supplied['trend_state']['market_trend']['classified_at'] = when
    supplied['trend_state']['last_update'] = when
    if modify is not None:
        modify(supplied)
    await RegimeOwner.register_baseline(runtime, RegimeBaseline.from_dict(supplied),
                                      expected_version=runtime.owner.version)
    await runtime.owner.register_policy_generations('boundary-reads', POLICY_READS)
    async def no_vix():
        return None
    writer = RegimeOwner(runtime, adapter=engine._regime_adapter, sidecar=runtime.risk_manager,
                         vix_fetcher=fetcher or no_vix)
    if vix is not None:
        fetched = vix_time or clock()
        ticket = await writer.sources.begin('boundary-vix', 'vix_regime')
        await writer.sources.complete(ticket, 'success',
            {'value': vix, 'fetched_at': fetched.isoformat()},
            source='synthetic', source_event_id='boundary-vix', received_at=fetched)
    return engine, exits, store, runtime, writer


def provider(clock, *, bull=False, calls=None):
    async def fetch(code):
        if calls is not None:
            calls.append(code)
        value = quote(code, pct=1.2 if bull else 0.0)
        value['_observation']['received_at'] = clock().isoformat()
        if bull:
            value['price'] = 100.5
            value['_observation']['fields']['price']['value'] = 100.5
        return value
    return SimpleNamespace(fetch_index_price=fetch)


def hold_lookup(monkeypatch, store, predicate):
    entered, release = asyncio.Event(), asyncio.Event()
    original = store.lookup_commit
    async def lookup(commit_id):
        if predicate(commit_id):
            entered.set()
            await release.wait()
        return await original(commit_id)
    monkeypatch.setattr(store, 'lookup_commit', lookup)
    return entered, release


@pytest.mark.parametrize('race', ['version', 'day', 'account'])
def test_baseline_normal_race_rejection_does_not_poison_result_latch(tmp_path, monkeypatch, race):
    async def scenario():
        current = [NOW]
        _, _, store, runtime = await prerequisites(tmp_path, lambda: current[0])
        baseline = RegimeBaseline.from_dict(baseline_json(runtime))
        expected = runtime.owner.version
        target = 'command:before-registration' if race == 'version' else 'command:regime-baseline:'
        entered, release = hold_lookup(monkeypatch, store, lambda key: key.startswith(target))
        preceding = None
        task = None
        try:
            if race == 'version':
                preceding = asyncio.create_task(runtime.owner.mutate('before-registration', lambda state: state))
                await asyncio.wait_for(entered.wait(), 2)
                admitted = asyncio.Event()
                original = runtime.start_command_result
                def start(token, operation):
                    result = original(token, operation)
                    admitted.set()
                    return result
                monkeypatch.setattr(runtime, 'start_command_result', start)
            task = asyncio.create_task(RegimeOwner.register_baseline(runtime, baseline, expected_version=expected))
            await asyncio.wait_for(admitted.wait() if race == 'version' else entered.wait(), 2)
            if race == 'day':
                current[0] += timedelta(days=1)
            elif race == 'account':
                runtime.account_scope = 'other-synthetic-scope'
            release.set()
            if preceding is not None:
                await preceding
            reason = 'day_transition_admission_closed' if race == 'day' else 'regime_baseline_(version|context)_conflict'
            with pytest.raises((ValueError, ApplicationBlocked), match=reason):
                await task
            assert 'regime_policy' not in runtime.owner.state
            # 일자/account 변경을 되돌려도 저장 장애가 된 것은 아니어야 한다.
            current[0] = NOW
            runtime.account_scope = 'scope'
            shutdown_reason = None
            try:
                await runtime.shutdown()
            except ApplicationBlocked as exc:
                shutdown_reason = str(exc)
            assert (runtime._command_results_failed, runtime.owner.healthy, shutdown_reason) == (False, True, None), (
                'normal baseline rejection poisoned durable-result latch and shutdown')
        finally:
            release.set()
            await asyncio.gather(*(t for t in (task, preceding) if t is not None), return_exceptions=True)
            await store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('seed_old', [False, True])
def test_actual_vix_worker_starts_after_trend_publish_and_first_await_is_pending(tmp_path, monkeypatch, seed_old):
    async def scenario():
        fetched_entered, finish_fetch = asyncio.Event(), asyncio.Event()
        fetch_calls = []
        async def fetch_vix():
            fetch_calls.append('vix')
            state = runtime.owner.state
            latest = state['risk_sources']['latest']['vix_regime']
            assert state['risk_sources']['records'][latest]['terminal'] is None
            assert state['regime_policy']['engine_projection']['operation_id'] is not None
            fetched_entered.set()
            await finish_fetch.wait()
            return 40.0
        old_time = NOW - timedelta(hours=6)
        _, _, store, runtime, writer = await install(tmp_path, lambda: NOW,
            vix=20.0 if seed_old else None, vix_time=old_time, fetcher=fetch_vix)
        worker_scheduled = [False]
        original_schedule = writer._schedule_vix
        def schedule(token):
            # 제품 메서드 호출 시점의 trend 게시를 실제 정본으로 확인한다.
            state = runtime.owner.state
            latest = state['risk_sources']['latest']['index_trend']
            assert state['risk_sources']['records'][latest]['terminal']['receipt']['status'] == 'accepted'
            assert state['regime_policy']['engine_projection']['operation_id'] == latest
            worker_scheduled[0] = True
            return original_schedule(token)
        monkeypatch.setattr(writer, '_schedule_vix', schedule)
        entered, release = hold_lookup(monkeypatch, store,
            lambda key: worker_scheduled[0] and key.startswith('command:risk-begin:'))
        try:
            receipt = await writer.refresh_trend(provider(lambda: NOW, bull=True))
            assert receipt.status == 'accepted'
            await asyncio.wait_for(entered.wait(), 2)
            assert fetch_calls == [], 'VIX provider must not run before durable begin'
            trend = deepcopy(runtime.owner.state['regime_policy'])
            assert not runtime.trading_ready
            assert writer.sources.read_source('index_trend').authority_status == 'pending'
            assert writer.sources.read_source('vix_regime').authority_status == 'pending'
            seal = runtime.owner.state['risk_input_seals']['records'][receipt.operation_id]['original']
            if seed_old:
                assert seal['request']['inputs']['selected_vix']['operation_id'] == 'boundary-vix'
                retained = seal['reads']['retained']['boundary-vix']['terminal']['envelope']
                assert retained['payload'] == {'value': 20.0, 'fetched_at': old_time.isoformat()}
                assert retained['received_at'] == old_time.isoformat()
                assert trend['trend_state']['regime_data']['vix'] == 20.0
            else:
                assert seal['request']['inputs']['selected_vix'] is None
                assert seal['reads']['retained'] == {}
                assert trend['trend_state']['regime_data']['vix'] is None
            release.set()
            await asyncio.wait_for(fetched_entered.wait(), 2)
            assert fetch_calls == ['vix']
            index_read = writer.sources.read_source('index_trend')
            assert (index_read.authority_status, index_read.reason) == ('stale', 'source_read_changed')
            finish_fetch.set()
            await asyncio.wait_for(asyncio.shield(writer._vix_refresh_task), 2)
            assert runtime.owner.state['regime_policy'] == trend, 'VIX completion must not reclassify mid/sidecar'
            assert writer.adapter._vix_value == 40.0
            assert writer.adapter._vix_state == 'fear'
            assert writer.sources.read_source('index_trend').authority_status == 'stale'
            assert runtime.owner.state['risk_sources']['records'][receipt.operation_id]['terminal']['receipt']['status'] == 'accepted'
        finally:
            release.set()
            finish_fetch.set()
            await runtime.shutdown()
            await store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('identical', [True, False])
def test_concurrent_baselines_idempotency_and_normal_conflict(tmp_path, monkeypatch, identical):
    async def scenario():
        _, _, store, runtime = await prerequisites(tmp_path)
        first = baseline_json(runtime)
        second = deepcopy(first)
        if not identical:
            second['baseline_id'] = 'known-2'
        expected = runtime.owner.version
        entered, release = hold_lookup(monkeypatch, store, lambda key: key.startswith('command:regime-baseline:'))
        first_task = asyncio.create_task(RegimeOwner.register_baseline(runtime,
            RegimeBaseline.from_dict(first), expected_version=expected))
        second_task = None
        try:
            await asyncio.wait_for(entered.wait(), 2)
            admitted = asyncio.Event()
            original = runtime.start_command_result
            def start(token, operation):
                result = original(token, operation)
                admitted.set()
                return result
            monkeypatch.setattr(runtime, 'start_command_result', start)
            second_task = asyncio.create_task(RegimeOwner.register_baseline(runtime,
                RegimeBaseline.from_dict(second), expected_version=expected))
            await asyncio.wait_for(admitted.wait(), 2)
            release.set()
            first_version = await first_task
            if identical:
                assert await second_task == first_version == expected + 1
                assert runtime.owner.version == first_version
            else:
                with pytest.raises(ValueError, match='regime_baseline_conflict'):
                    await second_task
                assert runtime.owner.state['regime_policy']['baseline']['supplied'] == first
            shutdown_reason = None
            try:
                await runtime.shutdown()
            except ApplicationBlocked as exc:
                shutdown_reason = str(exc)
            assert (runtime._command_results_failed, runtime.owner.healthy, shutdown_reason) == (False, True, None), (
                'concurrent request conflict poisoned result latch and shutdown')
        finally:
            release.set()
            await asyncio.gather(*(t for t in (first_task, second_task) if t is not None), return_exceptions=True)
            await store.close()
    asyncio.run(scenario())


def test_baseline_real_sql_failure_keeps_failed_latch(tmp_path, monkeypatch):
    async def scenario():
        _, _, store, runtime = await prerequisites(tmp_path)
        original = store.lookup_commit
        async def fail(key):
            if key.startswith('command:regime-baseline:'):
                raise OSError('synthetic SQL lookup unavailable')
            return await original(key)
        monkeypatch.setattr(store, 'lookup_commit', fail)
        try:
            with pytest.raises(OSError, match='SQL lookup'):
                await RegimeOwner.register_baseline(runtime,
                    RegimeBaseline.from_dict(baseline_json(runtime)), expected_version=runtime.owner.version)
            assert runtime._command_results_failed and not runtime.owner.healthy
            assert 'regime_policy' not in runtime.owner.state
            with pytest.raises(ApplicationBlocked, match='command_result_drain_failed'):
                await runtime.shutdown()
        finally:
            await store.close()
    asyncio.run(scenario())


def test_actual_refresh_cancelled_during_initial_begin_records_cancelled_terminal(tmp_path, monkeypatch):
    async def scenario():
        _, _, store, runtime, writer = await install(tmp_path, lambda: NOW)
        before = deepcopy(runtime.owner.state['regime_policy'])
        calls = []
        entered, release = asyncio.Event(), asyncio.Event()
        terminal_entered, terminal_release = asyncio.Event(), asyncio.Event()
        original = store.lookup_commit
        async def lookup(key):
            if key.startswith('command:risk-begin:'):
                entered.set()
                await release.wait()
            elif key.startswith('command:risk-complete:'):
                terminal_entered.set()
                await terminal_release.wait()
            return await original(key)
        monkeypatch.setattr(store, 'lookup_commit', lookup)
        task = asyncio.create_task(writer.refresh_trend(provider(lambda: NOW, calls=calls)))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            assert 'index_trend' in runtime._risk_source_pending.values()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert runtime._risk_source_pending, 'admitted begin must remain pending until SQL drains'
            release.set()
            # finalizer까지 기다린 뒤 pending을 기대하지 않는다. terminal SQL을 직접 보류한다.
            await asyncio.wait_for(terminal_entered.wait(), 2)
            assert writer.sources.read_source('index_trend').authority_status == 'pending'
            assert not runtime.trading_ready
            terminal_release.set()
            await asyncio.wait_for(runtime.shutdown(), 2)
            rows = [row for row in runtime.owner.state['risk_sources']['records'].values()
                    if row['ticket']['lane'] == 'index_trend']
            assert len(rows) == 1 and calls == []
            assert runtime.owner.state['regime_policy'] == before
            assert not runtime._risk_source_pending and not runtime._command_scopes
            assert runtime.owner.healthy and not runtime._command_results_failed
            assert rows[0]['terminal'] is not None, 'healthy shutdown left cancelled refresh durably pending'
            assert rows[0]['terminal']['receipt']['status'] == 'cancelled'
        finally:
            release.set()
            terminal_release.set()
            await asyncio.gather(task, return_exceptions=True)
            await runtime.shutdown()
            await store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize(('vix', 'delay'), [(20.0, 1800), (14.0, 600)])
def test_actual_mid_confirmation_uses_owned_threshold_and_stage_clocks(tmp_path, vix, delay):
    async def scenario():
        current = [NOW]
        clock = lambda: current[0]
        _, _, store, runtime, writer = await install(tmp_path, clock, vix=vix)
        try:
            for offset, expected_regime, stage in [(0, 'sideways', 'start'),
                    (delay - 1, 'sideways', 'confirm'), (delay, 'bull', 'confirm')]:
                current[0] = NOW + timedelta(seconds=offset)
                receipt = await writer.refresh_trend(provider(clock, bull=True))
                assert receipt.status == 'accepted'
                trend = runtime.owner.state['regime_policy']['trend_state']
                assert trend['mid_regime'] == expected_regime
                assert trend['mid_pending']['since'] == NOW.isoformat()
                assert trend['mid_pending']['target'] == (None if expected_regime == 'bull' else 'bull')
                clocks = runtime.owner.state['risk_input_seals']['records'][receipt.operation_id]['original']['request']['inputs']['clocks']
                assert set(clocks) == {'sidecar', 'opening', 'technical_update', 'mid_' + stage}
                assert clocks['mid_' + stage] == clock().isoformat()
                assert writer.adapter._last_update == clock()
        finally:
            await runtime.shutdown()
            await store.close()
    asyncio.run(scenario())


def test_actual_expert_confirmation_600_and_technical_timestamp(tmp_path):
    async def scenario():
        current = [NOW]
        clock = lambda: current[0]
        _, _, store, runtime, writer = await install(tmp_path, clock)
        experts = SimpleNamespace(aggregate_regime_score=lambda: 25,
                                  bear_consensus=lambda **kwargs: False)
        try:
            for offset, expected_regime, stage in [(0, 'sideways', 'start'),
                    (599, 'sideways', 'confirm'), (600, 'bull', 'confirm')]:
                current[0] = NOW + timedelta(seconds=offset)
                receipt = await writer.refresh_trend(provider(clock), expert_orchestrator=experts)
                assert receipt.status == 'accepted'
                trend = runtime.owner.state['regime_policy']['trend_state']
                assert trend['mid_regime'] == expected_regime
                assert trend['expert_pending']['since'] == NOW.isoformat()
                assert trend['expert_pending']['target'] == (None if offset == 600 else 'bull')
                clocks = runtime.owner.state['risk_input_seals']['records'][receipt.operation_id]['original']['request']['inputs']['clocks']
                assert set(clocks) == {'sidecar', 'opening', 'technical_update', 'expert_' + stage}
                assert writer.adapter._last_update.isoformat() == clocks['technical_update']
        finally:
            await runtime.shutdown()
            await store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('expert_mode', ['missing', 'error', 'no_proposal'])
def test_actual_opening_preserves_mid_pending_and_expert_absence_is_noop(tmp_path, expert_mode):
    async def scenario():
        opening = NOW.replace(hour=9, minute=30)
        pending = {'present': True, 'target': 'bull', 'since': (opening - timedelta(minutes=20)).isoformat()}
        def modify(value):
            value['trend_state']['mid_pending'] = deepcopy(pending)
            value['trend_state']['expert_pending'] = deepcopy(pending)
        _, _, store, runtime, writer = await install(tmp_path, lambda: opening, modify=modify)
        def broken():
            raise RuntimeError('synthetic expert failure')
        experts = None if expert_mode == 'missing' else SimpleNamespace(
            aggregate_regime_score=broken if expert_mode == 'error' else lambda: 0,
            bear_consensus=lambda **kwargs: False)
        try:
            receipt = await writer.refresh_trend(provider(lambda: opening, bull=True), expert_orchestrator=experts)
            assert receipt.status == 'accepted'
            trend = runtime.owner.state['regime_policy']['trend_state']
            assert trend['mid_regime'] == 'neutral'
            assert trend['mid_pending'] == pending
            assert trend['expert_pending'] == ({**pending, 'target': None} if expert_mode == 'no_proposal' else pending)
            clocks = runtime.owner.state['risk_input_seals']['records'][receipt.operation_id]['original']['request']['inputs']['clocks']
            assert set(clocks) == {'sidecar', 'opening', 'technical_update'}
            lane = runtime.owner.state['risk_sources']['latest']['expert_regime']
            status = runtime.owner.state['risk_sources']['records'][lane]['terminal']['receipt']['status']
            assert status == {'missing': 'missing', 'error': 'failed', 'no_proposal': 'accepted'}[expert_mode]
        finally:
            await runtime.shutdown()
            await store.close()
    asyncio.run(scenario())
