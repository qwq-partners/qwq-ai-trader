"""Detached, immutable owner recovery facts and bounded producer read views.

This module deliberately has no owner, store, or runtime side effects.  Full
construction is a restore/migration operation; normal writers install later
incremental replacements rather than using this builder on their hot path.
"""
from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from enum import Enum
import inspect
import math
import time
from types import MappingProxyType
from typing import Any, TypeAlias

from .lifecycle import TERMINAL_STATES
from .recovery_diagnostics import _classify


class FrozenMap(Mapping[str, "FrozenJSON"]):
    """A recursively detached mapping whose backing dictionary is private."""

    __slots__ = ("_values",)

    def __init__(self, values: Mapping[str, "FrozenJSON"]):
        self._values = MappingProxyType(dict(values))

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
    unsubmitted_by_intent: FrozenMap
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


@dataclass
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
PRODUCER_INDEX_SOURCE_USES = FrozenMap({
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
    if value is None or type(value) in (str, int, bool):
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError("non_finite_checkpoint_float")
        return value
    if type(value) in (list, tuple):
        return tuple(freeze_checkpoint_facts(item) for item in value)
    if isinstance(value, Mapping):
        frozen: dict[str, FrozenJSON] = {}
        for key, item in value.items():
            if type(key) is not str:
                raise ValueError("invalid_checkpoint_key")
            frozen[key] = freeze_checkpoint_facts(item)
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
    state = _fmap(frozen)
    audits: dict[str, int] = {}
    unsubmitted: dict[str, int] = {}
    pending: dict[str, tuple[str, ...]] = {}
    for _, row in _fmap(state.get("outbox")).items():
        if _text(row.get("kind")) != "protection_decision" or row.get("effect_source") not in (None,):
            continue
        intent = _text(row.get("intent_id"))
        if intent is None:
            continue
        audits[intent] = audits.get(intent, 0) + 1
        unsubmitted[intent] = unsubmitted.get(intent, 0) + 1
    for symbol, intent in _fmap(_fmap(state.get("protection")).get("pending_owners")).items():
        if _text(symbol) is not None and _text(intent) is not None:
            pending[intent] = tuple((*pending.get(intent, ()), symbol))
    return OwnerRecoveryJoinView(token, True, _freeze_scalar_map(audits), _freeze_scalar_map(unsubmitted),
                                 FrozenMap({key: tuple(sorted(value)) for key, value in pending.items()}))


def build_producer_index(frozen: FrozenJSON, *, token: OwnerToken) -> ProducerRecoveryIndex:
    state = _fmap(frozen)
    invalid: list[InvalidRecoveryFact] = []
    attempts_by_symbol: dict[str, list[AttemptFact]] = {}
    attempts_by_intent: dict[str, list[AttemptFact]] = {}
    active_buys: set[str] = set()
    for identity, row in _fmap(state.get("attempts")).items():
        data = _fmap(row)
        fact = AttemptFact(identity, _text(data.get("intent_id")), _text(data.get("symbol")),
                           _text(data.get("kind")), _text(data.get("side")), _text(data.get("state")),
                           _text(data.get("command_status")), data)
        if not _valid_attempt(identity, data):
            invalid.append(InvalidRecoveryFact("attempt", identity, "invalid_or_missing_link"))
        if fact.symbol is not None:
            attempts_by_symbol.setdefault(fact.symbol, []).append(fact)
        if fact.intent_id is not None:
            attempts_by_intent.setdefault(fact.intent_id, []).append(fact)
        if fact.side == "buy" and fact.symbol and fact.state not in {item.value for item in TERMINAL_STATES}:
            active_buys.add(fact.symbol)

    intents: dict[str, IntentFact] = {}
    intents_by_symbol: dict[str, list[IntentFact]] = {}
    for identity, row in _fmap(state.get("intents")).items():
        data = _fmap(row)
        ids = data.get("attempt_ids")
        attempt_ids = tuple(item for item in ids if type(item) is str) if type(ids) is tuple else ()
        fact = IntentFact(identity, _text(data.get("symbol")), _text(data.get("side")), attempt_ids, data)
        intents[identity] = fact
        if fact.symbol is None or fact.side not in ("buy", "sell") or len(attempt_ids) != len(ids or ()):
            invalid.append(InvalidRecoveryFact("intent", identity, "invalid_or_missing_link"))
        else:
            intents_by_symbol.setdefault(fact.symbol, []).append(fact)
        for attempt_id in attempt_ids:
            matching = _fmap(state.get("attempts")).get(attempt_id)
            if not isinstance(matching, FrozenMap) or matching.get("intent_id") != identity:
                invalid.append(InvalidRecoveryFact("intent", identity, "invalid_or_missing_link"))

    audits_by_intent: dict[str, list[AuditFact]] = {}
    audits_by_symbol: dict[str, list[AuditFact]] = {}
    for command, row in _fmap(state.get("outbox")).items():
        data = _fmap(row)
        if data.get("kind") != "protection_decision" or data.get("effect_source") not in (None,):
            continue
        decision = data.get("decision")
        fact = AuditFact(command, _text(data.get("intent_id")), _text(data.get("symbol")),
                         decision if type(decision) is tuple else None, data)
        if fact.intent_id is None or fact.symbol is None or fact.decision is None:
            invalid.append(InvalidRecoveryFact("audit", command, "invalid_or_missing_link"))
        if fact.intent_id is not None:
            audits_by_intent.setdefault(fact.intent_id, []).append(fact)
        if fact.symbol is not None:
            audits_by_symbol.setdefault(fact.symbol, []).append(fact)

    admissions_by_intent: dict[str, list[AdmissionFact]] = {}
    admissions_by_symbol: dict[str, list[AdmissionFact]] = {}
    admissions_by_command: dict[str, AdmissionFact] = {}
    for command, row in _fmap(state.get("protection_quote_admissions")).items():
        data = _fmap(row)
        fact = AdmissionFact(command, _text(data.get("intent_id")), _text(data.get("symbol")), data)
        admissions_by_command[command] = fact
        if fact.intent_id is None or fact.symbol is None:
            invalid.append(InvalidRecoveryFact("admission", command, "invalid_or_missing_link"))
        if fact.intent_id is not None:
            admissions_by_intent.setdefault(fact.intent_id, []).append(fact)
        if fact.symbol is not None:
            admissions_by_symbol.setdefault(fact.symbol, []).append(fact)

    protection = _fmap(state.get("protection"))
    protection_by_symbol = {symbol: ProtectionFact(symbol, _fmap(row))
                            for symbol, row in _fmap(protection.get("states")).items()}
    pending_owners = _fmap(protection.get("pending_owners"))
    pending_by_intent: dict[str, list[str]] = {}
    for symbol, intent in pending_owners.items():
        if _text(symbol) is None or _text(intent) is None:
            invalid.append(InvalidRecoveryFact("pending_owner", _text(symbol), "invalid_or_missing_link"))
            continue
        pending_by_intent.setdefault(intent, []).append(symbol)
    symbols = (set(protection_by_symbol) | set(pending_owners) | set(_fmap(protection.get("degraded")))
               | set(active_buys) | set(attempts_by_symbol) | set(audits_by_symbol)
               | set(admissions_by_symbol) | set(_fmap(state.get("latest_explicit_quote"))))
    inputs = {symbol: _protection_input(protection, symbol) for symbol in symbols if _text(symbol) is not None}
    quotes = {symbol: ExplicitQuoteFact(symbol, _fmap(row))
              for symbol, row in _fmap(state.get("latest_explicit_quote")).items() if _text(symbol) is not None}
    data = _IndexData(
        _fact_buckets(attempts_by_symbol), _fact_buckets(attempts_by_intent), FrozenMap(intents),
        _fact_buckets(intents_by_symbol), _fact_buckets(audits_by_intent), _fact_buckets(audits_by_symbol),
        _fact_buckets(admissions_by_intent), _fact_buckets(admissions_by_symbol), FrozenMap(admissions_by_command),
        tuple(admissions_by_command[key] for key in sorted(admissions_by_command)),
        FrozenMap({key: tuple(sorted(value)) for key, value in pending_by_intent.items()}),
        FrozenMap({key: value for key, value in pending_owners.items() if type(value) is str}),
        FrozenMap(protection_by_symbol), FrozenMap(inputs), FrozenMap(quotes), tuple(sorted(pending_owners)),
        tuple(sorted(active_buys)), tuple(sorted(symbols)), tuple(invalid), bool(_fmap(protection.get("degraded"))),
    )
    return ProducerRecoveryIndex(token, not invalid, data)


def build_owner_recovery_models(state: Mapping[str, object], *, token: OwnerToken) -> FullBuildResult:
    frozen = freeze_checkpoint_facts(state)
    return _build_frozen_models(frozen, token)


async def build_owner_recovery_models_cooperatively(
    state: Mapping[str, object], *, token: OwnerToken, yield_hook: Callable[[], object] | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> FullBuildResult:
    """Build from a detached payload and expose no partial result on cancellation."""
    frozen = freeze_checkpoint_facts(state)
    last_yield, examined = clock(), 0
    for _ in _walk_facts(frozen):
        examined += 1
        if examined >= 256 or clock() - last_yield >= 0.005:
            examined, last_yield = 0, clock()
            if yield_hook is not None:
                value = yield_hook()
                if inspect.isawaitable(value):
                    await value
                else:
                    await asyncio.sleep(0)
            else:
                await asyncio.sleep(0)
    return _build_frozen_models(frozen, token)


build_owner_recovery_models_async = build_owner_recovery_models_cooperatively


def _build_frozen_models(frozen: FrozenJSON, token: OwnerToken) -> FullBuildResult:
    projection = classify_owner_facts(frozen, token=token)
    join_view = build_owner_join_view(frozen, token=token)
    index = build_producer_index(frozen, token=token)
    if not index.complete and projection.complete:
        projection = OwnerRecoveryProjection(projection.schema_version, token, False, "invalid_index",
                                             projection.findings)
    return FullBuildResult(projection, join_view, index)


def _fmap(value: object) -> FrozenMap:
    return value if isinstance(value, FrozenMap) else FrozenMap({})


def _text(value: object) -> str | None:
    return value if type(value) is str and value and value.strip() == value else None


def _tuple_at(mapping: FrozenMap, key: str) -> tuple[Any, ...]:
    value = mapping.get(key, ())
    return value if type(value) is tuple else ()


def _freeze_scalar_map(values: Mapping[str, int]) -> FrozenMap:
    return FrozenMap({key: value for key, value in values.items()})


def _fact_buckets(values: Mapping[str, list[Any]]) -> FrozenMap:
    return FrozenMap({key: tuple(value) for key, value in values.items()})


def _valid_attempt(identity: str, row: FrozenMap) -> bool:
    return (_text(identity) is not None and _text(row.get("intent_id")) is not None
            and _text(row.get("symbol")) is not None and row.get("kind") in ("submit", "cancel", "modify")
            and row.get("side") in ("buy", "sell"))


def _protection_input(protection: FrozenMap, symbol: str) -> FrozenMap:
    per_symbol = ("states", "entry_times", "degraded", "orders", "pending_owners")
    selected: dict[str, FrozenJSON] = {"schema": protection.get("schema"), "market": protection.get("market")}
    for name in ("config", "exit_exempt", "max_holding_days", "current_regime", "intraday_crash_level",
                 "integrity_reset_symbols"):
        selected[name] = protection.get(name)
    for name in per_symbol:
        row = _fmap(protection.get(name))
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
