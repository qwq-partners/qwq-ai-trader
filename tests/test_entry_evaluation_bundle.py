"""Offline quote-review binding; every market record here is synthetic."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / 'scripts/prepare_entry_evaluation.py'


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def fixture():
    saved = json.loads((ROOT / 'docs/research/current-engine-evaluation-rehearsal-2026-10-01.json').read_text())
    x = saved['synthetic_input']
    return x['context'], x['observations'], saved['synthetic_study_sha256']


def review(c, o, sha):
    return {'version': 'entry-session-review-v1', 'dataset_kind': c['dataset_kind'],
            'binding': {'study_sha256': sha, 'observation_sha256': digest(o),
                        'capture_id': o['journal']['capture_id'], 'evaluation_epoch': c['evaluation_epoch']},
            'quotes': [{'quote_id': q['quote_id'], 'symbol': q['symbol'], 'record_sha256': digest(q),
                        'session': 'KRX_REGULAR_CONTINUOUS', 'basis': 'external_event_history_review',
                        'source_ref': 'synthetic-state-events', 'source_sha256': 'a' * 64,
                        'reviewer_ref': 'synthetic-reviewer', 'reviewed_at': '2026-10-02T10:00:00+09:00',
                        'valid_from': '2026-10-02T09:00:00+09:00',
                        'valid_until': '2026-10-02T09:45:00+09:00'}
                       for q in o['records'] if q['kind'] == 'ws_quote']}


def build(c, o, sha, r=None):
    from src.analytics.entry_evaluation_bundle import build_evaluation_bundle
    raw = (json.dumps(c, ensure_ascii=False, sort_keys=True, separators=(',', ':')) + '\n').encode()
    return build_evaluation_bundle(raw, o, study_sha256=sha, session_review=r)


def test_missing_review_does_not_infer_regular_session_from_hour_zero():
    c, o, sha = fixture(); before = deepcopy((c, o))
    out = build(c, o, sha)
    assert out['original']['report']['counts']['unknown'] == 4
    assert out['readiness']['status'] == 'no_comparable_pairs'
    assert out['original']['missing_evaluation_inputs'] == 4
    assert out['readiness']['missing_required_session_evidence_candidates'] == 4
    assert len(out['review_tasks']) == 4
    assert out['review_tasks'][0]['entry_quote']['quote_id'] == 'synthetic-entry-SYNTH_LOSS'
    assert out['review_tasks'][0]['entry_quote']['review_status'] == 'UNREVIEWED'
    assert out['source_authenticity_verified'] is False
    assert out['production_eligible'] is False and out['account_return'] is None
    assert (c, o) == before


def test_reviewed_quote_binding_assembles_original_and_all_cost_stresses():
    c, o, sha = fixture(); r = review(c, o, sha); before = deepcopy((c, o, r))
    out = build(c, o, sha, r)
    assert out['readiness']['status'] == 'partial_price_diagnostic'
    assert out['original']['report']['counts']['paired_outcomes'] == 3
    assert out['original']['report']['counts']['total'] == 4
    assert [v['slippage_bps_each'] for v in out['sensitivity']] == [0, 10, 30]
    assert [v['result']['report']['summary']['known_pair_delta_net_pnl'] for v in out['sensitivity']] == ['-11333', '-10321', '-8298']
    assert all(v['result']['report']['summary']['complete_delta_net_pnl'] is None for v in out['sensitivity'])
    assert out['binding'] == r['binding']
    assert len(out['evaluation_inputs']) == 3
    assert out['readiness']['missing_required_session_evidence_candidates'] == 1
    assert (c, o, r) == before


@pytest.mark.parametrize('field', ['study_sha256', 'observation_sha256', 'capture_id', 'evaluation_epoch'])
def test_another_capture_or_study_review_is_rejected(field):
    c, o, sha = fixture(); r = review(c, o, sha); r['binding'][field] = 'wrong'
    with pytest.raises(ValueError, match='binding'): build(c, o, sha, r)


@pytest.mark.parametrize('defect', ['duplicate', 'foreign_quote', 'symbol', 'hash', 'extra', 'missing',
                                  'kind', 'basis', 'bad_sha', 'bad_session'])
def test_review_cannot_overwrite_or_invent_quote_evidence(defect):
    c, o, sha = fixture(); r = review(c, o, sha); q = r['quotes'][0]
    if defect == 'duplicate': r['quotes'].append(deepcopy(q))
    elif defect == 'foreign_quote': q['quote_id'] = 'foreign'
    elif defect == 'symbol': q['symbol'] = 'different'
    elif defect == 'hash': q['record_sha256'] = 'b' * 64
    elif defect == 'extra': q['ask'] = '1'
    elif defect == 'missing': q.pop('source_ref')
    elif defect == 'kind': r['dataset_kind'] = 'observed'
    elif defect == 'basis': q['basis'] = 'hour_code_zero'
    elif defect == 'bad_sha': q['source_sha256'] = 'not-a-hash'
    else: q['session'] = 'NORMAL'
    with pytest.raises(ValueError): build(c, o, sha, r)


@pytest.mark.parametrize('field,value', [
    ('valid_from', '2026-10-02T09:15:06+09:00'),
    ('valid_until', '2026-10-02T09:15:05+09:00'),
    ('valid_from', '2026-10-01T09:00:00+09:00'),
    ('reviewed_at', '2026-10-02T09:15:04+09:00'),
    ('reviewed_at', '2026-10-02T09:15:05+09:00'),
    ('reviewed_at', '2026-10-02T10:00:00'),
])
def test_review_must_cover_quote_and_have_aware_nonretroactive_review_time(field, value):
    c, o, sha = fixture(); r = review(c, o, sha); r['quotes'][0][field] = value
    with pytest.raises(ValueError): build(c, o, sha, r)


def test_vi_evidence_preserves_unknown_and_missing_markout_does_not_create_outcome():
    c, o, sha = fixture(); r = review(c, o, sha)
    r['quotes'][0]['session'] = 'VI'
    out = build(c, o, sha, r)
    assert out['original']['report']['opportunities'][0]['gate_status'] == 'unknown'
    r = review(c, o, sha)
    r['quotes'] = [q for q in r['quotes'] if 'mark-' not in q['quote_id']]
    out = build(c, o, sha, r)
    assert out['original']['report']['counts']['paired_outcomes'] == 0
    assert out['original']['report']['counts']['allow'] == 1


def test_later_reviewed_good_quote_never_replaces_bad_first_quote():
    c, o, sha = fixture()
    first = next(q for q in o['records'] if q.get('quote_id') == 'synthetic-entry-SYNTH_LOSS')
    first['ask_size'] = 1
    later = deepcopy(first); later.update(quote_id='synthetic-later-good', ask_size=25)
    o['records'].insert(o['records'].index(first) + 1, later)
    for seq, record in enumerate(o['records'], 1): record['sequence'] = seq
    r = review(c, o, sha)
    r['quotes'] = [q for q in r['quotes'] if q['quote_id'] != first['quote_id']]
    out = build(c, o, sha, r)
    assert out['review_tasks'][0]['entry_quote']['quote_id'] == first['quote_id']
    assert out['original']['report']['opportunities'][0]['gate_status'] == 'unknown'


def test_unsealed_or_missing_records_cannot_become_complete_with_review():
    c, o, sha = fixture(); o.update(complete=False, dropped_records=1)
    out = build(c, o, sha, review(c, o, sha))
    assert out['original']['report']['summary']['complete_delta_net_pnl'] is None
    assert out['readiness']['status'] == 'no_comparable_pairs'


def test_study_argument_must_match_loaded_original_journal():
    c, o, sha = fixture()
    with pytest.raises(ValueError): build(c, o, 'b' * 64)


def test_changed_policy_cannot_reuse_original_study_identity_and_review():
    c, o, sha = fixture(); r = review(c, o, sha)
    c['policy']['entry_slippage_bps'] = '77'
    with pytest.raises(ValueError, match='study'): build(c, o, sha, r)


@pytest.fixture
def journal_files(tmp_path):
    import asyncio
    from unittest.mock import patch
    from src.analytics.entry_observation import EntryObservationBuffer
    from src.analytics.entry_observation_journal import ObservationJournal
    c, o, _ = fixture()
    study = tmp_path / 'study.json'; journal = tmp_path / 'journal.jsonl'
    study.write_text(json.dumps(c, ensure_ascii=False, sort_keys=True, separators=(',', ':')) + '\n')
    sha = hashlib.sha256(study.read_bytes()).hexdigest()
    async def write():
        b = EntryObservationBuffer(evaluation_epoch=c['evaluation_epoch'], capacity=100)
        with patch('src.analytics.entry_observation_journal._timestamp', return_value='2026-10-02T09:14:59+09:00'):
            j = await ObservationJournal.open(b, journal, study_ref='synthetic-only', study_sha256=sha,
                    queue_capacity=100, batch_size=10, max_bytes=1048576, max_record_bytes=65536)
        for record in o['records']:
            row = deepcopy(record); row.pop('sequence'); assert b.publish(row)
        with patch('src.analytics.entry_observation_journal._timestamp', return_value='2026-10-02T09:45:00+09:00'):
            closed = await j.close()
        assert closed['sealed'] and closed['fsync_confirmed']
    asyncio.run(write())
    return study, journal


def run_cli(study, journal, *extra):
    return subprocess.run([sys.executable, str(CLI), '--study', str(study), '--journal', str(journal),
                           '--max-journal-bytes', '1048576', *map(str, extra)], capture_output=True, text=True,
                          timeout=5)


def test_cli_loads_original_hash_then_emits_review_tasks_without_mutating_inputs(journal_files):
    study, journal = journal_files; before = (study.read_bytes(), journal.read_bytes())
    r = run_cli(study, journal)
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert out['binding']['study_sha256'] == hashlib.sha256(before[0]).hexdigest()
    assert out['readiness']['status'] == 'no_comparable_pairs'
    assert (study.read_bytes(), journal.read_bytes()) == before


def test_cli_reviewed_bundle_reproduces_existing_calculator(journal_files, tmp_path):
    from src.analytics.entry_observation_journal import read_observation_journal
    study, journal = journal_files; before = (study.read_bytes(), journal.read_bytes())
    sha = hashlib.sha256(before[0]).hexdigest()
    c = json.loads(before[0]); o = read_observation_journal(journal, max_bytes=1048576,
                                                        expected_study_sha256=sha)
    evidence = tmp_path / 'review.json'; evidence.write_text(json.dumps(review(c, o, sha)))
    r = run_cli(study, journal, '--session-review', evidence)
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert out['original']['report']['summary']['known_pair_delta_net_pnl'] == '-11333'
    assert out['readiness']['minimum_paired_outcomes_across_scenarios'] == 3
    assert out['readiness']['profitability_pass'] is False
    inputs = tmp_path / 'inputs.json'; inputs.write_text(json.dumps(out['evaluation_inputs']))
    old = subprocess.run([sys.executable, str(ROOT / 'scripts/compare_entry_price_shadow.py'),
                          '--study', str(study), '--journal', str(journal), '--evaluation-inputs', str(inputs),
                          '--max-journal-bytes', '1048576'], capture_output=True, text=True, timeout=5)
    assert old.returncode == 0, old.stderr
    assert json.loads(old.stdout)['report'] == out['original']['report']
    assert (study.read_bytes(), journal.read_bytes()) == before


@pytest.mark.parametrize('defect', ['changed_study', 'symlink', 'fifo', 'oversized', 'duplicate_key', 'nan'])
def test_cli_rejects_bad_original_or_review_files(journal_files, tmp_path, defect):
    study, journal = journal_files; review_path = tmp_path / 'review.json'
    review_path.write_text('{}')
    if defect == 'changed_study': study.write_text(study.read_text() + ' ')
    elif defect == 'symlink':
        link = tmp_path / 'link.json'; link.symlink_to(review_path); review_path = link
    elif defect == 'fifo':
        import os
        fifo = tmp_path / 'fifo'; os.mkfifo(fifo); review_path = fifo
    elif defect == 'oversized': review_path.write_bytes(b' ' * (4 * 1024 * 1024 + 1))
    elif defect == 'duplicate_key': review_path.write_text('{"version":1,"version":2}')
    else: review_path.write_text('{"version":NaN}')
    r = run_cli(study, journal, '--session-review', review_path)
    assert r.returncode == 2
    assert not r.stdout
    assert '[평가입력 준비]' in r.stderr
