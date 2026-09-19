"""C3: 실제 SQLite/core 실패 체결과 명시 horizon 계약."""
import asyncio
from copy import deepcopy
from pathlib import Path
from datetime import timedelta
from types import SimpleNamespace

import pytest

from test_execution_regime_owner import owned
from test_execution_intraday_replay_integration import failed_fill
from test_execution_intraday_owner import batch_for
from test_execution_regime_owner import quote
from test_execution_runtime import NOW


def stage_input(name, payload=None):
    from src.execution.safety.regime_owner import RegimeStageInput
    return RegimeStageInput.from_dict({'schema': 1, 'stage': name,
        'outcome': 'missing' if payload is None else 'success', 'source': 'synthetic',
        'event_id': None, 'received_at': None, 'market_as_of': None, 'payload': payload})


def sync_context(now=NOW, technical=None, guard=False):
    from src.execution.safety.regime_owner import RegimeSyncContext
    return RegimeSyncContext.from_dict({'schema': 1, 'captured_at': now.isoformat(),
        'regime_conflict_guard_enabled': guard, 'screener': {'regime': technical,
            'source': 'synthetic', 'event_id': None, 'observed_at': None}})


class Inputs:
    def __init__(self, *, pct=None, malformed_us=False):
        self.pct, self.malformed_us = pct, malformed_us
        self.calls = []
    async def read_daily_bias(self):
        self.calls.append('daily'); return stage_input('daily_bias')
    async def fetch_us_overnight(self):
        self.calls.append('us')
        return stage_input('us_overnight', {'overnight': {'indices_normalized': ['bad']}} if self.malformed_us else None)
    def snapshot_screener(self): return stage_input('screener')
    async def fetch_index_price(self, code):
        self.calls.append(code)
        return stage_input('index' + code, {'quote': quote(code, pct=self.pct)} if self.pct is not None else None)
    def snapshot_application_context(self, *, captured_at): return sync_context(captured_at)


@pytest.mark.parametrize('field', ['stage', 'outcome'])
def test_stage_non_scalar_discriminator_is_a_value_error(field):
    from src.execution.safety.regime_owner import RegimeStageInput
    supplied = stage_input('daily_bias').to_dict()
    supplied[field] = []
    with pytest.raises(ValueError): RegimeStageInput.from_dict(supplied)


async def c3_owned(tmp_path, monkeypatch, *, clock=None):
    from src.execution.safety.regime_owner import RegimeOwner, RegimeHorizonBaseline
    result = await owned(tmp_path, clock=clock)
    engine, exits, store, runtime, writer, _ = result
    await failed_fill(engine, runtime, monkeypatch)
    await RegimeOwner.register_horizon_baseline(runtime,
        RegimeHorizonBaseline.from_dict(horizon_json(runtime)), expected_version=runtime.owner.version)
    await runtime.owner.register_policy_generations('c3-reads', ('regime_policy.horizon',
        'intraday_policy.current', 'protection.config', 'protection.current_regime', 'protection.intraday_crash_level'))
    return result


@pytest.fixture(autouse=True)
def synthetic_home(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)


def horizon_json(runtime):
    state = runtime.owner.state
    return {'schema': 1, 'baseline_id': 'c3-known', 'account_scope': 'scope',
        'business_day': runtime._now().date().isoformat(), 'generation': runtime._day_generation,
        'fence_id': runtime._day_fence_id,
        'evidence': {'source': 'synthetic', 'event_id': 'c3-known',
                     'observed_at': runtime._now().isoformat()},
        'regime_baseline_version': state['regime_policy']['baseline']['baseline_version'],
        'intraday': {'baseline_version': state['intraday_policy']['baseline_version'],
                     'current': state['intraday_policy']['current']},
        'horizon': {'level': 'normal', 'change_pct': 0.0, 'classified_at': None}}


def test_explicit_horizon_preserves_real_failed_fill_anchor(tmp_path, monkeypatch):
    async def scenario():
        from src.execution.safety.regime_owner import RegimeOwner, RegimeHorizonBaseline
        engine, exits, store, runtime, writer, _ = await owned(tmp_path)
        try:
            await failed_fill(engine, runtime, monkeypatch)
            before = runtime.owner.state
            supplied = horizon_json(runtime)
            baseline = RegimeHorizonBaseline.from_dict(supplied)
            version = await RegimeOwner.register_horizon_baseline(runtime, baseline,
                expected_version=runtime.owner.version)
            assert runtime.owner.state['regime_policy']['schema'] == 2
            assert runtime.owner.state['regime_policy']['horizon'] == {
                **supplied['horizon'], 'writer_kind': 'baseline', 'operation_id': 'c3-known',
                'version': version}
            assert runtime.owner.state['protection_replay'] == before['protection_replay']
            assert runtime.owner.state['outbox'] == before['outbox']
            assert await RegimeOwner.register_horizon_baseline(runtime, baseline,
                expected_version=version) == version
            await runtime.restore()
            result = await runtime.repair_protection('c3-baseline-repair', '005930',
                expected_version=runtime.owner.version)
            assert result.status == 'APPLIED', result.reason
            assert exits.get_state('005930').remaining_quantity == 40
            assert not runtime.trading_ready and not engine._event_queue
        finally:
            await runtime.shutdown()
            await store.close()
    asyncio.run(scenario())


