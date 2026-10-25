"""Managed receive progress must not depend on unrelated paced sends."""
import asyncio
import json
from datetime import datetime, timezone

import pytest

from src.analytics.entry_observation import EntryObservationBuffer, capture_quote
from src.analytics.received_entry_input import _subscription_stream_complete
from test_quote_subscription import BOOK, PRICE, ack, book_frame, make_feed, setup_owner


async def cleanup(owner, *tasks):
    for task in tasks:
        if task is not None and not task.done():
            task.cancel()
    await asyncio.gather(*(t for t in tasks if t is not None), return_exceptions=True)
    await owner.cancel_pending()
    await owner.stop()


@pytest.mark.asyncio
async def test_real_feed_processes_ack_and_book_before_fifteen_send_batch_ends():
    feed = make_feed()
    buffer = EntryObservationBuffer(evaluation_epoch="synthetic", capacity=1000)
    feed.enable_quote_observation(buffer, registration_cap=41, external_reserved=0,
        operational_headroom=0, max_candidates=5, lease_seconds=600,
        evidence_ref="synthetic", request_interval=0, market_state_evidence=True,
        market_state_max_records=100)
    owner = feed._quote_subscription_owner
    entered, release = asyncio.Event(), asyncio.Event()
    sent = []

    async def send(action, key):
        sent.append((action, key))
        if len(sent) == 15:
            entered.set()
            await release.wait()

    gen = await owner.start(send)
    feed._connected = True
    feed._ws = object()
    symbols = ["005930", "000660", "035420", "051910", "005380"]

    async def save(quote):
        capture_quote(buffer, quote)

    feed.on_quote(save)
    enrollment = asyncio.create_task(owner.enroll("scan", symbols))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        await asyncio.wait_for(feed._handle_message(json.dumps(ack((BOOK, symbols[0]))),
            socket=feed._ws, generation=gen), 0.5)
        await feed._handle_message(book_frame(), socket=feed._ws, generation=gen)
        rows = buffer.export()["records"]
        assert not enrollment.done()
        assert sum(r.get("status") == "requested" for r in rows) == 14
        assert sum(r.get("status") == "allocated" for r in rows) == 0
        assert sum(r["kind"] == "ws_quote" for r in rows) == 1
        assert not feed._subscribed_symbols  # A candidate is not holding-price coverage.
        release.set()
        await enrollment
        rows = buffer.export()["records"]
        assert sum(r.get("status") == "requested" for r in rows) == 15
        assert sum(r.get("status") == "allocated" for r in rows) == 5
        scan = {"kind": "scan", "candidates": [
            {"candidate_id": f"scan:{s}", "symbol": s} for s in symbols]}
        assert _subscription_stream_complete([scan, *rows], datetime.now(timezone.utc),
                                             market_state_evidence=True)
    finally:
        release.set()
        await cleanup(owner, enrollment)


