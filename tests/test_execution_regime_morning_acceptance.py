"""Independent C4 acceptance coverage for the installed morning scheduler path."""

import asyncio
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from src.core.types import MarketSession
from src.schedulers.kr_scheduler import KRScheduler
from test_execution_regime_owner import baseline_json, quote
from test_execution_runtime import setup


MORNING_CLOCK = datetime(2026, 9, 18, 8, 48, tzinfo=ZoneInfo("Asia/Seoul"))


@pytest.fixture(autouse=True)
def synthetic_home(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)


async def _owned_with_known_morning_baseline(tmp_path, *, clock=MORNING_CLOCK, mid_regime="sideways"):
    """The existing SQLite fixture, with all baseline evidence at 08:48."""
    from src.core.market_regime import MarketRegimeAdapter
    from src.core.types import RiskConfig
    from src.execution.safety.regime_owner import POLICY_READS, RegimeBaseline, RegimeOwner
    from src.execution.safety.risk_transition import IntradayPolicyState
    from src.risk.manager import RiskManager

    sidecar = RiskManager(RiskConfig(), Decimal("2000000"))
    engine, exits, store, runtime = await setup(
        tmp_path,
        account_scope="scope",
        risk_manager=sidecar,
        clock=lambda: clock,
    )
    adapter = MarketRegimeAdapter()
    engine._regime_adapter = adapter

    def seed(state):
        value = IntradayPolicyState("normal", 0.0, None, None).to_dict()
        state["intraday_policy"] = {
            "schema": 1,
            "baseline": value,
            "baseline_version": runtime.owner.version + 1,
            "current": value.copy(),
            "transitions": {},
        }
        state["entry_policy_effects"] = {
            "pending_sectors": {},
            "sidecar_active": True,
        }
        return state

    await runtime.owner.mutate("known-prerequisite", seed)
    supplied = baseline_json(runtime)
    supplied["evidence"]["observed_at"] = clock.isoformat()
    supplied["trend_state"]["mid_regime"] = mid_regime
    supplied["engine_regime"] = mid_regime
    supplied["trend_state"]["market_trend"]["classified_at"] = clock.isoformat()
    supplied["trend_state"]["last_update"] = clock.isoformat()
    await RegimeOwner.register_baseline(
        runtime,
        RegimeBaseline.from_dict(supplied),
        expected_version=runtime.owner.version,
    )
    await runtime.owner.register_policy_generations("known-reads", POLICY_READS)

    async def missing_vix():
        return None

    writer = RegimeOwner(
        runtime, adapter=adapter, sidecar=sidecar, vix_fetcher=missing_vix
    )
    ticket = await writer.sources.begin("fresh-vix", "vix_regime")
    await writer.sources.complete(
        ticket,
        "success",
        {"value": 20.0, "fetched_at": clock.isoformat()},
        source="synthetic-vix",
        source_event_id="fresh-vix",
        received_at=clock,
    )
    return engine, exits, store, runtime, writer, supplied


