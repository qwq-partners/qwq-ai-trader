"""Offline contract tests for independent Claude review and retained evidence."""
import asyncio
from types import SimpleNamespace

import pytest

from src.agents import researchers, reproducibility
from src.core import adversarial_validator as av
from src.core.cross_validator import CrossStrategyValidator
from src.utils.llm import LLMProvider, LLMResponse


@pytest.fixture(autouse=True)
def isolate_evidence(monkeypatch, tmp_path):
    from src.analytics import llm_decision_record
    monkeypatch.setattr(reproducibility, "LEDGER_DIR", tmp_path / "repro")
    monkeypatch.setattr(reproducibility, "_ledger", None)
    monkeypatch.setattr(llm_decision_record, "get_decision_recorder", lambda: SimpleNamespace(submit=lambda record: "written"))


def reply(provider, content=None, **kwargs):
    return LLMResponse(content if content is not None else (
        "APPROVE evidence" if provider == LLMProvider.OPENAI else "ACCEPT risk"),
        "requested", provider, **kwargs)


class ReviewManager:
    requires_independent_review = True

    def __init__(self, failure=None, failed_provider=LLMProvider.ANTHROPIC):
        self.failure = failure
        self.failed_provider = failed_provider
        self.calls = []
        self.fallback_calls = 0

    async def complete_with(self, prompt, *, provider, **kwargs):
        self.calls.append((provider, kwargs))
        if provider == self.failed_provider and self.failure:
            if self.failure == "exception":
                raise RuntimeError("unavailable")
            if self.failure == "timeout":
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError as error:
                    error._llm_response = reply(provider, "", success=False, transport="claude_cli", billing_channel="subscription")
                    error._llm_trail = [{"transport": "claude_cli", "billing_channel": "subscription", "status": "cancelled"}]
                    raise
            return reply(provider, "uncertain" if self.failure == "unparseable" else "", success=self.failure == "unparseable", provider_error_code=self.failure)
        return reply(provider, transport="anthropic_messages" if provider == LLMProvider.ANTHROPIC else "openai_chat",
                     requested_model="claude-opus-5-5" if provider == LLMProvider.ANTHROPIC else "gpt-5-mini",
                     resolved_model="claude-opus-5-5" if provider == LLMProvider.ANTHROPIC else "gpt-5-mini",
                     attempt_trail=[{"transport": "claude_cli", "billing_channel": "subscription", "status": "timeout"},
                                    {"transport": "anthropic_messages", "billing_channel": "api", "status": "success"}])

    async def complete(self, *args, **kwargs):
        self.fallback_calls += 1
        return reply(LLMProvider.OPENAI, "YES")


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['refusal', 'identity_mismatch', 'policy_violation'])
@pytest.mark.parametrize('provider', [LLMProvider.OPENAI, LLMProvider.ANTHROPIC])
async def test_forbidden_review_failure_stops_debate_after_recording_both_sides(failure, provider):
    class Manager(ReviewManager):
        async def complete_with(self, prompt, *, provider, **kwargs):
            response = await super().complete_with(prompt, provider=provider, **kwargs)
            if provider == failed_provider and len([p for p, _ in self.calls if p == provider]) == 1:
                response.success = False
                response.refusal_detected = failure == 'refusal'
                response.transport_error_kind = failure
            return response
    failed_provider = provider
    manager = Manager()
    result = await researchers.ResearchTeam(manager, rounds=2).debate('S', '', [])
    assert result.consensus is not True and result.rounds_run == 1
    assert len(manager.calls) == 2
    rows = reproducibility.LLMLedger.load()
    assert len(rows) == 2
    failed = next(row for row in rows if row['provider'] == provider.value)
    assert failed['transport_error_kind'] == failure
    assert failed['refusal_detected'] == (failure == 'refusal')


@pytest.mark.asyncio
async def test_both_bears_request_claude_with_bounded_low_effort():
    manager = ReviewManager()
    result = await av.AdversarialValidator(manager).validate("S", "sepa", 90, {}, "sideways")
    team = await researchers.ResearchTeam(manager, rounds=1).debate("S", "", [])
    assert result.approved and team.consensus
    bears = [kwargs for provider, kwargs in manager.calls if provider == LLMProvider.ANTHROPIC]
    assert len(bears) == 2
    assert [call["call_timeout_seconds"] for call in bears] == [11.5, 19.5]
    for call in bears:
        assert call["reasoning_effort"] == "low" and call["retry_on_empty"] == 0
        assert call["max_tokens"] == 400
        assert "seed" not in call and "temperature" not in call
    rows = reproducibility.LLMLedger.load()
    bear = next(row for row in rows if row["role"] == "bear")
    assert bear["transport"] == "anthropic_messages" and bear["billing_channel"] == "api"
    assert bear["requested_model"] == bear["resolved_model"] == "claude-opus-5-5"
    assert [row["transport"] for row in bear["attempt_trail"]] == ["claude_cli", "anthropic_messages"]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["exception", "timeout", "unparseable", "refusal", "model_identity_mismatch", "budget_blocked"])
