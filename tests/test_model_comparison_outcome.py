"""모델 판정별 수량·응답 뒤 가격의 조건부 증분 계산. 실제 거래 자료를 사용하지 않는다."""
from copy import deepcopy
from decimal import Decimal

import pytest

from src.analytics import model_comparison_admission as adm
from src.analytics import model_comparison_outcome as outcome
from src.utils.fee_calculator import FeeCalculator, FeeConfig


def sample():
    arm = {
        'verdict': 'approve', 'applied_multiplier': '1', 'quantity': 100,
        'sizing_snapshot_ref': 'predecision-1', 'sizing_replay_ref': 'replay-1',
        'quantity_basis': 'policy_replay', 'api_cost_usd': '0.01',
        'decision_available_at': '2026-10-15T01:00:01+00:00',
        'entry_quote': {'received_at': '2026-10-15T01:00:02+00:00', 'ask': '1000',
                        'bid': '999', 'ask_size': 1000, 'evidence_ref': 'quote-1',
                        'market_state_ref': 'state-1', 'session': 'KRX_REGULAR_CONTINUOUS',
                        'selection_rule': 'first_received_after_decision'},
    }
    row = {'decision_id': 'd1', 'pricing_basis': 'paired_live_shadow',
           'requested_at': '2026-10-15T01:00:00+00:00',
           'evaluation_at': '2026-10-15T01:15:00+00:00',
           'sizing_snapshot': {'ref': 'predecision-1', 'available_at': '2026-10-15T00:59:59+00:00'},
           'policy': {'ref': 'policy-1', 'fixed_at': '2026-10-15T00:00:00+00:00',
                      'fee_schedule_ref': 'fee-1', 'reject_multiplier': '0.5',
                      'evaluation_anchor': 'requested_at', 'horizon_seconds': 900,
                      'unavailable_multiplier': '1', 'max_quote_delay_seconds': '5',
                      'entry_slippage_bps': '0', 'exit_slippage_bps': '0'},
           'exit_quote': {'received_at': '2026-10-15T01:15:01+00:00', 'bid': '1100',
                          'ask': '1101', 'bid_size': 1000, 'evidence_ref': 'exit-1',
                          'market_state_ref': 'exit-state', 'session': 'KRX_REGULAR_CONTINUOUS',
                          'selection_rule': 'first_received_at_or_after_evaluation'},
           'fx': {'krw_per_usd': '1300', 'evidence_ref': 'fx-1',
                  'available_at': '2026-10-15T00:00:00+00:00'},
           'current': deepcopy(arm), 'candidate': deepcopy(arm)}
    row['current'].update(verdict='reject', applied_multiplier='0.5', quantity=50,
                          sizing_replay_ref='replay-c')
    row['admission'] = {name: admitted(row[name]) for name in ('current', 'candidate')}
    row['market_state'] = {'current': normal(row['current']['entry_quote']['received_at']),
                           'candidate': normal(row['candidate']['entry_quote']['received_at']),
                           'exit': normal(row['exit_quote']['received_at'])}
    return row


def admitted(arm, status='passed', anchor_at='2026-10-15T01:00:00+00:00'):
    gates = {name: {'status': status, 'as_of': '2026-10-15T00:59:59+00:00', 'reason': 'synthetic',
                    'replayable': True} for name in (*adm.REPLAYABLE_GATES, *adm.EXTERNAL_GATES)}
    return {'schema_version': adm.ADMISSION_SCHEMA, 'gates': gates,
            'admissible': {'passed': True, 'failed': False}.get(status),
            'snapshot_id': arm['sizing_snapshot_ref'], 'anchor_at': anchor_at,
            'replay_id': arm['sizing_replay_ref'], 'verdict': arm['verdict'], 'quantity': arm['quantity']}


def normal(at, verdict='normal_continuous'):
    return {'schema_version': adm.MARKET_STATE_SCHEMA, 'verdict': verdict, 'basis': ['synthetic'],
            'source_refs': [{'tr_id': 'H0STCNT0'}], 'evaluated_at': at}


def live_v2():
    row = sample()
    row.update(schema_version='model-comparison-pair-v2', live_provenance='in_process_attested',
               input_available_at='2026-10-15T00:59:59.500000+00:00', evaluation_anchor='input_available_at',
               evaluation_at='2026-10-15T01:14:59.500000+00:00')
    row['policy']['evaluation_anchor'] = 'input_available_at'
    row['sizing_snapshot']['available_at'] = '2026-10-15T00:59:59.400000+00:00'
    row['exit_quote']['received_at'] = '2026-10-15T01:15:00+00:00'
    row['market_state']['exit'] = normal('2026-10-15T01:15:00+00:00')
    for name in ('current', 'candidate'):
        row['admission'][name]['anchor_at'] = row['input_available_at']
    return row


