"""초기 R 순수 reducer 계약. 최종성 fixture는 공식 KIS 지원 증명이 아니다."""
import asyncio
from copy import deepcopy
from dataclasses import replace
from decimal import Decimal

import pytest

from src.execution.safety import initial_r
from src.execution.safety.application import FillObservation
from src.execution.safety.lifecycle import OrderEvidence, OrderState
from test_execution_runtime import NOW, setup, opened, observed, queued


def finality(ref, quantity=100, amount="1000000"):
    return OrderEvidence(ref, "005930", "buy", quantity, quantity, Decimal(amount), 0, 0,
                         OrderState.FINAL_FILLED, complete=True, supported_finality=True,
                         source_contract="synthetic-test", observed_at=NOW, request_started_at=NOW,
                         query_scope={"account_scope": "scope", "market": "KR", "exchange": "KRX",
                                      "start_date": "2026-09-18", "end_date": "2026-09-18",
                                      "tr_id": "TTTC0081R", "query_kind": "all", "session": "regular"})


async def candidate(tmp_path, *, quantity=100, amount="1000000", stop=5, partial=False):
    engine, exits, store, runtime = await setup(tmp_path)
    ref = await opened(runtime, "B1", quantity=quantity)
    metadata = {"registration_params": {"stop_loss_pct": stop, "atr_pct_hint": 9}}
    first = await observed(runtime, ref, 40 if partial else quantity,
                           "400000" if partial else amount, total=quantity, metadata=metadata)
    before = runtime.owner.state
    await queued(engine, first)
    state = runtime.owner.state
    initial_r.capture_initial_stop(before, state, first, fill_kind="initial_entry", status="ready",
                                   version=runtime.owner.version, now=NOW)
    if partial:
        initial = deepcopy(state["initial_stop_evidence"])
        second = await observed(runtime, ref, quantity, amount, total=quantity, metadata=metadata)
        await queued(engine, second)
        state = runtime.owner.state
        state["initial_stop_evidence"] = initial
    initial_r.capture_finality(state, "B1", finality(ref, quantity, amount),
                               version=runtime.owner.version, now=NOW)
    return engine, exits, store, runtime, ref, state


def finalize(state, ref, *, operation="r1", version=30, expected=30):
    return initial_r.reduce_finalize_initial_r(
        state, operation, ref.key, initial_stop_evidence_id=state["initial_stop_evidence"][ref.key]["evidence_id"],
        finality_evidence_id=state["attempts"]["B1"]["accepted_finality_evidence_id"],
        expected_version=expected, state_version=version, now=NOW)


def test_superseded_late_observation_does_not_block_initial_r(tmp_path):
    async def run():
        engine, _, store, runtime, ref, state = await candidate(tmp_path)
        try:
            await runtime.owner.mutate("synthetic-initial-r-evidence", lambda _: state)
            first = next(iter(state["inbox"].values()))["observation"]
            stale = FillObservation(**{**first, "cumulative_quantity": 20, "cumulative_amount": "200000"})
            assert (await queued(engine, stale)).status == "ALREADY_APPLIED"
            assert runtime.owner.state["inbox"][stale.observation_id]["status"] == "SUPERSEDED"
            result = finalize(runtime.owner.state, ref, version=runtime.owner.version,
                              expected=runtime.owner.version)
            assert result["recovery_receipts"]["r1"]["status"] == "APPLIED"
            assert Decimal(result["lots"][ref.key]["initial_r"]) == Decimal("50000")
        finally:
            await runtime.shutdown()
            await store.close()
    asyncio.run(run())


@pytest.mark.parametrize(("quantity", "amount", "want"), [(100, "1000000", "50000"),
                                                          (140, "1400001", "70000.05")])
def test_final_amount_not_average_or_current_config_defines_r(tmp_path, quantity, amount, want):
    async def run():
        _, _, store, _, ref, state = await candidate(tmp_path, quantity=quantity, amount=amount, partial=True)
        try:
            proof = deepcopy(state["initial_stop_evidence"])
            state["protection"]["config"]["stop_loss_pct"] = 7
            state["protection"]["intraday_crash_level"] = "severe"
            domains = {key: deepcopy(state[key]) for key in ("portfolio", "risk", "attempts", "cursors", "inbox")}
            original_outbox = deepcopy(state["outbox"])
            result = finalize(state, ref)
            assert result["recovery_receipts"]["r1"]["status"] == "APPLIED"
            assert Decimal(result["lots"][ref.key]["initial_r"]) == Decimal(want)
            assert result["lots"][ref.key]["initial_r_journal_pending"]
            assert Decimal(result["protection"]["states"]["005930"]["initial_risk_amount"]) == Decimal(want)
            assert result["initial_stop_evidence"] == proof
            assert {key: result[key] for key in domains} == domains
            assert {key: result["outbox"][key] for key in original_outbox} == original_outbox
            assert len(result["outbox"]) == len(original_outbox) + 1
            again = deepcopy(result)
            assert finalize(result, ref) == again
            with pytest.raises(ValueError, match="conflict"):
                finalize(result, ref, expected=31)
        finally:
            await store.close()
    asyncio.run(run())


