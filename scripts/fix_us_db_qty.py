"""
US DB 포지션 수량 불일치 즉각 정리 스크립트

문제: 재시작 시마다 동일 종목에 새 SYNC_ 레코드가 누적되고
     기존 레코드가 닫히지 않아 DB qty > KIS qty 발생

대상:
- ADEA: DB 6주(3레코드) → KIS 1주  → 오래된 2레코드 close, 최근 1레코드 qty=1
- PTEN: DB 16주(2레코드) → KIS 4주 → 오래된 1레코드 close, 최근 1레코드 qty=4
- TYRA: DB 2주(1레코드) → KIS 1주  → 레코드 qty=1 (1주 partial sell 기록)
- SEI:  DB 2주(1레코드) → KIS 0주  → close (전량 손절 기록)
"""
import asyncio, sys, os
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), '..', '.env'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from datetime import datetime


async def main():
    import asyncpg, aiohttp
    from src.execution.broker.kis_us import KISUSBroker
    from src.utils.token_manager import KISTokenManager

    pool = await asyncpg.create_pool('postgresql://postgres:postgres@localhost:5432/ai_db')
    token_mgr = KISTokenManager()
    broker = KISUSBroker(token_manager=token_mgr)
    broker._session = aiohttp.ClientSession()

    now = datetime.now()

    # 청산된 종목의 실제 체결가 (KIS 체결 내역에서 확인한 값)
    known_exit_prices = {
        'SEI': 63.50,   # 2주 @ $63.50 (03/17 체결 확인)
    }

    try:
        # KIS 실제 포지션
        balance = await broker.get_balance()
        kis_pos = {p['symbol']: p for p in balance.get('positions', [])}
        print('KIS 실제 포지션:')
        for sym, p in kis_pos.items():
            print(f'  {sym}: {p["qty"]}주 @ ${p["avg_price"]:.2f}  현재가 ${p.get("current_price", 0):.2f}')

        async with pool.acquire() as conn:
            db_rows = await conn.fetch(
                """SELECT id, symbol, entry_time, entry_price, entry_quantity
                   FROM trades WHERE market='US' AND exit_time IS NULL
                   ORDER BY symbol, entry_time ASC"""
            )

        # symbol별 그룹화
        from collections import defaultdict
        db_by_sym = defaultdict(list)
        for r in db_rows:
            db_by_sym[r['symbol']].append(dict(r))

        print(f'\n정리 시작...\n')
        total_closed = 0
        total_updated = 0

        async with pool.acquire() as conn:
            for sym, records in sorted(db_by_sym.items()):
                kis = kis_pos.get(sym)
                kis_qty = int(kis['qty']) if kis else 0
                kis_price = float(kis.get('current_price') or kis.get('avg_price') or 0) if kis else 0
                db_total = sum(r['entry_quantity'] for r in records)

                if db_total == kis_qty and len(records) == 1:
                    print(f'  ✅ {sym}: DB={db_total}주 = KIS={kis_qty}주 (정상)')
                    continue

                print(f'  ⚠️  {sym}: DB={db_total}주({len(records)}건) → KIS={kis_qty}주  처리 시작')

                remaining = kis_qty
                # 최신 레코드 우선 보존 → 오래된 레코드부터 close
                # (KIS avg_price가 최신 레코드와 일치하는 게 일반적)
                for i, rec in enumerate(reversed(records)):
                    rec_qty = rec['entry_quantity']
                    rec_price = float(rec['entry_price'])
                    trade_id = rec['id']

                    if remaining <= 0:
                        # 이 레코드는 전량 close
                        exit_price = (known_exit_prices.get(sym)
                                      or (kis_price if kis_price > 0 else None)
                                      or rec_price)
                        pnl = round((exit_price - rec_price) * rec_qty, 2)
                        pct = round((exit_price - rec_price) / rec_price * 100, 2) if rec_price > 0 else 0
                        await conn.execute(
                            """UPDATE trades SET exit_time=$1, exit_price=$2, exit_quantity=$3,
                               exit_reason=$4, exit_type=$5, pnl=$6, pnl_pct=$7, updated_at=$8
                               WHERE id=$9 AND exit_time IS NULL""",
                            now, exit_price, rec_qty,
                            'sync_qty_reconcile (수동 정리: 전량 청산)', 'sync_reconcile',
                            pnl, pct, now, trade_id
                        )
                        print(f'    close {trade_id[:24]}: {rec_qty}주@${rec_price:.2f} → exit${exit_price:.2f} pnl={pnl:+.2f}')
                        total_closed += 1
                    elif rec_qty <= remaining:
                        # 이 레코드는 그대로 유지 (remaining 차감)
                        remaining -= rec_qty
                        print(f'    keep  {trade_id[:24]}: {rec_qty}주 (remaining={remaining}주)')
                    else:
                        # 이 레코드는 부분 매도: remaining만 남기고 나머지 close
                        sold_qty = rec_qty - remaining
                        exit_price = (known_exit_prices.get(sym)
                                      or (kis_price if kis_price > 0 else rec_price))
                        pnl = round((exit_price - rec_price) * sold_qty, 2)
                        pct = round((exit_price - rec_price) / rec_price * 100, 2) if rec_price > 0 else 0
                        # entry_quantity를 remaining으로 업데이트
                        await conn.execute(
                            """UPDATE trades SET entry_quantity=$1, updated_at=$2
                               WHERE id=$3 AND exit_time IS NULL""",
                            remaining, now, trade_id
                        )
                        print(f'    trim  {trade_id[:24]}: {rec_qty}주 → {remaining}주 (매도 {sold_qty}주 기록, pnl={pnl:+.2f})')
                        total_updated += 1
                        remaining = 0

                # kis qty=0이면 reversed loop에서 remaining=0으로 시작하여 모두 close됨

        print(f'\n완료: close {total_closed}건, trim {total_updated}건')

        # 결과 확인
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """SELECT symbol, id, entry_quantity, entry_price
                   FROM trades WHERE market='US' AND exit_time IS NULL
                   ORDER BY symbol, entry_time"""
            )
        print(f'\n정리 후 DB 오픈 포지션: {len(rows)}건')
        for r in rows:
            print(f'  {r["symbol"]:6} {r["entry_quantity"]}주 @ ${r["entry_price"]}  id={r["id"][:24]}')

    finally:
        await broker._session.close()
        await pool.close()


if __name__ == '__main__':
    asyncio.run(main())
