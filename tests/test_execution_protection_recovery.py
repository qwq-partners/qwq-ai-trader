"""실제 큐/SQLite 보호 복구: 경제 체결 재적용 없는 durable 증거 계약."""
import asyncio
from copy import deepcopy
from decimal import Decimal

import pytest

from src.execution.safety.runtime import KRExecutionRuntime
from src.execution.safety.application import ApplicationBlocked
from src.execution.safety.store import ExecutionStateStore
from src.execution.safety.protection_recovery import digest, _scope, capture_quote
from src.execution.safety.store import encode_state
from src.strategies.exit_manager import ExitManager
from test_execution_runtime import NOW, setup, opened, observed, queued


def test_degraded_quote_evidence_preserves_original_source_time(tmp_path, monkeypatch):
    async def scenario():
        _, _, store, runtime, _, _ = await failed_entry(tmp_path, monkeypatch)
        try:
            before = runtime.owner.state
            after = deepcopy(before)
            provenance = {"market_as_of": "2026-09-18T09:59:59+09:00", "received_at": NOW.isoformat(),
                          "source": "synthetic-feed", "source_event_id": "market-tick-1"}
            expected = deepcopy(provenance)
            capture_quote(before, after, "005930", price=Decimal("10400"), market_data=None,
                          intent_id=None, command_id="synthetic-quote", decision=None,
                          version=runtime.owner.version + 1, now=NOW, provenance=provenance)
            provenance["market_as_of"] = NOW.isoformat()
            event = after["protection_replay"]["005930"]["events"][-1]
            assert event["provenance"] == expected
            assert event["provenance"]["market_as_of"] != event["applied_at"]
            assert event["digest"] == digest({key: value for key, value in event.items() if key != "digest"})
        finally:
            await runtime.shutdown()
            await store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("legacy_first", [False, True])
def test_degraded_quote_keeps_every_input_without_repeating_full_scope(tmp_path, monkeypatch, legacy_first):
    async def scenario():
        _, exits, store, runtime, _, _ = await failed_entry(tmp_path, monkeypatch)
        try:
            for price in ("10100", "10400", "10200"):
                await runtime.quote("005930", Decimal(price))
            before = runtime.owner.state
            events = before["protection_replay"]["005930"]["events"]
            assert len(events) == 4
            assert [row["price"] for row in events[1:]] == ["10100", "10400", "10200"]
            for row in events[1:]:
                assert row["scope_digest"] == digest(events[0]["after"])
                assert "before" not in row and "after" not in row
                assert len(encode_state(row)) < len(encode_state(events[0]["after"]))
            if legacy_first:
                def legacy(state):
                    history = state["protection_replay"]["005930"]
                    row = history["events"][1]
                    row.pop("scope_digest")
                    row["before"] = deepcopy(history["events"][0]["after"])
                    row["after"] = deepcopy(row["before"])
                    previous = ""
                    for event in history["events"]:
                        event["previous"] = previous
                        event["digest"] = digest({k: v for k, v in event.items() if k != "digest"})
                        previous = event["digest"]
                    history["tail"] = previous
                    return state
                await runtime.owner.mutate("synthetic-legacy-format", legacy)
            receipt = await runtime.repair_protection("compact-repair", "005930", expected_version=runtime.owner.version)
            assert receipt.status == "APPLIED"
            assert exits.get_state("005930").highest_price == Decimal("10400")
            assert runtime.owner.state["portfolio"] == before["portfolio"]
        finally:
            await runtime.shutdown()
            await store.close()
    asyncio.run(scenario())