@pytest.mark.asyncio
@pytest.mark.parametrize("success", [True, False])
async def test_early_ack_waits_only_for_its_send_and_preserves_requested_order(success):
    owner, buffer, _, _, _ = setup_owner()
    first, first_done, second, second_done = [asyncio.Event() for _ in range(4)]

    async def send(action, key):
        if key[1] == "005930":
            first.set()
            await first_done.wait()
        else:
            second.set()
            await second_done.wait()

    gen = await owner.start(send)
    enroll = asyncio.create_task(owner.enroll("scan", ["005930", "000660"]))
    pending_ack = None
    try:
        await asyncio.wait_for(first.wait(), 1)
        pending_ack = asyncio.create_task(owner.handle_ack(ack((BOOK, "005930"), success=success), gen))
        await asyncio.sleep(0)
        assert not pending_ack.done()
        first_done.set()
        await asyncio.wait_for(second.wait(), 1)
        await asyncio.wait_for(asyncio.shield(pending_ack), 0.5)
        assert not enroll.done()
        statuses = [r["status"] for r in buffer.export()["records"]
                    if r.get("symbol") == "005930"]
        assert statuses == ["desired", "requested", "acknowledged" if success else "rejected"]
    finally:
        first_done.set()
        second_done.set()
        await cleanup(owner, enroll, pending_ack)


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["error", "cancel", "timeout"])
@pytest.mark.parametrize("success", [True, False])
async def test_early_ack_cannot_revive_uncertain_send(outcome, success):
    owner, buffer, _, _, _ = setup_owner(cap=1)
    entered, release = asyncio.Event(), asyncio.Event()
    if outcome == "timeout":
        owner.send_timeout = 0.05

    async def send(action, key):
        entered.set()
        await release.wait()
        raise RuntimeError("synthetic send failure")

    gen = await owner.start(send)
    enroll = asyncio.create_task(owner.enroll("scan", ["005930"]))
    pending_ack = None
    try:
        await asyncio.wait_for(entered.wait(), 1)
        pending_ack = asyncio.create_task(owner.handle_ack(ack((BOOK, "005930"), success=success), gen))
        await asyncio.sleep(0)
        if outcome == "cancel":
            enroll.cancel()
        elif outcome == "error":
            release.set()
        await asyncio.gather(enroll, return_exceptions=True)
        await asyncio.wait_for(pending_ack, 1)
        assert owner.snapshot()["occupied"] == 1
        assert owner.snapshot()["acknowledged"] == 0
        assert not owner.accepts_frame((BOOK, "005930"), gen)
        assert not buffer.export()["complete"]
        assert not any(r.get("status") in ("acknowledged", "rejected") for r in buffer.export()["records"])
    finally:
        await cleanup(owner, enroll, pending_ack)


@pytest.mark.asyncio
async def test_release_ack_returns_while_replacement_send_waits_and_second_ack_progresses():
    owner, _, _, sent, send = setup_owner(cap=2)
    gen = await owner.start(send)
    await owner.enroll("scan", ["005930", "000660"])
    for symbol in ("005930", "000660"):
        await owner.handle_ack(ack((BOOK, symbol)), gen)
    await owner.set_operational(["035420"], PRICE, BOOK)
    entered, release = asyncio.Event(), asyncio.Event()

    async def blocked_send(action, key):
        sent.append((action, key))
        if action == "subscribe" and key == (PRICE, "035420"):
            entered.set()
            await release.wait()

    owner._send = blocked_send
    first_ack = asyncio.create_task(owner.handle_ack(ack((BOOK, "005930"), "unsubscribe"), gen))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        await asyncio.wait_for(asyncio.shield(first_ack), 0.5)
        await asyncio.wait_for(owner.handle_ack(ack((BOOK, "000660"), "unsubscribe"), gen), 0.5)
        # Duplicate release cannot free another slot or spawn an extra registration.
        await owner.handle_ack(ack((BOOK, "000660"), "unsubscribe"), gen)
        release.set()
        await asyncio.wait_for(owner.maintain(), 1)
        assert sent.count(("subscribe", (PRICE, "035420"))) == 1
        assert sent.count(("subscribe", (BOOK, "035420"))) == 1
        assert owner.snapshot()["occupied"] == 2
        assert owner.snapshot()["retired_channels"] == 2
    finally:
        release.set()
        await cleanup(owner, first_ack)


@pytest.mark.asyncio
async def test_ack_progress_during_pacing_keeps_original_send_interval(monkeypatch):
    owner, _, clock, _, _ = setup_owner()
    owner.interval = 0.15
    pacing, release = asyncio.Event(), asyncio.Event()
    sent_at = []
    real_sleep = asyncio.sleep

    async def paced_sleep(delay):
        if delay > 0:
            pacing.set()
            await release.wait()
            clock[0] += delay
        else:
            await real_sleep(0)

    async def send(action, key):
        sent_at.append(clock[0])

    monkeypatch.setattr(asyncio, "sleep", paced_sleep)
    gen = await owner.start(send)
    enroll = asyncio.create_task(owner.enroll("scan", ["005930", "000660"]))
    try:
        await asyncio.wait_for(pacing.wait(), 1)
        await asyncio.wait_for(owner.handle_ack(ack((BOOK, "005930")), gen), 0.5)
        assert sent_at == [100.0]
        release.set()
        await enroll
        assert sent_at == pytest.approx([100.0, 100.15])
    finally:
        release.set()
        await cleanup(owner, enroll)


