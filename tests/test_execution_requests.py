"""실제 주문 의미를 고정하는 순수 요청 경계; 송신 권한은 검증 범위가 아니다."""
from copy import deepcopy
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timezone
from decimal import Decimal, localcontext
from zoneinfo import ZoneInfo

import pytest

from src.core.types import Order, OrderSide, OrderType
from src.execution.broker import kis_kr
from src.execution.broker.base import BaseBroker
from src.execution.broker.kis_kr import KISBroker
from src.execution.safety.lifecycle import CommandKind, OrderRef
from src.execution.safety.requests import (
    CancelParent, KISRequestBuilder, RequestAccount, RequestSession,
    RequestValidationError, round_kr_price,
)


KST = ZoneInfo('Asia/Seoul')
NOW = datetime(2026, 9, 18, 10, 0, tzinfo=KST)


def account(**changes):
    values = dict(account_scope='test-scope', account_no='SYNTHETIC_ACCOUNT',
                  account_product_cd='SYNTHETIC_PRODUCT', environment='prod',
                  endpoint='https://openapi.koreainvestment.com:9443', config_version=3)
    return RequestAccount(**(values | changes))


def session(hour=10, minute=0, name='regular'):
    return RequestSession('2026-09-18', NOW.replace(hour=hour, minute=minute), name)


def order(**changes):
    return Order(**(dict(symbol='5930', side=OrderSide.BUY, order_type=OrderType.LIMIT,
                         quantity=3, price=Decimal('10025')) | changes))


def submit(builder=None, **changes):
    return (builder or KISRequestBuilder(account())).prepare_submit(
        changes.pop('order', order()), intent_id='intent-1', attempt_id='attempt-1',
        session=changes.pop('session', session()),
        valuation_price=changes.pop('valuation_price', Decimal('10100')), **changes)


def parent(**changes):
    values = dict(intent_id='intent-1', attempt_id='original-attempt', version=7,
                  order_ref=OrderRef('test-scope', 'KR', '2026-09-18', 'KRX',
                                     'SYNTHETIC_ORDER', 'SYNTHETIC_ORG'),
                  symbol='5930', side=OrderSide.BUY, order_type=OrderType.LIMIT,
                  remaining_quantity=2, valuation_price=Decimal('10100'))
    return CancelParent(**(values | changes))


def cancel(builder=None, **changes):
    return (builder or KISRequestBuilder(account())).prepare_cancel(
        intent_id=changes.pop('intent_id', 'intent-1'), attempt_id='cancel-attempt',
        session=changes.pop('session', session()), parent=changes.pop('parent', parent()),
        **changes)


def test_limit_body_preserves_actual_wire_price_and_separate_valuation():
    request = submit()
    assert request.body() == {
        'CANO': 'SYNTHETIC_ACCOUNT', 'ACNT_PRDT_CD': 'SYNTHETIC_PRODUCT',
        'PDNO': '005930', 'ORD_DVSN': '00', 'ORD_QTY': '3', 'ORD_UNPR': '10000',
        'CTAC_TLNO': '', 'SLL_TYPE': '', 'ALGO_NO': '',
    }
    assert request.tr_id == 'TTTC0802U'
    assert request.path == '/uapi/domestic-stock/v1/trading/order-cash'
    assert request.wire_price == Decimal('10000')
    assert request.valuation_price == Decimal('10100')
    assert request.quantity == 3 and request.command is CommandKind.SUBMIT


def test_request_does_not_retain_caller_order_or_expose_mutable_body():
    original = order()
    builder = KISRequestBuilder(account())
    request = submit(builder, order=original)
    digest = request.fingerprint
    original.quantity, original.symbol, original.price = 99, '000001', Decimal('1')
    body = request.body()
    body['ORD_QTY'], body['CANO'] = '99', 'OTHER_ACCOUNT'
    copied = deepcopy(request)
    copied.body()['ORD_UNPR'] = '0'
    builder.validate(copied)
    assert copied.fingerprint == digest and copied.body()['ORD_QTY'] == '3'
    assert request.body()['CANO'] == 'SYNTHETIC_ACCOUNT'
    with pytest.raises(FrozenInstanceError):
        request.quantity = 99


