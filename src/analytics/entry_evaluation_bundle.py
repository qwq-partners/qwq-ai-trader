"""Offline assembly of explicitly reviewed quote sessions, never automatic market proof.

Hashes bind supplied assertions to decoded records. They authenticate neither the
reviewer nor the external source. Trading, source acquisition and promotion are absent.
"""
from copy import deepcopy
import hashlib
import json
import re

from .entry_markout import MarkoutPolicy, select_markout_quote
from .entry_observation import prepare_input
from .entry_price_shadow import KST, _mapping, _text, _timestamp


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def _sha(value):
    if not isinstance(value, str) or re.fullmatch('[0-9a-f]{64}', value) is None:
        raise ValueError('SHA-256 lowercase hex required')
    return value


def load_json_bytes(raw):
    """Decode JSON without accepting ambiguous keys or nonfinite constants."""
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('duplicate JSON key')
            result[key] = value
        return result

    def constant(value):
        raise ValueError('nonfinite JSON constant')

    return json.loads(raw, object_pairs_hook=unique, parse_constant=constant)


def _reviewed_quotes(review, context, records, binding, *, analysis_as_of=None):
    if review is None:
        return {}
    if (not isinstance(review, dict) or set(review) != {'version', 'dataset_kind', 'binding', 'quotes'}
            or review['version'] != 'entry-session-review-v1'
            or review['dataset_kind'] != context['dataset_kind']):
        raise ValueError('session review schema/dataset mismatch')
    if review['binding'] != binding:
        raise ValueError('session review binding mismatch')
    items = review['quotes']
    quotes = {r['quote_id']: r for r in records if r['kind'] == 'ws_quote'}
    if not isinstance(items, list) or len(items) > len(quotes):
        raise ValueError('session review quote count')
    fields = {'quote_id', 'symbol', 'record_sha256', 'session', 'basis', 'source_ref', 'source_sha256',
              'reviewer_ref', 'reviewed_at', 'valid_from', 'valid_until'}
    accepted = {}
    for item in items:
        if not isinstance(item, dict) or set(item) != fields:
            raise ValueError('session review quote fields')
        qid = _text(item['quote_id'], 'quote_id')
        if qid not in quotes or qid in accepted:
            raise ValueError('foreign/duplicate reviewed quote')
        q = quotes[qid]
        if item['symbol'] != q['symbol'] or _sha(item['record_sha256']) != _digest(q):
            raise ValueError('reviewed quote identity/hash mismatch')
        if (item['session'] not in {'KRX_REGULAR_CONTINUOUS', 'AUCTION', 'VI', 'HALT', 'UNKNOWN'}
                or item['basis'] not in {'external_event_history_review', 'official_per_quote_status_review'}):
            raise ValueError('unsupported session evidence basis/state')
        for key in ('source_ref', 'reviewer_ref'):
            _text(item[key], key)
        _sha(item['source_sha256'])
        received = _timestamp(q['provenance']['received_at'], 'received_at')
        observed = _timestamp(q['observed_at'], 'observed_at')
        start = _timestamp(item['valid_from'], 'valid_from')
        end = _timestamp(item['valid_until'], 'valid_until')
        reviewed = _timestamp(item['reviewed_at'], 'reviewed_at')
        if analysis_as_of is not None and reviewed > _timestamp(analysis_as_of, 'analysis_as_of'):
            raise ValueError('session review after analysis_as_of')
        if (not start <= received <= observed < end or reviewed <= observed
                or len({t.astimezone(KST).date() for t in (start, received, observed, end)}) != 1):
            raise ValueError('session evidence interval/review time mismatch')
        accepted[qid] = deepcopy(item)
    return accepted


def _quote_task(quote, reviewed):
    if quote is None:
        return None
    evidence = reviewed.get(quote['quote_id'])
    return {'quote_id': quote['quote_id'], 'symbol': quote['symbol'], 'record_sha256': _digest(quote),
            'record': deepcopy(quote),
            'review_status': evidence['session'] if evidence else 'UNREVIEWED',
            'reviewed_assertion': deepcopy(evidence)}