@pytest.mark.asyncio
async def test_ack_during_reconcile_causes_another_pass_without_maintenance():
    owner, _, _, sent, send = setup_owner(cap=2)
    entered, release, earlier_unsubscribe = [asyncio.Event() for _ in range(3)]

    async def tracked_send(action, key):
        await send(action, key)
        if action == "unsubscribe" and key == (BOOK, "000660"):
            entered.set()
            await release.wait()
        if action == "unsubscribe" and key == (BOOK, "005930"):
            earlier_unsubscribe.set()

    gen = await owner.start(tracked_send)
    await owner.enroll("scan", ["005930", "000660"])
    await owner.set_operational(["035420"], PRICE, BOOK)
    try:
        await owner.handle_ack(ack((BOOK, "000660")), gen)
        await asyncio.wait_for(entered.wait(), 1)
        await owner.handle_ack(ack((BOOK, "005930")), gen)
        release.set()
        # The worker already skipped this pending registration in its first pass.
        await asyncio.wait_for(earlier_unsubscribe.wait(), 1)
        assert sent.count(("unsubscribe", (BOOK, "005930"))) == 1
        assert sent.count(("unsubscribe", (BOOK, "000660"))) == 1
    finally:
        release.set()
        await cleanup(owner)


@pytest.mark.asyncio
async def test_disconnect_drains_ack_worker_early_ack_and_waiting_enrollment():
    owner, buffer, _, sent, send = setup_owner(cap=1)
    feed = make_feed()
    feed._quote_subscription_owner = owner
    feed._quote_lifecycle_lock = asyncio.Lock()
    feed._session = None

    class Socket:
        closed = False
        async def close(self):
            self.closed = True

    socket = feed._ws = Socket()
    feed._connected = True
    entered, release = asyncio.Event(), asyncio.Event()

    async def tracked_send(action, key):
        await send(action, key)
        if action == "subscribe" and key == (PRICE, "035420"):
            entered.set()
            await release.wait()

    gen = await owner.start(tracked_send)
    await owner.enroll("scan", ["005930"])
    await owner.handle_ack(ack((BOOK, "005930")), gen)
    await owner.set_operational(["035420"], PRICE, BOOK)
    await owner.handle_ack(ack((BOOK, "005930"), "unsubscribe"), gen)
    early_ack = None
    try:
        await asyncio.wait_for(entered.wait(), 1)
        early_ack = asyncio.create_task(owner.handle_ack(ack((PRICE, "035420")), gen))
        owner.submit("queued", ["000660"])
        await asyncio.sleep(0)
        assert not early_ack.done()
        await asyncio.wait_for(feed.disconnect(), 1)
        await asyncio.wait_for(early_ack, 1)
        assert socket.closed and not feed._connected
        assert not owner._tasks and owner._ack_reconcile_task is None
        assert owner.snapshot()["occupied"] == 0
        before = list(sent)
        await owner.handle_ack(ack((PRICE, "035420")), gen)
        await owner.maintain()
        assert sent == before
        assert not any(r.get("symbol") == "035420" and r.get("status") == "acknowledged"
                       for r in buffer.export()["records"])
    finally:
        release.set()
        await cleanup(owner, early_ack)


