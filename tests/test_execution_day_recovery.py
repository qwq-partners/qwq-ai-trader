"""실제 큐/SQLite의 일자 경계와 durable 사실 접수 계약."""
import asyncio
from copy import deepcopy
from datetime import timedelta
from decimal import Decimal

import pytest

from src.execution.safety.application import ApplicationBlocked
from tests.test_execution_runtime import setup, opened, observed, queued, wait_queued, NOW


async def current_fill(runtime, order_id, *, quantity=1, amount="10800"):
    from src.execution.safety.application import FillObservation
    from src.execution.safety.lifecycle import OrderRef, CommandResult, CommandStatus, OrderEvidence, OrderState
    now = runtime._now()
    day = now.date().isoformat()
    ref = OrderRef("scope", "KR", day, "KRX", order_id)
    await runtime.lifecycle.prepare(order_id, order_id, quantity, "005930", "buy", reserved_cash=amount, strategy="sepa_trend")
    await runtime.lifecycle.claim(order_id, "sender")
    await runtime.lifecycle.record_result(order_id, "sender", CommandResult(CommandStatus.ACKNOWLEDGED, order_id, ref))
    evidence = OrderEvidence(ref, "005930", "buy", quantity, quantity, Decimal(amount), 0, 0, OrderState.FINAL_FILLED,
        complete=True, supported_finality=True, source_contract="synthetic-test", observed_at=now,
        request_started_at=now-timedelta(seconds=1), query_scope={"account_scope": "scope", "market": "KR", "exchange": "KRX",
        "start_date": day, "end_date": day, "tr_id": "TTTC0081R", "query_kind": "all", "session": "regular"})
    assert await runtime.lifecycle.reconcile(order_id, evidence)
    return FillObservation("scope", "KR", day, "KRX", order_id, "005930", "BUY", quantity, Decimal(amount))


async def quote_day_setup(tmp_path):
    from src.execution.safety.day_recovery import ValuationPrice
    engine, exits, store, runtime, times = await day_setup(tmp_path)
    ref = await opened(runtime, "B1")
    await queued(engine, await observed(runtime, ref, 100, "1000000"))
    fence = await prepare(runtime, times)
    eid = await valued(runtime, fence, times, [ValuationPrice("005930", Decimal("10500"), times[0], "fixture", "boundary")])
    assert (await runtime.rollover_day("roll", expected_version=runtime.owner.version, fence_id=fence.fence_id, valuation_evidence_id=eid)).status == "APPLIED"
    assert (await runtime.resume_after_rollover("resume", expected_version=runtime.owner.version, fence_id=fence.fence_id)).status == "APPLIED"
    times[0] += timedelta(minutes=5)
    return engine, exits, store, runtime, times


async def reopen_quote_runtime(engine, exits, store, times, tmp_path):
    from src.execution.safety.runtime import KRExecutionRuntime
    from src.execution.safety.store import ExecutionStateStore
    await store.close()
    reopened = ExecutionStateStore(tmp_path / "execution" / "state.sqlite3")
    restored = KRExecutionRuntime(reopened, engine, exits, clock=lambda: times[0], account_scope="scope")
    await restored.restore()
    return reopened, restored


@pytest.mark.parametrize("provenance", ["explicit", "unknown"])
def test_same_boundary_quote_after_rollover_uses_admission_order(tmp_path, provenance):
    async def scenario():
        engine, _, store, runtime, times = await quote_day_setup(tmp_path)
        release, entered = asyncio.Event(), asyncio.Event()
        caller = None
        try:
            times[0] -= timedelta(minutes=5)
            original = store.commit
            async def gate(version, state, command):
                if command.startswith("command:quote:"):
                    entered.set()
                    await release.wait()
                return await original(version, state, command)
            store.commit = gate
            metadata = {"market_as_of": times[0], "source": "fixture", "source_event_id": "same-boundary"} if provenance == "explicit" else {}
            caller = asyncio.create_task(runtime.quote("005930", Decimal("10400"), **metadata))
            await asyncio.wait_for(entered.wait(), 2)
            during = engine.portfolio.positions["005930"].current_price
            release.set()
            await caller
            assert (during, engine.portfolio.positions["005930"].current_price) == (Decimal("10400"), Decimal("10400"))
            await runtime.restore()
            assert engine.portfolio.positions["005930"].current_price == Decimal("10400")
        finally:
            release.set()
            if caller is not None:
                await asyncio.gather(caller, return_exceptions=True)
            await engine._shutdown()
            await store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("same_timestamp_cache", [False, True])
def test_quote_admitted_before_rollover_cannot_override_valuation(tmp_path, same_timestamp_cache):
    async def scenario():
        from src.execution.safety.day_recovery import ValuationPrice
        engine, _, store, runtime, times = await day_setup(tmp_path)
        try:
            ref = await opened(runtime, "B1")
            await queued(engine, await observed(runtime, ref, 100, "1000000"))
            await runtime.quote("005930", Decimal("10400"))
            old_version = runtime._quote_metadata["005930"]["source_version"]
            fence = await prepare(runtime, times)
            if same_timestamp_cache:
                # 합성 동시각 cache의 순서 판정만 시험한다. 새 시장 provenance를 만들지 않는다.
                runtime._quote_metadata["005930"]["received_at"] = times[0].isoformat()
            eid = await valued(runtime, fence, times, [ValuationPrice("005930", Decimal("10500"), times[0], "fixture", "boundary")])
            assert (await runtime.rollover_day("roll", expected_version=runtime.owner.version, fence_id=fence.fence_id, valuation_evidence_id=eid)).status == "APPLIED"
            assert old_version < runtime.owner.state["day_valuation_view"]["rollover_version"]
            assert engine.portfolio.positions["005930"].current_price == Decimal("10500")
            await runtime.restore()
            assert engine.portfolio.positions["005930"].current_price == Decimal("10500")
        finally:
            await engine._shutdown()
            await store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("reopen", [False, True])
@pytest.mark.parametrize("unknown_between", [False, True])
@pytest.mark.parametrize("price", ["10400", "9000"])
def test_latest_explicit_quote_rejects_older_market_time_after_completion(tmp_path, reopen, unknown_between, price):
    async def scenario():
        engine, exits, store, runtime, times = await quote_day_setup(tmp_path)
        try:
            as_of = times[0]
            await runtime.quote("005930", Decimal("10500"), market_as_of=as_of, source="fixture", source_event_id="latest")
            times[0] += timedelta(seconds=1)
            if unknown_between:
                await runtime.quote("005930", Decimal("10600"))
            if reopen:
                store, runtime = await reopen_quote_runtime(engine, exits, store, times, tmp_path)
            before = runtime.owner.state
            current = engine.portfolio.positions["005930"].current_price
            version = runtime.owner.version
            with pytest.raises(ApplicationBlocked, match="stale_market_quote"):
                await runtime.quote("005930", Decimal(price), intent_id="old-stop", market_as_of=as_of-timedelta(minutes=1), source="fixture", source_event_id="older")
            assert runtime.owner.state == before and runtime.owner.version == version
            assert engine.portfolio.positions["005930"].current_price == current
            watermark = before["latest_explicit_quote"]["005930"]
            assert watermark["as_of"] == as_of.isoformat()
            assert watermark["received_at"] == as_of.isoformat()
            assert watermark["price"] == "10500"
            assert not runtime.trading_ready
        finally:
            await runtime.shutdown()
            await engine._shutdown()
            await store.close()
    asyncio.run(scenario())