def fees():
    return FeeCalculator(FeeConfig(Decimal('0'), Decimal('0'), Decimal('0')))


def test_soft_reject_delta_is_fifty_shares_not_whole_trade():
    result = outcome.compare_policy_outcomes(sample(), fees=fees())
    assert result['status'] == 'conditional_price_proxy'
    assert result['current']['trade_pnl_krw'] == '5000'
    assert result['candidate']['trade_pnl_krw'] == '10000'
    assert result['net_delta_krw'] == '5000.00'
    assert result['actual_execution'] is False


def test_same_quantity_and_zero_quantity_cash_are_preserved():
    row = sample()
    row['current'] = deepcopy(row['candidate'])
    row['admission']['current'] = deepcopy(row['admission']['candidate'])
    row['market_state']['current'] = deepcopy(row['market_state']['candidate'])
    assert outcome.compare_policy_outcomes(row, fees=fees())['net_delta_krw'] == '0.00'
    for arm in ('current', 'candidate'):
        row[arm]['quantity'] = 0
        row[arm].pop('entry_quote')
        row['admission'][arm]['quantity'] = 0
        row['admission'][arm]['gates']['sizing_quantity']['status'] = 'failed'
        row['admission'][arm]['admissible'] = False
        row['market_state'].pop(arm)
    row['market_state'].pop('exit')
    result = outcome.compare_policy_outcomes(row, fees=fees())
    assert result['current']['trade_pnl_krw'] == '0'
    assert result['current']['net_proxy_krw'] == '-13.00'


@pytest.mark.parametrize('mutation', [
    lambda r: r.pop('sizing_snapshot'),
    lambda r: r['current'].update(quantity_basis='post_decision_size_trace'),
    lambda r: r['candidate'].update(sizing_snapshot_ref='other'),
    lambda r: r['current'].update(applied_multiplier='0'),
    lambda r: r['candidate'].update(quantity=True),
    lambda r: r['candidate'].update(api_cost_usd=None),
    lambda r: r.pop('fx'),
    lambda r: r['fx'].update(krw_per_usd='NaN'),
    lambda r: r['candidate']['entry_quote'].update(received_at='2026-10-15T01:00:00+00:00'),
    lambda r: r['candidate']['entry_quote'].update(received_at='2026-10-15T01:00:20+00:00'),
    lambda r: r['candidate']['entry_quote'].pop('market_state_ref'),
    lambda r: r['candidate']['entry_quote'].update(ask_size=1),
    lambda r: r['exit_quote'].update(received_at='2026-10-15T01:14:59+00:00'),
    lambda r: r.update(evaluation_at='2026-10-15T01:16:00+00:00'),
    lambda r: r['policy'].update(evaluation_anchor='entry_quote_at'),
    lambda r: r['policy'].update(horizon_seconds=True),
    lambda r: r['sizing_snapshot'].update(available_at='2026-10-15T01:00:01+00:00'),
    lambda r: r.update(pricing_basis='offline_replay_latency'),
])
def test_missing_or_incompatible_evidence_stays_in_denominator_with_null(mutation):
    row = sample()
    mutation(row)
    result = outcome.compare_policy_outcomes(row, fees=fees())
    assert result['decision_id'] == 'd1'
    assert result['status'] == 'not_evaluable'
    assert result['net_delta_krw'] is None
    assert result['reasons']


def test_fees_are_required_and_real_calculator_is_used_per_arm():
    assert outcome.compare_policy_outcomes(sample(), fees=None)['net_delta_krw'] is None
    calculator = FeeCalculator(FeeConfig(Decimal('0.001'), Decimal('0.002'), Decimal('0')))
    result = outcome.compare_policy_outcomes(sample(), fees=calculator)
    a = calculator.calculate_net_pnl(Decimal('1000'), Decimal('1100'), 50)[0]
    b = calculator.calculate_net_pnl(Decimal('1000'), Decimal('1100'), 100)[0]
    assert Decimal(result['net_delta_krw']) == b - a


def test_each_response_uses_own_later_entry_price_and_api_cost_currency():
    row = sample()
    row['candidate']['decision_available_at'] = '2026-10-15T01:00:03+00:00'
    row['candidate']['entry_quote'].update(received_at='2026-10-15T01:00:04+00:00', ask='1020')
    row['candidate']['api_cost_usd'] = '0.02'
    row['market_state']['candidate'] = normal('2026-10-15T01:00:04+00:00')
    result = outcome.compare_policy_outcomes(row, fees=fees())
    assert result['net_delta_krw'] == '2987.00'


