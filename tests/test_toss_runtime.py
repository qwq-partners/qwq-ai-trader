"""Thread/preflight isolation tests: fake apps, no token/API/production paths."""
import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
import threading
import time

import pytest

from src.data.providers.toss.runtime import (
    Preflight, RuntimeUnavailable, TossWorker, WorkerCommand,
)


async def eventually(predicate):
    for _ in range(200):
        if predicate():
            return
        await asyncio.sleep(.002)
    assert predicate()


@dataclass(frozen=True)
class Result:
    outcome: str = 'success'


class App:
    def __init__(self, stop, *, block=None):
        self.stop = stop
        self.block = block
        self.thread = threading.get_ident()
        self.events = []

    async def start(self):
        self.events.append('start')

    async def run(self, command):
        self.events.append(command)
        if self.block:
            self.block.wait(2)  # deliberately model a blocked fsync in worker
        return Result()

    async def close(self):
        self.events.append('oauth_closed')
        self.events.append('query_closed')


def test_worker_lazy_single_thread_bounded_queue_and_shutdown():
    async def run():
        made = []
        gate = threading.Event()

        def factory(stop):
            app = App(stop, block=gate)
            made.append(app)
            return app

        worker = TossWorker(factory, ownership_key='test-lazy')
        assert not made and worker.state == 'new'
        await worker.start(timeout=1)
        assert made[0].thread != threading.get_ident()
        command = WorkerCommand('prices', 'slot', {'symbols': ['005930']})
        result = worker.submit(command)
        with pytest.raises(RuntimeUnavailable, match='worker_busy'):
            worker.submit(command)
        # Even a synchronous fsync stall must leave the parent event loop alive.
        ticks = 0
        for _ in range(5):
            await asyncio.sleep(.001)
            ticks += 1
        assert ticks == 5
        gate.set()
        assert (await asyncio.wrap_future(result)).outcome == 'success'
        assert await worker.stop(timeout=1) == 'closed'
        assert made[0].events[-2:] == ['oauth_closed', 'query_closed']
        with pytest.raises(RuntimeUnavailable):
            worker.submit(command)
    asyncio.run(run())


def test_worker_freezes_inputs_rejects_arbitrary_references():
    async def run():
        seen = []

        class CopyApp(App):
            async def run(self, command):
                seen.append(command)
                return Result()

        worker = TossWorker(lambda stop: CopyApp(stop), ownership_key='test-copy')
        await worker.start(timeout=1)
        snapshot = {'symbols': ['005930'], 'selected_at': datetime.now(timezone.utc)}
        result = worker.submit(WorkerCommand('prices', 'slot', snapshot))
        snapshot['symbols'].append('000660')
        await asyncio.wrap_future(result)
        assert seen[0].snapshot['symbols'] == ('005930',)
        with pytest.raises(TypeError):
            seen[0].snapshot['symbols'] = ()
        with pytest.raises(RuntimeUnavailable, match='invalid_command'):
            worker.submit(WorkerCommand('prices', 'slot2', {'broker': object()}))
        await worker.stop(timeout=1)
    asyncio.run(run())


def test_stalled_stop_does_not_release_owner_or_start_replacement():
    async def run():
        gate = threading.Event()
        made = []

        def factory(stop):
            app = App(stop, block=gate)
            made.append(app)
            return app

        worker = TossWorker(factory, ownership_key='test-stall')
        await worker.start(timeout=1)
        worker.submit(WorkerCommand('prices', 'slot', {'symbols': ['005930']}))
        await eventually(lambda: len(made[0].events) == 2)
        assert await worker.stop(timeout=.01) == 'stopping_unconfirmed'
        replacement = TossWorker(factory, ownership_key='test-stall')
        with pytest.raises(RuntimeUnavailable, match='worker_owned'):
            await replacement.start(timeout=.1)
        gate.set()
        assert await worker.stop(timeout=1) == 'closed'
    asyncio.run(run())


def test_start_cancellation_closes_partial_app_bootstrap_before_sender():
    async def run():
        events = []

        class BootApp(App):
            async def start(self):
                events.append('bootstrap')
                try:
                    await asyncio.Event().wait()
                finally:
                    events.append('issuance_unknown_preserved')

            async def close(self):
                events.extend(['oauth_closed', 'sender_released'])

        worker = TossWorker(lambda stop: BootApp(stop), ownership_key='test-start-cancel')
        startup = asyncio.create_task(worker.start(timeout=2))
        await eventually(lambda: events == ['bootstrap'])
        startup.cancel()
        with pytest.raises(asyncio.CancelledError):
            await startup
        assert await worker.stop(timeout=1) == 'closed'
        assert events == ['bootstrap', 'issuance_unknown_preserved', 'oauth_closed', 'sender_released']
    asyncio.run(run())


def test_preflight_is_bounded_single_use_and_late_success_discarded():
    async def run():
        gate = threading.Event()
        calls = []

        def loader():
            calls.append(threading.get_ident())
            gate.wait(1)
            return object()

        preflight = Preflight(loader)
        assert calls == []
        with pytest.raises(RuntimeUnavailable, match='approval_timeout'):
            await preflight.run(timeout=.01)
        assert calls[0] != threading.get_ident()
        with pytest.raises(RuntimeUnavailable, match='approval_consumed'):
            await preflight.run(timeout=.01)
        gate.set()
        await asyncio.sleep(.02)
        assert preflight.state == 'unavailable'
    asyncio.run(run())


def test_errors_are_sanitized_not_worker_credentials():
    async def run():
        def fail():
            raise ValueError('a-secret-value')
        with pytest.raises(RuntimeUnavailable) as error:
            await Preflight(fail).run(timeout=1)
        assert str(error.value) == 'approval_unavailable'
        assert error.value.__cause__ is None
    asyncio.run(run())


def test_old_repeated_stop_cannot_erase_a_new_workers_ownership():
    async def run():
        old = TossWorker(lambda stop: App(stop), ownership_key='test-repeat-owner')
        replacement = TossWorker(lambda stop: App(stop), ownership_key='test-repeat-owner')
        third = TossWorker(lambda stop: App(stop), ownership_key='test-repeat-owner')
        try:
            await old.start(timeout=1)
            assert await old.stop(timeout=1) == 'closed'
            await replacement.start(timeout=1)
            assert await old.stop(timeout=1) == 'closed'
            with pytest.raises(RuntimeUnavailable, match='worker_owned'):
                await third.start(timeout=1)
        finally:
            await replacement.stop(timeout=1)
            await third.stop(timeout=1)
    asyncio.run(run())


def test_cancelled_submission_stops_job_before_later_send():
    async def run():
        events = []

        class WaitingApp(App):
            async def run(self, command):
                events.append('waiting')
                try:
                    await asyncio.sleep(1)
                    events.append('sent')
                    return Result()
                except asyncio.CancelledError:
                    events.append('cancelled')
                    raise

        worker = TossWorker(lambda stop: WaitingApp(stop), ownership_key='test-job-cancel')
        await worker.start(timeout=1)
        future = worker.submit(WorkerCommand('prices', 'slot', {}))
        await eventually(lambda: events == ['waiting'])
        future.cancel()
        await asyncio.sleep(.01)
        assert worker.state != 'ready'
        assert await worker.stop(timeout=1) == 'closed'
        assert events == ['waiting', 'cancelled']
    asyncio.run(run())
