"""비식별 복구 진단 DTO와 순수 보고서 builder. 어떤 판정도 복구 권한이 아니다."""
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from .lifecycle import TERMINAL_STATES
from .reservations import has_remaining_reservation


# 외부 문자열은 이 사전에 합류하지 않는다.
FINDINGS = {
    'snapshot_unavailable': ('unsupported_capture', 'inspect_capture_support'),
    'snapshot_volatile': ('memory_changed_between_samples', 'capture_again_when_quiet'),
    'publication_inconsistent': ('publication_versions_disagree', 'inspect_publication'),
    'owner_health_unconfirmed': ('owner_composite_not_confirmed', 'inspect_owner_barrier'),
    'unapplied_inbox': ('inbox_not_applied', 'inspect_fill_application'),
    'observation_not_applied': ('observed_applied_difference', 'inspect_fill_application'),
    'unknown_buy': ('buy_ack_unknown', 'inspect_broker_evidence'),
    'remaining_reservation': ('positive_reserved_resource', 'inspect_reservation_evidence'),
    'terminal_reservation': ('terminal_with_reserved_resource', 'inspect_reservation_evidence'),
    'cancel_unconfirmed': ('cancel_without_parent_finality', 'inspect_broker_evidence'),
    'attempt_link_inconsistent': ('attempt_relationship_mismatch', 'inspect_attempt_links'),
    'protection_unsubmitted': ('audit_without_submission_link', 'inspect_original_protection'),
    'protection_link_inconsistent': ('protection_relationship_mismatch', 'inspect_original_protection'),
    'protection_quantity_inconsistent': ('linked_quantity_mismatch', 'inspect_protection_quantity'),
    'protection_failure_unattributed': ('failure_without_durable_attribution', 'inspect_failure_origin'),
    'repair_only': ('manual_investigation_required', 'inspect_recovery_evidence'),
    'pending_sell': ('acknowledged_nonterminal_sell', 'observe_existing_attempt'),
    'evidence_invalid': ('unsupported_or_invalid_evidence', 'inspect_evidence_schema'),
    'disposition_not_durable': ('stale_abandoned_history_unavailable', 'inspect_original_history'),
}


@dataclass(frozen=True, slots=True)
class RecoverySnapshot:
    """원문/식별자/가격/예외/작업 참조를 포함하지 않는 값 객체."""
    captured_at: str | None
    mode: str = 'unknown'
    snapshot_stable: bool | None = None
    publication_consistent: bool | None = None
    owner_health_confirmed: bool | None = None
    execution_version: int | None = None
    published_version: int | None = None
    engine_version: int | None = None
    findings: tuple[tuple[str, int], ...] = ()


def build_recovery_diagnostic(snapshot: RecoverySnapshot) -> dict:
    """캡처의 안전 DTO만 JSON tree로 옮긴다. 매번 새 중첩 객체를 만든다."""
    if type(snapshot) is not RecoverySnapshot:
        snapshot = RecoverySnapshot(None, findings=(('snapshot_unavailable', 1),))
    return {
        'schema_version': 1, 'read_only': True, 'automatic_action_allowed': False,
        'trading_ready': False, 'installation_verified': False,
        'captured_at': snapshot.captured_at, 'mode': snapshot.mode,
        'snapshot_stable': snapshot.snapshot_stable,
        'publication_consistent': snapshot.publication_consistent,
        'owner_health_confirmed': snapshot.owner_health_confirmed,
        'versions': {'execution': snapshot.execution_version,
                     'published': snapshot.published_version, 'engine': snapshot.engine_version},
        'findings': [dict(code=code, count=count, evidence=FINDINGS[code][0],
                          next_check=FINDINGS[code][1])
                     for code, count in sorted(dict(snapshot.findings,
                         disposition_not_durable=1).items())],
    }


def _integer(value):
    if type(value) is not int or value < 0:
        raise ValueError('invalid_evidence')
    return value


def _mapping(value):
    if type(value) is not dict:
        raise ValueError('invalid_evidence')
    return value


def _identity(value):
    if type(value) is not str or not value or value.strip() != value:
        raise ValueError('invalid_evidence')
    return value


