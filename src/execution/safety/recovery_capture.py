"""제품 RAM의 두 번 읽기. 원자성·durable 상태·브로커 최종성은 보장하지 않는다."""
import asyncio
from datetime import datetime, timezone
from decimal import Decimal
import math
from types import MethodType
from zoneinfo import ZoneInfo

from ...core.engine import UnifiedEngine
from ...core.event import EventType
from .application import FillApplicationCoordinator, IngressContext
from .gateway import SignalGateway
from .protection_producer import ProtectionProducer, _Episode
from .runtime import KRExecutionRuntime
from .recovery_diagnostics import RecoverySnapshot, _classify


MAX_DEPTH = 32
MAX_NODES = 100_000
MAX_TEXT = 16_384
MAX_TOTAL_TEXT = 2_000_000
MAX_INTEGER_BITS = 256

RUNTIME_FIELDS = (
    '_day_closed', '_day_generation', '_day_fence_id', '_day_tasks', '_published_day',
    '_protection_tasks', '_command_scopes', '_command_tracking_started',
    '_command_result_tasks', '_command_results_failed', '_protection_failures',
    '_protection_failure_generation', '_protection_unattributed_failed',
    '_protection_recovery_counts', '_closing', '_reconciler_task', '_reconciler_tasks',
    '_reconciler_interval', '_reconciler_cycle_timeout', '_reconciler_started_at',
    '_reconciler_cycle_started_at', '_reconciler_complete_at', '_reconciler_cycle_completed_at',
    '_reconciler_progress_at', '_reconciler_last_reason', '_reconciler_target_count',
    '_reconciler_in_cycle', '_reconciler_skipped', '_reconciler_outcomes', '_reconciler_parked',
    '_reconciler_apply_seconds_last', '_reconciler_apply_seconds_max', '_quotes', '_quote_metadata',
)
ENGINE_FIELDS = ('_execution_version', '_execution_accepting', '_execution_ingress',
                 '_execution_ingress_tasks', '_execution_apply_tasks', '_execution_ticket')
PRODUCER_FIELDS = ('_tasks', '_episodes', '_last_quote', '_sources', '_restart_originals',
                   '_restart_retries', '_restart_checked', '_recovery_required',
                   '_pending_reasons', '_stats')
ADAPTED_FIELDS = {
    IngressContext: ('ticket', 'generation', 'received_at', 'fence_id', 'defer_reason', 'replay_fence_id'),
    _Episode: ('intent_id', 'decision', 'price', 'source', 'command_id', 'rejected_at', 'reason'),
}


class _Invalid(ValueError):
    def __init__(self):
        super().__init__('invalid_capture_evidence')


def _timestamp(value):
    # exact datetime만으로는 악성 tzinfo 호출을 차단할 수 없어 tz 타입도 한정한다.
    if type(value) is not datetime or not any(type(value.tzinfo) is item for item in (timezone, ZoneInfo)):
        raise _Invalid()
    return value.astimezone(timezone.utc).isoformat()


