"""명시 외부 context + 소유 checkpoint의 순수 정책 snapshot.

context의 observed_at은 자료 인계시각, snapshot.captured_at은 계산시각이다.
실제 공급원 freshness·최초 계좌 인계·권한을 증명하거나 runtime에 자동 설치하지 않는다.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, fields, replace
from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from .economics import decode_portfolio, validate_risk
from .protection import decode_protection
from .lifecycle import TERMINAL_STATES
from .reservations import has_remaining_reservation
from . import risk_policy as p

_KST = ZoneInfo('Asia/Seoul')


def _aware(value):
    if type(value) is not datetime or value.utcoffset() is None:
        raise ValueError('invalid_policy_context_time')
    return value.astimezone(_KST)


def _fields(value, names):
    if type(value) is not dict or value.keys() != set(names):
        raise ValueError('invalid_policy_context_fields')


@dataclass(frozen=True, slots=True)
class PolicyContext:
    business_day: date
    observed_at: datetime
    versions: p.PolicyVersions
    policy: p.EffectiveRiskPolicy
    sync: p.SyncPolicySnapshot
    trend: p.MarketTrendPolicySnapshot
    macro: p.MacroPolicySnapshot

    def __post_init__(self):
        if type(self.business_day) is not date or _aware(self.observed_at).date() != self.business_day:
            raise ValueError('invalid_policy_context_day')
        for value, kind in ((self.versions, p.PolicyVersions), (self.policy, p.EffectiveRiskPolicy),
                            (self.sync, p.SyncPolicySnapshot), (self.trend, p.MarketTrendPolicySnapshot),
                            (self.macro, p.MacroPolicySnapshot)):
            if type(value) is not kind:
                raise ValueError('invalid_policy_context_type')
        if self.sync.unhealthy_since is not None and self.sync.unhealthy_since > self.observed_at:
            raise ValueError('future_policy_sync_fact')

    @classmethod
    def from_snapshot(cls, snapshot):
        if type(snapshot) is not p.EntryPolicySnapshot:
            raise ValueError('explicit_policy_snapshot_required')
        return cls(snapshot.business_day, snapshot.captured_at, snapshot.versions,
                   snapshot.policy, snapshot.sync, snapshot.trend, snapshot.macro)

    def to_dict(self):
        result = asdict(self)
        result['business_day'] = self.business_day.isoformat()
        result['observed_at'] = self.observed_at.isoformat()
        result['policy']['buy_commission_rate'] = str(self.policy.buy_commission_rate)
        result['sync']['unhealthy_since'] = (None if self.sync.unhealthy_since is None
                                             else self.sync.unhealthy_since.isoformat())
        return result

    @classmethod
    def from_dict(cls, row):
        _fields(row, (f.name for f in fields(cls)))
        types = {'versions': p.PolicyVersions, 'policy': p.EffectiveRiskPolicy,
                 'sync': p.SyncPolicySnapshot, 'trend': p.MarketTrendPolicySnapshot,
                 'macro': p.MacroPolicySnapshot}
        try:
            parts = {}
            for key, kind in types.items():
                _fields(row[key], (f.name for f in fields(kind)))
                values = dict(row[key])
                if key == 'policy':
                    if type(values['buy_commission_rate']) is not str:
                        raise ValueError('invalid_policy_fee_type')
                    values['buy_commission_rate'] = Decimal(values['buy_commission_rate'])
                if key == 'sync' and values['unhealthy_since'] is not None:
                    values['unhealthy_since'] = datetime.fromisoformat(values['unhealthy_since'])
                parts[key] = kind(**values)
            result = cls(date.fromisoformat(row['business_day']), datetime.fromisoformat(row['observed_at']), **parts)
            # 날짜/시각까지 canonical DTO여야 하며 bool/int coercion을 사용하지 않는다.
            if result.to_dict() != row:
                raise ValueError('noncanonical_policy_context')
            return result
        except (TypeError, ArithmeticError, KeyError) as exc:
            raise ValueError('invalid_policy_context') from exc


def build_owned_snapshot(state: dict, *, context: PolicyContext, version: int,
                         now: datetime, prices: dict[str, Decimal],
                         exclude_attempt: str | None = None) -> p.EntryPolicySnapshot:
    """owner가 잠금/게시 버전 검사를 한 뒤 호출한다. 이 함수에는 I/O/시계 조회가 없다.

    prices는 runtime의 동일 checkpoint+수락 시세 projection에서 가져와야 한다.
    cash/pending/reentry는 외부 snapshot이 아니라 checkpoint에서 매번 만든다.
    owned 네 source version은 읽은 checkpoint 버전이며 외부 regime/macro는 인계값이다.
    """
    if type(context) is not PolicyContext or type(version) is not int or version < 0:
        raise ValueError('invalid_owned_snapshot_input')
    now = _aware(now)
    if context.business_day != now.date() or context.observed_at > now:
        raise ValueError('policy_context_day_or_time_mismatch')
    pf = decode_portfolio(state['portfolio'])
    risk = validate_risk(state['risk'])
    protection = decode_protection(state['protection'], clock=lambda: now)
    if risk['day'] != now.date().isoformat() or risk['daily_stats']['trades'] != pf.daily_trades:
        raise ValueError('policy_owned_day_or_counter_mismatch')
    if type(prices) is not dict or prices.keys() != pf.positions.keys():
        raise ValueError('policy_price_scope_mismatch')
    for symbol, price in prices.items():
        if type(price) is not Decimal or not price.is_finite() or price <= 0:
            raise ValueError('invalid_owned_price')
        pf.positions[symbol].current_price = price
    positions = []
    for symbol, position in pf.positions.items():
        protective = protection.get_state(symbol)
        valid = protective is not None and symbol not in state['protection']['degraded']
        positions.append(p.PositionPolicyFact(
            symbol, position.strategy, position.sector, position.quantity, position.market_value,
            None if position.entry_time is None else position.entry_time.astimezone(_KST).date(),
            protective.original_quantity if valid else None,
            protective.remaining_quantity if valid else None,
            protective.current_stage.value if valid else None, valid))
    attempts = state.get('attempts', {})
    if type(attempts) is not dict:
        raise ValueError('invalid_policy_attempts')
    pending = []
    excluded_symbol = None
    for aid, attempt in attempts.items():
        if aid == exclude_attempt:
            excluded_symbol = attempt['symbol']
            continue
        if attempt.get('kind') != 'submit':
            continue
        remaining = has_remaining_reservation(attempt)
        cash = attempt['reserved_cash']
        if attempt.get('state') in TERMINAL_STATES and not remaining:
            continue
        if type(aid) is not str or aid != attempt['attempt_id']:
            raise ValueError('policy_attempt_identity_mismatch')
        pending.append(p.PendingPolicyFact(aid, attempt['intent_id'], attempt['symbol'], attempt['side'],
                                          attempt['strategy'], attempt.get('sector'), Decimal(cash)))
    effects = state.get('entry_policy_effects', {'pending_sectors': {}})
    if type(effects) is not dict or type(effects.get('pending_sectors')) is not dict:
        raise ValueError('invalid_owned_policy_effects')
    trend = context.trend
    regime_version, effective_policy = context.versions.regime, context.policy
    if 'regime_policy' in state:
        from .regime_owner import validate_regime_policy, require_current_trend, effective_regime
        from ...core.market_regime import MarketRegimeAdapter
        root = validate_regime_policy(state, version)
        regime_version = require_current_trend(state, now.date().isoformat())
        owned = root['trend_state']['market_trend']
        trend = p.MarketTrendPolicySnapshot(owned['present'], owned['recovering'], effects['sidecar_active'])
        effective_policy = replace(context.policy, regime_min_cash_reserve_pct=
            MarketRegimeAdapter.REGIME_PARAMS[effective_regime(state, now)]['min_cash_reserve_pct'])
    if 'sidecar_active' in effects:
        trend = replace(trend, sidecar_active=effects['sidecar_active'])
    sectors = tuple(p.PendingSectorFact(symbol, sector) for symbol, sector in effects['pending_sectors'].items()
                    if symbol != excluded_symbol)
    return p.EntryPolicySnapshot(
        p.PolicyVersions(version, version, version, version, context.versions.config,
                         regime_version, context.versions.macro),
        context.business_day, now, effective_policy,
        p.PortfolioPolicySnapshot(pf.cash, pf.total_equity, pf.effective_daily_pnl, pf.daily_trades, tuple(positions)),
        p.ReentryPolicySnapshot(frozenset(risk['stop_loss_today']), frozenset(risk['stop_loss_rebound_used']),
            tuple(p.ExitPolicyFact(symbol, Decimal(row['price']), datetime.fromisoformat(row['time']))
                  for symbol, row in risk['exited_today'].items())),
        context.sync, trend, context.macro,
        p.ExitCooldownPolicySnapshot(date.fromisoformat(risk['daily_exit_count_date']), risk['daily_exit_count']),
        tuple(pending), sectors)
