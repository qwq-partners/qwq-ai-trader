"""Exercise active Bull transport and the unavailable Claude Bear unsent contract."""

import pytest

from src.agents import researchers
from src.utils.llm import LLMConfig, LLMManager, LLMProvider, LLMResponse


class Response:
    status = 200

    def __init__(self, data):
        self.data = data

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def json(self):
        return self.data


class Session:
    closed = False

    def __init__(self, data):
        self.data = data
        self.payloads = []

    def post(self, url, **kwargs):
        self.payloads.append(kwargs["json"])
        return Response(self.data)


@pytest.mark.asyncio
async def test_only_bull_reaches_http_when_claude_unavailable_cannot_form_consensus(monkeypatch):
    rows = []

    class Ledger:
        def record(self, **kwargs):
            rows.append(kwargs)

    monkeypatch.setattr(researchers, "get_ledger", Ledger)
    manager = LLMManager(LLMConfig(openai_api_key="test", gemini_api_key="test"))
    manager.openai._session = Session({
        "choices": [{"message": {"content": "APPROVE evidence"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 20},
    })
    manager.gemini._session = Session({
        "candidates": [{"content": {"parts": [{"text": "ACCEPT evidence"}]}, "finishReason": "STOP"}],
        "usageMetadata": {"promptTokenCount": 100, "candidatesTokenCount": 20},
    })
    real_complete_with = manager.complete_with
    async def offline_complete_with(prompt, *, provider, **kwargs):
        if provider == LLMProvider.ANTHROPIC:
            return LLMResponse("", "claude-opus-5-5", provider, success=False,
                               request_sent=False, billing_status="not_sent",
                               error="offline unavailable")
        return await real_complete_with(prompt, provider=provider, **kwargs)
    monkeypatch.setattr(manager, "complete_with", offline_complete_with)
    result = await researchers.ResearchTeam(manager, rounds=1).debate("000001", "synthetic", [])
    assert result.bull_final is True
    assert result.bear_final is None and result.consensus is None
    assert len(manager.openai._session.payloads) == 1
    assert manager.gemini._session.payloads == []
    assert manager.openai._session.payloads[0]["reasoning_effort"] == "minimal"
    assert rows[0]["request_sent"] is True and rows[0]["verdict"] is True
    assert rows[1]["provider"] == "anthropic" and rows[1]["role"] == "bear"
    assert rows[1]["request_sent"] is False and rows[1]["billing_status"] == "not_sent"
    assert rows[1]["success"] is False and rows[1]["verdict"] is None
    assert rows[1]["error"] == "offline unavailable"
    assert rows[1]["response"] == ""
    assert rows[0]["params"]["reasoning_effort"] == "minimal"
    assert rows[1]["params"]["reasoning_effort"] == "low"
    assert "seed" not in rows[1]["params"]
    assert "temperature" not in rows[1]["params"]
