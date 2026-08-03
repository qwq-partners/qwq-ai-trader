"""
DART 재무 스냅샷 프로바이더 — 밸류코어(value_growth_core) 데이터 계층 (2026-08-04)

fnlttSinglAcnt 사업보고서 1회 호출로 당기/전기/전전기 3개년의
매출액·영업이익·당기순이익·자산총계·부채총계·자본총계를 확보한다.
(설계: docs/strategies/value-growth-core-design.md §3)

- AssetGrowthProvider 패턴 미러링: 30일 디스크 캐시 (실패 건 1일), CFS 우선 OFS 폴백
- 스크리너 자격 필터가 fail-closed이므로 조회 실패 시 None 반환
  → 해당 종목은 후보에서 제외된다 (감점이 아니라 배제)
"""

import asyncio
import json
import os
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Dict, Optional

import aiohttp
from loguru import logger

from .dart_checker import DartChecker

CACHE_PATH = Path.home() / ".cache" / "ai_trader" / "financials.json"
CACHE_TTL_SEC = 30 * 86400        # 연 단위 갱신 데이터 — 30일 캐시
CACHE_NONE_TTL_SEC = 86400        # 조회 실패는 1일만 (일시 장애 고착 방지)
FNLTT_URL = "https://opendart.fss.or.kr/api/fnlttSinglAcnt.json"

# fnlttSinglAcnt 계정과목 → 내부 키
# ⚠️ 실측(2026-08-04): 순이익 계정명은 "당기순이익(손실)" — 접두 매칭 필요.
#    fs_div 파라미터는 API가 무시하고 항상 CFS+OFS 전 행을 반환 → 파싱에서 행 필터.
_ACCOUNT_KEYS = {
    "매출액": "revenue",
    "영업이익": "op",
    "자산총계": "assets",
    "부채총계": "liab",
    "자본총계": "equity",
}


def _account_key(account_nm: str) -> Optional[str]:
    nm = (account_nm or "").strip()
    if nm.startswith("당기순이익"):        # "당기순이익(손실)" 포함
        return "net"
    return _ACCOUNT_KEYS.get(nm)


@dataclass
class FinancialSnapshot:
    """3개년 재무 스냅샷 + 파생 지표 (금액 단위: 원)"""
    symbol: str
    fiscal_year: int                       # 당기 사업연도
    revenue: Optional[float] = None
    revenue_prev: Optional[float] = None
    op: Optional[float] = None
    op_prev: Optional[float] = None
    net: Optional[float] = None
    net_prev: Optional[float] = None
    net_prev2: Optional[float] = None
    assets: Optional[float] = None
    liab: Optional[float] = None
    equity: Optional[float] = None

    # ── 파생 지표 ───────────────────────────────────────────
    @property
    def revenue_yoy_pct(self) -> Optional[float]:
        if self.revenue is not None and self.revenue_prev is not None \
                and self.revenue_prev > 0:
            return (self.revenue / self.revenue_prev - 1.0) * 100.0
        return None

    @property
    def op_yoy_pct(self) -> Optional[float]:
        if self.op is not None and self.op_prev is not None and self.op_prev > 0:
            return (self.op / self.op_prev - 1.0) * 100.0
        return None

    @property
    def roe_pct(self) -> Optional[float]:
        if self.net is not None and self.equity is not None and self.equity > 0:
            return self.net / self.equity * 100.0
        return None

    @property
    def debt_ratio_pct(self) -> Optional[float]:
        if self.liab is not None and self.equity is not None and self.equity > 0:
            return self.liab / self.equity * 100.0
        return None

    @property
    def op_margin_pct(self) -> Optional[float]:
        if self.op is not None and self.revenue is not None and self.revenue > 0:
            return self.op / self.revenue * 100.0
        return None

    @property
    def op_margin_prev_pct(self) -> Optional[float]:
        if self.op_prev is not None and self.revenue_prev is not None \
                and self.revenue_prev > 0:
            return self.op_prev / self.revenue_prev * 100.0
        return None

    @property
    def op_margin_improve_pp(self) -> Optional[float]:
        cur, prev = self.op_margin_pct, self.op_margin_prev_pct
        if cur is not None and prev is not None:
            return cur - prev
        return None

    @property
    def profit_years(self) -> int:
        """당기부터 연속 흑자 연수 (0~3)"""
        count = 0
        for v in (self.net, self.net_prev, self.net_prev2):
            if v is not None and v > 0:
                count += 1
            else:
                break
        return count


