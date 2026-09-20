"""Explicit two-minute writer. Stored facts are not startup/trading permission."""
from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import date, datetime
import json
import math
from uuid import uuid4
from zoneinfo import ZoneInfo

from ...core.market_regime import MarketRegimeAdapter, cap_regime_by_intraday_risk
from ...utils import regime_transition as calc
from .application import ApplicationBlocked
from .day_recovery import scope_reason
from .index_risk_input import normalize_index_trend
from .intraday_owner import validate_intraday_policy
from .policy_generations import canonical, versioned_fact
from .protection_recovery import digest
from .risk_sources import RiskSourceCoordinator, _SourceAuthority
from .regime_horizon import (RegimeHorizonBaseline, RegimeStageInput, RegimeSyncContext,
                             RegimeApplicationReceipt)

POLICY_READS = ('regime_policy.trend_state', 'entry_policy_effects.sidecar_active', 'intraday_policy.current')
_REGIMES = {'bull', 'bear', 'sideways', 'neutral'}
_RULE = digest('regime_transition:sidecar-mid-expert:v1')
_HORIZON_RULE = digest('regime_transition:sidecar-mid-expert:folded-horizon:v2')


def _index_rule_at(state, version):
    """Bind semantics to the durable activation boundary, including cold history."""
    root = state.get('regime_policy', {})
    baseline = root.get('morning_baseline')
    if root.get('schema') == 3 and baseline is not None and version > baseline['baseline_version']:
        return _HORIZON_RULE
    return _RULE


class _BaselineAdmissionRejected(Exception):
    """Only explicit registration admission checks may create this marker."""
    def __init__(self, reason, *, blocked=False):
        super().__init__(reason)
        self.blocked = blocked


class _OwnerBeginCancelled(asyncio.CancelledError):
    """The original begin SQL and its terminal cleanup remain strongly tracked."""


def _keys(value, keys):
    if type(value) is not dict or set(value) != set(keys):
        raise ValueError('invalid_regime_shape')


def _time(value):
    if type(value) is not str:
        raise ValueError('invalid_regime_time')
    result = datetime.fromisoformat(value)
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError('naive_regime_time')
    return result


def _number(value):
    if type(value) not in (int, float):
        raise ValueError('invalid_regime_number')
    try:
        finite = math.isfinite(value)
    except OverflowError:
        raise ValueError('invalid_regime_number') from None
    if not finite:
        raise ValueError('invalid_regime_number')


def _positive(value):
    if type(value) is not int or value <= 0:
        raise ValueError('invalid_regime_version')


def _vix_payload(row):
    envelope = row['terminal']['envelope']; value = envelope['payload']
    _keys(value, ('value', 'fetched_at')); _number(value['value'])
    when = _time(value['fetched_at'])
    if (when > _time(row['terminal']['completed_at'])
            or (envelope['received_at'] is not None and when != _time(envelope['received_at']))):
        raise ValueError('invalid_vix_original_time')
    return value['value'], when


def _pending(value):
    _keys(value, ('present', 'target', 'since'))
    if (type(value['present']) is not bool or value['target'] not in _REGIMES | {None}
            or (not value['present'] and value['target'] is not None)
            or (value['target'] is not None and value['since'] is None)):
        raise ValueError('invalid_regime_pending')
    if value['since'] is not None:
        _time(value['since'])


def _trend(value):
    _keys(value, ('mid_regime', 'mid_pending', 'expert_pending', 'market_trend',
                  'regime_data', 'last_update', 'source_refs'))
    if value['mid_regime'] not in _REGIMES:
        raise ValueError('invalid_mid_regime')
    _pending(value['mid_pending']); _pending(value['expert_pending'])
    trend = value['market_trend']
    _keys(trend, ('present', 'kospi_pct', 'kosdaq_pct', 'avg_pct', 'vs_open_pct',
                  'position_pct', 'recovering', 'classified_at'))
    if trend['present'] is not True or type(trend['recovering']) is not bool:
        raise ValueError('known_regime_trend_required')
    for key in ('kospi_pct', 'kosdaq_pct', 'avg_pct', 'vs_open_pct', 'position_pct'):
        _number(trend[key])
    _time(trend['classified_at'])
    if value['last_update'] is not None: _time(value['last_update'])
    if type(value['regime_data']) is not dict:
        raise ValueError('invalid_regime_data')
    canonical(value['regime_data'])
    _keys(value['source_refs'], ('trend', 'vix', 'expert'))
    for ref in value['source_refs'].values():
        if ref is not None:
            _keys(ref, ('operation_id', 'committed_version'))
            if type(ref['operation_id']) is not str or not ref['operation_id'].strip():
                raise ValueError('invalid_regime_source_ref')
            _positive(ref['committed_version'])


@dataclass(frozen=True)
class RegimeBaseline:
    json: str

    @classmethod
    def from_dict(cls, value):
        _keys(value, ('schema', 'baseline_id', 'account_scope', 'business_day', 'generation',
            'fence_id', 'evidence', 'sidecar_active', 'intraday', 'trend_state', 'engine_regime'))
        if (type(value['schema']) is not int or value['schema'] != 1
                or type(value['generation']) is not int or value['generation'] < 0
                or type(value['sidecar_active']) is not bool or value['engine_regime'] not in _REGIMES):
            raise ValueError('invalid_regime_baseline')
        for name in ('baseline_id', 'account_scope'):
            if type(value[name]) is not str or not value[name].strip():
                raise ValueError('invalid_regime_identity')
        if value['fence_id'] is not None and (type(value['fence_id']) is not str or not value['fence_id']):
            raise ValueError('invalid_regime_fence')
        if date.fromisoformat(value['business_day']).isoformat() != value['business_day']:
            raise ValueError('invalid_regime_day')
        _keys(value['evidence'], ('source', 'event_id', 'observed_at'))
        for name in ('source', 'event_id'):
            if type(value['evidence'][name]) is not str or not value['evidence'][name].strip():
                raise ValueError('invalid_regime_evidence')
        _time(value['evidence']['observed_at'])
        _keys(value['intraday'], ('baseline_version', 'current'))
        _positive(value['intraday']['baseline_version'])
        from .risk_transition import IntradayPolicyState
        IntradayPolicyState.from_dict(value['intraday']['current'])
        _trend(value['trend_state'])
        return cls(canonical(value))

    def to_dict(self):
        return json.loads(self.json)


