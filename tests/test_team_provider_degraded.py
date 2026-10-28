"""Offline E04 regressions: incomplete validation and typed provider cooldown."""
import asyncio
from datetime import datetime

import pytest
from loguru import logger

from src.agents import reproducibility, researchers
from src.agents.trader import TraderAgent
from src.agents.types import AnalystKind, AnalystReport, DebateResult, Stance
from src.utils.llm import LLMProvider, LLMResponse


@pytest.fixture(autouse=True)
def isolated_ledger(monkeypatch, tmp_path):
    monkeypatch.setattr(reproducibility, "LEDGER_DIR", tmp_path / "ledger")
    monkeypatch.setattr(reproducibility, "_ledger", None)


def reports(score=90):
    return [AnalystReport(kind=kind, symbol="synthetic", score=score,
                          confidence=0.9, data_as_of=datetime.now())
            for kind in (AnalystKind.FUNDAMENTAL, AnalystKind.TECHNICAL)]


@pytest.mark.parametrize("bull,bear", [(True, None), (None, True), (False, None),
                                        (None, False), (None, None), (1, True),
                                        (True, 0), ("APPROVE", True)])
def test_incomplete_final_votes_block_high_score_buy(bull, bear):
    debate = DebateResult(symbol="synthetic", bull_final=bull, bear_final=bear,
                          consensus=True, confidence=1.0)
    result = TraderAgent().propose("synthetic", "", reports(), debate)
    assert result.stance == Stance.HOLD
    assert "양측" in result.rationale


@pytest.mark.parametrize("debate", [None, DebateResult(symbol="synthetic", failed=True,
                                                     bull_final=True, bear_final=True)])
def test_failed_or_absent_debate_blocks_high_score_buy(debate):
    assert TraderAgent().propose("synthetic", "", reports(), debate).stance == Stance.HOLD


@pytest.mark.parametrize("bull,bear,consensus,confidence,conviction", [
    (True, True, True, 1.0, 0.9), (True, False, None, 0.5, 0.35),
    (False, True, None, 0.5, 0.35), (False, False, False, 1.0, 0.2)])
def test_actual_boolean_votes_keep_existing_scoring(bull, bear, consensus, confidence, conviction):
    debate = DebateResult(symbol="synthetic", bull_final=bull, bear_final=bear,
                          consensus=consensus, confidence=confidence)
    result = TraderAgent().propose("synthetic", "", reports(), debate)
    assert result.stance == Stance.BUY
    assert result.conviction == conviction


@pytest.mark.parametrize("debate", [None, DebateResult(symbol="synthetic", failed=True),
                                    DebateResult(symbol="synthetic", bull_final=True)])
def test_held_sell_survives_missing_debate(debate):
    assert TraderAgent().propose("synthetic", "", reports(-90), debate,
                                 holding=True).stance == Stance.SELL


def response(provider, *, success=True, status=200):
    return LLMResponse(content=("APPROVE evidence" if provider == LLMProvider.OPENAI else
                                "ACCEPT evidence") if success else "",
                       model="synthetic-model", provider=provider, success=success,
                       http_status=status, error=None if success else "sanitized 402 text",
                       usage_known=success, billing_status="reported_usage" if success else "unknown")


class ScriptedLLM:
    def __init__(self, failed_provider=LLMProvider.GEMINI, status=402, success=False):
        self.calls = []
        self.failed_provider = failed_provider
        self.status = status
        self.success = success

    async def complete_with(self, prompt, *, provider, **kwargs):
        self.calls.append(provider)
        return response(provider, success=self.success if provider == self.failed_provider else True,
                        status=self.status if provider == self.failed_provider else 200)


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", [LLMProvider.OPENAI, LLMProvider.GEMINI])
async def test_402_suppresses_later_rounds_symbols_and_records_unsent(provider):
    llm = ScriptedLLM(provider)
    team = researchers.ResearchTeam(llm, rounds=2)
    first = await team.debate("synthetic1", "", [])
    second = await team.debate("synthetic2", "", [])
    other = LLMProvider.GEMINI if provider == LLMProvider.OPENAI else LLMProvider.OPENAI
    assert llm.calls.count(provider) == 1
    assert llm.calls.count(other) == 4
    assert first.consensus is None and second.consensus is None
    missing = "bull_final" if provider == LLMProvider.OPENAI else "bear_final"
    assert getattr(first, missing) is None and getattr(second, missing) is None
    rows = reproducibility.LLMLedger.load()
    skipped = [row for row in rows if row.get("request_sent") is False]
    assert len(skipped) == 3
    assert all(row["error"] == "provider_unavailable" and row["billing_status"] == "not_sent"
               and row["model"] == "" and row["verdict"] is None and row["response"] == ""
               and row["success"] is False for row in skipped)
    health = team.get_stats()["providers"][provider.value]
    assert health["cooldowns"] == 1 and health["skipped"] == 3
    assert health["cooldown_active"] is True


