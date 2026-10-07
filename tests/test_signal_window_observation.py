"""Synthetic multi-scan evidence; no account, feed socket or installation."""
import asyncio
from copy import deepcopy
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from src.analytics import entry_observation as obs
from src.analytics import entry_gate_trace as gates
from src.analytics.entry_gate_report import build_gate_report
from src.analytics.entry_observation_runtime import _WindowBuffer, ObservationRuntime
from src.data.feeds.quote_subscription import observe_screen_candidates
from test_entry_observation_runtime import load, NOW, owner, read
from test_entry_gate_trace import gate_plan_change
from test_entry_capital_limits import capital_policy
from test_entry_markout import policy


def window_plan(c):
    gate_plan_change(c)
    c['capture'].update(version='runner-signal-window-v1', max_scans=3,
                        quote_admission='signal_created',
                        max_bytes=4000000,
                        frame_diagnostics={'version': 'kis-frame-diagnostics-v1'})
    c.update(capital_policy=capital_policy(), outcome_basis='fixed_horizon_bid_markout',
             markout_policy={**policy(), 'horizon_seconds': 60})


def buffer(tmp_path, monkeypatch):
    plan, context = load(tmp_path, window_plan)
    clock = [plan.start_at]
    monkeypatch.setattr(obs, '_now', lambda: clock[0].isoformat())
    monkeypatch.setattr(gates, '_now', lambda: clock[0].isoformat())
    return plan, context, _WindowBuffer(plan, lambda: clock[0]), clock


def stock(symbol='900001'):
    return SimpleNamespace(symbol=symbol, score=90)


def event(sid, symbol='900001'):
    return SimpleNamespace(id=sid, symbol=symbol, timestamp=NOW,
        strategy=SimpleNamespace(value='gap_and_go'), price=10000,
        target_price=12000, stop_price=9500)


def test_later_scan_repeated_symbol_distinct_gate_and_full_denominator(tmp_path, monkeypatch):
    plan, _, b, clock = buffer(tmp_path, monkeypatch)
    ids = []
    for n in range(2):
        sid = obs.capture_scan(b, [stock()], 'regular'); ids.append(sid)
        trace = gates.begin_gate_trace(b, sid, [])
        trace.note('enabled', 'fail'); trace.finish()
        assert gates.begin_gate_trace(b, sid, []) is gates.NULL_TRACE
        clock[0] += timedelta(seconds=1)
    assert all(ids) and len(set(ids)) == 2
    report = build_gate_report(b.export(), as_of=plan.end_at.isoformat())
    assert report['population']['population_scope'] == 'window_returned_scan_candidates'
    assert report['counts']['total'] == report['counts']['blocked'] == 2
    assert report['trace_coverage_complete'] is True
    assert not b.accepts_order_signal('foreign')


def test_empty_and_failed_scans_consume_cap_without_replacement(tmp_path, monkeypatch):
    _, _, b, _ = buffer(tmp_path, monkeypatch)
    assert obs.capture_scan(b, [], 'regular')
    class Broken:
        @property
        def symbol(self): raise ValueError('synthetic')
    assert obs.capture_scan(b, [Broken()], 'regular') is None
    assert obs.capture_scan(b, [], 'regular')
    assert obs.capture_scan(b, [], 'regular') is None
    assert b.capture_status()['complete'] is False
    assert 'scan_limit_exceeded' in b.capture_status()['incomplete_reasons']


@pytest.mark.parametrize('defect', ['missing_capital', 'capital_source', 'capital_config',
    'missing_markout', 'bool_cap', 'large_cap', 'quote_admission', 'zero_cap'])
def test_window_contract_rejects_unbound_or_unbounded_plan(tmp_path, defect):
    def change(c):
        window_plan(c)
        if defect == 'missing_capital': c.pop('capital_policy')
        elif defect == 'capital_source': c['capital_policy']['source_version_ref'] = 'other'
        elif defect == 'capital_config': c['capital_policy']['configuration_ref'] = 'other'
        elif defect == 'missing_markout': c.pop('outcome_basis'); c.pop('markout_policy')
        elif defect == 'quote_admission': c['capture']['quote_admission'] = 'all_candidates'
        else: c['capture']['max_scans'] = {'bool_cap': True, 'large_cap': 101, 'zero_cap': 0}[defect]
    with pytest.raises(ValueError): load(tmp_path, change)


