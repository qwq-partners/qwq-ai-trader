"""Regression coverage for factual weekly post-exit reporting."""

from __future__ import annotations

import asyncio
import json
import math
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.analytics import post_exit_review
from src.analytics.post_exit_review import PostExitReviewer
from src.schedulers import kr_scheduler
from src.schedulers.kr_scheduler import KRScheduler


class _NoCallLLM:
    def __init__(self):
        self.calls = 0

    async def complete(self, *args, **kwargs):
        self.calls += 1
        raise AssertionError("a zero-observation report must not call the LLM")


class _QuoteBroker:
    def __init__(self, quotes):
        self.quotes = quotes

    async def get_quote(self, symbol):
        return {"price": self.quotes[symbol]}


def _trade(symbol, exit_price=100.0):
    return {
        "symbol": symbol,
        "name": symbol,
        "strategy": "sepa_trend",
        "exit_time": "2026-10-20T09:00:00",
        "exit_price": exit_price,
        "pnl_pct": 1.0,
        "exit_type": "take_profit",
    }


def test_no_quotes_saves_and_formats_factual_denominator_without_wiki_or_llm(monkeypatch, tmp_path):
    """Catches the early return that discarded an eligible but unobserved cohort."""
    llm = _NoCallLLM()
    reviewer = PostExitReviewer(
        broker=_QuoteBroker({"A": None, "B": None}), llm_manager=llm
    )
    trades = [_trade("A"), _trade("B")]
    monkeypatch.setattr(reviewer, "_fetch_recent_exits", lambda: _async_value(trades))
    monkeypatch.setattr(post_exit_review, "JOURNAL_DIR", tmp_path / "journal")
    monkeypatch.setattr(post_exit_review, "WIKI_DIR", tmp_path / "wiki")
    reviewer.API_RATE_LIMIT_SLEEP = 0

    report = asyncio.run(reviewer.run_weekly())

    assert report["status"] == "no_quotes"
    assert report["samples"] == 0
    assert report["eligible_samples"] == 2
    assert report["quote_missing_samples"] == 2
    assert report["missed"] == [] and report["avoided"] == []
    assert "saved_path" in report and "wiki_path" not in report
    assert "관측 0/2건" in report["telegram_message"]
    assert "가격 변동·성과 판단 보류" in report["telegram_message"]
    assert llm.calls == 0
    assert not (tmp_path / "wiki").exists()
    saved = json.loads(Path(report["saved_path"]).read_text(encoding="utf-8"))
    assert saved["status"] == "no_quotes"
    assert saved["eligible_samples"] == 2
    assert saved["quote_missing_samples"] == 2


def test_no_recent_trades_stays_no_data_without_creating_a_no_sample_report(monkeypatch, tmp_path):
    """Characterizes the distinct, truthful no-eligible-cohort outcome."""
    reviewer = PostExitReviewer(llm_manager=_NoCallLLM())
    monkeypatch.setattr(reviewer, "_fetch_recent_exits", lambda: _async_value([]))
    monkeypatch.setattr(post_exit_review, "JOURNAL_DIR", tmp_path / "journal")

    report = asyncio.run(reviewer.run_weekly())

    assert report == {"status": "no_data", "samples": 0}
    assert not (tmp_path / "journal").exists()


def test_tracking_rejects_nonfinite_boolean_and_overflow_values_but_keeps_zero_return():
    """Catches accepting +inf quotes/returns and bool prices as numeric observations."""
    reviewer = PostExitReviewer(
        broker=_QuoteBroker({
            "VALID": 100.0,
            "QUOTE_BOOL": True,
            "QUOTE_INF": float("inf"),
            "QUOTE_OVERFLOW": "1e309",
            "EXIT_BOOL": 100.0,
            "EXIT_NAN": 100.0,
            "EXIT_INF": 100.0,
            "RETURN_OVERFLOW": 1e308,
        }),
        llm_manager=SimpleNamespace(),
    )
    reviewer.API_RATE_LIMIT_SLEEP = 0
    trades = [
        _trade("VALID", 100.0),
        _trade("QUOTE_BOOL", 100.0),
        _trade("QUOTE_INF", 100.0),
        _trade("QUOTE_OVERFLOW", 100.0),
        _trade("EXIT_BOOL", True),
        _trade("EXIT_NAN", float("nan")),
        _trade("EXIT_INF", float("inf")),
        _trade("RETURN_OVERFLOW", 1e-308),
    ]

    tracked = asyncio.run(reviewer._track_post_exit_prices(trades))

    assert [item["symbol"] for item in tracked] == ["VALID"]
    assert tracked[0]["post_exit_pct"] == 0.0
    assert math.isfinite(tracked[0]["current_price"])
    assert math.isfinite(tracked[0]["post_exit_pct"])


def test_nonfinite_journal_pnl_does_not_fabricate_zero_or_leak_into_aggregate():
    """Catches a valid quote observation rendering an invalid journal P&L as infinity."""
    reviewer = PostExitReviewer(
        broker=_QuoteBroker({"VALID": 104.0}), llm_manager=SimpleNamespace()
    )
    reviewer.API_RATE_LIMIT_SLEEP = 0
    trade = _trade("VALID", 100.0)
    trade["pnl_pct"] = float("inf")

    tracked = asyncio.run(reviewer._track_post_exit_prices([trade]))
    aggregated = reviewer._aggregate(tracked)
    classified = reviewer._classify(tracked)
    prompt = reviewer._build_prompt(
        tracked,
        classified,
        aggregated,
    )

    assert len(tracked) == 1 and tracked[0]["post_exit_pct"] == 4.0
    assert tracked[0]["pnl_pct"] is None
    assert aggregated["by_strategy"]["sepa_trend"]["avg_pnl_pct"] is None
    assert "미기록" in prompt and "inf" not in prompt