def test_quote_waiting_for_admission_rechecks_newer_explicit_watermark(tmp_path):
    async def scenario():
        engine, _, store, runtime, times = await quote_day_setup(tmp_path)
        release, entered, invoked = asyncio.Event(), asyncio.Event(), asyncio.Event()
        first = second = None
        try:
            original = store.commit
            async def gate(version, state, command):
                if command.startswith("command:quote-admit:") and not entered.is_set():
                    entered.set()
                    await release.wait()
                return await original(version, state, command)
            store.commit = gate
            first = asyncio.create_task(runtime.quote("005930", Decimal("10500"), market_as_of=times[0], source="fixture", source_event_id="newest"))
            await asyncio.wait_for(entered.wait(), 2)
            async def older():
                invoked.set()
                return await runtime.quote("005930", Decimal("9000"), intent_id="old-stop", market_as_of=times[0]-timedelta(minutes=1), source="fixture", source_event_id="older")
            second = asyncio.create_task(older())
            await invoked.wait()
            assert not second.done()
            release.set()
            await first
            with pytest.raises(ApplicationBlocked, match="stale_market_quote"):
                await second
            assert engine.portfolio.positions["005930"].current_price == Decimal("10500")
            assert not any(row.get("kind") == "protection_decision" for row in runtime.owner.state["outbox"].values())
            assert not runtime._protection_failed
        finally:
            release.set()
            await asyncio.gather(*(task for task in (first, second) if task), return_exceptions=True)
            await engine._shutdown()
            await store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["admit_precommit", "admit_postcommit", "apply_precommit", "apply_postcommit"])
def test_quote_watermark_and_unresolved_input_have_atomic_durability(tmp_path, failure):
    async def scenario():
        engine, exits, store, runtime, times = await quote_day_setup(tmp_path)
        try:
            original = store.commit
            async def fault(version, state, command):
                target = "command:quote-admit:" if failure.startswith("admit") else "command:quote:"
                if command.startswith(target):
                    if failure.endswith("postcommit"):
                        await original(version, state, command)
                    raise OSError("synthetic quote commit failure")
                return await original(version, state, command)
            store.commit = fault
            with pytest.raises(OSError):
                await runtime.quote("005930", Decimal("10400"), market_as_of=times[0], source="fixture", source_event_id="atomic")
            store, runtime = await reopen_quote_runtime(engine, exits, store, times, tmp_path)
            state = runtime.owner.state
            admissions = state.get("protection_quote_admissions", {})
            watermarks = state.get("latest_explicit_quote", {})
            if failure == "admit_precommit":
                assert admissions == {} and watermarks == {}
            else:
                row = watermarks["005930"]
                assert row["price"] == "10400" and row["as_of"] == times[0].isoformat()
                assert len(admissions) == (0 if failure == "apply_postcommit" else 1)
                if admissions:
                    admission = next(iter(admissions.values()))
                    assert admission["market_as_of"] == row["as_of"]
                    assert admission["source_version"] == row["admission_version"]
                    assert admission["price"] == row["price"]
                    before = runtime.owner.state
                    with pytest.raises(ApplicationBlocked):
                        await runtime.quote("005930", Decimal("10500"))
                    assert runtime.owner.state == before
            # A2b F1: 워터마크와 별개로 수락된 가격 view도 같은 admission에 저장한다.
            # commit 전 실패만 기존 일일 평가를 유지하고, 나머지는 수락 가격을 복원한다.
            # 보호 미완 장벽/원 입력 보존은 위 assertion대로 유지하며 복원 != 거래 허가다.
            expected_price = "10500" if failure == "admit_precommit" else "10400"
            assert engine.portfolio.positions["005930"].current_price == Decimal(expected_price)
            view = state.get('quote_price_views', {}).get('005930')
            assert (view is None) == (failure == 'admit_precommit')
            if view is not None:
                assert view['price'] == '10400' and view['source_version'] == row['admission_version']
            assert not runtime.trading_ready
        finally:
            await runtime.shutdown()
            await engine._shutdown()
            await store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("changed", ["price", "market_data", "as_of"])
def test_quote_source_event_conflict_does_not_confuse_received_time_or_intent(tmp_path, changed):
    async def scenario():
        engine, _, store, runtime, times = await quote_day_setup(tmp_path)
        try:
            as_of = times[0]
            args = {"market_as_of": as_of, "source": "fixture", "source_event_id": "same", "market_data": {"fixture": 1}}
            await runtime.quote("005930", Decimal("10500"), intent_id="first", **args)
            times[0] += timedelta(seconds=1)
            await runtime.quote("005930", Decimal("10500"), intent_id="retry", **args)
            before = runtime.owner.state
            if changed == "market_data":
                args["market_data"] = {"fixture": 2}
            elif changed == "as_of":
                args["market_as_of"] = times[0]
            with pytest.raises(ApplicationBlocked, match="market_quote_event_conflict"):
                await runtime.quote("005930", Decimal("10400" if changed == "price" else "10500"), **args)
            assert runtime.owner.state == before
            args = {"market_as_of": as_of, "source": "fixture", "source_event_id": "another"}
            await runtime.quote("005930", Decimal("10400"), **args)
            assert engine.portfolio.positions["005930"].current_price == Decimal("10400")
            assert len(runtime.owner.state["latest_explicit_quote"]) == 1
        finally:
            await engine._shutdown()
            await store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("corruption", ["root", "price", "naive_time", "future_asof", "source", "bool_version", "future_version", "digest"])
def test_invalid_quote_watermark_never_publishes_healthy_checkpoint(tmp_path, corruption):
    async def scenario():
        engine, _, store, runtime, times = await quote_day_setup(tmp_path)
        try:
            await runtime.quote("005930", Decimal("10400"), market_as_of=times[0], source="fixture", source_event_id="latest")
            before = engine.portfolio.positions["005930"].current_price
            def corrupt(state):
                if corruption == "root":
                    state["latest_explicit_quote"] = []
                else:
                    row = state["latest_explicit_quote"]["005930"]
                    field, value = {"price": ("price", "NaN"), "naive_time": ("as_of", "2026-09-19T10:00:00"),
                        "future_asof": ("as_of", (times[0]+timedelta(seconds=1)).isoformat()), "source": ("source", ""),
                        "bool_version": ("admission_version", True), "future_version": ("admission_version", runtime.owner.version+100),
                        "digest": ("payload_digest", "bad")}[corruption]
                    row[field] = value
                return state
            with pytest.raises(ApplicationBlocked):
                await runtime.owner.mutate("corrupt-watermark", corrupt)
            assert not runtime.owner.healthy
            assert engine.portfolio.positions["005930"].current_price == before
            with pytest.raises(ApplicationBlocked):
                await runtime.restore()
            with pytest.raises(ApplicationBlocked):
                await runtime.quote("005930", Decimal("10500"))
        finally:
            await engine._shutdown()
            await store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("after_fill", [False, True])
@pytest.mark.parametrize("provenance", ["fresh", "unknown"])
def test_accepted_quote_uses_same_price_during_admission_and_after_commit(tmp_path, after_fill, provenance):
    async def scenario():
        from src.execution.safety.day_recovery import ValuationPrice
        engine, _, store, runtime, times = await day_setup(tmp_path)
        release, entered = asyncio.Event(), asyncio.Event()
        caller = None
        try:
            ref = await opened(runtime, "B1")
            await queued(engine, await observed(runtime, ref, 100, "1000000"))
            fence = await prepare(runtime, times)
            boundary = times[0]
            eid = await valued(runtime, fence, times, [ValuationPrice("005930", Decimal("10500"), boundary, "fixture", "boundary")])
            assert (await runtime.rollover_day("roll", expected_version=runtime.owner.version, fence_id=fence.fence_id, valuation_evidence_id=eid)).status == "APPLIED"
            assert (await runtime.resume_after_rollover("resume", expected_version=runtime.owner.version, fence_id=fence.fence_id)).status == "APPLIED"
            times[0] += timedelta(seconds=2)
            if after_fill:
                await queued(engine, await current_fill(runtime, "B2"))
            expected = Decimal("10100")
            original = store.commit
            async def gated(version, state, commit_id):
                if commit_id.startswith("command:quote:"):
                    entered.set()
                    await release.wait()
                return await original(version, state, commit_id)
            store.commit = gated
            metadata = {"market_as_of": times[0], "source": "fixture", "source_event_id": "fresh"} if provenance == "fresh" else {}
            caller = asyncio.create_task(runtime.quote("005930", Decimal("10100"), **metadata))
            await asyncio.wait_for(entered.wait(), 2)
            mid = engine.portfolio.positions["005930"].current_price
            release.set()
            await caller
            final = engine.portfolio.positions["005930"].current_price
            assert (mid, final) == (expected, expected)
        finally:
            release.set()
            if caller is not None:
                await asyncio.gather(caller, return_exceptions=True)
            await engine._shutdown()
            await store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("after_fill", [False, True])