def test_compact_quote_scope_gap_blocks_even_with_resealed_hash_chain(tmp_path, monkeypatch):
    async def scenario():
        _, _, store, runtime, _, _ = await failed_entry(tmp_path, monkeypatch)
        try:
            await runtime.quote("005930", Decimal("10400"))
            def corrupt(state):
                history = state["protection_replay"]["005930"]
                event = history["events"][-1]
                assert "scope_digest" in event
                event["scope_digest"] = "0" * 64
                event["digest"] = digest({k: v for k, v in event.items() if k != "digest"})
                history["tail"] = event["digest"]
                return state
            await runtime.owner.mutate("synthetic-scope-gap", corrupt)
            receipt = await runtime.repair_protection("gap-repair", "005930", expected_version=runtime.owner.version)
            assert receipt.status == "BLOCKED"
        finally:
            await runtime.shutdown()
            await store.close()
    asyncio.run(scenario())


def test_initial_r_delivery_flag_is_not_protection_input(tmp_path):
    async def scenario():
        engine, _, store, runtime = await setup(tmp_path)
        try:
            ref = await opened(runtime, "B1")
            obs = await observed(runtime, ref, 40, "400000")
            await queued(engine, obs)
            state = runtime.owner.state
            state["lots"][obs.order_key]["initial_r_journal_pending"] = True
            before = digest(_scope(state, "005930"))
            state["lots"][obs.order_key]["initial_r_journal_pending"] = False
            assert digest(_scope(state, "005930")) == before
            state["lots"][obs.order_key]["initial_r"] = "20000"
            assert digest(_scope(state, "005930")) != before
        finally:
            await runtime.shutdown()
            await store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("reopen", [False, True])
@pytest.mark.parametrize("failure", ["apply_precommit", "apply_postcommit", "admit_postcommit", "admit_publication"])
def test_lost_stop_quote_cannot_be_ignored_after_restore(tmp_path, monkeypatch, reopen, failure):
    async def scenario():
        engine, exits, store, runtime, _, _ = await failed_entry(tmp_path, monkeypatch)
        path = store.path
        original = store.commit
        async def fail(expected, state, command):
            target = "command:quote-admit:" if failure == "admit_postcommit" else "command:quote:"
            if command.startswith(target):
                if failure.endswith("postcommit"):
                    await original(expected, state, command)
                raise OSError("synthetic quote persistence failure")
            return await original(expected, state, command)
        try:
            with monkeypatch.context() as patch:
                if failure == "admit_publication":
                    def bad_publish(*args):
                        raise ValueError("synthetic admission publication failure")
                    patch.setattr(runtime.owner, "publisher", bad_publish)
                else:
                    patch.setattr(store, "commit", fail)
                with pytest.raises(ApplicationBlocked if failure == "admit_publication" else OSError):
                    await runtime.quote("005930", Decimal("9000"), intent_id="lost-stop-intent")
            assert not runtime.owner.healthy
            if reopen:
                await store.close()
                store = ExecutionStateStore(path)
                runtime = KRExecutionRuntime(store, engine, exits, clock=lambda: NOW)
            await runtime.restore()
            before = runtime.owner.state
            receipt = await runtime.repair_protection("after-recovery", "005930", expected_version=runtime.owner.version)
            assert receipt.status == "BLOCKED"
            assert receipt.reason in {"unresolved_quote_admission", "unrecorded_historical_decision"}
            assert runtime.owner.state["protection"] == before["protection"]
            assert runtime.owner.state["portfolio"] == before["portfolio"]
            assert runtime.health()["protection_degraded"] == 1
            assert runtime.health()["protection_quote_admissions_pending"] == (0 if failure == "apply_postcommit" else 1)
            assert not runtime.trading_ready
        finally:
            await store.close()
    asyncio.run(scenario())


