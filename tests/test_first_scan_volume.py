"""실제 수집·봉인·판독 경로의 합성 유량 시험. 실시장 처리율 증명은 아니다."""
import asyncio
from copy import deepcopy
from collections import Counter
from datetime import timedelta
import json
from pathlib import Path
import resource
from time import monotonic
from types import SimpleNamespace

import pytest

from src.analytics import entry_gate_trace, entry_observation
from src.analytics.entry_observation_journal import read_observation_journal
from src.analytics.entry_observation_runtime import ObservationRuntime
from src.analytics.first_scan_resources import first_scan_journal_bound
from src.analytics.pregate_candidate_readout import build_pregate_candidate_report
from src.analytics.pregate_observed_regular_flow import build_observed_regular_flow_report
from src.data.feeds.quote_subscription import observe_screen_candidates
from test_entry_observation_runtime import load, NOW
from test_first_scan_memory_guard import guarded_study
from test_market_state_capture import advancing_capture_clock, fields, frame, PRICE, BOOK, STATUS, receive
from test_quote_subscription import ack, make_feed


CAPTURE_SECONDS = 1260
CANDIDATE_SYMBOLS = 5
CANDIDATE_PRICE_PER_SECOND = 50
CANDIDATE_BOOK_PER_SECOND = 25
BURST_TICK = 630
BURST_STATES = 458
BURST_BOOKS = 114
FRESH_ALLOCATIONS = {'ws_quote': 35000, 'market_state': 65000, 'other': 512}
FRESH_RECORD_BYTES = {'scan_metadata': 65536, 'ws_quote': 2048, 'market_state': 2048, 'other': 4096}
METADATA_ALLOCATION = 201
BUFFER_CAPACITY = 100713


def price_batch(symbol, count):
    """Make parser-conformant raw CNT frames without bypassing the feed."""
    rows = []
    for _ in range(count):
        row = fields(PRICE, symbol)
        row[1] = '093000'
        row[33] = NOW.strftime('%Y%m%d')
        rows.extend(row)
    return f'0|{PRICE}|{count:03}|' + '^'.join(rows)


def high_volume_capture_clock(monkeypatch):
    """Keep all 572 raw events in the declared one-second burst tick."""
    from src.analytics import entry_observation_journal
    from src.data.feeds import kis_websocket, quote_subscription

    clock = [NOW]

    def now(tz=None):
        value = clock[0]
        clock[0] += timedelta(microseconds=100)
        return value

    for module in (kis_websocket, quote_subscription):
        monkeypatch.setattr(module, 'datetime', SimpleNamespace(now=now))
    monkeypatch.setattr(entry_observation, '_now', lambda: now().isoformat())
    monkeypatch.setattr(entry_observation_journal, '_timestamp', lambda: now().isoformat())
    return clock


def high_volume_settings(study, *, market_state_quota=65000):
    guarded_study(study)
    s = study['capture']
    # The last 1 Hz tick is at start+1260; leave a two-second seal margin.
    study['as_of'] = (NOW + timedelta(minutes=1, seconds=1263)).isoformat()
    study['observed_regular_flow_policy'] = {
        'version': 'pregate-observed-regular-flow-v1',
        'fixed_at': (NOW - timedelta(days=1)).isoformat(),
        'preparation_seconds': 5,
        'state_max_age_seconds': 5,
        'horizon_seconds': 900,
        'max_markout_delay_seconds': 5,
    }
    s.update(end_at=study['as_of'], admission_end_at=(NOW + timedelta(minutes=6)).isoformat())
    s['channels']['lease_seconds'] = 1263
    s['selection_basis'].update(version='selection-basis-v4', max_candidates=100, max_modifier_stages=24)
    s['entry_gate_trace']['max_candidates'] = 100
    s['journal_budget_version'] = 'bounded-envelope-v1'
    s['record_allocations'] = {**FRESH_ALLOCATIONS, 'market_state': market_state_quota}
    s['channels']['market_state_max_records'] = market_state_quota
    s['record_max_bytes'] = dict(FRESH_RECORD_BYTES)
    limits = {**s['record_allocations'], 'scan_metadata': METADATA_ALLOCATION}
    s.update(buffer_capacity=BUFFER_CAPACITY, queue_capacity=BUFFER_CAPACITY, batch_size=100,
             close_timeout_seconds=10)
    s['max_bytes'] = first_scan_journal_bound(s, limits)


