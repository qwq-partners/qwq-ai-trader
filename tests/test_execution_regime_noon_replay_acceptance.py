"""Independent C3 acceptance: real queue/SQLite/protection, fake external stages only.

This file deliberately waits for the frozen C3 public API.  An absent export on
the C2-only base is a bootstrap gap, not a behavioral acceptance failure.
"""
import asyncio
from copy import deepcopy
from dataclasses import fields
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest

try:
    from src.execution.safety.regime_owner import (
        RegimeApplicationReceipt,
        RegimeHorizonBaseline,
        RegimeOwner,
        RegimeStageInput,
        RegimeSyncContext,
    )
except ImportError:
    pytest.skip("C3 public regime-owner API is not present on the C2-only base", allow_module_level=True)

from test_execution_intraday_replay_integration import failed_fill
from test_execution_intraday_replay_integration import refresh as refresh_intraday
from test_execution_intraday_owner import batch_for
from test_execution_runtime import opened, observed, queued
from test_execution_regime_owner import NOW, owned as owned_c2, quote


POLICY_READS = (
    "regime_policy.horizon",
    "intraday_policy.current",
    "protection.config",
    "protection.current_regime",
    "protection.intraday_crash_level",
)


@pytest.fixture(autouse=True)
def synthetic_home(tmp_path, monkeypatch):
    """All real collaborators remain in the temporary test state tree."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)


class StageProvider:
    """C3's complete external-stage boundary; no source/result rows are faked."""

    def __init__(self, *, now, entries=None, stage_release=None, held_stage=None, context=None):
        self.now = now
        self.entries = entries or {}
        self.stage_release = stage_release
        self.held_stage = held_stage
        self.context = context or sync_context(now)
        self.calls = []

    async def _stage(self, name, payload):
        self.calls.append(name)
        event = self.entries.get(name)
        if event is not None:
            event.set()
        if self.stage_release is not None and name == self.held_stage:
            await self.stage_release.wait()
        return RegimeStageInput.from_dict(payload)

    async def read_daily_bias(self):
        return await self._stage("daily_bias", stage_input(
            "daily_bias", {"data": {"assessment": "unknown", "top_lesson": ""}}, received_at=self.now))

    async def fetch_us_overnight(self):
        return await self._stage("us_overnight", stage_input(
            "us_overnight", {"overnight": {"indices": {}, "indices_normalized": {}}}, received_at=self.now))

    def snapshot_screener(self):
        self.calls.append("screener")
        return RegimeStageInput.from_dict(stage_input("screener", {
            "closes": [100.0, 101.0, 102.0, 103.0, 104.0, 105.0],
            "last_bar_date": (self.now - timedelta(days=1)).date().isoformat(),
            "loaded_at": self.now.isoformat(),
        }, received_at=self.now))

    async def fetch_index_price(self, index_code):
        value = quote(index_code, pct=-3.0 if index_code == "0001" else -1.0)
        value["_observation"]["received_at"] = self.now.isoformat()
        return await self._stage("index" + index_code, stage_input(
            "index" + index_code, {"quote": value}, received_at=self.now))

    def snapshot_application_context(self, *, captured_at):
        self.calls.append("application_context")
        assert captured_at == self.now
        return self.context


class QuickLLM:
    """Only the external model call is fake; assertions inspect owner receipts/state."""

    def __init__(self, result=None, *, entered=None, release=None, raises=None):
        self.result = result if result is not None else {"regime": "trending_bear", "confidence": 0.8}
        self.entered, self.release, self.raises, self.calls = entered, release, raises, []

    async def complete_json(self, *, prompt, system, task):
        self.calls.append((prompt, system, task))
        if self.entered is not None:
            self.entered.set()
        if self.release is not None:
            await self.release.wait()
        if self.raises is not None:
            raise self.raises
        return deepcopy(self.result)


def stage_input(stage, payload, *, received_at):
    return {
        "schema": 1, "stage": stage, "outcome": "success", "source": "acceptance-test",
        "event_id": "stage-" + stage, "received_at": received_at.isoformat(), "market_as_of": None,
        "payload": payload,
    }


def sync_context(captured_at, *, guard=True, regime="neutral", provenance="acceptance-test"):
    return RegimeSyncContext.from_dict({
        "schema": 1, "captured_at": captured_at.isoformat(),
        "regime_conflict_guard_enabled": guard,
        "screener": {"regime": regime if guard else None, "source": provenance,
                     "event_id": None, "observed_at": None},
    })


async def owned_c3(tmp_path, *, clock=None):
    engine, exits, store, runtime, owner, _ = await owned_c2(tmp_path, clock=clock)
    state = runtime.owner.state
    baseline = RegimeHorizonBaseline.from_dict({
        "schema": 1, "baseline_id": "acceptance-c3-horizon-1", "account_scope": "scope",
        "business_day": NOW.date().isoformat(), "generation": runtime._day_generation,
        "fence_id": runtime._day_fence_id,
        "evidence": {"source": "acceptance-test", "event_id": "known-horizon-1", "observed_at": NOW.isoformat()},
        "regime_baseline_version": state["regime_policy"]["baseline"]["baseline_version"],
        "intraday": {"baseline_version": state["intraday_policy"]["baseline_version"],
                     "current": deepcopy(state["intraday_policy"]["current"])},
        "horizon": {"level": "normal", "change_pct": 0.0, "classified_at": None},
    })
    await RegimeOwner.register_horizon_baseline(runtime, baseline, expected_version=runtime.owner.version)
    await runtime.owner.register_policy_generations("acceptance-c3-reads", POLICY_READS)
    return engine, exits, store, runtime, owner


def snapshot_roots(state):
    return {key: deepcopy(state.get(key)) for key in (
        "portfolio", "risk", "lots", "intents", "attempts", "reservations", "inbox", "outbox",
        "startup_reconciliation", "risk_sources", "regime_policy", "intraday_policy",
    )}


