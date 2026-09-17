"""Independent supervisor: fake authority/worker, no keys or actual endpoints."""
import asyncio
from concurrent.futures import Future
from datetime import datetime, timedelta, timezone
import json
import os
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from src.data.providers.toss.observation import ObservationResult

KST = ZoneInfo('Asia/Seoul')


def fixture_service(tmp_path, monkeypatch, *, holdings=('087010',), input_error=False,
                    outcome='success', complete=True, stop_state='closed'):
    from src.observation import toss_service
    monkeypatch.setenv('TOSS_API', '1')
    at = [datetime(2026, 9, 18, 9, 0, tzinfo=KST)]
    stop = asyncio.Event()
    events, commands = [], []
    policy = {'dates': ['2026-09-18'], 'sessions': [{'name': 'regular', 'start': '09:00', 'end': '09:05'}],
              'calendar_time': '09:10', 'selection': {'max_symbols': 20, 'candidate_limit': 0,
              'max_snapshot_age_seconds': 300}, 'comparison': {'min_valid_pairs': 100},
              'limits': {'job_timeout_seconds': 1, 'cleanup_timeout_seconds': .1,
                         'ledger_max_bytes': 16777216},
              'acceptance': {'min_coverage': .95, 'max_provider_failure_rate': .05,
                 'max_missed_slots': 11, 'max_latency_seconds': 20, 'min_business_days': 3}}
    grant = SimpleNamespace(client_identity='synthetic-client', expires_at=(at[0] + timedelta(days=1)).isoformat(),
                            grant_id='synthetic-grant', ledger_path=str(tmp_path / 'observations.jsonl'))
    authority = SimpleNamespace(plan=SimpleNamespace(document=policy, canonical_hash='a'*64), grant=grant,
                                require=lambda *a, **kw: kw['deadline'], stop=lambda: events.append('authority-stop'))
    deployment = SimpleNamespace(preflight_timeout_seconds=1, load=lambda: authority)
    settings = {'status_path': str(tmp_path / 'status.json'), 'release_id': 'synthetic-release',
                'artifact_sha256': 'b'*64, 'config_hash': 'c'*64}

    class Worker:
        def __init__(self, factory, *, ownership_key):
            events.append('worker-created')

        async def start(self, **kwargs):
            events.append('worker-started')

        def submit(self, command):
            commands.append(command)
            stop.set()
            result = Future()
            if command.kind == 'missed':
                value = ObservationResult('idle', False, 0, 0, True, 'missed_slot')
            else:
                count = int(bool(holdings) and outcome == 'success')
                value = ObservationResult(outcome if holdings else 'idle', True, count, 0, complete,
                    'ok' if count else 'empty_selection' if not holdings else 'provider_failure',
                    provider_failures=int(bool(holdings) and outcome == 'failure'))
            result.set_result(value)
            return result

        async def stop(self, **kwargs):
            events.append('worker-stopped')
            return stop_state

    class Positions:
        async def fetch(self):
            events.append('positions')
            if input_error:
                from src.observation.toss_positions import InputUnavailable
                raise InputUnavailable()
            return holdings

        async def close(self):
            events.append('positions-closed')

    monkeypatch.setattr(toss_service, 'reconcile_ledger', lambda **kwargs: True)
    args = dict(deployment=deployment, settings=settings, claim_start=lambda _: events.append('receipt'),
                stop_event=stop, now=lambda: at[0], worker_factory=Worker, positions_factory=Positions)
    return toss_service, args, events, commands, at


def read_status(args):
    with open(args['settings']['status_path']) as stream:
        return json.load(stream)


def test_receipt_failure_prevents_worker(tmp_path, monkeypatch):
    module, args, events, _, _ = fixture_service(tmp_path, monkeypatch)
    args['claim_start'] = lambda _: (_ for _ in ()).throw(RuntimeError('sensitive text'))
    assert asyncio.run(module.run_service(**args)) != 0
    assert 'worker-created' not in events
    assert not (tmp_path / 'status.json').exists()


def test_valid_snapshot_has_no_quote_or_fake_time(tmp_path, monkeypatch):
    module, args, events, commands, _ = fixture_service(tmp_path, monkeypatch)
    assert asyncio.run(module.run_service(**args)) == 0
    snap = commands[0].snapshot
    assert snap['kis'] == {} and snap['source_success_at'] is None
    assert snap['selection_partial'] is True
    assert snap['selection_metadata']['candidates'] == ()
    assert events.index('receipt') < events.index('worker-created') < events.index('positions')
    status = read_status(args)
    assert status['last_observation_success_at'] is not None
    assert status['last_valid_pair_at'] is None and status['production_eligible'] is False
    assert status['by_kind']['prices']['selected_attempts'] == 1