@pytest.mark.parametrize("failed_provider", [LLMProvider.OPENAI, LLMProvider.ANTHROPIC])
async def test_missing_either_review_holds_without_requesting_single_fallback(monkeypatch, failure, failed_provider):
    monkeypatch.setattr(av, "LLM_TIMEOUT", .03)
    manager = ReviewManager(failure, failed_provider)
    result = await av.AdversarialValidator(manager).validate("S", "sepa", 90, {}, "sideways")
    assert result.approved is False and result.failed is False
    assert result.meta["independent_review_status"] == "unavailable"
    assert len(result.meta["attempts"]) == 2
    assert manager.fallback_calls == 0
    if failure == "timeout":
        completed = next(row for row in result.meta["attempts"] if row["provider"] != failed_provider.value)
        assert completed["outcome"] == "success"


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["daily_cap", "missing_validator", "validator_exception", "invalid_response"])
async def test_cross_validator_holds_required_review_unavailability(monkeypatch, mode):
    from src.data.providers import disclosure_feed
    manager = ReviewManager("refusal")
    cross = CrossStrategyValidator(trade_memory=SimpleNamespace(get_context_for_signal=lambda *a: ""),
                                   trade_wiki=SimpleNamespace(query=lambda *a: "", query_symbol=lambda *a: ""))
    cross._llm_manager = manager
    cross._adversarial = av.AdversarialValidator(manager)
    monkeypatch.setattr(cross, "_load_panel_outlook", lambda: None)
    monkeypatch.setattr(disclosure_feed, "get_symbol_disclosure_context", lambda *a: "")
    if mode == "daily_cap":
        cross._daily_llm_max = 0
    elif mode == "missing_validator":
        cross._adversarial = None
    elif mode == "validator_exception":
        async def fail(**kwargs):
            raise RuntimeError("unavailable")
        cross._adversarial.validate = fail
    captured = []
    def capture(approved, **kwargs):
        captured.append(kwargs)
        return approved
    monkeypatch.setattr(cross, "_finish_decision", capture)
    assert await cross.llm_second_check("S", "sepa", 90, {}, "sideways") is False
    assert manager.fallback_calls == 0
    assert captured[-1]["result"]["independent_review_status"] == "unavailable"
    assert captured[-1]["result"]["failed"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("required", [False, "yes", 1, SimpleNamespace()])
async def test_legacy_manager_daily_cap_retains_advisory_contract(required):
    cross = CrossStrategyValidator()
    cross._llm_manager = SimpleNamespace(requires_independent_review=required)
    cross._daily_llm_max = 0
    assert await cross.llm_second_check("S", "sepa", 90, {}, "sideways") is True


@pytest.mark.asyncio
async def test_required_review_does_not_change_ineligible_or_no_manager_paths():
    cross = CrossStrategyValidator()
    assert await cross.llm_second_check("S", "sepa", 90, {}, "sideways") is True
    cross._llm_manager = ReviewManager()
    assert await cross.llm_second_check("S", "sepa", 90, {}, "bull") is True
    assert await cross.llm_second_check("S", "sepa", 84, {}, "sideways") is True
    assert cross._llm_manager.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["timeout", "empty", "exception"])
async def test_round_two_failure_records_sibling_and_cannot_reuse_prior_vote(monkeypatch, failure):
    monkeypatch.setattr(researchers, "ROUND_TIMEOUT", .03)
    class TwoRounds(ReviewManager):
        def __init__(self):
            super().__init__()
            self.rounds = {}
        async def complete_with(self, prompt, *, provider, **kwargs):
            self.rounds[provider] = self.rounds.get(provider, 0) + 1
            if provider == LLMProvider.ANTHROPIC and self.rounds[provider] == 2:
                self.failure = failure
            return await super().complete_with(prompt, provider=provider, **kwargs)
    team = researchers.ResearchTeam(TwoRounds(), rounds=2)
    result = await team.debate("S", "", [])
    assert result.consensus is None and result.bull_final is True and result.bear_final is None
    assert result.rounds_run == 2 and len(result.turns) == 4
    rows = reproducibility.LLMLedger.load()
    assert len(rows) == 4
    assert rows[2]["success"] is True and rows[2]["verdict"] is True
    assert rows[3]["success"] is False and rows[3]["verdict"] is None
    assert rows[3]["provider"] == "anthropic"
    if failure == "timeout":
        assert rows[3]["error"] == "timeout"
        assert rows[3]["transport"] == "claude_cli"
        assert rows[3]["attempt_trail"][0]["status"] == "cancelled"