def test_malformed_external_us_data_terminates_outer_source(tmp_path, monkeypatch):
    async def scenario():
        _, _, store, runtime, writer, _ = await c3_owned(tmp_path, monkeypatch)
        class LLM:
            async def complete_json(self, **kwargs): raise AssertionError('invalid inputs reached model')
        try:
            result = await writer.classify('12:00', inputs_provider=Inputs(malformed_us=True), llm=LLM())
            assert result.status in {'failed', 'missing'}
            row = runtime.owner.state['risk_sources']['records'][result.operation_id]
            assert row['terminal'] is not None
            assert not runtime.owner.state['regime_policy']['applications']
        finally: await runtime.shutdown(); await store.close()
    asyncio.run(scenario())


def test_valid_quote_after_fixed_classifier_clock_is_not_future(tmp_path, monkeypatch):
    async def scenario():
        from src.execution.safety.regime_owner import RegimeStageInput
        clock = [NOW]
        _, _, store, runtime, writer, _ = await c3_owned(tmp_path, monkeypatch, clock=lambda: clock[0])
        class DelayedInputs(Inputs):
            async def fetch_index_price(self, code):
                clock[0] = NOW + timedelta(seconds=1)
                value = quote(code, pct=-3.0)
                value['_observation']['received_at'] = clock[0].isoformat()
                return stage_input('index' + code, {'quote': value})
        class LLM:
            async def complete_json(self, **kwargs):
                assert 'KOSPI 당일 등락률: -3.00%' in kwargs['prompt']
                return {'regime': 'trending_bull'}
        try:
            receipt = await writer.classify('12:00', inputs_provider=DelayedInputs(), llm=LLM())
            assert receipt.status == 'accepted'
            assert runtime.owner.state['regime_policy']['horizon']['level'] == 'crash'
            await runtime.restore()
        finally: await runtime.shutdown(); await store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('failure', [False, True])
def test_noon_is_durable_while_model_held_and_survives_failure(tmp_path, monkeypatch, failure):
    async def scenario():
        _, _, store, runtime, writer, _ = await c3_owned(tmp_path, monkeypatch)
        reached, release = asyncio.Event(), asyncio.Event()
        class LLM:
            async def complete_json(self, **kwargs):
                reached.set(); await release.wait()
                return None if failure else {'regime': 'trending_bull'}
        before = runtime.owner.state
        provider = Inputs(pct=-3.0)
        task = asyncio.create_task(writer.classify('12:00', inputs_provider=provider, llm=LLM()))
        try:
            await asyncio.wait_for(reached.wait(), 3)
            state = (await store.load())[1]
            assert state['regime_policy']['horizon']['level'] == 'crash'
            assert state['protection'] == before['protection']
            assert state['intraday_policy'] == before['intraday_policy']
            assert provider.calls == ['daily', 'us', '0001', '1001']
            release.set(); receipt = await task
            assert receipt.status == ('failed' if failure else 'accepted')
            assert runtime.owner.state['regime_policy']['horizon']['level'] == 'crash'
            if failure: assert not runtime.owner.state['regime_policy']['applications']
            else: assert writer.classifier_application_receipt(receipt.operation_id).status == 'applied'
        finally:
            release.set(); await asyncio.gather(task, return_exceptions=True)
            if runtime.owner.healthy: await runtime.shutdown()
            await store.close()
    asyncio.run(scenario())


def test_sync_retry_original_receipt_and_removed_aba_blocks_repair(tmp_path, monkeypatch):
    async def scenario():
        from src.execution.safety.protection_recovery import digest, _replay
        _, _, store, runtime, writer, _ = await c3_owned(tmp_path, monkeypatch)
        class LLM:
            async def complete_json(self, **kwargs): return {'regime': 'trending_bull'}
        try:
            receipt = await writer.classify('12:00', inputs_provider=Inputs(), llm=LLM())
            assert receipt.status == 'accepted'
            one = await writer.sync_protection('sync-one', supplied_context=sync_context(technical='bear', guard=True))
            two = await writer.sync_protection('sync-two', supplied_context=sync_context(technical='bull', guard=True))
            assert one.status == two.status == 'applied'
            assert await writer.sync_protection('sync-one', supplied_context=sync_context(technical='bear', guard=True)) == one
            with pytest.raises(ValueError, match='regime_application_request_conflict'):
                await writer.sync_protection('sync-one', supplied_context=sync_context(technical='bull', guard=True))
            tampered = runtime.owner.state
            for operation in ('sync-one', 'sync-two'): del tampered['regime_policy']['applications'][operation]
            history = tampered['protection_replay']['005930']
            history['events'] = [event for event in history['events'] if event.get('application_id') not in {'sync-one', 'sync-two'}]
            previous = ''
            for event in history['events']:
                event['previous'] = previous
                event['digest'] = digest({key: value for key, value in event.items() if key != 'digest'})
                previous = event['digest']
            history['tail'] = previous
            with pytest.raises(ValueError, match='unrecorded|incomplete'):
                _replay(tampered, '005930', state_version=runtime.owner.version)
        finally: await runtime.shutdown(); await store.close()
    asyncio.run(scenario())


