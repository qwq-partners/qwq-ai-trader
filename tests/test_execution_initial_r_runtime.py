"""실제 core 큐·SQLite의 초기 R 훅. 합성 finality는 KIS 계약 증명이 아니다."""
import asyncio
from copy import deepcopy
from decimal import Decimal

import pytest

from src.execution.safety.application import ApplicationBlocked
from src.execution.safety.journal_delivery import OutboxDispatcher, PostgresExecutionJournal
from src.execution.safety.runtime import KRExecutionRuntime
from src.execution.safety.store import ExecutionStateStore
from src.strategies.exit_manager import ExitManager
from test_execution_journal_delivery import Pool
from test_execution_runtime import NOW, setup, opened, observed, queued


async def ready(tmp_path):
    engine, exits, store, runtime = await setup(tmp_path)
    ref = await opened(runtime, "B1")
    observation = await observed(runtime, ref, 100, "1000000",
                                 metadata={"registration_params": {"stop_loss_pct": 5}})
    assert (await queued(engine, observation)).status == "APPLIED"
    state = runtime.owner.state
    arguments = {"expected_version": runtime.owner.version,
                 "initial_stop_evidence_id": state["initial_stop_evidence"][ref.key]["evidence_id"],
                 "finality_evidence_id": state["attempts"]["B1"]["accepted_finality_evidence_id"]}
    return engine, exits, store, runtime, ref, arguments


@pytest.mark.parametrize("fault", ["precommit", "postcommit", "publication"])
def test_initial_r_failed_commit_or_publish_reopens_without_reapplying_economics(tmp_path, monkeypatch, fault):
    async def scenario():
        engine, exits, store, runtime, ref, arguments = await ready(tmp_path)
        path, economic = store.path, deepcopy(runtime.owner.state["portfolio"])
        original = store.commit
        async def fail(version, state, command):
            if command.startswith("command:initial-r:"):
                if fault == "postcommit":
                    await original(version, state, command)
                raise OSError("synthetic R commit failure")
            return await original(version, state, command)
        try:
            with monkeypatch.context() as patch:
                if fault == "publication":
                    def bad_publish(*args):
                        raise ValueError("synthetic R publication failure")
                    patch.setattr(runtime.owner, "publisher", bad_publish)
                else:
                    patch.setattr(store, "commit", fail)
                with pytest.raises(ApplicationBlocked if fault == "publication" else OSError):
                    await runtime.finalize_initial_r("initial-r-test", ref.key, **arguments)
            assert not runtime.owner.healthy
            await runtime.shutdown()
            await store.close()
            store = ExecutionStateStore(path)
            runtime = KRExecutionRuntime(store, engine, exits, clock=lambda: NOW)
            await runtime.restore()
            receipt = await runtime.finalize_initial_r("initial-r-test", ref.key, **arguments)
            assert receipt.status == "APPLIED"
            assert runtime.owner.state["portfolio"] == economic
            assert Decimal(runtime.owner.state["lots"][ref.key]["initial_r"]) == Decimal("50000")
            assert sum(row.get("kind") == "initial_r_finalized" for row in runtime.owner.state["outbox"].values()) == 1
            assert not runtime.trading_ready
        finally:
            await runtime.shutdown()
            await store.close()
    asyncio.run(scenario())


def test_cancelled_initial_r_waiter_is_drained_and_same_request_replays(tmp_path, monkeypatch):
    async def scenario():
        _, _, store, runtime, ref, arguments = await ready(tmp_path)
        entered, release = asyncio.Event(), asyncio.Event()
        original = store.commit
        async def gate(version, state, command):
            if command.startswith("command:initial-r:"):
                entered.set()
                await release.wait()
            return await original(version, state, command)
        monkeypatch.setattr(store, "commit", gate)
        try:
            caller = asyncio.create_task(runtime.finalize_initial_r("r-cancel", ref.key, **arguments))
            await asyncio.wait_for(entered.wait(), 2)
            caller.cancel()
            with pytest.raises(asyncio.CancelledError):
                await caller
            assert runtime.health()["protection_updates_pending"] == 1
            release.set()
            await asyncio.wait_for(asyncio.gather(
                *(asyncio.shield(task) for task in tuple(runtime._protection_tasks))), 2)
            assert runtime.owner.state["lots"][ref.key]["initial_r_status"] == "confirmed"
            assert (await runtime.finalize_initial_r("r-cancel", ref.key, **arguments)).status == "APPLIED"
        finally:
            release.set()
            await runtime.shutdown()
            await store.close()
    asyncio.run(scenario())


