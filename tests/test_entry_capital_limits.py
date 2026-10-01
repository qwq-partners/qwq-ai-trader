"""Declared capital guards for price diagnostics; all money/quotes are synthetic."""
from copy import deepcopy
from decimal import Decimal
import hashlib
import json

import pytest

from src.analytics.entry_observation import prepare_input
from src.analytics.entry_price_shadow import build_report
from test_received_entry_shadow import observed_bundle


def capital_policy():
    return {'version': 'pre-pending-guards-v1', 'policy_ref': 'synthetic-capital-policy',
            'fixed_at': '2026-09-30T09:00:00+09:00', 'source_version_ref': 'synthetic-source',
            'configuration_ref': 'synthetic-config', 'max_snapshot_age_seconds': '1',
            'risk_per_trade_pct': '0.7', 'max_position_pct': '18'}


def rehash(order):
    order['capital_snapshot_ref'] = hashlib.sha256(
        json.dumps(order['capital_snapshot'], sort_keys=True).encode()).hexdigest()


def bundle():
    context, observations, inputs = observed_bundle()
    context['capital_policy'] = capital_policy()
    order = observations['records'][3]
    order.update(order_id='synthetic-order', order_type='market', order_reference_price='10000',
                 capital_snapshot_status='recorded')
    order['capital_snapshot'] = {
        'version': 'pre-pending-capacity-v1', 'basis': 'engine_memory_not_broker_balance',
        'stage': 'before_current_pending_registration', 'captured_at': '2026-09-30T09:59:59.5+09:00',
        'signal_id': 'sig', 'order_id': 'synthetic-order', 'symbol': 'SYNTH1', 'strategy': 'gap_and_go',
        'requested_quantity': 100, 'reference_price': '10000',
        'current_order_reservation_included': False, 'equity': '10000000', 'cash': '3300000',
        'cash_after_reserve': '2800000', 'prior_pending_cash_reserved': '100000',
        'core_cash_reserved': '100000', 'cash_capacity_before': '2600000',
        'strategy_allocation_pct': '15', 'strategy_held_notional': '100000',
        'strategy_pending_reserved': '100000', 'strategy_cap_notional': '1500000',
        'strategy_remaining_notional': '1300000',
    }
    rehash(order)
    inputs[0].pop('capital_budget'); inputs[0].pop('capital_budget_ref')
    inputs[0]['outcome'] = {'basis': 'common_policy_exit_proxy', 'exit_policy_ref': 'synthetic-fixed-net5',
                          'legs': [{'at': '2026-09-30T11:00:00+09:00', 'price': '9500', 'quantity': 100}]}
    return context, observations, inputs


def calculate(args):
    return prepare_input(*args)['report']


def set_cash(args, capacity):
    order = args[1]['records'][3]; s = order['capital_snapshot']
    s['cash_capacity_before'] = str(capacity)
    s['cash_after_reserve'] = str(Decimal(capacity) + 200000)
    s['cash'] = str(Decimal(capacity) + 700000)
    rehash(order)


def test_observed_components_replace_manual_budget_only_with_explicit_policy():
    args = bundle(); before = deepcopy(args)
    report = calculate(args); row = report['opportunities'][0]
    assert row['gate_status'] == 'allow'
    assert row['a_quantity'] == row['b_quantity'] == 100
    assert Decimal(row['a_net_pnl']) == -50000
    assert row['capital_basis'] == 'pre_pending_internal_guard_proxy'
    assert row['max_ask_whole_krw'] == '10232'
    assert report['counts']['unknown'] == 1  # Original missing candidate stays in the denominator.
    assert report['summary']['complete_delta_net_pnl'] is None
    assert report['production_eligible'] is False and report['account_return'] is None
    assert args == before


@pytest.mark.parametrize('constraint', ['market', 'cash_cost', 'strategy', 'position', 'risk'])
def test_common_quantity_over_any_guard_is_unknown_not_avoided_loss(constraint):
    args = bundle(); context, observations, _ = args
    order = observations['records'][3]; s = order['capital_snapshot']
    if constraint == 'market': set_cash(args, '1299999')
    elif constraint == 'cash_cost':
        set_cash(args, '1300000'); context['fees']['buy_commission_rate'] = '0.4'
    elif constraint == 'strategy':
        s.update(strategy_held_notional='400001', strategy_remaining_notional='999999'); rehash(order)
    elif constraint == 'position': context['capital_policy']['max_position_pct'] = '9'
    else: context['capital_policy']['risk_per_trade_pct'] = '0.4'
    report = calculate(args); row = report['opportunities'][0]
    assert row['gate_status'] == 'unknown'
    assert row['reason_codes'] == ['CAPITAL_CONSTRAINT_FAILED:' + constraint]
    assert row['a_net_pnl'] is None and row['b_net_pnl'] is None
    assert row['max_ask_whole_krw'] is None
    assert report['summary']['known_avoided_loss'] is None