def test_admitted_quote_view_and_concurrent_repair_are_separate_from_application(tmp_path, monkeypatch):
    async def scenario():
        engine, _, store, runtime, _, _ = await failed_entry(tmp_path, monkeypatch)
        try:
            version = runtime.owner.version
            admitted, release, applying, finish = (asyncio.Event() for _ in range(4))
            original = store.commit
            async def gate(expected, state, command):
                if command.startswith("command:quote-admit:"):
                    result = await original(expected, state, command)
                    admitted.set()
                    await release.wait()
                    return result
                if command.startswith("command:quote:"):
                    applying.set()
                    await finish.wait()
                return await original(expected, state, command)
            monkeypatch.setattr(store, "commit", gate)
            quote = asyncio.create_task(runtime.quote("005930", Decimal("9000"), intent_id="stop"))
            await admitted.wait()
            # durable commit가 끝나도 admission publish/호출 복귀 전에는 view 미게시.
            assert engine.portfolio.positions["005930"].current_price == Decimal("10000")
            repair = asyncio.create_task(runtime.repair_protection("racing-repair", "005930", expected_version=version + 1))
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            release.set()
            receipt = await repair
            assert (receipt.status, receipt.reason) == ("BLOCKED", "unresolved_quote_admission")
            await applying.wait()
            assert engine.portfolio.positions["005930"].current_price == Decimal("9000")
            assert runtime.health()["protection_quote_admissions_pending"] == 1
            finish.set()
            assert await quote is None
            assert runtime.health()["protection_quote_admissions_pending"] == 0
            receipt = await runtime.repair_protection("after-quote", "005930", expected_version=runtime.owner.version)
            assert (receipt.status, receipt.reason) == ("BLOCKED", "unrecorded_historical_decision")
        finally:
            release.set()
            finish.set()
            await runtime.shutdown()
            await store.close()
    asyncio.run(scenario())


def test_failed_admitted_quote_cannot_be_overwritten_by_later_price(tmp_path, monkeypatch):
    async def scenario():
        _, _, store, runtime, _, _ = await failed_entry(tmp_path, monkeypatch)
        try:
            original = store.commit
            async def fail(expected, state, command):
                if command.startswith("command:quote:"):
                    raise OSError("synthetic failed stop application")
                return await original(expected, state, command)
            with monkeypatch.context() as patch:
                patch.setattr(store, "commit", fail)
                with pytest.raises(OSError):
                    await runtime.quote("005930", Decimal("9000"), intent_id="first-stop")
            await runtime.restore()
            evidence = runtime.owner.state["protection_quote_admissions"]
            assert len(evidence) == 1
            assert next(iter(evidence.values()))["price"] == "9000"
            with pytest.raises(ApplicationBlocked):
                await runtime.quote("005930", Decimal("10400"))
            assert runtime.owner.state["protection_quote_admissions"] == evidence
            assert runtime._quotes["005930"] == Decimal("9000")
        finally:
            await store.close()
    asyncio.run(scenario())


def test_unwritable_quote_is_not_accepted_or_published(tmp_path, monkeypatch):
    async def scenario():
        engine, _, store, runtime, _, _ = await failed_entry(tmp_path, monkeypatch)
        try:
            old_price = engine.portfolio.positions["005930"].current_price
            before = runtime.owner.state
            async def fail(*args):
                raise OSError("synthetic total persistence failure")
            with monkeypatch.context() as patch:
                patch.setattr(store, "commit", fail)
                with pytest.raises(OSError):
                    await runtime.quote("005930", Decimal("9000"), intent_id="unaccepted-stop")
            assert engine.portfolio.positions["005930"].current_price == old_price
            assert "005930" not in runtime._quotes
            await runtime.restore()
            assert runtime.owner.state == before
        finally:
            await store.close()
    asyncio.run(scenario())


async def failed_entry(tmp_path, monkeypatch):
    engine, exits, store, runtime = await setup(tmp_path)
    ref = await opened(runtime, "B1")
    obs = await observed(runtime, ref, 40, "400000")
    def fail(*args, **kwargs):
        raise ValueError("synthetic registration failure")
    with monkeypatch.context() as patch:
        patch.setattr(ExitManager, "register_position", fail)
        receipt = await queued(engine, obs)
    assert (receipt.status, receipt.protection_status) == ("APPLIED", "degraded")
    return engine, exits, store, runtime, ref, obs