def build_evaluation_bundle(study_bytes, observations, *, study_sha256, session_review=None,
                            analysis_as_of=None):
    """Preserve the original cohort, select first quotes, then reuse existing evaluators.

The original study bytes are verified and decoded here, including for direct callers.
No session assertion is inferred from clock time, channel or hour code.
"""
    if (not isinstance(study_bytes, bytes) or len(study_bytes) > 4 * 1024 * 1024
            or hashlib.sha256(study_bytes).hexdigest() != _sha(study_sha256)):
        raise ValueError('original study bytes/hash mismatch')
    context = _mapping(load_json_bytes(study_bytes), 'study')
    if (context.get('data_basis') != 'received_snapshot' or 'capital_policy' not in context
            or context.get('outcome_basis') != 'fixed_horizon_bid_markout'):
        raise ValueError('requires received capital-snapshot/fixed-horizon study')
    journal = _mapping(observations.get('journal'), 'journal')
    if journal.get('study_sha256') != _sha(study_sha256):
        raise ValueError('original study/journal binding mismatch')
    binding = {'study_sha256': study_sha256, 'observation_sha256': _digest(observations),
               'capture_id': _text(journal.get('capture_id'), 'capture_id'),
               'evaluation_epoch': context['evaluation_epoch']}
    # Reuse validation of original IDs, sequence, population, study and epoch.
    prepare_input(context, observations, [], analysis_as_of=analysis_as_of)
    records = observations['records']
    reviewed = _reviewed_quotes(session_review, context, records, binding, analysis_as_of=analysis_as_of)
    policy = MarkoutPolicy.from_dict(context['markout_policy'])
    fixed_at = max(policy.fixed_at, _timestamp(context['capital_policy']['fixed_at'], 'capital.fixed_at'))
    quotes = [r for r in records if r['kind'] == 'ws_quote']
    tasks, inputs = [], []
    for scan in (r for r in records if r['kind'] == 'scan'):
        for rank, candidate in enumerate(scan['candidates'], 1):
            cid = candidate['candidate_id']
            task = {'opportunity_id': cid, 'symbol': candidate['symbol'], 'scan_id': scan['scan_id'],
                    'returned_rank': rank, 'entry_quote': None, 'markout_quote': None, 'reasons': []}
            item = {'opportunity_id': cid, 'policy_inputs_at': fixed_at.isoformat()}
            signals = [r for r in records if r['kind'] == 'signal' and r.get('candidate_id') == cid]
            orders = ([r for r in records if r['kind'] == 'order_ready'
                       and r.get('signal_id') == signals[0]['signal_id']] if len(signals) == 1 else [])
            first, mark = None, None
            if len(signals) != 1:
                task['reasons'].append('MISSING_OR_AMBIGUOUS_SIGNAL')
            elif len(orders) != 1:
                task['reasons'].append('MISSING_OR_AMBIGUOUS_ORDER_READY')
            else:
                # Same unfiltered first-record rule as prepare_received_input.
                first = next((q for q in quotes if q.get('symbol') == candidate['symbol']
                              and q['sequence'] > orders[0]['sequence']
                              and q['provenance'].get('tr_id') != 'H0NXASP0'), None)
                if first is None:
                    task['reasons'].append('MISSING_FIRST_RECORDED_KRX_QUOTE')
                else:
                    try:
                        mark = select_markout_quote(first, quotes, policy)
                    except ValueError as exc:
                        task['reasons'].append(str(exc))
                    if mark is None:
                        task['reasons'].append('MISSING_MARKOUT_QUOTE')
            for role, quote in (('entry', first), ('markout', mark)):
                task[role + '_quote'] = _quote_task(quote, reviewed)
                if quote is not None:
                    evidence = reviewed.get(quote['quote_id'])
                    if evidence is None:
                        task['reasons'].append('MISSING_' + role.upper() + '_SESSION_REVIEW')
                    else:
                        name = 'quote_session' if role == 'entry' else 'markout_session'
                        item[name] = {'quote_id': quote['quote_id'], 'session': evidence['session'],
                                      'evidence_ref': 'entry-session-review:' + _digest(evidence)}
            tasks.append(task)
            if 'quote_session' in item or 'markout_session' in item:
                inputs.append(item)
    original = prepare_input(context, observations, inputs, analysis_as_of=analysis_as_of)
    variants = []
    for bps in (0, 10, 30):
        derived = deepcopy(context)
        derived['policy'].update(entry_slippage_bps=str(bps), exit_slippage_bps=str(bps))
        variants.append({'slippage_bps_each': bps, 'derived_context_sha256': _digest(derived),
                         'result': prepare_input(derived, observations, inputs, analysis_as_of=analysis_as_of)})
    reports = [original['report']] + [v['result']['report'] for v in variants]
    pairs = [r['counts']['paired_outcomes'] for r in reports]
    status = ('no_comparable_pairs' if min(pairs) == 0 else
              'complete_price_diagnostic' if all(r['summary']['complete_delta_net_pnl'] is not None
                                               for r in reports) else 'partial_price_diagnostic')
    from .selection_basis import build_selection_report
    return {'version': 'entry-evaluation-bundle-v1', 'dataset_kind': context['dataset_kind'],
            'analysis_as_of': original['analysis_as_of'],
            'binding': binding, 'review_tasks': tasks, 'evaluation_inputs': inputs,
            'selection_basis': build_selection_report(observations),
            'original': original, 'sensitivity': variants,
            'readiness': {'status': status, 'minimum_paired_outcomes_across_scenarios': min(pairs),
                          'original_cohort_count': len(tasks),
                          'missing_required_session_evidence_candidates': len(tasks) - sum(
                              'quote_session' in item and 'markout_session' in item for item in inputs),
                          'profitability_pass': False},
            'session_review_sha256': _digest(session_review) if session_review is not None else None,
            'source_authenticity_verified': False, 'production_eligible': False, 'account_return': None,
            'limitations': [
                'Session facts are explicit reviewed assertions; hashes are binding, not source authentication.',
                'No regular-continuous session is inferred from hour code, local clock or absence of VI events.',
                'Review tasks include provisional first quotes; the existing evaluator retains all validity checks.',
                'No actual exit/capital-reuse replay, whole-ranking evaluation or profitability promotion.',
                'Original study bytes are bound separately; derived context hashes use canonical JSON without LF.',
            ]}
