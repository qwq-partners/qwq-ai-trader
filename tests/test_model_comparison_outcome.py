"""모델 판정별 수량·응답 뒤 가격의 조건부 증분 계산. 실제 거래 자료를 사용하지 않는다."""
from copy import deepcopy
from decimal import Decimal

import pytest

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
    row['current'].update(verdict='reject', applied_multiplier='0.5', quantity=50)
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
    assert outcome.compare_policy_outcomes(row, fees=fees())['net_delta_krw'] == '0.00'
    for arm in ('current', 'candidate'):
        row[arm]['quantity'] = 0
        row[arm].pop('entry_quote')
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
