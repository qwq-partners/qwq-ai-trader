"""Opt-in internal capital guards for an offline price proxy, never broker approval."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, DecimalException, localcontext
import hashlib
import json
import re

from .entry_price_shadow import _mapping, _number, _integer, _text, _timestamp

SNAPSHOT_FIELDS = set(('version basis stage captured_at signal_id order_id symbol strategy requested_quantity '
    'current_order_reservation_included equity cash cash_after_reserve prior_pending_cash_reserved '
    'core_cash_reserved cash_capacity_before strategy_allocation_pct strategy_held_notional '
    'strategy_pending_reserved strategy_cap_notional strategy_remaining_notional reference_price').split())


@dataclass(frozen=True)
class CapitalPolicy:
    policy_ref: str
    fixed_at: datetime
    source_version_ref: str
    configuration_ref: str
    max_snapshot_age_seconds: Decimal
    risk_per_trade_pct: Decimal
    max_position_pct: Decimal

    @classmethod
    def from_dict(cls, value):
        source = _mapping(value, 'capital_policy')
        if set(source) != set(cls.__dataclass_fields__) | {'version'} or source['version'] != 'pre-pending-guards-v1':
            raise ValueError('INVALID:capital_policy.schema')
        refs = {}
        for key in ('policy_ref', 'source_version_ref', 'configuration_ref'):
            refs[key] = _text(source[key], 'capital_policy.' + key)
            if len(refs[key]) > 200:
                raise ValueError('INVALID:capital_policy.reference')
        age = _number(source['max_snapshot_age_seconds'], 'capital_policy.age', zero=True)
        risk = _number(source['risk_per_trade_pct'], 'capital_policy.risk')
        position = _number(source['max_position_pct'], 'capital_policy.position')
        if risk > 100 or position > 100:
            raise ValueError('INVALID:capital_policy.percent')
        return cls(**refs, fixed_at=_timestamp(source['fixed_at'], 'capital_policy.fixed_at'),
                   max_snapshot_age_seconds=age, risk_per_trade_pct=risk, max_position_pct=position)

    def export(self):
        return {'version': 'pre-pending-guards-v1', 'policy_ref': self.policy_ref,
                'fixed_at': self.fixed_at.isoformat(), 'source_version_ref': self.source_version_ref,
                'configuration_ref': self.configuration_ref,
                'max_snapshot_age_seconds': str(self.max_snapshot_age_seconds),
                'risk_per_trade_pct': str(self.risk_per_trade_pct), 'max_position_pct': str(self.max_position_pct),
                'market_buffer_multiplier': '1.3', 'cash_gate_multiplier': '1.001',
                'basis': 'declared_guards_not_verified_effective_configuration'}


@dataclass(frozen=True)
class CapitalLimits:
    cash: Decimal
    strategy: Decimal | None
    position: Decimal
    risk: Decimal
    risk_stop: Decimal
    snapshot_ref: str
    policy_ref: str

    def failure(self, notional, cost):
        # The same monotone predicates run at the observed ask and each price-cap midpoint.
        if cost > self.cash:
            return 'cash_cost'
        if notional * Decimal('1.3') > self.cash:
            return 'market'
        if notional * Decimal('1.001') > self.cash:
            return 'cash_gate'
        if self.strategy is not None and notional > self.strategy:
            return 'strategy'
        if notional > self.position:
            return 'position'
        if cost * self.risk_stop / 100 > self.risk:
            return 'risk'
        return None

    def export(self):
        return {'basis': 'pre_pending_internal_guard_proxy', 'snapshot_ref': self.snapshot_ref,
                'policy_ref': self.policy_ref, 'cash_cost_capacity': str(self.cash),
                'strategy_notional_remaining': None if self.strategy is None else str(self.strategy),
                'strategy_cap_status': 'not_applicable' if self.strategy is None else 'applied',
                'position_notional_cap': str(self.position), 'risk_loss_budget': str(self.risk),
                'risk_stop_pct': str(self.risk_stop)}


def _signed(value, name):
    if value is None or isinstance(value, bool):
        raise ValueError('INVALID:capital.' + name)
    try:
        number = Decimal(str(value))
    except (ValueError, DecimalException):
        raise ValueError('INVALID:capital.' + name) from None
    if not number.is_finite() or abs(number) > Decimal('1e15'):
        raise ValueError('INVALID:capital.' + name)
    return number


def limits_from_record(record, policy):
    evidence = _mapping(record.get('capital_evidence'), 'capital_evidence')
    required = {'snapshot', 'snapshot_ref', 'snapshot_status', 'signal_id', 'order_id',
                'signal_observed_at', 'order_reference_price', 'order_type'}
    if set(evidence) != required or evidence['snapshot_status'] != 'recorded' or evidence['order_type'] != 'market':
        raise ValueError('CAPITAL_EVIDENCE_CONTRACT')
    snapshot = _mapping(evidence['snapshot'], 'capital_snapshot')
    if (set(snapshot) != SNAPSHOT_FIELDS or snapshot['version'] != 'pre-pending-capacity-v1'
            or snapshot['basis'] != 'engine_memory_not_broker_balance'
            or snapshot['stage'] != 'before_current_pending_registration'
            or snapshot['current_order_reservation_included'] is not False):
        raise ValueError('CAPITAL_SNAPSHOT_CONTRACT')
    digest = _text(evidence['snapshot_ref'], 'capital_snapshot_ref')
    try:
        actual = hashlib.sha256(json.dumps(snapshot, sort_keys=True, allow_nan=False).encode()).hexdigest()
    except (ValueError, TypeError):
        raise ValueError('CAPITAL_SNAPSHOT_ENCODING') from None
    if not re.fullmatch('[0-9a-f]{64}', digest) or actual != digest:
        raise ValueError('CAPITAL_SNAPSHOT_HASH_MISMATCH')
    for key in ('signal_id', 'order_id'):
        if _text(evidence[key], key) != snapshot[key]:
            raise ValueError('CAPITAL_ORDER_IDENTITY_MISMATCH')
    if (snapshot['symbol'] != record['symbol'] or snapshot['strategy'] != record['strategy']
            or _integer(snapshot['requested_quantity'], 'capital.quantity') != _integer(record['quantity'], 'quantity')
            or _number(snapshot['reference_price'], 'capital.price') != _number(evidence['order_reference_price'], 'order.price')):
        raise ValueError('CAPITAL_ORDER_IDENTITY_MISMATCH')
    signal_at = _timestamp(evidence['signal_observed_at'], 'capital.signal_at')
    captured = _timestamp(snapshot['captured_at'], 'capital.captured_at')
    decision = _timestamp(record['decision_at'], 'decision_at')
    if (not policy.fixed_at <= signal_at <= captured <= decision
            or Decimal(str((decision - captured).total_seconds())) > policy.max_snapshot_age_seconds):
        raise ValueError('CAPITAL_TIME_ORDER_OR_STALE')
    equity = _number(snapshot['equity'], 'capital.equity')
    cash = _signed(snapshot['cash'], 'cash')
    after = _number(snapshot['cash_after_reserve'], 'capital.after_reserve', zero=True)
    pending = _number(snapshot['prior_pending_cash_reserved'], 'capital.pending', zero=True)
    core = _number(snapshot['core_cash_reserved'], 'capital.core', zero=True)
    capacity = _signed(snapshot['cash_capacity_before'], 'cash_capacity_before')
    held = _number(snapshot['strategy_held_notional'], 'capital.strategy_held', zero=True)
    strategy_pending = _number(snapshot['strategy_pending_reserved'], 'capital.strategy_pending', zero=True)
    allocation = _number(snapshot['strategy_allocation_pct'], 'capital.allocation', zero=True)
    if equity > Decimal('1e15') or allocation > 100 or after > max(cash, Decimal(0)):
        raise ValueError('CAPITAL_COMPONENT_RANGE')
    # v1 was produced with the engine's float-percentage conversion and default Decimal precision.
    # Match its recorded arithmetic; do not silently reinterpret it as a newer exact-Decimal contract.
    with localcontext() as ctx:
        ctx.prec = 28
        if capacity != after - pending - core:
            raise ValueError('CAPITAL_CASH_ARITHMETIC_MISMATCH')
        if allocation > 0:
            cap = equity * Decimal(str(float(allocation) / 100))
            remaining = _signed(snapshot['strategy_remaining_notional'], 'strategy_remaining')
            if (_signed(snapshot['strategy_cap_notional'], 'strategy_cap') != cap
                    or remaining != cap - held - strategy_pending):
                raise ValueError('CAPITAL_STRATEGY_ARITHMETIC_MISMATCH')
        else:
            if snapshot['strategy_cap_notional'] is not None or snapshot['strategy_remaining_notional'] is not None:
                raise ValueError('CAPITAL_UNCAPPED_STRATEGY_MISMATCH')
            remaining = None
    stop = _number(record.get('risk_stop_pct'), 'capital.risk_stop')
    if stop >= 100:
        raise ValueError('INVALID:capital.risk_stop')
    return CapitalLimits(capacity, remaining, equity * policy.max_position_pct / 100,
                         equity * policy.risk_per_trade_pct / 100, stop, digest, policy.policy_ref)
