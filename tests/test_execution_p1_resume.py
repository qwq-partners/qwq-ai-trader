"""재개는 원 접수의 보호만 완료하며 진입 권한을 재생하지 않는다."""
import asyncio
from copy import deepcopy
from datetime import timedelta
from decimal import Decimal

import pytest

from src.execution.safety.application import ApplicationBlocked
from src.execution.safety.runtime import KRExecutionRuntime
from src.execution.safety.protection_recovery import digest
from test_execution_p1_pending import holding, frozen_product_clock
from test_execution_runtime import NOW
from test_execution_market_source import market_event


async def fail_quote(runtime, store, monkeypatch, *, event=None, symbol="005930", price="9000"):
    original = store.commit
    async def fail(expected, state, command):
        if command.startswith("command:quote:"):
            raise OSError("합성 보호 저장 실패")
        return await original(expected, state, command)
    with monkeypatch.context() as patch:
        patch.setattr(store, "commit", fail)
        with pytest.raises(OSError):
            if event is None:
                await runtime.quote(symbol, Decimal(price), intent_id="stop-" + symbol)
            else:
                await runtime.observe_market(event, intent_id="stop-" + symbol)
    await runtime.restore()
    return next(key for key, row in runtime.owner.state["protection_quote_admissions"].items() if row["symbol"] == symbol)


@pytest.mark.parametrize("disposition", ["resumed", "stale", "abandoned"])
def test_resume_invalidates_previous_ws_source_and_preserves_original_view(tmp_path, monkeypatch, disposition):
    async def scenario():
        _, _, store, runtime = await holding(tmp_path)
        try:
            event = await market_event(monkeypatch, NOW)
            await runtime.observe_market(event)
            newer = await market_event(monkeypatch, NOW + timedelta(seconds=1), price="9000")
            runtime.clock = lambda: NOW + timedelta(seconds=1)
            command = await fail_quote(runtime, store, monkeypatch, event=newer)
            before = runtime.owner.state
            if disposition == "stale":
                runtime.clock = lambda: NOW + timedelta(seconds=62)
            elif disposition == "abandoned":
                from src.strategies.exit_manager import ExitManager
                calls = []
                def broken(manager, symbol, *args, **kwargs):
                    calls.append(symbol)
                    manager._states[symbol].highest_price = Decimal("999999")
                    raise ValueError("합성 순수 보호 계산 실패")
                monkeypatch.setattr(ExitManager, "update_price", broken)
            runtime._day_closed = True
            result = await runtime.resume_protection_admission()
            expected = {"resumed": "protection_admission_resumed", "stale": "stale_admission_discarded",
                        "abandoned": "protection_admission_abandoned"}[disposition]
            assert len(result) == 1 and result[0][:2] == ("005930", expected)
            state = runtime.owner.state
            assert command not in state["protection_quote_admissions"]
            assert state["market_sources"]["005930"]["invalidated_at_version"] == runtime.owner.version
            assert state["market_sources"]["005930"]["admission_id"] == before["market_sources"]["005930"]["admission_id"]
            assert state["quote_price_views"] == before["quote_price_views"]
            assert state["latest_explicit_quote"] == before["latest_explicit_quote"]
            assert runtime.health()["protection_updates_failed"] is (disposition != "resumed")
            if disposition == "resumed":
                assert state["outbox"][command]["provenance"]["received_at"] == (NOW + timedelta(seconds=1)).isoformat()
            elif disposition == "abandoned":
                assert state["protection"]["degraded"]["005930"]["quantity"] == 100
                expected_dto = deepcopy(before["protection"])
                expected_dto["degraded"]["005930"] = {"quantity": 100, "reason": "protection_calculation_failed"}
                assert state["protection"] == expected_dto
                assert calls == ["005930"]
                runtime._day_closed = False
                receipt = await runtime.repair_protection("repair", "005930", expected_version=runtime.owner.version)
                assert receipt.status == "BLOCKED"
            await runtime.restore()
            assert await runtime.resume_protection_admission() == []
            if disposition == "abandoned":
                assert calls == ["005930"]
        finally:
            await runtime.shutdown()
            await store.close()
    asyncio.run(scenario())