@pytest.mark.asyncio
async def test_only_captured_signals_request_quotes_and_emit_result_is_unchanged(tmp_path, monkeypatch):
    plan, _ = load(tmp_path, window_plan); bot = owner(); clock = [NOW]
    monkeypatch.setattr(obs, '_now', lambda: clock[0].isoformat())
    runtime = await ObservationRuntime.install(bot, plan, now=lambda: clock[0])
    b = runtime.buffer; coordinator = bot.ws_feed._quote_subscription_owner
    try:
        clock[0] = plan.start_at
        scan1 = obs.capture_scan(b, [stock()], 'regular')
        observe_screen_candidates(bot.ws_feed, b, scan1, [stock()])
        await asyncio.sleep(0)
        assert coordinator.snapshot()['leases'] == 0
        scan2 = obs.capture_scan(b, [stock()], 'regular')
        emitted = []
        async def emit(e): emitted.append(e); return 'original-result'
        e = event('later-signal')
        assert await obs.emit_with_observation(SimpleNamespace(emit=emit), e, b, scan2) == 'original-result'
        await asyncio.gather(*list(coordinator._tasks))
        assert emitted == [e] and b.accepts_order_signal(e.id)
        assert set(coordinator._leases) == {f'{scan2}:900001'}
        obs.capture_signal(b, 'foreign', event('foreign-signal'))
        assert not b.accepts_order_signal('foreign-signal')
        clock[0] = plan.end_at
        obs.capture_signal(b, scan1, event('too-late'))
        assert not b.accepts_order_signal('too-late')
    finally:
        clock[0] = plan.end_at
        await runtime.close()
    assert read(plan)['complete']
    assert coordinator.snapshot()['leases'] == 0


def test_window_boundary_and_unrecorded_scan_are_incomplete(tmp_path, monkeypatch):
    plan, _, b, clock = buffer(tmp_path, monkeypatch)
    assert b.capture_status()['complete'] is False
    clock[0] = plan.start_at - timedelta(microseconds=1)
    assert obs.capture_scan(b, [], 'regular') is None
    clock[0] = b.admission_end
    assert obs.capture_scan(b, [], 'regular') is None
    assert not b.selection_capture_enabled


def test_signal_subscription_failure_does_not_discard_signal(tmp_path, monkeypatch):
    _, _, b, _ = buffer(tmp_path, monkeypatch)
    scan = obs.capture_scan(b, [stock()], 'regular')
    def broken(*args): raise RuntimeError('synthetic')
    b.signal_quote_submit = broken
    obs.capture_signal(b, scan, event('kept'))
    assert b.accepts_order_signal('kept')
    assert 'signal_enrollment_failed' in b.capture_status()['incomplete_reasons']


def test_toss_projection_rejects_window_even_before_first_scan(tmp_path, monkeypatch):
    from src.observation.entry_anchor_input import project_anchors
    _, _, b, _ = buffer(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match='first_scan'):
        project_anchors(SimpleNamespace(buffer=b))


@pytest.mark.parametrize('budget', ['buffer_capacity', 'max_bytes'])
def test_window_requires_metadata_budget_for_all_declared_scans(tmp_path, budget):
    def change(c):
        window_plan(c)
        c['capture'][budget] = 10 if budget == 'buffer_capacity' else 500000
    with pytest.raises(ValueError, match='예산'): load(tmp_path, change)


@pytest.mark.parametrize('defect', ['before', 'at_end', 'reference', 'legacy_scope', 'legacy_study',
                                  'count', 'mixed_gate', 'missing_gate', 'wrong_selection_bound'])
def test_offline_window_binding_rejects_foreign_scope_or_time(tmp_path, monkeypatch, defect):
    from src.analytics.entry_observation_runtime import validate_window_contract
    plan, context, b, _ = buffer(tmp_path, monkeypatch)
    for _ in range(2): obs.capture_scan(b, [stock()], 'regular')
    data = b.export(); scans = [r for r in data['records'] if r['kind'] == 'scan']
    if defect == 'before': scans[0]['observed_at'] = (plan.start_at - timedelta(seconds=1)).isoformat()
    elif defect == 'at_end': scans[0]['observed_at'] = b.admission_end.isoformat()
    elif defect == 'reference': scans[0]['scan_admission_ref'] = 'other'
    elif defect == 'legacy_scope': scans[0]['population_scope'] = 'returned_screen_candidates'
    elif defect == 'legacy_study': context['capture']['version'] = 'runner-first-scan-v4'
    elif defect == 'count': context['capture']['max_scans'] = 1
    elif defect == 'mixed_gate': scans[1]['entry_gate_configuration_ref'] = 'other'
    elif defect == 'missing_gate':
        for scan in scans:
            for key in gates.SCAN_FIELDS: scan.pop(key)
    else:
        for scan in scans: scan['selection_basis_max_candidates'] = 99
    with pytest.raises(ValueError):
        validate_window_contract(context, data)
        build_gate_report(data, as_of=context['as_of'])


