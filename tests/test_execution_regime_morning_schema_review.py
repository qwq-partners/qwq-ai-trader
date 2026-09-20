"""A2 disposition probes: actual SQLite/source queues, only timing is controlled."""
import asyncio
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from test_execution_regime_morning import Inputs, _TextLLM, baseline, morning_owned
from test_execution_regime_owner import owned, quote
from test_execution_regime_noon_replay import horizon_json
from test_execution_runtime import NOW
from src.execution.safety.application import ApplicationBlocked
from src.execution.safety.regime_owner import RegimeOwner, RegimeHorizonBaseline, require_current_trend
from src.execution.safety.regime_morning import RegimeMorningBaseline


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)


async def schema2(tmp_path, monkeypatch):
    import test_execution_regime_owner as fixtures
    now = NOW.replace(hour=8, minute=50)
    monkeypatch.setattr(fixtures, 'NOW', now)
    original = fixtures.baseline_json
    def bullish_baseline(runtime):
        value = original(runtime)
        value['trend_state']['mid_regime'] = value['engine_regime'] = 'bull'
        return value
    monkeypatch.setattr(fixtures, 'baseline_json', bullish_baseline)
    result = await owned(tmp_path, clock=lambda: now)
    runtime = result[3]
    supplied = horizon_json(runtime)
    supplied['horizon'] = {'level': 'severe', 'change_pct': -4.0, 'classified_at': now.isoformat()}
    await RegimeOwner.register_horizon_baseline(runtime, RegimeHorizonBaseline.from_dict(supplied),
        expected_version=runtime.owner.version)
    await runtime.owner.register_policy_generations('schema3-horizon', ('regime_policy.horizon',))
    return result


def bullish_quote(code):
    value = quote(code, pct=2.0, high=103.0)
    value['price'] = value['_observation']['fields']['price']['value'] = 102.0
    return value


@pytest.mark.parametrize('hold', ['expert', 'seal'])
def test_schema3_registration_stales_inflight_v1_index_without_poisoning(tmp_path, monkeypatch, hold):
    async def scenario():
        engine, _, store, runtime, writer, _ = await schema2(tmp_path, monkeypatch)
        entered, release = asyncio.Event(), asyncio.Event()
        original_complete, original_seal = writer.sources.complete, writer.sources.seal
        async def complete(ticket, *args, **kwargs):
            result = await original_complete(ticket, *args, **kwargs)
            if hold == 'expert' and ticket.kind == 'expert_regime':
                entered.set(); await release.wait()
            return result
        async def seal(ticket, **kwargs):
            result = await original_seal(ticket, **kwargs)
            if hold == 'seal' and ticket.kind == 'index_trend':
                entered.set(); await release.wait()
            return result
        monkeypatch.setattr(writer.sources, 'complete', complete)
        monkeypatch.setattr(writer.sources, 'seal', seal)
        async def fetch(code): return bullish_quote(code)
        task = asyncio.create_task(writer.refresh_trend(SimpleNamespace(fetch_index_price=fetch)))
        try:
            await asyncio.wait_for(entered.wait(), 3)
            migration = await RegimeOwner.register_morning_baseline(runtime,
                RegimeMorningBaseline.from_dict(baseline(runtime)), expected_version=runtime.owner.version)
            before = runtime.owner.state['regime_policy']
            release.set(); receipt = await task
            assert receipt.status == 'stale', 'v1 completion must not append past the schema3 activation boundary'
            assert runtime.owner.state['regime_policy'] == before
            assert runtime.owner.healthy and not runtime._command_results_failed
            await runtime.restore()
            next_receipt = await writer.refresh_trend(SimpleNamespace(fetch_index_price=fetch))
            assert next_receipt.status == 'accepted'
            assert next_receipt.committed_version > migration
            root = runtime.owner.state['regime_policy']
            assert root['trend_state']['mid_regime'] == 'bull'
            assert root['engine_projection']['regime'] == engine._market_regime == 'sideways'
            await runtime.restore()
        finally:
            release.set(); await asyncio.gather(task, return_exceptions=True)
            if runtime.owner.healthy: await runtime.shutdown()
            await store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('replacement', ['pending', 'failed', 'missing'])
def test_morning_authority_recurses_into_replaced_index_source(tmp_path, monkeypatch, replacement):
    async def scenario():
        _, _, store, runtime, writer, _, clock = await morning_owned(tmp_path, monkeypatch)
        try:
            receipt = (await writer.diagnose_morning(Inputs(), _TextLLM())).receipt
            assert require_current_trend(runtime.owner.state, clock[0].date().isoformat()) == receipt.committed_version
            index = await writer.sources.begin('next-index', 'index_trend', require_seal=True)
            if replacement != 'pending': await writer.sources.complete(index, replacement)
            with pytest.raises(ApplicationBlocked, match='regime_source_not_current'):
                require_current_trend(runtime.owner.state, clock[0].date().isoformat())
            assert writer.morning_assessment()['assessment'] == '[방어] SQL boundary'
        finally: await runtime.shutdown(); await store.close()
    asyncio.run(scenario())


def test_legitimate_v1_index_before_schema3_registration_restores(tmp_path, monkeypatch):
    async def scenario():
        _, _, store, runtime, writer, _ = await schema2(tmp_path, monkeypatch)
        async def fetch(code): return bullish_quote(code)
        try:
            old = await writer.refresh_trend(SimpleNamespace(fetch_index_price=fetch))
            assert old.status == 'accepted'
            old_row = runtime.owner.state['regime_policy']['transitions'][old.operation_id]
            migration = await RegimeOwner.register_morning_baseline(runtime,
                RegimeMorningBaseline.from_dict(baseline(runtime)), expected_version=runtime.owner.version)
            assert old.committed_version < migration
            await runtime.restore()
            assert runtime.owner.state['regime_policy']['transitions'][old.operation_id] == old_row
        finally: await runtime.shutdown(); await store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('schema', [2, 3])
def test_index_midnight_after_seal_is_stale_not_calculation_failure(tmp_path, monkeypatch, schema):
    async def scenario():
        _, _, store, runtime, writer, _ = await schema2(tmp_path, monkeypatch)
        if schema == 3:
            await RegimeOwner.register_morning_baseline(runtime,
                RegimeMorningBaseline.from_dict(baseline(runtime)), expected_version=runtime.owner.version)
        original = writer.sources.seal
        async def seal(ticket, **kwargs):
            result = await original(ticket, **kwargs)
            if ticket.kind == 'index_trend':
                tomorrow = runtime._now() + timedelta(days=1)
                monkeypatch.setattr(runtime, 'clock', lambda: tomorrow)
            return result
        monkeypatch.setattr(writer.sources, 'seal', seal)
        async def fetch(code): return bullish_quote(code)
        try:
            before = runtime.owner.state['regime_policy']
            result = await writer.refresh_trend(SimpleNamespace(fetch_index_price=fetch))
            assert result.status == 'stale' and result.reason == 'day_or_generation'
            assert runtime.owner.state['regime_policy'] == before
            assert runtime.owner.healthy and not runtime._command_results_failed
        finally: await runtime.shutdown(); await store.close()
    asyncio.run(scenario())