def test_inputs_unchanged_and_zero_api_spend_needs_no_conversion():
    row = sample()
    row.pop('fx')
    for arm in ('current', 'candidate'):
        row[arm]['api_cost_usd'] = '0'
    before = deepcopy(row)
    assert outcome.compare_policy_outcomes(row, fees=fees())['net_delta_krw'] == '5000'
    assert row == before


@pytest.mark.parametrize('rate', [0.001, '0.001'])
def test_fee_rates_are_normalized_without_mutating_callers_config(rate):
    calculator = FeeCalculator(FeeConfig(rate, Decimal('0'), Decimal('0')))
    result = outcome.compare_policy_outcomes(sample(), fees=calculator)
    assert result['status'] == 'conditional_price_proxy'
    assert Decimal(result['net_delta_krw']) == Decimal('4950')
    assert calculator.config.buy_commission_rate == rate


def test_missing_fee_config_and_date_overflow_preserve_null_row():
    calculator = fees()
    calculator.config = None
    result = outcome.compare_policy_outcomes(sample(), fees=calculator)
    assert result['status'] == 'not_evaluable' and result['net_delta_krw'] is None
    row = sample()
    row.update(requested_at='9999-12-31T23:59:58Z', evaluation_at='9999-12-31T23:59:59Z')
    result = outcome.compare_policy_outcomes(row, fees=fees())
    assert result['decision_id'] == 'd1'
    assert result['status'] == 'not_evaluable' and result['net_delta_krw'] is None


def test_input_available_anchor_uses_common_input_time_plus_horizon():
    result = outcome.compare_policy_outcomes(live_v2(), fees=fees())
    assert result['status'] == 'conditional_price_proxy', result['reasons']
    assert result['evaluation_anchor'] == 'input_available_at'
    assert result['evaluation_at'] == '2026-10-15T01:14:59.500000+00:00'
    assert result['net_delta_krw'] == '5000.00'
    assert any('not_fill_vwap_or_slippage_certification' in a for a in result['assumptions'])


@pytest.mark.parametrize('mutation,reason', [
    (lambda r: r.pop('input_available_at'), 'INVALID_TIME:input_available_at'),
    (lambda r: r.update(live_provenance='caller_supplied_not_attested'), 'LIVE_PROVENANCE_NOT_ATTESTED'),
    (lambda r: r.update(evaluation_anchor='requested_at'), 'EVALUATION_ANCHOR_MISMATCH'),
    (lambda r: r.update(evaluation_at='2026-10-15T01:15:00+00:00'), 'COMMON_EVALUATION_POLICY_MISMATCH'),
    (lambda r: r['sizing_snapshot'].update(available_at='2026-10-15T00:59:59.600000+00:00'),
     'SIZING_SNAPSHOT_NOT_AVAILABLE_AT_ANCHOR'),
    (lambda r: r['current'].update(decision_available_at='2026-10-15T00:59:59.400000+00:00'), 'DECISION_TIME:current'),
])
def test_input_anchor_requires_attested_input_and_its_own_times(mutation, reason):
    row = live_v2()
    mutation(row)
    result = outcome.compare_policy_outcomes(row, fees=fees())
    assert result['net_delta_krw'] is None and result['status'] == 'not_evaluable'
    assert reason in result['reasons']


@pytest.mark.parametrize('mutation,arm,reason', [
    (lambda r: r.pop('admission'), 'current', 'ADMISSION_MISSING'),
    (lambda r: r['admission'].update(candidate=admitted(r['candidate'], 'unknown')), 'candidate',
     'ADMISSION_NOT_PASSED'),
    (lambda r: r['admission']['current']['gates']['kill_switch'].update(status='unknown'), 'current',
     'ADMISSION_GATE_NOT_PASSED:kill_switch'),
    (lambda r: r['admission']['current'].update(replay_id='other'), 'current', 'ADMISSION_REPLAY_MISMATCH'),
    (lambda r: r['admission']['candidate'].update(quantity=1), 'candidate', 'ADMISSION_REPLAY_MISMATCH'),
    (lambda r: r['market_state'].update(candidate=normal('2026-10-15T01:00:02+00:00', 'unknown')), 'candidate',
     'MARKET_STATE_NOT_NORMAL:unknown'),
    (lambda r: r['market_state'].update(current=normal('2026-10-15T01:00:03+00:00')), 'current',
     'MARKET_STATE_TIME_MISMATCH'),
    (lambda r: r['market_state'].pop('exit'), 'exit', 'MARKET_STATE_MISSING'),
    (lambda r: r['market_state'].update(exit=normal('2026-10-15T01:15:01+00:00', 'not_normal')), 'exit',
     'MARKET_STATE_NOT_NORMAL:not_normal'),
])
def test_admission_and_market_state_gate_each_arm(mutation, arm, reason):
    row = sample()
    mutation(row)
    result = outcome.compare_policy_outcomes(row, fees=fees())
    assert result['net_delta_krw'] is None and result['status'] == 'not_evaluable'
    assert reason in result['arm_reasons'][arm]
    assert f'{arm}:{reason}' in result['reasons']


