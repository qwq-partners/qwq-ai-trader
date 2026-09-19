"""고정 C3 후보의 독립 리뷰 재현. 실제 owner/SQLite 경로만 사용한다."""
import asyncio
from copy import deepcopy
from datetime import timedelta
import json
from pathlib import Path

import pytest

from test_execution_regime_noon_replay_acceptance import (
    NOW, StageProvider, QuickLLM, owned_c3, stage_input,
)
from src.execution.safety.regime_owner import RegimeStageInput


@pytest.fixture(autouse=True)
def temporary_home(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)


@pytest.mark.parametrize("corruption", ["wrong_tr", "yesterday", "wrong_price"])
def test_invalid_index_fact_is_not_reintroduced_as_classifier_evidence(tmp_path, corruption):
    async def scenario():
        _, _, store, runtime, owner = await owned_c3(tmp_path)
        class Inputs(StageProvider):
            async def fetch_index_price(self, code):
                item = (await super().fetch_index_price(code)).to_dict()
                if code == "0001":
                    quote = item["payload"]["quote"]
                    if corruption == "wrong_tr":
                        quote["_observation"]["source_tr"] = "UNPROVEN"
                    elif corruption == "yesterday":
                        quote["_observation"]["received_at"] = (NOW - timedelta(days=1)).isoformat()
                        item["received_at"] = quote["_observation"]["received_at"]
                    else:
                        quote["price"] = 10000.0
                return RegimeStageInput.from_dict(item)
        try:
            source = await owner.classify("review", inputs_provider=Inputs(now=NOW),
                llm=QuickLLM({"regime": "trending_bull"}))
            state = runtime.owner.state
            meta = state["risk_sources"]["records"][source.operation_id]["terminal"]["envelope"]["payload"]["result"]["input_meta"]
            caps = state["regime_policy"]["noon_caps"]
            assert source.status == "accepted"
            if corruption == "wrong_price":
                assert "당일 미반영" in meta["kospi_bars_as_of"], json.dumps(meta, ensure_ascii=False)
            else:
                assert not caps
                assert meta["kospi_today_pct"] is None, json.dumps(meta, ensure_ascii=False)
        finally:
            if runtime.owner.healthy: await runtime.shutdown()
            await store.close()
    asyncio.run(scenario())


def test_public_owned_noon_completion_cannot_invent_market_timestamp(tmp_path):
    async def scenario():
        from test_execution_regime_owner import quote
        from src.execution.safety.index_risk_input import normalize_index_risk
        from src.execution.safety.regime_commands import noon_candidate
        from src.execution.safety.regime_application import HORIZON_READS
        _, _, store, runtime, owner = await owned_c3(tmp_path)
        try:
            ticket = await owner.sources.begin("review-noon-metadata", "noon_index", require_seal=True)
            fact = normalize_index_risk(quote("0001", pct=-3.0), now=NOW, business_day=ticket.business_day)
            reads = owner.sources.capture_reads(ticket, versioned_policy_reads=HORIZON_READS)
            candidate = noon_candidate(json.loads(reads), fact, NOW)
            inputs = {"observation_json": fact.observation_json, "candidate": candidate}
            await owner.sources.seal(ticket, versioned_policy_reads=HORIZON_READS,
                inputs=inputs, expected_reads_json=reads)
            before = await store.load()
            error = None
            try:
                receipt = await owner.sources.complete(ticket, "success", {
                    "level": candidate["level"], "change_pct": fact.change_pct, **inputs},
                    source=fact.source, source_event_id=fact.source_event_id,
                    received_at=fact.received_at, market_as_of=NOW, classified_at=NOW)
            except ValueError as exc:
                error = str(exc)
            after = await store.load()
            await runtime.restore()
            read = owner.sources.read_source("intraday")
            assert error is not None and after == before, {
                "rejected": error, "changed": after != before,
                "authority": read.authority_status, "envelope": read.envelope_json,
            }
        finally:
            if runtime.owner.healthy: await runtime.shutdown()
            await store.close()
    asyncio.run(scenario())


def test_optional_unconsumed_ohlc_missing_does_not_reject_valid_percent(tmp_path):
    """필드별 검증 수정이 정오에2분 OHLC 규칙을 강제하지 않도록 대조한다."""
    async def scenario():
        _, _, store, runtime, owner = await owned_c3(tmp_path)
        class Inputs(StageProvider):
            async def fetch_index_price(self, code):
                item = (await super().fetch_index_price(code)).to_dict()
                quote = item["payload"]["quote"]
                for name in ("open", "high", "low"):
                    quote[name] = 0.0
                    quote["_observation"]["fields"][name] = {"status": "missing", "value": None}
                return RegimeStageInput.from_dict(item)
        try:
            receipt = await owner.classify("review", inputs_provider=Inputs(now=NOW), llm=QuickLLM())
            assert receipt.status == "accepted"
            assert runtime.owner.state["regime_policy"]["horizon"]["level"] == "crash"
        finally:
            if runtime.owner.healthy: await runtime.shutdown()
            await store.close()
    asyncio.run(scenario())


def test_cold_restore_rejects_rehashed_cross_account_horizon_baseline(tmp_path):
    """의도적 SQL 손상 경계. 정상 등록 writer로 이 손상을 만들었다고 주장하지 않는다."""
    async def scenario():
        from decimal import Decimal
        from src.core.engine import UnifiedEngine
        from src.core.types import TradingConfig
        from src.execution.safety.application import ApplicationBlocked
        from src.execution.safety.protection_recovery import digest
        from src.execution.safety.runtime import KRExecutionRuntime
        from src.execution.safety.store import ExecutionStateStore
        from src.strategies.exit_manager import ExitManager
        _, _, store, runtime, _ = await owned_c3(tmp_path)
        state = deepcopy(runtime.owner.state)
        baseline = state["regime_policy"]["horizon_baseline"]
        baseline["supplied"]["account_scope"] = "foreign-account"
        baseline["digest"] = digest(baseline["supplied"])
        await store.commit(runtime.owner.version, state, "review-sql-baseline-corruption")
        await runtime.shutdown()
        await store.close()
        reopened = ExecutionStateStore(store.path)
        restored = KRExecutionRuntime(reopened,
            UnifiedEngine(TradingConfig(initial_capital=Decimal("2000000"))),
            ExitManager(persist=False, clock=lambda: NOW), clock=lambda: NOW, account_scope="scope")
        try:
            with pytest.raises(ApplicationBlocked):
                await restored.restore()
        finally:
            if restored.owner.healthy: await restored.shutdown()
            await reopened.close()
    asyncio.run(scenario())
