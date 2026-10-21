from __future__ import annotations

import asyncio
from datetime import date, datetime
from types import SimpleNamespace

from src.analytics.post_exit_review import PostExitReviewer
from src.core.evolution.daily_reviewer import DailyReviewer
from src.core.evolution.trading_principles import TradingPrinciplesManager


class _LLMResult:
    def __init__(self, content: str):
        self.success = True
        self.content = content
        self.error = None


class _ReviewLLM:
    def __init__(self, content: str):
        self.content = content
        self.calls = 0

    async def complete(self, *args, **kwargs):
        self.calls += 1
        return _LLMResult(self.content)


def _daily_report(total_trades: int = 2):
    trades = [
        {"symbol": "005930", "name": "삼성전자", "strategy": "sepa_trend",
         "entry_time": "2026-10-22T09:10:00", "exit_time": "2026-10-22T10:10:00",
         "entry_price": 10000, "exit_price": 10200, "quantity": 10,
         "pnl": 20000, "pnl_pct": 1.0, "holding_minutes": 60,
         "entry_reason": "fixture", "exit_reason": "fixture", "exit_type": "take_profit",
         "indicators_at_entry": {}},
        {"symbol": "000660", "name": "SK하이닉스", "strategy": "sepa_trend",
         "entry_time": "2026-10-22T10:20:00", "exit_time": "2026-10-22T11:20:00",
         "entry_price": 10000, "exit_price": 9942, "quantity": 10,
         "pnl": -7655, "pnl_pct": -0.58, "holding_minutes": 60,
         "entry_reason": "fixture", "exit_reason": "fixture", "exit_type": "stop_loss",
         "indicators_at_entry": {}},
    ]
    return {
        "trades": trades if total_trades else [],
        "summary": {
            "total_trades": total_trades,
            "wins": 1 if total_trades else 0,
            "losses": 1 if total_trades == 2 else 0,
            "win_rate": 50.0 if total_trades else 0.0,
            "total_pnl": 12345 if total_trades else 0,
            "total_pnl_pct": 0.42 if total_trades else 0.0,
            "capital_pnl_pct": 0.049 if total_trades else 0.0,
            "capital_basis": 25_000_000,
            "capital_basis_source": "static_fallback",
            "pnl_basis": "net_of_commission_and_sell_tax",
            "return_basis": "sum_of_trade_net_pnl_pct",
            "profit_factor": 1.2 if total_trades else 0.0,
            "best_trade": {"symbol": "005930", "name": "삼성전자",
                           "pnl_pct": 1.0, "pnl": 20000} if total_trades else None,
            "worst_trade": {"symbol": "000660", "name": "SK하이닉스",
                            "pnl_pct": -0.58, "pnl": -7655} if total_trades else None,
        },
        "strategy_performance": {},
    }


def test_daily_review_sends_authoritative_numbers_and_keeps_machine_feedback(monkeypatch, tmp_path):
    model = _ReviewLLM(
        '{"assessment":"good","confidence":1,"daily_return_pct":999,'
        '"insights":["삼성전자 30% 추가매수"],"avoid_patterns":[],'
        '"focus_opportunities":[],"trade_reviews":[],'
        '"parameter_suggestions":[{"strategy":"sepa_trend","parameter":"min_score",'
        '"suggested_value":1,"confidence":1}],"telegram_summary":"수익률 999%"}'
    )
    reviewer = DailyReviewer(storage_dir=str(tmp_path), llm_manager=model)
    report = _daily_report()
    monkeypatch.setattr(reviewer, "load_report", lambda _day: report)
    monkeypatch.setattr(reviewer, "_build_llm_prompt", lambda *_args: "fixture prompt")
    saved = {}
    monkeypatch.setattr(reviewer, "_save_llm_review", lambda _day, value: saved.update(value))
    bias_inputs = []

    async def _bias(value, summary):
        bias_inputs.append((value, summary))

    sent = []

    async def _send(message):
        sent.append(message)

    monkeypatch.setattr(reviewer, "_save_daily_bias", _bias)
    monkeypatch.setattr("src.core.evolution.daily_reviewer.send_alert", _send)

    result = asyncio.run(reviewer.generate_llm_review(SimpleNamespace(), "2026-10-22"))

    assert model.calls == 1
    assert len(sent) == 1
    assert "12,345원" in sent[0] and "0.42%" in sent[0]
    assert "999" not in sent[0] and "삼성전자 30% 추가매수" not in sent[0]
    assert "수수료·매도세 반영" in sent[0]
    assert "계좌 기준 수익 미산출" in sent[0]
    assert "25,000,000" not in sent[0] and "0.049%" not in sent[0]
    assert "개별 수익률 상단" in sent[0] and "삼성전자" in sent[0]
    assert "개별 수익률 하단" in sent[0] and "SK하이닉스" in sent[0]
    assert "다음 확인" in sent[0] and "stop_loss 1건" in sent[0]
    assert "동일 진입" in sent[0] and "저장된 복기 JSON" not in sent[0]
    assert result["parameter_suggestions"][0]["suggested_value"] == 1
    assert bias_inputs[0][0]["parameter_suggestions"] == result["parameter_suggestions"]
    assert saved["telegram_summary"] == sent[0]


