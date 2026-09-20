"""C4 writer behavior against the real owner/store, with external inputs synthetic."""
import asyncio
from copy import deepcopy
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from test_execution_regime_owner import owned, quote
from test_execution_regime_noon_replay import horizon_json
from test_execution_runtime import NOW
from src.execution.safety.risk_sources import RiskSourceCoordinator


@pytest.fixture(autouse=True)
def synthetic_home(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)


def test_generic_morning_success_cannot_bypass_typed_owner(tmp_path):
    async def scenario():
        _, _, store, runtime, _, _ = await owned(tmp_path)
        source = RiskSourceCoordinator(runtime)
        try:
            ticket = await source.begin('foreign-morning', 'llm_morning_diagnosis')
            before = runtime.owner.state
            with pytest.raises(ValueError, match='morning|writer'):
                await source.complete(ticket, 'success', {'assessment': '[방어] synthetic'})
            assert runtime.owner.state == before
        finally:
            await runtime.shutdown(); await store.close()
    asyncio.run(scenario())


def baseline(runtime, knowledge='known'):
    root = runtime.owner.state['regime_policy']
    return {'schema': 1, 'baseline_id': 'morning-known', 'account_scope': 'scope',
        'business_day': runtime._now().date().isoformat(), 'generation': runtime._day_generation,
        'fence_id': runtime._day_fence_id,
        'evidence': {'source': 'synthetic', 'event_id': 'morning-known', 'observed_at': runtime._now().isoformat()},
        'regime_baseline_version': root['baseline']['baseline_version'],
        'horizon_baseline_version': root['horizon_baseline']['baseline_version'],
        'morning': {'knowledge': knowledge, 'assessment': None, 'assessment_day': None,
                    'open_expectation': None, 'open_expectation_as_of': None}}


async def morning_owned(tmp_path, monkeypatch, *, knowledge='known'):
    import test_execution_regime_owner as fixtures
    from src.execution.safety.regime_owner import RegimeOwner, RegimeHorizonBaseline
    from src.execution.safety.regime_morning import RegimeMorningBaseline
    clock = [NOW.replace(hour=8, minute=50)]
    monkeypatch.setattr(fixtures, 'NOW', clock[0])
    result = await owned(tmp_path, clock=lambda: clock[0])
    _, _, _, runtime, writer, _ = result
    await RegimeOwner.register_horizon_baseline(runtime,
        RegimeHorizonBaseline.from_dict(horizon_json(runtime)), expected_version=runtime.owner.version)
    await runtime.owner.register_policy_generations('morning-horizon', ('regime_policy.horizon',))
    await RegimeOwner.register_morning_baseline(runtime,
        RegimeMorningBaseline.from_dict(baseline(runtime, knowledge)), expected_version=runtime.owner.version)
    async def fetch(code): return quote(code)
    receipt = await writer.refresh_trend(SimpleNamespace(fetch_index_price=fetch))
    assert receipt.status == 'accepted'
    return (*result, clock)


def stage(name, payload=None):
    from src.execution.safety.regime_morning import RegimeMorningStageInput
    return RegimeMorningStageInput.from_dict({'schema': 1, 'stage': name,
        'outcome': 'missing' if payload is None else 'success', 'source': 'synthetic',
        'event_id': None, 'received_at': None, 'market_as_of': None, 'payload': payload})


class Inputs:
    def __init__(self): self.calls = []
    def snapshot_themes(self): self.calls.append('themes'); return stage('theme', {'text': 'chips'})
    def snapshot_symbols(self): self.calls.append('symbols'); return ('005930', '000660')
    async def fetch_overtime_price(self, symbol):
        self.calls.append(symbol)
        return stage('overtime', {'symbol': symbol, 'quote': {'price': 100, 'change_pct': 1, 'volume': 2}})
    def snapshot_news(self): self.calls.append('news'); return stage('news')
    async def fetch_macro_context(self): self.calls.append('macro'); return stage('macro')