@pytest.mark.parametrize("price", ["10100", "9500"])
def test_explicit_stale_quote_is_rejected_without_price_or_protection_changes(tmp_path, after_fill, price):
    async def scenario():
        from src.execution.safety.day_recovery import ValuationPrice
        engine, _, store, runtime, times = await day_setup(tmp_path)
        try:
            ref = await opened(runtime, "B1")
            await queued(engine, await observed(runtime, ref, 100, "1000000"))
            fence = await prepare(runtime, times)
            boundary = times[0]
            eid = await valued(runtime, fence, times, [ValuationPrice("005930", Decimal("10500"), boundary, "fixture", "boundary")])
            assert (await runtime.rollover_day("roll", expected_version=runtime.owner.version, fence_id=fence.fence_id, valuation_evidence_id=eid)).status == "APPLIED"
            assert (await runtime.resume_after_rollover("resume", expected_version=runtime.owner.version, fence_id=fence.fence_id)).status == "APPLIED"
            times[0] += timedelta(seconds=2)
            if after_fill:
                await queued(engine, await current_fill(runtime, "B2"))
            before = runtime.owner.state
            version = runtime.owner.version
            with pytest.raises(ApplicationBlocked, match="stale_market_quote"):
                await runtime.quote("005930", Decimal(price), market_as_of=boundary-timedelta(seconds=1), source="fixture", source_event_id="delayed")
            assert runtime.owner.state == before
            assert runtime.owner.version == version
            assert engine.portfolio.positions["005930"].current_price == Decimal("10800" if after_fill else "10500")
            assert not runtime._protection_failed
        finally:
            await engine._shutdown()
            await store.close()
    asyncio.run(scenario())


def test_fresh_day_quotes_preserve_high_breakeven_and_stop_provenance(tmp_path):
    async def scenario():
        from src.execution.safety.day_recovery import ValuationPrice
        engine, exits, store, runtime, times = await day_setup(tmp_path)
        try:
            ref = await opened(runtime, "B1")
            await queued(engine, await observed(runtime, ref, 100, "1000000"))
            def stage(state):
                state["protection"]["states"]["005930"]["current_stage"] = "second"
                return state
            await runtime.owner.mutate("synthetic-stage", stage)
            fence = await prepare(runtime, times)
            eid = await valued(runtime, fence, times, [ValuationPrice("005930", Decimal("10500"), times[0], "fixture", "boundary")])
            assert (await runtime.rollover_day("roll", expected_version=runtime.owner.version, fence_id=fence.fence_id, valuation_evidence_id=eid)).status == "APPLIED"
            assert (await runtime.resume_after_rollover("resume", expected_version=runtime.owner.version, fence_id=fence.fence_id)).status == "APPLIED"
            times[0] += timedelta(seconds=1)
            assert await runtime.quote("005930", Decimal("11000"), market_as_of=times[0], source="fixture", source_event_id="high") is None
            assert exits.get_state("005930").highest_price == Decimal("11000")
            assert exits.get_state("005930").breakeven_activated
            times[0] += timedelta(seconds=1)
            decision = await runtime.quote("005930", Decimal("9500"), intent_id="fresh-stop", market_as_of=times[0], source="fixture", source_event_id="stop")
            assert decision is not None
            assert exits.get_state("005930").highest_price == Decimal("11000")
            assert exits.get_state("005930").breakeven_activated
            protection = runtime.owner.state["protection"]
            rows = [row for row in runtime.owner.state["outbox"].values() if row.get("kind") == "protection_decision"]
            assert len(rows) == 1
            assert rows[0]["provenance"] == {"market_as_of": times[0].isoformat(), "source": "fixture", "source_event_id": "stop", "received_at": times[0].isoformat()}
            await runtime.restore()
            assert exits.get_state("005930").highest_price == Decimal("11000")
            assert exits.get_state("005930").breakeven_activated
            assert runtime.owner.state["protection"] == protection
        finally:
            await engine._shutdown()
            await store.close()
    asyncio.run(scenario())


def test_next_day_partial_sale_uses_incremental_fill_price_on_publish_and_restore(tmp_path):
    async def scenario():
        from src.execution.safety.application import FillObservation
        from src.execution.safety.day_recovery import ValuationPrice
        from src.execution.safety.lifecycle import OrderRef, CommandResult, CommandStatus, OrderEvidence, OrderState
        engine, _, store, runtime, times = await day_setup(tmp_path)
        try:
            ref = await opened(runtime, "B1")
            await queued(engine, await observed(runtime, ref, 100, "1000000"))
            fence = await prepare(runtime, times)
            eid = await valued(runtime, fence, times, [ValuationPrice("005930", Decimal("10500"), times[0], "fixture", "boundary")])
            assert (await runtime.rollover_day("roll", expected_version=runtime.owner.version, fence_id=fence.fence_id, valuation_evidence_id=eid)).status == "APPLIED"
            assert (await runtime.resume_after_rollover("resume", expected_version=runtime.owner.version, fence_id=fence.fence_id)).status == "APPLIED"
            times[0] += timedelta(seconds=1)
            day = times[0].date().isoformat()
            sale_ref = OrderRef("scope", "KR", day, "KRX", "S1")
            await runtime.lifecycle.prepare("S1", "S1", 40, "005930", "sell", strategy="sepa_trend")
            await runtime.lifecycle.claim("S1", "sender")
            await runtime.lifecycle.record_result("S1", "sender", CommandResult(CommandStatus.ACKNOWLEDGED, "S1", sale_ref))
            for quantity, amount, expected in ((20, "212000", "10600"), (40, "428000", "10800")):
                evidence = OrderEvidence(sale_ref, "005930", "sell", 40, quantity, Decimal(amount), 40-quantity, 0,
                    OrderState.FINAL_FILLED if quantity == 40 else OrderState.PARTIAL,
                    complete=True, supported_finality=True, source_contract="synthetic-test", observed_at=times[0],
                    request_started_at=times[0]-timedelta(seconds=1), query_scope={"account_scope": "scope", "market": "KR", "exchange": "KRX",
                    "start_date": day, "end_date": day, "tr_id": "TTTC0081R", "query_kind": "all", "session": "regular"})
                assert await runtime.lifecycle.reconcile("S1", evidence)
                sale = FillObservation("scope", "KR", day, "KRX", "S1", "005930", "SELL", quantity, Decimal(amount))
                assert (await queued(engine, sale)).status == "APPLIED"
                assert engine.portfolio.positions["005930"].quantity == 100-quantity
                assert engine.portfolio.positions["005930"].current_price == Decimal(expected)
                await runtime.restore()
                assert engine.portfolio.positions["005930"].current_price == Decimal(expected)
        finally:
            await engine._shutdown()
            await store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("phase", ["queue_lock", "dequeued"])
def test_empty_queue_is_not_quiescent_with_admitted_ingress(tmp_path, phase):
    async def scenario():
        engine, _, store, runtime, times = await day_setup(tmp_path)
        caller = process = None
        try:
            ref = await opened(runtime, "B1")
            obs = await observed(runtime, ref, 100, "1000000")
            received = asyncio.Event()
            publish = runtime.owner.publisher
            def publish_received(state, version):
                publish(state, version)
                if obs.observation_id in state.get("inbox", {}):
                    received.set()
            runtime.owner.publisher = publish_received
            if phase == "queue_lock":
                await engine._queue_lock.acquire()
            caller = asyncio.create_task(engine.apply_execution_observation(obs))
            if phase == "dequeued":
                await wait_queued(engine, caller)
                event = await engine._get_next_event()
                assert not engine._event_queue
            else:
                await asyncio.wait_for(received.wait(), 2)
            assert not engine._event_queue
            fence = await prepare(runtime, times)
            assert fence.status == "BLOCKED" and fence.reason == "ingress_not_settled"
            assert fence.drained_ticket == 0
            assert runtime.owner.state["risk"]["day"] == "2026-09-18"
            if phase == "dequeued":
                process = asyncio.create_task(engine._process_event(event))
        finally:
            if engine._queue_lock.locked():
                engine._queue_lock.release()
            if process:
                await process
            await engine._shutdown()
            if caller:
                await asyncio.gather(caller, return_exceptions=True)
            await store.close()
    asyncio.run(scenario())


