"""
US 시장 테마/섹터 탐지기

RSS 뉴스 수집 + LLM 테마 추출 + 섹터 ETF 모멘텀으로
미국 시장의 투자 테마를 탐지합니다.

데이터 소스:
- MarketWatch, CNBC, Yahoo Finance RSS (무료, API 키 불필요)
- Finnhub 뉴스 API (선택적, API 키 있을 때 보너스)
- SPDR 섹터 ETF 모멘텀 (yfinance)
- LLM (Gemini Flash / OpenAI) 테마 추출
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Any, Dict, List, Optional

import aiohttp
from loguru import logger

try:
    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.metrics.pairwise import cosine_similarity
    import numpy as np
    SKLEARN_AVAILABLE = True
except ImportError:
    SKLEARN_AVAILABLE = False
    logger.warning("[US 뉴스] scikit-learn not available - similarity deduplication disabled")

from .kr_theme_detector import NewsArticle, ThemeInfo
from ...utils.llm import LLMManager, LLMTask, get_llm_manager


# ============================================================
# US 시장 테마-종목 매핑
# ============================================================

DEFAULT_THEME_STOCKS = {
    "AI/Semiconductors": ["NVDA", "AMD", "AVGO", "INTC", "MRVL", "TSM", "QCOM", "ARM"],
    "Cloud/SaaS": ["MSFT", "AMZN", "GOOGL", "CRM", "SNOW", "NET", "DDOG", "NOW"],
    "EV/Clean Energy": ["TSLA", "RIVN", "LCID", "ENPH", "FSLR", "PLUG", "NIO", "LI"],
    "Biotech/Pharma": ["LLY", "MRNA", "ABBV", "AMGN", "GILD", "BIIB", "REGN", "PFE"],
    "Fintech/Payments": ["V", "MA", "PYPL", "SQ", "SOFI", "COIN", "AFRM"],
    "Cybersecurity": ["CRWD", "PANW", "ZS", "FTNT", "S", "OKTA"],
    "Space/Defense": ["LMT", "RTX", "NOC", "BA", "RKLB", "PLTR", "GD"],
    "Nuclear Energy": ["CEG", "VST", "SMR", "NNE", "OKLO", "CCJ"],
    "Quantum Computing": ["IONQ", "RGTI", "QBTS", "QUBT"],
    "Robotics/Automation": ["ISRG", "ROK", "TER", "ABBNY"],
    "Streaming/Media": ["NFLX", "DIS", "WBD", "PARA", "ROKU", "SPOT"],
    "Cannabis": ["TLRY", "CGC", "ACB"],
}

THEME_KEYWORDS = {
    "AI/Semiconductors": [
        "AI", "artificial intelligence", "GPU", "chip", "semiconductor",
        "NVIDIA", "HBM", "data center", "LLM", "machine learning",
        "generative AI", "inference", "training", "AMD", "Broadcom",
    ],
    "Cloud/SaaS": [
        "cloud", "SaaS", "AWS", "Azure", "enterprise software",
        "cloud computing", "infrastructure", "Salesforce", "Snowflake",
    ],
    "EV/Clean Energy": [
        "electric vehicle", "EV", "solar", "wind", "renewable",
        "Tesla", "battery", "lithium", "charging", "clean energy",
    ],
    "Biotech/Pharma": [
        "biotech", "FDA", "clinical trial", "drug", "GLP-1",
        "obesity", "gene therapy", "pharma", "approval", "pipeline",
    ],
    "Fintech/Payments": [
        "fintech", "payment", "crypto", "bitcoin", "blockchain",
        "digital wallet", "Coinbase", "DeFi", "stablecoin",
    ],
    "Cybersecurity": [
        "cybersecurity", "hack", "breach", "ransomware", "zero trust",
        "security", "firewall", "CrowdStrike", "Palo Alto",
    ],
    "Space/Defense": [
        "defense", "military", "space", "satellite", "rocket",
        "Pentagon", "missile", "Lockheed", "Raytheon", "drone",
    ],
    "Nuclear Energy": [
        "nuclear", "SMR", "uranium", "power grid", "energy demand",
        "nuclear power", "reactor", "Constellation", "Vistra",
    ],
    "Quantum Computing": [
        "quantum", "qubit", "quantum computing", "quantum supremacy",
    ],
    "Robotics/Automation": [
        "robot", "robotics", "automation", "autonomous", "surgical robot",
    ],
    "Streaming/Media": [
        "streaming", "subscriber", "content", "Netflix", "Disney+",
        "ad-supported", "media", "entertainment",
    ],
    "Cannabis": [
        "cannabis", "marijuana", "weed", "legalization", "THC", "CBD",
    ],
}

# 섹터 ETF (SPDR)
SECTOR_ETFS = {
    "Technology": "XLK",
    "Financials": "XLF",
    "Energy": "XLE",
    "Healthcare": "XLV",
    "Consumer Discretionary": "XLY",
    "Consumer Staples": "XLP",
    "Industrials": "XLI",
    "Materials": "XLB",
    "Utilities": "XLU",
    "Real Estate": "XLRE",
    "Communication": "XLC",
}

# 테마 → 섹터 매핑 (ETF 모멘텀 점수 보정용)
THEME_SECTOR_MAP = {
    "AI/Semiconductors": ["Technology", "Communication"],
    "Cloud/SaaS": ["Technology"],
    "EV/Clean Energy": ["Consumer Discretionary", "Energy", "Utilities"],
    "Biotech/Pharma": ["Healthcare"],
    "Fintech/Payments": ["Financials", "Technology"],
    "Cybersecurity": ["Technology"],
    "Space/Defense": ["Industrials"],
    "Nuclear Energy": ["Utilities", "Energy"],
    "Quantum Computing": ["Technology"],
    "Robotics/Automation": ["Industrials", "Healthcare"],
    "Streaming/Media": ["Communication"],
    "Cannabis": ["Healthcare"],
}

# RSS 피드 소스
RSS_FEEDS = [
    ("https://feeds.marketwatch.com/marketwatch/topstories", "marketwatch"),
    ("https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=100003114", "cnbc"),
    ("https://feeds.finance.yahoo.com/rss/2.0/headline?s=^GSPC&region=US&lang=en-US", "yahoo"),
]

# 잘 알려진 US 종목 힌트 (LLM 프롬프트용)
KNOWN_US_STOCKS = {
    "NVDA": "NVIDIA", "AMD": "AMD", "AVGO": "Broadcom", "INTC": "Intel",
    "MSFT": "Microsoft", "AMZN": "Amazon", "GOOGL": "Alphabet", "AAPL": "Apple",
    "META": "Meta", "TSLA": "Tesla", "NFLX": "Netflix", "CRM": "Salesforce",
    "CRWD": "CrowdStrike", "PANW": "Palo Alto", "COIN": "Coinbase",
    "PLTR": "Palantir", "SNOW": "Snowflake", "LLY": "Eli Lilly",
    "MRNA": "Moderna", "PFE": "Pfizer", "ABBV": "AbbVie", "BA": "Boeing",
    "LMT": "Lockheed Martin", "RTX": "Raytheon", "V": "Visa", "MA": "Mastercard",
    "PYPL": "PayPal", "SQ": "Block", "SOFI": "SoFi", "DIS": "Disney",
    "CEG": "Constellation Energy", "VST": "Vistra", "SMR": "NuScale",
    "IONQ": "IonQ", "RIVN": "Rivian", "NIO": "NIO", "ENPH": "Enphase",
    "FSLR": "First Solar", "NET": "Cloudflare", "ZS": "Zscaler",
    "NOW": "ServiceNow", "DDOG": "Datadog", "SPOT": "Spotify",
    "ARM": "Arm Holdings", "TSM": "TSMC", "MRVL": "Marvell",
}

# 역매핑: 종목 → 테마
_STOCK_TO_THEMES: Dict[str, List[str]] = {}
for _theme, _symbols in DEFAULT_THEME_STOCKS.items():
    for _sym in _symbols:
        _STOCK_TO_THEMES.setdefault(_sym, []).append(_theme)


class USThemeDetector:
    """US 시장 테마 탐지기 (RSS 뉴스 + 섹터 ETF + LLM)"""

    def __init__(self, finnhub_key: str = None):
        self._finnhub_key = finnhub_key or ""
        self.llm: LLMManager = get_llm_manager()
        self._session: Optional[aiohttp.ClientSession] = None

        # 테마 상태
        self._themes: Dict[str, ThemeInfo] = {}
        self._stock_sentiments: Dict[str, Dict] = {}
        self._last_detection: Optional[datetime] = None

        # 중복 제거 1차: SHA1 TTL 기반 (hash → 최초 수집 시각)
        self._seen_hashes: Dict[str, datetime] = {}

        # 중복 제거 2차: TF-IDF 유사도 기반 (인메모리, 최대 500개)
        self._similarity_cache: List[Dict[str, Any]] = []
        self._similarity_threshold = 0.85  # 영문 뉴스 유사도 임계값

        # 섹터 ETF 캐시
        self._sector_momentum: Dict[str, float] = {}
        self._sector_momentum_updated: Optional[datetime] = None

        # 설정
        self.detection_interval_minutes = 30
        self.min_news_count = 2
        self.hot_theme_threshold = 70

        # 키워드 역매핑
        self._keyword_to_theme = self._build_keyword_map()

        logger.info(
            f"[US 테마] 초기화 완료 — "
            f"테마 {len(DEFAULT_THEME_STOCKS)}개, "
            f"RSS {len(RSS_FEEDS)}개, "
            f"Finnhub={'ON' if self._finnhub_key else 'OFF'}"
        )

    # ============================================================
    # 외부 인터페이스
    # ============================================================

    async def detect_themes(self, force: bool = False) -> List[ThemeInfo]:
        """메인 테마 탐지 (30분 주기)"""
        if not force and self._last_detection is not None:
            elapsed = (datetime.now() - self._last_detection).total_seconds() / 60
            if elapsed < self.detection_interval_minutes:
                return list(self._themes.values())

        self._last_detection = datetime.now()
        logger.info("[US 테마] 탐지 시작...")

        try:
            # 1. 뉴스 수집
            articles = await self._collect_news()
            logger.info(f"[US 테마] 뉴스 {len(articles)}건 수집")

            if len(articles) < self.min_news_count:
                logger.warning(f"[US 테마] 뉴스 부족 ({len(articles)}건)")
                self._cleanup_stale()
                return list(self._themes.values())

            # 2. LLM 테마 추출
            llm_result = await self._extract_themes_from_news(articles)
            detected = llm_result.get("themes", [])
            stock_impacts = llm_result.get("stock_impacts", [])

            if not detected and not stock_impacts:
                logger.info("[US 테마] 감지된 테마 없음")
                self._cleanup_stale()
                return list(self._themes.values())

            # 3. 종목 센티멘트 갱신
            now = datetime.now()
            self._update_sentiments(stock_impacts, detected, now)

            # 4. ThemeInfo 갱신
            self._update_themes(detected, articles)

            # 5. 섹터 ETF 모멘텀으로 점수 보정
            await self._adjust_scores_by_sector()

            # 6. stale 정리
            self._cleanup_stale()

            active = [t for t in self._themes.values() if t.score >= self.hot_theme_threshold]
            logger.info(
                f"[US 테마] 탐지 완료: {len(self._themes)}개 테마, "
                f"{len(active)}개 활성 (≥{self.hot_theme_threshold}점)"
            )
            return list(self._themes.values())

        except Exception as e:
            logger.exception(f"[US 테마] 탐지 오류: {e}")
            self._cleanup_stale()
            return list(self._themes.values())

    def get_stock_sentiment(self, symbol: str) -> Optional[Dict]:
        """종목 센티멘트 조회 (1시간 이내)"""
        data = self._stock_sentiments.get(symbol.upper())
        if data is None:
            return None
        if (datetime.now() - data["updated_at"]).total_seconds() > 3600:
            return None
        return data

    def get_all_stock_sentiments(self) -> Dict[str, Dict]:
        """전체 유효 센티멘트"""
        now = datetime.now()
        return {
            sym: data for sym, data in self._stock_sentiments.items()
            if (now - data["updated_at"]).total_seconds() <= 3600
        }

    def get_theme_score(self, symbol: str) -> float:
        """종목이 속한 테마의 최고 점수"""
        symbol = symbol.upper()
        max_score = 0.0
        for theme_name in _STOCK_TO_THEMES.get(symbol, []):
            info = self._themes.get(theme_name)
            if info is not None:
                max_score = max(max_score, info.score)
        # 동적 테마에서도 검색
        for info in self._themes.values():
            if symbol in info.related_stocks:
                max_score = max(max_score, info.score)
        return max_score

    def get_hot_themes(self, min_score: float = 70) -> List[ThemeInfo]:
        """핫 테마 목록"""
        return sorted(
            [t for t in self._themes.values() if t.score >= min_score],
            key=lambda t: t.score, reverse=True,
        )

    def get_stock_themes(self, symbol: str) -> List[str]:
        """종목이 속한 테마들"""
        symbol = symbol.upper()
        themes = list(_STOCK_TO_THEMES.get(symbol, []))
        # 동적 테마
        for name, info in self._themes.items():
            if symbol in info.related_stocks and name not in themes:
                themes.append(name)
        return themes

    def to_dict_list(self) -> List[Dict]:
        """대시보드 API용 딕셔너리 리스트"""
        result = []
        for info in sorted(self._themes.values(), key=lambda t: t.score, reverse=True):
            result.append({
                "name": info.name,
                "score": round(info.score, 1),
                "keywords": info.keywords[:5],
                "stocks": info.related_stocks[:8],
                "news_count": info.news_count,
                "news_items": info.news_items[:5],
                "detected_at": info.detected_at.isoformat(),
                "last_updated": info.last_updated.isoformat(),
            })
        return result

    async def close(self):
        """세션 정리"""
        if self._session is not None and not self._session.closed:
            await self._session.close()
            self._session = None

    # ============================================================
    # 뉴스 수집
    # ============================================================

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=15),
                headers={"User-Agent": "Mozilla/5.0 (compatible; AITrader/2.0)"},
            )
        return self._session

    async def _collect_news(self) -> List[NewsArticle]:
        """전체 뉴스 수집 + SHA1 중복 제거"""
        tasks = [self._fetch_rss(url, source) for url, source in RSS_FEEDS]
        if self._finnhub_key:
            tasks.append(self._fetch_finnhub_news())

        results = await asyncio.gather(*tasks, return_exceptions=True)

        all_articles: List[NewsArticle] = []
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                source = RSS_FEEDS[i][1] if i < len(RSS_FEEDS) else "finnhub"
                logger.warning(f"[US 테마] {source} 수집 실패: {result}")
            elif isinstance(result, list):
                all_articles.extend(result)

        # SHA1 중복 제거 (TTL 2시간: 2시간 지난 기사는 재수집 허용)
        now = datetime.now()
        sha1_cutoff = now - timedelta(hours=2)
        sim_cutoff = now - timedelta(hours=4)

        # 만료된 SHA1 해시 정리
        expired = [h for h, ts in self._seen_hashes.items() if ts < sha1_cutoff]
        for h in expired:
            del self._seen_hashes[h]

        # 만료된 유사도 캐시 정리 (4시간)
        self._similarity_cache = [
            item for item in self._similarity_cache
            if item.get("published_at", now) >= sim_cutoff
        ]

        unique: List[NewsArticle] = []
        for article in all_articles:
            key = hashlib.sha1(
                f"{article.title}{article.url}".encode()
            ).hexdigest()
            if key not in self._seen_hashes:
                self._seen_hashes[key] = now
                unique.append(article)

        # 2차: 유사도 기반 중복 제거 (TF-IDF 코사인 유사도)
        final: List[NewsArticle] = []
        similarity_removed = 0
        for article in unique:
            if self._is_similar_to_existing(article):
                similarity_removed += 1
            else:
                final.append(article)

        logger.info(
            f"[US 뉴스] 수집 완료: 전체={len(all_articles)}, "
            f"SHA1제거={len(unique)}, 유사도제거={similarity_removed}, 최종={len(final)}"
        )
        return final

    def _is_similar_to_existing(self, article: NewsArticle) -> bool:
        """TF-IDF 코사인 유사도 기반 중복 체크 (영문 뉴스)

        Returns:
            True  → 유사 기사 존재 (중복으로 판정)
            False → 신규 기사 (캐시에 추가)
        """
        if not SKLEARN_AVAILABLE or not self._similarity_cache:
            # sklearn 없거나 캐시 비어있으면 바로 캐시 추가 후 통과
            new_text = f"{article.title} {article.content or ''}".strip()
            if new_text:
                self._similarity_cache.append({
                    "text": new_text,
                    "published_at": datetime.now(),
                })
                if len(self._similarity_cache) > 500:
                    self._similarity_cache = self._similarity_cache[-500:]
            return False

        try:
            new_text = f"{article.title} {article.content or ''}".strip()
            if not new_text or len(new_text) < 10:
                return False

            existing_texts = [item["text"] for item in self._similarity_cache]
            all_texts = existing_texts + [new_text]

            vectorizer = TfidfVectorizer(
                max_features=200,
                ngram_range=(1, 2),
                min_df=1,
            )
            tfidf_matrix = vectorizer.fit_transform(all_texts)

            new_vec = tfidf_matrix[-1]
            existing_vecs = tfidf_matrix[:-1]
            similarities = cosine_similarity(new_vec, existing_vecs)[0]
            max_similarity = float(np.max(similarities)) if len(similarities) > 0 else 0.0

            if max_similarity >= self._similarity_threshold:
                logger.debug(
                    f"[US 뉴스 중복] 유사도 {max_similarity:.2f}: {article.title[:60]}"
                )
                return True

            # 신규 기사 → 캐시에 추가 (최대 500개 슬라이딩 윈도우, cached_at 기준)
            self._similarity_cache.append({
                "text": new_text,
                "published_at": datetime.now(),
            })
            if len(self._similarity_cache) > 500:
                self._similarity_cache = self._similarity_cache[-500:]
            return False

        except Exception as e:
            logger.warning(f"[US 뉴스] 유사도 계산 실패: {e}")
            return False

    async def _fetch_rss(self, url: str, source: str, limit: int = 20) -> List[NewsArticle]:
        """범용 RSS/Atom XML 파서"""
        session = await self._get_session()
        try:
            async with session.get(url) as resp:
                if resp.status != 200:
                    logger.warning(f"[US 뉴스] {source} HTTP {resp.status}")
                    return []
                xml_text = await resp.text()
        except Exception as e:
            logger.warning(f"[US 뉴스] {source} 연결 실패: {e}")
            return []

        articles: List[NewsArticle] = []
        try:
            root = ET.fromstring(xml_text)

            # RSS 2.0 <item> 또는 Atom <entry>
            items = list(root.iter("item")) or list(root.iter("entry"))
            for item in items[:limit]:
                title = (item.findtext("title") or "").strip()
                if not title:
                    continue

                link = (item.findtext("link") or "").strip()
                # Atom 형식 link
                if not link:
                    link_elem = item.find("{http://www.w3.org/2005/Atom}link")
                    if link_elem is not None:
                        link = link_elem.get("href", "")

                desc_raw = item.findtext("description") or item.findtext("summary") or ""
                desc = re.sub(r"<[^>]+>", "", desc_raw).strip()[:200]

                pub_dt = datetime.now()
                pub_text = item.findtext("pubDate") or item.findtext("published") or ""
                if pub_text:
                    try:
                        pub_dt = parsedate_to_datetime(pub_text.strip()).replace(tzinfo=None)
                    except Exception:
                        pass

                articles.append(NewsArticle(
                    title=title, content=desc, url=link,
                    source=source, published_at=pub_dt,
                ))
        except ET.ParseError as e:
            logger.warning(f"[US 뉴스] {source} XML 파싱 오류: {e}")

        return articles

    async def _fetch_finnhub_news(self, limit: int = 20) -> List[NewsArticle]:
        """Finnhub 뉴스 API (선택적)"""
        if not self._finnhub_key:
            return []

        session = await self._get_session()
        url = (
            f"https://finnhub.io/api/v1/news"
            f"?category=general&token={self._finnhub_key}"
        )
        try:
            async with session.get(url) as resp:
                if resp.status != 200:
                    return []
                data = await resp.json()
        except Exception as e:
            logger.warning(f"[US 뉴스] Finnhub 수집 실패: {e}")
            return []

        articles: List[NewsArticle] = []
        for item in (data or [])[:limit]:
            title = (item.get("headline") or "").strip()
            if not title:
                continue
            articles.append(NewsArticle(
                title=title,
                content=(item.get("summary") or "")[:200],
                url=item.get("url", ""),
                source="finnhub",
                published_at=datetime.fromtimestamp(item.get("datetime", 0))
                if item.get("datetime") else datetime.now(),
            ))
        return articles

    # ============================================================
    # LLM 테마 추출
    # ============================================================

    async def _extract_themes_from_news(self, articles: List[NewsArticle]) -> Dict:
        """LLM으로 뉴스에서 테마 + 종목 임팩트 추출"""
        # 뉴스 텍스트 (최대 25건)
        news_lines = []
        for i, a in enumerate(articles[:25], 1):
            text = f"{i}. [{a.source}] {a.title}"
            if a.content:
                text += f" — {a.content[:100]}"
            news_lines.append(text)
        news_text = "\n".join(news_lines)

        # 허용 테마 목록
        theme_list = "\n".join(
            f"  - {name}" for name in DEFAULT_THEME_STOCKS.keys()
        )

        # 종목 힌트
        stock_hints = ", ".join(
            f"{sym}={name}" for sym, name in list(KNOWN_US_STOCKS.items())[:30]
        )

        prompt = f"""Below are today's US stock market news headlines and summaries:

