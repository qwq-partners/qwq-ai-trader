"""실제 SQLite와 경제 상태 reducer를 연결한 체결 완료/복구 계약."""
import asyncio
from copy import deepcopy
from decimal import Decimal
import json
import sqlite3
import threading
from dataclasses import replace

import pytest

from src.execution.safety.application import (
    FillApplicationCoordinator, FillObservation, FillReduction,
    ApplicationBlocked, ObservationError,
)
from src.execution.safety.store import ExecutionStateStore, StoreError


def observation(qty, amount, *, side="BUY", order="order-1", metadata=None):
    return FillObservation(
        account_scope="test-account", market="KR", trading_day="2026-09-17",
        exchange="KRX", order_id=order, symbol="005930", side=side,
        cumulative_quantity=qty, cumulative_amount=Decimal(amount),
        cumulative_fee=Decimal("0"), metadata=metadata or {},
    )


def economic_reducer(state, obs, delta):
    """고정 합성 가격의 실제 수량·현금을 계산; 성공만 반환하는 mock이 아니다."""
    portfolio = state.setdefault("portfolio", {"quantity": 0, "cash": "2000"})
    quantity = portfolio["quantity"]
    cash = Decimal(portfolio["cash"])
    if obs.side == "BUY":
        quantity += delta.quantity
        cash -= delta.amount + delta.fee
    else:
        if delta.quantity > quantity:
            raise ValueError("unknown sell quantity")
        quantity -= delta.quantity
        cash += delta.amount - delta.fee
    portfolio.update(quantity=quantity, cash=str(cash))
    protection = state.setdefault("protection", {"stage": "FIRST", "high": "12"})
    protection["quantity"] = quantity
    return FillReduction(state, protection_status="ready", journal_pending=True)


def test_partial_duplicate_stale_and_restart_apply_economics_once(tmp_path):
    async def scenario():
        path = tmp_path / "state" / "execution.sqlite3"
        live = {}
        def publish(state, version):
            live.clear()
            live.update(state)
        store = ExecutionStateStore(path)
        app = FillApplicationCoordinator(store, publish, economic_reducer)
        await app.restore()
        first = await app.apply(observation(40, "400"))
        assert first.status == "APPLIED"
        assert live["portfolio"] == {"quantity": 40, "cash": "1600"}
        assert (await app.apply(observation(40, "400"))).status == "ALREADY_APPLIED"
        await app.apply(observation(100, "1060"))
        assert live["portfolio"] == {"quantity": 100, "cash": "940"}
        assert live["protection"]["quantity"] == 100
        assert live["protection"]["stage"] == "FIRST"
        assert (await app.apply(observation(40, "400"))).status == "ALREADY_APPLIED"
        await store.close()
        reopened = ExecutionStateStore(path)
        app = FillApplicationCoordinator(reopened, publish, economic_reducer)
        await app.restore()
        assert (await app.apply(observation(100, "1060"))).status == "ALREADY_APPLIED"
        await app.apply(observation(40, "480", side="SELL", order="sell-1"))
        assert live["portfolio"] == {"quantity": 60, "cash": "1420"}
        assert live["protection"]["quantity"] == 60
        await app.apply(observation(100, "1200", side="SELL", order="sell-1"))
        assert live["portfolio"] == {"quantity": 0, "cash": "2140"}
        assert live["protection"]["quantity"] == 0
        await reopened.close()
    asyncio.run(scenario())


def test_conflicting_cumulative_amount_and_identity_preserve_economic_state(tmp_path):
    async def scenario():
        store = ExecutionStateStore(tmp_path / "state" / "execution.sqlite3")
        app = FillApplicationCoordinator(store, lambda *_: None, economic_reducer)
        await app.restore()
        await app.apply(observation(40, "400"))
        assert (await app.apply(observation(40, "401"))).status == "NEEDS_RECONCILIATION"
        assert (await app.apply(observation(60, "600", side="SELL"))).status == "NEEDS_RECONCILIATION"
        assert app.state["portfolio"] == {"quantity": 40, "cash": "1600"}
        assert any(row["status"] == "NEEDS_RECONCILIATION" for row in app.state["inbox"].values())
        await store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("qty,amount", [(True, "1"), (-1, "1"), (1, "NaN"), (1, "Infinity"), (1, "-1"), (0, "1")])