def test_rest_staleness_does_not_create_source_or_clear_failure(tmp_path, monkeypatch):
    async def scenario():
        _, _, store, runtime = await holding(tmp_path)
        try:
            await fail_quote(runtime, store, monkeypatch)
            runtime.clock = lambda: NOW + timedelta(seconds=61)
            result = await runtime.resume_protection_admission()
            assert result == [("005930", "stale_admission_discarded", None)]
            assert "market_sources" not in runtime.owner.state
            assert runtime.health()["protection_updates_failed"]
        finally:
            await runtime.shutdown()
            await store.close()
    asyncio.run(scenario())


def test_health_separates_decision_audit_rows(tmp_path):
    async def scenario():
        _, _, store, runtime = await holding(tmp_path)
        try:
            def mixed(state):
                state["outbox"] = {"quote": {"kind": "protection_decision", "status": "pending"},
                    "intraday": {"kind": "protection_decision", "status": "pending", "effect_source": "intraday_preemptive"},
                    "unknown": {"kind": "protection_decision", "status": "pending", "effect_source": None},
                    "delivered": {"kind": "protection_decision", "status": "delivered"},
                    "fill": {"kind": "fill", "status": "pending"}, "malformed": None}
                return state
            # 비정형 행은 정상 게시 대상이 아니다. 장애 상태의 health만 검사한다.
            mixed(runtime.owner._state)
            health = runtime.health()
            assert health["outbox_pending"] == 2
            assert health["protection_decisions_pending"] == 1
            assert health["preemptive_decisions_pending"] == 1
            assert health["unclassified_protection_decisions_pending"] == 1
        finally:
            await runtime.shutdown()
            await store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("other", ["quote", "repair", "unattributed"])
def test_success_clears_only_its_quote_generation(tmp_path, monkeypatch, other):
    async def scenario():
        _, _, store, runtime = await holding(tmp_path)
        try:
            first = await fail_quote(runtime, store, monkeypatch)
            if other == "quote":
                second = await fail_quote(runtime, store, monkeypatch, symbol="000660")
                commit = store.commit
                async def fail_second(expected, state, command):
                    if command == "command:" + second:
                        raise OSError("다른 종목은 계속 저장 실패")
                    return await commit(expected, state, command)
                monkeypatch.setattr(store, "commit", fail_second)
            elif other == "repair":
                commit = store.commit
                async def fail_repair(expected, state, command):
                    if command.startswith("command:protection-repair:"):
                        raise OSError("합성 복구 저장 실패")
                    return await commit(expected, state, command)
                with monkeypatch.context() as patch:
                    patch.setattr(store, "commit", fail_repair)
                    with pytest.raises(OSError):
                        await runtime.repair_protection("repair-fails", "005930", expected_version=runtime.owner.version)
                await runtime.restore()
            else:
                runtime._protection_failed = True
            if other == "quote":
                result = await runtime.resume_protection_admission()
                assert result[0][0:2] == ("005930", "protection_admission_resumed")
                assert result[0][2] is not None
                assert second in runtime.owner.state["protection_quote_admissions"]
                first_outbox = deepcopy(runtime.owner.state["outbox"][first])
                with pytest.raises(OSError):
                    await runtime.resume_protection_admission()
                await runtime.restore()
                assert runtime.owner.state["outbox"][first] == first_outbox
            else:
                await runtime.resume_protection_admission()
            assert first not in runtime.owner.state["protection_quote_admissions"]
            assert runtime.health()["protection_updates_failed"]
            assert runtime.health()["protection_recovery"]["unresolved_failures"] == 1
        finally:
            await runtime.shutdown()
            await store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("boundary", ["lookup", "encode", "commit", "postcommit", "publish", "cancel", "duplicate"])
