"""Detached, immutable owner recovery facts and bounded producer read views.

This module deliberately has no owner, store, or runtime side effects.  Full
construction is a restore/migration operation; normal writers install later
incremental replacements rather than using this builder on their hot path.
"""
from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterator, Mapping, MutableMapping
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
import inspect
import heapq
from itertools import chain
import marshal
import math
import time
from types import MappingProxyType
from typing import Any, TypeAlias

from .lifecycle import TERMINAL_STATES
from .lifecycle import OrderState
from .market_source import validate_price_views
from decimal import Decimal, InvalidOperation
from .reservations import has_remaining_reservation
from .protection_recovery import digest
from .protection import (
    _STATE_FIELDS, _state_from_dict, _time as _protection_time, _validate as _validate_protection,
)


class _BuildMap(MutableMapping):
    """Bound dictionary resize/copy work during large cooperative builds.

    Fixed hash shards keep a single insertion from resizing the whole retained
    history. A frozen proxy can take ownership without merging the shards.
    Iteration order is internal only; public ordered buckets are built from the
    original checkpoint's order (or explicitly sorted).
    """
    __slots__ = ("_shards", "_size", "_pop_shard")

    def __init__(self):
        self._shards = [None] * 256
        self._size, self._pop_shard = 0, 0

    def __getitem__(self, key):
        shard = self._shards[hash(key) & 255]
        if shard is None:
            raise KeyError(key)
        return shard[key]

    def get(self, key, default=None):
        shard = self._shards[hash(key) & 255]
        return default if shard is None else shard.get(key, default)

    def setdefault(self, key, default=None):
        shard = self._shards[hash(key) & 255]
        if shard is None:
            shard = self._shards[hash(key) & 255] = {}
        before = len(shard)
        value = shard.setdefault(key, default)
        self._size += len(shard) - before
        return value

    def __setitem__(self, key, value):
        shard = self._shards[hash(key) & 255]
        if shard is None:
            shard = self._shards[hash(key) & 255] = {}
        if key not in shard:
            self._size += 1
        shard[key] = value

    def __delitem__(self, key):
        del self._shards[hash(key) & 255][key]
        self._size -= 1

    def __len__(self):
        return self._size

    def __iter__(self):
        for shard in self._shards:
            if shard:
                yield from shard

    def items(self):
        for shard in self._shards:
            if shard:
                yield from shard.items()

    def values(self):
        for shard in self._shards:
            if shard:
                yield from shard.values()

    def popitem(self):
        if not self._size:
            raise KeyError("empty_build_map")
        while not self._shards[self._pop_shard]:
            self._pop_shard = (self._pop_shard + 1) & 255
        self._size -= 1
        return self._shards[self._pop_shard].popitem()


class FrozenMap(Mapping[str, "FrozenJSON"]):
    """A recursively detached mapping whose backing dictionary is private."""

    __slots__ = ("_values",)

    def __init__(self, values: Mapping[str, "FrozenJSON"]):
        if any(type(key) is not str for key in values):
            raise ValueError("invalid_checkpoint_key")
        self._values = MappingProxyType({key: _freeze_json(item) for key, item in values.items()})

    @classmethod
    def _trusted(cls, values: Mapping[str, object]) -> "FrozenMap":
        """Use only for immutable fact DTO buckets built inside this module."""
        result = object.__new__(cls)
        object.__setattr__(result, "_values", MappingProxyType(dict(values)))
        return result

    @classmethod
    def _take_owned(cls, values: dict[str, object]) -> "FrozenMap":
        """Seal a private dictionary after its builder relinquishes ownership."""
        result = object.__new__(cls)
        object.__setattr__(result, "_values", MappingProxyType(values))
        return result

    def __setattr__(self, name: str, value: object) -> None:
        if name == "_values" and hasattr(self, "_values"):
            raise AttributeError("FrozenMap is immutable")
        object.__setattr__(self, name, value)

    def __getitem__(self, key: str) -> "FrozenJSON":
        return self._values[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)

    def __repr__(self) -> str:
        return f"FrozenMap({dict(self._values)!r})"

    def __reduce__(self):
        return (FrozenMap._trusted, (dict(self._values),))


class FrozenKeyMap(Mapping[object, int]):
    """Immutable aggregate map for non-JSON composite join keys."""
    __slots__ = ("_values",)

    def __init__(self, values: Mapping[object, int]):
        object.__setattr__(self, "_values", MappingProxyType(dict(values)))

    @classmethod
    def _take_owned(cls, values):
        result = object.__new__(cls)
        object.__setattr__(result, "_values", MappingProxyType(values))
        return result

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("FrozenKeyMap is immutable")

    def __getitem__(self, key: object) -> int:
        return self._values[key]

    def __iter__(self) -> Iterator[object]:
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)

    def __reduce__(self):
        return (FrozenKeyMap, (dict(self._values),))


FrozenJSON: TypeAlias = str | int | float | bool | None | tuple["FrozenJSON", ...] | FrozenMap
_EMPTY_MAP = FrozenMap._take_owned({})
_ORDER_STATES = frozenset(value.value for value in OrderState)
_SLICE_BOUNDARY = object()


class RecoveryProjectionMode(Enum):
    SHADOW_MIGRATION = "shadow_migration"
    ACTIVATED = "activated"


@dataclass(frozen=True, slots=True)
class OwnerToken:
    incarnation: str
    publication_epoch: int
    fault_epoch: int
    revision: int

    def __post_init__(self) -> None:
        values = (self.publication_epoch, self.fault_epoch, self.revision)
        if (type(self.incarnation) is not str or not self.incarnation
                or any(type(value) is not int or value < 0 for value in values)):
            raise ValueError("invalid_owner_token")


@dataclass(frozen=True, slots=True)
class AttemptFact:
    attempt_id: str
    intent_id: str | None
    symbol: str | None
    kind: str | None
    side: str | None
    state: str | None
    command_status: str | None
    data: FrozenMap


@dataclass(frozen=True, slots=True)
class IntentFact:
    intent_id: str
    symbol: str | None
    side: str | None
    attempt_ids: tuple[str, ...]
    data: FrozenMap


@dataclass(frozen=True, slots=True)
class AuditFact:
    command_id: str
    intent_id: str | None
    symbol: str | None
    decision: tuple[FrozenJSON, ...] | None
    data: FrozenMap


@dataclass(frozen=True, slots=True)
class AdmissionFact:
    command_id: str
    intent_id: str | None
    symbol: str | None
    data: FrozenMap


@dataclass(frozen=True, slots=True)
class ProtectionFact:
    symbol: str
    data: FrozenMap


@dataclass(frozen=True, slots=True)
class ExplicitQuoteFact:
    symbol: str
    data: FrozenMap


@dataclass(frozen=True, slots=True)
class InvalidRecoveryFact:
    category: str
    identity: str | None
    reason: str


@dataclass(frozen=True, slots=True)
class OwnerRecoveryProjection:
    schema_version: int
    token: OwnerToken
    complete: bool
    unavailable_reason: str | None
    findings: tuple[tuple[str, int | None], ...]