def test_horizon_applied_intraday_recovery_ignores_duplicate_envelope(tmp_path, monkeypatch):
    async def scenario():
        from src.execution.safety.regime_owner import RegimeOwner, RegimeHorizonBaseline
        from src.execution.safety.intraday_owner import IntradayRiskOwner
        clock = [NOW]
        engine, exits, store, runtime, writer, _ = await owned(tmp_path, clock=lambda: clock[0])
        batch = batch_for(engine, exits)
        intraday = IntradayRiskOwner(runtime, batch)
        try:
            await failed_fill(engine, runtime, monkeypatch)
            supplied = horizon_json(runtime)
            supplied['horizon'] = {'level': 'crash', 'change_pct': -3.0, 'classified_at': NOW.isoformat()}
            await RegimeOwner.register_horizon_baseline(runtime, RegimeHorizonBaseline.from_dict(supplied),
                expected_version=runtime.owner.version)
            await runtime.owner.register_policy_generations('c3-horizon-reads', ('regime_policy.horizon',))
            original = quote('0001', pct=0.0)
            async def fetch(_): return deepcopy(original)
            clock[0] += timedelta(seconds=1)
            result = await intraday.refresh(SimpleNamespace(fetch_index_price=fetch))
            assert result.status == 'accepted'
            first = runtime.owner.state['regime_policy']['horizon']
            assert first['level'] == 'normal' and first['writer_kind'] == 'intraday_5m'
            assert first['version'] == result.committed_version
            clock[0] += timedelta(seconds=1)
            duplicate = await intraday.refresh(SimpleNamespace(fetch_index_price=fetch))
            assert duplicate.status == 'accepted'
            assert runtime.owner.state['regime_policy']['horizon'] == first
            assert writer.adapter._horizons.intraday_risk_as_of.isoformat() == first['classified_at']
            await runtime.restore()
        finally:
            if runtime.owner.healthy: await runtime.shutdown()
            await store.close()
    asyncio.run(scenario())


def test_degraded_classifier_preserves_whole_invocation_and_repairs(tmp_path, monkeypatch):
    async def scenario():
        from src.execution.safety.regime_owner import (RegimeOwner, RegimeHorizonBaseline,
            RegimeStageInput, RegimeSyncContext)
        engine, exits, store, runtime, writer, _ = await owned(tmp_path)
        try:
            await failed_fill(engine, runtime, monkeypatch)
            await RegimeOwner.register_horizon_baseline(runtime,
                RegimeHorizonBaseline.from_dict(horizon_json(runtime)), expected_version=runtime.owner.version)
            await runtime.owner.register_policy_generations('c3-reads', ('regime_policy.horizon',
                'intraday_policy.current', 'protection.config', 'protection.current_regime',
                'protection.intraday_crash_level'))
            def stage(name, payload=None):
                return RegimeStageInput.from_dict({'schema': 1, 'stage': name,
                    'outcome': 'missing' if payload is None else 'success', 'source': 'synthetic',
                    'event_id': None, 'received_at': None, 'market_as_of': None, 'payload': payload})
            context = RegimeSyncContext.from_dict({'schema': 1, 'captured_at': NOW.isoformat(),
                'regime_conflict_guard_enabled': False, 'screener': {'regime': None,
                    'source': 'synthetic', 'event_id': None, 'observed_at': None}})
            class Inputs:
                async def read_daily_bias(self): return stage('daily_bias')
                async def fetch_us_overnight(self): return stage('us_overnight')
                def snapshot_screener(self): return stage('screener')
                async def fetch_index_price(self, code): return stage('index' + code)
                def snapshot_application_context(self, *, captured_at): return context
            class LLM:
                async def complete_json(self, **kwargs): return {'regime': 'trending_bear'}
            receipt = await writer.classify('12:00', inputs_provider=Inputs(), llm=LLM())
            assert receipt.status == 'accepted'
            application = writer.classifier_application_receipt(receipt.operation_id)
            assert application.status == 'applied'
            assert application.committed_version == receipt.committed_version
            history = runtime.owner.state['protection_replay']['005930']['events']
            assert history[-1]['kind'] == 'regime_application'
            before = runtime.owner.state
            await runtime.restore()
            result = await runtime.repair_protection('classifier-repair', '005930',
                expected_version=runtime.owner.version)
            assert result.status == 'APPLIED', result.reason
            assert exits.get_state('005930').remaining_quantity == 40
            for key in ('portfolio', 'risk', 'lots', 'outbox', 'inbox', 'risk_sources', 'regime_policy'):
                assert runtime.owner.state[key] == before[key]
        finally:
            if runtime.owner.healthy: await runtime.shutdown()
            await store.close()
    asyncio.run(scenario())
