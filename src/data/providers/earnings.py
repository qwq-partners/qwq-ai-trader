"""
AI Trader US - Earnings Calendar Provider

Finnhub API로 어닝 발표 예정일 조회.
EarningsDrift 전략의 오탐 방지용 — 실제 어닝 발표 종목에만 전략 적용.

Finnhub 무료 tier에서 지원하는 엔드포인트:
  GET /calendar/earnings?from=YYYY-MM-DD&to=YYYY-MM-DD
"""

import asyncio
import json
import os
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, Optional, Set

import aiohttp
from loguru import logger


CACHE_DIR = Path.home() / ".cache" / "ai_trader_us"
CACHE_DIR.mkdir(parents=True, exist_ok=True)


class EarningsProvider:
    """
    Finnhub 어닝 캘린더 + 로컬 1일 캐시.

    사용 패턴:
        provider = EarningsProvider(api_key)
        earnings_symbols = await provider.get_today_earnings()
        # → {"AAPL", "MSFT", ...} 오늘/어제 발표 종목
    """

    BASE_URL = "https://finnhub.io/api/v1"

    def __init__(self, api_key: str = ""):
        self._api_key = api_key or os.getenv("FINNHUB_API_KEY", "")
        self._session: Optional[aiohttp.ClientSession] = None
        self._timeout = aiohttp.ClientTimeout(total=10)

    async def _get_session(self) -> aiohttp.ClientSession:
        if not self._session or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=self._timeout)
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    # ── 캐시 헬퍼 ────────────────────────────────────────────────────────────

    def _cache_path(self, d: date) -> Path:
        return CACHE_DIR / f"earnings_{d.isoformat()}.json"

    def _load_cache(self, d: date) -> Optional[Set[str]]:
        path = self._cache_path(d)
        if path.exists():
            try:
                data = json.loads(path.read_text())
                return set(data.get("symbols", []))
            except Exception:
                pass
        return None

    def _save_cache(self, d: date, symbols: Set[str]):
        try:
            self._cache_path(d).write_text(
                json.dumps({"symbols": list(symbols), "date": d.isoformat()})
            )
        except Exception as e:
            logger.debug(f"[Earnings] 캐시 저장 실패: {e}")

    # ── API 조회 ──────────────────────────────────────────────────────────────

    async def get_earnings_calendar(self, start: date, end: date) -> Dict[date, Set[str]]:
        """
        기간 내 어닝 발표 예정 종목 조회.

        Returns:
            {date: {symbol, ...}}
        """
        if not self._api_key:
            return {}

        session = await self._get_session()
        url = f"{self.BASE_URL}/calendar/earnings"
        params = {
            "from": start.isoformat(),
            "to": end.isoformat(),
            "token": self._api_key,
        }

        try:
            async with session.get(url, params=params) as resp:
                if resp.status != 200:
                    logger.warning(f"[Earnings] HTTP {resp.status}")
                    return {}
                data = await resp.json()
        except Exception as e:
            logger.error(f"[Earnings] 캘린더 조회 실패: {e}")
            return {}

        result: Dict[date, Set[str]] = {}
        for item in data.get("earningsCalendar", []):
            symbol = (item.get("symbol") or "").strip()
            date_str = (item.get("date") or "").strip()
            if not symbol or not date_str:
                continue
            try:
                d = date.fromisoformat(date_str)
            except ValueError:
                continue
            result.setdefault(d, set()).add(symbol)

        logger.info(
            f"[Earnings] {start} ~ {end}: "
            f"{sum(len(v) for v in result.values())}건 "
            f"({len(result)}일)"
        )
        return result

    async def get_today_earnings(self, today: date = None) -> Set[str]:
        """
        오늘 + 어제 어닝 발표 종목 반환.

        PEAD 전략은 발표 다음날 갭업을 잡으므로:
        - 어제 장후 발표 → 오늘 갭업 (어제 포함)
        - 오늘 장전 발표 → 오늘 갭업 (오늘 포함)

        캐시: 1일 (하루 1회 API 호출)
        """
        if today is None:
            from zoneinfo import ZoneInfo
            today = datetime.now(ZoneInfo("America/New_York")).date()

        # 캐시 확인 (당일 캐시 있으면 바로 반환)
        cached = self._load_cache(today)
        if cached is not None:
            return cached

        if not self._api_key:
            logger.debug("[Earnings] API 키 없음 - 어닝 캘린더 비활성화")
            return set()

        yesterday = today - timedelta(days=1)
        tomorrow = today + timedelta(days=1)

        dates_map = await self.get_earnings_calendar(yesterday, tomorrow)

        # 오늘 + 어제 발표 종목 합산
        symbols: Set[str] = set()
        for d in (today, yesterday):
            symbols |= dates_map.get(d, set())

        self._save_cache(today, symbols)
        logger.info(
            f"[Earnings] 오늘 어닝 대상: {len(symbols)}개 종목 "
            f"(어제 {len(dates_map.get(yesterday, set()))}개 + "
            f"오늘 {len(dates_map.get(today, set()))}개)"
        )
        return symbols