@pytest.mark.parametrize("fault", ["stale", "unapplied", "conflict", "reserve", "child", "inbox",
                                  "degraded", "existing_r", "addon", "missing_stop", "terminal_only",
                                  "sibling_reserve", "first_fill_missing"])
def test_ambiguous_or_unproven_initial_r_is_blocked_without_economic_mutation(tmp_path, fault):
    async def run():
        _, _, store, _, ref, state = await candidate(tmp_path)
        try:
            if fault == "unapplied": state["attempts"]["B1"]["applied_quantity"] = 40
            if fault == "conflict": state["attempts"]["B1"]["evidence_conflict"] = True
            if fault == "reserve": state["attempts"]["B1"]["reserved_quantity"] = 1
            if fault == "child":
                state["attempts"]["C1"] = {"kind": "cancel", "intent_id": "B1", "parent_attempt_id": "B1", "command_status": "unknown"}
            if fault == "inbox":
                row = deepcopy(next(iter(state["inbox"].values())))
                row["status"] = "RECEIVED"
                state["inbox"]["unknown"] = row
            if fault == "degraded": state["protection"]["degraded"]["005930"] = {"quantity": 100, "reason": "failure"}
            if fault == "existing_r": state["lots"][ref.key]["initial_r"] = "1"
            if fault == "addon":
                state["lots"]["extra"] = {**deepcopy(state["lots"][ref.key]), "kind": "distinct_add_on", "lifecycle_id": None}
            if fault == "missing_stop": state["initial_stop_evidence"][ref.key]["status"] = "unmeasured"
            if fault == "terminal_only": state["accepted_finality_evidence"] = {}
            if fault == "sibling_reserve":
                state["attempts"]["B2"] = {**deepcopy(state["attempts"]["B1"]), "attempt_id": "B2", "reserved_quantity": 1}
                state["intents"]["B1"]["attempt_ids"].append("B2")
            if fault == "first_fill_missing": state["inbox"] = {}
            before = deepcopy(state)
            result = finalize(state, ref, expected=29 if fault == "stale" else 30)
            assert result["recovery_receipts"]["r1"]["status"] == "BLOCKED"
            result.pop("recovery_receipts")
            assert result == before
        finally:
            await store.close()
    asyncio.run(run())


def test_registration_evidence_is_append_only_and_atr_hint_is_not_dynamic_stop(tmp_path):
    async def run():
        engine, _, store, runtime = await setup(tmp_path)
        try:
            ref = await opened(runtime, "B1")
            obs = await observed(runtime, ref, 40, "400000", metadata={"registration_params": {"stop_loss_pct": 5, "atr_pct_hint": 9}})
            before = runtime.owner.state
            await queued(engine, obs)
            state = runtime.owner.state
            initial_r.capture_initial_stop(before, state, obs, fill_kind="initial_entry", status="ready", version=runtime.owner.version, now=NOW)
            proof = state["initial_stop_evidence"][ref.key]
            assert Decimal(proof["stop_pct"]) == Decimal("5")
            assert proof["resolve_inputs"]["dynamic_stop_pct"] is None
            initial_r.validate_initial_stop_write_set(before, state, obs)
            copied = deepcopy(state)
            copied["initial_stop_evidence"][ref.key]["stop_pct"] = "9"
            with pytest.raises(ValueError): initial_r.validate_initial_stop_write_set(state, copied, obs)
            copied = deepcopy(state)
            copied["initial_stop_evidence"][ref.key]["crash_capped"] = 0
            with pytest.raises(ValueError): initial_r.validate_initial_stop_write_set(state, copied, obs)
            failed = deepcopy(before)
            initial_r.capture_initial_stop(before, failed, obs, fill_kind="initial_entry", status="degraded", version=10, now=NOW)
            assert "initial_stop_evidence" not in failed
        finally:
            await store.close()
    asyncio.run(run())


@pytest.mark.parametrize("changes", [{"supported_finality": False}, {"complete": False}, {"query_scope": {}},
                                    {"remaining_quantity": 1}, {"state": OrderState.FINAL_EXPIRED}])
def test_finality_capture_cannot_upgrade_partial_or_unsupported_evidence(tmp_path, changes):
    async def run():
        _, _, store, _, ref, state = await candidate(tmp_path)
        try:
            before = deepcopy(state)
            with pytest.raises(ValueError):
                initial_r.capture_finality(state, "B1", replace(finality(ref), **changes), version=30, now=NOW)
            assert state == before
        finally:
            await store.close()
    asyncio.run(run())