async def set_protection_stop(runtime, value):
    """Fixture setup only: mutate the real durable global config before C3 capture."""
    def reduce(state):
        state["protection"]["config"]["stop_loss_pct"] = value
        return state
    await runtime.owner.mutate("acceptance-global-stop-" + str(value), reduce)


async def add_healthy_manager_state(runtime, *, symbol="000660", is_core=False):
    """Use the real ExitManager codec/state registration for whole-manager branch setup."""
    from src.core.types import Position
    from src.execution.safety.economics import decode_portfolio, encode_portfolio
    from src.execution.safety.protection import decode_protection, encode_protection
    def reduce(state):
        portfolio = decode_portfolio(state["portfolio"])
        position = Position(symbol=symbol, quantity=10, avg_price=Decimal("10000"),
                            current_price=Decimal("10000"), strategy="sepa_trend", entry_time=NOW)
        portfolio.positions[symbol] = position
        exits = decode_protection(state["protection"], clock=lambda: NOW)
        exits.register_position(position, is_core=is_core)
        state["portfolio"], state["protection"] = encode_portfolio(portfolio), encode_protection(exits)
        return state
    await runtime.owner.mutate("acceptance-healthy-manager-" + symbol, reduce)


def install_intraday_owner(engine, exits, runtime):
    from src.execution.safety.intraday_owner import IntradayRiskOwner
    batch = batch_for(engine, exits)
    IntradayRiskOwner(runtime, batch)
    return batch


async def reopen_c3(engine, exits, store, runtime, *, clock):
    """Real SQLite cold path: no baseline or replay evidence is recreated."""
    from src.core.engine import UnifiedEngine
    from src.core.market_regime import MarketRegimeAdapter
    from src.core.types import RiskConfig, TradingConfig
    from src.execution.safety.runtime import KRExecutionRuntime
    from src.execution.safety.store import ExecutionStateStore
    from src.risk.manager import RiskManager
    from src.strategies.exit_manager import ExitManager
    path = store.path
    await runtime.shutdown()
    await store.close()
    fresh_engine = UnifiedEngine(TradingConfig(initial_capital=Decimal("2000000")))
    fresh_exits = ExitManager(persist=False, clock=clock)
    fresh_store = ExecutionStateStore(path)
    sidecar = RiskManager(RiskConfig(), Decimal("2000000"))
    fresh_runtime = KRExecutionRuntime(fresh_store, fresh_engine, fresh_exits, clock=clock,
                                       risk_manager=sidecar, account_scope="scope")
    fresh_engine._regime_adapter = MarketRegimeAdapter()
    await fresh_runtime.restore()
    fresh_runtime.attach()
    owner = RegimeOwner(fresh_runtime, adapter=fresh_engine._regime_adapter, sidecar=sidecar)
    return fresh_engine, fresh_exits, fresh_store, fresh_runtime, owner


def assert_receipt_shape(receipt):
    assert type(receipt) is RegimeApplicationReceipt
    assert tuple(field.name for field in fields(receipt)) == (
        "operation_id", "status", "reason", "committed_version", "classifier_operation_id", "classifier_version")


def test_c3_contract_fixture_registers_explicit_detached_horizon_baseline(tmp_path):
    async def scenario():
        _, _, store, runtime, _ = await owned_c3(tmp_path)
        try:
            root = runtime.owner.state["regime_policy"]
            assert root["schema"] == 2
            assert set(("horizon_baseline", "horizon", "noon_caps", "applications")) <= set(root)
            assert root["horizon"]["classified_at"] is None
            assert root["horizon"]["writer_kind"] == "baseline"
            assert (await store.load())[1] == runtime.owner.state
        finally:
            await runtime.shutdown()
            await store.close()
    asyncio.run(scenario())


def test_c3_classifier_acceptance_receipt_is_atomic_with_source_and_application(tmp_path):
    async def scenario():
        clock = [NOW]
        _, _, store, runtime, owner = await owned_c3(tmp_path, clock=lambda: clock[0])
        provider, llm = StageProvider(now=clock[0]), QuickLLM()
        try:
            receipt = await owner.classify("12:00 (장중 업데이트)", inputs_provider=provider, llm=llm)
            application = owner.classifier_application_receipt(receipt.operation_id)
            assert receipt.status == "accepted"
            assert application is not None
            assert_receipt_shape(application)
            assert application.classifier_operation_id == receipt.operation_id
            state = runtime.owner.state
            assert receipt.operation_id in state["risk_sources"]["records"]
            assert application.operation_id in state["regime_policy"]["applications"]
            assert len(llm.calls) == 1
            assert not runtime.trading_ready
            assert (await store.load())[1] == state
        finally:
            await runtime.shutdown()
            await store.close()
    asyncio.run(scenario())


def test_rest_market_as_of_none_is_preserved_in_durable_classifier_stage_evidence(tmp_path):
    async def scenario():
        clock = [NOW]
        _, _, store, runtime, owner = await owned_c3(tmp_path, clock=lambda: clock[0])
        try:
            source = await owner.classify("12:00 (장중 업데이트)",
                                          inputs_provider=StageProvider(now=clock[0]), llm=QuickLLM())
            seal = runtime.owner.state["risk_input_seals"]["records"][source.operation_id]["original"]
            index = seal["request"]["inputs"]["stages"]["index0001"]
            observation = index["payload"]["quote"]["_observation"]
            assert index["market_as_of"] is None
            assert observation["market_as_of"] is None
            assert observation["received_at"] == clock[0].isoformat()
            assert runtime.owner.state["regime_policy"]["horizon"]["classified_at"] != observation["market_as_of"]
        finally:
            await runtime.shutdown()
            await store.close()
    asyncio.run(scenario())


