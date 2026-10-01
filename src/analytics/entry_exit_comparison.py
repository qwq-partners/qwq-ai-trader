"""Offline same-entry initial-stop versus fixed-horizon bid comparison.

The report binds supplied records and reviewed session assertions.  It does not
authenticate either the records, reviewer, or source, and never submits orders.
"""
from copy import deepcopy
from decimal import Decimal, DecimalException

from .entry_evaluation_bundle import _digest, _reviewed_quotes, build_evaluation_bundle, load_json_bytes
from .entry_markout import MarkoutPolicy
from .entry_price_shadow import KST, PriceGatePolicy, _fees, _integer, _mapping, _number, _text, _timestamp


_FIELDS = {'version', 'policy_ref', 'study_sha256', 'fixed_at', 'max_quote_gap_seconds'}


def _policy(value, study_sha256, horizon):
    data = _mapping(value, 'comparison_policy')
    if set(data) != _FIELDS or data.get('version') != 'same-entry-initial-stop-bid-v1':
        raise ValueError('comparison policy schema/version mismatch')
    if data.get('study_sha256') != study_sha256:
        raise ValueError('comparison policy study binding mismatch')
    fixed = _timestamp(data.get('fixed_at'), 'comparison_policy.fixed_at')
    gap = _number(data.get('max_quote_gap_seconds'), 'comparison_policy.max_quote_gap_seconds')
    if gap > horizon:
        raise ValueError('comparison policy gap exceeds original horizon')
    _text(data.get('policy_ref'), 'comparison_policy.policy_ref')
    return fixed, gap


def _unknown(row, *reasons, baseline=None):
    return {'opportunity_id': row['opportunity_id'], 'symbol': row['symbol'],
            'outcome_status': 'unknown', 'reason_codes': list(reasons),
            'baseline_gate_status': None if baseline is None else baseline.get('gate_status'),
            'baseline_quantity': None if baseline is None else baseline.get('a_quantity'),
            'baseline_reason_codes': None if baseline is None else deepcopy(baseline.get('reason_codes')),
            'baseline_outcome_status': None if baseline is None else baseline.get('outcome_status'),
            'entry_quote_id': None, 'horizon_quote_id': None, 'stop_trigger_quote_id': None,
            'hold_net_pnl': None, 'stop_net_pnl': None, 'stop_minus_hold_net_pnl': None}


def _path_quotes(entry, horizon, quotes, reviewed, qty, price_policy, max_gap):
    """Validate every KRX record in order; do not skip a bad first record."""
    path = [q for q in quotes if q.get('symbol') == entry.get('symbol')
            and entry['sequence'] <= q['sequence'] <= horizon['sequence']
            and _mapping(q.get('provenance'), 'quote.provenance').get('tr_id') != 'H0NXASP0']
    if not path or path[0].get('quote_id') != entry.get('quote_id') or path[-1].get('quote_id') != horizon.get('quote_id'):
        raise ValueError('PATH_QUOTE_SELECTION_MISMATCH')
    previous_received = previous_observed = None
    for quote in path:
        evidence = reviewed.get(quote.get('quote_id'))
        if evidence is None or evidence.get('session') != 'KRX_REGULAR_CONTINUOUS':
            raise ValueError('MISSING_OR_UNSUPPORTED_PATH_SESSION_REVIEW')
        provenance = _mapping(quote.get('provenance'), 'quote.provenance')
        received = _timestamp(provenance.get('received_at'), 'quote.received_at')
        observed = _timestamp(quote.get('observed_at'), 'quote.observed_at')
        if (provenance.get('tr_id') != 'H0STASP0' or provenance.get('source_as_of') is not None
                or type(provenance.get('message_count')) is not int or provenance['message_count'] != 1
                or received > observed):
            raise ValueError('PATH_QUOTE_PROVENANCE_OR_TIME')
        if Decimal(str((observed - received).total_seconds())) > price_policy.max_quote_age_seconds:
            raise ValueError('PATH_QUOTE_DELAYED')
        for instant in (received, observed):
            local = instant.astimezone(KST)
            if local.weekday() >= 5 or not (local.hour, local.minute) >= (9, 0) or not (local.hour, local.minute) < (15, 20):
                raise ValueError('PATH_QUOTE_SESSION_UNSUPPORTED')
            if local.date() != _timestamp(entry['observed_at'], 'entry.observed_at').astimezone(KST).date():
                raise ValueError('PATH_QUOTE_DAY_MISMATCH')
        if previous_received is not None:
            if received < previous_received or observed < previous_observed:
                raise ValueError('PATH_QUOTE_TIME_REGRESSION')
            if (Decimal(str((received - previous_received).total_seconds())) > max_gap
                    or Decimal(str((observed - previous_observed).total_seconds())) > max_gap):
                raise ValueError('PATH_QUOTE_GAP_EXCEEDED')
        bid, ask = _number(quote.get('bid'), 'quote.bid'), _number(quote.get('ask'), 'quote.ask')
        if bid > ask or _integer(quote.get('bid_size'), 'quote.bid_size') < qty:
            raise ValueError('PATH_QUOTE_CROSSED_OR_INSUFFICIENT_SIZE')
        previous_received, previous_observed = received, observed
    return path


