"""Owner-issued read evidence, not market validity or permission to trade.

Trusted callers choose the required reads; opt-in does not authenticate that choice or
the supplied input facts. No JSONPath, external I/O, new database or startup authority.

Legacy policy_reads remain VALUE-ONLY. Explicit versioned_policy_reads bind the
owner's registered change history; neither form grants persistent consumption
authority. Source selections include real admission/terminal/conflict versions.
"""
from __future__ import annotations

import asyncio
from contextlib import nullcontext
from copy import deepcopy
from dataclasses import dataclass
import json
import math

from .protection_recovery import digest

ROOT = 'risk_input_seals'
_POLICIES = {
    'regime_policy.horizon': ('regime_policy', 'horizon'),
    'regime_policy.trend_state': ('regime_policy', 'trend_state'),
    'intraday_policy.current': ('intraday_policy', 'current'),
    'protection.config': ('protection', 'config'),
    'protection.current_regime': ('protection', 'current_regime'),
    'protection.intraday_crash_level': ('protection', 'intraday_crash_level'),
    'entry_policy_effects.sidecar_active': ('entry_policy_effects', 'sidecar_active'),
}


def _json(value):
    def check(item):
        if item is None or type(item) in (str, bool, int):
            return
        if type(item) is float and math.isfinite(item):
            return
        if type(item) is list:
            for child in item:
                check(child)
            return
        if type(item) is dict and all(type(key) is str for key in item):
            for child in item.values():
                check(child)
            return
        raise ValueError('invalid_input_seal_json')
    check(value)
    return json.dumps(value, allow_nan=False, sort_keys=True, separators=(',', ':'), ensure_ascii=False)


@dataclass(frozen=True)
class InputSealReceipt:
    operation_id: str
    ticket_digest: str
    request_digest: str
    seal_digest: str
    status: str
    reason: str
    committed_version: int
    inputs_json: str
    reads_json: str


def _names(values):
    if type(values) is not tuple or any(type(value) is not str or not value or value != value.strip()
                                       for value in values) or len(set(values)) != len(values):
        raise ValueError('invalid_input_seal_selection')
    return sorted(values)


def _request(ticket, source_lanes, retained_sources, policy_reads, inputs, versioned_policy_reads=(), expected_reads_json=None):
    from .risk_sources import _LANES
    if type(inputs) is not dict:
        raise ValueError('input_seal_bundle_required')
    lanes, retained, policies = map(_names, (source_lanes, retained_sources, policy_reads))
    versioned = _names(versioned_policy_reads)
    if (not set(lanes) <= _LANES or ticket.lane in lanes
            or not set(policies) <= _POLICIES.keys() or ticket.operation_id in retained
            or not set(versioned) <= _POLICIES.keys() or set(versioned) & set(policies)):
        raise ValueError('unsupported_input_seal_selection')
    request = {'ticket_digest': ticket.request_digest, 'source_lanes': lanes,
            'retained_sources': retained, 'policy_reads': policies,
            'inputs': json.loads(_json(inputs))}
    if versioned:
        request.update(schema=2, versioned_policy_reads=versioned)
    if expected_reads_json is not None:
        if type(expected_reads_json) is not str:
            raise ValueError('invalid_captured_reads')
        expected = json.loads(expected_reads_json)
        _json(expected)
        request.update(schema=3, versioned_policy_reads=versioned, expected_reads=expected)
        _expected_shape(request)
    return request


def _expected_shape(request):
    from .risk_sources import _keys
    facts = request['expected_reads']
    _keys(facts, ('sources', 'retained', 'policies', 'versioned_policies'))
    for field, names in (('sources', 'source_lanes'), ('retained', 'retained_sources'),
                         ('policies', 'policy_reads'), ('versioned_policies', 'versioned_policy_reads')):
        if type(facts[field]) is not dict or set(facts[field]) != set(request[names]):
            raise ValueError('captured_reads_selection_conflict')
    for field in ('policies', 'versioned_policies'):
        for name, fact in facts[field].items():
            extra = ('selector', 'registered_version', 'generation') if field == 'versioned_policies' else ()
            _keys(fact, ('present', 'value', 'digest', *extra))
            if (type(fact['present']) is not bool or (not fact['present'] and fact['value'] is not None)
                    or fact['digest'] != digest({'present': fact['present'], 'value': fact['value']})):
                raise ValueError('captured_policy_digest_conflict')
            if extra and (fact['selector'] != name or type(fact['generation']) is not int
                    or type(fact['registered_version']) is not int
                    or not 0 < fact['registered_version'] <= fact['generation']):
                raise ValueError('captured_policy_generation_conflict')


