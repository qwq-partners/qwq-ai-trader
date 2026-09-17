"""보호 전용 재생 증거. 과거 이력이 없는 보유를 추정 등록하지 않는다."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
import hashlib

from .application import FillDelta, FillObservation
from .economics import _decode_position
from .protection import decode_protection, quote_protection, reduce_protection
from .store import encode_state
from ...strategies.exit_manager import REGIME_EXIT_PARAMS


ROOT = "protection_replay"


def digest(value):
    return hashlib.sha256(encode_state({"value": value}).encode()).hexdigest()


@dataclass(frozen=True)
class RecoveryReceipt:
    operation_id: str
    status: str
    reason: str
    committed_version: int


def _scope(state, symbol):
    """다른 종목 및 lifecycle 예약 변화와 독립적인 보호/경제 연속성."""
    protection = deepcopy(state["protection"])
    for name in ("states", "entry_times", "degraded", "pending_owners"):
        protection[name] = {key: value for key, value in protection[name].items() if key == symbol}
    protection["orders"] = {key: value for key, value in protection["orders"].items()
                            if value["symbol"] == symbol}
    for name in ("exit_exempt", "integrity_reset_symbols"):
        protection[name] = [value for value in protection[name] if value == symbol]
    return {
        "protection": protection,
        "position": deepcopy(state["portfolio"]["positions"].get(symbol)),
        "lots": {key: {field: deepcopy(value) for field, value in row.items()
                       if field != "initial_r_journal_pending"}
                 for key, row in state["lots"].items() if row["symbol"] == symbol},
        "cursors": {key: {field: deepcopy(value) for field, value in row.items() if field != "journal_pending"}
                    for key, row in state.get("cursors", {}).items()
                    if row["identity"]["symbol"] == symbol},
    }


def _append(before, after, symbol, payload, version, now):
    # 적용 시각이며 거래소 체결 시각으로 주장하지 않는다.
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("보호 증거에는 aware 시각이 필요합니다")
    history = after.setdefault(ROOT, {}).setdefault(symbol, {"events": [], "tail": ""})
    event = {"symbol": symbol, "source_version": version, "applied_at": now.isoformat(),
             "before": _scope(before, symbol), "after": _scope(after, symbol),
             "policy_code": digest(REGIME_EXIT_PARAMS), "previous": history["tail"], **payload}
    # degraded quote는 보호 상태를 변경하지 않는다. 모든 가격/시각/입력은 보존하되
    # 동일 경제·보호 snapshot 두 벌을 매 quote마다 복제하지 않는다.
    # 상태가 실제 변했다면 축약하지 않고 기존 전체 snapshot 형식을 유지한다.
    if event["kind"] == "quote" and digest(event["before"]) == digest(event["after"]):
        event["scope_digest"] = digest(event.pop("before"))
        event.pop("after")
    event["digest"] = digest(event)
    history["events"].append(event)
    history["tail"] = event["digest"]


def capture_fill(before, after, observation, delta, *, fill_kind, intent_id, status, version, now):
    if status != "degraded" and observation.symbol not in before["protection"]["degraded"]:
        return
    # coordinator가 직후 설치하는 cursor를 예측하되 실제 소유 root는 변경하지 않는다.
    projected = deepcopy(after)
    projected.setdefault("cursors", {})[observation.order_key] = {
        "identity": observation.identity, "quantity": observation.cumulative_quantity,
        "amount": str(observation.cumulative_amount), "fee": str(observation.cumulative_fee),
        "protection_status": status, "journal_pending": True,
    }
    _append(before, projected, observation.symbol, {
        "kind": "fill", "observation": observation.to_dict(),
        "observation_id": observation.observation_id,
        "delta": {"quantity": delta.quantity, "amount": str(delta.amount), "fee": str(delta.fee)},
        "fill_kind": fill_kind, "intent_id": intent_id,
    }, version, now)
    after[ROOT] = projected[ROOT]


def capture_quote(before, after, symbol, *, price, market_data, intent_id, command_id, decision,
                  version, now, provenance=None):
    if symbol not in before["protection"]["degraded"]:
        return
    _append(before, after, symbol, {
        "kind": "quote", "price": str(price), "market_data": deepcopy(market_data),
        "intent_id": intent_id, "command_id": command_id,
        "decision": None if decision is None else list(decision),
        "provenance": deepcopy(provenance),
    }, version, now)


def validate_fill_evidence(previous, candidate, observation):
    """새 root 허용은 현재 관측의 단일 append에만 한정한다."""
    old, new = previous.get(ROOT, {}), candidate.get(ROOT, {})
    symbol = observation.symbol
    if (symbol not in previous["protection"].get("degraded", {})
            and symbol not in candidate["protection"].get("degraded", {})
            and digest(old) == digest(new)):
        return
    if type(new) is not dict or set(new) != set(old) | {symbol}:
        raise ValueError("보호 증거 종목 삭제/추가 금지")
    for key in old.keys() - {symbol}:
        if digest(old[key]) != digest(new[key]):
            raise ValueError("다른 종목 보호 증거 변경 금지")
    prior = old.get(symbol, {"events": [], "tail": ""})
    current = new[symbol]
    if (len(current["events"]) != len(prior["events"]) + 1
            or digest(current["events"][:-1]) != digest(prior["events"])):
        raise ValueError("기존 보호 증거 삭제/덮어쓰기 금지")
    event = current["events"][-1]
    raw = {key: value for key, value in event.items() if key != "digest"}
    if (event["kind"] != "fill" or event["observation"] != observation.to_dict()
            or event["previous"] != prior["tail"] or event["digest"] != digest(raw)
            or current["tail"] != event["digest"]):
        raise ValueError("보호 증거와 현재 체결 불일치")


def _pending_known(dto, symbol, state):
    row = dto["states"].get(symbol)
    owner = dto["pending_owners"].get(symbol)
    if row is None or row["pending_stage"] is None:
        return owner is None
    intent = state["intents"].get(owner)
    return bool(intent and intent["symbol"] == symbol and intent["side"] == "sell"
                and intent["target_quantity"] == row["pending_target_qty"])


def _replay(state, symbol):
    if any(row["symbol"] == symbol for row in state.get("protection_quote_admissions", {}).values()):
        raise ValueError("unresolved_quote_admission")
    history = state.get(ROOT, {}).get(symbol)
    if not history or not history["events"]:
        raise ValueError("missing_protection_history")
    events, previous, last_version, prior_scope = [], "", -1, None
    for event in history["events"]:
        raw = {key: value for key, value in event.items() if key != "digest"}
        if (event["previous"] != previous or digest(raw) != event["digest"]
                or event["symbol"] != symbol or event["source_version"] <= last_version):
            raise ValueError("protection_history_gap")
        previous, last_version = event["digest"], event["source_version"]
        if "scope_digest" in event:
            if (event["kind"] != "quote" or "before" in event or "after" in event
                    or prior_scope is None or event["scope_digest"] != digest(prior_scope)):
                raise ValueError("unrecorded_protection_input")
            # 읽기 전용 재생 view만 확장한다. 원본 durable event/hash는 변경하지 않는다.
            resolved = {**event, "before": prior_scope, "after": prior_scope}
        else:
            resolved = event
        prior_scope = resolved["after"]
        events.append(resolved)
    if history["tail"] != previous or digest(events[-1]["after"]) != digest(_scope(state, symbol)):
        raise ValueError("current_protection_evidence_mismatch")
    start = None
    for index, event in enumerate(events):
        snapshot = event["before"]
        dto, position = snapshot["protection"], snapshot["position"]
        guarded = dto["states"].get(symbol)
        if symbol not in dto["degraded"] and (
            (position is None and guarded is None and event["kind"] == "fill"
             and event["fill_kind"] == "initial_entry")
            or (position is not None and guarded is not None
                and guarded["remaining_quantity"] == position["quantity"])
        ):
            start = index
    if start is None:
        raise ValueError("missing_healthy_anchor")
    candidate = deepcopy(events[start]["before"]["protection"])
    if not _pending_known(candidate, symbol, state):
        raise ValueError("unknown_pending_owner")
    previous_after = None
    replayed_orders = set()
    for event in events[start:]:
        if previous_after is not None and digest(previous_after) != digest(event["before"]):
            raise ValueError("unrecorded_protection_input")
        if event["policy_code"] != digest(REGIME_EXIT_PARAMS):
            raise ValueError("changed_protection_policy")
        now = datetime.fromisoformat(event["applied_at"])
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("invalid_evidence_time")
        if event["kind"] == "fill":
            observation = FillObservation(**event["observation"])
            replayed_orders.add(observation.order_key)
            inbox = state["inbox"].get(event["observation_id"])
            if (observation.observation_id != event["observation_id"] or not inbox
                    or inbox["status"] != "APPLIED" or inbox["observation"] != observation.to_dict()):
                raise ValueError("missing_applied_fill")
            row = event["delta"]
            before, after = event["before"]["position"], event["after"]["position"]
            candidate, status = reduce_protection(
                candidate, before=None if before is None else _decode_position(before),
                after=None if after is None else _decode_position(after), observation=observation,
                delta=FillDelta(row["quantity"], Decimal(row["amount"]), Decimal(row["fee"])),
                fill_kind=event["fill_kind"], intent_id=event["intent_id"], now=now,
            )
            if status == "degraded":
                raise ValueError("protection_replay_failed")
            if after is None:
                raise ValueError("closed_lifecycle")
        elif event["kind"] == "quote":
            candidate, decision = quote_protection(
                candidate, symbol=symbol, price=Decimal(event["price"]), now=now,
                market_data=event["market_data"], intent_id=event["intent_id"],
            )
            # degraded 구간의 새 과거 결정은 원래 durable 결정/intent가 없다.
            if decision is not None:
                recorded = state["outbox"].get(event["command_id"])
                if (list(decision) != event["decision"] or not recorded
                        or recorded.get("decision") != list(decision)
                        or recorded.get("intent_id") != event["intent_id"]
                        or not _pending_known(candidate, symbol, state)):
                    raise ValueError("unrecorded_historical_decision")
        else:
            raise ValueError("unsupported_protection_input")
        previous_after = event["after"]
    protected = decode_protection(candidate, clock=lambda: now).get_state(symbol)
    position = state["portfolio"]["positions"].get(symbol)
    if (protected is None or position is None or protected.remaining_quantity != position["quantity"]
            or protected.entry_price != Decimal(position["avg_price"])
            or not _pending_known(candidate, symbol, state)):
        raise ValueError("replayed_protection_mismatch")
    return candidate, replayed_orders


def reduce_repair(state, operation_id, symbol, *, expected_version, state_version):
    request = {"kind": "protection_repair", "operation_id": operation_id,
               "symbol": symbol, "expected_version": expected_version}
    request_digest = digest(request)
    receipts = state.setdefault("recovery_receipts", {})
    previous = receipts.get(operation_id)
    if previous is not None:
        if previous["request_digest"] != request_digest:
            raise ValueError("recovery_operation_id_conflict")
        return state
    status, reason = "BLOCKED", "stale_execution_version"
    if state_version == expected_version:
        try:
            if symbol not in state["protection"]["degraded"]:
                raise ValueError("protection_not_degraded")
            candidate, replayed_orders = _replay(state, symbol)
            # 정확한 symbol만 게시하고 경제/예약/원장/시작 장벽은 변경하지 않는다.
            for name in ("states", "entry_times", "degraded", "pending_owners"):
                state["protection"][name].pop(symbol, None)
                if symbol in candidate[name]:
                    state["protection"][name][symbol] = candidate[name][symbol]
            for key, row in candidate["orders"].items():
                state["protection"]["orders"][key] = row
            # 현재 cursor만 갱신. 과거 inbox/outbox receipt를 ready로 소급하지 않는다.
            for key in replayed_orders:
                row = candidate["orders"][key]
                cursor = state.get("cursors", {}).get(key)
                if cursor and cursor["quantity"] == row["cumulative_quantity"]:
                    cursor["protection_status"] = "exempt" if symbol in candidate["exit_exempt"] else "ready"
            status, reason = "APPLIED", ""
        except (ValueError, KeyError, TypeError) as exc:
            reason = str(exc)
    receipts[operation_id] = {"request_digest": request_digest, "request": request,
                              "status": status, "reason": reason, "committed_version": state_version + 1}
    return state
