"""실제 저널 진단 전달과 비용 부분합에 추정액을 사용했는지 구분한다."""
import json
from pathlib import Path

import pytest

from scripts.report_model_comparison import main
from src.analytics.model_comparison_daily_report import build_daily_report
from tests.test_model_comparison_daily_report import DAY, pair
from tests.test_model_comparison_incomplete_report import sealed_status


@pytest.fixture(autouse=True)
def isolated_home(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)


@pytest.mark.parametrize('option,key', [('--offers', 'offer_journal'), ('--incomplete', 'incomplete_journal')])
@pytest.mark.parametrize('conflicting', [False, True])
def test_cli_exposes_identical_or_conflicting_duplicate_sequences(tmp_path, capsys, option, key, conflicting):
    close = {'schema_version': 'comparison-offer-v1', 'kind': 'journal_close',
             'run_id': 'run-1', 'journal_seq': 1, 'final_seq': 1,
             'journal_failed_total': 0, 'journal_dropped_total': 0}
    duplicate = dict(close, journal_failed_total=1) if conflicting else close
    journal = tmp_path / 'journal.jsonl'
    journal.write_text(json.dumps(close) + '\n' + json.dumps(duplicate) + '\n')
    pairs = tmp_path / 'pairs.jsonl'
    pairs.write_text('')
    assert main(['--pairs', str(pairs), '--daily', DAY, option, str(journal)]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report[key]['duplicate_sequences'] == 1
    assert report[key]['conflicting_sequences'] == int(conflicting)
    assert report[key]['skipped_lines'] == 1
    assert report[key]['complete'] is False


def test_legacy_sealed_status_stays_complete_without_optional_diagnostics():
    report = build_daily_report([], day_kst=DAY, journal_status=sealed_status(),
                                incomplete_journal_status=sealed_status())
    for key in ('offer_journal', 'incomplete_journal'):
        assert report[key]['complete'] is True
        assert report[key]['duplicate_sequences'] is None
        assert report[key]['conflicting_sequences'] is None
    assert report['spend']['total_usd'] == '0'


@pytest.mark.parametrize('source,expected_subtotal,uses_estimate', [
    ('original', '0.004', True), ('zero_original', '0', True),
    ('reconciled', '0.006', False), ('ledger', '0.009', False),
    ('empty', '0', False), ('unknown', '0', False),
])
def test_subtotal_provenance_tracks_used_amounts_not_available_estimates(source, expected_subtotal, uses_estimate):
    row = pair('d1')
    attempts = {a['attempt_id']: a for arm in ('current', 'candidate') for a in row[arm]['attempts']}
    kwargs = {}
    if source == 'zero_original':
        for arm in ('current', 'candidate'):
            for attempt in row[arm]['attempts']:
                attempt['api_cost_usd'] = '0'
    elif source == 'reconciled':
        kwargs['reconciled_lookup'] = {key: {'actual_cost_usd': '0.002', 'reconciliation_id': 7}
                                      for key in attempts}
    elif source in ('ledger', 'unknown'):
        kwargs['ledger_attempts'] = [{'attempt_id': key, 'state': 'known' if source == 'ledger' else 'unknown',
                                     'cost_usd': '0.003' if source == 'ledger' else None}
                                    for key in attempts]
    report = build_daily_report([] if source == 'empty' else [row], day_kst=DAY, **kwargs)
    assert report['spend']['known_subtotal_usd'] == expected_subtotal
    assert report['spend']['subtotal_uses_original_estimate'] is uses_estimate
    assert report['spend']['total_usd'] == (None if source == 'unknown' else expected_subtotal)
    if source in ('reconciled', 'ledger', 'unknown'):
        assert report['spend']['original_estimated_usd'] == '0.004'
    if source == 'reconciled':
        assert report['spend']['reconciled_usd'] == '0.006'


def test_mixed_reconciled_and_original_zero_still_marks_estimate_used():
    row = pair('d1', bear_cost='0')
    lookup = {f'p:bull_{arm}:0': {'actual_cost_usd': '0.002'} for arm in ('current', 'candidate')}
    report = build_daily_report([row], day_kst=DAY, reconciled_lookup=lookup)
    assert report['spend']['known_subtotal_usd'] == '0.004'
    assert report['spend']['subtotal_uses_original_estimate'] is True
