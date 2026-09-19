"""명시 C3 기준선과 단일 horizon. 과거 관측을 새 source로 승격하지 않는다."""
from copy import deepcopy
from dataclasses import dataclass
from datetime import date
import json

from .policy_generations import canonical
from .protection_recovery import digest
from .risk_transition import IntradayPolicyState


def _helpers():
    from .regime_owner import _keys, _time, _number, _positive
    return _keys, _time, _number, _positive


@dataclass(frozen=True)
class RegimeHorizonBaseline:
    json: str

    @classmethod
    def from_dict(cls, value):
        keys, time, number, positive = _helpers()
        keys(value, ('schema', 'baseline_id', 'account_scope', 'business_day', 'generation',
                     'fence_id', 'evidence', 'regime_baseline_version', 'intraday', 'horizon'))
        if (type(value['schema']) is not int or value['schema'] != 1
                or type(value['generation']) is not int or value['generation'] < 0):
            raise ValueError('invalid_regime_horizon_baseline')
        for name in ('baseline_id', 'account_scope'):
            if type(value[name]) is not str or not value[name].strip():
                raise ValueError('invalid_regime_identity')
        if value['fence_id'] is not None and (type(value['fence_id']) is not str or not value['fence_id']):
            raise ValueError('invalid_regime_fence')
        if date.fromisoformat(value['business_day']).isoformat() != value['business_day']:
            raise ValueError('invalid_regime_day')
        keys(value['evidence'], ('source', 'event_id', 'observed_at'))
        for name in ('source', 'event_id'):
            if type(value['evidence'][name]) is not str or not value['evidence'][name].strip():
                raise ValueError('invalid_regime_evidence')
        time(value['evidence']['observed_at'])
        positive(value['regime_baseline_version'])
        keys(value['intraday'], ('baseline_version', 'current'))
        positive(value['intraday']['baseline_version'])
        IntradayPolicyState.from_dict(value['intraday']['current'])
        keys(value['horizon'], ('level', 'change_pct', 'classified_at'))
        horizon = value['horizon']
        if horizon['level'] not in {'normal', 'caution', 'crash', 'severe'}:
            raise ValueError('invalid_regime_horizon_level')
        if horizon['change_pct'] is not None: number(horizon['change_pct'])
        if horizon['classified_at'] is not None: time(horizon['classified_at'])
        return cls(canonical(value))

    def to_dict(self):
        return json.loads(self.json)


@dataclass(frozen=True)
class RegimeStageInput:
    json: str

    @classmethod
    def from_dict(cls, value):
        keys, time, number, _ = _helpers()
        keys(value, ('schema', 'stage', 'outcome', 'source', 'event_id', 'received_at', 'market_as_of', 'payload'))
        if (type(value['schema']) is not int or value['schema'] != 1
                or type(value['stage']) is not str or type(value['outcome']) is not str
                or value['stage'] not in {'daily_bias', 'us_overnight', 'screener', 'index0001', 'index1001'}
                or value['outcome'] not in {'success', 'missing', 'failed'}
                or type(value['source']) is not str or not value['source']):
            raise ValueError('invalid_regime_stage')
        if value['event_id'] is not None and (type(value['event_id']) is not str or not value['event_id']):
            raise ValueError('invalid_regime_stage_identity')
        for name in ('received_at', 'market_as_of'):
            if value[name] is not None: time(value[name])
        payload = value['payload']
        if value['outcome'] != 'success':
            if payload is not None: raise ValueError('failed_stage_has_payload')
        elif value['stage'] == 'screener':
            keys(payload, ('closes', 'last_bar_date', 'loaded_at'))
            if type(payload['closes']) is not list: raise ValueError('invalid_regime_closes')
            for item in payload['closes']: number(item)
            if payload['last_bar_date'] is not None:
                if date.fromisoformat(payload['last_bar_date']).isoformat() != payload['last_bar_date']:
                    raise ValueError('invalid_regime_bar_date')
            if payload['loaded_at'] is not None: time(payload['loaded_at'])
        else:
            key = {'daily_bias': 'data', 'us_overnight': 'overnight'}.get(value['stage'], 'quote')
            keys(payload, (key,))
            if type(payload[key]) is not dict: raise ValueError('invalid_regime_stage_payload')
        return cls(canonical(value))

    def to_dict(self):
        return json.loads(self.json)