def _references(state, trend):
    records = state.get('risk_sources', {}).get('records', {})
    for name, ref in trend['source_refs'].items():
        if ref is None: continue
        row = records.get(ref['operation_id'], {})
        receipt = (row.get('terminal') or {}).get('receipt', {})
        if (row.get('ticket', {}).get('lane') != {'trend': 'index_trend', 'vix': 'vix_regime', 'expert': 'expert_regime'}[name]
                or receipt.get('status') != 'accepted'
                or receipt.get('committed_version') != ref['committed_version']):
            raise ValueError('regime_source_crosslink_conflict')


def effective_regime(state, now):
    if state['regime_policy'].get('schema') == 3:
        value = state['regime_policy']['horizon']
        when = value['classified_at']
        kst = ZoneInfo('Asia/Seoul')
        level = value['level'] if (when is not None
            and _time(when).astimezone(kst).date() == now.astimezone(kst).date()) else None
        return cap_regime_by_intraday_risk(state['regime_policy']['trend_state']['mid_regime'], level)
    value = state['intraday_policy']['current']
    when = value['updated_at']
    level = value['level'] if when is not None and _time(when).date() == now.date() else None
    return cap_regime_by_intraday_risk(state['regime_policy']['trend_state']['mid_regime'], level)


def _verify_calculation(payload, seal):
    """Recompute with sealed facts only; no live mirrors, clock, or provider."""
    inputs, reads = seal['request']['inputs'], seal['reads']
    _keys(inputs, ('observations', 'clocks', 'selected_vix', 'expert', 'rule_digest'))
    if inputs['rule_digest'] not in (_RULE, _HORIZON_RULE) or len(inputs['observations']) != 2:
        raise ValueError('regime_calculation_inputs')
    clocks = inputs['clocks']; before = payload['before']
    parsed = {name: _time(value) for name, value in clocks.items()}
    policies = reads['versioned_policies']
    if (canonical(policies['regime_policy.trend_state']['value']) != canonical(before)
            or canonical(policies['entry_policy_effects.sidecar_active']['value']) != canonical(payload['sidecar_before'])):
        raise ValueError('regime_calculation_policy_crosslink')
    quotes = []
    for code, original in zip(('0001', '1001'), inputs['observations']):
        observation = json.loads(original)
        quote = {name: fact['value'] for name, fact in observation['fields'].items()}
        quote['_observation'] = observation
        fact = normalize_index_trend(quote, index_code=code, now=parsed['sidecar'],
            business_day=parsed['sidecar'].date().isoformat())
        if fact.outcome != 'success': raise ValueError('regime_calculation_observation')
        values = dict(fact.values)
        if not values['low'] <= min(values['price'], values['open']) <= max(values['price'], values['open']) <= values['high']:
            raise ValueError('regime_calculation_ohlc')
        quotes.append(values)
    selected = inputs['selected_vix']; vix = None
    if selected is not None:
        raw = reads['retained'].get(selected['operation_id'])
        if raw is None or raw['terminal']['receipt']['committed_version'] != selected['committed_version']:
            raise ValueError('regime_calculation_vix_crosslink')
        value = raw['terminal']['envelope']['payload']
        _keys(value, ('value', 'fetched_at')); _number(value['value']); _time(value['fetched_at'])
        vix = value['value']
    sidecar = calc.transition_sidecar_trend(*quotes, calc.SidecarState(before['market_trend'], payload['sidecar_before']))
    pending = before['mid_pending']
    plan = calc.plan_mid_regime_transition(calc.MidRegimeState(before['mid_regime'], pending['present'], pending['target'],
        None if pending['since'] is None else _time(pending['since'])), calc.calculate_mid_regime_facts(*quotes),
        parsed['opening'].strftime('%H:%M'), 'normal' if vix is None else MarketRegimeAdapter._classify_vix(vix), vix)
    expected_clocks = {'sidecar', 'opening', 'technical_update'}
    if plan.clock_stage != 'none': expected_clocks.add('mid_' + plan.clock_stage)
    mid = calc.complete_mid_regime_transition(plan, parsed.get('mid_' + plan.clock_stage))
    expected = deepcopy(before)
    expected.update(mid_regime=mid.state.current_regime, mid_pending=_pending_dict(mid.state),
        market_trend={**sidecar.market_trend, 'present': True, 'classified_at': clocks['sidecar']},
        last_update=clocks['technical_update'], regime_data={'kospi_change': mid.kospi_change,
            'kosdaq_change': mid.kosdaq_change, 'avg_change': mid.avg_change, 'avg_vs_open': mid.avg_vs_open,
            'vix': vix, 'vix_state': 'normal' if vix is None else MarketRegimeAdapter._classify_vix(vix)})
    raw = reads['sources']['expert_regime']; expert = inputs['expert']; expert_ref = None
    if expert is not None:
        if (raw is None or not raw['terminal'] or raw['terminal']['receipt']['status'] != 'accepted'
                or canonical(raw['terminal']['envelope']['payload']) != canonical(expert)):
            raise ValueError('regime_calculation_expert_crosslink')
        _keys(expert, ('score', 'bear_consensus')); _number(expert['score'])
        if type(expert['bear_consensus']) is not bool: raise ValueError('regime_calculation_expert')
        pending = before['expert_pending']
        plan = calc.plan_expert_transition(calc.ExpertState(expected['mid_regime'], pending['present'], pending['target'],
            None if pending['since'] is None else _time(pending['since'])), expert['score'], expert['bear_consensus'])
        if plan.clock_stage != 'none': expected_clocks.add('expert_' + plan.clock_stage)
        adjusted = calc.complete_expert_transition(plan, parsed.get('expert_' + plan.clock_stage))
        expected['mid_regime'], expected['expert_pending'] = adjusted.state.current_regime, _pending_dict(adjusted.state)
        expected['regime_data'].update(expert_score=expert['score'], expert_bear_consensus=expert['bear_consensus'])
        expert_ref = {'operation_id': raw['ticket']['operation_id'], 'committed_version': raw['terminal']['receipt']['committed_version']}
    elif raw and raw['terminal'] and raw['terminal']['receipt']['status'] == 'accepted':
        raise ValueError('regime_calculation_expert_omitted')
    expected['source_refs'] = {'trend': None, 'vix': selected, 'expert': expert_ref}
    if inputs['rule_digest'] == _HORIZON_RULE:
        horizon = policies['regime_policy.horizon']['value']
        kst = ZoneInfo('Asia/Seoul')
        risk = horizon['level'] if (horizon['classified_at'] is not None
            and _time(horizon['classified_at']).astimezone(kst).date()
            == parsed['sidecar'].astimezone(kst).date()) else None
    else:
        if 'regime_policy.horizon' in policies: raise ValueError('regime_rule_policy_conflict')
        intraday = policies['intraday_policy.current']['value']
        risk = intraday['level'] if intraday['updated_at'] is not None and _time(intraday['updated_at']).date() == parsed['sidecar'].date() else None
    if (set(clocks) != expected_clocks or canonical(expected) != canonical(payload['after'])
            or payload['sidecar_after'] is not sidecar.sidecar_active
            or payload['engine_regime'] != cap_regime_by_intraday_risk(expected['mid_regime'], risk)):
        raise ValueError('regime_calculation_conflict')