@dataclass(frozen=True, slots=True)
class OwnerRecoveryJoinView:
    token: OwnerToken
    complete: bool
    audits_by_intent: FrozenMap
    audits_by_key: FrozenKeyMap
    unsubmitted_by_intent: FrozenMap
    pending_fallback_by_pair: FrozenKeyMap
    pending_by_intent: FrozenMap


@dataclass(frozen=True, slots=True)
class _IndexData:
    attempts_by_symbol: FrozenMap
    attempts_by_intent: FrozenMap
    intents: FrozenMap
    intents_by_symbol: FrozenMap
    audits_by_intent: FrozenMap
    audits_by_symbol: FrozenMap
    admissions_by_intent: FrozenMap
    admissions_by_symbol: FrozenMap
    admissions_by_command: FrozenMap
    pending_admissions: tuple[AdmissionFact, ...]
    pending_symbols_by_intent: FrozenMap
    pending_owner_by_symbol: FrozenMap
    protection_by_symbol: FrozenMap
    protection_inputs_by_symbol: FrozenMap
    quotes_by_symbol: FrozenMap
    pending_recovery: tuple[str, ...]
    active_open_buys: tuple[str, ...]
    active_recovery: tuple[str, ...]
    invalid: tuple[InvalidRecoveryFact, ...]
    has_degraded: bool


@dataclass(frozen=True, slots=True)
class ProducerReadView:
    """Read-only query facade.  Its fields are private frozen index data.

    The class intentionally remains monkeypatchable: tests can prove a scoped
    method does not fall back to a historical all-row helper.
    """

    token: OwnerToken
    complete: bool
    _data: _IndexData

    def attempts_for_symbol(self, symbol: str) -> tuple[AttemptFact, ...]:
        return _tuple_at(self._data.attempts_by_symbol, symbol)

    def attempts_for_intent(self, intent_id: str) -> tuple[AttemptFact, ...]:
        return _tuple_at(self._data.attempts_by_intent, intent_id)

    def all_attempts(self) -> tuple[AttemptFact, ...]:
        """Diagnostic-only helper; scoped methods never call it."""
        return tuple(fact for facts in self._data.attempts_by_intent.values() for fact in facts)

    def intent(self, intent_id: str) -> IntentFact | None:
        value = self._data.intents.get(intent_id)
        return value if isinstance(value, IntentFact) else None

    def intents_for_symbol(self, symbol: str) -> tuple[IntentFact, ...]:
        return _tuple_at(self._data.intents_by_symbol, symbol)

    def audits_for_intent(self, intent_id: str) -> tuple[AuditFact, ...]:
        return _tuple_at(self._data.audits_by_intent, intent_id)

    def audits_for_symbol(self, symbol: str) -> tuple[AuditFact, ...]:
        return _tuple_at(self._data.audits_by_symbol, symbol)

    def admissions_for_intent(self, intent_id: str) -> tuple[AdmissionFact, ...]:
        return _tuple_at(self._data.admissions_by_intent, intent_id)

    def admissions_for_symbol(self, symbol: str) -> tuple[AdmissionFact, ...]:
        return _tuple_at(self._data.admissions_by_symbol, symbol)

    def pending_admissions(self) -> tuple[AdmissionFact, ...]:
        return self._data.pending_admissions

    def admission(self, command_id: str) -> AdmissionFact | None:
        value = self._data.admissions_by_command.get(command_id)
        return value if isinstance(value, AdmissionFact) else None

    def next_quote_admission(self) -> AdmissionFact | None:
        return self._data.pending_admissions[0] if self._data.pending_admissions else None

    def pending_symbols_for_intent(self, intent_id: str) -> tuple[str, ...]:
        return _tuple_at(self._data.pending_symbols_by_intent, intent_id)

    def pending_owner_for_symbol(self, symbol: str) -> str | None:
        value = self._data.pending_owner_by_symbol.get(symbol)
        return value if type(value) is str else None

    def protection_for_symbol(self, symbol: str) -> ProtectionFact | None:
        value = self._data.protection_by_symbol.get(symbol)
        return value if isinstance(value, ProtectionFact) else None

    def protection_quote_input(self, symbol: str) -> FrozenJSON:
        return self._data.protection_inputs_by_symbol.get(symbol, FrozenMap({}))

    def latest_quote_for_symbol(self, symbol: str) -> ExplicitQuoteFact | None:
        value = self._data.quotes_by_symbol.get(symbol)
        return value if isinstance(value, ExplicitQuoteFact) else None

    def pending_recovery_symbols(self) -> tuple[str, ...]:
        return self._data.pending_recovery

    def active_open_buy_symbols(self) -> tuple[str, ...]:
        return self._data.active_open_buys

    def active_recovery_symbols(self) -> tuple[str, ...]:
        return self._data.active_recovery

    def invalid_facts(self) -> tuple[InvalidRecoveryFact, ...]:
        return self._data.invalid

    @property
    def has_degraded_protection(self) -> bool:
        return self._data.has_degraded


@dataclass(frozen=True, slots=True)
class ProducerRecoveryIndex(ProducerReadView):
    """Owner-owned implementation of the producer query view."""


@dataclass(frozen=True, slots=True)
class FullBuildResult:
    projection: OwnerRecoveryProjection
    join_view: OwnerRecoveryJoinView
    index: ProducerRecoveryIndex


# Each query/builder source is listed explicitly.  Schema/market are frozen
# format inputs, not product-writable dependencies.
PRODUCER_INDEX_DEPENDENCIES = frozenset({
    "attempts", "intents", "outbox", "inbox", "portfolio.positions",
    "protection_quote_admissions", "latest_explicit_quote", "quote_price_views",
    "day_valuation_view", "day_valuations", "protection.config", "protection.states",
    "protection.entry_times", "protection.exit_exempt", "protection.max_holding_days",
    "protection.current_regime", "protection.intraday_crash_level",
    "protection.integrity_reset_symbols", "protection.degraded", "protection.orders",
    "protection.pending_owners",
})
PRODUCER_INDEX_EXCLUSIONS = frozenset({"protection.schema", "protection.market", "market_sources", "entry_quotes"})
PRODUCER_INDEX_SOURCE_USES = FrozenMap._trusted({
    "protection_for_symbol": ("protection.states",),
    "protection_quote_input": ("protection.config", "protection.states", "protection.entry_times",
                                "protection.exit_exempt", "protection.max_holding_days",
                                "protection.current_regime", "protection.intraday_crash_level",
                                "protection.integrity_reset_symbols", "protection.degraded",
                                "protection.orders", "protection.pending_owners"),
    "latest_quote_for_symbol": ("latest_explicit_quote", "quote_price_views", "day_valuation_view", "day_valuations"),
    "pending_recovery_symbols": ("protection.pending_owners",),
    "pending_admissions": ("protection_quote_admissions",),
    "admission": ("protection_quote_admissions",),
    "audits_for_symbol": ("outbox",),
    "active_open_buy_symbols": ("attempts",),
    "has_degraded_protection": ("protection.degraded",),
})


def freeze_checkpoint_facts(value: Mapping[str, object] | object) -> FrozenJSON:
    """Detach a JSON checkpoint while preserving finite float identity/type."""
    return _freeze_json(value)


