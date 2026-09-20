"""Independent C4 review probes: real SQLite owner; synthetic external I/O only."""
import asyncio
from copy import deepcopy
from datetime import timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from test_execution_regime_morning import Inputs, morning_owned, stage
from test_execution_regime_owner import quote
from src.core.market_regime import MarketRegimeAdapter
from src.execution.safety.application import ApplicationBlocked
from src.execution.safety.regime_owner import require_current_trend


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)


class NoModel:
    async def complete(self, *args, **kwargs):
        raise AssertionError('model must not be called')


def test_malformed_overtime_format_preserves_legacy_zero_macro_calls(tmp_path, monkeypatch):
    """Positive price with malformed display data aborts before macro in legacy."""
    async def scenario():
        from src.schedulers.kr_scheduler import _RegimeMorningInputs
        engine, _, store, runtime, writer, _, _ = await morning_owned(tmp_path, monkeypatch)
        bad_quote = {'price': 100, 'change_pct': 'malformed', 'volume': 2}
        calls = []
        async def macro(_key):
            calls.append('macro')
            return 'synthetic macro'
        monkeypatch.setenv('PERPLEXITY_API_KEY', 'synthetic-test-value')
        legacy = MarketRegimeAdapter()
        monkeypatch.setattr(legacy, '_fetch_perplexity_context', macro)
        try:
            with pytest.raises(ValueError):
                await legacy.llm_morning_diagnosis(NoModel(), premarket_data={'005930': bad_quote})
            assert calls == []
            async def overtime(_symbol):
                calls.append('overtime')
                return deepcopy(bad_quote)
            monkeypatch.setattr(engine._regime_adapter, '_fetch_perplexity_context', macro)
            provider = _RegimeMorningInputs(SimpleNamespace(bot=SimpleNamespace(
                engine=engine, theme_detector=None, broker=SimpleNamespace(get_overtime_price=overtime))))
            # Synthetic portfolio selection only; the actual provider and owner remain intact.
            monkeypatch.setattr(provider, 'snapshot_symbols', lambda: ('005930',))
            result = await writer.diagnose_morning(provider, NoModel())
            assert result.receipt.status == 'failed'
            assert calls == ['overtime'], 'C4 performed macro I/O that legacy skipped'
        finally:
            await runtime.shutdown()
            await store.close()
    asyncio.run(scenario())


def test_selected_morning_version_then_new_index_and_historical_display(tmp_path, monkeypatch):
    async def scenario():
        engine, _, store, runtime, writer, _, clock = await morning_owned(tmp_path, monkeypatch)
        class LLM:
            async def complete(self, *args, **kwargs):
                return SimpleNamespace(success=True, content='[방어] independent')
        try:
            index_version = require_current_trend(runtime.owner.state, clock[0].date().isoformat())
            result = await writer.diagnose_morning(Inputs(), LLM())
            assert result.receipt.status == 'accepted'
            assert require_current_trend(runtime.owner.state, clock[0].date().isoformat()) == result.receipt.committed_version > index_version
            before = deepcopy(runtime.owner.state['regime_policy']['morning'])
            clock[0] += timedelta(minutes=2)
            async def fetch(code): return quote(code)
            next_index = await writer.refresh_trend(SimpleNamespace(fetch_index_price=fetch))
            assert next_index.status == 'accepted'
            assert require_current_trend(runtime.owner.state, clock[0].date().isoformat()) == next_index.committed_version
            assert runtime.owner.state['regime_policy']['morning'] == before
            await runtime.restore()
            assert writer.morning_assessment()['assessment'] == '[방어] independent'
            assert engine._market_regime == writer.adapter.regime
        finally:
            await runtime.shutdown()
            await store.close()
    asyncio.run(scenario())


