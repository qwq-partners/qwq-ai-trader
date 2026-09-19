"""원 전체 청산 호출을 검증하는 C3 application 원장과 보호 전용 replay."""
from copy import deepcopy
from dataclasses import asdict
import json

from .policy_generations import canonical, versioned_fact
from .protection import decode_protection, encode_protection
from .protection_recovery import digest, _scope, _append
from .regime_horizon import RegimeApplicationReceipt, RegimeSyncContext
from ...strategies.exit_manager import REGIME_EXIT_PARAMS, INTRADAY_CRASH_PARAMS
from ...core.market_regime import cap_regime_by_intraday_risk, classify_intraday_level, max_intraday_level

PROTECTION_READS = ('protection.config', 'protection.current_regime', 'protection.intraday_crash_level')
HORIZON_READS = ('intraday_policy.current', 'regime_policy.horizon')
CLASSIFIER_READS = PROTECTION_READS + HORIZON_READS
GLOBAL_KEYS = ('schema', 'market', 'config', 'max_holding_days', 'current_regime',
               'intraday_crash_level', 'exit_exempt')


def rule_digest():
    from ...schedulers.kr_scheduler import KRScheduler
    return digest({'rule': 'regime_application_v1', 'regime': REGIME_EXIT_PARAMS,
        'intraday': INTRADAY_CRASH_PARAMS, 'caps': KRScheduler._KOSPI_CAP,
        'order': KRScheduler._LLM_OPTIMISM_ORDER})


def classifier_ref(state, operation, *, envelope=None, version=None):
    row = state['risk_sources']['records'][operation]
    terminal = row['terminal']
    if terminal is not None:
        if terminal['receipt']['status'] != 'accepted': raise ValueError('regime_classifier_not_accepted')
        envelope, version = terminal['envelope'], terminal['receipt']['committed_version']
    seal = state['risk_input_seals']['records'][operation]['original']
    return {'operation_id': operation, 'committed_version': version,
        'request_digest': row['ticket']['request_digest'], 'outcome_digest': digest(envelope),
        'seal_digest': seal['seal_digest']}


def application_labels(raw, projected, context, policies):
    from ...schedulers.kr_scheduler import KRScheduler
    raw_label = raw.get('regime')
    capped = projected.get('regime')
    regime = projected.get('regime', 'neutral')
    fallback = None
    if type(regime) is not str or regime not in REGIME_EXIT_PARAMS:
        fallback, regime = 'neutral', 'neutral'
    if context['regime_conflict_guard_enabled']:
        from .regime_owner import _time
        day = _time(context['captured_at']).date()
        batch = policies['intraday_policy.current']['value']
        horizon = policies['regime_policy.horizon']['value']
        batch_level = batch['level'] if batch['updated_at'] and _time(batch['updated_at']).date() == day else None
        horizon_level = horizon['level'] if horizon['classified_at'] and _time(horizon['classified_at']).date() == day else None
        cap = max_intraday_level(batch_level, horizon_level,
            classify_intraday_level(projected['input_meta']['kospi_today_pct']))
        technical = context['screener']['regime']
        if technical is None: technical = 'neutral'
        regime = KRScheduler._resolve_regime_conflict(KRScheduler, technical, regime, cap)
    return {'raw': raw_label, 'classifier_capped': capped, 'fallback': fallback, 'final': regime}


def protection_scope(dto, symbol):
    return _scope({'protection': dto, 'portfolio': {'positions': {}}, 'lots': {}, 'cursors': {}}, symbol)['protection']