def _freeze_json(value: object) -> FrozenJSON:
    if value is None or type(value) in (str, int, bool):
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError("non_finite_checkpoint_float")
        return value
    if type(value) in (list, tuple):
        return tuple(_freeze_json(item) for item in value)
    if isinstance(value, Mapping):
        frozen: dict[str, FrozenJSON] = {}
        for key, item in value.items():
            if type(key) is not str:
                raise ValueError("invalid_checkpoint_key")
            frozen[key] = _freeze_json(item)
        return FrozenMap._trusted(frozen)
    raise ValueError("invalid_checkpoint_value")


def thaw_checkpoint_facts(value: FrozenJSON) -> Any:
    """Return a fresh JSON-shaped tree; tuples represent frozen JSON lists."""
    if isinstance(value, FrozenMap):
        return {key: thaw_checkpoint_facts(item) for key, item in value.items()}
    if type(value) is tuple:
        return [thaw_checkpoint_facts(item) for item in value]
    return value



def _integer(value):
    if type(value) is not int or value < 0:
        raise ValueError("invalid_evidence")
    return value


def _mapping(value):
    if type(value) is not dict:
        raise ValueError("invalid_evidence")
    return value


def _identity(value):
    if type(value) is not str or not value or value.strip() != value:
        raise ValueError("invalid_evidence")
    return value


def _drain(steps):
    while True:
        try:
            next(steps)
        except StopIteration as done:
            return done.value


def _projection_steps(state, token):
    try:
        counts = yield from _classify_steps(state)
    except (AttributeError, KeyError, TypeError, ValueError, OverflowError, InvalidOperation):
        counts = {"evidence_invalid": 1}
    complete = not counts.get("evidence_invalid")
    return OwnerRecoveryProjection(1, token, complete, None if complete else "evidence_invalid",
                                   tuple(sorted((code, count) for code, count in counts.items() if count)))


def classify_owner_facts(frozen: FrozenJSON, *, token: OwnerToken) -> OwnerRecoveryProjection:
    _require_token(token)
    return _drain(_projection_steps(thaw_checkpoint_facts(frozen), token))


def _classify_steps(state):
    """Incremental owner-only N3 semantics; RAM joins belong to composition."""
    counts = {}

    def add(code, count=1):
        if count:
            counts[code] = counts.get(code, 0) + count

    ram = {'_protection_unattributed_failed': False, '_protection_failures': {}}
    producer = None
    attempts = _mapping(state.get('attempts'))
    intents = _mapping(state.get('intents'))
    protection = _mapping(state.get('protection'))
    positions = _mapping(_mapping(state.get('portfolio')).get('positions'))
    protected = _mapping(protection.get('states'))
    pending = _mapping(protection.get('pending_owners'))
    episodes = {} if producer is None else _mapping(producer['_episodes'])
    admissions = _mapping(state.get('protection_quote_admissions', {}))
    members, admissions_by_intent, pending_by_intent = _BuildMap(), _BuildMap(), _BuildMap()
    malformed_admissions = False
    for identity, row in intents.items():
        yield
        if type(row) is dict and type(row.get('attempt_ids')) is list:
            members[identity] = set()
            for key in row['attempt_ids']:
                yield
                if type(key) is str:
                    members[identity].add(key)
    for rank, (command, row) in enumerate(admissions.items()):
        yield
        if type(row) is not dict:
            malformed_admissions = True
        elif row.get('intent_id') is None or type(row.get('intent_id')) is str:
            admissions_by_intent.setdefault(row.get('intent_id'), []).append((rank, command, row))
        else:
            # N3 compares even malformed scalar identities. Keep its full scan
            # on unsupported index keys instead of silently losing a match.
            malformed_admissions = True
    for symbol, identity in pending.items():
        yield
        if type(identity) is str:
            pending_by_intent.setdefault(identity, []).append((symbol, identity))
    for row in _mapping(state.get('inbox', {})).values():
        yield
        if _mapping(row).get('status') not in ('APPLIED', 'SUPERSEDED'):
            add('unapplied_inbox')

    for key, row in attempts.items():
        yield
        try:
            _identity(key)
            _mapping(row)
            kind, side = row.get('kind'), row.get('side')
            if kind not in ('submit', 'cancel', 'modify') or side not in ('buy', 'sell'):
                raise ValueError('invalid_evidence')
            if (row.get('state') not in _ORDER_STATES
                    or row.get('command_status') not in (None, 'not_sent', 'acknowledged', 'rejected', 'unknown')):
                raise ValueError('invalid_evidence')
            observed = _integer(row.get('observed_quantity'))
            applied = _integer(row.get('applied_quantity'))
            if observed != applied:
                add('observation_not_applied')
            if applied > observed:
                add('evidence_invalid')
            intent = intents.get(row.get('intent_id'))
            link_ok = (type(intent) is dict and row.get('attempt_id') == key
                       and intent.get('symbol') == row.get('symbol')
                       and intent.get('side') == side
                       and type(intent.get('attempt_ids')) is list
                       and key in members.get(row.get('intent_id'), ()))
            if not link_ok:
                add('attempt_link_inconsistent')
            remaining = has_remaining_reservation(row)
            if 'reserved_planned_risk' in row and row['reserved_planned_risk'] is None:
                add('evidence_invalid')
            terminal = row.get('state') in TERMINAL_STATES
            if remaining:
                add('remaining_reservation')
                if terminal:
                    add('terminal_reservation')
            if kind == 'submit' and side == 'buy' and (
                    row.get('command_status') == 'unknown' or row.get('state') == 'blocked_unknown'):
                add('unknown_buy')
            if (kind == 'submit' and side == 'sell' and link_ok and not terminal
                    and row.get('state') != 'blocked_unknown'
                    and row.get('command_status') == 'acknowledged'
                    and not row.get('evidence_conflict') and observed == applied):
                add('pending_sell')
            if kind in ('cancel', 'modify'):
                parent = attempts.get(row.get('parent_attempt_id'))
                parent_ok = (type(parent) is dict and parent.get('kind') == 'submit'
                    and parent is not row and parent.get('order_ref') is not None
                    and row.get('order_ref') == parent.get('order_ref')
                    and all(row.get(name) == parent.get(name) for name in ('intent_id', 'symbol', 'side')))
                command_ref = row.get('command_ref')
                command_ref_ok = False
                if parent_ok and type(command_ref) is dict and type(parent.get('order_ref')) is dict:
                    original_ref = parent['order_ref']
                    scope = ('account_scope', 'market', 'order_date', 'exchange')
                    command_ref_ok = (all(type(command_ref.get(name)) is str and command_ref[name]
                                         and command_ref[name] == original_ref.get(name) for name in scope)
                                      and type(original_ref.get('order_no')) is str and original_ref['order_no']
                                      and type(command_ref.get('order_no')) is str and command_ref['order_no']
                                      and command_ref.get('parent_order_no') == original_ref['order_no'])
                if command_ref is not None and not command_ref_ok:
                    parent_ok = False
                if not parent_ok:
                    add('attempt_link_inconsistent')
                if kind == 'cancel' and row.get('command_status') not in ('not_sent', 'rejected'):
                    if (not parent_ok or not command_ref_ok or parent.get('state') not in TERMINAL_STATES
                            or parent.get('observed_quantity') != parent.get('applied_quantity')):
                        add('cancel_unconfirmed')
        except (ValueError, TypeError, KeyError, InvalidOperation):
            add('evidence_invalid')

    for identity, intent in intents.items():
        yield
        try:
            _identity(identity)
            _mapping(intent)
            _identity(intent.get('symbol'))
            _integer(intent.get('target_quantity'))
            ids = intent.get('attempt_ids')
            if type(ids) is not list or intent.get('side') not in ('buy', 'sell'):
                raise ValueError('invalid_evidence')
            for key in ids:
                yield
                if type(key) is not str or key not in attempts or attempts[key].get('intent_id') != identity:
                    add('attempt_link_inconsistent')
                    break
        except (ValueError, TypeError, KeyError, AttributeError):
            add('evidence_invalid')

    for command, row in _mapping(state.get('outbox')).items():
        yield
        try:
            _mapping(row)
            if row.get('kind') != 'protection_decision':
                continue
            source = row.get('effect_source')
            if source is not None:
                if type(source) is not str or source != 'intraday_preemptive':
                    add('evidence_invalid')
                continue
            symbol, identity = _identity(row.get('symbol')), _identity(row.get('intent_id'))
            decision = row.get('decision')
            if (type(decision) is not list or len(decision) != 3
                    or decision[0] not in ('sell_all', 'sell_partial')
                    or _integer(decision[1]) == 0 or type(decision[2]) is not str):
                raise ValueError('invalid_evidence')
            intent = intents.get(identity)
            linked = False
            if intent is not None:
                linked = True
                if intent.get('symbol') != symbol or intent.get('side') != 'sell':
                    add('protection_link_inconsistent')
                if intent.get('target_quantity') != decision[1]:
                    add('protection_quantity_inconsistent')
            for other_symbol, episode in episodes.items():
                yield
                if episode.get('intent_id') == identity:
                    linked = True
                    if other_symbol != symbol or tuple(decision) != episode.get('decision'):
                        add('protection_link_inconsistent')
            relevant_admissions = admissions.items()
            if not malformed_admissions:
                relevant_admissions = ((key, value) for _, key, value in admissions_by_intent.get(identity, ()))
                direct = admissions.get(command)
                if direct is not None and direct.get('intent_id') != identity:
                    relevant_admissions = chain(relevant_admissions, ((command, direct),))
            for admission_key, admission in relevant_admissions:
                yield
                if admission_key == command or admission.get('intent_id') == identity:
                    linked = True
                    if admission.get('symbol') != symbol or admission.get('intent_id') != identity:
                        add('protection_link_inconsistent')
            for other_symbol, pending_identity in pending_by_intent.get(identity, ()):
                yield
                if pending_identity == identity:
                    if other_symbol != symbol:
                        add('protection_link_inconsistent')
                    current = protected.get(other_symbol)
                    if type(current) is not dict or current.get('pending_target_qty') != decision[1]:
                        add('protection_quantity_inconsistent')
            if not linked:
                add('protection_unsubmitted')
        except (ValueError, TypeError, KeyError, AttributeError):
            add('evidence_invalid')

    for symbol, row in protected.items():
        yield
        try:
            qty = _integer(_mapping(row).get('remaining_quantity'))
            position = positions.get(symbol)
            if position is None or qty != _integer(_mapping(position).get('quantity')):
                add('protection_quantity_inconsistent')
        except (ValueError, TypeError, KeyError):
            add('evidence_invalid')
    for symbol in positions:
        yield
        if symbol not in protected and symbol not in _mapping(protection.get('degraded')):
            add('protection_quantity_inconsistent')
    for symbol in pending:
        yield
        intent = intents.get(pending[symbol])
        episode = episodes.get(symbol)
        if intent is not None and (intent.get('symbol') != symbol or intent.get('side') != 'sell'):
            add('protection_link_inconsistent')
        admission_match = False
        if intent is None:
            relevant = admissions.values() if malformed_admissions else (
                row for _, _, row in admissions_by_intent.get(pending[symbol], ()))
            for row in relevant:
                yield
                if row.get('intent_id') == pending[symbol] and row.get('symbol') == symbol:
                    admission_match = True
                    break
        if (intent is None and (episode is None or episode.get('intent_id') != pending[symbol])
                and not admission_match):
            add('protection_link_inconsistent')
    if ram['_protection_unattributed_failed']:
        add('protection_failure_unattributed')
    repair_count = len(_mapping(protection.get('degraded')))
    repair_count += len(ram['_protection_failures'])
    if producer is not None:
        repair_count += len(producer['_recovery_required'])
        if producer['_stats'].get('invariant_violation') is not None:
            repair_count += 1
    add('repair_only', repair_count)
    for temporary in (members, admissions_by_intent, pending_by_intent):
        yield from _clear_steps(temporary)
    return counts