def test_singleflight_cancel_drains_original_attempt_before_retry(tmp_path, monkeypatch):
    async def scenario():
        _, _, store, runtime, writer, _, _ = await morning_owned(tmp_path, monkeypatch)
        reached = asyncio.Event()
        class Waiting(Inputs):
            async def fetch_overtime_price(self, symbol):
                reached.set()
                await asyncio.Event().wait()
        task = asyncio.create_task(writer.diagnose_morning(Waiting(), NoModel()))
        try:
            await asyncio.wait_for(reached.wait(), 3)
            second_provider = Inputs()
            result = await writer.diagnose_morning(second_provider, NoModel())
            assert result.status == 'pending' and second_provider.calls == []
            task.cancel()
            with pytest.raises(asyncio.CancelledError): await task
            await runtime.shutdown()
            rows = [r for r in runtime.owner.state['risk_sources']['records'].values()
                    if r['ticket']['kind'] == 'llm_morning_diagnosis']
            assert len(rows) == 1 and rows[0]['terminal']['receipt']['status'] == 'cancelled'
            assert runtime.owner.healthy and not runtime._command_results_failed
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await store.close()
    asyncio.run(scenario())


def test_unknown_baseline_stays_blocked_without_optional_calls(tmp_path, monkeypatch):
    async def scenario():
        _, _, store, runtime, writer, _, clock = await morning_owned(tmp_path, monkeypatch, knowledge='unknown')
        inputs = Inputs()
        try:
            with pytest.raises(ApplicationBlocked, match='morning_baseline_unknown'):
                await writer.diagnose_morning(inputs, NoModel())
            clock[0] += timedelta(days=1)
            with pytest.raises(ApplicationBlocked):
                await writer.diagnose_morning(inputs, NoModel())
            assert inputs.calls == []
        finally:
            await runtime.shutdown()
            await store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('point', ['theme', 'symbols', 'news'])
def test_optional_required_stage_errors_stop_before_macro_model(tmp_path, monkeypatch, point):
    async def scenario():
        _, _, store, runtime, writer, _, _ = await morning_owned(tmp_path, monkeypatch)
        class Broken(Inputs):
            def snapshot_themes(self):
                if point == 'theme': raise ValueError('synthetic')
                return super().snapshot_themes()
            def snapshot_symbols(self):
                if point == 'symbols': raise ValueError('synthetic')
                return super().snapshot_symbols()
            def snapshot_news(self):
                if point == 'news': raise ValueError('synthetic')
                return super().snapshot_news()
        inputs = Broken()
        try:
            result = await writer.diagnose_morning(inputs, NoModel())
            assert result.receipt.status == 'failed'
            assert 'macro' not in inputs.calls
            assert writer.morning_assessment()['assessment_day'] is None
            assert runtime.owner.healthy
        finally:
            await runtime.shutdown()
            await store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('change', ['index', 'ack'])
def test_related_index_invalidates_but_unrelated_ack_accepts(tmp_path, monkeypatch, change):
    async def scenario():
        from test_execution_runtime import opened
        _, _, store, runtime, writer, _, _ = await morning_owned(tmp_path, monkeypatch)
        class LLM:
            async def complete(self, *args, **kwargs):
                if change == 'index':
                    async def fetch(code): return quote(code)
                    assert (await writer.refresh_trend(SimpleNamespace(fetch_index_price=fetch))).status == 'accepted'
                else:
                    await opened(runtime, 'unrelated-ack')
                return SimpleNamespace(success=True, content='[방어] independent')
        try:
            result = await writer.diagnose_morning(Inputs(), LLM())
            assert result.receipt.status == ('stale' if change == 'index' else 'accepted')
            assert bool(writer.morning_assessment()['assessment']) is (change == 'ack')
            assert runtime.owner.healthy and not runtime.trading_ready
        finally:
            await runtime.shutdown()
            await store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('completed', [True, False])
