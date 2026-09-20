"""Real owned morning notification boundary; external model/Telegram only are fake."""
import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.schedulers.kr_scheduler import KRScheduler
from test_execution_regime_morning import morning_owned


@pytest.fixture(autouse=True)
def synthetic_home(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)


@pytest.mark.parametrize("send_fails", [False, True])
def test_owned_notification_escapes_external_text_without_changing_durable_result(
    tmp_path, monkeypatch, send_fails
):
    async def scenario():
        engine, _, store, runtime, writer, _, _ = await morning_owned(tmp_path, monkeypatch)
        assessment = "[중립] price < 10 & news"
        calls = []
        model_calls = []
        class LLM:
            async def complete(self, prompt, **kwargs):
                model_calls.append(prompt)
                return SimpleNamespace(success=True, content=assessment)
        class Telegram:
            async def send_message(self, text, **kwargs):
                calls.append((text, kwargs))
                if send_fails:
                    raise RuntimeError("synthetic notification failure")
        theme = SimpleNamespace(
            get_active_themes=lambda: ["chips < news & more"],
            _recent_news=[SimpleNamespace(title="export < forecast & costs")],
        )
        scheduler = object.__new__(KRScheduler)
        scheduler.bot = SimpleNamespace(
            engine=engine, risk_manager=writer.sidecar, broker=None,
            telegram=Telegram(), theme_detector=theme,
        )
        import src.utils.llm as llm_module
        monkeypatch.setattr(llm_module, "get_llm_manager", lambda: LLM())
        monkeypatch.delenv("PERPLEXITY_API_KEY", raising=False)
        try:
            result = await scheduler._run_morning_diagnosis()
            assert result.receipt.status == "accepted"
            assert len(calls) == 1
            message, kwargs = calls[0]
            assert kwargs == {"parse_mode": "HTML"}
            assert "<b>장전 시장 진단</b>" in message
            assert "[중립] price &lt; 10 &amp; news" in message
            assert "chips &lt; news &amp; more" in message
            assert "export &lt; forecast &amp; costs" in message
            assert writer.morning_assessment()["assessment"] == assessment
            assert "chips < news & more" in model_calls[0]
            state = runtime.owner.state
            operation = result.receipt.operation_id
            assert state["risk_sources"]["records"][operation]["terminal"]["envelope"]["payload"]["raw_text"] == assessment
            assert (await scheduler._run_morning_diagnosis()).status == "already_done"
            assert len(calls) == len(model_calls) == 1
        finally:
            await runtime.shutdown()
            await store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("content", ["", None])
def test_failed_owned_text_never_builds_notification_and_can_retry(tmp_path, monkeypatch, content):
    async def scenario():
        engine, _, store, runtime, writer, _, _ = await morning_owned(tmp_path, monkeypatch)
        class LLM:
            async def complete(self, *args, **kwargs):
                return SimpleNamespace(success=True, content=content)
        class Telegram:
            async def send_message(self, *args, **kwargs):
                raise AssertionError("failed diagnosis must not notify")
        scheduler = object.__new__(KRScheduler)
        scheduler.bot = SimpleNamespace(
            engine=engine, risk_manager=writer.sidecar, broker=None,
            telegram=Telegram(), theme_detector=None,
        )
        import src.utils.llm as llm_module
        monkeypatch.setattr(llm_module, "get_llm_manager", lambda: LLM())
        monkeypatch.delenv("PERPLEXITY_API_KEY", raising=False)
        try:
            first = await scheduler._run_morning_diagnosis()
            second = await scheduler._run_morning_diagnosis()
            assert first.receipt.status == second.receipt.status == "failed"
            assert first.receipt.operation_id != second.receipt.operation_id
            assert writer.morning_assessment()["assessment_day"] is None
        finally:
            await runtime.shutdown()
            await store.close()
    asyncio.run(scenario())
