"""손상된 incomplete 저널은 지출 0의 증거가 아니다 — 파일 기반 보고 경계."""
import json
from copy import deepcopy
from pathlib import Path

import pytest

from scripts.report_model_comparison import main
from src.analytics.model_comparison_daily_report import build_daily_report
from src.analytics.model_comparison_worker import read_incomplete_journal, row_sha256
from tests.test_model_comparison_daily_report import DAY, env, pair


@pytest.fixture(autouse=True)
def isolated_home(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)


def sealed_status(**changes):
    return dict({'missing': False, 'skipped_lines': 0, 'sequence_gaps': 0,
                 'dropped_records': 0, 'unsequenced_records': 0, 'write_failures': 0,
                 'open_runs': [], 'closed_runs': ['run-1']}, **changes)


@pytest.mark.parametrize('damage', ['truncated_json', 'bad_row_hash'])
def test_cli_preserves_unknown_spend_when_incomplete_evidence_is_damaged(tmp_path, capsys, damage):
    pairs = tmp_path / 'pairs.jsonl'
    pairs.write_text('')
    row = {'pair_id': 'paid-pair-lost', 'total_comparison_cost_usd': '0.003'}
    record = {'schema_version': 'comparison-offer-v1', 'kind': 'incomplete_result',
              'run_id': 'run-1', 'journal_seq': 1, 'recorded_at': '2026-10-15T09:30:05+09:00',
              'decision_id': 'decision-paid', 'pair_id': row['pair_id'],
              'row': row, 'row_sha256': row_sha256(row)}
    incomplete = tmp_path / 'incomplete.jsonl'
    incomplete.write_text(json.dumps(record) + '\n')
    argv = ['--pairs', str(pairs), '--daily', DAY, '--incomplete', str(incomplete)]
    assert main(argv) == 0
    assert json.loads(capsys.readouterr().out)['spend']['total_usd'] is None
    if damage == 'truncated_json':
        incomplete.write_text(json.dumps(record)[:-20])
    else:
        record['row_sha256'] = '0' * 64
        incomplete.write_text(json.dumps(record) + '\n')
    assert read_incomplete_journal(incomplete, max_bytes=1 << 20)['skipped_lines'] == 1
    assert main(argv) == 0
    report = json.loads(capsys.readouterr().out)
    assert report['spend']['total_usd'] is None
    assert report['incomplete_journal']['skipped_lines'] == 1
    assert report['incomplete_journal']['complete'] is False
    assert '지출 미확정' in report['telegram_preview']


@pytest.mark.parametrize('changes', [
    {'skipped_lines': 1}, {'sequence_gaps': 1}, {'dropped_records': 1},
    {'write_failures': 1}, {'unsequenced_records': 1},
    {'open_runs': ['run-2']}, {'closed_runs': []}, {'missing': True},
    {'sequence_gaps': None}, {'dropped_records': -1}, {'write_failures': True},
])
def test_incomplete_integrity_prevents_certifying_total_without_erasing_known_costs(changes):
    row = pair('d1')
    before = deepcopy(row)
    report = build_daily_report([row], day_kst=DAY, offered_envelopes=[env('d1')],
                                incomplete_journal_status=sealed_status(**changes))
    assert report['spend']['total_usd'] is None
    assert report['spend']['known_subtotal_usd'] == '0.004'
    assert report['spend']['original_estimated_usd'] == '0.004'
    assert report['denominators']['complete'] == 1
    assert len(report['rows']) == 1 and row == before
    assert report['incomplete_journal']['complete'] is False
    assert 'incomplete_journal_incomplete' in report['limitations']


def test_sealed_incomplete_journal_with_no_lost_pairs_can_report_zero():
    report = build_daily_report([], day_kst=DAY, incomplete_journal_status=sealed_status())
    assert report['incomplete_journal']['complete'] is True
    assert report['spend']['total_usd'] == report['spend']['known_subtotal_usd'] == '0'
    assert 'incomplete_journal_incomplete' not in report['limitations']


