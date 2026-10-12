"""Portfolio return must not be invented from trade averages; failures abstain."""

from datetime import datetime
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.core.evolution import llm_strategist as module
from src.core.evolution.trade_reviewer import ReviewResult


@pytest.fixture
def strategist(monkeypatch):
    from src.core.evolution import lesson_store

    monkeypatch.setattr(
        lesson_store, "get_lesson_store",
        lambda: SimpleNamespace(format_context=lambda **kwargs: ""),
    )
    monkeypatch.setattr(module, "_count_trading_days", lambda *args: 5)
    return module.LLMStrategist(llm_manager=SimpleNamespace(), reviewer=SimpleNamespace())


def review(**changes):
    values = dict(
        period_start=datetime(2026, 10, 5), period_end=datetime(2026, 10, 9),
        total_trades=10, win_rate=30.0, avg_pnl_pct=-0.7,
        total_pnl=-70000.0, profit_factor=0.6, max_drawdown_pct=2.0,
        summary_for_llm="기록된 거래 표본", issues=["손실 표본 관찰"],
    )
    values.update(changes)
    return ReviewResult(**values)


@pytest.mark.parametrize("trades", [1, 10, 100])
def test_trade_frequency_cannot_create_account_daily_return(strategist, trades):
    prompt = strategist._build_analysis_prompt(review(total_trades=trades), True)
    assert "계좌 일수익률/TWR/벤치마크 초과수익: 미측정" in prompt
    assert "거래별 수익률 단순 평균: -0.70%" in prompt
    assert "기록된 거래 손익 합계: -70,000원" in prompt
    assert "일평균 수익률:" not in prompt
    assert "목표 대비 달성률" not in prompt
    assert "daily_return_gap" not in prompt


def test_cost_and_latency_evidence_required_without_return_or_trade_quota(strategist):
    prompt = strategist._build_analysis_prompt(review(), True)
    combined = strategist.SYSTEM_PROMPT + prompt
    for evidence in ["비용 후", "응답 이후", "기권", "잘못 거절", "회피한 손실", "KODEX200", "반도체", "버전"]:
        assert evidence in combined
    for quota in ["일 1%", "일평균 수익률 1%", "하루 2~5건", "-1.5~2%"]:
        assert quota not in combined


@pytest.mark.parametrize("include", [True, False])
def test_parameter_context_remains_optional(strategist, include):
    strategist.set_current_params("gap", {"min_score": 85})
    prompt = strategist._build_analysis_prompt(review(), include, extra_context="고정 코호트 A")
    assert ("min_score: 85" in prompt) is include
    assert "고정 코호트 A" in prompt


@pytest.mark.parametrize("changes", [
    {}, {"win_rate": 90.0, "profit_factor": 4.0, "avg_pnl_pct": 5.0},
    {"total_trades": 1, "win_rate": 0.0, "profit_factor": 0.0},
])
def test_fallback_abstains_instead_of_tightening_stops_or_expanding_trades(strategist, changes):
    strategist.set_current_params("gap", {"min_score": 85, "stop_loss_pct": 3.5})
    advice = strategist._create_fallback_advice(review(**changes), 7)
    assert advice.parameter_adjustments == []
    assert advice.new_rules == []
    assert advice.strategy_recommendations == {}
    assert advice.confidence_score == 0.0
    assert advice.overall_assessment == "fair"
    assert "미측정" in " ".join(advice.key_insights)
    assert "기권" in advice.next_week_outlook
    assert "일 1%" not in json.dumps(advice.to_dict(), ensure_ascii=False)


@pytest.mark.asyncio
@pytest.mark.parametrize("response", [
    SimpleNamespace(success=False, content="", error="timeout"),
    SimpleNamespace(success=True, content="malformed", error=None),
])
async def test_actual_analysis_failure_path_returns_no_parameter_proposals(strategist, monkeypatch, response):
    from src.signals.strategic.data_collector import StrategicDataCollector

    monkeypatch.setattr(StrategicDataCollector, "collect_macro_context", AsyncMock(return_value={}))
    strategist.reviewer.review_period = lambda days: review()
    strategist.llm.complete = AsyncMock(return_value=response)
    advice = await strategist.analyze_and_advise(days=7)
    assert advice.parameter_adjustments == []
    assert advice.confidence_score == 0.0
    strategist.llm.complete.assert_awaited_once()


def test_response_schema_and_serialization_stay_compatible(strategist):
    payload = dict(
        overall_assessment="fair", confidence_score=0.2,
        key_insights=["검증할 가설"], parameter_adjustments=[dict(
            parameter="gap.min_score", current_value=85, suggested_value=86,
            reason="별도 검증 후보", confidence=0.2, expected_impact="미검증",
        )], new_rules=[], strategy_recommendations={},
    )
    advice = strategist._parse_llm_response(json.dumps(payload), 7)
    assert advice.to_dict()["parameter_adjustments"] == payload["parameter_adjustments"]
    assert advice.overall_assessment == "fair"
    assert advice.period_days == 7
