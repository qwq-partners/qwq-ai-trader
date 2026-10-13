"""일별 비교 보고·실험 manifest 상한 — 합성 행만, 발송·호출·원장 접근 없음."""
import json
from copy import deepcopy
from pathlib import Path

import pytest

from src.analytics import model_comparison_daily_report as daily
from scripts.report_model_comparison import main as report_main

MANIFEST = Path(__file__).resolve().parents[1] / 'config/experiments/R1-bull-reasoning-20261013.json'
DAY = '2026-10-15'


@pytest.fixture(autouse=True)
def isolated_home(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)


def env(decision_id, at='2026-10-15T00:30:00+00:00', age=1.0, kind='in_process_nonce'):
    from datetime import datetime, timedelta
    received = (datetime.fromisoformat(at) + timedelta(seconds=age)).isoformat()
    return {'decision_id': decision_id, 'input_available_at': at, 'received_at': received,
            'attestation': {'kind': kind, 'nonce_sha256': 'x'}, 'common_context': 'ctx'}


def attempt(attempt_id, cost='0.001', sent=True, error=None):
    return {'attempt_id': attempt_id, 'role': attempt_id.split(':')[1], 'requested_model': 'gpt-5-mini',
            'resolved_model': 'gpt-5-mini', 'model_identity': 'provider_reported_unverified',
            'requested_model_match': True, 'reasoning_effort': 'minimal', 'max_output_tokens': 400,
            'latency_ms': 800.0, 'request_sent': sent, 'api_cost_usd': cost, 'error': error}


def pair(decision_id, *, v2=True, bear_cost='0.002', at='2026-10-15T00:30:00+00:00'):
    bear = attempt('p:bear:0', bear_cost)

    def arm(name, effort):
        bull = attempt(f'p:bull_{name}:0')
        bull['reasoning_effort'] = effort
        value = {'verdict': 'approve', 'validation_verdict': 'approve', 'approval_basis': 'adversarial_policy',
                 'decision_available_at': '2026-10-15T00:30:03+00:00', 'attempts': [bull, bear],
                 'api_cost_usd': '0.003', 'sizing_replay': {'quantity': 200}, 'reasons': []}
        if v2:
            value.update(queue_delay_ms=120.0, provider_latency_ms=1600.0)
        return value

    row = {'schema_version': 'model-comparison-pair-v2' if v2 else 'model-comparison-pair-v1',
           'pair_id': 'p', 'decision_id': decision_id, 'pricing_basis': 'paired_live_shadow',
           'requested_at': '2026-10-15T00:30:01+00:00', 'evaluation_at': '2026-10-15T00:45:00+00:00',
           'policy': {'input_available_at': at, 'horizon_seconds': 900},
           'current': arm('current', 'minimal'), 'candidate': arm('candidate', 'low')}
    if v2:
        row.update(input_available_at=at, enqueued_at=at, evaluation_anchor='input_available_at',
                   live_provenance='in_process_attested')
    return row


def test_manifest_budget_matches_topology_and_reserve_formula():
    manifest = json.loads(MANIFEST.read_text())
    assert manifest['enabled'] is False
    computed = daily.worst_case_reservation(manifest)
    assert computed['attempts_per_pair'] == 2 + 2 * (2 + 2) == 10
    for key in ('attempts_per_pair', 'daily_attempt_cap', 'reservation_usd_ceiling_per_pair',
                'experiment_usd_ceiling'):
        assert manifest['budget'][key] == computed[key]
    roles = manifest['roles']
    assert roles['bull_current']['reasoning_effort'] == 'minimal'
    assert roles['bull_candidate']['reasoning_effort'] == 'low'
    changed = {k for k in roles['bull_current'] if roles['bull_current'][k] != roles['bull_candidate'][k]}
    assert changed == {'reasoning_effort'}
    from src.utils.llm import _OPENAI_EFFORTS
    assert {'minimal', 'low'} <= _OPENAI_EFFORTS['gpt-5-mini']
    from src.core import adversarial_validator as av
    assert roles['bear']['max_output_tokens'] == roles['bull_current']['max_output_tokens'] == av.MAX_TOKENS
    assert roles['bull_current']['seed'] == av.VERDICT_SEED
    assert manifest['strict_on_disagreement'] is av.STRICT_ON_DISAGREEMENT


def test_zero_samples():
    report = daily.build_daily_report([], day_kst=DAY, offered_envelopes=[])
    assert report['denominators'] == {'offered': 0, 'eligible': 0, 'sampled': 0, 'complete': 0, 'excluded': 0}
    assert report['spend']['total_usd'] == '0'
    assert '제공 0' in report['telegram_preview']


def test_exclusion_only_day_keeps_denominator_and_reasons():
    envs = [env('d1', age=9), env('d2', kind='caller_supplied_not_attested'),
            dict(env('d3'), received_at=None)]
    report = daily.build_daily_report([], day_kst=DAY, offered_envelopes=envs)
    assert report['denominators'] == {'offered': 3, 'eligible': 0, 'sampled': 0, 'complete': 0, 'excluded': 3}
    assert report['missing_reasons'] == {'input_too_old': 1, 'not_received': 1, 'unattested_input': 1}


