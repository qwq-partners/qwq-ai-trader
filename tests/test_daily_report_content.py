"""Factual-content regressions for scheduled daily reports.

Each test protects a user-visible claim: candidate provenance, price semantics,
evaluation coverage, and the boundary between observed US facts and LLM prose.
"""

from __future__ import annotations

import asyncio
import json
from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest

from src.analytics import daily_report as dr
from src.signals.screener.kr_screener import ScreenedStock


def _generator() -> dr.DailyReportGenerator:
    gen = dr.DailyReportGenerator.__new__(dr.DailyReportGenerator)
    gen.screener = SimpleNamespace(_is_etf_etn=lambda _name: False)
    gen.runtime_context_provider = None
    return gen


def test_direct_screen_watchlist_is_score_sorted_without_fabricated_prices_or_floor(monkeypatch):
    """An unsorted three-row screen must not become ten recommendations or gain fake exits."""
    now = datetime(2026, 10, 22, 8, 24)
    monkeypatch.setattr(dr, "_now", lambda: now)
    rows = [
        ScreenedStock("000001", "낮은점수", price=10_000, score=20, reasons=["관측 A"], screened_at=now),
        ScreenedStock("000002", "높은점수", price=20_000, score=90, reasons=["관측 B"], screened_at=now),
        ScreenedStock("000003", "중간점수", price=30_000, score=50, reasons=["관측 C"], screened_at=now),
    ]

    got = asyncio.run(_generator()._rank_stocks(rows, [], 10))

    assert [r.symbol for r in got] == ["000002", "000003", "000001"]
    assert len(got) == 3
    assert all(r.target_entry == r.target_exit == r.stop_loss == 0 for r in got)
    assert all(r.catalyst == "" for r in got)
    assert got[0].reference_price == 20_000
    assert got[0].reference_source == "KR 스크리너 관측"
    assert got[0].reference_as_of == "2026-10-22T08:24:00"


def test_direct_screen_watchlist_rejects_non_finite_scores(monkeypatch):
    """NaN/inf scores must not leak into sorting or user-visible report scores."""
    now = datetime(2026, 10, 22, 8, 24)
    monkeypatch.setattr(dr, "_now", lambda: now)
    rows = [
        ScreenedStock("000001", "정상", price=10_000, score=20, screened_at=now),
        ScreenedStock("000002", "NaN", price=20_000, score=float("nan"), screened_at=now),
        ScreenedStock("000003", "무한대", price=30_000, score=float("inf"), screened_at=now),
    ]

    got = asyncio.run(_generator()._rank_stocks(rows, [], 10))

    assert [r.symbol for r in got] == ["000001"]


def test_pending_watchlist_keeps_only_fresh_finite_signal_plans(monkeypatch, tmp_path):
    """Expired, malformed, and non-finite plans must never appear as current candidates."""
    now = datetime(2026, 10, 22, 8, 25)
    monkeypatch.setattr(dr, "_now", lambda: now)
    monkeypatch.setattr(dr, "_REC_CACHE_DIR", tmp_path)
    rows = [
        {
            "symbol": "005930", "name": "삼성전자", "strategy": "sepa_trend",
            "score": 81, "reason": "SEPA 관측", "entry_price": 70_000,
            "max_entry_price": 71_000, "stop_price": 67_000, "target_price": 76_000,
            "created_at": "2026-10-22T08:20:00", "decided_at": "2026-10-22T08:20:00",
            "expires_at": "2026-10-22T15:30:00", "entry_mode": "breakout",
            "breakout_trigger": 70_500,
        },
        {
            "symbol": "000660", "name": "만료", "strategy": "sepa_trend",
            "score": 99, "reason": "old", "entry_price": 100_000,
            "created_at": "2026-10-21T08:20:00", "expires_at": "2026-10-21T15:30:00",
        },
        {
            "symbol": "035420", "name": "비정상", "strategy": "sepa_trend",
            "score": 95, "reason": "bad", "entry_price": "NaN",
            "created_at": "2026-10-22T08:20:00", "expires_at": "2026-10-22T15:30:00",
        },
    ]
    (tmp_path / "pending_signals.json").write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")

    got = _generator()._load_from_pending_signals(date(2026, 10, 22), 10, [])

    assert [r.symbol for r in got] == ["005930"]
    rec = got[0]
    assert rec.reference_price == 70_000
    assert rec.reference_source == "pending_signals 진입계획"
    assert rec.max_entry_price == 71_000
    assert rec.trigger_price == 70_500
    assert rec.stop_loss == 67_000 and rec.target_exit == 76_000
    assert rec.expires_at == "2026-10-22T15:30:00"


