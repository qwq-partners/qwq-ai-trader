"""A prohibited provider must not turn into a one-sided entry approval."""
import asyncio
from datetime import datetime
from types import SimpleNamespace

import pytest

from src.core import adversarial_validator as av
from src.core.cross_validator import CrossStrategyValidator
from src.utils.llm import LLMProvider, LLMResponse


class ProviderReplies:
    def __init__(self, disabled=LLMProvider.ANTHROPIC, other="approve"):
        self.disabled = disabled
        self.other = other
        self.disabled_ready = asyncio.Event()
        self.fallback_calls = 0

    async def complete_with(self, prompt, *, provider, **kwargs):
        if provider == self.disabled:
            self.disabled_ready.set()
            return LLMResponse(
                "", "retired-model", provider, success=False,
                provider_error_code="provider_disabled", request_sent=False,
                billing_status="not_sent", usage_known=False,
                error="Provider disabled by user policy")
        await self.disabled_ready.wait()
        if self.other == "timeout":
            await asyncio.Event().wait()
        if self.other == "error":
            raise RuntimeError("upstream unavailable")
        content = "APPROVE 근거" if provider == LLMProvider.OPENAI else "ACCEPT 근거"
        return LLMResponse(content, "model", provider, usage_known=True)

    async def complete(self, *args, **kwargs):
        self.fallback_calls += 1
        return LLMResponse("YES", "gpt-5-mini", LLMProvider.OPENAI)


@pytest.mark.parametrize("disabled", [LLMProvider.ANTHROPIC, LLMProvider.OPENAI])
@pytest.mark.parametrize("other", ["approve", "error", "timeout"])
@pytest.mark.asyncio
async def test_disabled_provider_blocks_even_if_peer_approves_or_times_out(monkeypatch, disabled, other):
    monkeypatch.setattr(av, "LLM_TIMEOUT", 0.03)
    validator = av.AdversarialValidator(ProviderReplies(disabled, other))
    result = await validator.validate("005930", "momentum", 90, {}, "sideways", common_context="ctx")
    assert result.approved is False
    assert result.failed is False  # policy block is final, not a request for paid fallback
    assert result.confidence == 0.0
    assert "독립 검토 미완료" in result.reason
    assert result.meta["independent_review_status"] == "provider_disabled"
    assert len(result.meta["attempts"]) == 2
    assert validator.stats["blocked"] == 1
    assert validator.stats["approved"] == 0


@pytest.mark.asyncio
async def test_cross_validator_cannot_override_disabled_provider_with_single_gpt(monkeypatch):
    from src.data.providers import disclosure_feed
    llm = ProviderReplies()
    cross = CrossStrategyValidator(
        trade_memory=SimpleNamespace(get_context_for_signal=lambda *a: ""),
        trade_wiki=SimpleNamespace(query=lambda *a: "", query_symbol=lambda *a: ""))
    cross._llm_manager = llm
    cross._adversarial = av.AdversarialValidator(llm)
    monkeypatch.setattr(cross, "_load_panel_outlook", lambda: None)
    monkeypatch.setattr(disclosure_feed, "get_symbol_disclosure_context", lambda *a: "")
    cross._panel_risk_factors, cross._panel_regime = [], "sideways"
    result = await cross.llm_second_check("005930", "momentum", 90, {}, "sideways")
    assert result is False
    assert llm.fallback_calls == 0
    assert cross.last_llm_decision["stage"] == "adversarial"


@pytest.mark.parametrize("cap,already_used", [(0, 0), (10, 10), (10, 0)])
@pytest.mark.asyncio
async def test_provider_policy_block_precedes_daily_cap_and_consumes_no_calls(monkeypatch, cap, already_used):
    manager = ProviderReplies()
    manager.requires_independent_review = True
    manager.provider_policy_block_reason = lambda provider: "provider_disabled" if provider == LLMProvider.ANTHROPIC else None
    cross = CrossStrategyValidator()
    cross._llm_manager = manager
    cross._adversarial = av.AdversarialValidator(manager)
    cross._daily_llm_max, cross._daily_llm_count = cap, already_used
    cross._daily_llm_count_date = datetime.now().date()
    results = [await cross.llm_second_check("005930", "momentum", 90, {}, "sideways")
               for _ in range(6)]
    assert results == [False] * 6
    assert cross._daily_llm_count == already_used
    assert manager.fallback_calls == 0
    assert not manager.disabled_ready.is_set()
    assert cross.last_llm_decision["stage"] == "provider_policy"
    assert cross.last_llm_decision["attempts"] == 0
