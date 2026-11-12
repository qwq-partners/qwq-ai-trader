"""V01 판독 스크립트의 순수 계산 검사 (합성 일봉, 네트워크 없음)."""
import importlib.util
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location(
    "candidate_forward_excess", Path(__file__).resolve().parents[1] / "scripts/research/candidate_forward_excess.py")
m = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(m)

CAL = [f"2026-01-{d:02d}" for d in range(1, 11)]


def bars(closes, opens=None, lows=None):
    opens = opens or closes
    lows = lows or [min(o, c) for o, c in zip(opens, closes)]
    return {d: (o, max(o, c), low, c) for d, o, low, c in zip(CAL, opens, lows, closes)}


def test_open_entry_sells_on_hth_close_including_entry_day():
    b = bars([100, 110, 121, 121, 121, 121, 121, 121, 121, 121], opens=[100] * 10, lows=[100] * 10)
    (ret, clip, mae, _), why = m.forward(b, CAL, CAL[0], "open", 2)
    assert why is None and ret == pytest.approx(0.10) and clip == ret and mae == 0


def test_close_entry_starts_next_day_and_ignores_entry_day_low():
    b = bars([100, 105, 110] + [110] * 7, lows=[50, 105, 110] + [110] * 7)
    (ret, clip, mae, _), _ = m.forward(b, CAL, CAL[0], "close", 2)
    assert ret == pytest.approx(0.10) and clip == ret and mae == 0


def test_stop_clip_intraday_and_gap():
    b = bars([100, 99, 99] + [99] * 7, opens=[100, 100, 99] + [99] * 7, lows=[100, 94, 99] + [99] * 7)
    (_, clip, mae, day), _ = m.forward(b, CAL, CAL[0], "close", 3)
    assert clip == pytest.approx(-0.05) and day == CAL[1] and mae == pytest.approx(-0.06)
    g = bars([100, 92, 92] + [92] * 7, opens=[100, 93, 92] + [92] * 7, lows=[100, 91, 92] + [92] * 7)
    (_, clip, _, _), _ = m.forward(g, CAL, CAL[0], "close", 3)
    assert clip == pytest.approx(-0.07)   # 갭 하락은 시가로 종료


@pytest.mark.parametrize("day,h,patch,why", [
    ("2025-12-31", 1, None, "not_trading_day"),
    (CAL[-1], 1, None, "horizon_not_reached"),
    (CAL[0], 2, ("drop", CAL[1]), "missing_bar"),
    (CAL[0], 2, ("jump", CAL[1]), "data_error"),
])
def test_exclusions(day, h, patch, why):
    b = bars([100.0] * 10)
    if patch and patch[0] == "drop":
        del b[patch[1]]
    if patch and patch[0] == "jump":
        b[patch[1]] = (100, 140, 100, 140)
    assert m.forward(b, CAL, day, "close", h) == (None, why)


def test_newey_west_matches_plain_t_at_lag0_and_shrinks_with_positive_autocorrelation():
    xs = [1.0, 1.0, 1.0, 1.0, 3.0, 3.0, 3.0, 3.0]   # 뭉친 수열: 양의 자기상관
    n = len(xs)
    mean = sum(xs) / n
    sd = (sum((x - mean) ** 2 for x in xs) / n) ** 0.5
    assert m.nw_t(xs, 0) == pytest.approx(mean / (sd / n ** 0.5))
    assert abs(m.nw_t(xs, 2)) < abs(m.nw_t(xs, 0))


def test_judge_requires_all_four_and_flags_negative():
    days = [f"d{i:02d}" for i in range(30)]
    mk = lambda v: [(d, {"net5": v + (i % 3) * 0.001, "net20": v + (i % 3) * 0.001}) for i, d in enumerate(days)]  # noqa: E731
    good = {"high": mk(0.03), "mid": mk(0.01), "low": mk(-0.01)}
    assert m.verdict(m.judge(good, 5), m.judge(good, 20)) == "선정 신호 있음"
    flat = {"high": mk(0.01), "mid": mk(0.03), "low": mk(-0.01)}       # 단조성 위반
    assert m.verdict(m.judge(flat, 5), m.judge(flat, 20)) == "미입증"
    bad = {"high": mk(-0.03), "mid": mk(0.0), "low": mk(0.01)}
    assert m.verdict(m.judge(bad, 5), m.judge(bad, 20)) == "음의 신호"


def test_terciles_split_into_thirds():
    q1, q2 = m.terciles(list(range(1, 10)))
    assert (q1, q2) == (3, 6)


def test_bench_adjust_scales_ohlc_to_adjusted_close(tmp_path):
    p = tmp_path / "adj.csv"
    p.write_text("date,close\n2026-01-01,110\n")
    out = m.adjust_bench({"2026-01-01": (90.0, 105.0, 80.0, 100.0), "2026-01-02": (1.0, 1.0, 1.0, 1.0)}, p)
    assert out == {"2026-01-01": pytest.approx((99.0, 115.5, 88.0, 110.0))}


def test_spread_uses_only_days_with_both_groups():
    high = [("d1", {"net5": 0.03}), ("d2", {"net5": 0.05}), ("d3", {"net5": 0.9})]
    low = [("d1", {"net5": 0.01}), ("d2", {"net5": 0.01})]
    out = m.spread_stat(high, low, "net5", 0)
    assert out["days"] == 2 and out["mean"] == pytest.approx(0.03)
