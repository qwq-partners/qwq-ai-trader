"""최초 SEPA RR 근거와 실행 신호의 제한된 JSON 스냅샷.

진단 전용: 가격/게이트를 재계산하거나 수량을 만들지 않는다. 후속 프리장 검사,
주문 제출/체결, ExitManager 청산 설정은 이 기록의 근거 범위에 포함되지 않는다.
"""
from datetime import datetime
from decimal import Decimal
from math import isfinite

from .fee_calculator import FeeCalculator, FeeConfig

SOURCE_REF = 'src/strategies/kr/sepa_trend.py:generate_batch_signals'
_RATES = ('buy_commission_rate', 'sell_commission_rate', 'sell_tax_rate')
_DIAGNOSTICS = ('gross_rr', 'fee_adjusted_rr', 'gate_rr', 'round_trip_cost')
_REASONS = {'not_captured', 'invalid_snapshot', 'unsupported_version', 'source_mismatch',
            'identity_mismatch', 'selection_conflict', 'capture_failed', 'diagnostics_unavailable',
            'fee_config_unavailable'}


def _unavailable(reason='invalid_snapshot', *, conflict=False):
    return {'version': 1, 'status': 'conflict' if conflict else 'unavailable', 'reason': reason}


def _text(value):
    if type(value) is not str or len(value) > 256:
        raise ValueError('invalid text')
    return value


def _number(value):
    # 형식을 제한한 뒤 변환: 임의 객체 __str__/직렬화/재귀 순회는 금지한다.
    if type(value) not in (str, int, float, Decimal):
        raise ValueError('invalid number')
    if type(value) is int and abs(value) > 10**128:
        raise ValueError('oversized number')
    if type(value) is Decimal and (not value.is_finite() or len(value.as_tuple().digits) > 128):
        raise ValueError('invalid decimal')
    text = str(value)
    if len(text) > 128 or text != text.strip():
        raise ValueError('oversized number')
    number = Decimal(text)
    if (not number.is_finite() or abs(number.adjusted()) > 128
            or abs(number.as_tuple().exponent) > 128):
        raise ValueError('invalid number')
    return text


def _time(value):
    text = value.isoformat() if isinstance(value, datetime) else _text(value)
    _text(text)
    datetime.fromisoformat(text)
    return text


def _mapping(value, keys):
    if type(value) is not dict or len(value) > len(keys) or not value.keys() <= keys:
        raise ValueError('invalid mapping')
    return value


def _copy_fee(value):
    value = _mapping(value, {'version', 'status', 'reason', *_RATES})
    if type(value.get('version')) is not int or value['version'] != 1:
        raise ValueError('invalid fee version')
    if value.get('status') == 'unavailable':
        return _unavailable('fee_config_unavailable')
    if value.get('status') != 'captured' or value.get('reason') is not None:
        raise ValueError('invalid fee status')
    rates = {key: _number(value[key]) for key in _RATES}
    if any(not 0 <= Decimal(rate) <= 1 for rate in rates.values()):
        raise ValueError('invalid fee rate')
    return {'version': 1, 'status': 'captured', 'reason': None, **rates}


def capture_fee_snapshot(calculator) -> dict:
    """이미 사용한 FeeCalculator/FeeConfig의 비용률만 복사한다. 재조회 없음.

    비율은 계산 규칙·원 단위 반올림의 스냅샷이 아니다. 실제 사용한 비용은
    diagnostics.round_trip_cost가 정본이다. 알 수 없는 대체 구현은 추정하지 않는다.
    """
    try:
        if type(calculator) is not FeeCalculator or type(calculator.config) is not FeeConfig:
            return _unavailable('fee_config_unavailable')
        config = calculator.config
        return _copy_fee({'version': 1, 'status': 'captured', 'reason': None,
                          **{key: getattr(config, key) for key in _RATES}})
    except Exception:
        return _unavailable('fee_config_unavailable')


def copy_rr_selection(value, *, symbol, strategy) -> dict:
    """v1 고정 필드만 검증·복사하여 신호/계획과의 별칭을 제거한다."""
    if value is None:
        return _unavailable('not_captured')
    try:
        value = _mapping(value, {'version', 'status', 'reason', 'source_ref', 'symbol',
                                 'strategy', 'prices', 'captured_at', 'gate', 'diagnostics',
                                 'fee_snapshot'})
        if type(value.get('version')) is not int or value['version'] != 1:
            return _unavailable('unsupported_version')
        status = value.get('status')
        if status in ('unavailable', 'conflict'):
            reason = _text(value.get('reason'))
            if reason not in _REASONS or len(value) != 3:
                raise ValueError('invalid marker')
            return _unavailable(reason, conflict=status == 'conflict')
        if status != 'captured' or value.get('reason') is not None:
            raise ValueError('invalid status')
        if _text(value.get('source_ref')) != SOURCE_REF:
            return _unavailable('source_mismatch')
        if (_text(value.get('symbol')) != _text(symbol)
                or _text(value.get('strategy')) != _text(strategy)
                or strategy != 'sepa_trend'):
            return _unavailable('identity_mismatch', conflict=True)
        prices = _mapping(value['prices'], {'entry', 'target', 'stop'})
        prices = {key: _number(prices[key]) for key in ('entry', 'target', 'stop')}
        if any(Decimal(price) <= 0 for price in prices.values()):
            raise ValueError('invalid price')
        gate = _mapping(value['gate'], {'basis', 'threshold', 'passed'})
        if (type(gate.get('basis')) is not str or gate['basis'] != 'entry_roundtrip_per_share'
                or type(gate.get('threshold')) is not str or gate['threshold'] != '2.0'
                or gate.get('passed') is not True):
            raise ValueError('invalid gate')
        details = _mapping(value['diagnostics'], {*_DIAGNOSTICS, 'cost_status'})
        if details.get('cost_status') != 'calculated':
            raise ValueError('invalid diagnostics')
        copied = {}
        for key in _DIAGNOSTICS:
            scalar = details[key]
            if type(scalar) not in (float, int) or not isfinite(scalar) or abs(scalar) > 10**128:
                raise ValueError('invalid diagnostic number')
            copied[key] = scalar
        copied['cost_status'] = 'calculated'
        return {'version': 1, 'status': 'captured', 'reason': None,
                'source_ref': SOURCE_REF, 'symbol': symbol, 'strategy': strategy,
                'prices': prices, 'captured_at': _time(value['captured_at']),
                'gate': {'basis': gate['basis'], 'threshold': gate['threshold'], 'passed': True},
                'diagnostics': copied, 'fee_snapshot': _copy_fee(value['fee_snapshot'])}
    except Exception:
        return _unavailable()