def test_invalid_observations_are_rejected(qty, amount):
    with pytest.raises(ObservationError):
        observation(qty, amount)


@pytest.mark.parametrize("field", ["account_scope", "market", "exchange", "order_id", "symbol", "org_no", "parent_order_no"])
def test_identity_whitespace_is_rejected(field):
    with pytest.raises(ObservationError):
        replace(observation(40, "400"), **{field: "000123 "})


@pytest.mark.parametrize("order_id", ["TEMP_123", "local-123"])
def test_synthetic_order_identity_is_rejected(order_id):
    with pytest.raises(ObservationError):
        observation(40, "400", order=order_id)


def test_whitespace_variant_cannot_reapply_after_restart(tmp_path):
    async def scenario():
        path = tmp_path / "state" / "execution.sqlite3"
        store = ExecutionStateStore(path)
        app = FillApplicationCoordinator(store, lambda *_: None, economic_reducer)
        await app.restore()
        await app.apply(observation(40, "400", order="000123"))
        await store.close()
        store = ExecutionStateStore(path)
        app = FillApplicationCoordinator(store, lambda *_: None, economic_reducer)
        await app.restore()
        with pytest.raises(ObservationError):
            await app.apply(observation(40, "400", order="000123 "))
        assert (await app.apply(observation(40, "400", order="000123"))).status == "ALREADY_APPLIED"
        assert app.state["portfolio"]["quantity"] == 40
        await store.close()
    asyncio.run(scenario())


def reserved_checkpoint(obs):
    ref = {"account_scope": obs.account_scope, "market": obs.market,
           "order_date": obs.trading_day, "exchange": obs.exchange,
           "order_no": obs.order_id, "org_no": obs.org_no,
           "parent_order_no": obs.parent_order_no}
    return {"startup_reconciliation": {"ready": False},
            "intents": {"intent-1": {"target_quantity": 100}},
            "attempts": {"attempt-1": {
                "kind": "submit", "order_ref": ref, "symbol": obs.symbol,
                "side": obs.side.lower(), "quantity": 100,
                "applied_quantity": 0, "observed_quantity": 100,
                "reserved_quantity": 100, "reserved_cash": "1000",
                "state": "partial", "version": 3}}}


@pytest.mark.parametrize("corruption", ["economic_only", "drop_attempts", "drop_intents", "startup", "new_root", "inbox", "cursor", "attempt_delete", "attempt_identity", "attempt_state", "unrelated_attempt", "applied_ahead", "reservation_overrelease"])
def test_fill_write_set_rejects_protected_state_changes(tmp_path, corruption):
    async def scenario():
        obs = observation(40, "400")
        baseline = reserved_checkpoint(obs)
        baseline["attempts"]["other"] = {**deepcopy(baseline["attempts"]["attempt-1"]),
                                           "order_ref": {**baseline["attempts"]["attempt-1"]["order_ref"], "order_no": "other"}}
        def corrupt(state, obs, delta):
            result = economic_reducer(state, obs, delta)
            if corruption == "economic_only":
                return FillReduction({"portfolio": state["portfolio"]})
            if corruption == "drop_attempts": del state["attempts"]
            elif corruption == "drop_intents": del state["intents"]
            elif corruption == "startup": state["startup_reconciliation"]["ready"] = True
            elif corruption == "new_root": state["unknown_control"] = True
            elif corruption == "inbox": state["inbox"] = {}
            elif corruption == "cursor": state["cursors"] = {}
            elif corruption == "attempt_delete": del state["attempts"]["other"]
            elif corruption == "attempt_identity": state["attempts"]["attempt-1"]["symbol"] = "000660"
            elif corruption == "attempt_state": state["attempts"]["attempt-1"]["state"] = "final_cancelled"
            elif corruption == "unrelated_attempt": state["attempts"]["other"]["reserved_quantity"] = 0
            elif corruption == "applied_ahead": state["attempts"]["attempt-1"]["applied_quantity"] = 100
            elif corruption == "reservation_overrelease":
                state["attempts"]["attempt-1"].update(applied_quantity=40, reserved_quantity=60, reserved_cash="0")
            return result
        store = ExecutionStateStore(tmp_path / "state" / "execution.sqlite3")
        app = FillApplicationCoordinator(store, lambda *_: None, corrupt)
        await app.restore()
        await app.mutate("baseline", lambda _: deepcopy(baseline))
        receipt = await app.apply(obs)
        assert receipt.status == "FAILED"
        for key in ("attempts", "intents", "startup_reconciliation"):
            assert app.state[key] == baseline[key]
        assert "portfolio" not in app.state
        assert not app.state.get("cursors")
        assert app.state["inbox"][obs.observation_id]["status"] == "RECEIVED"
        await store.close()
    asyncio.run(scenario())


