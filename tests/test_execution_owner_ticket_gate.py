"""Contract tests for the standalone owner ticket admission gate."""

import asyncio
from dataclasses import FrozenInstanceError

import pytest

from src.execution.safety.owner_ticket_gate import (
    OwnerTicketGate,
    OwnerTicketKind,
)


class Clock:
    def __init__(self, value: float = 0.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


class RaisingClock:
    def __init__(self, raising_calls: set[int]) -> None:
        self.calls = 0
        self.raising_calls = raising_calls

    def __call__(self) -> float:
        self.calls += 1
        if self.calls in self.raising_calls:
            raise RuntimeError(f"clock failed on call {self.calls}")
        return 0.0


async def record_ticket(
    gate: OwnerTicketGate, kind: OwnerTicketKind, label: str, order: list[str]
) -> None:
    async with gate.hold(kind):
        order.append(label)


def test_fifo_orders_writer_producer_writer() -> None:
    async def scenario() -> None:
        clock = Clock()
        gate = OwnerTicketGate(asyncio.Lock(), clock=clock)
        order: list[str] = []
        first = asyncio.create_task(
            record_ticket(gate, OwnerTicketKind.COMMIT, "writer-1", order)
        )
        producer = asyncio.create_task(
            record_ticket(gate, OwnerTicketKind.PRODUCER_VIEW, "producer", order)
        )
        last = asyncio.create_task(
            record_ticket(gate, OwnerTicketKind.COMMIT, "writer-2", order)
        )
        await asyncio.gather(first, producer, last)
        assert order == ["writer-1", "producer", "writer-2"]

    asyncio.run(scenario())


def test_cancelled_queued_ticket_is_skipped_without_reordering_survivors() -> None:
    async def scenario() -> None:
        gate = OwnerTicketGate(asyncio.Lock(), clock=Clock())
        entered = asyncio.Event()
        release = asyncio.Event()
        order: list[str] = []

        async def first() -> None:
            async with gate.hold(OwnerTicketKind.COMMIT):
                entered.set()
                await release.wait()
                order.append("first")

        async def queued(label: str) -> None:
            async with gate.hold(OwnerTicketKind.COMMIT):
                order.append(label)

        first_task = asyncio.create_task(first())
        await entered.wait()
        cancelled = asyncio.create_task(queued("cancelled"))
        survivor = asyncio.create_task(queued("survivor"))
        await asyncio.sleep(0)
        cancelled.cancel()
        with pytest.raises(asyncio.CancelledError):
            await cancelled
        release.set()
        await asyncio.gather(first_task, survivor)
        assert order == ["first", "survivor"]

    asyncio.run(scenario())


def test_cancelled_active_ticket_keeps_gate_until_submitted_store_drains() -> None:
    async def scenario() -> None:
        gate = OwnerTicketGate(asyncio.Lock(), clock=Clock())
        entered = asyncio.Event()
        drain_release = asyncio.Event()
        store_cancelled = asyncio.Event()
        follower_entered = asyncio.Event()

        async def store() -> None:
            try:
                await drain_release.wait()
            except asyncio.CancelledError:
                store_cancelled.set()
                raise

        async def active() -> None:
            async with gate.hold(OwnerTicketKind.COMMIT) as ticket:
                gate.register_submitted_drain(ticket, asyncio.create_task(store()))
                entered.set()
                await asyncio.Event().wait()

        async def follower() -> None:
            async with gate.hold(OwnerTicketKind.COMMIT):
                follower_entered.set()

        active_task = asyncio.create_task(active())
        await entered.wait()
        follower_task = asyncio.create_task(follower())
        active_task.cancel()
        await asyncio.sleep(0)
        assert not follower_entered.is_set()
        assert not store_cancelled.is_set()
        drain_release.set()
        with pytest.raises(asyncio.CancelledError):
            await active_task
        await follower_task
        assert follower_entered.is_set()
        assert not store_cancelled.is_set()

    asyncio.run(scenario())


def test_repeated_cancellation_during_drain_keeps_gate_owned() -> None:
    async def scenario() -> None:
        gate = OwnerTicketGate(asyncio.Lock(), clock=Clock())
        entered = asyncio.Event()
        drain_release = asyncio.Event()
        follower_entered = asyncio.Event()

        async def active() -> None:
            async with gate.hold(OwnerTicketKind.COMMIT) as ticket:
                gate.register_submitted_drain(
                    ticket, asyncio.create_task(drain_release.wait())
                )
                entered.set()
                await asyncio.Event().wait()

        async def follower() -> None:
            async with gate.hold(OwnerTicketKind.COMMIT):
                follower_entered.set()

        active_task = asyncio.create_task(active())
        await entered.wait()
        follower_task = asyncio.create_task(follower())
        active_task.cancel()
        await asyncio.sleep(0)
        active_task.cancel()
        await asyncio.sleep(0)
        assert not follower_entered.is_set()
        drain_release.set()
        with pytest.raises(asyncio.CancelledError):
            await active_task
        await follower_task

    asyncio.run(scenario())


def test_hold_overrun_records_failure_without_cancelling_store() -> None:
    async def scenario() -> None:
        clock = Clock()
        gate = OwnerTicketGate(asyncio.Lock(), clock=clock)
        release_store = asyncio.Event()
        store_cancelled = False

        async def store() -> None:
            nonlocal store_cancelled
            try:
                await release_store.wait()
                clock.value = 1.501
            except asyncio.CancelledError:
                store_cancelled = True
                raise

        async with gate.hold(OwnerTicketKind.COMMIT) as ticket:
            gate.register_submitted_drain(ticket, asyncio.create_task(store()))
            release_store.set()
        metrics = gate.metrics_snapshot()
        assert metrics.completed[OwnerTicketKind.COMMIT] == 1
        assert metrics.overruns[OwnerTicketKind.COMMIT] == 1
        assert not store_cancelled

    asyncio.run(scenario())


def test_one_apply_ticket_covers_receive_and_fill_commits() -> None:
    async def scenario() -> None:
        clock = Clock()
        gate = OwnerTicketGate(asyncio.Lock(), clock=clock)
        commits: list[str] = []
        receive_release = asyncio.Event()
        fill_release = asyncio.Event()

        async def receive_commit() -> None:
            await receive_release.wait()
            commits.append("receive")
            clock.value = 1.25

        async def fill_commit() -> None:
            await fill_release.wait()
            commits.append("fill")
            clock.value = 2.5

        async with gate.hold(OwnerTicketKind.FILL_APPLY) as ticket:
            receive = asyncio.create_task(receive_commit())
            gate.register_submitted_drain(ticket, receive)
            receive_release.set()
            await receive
            fill = asyncio.create_task(fill_commit())
            gate.register_submitted_drain(ticket, fill)
            fill_release.set()
        metrics = gate.metrics_snapshot()
        assert commits == ["receive", "fill"]
        assert metrics.completed[OwnerTicketKind.FILL_APPLY] == 1
        assert metrics.overruns[OwnerTicketKind.FILL_APPLY] == 0

    asyncio.run(scenario())


def test_cancellation_while_ready_to_acquire_does_not_strand_lock_or_queue() -> None:
    async def scenario() -> None:
        lock = asyncio.Lock()
        await lock.acquire()
        gate = OwnerTicketGate(lock, clock=Clock())
        blocked = asyncio.create_task(record_ticket(gate, OwnerTicketKind.COMMIT, "bad", []))
        await asyncio.sleep(0)
        blocked.cancel()
        with pytest.raises(asyncio.CancelledError):
            await blocked
        lock.release()
        order: list[str] = []
        await record_ticket(gate, OwnerTicketKind.COMMIT, "good", order)
        assert order == ["good"]
        assert not lock.locked()

    asyncio.run(scenario())


def test_body_and_store_errors_do_not_strand_lock() -> None:
    async def scenario() -> None:
        gate = OwnerTicketGate(asyncio.Lock(), clock=Clock())

        async def broken_body() -> None:
            async with gate.hold(OwnerTicketKind.LOOKUP) as ticket:
                async def broken_store() -> None:
                    raise RuntimeError("store failed")

                gate.register_submitted_drain(ticket, asyncio.create_task(broken_store()))
                raise ValueError("body failed")

        with pytest.raises(ValueError, match="body failed"):
            await broken_body()
        async with gate.hold(OwnerTicketKind.PRODUCER_VIEW):
            pass
        assert not gate.lock.locked()

    asyncio.run(scenario())


def test_lock_identity_and_metrics_snapshot_are_immutable_and_bounded() -> None:
    async def scenario() -> None:
        lock = asyncio.Lock()
        gate = OwnerTicketGate(lock, clock=Clock())
        assert gate.lock is lock
        snapshot = gate.metrics_snapshot()
        assert set(snapshot.completed) == set(OwnerTicketKind)
        with pytest.raises(TypeError):
            snapshot.completed[OwnerTicketKind.COMMIT] = 99  # type: ignore[index]

    asyncio.run(scenario())


def test_drain_registration_rejects_foreign_loop_and_owner_task() -> None:
    async def scenario() -> None:
        gate = OwnerTicketGate(asyncio.Lock(), clock=Clock())
        foreign_loop = asyncio.new_event_loop()
        foreign_drain = foreign_loop.create_future()
        try:
            async with gate.hold(OwnerTicketKind.COMMIT) as ticket:
                with pytest.raises(ValueError, match="same event loop"):
                    gate.register_submitted_drain(ticket, foreign_drain)
                owner_task = asyncio.current_task()
                assert owner_task is not None
                with pytest.raises(ValueError, match="owner task"):
                    gate.register_submitted_drain(ticket, owner_task)
        finally:
            foreign_drain.cancel()
            foreign_loop.close()

    asyncio.run(scenario())


def test_ticket_handle_is_immutable_and_cannot_change_cleanup_attribution() -> None:
    async def scenario() -> None:
        gate = OwnerTicketGate(asyncio.Lock(), clock=Clock())
        async with gate.hold(OwnerTicketKind.COMMIT) as ticket:
            with pytest.raises(FrozenInstanceError):
                ticket.kind = OwnerTicketKind.LOOKUP  # type: ignore[misc]
        metrics = gate.metrics_snapshot()
        assert metrics.completed[OwnerTicketKind.COMMIT] == 1
        assert metrics.completed[OwnerTicketKind.LOOKUP] == 0
        async with gate.hold(OwnerTicketKind.PRODUCER_VIEW):
            pass
        assert not gate.lock.locked()

    asyncio.run(scenario())


def test_raising_entry_or_cleanup_clock_releases_and_advances_queue() -> None:
    async def scenario() -> None:
        entry_gate = OwnerTicketGate(asyncio.Lock(), clock=RaisingClock({1}))
        with pytest.raises(RuntimeError, match="call 1"):
            async with entry_gate.hold(OwnerTicketKind.COMMIT):
                pytest.fail("entry clock must fail before the body")
        assert not entry_gate.lock.locked()
        async with entry_gate.hold(OwnerTicketKind.COMMIT):
            pass
        assert not entry_gate.lock.locked()

        cleanup_gate = OwnerTicketGate(asyncio.Lock(), clock=RaisingClock({2}))
        first_entered = asyncio.Event()
        release_first = asyncio.Event()
        follower_entered = asyncio.Event()

        async def first() -> None:
            async with cleanup_gate.hold(OwnerTicketKind.COMMIT):
                first_entered.set()
                await release_first.wait()

        async def follower() -> None:
            async with cleanup_gate.hold(OwnerTicketKind.PRODUCER_VIEW):
                follower_entered.set()

        first_task = asyncio.create_task(first())
        await first_entered.wait()
        follower_task = asyncio.create_task(follower())
        release_first.set()
        with pytest.raises(RuntimeError, match="call 2"):
            await first_task
        await follower_task
        assert follower_entered.is_set()
        assert not cleanup_gate.lock.locked()

    asyncio.run(scenario())
