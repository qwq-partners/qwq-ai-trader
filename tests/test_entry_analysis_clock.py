"""Synthetic immutable-window regression: audit time never extends market evidence."""
import asyncio
from copy import deepcopy
from datetime import timedelta
import hashlib
import json
from pathlib import Path

import pytest

from src.analytics.entry_evaluation_bundle import build_evaluation_bundle
from src.analytics.entry_observation import prepare_input
from src.analytics.entry_observation_runtime import ObservationRuntime, validate_window_contract
from test_entry_observation_runtime import load, NOW, owner, read
from test_signal_window_observation import window_plan, stock
from src.analytics import entry_observation as obs, entry_gate_trace as gates


@pytest.fixture
def sealed_window(tmp_path, monkeypatch):
    import src.analytics.entry_observation_journal as journal
    plan, context = load(tmp_path, window_plan)
    clock = [NOW]
    for mod in (obs, gates):
        monkeypatch.setattr(mod, '_now', lambda: clock[0].isoformat())
    monkeypatch.setattr(journal, '_timestamp', lambda: clock[0].isoformat())

    async def capture():
        runtime = await ObservationRuntime.install(owner(), plan, now=lambda: clock[0])
        clock[0] = plan.start_at
        scan = obs.capture_scan(runtime.buffer, [stock(), stock('900002')], 'regular')
        trace = gates.begin_gate_trace(runtime.buffer, scan, [])
        trace.note('enabled', 'fail'); trace.finish()
        clock[0] = plan.end_at + timedelta(microseconds=17878)
        await runtime.close()
    asyncio.run(capture())
    return plan, context, read(plan)


def build(plan, data, **kwargs):
    return build_evaluation_bundle(plan.study_bytes, data, study_sha256=plan.study_sha256, **kwargs)


def test_explicit_audit_clock_accepts_late_seal_without_changing_economics(sealed_window):
    plan, context, data = sealed_window
    before = deepcopy(data), plan.study_path.read_bytes()
    with pytest.raises(ValueError, match='future journal seal'):
        build(plan, data)
    audit = (plan.end_at + timedelta(seconds=1)).isoformat()
    out = build(plan, data, analysis_as_of=audit)
    assert out['analysis_as_of'] == audit
    assert out['binding']['study_sha256'] == hashlib.sha256(before[1]).hexdigest()
    assert out['original']['payload']['as_of'] == context['as_of']
    assert out['original']['analysis_as_of'] == audit
    for result in [out['original']] + [v['result'] for v in out['sensitivity']]:
        assert result['payload']['as_of'] == context['as_of']
        assert result['report']['counts']['unknown'] == 2
        assert result['report']['counts']['paired_outcomes'] == 0
        assert result['report']['summary']['complete_delta_net_pnl'] is None
        assert result['report']['summary']['known_pair_delta_net_pnl'] is None
    assert out['readiness']['profitability_pass'] is False
    assert out['account_return'] is None
    assert (data, plan.study_path.read_bytes()) == before


@pytest.mark.parametrize('audit', ['not-a-time', '2026-10-01T01:10:00',
                                  '2026-10-01T01:08:59+00:00', '', 42])
def test_invalid_naive_or_early_audit_clock_rejected(sealed_window, audit):
    plan, _, data = sealed_window
    with pytest.raises(ValueError, match='analysis_as_of'):
        build(plan, data, analysis_as_of=audit)


def test_seal_after_explicit_audit_clock_rejected(sealed_window):
    plan, _, data = sealed_window
    with pytest.raises(ValueError, match='future journal seal'):
        build(plan, data, analysis_as_of=(plan.end_at + timedelta(microseconds=1)).isoformat())


@pytest.mark.parametrize('kind', ['ws_quote', 'signal', 'order_ready', 'entry_gate_trace',
                                 'quote_subscription', 'selection_basis', 'emit_result', 'rest_quote'])
def test_every_late_record_rejected_even_with_later_audit_clock(sealed_window, kind):
    plan, context, data = sealed_window
    data['records'].append({'kind': kind, 'sequence': len(data['records']) + 1,
                            'observed_at': (plan.end_at + timedelta(microseconds=1)).isoformat()})
    with pytest.raises(ValueError, match='window record'):
        validate_window_contract(context, data)
    with pytest.raises(ValueError, match='window record'):
        prepare_input(context, data, [], analysis_as_of=(plan.end_at + timedelta(days=1)).isoformat())


@pytest.mark.parametrize('observed_at', [None, '', '2026-10-01T01:00:00'])
def test_window_record_requires_aware_observation_time(sealed_window, observed_at):
    _, context, data = sealed_window
    data['records'].append({'kind': 'quote_subscription', 'observed_at': observed_at})
    with pytest.raises(ValueError):
        validate_window_contract(context, data)