@pytest.mark.parametrize('constraint', ['market', 'strategy', 'position', 'risk'])
def test_price_cap_search_uses_the_same_capital_guard(constraint):
    args = bundle(); context, observations, _ = args
    order = observations['records'][3]; s = order['capital_snapshot']
    if constraint == 'market': set_cash(args, '1300000')
    elif constraint == 'strategy':
        s.update(strategy_held_notional='400000', strategy_remaining_notional='1000000'); rehash(order)
    elif constraint == 'position': context['capital_policy']['max_position_pct'] = '10'
    else: context['capital_policy']['risk_per_trade_pct'] = '0.5'
    row = calculate(args)['opportunities'][0]
    assert row['gate_status'] == 'allow' and row['max_ask_whole_krw'] == '10000'
    observations['records'][4]['ask'] = '10001'
    assert calculate(args)['opportunities'][0]['gate_status'] == 'unknown'


def test_risk_guard_counts_rounded_buy_fee_and_keeps_sizing_stop_separate():
    args = bundle(); context, observations, _ = args
    context['fees']['buy_commission_rate'] = '0.000140527'
    context['capital_policy']['risk_per_trade_pct'] = '0.5000705'  # Exactly 50,007.05 KRW.
    order = observations['records'][3]; order['effective_stop_pct'] = '2.5'
    row = calculate(args)['opportunities'][0]
    assert row['gate_status'] == 'allow' and row['max_ask_whole_krw'] == '10000'
    assert Decimal(row['planned_net_loss']) == Decimal('25003.525')
    observations['records'][4]['ask'] = '10001'
    assert calculate(args)['opportunities'][0]['reason_codes'] == ['CAPITAL_CONSTRAINT_FAILED:risk']


@pytest.mark.parametrize('defect', ['missing', 'hash', 'identity', 'quantity', 'price', 'cash_math',
    'strategy_math', 'future', 'stale', 'own_reserved', 'extra', 'nonfinite', 'boolean', 'late_policy'])
def test_bad_capital_evidence_never_falls_back_to_total_equity(defect):
    args = bundle(); context, observations, _ = args
    order = observations['records'][3]; s = order['capital_snapshot']
    if defect == 'missing': order['capital_snapshot'] = None
    elif defect == 'hash': order['capital_snapshot_ref'] = '0' * 64
    elif defect == 'identity': s['order_id'] = 'different-order'
    elif defect == 'quantity': s['requested_quantity'] = 99
    elif defect == 'price': s['reference_price'] = '9999'
    elif defect == 'cash_math': s['cash_capacity_before'] = '99999999'
    elif defect == 'strategy_math': s['strategy_remaining_notional'] = '99999999'
    elif defect == 'future': s['captured_at'] = '2026-09-30T10:00:01+09:00'
    elif defect == 'stale': s['captured_at'] = '2026-09-30T09:59:56+09:00'
    elif defect == 'own_reserved': s['current_order_reservation_included'] = True
    elif defect == 'extra': s['account_number'] = 'synthetic-private'
    elif defect == 'nonfinite': s['equity'] = 'NaN'
    elif defect == 'boolean': s['cash'] = True
    else: context['capital_policy']['fixed_at'] = '2026-09-30T09:59:56+09:00'
    if defect not in ('missing', 'hash'): rehash(order)
    report = calculate(args); row = report['opportunities'][0]
    assert row['gate_status'] == 'unknown'
    assert row['a_net_pnl'] is None and row['b_net_pnl'] is None
    assert report['summary']['complete_delta_net_pnl'] is None


def test_explicit_budget_cannot_override_snapshot_mode():
    args = bundle(); args[2][0]['capital_budget'] = '99999999'
    with pytest.raises(ValueError): calculate(args)
    args = bundle(); args[0].pop('capital_policy')
    # Snapshot existence alone never enables the new mode.
    assert calculate(args)['opportunities'][0]['gate_status'] == 'unknown'


@pytest.mark.parametrize('defect', ['version', 'extra', 'mode', 'bad_risk', 'bad_position'])
def test_bad_or_mixed_policy_is_rejected_before_rows(defect):
    args = bundle(); policy = args[0]['capital_policy']
    if defect == 'version': policy['version'] = 'future'
    elif defect == 'extra': policy['auto_budget'] = True
    elif defect == 'mode': args[0]['data_basis'] = 'market_timestamp'
    elif defect == 'bad_risk': policy['risk_per_trade_pct'] = '0'
    else: policy['max_position_pct'] = '101'
    with pytest.raises(ValueError): calculate(args)


def test_uncapped_strategy_is_explicit_and_negative_capacity_stays_unknown():
    args = bundle(); order = args[1]['records'][3]
    order['capital_snapshot'].update(strategy_allocation_pct='0', strategy_cap_notional=None,
                                     strategy_remaining_notional=None)
    rehash(order)
    row = calculate(args)['opportunities'][0]
    assert row['gate_status'] == 'allow'
    assert row['capital_constraints']['strategy_cap_status'] == 'not_applicable'
    set_cash(args, '-1')
    row = calculate(args)['opportunities'][0]
    assert row['reason_codes'] == ['CAPITAL_CONSTRAINT_FAILED:cash_cost']
    assert row['capital_constraints']['cash_cost_capacity'] == '-1'