def _scenario(base, evaluation, context, quotes, reviewed, fixed_at, max_gap, slippage_bps=None):
    policy = PriceGatePolicy.from_dict(context['policy'])
    if slippage_bps is not None:
        policy = PriceGatePolicy(policy.max_quote_age_seconds, policy.max_decision_delay_seconds,
                                 Decimal(slippage_bps), Decimal(slippage_bps))
    fees, rows, known_delta = _fees(context['fees']), [], Decimal(0)
    tasks = {item['opportunity_id']: item for item in base['review_tasks']}
    baseline_rows = {item['opportunity_id']: item for item in evaluation['report']['opportunities']}
    for row in evaluation['payload']['opportunities']:
        task = tasks[row['opportunity_id']]
        baseline = baseline_rows[row['opportunity_id']]
        try:
            if baseline.get('gate_status') not in {'allow', 'cash'}:
                raise ValueError('BASELINE_NOT_ENTRY_VALID')
            _integer(baseline.get('a_quantity'), 'baseline.a_quantity')
            if baseline.get('outcome_status') != 'complete_markout_proxy':
                raise ValueError('BASELINE_HORIZON_NOT_VALID')
        except (ValueError, TypeError, AttributeError) as exc:
            rows.append(_unknown(row, str(exc), baseline=baseline))
            continue
        entry_task, horizon_task = task.get('entry_quote'), task.get('markout_quote')
        if entry_task is None or horizon_task is None:
            rows.append(_unknown(row, *(task['reasons'] or ['MISSING_ENTRY_OR_HORIZON_QUOTE']), baseline=baseline))
            continue
        try:
            decision = _timestamp(row.get('decision_at'), 'decision_at')
            if fixed_at > decision:
                raise ValueError('COMPARISON_POLICY_AFTER_DECISION')
            qty = _integer(row.get('quantity'), 'quantity')
            stop_pct = _number(_mapping(row.get('stop'), 'stop').get('pct'), 'effective_stop_pct')
            entry, horizon = entry_task['record'], horizon_task['record']
            path = _path_quotes(entry, horizon, quotes, reviewed, qty, policy, max_gap)
            entry_price = _number(entry.get('ask'), 'entry.ask') * (1 + policy.entry_slippage_bps / 10000)
            hold_price = _number(horizon.get('bid'), 'horizon.bid') * (1 - policy.exit_slippage_bps / 10000)
            hold_pnl, _ = fees.calculate_net_pnl(entry_price, hold_price, qty)
            trigger, stop_pnl = None, None
            for quote in path:
                displayed_bid = _number(quote.get('bid'), 'quote.bid')
                _, net_pct = fees.calculate_net_pnl(entry_price, displayed_bid, qty)
                if net_pct <= -stop_pct:
                    trigger = quote
                    sell = displayed_bid * (1 - policy.exit_slippage_bps / 10000)
                    stop_pnl, _ = fees.calculate_net_pnl(entry_price, sell, qty)
                    break
            if stop_pnl is None:
                stop_pnl = hold_pnl
            delta = stop_pnl - hold_pnl
            known_delta += delta
            rows.append({'opportunity_id': row['opportunity_id'], 'symbol': row['symbol'],
                         'outcome_status': 'known_pair', 'reason_codes': [],
                         'baseline_gate_status': baseline.get('gate_status'),
                         'baseline_quantity': baseline.get('a_quantity'),
                         'baseline_reason_codes': deepcopy(baseline.get('reason_codes')),
                         'baseline_outcome_status': baseline.get('outcome_status'),
                         'entry_quote_id': entry['quote_id'], 'horizon_quote_id': horizon['quote_id'],
                         'stop_trigger_quote_id': None if trigger is None else trigger['quote_id'],
                         'hold_net_pnl': str(hold_pnl), 'stop_net_pnl': str(stop_pnl),
                         'stop_minus_hold_net_pnl': str(delta)})
        except (KeyError, TypeError, ValueError, AttributeError, DecimalException) as exc:
            rows.append(_unknown(row, str(exc), baseline=baseline))
    known = sum(item['outcome_status'] == 'known_pair' for item in rows)
    return {'slippage_bps_each': None if slippage_bps is None else slippage_bps, 'candidates': rows,
            'counts': {'total': len(rows), 'known_pairs': known, 'unknown': len(rows) - known,
                       'baseline_cash': sum(r.get('gate_status') == 'cash' for r in evaluation['report']['opportunities'])},
            'summary': {'known_stop_minus_hold_net_pnl': str(known_delta) if known else None,
                        'complete_stop_minus_hold_net_pnl': str(known_delta) if known and known == len(rows) else None}}


