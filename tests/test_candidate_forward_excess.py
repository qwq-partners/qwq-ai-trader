"""V01 판독 스크립트의 순수 계산 검사 (합성 일봉, 네트워크 없음)."""
import importlib.util
from decimal import Decimal
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location(
    "candidate_forward_excess", Path(__file__).resolve().parents[1] / "scripts/research/candidate_forward_excess.py")
m = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(m)

CAL = [f"2026-01-{d:02d}" for d in range(1, 11)]


def bars(closes, opens=None, lows=None):
    opens = closes if opens is None else opens
    lows = [min(o, c) for o, c in zip(opens, closes)] if lows is None else lows
    dec = lambda x: Decimal(str(x))  # noqa: E731
    return {d: (dec(o), dec(max(o, c)), dec(low), dec(c)) for d, o, low, c in zip(CAL, opens, lows, closes)}


def test_open_entry_sells_on_hth_close_including_entry_day():
    b = bars([100, 110, 121, 121, 121, 121, 121, 121, 121, 121], opens=[100] * 10, lows=[100] * 10)
    (ret, clip, mae, _, _), why = m.forward(b, CAL, CAL[0], "open", 2)
    assert why is None and ret == Decimal("0.1") and clip == ret and mae == 0   # Decimal 이라 정확히 일치


def test_close_entry_starts_next_day_and_ignores_entry_day_low():
    b = bars([100, 105, 110] + [110] * 7, lows=[50, 105, 110] + [110] * 7)
    (ret, clip, mae, _, _), _ = m.forward(b, CAL, CAL[0], "close", 2)
    assert ret == Decimal("0.1") and clip == ret and mae == 0


def test_stop_clip_intraday_and_gap():
    b = bars([100, 99, 99] + [99] * 7, opens=[100, 100, 99] + [99] * 7, lows=[100, 94, 99] + [99] * 7)
    (_, clip, mae, day, gapped), _ = m.forward(b, CAL, CAL[0], "close", 3)
    assert clip == Decimal("-0.05") and day == CAL[1] and mae == Decimal("-0.06") and not gapped
    g = bars([100, 92, 92] + [92] * 7, opens=[100, 93, 92] + [92] * 7, lows=[100, 91, 92] + [92] * 7)
    (_, clip, _, _, gapped), _ = m.forward(g, CAL, CAL[0], "close", 3)
    assert clip == Decimal("-0.07") and gapped   # 갭 하락은 시가로 종료


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
        b[patch[1]] = tuple(Decimal(x) for x in (100, 140, 100, 140))
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


def test_bench_adjust_scales_ohlc_and_drops_only_days_outside_adjust_range(tmp_path):
    p = tmp_path / "adj.csv"
    p.write_text("date,close\n2026-01-02,110\n")
    one = tuple(Decimal(1) for _ in range(4))
    raw = {"2026-01-01": one, "2026-01-02": tuple(Decimal(x) for x in (90, 105, 80, 100)), "2026-01-03": one}
    assert m.adjust_bench(raw, p) == {"2026-01-02": tuple(Decimal(x) for x in ("99", "115.5", "88", "110"))}


@pytest.mark.parametrize("adj,raw_days", [
    ("date,close\n2026-01-01,100\n2026-01-03,100\n", ["2026-01-01", "2026-01-02", "2026-01-03"]),   # 공개 일봉에만 있는 거래일
    ("date,close\n2026-01-01,100\n2026-01-02,100\n2026-01-03,100\n", ["2026-01-01", "2026-01-03"]),  # 보정 자료에만 있는 날
    ("date,close\n2026-01-01,0\n", ["2026-01-01"]),                                                    # 비양수
    ("date,close\n2026-01-01,100\n2026-01-01,101\n", ["2026-01-01"]),                                # 중복
])
def test_bench_adjust_fails_closed_on_calendar_mismatch_or_bad_values(tmp_path, adj, raw_days):
    p = tmp_path / "adj.csv"
    p.write_text(adj)
    with pytest.raises(SystemExit):
        m.adjust_bench({d: tuple(Decimal(100) for _ in range(4)) for d in raw_days}, p)


