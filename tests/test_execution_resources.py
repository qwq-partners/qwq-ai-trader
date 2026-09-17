"""실제 준비 요청·수수료·기존 위험 산식의 순수 자원 계약."""
from copy import deepcopy
from dataclasses import FrozenInstanceError, asdict, replace
from datetime import datetime
from decimal import Decimal as D, localcontext
import builtins
import importlib
import importlib.util
import json
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from src.core.types import Order, OrderSide, OrderType
from src.execution.safety.guards import EntryOrigin
from src.execution.safety.lifecycle import CommandKind, OrderRef
from src.execution.safety.requests import CancelParent, KISRequestBuilder, RequestAccount, RequestSession
from src.execution.safety.risk_policy import EffectiveRiskPolicy
from src.utils.fee_calculator import FeeCalculator, FeeConfig
from src.utils.sizing import planned_risk, risk_quantity_cap
from src.utils.stop_policy import StopDecision

NOW = datetime(2026, 9, 18, 11, tzinfo=ZoneInfo('Asia/Seoul'))
STOP = StopDecision(D('5'), 'strategy', False)
EQUITY = D('10000000')


@pytest.fixture
def resources():
    assert importlib.util.find_spec('src.execution.safety.resources') is not None, '순수 요청 자원 모듈 미구현'
    return importlib.import_module('src.execution.safety.resources')


def policy(**changes):
    return replace(EffectiveRiskPolicy(
        daily_max_loss_pct=5.0, daily_max_trades=10, max_daily_new_buys=5,
        max_positions=8, max_core_positions=3, max_position_pct=28.0,
        min_cash_reserve_pct=5.0, max_positions_per_sector=2,
        daily_exit_cooldown_threshold=3, regime_min_cash_reserve_pct=5.0,
        core_allocation_pct=0.0, sizing_mode='risk', risk_per_trade_pct=0.7,
        risk_max_position_pct=18.0, buy_commission_rate=D('0.000140527')), **changes)


def builder():
    return KISRequestBuilder(RequestAccount('test-scope', 'SYNTHETIC_ACCOUNT',
        'SYNTHETIC_PRODUCT', 'prod', 'https://openapi.koreainvestment.com:9443', 3))


def request(b, *, quantity=139, valuation=D('10000'), price=D('10000'),
            side=OrderSide.BUY, order_type=OrderType.MARKET, strategy='sepa_trend'):
    return b.prepare_submit(Order(symbol='005930', side=side, order_type=order_type,
        quantity=quantity, price=price, strategy=strategy), intent_id='I', attempt_id='A',
        session=RequestSession('2026-09-18', NOW, 'regular'), valuation_price=valuation)


def calculate(resources, req=None, *, b=None, **changes):
    b = b or builder()
    args = dict(builder=b, policy=policy(), equity=EQUITY,
        origin=EntryOrigin.AUTOMATIC, stop_decision=STOP)
    args.update(changes)
    return resources.calculate_resources(req or request(b), **args)


def test_market_139_uses_fee_risk_and_distinct_pending_buffer(resources):
    b = builder()
    req = request(b)
    plan = calculate(resources, req, b=b)
    assert req.wire_price == D('0') and plan.valuation_price == D('10000')
    assert plan.quantity == 139 and plan.exposure == D('1390000')
    assert plan.cash == D('1410850')  # 1.015, 시장가 sizing1.3/gate1.001 아님
    assert plan.planned_risk == D('69509.75')
    assert plan.request_fingerprint == req.fingerprint and plan.risk_applicable
    assert plan.stop_pct == D('5') and plan.stop_source == 'strategy'
    assert plan.stop_crash_active is False and plan.fee_rate == D('0.000140527')
    fee = FeeCalculator(FeeConfig(buy_commission_rate=plan.fee_rate))
    assert plan.planned_risk == planned_risk(D('10000'), 139, D('5'), fee)
    assert risk_quantity_cap(EQUITY, D('10000'), D('5'), risk_per_trade_pct=0.7, fee_calc=fee) == 139


