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
        if existing is None and any(not _dependency_current(root, lane, ver, business_day,
                                    seals=state.get('risk_input_seals', {}).get('records', {}))
                                    for lane, ver in request['dependencies'].items()):
            raise ValueError('risk_source_dependency_not_current')
        with runtime.command_scope() as token:
            runtime._risk_source_pending[token] = _KINDS[kind]
            async def execute():
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
                        if any(not _dependency_current(registry, lane, ver, business_day,
                               seals=candidate.get('risk_input_seals', {}).get('records', {}))
                               for lane, ver in request['dependencies'].items()):
                            raise ValueError('risk_source_dependency_not_current')
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
                finally:
                    runtime._risk_source_pending.pop(token, None)
            task = runtime.start_command_result(token, execute)
            await asyncio.shield(task)
            if self._context(runtime.owner.state) != (business_day, generation, fence_id):
                raise ApplicationBlocked('risk_source_day_changed')
            return _ticket(runtime.owner.state['risk_sources']['records'][operation_id])

    async def seal(self, ticket, *, source_lanes=(), retained_sources=(), policy_reads=(),
                   inputs, scope_token=None):
        """Seal actual owner reads; trusted callers remain responsible for selecting all inputs."""
        from .risk_input_seal import seal_input
        return await seal_input(self, ticket, source_lanes=source_lanes,
            retained_sources=retained_sources, policy_reads=policy_reads, inputs=inputs,
            scope_token=scope_token)

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
                    elif any(not _dependency_current(registry, lane, ver, ticket.business_day,
                             seals=candidate.get('risk_input_seals', {}).get('records', {}))
                             for lane, ver in ticket.dependencies):
                        status, reason = 'stale', 'dependency'
                    if status == 'accepted':
                        from .risk_input_seal import input_seal_current
                        if not input_seal_current(candidate, ticket):
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

    def snapshot(self):
        runtime = self.runtime
        state, version = runtime.owner.state, runtime.owner.version
        root = state.get('risk_sources')
        row = _current(root, 'intraday') if root else None
        sequence = row['ticket']['sequence'] if row else 0
        source_version = ((row['conflict'] or row['terminal'])['receipt']['committed_version']
                          if row and (row['conflict'] or row['terminal']) else
                          row['ticket']['admission_version'] if row else 0)
        def unknown(status):
            return RiskSnapshot(source_version, sequence, status, None, None)
        if (not runtime.owner.healthy or runtime.owner.published_version != version
                or runtime.engine._execution_version != version or runtime._closing
                or runtime.day_admission_closed):
            return unknown('missing')
        relevant_lanes = {'intraday'}
        todo = [row] if row else []
        while todo:
            current = todo.pop()
            for lane, _ in current['ticket']['dependencies']:
                if lane not in relevant_lanes:
                    relevant_lanes.add(lane)
                    dependency = _current(root, lane)
                    if dependency:
                        todo.append(dependency)
        if relevant_lanes.intersection(runtime._risk_source_pending.values()):
            return unknown('pending')
        if row is None:
            return unknown('missing')
        validate_risk_sources(state, version)
        if row['ticket']['business_day'] != runtime._now().date().isoformat():
            return unknown('missing')
        if row['conflict']:
            return unknown('conflict')
        seal = state.get('risk_input_seals', {}).get('records', {}).get(row['ticket']['operation_id'])
        if seal and seal['conflict']:
            return unknown('conflict')
        terminal = row['terminal']
        if terminal is None:
            return unknown('pending')
        receipt, envelope = terminal['receipt'], terminal['envelope']
        if receipt['status'] != 'accepted':
            return unknown(receipt['status'])
        if (any(not _dependency_current(root, lane, ver, row['ticket']['business_day'],
                                       seals=state.get('risk_input_seals', {}).get('records', {}))
                for lane, ver in row['ticket']['dependencies'])
                or envelope['market_as_of'] is None or envelope['payload'].get('level') not in _LEVELS):
            return unknown('missing')
        return RiskSnapshot(receipt['committed_version'], sequence, 'success',
                            _time(envelope['market_as_of']), envelope['payload']['level'],
                            _time(envelope['recovery_until']) if envelope['recovery_until'] else None)
