"""식별된 체결의 입력 계약과 실제 DB 쓰기 상태. 원시 오류/자격증명은 담지 않는다."""
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
import hashlib
import json
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class ExecutionIdentity:
    account_scope: str
    order_date: date
    kis_order_no: str
    execution_id: str

    def __post_init__(self):
        for value in (self.account_scope, self.kis_order_no, self.execution_id):
            if not isinstance(value, str) or not value.strip() or "\x00" in value:
                raise ValueError("invalid_execution_identity")
        if type(self.order_date) is not date:
            raise ValueError("invalid_order_date")
        if len(self.kis_order_no) > 20:
            raise ValueError("invalid_order_number")


@dataclass(frozen=True)
class ExecutionWriteReceipt:
    status: str
    execution_id: str
    reason: str = ""


@dataclass(frozen=True)
class ExecutionBatch:
    identity: ExecutionIdentity
    signature: str
    payload: str
    predecessor: str | None = None


def execution_key(scope, execution_id):
    return json.dumps([scope, execution_id], separators=(",", ":"))


def execution_datetime(value):
    if value is None:
        return datetime.now(ZoneInfo("Asia/Seoul")).replace(tzinfo=None)
    if not isinstance(value, datetime):
        raise ValueError("invalid_execution_time")
    if value.tzinfo is not None:
        return value.astimezone(ZoneInfo("Asia/Seoul")).replace(tzinfo=None)
    return value


def decimal_text(value):
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


class ExecutionPayloadError(ValueError):
    """보존된 실행 batch의 손상. 누락된 실행으로 재해석하지 않는다."""


def execution_payload_digest(record):
    payload = {key: value for key,value in record.items() if key != "payload_digest"}
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode()).hexdigest()


def validate_execution_record(record):
    try:
        if not isinstance(record, dict) or record.get("payload_digest") != execution_payload_digest(record):
            raise ExecutionPayloadError("execution_payload_digest_conflict")
        if not {"signature", "facts", "summary", "event", "predecessor"}.issubset(record):
            raise ExecutionPayloadError("execution_payload_fields_missing")
    except (TypeError, ValueError, RecursionError):
        raise ExecutionPayloadError("execution_payload_corrupt") from None
    return record


def validate_batch(batch):
    data = validate_execution_record(json.loads(batch.payload))
    facts = data["facts"]
    encoded = json.dumps(facts, sort_keys=True, separators=(",", ":"), allow_nan=False)
    if hashlib.sha256(encoded.encode()).hexdigest() != batch.signature or data["signature"] != batch.signature:
        raise ValueError("execution_payload_signature_conflict")
    identity = batch.identity
    if (facts["account_scope"],facts["order_date"],facts["kis_order_no"],facts["execution_id"]) != (
            identity.account_scope,identity.order_date.isoformat(),identity.kis_order_no,identity.execution_id):
        raise ValueError("execution_payload_identity_conflict")
    if any(data["event"].get(key) != value for key,value in facts.items()):
        raise ValueError("execution_event_fact_conflict")
    if data["summary"]["id"] != facts["trade_id"] or data["summary"]["symbol"] != facts["symbol"] or data["predecessor"] != batch.predecessor:
        raise ValueError("execution_summary_identity_conflict")
    return data



# Context/review enrichment is mutable; accounting is the latest sealed projection.
_EXECUTION_ACCOUNTING_FIELDS = (
    "id", "symbol", "name", "entry_time", "entry_price", "entry_quantity",
    "entry_reason", "entry_reasons", "entry_strategy", "entry_signal_score",
    "score_breakdown", "entry_tags", "exit_time", "exit_price", "exit_quantity",
    "exit_reason", "exit_type", "pnl", "pnl_pct", "holding_minutes", "created_at",
)


