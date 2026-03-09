"""
AI Trader US - KIS 해외주식 실시간체결 WebSocket (HDFSCNT0)

보유 US 종목의 실시간 체결가를 수신 → ExitManager에 즉시 전달.
Finnhub WS 무료 플랜(15분 지연)을 대체하여 실제 exit 결정에 활용합니다.

접속: ws://ops.koreainvestment.com:21000  (실전)
      ws://ops.koreainvestment.com:31000  (모의)

TR ID: HDFSCNT0 (해외주식 실시간체결)
tr_key: {exchange}{symbol}  →  예: NASDAAPL, NYSEMSFT, AMEXSPY

HDFSCNT0 응답 필드 (^-구분, 0-indexed):
  0: EXCD   거래소코드
  1: SYMB   종목코드
  2: KYMD   현지일자
  3: KHMS   현지시각
  4: OPEN   시가
  5: HIGH   고가
  6: LOW    저가
  7: LAST   현재가  ← 핵심
  8: SIGN   전일대비부호
  9: DIFF   전일대비
 10: RATE   등락률
 11: PBID   매수호가
 12: VBID   매수호가잔량
 13: PASK   매도호가
 14: VASK   매도호가잔량
 15: EVOL   체결량 (이번 틱)
 16: TVOL   누적거래량
 17: TAMT   누적거래대금
"""

from __future__ import annotations

import asyncio
import json
from typing import Callable, Coroutine, Dict, Optional

import aiohttp
from loguru import logger

# 거래소 코드 정규화 → tr_key 접두사
_EXCD_PREFIX: Dict[str, str] = {
    "NASD": "NASD",
    "NAS":  "NASD",
    "NYSE": "NYSE",
    "NYS":  "NYSE",
    "AMEX": "AMEX",
    "AMS":  "AMEX",
}

# HDFSCNT0 필드 인덱스
_F_SYMB = 1
_F_LAST = 7
_F_EVOL = 15