def test_pending_watchlist_compares_offset_timestamps_in_korea_time(monkeypatch, tmp_path):
    """A UTC timestamp from the prior UTC date can still be today's valid KST plan."""
    monkeypatch.setattr(dr, "_now", lambda: datetime(2026, 10, 22, 8, 25))
    monkeypatch.setattr(dr, "_REC_CACHE_DIR", tmp_path)
    rows = [{
        "symbol": "005930", "name": "삼성전자", "strategy": "sepa_trend",
        "score": 81, "entry_price": 70_000,
        "created_at": "2026-10-21T23:20:00+00:00",
        "expires_at": "2026-10-22T06:30:00+00:00",
    }]
    (tmp_path / "pending_signals.json").write_text(json.dumps(rows), encoding="utf-8")

    got = _generator()._load_from_pending_signals(date(2026, 10, 22), 10, [])

    assert [r.symbol for r in got] == ["005930"]


def test_pending_watchlist_skips_malformed_rows_without_losing_valid_rows(monkeypatch, tmp_path):
    monkeypatch.setattr(dr, "_now", lambda: datetime(2026, 10, 22, 8, 25))
    monkeypatch.setattr(dr, "_REC_CACHE_DIR", tmp_path)
    rows = [
        "malformed",
        {"symbol": "", "name": "invalid", "score": 999, "entry_price": 1,
         "created_at": "2026-10-22T08:20:00", "expires_at": "2026-10-22T15:30:00"},
        {"symbol": "005930", "name": "valid", "score": 10, "entry_price": 70_000,
         "created_at": "2026-10-22T08:20:00", "expires_at": "2026-10-22T15:30:00"},
    ]
    (tmp_path / "pending_signals.json").write_text(json.dumps(rows), encoding="utf-8")
    assert [r.symbol for r in _generator()._load_from_pending_signals(date(2026, 10, 22), 1, [])] == ["005930"]


def test_morning_watchlist_labels_reference_conditions_and_runtime_context(monkeypatch):
    """A candidate body must distinguish observed references from execution readiness."""
    monkeypatch.setattr(dr, "_now", lambda: datetime(2026, 10, 22, 8, 25))
    gen = _generator()
    gen.runtime_context_provider = lambda: "<b>운영 상태</b>: 매수 중지"
    rec = dr.RecommendedStock(
        rank=1, symbol="005930", name="삼성전자", investment_thesis="SEPA 관측",
        catalyst="", total_score=81, reference_price=70_000,
        reference_as_of="2026-10-22T08:20:00", reference_source="pending_signals 진입계획",
        target_entry=70_000, max_entry_price=71_000, trigger_price=70_500,
        stop_loss=67_000, target_exit=76_000, expires_at="2026-10-22T15:30:00",
    )

    text = gen._format_morning_report([rec], [], date(2026, 10, 22))

    assert "오늘의 관찰 후보 (1개)" in text
    assert "생성 08:25" in text
    assert "참고가 70,000원" in text and "pending_signals 진입계획" in text
    assert "70,500원 돌파 확인" in text and "최대 71,000원" in text
    assert "무효화 참고 67,000원" in text and "목표 관찰 76,000원" in text
    assert "체결·수익을 의미하지 않음" in text
    assert "매수 중지" in text
    assert "오늘의 추천" not in text


