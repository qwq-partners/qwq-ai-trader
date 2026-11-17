"""V08 판독 스크립트의 순수 계산 검사 (합성 자료, 네트워크 없음)."""
import importlib.util
import json
import sys
from decimal import Decimal as D
from pathlib import Path

import pytest

_dir = Path(__file__).resolve().parents[1] / "scripts/research"
sys.path.insert(0, str(_dir))   # 스크립트가 V01 모듈을 같은 디렉터리에서 불러온다
_spec = importlib.util.spec_from_file_location("value_core_shadow_readout", _dir / "value_core_shadow_readout.py")
m = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(m)


def _obs(week, gross, bench="0.00", symbol="A"):
    g, b = D(gross), D(bench)
    return {"week": week, "scan_date": week, "symbol": symbol, "bucket": "value", "kind": "final",
            "gross20": g, "bench20": b, "excess20": g - m.COST - b, "mae20": D("0")}


def _weeks(n, gross):
    return [_obs(f"W{i:02d}", gross) for i in range(n)]


@pytest.mark.parametrize("obs,verdict", [
    (_weeks(5, "0.10"), "extend"),                                    # 평가 가능 주차 < 6
    (_weeks(6, "0.00"), "close"),                                     # 비용만큼 음수 → 주차 초과 평균 ≤ 0
    (_weeks(6, "0.10") + [_obs("W00", "-0.30"), _obs("W01", "-0.30")], "extend_once"),   # 초과 > 0, 하위 25% < −15%
    (_weeks(6, "0.10"), "eligible"),
])
def test_verdict_follows_preregistered_table(obs, verdict):
    assert m.judge(obs)["verdict"] == verdict


def test_cost_alone_makes_a_flat_pick_lose_to_benchmark():
    j = m.judge(_weeks(6, "0.00"))
    assert j["mean_weekly_excess"] == -m.COST and j["criterion2"] is False


def test_lower_quartile_is_the_ceil_quarter_th_smallest_gross_return():
    # 15픽 → ceil(3.75) = 4번째로 작은 값. 가장 나쁜 3건이 −40% 여도 4번째가 −14% 면 기준 3 충족
    obs = [_obs(f"W{i:02d}", g) for i, g in enumerate(["-0.40"] * 3 + ["-0.14"] + ["0.20"] * 11)]
    j = m.judge(obs)
    assert j["lower_quartile_gross"] == D("-0.14") and j["criterion3"] is True
    obs[3] = _obs("W03", "-0.16")
    assert m.judge(obs)["criterion3"] is False


def test_weekly_excess_is_equal_weighted_within_week_then_across_weeks():
    obs = [_obs("W1", "0.30"), _obs("W1", "0.10"), _obs("W2", "0.00")]
    wk = m.weekly_excess(obs)
    assert wk == {"W1": D("0.20") - m.COST, "W2": -m.COST}
    assert m.judge(obs)["mean_weekly_excess"] == D("0.10") - m.COST     # 주차 평균의 평균(픽 3건의 평균이 아니다)


def test_entry_is_open_of_first_trading_day_after_scan_and_exclusions_are_counted():
    cal = [f"2026-01-{d:02d}" for d in range(5, 10)]                   # 월~금
    flat = {d: (D(100), D(100), D(100), D(100)) for d in cal}
    up = dict(flat)
    up[cal[1]] = (D(100), D(110), D(100), D(110))                      # 화 시가 100 → 종가 110
    rows = [{"week": "W1", "scan_date": cal[0], "symbol": "UP", "bucket": "growth", "kind": "final"},
            {"week": "W1", "scan_date": cal[0], "symbol": "NOBAR", "bucket": "value", "kind": "final"},
            {"week": "W9", "scan_date": cal[-1], "symbol": "UP", "bucket": "growth", "kind": "final"}]
    obs, excluded = m.observe(rows, {"UP": up}, flat, cal, horizons=(1,), main=1)   # 합성 달력이 짧아 판정 구간 1일
    assert [(o["symbol"], o["entry_day"], o["gross1"], o["excess1"]) for o in obs] == \
        [("UP", cal[1], D("0.1"), D("0.1") - m.COST)]
    assert excluded == {"final:missing_bar": 1, "final:no_next_trading_day": 1}


