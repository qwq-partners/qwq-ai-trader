"""수급 가점은 이번 스캔까지 끊기지 않은 참여 표시만 센다."""
from datetime import date

import pytest

from src.signals.screener.kr_screener import StockScreener
from test_selection_source_basis import run, screener, stock


def live_history_screener(monkeypatch, sources):
    result = screener(monkeypatch, sources)
    result._sd_history = {}
    result._sd_history_date = date.today().isoformat()
    for name in ("_record_supply_demand", "_apply_supply_accumulation_bonus"):
        setattr(result, name, getattr(StockScreener, name).__get__(result))
    return result


@pytest.mark.parametrize("gap", ["unconfirmed", "missing_candidate", "empty_scan"])
def test_interrupted_supply_does_not_accumulate_into_a_streak(monkeypatch, gap):
    sources = {}
    s = live_history_screener(monkeypatch, sources)
    for _ in range(3):
        sources["screen_volume_surge"] = [stock(has_foreign_buying=True)]
        result = run(s, capture_selection=True, capture_selection_modifiers=True)
        assert result[0].score == 50
        assert not any("수급누적" in reason for reason in result[0].reasons)
        if gap == "unconfirmed":
            sources["screen_volume_surge"] = [stock()]
        elif gap == "missing_candidate":
            sources["screen_volume_surge"] = [stock("000002")]
        else:
            sources["screen_volume_surge"] = []
        run(s)


def test_current_unconfirmed_supply_gets_no_old_streak_bonus(monkeypatch):
    sources = {"screen_volume_surge": [stock(has_inst_buying=True)]}
    s = live_history_screener(monkeypatch, sources)
    for expected in (50, 50, 56):
        assert run(s)[0].score == expected
    sources["screen_volume_surge"] = [stock()]
    current = run(s)[0]
    assert current.score == 50
    assert current.has_inst_buying is False
    assert current.has_foreign_buying is False
    assert not any("수급누적" in reason for reason in current.reasons)


def test_uninterrupted_foreign_or_institution_supply_keeps_bonus_and_cap(monkeypatch):
    sources = {}
    s = live_history_screener(monkeypatch, sources)
    for i, expected in enumerate((50, 50, 56, 58, 60, 62, 64, 65, 65)):
        sources["screen_volume_surge"] = [
            stock(has_foreign_buying=i % 2 == 0, has_inst_buying=i % 2 == 1)
        ]
        assert run(s)[0].score == expected


def test_one_candidates_gap_does_not_reset_another_candidates_streak(monkeypatch):
    sources = {}
    s = live_history_screener(monkeypatch, sources)
    for i in range(4):
        sources["screen_volume_surge"] = [
            stock(has_foreign_buying=True),
            stock("000002", has_inst_buying=i != 2),
        ]
        result = {item.symbol: item for item in run(s)}
    assert result["000001"].score == 58
    assert result["000002"].score == 50


def test_day_boundary_does_not_carry_old_streak_bonus(monkeypatch):
    sources = {"screen_volume_surge": [stock(has_foreign_buying=True)]}
    s = live_history_screener(monkeypatch, sources)
    for _ in range(3):
        run(s)
    s._sd_history_date = "1900-01-01"
    assert run(s)[0].score == 50


def test_supply_history_remains_bounded(monkeypatch):
    sources = {"screen_volume_surge": [stock(has_foreign_buying=True)]}
    s = live_history_screener(monkeypatch, sources)
    for _ in range(40):
        result = run(s)
    assert result[0].score == 65
    assert len(s._sd_history["000001"]) <= 36
