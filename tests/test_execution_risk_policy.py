"""실제 기존 gate와 순수 결정의 경계·효과를 비교한다(외부 I/O 없음)."""
from dataclasses import FrozenInstanceError, replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal as D
import builtins
import importlib
import importlib.util
from types import SimpleNamespace as NS
from zoneinfo import ZoneInfo

import pytest

from src.core.types import OrderSide, Portfolio, Position, PositionSide, RiskConfig
from src.core.engine import UnifiedEngine, RiskManager as EngineRiskManager
from src.risk.manager import RiskManager
import src.risk.manager as legacy_risk
import src.utils.macro_calendar as calendar

KST = ZoneInfo('Asia/Seoul')
NOW = datetime(2026, 9, 18, 11, tzinfo=KST)


@pytest.fixture
def p():
    assert importlib.util.find_spec('src.execution.safety.risk_policy') is not None, '순수 위험 정책 모듈 미구현'
    return importlib.import_module('src.execution.safety.risk_policy')


def snapshot(p, **changes):
    policy = p.EffectiveRiskPolicy(
        daily_max_loss_pct=5.0, daily_max_trades=10, max_daily_new_buys=5,
        max_positions=8, max_core_positions=3, max_position_pct=28.0,
        min_cash_reserve_pct=5.0, max_positions_per_sector=2,
        daily_exit_cooldown_threshold=3, regime_min_cash_reserve_pct=5.0,
        core_allocation_pct=0.0, sizing_mode='risk', risk_per_trade_pct=0.7,
        risk_max_position_pct=18.0, buy_commission_rate=D('0.000140527'), hybrid_enabled=False)
    value = p.EntryPolicySnapshot(
        versions=p.PolicyVersions(1, 2, 3, 4, 'effective-config', 5, 6),
        business_day=NOW.date(), captured_at=NOW, policy=policy,
        portfolio=p.PortfolioPolicySnapshot(D('10000000'), D('10000000'), D('0'), 0, ()),
        reentry=p.ReentryPolicySnapshot(frozenset(), frozenset(), ()),
        sync=p.SyncPolicySnapshot(True, 0, None, 10),
        trend=p.MarketTrendPolicySnapshot(False, False, False),
        macro=p.MacroPolicySnapshot(None, False, False),
        exit_cooldown=p.ExitCooldownPolicySnapshot(NOW.date(), 0), pending=(), pending_sectors=())
    if 'pending' in changes and 'pending_sectors' not in changes:
        changes['pending_sectors'] = tuple(p.PendingSectorFact(x.symbol, x.sector)
            for x in changes['pending'] if x.sector)
    return replace(value, **changes)


def entry(p, **changes):
    return replace(p.EntryPolicyInput('NEW', OrderSide.BUY, 10, D('10000'), 'sepa_trend', None), **changes)


def position(p, symbol='OLD', **changes):
    return replace(p.PositionPolicyFact(symbol, 'sepa_trend', None, 100,
        D('100000'), NOW.date(), None, None, None, False), **changes)


def portfolio_with(p, s, positions=(), **changes):
    cash = s.portfolio.equity - sum((x.market_value for x in positions), D('0'))
    return replace(s, portfolio=replace(s.portfolio, cash=cash, positions=positions, **changes))


