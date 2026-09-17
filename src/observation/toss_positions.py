"""Bounded best-effort holdings input, never broker or market-time evidence."""
from __future__ import annotations

import asyncio
import math
import re
import time

from src.data.providers.toss.http_body import BodyLimits, read_json_bounded

POSITIONS_URL = 'http://127.0.0.1:8080/api/positions'
LIMITS = BodyLimits(65536, 8, 4096, 1024, .2)


class InputUnavailable(Exception):
    def __init__(self):
        super().__init__('input_unavailable')


class PositionsClient:
    def __init__(self, *, session_factory=None, clock=time.monotonic):
        self._factory, self._clock = session_factory, clock
        self._session = None
        self._closed = False

    async def fetch(self) -> tuple[str, ...]:
        # Exactly one literal GET; never retry a failed, stale, or empty response.
        import aiohttp
        try:
            if self._closed:
                raise InputUnavailable()
            deadline = self._clock() + 2
            async with asyncio.timeout(2):
                if self._session is None:
                    self._session = (self._factory() if self._factory else
                        aiohttp.ClientSession(trust_env=False, cookie_jar=aiohttp.DummyCookieJar(),
                                              auto_decompress=False))
                    if not hasattr(self._session, '_retry_connection'):
                        raise InputUnavailable()
                    self._session._retry_connection = False
                async with self._session.get(
                    POSITIONS_URL, allow_redirects=False,
                    headers={'Accept-Encoding': 'identity'},
                    timeout=aiohttp.ClientTimeout(total=2, connect=.5, sock_connect=.5),
                ) as response:
                    if response.status != 200:
                        raise InputUnavailable()
                    value = await read_json_bounded(response, limits=LIMITS,
                                                    deadline=deadline, clock=self._clock)
            if type(value) is not list or len(value) > 64:
                raise InputUnavailable()
            codes = set()
            for row in value:
                if type(row) is not dict:
                    raise InputUnavailable()
                code, quantity = row.get('symbol'), row.get('quantity')
                if (type(code) is not str or re.fullmatch(r'[0-9]{6}', code) is None
                        or type(quantity) not in (int, float)
                        or not math.isfinite(quantity) or quantity <= 0):
                    raise InputUnavailable()
                codes.add(code)
            return tuple(sorted(codes))
        except asyncio.CancelledError:
            raise
        except Exception:
            raise InputUnavailable() from None

    async def close(self):
        self._closed = True
        if self._session is not None:
            await self._session.close()