def test_zero_trade_review_is_truthful_and_skips_paid_call(monkeypatch, tmp_path):
    model = _ReviewLLM("{}")
    reviewer = DailyReviewer(storage_dir=str(tmp_path), llm_manager=model)
    monkeypatch.setattr(reviewer, "load_report", lambda _day: _daily_report(0))
    monkeypatch.setattr(reviewer, "_save_llm_review", lambda *_args: None)
    sent = []

    async def _send(message):
        sent.append(message)

    monkeypatch.setattr("src.core.evolution.daily_reviewer.send_alert", _send)
    result = asyncio.run(reviewer.generate_llm_review(SimpleNamespace(), "2026-10-22"))

    assert model.calls == 0
    assert result["assessment"] == "no_data"
    assert len(sent) == 1 and "청산 거래 0건" in sent[0]
    assert "성과 평가 보류" in sent[0]
    assert "실패" not in sent[0] and "기회" not in sent[0]


def test_daily_review_does_not_invent_missing_or_nonfinite_facts():
    body = DailyReviewer._build_authoritative_telegram_summary(
        date(2026, 10, 22),
        {
            "total_trades": float("nan"), "total_pnl": 1000,
            "total_pnl_pct": float("inf"), "capital_pnl_pct": 4.0,
            "capital_basis": 25_000_000, "capital_basis_source": "static_fallback",
            "best_trade": None, "worst_trade": None,
        },
        [{"name": "자료미기록", "pnl": None, "pnl_pct": None, "exit_type": ""}],
    )

    assert "청산 기록 1건" in body
    assert "손익 부호 미기록 1건" in body
    assert "개별 거래 수익률 합계 미산출" in body
    assert "비용 반영 여부 미기록" in body
    assert "기록 손익" in body and "순손익 <" not in body
    assert "계좌 기준 수익 미산출" in body
    assert "25,000,000" not in body and "4.000%" not in body
    assert "개별 수익률 상단: 미산출" in body
    assert "개별 수익률 하단: 미산출" in body
    assert "손실 거래 없음" not in body

    missing_count = DailyReviewer._build_authoritative_telegram_summary(
        date(2026, 10, 22), {}, None
    )
    assert "거래 건수 미기록" in missing_count
    assert "청산 거래 0건" not in missing_count

    sourced_ratio = DailyReviewer._build_authoritative_telegram_summary(
        date(2026, 10, 22),
        {
            "total_pnl": 1000, "total_pnl_pct": 0.1,
            "capital_pnl_pct": 0.01, "capital_basis": 10_000_000,
            "capital_basis_source": "local_portfolio_snapshot",
            "pnl_basis": "net_of_commission_and_sell_tax",
            "profit_factor": 99.9,
        },
        [{"pnl": 1000, "pnl_pct": 0.1, "exit_type": "take_profit"}],
    )
    assert "참고 실현손익/기준자본 +0.010%" in sourced_ratio
    assert "로컬 포트폴리오 스냅샷" in sourced_ratio
    assert "계좌수익률/TWR 아님" in sourced_ratio
    assert "손실 표본 없음 — 비율 미산출" in sourced_ratio
    assert "PF 99.90" not in sourced_ratio

    unknown_basis = DailyReviewer._build_authoritative_telegram_summary(
        date(2026, 10, 22),
        {"total_pnl": 1000, "total_pnl_pct": 0.1},
        [{"pnl": 1000, "pnl_pct": 0.1, "exit_type": "take_profit"}],
    )
    assert "기록 손익" in unknown_basis
    assert "개별 거래 수익률 합계 +0.10%" in unknown_basis
    assert "개별 거래 순손익률 합계" not in unknown_basis


