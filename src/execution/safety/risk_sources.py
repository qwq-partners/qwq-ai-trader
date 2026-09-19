"""단일 owner의 위험 source 수명. 실제 provider 인증/정책/거래 허가는 아니다.

trusted 설치자가 공급한 사실만 보존한다. 실제 batch/adapter/ExitManager writer,
보호 전이와 effect 발행은 후속이며, market_as_of 없는 REST 성공은 진입 unknown이다.
"""
from __future__ import annotations

import asyncio
from contextlib import nullcontext
from copy import deepcopy
from dataclasses import asdict, dataclass
from datetime import datetime
import json
from typing import Mapping

from .application import ApplicationBlocked
from .day_recovery import aware, day, scope_reason, text
from .guards import RiskSnapshot
from .protection_recovery import digest

_KINDS = {'intraday_5m': 'intraday', 'noon_index': 'intraday',
          'index_trend': 'index_trend', 'llm_regime': 'llm_regime',
          'vix_regime': 'vix_regime', 'expert_regime': 'expert_regime',
          'llm_morning_diagnosis': 'llm_morning_diagnosis'}
_LANES = frozenset(_KINDS.values())
_OUTCOMES = frozenset({'success', 'missing', 'failed', 'cancelled'})
_LEVELS = frozenset({'normal', 'caution', 'crash', 'severe'})


@dataclass(frozen=True)
class RefreshTicket:
    operation_id: str
    account_scope: str
    business_day: str
    generation: int
    fence_id: str | None
    kind: str
    lane: str
    sequence: int
    admission_version: int
    begun_at: str
    dependencies: tuple[tuple[str, int], ...]
    request_digest: str

    def __post_init__(self):
        for field in ('operation_id', 'account_scope', 'request_digest'):
            text(getattr(self, field))
        day(self.business_day)
        _integer(self.generation)
        _integer(self.sequence, 1)
        _integer(self.admission_version, 1)
        _time(self.begun_at)
        if (type(self.kind) is not str or self.kind not in _KINDS
                or self.lane != _KINDS[self.kind] or type(self.dependencies) is not tuple
                or self.dependencies != tuple(sorted(_dependencies(dict(self.dependencies)).items()))):
            raise ValueError('invalid_risk_source_ticket')
        for lane, version in self.dependencies:
            _integer(version, 1)


@dataclass(frozen=True)
class RefreshReceipt:
    operation_id: str
    sequence: int
    status: str
    reason: str
    committed_version: int
    outcome_digest: str

    def __post_init__(self):
        text(self.operation_id)
        _integer(self.sequence, 1)
        _integer(self.committed_version, 1)
        text(self.outcome_digest)


@dataclass(frozen=True)
class SourceRead:
    """현재 소비 권한과 원 terminal 사실은 별개다. 거래 허가가 아니다."""
    lane: str
    operation_id: str | None
    committed_version: int | None
    authority_status: str
    terminal_status: str | None
    reason: str
    envelope_json: str | None


class _SourceDependencyChanged(ValueError):
    """SQL 접수 전 reducer가 확인한 정상 dependency 경합만 표시한다."""


