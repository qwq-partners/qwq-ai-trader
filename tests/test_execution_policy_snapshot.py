"""소유 checkpoint에서 계산한 정책 snapshot. 운영/외부 자료를 읽지 않는다."""
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest

from src.core.types import Portfolio, Position, PositionSide
from src.execution.safety import risk_policy as policy
from src.execution.safety.economics import encode_portfolio, new_risk_state
from src.execution.safety.protection import encode_protection
from src.strategies.exit_manager import ExitManager
from test_execution_risk_policy import snapshot, NOW


def api():
    from src.execution.safety.policy_snapshot import PolicyContext, build_owned_snapshot
    return PolicyContext, build_owned_snapshot


def baseline():
    exits = ExitManager(persist=False, clock=lambda: NOW)
    return {'portfolio': encode_portfolio(Portfolio(cash=Decimal('10000000'))),
            'risk': new_risk_state(NOW.date().isoformat()), 'protection': encode_protection(exits),
            'attempts': {}, 'entry_policy_effects': {'pending_sectors': {}}}


def test_context_round_trip_preserves_source_time_and_has_no_portfolio_cache():
    cls, build = api()
    original = snapshot(policy)
    ctx = cls.from_snapshot(original)
    payload = ctx.to_dict()
    assert not {'portfolio', 'pending', 'reentry', 'exit_cooldown'} & payload.keys()
    assert cls.from_dict(deepcopy(payload)) == ctx
    later = NOW + timedelta(minutes=5)
    state = baseline()
    state['portfolio']['cash'] = '8000000'
    result = build(state, context=ctx, version=8, now=later, prices={})
    assert result.portfolio.cash == Decimal('8000000')
    assert result.portfolio.equity == Decimal('8000000')
    assert result.captured_at == later and ctx.observed_at == NOW
    assert result.versions == policy.PolicyVersions(8, 8, 8, 8, original.versions.config,
                                                     original.versions.regime, original.versions.macro)


def test_owned_economics_risk_and_protection_override_stale_input_snapshot():
    cls, build = api()
    state = baseline()
    exits = ExitManager(persist=False, clock=lambda: NOW)
    pf = Portfolio(cash=Decimal('9000000'), daily_trades=2, daily_pnl=Decimal('-5000'),
                   positions={'005930': Position('005930', side=PositionSide.LONG, quantity=100,
                       avg_price=Decimal('10000'), current_price=Decimal('10000'),
                       strategy='sepa_trend', entry_time=NOW, sector='반도체')})
    exits.register_position(pf.positions['005930'])
    state['portfolio'], state['protection'] = encode_portfolio(pf), encode_protection(exits)
    state['risk']['daily_stats']['trades'] = 2
    state['risk']['stop_loss_today'] = ['OLD']
    state['risk']['stop_loss_rebound_used'] = ['OLD']
    state['risk']['exited_today'] = {'OLD': {'price': '12000', 'time': NOW.isoformat(), 'sector': ''}}
    result = build(state, context=cls.from_snapshot(snapshot(policy)), version=9, now=NOW,
                   prices={'005930': Decimal('11000')})
    assert result.portfolio.equity == Decimal('10100000')
    assert result.portfolio.effective_daily_pnl == Decimal('95000')
    assert result.portfolio.daily_trades == 2
    assert result.portfolio.positions[0].exit_state_valid is True
    assert result.portfolio.positions[0].exit_original_quantity == 100
    assert result.reentry.rebound_used == frozenset({'OLD'})
    assert result.reentry.exited_today[0].price == Decimal('12000')


def test_pending_comes_from_remaining_owner_reservations_and_self_is_excluded():
    cls, build = api()
    state = baseline()
    def pending(aid, symbol, quantity, cash, status='open'):
        return dict(attempt_id=aid, intent_id='intent-'+aid, symbol=symbol, kind='submit',
                    side='buy', strategy='sepa_trend', sector='반도체', state=status,
                    quantity=100, reserved_quantity=quantity, reserved_cash=cash)
    state['attempts'] = {'A': pending('A', '005930', 40, '406000'),
                         'B': pending('B', '000660', 100, '1015000'),
                         'Z': pending('Z', 'OTHER', 0, '0', 'final_filled')}
    state['entry_policy_effects']['pending_sectors'] = {'005930': '반도체', '000660': '반도체', 'SECTOR_ONLY': '철강'}
    result = build(state, context=cls.from_snapshot(snapshot(policy)), version=3, now=NOW,
                   prices={}, exclude_attempt='B')
    assert len(result.pending) == 1
    assert result.pending[0].attempt_id == 'A' and result.pending[0].reserved_cash == Decimal('406000')
    assert {p.symbol for p in result.pending_sectors} == {'005930', 'SECTOR_ONLY'}


