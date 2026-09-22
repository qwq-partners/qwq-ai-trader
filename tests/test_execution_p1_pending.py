"""보호 pending 해제는 관측·적용 체결이 모두 없는 종결 증거에만 의존한다."""
import asyncio
from copy import deepcopy
from datetime import timedelta
from decimal import Decimal

import pytest

from src.execution.safety.lifecycle import CommandResult, CommandStatus, OrderEvidence, OrderState
from src.execution.safety.runtime import KRExecutionRuntime
from test_execution_runtime import NOW, setup, opened, observed, queued


@pytest.fixture(autouse=True)
def frozen_product_clock(monkeypatch):
    from test_execution_install_factory import freeze
    freeze(monkeypatch)


async def holding(tmp_path):
    engine, exits, store, runtime = await setup(tmp_path, clock=lambda: NOW)
    ref = await opened(runtime, "buy")
    await queued(engine, await observed(runtime, ref, 100, "1000000"))
    return engine, exits, store, runtime


@pytest.mark.parametrize("restart", [False, True])
@pytest.mark.parametrize("attempt", [False, True])
def test_release_terminal_or_orphan_preserves_other_owner_state(tmp_path, restart, attempt):
    async def scenario():
        engine, exits, store, runtime = await holding(tmp_path)
        try:
            assert await runtime.quote("005930", Decimal("12000"), intent_id="stop") is not None
            assert runtime.owner.state["protection"]["states"]["005930"]["pending_stage"] is not None
            if attempt:
                await runtime.lifecycle.prepare("stop", "sell", 100, "005930", "sell")
                await runtime.lifecycle.claim("sell", "sender")
                await runtime.lifecycle.record_result("sell", "sender", CommandResult(CommandStatus.NOT_SENT, "sell"))
            if restart:
                await runtime.shutdown()
                runtime = KRExecutionRuntime(store, engine, exits, clock=lambda: NOW)
                await runtime.restore()
            runtime._day_closed = True
            before = runtime.owner.state
            assert await runtime.release_protection_pending("005930")
            after = runtime.owner.state
            expected = deepcopy(before)
            row = expected["protection"]["states"]["005930"]
            row.update(pending_stage=None, pending_since=None, pending_target_qty=0, pending_filled_qty=0)
            del expected["protection"]["pending_owners"]["005930"]
            assert after == expected
            version = runtime.owner.version
            assert not await runtime.release_protection_pending("005930")
            assert runtime.owner.version == version
        finally:
            await runtime.shutdown()
            await store.close()
    asyncio.run(scenario())


def test_real_partial_applied_terminal_sell_keeps_pending(tmp_path):
    async def scenario():
        engine, _, store, runtime = await holding(tmp_path)
        try:
            decision = await runtime.quote("005930", Decimal("12000"), intent_id="stop")
            quantity = decision[1]
            assert quantity > 1
            ref = await opened(runtime, "sell", "sell", quantity=quantity, intent="stop")
            await queued(engine, await observed(runtime, ref, 1, "12000", side="sell", total=quantity))
            evidence = OrderEvidence(ref, "005930", "sell", quantity, 1, Decimal("12000"), 0,
                quantity - 1, OrderState.FINAL_CANCELLED, complete=True, supported_finality=True,
                source_contract="synthetic-test", observed_at=NOW, request_started_at=NOW - timedelta(seconds=1),
                query_scope={"account_scope": "scope", "market": "KR", "exchange": "KRX",
                    "start_date": "2026-09-18", "end_date": "2026-09-18", "tr_id": "TTTC0081R",
                    "query_kind": "all", "session": "regular"})
            assert await runtime.lifecycle.reconcile("sell", evidence)
            state = runtime.owner.state
            assert state["attempts"]["sell"]["state"] == "final_cancelled"
            assert state["attempts"]["sell"]["applied_quantity"] == 1
            assert state["protection"]["states"]["005930"]["pending_filled_qty"] == 1
            assert not await runtime.release_protection_pending("005930")
            assert runtime.owner.state == state
        finally:
            await runtime.shutdown()
            await store.close()
    asyncio.run(scenario())


