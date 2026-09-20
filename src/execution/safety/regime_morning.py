"""Typed morning text facts and chronological policy history; no external I/O."""
from copy import deepcopy
from dataclasses import dataclass
from datetime import date
import json
from zoneinfo import ZoneInfo

from .policy_generations import canonical
from .protection_recovery import digest

READS = ('regime_policy.trend_state', 'regime_policy.horizon')
KIND = 'llm_morning_diagnosis'
RULE = digest({'rule': 'morning-text-v1', 'defense': ['bull', 'sideways'],
               'attack': ['bear', 'sideways'], 'blocking': ['crash', 'severe'], 'max_tokens': 150})
_FIELDS = ('knowledge', 'assessment', 'assessment_day', 'open_expectation', 'open_expectation_as_of')


def _helpers():
    from .regime_owner import _keys, _time, _positive
    return _keys, _time, _positive


def validate_display(value):
    keys, time, _ = _helpers()
    keys(value, _FIELDS)
    if value['knowledge'] not in ('known', 'unknown'):
        raise ValueError('invalid_morning_knowledge')
    for name in ('assessment', 'open_expectation'):
        if value[name] is not None and (type(value[name]) is not str or not value[name].strip()):
            raise ValueError('invalid_morning_text')
    if (value['assessment'] is None) != (value['assessment_day'] is None):
        raise ValueError('invalid_morning_assessment_day')
    if value['assessment_day'] is not None:
        if (type(value['assessment_day']) is not str
                or date.fromisoformat(value['assessment_day']).isoformat() != value['assessment_day']):
            raise ValueError('invalid_morning_day')
    if (value['open_expectation'] is None) != (value['open_expectation_as_of'] is None):
        raise ValueError('invalid_morning_expectation_time')
    if value['open_expectation_as_of'] is not None:
        when = time(value['open_expectation_as_of']).astimezone(ZoneInfo('Asia/Seoul'))
        if (when.date().isoformat() != value['assessment_day']
                or value['open_expectation'] != value['assessment']):
            raise ValueError('invalid_morning_expectation_crosslink')
    if value['knowledge'] == 'unknown' and any(value[name] is not None for name in _FIELDS[1:]):
        raise ValueError('unknown_morning_has_result')


@dataclass(frozen=True)
class RegimeMorningBaseline:
    json: str

    @classmethod
    def from_dict(cls, value):
        keys, time, positive = _helpers()
        keys(value, ('schema', 'baseline_id', 'account_scope', 'business_day', 'generation',
            'fence_id', 'evidence', 'regime_baseline_version', 'horizon_baseline_version', 'morning'))
        if (type(value['schema']) is not int or value['schema'] != 1
                or type(value['generation']) is not int or value['generation'] < 0):
            raise ValueError('invalid_morning_baseline')
        for name in ('baseline_id', 'account_scope'):
            if type(value[name]) is not str or not value[name].strip():
                raise ValueError('invalid_morning_identity')
        if value['fence_id'] is not None and (type(value['fence_id']) is not str or not value['fence_id']):
            raise ValueError('invalid_morning_fence')
        if (type(value['business_day']) is not str
                or date.fromisoformat(value['business_day']).isoformat() != value['business_day']):
            raise ValueError('invalid_morning_day')
        keys(value['evidence'], ('source', 'event_id', 'observed_at'))
        for name in ('source', 'event_id'):
            if type(value['evidence'][name]) is not str or not value['evidence'][name].strip():
                raise ValueError('invalid_morning_evidence')
        time(value['evidence']['observed_at'])
        positive(value['regime_baseline_version']); positive(value['horizon_baseline_version'])
        validate_display(value['morning'])
        return cls(canonical(value))

    def to_dict(self): return json.loads(self.json)


@dataclass(frozen=True)
class RegimeMorningStageInput:
    json: str

    @classmethod
    def from_dict(cls, value):
        keys, time, _ = _helpers()
        keys(value, ('schema', 'stage', 'outcome', 'source', 'event_id', 'received_at', 'market_as_of', 'payload'))
        if (type(value['schema']) is not int or value['schema'] != 1
                or value['stage'] not in ('theme', 'overtime', 'news', 'macro')
                or value['outcome'] not in ('success', 'missing', 'failed')
                or type(value['source']) is not str or not value['source']):
            raise ValueError('invalid_morning_stage')
        if value['event_id'] is not None and (type(value['event_id']) is not str or not value['event_id']):
            raise ValueError('invalid_morning_stage_id')
        for name in ('received_at', 'market_as_of'):
            if value[name] is not None: time(value[name])
        payload = value['payload']
        if value['outcome'] != 'success':
            if payload is not None: raise ValueError('failed_morning_stage_has_payload')
        elif value['stage'] == 'overtime':
            keys(payload, ('symbol', 'quote'))
            if type(payload['symbol']) is not str or not payload['symbol'] or type(payload['quote']) is not dict:
                raise ValueError('invalid_morning_quote')
        else:
            keys(payload, ('text',))
            if type(payload['text']) is not str: raise ValueError('invalid_morning_stage_text')
        return cls(canonical(value))

    def to_dict(self): return json.loads(self.json)


