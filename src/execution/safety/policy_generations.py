"""명시 등록한 정책만 실제 owner commit에서 추적하는 변경 이력.

등록은 관측 시작이며 정책 유효성/거래 허가가 아니다. 역사 검증과 최종
현재값 검증을 분리하여 완료 hook 자신의 정책 변경을 선행조건으로 오인하지 않는다.
"""
from copy import deepcopy
import json
import math

from .protection_recovery import digest

ROOT = 'policy_generations'
SELECTORS = {
    'intraday_policy.current': ('intraday_policy', 'current'),
    'protection.config': ('protection', 'config'),
    'protection.current_regime': ('protection', 'current_regime'),
    'protection.intraday_crash_level': ('protection', 'intraday_crash_level'),
    'entry_policy_effects.sidecar_active': ('entry_policy_effects', 'sidecar_active'),
}


def canonical(value):
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
        raise ValueError('invalid_policy_generation_json')
    check(value)
    return json.dumps(value, allow_nan=False, sort_keys=True, separators=(',', ':'), ensure_ascii=False)


def registration_names(selectors):
    if (type(selectors) is not tuple or not selectors
            or any(type(name) is not str or name not in SELECTORS for name in selectors)
            or len(set(selectors)) != len(selectors)):
        raise ValueError('invalid_policy_generation_selection')
    return tuple(sorted(selectors))


def selected_fact(state, name):
    root, field = SELECTORS[name]
    container = state.get(root, {})
    if type(container) is not dict:
        raise ValueError('invalid_policy_generation_parent')
    body = {'present': field in container,
            'value': deepcopy(container[field]) if field in container else None}
    canonical(body)
    return {**body, 'digest': digest(body)}


def _keys(value, names):
    if type(value) is not dict or set(value) != set(names):
        raise ValueError('invalid_policy_generation_shape')


def validate_policy_history(state, version):
    """pre-stamp 후보에서도 사용 가능한 순수 역사/형태 검사."""
    if ROOT not in state:
        return
    root = state[ROOT]
    _keys(root, ('schema', 'selectors', 'registrations'))
    if (type(root['schema']) is not int or root['schema'] != 1
            or type(root['selectors']) is not dict or not root['selectors']
            or not set(root['selectors']) <= SELECTORS.keys()
            or type(root['registrations']) is not dict or not root['registrations']):
        raise ValueError('invalid_policy_generation_registry')
    registered, used_versions = {}, set()
    for command, request in root['registrations'].items():
        _keys(request, ('selectors', 'version'))
        if (type(command) is not str or not command.strip()
                or type(request['selectors']) is not list
                or list(registration_names(tuple(request['selectors']))) != request['selectors']
                or type(request['version']) is not int or not 0 < request['version'] <= version
                or request['version'] in used_versions):
            raise ValueError('invalid_policy_registration_request')
        used_versions.add(request['version'])
        for name in request['selectors']:
            registered[name] = min(registered.get(name, request['version']), request['version'])
    if set(registered) != set(root['selectors']):
        raise ValueError('policy_registration_selector_crosslink')
    for name, row in root['selectors'].items():
        _keys(row, ('registered_version', 'history'))
        if (type(row['registered_version']) is not int
                or not 0 < row['registered_version'] <= version
                or type(row['history']) is not list or not row['history']):
            raise ValueError('invalid_policy_generation_registration')
        previous_version, previous_fact = 0, None
        for event in row['history']:
            _keys(event, ('version', 'present', 'value', 'digest'))
            body = {'present': event['present'], 'value': event['value']}
            identity = canonical(body)
            if (type(event['version']) is not int or not previous_version < event['version'] <= version
                    or type(event['present']) is not bool
                    or (not event['present'] and event['value'] is not None)
                    or event['digest'] != digest(body) or identity == previous_fact):
                raise ValueError('invalid_policy_generation_event')
            previous_version, previous_fact = event['version'], identity
        if row['history'][0]['version'] != row['registered_version'] or registered[name] != row['registered_version']:
            raise ValueError('policy_generation_registration_crosslink')


def validate_policy_generations(state, version):
    """최종 commit/복원 전에는 현재 정책과 tail을 반드시 대조한다."""
    validate_policy_history(state, version)
    for name, row in state.get(ROOT, {}).get('selectors', {}).items():
        tail = {key: value for key, value in row['history'][-1].items() if key != 'version'}
        if canonical(tail) != canonical(selected_fact(state, name)):
            raise ValueError('policy_generation_current_tail_conflict')


def versioned_fact(state, name, *, cutoff=None):
    row = state.get(ROOT, {}).get('selectors', {}).get(name)
    if row is None:
        raise ValueError('policy_generation_unregistered')
    event = next((item for item in reversed(row['history'])
                  if cutoff is None or item['version'] < cutoff), None)
    if event is None:
        raise ValueError('policy_generation_no_historical_proof')
    return {'selector': name, 'registered_version': row['registered_version'],
            'generation': event['version'], 'present': event['present'],
            'value': deepcopy(event['value']), 'digest': event['digest']}


def finalize_policy_generations(previous, candidate, expected_version, *, registration=(), registration_id=None):
    """reducer 쓰기 검사 뒤, SQL 전에만 호출한다. 입력 객체를 바꾸지 않는다."""
    validate_policy_generations(previous, expected_version)
    if ((ROOT in previous) != (ROOT in candidate)
            or canonical(previous.get(ROOT)) != canonical(candidate.get(ROOT))):
        raise ValueError('reducer_changed_policy_generations')
    result = deepcopy(candidate)
    if registration:
        registration_names(registration)
        if type(registration_id) is not str or not registration_id.strip():
            raise ValueError('invalid_policy_registration_id')
        root = result.setdefault(ROOT, {'schema': 1, 'selectors': {}, 'registrations': {}})
        if registration_id in root['registrations']:
            raise ValueError('policy_registration_conflict')
        root['registrations'][registration_id] = {'selectors': list(registration), 'version': expected_version + 1}
        for name in registration:
            if name not in root['selectors']:
                root['selectors'][name] = {'registered_version': expected_version + 1,
                    'history': [{'version': expected_version + 1, **selected_fact(result, name)}]}
    for name, row in result.get(ROOT, {}).get('selectors', {}).items():
        fact = selected_fact(result, name)
        tail = {key: value for key, value in row['history'][-1].items() if key != 'version'}
        if canonical(fact) != canonical(tail):
            row['history'].append({'version': expected_version + 1, **fact})
    validate_policy_generations(result, expected_version + 1)
    from .risk_input_seal import validate_risk_input_seals
    validate_risk_input_seals(result, expected_version + 1)
    return result