def test_queue_failed_registration_duplicate_repair_and_reopen(tmp_path, monkeypatch):
    async def scenario():
        engine, exits, store, runtime, ref, obs = await failed_entry(tmp_path, monkeypatch)
        try:
            assert (await queued(engine, obs)).status == "ALREADY_APPLIED"
            second = await observed(runtime, ref, 100, "1000000")
            assert (await queued(engine, second)).protection_status == "degraded"
            before = runtime.owner.state
            version = runtime.owner.version
            receipt = await runtime.repair_protection("repair-1", "005930", expected_version=version)
            assert receipt.status == "APPLIED"
            assert exits.get_state("005930").remaining_quantity == 100
            assert engine.portfolio.cash == Decimal("999859")
            after = runtime.owner.state
            for key in before.keys() - {"protection", "cursors", "recovery_receipts"}:
                assert after[key] == before[key]
            assert after["cursors"][second.order_key]["protection_status"] == "ready"
            assert (await runtime.repair_protection("repair-1", "005930", expected_version=version)).status == "ALREADY_APPLIED"
            assert not runtime.trading_ready
            path = store.path
        finally:
            await store.close()
        reopened = ExecutionStateStore(path)
        try:
            restored = KRExecutionRuntime(reopened, engine, exits, clock=lambda: NOW)
            await restored.restore()
            assert (await restored.repair_protection("repair-1", "005930", expected_version=version)).status == "ALREADY_APPLIED"
            assert restored.owner.state["portfolio"] == before["portfolio"]
        finally:
            await reopened.close()
    asyncio.run(scenario())


def test_concurrent_same_request_has_one_applied_receipt(tmp_path, monkeypatch):
    async def scenario():
        _, _, store, runtime, _, _ = await failed_entry(tmp_path, monkeypatch)
        try:
            version = runtime.owner.version
            entered, release = asyncio.Event(), asyncio.Event()
            original = store.commit
            async def commit(expected, state, command):
                if command.startswith("command:protection-repair:"):
                    entered.set()
                    await release.wait()
                return await original(expected, state, command)
            monkeypatch.setattr(store, "commit", commit)
            first = asyncio.create_task(runtime.repair_protection("repair", "005930", expected_version=version))
            await entered.wait()
            second = asyncio.create_task(runtime.repair_protection("repair", "005930", expected_version=version))
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            release.set()
            receipts = await asyncio.gather(first, second)
            assert [receipt.status for receipt in receipts] == ["APPLIED", "ALREADY_APPLIED"]
            assert runtime.owner.version == version + 1
        finally:
            await store.close()
    asyncio.run(scenario())


def test_cancelled_repair_waiter_still_commits_and_drains(tmp_path, monkeypatch):
    async def scenario():
        _, exits, store, runtime, _, _ = await failed_entry(tmp_path, monkeypatch)
        try:
            version = runtime.owner.version
            entered, release = asyncio.Event(), asyncio.Event()
            original = store.commit
            async def commit(expected, state, command):
                entered.set()
                await release.wait()
                return await original(expected, state, command)
            monkeypatch.setattr(store, "commit", commit)
            caller = asyncio.create_task(runtime.repair_protection("repair", "005930", expected_version=version))
            await entered.wait()
            caller.cancel()
            with pytest.raises(asyncio.CancelledError):
                await caller
            release.set()
            await runtime.shutdown()
            assert exits.get_state("005930").remaining_quantity == 40
            assert runtime.owner.state["recovery_receipts"]["repair"]["status"] == "APPLIED"
            assert runtime.health()["protection_updates_pending"] == 0
            assert not runtime.trading_ready
        finally:
            await store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["database", "publication"])