class _SourceAuthority:
    """한 detached state의 현재 graph. 과거 receipt 검증에는 사용하지 않는다."""
    def __init__(self, state, business_day, pending=()):
        self.state = state
        self.root = state.get('risk_sources', {'records': {}, 'latest': {}})
        self.seals = state.get('risk_input_seals', {}).get('records', {})
        self.business_day = business_day
        self.pending = frozenset(pending)
        self.memo = {}
        self.visiting = set()

    def _pending(self, result):
        status, reason, lanes = result
        return ('pending', 'source_pending', lanes) if self.pending.intersection(lanes) else result

    def dependency(self, lane, version):
        row = _current(self.root, lane)
        result = self.inspect(row) if row else ('missing', 'source_missing', frozenset({lane}))
        terminal = row['terminal'] if row else None
        if (not terminal or terminal['receipt']['status'] != 'accepted'
                or terminal['receipt']['committed_version'] != version):
            result = ('stale', 'dependency', result[2])
        return self._pending(result)

    def inputs(self, row):
        from .risk_input_seal import _json, _source_reads
        lanes = set()
        reason = ''
        for lane, version in row['ticket']['dependencies']:
            status, _, closure = self.dependency(lane, version)
            lanes.update(closure)
            if status != 'current':
                reason = 'dependency'
        seal = self.seals.get(row['ticket']['operation_id'])
        if seal is None:
            if row['request'].get('require_seal', False):
                reason = reason or 'input_seal'
        else:
            original = seal['original']
            lanes.update(original['request']['source_lanes'])
            if seal['conflict'] or original['status'] != 'sealed':
                reason = reason or 'input_seal'
            else:
                facts = original['reads']
                try:
                    current = _source_reads(self.state, original['request'])
                    if any(_json(current[key]) != _json(facts[key]) for key in ('sources', 'retained')):
                        reason = reason or 'source_read_changed'
                except ValueError:
                    reason = reason or 'source_read_changed'
                # 실패/결측/이미 충돌한 관측은 사실 leaf다. retained는 재귀하지 않는다.
                for observed in facts['sources'].values():
                    if (observed and not observed['conflict'] and not observed['input_seal_conflict']
                            and observed['terminal'] and observed['terminal']['receipt']['status'] == 'accepted'):
                        selected = self.root['records'].get(observed['ticket']['operation_id'])
                        status, _, closure = self.inspect(selected)
                        lanes.update(closure)
                        if status != 'current':
                            reason = reason or 'observed_source_not_current'
        return self._pending(('stale' if reason else 'current', reason, frozenset(lanes)))

    def inspect(self, row):
        if row is None:
            return 'missing', 'source_missing', frozenset()
        ticket = row['ticket']
        operation, lane = ticket['operation_id'], ticket['lane']
        if operation in self.visiting:
            return 'stale', 'source_cycle', frozenset({lane})
        if operation in self.memo:
            return self.memo[operation]
        self.visiting.add(operation)
        try:
            terminal = row['terminal']
            result = (self.inputs(row) if terminal and terminal['receipt']['status'] == 'accepted'
                      else ('current', '', frozenset({lane})))
            seal = self.seals.get(operation)
            if ticket['business_day'] != self.business_day or self.root['latest'].get(lane) != operation:
                result = 'stale', 'source_context', result[2]
            elif row['conflict'] or (seal and seal['conflict']):
                result = 'conflict', 'source_conflict', result[2]
            elif terminal is None:
                result = 'pending', 'source_incomplete', result[2]
            elif terminal['receipt']['status'] == 'stale':
                result = 'stale', terminal['receipt']['reason'], result[2]
            result = self._pending((result[0], result[1], result[2] | {lane}))
            self.memo[operation] = result
            return result
        finally:
            self.visiting.remove(operation)


def _integer(value, minimum=0):
    if type(value) is not int or value < minimum:
        raise ValueError('invalid_risk_source_integer')
    return value


def _time(value):
    if type(value) is not str:
        raise ValueError('invalid_risk_source_time')
    parsed = aware(datetime.fromisoformat(value))
    if parsed.isoformat() != value:
        raise ValueError('noncanonical_risk_source_time')
    return parsed


def _keys(value, expected):
    if type(value) is not dict or set(value) != set(expected):
        raise ValueError('invalid_risk_source_fields')


def _dependencies(value):
    if not isinstance(value, Mapping):
        raise ValueError('invalid_risk_dependency')
    result = dict(value)
    for lane, version in result.items():
        if lane not in _LANES or type(version) is not int or version <= 0:
            raise ValueError('invalid_risk_dependency')
    return dict(sorted(result.items()))