def test_storage_boundary_failure_is_never_a_successful_disposition(tmp_path, monkeypatch, boundary):
    async def scenario():
        _, _, store, runtime = await holding(tmp_path)
        try:
            command = await fail_quote(runtime, store, monkeypatch)
            before = runtime.owner.state
            commit = store.commit
            async def failure(expected, state, commit_id):
                if commit_id == "command:" + command:
                    if boundary == "postcommit":
                        await commit(expected, state, commit_id)
                    if boundary == "cancel":
                        raise asyncio.CancelledError()
                    raise OSError("합성 저장 경계 실패")
                return await commit(expected, state, commit_id)
            with monkeypatch.context() as patch:
                if boundary == "lookup":
                    async def bad_lookup(commit_id):
                        raise OSError("합성 lookup 실패")
                    patch.setattr(store, "lookup_commit", bad_lookup)
                elif boundary == "encode":
                    import src.execution.safety.application as application
                    def bad_encode(value):
                        raise ValueError("합성 encode 실패")
                    patch.setattr(application, "encode_state", bad_encode)
                elif boundary == "publish":
                    def bad_publish(*args):
                        raise ValueError("합성 publish 실패")
                    patch.setattr(runtime.owner, "publisher", bad_publish)
                elif boundary == "duplicate":
                    await runtime.owner.mutate(command, lambda state: state)
                else:
                    patch.setattr(store, "commit", failure)
                with pytest.raises((OSError, ValueError, ApplicationBlocked, asyncio.CancelledError)):
                    await runtime.resume_protection_admission()
            assert runtime.health()["protection_updates_failed"]
            recovery = runtime.health()["protection_recovery"]
            assert recovery.get("protection_admission_resumed", 0) == 0
            assert recovery.get("protection_admission_abandoned", 0) == 0
            await runtime.restore()
            if boundary in ("postcommit", "publish"):
                assert command not in runtime.owner.state["protection_quote_admissions"]
                assert await runtime.resume_protection_admission() == []
                assert runtime.health()["protection_updates_failed"]
            else:
                assert runtime.owner.state["protection_quote_admissions"] == before["protection_quote_admissions"]
        finally:
            await runtime.shutdown()
            await store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("damage", ["digest", "future", "inconsistent", "view", "market_data"])
def test_malformed_admission_is_preserved_even_when_old(tmp_path, monkeypatch, damage):
    async def scenario():
        _, _, store, runtime = await holding(tmp_path)
        try:
            command = await fail_quote(runtime, store, monkeypatch)
            def corrupt(state):
                row = state["protection_quote_admissions"][command]
                if damage == "digest":
                    row["payload_digest"] = "0" * 64
                else:
                    if damage == "future":
                        row["observed_at"] = (NOW + timedelta(seconds=500)).isoformat()
                    elif damage == "inconsistent":
                        row["admitted_at"] = (NOW - timedelta(seconds=1)).isoformat()
                    elif damage == "market_data":
                        row["market_data"] = []
                    else:
                        row["price"] = "8800"
                    request = {k: v for k, v in row.items() if k not in
                               {"payload_digest", "status", "source_version", "admitted_at"}}
                    row["payload_digest"] = digest(request)
                return state
            await runtime.owner.mutate("synthetic-corrupt", corrupt)
            before = runtime.owner.state
            runtime.clock = lambda: NOW + timedelta(seconds=61)
            with pytest.raises((ValueError, ApplicationBlocked)):
                await runtime.resume_protection_admission()
            assert runtime.owner.state == before
        finally:
            await runtime.shutdown()
            await store.close()
    asyncio.run(scenario())


def test_completed_ws_duplicate_and_other_symbol_success_do_not_clear_failure(tmp_path, monkeypatch):
    async def scenario():
        _, _, store, runtime = await holding(tmp_path)
        try:
            event = await market_event(monkeypatch, NOW)
            await runtime.observe_market(event)
            await fail_quote(runtime, store, monkeypatch, symbol="000660")
            version = runtime.owner.version
            await runtime.observe_market(event)
            assert runtime.owner.version == version
            await runtime.quote("005930", Decimal("10100"))
            assert runtime.health()["protection_updates_failed"]
            assert runtime.health()["protection_recovery"]["unresolved_failures"] == 1
        finally:
            await runtime.shutdown()
            await store.close()
    asyncio.run(scenario())


def test_late_failed_task_callback_does_not_relatch_resumed_quote(tmp_path, monkeypatch):
    async def scenario():
        _, _, store, runtime = await holding(tmp_path)
        captured = []
        try:
            original = runtime._protection_task
            def capture(*args, **kwargs):
                task = original(*args, **kwargs)
                captured.append((task, task._callbacks[0][0]))
                return task
            monkeypatch.setattr(runtime, "_protection_task", capture)
            await fail_quote(runtime, store, monkeypatch)
            task, callback = captured[0]
            await runtime.resume_protection_admission()
            assert not runtime.health()["protection_updates_failed"]
            callback(task)
            assert not runtime.health()["protection_updates_failed"]
        finally:
            await runtime.shutdown()
            await store.close()
    asyncio.run(scenario())


