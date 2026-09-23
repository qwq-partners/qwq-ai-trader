"""종가 LLM에 호환용 0 또는 5일 수익률을 당일 수익률로 주지 않는다."""

import asyncio
from types import SimpleNamespace

import pytest

from src.schedulers import kr_scheduler as ks
from test_sell_journal_reason import harness  # noqa: F401


@pytest.mark.parametrize("status,expected", [("stale", "결측"), ("fresh", "+8.0%")])
def test_eod_benchmark_has_correct_horizon(monkeypatch, harness, status, expected):
    import src.utils.llm as lm
    prompts = []

    async def complete_json(**kwargs):
        prompts.append(kwargs["prompt"])
        return {"positions": []}

    harness.bot.batch_analyzer = SimpleNamespace(_screener=SimpleNamespace(
        get_benchmark_status=lambda: {"status": status},
        get_kospi_change=lambda: {"c5": 8.0, "c20": 10.0},
    ))
    monkeypatch.setattr(lm, "get_llm_manager", lambda: SimpleNamespace(complete_json=complete_json))
    asyncio.run(harness.sched._run_position_eod_llm_check())
    assert len(prompts) == 1
    assert f"KOSPI 최근 5거래일: {expected}" in prompts[0]
    assert "오늘 KOSPI:" not in prompts[0]