def test_final_commit_late_prior_day_fact_is_parked_and_resume_blocked(tmp_path):
    async def scenario():
        engine, _, store, runtime, times = await day_setup(tmp_path)
        entered, release = asyncio.Event(), asyncio.Event()
        original = store.commit
        async def gated(version, state, commit_id):
            if state.get("day_transition", {}).get("phase") == "ROLLED_OVER" and not entered.is_set():
                entered.set()
                await release.wait()
            return await original(version, state, commit_id)
        store.commit = gated
        try:
            fence = await prepare(runtime, times)
            evidence_id = await valued(runtime, fence, times)
            roll = asyncio.create_task(runtime.rollover_day("roll", expected_version=runtime.owner.version,
                                                           fence_id=fence.fence_id, valuation_evidence_id=evidence_id))
            await asyncio.wait_for(entered.wait(), 2)
            from src.execution.safety.application import FillObservation
            obs = FillObservation("scope", "KR", "2026-09-18", "KRX", "late", "005930", "BUY", 1, Decimal("10000"))
            caller = asyncio.create_task(engine.apply_execution_observation(obs))
            await asyncio.sleep(0)
            release.set()
            assert (await roll).status == "APPLIED"
            receipt = await caller
            assert receipt.reason == "late_prior_day_requires_reconciliation"
            assert not receipt.application_complete
            assert runtime.owner.state["inbox"][obs.observation_id]["observation"] == obs.to_dict()
            result = await runtime.resume_after_rollover("resume", expected_version=runtime.owner.version, fence_id=fence.fence_id)
            assert result.status == "BLOCKED"
            assert runtime.day_admission_closed
        finally:
            release.set()
            await engine._shutdown()
            await store.close()
    asyncio.run(scenario())


def test_dequeued_handler_waiting_owner_does_not_allow_day_reset(tmp_path):
    async def scenario():
        engine, _, store, runtime, times = await day_setup(tmp_path)
        caller = process = preparing = None
        try:
            obs = await current_fill(runtime, "B1", quantity=1, amount="10000")
            caller = asyncio.create_task(engine.apply_execution_observation(obs))
            await wait_queued(engine, caller)
            event = await engine._get_next_event()
            await runtime.owner._lock.acquire()
            started = asyncio.Event()
            async def process_event():
                started.set()
                await engine._process_event(event)
            process = asyncio.create_task(process_event())
            await started.wait()
            preparing = asyncio.create_task(prepare(runtime, times))
            await asyncio.sleep(0)
            assert not engine._event_queue and not preparing.done()
            assert runtime.owner.state["risk"]["day"] == "2026-09-18"
            runtime.owner._lock.release()
            await process
            await asyncio.gather(caller, return_exceptions=True)
            assert (await preparing).status == "BLOCKED"
            assert runtime.owner.state["risk"]["day"] == "2026-09-18"
        finally:
            if runtime.owner._lock.locked():
                runtime.owner._lock.release()
            await asyncio.gather(*(task for task in (caller, process, preparing) if task), return_exceptions=True)
            await engine._shutdown()
            await store.close()
    asyncio.run(scenario())


def test_old_quote_does_not_override_new_day_valuation_or_change_protection(tmp_path):
    async def scenario():
        engine, exits, store, runtime, times = await day_setup(tmp_path)
        try:
            ref = await opened(runtime, "B1")
            await queued(engine, await observed(runtime, ref, 100, "1000000"))
            await runtime.quote("005930", Decimal("10100"))
            before = runtime.owner.state
            fence = await prepare(runtime, times)
            from src.execution.safety.day_recovery import ValuationPrice
            evidence_id = await valued(runtime, fence, times, [ValuationPrice("005930", Decimal("10500"), times[0], "fixture", "p1")])
            receipt = await runtime.rollover_day("roll", expected_version=runtime.owner.version,
                                                fence_id=fence.fence_id, valuation_evidence_id=evidence_id)
            assert receipt.status == "APPLIED"
            assert engine.portfolio.daily_start_unrealized_pnl == Decimal("50000")
            assert engine.portfolio.positions["005930"].current_price == Decimal("10500")
            assert engine.portfolio.effective_daily_pnl == Decimal("0")
            assert runtime.owner.state["portfolio"]["positions"] == before["portfolio"]["positions"]
            for root in ("protection", "lots", "cursors", "outbox"):
                assert runtime.owner.state[root] == before[root]
            for field in ("counted_buy_orders", "count_loss_intents", "cost_basis_remaining", "buy_fee_remaining"):
                assert runtime.owner.state["risk"][field] == before["risk"][field]
            assert exits.get_state("005930").highest_price == Decimal("10100")
            assert (await runtime.resume_after_rollover("resume", expected_version=runtime.owner.version, fence_id=fence.fence_id)).status == "APPLIED"
            times[0] += timedelta(seconds=1)
            next_fill = await current_fill(runtime, "B2")
            assert (await queued(engine, next_fill)).status == "APPLIED"
            assert engine.portfolio.positions["005930"].current_price == Decimal("10800")
            assert engine.portfolio.daily_start_unrealized_pnl == Decimal("50000")
            assert engine.portfolio.daily_trades == 1
            assert (await queued(engine, next_fill)).status == "ALREADY_APPLIED"
            assert engine.portfolio.daily_trades == 1
        finally:
            await engine._shutdown()
            await store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("race", ["normal", "cancel", "concurrent", "day_changed"])
def test_current_day_parked_replay_uses_real_queue_before_resume(tmp_path, race):
    async def scenario():
        engine, _, store, runtime, times = await day_setup(tmp_path)
        try:
            fence = await prepare(runtime, times)
            evidence_id = await valued(runtime, fence, times)
            assert (await runtime.rollover_day("roll", expected_version=runtime.owner.version,
                        fence_id=fence.fence_id, valuation_evidence_id=evidence_id)).status == "APPLIED"
            # 이미 송신된 주문의 조회는 fence 중에도 허용된다. 기준선 주입은 시험 전용이다.
            from src.execution.safety.application import FillObservation
            obs = FillObservation("scope", "KR", "2026-09-19", "KRX", "B2", "005930", "BUY", 1, Decimal("10000"))
            def known(state):
                state["intents"]["B2"] = {"symbol": "005930", "side": "buy", "target_quantity": 1, "attempt_ids": ["B2"]}
                state["attempts"]["B2"] = {"attempt_id": "B2", "kind": "submit", "state": "final_filled", "side": "buy", "symbol": "005930",
                    "quantity": 1, "observed_quantity": 1, "observed_amount": "10000", "applied_quantity": 0,
                    "reserved_quantity": 1, "reserved_cash": "10002", "strategy": "sepa_trend", "intent_id": "B2",
                    "order_ref": {"account_scope": "scope", "market": "KR", "order_date": "2026-09-19", "exchange": "KRX",
                                  "order_no": "B2", "org_no": "", "parent_order_no": ""}}
                return state
            await runtime.owner.mutate("known-inflight", known)
            receipt = await engine.apply_execution_observation(obs)
            assert receipt.status == "RECEIVED"
            pending = asyncio.create_task(runtime.replay_parked_observation(obs.observation_id, fence_id=fence.fence_id))
            await wait_queued(engine, pending)
            second = None
            if race == "cancel":
                pending.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await pending
            elif race == "concurrent":
                second = asyncio.create_task(runtime.replay_parked_observation(obs.observation_id, fence_id=fence.fence_id))
            elif race == "day_changed":
                times[0] += timedelta(days=1)
            await engine._process_event(await engine._get_next_event())
            if race == "day_changed":
                with pytest.raises(ApplicationBlocked):
                    await pending
                assert runtime.owner.state["inbox"][obs.observation_id]["status"] == "RECEIVED"
                assert not engine.portfolio.positions
                return
            if race != "cancel":
                assert (await pending).status == "APPLIED"
            else:
                await asyncio.gather(*tuple(engine._execution_ingress_tasks))
            if second:
                await wait_queued(engine, second)
                await engine._process_event(await engine._get_next_event())
                assert (await second).status == "ALREADY_APPLIED"
            assert engine.portfolio.positions["005930"].quantity == 1
            assert runtime.day_admission_closed
            resume = await runtime.resume_after_rollover("resume", expected_version=runtime.owner.version, fence_id=fence.fence_id)
            assert resume.status == "APPLIED"
            assert not runtime.day_admission_closed and not runtime.trading_ready
            assert (await queued(engine, obs)).status == "ALREADY_APPLIED"
            assert engine.portfolio.daily_trades == 1
        finally:
            await engine._shutdown()
            await store.close()
    asyncio.run(scenario())


