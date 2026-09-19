"""Permanent C2a day/account/fill-fault acceptance; original review probe is preserved."""
import conftest as isolation

import asyncio
from copy import deepcopy
from decimal import Decimal
import sqlite3
import threading

import pytest

from src.core.engine import UnifiedEngine
from src.core.types import TradingConfig
from src.execution.safety.application import ApplicationBlocked
from src.execution.safety.risk_sources import RiskSourceCoordinator
from src.execution.safety.runtime import KRExecutionRuntime
from src.execution.safety.store import ExecutionStateStore
from src.strategies.exit_manager import ExitManager
from test_execution_day_recovery import day_setup, prepare, valued
from test_execution_runtime import NOW, opened, observed, queued, setup, wait_queued



@pytest.fixture(autouse=True)
def isolation_guard():
    assert isolation.VIOLATIONS == []
    yield
    assert isolation.VIOLATIONS == []


async def cold_restore(store, runtime, *, account_scope, clock):
    await runtime.shutdown()
    path = store.path
    await store.close()
    engine = UnifiedEngine(TradingConfig(initial_capital=Decimal("2000000")))
    exits = ExitManager(persist=False, clock=clock)
    store = ExecutionStateStore(path)
    runtime = KRExecutionRuntime(store, engine, exits, account_scope=account_scope, clock=clock)
    await runtime.restore()
    runtime.attach()
    return engine, store, runtime


def policy_economics(state):
    return {key: deepcopy(state[key]) for key in ("portfolio", "lots", "risk", "outbox", "attempts",
                                                   "policy_generations")}


def test_registered_generation_survives_actual_day_fence_roll_and_cold_restore(tmp_path):
    """A registry must not be rewritten by the real prepare/value/roll/resume commits."""
    async def scenario():
        engine, _, store, runtime, times = await day_setup(tmp_path)
        try:
            registered = await runtime.owner.register_policy_generations("register-day", ("protection.config",))
            original = deepcopy(runtime.owner.state["policy_generations"])
            fence = await prepare(runtime, times)
            evidence_id = await valued(runtime, fence, times)
            assert (await runtime.rollover_day("roll", expected_version=runtime.owner.version,
                fence_id=fence.fence_id, valuation_evidence_id=evidence_id)).status == "APPLIED"
            assert (await runtime.resume_after_rollover("resume", expected_version=runtime.owner.version,
                fence_id=fence.fence_id)).status == "APPLIED"
            assert runtime.owner.state["policy_generations"] == original
            engine, store, runtime = await cold_restore(store, runtime, account_scope="scope",
                                                        clock=lambda: times[0])
            assert runtime.owner.state["policy_generations"] == original
            source = RiskSourceCoordinator(runtime)
            ticket = await source.begin("post-roll", "llm_regime", require_seal=True)
            seal = await source.seal(ticket, versioned_policy_reads=("protection.config",), inputs={})
            assert '"generation":' + str(registered) in seal.reads_json
            assert (await source.complete(ticket, "success", {})).status == "accepted"
            assert runtime.owner.state["policy_generations"] == original
        finally:
            await engine._shutdown()
            await store.close()
    asyncio.run(scenario())


def test_registered_generation_account_mismatch_cold_restore_only_stales_old_ticket(tmp_path):
    """An old scope cannot complete accepted work or mutate the retained registry after restore."""
    async def scenario():
        engine, _, store, runtime = await setup(tmp_path, account_scope="scope")
        try:
            await runtime.owner.register_policy_generations("register-scope", ("protection.config",))
            source = RiskSourceCoordinator(runtime)
            ticket = await source.begin("scope-old", "llm_regime", require_seal=True)
            await source.seal(ticket, versioned_policy_reads=("protection.config",), inputs={})
            registry = deepcopy(runtime.owner.state["policy_generations"])
            engine, store, runtime = await cold_restore(store, runtime, account_scope="other",
                                                        clock=lambda: NOW)
            source = RiskSourceCoordinator(runtime)
            result = await source.complete(ticket, "success", {})
            assert (result.status, result.reason) == ("stale", "day_or_generation")
            assert runtime.owner.state["policy_generations"] == registry
            with pytest.raises(ApplicationBlocked, match="risk_source_scope_conflict"):
                await source.begin("scope-other", "llm_regime", require_seal=True)
        finally:
            await engine._shutdown()
            await store.close()
    asyncio.run(scenario())