def test_initial_r_rechecks_day_fence_after_waiting_for_owner(tmp_path):
    async def scenario():
        _, _, store, runtime, ref, arguments = await ready(tmp_path)
        try:
            async with runtime.owner._lock:
                caller = asyncio.create_task(runtime.finalize_initial_r("r-fence", ref.key, **arguments))
                await asyncio.sleep(0)
                await asyncio.sleep(0)
                runtime._day_closed = True
            with pytest.raises(ApplicationBlocked):
                await caller
            assert runtime.owner.state["lots"][ref.key]["initial_r_status"] == "pending"
            assert "r-fence" not in runtime.owner.state.get("recovery_receipts", {})
        finally:
            await runtime.shutdown()
            await store.close()
    asyncio.run(scenario())


def test_registered_failure_repair_never_invents_first_stop_or_r(tmp_path, monkeypatch):
    async def scenario():
        engine, _, store, runtime = await setup(tmp_path)
        try:
            ref = await opened(runtime, "B1")
            observation = await observed(runtime, ref, 100, "1000000")
            with monkeypatch.context() as patch:
                def fail(*args, **kwargs):
                    raise ValueError("synthetic registration failure")
                patch.setattr(ExitManager, "register_position", fail)
                assert (await queued(engine, observation)).protection_status == "degraded"
            assert ref.key not in runtime.owner.state.get("initial_stop_evidence", {})
            assert (await runtime.repair_protection("repair", "005930", expected_version=runtime.owner.version)).status == "APPLIED"
            receipt = await runtime.finalize_initial_r(
                "missing-stop", ref.key, expected_version=runtime.owner.version,
                initial_stop_evidence_id="0" * 64,
                finality_evidence_id=runtime.owner.state["attempts"]["B1"]["accepted_finality_evidence_id"])
            assert receipt.status == "BLOCKED"
            assert runtime.owner.state["lots"][ref.key]["initial_r"] is None
        finally:
            await runtime.shutdown()
            await store.close()
    asyncio.run(scenario())


def test_initial_r_ack_during_addon_repair_preserves_protection_evidence(tmp_path, monkeypatch):
    async def scenario():
        engine, _, store, runtime, ref, arguments = await ready(tmp_path)
        try:
            assert (await runtime.finalize_initial_r("r", ref.key, **arguments)).status == "APPLIED"
            addon = await opened(runtime, "B2", quantity=10)
            observation = await observed(runtime, addon, 10, "100000", total=10)
            with monkeypatch.context() as patch:
                def fail(*args, **kwargs):
                    raise ValueError("synthetic add-on parameter interpretation failure")
                patch.setattr("src.execution.safety.protection._registration", fail)
                assert (await queued(engine, observation)).protection_status == "degraded"
            history = deepcopy(runtime.owner.state["protection_replay"])
            dispatcher = OutboxDispatcher(runtime.owner, PostgresExecutionJournal(Pool()))
            assert (await dispatcher.drain()).delivered == 3
            assert not runtime.owner.state["lots"][ref.key]["initial_r_journal_pending"]
            assert runtime.owner.state["protection_replay"] == history
            assert (await runtime.repair_protection("addon-repair", "005930", expected_version=runtime.owner.version)).status == "APPLIED"
        finally:
            await runtime.shutdown()
            await store.close()
    asyncio.run(scenario())
