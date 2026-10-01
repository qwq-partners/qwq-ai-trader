"""명시 WS 승인 아래 고정 토스 origin에 한 번만 접속하는 전송 경계."""
from __future__ import annotations

import asyncio
import re

import aiohttp

from .approval import ApprovedAuthority, WS_ENDPOINT


class WebSocketUnavailable(Exception):
    def __init__(self):
        super().__init__('websocket_unavailable')


async def reject_ws_redirect(request, handler):
    # ws_connect에는 allow_redirects 인자가 없다. 응답을 redirect 처리 전에 차단한다.
    if str(request.url) != WS_ENDPOINT or request.method != 'GET':
        raise WebSocketUnavailable()
    response = await handler(request)
    if 300 <= response.status < 400:
        response.close()
        raise WebSocketUnavailable()
    return response


class _AuthorizedSocket:
    def __init__(self, socket, owner, deadline):
        self.socket, self.owner, self.deadline = socket, owner, deadline

    async def send_str(self, text):
        remaining = self.owner._remaining(self.deadline)
        try:
            async with asyncio.timeout(remaining):
                await self.socket.send_str(text)
            self.owner._remaining(self.deadline)
        except asyncio.CancelledError:
            raise
        except Exception:
            raise WebSocketUnavailable() from None

    async def receive_str(self):
        remaining = self.owner._remaining(self.deadline)
        try:
            async with asyncio.timeout(min(1, remaining)):
                result = await self.socket.receive_str()
            self.owner._remaining(self.deadline)
            return result
        except (asyncio.CancelledError, TimeoutError):
            raise
        except Exception:
            raise WebSocketUnavailable() from None

    async def close(self):
        await self.socket.close()


class TossWebSocketTransport:
    def __init__(self, authority, *, session_factory=None):
        if type(authority) is not ApprovedAuthority:
            raise WebSocketUnavailable()
        self.authority, self._factory = authority, session_factory
        self._session = self._socket = None
        self._claimed = self._closed = False

    def _remaining(self, deadline):
        if self._closed:
            raise WebSocketUnavailable()
        bound = self.authority.require('websocket', deadline=deadline)
        return bound - self.authority.clock()

    async def connect(self, access_token, *, deadline):
        if (self._claimed or type(access_token) is not str or len(access_token) > 8192
                or re.fullmatch(r'[\x21-\x7e]+', access_token) is None):
            raise WebSocketUnavailable()
        self._remaining(deadline)
        self._claimed = True
        try:
            options = dict(trust_env=False, cookie_jar=aiohttp.DummyCookieJar(),
                           auto_decompress=False, middlewares=(reject_ws_redirect,))
            self._session = self._factory(**options) if self._factory else aiohttp.ClientSession(**options)
            if not hasattr(self._session, '_retry_connection'):
                raise WebSocketUnavailable()
            self._session._retry_connection = False
            async with asyncio.timeout(min(5, self._remaining(deadline))):
                self._socket = await self._session.ws_connect(WS_ENDPOINT,
                    headers={'Authorization': 'Bearer ' + access_token}, ssl=True, compress=0,
                    max_msg_size=65536, timeout=aiohttp.ClientWSTimeout(ws_close=2), heartbeat=None)
            self._remaining(deadline)
            return _AuthorizedSocket(self._socket, self, deadline)
        except asyncio.CancelledError:
            raise
        except Exception:
            raise WebSocketUnavailable() from None

    async def close(self):
        self._closed = True
        if self._socket is not None:
            await self._socket.close()
        if self._session is not None:
            await self._session.close()
