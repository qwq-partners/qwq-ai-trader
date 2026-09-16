"""토스 그룹별 TPS 게이트 (설계 §4.3)

`kis_rate_limit` 과 **완전히 분리된 독립 모듈**이다. 토스 호출이 KIS 초당 예산을
소모해서는 안 된다 (지금 네이버 크롤링이 저지르고 있는 실수의 반복 금지).

- 그룹별 토큰 버킷. 버킷 용량 = 초당 허용 요청 수(burst capacity), 초당 같은 수만큼 재충전
- 문서상 한도는 사전 공지 없이 바뀔 수 있으므로 응답 헤더 `X-RateLimit-Limit` 을 읽어
  **런타임에 상한을 낮춘다**(올리지는 않는다 — 낙관적 상향은 429 를 부른다)
- 429 는 `Retry-After` 우선, 없으면 지수 백오프 + jitter, 재시도 상한 2회
"""

from __future__ import annotations

import asyncio
import random
import time
from typing import Dict, Mapping, Optional

from loguru import logger

# 실측 그룹별 TPS (설계 §1.1)
GROUP_LIMITS: Dict[str, int] = {
    "AUTH": 5,
    "ACCOUNT": 1,
    "ASSET": 5,
    "STOCK": 5,
    "STOCK_ALL": 1,
    "STOCK_TRADING_TREND": 10,
    "MARKET_INFO": 3,
    "MARKET_DATA": 15,
    "MARKET_DATA_CHART": 20,
    "RANKING": 5,
    "MARKET_INDICATOR": 10,
    "MARKET_INDICATOR_CHART": 5,
}

UNKNOWN_GROUP_LIMIT = 1     # 모르는 그룹은 가장 보수적으로
MAX_RETRIES = 2             # 429 재시도 상한
BACKOFF_BASE_SEC = 0.5
BACKOFF_JITTER_SEC = 0.25
RETRY_AFTER_CAP_SEC = 30.0  # 서버가 비정상적으로 큰 값을 줘도 여기서 자른다


class _Bucket:
    """초당 `rate` 개를 재충전하는 토큰 버킷"""

    def __init__(self, rate: int) -> None:
        self.rate = float(rate)
        self.tokens = float(rate)
        self.updated = time.monotonic()
        self.lock = asyncio.Lock()

    def _refill(self) -> None:
        now = time.monotonic()
        self.tokens = min(self.rate, self.tokens + (now - self.updated) * self.rate)
        self.updated = now

    async def acquire(self) -> None:
        async with self.lock:       # 대기 중인 코루틴을 직렬화 (동시 초과 방지)
            self._refill()
            if self.tokens < 1.0:
                wait = (1.0 - self.tokens) / self.rate
                await asyncio.sleep(wait)
                self._refill()
            self.tokens -= 1.0

    def lower_to(self, rate: int) -> bool:
        """서버가 알려준 한도로 하향 (상향은 하지 않는다)"""
        if rate <= 0 or rate >= self.rate:
            return False
        self._refill()
        self.rate = float(rate)
        self.tokens = min(self.tokens, self.rate)
        return True


_buckets: Dict[str, _Bucket] = {}


def _bucket(group: str) -> _Bucket:
    bucket = _buckets.get(group)
    if bucket is None:
        limit = GROUP_LIMITS.get(group)
        if limit is None:
            logger.warning(f"[토스리미터] 미등록 그룹 '{group}' — {UNKNOWN_GROUP_LIMIT}/s 로 제한")
            limit = UNKNOWN_GROUP_LIMIT
        bucket = _Bucket(limit)
        _buckets[group] = bucket
    return bucket


async def acquire(group: str) -> None:
    """그룹 TPS 한도 안에서 호출 슬롯을 얻는다 (필요하면 대기)"""
    await _bucket(group).acquire()


def note_limit_header(group: str, headers: Optional[Mapping[str, str]]) -> None:
    """응답 헤더 `X-RateLimit-Limit` 으로 런타임 상한 하향"""
    if not headers:
        return
    raw = headers.get("X-RateLimit-Limit") or headers.get("x-ratelimit-limit")
    if raw is None:
        return
    try:
        limit = int(float(str(raw).strip()))
    except (TypeError, ValueError):
        return
    if _bucket(group).lower_to(limit):
        logger.warning(f"[토스리미터] {group} 상한 하향 → {limit}/s (서버 헤더 기준)")


def parse_retry_after(headers: Optional[Mapping[str, str]]) -> Optional[float]:
    """429 응답의 `Retry-After`(초) 파싱 — 없거나 파싱 불가면 None"""
    if not headers:
        return None
    raw = headers.get("Retry-After") or headers.get("retry-after")
    if raw is None:
        return None
    try:
        seconds = float(str(raw).strip())
    except (TypeError, ValueError):
        return None
    if seconds < 0:
        return None
    return min(seconds, RETRY_AFTER_CAP_SEC)


def retry_delay(attempt: int, retry_after: Optional[float] = None) -> float:
    """재시도 대기 초 — `Retry-After` 우선, 없으면 지수 백오프 + jitter

    attempt 는 0-based (첫 재시도 = 0).
    """
    if retry_after is not None:
        return retry_after
    return BACKOFF_BASE_SEC * (2 ** attempt) + random.uniform(0, BACKOFF_JITTER_SEC)


def current_limit(group: str) -> float:
    """현재 적용 중인 초당 상한 (테스트·계측용)"""
    return _bucket(group).rate


def reset() -> None:
    """모듈 전역 버킷 초기화 (테스트 전용)"""
    _buckets.clear()
