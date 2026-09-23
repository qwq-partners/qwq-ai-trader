"""N3 independent acceptance: real owner/queue, synthetic data, no live I/O.

The exporter is not installed in the engine. Every diagnostic remains non-authorizing.
Synthetic corruption below changes only test-owned in-memory state, never a live store.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime, tzinfo
from decimal import Decimal
import importlib
import importlib.util
import json
from pathlib import Path

import pytest

from src.execution.safety.economics import encode_portfolio
from src.execution.safety.protection import encode_protection
from src.execution.safety.protection_producer import ProtectionProducer, _Episode
from test_execution_runtime import NOW, setup, opened, observed, queued


SENTINEL = 'PRIVATE-DIAGNOSTIC-SENTINEL-DO-NOT-EXPORT'


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)


def api():
    name = 'src.execution.safety.recovery_capture'
    assert importlib.util.find_spec(name) is not None, 'N3 capture module not implemented'
    capture = importlib.import_module(name)
    diagnostic = importlib.import_module('src.execution.safety.recovery_diagnostics')
    return capture.capture_recovery_snapshot, diagnostic.build_recovery_diagnostic


def report_for(runtime, *, captured_at=NOW):
    capture, build = api()
    report = build(capture(runtime, captured_at=captured_at))
    assert report['read_only'] is True
    assert report['automatic_action_allowed'] is False
    assert report['trading_ready'] is False
    assert report['installation_verified'] is False
    # Every path, including malformed input, must remain strict JSON (no NaN).
    json.dumps(report, allow_nan=False, sort_keys=True)
    return report


def codes(report):
    return {row['code'] for row in report['findings']}


def producer_for(runtime):
    producer = ProtectionProducer(runtime, clock=lambda: NOW, indicator_source=lambda _: {})
    runtime._protection_producer = producer
    return producer


def preserved(runtime, engine, exits, producer):
    names = ('_episodes', '_last_quote', '_sources', '_restart_originals',
             '_restart_retries', '_restart_checked', '_recovery_required',
             '_pending_reasons', '_stats')
    return (deepcopy(runtime.owner.state), runtime.owner.version,
            runtime.owner.published_version, engine._execution_version,
            encode_portfolio(engine.portfolio), encode_protection(exits),
            deepcopy([getattr(producer, name) for name in names]),
            deepcopy(runtime._protection_failures),
            deepcopy(runtime._protection_recovery_counts))


def test_missing_runtime_is_unknown_not_legacy_or_zero_evidence():
    report = report_for(None)
    assert report['mode'] == 'unknown'
    assert report['snapshot_stable'] is None
    assert report['publication_consistent'] is None
    assert 'snapshot_unavailable' in codes(report)
    assert 'disposition_not_durable' in codes(report)


def test_actual_runtime_is_read_only_and_returns_no_live_aliases(tmp_path, monkeypatch):
    async def scenario():
        engine, exits, store, runtime = await setup(tmp_path)
        try:
            producer = producer_for(runtime)
            producer._episodes[SENTINEL] = _Episode(
                SENTINEL, ('sell_partial', 30, SENTINEL), Decimal('98123.4567'),
                SENTINEL, command_id=SENTINEL, reason=SENTINEL)
            producer._restart_retries[SENTINEL] = {
                'started_at': NOW, 'intent_ids': [SENTINEL], 'reason': SENTINEL}
            producer._restart_originals = {SENTINEL: [SENTINEL]}
            producer._restart_checked.add(SENTINEL)
            producer._recovery_required[SENTINEL] = {
                'reason': SENTINEL, 'intent_ids': [SENTINEL], 'command_ids': [SENTINEL]}
            runtime._protection_failures[('quote', SENTINEL)] = {
                'generation': 1, 'symbol': SENTINEL, 'phase': SENTINEL}
            before = preserved(runtime, engine, exits, producer)
            calls = []

            def forbidden(*_args, **_kwargs):
                calls.append('forbidden')
                raise AssertionError('diagnostic invoked clock/health/I/O/mutation')

            with monkeypatch.context() as guards:
                for obj, names in (
                    (runtime, ('health', 'clock', 'restore', 'resume_protection_admission',
                               'repair_protection', 'release_protection_pending')),
                    (runtime.owner, ('mutate', 'restore')),
                    (store, ('load', 'commit')),
                    (producer, ('health', 'clock', '_audit_recovery', '_sweep',
                                'sweep', 'on_market_data')),
                ):
                    for name in names:
                        guards.setattr(obj, name, forbidden)
                capture, build = api()
                snapshot = capture(runtime, captured_at=NOW)
                first = build(snapshot)
                original = deepcopy(first)
                assert first['snapshot_stable'] is True
                assert first['mode'] == 'partial_install'  # No gateway/factory receipt.
                assert SENTINEL not in json.dumps(first)
                assert '98123.4567' not in json.dumps(first)
                assert SENTINEL not in repr(snapshot)
                first['findings'].append({'code': 'caller-owned-mutation'})
                if first.get('findings'):
                    first['findings'][0]['count'] = 99999
                assert build(snapshot) == original
                assert report_for(runtime) == original
                assert preserved(runtime, engine, exits, producer) == before
                assert calls == []
        finally:
            await store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('change', ['published', 'engine', 'latch', 'health'])
def test_stable_snapshot_does_not_hide_publication_failure(tmp_path, change):
    async def scenario():
        engine, _, store, runtime = await setup(tmp_path)
        try:
            producer_for(runtime)
            if change == 'published':
                runtime.owner._published_version -= 1
            elif change == 'engine':
                engine._execution_version -= 1
            elif change == 'latch':
                runtime.owner._publication_recovery_required = True
            else:
                runtime.owner._healthy = False
            report = report_for(runtime)
            assert report['snapshot_stable'] is True
            if change != 'health':
                assert report['publication_consistent'] is False
                assert 'publication_inconsistent' in codes(report)
            assert 'owner_health_unconfirmed' in codes(report) or change == 'engine'
        finally:
            await store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('null_source', [False, True])
@pytest.mark.parametrize('delivered', [False, True])
def test_general_orphan_audit_includes_null_source_and_delivered_history(
        tmp_path, null_source, delivered):
    async def scenario():
        _, _, store, runtime = await setup(tmp_path)
        try:
            producer_for(runtime)
            row = {'kind': 'protection_decision', 'symbol': SENTINEL,
                   'intent_id': SENTINEL, 'decision': ['sell_all', 7, SENTINEL],
                   'delivered': delivered}
            if null_source:
                row['effect_source'] = None
            runtime.owner._state['outbox'][SENTINEL] = row
            report = report_for(runtime)
            assert 'protection_unsubmitted' in codes(report)
            assert SENTINEL not in json.dumps(report)
        finally:
            await store.close()
    asyncio.run(scenario())


def test_unknown_buy_and_cash_only_terminal_reservation_are_not_lost(tmp_path):
    async def scenario():
        _, _, store, runtime = await setup(tmp_path)
        try:
            producer_for(runtime)
            await opened(runtime, 'B1')
            row = runtime.owner._state['attempts']['B1']
            row['command_status'] = 'unknown'
            assert 'unknown_buy' in codes(report_for(runtime))
            row['command_status'] = 'acknowledged'
            row['state'] = 'final_filled'
            row['reserved_quantity'] = 0
            row['reserved_cash'] = '0.01'
            report = report_for(runtime)
            assert 'terminal_reservation' in codes(report)
            assert 'remaining_reservation' in codes(report)
        finally:
            await store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('field,value', [
    ('reserved_quantity', True), ('reserved_quantity', -1),
    ('reserved_cash', 'NaN'), ('reserved_cash', 'Infinity'),
    ('reserved_cash', None), ('observed_quantity', True),
])
def test_invalid_reservation_or_quantity_is_not_numeric_zero(tmp_path, field, value):
    async def scenario():
        _, _, store, runtime = await setup(tmp_path)
        try:
            producer_for(runtime)
            await opened(runtime, 'B1')
            runtime.owner._state['attempts']['B1'][field] = value
            report = report_for(runtime)
            assert {'evidence_invalid', 'snapshot_unavailable'} & codes(report)
        finally:
            await store.close()
    asyncio.run(scenario())


def test_actual_partial_sell_is_pending_not_target_quantity_corruption(tmp_path):
    async def scenario():
        engine, exits, store, runtime = await setup(tmp_path)
        try:
            producer = producer_for(runtime)
            buy = await opened(runtime, 'B1')
            await queued(engine, await observed(runtime, buy, 100, '1000000'))
            sell = await opened(runtime, 'S1', 'sell')
            await queued(engine, await observed(runtime, sell, 40, '440000', side='sell'))
            assert engine.portfolio.positions['005930'].quantity == 60
            assert exits.get_state('005930').remaining_quantity == 60
            decision = ['sell_all', 100, 'synthetic original decision']
            runtime.owner._state['outbox']['audit-S1'] = {
                'kind': 'protection_decision', 'symbol': '005930',
                'intent_id': 'S1', 'decision': decision, 'effect_source': None,
                'delivered': True}
            producer._episodes['005930'] = _Episode(
                'S1', tuple(decision), Decimal('11000'), 'rest', command_id='audit-S1')
            report = report_for(runtime)
            assert report['snapshot_stable'] is True
            assert 'pending_sell' in codes(report)
            assert 'protection_quantity_inconsistent' not in codes(report)
            assert 'protection_link_inconsistent' not in codes(report)
            assert 'observation_not_applied' not in codes(report)
        finally:
            await store.close()
    asyncio.run(scenario())


def test_aware_capture_time_is_required_without_asking_runtime_clock(tmp_path, monkeypatch):
    async def scenario():
        _, _, store, runtime = await setup(tmp_path)
        try:
            producer_for(runtime)
            def forbidden():
                raise AssertionError('runtime clock called')
            monkeypatch.setattr(runtime, 'clock', forbidden)
            report = report_for(runtime, captured_at=datetime(2026, 9, 18, 10))
            assert {'snapshot_unavailable', 'evidence_invalid'} & codes(report)
        finally:
            await store.close()
    asyncio.run(scenario())


def test_unknown_object_cannot_run_custom_hooks():
    calls = []
    class Hostile:
        def __getattribute__(self, key):
            calls.append(key)
            raise AssertionError('unknown object inspected')
        def __repr__(self):
            calls.append('repr')
            raise AssertionError('unknown object formatted')
        def __bool__(self):
            calls.append('bool')
            raise AssertionError('unknown object coerced')
        def __eq__(self, other):
            calls.append('eq')
            raise AssertionError('unknown object compared')
    report = report_for(Hostile())
    assert report['mode'] == 'unknown'
    assert calls == []


def test_datetime_with_custom_timezone_does_not_execute_timezone_hook():
    calls = []
    class HostileTimezone(tzinfo):
        def utcoffset(self, value):
            calls.append('utcoffset')
            raise AssertionError('timezone callback executed')
        def dst(self, value):
            calls.append('dst')
            raise AssertionError('timezone callback executed')
    capture_time = datetime(2026, 9, 18, 10, tzinfo=HostileTimezone())
    report = report_for(None, captured_at=capture_time)
    assert 'snapshot_unavailable' in codes(report)
    assert calls == []
