"""Offline tests for same-entry initial-stop diagnostics."""
from copy import deepcopy
from decimal import Decimal
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
    value = saved['synthetic_input']
    return deepcopy(value['context']), deepcopy(value['observations'])


def review(context, observations, sha):
    return {
        'version': 'entry-session-review-v1', 'dataset_kind': context['dataset_kind'],
        'binding': {'study_sha256': sha, 'observation_sha256': digest(observations),
                    'capture_id': observations['journal']['capture_id'],
                    'evaluation_epoch': context['evaluation_epoch']},
        'quotes': [
            {'quote_id': q['quote_id'], 'symbol': q['symbol'], 'record_sha256': digest(q),
             'session': 'KRX_REGULAR_CONTINUOUS', 'basis': 'external_event_history_review',
             'source_ref': 'synthetic-state-events', 'source_sha256': 'a' * 64,
             'reviewer_ref': 'synthetic-reviewer', 'reviewed_at': '2026-10-02T10:00:00+09:00',
             'valid_from': '2026-10-02T09:00:00+09:00',
             'valid_until': '2026-10-02T09:45:00+09:00'}
            for q in observations['records'] if q['kind'] == 'ws_quote'
        ],
    }


def policy(sha):
    return {'version': 'same-entry-initial-stop-bid-v1', 'policy_ref': 'synthetic-initial-stop-v1',
            'study_sha256': sha, 'fixed_at': '2026-10-01T13:02:06+00:00',
            'max_quote_gap_seconds': 600}


def study_bytes(context):
    return (json.dumps(context, ensure_ascii=False, sort_keys=True, separators=(',', ':')) + '\n').encode()


def add_loss_path(observations):
    records = observations['records']
    for quote in records:
        if quote.get('kind') == 'ws_quote':
            quote['bid_size'] = 25
    entry = next(q for q in records if q.get('quote_id') == 'synthetic-entry-SYNTH_LOSS')
    middle = deepcopy(entry)
    middle.update(quote_id='synthetic-stop-SYNTH_LOSS', observed_at='2026-10-02T09:20:06+09:00',
                  ask='9210', bid='9200')
    middle['provenance']['received_at'] = '2026-10-02T09:20:05.5+09:00'
    records.insert(records.index(entry) + 1, middle)
    mark = next(q for q in records if q.get('quote_id') == 'synthetic-mark-SYNTH_LOSS')
    mark.update(ask='8510', bid='8500')
    for sequence, record in enumerate(records, 1):
        record['sequence'] = sequence


def test_initial_stop_uses_bid_net_loss_keeps_full_path_and_cash_cohort():
    from src.analytics.entry_exit_comparison import build_exit_comparison

    context, observations = fixture()
    add_loss_path(observations)
    raw = study_bytes(context); sha = hashlib.sha256(raw).hexdigest()
    before = deepcopy((context, observations))
    out = build_exit_comparison(raw, observations, study_sha256=sha,
                                comparison_policy=policy(sha), session_review=review(context, observations, sha))

    loss = next(row for row in out['original']['candidates'] if row['symbol'] == 'SYNTH_LOSS')
    assert loss['outcome_status'] == 'known_pair'
    assert loss['stop_trigger_quote_id'] == 'synthetic-stop-SYNTH_LOSS'
    assert Decimal(loss['stop_minus_hold_net_pnl']) > 0
    assert out['original']['counts']['total'] == 4
    assert out['original']['counts']['known_pairs'] == 1
    assert out['original']['counts']['unknown'] == 3
    assert out['production_eligible'] is False and out['account_return'] is None
    assert [item['slippage_bps_each'] for item in out['sensitivity']] == [0, 10, 30]
    assert (context, observations) == before


@pytest.mark.parametrize('defect', ['gap', 'vi', 'delayed', 'regression', 'size'])
def test_first_bad_path_quote_preserves_unknown(defect):
    from src.analytics.entry_exit_comparison import build_exit_comparison

    context, observations = fixture(); add_loss_path(observations)
    middle = next(q for q in observations['records'] if q.get('quote_id') == 'synthetic-stop-SYNTH_LOSS')
    if defect == 'gap':
        middle['observed_at'] = '2026-10-02T09:29:05+09:00'; middle['provenance']['received_at'] = '2026-10-02T09:29:04.5+09:00'
    elif defect == 'delayed':
        middle['observed_at'] = '2026-10-02T09:20:20+09:00'
    elif defect == 'regression':
        middle['observed_at'] = '2026-10-02T09:14:05+09:00'; middle['provenance']['received_at'] = '2026-10-02T09:14:04.5+09:00'
    elif defect == 'size':
        middle['bid_size'] = 1
    raw = study_bytes(context); sha = hashlib.sha256(raw).hexdigest(); evidence = review(context, observations, sha)
    if defect == 'vi':
        next(q for q in evidence['quotes'] if q['quote_id'] == middle['quote_id'])['session'] = 'VI'
    out = build_exit_comparison(raw, observations, study_sha256=sha,
                                comparison_policy=policy(sha), session_review=evidence)
    loss = next(row for row in out['original']['candidates'] if row['symbol'] == 'SYNTH_LOSS')
    assert loss['outcome_status'] == 'unknown'
    assert loss['reason_codes']