def test_140_is_rejected_by_existing_per_intent_risk_not_resized(resources):
    b = builder()
    req = request(b, quantity=140)
    with pytest.raises(resources.ResourceValidationError, match='^risk_budget_exceeded$'):
        calculate(resources, req, b=b)
    assert req.quantity == 140 and req.body()['ORD_QTY'] == '140'


@pytest.mark.parametrize('wire_input,valuation,expected', [('10026', '9000', '10050'), ('10025', '10100', '10100'), ('10025', '9000', '10000')])
def test_limit_uses_max_of_actual_rounded_wire_and_valuation(resources, wire_input, valuation, expected):
    b = builder()
    plan = calculate(resources, request(b, quantity=10, order_type=OrderType.LIMIT,
        price=D(wire_input), valuation=D(valuation)), b=b)
    assert plan.valuation_price == D(expected)
    assert plan.exposure == D(expected)*10
    assert plan.cash == D(expected)*D('10.15')


def test_limit_understated_quote_cannot_avoid_actual_wire_risk_cap(resources):
    b = builder()
    req = request(b, quantity=139, order_type=OrderType.LIMIT, price=D('20000'), valuation=D('10000'))
    with pytest.raises(resources.ResourceValidationError):
        calculate(resources, req, b=b)


def test_cash_covers_fee_above_legacy_pending_buffer(resources):
    b = builder()
    plan = calculate(resources, request(b, quantity=100), b=b,
        policy=policy(buy_commission_rate=D('0.02')))
    assert plan.cash == D('1020000') and plan.planned_risk == D('51000')


def test_fee_rounding_is_existing_round_half_up(resources):
    b = builder()
    plan = calculate(resources, request(b, quantity=1, valuation=D('10000')), b=b,
        policy=policy(buy_commission_rate=D('0.00005')))
    assert plan.planned_risk == D('500.05')  # 0.5원 수수료를1원으로


@pytest.mark.parametrize('strategy,origin,mode', [('core_holding', EntryOrigin.AUTOMATIC, 'risk'),
    ('manual', EntryOrigin.USER, 'risk'), ('safe_asset', EntryOrigin.SAFE_ASSET, 'risk'),
    ('sepa_trend', EntryOrigin.AUTOMATIC, 'nominal')])
def test_existing_exempt_routes_do_not_gain_automatic_caps(resources, strategy, origin, mode):
    b = builder()
    req = request(b, quantity=500, strategy=strategy)
    plan = calculate(resources, req, b=b, origin=origin, policy=policy(sizing_mode=mode), stop_decision=None)
    assert plan.exposure == D('5000000') and plan.cash == D('5075000')
    assert not plan.risk_applicable and plan.planned_risk is None
    assert plan.stop_pct is None and plan.stop_source is None and plan.stop_crash_active is None
    measured = calculate(resources, req, b=b, origin=origin, policy=policy(sizing_mode=mode))
    assert measured.planned_risk == D('250035.15') and not measured.risk_applicable


def test_strategy_manual_string_does_not_issue_user_authority(resources):
    b = builder()
    with pytest.raises(resources.ResourceValidationError, match='^risk_budget_exceeded$'):
        calculate(resources, request(b, strategy='manual', quantity=140), b=b)


def test_risk_max_position_cap_stays_independent_of_risk_budget(resources):
    b = builder()
    req = request(b, quantity=181)
    with pytest.raises(resources.ResourceValidationError, match='^risk_position_cap_exceeded$'):
        calculate(resources, req, b=b, stop_decision=StopDecision(D('1'), 'global', False))
    at_cap = calculate(resources, request(b, quantity=180), b=b,
        stop_decision=StopDecision(D('1'), 'global', False))
    assert at_cap.exposure == D('1800000')