def test_input_failure_is_missed_not_empty_or_success(tmp_path, monkeypatch):
    module, args, _, commands, _ = fixture_service(tmp_path, monkeypatch, input_error=True)
    assert asyncio.run(module.run_service(**args)) == 0
    assert commands[0].kind == 'missed'
    status = read_status(args)
    assert status['last_result'] == 'input_unavailable'
    assert status['input_last_transport_success_at'] is None
    assert status['last_observation_success_at'] is None
    assert status['by_kind']['prices']['input_failures'] == 1


@pytest.mark.parametrize('holdings,outcome,expected', [((), 'success', 'empty_selection'),
                                                       (('087010',), 'failure', 'provider_failure')])
def test_empty_and_provider_failure_do_not_refresh_success(tmp_path, monkeypatch, holdings, outcome, expected):
    module, args, _, _, _ = fixture_service(tmp_path, monkeypatch, holdings=holdings, outcome=outcome)
    assert asyncio.run(module.run_service(**args)) == 0
    status = read_status(args)
    assert status['last_observation_success_at'] is None
    assert status['last_result'] == expected
    assert status['input_last_transport_success_at'] is not None


def test_incomplete_ledger_and_uncertain_stop_fail_closed(tmp_path, monkeypatch):
    module, args, events, commands, _ = fixture_service(tmp_path, monkeypatch, complete=False,
                                                      stop_state='stopping_unconfirmed')
    assert asyncio.run(module.run_service(**args)) != 0
    status = read_status(args)
    assert status['state'] == 'stopping_unconfirmed'
    assert status['last_observation_success_at'] is None
    assert events.count('worker-created') == 1 and len(commands) == 1


def test_off_has_no_setup(tmp_path, monkeypatch):
    from src.observation.toss_service import run_service
    monkeypatch.delenv('TOSS_API', raising=False)
    def forbidden(*a, **kw):
        raise AssertionError('OFF touched dependencies')
    assert asyncio.run(run_service(deployment=None, settings=None, claim_start=forbidden,
                                   worker_factory=forbidden, positions_factory=forbidden)) == 0
    assert list(tmp_path.iterdir()) == []


def test_ledger_mismatch_cannot_claim_success(tmp_path, monkeypatch):
    module, args, _, _, _ = fixture_service(tmp_path, monkeypatch)
    monkeypatch.setattr(module, 'reconcile_ledger', lambda **kwargs: False)
    assert asyncio.run(module.run_service(**args)) != 0
    assert read_status(args)['observation_acceptance'] is None
    assert read_status(args)['last_observation_success_at'] is None
    assert read_status(args)['last_ledger_complete_at'] is None


def test_ledger_reader_failure_does_not_refresh_success(tmp_path, monkeypatch):
    module, args, _, _, _ = fixture_service(tmp_path, monkeypatch)
    def unavailable(**kwargs):
        raise OSError('private-file-detail')
    monkeypatch.setattr(module, 'reconcile_ledger', unavailable)
    assert asyncio.run(module.run_service(**args)) != 0
    assert read_status(args)['last_observation_success_at'] is None


def test_past_slot_does_not_fetch_positions(tmp_path, monkeypatch):
    module, args, events, commands, at = fixture_service(tmp_path, monkeypatch)
    at[0] += timedelta(minutes=6)
    assert asyncio.run(module.run_service(**args)) == 0
    assert commands[0].kind == 'missed' and 'positions' not in events


def test_status_fsync_failure_stops_without_replacement(tmp_path, monkeypatch):
    module, args, events, _, _ = fixture_service(tmp_path, monkeypatch)
    monkeypatch.setattr(module.StatusWriter, 'write', lambda *a: (_ for _ in ()).throw(OSError()))
    assert asyncio.run(module.run_service(**args)) != 0
    assert 'worker-created' not in events


def test_zero_denominator_and_future_slots_are_insufficient(tmp_path):
    from src.observation.toss_service import ServiceStatus
    policy = {'dates': ['2026-09-18'], 'sessions': [{'start': '09:00', 'end': '09:10'}],
              'calendar_time': '08:55', 'limits': {'job_timeout_seconds': 20},
              'acceptance': {'min_coverage': .95, 'max_provider_failure_rate': .05,
                 'max_missed_slots': 11, 'max_latency_seconds': 20, 'min_business_days': 3}}
    start = datetime(2026, 9, 17, 20, tzinfo=KST)
    tracker = ServiceStatus(policy, {}, SimpleNamespace(grant_id='synthetic', expires_at='synthetic'), 'a'*64, start)
    doc = tracker.document(start)
    assert doc['by_kind']['prices']['full_plan_slots'] == 2
    assert doc['by_kind']['prices']['ended_expected_slots'] == 0
    assert doc['by_kind']['prices']['slot_coverage'] is None
    assert doc['by_kind']['prices']['attempt_coverage'] is None
    assert doc['observation_acceptance'] is None


