"""누적 체결의 durable inbox와 단일 checkpoint 적용/게시 계약.

전략/Portfolio/ExitManager의 실제 변환은 주입된 순수 reducer의 책임이다.
이 모듈을 만드는 것만으로 기존 KR 거래 경로가 연결되지는 않는다.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
import hashlib
import inspect
import json
from types import MappingProxyType
from typing import Any, Callable, Mapping

from .store import ExecutionStateStore, encode_state


class ObservationError(ValueError):
    """체결 관측의 식별/수량/금액 계약 위반."""


class ApplicationBlocked(RuntimeError):
    """저장/게시 상태가 확정될 때까지 추가 실행을 금지한다."""


def _freeze(value):
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


def _thaw(value):
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


@dataclass(frozen=True)
class FillObservation:
    account_scope: str
    market: str
    trading_day: str
    exchange: str
    order_id: str
    symbol: str
    side: str
    cumulative_quantity: int
    cumulative_amount: Decimal
    cumulative_fee: Decimal = Decimal("0")
    metadata: Mapping[str, Any] = field(default_factory=dict)
    org_no: str = ""
    parent_order_no: str = ""

    def __post_init__(self):
        for value in (self.account_scope, self.market, self.exchange, self.order_id, self.symbol):
            if type(value) is not str or not value or value != value.strip():
                raise ObservationError("완전한 주문 범위 식별자가 필요합니다")
        for value in (self.org_no, self.parent_order_no):
            if type(value) is not str or value != value.strip():
                raise ObservationError("기관/원주문 식별자는 공백 없는 문자열이어야 합니다")
        if self.order_id.startswith(("TEMP_", "local-")):
            raise ObservationError("합성 주문번호는 브로커 체결 근거가 아닙니다")
        try:
            if date.fromisoformat(self.trading_day).isoformat() != self.trading_day:
                raise ValueError
        except (TypeError, ValueError) as exc:
            raise ObservationError("주문 영업일은 ISO 날짜여야 합니다") from exc
        if self.side not in ("BUY", "SELL"):
            raise ObservationError("체결 방향은 BUY/SELL이어야 합니다")
        if type(self.cumulative_quantity) is not int or self.cumulative_quantity < 0:
            raise ObservationError("누적체결수량은 0 이상 정수여야 합니다")
        for name in ("cumulative_amount", "cumulative_fee"):
            try:
                value = getattr(self, name)
                if isinstance(value, bool):
                    raise ValueError
                amount = Decimal(str(value))
                if not amount.is_finite() or amount < 0:
                    raise ValueError
            except (InvalidOperation, ValueError) as exc:
                raise ObservationError("체결 금액/비용은 유한한 0 이상 값이어야 합니다") from exc
            object.__setattr__(self, name, amount)
        if ((self.cumulative_quantity == 0 and (self.cumulative_amount != 0 or self.cumulative_fee != 0))
                or (self.cumulative_quantity > 0 and self.cumulative_amount <= 0)):
            raise ObservationError("체결수량과 금액 불일치")
        try:
            metadata = json.loads(encode_state(dict(self.metadata)))
        except (TypeError, ValueError) as exc:
            raise ObservationError("체결 metadata는 JSON dict여야 합니다") from exc
        object.__setattr__(self, "metadata", _freeze(metadata))

    @property
    def order_key(self) -> str:
        # lifecycle.OrderRef.key와 같은 순서: side/symbol은 아래 identity 충돌로 검사.
        return json.dumps([self.account_scope, self.market, self.trading_day,
                           self.exchange, self.order_id, self.org_no, self.parent_order_no],
                          separators=(",", ":"), ensure_ascii=False)

    def to_dict(self) -> dict:
        return {
            "account_scope": self.account_scope, "market": self.market,
            "trading_day": self.trading_day, "exchange": self.exchange,
            "order_id": self.order_id, "org_no": self.org_no,
            "parent_order_no": self.parent_order_no, "symbol": self.symbol,
            "side": self.side, "cumulative_quantity": self.cumulative_quantity,
            "cumulative_amount": str(self.cumulative_amount),
            "cumulative_fee": str(self.cumulative_fee), "metadata": _thaw(self.metadata),
        }

    @property
    def observation_id(self) -> str:
        return hashlib.sha256(encode_state(self.to_dict()).encode()).hexdigest()

    @property
    def identity(self) -> dict:
        return {"order_key": self.order_key, "symbol": self.symbol,
                "side": self.side, "metadata": _thaw(self.metadata)}


@dataclass(frozen=True)
class FillDelta:
    quantity: int
    amount: Decimal
    fee: Decimal

    @property
    def price(self) -> Decimal:
        return self.amount / self.quantity


@dataclass(frozen=True)
class FillReduction:
    state: dict
    protection_status: str = "ready"
    journal_pending: bool = True


@dataclass(frozen=True)
class FillReceipt:
    status: str
    protection_status: str
    execution_version: int
    journal_pending: bool
    reason: str = ""


@dataclass(frozen=True)
class IngressContext:
    ticket: int
    generation: int
    received_at: datetime
    fence_id: str | None = None
    defer_reason: str = ""
    replay_fence_id: str | None = None

    def to_dict(self):
        if (type(self.ticket) is not int or self.ticket < 1
                or type(self.generation) is not int or self.generation < 0
                or self.received_at.tzinfo is None or self.received_at.utcoffset() is None):
            raise ObservationError("수신 ticket/세대/aware 시각이 필요합니다")
        return {"ticket": self.ticket, "generation": self.generation,
                "received_at": self.received_at.isoformat(), "fence_id": self.fence_id,
                "defer_reason": self.defer_reason, "replay_fence_id": self.replay_fence_id}


@dataclass(frozen=True)
class InboxReceipt:
    observation_id: str
    status: str
    execution_version: int
    application_complete: bool
    reason: str = ""


class FillApplicationCoordinator:
    """모든 core 명령에 하나의 순서를 부여하고 publish 전 성공을 반환하지 않는다."""

    def __init__(self, store: ExecutionStateStore, publisher: Callable,
                 reducer: Callable | None = None):
        self.store = store
        self.publisher = publisher
        self.reducer = reducer
        self._lock = asyncio.Lock()
        self._state: dict = {}
        self._version = 0
        self._published_version = -1
        self._healthy = False
        self._publication_recovery_required = True

    @property
    def state(self) -> dict:
        return deepcopy(self._state)

    @property
    def version(self) -> int:
        return self._version

    @property
    def published_version(self) -> int:
        return self._published_version

    @property
    def healthy(self) -> bool:
        return (self._healthy and not self._publication_recovery_required
                and self._published_version == self._version)

    @property
    def publication_recovery_required(self) -> bool:
        return self._publication_recovery_required

    def _block(self) -> None:
        self._healthy = False
        self._publication_recovery_required = True

    def _require_ready(self) -> None:
        if not self.healthy:
            raise ApplicationBlocked("실행 checkpoint 복구/게시가 필요합니다")

    @staticmethod
    def _same(left, right) -> bool:
        # Python의 True == 1도 제어 상태의 동일성으로 인정하지 않는다.
        return encode_state({"value": left}) == encode_state({"value": right})

    def _validate_fill_write_set(self, candidate: dict, observation: FillObservation,
                                 delta: FillDelta) -> None:
        """fill만의 쓰기 집합. lifecycle의 trusted mutate에는 적용하지 않는다."""
        domain_roots = {"portfolio", "protection", "risk", "lots", "outbox"}
        if not self._state.keys() <= candidate.keys():
            raise ValueError("fill reducer가 기존 checkpoint root를 삭제했습니다")
        for key in candidate.keys() | self._state.keys():
            if key in domain_roots:
                continue
            if key == "protection_replay":
                from .protection_recovery import validate_fill_evidence
                validate_fill_evidence(self._state, candidate, observation)
                continue
            if key == "initial_stop_evidence":
                from .initial_r import validate_initial_stop_write_set
                validate_initial_stop_write_set(self._state, candidate, observation)
                continue
            if key == 'entry_policy_effects':
                self._validate_policy_effect_write_set(candidate, observation)
                continue
            if key not in self._state:
                raise ValueError("fill reducer의 알 수 없는 신규 root")
            if key == "attempts":
                self._validate_attempt_write_set(candidate[key], observation, delta)
            elif not self._same(candidate[key], self._state[key]):
                raise ValueError("fill reducer가 보호된 checkpoint root를 변경했습니다")

    def _validate_policy_effect_write_set(self, candidate: dict, observation: FillObservation) -> None:
        from .lifecycle import OrderRef, clear_settled_pending_sector
        ref = OrderRef(observation.account_scope, observation.market, observation.trading_day,
                       observation.exchange, observation.order_id, observation.org_no, observation.parent_order_no)
        matches = [aid for aid, row in self._state['attempts'].items()
                   if row.get('kind') == 'submit' and row.get('order_ref') == ref.to_dict()
                   and row.get('symbol') == observation.symbol and row.get('side') == observation.side.lower()]
        if len(matches) != 1:
            raise ValueError('policy_release_requires_matching_submit')
        expected = {'attempts': deepcopy(candidate['attempts'])}
        if 'entry_policy_effects' in self._state:
            expected['entry_policy_effects'] = deepcopy(self._state['entry_policy_effects'])
        clear_settled_pending_sector(expected, matches[0])
        if not self._same(expected.get('entry_policy_effects'), candidate.get('entry_policy_effects')):
            raise ValueError('foreign_policy_effect_change_by_fill')

    def _validate_attempt_write_set(self, candidate: dict, observation: FillObservation,
                                    delta: FillDelta) -> None:
        previous = self._state["attempts"]
        if self._same(candidate, previous):
            return
        if type(candidate) is not dict or type(previous) is not dict or candidate.keys() != previous.keys():
            raise ValueError("fill reducer는 attempt를 생성/삭제할 수 없습니다")
        ref = {"account_scope": observation.account_scope, "market": observation.market,
               "order_date": observation.trading_day, "exchange": observation.exchange,
               "order_no": observation.order_id, "org_no": observation.org_no,
               "parent_order_no": observation.parent_order_no}
        matches = [key for key, row in previous.items() if type(row) is dict
                   and row.get("kind") == "submit" and row.get("order_ref") == ref
                   and row.get("symbol") == observation.symbol
                   and row.get("side") == observation.side.lower()]
        changed = [key for key in previous if not self._same(previous[key], candidate[key])]
        if len(matches) != 1 or changed != matches:
            raise ValueError("체결은 같은 범위의 단일 submit attempt만 변경할 수 있습니다")
        old, new = previous[matches[0]], candidate[matches[0]]
        allowed = {"applied_quantity", "reserved_quantity", "reserved_cash",
                   "reserved_exposure", "reserved_planned_risk"}
        if type(new) is not dict or old.keys() != new.keys():
            raise ValueError("attempt 필드는 보존해야 합니다")
        if any(not self._same(old[key], new[key]) for key in old.keys() - allowed):
            raise ValueError("체결 누적/예약 외 attempt 변경은 lifecycle 소유입니다")
        quantities = (old.get("applied_quantity"), new.get("applied_quantity"),
                      old.get("observed_quantity"), old.get("quantity"),
                      old.get("reserved_quantity"), new.get("reserved_quantity"))
        if any(type(value) is not int or value < 0 for value in quantities):
            raise ValueError("attempt 수량은 0 이상 정수여야 합니다")
        applied_before, applied_after, observed, ordered, reserved_before, reserved_after = quantities
        # Runtime 순서: reconcile 관측 저장 → apply → 필요 시 최종성 reconcile.
        if (applied_before != observation.cumulative_quantity - delta.quantity
                or applied_after != observation.cumulative_quantity
                or not applied_before <= applied_after <= observed <= ordered):
            raise ValueError("attempt applied 누적과 관측/cursor 불일치")
        if not max(0, reserved_before - delta.quantity) <= reserved_after <= reserved_before <= ordered:
            raise ValueError("이번 체결보다 큰 예약 수량 해제")
        if type(old.get("reserved_cash")) is not str or type(new.get("reserved_cash")) is not str:
            raise ValueError("예약 현금은 Decimal 문자열이어야 합니다")
        cash_before, cash_after = Decimal(old["reserved_cash"]), Decimal(new["reserved_cash"])
        if not cash_before.is_finite() or not cash_after.is_finite() or not 0 <= cash_after <= cash_before:
            raise ValueError("예약 현금 증가 또는 비유한/음수")
        # 수량 비례 해제보다 보수적으로 남기는 것은 허용. 나눗셈 반올림 없음.
        if ((reserved_before == 0 and cash_after != cash_before)
                or (cash_before - cash_after) * reserved_before
                > cash_before * (reserved_before - reserved_after)):
            raise ValueError("수량 비례 한도를 넘는 예약 현금 해제")
        resource_fields = {'reserved_exposure', 'reserved_planned_risk'}
        if resource_fields & old.keys():
            if not resource_fields <= old.keys():
                raise ValueError('incomplete_request_resource_reservation')
            for field in resource_fields:
                before, after = old[field], new[field]
                if before is None and field == 'reserved_planned_risk':
                    if after is not None:
                        raise ValueError('unmeasured_risk_must_remain_unknown')
                    continue
                if type(before) is not str or type(after) is not str:
                    raise ValueError('request_resource_requires_decimal_string')
                before, after = Decimal(before), Decimal(after)
                if not before.is_finite() or not after.is_finite() or not 0 <= after <= before:
                    raise ValueError('invalid_request_resource_release')
                if ((reserved_before == 0 and after != before)
                        or (before - after) * reserved_before > before * (reserved_before - reserved_after)):
                    raise ValueError('excessive_request_resource_release')

    @staticmethod
    def _require_sync(result):
        if inspect.isawaitable(result):
            if inspect.iscoroutine(result):
                result.close()
            raise TypeError("reducer/publisher는 await 없는 동기 함수여야 합니다")
        return result

    def _publish(self, state: dict, version: int) -> None:
        self._state, self._version = deepcopy(state), version
        self._block()
        try:
            self._require_sync(self.publisher(deepcopy(state), version))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise ApplicationBlocked("checkpoint 저장 후 게시 실패; 복구 필요") from exc
        self._published_version = version
        self._publication_recovery_required = False
        self._healthy = True

    async def restore(self) -> int:
        async with self._lock:
            self._block()
            version, state = await self.store.load()
            self._publish(state, version)
            return version

    async def _commit_publish(self, state: dict, commit_id: str) -> int:
        payload = json.loads(encode_state(state))
        # SQL 대기 중의 옛 게시 상태로 송신을 승인하지 못하게 한다.
        self._block()
        try:
            version = await self.store.commit(self._version, payload, commit_id)
        except BaseException:
            self._block()
            raise
        self._publish(payload, version)
        return version

    async def mutate(self, command_id: str, reducer: Callable[[dict], dict]) -> int:
        if not isinstance(command_id, str) or not command_id.strip():
            raise ValueError("command_id가 필요합니다")
        async with self._lock:
            self._require_ready()
            commit_id = "command:" + command_id
            try:
                existing = await self.store.lookup_commit(commit_id)
            except BaseException:
                self._block()
                raise
            if existing is not None:
                return existing
            candidate = self._require_sync(reducer(deepcopy(self._state)))
            return await self._commit_publish(candidate, commit_id)

    def _receipt(self, status: str, cursor: dict | None = None, reason="") -> FillReceipt:
        cursor = cursor or {}
        return FillReceipt(status, cursor.get("protection_status", "degraded"),
                           self._version, cursor.get("journal_pending", True), reason)

    async def _supersede(self, observation_id: str) -> None:
        if self._state["inbox"][observation_id]["status"] == "RECEIVED":
            candidate = deepcopy(self._state)
            candidate["inbox"][observation_id]["status"] = "SUPERSEDED"
            await self._commit_publish(candidate, "superseded:" + observation_id)

    async def _receive_locked(self, observation, context=None):
        key, oid = observation.order_key, observation.observation_id
        existing = self._state.get("inbox", {}).get(oid)
        if existing is not None:
            if (not self._same(existing.get("observation"), observation.to_dict())
                    or existing.get("order_key") != key
                    or existing.get("payload_digest", oid) != oid):
                raise ObservationError("저장된 관측 본문/identity/digest 충돌")
        else:
            received = deepcopy(self._state)
            row = {"observation": observation.to_dict(), "status": "RECEIVED",
                   "order_key": key, "payload_digest": oid}
            if context is not None:
                row["ingress_context"] = context.to_dict()
                day = received.get("risk", {}).get("day")
                if day and observation.trading_day < day:
                    row["ingress_context"]["defer_reason"] = "late_prior_day_requires_reconciliation"
            received.setdefault("inbox", {})[oid] = row
            received.setdefault("fill_identities", {}).setdefault(key, observation.identity)
            await self._commit_publish(received, "inbox:" + oid)
            existing = self._state["inbox"][oid]
        complete = existing["status"] in ("APPLIED", "SUPERSEDED")
        return InboxReceipt(oid, "ALREADY_APPLIED" if complete else existing["status"],
                            self._version, complete,
                            "" if complete else existing.get("ingress_context", {}).get("defer_reason", ""))

    async def receive(self, observation: FillObservation, *, ingress_context: IngressContext) -> InboxReceipt:
        """관측 원문만 내구 저장한다. RECEIVED는 경제 적용 성공이 아니다."""
        if not isinstance(observation, FillObservation):
            raise ObservationError("정규화된 FillObservation이 필요합니다")
        ingress_context.to_dict()
        async with self._lock:
            self._require_ready()
            return await self._receive_locked(observation, ingress_context)

    async def apply(self, observation: FillObservation, *, application_gate=None) -> FillReceipt:
        if not isinstance(observation, FillObservation):
            raise ObservationError("정규화된 FillObservation이 필요합니다")
        async with self._lock:
            self._require_ready()
            key, oid = observation.order_key, observation.observation_id
            await self._receive_locked(observation)
            if application_gate is not None:
                application_gate()
            cursor = self._state.get("cursors", {}).get(key)
            reason = ("order_identity_conflict"
                      if self._state.get("fill_identities", {}).get(key, observation.identity) != observation.identity
                      else "")
            if cursor is not None:
                quantity = cursor["quantity"]
                amount, fee = Decimal(cursor["amount"]), Decimal(cursor["fee"])
                if reason or cursor["identity"] != observation.identity:
                    reason = "order_identity_conflict"
                elif observation.cumulative_quantity == quantity:
                    if observation.cumulative_amount == amount and observation.cumulative_fee == fee:
                        await self._supersede(oid)
                        return self._receipt("ALREADY_APPLIED", cursor)
                    reason = "cumulative_amount_conflict"
                elif observation.cumulative_quantity < quantity:
                    prior_totals = [row["observation"] for row in self._state["inbox"].values()
                                    if row.get("order_key") == key
                                    and row["status"] in ("APPLIED", "SUPERSEDED")
                                    and row["observation"]["cumulative_quantity"] == observation.cumulative_quantity]
                    if any(Decimal(prior["cumulative_amount"]) != observation.cumulative_amount
                           or Decimal(prior["cumulative_fee"]) != observation.cumulative_fee
                           for prior in prior_totals):
                        reason = "historical_amount_conflict"
                    elif observation.cumulative_amount <= amount and observation.cumulative_fee <= fee:
                        await self._supersede(oid)
                        return self._receipt("ALREADY_APPLIED", cursor, "stale_observation")
                    else:
                        reason = "stale_amount_conflict"
            else:
                quantity, amount, fee = 0, Decimal("0"), Decimal("0")
            delta = FillDelta(observation.cumulative_quantity - quantity,
                              observation.cumulative_amount - amount,
                              observation.cumulative_fee - fee)
            if not reason and (delta.quantity <= 0 or delta.amount <= 0 or delta.fee < 0):
                reason = "invalid_increment"
            if reason:
                candidate = deepcopy(self._state)
                row = candidate["inbox"][oid]
                if row["status"] != "NEEDS_RECONCILIATION":
                    row.update(status="NEEDS_RECONCILIATION", reason=reason)
                    await self._commit_publish(candidate, "conflict:" + oid)
                return self._receipt("NEEDS_RECONCILIATION", cursor, reason)
            if self.reducer is None:
                return self._receipt("FAILED", cursor, "missing_reducer")
            try:
                reduction = self._require_sync(self.reducer(deepcopy(self._state), observation, delta))
                if not isinstance(reduction, FillReduction):
                    raise TypeError("reducer는 FillReduction을 반환해야 합니다")
                if reduction.protection_status not in ("ready", "degraded", "exempt"):
                    raise ValueError("알 수 없는 보호 상태")
                candidate = deepcopy(reduction.state)
                encode_state(candidate)
                self._validate_fill_write_set(candidate, observation, delta)
            except Exception:
                return self._receipt("FAILED", cursor, "reducer_failed")
            # inbox/cursor 소유권은 coordinator에 있다. domain reducer가 지우지 못한다.
            candidate["inbox"] = deepcopy(self._state.get("inbox", {}))
            candidate["cursors"] = deepcopy(self._state.get("cursors", {}))
            candidate["fill_identities"] = deepcopy(self._state.get("fill_identities", {}))
            next_cursor = {
                "identity": observation.identity, "quantity": observation.cumulative_quantity,
                "amount": str(observation.cumulative_amount), "fee": str(observation.cumulative_fee),
                "protection_status": reduction.protection_status,
                "journal_pending": reduction.journal_pending,
            }
            candidate["cursors"][key] = next_cursor
            candidate["inbox"][oid]["status"] = "APPLIED"
            await self._commit_publish(candidate, "fill:" + oid)
            return self._receipt("APPLIED", next_cursor)