def test_noon_cap_is_durable_before_the_owner_releases_the_model(tmp_path):
    async def scenario():
        clock = [NOW]
        engine, exits, store, runtime, owner = await owned_c3(tmp_path, clock=lambda: clock[0])
        entered, stage_release, model_release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        stage_entered = asyncio.Event()
        llm = QuickLLM(entered=entered, release=model_release)
        provider = StageProvider(now=clock[0], entries={"daily_bias": stage_entered},
                                 stage_release=stage_release, held_stage="daily_bias")
        before = deepcopy(runtime.owner.state)
        task = asyncio.create_task(owner.classify("12:00 (장중 업데이트)", inputs_provider=provider, llm=llm))
        try:
            await asyncio.wait_for(stage_entered.wait(), 2)
            assert runtime.owner.state["risk_sources"]["records"]
            assert runtime.owner.state["regime_policy"]["noon_caps"] == {}
            stage_release.set()
            await asyncio.wait_for(entered.wait(), 2)
            held = runtime.owner.state
            assert held["regime_policy"]["noon_caps"]
            assert held["regime_policy"]["horizon"]["writer_kind"] == "noon_index"
            assert held["intraday_policy"] == before["intraday_policy"]
            assert held["protection"] == before["protection"]
            assert held.get("protection_replay") == before.get("protection_replay")
            assert (await store.load())[1] == held
            model_release.set()
            source = await asyncio.wait_for(task, 2)
            assert source.status == "accepted" and len(llm.calls) == 1
            application = owner.classifier_application_receipt(source.operation_id)
            assert application is not None and application.committed_version == source.committed_version
        finally:
            stage_release.set(); model_release.set()
            await asyncio.gather(task, return_exceptions=True)
            await runtime.shutdown()
            await store.close()
    asyncio.run(scenario())


def test_sync_exact_retry_preserves_original_receipt_and_context_conflict_is_closed(tmp_path):
    async def scenario():
        clock = [NOW]
        _, _, store, runtime, owner = await owned_c3(tmp_path, clock=lambda: clock[0])
        try:
            source = await owner.classify("12:00 (장중 업데이트)", inputs_provider=StageProvider(now=clock[0]), llm=QuickLLM())
            context = sync_context(clock[0])
            first = await owner.sync_protection("regime-sync:acceptance", supplied_context=context)
            same = await owner.sync_protection("regime-sync:acceptance", supplied_context=context)
            assert first == same
            assert_receipt_shape(first)
            different = sync_context(clock[0], regime="bear")
            with pytest.raises(ValueError, match="regime_application_request_conflict"):
                await owner.sync_protection("regime-sync:acceptance", supplied_context=different)
            assert owner.classifier_application_receipt(source.operation_id) is not None
        finally:
            await runtime.shutdown()
            await store.close()
    asyncio.run(scenario())


def test_sync_retry_after_new_classifier_keeps_the_original_classifier_receipt(tmp_path):
    """An idempotent sync operation is bound to its first durable classifier."""
    async def scenario():
        clock = [NOW]
        _, _, store, runtime, owner = await owned_c3(tmp_path, clock=lambda: clock[0])
        try:
            first_source = await owner.classify(
                "12:00 (장중 업데이트)", inputs_provider=StageProvider(now=clock[0]),
                llm=QuickLLM({"regime": "trending_bear", "confidence": 0.8}))
            context = sync_context(clock[0])
            first = await owner.sync_protection("regime-sync:retained-retry", supplied_context=context)
            second_source = await owner.classify(
                "12:00 (장중 업데이트)", inputs_provider=StageProvider(now=clock[0]),
                llm=QuickLLM({"regime": "neutral", "confidence": 0.6}))
            retry = await owner.sync_protection("regime-sync:retained-retry", supplied_context=context)
            assert retry == first
            assert retry.classifier_operation_id == first_source.operation_id
            assert retry.classifier_operation_id != second_source.operation_id
            row = runtime.owner.state["regime_policy"]["applications"]["regime-sync:retained-retry"]
            assert row["classifier_ref"]["operation_id"] == first_source.operation_id
        finally:
            await runtime.shutdown()
            await store.close()
    asyncio.run(scenario())


def test_concurrent_sync_same_id_with_different_context_conflicts_after_the_first_await(tmp_path, monkeypatch):
    async def scenario():
        clock = [NOW]
        _, _, store, runtime, owner = await owned_c3(tmp_path, clock=lambda: clock[0])
        try:
            await owner.classify("12:00 (장중 업데이트)", inputs_provider=StageProvider(now=clock[0]), llm=QuickLLM())
            entered, release = asyncio.Event(), asyncio.Event()
            original, calls = runtime.owner.mutate, 0
            async def held(command, reducer):
                nonlocal calls
                calls += 1
                if calls == 1:
                    entered.set()
                    await release.wait()
                return await original(command, reducer)
            monkeypatch.setattr(runtime.owner, "mutate", held)
            first = asyncio.create_task(owner.sync_protection(
                "regime-sync:concurrent", supplied_context=sync_context(clock[0])))
            await asyncio.wait_for(entered.wait(), 2)
            second = asyncio.create_task(owner.sync_protection(
                "regime-sync:concurrent", supplied_context=sync_context(clock[0], regime="bear")))
            release.set()
            assert (await asyncio.wait_for(first, 2)).operation_id == "regime-sync:concurrent"
            with pytest.raises(ValueError, match="regime_application_request_conflict"):
                await asyncio.wait_for(second, 2)
        finally:
            release.set() if "release" in locals() else None
            await asyncio.gather(*(task for task in (locals().get("first"), locals().get("second")) if task),
                                 return_exceptions=True)
            await runtime.shutdown()
            await store.close()
    asyncio.run(scenario())


def test_classifier_model_failure_keeps_accepted_noon_cap_without_application_or_replay(tmp_path):
    async def scenario():
        clock = [NOW]
        _, _, store, runtime, owner = await owned_c3(tmp_path, clock=lambda: clock[0])
        try:
            receipt = await owner.classify("12:00 (장중 업데이트)", inputs_provider=StageProvider(now=clock[0]),
                                           llm=QuickLLM(raises=RuntimeError("external-model-failure")))
            state = runtime.owner.state
            assert receipt.status != "accepted"
            assert state["regime_policy"]["noon_caps"]
            assert owner.classifier_application_receipt(receipt.operation_id) is None
            assert state["regime_policy"]["applications"] == {}
            assert not state.get("protection_replay")
            assert (await store.load())[1] == state
        finally:
            await runtime.shutdown()
            await store.close()
    asyncio.run(scenario())


