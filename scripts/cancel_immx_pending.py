"""
IMMX 미체결 매도 주문 일괄 취소
"""
import asyncio, sys, os
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), '..', '.env'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from datetime import datetime, timedelta


async def main():
    import aiohttp
    from src.execution.broker.kis_us import KISUSBroker
    from src.utils.token_manager import KISTokenManager

    token_mgr = KISTokenManager()
    broker = KISUSBroker(token_manager=token_mgr)
    broker._session = aiohttp.ClientSession()

    today     = datetime.now().strftime('%Y%m%d')
    yesterday = (datetime.now() - timedelta(days=1)).strftime('%Y%m%d')

    try:
        # 1. 미체결 주문 조회 (get_outstanding_orders - inquire-nccs)
        print('📋 미체결 주문 조회 중...')
        pending = await broker.get_outstanding_orders()
        immx_pending = [o for o in pending if o['symbol'] == 'IMMX']
        print(f'  전체 미체결: {len(pending)}건 / IMMX: {len(immx_pending)}건\n')

        if not immx_pending:
            # inquire-ccnl에서도 확인
            print('inquire-nccs에서 없음 → inquire-ccnl로 재확인...')
            url = f'{broker.config.base_url}/uapi/overseas-stock/v1/trading/inquire-ccnl'
            params = {
                'CANO': broker.config.account_no,
                'ACNT_PRDT_CD': broker.config.account_product_cd,
                'PDNO': 'IMMX',
                'ORD_STRT_DT': yesterday,
                'ORD_END_DT': today,
                'SLL_BUY_DVSN': '01',   # 매도만
                'CCLD_NCCS_DVSN': '02', # 미체결만
                'OVRS_EXCG_CD': 'NASD',
                'SORT_SQN': 'DS',
                'ORD_DT': '', 'ORD_GNO_BRNO': '', 'ODNO': '',
                'CTX_AREA_NK200': '', 'CTX_AREA_FK200': '',
            }
            data = await broker._api_get(url, broker._tr_ccld, params, return_headers=True)
            output = data.get('output', data.get('output1', []))
            print(f'  CCLD_NCCS_DVSN=02 조회: {len(output)}건')

            for item in output:
                nccs_qty = int(item.get('nccs_qty', '0') or '0')
                if nccs_qty > 0:
                    immx_pending.append({
                        'order_no': item.get('odno', '').strip(),
                        'symbol': 'IMMX',
                        'qty': nccs_qty,
                        'price': float(item.get('ft_ord_unpr3', '0') or '0'),
                        'exchange': 'NASD',
                    })

        if not immx_pending:
            print('✅ 취소할 IMMX 미체결 주문 없음 (이미 자동 취소됐을 수 있음)')
            return

        print(f'🗑️  취소 대상 IMMX 주문 {len(immx_pending)}건:')
        for o in immx_pending:
            print(f"  order_no={o['order_no']}  qty={o.get('qty',0)}주  price=${o.get('price',0):.3f}")

        print()
        for o in immx_pending:
            order_no = o['order_no']
            qty = o.get('qty', 0)
            excg = o.get('exchange', 'NASD')
            result = await broker.cancel_order(
                order_no=order_no,
                exchange=excg,
                symbol='IMMX',
                qty=qty,
            )
            if result.get('success'):
                print(f"  ✅ {order_no} 취소 성공")
            else:
                print(f"  ❌ {order_no} 취소 실패: {result.get('message','')}")
            await asyncio.sleep(0.3)  # rate limit 보호

        print('\n완료')

    finally:
        await broker._session.close()


if __name__ == '__main__':
    asyncio.run(main())
