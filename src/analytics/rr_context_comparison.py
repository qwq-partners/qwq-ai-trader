"""Offline comparison of captured SEPA selection and later signal references.

This is conditional analysis using the selection's fee rates and each existing
quantity separately. It does not establish orders, fills, or realized results.
"""
from collections import Counter
from decimal import Decimal, DecimalException, localcontext

from src.analytics.order_rr_comparison import DECIMAL_CONTEXT, _decimal_text, _supported_decimal, compare_order_rr
from src.utils.fee_calculator import FeeConfig
from src.utils.rr_context import SOURCE_REF, copy_rr_selection

MAX_ROWS = 10_000
LANES = ('requested_quantity', 'entry_risk.planned_quantity', 'size_trace_quantity')
RATES = ('buy_commission_rate', 'sell_commission_rate', 'sell_tax_rate')


def _text(value):
    return value if type(value) is str and 0 < len(value) <= 256 else None


def _mapping(value):
    return value if type(value) is dict else {}


def _price(value):
    if type(value) is not str or len(value) > 128 or value != value.strip():
        return None
    try:
        number = Decimal(value)
        return value if _supported_decimal(number) and number > 0 else None
    except (DecimalException, ValueError):
        return None


def _changes(selected, current, current_status):
    changes = {}
    for key in ('entry', 'target', 'stop'):
        old, new = _price(selected.get(key)), current.get(key)
        reason = ('missing_current_price' if current_status[key] == 'missing' else
                  'invalid_current_price' if current_status[key] == 'invalid' else
                  'unsupported_selected_price')
        change = {'changed': None, 'percent': None, 'reason': reason}
        if old is not None and new is not None:
            try:
                with localcontext(DECIMAL_CONTEXT):
                    before, after = Decimal(old), Decimal(new)
                    percent = (after - before) / before * 100
                    change = {'changed': before != after, 'percent': _decimal_text(percent), 'reason': None}
            except DecimalException:
                change['reason'] = 'calculation_unavailable'
        changes[key] = change
    return changes


def _analyze(row, source_ref):
    row = _mapping(row)
    metadata = _mapping(row.get('metadata'))
    context = _mapping(metadata.get('rr_context'))
    signal = _mapping(context.get('signal'))
    symbol, strategy = _text(row.get('symbol')), _text(row.get('strategy'))
    signal_id, metadata_id = _text(signal.get('signal_id')), _text(metadata.get('signal_id'))
    record = {'row_id': row.get('id') if type(row.get('id')) is int and 0 <= row['id'] <= 2**63 - 1 else None,
              'event_time': _text(row.get('event_time')), 'event_type': _text(row.get('event_type')),
              'symbol': symbol, 'strategy': strategy, 'side': _text(row.get('side')),
              'signal_id': signal_id, 'status': 'unavailable', 'reason': None,
              'selection_observed': None, 'current_reference': None, 'price_changes': None,
              'quantity_conflict': None, 'quantity_lanes': {}}
    if not context:
        record['reason'] = 'context_missing'
    elif type(context.get('version')) is not int or context['version'] != 1:
        record['reason'] = 'unsupported_context_version'
    elif context.get('status') != 'captured' or context.get('reason') is not None:
        record['reason'] = 'context_unavailable'
    elif strategy != 'sepa_trend' or row.get('side') != 'buy':
        record['reason'] = 'unsupported_strategy_or_side'
    elif not symbol or not signal_id or not metadata_id:
        record['reason'] = 'identity_missing'
    elif (signal_id != metadata_id or signal.get('symbol') != symbol
          or signal.get('strategy') != strategy):
        record['reason'] = 'identity_mismatch'
    else:
        selection = copy_rr_selection(context.get('selection'), symbol=symbol, strategy=strategy)
        if selection['status'] != 'captured':
            record['reason'] = selection['reason']
        else:
            record['selection_observed'] = selection
            current_prices = _mapping(signal.get('prices'))
            # Only known bounded fields are copied; never return arbitrary metadata.
            safe_prices = {k: _price(current_prices.get(k)) for k in ('entry', 'target', 'stop')}
            price_status = {k: ('missing' if current_prices.get(k) is None else
                                'invalid' if safe_prices[k] is None else 'valid') for k in safe_prices}
            calculation_prices = {k: ('invalid' if price_status[k] == 'invalid' else safe_prices[k])
                                  for k in safe_prices}
            plan = _mapping(context.get('plan'))
            risk = _mapping(_mapping(context.get('quantities')).get('entry_risk'))
            version = plan.get('plan_version')
            record['current_reference'] = {
                'signal_id': signal_id, 'source': _text(signal.get('source')),
                'timestamp': _text(signal.get('timestamp')), 'quote_as_of': _text(signal.get('quote_as_of')),
                'prices': safe_prices, 'price_status': price_status,
                'plan': {'plan_id': _text(plan.get('plan_id')),
                         'plan_version': version if type(version) is int and 0 <= version <= 10**12 else None,
                         'planned_price': _price(risk.get('planned_price'))},
            }
            record['price_changes'] = _changes(selection['prices'], safe_prices, price_status)
            quantities = _mapping(context.get('quantities'))
            risk = _mapping(quantities.get('entry_risk'))
            values = (quantities.get('requested_quantity'), risk.get('planned_quantity'),
                      quantities.get('size_trace_quantity'))
            valid_values = [x for x in values if type(x) is int and 0 <= x <= 10**12]
            record['quantity_conflict'] = len(set(valid_values)) > 1
            fee = selection['fee_snapshot']
            config = None
            if fee['status'] == 'captured':
                config = FeeConfig(**{k: Decimal(fee[k]) for k in RATES})
            gate = {'basis': selection['gate']['basis'], 'positive_threshold': selection['gate']['threshold'],
                    'evidence_ref': SOURCE_REF}
            for name, value in zip(LANES, values):
                # Keep missing and actual zero distinct; invalid values never become defaults.
                safe_quantity = value if type(value) is int and 0 <= value <= 10**12 else None
                invalid_quantity = value is not None and safe_quantity is None
                lane = {'quantity': safe_quantity, 'quantity_status': ('invalid' if invalid_quantity else
                        'missing' if value is None else 'zero' if value == 0 else 'present')}
                for basis, prices in (('selected_prices', selection['prices']), ('current_prices', calculation_prices)):
                    lane[basis] = compare_order_rr(
                        entry_price=prices.get('entry'), target_price=prices.get('target'),
                        stop_price=prices.get('stop'), quantity=value, source_path='signal_events.metadata.rr_context',
                        source_ref=source_ref, price_basis=basis, fee_config=config,
                        fee_config_ref='selection.fee_snapshot', original_gate=gate)
                record['quantity_lanes'][name] = lane
            record['status'] = 'compared'
    return record


