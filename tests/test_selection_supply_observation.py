"""Explicit policy evidence must describe the decision input, not later cache state."""
from copy import deepcopy
from datetime import datetime, timezone
from dataclasses import asdict
import pytest
from test_selection_source_basis import run
from test_selection_supply_policy import native_screener
from src.analytics.selection_basis import validate_settings, validate_basis, build_selection_report
from src.analytics.entry_observation import EntryObservationBuffer, capture_scan

SETTINGS = dict(version='selection-basis-v4', max_candidates=100, max_source_terms=16, max_modifier_stages=24)
FLAGS = dict(capture_selection=True, capture_selection_modifiers=True, capture_selection_supply_policy=True)


def test_v4_requires_explicit_settings_and_distinct_payload(monkeypatch):
    assert validate_settings(SETTINGS) == SETTINGS
    s, _, _ = native_screener(monkeypatch, '0')
    result = run(s, **FLAGS)
    assert result[0].selection_basis['version'] == 'kr-selection-basis-v3'


@pytest.mark.parametrize('raw,state,actual,legacy', [('0','observed_nonpositive',30,30),('-1','observed_nonpositive',30,30),('1','positive',50,30),('MISSING','unobserved',50,30)])
def test_spdi_compares_native_policy_against_old_flags(monkeypatch, raw, state, actual, legacy):
    s, _, calls = native_screener(monkeypatch, raw)
    result = run(s, **FLAGS)
    b = result[0].selection_basis
    decision = b['supply_policy_decisions']['spdi']
    assert decision['foreign']['state'] == state
    assert decision['before'] == 50 and decision['after'] == actual
    shadow = b['supply_policy_shadow']
    assert shadow['status'] == 'comparable'
    assert shadow['legacy_after'] == legacy and shadow['actual_after'] == actual
    assert shadow['delta_vs_legacy'] == actual - legacy
    assert shadow['final_score_delta'] is shadow['gate_delta'] is shadow['economic_effect'] is None
    assert len(calls) == 4
    validate_basis(b, score=result[0].score, rank=1, observed_at=datetime.now(timezone.utc).isoformat())


def test_snapshot_keeps_spdi_input_when_proof_expires_before_later_stage(monkeypatch):
    from src.analytics import selection_supply_policy as policy
    now = [1000.0]
    monkeypatch.setattr(policy.time, 'monotonic', lambda: now[0])
    s, _, _ = native_screener(monkeypatch, '0')
    async def expire(*args): now[0] = 1601.0
    s._apply_inst_sell_blacklist = expire
    result = run(s, overnight_sentiment='bearish', **FLAGS)
    b = result[0].selection_basis
    assert result[0].score == 25  # SPDI -20 before expiry, bearish baseline -5 afterward
    spdi = b['supply_policy_decisions']['spdi']
    later = b['supply_policy_decisions']['overnight_regime']
    assert spdi['foreign']['state'] == 'observed_nonpositive'
    assert spdi['foreign']['proof_age_seconds'] == 0
    assert later['foreign']['state'] == 'unobserved'
    assert later['foreign']['proof_age_seconds'] == 601
    assert b['supply_policy_shadow']['actual_after'] == 30
    validate_basis(b, score=25, rank=1, observed_at=datetime.now(timezone.utc).isoformat())


def test_cached_proof_is_memory_evidence_and_observer_does_not_change_scores(monkeypatch):
    s, _, calls = native_screener(monkeypatch, '0')
    plain = run(s)
    traced = run(s, **FLAGS)
    a, b = asdict(plain[0]), asdict(traced[0]); a.pop('selection_basis'); b.pop('selection_basis')
    assert a == b and len(calls) == 4
    basis = traced[0].selection_basis
    proof = basis['supply_policy_decisions']['spdi']['foreign']
    assert proof['origin'] == 'policy_memory_proof' and proof['source_as_of'] is None
    assert 0 <= proof['proof_age_seconds'] <= 600
    assert basis['same_input_shadow']['status'] == 'inputs_incomplete'
    assert basis['supply_policy_shadow']['status'] == 'comparable'