def test_fill_matching_attempt_updates_and_trusted_mutate_remain_supported(tmp_path):
    async def scenario():
        def reduce(state, obs, delta):
            result = economic_reducer(state, obs, delta)
            state["attempts"]["attempt-1"].update(
                applied_quantity=obs.cumulative_quantity,
                reserved_quantity=100 - obs.cumulative_quantity,
                reserved_cash=str(Decimal(100 - obs.cumulative_quantity) * 10))
            return result
        store = ExecutionStateStore(tmp_path / "state" / "execution.sqlite3")
        app = FillApplicationCoordinator(store, lambda *_: None, reduce)
        await app.restore()
        await app.mutate("baseline", lambda _: reserved_checkpoint(observation(40, "400")))
        assert (await app.apply(observation(40, "400"))).status == "APPLIED"
        assert app.state["attempts"]["attempt-1"]["reserved_cash"] == "600"
        assert (await app.apply(observation(100, "1000"))).status == "APPLIED"
        assert app.state["attempts"]["attempt-1"]["reserved_quantity"] == 0
        await app.mutate("trusted-control", lambda state: {**state, "startup_reconciliation": {"ready": True}})
        assert app.state["startup_reconciliation"]["ready"] is True
        await store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("boundary", ["duplicate_match", "observed_behind", "cursor_mismatch", "reservation_quantity_overrelease", "reservation_increase", "cash_increase", "cash_nonfinite", "boolean_quantity", "new_attempt", "drop_domain_root", "protected_bool_to_int"])
def test_attempt_write_set_boundary_preserves_checkpoint(tmp_path, boundary):
    async def scenario():
        obs = observation(40, "400")
        baseline = reserved_checkpoint(obs)
        baseline["portfolio"] = {"quantity": 0, "cash": "2000"}
        if boundary == "duplicate_match":
            baseline["attempts"]["duplicate"] = deepcopy(baseline["attempts"]["attempt-1"])
        elif boundary == "observed_behind": baseline["attempts"]["attempt-1"]["observed_quantity"] = 20
        elif boundary == "cursor_mismatch": baseline["attempts"]["attempt-1"]["applied_quantity"] = 10
        def reduce(state, obs, delta):
            result = economic_reducer(state, obs, delta)
            attempt = state["attempts"]["attempt-1"]
            attempt.update(applied_quantity=40, reserved_quantity=60, reserved_cash="600")
            if boundary == "reservation_quantity_overrelease": attempt["reserved_quantity"] = 59
            elif boundary == "reservation_increase": attempt["reserved_quantity"] = 101
            elif boundary == "cash_increase": attempt["reserved_cash"] = "1001"
            elif boundary == "cash_nonfinite": attempt["reserved_cash"] = "NaN"
            elif boundary == "boolean_quantity": attempt["applied_quantity"] = True
            elif boundary == "new_attempt": state["attempts"]["new"] = deepcopy(attempt)
            elif boundary == "drop_domain_root": del state["portfolio"]
            elif boundary == "protected_bool_to_int": state["startup_reconciliation"]["ready"] = 0
            return result
        path = tmp_path / "state" / "execution.sqlite3"
        store = ExecutionStateStore(path)
        app = FillApplicationCoordinator(store, lambda *_: None, reduce)
        await app.restore()
        await app.mutate("baseline", lambda _: deepcopy(baseline))
        assert (await app.apply(obs)).status == "FAILED"
        await store.close()
        store = ExecutionStateStore(path)
        app = FillApplicationCoordinator(store, lambda *_: None, reduce)
        await app.restore()
        for key, value in baseline.items():
            assert app.state[key] == value
        assert not app.state.get("cursors")
        assert app.state["inbox"][obs.observation_id]["status"] == "RECEIVED"
        await store.close()
    asyncio.run(scenario())