def _post_exit_report():
    return {
        "report_date": "2026-10-22", "review_window_days": 30,
        "eligible_samples": 8, "samples": 6, "quote_missing_samples": 2,
        "observation_horizon_min_days": 1, "observation_horizon_max_days": 29,
        "quote_observed_at": "2026-10-22T09:31:00+09:00",
        "missed_count": 2, "avoided_count": 1, "neutral_count": 3,
        "missed": [{"name": "A", "symbol": "000001", "strategy": "sepa_trend",
                    "exit_type": "trailing", "post_exit_pct": 4.2, "days_since_exit": 1,
                    "pnl_pct": 1.0}],
        "avoided": [{"name": "B", "symbol": "000002", "strategy": "rsi2_reversal",
                     "exit_type": "stop_loss", "post_exit_pct": -3.5, "days_since_exit": 29,
                     "pnl_pct": -1.0}],
        "by_strategy": {"sepa_trend": {"count": 2, "missed": 1, "avoided": 0,
                                           "avg_post_exit": 2.1, "avg_pnl_pct": 0.5}},
        "by_exit_type": {}, "llm_insights": "파라미터를 30% 변경하세요",
    }


def test_post_exit_body_uses_observation_language_and_discloses_limits(monkeypatch, tmp_path):
    reviewer = PostExitReviewer(llm_manager=SimpleNamespace())
    report = _post_exit_report()
    body = reviewer._format_telegram(report)
    monkeypatch.setattr("src.analytics.post_exit_review.WIKI_DIR", tmp_path)
    wiki = reviewer._write_wiki_page(report).read_text(encoding="utf-8")
    prompt = reviewer._build_prompt(
        report["missed"] + report["avoided"],
        {"missed": report["missed"], "avoided": report["avoided"], "neutral": []},
        {"by_strategy": report["by_strategy"], "by_exit_type": {}},
        coverage={"eligible": 8, "tracked": 6, "missing": 2,
                  "quote_observed_at": report["quote_observed_at"]},
    )

    assert "관측 6/8건" in body and "미관측 2건" in body
    assert "관측 시차 1~29일" in body
    assert "비용·세금·체결 가능성 미반영" in body
    assert "다음 확인" in body and "5영업일 고정" in body
    assert "관측 6/8건" in wiki and "미관측 2건" in wiki
    assert "조회 시각: 2026-10-22T09:31:00+09:00" in wiki
    assert "다음 확인" in wiki and "5영업일 고정" in wiki
    for forbidden in ("놓친 수익", "회피 성공", "성공", "최적", "P0 파라미터", "몇 % 설명력"):
        assert forbidden not in body
        assert forbidden not in prompt
        assert forbidden not in wiki
    assert "가설" in prompt and "행동 지시" in prompt


class _PrincipleLLM:
    def __init__(self):
        self.prompts = []

    async def complete(self, prompt, **kwargs):
        self.prompts.append(prompt)
        return _LLMResult("<b>매수 30%</b>")