async def collect_volume(tmp_path, monkeypatch, *, fail_memory=False, plan_factory=None,
                         cycles=1205, high_volume=False, market_state_quota=65000,
                         producer_checkpoint=None):
    def change(study):
        if high_volume:
            high_volume_settings(study, market_state_quota=market_state_quota)
            return
        guarded_study(study)
        s = study['capture']
        study['as_of'] = (NOW + timedelta(minutes=1, seconds=1208)).isoformat()
        s.update(end_at=study['as_of'], admission_end_at=(NOW + timedelta(minutes=6)).isoformat())
        s['channels']['lease_seconds'] = 1210
        s['selection_basis'].update(version='selection-basis-v4', max_candidates=100, max_modifier_stages=24)
        s['entry_gate_trace']['max_candidates'] = 100
        s['record_allocations'] = {'ws_quote': 21000, 'market_state': 19000, 'other': 512}
        s['channels']['market_state_max_records'] = 19000
        s['record_max_bytes'] = {'scan_metadata': 65536, 'ws_quote': 4096, 'market_state': 4096, 'other': 4096}
        s.update(buffer_capacity=40713, queue_capacity=40713, batch_size=100,
                 max_bytes=201 * (65536 + 2048) + 40512 * (4096 + 2048) + 2048, close_timeout_seconds=10)
    plan, study = load(tmp_path, change) if plan_factory is None else plan_factory(tmp_path)
    clock = high_volume_capture_clock(monkeypatch) if high_volume else advancing_capture_clock(monkeypatch)
    clock[0] = NOW
    monkeypatch.setattr(entry_gate_trace, '_now', lambda: clock[0].isoformat())
    feed = make_feed()
    bot = SimpleNamespace(market='kr', dry_run=False, ws_feed=feed)
    runtime = await ObservationRuntime.install(bot, plan, now=lambda: clock[0])
    coordinator = feed._quote_subscription_owner
    coordinator.interval = 0
    engine_prices = []

    async def quote(event):
        entry_observation.capture_quote(runtime.buffer, event)

    async def price(event):
        engine_prices.append(event.symbol)

    feed.on_quote(quote)
    feed.on_market_data(price)
    symbols = [f'{900001 + i:06}' for i in range(7)]
    candidates = [SimpleNamespace(symbol=s, score=90-i) for i, s in enumerate(symbols)]
    try:
        assert await feed.connect()
        await coordinator.set_operational(['800001'], PRICE, BOOK)
        clock[0] = plan.start_at + timedelta(seconds=1)
        scan = entry_observation.capture_scan(runtime.buffer, candidates, 'regular')
        trace = entry_gate_trace.begin_gate_trace(runtime.buffer, scan, candidates)
        observe_screen_candidates(feed, runtime.buffer, scan, candidates)
        trace.note('regime', 'fail', reason='weighted_or_index_floor')
        trace.finish()
        await asyncio.gather(*list(coordinator._tasks))
        for key in list(coordinator._states):
            await coordinator.handle_ack(ack(key), coordinator.generation)
        assert len(coordinator._states) == 17  # 후보15 + 기존 운영2
        if fail_memory:
            runtime.buffer.retained_memory_limit_bytes = runtime.buffer.retained_memory_bytes
        anchor = clock[0] + timedelta(seconds=1)
        cycles = 3 if fail_memory else cycles
        for tick in range(cycles):
            clock[0] = anchor + timedelta(seconds=tick)
            if high_volume and tick == 900:
                # Keep the 900-second endpoint after each tick-0 anchor's publication.
                clock[0] += timedelta(milliseconds=2)
            if tick in (0, 900):
                for symbol in symbols[:CANDIDATE_SYMBOLS]:
                    await receive(feed, coordinator, frame(STATUS, symbol))
            if high_volume:
                # Candidate state records are raw events: no deduplication or sampling.
                # The intermediate one-second burst is deliberately above the steady rate.
                price_count = BURST_STATES if tick == BURST_TICK else CANDIDATE_PRICE_PER_SECOND
                book_count = BURST_BOOKS if tick == BURST_TICK else CANDIDATE_BOOK_PER_SECOND
                for index, symbol in enumerate(symbols[:CANDIDATE_SYMBOLS]):
                    per_symbol, remainder = divmod(price_count, CANDIDATE_SYMBOLS)
                    await receive(feed, coordinator, price_batch(symbol, per_symbol + (index < remainder)))
                for index in range(book_count):
                    await receive(feed, coordinator, frame(BOOK, symbols[index % CANDIDATE_SYMBOLS]))
            else:
                for _ in range(3):
                    for symbol in symbols[:5]:
                        await receive(feed, coordinator, frame(PRICE, symbol))
                        await receive(feed, coordinator, frame(BOOK, symbol))
            await receive(feed, coordinator, frame(PRICE, '800001'))
            await receive(feed, coordinator, frame(BOOK, '800001'))
            if tick % 20 == 0:
                await asyncio.sleep(0)
            if producer_checkpoint is not None:
                await producer_checkpoint(runtime, tick)
        assert engine_prices == ['800001'] * cycles
        assert not runtime.buffer.llm_admission and not runtime.buffer._llm_symbols
        retained = runtime.buffer.retained_memory_bytes
        counts = Counter(r['kind'] for r in runtime.buffer._records)
        clock[0] = plan.end_at
        await runtime.close()
        assert runtime.result['sealed']
        observed = read_observation_journal(plan.settings['journal_path'], max_bytes=256 * 1024 * 1024,
                                             expected_study_sha256=plan.study_sha256)
        return plan, study, observed, retained, counts, runtime.buffer, clock[0]
    finally:
        clock[0] = plan.end_at
        await runtime.close()
        await feed.disconnect()