@pytest.mark.parametrize('defect', ['value','age','state','origin','asof','policy','future','flag','after','native','delta','profit','fields','version'])
def test_policy_tampering_fails_closed(monkeypatch, defect):
    s, _, _ = native_screener(monkeypatch, '0')
    b = deepcopy(run(s, **FLAGS)[0].selection_basis)
    decision = b['supply_policy_decisions']['spdi']; row = decision['foreign']
    if defect == 'value': row['value'] = True
    elif defect == 'age': row['proof_age_seconds'] = 601
    elif defect == 'state': row['state'] = 'positive'
    elif defect == 'origin': row['origin'] = 'current_raw_row'
    elif defect == 'asof': row['source_as_of'] = b['produced_at']
    elif defect == 'policy': b['supply_policy_id'] = 'unverified'
    elif defect == 'future': decision['captured_at'] = '2099-01-01T00:00:00+00:00'
    elif defect == 'flag': decision['absence_observed'] = False
    elif defect == 'after': decision['after'] = 31
    elif defect == 'native': decision['native_evaluated'] = False
    elif defect == 'delta': b['supply_policy_shadow']['delta_vs_legacy'] = 20
    elif defect == 'profit': b['supply_policy_shadow']['economic_effect'] = 0
    elif defect == 'fields': row['raw_payload'] = 'forbidden'
    else: b['version'] = 'kr-selection-basis-v2'
    with pytest.raises(ValueError):
        validate_basis(b, score=30, rank=1, observed_at=datetime.now(timezone.utc).isoformat())


@pytest.mark.asyncio
async def test_v4_journal_roundtrip_scan_version_and_budget(tmp_path, monkeypatch):
    from src.analytics.entry_observation_journal import ObservationJournal, read_observation_journal
    s, _, _ = native_screener(monkeypatch, '0')
    buffer = EntryObservationBuffer(evaluation_epoch='synthetic', capacity=200, selection_basis_settings=SETTINGS)
    assert buffer.selection_supply_policy_capture_enabled
    assert buffer.selection_modifiers_capture_enabled and buffer.selection_sources_capture_enabled
    path = tmp_path/'v4.jsonl'
    journal = await ObservationJournal.open(buffer, path, study_ref='synthetic', study_sha256='a'*64,
        queue_capacity=200, batch_size=25, max_bytes=8000000, max_record_bytes=65536)
    result = await s.screen_all(**FLAGS)
    capture_scan(buffer, result, 'regular')
    assert (await journal.close())['sealed']
    loaded = read_observation_journal(path, max_bytes=8000000, expected_study_sha256='a'*64)
    assert loaded['complete']
    assert loaded['records'][0]['selection_basis_version'] == 'selection-basis-v4'
    report = build_selection_report(loaded)
    assert report['supply_policy_evidence']['comparable_spdi_shadows'] == 1
    assert report['production_eligible'] is False
    wrong = deepcopy(loaded)
    wrong['records'][0].pop('selection_basis_version')
    with pytest.raises(ValueError): build_selection_report(wrong)


@pytest.mark.asyncio
async def test_runtime_v4_settings_and_readiness(tmp_path):
    from types import SimpleNamespace
    from datetime import timedelta
    from test_llm_capture_readiness import fixed_llm
    from test_entry_observation_runtime import load
    from test_market_state_pilot_runtime import bot_with
    from src.analytics.entry_observation_runtime import ObservationRuntime
    def change(c):
        fixed_llm(c); c['capture']['selection_basis'] = SETTINGS
    plan, _ = load(tmp_path, change)
    bot = bot_with([])
    bot.engine = SimpleNamespace(risk_manager=SimpleNamespace(_entry_observation_owner=bot))
    runtime = await ObservationRuntime.install(bot, plan, now=lambda: plan.start_at-timedelta(seconds=1))
    try:
        assert runtime.llm_readiness()['selection_basis_version'] == 'selection-basis-v4'
        assert runtime.llm_readiness()['selection_max_modifier_stages'] == 24
    finally:
        await runtime.close('test_finished')


@pytest.mark.parametrize('aborted', [True, False])
def test_aborted_or_wrong_native_arithmetic_never_claims_comparison(monkeypatch, aborted):
    from src.analytics.selection_supply_policy import decision_snapshot
    from src.signals.screener.kr_screener import _observe_modifier
    s, _, _ = native_screener(monkeypatch, '0')
    async def wrong(stocks):
        for stock in stocks.values():
            _observe_modifier('policy_begin', 'spdi', stock, decision_snapshot(stock))
            if not aborted:
                stock.score -= 1  # deliberately differs from the declared production formula
                _observe_modifier('policy_end', 'spdi', stock)
    s._apply_spdi_filter = wrong
    result = run(s, **FLAGS)
    b = result[0].selection_basis
    assert b['supply_policy_shadow']['status'] == 'inputs_incomplete'
    assert b['supply_policy_shadow']['actual_after'] is None
    assert b['supply_policy_decisions']['spdi']['native_evaluated'] is (not aborted)
    validate_basis(b, score=result[0].score, rank=1, observed_at=datetime.now(timezone.utc).isoformat())