def test_first_stop_trigger_is_retained_after_rebound_and_no_trigger_matches_hold():
    from src.analytics.entry_exit_comparison import build_exit_comparison

    context, observations = fixture(); add_loss_path(observations)
    mark = next(q for q in observations['records'] if q.get('quote_id') == 'synthetic-mark-SYNTH_LOSS')
    mark.update(ask='11010', bid='11000')
    raw = study_bytes(context); sha = hashlib.sha256(raw).hexdigest()
    out = build_exit_comparison(raw, observations, study_sha256=sha,
                                comparison_policy=policy(sha), session_review=review(context, observations, sha))
    loss = next(row for row in out['original']['candidates'] if row['symbol'] == 'SYNTH_LOSS')
    assert loss['stop_trigger_quote_id'] == 'synthetic-stop-SYNTH_LOSS'
    assert Decimal(loss['stop_minus_hold_net_pnl']) < 0

    middle = next(q for q in observations['records'] if q.get('quote_id') == 'synthetic-stop-SYNTH_LOSS')
    middle.update(ask='9810', bid='9800')
    out = build_exit_comparison(raw, observations, study_sha256=sha,
                                comparison_policy=policy(sha), session_review=review(context, observations, sha))
    loss = next(row for row in out['original']['candidates'] if row['symbol'] == 'SYNTH_LOSS')
    assert loss['stop_trigger_quote_id'] is None
    assert loss['stop_net_pnl'] == loss['hold_net_pnl']


def test_unknown_baseline_and_zero_known_pairs_never_report_zero_effect():
    from src.analytics.entry_exit_comparison import build_exit_comparison

    context, observations = fixture(); raw = study_bytes(context); sha = hashlib.sha256(raw).hexdigest()
    out = build_exit_comparison(raw, observations, study_sha256=sha,
                                comparison_policy=policy(sha), session_review=review(context, observations, sha))
    assert out['original']['counts']['known_pairs'] == 0
    assert out['original']['summary']['known_stop_minus_hold_net_pnl'] is None
    assert out['original']['summary']['complete_stop_minus_hold_net_pnl'] is None

    context, observations = fixture(); add_loss_path(observations); observations['complete'] = False
    raw = study_bytes(context); sha = hashlib.sha256(raw).hexdigest()
    out = build_exit_comparison(raw, observations, study_sha256=sha,
                                comparison_policy=policy(sha), session_review=review(context, observations, sha))
    loss = next(row for row in out['original']['candidates'] if row['symbol'] == 'SYNTH_LOSS')
    assert loss['outcome_status'] == 'unknown'
    assert loss['reason_codes'] == ['BASELINE_NOT_ENTRY_VALID']


def test_net_loss_boundary_equal_triggers_and_strictly_above_does_not():
    from src.analytics.entry_exit_comparison import build_exit_comparison
    from src.analytics.entry_price_shadow import PriceGatePolicy, _fees

    context, observations = fixture(); add_loss_path(observations)
    middle = next(q for q in observations['records'] if q.get('quote_id') == 'synthetic-stop-SYNTH_LOSS')
    mark = next(q for q in observations['records'] if q.get('quote_id') == 'synthetic-mark-SYNTH_LOSS')
    mark.update(ask='11010', bid='11000')
    order = next(q for q in observations['records'] if q.get('kind') == 'order_ready' and q.get('symbol') == 'SYNTH_LOSS')
    price = PriceGatePolicy.from_dict(context['policy'])
    entry = Decimal('10000') * (1 + price.entry_slippage_bps / 10000)
    _, net_pct = _fees(context['fees']).calculate_net_pnl(entry, Decimal(middle['bid']), 25)
    order['effective_stop_pct'] = str(-net_pct)
    raw = study_bytes(context); sha = hashlib.sha256(raw).hexdigest()
    out = build_exit_comparison(raw, observations, study_sha256=sha,
                                comparison_policy=policy(sha), session_review=review(context, observations, sha))
    loss = next(row for row in out['original']['candidates'] if row['symbol'] == 'SYNTH_LOSS')
    assert loss['stop_trigger_quote_id'] == middle['quote_id']

    order['effective_stop_pct'] = str(-net_pct + Decimal('0.000001'))
    out = build_exit_comparison(raw, observations, study_sha256=sha,
                                comparison_policy=policy(sha), session_review=review(context, observations, sha))
    loss = next(row for row in out['original']['candidates'] if row['symbol'] == 'SYNTH_LOSS')
    assert loss['stop_trigger_quote_id'] is None