@pytest.mark.parametrize("count,journal_pnl", [(2, 1e308), (3, sys.float_info.max)])
def test_weekly_mean_does_not_overflow_when_individual_observations_are_finite(monkeypatch, tmp_path, count, journal_pnl):
    """유한한 두 값의 합산이 JSON/본문의 무한대 성과로 바뀌지 않아야 한다."""
    trades = [_trade(str(i), 1.0) for i in range(count)]
    for trade in trades:
        trade["pnl_pct"] = journal_pnl
    reviewer = PostExitReviewer(
        broker=_QuoteBroker({str(i): 1e306 for i in range(count)}), llm_manager=_NoCallLLM()
    )
    reviewer.API_RATE_LIMIT_SLEEP = 0
    monkeypatch.setattr(reviewer, "_fetch_recent_exits", lambda: _async_value(trades))
    monkeypatch.setattr(post_exit_review, "JOURNAL_DIR", tmp_path / "journal")
    monkeypatch.setattr(post_exit_review, "WIKI_DIR", tmp_path / "wiki")

    report = asyncio.run(reviewer.run_weekly())

    assert report["status"] == "ok" and report["samples"] == count
    for grouping in ("by_strategy", "by_exit_type"):
        aggregate = next(iter(report[grouping].values()))
        assert aggregate["avg_post_exit"] == 1e308
        assert aggregate["avg_pnl_pct"] == journal_pnl
    json.dumps(report, allow_nan=False)
    assert "inf%" not in report["telegram_message"]
    assert "inf%" not in Path(report["wiki_path"]).read_text()


class _SaturdayClock(datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2026, 10, 24, 9, 30)


class _NoQuotesReviewer:
    def __init__(self, **kwargs):
        pass

    async def run_weekly(self):
        return {
            "status": "no_quotes",
            "samples": 0,
            "eligible_samples": 2,
            "quote_missing_samples": 2,
            "telegram_message": "관측 0/2건 · 가격 변동·성과 판단 보류",
        }


def test_scheduler_sends_no_quotes_report_with_existing_week_consumption(monkeypatch, tmp_path):
    """Catches filtering the factual no_quotes report behind status == 'ok'."""
    from src.analytics import counterfactual_tracker, shadow_lab, tca, weakness_miner
    from src.core.evolution import gate_replay, lesson_store
    from src.strategies import harvest_shadow
    from src.utils import telegram

    # 다른 주간 작업은 이 회귀의 대상이 아니다. 모듈이 먼저 import되어도
    # 기존 운영 경로/싱글톤에 접근하지 않도록 공급자 경계에 가짜를 주입한다.
    monkeypatch.setattr(
        counterfactual_tracker, "get_counterfactual_tracker",
        lambda: SimpleNamespace(update=lambda _broker: _async_value(None), summary=lambda: ""),
    )
    monkeypatch.setattr(tca, "tca_summary", lambda **_kwargs: "")
    monkeypatch.setattr(weakness_miner, "mine", lambda **_kwargs: [])
    monkeypatch.setattr(weakness_miner, "format_summary", lambda _items: "")
    monkeypatch.setattr(lesson_store, "get_lesson_store", lambda: SimpleNamespace(curate=lambda: {}))
    monkeypatch.setattr(gate_replay, "run_due_replays", lambda: _async_value(""))
    monkeypatch.setattr(shadow_lab, "expert_calibration_report", lambda _broker: _async_value(""))
    monkeypatch.setattr(shadow_lab, "bandit_shadow_report", lambda: "")
    monkeypatch.setattr(shadow_lab, "promotion_readiness_report", lambda: _async_value(""))
    monkeypatch.setattr(harvest_shadow, "weekly_progress", lambda: "")
    monkeypatch.setattr(telegram, "get_telegram_notifier", lambda: None)
    sent = []

    async def _send(kind, message):
        assert kind == "post_exit_weekly"
        sent.append(message)
        raise asyncio.CancelledError

    def _warning(message):
        if "[후속복기] 결과:" in message:
            raise asyncio.CancelledError

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(kr_scheduler, "datetime", _SaturdayClock)
    monkeypatch.setattr(post_exit_review, "PostExitReviewer", _NoQuotesReviewer)
    monkeypatch.setattr(kr_scheduler, "record_report", _send)
    monkeypatch.setattr(
        kr_scheduler,
        "logger",
        SimpleNamespace(info=lambda *_a, **_k: None, warning=_warning, error=lambda *_a, **_k: None),
    )
    scheduler = object.__new__(KRScheduler)
    scheduler.bot = SimpleNamespace(running=True, trade_journal=None, broker=None)

    asyncio.run(scheduler.run_post_exit_review_scheduler())

    assert sent == ["관측 0/2건 · 가격 변동·성과 판단 보류"]
    state = json.loads(
        (tmp_path / ".cache" / "ai_trader" / "last_post_exit_review.json").read_text(
            encoding="utf-8"
        )
    )
    assert state["iso_year"] == 2026 and state["iso_week"] == 43


async def _async_value(value):
    return value
