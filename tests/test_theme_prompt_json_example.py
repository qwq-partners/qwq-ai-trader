import json

import pytest

from src.signals.sentiment.kr_theme_detector import NewsArticle, ThemeDetector
from src.utils.llm import LLMTask


class CapturingLLM:
    def __init__(self):
        self.prompt = None

    async def complete_json(self, prompt, *, task):
        assert task == LLMTask.THEME_DETECTION
        self.prompt = prompt
        return {"themes": [], "stock_impacts": []}


@pytest.mark.asyncio
async def test_theme_prompt_example_is_valid_json_with_numeric_fields():
    llm = CapturingLLM()
    detector = ThemeDetector(llm_manager=llm)
    result = await detector._extract_themes_from_news(
        [NewsArticle(title="합성 뉴스", content="테스트 요약")]
    )
    assert result == {"themes": [], "stock_impacts": []}
    example = llm.prompt.split("다음 JSON 형식으로만 응답하세요:", 1)[1].split(
        "\n\n규칙:", 1
    )[0]
    payload = json.loads(example)
    assert set(payload) == {"themes", "stock_impacts"}
    assert 1 <= len(payload["themes"]) <= 5
    assert 1 <= len(payload["stock_impacts"]) <= 10

    stocks = []
    for theme in payload["themes"]:
        assert set(theme) == {"theme", "news_count", "score", "reason", "stocks"}
        assert type(theme["news_count"]) is int and theme["news_count"] >= 2
        assert type(theme["score"]) in (int, float) and 0 <= theme["score"] <= 100
        assert 1 <= len(theme["stocks"]) <= 5
        for stock in theme["stocks"]:
            assert set(stock) == {"symbol", "name", "impact", "direction"}
            stocks.append(stock)

    for stock in payload["stock_impacts"]:
        assert set(stock) == {"symbol", "name", "impact", "direction", "catalyst_phase", "reason"}
        assert stock["catalyst_phase"] in {"rumor", "confirmed", "unknown"}
        stocks.append(stock)

    for stock in stocks:
        impact = stock["impact"]
        assert type(impact) in (int, float) and -10 <= impact <= 10
        assert impact != 0
        assert stock["direction"] == ("bullish" if impact > 0 else "bearish")
    assert {stock["direction"] for stock in stocks} == {"bullish", "bearish"}
