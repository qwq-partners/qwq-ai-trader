"""선정 계산 근거의 선택적 저장·원장 연결. 계좌/실시간 API를 사용하지 않는다."""
from copy import deepcopy
from datetime import datetime, timezone
import json
from types import SimpleNamespace

import pytest

from src.analytics.entry_observation import EntryObservationBuffer, capture_scan, prepare_input
from src.analytics.entry_observation_journal import ObservationJournal, read_observation_journal
from test_selection_source_basis import screener, stock, run


SETTINGS = {'version': 'selection-basis-v1', 'max_candidates': 100, 'max_source_terms': 16}


def buffer(capacity=250):
    return EntryObservationBuffer(evaluation_epoch='synthetic', capacity=capacity, scan_scope='first',
                                 scan_admission_ref='synthetic-first', selection_basis_settings=SETTINGS)


def traced(monkeypatch, n=2):
    return run(screener(monkeypatch, {'screen_volume_surge': [stock(f'{i+1:06d}') for i in range(n)]}),
               capture_selection=True)


def report(observations):
    from src.analytics.selection_basis import build_selection_report
    return build_selection_report(observations)


def test_enabled_scan_preserves_full_candidates_and_copies_separate_basis_rows(monkeypatch):
    stocks = traced(monkeypatch); b = buffer(); sid = capture_scan(b, stocks, 'regular')
    rows = b.export()['records']
    assert len(rows) == 3 and rows[0]['selection_basis_expected'] is True
    assert rows[1]['candidate_id'] == sid + ':000001' and rows[2]['candidate_id'] == sid + ':000002'
    assert rows[1]['source_terms'][0]['weighted_score'] == 50
    stocks[0].selection_basis['source_terms'][0]['weighted_score'] = -99
    assert rows[1]['source_terms'][0]['weighted_score'] == 50
    out = report(b.export())
    assert out['counts'] == {'candidates': 2, 'expected': 2, 'recorded': 2, 'accounted': 2,
                             'cache_fallback': 0, 'unavailable': 0, 'missing_records': 0, 'disabled': 0}
    assert out['profit_contribution_verified'] is False
    assert out['production_eligible'] is False


def test_disabled_empty_and_missing_basis_are_distinct():
    plain = EntryObservationBuffer(evaluation_epoch='synthetic', capacity=10)
    capture_scan(plain, [SimpleNamespace(symbol='000001')], 'regular')
    assert report(plain.export())['counts']['disabled'] == 1
    empty = buffer(); capture_scan(empty, [], 'regular')
    assert empty.export()['records'][0]['selection_basis_expected'] is True
    assert report(empty.export())['counts']['candidates'] == 0
    missing = buffer(); capture_scan(missing, [SimpleNamespace(symbol='000001')], 'regular')
    assert missing.export()['records'][1]['basis_status'] == 'unavailable'
    assert report(missing.export())['counts']['unavailable'] == 1


def test_basis_capacity_loss_is_visible_without_dropping_scan_candidates(monkeypatch):
    b = buffer(capacity=2); stocks = traced(monkeypatch)
    assert capture_scan(b, stocks, 'regular') is not None
    out = b.export()
    assert out['complete'] is False and len(out['records'][0]['candidates']) == 2
    assert report(out)['counts']['missing_records'] == 1
    assert [s.score for s in stocks] == [50, 50]


def test_first_scan_rejects_foreign_basis(monkeypatch):
    b = buffer(); capture_scan(b, traced(monkeypatch), 'regular')
    record = deepcopy(b.export()['records'][1]); record['candidate_id'] = 'foreign:000001'
    assert b.publish(record) is False


@pytest.mark.parametrize('defect', ['orphan', 'duplicate', 'symbol', 'rank', 'score', 'source',
                                  'extra_term', 'nonfinite', 'bad_sum', 'bad_count', 'time', 'status_claim'])