def test_complete_pair_counts_unique_attempts_once_and_shows_latency_and_quantity():
    report = daily.build_daily_report([pair('d1')], day_kst=DAY, offered_envelopes=[env('d1')])
    assert report['denominators']['complete'] == 1 and report['denominators']['excluded'] == 0
    # 공통 Bear 0.002 를 한 번만 센다: bull 0.001×2 + bear 0.002
    assert report['spend']['original_estimated_known_usd'] == '0.004'
    assert report['spend']['unique_attempts'] == 3
    arm = report['rows'][0]['candidate']
    assert arm['quantity'] == 200 and arm['queue_delay_ms'] == 120.0
    assert arm['completion_minus_input_ms'] == 3000.0
    assert arm['attempts'][0]['reasoning_effort'] == 'low'
    assert report['rows'][0]['conditional_economics']['net_delta_krw'] is None


def test_partial_result_and_missing_pair():
    row = pair('d1')
    row['candidate'] = None
    report = daily.build_daily_report([row], day_kst=DAY, offered_envelopes=[env('d1')])
    assert report['missing_reasons'] == {'partial_result': 1}
    report = daily.build_daily_report([], day_kst=DAY, offered_envelopes=[env('d1')])
    assert report['missing_reasons'] == {'pair_result_missing': 1}


def test_unknown_cost_then_reconciled_cost():
    row = pair('d1', bear_cost=None)
    report = daily.build_daily_report([row], day_kst=DAY, offered_envelopes=[env('d1')])
    assert report['spend']['still_unknown_attempts'] == ['p:bear:0'] and report['spend']['total_usd'] is None
    assert '불명 1건' in report['telegram_preview']
    lookup = {'p:bear:0': {'reconciled': {'actual_cost_usd': '0.0025', 'reconciliation_id': 7}}}
    report = daily.build_daily_report([row], day_kst=DAY, offered_envelopes=[env('d1')], reconciled_lookup=lookup)
    assert report['spend']['reconciled_usd'] == '0.0025' and report['spend']['total_usd'] == '0.0045'
    assert report['spend']['reconciled_attempts'] == [{'attempt_id': 'p:bear:0', 'reconciliation_id': 7}]


def test_budget_mode_change_stops_the_day():
    row = pair('d1')
    row['candidate']['attempts'][0].update(error='comparison_policy_mode_not_normal', api_cost_usd='0',
                                           request_sent=False)
    report = daily.build_daily_report([row], day_kst=DAY, offered_envelopes=[env('d1'), env('d2', at='2026-10-15T01:00:00+00:00')],
                                      exclusions=[{'decision_id': 'd2', 'reason': 'comparison_policy_mode_not_normal'}])
    assert report['stop_conditions_reached'] == ['comparison_policy_mode_not_normal']
    assert 'comparison_policy_mode_not_normal' in report['telegram_preview']


def test_kst_day_and_month_boundaries():
    late = env('d-late', at='2026-10-31T14:59:59+00:00')     # KST 10-31 23:59:59
    early = env('d-early', at='2026-10-31T15:00:00+00:00')   # KST 11-01 00:00:00
    october = daily.build_daily_report([], day_kst='2026-10-31', offered_envelopes=[late, early])
    november = daily.build_daily_report([], day_kst='2026-11-01', offered_envelopes=[late, early])
    assert october['denominators']['offered'] == november['denominators']['offered'] == 1
    assert daily.select_daily_sample([late, early], day_kst='2026-11-01')['sampled'] == ['d-early']


def test_selection_is_independent_of_later_results():
    envs = [env('d2', at='2026-10-15T01:00:00+00:00'), env('d1', at='2026-10-15T00:30:00+00:00')]
    good = pair('d1')
    bad = deepcopy(good)
    bad['current']['verdict'] = bad['candidate']['verdict'] = 'reject'
    outcomes = {'d1': {'status': 'conditional_price_proxy', 'net_delta_krw': '-99999'}}
    a = daily.build_daily_report([good], day_kst=DAY, offered_envelopes=envs)
    b = daily.build_daily_report([bad], day_kst=DAY, offered_envelopes=envs, outcomes=outcomes)
    assert [r['decision_id'] for r in a['rows'] if r['sampled']] == ['d1']
    assert [r['decision_id'] for r in b['rows'] if r['sampled']] == ['d1']
    assert a['denominators'] == b['denominators']
    assert a['missing_reasons'] == b['missing_reasons'] == {'daily_quota': 1}
    assert b['rows'][0]['conditional_economics']['net_delta_krw'] == '-99999'


def test_v1_rows_are_labelled_legacy_and_duplicates_count_once():
    report = daily.build_daily_report([pair('d1', v2=False)], day_kst=DAY, offered_envelopes=[env('d1'), env('d1')])
    assert report['rows'][0]['evaluation_anchor'] == 'requested_at (legacy)'
    assert report['rows'][0]['current']['queue_delay_ms'] is None
    assert report['denominators']['offered'] == 1


def test_cli_daily_is_read_only(tmp_path, capsys):
    pairs = tmp_path / 'pairs.jsonl'
    pairs.write_text(json.dumps(pair('d1')) + '\n')
    envelopes = tmp_path / 'env.jsonl'
    envelopes.write_text(json.dumps(env('d1')) + '\n')
    assert report_main(['--pairs', str(pairs), '--daily', DAY, '--envelopes', str(envelopes)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out['schema_version'] == daily.SCHEMA_VERSION and out['denominators']['complete'] == 1
    assert report_main(['--pairs', str(pairs), '--daily', '2026-13-01']) == 2
