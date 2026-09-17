"""보호 전용 재생 증거. 과거 이력이 없는 보유를 추정 등록하지 않는다."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
import hashlib
from zoneinfo import ZoneInfo

from .application import FillDelta, FillObservation
from .economics import _decode_position
from .protection import decode_protection, quote_protection, reduce_protection
from .store import encode_state
from ...strategies.exit_manager import REGIME_EXIT_PARAMS, INTRADAY_CRASH_PARAMS
from .risk_transition import IntradayPolicyState, transition_intraday


ROOT = "protection_replay"


def digest(value):
    return hashlib.sha256(encode_state({"value": value}).encode()).hexdigest()


@dataclass(frozen=True)
class RecoveryReceipt:
    operation_id: str
    status: str
    reason: str
    committed_version: int


def _intraday_rule_digest():
    return digest({'rule': 'intraday_transition_v1', 'regime': REGIME_EXIT_PARAMS,
                   'intraday': INTRADAY_CRASH_PARAMS})


@dataclass(frozen=True)
class IntradayPolicyReplayInput:
    """현재 관측이 아닌 원 accepted source/정책 전이를 가리키는 재생 입력."""

    operation_id: str
    request_digest: str
    attempt_sequence: int
    source_version: int
    outcome_digest: str
    before_policy: IntradayPolicyState
    after_policy: IntradayPolicyState
    rule_digest: str
    schema: int = 1
    rule: str = 'intraday_transition_v1'

    def __post_init__(self):
        if type(self.schema) is not int or self.schema != 1 or self.rule != 'intraday_transition_v1':
            raise ValueError('unsupported_intraday_replay_rule')
        if (type(self.operation_id) is not str or not self.operation_id
                or self.operation_id != self.operation_id.strip()):
            raise ValueError('invalid_intraday_replay_operation')
        for value in (self.request_digest, self.outcome_digest, self.rule_digest):
            if type(value) is not str or len(value) != 64 or any(c not in '0123456789abcdef' for c in value):
                raise ValueError('invalid_intraday_replay_digest')
        for value in (self.attempt_sequence, self.source_version):
            if type(value) is not int or value <= 0:
                raise ValueError('invalid_intraday_replay_version')
        if not isinstance(self.before_policy, IntradayPolicyState) or not isinstance(self.after_policy, IntradayPolicyState):
            raise ValueError('invalid_intraday_replay_policy')

    def to_dict(self):
        return {**{name: getattr(self, name) for name in self.__dataclass_fields__
                   if name not in {'before_policy', 'after_policy'}},
                'before_policy': self.before_policy.to_dict(), 'after_policy': self.after_policy.to_dict()}

    @classmethod
    def from_dict(cls, value):
        if type(value) is not dict or set(value) != set(cls.__dataclass_fields__):
            raise ValueError('invalid_intraday_replay_fields')
        result = cls(**{**value, 'before_policy': IntradayPolicyState.from_dict(value['before_policy']),
                        'after_policy': IntradayPolicyState.from_dict(value['after_policy'])})
        if digest(result.to_dict()) != digest(value):
            raise ValueError('noncanonical_intraday_replay_input')
        return result


def _intraday_transition_input(state, fact, envelope):
    """pending capture와 terminal replay에서 공통인 정책 연결을 확인한다."""
    row = state['intraday_policy']['transitions'].get(fact.operation_id)
    if (not row or row['disposition'] != 'applied' or row['version'] != fact.source_version
            or row['outcome_digest'] != fact.outcome_digest
            or row['before'] != fact.before_policy.to_dict() or row['after'] != fact.after_policy.to_dict()
            or fact.outcome_digest != digest(envelope) or envelope['outcome'] != 'success'
            or fact.rule_digest != _intraday_rule_digest()):
        raise ValueError('intraday_replay_policy_crosslink_conflict')
    classified = datetime.fromisoformat(envelope['classified_at'])
    if (classified.tzinfo is None or classified.utcoffset() is None
            or classified.isoformat() != classified.astimezone(ZoneInfo('Asia/Seoul')).isoformat()):
        raise ValueError('invalid_intraday_replay_time')
    return classified


def _validate_intraday_scopes(before, after, symbol, fact, envelope):
    if (set(before) != {'protection', 'position', 'lots', 'cursors'} or set(after) != set(before)
            or any(digest(before[key]) != digest(after[key]) for key in ('position', 'lots', 'cursors'))
            or symbol not in before['protection']['degraded']
            or symbol not in after['protection']['degraded'] or digest(before) == digest(after)):
        raise ValueError('invalid_intraday_replay_scope')
    classified = datetime.fromisoformat(envelope['classified_at'])
    # 원 degraded DTO의 실제 변화부터 검증. 복구된 per-position 상태와 혼동하지 않는다.
    decode_protection(before['protection'], clock=lambda: classified)
    decode_protection(after['protection'], clock=lambda: classified)
    result = transition_intraday(fact.before_policy, before['protection'],
        change_pct=envelope['payload']['change_pct'], classified_at=classified)
    if (result.status != 'applied' or result.state != fact.after_policy
            or digest(result.protection_dto) != digest(after['protection'])):
        raise ValueError('intraday_replay_original_transition_conflict')


def capture_intraday_transition(before, after, *, ticket, envelope, version, now):
    """실제 completion reducer 끝에서 호출. terminal은 coordinator가 같은 commit에 저장한다."""
    from .risk_sources import RefreshTicket, _ticket_dict
    if not isinstance(ticket, RefreshTicket) or ticket.kind != 'intraday_5m':
        raise ValueError('intraday_replay_ticket_required')
    if digest(before.get(ROOT, {})) != digest(after.get(ROOT, {})):
        raise ValueError('intraday_replay_history_already_changed')
    row = after['intraday_policy']['transitions'].get(ticket.operation_id)
    if row and row['disposition'] in {'duplicate_observation', 'older_observation'}:
        if digest(before['protection']) != digest(after['protection']):
            raise ValueError('ignored_intraday_changed_protection')
        return
    source = after['risk_sources']['records'].get(ticket.operation_id)
    if (not row or not source or source['ticket'] != _ticket_dict(ticket) or source['terminal'] is not None
            or source['conflict'] is not None or not ticket.admission_version < version
            or before['intraday_policy']['current'] != row['before']):
        raise ValueError('intraday_replay_pending_source_conflict')
    fact = IntradayPolicyReplayInput(ticket.operation_id, ticket.request_digest, ticket.sequence,
        version, digest(envelope), IntradayPolicyState.from_dict(row['before']),
        IntradayPolicyState.from_dict(row['after']), _intraday_rule_digest())
    classified = _intraday_transition_input(after, fact, envelope)
    if (now.tzinfo is None or now.utcoffset() is None or classified > now
            or classified.date().isoformat() != ticket.business_day):
        raise ValueError('invalid_intraday_replay_time')
    if digest(before['protection']['degraded']) != digest(after['protection']['degraded']):
        raise ValueError('intraday_policy_changed_degraded_ownership')
    # 먼저 모두 검증하고 나서 append: 잘못된 두 번째 종목 때문에 부분 기록하지 않는다.
    symbols = []
    for symbol in before['protection']['degraded']:
        old_scope, new_scope = _scope(before, symbol), _scope(after, symbol)
        if digest(old_scope) == digest(new_scope):
            continue
        _validate_intraday_scopes(old_scope, new_scope, symbol, fact, envelope)
        symbols.append(symbol)
    for symbol in symbols:
        _append(before, after, symbol, {'kind': 'intraday_policy', 'policy_input': fact.to_dict()},
                version, now.astimezone(ZoneInfo('Asia/Seoul')))


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
    # 새 체결의 최초 commit에서만 원 보호 증거를 독립 경제 outbox에 결합한다.
    # event ID/version만으로는 before가 실제로 healthy였는지 증명할 수 없다.
    # 이 필드는 journal의 원 payload에 포함되며 repair/restore/ACK에서 만들지 않는다.
    event = after[ROOT][observation.symbol]['events'][-1]
    outbox = after['outbox'][observation.observation_id]
    if (type(outbox.get('source_version')) is not int
            or outbox['source_version'] != version
            or 'protection_replay_digest' in outbox):
        raise ValueError('original_fill_replay_link_required')
    outbox['protection_replay_digest'] = event['digest']


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
            or current["tail"] != event["digest"]
            or candidate.get('outbox', {}).get(observation.observation_id, {}).get(
                'protection_replay_digest') != event['digest']):
        raise ValueError("보호 증거와 현재 체결 불일치")


def _pending_known(dto, symbol, state):
    row = dto["states"].get(symbol)
    owner = dto["pending_owners"].get(symbol)
    if row is None or row["pending_stage"] is None:
        return owner is None
    intent = state["intents"].get(owner)
    return bool(intent and intent["symbol"] == symbol and intent["side"] == "sell"
                and intent["target_quantity"] == row["pending_target_qty"])


def _require_replay_policy(candidate, recorded):
    """등록 성공/실패의 종목 차이와 별개로 모든 event의 글로벌 정책은 같아야 한다."""
    if any(digest(candidate[key]) != digest(recorded[key]) for key in (
            'schema', 'market', 'config', 'max_holding_days', 'current_regime',
            'intraday_crash_level', 'exit_exempt')):
        raise ValueError('protection_replay_global_policy_gap')


def _require_intraday_completeness(state, events):
    """남아 있는 event만 믿지 않고 독립 정책 원장에서 누락/중복 전이를 찾는다."""
    anchor_version = events[0]['source_version']
    transitions = state.get('intraday_policy', {}).get('transitions', {})
    expected = [(operation, row['version']) for operation, row in
                sorted(transitions.items(), key=lambda pair: pair[1]['version'])
                if row['version'] > anchor_version and row['disposition'] == 'applied'
                and row['before']['level'] != row['after']['level']]
    actual = []
    for event in events:
        if event['kind'] == 'intraday_policy':
            fact = IntradayPolicyReplayInput.from_dict(event['policy_input'])
            if fact.source_version != event['source_version']:
                raise ValueError('intraday_replay_source_crosslink_conflict')
            actual.append((fact.operation_id, fact.source_version))
    if actual != expected:
        raise ValueError('intraday_replay_policy_history_incomplete')


def _replay(state, symbol, *, state_version):
    if any(row["symbol"] == symbol for row in state.get("protection_quote_admissions", {}).values()):
        raise ValueError("unresolved_quote_admission")
    history = state.get(ROOT, {}).get(symbol)
    if not history or not history["events"]:
        raise ValueError("missing_protection_history")
    # kind가 삭제/변환된 이력도 원 source/정책 root 검증을 피하지 못한다.
    # 두 root가 없는 기존 fill/quote 이력은 각 validator가 그대로 허용한다.
    from .risk_sources import validate_risk_sources
    from .intraday_owner import validate_intraday_policy
    validate_risk_sources(state, state_version)
    validate_intraday_policy(state, state_version)
    events, previous, last_version, prior_scope = [], "", -1, None
    for event in history["events"]:
        raw = {key: value for key, value in event.items() if key != "digest"}
        if (event["previous"] != previous or digest(raw) != event["digest"]
                or event["symbol"] != symbol or type(event['source_version']) is not int
                or not last_version < event['source_version'] <= state_version):
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
        if event['kind'] == 'fill':
            # 검사 범위를 정하는 anchor version은 재생 이력 자체가 아니라
            # 실제 경제 체결 commit의 독립 outbox 기록과도 일치해야 한다.
            recorded_fill = state['outbox'].get(event['observation_id'])
            if (not recorded_fill
                    or type(recorded_fill.get('source_version')) is not int
                    or recorded_fill['source_version'] != event['source_version']):
                raise ValueError('protection_replay_fill_version_conflict')
            if (type(recorded_fill.get('protection_replay_digest')) is not str
                    or recorded_fill['protection_replay_digest'] != event['digest']):
                # 증거 없는 과거 row를 현재 history로 소급 보정하지 않는다.
                raise ValueError('protection_replay_fill_digest_conflict')
        prior_scope = resolved["after"]
        events.append(resolved)
    if history["tail"] != previous or digest(events[-1]["after"]) != digest(_scope(state, symbol)):
        raise ValueError("current_protection_evidence_mismatch")
    start = None
    for index, event in enumerate(events):
        snapshot = event["before"]
        dto, position = snapshot["protection"], snapshot["position"]
        guarded = dto["states"].get(symbol)
        # quote/policy capture는 이미 degraded인 before만 기록한다.
        # 따라서 새로운 healthy anchor를 제공할 수 있는 실제 입력은 fill뿐이다.
        if event['kind'] == 'fill' and symbol not in dto["degraded"] and (
            (position is None and guarded is None
             and event["fill_kind"] == "initial_entry")
            or (position is not None and guarded is not None
                and guarded["remaining_quantity"] == position["quantity"])
        ):
            start = index
    if start is None:
        raise ValueError("missing_healthy_anchor")
    _require_intraday_completeness(state, events[start:])
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
        _require_replay_policy(candidate, event['before']['protection'])
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
        elif event['kind'] == 'intraday_policy':
            if (set(event) != {'kind', 'symbol', 'source_version', 'applied_at', 'before', 'after',
                               'policy_code', 'previous', 'policy_input', 'digest'}
                    or type(event['source_version']) is not int
                    or now.isoformat() != now.astimezone(ZoneInfo('Asia/Seoul')).isoformat()):
                raise ValueError('invalid_intraday_replay_event')
            fact = IntradayPolicyReplayInput.from_dict(event['policy_input'])
            source = state['risk_sources']['records'].get(fact.operation_id)
            terminal = (source or {}).get('terminal')
            receipt = (terminal or {}).get('receipt', {})
            ticket = (source or {}).get('ticket', {})
            if (receipt.get('status') != 'accepted' or receipt.get('committed_version') != fact.source_version
                    or receipt.get('outcome_digest') != fact.outcome_digest
                    or ticket.get('kind') != 'intraday_5m' or ticket.get('sequence') != fact.attempt_sequence
                    or ticket.get('request_digest') != fact.request_digest
                    or event['source_version'] != fact.source_version):
                raise ValueError('intraday_replay_source_crosslink_conflict')
            # 후속 conflict는 현재 entry unknown이다. 이미 적용된 원 terminal은 별도 사실이다.
            envelope = terminal['envelope']
            classified = _intraday_transition_input(state, fact, envelope)
            if classified > now or now.date().isoformat() != ticket['business_day']:
                raise ValueError('invalid_intraday_replay_time')
            _validate_intraday_scopes(event['before'], event['after'], symbol, fact, envelope)
            result = transition_intraday(fact.before_policy, candidate,
                change_pct=envelope['payload']['change_pct'], classified_at=classified)
            if result.status != 'applied' or result.state != fact.after_policy:
                raise ValueError('intraday_replay_recovered_transition_conflict')
            candidate = result.protection_dto
        else:
            raise ValueError("unsupported_protection_input")
        _require_replay_policy(candidate, event['after']['protection'])
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
            candidate, replayed_orders = _replay(state, symbol, state_version=state_version)
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
