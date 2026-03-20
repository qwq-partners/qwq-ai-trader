#!/usr/bin/env python3
"""
H0NXCNT0 WebSocket 검증 스크립트
넥스트장(15:40~20:00) 시간에 실행 → 시간외단일가 실시간 체결가 수신 확인

사용법:
    cd /home/user/projects/qwq-ai-trader
    source venv/bin/activate
    python scripts/test_nxt_websocket.py [--duration 120] [--symbols 005930,000660]
"""

import argparse
import asyncio
import json
import os
import sys
import time
from datetime import datetime

import aiohttp
import websockets
from dotenv import load_dotenv

# .env 로드
load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

BASE_URL   = "https://openapi.koreainvestment.com:9443"
WS_URL     = "ws://ops.koreainvestment.com:21000"
APP_KEY    = os.getenv("KIS_APPKEY", "") or os.getenv("KIS_APP_KEY", "")
APP_SECRET = os.getenv("KIS_APPSECRET", "") or os.getenv("KIS_SECRET_KEY", "")

TR_NXT_PRICE     = "H0NXCNT0"
TR_NXT_ORDERBOOK = "H0NXASP0"
TR_REG_PRICE     = "H0STCNT0"  # 비교용 (정규장 TR)

# 기본 NXT 대상 종목 (삼전, SK하이닉스, 셀트리온, 카카오, NAVER)
DEFAULT_SYMBOLS = ["005930", "000660", "068270", "035720", "035420"]


async def get_approval_key() -> str:
    """KIS WebSocket 접속키 발급"""
    url = f"{BASE_URL}/oauth2/Approval"
    payload = {
        "grant_type": "client_credentials",
        "appkey": APP_KEY,
        "secretkey": APP_SECRET,
    }
    async with aiohttp.ClientSession() as sess:
        async with sess.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            data = await resp.json(content_type=None)
            key = data.get("approval_key", "")
            if not key:
                raise RuntimeError(f"approval_key 발급 실패: {data}")
            print(f"✅ approval_key 발급 완료 (앞 20자: {key[:20]}...)")
            return key


async def get_nxt_symbols_rest(limit: int = 30) -> list[str]:
    """거래량 상위 NXT 종목 조회 (FHKST01010900: 시간외단일가 거래현황)
    실패 시 DEFAULT_SYMBOLS 반환
    """
    url = f"{BASE_URL}/uapi/domestic-stock/v1/quotations/volume-rank"
    try:
        # 현재가 기준 거래량 순위 → 일단 상위 종목 가져옴 (NXT 필터는 별도)
        # 실제 NXT 종목 목록은 봇 내부 broker.get_nxt_symbols() 사용
        # 여기서는 간단히 대형주 코드로 대체
        print(f"ℹ️  NXT 대상 종목: {DEFAULT_SYMBOLS[:limit]} (기본 대형주 사용)")
        return DEFAULT_SYMBOLS[:limit]
    except Exception as e:
        print(f"⚠️  종목 조회 실패, 기본값 사용: {e}")
        return DEFAULT_SYMBOLS


def make_subscribe_msg(approval_key: str, symbol: str, tr_id: str, subscribe: bool = True) -> dict:
    return {
        "header": {
            "approval_key": approval_key,
            "custtype": "P",
            "tr_type": "1" if subscribe else "2",
            "content-type": "utf-8",
        },
        "body": {
            "input": {
                "tr_id": tr_id,
                "tr_key": symbol,
            }
        }
    }


