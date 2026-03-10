#!/usr/bin/env python3
"""
코스피200 KRX 야간선물 실시간 시세 모니터

2025-06-09부터 EUREX 연계 야간거래 종료 → KRX 자체 야간시장으로 전환
- TR_ID: H0MFCNT0 (KRX야간선물 실시간종목체결)
- 거래시간: 18:00 ~ 익일 06:00 (KST)
- 종목코드: 101W9000 (KRX 야간선물 근월물)

사용법:
    source venv/bin/activate
    python scripts/futures_monitor.py                    # 기본 (101W9000)
    python scripts/futures_monitor.py --code 101W9000    # 종목코드 지정
    python scripts/futures_monitor.py --raw              # 원시 필드 전체 출력
"""
import argparse
import asyncio
import json
import os
import signal
import sys
from datetime import datetime
from pathlib import Path

# 프로젝트 루트를 path에 추가
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.utils.config import load_dotenv

import aiohttp
from loguru import logger

# ── 로거 설정 ──
logger.remove()
logger.add(
    sys.stderr,
    format="<green>{time:HH:mm:ss}</green> | <level>{message}</level>",
    level="INFO",
)

# ── KRX 야간선물 체결가 필드 매핑 (H0MFCNT0, 실시간-064) ──
# KIS 공식 샘플 기준 46개 필드
FIELD_NAMES = [
    "선물단축종목코드",         # 0  futs_shrn_iscd
    "영업시간",                # 1  bsop_hour (HHMMSS)
    "선물전일대비",            # 2  futs_prdy_vrss
    "전일대비부호",            # 3  prdy_vrss_sign
    "선물전일대비율",          # 4  futs_prdy_ctrt
    "선물현재가",              # 5  futs_prpr
    "선물시가",                # 6  futs_oprc
    "선물최고가",              # 7  futs_hgpr
    "선물최저가",              # 8  futs_lwpr
    "최종거래량",              # 9  last_cnqn
    "누적거래량",              # 10 acml_vol
    "누적거래대금",            # 11 acml_tr_pbmn
    "HTS이론가",               # 12 hts_thpr
    "시장베이시스",            # 13 mrkt_basis
    "괴리율",                  # 14 dprt
    "근월물약정가",            # 15 nmsc_fctn_stpl_prc
    "원월물약정가",            # 16 fmsc_fctn_stpl_prc
    "스프레드",                # 17 spead_prc
    "HTS미결제약정수량",       # 18 hts_otst_stpl_qty
    "미결제약정수량증감",      # 19 otst_stpl_qty_icdc
    "시가시간",                # 20 oprc_hour
    "시가대비현재가부호",      # 21 oprc_vrss_prpr_sign
    "시가대비지수현재가",      # 22 oprc_vrss_nmix_prpr
    "최고가시간",              # 23 hgpr_hour
    "최고가대비현재가부호",    # 24 hgpr_vrss_prpr_sign
    "최고가대비지수현재가",    # 25 hgpr_vrss_nmix_prpr
    "최저가시간",              # 26 lwpr_hour
    "최저가대비현재가부호",    # 27 lwpr_vrss_prpr_sign
    "최저가대비지수현재가",    # 28 lwpr_vrss_nmix_prpr
    "매수비율",                # 29 shnu_rate
    "체결강도",                # 30 cttr
    "괴리도",                  # 31 esdg
    "미결제약정직전수량증감",  # 32 otst_stpl_rgbf_qty_icdc
    "이론베이시스",            # 33 thpr_basis
    "선물매도호가1",           # 34 futs_askp1
    "선물매수호가1",           # 35 futs_bidp1
    "매도호가잔량1",           # 36 askp_rsqn1
    "매수호가잔량1",           # 37 bidp_rsqn1
    "매도체결건수",            # 38 seln_cntg_csnu
    "매수체결건수",            # 39 shnu_cntg_csnu
    "순매수체결건수",          # 40 ntby_cntg_csnu
    "총매도수량",              # 41 seln_cntg_smtn
    "총매수수량",              # 42 shnu_cntg_smtn
    "총매도호가잔량",          # 43 total_askp_rsqn
    "총매수호가잔량",          # 44 total_bidp_rsqn
    "전일거래량대비등락율",    # 45 prdy_vol_vrss_acml_vol_rate
    "실시간상한가",            # 46 dynm_mxpr
    "실시간하한가",            # 47 dynm_llam
    "실시간가격제한구분",      # 48 dynm_prc_limt_yn
]