def _join_steps(frozen: FrozenJSON, *, token: OwnerToken) -> OwnerRecoveryJoinView:
    _require_token(token)
    state = _fmap(frozen)
    audits, audit_keys, unsubmitted, pending = _BuildMap(), _BuildMap(), _BuildMap(), _BuildMap()
    complete = True
    intents = _fmap(state.get("intents"))
    admissions = _fmap(state.get("protection_quote_admissions"))
    admission_intents, admission_pairs = set(), set()
    for row in admissions.values():
        yield
        if isinstance(row, FrozenMap):
            identity, symbol = _text(row.get("intent_id")), _text(row.get("symbol"))
            if identity is not None:
                admission_intents.add(identity)
                admission_pairs.add((symbol, identity))
    for command, raw in _fmap(state.get("outbox")).items():
        yield
        if not isinstance(raw, FrozenMap):
            complete = False
            continue
        if raw.get("kind") != "protection_decision":
            continue
        source = raw.get("effect_source")
        if source == "intraday_preemptive":
            continue
        intent, symbol, decision = _text(raw.get("intent_id")), _text(raw.get("symbol")), raw.get("decision")
        if source is not None or intent is None or symbol is None or not _valid_decision(decision):
            complete = False
            continue
        key = (intent, symbol, decision)
        audits[intent] = audits.get(intent, 0) + 1
        audit_keys[key] = audit_keys.get(key, 0) + 1
        linked = intent in intents or command in admissions or intent in admission_intents
        if not linked:
            unsubmitted[intent] = unsubmitted.get(intent, 0) + 1
    for symbol, intent in _fmap(_fmap(state.get("protection")).get("pending_owners")).items():
        yield
        if _text(symbol) is not None and _text(intent) is not None:
            pending.setdefault(intent, []).append(symbol)
        else:
            complete = False
    pairs, pending_frozen = _BuildMap(), _BuildMap()
    for intent, symbols in pending.items():
        yield
        for symbol in symbols:
            yield
            if intent not in intents and (symbol, intent) not in admission_pairs:
                pairs[symbol, intent] = 1
        pending_frozen[intent] = tuple(sorted(symbols))
    for temporary in (admission_intents, admission_pairs, pending):
        yield from _clear_steps(temporary)
    return OwnerRecoveryJoinView(token, complete, FrozenMap._take_owned(audits), FrozenKeyMap._take_owned(audit_keys),
                                 FrozenMap._take_owned(unsubmitted), FrozenKeyMap._take_owned(pairs),
                                 FrozenMap._take_owned(pending_frozen))


