import importlib
from copy import deepcopy
from decimal import Decimal

from src.analytics.order_rr_comparison import compare_order_rr
from src.utils.fee_calculator import FeeConfig
from src.utils.rr_context import SOURCE_REF


def row():
    selection = {'version': 1, 'status': 'captured', 'reason': None,
                 'source_ref': SOURCE_REF, 'symbol': '000001', 'strategy': 'sepa_trend',
                 'prices': {'entry': '10000', 'target': '12000', 'stop': '9000'},
                 'captured_at': '2026-10-28T09:00:00+09:00',
                 'gate': {'basis': 'entry_roundtrip_per_share', 'threshold': '2.0', 'passed': True},
                 'diagnostics': {'gross_rr': 2.0, 'fee_adjusted_rr': 1.9, 'gate_rr': 1.9,
                                 'round_trip_cost': 22.0, 'cost_status': 'calculated'},
                 'fee_snapshot': {'version': 1, 'status': 'captured', 'reason': None,
                                  'buy_commission_rate': '0.000140527',
                                  'sell_commission_rate': '0.000130527', 'sell_tax_rate': '0.002'}}
    context = {'version': 1, 'status': 'captured', 'reason': None, 'selection': selection,
               'signal': {'signal_id': 'sig-1', 'source': 'batch_analyzer', 'symbol': '000001',
                          'strategy': 'sepa_trend', 'timestamp': '2026-10-28T09:30:00+09:00',
                          'quote_as_of': None, 'prices': {'entry': '10100', 'target': '12000', 'stop': '9500'}},
               'plan': {'plan_id': 'plan-1', 'plan_version': 1},
               'quantities': {'requested_quantity': 10, 'entry_risk': {'planned_quantity': 10,
                              'planned_price': '10100'}, 'size_trace_quantity': 10}}
    return {'id': 55, 'event_time': '2026-10-28T09:30:00+09:00', 'symbol': '000001',
            'strategy': 'sepa_trend', 'side': 'buy', 'event_type': 'passed',
            'metadata': {'signal_id': 'sig-1', 'rr_context': context}}


def test_selected_and_current_prices_use_existing_comparator_for_each_quantity_lane():
    try:
        module = importlib.import_module('src.analytics.rr_context_comparison')
    except ModuleNotFoundError as exc:
        assert False, f'comparison adapter is missing: {exc.name}'
    result = module.compare_rr_rows([row()], source_ref='fixture', data_kind='synthetic')
    item = result['rows'][0]
    assert item['selection_observed']['diagnostics']['fee_adjusted_rr'] == 1.9
    assert item['price_changes']['entry']['percent'] == '1'
    assert item['price_changes']['stop']['changed'] is True
    fees = FeeConfig(Decimal('0.000140527'), Decimal('0.000130527'), Decimal('0.002'))
    for name in ('requested_quantity', 'entry_risk.planned_quantity', 'size_trace_quantity'):
        lane = item['quantity_lanes'][name]
        for key, prices in [('selected_prices', ('10000', '12000', '9000')),
                            ('current_prices', ('10100', '12000', '9500'))]:
            expected = compare_order_rr(entry_price=prices[0], target_price=prices[1], stop_price=prices[2],
                                        quantity=10, source_path='signal_events.metadata.rr_context',
                                        source_ref='fixture', price_basis=key, fee_config=fees,
                                        fee_config_ref='selection.fee_snapshot',
                                        original_gate={'basis': 'entry_roundtrip_per_share',
                                                       'positive_threshold': '2.0', 'evidence_ref': SOURCE_REF})
            assert lane[key] == expected
    assert result['coverage']['row_count'] == result['coverage']['unique_signal_count'] == 1


def compare(rows):
    from src.analytics.rr_context_comparison import compare_rr_rows
    return compare_rr_rows(rows, source_ref='fixture', data_kind='synthetic')


def test_lanes_keep_missing_zero_and_conflicts_separate():
    r = row()
    q = r['metadata']['rr_context']['quantities']
    q['requested_quantity'] = None
    q['entry_risk']['planned_quantity'] = 0
    q['size_trace_quantity'] = 12
    item = compare([r])['rows'][0]
    assert item['quantity_conflict'] is True
    lanes = item['quantity_lanes']
    assert lanes['requested_quantity']['selected_prices']['calculation_reason'] == 'missing_quantity'
    assert lanes['entry_risk.planned_quantity']['selected_prices']['calculation_reason'] == 'zero_quantity'
    assert lanes['size_trace_quantity']['selected_prices']['inputs']['quantity'] == 12


