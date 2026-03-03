"""
AI Trader US - Finnhub WebSocket 실시간 시세 피드

보유 종목의 실시간 체결가를 수신하여 ExitManager에 전달.
지수 백오프 재연결 (5s → 120s), aiohttp ws_connect 사용.

접속: wss://ws.finnhub.io?token={API_KEY}
구독: {"type":"subscribe","symbol":"AAPL"}
응답: {"type":"trade","data":[{"s":"AAPL","p":185.23,"t":ms,"v":100}]}
"""

from __future__ import annotations

import asyncio
from typing import Callable, Coroutine, Optional, Set

import aiohttp
from loguru import logger


class FinnhubWSFeed:
    """Finnhub WebSocket 실시간 시세 피드"""

    # 재연결 백오프 (초)
    _BACKOFF_BASE = 5
    _BACKOFF_MAX = 120

    def __init__(self, api_key: str):
        self._api_key = api_key
        self._url = f"wss://ws.finnhub.io?token={api_key}"
        self._session: Optional[aiohttp.ClientSession] = None
        self._ws: Optional[aiohttp.ClientWebSocketResponse] = None
        self._subscribed: Set[str] = set()
        self._callback: Optional[Callable[..., Coroutine]] = None
        self._running = False
        self._connected = False
        self._task: Optional[asyncio.Task] = None

    @property
    def is_connected(self) -> bool:
        return self._connected and self._ws is not None and not self._ws.closed

    def on_trade(self, callback: Callable[..., Coroutine]):
        """체결 콜백 등록: async def callback(symbol, price, timestamp)"""
        self._callback = callback

    async def start(self):
        """WS 연결 + 수신 루프 (재연결 포함)"""
        self._running = True
        backoff = self._BACKOFF_BASE

        while self._running:
            try:
                await self._connect_and_listen()
                # 정상 종료 시 백오프 리셋
                backoff = self._BACKOFF_BASE
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"[Finnhub WS] 연결 끊김: {e} — {backoff}초 후 재연결")

            if not self._running:
                break

            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, self._BACKOFF_MAX)

        await self._cleanup()

    async def stop(self):
        """graceful 종료"""
        self._running = False
        await self._cleanup()

    async def subscribe(self, symbols: list[str]):
        """종목 구독 추가"""
        for symbol in symbols:
            if symbol not in self._subscribed:
                self._subscribed.add(symbol)
                if self.is_connected:
                    await self._ws.send_json({"type": "subscribe", "symbol": symbol})
                    logger.debug(f"[Finnhub WS] 구독 추가: {symbol}")

    async def unsubscribe(self, symbols: list[str]):
        """종목 구독 해제"""
        for symbol in symbols:
            if symbol in self._subscribed:
                self._subscribed.discard(symbol)
                if self.is_connected:
                    await self._ws.send_json({"type": "unsubscribe", "symbol": symbol})
                    logger.debug(f"[Finnhub WS] 구독 해제: {symbol}")

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _connect_and_listen(self):
        """WS 연결 → 구독 복구 → 수신 루프"""
        self._session = aiohttp.ClientSession()
        try:
            self._ws = await self._session.ws_connect(
                self._url,
                heartbeat=30,
                timeout=aiohttp.ClientTimeout(total=15),
            )
            self._connected = True
            logger.info(f"[Finnhub WS] 연결 성공 (구독 {len(self._subscribed)}개)")

            # 기존 구독 복구
            for symbol in self._subscribed:
                await self._ws.send_json({"type": "subscribe", "symbol": symbol})

            # 수신 루프
            async for msg in self._ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    await self._handle_message(msg.json())
                elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                    break

        finally:
            self._connected = False
            # WS/세션만 정리 (start/stop에서 _cleanup 호출)
            if self._ws and not self._ws.closed:
                await self._ws.close()
            self._ws = None
            if self._session and not self._session.closed:
                await self._session.close()
            self._session = None

    async def _handle_message(self, data: dict):
        """메시지 파싱 → 콜백 호출"""
        if data.get("type") != "trade" or not self._callback:
            return

        trades = data.get("data", [])
        if not trades:
            return

        # 종목당 마지막 체결가만 사용 (중복 콜백 방지)
        latest: dict[str, dict] = {}
        for t in trades:
            symbol = t.get("s")
            if symbol:
                latest[symbol] = t

        for symbol, t in latest.items():
            price = t.get("p", 0)
            ts = t.get("t", 0)
            if price > 0:
                try:
                    await self._callback(symbol, price, ts)
                except Exception as e:
                    logger.error(f"[Finnhub WS] 콜백 오류 {symbol}: {e}")

    async def _cleanup(self):
        """리소스 정리"""
        self._connected = False
        if self._ws and not self._ws.closed:
            await self._ws.close()
        self._ws = None
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None