def _classify(sample):
    """검증·복사된 private 표본에만 적용한다. 원문은 반환하지 않는다."""
    counts = {}

    def add(code, count=1):
        if count:
            counts[code] = counts.get(code, 0) + count

    state, ram, producer = sample['state'], sample['runtime'], sample['producer']
    attempts = _mapping(state.get('attempts'))
    intents = _mapping(state.get('intents'))
    protection = _mapping(state.get('protection'))
    positions = _mapping(_mapping(state.get('portfolio')).get('positions'))
    protected = _mapping(protection.get('states'))
    pending = _mapping(protection.get('pending_owners'))
    episodes = {} if producer is None else _mapping(producer['_episodes'])
    admissions = _mapping(state.get('protection_quote_admissions', {}))
    for row in _mapping(state.get('inbox', {})).values():
        if _mapping(row).get('status') not in ('APPLIED', 'SUPERSEDED'):
            add('unapplied_inbox')

    for key, row in attempts.items():
        try:
            _identity(key)
            _mapping(row)
            kind, side = row.get('kind'), row.get('side')
            if kind not in ('submit', 'cancel', 'modify') or side not in ('buy', 'sell'):
                raise ValueError('invalid_evidence')
            observed = _integer(row.get('observed_quantity'))
            applied = _integer(row.get('applied_quantity'))
            if observed != applied:
                add('observation_not_applied')
            if applied > observed:
                add('evidence_invalid')
            intent = intents.get(row.get('intent_id'))
            link_ok = (type(intent) is dict and row.get('attempt_id') == key
                       and intent.get('symbol') == row.get('symbol')
                       and intent.get('side') == side
                       and type(intent.get('attempt_ids')) is list
                       and key in intent['attempt_ids'])
            if not link_ok:
                add('attempt_link_inconsistent')
            remaining = has_remaining_reservation(row)
            if 'reserved_planned_risk' in row and row['reserved_planned_risk'] is None:
                add('evidence_invalid')
            terminal = row.get('state') in TERMINAL_STATES
            if remaining:
                add('remaining_reservation')
                if terminal:
                    add('terminal_reservation')
            if kind == 'submit' and side == 'buy' and (
                    row.get('command_status') == 'unknown' or row.get('state') == 'blocked_unknown'):
                add('unknown_buy')
            if (kind == 'submit' and side == 'sell' and link_ok and not terminal
                    and row.get('state') != 'blocked_unknown'
                    and row.get('command_status') == 'acknowledged'
                    and not row.get('evidence_conflict') and observed == applied):
                add('pending_sell')
            if kind in ('cancel', 'modify'):
                parent = attempts.get(row.get('parent_attempt_id'))
                parent_ok = (type(parent) is dict and parent.get('kind') == 'submit'
                    and parent is not row and parent.get('order_ref') is not None
                    and row.get('order_ref') == parent.get('order_ref')
                    and all(row.get(name) == parent.get(name) for name in ('intent_id', 'symbol', 'side')))
                if not parent_ok:
                    add('attempt_link_inconsistent')
                if kind == 'cancel' and row.get('command_status') not in ('not_sent', 'rejected'):
                    if (not parent_ok or parent.get('state') not in TERMINAL_STATES
                            or parent.get('observed_quantity') != parent.get('applied_quantity')):
                        add('cancel_unconfirmed')
        except (ValueError, TypeError, KeyError, InvalidOperation):
            add('evidence_invalid')

    for identity, intent in intents.items():
        try:
            _identity(identity)
            _mapping(intent)
            _identity(intent.get('symbol'))
            _integer(intent.get('target_quantity'))
            ids = intent.get('attempt_ids')
            if type(ids) is not list or intent.get('side') not in ('buy', 'sell'):
                raise ValueError('invalid_evidence')
            if any(type(key) is not str or key not in attempts
                   or attempts[key].get('intent_id') != identity for key in ids):
                add('attempt_link_inconsistent')
        except (ValueError, TypeError, KeyError, AttributeError):
            add('evidence_invalid')

    for command, row in _mapping(state.get('outbox')).items():
        try:
            _mapping(row)
            if row.get('kind') != 'protection_decision' or row.get('effect_source') is not None:
                continue
            symbol, identity = _identity(row.get('symbol')), _identity(row.get('intent_id'))
            decision = row.get('decision')
            if (type(decision) is not list or len(decision) != 3
                    or decision[0] not in ('sell_all', 'sell_partial')
                    or _integer(decision[1]) == 0 or type(decision[2]) is not str):
                raise ValueError('invalid_evidence')
            intent = intents.get(identity)
            linked = False
            if intent is not None:
                linked = True
                if intent.get('symbol') != symbol or intent.get('side') != 'sell':
                    add('protection_link_inconsistent')
                if intent.get('target_quantity') != decision[1]:
                    add('protection_quantity_inconsistent')
            for other_symbol, episode in episodes.items():
                if episode.get('intent_id') == identity:
                    linked = True
                    if other_symbol != symbol or tuple(decision) != episode.get('decision'):
                        add('protection_link_inconsistent')
            for admission_key, admission in admissions.items():
                if admission_key == command or admission.get('intent_id') == identity:
                    linked = True
                    if admission.get('symbol') != symbol or admission.get('intent_id') != identity:
                        add('protection_link_inconsistent')
            for other_symbol, pending_identity in pending.items():
                if pending_identity == identity:
                    if other_symbol != symbol:
                        add('protection_link_inconsistent')
                    current = protected.get(other_symbol)
                    if type(current) is not dict or current.get('pending_target_qty') != decision[1]:
                        add('protection_quantity_inconsistent')
            if not linked:
                add('protection_unsubmitted')
        except (ValueError, TypeError, KeyError, AttributeError):
            add('evidence_invalid')

    for symbol, row in protected.items():
        try:
            qty = _integer(_mapping(row).get('remaining_quantity'))
            position = positions.get(symbol)
            if position is None or qty != _integer(_mapping(position).get('quantity')):
                add('protection_quantity_inconsistent')
        except (ValueError, TypeError, KeyError):
            add('evidence_invalid')
    for symbol in pending:
        intent = intents.get(pending[symbol])
        episode = episodes.get(symbol)
        if (intent is None and (episode is None or episode.get('intent_id') != pending[symbol])
                and not any(row.get('intent_id') == pending[symbol] and row.get('symbol') == symbol
                            for row in admissions.values())):
            add('protection_link_inconsistent')
    if ram['_protection_unattributed_failed']:
        add('protection_failure_unattributed')
    repair_count = len(_mapping(protection.get('degraded')))
    repair_count += len(ram['_protection_failures'])
    if producer is not None:
        repair_count += len(producer['_recovery_required'])
        if producer['_stats'].get('invariant_violation') is not None:
            repair_count += 1
    add('repair_only', repair_count)
    return counts