def legacy_objects(s, monkeypatch, now=NOW):
    """실제 메서드는 유지하고 외부 시계/캘린더/로그만 고정한다."""
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return now.astimezone(tz) if tz else now.astimezone(KST).replace(tzinfo=None)
    class Day(date):
        @classmethod
        def today(cls):
            return now.astimezone(KST).date()
    monkeypatch.setattr(legacy_risk, 'datetime', Clock)
    monkeypatch.setattr(legacy_risk, 'date', Day)
    # can_open_position 내부의 지역 import(date as _d)까지 같은 외부 시계다.
    import datetime as datetime_module
    monkeypatch.setattr(datetime_module, 'date', Day)
    if s.macro.lookup_failed:
        def macro_error():
            raise RuntimeError('합성 calendar 실패')
        monkeypatch.setattr(calendar, 'is_macro_event_day', macro_error)
    else:
        monkeypatch.setattr(calendar, 'is_macro_event_day', lambda: (s.macro.label, s.macro.is_event))
    cfg = RiskConfig(**{key: getattr(s.policy, key) for key in (
        'daily_max_loss_pct', 'daily_max_trades', 'max_daily_new_buys', 'max_positions',
        'max_core_positions', 'max_position_pct', 'min_cash_reserve_pct',
        'max_positions_per_sector', 'daily_exit_cooldown_threshold')})
    positions = {}
    states = {}
    for f in s.portfolio.positions:
        price = f.market_value / f.quantity if f.quantity else D('0')
        positions[f.symbol] = Position(symbol=f.symbol, side=PositionSide.LONG,
            quantity=f.quantity, avg_price=price, current_price=price,
            strategy=f.strategy, sector=f.sector,
            entry_time=datetime.combine(f.entry_day, datetime.min.time()) if f.entry_day else None)
        if f.exit_state_valid:
            state = NS(current_stage=NS(value=f.exit_stage))
            if f.exit_original_quantity is not None:
                state.original_quantity = f.exit_original_quantity
            if f.exit_remaining_quantity is not None:
                state.remaining_quantity = f.exit_remaining_quantity
            states[f.symbol] = state
    pf = Portfolio(cash=s.portfolio.cash, initial_capital=s.portfolio.equity,
        daily_pnl=s.portfolio.effective_daily_pnl, daily_trades=s.portfolio.daily_trades,
        positions=positions)
    assert pf.total_equity == s.portfolio.equity
    rm = object.__new__(RiskManager)
    rm.market, rm.config = 'KR', cfg
    rm._stop_loss_today = set(s.reentry.stop_loss_today)
    rm._stop_loss_rebound_used = set(s.reentry.rebound_used)
    rm._exited_today = {x.symbol: {'price': float(x.price),
        'time': x.exited_at.astimezone(KST).replace(tzinfo=None).isoformat(), 'sector': ''}
        for x in s.reentry.exited_today}
    rm._sync_healthy, rm._sync_fail_count = s.sync.healthy, s.sync.fail_count
    rm._sync_unhealthy_since = s.sync.unhealthy_since.astimezone(KST).replace(tzinfo=None) if s.sync.unhealthy_since else None
    rm._sync_timeout_minutes = s.sync.timeout_minutes
    rm._last_sync_block_log = {}
    rm._sidecar_active = s.trend.sidecar_active
    rm._market_trend = {'recovering': s.trend.recovering} if s.trend.present else {}
    rm._daily_exit_count, rm._daily_exit_count_date = s.exit_cooldown.count, s.exit_cooldown.day
    rm._last_exit_cooldown_log = {}
    rm._exit_manager = NS(_states=states)
    eng = object.__new__(UnifiedEngine)
    eng.config, eng.portfolio = NS(risk=cfg), pf
    eng._regime_adapter = NS(get_params=lambda: {'min_cash_reserve_pct': s.policy.regime_min_cash_reserve_pct})
    eng._pending_sector_map = {x.symbol: x.sector for x in s.pending_sectors}
    eng.risk_manager = NS(_pending_sides={x.symbol: OrderSide(x.side) for x in s.pending})
    return rm, eng


def compare_rm(p, monkeypatch, s, e=None, now=NOW):
    e = e or entry(p)
    with monkeypatch.context() as m:
        rm, eng = legacy_objects(s, m, now)
        expected = rm.can_open_position(e.symbol, e.side, e.quantity, e.valuation_price,
            eng.portfolio, strategy_type=e.strategy, sector=e.sector)
    actual = p.evaluate_risk_manager(e, s, now=now)
    assert (actual.allowed, actual.legacy_reason) == expected
    assert actual.versions == s.versions
    effects = {x.kind.value: x.value for x in actual.effects}
    assert effects.get('sidecar_set', s.trend.sidecar_active) == rm._sidecar_active
    assert s.reentry.rebound_used == frozenset(rm._stop_loss_rebound_used)
    if rm._sync_healthy != s.sync.healthy:
        assert 'legacy_sync_timeout_observed' in effects
        assert rm._sync_fail_count == 0 and rm._sync_unhealthy_since is None
    if s.exit_cooldown.day != rm._daily_exit_count_date:
        assert effects['daily_exit_rollover_required'] == now.astimezone(KST).date()
        assert rm._daily_exit_count == 0
    return actual


