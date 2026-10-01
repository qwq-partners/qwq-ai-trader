"""원천 관측의 명시 활성화·빈 모집단·파일 왕복·가격 계산 무영향."""
from copy import deepcopy
import asyncio
from types import SimpleNamespace

import pytest

from src.analytics.entry_observation import EntryObservationBuffer, capture_scan, prepare_input
from src.analytics.selection_basis import build_selection_report
from src.analytics.selection_source_status import SourceCapture
from test_selection_source_basis import screener, stock, run

SETTINGS = {'version': 'selection-basis-v2', 'max_candidates': 100, 'max_source_terms': 16}


def buffer():
    return EntryObservationBuffer(evaluation_epoch='synthetic', capacity=250, scan_scope='first',
        scan_admission_ref='synthetic-first', selection_basis_settings=SETTINGS)


def observed(monkeypatch, n=2):
    c = SourceCapture()
    source = [stock(f'{i+1:06d}') for i in range(n)]
    asyncio.run(c.call('kis_volume_surge', lambda: source))
    stocks = run(screener(monkeypatch, {'screen_volume_surge': source}),
                 capture_selection=True)
    return c.wrap(stocks)


def test_sources_require_v2_and_stop_with_first_scan():
    b = buffer(); assert b.selection_sources_capture_enabled is True
    capture_scan(b, SourceCapture().wrap([]), 'regular')
    assert b.selection_sources_capture_enabled is False
    report = build_selection_report(b.export())
    assert report['counts']['candidates'] == 0
    assert report['source_scans'][0]['status'] == 'observed'
    legacy = EntryObservationBuffer(evaluation_epoch='synthetic', capacity=10,
        selection_basis_settings={**SETTINGS, 'version':'selection-basis-v1'})
    assert legacy.selection_sources_capture_enabled is False
    capture_scan(legacy, SourceCapture().wrap([]), 'regular')
    assert 'selection_sources_expected' not in legacy.export()['records'][0]


def test_missing_trace_does_not_remove_candidates_or_claim_success(monkeypatch):
    b = buffer(); stocks = [stock()]; assert capture_scan(b, stocks, 'regular')
    result = build_selection_report(b.export())
    assert result['counts']['candidates'] == 1
    assert result['source_scans'][0]['status'] == 'unavailable'


@pytest.mark.parametrize('defect', ['future', 'duplicate', 'disabled', 'unavailable_values', 'wrong_outcome',
                                   'mixed_fallback', 'old_fresh_basis', 'term_rank', 'before_source_end', 'missing_contributor'])
def test_source_snapshot_rejects_time_population_and_fallback_conflicts(monkeypatch, defect):
    b = buffer(); capture_scan(b, observed(monkeypatch), 'regular'); o = b.export(); scan = o['records'][0]
    if defect == 'future': scan['selection_sources_completed_at'] = '2099-01-01T00:00:00+00:00'
    elif defect == 'duplicate': scan['selection_source_runs'][1] = deepcopy(scan['selection_source_runs'][0])
    elif defect == 'disabled': scan['selection_basis_expected'] = False
    elif defect == 'unavailable_values': scan['selection_sources_status'] = 'unavailable'
    elif defect == 'wrong_outcome': scan['selection_source_runs'][0]['outcome'] = 'completed_empty'
    elif defect == 'mixed_fallback': scan['selection_sources_fallback_used'] = True
    elif defect == 'term_rank': o['records'][1]['source_terms'][0]['input_rank'] = 99
    elif defect == 'before_source_end':
        for r in o['records'][1:]: r['produced_at'] = scan['selection_sources_started_at']
    elif defect == 'missing_contributor':
        for r in o['records'][1:]: r['source_terms'][0]['source_id'] = 'kis_new_highs'
    else:
        for r in o['records'][1:]: r['produced_at'] = '2000-01-01T00:00:00+00:00'
    with pytest.raises(ValueError): build_selection_report(o)


def test_same_input_family_is_diagnostic_and_does_not_change_score(monkeypatch):
    c = SourceCapture(); rise = stock(score=90, change_pct=10, has_inst_buying=True)
    asyncio.run(c.call('naver_rise_rank', lambda:[rise]))
    asyncio.run(c.call('naver_new_high', lambda:[stock()]))
    stocks = run(screener(monkeypatch, {'naver_rise_rank':[rise]}), use_naver=True, capture_selection=True)
    b = buffer(); capture_scan(b, c.wrap(stocks), 'regular')
    row = build_selection_report(b.export())['candidates'][0]
    assert row['basis']['final_score'] == 77
    assert row['lineage']['occurrences'] == 2 and row['lineage']['known_input_families'] == 1
    assert row['lineage']['overlapping_families'] == {'naver_rise':['naver_rise_rank','naver_new_high']}
    assert row['lineage']['independence_verified'] is False


@pytest.mark.asyncio
async def test_source_snapshot_round_trip_preserves_100_candidate_denominator(tmp_path, monkeypatch):
    from src.analytics.entry_observation_journal import ObservationJournal, read_observation_journal
    b = buffer(); path = tmp_path / 'synthetic.jsonl'
    journal = await ObservationJournal.open(b, path, study_ref='synthetic', study_sha256='a'*64,
        queue_capacity=250, batch_size=25, max_bytes=8000000, max_record_bytes=65536)
    c = SourceCapture()
    source = [stock(f'{i+1:06d}') for i in range(100)]
    await c.call('kis_volume_surge', lambda:source)
    stocks = await screener(monkeypatch, {'screen_volume_surge':source}).screen_all(capture_selection=True)
    capture_scan(b, c.wrap(stocks), 'regular')
    closed = await journal.close()
    assert closed['sealed'] and closed['fsync_confirmed']
    loaded = read_observation_journal(path, max_bytes=8000000, expected_study_sha256='a'*64)
    result = build_selection_report(loaded)
    assert result['capture_complete'] and result['counts']['accounted'] == 100
    assert result['source_scans'][0]['status'] == 'observed'


@pytest.mark.parametrize('small', [False, True])
def test_runtime_requires_larger_explicit_record_budget_for_v2(tmp_path, small):
    from test_entry_observation_runtime import fixture_plan, NOW, module
    def enable(c):
        c['capture'].update(version='runner-first-scan-v2', selection_basis=SETTINGS,
                            max_record_bytes=32768 if small else 65536, max_bytes=8000000)
    path, _ = fixture_plan(tmp_path, enable)
    if small:
        with pytest.raises(ValueError): module().CapturePlan.load(path, now=NOW)
    else:
        plan = module().CapturePlan.load(path, now=NOW)
        assert module()._WindowBuffer(plan, lambda:NOW).selection_sources_capture_enabled is True


def test_price_report_unchanged_and_orphan_source_fields_rejected():
    from test_entry_evaluation_bundle import fixture
    from src.analytics.selection_source_status import snapshot_fields
    c, o, _ = fixture(); before = prepare_input(c, o, [])['report']
    scan = next(r for r in o['records'] if r['kind']=='scan')
    scan.update(selection_basis_expected=True, selection_basis_max_candidates=100, selection_basis_max_terms=16,
                selection_sources_expected=True, selection_sources_status='unavailable')
    assert prepare_input(c, o, [])['report'] == before
    scan.pop('selection_basis_expected')
    with pytest.raises(ValueError): prepare_input(c, o, [])
