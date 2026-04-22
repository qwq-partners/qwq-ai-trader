#!/usr/bin/env python3
"""
QWQ AI Trader - 긴급 전량 매도 (KR + US)

사용법:
    python scripts/liquidate_all.py                # KR+US 대화형
    python scripts/liquidate_all.py --market kr    # KR만
    python scripts/liquidate_all.py --market us    # US만
    python scripts/liquidate_all.py --force        # 확인 없이 실행
    python scripts/liquidate_all.py --dry-run      # 포지션 조회만
"""

import argparse
import asyncio
import os
import sys
from datetime import datetime
from decimal import Decimal
from pathlib import Path

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))
sys.path.insert(0, str(project_root / "src"))

from loguru import logger


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


load_env()

from src.utils.token_manager import KISTokenManager
from src.execution.broker.kis_kr import KISBroker
from src.execution.broker.kis_us import KISUSBroker
from src.core.types import Order, OrderSide, OrderType


def parse_args():
    parser = argparse.ArgumentParser(description="긴급 전량 매도 (KR+US)")
    parser.add_argument("--market", type=str, default="both", choices=["kr", "us", "both"])
    parser.add_argument("--force", action="store_true", help="확인 없이 실행")
    parser.add_argument("--dry-run", action="store_true", help="포지션 조회만")
    return parser.parse_args()


async def liquidate_kr(broker, dry_run: bool, force: bool):
    """KR 포지션 전량 청산"""
    print("\n=== KR 포지션 ===")
    positions = await broker.get_positions()
    active = {s: p for s, p in positions.items() if p.quantity > 0}

    if not active:
        print("KR 보유 종목 없음")
        return

    print(f"{'종목코드':<10} {'종목명':<14} {'수량':>6} {'평균단가':>10} {'현재가':>10} {'손익률':>8}")
    print("-" * 65)
    for symbol, pos in active.items():
        print(f"{symbol:<10} {pos.name:<14} {pos.quantity:>6}주 "
              f"{float(pos.avg_price):>10,.0f} {float(pos.current_price):>10,.0f} "
              f"{pos.unrealized_pnl_pct:>+7.2f}%")

    if dry_run:
        return

    if not force:
        confirm = input(f"\nKR {len(active)}건 시장가 매도? (y/N): ").strip().lower()
        if confirm != 'y':
            print("취소")
            return

    # 1차: 매수1호가 지정가
    for symbol, pos in active.items():
        bid = await broker.get_best_bid(symbol)
        if bid:
            order = Order(symbol=symbol, side=OrderSide.SELL, order_type=OrderType.LIMIT,
                          quantity=pos.quantity, price=Decimal(str(bid)), reason="긴급전량청산")
        else:
            order = Order(symbol=symbol, side=OrderSide.SELL, order_type=OrderType.MARKET,
                          quantity=pos.quantity, reason="긴급전량청산")
        success, oid = await broker.submit_order(order)
        print(f"  {symbol} {pos.quantity}주 → {'OK' if success else 'FAIL'} ({oid})")
        await asyncio.sleep(0.5)

    # 15초 대기 → 미체결 시장가 전환
    print("\n15초 대기...")
    await asyncio.sleep(15)
    remaining = await broker.get_positions()
    remaining_active = {s: p for s, p in remaining.items() if p.quantity > 0}
    if remaining_active:
        print(f"미체결 {len(remaining_active)}건 시장가 전환")
        for symbol in remaining_active:
            try:
                await broker.cancel_all_for_symbol(symbol)
            except Exception:
                pass
        await asyncio.sleep(1)
        for symbol, pos in remaining_active.items():
            order = Order(symbol=symbol, side=OrderSide.SELL, order_type=OrderType.MARKET,
                          quantity=pos.quantity, reason="긴급전량청산(폴백)")
            success, oid = await broker.submit_order(order)
            print(f"  {symbol} 시장가 → {'OK' if success else 'FAIL'}")
            await asyncio.sleep(0.5)