def test_principles_report_uses_current_iso_week_and_escapes_dynamic_text(monkeypatch, tmp_path):
    current = SimpleNamespace(
        symbol="NOW", strategy="sepa_trend", pnl_pct=1.2, exit_type="take_profit",
        holding_days=2, market_regime="neutral", exit_date="2026-10-22",
        timestamp="2026-10-22T10:00:00",
    )
    old = SimpleNamespace(
        symbol="OLD", strategy="sepa_trend", pnl_pct=-9.0, exit_type="stop_loss",
        holding_days=8, market_regime="bear", exit_date="2026-10-10",
        timestamp="2026-10-10T10:00:00",
    )
    missing_exit_date = SimpleNamespace(
        symbol="TIMESTAMP_ONLY", strategy="sepa_trend", pnl_pct=2.0,
        exit_type="take_profit", holding_days=1, market_regime="neutral",
        timestamp="2026-10-22T11:00:00",
    )
    principle = {"rule": "<b>원칙</b>", "confidence": 0.91, "delta": 2, "source_count": 7}
    memory = SimpleNamespace(
        _layer1=[old, current, missing_exit_date],
        get_summary=lambda: {"layer1_count": 3, "layer2_count": 3, "layer3_active": 1,
                             "layer3_total": 1, "principles": [principle]},
    )
    llm = _PrincipleLLM()
    manager = TradingPrinciplesManager.__new__(TradingPrinciplesManager)
    manager._trade_memory = memory
    manager._llm_manager = llm
    manager._cache_dir = tmp_path

    report = asyncio.run(manager.generate_weekly_report(as_of=datetime(2026, 10, 22, 20, 0)))

    assert "2026-W43" in report and "이번 주 관측: 1건" in report
    assert "분석 후보: 1/1건" in report
    assert "청산일 미기록/무효: 1건" in report
    assert "NOW" not in report  # LLM 호출 최소 표본 3건 미만
    assert "OLD" not in report
    assert "TIMESTAMP_ONLY" not in report
    assert "신뢰도=91%" not in report
    assert "&lt;b&gt;원칙&lt;/b&gt;" in report and "<b>원칙</b>" not in report
    assert "핵심 원칙 리마인더" not in report
    assert llm.prompts == []


def test_principles_llm_uses_disclosed_cap_and_escapes_output(tmp_path):
    observations = [
        SimpleNamespace(
            symbol=f"SYM{i:02d}", strategy="sepa_trend", pnl_pct=float(i),
            exit_type="take_profit", holding_days=1, market_regime="neutral",
            exit_date="2026-10-22", timestamp="2026-10-22T10:00:00",
        )
        for i in range(16)
    ]
    memory = SimpleNamespace(
        _layer1=observations,
        get_summary=lambda: {
            "layer1_count": 16, "layer2_count": 4, "layer3_active": 0,
            "layer3_total": 0, "principles": [],
        },
    )
    llm = _PrincipleLLM()
    manager = TradingPrinciplesManager.__new__(TradingPrinciplesManager)
    manager._trade_memory = memory
    manager._llm_manager = llm
    manager._cache_dir = tmp_path

    report = asyncio.run(
        manager.generate_weekly_report(as_of=datetime(2026, 10, 22, 20, 0))
    )

    assert "이번 주 관측: 16건" in report and "분석 후보: 15/16건" in report
    assert len(llm.prompts) == 1
    assert "SYM00" not in llm.prompts[0] and llm.prompts[0].count("SYM") == 15
    assert "&lt;b&gt;매수 30%&lt;/b&gt;" in report
    assert "<b>매수 30%</b>" not in report
    assert "매매·파라미터 변경 지시가 아닙니다" in report


def test_weekly_missing_memory_is_not_empty_observation(tmp_path):
    for memory in (None, SimpleNamespace(), SimpleNamespace(_layer1=None)):
        manager = TradingPrinciplesManager.__new__(TradingPrinciplesManager)
        manager._trade_memory = memory
        manager._llm_manager = _PrincipleLLM()
        manager._cache_dir = tmp_path
        report = asyncio.run(manager.generate_weekly_report(as_of=datetime(2026, 10, 22)))
        assert '거래 메모리 미확인' in report
        assert '이번 주 관측: 0건' not in report
        assert '분석 후보: 0/0' not in report
        assert manager._llm_manager.prompts == []


def test_weekly_known_empty_memory_still_reports_zero(tmp_path):
    manager = TradingPrinciplesManager.__new__(TradingPrinciplesManager)
    manager._trade_memory = SimpleNamespace(_layer1=[], get_summary=lambda: {})
    manager._llm_manager = _PrincipleLLM()
    manager._cache_dir = tmp_path
    report = asyncio.run(manager.generate_weekly_report(as_of=datetime(2026, 10, 22)))
    assert '이번 주 관측: 0건' in report
    assert '거래 메모리 미확인' not in report