def test_bad_basis_is_rejected_in_offline_report(monkeypatch, defect):
    b = buffer(); capture_scan(b, traced(monkeypatch), 'regular'); o = b.export(); r = o['records'][1]
    if defect == 'orphan': r['candidate_id'] = 'missing'
    elif defect == 'duplicate': o['records'].append(deepcopy(r))
    elif defect == 'symbol': r['symbol'] = '000002'
    elif defect == 'rank': r['returned_rank'] = 2
    elif defect == 'score': r['final_score'] = 99
    elif defect == 'source': r['source_terms'][0]['source_id'] = 'arbitrary-provider'
    elif defect == 'extra_term': r['source_terms'][0]['raw_response'] = 'not allowed'
    elif defect == 'nonfinite': r['source_terms'][0]['score_at_merge'] = float('nan')
    elif defect == 'bad_sum': r['merge_score_sum'] = 51
    elif defect == 'bad_count': r['occurrence_count'] = 3
    elif defect == 'time': r['produced_at'] = '2099-01-01T00:00:00+00:00'
    else: r['source_status_known'] = True
    for seq, row in enumerate(o['records'], 1): row['sequence'] = seq
    with pytest.raises(ValueError): report(o)


@pytest.mark.asyncio
async def test_journal_round_trip_supports_100_bases_under_fixed_per_record_limit(tmp_path, monkeypatch):
    b = buffer(); path = tmp_path / 'synthetic.jsonl'
    journal = await ObservationJournal.open(b, path, study_ref='synthetic', study_sha256='a'*64,
        queue_capacity=250, batch_size=25, max_bytes=4000000, max_record_bytes=32768)
    stocks = await screener(monkeypatch, {'screen_volume_surge': [stock(f'{i+1:06d}') for i in range(100)]}).screen_all(capture_selection=True)
    capture_scan(b, stocks, 'regular')
    closed = await journal.close()
    assert closed['sealed'] and closed['fsync_confirmed']
    loaded = read_observation_journal(path, max_bytes=4000000, expected_study_sha256='a'*64)
    assert loaded['complete'] is True and len(loaded['records']) == 101
    assert report(loaded)['counts']['accounted'] == 100


def test_received_price_parser_ignores_valid_selection_basis_but_rejects_orphans(monkeypatch):
    from test_entry_evaluation_bundle import fixture
    c, o, _ = fixture()
    scan = next(r for r in o['records'] if r['kind'] == 'scan')
    scan.update(selection_basis_expected=True, selection_basis_max_candidates=100, selection_basis_max_terms=16)
    before = prepare_input(c, o, [])['report']
    basis_rows = [{'kind': 'selection_basis', 'candidate_id': x['candidate_id'], 'symbol': x['symbol'],
                   'observed_at': scan['observed_at'], 'basis_status': 'unavailable'} for x in scan['candidates']]
    o['records'][o['records'].index(scan)+1:o['records'].index(scan)+1] = basis_rows
    for seq, r in enumerate(o['records'], 1): r['sequence'] = seq
    assert prepare_input(c, o, [])['report'] == before
    basis_rows[0]['candidate_id'] = 'foreign'
    with pytest.raises(ValueError): prepare_input(c, o, [])


@pytest.mark.parametrize('bad', [None, {}, {'version':'selection-basis-v1','max_candidates':0,'max_source_terms':16},
                               {'version':'selection-basis-v1','max_candidates':101,'max_source_terms':16},
                               {'version':'selection-basis-v1','max_candidates':100,'max_source_terms':True}])
def test_explicit_selection_limits_reject_bad_contract(bad):
    if bad is None:
        b = EntryObservationBuffer(evaluation_epoch='synthetic', capacity=10)
        assert b.selection_capture_enabled is False
    else:
        with pytest.raises(ValueError):
            EntryObservationBuffer(evaluation_epoch='synthetic', capacity=10, selection_basis_settings=bad)


def test_capture_plan_v2_is_explicit_and_v1_stays_disabled(tmp_path):
    from test_entry_observation_runtime import fixture_plan, NOW, module
    def enable(c):
        c['capture'].update(version='runner-first-scan-v2', selection_basis=SETTINGS,
                            max_record_bytes=32768, max_bytes=4000000)
    path, _ = fixture_plan(tmp_path, enable)
    plan = module().CapturePlan.load(path, now=NOW)
    b = module()._WindowBuffer(plan, lambda: NOW)
    assert b.selection_capture_enabled is True and b.selection_basis_settings == SETTINGS