def test_build_obs_and_report_end_to_end(tmp_path):
    import json
    pit, team, prices = tmp_path / "pit", tmp_path / "team", tmp_path / "prices"
    for d in (pit, team, prices):
        d.mkdir()
    (pit / "2026-01-02.json").write_text(json.dumps({"date": "2026-01-02", "candidates": [
        {"symbol": "000001", "strategy": "sepa_trend", "score": 90.0},
        {"symbol": "000002", "strategy": "sepa_trend", "score": 70.0},
        {"symbol": "000003", "strategy": "sepa_trend", "score": 50.0}]}))
    sig = tmp_path / "signals.jsonl"
    sig.write_text("".join(json.dumps(r) + "\n" for r in [
        {"t": "2026-01-02T10:00:00", "symbol": "000001", "strategy": "gap_and_go", "score": 80.0, "type": "blocked", "gate": "G1_regime"},
        {"t": "2026-01-02T09:30:00", "symbol": "000001", "strategy": "gap_and_go", "score": 60.0, "type": "penalized", "gate": None},
        {"t": "2026-01-02T11:00:00", "symbol": "000002", "strategy": "sepa_trend", "score": 85.0, "adj": 81.0, "type": "passed", "gate": None},
        {"t": "2026-01-02T09:10:00", "symbol": "000002", "strategy": "gap_and_go", "score": 75.0, "type": "blocked", "gate": "G2_cross"},
        {"t": "2026-01-02T09:10:00", "symbol": "000003", "strategy": "gap_and_go", "score": 95.0, "type": "blocked", "gate": "G1_regime"}]))
    (team / "deliberations_20260102.jsonl").write_text("".join(json.dumps(r) + "\n" for r in [
        {"symbol": "000001", "decided_at": "2026-01-02T10:30:00", "decision": {"stance": "buy", "approved": True}},
        {"symbol": "000001", "decided_at": "2026-01-02T11:30:00", "decision": {"stance": "hold", "approved": True}},
        {"symbol": "000002", "decided_at": None, "recorded_at": "2026-01-02T10:31:00", "decision": None}]))
    obs = m.build_obs(str(pit), str(sig), str(team))
    b = {(o["family"], o["symbol"]): o for o in obs}
    assert len(obs) == 8
    assert b[("B", "000001")]["group"] == "penalized" and b[("B", "000001")]["score"] == 60.0   # 그날 첫 이벤트
    p2 = b[("B", "000002")]
    assert p2["group"] == "passed" and p2["score"] == 75.0             # 주 판정 점수는 사전 등록대로 첫 이벤트
    assert p2["strategy"] == "sepa_trend" and p2["rep_score"] == 81.0   # 참고 집계는 통과 이벤트의 전략·조정 점수
    assert b[("B", "000001")]["rep_score"] == 60.0                     # 조정 점수가 없으면 원점수
    assert b[("C", "000001")]["group"] == "buy_approved" and b[("C", "000002")]["group"] == "not_approved"

    days = [f"2026-01-{d:02d}" for d in range(1, 31)]
    def write(sym, daily):
        px, lines = 100.0, ["date,open,high,low,close"]
        for d in days:
            nxt = px * (1 + daily)
            lines.append(f"{d},{px},{max(px, nxt)},{min(px, nxt)},{nxt}")
            px = nxt
        (prices / f"{sym}.csv").write_text("\n".join(lines) + "\n")
    write(m.BENCH, 0.0)
    write("000001", 0.01)
    write("000002", 0.0)
    write("000003", -0.01)
    adj = tmp_path / "adj.csv"
    adj.write_text("date,close\n" + "".join(f"{d},100.0\n" for d in days))
    out = m.report(obs, str(prices), str(adj))
    a = out["primary"]["A-sepa"]
    assert a["n"] == 3 and a["verdict"] == "미입증"                     # 날짜 1일 → 판정 불가
    assert a["tercile"]["high"]["h5"]["mean"] == pytest.approx(1.01 ** 5 - 1 - float(m.COST))
    assert a["tercile"]["low"]["h5"]["mean"] == pytest.approx(0.99 ** 5 - 1 - float(m.COST))
    assert out["tables"]["B_by_gate"]["passed"]["h5"]["n"] == 1
    assert out["tables"]["daily_top3"]["A-sepa"]["h5"]["n"] == 3
    assert out["bench"]["rows"] == 30 and len(out["bench"]["sha256"]) == 64
    assert out["prices"]["symbols"] == 3 and out["prices"]["last_bar_before_bench_end"] == 0
    before = (out["prices"]["sha256"], out["obs"]["sha256"])
    bench_csv = prices / f"{m.BENCH}.csv"
    bench_csv.write_text(bench_csv.read_text().replace("2026-01-05,100.0,100.0", "2026-01-05,100.0,100.5", 1))
    again = m.report(obs, str(prices), str(adj))
    assert again["prices"]["sha256"] != before[0] and again["obs"]["sha256"] == before[1]   # 벤치마크 원본이 바뀌면 지문도 바뀐다
    (prices / "000003.csv").unlink()
    with pytest.raises(SystemExit):       # 가격 파일이 없는 종목이 있으면 조용히 제외하지 않고 실패한다
        m.report(obs, str(prices), str(adj))


