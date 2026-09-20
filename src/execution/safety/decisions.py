"""요청에 묶인 진입 판단 사실(immutable).

판단 시점에 굳는 값만 담는다. 현금·예약·노출·일손익·정책 수치 같은 "현재 경제"는
담지 않으며 재유도 때마다 owner snapshot에서 다시 읽는다. mutable SignalEvent/Order/
metadata에서는 아무것도 읽지 않는다. 새 직렬화기를 만들지 않고 기존 canonical/digest를
재사용한다. 이 모듈은 CV/LLM 임계값을 다시 계산하지 않고 기록값만 보관한다.
"""
from __future__ import annotations

from dataclasses import dataclass, fields
from datetime import datetime
from decimal import Decimal, DecimalException

from ...utils import position_sizing_kernel as k
from . import risk_policy as p
from .policy_generations import canonical
from .protection_recovery import digest as _sha256

_STOP_SOURCES = ('dynamic', 'strategy', 'global')


def _text(value, reason):
    if type(value) is not str or not value or value != value.strip():
        raise ValueError(reason)


def _number(value, reason, *, positive=False):
    # 0·0.0을 falsy로 거르지 않는다. 유한 float만 받는다.
    if type(value) is not float or value != value or value in (float('inf'), float('-inf')):
        raise ValueError(reason)
    if positive and value <= 0:
        raise ValueError(reason)


def _money(value, reason, *, positive=False):
    if type(value) is not Decimal or not value.is_finite() or value < 0:
        raise ValueError(reason)
    if positive and value == 0:
        raise ValueError(reason)


def _aware(value, reason):
    if type(value) is not datetime or value.utcoffset() is None:
        raise ValueError(reason)


def _row(row, kind, reason):
    if type(row) is not dict or row.keys() != {f.name for f in fields(kind)}:
        raise ValueError(reason)


def _time(row, key, reason):
    try:
        return datetime.fromisoformat(row[key])
    except (TypeError, ValueError):
        raise ValueError(reason) from None


def _decimal(row, key, reason):
    try:
        return Decimal(row[key])
    except (TypeError, ValueError, DecimalException):
        raise ValueError(reason) from None


@dataclass(frozen=True, slots=True)
class ConsumedSource:
    """판단이 실제로 소비한 출처의 게시 시점 신원. 미소비 출처는 담지 않는다."""
    name: str
    version: int
    as_of: datetime
    digest: str

    def __post_init__(self):
        _text(self.name, 'invalid_consumed_source')
        _text(self.digest, 'invalid_consumed_source')
        if type(self.version) is not int or self.version < 1:
            raise ValueError('invalid_consumed_source')
        _aware(self.as_of, 'invalid_consumed_source_time')

    def to_dict(self):
        return {'name': self.name, 'version': self.version,
                'as_of': self.as_of.isoformat(), 'digest': self.digest}

    @classmethod
    def from_dict(cls, row):
        _row(row, cls, 'invalid_consumed_source')
        return cls(row['name'], row['version'], _time(row, 'as_of', 'invalid_consumed_source_time'),
                   row['digest'])


@dataclass(frozen=True, slots=True)
class QualificationFacts:
    """CV/LLM 판단의 기록값. 여기서 규칙을 다시 적용하거나 감점하지 않는다."""
    signal_score: float
    original_score: float
    adjusted_score: float
    applied_rule_ids: tuple[str, ...]
    llm_verdict: str | None

    def __post_init__(self):
        for value in (self.signal_score, self.original_score, self.adjusted_score):
            _number(value, 'invalid_qualification_score')
        if type(self.applied_rule_ids) is not tuple:
            raise ValueError('invalid_qualification_rules')
        for rule in self.applied_rule_ids:
            _text(rule, 'invalid_qualification_rules')
        if len(set(self.applied_rule_ids)) != len(self.applied_rule_ids):
            raise ValueError('invalid_qualification_rules')
        if self.llm_verdict is not None:
            _text(self.llm_verdict, 'invalid_qualification_verdict')

    def to_dict(self):
        return {'signal_score': self.signal_score, 'original_score': self.original_score,
                'adjusted_score': self.adjusted_score, 'applied_rule_ids': list(self.applied_rule_ids),
                'llm_verdict': self.llm_verdict}

    @classmethod
    def from_dict(cls, row):
        _row(row, cls, 'invalid_qualification_facts')
        rules = row['applied_rule_ids']
        if type(rules) is not list:
            raise ValueError('invalid_qualification_rules')
        return cls(row['signal_score'], row['original_score'], row['adjusted_score'],
                   tuple(rules), row['llm_verdict'])


