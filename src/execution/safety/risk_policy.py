"""KR 기존 진입 gate의 순수 특성화 경계.

allowed는 legacy 정책 결과이며 송신/예약 권한이 아니다. 반환 effects는
미실행 제안이고 적용은 후속 단일 owner가 담당한다. 파일·시계·manager 접근,
UNKNOWN 해제, 전략 sizing/alpha 파이프라인 및 실제 주문 배선은 없다.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
import math
from zoneinfo import ZoneInfo

from ...core.types import OrderSide
from .guards import EntryOrigin

_KST = ZoneInfo('Asia/Seoul')
_DATE_TYPE, _TIME_TYPE = date, datetime


def _int(value, *, nonnegative=True):
    if type(value) is not int or (nonnegative and value < 0):
        raise ValueError('명시 정수 입력이 필요합니다')


def _number(value):
    try:
        valid = type(value) in (float, int) and math.isfinite(value) and value >= 0
    except OverflowError:
        valid = False
    if not valid:
        raise ValueError('유한한 비음수 정책 수치가 필요합니다')


def _money(value, *, nonnegative=False):
    if type(value) is not Decimal or not value.is_finite() or (nonnegative and value < 0):
        raise ValueError('유한한 Decimal 입력이 필요합니다')


def _text(value, *, optional=False):
    if optional and value is None:
        return
    if type(value) is not str or not value:
        raise ValueError('비어 있지 않은 문자열이 필요합니다')


def _bool(value):
    if type(value) is not bool:
        raise ValueError('명시 bool 입력이 필요합니다')


def _date(value):
    if type(value) is not _DATE_TYPE:
        raise ValueError('명시 사업일이 필요합니다')


def _time(value):
    if not isinstance(value, _TIME_TYPE) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError('aware 시각이 필요합니다')


def _tuple(value, item_type, key):
    if type(value) is not tuple or any(type(x) is not item_type for x in value):
        raise ValueError('불변 fact tuple이 필요합니다')
    if len({getattr(x, key) for x in value}) != len(value):
        raise ValueError('중복 fact 식별자')


@dataclass(frozen=True, slots=True)
class PolicyVersions:
    execution: int
    quote: int
    risk: int
    protection: int
    config: str
    regime: int
    macro: int

    def __post_init__(self):
        for value in (self.execution, self.quote, self.risk, self.protection, self.regime, self.macro):
            _int(value)
        _text(self.config)


@dataclass(frozen=True, slots=True)
class EffectiveRiskPolicy:
    """운영값을 추측하지 않는다. 기존 float→Decimal 변환 순서를 보존한다.

    sizing/fee 필드는 후속 예약의 명시 basis이며 이 gate가 0.7%/18%를
    새로 강제하지 않는다. 시장가 sizing1.3/pending1.015/gate1.001은 별개다.
    """
    daily_max_loss_pct: float
    daily_max_trades: int
    max_daily_new_buys: int
    max_positions: int
    max_core_positions: int
    max_position_pct: float
    min_cash_reserve_pct: float
    max_positions_per_sector: int
    daily_exit_cooldown_threshold: int
    regime_min_cash_reserve_pct: float
    core_allocation_pct: float
    sizing_mode: str
    risk_per_trade_pct: float
    risk_max_position_pct: float
    buy_commission_rate: Decimal
    # 설정 hybrid 축. 기본값을 주면 '신고 없음'이 조용히 off로 읽혀 대조가 무력해진다.
    hybrid_enabled: bool

    def __post_init__(self):
        for value in (self.daily_max_trades, self.max_daily_new_buys, self.max_positions,
                      self.max_core_positions, self.max_positions_per_sector,
                      self.daily_exit_cooldown_threshold):
            _int(value)
        for value in (self.daily_max_loss_pct, self.max_position_pct, self.min_cash_reserve_pct,
                      self.regime_min_cash_reserve_pct, self.core_allocation_pct,
                      self.risk_per_trade_pct, self.risk_max_position_pct):
            _number(value)
        if self.sizing_mode not in ('nominal', 'risk'):
            raise ValueError('알 수 없는 sizing basis')
        _money(self.buy_commission_rate, nonnegative=True)
        _bool(self.hybrid_enabled)


@dataclass(frozen=True, slots=True)
class PositionPolicyFact:
    symbol: str
    strategy: str | None
    sector: str | None
    quantity: int
    market_value: Decimal
    entry_day: date | None
    exit_original_quantity: int | None
    exit_remaining_quantity: int | None
    exit_stage: str | None
    exit_state_valid: bool

    def __post_init__(self):
        _text(self.symbol)
        for value in (self.strategy, self.sector, self.exit_stage):
            if value is not None and type(value) is not str:
                raise ValueError('포지션 문자열이 필요합니다')
        _int(self.quantity)
        _money(self.market_value, nonnegative=True)
        if self.entry_day is not None:
            _date(self.entry_day)
        for value in (self.exit_original_quantity, self.exit_remaining_quantity):
            if value is not None:
                _int(value, nonnegative=False)
        _bool(self.exit_state_valid)


@dataclass(frozen=True, slots=True)
class PortfolioPolicySnapshot:
    cash: Decimal
    equity: Decimal
    effective_daily_pnl: Decimal
    daily_trades: int
    positions: tuple[PositionPolicyFact, ...]

    def __post_init__(self):
        for value in (self.cash, self.equity, self.effective_daily_pnl):
            _money(value)
        _int(self.daily_trades)
        _tuple(self.positions, PositionPolicyFact, 'symbol')
        if self.cash + sum((x.market_value for x in self.positions), Decimal('0')) != self.equity:
            raise ValueError('포트폴리오 평가금액 불일치')


@dataclass(frozen=True, slots=True)
class ExitPolicyFact:
    symbol: str
    price: Decimal
    exited_at: datetime

    def __post_init__(self):
        _text(self.symbol)
        _money(self.price, nonnegative=True)
        if self.price <= 0:
            raise ValueError('측정된 청산 가격이 필요합니다')
        _time(self.exited_at)


@dataclass(frozen=True, slots=True)
class ReentryPolicySnapshot:
    stop_loss_today: frozenset[str]
    rebound_used: frozenset[str]
    exited_today: tuple[ExitPolicyFact, ...]

    def __post_init__(self):
        for value in (self.stop_loss_today, self.rebound_used):
            if type(value) is not frozenset:
                raise ValueError('불변 재진입 집합이 필요합니다')
            for symbol in value:
                _text(symbol)
        _tuple(self.exited_today, ExitPolicyFact, 'symbol')


@dataclass(frozen=True, slots=True)
class SyncPolicySnapshot:
    healthy: bool
    fail_count: int
    unhealthy_since: datetime | None
    timeout_minutes: int

    def __post_init__(self):
        _bool(self.healthy)
        _int(self.fail_count)
        _int(self.timeout_minutes)
        if self.unhealthy_since is not None:
            _time(self.unhealthy_since)


@dataclass(frozen=True, slots=True)
class MarketTrendPolicySnapshot:
    present: bool
    recovering: bool
    sidecar_active: bool

    def __post_init__(self):
        for value in (self.present, self.recovering, self.sidecar_active):
            _bool(value)


@dataclass(frozen=True, slots=True)
class MacroPolicySnapshot:
    label: str | None
    is_event: bool
    lookup_failed: bool

    def __post_init__(self):
        _text(self.label, optional=True)
        _bool(self.is_event)
        _bool(self.lookup_failed)
        if self.is_event and self.label is None:
            raise ValueError('이벤트 라벨이 필요합니다')


@dataclass(frozen=True, slots=True)
class ExitCooldownPolicySnapshot:
    day: date
    count: int

    def __post_init__(self):
        _date(self.day)
        _int(self.count)


@dataclass(frozen=True, slots=True)
class PendingPolicyFact:
    attempt_id: str
    intent_id: str
    symbol: str
    side: str
    strategy: str
    sector: str | None
    reserved_cash: Decimal

    def __post_init__(self):
        for value in (self.attempt_id, self.intent_id, self.symbol):
            _text(value)
        if type(self.strategy) is not str:
            raise ValueError('명시 pending strategy가 필요합니다')
        if type(self.side) is not str or self.side not in ('buy', 'sell'):
            raise ValueError('명시 pending side가 필요합니다')
        if self.sector is not None and type(self.sector) is not str:
            raise ValueError('명시 sector가 필요합니다')
        _money(self.reserved_cash, nonnegative=True)


@dataclass(frozen=True, slots=True)
class PendingSectorFact:
    """engine의 sector map은 pending side/cash map과 별개다."""
    symbol: str
    sector: str

    def __post_init__(self):
        _text(self.symbol)
        _text(self.sector)


@dataclass(frozen=True, slots=True)
class EntryPolicyInput:
    symbol: str
    side: OrderSide
    quantity: int
    valuation_price: Decimal
    strategy: str
    sector: str | None

    def __post_init__(self):
        _text(self.symbol)
        if type(self.side) is not OrderSide:
            raise ValueError('명시 OrderSide가 필요합니다')
        _int(self.quantity)
        _money(self.valuation_price, nonnegative=True)
        if self.quantity <= 0 or self.valuation_price <= 0:
            raise ValueError('양의 수량/평가가격이 필요합니다')
        if type(self.strategy) is not str or (self.sector is not None and type(self.sector) is not str):
            raise ValueError('명시 전략/sector가 필요합니다')


@dataclass(frozen=True, slots=True)
class EntryPolicySnapshot:
    versions: PolicyVersions
    business_day: date
    captured_at: datetime
    policy: EffectiveRiskPolicy
    portfolio: PortfolioPolicySnapshot
    reentry: ReentryPolicySnapshot
    sync: SyncPolicySnapshot
    trend: MarketTrendPolicySnapshot
    macro: MacroPolicySnapshot
    exit_cooldown: ExitCooldownPolicySnapshot
    pending: tuple[PendingPolicyFact, ...]
    pending_sectors: tuple[PendingSectorFact, ...]

    def __post_init__(self):
        for value, kind in ((self.versions, PolicyVersions), (self.policy, EffectiveRiskPolicy),
            (self.portfolio, PortfolioPolicySnapshot), (self.reentry, ReentryPolicySnapshot),
            (self.sync, SyncPolicySnapshot), (self.trend, MarketTrendPolicySnapshot),
            (self.macro, MacroPolicySnapshot), (self.exit_cooldown, ExitCooldownPolicySnapshot)):
            if type(value) is not kind:
                raise ValueError('명시 snapshot 구성요소가 필요합니다')
        _date(self.business_day)
        _time(self.captured_at)
        if self.captured_at.astimezone(_KST).date() != self.business_day:
            raise ValueError('게시 시각과 사업일 불일치')
        _tuple(self.pending, PendingPolicyFact, 'attempt_id')
        _tuple(self.pending_sectors, PendingSectorFact, 'symbol')
        if len({x.symbol for x in self.pending}) != len(self.pending):
            raise ValueError('legacy pending은 심볼별 하나여야 합니다')
        for fact in self.reentry.exited_today:
            if fact.exited_at > self.captured_at or fact.exited_at.astimezone(_KST).date() != self.business_day:
                raise ValueError('청산 시각/사업일 불일치')
        if self.sync.unhealthy_since is not None and self.sync.unhealthy_since > self.captured_at:
            raise ValueError('미래 동기화 실패 시각')
        if self.exit_cooldown.day > self.business_day:
            raise ValueError('미래 청산 카운터 일자')


class PolicyReason(str, Enum):
    ALLOWED = 'allowed'
    MACRO_LIMIT = 'macro_limit'
    REBOUND_USED = 'rebound_used'
    REBOUND_MISSING_EXIT = 'rebound_missing_exit'
    REBOUND_COOLDOWN = 'rebound_cooldown'
    REBOUND_PRICE = 'rebound_price'
    REENTRY_COOLDOWN = 'reentry_cooldown'
    REENTRY_FALLING = 'reentry_falling'
    SYNC_UNHEALTHY = 'sync_unhealthy'
    DAILY_LOSS_HARD_STOP = 'daily_loss_hard_stop'
    DAILY_LOSS_LIMIT = 'daily_loss_limit'
    DAILY_NEW_BUY_LIMIT = 'daily_new_buy_limit'
    CORE_SLOT_LIMIT = 'core_slot_limit'
    POSITION_SLOT_LIMIT = 'position_slot_limit'
    MINIMUM_CASH_RESERVE = 'minimum_cash_reserve'
    POSITION_VALUE_LIMIT = 'position_value_limit'
    CASH_INSUFFICIENT = 'cash_insufficient'
    SECTOR_LIMIT = 'sector_limit'
    EXIT_LOSS_COOLDOWN = 'exit_loss_cooldown'
    ENGINE_DAILY_HARD_CAP = 'engine_daily_hard_cap'
    DAILY_TRADE_LIMIT = 'daily_trade_limit'
    POLICY_DAY_MISMATCH = 'policy_day_mismatch'
    POLICY_EFFECT_PENDING = 'policy_effect_pending'


class EffectKind(str, Enum):
    SIDECAR_SET = 'sidecar_set'
    SYNC_START_REQUIRED = 'sync_start_required'
    LEGACY_SYNC_TIMEOUT_OBSERVED = 'legacy_sync_timeout_observed'
    DAILY_EXIT_ROLLOVER_REQUIRED = 'daily_exit_rollover_required'
    PENDING_SECTOR_SET = 'pending_sector_set'


@dataclass(frozen=True, slots=True)
class PolicyEffect:
    kind: EffectKind
    expected_versions: PolicyVersions
    value: bool | int | datetime | date | str | None

    def __post_init__(self):
        if type(self.kind) is not EffectKind or type(self.expected_versions) is not PolicyVersions:
            raise ValueError('명시 effect 종류와 source version이 필요합니다')
        if self.kind is EffectKind.SIDECAR_SET:
            _bool(self.value)
        elif self.kind in (EffectKind.SYNC_START_REQUIRED, EffectKind.LEGACY_SYNC_TIMEOUT_OBSERVED):
            _time(self.value)
        elif self.kind is EffectKind.DAILY_EXIT_ROLLOVER_REQUIRED:
            _date(self.value)
        else:
            _text(self.value)


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    allowed: bool
    reason: PolicyReason
    legacy_reason: str
    versions: PolicyVersions
    effects: tuple[PolicyEffect, ...]

    def __post_init__(self):
        _bool(self.allowed)
        if type(self.reason) is not PolicyReason or self.allowed != (self.reason is PolicyReason.ALLOWED):
            raise ValueError('정책 결과와 reason 불일치')
        if type(self.versions) is not PolicyVersions or type(self.legacy_reason) is not str:
            raise ValueError('명시 결과 source version과 사유가 필요합니다')
        if type(self.effects) is not tuple or any(type(e) is not PolicyEffect or e.expected_versions != self.versions for e in self.effects):
            raise ValueError('같은 source version의 불변 effect가 필요합니다')


def position_weight(fact: PositionPolicyFact) -> float:
    if type(fact) is not PositionPolicyFact:
        raise ValueError('명시 포지션 fact가 필요합니다')
    if not fact.exit_state_valid:
        return 1.0
    original = max(fact.exit_original_quantity if fact.exit_original_quantity is not None else 0, 1)
    remaining = max(fact.exit_remaining_quantity if fact.exit_remaining_quantity is not None else original, 0)
    try:
        weight = remaining / original
    except OverflowError:
        return 1.0  # 실제 helper의 계산 실패 fallback 보존
    if fact.exit_stage == 'trailing':
        return max(0.1, min(1.0, weight * 0.5))
    if fact.exit_stage in ('second', 'third'):
        return max(0.15, min(1.0, weight * 0.7))
    return max(0.2, min(1.0, weight))


def _snapshot(snapshot):
    if type(snapshot) is not EntryPolicySnapshot:
        raise ValueError('명시 위험 snapshot이 필요합니다')


def _input(entry, snapshot):
    _snapshot(snapshot)
    if type(entry) is not EntryPolicyInput:
        raise ValueError('명시 진입 입력이 필요합니다')


def _now(snapshot, now, *, check_day=True):
    _time(now)
    if now < snapshot.captured_at:
        raise ValueError('미래 snapshot')
    if check_day and now.astimezone(_KST).date() != snapshot.business_day:
        raise ValueError('현재 KST 사업일과 snapshot 불일치')


def _decision(snapshot, reason=PolicyReason.ALLOWED, message='', effects=()):
    return PolicyDecision(reason is PolicyReason.ALLOWED, reason, message,
                          snapshot.versions, tuple(effects))


def _effect(snapshot, kind, value):
    return PolicyEffect(kind, snapshot.versions, value)


def _with_effects(decision, effects):
    return PolicyDecision(decision.allowed, decision.reason, decision.legacy_reason,
                          decision.versions, tuple(effects) + decision.effects)


def available_cash(snapshot: EntryPolicySnapshot, *, regime: bool) -> Decimal:
    _snapshot(snapshot)
    _bool(regime)
    pct = snapshot.policy.regime_min_cash_reserve_pct if regime else snapshot.policy.min_cash_reserve_pct
    reserve = snapshot.portfolio.equity * Decimal(str(pct / 100))
    return max(snapshot.portfolio.cash - reserve, Decimal('0'))


def core_reserve(snapshot: EntryPolicySnapshot) -> Decimal:
    _snapshot(snapshot)
    pct = snapshot.policy.core_allocation_pct
    equity = snapshot.portfolio.equity
    if pct <= 0 or equity <= 0:
        return Decimal('0')
    current = sum((p.market_value for p in snapshot.portfolio.positions
                   if p.strategy == 'core_holding'), Decimal('0'))
    return max(equity * Decimal(str(pct / 100)) - current, Decimal('0'))


def evaluate_daily_loss(strategy: str, snapshot: EntryPolicySnapshot) -> PolicyDecision:
    _snapshot(snapshot)
    if type(strategy) is not str:
        raise ValueError('명시 전략이 필요합니다')
    pf, policy, trend = snapshot.portfolio, snapshot.policy, snapshot.trend
    pnl_pct = float(pf.effective_daily_pnl / pf.equity * 100) if pf.equity > 0 else 0.0
    limit = policy.daily_max_loss_pct
    hard = max(limit * 2.5, 5.0)
    effects = []

    def sidecar(active):
        if trend.sidecar_active != active:
            effects.append(_effect(snapshot, EffectKind.SIDECAR_SET, active))

    hit = pf.equity <= 0
    if pf.equity > 0:
        if -limit < pnl_pct <= -(limit * 0.7):
            if trend.present:
                sidecar(not trend.recovering)
                hit = not trend.recovering
            # 실제 소스는 경고구간 trend 결측을 허용한다.
        elif -hard < pnl_pct <= -limit:
            if trend.present and trend.recovering:
                sidecar(False)
                hit = strategy not in {'core_holding', 'sepa_trend'}
            else:
                if trend.present:
                    sidecar(True)
                hit = True
        elif pnl_pct <= -hard:
            hit = True
    if hit:
        if pnl_pct <= -hard:
            return _decision(snapshot, PolicyReason.DAILY_LOSS_HARD_STOP,
                f'일일 손실 한도 초과 ({pnl_pct:.1f}%) - 전면 차단', effects)
        return _decision(snapshot, PolicyReason.DAILY_LOSS_LIMIT,
            f'일일 손실 한도 도달 ({pnl_pct:.1f}%) - 방어적 전략만 허용', effects)
    return _decision(snapshot, effects=effects)


def evaluate_reentry(entry: EntryPolicyInput, snapshot: EntryPolicySnapshot, *, now: datetime) -> PolicyDecision:
    _input(entry, snapshot)
    _now(snapshot, now)
    reentry = snapshot.reentry
    symbol = entry.symbol
    fact = next((x for x in reentry.exited_today if x.symbol == symbol), None)
    stopped = symbol in reentry.stop_loss_today
    if stopped and symbol in reentry.rebound_used:
        return _decision(snapshot, PolicyReason.REBOUND_USED,
                         '당일 V자 반등 재진입 1회 제한 (재손절 후 차단)')
    if stopped and fact is None:
        return _decision(snapshot, PolicyReason.REBOUND_MISSING_EXIT,
                         '당일 손절 종목 재진입 금지 (청산 정보 없음)')
    if fact is None:
        return _decision(snapshot)
    elapsed = (now - fact.exited_at).total_seconds() / 60
    # legacy 경계값의 float 평가 순서를 그대로 유지한다.
    price, exit_price = float(entry.valuation_price), float(fact.price)
    if any(not math.isfinite(value) or value <= 0 for value in (price, exit_price)):
        raise ValueError('표현 가능한 재진입 가격이 필요합니다')
    change = (price - exit_price) / exit_price * 100
    if not math.isfinite(change):
        raise ValueError('표현 가능한 재진입 변화율이 필요합니다')
    if stopped:
        if elapsed < 30:
            return _decision(snapshot, PolicyReason.REBOUND_COOLDOWN,
                f'당일 손절 종목 재진입 금지 (쿨다운 미충족 ({elapsed:.0f}분/30분))')
        if change < 5:
            return _decision(snapshot, PolicyReason.REBOUND_PRICE,
                f'당일 손절 종목 재진입 금지 (반등 미달 (청산가 대비 {change:+.1f}% < +5%))')
        return _decision(snapshot)  # V 재진입의 일반 청산 분기 단축, token 소모 없음
    if elapsed < 30:
        return _decision(snapshot, PolicyReason.REENTRY_COOLDOWN,
            f'재진입 제한: 당일 청산 후 쿨다운 ({elapsed:.0f}분/30분)')
    if change < -5:
        return _decision(snapshot, PolicyReason.REENTRY_FALLING,
            f'재진입 제한: 급락 중 재진입 차단 (청산가 대비 {change:+.1f}%)')
    return _decision(snapshot)


def evaluate_risk_manager(entry: EntryPolicyInput, snapshot: EntryPolicySnapshot, *, now: datetime) -> PolicyDecision:
    """기존 RiskManager의 결과/도달한 상태 효과를 재현한다(KR 전용)."""
    _input(entry, snapshot)
    _now(snapshot, now)
    pf, policy = snapshot.portfolio, snapshot.policy
    today = now.astimezone(_KST).date()
    if entry.side is OrderSide.BUY and snapshot.macro.is_event and not snapshot.macro.lookup_failed:
        count = sum(p.entry_day == today for p in pf.positions)
        if count >= 1:
            return _decision(snapshot, PolicyReason.MACRO_LIMIT,
                f'매크로 이벤트({snapshot.macro.label}) — 변동성 방어로 신규 매수 1건 제한 (이미 {count}건 진입)')
    reentry = evaluate_reentry(entry, snapshot, now=now)
    if not reentry.allowed:
        return reentry
    effects = []
    sync = snapshot.sync
    if not sync.healthy:
        if sync.unhealthy_since is None:
            effects.append(_effect(snapshot, EffectKind.SYNC_START_REQUIRED, now))
            return _decision(snapshot, PolicyReason.SYNC_UNHEALTHY,
                f'동기화 복구 중 매수 차단 (연속 {sync.fail_count}회 실패)', effects)
        elapsed = (now - sync.unhealthy_since).total_seconds() / 60
        if elapsed >= sync.timeout_minutes:
            # legacy 결과 특성화만. owner가 적용하지 않은 상태로 송신하지 않는다.
            effects.append(_effect(snapshot, EffectKind.LEGACY_SYNC_TIMEOUT_OBSERVED, now))
        else:
            return _decision(snapshot, PolicyReason.SYNC_UNHEALTHY,
                f'동기화 복구 중 매수 차단 (연속 {sync.fail_count}회 실패, {elapsed:.1f}분)')
    daily = evaluate_daily_loss(entry.strategy, snapshot)
    if not daily.allowed:
        return _with_effects(daily, effects)
    effects.extend(daily.effects)

    def deny(reason, message):
        return _decision(snapshot, reason, message, effects)

    if entry.side is OrderSide.BUY and entry.strategy != 'core_holding' and policy.max_daily_new_buys > 0:
        count = sum(p.strategy != 'core_holding' and p.entry_day == today for p in pf.positions)
        if count >= policy.max_daily_new_buys:
            return deny(PolicyReason.DAILY_NEW_BUY_LIMIT,
                f'일일 신규 매수 한도 도달 ({count}/{policy.max_daily_new_buys}, 코어 제외)')
    core_count = sum(p.strategy == 'core_holding' for p in pf.positions)
    if entry.strategy == 'core_holding':
        if core_count >= policy.max_core_positions:
            return deny(PolicyReason.CORE_SLOT_LIMIT,
                f'코어홀딩 상한 도달 ({core_count}/{policy.max_core_positions}개)')
    else:
        weighted = 0.0
        raw = 0
        for fact in pf.positions:
            if fact.strategy != 'core_holding':
                raw += 1
                weighted += position_weight(fact)
        if weighted >= policy.max_positions:
            return deny(PolicyReason.POSITION_SLOT_LIMIT,
                f'최대 포지션 수 도달 (가중 {weighted:.1f}/{policy.max_positions}, raw={raw}, 코어 {core_count}개 제외)')
    minimum = pf.equity * Decimal(str(policy.min_cash_reserve_pct / 100))
    if pf.cash < minimum:
        return deny(PolicyReason.MINIMUM_CASH_RESERVE,
            f'최소 현금 보유 미달 ({pf.cash:,.0f} < {minimum:,.0f})')
    value = entry.valuation_price * entry.quantity
    maximum = pf.equity * Decimal(str(policy.max_position_pct / 100))
    if value > maximum:
        return deny(PolicyReason.POSITION_VALUE_LIMIT,
            f'포지션 크기 초과 ({value:,.0f} > {maximum:,.0f})')
    if entry.side is OrderSide.BUY:
        required, available = value * Decimal('1.001'), available_cash(snapshot, regime=False)
        if required > available:
            return deny(PolicyReason.CASH_INSUFFICIENT,
                f'현금 부족 ({available:,.0f} < {required:,.0f})')
    if entry.sector and policy.max_positions_per_sector > 0:
        count = sum(p.sector == entry.sector for p in pf.positions)
        if count >= policy.max_positions_per_sector:
            return deny(PolicyReason.SECTOR_LIMIT, f'Sector limit reached for {entry.sector}')
    if policy.daily_exit_cooldown_threshold > 0:
        count = snapshot.exit_cooldown.count
        if snapshot.exit_cooldown.day != today:
            count = 0
            effects.append(_effect(snapshot, EffectKind.DAILY_EXIT_ROLLOVER_REQUIRED, today))
        if count >= policy.daily_exit_cooldown_threshold:
            equity = float(pf.equity)
            pnl_pct = float(pf.effective_daily_pnl) / equity * 100 if equity > 0 else 0.0
            if pnl_pct < -1.0:
                return deny(PolicyReason.EXIT_LOSS_COOLDOWN,
                    f'당일 손실청산 {count}건 누적 + PnL {pnl_pct:+.1f}% < -1% → 차단')
    return _decision(snapshot, effects=effects)


def evaluate_engine(entry: EntryPolicyInput, snapshot: EntryPolicySnapshot, *, reserved_cash: Decimal) -> PolicyDecision:
    """기존 UnifiedEngine gate. reserved_cash의 provenance는 owner 책임이다."""
    _input(entry, snapshot)
    _money(reserved_cash, nonnegative=True)
    pf, policy = snapshot.portfolio, snapshot.policy
    pnl_pct = float(pf.effective_daily_pnl / pf.equity * 100) if pf.equity > 0 else 0.0
    hard = policy.daily_max_loss_pct * 2.5
    if pnl_pct <= -hard:
        return _decision(snapshot, PolicyReason.ENGINE_DAILY_HARD_CAP,
            f'일일 손실 하드캡 초과 (미실현포함={pnl_pct:.1f}%, 한도={-hard:.1f}%)')
    if entry.side is OrderSide.BUY:
        count = sum(p.side == 'buy' for p in snapshot.pending)
        if pf.daily_trades + count >= policy.daily_max_trades:
            return _decision(snapshot, PolicyReason.DAILY_TRADE_LIMIT,
                f'일일 거래 횟수 한도 초과 (체결 {pf.daily_trades} + 진행중 {count}/{policy.daily_max_trades})')
    held = {p.symbol for p in pf.positions}
    if entry.sector and policy.max_positions_per_sector > 0 and entry.symbol not in held:
        existing = [p.symbol for p in pf.positions if p.sector == entry.sector]
        existing += [p.symbol for p in snapshot.pending_sectors if p.sector == entry.sector
                     and p.symbol not in held and p.symbol != entry.symbol]
        if len(existing) >= policy.max_positions_per_sector:
            return _decision(snapshot, PolicyReason.SECTOR_LIMIT,
                f'섹터 포지션 한도 초과 ({entry.sector}: {len(existing)}/{policy.max_positions_per_sector}, 기존+pending={existing})')
    value = entry.valuation_price * entry.quantity
    maximum = pf.equity * Decimal(str(policy.max_position_pct / 100))
    if value > maximum:
        return _decision(snapshot, PolicyReason.POSITION_VALUE_LIMIT,
            f'포지션 크기 초과 ({value:,.0f} > {maximum:,.0f})')
    if entry.side is OrderSide.BUY:
        required = value * Decimal('1.001')
        available = available_cash(snapshot, regime=True) - reserved_cash
        if required > available:
            return _decision(snapshot, PolicyReason.CASH_INSUFFICIENT,
                f'현금 부족 ({available:,.0f} < {required:,.0f})')
    effects = (_effect(snapshot, EffectKind.PENDING_SECTOR_SET, entry.sector),) if entry.sector else ()
    return _decision(snapshot, effects=effects)


def evaluate_entry_policy(entry: EntryPolicyInput, snapshot: EntryPolicySnapshot, *, now: datetime, origin: EntryOrigin) -> PolicyDecision:
    """권한 검사 뒤 사용할 route 구성. 공통 dispatch/예약 검사를 대신하지 않는다.

    legacy sync timeout/일자 리셋은 미적용 효과를 통한 승인을 허용하지 않는다.
    sidecar/sector 효과는 owner가 명시 commit하고 마지막 guard에서 대조해야 한다.
    """
    _input(entry, snapshot)
    _now(snapshot, now, check_day=False)
    if type(origin) is not EntryOrigin:
        raise ValueError('신뢰 경로에서 확정된 EntryOrigin이 필요합니다')
    if now.astimezone(_KST).date() != snapshot.business_day:
        return _decision(snapshot, PolicyReason.POLICY_DAY_MISMATCH, '위험 snapshot KST 사업일 불일치')
    if entry.side is OrderSide.SELL or origin is EntryOrigin.USER:
        return _decision(snapshot)
    result = evaluate_risk_manager(entry, snapshot, now=now)
    if not result.allowed:
        return result
    if any(e.kind is EffectKind.DAILY_EXIT_ROLLOVER_REQUIRED for e in result.effects):
        return _decision(snapshot, PolicyReason.POLICY_DAY_MISMATCH,
                         '명시 일자 전환이 필요합니다', result.effects)
    if any(e.kind is EffectKind.LEGACY_SYNC_TIMEOUT_OBSERVED for e in result.effects):
        return _decision(snapshot, PolicyReason.POLICY_EFFECT_PENDING,
                         'legacy sync 효과 미반영', result.effects)
    if origin is EntryOrigin.SAFE_ASSET:
        return result
    reserved = sum((p.reserved_cash for p in snapshot.pending), Decimal('0'))
    if entry.strategy != 'core_holding':
        reserved += core_reserve(snapshot)
    return _with_effects(evaluate_engine(entry, snapshot, reserved_cash=reserved), result.effects)
