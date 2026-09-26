"""Fair, standalone admission for serialized owner work.

The supplied lock remains the actual mutex and is exposed unchanged through
``lock``.  Callers enter ``hold(kind)`` and may register an *already submitted*
``asyncio.Task`` or ``asyncio.Future`` with
``register_submitted_drain(ticket, drain)`` while that ticket is active.  The
gate retains that future and does not release the supplied lock until it has
settled, including when the holding caller is cancelled.  Registration is
closed when context exit begins, so an owner route must register its drain
immediately after submission and before any cancellation point.

This module deliberately does not start work, cancel drains, or wire itself
into an owner runtime.  It only serializes callers that explicitly use it.
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from enum import Enum
from time import monotonic
from types import MappingProxyType
from typing import Any


class OwnerTicketKind(str, Enum):
    """The only owner ticket classes and their whole-ticket budgets."""

    LOOKUP = "lookup"
    COMMIT = "commit"
    FILL_APPLY = "fill_apply"
    RESTORE = "restore"
    PRODUCER_VIEW = "producer_view"


_BUDGET_SECONDS: Mapping[OwnerTicketKind, float] = MappingProxyType(
    {
        OwnerTicketKind.LOOKUP: 1.000,
        OwnerTicketKind.COMMIT: 1.500,
        OwnerTicketKind.FILL_APPLY: 2.500,
        OwnerTicketKind.RESTORE: 1.500,
        OwnerTicketKind.PRODUCER_VIEW: 0.005,
    }
)


@dataclass(frozen=True)
class OwnerTicketMetrics:
    """A bounded, immutable aggregate snapshot; no ticket history is kept."""

    completed: Mapping[OwnerTicketKind, int]
    overruns: Mapping[OwnerTicketKind, int]


@dataclass(frozen=True, slots=True)
class OwnerTicket:
    """Immutable caller-visible handle for one active admission.

    A ticket is yielded only after it owns the supplied lock.  Gate lifecycle
    state is deliberately not stored on this handle: callers cannot change
    queue, drain, timing, or accounting behavior through the yielded value.
    """

    kind: OwnerTicketKind

    @property
    def budget_seconds(self) -> float:
        """The fixed elapsed hold budget, excluding time queued."""
        return _BUDGET_SECONDS[self.kind]


@dataclass(slots=True)
class _TicketState:
    """Gate-private lifecycle state corresponding to one immutable handle."""

    ticket: OwnerTicket
    kind: OwnerTicketKind
    ready: asyncio.Future[None]
    _started_at: float | None = None
    _active: bool = False
    _drains_open: bool = True
    _drains: list[asyncio.Future[Any]] = field(default_factory=list)


class OwnerTicketGate:
    """Ticket-FIFO wrapper around one existing :class:`asyncio.Lock`.

    ``register_submitted_drain`` accepts only a task/future that has already
    been submitted by the caller.  Its result (including an exception) is
    observed only to settle ownership; it is never cancelled or re-raised by
    this gate.  The submitting owner remains responsible for the operation's
    semantic result.
    """

    def __init__(
        self, lock: asyncio.Lock, *, clock: Callable[[], float] = monotonic
    ) -> None:
        self._lock = lock
        self._clock = clock
        self._queue: deque[_TicketState] = deque()
        self._active_state: _TicketState | None = None
        self._completed = {kind: 0 for kind in OwnerTicketKind}
        self._overruns = {kind: 0 for kind in OwnerTicketKind}

    @property
    def lock(self) -> asyncio.Lock:
        """The exact lock object supplied to the gate (identity preserved)."""
        return self._lock

    @asynccontextmanager
    async def hold(self, kind: OwnerTicketKind) -> AsyncIterator[OwnerTicket]:
        """Queue a ticket and hold the original lock through its drain.

        Cancelling while queued (or while acquiring the underlying lock)
        removes this ticket and preserves survivor order.  Once active, caller
        cancellation cannot make the next ticket ready until all registered
        drains have settled.
        """
        state = self._enqueue(kind)
        try:
            # Shield only the queue signal: cancelling a queued owner must not
            # cancel the shared signal that wakes its surviving successor.
            await asyncio.shield(state.ready)
            await self._lock.acquire()
            # Establish cleanup ownership before invoking the injected clock:
            # an entry-clock error still has to release this acquired lock.
            state._active = True
            self._active_state = state
            state._started_at = self._clock()
            yield state.ticket
        finally:
            if state._active:
                await self._finish_active_ticket(state)
            else:
                self._skip(state)

    def register_submitted_drain(
        self, ticket: OwnerTicket, drain: asyncio.Future[Any]
    ) -> None:
        """Retain an already-submitted task/future until this hold is released.

        The caller must call this synchronously immediately after submitting a
        store operation, while ``ticket`` is still the active ticket.  Coroutines
        are rejected because accepting one would let the gate create work rather
        than merely protect submitted work.
        """
        if not isinstance(drain, asyncio.Future):
            raise TypeError("drain must be an already-submitted asyncio Task or Future")
        if drain.get_loop() is not asyncio.get_running_loop():
            raise ValueError("drain must belong to the same event loop as the owner")
        if drain is asyncio.current_task():
            raise ValueError("drain must not be the current owner task")
        state = self._active_state
        if state is None or state.ticket is not ticket or not state._active:
            raise RuntimeError("drain registration requires the active owner ticket")
        if not state._drains_open:
            raise RuntimeError("drain registration is closed for this owner ticket")
        state._drains.append(drain)

    def metrics_snapshot(self) -> OwnerTicketMetrics:
        """Return fixed-cardinality counters detached from mutable gate state."""
        return OwnerTicketMetrics(
            completed=MappingProxyType(dict(self._completed)),
            overruns=MappingProxyType(dict(self._overruns)),
        )

    def _enqueue(self, kind: OwnerTicketKind) -> _TicketState:
        if not isinstance(kind, OwnerTicketKind):
            raise TypeError("kind must be an OwnerTicketKind")
        ticket = OwnerTicket(kind=kind)
        state = _TicketState(
            ticket=ticket,
            kind=kind,
            ready=asyncio.get_running_loop().create_future(),
        )
        self._queue.append(state)
        self._advance_queue()
        return state

    def _skip(self, state: _TicketState) -> None:
        """Mark a non-active cancelled ticket absent and wake the next survivor."""
        state._drains_open = False
        try:
            self._queue.remove(state)
        except ValueError:
            pass
        self._advance_queue()

    def _advance_queue(self) -> None:
        if self._active_state is not None or not self._queue:
            return
        head = self._queue[0]
        if not head.ready.done():
            head.ready.set_result(None)

    async def _finish_active_ticket(self, state: _TicketState) -> None:
        """Drain without allowing repeated caller cancellation to release early."""
        state._drains_open = False
        cancelled_while_draining = False
        accounting_error: BaseException | None = None
        try:
            cancelled_while_draining = await self._settle_drains(state)
        finally:
            try:
                self._record_hold(state)
            except BaseException as exc:
                # Surface accounting failure after safety cleanup.  An entry
                # clock failure has no start time and remains the original body
                # exception, without attempting a second clock call here.
                accounting_error = exc
            finally:
                state._active = False
                self._active_state = None
                try:
                    self._lock.release()
                finally:
                    self._skip(state)
        if accounting_error is not None:
            raise accounting_error
        if cancelled_while_draining:
            raise asyncio.CancelledError

    def _record_hold(self, state: _TicketState) -> None:
        """Account only successfully clocked entries; this must not own cleanup."""
        if state._started_at is None:
            return
        elapsed = self._clock() - state._started_at
        self._completed[state.kind] += 1
        if elapsed > _BUDGET_SECONDS[state.kind]:
            self._overruns[state.kind] += 1

    async def _settle_drains(self, state: _TicketState) -> bool:
        if not state._drains:
            return False
        waiter = asyncio.gather(*state._drains, return_exceptions=True)
        cancelled_while_draining = False
        while not waiter.done():
            try:
                await asyncio.shield(waiter)
            except asyncio.CancelledError:
                # A caller may be cancelled again while its previously submitted
                # store work drains.  Keep the lock and wait; the cancellation
                # that initiated context exit still propagates after finalization.
                cancelled_while_draining = True
                continue
        return cancelled_while_draining


__all__ = ["OwnerTicket", "OwnerTicketGate", "OwnerTicketKind", "OwnerTicketMetrics"]