def compare_rr_rows(rows: list, *, source_ref: str, data_kind: str) -> dict:
    if type(rows) is not list or len(rows) > MAX_ROWS:
        raise ValueError('invalid_rows')
    if data_kind not in ('actual', 'synthetic') or not _text(source_ref):
        raise ValueError('invalid_provenance')
    results = [_analyze(row, source_ref) for row in rows]
    # Validate identity separately from context status, including cross-row reuse.
    identities = {}
    for raw in rows:
        raw = _mapping(raw)
        meta = _mapping(raw.get('metadata'))
        sig = _mapping(_mapping(meta.get('rr_context')).get('signal'))
        sid, symbol, strategy = _text(sig.get('signal_id')), _text(raw.get('symbol')), _text(raw.get('strategy'))
        if (sid and sid == _text(meta.get('signal_id')) and symbol and strategy
                and sig.get('symbol') == symbol and sig.get('strategy') == strategy):
            identities.setdefault(sid, set()).add((symbol, strategy))
    conflicts = {sid for sid, values in identities.items() if len(values) > 1}
    for result in results:
        if result['signal_id'] in conflicts:
            result.update(status='unavailable', reason='signal_identity_conflict',
                          selection_observed=None, current_reference=None, price_changes=None,
                          quantity_conflict=None, quantity_lanes={})
    signal_ids = set(identities) - conflicts
    reasons = Counter(r['reason'] for r in results if r['reason'])
    lane_reasons = Counter(
        f"{name}:{basis}:{lane[basis]['calculation_reason']}"
        for r in results for name, lane in r['quantity_lanes'].items()
        for basis in ('selected_prices', 'current_prices') if lane[basis]['calculation_reason'])
    pairs = sum(1 for r in results for lane in r['quantity_lanes'].values()
                if all(lane[basis]['calculation_status'] == 'ok'
                       for basis in ('selected_prices', 'current_prices')))
    return {'data_kind': data_kind, 'source_ref': source_ref, 'analytical_only': True,
            'limitations': ['selection fee rates are held fixed; not actual execution RR or realized profit',
                            'the current recorded quantity is applied to both price sets; selection quantity is unknown',
                            'original float gate and Decimal reconstruction are distinct; the current-price reconstruction is hypothetical and neither is an order approval',
                            'only the initial SEPA gate is captured'],
            'coverage': {'row_count': len(rows), 'unique_signal_count': len(signal_ids),
                         'eligible_rows': sum(r['status'] == 'compared' for r in results),
                         'comparison_pairs': pairs,
                         'no_comparison_reason': None if pairs else ('no_rows' if not rows else 'no_comparable_lanes'),
                         'unavailable_rows': dict(sorted(reasons.items())),
                         'lane_calculation_reasons': dict(sorted(lane_reasons.items()))}, 'rows': results}