def test_runtime_context_failure_is_explicit(monkeypatch):
    """A provider error must not be silently presented as normal execution readiness."""
    monkeypatch.setattr(dr, "_now", lambda: datetime(2026, 10, 22, 8, 25))
    gen = _generator()

    def broken():
        raise RuntimeError("unavailable")

    gen.runtime_context_provider = broken
    text = gen._format_morning_report([], [], date(2026, 10, 22))
    assert "운영 상태: 확인 불가" in text


def test_report_external_fields_are_html_escaped_before_rendering(monkeypatch):
    monkeypatch.setattr(dr, "_now", lambda: datetime(2026, 10, 22, 8, 25))
    gen = _generator()
    gen.runtime_context_provider = lambda: "<b>unsafe</b> & state"
    rec = dr.RecommendedStock(
        1, "<X>", "A&B <i>x</i>", "<b>thesis</b>", "",
        total_score=10, reference_price=100, reference_source="s&<x>",
        reference_as_of="t<1>", key_news="<script>alert(1)</script>",
        risk_factors=["r&<x>"], result_price=101, result_pct=1,
        result_source="src&<x>", result_as_of="time<1>",
    )
    morning = gen._format_morning_report([rec], [], date(2026, 10, 22))
    evening = gen._format_evening_report([rec], date(2026, 10, 22))
    for text in (morning, evening):
        assert "<script>" not in text and "<i>x</i>" not in text
        assert "A&amp;B &lt;i&gt;x&lt;/i&gt;" in text
    assert "&lt;b&gt;unsafe&lt;/b&gt; &amp; state" in morning


def test_evening_report_is_reference_change_with_coverage_not_hit_rate():
    """A watchlist close comparison must not claim entries, wins, or stop/target hits."""
    observed = dr.RecommendedStock(
        1, "005930", "삼성전자", "관측", "", prev_close=70_000,
        reference_price=70_000, reference_source="pending_signals 진입계획",
        result_price=71_400, result_pct=2.0,
    )
    missing = dr.RecommendedStock(
        2, "000660", "SK하이닉스", "관측", "", prev_close=100_000,
        reference_price=100_000, result_price=None, result_pct=None,
    )

    text = _generator()._format_evening_report([observed, missing], date(2026, 10, 22))

    assert "관찰 후보 가격 변화" in text
    assert "참고가 대비 +2.0%" in text
    assert "관측 가능 1 / 2" in text and "가격 미확인 1" in text
    assert "단순평균 +2.00%" in text
    for misleading in ("적중률", "🏆목표", "🛑손절", "추천종목 결과"):
        assert misleading not in text


def test_evening_report_treats_non_finite_results_as_unavailable():
    """Malformed cached results must not render NaN/inf as observed performance."""
    malformed = dr.RecommendedStock(
        1, "005930", "삼성전자", "관측", "", reference_price=70_000,
        result_price=float("inf"), result_pct=float("nan"),
    )

    text = _generator()._format_evening_report([malformed], date(2026, 10, 22))

    assert "관측 가능 0 / 1" in text
    assert "가격 미확인 1" in text
    assert "nan" not in text.lower() and "inf" not in text.lower()


def test_result_refresh_clears_stale_price_metadata_when_all_sources_fail(monkeypatch):
    rec = dr.RecommendedStock(
        1, "005930", "삼성전자", "관측", "", reference_price=70_000,
        result_price=71_000, result_pct=1.4, result_source="stale source",
        result_as_of="stale time",
    )
    gen = _generator()
    gen._today_recommendations = [rec]

    async def no_thread_result(*_args, **_kwargs):
        return None

    import aiohttp
    monkeypatch.setattr(dr.asyncio, "to_thread", no_thread_result)
    monkeypatch.setattr(
        aiohttp, "ClientSession",
        lambda: (_ for _ in ()).throw(RuntimeError("offline test")),
    )
    asyncio.run(gen._update_results())

    assert rec.result_price is None and rec.result_pct is None
    assert rec.result_source == "" and rec.result_as_of == ""


