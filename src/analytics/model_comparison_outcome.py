"""판정별 정책 재생 수량으로 계산하는 조건부 가격 손익 차이.

네트워크·주문·사이징 재생·원장 인증·호가 선택을 수행하지 않는다. 호출자가 별도로
검증한 재생/원천 참조를 받아 일관성을 검사한다. 참조 문자열만으로 원본의 진실성이나
체결을 인증하지 않는다. 판단 후 수량을 역산하거나 오프라인 응답 시간을 과거 진입
시각으로 쓰는 입력은 받지 않는다. 실자료 연결 전의 순수 계산 경계다.

v2(2026-10-13): 평가 기준시각은 policy.evaluation_anchor(requested_at=legacy 작업자 dispatch,
input_available_at=공통 입력 구성 시각)이며, arm 별 진입 조건 재생(model_comparison_admission.
replay_admission)과 장 상태 분류(classify_market_state)가 모두 통과한 arm 만 계산한다.
"""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal, DecimalException
from typing import Mapping

from .entry_price_shadow import _mapping, _number, _text, _timestamp
from .model_comparison_admission import (ADMISSION_SCHEMA, EXTERNAL_GATES, MARKET_STATE_SCHEMA,
                                         REPLAYABLE_GATES)
from ..utils.fee_calculator import FeeCalculator, FeeConfig


def _quantity(value, name):
    if type(value) is not int or value < 0:
        raise ValueError(f'INVALID:{name}')
    return value


def _quote(value, *, name, anchor, last_entry_at, max_delay, quantity, entry):
    quote = _mapping(value, name)
    _text(quote.get('evidence_ref'), f'{name}.evidence_ref')
    _text(quote.get('market_state_ref'), f'{name}.market_state_ref')
    expected = 'first_received_after_decision' if entry else 'first_received_at_or_after_evaluation'
    if (quote.get('selection_rule') != expected
            or quote.get('session') != 'KRX_REGULAR_CONTINUOUS'):
        raise ValueError(f'UNSUPPORTED_QUOTE_BASIS:{name}')
    received = _timestamp(quote.get('received_at'), f'{name}.received_at')
    if ((entry and not anchor < received < last_entry_at)
            or (not entry and received < anchor)
            or Decimal(str((received - anchor).total_seconds())) > max_delay):
        raise ValueError(f'QUOTE_TIME_OR_DELAY:{name}')
    ask = _number(quote.get('ask'), f'{name}.ask')
    bid = _number(quote.get('bid'), f'{name}.bid')
    size_key = 'ask_size' if entry else 'bid_size'
    if bid > ask or _quantity(quote.get(size_key), f'{name}.{size_key}') < quantity:
        raise ValueError(f'QUOTE_CROSSED_OR_INSUFFICIENT_SIZE:{name}')
    return (ask if entry else bid), received


def _arm(name, row, *, requested, evaluation, snapshot_ref, policy, fees):
    arm = _mapping(row.get(name), name)
    if arm.get('quantity_basis') != 'policy_replay' or arm.get('sizing_snapshot_ref') != snapshot_ref:
        raise ValueError(f'PREDECISION_SIZING_REQUIRED:{name}')
    _text(arm.get('sizing_replay_ref'), f'{name}.sizing_replay_ref')
    multipliers = {'approve': Decimal('1'), 'reject': policy['reject_multiplier'],
                   'unavailable': policy['unavailable_multiplier']}
    verdict = arm.get('verdict')
    if not isinstance(verdict, str) or verdict not in multipliers:
        raise ValueError(f'UNSUPPORTED_VERDICT:{name}')
    applied = _number(arm.get('applied_multiplier'), f'{name}.applied_multiplier', zero=True)
    if applied != multipliers[verdict]:
        raise ValueError(f'POLICY_MULTIPLIER_MISMATCH:{name}')
    qty = _quantity(arm.get('quantity'), f'{name}.quantity')
    cost = _number(arm.get('api_cost_usd'), f'{name}.api_cost_usd', zero=True)
    completed = _timestamp(arm.get('decision_available_at'), f'{name}.decision_available_at')
    if not requested <= completed < evaluation:
        raise ValueError(f'DECISION_TIME:{name}')
    trade_pnl = Decimal('0')
    entry_ref = None
    if qty > 0:
        entry, _ = _quote(arm.get('entry_quote'), name=f'{name}.entry_quote', anchor=completed,
                          last_entry_at=evaluation, max_delay=policy['max_quote_delay_seconds'],
                          quantity=qty, entry=True)
        exit_price, _ = _quote(row.get('exit_quote'), name='exit_quote', anchor=evaluation,
                               last_entry_at=evaluation, max_delay=policy['max_quote_delay_seconds'],
                               quantity=qty, entry=False)
        entry *= 1 + policy['entry_slippage_bps'] / 10000
        exit_price *= 1 - policy['exit_slippage_bps'] / 10000
        trade_pnl, _ = fees.calculate_net_pnl(entry, exit_price, qty)
        entry_ref = arm['entry_quote']['evidence_ref']
    return {'verdict': verdict, 'quantity': qty, 'applied_multiplier': str(applied),
            'sizing_replay_ref': arm['sizing_replay_ref'], 'entry_quote_ref': entry_ref,
            'trade_pnl_krw': str(trade_pnl), 'api_cost_usd': str(cost)}