@pytest.mark.asyncio
async def test_guarded_pilot_scaled_synthetic_stream_seals_and_reads(tmp_path, monkeypatch):
    plan, study, observed, retained, counts, buffer, now = await collect_volume(tmp_path, monkeypatch)
    assert observed['complete'] and observed['dropped_records'] == 0
    assert counts['ws_quote'] == 16 * 1205 and counts['market_state'] == 15 * 1205 + 10
    assert retained <= 512 * 1024 * 1024 and not buffer.memory_exhausted
    report = build_pregate_candidate_report(plan.study_bytes, observed,
        study_sha256=plan.study_sha256, analysis_as_of=now.isoformat())
    assert len(report['candidates']) == 7
    assert all(r['entry_quote'] and r['markout_quote'] for r in report['candidates'][:5])
    assert all(r['entry_market_state']['verdict'] == r['markout_market_state']['verdict'] == 'normal_continuous'
               for r in report['candidates'][:5])
    assert report['account_return'] is None and not report['production_eligible']
    from src.analytics.pregate_capture_capacity import assess_pregate_capture_resources
    capacity = assess_pregate_capture_resources(study, operational_registrations=2, external_registrations=0)
    assert capacity['checks']['readout_limit']['passed']
    assert capacity['numeric_configuration_passed']
    uniform = deepcopy(study)
    uniform['capture'].pop('record_max_bytes')
    uniform['capture']['max_bytes'] = 40713 * (65536 + 2048) + 2048
    assert not assess_pregate_capture_resources(uniform, operational_registrations=2,
        external_registrations=0)['checks']['readout_limit']['passed']
    assert not capacity['installation_ready']
    print('SYNTHETIC_VOLUME_RECEIPT=' + json.dumps({
        'cycles': 1205, 'candidate_symbols': 5, 'candidate_channels': 15, 'operational_channels': 2,
        'frames': 32 * 1205 + 10, 'records': len(observed['records']), 'counts': dict(counts),
        'retained_record_bytes': retained, 'journal_bytes': Path(plan.settings['journal_path']).stat().st_size,
        'configured_journal_max_bytes': plan.settings['max_bytes'], 'reader_limit_bytes': 256 * 1024 * 1024,
        'process_peak_rss_kib': resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        'live_volume_qualified': False, 'installation_ready': False}, sort_keys=True))


