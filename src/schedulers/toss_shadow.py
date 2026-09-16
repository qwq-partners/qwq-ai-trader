"""Default-OFF observer supervisor. Never calls a KIS/broker/trading method."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
import math
import os
import time
from types import MappingProxyType
from zoneinfo import ZoneInfo

from src.data.providers.toss.market_types import Quote
from src.data.providers.toss.runtime import Preflight, TossWorker, WorkerCommand, RuntimeUnavailable
from src.utils import loop_heartbeat as hb

KST = ZoneInfo('Asia/Seoul')
NAMES = {'prices': 'kr_toss_prices', 'calendar': 'kr_toss_calendar'}


@dataclass(frozen=True)
class CandidateSnapshot:
    candidates: tuple
    source_success_at: datetime


@dataclass(frozen=True)
class SelectionInputs:
    candidates: tuple
    holdings: tuple
    source_success_at: datetime | None
    kis_quotes: MappingProxyType


def candidate_copy(stocks, *, now):
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        return None
    candidates = []
    for stock in stocks:
        symbol, score = getattr(stock, 'symbol', None), getattr(stock, 'score', None)
        if (type(symbol) is str and type(score) in (int, float)
                and math.isfinite(score)):
            candidates.append((symbol, score))
    return CandidateSnapshot(tuple(candidates), now)


def capture_inputs(bot, candidate_snapshot):
    portfolio = getattr(getattr(bot, 'engine', None), 'portfolio', None)
    positions = getattr(portfolio, 'positions', {})
    quotes = {}
    # All mutations remain with the engine. This synchronous main-loop copy has
    # no broker call and never promotes a selection time into a quote time.
    for symbol, position in positions.items():
        if type(symbol) is not str:
            continue
        try:
            value = getattr(position, 'current_price', None)
            price = None if isinstance(value, bool) else Decimal(str(value))
            if price is not None and (not price.is_finite() or price <= 0):
                price = None
        except (InvalidOperation, TypeError, ValueError):
            price = None
        quotes[symbol] = Quote(symbol, price, None, None, 'partial',
                               frozenset({'observed_at', 'fetched_at', 'market_basis'}),
                               'unknown', 'KRW')
    return SelectionInputs(
        candidate_snapshot.candidates if candidate_snapshot else (),
        tuple(sorted(quotes)), candidate_snapshot.source_success_at if candidate_snapshot else None,
        MappingProxyType(quotes),
    )


@dataclass(frozen=True)
class DueSlot:
    kind: str
    slot_id: str
    missed: bool
    requested_date: str


def due_slots(policy, now):
    """Fixed KST slots, not a drifting sleep(300). No market-open dependency."""
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError('aware_clock_required')
    local = now.astimezone(KST)
    for day in policy['dates']:
        if day > local.date().isoformat():
            break
        midnight = datetime.fromisoformat(day).replace(tzinfo=KST)
        h, m = map(int, policy['calendar_time'].split(':'))
        scheduled = midnight.replace(hour=h, minute=m)
        if scheduled <= local:
            yield DueSlot('calendar', scheduled.isoformat(), scheduled.date() != local.date(), day)
        for session in sorted(policy['sessions'], key=lambda s: s['start']):
            h, m = map(int, session['start'].split(':'))
            cursor = midnight.replace(hour=h, minute=m)
            # Slots are wall-clock multiples of five minutes, even if a session
            # begins at an unusual minute. Never query before approved start.
            cursor += timedelta(minutes=(-cursor.minute) % 5)
            eh, em = map(int, session['end'].split(':'))
            end = midnight.replace(hour=eh, minute=em)
            while cursor < end and cursor <= local:
                yield DueSlot('prices', cursor.isoformat(), local >= min(cursor + timedelta(minutes=5), end), day)
                cursor += timedelta(minutes=5)


def update_observation_health(kind, result, *, now):
    name = NAMES[kind]
    local_day = now.astimezone(KST).date().isoformat()
    date_valid = kind != 'calendar' or result.requested_date == local_day
    success = (result.outcome == 'success' and result.ledger_complete
               and result.observation_count > 0 and date_valid)
    comparison = 'observed' if result.valid_pairs > 0 else 'insufficient'
    previous = hb._observer_status.get(name, {})
    details = dict(state='observing', comparison=comparison, degraded=bool(result.degraded),
                   observation_count=result.observation_count, valid_pairs=result.valid_pairs,
                   provider_failures=getattr(result, 'provider_failures', 0),
                   budget_skips=getattr(result, 'budget_skips', 0),
                   excluded_pairs=getattr(result, 'excluded_pairs', 0),
                   ledger_complete=bool(result.ledger_complete), production_eligible=False)
    if result.ledger_complete:
        details['last_ledger_complete'] = now.timestamp()
    if success:
        hb.record_success(name, note='degraded' if result.degraded else None)
        if result.valid_pairs > 0:
            details['last_valid_pair'] = now.timestamp()
    elif result.outcome == 'idle' and result.ledger_complete:
        hb.record_idle(name, 'empty_selection')
    elif result.outcome == 'duplicate':
        # Replay does not refresh staleness/last_success.
        details['skip_count'] = previous.get('skip_count', 0) + 1
    else:
        hb.record_failure(name, 'observation_incomplete' if not result.ledger_complete else 'no_valid_observation')
        details['state'] = 'degraded'
        if result.reason in {'worker_busy', 'budget_skip'}:
            details['skip_count'] = previous.get('skip_count', 0) + 1
    hb.observer_status(name, **details)


def _unavailable(state='unavailable'):
    for name in NAMES.values():
        hb.observer_status(name, state=state, comparison='insufficient', production_eligible=False)
        hb.record_failure(name, state)


def attach_shadow(bot):
    """Called by create_tasks; disabled path has literally no observer setup."""
    flag = os.environ.get('TOSS_API', '0')
    if flag in {'0', ''}:
        return None
    if flag != '1':
        _unavailable('configuration_invalid')
        return None
    if getattr(bot, '_toss_shadow_supervisor', None) is not None:
        # Includes pending/failed/hung preflight: no overlapping retry factory.
        return None
    deployment = getattr(bot, '_toss_observation_deployment', None)
    if deployment is None:
        _unavailable()
        return None
    # Deployment types and actual factories are loaded only after explicit ON.
    from src.data.providers.toss.runtime_factory import Deployment
    if not isinstance(deployment, Deployment):
        _unavailable()
        return None
    supervisor = TossShadowSupervisor(bot, deployment)
    bot._toss_shadow_supervisor = supervisor
    return asyncio.create_task(supervisor.run(), name='kr_toss_shadow_supervisor')


class TossShadowSupervisor:
    def __init__(self, bot, deployment):
        self.bot = bot
        self.deployment = deployment
        self.candidates = None
        self.worker = None
        self.state = 'new'
        self._seen = set()

    def publish_candidates(self, stocks):
        self.candidates = candidate_copy(stocks, now=datetime.now(KST))

    async def run(self):
        # Task1/2/3 integration is lazy: OFF imports no auth/HTTP implementation.
        from src.data.providers.toss.runtime_factory import build_app
        from src.data.providers.toss.observation import select_snapshot
        authority = None
        cleanup_timeout = self.deployment.preflight_timeout_seconds
        try:
            self.state = 'approval_pending'
            for name in NAMES.values():
                hb.observer_status(name, state=self.state, production_eligible=False)
            authority = await Preflight(self.deployment.load).run(timeout=self.deployment.preflight_timeout_seconds)
            # OFF may arrive while bounded approval I/O is still in progress.
            # Do not create even an observation worker with a discarded grant.
            if os.environ.get('TOSS_API', '0') != '1':
                self.state = 'disabled'
                for name in NAMES.values():
                    hb.set_enabled(name, False, 'flag_off')
                    hb.observer_status(name, state=self.state, production_eligible=False)
                return
            policy = authority.plan.document
            cleanup_timeout = policy['limits']['cleanup_timeout_seconds']
            hb.register_observer(NAMES['prices'], dates=policy['dates'],
                                 windows=tuple((s['start'], s['end']) for s in policy['sessions']))
            hb.register_observer(NAMES['calendar'], dates=policy['dates'], daily_time=policy['calendar_time'])
            self.worker = TossWorker(lambda stop: build_app(authority, stop),
                                     ownership_key=authority.grant.client_identity)
            await self.worker.start(timeout=policy['limits']['job_timeout_seconds'])
            self.state = 'observing'
            for name in NAMES.values():
                hb.observer_status(name, state=self.state, production_eligible=False)
            while getattr(self.bot, 'running', True):
                now = datetime.now(KST)
                if os.environ.get('TOSS_API', '0') != '1':
                    break
                authority.require('query', deadline=time.monotonic() + policy['limits']['job_timeout_seconds'])
                for slot in due_slots(policy, now):
                    key = slot.kind, slot.slot_id
                    if key in self._seen:
                        continue
                    # Earlier jobs (including calendar/fsync) may cross this
                    # slot's boundary. Re-evaluate at submission, not batch start.
                    submitted_at = datetime.now(KST)
                    slot_at = datetime.fromisoformat(slot.slot_id)
                    if slot_at > submitted_at:
                        continue  # backwards wall-clock jump: never query future
                    missed = slot.missed
                    if slot.kind == 'calendar':
                        missed |= slot_at.date() != submitted_at.date()
                    else:
                        session = next(s for s in policy['sessions']
                                       if s['start'] <= slot_at.strftime('%H:%M') < s['end'])
                        hour, minute = map(int, session['end'].split(':'))
                        end = min(slot_at + timedelta(minutes=5), slot_at.replace(hour=hour, minute=minute))
                        missed |= submitted_at >= end
                    hb.record_attempt(NAMES[slot.kind])
                    if missed:
                        command = WorkerCommand('missed', slot.slot_id, {'kind': slot.kind})
                    elif slot.kind == 'calendar':
                        command = WorkerCommand('calendar', slot.slot_id, requested_date=slot.requested_date)
                    else:
                        inputs = capture_inputs(self.bot, self.candidates)
                        snapshot = select_snapshot(candidates=inputs.candidates, holdings=inputs.holdings,
                                                   source_success_at=inputs.source_success_at, now=submitted_at,
                                                   policy=policy, kis_quotes=inputs.kis_quotes)
                        command = WorkerCommand('prices', slot.slot_id, snapshot)
                    future = self.worker.submit(command)
                    wrapped = asyncio.wrap_future(future)
                    wrapped.add_done_callback(lambda f: None if f.cancelled() else f.exception())
                    # Includes stuck ledger I/O. Timeout gates the owner in
                    # finally; only the worker's confirmed cleanup releases it.
                    outcome = await asyncio.wait_for(asyncio.shield(wrapped),
                                                     policy['limits']['job_timeout_seconds'])
                    self._seen.add(key)
                    if not missed:
                        update_observation_health(slot.kind, outcome, now=datetime.now(KST))
                # Supervisor sleep never occupies an existing trading loop.
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            self.state = 'cancelled'
            for name in NAMES.values():
                hb.observer_status(name, state=self.state, production_eligible=False)
            raise
        except Exception:
            self.state = 'unavailable'
            _unavailable()
        finally:
            if authority is not None:
                authority.stop()
            if self.worker is not None:
                cleanup = asyncio.create_task(self.worker.stop(timeout=cleanup_timeout))
                while True:
                    try:
                        cleanup_state = await asyncio.shield(cleanup)
                        break
                    except asyncio.CancelledError:
                        continue
                if cleanup_state != 'closed':
                    self.state = 'stopping_unconfirmed'
                    _unavailable('stopping_unconfirmed')
                else:
                    disabled = os.environ.get('TOSS_API', '0') in {'0', ''}
                    # OFF can itself abort app.start()/an active command. Once
                    # cleanup is confirmed, explicit OFF outranks that failure;
                    # ON + unavailable and any unconfirmed stop stay visible.
                    if disabled or self.state != 'unavailable':
                        self.state = 'disabled' if disabled else 'closed'
                        for name in NAMES.values():
                            if disabled:
                                hb.set_enabled(name, False, 'flag_off')
                            hb.observer_status(name, state=self.state, production_eligible=False)