@pytest.mark.parametrize('change', ['yesterday', 'future', 'naive', 'version_bool', 'missing_price',
                                    'extra_price', 'negative_price', 'risk_day', 'trade_count', 'unknown_field'])
def test_context_or_owner_mismatch_rejected_without_mutation(change):
    cls, build = api()
    ctx = cls.from_snapshot(snapshot(policy))
    state, prices, now, version = baseline(), {}, NOW, 3
    if change == 'yesterday': now += timedelta(days=1)
    elif change == 'future': now -= timedelta(seconds=1)
    elif change == 'naive': now = now.replace(tzinfo=None)
    elif change == 'version_bool': version = True
    elif change in ('missing_price', 'negative_price'):
        state['portfolio'] = encode_portfolio(Portfolio(positions={'005930': Position(
            '005930', side=PositionSide.LONG, quantity=1, avg_price=Decimal('10000'))}))
        if change == 'negative_price': prices = {'005930': Decimal('-1')}
    elif change == 'extra_price': prices['UNKNOWN'] = Decimal('1')
    elif change == 'risk_day': state['risk']['day'] = '2026-09-17'
    elif change == 'trade_count': state['risk']['daily_stats']['trades'] = 1
    else:
        payload = ctx.to_dict()
        payload['approve'] = True
        with pytest.raises(ValueError): cls.from_dict(payload)
        return
    before = deepcopy(state)
    with pytest.raises(ValueError): build(state, context=ctx, version=version, now=now, prices=prices)
    assert state == before


def test_committed_sidecar_effect_overrides_older_external_context():
    cls, build = api()
    state = baseline()
    state['entry_policy_effects']['sidecar_active'] = True
    original = snapshot(policy, trend=policy.MarketTrendPolicySnapshot(True, False, False))
    current = build(state, context=cls.from_snapshot(original), version=3, now=NOW, prices={})
    assert current.trend.sidecar_active is True
    assert original.trend.sidecar_active is False


@pytest.mark.parametrize('value', [1, 'false', None])
def test_invalid_committed_sidecar_effect_is_not_coerced(value):
    cls, build = api()
    state = baseline()
    state['entry_policy_effects']['sidecar_active'] = value
    with pytest.raises(ValueError):
        build(state, context=cls.from_snapshot(snapshot(policy)), version=3, now=NOW, prices={})


@pytest.mark.parametrize('status', ['final_filled', 'final_cancelled', 'final_rejected', 'final_expired'])
@pytest.mark.parametrize('extra, invalid', [
    ({'reserved_exposure': '1', 'reserved_planned_risk': None}, False),
    ({'reserved_exposure': '0', 'reserved_planned_risk': '1'}, False),
    ({'reserved_exposure': '1', 'reserved_planned_risk': '1'}, False),
    ({'reserved_exposure': '0'}, True),
    ({'reserved_planned_risk': None}, True),
    ({'reserved_exposure': 'NaN', 'reserved_planned_risk': '0'}, True),
    ({'reserved_exposure': '0', 'reserved_planned_risk': 'NaN'}, True),
    ({'reserved_exposure': True, 'reserved_planned_risk': '0'}, True),
])
def test_terminal_pending_cannot_hide_remaining_or_invalid_new_resources(status, extra, invalid):
    cls, build = api()
    state = baseline()
    state['attempts']['A'] = dict(attempt_id='A', intent_id='I', symbol='OLD', kind='submit',
        side='buy', strategy='', sector=None, state=status, quantity=10,
        reserved_quantity=0, reserved_cash='0', **extra)
    before = deepcopy(state)
    if invalid:
        with pytest.raises(ValueError):
            build(state, context=cls.from_snapshot(snapshot(policy)), version=3, now=NOW, prices={})
    else:
        result = build(state, context=cls.from_snapshot(snapshot(policy)), version=3, now=NOW, prices={})
        assert [pending.attempt_id for pending in result.pending] == ['A']
    assert state == before


@pytest.mark.parametrize('risk', ['0', None])
def test_terminal_measured_resources_exhausted_is_not_permanently_pending(risk):
    cls, build = api()
    state = baseline()
    state['attempts']['A'] = dict(attempt_id='A', intent_id='I', symbol='OLD', kind='submit',
        side='buy', strategy='', sector=None, state='final_filled', quantity=10,
        reserved_quantity=0, reserved_cash='0', reserved_exposure='0', reserved_planned_risk=risk)
    result = build(state, context=cls.from_snapshot(snapshot(policy)), version=3, now=NOW, prices={})
    assert not result.pending