@pytest.mark.asyncio
async def test_high_volume_raw_candidate_stream_seals_reads_and_preserves_regular_flow(tmp_path, monkeypatch):
    """Dropping, sampling, or deduplicating raw state events must fail this exact-count stream."""
    started = monotonic()
    plan, study, observed, retained, counts, buffer, now = await collect_volume(
        tmp_path, monkeypatch, cycles=CAPTURE_SECONDS, high_volume=True)
    duration = monotonic() - started
    expected_states = CAPTURE_SECONDS * CANDIDATE_PRICE_PER_SECOND + (BURST_STATES - CANDIDATE_PRICE_PER_SECOND) + 10
    expected_books = (CAPTURE_SECONDS * CANDIDATE_BOOK_PER_SECOND + (BURST_BOOKS - CANDIDATE_BOOK_PER_SECOND)
                      + CAPTURE_SECONDS)
    assert observed['complete'] and observed['dropped_records'] == 0
    assert counts['ws_quote'] == expected_books and counts['market_state'] == expected_states
    state_seconds = Counter(row['received_at'][:19] for row in observed['records']
                            if row.get('kind') == 'market_state' and row.get('tr_id') == PRICE)
    candidate_book_seconds = Counter(row['provenance']['received_at'][:19] for row in observed['records']
                                     if row.get('kind') == 'ws_quote' and row.get('symbol') in {
                                         '900001', '900002', '900003', '900004', '900005'})
    all_book_seconds = Counter(row['provenance']['received_at'][:19] for row in observed['records']
                               if row.get('kind') == 'ws_quote')
    assert max(state_seconds.values()) == BURST_STATES
    assert max(candidate_book_seconds.values()) == BURST_BOOKS
    assert max(all_book_seconds.values()) == BURST_BOOKS + 1
    assert buffer._record_counts['scan_metadata'] <= METADATA_ALLOCATION
    assert buffer._record_counts['ws_quote'] == expected_books
    assert buffer._record_counts['market_state'] == expected_states
    assert buffer._record_counts['other'] <= FRESH_ALLOCATIONS['other']
    assert plan.settings['record_allocations'] == FRESH_ALLOCATIONS
    assert plan.settings['record_max_bytes'] == FRESH_RECORD_BYTES
    assert plan.settings['buffer_capacity'] == plan.settings['queue_capacity'] == BUFFER_CAPACITY
    assert plan.settings['max_bytes'] == 240144543
    assert retained <= 512 * 1024 * 1024 and not buffer.memory_exhausted
    assert Path(plan.settings['journal_path']).stat().st_size <= 256 * 1024 * 1024
    report = build_pregate_candidate_report(plan.study_bytes, observed,
        study_sha256=plan.study_sha256, analysis_as_of=now.isoformat())
    assert len(report['candidates']) == 7
    assert all(r['entry_quote'] and r['markout_quote'] for r in report['candidates'][:5])
    assert all(r['entry_market_state']['verdict'] == r['markout_market_state']['verdict'] == 'normal_continuous'
               for r in report['candidates'][:5])
    assert report['account_return'] is None and not report['production_eligible']
    observed_flow = build_observed_regular_flow_report(plan.study_bytes, observed,
        study_sha256=plan.study_sha256, analysis_as_of=now.isoformat())
    assert observed_flow['strict_report'] == report
    assert observed_flow['counts'] == {'denominator': 7, 'eligible_pairs': 5}, [
        (row['symbol'], row['reasons']) for row in observed_flow['candidates'][:5]]
    assert all(row['gross_quote_move_pct'] is not None for row in observed_flow['candidates'][:5])
    assert all(row['markout_quote'] is not None for row in observed_flow['candidates'][:5])
    from src.analytics.pregate_capture_capacity import assess_pregate_capture_resources
    capacity = assess_pregate_capture_resources(study, operational_registrations=2, external_registrations=0)
    assert capacity['checks']['readout_limit']['passed']
    assert capacity['numeric_configuration_passed']
    assert not capacity['installation_ready']
    print('HIGH_VOLUME_SYNTHETIC_RECEIPT=' + json.dumps({
        'cycles': CAPTURE_SECONDS, 'candidate_symbols': CANDIDATE_SYMBOLS, 'candidate_channels': 15,
        'candidate_price_per_second': CANDIDATE_PRICE_PER_SECOND, 'candidate_book_per_second': CANDIDATE_BOOK_PER_SECOND,
        'burst_tick': BURST_TICK, 'burst_states': BURST_STATES, 'burst_books': BURST_BOOKS,
        'operational_channels': 2, 'records': len(observed['records']), 'counts': dict(counts),
        'retained_record_bytes': retained, 'journal_bytes': Path(plan.settings['journal_path']).stat().st_size,
        'configured_journal_max_bytes': plan.settings['max_bytes'], 'reader_limit_bytes': 256 * 1024 * 1024,
        'duration_seconds': duration,
        'process_peak_rss_kib': resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        'live_volume_qualified': False, 'installation_ready': False}, sort_keys=True))


@pytest.mark.asyncio
async def test_exhausted_observer_preserves_operational_prices_and_seals_incomplete(tmp_path, monkeypatch):
    plan, _, observed, _, counts, buffer, now = await collect_volume(tmp_path, monkeypatch, fail_memory=True)
    assert buffer.memory_exhausted
    assert not observed['complete'] and observed['dropped_records'] > 0
    assert 'retained_memory_limit' in observed['incomplete_reasons']
    assert counts['ws_quote'] == counts['market_state'] == 0
    with pytest.raises(ValueError, match='sealed complete undropped'):
        build_pregate_candidate_report(plan.study_bytes, observed,
            study_sha256=plan.study_sha256, analysis_as_of=now.isoformat())


@pytest.mark.asyncio
async def test_old_19000_state_budget_fails_closed_before_high_volume_window_completes(tmp_path, monkeypatch):
    """A regression to the 19,000 raw-state allowance must seal incomplete, not sample events."""
    cycles = 381  # 380 seconds fit exactly; the next one-second 50-state slice crosses 19,000.
    plan, _, observed, _, counts, buffer, _ = await collect_volume(
        tmp_path, monkeypatch, cycles=cycles, high_volume=True, market_state_quota=19000)
    assert not observed['complete']
    assert 'market_state_record_limit' in observed['incomplete_reasons']
    assert counts['market_state'] == 19000
    assert buffer._record_counts['market_state'] == 19000
    # Price delivery keeps its operational contract despite observer exhaustion.
    assert plan.settings['channels']['market_state_max_records'] == 19000