@pytest.mark.parametrize('value', [True, False, 1.0, '1', Decimal('1'), 0, -1])
def test_submit_rejects_quantity_coercion(value):
    with pytest.raises(RequestValidationError):
        submit(order=order(quantity=value))


@pytest.mark.parametrize('value', [True, 10000, 10000.0, '10000', None,
                                  Decimal('0'), Decimal('-1'), Decimal('NaN'),
                                  Decimal('sNaN'), Decimal('Infinity')])
def test_limit_rejects_non_decimal_nonpositive_or_nonfinite_price(value):
    with pytest.raises(RequestValidationError):
        submit(order=order(price=value))


@pytest.mark.parametrize('value', [True, '10100', 10100.0, Decimal('0'),
                                  Decimal('-1'), Decimal('NaN'), Decimal('Infinity')])
def test_market_cannot_use_wire_zero_as_valuation(value):
    with pytest.raises(RequestValidationError):
        submit(order=order(order_type=OrderType.MARKET, price=None), valuation_price=value)


def test_market_wire_zero_has_positive_valuation():
    request = submit(order=order(order_type=OrderType.MARKET, price=None))
    assert request.body()['ORD_UNPR'] == '0' and request.body()['ORD_DVSN'] == '01'
    assert request.wire_price == 0 and request.valuation_price == Decimal('10100')


@pytest.mark.parametrize('value', ['buy', 'sell', None, True])
def test_side_requires_actual_enum(value):
    with pytest.raises(RequestValidationError):
        submit(order=order(side=value))


@pytest.mark.parametrize('value', [OrderType.STOP, OrderType.STOP_LIMIT, 'limit',
                                  'market', 'unknown', None, True])
def test_unsupported_types_do_not_fall_back_to_market(value):
    with pytest.raises(RequestValidationError):
        submit(order=order(order_type=value))


@pytest.mark.parametrize('raw,want', [
    ('998.5', '998'), ('999.5', '1000'), ('1000', '1000'),
    ('1002.5', '1000'), ('1007.5', '1010'), ('4999', '5000'),
    ('5005', '5000'), ('5015', '5020'), ('9999', '10000'),
    ('10025', '10000'), ('10075', '10100'), ('49999', '50000'),
    ('50050', '50000'), ('50150', '50200'), ('99999', '100000'),
    ('100250', '100000'), ('100750', '101000'), ('499999', '500000'),
    ('500500', '500000'), ('501500', '502000'),
])
def test_decimal_ticks_match_actual_broker_half_even(raw, want):
    assert round_kr_price(Decimal(raw)) == Decimal(want)
    assert BaseBroker.round_to_tick(float(raw)) == int(want)


def test_large_decimal_tick_does_not_lose_digits_or_depend_on_caller_context():
    value = Decimal('1234567890123456789012345678901234567891500')
    with localcontext() as context:
        context.prec = 6
        assert round_kr_price(value) == Decimal('1234567890123456789012345678901234567892000')
        assert context.prec == 6
    with pytest.raises(RequestValidationError):
        submit(order=order(price=Decimal('0.1')))