@pytest.mark.asyncio
async def test_repeated_signal_lease_cap_and_emit_exception_preserved(tmp_path, monkeypatch):
    plan, _, b, clock = buffer(tmp_path, monkeypatch)
    from src.data.feeds.quote_subscription import QuoteSubscriptionCoordinator
    q = QuoteSubscriptionCoordinator(b, **{**plan.settings['channels'], 'max_candidates': 1})
    b.signal_quote_submit = q.submit
    for i in range(2):
        sid = obs.capture_scan(b, [stock()], 'regular')
        async def fail(e): raise RuntimeError('original-emit-error')
        with pytest.raises(RuntimeError, match='original-emit-error'):
            await obs.emit_with_observation(SimpleNamespace(emit=fail), event(f's{i}'), b, sid)
        await asyncio.gather(*list(q._tasks))
    rows = b.export()['records']
    assert q.snapshot()['leases'] == 1
    assert sum(r['kind'] == 'signal' for r in rows) == 2
    assert [r['emitted'] for r in rows if r['kind'] == 'emit_result'] == [False, False]
    assert any(r.get('status') == 'unallocated' and r.get('candidate_id') == f'{sid}:900001' for r in rows)


@pytest.mark.asyncio
async def test_window_signal_waiting_for_lock_cannot_restore_ended_lease(tmp_path, monkeypatch):
    plan, _, b, _ = buffer(tmp_path, monkeypatch)
    from src.data.feeds.quote_subscription import QuoteSubscriptionCoordinator
    q = QuoteSubscriptionCoordinator(b, **plan.settings['channels'])
    b.signal_quote_submit = q.submit
    await q._lock.acquire()
    sid = obs.capture_scan(b, [stock()], 'regular')
    obs.capture_signal(b, sid, event('waiting'))
    waiting = list(q._tasks)
    q.end_observation(); q._lock.release()
    await asyncio.gather(*waiting)
    assert q.snapshot()['leases'] == 0


@pytest.mark.parametrize('late_markout', [False, True])
def test_second_scan_economics_preserves_unobserved_first_candidate(tmp_path, late_markout):
    from test_entry_evaluation_bundle import fixture, review, build
    import hashlib
    import json
    c, o, _ = fixture()
    # Existing fully specified capital/fee/markout fixture, add an earlier scan
    # that yielded no signal. Never discard it from the economic denominator.
    original = next(r for r in o['records'] if r['kind'] == 'scan')
    early = deepcopy(original)
    early.update(scan_id='earlier', observed_at='2026-10-02T09:14:00+09:00')
    early['candidates'] = [{**deepcopy(original['candidates'][0]), 'candidate_id':
                            'earlier:' + original['candidates'][0]['symbol']}]
    o['records'].insert(0, early)
    _, declared = load(tmp_path, window_plan)
    c['capture'] = declared['capture']
    c['capture'].update(start_at='2026-10-02T09:13:00+09:00',
                        admission_end_at='2026-10-02T09:20:00+09:00', end_at=c['as_of'],
                        source_version_ref=c['capital_policy']['source_version_ref'],
                        configuration_ref=c['capital_policy']['configuration_ref'])
    c['capture']['entry_gate_trace'].update(max_candidates=5,
        source_version_ref=c['capture']['source_version_ref'], configuration_ref=c['capture']['configuration_ref'])
    c['capture']['selection_basis']['max_candidates'] = 5
    for r in o['records']:
        if r['kind'] == 'scan':
            r.update(population_scope='window_returned_scan_candidates',
                     scan_admission_ref=c['capture']['scan_admission_ref'],
                     selection_basis_expected=True, selection_basis_max_candidates=5,
                     selection_basis_max_terms=16, selection_sources_expected=True,
                     selection_sources_status='unavailable', **gates.scan_fields(c['capture']['entry_gate_trace']))
    if late_markout:
        # Capture ends before the first eligible markout; do not backfill it.
        o['records'] = [r for r in o['records'] if not (r['kind'] == 'ws_quote'
                        and 'mark' in r['quote_id'])]
    # Construct a causal synthetic event stream; this is never applied to observed input.
    o['records'].sort(key=lambda r: datetime.fromisoformat(r['observed_at']))
    for i, r in enumerate(o['records'], 1): r['sequence'] = i
    o['frame_diagnostics'] = c['capture']['frame_diagnostics']
    o['journal'].update(format='entry-observation-journal-v2', frame_diagnostics=o['frame_diagnostics'])
    sha = hashlib.sha256((json.dumps(c, ensure_ascii=False, sort_keys=True,
                         separators=(',', ':')) + '\n').encode()).hexdigest()
    o['journal']['study_sha256'] = sha
    out = build(c, o, sha, review(c, o, sha))
    assert out['readiness']['original_cohort_count'] == 5
    assert out['original']['report']['counts']['paired_outcomes'] == (0 if late_markout else 3)
    assert out['review_tasks'][0]['reasons'] == ['MISSING_OR_AMBIGUOUS_SIGNAL']
    assert out['production_eligible'] is False and out['account_return'] is None