def test_prepare_cas_is_checked_after_owner_wait_and_failure_stays_closed(tmp_path):
    async def scenario():
        engine, _, store, runtime, times = await day_setup(tmp_path)
        entered = asyncio.Event()
        await runtime.owner._lock.acquire()
        async def older_command():
            entered.set()
            await runtime.owner.mutate("older", lambda state: state)
        older = asyncio.create_task(older_command())
        await entered.wait()
        prepared = asyncio.create_task(prepare(runtime, times))
        try:
            await asyncio.sleep(0)
            runtime.owner._lock.release()
            await older
            receipt = await prepared
            assert receipt.status == "BLOCKED" and receipt.reason == "stale_execution_version"
            assert runtime.owner.state["day_transition"]["phase"] == "PREPARED"
            await runtime.restore()
            assert runtime.day_admission_closed
        finally:
            if runtime.owner._lock.locked():
                runtime.owner._lock.release()
            await asyncio.gather(older, prepared, return_exceptions=True)
            await engine._shutdown()
            await store.close()
    asyncio.run(scenario())


def test_concurrent_prepare_keeps_first_fence_and_generation(tmp_path):
    async def scenario():
        engine, _, store, runtime, times = await day_setup(tmp_path)
        await runtime.owner._lock.acquire()
        first = asyncio.create_task(prepare(runtime, times, "first"))
        await asyncio.sleep(0)
        first_fence, first_generation = runtime._day_fence_id, runtime._day_generation
        second = asyncio.create_task(prepare(runtime, times, "second"))
        try:
            await asyncio.sleep(0)
            assert runtime._day_fence_id == first_fence
            assert runtime._day_generation == first_generation
            runtime.owner._lock.release()
            a, b = await asyncio.gather(first, second)
            assert a.status == "PREPARED" and b.status == "BLOCKED"
            assert a.fence_id == b.fence_id == first_fence
        finally:
            if runtime.owner._lock.locked():
                runtime.owner._lock.release()
            await asyncio.gather(first, second, return_exceptions=True)
            await engine._shutdown()
            await store.close()
    asyncio.run(scenario())


def test_kst_midnight_uses_aware_clock_and_does_not_advance_from_host_day(tmp_path):
    from datetime import datetime, timezone
    async def scenario():
        engine, _, store, runtime, times = await day_setup(tmp_path)
        try:
            times[0] = datetime(2026, 9, 18, 14, 59, 59, tzinfo=timezone.utc)
            with pytest.raises(ValueError):
                await runtime.prepare_day_rollover("early", expected_version=runtime.owner.version,
                    from_day="2026-09-18", to_day="2026-09-19", valuation_boundary=times[0])
            assert not runtime.day_admission_closed
            times[0] += timedelta(seconds=1)
            assert runtime.day_admission_closed
            fence = await runtime.prepare_day_rollover("midnight", expected_version=runtime.owner.version,
                from_day="2026-09-18", to_day="2026-09-19", valuation_boundary=times[0])
            assert fence.status == "PREPARED"
            assert fence.to_day == "2026-09-19" and fence.valuation_boundary.endswith("+09:00")
        finally:
            await engine._shutdown()
            await store.close()
    asyncio.run(scenario())


def test_shutdown_does_not_hang_on_held_queue_lock_after_receive(tmp_path):
    async def scenario():
        engine, _, store, runtime = await setup(tmp_path)
        await engine._queue_lock.acquire()
        try:
            ref = await opened(runtime, "B1")
            obs = await observed(runtime, ref, 100, "1000000")
            received = asyncio.Event()
            publish = runtime.owner.publisher
            def ready(state, version):
                publish(state, version)
                if obs.observation_id in state.get("inbox", {}):
                    received.set()
            runtime.owner.publisher = ready
            caller = asyncio.create_task(engine.apply_execution_observation(obs))
            await asyncio.wait_for(received.wait(), 2)
            await asyncio.wait_for(engine._shutdown(), 2)
            with pytest.raises(ApplicationBlocked):
                await caller
            assert runtime.owner.state["inbox"][obs.observation_id]["status"] == "RECEIVED"
            assert not engine._execution_ingress_tasks and not engine._event_queue
        finally:
            if engine._queue_lock.locked():
                engine._queue_lock.release()
            await store.close()
    asyncio.run(scenario())


async def day_setup(tmp_path):
    times = [NOW]
    engine, exits, store, runtime = await setup(tmp_path, account_scope="scope", clock=lambda: times[0])
    return engine, exits, store, runtime, times


@pytest.mark.parametrize("bad", ["scope", "fence", "fingerprint", "future", "prior_day", "missing_price", "no_provenance"])
def test_valuation_rejects_wrong_scope_or_unmeasured_prices(tmp_path, bad):
    async def scenario():
        engine, _, store, runtime, times = await day_setup(tmp_path)
        try:
            ref = await opened(runtime, "B1")
            await queued(engine, await observed(runtime, ref, 100, "1000000"))
            fence = await prepare(runtime, times)
            from src.execution.safety.day_recovery import ValuationEvidence, ValuationPrice, portfolio_fingerprint
            as_of = times[0] + timedelta(seconds=1) if bad == "future" else times[0] - timedelta(days=1) if bad == "prior_day" else times[0]
            prices = () if bad == "missing_price" else (ValuationPrice("005930", Decimal("10500"), as_of,
                                                                        "" if bad == "no_provenance" else "fixture", "p1"),)
            evidence = ValuationEvidence("other" if bad == "scope" else "scope", "KR",
                "other" if bad == "fence" else fence.fence_id,
                "other" if bad == "fingerprint" else portfolio_fingerprint(runtime.owner.state), times[0], prices)
            if bad == "no_provenance":
                with pytest.raises(ValueError):
                    await runtime.accept_valuation_evidence("value", evidence, expected_version=runtime.owner.version)
            else:
                receipt = await runtime.accept_valuation_evidence("value", evidence, expected_version=runtime.owner.version)
                assert receipt.status == "BLOCKED"
            assert not runtime.owner.state.get("day_valuations")
            assert runtime.owner.state["risk"]["day"] == "2026-09-18"
        finally:
            await engine._shutdown()
            await store.close()
    asyncio.run(scenario())


def test_day_publish_resets_real_risk_metrics_once_without_legacy_writes(tmp_path, monkeypatch):
    from pathlib import Path
    import src.risk.manager as policy
    from src.core.types import RiskConfig
    class TestPath(type(Path())):
        @classmethod
        def home(cls):
            return tmp_path / "risk-home"
    monkeypatch.setattr(policy, "Path", TestPath)
    risk = policy.RiskManager(RiskConfig(), Decimal("2000000"), "KR")
    async def scenario():
        times = [NOW]
        engine, _, store, runtime = await setup(tmp_path, risk_manager=risk, account_scope="scope", clock=lambda: times[0])
        try:
            risk.metrics.can_trade = False
            risk.metrics.is_daily_loss_limit_hit = True
            risk.metrics.daily_loss = Decimal("-100")
            risk._last_exit_cooldown_log["005930"] = NOW
            engine.risk_metrics.is_daily_loss_limit_hit = True
            def no_legacy_write(*args, **kwargs):
                raise AssertionError("legacy daily writer called")
            monkeypatch.setattr(risk, "reset_daily_stats", no_legacy_write)
            monkeypatch.setattr(engine, "reset_daily_stats", no_legacy_write)
            fence = await prepare(runtime, times)
            assert risk.metrics.is_daily_loss_limit_hit
            evidence_id = await valued(runtime, fence, times)
            assert (await runtime.rollover_day("roll", expected_version=runtime.owner.version,
                     fence_id=fence.fence_id, valuation_evidence_id=evidence_id)).status == "APPLIED"
            assert risk.metrics.can_trade and not risk.metrics.is_daily_loss_limit_hit
            assert risk.metrics.daily_loss == 0 and not risk._last_exit_cooldown_log
            assert not engine.risk_metrics.is_daily_loss_limit_hit
            assert risk.daily_stats.peak_equity == Decimal("2000000")
            risk.metrics.daily_loss = Decimal("-200")
            await runtime.owner.mutate("same-day-publish", lambda state: state)
            assert risk.metrics.daily_loss == Decimal("-200")
        finally:
            await engine._shutdown()
            await store.close()
    asyncio.run(scenario())