@dataclass(frozen=True)
class RegimeMorningResult:
    status: str
    reason: str
    receipt: object = None


def prompt_for(trend, stages, symbols):
    """Original text prompt, computed exclusively from sealed detached facts."""
    keys, _, _ = _helpers()
    keys(stages, ('theme', 'overtime', 'news', 'macro'))
    if type(symbols) is not list or len(symbols) > 5 or any(type(s) is not str or not s for s in symbols):
        raise ValueError('invalid_morning_symbols')
    if type(stages['overtime']) is not list or len(stages['overtime']) != len(symbols):
        raise ValueError('invalid_morning_overtime_count')
    texts = {}
    for name in ('theme', 'news', 'macro'):
        stage = RegimeMorningStageInput.from_dict(stages[name]).to_dict()
        if stage['stage'] != name: raise ValueError('morning_stage_mismatch')
        texts[name] = stage['payload']['text'] if stage['outcome'] == 'success' else ''
    quotes = {}
    for symbol, value in zip(symbols, stages['overtime']):
        stage = RegimeMorningStageInput.from_dict(value).to_dict()
        if stage['stage'] != 'overtime': raise ValueError('morning_stage_mismatch')
        if stage['outcome'] == 'success':
            if stage['payload']['symbol'] != symbol: raise ValueError('morning_symbol_mismatch')
            quotes[symbol] = stage['payload']['quote']
    from ...core.market_regime import morning_diagnosis_prompt
    return morning_diagnosis_prompt(trend['mid_regime'], trend['regime_data'],
        texts['theme'], quotes, texts['news'], texts['macro'])


def calculation(before, horizon, text, when):
    from ...core.market_regime import cap_regime_by_intraday_risk, morning_mid_regime
    _, time, _ = _helpers()
    kst = ZoneInfo('Asia/Seoul')
    risk = horizon['level'] if (horizon['classified_at']
        and time(horizon['classified_at']).astimezone(kst).date() == when.astimezone(kst).date()) else None
    after = deepcopy(before)
    after['mid_regime'] = morning_mid_regime(before['mid_regime'], risk, text)
    return after, cap_regime_by_intraday_risk(after['mid_regime'], risk)


def validate_envelope(state, ticket, envelope):
    keys, time, _ = _helpers()
    root = state['regime_policy']
    if root.get('schema') != 3: raise ValueError('morning_baseline_required')
    seal = state.get('risk_input_seals', {}).get('records', {}).get(ticket.operation_id, {}).get('original')
    if seal is None: raise ValueError('morning_input_seal_required')
    inputs, policies = seal['request']['inputs'], seal['reads']['versioned_policies']
    keys(inputs, ('stages', 'symbols', 'prompt', 'task', 'max_tokens', 'rule_digest'))
    if (inputs['task'] != 'MARKET_ANALYSIS' or type(inputs['max_tokens']) is not int
            or inputs['max_tokens'] != 150 or inputs['rule_digest'] != RULE
            or set(policies) != set(READS) or seal['request']['source_lanes'] != ['index_trend']):
        raise ValueError('morning_input_contract')
    source = seal['reads']['sources']['index_trend']
    if not source or not source['terminal'] or source['terminal']['receipt']['status'] != 'accepted':
        raise ValueError('morning_index_source_required')
    before = policies['regime_policy.trend_state']['value']
    ref = before['source_refs']['trend']
    if ref != {'operation_id': source['ticket']['operation_id'],
               'committed_version': source['terminal']['receipt']['committed_version']}:
        raise ValueError('morning_index_crosslink')
    if prompt_for(before, inputs['stages'], inputs['symbols']) != inputs['prompt']:
        raise ValueError('morning_prompt_conflict')
    payload = envelope['payload']
    keys(payload, ('raw_text', 'assessment', 'before', 'after', 'engine_regime', 'morning_before', 'morning_after'))
    if (type(payload['raw_text']) is not str or not payload['raw_text'].strip()
            or payload['assessment'] != payload['raw_text'].strip()):
        raise ValueError('invalid_morning_response')
    when = time(envelope['classified_at'])
    if (when.date().isoformat() != ticket.business_day or envelope['source'] != 'llm.complete'
            or envelope['source_event_id'] != ticket.operation_id
            or envelope['received_at'] != envelope['classified_at'] or envelope['market_as_of'] is not None
            or envelope['recovery_until'] is not None):
        raise ValueError('morning_original_metadata_conflict')
    after, engine = calculation(before, policies['regime_policy.horizon']['value'], payload['assessment'], when)
    expected = {'knowledge': 'known', 'assessment': payload['assessment'], 'assessment_day': ticket.business_day,
        'open_expectation': payload['assessment'], 'open_expectation_as_of': when.isoformat(),
        'source_ref': {'operation_id': ticket.operation_id, 'committed_version': None}}
    if (canonical(before) != canonical(payload['before']) or canonical(after) != canonical(payload['after'])
            or engine != payload['engine_regime'] or canonical(expected) != canonical(payload['morning_after'])):
        raise ValueError('morning_calculation_conflict')
    prior = payload['morning_before']
    keys(prior, (*_FIELDS, 'source_ref')); validate_display({k: prior[k] for k in _FIELDS})
    if prior['knowledge'] != 'known' or prior['assessment_day'] == ticket.business_day:
        raise ValueError('morning_already_done_or_unknown')
    return payload