def compare_engine(p, monkeypatch, s, e=None, reserved=D('0')):
    e = e or entry(p)
    with monkeypatch.context() as m:
        _, eng = legacy_objects(s, m)
        expected = eng.can_open_position(e.symbol, e.side, e.quantity, e.valuation_price,
            pending_symbols={x.symbol for x in s.pending}, reserved_cash=reserved, sector=e.sector)
    actual = p.evaluate_engine(e, s, reserved_cash=reserved)
    assert (actual.allowed, actual.legacy_reason) == expected
    assert any(x.kind.value == 'pending_sector_set' for x in actual.effects) == bool(actual.allowed and e.sector)
    return actual


@pytest.mark.parametrize('pnl', ['-349999', '-350000', '-350001', '-499999', '-500000', '-500001', '-1249999', '-1250000', '-1250001', '0'])
@pytest.mark.parametrize('trend', ['absent', 'recovering', 'falling'])
@pytest.mark.parametrize('strategy', ['core_holding', 'sepa_trend', 'rsi2_reversal', 'safe_asset'])
@pytest.mark.parametrize('active', [False, True])
def test_daily_loss_boundaries_match_real_manager(p, monkeypatch, pnl, trend, strategy, active):
    s = snapshot(p)
    s = replace(s, portfolio=replace(s.portfolio, effective_daily_pnl=D(pnl)),
        trend=p.MarketTrendPolicySnapshot(trend != 'absent', trend == 'recovering', active))
    result = compare_rm(p, monkeypatch, s, entry(p, strategy=strategy))
    with monkeypatch.context() as m:
        rm, eng = legacy_objects(s, m)
        hit = rm._is_daily_loss_limit_hit(eng.portfolio, strategy)
    assert p.evaluate_daily_loss(strategy, s).allowed == (not hit)
    if pnl == '-350000' and trend == 'absent':
        assert result.allowed


@pytest.mark.parametrize('stopped,used,seconds,price,allowed', [
    (True, False, 1799, '10500', False), (True, False, 1800, '10500', True),
    (True, False, 1800, '10499', False), (True, True, 1800, '11000', False),
    (False, False, 1799, '10000', False), (False, False, 1800, '9500', True),
    (False, False, 1800, '9499', False), (False, False, 1800, '10500', True),
    (False, False, 1800, '10501', True)])
def test_reentry_time_price_and_fill_only_token(p, monkeypatch, stopped, used, seconds, price, allowed):
    s = snapshot(p, reentry=p.ReentryPolicySnapshot(
        frozenset({'NEW'}) if stopped else frozenset(), frozenset({'NEW'}) if used else frozenset(),
        (p.ExitPolicyFact('NEW', D('10000'), NOW - timedelta(seconds=seconds)),)))
    assert compare_rm(p, monkeypatch, s, entry(p, valuation_price=D(price))).allowed == allowed
    assert p.evaluate_reentry(entry(p, valuation_price=D(price)), s, now=NOW).allowed == allowed


def test_rebound_missing_exit_and_later_cash_failure_do_not_consume(p, monkeypatch):
    s = snapshot(p, reentry=p.ReentryPolicySnapshot(frozenset({'NEW'}), frozenset(), ()))
    assert not compare_rm(p, monkeypatch, s).allowed
    s = replace(s, reentry=replace(s.reentry, exited_today=(p.ExitPolicyFact('NEW', D('10000'), NOW-timedelta(hours=1)),)))
    assert not compare_rm(p, monkeypatch, s, entry(p, valuation_price=D('11000'), quantity=1000)).allowed
    assert not s.reentry.rebound_used


@pytest.mark.parametrize('seconds,legacy_allowed', [(None, False), (599, False), (600, True), (601, True)])
def test_sync_legacy_effect_is_not_execution_unlock(p, monkeypatch, seconds, legacy_allowed):
    s = snapshot(p, sync=p.SyncPolicySnapshot(False, 3,
        None if seconds is None else NOW-timedelta(seconds=seconds), 10))
    actual = compare_rm(p, monkeypatch, s)
    assert actual.allowed == legacy_allowed
    composed = p.evaluate_entry_policy(entry(p), s, now=NOW, origin=p.EntryOrigin.AUTOMATIC)
    assert not composed.allowed
    if legacy_allowed:
        assert composed.reason.value == 'policy_effect_pending'
    if seconds is None:
        assert actual.effects[0].kind.value == 'sync_start_required'