def test_same_label_global_only_change_is_applied_and_repairable_when_original_states_are_empty(tmp_path, monkeypatch):
    async def scenario():
        clock = [NOW]
        engine, exits, store, runtime, owner = await owned_c3(tmp_path, clock=lambda: clock[0])
        try:
            await set_protection_stop(runtime, 9.0)
            await failed_fill(engine, runtime, monkeypatch)
            source = await owner.classify(
                "12:00 (장중 업데이트)", inputs_provider=StageProvider(now=clock[0]),
                llm=QuickLLM({"regime": "neutral", "confidence": 0.6}))
            application = owner.classifier_application_receipt(source.operation_id)
            assert application is not None and application.status == "applied"
            row = runtime.owner.state["regime_policy"]["applications"][application.operation_id]
            assert row["calculation"]["global_before_digest"] != row["calculation"]["global_after_digest"]
            events = runtime.owner.state["protection_replay"]["005930"]["events"]
            assert events[-1]["kind"] == "regime_application"
            repaired = await runtime.repair_protection("same-label-global-repair", "005930", expected_version=runtime.owner.version)
            assert repaired.status == "APPLIED", repaired.reason
            assert exits.get_state("005930").stop_loss_pct == 4.0
        finally:
            await runtime.shutdown()
            await store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("is_core", (False, True))
def test_whole_original_manager_state_controls_same_label_replay_branch(tmp_path, monkeypatch, is_core):
    async def scenario():
        clock = [NOW]
        engine, exits, store, runtime, owner = await owned_c3(tmp_path, clock=lambda: clock[0])
        try:
            await set_protection_stop(runtime, 9.0)
            await add_healthy_manager_state(runtime, is_core=is_core)
            await failed_fill(engine, runtime, monkeypatch)
            source = await owner.classify(
                "12:00 (장중 업데이트)", inputs_provider=StageProvider(now=clock[0]),
                llm=QuickLLM({"regime": "neutral", "confidence": 0.6}))
            application = owner.classifier_application_receipt(source.operation_id)
            assert application is not None and application.status == "unchanged"
            row = runtime.owner.state["regime_policy"]["applications"][application.operation_id]
            assert row["calculation"]["original_protection_before"] == row["calculation"]["original_protection_after"]
            assert not runtime.owner.state.get("protection_replay", {}).get("005930", {}).get("events", [])[1:]
            repaired = await runtime.repair_protection("whole-manager-skip", "005930", expected_version=runtime.owner.version)
            assert repaired.status == "APPLIED", repaired.reason
            assert exits.get_state("005930").stop_loss_pct is None
            assert runtime.owner.state["protection"]["config"]["stop_loss_pct"] == 9.0
        finally:
            await runtime.shutdown()
            await store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("stage_name", ("first", "second"))
def test_classifier_application_matches_real_exit_manager_for_stage_core_atr_and_active_crash(tmp_path, monkeypatch,
                                                                                               stage_name):
    async def scenario():
        from src.execution.safety.protection import decode_protection, encode_protection
        from src.strategies.exit_manager import ExitStage
        clock = [NOW]
        engine, exits, store, runtime, owner = await owned_c3(tmp_path, clock=lambda: clock[0])
        batch = install_intraday_owner(engine, exits, runtime)
        try:
            await refresh_intraday(engine, batch, monkeypatch, -3.0, clock)
            await add_healthy_manager_state(runtime, symbol="000660")
            await add_healthy_manager_state(runtime, symbol="000001", is_core=True)
            def configure(state):
                manager = decode_protection(state["protection"], clock=lambda: clock[0])
                staged = manager.get_state("000660")
                staged.current_stage = ExitStage(stage_name)
                staged.atr_pct = 4.0
                staged.effective_trailing_stop_pct = 4.0
                state["protection"] = encode_protection(manager)
                return state
            await runtime.owner.mutate("acceptance-stage-core-atr-crash-" + stage_name, configure)
            before = deepcopy(runtime.owner.state["protection"])
            oracle = decode_protection(before, clock=lambda: clock[0])
            oracle.apply_regime_params("trending_bear", force=False)
            expected = encode_protection(oracle)
            source = await owner.classify("12:00 (장중 업데이트)",
                                          inputs_provider=StageProvider(now=clock[0]),
                                          llm=QuickLLM({"regime": "trending_bear", "confidence": 0.8}))
            application = owner.classifier_application_receipt(source.operation_id)
            record = runtime.owner.state["regime_policy"]["applications"][application.operation_id]["calculation"]
            assert record["force"] is False
            assert record["original_protection_before"] == before
            assert record["original_protection_after"] == expected
            assert expected["states"]["000001"] == before["states"]["000001"]  # core exclusion
            assert expected["states"]["000660"]["current_stage"] == stage_name
            assert expected["states"]["000660"]["effective_trailing_stop_pct"] == 4.0
            assert expected["intraday_crash_level"] == "crash"
        finally:
            await runtime.shutdown()
            await store.close()
    asyncio.run(scenario())