@pytest.mark.parametrize('defect', ['v1_extra', 'v2_missing', 'candidate_limit', 'term_limit',
                                  'buffer', 'queue', 'record_bytes', 'total_bytes'])
def test_capture_plan_v2_rejects_inadequate_basis_contract(tmp_path, defect):
    from test_entry_observation_runtime import fixture_plan, NOW, module
    def change(c):
        s = c['capture']; s.update(version='runner-first-scan-v2', selection_basis=deepcopy(SETTINGS),
                                 max_record_bytes=32768, max_bytes=4000000)
        if defect == 'v1_extra': s['version'] = 'runner-first-scan-v1'
        elif defect == 'v2_missing': s.pop('selection_basis')
        elif defect == 'candidate_limit': s['selection_basis']['max_candidates'] = 101
        elif defect == 'term_limit': s['selection_basis']['max_source_terms'] = 17
        elif defect == 'buffer': s['buffer_capacity'] = 100
        elif defect == 'queue': s['queue_capacity'] = 100
        elif defect == 'record_bytes': s['max_record_bytes'] = 4096
        else: s['max_bytes'] = 5000
    path, _ = fixture_plan(tmp_path, change)
    with pytest.raises(ValueError): module().CapturePlan.load(path, now=NOW)


@pytest.mark.parametrize('defect', ['over_limit', 'disabled_limits', 'partial_profile', 'after_scan'])
def test_reviewed_contract_and_profile_boundaries(monkeypatch, defect):
    b = buffer(); capture_scan(b, traced(monkeypatch, 3), 'regular'); o = b.export(); scan = o['records'][0]
    if defect == 'over_limit': scan['selection_basis_max_candidates'] = 2
    elif defect == 'disabled_limits':
        scan['selection_basis_expected'] = False; o['records'] = [scan]
    elif defect == 'partial_profile':
        row = o['records'][3]
        o['records'][3] = {k:row[k] for k in ('kind','candidate_id','symbol','observed_at','sequence')}
        o['records'][3]['basis_status'] = 'unavailable'
        o['records'][2]['basis_id'] = 'b' * 32
    else:
        for row in o['records'][1:]:
            row['produced_at'] = o['records'][1]['observed_at']
        assert o['records'][1]['produced_at'] > scan['observed_at']
    with pytest.raises(ValueError): report(o)


def test_declared_population_overflow_keeps_incomplete_full_denominator(monkeypatch):
    settings = {**SETTINGS, 'max_candidates': 1}
    b = EntryObservationBuffer(evaluation_epoch='synthetic', capacity=10, selection_basis_settings=settings)
    capture_scan(b, traced(monkeypatch, 2), 'regular'); out = report(b.export())
    assert out['counts']['candidates'] == out['counts']['missing_records'] == 2
    assert out['capture_complete'] is False and out['population_limits_respected'] is False


def test_selection_computation_stops_after_first_scan_or_close(monkeypatch):
    b = buffer(); assert b.selection_capture_enabled is True
    capture_scan(b, traced(monkeypatch), 'regular')
    assert b.selection_capture_enabled is False
    b = buffer(); b._capture_closed = True
    assert b.selection_capture_enabled is False


def test_anchor_projection_keeps_same_candidates_despite_basis_rows(monkeypatch):
    from src.observation.entry_anchor_input import project_anchors
    b = buffer(); capture_scan(b, traced(monkeypatch, 4), 'regular')
    runtime = SimpleNamespace(buffer=b, result=None, plan=SimpleNamespace(study_sha256='a'*64),
                              journal=SimpleNamespace(_header={'capture_id':'synthetic'}))
    enriched = project_anchors(runtime)
    b._records = [r for r in b._records if r['kind'] != 'selection_basis']
    original = project_anchors(runtime)
    assert enriched['selection'] == original['selection']
    assert enriched['records'] == original['records']
    assert enriched['source_record_count'] == 5 and original['source_record_count'] == 1
