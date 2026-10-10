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


def test_thin_control_avoidance_mark_uses_absolute_return_not_excess():
    """대조군 부족 분기의 '절대 기준 회피 구간' 표시는 초과수익이 아니라 절대수익률로 판정한다 (2026-09-29)."""
    ga = GatePerformanceAnalyzer.__new__(GatePerformanceAnalyzer)

    def g(abs_ret, excess):
        return {**_g(abs_ret), "avg_excess": excess}

    gates = {"PASSED(대조군)": _g(1.0, n=29),
             "G_up_abs": g(2.0, -4.0),       # 절대 +2%·초과 -4% → 회피 표시 없음
             "G_dn_abs": g(-4.0, 1.0),       # 절대 -4%·초과 +1% → 회피 표시
             "G_edge": g(-3.0, 0.5),         # 정확히 -3% 경계 → 회피 표시
             "G_above": g(-2.99, -5.0)}      # 경계 바로 위 → 표시 없음
    lines = {v.split(":")[0].lstrip("⚠️✅➖ "): v for v in ga._build_verdicts(gates) if not v.startswith("[")}
    assert "초과 -4.00%" in lines["G_up_abs"] and "절대 기준 회피 구간" not in lines["G_up_abs"], lines["G_up_abs"]
    assert "절대 기준 회피 구간(절대 -4.00%" in lines["G_dn_abs"], lines["G_dn_abs"]
    assert "절대 기준 회피 구간(절대 -3.00%" in lines["G_edge"], lines["G_edge"]
    assert "절대 기준 회피 구간" not in lines["G_above"], lines["G_above"]


def test_size_zero_gate_is_capacity_not_selection():
    """60차: 수량 0(G3_size, |wiki 포함)은 자본·정수 수량 제약이라 '게이트 완화 검토' 대상이 아니다 — G3_risk 는 그대로."""
    ga = GatePerformanceAnalyzer.__new__(GatePerformanceAnalyzer)
    ga.horizon_days = 20
    gates = {"PASSED(대조군)": _g(1.0), "G3_size": _g(10.0), "G3_size|wiki": _g(10.0), "G3_risk": _g(10.0)}
    lines = {v.split(":")[0].lstrip("⚠️✅➖ "): v for v in ga._build_verdicts(gates) if not v.startswith("[")}
    for b in ("G3_size", "G3_size|wiki"):
        assert "용량 게이트" in lines[b] and "완화" not in lines[b], lines[b]
    assert "완화" in lines["G3_risk"], lines["G3_risk"]