def test_mixed_intraday_and_regime_replay_is_version_ordered_and_preserves_quote_safety(tmp_path, monkeypatch):
    async def scenario():
        clock = [NOW]
        engine, exits, store, runtime, owner = await owned_c3(tmp_path, clock=lambda: clock[0])
        batch = install_intraday_owner(engine, exits, runtime)
        try:
            await failed_fill(engine, runtime, monkeypatch)
            crash = await refresh_intraday(engine, batch, monkeypatch, -3.0, clock)
            source = await owner.classify(
                "12:00 (장중 업데이트)", inputs_provider=StageProvider(now=clock[0]),
                llm=QuickLLM({"regime": "trending_bear", "confidence": 0.8}))
            application = owner.classifier_application_receipt(source.operation_id)
            await runtime.quote("005930", Decimal("10100"))
            clock[0] += timedelta(seconds=1)
            normal = await refresh_intraday(engine, batch, monkeypatch, 0.0, clock)
            events = runtime.owner.state["protection_replay"]["005930"]["events"]
            versions = [row["source_version"] for row in events]
            assert versions == sorted(versions)
            assert {"fill", "intraday_policy", "regime_application", "quote"} <= {row["kind"] for row in events}
            assert crash.committed_version < application.committed_version < normal.committed_version
            before = deepcopy(runtime.owner.state)
            repaired = await runtime.repair_protection("mixed-union-repair", "005930", expected_version=runtime.owner.version)
            assert repaired.status == "APPLIED", repaired.reason
            assert runtime.owner.state["portfolio"] == before["portfolio"]
            assert not engine._event_queue and not runtime.trading_ready
        finally:
            await runtime.shutdown()
            await store.close()
    asyncio.run(scenario())


def test_post_policy_historical_sell_quote_is_blocked_without_economic_reapplication(tmp_path, monkeypatch):
    async def scenario():
        clock = [NOW]
        engine, exits, store, runtime, owner = await owned_c3(tmp_path, clock=lambda: clock[0])
        batch = install_intraday_owner(engine, exits, runtime)
        try:
            await failed_fill(engine, runtime, monkeypatch)
            await refresh_intraday(engine, batch, monkeypatch, -3.0, clock)
            await owner.classify("12:00 (장중 업데이트)", inputs_provider=StageProvider(now=clock[0]),
                                 llm=QuickLLM({"regime": "trending_bear", "confidence": 0.8}))
            await runtime.quote("005930", Decimal("9600"))
            before = deepcopy(runtime.owner.state)
            result = await runtime.repair_protection("post-policy-sell-block", "005930",
                                                      expected_version=runtime.owner.version)
            assert (result.status, result.reason) == ("BLOCKED", "unrecorded_historical_decision")
            assert runtime.owner.state["portfolio"] == before["portfolio"]
            assert runtime.owner.state["protection"] == before["protection"]
            assert not engine._event_queue and not runtime.trading_ready
        finally:
            await runtime.shutdown()
            await store.close()
    asyncio.run(scenario())


def test_older_held_noon_observation_cannot_overwrite_a_later_applied_intraday_horizon(tmp_path, monkeypatch):
    async def scenario():
        clock = [NOW]
        engine, exits, store, runtime, owner = await owned_c3(tmp_path, clock=lambda: clock[0])
        batch = install_intraday_owner(engine, exits, runtime)
        entered, release = asyncio.Event(), asyncio.Event()
        provider = StageProvider(now=clock[0], entries={"index0001": entered},
                                 stage_release=release, held_stage="index0001")
        task = asyncio.create_task(owner.classify(
            "12:00 (장중 업데이트)", inputs_provider=provider,
            llm=QuickLLM({"regime": "neutral", "confidence": 0.6})))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            clock[0] += timedelta(seconds=1)
            await refresh_intraday(engine, batch, monkeypatch, -3.0, clock)
            clock[0] += timedelta(seconds=1)
            later = await refresh_intraday(engine, batch, monkeypatch, 0.0, clock)
            release.set()
            await asyncio.wait_for(task, 2)
            horizon = runtime.owner.state["regime_policy"]["horizon"]
            assert horizon["writer_kind"] == "intraday_5m"
            assert horizon["version"] == later.committed_version
            assert horizon["level"] == "normal"
            assert runtime.owner.state["intraday_policy"]["current"]["level"] == "normal"
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)
            await runtime.shutdown()
            await store.close()
    asyncio.run(scenario())


def test_equal_classified_at_horizon_tie_uses_later_durable_commit_order(tmp_path, monkeypatch):
    async def scenario():
        clock = [NOW]
        engine, exits, store, runtime, owner = await owned_c3(tmp_path, clock=lambda: clock[0])
        batch = install_intraday_owner(engine, exits, runtime)
        try:
            first = await refresh_intraday(engine, batch, monkeypatch, -3.0, clock)
            source = await owner.classify("12:00 (장중 업데이트)",
                                          inputs_provider=StageProvider(now=clock[0]), llm=QuickLLM())
            noon = runtime.owner.state["regime_policy"]["noon_caps"]
            assert next(iter(noon.values()))["candidate"]["classified_at"] == clock[0].isoformat()
            later = await refresh_intraday(engine, batch, monkeypatch, 0.0, clock)
            horizon = runtime.owner.state["regime_policy"]["horizon"]
            assert first.committed_version < source.committed_version < later.committed_version
            assert horizon == {"level": "normal", "change_pct": 0.0, "classified_at": clock[0].isoformat(),
                               "writer_kind": "intraday_5m", "operation_id": later.operation_id,
                               "version": later.committed_version}
        finally:
            await runtime.shutdown()
            await store.close()
    asyncio.run(scenario())


def test_day_fence_rejects_held_classifier_without_relabeling_missing_market_time(tmp_path):
    async def scenario():
        from test_execution_day_recovery import prepare, valued
        clock = [NOW]
        _, _, store, runtime, owner = await owned_c3(tmp_path, clock=lambda: clock[0])
        entered, release = asyncio.Event(), asyncio.Event()
        provider = StageProvider(now=clock[0])
        task = asyncio.create_task(owner.classify(
            "12:00 (장중 업데이트)", inputs_provider=provider,
            llm=QuickLLM(entered=entered, release=release)))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            fence = await prepare(runtime, clock)
            evidence_id = await valued(runtime, fence, clock)
            assert (await runtime.rollover_day("c3-roll", expected_version=runtime.owner.version,
                                               fence_id=fence.fence_id, valuation_evidence_id=evidence_id)).status == "APPLIED"
            assert (await runtime.resume_after_rollover("c3-resume", expected_version=runtime.owner.version,
                                                        fence_id=fence.fence_id)).status == "APPLIED"
            release.set()
            receipt = await asyncio.wait_for(task, 2)
            assert receipt.status == "stale"
            assert owner.classifier_application_receipt(receipt.operation_id) is None
            source = runtime.owner.state["risk_sources"]["records"][receipt.operation_id]
            assert source["terminal"]["receipt"]["status"] == "stale"
            assert runtime.owner.state["regime_policy"]["horizon_baseline"]["supplied"]["horizon"]["classified_at"] is None
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)
            await runtime.shutdown()
            await store.close()
    asyncio.run(scenario())