def test_slippage_is_included_in_each_candidate_price_and_market_cap():
    args = bundle(); set_cash(args, '1301300')
    args[0]['policy']['entry_slippage_bps'] = '10'
    row = calculate(args)['opportunities'][0]
    assert row['gate_status'] == 'allow' and row['max_ask_whole_krw'] == '10000'
    assert Decimal(row['entry_price_proxy']) == 10010
    args[1]['records'][4]['ask'] = '10001'
    assert calculate(args)['opportunities'][0]['reason_codes'] == ['CAPITAL_CONSTRAINT_FAILED:market']


def test_v1_float_percentage_snapshot_arithmetic_is_preserved():
    args = bundle(); order = args[1]['records'][3]
    order['capital_snapshot'].update(strategy_allocation_pct='29.1',
        strategy_cap_notional='2910000.00000000040000000',
        strategy_remaining_notional='2710000.00000000040000000')
    rehash(order)
    assert calculate(args)['opportunities'][0]['gate_status'] == 'allow'
    # An exact-decimal recomputation would change this v1 producer contract.
    order['capital_snapshot'].update(strategy_cap_notional='2910000', strategy_remaining_notional='2710000')
    rehash(order)
    assert calculate(args)['opportunities'][0]['reason_codes'] == ['CAPITAL_STRATEGY_ARITHMETIC_MISMATCH']


@pytest.mark.parametrize('defect', ['manual_override', 'implicit_mode', 'baseline_cash'])
def test_direct_payload_cannot_bypass_mode_or_turn_missing_order_into_cash(defect):
    p = prepare_input(*bundle())['payload']
    if defect == 'manual_override': p['opportunities'][0]['capital_budget'] = '99999999'
    elif defect == 'implicit_mode': p.pop('capital_policy')
    else:
        p['opportunities'][0].update(baseline_eligible=False, baseline_basis='order_free_policy')
        row = build_report(p)['opportunities'][0]
        assert row['gate_status'] == 'unknown' and row['b_net_pnl'] is None
        return
    with pytest.raises(ValueError): build_report(p)


@pytest.mark.parametrize('defect', ['none', 'future', 'source', 'configuration'])
def test_capture_requires_frozen_and_matching_capital_policy(tmp_path, defect):
    from datetime import timedelta
    from test_entry_observation_runtime import fixture_plan, NOW
    from src.analytics.entry_observation_runtime import CapturePlan
    def change(c):
        c['capital_policy'] = capital_policy()
        if defect == 'future': c['capital_policy']['fixed_at'] = (NOW + timedelta(seconds=1)).isoformat()
        elif defect in ('source', 'configuration'):
            key = 'source_version_ref' if defect == 'source' else 'configuration_ref'
            c['capital_policy'][key] = 'different'
    path, context = fixture_plan(tmp_path, change)
    if defect == 'none':
        assert CapturePlan.load(path, now=NOW).context == context
    else:
        with pytest.raises(ValueError): CapturePlan.load(path, now=NOW)
    assert not list(tmp_path.glob('*.jsonl'))


@pytest.mark.asyncio
async def test_capital_and_fixed_bid_markout_roundtrip_through_journal_cli(tmp_path, capsys):
    from test_entry_markout import markout_bundle
    from src.analytics.entry_observation import EntryObservationBuffer
    from src.analytics.entry_observation_journal import ObservationJournal
    from scripts.compare_entry_price_shadow import main
    c, o, inputs = markout_bundle()
    c['capital_policy'] = capital_policy()
    o['records'][3] = bundle()[1]['records'][3]
    inputs[0].pop('capital_budget'); inputs[0].pop('capital_budget_ref')
    study = tmp_path / 'study.json'; study.write_text(json.dumps(c))
    supplement = tmp_path / 'inputs.json'; supplement.write_text(json.dumps(inputs))
    path = tmp_path / 'capture.jsonl'
    b = EntryObservationBuffer(evaluation_epoch=c['evaluation_epoch'], capacity=100)
    j = await ObservationJournal.open(b, path, study_ref='synthetic-capital',
        study_sha256=hashlib.sha256(study.read_bytes()).hexdigest(), queue_capacity=20,
        batch_size=5, max_bytes=100000, max_record_bytes=10000)
    for record in o['records']: assert b.publish(record)
    assert (await j.close())['sealed']
    before = {p: p.read_bytes() for p in (path, study, supplement)}
    assert main(['--journal', str(path), '--study', str(study), '--evaluation-inputs', str(supplement),
                 '--max-journal-bytes', '100000']) == 0
    output = json.loads(capsys.readouterr().out)
    assert output['report'] == prepare_input(c, o, inputs)['report']
    row = output['report']['opportunities'][0]
    assert row['capital_basis'] == 'pre_pending_internal_guard_proxy'
    assert row['gate_status'] == 'cash' and row['outcome_status'] == 'complete_markout_proxy'
    assert Decimal(row['delta_net_pnl']) == 50000
    assert all(p.read_bytes() == raw for p, raw in before.items())