def test_installed_monitor_completes_owned_morning_diagnosis_within_legacy_window(
    tmp_path, monkeypatch
):
    """Catches the installed runtime ``continue`` that skips the C4 caller."""

    async def scenario():
        engine, _, store, runtime, owner, _ = await _owned_with_c4_baselines(tmp_path)
        index_calls = []

        class IndexProvider:
            async def fetch_index_price(self, code):
                index_calls.append(code)
                observation = quote(code)
                observation["_observation"]["received_at"] = MORNING_CLOCK.isoformat()
                return observation

        scheduler = object.__new__(KRScheduler)

        class ThemeDetector:
            _recent_news = []

            def get_active_themes(self):
                return []

        model_calls = []

        class LLM:
            async def complete(self, prompt, *, task, max_tokens):
                model_calls.append((task.name, max_tokens, prompt))
                return SimpleNamespace(success=True, content="[중립] synthetic", error="")

        bot = SimpleNamespace(
            running=True,
            engine=engine,
            risk_manager=owner.sidecar,
            kis_market_data=IndexProvider(),
            expert_orchestrator=None,
            theme_detector=ThemeDetector(),
            broker=None,
            telegram=None,
            _get_current_session=lambda: MarketSession.PRE_MARKET,
        )
        scheduler.bot = bot

        import src.schedulers.kr_scheduler as scheduler_module

        class FixedDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return MORNING_CLOCK if tz is None else MORNING_CLOCK.astimezone(tz)

        async def controlled_sleep(seconds):
            if seconds == 120:
                bot.running = False

        monkeypatch.setattr(scheduler_module, "datetime", FixedDatetime)
        import src.utils.llm as llm_module

        monkeypatch.setattr(llm_module, "get_llm_manager", lambda: LLM())
        monkeypatch.setattr(scheduler_module, "get_llm_manager", lambda: LLM(), raising=False)
        monkeypatch.setattr(
            scheduler_module,
            "asyncio",
            SimpleNamespace(sleep=controlled_sleep, CancelledError=asyncio.CancelledError),
        )
        try:
            await scheduler.run_market_trend_monitor()
            records = runtime.owner.state["risk_sources"]["records"].values()
            assert any(
                record["ticket"]["kind"] == "llm_morning_diagnosis"
                and record["terminal"]["receipt"]["status"] == "accepted"
                for record in records
            )
            assert index_calls == ["0001", "1001"]
            assert [(task, limit) for task, limit, _ in model_calls] == [("MARKET_ANALYSIS", 150)]
            assert owner.morning_assessment(now=MORNING_CLOCK)["assessment"] == "[중립] synthetic"
        finally:
            await runtime.shutdown()
            await store.close()

    asyncio.run(scenario())


def _c4_api():
    """The frozen C4 API is absent on the pre-overlay base by design.

    Its absence is reported as a skip, never as this file's behavioral RED.
    """
    try:
        from src.execution.safety.regime_morning import (
            RegimeMorningBaseline,
            RegimeMorningStageInput,
        )
    except ImportError:
        pytest.skip("C4 morning API absent on pre-overlay baseline; not behavioral RED")
    from src.execution.safety.regime_owner import RegimeOwner

    return RegimeMorningBaseline, RegimeMorningStageInput, RegimeOwner


def _observation_at(code, now):
    value = quote(code)
    value["_observation"]["received_at"] = now.isoformat()
    return value


