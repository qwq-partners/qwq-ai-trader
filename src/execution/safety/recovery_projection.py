"""Detached, immutable owner recovery facts and bounded producer read views.

This module deliberately has no owner, store, or runtime side effects.  Full
construction is a restore/migration operation; normal writers install later
incremental replacements rather than using this builder on their hot path.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from enum import Enum
import inspect
import sys
import math
import time
from types import MappingProxyType
from typing import Any, TypeAlias

from .lifecycle import TERMINAL_STATES
from .lifecycle import OrderState
from .market_source import validate_price_views
from .recovery_diagnostics import _classify


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
    "build_producer_index": tuple(sorted(PRODUCER_INDEX_DEPENDENCIES | PRODUCER_INDEX_EXCLUSIONS)),
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
        return FrozenMap(frozen)
    raise ValueError("invalid_checkpoint_value")


def thaw_checkpoint_facts(value: FrozenJSON) -> Any:
    """Return a fresh JSON-shaped tree; tuples represent frozen JSON lists."""
    if isinstance(value, FrozenMap):
        return {key: thaw_checkpoint_facts(item) for key, item in value.items()}
    if type(value) is tuple:
        return [thaw_checkpoint_facts(item) for item in value]
    return value


def classify_owner_facts(frozen: FrozenJSON, *, token: OwnerToken) -> OwnerRecoveryProjection:
    _require_token(token)
    state = thaw_checkpoint_facts(frozen)
    complete = True
    try:
        counts = _classify({"state": state,
                            "runtime": {"_protection_unattributed_failed": False,
                                        "_protection_failures": {}}, "producer": None})
    except (AttributeError, KeyError, TypeError, ValueError, OverflowError):
        counts = {"evidence_invalid": 1}
        complete = False
    if counts.get("evidence_invalid"):
        complete = False
    findings = tuple(sorted((code, count) for code, count in counts.items() if count))
    return OwnerRecoveryProjection(1, token, complete, None if complete else "evidence_invalid", findings)


def build_owner_join_view(frozen: FrozenJSON, *, token: OwnerToken) -> OwnerRecoveryJoinView:
    _require_token(token)
    state = _fmap(frozen)
    audits: dict[str, int] = {}
    audit_keys: dict[tuple[str, str, tuple[FrozenJSON, ...]], int] = {}
    unsubmitted: dict[str, int] = {}
    pending: dict[str, tuple[str, ...]] = {}
    complete = True
    intents = _fmap(state.get("intents"))
    admissions = _fmap(state.get("protection_quote_admissions"))
    for command, raw in _fmap(state.get("outbox")).items():
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
        linked = intent in intents or command in admissions or any(
            _fmap(row).get("intent_id") == intent for row in admissions.values() if isinstance(row, FrozenMap))
        if not linked:
            unsubmitted[intent] = unsubmitted.get(intent, 0) + 1
    for symbol, intent in _fmap(_fmap(state.get("protection")).get("pending_owners")).items():
        if _text(symbol) is not None and _text(intent) is not None:
            pending[intent] = tuple((*pending.get(intent, ()), symbol))
        else:
            complete = False
    pairs = {(symbol, intent): 1 for intent, symbols in pending.items() for symbol in symbols
             if intent not in intents and not any(isinstance(row, FrozenMap)
             and row.get("symbol") == symbol and row.get("intent_id") == intent for row in admissions.values())}
    return OwnerRecoveryJoinView(token, complete, _freeze_scalar_map(audits), FrozenKeyMap(audit_keys),
                                 _freeze_scalar_map(unsubmitted), FrozenKeyMap(pairs),
                                 FrozenMap({key: tuple(sorted(value)) for key, value in pending.items()}))


def build_producer_index(frozen: FrozenJSON, *, token: OwnerToken) -> ProducerRecoveryIndex:
    _require_token(token)
    state = _fmap(frozen)
    invalid: list[InvalidRecoveryFact] = []
    def bad(category: str, identity: object, reason: str = "invalid_or_missing_link") -> None:
        invalid.append(InvalidRecoveryFact(category, _text(identity), reason))

    attempts_by_symbol: dict[str, list[AttemptFact]] = {}
    attempts_by_intent: dict[str, list[AttemptFact]] = {}
    active_buys: set[str] = set()
    raw_attempts, raw_intents = _fmap(state.get("attempts")), _fmap(state.get("intents"))
    for identity, row in raw_attempts.items():
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
                  and type(member_ids) is tuple and member_ids.count(identity) == 1
                  and data.get("attempt_id") == identity and intent_row.get("symbol") == fact.symbol
                  and intent_row.get("side") == fact.side)
        if not _valid_attempt(identity, data) or not _valid_attempt_schema(data) or not linked:
            bad("attempt", identity)
        if fact.symbol is not None:
            attempts_by_symbol.setdefault(fact.symbol, []).append(fact)
        if fact.intent_id is not None:
            attempts_by_intent.setdefault(fact.intent_id, []).append(fact)
        if (fact.kind == "submit" and fact.side == "buy" and fact.symbol
                and (fact.state not in TERMINAL_STATES or data.get("reserved_quantity") != 0)):
            active_buys.add(fact.symbol)

    intents: dict[str, IntentFact] = {}
    intents_by_symbol: dict[str, list[IntentFact]] = {}
    for identity, row in raw_intents.items():
        if not isinstance(row, FrozenMap):
            bad("intent", identity, "malformed")
            continue
        data = _fmap(row)
        ids = data.get("attempt_ids")
        attempt_ids = tuple(item for item in ids if type(item) is str) if type(ids) is tuple else ()
        fact = IntentFact(identity, _text(data.get("symbol")), _text(data.get("side")), attempt_ids, data)
        intents[identity] = fact
        if (fact.symbol is None or fact.side not in ("buy", "sell") or len(attempt_ids) != len(ids or ())
                or len(set(attempt_ids)) != len(attempt_ids)):
            bad("intent", identity)
        else:
            intents_by_symbol.setdefault(fact.symbol, []).append(fact)
        for attempt_id in attempt_ids:
            matching = raw_attempts.get(attempt_id)
            if not isinstance(matching, FrozenMap) or matching.get("intent_id") != identity:
                bad("intent", identity)

    audits_by_intent: dict[str, list[AuditFact]] = {}
    audits_by_symbol: dict[str, list[AuditFact]] = {}
    for command, row in _fmap(state.get("outbox")).items():
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

    admissions_by_intent: dict[str, list[AdmissionFact]] = {}
    admissions_by_symbol: dict[str, list[AdmissionFact]] = {}
    admissions_by_command: dict[str, AdmissionFact] = {}
    for command, row in _fmap(state.get("protection_quote_admissions")).items():
        if not isinstance(row, FrozenMap):
            bad("admission", command, "malformed")
            continue
        data = _fmap(row)
        fact = AdmissionFact(command, _text(data.get("intent_id")), _text(data.get("symbol")), data)
        admissions_by_command[command] = fact
        intent = _fmap(raw_intents.get(fact.intent_id))
        if (fact.intent_id is None or fact.symbol is None or not isinstance(intent, FrozenMap)
                or intent.get("symbol") != fact.symbol or not _valid_admission(data)):
            bad("admission", command)
        if fact.intent_id is not None:
            admissions_by_intent.setdefault(fact.intent_id, []).append(fact)
        if fact.symbol is not None:
            admissions_by_symbol.setdefault(fact.symbol, []).append(fact)

    protection = _fmap(state.get("protection"))
    protection_by_symbol = {}
    for symbol, row in _fmap(protection.get("states")).items():
        if _text(symbol) is None or not isinstance(row, FrozenMap) or not _valid_protection(row):
            bad("protection", symbol, "malformed")
        else:
            protection_by_symbol[symbol] = ProtectionFact(symbol, row)
    pending_owners = _fmap(protection.get("pending_owners"))
    pending_by_intent: dict[str, list[str]] = {}
    for symbol, intent in pending_owners.items():
        if _text(symbol) is None or _text(intent) is None:
            bad("pending_owner", symbol)
            continue
        pending_by_intent.setdefault(intent, []).append(symbol)
    positions = _fmap(_fmap(state.get("portfolio")).get("positions"))
    for symbol, row in positions.items():
        if _text(symbol) is None or not isinstance(row, FrozenMap):
            bad("position", symbol, "malformed")
    orders = _fmap(protection.get("orders"))
    for identity, row in orders.items():
        if not isinstance(row, FrozenMap) or _text(row.get("symbol")) is None:
            bad("order", identity, "malformed")
    for identity, row in _fmap(protection.get("degraded")).items():
        if _text(identity) is None or not isinstance(row, FrozenMap):
            bad("degraded", identity, "malformed")
    def order_symbols() -> set[str]:
        return {_text(_fmap(row).get("symbol")) for row in orders.values()
                if _text(_fmap(row).get("symbol")) is not None}
    explicit, quote_views = _fmap(state.get("latest_explicit_quote")), _fmap(state.get("quote_price_views"))
    symbols = (set(protection_by_symbol) | set(pending_owners) | set(_fmap(protection.get("degraded")))
               | set(active_buys) | set(attempts_by_symbol) | set(audits_by_symbol)
               | set(admissions_by_symbol) | set(explicit) | set(quote_views) | set(positions)
               | set(_fmap(protection.get("entry_times"))) | set(_strings(protection.get("exit_exempt")))
               | set(_strings(protection.get("integrity_reset_symbols"))) | order_symbols())
    inputs = {symbol: _protection_input(protection, symbol) for symbol in symbols if _text(symbol) is not None}
    thawed_state = thaw_checkpoint_facts(frozen)
    quotes = {}
    for symbol in set(explicit) | set(quote_views):
        exact, view = explicit.get(symbol), quote_views.get(symbol)
        if (exact is not None and not isinstance(exact, FrozenMap)) or (view is not None and not isinstance(view, FrozenMap)):
            bad("quote", symbol, "malformed")
            continue
        exact_map, view_map = _fmap(exact), _fmap(view)
        if view is not None and not _valid_price_view_for_symbol(thawed_state, symbol, token.revision):
            bad("quote", symbol, "invalid_or_stale_source")
        quotes[symbol] = ExplicitQuoteFact(symbol, FrozenMap({"explicit": exact_map, "price_view": view_map,
            "day_valuation_view": _scoped_day_view(state.get("day_valuation_view"), symbol),
            "day_valuations": _fmap(state.get("day_valuations")).get(symbol, FrozenMap({}))}))
    active_recovery = set(pending_owners) | set(_fmap(protection.get("degraded"))) | set(positions)
    for facts in attempts_by_symbol.values():
        for fact in facts:
            if (fact.kind == "submit" and fact.symbol
                    and (fact.state not in TERMINAL_STATES or fact.data.get("reserved_quantity") != 0)):
                active_recovery.add(fact.symbol)
    active_recovery.update(protection_by_symbol)
    data = _IndexData(
        _fact_buckets(attempts_by_symbol), _fact_buckets(attempts_by_intent), FrozenMap._trusted(intents),
        _fact_buckets(intents_by_symbol), _fact_buckets(audits_by_intent), _fact_buckets(audits_by_symbol),
        _fact_buckets(admissions_by_intent), _fact_buckets(admissions_by_symbol), FrozenMap._trusted(admissions_by_command),
        tuple(sorted(admissions_by_command.values(), key=lambda fact: (_source_version(fact.data), fact.command_id))),
        FrozenMap({key: tuple(sorted(value)) for key, value in pending_by_intent.items()}),
        FrozenMap({key: value for key, value in pending_owners.items() if type(value) is str}),
        FrozenMap._trusted(protection_by_symbol), FrozenMap(inputs), FrozenMap._trusted(quotes), tuple(sorted(pending_owners)),
        tuple(sorted(active_buys)), tuple(sorted(active_recovery)), tuple(invalid), bool(_fmap(protection.get("degraded"))),
    )
    return ProducerRecoveryIndex(token, not invalid, data)


def build_owner_recovery_models(state: Mapping[str, object], *, token: OwnerToken) -> FullBuildResult:
    _require_token(token)
    frozen = freeze_checkpoint_facts(state)
    return _build_frozen_models(frozen, token)


def build_owner_recovery_models_cooperatively(
    state: Mapping[str, object], *, token: OwnerToken, yield_hook: Callable[[], object] | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> object:
    """Build from a detached payload and expose no partial result on cancellation."""
    # This synchronous boundary is intentional: hand an immutable snapshot, never
    # a caller-owned mapping, to background construction.
    _require_token(token)
    # C serialization makes a complete value snapshot before the first
    # await; no worker is ever handed a caller-owned mutable graph.
    snapshot = deepcopy(state)
    async def run() -> FullBuildResult:
        frozen = await asyncio.to_thread(_fast_worker, freeze_checkpoint_facts, snapshot)
        facts = await asyncio.to_thread(lambda: sum(1 for _ in _walk_facts(frozen)))
        last_yield = clock()
        for _ in range(max(1, (facts + 255) // 256)):
            if yield_hook is not None:
                value = yield_hook()
                if inspect.isawaitable(value):
                    await value
            await asyncio.sleep(0)
            if clock() - last_yield >= 0.005:
                last_yield = clock()
        return await asyncio.to_thread(_fast_worker, _build_frozen_models, frozen, token)
    return run()


build_owner_recovery_models_async = build_owner_recovery_models_cooperatively

def _build_frozen_models(frozen: FrozenJSON, token: OwnerToken) -> FullBuildResult:
    projection = classify_owner_facts(frozen, token=token)
    join_view = build_owner_join_view(frozen, token=token)
    index = build_producer_index(frozen, token=token)
    if (not index.complete or not join_view.complete) and projection.complete:
        projection = OwnerRecoveryProjection(projection.schema_version, token, False, "invalid_index",
                                             projection.findings)
    return FullBuildResult(projection, join_view, index)


def _fast_worker(function: Callable[..., Any], *args: object) -> Any:
    """Bound worker GIL ownership so the owner loop can service its heartbeat."""
    previous = sys.getswitchinterval()
    try:
        sys.setswitchinterval(0.0005)
        return function(*args)
    finally:
        sys.setswitchinterval(previous)


def _fmap(value: object) -> FrozenMap:
    return value if isinstance(value, FrozenMap) else FrozenMap({})


def _require_token(token: object) -> None:
    if type(token) is not OwnerToken:
        raise ValueError("invalid_owner_token")


def _text(value: object) -> str | None:
    return value if type(value) is str and value and value.strip() == value else None


def _tuple_at(mapping: FrozenMap, key: str) -> tuple[Any, ...]:
    value = mapping.get(key, ())
    return value if type(value) is tuple else ()


def _freeze_scalar_map(values: Mapping[str, int]) -> FrozenMap:
    return FrozenMap({key: value for key, value in values.items()})


def _fact_buckets(values: Mapping[str, list[Any]]) -> FrozenMap:
    return FrozenMap._trusted({key: tuple(value) for key, value in values.items()})


def _valid_attempt(identity: str, row: FrozenMap) -> bool:
    return (_text(identity) is not None and _text(row.get("intent_id")) is not None
            and _text(row.get("symbol")) is not None and row.get("kind") in ("submit", "cancel", "modify")
            and row.get("side") in ("buy", "sell"))


def _valid_attempt_schema(row: FrozenMap) -> bool:
    if row.get("state") not in {item.value for item in OrderState}:
        return False
    if row.get("command_status") not in (None, "not_sent", "acknowledged", "rejected", "unknown"):
        return False
    return all(type(row.get(name)) is int and row[name] >= 0
               for name in ("observed_quantity", "applied_quantity", "reserved_quantity"))


def _valid_admission(row: FrozenMap) -> bool:
    version = row.get("source_version")
    status = row.get("status")
    return type(version) is int and version > 0 and (status is None or status == "RECEIVED")


def _valid_protection(row: FrozenMap) -> bool:
    value = row.get("remaining_quantity")
    return type(value) is int and value >= 0


def _valid_decision(value: object) -> bool:
    return (type(value) is tuple and len(value) == 3 and value[0] in ("sell_all", "sell_partial")
            and type(value[1]) is int and value[1] > 0 and type(value[2]) is str)


def _strings(value: object) -> tuple[str, ...]:
    return tuple(item for item in value if _text(item) is not None) if type(value) is tuple else ()


def _source_version(data: FrozenMap) -> int:
    value = data.get("source_version")
    return value if type(value) is int else -1


def _quote_conflict(explicit: FrozenMap, view: FrozenMap) -> bool:
    version = explicit.get("admission_version")
    if type(version) is not int or view.get("source_version") != version:
        return False
    pairs = (("price", "price"), ("source", "source"), ("source_event_id", "source_event_id"))
    return any(explicit.get(left) != view.get(right) for left, right in pairs)


def _valid_price_view_for_symbol(state: dict[str, object], symbol: str, revision: int) -> bool:
    """Delegate freshness/source semantics to the product's canonical validator."""
    views = state.get("quote_price_views")
    if type(views) is not dict or symbol not in views:
        return True
    version = revision
    row = views[symbol]
    if type(row) is dict and type(row.get("source_version")) is int:
        version = max(version, row["source_version"])
    try:
        validate_price_views({"quote_price_views": {symbol: row},
                              "latest_explicit_quote": state.get("latest_explicit_quote", {})}, version)
    except (ValueError, TypeError, KeyError, OverflowError):
        return False
    return True


def _scoped_day_view(value: object, symbol: str) -> FrozenJSON:
    row = _fmap(value)
    if row.get("symbol") == symbol:
        return row
    return FrozenMap({})


def _protection_input(protection: FrozenMap, symbol: str) -> FrozenMap:
    per_symbol = ("states", "entry_times", "degraded", "orders", "pending_owners")
    selected: dict[str, FrozenJSON] = {"schema": protection.get("schema"), "market": protection.get("market")}
    for name in ("config", "exit_exempt", "max_holding_days", "current_regime", "intraday_crash_level",
                 "integrity_reset_symbols"):
        selected[name] = protection.get(name)
    for name in per_symbol:
        row = _fmap(protection.get(name))
        if name == "orders":
            selected[name] = tuple(value for value in row.values()
                                    if isinstance(value, FrozenMap) and value.get("symbol") == symbol)
        else:
            selected[name] = row.get(symbol)
    return FrozenMap(selected)


def _walk_facts(value: FrozenJSON) -> Iterator[FrozenJSON]:
    if isinstance(value, FrozenMap):
        for item in value.values():
            yield from _walk_facts(item)
    elif type(value) is tuple:
        for item in value:
            yield from _walk_facts(item)
    else:
        yield value