def calculation(before, raw_json, projected, context, policies):
    from .regime_owner import _time
    when = _time(context['captured_at'])
    labels = application_labels(json.loads(raw_json), projected, context, policies)
    manager = decode_protection(before, clock=lambda: when)
    manager.apply_regime_params(labels['final'], force=False)
    after = encode_protection(manager)
    symbols = set(before['states']) | set(after['states']) | set(before['degraded']) | set(after['degraded'])
    changed = {}
    for symbol in sorted(symbols):
        old, new = digest(protection_scope(before, symbol)), digest(protection_scope(after, symbol))
        if old != new: changed[symbol] = {'before_digest': old, 'after_digest': new}
    return {'raw_result_json': raw_json, 'labels': labels, 'rule_digest': rule_digest(), 'force': False,
        'original_protection_before': deepcopy(before), 'before_digest': digest(before),
        'original_protection_after': after, 'after_digest': digest(after),
        'global_before_digest': digest({key: before[key] for key in GLOBAL_KEYS}),
        'global_after_digest': digest({key: after[key] for key in GLOBAL_KEYS}),
        'changed_symbol_scopes': changed}


def append_application(state, *, operation, kind, ref, request, read_bundle, context,
                       payload, version, now, stale_reason=''):
    before = deepcopy(state)
    policies = (json.loads(read_bundle['reads_json'])['versioned_policies'] if kind == 'classifier'
                else read_bundle['versioned_policies'])
    computed = None if stale_reason else calculation(state['protection'], payload['raw_result_json'],
        payload['result'], context, policies)
    status = ('stale' if stale_reason else 'applied' if computed['before_digest'] != computed['after_digest']
              else 'unchanged')
    receipt = RegimeApplicationReceipt(operation, status, stale_reason, version,
        ref['operation_id'], ref['committed_version'])
    source_ticket = state['risk_sources']['records'][ref['operation_id']]['ticket']
    row = {'schema': 1, 'operation_id': operation, 'kind': kind,
        **{key: source_ticket[key] for key in ('account_scope', 'business_day', 'generation', 'fence_id')},
        'request': deepcopy(request), 'request_digest': digest({'operation_id': operation, **request}),
        'receipt': asdict(receipt), 'classifier_ref': ref, 'read_bundle': read_bundle,
        'context': context, 'context_digest': digest(context), 'calculation': computed}
    row['digest'] = digest(row)
    state['regime_policy']['applications'][operation] = row
    if computed is not None:
        state['protection'] = deepcopy(computed['original_protection_after'])
        for symbol in before['protection']['degraded']:
            if symbol in computed['changed_symbol_scopes']:
                _append(before, state, symbol, {'kind': 'regime_application', 'application_id': operation,
                    'application_digest': row['digest']}, version, now)
    return row