def test_actual_fill_precommit_abort_preserves_economics_and_registered_history(tmp_path):
    """A SQLite abort before the fill commit may retain reception, never economics or policy history."""
    async def scenario():
        engine, _, store, runtime = await setup(tmp_path, account_scope="scope")
        try:
            await runtime.owner.register_policy_generations("register-abort", ("protection.config",))
            ref = await opened(runtime, "B1")
            observation = await observed(runtime, ref, 100, "1000000")
            before = policy_economics(runtime.owner.state)
            with sqlite3.connect(store.path) as db:
                db.execute("""CREATE TRIGGER abort_fill BEFORE UPDATE ON checkpoint
                    WHEN json_extract(NEW.state, '$.cursors') IS NOT NULL
                    BEGIN SELECT RAISE(ABORT, 'probe fill precommit abort'); END""")
            with pytest.raises(ApplicationBlocked, match="체결 적용 실패"):
                await queued(engine, observation)
            durable = (await store.load())[1]
            assert policy_economics(durable) == before
            assert durable["inbox"][observation.observation_id]["status"] == "RECEIVED"
            assert durable["attempts"]["B1"]["applied_quantity"] == 0
            with sqlite3.connect(store.path) as db:
                db.execute("DROP TRIGGER abort_fill")
            await runtime.restore()
            assert (await queued(engine, observation)).status == "APPLIED"
            assert runtime.owner.state["policy_generations"] == before["policy_generations"]
        finally:
            await engine._shutdown()
            await store.close()
    asyncio.run(scenario())


def test_cancelled_actual_fill_waiters_commit_once_and_keep_registered_history(tmp_path, monkeypatch):
    """Cancelling queue/process waiters cannot make a committed fill provisional or repeat it."""
    async def scenario():
        engine, _, store, runtime = await setup(tmp_path, account_scope="scope")
        entered, release = threading.Event(), threading.Event()
        try:
            await runtime.owner.register_policy_generations("register-cancel", ("protection.config",))
            registry = deepcopy(runtime.owner.state["policy_generations"])
            ref = await opened(runtime, "B1")
            observation = await observed(runtime, ref, 100, "1000000")
            original = store._commit
            def gate(version, payload, commit_id):
                if commit_id.startswith("fill:"):
                    entered.set()
                    assert release.wait(5)
                return original(version, payload, commit_id)
            with monkeypatch.context() as patch:
                patch.setattr(store, "_commit", gate)
                caller = asyncio.create_task(engine.apply_execution_observation(observation))
                await wait_queued(engine, caller)
                processor = asyncio.create_task(engine._process_event(await engine._get_next_event()))
                assert await asyncio.to_thread(entered.wait, 5)
                caller.cancel()
                processor.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await caller
                with pytest.raises(asyncio.CancelledError):
                    await processor
                release.set()
                await asyncio.gather(*tuple(engine._execution_apply_tasks))
            durable = (await store.load())[1]
            assert durable["cursors"][observation.order_key]["quantity"] == 100
            assert durable["portfolio"]["cash"] == "999859"
            assert durable["policy_generations"] == registry
            await runtime.restore()
            assert (await queued(engine, observation)).status == "ALREADY_APPLIED"
            assert runtime.owner.state["policy_generations"] == registry
            assert runtime.engine.portfolio.cash == Decimal("999859")
        finally:
            release.set()
            await engine._shutdown()
            await store.close()
    asyncio.run(scenario())