ANCHORS = ('requested_at', 'input_available_at')
ASSUMPTIONS = (
    'quote_size_is_displayed_top_of_book_not_fill_vwap_or_slippage_certification',
    'explicit_slippage_bps_and_fee_schedule_are_assumptions_not_measured_costs',
    'sizing_and_admission_replay_use_pre_llm_snapshot_not_order_time_state',
    'market_state_is_raw_kis_code_reading_not_exchange_certification',
)


def _same_time(value, expected):
    try:
        return _timestamp(value, 'evaluated_at') == expected
    except ValueError:
        return False


def _market_reasons(states, key, quote_at):
    state = states.get(key) if isinstance(states, Mapping) else None
    if not isinstance(state, Mapping) or state.get('schema_version') != MARKET_STATE_SCHEMA:
        return ['MARKET_STATE_MISSING']
    reasons = []
    if state.get('verdict') != 'normal_continuous':
        reasons.append(f"MARKET_STATE_NOT_NORMAL:{state.get('verdict')}")
    if not _same_time(state.get('evaluated_at'), quote_at):
        reasons.append('MARKET_STATE_TIME_MISMATCH')
    return reasons


def _admission_reasons(row, name, arm):
    """호출자가 붙인 증거의 구조·일관성 검사. 일괄 True 표시는 게이트 통과를 대신하지 않는다."""
    admissions = row.get('admission')
    admission = admissions.get(name) if isinstance(admissions, Mapping) else None
    if not isinstance(admission, Mapping) or admission.get('schema_version') != ADMISSION_SCHEMA:
        return ['ADMISSION_MISSING']
    reasons = []
    if (admission.get('replay_id') != row[name]['sizing_replay_ref']
            or admission.get('quantity') != arm['quantity'] or admission.get('verdict') != arm['verdict']):
        reasons.append('ADMISSION_REPLAY_MISMATCH')
    gates = admission.get('gates') if isinstance(admission.get('gates'), Mapping) else {}
    if arm['quantity'] == 0:
        gate = gates.get('sizing_quantity')
        if not isinstance(gate, Mapping) or gate.get('status') != 'failed':
            reasons.append('ADMISSION_QUANTITY_INCONSISTENT')
        return reasons
    if admission.get('admissible') is not True:
        reasons.append('ADMISSION_NOT_PASSED')
    for gate_name in (*REPLAYABLE_GATES, *EXTERNAL_GATES):
        gate = gates.get(gate_name)
        if not isinstance(gate, Mapping):
            reasons.append(f'ADMISSION_GATE_MISSING:{gate_name}')
        elif gate.get('status') != 'passed':
            reasons.append(f'ADMISSION_GATE_NOT_PASSED:{gate_name}')
    return reasons