def test_morning_success_dedupe_projection_and_restore(tmp_path, monkeypatch):
    async def scenario():
        engine, _, store, runtime, writer, _, clock = await morning_owned(tmp_path, monkeypatch)
        provider = Inputs(); calls = []
        class LLM:
            async def complete(self, prompt, **kwargs):
                calls.append((prompt, kwargs))
                assert kwargs['max_tokens'] == 150
                return SimpleNamespace(success=True, content='[방어] synthetic', error=None)
        try:
            before = runtime.owner.state
            result = await writer.diagnose_morning(provider, LLM())
            assert result.status == 'completed' and result.receipt.status == 'accepted'
            assert provider.calls == ['themes', 'symbols', '005930', '000660', 'news', 'macro']
            assert engine._regime_adapter.open_expectation() == '[방어] synthetic'
            assert runtime.owner.state['protection'] == before['protection']
            assert runtime.owner.state['outbox'] == before['outbox']
            assert (await writer.diagnose_morning(provider, LLM())).status == 'already_done'
            engine._regime_adapter._llm_assessment = 'tampered'
            await runtime.restore()
            assert engine._regime_adapter._llm_assessment == '[방어] synthetic'
            assert (await writer.diagnose_morning(provider, LLM())).status == 'already_done'
            assert len(calls) == 1
            clock[0] = clock[0].replace(hour=9, minute=30)
            assert engine._regime_adapter.open_expectation() is None
            assert writer.morning_assessment()['assessment'] == '[방어] synthetic'
        finally: await runtime.shutdown(); await store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('content', ['', '   ', None])
def test_failed_text_can_retry_and_does_not_record_success(tmp_path, monkeypatch, content):
    async def scenario():
        _, _, store, runtime, writer, _, _ = await morning_owned(tmp_path, monkeypatch)
        class LLM:
            async def complete(self, *args, **kwargs): return SimpleNamespace(success=True, content=content)
        try:
            result = await writer.diagnose_morning(Inputs(), LLM())
            assert result.receipt.status == 'failed'
            assert writer.morning_assessment()['assessment_day'] is None
            next_result = await writer.diagnose_morning(Inputs(), LLM())
            assert next_result.receipt.operation_id != result.receipt.operation_id
        finally: await runtime.shutdown(); await store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('field', ['change_pct', 'volume'])
def test_malformed_overtime_display_stops_before_macro(tmp_path, monkeypatch, field):
    """Legacy formats positive-price quotes before any optional macro request."""
    async def scenario():
        from src.core.market_regime import MarketRegimeAdapter
        _, _, store, runtime, writer, _, _ = await morning_owned(tmp_path, monkeypatch)
        bad_quote = {'price': 100, 'change_pct': 1.0, 'volume': 2}
        bad_quote[field] = 'malformed'
        calls = []
        class NoModel:
            async def complete(self, *args, **kwargs):
                calls.append('model')
                return SimpleNamespace(success=True, content='unexpected')
        async def macro(_key): calls.append('legacy-macro'); return 'macro'
        legacy = MarketRegimeAdapter()
        monkeypatch.setenv('PERPLEXITY_API_KEY', 'synthetic-only')
        monkeypatch.setattr(legacy, '_fetch_perplexity_context', macro)
        class Malformed(Inputs):
            def snapshot_symbols(self): return ('005930',)
            async def fetch_overtime_price(self, symbol):
                self.calls.append(symbol)
                return stage('overtime', {'symbol': symbol, 'quote': bad_quote})
        provider = Malformed()
        try:
            with pytest.raises((TypeError, ValueError)):
                await legacy.llm_morning_diagnosis(NoModel(), premarket_data={'005930': bad_quote})
            assert calls == []
            result = await writer.diagnose_morning(provider, NoModel())
            assert result.receipt.status == 'failed'
            assert provider.calls == ['themes', '005930', 'news']
            assert calls == []
            assert writer.morning_assessment()['assessment_day'] is None
            assert runtime.owner.healthy
        finally: await runtime.shutdown(); await store.close()
    asyncio.run(scenario())


class _TextLLM:
    async def complete(self, *args, **kwargs):
        return SimpleNamespace(success=True, content='[방어] SQL boundary')


