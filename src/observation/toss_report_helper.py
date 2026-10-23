"""Sealed, single-life report authority with bounded JSONL input/output only."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import os
import signal
import sys
import time
from zoneinfo import ZoneInfo

from src.data.providers.toss.report_protocol import (
    REQUEST_MAX_BYTES, RESPONSE_MAX_BYTES, REQUEST_TIMEOUT_SECONDS,
    canonical_bytes, strict_loads, validate_request, report_snapshot, make_result,
)
from src.data.providers.toss.report_quotes import ReportQuotes
from src.data.providers.toss.runtime import Preflight, TossWorker, WorkerCommand
from src.data.providers.toss.runtime_factory import build_app, command_deadline

KST = ZoneInfo('Asia/Seoul')


async def read_request(reader):
    try:
        raw = await reader.readuntil(b'\n')
    except asyncio.IncompleteReadError as error:
        if not error.partial:
            return None
        raise ValueError('request_incomplete') from None
    except asyncio.LimitOverrunError:
        raise ValueError('request_oversized') from None
    if len(raw) > REQUEST_MAX_BYTES + 1:
        raise ValueError('request_oversized')
    return strict_loads(raw[:-1], maximum=REQUEST_MAX_BYTES)


async def _stdio():
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader(limit=REQUEST_MAX_BYTES + 1)
    transport, _ = await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin.buffer)
    writer_transport, protocol = await loop.connect_write_pipe(asyncio.streams.FlowControlMixin, sys.stdout.buffer)
    writer = asyncio.StreamWriter(writer_transport, protocol, None, loop)
    return reader, writer, transport


class _ReportApp:
    """Bind startup OAuth to the same report window as the following GET.

    The underlying worker app scopes normal queries itself. Its initial token
    request also needs the original command cutoff, before any network send.
    """
    def __init__(self, app, command, deadline):
        self.app, self.command, self.deadline = app, command, deadline

    async def start(self):
        token = self.app.query_scope.set((self.command, self.deadline))
        try:
            await self.app.start()
        finally:
            self.app.query_scope.reset(token)

    async def run(self, command):
        return await self.app.run(command)

    async def close(self):
        await self.app.close()


async def _execute(request, *, authority, worker, quotes, deadline, now, clock):
    policy = authority.plan.document
    cohort = validate_request(request, now=now(), policy=policy)
    stamp = now().astimezone(KST)
    slot = stamp.replace(hour=16, minute=0, second=0, microsecond=0).isoformat()
    command = WorkerCommand('prices', slot, report_snapshot(cohort, now=now(), policy=policy))
    query_deadline = min(deadline, command_deadline(command, policy, now=now(), clock=clock))
    future = asyncio.wrap_future(worker.submit(command))
    future.add_done_callback(lambda done: None if done.cancelled() else done.exception())
    outcome = await asyncio.wait_for(asyncio.shield(future), max(.001, query_deadline - clock()))
    if not quotes.publish(outcome, command=command, deadline=query_deadline):
        return make_result(request, quotes=(), valid_until=None, status='unavailable')
    result = tuple(q for _, symbol in cohort.members if (q := quotes.get_quote(symbol)) is not None)
    if not result or clock() >= deadline:
        return make_result(request, quotes=(), valid_until=None, status='unavailable')
    authority.require('report_display', deadline=deadline)
    end = stamp.replace(hour=16, minute=5, second=0, microsecond=0)
    expiry = min(datetime.fromisoformat(authority.grant.expires_at), end,
                 *(q.observed_at + timedelta(seconds=60) for q in result),
                 *(q.fetched_at + timedelta(seconds=60) for q in result))
    if expiry <= now():
        return make_result(request, quotes=(), valid_until=None, status='unavailable')
    return make_result(request, quotes=result, valid_until=expiry)


async def run_report_helper(*, deployment, settings, claim_start, reader=None, writer=None,
                            stop_event=None, now=None, clock=None):
    """No authority crosses this process boundary; failures end this lifetime."""
    flag = os.environ.get('TOSS_API', '0')
    if flag in {'0', ''}:
        return 0
    if flag != '1':
        return 1
    now = now or (lambda: datetime.now(timezone.utc))
    clock = clock or time.monotonic
    stop = stop_event if stop_event is not None else asyncio.Event()
    authority = worker = quotes = incoming = job = stopping = input_transport = None
    handlers, seen = [], set()
    code = 0
    loop = asyncio.get_running_loop()
    try:
        if stop_event is None:
            for signum in (signal.SIGINT, signal.SIGTERM):
                loop.add_signal_handler(signum, stop.set)
                handlers.append(signum)
        authority = await Preflight(deployment.load).run(timeout=deployment.preflight_timeout_seconds)
        if reader is None or writer is None:
            reader, writer, input_transport = await _stdio()
        quotes = ReportQuotes(authority, enabled=lambda: not stop.is_set() and os.environ.get('TOSS_API') == '1')
        incoming = asyncio.create_task(read_request(reader))
        stopping = asyncio.create_task(stop.wait())
        while not stop.is_set() and os.environ.get('TOSS_API') == '1':
            authority.require('query', deadline=clock() + 1)
            done, _ = await asyncio.wait((incoming, stopping), timeout=1, return_when=asyncio.FIRST_COMPLETED)
            if stopping in done:
                break
            if incoming not in done:
                continue
            request = incoming.result()
            incoming = None
            if request is None:
                break
            cohort = validate_request(request, now=now(), policy=authority.plan.document)
            day = cohort.report_date.isoformat()
            if day in seen:
                raise ValueError('duplicate_report_slot')
            seen.add(day)  # claim an attempt before any token/ledger operation
            requested_at = datetime.fromisoformat(request['requested_at'])
            remaining = REQUEST_TIMEOUT_SECONDS - (now() - requested_at).total_seconds()
            session_end = now().astimezone(KST).replace(hour=16, minute=5, second=0, microsecond=0)
            remaining = min(remaining, (session_end - now()).total_seconds())
            deadline = authority.require('report_display', deadline=clock() + remaining)
            # Monitor EOF and prohibited overlapping input while startup/query is active.
            incoming = asyncio.create_task(read_request(reader))

            async def operation():
                nonlocal worker
                if worker is None:
                    await Preflight(lambda: claim_start(authority)).run(timeout=min(5, max(.001, deadline - clock())))
                    startup_slot = now().astimezone(KST).replace(hour=16, minute=0, second=0, microsecond=0).isoformat()
                    startup_command = WorkerCommand('prices', startup_slot,
                        report_snapshot(cohort, now=now(), policy=authority.plan.document))
                    worker = TossWorker(lambda stopping: _ReportApp(build_app(authority, stopping),
                        startup_command, deadline), ownership_key=authority.grant.client_identity)
                    await worker.start(timeout=min(20, max(.001, deadline - clock())))
                return await _execute(request, authority=authority, worker=worker,
                                      quotes=quotes, deadline=deadline, now=now, clock=clock)

            job = asyncio.create_task(operation())
            done, _ = await asyncio.wait((job, incoming, stopping),
                timeout=max(0, deadline - clock()), return_when=asyncio.FIRST_COMPLETED)
            if incoming in done or stopping in done or job not in done:
                # Parent death/cancellation and pipelined commands never publish a late result.
                raise ValueError('report_interrupted')
            response = job.result()
            job = None
            if clock() >= deadline or stop.is_set():
                raise ValueError('report_expired')
            authority.require('report_display', deadline=deadline)
            payload = canonical_bytes(response)
            if len(payload) > RESPONSE_MAX_BYTES:
                raise ValueError('response_oversized')
            writer.write(payload + b'\n')
            await asyncio.wait_for(writer.drain(), max(.001, deadline - clock()))
    except asyncio.CancelledError:
        code = 1
    except Exception:
        code = 1
    finally:
        stop.set()
        if quotes is not None:
            quotes.clear()
        if authority is not None:
            authority.stop()
        tasks = [task for task in (job, incoming, stopping) if task is not None]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if worker is not None:
            try:
                if await worker.stop(timeout=10) != 'closed':
                    code = 1
            except BaseException:
                worker.request_stop()
                code = 1
        if input_transport is not None:
            input_transport.close()
        if writer is not None:
            writer.close()
        for signum in handlers:
            loop.remove_signal_handler(signum)
    return code