async def liquidate_us(broker, dry_run: bool, force: bool):
    """US 포지션 전량 청산"""
    print("\n=== US 포지션 ===")
    positions = await broker.get_positions()

    # 2026-04-22 수정: KIS US API는 `qty` 키 반환 (quantity 아님)
    # MEMORY: feedback_is_core_init_order.md의 KIS API key naming 규칙
    def _qty(p):
        """포지션 dict에서 수량 추출 — qty 우선, quantity 폴백"""
        v = p.get('qty', p.get('quantity', 0)) if isinstance(p, dict) else getattr(p, 'quantity', 0)
        try:
            return int(float(v))
        except (TypeError, ValueError):
            return 0

    active = {}
    if isinstance(positions, list):
        for p in positions:
            sym = p.get('symbol', '') if isinstance(p, dict) else getattr(p, 'symbol', '')
            q = _qty(p)
            if q > 0 and sym:
                active[sym] = p
    elif isinstance(positions, dict):
        for sym, p in positions.items():
            if _qty(p) > 0:
                active[sym] = p

    if not active:
        print("US 보유 종목 없음")
        return

    for symbol, pos in active.items():
        qty = _qty(pos)
        if isinstance(pos, dict):
            avg = pos.get('avg_price', 0)
            cur = pos.get('current_price', 0)
            name = pos.get('name', symbol)
            print(f"  {symbol:<8} {name:<20} {qty}주 avg=${avg:.2f} cur=${cur:.2f}")
        else:
            print(f"  {symbol} {qty}주")

    if dry_run:
        return

    if not force:
        confirm = input(f"\nUS {len(active)}건 시장가 매도? (y/N): ").strip().lower()
        if confirm != 'y':
            print("취소")
            return

    for symbol, pos in active.items():
        qty = _qty(pos)
        if qty <= 0:
            continue
        # 거래소 코드 자동 추출 (KIS_US 주문은 exchange 필수 — NASD/NYSE/AMEX)
        exchange = pos.get('exchange', 'NASD') if isinstance(pos, dict) else 'NASD'
        # 2026-04-22 수정: KIS 해외주식은 price=0 시장가 미지원 ("주문단가를 입력 하십시오")
        # 현재가 대비 2% 하향 지정가로 즉시체결 유도 (SELL slippage buffer)
        cur_price = float(pos.get('current_price', 0)) if isinstance(pos, dict) else 0
        if cur_price <= 0:
            print(f"  {symbol}({exchange}) 현재가 미확인 — 스킵")
            continue
        limit_price = round(cur_price * 0.98, 2)  # 2% 하향 지정가
        try:
            result = await broker.submit_sell_order(symbol, exchange=exchange, qty=qty, price=limit_price)
            ok = result.get('success') if isinstance(result, dict) else False
            print(f"  {symbol}({exchange}) {qty}주 @ ${limit_price} (cur ${cur_price}) → {'OK' if ok else 'FAIL'} ({result.get('message','')})")
        except Exception as e:
            print(f"  {symbol}({exchange}) 매도 실패: {e}")
        await asyncio.sleep(0.5)


async def main():
    args = parse_args()
    logger.remove()
    logger.add(sys.stdout, format="{time:HH:mm:ss} | {message}", level="INFO")

    print("=" * 60)
    print("QWQ AI Trader - 긴급 전량 매도")
    print(f"시간: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"시장: {args.market}")
    print("=" * 60)

    token_mgr = KISTokenManager()
    token = await token_mgr.get_access_token()
    if not token:
        print("토큰 획득 실패")
        return

    try:
        if args.market in ("kr", "both"):
            kr_broker = KISBroker(token_manager=token_mgr)
            await kr_broker.connect()
            await liquidate_kr(kr_broker, args.dry_run, args.force)
            await kr_broker.disconnect()

        if args.market in ("us", "both"):
            us_broker = KISUSBroker(token_manager=token_mgr)
            await us_broker.connect()
            await liquidate_us(us_broker, args.dry_run, args.force)
            await us_broker.disconnect()
    finally:
        await token_mgr.close()

    print("\n완료")


if __name__ == "__main__":
    asyncio.run(main())