def _validate_transition(state, operation, row, previous, last_version, version):
    """One transition, shared by cold history replay and the precommit append gate."""
    if row.get('kind') == 'llm_morning_diagnosis':
        from .regime_morning import validate_transition
        return validate_transition(state, operation, row, previous, last_version, version)
    _keys(row, ('kind', 'version', 'before', 'after', 'sidecar_before', 'sidecar_after',
        'request_digest', 'outcome_digest', 'seal_digest', 'rule_digest', 'engine_regime'))
    source = state.get('risk_sources', {}).get('records', {}).get(operation, {})
    terminal = source.get('terminal') or {}
    receipt = terminal.get('receipt', {})
    seal = state.get('risk_input_seals', {}).get('records', {}).get(operation, {}).get('original', {})
    if (row['kind'] != 'index_trend' or type(row['version']) is not int
            or not last_version < row['version'] <= version or canonical(row['before']) != canonical(previous)
            or row['rule_digest'] != _index_rule_at(state, row['version']) or receipt.get('status') != 'accepted'
            or source.get('ticket', {}).get('kind') != 'index_trend'
            or receipt.get('committed_version') != row['version']
            or receipt.get('outcome_digest') != row['outcome_digest']
            or source['ticket']['request_digest'] != row['request_digest']
            or seal.get('seal_digest') != row['seal_digest']):
        raise ValueError('regime_transition_crosslink_conflict')
    payload = terminal['envelope']['payload']
    if row['rule_digest'] != seal.get('request', {}).get('inputs', {}).get('rule_digest'):
        raise ValueError('regime_rule_crosslink')
    _verify_calculation(payload, seal)
    expected_after = deepcopy(payload['after'])
    expected_after['source_refs']['trend'] = {'operation_id': operation, 'committed_version': row['version']}
    if (canonical(expected_after) != canonical(row['after'])
            or any(canonical(payload[key]) != canonical(row[key]) for key in
                   ('before', 'sidecar_before', 'sidecar_after', 'engine_regime'))
            or canonical(payload['inputs']) != canonical(seal['request']['inputs'])):
        raise ValueError('regime_transition_payload_conflict')
    _trend(row['after']); _references(state, row['after'])
    if canonical(row['after']['source_refs']['trend']) != canonical(
            {'operation_id': operation, 'committed_version': row['version']}):
        raise ValueError('regime_transition_ref_conflict')
    return row['after'], {'regime': row['engine_regime'], 'operation_id': operation, 'version': row['version']}


def _validate_regime_append(state, before, operation, version):
    """Keep the prior root/history verbatim and validate only the new typed transition."""
    root = state['regime_policy']
    _keys(root, before.keys())
    if (state['risk_sources']['records'][operation]['ticket']['admission_version']
            <= root['baseline']['baseline_version']):
        raise ValueError('regime_transition_baseline_scope_conflict')
    transitions = root['transitions']
    changed = ('transitions', 'trend_state', 'engine_projection')
    if transitions[operation]['kind'] == 'llm_morning_diagnosis': changed += ('morning',)
    if (operation in before['transitions'] or set(transitions) != set(before['transitions']) | {operation}
            or any(canonical(transitions[key]) != canonical(value) for key, value in before['transitions'].items())
            or any(canonical(root[key]) != canonical(value) for key, value in before.items()
                   if key not in changed)):
        raise ValueError('regime_prior_root_changed')
    row = transitions[operation]
    if row['version'] != version:
        raise ValueError('regime_transition_version_conflict')
    trend, engine = _validate_transition(state, operation, row, before['trend_state'],
        before['engine_projection']['version'], version)
    if (canonical(root['trend_state']) != canonical(trend)
            or canonical(root['engine_projection']) != canonical(engine)
            or type(state['entry_policy_effects']['sidecar_active']) is not bool
            or state['entry_policy_effects']['sidecar_active'] is not row['sidecar_after']):
        raise ValueError('regime_projection_conflict')
    if root.get('schema') == 3:
        from .regime_morning import validate_history
        validate_history(state, version)