def test_existing_floor_discards_recent_explicit_quote_without_calculation(tmp_path, monkeypatch):
    async def scenario():
        _, _, store, runtime = await holding(tmp_path)
        try:
            event = await market_event(monkeypatch, NOW, price="9000")
            command = await fail_quote(runtime, store, monkeypatch, event=event)
            runtime.clock = lambda: NOW + timedelta(seconds=2)
            def newer_valuation(state):
                state["day_valuation_view"] = {"evidence_id": "synthetic", "rollover_version": runtime.owner.version + 1}
                state["day_valuations"] = {"synthetic": {"payload": {"prices": [{"symbol": "005930",
                    "price": "10000", "as_of": (NOW + timedelta(seconds=1)).isoformat()}]}}}
                return state
            await runtime.owner.mutate("synthetic-floor", newer_valuation)
            before = runtime.owner.state["protection"]
            assert await runtime.resume_protection_admission() == [("005930", "stale_admission_discarded", None)]
            assert runtime.owner.state["protection"] == before
            assert command not in runtime.owner.state["protection_quote_admissions"]
            assert "market_sources" not in runtime.owner.state
        finally:
            await runtime.shutdown()
            await store.close()
    asyncio.run(scenario())


def test_resume_decode_and_encode_errors_are_not_calculation_abandonment(tmp_path, monkeypatch):
    async def scenario():
        _, _, store, runtime = await holding(tmp_path)
        try:
            await fail_quote(runtime, store, monkeypatch)
            import src.execution.safety.runtime as module
            for method in ("decode_protection", "encode_protection"):
                before = runtime.owner.state
                def broken(*args, **kwargs):
                    raise ValueError("합성 보호 직렬화 오류")
                with monkeypatch.context() as patch:
                    patch.setattr(module, method, broken)
                    with pytest.raises(ValueError):
                        await runtime.resume_protection_admission()
                await runtime.restore()
                assert runtime.owner.state == before
                assert runtime.health()["protection_recovery"].get("protection_admission_abandoned", 0) == 0
        finally:
            await runtime.shutdown()
            await store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("mode", ["stale", "resumed", "abandoned"])
def test_old_ws_source_cannot_authorize_buy_even_in_new_runtime(tmp_path, monkeypatch, mode):
    async def scenario():
        from test_execution_command_owner import fixture
        f = await fixture(tmp_path, monkeypatch)
        runtime, store = f["runtime"], f["store"]
        fresh = None
        try:
            now = f["clock"][0]
            event = await market_event(monkeypatch, now)
            await runtime.observe_market(event)
            newer = await market_event(monkeypatch, now + timedelta(seconds=1), price="10100")
            f["clock"][0] = now + timedelta(seconds=1)
            await fail_quote(runtime, store, monkeypatch, event=newer)
            with monkeypatch.context() as patch:
                if mode == "stale":
                    f["clock"][0] = now + timedelta(seconds=62)
                elif mode == "abandoned":
                    from src.strategies.exit_manager import ExitManager
                    def bad_calculation(*args):
                        raise ValueError("합성 계산 실패")
                    patch.setattr(ExitManager, "update_price", bad_calculation)
                await runtime.resume_protection_admission()
            await runtime.shutdown()
            fresh = KRExecutionRuntime(store, f["engine"], f["exits"], clock=lambda: f["clock"][0], account_scope="test-scope")
            await fresh.restore()
            previous = f["commands"]
            commands = type(previous)(fresh, builder=f["builder"], authority=f["authority"],
                entry_guard=previous.entry_guard, stop_resolver=previous.stop_resolver, session_guard=previous.session_guard)
            request = f["request"]()
            assert not fresh.market_source_pending(fresh.owner.state)
            with pytest.raises(ValueError, match="current_market_source_required"):
                await commands.prepare(request, f["entry"](request))
            assert f["broker"]._session.posts == []
        finally:
            if fresh is not None:
                await fresh.shutdown()
            await runtime.shutdown()
            await store.close()
    asyncio.run(scenario())