def validate_transition(state, operation, row, previous, last_version, version):
    from .risk_sources import _ticket
    keys, _, _ = _helpers()
    keys(row, ('kind', 'version', 'before', 'after', 'sidecar_before', 'sidecar_after',
        'request_digest', 'outcome_digest', 'seal_digest', 'rule_digest', 'engine_regime', 'morning_before', 'morning_after'))
    source = state['risk_sources']['records'].get(operation)
    if source is None: raise ValueError('morning_source_missing')
    ticket = _ticket(source); terminal = source['terminal'] or {}; receipt = terminal.get('receipt', {})
    seal = state['risk_input_seals']['records'].get(operation, {}).get('original', {})
    if (row['kind'] != KIND or ticket.kind != KIND or type(row['version']) is not int
            or not last_version < row['version'] <= version
            or ticket.admission_version <= state['regime_policy']['morning_baseline']['baseline_version']
            or receipt.get('status') != 'accepted' or receipt.get('committed_version') != row['version']
            or receipt.get('outcome_digest') != row['outcome_digest'] or ticket.request_digest != row['request_digest']
            or seal.get('seal_digest') != row['seal_digest'] or row['rule_digest'] != RULE
            or canonical(previous) != canonical(row['before']) or row['sidecar_before'] is not row['sidecar_after']):
        raise ValueError('morning_transition_crosslink')
    payload = validate_envelope(state, ticket, terminal['envelope'])
    expected = deepcopy(payload['morning_after']); expected['source_ref']['committed_version'] = row['version']
    if (canonical(expected) != canonical(row['morning_after'])
            or any(canonical(payload[k]) != canonical(row[k]) for k in
                   ('before', 'after', 'engine_regime', 'morning_before'))):
        raise ValueError('morning_transition_payload_conflict')
    return row['after'], {'regime': row['engine_regime'], 'operation_id': operation, 'version': row['version']}


def validate_history(state, version):
    keys, time, positive = _helpers()
    root = state['regime_policy']; baseline = root['morning_baseline']
    keys(baseline, ('supplied', 'digest', 'baseline_version'))
    supplied = RegimeMorningBaseline.from_dict(baseline['supplied']).to_dict()
    positive(baseline['baseline_version'])
    if (baseline['digest'] != digest(supplied)
            or supplied['account_scope'] != root['baseline']['supplied']['account_scope']
            or supplied['regime_baseline_version'] != root['baseline']['baseline_version']
            or supplied['horizon_baseline_version'] != root['horizon_baseline']['baseline_version']
            or not root['horizon_baseline']['baseline_version'] < baseline['baseline_version'] <= version):
        raise ValueError('morning_baseline_crosslink')
    current = {**supplied['morning'], 'source_ref': None}
    days = {current['assessment_day']} if current['assessment_day'] is not None else set()
    for row in sorted(root['transitions'].values(), key=lambda item: item['version']):
        if row['kind'] != KIND: continue
        if (canonical(row['morning_before']) != canonical(current)
                or row['morning_after']['assessment_day'] in days):
            raise ValueError('morning_history_conflict')
        current = row['morning_after']; days.add(current['assessment_day'])
    if canonical(root['morning']) != canonical(current): raise ValueError('morning_projection_conflict')


def append_source(state, ticket, envelope, version):
    root = state['regime_policy']; before = deepcopy(root['morning'])
    payload = validate_envelope(state, ticket, envelope)
    if canonical(payload['morning_before']) != canonical(before): raise ValueError('morning_state_changed')
    after = deepcopy(payload['morning_after']); after['source_ref']['committed_version'] = version
    seal = state['risk_input_seals']['records'][ticket.operation_id]['original']
    sidecar = state['entry_policy_effects']['sidecar_active']
    root['transitions'][ticket.operation_id] = {
        'kind': KIND, 'version': version, 'before': payload['before'], 'after': payload['after'],
        'sidecar_before': sidecar, 'sidecar_after': sidecar, 'request_digest': ticket.request_digest,
        'outcome_digest': digest(envelope), 'seal_digest': seal['seal_digest'], 'rule_digest': RULE,
        'engine_regime': payload['engine_regime'], 'morning_before': before, 'morning_after': after}
    root['trend_state'] = deepcopy(payload['after']); root['morning'] = after
    root['engine_projection'] = {'regime': payload['engine_regime'], 'operation_id': ticket.operation_id, 'version': version}
    return state