def _safe_float(fields, idx, default=0.0):
    """안전한 float 변환"""
    try:
        return float(fields[idx]) if len(fields) > idx and fields[idx] else default
    except (ValueError, IndexError):
        return default


def _safe_int(fields, idx, default=0):
    """안전한 int 변환"""
    try:
        return int(fields[idx]) if len(fields) > idx and fields[idx] else default
    except (ValueError, IndexError):
        return default


class FuturesMonitor:
    """코스피200 KRX 야간선물 실시간 모니터"""

    WS_URL = "ws://ops.koreainvestment.com:21000"
    TR_ID = "H0MFCNT0"  # KRX야간선물 실시간종목체결

    def __init__(self, futures_code: str = "101W9000", raw_mode: bool = False):
        self.futures_code = futures_code
        self.raw_mode = raw_mode
        self._running = False
        self._ws = None
        self._session: aiohttp.ClientSession | None = None
        self._approval_key: str | None = None
        self._msg_count = 0
        self._last_price = 0.0

    async def _get_approval_key(self) -> str | None:
        """KIS Approval Key 발급"""
        app_key = os.getenv("KIS_APPKEY", "")
        app_secret = os.getenv("KIS_APPSECRET", "")
        env = os.getenv("KIS_ENV", "prod")
        base_url = (
            "https://openapi.koreainvestment.com:9443"
            if env == "prod"
            else "https://openapivts.koreainvestment.com:29443"
        )

        url = f"{base_url}/oauth2/Approval"
        body = {
            "grant_type": "client_credentials",
            "appkey": app_key,
            "secretkey": app_secret,
        }

        try:
            async with self._session.post(url, json=body, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    logger.error(f"Approval Key 발급 실패: {resp.status} - {text}")
                    return None
                data = await resp.json()
                key = data.get("approval_key", "")
                if key:
                    logger.info("Approval Key 발급 완료")
                return key or None
        except Exception as e:
            logger.error(f"Approval Key 발급 오류: {e}")
            return None

    async def _subscribe(self):
        """야간선물 구독 요청"""
        msg = {
            "header": {
                "approval_key": self._approval_key,
                "custtype": "P",
                "tr_type": "1",  # 1: 등록
                "content-type": "utf-8",
            },
            "body": {
                "input": {
                    "tr_id": self.TR_ID,
                    "tr_key": self.futures_code,
                }
            },
        }
        await self._ws.send_json(msg)
        logger.info(f"구독 요청: TR_ID={self.TR_ID}, 종목={self.futures_code}")

    def _parse_price_data(self, raw_data: str):
        """체결가 데이터 파싱 및 출력"""
        fields = raw_data.split("^")
        self._msg_count += 1

        if self.raw_mode:
            for i, val in enumerate(fields):
                name = FIELD_NAMES[i] if i < len(FIELD_NAMES) else f"field_{i}"
                print(f"  [{i:2d}] {name}: {val}")
            print("---")
            return

        # 필드 매핑 (KIS 공식 샘플 기준)
        code = fields[0] if fields else ""
        time_str = fields[1] if len(fields) > 1 else ""
        change = _safe_float(fields, 2)       # 선물전일대비
        sign = fields[3] if len(fields) > 3 else ""  # 전일대비부호
        change_pct = _safe_float(fields, 4)   # 선물전일대비율
        price = _safe_float(fields, 5)        # 선물현재가
        open_price = _safe_float(fields, 6)   # 선물시가
        high = _safe_float(fields, 7)         # 선물최고가
        low = _safe_float(fields, 8)          # 선물최저가
        volume = _safe_int(fields, 9)         # 최종거래량
        cum_volume = _safe_int(fields, 10)    # 누적거래량
        best_ask = _safe_float(fields, 34)    # 선물매도호가1
        best_bid = _safe_float(fields, 35)    # 선물매수호가1
        strength = _safe_float(fields, 30)    # 체결강도
        oi = _safe_int(fields, 18)            # 미결제약정수량

        # 체결시간 포맷
        t = f"{time_str[:2]}:{time_str[2:4]}:{time_str[4:6]}" if len(time_str) >= 6 else time_str

        # 등락 표시
        if sign in ("1", "2"):
            arrow = "▲"
            color = "\033[91m"  # 빨강
        elif sign in ("4", "5"):
            arrow = "▼"
            color = "\033[94m"  # 파랑
        else:
            arrow = "─"
            color = "\033[0m"
        reset = "\033[0m"

        # 틱 방향 (직전가 대비)
        if self._last_price:
            tick = "↑" if price > self._last_price else ("↓" if price < self._last_price else "=")
        else:
            tick = " "
        self._last_price = price

        print(
            f"{t}  {color}{price:>8.2f}{reset} {arrow}{change:+.2f} ({change_pct:+.2f}%)  "
            f"체결 {volume:>4}  누적 {cum_volume:>8,}  "
            f"매도 {best_ask:.2f} / 매수 {best_bid:.2f}  "
            f"강도 {strength:>5.1f}  OI {oi:>6,}  "
            f"시{open_price:.2f} 고{high:.2f} 저{low:.2f} {tick}"
        )

    async def _handle_message(self, data: str):
        """수신 메시지 처리"""
        # JSON 응답 (구독 확인, PINGPONG 등)
        if data.startswith("{"):
            msg = json.loads(data)
            header = msg.get("header", {})
            body = msg.get("body", {})

            tr_id = header.get("tr_id", "")
            if tr_id == "PINGPONG":
                await self._ws.send_str(data)
                return

            rt_cd = body.get("rt_cd", "")
            msg1 = body.get("msg1", "")
            msg_cd = body.get("msg_cd", "")

            if rt_cd == "0":
                logger.info(f"구독 성공: {msg1}")
            elif rt_cd:
                logger.warning(f"구독 응답: rt_cd={rt_cd}, msg_cd={msg_cd}, msg={msg1}")

            # 승인키 오류
            if msg_cd in ("EGW00123", "EGW00121", "EGW00201"):
                logger.error(f"승인키 오류 ({msg_cd}): {msg1}")
                self._running = False
            return

        # 파이프 구분 실시간 데이터
        parts = data.split("|")
        if len(parts) < 4:
            return

        tr_id = parts[1]
        raw_data = parts[3]

        if tr_id == self.TR_ID:
            self._parse_price_data(raw_data)

    async def run(self):
        """메인 실행 루프"""
        load_dotenv()
        self._running = True

        # 시그널 핸들러
        loop = asyncio.get_event_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, lambda: setattr(self, "_running", False))

        logger.info(f"KRX 야간선물 모니터 시작: {self.futures_code}")
        logger.info(f"WS 서버: {self.WS_URL}")
        logger.info("거래시간: 18:00 ~ 익일 06:00 (KST)")

        self._session = aiohttp.ClientSession()
        try:
            # Approval Key 발급
            self._approval_key = await self._get_approval_key()
            if not self._approval_key:
                logger.error("Approval Key 발급 실패, 종료")
                return

            # WebSocket 연결
            while self._running:
                try:
                    logger.info("WebSocket 연결 중...")
                    self._ws = await self._session.ws_connect(
                        self.WS_URL,
                        heartbeat=30,
                    )
                    logger.info("WebSocket 연결 완료")

                    # 구독
                    await self._subscribe()

                    # 헤더 출력
                    if not self.raw_mode:
                        print()
                        print(
                            f"{'시간':^10}  {'현재가':>8}  {'등락':>12}  {'변동률':>8}  "
                            f"{'체결':>5}  {'누적거래량':>10}  "
                            f"{'매도호가':>8} / {'매수호가':>8}  "
                            f"{'강도':>5}  {'미결제':>8}  "
                            f"{'시가':>8} {'고가':>8} {'저가':>8}"
                        )
                        print("-" * 140)

                    # 메시지 수신 루프
                    async for msg in self._ws:
                        if not self._running:
                            break
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            await self._handle_message(msg.data)
                        elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                            logger.warning(f"WebSocket 종료/오류: {msg.type}")
                            break

                except aiohttp.WSServerHandshakeError as e:
                    logger.error(f"WebSocket 핸드셰이크 실패: {e}")
                except Exception as e:
                    logger.error(f"WebSocket 오류: {e}")

                if self._running:
                    logger.info("5초 후 재연결...")
                    await asyncio.sleep(5)

        finally:
            if self._ws and not self._ws.closed:
                await self._ws.close()
            await self._session.close()
            logger.info(f"종료 (총 {self._msg_count}건 수신)")


def main():
    parser = argparse.ArgumentParser(description="코스피200 KRX 야간선물 실시간 시세")
    parser.add_argument(
        "--code", default="101W9000",
        help="선물 종목코드 (기본: 101W9000, KRX 야간선물 근월물)"
    )
    parser.add_argument("--raw", action="store_true", help="원시 필드 전체 출력 모드")
    args = parser.parse_args()

    monitor = FuturesMonitor(futures_code=args.code, raw_mode=args.raw)
    asyncio.run(monitor.run())


if __name__ == "__main__":
    main()
