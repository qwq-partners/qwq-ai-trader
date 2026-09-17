"""KR 거래 요청의 순수 바인딩 경계. 송신·예약·최종성 권한을 발급하지 않는다.

계좌는 설치자가 broker.config에서 추출하고 부모는 owner의 원장에서 가져온다.
이 모듈은 그 출처를 인증하지 않는다. 후속 owner/guard가 현재 원장 버전·잔량·
예약·세션 정책과 저장된 fingerprint를 대조해야 실제 송신을 판단할 수 있다.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass, field, replace
from datetime import date, datetime
from decimal import Decimal, ROUND_HALF_EVEN, localcontext
from enum import Enum
from hashlib import sha256
import json
from zoneinfo import ZoneInfo

from ...core.types import Order, OrderSide, OrderType
from .lifecycle import CommandKind, OrderRef


_KST = ZoneInfo('Asia/Seoul')
_ENDPOINT = 'https://openapi.koreainvestment.com:9443'
_TRADING_PATH = '/uapi/domestic-stock/v1/trading/'


class RequestValidationError(ValueError):
    """계좌나 본문 원문을 포함하지 않는 요청 거부 사유."""


def _text(value: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise RequestValidationError('invalid_request_identity')
    return value


def _integer(value: int, *, positive: bool = False) -> int:
    if type(value) is not int or value < (1 if positive else 0):
        raise RequestValidationError('invalid_request_quantity_or_version')
    return value


def _price(value: Decimal) -> Decimal:
    if type(value) is not Decimal or not value.is_finite() or value <= 0:
        raise RequestValidationError('invalid_request_price')
    return value


def _strategy(value: str | None) -> str:
    if value is None:
        return ''
    if type(value) is not str or value != value.strip():
        raise RequestValidationError('invalid_request_strategy')
    return value


def _side_and_type(side: OrderSide, order_type: OrderType) -> None:
    if type(side) is not OrderSide:
        raise RequestValidationError('invalid_request_side')
    if type(order_type) is not OrderType or order_type not in (OrderType.LIMIT, OrderType.MARKET):
        raise RequestValidationError('unsupported_order_type')


def _session_at(now: datetime) -> str:
    """broker의 시간 구간만 보존한다. 휴장/종목 지원/송신 정책 승인이 아니다."""
    minute = now.hour * 100 + now.minute
    for start, end, name in ((800, 850, 'pre_market'), (850, 900, 'pre_close'),
                             (900, 1520, 'regular'), (1520, 1530, 'closing'),
                             (1530, 1540, 'break'), (1540, 2000, 'next_market')):
        if start <= minute < end:
            return name
    return 'closed'


@dataclass(frozen=True)
class RequestAccount:
    account_scope: str = field(repr=False)
    account_no: str = field(repr=False)
    account_product_cd: str = field(repr=False)
    environment: str
    endpoint: str
    config_version: int

    def __post_init__(self):
        for value in (self.account_scope, self.account_no, self.account_product_cd):
            _text(value)
        if type(self.environment) is not str or self.environment != 'prod':
            raise RequestValidationError('unsupported_request_environment')
        if type(self.endpoint) is not str or self.endpoint != _ENDPOINT:
            raise RequestValidationError('unsupported_request_endpoint')
        _integer(self.config_version)


@dataclass(frozen=True)
class RequestSession:
    """시간 사실의 snapshot. calendar·종목 지원 bool이나 거래 권한을 받지 않는다."""
    business_date_kst: str
    observed_at: datetime
    session: str

    def __post_init__(self):
        _text(self.business_date_kst)
        if type(self.observed_at) is not datetime or self.observed_at.utcoffset() is None:
            raise RequestValidationError('invalid_request_time')
        now = self.observed_at.astimezone(_KST)
        if self.business_date_kst != now.date().isoformat():
            raise RequestValidationError('request_business_date_mismatch')
        if type(self.session) is not str or self.session != _session_at(now):
            raise RequestValidationError('request_session_mismatch')
        object.__setattr__(self, 'observed_at', now)


@dataclass(frozen=True, repr=False)
class CancelParent:
    intent_id: str
    attempt_id: str
    version: int
    order_ref: OrderRef
    symbol: str
    side: OrderSide
    order_type: OrderType
    remaining_quantity: int
    valuation_price: Decimal
    strategy: str = ''

    def __post_init__(self):
        _text(self.intent_id)
        _text(self.attempt_id)
        _integer(self.version)
        _text(self.symbol)
        _side_and_type(self.side, self.order_type)
        _integer(self.remaining_quantity, positive=True)
        _price(self.valuation_price)
        object.__setattr__(self, 'strategy', _strategy(self.strategy))
        if type(self.order_ref) is not OrderRef:
            raise RequestValidationError('incomplete_cancel_parent')
        ref = self.order_ref
        for value in (ref.account_scope, ref.market, ref.order_date, ref.exchange,
                      ref.order_no, ref.org_no):
            _text(value)
        if type(ref.parent_order_no) is not str or ref.parent_order_no != ref.parent_order_no.strip():
            raise RequestValidationError('incomplete_cancel_parent')
        try:
            canonical_date = date.fromisoformat(ref.order_date).isoformat()
        except ValueError:
            raise RequestValidationError('incomplete_cancel_parent') from None
        if ref.order_date != canonical_date or ref.order_no.startswith(('TEMP_', 'local-')):
            raise RequestValidationError('incomplete_cancel_parent')
        object.__setattr__(self, 'order_ref', deepcopy(ref))


@dataclass(frozen=True)
class PreparedTradeRequest:
    """정확한 JSON 필드 값/타입 바인딩. hashkey나 네트워크 raw-byte 해시가 아니다.

    frozen DTO 자체는 승인 capability가 아니다. 소비자는 builder.validate와 함께
    owner가 저장한 동일 attempt의 fingerprint·예약·현재 상태를 확인해야 한다.
    """
    schema_version: int
    account: RequestAccount = field(repr=False)
    session: RequestSession
    intent_id: str = field(repr=False)
    attempt_id: str = field(repr=False)
    command: CommandKind
    market: str
    method: str
    path: str
    tr_id: str
    symbol: str
    strategy: str
    side: OrderSide
    order_type: OrderType
    quantity: int
    limit_price: Decimal | None
    wire_price: Decimal
    valuation_price: Decimal
    parent: CancelParent | None = field(repr=False)
    wire_fields: tuple[tuple[str, str], ...] = field(repr=False)
    fingerprint: str

    def body(self) -> dict[str, str]:
        """hashkey/POST 소비자에게 각각 사본을 준다. 내부 원본은 불변 tuple이다."""
        return dict(self.wire_fields)


def _canonical(request: PreparedTradeRequest) -> str:
    def json_value(value):
        if isinstance(value, Enum):
            return value.value
        if isinstance(value, Decimal):
            return str(value)
        if isinstance(value, datetime):
            return value.isoformat()
        if type(value) is dict:
            return {key: json_value(item) for key, item in value.items()}
        if type(value) in (tuple, list):
            return [json_value(item) for item in value]
        return value
    fields = asdict(request)
    fields.pop('fingerprint')
    return json.dumps(json_value(fields), sort_keys=True, ensure_ascii=False,
                      separators=(',', ':'), allow_nan=False)


class KISRequestBuilder:
    """설치자가 주입한 계좌에 요청을 묶는다. 외부 I/O나 권한 발급은 하지 않는다."""

    def __init__(self, account: RequestAccount):
        if type(account) is not RequestAccount:
            raise RequestValidationError('invalid_request_account')
        self._account = replace(account)

    def _scope(self, intent_id: str, attempt_id: str, session: RequestSession) -> RequestSession:
        _text(intent_id)
        _text(attempt_id)
        if type(session) is not RequestSession:
            raise RequestValidationError('invalid_request_session')
        return replace(session)

    def _finish(self, *, session, intent_id, attempt_id, command, path, tr_id,
                symbol, strategy, side, order_type, quantity, limit_price, wire_price,
                valuation_price, parent, body) -> PreparedTradeRequest:
        request = PreparedTradeRequest(
            schema_version=1, account=replace(self._account), session=session,
            intent_id=intent_id, attempt_id=attempt_id, command=command, market='KR',
            method='POST', path=_TRADING_PATH + path, tr_id=tr_id, symbol=symbol, strategy=strategy,
            side=side, order_type=order_type, quantity=quantity, limit_price=limit_price,
            wire_price=wire_price, valuation_price=valuation_price, parent=parent,
            wire_fields=tuple(sorted(body.items())), fingerprint='',
        )
        return replace(request, fingerprint=sha256(_canonical(request).encode('utf-8')).hexdigest())

    def prepare_submit(self, order: Order, *, intent_id: str, attempt_id: str,
                       session: RequestSession, valuation_price: Decimal) -> PreparedTradeRequest:
        session = self._scope(intent_id, attempt_id, session)
        if type(order) is not Order:
            raise RequestValidationError('invalid_request_order')
        _side_and_type(order.side, order.order_type)
        quantity = _integer(order.quantity, positive=True)
        symbol = _text(order.symbol).zfill(6)
        strategy = _strategy(order.strategy)
        _price(valuation_price)
        if session.session in ('break', 'closed'):
            raise RequestValidationError('unsupported_submit_session')
        if session.session in ('pre_close', 'closing') and order.order_type is OrderType.MARKET:
            raise RequestValidationError('unsupported_submit_session')
        limit_price = _price(order.price) if order.order_type is OrderType.LIMIT else None
        wire_price = round_kr_price(limit_price) if limit_price is not None else Decimal('0')
        if limit_price is not None:
            _price(wire_price)
        after_hours = session.session in ('pre_market', 'next_market')
        division = '05' if after_hours else ('01' if order.order_type is OrderType.MARKET else '00')
        body = {
            'CANO': self._account.account_no, 'ACNT_PRDT_CD': self._account.account_product_cd,
            'PDNO': symbol, 'ORD_DVSN': division, 'ORD_QTY': str(quantity),
            'ORD_UNPR': format(wire_price, 'f'), 'CTAC_TLNO': '',
            'SLL_TYPE': '01' if order.side is OrderSide.SELL else '', 'ALGO_NO': '',
        }
        if after_hours:
            body['AFHR_FLPR_YN'] = 'Y'
        return self._finish(
            session=session, intent_id=intent_id, attempt_id=attempt_id, command=CommandKind.SUBMIT,
            path='order-cash', tr_id='TTTC0802U' if order.side is OrderSide.BUY else 'TTTC0801U',
            symbol=symbol, strategy=strategy, side=order.side, order_type=order.order_type,
            quantity=quantity,
            limit_price=limit_price, wire_price=wire_price, valuation_price=valuation_price,
            parent=None, body=body,
        )

    def prepare_cancel(self, *, intent_id: str, attempt_id: str, session: RequestSession,
                       parent: CancelParent) -> PreparedTradeRequest:
        session = self._scope(intent_id, attempt_id, session)
        if type(parent) is not CancelParent:
            raise RequestValidationError('incomplete_cancel_parent')
        parent = replace(parent)
        ref = parent.order_ref
        if (parent.intent_id != intent_id or parent.attempt_id == attempt_id
                or ref.account_scope != self._account.account_scope or ref.market != 'KR'
                or ref.order_date != session.business_date_kst):
            raise RequestValidationError('cancel_parent_scope_mismatch')
        body = {
            'CANO': self._account.account_no, 'ACNT_PRDT_CD': self._account.account_product_cd,
            'KRX_FWDG_ORD_ORGNO': ref.org_no, 'ORGN_ODNO': ref.order_no,
            'ORD_DVSN': '00', 'RVSE_CNCL_DVSN_CD': '02', 'ORD_QTY': '0',
            'ORD_UNPR': '0', 'QTY_ALL_ORD_YN': 'Y',
        }
        return self._finish(
            session=session, intent_id=intent_id, attempt_id=attempt_id, command=CommandKind.CANCEL,
            path='order-rvsecncl', tr_id='TTTC0803U', symbol=parent.symbol.zfill(6),
            strategy=parent.strategy,
            side=parent.side, order_type=parent.order_type, quantity=parent.remaining_quantity,
            limit_price=None, wire_price=Decimal('0'), valuation_price=parent.valuation_price,
            parent=parent, body=body,
        )

    def prepare_modify(self, *args, **kwargs) -> PreparedTradeRequest:
        raise RequestValidationError('unsupported_modify_contract')

    def validate(self, request: PreparedTradeRequest) -> None:
        """현재 builder 계좌와 도메인·정확 본문·digest를 대조한다. 예약 승인은 아니다."""
        if type(request) is not PreparedTradeRequest:
            raise RequestValidationError('invalid_prepared_request')
        if type(request.command) is not CommandKind:
            raise RequestValidationError('invalid_request_command')
        if request.command is CommandKind.MODIFY:
            raise RequestValidationError('unsupported_modify_contract')
        if type(request.account) is not RequestAccount or replace(request.account) != self._account:
            raise RequestValidationError('request_account_mismatch')
        if type(request.schema_version) is not int or request.schema_version != 1:
            raise RequestValidationError('unsupported_request_schema')
        for value in (request.market, request.method, request.path, request.tr_id, request.symbol):
            _text(value)
        if type(request.strategy) is not str:
            raise RequestValidationError('invalid_request_strategy')
        _strategy(request.strategy)
        _side_and_type(request.side, request.order_type)
        _integer(request.quantity, positive=True)
        _price(request.valuation_price)
        if request.limit_price is not None:
            _price(request.limit_price)
        if request.command is CommandKind.SUBMIT and request.parent is not None:
            raise RequestValidationError('request_meaning_mismatch')
        if type(request.wire_price) is not Decimal or not request.wire_price.is_finite():
            raise RequestValidationError('invalid_request_price')
        if type(request.wire_fields) is not tuple or any(
            type(pair) is not tuple or len(pair) != 2
            or type(pair[0]) is not str or type(pair[1]) is not str
            for pair in request.wire_fields
        ):
            raise RequestValidationError('request_meaning_mismatch')
        if request.command is CommandKind.SUBMIT:
            expected = self.prepare_submit(
                Order(symbol=request.symbol, side=request.side, order_type=request.order_type,
                      quantity=request.quantity, price=request.limit_price, strategy=request.strategy),
                intent_id=request.intent_id, attempt_id=request.attempt_id,
                session=request.session, valuation_price=request.valuation_price,
            )
        else:
            expected = self.prepare_cancel(intent_id=request.intent_id, attempt_id=request.attempt_id,
                                           session=request.session, parent=request.parent)
        # 재계산한 digest만으로 command/TR/본문 불일치를 정당화할 수 없다.
        if _canonical(request) != _canonical(expected):
            raise RequestValidationError('request_meaning_mismatch')
        if type(request.fingerprint) is not str or request.fingerprint != expected.fingerprint:
            raise RequestValidationError('request_fingerprint_mismatch')


def round_kr_price(price: Decimal) -> Decimal:
    """기존 broker의 가격 구간과 half-even을 float 변환 없이 보존한다."""
    _price(price)
    tick = 1000
    for boundary, unit in ((1000, 1), (5000, 5), (10000, 10), (50000, 50),
                           (100000, 100), (500000, 500)):
        if price < boundary:
            tick = unit
            break
    with localcontext() as context:
        context.prec = max(28, len(price.as_tuple().digits), price.adjusted() + 1) + 10
        units = (price / Decimal(tick)).to_integral_value(rounding=ROUND_HALF_EVEN)
        return (units * Decimal(tick)).quantize(Decimal('1'))