def test_classifier_application_sql_failure_does_not_create_partial_source_application_or_replay(tmp_path, monkeypatch):
    async def scenario():
        clock = [NOW]
        _, _, store, runtime, owner = await owned_c3(tmp_path, clock=lambda: clock[0])
        entered, release = asyncio.Event(), asyncio.Event()
        llm = QuickLLM(entered=entered, release=release)
        task = asyncio.create_task(owner.classify(
            "12:00 (장중 업데이트)", inputs_provider=StageProvider(now=clock[0]), llm=llm))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            source_id = runtime.owner.state["risk_sources"]["latest"]["llm_regime"]
            assert runtime.owner.state["risk_sources"]["records"][source_id]["terminal"] is None
            assert runtime.owner.state["regime_policy"]["applications"] == {}
            original = store.commit
            async def fail(expected, state, command):
                raise OSError("acceptance classifier/application sql fault")
            monkeypatch.setattr(store, "commit", fail)
            release.set()
            with pytest.raises(OSError, match="classifier/application sql fault"):
                await asyncio.wait_for(task, 2)
            assert not runtime.owner.healthy and runtime._command_results_failed
            assert runtime.owner.state["risk_sources"]["records"][source_id]["terminal"] is None
            assert runtime.owner.state["regime_policy"]["applications"] == {}
            assert not runtime.owner.state.get("protection_replay")
            monkeypatch.setattr(store, "commit", original)
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)
            await store.close()
    asyncio.run(scenario())


def test_projection_failure_does_not_retry_model_source_or_protection_application(tmp_path, monkeypatch):
    async def scenario():
        clock = [NOW]
        _, _, store, runtime, owner = await owned_c3(tmp_path, clock=lambda: clock[0])
        llm = QuickLLM()
        calls = []
        def fail_projection(payload):
            calls.append(payload)
            raise OSError("acceptance projection fault")
        monkeypatch.setattr(owner, "_replace_classifier_projection", fail_projection)
        try:
            source = await owner.classify("12:00 (장중 업데이트)",
                                          inputs_provider=StageProvider(now=clock[0]), llm=llm)
            state = deepcopy(runtime.owner.state)
            assert source.status == "accepted"
            assert owner.classifier_application_receipt(source.operation_id) is not None
            assert len(llm.calls) == 1 and len(calls) == 1
            assert await owner._write_classifier_projection(source.operation_id) is False
            assert len(llm.calls) == 1 and len(calls) == 2
            assert runtime.owner.state == state
        finally:
            await runtime.shutdown()
            await store.close()
    asyncio.run(scenario())


def test_reverse_classifier_projection_completion_cannot_overwrite_newer_operation(tmp_path, monkeypatch):
    async def scenario():
        import json
        clock = [NOW]
        _, _, store, runtime, owner = await owned_c3(tmp_path, clock=lambda: clock[0])
        entered, release = asyncio.Event(), asyncio.Event()
        original, calls = owner._write_classifier_projection, []
        async def hold_first(operation_id):
            calls.append(operation_id)
            if len(calls) == 1:
                entered.set()
                await release.wait()
            return await original(operation_id)
        monkeypatch.setattr(owner, "_write_classifier_projection", hold_first)
        first = asyncio.create_task(owner.classify(
            "12:00 (장중 업데이트)", inputs_provider=StageProvider(now=clock[0]),
            llm=QuickLLM({"regime": "neutral", "confidence": 0.6})))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            second = await owner.classify(
                "12:00 (장중 업데이트)", inputs_provider=StageProvider(now=clock[0]),
                llm=QuickLLM({"regime": "trending_bear", "confidence": 0.8}))
            release.set()
            first_receipt = await asyncio.wait_for(first, 2)
            projection = json.loads((Path.home() / ".cache" / "ai_trader" / "llm_regime_today.json").read_text())
            assert first_receipt.status == second.status == "accepted"
            assert calls == [first_receipt.operation_id, second.operation_id]
            assert projection["regime"] == "trending_bear"
            assert runtime.owner.state["risk_sources"]["latest"]["llm_regime"] == second.operation_id
        finally:
            release.set()
            await asyncio.gather(first, *(task for task in (locals().get("second"),) if hasattr(task, "__await__")),
                                 return_exceptions=True)
            await runtime.shutdown()
            await store.close()
    asyncio.run(scenario())


def test_cancelled_classifier_caller_drains_terminal_without_application_or_replay(tmp_path):
    async def scenario():
        clock = [NOW]
        _, _, store, runtime, owner = await owned_c3(tmp_path, clock=lambda: clock[0])
        entered, release = asyncio.Event(), asyncio.Event()
        task = asyncio.create_task(owner.classify(
            "12:00 (장중 업데이트)", inputs_provider=StageProvider(now=clock[0]),
            llm=QuickLLM(entered=entered, release=release)))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            operation = runtime.owner.state["risk_sources"]["latest"]["llm_regime"]
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            await runtime.shutdown()
            terminal = runtime.owner.state["risk_sources"]["records"][operation]["terminal"]
            assert terminal["receipt"]["status"] == "cancelled"
            assert owner.classifier_application_receipt(operation) is None
            assert not runtime.owner.state.get("protection_replay")
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)
            if not runtime._closing:
                await runtime.shutdown()
            await store.close()
    asyncio.run(scenario())