@pytest.mark.parametrize('stop_pct,original_trigger', [
    ('0.25', 'synthetic-entry-SYNTH_LOSS'),
    ('0.4', 'synthetic-stop-SYNTH_LOSS'),
])
def test_entry_spread_and_scenario_cost_can_trigger_stop_on_first_quote(stop_pct, original_trigger):
    from src.analytics.entry_exit_comparison import build_exit_comparison

    context, observations = fixture(); add_loss_path(observations)
    order = next(r for r in observations['records']
                 if r['kind'] == 'order_ready' and r['symbol'] == 'SYNTH_LOSS')
    order['effective_stop_pct'] = stop_pct
    raw = study_bytes(context); sha = hashlib.sha256(raw).hexdigest()
    out = build_exit_comparison(raw, observations, study_sha256=sha,
                                comparison_policy=policy(sha), session_review=review(context, observations, sha))
    original = next(r for r in out['original']['candidates'] if r['symbol'] == 'SYNTH_LOSS')
    stressed = next(r for r in out['sensitivity'][2]['candidates'] if r['symbol'] == 'SYNTH_LOSS')
    assert original['outcome_status'] == stressed['outcome_status'] == 'known_pair'
    assert original['stop_trigger_quote_id'] == original_trigger
    assert out['sensitivity'][2]['slippage_bps_each'] == 30
    assert stressed['stop_trigger_quote_id'] == 'synthetic-entry-SYNTH_LOSS'
    assert original['baseline_quantity'] == stressed['baseline_quantity'] == 25


def test_cli_exit_policy_adds_comparison_without_replacing_legacy_bundle(tmp_path):
    import asyncio
    from unittest.mock import patch
    from src.analytics.entry_observation import EntryObservationBuffer
    from src.analytics.entry_observation_journal import ObservationJournal, read_observation_journal

    context, observations = fixture(); add_loss_path(observations)
    raw = study_bytes(context); sha = hashlib.sha256(raw).hexdigest()
    study, journal = tmp_path / 'study.json', tmp_path / 'journal.jsonl'
    study.write_bytes(raw)

    async def write_journal():
        buffer = EntryObservationBuffer(evaluation_epoch=context['evaluation_epoch'], capacity=100)
        with patch('src.analytics.entry_observation_journal._timestamp', return_value='2026-10-02T09:14:59+09:00'):
            writer = await ObservationJournal.open(buffer, journal, study_ref='synthetic-only', study_sha256=sha,
                                                   queue_capacity=100, batch_size=10, max_bytes=1048576,
                                                   max_record_bytes=65536)
        for record in observations['records']:
            value = deepcopy(record); value.pop('sequence'); assert buffer.publish(value)
        with patch('src.analytics.entry_observation_journal._timestamp', return_value='2026-10-02T09:45:00+09:00'):
            await writer.close()

    asyncio.run(write_journal())
    loaded = read_observation_journal(journal, max_bytes=1048576, expected_study_sha256=sha)
    evidence, exit_policy = tmp_path / 'review.json', tmp_path / 'exit-policy.json'
    evidence.write_text(json.dumps(review(context, loaded, sha)))
    exit_policy.write_text(json.dumps(policy(sha)))
    before = (study.read_bytes(), journal.read_bytes(), evidence.read_bytes(), exit_policy.read_bytes())
    run = subprocess.run([sys.executable, str(CLI), '--study', str(study), '--journal', str(journal),
                          '--max-journal-bytes', '1048576', '--session-review', str(evidence),
                          '--exit-policy', str(exit_policy)], capture_output=True, text=True, timeout=5)
    assert run.returncode == 0, run.stderr
    output = json.loads(run.stdout)
    assert output['version'] == 'entry-evaluation-bundle-v1'
    assert output['exit_comparison']['version'] == 'entry-exit-comparison-v1'
    assert (study.read_bytes(), journal.read_bytes(), evidence.read_bytes(), exit_policy.read_bytes()) == before
