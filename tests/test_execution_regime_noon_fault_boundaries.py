"""Independent C3 fault boundaries: real SQLite source/application commits only."""
import asyncio
from copy import deepcopy
from decimal import Decimal
from pathlib import Path

import pytest

from src.core.engine import UnifiedEngine
from src.core.market_regime import MarketRegimeAdapter
from src.core.types import RiskConfig, TradingConfig
from src.execution.safety.application import ApplicationBlocked
from src.execution.safety.regime_owner import RegimeOwner
from src.execution.safety.protection_recovery import digest
from src.execution.safety.runtime import KRExecutionRuntime
from src.execution.safety.store import ExecutionStateStore
from src.risk.manager import RiskManager
from src.strategies.exit_manager import ExitManager
from test_execution_regime_noon_replay_acceptance import NOW, QuickLLM, StageProvider, failed_fill, owned_c3


@pytest.fixture(autouse=True)
def synthetic_home(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)


async def cold_reopen(engine, exits, store, runtime, *, clock):
    """Construct a fresh runtime; never recreate C3 baseline/source/application rows."""
    path = store.path
    try:
        await runtime.shutdown()
    except ApplicationBlocked:
        pass
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
    fresh_owner = RegimeOwner(fresh_runtime, adapter=fresh_engine._regime_adapter, sidecar=sidecar)
    return fresh_engine, fresh_exits, fresh_store, fresh_runtime, fresh_owner


@pytest.mark.parametrize("fault", ("publication", "committed_response_loss"))
def test_committed_classifier_application_fault_recovers_once_without_live_partial_economics(tmp_path, monkeypatch, fault):
    async def scenario():
        clock = [NOW]
        engine, exits, store, runtime, owner = await owned_c3(tmp_path, clock=lambda: clock[0])
        fresh_store = fresh_runtime = None
        task = None
        release = asyncio.Event()
        try:
            await failed_fill(engine, runtime, monkeypatch)
            live_portfolio = deepcopy(engine.portfolio)
            entered = asyncio.Event()
            task = asyncio.create_task(owner.classify(
                "12:00 (장중 업데이트)", inputs_provider=StageProvider(now=clock[0]),
                llm=QuickLLM(entered=entered, release=release)))
            await asyncio.wait_for(entered.wait(), 2)
            published_before = runtime.owner.published_version
            original_commit, original_publish = store.commit, runtime.owner.publisher
            if fault == "publication":
                def fail_publish(state, version):
                    if state.get("regime_policy", {}).get("applications"):
                        raise ValueError("independent C3 publication fault")
                    return original_publish(state, version)
                monkeypatch.setattr(runtime.owner, "publisher", fail_publish)
            else:
                async def lose_committed_response(expected, state, command):
                    version = await original_commit(expected, state, command)
                    if (command.startswith("command:risk-complete:")
                            and state.get("regime_policy", {}).get("applications")):
                        raise OSError("independent C3 committed response loss")
                    return version
                monkeypatch.setattr(store, "commit", lose_committed_response)
            release.set()
            with pytest.raises(ApplicationBlocked if fault == "publication" else OSError):
                await task
            durable_version, durable = await store.load()
            source_id = durable["risk_sources"]["latest"]["llm_regime"]
            application_id = "classifier:" + source_id
            assert durable["risk_sources"]["records"][source_id]["terminal"]["receipt"]["status"] == "accepted"
            assert durable["regime_policy"]["applications"][application_id]["receipt"]["classifier_operation_id"] == source_id
            assert durable["portfolio"] == runtime.owner.state["portfolio"]
            assert engine.portfolio == live_portfolio
            assert runtime.owner.published_version == published_before
            assert not runtime.owner.healthy and runtime.owner.publication_recovery_required
            fresh_engine, _, fresh_store, fresh_runtime, fresh_owner = await cold_reopen(
                engine, exits, store, runtime, clock=lambda: clock[0])
            runtime = store = None
            assert fresh_runtime.owner.state == durable
            assert fresh_runtime.owner.version == fresh_runtime.owner.published_version == durable_version
            after_first = deepcopy(fresh_runtime.owner.state)
            assert fresh_engine.portfolio == live_portfolio
            await fresh_runtime.restore()
            assert fresh_runtime.owner.state == after_first
            assert fresh_owner.classifier_application_receipt(source_id).operation_id == application_id
        finally:
            release.set()
            await asyncio.gather(*(value for value in (task,) if value), return_exceptions=True)
            if runtime is not None and not runtime._closing:
                try:
                    await runtime.shutdown()
                except ApplicationBlocked:
                    pass
            if store is not None and not store._closed:
                await store.close()
            if fresh_runtime is not None and not fresh_runtime._closing:
                await fresh_runtime.shutdown()
            if fresh_store is not None and not fresh_store._closed:
                await fresh_store.close()
    asyncio.run(scenario())