def test_cold_restore_rejects_checksum_tampered_regime_application_before_publication(tmp_path, monkeypatch):
    async def scenario():
        from src.core.engine import UnifiedEngine
        from src.core.types import TradingConfig
        from src.execution.safety.application import ApplicationBlocked
        from src.execution.safety.runtime import KRExecutionRuntime
        from src.execution.safety.store import ExecutionStateStore
        from src.strategies.exit_manager import ExitManager
        clock = [NOW]
        engine, exits, store, runtime, owner = await owned_c3(tmp_path, clock=lambda: clock[0])
        try:
            await failed_fill(engine, runtime, monkeypatch)
            source = await owner.classify(
                "12:00 (장중 업데이트)", inputs_provider=StageProvider(now=clock[0]),
                llm=QuickLLM({"regime": "trending_bear", "confidence": 0.8}))
            application = owner.classifier_application_receipt(source.operation_id)
            tampered = deepcopy(runtime.owner.state)
            tampered["regime_policy"]["applications"][application.operation_id]["calculation"]["force"] = True
            path = store.path
            await store.commit(runtime.owner.version, tampered, "acceptance-tampered-regime-application")
            await runtime.shutdown()
            await store.close()
            reopened = ExecutionStateStore(path)
            restored = KRExecutionRuntime(reopened, UnifiedEngine(TradingConfig(initial_capital=Decimal("2000000"))),
                                          ExitManager(persist=False, clock=lambda: clock[0]),
                                          clock=lambda: clock[0], account_scope="scope")
            try:
                with pytest.raises(ApplicationBlocked):
                    await restored.restore()
                assert not restored.owner.healthy
            finally:
                await reopened.close()
        finally:
            if not runtime._closing:
                await runtime.shutdown()
            if not store._closed:
                await store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("field", ("force", "rule_digest", "changed_symbol_scopes"))
def test_cold_restore_rejects_rehashed_semantic_classifier_application_and_replay(tmp_path, monkeypatch, field):
    """A self-consistent checksum chain cannot legitimize changed application meaning."""
    async def scenario():
        from src.core.engine import UnifiedEngine
        from src.core.types import TradingConfig
        from src.execution.safety.application import ApplicationBlocked
        from src.execution.safety.protection_recovery import digest
        from src.execution.safety.runtime import KRExecutionRuntime
        from src.execution.safety.store import ExecutionStateStore
        from src.strategies.exit_manager import ExitManager
        clock = [NOW]
        engine, _, store, runtime, owner = await owned_c3(tmp_path, clock=lambda: clock[0])
        try:
            await failed_fill(engine, runtime, monkeypatch)
            source = await owner.classify(
                "12:00 (장중 업데이트)", inputs_provider=StageProvider(now=clock[0]),
                llm=QuickLLM({"regime": "trending_bear", "confidence": 0.8}))
            application = owner.classifier_application_receipt(source.operation_id)
            original_anchor = deepcopy(runtime.owner.state["outbox"])
            tampered = deepcopy(runtime.owner.state)
            row = tampered["regime_policy"]["applications"][application.operation_id]
            calculation = row["calculation"]
            if field == "force":
                calculation["force"] = True
            elif field == "rule_digest":
                calculation["rule_digest"] = "rehashed-but-wrong-rule"
            else:
                calculation["changed_symbol_scopes"] = {}
            row["digest"] = digest({key: value for key, value in row.items() if key != "digest"})
            history = tampered["protection_replay"]["005930"]
            previous = ""
            for event in history["events"]:
                if event["kind"] == "regime_application" and event["application_id"] == application.operation_id:
                    event["application_digest"] = row["digest"]
                event["previous"] = previous
                event["digest"] = digest({key: value for key, value in event.items() if key != "digest"})
                previous = event["digest"]
            history["tail"] = previous
            # The original actual fill anchor is not part of the semantic alteration.
            assert tampered["outbox"] == original_anchor
            path = store.path
            await store.commit(runtime.owner.version, tampered, "acceptance-rehashed-regime-application-" + field)
            await runtime.shutdown()
            await store.close()
            reopened = ExecutionStateStore(path)
            restored = KRExecutionRuntime(reopened, UnifiedEngine(TradingConfig(initial_capital=Decimal("2000000"))),
                                          ExitManager(persist=False, clock=lambda: clock[0]),
                                          clock=lambda: clock[0], account_scope="scope")
            try:
                with pytest.raises(ApplicationBlocked):
                    await restored.restore()
                assert not restored.owner.healthy
            finally:
                await reopened.close()
        finally:
            if not runtime._closing:
                await runtime.shutdown()
            if not store._closed:
                await store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("change", ("declared_policy", "actual_fill"))
def test_classifier_capture_race_stales_only_a_declared_policy_read(tmp_path, change):
    async def scenario():
        clock = [NOW]
        engine, _, store, runtime, owner = await owned_c3(tmp_path, clock=lambda: clock[0])
        entered, model_release = asyncio.Event(), asyncio.Event()
        provider = StageProvider(now=clock[0])
        before = deepcopy(runtime.owner.state)
        task = asyncio.create_task(owner.classify(
            "12:00 (장중 업데이트)", inputs_provider=provider,
            llm=QuickLLM(entered=entered, release=model_release)))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            if change == "declared_policy":
                def mutate(state):
                    state["protection"]["config"]["stop_loss_pct"] = 4.1
                    return state
                await runtime.owner.mutate("c3-relevant-protection-change", mutate)
            else:
                ref = await opened(runtime, "c3-unrelated-fill")
                await queued(engine, await observed(runtime, ref, 40, "400000"))
            model_release.set()
            receipt = await asyncio.wait_for(task, 2)
            if change == "declared_policy":
                assert receipt.status == "stale"
                assert owner.classifier_application_receipt(receipt.operation_id) is None
                assert runtime.owner.state["regime_policy"]["applications"] == before["regime_policy"]["applications"]
                assert not runtime.owner.state.get("protection_replay")
            else:
                assert receipt.status == "accepted"
                assert owner.classifier_application_receipt(receipt.operation_id) is not None
        finally:
            model_release.set()
            await asyncio.gather(task, return_exceptions=True)
            await runtime.shutdown()
            await store.close()
    asyncio.run(scenario())


