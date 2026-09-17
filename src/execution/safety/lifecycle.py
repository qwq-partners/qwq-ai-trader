"""공유 적용 담당이 직렬화하는 KR 주문 생명주기 전이.

네트워크 송신이나 별도 writer를 만들지 않는다. durable claim은 이미 송신됐을
가능성의 근거이며 재시작 후 같은 POST를 재전송하는 권한이 아니다.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from enum import Enum
import json
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo


class CommandKind(str, Enum):
    SUBMIT = "submit"
    CANCEL = "cancel"
    MODIFY = "modify"


class CommandStatus(str, Enum):
    NOT_SENT = "not_sent"
    ACKNOWLEDGED = "acknowledged"
    REJECTED = "rejected"
    UNKNOWN = "unknown"


class OrderState(str, Enum):
    PREPARED = "prepared"
    SUBMITTING = "submitting"
    OPEN = "open"
    PARTIAL = "partial"
    CANCEL_REQUESTED = "cancel_requested"
    RECONCILING = "reconciling"
    BLOCKED_UNKNOWN = "blocked_unknown"
    FINAL_FILLED = "final_filled"
    FINAL_CANCELLED = "final_cancelled"
    FINAL_REJECTED = "final_rejected"
    FINAL_EXPIRED = "final_expired"


TERMINAL_STATES = frozenset(s.value for s in (
    OrderState.FINAL_FILLED, OrderState.FINAL_CANCELLED,
    OrderState.FINAL_REJECTED, OrderState.FINAL_EXPIRED,
))


def _quantity(value: int, *, positive: bool = False) -> int:
    if type(value) is not int or value < (1 if positive else 0):
        raise ValueError("invalid integer quantity")
    return value


def _money(value: Any) -> Decimal:
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError("invalid amount") from None
    if not amount.is_finite() or amount < 0:
        raise ValueError("invalid amount")
    return amount


@dataclass(frozen=True)
class OrderRef:
    account_scope: str
    market: str
    order_date: str
    exchange: str
    order_no: str
    org_no: str = ""
    parent_order_no: str = ""

    def __post_init__(self):
        for value in (self.account_scope, self.market, self.exchange, self.order_no):
            if not isinstance(value, str) or not value or value != value.strip():
                raise ValueError("incomplete order scope")
        if not isinstance(self.order_date, str) or date.fromisoformat(self.order_date).isoformat() != self.order_date:
            raise ValueError("invalid order date")
        for value in (self.org_no, self.parent_order_no):
            if not isinstance(value, str) or value != value.strip():
                raise ValueError("invalid order relationship")
        if self.order_no.startswith(("TEMP_", "local-")):
            raise ValueError("synthetic identifier is not broker evidence")

    @property
    def key(self) -> str:
        return json.dumps([self.account_scope, self.market, self.order_date,
                           self.exchange, self.order_no, self.org_no, self.parent_order_no],
                          ensure_ascii=False, separators=(",", ":"))

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict) -> OrderRef:
        return cls(**value)


@dataclass(frozen=True)
class CommandResult:
    status: CommandStatus
    attempt_id: str
    order_ref: OrderRef | None = None
    reason_code: str = ""

    def __post_init__(self):
        object.__setattr__(self, "status", CommandStatus(self.status))
        if not isinstance(self.attempt_id, str) or not self.attempt_id:
            raise ValueError("attempt identity required")

    def __bool__(self):
        raise TypeError("inspect CommandResult.status; UNKNOWN is not a boolean")


@dataclass(frozen=True)
class OrderEvidence:
    ref: OrderRef
    symbol: str
    side: str
    order_quantity: int = 0
    cumulative_quantity: int = 0
    cumulative_amount: Decimal = Decimal("0")
    remaining_quantity: int | None = None
    cancelled_quantity: int | None = None
    state: OrderState = OrderState.BLOCKED_UNKNOWN
    complete: bool = False
    schema_valid: bool = True
    supported_finality: bool = False
    source_contract: str = ""
    reason: str = ""
    rejected_quantity: int = 0
    observed_at: datetime | None = None
    request_started_at: datetime | None = None
    query_scope: dict = field(default_factory=dict)

    def __post_init__(self):
        _quantity(self.order_quantity)
        _quantity(self.cumulative_quantity)
        _quantity(self.rejected_quantity)
        for qty in (self.remaining_quantity, self.cancelled_quantity):
            if qty is not None:
                _quantity(qty)
        object.__setattr__(self, "cumulative_amount", _money(self.cumulative_amount))
        object.__setattr__(self, "state", OrderState(self.state))
        if self.side not in ("buy", "sell") or not self.symbol:
            raise ValueError("invalid evidence identity")


def valid_evidence_provenance(ref: OrderRef, observed_at: datetime | None,
                              request_started_at: datetime | None,
                              query_scope: dict, now: datetime) -> bool:
    """요청 범위·수신 시각 검증. 원자적 거래소 snapshot 증명은 아니다."""
    stamps = (observed_at, request_started_at, now)
    if any(not isinstance(t, datetime) or t.tzinfo is None or t.utcoffset() is None for t in stamps):
        return False
    if not request_started_at <= observed_at <= now or not isinstance(query_scope, dict):
        return False
    try:
        start = date.fromisoformat(query_scope["start_date"])
        end = date.fromisoformat(query_scope["end_date"])
        day = date.fromisoformat(ref.order_date)
        if ref.market == "KR" and day > observed_at.astimezone(ZoneInfo("Asia/Seoul")).date():
            return False
        return (start <= day <= end and query_scope["account_scope"] == ref.account_scope
                and query_scope["market"] == ref.market
                and query_scope["exchange"] == ref.exchange
                and all(isinstance(query_scope[key], str) and bool(query_scope[key])
                        for key in ("tr_id", "query_kind", "session")))
    except (KeyError, ValueError, TypeError):
        return False


def _replacement(state: dict, intent_id: str) -> int:
    intent = state.get("intents", {}).get(intent_id)
    if not intent:
        return 0
    attempts = [state["attempts"][aid] for aid in intent["attempt_ids"]]
    orders = [a for a in attempts if a["kind"] == CommandKind.SUBMIT.value]
    for order in orders:
        if order["state"] not in TERMINAL_STATES:
            return 0
        if order["observed_quantity"] != order["applied_quantity"]:
            return 0
    # 정정은 노출을 바꿀 수 있으므로 체인 확인 전 재주문 권한을 주지 않는다.
    for attempt in attempts:
        if attempt["kind"] == CommandKind.MODIFY.value and attempt.get("command_status") not in (
            CommandStatus.NOT_SENT.value, CommandStatus.REJECTED.value,
        ):
            return 0
    return max(0, intent["target_quantity"] - sum(a["applied_quantity"] for a in orders))


class OrderLifecycleCoordinator:
    """모든 전이는 owner.mutate(command_id, 동기 reducer)로만 저장한다."""

    def __init__(self, owner, *, clock=None):
        self.owner = owner
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    async def prepare(self, intent_id: str, attempt_id: str, quantity: int,
                      symbol: str, side: str, *, command: CommandKind = CommandKind.SUBMIT,
                      parent_attempt_id: str | None = None, order_ref: OrderRef | None = None,
                      reserved_cash: str = "0", origin: str = "auto", strategy: str = "") -> dict:
        _quantity(quantity, positive=True)
        cash = str(_money(reserved_cash))
        kind = CommandKind(command)
        if not intent_id or not attempt_id or not symbol or side not in ("buy", "sell"):
            raise ValueError("invalid order intent")

        def reduce(state):
            intents = state.setdefault("intents", {})
            attempts = state.setdefault("attempts", {})
            if attempt_id in attempts:
                raise ValueError("attempt already exists")
            intent = intents.get(intent_id)
            if intent and (intent["symbol"], intent["side"]) != (symbol, side):
                raise ValueError("intent identity conflict")
            if kind is CommandKind.SUBMIT:
                if intent and quantity > _replacement(state, intent_id):
                    raise ValueError("previous attempt unresolved or target exceeded")
                if not intent:
                    intent = {"symbol": symbol, "side": side, "target_quantity": quantity,
                              "attempt_ids": []}
                    intents[intent_id] = intent
            else:
                parent = attempts.get(parent_attempt_id)
                if not intent or not parent or parent["intent_id"] != intent_id or parent["kind"] != "submit":
                    raise ValueError("parent order required")
                if parent["state"] in TERMINAL_STATES or not parent.get("order_ref"):
                    raise ValueError("no known active broker order")
                if order_ref is None or order_ref.to_dict() != parent["order_ref"]:
                    raise ValueError("parent order scope mismatch")
                if any(attempts[a]["parent_attempt_id"] == parent_attempt_id
                       and attempts[a]["kind"] in ("cancel", "modify")
                       and attempts[a].get("command_status") not in ("not_sent", "rejected")
                       for a in intent["attempt_ids"]):
                    raise ValueError("previous child command unresolved")
            attempts[attempt_id] = {
                "intent_id": intent_id, "attempt_id": attempt_id, "kind": kind.value,
                "command": kind.value, "state": OrderState.PREPARED.value,
                "status": OrderState.PREPARED.value, "command_status": None,
                "claim_id": None, "version": 0, "side": side, "symbol": symbol,
                "origin": origin, "strategy": strategy, "quantity": quantity,
                "reserved_quantity": quantity if kind is CommandKind.SUBMIT else 0,
                "reserved_cash": cash if kind is CommandKind.SUBMIT else "0",
                "order_ref": order_ref.to_dict() if order_ref else None,
                "parent_attempt_id": parent_attempt_id,
                "observed_quantity": 0, "observed_amount": "0", "applied_quantity": 0,
            }
            intent["attempt_ids"].append(attempt_id)
            return state

        await self.owner.mutate(f"prepare:{attempt_id}:{uuid4().hex}", reduce)
        return self.owner.state["attempts"][attempt_id]

    async def claim(self, attempt_id: str, claim_id: str) -> bool:
        if not claim_id:
            raise ValueError("sender identity required")
        claimed = False

        def reduce(state):
            nonlocal claimed
            attempt = state.get("attempts", {}).get(attempt_id)
            if not attempt or attempt["claim_id"] is not None or attempt["state"] != "prepared":
                return state
            if attempt["kind"] != "submit":
                parent = state["attempts"][attempt["parent_attempt_id"]]
                if parent["state"] in TERMINAL_STATES or parent.get("evidence_conflict"):
                    return state
            else:
                siblings = [state["attempts"][aid] for aid in
                            state["intents"][attempt["intent_id"]]["attempt_ids"] if aid != attempt_id]
                if any(a["kind"] == "submit" and (a["state"] not in TERMINAL_STATES
                       or a["observed_quantity"] != a["applied_quantity"]) for a in siblings):
                    return state
                if any(a["kind"] == "modify" and a.get("command_status") not in
                       ("not_sent", "rejected") for a in siblings):
                    return state
            attempt["claim_id"] = claim_id
            attempt["state"] = ("cancel_requested" if attempt["kind"] == "cancel" else "submitting")
            attempt["status"] = attempt["state"]
            attempt["version"] += 1
            claimed = True
            return state

        await self.owner.mutate(f"claim:{attempt_id}:{uuid4().hex}", reduce)
        return claimed

    async def record_result(self, attempt_id: str, claim_id: str, result: CommandResult,
                            *, expected_attempt_version: int | None = None) -> bool:
        accepted = False

        def reduce(state):
            nonlocal accepted
            attempt = state.get("attempts", {}).get(attempt_id)
            if (not attempt or result.attempt_id != attempt_id or not claim_id
                    or attempt["claim_id"] != claim_id
                    or (expected_attempt_version is not None and attempt["version"] != expected_attempt_version)):
                return state
            if attempt["state"] in TERMINAL_STATES or attempt.get("evidence_conflict"):
                return state
            if (attempt["kind"] == "submit"
                    and result.status in (CommandStatus.NOT_SENT, CommandStatus.REJECTED)
                    and (attempt["observed_quantity"] > 0 or attempt["applied_quantity"] > 0
                         or Decimal(attempt["observed_amount"]) > 0)):
                # ACK 저장 유실 뒤 체결 관측이 먼저 올 수 있다. 늦은 명령 실패는
                # 경제적 체결 사실을 지우거나 남은 주문 예약을 해제할 수 없다.
                attempt["command_status"] = CommandStatus.UNKNOWN.value
                attempt["state"] = OrderState.BLOCKED_UNKNOWN.value
                attempt["status"] = attempt["state"]
                attempt["reason_code"] = "command_result_conflicts_with_fill"
                attempt["evidence_conflict"] = True
                attempt["version"] += 1
                return state
            previous = attempt.get("command_status")
            if previous and not (previous == "unknown" and result.status is CommandStatus.ACKNOWLEDGED):
                return state
            if result.order_ref is not None:
                if attempt["kind"] == "submit":
                    # 전송 담당은 합성 ID 대신 실제 요청 범위의 식별자를 전달한다.
                    if attempt["order_ref"] and attempt["order_ref"] != result.order_ref.to_dict():
                        return state
                    if any(aid != attempt_id and a.get("kind") == "submit"
                           and a.get("order_ref") == result.order_ref.to_dict()
                           for aid, a in state["attempts"].items()):
                        return state
                    attempt["order_ref"] = result.order_ref.to_dict()
                # 취소 ACK의 ODNO는 자식 명령일 수 있어 원 주문을 덮어쓰지 않는다.
                else:
                    attempt["command_ref"] = result.order_ref.to_dict()
            status = result.status
            if status is CommandStatus.ACKNOWLEDGED and attempt["kind"] == "submit" and not attempt["order_ref"]:
                status = CommandStatus.UNKNOWN
            attempt["command_status"] = status.value
            attempt["reason_code"] = result.reason_code
            if attempt["kind"] == "submit":
                if status in (CommandStatus.NOT_SENT, CommandStatus.REJECTED):
                    attempt["state"] = OrderState.FINAL_REJECTED.value
                    attempt["reserved_quantity"] = 0
                    attempt["reserved_cash"] = "0"
                elif status is CommandStatus.ACKNOWLEDGED:
                    attempt["state"] = OrderState.OPEN.value
                else:
                    attempt["state"] = OrderState.BLOCKED_UNKNOWN.value
            else:
                attempt["state"] = OrderState.RECONCILING.value
            attempt["status"] = attempt["state"]
            attempt["version"] += 1
            accepted = True
            return state

        await self.owner.mutate(f"result:{attempt_id}:{uuid4().hex}", reduce)
        return accepted

    async def reconcile(self, attempt_id: str, evidence: OrderEvidence,
                        *, applied_quantity: int | None = None) -> bool:
        if applied_quantity is not None:
            _quantity(applied_quantity)
        accepted = False

        def reduce(state):
            nonlocal accepted
            attempt = state.get("attempts", {}).get(attempt_id)
            if (not attempt or attempt["kind"] != "submit" or not evidence.schema_valid
                    or attempt["order_ref"] != evidence.ref.to_dict()
                    or (attempt["symbol"], attempt["side"]) != (evidence.symbol, evidence.side)
                    or evidence.order_quantity != attempt["quantity"]):
                return state
            if not valid_evidence_provenance(evidence.ref, evidence.observed_at,
                                              evidence.request_started_at, evidence.query_scope, self.clock()):
                return state
            previous_observed_at = attempt.get("last_observed_at")
            if previous_observed_at and evidence.observed_at < datetime.fromisoformat(previous_observed_at):
                return state
            qty, amount = evidence.cumulative_quantity, evidence.cumulative_amount
            old_qty, old_amount = attempt["observed_quantity"], Decimal(attempt["observed_amount"])
            if attempt.get("evidence_conflict"):
                return state
            invalid = (qty > attempt["quantity"] or amount < old_amount
                       or (qty > 0 and amount <= 0) or (qty == 0 and amount != 0)
                       or (qty == old_qty and amount != old_amount)
                       or (attempt["state"] in TERMINAL_STATES and qty != old_qty)
                       or (applied_quantity is not None and (applied_quantity > qty or applied_quantity < attempt["applied_quantity"])))
            if qty < old_qty:
                return state
            if invalid:
                attempt["state"] = OrderState.BLOCKED_UNKNOWN.value
                attempt["status"] = attempt["state"]
                attempt["reason_code"] = "conflicting_evidence"
                attempt["evidence_conflict"] = True
                attempt["version"] += 1
                return state
            attempt["observed_quantity"], attempt["observed_amount"] = qty, str(amount)
            attempt["last_observed_at"] = evidence.observed_at.isoformat()
            attempt["request_started_at"] = evidence.request_started_at.isoformat()
            attempt["query_scope"] = dict(evidence.query_scope)
            if applied_quantity is not None:
                attempt["applied_quantity"] = applied_quantity
            final = evidence.complete and evidence.supported_finality and evidence.state.value in TERMINAL_STATES
            # 어댑터가 종결이라고 표시해도 수량 보존을 독립 확인한다.
            if evidence.state is OrderState.FINAL_FILLED:
                final = (final and qty == attempt["quantity"] and evidence.remaining_quantity == 0
                         and evidence.cancelled_quantity == 0 and evidence.rejected_quantity == 0)
            elif evidence.state is OrderState.FINAL_CANCELLED:
                final = (final and evidence.remaining_quantity == 0 and evidence.cancelled_quantity is not None
                         and qty + evidence.cancelled_quantity == attempt["quantity"] and evidence.rejected_quantity == 0)
            elif evidence.state is OrderState.FINAL_REJECTED:
                final = final and qty == 0 and evidence.rejected_quantity == attempt["quantity"]
            elif evidence.state is OrderState.FINAL_EXPIRED:
                # 이번 단계에서는 고정된 만료 계약을 지원하지 않는다.
                final = False
            if final:
                attempt["state"] = evidence.state.value
                if attempt["applied_quantity"] == qty:
                    attempt["reserved_quantity"] = 0
                    attempt["reserved_cash"] = "0"
            elif attempt["state"] not in TERMINAL_STATES:
                attempt["state"] = (OrderState.PARTIAL.value if qty else OrderState.RECONCILING.value)
            attempt["status"] = attempt["state"]
            attempt["version"] += 1
            attempt["reason_code"] = evidence.reason
            accepted = True
            return state

        await self.owner.mutate(f"reconcile:{attempt_id}:{uuid4().hex}", reduce)
        return accepted

    def replacement_quantity(self, intent_id: str) -> int:
        return _replacement(self.owner.state, intent_id)
