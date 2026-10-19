"""Real first-scan producer -> sealed journal -> offline readout, synthetic socket only."""
import asyncio
from copy import deepcopy
from datetime import timedelta
import hashlib
import json
from types import SimpleNamespace

import pytest

from src.analytics import entry_gate_trace, entry_observation
from src.analytics.entry_observation_journal import read_observation_journal
from src.analytics.entry_observation_runtime import ObservationRuntime
from src.data.feeds.quote_subscription import observe_screen_candidates
from test_entry_observation_runtime import load, NOW
from test_kis_frame_diagnostics import v4_change
from test_market_state_capture import advancing_capture_clock, frame, PRICE, BOOK, STATUS, receive
from test_quote_subscription import ack, make_feed


def study_change(study):
    v4_change(study)
    capture = study['capture']
    capture['selection_basis']['max_candidates'] = 10
    capture['entry_gate_trace']['max_candidates'] = 10
    study['as_of'] = (NOW + timedelta(minutes=31)).isoformat()
    capture.update(end_at=study['as_of'])
    capture['channels'].update(lease_seconds=1800, market_state_evidence=True,
                               market_state_max_records=100)
    capture['max_bytes'] = capture['buffer_capacity'] * (capture['max_record_bytes'] + 2048) + 2048
    study.update(outcome_basis='fixed_horizon_bid_markout', markout_policy={
        'version': 'fixed-horizon-bid-v1', 'policy_ref': 'synthetic-pregate-900',
        'fixed_at': (NOW - timedelta(days=1)).isoformat(), 'horizon_seconds': 900,
        'max_delay_seconds': 5, 'anchor': 'entry_quote_observed_at'})


async def collect_first_scan(tmp_path, monkeypatch, empty_first=False):
    plan, _ = load(tmp_path, study_change)
    clock = advancing_capture_clock(monkeypatch)
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
        clock[0] = plan.start_at + timedelta(seconds=1)
        cohort = [] if empty_first else candidates
        scan_id = entry_observation.capture_scan(runtime.buffer, cohort, 'regular')
        trace = entry_gate_trace.begin_gate_trace(runtime.buffer, scan_id, cohort)
        observe_screen_candidates(feed, runtime.buffer, scan_id, cohort)
        trace.note('regime', 'fail', reason='weighted_or_index_floor')
        trace.finish()
        await asyncio.gather(*list(coordinator._tasks))
        later = entry_observation.capture_scan(runtime.buffer, candidates, 'regular')
        assert later is None
        observe_screen_candidates(feed, runtime.buffer, later, candidates)
        await asyncio.gather(*list(coordinator._tasks))
        assert len(coordinator._leases) == (0 if empty_first else 5)
        if not empty_first:
            assert len(coordinator._states) == 15
            for key in list(coordinator._states):
                await coordinator.handle_ack(ack(key), coordinator.generation)
            for symbol in symbols[:5]:
                await receive(feed, coordinator, frame(PRICE, symbol))
                await receive(feed, coordinator, frame(STATUS, symbol))
                await receive(feed, coordinator, frame(BOOK, symbol))
            entries = [r for r in runtime.buffer.export()['records'] if r['kind'] == 'ws_quote']
            assert len(entries) == 5
            clock[0] += timedelta(seconds=901)
            for symbol in symbols[:5]:
                await receive(feed, coordinator, frame(PRICE, symbol))
                await receive(feed, coordinator, frame(STATUS, symbol))
                await receive(feed, coordinator, frame(BOOK, symbol))
        assert engine_prices == []  # candidate-only trade frames never become trading signals
        clock[0] = plan.end_at
        await runtime.close()
        assert runtime.result['sealed']
    finally:
        clock[0] = max(clock[0], plan.end_at)
        await runtime.close()
        await feed.disconnect()

    observed = read_observation_journal(plan.settings['journal_path'],
        max_bytes=plan.settings['max_bytes'], expected_study_sha256=plan.study_sha256)
    assert observed['complete'] and observed['dropped_records'] == 0
    return plan, observed, clock[0].isoformat(), symbols


@pytest.mark.asyncio
@pytest.mark.parametrize('empty_first', [False, True])
async def test_real_first_scan_collector_to_candidate_readout(tmp_path, monkeypatch, empty_first):
    from src.analytics.pregate_candidate_readout import build_pregate_candidate_report
    plan, observed, as_of, symbols = await collect_first_scan(tmp_path, monkeypatch, empty_first)
    result = build_pregate_candidate_report(plan.study_bytes, observed,
        study_sha256=plan.study_sha256, analysis_as_of=as_of)
    assert len(result['candidates']) == (0 if empty_first else 7)
    assert result['account_return'] is None
    assert result['profitability_pass'] is result['production_eligible'] is False
    if not empty_first:
        rows = result['candidates']
        assert [r['symbol'] for r in rows] == symbols
        assert [r['rank'] for r in rows] == list(range(1, 8))
        assert all(r['entry_quote'] and r['markout_quote'] for r in rows[:5])
        assert all(r['entry_quote'] is None for r in rows[5:])
        assert all(r['allocation_status'] == 'not_subscribed_by_fixed_top_n' for r in rows[5:])
        assert all(r['entry_market_state']['verdict'] == 'normal_continuous' for r in rows[:5])
        assert all(r['markout_market_state']['verdict'] == 'normal_continuous' for r in rows[:5])


