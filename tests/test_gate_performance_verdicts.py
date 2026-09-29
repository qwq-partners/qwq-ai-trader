"""차단 게이트 판정 — 대조군 표본 부족 시 '통과 +0.00%' 가상값과 비교하지 않는다 (2026-09-29)."""
from src.analytics.gate_performance import GatePerformanceAnalyzer


def _g(avg, n=30):
    return {"samples": n, "avg_return": avg, "avg_excess": avg, "avg_clipped": avg,
            "median_return": avg, "opportunity_loss_cnt": 0, "opportunity_loss_pct": 40.0,
            "avoided_cnt": 0, "avoided_pct": 10.0, "best": None, "worst": None}


def _verdicts(control_n):
    ga = GatePerformanceAnalyzer.__new__(GatePerformanceAnalyzer)
    gates = {"PASSED(대조군)": _g(1.0, n=control_n), "G2_cross": _g(5.0), "G1_regime": _g(-5.0),
             "G3_flat": _g(-1.0)}
    return {v.split(":")[0].lstrip("⚠️✅➖ "): v for v in ga._build_verdicts(gates) if not v.startswith("[")}


def test_thin_control_holds_comparison_verdicts():
    lines = _verdicts(29)
    assert set(lines) == {"G2_cross", "G1_regime", "G3_flat"}
    for line in lines.values():
        assert "+0.00%" not in line and "통과 " not in line, line
        assert "대조군 표본 부족" in line and "비교 판정 보류" in line, line
        assert not any(w in line for w in ("완화 검토", "선별 효과", "효과 불명확")), line
    assert "절대 기준 회피 구간" in lines["G1_regime"]            # 절대 수익률만 쓰는 사실은 유지
    assert "절대 기준 회피 구간" not in lines["G2_cross"] + lines["G3_flat"]


def test_enough_control_keeps_existing_verdicts():
    lines = _verdicts(30)
    assert "완화 검토" in lines["G2_cross"] and "> 통과 +1.00%" in lines["G2_cross"]
    assert "선별 효과" in lines["G1_regime"] and "< 통과 +1.00%" in lines["G1_regime"]
    assert "효과 불명확" in lines["G3_flat"]
    assert not any("표본 부족" in v for v in lines.values())