def compare_policy_outcomes(row: Mapping, *, fees: FeeCalculator | None) -> dict:
    """같은 선행 스냅샷에서 별도로 검증한 두 정책 재생의 가격 손익 차이.

    누락 행도 decision_id와 사유를 보존한다. 수량은 이 함수가 생성하거나 검증된
    엔진 출력이라고 인증하지 않는다. 조건부 계산 결과를 실현손익으로 쓰면 안 된다.
    """
    result = {'schema_version': 'model-policy-outcome-v2',
              'decision_id': row.get('decision_id') if isinstance(row, Mapping) else None,
              'status': 'not_evaluable', 'actual_execution': False,
              'evidence_validation': 'reference_presence_and_consistency_only',
              'evaluation_anchor': None, 'assumptions': list(ASSUMPTIONS),
              'current': None, 'candidate': None, 'net_delta_krw': None,
              'arm_reasons': {}, 'reasons': []}
    try:
        row = _mapping(row, 'row')
        _text(row.get('decision_id'), 'decision_id')
        if row.get('pricing_basis') != 'paired_live_shadow':
            raise ValueError('PAIRED_LIVE_PRICE_BASIS_REQUIRED')
        if not isinstance(fees, FeeCalculator):
            raise ValueError('EXPLICIT_FEE_CALCULATOR_REQUIRED')
        rates = {}
        for key in ('buy_commission_rate', 'sell_commission_rate', 'sell_tax_rate'):
            rate = _number(getattr(fees.config, key, None), f'fees.{key}', zero=True)
            if rate >= 1:
                raise ValueError(f'INVALID_FEE_RATE:{key}')
            rates[key] = rate
        fees = FeeCalculator(FeeConfig(**rates))
        raw_policy = _mapping(row.get('policy'), 'policy')
        anchor_name = raw_policy.get('evaluation_anchor')
        if anchor_name not in ANCHORS:
            raise ValueError('COMMON_EVALUATION_POLICY_MISMATCH')
        if row.get('evaluation_anchor', 'requested_at') != anchor_name:
            raise ValueError('EVALUATION_ANCHOR_MISMATCH')
        result['evaluation_anchor'] = anchor_name
        if anchor_name == 'requested_at':
            result['assumptions'].append('legacy_requested_at_anchor_is_worker_dispatch_time')
        elif row.get('live_provenance') != 'in_process_attested':
            raise ValueError('LIVE_PROVENANCE_NOT_ATTESTED')
        requested = _timestamp(row.get(anchor_name), anchor_name)
        if anchor_name == 'input_available_at' and row.get('requested_at') is not None:
            if _timestamp(row.get('requested_at'), 'requested_at') < requested:
                raise ValueError('DISPATCH_BEFORE_INPUT')
        evaluation = _timestamp(row.get('evaluation_at'), 'evaluation_at')
        if evaluation <= requested:
            raise ValueError('COMMON_EVALUATION_TIME_REQUIRED')
        snapshot = _mapping(row.get('sizing_snapshot'), 'sizing_snapshot')
        snapshot_ref = _text(snapshot.get('ref'), 'sizing_snapshot.ref')
        if _timestamp(snapshot.get('available_at'), 'sizing_snapshot.available_at') > requested:
            raise ValueError('SIZING_SNAPSHOT_NOT_AVAILABLE_AT_ANCHOR')
        _text(raw_policy.get('ref'), 'policy.ref')
        _text(raw_policy.get('fee_schedule_ref'), 'policy.fee_schedule_ref')
        if _timestamp(raw_policy.get('fixed_at'), 'policy.fixed_at') > requested:
            raise ValueError('POLICY_NOT_FIXED_AT_ANCHOR')
        horizon = raw_policy.get('horizon_seconds')
        if (type(horizon) is not int or not 0 < horizon <= 86400
                or evaluation - requested != timedelta(seconds=horizon)):
            raise ValueError('COMMON_EVALUATION_POLICY_MISMATCH')
        keys = ('reject_multiplier', 'unavailable_multiplier', 'max_quote_delay_seconds',
                'entry_slippage_bps', 'exit_slippage_bps')
        policy = {key: _number(raw_policy.get(key), f'policy.{key}', zero=True) for key in keys}
        if (policy['reject_multiplier'] > 1 or policy['unavailable_multiplier'] > 1
                or policy['entry_slippage_bps'] >= 10000 or policy['exit_slippage_bps'] >= 10000):
            raise ValueError('POLICY_RANGE')
        arms = {name: _arm(name, row, requested=requested, evaluation=evaluation,
                          snapshot_ref=snapshot_ref, policy=policy, fees=fees)
                for name in ('current', 'candidate')}
        arm_reasons = {name: _admission_reasons(row, name, arm) for name, arm in arms.items()}
        for name, arm in arms.items():
            if arm['quantity'] > 0:
                quote_at = _timestamp(row[name]['entry_quote']['received_at'], 'entry_quote.received_at')
                arm_reasons[name] += _market_reasons(row.get('market_state'), name, quote_at)
        if any(arm['quantity'] > 0 for arm in arms.values()):
            exit_at = _timestamp(row['exit_quote']['received_at'], 'exit_quote.received_at')
            arm_reasons['exit'] = _market_reasons(row.get('market_state'), 'exit', exit_at)
        arm_reasons = {name: reasons for name, reasons in arm_reasons.items() if reasons}
        if arm_reasons:
            result['arm_reasons'] = arm_reasons
            result['reasons'].extend(f'{name}:{reason}' for name, reasons in arm_reasons.items()
                                     for reason in reasons)
            return result
        fx = Decimal('0')
        if any(Decimal(arm['api_cost_usd']) > 0 for arm in arms.values()):
            raw_fx = _mapping(row.get('fx'), 'fx')
            _text(raw_fx.get('evidence_ref'), 'fx.evidence_ref')
            fx = _number(raw_fx.get('krw_per_usd'), 'fx.krw_per_usd')
            if _timestamp(raw_fx.get('available_at'), 'fx.available_at') > requested:
                raise ValueError('FX_NOT_AVAILABLE_AT_ANCHOR')
        for arm in arms.values():
            arm['net_proxy_krw'] = str(Decimal(arm['trade_pnl_krw']) - Decimal(arm['api_cost_usd']) * fx)
        result.update(arms, status='conditional_price_proxy',
                      net_delta_krw=str(Decimal(arms['candidate']['net_proxy_krw'])
                                        - Decimal(arms['current']['net_proxy_krw'])),
                      sizing_snapshot_ref=snapshot_ref, policy_ref=raw_policy['ref'],
                      evaluation_at=evaluation.isoformat())
    except (ValueError, DecimalException) as exc:
        result['reasons'].append(str(exc))
    return result
