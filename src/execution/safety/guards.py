"""KR 송신 직전의 순수 검사. 승인 결과는 로컬 HTTP 시작 시점에만 유효하다."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time
from enum import Enum
from typing import Callable
from zoneinfo import ZoneInfo

_KST = ZoneInfo('Asia/Seoul')
_LEVELS = frozenset({'normal', 'caution', 'crash', 'severe'})


@dataclass(frozen=True)
class GuardDecision:
    allowed: bool
    reason: str


@dataclass(frozen=True)
class RiskSnapshot:
    version: int
    attempt_sequence: int
    observation_status: str
    as_of: datetime | None
    level: str | None
    recovery_until: datetime | None = None


class RiskSnapshotPublisher:
    """이전 정상 관측이 최신 실패·결측을 가리지 않도록 원자적으로 게시한다."""

    def __init__(self):
        self.snapshot = RiskSnapshot(0, 0, 'missing', None, None)

    def publish(self, *, attempt_sequence: int, as_of: datetime | None,
                level: str | None, observation_status: str = 'success',
                recovery_until: datetime | None = None) -> RiskSnapshot:
        if type(attempt_sequence) is not int or attempt_sequence <= 0:
            raise ValueError('관측 시도 번호는 양의 정수여야 합니다')
        previous = self.snapshot
        if attempt_sequence < previous.attempt_sequence:
            return previous
        candidate = RiskSnapshot(previous.version + 1, attempt_sequence,
                                 observation_status, as_of, level, recovery_until)
        if attempt_sequence == previous.attempt_sequence:
            if previous.observation_status == 'conflict':
                return previous
            if (as_of, level, observation_status, recovery_until) == (
                previous.as_of, previous.level, previous.observation_status,
                previous.recovery_until,
            ):
                return previous
            candidate = RiskSnapshot(previous.version + 1, attempt_sequence,
                                     'conflict', None, None)
        self.snapshot = candidate
        return candidate


class EntryOrigin(str, Enum):
    AUTOMATIC = 'automatic'
    SAFE_ASSET = 'safe_asset'
    USER = 'user'


@dataclass(frozen=True)
class EntryContext:
    symbol: str
    side: str
    strategy: str
    origin: EntryOrigin
    _issuer: object = field(repr=False, compare=False)


class EntryAuthority:
    """런타임이 신뢰된 호출 경로에만 전달하는 발급기. metadata를 받지 않는다.

    Python 프로세스 안의 악성 코드에 대한 sandbox가 아니라, 외부 문자열을
    수동 주문 권한으로 오인하지 않게 하는 호출 계약이다.
    """

    def __init__(self):
        self._identity = object()

    def automatic(self, symbol: str, side: str, strategy: str) -> EntryContext:
        return EntryContext(symbol, side, strategy, EntryOrigin.AUTOMATIC, self._identity)

    def safe_asset(self, symbol: str, side: str) -> EntryContext:
        return EntryContext(symbol, side, 'safe_asset', EntryOrigin.SAFE_ASSET, self._identity)

    def user_order(self, symbol: str, side: str) -> EntryContext:
        return EntryContext(symbol, side, 'manual', EntryOrigin.USER, self._identity)

    def owns(self, context: EntryContext | None) -> bool:
        return (isinstance(context, EntryContext)
                and context._issuer is self._identity
                and isinstance(context.origin, EntryOrigin)
                and bool(context.symbol) and context.side in {'buy', 'sell'})


def _aware(value: object) -> bool:
    return (isinstance(value, datetime) and value.tzinfo is not None
            and value.utcoffset() is not None)


class FinalEntryGuard:
    def __init__(self, authority: EntryAuthority,
                 snapshot: Callable[[], RiskSnapshot],
                 clock: Callable[[], datetime]):
        self.authority = authority
        self._snapshot = snapshot
        self._clock = clock

    def evaluate(self, context: EntryContext | None) -> GuardDecision:
        if not self.authority.owns(context):
            return GuardDecision(False, 'untrusted_entry_context')
        if context.side == 'sell' or context.origin != EntryOrigin.AUTOMATIC:
            return GuardDecision(True, 'alpha_guard_not_applicable')
        try:
            snapshot = self._snapshot()
            now = self._clock()
            if not _aware(now):
                return GuardDecision(False, 'risk_unknown')
            now = now.astimezone(_KST)
            if (not isinstance(snapshot, RiskSnapshot)
                    or type(snapshot.version) is not int or snapshot.version <= 0
                    or type(snapshot.attempt_sequence) is not int or snapshot.attempt_sequence <= 0
                    or snapshot.observation_status != 'success'
                    or snapshot.level not in _LEVELS or not _aware(snapshot.as_of)):
                return GuardDecision(False, 'risk_unknown')
            observed = snapshot.as_of.astimezone(_KST)
            if observed.date() != now.date() or observed > now:
                return GuardDecision(False, 'risk_unknown')
            if snapshot.recovery_until is not None and not _aware(snapshot.recovery_until):
                return GuardDecision(False, 'risk_unknown')
            if snapshot.level == 'severe':
                return GuardDecision(False, 'intraday_severe')
            if snapshot.recovery_until is not None and now < snapshot.recovery_until:
                return GuardDecision(False, 'recovery_cooldown')
            if context.strategy == 'sepa_trend' and now.time() >= time(14, 30):
                return GuardDecision(False, 'sepa_entry_cutoff')
        except (TypeError, ValueError, AttributeError, OverflowError):
            return GuardDecision(False, 'risk_unknown')
        return GuardDecision(True, 'allowed')


@dataclass(frozen=True)
class DispatchSnapshot:
    healthy: bool
    version: int
    published_version: int
    startup_reconciliation: bool
    publication_recovery_required: bool
    attempt_id: str
    owner_token: str
    claim_active: bool
    sent: bool
    conflicting_attempt: bool
    protection_degraded: bool


class FinalDispatchGuard:
    def __init__(self, snapshot: Callable[[], DispatchSnapshot | None],
                 entry_guard: FinalEntryGuard):
        self._snapshot = snapshot
        self._entry = entry_guard

    def evaluate(self, attempt_id: str, owner_token: str,
                 context: EntryContext | None, *, command: str = 'submit') -> GuardDecision:
        if command not in {'submit', 'cancel', 'modify'}:
            return GuardDecision(False, 'unknown_command')
        try:
            state = self._snapshot()
        except Exception:
            return GuardDecision(False, 'store_unhealthy')
        if not isinstance(state, DispatchSnapshot) or state.healthy is not True:
            return GuardDecision(False, 'store_unhealthy')
        if (state.publication_recovery_required or state.version != state.published_version):
            return GuardDecision(False, 'publication_recovery_required')
        if state.startup_reconciliation:
            return GuardDecision(False, 'startup_reconciliation')
        if (not attempt_id or not owner_token or not state.claim_active
                or state.attempt_id != attempt_id or state.owner_token != owner_token):
            return GuardDecision(False, 'sender_claim_invalid')
        if state.sent:
            return GuardDecision(False, 'already_dispatched')
        if state.conflicting_attempt:
            return GuardDecision(False, 'intent_conflict')
        if not self._entry.authority.owns(context):
            return GuardDecision(False, 'untrusted_entry_context')
        # BUY 정정도 신규 노출/추격을 만들 수 있다. 취소만 alpha 예외다.
        if command == 'cancel':
            return GuardDecision(True, 'alpha_guard_not_applicable')
        if (state.protection_degraded and context.side == 'buy'
                and context.origin == EntryOrigin.AUTOMATIC):
            return GuardDecision(False, 'protection_degraded')
        return self._entry.evaluate(context)