def test_identity_version_source_and_unsupported_rows_are_not_compared():
    cases = []
    for path, value, reason in [
        (('side',), 'sell', 'unsupported_strategy_or_side'),
        (('strategy',), 'other', 'unsupported_strategy_or_side'),
        (('metadata', 'signal_id'), 'wrong', 'identity_mismatch'),
        (('metadata', 'rr_context', 'signal', 'symbol'), 'other', 'identity_mismatch'),
        (('metadata', 'rr_context', 'version'), 2, 'unsupported_context_version'),
        (('metadata', 'rr_context', 'selection', 'source_ref'), 'wrong', 'source_mismatch'),
        (('metadata', 'rr_context', 'selection', 'version'), 2, 'unsupported_version'),
        (('metadata', 'rr_context', 'selection', 'gate', 'basis'), 'unknown', 'invalid_snapshot'),
        (('metadata', 'rr_context', 'selection'), {'version': 1, 'status': 'conflict', 'reason': 'selection_conflict'}, 'selection_conflict')]:
        r = row()
        target = r
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = value
        cases.append((r, reason))
    cases.append(({'id': 88, 'metadata': {}}, 'context_missing'))
    for r, reason in cases:
        result = compare([r])['rows'][0]
        assert result['reason'] == reason
        assert result['quantity_lanes'] == {}


def test_missing_fees_zero_fees_and_comparator_range():
    r = row()
    sel = r['metadata']['rr_context']['selection']
    sel['fee_snapshot'] = {'version': 1, 'status': 'unavailable', 'reason': 'fee_config_unavailable'}
    result = compare([r])['rows'][0]['quantity_lanes']['requested_quantity']['selected_prices']
    assert result['calculation_reason'] == 'invalid_fee_config'
    sel['fee_snapshot'] = {'version': 1, 'status': 'captured', 'reason': None,
                           'buy_commission_rate': '0', 'sell_commission_rate': '0', 'sell_tax_rate': '0'}
    assert compare([r])['rows'][0]['quantity_lanes']['requested_quantity']['selected_prices']['quantity_adjusted']['rr'] == '2'
    sel['prices']['entry'] = '1' * 29
    assert compare([r])['rows'][0]['quantity_lanes']['requested_quantity']['selected_prices']['calculation_reason'] == 'invalid_price'
    sel['prices']['entry'] = '10000'
    r['metadata']['rr_context']['signal']['prices']['target'] = None
    assert compare([r])['rows'][0]['quantity_lanes']['requested_quantity']['current_prices']['calculation_reason'] == 'missing_price'


def test_coverage_counts_rows_and_unique_signals_separately_and_empty():
    r = row()
    second = deepcopy(r)
    second['id'] = 56
    coverage = compare([r, second])['coverage']
    assert (coverage['row_count'], coverage['unique_signal_count']) == (2, 1)
    assert compare([])['coverage']['row_count'] == compare([])['coverage']['unique_signal_count'] == 0


def test_cli_json_markdown_and_rejects_bad_input_without_payload(tmp_path):
    import hashlib
    import json
    import os
    import subprocess
    import sys
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    script = root / 'scripts' / 'compare_rr_context.py'
    path = tmp_path / 'input.json'
    payload = json.dumps([row()]).encode()
    path.write_bytes(payload)
    env = {'HOME': str(tmp_path), 'PATH': '/usr/bin:/bin', 'PYTHONPATH': str(root)}
    def run(fmt='json'):
        return subprocess.run([sys.executable, str(script), '--input', str(path), '--data-kind', 'synthetic',
                               '--format', fmt], capture_output=True, text=True, env=env, cwd=tmp_path)
    result = run()
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data['input_sha256'] == hashlib.sha256(payload).hexdigest()
    assert data['data_kind'] == 'synthetic'
    result = run('markdown')
    assert result.returncode == 0 and hashlib.sha256(payload).hexdigest() in result.stdout
    assert 'sig-1' in result.stdout and 'selected_prices' in result.stdout and 'current_prices' in result.stdout
    for bad in ('[{"secret":"private-value","secret":0}]', '[NaN]', '[1e999]', '{}', '['):
        path.write_text(bad)
        result = run()
        assert result.returncode == 2 and not result.stdout
        assert 'private-value' not in result.stderr and str(path) not in result.stderr
    path.write_bytes(b' ' * (32 * 1024 * 1024 + 1))
    assert run().returncode == 2


def test_context_version_does_not_validate_a_conflicting_signal_id():
    r = row()
    r['metadata']['signal_id'] = 'different'
    r['metadata']['rr_context']['version'] = 99
    assert compare([r])['coverage']['unique_signal_count'] == 0