@dataclass(frozen=True, slots=True)
class EntryDecisionFacts:
    """AUTOMATIC BUY 한 건의 판단 시점 사실. 같은 intent로 다른 내용을 재게시할 수 없다."""
    intent_id: str
    symbol: str
    side: str
    strategy: str
    origin: str
    sector: str | None
    config_version: str
    decided_at: datetime
    expires_at: datetime
    base_pct: float
    strategy_allocation_pct: float | None
    min_position_value: Decimal
    strength_multiplier: float
    position_multiplier: float
    calendar_multiplier: float
    volatility_multiplier: float
    conviction_multiplier: float
    atr_pct: float | None
    stop_pct: Decimal | None
    stop_source: str | None
    stop_crash_capped: bool | None
    qualification: QualificationFacts
    sources: tuple[ConsumedSource, ...]

    def __post_init__(self):
        for value in (self.intent_id, self.symbol, self.strategy, self.config_version):
            _text(value, 'invalid_decision_identity')
        # 소비 범위는 자동 매수뿐이다. 다른 경로의 현행 동작을 이 계약에 끌어들이지 않는다.
        if self.side != 'buy' or self.origin != 'automatic':
            raise ValueError('unsupported_decision_scope')
        if self.sector is not None:
            _text(self.sector, 'invalid_decision_sector')
        _aware(self.decided_at, 'invalid_decision_time')
        _aware(self.expires_at, 'invalid_decision_time')
        if self.decided_at >= self.expires_at:
            raise ValueError('invalid_decision_expiry')
        _number(self.base_pct, 'invalid_decision_base_pct', positive=True)
        if self.strategy_allocation_pct is not None:
            _number(self.strategy_allocation_pct, 'invalid_decision_allocation', positive=True)
        _money(self.min_position_value, 'invalid_decision_min_position_value')
        for value in (self.strength_multiplier, self.position_multiplier, self.calendar_multiplier,
                      self.volatility_multiplier, self.conviction_multiplier):
            _number(value, 'invalid_decision_multiplier', positive=True)
        if self.atr_pct is not None:
            _number(self.atr_pct, 'invalid_decision_atr')
        if self.stop_pct is None:
            if self.stop_source is not None or self.stop_crash_capped is not None:
                raise ValueError('invalid_decision_stop')
        else:
            _money(self.stop_pct, 'invalid_decision_stop', positive=True)
            if self.stop_source not in _STOP_SOURCES or type(self.stop_crash_capped) is not bool:
                raise ValueError('invalid_decision_stop')
        if type(self.qualification) is not QualificationFacts:
            raise ValueError('invalid_qualification_facts')
        if type(self.sources) is not tuple or any(type(s) is not ConsumedSource for s in self.sources):
            raise ValueError('invalid_consumed_source')
        names = [source.name for source in self.sources]
        if len(set(names)) != len(names):
            raise ValueError('invalid_consumed_source')

    def to_dict(self):
        return {
            'intent_id': self.intent_id, 'symbol': self.symbol, 'side': self.side,
            'strategy': self.strategy, 'origin': self.origin, 'sector': self.sector,
            'config_version': self.config_version, 'decided_at': self.decided_at.isoformat(),
            'expires_at': self.expires_at.isoformat(), 'base_pct': self.base_pct,
            'strategy_allocation_pct': self.strategy_allocation_pct,
            'min_position_value': str(self.min_position_value),
            'strength_multiplier': self.strength_multiplier,
            'position_multiplier': self.position_multiplier,
            'calendar_multiplier': self.calendar_multiplier,
            'volatility_multiplier': self.volatility_multiplier,
            'conviction_multiplier': self.conviction_multiplier, 'atr_pct': self.atr_pct,
            'stop_pct': None if self.stop_pct is None else str(self.stop_pct),
            'stop_source': self.stop_source, 'stop_crash_capped': self.stop_crash_capped,
            'qualification': self.qualification.to_dict(),
            'sources': [source.to_dict() for source in self.sources],
        }

    @classmethod
    def from_dict(cls, row):
        _row(row, cls, 'invalid_decision_facts')
        values = dict(row)
        values['decided_at'] = _time(row, 'decided_at', 'invalid_decision_time')
        values['expires_at'] = _time(row, 'expires_at', 'invalid_decision_time')
        values['min_position_value'] = _decimal(row, 'min_position_value',
                                                'invalid_decision_min_position_value')
        if row['stop_pct'] is not None:
            values['stop_pct'] = _decimal(row, 'stop_pct', 'invalid_decision_stop')
        values['qualification'] = QualificationFacts.from_dict(row['qualification'])
        sources = row['sources']
        if type(sources) is not list:
            raise ValueError('invalid_consumed_source')
        values['sources'] = tuple(ConsumedSource.from_dict(source) for source in sources)
        result = cls(**values)
        # 날짜/금액까지 canonical DTO여야 하며 bool/int coercion을 사용하지 않는다.
        if result.to_dict() != row:
            raise ValueError('noncanonical_decision_facts')
        return result

    @property
    def digest(self) -> str:
        return _sha256(canonical(self.to_dict()))