def test_window_allows_prestart_subscription_ack_and_exact_end_record(sealed_window):
    plan, context, data = sealed_window
    data['records'].insert(0, {'kind': 'quote_subscription', 'status': 'acknowledged',
                               'observed_at': (plan.start_at - timedelta(seconds=1)).isoformat()})
    data['records'].append({'kind': 'quote_subscription', 'observed_at': plan.end_at.isoformat()})
    validate_window_contract(context, data)


def test_prepare_cli_propagates_audit_clock_and_preserves_files(sealed_window, capsys):
    from scripts.prepare_entry_evaluation import main
    plan, context, _ = sealed_window
    paths = [plan.study_path, Path(plan.settings['journal_path'])]
    before = [p.read_bytes() for p in paths]
    audit = (plan.end_at + timedelta(seconds=1)).isoformat()
    assert main(['--study', str(plan.study_path), '--journal', plan.settings['journal_path'],
                 '--max-journal-bytes', '4000000', '--analysis-as-of', audit]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out['analysis_as_of'] == audit
    assert out['original']['payload']['as_of'] == context['as_of']
    assert [p.read_bytes() for p in paths] == before


def test_compare_cli_propagates_audit_clock(sealed_window, tmp_path, capsys):
    from scripts.compare_entry_price_shadow import main
    plan, context, _ = sealed_window
    supplements = tmp_path / 'supplements.json'; supplements.write_text('[]')
    audit = (plan.end_at + timedelta(seconds=1)).isoformat()
    assert main(['--study', str(plan.study_path), '--journal', plan.settings['journal_path'],
                 '--max-journal-bytes', '4000000', '--evaluation-inputs', str(supplements),
                 '--analysis-as-of', audit]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out['analysis_as_of'] == audit
    assert out['payload']['as_of'] == context['as_of']


def test_explicit_analysis_clock_bounds_session_review_without_changing_legacy_default():
    from test_entry_evaluation_bundle import fixture, review
    c, data, sha = fixture()
    raw = (json.dumps(c, ensure_ascii=False, sort_keys=True, separators=(',', ':')) + '\n').encode()
    evidence = review(c, data, sha)
    legacy = build_evaluation_bundle(raw, data, study_sha256=sha, session_review=evidence)
    with pytest.raises(ValueError, match='session review after analysis_as_of'):
        build_evaluation_bundle(raw, data, study_sha256=sha, session_review=evidence,
                                analysis_as_of='2026-10-02T09:59:59+09:00')
    explicit = build_evaluation_bundle(raw, data, study_sha256=sha, session_review=evidence,
                                      analysis_as_of='2026-10-02T10:00:00+09:00')
    assert explicit['original']['report'] == legacy['original']['report']
    assert explicit['binding'] == legacy['binding']


def test_exit_comparison_propagates_analysis_clock(sealed_window):
    from src.analytics.entry_exit_comparison import build_exit_comparison
    from test_entry_exit_comparison import policy
    plan, _, data = sealed_window
    comparison = policy(plan.study_sha256)
    comparison['fixed_at'] = '2026-09-30T00:00:00+00:00'
    comparison['max_quote_gap_seconds'] = 60
    audit = (plan.end_at + timedelta(seconds=1)).isoformat()
    out = build_exit_comparison(plan.study_bytes, data, study_sha256=plan.study_sha256,
                               comparison_policy=comparison, analysis_as_of=audit)
    assert out['analysis_as_of'] == audit
    assert out['original']['summary']['known_stop_minus_hold_net_pnl'] is None
    assert out['account_return'] is None


def test_window_wrapper_uses_report_clock_and_keeps_multiscan_unsupported(sealed_window):
    from src.analytics.entry_window_evaluation import build_window_report
    plan, context, data = sealed_window
    protocol = {'version': 'entry-window-protocol-v1', 'phase': 'diagnostic',
                'dataset_kind': context['dataset_kind'], 'evaluation_epoch': context['evaluation_epoch'],
                'fixed_at': '2026-09-30T20:00:00+09:00',
                'calendar': {'source_ref': 'synthetic-calendar', 'source_sha256': 'a' * 64,
                             'scheduled_dates': ['2026-10-06']}}
    raw = plan.study_bytes.decode().replace('2026-10-01', '2026-10-06')
    data = json.loads(json.dumps(data).replace('2026-10-01', '2026-10-06'))
    data['journal']['study_sha256'] = hashlib.sha256(raw.encode()).hexdigest()
    day = {'date': '2026-10-06', 'study_utf8': raw, 'observations': data}
    audit = plan.end_at + timedelta(days=5, seconds=1)
    out = build_window_report(protocol, [day], as_of=audit)
    assert out['economic_verdict'] == 'suppressed'
    assert out['account_return'] is None
    data['records'].append(deepcopy(next(r for r in data['records'] if r['kind'] == 'scan')))
    with pytest.raises(ValueError, match='one scan per day supported'):
        build_window_report(protocol, [day], as_of=audit)