def test_observed_unapplied_terminal_fill_preserves_first_stage_like_control(tmp_path):
    async def scenario():
        results = {}
        for invoke_release in (False, True):
            directory = tmp_path / ("release" if invoke_release else "control")
            engine, _, store, runtime = await holding(directory)
            try:
                decision = await runtime.quote("005930", Decimal("12000"), intent_id="first-take-profit")
                assert decision[0:2] == ("sell_partial", 10)
                ref = await opened(runtime, "sell", "sell", quantity=10, intent="first-take-profit")
                observation = await observed(runtime, ref, 10, "120000", side="sell", total=10)
                before = runtime.owner.state
                attempt = before["attempts"]["sell"]
                assert (attempt["state"], attempt["observed_quantity"], attempt["applied_quantity"]) == (
                    "final_filled", 10, 0)
                assert before["protection"]["states"]["005930"]["pending_stage"] == "first"
                released = await runtime.release_protection_pending("005930") if invoke_release else None
                receipt = await queued(engine, observation)
                assert receipt.status == "APPLIED"
                assert engine.portfolio.positions["005930"].quantity == 90
                state = runtime.owner.state["protection"]["states"]["005930"]
                results[invoke_release] = (state["current_stage"], released)
            finally:
                await runtime.shutdown()
                await store.close()
        assert results[False][0] == "first"
        assert results[True][0] == "first"
        assert results[True][1] is False
    asyncio.run(scenario())


def test_inflight_quote_prevents_pending_release(tmp_path, monkeypatch):
    async def scenario():
        _, _, store, runtime = await holding(tmp_path)
        reached, release = asyncio.Event(), asyncio.Event()
        caller = None
        try:
            await runtime.quote("005930", Decimal("12000"), intent_id="stop")
            mutate = runtime.owner.mutate
            async def hold(command, reducer):
                if command.startswith("quote-admit:"):
                    reached.set()
                    await release.wait()
                return await mutate(command, reducer)
            monkeypatch.setattr(runtime.owner, "mutate", hold)
            caller = asyncio.create_task(runtime.quote("005930", Decimal("12100")))
            await asyncio.wait_for(reached.wait(), 2)
            version = runtime.owner.version
            assert not await runtime.release_protection_pending("005930")
            assert runtime.owner.version == version
        finally:
            release.set()
            if caller is not None:
                await caller
            await runtime.shutdown()
            await store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("attempt", [False, True])
def test_pending_fill_evidence_conflict_is_not_erased(tmp_path, attempt):
    async def scenario():
        _, _, store, runtime = await holding(tmp_path)
        try:
            await runtime.quote("005930", Decimal("12000"), intent_id="stop")
            if attempt:
                await runtime.lifecycle.prepare("stop", "sell", 10, "005930", "sell")
                await runtime.lifecycle.claim("sell", "sender")
                await runtime.lifecycle.record_result("sell", "sender", CommandResult(CommandStatus.NOT_SENT, "sell"))
            def conflict(state):
                state["protection"]["states"]["005930"]["pending_filled_qty"] = 1
                return state
            await runtime.owner.mutate("synthetic-conflicting-pending-fill", conflict)
            before = runtime.owner.state
            assert not await runtime.release_protection_pending("005930")
            assert runtime.owner.state == before
        finally:
            await runtime.shutdown()
            await store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("damage", ["applied", "unknown", "missing", "side", "symbol", "intent", "negative", "bool", "unlisted", "quantity", "target"])
def test_unproven_or_applied_attempt_never_releases_pending(tmp_path, damage):
    async def scenario():
        _, _, store, runtime = await holding(tmp_path)
        try:
            await runtime.quote("005930", Decimal("12000"), intent_id="stop")
            assert runtime.owner.state["protection"]["states"]["005930"]["pending_stage"] is not None
            await runtime.lifecycle.prepare("stop", "sell", 100, "005930", "sell")
            await runtime.lifecycle.claim("sell", "sender")
            await runtime.lifecycle.record_result("sell", "sender", CommandResult(CommandStatus.NOT_SENT, "sell"))
            def alter(state):
                row = state["attempts"]["sell"]
                if damage == "missing":
                    del state["attempts"]["sell"]
                elif damage == "unlisted":
                    state["attempts"]["unlisted"] = {**row, "attempt_id": "unlisted", "state": "open"}
                elif damage == "target":
                    state["intents"]["stop"]["target_quantity"] = -1
                else:
                    key, value = {"applied": ("applied_quantity", 1), "unknown": ("state", "unknown"),
                        "side": ("side", "buy"), "symbol": ("symbol", "000660"), "intent": ("intent_id", "other"),
                        "negative": ("applied_quantity", -1), "bool": ("applied_quantity", False),
                        "quantity": ("quantity", -1)}[damage]
                    row[key] = value
                return state
            await runtime.owner.mutate("synthetic-damage", alter)
            before, version = runtime.owner.state, runtime.owner.version
            assert not await runtime.release_protection_pending("005930")
            assert runtime.owner.state == before
            assert runtime.owner.version == version
        finally:
            await runtime.shutdown()
            await store.close()
    asyncio.run(scenario())