def build_exit_comparison(study_bytes, observations, *, study_sha256, comparison_policy, session_review=None):
    """Build a fail-closed initial-stop diagnostic for the original full cohort."""
    base = build_evaluation_bundle(study_bytes, observations, study_sha256=study_sha256,
                                   session_review=session_review)
    context = _mapping(load_json_bytes(study_bytes), 'study')
    markout = MarkoutPolicy.from_dict(context['markout_policy'])
    fixed_at, max_gap = _policy(comparison_policy, study_sha256, Decimal(markout.horizon_seconds))
    reviewed = _reviewed_quotes(session_review, context, observations['records'], base['binding'])
    quotes = [record for record in observations['records'] if record.get('kind') == 'ws_quote']
    original = _scenario(base, base['original'], context, quotes, reviewed, fixed_at, max_gap)
    sensitivity = []
    for variant in base['sensitivity']:
        derived = deepcopy(context)
        bps = variant['slippage_bps_each']
        derived['policy'].update(entry_slippage_bps=str(bps), exit_slippage_bps=str(bps))
        sensitivity.append(_scenario(base, variant['result'], derived, quotes, reviewed, fixed_at, max_gap, bps))
    return {'version': 'entry-exit-comparison-v1', 'dataset_kind': context['dataset_kind'],
            'binding': {**deepcopy(base['binding']), 'comparison_policy': deepcopy(comparison_policy),
                        'comparison_policy_sha256': _digest(comparison_policy),
                        'session_review_sha256': base['session_review_sha256']},
            'original': original, 'sensitivity': sensitivity,
            'source_authenticity_verified': False, 'production_eligible': False,
            'profitability_established': False, 'account_return': None,
            'limitations': [
                'Hashes and reviewed assertions bind supplied inputs; they do not authenticate a source, reviewer, or fixed-time claim.',
                'A known pair is a bid-price proxy, not an actual exit, portfolio replay, or account return.',
                'The comparison inherits the existing bundle scope: gap_and_go, order_ready/capital and target validation.',
                'Gap limits do not prove that no market event or source message was lost.',
            ]}
