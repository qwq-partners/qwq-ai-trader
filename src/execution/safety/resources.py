"""실제 준비 요청의 계획 자원 계산. 권한·예약·체결 적용을 수행하지 않는다."""
from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal, DecimalException, ROUND_CEILING

from ...core.types import OrderSide, OrderType
from ...utils.fee_calculator import FeeCalculator, FeeConfig
from ...utils.sizing import planned_risk, risk_quantity_cap
from ...utils.stop_policy import StopDecision
from .guards import EntryOrigin
from .lifecycle import CommandKind
from .requests import KISRequestBuilder, PreparedTradeRequest, RequestValidationError
from .risk_policy import EffectiveRiskPolicy

_ZERO = Decimal('0')


class ResourceValidationError(ValueError):
    """계좌·본문·설정 원문 대신 안정된 사유 코드만 반환한다."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


def _decimal(value, reason, *, positive=False):
    if (type(value) is not Decimal or not value.is_finite()
            or value < 0 or (positive and value == 0)):
        raise ResourceValidationError(reason)


def _quantity(value, *, positive=False):
    if type(value) is not int or value < (1 if positive else 0):
        raise ResourceValidationError('invalid_resource_quantity')


def _stop_values(stop_pct, source, crash_active):
    _decimal(stop_pct, 'invalid_stop_decision', positive=True)
    if type(source) is not str or source not in ('dynamic', 'strategy', 'global') or type(crash_active) is not bool:
        raise ResourceValidationError('invalid_stop_decision')


@dataclass(frozen=True, slots=True)
class ResourcePlan:
    """실제 가격 상한/송신 허가가 아닌 계획값. 미측정 위험은 None이다."""
    request_fingerprint: str
    quantity: int
    valuation_price: Decimal
    exposure: Decimal
    cash: Decimal
    planned_risk: Decimal | None
    stop_pct: Decimal | None
    stop_source: str | None
    stop_crash_active: bool | None
    fee_rate: Decimal
    risk_applicable: bool

    def __post_init__(self):
        if (type(self.request_fingerprint) is not str or len(self.request_fingerprint) != 64
                or any(c not in '0123456789abcdef' for c in self.request_fingerprint)):
            raise ResourceValidationError('invalid_resource_fingerprint')
        _quantity(self.quantity, positive=True)
        _decimal(self.valuation_price, 'invalid_resource_price', positive=True)
        for value in (self.exposure, self.cash, self.fee_rate):
            _decimal(value, 'invalid_resource_amount')
        if type(self.risk_applicable) is not bool:
            raise ResourceValidationError('invalid_risk_applicability')
        if self.stop_pct is None:
            if self.stop_source is not None or self.stop_crash_active is not None:
                raise ResourceValidationError('invalid_stop_decision')
        else:
            _stop_values(self.stop_pct, self.stop_source, self.stop_crash_active)
        if self.planned_risk is not None:
            _decimal(self.planned_risk, 'invalid_resource_amount', positive=True)
            if self.stop_pct is None:
                raise ResourceValidationError('missing_stop_decision')
        if self.risk_applicable and self.planned_risk is None:
            raise ResourceValidationError('missing_stop_decision')
        if self.cash < self.exposure or (self.exposure == 0 and (self.cash != 0 or self.planned_risk is not None)):
            raise ResourceValidationError('inconsistent_resource_amount')

    def to_dict(self) -> dict[str, str | int | bool | None]:
        # frozen 우회 변경도 직렬화 전에 재검증한다. caller에게 사본만 전달한다.
        replace(self)
        return {
            'request_fingerprint': self.request_fingerprint,
            'quantity': self.quantity,
            'valuation_price': str(self.valuation_price),
            'exposure': str(self.exposure),
            'cash': str(self.cash),
            'planned_risk': None if self.planned_risk is None else str(self.planned_risk),
            'stop_pct': None if self.stop_pct is None else str(self.stop_pct),
            'stop_source': self.stop_source,
            'stop_crash_active': self.stop_crash_active,
            'fee_rate': str(self.fee_rate),
            'risk_applicable': self.risk_applicable,
        }


def calculate_resources(request: PreparedTradeRequest, *, builder: KISRequestBuilder,
                        policy: EffectiveRiskPolicy, equity: Decimal, origin: EntryOrigin,
                        stop_decision: StopDecision | None = None) -> ResourcePlan:
    """본문 검증 뒤 계획 자원을 계산한다. 요청을 줄이거나 예약하지 않는다.

    origin은 발급 권한이 아니다. 실제 owner의 EntryAuthority 검증이 선행해야
    한다. 1.015는 기존 pending 여유, sizing1.3/gate1.001은 다른 경계에 남는다.
    """
    if type(builder) is not KISRequestBuilder:
        raise ResourceValidationError('invalid_resource_builder')
    try:
        builder.validate(request)
    except RequestValidationError as exc:
        reason = 'unsupported_modify_contract' if str(exc) == 'unsupported_modify_contract' else 'invalid_prepared_request'
        raise ResourceValidationError(reason) from None
    except (ValueError, TypeError, AttributeError, ArithmeticError):
        raise ResourceValidationError('invalid_prepared_request') from None
    if type(policy) is not EffectiveRiskPolicy:
        raise ResourceValidationError('invalid_resource_policy')
    try:
        policy = replace(policy)
    except (ValueError, TypeError, AttributeError, ArithmeticError):
        raise ResourceValidationError('invalid_resource_policy') from None
    is_buy = request.command is CommandKind.SUBMIT and request.side is OrderSide.BUY
    # 노출을 줄이는 SELL/CANCEL에 BUY 예산의 양의 자산 조건을 새로 씌우지 않는다.
    if type(equity) is not Decimal or not equity.is_finite() or (is_buy and equity <= 0):
        raise ResourceValidationError('invalid_resource_equity')
    if type(origin) is not EntryOrigin:
        raise ResourceValidationError('invalid_entry_origin')
    stop_pct = stop_source = stop_crash_active = None
    if stop_decision is not None:
        if type(stop_decision) is not StopDecision:
            raise ResourceValidationError('invalid_stop_decision')
        _stop_values(stop_decision.stop_pct, stop_decision.source, stop_decision.crash_capped)
        stop_pct, stop_source, stop_crash_active = (
            stop_decision.stop_pct, stop_decision.source, stop_decision.crash_capped)
    risk_applicable = (is_buy and origin is EntryOrigin.AUTOMATIC
        and request.strategy != 'core_holding' and policy.sizing_mode == 'risk')
    if risk_applicable and stop_decision is None:
        raise ResourceValidationError('missing_stop_decision')
    valuation = request.valuation_price
    if request.command is CommandKind.SUBMIT and request.order_type is OrderType.LIMIT:
        valuation = max(valuation, request.wire_price)
    cash = exposure = _ZERO
    risk = None
    if is_buy:
        # 매도비용은 매수 planned_risk에 다시 더하지 않는다.
        fee = FeeCalculator(FeeConfig(buy_commission_rate=policy.buy_commission_rate))
        try:
            exposure = valuation * request.quantity
            cost = exposure + fee.calculate_buy_fee(exposure)
            cash = max(exposure * Decimal('1.015'), cost)
            if stop_pct is not None:
                risk = planned_risk(valuation, request.quantity, stop_pct, fee)
            if risk_applicable:
                maximum = equity * Decimal(str(policy.risk_max_position_pct / 100))
                if exposure > maximum:
                    raise ResourceValidationError('risk_position_cap_exceeded')
                budget = equity * Decimal(str(policy.risk_per_trade_pct)) / 100
                cap = risk_quantity_cap(equity, valuation, stop_pct,
                    risk_per_trade_pct=policy.risk_per_trade_pct, fee_calc=fee)
                if risk > budget or request.quantity > cap:
                    raise ResourceValidationError('risk_budget_exceeded')
        except (DecimalException, OverflowError):
            raise ResourceValidationError('resource_arithmetic_failed') from None
    return ResourcePlan(request.fingerprint, request.quantity, valuation, exposure, cash,
        risk, stop_pct, stop_source, stop_crash_active, policy.buy_commission_rate, risk_applicable)


def remaining_resource_amount(amount: Decimal | None, before_quantity: int, after_quantity: int) -> Decimal | None:
    """기존 현금 예약과 같은 CEILING 비례 계산. 사실 적용/해제 권한은 없다."""
    _quantity(before_quantity)
    _quantity(after_quantity)
    if after_quantity > before_quantity:
        raise ResourceValidationError('increasing_resource_quantity')
    if amount is None:
        return None
    _decimal(amount, 'invalid_resource_amount')
    if before_quantity == 0:
        return amount
    try:
        return min(amount, (amount * after_quantity / before_quantity).quantize(Decimal('1'), rounding=ROUND_CEILING))
    except (DecimalException, OverflowError):
        raise ResourceValidationError('resource_arithmetic_failed') from None