async def _owned_with_c4_baselines(tmp_path, *, clock=MORNING_CLOCK, mid_regime="sideways"):
    """Register C2, C3, then C4 baselines through their real owner APIs."""
    RegimeMorningBaseline, _, RegimeOwner = _c4_api()
    from src.execution.safety.regime_owner import RegimeHorizonBaseline
    from src.execution.safety.intraday_owner import IntradayRiskOwner
    from test_execution_intraday_owner import batch_for

    engine, exits, store, runtime, owner, _ = await _owned_with_known_morning_baseline(
        tmp_path, clock=clock, mid_regime=mid_regime
    )
    batch = batch_for(engine, exits)
    intraday = IntradayRiskOwner(runtime, batch)

    class CurrentIndex:
        async def fetch_index_price(self, code):
            assert code == "0001"
            return _observation_at(code, clock)

    current = await intraday.refresh(CurrentIndex())
    assert current.status == "accepted"
    state = runtime.owner.state
    horizon = RegimeHorizonBaseline.from_dict(
        {
            "schema": 1,
            "baseline_id": "independent-c4-horizon",
            "account_scope": "scope",
            "business_day": clock.date().isoformat(),
            "generation": runtime._day_generation,
            "fence_id": runtime._day_fence_id,
            "evidence": {
                "source": "independent-c4-acceptance",
                "event_id": "known-horizon",
                "observed_at": clock.isoformat(),
            },
            "regime_baseline_version": state["regime_policy"]["baseline"]["baseline_version"],
            "intraday": {
                "baseline_version": state["intraday_policy"]["baseline_version"],
                "current": state["intraday_policy"]["current"],
            },
            "horizon": {"level": "normal", "change_pct": 0.0, "classified_at": None},
        }
    )
    await RegimeOwner.register_horizon_baseline(
        runtime, horizon, expected_version=runtime.owner.version
    )
    await runtime.owner.register_policy_generations(
        "independent-c4-policy-reads",
        (
            "regime_policy.horizon",
            "intraday_policy.current",
            "protection.config",
            "protection.current_regime",
            "protection.intraday_crash_level",
        ),
    )
    state = runtime.owner.state
    morning = RegimeMorningBaseline.from_dict(
        {
            "schema": 1,
            "baseline_id": "independent-c4-morning",
            "account_scope": "scope",
            "business_day": clock.date().isoformat(),
            "generation": runtime._day_generation,
            "fence_id": runtime._day_fence_id,
            "evidence": {
                "source": "independent-c4-acceptance",
                "event_id": "known-morning",
                "observed_at": clock.isoformat(),
            },
            "regime_baseline_version": state["regime_policy"]["baseline"]["baseline_version"],
            "horizon_baseline_version": state["regime_policy"]["horizon_baseline"]["baseline_version"],
            "morning": {
                "knowledge": "known",
                "assessment": None,
                "assessment_day": None,
                "open_expectation": None,
                "open_expectation_as_of": None,
            },
        }
    )
    await RegimeOwner.register_morning_baseline(
        runtime, morning, expected_version=runtime.owner.version
    )
    # The C4 caller is authorized only after a current real owned index trend;
    # this is not a manufactured source record.
    class CurrentTrend:
        async def fetch_index_price(self, code):
            return _observation_at(code, clock)

    trend = await owner.refresh_trend(CurrentTrend())
    assert trend.status == "accepted"
    return engine, exits, store, runtime, owner, batch


def _stage(Stage, stage, *, payload, outcome="success", clock=MORNING_CLOCK):
    return Stage.from_dict(
        {
            "schema": 1,
            "stage": stage,
            "outcome": outcome,
            "source": "independent-c4-external-fake",
            "event_id": f"{stage}-event",
            "received_at": clock.isoformat(),
            "market_as_of": None,
            "payload": payload,
        }
    )