@pytest.mark.parametrize('strategy,today,failed,allowed', [('core_holding', True, False, False), ('manual', True, False, False), ('sepa_trend', False, False, True), ('manual', True, True, True)])
def test_macro_counts_all_today_holdings_and_keeps_error_fallback(p, monkeypatch, strategy, today, failed, allowed):
    s = snapshot(p, macro=p.MacroPolicySnapshot('행사', True, failed))
    s = portfolio_with(p, s, (position(p, strategy=strategy, entry_day=NOW.date() if today else NOW.date()-timedelta(days=1)),))
    result = compare_rm(p, monkeypatch, s)
    assert result.allowed == allowed
    if not allowed:
        assert not result.effects


@pytest.mark.parametrize('original,remaining,stage,valid', [(100, 100, 'none', True), (100, 1, 'first', True), (100, 1, 'second', True), (100, 1, 'third', True), (100, 1, 'trailing', True), (100, 400, 'trailing', True), (0, 0, 'none', True), (-1, -1, 'none', True), (None, None, None, True), (None, None, None, False)])
def test_position_weight_real_helper_floors_and_fallback(p, monkeypatch, original, remaining, stage, valid):
    f = position(p, exit_original_quantity=original, exit_remaining_quantity=remaining, exit_stage=stage, exit_state_valid=valid)
    s = portfolio_with(p, snapshot(p), (f,))
    with monkeypatch.context() as m:
        rm, _ = legacy_objects(s, m)
        assert p.position_weight(f) == rm._get_position_weight(f.symbol)


@pytest.mark.parametrize('last,allowed', [(90, True), (100, False)])
def test_weighted_slot_checks_existing_not_candidate(p, monkeypatch, last, allowed):
    facts = tuple(position(p, str(i), strategy='manual', entry_day=None) for i in range(7)) + (position(p, 'TAIL', entry_day=None, exit_original_quantity=100, exit_remaining_quantity=last, exit_stage='none', exit_state_valid=True),)
    s = portfolio_with(p, snapshot(p), facts)
    assert compare_rm(p, monkeypatch, s).allowed == allowed
    assert compare_engine(p, monkeypatch, s).allowed  # engine에 hard slot 없음


def test_core_slots_and_new_buy_count_are_separate(p, monkeypatch):
    facts = tuple(position(p, str(i), strategy='core_holding') for i in range(3))
    s = portfolio_with(p, snapshot(p), facts)
    assert compare_rm(p, monkeypatch, s).allowed
    assert not compare_rm(p, monkeypatch, s, entry(p, strategy='core_holding')).allowed
    s = portfolio_with(p, snapshot(p), tuple(position(p, str(i)) for i in range(5)))
    assert not compare_rm(p, monkeypatch, s).allowed
    assert compare_rm(p, monkeypatch, s, entry(p, strategy='core_holding')).allowed
    assert compare_rm(p, monkeypatch, replace(s, policy=replace(s.policy, max_daily_new_buys=0))).allowed


def pending(p, symbol='PENDING', side='buy', sector=None):
    return p.PendingPolicyFact('a-'+symbol, 'i-'+symbol, symbol, side, 'sepa_trend', sector, D('101500'))


@pytest.mark.parametrize('trades,pending_side,side,allowed', [(8, 'buy', OrderSide.BUY, True), (9, 'buy', OrderSide.BUY, False), (9, 'sell', OrderSide.BUY, True), (10, 'buy', OrderSide.SELL, True)])
def test_engine_daily_trade_pending_count_and_sell_exemption(p, monkeypatch, trades, pending_side, side, allowed):
    s = snapshot(p, pending=(pending(p, side=pending_side),))
    s = replace(s, portfolio=replace(s.portfolio, daily_trades=trades))
    assert compare_engine(p, monkeypatch, s, entry(p, side=side)).allowed == allowed