def test_recorded_plan_provenance_is_separate_from_signal():
    reference = compare([row()])['rows'][0]['current_reference']
    assert reference['plan'] == {'plan_id': 'plan-1', 'plan_version': 1, 'planned_price': '10100'}


def test_input_change_and_non_regular_file_are_rejected(tmp_path, monkeypatch):
    import importlib.util
    import os
    from pathlib import Path
    import pytest
    spec = importlib.util.spec_from_file_location('rr_cli', Path(__file__).resolve().parents[1] / 'scripts/compare_rr_context.py')
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    path = tmp_path / 'input.json'
    path.write_text('[]')
    real_stat = cli.os.stat
    def changed_stat(p):
        result = real_stat(p)
        values = list(result)
        values[6] += 1
        return os.stat_result(values)
    monkeypatch.setattr(cli.os, 'stat', changed_stat)
    with pytest.raises(cli.InputError, match='input_changed'):
        cli.read_input(path)
    monkeypatch.undo()
    fifo = tmp_path / 'pipe'
    os.mkfifo(fifo)
    with pytest.raises(cli.InputError, match='invalid_file'):
        cli.read_input(fifo)


def test_reused_signal_id_with_other_symbol_is_conflict():
    a, b = row(), row()
    b['symbol'] = '000002'
    context = b['metadata']['rr_context']
    context['signal']['symbol'] = context['selection']['symbol'] = '000002'
    result = compare([a, b])
    assert result['coverage']['unique_signal_count'] == 0
    assert [item['reason'] for item in result['rows']] == ['signal_identity_conflict'] * 2
    assert all(not item['quantity_lanes'] for item in result['rows'])


def test_empty_actual_input_explicitly_reports_no_comparison():
    from src.analytics.rr_context_comparison import compare_rr_rows
    result = compare_rr_rows([], source_ref='fixture', data_kind='actual')
    assert result['coverage']['comparison_pairs'] == 0
    assert result['coverage']['no_comparison_reason'] == 'no_rows'


def test_noncomparable_rows_report_no_comparison_and_reasons():
    r = row()
    r['metadata']['rr_context']['quantities'] = {}
    coverage = compare([r])['coverage']
    assert coverage['comparison_pairs'] == 0
    assert coverage['no_comparison_reason'] == 'no_comparable_lanes'
    assert coverage['lane_calculation_reasons']['requested_quantity:selected_prices:missing_quantity'] == 1


def test_markdown_includes_readable_quantity_comparison(tmp_path):
    import json
    import subprocess
    import sys
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    path = tmp_path / 'input.json'
    path.write_text(json.dumps([row()]))
    run = subprocess.run([sys.executable, str(root / 'scripts/compare_rr_context.py'),
                          '--input', str(path), '--data-kind', 'synthetic', '--format', 'markdown'],
                         capture_output=True, text=True, cwd=tmp_path,
                         env={'HOME': str(tmp_path), 'PATH': '/usr/bin:/bin'})
    assert run.returncode == 0
    assert '| Signal | Quantity source | Quantity | Selected RR | Current RR |' in run.stdout
    assert '| sig-1 | requested_quantity | 10 |' in run.stdout


def test_snapshot_fee_outside_decimal_range_is_not_treated_as_zero():
    r = row()
    r['metadata']['rr_context']['selection']['fee_snapshot']['sell_tax_rate'] = '0.' + '0' * 28 + '1'
    result = compare([r])['rows'][0]['quantity_lanes']['requested_quantity']['selected_prices']
    assert result['calculation_status'] == 'invalid'
    assert result['calculation_reason'] == 'invalid_fee_config'


def test_cli_empty_actual_and_row_limit(tmp_path):
    import json
    import subprocess
    import sys
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    path = tmp_path / 'input.json'
    def run():
        return subprocess.run([sys.executable, str(root / 'scripts/compare_rr_context.py'),
                               '--input', str(path), '--data-kind', 'actual'], capture_output=True, text=True,
                              env={'HOME': str(tmp_path), 'PATH': '/usr/bin:/bin'}, cwd=tmp_path)
    path.write_text('[]')
    result = run()
    assert result.returncode == 0
    output = json.loads(result.stdout)
    assert output['data_kind'] == 'actual' and output['coverage']['no_comparison_reason'] == 'no_rows'
    path.write_text(json.dumps([None] * 10001))
    result = run()
    assert result.returncode == 2 and not result.stdout and 'invalid_rows' in result.stderr