def test_cancel_during_morning_begin_sql_drains_without_optional_io(tmp_path, monkeypatch):
    async def scenario():
        _, _, store, runtime, writer, _, _ = await morning_owned(tmp_path, monkeypatch)
        entered, release = asyncio.Event(), asyncio.Event()
        original = store.commit
        async def commit(version, state, command):
            rows = state.get('risk_sources', {}).get('records', {}).values()
            if command.startswith('command:risk-begin:') and any(
                    row['ticket']['kind'] == 'llm_morning_diagnosis' for row in rows):
                entered.set(); await release.wait()
            return await original(version, state, command)
        monkeypatch.setattr(store, 'commit', commit)
        provider = Inputs()
        task = asyncio.create_task(writer.diagnose_morning(provider, _TextLLM()))
        closing = None
        try:
            await asyncio.wait_for(entered.wait(), 3)
            task.cancel()
            with pytest.raises(asyncio.CancelledError): await asyncio.wait_for(task, 3)
            # A commit already in progress keeps the owner temporarily unready;
            # another caller must respect that existing publication barrier.
            from src.execution.safety.application import ApplicationBlocked
            with pytest.raises(ApplicationBlocked): await writer.diagnose_morning(Inputs(), _TextLLM())
            closing = asyncio.create_task(runtime.shutdown())
            await asyncio.sleep(0)
            assert not closing.done()
            release.set(); await asyncio.wait_for(closing, 3)
            rows = [row for row in runtime.owner.state['risk_sources']['records'].values()
                    if row['ticket']['kind'] == 'llm_morning_diagnosis']
            assert len(rows) == 1
            assert rows[0]['terminal']['receipt']['status'] == 'cancelled' and rows[0]['conflict'] is None
            assert provider.calls == []
            assert runtime.owner.healthy and not runtime._command_results_failed
        finally:
            release.set()
            await asyncio.gather(*(t for t in (task, closing) if t is not None), return_exceptions=True)
            if not runtime._closing: await runtime.shutdown()
            await store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('fault', ['precommit', 'committed_response_loss', 'publication'])
def test_morning_completion_sql_fault_requires_restore_and_preserves_once_daily(tmp_path, monkeypatch, fault):
    """Actual SQL commit/owner publication, with faults at the outer I/O boundary."""
    async def scenario():
        from src.execution.safety.application import ApplicationBlocked
        from test_execution_regime_noon_fault_boundaries import cold_reopen
        engine, exits, store, runtime, writer, _, clock = await morning_owned(tmp_path, monkeypatch)
        original_commit, original_publish = store.commit, runtime.owner.publisher
        fresh_store = fresh_runtime = None
        def contains_result(state):
            return state['regime_policy'].get('morning', {}).get('assessment_day') is not None
        async def commit(version, state, command):
            target = command.startswith('command:risk-complete:') and contains_result(state)
            if target and fault == 'precommit': raise OSError('synthetic morning precommit')
            result = await original_commit(version, state, command)
            if target and fault == 'committed_response_loss': raise OSError('synthetic morning response loss')
            return result
        def publish(state, version):
            if contains_result(state) and fault == 'publication': raise ValueError('synthetic morning publication')
            return original_publish(state, version)
        monkeypatch.setattr(store, 'commit', commit)
        monkeypatch.setattr(runtime.owner, 'publisher', publish)
        try:
            before = runtime.owner.state
            with pytest.raises(ApplicationBlocked if fault == 'publication' else OSError):
                await writer.diagnose_morning(Inputs(), _TextLLM())
            durable_version, durable = await store.load()
            operation = durable['risk_sources']['latest']['llm_morning_diagnosis']
            terminal = durable['risk_sources']['records'][operation]['terminal']
            if fault == 'precommit':
                assert terminal is None and durable['regime_policy']['morning']['assessment_day'] is None
            else:
                assert terminal['receipt']['status'] == 'accepted'
                assert durable['regime_policy']['morning']['assessment_day'] == clock[0].date().isoformat()
            assert engine._regime_adapter._llm_assessment == ''
            assert not runtime.owner.healthy
            assert durable['portfolio'] == before['portfolio'] and durable['outbox'] == before['outbox']
            fresh_engine, _, fresh_store, fresh_runtime, fresh_writer = await cold_reopen(
                engine, exits, store, runtime, clock=lambda: clock[0])
            runtime = store = None
            assert fresh_runtime.owner.state == durable
            assert fresh_runtime.owner.version == fresh_runtime.owner.published_version == durable_version
            provider = Inputs()
            result = await fresh_writer.diagnose_morning(provider, _TextLLM())
            assert result.status == ('pending' if fault == 'precommit' else 'already_done')
            assert provider.calls == []
            assert fresh_engine._regime_adapter._llm_assessment == ('' if fault == 'precommit' else '[방어] SQL boundary')
            await fresh_runtime.restore()
            assert fresh_runtime.owner.state == durable
            assert not fresh_runtime.trading_ready
        finally:
            if runtime is not None and not runtime._closing:
                try: await runtime.shutdown()
                except ApplicationBlocked: pass
            if store is not None and not store._closed: await store.close()
            if fresh_runtime is not None and not fresh_runtime._closing: await fresh_runtime.shutdown()
            if fresh_store is not None and not fresh_store._closed: await fresh_store.close()
    asyncio.run(scenario())