class _Copier:
    def __init__(self):
        self.nodes = 0
        self.text_size = 0
        self.active = set()
        self.references = []

    def copy(self, value, depth=0):
        self.nodes += 1
        if self.nodes > MAX_NODES or depth > MAX_DEPTH:
            raise _Invalid()
        kind = type(value)
        if value is None or kind is bool:
            return value
        if kind is int:
            if value.bit_length() > MAX_INTEGER_BITS:
                raise _Invalid()
            return value
        if kind is str:
            self.text_size += len(value)
            if len(value) > MAX_TEXT or self.text_size > MAX_TOTAL_TEXT:
                raise _Invalid()
            return value
        if kind is float:
            if not math.isfinite(value):
                raise _Invalid()
            return value
        if kind is Decimal:
            if not value.is_finite() or len(value.as_tuple().digits) > MAX_TEXT:
                raise _Invalid()
            return value
        if kind is datetime:
            return _timestamp(value)
        if kind is asyncio.Task or kind is asyncio.Future:
            self.references.append(value)
            return ('task', id(value), kind.done(value), kind.cancelled(value))
        if not any(kind is item for item in (dict, list, tuple, set, frozenset, IngressContext, _Episode)):
            raise _Invalid()
        if id(value) in self.active:
            raise _Invalid()
        self.active.add(id(value))
        try:
            if kind in ADAPTED_FIELDS:
                data = object.__getattribute__(value, '__dict__')
                return {key: self.copy(data[key], depth + 1) for key in ADAPTED_FIELDS[kind]}
            if kind is dict:
                result = {}
                for key, item in value.items():
                    if not any(type(key) is item for item in (str, int, tuple, asyncio.Task, asyncio.Future)):
                        raise _Invalid()
                    result[self.copy(key, depth + 1)] = self.copy(item, depth + 1)
                return result
            copied = [self.copy(item, depth + 1) for item in value]
            if kind is list:
                return copied
            if kind is tuple:
                return tuple(copied)
            return frozenset(copied)
        finally:
            self.active.remove(id(value))


def _data(value, expected):
    if type(value) is not expected:
        raise _Invalid()
    data = object.__getattribute__(value, '__dict__')
    if type(data) is not dict:
        raise _Invalid()
    # 키 비교/조회 이전에 임의 key의 hash/eq hook을 차단한다.
    if any(type(key) is not str for key in data):
        raise _Invalid()
    return data


def _read_sample(runtime):
    """테스트 seam. 참조는 두 표본 비교가 끝날 때까지 유지하며 절대 반환 DTO에 담지 않는다."""
    raw = _data(runtime, KRExecutionRuntime)
    owner, engine = raw['owner'], raw['engine']
    own, eng = _data(owner, FillApplicationCoordinator), _data(engine, UnifiedEngine)
    producer, gateway = raw['_protection_producer'], raw['gateway']
    prod = None if producer is None else _data(producer, ProtectionProducer)
    gate = None if gateway is None else _data(gateway, SignalGateway)
    copier = _Copier()
    handlers = eng['_handlers']
    if type(handlers) is not dict or any(type(key) is not EventType for key in handlers):
        raise _Invalid()
    market_handlers = handlers.get(EventType.MARKET_DATA)
    if type(market_handlers) is not list or len(market_handlers) > MAX_NODES:
        raise _Invalid()
    copier.references.extend(market_handlers)
    first_handler = market_handlers[0] if market_handlers else None
    producer_first = (type(first_handler) is MethodType and first_handler.__self__ is producer
                      and first_handler.__func__ is ProtectionProducer.on_market_data)
    locks = (own['_lock'], raw['_quote_lock']) + (() if prod is None else (prod['_lock'],))
    if any(type(lock) is not asyncio.Lock for lock in locks):
        raise _Invalid()
    lock_states = tuple(object.__getattribute__(lock, '__dict__')['_locked'] for lock in locks)
    if any(type(locked) is not bool for locked in lock_states):
        raise _Invalid()
    copier.references.extend(locks)
    writers = (raw['_intraday_writer'], raw['_regime_writer'])
    copier.references.extend(writers)
    sample = {
        'state': copier.copy(own['_state']),
        'owner': copier.copy({key: own[key] for key in (
            '_version', '_published_version', '_healthy', '_publication_recovery_required')}),
        'runtime': copier.copy({key: raw[key] for key in RUNTIME_FIELDS}),
        'engine': copier.copy({key: eng[key] for key in ENGINE_FIELDS}),
        'producer': None if prod is None else copier.copy({key: prod[key] for key in PRODUCER_FIELDS}),
        'bindings': (id(owner), id(engine), id(producer), id(gateway),
                     id(eng['_execution_runtime']),
                     None if prod is None else (id(prod['runtime']), id(prod['engine'])),
                     None if gate is None else id(gate['runtime'])),
        'locks': (tuple(id(lock) for lock in locks), lock_states),
        'writers': tuple(id(writer) for writer in writers),
        'handlers': tuple(id(handler) for handler in market_handlers),
        'candidate': (eng['_execution_runtime'] is runtime and prod is not None
                      and prod['runtime'] is runtime and prod['engine'] is engine
                      and gate is not None and gate['runtime'] is runtime and producer_first
                      and type(raw['_reconciler_task']) is asyncio.Task
                      and not asyncio.Task.done(raw['_reconciler_task'])),
    }
    copier.references.extend((owner, engine, producer, gateway, eng['_execution_runtime']))
    return sample, copier.references


