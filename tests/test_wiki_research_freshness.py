"""Research collection dates bound prompt reuse; historical trades stay available."""
from datetime import datetime, timezone
import json
from types import SimpleNamespace

import pytest

from src.core.evolution import trade_wiki
from src.agents.team import TradingTeam


class Clock(datetime):
    instant = datetime(2026, 10, 24, 12, tzinfo=timezone.utc)

    @classmethod
    def now(cls, tz=None):
        # Deliberately simulate a UTC host, including the legacy naive call.
        return cls.instant.astimezone(tz) if tz else cls.instant.replace(tzinfo=None)


@pytest.fixture
def wiki(tmp_path, monkeypatch):
    monkeypatch.setattr(trade_wiki, "datetime", Clock)
    return trade_wiki.TradeWiki(wiki_dir=tmp_path / "wiki")


def page(wiki, notes, *, trades=0, lessons="", updated="2026-10-24 20:00"):
    path = wiki._dir / "symbols" / "005930.md"
    path.write_text(
        f"---\nsymbol: 005930\ntrade_count: {trades}\nwin_rate: 50\n"
        f"last_updated: {updated}\n---\n## 거래 교훈\n{lessons}\n"
        f"## 리서치 노트\n{notes}\n", encoding="utf-8")
    return path


@pytest.mark.parametrize("stamp", [
    "2026-09-23", "2026-10-25", "2026-02-30", "10/24", "", "unknown",
])
def test_unusable_research_does_not_become_context_from_fresh_page(wiki, stamp):
    path = page(wiki, f"- [{stamp}] [news] excluded-research")
    original = path.read_bytes()
    assert wiki.query_symbol("005930") == ""
    assert path.read_bytes() == original


def test_boundary_recent_research_and_old_trade_lesson_are_distinguished(wiki):
    path = page(wiki,
                "- [2026-09-23] [news] too-old\n"
                "- [2026-09-24] [news] boundary-30\n"
                "- [2026-10-24] [news] same-day",
                trades=8, lessons="- [06/01] historical-lesson")
    original = path.read_bytes()
    text = wiki.query_symbol("005930")
    assert "too-old" not in text
    assert "boundary-30" in text and "same-day" in text
    assert "historical-lesson" in text and "8건" in text
    assert "과거 참고" in text and "현재 신호 아님" in text
    assert "수집일" in text and "관측일 미확인" in text
    assert "누적" in text
    assert path.read_bytes() == original


def test_research_is_sorted_before_limit_and_ties_keep_stored_order(wiki):
    page(wiki, "\n".join([
        "- [2026-10-01] [news] oldest",
        "- [2026-10-20] [news] tie-first",
        "- [2026-10-24] [news] latest",
        "- [2026-10-20] [news] tie-second",
        "- [2026-10-19] [news] fourth",
        "- [2026-10-18] [news] fifth",
        "- [2026-10-17] [news] sixth",
    ]))
    text = wiki.query_symbol("005930")
    assert "oldest" not in text and "sixth" not in text
    assert text.index("latest") < text.index("tie-first") < text.index("tie-second")
    assert "fourth" in text and "fifth" in text


def test_historical_only_page_is_not_expired(wiki):
    page(wiki, "", trades=6, lessons="- [01/01] durable-lesson", updated="2026-01-01")
    text = wiki.query_symbol("005930")
    assert "durable-lesson" in text and "6건" in text
    assert "과거 참고" in text
    assert wiki.query_symbol("missing") == ""


def test_mixed_iso_dated_lessons_and_research_keep_distinct_labels(wiki):
    page(wiki, "- [2026-10-24] [news] research-evidence",
         trades=8, lessons="- [2026-10-24] old-trade-lesson")
    text = wiki.query_symbol("005930")
    assert "[리서치] - [2026-10-24] [news] research-evidence" in text
    assert "[거래 교훈] - [2026-10-24] old-trade-lesson" in text