def _validate_expected(state, request):
    """Validate supplied facts against real retained history, not today's values."""
    from .policy_generations import versioned_fact
    _expected_shape(request)
    facts = request['expected_reads']
    for name, fact in facts['versioned_policies'].items():
        if _json(fact) != _json(versioned_fact(state, name, cutoff=fact['generation'] + 1)):
            raise ValueError('captured_policy_history_conflict')
    records, seals = state.get('risk_sources', {}).get('records', {}), state.get(ROOT, {}).get('records', {})
    for field in ('sources', 'retained'):
        for name, fact in facts[field].items():
            if fact is None and field == 'sources': continue
            if type(fact) is not dict or type(fact.get('ticket')) is not dict:
                raise ValueError('invalid_captured_source')
            operation = fact['ticket'].get('operation_id'); row = records.get(operation)
            if (row is None or fact['ticket'] != row['ticket']
                    or (field == 'sources' and fact['ticket']['lane'] != name)
                    or (field == 'retained' and (operation != name or fact['ticket']['lane'] not in request['source_lanes']))):
                raise ValueError('captured_source_selection_conflict')
            cutoffs = {row['ticket']['admission_version'] + 1}
            cutoffs.update(row[key]['receipt']['committed_version'] + 1 for key in ('terminal', 'conflict') if row[key])
            seal = seals.get(operation)
            if seal and seal['conflict']: cutoffs.add(seal['conflict']['version'] + 1)
            if not any(_json(fact) == _json(_source_at(row, cutoff, seals)) for cutoff in cutoffs):
                raise ValueError('captured_source_history_conflict')
            if field == 'retained' and (not fact['terminal'] or fact['terminal']['receipt']['status'] != 'accepted'):
                raise ValueError('invalid_retained_input_source')


def _source_at(row, cutoff, seals):
    if row is None or (cutoff is not None and row['ticket']['admission_version'] >= cutoff):
        return None
    value = deepcopy(row)
    for key in ('terminal', 'conflict'):
        if value[key] and cutoff is not None and value[key]['receipt']['committed_version'] >= cutoff:
            value[key] = None
    seal = seals.get(row['ticket']['operation_id'])
    conflict = seal['conflict'] if seal else None
    value['input_seal_conflict'] = (
        {'version': conflict['version'], 'request_digest': conflict['request_digest']}
        if conflict and (cutoff is None or conflict['version'] < cutoff) else None)
    return value


def _retained_reads(state, request, *, cutoff=None, seals=None):
    records = state['risk_sources']['records']
    seals = state.get(ROOT, {}).get('records', {}) if seals is None else seals
    retained = {}
    for operation in request['retained_sources']:
        row = _source_at(records.get(operation), cutoff, seals)
        if (not row or row['ticket']['lane'] not in request['source_lanes']
                or not row['terminal'] or row['terminal']['receipt']['status'] != 'accepted'):
            raise ValueError('invalid_retained_input_source')
        retained[operation] = row
    return retained


def _source_reads(state, request, *, cutoff=None):
    """정책과 분리된 원 source 사실. 역사 cutoff의 의미도 그대로 보존한다."""
    records = state['risk_sources']['records']
    seals = state.get(ROOT, {}).get('records', {})
    facts = {'sources': {}, 'retained': {}}
    for lane in request['source_lanes']:
        rows = [_source_at(row, cutoff, seals) for row in records.values() if row['ticket']['lane'] == lane]
        rows = [row for row in rows if row is not None]
        facts['sources'][lane] = max(rows, key=lambda row: row['ticket']['sequence']) if rows else None
    facts['retained'] = _retained_reads(state, request, cutoff=cutoff, seals=seals)
    return facts