def validate_application(state, operation, row, version):
    from .regime_owner import _keys, _time
    _keys(row, ('schema', 'operation_id', 'kind', 'account_scope', 'business_day', 'generation',
        'fence_id', 'request', 'request_digest', 'receipt', 'classifier_ref', 'read_bundle',
        'context', 'context_digest', 'calculation', 'digest'))
    receipt = row['receipt']; _keys(receipt, RegimeApplicationReceipt.__dataclass_fields__)
    if (type(row['schema']) is not int or row['schema'] != 1 or row['operation_id'] != operation
            or row['kind'] not in {'classifier', 'sync'} or receipt['operation_id'] != operation
            or type(receipt['committed_version']) is not int
            or not state['regime_policy']['horizon_baseline']['baseline_version'] < receipt['committed_version'] <= version
            or row['digest'] != digest({key: value for key, value in row.items() if key != 'digest'})
            or row['request_digest'] != digest({'operation_id': operation, **row['request']})
            or row['context_digest'] != digest(row['context'])):
        raise ValueError('regime_application_crosslink')
    context = RegimeSyncContext.from_dict(row['context']).to_dict()
    ref = row['classifier_ref']; _keys(ref, ('operation_id', 'committed_version', 'request_digest', 'outcome_digest', 'seal_digest'))
    source = state['risk_sources']['records'][ref['operation_id']]
    if (source['ticket']['kind'] != 'llm_regime' or canonical(ref) != canonical(classifier_ref(state, ref['operation_id']))
            or receipt['classifier_operation_id'] != ref['operation_id']
            or receipt['classifier_version'] != ref['committed_version']
            or any(row[key] != source['ticket'][key] for key in ('account_scope', 'business_day', 'generation', 'fence_id'))):
        raise ValueError('regime_application_source_crosslink')
    bundle = row['read_bundle']; seal = state['risk_input_seals']['records'][ref['operation_id']]['original']
    if row['kind'] == 'classifier':
        expected = {'kind': 'classifier', 'classifier_operation_id': ref['operation_id']}
        if (operation != 'classifier:' + ref['operation_id'] or receipt['committed_version'] != ref['committed_version']
                or canonical(bundle) != canonical({'source_seal_digest': seal['seal_digest'],
                    'reads_json': canonical(seal['reads'])}) or context != seal['request']['inputs']['context']):
            raise ValueError('regime_classifier_application_crosslink')
        policies = seal['reads']['versioned_policies']
    else:
        expected = {'kind': 'sync', 'supplied_context': context}
        _keys(bundle, ('captured_version', 'captured_at', 'classifier_ref', 'versioned_policies'))
        if (type(bundle['captured_version']) is not int or bundle['captured_version'] >= receipt['committed_version']
                or bundle['captured_at'] != context['captured_at'] or bundle['classifier_ref'] != ref):
            raise ValueError('regime_sync_read_crosslink')
        policies = bundle['versioned_policies']
        for name, fact in policies.items():
            if canonical(fact) != canonical(versioned_fact(state, name, cutoff=bundle['captured_version'] + 1)):
                raise ValueError('regime_sync_capture_history')
            if receipt['status'] != 'stale' and canonical(fact) != canonical(versioned_fact(state, name, cutoff=receipt['committed_version'])):
                raise ValueError('regime_sync_commit_history')
    names = CLASSIFIER_READS if row['kind'] == 'classifier' or context['regime_conflict_guard_enabled'] else PROTECTION_READS
    if set(policies) != set(names) or canonical(row['request']) != canonical(expected):
        raise ValueError('regime_application_request_conflict')
    if receipt['status'] == 'stale':
        if row['kind'] != 'sync' or row['calculation'] is not None or not receipt['reason']:
            raise ValueError('invalid_regime_stale_application')
        return row
    if receipt['status'] not in {'applied', 'unchanged'} or receipt['reason']:
        raise ValueError('invalid_regime_application_receipt')
    computed = row['calculation']; payload = source['terminal']['envelope']['payload']
    for name in PROTECTION_READS:
        field = name.split('.')[1]
        if canonical(computed['original_protection_before'][field]) != canonical(policies[name]['value']):
            raise ValueError('regime_original_protection_read_conflict')
    if _time(context['captured_at']).date().isoformat() != row['business_day']:
        raise ValueError('regime_application_day')
    expected = calculation(computed['original_protection_before'], payload['raw_result_json'],
        payload['result'], context, policies)
    if canonical(computed) != canonical(expected): raise ValueError('regime_original_application_conflict')
    if receipt['status'] != ('applied' if expected['before_digest'] != expected['after_digest'] else 'unchanged'):
        raise ValueError('regime_application_disposition_conflict')
    return row


def replay_application(state, event, candidate, now):
    row = state['regime_policy']['applications'].get(event['application_id'])
    if (row is None or event['application_digest'] != row['digest']
            or event['source_version'] != row['receipt']['committed_version']):
        raise ValueError('regime_replay_application_crosslink')
    computed = row['calculation']; symbol = event['symbol']
    if computed is None or symbol not in computed['changed_symbol_scopes']:
        raise ValueError('regime_replay_unchanged_scope')
    for side in ('before', 'after'):
        if canonical(event[side]['protection']) != canonical(protection_scope(computed['original_protection_' + side], symbol)):
            raise ValueError('regime_replay_original_scope')
    if any(canonical(event['before'][key]) != canonical(event['after'][key]) for key in ('position', 'lots', 'cursors')):
        raise ValueError('regime_replay_economic_scope')
    # validate_application은 원 full DTO에서 public force=False 호출을 먼저 증명한다.
    original = computed['original_protection_before']; label = computed['labels']['final']
    manager = decode_protection(candidate, clock=lambda: now)
    if not (original['current_regime'] == label and original['states']):
        manager._apply_regime_params_body(label, REGIME_EXIT_PARAMS[label])
    return encode_protection(manager)