def test_degraded_is_committed_once_and_repair_is_not_another_fill(tmp_path):
    async def scenario():
        def degraded(state, obs, delta):
            result = economic_reducer(state, obs, delta)
            return FillReduction(result.state, protection_status="degraded", journal_pending=True)
        store = ExecutionStateStore(tmp_path / "state" / "execution.sqlite3")
        app = FillApplicationCoordinator(store, lambda *_: None, degraded)
        await app.restore()
        receipt = await app.apply(observation(40, "400"))
        assert (receipt.status, receipt.protection_status) == ("APPLIED", "degraded")
        assert (await app.apply(observation(40, "400"))).protection_status == "degraded"
        def repair(state):
            state["protection"]["repaired"] = True
            return state
        await app.mutate("repair-1", repair)
        assert app.state["portfolio"] == {"quantity": 40, "cash": "1600"}
        await store.close()
    asyncio.run(scenario())


def test_failed_reducer_preserves_durable_inbox_and_retries_without_lost_fill(tmp_path):
    async def scenario():
        def fail(state, obs, delta):
            state["portfolio"] = {"quantity": 999, "cash": "0"}
            raise ValueError("injected reducer failure")
        store = ExecutionStateStore(tmp_path / "state" / "execution.sqlite3")
        app = FillApplicationCoordinator(store, lambda *_: None, fail)
        await app.restore()
        receipt = await app.apply(observation(40, "400"))
        assert receipt.status == "FAILED"
        assert "portfolio" not in app.state
        assert len(app.state["inbox"]) == 1
        assert not app.state.get("cursors")
        app = FillApplicationCoordinator(store, lambda *_: None, economic_reducer)
        await app.restore()
        assert (await app.apply(observation(40, "400"))).status == "APPLIED"
        assert app.state["portfolio"]["quantity"] == 40
        await store.close()
    asyncio.run(scenario())


def test_commit_then_failed_publication_blocks_next_command_until_restore(tmp_path):
    async def scenario():
        failing = False
        live = {"quote": "99"}
        def publish(state, version):
            if failing and "portfolio" in state:
                raise RuntimeError("publication failed")
            live["execution"] = deepcopy(state)
        store = ExecutionStateStore(tmp_path / "state" / "execution.sqlite3")
        app = FillApplicationCoordinator(store, publish, economic_reducer)
        await app.restore()
        failing = True
        with pytest.raises(ApplicationBlocked):
            await app.apply(observation(40, "400"))
        assert app.publication_recovery_required and not app.healthy
        with pytest.raises(ApplicationBlocked):
            await app.mutate("other", lambda state: state)
        failing = False
        await app.restore()
        assert app.healthy and app.published_version == app.version
        assert live["quote"] == "99"
        assert live["execution"]["portfolio"]["quantity"] == 40
        assert (await app.apply(observation(40, "400"))).status == "ALREADY_APPLIED"
        await store.close()
    asyncio.run(scenario())