def test_detached_vix_lane_change_does_not_stale_classifier_that_did_not_capture_it(tmp_path):
    async def scenario():
        clock = [NOW]
        _, _, store, runtime, owner = await owned_c3(tmp_path, clock=lambda: clock[0])
        entered, release = asyncio.Event(), asyncio.Event()
        task = asyncio.create_task(owner.classify(
            "12:00 (장중 업데이트)", inputs_provider=StageProvider(now=clock[0]),
            llm=QuickLLM(entered=entered, release=release)))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            with runtime.command_scope() as token:
                ticket = await owner.sources.begin("acceptance-detached-vix", "vix_regime", scope_token=token)
                receipt = await owner.sources.complete(
                    ticket, "success", {"value": 21.0, "fetched_at": clock[0].isoformat()},
                    source="acceptance-vix", source_event_id="acceptance-detached-vix", received_at=clock[0],
                    scope_token=token)
            assert receipt.status == "accepted"
            release.set()
            classifier = await asyncio.wait_for(task, 2)
            assert classifier.status == "accepted"
            assert owner.classifier_application_receipt(classifier.operation_id) is not None
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)
            await runtime.shutdown()
            await store.close()
    asyncio.run(scenario())


def test_accepted_index_trend_and_expert_lanes_do_not_stale_held_classifier(tmp_path):
    """The classifier seal names neither independent trend lane nor its expert fact."""
    async def scenario():
        from types import SimpleNamespace
        clock = [NOW]
        _, _, store, runtime, owner = await owned_c3(tmp_path, clock=lambda: clock[0])
        entered, release = asyncio.Event(), asyncio.Event()
        task = asyncio.create_task(owner.classify(
            "12:00 (장중 업데이트)", inputs_provider=StageProvider(now=clock[0]),
            llm=QuickLLM(entered=entered, release=release)))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            async def fetch(code):
                return quote(code, pct=0.0)
            class Experts:
                def aggregate_regime_score(self):
                    return 0.0
                def bear_consensus(self, **kwargs):
                    assert kwargs == {"threshold_confidence": .7, "min_count": 2}
                    return False
            trend = await owner.refresh_trend(SimpleNamespace(fetch_index_price=fetch),
                                              expert_orchestrator=Experts())
            assert trend.status == "accepted"
            records = runtime.owner.state["risk_sources"]
            expert_id = records["latest"]["expert_regime"]
            assert records["records"][expert_id]["terminal"]["receipt"]["status"] == "accepted"
            release.set()
            classifier = await asyncio.wait_for(task, 2)
            assert classifier.status == "accepted"
            assert owner.classifier_application_receipt(classifier.operation_id) is not None
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)
            await runtime.shutdown()
            await store.close()
    asyncio.run(scenario())


def test_c3_degraded_queue_repair_is_idempotent_and_does_not_reapply_economics(tmp_path, monkeypatch):
    async def scenario():
        clock = [NOW]
        engine, exits, store, runtime, owner = await owned_c3(tmp_path, clock=lambda: clock[0])
        try:
            await failed_fill(engine, runtime, monkeypatch)
            source = await owner.classify("12:00 (장중 업데이트)", inputs_provider=StageProvider(now=clock[0]), llm=QuickLLM())
            application = owner.classifier_application_receipt(source.operation_id)
            before = snapshot_roots(runtime.owner.state)
            repair_version = runtime.owner.version
            repaired = await runtime.repair_protection("c3-actual-repair", "005930", expected_version=repair_version)
            assert repaired.status == "APPLIED", repaired.reason
            assert exits.get_state("005930").remaining_quantity == 40
            assert application is not None
            after = runtime.owner.state
            for root in ("portfolio", "risk", "lots", "intents", "attempts", "reservations", "inbox", "outbox",
                         "startup_reconciliation", "risk_sources", "regime_policy", "intraday_policy"):
                assert after.get(root) == before[root], root
            assert engine._event_queue == []
            assert (await runtime.repair_protection("c3-actual-repair", "005930", expected_version=repair_version)).status == "ALREADY_APPLIED"
            assert not runtime.trading_ready
        finally:
            await runtime.shutdown()
            await store.close()
    asyncio.run(scenario())


def test_cold_c3_reopen_repairs_existing_application_without_reregistering_baselines(tmp_path, monkeypatch):
    async def scenario():
        clock = [NOW]
        engine, exits, store, runtime, owner = await owned_c3(tmp_path, clock=lambda: clock[0])
        try:
            await failed_fill(engine, runtime, monkeypatch)
            source = await owner.classify(
                "12:00 (장중 업데이트)", inputs_provider=StageProvider(now=clock[0]),
                llm=QuickLLM({"regime": "trending_bear", "confidence": 0.8}))
            before = snapshot_roots(runtime.owner.state)
            engine, exits, store, runtime, restored_owner = await reopen_c3(
                engine, exits, store, runtime, clock=lambda: clock[0])
            repaired = await runtime.repair_protection("cold-c3-repair", "005930", expected_version=runtime.owner.version)
            assert repaired.status == "APPLIED", repaired.reason
            assert exits.get_state("005930").remaining_quantity == 40
            assert restored_owner.classifier_application_receipt(source.operation_id) is not None
            for key in ("portfolio", "risk", "lots", "intents", "attempts", "reservations", "inbox", "outbox",
                        "startup_reconciliation", "risk_sources", "regime_policy", "intraday_policy"):
                assert runtime.owner.state.get(key) == before[key], key
        finally:
            if not runtime._closing:
                await runtime.shutdown()
            await store.close()
    asyncio.run(scenario())
