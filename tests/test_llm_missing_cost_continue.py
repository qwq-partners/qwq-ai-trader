"""User-approved missing-cost policy through real manager/transport/SQLite paths."""
import json
import sqlite3

import pytest

from src.utils.llm import LLMConfig, LLMManager, LLMProvider, LLMTask
from test_llm_transport_contract import Reply, Session, gemini_reply, openai_reply


def manager_for(tmp_path, **overrides):
    cfg = dict(openai_api_key="offline", gemini_api_key="offline",
               monthly_budget_usd=100, budget_state_path=str(tmp_path / "budget.sqlite3"),
               missing_cost_policy="log_and_continue")
    cfg.update(overrides)
    manager = LLMManager(LLMConfig(**cfg), audit_dir=tmp_path / "audit")
    manager._LLM_LOG_DIR = tmp_path / "responses"
    return manager


@pytest.mark.parametrize("value", [None, True, "skip", "", [], 1])
def test_invalid_policy_rejected(value):
    with pytest.raises(ValueError, match="missing_cost_policy"):
        LLMConfig.from_config({"llm": {"missing_cost_policy": value}})


def test_explicit_policy_and_legacy_default():
    assert LLMConfig().missing_cost_policy == "block"
    assert LLMConfig.from_config({"llm": {
        "missing_cost_policy": "log_and_continue"}}).missing_cost_policy == "log_and_continue"


@pytest.mark.asyncio
async def test_old_unknown_can_continue_without_rewriting_its_cost(tmp_path):
    old = manager_for(tmp_path, missing_cost_policy="block")
    token = old.monthly_budget.reserve("gpt-5-mini", "old", "", 100)
    old.monthly_budget.settle(token, outcome="unknown")
    with sqlite3.connect(tmp_path / "budget.sqlite3") as db:
        before = db.execute("SELECT * FROM reservations WHERE token=?", (token,)).fetchone()
    manager = manager_for(tmp_path)
    manager.openai._session = Session(openai_reply())
    result = await manager.complete("news", LLMTask.MARKET_ANALYSIS, max_tokens=100)
    assert result.success and len(manager.openai._session.payloads) == 1
    with sqlite3.connect(tmp_path / "budget.sqlite3") as db:
        assert db.execute("SELECT * FROM reservations WHERE token=?", (token,)).fetchone() == before
    status = manager.get_monthly_budget_status()
    assert not status["blocked"] and status["skipped_unknown_count"] == 1
    assert status["cost_accounting_complete"] is False


@pytest.mark.asyncio
async def test_unbounded_error_falls_back_once_and_next_call_is_admitted(tmp_path):
    manager = manager_for(tmp_path)
    manager.openai._session = Session(Reply({}, status=503), openai_reply(), openai_reply())
    result = await manager.complete("news", LLMTask.MARKET_ANALYSIS, max_tokens=100)
    assert result.success and result.provider == LLMProvider.OPENAI
    assert len(result.attempt_trail) == 2
    first, second = result.attempt_trail
    assert first["estimated_cost_usd"] is None
    assert first["uncertain_cost_ceiling_usd"] is None
    assert first["cost_accounting_skipped"] is True
    assert first["missing_cost_policy"] == "log_and_continue"
    assert first["budget_reservation_token"] != second["budget_reservation_token"]
    summary = manager.get_usage_summary()
    assert summary["cost_missing"] is True
    assert summary["skipped_unknown_requests"] == 1
    assert summary["cost_accounting_complete"] is False
    assert summary["budget_remaining_usd"] is not None
    rows = [json.loads(line) for p in (tmp_path / "audit").glob("*.jsonl")
            for line in p.read_text().splitlines()]
    assert rows[0]["estimated_cost_usd"] is None
    assert rows[0]["cost_accounting_skipped"] is True
    assert rows[0]["cost_basis"] == "unknown"
    assert (await manager.complete("next", LLMTask.MARKET_ANALYSIS, max_tokens=100)).success