@pytest.mark.parametrize('cash_delta,allowed', [('0', True), ('-0.01', False)])
def test_cash_1001_exact_boundary_and_reserve_sources(p, monkeypatch, cash_delta, allowed):
    s = snapshot(p)
    # equity 고정, 나머지는 보유 평가금액: available=100100±delta
    cash = D('600100')+D(cash_delta)
    f = position(p, market_value=D('10000000')-cash, entry_day=None)
    s = portfolio_with(p, s, (f,))
    assert compare_rm(p, monkeypatch, s).allowed == allowed
    assert compare_engine(p, monkeypatch, s).allowed == allowed
    s = replace(s, policy=replace(s.policy, regime_min_cash_reserve_pct=0.0))
    assert p.available_cash(s, regime=False) == cash-D('500000')
    assert p.available_cash(s, regime=True) == cash
    assert compare_engine(p, monkeypatch, s).allowed


@pytest.mark.parametrize('quantity,allowed', [(280, True), (281, False)])
def test_position_value_nominal_cap_is_not_per_intent_risk(p, monkeypatch, quantity, allowed):
    assert compare_rm(p, monkeypatch, snapshot(p), entry(p, quantity=quantity)).allowed == allowed
    assert compare_engine(p, monkeypatch, snapshot(p), entry(p, quantity=quantity)).allowed == allowed


@pytest.mark.parametrize('allocation,core_value,want', [(0.0, '0', '0'), (30.0, '1000000', '2000000'), (30.0, '4000000', '0')])
def test_core_reserve_real_engine_helper(p, monkeypatch, allocation, core_value, want):
    s = snapshot(p)
    s = replace(s, policy=replace(s.policy, core_allocation_pct=allocation))
    s = portfolio_with(p, s, (position(p, strategy='core_holding', market_value=D(core_value)),))
    with monkeypatch.context() as m:
        _, eng = legacy_objects(s, m)
        rm = object.__new__(EngineRiskManager)
        rm.config, rm.engine = NS(strategy_allocation={'core_holding': allocation}), eng
        assert p.core_reserve(s) == rm._get_core_reserve() == D(want)
        assert p.available_cash(s, regime=True) == eng.get_available_cash()


def test_sector_gate_order_and_pending_effect(p, monkeypatch):
    s = snapshot(p, pending=(pending(p, 'A', sector='tech'), pending(p, 'B', sector='tech')))
    e = entry(p, sector='tech')
    assert compare_rm(p, monkeypatch, s, e).allowed
    assert not compare_engine(p, monkeypatch, s, e).allowed
    s = replace(s, pending=(pending(p, 'NEW', sector='tech'),),
        pending_sectors=(p.PendingSectorFact('NEW', 'tech'),))
    assert compare_engine(p, monkeypatch, s, e).allowed
    assert not compare_engine(p, monkeypatch, s, replace(e, quantity=1000)).allowed
    # 정상 sector effect만으로 composition을 영구 차단하지 않는다.
    assert p.evaluate_entry_policy(e, s, now=NOW, origin=p.EntryOrigin.AUTOMATIC).allowed


@pytest.mark.parametrize('threshold,count,pnl,old_day,allowed', [(0, 3, '-100001', False, True), (3, 2, '-100001', False, True), (3, 3, '-100000', False, True), (3, 3, '-100001', False, False), (3, 3, '-100001', True, True)])
def test_exit_cooldown_strict_loss_and_rollover_effect(p, monkeypatch, threshold, count, pnl, old_day, allowed):
    s = snapshot(p)
    s = replace(s, policy=replace(s.policy, daily_exit_cooldown_threshold=threshold),
        portfolio=replace(s.portfolio, effective_daily_pnl=D(pnl)),
        exit_cooldown=p.ExitCooldownPolicySnapshot(NOW.date()-timedelta(days=int(old_day)), count))
    assert compare_rm(p, monkeypatch, s).allowed == allowed
    if old_day:
        assert not p.evaluate_entry_policy(entry(p), s, now=NOW, origin=p.EntryOrigin.AUTOMATIC).allowed


