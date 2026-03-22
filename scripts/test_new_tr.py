#!/usr/bin/env python3
"""
신규 KIS TR 검증 스크립트 (2026-03-22 추가)

검증 대상:
  1. FHKST644100C0 — 외국계 매매종목 가집계 (fetch_frgnmem_trade_estimate)
  2. HHPTJ04160200 — 종목별 외인/기관 추정가집계 (fetch_investor_trend_estimate)

사용법:
    source venv/bin/activate
    python scripts/test_new_tr.py          # 전체 테스트 + 텔레그램 전송
    python scripts/test_new_tr.py --no-tg  # 텔레그램 없이 콘솔만
    python scripts/test_new_tr.py --tr1    # FHKST644100C0 만
    python scripts/test_new_tr.py --tr2    # HHPTJ04160200 만

장중(09:00~15:30) 실행 권장. 장 외 시간엔 빈 응답일 수 있음.
"""

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime
from typing import Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ── 검증용 종목 (대형주 + 소형주 혼합) ─────────────────────────
TEST_SYMBOLS = [
    ("005930", "삼성전자"),
    ("000660", "SK하이닉스"),
    ("005380", "현대차"),
    ("035420", "NAVER"),
    ("051910", "LG화학"),
]

# ── 텔레그램 설정 ────────────────────────────────────────────────
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "1754899925")


async def send_telegram(text: str) -> bool:
    """텔레그램 메시지 전송"""
    if not TELEGRAM_TOKEN:
        print("[TG] TELEGRAM_BOT_TOKEN 없음 → 전송 스킵")
        return False
    try:
        import aiohttp
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"}
        async with aiohttp.ClientSession() as sess:
            async with sess.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as r:
                return r.status == 200
    except Exception as e:
        print(f"[TG] 전송 실패: {e}")
        return False


# ════════════════════════════════════════════════════════════════
# TEST 1: FHKST644100C0 — 외국계 매매종목 가집계
# ════════════════════════════════════════════════════════════════

async def test_tr1_frgnmem_estimate(kis_md) -> Dict:
    """
    FHKST644100C0 응답 검증
    - 응답 유무 확인
    - 반환 종목 수
    - 실제 output 필드명 확인
    - 코스피 / 코스닥 별도 확인
    """
    print("\n" + "=" * 60)
    print("TR1: FHKST644100C0 — 외국계 매매종목 가집계")
    print("=" * 60)

    result = {"tr": "FHKST644100C0", "ok": False, "markets": {}, "raw_keys": [], "error": None}

    for mkt_code, mkt_name in [("1001", "코스피"), ("2001", "코스닥"), ("0000", "전체")]:
        try:
            data = await kis_md.fetch_frgnmem_trade_estimate(market=mkt_code, sort_cls="0")
            count = len(data)
            print(f"  [{mkt_name}({mkt_code})] → {count}종목")

            if data:
                # 첫 번째 항목 상세 출력
                first = data[0]
                print(f"    1위: {first.get('symbol')} {first.get('name')}")
                print(f"         순매수qty={first.get('net_buy_qty'):,}  amt={first.get('net_buy_amt'):,}")
                print(f"         price={first.get('price')}  chg={first.get('change_pct'):+.2f}%")
                result["markets"][mkt_name] = {"count": count, "top1": first}
                result["ok"] = True
            else:
                print(f"    → 빈 응답 (장 외 시간이거나 API 오류)")
                result["markets"][mkt_name] = {"count": 0}
        except Exception as e:
            print(f"    → 오류: {e}")
            result["error"] = str(e)

    # 원본 필드명 확인 (캐시 초기화 후 재호출로 DEBUG 강제)
    try:
        import aiohttp
        from src.utils.token_manager import get_token_manager
        tm = get_token_manager()
        token = await tm.get_access_token()
        headers = {
            "Content-Type": "application/json; charset=utf-8",
            "authorization": f"Bearer {token}",
            "appkey": tm.app_key,
            "appsecret": tm.app_secret,
            "tr_id": "FHKST644100C0",
        }
        url = f"{tm.base_url}/uapi/domestic-stock/v1/quotations/frgnmem-trade-estimate"
        params = {
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_COND_SCR_DIV_CODE": "16441",
            "FID_INPUT_ISCD": "1001",
            "FID_RANK_SORT_CLS_CODE": "0",
            "FID_RANK_SORT_CLS_CODE_2": "0",
        }
        async with aiohttp.ClientSession() as sess:
            async with sess.get(url, headers=headers, params=params,
                                timeout=aiohttp.ClientTimeout(total=15)) as resp:
                raw = await resp.json()
                rt_cd = raw.get("rt_cd")
                output = raw.get("output", [])
                print(f"\n  [원본 응답] rt_cd={rt_cd}  msg1={raw.get('msg1','')}")
                if isinstance(output, list) and output:
                    keys = list(output[0].keys())
                    print(f"  [output[0] keys({len(keys)}개)]: {keys}")
                    result["raw_keys"] = keys
                    # 주요 필드값 출력
                    print("  [output[0] 주요값]:")
                    for k, v in output[0].items():
                        if v and v != "0" and v != "":
                            print(f"    {k}: {v}")
                elif isinstance(output, dict) and output:
                    keys = list(output.keys())
                    print(f"  [output keys({len(keys)}개)]: {keys}")
                    result["raw_keys"] = keys
                else:
                    print(f"  [output] {type(output).__name__} = {output}")
    except Exception as e:
        print(f"  [원본 응답 조회 오류] {e}")

    return result