def test_successful_redelivery_resolves_failed_ticket_without_blocking_rollover(tmp_path):
    async def scenario():
        engine, _, store, runtime, times = await day_setup(tmp_path)
        try:
            obs = await current_fill(runtime, "B1", quantity=1, amount="10000")
            publish = runtime.owner.publisher
            def fail(state, version):
                if state.get("cursors"):
                    raise ValueError("synthetic publication failure")
                publish(state, version)
            runtime.owner.publisher = fail
            with pytest.raises(ApplicationBlocked):
                await queued(engine, obs)
            runtime.owner.publisher = publish
            await runtime.restore()
            assert (await queued(engine, obs)).status == "ALREADY_APPLIED"
            fence = await prepare(runtime, times)
            assert fence.status == "PREPARED"
        finally:
            await engine._shutdown()
            await store.close()
    asyncio.run(scenario())


def test_equal_quantity_but_unapplied_observed_amount_blocks_day_change(tmp_path):
    async def scenario():
        engine, _, store, runtime, times = await day_setup(tmp_path)
        try:
            obs = await current_fill(runtime, "B1", quantity=1, amount="10000")
            await queued(engine, obs)
            def conflict(state):
                state["attempts"]["B1"]["observed_amount"] = "10001"
                return state
            await runtime.owner.mutate("synthetic-amount-gap", conflict)
            fence = await prepare(runtime, times)
            assert fence.status == "BLOCKED" and fence.reason == "observed_amount_not_applied"
            assert runtime.owner.state["risk"]["day"] == "2026-09-18"
        finally:
            await engine._shutdown()
            await store.close()
    asyncio.run(scenario())


def test_receive_is_durable_immutable_and_is_not_an_application_receipt(tmp_path):
    from src.execution.safety.application import IngressContext, ObservationError
    async def scenario():
        engine, _, store, runtime = await setup(tmp_path)
        try:
            obs = await current_fill(runtime, "B1", quantity=1, amount="10000")
            context = IngressContext(1, 0, NOW)
            receipt = await runtime.owner.receive(obs, ingress_context=context)
            assert receipt.status == "RECEIVED" and not receipt.application_complete
            version = runtime.owner.version
            again = await runtime.owner.receive(obs, ingress_context=IngressContext(2, 1, NOW+timedelta(seconds=1), "fence", "parked"))
            assert again.execution_version == version
            assert runtime.owner.state["inbox"][obs.observation_id]["ingress_context"] == context.to_dict()
            assert engine.portfolio.cash == Decimal("2000000")
            def corrupt(state):
                state["inbox"][obs.observation_id]["observation"]["cumulative_amount"] = "10001"
                return state
            await runtime.owner.mutate("synthetic-inbox-corruption", corrupt)
            before = runtime.owner.state
            with pytest.raises(ObservationError):
                await runtime.owner.receive(obs, ingress_context=context)
            assert runtime.owner.state == before
        finally:
            await engine._shutdown()
            await store.close()
    asyncio.run(scenario())


def test_publication_version_mismatch_prevents_rollover_reset(tmp_path):
    async def scenario():
        engine, _, store, runtime, times = await day_setup(tmp_path)
        try:
            fence = await prepare(runtime, times)
            evidence_id = await valued(runtime, fence, times)
            engine._execution_version -= 1
            receipt = await runtime.rollover_day("roll", expected_version=runtime.owner.version,
                        fence_id=fence.fence_id, valuation_evidence_id=evidence_id)
            assert receipt.status == "BLOCKED" and receipt.reason == "publication_mismatch"
            assert runtime.owner.state["risk"]["day"] == "2026-09-18"
        finally:
            await engine._shutdown()
            await store.close()
    asyncio.run(scenario())


def test_cancelled_prepare_drains_preaccepted_quote_then_retains_closed_fence(tmp_path):
    async def scenario():
        engine, _, store, runtime, times = await day_setup(tmp_path)
        await runtime.owner._lock.acquire()
        quote = asyncio.create_task(runtime.quote("005930", Decimal("10000")))
        await asyncio.sleep(0)
        preparing = asyncio.create_task(prepare(runtime, times))
        try:
            await asyncio.sleep(0)
            preparing.cancel()
            with pytest.raises(asyncio.CancelledError):
                await preparing
            assert runtime.day_admission_closed
            runtime.owner._lock.release()
            await quote
            await asyncio.gather(*tuple(runtime._day_tasks))
            assert runtime.owner.state["day_transition"]["phase"] == "PREPARED"
            assert runtime.owner.state["recovery_receipts"]["prepare"]["status"] == "BLOCKED"
            assert not runtime.owner.state.get("protection_quote_admissions")
            assert not runtime._protection_tasks
            assert runtime.day_admission_closed
        finally:
            if runtime.owner._lock.locked():
                runtime.owner._lock.release()
            await asyncio.gather(quote, preparing, return_exceptions=True)
            await engine._shutdown()
            await store.close()
    asyncio.run(scenario())


def test_prepare_fences_lifecycle_and_protection_before_owner_lock(tmp_path):
    async def scenario():
        engine, _, store, runtime, times = await day_setup(tmp_path)
        await runtime.owner._lock.acquire()
        started = asyncio.Event()
        async def lifecycle_prepare():
            started.set()
            return await runtime.lifecycle.prepare("new", "new", 1, "005930", "buy")
        pending = asyncio.create_task(lifecycle_prepare())
        await started.wait()
        preparing = asyncio.create_task(prepare(runtime, times))
        try:
            await asyncio.sleep(0)
            assert runtime.day_admission_closed
            with pytest.raises(ApplicationBlocked):
                await runtime.quote("005930", Decimal("10000"))
            with pytest.raises(ApplicationBlocked):
                await runtime.repair_protection("repair", "005930", expected_version=runtime.owner.version)
            with pytest.raises(ApplicationBlocked):
                await runtime.lifecycle.claim("new", "sender")
            runtime.owner._lock.release()
            with pytest.raises(ApplicationBlocked):
                await pending
            assert (await preparing).status == "PREPARED"
            assert not runtime.owner.state["attempts"]
        finally:
            if runtime.owner._lock.locked():
                runtime.owner._lock.release()
            await asyncio.gather(pending, preparing, return_exceptions=True)
            await engine._shutdown()
            await store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("scope", [None, "other"])
def test_prepare_requires_explicit_matching_account_scope(tmp_path, scope):
    async def scenario():
        engine, _, store, runtime, times = await day_setup(tmp_path)
        try:
            if scope == "other":
                await opened(runtime, "B1")
            runtime.account_scope = scope
            receipt = await prepare(runtime, times)
            assert receipt.status == "BLOCKED"
            assert receipt.reason in ("account_scope_required", "account_scope_conflict")
            assert runtime.day_admission_closed
        finally:
            await engine._shutdown()
            await store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["precommit", "postcommit", "publication", "caller_cancel"])
