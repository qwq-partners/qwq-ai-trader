"""Single-life, separately supervised Toss observation, with no bot object."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import os
import signal
import time
from zoneinfo import ZoneInfo

from src.data.providers.toss.observation import select_snapshot
from src.data.providers.toss.observation_ledger import ObservationLedger
from src.data.providers.toss.runtime import Preflight, TossWorker, WorkerCommand
from src.data.providers.toss.runtime_factory import build_app, command_deadline
from src.schedulers.toss_shadow import due_slots
from .toss_positions import InputUnavailable, PositionsClient
from .toss_status import StatusWriter

KST = ZoneInfo('Asia/Seoul')


class _Stopped(Exception):
    pass


class ServiceStatus:
    """ACK-derived counters; acceptance remains separate from accounting coverage."""
    def __init__(self, policy, settings, grant, plan_hash, started):
        self.policy, self.settings, self.grant, self.plan_hash = policy, settings, grant, plan_hash
        self.started = started
        self.state, self.last_result = 'starting', None
        self.input_success = self.observation_success = self.ledger_complete = None
        self.latency_max, self.latency_samples = None, 0
        self.consistent = True
        self.records = {}
        # Full schedule contains only approved dates, with bounded three-day policy.
        final = datetime.fromisoformat(policy['dates'][-1]).replace(tzinfo=KST) + timedelta(days=1)
        self.schedule = tuple(due_slots(policy, final))

    def begin(self, slot, selected, *, missed=False, input_failure=False):
        key = slot.kind, slot.slot_id
        if key in self.records:
            raise ValueError('duplicate_submission')
        self.records[key] = dict(selected=selected, observed=0, terminal=0, complete=False,
                                 missed=missed, input_failure=input_failure, provider_failures=0,
                                 budget_skips=0, idle=False, open_calendar=False)

    def finish(self, slot, result, *, latency, at):
        row = self.records[slot.kind, slot.slot_id]
        if (not result.ledger_complete or result.outcome == 'duplicate'
                or result.valid_pairs != 0 or result.production_eligible
                or result.observation_count > row['selected']):
            self.consistent = False
            raise ValueError('ledger_incomplete')
        row.update(observed=result.observation_count, terminal=row['selected'], complete=True,
                   provider_failures=result.provider_failures, budget_skips=result.budget_skips,
                   idle=result.reason == 'empty_selection',
                   open_calendar=slot.kind == 'calendar' and result.reason == 'ok'
                                 and result.requested_date == slot.requested_date
                                 and result.observation_count > 0)
        self.latency_samples += 1
        self.latency_max = latency if self.latency_max is None else max(self.latency_max, latency)
        self.ledger_complete = at.isoformat()
        self.last_result = 'input_unavailable' if row['input_failure'] else result.reason
        if result.outcome == 'success' and result.observation_count > 0 and not row['missed']:
            self.observation_success = at.isoformat()

    def counts(self, kind):
        rows = [r for (k, _), r in self.records.items() if k == kind]
        return dict(recorded_slots=sum(r['complete'] and not r['missed'] for r in rows),
                    missed_slots=sum(r['complete'] and r['missed'] for r in rows),
                    selected_attempts=sum(r['selected'] for r in rows),
                    terminal_attempts=sum(r['terminal'] for r in rows),
                    observations=sum(r['observed'] for r in rows),
                    provider_failures=sum(r['provider_failures'] for r in rows),
                    budget_skips=sum(r['budget_skips'] for r in rows),
                    input_failures=sum(r['input_failure'] for r in rows),
                    empty_slots=sum(r['idle'] for r in rows))

    def _end(self, slot):
        at = datetime.fromisoformat(slot.slot_id)
        if slot.kind == 'calendar':
            # Expected observation window is 20s, even though same-day catch-up
            # is allowed by the underlying sender. Never count future dates.
            return at + timedelta(seconds=self.policy['limits']['job_timeout_seconds'])
        session = next(s for s in self.policy['sessions'] if s['start'] <= at.strftime('%H:%M') < s['end'])
        h, m = map(int, session['end'].split(':'))
        return min(at + timedelta(minutes=5), at.replace(hour=h, minute=m))

    def document(self, at):
        grouped, complete = {}, self.consistent
        for kind in ('prices', 'calendar'):
            counts = self.counts(kind)
            slots = [s for s in self.schedule if s.kind == kind]
            ended = [s for s in slots if self._end(s) <= at]
            success = sum(bool(self.records.get((kind, s.slot_id), {}).get('observed'))
                          and self.records[kind, s.slot_id]['complete'] for s in ended)
            terminal = counts['selected_attempts'] == counts['terminal_attempts']
            counts.update(full_plan_slots=len(slots), ended_expected_slots=len(ended),
                          observed_ended_slots=success,
                          slot_coverage=success / len(ended) if ended and self.consistent else None,
                          attempt_coverage=(counts['observations'] / counts['selected_attempts']
                                            if counts['selected_attempts'] and self.consistent else None),
                          provider_failure_rate=(counts['provider_failures'] / counts['selected_attempts']
                                                 if counts['selected_attempts'] and self.consistent else None),
                          unfinished_attempts=counts['selected_attempts'] - counts['terminal_attempts'])
            complete &= (len(ended) == len(slots) and terminal
                         and counts['recorded_slots'] + counts['missed_slots'] == len(slots))
            grouped[kind] = counts
        price_days = {slot[:10] for (kind, slot), row in self.records.items()
                      if kind == 'prices' and row['observed'] and row['complete']}
        open_days = {slot[:10] for (kind, slot), row in self.records.items()
                     if kind == 'calendar' and row['open_calendar'] and row['complete']}
        business_days = len(price_days & open_days)
        acceptance = None
        if complete:
            a = self.policy['acceptance']
            acceptance = all(
                c['slot_coverage'] is not None and c['slot_coverage'] >= a['min_coverage']
                and c['attempt_coverage'] is not None and c['attempt_coverage'] >= a['min_coverage']
                and c['provider_failure_rate'] is not None
                and c['provider_failure_rate'] <= a['max_provider_failure_rate'] for c in grouped.values()
            ) and (sum(c['missed_slots'] for c in grouped.values()) <= a['max_missed_slots']
                   and self.latency_max is not None and self.latency_max <= a['max_latency_seconds']
                   and business_days >= a['min_business_days'])
        return dict(schema_version=1, release_id=self.settings.get('release_id'),
                    artifact_sha256=self.settings.get('artifact_sha256'), config_hash=self.settings.get('config_hash'),
                    grant_id=self.grant.grant_id, plan_hash=self.plan_hash, pid=os.getpid(),
                    expires_at=self.grant.expires_at, started_at=self.started.isoformat(), updated_at=at.isoformat(),
                    state=self.state, last_result=self.last_result,
                    input_last_transport_success_at=self.input_success,
                    last_observation_success_at=self.observation_success,
                    last_ledger_complete_at=self.ledger_complete, last_valid_pair_at=None,
                    by_kind=grouped, latency_max_seconds=self.latency_max, latency_samples=self.latency_samples,
                    input_timeout_seconds=2, confirmed_business_days=business_days,
                    accounting_consistent=self.consistent, observation_acceptance=acceptance,
                    comparison='insufficient', valid_pairs=0, p95=None, outlier_rate=None,
                    production_eligible=False)


def reconcile_ledger(*, authority, tracker):
    summary = ObservationLedger.read_only_summary(
        authority.grant.ledger_path, plan_hash=authority.plan.canonical_hash,
        max_bytes=authority.plan.document['limits']['ledger_max_bytes'])
    if summary['incomplete'] or summary['duplicate_submissions'] or summary['valid_pairs']:
        return False
    for kind in ('prices', 'calendar'):
        expected, actual = tracker.counts(kind), summary['by_kind'][kind]
        for key in ('recorded_slots', 'missed_slots', 'selected_attempts', 'terminal_attempts',
                    'provider_failures', 'budget_skips'):
            if expected[key] != actual[key]:
                return False
        if expected['observations'] != actual['terminal_reasons']['success']:
            return False
    return True


async def _job(worker, command, *, stop, timeout):
    wrapped = asyncio.wrap_future(worker.submit(command))
    wrapped.add_done_callback(lambda future: None if future.cancelled() else future.exception())
    stopping = asyncio.create_task(stop.wait())
    try:
        done, _ = await asyncio.wait((wrapped, stopping), timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
        if wrapped in done:
            return wrapped.result()
        if stopping in done:
            raise _Stopped()
        raise TimeoutError('worker_timeout')
    finally:
        stopping.cancel()
        await asyncio.gather(stopping, return_exceptions=True)


async def _start(worker, *, stop, timeout):
    starting = asyncio.create_task(worker.start(timeout=timeout))
    stopping = asyncio.create_task(stop.wait())
    try:
        done, _ = await asyncio.wait((starting, stopping), return_when=asyncio.FIRST_COMPLETED)
        if starting in done:
            return starting.result()
        raise _Stopped()
    finally:
        if not starting.done():
            starting.cancel()  # TossWorker.start cancels startup and retains owner.
        stopping.cancel()
        await asyncio.gather(starting, stopping, return_exceptions=True)


async def run_service(*, deployment, settings: dict, claim_start, stop_event=None, now=None,
                      clock=None, worker_factory=None, positions_factory=None) -> int:
    flag = os.environ.get('TOSS_API', '0')
    if flag in {'0', ''}:
        return 0
    if flag != '1':
        return 1
    now = now or (lambda: datetime.now(timezone.utc))
    clock = clock or time.monotonic
    stop = stop_event if stop_event is not None else asyncio.Event()
    worker = authority = positions = tracker = writer = None
    handlers = []
    code = 0
    cleanup_timeout = 10
    loop = asyncio.get_running_loop()
    try:
        if stop_event is None:
            for signum in (signal.SIGTERM, signal.SIGINT):
                loop.add_signal_handler(signum, stop.set)
                handlers.append(signum)
        authority = await Preflight(deployment.load).run(timeout=deployment.preflight_timeout_seconds)
        if stop.is_set() or os.environ.get('TOSS_API', '0') != '1':
            return 0
        policy = authority.plan.document
        cleanup_timeout = policy['limits']['cleanup_timeout_seconds']
        authority.require('query', deadline=clock() + policy['limits']['job_timeout_seconds'])
        claim_start(authority)
        tracker = ServiceStatus(policy, settings, authority.grant, authority.plan.canonical_hash, now())
        writer = StatusWriter(settings['status_path'])
        writer.write(tracker.document(now()))
        worker = (worker_factory or TossWorker)(lambda stopping: build_app(authority, stopping),
                                                ownership_key=authority.grant.client_identity)
        await _start(worker, stop=stop, timeout=policy['limits']['job_timeout_seconds'])
        positions = (positions_factory or PositionsClient)()
        tracker.state = 'idle'
        while not stop.is_set() and os.environ.get('TOSS_API', '0') == '1':
            stamp = now()
            if stamp.tzinfo is None or stamp.utcoffset() is None:
                raise ValueError('clock_invalid')
            if stamp >= datetime.fromisoformat(authority.grant.expires_at):
                tracker.last_result = 'grant_expired'
                break
            authority.require('query', deadline=clock() + policy['limits']['job_timeout_seconds'])
            for slot in due_slots(policy, stamp):
                if stop.is_set() or os.environ.get('TOSS_API', '0') != '1':
                    break
                if (slot.kind, slot.slot_id) in tracker.records:
                    continue
                current = now()
                if datetime.fromisoformat(slot.slot_id) > current:
                    continue
                missed = slot.missed
                failed_input = False
                snapshot = None
                if not missed and slot.kind == 'prices':
                    try:
                        holdings = await positions.fetch()
                        tracker.input_success = now().isoformat()
                        snapshot = select_snapshot(candidates=(), holdings=holdings, source_success_at=None,
                                                   now=now(), policy=policy, kis_quotes={})
                    except InputUnavailable:
                        missed, failed_input = True, True
                if not missed:
                    command = (WorkerCommand('prices', slot.slot_id, snapshot) if slot.kind == 'prices' else
                               WorkerCommand('calendar', slot.slot_id, requested_date=slot.requested_date))
                    # Input or a previous fsync may cross the slot cutoff.
                    try:
                        command_deadline(command, policy, now=now(), clock=clock)
                    except Exception:
                        missed = True
                if missed:
                    command = WorkerCommand('missed', slot.slot_id, {'kind': slot.kind})
                if stop.is_set() or os.environ.get('TOSS_API', '0') != '1':
                    break
                authority.require('query', deadline=clock() + policy['limits']['job_timeout_seconds'])
                selected = 0 if missed else len(snapshot['symbols']) if slot.kind == 'prices' else 1
                tracker.begin(slot, selected, missed=missed, input_failure=failed_input)
                tracker.state = 'observing'
                writer.write(tracker.document(now()))
                started = clock()
                result = await _job(worker, command, stop=stop, timeout=policy['limits']['job_timeout_seconds'])
                latency = max(0, clock() - started)
                prior_success, prior_ledger = tracker.observation_success, tracker.ledger_complete
                tracker.finish(slot, result, latency=latency, at=now())
                try:
                    matched = await Preflight(lambda: reconcile_ledger(authority=authority, tracker=tracker)).run(
                        timeout=deployment.preflight_timeout_seconds)
                    if not matched:
                        raise ValueError('ledger_mismatch')
                except BaseException:
                    tracker.observation_success, tracker.ledger_complete = prior_success, prior_ledger
                    tracker.consistent = False
                    raise
                tracker.state = 'degraded' if failed_input or result.outcome == 'failure' else 'idle'
                writer.write(tracker.document(now()))
            writer.write(tracker.document(now()))
            expiry = datetime.fromisoformat(authority.grant.expires_at)
            remaining = max(0, (expiry - now()).total_seconds())
            # Wake at the next wall-clock minute; never a drifting sleep(300).
            stamp = now()
            delay = min(60 - stamp.second - stamp.microsecond / 1e6, remaining)
            if delay <= 0:
                break
            try:
                await asyncio.wait_for(stop.wait(), timeout=delay)
            except TimeoutError:
                pass
    except _Stopped:
        if tracker is not None:
            tracker.consistent = False
            tracker.last_result = 'interrupted'
    except asyncio.CancelledError:
        code = 1
        if tracker is not None:
            tracker.consistent = False
            tracker.last_result = 'cancelled'
    except Exception:
        code = 1
        if tracker is not None:
            tracker.consistent = False
            tracker.state, tracker.last_result = 'unavailable', 'observation_incomplete'
    finally:
        if authority is not None:
            authority.stop()
        if positions is not None:
            try:
                await asyncio.wait_for(positions.close(), timeout=1)
            except Exception:
                code = 1
        final_state = 'closed'
        if worker is not None:
            try:
                final_state = await worker.stop(timeout=cleanup_timeout)
            except BaseException:
                final_state = 'stopping_unconfirmed'
            if final_state != 'closed':
                code = 1
        if tracker is not None and writer is not None:
            tracker.state = final_state if final_state != 'closed' or not code else 'unavailable'
            try:
                writer.write(tracker.document(now()))
            except Exception:
                code = 1
        for signum in handlers:
            loop.remove_signal_handler(signum)
    return code
