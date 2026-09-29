#!/usr/bin/env python3
"""특정 종목 시장가 매도 — 코어 손절용 일회성 스크립트.

사용법:
    python scripts/sell_specific.py 271560:16 034730:3
"""
import argparse
import asyncio
import os
import sys
from decimal import Decimal
from pathlib import Path

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))
sys.path.insert(0, str(project_root / "src"))


def load_env():
    env_path = project_root / ".env"
    if env_path.exists():
        with open(env_path, 'r') as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#') and '=' in line:
                    key, value = line.split('=', 1)
                    key = key.strip()
                    value = value.strip().strip('"').strip("'")
                    if key not in os.environ:
                        os.environ[key] = value


def fallback_qty(requested, before, now):
    """15초 폴백 수량 — 요청 중 아직 안 팔린 몫만, 현재 보유 한도 안에서.

    before 는 첫 주문 전 보유량 스냅샷(조회 실패·종목 없음이면 None → 폴백 안 함).
    """
    if before is None:
        return 0
    sold = max(0, before - now)
    return max(0, min(requested - sold, now))


load_env()

from src.utils.token_manager import KISTokenManager
from src.execution.broker.kis_kr import KISBroker
from src.core.types import Order, OrderSide, OrderType
from src.utils.trader_lock import hold_or_exit


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("orders", nargs="+", help="symbol:qty (예: 271560:16)")
    args = parser.parse_args()
    hold_or_exit("sell_specific")  # 봇·다른 주문 CLI 실행 중이면 exit 2 (KIS 호출 전)

    req = {}  # 같은 종목이 여러 번 오면 합쳐 1건 — 체결량 이중 차감 방지
    for s in args.orders:
        sym, q = s.split(":")
        req[sym.strip()] = req.get(sym.strip(), 0) + int(q)
    targets = list(req.items())

    token = KISTokenManager()
    broker = KISBroker(token_manager=token)
    await broker.connect()

    print("\n=== 매도 대상 ===")
    for sym, qty in targets:
        print(f"  {sym}: {qty}주")

    # 폴백 수량 기준 — 조회 실패(예외·빈 응답)나 스냅샷에 없는 종목은 폴백하지 않는다
    try:
        before = {sym: pos.quantity for sym, pos in (await broker.get_positions()).items()}
    except Exception as e:
        print(f"  보유량 스냅샷 실패 — 시장가 폴백 없음: {e}")
        before = {}

    print("\n=== 1차 매수1호가 지정가 ===")
    for sym, qty in targets:
        bid = await broker.get_best_bid(sym)
        if bid:
            order = Order(symbol=sym, side=OrderSide.SELL,
                          order_type=OrderType.LIMIT,
                          quantity=qty, price=Decimal(str(bid)),
                          reason="코어 추세 진입 실패 손절")
        else:
            order = Order(symbol=sym, side=OrderSide.SELL,
                          order_type=OrderType.MARKET,
                          quantity=qty, reason="코어 추세 진입 실패 손절")
        success, oid = await broker.submit_order(order)
        print(f"  {sym} {qty}주 @{bid or 'MKT'} → {'OK' if success else 'FAIL'} ({oid})")
        await asyncio.sleep(0.5)

    print("\n15초 대기...")
    await asyncio.sleep(15)

    # 자기 주문부터 취소(폴백 수량과 무관) → 잔고 재조회 — 조회 뒤 취소하면 그 사이 체결분만큼 넘겨 판다
    cancelled = {}
    for sym, _ in targets:
        try:
            cancelled[sym] = await broker.cancel_all_for_symbol(sym)
        except Exception as e:
            print(f"  {sym} 취소 오류: {e}")
            cancelled[sym] = 0
        print(f"  {sym} 자기 주문 취소 {cancelled[sym]}건")
    await asyncio.sleep(1)

    positions = await broker.get_positions()  # 실패면 {} → 현재 보유 0 → 폴백 0
    remaining = []
    for sym, qty in targets:
        pos = positions.get(sym)
        now = pos.quantity if pos else 0
        fb = fallback_qty(qty, before.get(sym), now)
        if fb > 0 and cancelled[sym] == 0:
            # 취소 0건: 방금 체결됐는지 취소가 실패해 지정가가 살아 있는지 모른다 — 중복 매도 위험
            print(f"  {sym} 취소 0건·목표 미달 — 상태 불명, 추가 주문 안 함, MTS/HTS 확인")
        elif fb > 0:
            remaining.append((sym, fb))

    if remaining:
        print(f"\n=== 미체결 {len(remaining)}건 시장가 전환 ===")
        for sym, qty in remaining:
            order = Order(symbol=sym, side=OrderSide.SELL,
                          order_type=OrderType.MARKET,
                          quantity=qty, reason="코어 손절(시장가 폴백)")
            success, oid = await broker.submit_order(order)
            print(f"  {sym} {qty}주 시장가 → {'OK' if success else 'FAIL'}")
            await asyncio.sleep(0.5)

    await broker.disconnect()
    print("\n완료")


if __name__ == "__main__":
    asyncio.run(main())