def test_rollover_fault_and_reopen_keep_durable_date_and_closed_admission(tmp_path, failure):
    async def scenario():
        engine, _, store, runtime, times = await day_setup(tmp_path)
        release, entered = asyncio.Event(), asyncio.Event()
        original_commit, original_publish = store.commit, runtime.owner.publisher
        try:
            fence = await prepare(runtime, times)
            evidence_id = await valued(runtime, fence, times)
            expected = runtime.owner.version
            armed = True
            async def failing_commit(version, state, commit_id):
                nonlocal armed
                if armed and state.get("day_transition", {}).get("phase") == "ROLLED_OVER":
                    armed = False
                    if failure == "precommit":
                        raise OSError("synthetic precommit")
                    if failure == "caller_cancel":
                        entered.set()
                        await release.wait()
                    result = await original_commit(version, state, commit_id)
                    if failure == "postcommit":
                        raise OSError("synthetic acknowledgement lost")
                    return result
                return await original_commit(version, state, commit_id)
            def failing_publish(state, version):
                if failure == "publication" and state.get("day_transition", {}).get("phase") == "ROLLED_OVER":
                    raise ValueError("synthetic publish failure")
                original_publish(state, version)
            store.commit, runtime.owner.publisher = failing_commit, failing_publish
            call = asyncio.create_task(runtime.rollover_day("roll", expected_version=expected,
                                                           fence_id=fence.fence_id, valuation_evidence_id=evidence_id))
            if failure == "caller_cancel":
                await asyncio.wait_for(entered.wait(), 2)
                call.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await call
                release.set()
                await asyncio.gather(*tuple(runtime._day_tasks))
            else:
                with pytest.raises((OSError, ApplicationBlocked)):
                    await call
            stored = (await store.load())[1]
            expected_day = "2026-09-18" if failure == "precommit" else "2026-09-19"
            assert stored["risk"]["day"] == expected_day
            assert runtime.day_admission_closed and not runtime.trading_ready
            runtime.owner.publisher = original_publish
            await runtime.restore()
            assert runtime.owner.state["risk"]["day"] == expected_day
            await engine._shutdown()
            await store.close()
            from src.core.engine import UnifiedEngine
            from src.core.types import TradingConfig
            from src.execution.safety.runtime import KRExecutionRuntime
            from src.execution.safety.store import ExecutionStateStore
            from src.strategies.exit_manager import ExitManager
            reopened = ExecutionStateStore(tmp_path / "execution" / "state.sqlite3")
            new_engine = UnifiedEngine(TradingConfig())
            new_runtime = KRExecutionRuntime(reopened, new_engine, ExitManager(persist=False),
                                             clock=lambda: times[0], account_scope="scope")
            try:
                await new_runtime.restore()
                new_runtime.attach()
                assert new_runtime.day_admission_closed and not new_runtime.trading_ready
                if failure != "precommit":
                    replay = await new_runtime.rollover_day("roll", expected_version=expected,
                                fence_id=fence.fence_id, valuation_evidence_id=evidence_id)
                    assert replay.status == "APPLIED" and new_runtime.owner.state["risk"]["day"] == "2026-09-19"
            finally:
                await new_engine._shutdown()
                await reopened.close()
        finally:
            release.set()
            runtime.owner.publisher = original_publish
            await engine._shutdown()
            await store.close()
    asyncio.run(scenario())


async def prepare(runtime, times, operation="prepare"):
    times[0] = NOW + timedelta(days=1)
    return await runtime.prepare_day_rollover(
        operation, expected_version=runtime.owner.version, from_day="2026-09-18",
        to_day="2026-09-19", valuation_boundary=times[0])


async def valued(runtime, fence, times, prices=()):
    from src.execution.safety.day_recovery import ValuationEvidence, portfolio_fingerprint
    evidence = ValuationEvidence("scope", "KR", fence.fence_id,
                                 portfolio_fingerprint(runtime.owner.state), times[0], tuple(prices))
    receipt = await runtime.accept_valuation_evidence("valuation", evidence,
                                                      expected_version=runtime.owner.version)
    assert receipt.status == "APPLIED"
    return receipt.evidence_id


def test_fence_receives_late_facts_without_applying_or_reopening(tmp_path):
    async def scenario():
        engine, _, store, runtime, times = await day_setup(tmp_path)
        try:
            fence = await prepare(runtime, times)
            assert fence.status == "PREPARED"
            from src.execution.safety.application import FillObservation
            obs = FillObservation("scope", "KR", "2026-09-18", "KRX", "late", "005930",
                                  "BUY", 1, Decimal("10000"))
            receipt = await asyncio.wait_for(engine.apply_execution_observation(obs), 2)
            assert receipt.status == "RECEIVED" and not receipt.application_complete
            assert runtime.owner.state["inbox"][obs.observation_id]["observation"]["trading_day"] == "2026-09-18"
            assert engine.portfolio.cash == Decimal("2000000")
            result = await runtime.resume_after_rollover("resume", expected_version=runtime.owner.version,
                                                        fence_id=fence.fence_id)
            assert result.status == "BLOCKED"
            assert runtime.day_admission_closed and not runtime.trading_ready
        finally:
            await engine._shutdown()
            await store.close()
    asyncio.run(scenario())


def test_empty_rollover_requires_valuation_and_preserves_startup(tmp_path):
    async def scenario():
        engine, _, store, runtime, times = await day_setup(tmp_path)
        try:
            before = runtime.owner.state
            fence = await prepare(runtime, times)
            missing = await runtime.rollover_day("missing", expected_version=runtime.owner.version,
                                                 fence_id=fence.fence_id, valuation_evidence_id="absent")
            assert missing.status == "BLOCKED"
            evidence_id = await valued(runtime, fence, times)
            expected = runtime.owner.version
            receipt = await runtime.rollover_day("roll", expected_version=expected,
                                                 fence_id=fence.fence_id, valuation_evidence_id=evidence_id)
            assert receipt.status == "APPLIED"
            assert runtime.owner.state["risk"]["day"] == "2026-09-19"
            assert runtime.owner.state["startup_reconciliation"] == before["startup_reconciliation"]
            assert runtime.day_admission_closed
            replay = await runtime.rollover_day("roll", expected_version=expected,
                                                fence_id=fence.fence_id, valuation_evidence_id=evidence_id)
            assert replay.committed_version == receipt.committed_version
            with pytest.raises(ValueError, match="conflict"):
                await runtime.rollover_day("roll", expected_version=runtime.owner.version,
                                            fence_id=fence.fence_id, valuation_evidence_id="other")
            resume = await runtime.resume_after_rollover("resume", expected_version=runtime.owner.version,
                                                        fence_id=fence.fence_id)
            assert resume.status == "APPLIED"
            assert not runtime.day_admission_closed and not runtime.trading_ready
        finally:
            await engine._shutdown()
            await store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("blocker", ["reservation", "unknown_child", "unapplied", "failed_inbox", "quote_admission"])
def test_unresolved_state_blocks_rollover_without_economic_reset(tmp_path, blocker):
    async def scenario():
        engine, _, store, runtime, times = await day_setup(tmp_path)
        try:
            def poison(state):
                if blocker == "reservation":
                    state["attempts"]["x"] = {"kind": "submit", "state": "final_filled", "observed_quantity": 1,
                                               "applied_quantity": 1, "reserved_quantity": 0, "reserved_cash": "1"}
                elif blocker == "unknown_child":
                    state["attempts"]["x"] = {"kind": "cancel", "state": "final_rejected", "command_status": "unknown"}
                elif blocker == "unapplied":
                    state["attempts"]["x"] = {"kind": "submit", "state": "final_filled", "observed_quantity": 2,
                                               "applied_quantity": 1, "reserved_quantity": 0, "reserved_cash": "0"}
                elif blocker == "failed_inbox":
                    state["inbox"] = {"x": {"status": "FAILED"}}
                else:
                    state["protection_quote_admissions"] = {"x": {"symbol": "005930", "status": "RECEIVED"}}
                return state
            await runtime.owner.mutate("poison", poison)
            before = deepcopy(runtime.owner.state)
            fence = await prepare(runtime, times)
            evidence_id = await valued(runtime, fence, times)
            receipt = await runtime.rollover_day("roll", expected_version=runtime.owner.version,
                                                 fence_id=fence.fence_id, valuation_evidence_id=evidence_id)
            assert receipt.status == "BLOCKED"
            for root in ("portfolio", "risk", "attempts", "inbox", "startup_reconciliation"):
                assert runtime.owner.state.get(root) == before.get(root)
        finally:
            await engine._shutdown()
            await store.close()
    asyncio.run(scenario())