def test_new_store_new_runtime_restore_preserves_dedupe_or_pending(tmp_path, monkeypatch, completed):
    async def scenario():
        from decimal import Decimal
        from src.core.engine import UnifiedEngine
        from src.core.types import TradingConfig, RiskConfig
        from src.execution.safety.regime_owner import RegimeOwner
        from src.execution.safety.runtime import KRExecutionRuntime
        from src.execution.safety.store import ExecutionStateStore
        from src.risk.manager import RiskManager
        from src.strategies.exit_manager import ExitManager
        _, _, store, runtime, writer, _, clock = await morning_owned(tmp_path, monkeypatch)
        class LLM:
            async def complete(self, *args, **kwargs):
                return SimpleNamespace(success=True, content='[방어] independent')
        if completed:
            assert (await writer.diagnose_morning(Inputs(), LLM())).receipt.status == 'accepted'
        else:
            await writer.sources.begin('unresolved-morning', 'llm_morning_diagnosis', require_seal=True)
        db_path = store.path
        await runtime.shutdown()
        await store.close()
        engine = UnifiedEngine(TradingConfig(initial_capital=Decimal('2000000')))
        sidecar = RiskManager(RiskConfig(), Decimal('2000000'))
        engine._regime_adapter = MarketRegimeAdapter()
        reopened = ExecutionStateStore(db_path)
        replacement = KRExecutionRuntime(reopened, engine, ExitManager(persist=False, clock=lambda: clock[0]),
            clock=lambda: clock[0], risk_manager=sidecar, account_scope='scope')
        try:
            await replacement.restore()
            replacement.attach()
            restored_owner = RegimeOwner(replacement, adapter=engine._regime_adapter, sidecar=sidecar)
            inputs = Inputs()
            result = await restored_owner.diagnose_morning(inputs, NoModel())
            assert result.status == ('already_done' if completed else 'pending')
            assert inputs.calls == []
            assert not replacement.trading_ready
        finally:
            await replacement.shutdown()
            await reopened.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('stamp_zone', [timezone.utc, ZoneInfo('Asia/Seoul')], ids=['utc', 'kst'])
@pytest.mark.parametrize('age_days', [0, 1], ids=['today', 'yesterday'])
def test_same_kst_day_utc_horizon_blocks_attack_upgrade(tmp_path, monkeypatch, stamp_zone, age_days):
    """Valid UTC provenance can have yesterday's date while it is this KST day."""
    async def scenario():
        import test_execution_regime_owner as fixtures
        from test_execution_regime_morning import baseline
        from test_execution_regime_noon_replay import horizon_json
        from test_execution_runtime import NOW
        from src.execution.safety.regime_owner import RegimeOwner, RegimeHorizonBaseline
        from src.execution.safety.regime_morning import RegimeMorningBaseline
        now = NOW.replace(hour=8, minute=50)
        original = fixtures.baseline_json
        def bearish_baseline(runtime):
            value = original(runtime)
            value['trend_state']['mid_regime'] = 'bear'
            value['engine_regime'] = 'bear'
            return value
        monkeypatch.setattr(fixtures, 'NOW', now)
        monkeypatch.setattr(fixtures, 'baseline_json', bearish_baseline)
        _, _, store, runtime, writer, _ = await fixtures.owned(tmp_path, clock=lambda: now)
        try:
            horizon = horizon_json(runtime)
            horizon['horizon'] = {'level': 'crash', 'change_pct': -3.0,
                                  'classified_at': (now - timedelta(days=age_days)).astimezone(stamp_zone).isoformat()}
            await RegimeOwner.register_horizon_baseline(runtime, RegimeHorizonBaseline.from_dict(horizon),
                expected_version=runtime.owner.version)
            await runtime.owner.register_policy_generations('utc-horizon', ('regime_policy.horizon',))
            await RegimeOwner.register_morning_baseline(runtime, RegimeMorningBaseline.from_dict(baseline(runtime)),
                expected_version=runtime.owner.version)
            async def fetch(code):
                value = quote(code, pct=-3.0)
                value['open'] = 101.0
                value['_observation']['fields']['open']['value'] = 101.0
                return value
            assert (await writer.refresh_trend(SimpleNamespace(fetch_index_price=fetch))).status == 'accepted'
            assert runtime.owner.state['regime_policy']['trend_state']['mid_regime'] == 'bear'
            class LLM:
                async def complete(self, *args, **kwargs):
                    return SimpleNamespace(success=True, content='[공격] independent')
            assert (await writer.diagnose_morning(Inputs(), LLM())).receipt.status == 'accepted'
            assert runtime.owner.state['regime_policy']['trend_state']['mid_regime'] == ('bear' if age_days == 0 else 'sideways'), \
                'same KST day crash must block [공격] upgrade even with UTC provenance'
        finally:
            await runtime.shutdown()
            await store.close()
    asyncio.run(scenario())
