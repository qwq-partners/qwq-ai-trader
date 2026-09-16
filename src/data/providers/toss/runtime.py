"""Bounded, one-owner thread isolation. No credentials or network at import/startup.

Only this observer's supervisor awaits these futures. A stuck filesystem call
cannot be killed safely: ownership stays reserved until the thread really exits.
"""
from __future__ import annotations

import asyncio
from concurrent.futures import Future
from dataclasses import dataclass, fields, is_dataclass, replace
from datetime import date, datetime
from decimal import Decimal
from fractions import Fraction
import math
import threading
import time
from types import MappingProxyType
from collections.abc import Mapping

from .market_types import Quote


_CODES = frozenset({
    'approval_timeout', 'approval_unavailable', 'approval_consumed',
    'worker_busy', 'worker_owned', 'worker_unavailable', 'worker_timeout',
    'invalid_command', 'stopping',
})


class RuntimeUnavailable(Exception):
    def __init__(self, code):
        self.code = code if code in _CODES else 'worker_unavailable'
        super().__init__(self.code)


def _timeout(value):
    if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
        raise RuntimeUnavailable('worker_timeout')
    return value


async def _bounded_future(future, timeout):
    # wait_for must never cancel the worker/preflight's actual completion signal.
    wrapped = asyncio.wrap_future(future)
    # Retrieve late exceptions even if the supervisor is cancelled/times out.
    wrapped.add_done_callback(lambda f: None if f.cancelled() else f.exception())
    return await asyncio.wait_for(asyncio.shield(wrapped), _timeout(timeout))


class Preflight:
    """A single-use, non-secret file validation operation outside the main loop."""
    def __init__(self, loader):
        self._loader = loader
        self._future = Future()
        self.state = 'new'

    async def run(self, *, timeout):
        _timeout(timeout)
        if self.state != 'new':
            raise RuntimeUnavailable('approval_consumed')
        self.state = 'approval_pending'

        def work():
            try:
                value = self._loader()
            except BaseException:
                self._future.set_exception(RuntimeUnavailable('approval_unavailable'))
            else:
                self._future.set_result(value)

        thread = threading.Thread(target=work, name='toss-approval-preflight', daemon=True)
        try:
            thread.start()
            value = await _bounded_future(self._future, timeout)
        except asyncio.TimeoutError:
            self.state = 'unavailable'
            raise RuntimeUnavailable('approval_timeout') from None
        except asyncio.CancelledError:
            self.state = 'unavailable'
            raise
        except Exception:
            self.state = 'unavailable'
            raise RuntimeUnavailable('approval_unavailable') from None
        self.state = 'approved'
        return value


def _freeze(value, *, depth=0):
    """Copy only inert data, never engine/broker references or arbitrary objects."""
    if depth > 12:
        raise RuntimeUnavailable('invalid_command')
    if value is None or type(value) in (str, bool, int, float, Decimal, Fraction, date, datetime):
        return value
    if isinstance(value, Quote):
        # Quote is frozen, but validate the types of its fields as well.
        for item in fields(value):
            item_value = getattr(value, item.name)
            if isinstance(item_value, frozenset) and all(type(x) is str for x in item_value):
                continue
            _freeze(item_value, depth=depth + 1)
        return value
    if isinstance(value, Mapping) and len(value) <= 10000:
        if not all(type(k) is str for k in value):
            raise RuntimeUnavailable('invalid_command')
        return MappingProxyType({k: _freeze(v, depth=depth + 1) for k, v in value.items()})
    if type(value) in (list, tuple) and len(value) <= 10000:
        return tuple(_freeze(v, depth=depth + 1) for v in value)
    raise RuntimeUnavailable('invalid_command')


@dataclass(frozen=True)
class WorkerCommand:
    kind: str
    slot_id: str
    snapshot: Mapping | None = None
    requested_date: str | None = None

    def frozen_copy(self):
        if self.kind not in {'prices', 'calendar', 'missed'} or not isinstance(self.slot_id, str):
            raise RuntimeUnavailable('invalid_command')
        if not 1 <= len(self.slot_id) <= 128:
            raise RuntimeUnavailable('invalid_command')
        if self.requested_date is not None and type(self.requested_date) is not str:
            raise RuntimeUnavailable('invalid_command')
        return WorkerCommand(self.kind, self.slot_id, _freeze(self.snapshot), self.requested_date)