def test_history_loader_splits_final_and_llm_rejected_and_respects_through(tmp_path):
    def week(name, scan_date, final):
        (tmp_path / f"{name}.json").write_text(json.dumps({
            "scan_date": scan_date, "scan_week": name, "final": final,
            "quant_passed": [{"symbol": s, "bucket": "value"} for s in ("A", "B", "C")],
            "llm_verdicts": {"A": {"total": 70, "passed": True, "one_line": "x"},
                             "B": {"total": 40, "passed": False, "one_line": "x"},
                             "C": {"total": 0.0, "passed": False, "one_line": ""}}}))   # C: 실패인지 0점 탈락인지 불명
    week("2026-W01", "2026-01-05", ["A"])
    week("2026-W02", "2026-01-12", [])
    week("2026-W03", "2026-01-19", ["A"])
    rows, weeks, empty = m.load_history(str(tmp_path), "2026-01-12")
    assert weeks == [("2026-W01", "2026-01-05"), ("2026-W02", "2026-01-12")] and empty == ["2026-W02"]
    assert [(r["week"], r["symbol"], r["kind"]) for r in rows] == [
        ("2026-W01", "A", "final"), ("2026-W01", "B", "llm_rejected"), ("2026-W01", "C", "llm_unknown"),
        ("2026-W02", "B", "llm_rejected"), ("2026-W02", "C", "llm_unknown")]


def test_supplementary_t_measures_overlap_in_trading_days_between_entry_days():
    """주 간격이 4주여도 휴일로 진입일이 19거래일 이내면 20거래일 구간이 겹친다 — 그 공분산이 t 에 들어간다."""
    cal = [f"d{i:03d}" for i in range(120)]
    entries = [0, 5, 10, 29, 34, 53]            # 5·5·19·5·19거래일 간격: 0↔29(29)는 안 겹치고 10↔29(19)는 겹친다
    obs = []
    for i, (e, g) in enumerate(zip(entries, ["0.10", "0.02", "0.09", "0.01", "0.08", "0.03"])):
        o = _obs(f"W{i}", g)
        o["entry_day"] = cal[e]
        obs.append(o)
    xs = [float(v) for v in m.weekly_excess(obs).values()]
    got = m.supplementary(obs, [], cal)["t_weekly_nw"]
    assert got == pytest.approx(m.nw_t(xs, m.H - 1, entries))
    assert got != pytest.approx(m.nw_t(xs, 3))                           # 주 단위 3시차로 보면 다른 값이다


def test_post_hoc_means_share_the_weekly_weighting_of_the_verdict():
    obs = [_obs("2026-W01", "0.30", "0.10"), _obs("2026-W01", "0.10", "0.10"), _obs("2026-W02", "0.00", "-0.20")]
    ph = m.post_hoc(obs, m.weekly_excess(obs))
    assert ph["mean_weekly_gross"] == D("0.10") and ph["mean_weekly_bench"] == D("-0.05")
    assert ph["mean_weekly_gross"] - m.COST - ph["mean_weekly_bench"] == m.judge(obs)["mean_weekly_excess"]


@pytest.mark.parametrize("verdict,kind", [
    ({"total": 40.0, "passed": False, "one_line": "근거"}, "llm_rejected"),
    ({"total": 0.0, "passed": False, "one_line": "0점 근거"}, "llm_rejected"),   # 근거가 있는 0점은 심사 결과다
    ({"total": 40.0, "passed": False, "one_line": ""}, "llm_rejected"),
    ({"total": 0.0, "passed": False, "one_line": ""}, "llm_unknown"),            # 호출 실패 기록과 같은 모양
    ({"total": 0, "passed": False, "one_line": ""}, "llm_unknown"),
    ({"passed": False, "one_line": "근거"}, "llm_unknown"),                      # 점수 필드 없음
    ({"total": 40.0, "passed": False}, "llm_unknown"),
])
def test_not_passed_records_are_split_without_falsy_shortcuts(verdict, kind):
    assert m.not_passed_kind(verdict) == kind


def _write_report_inputs(tmp_path, pick_days, adj_days=lambda days: days, bench_days=lambda days: days):
    """월~금 3주치 달력. 픽 X 의 가격 파일은 pick_days, 보정 자료는 adj_days, 벤치마크 일봉은 bench_days 만 담는다."""
    import datetime
    days = [(datetime.date(2026, 1, 5) + datetime.timedelta(days=i)).isoformat() for i in range(19) if i % 7 < 5]
    hist, prices = tmp_path / "hist", tmp_path / "prices"
    hist.mkdir(), prices.mkdir()
    (hist / "2026-W02.json").write_text(json.dumps({
        "scan_date": days[0], "scan_week": "2026-W02", "final": ["X"],
        "quant_passed": [{"symbol": "X", "bucket": "value"}], "llm_verdicts": {}}))
    rows = lambda ds: "date,open,high,low,close\n" + "".join(f"{d},100,100,100,100\n" for d in ds)  # noqa: E731
    (prices / f"{m.BENCH}.csv").write_text(rows(bench_days(days)))
    (prices / "X.csv").write_text(rows(pick_days(days)))
    adj = tmp_path / "adj.csv"
    adj.write_text("date,close\n" + "".join(f"{d},100\n" for d in adj_days(days)))
    return str(hist), str(prices), str(adj), days[-1]