def _request(operation_id, kind, dependencies, scope, business_day, generation, fence_id, require_seal=False):
    if type(kind) is not str or kind not in _KINDS:
        raise ValueError('unsupported_risk_source_kind')
    if type(require_seal) is not bool:
        raise ValueError('invalid_input_seal_requirement')
    return {**({'require_seal': True} if require_seal else {}),
            'operation_id': text(operation_id), 'kind': kind,
            'dependencies': _dependencies(dependencies), 'account_scope': text(scope),
            'business_day': day(business_day), 'generation': _integer(generation),
            'fence_id': None if fence_id is None else text(fence_id)}


def _ticket(row):
    value = dict(row['ticket'])
    value['dependencies'] = tuple(tuple(pair) for pair in value['dependencies'])
    return RefreshTicket(**value)


def _ticket_dict(ticket):
    value = asdict(ticket)
    value['dependencies'] = [list(pair) for pair in ticket.dependencies]
    return value


def _envelope(outcome, payload, *, source, source_event_id, received_at,
              market_as_of, classified_at, recovery_until):
    if outcome not in _OUTCOMES or (payload is not None and type(payload) is not dict):
        raise ValueError('invalid_risk_source_outcome')
    # JSON roundtrip fixes caller-owned mutable data and rejects NaN/Infinity.
    value = json.loads(json.dumps(payload or {}, allow_nan=False, sort_keys=True))
    if 'level' in value and (type(value['level']) is not str or value['level'] not in _LEVELS):
        raise ValueError('invalid_risk_source_level')
    return {'outcome': outcome, 'payload': value,
            'source': text(source) if source else '',
            'source_event_id': text(source_event_id) if source_event_id else '',
            **{key: None if val is None else aware(val).isoformat() for key, val in (
                ('received_at', received_at), ('market_as_of', market_as_of),
                ('classified_at', classified_at), ('recovery_until', recovery_until))}}


def _valid_envelope(envelope, ticket, completed_at):
    _keys(envelope, ('outcome', 'payload', 'source', 'source_event_id', 'received_at',
                     'market_as_of', 'classified_at', 'recovery_until'))
    canonical = _envelope(**{key: (None if val is None else _time(val))
                             if key in {'received_at', 'market_as_of', 'classified_at', 'recovery_until'}
                             else val for key, val in envelope.items()})
    if canonical != envelope:
        raise ValueError('noncanonical_risk_source_outcome')
    times = {key: _time(envelope[key]) for key in
             ('received_at', 'market_as_of', 'classified_at') if envelope[key] is not None}
    if any(stamp > completed_at for stamp in times.values()):
        raise ValueError('future_risk_source_time')
    if 'market_as_of' in times:
        if (not envelope['source'] or not envelope['source_event_id'] or 'received_at' not in times
                or times['market_as_of'] > times['received_at']
                or times['market_as_of'].date().isoformat() != ticket.business_day):
            raise ValueError('invalid_risk_market_provenance')


def _current(root, lane):
    operation_id = root['latest'].get(lane)
    return root['records'].get(operation_id) if operation_id else None


def _dependency_current(root, lane, version, business_day, *, cutoff=None, visited=frozenset(), seals=None):
    if lane in visited:
        return False
    candidates = [row for row in root['records'].values() if row['ticket']['lane'] == lane
                  and (cutoff is None or row['ticket']['admission_version'] < cutoff)]
    row = max(candidates, key=lambda value: value['ticket']['sequence']) if candidates else None
    seal = (seals or {}).get(row['ticket']['operation_id']) if row else None
    seal_conflict = seal and seal['conflict'] and (
        cutoff is None or seal['conflict']['version'] < cutoff)
    return bool(row and row['ticket']['business_day'] == business_day
                and not seal_conflict
                and (not row['conflict'] or (cutoff is not None and
                     row['conflict']['receipt']['committed_version'] >= cutoff))
                and row['terminal'] and row['terminal']['receipt']['status'] == 'accepted'
                and row['terminal']['receipt']['committed_version'] == version
                and (cutoff is None or version < cutoff)
                and all(_dependency_current(root, dep_lane, dep_version, business_day,
                        cutoff=cutoff, visited=visited | {lane}, seals=seals)
                        for dep_lane, dep_version in row['ticket']['dependencies']))