def test_stop_during_startup_does_not_wait_for_job_timeout(tmp_path, monkeypatch):
    module, args, events, _, _ = fixture_service(tmp_path, monkeypatch)
    class SlowWorker:
        def __init__(self, *a, **kw):
            pass

        async def start(self, **kwargs):
            args['stop_event'].set()
            try:
                await asyncio.Event().wait()
            finally:
                events.append('startup-cancelled')

        async def stop(self, **kwargs):
            return 'closed'
    args['worker_factory'] = SlowWorker
    async def run():
        return await asyncio.wait_for(module.run_service(**args), timeout=.2)
    assert asyncio.run(run()) == 0
    assert 'startup-cancelled' in events


def test_calendar_is_independent_and_confirms_business_day(tmp_path, monkeypatch):
    module, args, events, commands, at = fixture_service(tmp_path, monkeypatch)
    policy = args['deployment'].load().plan.document
    policy['calendar_time'] = '08:55'
    at[0] = at[0].replace(hour=8, minute=55)
    assert asyncio.run(module.run_service(**args)) == 0
    assert commands[0].kind == 'calendar'
    assert 'positions' not in events
    assert read_status(args)['confirmed_business_days'] == 0


def test_input_crossing_slot_boundary_only_records_missed(tmp_path, monkeypatch):
    module, args, _, commands, at = fixture_service(tmp_path, monkeypatch)
    class LatePositions:
        async def fetch(self):
            at[0] += timedelta(minutes=5)
            return ('087010',)

        async def close(self):
            pass
    args['positions_factory'] = LatePositions
    assert asyncio.run(module.run_service(**args)) == 0
    assert commands[0].kind == 'missed'


def test_statistics_keep_skips_in_denominator_and_do_not_mix_calendar(tmp_path):
    from src.observation.toss_service import ServiceStatus
    from src.schedulers.toss_shadow import DueSlot
    policy = {'dates': ['2026-09-18'], 'sessions': [{'start': '09:00', 'end': '09:05'}],
              'calendar_time': '08:55', 'limits': {'job_timeout_seconds': 20},
              'acceptance': {'min_coverage': .95, 'max_provider_failure_rate': .05,
                 'max_missed_slots': 11, 'max_latency_seconds': 20, 'min_business_days': 3}}
    at = datetime(2026, 9, 18, 10, tzinfo=KST)
    tracker = ServiceStatus(policy, {}, SimpleNamespace(grant_id='test', expires_at='test'), 'a'*64, at)
    price = DueSlot('prices', '2026-09-18T09:00:00+09:00', False, '2026-09-18')
    calendar = DueSlot('calendar', '2026-09-18T08:55:00+09:00', False, '2026-09-18')
    tracker.begin(price, 20)
    tracker.finish(price, ObservationResult('success', True, 1, 0, True, 'ok', budget_skips=19), latency=1, at=at)
    tracker.begin(calendar, 1)
    tracker.finish(calendar, ObservationResult('success', False, 1, 0, True, 'ok', '2026-09-18'), latency=1, at=at)
    doc = tracker.document(at)
    assert doc['by_kind']['prices']['attempt_coverage'] == .05
    assert doc['by_kind']['calendar']['attempt_coverage'] == 1
    assert doc['confirmed_business_days'] == 1
    assert doc['observation_acceptance'] is False


def test_cleanup_clients_share_one_deadline(tmp_path, monkeypatch):
    module, args, events, _, _ = fixture_service(tmp_path, monkeypatch)
    ticks = [100.0]
    args['clock'] = lambda: ticks[0]
    original_worker = args['worker_factory']
    class Worker(original_worker):
        async def stop(self, *, timeout):
            assert timeout <= .061  # .1 total minus .04 input cleanup
            return 'closed'
    class Positions:
        async def fetch(self):
            return ('087010',)

        async def close(self):
            ticks[0] += .04
    args.update(worker_factory=Worker, positions_factory=Positions)
    assert asyncio.run(module.run_service(**args)) == 0


def test_uncertain_cleanup_invalidates_acceptance_even_after_observation(tmp_path, monkeypatch):
    module, args, _, _, _ = fixture_service(tmp_path, monkeypatch, stop_state='stopping_unconfirmed')
    assert asyncio.run(module.run_service(**args)) != 0
    assert read_status(args)['accounting_consistent'] is False
    assert read_status(args)['observation_acceptance'] is None