def test_queue_lock_waiter_cancellation_keeps_durable_fact_on_shutdown(tmp_path):
    async def scenario():
        engine, _, store, runtime = await setup(tmp_path)
        try:
            ref = await opened(runtime, "B1")
            obs = await observed(runtime, ref, 100, "1000000")
            await engine._queue_lock.acquire()
            started = asyncio.Event()

            async def deliver():
                started.set()
                return await engine.apply_execution_observation(obs)

            caller = asyncio.create_task(deliver())
            await started.wait()
            caller.cancel()
            with pytest.raises(asyncio.CancelledError):
                await caller
            engine._queue_lock.release()
            await asyncio.wait_for(engine._shutdown(), 2)
            durable = (await store.load())[1]
            assert obs.observation_id in durable.get("inbox", {})
            assert durable["inbox"][obs.observation_id]["status"] == "RECEIVED"
            assert engine.portfolio.cash == Decimal("2000000")
        finally:
            if engine._queue_lock.locked():
                engine._queue_lock.release()
            await store.close()
    asyncio.run(scenario())


def test_a_store_failure_keeps_submit_closed_with_its_own_reason(tmp_path, monkeypatch):
    """P0-4 A5 대조 — 저장 실패는 보호 래치와 다른 장치로 막는다(완화되지 않았음의 증거)."""
    async def scenario():
        from contextlib import suppress
        from src.execution.safety.store import StoreError
        from tests.test_execution_command_owner import fixture as command_fixture
        f = await command_fixture(tmp_path, monkeypatch)
        runtime, commands = f['runtime'], f['commands']
        try:
            request = f['request']()
            await f['quote'](request)
            commit = f['store'].commit

            async def broken(version, state, command_id):
                raise StoreError('synthetic-store-failure')

            monkeypatch.setattr(f['store'], 'commit', broken)
            with pytest.raises(StoreError):
                await commands.prepare(request, f['entry'](request))
            assert runtime.owner.healthy is False
            assert runtime._protection_failed is False  # 래치가 아니라 저장 건강이 막는다.
            monkeypatch.setattr(f['store'], 'commit', commit)
            # 게이트 낱말은 `store_or_publication_unhealthy` 이고, 실제 prepare 는 그보다
            # 앞선 owner 의 준비 검사에서 끝난다 — 두 겹 다 닫혀 있음을 각각 고정한다.
            with pytest.raises(ValueError, match='store_or_publication_unhealthy'):
                commands._owner_ready(runtime.owner.state)
            with pytest.raises(ApplicationBlocked):
                await commands.prepare(request, f['entry'](request))
            assert not f['broker']._session.posts
        finally:
            with suppress(ApplicationBlocked):
                await runtime.shutdown()
            await f['store'].close()
    asyncio.run(scenario())


def test_a_blocked_rollover_latches_the_day_fence_for_cancel_and_across_restart(tmp_path, monkeypatch):
    """P0-4 A6 특성화 — 래치·미해결 접수 행이 서면 일자 전환이 BLOCKED 이고 울타리는 재시작을 넘는다.

    코드 추론(F-A1-7·②M1)을 런타임으로 처음 재현한다: BLOCKED 로 끝난 `prepare_day_rollover`
    한 번이 `day_transition` 을 PREPARED 로 durable 하게 남겨 다음 날 CANCEL 까지 닫고,
    새 runtime 의 `restore()` 는 프로세스 bool 만 지울 뿐 그 울타리를 열지 못한다(차단 사유 23).
    """
    async def scenario():
        from contextlib import suppress
        from src.execution.safety.lifecycle import CommandStatus, OrderRef
        from src.execution.safety.requests import CancelParent
        from src.execution.safety.runtime import KRExecutionRuntime
        from src.execution.safety.store import ExecutionStateStore
        from src.execution.safety.transport import GuardedKISTransport
        from tests.test_execution_command_owner import fixture as command_fixture
        f = await command_fixture(tmp_path, monkeypatch)
        runtime, commands, clock = f['runtime'], f['commands'], f['clock']
        fresh = None
        try:
            request = f['request']()
            await f['quote'](request)
            await commands.prepare(request, f['entry'](request))
            transport = GuardedKISTransport(f['broker'], request_builder=f['builder'])
            ack = await commands.dispatch(request, f['entry'](request), transport)
            assert ack.status is CommandStatus.ACKNOWLEDGED
            # ① 같은 operation_id·다른 본문의 복구 명령은 사전 거부인데도 래치한다(H1-3 은 P1 보류).
            assert (await runtime.repair_protection('R1', request.symbol,
                expected_version=runtime.owner.version)).status == 'BLOCKED'
            with pytest.raises(ValueError, match='recovery_operation_id_conflict'):
                await runtime.repair_protection('R1', '000660', expected_version=runtime.owner.version)
            assert runtime._protection_failed is True and runtime.owner.healthy
            # ② 미해결 보호 접수 행도 남긴다 — 둘 다 따로 일자 전환을 막는다.
            def admitted(state):
                state.setdefault('protection_quote_admissions', {})['x'] = {
                    'symbol': request.symbol, 'status': 'RECEIVED'}
                return state

            await runtime.owner.mutate('synthetic-unresolved-admission', admitted)
            # ③ 래치가 서 있어도 당일 CANCEL 은 준비된다 — 래치는 SUBMIT 만 막는다.
            parent = runtime.owner.state['attempts'][request.attempt_id]
            ref = CancelParent(request.intent_id, request.attempt_id, parent['version'],
                OrderRef.from_dict(parent['order_ref']), request.symbol, request.side,
                request.order_type, parent['reserved_quantity'], request.valuation_price,
                request.strategy)
            cancel = f['builder'].prepare_cancel(intent_id=request.intent_id, attempt_id='C',
                                                 session=request.session, parent=ref)
            await commands.prepare(cancel, f['entry'](cancel))
            # ④ 다음 날 일자 전환은 BLOCKED 인데 fence 는 durable 하게 남는다.
            clock[0] = clock[0] + timedelta(days=1)
            receipt = await runtime.prepare_day_rollover('P1', expected_version=runtime.owner.version,
                from_day=runtime.owner.state['risk']['day'], to_day=clock[0].date().isoformat(),
                valuation_boundary=clock[0])
            assert receipt.status == 'BLOCKED' and receipt.reason == 'protection_update_failed'
            assert runtime._day_closed is True
            assert runtime.owner.state['day_transition']['phase'] == 'PREPARED'
            # ⑤ 그래서 다음 날에는 CANCEL 까지 닫힌다.
            with pytest.raises(ApplicationBlocked, match='day_transition_admission_closed'):
                runtime._require_day_admission()
            result = await commands.dispatch(cancel, f['entry'](cancel), transport)
            assert result.status is CommandStatus.NOT_SENT
            assert result.reason_code == 'command_admission_closed'
            # ⑥ 재시작은 프로세스 bool 만 지우고 울타리는 그대로다(해제 경로 0).
            with suppress(ApplicationBlocked):
                await runtime.shutdown()
            await f['store'].close()
            reopened = ExecutionStateStore(f['store'].path)
            fresh = KRExecutionRuntime(reopened, f['engine'], f['exits'],
                                       clock=lambda: clock[0], account_scope='test-scope')
            await fresh.restore()
            assert fresh._protection_failed is False and fresh._day_closed is True
            with pytest.raises(ApplicationBlocked, match='day_transition_admission_closed'):
                fresh._require_day_admission()
            assert len(f['broker']._session.posts) == 1
        finally:
            with suppress(ApplicationBlocked):
                await runtime.shutdown()
            await f['store'].close()
            if fresh is not None:
                with suppress(ApplicationBlocked):
                    await fresh.shutdown()
                await fresh.owner.store.close()
    asyncio.run(scenario())