@pytest.mark.parametrize('hour,minute,name', [
    (8, 0, 'pre_market'), (8, 49, 'pre_market'), (8, 50, 'pre_close'),
    (8, 59, 'pre_close'), (9, 0, 'regular'), (15, 19, 'regular'),
    (15, 20, 'closing'), (15, 29, 'closing'), (15, 40, 'next_market'),
    (19, 59, 'next_market'),
])
@pytest.mark.parametrize('side', [OrderSide.BUY, OrderSide.SELL])
def test_session_and_division_match_real_broker_without_io(monkeypatch, hour, minute, name, side):
    now = NOW.replace(hour=hour, minute=minute)
    class Clock:
        @staticmethod
        def now():
            return now
    monkeypatch.setattr(kis_kr, 'datetime', Clock)
    # 실제 __init__는 token manager를 구성하므로 순수 helper만 사용한다.
    broker = object.__new__(KISBroker)
    def forbidden(*args, **kwargs):
        pytest.fail('순수 요청 생성에서 외부 경계를 호출했습니다')
    for method in ('connect', '_get_hashkey', '_rate_limit', '_api_post', 'get_nxt_symbols'):
        monkeypatch.setattr(KISBroker, method, forbidden)
    original = order(side=side)
    request = submit(order=original, session=session(hour, minute, name))
    assert broker._get_current_market_session() == name
    assert request.tr_id == broker._get_tr_id_for_session(side)
    assert request.body()['ORD_DVSN'] == broker._get_order_division(original)
    assert request.body()['SLL_TYPE'] == ('01' if side is OrderSide.SELL else '')
    assert ('AFHR_FLPR_YN' in request.body()) == (name in ('pre_market', 'next_market'))


@pytest.mark.parametrize('hour,minute,name', [(8, 50, 'pre_close'), (15, 20, 'closing'),
                                            (15, 30, 'break'), (20, 0, 'closed')])
def test_submit_retains_session_restrictions(hour, minute, name):
    with pytest.raises(RequestValidationError):
        submit(order=order(order_type=OrderType.MARKET), session=session(hour, minute, name))


@pytest.mark.parametrize('hour,minute,name', [(8, 0, 'pre_market'), (15, 40, 'next_market')])
def test_existing_after_hours_market_encoding_is_not_reclassified(hour, minute, name):
    request = submit(order=order(order_type=OrderType.MARKET), session=session(hour, minute, name))
    assert request.body()['ORD_DVSN'] == '05'
    assert request.body()['AFHR_FLPR_YN'] == 'Y' and request.body()['ORD_UNPR'] == '0'


@pytest.mark.parametrize('day,now,name', [
    ('2026-09-17', NOW, 'regular'), ('2026-09-18', NOW.replace(tzinfo=None), 'regular'),
    ('2026-09-18', NOW, 'next_market'), ('2026-09-18', NOW, 'unknown'),
    ('2026-9-18', NOW, 'regular'),
])
def test_session_rejects_inconsistent_time_date_and_label(day, now, name):
    with pytest.raises(RequestValidationError):
        RequestSession(day, now, name)


def test_aware_utc_input_is_bound_as_kst_without_host_timezone_dependency():
    converted = RequestSession('2026-09-18', NOW.astimezone(timezone.utc), 'regular')
    assert converted.observed_at.tzinfo == KST and converted.observed_at.hour == 10
    assert submit(session=converted).fingerprint == submit().fingerprint


@pytest.mark.parametrize('changes', [dict(environment='dev'), dict(environment=True),
    dict(endpoint='https://openapivts.koreainvestment.com:29443'),
    dict(endpoint='https://other.invalid'), dict(account_no=''), dict(account_scope=' '),
    dict(account_product_cd=1), dict(config_version=True), dict(config_version=-1)])
def test_account_rejects_unsupported_env_endpoint_and_ambiguous_types(changes):
    with pytest.raises(RequestValidationError):
        account(**changes)


def test_cancel_binds_parent_coverage_while_wire_requests_all_remaining():
    request = cancel()
    assert request.body() == {
        'CANO': 'SYNTHETIC_ACCOUNT', 'ACNT_PRDT_CD': 'SYNTHETIC_PRODUCT',
        'KRX_FWDG_ORD_ORGNO': 'SYNTHETIC_ORG', 'ORGN_ODNO': 'SYNTHETIC_ORDER',
        'ORD_DVSN': '00', 'RVSE_CNCL_DVSN_CD': '02', 'ORD_QTY': '0',
        'ORD_UNPR': '0', 'QTY_ALL_ORD_YN': 'Y',
    }
    assert request.quantity == 2 and request.parent.version == 7
    assert request.tr_id == 'TTTC0803U'
    assert request.path == '/uapi/domestic-stock/v1/trading/order-rvsecncl'
    assert request.valuation_price > 0