def test_import_and_research_work_without_system_timezone_database(tmp_path, monkeypatch):
    import builtins
    import importlib.util

    original_import = builtins.__import__

    def without_zoneinfo(name, *args, **kwargs):
        if name == "zoneinfo":
            raise ImportError("synthetic runtime without timezone database")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", without_zoneinfo)
    spec = importlib.util.spec_from_file_location("isolated_trade_wiki", trade_wiki.__file__)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "datetime", Clock)
    isolated = module.TradeWiki(wiki_dir=tmp_path / "isolated")
    page(isolated, "- [2026-10-24] [news] research-evidence")
    assert "research-evidence" in isolated.query_symbol("005930")


def test_recent_note_survives_real_team_prefix_budget(wiki, monkeypatch):
    page(wiki, "- [2026-10-24] [news] latest-evidence", trades=8,
         lessons="\n".join(f"- [01/01] lesson-{i} " + "과거교훈" * 30 for i in range(3)))
    team = object.__new__(TradingTeam)
    team._expert_orch = SimpleNamespace(trade_wiki=wiki, agents={})
    monkeypatch.setenv("RUNTIME_WIKI", "1")
    text = team._symbol_context("005930", "sample", None)
    assert "latest-evidence" in text and "수집일" in text
    assert len(wiki.query_symbol("005930")) <= 600
    monkeypatch.setenv("RUNTIME_WIKI", "0")
    assert team._symbol_context("005930", "sample", None) == ""


@pytest.mark.asyncio
async def test_kst_recording_and_query_agree_across_year_boundary(wiki, monkeypatch):
    monkeypatch.setattr(Clock, "instant", datetime(2026, 12, 31, 15, 30, tzinfo=timezone.utc))
    await wiki.note_research("005930", source="news", text="new-year-research")
    text = (wiki._dir / "symbols" / "005930.md").read_text()
    assert "[2027-01-01] [news]" in text
    assert "last_updated: 2027-01-01 00:30" in text
    assert "new-year-research" in wiki.query_symbol("005930")
    page(wiki, "- [2026-12-02] [news] boundary\n- [2026-12-01] [news] too-old")
    assert "boundary" in wiki.query_symbol("005930")
    assert "too-old" not in wiki.query_symbol("005930")


@pytest.mark.asyncio
@pytest.mark.parametrize("stamp,available", [("2026-10-24", True), ("2026-09-23", False)])
async def test_actual_entry_prompt_preserves_filtered_context_and_unknown_observation_time(
        wiki, monkeypatch, stamp, available):
    from src.core.cross_validator import CrossStrategyValidator
    from src.core.evolution import lesson_store
    from src.data.providers import disclosure_feed
    from src.utils.llm import LLMProvider, LLMResponse

    page(wiki, f"- [{stamp}] [news] research-evidence")
    monkeypatch.setattr(lesson_store, "get_lesson_store",
                        lambda: SimpleNamespace(format_context=lambda **kwargs: ""))
    monkeypatch.setattr(disclosure_feed, "get_symbol_disclosure_context", lambda *args: "")
    prompts = []

    async def complete(prompt, **kwargs):
        prompts.append(prompt)
        return LLMResponse("YES", "synthetic", LLMProvider.OPENAI)

    cross = CrossStrategyValidator(trade_wiki=wiki)
    cross._llm_manager = SimpleNamespace(complete=complete)
    cross._adversarial = None
    monkeypatch.setattr(cross, "_load_panel_outlook", lambda: None)
    assert await cross.llm_second_check("005930", "sepa_trend", 90, {}, "bear") is True
    assert len(prompts) == 1
    packet = json.loads(prompts[0].split("<entry_facts>\n", 1)[1].split("\n</entry_facts>", 1)[0])
    context = packet["context_sources"]["symbol_wiki"]
    assert context["source_observed_at"] is None
    if available:
        assert "research-evidence" in context["text"] and "수집일" in context["text"]
    else:
        assert context["text"] is None and context["status"] == "unavailable"
