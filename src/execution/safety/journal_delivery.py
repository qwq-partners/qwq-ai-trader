"""경제 체결 outbox의 DB 전용 멱등 원장 전달. 기존 분석 projection은 별도다."""
from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass, field
from decimal import Decimal
import hashlib
import json
from uuid import uuid4

from .application import FillObservation
from .store import encode_state


def _digest(payload: str) -> str:
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ExecutionEnvelope:
    execution_key: str
    payload_json: str = field(repr=False)
    payload_digest: str = field(init=False)
    sink_schema_version: int = field(default=1, init=False)

    def __post_init__(self):
        if type(self.execution_key) is not str or type(self.payload_json) is not str:
            raise ValueError("invalid execution envelope")
        row = json.loads(self.payload_json)
        if type(row) is not dict or encode_state(row) != self.payload_json:
            raise ValueError("noncanonical execution payload")
        required = {"source_version", "order_key", "attempt_id", "intent_id", "observation",
                    "quantity", "amount", "estimated_fee", "fee_basis", "realized_pnl",
                    "before_position", "after_position", "portfolio", "risk"}
        if not required <= row.keys() or row.get("kind", "fill") != "fill":
            raise ValueError("unsupported execution event")
        if "status" in row or "delivery" in row:
            raise ValueError("delivery metadata is not event content")
        if type(row["source_version"]) is not int or row["source_version"] <= 0:
            raise ValueError("missing execution ordering evidence")
        observation = FillObservation(**row["observation"])
        if observation.observation_id != self.execution_key or observation.order_key != row["order_key"]:
            raise ValueError("execution identity mismatch")
        if type(row["quantity"]) is not int or not 0 < row["quantity"] <= observation.cumulative_quantity:
            raise ValueError("invalid execution increment")
        for name in ("amount", "estimated_fee", "realized_pnl"):
            if type(row[name]) is not str:
                raise ValueError("invalid execution amount")
            amount = Decimal(row[name])
            if not amount.is_finite() or (name != "realized_pnl" and amount < 0):
                raise ValueError("invalid execution amount")
        if not 0 < Decimal(row["amount"]) <= observation.cumulative_amount:
            raise ValueError("invalid execution amount")
        object.__setattr__(self, "payload_digest", _digest(self.payload_json))

    @classmethod
    def from_outbox(cls, key: str, row: dict) -> ExecutionEnvelope:
        if type(row) is not dict:
            raise ValueError("invalid outbox row")
        return cls(key, encode_state({name: value for name, value in row.items()
                                      if name not in {"status", "delivery"}}))


@dataclass(frozen=True)
class SinkReceipt:
    execution_key: str
    payload_digest: str
    sink_schema_version: int = 1
    durable_status: str = "committed"

    def __post_init__(self):
        for value in (self.execution_key, self.payload_digest):
            if (type(value) is not str or len(value) != 64
                    or any(character not in "0123456789abcdef" for character in value)):
                raise ValueError("invalid execution receipt identity")
        if type(self.sink_schema_version) is not int or self.sink_schema_version != 1:
            raise ValueError("unsupported execution receipt schema")
        if self.durable_status != "committed":
            raise ValueError("execution receipt is not durable")


class PostgresExecutionJournal:
    """기존 TradeStorage pool의 전용 event 원장. 연결/DDL/JSON fallback은 하지 않는다."""

    def __init__(self, pool):
        if pool is None:
            raise RuntimeError("execution journal database unavailable")
        self.pool = pool

    @staticmethod
    def _receipt(row) -> SinkReceipt:
        payload = row["payload"]
        if type(payload) is str:
            payload = json.loads(payload)
        event = ExecutionEnvelope(row["execution_key"], encode_state(payload))
        receipt = SinkReceipt(row["execution_key"], row["payload_digest"], row["sink_schema_version"])
        if event.payload_digest != receipt.payload_digest:
            raise ValueError("execution journal payload conflict")
        return receipt

    async def lookup(self, execution_key: str) -> SinkReceipt | None:
        async with self.pool.acquire() as connection:
            row = await connection.fetchrow(
                "SELECT execution_key, payload_digest, sink_schema_version, payload "
                "FROM execution_journal WHERE execution_key = $1", execution_key)
        return None if row is None else self._receipt(row)

    async def apply_once(self, event: ExecutionEnvelope) -> SinkReceipt:
        if type(event) is not ExecutionEnvelope:
            raise ValueError("execution envelope required")
        async with self.pool.acquire() as connection:
            async with connection.transaction(isolation="read_committed"):
                await connection.execute(
                    "INSERT INTO execution_journal "
                    "(execution_key, payload_digest, sink_schema_version, payload) "
                    "VALUES ($1, $2, $3, $4::jsonb) "
                    "ON CONFLICT (execution_key) DO NOTHING",
                    event.execution_key, event.payload_digest, event.sink_schema_version, event.payload_json)
                row = await connection.fetchrow(
                    "SELECT execution_key, payload_digest, sink_schema_version, payload "
                    "FROM execution_journal WHERE execution_key = $1", event.execution_key)
                if row is None:
                    raise ValueError("execution journal write unconfirmed")
                receipt = self._receipt(row)
                if receipt.payload_digest != event.payload_digest:
                    raise ValueError("execution key already has different payload")
        # transaction context가 정상 commit한 뒤에만 완료 영수증을 반환한다.
        return receipt


