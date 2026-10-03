"""기동 현금 보류가 브로커 직접 BUY와 실제 전송 경계에도 적용되는지 검증한다."""
import asyncio

from src.core.types import OrderSide
from test_order_post_unknown import ub, _order


def test_unverified_cash_blocks_direct_buy_but_preserves_sell(ub):
    async def run():
        ub.set_cash_verification(False)
        ok, reason = await ub.submit_order(_order())
        assert not ok and "현금" in reason
        assert ub._session.sent == []
        ok, reason = await ub.submit_order(_order(OrderSide.SELL))
        assert ok, reason
        assert len(ub._session.sent) == 1
    asyncio.run(run())


def test_cash_hold_is_rechecked_after_rate_limit_before_post(ub):
    async def run():
        ub.set_cash_verification(True)
        async def wait_then_hold():
            ub.set_cash_verification(False)
        ub._rate_limit = wait_then_hold
        ok, reason = await ub.submit_order(_order())
        assert not ok and "현금" in reason
        assert ub._session.sent == []
    asyncio.run(run())


def test_verified_cash_releases_only_cash_buy_hold(ub):
    async def run():
        ub.set_cash_verification(False)
        assert not (await ub.submit_order(_order()))[0]
        ub.set_cash_verification(True)
        ok, reason = await ub.submit_order(_order())
        assert ok, reason
        assert len(ub._session.sent) == 1
    asyncio.run(run())