def _index_steps(frozen: FrozenJSON, *, token: OwnerToken) -> ProducerRecoveryIndex:
    _require_token(token)
    state = _fmap(frozen)
    invalid: list[InvalidRecoveryFact] = []
    def bad(category: str, identity: object, reason: str = "invalid_or_missing_link") -> None:
        invalid.append(InvalidRecoveryFact(category, _text(identity), reason))

    for name in ("attempts", "intents", "outbox", "inbox", "portfolio", "protection",
                 "protection_quote_admissions", "latest_explicit_quote", "quote_price_views",
                 "day_valuation_view", "day_valuations"):
        yield
        if ((name in state or name in ("attempts", "intents", "outbox", "portfolio", "protection"))
                and not isinstance(state.get(name), FrozenMap)):
            bad(name, None, "malformed_container")
    for identity, row in _fmap(state.get("inbox")).items():
        yield
        if not isinstance(row, FrozenMap) or row.get("status") not in ("APPLIED", "SUPERSEDED", "RECEIVED"):
            bad("inbox", identity)

    attempts_by_symbol, attempts_by_intent = _BuildMap(), _BuildMap()
    active_buys: set[str] = set()
    raw_attempts, raw_intents = _fmap(state.get("attempts")), _fmap(state.get("intents"))
    memberships = _BuildMap()
    for identity, row in raw_intents.items():
        yield
        ids = _fmap(row).get("attempt_ids")
        if type(ids) is tuple:
            counts = {}
            for aid in ids:
                yield
                if type(aid) is str:
                    counts[aid] = counts.get(aid, 0) + 1
            memberships[identity] = counts
    for identity, row in raw_attempts.items():
        yield
        if not isinstance(row, FrozenMap):
            bad("attempt", identity, "malformed")
            continue
        data = _fmap(row)
        fact = AttemptFact(identity, _text(data.get("intent_id")), _text(data.get("symbol")),
                           _text(data.get("kind")), _text(data.get("side")), _text(data.get("state")),
                           _text(data.get("command_status")), data)
        intent_row = _fmap(raw_intents.get(fact.intent_id))
        member_ids = intent_row.get("attempt_ids")
        linked = (fact.intent_id is not None and fact.symbol is not None and fact.intent_id in raw_intents
                  and type(member_ids) is tuple and memberships.get(fact.intent_id, {}).get(identity) == 1
                  and data.get("attempt_id") == identity and intent_row.get("symbol") == fact.symbol
                  and intent_row.get("side") == fact.side)
        if not _valid_attempt(identity, data) or not _valid_attempt_schema(data) or not linked:
            bad("attempt", identity)
        if fact.symbol is not None:
            attempts_by_symbol.setdefault(fact.symbol, []).append(fact)
        if fact.intent_id is not None:
            attempts_by_intent.setdefault(fact.intent_id, []).append(fact)
        if (fact.kind == "submit" and fact.side == "buy" and fact.symbol
                and (fact.state not in TERMINAL_STATES or _remaining(data))):
            active_buys.add(fact.symbol)

    intents, intents_by_symbol = _BuildMap(), _BuildMap()
    for identity, row in raw_intents.items():
        yield
        if not isinstance(row, FrozenMap):
            bad("intent", identity, "malformed")
            continue
        data = _fmap(row)
        ids = data.get("attempt_ids")
        selected_ids = []
        if type(ids) is tuple:
            for item in ids:
                yield
                if type(item) is str:
                    selected_ids.append(item)
        attempt_ids = tuple(selected_ids)
        fact = IntentFact(identity, _text(data.get("symbol")), _text(data.get("side")), attempt_ids, data)
        intents[identity] = fact
        if (fact.symbol is None or fact.side not in ("buy", "sell") or type(ids) is not tuple
                or type(data.get("target_quantity")) is not int or data["target_quantity"] < 0
                or len(attempt_ids) != len(ids)
                or len(memberships.get(identity, {})) != len(attempt_ids)):
            bad("intent", identity)
        else:
            intents_by_symbol.setdefault(fact.symbol, []).append(fact)
        for attempt_id in attempt_ids:
            yield
            matching = raw_attempts.get(attempt_id)
            if not isinstance(matching, FrozenMap) or matching.get("intent_id") != identity:
                bad("intent", identity)
    yield from _clear_steps(memberships)

    audits_by_intent, audits_by_symbol = _BuildMap(), _BuildMap()
    for command, row in _fmap(state.get("outbox")).items():
        yield
        if not isinstance(row, FrozenMap):
            bad("outbox", command, "malformed")
            continue
        data = _fmap(row)
        if data.get("kind") != "protection_decision":
            continue
        if data.get("effect_source") == "intraday_preemptive":
            continue
        decision = data.get("decision")
        fact = AuditFact(command, _text(data.get("intent_id")), _text(data.get("symbol")),
                         decision if type(decision) is tuple else None, data)
        intent = _fmap(raw_intents.get(fact.intent_id))
        if (data.get("effect_source") is not None or fact.intent_id is None or fact.symbol is None
                or not _valid_decision(fact.decision) or not isinstance(intent, FrozenMap)
                or intent.get("symbol") != fact.symbol or intent.get("side") != "sell"
                or intent.get("target_quantity") != fact.decision[1]):
            bad("audit", command)
        if fact.intent_id is not None:
            audits_by_intent.setdefault(fact.intent_id, []).append(fact)
        if fact.symbol is not None:
            audits_by_symbol.setdefault(fact.symbol, []).append(fact)

    admissions_by_intent, admissions_by_symbol, admissions_by_command = _BuildMap(), _BuildMap(), _BuildMap()
    admission_heap = []
    for command, row in _fmap(state.get("protection_quote_admissions")).items():
        yield
        if not isinstance(row, FrozenMap):
            bad("admission", command, "malformed")
            continue
        data = _fmap(row)
        fact = AdmissionFact(command, _text(data.get("intent_id")), _text(data.get("symbol")), data)
        admissions_by_command[command] = fact
        heapq.heappush(admission_heap, (_source_version(data), command, fact))
        intent = _fmap(raw_intents.get(fact.intent_id))
        if (fact.symbol is None or (data.get("intent_id") is not None and fact.intent_id is None)
                or (intent and intent.get("symbol") != fact.symbol)
                or not _valid_admission(data, command, token.revision)):
            bad("admission", command)
        if fact.intent_id is not None:
            admissions_by_intent.setdefault(fact.intent_id, []).append(fact)
        if fact.symbol is not None:
            admissions_by_symbol.setdefault(fact.symbol, []).append(fact)

    protection = _fmap(state.get("protection"))
    # Delegate the complete scalar/config/root contract to the canonical
    # decoder's validator. Collections are examined separately, one row at a
    # time; validating the entire protection history here would not yield.
    header = {}
    for name, value in protection.items():
        yield
        if name in ("states", "entry_times", "degraded", "orders", "pending_owners"):
            header[name] = {}
        elif name in ("exit_exempt", "integrity_reset_symbols"):
            header[name] = []
        else:
            header[name] = thaw_checkpoint_facts(value)
    try:
        _validate_protection(header)
    except (ValueError, TypeError, KeyError, InvalidOperation, OverflowError):
        bad("protection", None, "invalid_canonical_root")
    for name in ("config", "states", "entry_times", "degraded", "orders", "pending_owners"):
        yield
        if not isinstance(protection.get(name), FrozenMap):
            bad("protection", name, "malformed_container")
    protection_sets = {}
    for name in ("exit_exempt", "integrity_reset_symbols"):
        seen = protection_sets[name] = set()
        values = protection.get(name)
        if type(values) is not tuple:
            bad("protection", name, "malformed_set")
            continue
        for value in values:
            yield
            if _text(value) is None or value in seen:
                bad("protection", name, "malformed_set")
            else:
                seen.add(value)
    protection_by_symbol = _BuildMap()
    for symbol, row in _fmap(protection.get("states")).items():
        yield
        if _text(symbol) is None or not isinstance(row, FrozenMap):
            bad("protection", symbol, "malformed")
        else:
            protection_by_symbol[symbol] = ProtectionFact(symbol, row)
            if not (yield from _valid_protection_steps(symbol, row)):
                bad("protection", symbol, "malformed")
            if symbol not in _fmap(protection.get("entry_times")) and symbol not in _fmap(protection.get("degraded")):
                bad("protection", symbol, "missing_entry_time")
    for symbol, value in _fmap(protection.get("entry_times")).items():
        yield
        try:
            _identity(symbol)
            _protection_time(value)
        except (ValueError, TypeError, OverflowError):
            bad("protection", symbol, "invalid_entry_time")
    pending_owners = _fmap(protection.get("pending_owners"))
    pending_by_intent, pending_owner_values = _BuildMap(), _BuildMap()
    for symbol, intent in pending_owners.items():
        yield
        if _text(symbol) is None or _text(intent) is None:
            bad("pending_owner", symbol)
            continue
        pending_by_intent.setdefault(intent, []).append(symbol)
        pending_owner_values[symbol] = intent
    positions = _fmap(_fmap(state.get("portfolio")).get("positions"))
    if not isinstance(_fmap(state.get("portfolio")).get("positions"), FrozenMap):
        bad("position", None, "malformed_container")
    for symbol, row in positions.items():
        yield
        if _text(symbol) is None or not isinstance(row, FrozenMap):
            bad("position", symbol, "malformed")
    orders = _fmap(protection.get("orders"))
    orders_by_symbol = _BuildMap()
    for identity, row in orders.items():
        yield
        if not isinstance(row, FrozenMap) or _text(row.get("symbol")) is None:
            bad("order", identity, "malformed")
        else:
            orders_by_symbol.setdefault(row["symbol"], []).append(row)
            if (set(row) != {"symbol", "side", "intent_id", "kind", "base_quantity", "cumulative_quantity", "reset_applied"}
                    or _text(row.get("intent_id")) is None or row.get("side") not in ("BUY", "SELL")
                    or row.get("kind") not in ("initial_entry", "same_entry_fill", "distinct_add_on", "exit")
                    or (row.get("side") == "SELL") != (row.get("kind") == "exit")
                    or any(type(row.get(name)) is not int or row[name] < 0 for name in ("base_quantity", "cumulative_quantity"))
                    or type(row.get("reset_applied")) is not bool):
                bad("order", identity)
    for identity, row in _fmap(protection.get("degraded")).items():
        yield
        if (_text(identity) is None or not isinstance(row, FrozenMap)
                or set(row) != {"quantity", "reason"} or type(row.get("quantity")) is not int
                or row["quantity"] < 0 or _text(row.get("reason")) is None):
            bad("degraded", identity, "malformed")
    exempt = protection_sets["exit_exempt"]
    reset = protection_sets["integrity_reset_symbols"]
    explicit, quote_views = _fmap(state.get("latest_explicit_quote")), _fmap(state.get("quote_price_views"))
    symbols = set()
    for source in (protection_by_symbol, pending_owners, _fmap(protection.get("degraded")),
                   active_buys, attempts_by_symbol, audits_by_symbol, admissions_by_symbol,
                   explicit, quote_views, positions, _fmap(protection.get("entry_times")),
                   exempt, reset, orders_by_symbol):
        for symbol in source:
            yield
            symbols.add(symbol)
    inputs = _BuildMap()
    for symbol in symbols:
        yield
        if _text(symbol) is not None:
            inputs[symbol] = _protection_input(protection, symbol, orders_by_symbol, exempt, reset)
    day_view = _fmap(state.get("day_valuation_view"))
    selected = _fmap(_fmap(state.get("day_valuations")).get(day_view.get("evidence_id")))
    prices = _fmap(selected.get("payload")).get("prices", ())
    day_prices = _BuildMap()
    if type(prices) is not tuple:
        bad("quote", None, "malformed_day_prices")
        prices = ()
    for row in prices:
        yield
        if not isinstance(row, FrozenMap) or _text(row.get("symbol")) is None:
            bad("quote", None, "malformed_day_price")
        else:
            if row["symbol"] in day_prices:
                bad("quote", row["symbol"], "duplicate_day_price")
            day_prices[row["symbol"]] = row
    if day_view.get("evidence_id") is not None and not selected:
        bad("quote", None, "missing_day_valuation")
    quotes = _BuildMap()
    quote_symbols = set()
    for source in (explicit, quote_views, day_prices):
        for symbol in source:
            yield
            quote_symbols.add(symbol)
    for symbol in quote_symbols:
        yield
        exact, view = explicit.get(symbol), quote_views.get(symbol)
        if (exact is not None and not isinstance(exact, FrozenMap)) or (view is not None and not isinstance(view, FrozenMap)):
            bad("quote", symbol, "malformed")
            continue
        exact_map, view_map = _fmap(exact), _fmap(view)
        if exact is not None and not _valid_explicit_quote(symbol, exact_map, token.revision):
            bad("quote", symbol, "invalid_explicit_quote")
        scoped = {"quote_price_views": {symbol: thaw_checkpoint_facts(view)} if view is not None else {},
                  "latest_explicit_quote": {symbol: thaw_checkpoint_facts(exact)} if exact is not None else {}}
        if view is not None and not _valid_price_view_for_symbol(scoped, symbol, token.revision):
            bad("quote", symbol, "invalid_or_stale_source")
        valued = day_prices.get(symbol, FrozenMap({}))
        floor = None
        try:
            stamps = [row["as_of"] for row in (valued, exact_map) if row]
            for stamp in stamps:
                if datetime.fromisoformat(stamp).utcoffset() is None:
                    raise ValueError("naive_quote_time")
            floor = max(stamps, key=datetime.fromisoformat, default=None)
        except (ValueError, KeyError, TypeError):
            bad("quote", symbol, "invalid_quote_time")
        quotes[symbol] = ExplicitQuoteFact(symbol, FrozenMap._trusted({"explicit": exact_map, "price_view": view_map,
            "day_valuation_view": day_view if valued else FrozenMap({}),
            "day_valuations": valued, "time_floor": floor}))
    active_recovery = set()
    for source in (pending_owners, _fmap(protection.get("degraded")), positions, protection_by_symbol):
        for symbol in source:
            yield
            active_recovery.add(symbol)
    for facts in attempts_by_symbol.values():
        yield
        for fact in facts:
            yield
            if (fact.kind == "submit" and fact.symbol
                    and (fact.state not in TERMINAL_STATES or _remaining(fact.data))):
                active_recovery.add(fact.symbol)
    buckets = []
    for source in (attempts_by_symbol, attempts_by_intent, intents_by_symbol, audits_by_intent,
                   audits_by_symbol, admissions_by_intent, admissions_by_symbol, pending_by_intent):
        for key, rows in source.items():
            yield
            source[key] = tuple(rows)
        buckets.append(FrozenMap._take_owned(source))
    ordered_admissions = []
    while admission_heap:
        yield
        ordered_admissions.append(heapq.heappop(admission_heap)[2])
    pending_recovery = yield from _sorted_steps(pending_owners)
    open_buys = yield from _sorted_steps(active_buys)
    recovery = yield from _sorted_steps(active_recovery)
    # Seals take ownership in O(1); do not copy the entire 100k-key index at
    # finalization. Scratch containers are released incrementally as well.
    for temporary in (symbols, quote_symbols, day_prices, orders_by_symbol,
                      active_buys, active_recovery, exempt, reset):
        yield from _clear_steps(temporary)
    data = _IndexData(
        buckets[0], buckets[1], FrozenMap._take_owned(intents),
        buckets[2], buckets[3], buckets[4],
        buckets[5], buckets[6], FrozenMap._take_owned(admissions_by_command),
        tuple(ordered_admissions),
        buckets[7],
        FrozenMap._take_owned(pending_owner_values),
        FrozenMap._take_owned(protection_by_symbol), FrozenMap._take_owned(inputs), FrozenMap._take_owned(quotes), pending_recovery,
        open_buys, recovery, tuple(invalid), bool(_fmap(protection.get("degraded"))),
    )
    return ProducerRecoveryIndex(token, not invalid, data)