def test_caller_true_flag_without_passed_gates_is_not_an_override():
    row = sample()
    row['admission']['current'] = admitted(row['current'], 'unknown')
    row['admission']['current']['admissible'] = True        # 일괄 True 덮어쓰기
    assert outcome.compare_policy_outcomes(row, fees=fees())['net_delta_krw'] is None


def test_legacy_requested_anchor_is_labelled():
    result = outcome.compare_policy_outcomes(sample(), fees=fees())
    assert result['evaluation_anchor'] == 'requested_at'
    assert 'legacy_requested_at_anchor_is_worker_dispatch_time' in result['assumptions']


def _set_gate_time(row, arm, value):
    for gate in row['admission'][arm]['gates'].values():
        gate['as_of'] = value


@pytest.mark.parametrize('build,mutation,arm,reason', [
    (sample, lambda r: r['admission']['current'].update(snapshot_id='other-snapshot'), 'current',
     'ADMISSION_SNAPSHOT_MISMATCH'),
    (sample, lambda r: r['admission']['candidate'].pop('snapshot_id'), 'candidate', 'ADMISSION_SNAPSHOT_MISMATCH'),
    (sample, lambda r: r['admission']['candidate'].update(anchor_at='2026-10-15T00:59:00+00:00'), 'candidate',
     'ADMISSION_ANCHOR_MISMATCH'),
    (sample, lambda r: r['admission']['candidate'].update(anchor_at=None), 'candidate', 'ADMISSION_ANCHOR_MISMATCH'),
    (live_v2, lambda r: r['admission']['current'].update(anchor_at=r['requested_at']), 'current',
     'ADMISSION_ANCHOR_MISMATCH'),
    (sample, lambda r: _set_gate_time(r, 'candidate', '2026-10-16T00:59:59+00:00'), 'candidate',
     'ADMISSION_GATE_FUTURE:cash_precheck'),
    (sample, lambda r: r['admission']['current']['gates']['kill_switch'].update(as_of='2026-10-15T01:00:01+00:00'),
     'current', 'ADMISSION_GATE_FUTURE:kill_switch'),
    (sample, lambda r: r['admission']['current']['gates']['engine_cash'].update(as_of='2026-10-15T00:59:59'),
     'current', 'ADMISSION_GATE_TIME_INVALID:engine_cash'),
    (sample, lambda r: r['admission']['current']['gates']['engine_cash'].update(as_of=None),
     'current', 'ADMISSION_GATE_TIME_INVALID:engine_cash'),
])
def test_admission_from_other_snapshot_anchor_or_future_is_not_evaluable(build, mutation, arm, reason):
    row = build()
    mutation(row)
    result = outcome.compare_policy_outcomes(row, fees=fees())
    assert result['net_delta_krw'] is None and result['status'] == 'not_evaluable'
    assert reason in result['arm_reasons'][arm]


def test_same_instant_in_other_offset_is_the_same_anchor():
    row = sample()
    row['admission']['candidate']['anchor_at'] = '2026-10-15T10:00:00+09:00'
    assert outcome.compare_policy_outcomes(row, fees=fees())['net_delta_krw'] == '5000.00'


def test_zero_quantity_arm_still_requires_same_snapshot_and_anchor():
    row = sample()
    row['current']['quantity'] = 0
    row['current'].pop('entry_quote')
    row['admission']['current'].update(quantity=0, admissible=False, snapshot_id='other')
    row['admission']['current']['gates']['sizing_quantity']['status'] = 'failed'
    row['market_state'].pop('current')
    result = outcome.compare_policy_outcomes(row, fees=fees())
    assert result['net_delta_krw'] is None
    assert 'ADMISSION_SNAPSHOT_MISMATCH' in result['arm_reasons']['current']