def test_mutation_is_copy_isolated_and_serialized_with_fill_writer(tmp_path):
    async def scenario():
        store = ExecutionStateStore(tmp_path / "state" / "execution.sqlite3")
        app = FillApplicationCoordinator(store, lambda *_: None, economic_reducer)
        await app.restore()
        entered, release = asyncio.Event(), asyncio.Event()
        real_commit = store.commit
        async def delayed_commit(expected_version, state, commit_id):
            if "portfolio" in state and state["portfolio"]["quantity"] == 40 and not entered.is_set():
                entered.set()
                await release.wait()
            return await real_commit(expected_version, state, commit_id)
        store.commit = delayed_commit
        fill = asyncio.create_task(app.apply(observation(40, "400")))
        await entered.wait()
        called = asyncio.Event()
        def quote(state):
            called.set()
            state["protection"]["high"] = "15"
            return state
        mutation = asyncio.create_task(app.mutate("quote-1", quote))
        await asyncio.sleep(0)
        assert not called.is_set()
        # commit 전/게시 전 사이에는 옛 상태로 새 주문을 승인하면 안 된다.
        assert not app.healthy
        release.set()
        await fill
        await mutation
        external = app.state
        external["portfolio"]["quantity"] = 999
        assert app.state["portfolio"]["quantity"] == 40
        assert app.state["protection"]["high"] == "15"
        assert app.published_version == app.version
        await store.close()
    asyncio.run(scenario())


def test_unseen_stale_or_decimal_equivalent_observation_does_not_leave_pending_inbox(tmp_path):
    async def scenario():
        store = ExecutionStateStore(tmp_path / "state" / "execution.sqlite3")
        app = FillApplicationCoordinator(store, lambda *_: None, economic_reducer)
        await app.restore()
        await app.apply(observation(100, "1000"))
        assert (await app.apply(observation(40, "400"))).status == "ALREADY_APPLIED"
        assert (await app.apply(observation(100, "1000.00"))).status == "ALREADY_APPLIED"
        assert all(row["status"] in ("APPLIED", "SUPERSEDED") for row in app.state["inbox"].values())
        assert app.state["portfolio"] == {"quantity": 100, "cash": "1000"}
        await store.close()
    asyncio.run(scenario())


def test_cancelled_writer_commit_must_be_reloaded_before_replay(tmp_path):
    async def scenario():
        store = ExecutionStateStore(tmp_path / "state" / "execution.sqlite3")
        app = FillApplicationCoordinator(store, lambda *_: None, economic_reducer)
        await app.restore()
        entered, release = asyncio.Event(), threading.Event()
        loop = asyncio.get_running_loop()
        real_commit = store._commit
        def delayed_commit(version, payload, commit_id):
            if "portfolio" in json.loads(payload):
                loop.call_soon_threadsafe(entered.set)
                if not release.wait(5):
                    raise RuntimeError("test writer barrier timeout")
            return real_commit(version, payload, commit_id)
        store._commit = delayed_commit
        task = asyncio.create_task(app.apply(observation(40, "400")))
        await entered.wait()
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert app.publication_recovery_required and not app.healthy
        durable_version, durable = await store.load()
        assert durable["portfolio"]["quantity"] == 40
        await app.restore()
        assert app.published_version == durable_version
        assert (await app.apply(observation(40, "400"))).status == "ALREADY_APPLIED"
        assert app.state["portfolio"]["quantity"] == 40
        await store.close()
    asyncio.run(scenario())


def test_mutate_command_replay_and_reducer_reference_cannot_change_committed_state(tmp_path):
    async def scenario():
        store = ExecutionStateStore(tmp_path / "state" / "execution.sqlite3")
        app = FillApplicationCoordinator(store, lambda *_: None)
        await app.restore()
        retained = []
        def reserve(state):
            state["attempts"] = {"one": {"reserved": "100"}}
            retained.append(state)
            return state
        version = await app.mutate("reserve-1", reserve)
        retained[0]["attempts"]["one"]["reserved"] = "999"
        assert await app.mutate("reserve-1", reserve) == version
        assert app.state["attempts"]["one"]["reserved"] == "100"
        assert len(retained) == 1
        await store.close()
    asyncio.run(scenario())