def test_us_market_date_comes_from_quote_timestamp_not_calendar_guess(monkeypatch):
    """A Wednesday quote after a Thursday holiday must remain Wednesday."""
    wednesday_close = datetime(2026, 11, 25, 21, 0, tzinfo=timezone.utc).timestamp()
    quotes = {
        "^GSPC": {"price": 6_000.0, "change_pct": 1.0, "market_time": wednesday_close},
        "^IXIC": {"price": 20_000.0, "change_pct": 1.2, "market_time": wednesday_close},
    }

    monkeypatch.setattr(dr, "_now", lambda: datetime(2026, 12, 1, 8, 0))
    basis = dr.resolve_us_quote_basis(quotes)

    assert basis["as_of"] == "2026.11.25"
    assert basis["source"] == "Yahoo regularMarketTime"
    assert basis["coverage"] == "2/2종"


def test_us_quote_basis_uses_new_york_date_and_excludes_future_partial_quotes(monkeypatch):
    monkeypatch.setattr(dr, "_now", lambda: datetime(2026, 10, 22, 10, 0))
    # 00:30 UTC is still the prior New York market date.
    prior_ny = datetime(2026, 10, 22, 0, 30, tzinfo=timezone.utc).timestamp()
    future = datetime(2026, 10, 23, 0, 30, tzinfo=timezone.utc).timestamp()
    basis = dr.resolve_us_quote_basis({
        "^GSPC": {"market_time": prior_ny}, "^IXIC": {}, "^DJI": {"market_time": future},
    })
    assert basis["as_of"] == "2026.10.21"
    assert basis["coverage"] == "1/3종"


def test_us_market_date_missing_is_disclosed_not_inferred():
    """When the provider omits market time, a report must not invent the previous day."""
    basis = dr.resolve_us_quote_basis({"^GSPC": {"price": 6_000.0, "change_pct": 1.0}})
    assert basis["as_of"] is None
    assert basis["source"] == "시장 시각 미제공"
    assert basis["coverage"] == "0/1종"


def test_morning_brief_is_deterministic_and_does_not_call_llm(monkeypatch):
    import src.utils.llm as llm_mod
    monkeypatch.setattr(
        llm_mod, "get_llm_manager",
        lambda: (_ for _ in ()).throw(AssertionError("LLM manager must not be loaded")),
    )
    monkeypatch.setattr(dr, "_today", lambda: date(2026, 10, 22))
    gen = _generator()
    record = asyncio.run(gen.build_morning_brief(
        quotes={"^GSPC": {"price": 6_000.0, "change_pct": 1.2}},
        sector_signals={}, avg_pct=1.2, us_date_str="2026.10.21", mood="강세",
    ))

    assert record is not None
    assert "확인된 가격 사실" in record["text"]
    assert "S&P500: +1.20% (6,000.0)" in record["text"]
    assert "S&P500은 +1.20% 상승했다" not in record["text"]
    assert "금리 인하 기대감" not in record["text"]
    assert "AI 기대에 힘입어" not in record["text"]
    assert "72000원" not in record["text"]
    assert "원인 자료 없음" not in record["text"]
    assert record["interpretation_removed"] == []
    assert "추가 확인" in record["text"]
    assert record["model"] == "deterministic-source-summary"
    assert record["raw_text"] == "" and record["body_full"] == record["text"]


def test_morning_brief_missing_quotes_are_not_rendered_as_confirmed_zero(monkeypatch):
    class Response:
        success, error, model, content = True, None, "test", "<b>애플 +1.2%</b>"
    class LLM:
        async def complete_role(self, **_kwargs):
            return Response()
    import src.utils.llm as llm_mod
    monkeypatch.setattr(llm_mod, "get_llm_manager", lambda: LLM())
    record = asyncio.run(_generator().build_morning_brief(
        quotes={"^GSPC": {"price": None}, "AAPL": {}}, sector_signals={},
        avg_pct=0, us_date_str="시장 시각 미제공", mood="보합",
    ))
    assert "+0.00%" not in record["text"] and "(0.0)" not in record["text"]
    assert "애플 +1.2%" not in record["text"]
    assert "가격·등락 미수집" in record["text"]