@pytest.mark.asyncio
async def test_sealed_window_gate_cli_uses_per_scan_candidate_bound(tmp_path, monkeypatch, capsys):
    import src.analytics.entry_observation_journal as journal
    from scripts.report_entry_gate_trace import main
    plan, _ = load(tmp_path, window_plan); bot = owner(); clock = [NOW]
    for mod in (obs, gates): monkeypatch.setattr(mod, '_now', lambda: clock[0].isoformat())
    monkeypatch.setattr(journal, '_timestamp', lambda: clock[0].isoformat())
    runtime = await ObservationRuntime.install(bot, plan, now=lambda: clock[0])
    try:
        clock[0] = plan.start_at
        for _ in range(2):
            scan = obs.capture_scan(runtime.buffer, [stock(), stock('900002')], 'regular')
            trace = gates.begin_gate_trace(runtime.buffer, scan, [])
            trace.note('enabled', 'fail'); trace.finish()
            clock[0] += timedelta(seconds=1)
    finally:
        clock[0] = plan.end_at
        await runtime.close()
    assert main(['--journal', plan.settings['journal_path'], '--study', str(plan.study_path),
                 '--as-of', plan.end_at.isoformat(), '--max-journal-bytes', '4000000']) == 0
    import json
    result = json.loads(capsys.readouterr().out)
    assert result['counts']['total'] == 4 and result['trace_coverage_complete'] is True


def test_toss_offline_reader_rejects_even_a_single_window_scan():
    from test_toss_orderbook_stream import engine_records, report
    data = engine_records()
    data['records'][0].update(population_scope='window_returned_scan_candidates',
                              scan_admission_ref='synthetic-window')
    with pytest.raises(ValueError, match='first.scan'):
        report(engine=data)


def test_toss_projection_consumer_rejects_window_scope():
    from test_toss_ws_runtime import projection
    from src.observation.entry_anchor_input import validate_projection
    data = projection()
    data['records'][0].update(population_scope='window_returned_scan_candidates',
                              scan_admission_ref='synthetic-window')
    with pytest.raises(ValueError, match='first.scan'):
        validate_projection(data)


@pytest.mark.asyncio
async def test_window_gate_cli_rejects_legacy_journal_with_matching_study_hash(tmp_path, monkeypatch):
    import src.analytics.entry_observation_journal as journal
    from scripts.report_entry_gate_trace import main
    plan, _ = load(tmp_path, window_plan); clock = [NOW]
    for mod in (obs, gates): monkeypatch.setattr(mod, '_now', lambda: clock[0].isoformat())
    monkeypatch.setattr(journal, '_timestamp', lambda: clock[0].isoformat())
    b = _WindowBuffer(plan, lambda: clock[0])
    b.frame_diagnostics_settings = None  # Deliberate incompatible producer, same study hash.
    s = plan.settings
    j = await journal.ObservationJournal.open(b, s['journal_path'], study_ref=s['study_ref'],
        study_sha256=plan.study_sha256,
        **{k: s[k] for k in ('queue_capacity', 'batch_size', 'max_bytes', 'max_record_bytes')})
    try:
        clock[0] = plan.start_at
        scan = obs.capture_scan(b, [stock()], 'regular')
        trace = gates.begin_gate_trace(b, scan, [])
        trace.note('enabled', 'fail'); trace.finish()
    finally:
        clock[0] = plan.end_at
        await j.close()
    assert main(['--journal', s['journal_path'], '--study', str(plan.study_path),
                 '--as-of', plan.end_at.isoformat(), '--max-journal-bytes', '4000000']) == 2