@pytest.mark.parametrize('hour,minute,name', [(15, 30, 'break'), (20, 0, 'closed')])
def test_cancel_does_not_inherit_submit_session_ban(hour, minute, name):
    assert cancel(session=session(hour, minute, name)).body()['ORD_QTY'] == '0'


@pytest.mark.parametrize('changes', [dict(remaining_quantity=0), dict(remaining_quantity=True),
    dict(remaining_quantity='2'), dict(remaining_quantity=2.0), dict(attempt_id=''),
    dict(version=True), dict(side='buy'), dict(order_type=OrderType.STOP),
    dict(valuation_price=Decimal('0'))])
def test_cancel_requires_complete_typed_parent(changes):
    with pytest.raises(RequestValidationError):
        cancel(parent=parent(**changes))


@pytest.mark.parametrize('changes', [dict(account_scope='foreign'), dict(market='US'),
    dict(order_date='2026-09-17'), dict(org_no='')])
def test_cancel_rejects_parent_scope_mismatch(changes):
    ref = replace(parent().order_ref, **changes)
    with pytest.raises(RequestValidationError):
        cancel(parent=parent(order_ref=ref))


def test_cancel_rejects_foreign_intent_and_self_parent_attempt():
    with pytest.raises(RequestValidationError):
        cancel(intent_id='foreign')
    with pytest.raises(RequestValidationError):
        cancel(parent=parent(attempt_id='cancel-attempt'))


@pytest.mark.parametrize('field,value', [('command', CommandKind.CANCEL), ('tr_id', 'TTTC0801U'),
    ('path', '/uapi/domestic-stock/v1/trading/order-rvsecncl'), ('method', 'GET'),
    ('market', 'US'), ('side', OrderSide.SELL), ('quantity', True), ('quantity', 3.0),
    ('side', 'buy'), ('wire_price', Decimal('0')), ('parent', parent()),
    ('schema_version', True), ('fingerprint', '0' * 64)])
def test_validation_rejects_tampered_request(field, value):
    builder = KISRequestBuilder(account())
    with pytest.raises(RequestValidationError):
        builder.validate(replace(submit(builder), **{field: value}))


@pytest.mark.parametrize('field,value', [('CANO', 'OTHER_ACCOUNT'), ('ORD_QTY', 3),
    ('ORD_QTY', True), ('ORD_QTY', '03'), ('ORD_QTY', '3.0'), ('ORD_DVSN', '01'),
    ('SLL_TYPE', '01'), ('ORD_UNPR', '0')])
def test_semantic_validation_checks_exact_wire_fields_before_digest(field, value):
    builder = KISRequestBuilder(account())
    request = submit(builder)
    fields = dict(request.wire_fields)
    fields[field] = value
    with pytest.raises(RequestValidationError, match='request_meaning_mismatch'):
        builder.validate(replace(request, wire_fields=tuple(sorted(fields.items()))))


def test_builder_rejects_foreign_account_even_when_request_is_internally_consistent():
    request = submit(KISRequestBuilder(account(account_no='OTHER_ACCOUNT')))
    with pytest.raises(RequestValidationError):
        KISRequestBuilder(account()).validate(request)
    assert request.fingerprint != submit().fingerprint


def test_cancel_parent_relationship_version_and_coverage_are_fingerprinted():
    builder = KISRequestBuilder(account())
    request = cancel(builder)
    for changes in (dict(version=8), dict(remaining_quantity=1), dict(attempt_id='other'),
                    dict(order_ref=replace(parent().order_ref, parent_order_no='ANCESTOR'))):
        changed = replace(request, parent=parent(**changes))
        with pytest.raises(RequestValidationError):
            builder.validate(changed)
        assert cancel(parent=parent(**changes)).fingerprint != request.fingerprint