@pytest.mark.asyncio
async def test_required_cross_check_rejects_one_sided_legacy_validator_result(monkeypatch):
    from src.data.providers import disclosure_feed
    cross = CrossStrategyValidator()
    cross._llm_manager = ReviewManager()
    async def one_sided(**kwargs):
        return av.AdversarialResult(True, .5, bull_verdict=True, bear_verdict=None,
                                    meta={"attempts": [{"role": "bull", "outcome": "success"}]})
    cross._adversarial = SimpleNamespace(validate=one_sided)
    monkeypatch.setattr(cross, "_load_panel_outlook", lambda: None)
    monkeypatch.setattr(disclosure_feed, "get_symbol_disclosure_context", lambda *a: "")
    captured = []
    def capture(approved, **kwargs):
        captured.append(kwargs)
        return approved
    monkeypatch.setattr(cross, "_finish_decision", capture)
    assert await cross.llm_second_check("S", "sepa", 90, {}, "sideways") is False
    assert captured[-1]["attempts"] == [{"role": "bull", "outcome": "success"}]
    assert captured[-1]["result"]["independent_review_status"] == "unavailable"


@pytest.mark.asyncio
async def test_budget_denial_is_unavailable_not_partial_review(monkeypatch):
    from src.data.providers import disclosure_feed
    manager = ReviewManager()
    async def denial(prompt, *, provider, **kwargs):
        return reply(provider, "", success=False, budget_block_reason="monthly_limit")
    manager.complete_with = denial
    cross = CrossStrategyValidator()
    cross._llm_manager = manager
    cross._adversarial = av.AdversarialValidator(manager)
    monkeypatch.setattr(cross, "_load_panel_outlook", lambda: None)
    monkeypatch.setattr(disclosure_feed, "get_symbol_disclosure_context", lambda *a: "")
    assert await cross.llm_second_check("S", "sepa", 90, {}, "sideways") is False
    assert cross.last_llm_budget_status["status"] == "unavailable"


@pytest.mark.asyncio
async def test_caller_cancellation_records_completed_sibling_and_propagates(monkeypatch):
    entered = asyncio.Event()
    class CancelledReview(ReviewManager):
        async def complete_with(self, prompt, *, provider, **kwargs):
            if provider == LLMProvider.ANTHROPIC:
                entered.set()
            return await super().complete_with(prompt, provider=provider, **kwargs)
    manager = CancelledReview("timeout")
    task = asyncio.create_task(researchers.ResearchTeam(manager).debate("S", "", []))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    rows = reproducibility.LLMLedger.load()
    assert len(rows) == 2 and rows[0]["success"] is True
    assert rows[1]["error"] == "cancelled"
    assert rows[1]["attempt_trail"][0]["status"] == "cancelled"


@pytest.mark.asyncio
async def test_anthropic_api_402_does_not_suppress_later_subscription_review():
    class ApiThenSubscription(ReviewManager):
        anthropic_calls = 0
        async def complete_with(self, prompt, *, provider, **kwargs):
            if provider == LLMProvider.ANTHROPIC:
                self.anthropic_calls += 1
                if self.anthropic_calls == 1:
                    return reply(provider, "", success=False, http_status=402,
                                 transport="anthropic_messages", billing_channel="api",
                                 error="API payment required")
                return reply(provider, transport="claude_cli", billing_channel="subscription")
            return reply(provider)
    manager = ApiThenSubscription()
    team = researchers.ResearchTeam(manager, rounds=1)
    first = await team.debate("S1", "", [])
    second = await team.debate("S2", "", [])
    assert first.bear_final is None and first.consensus is None
    assert second.bear_final is True and second.consensus is True
    assert manager.anthropic_calls == 2
    rows = reproducibility.LLMLedger.load()
    assert rows[1]["http_status"] == 402 and rows[1]["billing_channel"] == "api"
    assert rows[3]["transport"] == "claude_cli" and rows[3]["billing_channel"] == "subscription"
    assert team.get_stats()["providers"]["anthropic"]["skipped"] == 0