def test_repair_failure_keeps_publication_barrier_until_restore(tmp_path, monkeypatch, failure):
    async def scenario():
        _, exits, store, runtime, _, _ = await failed_entry(tmp_path, monkeypatch)
        try:
            version = runtime.owner.version
            before = runtime.owner.state
            with monkeypatch.context() as patch:
                if failure == "database":
                    async def fail(*args):
                        raise OSError("synthetic database failure")
                    patch.setattr(store, "commit", fail)
                    expected_error = OSError
                else:
                    def fail(*args):
                        raise ValueError("synthetic publication failure")
                    patch.setattr(runtime.owner, "publisher", fail)
                    expected_error = ApplicationBlocked
                with pytest.raises(expected_error):
                    await runtime.repair_protection("repair", "005930", expected_version=version)
                assert runtime.owner.publication_recovery_required
                with pytest.raises(ApplicationBlocked):
                    await runtime.repair_protection("retry", "005930", expected_version=version)
            await runtime.restore()
            receipt = await runtime.repair_protection("repair", "005930", expected_version=version)
            assert receipt.status == ("APPLIED" if failure == "database" else "ALREADY_APPLIED")
            assert exits.get_state("005930").remaining_quantity == 40
            assert runtime.owner.state["portfolio"] == before["portfolio"]
            assert not runtime.trading_ready
        finally:
            await store.close()
    asyncio.run(scenario())


def test_repair_preserves_healthy_stage_be_pending_initial_r_and_other_symbol(tmp_path, monkeypatch):
    async def scenario():
        engine, exits, store, runtime = await setup(tmp_path)
        try:
            ref = await opened(runtime, "B1")
            await queued(engine, await observed(runtime, ref, 40, "400000"))
            await opened(runtime, "S1", "sell", quantity=10)
            def anchor(state):
                row = state["protection"]["states"]["005930"]
                row.update(current_stage="second", highest_price="12000", breakeven_activated=True,
                           initial_risk_amount="20000", actual_stop_pct="5", pending_stage="third",
                           pending_since=NOW.isoformat(), pending_target_qty=10, pending_filled_qty=0)
                state["protection"]["pending_owners"]["005930"] = "S1"
                state["protection"]["exit_exempt"].append("OTHER")
                return state
            await runtime.owner.mutate("synthetic-anchor", anchor)
            second = await observed(runtime, ref, 100, "1000000")
            def fail(*args):
                raise ValueError("synthetic policy calculation failure")
            with monkeypatch.context() as patch:
                patch.setattr("src.execution.safety.protection._registration", fail)
                assert (await queued(engine, second)).protection_status == "degraded"
            await runtime.quote("005930", Decimal("12500"))
            before = runtime.owner.state
            receipt = await runtime.repair_protection("repair", "005930", expected_version=runtime.owner.version)
            assert receipt.status == "APPLIED"
            row = runtime.owner.state["protection"]["states"]["005930"]
            for key, value in {"current_stage": "second", "highest_price": "12500", "breakeven_activated": True,
                               "initial_risk_amount": "20000", "actual_stop_pct": "5", "pending_stage": "third",
                               "pending_target_qty": 10, "pending_filled_qty": 0, "remaining_quantity": 100}.items():
                assert row[key] == value
            for key in ("portfolio", "risk", "attempts", "intents", "lots", "outbox", "startup_reconciliation"):
                assert runtime.owner.state[key] == before[key]
            assert runtime.owner.state["protection"]["exit_exempt"] == ["OTHER"]
        finally:
            await store.close()
    asyncio.run(scenario())


def test_journal_ack_does_not_invalidate_protection_evidence(tmp_path, monkeypatch):
    async def scenario():
        _, _, store, runtime, _, obs = await failed_entry(tmp_path, monkeypatch)
        try:
            def ack(state):
                state["cursors"][obs.order_key]["journal_pending"] = False
                return state
            await runtime.owner.mutate("synthetic-journal-ack", ack)
            receipt = await runtime.repair_protection("repair", "005930", expected_version=runtime.owner.version)
            assert receipt.status == "APPLIED"
            assert not runtime.owner.state["cursors"][obs.order_key]["journal_pending"]
        finally:
            await store.close()
    asyncio.run(scenario())


