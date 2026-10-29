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

@pytest.mark.asyncio
async def test_only_team_claude_opts_in_to_api_cap(monkeypatch):
    monkeypatch.setattr(researchers, "get_ledger", lambda: None)
    calls = []
    class Manager:
        async def complete_with(self, prompt, **kwargs):
            calls.append(kwargs)
            return LLMResponse('ACCEPT', 'synthetic', kwargs['provider'])
    team = researchers.ResearchTeam(Manager(), rounds=1)
    await team._ask('synthetic', 'rules', LLMProvider.ANTHROPIC)
    await team._ask('synthetic', 'rules', LLMProvider.OPENAI)
    assert calls[0]['max_tokens'] == 400 and calls[0]['api_max_tokens'] == 800
    assert calls[1]['max_tokens'] == 400 and 'api_max_tokens' not in calls[1]

@pytest.mark.asyncio
@pytest.mark.parametrize('stop,content', [('max_tokens', [{'type': 'text', 'text': 'ACCEPT'}]),
                                          ('end_turn', [])])
async def test_incomplete_api_800_leaves_bear_unknown_and_buy_on_hold(monkeypatch, tmp_path, stop, content):
    from src.agents.trader import TraderAgent, Stance
    from src.agents.analysts import AnalystTeam
    from src.utils.claude_transport import AnthropicAPIClient
    rows = []
    class Ledger:
        def record(self, **kwargs):
            rows.append(kwargs)
    class CLI:
        async def complete(self, *args, **kwargs):
            return LLMResponse('', 'claude-opus-5-5', LLMProvider.ANTHROPIC,
                               success=False, transport_error_kind='cli_timeout',
                               request_sent=False, usage_known=False)
        async def close(self):
            pass
    monkeypatch.setattr(researchers, 'get_ledger', Ledger)
    monkeypatch.setattr(AnalystTeam, 'aggregate_score', lambda reports: 80)
    monkeypatch.setattr(AnalystTeam, 'evidence_quality', lambda reports: (True, '', 1.0))
    manager = LLMManager(LLMConfig(openai_api_key='synthetic', anthropic_api_key='synthetic',
                                   monthly_budget_usd=100, daily_budget_usd=100,
                                   budget_state_path=str(tmp_path / 'budget.sqlite3')))
    manager.openai._session = Session({
        'choices': [{'message': {'content': 'APPROVE evidence'}, 'finish_reason': 'stop'}],
        'usage': {'prompt_tokens': 100, 'completion_tokens': 20}})
    manager.claude_cli = CLI()
    manager.anthropic = AnthropicAPIClient(manager.config)
    manager.anthropic._session = Session({
        'model': 'claude-opus-5-5', 'stop_reason': stop, 'content': content,
        'usage': {'input_tokens': 100, 'output_tokens': 800}})
    result = await researchers.ResearchTeam(manager, rounds=1).debate('S', '', [])
    proposal = TraderAgent().propose('S', '', [], result)
    assert result.bull_final is True and result.bear_final is None and result.consensus is None
    assert proposal.stance == Stance.HOLD
    bear = next(row for row in rows if row['role'] == 'bear')
    api = bear['attempt_trail'][-1]
    assert api['requested_max_tokens'] == 800 and api['finish_reason'] == stop
    assert api['usage_known'] and api['output_tokens'] == 800 and not api['success']
    assert len(manager.anthropic._session.payloads) == 1
    await manager.close()