def test_morning_response_after_day_change_is_durable_stale_without_projection(tmp_path, monkeypatch):
    async def scenario():
        _, _, store, runtime, writer, _, clock = await morning_owned(tmp_path, monkeypatch)
        class NewDay(_TextLLM):
            async def complete(self, *args, **kwargs):
                clock[0] += timedelta(days=1)
                return await super().complete(*args, **kwargs)
        try:
            before = runtime.owner.state['regime_policy']
            result = await writer.diagnose_morning(Inputs(), NewDay())
            assert result.receipt.status == 'stale'
            assert runtime.owner.state['regime_policy'] == before
            assert (await store.load())[1]['risk_sources']['records'][result.receipt.operation_id]['terminal']['receipt']['status'] == 'stale'
            assert writer.morning_assessment()['assessment_day'] is None
            assert runtime.owner.healthy
        finally: await runtime.shutdown(); await store.close()
    asyncio.run(scenario())


def test_schema3_intraday_classifier_and_degraded_protection_repair_remain_connected(tmp_path, monkeypatch):
    async def scenario():
        from src.execution.safety.intraday_owner import IntradayRiskOwner
        from test_execution_intraday_owner import batch_for
        from test_execution_intraday_replay_integration import failed_fill, refresh
        from test_execution_regime_noon_replay import Inputs as ClassifierInputs
        engine, exits, store, runtime, writer, _, clock = await morning_owned(tmp_path, monkeypatch)
        try:
            assert (await writer.diagnose_morning(Inputs(), _TextLLM())).receipt.status == 'accepted'
            clock[0] = NOW
            await runtime.owner.register_policy_generations('schema3-protection',
                ('protection.config', 'protection.current_regime', 'protection.intraday_crash_level'))
            await failed_fill(engine, runtime, monkeypatch)
            batch = batch_for(engine, exits)
            IntradayRiskOwner(runtime, batch)
            clock[0] += timedelta(seconds=1)
            assert (await refresh(engine, batch, monkeypatch, -3.0, clock)).status == 'accepted'
            assert runtime.owner.state['regime_policy']['horizon']['writer_kind'] == 'intraday_5m'
            class JSONLLM:
                async def complete_json(self, **kwargs): return {'regime': 'trending_bear'}
            classifier = await writer.classify('12:00', inputs_provider=ClassifierInputs(), llm=JSONLLM())
            assert classifier.status == 'accepted'
            assert writer.classifier_application_receipt(classifier.operation_id).status in ('applied', 'unchanged')
            await runtime.restore()
            before = runtime.owner.state
            receipt = await runtime.repair_protection('schema3-repair', '005930', expected_version=runtime.owner.version)
            assert receipt.status == 'APPLIED', receipt.reason
            assert exits.get_state('005930').remaining_quantity == 40
            assert runtime.owner.state['regime_policy']['schema'] == 3
            for key in ('portfolio', 'risk', 'intents', 'attempts', 'lots', 'outbox', 'risk_sources', 'intraday_policy'):
                assert runtime.owner.state[key] == before[key]
            assert not runtime.trading_ready and not engine._event_queue
        finally: await runtime.shutdown(); await store.close()
    asyncio.run(scenario())