def test_cancelled_resume_waiter_keeps_accepted_commit_and_shutdown_drain(tmp_path, monkeypatch):
    async def scenario():
        _, _, store, runtime = await holding(tmp_path)
        reached, release = asyncio.Event(), asyncio.Event()
        try:
            command = await fail_quote(runtime, store, monkeypatch)
            commit = store.commit
            async def held(expected, state, commit_id):
                if commit_id == "command:" + command:
                    reached.set()
                    await release.wait()
                return await commit(expected, state, commit_id)
            monkeypatch.setattr(store, "commit", held)
            caller = asyncio.create_task(runtime.resume_protection_admission())
            await asyncio.wait_for(reached.wait(), 2)
            caller.cancel()
            with pytest.raises(asyncio.CancelledError):
                await caller
            shutdown = asyncio.create_task(runtime.shutdown())
            await asyncio.sleep(0)
            assert not shutdown.done()
            release.set()
            await shutdown
            assert command not in runtime.owner.state["protection_quote_admissions"]
            assert command in runtime.owner.state["outbox"]
            assert not runtime.health()["protection_updates_failed"]
        finally:
            release.set()
            await runtime.shutdown()
            await store.close()
    asyncio.run(scenario())


def test_admission_id_requires_its_original_sql_admission_receipt(tmp_path, monkeypatch):
    async def scenario():
        _, _, store, runtime = await holding(tmp_path)
        try:
            original = await fail_quote(runtime, store, monkeypatch)
            def forge_id(state):
                state["protection_quote_admissions"]["quote:forged"] = state["protection_quote_admissions"].pop(original)
                return state
            await runtime.owner.mutate("synthetic-forged-admission-id", forge_id)
            before = runtime.owner.state
            with pytest.raises(ApplicationBlocked):
                await runtime.resume_protection_admission()
            assert runtime.owner.state == before
        finally:
            await runtime.shutdown()
            await store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("price", ["9000", "10100", "12000"])
@pytest.mark.parametrize("pending", [False, True])
def test_resume_calculation_matches_normal_quote_with_and_without_pending(tmp_path, monkeypatch, price, pending):
    async def scenario():
        _, _, original_store, original = await holding(tmp_path / "normal")
        _, _, resumed_store, resumed = await holding(tmp_path / "resumed")
        try:
            if pending:
                for runtime in (original, resumed):
                    await runtime.quote("005930", Decimal("12000"), intent_id="first")
            expected = await original.quote("005930", Decimal(price), intent_id="stop-005930")
            await fail_quote(resumed, resumed_store, monkeypatch, price=price)
            result = await resumed.resume_protection_admission()
            assert result == [("005930", "protection_admission_resumed", expected)]
            assert resumed.owner.state["protection"] == original.owner.state["protection"]
            assert resumed.owner.state["portfolio"] == original.owner.state["portfolio"]
        finally:
            await original.shutdown()
            await resumed.shutdown()
            await original_store.close()
            await resumed_store.close()
    asyncio.run(scenario())


def test_cancelled_accepted_quote_is_attributed_before_callback_and_resumable(tmp_path, monkeypatch):
    async def scenario():
        _, _, store, runtime = await holding(tmp_path)
        reached, release = asyncio.Event(), asyncio.Event()
        caller = None
        try:
            commit = store.commit
            async def hold(expected, state, command):
                if command.startswith("command:quote:"):
                    reached.set()
                    await release.wait()
                return await commit(expected, state, command)
            with monkeypatch.context() as patch:
                patch.setattr(store, "commit", hold)
                caller = asyncio.create_task(runtime.quote("005930", Decimal("9000"), intent_id="stop"))
                await asyncio.wait_for(reached.wait(), 2)
                accepted = next(task for task in runtime._protection_tasks if not task.done())
                accepted.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await caller
            await runtime.restore()
            assert runtime.health()["protection_recovery"]["unresolved_failures"] == 1
            assert len(runtime.owner.state["protection_quote_admissions"]) == 1
            assert (await runtime.resume_protection_admission())[0][1] == "protection_admission_resumed"
            assert not runtime.health()["protection_updates_failed"]
        finally:
            release.set()
            if caller is not None:
                await asyncio.gather(caller, return_exceptions=True)
            await runtime.shutdown()
            await store.close()
    asyncio.run(scenario())