def test_morning_owner_preserves_sequential_optional_order_and_model_contract(tmp_path):
    """Catches reordering/over-querying optional inputs or changing text-model limits."""

    async def scenario():
        _, _, store, runtime, owner, _ = await _owned_with_c4_baselines(tmp_path)
        _, Stage, _ = _c4_api()
        calls = []

        class Provider:
            def snapshot_themes(self):
                calls.append("theme")
                return _stage(Stage, "theme", payload={"text": "semiconductors"})

            def snapshot_symbols(self):
                calls.append("symbols")
                return ("000001", "000002", "000003", "000004", "000005")

            async def fetch_overtime_price(self, symbol):
                calls.append(f"overtime:{symbol}")
                return _stage(
                    Stage,
                    "overtime",
                    payload={"symbol": symbol, "quote": {"price": 100.0, "change_pct": 0.0}},
                )

            def snapshot_news(self):
                calls.append("news")
                return _stage(Stage, "news", payload={"text": "headline"})

            async def fetch_macro_context(self):
                calls.append("macro")
                return _stage(Stage, "macro", payload={"text": "macro"})

        class LLM:
            async def complete(self, prompt, *, task, max_tokens):
                calls.append("model")
                assert "semiconductors" in prompt and "headline" in prompt and "macro" in prompt
                assert task.name == "MARKET_ANALYSIS" and max_tokens == 150
                return SimpleNamespace(success=True, content="[방어] synthetic", error="")

        try:
            result = await owner.diagnose_morning(Provider(), LLM())
            assert result.status == "completed"
            assert result.receipt.status == "accepted", result.receipt
            assert calls == [
                "theme",
                "symbols",
                "overtime:000001",
                "overtime:000002",
                "overtime:000003",
                "overtime:000004",
                "overtime:000005",
                "news",
                "macro",
                "model",
            ]
            assessment = owner.morning_assessment(now=MORNING_CLOCK)
            assert assessment["assessment"] == "[방어] synthetic"
            assert assessment["assessment_day"] == MORNING_CLOCK.date().isoformat()
            assert assessment["open_expectation"] == "[방어] synthetic"
            assert assessment["source_ref"]["committed_version"] == result.receipt.committed_version
            from src.execution.safety import risk_policy
            from src.execution.safety.policy_snapshot import PolicyContext, build_owned_snapshot
            from test_execution_risk_policy import snapshot

            context = replace(
                PolicyContext.from_snapshot(snapshot(risk_policy)),
                business_day=MORNING_CLOCK.date(),
                observed_at=MORNING_CLOCK,
                trend=risk_policy.MarketTrendPolicySnapshot(True, False, True),
            )
            selected = build_owned_snapshot(
                runtime.owner.state,
                context=context,
                version=runtime.owner.version,
                now=MORNING_CLOCK,
                prices={},
            )
            assert selected.versions.regime == result.receipt.committed_version
            expired = owner.morning_assessment(now=MORNING_CLOCK.replace(hour=9, minute=30))
            next_day = owner.morning_assessment(now=MORNING_CLOCK + timedelta(days=1))
            assert expired["open_expectation"] is None
            assert next_day["open_expectation"] is None
            assert next_day["assessment"] == "[방어] synthetic"
            class NextIndex:
                async def fetch_index_price(self, code):
                    return _observation_at(code, MORNING_CLOCK)

            next_trend = await owner.refresh_trend(NextIndex())
            assert next_trend.status == "accepted"
            technical = build_owned_snapshot(
                runtime.owner.state,
                context=context,
                version=runtime.owner.version,
                now=MORNING_CLOCK,
                prices={},
            )
            assert technical.versions.regime == next_trend.committed_version
            # A following index refresh owns current policy again, but cannot
            # erase the historical morning diagnosis or re-send it.
            assert owner.morning_assessment(now=MORNING_CLOCK)["assessment"] == "[방어] synthetic"
            assert runtime.owner.state["regime_policy"]["schema"] == 3
            assert not runtime.trading_ready and not runtime.owner.state["outbox"]
        finally:
            await runtime.shutdown()
            await store.close()

    asyncio.run(scenario())


def test_morning_owner_excludes_0847_and_0856_but_includes_both_window_endpoints(tmp_path):
    """Catches widening/narrowing the legacy 08:48:00..08:55:59 eligibility window."""

    async def scenario():
        _, Stage, _ = _c4_api()
        cases = (
            ((8, 47), "outside_window", None),
            ((8, 48), "completed", "failed"),
            ((8, 55), "completed", "failed"),
            ((8, 56), "outside_window", None),
        )
        for (hour, minute), expected, receipt_status in cases:
            clock = MORNING_CLOCK.replace(hour=hour, minute=minute)
            _, _, store, runtime, owner, _ = await _owned_with_c4_baselines(
                tmp_path / f"{hour}{minute}", clock=clock
            )

            class Provider:
                def snapshot_themes(self):
                    return _stage(Stage, "theme", payload={"text": ""}, clock=clock)

                def snapshot_symbols(self):
                    return ()

                async def fetch_overtime_price(self, symbol):
                    raise AssertionError("no symbols means no overtime lookup")

                def snapshot_news(self):
                    return _stage(Stage, "news", payload={"text": ""}, clock=clock)

                async def fetch_macro_context(self):
                    return _stage(Stage, "macro", payload={"text": ""}, clock=clock)

            class EmptyLLM:
                async def complete(self, *args, **kwargs):
                    return SimpleNamespace(success=False, content="", error="synthetic")

            try:
                result = await owner.diagnose_morning(Provider(), EmptyLLM())
                assert result.status == expected
                if receipt_status is None:
                    assert result.receipt is None
                else:
                    assert result.receipt.status == receipt_status
            finally:
                await runtime.shutdown()
                await store.close()

    asyncio.run(scenario())