def _reads(state, ticket, request, *, cutoff=None, policies=True):
    facts = {**_source_reads(state, request, cutoff=cutoff), 'policies': {}}
    if request.get('schema') in (2, 3):
        from .policy_generations import versioned_fact
        facts['versioned_policies'] = {
            name: versioned_fact(state, name, cutoff=cutoff)
            for name in request['versioned_policy_reads']}
    if policies:
        for name in request['policy_reads']:
            root, field = _POLICIES[name]
            container = state.get(root, {})
            if type(container) is not dict:
                raise ValueError('invalid_input_policy_root')
            present = field in container
            value = deepcopy(container[field]) if present else None
            facts['policies'][name] = {'present': present, 'value': value,
                                      'digest': digest({'present': present, 'value': value})}
    return facts


def _context_reason(runtime, state, ticket):
    from .day_recovery import scope_reason
    if (ticket.business_day != state['risk']['day']
            or ticket.business_day != runtime._now().date().isoformat()
            or ticket.generation != runtime._day_generation
            or ticket.fence_id != runtime._day_fence_id or runtime.day_admission_closed
            or ticket.account_scope != runtime.account_scope or scope_reason(state, runtime.account_scope)):
        return 'day_or_generation'
    if state['risk_sources']['latest'].get(ticket.lane) != ticket.operation_id:
        return 'superseded'
    return ''


def _receipt(operation, row):
    original = row['original']
    result = row['conflict'] or original
    return InputSealReceipt(operation, original['request']['ticket_digest'],
        result['request_digest'], original['seal_digest'],
        result['status'], result['reason'], result['version'],
        _json(original['request']['inputs']), _json(original['reads']))


async def seal_input(coordinator, ticket, *, source_lanes=(), retained_sources=(),
                     policy_reads=(), versioned_policy_reads=(), inputs, scope_token=None, expected_reads_json=None):
    from .risk_sources import RefreshTicket, _ticket_dict, validate_risk_sources
    if type(ticket) is not RefreshTicket:
        raise TypeError('RefreshTicket_required')
    runtime = coordinator.runtime
    request = _request(ticket, source_lanes, retained_sources, policy_reads, inputs, versioned_policy_reads, expected_reads_json)
    request_digest = digest(request)
    initial = runtime.owner.state
    if request.get('schema') == 3:
        _validate_expected(initial, request)
    source = initial.get('risk_sources', {}).get('records', {}).get(ticket.operation_id)
    if source is None or source['ticket'] != _ticket_dict(ticket):
        raise ValueError('risk_source_ticket_mismatch')
    if (source['terminal'] and ticket.operation_id not in initial.get(ROOT, {}).get('records', {})):
        raise ValueError('input_seal_source_terminal')
    # 최초 유효 seal의 잘못된 선택은 결과 저장 접수 전에 거부한다.
    # 기존 seal 충돌과 이미 stale인 요청은 아래의 durable 판정을 유지한다.
    if (request.get('versioned_policy_reads')
            and ticket.operation_id not in initial.get(ROOT, {}).get('records', {})
            and not _context_reason(runtime, initial, ticket)):
        registered = initial.get('policy_generations', {}).get('selectors', {})
        if any(name not in registered for name in request['versioned_policy_reads']):
            raise ValueError('policy_generation_unregistered')
    if (ticket.operation_id not in initial.get(ROOT, {}).get('records', {})
            and not source['terminal'] and not source['conflict']
            and not _context_reason(runtime, initial, ticket)):
        _retained_reads(initial, request)
    with (runtime.command_scope() if scope_token is None else nullcontext(scope_token)) as token:
        async def execute():
            def reduce(state):
                source = state.get('risk_sources', {}).get('records', {}).get(ticket.operation_id)
                if source is None or source['ticket'] != _ticket_dict(ticket):
                    raise ValueError('risk_source_ticket_mismatch')
                root = state.setdefault(ROOT, {'schema': 1, 'records': {}})
                prior = root['records'].get(ticket.operation_id)
                if prior:
                    if prior['conflict'] or prior['original']['request_digest'] == request_digest:
                        return state
                    prior['conflict'] = {'request': request, 'request_digest': request_digest,
                        'version': runtime.owner.version + 1, 'status': 'conflict', 'reason': 'reseal_conflict'}
                else:
                    reason = _context_reason(runtime, state, ticket)
                    if source['terminal'] or source['conflict']:
                        raise ValueError('input_seal_source_terminal')
                    reads = {} if reason else _reads(state, ticket, request)
                    if not reason and request.get('schema') == 3 and _json(reads) != _json(request['expected_reads']):
                        reason = 'captured_reads_changed'
                    body = {'request': request, 'request_digest': request_digest, 'reads': reads,
                        'version': runtime.owner.version + 1, 'status': 'stale' if reason else 'sealed',
                        'reason': reason, 'sealed_at': runtime._now().isoformat()}
                    root['records'][ticket.operation_id] = {
                        'original': {**body, 'seal_digest': digest(body)}, 'conflict': None}
                validate_risk_sources(state, runtime.owner.version + 1)
                return state
            await runtime.owner.mutate('risk-seal:' + ticket.request_digest + ':' + request_digest, reduce)
            return True
        await asyncio.shield(runtime.start_command_result(token, execute))
        return _receipt(ticket.operation_id, runtime.owner.state[ROOT]['records'][ticket.operation_id])