@dataclass(frozen=True)
class DrainReceipt:
    delivered: int
    pending: int
    reason: str


class OutboxDispatcher:
    """sink I/O는 owner lock 밖, 정확한 ACK 반영만 owner.mutate 안에서 한다."""

    def __init__(self, owner, sink):
        self.owner, self.sink = owner, sink
        self._drain_lock = asyncio.Lock()

    @staticmethod
    def _events(state: dict, version: int) -> list[ExecutionEnvelope]:
        events, seen = [], set()
        for key, row in state.get("outbox", {}).items():
            if type(row) is dict and row.get("kind") == "protection_decision":
                continue
            event = ExecutionEnvelope.from_outbox(key, row)
            source = json.loads(event.payload_json)["source_version"]
            if source > version or source in seen or row.get("status") not in {"pending", "delivered"}:
                raise ValueError("invalid outbox ordering or status")
            if row["status"] == "delivered":
                OutboxDispatcher._check_receipt(event, SinkReceipt(**row["delivery"]))
            seen.add(source)
            events.append(event)
        return sorted(events, key=lambda event: json.loads(event.payload_json)["source_version"])

    @staticmethod
    def _check_receipt(event: ExecutionEnvelope, receipt: SinkReceipt) -> None:
        if (type(receipt) is not SinkReceipt or receipt.execution_key != event.execution_key
                or receipt.payload_digest != event.payload_digest
                or receipt.sink_schema_version != event.sink_schema_version
                or receipt.durable_status != "committed"):
            raise ValueError("execution receipt mismatch")

    def _pending(self) -> int:
        return sum(type(row) is not dict or (row.get("kind") != "protection_decision"
                   and row.get("status") != "delivered")
                   for row in self.owner.state.get("outbox", {}).values())

    async def _ack(self, event: ExecutionEnvelope, receipt: SinkReceipt) -> bool:
        self._check_receipt(event, receipt)
        newly_delivered = False

        def reduce(state):
            nonlocal newly_delivered
            # 새 commit ID로 항상 현재 payload를 검사한다. mutate의 중복 ID 단락에 맡기지 않는다.
            current = state.get("outbox", {}).get(event.execution_key)
            if ExecutionEnvelope.from_outbox(event.execution_key, current).payload_digest != event.payload_digest:
                raise ValueError("outbox changed before acknowledgment")
            events = self._events(state, self.owner.version)
            if current["status"] == "delivered":
                return state
            current.update(status="delivered", delivery=asdict(receipt))
            newly_delivered = True
            body = json.loads(event.payload_json)
            order_key = body["order_key"]
            relevant = [item for item in events if json.loads(item.payload_json)["order_key"] == order_key]
            # 모든 경제 이벤트 ACK와 누적 cursor를 함께 확인해야 늦은 ACK가 최신 체결을 해제하지 않는다.
            complete = all(state["outbox"][item.execution_key]["status"] == "delivered" for item in relevant)
            quantity, amount = 0, Decimal("0")
            latest = None
            for item in relevant:
                row = json.loads(item.payload_json)
                quantity += row["quantity"]
                amount += Decimal(row["amount"])
                latest = FillObservation(**row["observation"])
                if quantity != latest.cumulative_quantity or amount != latest.cumulative_amount:
                    complete = False
            cursor = state.get("cursors", {}).get(order_key)
            if cursor is not None and latest is not None:
                matches = (cursor.get("identity") == latest.identity and cursor.get("quantity") == quantity
                           and Decimal(cursor["amount"]) == amount
                           and Decimal(cursor["fee"]) == latest.cumulative_fee)
                if complete and matches:
                    cursor["journal_pending"] = False
            return state

        await self.owner.mutate("journal-ack:" + event.execution_key + ":" + uuid4().hex, reduce)
        return newly_delivered

    async def drain(self, *, limit: int = 100) -> DrainReceipt:
        if type(limit) is not int or limit <= 0:
            raise ValueError("drain limit must be a positive integer")
        async with self._drain_lock:
            delivered = 0
            for _ in range(limit):
                if not self.owner.healthy:
                    return DrainReceipt(delivered, self._pending(), "owner_unhealthy")
                state = self.owner.state
                try:
                    events = self._events(state, self.owner.version)
                    event = next((item for item in events
                                  if state["outbox"][item.execution_key]["status"] == "pending"), None)
                except Exception:
                    return DrainReceipt(delivered, self._pending(), "outbox_invalid")
                if event is None:
                    return DrainReceipt(delivered, self._pending(), "complete")
                try:
                    receipt = await self.sink.lookup(event.execution_key)
                    if receipt is None:
                        receipt = await self.sink.apply_once(event)
                    self._check_receipt(event, receipt)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    return DrainReceipt(delivered, self._pending(), "sink_unconfirmed")
                try:
                    newly_delivered = await self._ack(event, receipt)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    return DrainReceipt(delivered, self._pending(), "owner_ack_unconfirmed")
                delivered += int(newly_delivered)
            return DrainReceipt(delivered, self._pending(), "limit")
