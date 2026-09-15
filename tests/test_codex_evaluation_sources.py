"""리뷰 R7/R8: 기권을 적중으로 세지 않고 당일 심의 이력을 잃지 않는다."""

import asyncio
import json
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.analytics import morning_brief_eval as mbe
from src.analytics import counterfactual_tracker as cf
from src.agents import team_ledger


def _evaluate(expert):
    return mbe.evaluate(
        {"kr_date": "2026-09-15", "claims": {}, "expert_consensus": expert},
        {"date": "2026-09-15", "kospi": {"close_change_pct": 0.1}},
        dispatched=True,
    )


@pytest.mark.parametrize("coverage", [0, 1, 3, None, -1, True, "4"])
def test_insufficient_or_unknown_coverage_abstains(coverage):
    axis = _evaluate({"score": 0, "bias": "neutral", "valid_n": coverage})["expert_direction"]
    assert axis["claimed"] is None
    assert axis["hit"] is None
    assert "전문가" in axis["reason"]


def test_legacy_coverage_missing_does_not_invent_a_flat_prediction():
    assert _evaluate({"score": 0, "bias": "neutral"})["expert_direction"]["hit"] is None


def test_sufficient_neutral_is_still_a_valid_prediction():
    assert _evaluate({"score": 0, "valid_n": 4})["expert_direction"]["hit"] is True


def test_abstained_experts_are_excluded_from_hit_rate(tmp_path):
    path = tmp_path / "evaluation.jsonl"
    mbe.append_ledger(path, _evaluate({"score": 0, "valid_n": 0}))
    mbe.append_ledger(path, _evaluate({"score": 0, "valid_n": 4}))
    assert mbe.summarize(path)["expert_direction"] == {"n": 1, "hits": 1, "hit_rate": 1.0}


@pytest.fixture
def tracker(tmp_path, monkeypatch):
    monkeypatch.setattr(cf, "_STATE_PATH", tmp_path / "state.json")
    monkeypatch.setattr(cf, "_TEAM_VERDICT_DIR", tmp_path / "team_verdicts")
    monkeypatch.setattr(team_ledger, "LEDGER_DIR", tmp_path / "team_ledger")
    monkeypatch.setattr(cf, "_SOURCES", {})
    return cf.CounterfactualTracker(fill_evidence_check=lambda symbol, day: False)


def _row(stance, slot, *, symbol="005930"):
    return {
        "symbol": symbol, "decided_at": f"2026-09-15T{slot}:00", "slot": slot,
        "input_snapshot_hash": slot, "deliberation_id": f"{symbol}-{slot}",
        "decision": {"stance": stance, "approved": True}, "wiki_context_used": False,
    }


def _latest(row):
    cf._TEAM_VERDICT_DIR.mkdir(parents=True, exist_ok=True)
    (cf._TEAM_VERDICT_DIR / "verdicts_20260915.json").write_text(json.dumps([row]))


def test_morning_buy_survives_latest_afternoon_hold(tracker):
    buy, hold = _row("buy", "10:30"), _row("hold", "14:00")
    for row in (buy, buy, hold):
        team_ledger.append_deliberation(row)
    _latest(hold)
    assert tracker._ingest_sources() == 1
    assert set(tracker._state) == {"team_buy_unfilled|005930|2026-09-15"}
    entry = next(iter(tracker._state.values()))
    assert entry["deliberation_ids"] == [buy["deliberation_id"], hold["deliberation_id"]]
    assert tracker._ingest_sources() == 0


def test_ledger_without_latest_file_is_ingested(tracker):
    team_ledger.append_deliberation(_row("buy", "10:30"))
    assert tracker._ingest_sources() == 1
    assert next(iter(tracker._state.values()))["source"] == "team_buy_unfilled"


def test_real_team_save_to_cf_keeps_morning_buy(tracker, monkeypatch):
    from src.agents import team

    class Clock(datetime):
        current = datetime(2026, 9, 15, 10, 30)

        @classmethod
        def now(cls, tz=None):
            return cls.current

    monkeypatch.setattr(team, "datetime", Clock)
    monkeypatch.setattr(team, "RESULT_DIR", cf._TEAM_VERDICT_DIR)
    monkeypatch.setenv("TEAM_ASSESSMENT_V2", "1")
    cf._TEAM_VERDICT_DIR.mkdir(parents=True)
    instance = object.__new__(team.TradingTeam)

    async def save_day():
        for stance, slot in (("buy", "10:30"), ("hold", "14:00")):
            row = _row(stance, slot)
            Clock.current = datetime.fromisoformat(row["decided_at"])
            verdict = SimpleNamespace(
                symbol="005930", slot=slot, reports=[], assessment=None, debate=None,
                deliberation_id=row["deliberation_id"], to_dict=lambda row=row: dict(row),
            )
            await instance._save(verdict)

    asyncio.run(save_day())
    latest = json.loads((cf._TEAM_VERDICT_DIR / "verdicts_20260915.json").read_text())
    assert len(latest) == 1 and latest[0]["decision"]["stance"] == "hold"
    assert len(team_ledger.load_day("2026-09-15")) == 2
    assert tracker._ingest_sources() == 1
    assert set(tracker._state) == {"team_buy_unfilled|005930|2026-09-15"}


def test_prior_hold_is_reclassified_without_losing_price_measurements(tracker):
    hold, buy = _row("hold", "10:30"), _row("buy", "14:00")
    _latest(hold)
    assert tracker._ingest_sources() == 1
    tracker._state["team_hold|005930|2026-09-15"].update(entry_px=10000, r5=2.0)
    team_ledger.append_deliberation(hold)
    team_ledger.append_deliberation(buy)
    tracker._ingest_sources()
    assert set(tracker._state) == {"team_buy_unfilled|005930|2026-09-15"}
    assert next(iter(tracker._state.values()))["entry_px"] == 10000
    assert next(iter(tracker._state.values()))["r5"] == 2.0


@pytest.mark.parametrize("filled", [True, None])
def test_morning_buy_never_becomes_hold_when_filled_or_unknown(tracker, filled, monkeypatch):
    team_ledger.append_deliberation(_row("buy", "10:30"))
    hold = _row("hold", "14:00")
    team_ledger.append_deliberation(hold)
    _latest(hold)
    monkeypatch.setattr(tracker, "_has_fill_evidence", lambda symbol, day: filled)
    assert tracker._ingest_sources() == 0
    assert tracker._state == {}


def test_existing_unfilled_is_removed_and_persisted_when_fill_arrives(tracker, monkeypatch):
    team_ledger.append_deliberation(_row("buy", "10:30"))
    assert tracker._ingest_sources() == 1
    tracker._save()
    monkeypatch.setattr(tracker, "_has_fill_evidence", lambda symbol, day: True)
    broker = SimpleNamespace(get_daily_prices=AsyncMock(return_value=[]))
    asyncio.run(tracker.update(broker))
    assert tracker._state == {}
    assert json.loads(cf._STATE_PATH.read_text()) == {}


def test_legacy_fallback_only_when_daily_ledger_absent(tracker):
    _latest(_row("buy", "10:30"))
    assert tracker._ingest_sources() == 1
    assert next(iter(tracker._state.values()))["source"] == "team_buy_unfilled"


def test_unreadable_ledger_does_not_silently_use_latest_hold(tracker):
    _latest(_row("hold", "14:00"))
    team_ledger.LEDGER_DIR.mkdir(parents=True)
    (team_ledger.LEDGER_DIR / "deliberations_20260915.jsonl").write_text("broken\n")
    assert tracker._ingest_sources() == 0
    assert tracker._state == {}