def input_seal_current(state, ticket):
    """Evaluate before the completion hook; own terminal publication is not a read."""
    source = state['risk_sources']['records'][ticket.operation_id]
    row = state.get(ROOT, {}).get('records', {}).get(ticket.operation_id)
    if row is None:
        return not source['request'].get('require_seal', False)
    original = row['original']
    if row['conflict'] or original['status'] != 'sealed':
        return False
    try:
        return _json(_reads(state, ticket, original['request'])) == _json(original['reads'])
    except ValueError:
        return False


def validate_risk_input_seals(state, version):
    """Historical source crosslinks use seal-time state, never current terminal status.

    Policy values are detached historical facts with canonical digests, not authenticated
    against arbitrary coordinated checkpoint rewrites. Relevant current reads are checked
    before completion; a successful hook may legitimately change those policy values.
    """
    from .risk_sources import _keys, _ticket, _time
    from .policy_generations import validate_policy_history
    validate_policy_history(state, version)
    sources = state.get('risk_sources', {}).get('records', {})
    root = state.get(ROOT)
    if ROOT not in state:
        records = {}
    else:
        _keys(root, ('schema', 'records'))
        if type(root['schema']) is not int or root['schema'] != 1 or type(root['records']) is not dict:
            raise ValueError('invalid_input_seal_root')
        records = root['records']
    used_versions = {row['ticket']['admission_version'] for row in sources.values()}
    used_versions.update(row[key]['receipt']['committed_version'] for row in sources.values()
                         for key in ('terminal', 'conflict') if row[key] is not None)
    for operation, row in records.items():
        _keys(row, ('original', 'conflict'))
        source = sources.get(operation)
        if source is None:
            raise ValueError('input_seal_source_missing')
        ticket = _ticket(source)
        original = row['original']
        _keys(original, ('request', 'request_digest', 'reads', 'version', 'status', 'reason', 'sealed_at', 'seal_digest'))
        for fact in [original, *([row['conflict']] if row['conflict'] is not None else [])]:
            if fact is not original:
                _keys(fact, ('request', 'request_digest', 'version', 'status', 'reason'))
            request = fact['request']
            names = ('source_lanes', 'retained_sources', 'policy_reads')
            extra = (('schema', 'versioned_policy_reads', 'expected_reads') if request.get('schema') == 3
                     else ('schema', 'versioned_policy_reads') if 'schema' in request else ())
            _keys(request, ('ticket_digest', *names, 'inputs', *extra))
            if extra and (type(request['schema']) is not int or request['schema'] not in (2, 3)):
                raise ValueError('invalid_input_seal_schema')
            if any(type(request[name]) is not list for name in (*names, *(('versioned_policy_reads',) if extra else ()))):
                raise ValueError('invalid_input_seal_request')
            expected = _request(ticket, tuple(request['source_lanes']), tuple(request['retained_sources']),
                                tuple(request['policy_reads']), request['inputs'],
                                tuple(request.get('versioned_policy_reads', ())),
                                _json(request['expected_reads']) if request.get('schema') == 3 else None)
            if request.get('schema') == 3: _validate_expected(state, request)
            if _json(expected) != _json(request) or fact['request_digest'] != digest(request):
                raise ValueError('input_seal_request_conflict')
            if (type(fact['version']) is not int or not ticket.admission_version < fact['version'] <= version
                    or fact['version'] in used_versions):
                raise ValueError('invalid_input_seal_version')
            used_versions.add(fact['version'])
        body = {key: value for key, value in original.items() if key != 'seal_digest'}
        when = _time(original['sealed_at'])
        if original['seal_digest'] != digest(body) or when < _time(ticket.begun_at):
            raise ValueError('input_seal_digest_conflict')
        captured_changed = original['status'] == 'stale' and original['reason'] == 'captured_reads_changed'
        if original['status'] == 'sealed' or captured_changed:
            if ((original['reason'] and not captured_changed) or when.date().isoformat() != ticket.business_day
                    or (captured_changed and original['request'].get('schema') != 3)):
                raise ValueError('invalid_input_seal_context')
            if original['request'].get('schema') == 3:
                same = _json(original['reads']) == _json(original['request']['expected_reads'])
                if same == captured_changed: raise ValueError('captured_reads_status_conflict')
            facts = original['reads']
            versioned_keys = ('versioned_policies',) if original['request'].get('schema') in (2, 3) else ()
            _keys(facts, ('sources', 'retained', 'policies', *versioned_keys))
            expected = _reads(state, ticket, original['request'], cutoff=original['version'], policies=False)
            if any(_json(facts[key]) != _json(expected[key]) for key in versioned_keys):
                raise ValueError('input_seal_policy_generation_crosslink_conflict')
            if (_json(facts['sources']) != _json(expected['sources'])
                    or _json(facts['retained']) != _json(expected['retained'])
                    or type(facts['policies']) is not dict
                    or set(facts['policies']) != set(original['request']['policy_reads'])):
                raise ValueError('input_seal_source_crosslink_conflict')
            for fact in facts['policies'].values():
                _keys(fact, ('present', 'value', 'digest'))
                if (type(fact['present']) is not bool or (not fact['present'] and fact['value'] is not None)
                        or fact['digest'] != digest({'present': fact['present'], 'value': fact['value']})):
                    raise ValueError('input_seal_policy_digest_conflict')
            # Own source must have been pending and latest when the seal was issued.
            rows = [r for r in sources.values() if r['ticket']['lane'] == ticket.lane
                    and r['ticket']['admission_version'] < original['version']]
            if (max(rows, key=lambda r: r['ticket']['sequence']) is not source
                    or (source['terminal'] and source['terminal']['receipt']['committed_version'] < original['version'])):
                raise ValueError('input_seal_historical_context_conflict')
        elif (original['status'] != 'stale' or original['reason'] not in {
                'day_or_generation', 'superseded', 'source_terminal'} or original['reads'] != {}):
            raise ValueError('invalid_input_seal_status')
        if row['conflict']:
            conflict = row['conflict']
            if (conflict['version'] <= original['version'] or conflict['status'] != 'conflict'
                    or conflict['reason'] != 'reseal_conflict'
                    or conflict['request_digest'] == original['request_digest']):
                raise ValueError('invalid_input_seal_conflict')
    for operation, source in sources.items():
        terminal = source['terminal']
        if terminal is None or terminal['receipt']['status'] != 'accepted':
            continue
        row = records.get(operation)
        if row is None:
            if source['request'].get('require_seal', False):
                raise ValueError('required_input_seal_missing')
            continue
        original, completed = row['original'], terminal['receipt']['committed_version']
        if (original['status'] != 'sealed' or original['version'] >= completed
                or (row['conflict'] and row['conflict']['version'] < completed)):
            raise ValueError('accepted_input_seal_conflict')
        facts = _reads(state, _ticket(source), original['request'], cutoff=completed, policies=False)
        if any(_json(facts[key]) != _json(original['reads'][key]) for key in ('sources', 'retained')):
            raise ValueError('accepted_input_seal_source_changed')
        if ('versioned_policies' in facts
                and _json(facts['versioned_policies']) != _json(original['reads']['versioned_policies'])):
            raise ValueError('accepted_input_seal_policy_generation_changed')