def validate_risk_sources(state, version):
    """게시 전 모든 source DTO와 과거 의존/최신 lane 연결을 검증한다."""
    if 'risk_sources' not in state:
        from .risk_input_seal import validate_risk_input_seals
        validate_risk_input_seals(state, version)
        return
    root = state['risk_sources']
    _keys(root, ('schema', 'account_scope', 'market', 'next_sequence', 'latest', 'records'))
    if type(root['schema']) is not int or root['schema'] != 1 or root['market'] != 'KR':
        raise ValueError('invalid_risk_source_schema')
    if scope_reason(state, text(root['account_scope'])):
        raise ValueError('risk_source_scope_conflict')
    _integer(root['next_sequence'], 1)
    if type(root['latest']) is not dict or type(root['records']) is not dict:
        raise ValueError('invalid_risk_source_registry')
    sequences, admissions, latest, history = set(), set(), {}, {}
    for operation_id, row in root['records'].items():
        _keys(row, ('request', 'ticket', 'terminal', 'conflict'))
        request, raw = row['request'], row['ticket']
        _keys(request, ('operation_id', 'kind', 'dependencies', 'account_scope',
                        'business_day', 'generation', 'fence_id',
                        *(('require_seal',) if 'require_seal' in request else ())))
        if 'require_seal' in request and request['require_seal'] is not True:
            raise ValueError('noncanonical_input_seal_requirement')
        if _request(**{('scope' if key == 'account_scope' else key): val
                       for key, val in request.items()}) != request:
            raise ValueError('noncanonical_risk_source_request')
        _keys(raw, RefreshTicket.__dataclass_fields__)
        ticket = _ticket(row)
        _integer(ticket.generation)
        _integer(ticket.sequence, 1)
        _integer(ticket.admission_version, 1)
        if (ticket.operation_id != operation_id or ticket.account_scope != root['account_scope']
                or ticket.admission_version > version or ticket.sequence in sequences
                or ticket.admission_version in admissions or ticket.lane != _KINDS[ticket.kind]
                or ticket.request_digest != digest(request)
                or ticket.dependencies != tuple(sorted(request['dependencies'].items()))
                or any(getattr(ticket, key) != request[key] for key in
                       ('operation_id', 'kind', 'account_scope', 'business_day', 'generation', 'fence_id'))
                or _time(ticket.begun_at).date().isoformat() != ticket.business_day
                or ticket.business_day > state['risk']['day']):
            raise ValueError('risk_source_ticket_crosslink_conflict')
        sequences.add(ticket.sequence)
        admissions.add(ticket.admission_version)
        if ticket.lane not in latest or raw['sequence'] > latest[ticket.lane]['sequence']:
            latest[ticket.lane] = raw
        terminal = row['terminal']
        if terminal is not None:
            _keys(terminal, ('envelope', 'receipt', 'completed_at'))
            _keys(terminal['receipt'], RefreshReceipt.__dataclass_fields__)
            receipt = RefreshReceipt(**terminal['receipt'])
            completed = _time(terminal['completed_at'])
            _valid_envelope(terminal['envelope'], ticket, completed)
            _integer(receipt.committed_version, 1)
            if (receipt.operation_id != operation_id or receipt.sequence != ticket.sequence
                    or not ticket.admission_version < receipt.committed_version <= version
                    or completed < _time(ticket.begun_at)
                    or receipt.outcome_digest != digest(terminal['envelope'])
                    or (receipt.status, receipt.reason) not in {
                        ('accepted', ''), ('missing', ''), ('failed', ''), ('cancelled', ''),
                        ('stale', 'superseded'), ('stale', 'day_or_generation'), ('stale', 'dependency'),
                        ('stale', 'input_seal')}):
                raise ValueError('risk_source_terminal_crosslink_conflict')
            if receipt.status != 'stale' and receipt.status != (
                    'accepted' if terminal['envelope']['outcome'] == 'success' else terminal['envelope']['outcome']):
                raise ValueError('risk_source_outcome_status_conflict')
            history[(ticket.lane, receipt.committed_version)] = row
        conflict = row['conflict']
        if conflict is not None:
            _keys(conflict, ('envelope', 'receipt', 'completed_at'))
            _keys(conflict['receipt'], RefreshReceipt.__dataclass_fields__)
            receipt = RefreshReceipt(**conflict['receipt'])
            _valid_envelope(conflict['envelope'], ticket, _time(conflict['completed_at']))
            if (terminal is None or receipt.status != 'conflict' or receipt.reason != 'outcome_conflict'
                    or receipt.operation_id != operation_id or receipt.sequence != ticket.sequence
                    or type(receipt.committed_version) is not int
                    or not terminal['receipt']['committed_version'] < receipt.committed_version <= version
                    or receipt.outcome_digest != digest(conflict['envelope'])
                    or receipt.outcome_digest == terminal['receipt']['outcome_digest']):
                raise ValueError('risk_source_conflict_crosslink_conflict')
    if (root['next_sequence'] != len(sequences) + 1
            or sequences != set(range(1, root['next_sequence']))
            or root['latest'] != {lane: row['operation_id'] for lane, row in latest.items()}):
        raise ValueError('risk_source_latest_crosslink_conflict')
    ordered = sorted(root['records'].values(), key=lambda row: row['ticket']['sequence'])
    if [row['ticket']['admission_version'] for row in ordered] != sorted(admissions):
        raise ValueError('risk_source_sequence_version_conflict')
    used_versions = set(admissions)
    for row in root['records'].values():
        ticket = row['ticket']
        terminal = row['terminal']
        if terminal:
            receipt = terminal['receipt']
            terminal_version = receipt['committed_version']
            if terminal_version in used_versions:
                raise ValueError('risk_source_duplicate_commit_version')
            used_versions.add(terminal_version)
            superseded = any(other['ticket']['lane'] == ticket['lane']
                             and ticket['sequence'] < other['ticket']['sequence']
                             and other['ticket']['admission_version'] < terminal_version
                             for other in root['records'].values())
            if receipt['status'] != 'stale' and (
                    superseded or _time(terminal['completed_at']).date().isoformat() != ticket['business_day']):
                raise ValueError('risk_source_accepted_context_conflict')
            if receipt['reason'] == 'superseded' and not superseded:
                raise ValueError('risk_source_stale_context_conflict')
        if row['conflict']:
            conflict_version = row['conflict']['receipt']['committed_version']
            if conflict_version in used_versions:
                raise ValueError('risk_source_duplicate_commit_version')
            used_versions.add(conflict_version)
        for lane, dep_version in row['ticket']['dependencies']:
            dependency = history.get((lane, dep_version))
            if (not dependency or dependency['terminal']['receipt']['status'] != 'accepted'
                    or dep_version >= row['ticket']['admission_version']
                    or dependency['ticket']['business_day'] != row['ticket']['business_day']):
                raise ValueError('risk_source_dependency_crosslink_conflict')
            if not _dependency_current(root, lane, dep_version, ticket['business_day'],
                                       cutoff=ticket['admission_version'],
                                       seals=state.get('risk_input_seals', {}).get('records', {})):
                raise ValueError('risk_source_historical_dependency_conflict')
            if terminal and terminal['receipt']['status'] != 'stale':
                if not _dependency_current(root, lane, dep_version, ticket['business_day'], cutoff=terminal_version,
                                           seals=state.get('risk_input_seals', {}).get('records', {})):
                    raise ValueError('risk_source_accepted_dependency_conflict')
    from .risk_input_seal import validate_risk_input_seals
    validate_risk_input_seals(state, version)