def _clear_steps(values):
    """Release scratch objects between deadlines, including nested buckets."""
    while values:
        yield
        if isinstance(values, MutableMapping):
            _, child = values.popitem()
            if type(child) in (list, set, dict) or isinstance(child, _BuildMap):
                yield from _clear_steps(child)
        else:
            values.pop()


def _sorted_steps(values):
    heap, ordered = [], []
    for value in values:
        yield
        heapq.heappush(heap, value)
    while heap:
        yield
        ordered.append(heapq.heappop(heap))
    return tuple(ordered)



class DetachedCheckpoint:
    """Single-use ownership handoff, created synchronously under caller ownership.

    Only detach_checkpoint creates this capsule. Its private graph has no aliases
    into owner RAM and transfers to exactly one build session.
    """
    __slots__ = ("__state",)

    def __init__(self):
        raise TypeError("use_detach_checkpoint")

    def __setattr__(self, name, value):
        raise AttributeError("detached_checkpoint_is_private")

    def _take(self):
        state = self.__state
        if state is None:
            raise ValueError("detached_checkpoint_already_consumed")
        object.__setattr__(self, "_DetachedCheckpoint__state", None)
        return state


def detach_checkpoint(state: Mapping[str, object]) -> DetachedCheckpoint:
    """Snapshot while the caller holds its owner/restore gate; never yields.

    marshal is an in-process copy, never a disk or untrusted wire format.
    Recursive JSON/finite validation follows in the incremental freeze phase.
    Exact dict input avoids invoking arbitrary mapping callbacks under ownership.
    """
    if type(state) is not dict:
        raise ValueError("checkpoint_dict_required")
    detached = marshal.loads(marshal.dumps(state))
    result = object.__new__(DetachedCheckpoint)
    object.__setattr__(result, "_DetachedCheckpoint__state", detached)
    return result