@pytest.mark.parametrize("closed", [False, True])
def test_sales_do_not_reduce_historical_r_or_overwrite_a_new_lifecycle(tmp_path, closed):
    async def run():
        engine, _, store, runtime, ref, state = await candidate(tmp_path)
        try:
            await runtime.owner.mutate("synthetic-stop-evidence", lambda _: state)
            sale = await opened(runtime, "S1", "sell", quantity=100 if closed else 40)
            await queued(engine, await observed(runtime, sale, 100 if closed else 40,
                                                "1100000" if closed else "440000", side="sell", total=100 if closed else 40))
            if closed:
                new = await opened(runtime, "B2")
                await queued(engine, await observed(runtime, new, 100, "1000000"))
            before = runtime.owner.state
            protected = deepcopy(before["protection"])
            result = finalize(before, ref, version=runtime.owner.version, expected=runtime.owner.version)
            assert result["recovery_receipts"]["r1"]["status"] == "APPLIED"
            assert Decimal(result["lots"][ref.key]["initial_r"]) == Decimal("50000")
            if closed:
                assert result["protection"] == protected
                assert result["lots"][new.key]["initial_r"] is None
            else:
                assert result["protection"]["states"]["005930"]["remaining_quantity"] == 60
                assert Decimal(result["protection"]["states"]["005930"]["initial_risk_amount"]) == Decimal("50000")
        finally:
            await store.close()
    asyncio.run(run())


def test_uncapped_crash_flag_and_invalid_stop_unmeasured_do_not_rollback_economics(tmp_path):
    async def run():
        for stop in (5, None, 0):
            engine, _, store, runtime = await setup(tmp_path / str(stop))
            try:
                def crash(state):
                    state["protection"]["intraday_crash_level"] = "severe"
                    state["protection"]["config"]["stop_loss_pct"] = 7
                    return state
                await runtime.owner.mutate("synthetic-crash", crash)
                ref = await opened(runtime, "B1")
                obs = await observed(runtime, ref, 100, "1000000", metadata={"registration_params": {"stop_loss_pct": stop}})
                before = runtime.owner.state
                assert (await queued(engine, obs)).status == "APPLIED"
                state = runtime.owner.state
                initial_r.capture_initial_stop(before, state, obs, fill_kind="initial_entry", status="ready", version=runtime.owner.version, now=NOW)
                proof = state["initial_stop_evidence"][ref.key]
                if stop != 0:
                    assert Decimal(proof["stop_pct"]) == Decimal(7 if stop is None else 5)
                    assert proof["crash_capped"] is True
                    assert proof["protection_snapshot"]["states"]["005930"]["stop_loss_pct"] == 2
                    assert proof["resolve_inputs"]["fixed_stop_pct"] == stop
                else:
                    assert proof["status"] == "unmeasured"
                assert state["portfolio"]["positions"]["005930"]["quantity"] == 100
            finally:
                await store.close()
    asyncio.run(run())


def test_runtime_queue_captures_first_stop_and_finality_then_finalizes_once(tmp_path):
    """부모 runtime/lifecycle/application 훅 배선 전에는 의도된 통합 RED다."""
    async def run():
        engine, exits, store, runtime = await setup(tmp_path)
        try:
            ref = await opened(runtime, "B1", quantity=140)
            metadata = {"registration_params": {"stop_loss_pct": 5}}
            first = await observed(runtime, ref, 40, "400000", total=140, metadata=metadata)
            await queued(engine, first)
            stop = deepcopy(runtime.owner.state["initial_stop_evidence"][ref.key])
            def changed_policy(state):
                state["protection"]["config"]["stop_loss_pct"] = 8
                state["protection"]["current_regime"] = "trending_bear"
                state["protection"]["intraday_crash_level"] = "severe"
                return state
            await runtime.owner.mutate("synthetic-policy-change", changed_policy)
            await queued(engine, await observed(runtime, ref, 140, "1400001", total=140, metadata=metadata))
            version = runtime.owner.version
            finality_id = runtime.owner.state["attempts"]["B1"]["accepted_finality_evidence_id"]
            receipt = await runtime.finalize_initial_r("confirmed-r", ref.key, expected_version=version,
                                                       initial_stop_evidence_id=stop["evidence_id"],
                                                       finality_evidence_id=finality_id)
            assert receipt.status == "APPLIED"
            assert exits.get_state("005930").initial_risk_amount == Decimal("70000.05")
            assert runtime.owner.state["initial_stop_evidence"][ref.key] == stop
            economic = deepcopy(runtime.owner.state["portfolio"])
            await runtime.restore()
            replay = await runtime.finalize_initial_r("confirmed-r", ref.key, expected_version=version,
                                                       initial_stop_evidence_id=stop["evidence_id"],
                                                       finality_evidence_id=finality_id)
            assert replay == receipt
            assert runtime.owner.state["portfolio"] == economic
        finally:
            await store.close()
    asyncio.run(run())
