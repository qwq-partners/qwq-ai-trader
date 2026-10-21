"""News briefing classification coverage and evidence regressions."""

import asyncio
from datetime import datetime

import pytest

from src.experts.news_curator import NewsCurator, NewsItem
from src.experts.types import ExpertConfig


class _LLM:
    def __init__(self, response):
        self.response = response
        self.calls = 0

    async def complete_json(self, *args, **kwargs):
        self.calls += 1
        self.last_prompt = args[0]
        return self.response


def _item(index, url=None):
    return NewsItem(
        title=f"제목{index}", summary=f"근거{index}",
        url=url or f"https://news.example/{index}", source="test",
        fetched_at=datetime(2026, 10, 21, 9, 30),
    )


def _curator(response, kr_items, global_items=()):
    curator = NewsCurator(ExpertConfig(), llm_manager=_LLM(response))

    async def _kr():
        return list(kr_items)

    async def _global():
        return list(global_items)

    curator._fetch_kr_market_news = _kr
    curator._fetch_global_speculative = _global
    return curator


def test_classified_neutral_is_counted_in_mean_and_evidence():
    """Dropping valid zero classifications changes this three-item mean from +10 to +15."""
    curator = _curator(
        {"results": [
            {"i": 0, "s": 0, "t": "기타"},
            {"i": 1, "s": 30, "t": "거시"},
            {"i": 2, "s": 0, "t": "공시"},
        ]},
        [_item(i) for i in range(3)],
    )

    opinion = asyncio.run(curator._analyze())
    raw = opinion.raw_evidence

    assert opinion.score == 10
    assert raw["classified_count"] == 3
    assert raw["neutral_count"] == 2
    assert raw["unclassified_count"] == 0
    assert raw["classification_coverage"] == 1.0
    assert raw["articles"][0]["sentiment"] == 0
    assert raw["articles"][0]["status"] == "neutral"
    assert opinion.data_status == "ok"
    assert opinion.confidence == pytest.approx(0.9)


def test_max_twenty_classifications_report_partial_coverage_for_twenty_five_items():
    """A change that reports the unclassified tail as covered must fail this test."""
    curator = _curator(
        {"results": [{"i": i, "s": 1, "t": "거시"} for i in range(20)]},
        [_item(i) for i in range(25)],
    )

    opinion = asyncio.run(curator._analyze())
    raw = opinion.raw_evidence

    assert raw["item_count"] == 25
    assert raw["classified_count"] == 20
    assert raw["unclassified_count"] == 5
    assert raw["classification_coverage"] == pytest.approx(0.8)
    assert len(raw["articles"]) == 25
    assert raw["articles"][-1]["classified"] is False
    assert raw["articles"][-1]["sentiment"] is None
    assert opinion.data_status == "partial"
    # Coverage produces 0.72 before ExpertAgent's shared partial-data cap (0.7).
    assert opinion.confidence == pytest.approx(0.7)
    assert curator.llm_manager.calls == 1


def test_all_classification_failures_are_insufficient_not_confident():
    """A malformed classifier response must leave every item explicitly unclassified."""
    curator = _curator({"not_results": []}, [_item(i) for i in range(3)])

    opinion = asyncio.run(curator._analyze())
    raw = opinion.raw_evidence

    assert opinion.score == 0
    assert raw["classified_count"] == 0
    assert raw["unclassified_count"] == 3
    assert raw["classification_coverage"] == 0.0
    assert all(article["status"] == "unclassified" for article in raw["articles"])
    assert opinion.data_status == "insufficient"
    assert opinion.confidence == 0.0


@pytest.mark.parametrize("response", [
    {"results": [{"i": 0, "s": float("nan"), "t": "거시"}]},
    {"results": "not-a-list"},
    {"results": [{"s": 20, "t": "거시"}]},
    {"results": [{"i": 0, "t": "거시"}]},
])
def test_invalid_classifier_records_do_not_turn_into_neutral_evidence(response):
    """Invalid scores or missing indexes/scores are unclassified, never valid zero sentiment."""
    curator = _curator(response, [_item(0)])

    opinion = asyncio.run(curator._analyze())

    assert opinion.raw_evidence["classified_count"] == 0
    assert opinion.raw_evidence["articles"][0]["status"] == "unclassified"


def test_market_path_deduplicates_same_url_and_keeps_title_jaccard_deduplication():
    """Same URL duplicates must be removed even when their title tokens are dissimilar."""
    curator = _curator(
        {"results": [{"i": 0, "s": 20, "t": "거시"}]},
        [
            NewsItem("반도체 이익 급증", "회복", "https://news.example/dup", "test"),
            NewsItem("전혀 다른 제목", "시장 전망", "https://news.example/dup", "test"),
        ],
    )

    opinion = asyncio.run(curator._analyze())

    assert opinion.raw_evidence["item_count"] == 1
    assert opinion.raw_evidence["articles"][0]["url"] == "https://news.example/dup"


def test_market_path_retains_title_jaccard_deduplication_for_distinct_urls():
    """A duplicate headline must still collapse when syndication gives it a new URL."""
    curator = _curator({"results": []}, [])

    remaining = curator._deduplicate([
        NewsItem("반도체 실적 개선 전망", "수요 회복 기대", "https://news.example/a", "test"),
        NewsItem("반도체 실적 개선 전망", "수요 회복 기대", "https://news.example/b", "test"),
    ])

    assert len(remaining) == 1


def test_representative_findings_are_ranked_by_absolute_market_contribution():
    """An early neutral ELW headline must not displace a later material market item."""
    curator = _curator(
        {"results": [
            {"i": 0, "s": 0, "t": "공시"},
            {"i": 1, "s": 15, "t": "거시"},
            {"i": 2, "s": -70, "t": "규제"},
        ]},
        [
            NewsItem("ELW 발행 공시", "ELW 발행", "https://news.example/elw", "test"),
            NewsItem("완만한 개선", "경기", "https://news.example/small", "test"),
            NewsItem("중대한 규제 충격", "정책", "https://news.example/large", "test"),
        ],
    )

    opinion = asyncio.run(curator._analyze())

    assert opinion.key_findings[0].startswith("중대한 규제 충격")
    assert all("ELW 발행" not in finding for finding in opinion.key_findings)


def test_symbol_classifier_targets_company_risk_instead_of_market_direction(monkeypatch):
    curator = _curator({"results": [{"i": 0, "s": -60, "t": "리콜"}]}, [])
    async def fetch(symbol):
        return [NewsItem("단일 회사 제품 리콜", "회사 손실", "https://news.example/recall", "test")]
    monkeypatch.setattr(curator, "_fetch_symbol_news", fetch)
    result = asyncio.run(curator.get_symbol_sentiment("005930"))
    assert result["score"] == -60
    assert "대상 종목 005930" in curator.llm_manager.last_prompt
    assert "한국/글로벌 주식시장 방향" not in curator.llm_manager.last_prompt


def test_market_representative_article_provenance_matches_displayed_finding():
    curator = _curator({"results": [{"i": 0, "s": 0}, {"i": 1, "s": -70}]},
                       [_item(0), _item(1)])
    opinion = asyncio.run(curator._analyze())
    representative = opinion.raw_evidence["representative_articles"][0]
    assert opinion.key_findings[0].startswith(representative["title"])
    assert representative["url"] == "https://news.example/1"
    assert representative["fetched_at"] == "2026-10-21T09:30:00"