def test_conflicting_quantities_stay_unknown_in_actual_policy_snapshot(monkeypatch):
    from src.analytics.selection_supply_policy import supply_proof
    s, provider, _ = native_screener(monkeypatch, '0')
    fetch = provider.fetch_foreign_institution
    async def conflicting(**kw):
        rows = await fetch(**kw)
        return rows + [dict(rows[0], _supply_proof=supply_proof('1'))]
    provider.fetch_foreign_institution = conflicting
    result = run(s, **FLAGS)
    assert result[0].score == 50
    decision = result[0].selection_basis['supply_policy_decisions']['spdi']
    assert decision['foreign']['state'] == decision['institution']['state'] == 'unobserved'
    assert decision['absence_observed'] is decision['buying_observed'] is False


def test_cached_old_basis_keeps_old_version_time_and_no_current_proof(monkeypatch):
    from test_selection_source_basis import screener, stock
    sources = {'screen_volume_surge': [stock()]}
    s = screener(monkeypatch, sources)
    old = run(s, capture_selection=True, capture_selection_modifiers=True)[0].selection_basis
    sources.clear()
    result = run(s, **FLAGS)
    cached = result[0].selection_basis
    assert cached['version'] == old['version'] == 'kr-selection-basis-v2'
    assert cached['produced_at'] == old['produced_at'] and cached['cache_fallback'] is True
    assert 'supply_policy_decisions' not in cached
    buffer = EntryObservationBuffer(evaluation_epoch='synthetic', capacity=200, selection_basis_settings=SETTINGS)
    capture_scan(buffer, result, 'regular')
    assert build_selection_report(buffer.export())['supply_policy_evidence']['candidates'] == 0


def test_cached_new_basis_is_unavailable_under_old_v3_settings(monkeypatch):
    from test_selection_source_basis import screener, stock
    sources = {'screen_volume_surge': [stock()]}; s = screener(monkeypatch, sources)
    original = run(s, **FLAGS)[0].selection_basis
    sources.clear()
    result = run(s, capture_selection=True, capture_selection_modifiers=True)
    assert result[0].selection_basis['version'] == original['version']
    assert result[0].selection_basis['produced_at'] == original['produced_at']
    old_settings = dict(SETTINGS, version='selection-basis-v3')
    buffer = EntryObservationBuffer(evaluation_epoch='synthetic', capacity=200, selection_basis_settings=old_settings)
    capture_scan(buffer, result, 'regular')
    assert buffer.export()['records'][1]['basis_status'] == 'unavailable'


def test_fresh_policy_decision_cannot_precede_capture_start(monkeypatch):
    s, _, _ = native_screener(monkeypatch, '0')
    b = deepcopy(run(s, **FLAGS)[0].selection_basis)
    b['supply_policy_decisions']['spdi']['captured_at'] = '2000-01-01T00:00:00+00:00'
    with pytest.raises(ValueError):
        validate_basis(b, score=30, rank=1, observed_at=datetime.now(timezone.utc).isoformat())


@pytest.mark.asyncio
async def test_v4_hundred_candidates_fit_existing_record_budget(tmp_path, monkeypatch):
    from test_selection_source_basis import screener, stock
    from src.signals.screener.kr_screener import StockScreener
    from src.analytics.entry_observation_journal import ObservationJournal, read_observation_journal
    s = screener(monkeypatch, {'screen_volume_surge': [stock(f'{i+1:06d}', change_pct=5) for i in range(100)]})
    s._apply_spdi_filter = StockScreener._apply_spdi_filter.__get__(s)
    buffer = EntryObservationBuffer(evaluation_epoch='synthetic', capacity=250, selection_basis_settings=SETTINGS)
    path = tmp_path/'bounded.jsonl'
    journal = await ObservationJournal.open(buffer, path, study_ref='synthetic', study_sha256='b'*64,
        queue_capacity=250, batch_size=25, max_bytes=8000000, max_record_bytes=65536)
    capture_scan(buffer, await s.screen_all(**FLAGS), 'regular')
    closed = await journal.close()
    assert closed['sealed'] and closed['fsync_confirmed']
    loaded = read_observation_journal(path, max_bytes=8000000, expected_study_sha256='b'*64)
    assert loaded['complete'] and len(loaded['records']) == 101
    assert build_selection_report(loaded)['supply_policy_evidence']['comparable_spdi_shadows'] == 100