def test_omitted_incomplete_journal_preserves_optional_behavior():
    report = build_daily_report([], day_kst=DAY)
    assert 'incomplete_journal' not in report
    assert report['spend']['total_usd'] == '0'


@pytest.mark.parametrize('closed', [False, True])
def test_cli_reports_open_or_sealed_empty_incomplete_stream(tmp_path, capsys, closed):
    pairs = tmp_path / 'pairs.jsonl'
    pairs.write_text('')
    records = [{'schema_version': 'comparison-offer-v1', 'kind': 'journal_drop',
                'run_id': 'run-1', 'journal_seq': 1, 'dropped_total': 0}]
    if closed:
        records.append({'schema_version': 'comparison-offer-v1', 'kind': 'journal_close',
                        'run_id': 'run-1', 'journal_seq': 2, 'final_seq': 2,
                        'journal_failed_total': 0, 'journal_dropped_total': 0})
    incomplete = tmp_path / 'incomplete.jsonl'
    incomplete.write_text(''.join(json.dumps(record) + '\n' for record in records))
    assert main(['--pairs', str(pairs), '--daily', DAY, '--incomplete', str(incomplete)]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report['spend']['total_usd'] == ('0' if closed else None)
    assert report['incomplete_journal']['complete'] is closed


def test_cli_missing_incomplete_file_is_an_error(tmp_path, capsys):
    pairs = tmp_path / 'pairs.jsonl'
    pairs.write_text('')
    assert main(['--pairs', str(pairs), '--daily', DAY,
                 '--incomplete', str(tmp_path / 'missing.jsonl')]) == 2
    assert 'incomplete journal missing' in json.loads(capsys.readouterr().out)['error']


def test_writer_close_preserves_dropped_recovery_evidence_in_next_day_cli(tmp_path, monkeypatch, capsys):
    from tests.test_model_comparison_worker import build
    from src.analytics.model_comparison_worker import read_offer_journal
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    worker, ledger, *_ = build(tmp_path, journal_queue=1, journal_dir=tmp_path / 'journals')
    old_row = {'pair_id': 'previous-day'}
    old = {'schema_version': 'comparison-offer-v1', 'kind': 'incomplete_result',
           'recorded_at': '2026-10-13T09:30:00+09:00', 'decision_id': 'old',
           'pair_id': 'previous-day', 'row': old_row, 'row_sha256': row_sha256(old_row)}
    worker._journal_put(old)
    worker._journal_write(worker._journal.get_nowait())
    worker._journal_put({'schema_version': 'comparison-offer-v1', 'kind': 'offer',
                         'recorded_at': '2026-10-14T09:30:00+09:00', 'decision_id': 'new',
                         'status': 'accepted', 'envelope': {}})
    lost_row = {'pair_id': 'today-paid-lost'}
    worker._journal_put(dict(old, recorded_at='2026-10-14T09:30:01+09:00',
                             decision_id='new', pair_id=lost_row['pair_id'],
                             row=lost_row, row_sha256=row_sha256(lost_row)), timeout=0)
    assert worker.health()['journal_dropped'] == 1
    worker._stop.set()
    worker._producer_done.set()
    worker._journal_run()  # Actual writer emits drop only to offers, and close to both touched files.
    ledger.close()
    incomplete, = worker.journal_dir.glob('incomplete-*.jsonl')
    offers, = worker.journal_dir.glob('offers-*.jsonl')
    assert read_offer_journal(offers, max_bytes=1 << 20)['dropped_records'] == 1
    assert read_incomplete_journal(incomplete, max_bytes=1 << 20)['dropped_records'] == 1
    pairs = tmp_path / 'pairs.jsonl'
    pairs.write_text('')
    assert main(['--pairs', str(pairs), '--daily', '2026-10-14', '--incomplete', str(incomplete)]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report['incomplete_journal']['complete'] is False
    assert report['spend']['total_usd'] is None
    assert report['spend']['known_subtotal_usd'] == '0'