def test_admitted_source_completion_caller_cancel_drains_accepted_terminal(tmp_path, monkeypatch):
    async def scenario():
        clock = [NOW]
        _, _, store, runtime, owner = await owned_c3(tmp_path, clock=lambda: clock[0])
        entered, release = asyncio.Event(), asyncio.Event()
        task = None
        try:
            ticket = await owner.sources.begin("c3-source-completion-cancel", "vix_regime")
            original = store.commit
            async def held_commit(expected, state, command):
                if command.startswith("command:risk-complete:"):
                    entered.set()
                    await release.wait()
                return await original(expected, state, command)
            monkeypatch.setattr(store, "commit", held_commit)
            task = asyncio.create_task(owner.sources.complete(
                ticket, "success", {"value": 21.0, "fetched_at": clock[0].isoformat()},
                source="acceptance-vix", source_event_id="c3-source-completion-cancel", received_at=clock[0]))
            await asyncio.wait_for(entered.wait(), 2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            closing = asyncio.create_task(runtime.shutdown())
            await asyncio.sleep(0)
            assert not closing.done()
            release.set()
            await asyncio.wait_for(closing, 2)
            row = runtime.owner.state["risk_sources"]["records"][ticket.operation_id]
            assert row["terminal"]["receipt"]["status"] == "accepted"
            assert not runtime._command_scopes and not runtime._risk_source_pending
            assert runtime.owner.healthy and not runtime._command_results_failed
        finally:
            release.set()
            await asyncio.gather(*(value for value in (task, locals().get("closing")) if value),
                                 return_exceptions=True)
            if not runtime._closing:
                try:
                    await runtime.shutdown()
                except ApplicationBlocked:
                    pass
            await store.close()
    asyncio.run(scenario())


def test_cancelled_classifier_caller_drains_held_application_completion_exactly_once(tmp_path, monkeypatch):
    async def scenario():
        clock = [NOW]
        engine, _, store, runtime, owner = await owned_c3(tmp_path, clock=lambda: clock[0])
        entered, release = asyncio.Event(), asyncio.Event()
        task = closing = None
        try:
            await failed_fill(engine, runtime, monkeypatch)
            original = store.commit
            async def hold_application_commit(expected, state, command):
                if (command.startswith("command:risk-complete:")
                        and state.get("regime_policy", {}).get("applications")):
                    entered.set()
                    await release.wait()
                return await original(expected, state, command)
            monkeypatch.setattr(store, "commit", hold_application_commit)
            task = asyncio.create_task(owner.classify(
                "12:00 (장중 업데이트)", inputs_provider=StageProvider(now=clock[0]), llm=QuickLLM()))
            await asyncio.wait_for(entered.wait(), 2)
            source_id = runtime.owner.state["risk_sources"]["latest"]["llm_regime"]
            assert runtime.owner.state["risk_sources"]["records"][source_id]["terminal"] is None
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            closing = asyncio.create_task(runtime.shutdown())
            await asyncio.sleep(0)
            assert not closing.done()
            release.set()
            await asyncio.wait_for(closing, 2)
            state = runtime.owner.state
            assert state["risk_sources"]["records"][source_id]["terminal"]["receipt"]["status"] == "accepted"
            application_id = "classifier:" + source_id
            assert set(state["regime_policy"]["applications"]) == {application_id}
            events = state.get("protection_replay", {}).get("005930", {}).get("events", [])
            assert [event["application_id"] for event in events if event["kind"] == "regime_application"] == [application_id]
            assert not runtime._command_scopes and not runtime._risk_source_pending
            assert runtime.owner.healthy and not runtime._command_results_failed
        finally:
            release.set()
            await asyncio.gather(*(value for value in (task, closing) if value), return_exceptions=True)
            if not runtime._closing:
                try:
                    await runtime.shutdown()
                except ApplicationBlocked:
                    pass
            await store.close()
    asyncio.run(scenario())


def test_cold_restore_blocks_rehashed_nested_original_protection_dto_tamper(tmp_path, monkeypatch):
    async def scenario():
        clock = [NOW]
        engine, _, store, runtime, owner = await owned_c3(tmp_path, clock=lambda: clock[0])
        reopened = restored = None
        try:
            await failed_fill(engine, runtime, monkeypatch)
            source = await owner.classify("12:00 (장중 업데이트)", inputs_provider=StageProvider(now=clock[0]), llm=QuickLLM())
            application = owner.classifier_application_receipt(source.operation_id)
            original_anchor = deepcopy(runtime.owner.state["outbox"])
            tampered = deepcopy(runtime.owner.state)
            row = tampered["regime_policy"]["applications"][application.operation_id]
            dto = row["calculation"]["original_protection_before"]
            dto["config"]["stop_loss_pct"] = 9.25
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
            assert tampered["outbox"] == original_anchor
            path = store.path
            await store.commit(runtime.owner.version, tampered, "c3-full-dto-rehashed-tamper")
            await runtime.shutdown()
            await store.close()
            runtime = store = None
            reopened = ExecutionStateStore(path)
            sidecar = RiskManager(RiskConfig(), Decimal("2000000"))
            restored = KRExecutionRuntime(reopened, UnifiedEngine(TradingConfig(initial_capital=Decimal("2000000"))),
                                          ExitManager(persist=False, clock=lambda: clock[0]), clock=lambda: clock[0],
                                          risk_manager=sidecar, account_scope="scope")
            with pytest.raises(ApplicationBlocked):
                await restored.restore()
            assert not restored.owner.healthy
        finally:
            if runtime is not None and not runtime._closing:
                await runtime.shutdown()
            if store is not None and not store._closed:
                await store.close()
            if restored is not None and not restored._closing:
                await restored.shutdown()
            if reopened is not None and not reopened._closed:
                await reopened.close()
    asyncio.run(scenario())