def validate_trade_execution_state(trade):
    """Validate registry identity/chain and its authoritative accounting projection."""
    try:
        records = trade.get("execution_records", {})
        if not isinstance(records, dict):
            raise ValueError("invalid_execution_registry")
        predecessor, scope, latest = None, None, None
        for key, record in records.items():
            validate_execution_record(record)
            facts = record["facts"]
            identity = ExecutionIdentity(facts["account_scope"], date.fromisoformat(facts["order_date"]),
                                         facts["kis_order_no"], facts["execution_id"])
            if key != execution_key(identity.account_scope, identity.execution_id):
                raise ValueError("execution_registry_key_conflict")
            if record["predecessor"] != predecessor:
                raise ValueError("execution_registry_chain_conflict")
            if scope is not None and scope != identity.account_scope:
                raise ValueError("execution_registry_scope_conflict")
            if facts["trade_id"] != trade["id"] or facts["symbol"] != trade["symbol"]:
                raise ValueError("execution_registry_trade_conflict")
            validate_batch(ExecutionBatch(identity, record["signature"], json.dumps(record), predecessor))
            predecessor, scope, latest = key, identity.account_scope, record["summary"]
        if latest is not None:
            def projection(source):
                values = {field: source[field] for field in _EXECUTION_ACCOUNTING_FIELDS}
                return json.dumps(values, sort_keys=True, separators=(",", ":"), allow_nan=False)
            if projection(trade) != projection(latest):
                raise ValueError("execution_accounting_projection_conflict")
    except (KeyError, TypeError, ValueError, RecursionError):
        raise ExecutionPayloadError("execution_trade_state_corrupt") from None
    return trade


def execution_signature(identity, *, trade_id, symbol, side, quantity, price,
                        reason, strategy, exit_type="", execution_time=None,
                        avg_entry_price=None):
    if not isinstance(identity, ExecutionIdentity):
        raise ValueError("invalid_execution_identity")
    if not isinstance(trade_id, str) or not trade_id or not isinstance(symbol, str) or not symbol:
        raise ValueError("invalid_trade_identity")
    value = Decimal(str(price))
    if type(quantity) is not int or quantity <= 0 or not value.is_finite() or value <= 0:
        raise ValueError("invalid_execution_values")
    facts = dict(account_scope=identity.account_scope, order_date=identity.order_date.isoformat(),
                 kis_order_no=identity.kis_order_no, execution_id=identity.execution_id,
                 trade_id=trade_id, symbol=symbol, side=side, quantity=quantity,
                 price=decimal_text(value), reason=reason, strategy=strategy,
                 exit_type=exit_type, execution_time=(execution_datetime(execution_time).isoformat()
                                                     if execution_time is not None else None),
                 avg_entry_price=decimal_text(Decimal(str(avg_entry_price))) if avg_entry_price is not None else None)
    encoded = json.dumps(facts, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode()).hexdigest(), facts


EXECUTION_SCHEMA_SQL = """
ALTER TABLE trades ADD COLUMN IF NOT EXISTS execution_scope TEXT;
ALTER TABLE trades ADD COLUMN IF NOT EXISTS execution_head TEXT;
ALTER TABLE trade_events ADD COLUMN IF NOT EXISTS account_scope TEXT;
ALTER TABLE trade_events ADD COLUMN IF NOT EXISTS order_date DATE;
ALTER TABLE trade_events ADD COLUMN IF NOT EXISTS execution_id TEXT;
ALTER TABLE trade_events ADD COLUMN IF NOT EXISTS execution_signature TEXT;
ALTER TABLE trade_events ADD COLUMN IF NOT EXISTS execution_payload_digest TEXT;
CREATE UNIQUE INDEX IF NOT EXISTS idx_te_execution_identity
ON trade_events(account_scope, execution_id) WHERE execution_id IS NOT NULL;
DO $$ BEGIN
 IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='trade_events_execution_identity_check'
                AND conrelid='trade_events'::regclass) THEN
 ALTER TABLE trade_events ADD CONSTRAINT trade_events_execution_identity_check CHECK (
   (execution_id IS NULL AND account_scope IS NULL AND order_date IS NULL AND execution_signature IS NULL)
   OR (execution_id IS NOT NULL AND length(execution_id)>0 AND account_scope IS NOT NULL
       AND length(account_scope)>0 AND order_date IS NOT NULL AND kis_order_no IS NOT NULL
       AND length(kis_order_no)>0 AND execution_signature IS NOT NULL
       AND length(execution_signature)=64 AND market='KR' AND quantity>0 AND price>0));
 END IF;
 IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='trade_events_execution_payload_check'
                AND conrelid='trade_events'::regclass) THEN
 ALTER TABLE trade_events ADD CONSTRAINT trade_events_execution_payload_check CHECK (
   (execution_id IS NULL AND execution_payload_digest IS NULL)
   OR (execution_id IS NOT NULL AND execution_payload_digest IS NOT NULL
       AND length(execution_payload_digest)=64));
 END IF;
END $$;
"""