@dataclass(frozen=True)
class RegimeSyncContext:
    json: str

    @classmethod
    def from_dict(cls, value):
        keys, time, _, _ = _helpers()
        keys(value, ('schema', 'captured_at', 'regime_conflict_guard_enabled', 'screener'))
        if (type(value['schema']) is not int or value['schema'] != 1
                or type(value['regime_conflict_guard_enabled']) is not bool):
            raise ValueError('invalid_regime_sync_context')
        time(value['captured_at'])
        screener = value['screener']
        keys(screener, ('regime', 'source', 'event_id', 'observed_at'))
        if (screener['regime'] is not None and type(screener['regime']) is not str
                or type(screener['source']) is not str or not screener['source']
                or screener['event_id'] is not None and type(screener['event_id']) is not str):
            raise ValueError('invalid_regime_sync_screener')
        if screener['observed_at'] is not None: time(screener['observed_at'])
        if not value['regime_conflict_guard_enabled'] and any(
                screener[key] is not None for key in ('regime', 'event_id', 'observed_at')):
            raise ValueError('disabled_regime_guard_has_input')
        return cls(canonical(value))

    def to_dict(self):
        return json.loads(self.json)


@dataclass(frozen=True)
class RegimeApplicationReceipt:
    operation_id: str
    status: str
    reason: str
    committed_version: int
    classifier_operation_id: str
    classifier_version: int


def select_horizon(previous, candidate):
    _, time, _, _ = _helpers()
    if (previous['classified_at'] is not None and candidate['classified_at'] is not None
            and time(candidate['classified_at']) < time(previous['classified_at'])):
        return deepcopy(previous)
    return deepcopy(candidate)


def fold_horizon(state):
    root = state['regime_policy']; baseline = root['horizon_baseline']
    supplied = baseline['supplied']; version = baseline['baseline_version']
    result = {**supplied['horizon'], 'writer_kind': 'baseline',
              'operation_id': supplied['baseline_id'], 'version': version}
    events = []
    for operation, row in state['intraday_policy']['transitions'].items():
        if row['version'] > version and row['disposition'] == 'applied':
            policy = row['after']
            events.append({'level': policy['level'], 'change_pct': policy['kospi_pct'],
                'classified_at': policy['updated_at'], 'writer_kind': 'intraday_5m',
                'operation_id': operation, 'version': row['version']})
    for operation, row in root['noon_caps'].items():
        events.append({**row['candidate'], 'writer_kind': 'noon_index',
                       'operation_id': operation, 'version': row['version']})
    for event in sorted(events, key=lambda item: item['version']):
        result = select_horizon(result, event)
    return result


def validate_horizon(state, version):
    keys, _, _, positive = _helpers()
    root = state['regime_policy']; baseline = root['horizon_baseline']
    keys(baseline, ('supplied', 'digest', 'baseline_version'))
    supplied = RegimeHorizonBaseline.from_dict(baseline['supplied']).to_dict()
    positive(baseline['baseline_version'])
    if (baseline['digest'] != digest(supplied)
            or supplied['account_scope'] != root['baseline']['supplied']['account_scope']
            or not root['baseline']['baseline_version'] < baseline['baseline_version'] <= version
            or supplied['regime_baseline_version'] != root['baseline']['baseline_version']
            or supplied['intraday']['baseline_version'] != state['intraday_policy']['baseline_version']):
        raise ValueError('regime_horizon_baseline_crosslink')
    prior = state['intraday_policy']['baseline']
    for row in sorted(state['intraday_policy']['transitions'].values(), key=lambda item: item['version']):
        if row['version'] < baseline['baseline_version']: prior = row['after']
    if canonical(prior) != canonical(supplied['intraday']['current']):
        raise ValueError('regime_horizon_intraday_crosslink')
    if canonical(root['horizon']) != canonical(fold_horizon(state)):
        raise ValueError('regime_horizon_projection_conflict')
    validate_c3_history(state, version)


