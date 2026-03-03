"""
AI Trader US - KIS 실시간체결통보 WebSocket (H0GSCNI0)

주문 체결 즉시 Push 수신 → _order_check_loop REST 폴링 보완.
연결 성공 시 REST 폴링 주기를 늘려 API 부하 감소.

접속: ws://ops.koreainvestment.com:21000  (실전)
      ws://ops.koreainvestment.com:31000  (모의)

WS Push 포맷: tr_id|tr_key|data_cnt|f1^f2^...^f25
체결 여부: 필드[12] CNTG_YN == "2" (체결통보) / "1" (주문통보)
"""

from __future__ import annotations

import asyncio
import json
from typing import Callable, Coroutine, Optional

import aiohttp
from loguru import logger

# H0GSCNI0 응답 필드 순서 (index → 필드명)
_FIELDS = [
    "CUST_ID",       # 0  고객 ID
    "ACNT_NO",       # 1  계좌번호
    "ODER_NO",       # 2  주문번호
    "OODER_NO",      # 3  원주문번호
    "SELN_BYOV_CLS", # 4  매도/매수 구분 (01=매도, 02=매수)
    "RCTF_CLS",      # 5  정정구분 (0=정상, 1=정정, 2=취소)
    "ODER_KIND2",    # 6  주문종류2
    "STCK_SHRN_ISCD",# 7  종목코드
    "CNTG_QTY",      # 8  체결수량 (주문통보 시 주문수량)
    "CNTG_UNPR",     # 9  체결단가 (주문통보 시 주문단가)
    "STCK_CNTG_HOUR",# 10 체결시간
    "RFUS_YN",       # 11 거부여부 (0=정상, 1=거부)
    "CNTG_YN",       # 12 체결여부 (1=주문/정정/취소/거부, 2=체결)
    "ACPT_YN",       # 13 접수여부
    "BRNC_NO",       # 14 지점번호
    "ODER_QTY",      # 15 주문수량
    "ACNT_NAME",     # 16 계좌명
    "CNTG_ISNM",     # 17 체결종목명
    "ODER_COND",     # 18 해외종목구분 (6=NASDAQ, 7=NYSE, 8=AMEX)
    "DEBT_GB",       # 19 담보유형코드
    "DEBT_DATE",     # 20 담보대출일자
    "START_TM",      # 21 분할매수/매도 시작시간
    "END_TM",        # 22 분할매수/매도 종료시간
    "TM_DIV_TP",     # 23 시간분할타입
    "CNTG_UNPR12",   # 24 체결단가12
]

_EXCD_MAP = {"6": "NASD", "7": "NYSE", "8": "AMEX", "C": "HKS", "9": "OTCB"}