def test_policy_risk_budget_is_injected_and_not_portfolio_total(resources):
    b = builder()
    assert calculate(resources, request(b, quantity=140), b=b, policy=policy(risk_per_trade_pct=1.0)).quantity == 140
    with pytest.raises(resources.ResourceValidationError, match='^risk_budget_exceeded$'):
        calculate(resources, request(b, quantity=100), b=b, policy=policy(risk_per_trade_pct=0.3))


@pytest.mark.parametrize('source', ['dynamic', 'strategy', 'global'])
def test_uncapped_stop_value_is_preserved_with_crash_flag(resources, source):
    plan = calculate(resources, stop_decision=StopDecision(D('5'), source, True))
    assert plan.planned_risk == D('69509.75')
    assert plan.stop_pct == D('5') and plan.stop_crash_active is True and plan.stop_source == source


def test_sell_keeps_quantity_but_has_no_buy_resource_credit(resources):
    b = builder()
    plan = calculate(resources, request(b, side=OrderSide.SELL, quantity=1000), b=b, stop_decision=None)
    assert plan.quantity == 1000 and plan.valuation_price == D('10000')
    assert plan.exposure == plan.cash == D('0')
    assert plan.planned_risk is None and not plan.risk_applicable


def cancel_request(b):
    return b.prepare_cancel(intent_id='I', attempt_id='C',
        session=RequestSession('2026-09-18', NOW, 'regular'),
        parent=CancelParent('I', 'A', 7, OrderRef('test-scope', 'KR', '2026-09-18',
            'KRX', 'SYNTHETIC_ORDER', 'SYNTHETIC_ORG'), '005930', OrderSide.BUY,
            OrderType.MARKET, 10, D('10000'), 'sepa_trend'))


def test_cancel_wire_zero_is_parent_coverage_not_zero_quantity(resources):
    b = builder()
    req = cancel_request(b)
    before = deepcopy(req)
    plan = calculate(resources, req, b=b, stop_decision=None)
    assert req.body()['ORD_QTY'] == '0' and plan.quantity == 10
    assert plan.cash == plan.exposure == D('0') and plan.planned_risk is None
    assert not plan.risk_applicable and req == before
    with pytest.raises(resources.ResourceValidationError):
        calculate(resources, replace(req, quantity=1), b=b, stop_decision=None)


def test_modify_stays_explicitly_unsupported(resources):
    b = builder()
    forged = replace(request(b), command=CommandKind.MODIFY)
    with pytest.raises(resources.ResourceValidationError, match='^unsupported_modify_contract$'):
        calculate(resources, forged, b=b)


@pytest.mark.parametrize('field,value', [('quantity', True), ('quantity', 1.0), ('quantity', 0),
    ('quantity', '139'), ('valuation_price', D('NaN')), ('valuation_price', D('Infinity')),
    ('valuation_price', D('0')), ('fingerprint', '0'*64), ('wire_fields', (('ORD_QTY', '1'),)),
    ('side', 'buy'), ('wire_price', D('1'))])
def test_tampered_request_rejected_before_resource_calculation(resources, field, value):
    b = builder()
    with pytest.raises(resources.ResourceValidationError):
        calculate(resources, replace(request(b), **{field: value}), b=b)


@pytest.mark.parametrize('stop', [None, StopDecision(D('0'), 'strategy', False),
    StopDecision(D('-1'), 'strategy', False), StopDecision(D('NaN'), 'strategy', False),
    StopDecision(D('Infinity'), 'strategy', False), StopDecision(True, 'strategy', False),
    StopDecision(5.0, 'strategy', False), StopDecision(D('5'), 'unknown', False),
    StopDecision(D('5'), 'strategy', 1), SimpleNamespace(stop_pct=D('5'), source='strategy', crash_capped=False)])
def test_automatic_risk_requires_exact_measured_stop(resources, stop):
    with pytest.raises(resources.ResourceValidationError):
        calculate(resources, stop_decision=stop)


