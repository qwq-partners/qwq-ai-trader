"""CF 체결 대조 장애가 측정값을 지우거나 다른 날짜 가격으로 복구하지 않는다."""

import asyncio
import json
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.agents import team_ledger
from src.analytics import counterfactual_tracker as cf
from src.analytics import shadow_lab


DAY = (datetime.now() - timedelta(days=100)).strftime("%Y-%m-%d")
KEY = f"team_buy_unfilled|005930|{DAY}"
MEASUREMENTS = {
    "entry_px": 100, "r1": -1, "r5": -10, "r20": -20,
    "x1": -2, "x5": -12, "x20": -24,
}


@pytest.fixture
def tracker(tmp_path, monkeypatch):
    monkeypatch.setattr(cf, "_STATE_PATH", tmp_path / "counterfactual_state.json")
    monkeypatch.setattr(cf, "_SOURCES", {})
    monkeypatch.setattr(cf, "_TEAM_VERDICT_DIR", tmp_path / "verdicts")
    monkeypatch.setattr(team_ledger, "LEDGER_DIR", tmp_path / "ledger")
    monkeypatch.setattr(cf, "_TRADE_JOURNAL_DIR", tmp_path / "journal")
    cf._TRADE_JOURNAL_DIR.mkdir()
    monkeypatch.setattr(shadow_lab, "_CACHE", tmp_path)
    return cf.CounterfactualTracker()


def _decision(stance="buy", slot="10:30", day=DAY):
    team_ledger.append_deliberation({
        "symbol": "005930", "decided_at": f"{day}T{slot}:00", "slot": slot,
        "deliberation_id": f"005930-{slot}", "input_snapshot_hash": slot,
        "decision": {"stance": stance, "approved": True},
    })


def _journal(text, day=DAY):
    (cf._TRADE_JOURNAL_DIR / f"trades_{day.replace('-', '')}.json").write_text(text)


def _seed(tracker):
    _decision()
    assert tracker._ingest_sources() == 1
    tracker._state[KEY].update(MEASUREMENTS)
    tracker._save()


def _bars(start=None, count=45, price=200):
    day = datetime.fromisoformat(start) if start else datetime.now() - timedelta(days=45)
    return [{"date": (day + timedelta(days=i)).strftime("%Y%m%d"),
             "close": price + i} for i in range(count)]


def _update(tracker, prices=None):
    broker = SimpleNamespace(get_daily_prices=AsyncMock(return_value=prices or []))
    result = asyncio.run(tracker.update(broker))
    return result, broker


def test_journal_failure_save_reload_and_recovery_preserve_old_measurements(tracker):
    _seed(tracker)
    _journal("broken")
    _decision("hold", "14:00")
    _update(tracker, _bars())
    held = cf.CounterfactualTracker()
    assert KEY in held._state
    assert {k: held._state[KEY][k] for k in MEASUREMENTS} == MEASUREMENTS
    assert held._state[KEY]["deliberation_ids"] == ["005930-10:30", "005930-14:00"]
    assert "승인 BUY 미체결" not in held.summary()
    assert "KODEX200 대비" not in held.summary()
    _journal('{"trades": []}')
    _update(held, _bars())
    recovered = cf.CounterfactualTracker()
    assert {k: recovered._state[KEY][k] for k in MEASUREMENTS} == MEASUREMENTS
    assert recovered._state[KEY]["deliberation_ids"] == ["005930-10:30", "005930-14:00"]
    assert "승인 BUY 미체결 1건" in recovered.summary()
    assert "x5 -12.00%" in recovered.summary()


def test_unknown_partial_sample_keeps_values_without_price_updates(tracker):
    _seed(tracker)
    tracker._state[KEY]["r20"] = None
    tracker._state[KEY]["x20"] = None
    _journal("broken")
    result, broker = _update(tracker, _bars(DAY))
    assert KEY in tracker._state
    assert tracker._state[KEY]["r20"] is None
    assert tracker._state[KEY]["x20"] is None
    assert tracker._state[KEY]["r5"] == -10
    assert result["filled"] == 0
    assert all(call.args[0] != "005930" for call in broker.get_daily_prices.call_args_list)
    assert cf.CounterfactualTracker._pending_order(tracker._state) == []


def test_unknown_is_excluded_from_all_summary_denominators(tracker):
    _seed(tracker)
    _journal("broken")
    _update(tracker)
    tracker._state["valid"] = {
        "symbol": "000001", "date": DAY, "source": "team_buy_unfilled",
        "r5": 4, "r20": 8, "x5": 2,
    }
    summary = tracker.summary()
    assert "승인 BUY 미체결 1건" in summary
    assert "틀린 비율 0%" in summary
    assert "평균 r5 +4.0%" in summary
    assert "평균 r20 +8.0%" in summary
    assert "team_buy_unfilled n=1 x5 +2.00%" in summary
    assert "판정 불가" in summary


def test_unknown_then_filled_removes_buy_and_persists(tracker):
    _seed(tracker)
    _journal("broken")
    _update(tracker)
    held = cf.CounterfactualTracker()
    assert KEY in held._state
    _journal(json.dumps({"trades": [{"symbol": "005930", "entry_time": f"{DAY}T11:00:00"}]}))
    _update(held)
    assert cf.CounterfactualTracker()._state == {}


@pytest.mark.parametrize("filled", [False, True])
def test_new_unknown_buy_and_afternoon_hold_wait_for_evidence(tracker, filled):
    _decision()
    _decision("hold", "14:00")
    _journal("broken")
    _update(tracker)
    assert tracker._state == {}
    trades = [{"symbol": "005930", "entry_time": f"{DAY}T11:00:00"}] if filled else []
    _journal(json.dumps({"trades": trades}))
    _update(tracker)
    assert set(tracker._state) == (set() if filled else {KEY})