@pytest.mark.asyncio
async def test_v4_scheduler_forwarding_and_window_replay_binding(tmp_path, monkeypatch):
    import json
    from pathlib import Path
    import test_llm_capture_reachability as reach
    from src.analytics import llm_decision_record as decisions
    from src.analytics.entry_observation_runtime import validate_window_contract
    from src.analytics.entry_observation_journal import read_observation_journal
    monkeypatch.setattr(Path, 'home', classmethod(lambda cls: tmp_path))
    recorder = decisions.DecisionRecorder(tmp_path/'decisions', enabled=True)
    monkeypatch.setattr(decisions, '_recorder', recorder)
    original_study = reach.study
    def study(context, **kw):
        original_study(context, **kw)
        context['capture']['selection_basis'].update(version='selection-basis-v4', max_modifier_stages=24)
    monkeypatch.setattr(reach, 'study', study)
    case = None
    try:
        case = await reach.connected_case(tmp_path, monkeypatch, selection_v3=True)
        assert case.capture_flags == [dict(FLAGS, capture_selection_sources=True)]
        assert case.signal.score == 85
        case.clock[0] = case.plan.end_at
        assert (await case.journal.close())['sealed']
        loaded = read_observation_journal(case.plan.settings['journal_path'],
            max_bytes=case.plan.settings['max_bytes'], expected_study_sha256=case.plan.study_sha256)
        report = build_selection_report(loaded)
        assert report['supply_policy_evidence']['comparable_spdi_shadows'] == 1
        context = json.loads(case.plan.study_path.read_text())
        validate_window_contract(context, loaded)
        wrong = deepcopy(loaded)
        next(r for r in wrong['records'] if r['kind'] == 'scan').pop('selection_basis_version')
        with pytest.raises(ValueError): validate_window_contract(context, wrong)
    finally:
        if case is not None:
            await case.journal.close()
            await case.subscriptions.stop()
        assert recorder.close()


def test_policy_shadow_boolean_is_not_a_numeric_delta(monkeypatch):
    s, _, _ = native_screener(monkeypatch, '0')
    b = deepcopy(run(s, **FLAGS)[0].selection_basis)
    b['supply_policy_shadow']['delta_vs_legacy'] = False
    with pytest.raises(ValueError):
        validate_basis(b, score=30, rank=1, observed_at=datetime.now(timezone.utc).isoformat())


def test_policy_report_counts_decision_states_separately_from_old_raw_evidence(monkeypatch):
    s, _, _ = native_screener(monkeypatch, '0')
    run(s)  # subsequent raw observer has no fresh rows
    result = run(s, **FLAGS)
    buffer = EntryObservationBuffer(evaluation_epoch='synthetic', capacity=200, selection_basis_settings=SETTINGS)
    capture_scan(buffer, result, 'regular')
    report = build_selection_report(buffer.export())
    assert report['modifier_evidence']['supply_states']['unobserved'] == 2
    counts = report['supply_policy_evidence']['by_stage']
    assert counts['spdi']['fresh']['states'] == dict(positive=0, observed_nonpositive=2, unobserved=0)
    assert counts['spdi']['fresh']['native_evaluated'] == 1
    assert counts['overnight_regime']['fresh']['not_evaluated'] == 1
    assert counts['overnight_regime']['fresh']['states'] == dict(positive=0, observed_nonpositive=0, unobserved=0)


def test_current_source_scan_boundary_rejects_prior_policy_decision(monkeypatch):
    s, _, _ = native_screener(monkeypatch, '0')
    result = run(s, capture_selection_sources=True, **FLAGS)
    buffer = EntryObservationBuffer(evaluation_epoch='synthetic', capacity=200, selection_basis_settings=SETTINGS)
    capture_scan(buffer, result, 'regular')
    payload = buffer.export()
    build_selection_report(payload)
    basis = payload['records'][1]
    basis['supply_policy_decisions']['spdi']['captured_at'] = basis['supply_policy_started_at']
    with pytest.raises(ValueError, match='current scan source boundary'):
        build_selection_report(payload)


def test_report_cached_candidate_decisions_are_not_current_evidence(monkeypatch):
    from test_selection_source_basis import screener, stock
    from src.signals.screener.kr_screener import StockScreener
    sources = {'screen_volume_surge': [stock(change_pct=5)]}; s = screener(monkeypatch, sources)
    s._apply_spdi_filter = StockScreener._apply_spdi_filter.__get__(s)
    run(s, **FLAGS)
    sources.clear()
    buffer = EntryObservationBuffer(evaluation_epoch='synthetic', capacity=200, selection_basis_settings=SETTINGS)
    capture_scan(buffer, run(s, **FLAGS), 'regular')
    evidence = build_selection_report(buffer.export())['supply_policy_evidence']
    assert evidence['cached_bases'] == 1
    assert evidence['by_stage']['spdi']['cached']['native_evaluated'] == 1
    assert evidence['by_stage']['spdi']['fresh']['native_evaluated'] == 0


@pytest.mark.parametrize('method', ['policy_begin', 'policy_end', 'finish'])
def test_new_observer_failure_does_not_change_policy_scoring(monkeypatch, method):
    from src.analytics.selection_modifiers import PolicyModifierCapture
    s, _, calls = native_screener(monkeypatch, '0')
    def fail(*args): raise RuntimeError('synthetic observer failure')
    monkeypatch.setattr(PolicyModifierCapture, method, fail)
    result = run(s, **FLAGS)
    assert result[0].score == 30 and len(calls) == 4
    assert result[0].selection_basis['version'] == 'kr-selection-basis-v1'