def test_closed_lifecycle_history_and_old_receipt_are_not_repaired(tmp_path, monkeypatch):
    async def scenario():
        engine, exits, store, runtime, _, old = await failed_entry(tmp_path, monkeypatch)
        try:
            sell = await opened(runtime, "S1", "sell", quantity=40)
            sale = await observed(runtime, sell, 40, "400000", side="sell", total=40)
            assert (await queued(engine, sale)).protection_status == "ready"
            assert exits.get_state("005930") is None
            next_ref = await opened(runtime, "B2", quantity=10)
            next_obs = await observed(runtime, next_ref, 10, "100000", total=10)
            def fail(*args, **kwargs):
                raise ValueError("synthetic new lifecycle registration failure")
            with monkeypatch.context() as patch:
                patch.setattr(ExitManager, "register_position", fail)
                assert (await queued(engine, next_obs)).protection_status == "degraded"
            before = runtime.owner.state
            receipt = await runtime.repair_protection("repair-new", "005930", expected_version=runtime.owner.version)
            assert receipt.status == "APPLIED"
            assert exits.get_state("005930").remaining_quantity == 10
            assert runtime.owner.state["cursors"][old.order_key] == before["cursors"][old.order_key]
            assert runtime.owner.state["cursors"][old.order_key]["protection_status"] == "degraded"
            assert runtime.owner.state["cursors"][next_obs.order_key]["protection_status"] == "ready"
            assert runtime.owner.state["lots"] == before["lots"]
            assert runtime.owner.state["protection_replay"] == before["protection_replay"]
        finally:
            await store.close()
    asyncio.run(scenario())


def test_repeated_replay_calculation_failure_keeps_degraded_state(tmp_path, monkeypatch):
    async def scenario():
        _, _, store, runtime, _, _ = await failed_entry(tmp_path, monkeypatch)
        try:
            before = runtime.owner.state
            def fail(*args, **kwargs):
                raise ValueError("synthetic continuing registration failure")
            with monkeypatch.context() as patch:
                patch.setattr(ExitManager, "register_position", fail)
                receipt = await runtime.repair_protection("repair", "005930", expected_version=runtime.owner.version)
            assert (receipt.status, receipt.reason) == ("BLOCKED", "protection_replay_failed")
            for key in before:
                assert runtime.owner.state[key] == before[key]
            # 새로운 복구 시도는 새 operation ID와 최신 version이 필요하다.
            receipt = await runtime.repair_protection("repair-2", "005930", expected_version=runtime.owner.version)
            assert receipt.status == "APPLIED"
        finally:
            await store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("damage", ["drop_root", "rewrite_anchor", "drop_event"])
def test_fill_write_set_rejects_evidence_erasure(tmp_path, monkeypatch, damage):
    async def scenario():
        engine, _, store, runtime, ref, _ = await failed_entry(tmp_path, monkeypatch)
        try:
            second = await observed(runtime, ref, 100, "1000000")
            original = runtime.owner.reducer
            def corrupt(state, observation, delta):
                reduction = original(state, observation, delta)
                candidate = reduction.state
                if damage == "drop_root":
                    candidate.pop("protection_replay")
                elif damage == "rewrite_anchor":
                    candidate["protection_replay"]["005930"]["events"][0]["before"]["position"] = {}
                else:
                    candidate["protection_replay"]["005930"]["events"].pop(0)
                return reduction
            monkeypatch.setattr(runtime.owner, "reducer", corrupt)
            before = runtime.owner.state
            assert (await queued(engine, second)).status == "FAILED"
            assert runtime.owner.state["portfolio"] == before["portfolio"]
            assert runtime.owner.state["protection_replay"] == before["protection_replay"]
            assert engine.portfolio.positions["005930"].quantity == 40
        finally:
            await store.close()
    asyncio.run(scenario())


def test_healthy_quotes_do_not_accumulate_recovery_history(tmp_path):
    async def scenario():
        engine, _, store, runtime = await setup(tmp_path)
        try:
            ref = await opened(runtime, "B1")
            await queued(engine, await observed(runtime, ref, 40, "400000"))
            await runtime.quote("005930", Decimal("10100"))
            assert not runtime.owner.state.get("protection_replay")
        finally:
            await store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("price, expected", [("10400", "APPLIED"), ("9000", "BLOCKED"), ("12000", "BLOCKED")])
