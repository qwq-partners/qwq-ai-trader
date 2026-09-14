"""
AI Trading Bot v2 - US 시장 오버나이트 데이터 프로바이더

Yahoo Finance REST API를 통해 미국 시장 마감 데이터를 조회하여
한국 장 개장 전 테마 점수를 사전 부스트합니다.

핵심 원리:
  US 반도체 +3% -> 한국 반도체 테마 +25점 부스트 -> 장 시작과 동시에 관련 종목 포착

타임라인:
  06:00 KST - US 시장 마감 -> Yahoo Finance 데이터 확정
  08:00 KST - 아침 레포트에 US 섹션 포함
  09:00 KST - ThemeDetector에서 캐시된 US 데이터로 부스트

API 호출 횟수: 하루 1회 (Yahoo Finance)
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import aiohttp
from loguru import logger


def _clean_num(value: Any) -> Optional[float]:
    """숫자만 통과시키고 나머지는 결측(None)으로 — 0으로 채우지 않는다 (T10 F18).

    Yahoo 응답에 필드가 아예 없거나(None), 문자열/NaN/inf처럼 계산에 쓸 수 없는
    값이면 전부 None. 정상적인 0.0(무변동)은 그대로 유지한다.
    """
    if value is None or isinstance(value, bool):
        return None
    if not isinstance(value, (int, float)):
        return None
    value = float(value)
    if not math.isfinite(value):
        return None
    return value


# ──────────────────────────────────────────────────────────────────
# 추적 대상 심볼 (~40개)
# ──────────────────────────────────────────────────────────────────
US_SYMBOLS: List[str] = [
    # 주요 지수
    "^GSPC", "^IXIC", "^SOX", "^DJI", "^VIX",
    # 섹터 ETF
    "XLK", "SMH", "SOXX", "XLV", "XLE", "ICLN", "URNM", "IBB", "XBI",
    "LIT", "TAN", "BOTZ", "ITA",
    # 반도체/AI
    "NVDA", "AMD", "TSM", "ASML", "INTC",
    # 원전/SMR
    "SMR", "OKLO", "CEG", "VST",
    # EV/배터리
    "TSLA", "RIVN", "LCID",
    # 빅테크
    "AAPL", "MSFT", "GOOG", "META", "AMZN",
    # 클린에너지
    "ENPH", "FSLR",
    # 방산
    "LMT", "RTX", "NOC", "GD",
]

# ──────────────────────────────────────────────────────────────────
# US -> 한국 테마 매핑
# ──────────────────────────────────────────────────────────────────
# 각 한국 테마에 대응하는 US 심볼 그룹
US_KOREA_SECTOR_MAP: Dict[str, Dict] = {
    "AI/반도체": {
        "symbols": ["^SOX", "SMH", "SOXX", "NVDA", "AMD", "TSM", "ASML", "INTC"],
        # (임계값, 부스트점수) 리스트 -- 내림차순
        "thresholds": [(5.0, 35), (3.0, 25), (2.0, 15), (1.0, 8)],
    },
    "원자력": {
        "symbols": ["URNM", "SMR", "OKLO", "CEG", "VST"],
        "thresholds": [(8.0, 40), (5.0, 30), (3.0, 20), (2.0, 10)],
    },
    "2차전지": {
        "symbols": ["TSLA", "LIT", "RIVN", "LCID"],
        "thresholds": [(5.0, 25), (3.0, 15), (2.0, 8)],
    },
    "바이오": {
        "symbols": ["XLV", "IBB", "XBI"],
        "thresholds": [(4.0, 25), (2.5, 15), (1.5, 8)],
    },
    "탄소중립": {
        "symbols": ["ICLN", "TAN", "ENPH", "FSLR"],
        "thresholds": [(5.0, 25), (3.0, 15), (2.0, 8)],
    },
    "로봇": {
        "symbols": ["BOTZ"],
        "thresholds": [(5.0, 25), (3.0, 15), (2.0, 8)],
    },
    "인터넷/플랫폼": {
        "symbols": ["META", "GOOG", "AMZN"],
        "thresholds": [(5.0, 20), (3.0, 15), (2.0, 8)],
    },
    "방산": {
        "symbols": ["ITA", "LMT", "RTX", "NOC", "GD"],
        "thresholds": [(5.0, 25), (3.0, 15), (1.5, 8)],
    },
}

# 지수 심볼 (종합 심리 판단용) — 표시명 기반, 기존 소비자(daily_report.py 등) 호환 유지
INDEX_SYMBOLS = ["^GSPC", "^IXIC", "^SOX", "^DJI"]
INDEX_NAMES = {
    "^GSPC": "S&P500",
    "^IXIC": "NASDAQ",
    "^SOX": "반도체(SOX)",
    "^DJI": "다우",
}

# 정규화 키 — LLM 레짐 분류 등 코드 소비측이 안정적으로 찾을 단일 출처 (2026-09-14 F10)
# 재현된 버그: 소비측(kr_scheduler.py)은 indices.get("SP500")/("SOX")/("VIX")를 찾는데
# 위 INDEX_NAMES는 "S&P500"/"반도체(SOX)" 같은 표시명이고 VIX는 수집 대상에도 없어
# 전부 0으로 전달됐다. get_overnight_signal()의 indices_normalized가 이 키를 쓴다.
# VIX는 레벨 지표라 등락 심리 평균(idx_pcts)에는 넣지 않는다(방향성 지표가 아님).
US_INDEX_KEYS: Dict[str, str] = {
    "^GSPC": "SP500",
    "^IXIC": "NASDAQ",
    "^DJI": "DOW",
    "^SOX": "SOX",
    "^VIX": "VIX",
}

# ──────────────────────────────────────────────────────────────────────────────
# S&P 500 대표 종목 (섹터 ETF별, 시총 비중 기준 정렬)
# (sym, 표시명, 상대 시총 비중)  <-  섹터 내 비중이므로 합산 100 불필요
# ──────────────────────────────────────────────────────────────────────────────
SP500_STOCKS: Dict[str, List] = {
    "XLK": [  # Technology
        ("AAPL",  "Apple",       15.0), ("MSFT",  "Microsoft",  13.5),
        ("NVDA",  "NVIDIA",      11.0), ("AVGO",  "Broadcom",    3.5),
        ("ORCL",  "Oracle",       2.0), ("ADBE",  "Adobe",       1.8),
        ("CRM",   "Salesforce",   1.6), ("AMD",   "AMD",         1.2),
        ("AMAT",  "App. Mater.",  1.1), ("QCOM",  "Qualcomm",    1.0),
    ],
    "XLF": [  # Financials
        ("BRK-B", "Berkshire",   4.5), ("JPM",   "JPMorgan",    4.2),
        ("V",     "Visa",        4.0), ("MA",    "Mastercard",  3.5),
        ("GS",    "Goldman",     1.6), ("MS",    "Morgan St.",  1.5),
        ("BAC",   "Bank of Am.", 2.0), ("WFC",   "Wells Fargo", 1.4),
        ("AXP",   "Amex",        1.1),
    ],
    "XLV": [  # Health Care
        ("LLY",   "Lilly",       5.0), ("UNH",   "UnitedHlth",  4.2),
        ("JNJ",   "J&J",         2.5), ("ABBV",  "AbbVie",      2.2),
        ("MRK",   "Merck",       2.0), ("PFE",   "Pfizer",      1.4),
        ("TMO",   "Thermo F.",   1.1), ("DHR",   "Danaher",     1.0),
    ],
    "XLY": [  # Consumer Discretionary
        ("AMZN",  "Amazon",      8.5), ("TSLA",  "Tesla",       4.2),
        ("HD",    "Home Depot",  2.2), ("MCD",   "McDonald's",  1.5),
        ("BKNG",  "Booking",     1.3), ("NKE",   "Nike",        0.8),
        ("LOW",   "Lowe's",      0.8),
    ],
    "XLC": [  # Communication Services
        ("GOOG",  "Alphabet",    9.0), ("META",  "Meta",        7.5),
        ("NFLX",  "Netflix",     2.5), ("DIS",   "Disney",      1.5),
        ("T",     "AT&T",        1.0), ("VZ",    "Verizon",     0.9),
    ],
    "XLI": [  # Industrials
        ("GE",    "GE Aero",     2.1), ("CAT",   "Caterpillar", 1.9),
        ("RTX",   "RTX Corp",    1.8), ("UNP",   "Union Pac.",  1.6),
        ("HON",   "Honeywell",   1.3), ("BA",    "Boeing",      1.0),
        ("ADP",   "ADP",         1.0),
    ],
    "XLP": [  # Consumer Staples
        ("WMT",   "Walmart",     3.2), ("COST",  "Costco",      2.8),
        ("PG",    "P&G",         2.6), ("KO",    "Coca-Cola",   2.0),
        ("PEP",   "PepsiCo",     1.8), ("PM",    "Phil. Morris",1.0),
    ],
    "XLE": [  # Energy
        ("XOM",   "ExxonMobil",  2.8), ("CVX",   "Chevron",     2.2),
        ("COP",   "ConocoPhil.", 1.3), ("SLB",   "SLB",         0.8),
        ("EOG",   "EOG Res.",    0.7),
    ],
    "XLB": [  # Materials
        ("LIN",   "Linde",       1.6), ("SHW",   "Sherwin-W.",  0.9),
        ("APD",   "Air Prod.",   0.7), ("ECL",   "Ecolab",      0.6),
    ],
    "XLRE": [  # Real Estate
        ("PLD",   "Prologis",    0.9), ("AMT",   "Amer. Tower", 0.8),
        ("EQIX",  "Equinix",     0.6), ("SPG",   "Simon Prop.", 0.5),
    ],
    "XLU": [  # Utilities
        ("NEE",   "NextEra",     1.0), ("DUK",   "Duke En.",    0.6),
        ("SO",    "Southern",    0.5), ("D",     "Dominion",    0.4),
    ],
}


class USMarketData:
    """미국 시장 오버나이트 데이터 (Yahoo Finance REST)"""

    YAHOO_BASE_URL = "https://query2.finance.yahoo.com"
    CACHE_TTL = 86400  # 1일 (초)

    def __init__(self):
        self._session: Optional[aiohttp.ClientSession] = None
        self._cache: Dict[str, Any] = {}
        self._cache_ts: Optional[datetime] = None
        # 2026-09-15 (T10 F18 2차 blocking 재수정): v7 응답에 "심볼은 있었지만
        # price/change_pct 중 하나 이상이 결측"이었던 종목 →
        # {"fields": [결측 필드명...], "data": {살아남은 값 포함 원본 dict}}.
        # _cache(=quotes)에는 넣지 않아(daily_report 등 기존 blind 소비자 보호)
        # get_overnight_signal의 indices_normalized가 "조회 실패"와 "필드만
        # 결측"을 구분하고, 살아남은 값(예: price)은 잃지 않게 하는 데 쓴다.
        self._seen_missing: Dict[str, Dict[str, Any]] = {}

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=20)
            headers = {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
            }
            self._session = aiohttp.ClientSession(timeout=timeout, headers=headers)
        return self._session

    def _is_cache_valid(self) -> bool:
        if not self._cache or self._cache_ts is None:
            return False
        elapsed = (datetime.now() - self._cache_ts).total_seconds()
        return elapsed < self.CACHE_TTL

    # ──────────────────────────────────────────────────────────────
    # 1. fetch_us_market_summary
    # ──────────────────────────────────────────────────────────────
    async def fetch_us_market_summary(self, force_refresh: bool = False) -> Dict[str, Dict]:
        """
        Yahoo Finance에서 전체 US 심볼 시세 조회 (1회 호출).

        Args:
            force_refresh: True이면 캐시를 무시하고 강제로 새 데이터 조회
        Returns:
            {심볼: {price, change, change_pct, name, ...}}
        """
        if not force_refresh and self._is_cache_valid():
            return self._cache

        result = await self._fetch_via_v7()
        # 2026-09-15 (T10 F18 2차): None(=v7 자체 실패)일 때만 v8 폴백. result가
        # {}(모든 심볼이 부분/완전 결측이지만 API 호출은 성공)인 경우는 이미
        # self._seen_missing에 유효한 사유가 채워져 있으므로 폴백·초기화하지 않는다.
        if result is None:
            logger.debug("[USMarket] v7 API 실패, v8 spark 폴백 시도")
            result = await self._fetch_via_v8_spark()
            # v8 spark는 "응답에서 봤지만 필드만 결측"을 추적하지 않으므로(자체적으로
            # 완전 결측 심볼을 건너뜀), v7 시도에서 남은 seen_missing을 그대로 두면
            # 실제로 이번 조회를 수행하지 않은 경로의 사유가 섞인다 — 초기화한다.
            self._seen_missing = {}
            result = result or {}

        if result:
            self._cache = result
            self._cache_ts = datetime.now()
            logger.info(f"[USMarket] {len(result)}개 심볼 시세 조회 완료")
        elif not self._seen_missing:
            logger.warning("[USMarket] US 시장 데이터 조회 실패")

        return result

    async def fetch_sp500_stocks(self) -> Dict[str, Dict]:
        """
        S&P 500 대표 종목 시세 조회 (SP500_STOCKS 기준 ~75개)

        섹터 treemap 차트용 데이터. 기존 캐시에 있으면 재활용, 없으면 Yahoo Finance
        v7 -> v8 spark 순으로 폴백 조회.

        Returns:
            {symbol: {price, change_pct, name}} -- 조회 실패한 심볼은 누락
        """
        # 이미 fetch_us_market_summary 캐시에 있는 심볼은 재활용
        all_syms = [
            sym
            for stocks in SP500_STOCKS.values()
            for sym, _, _ in stocks
        ]

        # 기존 캐시 활용 — 가격/등락률이 결측(None)인 캐시 항목은 재사용하지 않는다
        # (T10 F18: docstring이 약속하는 "조회 실패한 심볼은 누락" 계약을 유지).
        result: Dict[str, Dict] = {}
        missing = []
        for sym in all_syms:
            cached = self._cache.get(sym)
            if cached and cached.get("price") is not None and cached.get("change_pct") is not None:
                result[sym] = cached
            else:
                missing.append(sym)

        if not missing:
            return result

        # 누락 심볼 추가 조회 (v7 우선)
        try:
            session  = await self._get_session()
            url      = f"{self.YAHOO_BASE_URL}/v7/finance/quote"
            chunk_sz = 40
            for i in range(0, len(missing), chunk_sz):
                chunk = missing[i:i + chunk_sz]
                params = {
                    "symbols": ",".join(chunk),
                    "fields": "symbol,shortName,regularMarketPrice,"
                              "regularMarketChange,regularMarketChangePercent",
                }
                try:
                    async with session.get(url, params=params,
                                           timeout=aiohttp.ClientTimeout(total=15)) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            for q in data.get("quoteResponse", {}).get("result", []):
                                sym = q.get("symbol", "")
                                if not sym:
                                    continue
                                price = _clean_num(q.get("regularMarketPrice"))
                                change_pct = _clean_num(q.get("regularMarketChangePercent"))
                                if price is None or change_pct is None:
                                    continue  # 결측 — 0으로 채우지 않고 누락 계약 유지 (T10 F18)
                                result[sym] = {
                                    "price":      price,
                                    "change_pct": change_pct,
                                    "name":       q.get("shortName", sym),
                                }
                except Exception:
                    pass

            # v8 spark 폴백
            still_missing = [s for s in missing if s not in result]
            if still_missing:
                url8 = f"{self.YAHOO_BASE_URL}/v8/finance/spark"
                for i in range(0, len(still_missing), 20):
                    chunk = still_missing[i:i + 20]
                    params = {"symbols": ",".join(chunk), "range": "1d", "interval": "1d"}
                    try:
                        async with session.get(url8, params=params,
                                               timeout=aiohttp.ClientTimeout(total=15)) as resp:
                            if resp.status == 200:
                                data = await resp.json()
                                self._parse_v8_spark_data(data, result)
                    except Exception:
                        pass

        except Exception as e:
            logger.warning(f"[USMarket] S&P500 종목 조회 오류: {e}")

        logger.info(f"[USMarket] S&P500 종목 조회: {len(result)}개 수집")
        return result

    async def _fetch_via_v7(self) -> Optional[Dict[str, Dict]]:
        """Yahoo Finance v7 quote API"""
        try:
            session = await self._get_session()
            symbols_str = ",".join(US_SYMBOLS)
            url = f"{self.YAHOO_BASE_URL}/v7/finance/quote"
            params = {
                "symbols": symbols_str,
                # regularMarketTime: 실제 체결(마감) 시각(epoch, UTC) — 조회 시각과
                # 구분해 as_of를 채우는 데 쓴다(2026-09-14 T9 리뷰 advisory).
                "fields": "symbol,shortName,regularMarketPrice,"
                          "regularMarketChange,regularMarketChangePercent,"
                          "regularMarketVolume,regularMarketTime",
            }

            async with session.get(url, params=params) as resp:
                if resp.status != 200:
                    logger.debug(f"[USMarket] v7 HTTP {resp.status}")
                    return None
                data = await resp.json()

            quotes = data.get("quoteResponse", {}).get("result", [])
            if not quotes:
                return None

            result: Dict[str, Dict] = {}
            seen_missing: Dict[str, Dict[str, Any]] = {}
            for q in quotes:
                symbol = q.get("symbol", "")
                if not symbol:
                    continue
                price = _clean_num(q.get("regularMarketPrice"))
                change_pct = _clean_num(q.get("regularMarketChangePercent"))
                change = _clean_num(q.get("regularMarketChange"))
                name = q.get("shortName", symbol)
                volume = q.get("regularMarketVolume", 0)
                # epoch seconds(UTC) 또는 결측(v7가 안 주면 None) — 그대로 보존,
                # 시장 시각으로 변환/가공은 소비 지점(get_overnight_signal)에서.
                market_time = q.get("regularMarketTime")
                if price is None or change_pct is None:
                    # 2026-09-15 (T10 F18 2차 blocking 재수정): 완전 결측뿐 아니라
                    # "일부"만 결측이어도(price만 NaN 등) quotes(=self._cache)에는 넣지
                    # 않는다 — fetch_sp500_stocks(318행)의 "price is None or change_pct
                    # is None" 제외 기준과 맞춘다. daily_report/us_market_chart 등
                    # quotes[sym]["change_pct"]를 가드 없이 바로 쓰는 기존 소비자가
                    # None에서 TypeError를 내는 것을 막기 위함(리뷰 F18 blocking 2차).
                    # 살아남은 값(예: price)과 market_time은 seen_missing에 그대로
                    # 보존해 get_overnight_signal의 indices_normalized가 "price는
                    # 살리고 missing_fields로 change_pct만 표시"할 수 있게 한다.
                    seen_missing[symbol] = {
                        "fields": [
                            f for f, v in (("price", price), ("change_pct", change_pct))
                            if v is None
                        ],
                        "data": {
                            "price": price,
                            "change": change,
                            "change_pct": change_pct,
                            "name": name,
                            "volume": volume,
                            "market_time": market_time,
                        },
                    }
                    continue
                result[symbol] = {
                    # 정상적인 0.0 변동은 그대로 유지된다(0-fill과 구분).
                    "price": price,
                    "change": change,
                    "change_pct": change_pct,
                    "name": name,
                    "volume": volume,
                    "market_time": market_time,
                }
            self._seen_missing = seen_missing
            # 2026-09-15 (T10 F18 2차): API 호출 자체는 성공했으면(quotes 비어있지
            # 않음) result가 {}(모든 심볼이 부분/완전 결측)여도 None을 반환하지
            # 않는다 — None은 "v7 자체가 실패"(HTTP 오류·빈 응답·예외) 전용이다.
            # None을 반환하면 fetch_us_market_summary가 v8 폴백을 트리거하며
            # 위에서 채운 seen_missing까지 지워버려, 부분 결측 정보(예: SOX
            # price)가 통째로 사라진다.
            return result

        except Exception as e:
            logger.debug(f"[USMarket] v7 조회 오류: {e}")
            return None

    async def _fetch_via_v8_spark(self) -> Optional[Dict[str, Dict]]:
        """Yahoo Finance v8 spark API (폴백). 20개 제한 -> 청크 분할 호출."""
        try:
            session = await self._get_session()
            url = f"{self.YAHOO_BASE_URL}/v8/finance/spark"
            chunk_size = 20  # Yahoo v8 spark 심볼 제한

            result: Dict[str, Dict] = {}

            for i in range(0, len(US_SYMBOLS), chunk_size):
                chunk = US_SYMBOLS[i:i + chunk_size]
                params = {
                    "symbols": ",".join(chunk),
                    "range": "1d",
                    "interval": "1d",
                }

                async with session.get(url, params=params) as resp:
                    if resp.status != 200:
                        logger.debug(f"[USMarket] v8 spark chunk HTTP {resp.status}")
                        continue
                    data = await resp.json()

                if not data:
                    continue

                self._parse_v8_spark_data(data, result)

            return result if result else None

        except Exception as e:
            logger.debug(f"[USMarket] v8 spark 조회 오류: {e}")
            return None

    @staticmethod
    def _parse_v8_spark_data(data: Dict, result: Dict[str, Dict]):
        """v8 spark 응답 파싱 (flat / nested 형식 자동 감지).

        2026-09-15 (T10 F18): 가격/전일종가가 없거나 비숫자/NaN/inf면 change/
        change_pct를 0으로 채우지 않는다 — 계산 자체가 불가하므로 심볼을 result에
        아예 넣지 않는다(fetch_sp500_stocks 등 "조회 실패=누락" 기존 계약과 동일).
        """
        if "spark" in data:
            # 레거시 형식: {"spark": {"result": [...]}}
            items = data.get("spark", {}).get("result", [])
            if not items:
                return
            for item in items:
                symbol = item.get("symbol", "")
                if not symbol:
                    continue
                response_data = item.get("response", [{}])
                if not response_data:
                    continue
                meta = response_data[0].get("meta", {})
                prev_close = _clean_num(meta.get("chartPreviousClose"))
                if prev_close is None:
                    prev_close = _clean_num(meta.get("previousClose"))
                current = _clean_num(meta.get("regularMarketPrice"))
                # prev_close<=0은 분모로 쓸 수 없어 결측과 동일 취급
                # (T10 F18 advisory — 0 truthiness 금지 패턴 회피).
                if current is None or prev_close is None or prev_close <= 0:
                    continue
                change = current - prev_close
                change_pct = (change / prev_close) * 100
                result[symbol] = {
                    "price": current,
                    "change": change,
                    "change_pct": change_pct,
                    "name": symbol,
                    "volume": meta.get("regularMarketVolume", 0),
                }
        else:
            # 현행 형식: {symbol: {close: [...], chartPreviousClose: ...}}
            for symbol, item in data.items():
                if not isinstance(item, dict):
                    continue
                closes = item.get("close", [])
                current = _clean_num(closes[-1]) if closes else None
                prev_close = _clean_num(item.get("chartPreviousClose"))
                if prev_close is None:
                    prev_close = _clean_num(item.get("previousClose"))
                if current is None or not prev_close:
                    continue

                change = current - prev_close
                change_pct = (change / prev_close) * 100

                result[symbol] = {
                    "price": current,
                    "change": round(change, 2),
                    "change_pct": round(change_pct, 2),
                    "name": symbol,
                    "volume": 0,
                }

    # ──────────────────────────────────────────────────────────────
    # 2. get_sector_signals -- US 등락률 -> 한국 테마 부스트
    # ──────────────────────────────────────────────────────────────
    async def get_sector_signals(self) -> Dict[str, Dict]:
        """
        US 등락률 -> 한국 테마 부스트 점수 변환.

        Returns:
            {테마명: {boost, us_avg_pct, us_max_pct, top_movers}}
        """
        quotes = await self.fetch_us_market_summary()
        if not quotes:
            return {}

        signals: Dict[str, Dict] = {}

        for theme_name, mapping in US_KOREA_SECTOR_MAP.items():
            symbols = mapping["symbols"]
            thresholds = mapping["thresholds"]

            # 해당 그룹의 등락률 수집
            pcts: List[Tuple[str, float]] = []
            for sym in symbols:
                q = quotes.get(sym)
                # T10 F18: change_pct가 None(결측)이면 섹터 평균 계산에서 제외
                if q and q.get("change_pct") is not None:
                    pcts.append((sym, q["change_pct"]))

            if not pcts:
                continue

            pct_values = [p for _, p in pcts]
            avg_pct = sum(pct_values) / len(pct_values)
            max_pct = max(pct_values)
            min_pct = min(pct_values)

            # 상위 무버 (|등락률| 큰 순)
            sorted_movers = sorted(pcts, key=lambda x: abs(x[1]), reverse=True)
            top_movers = [
                f"{sym}({pct:+.1f}%)" for sym, pct in sorted_movers[:3]
            ]

            # 부스트 계산
            # 상승: max_pct가 임계값 이상이면 양수 부스트
            # 하락: 양수 부스트 없고 avg_pct가 음수이면 음수 부스트
            boost = 0
            if max_pct > 0:
                ref_pct = max_pct
                for threshold, score in thresholds:
                    if ref_pct >= threshold:
                        boost = score
                        break
            if boost == 0 and avg_pct < 0:
                ref_pct = abs(avg_pct)
                for threshold, score in thresholds:
                    if ref_pct >= threshold:
                        boost = -score
                        break

            if boost != 0:
                signals[theme_name] = {
                    "boost": boost,
                    "us_avg_pct": round(avg_pct, 2),
                    "us_max_pct": round(max_pct, 2),
                    "us_min_pct": round(min_pct, 2),
                    "top_movers": top_movers,
                }

        return signals

    # ──────────────────────────────────────────────────────────────
    # 3. get_overnight_signal -- 종합 시그널
    # ──────────────────────────────────────────────────────────────
    async def get_overnight_signal(self) -> Dict[str, Any]:
        """
        종합 오버나이트 시그널.

        Returns:
            {
                sentiment: "bullish" | "bearish" | "neutral",
                indices: {S&P500: ..., NASDAQ: ..., ...},
                sector_signals: {테마: {boost, ...}},
                summary: "요약 텍스트",
            }
        """
        quotes = await self.fetch_us_market_summary()
        # 2026-09-15 (T10 F18 2차): quotes가 비어도 self._seen_missing에 부분/완전
        # 결측 사유가 남아 있으면 "완전 실패" 지름길로 빠지지 않고 아래 정상 경로에서
        # indices_normalized를 seen_missing 기반으로 구성한다(예: 유일한 응답
        # 심볼이 price만 살아남은 경우).
        if not quotes and not self._seen_missing:
            return {
                "sentiment": "neutral",
                "indices": {},
                "indices_normalized": {
                    key: {
                        "price": None, "change": None, "change_pct": None,
                        "fetched_at": None, "as_of": None, "source": "yahoo_finance",
                        "missing": True, "reason": "US 시장 데이터 조회 실패",
                        "missing_fields": ["price", "change_pct"],
                    }
                    for key in US_INDEX_KEYS.values()
                },
                "sector_signals": {},
                "summary": "US 시장 데이터 조회 실패",
            }

        # fetched_at은 "쿼리 성공 시각"(캐시 기록 시각). as_of는 실제 체결(마감) 시각을
        # 채우려 시도하고, Yahoo v7이 regularMarketTime을 안 주면(v8 spark 폴백 등)
        # 조회 시각을 시장 시각처럼 보이지 않도록 None + 사유를 남긴다
        # (2026-09-14 T9 리뷰 advisory — as_of/fetched_at 의미 분리).
        fetch_as_of = (self._cache_ts or datetime.now()).isoformat()

        # 1. 지수 등락률 (표시명 기반 — 기존 소비자 호환, 필드 스키마 불변)
        # 2026-09-15 (T10 F18): price/change_pct가 결측(None)이면 이 딕셔너리에서
        # 제외한다 — round()/f-string이 None에서 예외를 내는 것도 막고, 0으로
        # 채워 "무변동"으로 오판되는 것도 막는다.
        indices: Dict[str, Dict] = {}
        idx_pcts: List[float] = []
        for sym in INDEX_SYMBOLS:
            q = quotes.get(sym)
            if not q or q.get("price") is None or q.get("change_pct") is None:
                continue
            display_name = INDEX_NAMES.get(sym, sym)
            change = q.get("change")
            indices[display_name] = {
                "price": q["price"],
                "change": round(change, 2) if change is not None else None,
                "change_pct": round(q["change_pct"], 2),
            }
            idx_pcts.append(q["change_pct"])

        # 1-2. 정규화 키 (F10) — 결측은 0이 아닌 명시적 missing 표식
        # 2026-09-15 (T10 F18): 심볼이 응답에 "있었지만" price/change_pct 필드가
        # 없거나 비숫자였던 경우도(예: {"symbol":"^VIX","regularMarketTime":...})
        # "완전히 없었던" 경우와 구분한다. price/change_pct 중 하나라도 유효하면
        # missing=False로 그 값을 보존하고(kr_scheduler._norm이 blanket으로
        # entry.get("missing")부터 검사하므로, 여기서 전부 결측 처리하면 VIX의
        # price(_norm("VIX","price"))처럼 실제로는 쓸 수 있는 값까지 같이 버려진다)
        # missing_fields로 결측 필드만 표시한다. 둘 다 없을 때만 missing=True.
        # 2026-09-15 (T10 F18 2차): 일부 결측 심볼은 quotes(self._cache)에서 빠지므로
        # (위 _fetch_via_v7 참조) self._seen_missing["data"]에서 살아남은 값을 읽는다.
        indices_normalized: Dict[str, Dict] = {}
        for sym, key in US_INDEX_KEYS.items():
            q = quotes.get(sym)
            seen = self._seen_missing.get(sym)
            src = q if q is not None else (seen["data"] if seen else None)
            price = src.get("price") if src else None
            change_pct = src.get("change_pct") if src else None
            if src and (price is not None or change_pct is not None):
                change = src.get("change")
                market_time = src.get("market_time")
                as_of_val = None
                as_of_note = "시장 시각 미제공"
                if isinstance(market_time, (int, float)) and market_time > 0:
                    try:
                        as_of_val = datetime.fromtimestamp(
                            market_time, tz=timezone.utc
                        ).isoformat()
                        as_of_note = None
                    except (OSError, OverflowError, ValueError):
                        as_of_val = None
                        as_of_note = "시장 시각 파싱 실패"
                indices_normalized[key] = {
                    "price": price,
                    "change": round(change, 2) if change is not None else None,
                    "change_pct": round(change_pct, 2) if change_pct is not None else None,
                    "fetched_at": fetch_as_of,
                    "as_of": as_of_val,
                    "source": "yahoo_finance",
                    "missing": False,
                }
                if as_of_note:
                    indices_normalized[key]["as_of_note"] = as_of_note
                missing_fields = [
                    f for f, v in (("price", price), ("change_pct", change_pct)) if v is None
                ]
                if missing_fields:
                    indices_normalized[key]["missing_fields"] = missing_fields
            elif seen is not None:
                # 2026-09-15 (T10 F18 2차 blocking 재수정): 심볼이 v7 응답에는 있었지만
                # price/change_pct가 둘 다 결측이어서 quotes(self._cache)에는 넣지
                # 않은 경우 — "조회 실패"와 구분해 사유를 남긴다. quotes에서는 빠졌지만
                # (daily_report 등 blind 소비자 보호) 여기서는 여전히 구분 가능하다.
                indices_normalized[key] = {
                    "price": None, "change": None, "change_pct": None,
                    "fetched_at": None, "as_of": None, "source": "yahoo_finance",
                    "missing": True,
                    "reason": f"{sym} 필드 결측(price, change_pct)",
                    "missing_fields": ["price", "change_pct"],
                }
            else:
                indices_normalized[key] = {
                    "price": None, "change": None, "change_pct": None,
                    "fetched_at": None, "as_of": None, "source": "yahoo_finance",
                    "missing": True, "reason": f"{sym} 조회 실패 또는 응답에 없음",
                    "missing_fields": ["price", "change_pct"],
                }

        # 2. 시장 심리 판단
        if idx_pcts:
            avg_idx = sum(idx_pcts) / len(idx_pcts)
            if avg_idx >= 1.0:
                sentiment = "bullish"
            elif avg_idx <= -1.0:
                sentiment = "bearish"
            else:
                sentiment = "neutral"
        else:
            sentiment = "neutral"
            avg_idx = 0

        # 3. 섹터 시그널
        sector_signals = await self.get_sector_signals()

        # 4. 요약 텍스트 생성
        summary_parts = []
        sentiment_kr = {"bullish": "강세", "bearish": "약세", "neutral": "보합"}.get(
            sentiment, "보합"
        )
        summary_parts.append(f"US 시장 {sentiment_kr} 마감")

        # 지수 요약
        idx_strs = []
        for name, info in indices.items():
            idx_strs.append(f"{name} {info['change_pct']:+.1f}%")
        if idx_strs:
            summary_parts.append(f"({', '.join(idx_strs)})")

        # 테마 영향 요약
        boosted_themes = [
            f"{t}({s['boost']:+d})" for t, s in sector_signals.items()
        ]
        if boosted_themes:
            summary_parts.append(f"-> 한국 테마 영향: {', '.join(boosted_themes)}")

        return {
            "sentiment": sentiment,
            "indices": indices,
            "indices_normalized": indices_normalized,
            "sector_signals": sector_signals,
            "summary": " ".join(summary_parts),
        }

    async def close(self):
        """세션 정리"""
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None


# ──────────────────────────────────────────────────────────────────
# 싱글톤
# ──────────────────────────────────────────────────────────────────
_us_market_data: Optional[USMarketData] = None


def get_us_market_data() -> USMarketData:
    """싱글톤 USMarketData 반환"""
    global _us_market_data
    if _us_market_data is None:
        _us_market_data = USMarketData()
    return _us_market_data