@pytest.mark.asyncio
@pytest.mark.parametrize("monthly", [False, True])
async def test_missing_cost_does_not_bypass_known_daily_cap(tmp_path, monthly):
    manager = manager_for(tmp_path, **({} if monthly else {"monthly_budget_usd": None}))
    manager.daily_usage.add(0, 0, "gpt-5-mini", usage_known=False)
    manager.daily_usage.estimated_cost_usd = 5
    manager.openai._session = Session(openai_reply())
    result = await manager.complete("news", LLMTask.MARKET_ANALYSIS)
    assert result.budget_block_reason == "daily_limit_exceeded"
    assert not manager.openai._session.payloads


@pytest.mark.asyncio
async def test_missing_cost_does_not_bypass_known_monthly_cap(tmp_path):
    from src.utils.llm_pricing import rate_quote
    ceiling = rate_quote("gpt-5-mini", 520, 100).cost_usd
    manager = manager_for(tmp_path, monthly_budget_usd=ceiling)
    old = manager.monthly_budget.reserve("gpt-5-mini", "old", "", 100)
    manager.monthly_budget.settle(old, outcome="unknown")
    paid = manager.monthly_budget.reserve("gpt-5-mini", "paid", "", 100)
    manager.monthly_budget.settle(paid, outcome="known", actual_cost_usd=ceiling)
    manager.openai._session = Session(openai_reply())
    result = await manager.complete("news", LLMTask.MARKET_ANALYSIS)
    assert result.budget_block_reason == "monthly_limit_exceeded"
    assert not manager.openai._session.payloads


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403])
async def test_permission_error_with_missing_body_cannot_trigger_fallback(tmp_path, status):
    class PermissionReply(Reply):
        async def json(self):
            raise TimeoutError()
    manager = manager_for(tmp_path)
    manager.openai._session = Session(PermissionReply({}, status=status), openai_reply())
    result = await manager.complete("news", LLMTask.MARKET_ANALYSIS)
    assert not result.success and len(manager.openai._session.payloads) == 1
    assert not manager.monthly_budget.status()["blocked"]


@pytest.mark.asyncio
async def test_safety_refusal_is_not_bypassed(tmp_path):
    manager = manager_for(tmp_path)
    refusal = openai_reply("")
    refusal.data["choices"][0]["message"]["refusal"] = "safety refusal"
    manager.openai._session = Session(refusal, openai_reply())
    result = await manager.complete("news", LLMTask.MARKET_ANALYSIS)
    assert not result.success and result.refusal_detected
    assert len(manager.openai._session.payloads) == 1


@pytest.mark.asyncio
async def test_missing_cost_fallback_does_not_expand_to_json_repair_or_shadow(tmp_path):
    manager = manager_for(tmp_path, openai_model_light_shadow="gpt-5-mini")
    manager.openai._session = Session(Reply({}, status=503), openai_reply("not json"), openai_reply("{}"))
    result = await manager.complete_json("news", LLMTask.MARKET_ANALYSIS, max_tokens=100)
    assert result["error"] == "Invalid JSON"
    assert len(manager.openai._session.payloads) == 2
    assert not manager._shadow_tasks


@pytest.mark.asyncio
async def test_json_repair_missing_cost_does_not_add_third_send(tmp_path):
    manager = manager_for(tmp_path)
    manager.openai._session = Session(openai_reply("not json"), Reply({}, status=503), openai_reply("{}"))
    result = await manager.complete_json("news", LLMTask.MARKET_ANALYSIS, max_tokens=100)
    assert result["error"]
    assert len(manager.openai._session.payloads) == 2


@pytest.mark.asyncio
async def test_fixed_provider_does_not_change_identity(tmp_path):
    manager = manager_for(tmp_path)
    manager.gemini._session = Session(Reply({}, status=503))
    manager.openai._session = Session(openai_reply())
    result = await manager.complete_with("news", provider=LLMProvider.GEMINI)
    assert not result.success and result.provider == LLMProvider.GEMINI
    assert not manager.openai._session.payloads