def test_morning_failed_model_response_retries_but_accepted_success_is_once_per_day(tmp_path):
    """Catches converting success-dedupe into attempt-dedupe, or duplicate accepted models."""

    async def scenario():
        _, Stage, _ = _c4_api()
        _, _, store, runtime, owner, _ = await _owned_with_c4_baselines(
            tmp_path, mid_regime="bear"
        )
        calls = []

        class Provider:
            def snapshot_themes(self):
                return _stage(Stage, "theme", payload={"text": ""})

            def snapshot_symbols(self):
                return ()

            async def fetch_overtime_price(self, symbol):
                raise AssertionError("no symbols means no overtime lookup")

            def snapshot_news(self):
                return _stage(Stage, "news", payload={"text": ""})

            async def fetch_macro_context(self):
                return _stage(Stage, "macro", payload={"text": ""})

        class RetryLLM:
            async def complete(self, *args, **kwargs):
                calls.append("model")
                if len(calls) == 1:
                    return SimpleNamespace(success=False, content="", error="synthetic failure")
                return SimpleNamespace(success=True, content="[중립] synthetic", error="")

        try:
            first = await owner.diagnose_morning(Provider(), RetryLLM())
            second = await owner.diagnose_morning(Provider(), RetryLLM())
            third = await owner.diagnose_morning(Provider(), RetryLLM())
            assert first.status == "completed" and first.receipt.status == "failed"
            assert second.status == "completed" and second.receipt.status == "accepted"
            assert third.status == "already_done"
            assert calls == ["model", "model"]
            assert owner.morning_assessment(now=MORNING_CLOCK)["assessment_day"] == "2026-09-18"
        finally:
            await runtime.shutdown()
            await store.close()

    asyncio.run(scenario())


def test_concurrent_morning_call_returns_pending_without_second_model_request(tmp_path):
    """Catches concurrent source admission that issues a second text-model request."""

    async def scenario():
        _, Stage, _ = _c4_api()
        _, _, store, runtime, owner, _ = await _owned_with_c4_baselines(tmp_path)
        entered, release = asyncio.Event(), asyncio.Event()

        class Provider:
            def snapshot_themes(self): return _stage(Stage, "theme", payload={"text": ""})
            def snapshot_symbols(self): return ()
            async def fetch_overtime_price(self, symbol): raise AssertionError("no symbols")
            def snapshot_news(self): return _stage(Stage, "news", payload={"text": ""})
            async def fetch_macro_context(self): return _stage(Stage, "macro", payload={"text": ""})

        class BlockingLLM:
            calls = 0

            async def complete(self, *args, **kwargs):
                self.calls += 1
                entered.set()
                await release.wait()
                return SimpleNamespace(success=True, content="[중립] synthetic", error="")

        llm = BlockingLLM()
        first = asyncio.create_task(owner.diagnose_morning(Provider(), llm))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            second = await owner.diagnose_morning(Provider(), llm)
            assert second.status == "pending" and llm.calls == 1
            release.set()
            completed = await asyncio.wait_for(first, 2)
            assert completed.status == "completed" and llm.calls == 1
        finally:
            release.set()
            await asyncio.gather(first, return_exceptions=True)
            await runtime.shutdown()
            await store.close()

    asyncio.run(scenario())