@pytest.mark.parametrize('origin,strategy', [(EntryOrigin.USER, 'manual'), (EntryOrigin.SAFE_ASSET, 'safe_asset')])
def test_optional_invalid_stop_is_not_ignored_on_exempt_buy(resources, origin, strategy):
    b = builder()
    with pytest.raises(resources.ResourceValidationError):
        calculate(resources, request(b, strategy=strategy), b=b, origin=origin,
            stop_decision=StopDecision(D('NaN'), 'strategy', False))


@pytest.mark.parametrize('equity', [D('0'), D('-1'), D('NaN'), D('Infinity'), True, 10000000, '10000000'])
def test_invalid_equity_is_explicit_error(resources, equity):
    with pytest.raises(resources.ResourceValidationError):
        calculate(resources, equity=equity)


def test_policy_and_origin_are_exact_and_revalidated(resources):
    with pytest.raises(resources.ResourceValidationError):
        calculate(resources, origin='user')
    with pytest.raises(resources.ResourceValidationError):
        calculate(resources, policy=SimpleNamespace(**asdict(policy())))
    forged = policy()
    object.__setattr__(forged, 'buy_commission_rate', D('NaN'))
    with pytest.raises(resources.ResourceValidationError):
        calculate(resources, policy=forged)


def test_different_builder_account_rejects_without_leaking_values(resources):
    req = request(builder())
    other = KISRequestBuilder(RequestAccount('other', 'PRIVATE_SYNTHETIC', '01', 'prod',
        'https://openapi.koreainvestment.com:9443', 3))
    with pytest.raises(resources.ResourceValidationError) as exc:
        calculate(resources, req, b=other)
    assert 'PRIVATE' not in str(exc.value) and 'SYNTHETIC' not in str(exc.value)
    assert exc.value.reason == str(exc.value)


def test_frozen_resource_plan_json_has_no_account_and_no_mutable_references(resources):
    b, pol = builder(), policy()
    req = request(b)
    before = deepcopy(req), hash(pol), hash(STOP)
    plan = calculate(resources, req, b=b, policy=pol)
    data = plan.to_dict()
    assert json.loads(json.dumps(data, allow_nan=False)) == data
    assert data['quantity'] == 139 and data['cash'] == '1410850.000'
    assert data['planned_risk'] == '69509.75' and data['risk_applicable'] is True
    assert 'SYNTHETIC_ACCOUNT' not in json.dumps(data) and 'CANO' not in json.dumps(data)
    data['cash'] = '0'
    assert plan.cash == D('1410850') and (req, hash(pol), hash(STOP)) == before
    with pytest.raises(FrozenInstanceError):
        plan.cash = D('0')


@pytest.mark.parametrize('field,value', [('quantity', True), ('valuation_price', D('NaN')),
    ('cash', D('-1')), ('planned_risk', D('Infinity')), ('risk_applicable', 1),
    ('stop_source', []), ('request_fingerprint', 'bad'), ('fee_rate', 0.01)])
def test_resource_plan_rejects_mutable_or_untyped_values(resources, field, value):
    with pytest.raises(resources.ResourceValidationError):
        replace(calculate(resources), **{field: value})


@pytest.mark.parametrize('amount,before,after,expected', [(D('100'), 3, 2, D('67')),
    (D('100'), 3, 0, D('0')), (D('0.5'), 2, 1, D('0.5')),
    (D('67'), 2, 1, D('34')), (D('34'), 1, 0, D('0')),
    (D('10.2'), 3, 3, D('10.2')), (D('10.2'), 0, 0, D('10.2')),
    (None, 3, 2, None), (None, 0, 0, None)])
def test_remaining_resources_use_existing_conservative_ceiling(resources, amount, before, after, expected):
    assert resources.remaining_resource_amount(amount, before, after) == expected


