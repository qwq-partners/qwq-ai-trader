"""최초 KR 기준자본 확정에 필요한 보유액 교차검증."""

from decimal import Decimal


def initial_holdings_match_balance(balance, positions):
    """요약과 보유 합이 원 단위로 맞을 때만 기준자본을 확정한다.

    정상 경로는 같은 잔고 스냅샷을 재사용한다. 별도 조회 중 가격이 바뀌면
    임의의 비율 오차로 누락을 허용하지 않고 다음 대사를 기다린다.
    """
    try:
        reported = Decimal(str(balance['stock_value']))
        observed = sum((position.market_value for position in positions.values()), Decimal('0'))
        return (reported.is_finite() and observed.is_finite()
                and reported >= 0 and observed >= 0
                and abs(reported - observed) <= Decimal('1'))
    except (KeyError, TypeError, AttributeError, ValueError, ArithmeticError):
        return False
