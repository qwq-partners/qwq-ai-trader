"""명시된 그룹별 한도와 단일 논리 조회의 시간/페이지/재시도 예산."""

from __future__ import annotations

import asyncio
import math
import random
import time
from dataclasses import dataclass

from .transport import ENDPOINT_GROUPS, TossRequestError


def positive_number(value):
    return type(value) in (int, float) and math.isfinite(value) and value > 0


class RequestBudget:
    def __init__(self, timeout_seconds, *, clock=time.monotonic, max_retries=1, max_pages=4):
        if not positive_number(timeout_seconds):
            raise ValueError("invalid_timeout")
        if type(max_retries) is not int or not 0 <= max_retries <= 1:
            raise ValueError("invalid_retry_limit")
        if type(max_pages) is not int or max_pages < 1:
            raise ValueError("invalid_page_limit")
        self._clock = clock
        self._deadline = clock() + timeout_seconds
        if not math.isfinite(self._deadline):
            raise ValueError("invalid_deadline")
        self._retries = max_retries
        self._pages = max_pages

    @property
    def deadline(self):
        return self._deadline

    def remaining(self):
        remaining = self._deadline - self._clock()
        if not math.isfinite(remaining) or remaining <= 0:
            raise TossRequestError("timeout")
        return remaining

    def consume_retry(self):
        self.remaining()
        if self._retries == 0:
            raise TossRequestError("retry_exhausted")
        self._retries -= 1

    def consume_page(self):
        self.remaining()
        if self._pages == 0:
            raise TossRequestError("page_exhausted")
        self._pages -= 1


@dataclass
class _Bucket:
    limit: float
    tokens: float
    updated: float
    hold_until: float = 0


class GroupRateLimiter:
    """동일 identity 송신자가 모든 조회·페이지에 같은 객체를 주입해야 한다."""

    def __init__(self, *, limits, clock=time.monotonic, sleep=asyncio.sleep):
        if set(limits) != set(ENDPOINT_GROUPS.values()) or any(
                not positive_number(v) or v < 1 for v in limits.values()):
            raise ValueError("invalid_group_limits")
        self._clock, self._sleep = clock, sleep
        self._buckets = {k: _Bucket(float(v), float(v), clock()) for k, v in limits.items()}
        self._lock = asyncio.Lock()

    def _bucket(self, group):
        if group not in self._buckets:
            raise TossRequestError("invalid_request")
        bucket = self._buckets[group]
        now = self._clock()
        if now > bucket.updated:
            bucket.tokens = min(bucket.limit, bucket.tokens + (now - bucket.updated) * bucket.limit)
            bucket.updated = now
        return bucket

    async def acquire(self, group, budget):
        while True:
            async with self._lock:
                bucket = self._bucket(group)
                now = self._clock()
                wait = max(bucket.hold_until - now, (1 - bucket.tokens) / bucket.limit, 0)
                if wait > 0:
                    wait = max(wait, 0.000001)
                if wait >= budget.remaining():
                    raise TossRequestError("timeout")
                if wait == 0:
                    bucket.tokens -= 1
                    return
            await self._sleep(wait)

    async def observe(self, group, headers, *, status):
        values = {k.lower(): v for k, v in headers.items() if isinstance(k, str)}
        async with self._lock:
            bucket = self._bucket(group)
            now = self._clock()
            limit = _header_number(values.get("x-ratelimit-limit"))
            if limit is not None and limit >= 1:
                bucket.limit = min(bucket.limit, limit)
                bucket.tokens = min(bucket.tokens, bucket.limit)
            remaining = _header_number(values.get("x-ratelimit-remaining"))
            if remaining is not None:
                bucket.tokens = min(bucket.tokens, remaining)
            reset = _header_number(values.get("x-ratelimit-reset"))
            if status == 429:
                wait = _header_number(values.get("retry-after"))
                if wait is None:
                    wait = reset if reset is not None else (1 + random.random()) / bucket.limit
                bucket.tokens = 0
                bucket.hold_until = max(bucket.hold_until, now + wait)
                bucket.updated = max(bucket.updated, bucket.hold_until - 1 / bucket.limit)
            elif remaining == 0 and reset is not None:
                bucket.hold_until = max(bucket.hold_until, now + reset)
                # Reset은 버킷 전체가 아니라 다음 한 토큰이 생길 때까지의 초다.
                bucket.updated = max(bucket.updated, bucket.hold_until - 1 / bucket.limit)


def _header_number(value):
    if not isinstance(value, (str, int, float)) or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (ValueError, OverflowError):
        return None
    return number if math.isfinite(number) and number >= 0 else None