def _validate_ram(sample):
    ram, eng, prod = sample['runtime'], sample['engine'], sample['producer']
    for key in ('_day_closed', '_command_tracking_started', '_command_results_failed',
                '_protection_unattributed_failed', '_closing', '_reconciler_in_cycle'):
        if type(ram[key]) is not bool:
            raise _Invalid()
    if type(eng['_execution_accepting']) is not bool:
        raise _Invalid()
    counters = [ram[key] for key in ('_day_generation', '_protection_failure_generation',
                '_reconciler_target_count', '_reconciler_parked')]
    counters.append(eng['_execution_ticket'])
    for key in ('_protection_recovery_counts', '_reconciler_skipped', '_reconciler_outcomes'):
        if type(ram[key]) is not dict:
            raise _Invalid()
        counters.extend(ram[key].values())
    if prod is not None:
        stats = prod['_stats']
        if type(stats) is not dict or type(stats.get('resume_dispositions')) is not dict:
            raise _Invalid()
        counters.extend(stats.get(key) for key in ('throttled', 'pending_released', 'reemissions',
                                                   'source_transitions'))
        counters.extend(stats['resume_dispositions'].values())
    if any(type(value) is not int or value < 0 for value in counters):
        raise _Invalid()


def capture_recovery_snapshot(runtime, *, captured_at) -> RecoverySnapshot:
    """동기 무재시도 캡처. 오류 객체·원문은 보존하거나 문자열로 변환하지 않는다."""
    stamp = None
    try:
        stamp = _timestamp(captured_at)
    except (ValueError, TypeError, OverflowError):
        return RecoverySnapshot(None, findings=(('snapshot_unavailable', 1), ('evidence_invalid', 1)))
    if type(runtime) is not KRExecutionRuntime:
        return RecoverySnapshot(stamp, findings=(('snapshot_unavailable', 1),))
    try:
        first, first_refs = _read_sample(runtime)
        second, second_refs = _read_sample(runtime)
        if first != second:
            return RecoverySnapshot(stamp, mode='partial_install', snapshot_stable=False,
                                    findings=(('snapshot_volatile', 1),))
        own, eng = second['owner'], second['engine']
        _validate_ram(second)
        versions = (own['_version'], own['_published_version'], eng['_execution_version'])
        if any(type(value) is not int or value < -1 for value in versions):
            raise _Invalid()
        if any(type(own[key]) is not bool for key in ('_healthy', '_publication_recovery_required')):
            raise _Invalid()
        consistent = (versions[0] >= 0 and versions[0] == versions[1] == versions[2]
                      and not own['_publication_recovery_required'])
        healthy = own['_healthy'] and not own['_publication_recovery_required'] and consistent
        counts = _classify(second)
        if not consistent:
            counts['publication_inconsistent'] = 1
        if not healthy:
            counts['owner_health_unconfirmed'] = 1
        return RecoverySnapshot(stamp,
            mode='attached_candidate' if second['candidate'] else 'partial_install',
            snapshot_stable=True, publication_consistent=consistent, owner_health_confirmed=healthy,
            execution_version=versions[0], published_version=versions[1], engine_version=versions[2],
            findings=tuple(sorted(counts.items())))
    except (ValueError, TypeError, KeyError, AttributeError, RuntimeError, OverflowError):
        return RecoverySnapshot(stamp, mode='partial_install',
                                findings=(('snapshot_unavailable', 1), ('evidence_invalid', 1)))