def _freeze_steps(value):
    yield
    if isinstance(value, FrozenMap):
        # A shared child already frozen inside this private detached graph.
        return value
    if type(value) is dict:
        for key, child in value.items():
            if type(key) is not str:
                raise ValueError("invalid_checkpoint_key")
            value[key] = yield from _freeze_steps(child)
        return FrozenMap._take_owned(value)
    if type(value) in (list, tuple):
        result = []
        for child in value:
            result.append((yield from _freeze_steps(child)))
        return tuple(result)
    return _freeze_json(value)


class RecoveryBuildSession:
    """Private resumable work. A result exists only after all phases finish."""
    __slots__ = ("phase", "result", "_steps", "_clock")

    def __init__(self, payload: DetachedCheckpoint, *, token: OwnerToken,
                 clock: Callable[[], float] = time.monotonic):
        _require_token(token)
        if type(payload) is not DetachedCheckpoint:
            raise ValueError("detached_checkpoint_required")
        self.phase, self.result, self._clock = "classify", None, clock
        self._steps = self._build(payload._take(), token)

    def _build(self, state, token):
        projection = yield from _projection_steps(state, token)
        self.phase = "freeze"
        yield
        frozen = yield from _freeze_steps(state)
        self.phase = "join"
        yield
        join_view = yield from _join_steps(frozen, token=token)
        self.phase = "index"
        yield
        index = yield from _index_steps(frozen, token=token)
        self.phase = "finalize"
        # Only the private checkpoint root is consumed, never any child fact
        # or consumer-owned FrozenMap. Drop one root reference per step so
        # independent history tables are not all decref'd on generator return.
        while state:
            yield _SLICE_BOUNDARY
            state.popitem()
        yield _SLICE_BOUNDARY
        if (not index.complete or not join_view.complete) and projection.complete:
            projection = OwnerRecoveryProjection(1, token, False, "invalid_index", projection.findings)
        return FullBuildResult(projection, join_view, index)

    def advance(self) -> int:
        if self._steps is None:
            raise ValueError("closed_build_session")
        started, examined = self._clock(), 0
        try:
            # Leave headroom for the final bounded step and return bookkeeping.
            # CPython's automatic GC is unpreemptible; it is measured separately
            # by the normal-GC continuous-stall acceptance cell.
            while examined < 256 and self._clock() - started < 0.004:
                step = next(self._steps)
                examined += 1
                if step is _SLICE_BOUNDARY:
                    break
        except StopIteration as done:
            self.result, self.phase, self._steps = done.value, "complete", None
        except BaseException:
            self.close()
            raise
        return examined

    def close(self):
        if self._steps is not None:
            self._steps.close()
            self._steps = None
        self.result = None
        self.phase = "cancelled"


def build_owner_join_view(frozen: FrozenJSON, *, token: OwnerToken) -> OwnerRecoveryJoinView:
    return _drain(_join_steps(frozen, token=token))


def build_producer_index(frozen: FrozenJSON, *, token: OwnerToken) -> ProducerRecoveryIndex:
    return _drain(_index_steps(frozen, token=token))


def build_owner_recovery_models(state: Mapping[str, object], *, token: OwnerToken) -> FullBuildResult:
    session = RecoveryBuildSession(detach_checkpoint(state), token=token)
    while session.result is None:
        session.advance()
    return session.result