class FinancialsProvider:
    """DART 사업보고서 기반 3개년 재무 스냅샷 (종목당 API 1콜)"""

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
            logger.debug(f"[재무] 캐시 저장 실패: {e}")

    def _cache_fresh(self, symbol: str) -> bool:
        hit = self._cache.get(symbol)
        if hit is None:
            return False
        ttl = CACHE_TTL_SEC if hit.get("data") else CACHE_NONE_TTL_SEC
        return time.time() - hit.get("ts", 0) < ttl

    def _from_cache(self, symbol: str) -> Optional[FinancialSnapshot]:
        data = self._cache.get(symbol, {}).get("data")
        if not data:
            return None
        try:
            return FinancialSnapshot(**data)
        except TypeError:
            # 필드 변경 후 남은 구버전 캐시 — 무효화하고 재조회 유도 (리뷰 P2-2)
            self._cache.pop(symbol, None)
            return None

    # ── 조회 ───────────────────────────────────────────────
    async def get_snapshot(self, symbol: str) -> Optional[FinancialSnapshot]:
        """3개년 재무 스냅샷. 미확보 시 None (호출측 fail-closed)."""
        if not self._api_key:
            return None

        self._load_cache()
        if self._cache_fresh(symbol):
            return self._from_cache(symbol)

        async with self._lock:
            if self._cache_fresh(symbol):
                return self._from_cache(symbol)

            snap = await self._fetch(symbol)
            self._cache[symbol] = {
                "data": snap.__dict__ if snap is not None else None,
                "ts": time.time(),
            }
            self._save_cache()
            return snap

    async def _fetch(self, symbol: str) -> Optional[FinancialSnapshot]:
        await self._dart.ensure_corp_code_map()
        corp_code = self._dart._corp_code_map.get(symbol)
        if not corp_code:
            return None

        # 직전 완결 사업연도 (3월 말 제출 마감 전에는 재작년이 최신)
        today = date.today()
        year = today.year - 1 if today.month >= 4 else today.year - 2

        for bsns_year in (year, year - 1):
            snap = await self._fetch_once(symbol, corp_code, bsns_year)
            if snap is not None:
                return snap
        return None

    async def _fetch_once(self, symbol: str, corp_code: str,
                          bsns_year: int) -> Optional[FinancialSnapshot]:
        params = {
            "crtfc_key": self._api_key,
            "corp_code": corp_code,
            "bsns_year": str(bsns_year),
            "reprt_code": "11011",   # 사업보고서 (CFS+OFS 전 행이 한 번에 온다)
        }
        try:
            timeout = aiohttp.ClientTimeout(total=15)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(FNLTT_URL, params=params) as resp:
                    if resp.status != 200:
                        return None
                    data = await resp.json(content_type=None)
        except Exception as e:
            logger.debug(f"[재무] DART 조회 실패 ({symbol}/{bsns_year}): {e}")
            return None

        if data.get("status") != "000":
            return None

        def _amt(row: dict, field: str) -> Optional[float]:
            try:
                return float(str(row.get(field, "")).replace(",", ""))
            except (ValueError, TypeError):
                return None

        rows = data.get("list", [])
        for fs_div in ("CFS", "OFS"):   # 연결 우선, 별도 폴백
            snap = FinancialSnapshot(symbol=symbol, fiscal_year=bsns_year)
            found = False
            for row in rows:
                if row.get("fs_div") != fs_div:
                    continue
                key = _account_key(row.get("account_nm", ""))
                if key is None:
                    continue
                cur = _amt(row, "thstrm_amount")       # 당기
                prev = _amt(row, "frmtrm_amount")      # 전기
                prev2 = _amt(row, "bfefrmtrm_amount")  # 전전기
                if cur is not None:
                    found = True
                setattr(snap, key, cur)
                if key in ("revenue", "op", "net"):
                    setattr(snap, f"{key}_prev", prev)
                if key == "net":
                    snap.net_prev2 = prev2

            # 자본총계 없으면 파생 지표 대부분 계산 불가 — 다음 fs_div 시도
            if found and snap.equity is not None:
                return snap
        return None


_provider: Optional[FinancialsProvider] = None


def get_financials_provider() -> FinancialsProvider:
    """싱글턴 (corp_code 맵/캐시 공유)"""
    global _provider
    if _provider is None:
        _provider = FinancialsProvider()
    return _provider