def test_route_selection_preserves_legacy_scope_without_issuing_authority(p):
    s = snapshot(p)
    s = replace(s, portfolio=replace(s.portfolio, daily_trades=10))
    assert not p.evaluate_entry_policy(entry(p), s, now=NOW, origin=p.EntryOrigin.AUTOMATIC).allowed
    assert p.evaluate_entry_policy(entry(p, strategy='safe_asset'), s, now=NOW, origin=p.EntryOrigin.SAFE_ASSET).allowed
    s = replace(s, portfolio=replace(s.portfolio, effective_daily_pnl=D('-2000000')))
    assert not p.evaluate_entry_policy(entry(p, strategy='safe_asset'), s, now=NOW, origin=p.EntryOrigin.SAFE_ASSET).allowed
    assert p.evaluate_entry_policy(entry(p, strategy='manual'), s, now=NOW, origin=p.EntryOrigin.USER).allowed
    assert p.evaluate_entry_policy(entry(p, side=OrderSide.SELL), s, now=NOW, origin=p.EntryOrigin.AUTOMATIC).allowed
    with pytest.raises(ValueError):
        p.evaluate_entry_policy(entry(p), s, now=NOW, origin='user')


def test_pure_no_clock_io_and_immutable_inputs(p, monkeypatch):
    s, e = snapshot(p), entry(p, sector='tech')
    before = hash(s), hash(e)
    def forbidden(*args, **kwargs):
        raise AssertionError('순수 평가에서 외부 호출')
    class NoClock(datetime):
        now = forbidden
    with monkeypatch.context() as m:
        m.setattr(builtins, 'open', forbidden)
        m.setattr(calendar, 'is_macro_event_day', forbidden)
        m.setattr(p, 'datetime', NoClock)
        a = p.evaluate_entry_policy(e, s, now=NOW, origin=p.EntryOrigin.AUTOMATIC)
        b = p.evaluate_entry_policy(e, s, now=NOW.astimezone(timezone.utc), origin=p.EntryOrigin.AUTOMATIC)
    assert a == b and a.allowed and before == (hash(s), hash(e))
    with pytest.raises(FrozenInstanceError):
        s.business_day = NOW.date()


@pytest.mark.parametrize('mutation', ['naive_now', 'future_snapshot', 'bad_quantity', 'nan_price', 'mutable_positions', 'nan_policy', 'bad_version', 'naive_exit'])
def test_invalid_or_unmeasured_input_never_allows(p, mutation):
    with pytest.raises(ValueError):
        s, e, now = snapshot(p), entry(p), NOW
        if mutation == 'naive_now': now = NOW.replace(tzinfo=None)
        if mutation == 'future_snapshot': s = replace(s, captured_at=NOW+timedelta(seconds=1))
        if mutation == 'bad_quantity': e = replace(e, quantity=True)
        if mutation == 'nan_price': e = replace(e, valuation_price=D('NaN'))
        if mutation == 'mutable_positions': s = replace(s, portfolio=replace(s.portfolio, positions=[]))
        if mutation == 'nan_policy': s = replace(s, policy=replace(s.policy, max_position_pct=float('nan')))
        if mutation == 'bad_version': s = replace(s, versions=replace(s.versions, execution=True))
        if mutation == 'naive_exit': s = replace(s, reentry=replace(s.reentry, exited_today=(p.ExitPolicyFact('NEW', D('1'), NOW.replace(tzinfo=None)),)))
        p.evaluate_entry_policy(e, s, now=now, origin=p.EntryOrigin.AUTOMATIC)


def test_next_kst_day_returns_structured_hold_without_rollover(p):
    s = snapshot(p)
    result = p.evaluate_entry_policy(entry(p), s, now=NOW+timedelta(days=1), origin=p.EntryOrigin.AUTOMATIC)
    assert not result.allowed and result.reason.value == 'policy_day_mismatch'
    assert not result.effects and s.business_day == NOW.date()


@pytest.mark.parametrize('equity', ['0', '-1'])
def test_nonpositive_equity_matches_legacy_rejection(p, monkeypatch, equity):
    s = snapshot(p, portfolio=p.PortfolioPolicySnapshot(D(equity), D(equity), D('0'), 0, ()))
    assert not compare_rm(p, monkeypatch, s).allowed
    assert not compare_engine(p, monkeypatch, s).allowed