def test_observation_metadata_is_deeply_immutable_and_order_key_has_complete_scope():
    metadata = {"signal": {"score": 80}, "tags": ["entry"]}
    obs = observation(40, "400", metadata=metadata)
    metadata["signal"]["score"] = 0
    assert obs.metadata["signal"]["score"] == 80
    with pytest.raises(TypeError):
        obs.metadata["signal"]["score"] = 1
    assert json.loads(obs.order_key) == ["test-account", "KR", "2026-09-17", "KRX", "order-1", "", ""]


def test_economic_commit_failure_keeps_inbox_and_reservation_for_recovery(tmp_path):
    async def scenario():
        path = tmp_path / "state" / "execution.sqlite3"
        store = ExecutionStateStore(path)
        app = FillApplicationCoordinator(store, lambda *_: None, economic_reducer)
        await app.restore()
        await app.mutate("reserve", lambda state: {**state, "attempts": {"one": {"reserved": "400"}}})
        with sqlite3.connect(path) as conn:
            conn.execute("""CREATE TRIGGER fail_money BEFORE UPDATE ON checkpoint
                WHEN json_extract(NEW.state, '$.portfolio') IS NOT NULL
                BEGIN SELECT RAISE(ABORT, 'injected economic commit failure'); END""")
        with pytest.raises(StoreError):
            await app.apply(observation(40, "400"))
        assert not app.healthy
        _, durable = await store.load()
        assert durable["attempts"]["one"]["reserved"] == "400"
        assert "portfolio" not in durable
        assert [row["status"] for row in durable["inbox"].values()] == ["RECEIVED"]
        assert not durable.get("cursors")
        with sqlite3.connect(path) as conn:
            conn.execute("DROP TRIGGER fail_money")
        await app.restore()
        assert (await app.apply(observation(40, "400"))).status == "APPLIED"
        assert app.state["portfolio"]["quantity"] == 40
        await store.close()
    asyncio.run(scenario())


def test_commit_success_with_lost_response_does_not_reapply_after_restore(tmp_path):
    async def scenario():
        store = ExecutionStateStore(tmp_path / "state" / "execution.sqlite3")
        app = FillApplicationCoordinator(store, lambda *_: None, economic_reducer)
        await app.restore()
        real_commit = store.commit
        async def lose_response(version, state, commit_id):
            result = await real_commit(version, state, commit_id)
            if commit_id.startswith("fill:"):
                raise StoreError("injected lost commit response")
            return result
        store.commit = lose_response
        with pytest.raises(StoreError):
            await app.apply(observation(40, "400"))
        assert not app.healthy
        await app.restore()
        assert (await app.apply(observation(40, "400"))).status == "ALREADY_APPLIED"
        assert app.state["portfolio"] == {"quantity": 40, "cash": "1600"}
        await store.close()
    asyncio.run(scenario())


def test_known_old_cumulative_amount_correction_is_not_silently_discarded(tmp_path):
    async def scenario():
        store = ExecutionStateStore(tmp_path / "state" / "execution.sqlite3")
        app = FillApplicationCoordinator(store, lambda *_: None, economic_reducer)
        await app.restore()
        await app.apply(observation(40, "400"))
        await app.apply(observation(100, "1000"))
        assert (await app.apply(observation(40, "401"))).status == "NEEDS_RECONCILIATION"
        assert app.state["portfolio"] == {"quantity": 100, "cash": "1000"}
        await store.close()
    asyncio.run(scenario())


def test_identity_cannot_change_after_inbox_received_but_before_application(tmp_path):
    async def scenario():
        store = ExecutionStateStore(tmp_path / "state" / "execution.sqlite3")
        app = FillApplicationCoordinator(store, lambda *_: None)
        await app.restore()
        assert (await app.apply(observation(40, "400"))).status == "FAILED"
        app.reducer = economic_reducer
        assert (await app.apply(observation(40, "400", metadata={"strategy": "other"}))).status == "NEEDS_RECONCILIATION"
        assert "portfolio" not in app.state
        await store.close()
    asyncio.run(scenario())