@pytest.mark.asyncio
async def test_expiry_retries_naturally_and_success_logs_recovery_once(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(researchers, "monotonic", lambda: clock[0])
    llm = ScriptedLLM()
    team = researchers.ResearchTeam(llm, rounds=1)
    events = []
    sink = logger.add(lambda message: events.append(message.record), level="INFO")
    try:
        await team.debate("synthetic", "", [])
        clock[0] = 399.999
        await team.debate("synthetic", "", [])
        assert llm.calls.count(LLMProvider.GEMINI) == 1
        clock[0] = 400.0
        llm.success = True
        result = await team.debate("synthetic", "", [])
        assert result.bear_final is True
        await team.debate("synthetic", "", [])
        assert llm.calls.count(LLMProvider.GEMINI) == 3
        health = team.get_stats()["providers"]["gemini"]
        assert health["cooldown_active"] is False
        assert health["cooldowns"] == 1 and health["skipped"] == 1
        warnings = [event["message"] for event in events if event["level"].name == "WARNING"]
        assert len(warnings) == 1
        assert "gemini" in warnings[0] and "402" in warnings[0]
        assert "sanitized" not in warnings[0] and "synthetic" not in warnings[0]
        assert sum("recovered" in event["message"] for event in events) == 1
    finally:
        logger.remove(sink)


@pytest.mark.asyncio
@pytest.mark.parametrize("status,success", [(200, False), (403, False), (429, False),
                                            (None, False), ("402", False), (402, True)])
async def test_only_failed_typed_402_starts_cooldown(status, success):
    llm = ScriptedLLM(status=status, success=success)
    team = researchers.ResearchTeam(llm, rounds=2)
    await team.debate("synthetic", "", [])
    assert llm.calls.count(LLMProvider.GEMINI) == 2


@pytest.mark.asyncio
async def test_older_inflight_success_cannot_clear_new_402():
    started = asyncio.Event()
    release = asyncio.Event()

    class ConcurrentLLM:
        calls = 0

        async def complete_with(self, prompt, *, provider, **kwargs):
            self.calls += 1
            if self.calls == 1:
                started.set()
                await release.wait()
                return response(provider)
            return response(provider, success=False, status=402)

    llm = ConcurrentLLM()
    team = researchers.ResearchTeam(llm)
    older = asyncio.create_task(team._ask("old", "", LLMProvider.GEMINI))
    await started.wait()
    await team._ask("new", "", LLMProvider.GEMINI)
    release.set()
    assert (await older)["success"] is True
    skipped = await team._ask("later", "", LLMProvider.GEMINI)
    assert llm.calls == 2
    assert skipped["request_sent"] is False
    assert team.get_stats()["providers"]["gemini"]["cooldown_active"] is True


@pytest.mark.asyncio
async def test_suppressed_requests_never_reserve_budget_or_rewrite_missing_cost(tmp_path):
    import sqlite3
    from src.utils.llm import LLMConfig, LLMManager
    from test_llm_transport_contract import Reply, Session, openai_reply

    manager = LLMManager(LLMConfig(
        openai_api_key="offline", gemini_api_key="offline",
        monthly_budget_usd=100, budget_state_path=str(tmp_path / "budget.sqlite3"),
        missing_cost_policy="log_and_continue"), audit_dir=tmp_path / "audit")
    manager._LLM_LOG_DIR = tmp_path / "responses"
    manager.gemini._session = Session(Reply({}, status=402))
    manager.openai._session = Session(*[openai_reply() for _ in range(4)])
    team = researchers.ResearchTeam(manager, rounds=2)
    await team.debate("synthetic1", "", [])
    await team.debate("synthetic2", "", [])
    assert len(manager.gemini._session.payloads) == 1
    assert len(manager.openai._session.payloads) == 4
    with sqlite3.connect(tmp_path / "budget.sqlite3") as db:
        assert db.execute("SELECT COUNT(*) FROM reservations").fetchone()[0] == 5
    status = manager.get_monthly_budget_status()
    assert status["skipped_unknown_count"] == 1
    assert status["cost_accounting_complete"] is False
    assert status["blocked"] is False
    rows = reproducibility.LLMLedger.load()
    assert sum(row["billing_status"] == "not_sent" for row in rows) == 3


@pytest.mark.asyncio
async def test_overlapping_402_warns_once_and_new_instance_has_no_cooldown(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(researchers, "monotonic", lambda: clock[0])
    first_started = asyncio.Event()
    release = asyncio.Event()

    class TwoFailures:
        calls = 0

        async def complete_with(self, prompt, *, provider, **kwargs):
            self.calls += 1
            if self.calls == 1:
                first_started.set()
                await release.wait()
            return response(provider, success=False, status=402)

    llm = TwoFailures()
    team = researchers.ResearchTeam(llm)
    events = []
    sink = logger.add(lambda message: events.append(message.record), level="WARNING")
    try:
        older = asyncio.create_task(team._ask("old", "", LLMProvider.GEMINI))
        await first_started.wait()
        await team._ask("new", "", LLMProvider.GEMINI)
        release.set()
        await older
        await team._ask("suppressed", "", LLMProvider.GEMINI)
        assert len(events) == 1
        assert llm.calls == 2  # both requests were already in flight before failure
        clock[0] = 400.0
        await team._ask("natural retry", "", LLMProvider.GEMINI)
        assert len(events) == 2
        assert team.get_stats()["providers"]["gemini"]["cooldowns"] == 2
        second = researchers.ResearchTeam(llm)
        await second._ask("independent instance", "", LLMProvider.GEMINI)
        assert llm.calls == 4
    finally:
        logger.remove(sink)