def test_engine_small_loss_limit_keeps_its_distinct_hard_cap(p, monkeypatch):
    s = snapshot(p)
    s = replace(s, policy=replace(s.policy, daily_max_loss_pct=1.0),
        portfolio=replace(s.portfolio, effective_daily_pnl=D('-300000')),
        trend=p.MarketTrendPolicySnapshot(True, True, False))
    assert compare_rm(p, monkeypatch, s).allowed  # 방어전략, RM hard floor5%
    assert not compare_engine(p, monkeypatch, s).allowed  # engine hard2.5%


def test_daily_loss_uses_effective_pnl_not_realized_only(p, monkeypatch):
    s = snapshot(p, trend=p.MarketTrendPolicySnapshot(True, False, False))
    s = portfolio_with(p, s, (position(p, market_value=D('1000000')),), effective_daily_pnl=D('-350000'))
    with monkeypatch.context() as m:
        rm, eng = legacy_objects(s, m)
        eng.portfolio.daily_pnl = D('0')
        eng.portfolio.daily_start_unrealized_pnl = D('350000')
        old = rm.can_open_position('NEW', OrderSide.BUY, 10, D('10000'), eng.portfolio, strategy_type='sepa_trend')
    new = p.evaluate_risk_manager(entry(p), s, now=NOW)
    assert (new.allowed, new.legacy_reason) == old and not new.allowed


def test_minimum_cash_precedes_value_and_does_not_use_regime(p, monkeypatch):
    s = portfolio_with(p, snapshot(p), (position(p, market_value=D('9600000')),))
    s = replace(s, policy=replace(s.policy, regime_min_cash_reserve_pct=0.0))
    result = compare_rm(p, monkeypatch, s, entry(p, quantity=1000))
    assert result.reason.value == 'minimum_cash_reserve'
    assert compare_engine(p, monkeypatch, s).allowed


def test_dynamic_flex_settings_are_not_an_engine_gate(p, monkeypatch):
    facts = tuple(position(p, str(i), entry_day=None) for i in range(8))
    s = portfolio_with(p, snapshot(p), facts)
    for dynamic, extra in ((True, 2), (False, 0)):
        with monkeypatch.context() as m:
            _, eng = legacy_objects(s, m)
            eng.config.risk.dynamic_max_positions = dynamic
            eng.config.risk.flex_extra_positions = extra
            old = eng.can_open_position('NEW', OrderSide.BUY, 10, D('10000'))
        result = p.evaluate_engine(entry(p), s, reserved_cash=D('0'))
        assert (result.allowed, result.legacy_reason) == old and result.allowed


def test_existing_symbol_sector_differs_between_real_gates(p, monkeypatch):
    s = portfolio_with(p, snapshot(p), (position(p, 'NEW', sector='tech'), position(p, 'OLD', sector='tech')))
    e = entry(p, sector='tech')
    assert not compare_rm(p, monkeypatch, s, e).allowed
    assert compare_engine(p, monkeypatch, s, e).allowed


def test_pending_held_sector_is_not_double_counted(p, monkeypatch):
    s = snapshot(p, pending=(pending(p, 'OLD', sector='tech'),))
    s = portfolio_with(p, s, (position(p, 'OLD', sector='tech'),))
    assert compare_engine(p, monkeypatch, s, entry(p, sector='tech')).allowed


def test_sidecar_effect_survives_later_cash_rejection_without_mutation(p, monkeypatch):
    s = snapshot(p, trend=p.MarketTrendPolicySnapshot(True, True, True))
    s = replace(s, portfolio=replace(s.portfolio, effective_daily_pnl=D('-350000')))
    result = compare_rm(p, monkeypatch, s, entry(p, quantity=1000))
    assert not result.allowed and result.effects[0].value is False
    assert s.trend.sidecar_active


def test_earlier_rejection_produces_no_sync_or_rollover_effect(p, monkeypatch):
    s = snapshot(p, reentry=p.ReentryPolicySnapshot(frozenset({'NEW'}), frozenset({'NEW'}), ()),
        sync=p.SyncPolicySnapshot(False, 3, NOW-timedelta(hours=1), 10),
        exit_cooldown=p.ExitCooldownPolicySnapshot(NOW.date()-timedelta(days=1), 3))
    assert not compare_rm(p, monkeypatch, s).effects


