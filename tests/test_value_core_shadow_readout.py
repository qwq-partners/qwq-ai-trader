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
            "llm_verdicts": {"A": {"passed": True}, "B": {"passed": False}}}))
    week("2026-W01", "2026-01-05", ["A"])
    week("2026-W02", "2026-01-12", [])
    week("2026-W03", "2026-01-19", ["A"])
    rows, weeks, empty = m.load_history(str(tmp_path), "2026-01-12")
    assert weeks == 2 and empty == ["2026-W02"]
    assert [(r["week"], r["symbol"], r["kind"]) for r in rows] == [
        ("2026-W01", "A", "final"), ("2026-W01", "B", "llm_rejected"), ("2026-W02", "B", "llm_rejected")]
