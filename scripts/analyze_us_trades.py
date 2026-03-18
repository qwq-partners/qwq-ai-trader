"""
KIS API를 통한 US 실제 체결 내역 분석 (NASD + NYSE 통합)
응답 필드 소문자 기준 (KIS inquire-ccnl 실제 응답 포맷)
"""
import asyncio, sys, os
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), '..', '.env'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from datetime import datetime, timedelta
from collections import defaultdict


async def fetch_orders_by_exchange(broker, excg, start_date, end_date):
    url = f'{broker.config.base_url}/uapi/overseas-stock/v1/trading/inquire-ccnl'
    ctx_fk, ctx_nk = "", ""
    orders = []

    for page in range(20):
        params = {
            'CANO': broker.config.account_no,
            'ACNT_PRDT_CD': broker.config.account_product_cd,
            'PDNO': '',
            'ORD_STRT_DT': start_date,
            'ORD_END_DT': end_date,
            'SLL_BUY_DVSN': '00',
            'CCLD_NCCS_DVSN': '00',
            'OVRS_EXCG_CD': excg,
            'SORT_SQN': 'DS',
            'ORD_DT': '',
            'ORD_GNO_BRNO': '',
            'ODNO': '',
            'CTX_AREA_NK200': ctx_nk,
            'CTX_AREA_FK200': ctx_fk,
        }
        data = await broker._api_get(url, broker._tr_ccld, params, return_headers=True)
        if data.get('rt_cd') != '0':
            break

        # 실제 응답 필드명은 소문자
        output = data.get('output', data.get('output1', []))
        for item in output:
            order_no = item.get('odno', '').strip()
            if not order_no:
                continue
            filled_qty = int(item.get('ft_ccld_qty', '0') or '0')
            ord_qty    = int(item.get('ft_ord_qty',  '0') or '0')
            nccs_qty   = int(item.get('nccs_qty',    '0') or '0')
            stat_name  = item.get('prcs_stat_name', '')

            if stat_name in ('완료',) and filled_qty > 0:
                status = 'filled'
            elif filled_qty > 0 and nccs_qty > 0:
                status = 'partial'
            elif filled_qty == 0:
                status = 'pending'
            else:
                status = 'filled'

            sll_buy = item.get('sll_buy_dvsn_cd', '')
            side = 'sell' if sll_buy == '01' else 'buy'

            orders.append({
                'order_no': order_no,
                'symbol': item.get('pdno', '').strip(),
                'name': item.get('prdt_name', ''),
                'side': side,
                'qty': ord_qty,
                'price': float(item.get('ft_ord_unpr3', '0') or '0'),
                'filled_qty': filled_qty,
                'filled_price': float(item.get('ft_ccld_unpr3', '0') or '0'),
                'filled_amt': float(item.get('ft_ccld_amt3', '0') or '0'),
                'status': status,
                'stat_name': stat_name,
                'time': item.get('ord_tmd', ''),
                'date': item.get('ord_dt', ''),
                'exchange': item.get('ovrs_excg_cd', excg),
                'market': item.get('tr_mket_name', ''),
            })

        tr_cont = data.get('_tr_cont', '')
        new_fk  = (data.get('ctx_area_fk200') or '').strip()
        new_nk  = (data.get('ctx_area_nk200') or '').strip()
        if tr_cont in ('M', 'F') and (new_fk or new_nk):
            if new_fk == ctx_fk and new_nk == ctx_nk:
                break
            ctx_fk, ctx_nk = new_fk, new_nk
        else:
            break

    return orders