def test_installed_monitor_observes_all_four_morning_window_boundaries(tmp_path, monkeypatch):
    """Catches a scheduler window that differs from the owner eligibility window."""

    async def scenario():
        import src.schedulers.kr_scheduler as scheduler_module
        import src.utils.llm as llm_module

        for hour, minute, expected_models in ((8, 47, 0), (8, 48, 1), (8, 55, 1), (8, 56, 0)):
            clock = MORNING_CLOCK.replace(hour=hour, minute=minute)
            engine, _, store, runtime, owner, _ = await _owned_with_c4_baselines(
                tmp_path / f"window-{hour}{minute}", clock=clock
            )
            index_calls, model_calls = [], []

            class IndexProvider:
                async def fetch_index_price(self, code):
                    index_calls.append(code)
                    return _observation_at(code, clock)

            class ThemeDetector:
                _recent_news = []

                def get_active_themes(self):
                    return []

            class LLM:
                async def complete(self, prompt, *, task, max_tokens):
                    model_calls.append((task.name, max_tokens))
                    return SimpleNamespace(success=True, content="[중립] synthetic", error="")

            scheduler = object.__new__(KRScheduler)
            bot = SimpleNamespace(
                running=True,
                engine=engine,
                risk_manager=owner.sidecar,
                kis_market_data=IndexProvider(),
                expert_orchestrator=None,
                theme_detector=ThemeDetector(),
                broker=None,
                telegram=None,
                _get_current_session=lambda: MarketSession.PRE_MARKET,
            )
            scheduler.bot = bot

            class FixedDatetime(datetime):
                @classmethod
                def now(cls, tz=None):
                    return clock if tz is None else clock.astimezone(tz)

            async def controlled_sleep(seconds):
                if seconds == 120:
                    bot.running = False

            try:
                with monkeypatch.context() as patch:
                    patch.setattr(scheduler_module, "datetime", FixedDatetime)
                    patch.setattr(
                        scheduler_module,
                        "asyncio",
                        SimpleNamespace(sleep=controlled_sleep, CancelledError=asyncio.CancelledError),
                    )
                    patch.setattr(llm_module, "get_llm_manager", lambda: LLM())
                    patch.setattr(scheduler_module, "get_llm_manager", lambda: LLM(), raising=False)
                    await scheduler.run_market_trend_monitor()
                assert index_calls == ["0001", "1001"]
                assert len(model_calls) == expected_models
                if expected_models:
                    assert model_calls == [("MARKET_ANALYSIS", 150)]
                    assert owner.morning_assessment(now=clock)["assessment"] == "[중립] synthetic"
                else:
                    assert owner.morning_assessment(now=clock)["assessment"] is None
            finally:
                await runtime.shutdown()
                await store.close()

    asyncio.run(scenario())


def test_cold_restore_preserves_accepted_morning_and_skips_external_replay(tmp_path):
    """Catches losing the accepted-day guard or display projection on cold restore."""

    async def scenario():
        _, Stage, RegimeOwner = _c4_api()
        engine, exits, store, runtime, owner, _ = await _owned_with_c4_baselines(tmp_path)

        class Provider:
            def snapshot_themes(self): return _stage(Stage, "theme", payload={"text": ""})
            def snapshot_symbols(self): return ()
            async def fetch_overtime_price(self, symbol): raise AssertionError("no symbols")
            def snapshot_news(self): return _stage(Stage, "news", payload={"text": ""})
            async def fetch_macro_context(self): return _stage(Stage, "macro", payload={"text": ""})

        class LLM:
            calls = 0

            async def complete(self, *args, **kwargs):
                self.calls += 1
                return SimpleNamespace(success=True, content="[중립] durable", error="")

        llm = LLM()
        try:
            accepted = await owner.diagnose_morning(Provider(), llm)
            assert accepted.status == "completed" and llm.calls == 1
            await runtime.shutdown()
            await store.close()

            from src.core.engine import UnifiedEngine
            from src.core.market_regime import MarketRegimeAdapter
            from src.core.types import RiskConfig, TradingConfig
            from src.execution.safety.runtime import KRExecutionRuntime
            from src.execution.safety.store import ExecutionStateStore
            from src.risk.manager import RiskManager

            restored_engine = UnifiedEngine(TradingConfig(initial_capital=Decimal("2000000")))
            restored_sidecar = RiskManager(RiskConfig(), Decimal("2000000"))
            restored_store = ExecutionStateStore(tmp_path / "execution" / "state.sqlite3")
            restored_runtime = KRExecutionRuntime(
                restored_store,
                restored_engine,
                exits,
                clock=lambda: MORNING_CLOCK,
                risk_manager=restored_sidecar,
                account_scope="scope",
            )
            await restored_runtime.restore()
            restored_runtime.attach()
            restored_adapter = MarketRegimeAdapter()
            restored_engine._regime_adapter = restored_adapter
            restored_owner = RegimeOwner(
                restored_runtime,
                adapter=restored_adapter,
                sidecar=restored_sidecar,
                vix_fetcher=lambda: None,
            )
            try:
                restored = restored_owner.morning_assessment(now=MORNING_CLOCK)
                assert restored["assessment"] == "[중립] durable"
                duplicate = await restored_owner.diagnose_morning(Provider(), llm)
                assert duplicate.status == "already_done" and llm.calls == 1
            finally:
                await restored_runtime.shutdown()
                await restored_store.close()
        finally:
            # The first runtime/store are already closed on the restore path.
            if not runtime._closing:
                await runtime.shutdown()

    asyncio.run(scenario())