class RiskSourceCoordinator:
    def __init__(self, runtime, *, completion_reducer=None):
        self.runtime = runtime
        if completion_reducer is not None and not callable(completion_reducer):
            raise TypeError('synchronous_completion_reducer_required')
        self._completion_reducer = completion_reducer
        # 동일 runtime의 여러 facade도 첫 await 전 장벽을 공유한다.
        if not hasattr(runtime, '_risk_source_pending'):
            runtime._risk_source_pending = {}

    def _context(self, state):
        runtime = self.runtime
        runtime._require_day_admission()
        reason = scope_reason(state, runtime.account_scope)
        if reason:
            raise ApplicationBlocked(reason)
        if state.get('risk_sources', {}).get('account_scope', runtime.account_scope) != runtime.account_scope:
            raise ApplicationBlocked('risk_source_scope_conflict')
        return runtime._now().date().isoformat(), runtime._day_generation, runtime._day_fence_id

    async def begin(self, operation_id, kind, *, dependencies=None, require_seal=False):
        """Trusted new callers opt in; the requirement is durable and ticket-digest bound."""
        runtime = self.runtime
        state = runtime.owner.state
        business_day, generation, fence_id = self._context(state)
        request = _request(operation_id, kind, {} if dependencies is None else dependencies, runtime.account_scope,
                           business_day, generation, fence_id, require_seal)
        request_digest = digest(request)
        root = state.get('risk_sources', {'latest': {}, 'records': {}})
        existing = root['records'].get(operation_id)
        if existing is not None and existing['ticket']['request_digest'] != request_digest:
            raise ValueError('risk_source_operation_conflict')
        authority = _SourceAuthority(state, business_day, runtime._risk_source_pending.values())
        if existing is None and any(authority.dependency(lane, ver)[0] != 'current'
                                    for lane, ver in request['dependencies'].items()):
            raise ValueError('risk_source_dependency_not_current')
        with runtime.command_scope() as token:
            runtime._risk_source_pending[token] = _KINDS[kind]
            admission_reason = ''
            async def execute():
                nonlocal admission_reason
                try:
                    def reduce(candidate):
                        current = self._context(candidate)
                        if current != (business_day, generation, fence_id):
                            raise ApplicationBlocked('risk_source_day_changed')
                        registry = candidate.setdefault('risk_sources', {
                            'schema': 1, 'account_scope': runtime.account_scope, 'market': 'KR',
                            'next_sequence': 1, 'latest': {}, 'records': {}})
                        if operation_id in registry['records']:
                            if registry['records'][operation_id]['ticket']['request_digest'] != request_digest:
                                raise ValueError('risk_source_operation_conflict')
                            return candidate
                        # 자기 접수는 아직 candidate에 쓰기 전이다. 다른 facade의 같은
                        # lane token까지 제외하면 실제 경쟁 갱신을 놓치므로 token만 뺀다.
                        authority = _SourceAuthority(candidate, business_day,
                            (lane for pending_token, lane in runtime._risk_source_pending.items()
                             if pending_token is not token))
                        if any(authority.dependency(lane, ver)[0] != 'current'
                               for lane, ver in request['dependencies'].items()):
                            raise _SourceDependencyChanged('risk_source_dependency_not_current')
                        ticket = RefreshTicket(operation_id, runtime.account_scope, business_day,
                            generation, fence_id, kind, _KINDS[kind], registry['next_sequence'],
                            runtime.owner.version + 1, runtime._now().isoformat(),
                            tuple(request['dependencies'].items()), request_digest)
                        registry['records'][operation_id] = {
                            'request': request, 'ticket': _ticket_dict(ticket), 'terminal': None, 'conflict': None}
                        registry['latest'][ticket.lane] = operation_id
                        registry['next_sequence'] += 1
                        validate_risk_sources(candidate, runtime.owner.version + 1)
                        return candidate
                    await runtime.owner.mutate('risk-begin:' + request_digest, reduce)
                    return True
                except _SourceDependencyChanged as exc:
                    # 정상 거부는 task drain 성공이지만 source 접수 성공이 아니다.
                    # lookup/SQL/게시 실패나 일반 ValueError는 이 경계에 포함하지 않는다.
                    admission_reason = str(exc)
                    return True
                finally:
                    runtime._risk_source_pending.pop(token, None)
            task = runtime.start_command_result(token, execute)
            await asyncio.shield(task)
            if admission_reason:
                raise ValueError(admission_reason)
            if self._context(runtime.owner.state) != (business_day, generation, fence_id):
                raise ApplicationBlocked('risk_source_day_changed')
            return _ticket(runtime.owner.state['risk_sources']['records'][operation_id])

    async def seal(self, ticket, *, source_lanes=(), retained_sources=(), policy_reads=(),
                   versioned_policy_reads=(), inputs, scope_token=None):
        """Seal actual owner reads; trusted callers remain responsible for selecting all inputs."""
        from .risk_input_seal import seal_input
        return await seal_input(self, ticket, source_lanes=source_lanes,
            retained_sources=retained_sources, policy_reads=policy_reads, inputs=inputs,
            versioned_policy_reads=versioned_policy_reads, scope_token=scope_token)

    async def complete(self, ticket, outcome, payload=None, *, source='', source_event_id='',
                       received_at=None, market_as_of=None, classified_at=None, recovery_until=None,
                       scope_token=None):
        """외부 fetch를 감싼 기존 scope token은 종료 중에도 같은 caller만 사용한다."""
        runtime = self.runtime
        if not isinstance(ticket, RefreshTicket):
            raise TypeError('RefreshTicket_required')
        row = runtime.owner.state.get('risk_sources', {}).get('records', {}).get(ticket.operation_id)
        if row is None or _ticket_dict(ticket) != row['ticket']:
            raise ValueError('risk_source_ticket_mismatch')
        envelope = _envelope(outcome, payload, source=source, source_event_id=source_event_id,
                             received_at=received_at, market_as_of=market_as_of,
                             classified_at=classified_at, recovery_until=recovery_until)
        _valid_envelope(envelope, ticket, runtime._now())
        outcome_digest = digest(envelope)
        with (runtime.command_scope() if scope_token is None else nullcontext(scope_token)) as token:
            async def execute():
                def reduce(candidate):
                    registry = candidate['risk_sources']
                    current = registry['records'][ticket.operation_id]
                    if current['ticket'] != _ticket_dict(ticket):
                        raise ValueError('risk_source_ticket_mismatch')
                    if current['conflict']:
                        return candidate
                    prior = current['terminal']
                    if prior and prior['receipt']['outcome_digest'] == outcome_digest:
                        return candidate
                    authority = _SourceAuthority(candidate, ticket.business_day,
                                                 runtime._risk_source_pending.values())
                    status, reason = ('accepted' if outcome == 'success' else outcome), ''
                    if prior:
                        status, reason = 'conflict', 'outcome_conflict'
                    elif (ticket.business_day != candidate['risk']['day']
                          or ticket.business_day != runtime._now().date().isoformat()
                          or ticket.generation != runtime._day_generation
                          or ticket.fence_id != runtime._day_fence_id or runtime.day_admission_closed
                          or ticket.account_scope != runtime.account_scope
                          or scope_reason(candidate, runtime.account_scope)):
                        status, reason = 'stale', 'day_or_generation'
                    elif registry['latest'].get(ticket.lane) != ticket.operation_id:
                        status, reason = 'stale', 'superseded'
                    elif any(authority.dependency(lane, ver)[0] != 'current'
                             for lane, ver in ticket.dependencies):
                        status, reason = 'stale', 'dependency'
                    if status == 'accepted':
                        from .risk_input_seal import input_seal_current
                        if (not input_seal_current(candidate, ticket)
                                or authority.inputs(current)[0] != 'current'):
                            status, reason = 'stale', 'input_seal'
                    if status == 'accepted' and self._completion_reducer is not None:
                        before_sources = deepcopy(registry)
                        before_seals = deepcopy(candidate.get('risk_input_seals'))
                        candidate = runtime.owner._require_sync(self._completion_reducer(
                            candidate, ticket, deepcopy(envelope), runtime.owner.version + 1))
                        if type(candidate) is not dict or candidate.get('risk_sources') != before_sources:
                            raise ValueError('completion_reducer_changed_risk_sources')
                        if candidate.get('risk_input_seals') != before_seals:
                            raise ValueError('completion_reducer_changed_input_seals')
                        registry = candidate['risk_sources']
                        current = registry['records'][ticket.operation_id]
                    receipt = RefreshReceipt(ticket.operation_id, ticket.sequence, status, reason,
                                             runtime.owner.version + 1, outcome_digest)
                    current['conflict' if prior else 'terminal'] = {
                        'envelope': envelope, 'receipt': asdict(receipt),
                        'completed_at': runtime._now().isoformat()}
                    validate_risk_sources(candidate, runtime.owner.version + 1)
                    return candidate
                await runtime.owner.mutate('risk-complete:' + ticket.request_digest + ':' + outcome_digest, reduce)
                return True
            await asyncio.shield(runtime.start_command_result(token, execute))
            current = runtime.owner.state['risk_sources']['records'][ticket.operation_id]
            return RefreshReceipt(**(current['conflict'] or current['terminal'])['receipt'])

    def read_source(self, lane, *, expected_version=None):
        """동기 detached 조회. current+failed는 현재 실패 사실이며 성공이 아니다."""
        from .risk_input_seal import _context_reason, _json
        if type(lane) is not str or lane not in _LANES:
            raise ValueError('invalid_risk_source_lane')
        if expected_version is not None:
            _integer(expected_version, 1)
        runtime = self.runtime
        state, version = runtime.owner.state, runtime.owner.version
        root = state.get('risk_sources')
        row = _current(root, lane) if root else None
        terminal = row['terminal'] if row else None
        source_version = ((row['conflict'] or row['terminal'])['receipt']['committed_version']
                          if row and (row['conflict'] or row['terminal']) else
                          row['ticket']['admission_version'] if row else None)
        def result(status, reason=''):
            return SourceRead(lane, row['ticket']['operation_id'] if row else None,
                source_version, status, terminal['receipt']['status'] if terminal else None,
                reason, _json(terminal['envelope']) if terminal else None)
        if (not runtime.owner.healthy or runtime.owner.published_version != version
                or runtime.engine._execution_version != version or runtime._closing
                or runtime.day_admission_closed):
            return result('unavailable', 'runtime_unavailable')
        if scope_reason(state, runtime.account_scope):
            return result('unavailable', 'scope_conflict')
        authority = _SourceAuthority(state, runtime._now().date().isoformat(),
                                     runtime._risk_source_pending.values())
        if row is None:
            return result('pending' if lane in authority.pending else 'missing', 'source_missing')
        validate_risk_sources(state, version)
        context = _context_reason(runtime, state, _ticket(row))
        if context:
            return result('stale', context)
        status, reason, _ = authority.inspect(row)
        if expected_version is not None and source_version != expected_version:
            status, reason = 'stale', 'source_version_changed'
        return result(status, reason)

    def snapshot(self):
        read = self.read_source('intraday')
        state = self.runtime.owner.state
        row = state.get('risk_sources', {}).get('records', {}).get(read.operation_id)
        sequence = row['ticket']['sequence'] if row else 0
        def unknown(status):
            return RiskSnapshot(read.committed_version or 0, sequence, status, None, None)
        if read.authority_status != 'current':
            status = read.authority_status
            if status not in {'pending', 'conflict'}:
                status = ('stale' if status == 'stale' and read.terminal_status == 'stale'
                          else 'missing')
            return unknown(status)
        terminal = row['terminal']
        receipt, envelope = terminal['receipt'], terminal['envelope']
        if receipt['status'] != 'accepted':
            return unknown(receipt['status'])
        if envelope['market_as_of'] is None or envelope['payload'].get('level') not in _LEVELS:
            return unknown('missing')
        return RiskSnapshot(receipt['committed_version'], sequence, 'success',
                            _time(envelope['market_as_of']), envelope['payload']['level'],
                            _time(envelope['recovery_until']) if envelope['recovery_until'] else None)