def recompose_quantity(facts: EntryDecisionFacts, snapshot, *, price: Decimal):
    """판단 시점 사실 + 현재 snapshot으로 기존 wrapper 순서를 그대로 재실행한다.

    kernel에 새 phase를 추가하지 않는다. 경제값은 전부 snapshot에서 읽고 facts에서
    복사해 오지 않는다. 반환값은 계획 수량일 뿐 송신 허가가 아니다.
    """
    if type(facts) is not EntryDecisionFacts or type(snapshot) is not p.EntryPolicySnapshot:
        raise ValueError('invalid_decision_recompose_input')
    _money(price, 'invalid_decision_price', positive=True)
    policy, pf = snapshot.policy, snapshot.portfolio
    is_core = facts.strategy == 'core_holding'
    if policy.sizing_mode == 'risk' and not is_core and facts.stop_pct is None:
        # 손절 미확정 구간에서 nominal로 조용히 되돌아가 수량이 커지는 일을 막는다.
        raise ValueError('missing_decision_stop')
    core_actual = sum((fact.market_value for fact in pf.positions
                       if fact.strategy == 'core_holding'), Decimal('0'))
    reserve = p.core_reserve(snapshot)
    pool = pf.equity if is_core else max(pf.equity - (reserve if reserve > 0 else core_actual), Decimal('0'))
    reserved = sum((fact.reserved_cash for fact in snapshot.pending if fact.side == 'buy'), Decimal('0'))
    available = p.available_cash(snapshot, regime=True) - reserved
    if not is_core:
        available -= reserve
    nominal = k.NominalSizingInput(equity=pf.equity, pool_equity=pool, base_pct=facts.base_pct,
        strength_multiplier=facts.strength_multiplier, max_pct=policy.max_position_pct / 100,
        global_max_position_pct=policy.max_position_pct, available=available)
    risk = None
    if policy.sizing_mode == 'risk' and not is_core:
        risk = k.RiskSizingInput(equity=pf.equity, available=available,
            global_max_position_pct=policy.max_position_pct,
            risk_per_trade_pct=policy.risk_per_trade_pct,
            risk_max_position_pct=policy.risk_max_position_pct, stop_pct=facts.stop_pct)
    remaining = None
    if facts.strategy_allocation_pct is not None:
        held = sum((fact.market_value for fact in pf.positions
                    if fact.strategy == facts.strategy), Decimal('0'))
        pending = sum((fact.reserved_cash for fact in snapshot.pending
                       if fact.side == 'buy' and fact.strategy == facts.strategy), Decimal('0'))
        remaining = pf.equity * Decimal(str(facts.strategy_allocation_pct / 100)) - held - pending
    multiplier = facts.position_multiplier
    if k.should_skip_atr_multiplier(risk is not None, multiplier, facts.atr_pct):
        multiplier = 1.0
    final = k.FinalizeSizingInput(price=price, min_position_value=facts.min_position_value,
        buy_commission_rate=policy.buy_commission_rate,
        stop_pct=None if risk is None else facts.stop_pct, equity=pf.equity,
        risk_per_trade_pct=policy.risk_per_trade_pct)
    return k.compose_sizing(nominal, final, risk=risk, strategy_remaining=remaining,
        daily_loss=(pf.effective_daily_pnl, pf.equity, policy.daily_max_loss_pct),
        overlays=(('position', multiplier), ('calendar', facts.calendar_multiplier),
                  ('volatility', facts.volatility_multiplier),
                  ('conviction', facts.conviction_multiplier)))