@pytest.mark.asyncio
@pytest.mark.parametrize('defect', ['source_ref', 'candidate_limit', 'policy', 'policy_without_entries', 'admission_edge', 'short_tail_empty'])
async def test_real_capture_rejects_changed_study_contract(tmp_path, monkeypatch, defect):
    from src.analytics.pregate_candidate_readout import build_pregate_candidate_report
    plan, observed, as_of, _ = await collect_first_scan(tmp_path, monkeypatch, defect in ('policy_without_entries', 'short_tail_empty'))
    study = json.loads(plan.study_bytes)
    if defect == 'source_ref':
        study['capture']['source_version_ref'] = 'other-source'
        study['capture']['entry_gate_trace']['source_version_ref'] = 'other-source'
    elif defect == 'candidate_limit':
        study['capture']['selection_basis']['max_candidates'] = 5
        study['capture']['entry_gate_trace']['max_candidates'] = 5
    elif defect in ('policy', 'policy_without_entries'):
        study['markout_policy']['horizon_seconds'] = 60
    elif defect == 'short_tail_empty':
        study['capture']['admission_end_at'] = (plan.end_at - timedelta(seconds=901)).isoformat()
    else:
        study['capture']['admission_end_at'] = next(r['observed_at'] for r in observed['records'] if r['kind'] == 'scan')
    raw = json.dumps(study).encode()
    sha = hashlib.sha256(raw).hexdigest()
    # The source hash is self-consistent; the internal producer declarations
    # must still agree with this study. This does not claim source authenticity.
    observed = deepcopy(observed)
    observed['journal']['study_sha256'] = sha
    with pytest.raises(ValueError):
        build_pregate_candidate_report(raw, observed, study_sha256=sha, analysis_as_of=as_of)


@pytest.mark.asyncio
@pytest.mark.parametrize('defect', ['generation', 'same_time', 'stale', 'missing_status'])
async def test_actual_state_records_remain_unknown_when_unqualified(tmp_path, monkeypatch, defect):
    from src.analytics.pregate_candidate_readout import build_pregate_candidate_report
    plan, observed, as_of, symbols = await collect_first_scan(tmp_path, monkeypatch)
    first = next(r for r in observed['records'] if r['kind'] == 'ws_quote' and r['symbol'] == symbols[0])
    state = next(r for r in observed['records'] if r['kind'] == 'market_state' and r['symbol'] == symbols[0] and r['tr_id'] == STATUS)
    if defect == 'generation':
        state['generation'] += 1
    elif defect == 'same_time':
        state['received_at'] = first['provenance']['received_at']
    elif defect == 'stale':
        state['received_at'] = (plan.start_at - timedelta(seconds=60)).isoformat()
    else:
        observed['records'].remove(state)
        for index, row in enumerate(observed['records'], 1):
            row['sequence'] = index
    result = build_pregate_candidate_report(plan.study_bytes, observed,
        study_sha256=plan.study_sha256, analysis_as_of=as_of)
    assert result['candidates'][0]['entry_market_state']['verdict'] == 'unknown'


@pytest.mark.asyncio
async def test_both_clis_use_the_real_study_and_preserve_input_bytes(tmp_path, monkeypatch, capsys):
    from scripts.assess_pregate_capture import main as assess_main
    from scripts.report_pregate_candidate_observation import main as report_main
    plan, _, as_of, _ = await collect_first_scan(tmp_path, monkeypatch)
    from pathlib import Path
    journal = Path(plan.settings['journal_path'])
    before = plan.study_path.read_bytes(), journal.read_bytes()
    assert assess_main([str(plan.study_path), '--operational-registrations', '16', '--external-registrations', '0']) == 0
    assessment = json.loads(capsys.readouterr().out)
    assert assessment['installation_ready'] is False
    assert assessment['record_volume_qualified'] is False
    assert report_main(['--study', str(plan.study_path), '--journal', str(journal),
                       '--as-of', as_of, '--max-journal-bytes', str(plan.settings['max_bytes'])]) == 0
    assert len(json.loads(capsys.readouterr().out)['candidates']) == 7
    assert (plan.study_path.read_bytes(), journal.read_bytes()) == before
