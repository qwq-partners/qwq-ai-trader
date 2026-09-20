"""Independent C4 consumer barrier: durable SQL success is not live publication."""
import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.execution.safety.application import ApplicationBlocked
from src.execution.safety.regime_morning import RegimeMorningBaseline
from src.execution.safety.regime_owner import RegimeOwner, RegimeHorizonBaseline
from test_execution_regime_morning import Inputs, baseline
from test_execution_regime_morning_acceptance import _owned_with_known_morning_baseline, MORNING_CLOCK
from test_execution_regime_noon_replay import horizon_json
from test_execution_regime_noon_fault_boundaries import cold_reopen
from test_execution_regime_owner import quote


@pytest.fixture(autouse=True)
def synthetic_home(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)


@pytest.mark.parametrize('reader', ['effective_regime', 'morning_assessment', 'open_expectation'])
def test_unpublished_morning_readers_block_until_real_cold_restore(tmp_path, monkeypatch, reader):
    async def scenario():
        engine, exits, store, runtime, owner, _ = await _owned_with_known_morning_baseline(
            tmp_path, clock=MORNING_CLOCK, mid_regime='bear')
        fresh_store = fresh_runtime = None
        def read(writer):
            if reader == 'effective_regime': return writer.adapter.effective_regime
            if reader == 'morning_assessment': return writer.morning_assessment()
            return writer.adapter.open_expectation()
        try:
            await RegimeOwner.register_horizon_baseline(runtime,
                RegimeHorizonBaseline.from_dict(horizon_json(runtime)), expected_version=runtime.owner.version)
            await runtime.owner.register_policy_generations('read-barrier-horizon', ('regime_policy.horizon',))
            await RegimeOwner.register_morning_baseline(runtime,
                RegimeMorningBaseline.from_dict(baseline(runtime)), expected_version=runtime.owner.version)
            async def fetch(code):
                value = quote(code, pct=-3.0)
                value['_observation']['received_at'] = MORNING_CLOCK.isoformat()
                value['open'] = value['_observation']['fields']['open']['value'] = 101.0
                return value
            assert (await owner.refresh_trend(SimpleNamespace(fetch_index_price=fetch))).status == 'accepted'
            assert engine._market_regime == owner.adapter.effective_regime == 'bear'
            original_publish = runtime.owner.publisher
            def failing_publish(state, version):
                if state['regime_policy']['morning']['assessment'] is not None:
                    raise ValueError('synthetic publication failure')
                return original_publish(state, version)
            monkeypatch.setattr(runtime.owner, 'publisher', failing_publish)
            class LLM:
                async def complete(self, *args, **kwargs):
                    return SimpleNamespace(success=True, content='[공격] accepted but unpublished')
            with pytest.raises(ApplicationBlocked):
                await owner.diagnose_morning(Inputs(), LLM())
            durable_version, durable = await store.load()
            operation = durable['risk_sources']['latest']['llm_morning_diagnosis']
            assert durable['risk_sources']['records'][operation]['terminal']['receipt']['status'] == 'accepted'
            assert durable['regime_policy']['trend_state']['mid_regime'] == 'sideways'
            assert not runtime.owner.healthy and runtime.owner.published_version < durable_version
            assert owner.adapter._current_regime == engine._market_regime == 'bear'
            assert owner.adapter._llm_assessment == ''
            assert owner.sources.read_source('llm_morning_diagnosis').authority_status == 'unavailable'
            blocked = False
            exposed = None
            try:
                exposed = read(owner)
            except ApplicationBlocked:
                blocked = True

            # Exercise the genuine recovery control even while the rejection assertion is RED.
            fresh_engine, _, fresh_store, fresh_runtime, fresh_owner = await cold_reopen(
                engine, exits, store, runtime, clock=lambda: MORNING_CLOCK)
            runtime = store = None
            assert fresh_runtime.owner.healthy
            assert fresh_runtime.owner.version == fresh_runtime.owner.published_version == durable_version
            assert fresh_engine._market_regime == fresh_owner.adapter.effective_regime == 'sideways'
            assert fresh_owner.morning_assessment()['assessment'] == '[공격] accepted but unpublished'
            assert fresh_owner.adapter.open_expectation() == '[공격] accepted but unpublished'
            assert not fresh_runtime.trading_ready
            assert blocked, f'{reader} exposed unpublished state: {exposed!r}'
        finally:
            if runtime is not None and not runtime._closing:
                try: await runtime.shutdown()
                except ApplicationBlocked: pass
            if store is not None and not store._closed: await store.close()
            if fresh_runtime is not None and not fresh_runtime._closing: await fresh_runtime.shutdown()
            if fresh_store is not None and not fresh_store._closed: await fresh_store.close()
    asyncio.run(scenario())