def validate_regime_policy(state, version):
    root = state.get('regime_policy')
    if root is None: return None
    _keys(root, ('schema', 'baseline', 'trend_state', 'transitions', 'engine_projection') +
        (('horizon_baseline', 'horizon', 'noon_caps', 'applications') if root.get('schema') in (2, 3) else ()) +
        (('morning_baseline', 'morning') if root.get('schema') == 3 else ()))
    _keys(root['baseline'], ('supplied', 'digest', 'baseline_version'))
    baseline = root['baseline']; supplied = RegimeBaseline.from_dict(baseline['supplied']).to_dict()
    _positive(baseline['baseline_version'])
    if (type(root['schema']) is not int or root['schema'] not in (1, 2, 3)
            or baseline['baseline_version'] > version or baseline['digest'] != digest(supplied)
            or type(root['transitions']) is not dict):
        raise ValueError('invalid_regime_baseline')
    previous, last_version = supplied['trend_state'], baseline['baseline_version']
    _references(state, previous)
    engine = {'regime': supplied['engine_regime'], 'operation_id': None, 'version': last_version}
    records = state.get('risk_sources', {}).get('records', {})
    # Cold restore validates typed VIX facts even before an explicit writer binding.
    for source in records.values():
        if (source['ticket']['lane'] == 'vix_regime' and source['terminal']
                and source['terminal']['receipt']['status'] == 'accepted'):
            _vix_payload(source)
    for operation, row in sorted(root['transitions'].items(), key=lambda item: item[1]['version']):
        previous, engine = _validate_transition(state, operation, row, previous, last_version, version)
        last_version = row['version']
    kinds = ('index_trend', 'llm_morning_diagnosis') if root['schema'] == 3 else ('index_trend',)
    accepted = {op for op, row in records.items() if row['ticket']['kind'] in kinds
        and row['ticket']['admission_version'] > baseline['baseline_version']
        and row['terminal'] and row['terminal']['receipt']['status'] == 'accepted'}
    if (accepted != set(root['transitions']) or canonical(root['trend_state']) != canonical(previous)
            or canonical(root['engine_projection']) != canonical(engine)):
        raise ValueError('regime_projection_conflict')
    if type(state.get('entry_policy_effects', {}).get('sidecar_active')) is not bool:
        raise ValueError('invalid_regime_sidecar_projection')
    if root['schema'] in (2, 3):
        from .regime_horizon import validate_horizon
        validate_horizon(state, version)
    if root['schema'] == 3:
        from .regime_morning import validate_history
        validate_history(state, version)
    return deepcopy(root)


def require_current_trend(state, day):
    root = state['regime_policy']
    selected = root['engine_projection']['operation_id']
    if selected is not None and root['transitions'][selected]['kind'] == 'llm_morning_diagnosis':
        version = root['transitions'][selected]['version']
        status, _, _ = _SourceAuthority(state, day).dependency('llm_morning_diagnosis', version)
        if status != 'current': raise ApplicationBlocked('regime_source_not_current')
        return version
    ref = state['regime_policy']['trend_state']['source_refs']['trend']
    if ref is None:
        raise ApplicationBlocked('regime_source_not_current')
    authority = _SourceAuthority(state, day)
    status, _, _ = authority.dependency('index_trend', ref['committed_version'])
    if status != 'current': raise ApplicationBlocked('regime_source_not_current')
    return ref['committed_version']