def test_stop_threshold_is_exact_at_boundary():
    # 저가가 정확히 −5% 인 경우: float 였다면 100*0.95 표현 오차로 경계 판정이 흔들릴 수 있다
    b = bars([100, 100, 100] + [100] * 7, lows=[100, 95, 100] + [100] * 7)
    (_, clip, mae, day, _), _ = m.forward(b, CAL, CAL[0], "close", 3)
    assert clip == Decimal("-0.05") and day == CAL[1] and mae == Decimal("-0.05")


def test_spread_uses_only_days_with_both_groups():
    high = [("d1", {"net5": 0.03}), ("d2", {"net5": 0.05}), ("d3", {"net5": 0.9})]
    low = [("d1", {"net5": 0.01}), ("d2", {"net5": 0.01})]
    out = m.spread_stat(high, low, "net5", 0)
    assert out["days"] == 2 and out["mean"] == pytest.approx(0.03)


def test_c4_drops_by_actual_removal_effect_on_date_weighted_mean():
    # d01 은 단독 관측(빼면 날짜가 사라짐), d02 는 값이 큰 1건 + 작은 1건(빼면 그날 평균이 크게 내려감), 나머지는 0
    days = [f"d{i:02d}" for i in range(3, 13)]
    rows = [("d01", {"net5": 0.20}), ("d02", {"net5": 0.50}), ("d02", {"net5": 0.00})] + [(d, {"net5": 0.0}) for d in days]
    # 1번째: d02 의 0.50 제거(평균 0.45/12 → 0.20/12), 2번째: d01 제거(→ 0), 3번째: 더 내릴 관측 없음
    assert m.drop_top_contributors(rows, "net5", 1) == pytest.approx(0.20 / 12)
    assert m.drop_top_contributors(rows, "net5", 3) == pytest.approx(0.0)
    # 값은 가장 크지만 빼면 평균이 오르는 관측(다른 날보다 낮은 날의 단독 관측)은 빼지 않는다
    low = [("a", {"net5": 0.01})] + [(f"b{i}", {"net5": 0.05}) for i in range(5)]
    assert m.drop_top_contributors(low, "net5", 1) == pytest.approx((0.01 + 4 * 0.05) / 5)


def test_slope_recovers_linear_relation():
    assert m.slope([(x / 10, 0.5 * x / 10 + 0.01) for x in range(10)]) == pytest.approx(0.5)


def test_load_bars_drops_nonfinite_or_nonpositive_rows_and_rejects_duplicates(tmp_path):
    p = tmp_path / "x.csv"
    p.write_text("date,open,high,low,close\n2026-01-01,1,1,1,1\n2026-01-02,nan,1,1,1\n2026-01-03,0,0,0,5\n2026-01-04,inf,1,1,1\n")
    assert list(m.load_bars(p)) == ["2026-01-01"]
    p.write_text("date,open,high,low,close\n2026-01-01,1,1,1,1\n2026-01-01,2,2,2,2\n")
    with pytest.raises(SystemExit):
        m.load_bars(p)


def test_newey_west_uses_trading_day_distance_not_array_position():
    xs = [1.0, 1.0, 1.0, 1.0, 3.0, 3.0, 3.0, 3.0]
    adjacent = m.nw_t(xs, 2, list(range(8)))
    assert adjacent == pytest.approx(m.nw_t(xs, 2))                 # 연속 거래일이면 위치 기준과 같다
    far = m.nw_t(xs, 2, [i * 10 for i in range(8)])                 # 시차보다 멀리 떨어진 날들은 서로 보정하지 않는다
    assert far == pytest.approx(m.nw_t(xs, 0)) and abs(adjacent) < abs(far)


def test_daily_stat_passes_calendar_positions():
    rows = [(f"d{i}", {"net5": x}) for i, x in enumerate([1.0, 1.0, 1.0, 1.0, 3.0, 3.0, 3.0, 3.0])]
    near = m.daily_stat(rows, "net5", 2, {f"d{i}": i for i in range(8)})["t"]
    far = m.daily_stat(rows, "net5", 2, {f"d{i}": i * 10 for i in range(8)})["t"]
    assert abs(near) < abs(far)


@pytest.mark.parametrize("raw,expect", [
    ({"A": "선정 신호 있음", "B": "선정 신호 있음"}, {"A": "선정 신호 있음", "B": "선정 신호 있음"}),
    ({"A": "선정 신호 있음", "B": "미입증"}, {"A": "선정 신호 있음(잠정)", "B": "미입증"}),
    ({"A": "미입증", "B": "미입증"}, {"A": "미입증", "B": "미입증"}),
    ({"A": "음의 신호", "B": "선정 신호 있음"}, {"A": "음의 신호", "B": "선정 신호 있음(잠정)"}),
])
def test_single_family_pass_is_provisional(raw, expect):
    assert m.combine(raw) == expect
