"""news-curator — 한국·미국·글로벌 뉴스 통합 수집·sentiment 산출

데이터 소스 (신규 키 0개):
- 네이버 뉴스 (NAVER_CLIENT_ID/SECRET)
- Finnhub (FINNHUB_API_KEY)
- Perplexity sonar (PERPLEXITY_API_KEY)
"""

from __future__ import annotations

import asyncio
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Set, Tuple

import aiohttp
from loguru import logger

from .base import ExpertAgent
from .types import ExpertOpinion, RegimeBias


_HTML_TAG_RE = re.compile(r"<[^>]+>")
_TOKEN_RE = re.compile(r"[가-힣A-Za-z0-9]+")


@dataclass
class NewsItem:
    title: str
    summary: str
    url: str
    source: str           # naver / finnhub / perplexity
    fetched_at: datetime = field(default_factory=datetime.now)
    sentiment: int = 0    # -100 ~ +100
    event_tags: List[str] = field(default_factory=list)
    related_symbols: List[str] = field(default_factory=list)

    def fingerprint_tokens(self) -> Set[str]:
        text = f"{self.title} {self.summary}".lower()
        return set(_TOKEN_RE.findall(text))


class NewsCurator(ExpertAgent):
    name = "news_curator"
    refresh_minutes = 30
    cost_per_call_usd = 0.003

    # KR 키워드 (시장 일반 sentiment 측정용)
    _KR_QUERIES = ["코스피", "외국인 매수", "반도체", "환율", "한국은행"]

    # 종목별 sentiment 캐시 (1h TTL)
    _SYMBOL_TTL = timedelta(hours=1)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._naver_id = os.getenv("NAVER_CLIENT_ID", "")
        self._naver_secret = os.getenv("NAVER_CLIENT_SECRET", "")
        self._finnhub_key = os.getenv("FINNHUB_API_KEY", "")
        self._session: Optional[aiohttp.ClientSession] = None

        # 종목별 sentiment 캐시
        self._symbol_cache: Dict[str, Tuple[Dict[str, Any], datetime]] = {}

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=15)
            )
        return self._session

    # ─────────────────────────────────────────
    # 메인 분석
    # ─────────────────────────────────────────
    async def _analyze(self) -> ExpertOpinion:
        # KR + 글로벌 뉴스 병렬 수집
        kr_task = self._fetch_kr_market_news()
        global_task = self._fetch_global_speculative()
        kr_items, global_items = await asyncio.gather(
            kr_task, global_task, return_exceptions=True
        )

        items: List[NewsItem] = []
        if isinstance(kr_items, list):
            items.extend(kr_items)
        if isinstance(global_items, list):
            items.extend(global_items)

        if not items:
            return self._build_opinion(
                score=0, bias=RegimeBias.NEUTRAL, confidence=0.0,
                findings=["뉴스 수집 실패"], valid_hours=1,
            )

        # 중복 제거
        items = self._deduplicate(items)

        # LLM 일괄 분류 (sentiment + tags)
        await self._classify_batch(items)

        # 집계
        sentiments = [i.sentiment for i in items if i.sentiment != 0]
        avg = sum(sentiments) / len(sentiments) if sentiments else 0
        bias = (
            RegimeBias.BULL if avg > 15
            else RegimeBias.BEAR if avg < -15
            else RegimeBias.NEUTRAL
        )

        # 섹터/이벤트 태그 집계
        event_count: Dict[str, int] = {}
        for it in items:
            for t in it.event_tags:
                event_count[t] = event_count.get(t, 0) + 1
        top_tags = sorted(event_count.items(), key=lambda x: -x[1])[:5]

        findings = [f"{it.title[:60]} ({it.sentiment:+d})" for it in items[:5]]
        confidence = min(0.9, 0.3 + len(items) * 0.03)

        return self._build_opinion(
            score=int(avg),
            bias=bias,
            confidence=confidence,
            findings=findings,
            raw=dict(
                item_count=len(items),
                avg_sentiment=avg,
                top_event_tags=top_tags,
            ),
            valid_hours=1,
        )

    # ─────────────────────────────────────────
    # 종목별 sentiment (engine.on_signal에서 호출)
    # ─────────────────────────────────────────
    async def get_symbol_sentiment(self, symbol: str) -> Optional[Dict[str, Any]]:
        """종목별 24h sentiment + 이벤트 태그

        Returns:
            {"score": -50, "tags": ["earnings_warning"], "items": 3} or None

        P1-4 (2026-05-29 리뷰): LLM 호출이 daily_call_budget를 우회하지 않도록
        budget 체크 + call count 증가.
        """
        # 캐시 체크
        cached = self._symbol_cache.get(symbol)
        if cached:
            data, ts = cached
            if datetime.now() - ts < self._SYMBOL_TTL:
                return data

        # P1-4: 예산 체크 (50회/agent 상한)
        if not self._check_budget():
            logger.debug(f"[뉴스큐레이터] 예산 초과 — {symbol} sentiment 조회 skip")
            return None

        try:
            items = await self._fetch_symbol_news(symbol)
            if not items:
                result = {"score": 0, "tags": [], "items": 0}
            else:
                await self._classify_batch(items)
                self._inc_call_count()  # LLM 분류 1회 카운트
                scores = [i.sentiment for i in items if i.sentiment != 0]
                avg = int(sum(scores) / len(scores)) if scores else 0
                tags = sorted(set(t for it in items for t in it.event_tags))
                result = {"score": avg, "tags": tags, "items": len(items)}

            self._symbol_cache[symbol] = (result, datetime.now())
            return result
        except Exception as e:
            logger.debug(f"[뉴스큐레이터] {symbol} sentiment 조회 실패: {e}")
            return None

    # ─────────────────────────────────────────
    # 수집 — KR (네이버)
    # ─────────────────────────────────────────
    async def _fetch_kr_market_news(self) -> List[NewsItem]:
        if not self._naver_id:
            return []
        out: List[NewsItem] = []
        session = await self._get_session()
        for query in self._KR_QUERIES:
            try:
                async with session.get(
                    "https://openapi.naver.com/v1/search/news.json",
                    headers={
                        "X-Naver-Client-Id": self._naver_id,
                        "X-Naver-Client-Secret": self._naver_secret,
                    },
                    params={"query": query, "display": 5, "sort": "date"},
                ) as resp:
                    if resp.status != 200:
                        continue
                    data = await resp.json()
                    for it in data.get("items", []):
                        out.append(NewsItem(
                            title=_HTML_TAG_RE.sub("", it.get("title", "")),
                            summary=_HTML_TAG_RE.sub("", it.get("description", "")),
                            url=it.get("link", ""),
                            source="naver",
                        ))
            except Exception as e:
                logger.debug(f"[뉴스큐레이터] 네이버 '{query}' 실패: {e}")
        return out

    async def _fetch_symbol_news(self, symbol: str) -> List[NewsItem]:
        """종목별 뉴스 수집 (KR: 네이버, US: Finnhub)"""
        items: List[NewsItem] = []
        # KR 종목코드 (6자리 숫자)
        if symbol.isdigit() and len(symbol) == 6:
            items.extend(await self._naver_symbol(symbol))
        else:
            items.extend(await self._finnhub_symbol(symbol))
        return items

    async def _naver_symbol(self, code: str) -> List[NewsItem]:
        if not self._naver_id:
            return []
        try:
            session = await self._get_session()
            async with session.get(
                "https://openapi.naver.com/v1/search/news.json",
                headers={
                    "X-Naver-Client-Id": self._naver_id,
                    "X-Naver-Client-Secret": self._naver_secret,
                },
                params={"query": code, "display": 10, "sort": "date"},
            ) as resp:
                if resp.status != 200:
                    return []
                data = await resp.json()
                return [
                    NewsItem(
                        title=_HTML_TAG_RE.sub("", it.get("title", "")),
                        summary=_HTML_TAG_RE.sub("", it.get("description", "")),
                        url=it.get("link", ""),
                        source="naver",
                        related_symbols=[code],
                    )
                    for it in data.get("items", [])
                ]
        except Exception as e:
            logger.debug(f"[뉴스큐레이터] naver {code} 실패: {e}")
            return []

    async def _finnhub_symbol(self, symbol: str) -> List[NewsItem]:
        if not self._finnhub_key:
            return []
        try:
            session = await self._get_session()
            today = datetime.now().date()
            week_ago = today - timedelta(days=3)
            async with session.get(
                "https://finnhub.io/api/v1/company-news",
                params={
                    "symbol": symbol,
                    "from": week_ago.isoformat(),
                    "to": today.isoformat(),
                    "token": self._finnhub_key,
                },
            ) as resp:
                if resp.status != 200:
                    return []
                data = await resp.json()
                return [
                    NewsItem(
                        title=it.get("headline", ""),
                        summary=it.get("summary", "")[:300],
                        url=it.get("url", ""),
                        source="finnhub",
                        related_symbols=[symbol],
                    )
                    for it in (data or [])[:10]
                ]
        except Exception as e:
            logger.debug(f"[뉴스큐레이터] finnhub {symbol} 실패: {e}")
            return []

    # ─────────────────────────────────────────
    # 수집 — 글로벌 속보 (Perplexity)
    # ─────────────────────────────────────────
    async def _fetch_global_speculative(self) -> List[NewsItem]:
        if not self.perplexity_key:
            return []
        text = await self._perplexity_search(
            "오늘 글로벌 금융시장에 영향을 줄 주요 속보 3가지를 각각 한 줄로 요약하세요. "
            "(미국/중국/유럽 거시, 원자재, 정책 위주)",
            max_tokens=350,
        )
        if not text:
            return []
        items: List[NewsItem] = []
        for line in text.split("\n"):
            line = line.strip(" -•*0123456789.")
            if len(line) < 10:
                continue
            items.append(NewsItem(
                title=line[:120],
                summary=line,
                url="",
                source="perplexity",
            ))
        return items[:5]

    # ─────────────────────────────────────────
    # 중복 제거 (Jaccard ≥ 0.6)
    # ─────────────────────────────────────────
    def _deduplicate(self, items: List[NewsItem]) -> List[NewsItem]:
        if len(items) <= 1:
            return items
        kept: List[NewsItem] = []
        kept_fps: List[Set[str]] = []
        for it in items:
            fp = it.fingerprint_tokens()
            is_dup = False
            for prev_fp in kept_fps:
                union = fp | prev_fp
                if not union:
                    continue
                jac = len(fp & prev_fp) / len(union)
                if jac >= 0.6:
                    is_dup = True
                    break
            if not is_dup:
                kept.append(it)
                kept_fps.append(fp)
        return kept

    # ─────────────────────────────────────────
    # LLM 일괄 분류
    # ─────────────────────────────────────────
    async def _classify_batch(self, items: List[NewsItem]) -> None:
        """배치 1회 호출로 모든 아이템 분류 — Gemini Flash Lite 권장"""
        if not items or self.llm_manager is None:
            return

        # 너무 많으면 상위 20개만
        batch = items[:20]
        lines = [f"{i}: {it.title}" for i, it in enumerate(batch)]
        prompt = (
            "다음 뉴스 헤드라인 각각에 대해 sentiment(-100~+100)와 "
            "event tag(어닝/M&A/리콜/소송/규제/공시/거시/기타 중 1개)를 JSON으로 분류하세요.\n\n"
            + "\n".join(lines)
            + "\n\n응답 예시:\n"
            '{"results": [{"i": 0, "s": 20, "t": "거시"}, {"i": 1, "s": -50, "t": "규제"}]}'
        )
        try:
            from src.utils.llm import LLMTask
            resp = await self.llm_manager.complete_json(
                prompt,
                task=LLMTask.QUICK_CLASSIFY,
                system="당신은 금융 뉴스 분류 전문가입니다.",
            )
        except Exception as e:
            logger.debug(f"[뉴스큐레이터] LLM 분류 실패: {e}")
            return

        if not isinstance(resp, dict):
            logger.warning(f"[뉴스큐레이터] LLM 응답이 dict 아님: {type(resp).__name__}")
            return
        # P1-5 (2026-05-29 리뷰): 응답 키 누락 시 warning
        if "results" not in resp:
            logger.warning(
                f"[뉴스큐레이터] LLM 응답에 'results' 키 누락. keys={list(resp.keys())[:5]}"
            )
            return
        for r in resp.get("results", []):
            try:
                idx = int(r.get("i", -1))
                if 0 <= idx < len(batch):
                    batch[idx].sentiment = max(-100, min(100, int(r.get("s", 0))))
                    tag = str(r.get("t", "")).strip()
                    if tag:
                        batch[idx].event_tags = [tag]
            except (ValueError, TypeError):
                continue

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