def test_auto_noncore_reserves_pending_and_core_cash_but_core_does_not(p):
    s = snapshot(p, pending=(pending(p),))
    s = replace(s, policy=replace(s.policy, core_allocation_pct=94.0))
    # 9.5m 가용−9.4m core−101500 pending이면 비코어 부족.
    assert not p.evaluate_entry_policy(entry(p), s, now=NOW, origin=p.EntryOrigin.AUTOMATIC).allowed
    assert p.evaluate_entry_policy(entry(p, strategy='core_holding'), s, now=NOW, origin=p.EntryOrigin.AUTOMATIC).allowed


@pytest.mark.parametrize('kind', ['effect', 'decision'])
def test_result_contract_cannot_hold_mutable_payloads(p, kind):
    s = snapshot(p)
    with pytest.raises(ValueError):
        if kind == 'effect':
            p.PolicyEffect(p.EffectKind.SIDECAR_SET, s.versions, [])
        else:
            p.PolicyDecision(True, p.PolicyReason.ALLOWED, '', s.versions, [])


def test_sector_only_pending_does_not_consume_daily_buy_slot(p, monkeypatch):
    s = snapshot(p)
    s = replace(s, portfolio=replace(s.portfolio, daily_trades=9),
        pending_sectors=(p.PendingSectorFact('A', 'tech'), p.PendingSectorFact('B', 'tech')))
    assert compare_engine(p, monkeypatch, s).allowed
    result = compare_engine(p, monkeypatch, s, entry(p, sector='tech'))
    assert not result.allowed and result.reason.value == 'sector_limit'


def test_pending_side_without_sector_map_is_not_sector_reservation(p, monkeypatch):
    s = snapshot(p, pending=(pending(p, 'A', sector='tech'), pending(p, 'B', sector='tech')),
        pending_sectors=())
    assert compare_engine(p, monkeypatch, s, entry(p, sector='tech')).allowed


def test_unrepresentable_weight_keeps_legacy_fallback(p, monkeypatch):
    fact = position(p, exit_original_quantity=1, exit_remaining_quantity=10**1000,
        exit_stage='none', exit_state_valid=True)
    s = portfolio_with(p, snapshot(p), (fact,))
    with monkeypatch.context() as m:
        rm, _ = legacy_objects(s, m)
        assert rm._get_position_weight('OLD') == 1.0
    assert p.position_weight(fact) == 1.0


def test_unrepresentable_policy_number_is_explicit_input_error(p):
    with pytest.raises(ValueError):
        replace(snapshot(p).policy, max_position_pct=10**1000)


def test_blank_strategy_from_request_remains_valid_pending(p, monkeypatch):
    from test_execution_requests import submit
    request = submit()
    assert request.strategy == ''
    fact = p.PendingPolicyFact('empty-strategy', 'intent', 'OLD', 'buy', request.strategy, None, D('101500'))
    current = snapshot(p, pending=(fact,))
    assert compare_rm(p, monkeypatch, current, entry(p, strategy='')).allowed
    assert compare_engine(p, monkeypatch, current, entry(p, strategy=''), reserved=D('101500')).allowed


@pytest.mark.parametrize('field', ['exit', 'entry'])
@pytest.mark.parametrize('value', ['1e1000', '1e-1000'])
def test_unrepresentable_reentry_prices_are_explicit_input_errors(p, field, value):
    fact = p.ExitPolicyFact('NEW', D(value if field == 'exit' else '10000'), NOW-timedelta(hours=1))
    current = snapshot(p, reentry=p.ReentryPolicySnapshot(frozenset(), frozenset(), (fact,)))
    candidate = entry(p, valuation_price=D(value if field == 'entry' else '10000'))
    with pytest.raises(ValueError):
        p.evaluate_entry_policy(candidate, current, now=NOW, origin=p.EntryOrigin.AUTOMATIC)


def test_reentry_calculation_overflow_is_not_an_allowed_numeric_result(p):
    fact = p.ExitPolicyFact('NEW', D('1e-300'), NOW-timedelta(hours=1))
    current = snapshot(p, reentry=p.ReentryPolicySnapshot(frozenset(), frozenset(), (fact,)))
    with pytest.raises(ValueError):
        p.evaluate_reentry(entry(p, valuation_price=D('1e300')), current, now=NOW)