def build_owner_recovery_models_cooperatively(
    payload: DetachedCheckpoint, *, token: OwnerToken, yield_hook: Callable[[], object] | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> object:
    _require_token(token)
    if type(payload) is not DetachedCheckpoint:
        raise ValueError("detached_checkpoint_required")

    async def run() -> FullBuildResult:
        session = RecoveryBuildSession(payload, token=token, clock=clock)
        try:
            while session.result is None:
                session.advance()
                if session.result is not None:
                    return session.result
                if yield_hook is not None:
                    value = yield_hook()
                    if inspect.isawaitable(value):
                        await value
                await asyncio.sleep(0)
        finally:
            session.close()
    return run()


build_owner_recovery_models_async = build_owner_recovery_models_cooperatively


def _fmap(value: object) -> FrozenMap:
    return value if isinstance(value, FrozenMap) else _EMPTY_MAP


def _require_token(token: object) -> None:
    if type(token) is not OwnerToken:
        raise ValueError("invalid_owner_token")


def _text(value: object) -> str | None:
    return value if type(value) is str and value and value.strip() == value else None


def _tuple_at(mapping: FrozenMap, key: str) -> tuple[Any, ...]:
    value = mapping.get(key, ())
    return value if type(value) is tuple else ()


def _valid_attempt(identity: str, row: FrozenMap) -> bool:
    return (_text(identity) is not None and _text(row.get("intent_id")) is not None
            and _text(row.get("symbol")) is not None and row.get("kind") in ("submit", "cancel", "modify")
            and row.get("side") in ("buy", "sell"))


def _valid_attempt_schema(row: FrozenMap) -> bool:
    if row.get("state") not in _ORDER_STATES:
        return False
    if row.get("command_status") not in (None, "not_sent", "acknowledged", "rejected", "unknown"):
        return False
    if not all(type(row.get(name)) is int and row[name] >= 0
               for name in ("observed_quantity", "applied_quantity", "reserved_quantity")):
        return False
    if row["applied_quantity"] > row["observed_quantity"] or row.get("reserved_planned_risk", "0") is None:
        return False
    try:
        has_remaining_reservation(dict(row))
    except (ValueError, TypeError, InvalidOperation):
        return False
    return True


def _remaining(row):
    try:
        return has_remaining_reservation(dict(row))
    except (ValueError, TypeError, InvalidOperation):
        # Damaged reservation evidence cannot remove an active symbol.
        return True


def _valid_admission(row: FrozenMap, command: str, revision: int) -> bool:
    version = row.get("source_version")
    fields = {"symbol", "price", "market_data", "intent_id", "observed_at", "market_as_of", "source", "source_event_id"}
    if "entry_observation" in row:
        fields.add("entry_observation")
    if (not command.startswith("quote:") or len(command) <= 6 or type(version) is not int
            or not 0 < version <= revision or row.get("status") != "RECEIVED"
            or set(row) != fields | {"payload_digest", "status", "source_version", "admitted_at"}):
        return False
    try:
        price = Decimal(row["price"]) if type(row["price"]) is str else Decimal("NaN")
        received, admitted = datetime.fromisoformat(row["observed_at"]), datetime.fromisoformat(row["admitted_at"])
        if (not price.is_finite() or price <= 0 or received.utcoffset() is None or admitted.utcoffset() is None
                or received > admitted or (row["market_data"] is not None and not isinstance(row["market_data"], FrozenMap))):
            return False
        if row["market_as_of"] is None:
            if row["source"] is not None or row["source_event_id"] is not None:
                return False
        else:
            observed = datetime.fromisoformat(row["market_as_of"])
            if (observed.utcoffset() is None or observed > received
                    or _text(row["source"]) is None or _text(row["source_event_id"]) is None):
                return False
        request = {key: thaw_checkpoint_facts(row[key]) for key in fields}
        return row["payload_digest"] == digest(request)
    except (ValueError, TypeError, KeyError, InvalidOperation):
        return False


def _valid_protection_steps(symbol: str, row: FrozenMap):
    # The canonical validator deep-copies an entire state's exit_history.
    # Its checks are separable: validate scalar state once and then each event
    # using the same canonical validator with a single-event history.
    if len(row) != len(_STATE_FIELDS):
        return False
    raw = {}
    for name, value in row.items():
        yield
        if name != "exit_history":
            if isinstance(value, FrozenMap) or type(value) is tuple:
                return False
            raw[name] = value
    history = row.get("exit_history")
    if type(history) is not tuple:
        return False
    raw["exit_history"] = []
    try:
        _state_from_dict(symbol, raw)
        for event in history:
            yield
            if not isinstance(event, FrozenMap) or len(event) != 5:
                return False
            if any(isinstance(value, FrozenMap) or type(value) is tuple for value in event.values()):
                return False
            raw["exit_history"] = [dict(event)]
            _state_from_dict(symbol, raw)
    except (ValueError, TypeError, KeyError, InvalidOperation, OverflowError):
        return False
    return True


def _valid_decision(value: object) -> bool:
    return (type(value) is tuple and len(value) == 3 and value[0] in ("sell_all", "sell_partial")
            and type(value[1]) is int and value[1] > 0 and type(value[2]) is str)


def _source_version(data: FrozenMap) -> int:
    value = data.get("source_version")
    return value if type(value) is int else -1


def _valid_price_view_for_symbol(state: dict[str, object], symbol: str, revision: int) -> bool:
    """Delegate freshness/source semantics to the product's canonical validator."""
    views = state.get("quote_price_views")
    if type(views) is not dict or symbol not in views:
        return True
    row = views[symbol]
    try:
        validate_price_views({"quote_price_views": {symbol: row},
                              "latest_explicit_quote": state.get("latest_explicit_quote", {})}, revision)
    except (ValueError, TypeError, KeyError, OverflowError, InvalidOperation):
        return False
    return True


def _valid_explicit_quote(symbol, row, revision):
    # The runtime validator is pure and uses only its static payload helper.
    # Passing the class avoids constructing a runtime (and any runtime I/O).
    from .runtime import KRExecutionRuntime
    try:
        KRExecutionRuntime._validate_explicit_quotes(KRExecutionRuntime,
            {"latest_explicit_quote": {symbol: thaw_checkpoint_facts(row)}}, revision)
    except (ValueError, TypeError, KeyError, InvalidOperation, OverflowError):
        return False
    return True


def _protection_input(protection: FrozenMap, symbol: str, orders_by_symbol, exempt, reset) -> FrozenMap:
    per_symbol = ("states", "entry_times", "degraded", "orders", "pending_owners")
    selected: dict[str, FrozenJSON] = {"schema": protection.get("schema"), "market": protection.get("market")}
    for name in ("config", "max_holding_days", "current_regime", "intraday_crash_level"):
        selected[name] = protection.get(name)
    selected["exit_exempt"] = (symbol,) if symbol in exempt else ()
    selected["integrity_reset_symbols"] = (symbol,) if symbol in reset else ()
    for name in per_symbol:
        row = _fmap(protection.get(name))
        if name == "orders":
            selected[name] = tuple(orders_by_symbol.get(symbol, ()))
        else:
            selected[name] = row.get(symbol)
    return FrozenMap._trusted(selected)