def test_owned_morning_prompt_and_defense_attack_order_match_legacy_text_path(tmp_path, monkeypatch):
    """Catches C4 prompt drift or changing legacy defense-before-attack semantics."""

    async def scenario():
        from src.core.market_regime import MarketRegimeAdapter

        _, Stage, _ = _c4_api()
        _, _, store, runtime, owner, _ = await _owned_with_c4_baselines(
            tmp_path, mid_regime="bear"
        )
        theme, news, macro = "semiconductors", "headline", "macro"
        quotes = {
            "000001": {"price": 100.0, "change_pct": 0.0, "volume": 0},
            "000002": {"price": 101.0, "change_pct": 0.1, "volume": 10},
        }
        raw = "[방어] [공격] synthetic"
        owned_prompts, legacy_prompts = [], []

        class Provider:
            def snapshot_themes(self): return _stage(Stage, "theme", payload={"text": theme})
            def snapshot_symbols(self): return tuple(quotes)
            async def fetch_overtime_price(self, symbol):
                return _stage(Stage, "overtime", payload={"symbol": symbol, "quote": quotes[symbol]})
            def snapshot_news(self): return _stage(Stage, "news", payload={"text": news})
            async def fetch_macro_context(self): return _stage(Stage, "macro", payload={"text": macro})

        class OwnedLLM:
            async def complete(self, prompt, *, task, max_tokens):
                owned_prompts.append((prompt, task.name, max_tokens))
                return SimpleNamespace(success=True, content=raw, error="")

        legacy = MarketRegimeAdapter()
        legacy._current_regime = owner.adapter._current_regime
        legacy._regime_data = deepcopy(owner.adapter._regime_data)

        class LegacyLLM:
            async def complete(self, prompt, *, task, max_tokens):
                legacy_prompts.append((prompt, task.name, max_tokens))
                return SimpleNamespace(success=True, content=raw, error="")

        async def macro_context(_key):
            return macro

        monkeypatch.setenv("PERPLEXITY_API_KEY", "synthetic")
        monkeypatch.setattr(legacy, "_fetch_perplexity_context", macro_context)
        try:
            await legacy.llm_morning_diagnosis(
                LegacyLLM(),
                theme_summary=theme,
                premarket_data=quotes,
                news_headlines=news,
            )
            result = await owner.diagnose_morning(Provider(), OwnedLLM())
            assert result.receipt.status == "accepted"
            assert owned_prompts == legacy_prompts == [(owned_prompts[0][0], "MARKET_ANALYSIS", 150)]
            assert legacy._current_regime == owner.adapter._current_regime

            # Execute the actual legacy if/elif path on bear, then compare its
            # result with the owned pure branch that C4 seals in its reducer.
            legacy_bear = MarketRegimeAdapter()
            legacy_bear._current_regime = "bear"
            legacy_bear._regime_data = deepcopy(owner.adapter._regime_data)
            monkeypatch.setattr(legacy_bear, "_fetch_perplexity_context", macro_context)
            await legacy_bear.llm_morning_diagnosis(
                LegacyLLM(), theme_summary=theme, premarket_data=quotes, news_headlines=news
            )
            from src.core.market_regime import morning_mid_regime
            assert legacy_bear._current_regime == morning_mid_regime("bear", None, raw) == "sideways"
        finally:
            await runtime.shutdown()
            await store.close()

    asyncio.run(scenario())