@pytest.mark.parametrize('amount,before,after', [(D('1'), True, 0), (D('1'), 1, False),
    (D('1'), 0, 1), (D('1'), 2, 3), (D('1'), -1, 0), (D('1'), 2.0, 1),
    (D('-1'), 2, 1), (D('NaN'), 2, 1), (D('Infinity'), 2, 1), (True, 2, 1),
    ('1', 2, 1), (None, 0, 1)])
def test_remaining_resources_reject_invalid_or_increasing_coverage(resources, amount, before, after):
    with pytest.raises(resources.ResourceValidationError):
        resources.remaining_resource_amount(amount, before, after)


def test_exact_budget_with_zero_fee_is_allowed(resources):
    b = builder()
    plan = calculate(resources, request(b, quantity=140), b=b,
        policy=policy(buy_commission_rate=D('0')))
    assert plan.planned_risk == D('70000') and plan.quantity == 140


def test_actual_helpers_need_no_global_fee_lookup_or_file_io(resources, monkeypatch):
    b, req, pol = builder(), request(builder()), policy()
    def forbidden(*args, **kwargs):
        raise AssertionError('외부 조회는 순수 계산 경계 밖이다')
    with monkeypatch.context() as m:
        m.setattr(builtins, 'open', forbidden)
        m.setattr('src.utils.sizing.get_fee_calculator', forbidden)
        first = calculate(resources, req, b=b, policy=pol)
        second = calculate(resources, req, b=b, policy=pol)
    assert first == second and first.planned_risk == D('69509.75')


def test_builder_validator_cannot_be_replaced_by_duck_type(resources):
    with pytest.raises(resources.ResourceValidationError, match='^invalid_resource_builder$'):
        resources.calculate_resources(request(builder()), builder=SimpleNamespace(validate=lambda req: None),
            policy=policy(), equity=EQUITY, origin=EntryOrigin.AUTOMATIC, stop_decision=STOP)


def test_arithmetic_failure_is_reason_only_and_does_not_return_underreservation(resources):
    b, req = builder(), request(builder())
    with localcontext() as context:
        context.prec = 2
        with pytest.raises(resources.ResourceValidationError, match='^resource_arithmetic_failed$'):
            calculate(resources, req, b=b)


@pytest.mark.parametrize('cancel', [False, True])
def test_non_buy_explicit_bad_stop_is_still_rejected(resources, cancel):
    b = builder()
    req = cancel_request(b) if cancel else request(b, side=OrderSide.SELL)
    stop = StopDecision(D('1'), [], False) if cancel else StopDecision(D('1'), 'global', 1)
    with pytest.raises(resources.ResourceValidationError, match='^invalid_stop_decision$'):
        calculate(resources, req, b=b, stop_decision=stop)


def test_cancel_parent_wire_tamper_cannot_change_coverage(resources):
    b = builder()
    req = cancel_request(b)
    forged = replace(req, parent=replace(req.parent, remaining_quantity=999))
    with pytest.raises(resources.ResourceValidationError, match='^invalid_prepared_request$'):
        calculate(resources, forged, b=b, stop_decision=None)


def test_serialization_revalidates_frozen_bypass_corruption(resources):
    plan = calculate(resources)
    object.__setattr__(plan, 'stop_source', [])
    with pytest.raises(resources.ResourceValidationError):
        plan.to_dict()


@pytest.mark.parametrize('command', ['sell', 'cancel'])
@pytest.mark.parametrize('equity', [D('0'), D('-100')])
def test_exposure_reducing_request_does_not_require_positive_equity(resources, command, equity):
    b = builder()
    req = cancel_request(b) if command == 'cancel' else request(b, side=OrderSide.SELL)
    plan = calculate(resources, req, b=b, equity=equity, stop_decision=None)
    assert plan.quantity == req.quantity
    assert plan.cash == plan.exposure == 0 and plan.planned_risk is None