class TossWorker:
    """Exactly one active job, no unbounded queue; app objects live in its loop.

    app_factory(stop_event) constructs without I/O. app.start/run/close execute
    only on the worker loop. app.close owns OAuth-before-client close ordering.
    app authorization must check stop_event directly at each send boundary.
    """
    _owners = {}
    _owners_lock = threading.Lock()

    def __init__(self, app_factory, *, ownership_key):
        self._factory = app_factory
        self._key = ownership_key
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._ready = Future()
        self._finished = Future()
        self._loop = None
        self._main = None
        self._job = None
        self._pending = None
        self._app = None
        self._thread = None
        self._busy = False
        self.state = 'new'

    async def start(self, *, timeout):
        _timeout(timeout)
        with self._lock:
            if self.state != 'new':
                raise RuntimeUnavailable('worker_unavailable')
            with self._owners_lock:
                if self._key in self._owners:
                    raise RuntimeUnavailable('worker_owned')
                self._owners[self._key] = self
            self.state = 'starting'
            self._thread = threading.Thread(target=self._thread_main, name='toss-shadow-worker', daemon=True)
            try:
                self._thread.start()
            except Exception:
                self.state = 'unavailable'
                with self._owners_lock:
                    if self._owners.get(self._key) is self:
                        self._owners.pop(self._key, None)
                raise RuntimeUnavailable('worker_unavailable') from None
        try:
            await _bounded_future(self._ready, timeout)
        except asyncio.TimeoutError:
            self.request_stop()
            raise RuntimeUnavailable('worker_timeout') from None
        except asyncio.CancelledError:
            self.request_stop()
            raise

    def _thread_main(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        self._main = loop.create_task(self._serve())
        try:
            loop.run_until_complete(self._main)
        except BaseException:
            if not self._ready.done():
                self._ready.set_exception(RuntimeUnavailable('worker_unavailable'))
        finally:
            # _serve owns cancellation and cleanup. No replacement before this.
            loop.close()
            with self._lock:
                clean = self.state == 'cleanup_complete'
                self.state = 'cleanup_complete' if clean else 'stopping_unconfirmed'
            self._finished.set_result('closed' if clean else 'stopping_unconfirmed')

    async def _serve(self):
        try:
            if self._stop.is_set():
                return
            self._app = self._factory(self._stop)
            await self._app.start()
            if self._stop.is_set():
                return
            with self._lock:
                self.state = 'ready'
            self._ready.set_result(None)
            await asyncio.Event().wait()
        finally:
            self._stop.set()
            # Includes startup/bootstrap and library tasks; cancellation is once.
            current = asyncio.current_task()
            children = [task for task in asyncio.all_tasks() if task is not current and not task.done()]
            for task in children:
                task.cancel()
            if children:
                await asyncio.gather(*children, return_exceptions=True)
            if self._pending is not None and not self._pending.done():
                self._pending.set_exception(RuntimeUnavailable('stopping'))
            if self._app is not None:
                await self._app.close()
            if not self._ready.done():
                self._ready.set_exception(RuntimeUnavailable('worker_unavailable'))
            with self._lock:
                self.state = 'cleanup_complete'

    def submit(self, command):
        if not isinstance(command, WorkerCommand):
            raise RuntimeUnavailable('invalid_command')
        command = command.frozen_copy()
        with self._lock:
            if self.state != 'ready' or self._stop.is_set():
                raise RuntimeUnavailable('worker_unavailable')
            if self._busy:
                raise RuntimeUnavailable('worker_busy')
            self._busy = True
            result = Future()
            self._pending = result
            # A cancelled caller cannot leave a hidden issuance/query running.
            # Stop the owner rather than replacing it or retrying the job.
            result.add_done_callback(lambda future: self.request_stop() if future.cancelled() else None)
        self._loop.call_soon_threadsafe(self._launch, command, result)
        return result

    def _launch(self, command, result):
        if self._stop.is_set():
            if not result.done():
                result.set_exception(RuntimeUnavailable('stopping'))
            with self._lock:
                self._busy = False
            return
        self._job = self._loop.create_task(self._run(command, result))

    async def _run(self, command, result):
        try:
            value = await self._app.run(command)
            if not is_dataclass(value) or not getattr(value.__dataclass_params__, 'frozen', False):
                raise RuntimeUnavailable('worker_unavailable')
            # Results are frozen data, not a handle to worker-owned state.
            frozen_values = {item.name: _freeze(getattr(value, item.name)) for item in fields(value)}
            value = replace(value, **frozen_values)
            if not result.done():
                result.set_result(value)
        except BaseException:
            if not result.done():
                result.set_exception(RuntimeUnavailable('worker_unavailable'))
        finally:
            with self._lock:
                self._busy = False

    def request_stop(self):
        with self._lock:
            if self._stop.is_set():
                return
            self._stop.set()
            if self.state == 'new':
                self.state = 'closed'
                self._finished.set_result('closed')
                return
            self.state = 'stopping'
            loop, main = self._loop, self._main
        if loop is not None and main is not None and not loop.is_closed():
            loop.call_soon_threadsafe(main.cancel)

    async def stop(self, *, timeout):
        _timeout(timeout)
        deadline = time.monotonic() + timeout
        self.request_stop()
        try:
            state = await _bounded_future(self._finished, timeout)
        except asyncio.TimeoutError:
            self.state = 'stopping_unconfirmed'
            return self.state
        # Completion arrives just before thread exit. Confirm actual exit without
        # joining/blocking the main loop, and only then release ownership.
        if state == 'closed' and self._thread is not None:
            while self._thread.is_alive() and time.monotonic() < deadline:
                await asyncio.sleep(min(.002, max(0, deadline - time.monotonic())))
            if self._thread.is_alive():
                self.state = 'stopping_unconfirmed'
            else:
                with self._owners_lock:
                    if self._owners.get(self._key) is self:
                        self._owners.pop(self._key, None)
                self.state = 'closed'
        return self.state