class RegimeOwner:
    @staticmethod
    async def register_morning_baseline(runtime, baseline, *, expected_version):
        from .regime_morning_commands import register_baseline
        return await register_baseline(runtime, baseline, expected_version)

    async def diagnose_morning(self, inputs_provider, llm):
        from .regime_morning_commands import diagnose
        return await diagnose(self, inputs_provider, llm)

    def morning_assessment(self, *, now=None):
        from zoneinfo import ZoneInfo
        from ...core.market_regime import OPEN_EXPECTATION_EXPIRY
        self.runtime.owner._require_ready()
        root = self.runtime.owner.state['regime_policy']
        if root.get('schema') != 3: raise ApplicationBlocked('morning_baseline_required')
        result = deepcopy(root['morning'])
        now = self.runtime._now() if now is None else now
        if now.utcoffset() is None: raise ValueError('naive_morning_time')
        now = now.astimezone(ZoneInfo('Asia/Seoul'))
        as_of = result['open_expectation_as_of']
        if (as_of is None or _time(as_of).astimezone(ZoneInfo('Asia/Seoul')).date() != now.date()
                or now.time() >= OPEN_EXPECTATION_EXPIRY):
            result['open_expectation'] = None
        return result

    @staticmethod
    async def register_horizon_baseline(runtime, baseline, *, expected_version):
        if (type(baseline) is not RegimeHorizonBaseline or type(expected_version) is not int
                or expected_version < 0):
            raise ValueError('invalid_regime_horizon_registration')
        supplied = RegimeHorizonBaseline.from_dict(baseline.to_dict()).to_dict()
        def check(state, *, admitted=False):
            def reject(reason, *, blocked=False):
                if admitted: raise _BaselineAdmissionRejected(reason, blocked=blocked)
                raise (ApplicationBlocked if blocked else ValueError)(reason)
            if admitted:
                if runtime.day_admission_closed: reject('day_transition_admission_closed', blocked=True)
            else:
                runtime._require_day_admission()
            root = state.get('regime_policy')
            if root is None: reject('regime_baseline_required', blocked=True)
            if root.get('horizon_baseline') is not None:
                if canonical(root['horizon_baseline']['supplied']) != canonical(supplied):
                    reject('regime_horizon_baseline_conflict')
                return root['horizon_baseline']['baseline_version']
            now = runtime._now()
            times = (supplied['evidence']['observed_at'], supplied['horizon']['classified_at'])
            if any(_time(t) > now for t in times if t is not None): reject('future_regime_baseline')
            if (supplied['account_scope'] != runtime.account_scope or scope_reason(state, runtime.account_scope)
                    or supplied['business_day'] != now.date().isoformat()
                    or supplied['generation'] != runtime._day_generation or supplied['fence_id'] != runtime._day_fence_id):
                reject('regime_baseline_context_conflict')
            if runtime.owner.version != expected_version: reject('regime_baseline_version_conflict')
            if (supplied['regime_baseline_version'] != root['baseline']['baseline_version']
                    or supplied['intraday']['baseline_version'] != state['intraday_policy']['baseline_version']
                    or canonical(supplied['intraday']['current']) != canonical(state['intraday_policy']['current'])):
                reject('regime_baseline_prerequisite_conflict')
            return None
        runtime.owner._require_ready()
        prior = check(runtime.owner.state)
        if prior is not None: return prior
        with runtime.command_scope() as token:
            rejection = None
            async def execute():
                nonlocal rejection
                def reduce(state):
                    if check(state, admitted=True) is not None: return state
                    version = runtime.owner.version + 1
                    root = state['regime_policy']
                    root.update(schema=2, horizon_baseline={'supplied': supplied,
                        'digest': digest(supplied), 'baseline_version': version}, noon_caps={}, applications={})
                    from .regime_horizon import fold_horizon
                    root['horizon'] = fold_horizon(state)
                    validate_regime_policy(state, version)
                    return state
                try:
                    await runtime.owner.mutate('regime-horizon-baseline:' + digest(supplied), reduce)
                except _BaselineAdmissionRejected as exc:
                    rejection = exc
                return True
            await asyncio.shield(runtime.start_command_result(token, execute))
            try:
                if rejection is not None: raise rejection
                result = check(runtime.owner.state, admitted=True)
                if result is None: raise ValueError('regime_horizon_baseline_result_missing')
                return result
            except _BaselineAdmissionRejected as exc:
                raise (ApplicationBlocked if exc.blocked else ValueError)(str(exc)) from None

    @staticmethod
    async def register_baseline(runtime, baseline, *, expected_version):
        if type(baseline) is not RegimeBaseline or type(expected_version) is not int or expected_version < 0:
            raise ValueError('invalid_regime_registration')
        supplied = RegimeBaseline.from_dict(baseline.to_dict()).to_dict()
        def check(state, *, admitted=False):
            def reject(reason, *, blocked=False):
                if admitted: raise _BaselineAdmissionRejected(reason, blocked=blocked)
                raise (ApplicationBlocked if blocked else ValueError)(reason)
            if admitted:
                if runtime.day_admission_closed: reject('day_transition_admission_closed', blocked=True)
            else:
                runtime._require_day_admission()
            now = runtime._now()
            times = [supplied['evidence']['observed_at'], supplied['trend_state']['market_trend']['classified_at'],
                supplied['trend_state']['last_update'], supplied['trend_state']['mid_pending']['since'],
                supplied['trend_state']['expert_pending']['since']]
            if any(_time(t) > now for t in times if t is not None):
                reject('future_regime_baseline')
            if (supplied['account_scope'] != runtime.account_scope or scope_reason(state, runtime.account_scope)
                    or supplied['business_day'] != now.date().isoformat()
                    or supplied['generation'] != runtime._day_generation or supplied['fence_id'] != runtime._day_fence_id):
                reject('regime_baseline_context_conflict')
            existing = state.get('regime_policy')
            if existing is not None:
                if canonical(existing['baseline']['supplied']) != canonical(supplied): reject('regime_baseline_conflict')
                return existing['baseline']['baseline_version']
            if runtime.owner.version != expected_version: reject('regime_baseline_version_conflict')
            policy = validate_intraday_policy(state, runtime.owner.version)
            if (policy is None or canonical(supplied['intraday']['current']) != canonical(policy.to_dict())
                    or supplied['intraday']['baseline_version'] != state['intraday_policy']['baseline_version']
                    or state.get('entry_policy_effects', {}).get('sidecar_active') is not supplied['sidecar_active']):
                reject('regime_baseline_prerequisite_conflict')
            _references(state, supplied['trend_state'])
            return None
        runtime.owner._require_ready()
        prior = check(runtime.owner.state)
        if prior is not None: return prior
        with runtime.command_scope() as token:
            rejection = None
            async def execute():
                nonlocal rejection
                def reduce(state):
                    if check(state, admitted=True) is not None: return state
                    version = runtime.owner.version + 1
                    state['regime_policy'] = {'schema': 1,
                        'baseline': {'supplied': supplied, 'digest': digest(supplied), 'baseline_version': version},
                        'trend_state': deepcopy(supplied['trend_state']), 'transitions': {},
                        'engine_projection': {'regime': supplied['engine_regime'], 'operation_id': None, 'version': version}}
                    validate_regime_policy(state, version)
                    return state
                try:
                    await runtime.owner.mutate('regime-baseline:' + digest(supplied), reduce)
                except _BaselineAdmissionRejected as exc:
                    rejection = exc
                return True
            await asyncio.shield(runtime.start_command_result(token, execute))
            try:
                if rejection is not None: raise rejection
                # Idempotent mutate may bypass the reducer; verify its actual result.
                result = check(runtime.owner.state, admitted=True)
                if result is None: raise ValueError('regime_baseline_result_missing')
                return result
            except _BaselineAdmissionRejected as exc:
                raise (ApplicationBlocked if exc.blocked else ValueError)(str(exc)) from None

    def __init__(self, runtime, *, adapter, sidecar, vix_fetcher=None):
        runtime._require_day_admission(); runtime.owner._require_ready()
        if (getattr(runtime, '_regime_writer', None) is not None or sidecar is not runtime.risk_manager
                or sidecar is None or adapter is not getattr(runtime.engine, '_regime_adapter', None)):
            raise ApplicationBlocked('regime_owner_binding_conflict')
        root = validate_regime_policy(runtime.owner.state, runtime.owner.version)
        if root is None: raise ApplicationBlocked('regime_baseline_required')
        if root['baseline']['supplied']['account_scope'] != runtime.account_scope:
            raise ApplicationBlocked('regime_baseline_scope_conflict')
        for name in POLICY_READS: versioned_fact(runtime.owner.state, name)
        self.runtime, self.adapter, self.sidecar = runtime, adapter, sidecar
        self.sources = RiskSourceCoordinator(runtime, completion_reducer=self._reduce)
        self._vix_fetcher = vix_fetcher
        self._vix_refresh_task = None
        self._classifier_projection_lock = asyncio.Lock()
        self._morning_running = False
        self.publish(runtime.owner.state, root)
        runtime._regime_writer = self
        adapter._regime_owner = sidecar._regime_owner = self

    def projection(self, state, root):
        if root is None: raise ValueError('installed_regime_baseline_missing')
        trend = root['trend_state']; value = trend['market_trend']
        intraday = state['intraday_policy']['current']
        horizon = root.get('horizon')
        selected = self._select_vix(state)
        morning = root.get('morning')
        return {
            'mid': trend['mid_regime'], 'data': deepcopy(trend['regime_data']),
            'updated': None if trend['last_update'] is None else _time(trend['last_update']),
            'pending': tuple((attr, clock_attr, trend[name]['present'], trend[name]['target'],
                None if trend[name]['since'] is None else _time(trend[name]['since']))
                for name, attr, clock_attr in (('mid_pending', '_pending_regime', '_pending_since'),
                    ('expert_pending', '_expert_pending', '_expert_pending_since'))),
            'market_trend': {**{key: val for key, val in value.items() if key not in ('present', 'classified_at')},
                             'ts': _time(value['classified_at'])},
            'sidecar': state['entry_policy_effects']['sidecar_active'],
            'morning': morning,
            'horizons': replace(self.adapter._horizons,
                **({'open_expectation': morning['open_expectation'], 'open_expectation_as_of':
                    None if morning['open_expectation_as_of'] is None else _time(morning['open_expectation_as_of'])}
                   if morning is not None else {}),
                intraday_risk=horizon['level'] if horizon else intraday['level'],
                intraday_change_pct=horizon['change_pct'] if horizon else intraday['kospi_pct'],
                intraday_risk_as_of=(None if horizon['classified_at'] is None else _time(horizon['classified_at']))
                    if horizon else (None if intraday['updated_at'] is None else _time(intraday['updated_at']))),
            'vix': selected[1], 'vix_when': selected[2],
            'vix_state': 'normal' if selected[1] is None else self.adapter._classify_vix(selected[1]),
            'engine_regime': effective_regime(state, self.runtime._now()) if root['schema'] == 3
                else root['engine_projection']['regime']}

    def publish(self, state, root, projection=None):
        plan = self.projection(state, root) if projection is None else projection
        adapter = self.adapter
        adapter._current_regime, adapter._regime_data, adapter._last_update = plan['mid'], plan['data'], plan['updated']
        for attr, clock_attr, present, target, since in plan['pending']:
            if present: setattr(adapter, attr, target)
            elif hasattr(adapter, attr): delattr(adapter, attr)
            if since is not None: setattr(adapter, clock_attr, since)
            elif hasattr(adapter, clock_attr): delattr(adapter, clock_attr)
        self.sidecar._market_trend, self.sidecar._sidecar_active = plan['market_trend'], plan['sidecar']
        adapter._horizons = plan['horizons']
        if plan['morning'] is not None:
            morning = plan['morning']
            adapter._llm_assessment = morning['assessment'] or ''
            adapter._llm_assessment_date = None if morning['assessment_day'] is None else date.fromisoformat(morning['assessment_day'])
            adapter._llm_assessment_inited = True
        adapter._vix_value, adapter._vix_state, adapter._vix_last_fetch = plan['vix'], plan['vix_state'], plan['vix_when']
        self.runtime.engine._market_regime = plan['engine_regime']

    def _select_vix(self, state):
        records = state.get('risk_sources', {}).get('records', {})
        candidates = [row for row in records.values() if row['ticket']['lane'] == 'vix_regime'
            and row['terminal'] and row['terminal']['receipt']['status'] == 'accepted' and not row['conflict']]
        if not candidates: return None, None, None
        row = max(candidates, key=lambda row: row['terminal']['receipt']['committed_version'])
        value, when = _vix_payload(row)
        return {'operation_id': row['ticket']['operation_id'],
            'committed_version': row['terminal']['receipt']['committed_version']}, value, when

    def _reduce(self, state, ticket, envelope, version):
        if ticket.kind == 'llm_morning_diagnosis':
            from .regime_morning import append_source
            return append_source(state, ticket, envelope, version)
        if state['regime_policy']['schema'] in (2, 3) and ticket.kind in {'noon_index', 'llm_regime'}:
            from .regime_commands import reduce_source
            return reduce_source(self, state, ticket, envelope, version)
        if ticket.kind != 'index_trend': return state
        payload = deepcopy(envelope['payload']); root = state['regime_policy']
        if (canonical(root['trend_state']) != canonical(payload['before'])
                or canonical(state['entry_policy_effects']['sidecar_active']) != canonical(payload['sidecar_before'])):
            raise ValueError('regime_unsealed_policy_change')
        payload['after']['source_refs']['trend'] = {'operation_id': ticket.operation_id, 'committed_version': version}
        seal = state['risk_input_seals']['records'][ticket.operation_id]['original']
        root['trend_state'] = payload['after']
        state['entry_policy_effects']['sidecar_active'] = payload['sidecar_after']
        root['transitions'][ticket.operation_id] = {key: payload[key] for key in
            ('before', 'after', 'sidecar_before', 'sidecar_after', 'engine_regime')}
        root['transitions'][ticket.operation_id].update(kind='index_trend', version=version,
            request_digest=ticket.request_digest, outcome_digest=digest(envelope),
            seal_digest=seal['seal_digest'], rule_digest=seal['request']['inputs']['rule_digest'])
        root['engine_projection'] = {'regime': payload['engine_regime'], 'operation_id': ticket.operation_id, 'version': version}
        return state

    async def classify(self, label, *, inputs_provider, llm):
        from .regime_commands import classify
        return await classify(self, label, inputs_provider, llm)

    def classifier_application_receipt(self, classifier_operation_id):
        row = self.runtime.owner.state['regime_policy'].get('applications', {}).get(
            'classifier:' + classifier_operation_id)
        return None if row is None else RegimeApplicationReceipt(**row['receipt'])

    async def sync_protection(self, operation_id, *, supplied_context):
        from .regime_commands import sync_protection
        return await sync_protection(self, operation_id, supplied_context)

    async def _write_classifier_projection(self, operation_id):
        from .regime_commands import write_projection
        return await write_projection(self, operation_id)

    def _replace_classifier_projection(self, payload):
        from .regime_commands import replace_projection
        return replace_projection(payload)

    async def refresh_trend(self, provider, *, expert_orchestrator=None):
        runtime = self.runtime
        with runtime.command_scope() as token:
            ticket = await self._begin('index-trend:' + uuid4().hex, 'index_trend', token, require_seal=True)
            terminal = {'started': False}
            try:
                return await self._refresh_ticket(ticket, provider, expert_orchestrator, token, terminal)
            except asyncio.CancelledError as exc:
                row = runtime.owner.state['risk_sources']['records'][ticket.operation_id]
                if row['terminal'] is None and not terminal['started']:
                    if isinstance(exc, _OwnerBeginCancelled):
                        self._defer_cancel(ticket.operation_id, ticket.kind, token)
                    else:
                        await self.sources.complete(ticket, 'cancelled', scope_token=token)
                raise

    async def _begin(self, operation, kind, token, *, require_seal=False, dependencies=None):
        admitted = []
        try:
            return await self.sources.begin(operation, kind, require_seal=require_seal,
                dependencies=dependencies, scope_token=token, _admitted_task_observer=admitted.append)
        except asyncio.CancelledError:
            if admitted:
                self._defer_cancel(operation, kind, token, admitted[0])
            raise _OwnerBeginCancelled() from None

    def _defer_cancel(self, operation, kind, parent_token, admitted_task=None):
        runtime = self.runtime
        async def finalize(child_token):
            if admitted_task is not None:
                await asyncio.shield(admitted_task)
            # Resolve the original durable ticket; never reconstruct today's begin request.
            from .risk_sources import _ticket
            row = runtime.owner.state.get('risk_sources', {}).get('records', {}).get(operation)
            if row is None or row['ticket']['kind'] != kind:
                raise ValueError('regime_cancelled_begin_result_missing')
            if row['terminal'] is None:
                await self.sources.complete(_ticket(row), 'cancelled', scope_token=child_token)
            return True
        return runtime._start_command_finalizer(parent_token, finalize)

    async def _refresh_ticket(self, ticket, provider, expert_orchestrator, token, terminal):
        runtime = self.runtime
        from .risk_input_seal import _context_reason, _source_at
        async def finish(*args, **kwargs):
            terminal['started'] = True
            return await self.sources.complete(ticket, *args, **kwargs)
        try:
            values = await asyncio.gather(provider.fetch_index_price('0001'), provider.fetch_index_price('1001'), return_exceptions=True)
        except asyncio.CancelledError:
            await finish('cancelled', scope_token=token); raise
        if any(isinstance(value, BaseException) for value in values):
            return await finish('failed', scope_token=token)
        if _context_reason(runtime, runtime.owner.state, ticket):
            return await finish('success', {}, scope_token=token)
        facts = [normalize_index_trend(value, index_code=code, now=runtime._now(), business_day=ticket.business_day)
                 for code, value in zip(('0001', '1001'), values)]
        if any(fact.outcome != 'success' for fact in facts):
            return await finish('missing', scope_token=token)
        for fact in facts:
            values = dict(fact.values)
            if not values['low'] <= min(values['price'], values['open']) <= max(values['price'], values['open']) <= values['high']:
                return await finish('missing', scope_token=token)
        expert = await self._begin('expert:' + uuid4().hex, 'expert_regime', token)
        if _context_reason(runtime, runtime.owner.state, ticket):
            await self.sources.complete(expert, 'cancelled', scope_token=token)
            return await finish('success', {}, scope_token=token)
        state = runtime.owner.state; before = state['regime_policy']['trend_state']
        sidecar_before = state['entry_policy_effects']['sidecar_active']
        kospi, kosdaq = [dict(fact.values) for fact in facts]
        sidecar = calc.transition_sidecar_trend(kospi, kosdaq, calc.SidecarState(before['market_trend'], sidecar_before))
        vix_ref, vix, vix_when = self._select_vix(state)
        retained = () if vix_ref is None else (vix_ref['operation_id'],)
        reads = POLICY_READS + (('regime_policy.horizon',) if state['regime_policy']['schema'] == 3 else ())
        expected = json.loads(self.sources.capture_reads(ticket, source_lanes=('vix_regime',),
            retained_sources=retained, versioned_policy_reads=reads))
        refresh_vix = vix_when is None or (runtime._now() - vix_when).total_seconds() >= 6 * 3600
        clocks = {'sidecar': runtime._now().isoformat()}
        mid = before['mid_pending']; mid_facts = calc.calculate_mid_regime_facts(kospi, kosdaq)
        clocks['opening'] = runtime._now().isoformat()
        plan = calc.plan_mid_regime_transition(calc.MidRegimeState(before['mid_regime'], mid['present'], mid['target'],
            None if mid['since'] is None else _time(mid['since'])), mid_facts, _time(clocks['opening']).strftime('%H:%M'),
            'normal' if vix is None else self.adapter._classify_vix(vix), vix)
        mid_now = None if plan.clock_stage == 'none' else runtime._now()
        if mid_now is not None: clocks['mid_' + plan.clock_stage] = mid_now.isoformat()
        technical = calc.complete_mid_regime_transition(plan, mid_now)
        clocks['technical_update'] = runtime._now().isoformat()
        after = deepcopy(before)
        after.update(mid_regime=technical.state.current_regime,
            mid_pending=_pending_dict(technical.state), market_trend={**sidecar.market_trend,
                'present': True, 'classified_at': clocks['sidecar']}, last_update=clocks['technical_update'],
            regime_data={'kospi_change': technical.kospi_change, 'kosdaq_change': technical.kosdaq_change,
                'avg_change': technical.avg_change, 'avg_vs_open': technical.avg_vs_open, 'vix': vix,
                'vix_state': 'normal' if vix is None else self.adapter._classify_vix(vix)})
        expert_payload, outcome = None, 'missing'
        if expert_orchestrator is not None:
            try:
                score = expert_orchestrator.aggregate_regime_score()
                consensus = expert_orchestrator.bear_consensus(threshold_confidence=.7, min_count=2)
                _number(score)
                if type(consensus) is not bool: raise ValueError('invalid_expert_consensus')
                expert_payload, outcome = {'score': score, 'bear_consensus': consensus}, 'success'
            except Exception: outcome = 'failed'
        if expert_payload is not None:
            pending = before['expert_pending']
            plan = calc.plan_expert_transition(calc.ExpertState(after['mid_regime'], pending['present'], pending['target'],
                None if pending['since'] is None else _time(pending['since'])), expert_payload['score'], expert_payload['bear_consensus'])
            expert_now = None if plan.clock_stage == 'none' else runtime._now()
            if expert_now is not None: clocks['expert_' + plan.clock_stage] = expert_now.isoformat()
            adjusted = calc.complete_expert_transition(plan, expert_now)
            after['mid_regime'], after['expert_pending'] = adjusted.state.current_regime, _pending_dict(adjusted.state)
            after['regime_data'].update(expert_score=score, expert_bear_consensus=consensus)
        expert_receipt = await self.sources.complete(expert, outcome, expert_payload, scope_token=token)
        if _context_reason(runtime, runtime.owner.state, ticket):
            return await finish('success', {}, scope_token=token)
        observed_expert = json.loads(self.sources.capture_reads(ticket, source_lanes=('expert_regime',)))
        if observed_expert['sources']['expert_regime']['ticket']['operation_id'] != expert.operation_id:
            # Preserve the fact actually used, not a concurrent expert's terminal.
            current = runtime.owner.state
            observed_expert['sources']['expert_regime'] = _source_at(
                current['risk_sources']['records'][expert.operation_id], None,
                current.get('risk_input_seals', {}).get('records', {}))
        expected['sources'].update(observed_expert['sources'])
        after['source_refs'] = {'trend': None, 'vix': vix_ref, 'expert': None if expert_receipt.status != 'accepted'
            else {'operation_id': expert.operation_id, 'committed_version': expert_receipt.committed_version}}
        inputs = {'observations': [fact.observation_json for fact in facts], 'clocks': clocks,
                  'selected_vix': vix_ref, 'expert': expert_payload,
                  'rule_digest': _HORIZON_RULE if state['regime_policy']['schema'] == 3 else _RULE}
        seal = await self.sources.seal(ticket, source_lanes=('vix_regime', 'expert_regime'),
            retained_sources=retained, versioned_policy_reads=reads, inputs=inputs,
            scope_token=token, expected_reads_json=canonical(expected))
        candidate = deepcopy(state); candidate['regime_policy']['trend_state'] = after
        payload = {'before': before, 'after': after, 'sidecar_before': sidecar_before,
            'sidecar_after': sidecar.sidecar_active, 'engine_regime': effective_regime(candidate, runtime._now()), 'inputs': inputs}
        receipt = await finish('success', payload, source='kis:index-trend',
            source_event_id=digest([fact.source_event_id for fact in facts]),
            received_at=max(fact.received_at for fact in facts), classified_at=_time(clocks['sidecar']), scope_token=token)
        if receipt.status == 'accepted' and refresh_vix:
            self._schedule_vix(token)
        return receipt

    def _schedule_vix(self, token):
        if self._vix_refresh_task is not None and not self._vix_refresh_task.done(): return
        runtime = self.runtime
        async def runner():
            if runtime._closing or runtime.day_admission_closed: return True
            with runtime.command_scope() as own_token:
                try:
                    ticket = await self._begin('vix:' + uuid4().hex, 'vix_regime', own_token)
                except _OwnerBeginCancelled:
                    return True
                try:
                    value = (await self._vix_fetcher() if self._vix_fetcher is not None
                             else await asyncio.to_thread(self.adapter._fetch_vix_sync))
                    if value is not None: _number(value)
                except asyncio.CancelledError:
                    await self.sources.complete(ticket, 'cancelled', scope_token=own_token)
                    return True
                except Exception:
                    await self.sources.complete(ticket, 'failed', scope_token=own_token)
                    return True
                when = runtime._now()
                await self.sources.complete(ticket, 'missing' if value is None else 'success',
                    None if value is None else {'value': value, 'fetched_at': when.isoformat()},
                    source='vix', source_event_id=ticket.operation_id, received_at=when, scope_token=own_token)
                return True
        self._vix_refresh_task = runtime.start_command_result(token, runner)


def _pending_dict(state):
    return {'present': state.pending_present, 'target': state.pending_regime,
            'since': None if state.pending_since is None else state.pending_since.isoformat()}