def validate_c3_history(state, version):
    from .regime_commands import noon_candidate
    from .index_risk_input import normalize_index_risk
    from .regime_application import validate_application
    from .regime_classifier import prompt_inputs, projected_result
    keys, time, _, _ = _helpers()
    root = state['regime_policy']; baseline_version = root['horizon_baseline']['baseline_version']
    if type(root['noon_caps']) is not dict or type(root['applications']) is not dict:
        raise ValueError('invalid_regime_c3_history')
    records = state.get('risk_sources', {}).get('records', {})
    accepted = {'noon_index': set(), 'llm_regime': set()}
    for operation, source in records.items():
        ticket, terminal = source['ticket'], source['terminal']
        if (ticket['kind'] not in accepted or ticket['admission_version'] <= baseline_version
                or terminal is None or terminal['receipt']['status'] != 'accepted'): continue
        accepted[ticket['kind']].add(operation)
        seal = state['risk_input_seals']['records'][operation]['original']
        payload = terminal['envelope']['payload']
        if ticket['kind'] == 'noon_index':
            row = root['noon_caps'].get(operation)
            if row is None: raise ValueError('regime_noon_history_incomplete')
            keys(row, ('version', 'candidate', 'request_digest', 'outcome_digest', 'seal_digest'))
            fact = validate_noon_envelope(terminal['envelope'], ticket['business_day'], time(terminal['completed_at']))
            candidate = noon_candidate(seal['reads'], fact, time(terminal['envelope']['classified_at']))
            if (fact.outcome != 'success' or row['version'] != terminal['receipt']['committed_version']
                    or row['outcome_digest'] != terminal['receipt']['outcome_digest']
                    or row['request_digest'] != ticket['request_digest'] or row['seal_digest'] != seal['seal_digest']
                    or canonical(candidate) != canonical(row['candidate'])
                    or canonical(candidate) != canonical(payload['candidate'])
                    or canonical(seal['request']['inputs']) != canonical({
                        'observation_json': payload['observation_json'], 'candidate': candidate})):
                raise ValueError('regime_noon_crosslink')
        else:
            inputs = seal['request']['inputs']; now = time(inputs['classified_at'])
            stages = {name: RegimeStageInput.from_dict(value).to_dict() for name, value in inputs['stages'].items()}
            prompt, meta = prompt_inputs(inputs['label'], stages, seal['reads']['versioned_policies'], now,
                captured_at=time(inputs['context']['captured_at']))
            raw = json.loads(payload['raw_result_json'])
            if (type(raw) is not dict or not raw or 'error' in raw or canonical(raw) != payload['raw_result_json']
                    or prompt != inputs['prompt'] or canonical(meta) != canonical(inputs['input_meta'])
                    or canonical(projected_result(raw, meta, now)) != canonical(payload['result'])):
                raise ValueError('regime_classifier_calculation_conflict')
    if set(root['noon_caps']) != accepted['noon_index']:
        raise ValueError('regime_noon_history_incomplete')
    immediate = set()
    for operation, row in root['applications'].items():
        validate_application(state, operation, row, version)
        if row['kind'] == 'classifier': immediate.add(row['classifier_ref']['operation_id'])
    if immediate != accepted['llm_regime']:
        raise ValueError('regime_classifier_application_incomplete')


def validate_noon_envelope(envelope, business_day, now):
    from .index_risk_input import normalize_index_risk
    keys, time, _, _ = _helpers()
    payload = envelope['payload']
    keys(payload, ('level', 'change_pct', 'observation_json', 'candidate'))
    observation = json.loads(payload['observation_json'])
    if type(observation) is not dict or type(observation.get('fields')) is not dict:
        raise ValueError('invalid_regime_noon_observation')
    quote = {name: fact.get('value') if type(fact) is dict else None for name, fact in observation['fields'].items()}
    quote['_observation'] = observation
    fact = normalize_index_risk(quote, now=now, business_day=business_day)
    if (fact.outcome != 'success' or fact.observation_json != payload['observation_json']
            or envelope['source'] != fact.source or envelope['source_event_id'] != fact.source_event_id
            or envelope['received_at'] != fact.received_at.isoformat() or envelope['market_as_of'] is not None
            or envelope['recovery_until'] is not None or envelope['classified_at'] != payload['candidate'].get('classified_at')
            or canonical(payload['change_pct']) != canonical(fact.change_pct)
            or payload['level'] != payload['candidate'].get('level')):
        raise ValueError('regime_noon_original_metadata_conflict')
    return fact


def validate_source_append(state, before, operation, version):
    root = state['regime_policy']; source = state['risk_sources']['records'][operation]
    kind = source['ticket']['kind']
    mutable = {'horizon', 'noon_caps'} if kind == 'noon_index' else {'applications'}
    if set(root) != set(before) or any(canonical(root[key]) != canonical(value)
            for key, value in before.items() if key not in mutable):
        raise ValueError('regime_prior_root_changed')
    collection, key = ('noon_caps', operation) if kind == 'noon_index' else ('applications', 'classifier:' + operation)
    if (key in before[collection] or set(root[collection]) != set(before[collection]) | {key}
            or any(canonical(root[collection][op]) != canonical(row) for op, row in before[collection].items())):
        raise ValueError('regime_prior_history_changed')
    validate_horizon(state, version)


def validate_intraday_horizon_append(state, before, operation, version):
    """C2 prior-root 불변을 유지하면서 검증된5분 projection만 허용한다."""
    from .intraday_owner import validate_intraday_policy
    root = state['regime_policy']
    if (set(root) != set(before) or any(canonical(root[key]) != canonical(value)
            for key, value in before.items() if key != 'horizon')):
        raise ValueError('regime_prior_root_changed')
    row = state['intraday_policy']['transitions'].get(operation)
    if row is None or row['version'] != version:
        raise ValueError('regime_horizon_intraday_crosslink')
    validate_intraday_policy(state, version)
    validate_horizon(state, version)