def test_report_rejects_price_cache_that_stops_before_the_last_trading_day(tmp_path):
    hist, prices, adj, through = _write_report_inputs(tmp_path, lambda days: days[:-3])
    with pytest.raises(SystemExit, match="가격 캐시가 분석 기간을 덮지 못하는 종목 1개"):
        m.report(hist, prices, adj, through)
    out = m.report(hist, prices, adj, through, allow_partial_prices=True)     # 명시하면 진행하고 사실을 남긴다
    assert out["prices"] == {"symbols": 1, "sha256": out["prices"]["sha256"], "partial_coverage": 1, "allow_partial_prices": True}


def test_report_accepts_full_price_cache(tmp_path):
    hist, prices, adj, through = _write_report_inputs(tmp_path, lambda days: days)
    out = m.report(hist, prices, adj, through)
    assert out["prices"]["partial_coverage"] == 0 and out["final_picks_total"] == 1
    assert out["excluded"] == {"final:horizon_not_reached": 1}                 # 달력이 20거래일보다 짧다


def test_report_stops_when_bench_adjust_is_older_than_the_price_data(tmp_path):
    """보정 자료만 오래되면 달력이 조용히 줄어 만기가 지난 픽이 '미도래'로 빠진다 — 판정 전에 멈춘다."""
    hist, prices, adj, through = _write_report_inputs(tmp_path, lambda days: days, adj_days=lambda days: days[:-2])
    with pytest.raises(SystemExit, match="달력의 마지막 거래일.*종료일.*다르다"):
        m.report(hist, prices, adj, through)


def test_report_stops_when_every_input_is_equally_stale(tmp_path):
    """종목·벤치마크·보정 자료가 모두 같은 날에서 끝나도, 그날이 종료일이 아니면 멈춘다."""
    stale = lambda days: days[:-2]  # noqa: E731
    hist, prices, adj, through = _write_report_inputs(tmp_path, stale, adj_days=stale, bench_days=stale)
    with pytest.raises(SystemExit, match="달력의 마지막 거래일.*종료일.*다르다"):
        m.report(hist, prices, adj, through)


def test_report_stops_when_calendar_starts_after_the_first_scan(tmp_path):
    """보정 자료가 스캔일보다 늦게 시작하면 진입일이 뒤로 밀린다 — 판정 전에 멈춘다."""
    late = lambda days: days[3:]  # noqa: E731
    hist, prices, adj, through = _write_report_inputs(tmp_path, lambda days: days, adj_days=late, bench_days=late)
    with pytest.raises(SystemExit, match="달력 시작.*가장 이른 스캔일.*늦다"):
        m.report(hist, prices, adj, through)


def test_history_loader_rejects_duplicate_week_or_duplicate_pick(tmp_path):
    def week(fname, scan_week, final):
        (tmp_path / fname).write_text(json.dumps({"scan_date": "2026-01-05", "scan_week": scan_week, "final": final,
                                                  "quant_passed": [], "llm_verdicts": {}}))
    week("a.json", "2026-W02", ["A", "A"])
    with pytest.raises(SystemExit, match="주간 이력 중복"):
        m.load_history(str(tmp_path), "2026-12-31")
    week("a.json", "2026-W02", ["A"])
    week("b.json", "2026-W02", ["B"])
    with pytest.raises(SystemExit, match="주간 이력 중복"):
        m.load_history(str(tmp_path), "2026-12-31")


def test_summary_records_input_fingerprints_that_follow_the_inputs(tmp_path):
    hist, prices, adj, through = _write_report_inputs(tmp_path, lambda days: days)
    a = m.report(hist, prices, adj, through)["inputs"]
    assert a == m.report(hist, prices, adj, through)["inputs"] and set(a) == {"history_sha256", "bench_adjust_sha256"}
    Path(adj).write_text(Path(adj).read_text().replace(",100\n", ",101\n", 1))        # 보정 종가 1개 변경
    b = m.report(hist, prices, adj, through)["inputs"]
    assert b["bench_adjust_sha256"] != a["bench_adjust_sha256"] and b["history_sha256"] == a["history_sha256"]
    wk = Path(hist) / "2026-W02.json"
    wk.write_text(wk.read_text().replace('"value"', '"growth"'))                        # 이력 변경
    assert m.report(hist, prices, adj, through)["inputs"]["history_sha256"] != a["history_sha256"]
    empty = Path(hist) / "2026-W03.json"                                               # 픽·심사 기록이 없는 주차 추가
    empty.write_text(json.dumps({"scan_date": through, "scan_week": "2026-W03", "final": [], "quant_passed": [], "llm_verdicts": {}}))
    before = m.report(hist, prices, adj, through)
    empty.unlink()
    after = m.report(hist, prices, adj, through)
    assert before["weeks_total"] == after["weeks_total"] + 1
    assert before["inputs"]["history_sha256"] != after["inputs"]["history_sha256"]