def test_removed_hallucination_does_not_set_brief_tone(monkeypatch):
    """Conflict/evaluation metadata must use retained prose, not a removed bullish story."""

    class Response:
        success = True
        error = None
        model = "test"
        content = "금리 인하 기대감 때문에 급등 랠리가 예상된다."

    class LLM:
        async def complete_role(self, **_kwargs):
            return Response()

    import src.utils.llm as llm_mod
    monkeypatch.setattr(llm_mod, "get_llm_manager", lambda: LLM())
    record = asyncio.run(_generator().build_morning_brief(
        quotes={"^GSPC": {"price": 6_000.0, "change_pct": 0.1}},
        sector_signals={}, avg_pct=0.1, us_date_str="2026.10.21", mood="보합",
    ))
    assert record["tone"] == "neutral"


def _brief_record(day: str, text: str) -> dict:
    return {
        "kr_date": day, "us_date": day, "text": text,
        "generated_at": f"{day}T07:00:00", "model": "test",
        "scope": dr.SCOPE_US_ONLY, "inputs": [],
        "claims": {"open_direction": None, "close_direction": None, "sectors": [], "basis": []},
        "removed_claims": [], "tone": "neutral", "expert_consensus": None,
        "expert_conflict": None,
    }


def test_dispatch_ref_matches_exact_displayed_brief_instead_of_latest(tmp_path):
    """A regenerated cache must not relabel the earlier displayed version as dispatched."""
    day = "2026-10-22"
    archive_dir = tmp_path / "archive"
    cache = tmp_path / "latest.json"
    first = _brief_record(day, "first displayed brief")
    second = _brief_record(day, "second undisplayed brief")
    dr.save_morning_brief(first, cache, archive_dir=archive_dir)
    dr.save_morning_brief(second, cache, archive_dir=archive_dir)

    path = dr.record_morning_brief_dispatch(
        kr_date=day, sent_text="combined message", dispatched_brief_text=first["text"],
        expert_consensus={"score": 0, "valid_n": 5}, status="sent",
        archive_dir=archive_dir,
    )

    dispatch = json.loads(path.read_text(encoding="utf-8"))["dispatch"][-1]
    assert dispatch["brief_ref"]["version"] == 1
    assert dispatch["brief_match"] == "exact_text"


def test_sent_dispatch_without_brief_does_not_fall_back_to_generated(tmp_path):
    """An expert-only dispatch must not score an undisplayed cached LLM generation."""
    day = "2026-10-22"
    archive_dir = tmp_path / "archive"
    cache = tmp_path / "latest.json"
    dr.save_morning_brief(_brief_record(day, "stale undisplayed brief"), cache, archive_dir=archive_dir)
    dr.record_morning_brief_dispatch(
        kr_date=day, sent_text="expert only", dispatched_brief_text=None,
        expert_consensus={"score": 0, "valid_n": 5}, status="sent",
        archive_dir=archive_dir,
    )

    brief, dispatched, ref, reason = _generator()._resolve_brief_for_eval(
        day, cache, archive_dir,
    )

    assert dispatched is True and ref is None and reason == "발송문에 LLM 브리프 없음"
    assert brief["claims"] == {}
    assert brief["text"] == "expert only"


def test_unverified_sector_aggregate_is_not_a_prediction_or_accuracy_sample():
    from src.analytics import morning_brief_eval
    record = asyncio.run(_generator().build_morning_brief(
        quotes={'^GSPC': {'price': 6000, 'change_pct': -1}},
        sector_signals={'AI/반도체': {'boost': -5, 'us_avg_pct': -3, 'top_movers': []}},
        avg_pct=-1, us_date_str='2026.10.21', mood='약세',
    ))
    assert 'AI/반도체' not in record['text']
    assert '부스트' not in record['text']
    assert record['claims']['sectors'] == []
    evaluated = morning_brief_eval.evaluate(record, {
        'date': record['kr_date'], 'kospi': {'close_change_pct': 0},
        'sectors': {'전기전자': 1},
    }, dispatched=True)
    assert evaluated['sectors'] == []