def test_degraded_quote_replay_preserves_high_but_never_creates_past_order(tmp_path, monkeypatch, price, expected):
    async def scenario():
        engine, exits, store, runtime, _, _ = await failed_entry(tmp_path, monkeypatch)
        try:
            await runtime.quote("005930", Decimal(price), intent_id="missing-sell-intent")
            before = runtime.owner.state
            receipt = await runtime.repair_protection("repair", "005930", expected_version=runtime.owner.version)
            assert receipt.status == expected
            assert runtime.owner.state["outbox"] == before["outbox"]
            assert runtime.owner.state["portfolio"] == before["portfolio"]
            if expected == "APPLIED":
                assert exits.get_state("005930").highest_price == Decimal("10400")
            else:
                assert runtime.owner.state["protection"] == before["protection"]
        finally:
            await store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("change", ["missing_history", "missing_quote", "policy", "lifecycle", "empty_holding_anchor", "unknown_pending"])
def test_missing_or_conflicting_evidence_is_blocked_without_economic_changes(tmp_path, monkeypatch, change):
    async def scenario():
        _, _, store, runtime, _, _ = await failed_entry(tmp_path, monkeypatch)
        try:
            await runtime.quote("005930", Decimal("10400"))
            def corrupt(state):
                from src.execution.safety.protection_recovery import digest
                history = state["protection_replay"]["005930"]
                if change == "missing_history":
                    state.pop("protection_replay")
                elif change == "missing_quote":
                    history["events"].pop()
                elif change == "policy":
                    state["protection"]["config"]["stop_loss_pct"] = 6.0
                elif change == "lifecycle":
                    next(iter(state["lots"].values()))["lifecycle_id"] = "different-lifecycle"
                else:
                    event = history["events"][0]
                    if change == "empty_holding_anchor":
                        event["before"]["position"] = deepcopy(event["after"]["position"])
                    else:
                        event["before"]["protection"]["pending_owners"]["005930"] = "unknown"
                    previous = ""
                    for row in history["events"]:
                        row["previous"] = previous
                        row["digest"] = digest({k: v for k, v in row.items() if k != "digest"})
                        previous = row["digest"]
                    history["tail"] = previous
                return state
            await runtime.owner.mutate("synthetic-corruption", corrupt)
            before = runtime.owner.state
            receipt = await runtime.repair_protection("repair", "005930", expected_version=runtime.owner.version)
            assert receipt.status == "BLOCKED"
            for key in before:
                assert runtime.owner.state[key] == before[key]
        finally:
            await store.close()
    asyncio.run(scenario())


def test_repair_version_is_checked_after_owner_lock_and_id_conflicts(tmp_path, monkeypatch):
    async def scenario():
        _, _, store, runtime, _, _ = await failed_entry(tmp_path, monkeypatch)
        try:
            version = runtime.owner.version
            entered, release = asyncio.Event(), asyncio.Event()
            original = store.lookup_commit
            async def lookup(commit_id):
                if commit_id == "command:earlier":
                    entered.set()
                    await release.wait()
                return await original(commit_id)
            monkeypatch.setattr(store, "lookup_commit", lookup)
            earlier = asyncio.create_task(runtime.owner.mutate("earlier", lambda state: state))
            await entered.wait()
            repair = asyncio.create_task(runtime.repair_protection("repair", "005930", expected_version=version))
            await asyncio.sleep(0)
            release.set()
            await earlier
            receipt = await repair
            assert (receipt.status, receipt.reason) == ("BLOCKED", "stale_execution_version")
            with pytest.raises(ValueError, match="operation_id_conflict"):
                await runtime.repair_protection("repair", "005930", expected_version=runtime.owner.version)
        finally:
            await store.close()
    asyncio.run(scenario())
