"""사이징 오버레이 회귀 테스트 (2026-08-20 — Codex 리뷰 P1 반영)

대상: 변동성 타게팅 배율 경계값 / 팀 conviction 부스트 티어 /
      밸류코어 승격 평가(동일 가중·하방 가드)

실행: venv/bin/python -m pytest tests/test_sizing_overlays.py -q
프로덕션 캐시·네트워크 무접촉 — tmp_path/합성 데이터만 사용.
"""

import json
import sys
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.utils import team_conviction, volatility_targeting  # noqa: E402
from src.analytics.shadow_lab import evaluate_vg_history  # noqa: E402


# ── 변동성 타게팅 ────────────────────────────────────────────


def _set_vol_state(tmp_path, monkeypatch, **state):
    f = tmp_path / "vol_targeting.json"
    f.write_text(json.dumps(state))
    monkeypatch.setattr(volatility_targeting, "_CACHE_FILE", f)
    volatility_targeting._mem_cache.clear()


def test_vol_targeting_no_action_below_one(tmp_path, monkeypatch):
    _set_vol_state(tmp_path, monkeypatch,
                   date=date.today().isoformat(), realized_vol=15.0, mult=1.0)
    assert volatility_targeting.vol_targeting_multiplier("sepa_trend")[0] == 1.0


def test_vol_targeting_downscale_and_floor(tmp_path, monkeypatch):
    _set_vol_state(tmp_path, monkeypatch,
                   date=date.today().isoformat(), realized_vol=75.0, mult=0.1)
    # MIN_MULT(0.4) 하한 clamp
    assert volatility_targeting.vol_targeting_multiplier("gap_and_go")[0] == \
        pytest.approx(volatility_targeting.MIN_MULT)


def test_vol_targeting_non_momentum_untouched(tmp_path, monkeypatch):
    _set_vol_state(tmp_path, monkeypatch,
                   date=date.today().isoformat(), realized_vol=75.0, mult=0.4)
    assert volatility_targeting.vol_targeting_multiplier("core_holding")[0] == 1.0


def test_vol_targeting_stale_cache_fail_open(tmp_path, monkeypatch):
    old = (date.today() - timedelta(days=10)).isoformat()
    _set_vol_state(tmp_path, monkeypatch, date=old, realized_vol=75.0, mult=0.4)
    assert volatility_targeting.vol_targeting_multiplier("sepa_trend")[0] == 1.0


def test_vol_targeting_nan_fail_open(tmp_path, monkeypatch):
    _set_vol_state(tmp_path, monkeypatch,
                   date=date.today().isoformat(), realized_vol=75.0, mult=float("nan"))
    assert volatility_targeting.vol_targeting_multiplier("sepa_trend")[0] == 1.0


# ── 팀 conviction 부스트 ─────────────────────────────────────


def _set_verdicts(tmp_path, monkeypatch, rows):
    d = tmp_path / "team_verdicts"
    d.mkdir(exist_ok=True)
    (d / f"verdicts_{date.today():%Y%m%d}.json").write_text(
        json.dumps(rows, ensure_ascii=False))
    monkeypatch.setattr(team_conviction, "VERDICT_DIR", d)
    team_conviction._cache.update({"path": None, "mtime": None, "verdicts": {}})


def _row(sym, stance, approved, conv):
    return {"symbol": sym,
            "decision": {"stance": stance, "approved": approved,
                         "proposal": {"conviction": conv}}}


def test_conviction_tiers(tmp_path, monkeypatch):
    _set_verdicts(tmp_path, monkeypatch, [
        _row("005930", "buy", True, 0.92),
        _row("000660", "buy", True, 0.80),
        _row("035420", "buy", True, 0.50),
        _row("007660", "hold", True, 0.95),
        _row("051910", "buy", False, 0.95),
    ])
    assert team_conviction.team_conviction_multiplier("005930")[0] == \
        pytest.approx(team_conviction.MULT_STRONG)
    assert team_conviction.team_conviction_multiplier("000660")[0] == \
        pytest.approx(team_conviction.MULT_BASE)
    assert team_conviction.team_conviction_multiplier("035420")[0] == 1.0  # conv 미달
    assert team_conviction.team_conviction_multiplier("007660")[0] == 1.0  # hold
    assert team_conviction.team_conviction_multiplier("051910")[0] == 1.0  # 미승인
    assert team_conviction.team_conviction_multiplier("999999")[0] == 1.0  # 미심의


def test_conviction_malformed_rows_skipped(tmp_path, monkeypatch):
    _set_verdicts(tmp_path, monkeypatch, [
        {"symbol": "005930", "decision": None},
        {"symbol": "000660", "decision": {"stance": "buy", "approved": True,
                                          "proposal": None}},
    ])
    assert team_conviction.team_conviction_multiplier("005930")[0] == 1.0
    assert team_conviction.team_conviction_multiplier("000660")[0] == 1.0


# ── 밸류코어 승격 평가 (설계 §9) ──────────────────────────────


def _series(start: str, days: int, daily_ret: float, start_px: float = 100.0):
    idx = pd.bdate_range(start, periods=days)
    px = [start_px * (1 + daily_ret) ** i for i in range(days)]
    return pd.Series(px, index=idx)


def _snaps(n_weeks: int, picks):
    base = date(2026, 6, 1)
    return [{"scan_date": (base + timedelta(weeks=i)).isoformat(), "final": picks}
            for i in range(n_weeks)]


def test_vg_promotion_pass():
    closes = {
        "069500": _series("2026-05-01", 200, 0.000),   # 벤치 무변동
        "AAA": _series("2026-05-01", 200, 0.002),      # 20일 ≈ +4%
    }
    r = evaluate_vg_history(_snaps(8, ["AAA"]), closes)
    assert r["n_eval_weeks"] >= 6 and r["ok"] is True
    assert r["mean_week_excess"] > 0


def test_vg_downside_gate_blocks():
    # 평균은 우위지만 픽 절반이 -20% 급락 → 하위25% < -15% → 승격 불가 (§9-3)
    closes = {
        "069500": _series("2026-05-01", 200, 0.000),
        "AAA": _series("2026-05-01", 200, 0.010),      # 20일 ≈ +22%
        "BBB": _series("2026-05-01", 200, -0.011),     # 20일 ≈ -20%
    }
    r = evaluate_vg_history(_snaps(8, ["AAA", "BBB"]), closes)
    assert r["n_eval_weeks"] >= 6
    assert r["q25_raw"] < -15.0
    assert r["ok"] is False


def test_vg_equal_weight_weeks():
    # 종목 수가 다른 주가 있어도 주 단위 동일 가중 (pooled 평균 금지 검증)
    closes = {
        "069500": _series("2026-05-01", 200, 0.000),
        "UP": _series("2026-05-01", 200, 0.002),       # +4%
        "DN": _series("2026-05-01", 200, -0.001),      # -2%
    }
    snaps = _snaps(6, ["UP"]) + _snaps(2, ["DN", "DN", "DN"])
    r = evaluate_vg_history(snaps, closes)
    # pooled면 DN 6표본이 지배(평균 음수), 주 가중이면 (6주 +4% + 2주 -2%)/8 > 0
    assert r["mean_week_excess"] > 0


def test_vg_insufficient_forward_window():
    closes = {"069500": _series("2026-08-01", 10, 0.0),
              "AAA": _series("2026-08-01", 10, 0.001)}
    r = evaluate_vg_history(_snaps(8, ["AAA"]), closes)
    assert r["n_eval_weeks"] == 0 and r["ok"] is False