class KISUSPriceFeed:
    """
    KIS 해외주식 실시간체결 WebSocket (HDFSCNT0)

    사용법:
        feed = KISUSPriceFeed(app_key, app_secret, is_mock=False)
        feed.on_price(callback)          # async def callback(symbol, price, volume)
        asyncio.create_task(feed.start())
        await feed.subscribe(["AAPL", "NVDA"], exchange="NASD")
        await feed.unsubscribe(["AAPL"])
        await feed.stop()
    """

    _TR_ID   = "HDFSCNT0"
    _WS_PROD = "ws://ops.koreainvestment.com:21000"
    _WS_MOCK = "ws://ops.koreainvestment.com:31000"
    _BASE_PROD = "https://openapi.koreainvestment.com:9443"
    _BASE_MOCK = "https://openapivts.koreainvestment.com:29443"

    _BACKOFF_BASE = 5
    _BACKOFF_MAX  = 120
    MAX_SUBSCRIPTIONS = 30  # 해외주식 WS 구독 안전 한도

    def __init__(self, app_key: str, app_secret: str, is_mock: bool = False):
        self._app_key    = app_key
        self._app_secret = app_secret
        self._is_mock    = is_mock

        self._ws_url   = self._WS_MOCK  if is_mock else self._WS_PROD
        self._base_url = self._BASE_MOCK if is_mock else self._BASE_PROD

        self._approval_key: Optional[str] = None
        self._session: Optional[aiohttp.ClientSession] = None
        self._ws: Optional[aiohttp.ClientWebSocketResponse] = None

        self._running   = False
        self._connected = False

        # 구독 상태: symbol → tr_key (예: "AAPL" → "NASDAAPL")
        self._subscribed:   Dict[str, str] = {}   # 현재 구독 중
        self._pending_sub:  Dict[str, str] = {}   # 연결 전 대기 큐
        self._exchange_map: Dict[str, str] = {}   # symbol → exchange prefix

        # 콜백: async def fn(symbol, price, volume)
        self._price_callback: Optional[Callable[..., Coroutine]] = None

    # ──────────────────────────────────────────────
    # Public API
    # ──────────────────────────────────────────────

    @property
    def is_connected(self) -> bool:
        return self._connected and self._ws is not None and not self._ws.closed

    @property
    def subscribed_count(self) -> int:
        return len(self._subscribed)

    def on_price(self, callback: Callable[..., Coroutine]):
        """실시간 가격 콜백 등록: async def callback(symbol, price, volume)"""
        self._price_callback = callback

    async def subscribe(self, symbols: list, exchange: str = "NASD"):
        """종목 구독 추가 (중복 제거, 한도 체크)"""
        excd = _EXCD_PREFIX.get(exchange.upper(), "NASD")
        for sym in symbols:
            if sym in self._subscribed or sym in self._pending_sub:
                continue
            total = len(self._subscribed) + len(self._pending_sub)
            if total >= self.MAX_SUBSCRIPTIONS:
                logger.warning(f"[KIS US WS] 구독 한도({self.MAX_SUBSCRIPTIONS}) 초과 — {sym} 스킵")
                continue
            tr_key = f"{excd}{sym}"
            self._exchange_map[sym] = excd
            if self.is_connected:
                await self._send_sub(tr_key, subscribe=True)
                self._subscribed[sym] = tr_key
                logger.info(f"[KIS US WS] 구독 → {sym} ({tr_key})")
            else:
                self._pending_sub[sym] = tr_key

    async def unsubscribe(self, symbols: list):
        """종목 구독 해제"""
        for sym in symbols:
            tr_key = (
                self._subscribed.pop(sym, None) or
                self._pending_sub.pop(sym, None)
            )
            if tr_key and self.is_connected:
                await self._send_sub(tr_key, subscribe=False)
                logger.info(f"[KIS US WS] 구독 해제 → {sym}")
            self._exchange_map.pop(sym, None)

    async def start(self):
        """WS 연결 루프 (자동 재연결 포함, approval_key 무효화 감지)"""
        self._running = True
        backoff = self._BACKOFF_BASE
        _instant_disconnect_count = 0
        _msg_count_before = 0

        while self._running:
            try:
                self._approval_key = await self._get_approval_key()
                if not self._approval_key:
                    logger.error("[KIS US WS] Approval Key 발급 실패 — 재시도")
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, self._BACKOFF_MAX)
                    continue

                _msg_count_before = getattr(self, '_msg_count', 0)
                await self._connect_and_listen()

                # 메시지 0개 수신 후 즉시 끊김 → approval_key 서버 측 무효화 감지
                if getattr(self, '_msg_count', 0) == _msg_count_before:
                    _instant_disconnect_count += 1
                    if _instant_disconnect_count >= 3:
                        logger.warning(
                            f"[KIS US WS] {_instant_disconnect_count}회 연속 즉시 끊김 "
                            f"→ approval_key 강제 재발급"
                        )
                        self._approval_key = None
                        _instant_disconnect_count = 0
                else:
                    _instant_disconnect_count = 0
                    backoff = self._BACKOFF_BASE  # 메시지 수신 성공 → 정상이었으므로 리셋

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"[KIS US WS] 연결 끊김: {e} — {backoff}초 후 재연결")
                _instant_disconnect_count = 0

            if not self._running:
                break
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, self._BACKOFF_MAX)

        await self._cleanup()

    async def stop(self):
        self._running = False
        await self._cleanup()

    # ──────────────────────────────────────────────
    # Internal
    # ──────────────────────────────────────────────

    async def _get_approval_key(self) -> Optional[str]:
        url = f"{self._base_url}/oauth2/Approval"
        body = {
            "grant_type": "client_credentials",
            "appkey": self._app_key,
            "secretkey": self._app_secret,
        }
        try:
            async with aiohttp.ClientSession() as sess:
                async with sess.post(
                    url, json=body,
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status != 200:
                        logger.error(f"[KIS US WS] Approval HTTP {resp.status}")
                        return None
                    data = await resp.json()
                    key  = data.get("approval_key", "")
                    if key:
                        logger.info("[KIS US WS] Approval Key 발급 성공")
                    return key or None
        except Exception as e:
            logger.error(f"[KIS US WS] Approval Key 오류: {e}")
            return None

    async def _connect_and_listen(self):
        self._session = aiohttp.ClientSession()
        try:
            self._ws = await self._session.ws_connect(
                self._ws_url,
                heartbeat=30,
                timeout=aiohttp.ClientTimeout(total=15),
            )
            self._connected = True
            logger.info(f"[KIS US WS] 연결 성공 ({self._TR_ID})")

            # 대기 중이던 구독 먼저 처리
            for sym, tr_key in list(self._pending_sub.items()):
                await self._send_sub(tr_key, subscribe=True)
                self._subscribed[sym] = tr_key
                logger.info(f"[KIS US WS] 구독 (pending) → {sym} ({tr_key})")
            self._pending_sub.clear()

            # 재연결 시 기존 구독 재신청
            for sym, tr_key in list(self._subscribed.items()):
                await self._send_sub(tr_key, subscribe=True)

            # 수신 루프
            async for msg in self._ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    await self._handle_message(msg.data)
                elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                    logger.warning(f"[KIS US WS] WS 종료: {msg.type}")
                    break

        finally:
            self._connected = False
            if self._ws and not self._ws.closed:
                await self._ws.close()
            self._ws = None
            if self._session and not self._session.closed:
                await self._session.close()
            self._session = None

    async def _send_sub(self, tr_key: str, subscribe: bool):
        if not self._ws or self._ws.closed:
            return
        payload = {
            "header": {
                "approval_key":  self._approval_key,
                "custtype":      "P",
                "tr_type":       "1" if subscribe else "2",
                "content-type":  "utf-8",
            },
            "body": {
                "input": {
                    "tr_id":  self._TR_ID,
                    "tr_key": tr_key,
                }
            },
        }
        try:
            await self._ws.send_str(json.dumps(payload))
        except Exception as e:
            logger.warning(f"[KIS US WS] 전송 오류 ({tr_key}): {e}")

    async def _handle_message(self, raw: str):
        # JSON → 구독 확인 / 오류 응답
        if raw.startswith("{"):
            try:
                obj   = json.loads(raw)
                tr_id = obj.get("header", {}).get("tr_id", "")
                # PINGPONG(서버 heartbeat) → PONG 응답 (응답 안 하면 KIS가 연결 끊음)
                if tr_id == "PINGPONG":
                    try:
                        pong = json.dumps({"header": {"tr_id": "PINGPONG"}, "body": {}})
                        await self._ws.send_str(pong)
                    except Exception:
                        pass
                    return
                rt_cd = obj.get("body", {}).get("rt_cd", "")
                msg1  = obj.get("body", {}).get("msg1", "")
                if rt_cd == "0":
                    logger.info(f"[KIS US WS] 구독 확인: {tr_id} — {msg1}")
                elif rt_cd:
                    logger.warning(f"[KIS US WS] 오류: {tr_id} rt_cd={rt_cd} {msg1}")
                # rt_cd 없는 기타 JSON 메시지 → 무시
            except Exception:
                pass
            return

        # Push 데이터: tr_id|tr_key|data_cnt|fields^...
        parts = raw.split("|")
        if len(parts) < 4:
            return

        fields = parts[3].split("^")
        if len(fields) <= _F_LAST:
            return

        try:
            symbol    = fields[_F_SYMB].strip()
            price_str = fields[_F_LAST].strip()
            if not price_str or price_str in ("0", "0.0", "0.00"):
                return
            price  = float(price_str)
            if price <= 0:
                return
            vol_str = fields[_F_EVOL].strip() if len(fields) > _F_EVOL else "0"
            volume  = int(vol_str) if vol_str.lstrip("-").isdigit() else 0
        except (ValueError, IndexError):
            return

        self._msg_count = getattr(self, '_msg_count', 0) + 1

        if self._price_callback:
            try:
                await self._price_callback(symbol=symbol, price=price, volume=volume)
            except Exception as e:
                logger.error(f"[KIS US WS] 가격 콜백 오류 ({symbol}): {e}")

    async def _cleanup(self):
        self._connected = False
        if self._ws and not self._ws.closed:
            await self._ws.close()
        self._ws = None
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None