# ════════════════════════════════════════════════════════════════
# TEST 2: HHPTJ04160200 — 종목별 외인/기관 추정가집계
# ════════════════════════════════════════════════════════════════

async def test_tr2_investor_estimate(kis_md) -> Dict:
    """
    HHPTJ04160200 응답 검증
    - 응답 유무 확인
    - 실제 output 필드명 확인 (⚠️ 미확인 상태)
    - frgn_ntby_qty / inst_ntby_qty 추출 여부
    - 종목별 결과 비교
    """
    print("\n" + "=" * 60)
    print("TR2: HHPTJ04160200 — 종목별 외인/기관 추정가집계")
    print("(집계 시간: 외국인 09:30/11:20/13:20/14:30, 기관 10:00/11:20/13:20/14:30)")
    print("=" * 60)

    result = {"tr": "HHPTJ04160200", "ok": False, "symbols": {}, "raw_keys": [], "error": None}

    for symbol, name in TEST_SYMBOLS:
        try:
            # 캐시 강제 우회: 직접 raw 조회
            import aiohttp
            from src.utils.token_manager import get_token_manager
            tm = get_token_manager()
            token = await tm.get_access_token()
            headers = {
                "Content-Type": "application/json; charset=utf-8",
                "authorization": f"Bearer {token}",
                "appkey": tm.app_key,
                "appsecret": tm.app_secret,
                "tr_id": "HHPTJ04160200",
            }
            url = f"{tm.base_url}/uapi/domestic-stock/v1/quotations/investor-trend-estimate"
            params = {"MKSC_SHRN_ISCD": symbol}

            async with aiohttp.ClientSession() as sess:
                async with sess.get(url, headers=headers, params=params,
                                    timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    raw = await resp.json()

            rt_cd = raw.get("rt_cd")
            msg1 = raw.get("msg1", "")
            output = raw.get("output", {}) or {}
            if isinstance(output, list):
                output = output[0] if output else {}

            if rt_cd != "0":
                print(f"  [{name}({symbol})] rt_cd={rt_cd}  msg={msg1}")
                result["symbols"][symbol] = {"ok": False, "rt_cd": rt_cd, "msg": msg1}
                continue

            # 필드명 탐색 결과 저장
            if not result["raw_keys"] and output:
                result["raw_keys"] = list(output.keys())

            # 추출 시도 (여러 패턴)
            frgn = int(output.get("frgn_ntby_qty") or output.get("frgn_stkp_qty")
                       or output.get("frgn_est_ntby_qty") or 0)
            inst = int(output.get("orgn_ntby_qty") or output.get("inst_ntby_qty")
                       or output.get("inst_est_ntby_qty") or 0)

            print(f"  [{name}({symbol})] frgn_net={frgn:+,}  inst_net={inst:+,}")
            result["symbols"][symbol] = {"ok": True, "frgn": frgn, "inst": inst}
            result["ok"] = True

        except Exception as e:
            print(f"  [{name}({symbol})] 오류: {e}")
            result["error"] = str(e)

    # raw keys 출력 (최초 성공 종목 기준)
    if result["raw_keys"]:
        print(f"\n  ✅ output 필드명 확인됨({len(result['raw_keys'])}개): {result['raw_keys']}")
        # 실제 값이 있는 필드만 찾기 위해 한 번 더 조회
        try:
            import aiohttp
            from src.utils.token_manager import get_token_manager
            tm = get_token_manager()
            token = await tm.get_access_token()
            headers = {
                "Content-Type": "application/json; charset=utf-8",
                "authorization": f"Bearer {token}",
                "appkey": tm.app_key,
                "appsecret": tm.app_secret,
                "tr_id": "HHPTJ04160200",
            }
            url = f"{tm.base_url}/uapi/domestic-stock/v1/quotations/investor-trend-estimate"
            # 삼성전자로 상세 확인
            params = {"MKSC_SHRN_ISCD": "005930"}
            async with aiohttp.ClientSession() as sess:
                async with sess.get(url, headers=headers, params=params,
                                    timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    raw2 = await resp.json()
            output2 = raw2.get("output", {}) or {}
            if isinstance(output2, list):
                output2 = output2[0] if output2 else {}
            print("\n  [삼성전자 output 전체 (비어있지 않은 필드)]:")
            for k, v in output2.items():
                print(f"    {k}: {repr(v)}")
        except Exception as e:
            print(f"  [상세 조회 오류]: {e}")
    else:
        print("\n  ⚠️  유효한 응답 없음 (장 외 시간이거나 집계 전일 수 있음)")
        print("  ℹ️  집계 시간 이후 재실행: 09:30, 10:00, 11:20, 13:20, 14:30")

    return result


# ════════════════════════════════════════════════════════════════
# TEST 3: 0차 수급이 실제 스크리너에 주입되는지 로그 확인
# ════════════════════════════════════════════════════════════════

def test_tr3_screener_log():
    """
    최근 봇 로그에서 0차 수급 주입 확인
    (장중 실행된 배치 스캔 로그 탐색)
    """
    print("\n" + "=" * 60)
    print("TR3: 스크리너 0차 수급 주입 로그 확인")
    print("=" * 60)

    import subprocess
    try:
        result = subprocess.run(
            ["journalctl", "-u", "qwq-ai-trader", "-n", "200", "--no-pager", "--output=short"],
            capture_output=True, text=True, timeout=10
        )
        lines = result.stdout.splitlines()
        relevant = [l for l in lines if "0차" in l or "외국계가집계" in l or
                    "frgnmem" in l or "추정가집계" in l or "FHKST644" in l or "HHPTJ" in l]
        if relevant:
            print(f"  ✅ 관련 로그 {len(relevant)}건 발견:")
            for l in relevant[-10:]:
                print(f"    {l}")
        else:
            print("  ℹ️  관련 로그 없음 (아직 배치 스캔이 실행되지 않았거나 08:20/12:30 전)")
            # 가장 최근 배치 스캔 시간 확인
            scan_lines = [l for l in lines if "배치스케줄러" in l or "아침 스캔" in l or "낮 추가 스캔" in l]
            if scan_lines:
                print(f"  최근 배치 스캔 로그: {scan_lines[-1]}")
    except Exception as e:
        print(f"  [로그 조회 오류]: {e}")


# ════════════════════════════════════════════════════════════════
# 텔레그램 요약 메시지 생성
# ════════════════════════════════════════════════════════════════

def build_telegram_summary(tr1: Dict, tr2: Dict, now_str: str) -> str:
    lines = [
        f"🧪 <b>신규 KIS TR 검증 결과</b> ({now_str})\n",
    ]

    # TR1
    tr1_ok = tr1.get("ok", False)
    icon1 = "✅" if tr1_ok else "❌"
    lines.append(f"{icon1} <b>TR1: FHKST644100C0 외국계가집계</b>")
    if tr1_ok:
        for mkt, d in tr1.get("markets", {}).items():
            cnt = d.get("count", 0)
            top1 = d.get("top1")
            if top1:
                lines.append(f"  • {mkt}({cnt}종목) 1위: {top1.get('symbol')} {top1.get('name')} {top1.get('net_buy_qty'):+,}주")
            else:
                lines.append(f"  • {mkt}: {cnt}종목")
        raw_keys = tr1.get("raw_keys", [])
        if raw_keys:
            lines.append(f"  📋 필드명({len(raw_keys)}개): <code>{', '.join(raw_keys[:8])}</code>")
    else:
        err = tr1.get("error") or "빈 응답 (장 외 시간?)"
        lines.append(f"  ⚠️ {err}")

    lines.append("")

    # TR2
    tr2_ok = tr2.get("ok", False)
    icon2 = "✅" if tr2_ok else "⚠️"
    lines.append(f"{icon2} <b>TR2: HHPTJ04160200 추정가집계</b>")
    raw_keys2 = tr2.get("raw_keys", [])
    if raw_keys2:
        lines.append(f"  📋 필드명({len(raw_keys2)}개): <code>{', '.join(raw_keys2[:8])}</code>")
    else:
        lines.append(f"  ⚠️ 집계 전 or 장 외 시간 — 09:30 이후 재확인 필요")

    syms_ok = [(s, v) for s, v in tr2.get("symbols", {}).items() if v.get("ok")]
    if syms_ok:
        for sym, v in syms_ok[:3]:
            lines.append(f"  • {sym}: 외국인 {v['frgn']:+,}주  기관 {v['inst']:+,}주")
    else:
        for sym, v in list(tr2.get("symbols", {}).items())[:3]:
            if not v.get("ok"):
                lines.append(f"  • {sym}: rt_cd={v.get('rt_cd')}  {v.get('msg','')}")

    lines.append("")
    lines.append("⏰ 집계 시간: 09:30 / 10:00 / 11:20 / 13:20 / 14:30")
    lines.append("📌 로그: <code>journalctl -u qwq-ai-trader | grep 외국계가집계</code>")

    return "\n".join(lines)


# ════════════════════════════════════════════════════════════════
# MAIN
# ════════════════════════════════════════════════════════════════

async def main(args):
    from src.data.providers.kis_market_data import get_kis_market_data

    kis_md = get_kis_market_data()
    now_str = datetime.now().strftime("%m/%d %H:%M")

    tr1_result = {"ok": False}
    tr2_result = {"ok": False}

    try:
        if args.tr1 or (not args.tr2):
            tr1_result = await test_tr1_frgnmem_estimate(kis_md)

        if args.tr2 or (not args.tr1):
            tr2_result = await test_tr2_investor_estimate(kis_md)

        if not args.tr1 and not args.tr2:
            test_tr3_screener_log()

        # 텔레그램 전송
        if not args.no_tg:
            msg = build_telegram_summary(tr1_result, tr2_result, now_str)
            print("\n" + "=" * 60)
            print("텔레그램 전송 중...")
            ok = await send_telegram(msg)
            print(f"  {'✅ 전송 완료' if ok else '❌ 전송 실패 (토큰 확인 필요)'}")

    finally:
        await kis_md.close()

    print("\n" + "=" * 60)
    print(f"완료 ({now_str})")
    print("=" * 60)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="신규 KIS TR 검증")
    parser.add_argument("--no-tg", action="store_true", help="텔레그램 전송 스킵")
    parser.add_argument("--tr1", action="store_true", help="TR1(FHKST644100C0)만 테스트")
    parser.add_argument("--tr2", action="store_true", help="TR2(HHPTJ04160200)만 테스트")
    args = parser.parse_args()

    asyncio.run(main(args))
