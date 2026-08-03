"""
DART 총자산 증가율 조회 — Asset Growth Effect 퀄리티 필터 (2026-08-03)

논문 재현 결과(awesome-systematic-trading, Sharpe 0.835, 연간 리밸런싱):
총자산 증가율이 **낮은** 기업이 높은 기업을 장기 아웃퍼폼한다.
무리한 자산 확장(증자·차입·인수)이 향후 수익률 저하로 이어지는 퀄리티 팩터.

코어홀딩 스크리너에서 감점 전용으로 사용한다:
  - 연 1회 갱신되는 데이터라 30일 디스크 캐시 (DART API 부하 최소화)
  - 사업보고서(11011) 기준, 연결(CFS) 우선 → 별도(OFS) 폴백
  - 조회 실패/데이터 없음 → None 반환, 감점 없음 (fail-open — 필터는 보조 지표)
"""

import asyncio
import json
import os
import time
from datetime import date
from pathlib import Path
from typing import Dict, Optional

import aiohttp
from loguru import logger

from .dart_checker import DartChecker

CACHE_PATH = Path.home() / ".cache" / "ai_trader" / "asset_growth.json"
CACHE_TTL_SEC = 30 * 86400        # 재무제표는 연 단위 갱신 — 30일 캐시
CACHE_NONE_TTL_SEC = 86400        # 조회 실패(None)는 1일만 — 일시 장애가 30일 굳는 것 방지
FNLTT_URL = "https://opendart.fss.or.kr/api/fnlttSinglAcnt.json"


class AssetGrowthProvider:
    """DART 재무제표에서 총자산 증가율(전년 대비 %)을 조회"""

    def __init__(self, dart_checker: Optional[DartChecker] = None):
        self._api_key = os.getenv("DART_API_KEY", "")
        self._dart = dart_checker or DartChecker()
        self._cache: Dict[str, Dict] = {}
        self._loaded = False
        self._lock = asyncio.Lock()

    # ── 디스크 캐시 ─────────────────────────────────────────
    def _load_cache(self):
        if self._loaded:
            return
        self._loaded = True
        if CACHE_PATH.exists():
            try:
                self._cache = json.loads(CACHE_PATH.read_text())
            except Exception:
                self._cache = {}

    def _save_cache(self):
        try:
            CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
            CACHE_PATH.write_text(json.dumps(self._cache, ensure_ascii=False))
        except Exception as e:
            logger.debug(f"[자산증가율] 캐시 저장 실패: {e}")

    def _cache_fresh(self, symbol: str) -> bool:
        hit = self._cache.get(symbol)
        if hit is None:
            return False
        ttl = CACHE_TTL_SEC if hit.get("growth_pct") is not None else CACHE_NONE_TTL_SEC
        return time.time() - hit.get("ts", 0) < ttl

    # ── 조회 ───────────────────────────────────────────────
    async def get_asset_growth(self, symbol: str) -> Optional[float]:
        """총자산 증가율 % (전년 대비). 실패 시 None."""
        if not self._api_key:
            return None

        self._load_cache()
        if self._cache_fresh(symbol):
            return self._cache[symbol].get("growth_pct")

        async with self._lock:
            # 락 대기 중 다른 코루틴이 채웠을 수 있다
            if self._cache_fresh(symbol):
                return self._cache[symbol].get("growth_pct")

            growth = await self._fetch(symbol)
            self._cache[symbol] = {"growth_pct": growth, "ts": time.time()}
            self._save_cache()
            return growth

    async def _fetch(self, symbol: str) -> Optional[float]:
        await self._dart.ensure_corp_code_map()
        corp_code = self._dart._corp_code_map.get(symbol)
        if not corp_code:
            return None

        # 직전 완결 사업연도: 3월 말 이전에는 재작년 보고서가 최신일 수 있다
        today = date.today()
        year = today.year - 1 if today.month >= 4 else today.year - 2

        for fs_div in ("CFS", "OFS"):   # 연결 우선, 별도 폴백
            for bsns_year in (year, year - 1):
                growth = await self._fetch_once(corp_code, bsns_year, fs_div)
                if growth is not None:
                    return growth
        return None

    async def _fetch_once(self, corp_code: str, bsns_year: int,
                          fs_div: str) -> Optional[float]:
        params = {
            "crtfc_key": self._api_key,
            "corp_code": corp_code,
            "bsns_year": str(bsns_year),
            "reprt_code": "11011",   # 사업보고서
            "fs_div": fs_div,
        }
        try:
            timeout = aiohttp.ClientTimeout(total=15)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(FNLTT_URL, params=params) as resp:
                    if resp.status != 200:
                        return None
                    data = await resp.json(content_type=None)
        except Exception as e:
            logger.debug(f"[자산증가율] DART 조회 실패 ({corp_code}/{bsns_year}): {e}")
            return None

        if data.get("status") != "000":
            return None

        for row in data.get("list", []):
            if row.get("account_nm") != "자산총계":
                continue
            try:
                cur = float(str(row.get("thstrm_amount", "")).replace(",", ""))
                prev = float(str(row.get("frmtrm_amount", "")).replace(",", ""))
            except (ValueError, TypeError):
                continue
            if prev > 0 and cur > 0:
                return (cur / prev - 1.0) * 100.0
        return None


_provider: Optional[AssetGrowthProvider] = None


def get_asset_growth_provider() -> AssetGrowthProvider:
    """싱글턴 (corp_code 맵/캐시 공유)"""
    global _provider
    if _provider is None:
        _provider = AssetGrowthProvider()
    return _provider