async def run_test(symbols: list[str], duration: int):
    """H0NXCNT0 WebSocket 연결 및 수신 테스트"""
    now = datetime.now()
    print(f"\n{'='*60}")
    print(f"H0NXCNT0 WS 검증 시작: {now.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"대상 종목: {symbols}")
    print(f"테스트 시간: {duration}초")
    print(f"{'='*60}\n")

    # 1. approval_key 발급
    try:
        approval_key = await get_approval_key()
    except Exception as e:
        print(f"❌ approval_key 발급 실패: {e}")
        return

    # 통계
    stats = {
        "connected": False,
        "first_msg_ts": None,
        "total_msgs": 0,
        "price_msgs": 0,       # H0NXCNT0 체결가
        "orderbook_msgs": 0,   # H0NXASP0 호가
        "error_msgs": 0,
        "pong_msgs": 0,
        "received": {},        # symbol → [price, ...]
        "errors": [],
    }

    start_ts = time.time()

    try:
        print(f"🔌 WS 연결: {WS_URL}")
        async with websockets.connect(
            WS_URL,
            ping_interval=None,
            open_timeout=15,
        ) as ws:
            stats["connected"] = True
            print(f"✅ WS 연결 성공\n")

            # 2. H0NXCNT0 구독 (체결가 + 호가)
            # 넥스트장 개장 직후 서버 준비 대기
            await asyncio.sleep(3)
            for sym in symbols:
                for tr_id in (TR_NXT_PRICE, TR_NXT_ORDERBOOK):
                    msg = make_subscribe_msg(approval_key, sym, tr_id, subscribe=True)
                    await ws.send(json.dumps(msg))
                    await asyncio.sleep(0.1)
            print(f"📡 구독 완료: {len(symbols)}종목 × 2TR (H0NXCNT0 + H0NXASP0)\n")

            # 3. 메시지 수신 루프
            deadline = start_ts + duration
            async for raw_msg in ws:
                now_ts = time.time()
                elapsed = now_ts - start_ts

                if now_ts >= deadline:
                    break

                stats["total_msgs"] += 1
                if stats["first_msg_ts"] is None:
                    stats["first_msg_ts"] = elapsed
                    print(f"⚡ 첫 메시지 수신: {elapsed:.1f}초 후")

                # JSON 응답 (구독 확인 / 에러)
                if isinstance(raw_msg, str) and raw_msg.startswith("{"):
                    try:
                        j = json.loads(raw_msg)
                        body = j.get("body", {})
                        rt_cd = body.get("rt_cd", "")
                        msg_text = body.get("msg1", "")
                        tr_id = j.get("header", {}).get("tr_id", "")

                        if rt_cd == "0":
                            print(f"  ✅ 구독 확인 [{tr_id}]: {msg_text}")
                        elif rt_cd == "1":
                            # PINGPONG
                            stats["pong_msgs"] += 1
                            await ws.send(raw_msg)  # PONG 응답
                        else:
                            stats["errors"].append(f"rt_cd={rt_cd} {msg_text}")
                            print(f"  ❌ 에러 [{tr_id}] rt_cd={rt_cd}: {msg_text}")
                    except Exception:
                        pass
                    continue

                # 파이프 구분 데이터 (실시간 체결/호가)
                if isinstance(raw_msg, str) and "|" in raw_msg:
                    parts = raw_msg.split("|")
                    if len(parts) < 4:
                        continue

                    tr_id = parts[1]
                    count = int(parts[2]) if parts[2].isdigit() else 1
                    data = parts[3]

                    if tr_id == TR_NXT_PRICE:
                        stats["price_msgs"] += 1
                        fields = data.split("^")
                        if len(fields) >= 10:
                            sym = fields[0].zfill(6)
                            price = int(fields[2]) if fields[2].isdigit() else 0
                            chg_pct = float(fields[5]) if fields[5] else 0.0
                            vol = int(fields[13]) if len(fields) > 13 and fields[13].isdigit() else 0

                            if sym not in stats["received"]:
                                stats["received"][sym] = []
                            stats["received"][sym].append(price)

                            # 처음 5건 상세 출력
                            if stats["price_msgs"] <= 5 or stats["price_msgs"] % 20 == 0:
                                ts = datetime.now().strftime("%H:%M:%S")
                                print(f"  [{ts}] H0NXCNT0 | {sym} | {price:>10,}원 | {chg_pct:+.2f}% | vol={vol:,}")

                    elif tr_id == TR_NXT_ORDERBOOK:
                        stats["orderbook_msgs"] += 1
                        fields = data.split("^")
                        if len(fields) >= 15 and stats["orderbook_msgs"] <= 3:
                            sym = fields[0].zfill(6)
                            ask = int(fields[3]) if fields[3].isdigit() else 0
                            bid = int(fields[13]) if len(fields) > 13 and fields[13].isdigit() else 0
                            ts = datetime.now().strftime("%H:%M:%S")
                            print(f"  [{ts}] H0NXASP0 | {sym} | 매도={ask:>10,} | 매수={bid:>10,}")

                # 남은 시간 표시 (30초마다)
                if stats["total_msgs"] % 50 == 0:
                    remaining = max(0, int(deadline - now_ts))
                    print(f"  ... {elapsed:.0f}초 경과 | 수신={stats['total_msgs']}건 (price={stats['price_msgs']}, book={stats['orderbook_msgs']}) | 남은={remaining}초")

    except asyncio.TimeoutError:
        print(f"\n⏰ {duration}초 완료")
    except Exception as e:
        print(f"\n❌ WS 오류: {type(e).__name__}: {e}")
        stats["errors"].append(str(e))

    # 4. 결과 리포트
    elapsed_total = time.time() - start_ts
    print(f"\n{'='*60}")
    print(f"검증 결과 요약 ({elapsed_total:.0f}초)")
    print(f"{'='*60}")
    ws_ok   = "✅ 성공" if stats["connected"] else "❌ 실패"
    first_t = f"{stats['first_msg_ts']:.1f}초 후" if stats["first_msg_ts"] else "❌ 수신 없음"
    price_s = "✅" if stats["price_msgs"] > 0 else "❌ 0건 — 넥스트장 WS 미지원 또는 장 외 시간"
    print(f"  WS 연결:       {ws_ok}")
    print(f"  첫 메시지:     {first_t}")
    print(f"  총 메시지:     {stats['total_msgs']}건")
    print(f"  H0NXCNT0:      {stats['price_msgs']}건 {price_s}")
    print(f"  H0NXASP0:      {stats['orderbook_msgs']}건")
    print(f"  PINGPONG:      {stats['pong_msgs']}건")

    if stats["received"]:
        print(f"\n  종목별 수신:")
        for sym, prices in stats["received"].items():
            avg = sum(prices) / len(prices)
            print(f"    {sym}: {len(prices)}건, 가격범위={min(prices):,}~{max(prices):,}원 (평균={avg:,.0f})")
    else:
        print(f"\n  ⚠️  체결가 수신 없음 → 넥스트장 시간(15:40~20:00)에 재시도 필요")

    if stats["errors"]:
        print(f"\n  에러:")
        for e in stats["errors"][:5]:
            print(f"    - {e}")

    print(f"\n{'='*60}")

    # 5. 진단
    if stats["price_msgs"] > 0:
        print("✅ H0NXCNT0 WS 지원 확인 → kis_websocket.py NEXT 세션 WS 활성화 가능")
    elif stats["connected"] and stats["total_msgs"] > 0:
        print("⚠️  연결은 됐지만 체결가 없음 → 넥스트장 종목 거래 없거나 시간 외")
    elif stats["connected"]:
        print("❌ 연결은 됐지만 메시지 없음 → 구독 오류 또는 장 외 시간")
    else:
        print("❌ WS 연결 자체 실패")
    print()


def main():
    parser = argparse.ArgumentParser(description="H0NXCNT0 WS 검증")
    parser.add_argument("--duration", type=int, default=120, help="테스트 시간(초), 기본=120")
    parser.add_argument("--symbols", type=str, default="",
                        help="종목코드 콤마 구분 (기본: 대형주 5개)")
    args = parser.parse_args()

    if not APP_KEY or not APP_SECRET:
        print("❌ KIS_APPKEY / KIS_APPSECRET 환경변수 없음")
        sys.exit(1)

    symbols = [s.strip().zfill(6) for s in args.symbols.split(",") if s.strip()] \
              if args.symbols else DEFAULT_SYMBOLS

    asyncio.run(run_test(symbols, args.duration))


if __name__ == "__main__":
    main()