def capture_sepa_rr_selection(signal, diagnostics) -> dict:
    """통과한 SEPA batch 호출의 기존 float 진단과 Decimal 가격을 고정한다.

    captured_at은 원래 Signal.timestamp다. 지표 관측 시각이나 현재 가격 승인이 아니다.
    """
    try:
        if type(diagnostics) is not dict or diagnostics.get('cost_status') != 'calculated':
            return _unavailable('diagnostics_unavailable')
        value = {'version': 1, 'status': 'captured', 'reason': None,
                 'source_ref': SOURCE_REF, 'symbol': signal.symbol, 'strategy': signal.strategy.value,
                 'prices': {'entry': signal.price, 'target': signal.target_price, 'stop': signal.stop_price},
                 'captured_at': signal.timestamp,
                 'gate': {'basis': 'entry_roundtrip_per_share', 'threshold': '2.0', 'passed': True},
                 'diagnostics': {key: diagnostics.get(key) for key in (*_DIAGNOSTICS, 'cost_status')},
                 'fee_snapshot': diagnostics.get('fee_snapshot', _unavailable('fee_config_unavailable'))}
        return copy_rr_selection(value, symbol=signal.symbol, strategy=signal.strategy.value)
    except Exception:
        return _unavailable('capture_failed')


def _optional(value, converter):
    if value is None:
        return None
    try:
        return converter(value)
    except Exception:
        return None


def _quantity(value):
    if type(value) is not int or not 0 <= value <= 10**12:
        raise ValueError('invalid quantity')
    return value


def build_rr_log_context(event, extra_metadata=None) -> dict:
    """await 전에 원래 선정/현재 신호/서로 다른 사이징 자료를 분리 복사한다.

    수량은 제출 승인·접수·체결의 증거가 아니다. 결측은 null이고 실제 0은 0이다.
    """
    try:
        meta = event.metadata if type(event.metadata) is dict else {}
        plan = meta.get('entry_plan')
        plan = plan if type(plan) is dict else {}
        refs = plan.get('inputs_ref')
        refs = refs if type(refs) is dict else {}
        strategy = event.strategy.value if event.strategy is not None else ''
        direct, planned = meta.get('rr_selection'), refs.get('rr_selection')
        planned_selection = copy_rr_selection(planned, symbol=event.symbol, strategy=strategy)
        selection = (copy_rr_selection(direct, symbol=event.symbol, strategy=strategy)
                     if 'rr_selection' in meta else planned_selection)
        if 'rr_selection' in meta and 'rr_selection' in refs:
            # 같은 결측 사유는 충돌이 아니다. 서로 다른 검증 결과만 충돌로 남긴다.
            if planned_selection != selection:
                selection = _unavailable('selection_conflict', conflict=True)
        if planned_selection['status'] == 'captured' and (
                plan.get('symbol') != event.symbol or plan.get('strategy') != strategy):
            selection = _unavailable('identity_mismatch', conflict=True)
        extra = extra_metadata if type(extra_metadata) is dict else {}
        risk = meta.get('entry_risk')
        risk = risk if type(risk) is dict else {}
        trace = meta.get('size_trace')
        trace = trace if type(trace) is dict else {}
        return {'version': 1, 'status': 'captured', 'reason': None, 'selection': selection,
                'signal': {'signal_id': _optional(event.id, _text),
                           'source': _optional(event.source, _text),
                           'symbol': _optional(event.symbol, _text),
                           'strategy': _optional(strategy, _text),
                           'timestamp': _optional(event.timestamp, _time),
                           'prices': {'entry': _optional(event.price, _number),
                                      'target': _optional(event.target_price, _number),
                                      'stop': _optional(event.stop_price, _number)},
                           'quote_as_of': _optional(meta.get('quote_as_of'), _time)},
                'plan': {'plan_id': _optional(plan.get('plan_id'), _text),
                         'plan_version': _optional(plan.get('plan_version'), _quantity)},
                'quantities': {'requested_quantity': _optional(extra.get('requested_quantity'), _quantity),
                               'entry_risk': {'planned_quantity': _optional(risk.get('planned_quantity'), _quantity),
                                              'planned_price': _optional(risk.get('planned_price'), _number)},
                               'size_trace_quantity': _optional(trace.get('quantity'), _quantity)}}
    except Exception:
        return _unavailable('capture_failed')
