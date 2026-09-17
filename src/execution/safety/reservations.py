"""잔여 자원의 정확 타입/도메인 판정. 해제·최종성·송신 권한은 주지 않는다."""
from decimal import Decimal, InvalidOperation


def _amount(value):
    if type(value) is not str:
        raise ValueError('invalid_reservation_amount')
    try:
        amount = Decimal(value)
    except InvalidOperation:
        raise ValueError('invalid_reservation_amount') from None
    if not amount.is_finite() or amount < 0:
        raise ValueError('invalid_reservation_amount')
    return amount


def has_remaining_reservation(attempt: dict) -> bool:
    """legacy는 새 pair 모두 없음만 허용; None 위험을 측정0으로 바꾸지 않는다.

    양의 필드를 먼저 찾더라도 나머지 형식 검증을 생략하지 않는다. False는
    예약이 소진됐다는 뜻일 뿐 주문 최종성/child 해결/소유권 증거가 아니다.
    """
    if type(attempt) is not dict:
        raise ValueError('invalid_reservation_row')
    quantity = attempt.get('reserved_quantity')
    if type(quantity) is not int or quantity < 0:
        raise ValueError('invalid_reservation_quantity')
    cash = _amount(attempt.get('reserved_cash'))
    exposure_present = 'reserved_exposure' in attempt
    risk_present = 'reserved_planned_risk' in attempt
    if exposure_present != risk_present:
        raise ValueError('incomplete_reservation_pair')
    exposure, risk = Decimal(0), None
    if exposure_present:
        exposure = _amount(attempt['reserved_exposure'])
        if attempt['reserved_planned_risk'] is not None:
            risk = _amount(attempt['reserved_planned_risk'])
    return quantity > 0 or cash > 0 or exposure > 0 or (risk is not None and risk > 0)