{news_text}

Analyze these news items and extract investment themes and individual stock impacts.

**Allowed themes (choose ONLY from these):**
{theme_list}

**Well-known US stock tickers:**
  {stock_hints}

Respond ONLY with this JSON:
{{
  "themes": [
    {{
      "theme": "Exact theme name from allowed list",
      "news_count": 3,
      "score": 80,
      "reason": "Brief reason under 30 chars",
      "stocks": [
        {{"symbol": "NVDA", "name": "NVIDIA", "impact": 8, "direction": "bullish"}}
      ]
    }}
  ],
  "stock_impacts": [
    {{
      "symbol": "AAPL", "name": "Apple", "impact": 5,
      "direction": "bullish", "reason": "Strong earnings beat"
    }}
  ]
}}

Rules:
- themes: max 5, only themes with 2+ related news, max 5 stocks per theme
- stock_impacts: individual stocks in news regardless of theme (max 10)
- impact: -10 (extreme bearish) to 0 (neutral) to +10 (extreme bullish)
- direction: "bullish" if impact > 0, "bearish" if impact < 0
- theme field MUST exactly match an allowed theme name
- symbol must be a valid US stock ticker (1-5 uppercase letters)"""

        try:
            result = await self.llm.complete_json(
                prompt=prompt,
                task=LLMTask.THEME_DETECTION,
                system="You are a US stock market theme analyst. Respond only with valid JSON.",
            )
            if "error" in result:
                logger.warning(f"[US 테마] LLM 오류: {result.get('error')}")
                return {"themes": [], "stock_impacts": []}
            return result
        except Exception as e:
            logger.error(f"[US 테마] LLM 추출 오류: {e}")
            return {"themes": [], "stock_impacts": []}

    # ============================================================
    # 테마 + 센티멘트 갱신
    # ============================================================

    def _update_themes(self, detected: List[Dict], articles: List[NewsArticle]):
        """LLM 결과로 ThemeInfo 갱신"""
        now = datetime.now()

        for item in detected:
            raw_name = item.get("theme", "")
            name = self._normalize_theme_name(raw_name)
            if not name:
                continue

            news_count = int(item.get("news_count", 0))
            score = float(item.get("score", 0))
            reason = item.get("reason", "")

            # 관련 종목
            stocks = []
            for s in item.get("stocks", []):
                sym = self._validate_symbol(s.get("symbol", ""))
                if sym:
                    stocks.append(sym)

            # 기본 매핑 종목 병합
            default_stocks = DEFAULT_THEME_STOCKS.get(name, [])
            merged_stocks = list(dict.fromkeys(stocks + default_stocks))[:10]

            # 키워드
            keywords = THEME_KEYWORDS.get(name, [])

            # 관련 뉴스 아이템
            news_items = self._find_related_news(name, articles)

            existing = self._themes.get(name)
            if existing is not None:
                # 기존 테마 업데이트: 점수 이동평균
                existing.score = existing.score * 0.3 + score * 0.7
                existing.news_count = max(existing.news_count, news_count)
                existing.last_updated = now
                existing.related_stocks = merged_stocks
                existing.news_items = news_items
                existing.news_titles = [n["title"] for n in news_items]
            else:
                self._themes[name] = ThemeInfo(
                    name=name,
                    keywords=keywords,
                    related_stocks=merged_stocks,
                    news_count=news_count,
                    score=score,
                    detected_at=now,
                    last_updated=now,
                    news_titles=[n["title"] for n in news_items],
                    news_items=news_items,
                )

    def _update_sentiments(
        self, stock_impacts: List[Dict], themes: List[Dict], now: datetime
    ):
        """종목별 센티멘트 갱신"""
        # stock_impacts에서
        for item in stock_impacts:
            sym = self._validate_symbol(item.get("symbol", ""))
            if not sym:
                continue
            self._stock_sentiments[sym] = {
                "sentiment": float(item.get("impact", 0)) / 10.0,
                "impact": int(item.get("impact", 0)),
                "direction": item.get("direction", "neutral"),
                "theme": "",
                "reason": item.get("reason", ""),
                "updated_at": now,
            }

        # themes[].stocks에서
        for theme_data in themes:
            theme_name = self._normalize_theme_name(theme_data.get("theme", ""))
            for stock in theme_data.get("stocks", []):
                sym = self._validate_symbol(stock.get("symbol", ""))
                if not sym:
                    continue
                impact = int(stock.get("impact", 0))
                # 이미 있으면 더 강한 영향만 갱신
                existing = self._stock_sentiments.get(sym)
                if existing is not None and abs(existing["impact"]) >= abs(impact):
                    continue
                self._stock_sentiments[sym] = {
                    "sentiment": impact / 10.0,
                    "impact": impact,
                    "direction": stock.get("direction", "neutral"),
                    "theme": theme_name,
                    "reason": theme_data.get("reason", ""),
                    "updated_at": now,
                }

    def _find_related_news(
        self, theme_name: str, articles: List[NewsArticle]
    ) -> List[Dict]:
        """테마 관련 뉴스 찾기 (키워드 매칭)"""
        keywords = THEME_KEYWORDS.get(theme_name, [])
        if not keywords:
            return []

        matched = []
        for article in articles:
            text = article.text.lower()
            if any(kw.lower() in text for kw in keywords):
                matched.append({"title": article.title, "url": article.url})
                if len(matched) >= 5:
                    break
        return matched

    # ============================================================
    # 섹터 ETF 모멘텀
    # ============================================================

    async def _update_sector_momentum(self):
        """yfinance로 섹터 ETF 1일 수익률 계산 (1시간 캐시)"""
        if (
            self._sector_momentum_updated is not None
            and (datetime.now() - self._sector_momentum_updated).total_seconds() < 3600
        ):
            return

        try:
            import yfinance as yf

            tickers = list(SECTOR_ETFS.values())
            data = await asyncio.to_thread(
                yf.download, tickers, period="5d", progress=False, group_by="ticker"
            )

            for sector, etf in SECTOR_ETFS.items():
                try:
                    close = data[etf]["Close"].dropna()
                    if len(close) >= 2:
                        daily_ret = (float(close.iloc[-1]) / float(close.iloc[-2]) - 1) * 100
                        self._sector_momentum[sector] = daily_ret
                except Exception:
                    pass

            self._sector_momentum_updated = datetime.now()
            logger.info(
                f"[US 테마] 섹터 ETF 모멘텀 갱신: "
                f"{len(self._sector_momentum)}개 섹터"
            )
        except Exception as e:
            logger.warning(f"[US 테마] 섹터 ETF 모멘텀 조회 실패: {e}")

    async def _adjust_scores_by_sector(self):
        """섹터 ETF 모멘텀으로 테마 점수 보정"""
        await self._update_sector_momentum()
        if not self._sector_momentum:
            return

        adjusted_count = 0
        for theme_name, theme in self._themes.items():
            sectors = THEME_SECTOR_MAP.get(theme_name, [])
            if not sectors:
                continue

            momentums = [
                self._sector_momentum[s]
                for s in sectors if s in self._sector_momentum
            ]
            if not momentums:
                continue

            avg_momentum = sum(momentums) / len(momentums)
            # 모멘텀 1% → 점수 ±5점, 최대 ±15점
            adjustment = max(-15.0, min(15.0, avg_momentum * 5))
            old_score = theme.score
            theme.score = max(0.0, min(100.0, theme.score + adjustment))
            if abs(adjustment) >= 1:
                adjusted_count += 1

        if adjusted_count:
            logger.info(f"[US 테마] 섹터 모멘텀 보정: {adjusted_count}개 테마")

    # ============================================================
    # 유틸리티
    # ============================================================

    def _build_keyword_map(self) -> Dict[str, str]:
        """키워드 → 테마명 역매핑"""
        mapping: Dict[str, str] = {}
        for theme_name, keywords in THEME_KEYWORDS.items():
            for kw in keywords:
                mapping[kw.lower()] = theme_name
        return mapping

    def _normalize_theme_name(self, raw: str) -> str:
        """LLM 반환 테마명을 DEFAULT_THEME_STOCKS 키로 정규화"""
        if not raw:
            return ""

        # 정확히 일치
        if raw in DEFAULT_THEME_STOCKS:
            return raw

        # 소문자 비교
        raw_lower = raw.lower()
        for name in DEFAULT_THEME_STOCKS:
            if name.lower() == raw_lower:
                return name

        # 부분 문자열 매칭 (가장 긴 키 우선)
        candidates = sorted(DEFAULT_THEME_STOCKS.keys(), key=len, reverse=True)
        for name in candidates:
            if name.lower() in raw_lower or raw_lower in name.lower():
                return name

        # 키워드 매칭
        for kw, theme_name in self._keyword_to_theme.items():
            if kw in raw_lower:
                return theme_name

        logger.debug(f"[US 테마] 정규화 실패: '{raw}'")
        return ""

    @staticmethod
    def _validate_symbol(symbol: str) -> str:
        """US 티커 심볼 검증 (1~5자 영문대문자)"""
        symbol = (symbol or "").strip().upper()
        if not symbol or len(symbol) > 5:
            return ""
        if not re.match(r"^[A-Z]+$", symbol):
            return ""
        return symbol

    def _cleanup_stale(self):
        """1시간 초과 stale 테마/센티멘트 제거"""
        now = datetime.now()
        cutoff = now - timedelta(hours=1)

        stale_themes = [
            name for name, info in self._themes.items()
            if info.last_updated < cutoff
        ]
        for name in stale_themes:
            del self._themes[name]

        stale_sentiments = [
            sym for sym, data in self._stock_sentiments.items()
            if data["updated_at"] < cutoff
        ]
        for sym in stale_sentiments:
            del self._stock_sentiments[sym]

        if stale_themes or stale_sentiments:
            logger.debug(
                f"[US 테마] stale 정리: 테마 {len(stale_themes)}개, "
                f"센티멘트 {len(stale_sentiments)}개 제거"
            )