async def main():
    import aiohttp
    from src.execution.broker.kis_us import KISUSBroker
    from src.utils.token_manager import KISTokenManager

    token_mgr = KISTokenManager()
    broker = KISUSBroker(token_manager=token_mgr)
    broker._session = aiohttp.ClientSession()

    today     = datetime.now().strftime('%Y%m%d')
    yesterday = (datetime.now() - timedelta(days=1)).strftime('%Y%m%d')
    print(f'📅 조회 기간: {yesterday} ~ {today}')
    print(f'   현재 시각: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")} KST\n')

    try:
        all_orders_map = {}
        for excg in ['NASD', 'NYSE']:
            ords = await fetch_orders_by_exchange(broker, excg, yesterday, today)
            print(f'  {excg}: {len(ords)}건')
            for o in ords:
                all_orders_map[o['order_no']] = o

        orders = list(all_orders_map.values())
        orders.sort(key=lambda x: (x.get('date', ''), x.get('time', '')), reverse=True)
        print(f'\n총 {len(orders)}건 (중복제거 후)\n')

        filled  = [o for o in orders if o['status'] == 'filled']
        pending = [o for o in orders if o['status'] == 'pending']
        partial = [o for o in orders if o['status'] == 'partial']

        buys  = [o for o in filled if o['side'] == 'buy']
        sells = [o for o in filled if o['side'] == 'sell']

        # ── 매수 ──
        print('=' * 70)
        print(f'🟢 체결 매수 ({len(buys)}건)')
        print('=' * 70)
        buy_total = 0.0
        for o in buys:
            amt = o['filled_amt'] if o['filled_amt'] > 0 else o['filled_qty'] * o['filled_price']
            buy_total += amt
            dt = f"{o['date'][4:6]}/{o['date'][6:8]}" if len(o['date']) == 8 else o['date']
            print(f"  {dt} {o['time'][:4]}  {o['symbol']:6} ({o['name']:10})  {o['filled_qty']:3}주 @ ${o['filled_price']:8.3f}  = ${amt:8.2f}  [{o['market']}]")
        print(f"  소계: ${buy_total:.2f}\n")

        # ── 매도 ──
        print('=' * 70)
        print(f'🔴 체결 매도 ({len(sells)}건)')
        print('=' * 70)
        sell_total = 0.0
        for o in sells:
            amt = o['filled_amt'] if o['filled_amt'] > 0 else o['filled_qty'] * o['filled_price']
            sell_total += amt
            dt = f"{o['date'][4:6]}/{o['date'][6:8]}" if len(o['date']) == 8 else o['date']
            print(f"  {dt} {o['time'][:4]}  {o['symbol']:6} ({o['name']:10})  {o['filled_qty']:3}주 @ ${o['filled_price']:8.3f}  = ${amt:8.2f}  [{o['market']}]")
        print(f"  소계: ${sell_total:.2f}\n")

        if pending:
            print('=' * 70)
            print(f'⏳ 미체결 ({len(pending)}건)')
            print('=' * 70)
            for o in pending:
                dt = f"{o['date'][4:6]}/{o['date'][6:8]}" if len(o['date']) == 8 else o['date']
                print(f"  {dt} {o['time'][:4]}  {o['symbol']:6}  {o['side']:4} {o['qty']:3}주 @ ${o['price']:.3f}  [{o['market']}]")
            print()

        if partial:
            print('=' * 70)
            print(f'⚠️  부분체결 ({len(partial)}건)')
            print('=' * 70)
            for o in partial:
                dt = f"{o['date'][4:6]}/{o['date'][6:8]}" if len(o['date']) == 8 else o['date']
                print(f"  {dt} {o['time'][:4]}  {o['symbol']:6}  {o['side']:4} {o['filled_qty']}/{o['qty']}주 @ ${o['filled_price']:.3f}  [{o['market']}]")
            print()

        # ── 종목별 P&L ──
        print('=' * 70)
        print('📊 종목별 집계')
        print('=' * 70)
        sym_data = defaultdict(lambda: {'buy_qty': 0, 'buy_amt': 0.0, 'sell_qty': 0, 'sell_amt': 0.0, 'name': ''})
        for o in buys:
            sym_data[o['symbol']]['buy_qty']  += o['filled_qty']
            sym_data[o['symbol']]['buy_amt']  += o['filled_qty'] * o['filled_price']
            sym_data[o['symbol']]['name'] = o['name']
        for o in sells:
            sym_data[o['symbol']]['sell_qty'] += o['filled_qty']
            sym_data[o['symbol']]['sell_amt'] += o['filled_qty'] * o['filled_price']
            if not sym_data[o['symbol']]['name']:
                sym_data[o['symbol']]['name'] = o['name']

        total_realized = 0.0
        for sym, d in sorted(sym_data.items()):
            avg_buy  = d['buy_amt']  / d['buy_qty']  if d['buy_qty']  > 0 else 0
            avg_sell = d['sell_amt'] / d['sell_qty'] if d['sell_qty'] > 0 else 0
            matched  = min(d['buy_qty'], d['sell_qty'])
            name     = d['name']

            if matched > 0 and avg_buy > 0:
                realized = (avg_sell - avg_buy) * matched
                pct = (avg_sell - avg_buy) / avg_buy * 100
                total_realized += realized
                icon = '✅' if realized > 0 else '❌'
                print(f"  {icon} {sym:6} ({name:10})  매수{d['buy_qty']:3}주@${avg_buy:.2f}  매도{d['sell_qty']:3}주@${avg_sell:.2f}  → {realized:+.2f} USD ({pct:+.1f}%)")
            elif d['buy_qty'] > 0:
                print(f"  📌 {sym:6} ({name:10})  매수{d['buy_qty']:3}주@${avg_buy:.2f}  (보유중)")
            elif d['sell_qty'] > 0:
                print(f"  📤 {sym:6} ({name:10})  매도{d['sell_qty']:3}주@${avg_sell:.2f}  (기존 포지션 청산)")

        print(f'\n{"=" * 70}')
        wins   = len([s for s in sym_data.values() if min(s['buy_qty'], s['sell_qty']) > 0 and s['buy_qty'] > 0 and (s['sell_amt']/s['sell_qty'] - s['buy_amt']/s['buy_qty']) > 0])
        losses = len([s for s in sym_data.values() if min(s['buy_qty'], s['sell_qty']) > 0 and s['buy_qty'] > 0 and (s['sell_amt']/s['sell_qty'] - s['buy_amt']/s['buy_qty']) <= 0])
        print(f'💰 추정 실현손익: {total_realized:+.2f} USD   (승 {wins} / 패 {losses})')
        print(f'   매수 총액: ${buy_total:.2f}   매도 총액: ${sell_total:.2f}')
        print(f'   체결 매수 {len(buys)}건  |  체결 매도 {len(sells)}건  |  미체결 {len(pending)}건')
        print('=' * 70)

    finally:
        await broker._session.close()


if __name__ == '__main__':
    asyncio.run(main())
