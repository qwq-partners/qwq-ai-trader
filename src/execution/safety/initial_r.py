"""최초 등록 손절 증거와 검증된 최종 대금에 귀속하는 초기 R."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict
from datetime import datetime
from decimal import Decimal

from .application import FillObservation
from .lifecycle import OrderEvidence, OrderRef, OrderState, TERMINAL_STATES, valid_evidence_provenance
from .protection import decode_protection
from .protection_recovery import digest
from ...strategies.exit_manager import REGIME_EXIT_PARAMS, INTRADAY_CRASH_PARAMS


def _stamp(now):
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("initial_r_requires_aware_time")
    return now.isoformat()


def _version(version):
    if type(version) is not int or version <= 0:
        raise ValueError("invalid_initial_r_version")
    return version


def _seal(row):
    row["digest"] = digest(row)
    row["evidence_id"] = row["digest"]
    return row


def _verify(row, evidence_id):
    if (row["evidence_id"] != evidence_id or row["digest"] != evidence_id
            or digest({key: value for key, value in row.items() if key not in {"digest", "evidence_id"}}) != evidence_id):
        raise ValueError("initial_r_evidence_digest_mismatch")
    _version(row["source_version"])


def capture_initial_stop(before, candidate, observation, *, fill_kind, status, version, now):
    """성공한 최초 등록 후보만 증거화한다. 손절 계산 실패는 경제 적용을 취소하지 않는다."""
    key, symbol = observation.order_key, observation.symbol
    if (fill_kind != "initial_entry" or status not in {"ready", "exempt"}
            or observation.side != "BUY" or key in before.get("lots", {})
            or symbol in before["portfolio"]["positions"]
            or key in candidate.get("initial_stop_evidence", {})):
        return
    lot = candidate["lots"][key]
    dto = candidate["protection"]
    if (lot["kind"] != "initial_entry" or lot["lifecycle_id"] != key
            or symbol in dto["degraded"] or symbol not in dto["states"]):
        return
    row = {"kind": "initial_stop", "order_key": key, "lifecycle_id": key,
           "attempt_id": lot["attempt_id"], "intent_id": lot["intent_id"],
           "observation": observation.to_dict(), "observation_id": observation.observation_id,
           "source_version": _version(version), "registered_at": _stamp(now),
           "mode": "initial_r_uncapped", "registration_params": observation.to_dict()["metadata"].get("registration_params", {}),
           "protection_snapshot": deepcopy(dto),
           "policy": {"regime": deepcopy(REGIME_EXIT_PARAMS), "crash": deepcopy(INTRADAY_CRASH_PARAMS)},
           "status": "unmeasured", "reason": "initial_stop_calculation_failed"}
    try:
        manager = decode_protection(dto, clock=lambda: now)
        registered = manager.get_state(symbol)
        if registered.dynamic_stop_pct is not None:
            raise ValueError("unexpected_initial_dynamic_stop")
        # register_position이 실제 보호 state에 적용한 일시 급락 cap은 이미 값에
        # 반영돼 있다. 초기 R은 기존 entry resolver와 같은 원본 등록 고정 SL을 쓴다.
        row["resolve_inputs"] = {"dynamic_stop_pct": None, "fixed_stop_pct": row["registration_params"].get("stop_loss_pct"),
                                 "is_core": registered.is_core, "apply_crash_cap": False}
        decision = manager.resolve_stop(**row["resolve_inputs"])
        row.update(status="measured", reason="", stop_pct=str(decision.stop_pct),
                   source=decision.source, crash_capped=decision.crash_capped)
    except Exception:
        pass
    candidate.setdefault("initial_stop_evidence", {})[key] = _seal(row)


def validate_initial_stop_write_set(before, candidate, observation):
    """기존 증거 불변과 현재 최초 후보에서 계산한 신규 단일 증거만 허용한다."""
    old, new = before.get("initial_stop_evidence", {}), candidate.get("initial_stop_evidence", {})
    if type(new) is not dict or any(key not in new or digest(new[key]) != digest(value) for key, value in old.items()):
        raise ValueError("initial_stop_evidence_is_immutable")
    added = new.keys() - old.keys()
    if not added:
        return
    if added != {observation.order_key}:
        raise ValueError("initial_stop_scope_mismatch")
    row = new[observation.order_key]
    _verify(row, row["evidence_id"])
    expected = deepcopy(candidate)
    expected["initial_stop_evidence"] = deepcopy(old)
    capture_initial_stop(before, expected, observation, fill_kind="initial_entry",
                         status="exempt" if observation.symbol in candidate["protection"]["exit_exempt"] else "ready",
                         version=row["source_version"], now=datetime.fromisoformat(row["registered_at"]))
    if digest(expected.get("initial_stop_evidence", {})) != digest(new):
        raise ValueError("initial_stop_candidate_mismatch")
    event = candidate["outbox"][observation.observation_id]
    if row["source_version"] != event["source_version"]:
        raise ValueError("initial_stop_source_version_mismatch")


def _validate_finality(attempt, evidence, now):
    valid = (type(evidence) is OrderEvidence and evidence.schema_valid and evidence.complete
             and evidence.supported_finality and bool(evidence.source_contract)
             and attempt["kind"] == "submit" and not attempt.get("evidence_conflict")
             and attempt["order_ref"] == evidence.ref.to_dict()
             and (attempt["symbol"], attempt["side"]) == (evidence.symbol, evidence.side)
             and attempt["quantity"] == evidence.order_quantity
             and valid_evidence_provenance(evidence.ref, evidence.observed_at, evidence.request_started_at,
                                           evidence.query_scope, now))
    if evidence.state is OrderState.FINAL_FILLED:
        valid = (valid and evidence.cumulative_quantity == evidence.order_quantity
                 and evidence.remaining_quantity == 0 and evidence.cancelled_quantity == 0
                 and evidence.rejected_quantity == 0)
    elif evidence.state is OrderState.FINAL_CANCELLED:
        valid = (valid and evidence.remaining_quantity == 0 and evidence.cancelled_quantity is not None
                 and evidence.cumulative_quantity + evidence.cancelled_quantity == evidence.order_quantity
                 and evidence.rejected_quantity == 0)
    elif evidence.state is OrderState.FINAL_REJECTED:
        valid = valid and evidence.cumulative_quantity == 0 and evidence.rejected_quantity == evidence.order_quantity
    else:
        valid = False
    if (not valid or attempt["observed_quantity"] != evidence.cumulative_quantity
            or Decimal(attempt["observed_amount"]) != evidence.cumulative_amount
            or (evidence.cumulative_quantity > 0 and evidence.cumulative_amount <= 0)):
        raise ValueError("unverified_finality_evidence")


def capture_finality(state, attempt_id, evidence, *, version, now):
    """lifecycle의 최종성 승인과 같은 commit에서 원본 조회 근거를 보존한다."""
    attempt = state["attempts"][attempt_id]
    _validate_finality(attempt, evidence, now)
    raw = asdict(evidence)
    raw.update(cumulative_amount=str(evidence.cumulative_amount), state=evidence.state.value,
               observed_at=_stamp(evidence.observed_at), request_started_at=_stamp(evidence.request_started_at))
    rows = state.get("accepted_finality_evidence", {})
    for key, previous in rows.items():
        if previous["attempt_id"] == attempt_id and previous["evidence"] == raw:
            _verify(previous, key)
            attempt["accepted_finality_evidence_id"] = key
            return key
    row = _seal({"kind": "accepted_finality", "attempt_id": attempt_id, "intent_id": attempt["intent_id"],
                 "order_key": evidence.ref.key, "evidence": raw,
                 "source_version": _version(version), "accepted_at": _stamp(now)})
    state.setdefault("accepted_finality_evidence", {})[row["evidence_id"]] = row
    attempt["accepted_finality_evidence_id"] = row["evidence_id"]
    return row["evidence_id"]


def _evidence(row):
    raw = deepcopy(row["evidence"])
    raw["ref"] = OrderRef.from_dict(raw["ref"])
    for field in ("observed_at", "request_started_at"):
        raw[field] = datetime.fromisoformat(raw[field])
    return OrderEvidence(**raw)


def _finalize(state, order_key, stop_id, finality_id, state_version, now):
    from .journal_delivery import ExecutionEnvelope, initial_r_execution_key
    lot = state["lots"][order_key]
    stop = state.get("initial_stop_evidence", {})[order_key]
    final = state.get("accepted_finality_evidence", {})[finality_id]
    _verify(stop, stop_id)
    _verify(final, finality_id)
    attempt = state["attempts"][lot["attempt_id"]]
    evidence = _evidence(final)
    _validate_finality(attempt, evidence, now)
    observation = FillObservation(**stop["observation"])
    cursor = state["cursors"][order_key]
    symbol = lot["symbol"]
    intent = state["intents"][lot["intent_id"]]
    if (lot["kind"] != "initial_entry" or lot["lifecycle_id"] != order_key
            or lot["pre_buy_quantity"] != 0 or evidence.side != "buy" or evidence.cumulative_quantity <= 0
            or OrderRef.from_dict(lot["order_ref"]).key != order_key
            or final["order_key"] != order_key or evidence.ref.key != order_key
            or observation.order_key != order_key or observation.symbol != symbol or observation.side != "BUY"
            or stop["order_key"] != order_key or stop["lifecycle_id"] != order_key
            or stop["status"] != "measured" or stop["mode"] != "initial_r_uncapped"
            or final["attempt_id"] != lot["attempt_id"] or stop["attempt_id"] != lot["attempt_id"]
            or stop["intent_id"] != lot["intent_id"] or final["intent_id"] != lot["intent_id"]
            or attempt["intent_id"] != lot["intent_id"] or lot["attempt_id"] not in intent["attempt_ids"]
            or (intent["symbol"], intent["side"]) != (symbol, "buy")
            or stop["source_version"] > state_version or final["source_version"] > state_version
            or attempt.get("accepted_finality_evidence_id") != finality_id
            or attempt["state"] != evidence.state.value
            or lot["quantity"] != evidence.cumulative_quantity or Decimal(lot["amount"]) != evidence.cumulative_amount
            or attempt["applied_quantity"] != evidence.cumulative_quantity
            or cursor["quantity"] != evidence.cumulative_quantity or Decimal(cursor["amount"]) != evidence.cumulative_amount
            or cursor["identity"] != observation.identity
            or attempt["reserved_quantity"] != 0 or Decimal(attempt["reserved_cash"]) != 0):
        raise ValueError("initial_r_scope_or_application_mismatch")
    if any(row.get("parent_attempt_id") == lot["attempt_id"] and row.get("command_status") not in {"not_sent", "rejected"}
           for row in state["attempts"].values()):
        raise ValueError("unresolved_initial_entry_child")
    for sibling_id in intent["attempt_ids"]:
        sibling = state["attempts"][sibling_id]
        if sibling["kind"] == "submit" and (sibling.get("state") not in TERMINAL_STATES
                or sibling.get("evidence_conflict") or sibling["reserved_quantity"] != 0
                or Decimal(sibling["reserved_cash"]) != 0
                or sibling["applied_quantity"] != sibling["observed_quantity"]):
            raise ValueError("unresolved_initial_entry_intent")
    first = state.get("inbox", {}).get(observation.observation_id)
    if not first or first["status"] != "APPLIED" or first["observation"] != observation.to_dict():
        raise ValueError("initial_stop_missing_applied_observation")
    if any(row.get("status") not in {"APPLIED", "SUPERSEDED"}
           and row.get("observation", {}).get("symbol") == symbol
           for row in state.get("inbox", {}).values()):
        raise ValueError("unresolved_initial_entry_inbox")
    if any(row.get("symbol") == symbol for row in state.get("protection_quote_admissions", {}).values()):
        raise ValueError("unresolved_initial_entry_quote")
    if any(key != order_key and row["symbol"] == symbol and row["kind"] == "distinct_add_on"
           and (not row["closed"] or row.get("lifecycle_id") in {None, order_key}) for key, row in state["lots"].items()):
        raise ValueError("ambiguous_initial_entry_addon")
    stop_pct = Decimal(stop["stop_pct"])
    if not stop_pct.is_finite() or stop_pct <= 0:
        raise ValueError("unmeasured_initial_stop")
    amount = evidence.cumulative_amount * stop_pct / Decimal(100)
    if lot["initial_r"] is not None or lot["initial_r_status"] != "pending":
        raise ValueError("initial_r_already_recorded")
    protected = None
    if not lot["closed"]:
        open_entries = [key for key, row in state["lots"].items()
                        if row["symbol"] == symbol and not row["closed"] and row["kind"] == "initial_entry"]
        if open_entries != [order_key] or symbol in state["protection"]["degraded"]:
            raise ValueError("initial_r_protection_lifecycle_mismatch")
        protected = state["protection"]["states"][symbol]
        position = state["portfolio"]["positions"][symbol]
        if (protected["remaining_quantity"] != position["quantity"]
                or protected["initial_risk_amount"] is not None or protected["actual_stop_pct"] is not None):
            raise ValueError("initial_r_protection_conflict")
    entries = []
    for key, row in state["outbox"].items():
        if row.get("kind", "fill") == "fill" and row.get("order_key") == order_key:
            event = ExecutionEnvelope.from_outbox(key, row)
            entries.append((row["source_version"], key, row))
    entries.sort()
    if (not entries or entries[0][1] != observation.observation_id
            or entries[0][0] != stop["source_version"]):
        raise ValueError("initial_stop_fill_history_mismatch")
    qty, paid = 0, Decimal(0)
    for version, key, row in entries:
        obs = FillObservation(**row["observation"])
        qty += row["quantity"]
        paid += Decimal(row["amount"])
        if (version > state_version or obs.identity != observation.identity
                or row["attempt_id"] != lot["attempt_id"] or row["intent_id"] != lot["intent_id"]
                or qty != obs.cumulative_quantity or paid != obs.cumulative_amount):
            raise ValueError("initial_r_fill_history_mismatch")
    if qty != evidence.cumulative_quantity or paid != evidence.cumulative_amount:
        raise ValueError("initial_r_fill_history_incomplete")
    body = {"kind": "initial_r_finalized", "schema": 1, "source_version": state_version + 1,
            "account_scope": evidence.ref.account_scope, "market": evidence.ref.market,
            "order_key": order_key, "lifecycle_id": order_key, "symbol": symbol,
            "attempt_id": lot["attempt_id"], "intent_id": lot["intent_id"],
            "quantity": qty, "amount": str(paid), "initial_r": str(amount), "actual_stop_pct": str(stop_pct),
            "initial_stop_evidence_id": stop_id, "initial_stop_digest": stop["digest"],
            "finality_evidence_id": finality_id, "finality_digest": final["digest"],
            "entry_execution_keys": [key for _, key, _ in entries], "confirmed_at": _stamp(now)}
    key = initial_r_execution_key(body)
    if key in state["outbox"]:
        raise ValueError("initial_r_execution_key_conflict")
    ExecutionEnvelope.from_outbox(key, body)
    # 여기까지 검증이 끝난 뒤 허용된 필드만 변경한다.
    lot.update(initial_r=str(amount), initial_r_status="confirmed", initial_stop_evidence_id=stop_id,
               finality_evidence_id=finality_id, initial_r_journal_pending=True)
    if protected is not None:
        protected.update(initial_risk_amount=str(amount), actual_stop_pct=str(stop_pct))
    state["outbox"][key] = {**body, "status": "pending"}


def reduce_finalize_initial_r(state, operation_id, order_key, *, initial_stop_evidence_id,
                              finality_evidence_id, expected_version, state_version, now):
    request = {"kind": "initial_r_finalize", "operation_id": operation_id, "order_key": order_key,
               "initial_stop_evidence_id": initial_stop_evidence_id, "finality_evidence_id": finality_evidence_id,
               "expected_version": expected_version}
    request_digest = digest(request)
    previous = state.get("recovery_receipts", {}).get(operation_id)
    if previous is not None:
        if previous["request_digest"] != request_digest:
            raise ValueError("recovery_operation_id_conflict")
        return state
    status, reason = "BLOCKED", "stale_execution_version"
    if type(expected_version) is int and expected_version == state_version:
        try:
            _finalize(state, order_key, initial_stop_evidence_id, finality_evidence_id, state_version, now)
            status, reason = "APPLIED", ""
        except (ValueError, KeyError, TypeError, ArithmeticError) as exc:
            reason = str(exc)
    state.setdefault("recovery_receipts", {})[operation_id] = {
        "request": request, "request_digest": request_digest, "status": status,
        "reason": reason, "committed_version": state_version + 1}
    return state