def test_hold_reclassified_during_unknown_preserves_measurements(tracker):
    _decision("hold")
    tracker._ingest_sources()
    tracker._state[f"team_hold|005930|{DAY}"].update(MEASUREMENTS)
    _decision("buy", "14:00")
    _journal("broken")
    _update(tracker)
    assert set(tracker._state) == {KEY}
    assert tracker._state[KEY]["r5"] == -10
    assert "승인 BUY 미체결" not in tracker.summary()
    _journal('{"trades": []}')
    _update(tracker)
    assert tracker._state[KEY]["r5"] == -10
    assert "승인 BUY 미체결 1건" in tracker.summary()


@pytest.mark.parametrize("entry_px", [None, 100])
def test_missing_exact_entry_day_never_substitutes_a_later_stock_bar(tracker, entry_px):
    _decision()
    tracker._ingest_sources()
    tracker._state[KEY]["entry_px"] = entry_px
    result, _ = _update(tracker, _bars())
    assert tracker._state[KEY]["entry_px"] == entry_px
    assert tracker._state[KEY]["r1"] is None
    assert tracker._state[KEY]["r5"] is None
    assert tracker._state[KEY]["r20"] is None
    assert result["filled"] == 0


def test_missing_exact_benchmark_day_does_not_invent_excess_return(tracker):
    _decision()
    tracker._ingest_sources()
    next_day = (datetime.fromisoformat(DAY) + timedelta(days=1)).isoformat()
    broker = SimpleNamespace(get_daily_prices=AsyncMock(side_effect=[_bars(next_day), _bars(DAY, price=100)]))
    asyncio.run(tracker.update(broker))
    assert tracker._state[KEY]["r5"] == 5
    assert tracker._state[KEY].get("x5") is None


def test_unknown_completed_sample_survives_retention_until_resolved(tracker):
    old_day = (datetime.now() - timedelta(days=200)).strftime("%Y-%m-%d")
    _decision(day=old_day)
    tracker._ingest_sources()
    key = f"team_buy_unfilled|005930|{old_day}"
    tracker._state[key].update(MEASUREMENTS)
    _journal("broken", day=old_day)
    _update(tracker)
    assert key in cf.CounterfactualTracker()._state


def test_shadow_readiness_counts_only_rule11_even_when_unknown_buy_has_returns(tracker):
    _seed(tracker)
    _journal("broken")
    _update(tracker)
    tracker._state["rule11|000001|2026-06-01"] = {"source": "rule11", "r5": -3}
    tracker._save()
    report = asyncio.run(shadow_lab.promotion_readiness_report())
    assert "CF 표본 1/20건, r5 정확도 100%" in report


def test_pending_batch_rotates_past_unpriceable_backfill_to_recent_sample(tracker):
    """45봉 밖 150건도 재조회하되, 뒤의 정상 후보를 영구히 굶기지 않는다."""
    old_day = "2020-01-02"
    recent_day = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    for idx in range(150):
        tracker._state[f"old-{idx:03}"] = {
            "symbol": f"OLD{idx:03}", "date": old_day, "source": "rule11",
            "entry_px": None, "r1": None, "r5": None, "r20": None,
        }
    tracker._state["recent"] = {
        "symbol": "RECENT", "date": recent_day, "source": "rule11",
        "entry_px": None, "r1": None, "r5": None, "r20": None,
    }
    requests = []
    recent_bars = _bars(recent_day, price=100)

    async def get_daily_prices(symbol, days):
        if symbol == "069500":
            return []
        requests.append(symbol)
        return recent_bars

    broker = SimpleNamespace(get_daily_prices=get_daily_prices)
    tracker._save()
    first = asyncio.run(tracker.update(broker))
    assert len(requests) == 150
    assert "RECENT" not in requests
    assert tracker._state["recent"]["entry_px"] is None
    assert first["filled"] == 0

    requests.clear()
    restored = cf.CounterfactualTracker()
    second = asyncio.run(restored.update(broker))

    assert "RECENT" in requests
    assert restored._state["recent"]["entry_px"] == 100
    assert second["filled"] == 3


def test_pending_batch_rotates_past_new_unpriceable_rows_to_price_ready_sample(tracker):
    """신규 손상 150건도 과거에 이미 가격이 있는 미완성 표본을 영구히 막지 않는다."""
    old_day = "2020-01-02"
    recent_day = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    for idx in range(150):
        tracker._state[f"new-{idx:03}"] = {
            "symbol": f"NEW{idx:03}", "date": recent_day, "source": "rule11",
            "entry_px": None, "r1": None, "r5": None, "r20": None,
        }
    tracker._state["old-priced"] = {
        "symbol": "OLD_PRICED", "date": old_day, "source": "rule11",
        "entry_px": 100, "r1": None, "r5": None, "r20": None,
    }
    requests = []
    old_bars = _bars(old_day, price=100)

    async def get_daily_prices(symbol, days):
        if symbol == "069500":
            return []
        requests.append(symbol)
        return old_bars

    broker = SimpleNamespace(get_daily_prices=get_daily_prices)
    tracker._save()
    asyncio.run(tracker.update(broker))
    assert len(requests) == 150
    assert "OLD_PRICED" not in requests

    requests.clear()
    restored = cf.CounterfactualTracker()
    second = asyncio.run(restored.update(broker))

    assert "OLD_PRICED" in requests
    assert restored._state["old-priced"]["entry_px"] == 100
    assert second["filled"] == 3