@pytest.mark.parametrize('side', [OrderSide.BUY, OrderSide.SELL])
@pytest.mark.parametrize('quantity', [1, 2, 4, None])
def test_all_modify_variants_are_explicitly_unsupported(side, quantity):
    with pytest.raises(RequestValidationError, match='unsupported_modify_contract'):
        KISRequestBuilder(account()).prepare_modify(
            parent=parent(side=side), new_quantity=quantity, new_price=Decimal('9000'))


def test_repr_hides_account_body_and_parent_identifiers():
    text = ' '.join(repr(x) for x in (account(), parent(), submit(), cancel()))
    for sensitive in ('test-scope', 'SYNTHETIC_ACCOUNT', 'SYNTHETIC_PRODUCT',
                      'SYNTHETIC_ORDER', 'SYNTHETIC_ORG', 'original-attempt'):
        assert sensitive not in text


def test_raw_dict_and_approval_bool_do_not_create_typed_requests():
    builder = KISRequestBuilder(account())
    for value in (True, {'approved': True}, submit().body()):
        with pytest.raises(RequestValidationError):
            builder.validate(value)
    with pytest.raises(RequestValidationError):
        KISRequestBuilder({'approved': True})


def test_strategy_is_snapshotted_and_fingerprinted_without_conferring_authority():
    original = order(strategy='sepa')
    builder = KISRequestBuilder(account())
    request = submit(builder, order=original)
    original.strategy = 'manual'
    assert request.strategy == 'sepa'
    builder.validate(request)
    assert submit(order=original).fingerprint != request.fingerprint
    with pytest.raises(RequestValidationError):
        builder.validate(replace(request, strategy='manual'))
    assert submit(order=order(strategy=None)).strategy == ''
    assert submit(order=order(strategy='')).fingerprint == submit(order=order(strategy=None)).fingerprint


@pytest.mark.parametrize('value', [True, 1, {}, ' sepa '])
def test_strategy_does_not_coerce_arbitrary_values(value):
    with pytest.raises(RequestValidationError):
        submit(order=order(strategy=value))


def test_cancel_preserves_parent_strategy_and_rejects_strategy_substitution():
    builder = KISRequestBuilder(account())
    request = cancel(builder, parent=parent(strategy='sepa'))
    assert request.strategy == 'sepa'
    builder.validate(request)
    with pytest.raises(RequestValidationError):
        builder.validate(replace(request, strategy='manual'))


def test_semantic_strings_do_not_accept_equal_string_enum_values():
    from enum import Enum
    class WireLike(str, Enum):
        PATH = '/uapi/domestic-stock/v1/trading/order-cash'
        TR = 'TTTC0802U'
        METHOD = 'POST'
        MARKET = 'KR'
    builder = KISRequestBuilder(account())
    for field, value in (('path', WireLike.PATH), ('tr_id', WireLike.TR),
                         ('method', WireLike.METHOD), ('market', WireLike.MARKET)):
        with pytest.raises(RequestValidationError):
            builder.validate(replace(submit(builder), **{field: value}))


@pytest.mark.parametrize('field', ['parent', 'limit_price'])
def test_malformed_domain_fields_are_rejected_before_json_encoding(field):
    builder = KISRequestBuilder(account())
    with pytest.raises(RequestValidationError):
        builder.validate(replace(cancel(builder), **{field: object()}))


def test_configuration_version_session_and_valuation_are_bound_to_fingerprint():
    request = submit()
    assert submit(KISRequestBuilder(account(config_version=4))).fingerprint != request.fingerprint
    assert submit(session=session(10, 1)).fingerprint != request.fingerprint
    assert submit(valuation_price=Decimal('10200')).fingerprint != request.fingerprint
    with pytest.raises(RequestValidationError):
        KISRequestBuilder(account(config_version=4)).validate(request)