class KISNotificationWS:
    """
    KIS 실시간체결통보 WebSocket (H0GSCNI0 / H0GSCNI9)

    체결 시 on_fill 콜백 호출:
        async def on_fill(order_no, symbol, side, qty, price, exchange)
    """

    _BACKOFF_BASE = 5
    _BACKOFF_MAX  = 120

    def __init__(
        self,
        app_key: str,
        app_secret: str,
        hts_id: str,
        is_mock: bool = False,
    ):
        self._app_key    = app_key
        self._app_secret = app_secret
        self._hts_id     = hts_id  # KIS HTS 로그인 ID (tr_key)
        self._is_mock    = is_mock

        self._ws_url = (
            "ws://ops.koreainvestment.com:31000"
            if is_mock else
            "ws://ops.koreainvestment.com:21000"
        )
        self._tr_id = "H0GSCNI9" if is_mock else "H0GSCNI0"

        self._approval_key: Optional[str] = None
        self._session: Optional[aiohttp.ClientSession] = None
        self._ws: Optional[aiohttp.ClientWebSocketResponse] = None

        self._running   = False
        self._connected = False

        # 콜백: async def fn(order_no, symbol, side, qty, price, exchange)
        self._fill_callback: Optional[Callable[..., Coroutine]] = None

    # ──────────────────────────────────────────────
    # Public API
    # ──────────────────────────────────────────────

    @property
    def is_connected(self) -> bool:
        return self._connected and self._ws is not None and not self._ws.closed

    def on_fill(self, callback: Callable[..., Coroutine]):
        """체결 콜백 등록"""
        self._fill_callback = callback

    async def start(self):
        """WS 연결 루프 시작 (재연결 포함)"""
        self._running = True
        backoff = self._BACKOFF_BASE

        while self._running:
            try:
                # Approval Key 갱신 (매 연결 시)
                self._approval_key = await self._get_approval_key()
                if not self._approval_key:
                    logger.error("[KIS WS] Approval Key 발급 실패 — 재시도")
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, self._BACKOFF_MAX)
                    continue

                await self._connect_and_listen()
                backoff = self._BACKOFF_BASE  # 정상 종료 시 리셋

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"[KIS WS] 연결 끊김: {e} — {backoff}초 후 재연결")

            if not self._running:
                break

            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, self._BACKOFF_MAX)

        await self._cleanup()

    async def stop(self):
        """graceful 종료"""
        self._running = False
        await self._cleanup()

    # ──────────────────────────────────────────────
    # Internal
    # ──────────────────────────────────────────────

    async def _get_approval_key(self) -> Optional[str]:
        """WebSocket 전용 접속키 발급 (/oauth2/Approval)"""
        base = (
            "https://openapivts.koreainvestment.com:29443"
            if self._is_mock else
            "https://openapi.koreainvestment.com:9443"
        )
        url = f"{base}/oauth2/Approval"
        body = {
            "grant_type": "client_credentials",
            "appkey": self._app_key,
            "secretkey": self._app_secret,
        }
        try:
            async with aiohttp.ClientSession() as sess:
                async with sess.post(url, json=body, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status != 200:
                        logger.error(f"[KIS WS] Approval Key HTTP {resp.status}")
                        return None
                    data = await resp.json()
                    key = data.get("approval_key", "")
                    if key:
                        logger.info("[KIS WS] Approval Key 발급 성공")
                    return key or None
        except Exception as e:
            logger.error(f"[KIS WS] Approval Key 발급 오류: {e}")
            return None

    async def _connect_and_listen(self):
        """WS 연결 → 구독 → 수신 루프"""
        self._session = aiohttp.ClientSession()
        try:
            self._ws = await self._session.ws_connect(
                self._ws_url,
                heartbeat=30,
                timeout=aiohttp.ClientTimeout(total=15),
            )
            self._connected = True
            logger.info(f"[KIS WS] 연결 성공 ({self._tr_id}) hts_id={self._hts_id}")

            # 체결통보 구독
            await self._subscribe()

            # 수신 루프
            async for msg in self._ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    await self._handle_message(msg.data)
                elif msg.type == aiohttp.WSMsgType.BINARY:
                    # PINGPONG 처리 (일부 환경)
                    pass
                elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                    logger.warning(f"[KIS WS] WS 종료: {msg.type}")
                    break

        finally:
            self._connected = False
            if self._ws and not self._ws.closed:
                await self._ws.close()
            self._ws = None
            if self._session and not self._session.closed:
                await self._session.close()
            self._session = None

    async def _subscribe(self):
        """실시간체결통보 구독 요청"""
        payload = {
            "header": {
                "approval_key": self._approval_key,
                "custtype": "P",
                "tr_type": "1",       # 1=등록
                "content-type": "utf-8",
            },
            "body": {
                "input": {
                    "tr_id": self._tr_id,
                    "tr_key": self._hts_id,
                }
            },
        }
        await self._ws.send_str(json.dumps(payload))
        logger.debug(f"[KIS WS] 체결통보 구독 전송 (tr_key={self._hts_id})")

    async def _handle_message(self, raw: str):
        """Push 메시지 파싱 → 체결 콜백"""
        # JSON 응답 (구독 확인 / PINGPONG)
        if raw.startswith("{"):
            try:
                obj = json.loads(raw)
                tr_id = obj.get("header", {}).get("tr_id", "")
                rt_cd = obj.get("body", {}).get("rt_cd", "")
                msg   = obj.get("body", {}).get("msg1", "")
                if rt_cd == "0":
                    logger.info(f"[KIS WS] 구독 확인: {tr_id} — {msg}")
                else:
                    logger.warning(f"[KIS WS] 응답 오류: {tr_id} rt_cd={rt_cd} {msg}")
                    # HTS ID 오류 → 연결 중단 (재연결 루프로 복귀하지 않도록 _running=False)
                    if "htsid" in msg.lower() or rt_cd == "9":
                        logger.error(
                            "[KIS WS] HTS ID 오류 — KIS_HTS_ID 환경변수를 KIS 포털 로그인 ID로 설정하세요. "
                            "WS 비활성화합니다."
                        )
                        self._running = False
            except Exception:
                pass
            return

        # Push 데이터: tr_id|tr_key|data_cnt|data
        parts = raw.split("|")
        if len(parts) < 4:
            return

        tr_id    = parts[0].strip()
        data_str = parts[3]
        fields   = data_str.split("^")

        if len(fields) < 13:
            return

        # dict 변환 (필드명 → 값)
        d = {_FIELDS[i]: fields[i] for i in range(min(len(_FIELDS), len(fields)))}

        rfus_yn = d.get("RFUS_YN", "1")
        cntg_yn = d.get("CNTG_YN", "1")

        # 체결통보만 처리 (CNTG_YN == "2", 거부 아님)
        if cntg_yn != "2" or rfus_yn != "0":
            return

        order_no = d.get("ODER_NO", "").strip()
        symbol   = d.get("STCK_SHRN_ISCD", "").strip()
        side_cls = d.get("SELN_BYOV_CLS", "")
        side     = "sell" if side_cls == "01" else "buy"
        qty      = int(d.get("CNTG_QTY", "0") or "0")
        price    = float(d.get("CNTG_UNPR", "0") or "0")
        excd_raw = d.get("ODER_COND", "")
        exchange = _EXCD_MAP.get(excd_raw, "NASD")
        name     = d.get("CNTG_ISNM", "").strip()

        if not symbol or not order_no or price <= 0:
            return

        logger.info(
            f"[KIS WS] 체결통보 수신 ← {side.upper()} {symbol}({name}) "
            f"{qty}주 @ ${price:.2f} (주문번호={order_no})"
        )

        if self._fill_callback:
            try:
                await self._fill_callback(
                    order_no=order_no,
                    symbol=symbol,
                    side=side,
                    qty=qty,
                    price=price,
                    exchange=exchange,
                )
            except Exception as e:
                logger.error(f"[KIS WS] 체결 콜백 오류: {e}")

    async def _cleanup(self):
        self._connected = False
        if self._ws and not self._ws.closed:
            await self._ws.close()
        self._ws = None
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None
