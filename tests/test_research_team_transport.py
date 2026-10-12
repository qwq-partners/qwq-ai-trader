"""Exercise both research roles through actual provider payload construction."""

import pytest

from src.agents import researchers
from src.utils.llm import LLMConfig, LLMManager


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
async def test_both_roles_reach_http_and_ledger_records_provider_specific_controls(monkeypatch):
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
    result = await researchers.ResearchTeam(manager, rounds=1).debate("000001", "synthetic", [])
    assert result.bull_final is True
    assert result.bear_final is True
    assert len(manager.openai._session.payloads) == len(manager.gemini._session.payloads) == 1
    assert manager.openai._session.payloads[0]["reasoning_effort"] == "minimal"
    assert "thinkingConfig" not in manager.gemini._session.payloads[0]["generationConfig"]
    assert manager.gemini._session.payloads[0]["generationConfig"]["temperature"] == 0.0
    assert rows[0]["params"]["reasoning_effort"] == "minimal"
    assert "reasoning_effort" not in rows[1]["params"]
    assert "seed" not in rows[1]["params"]
    assert rows[1]["params"]["temperature"] == 0.0