@pytest.mark.asyncio
async def test_ack_already_waiting_on_old_send_cannot_approve_reconnected_key():
    owner, _, _, _, send = setup_owner(cap=1)
    entered, release, waiting, resume_ack = [asyncio.Event() for _ in range(4)]
    key = (BOOK, "005930")

    async def held_send(action, channel):
        entered.set()
        await release.wait()

    gen = await owner.start(held_send)
    enroll = asyncio.create_task(owner.enroll("scan", [key[1]]))
    old_ack = None
    try:
        await asyncio.wait_for(entered.wait(), 1)
        event = owner._states[key].send_completed
        real_wait = event.wait

        async def paused_wait():
            waiting.set()
            await real_wait()
            await resume_ack.wait()

        event.wait = paused_wait
        old_ack = asyncio.create_task(owner.handle_ack(ack(key), gen))
        await asyncio.wait_for(waiting.wait(), 1)
        release.set()
        await enroll
        await owner.stop()
        new_gen = await owner.start(send)
        resume_ack.set()
        await old_ack
        assert owner.snapshot()["acknowledged"] == 0
        await owner.handle_ack(ack(key), new_gen)
        assert owner.snapshot()["acknowledged"] == 1
    finally:
        release.set()
        resume_ack.set()
        await cleanup(owner, enroll, old_ack)


@pytest.mark.asyncio
async def test_stop_before_worker_first_run_does_not_block_next_generation():
    owner, _, clock, sent, send = setup_owner(cap=1)
    gen = await owner.start(send)
    await owner.enroll("scan", ["005930"])
    await owner.handle_ack(ack((BOOK, "005930")), gen)
    # No yield between scheduling the worker and cancellation.
    await owner.stop()
    unsubscribed = asyncio.Event()

    async def tracked_send(action, key):
        await send(action, key)
        if action == "unsubscribe":
            unsubscribed.set()

    new_gen = await owner.start(tracked_send)
    try:
        clock[0] += 601
        await owner.handle_ack(ack((BOOK, "005930")), new_gen)
        await asyncio.wait_for(unsubscribed.wait(), 1)
        assert sent[-1] == ("unsubscribe", (BOOK, "005930"))
    finally:
        await cleanup(owner)


@pytest.mark.asyncio
async def test_ack_followup_failure_is_recorded_as_incomplete(monkeypatch):
    owner, buffer, _, _, send = setup_owner()
    gen = await owner.start(send)
    await owner.enroll("scan", ["005930"])
    failed = asyncio.Event()

    async def fail_reconcile():
        failed.set()
        raise RuntimeError("synthetic reconciliation failure")

    monkeypatch.setattr(owner, "_reconcile", fail_reconcile)
    try:
        await owner.handle_ack(ack((BOOK, "005930")), gen)
        await asyncio.wait_for(failed.wait(), 1)
        records = buffer.export()
        assert not records["complete"]
        assert any(r.get("reason") == "ack_reconcile_failed" for r in records["records"])
    finally:
        await cleanup(owner)


@pytest.mark.asyncio
async def test_later_ack_recovers_a_worker_cancelled_before_first_run():
    owner, _, clock, _, send = setup_owner(cap=2)
    released = asyncio.Event()
    unsubscribed = []

    async def tracked_send(action, key):
        await send(action, key)
        if action == "unsubscribe":
            unsubscribed.append(key)
            if len(unsubscribed) == 2:
                released.set()

    gen = await owner.start(tracked_send)
    await owner.enroll("scan", ["005930", "000660"])
    try:
        await owner.handle_ack(ack((BOOK, "005930")), gen)
        worker = owner._ack_reconcile_task
        worker.cancel()  # Fault injection: cancellation outside owned shutdown.
        await asyncio.gather(worker, return_exceptions=True)
        clock[0] += 601
        await owner.handle_ack(ack((BOOK, "000660")), gen)
        await asyncio.wait_for(released.wait(), 0.5)
        assert unsubscribed == [(BOOK, "005930"), (BOOK, "000660")]
    finally:
        await cleanup(owner)
