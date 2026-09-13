"""위험 기반 사이징 canary 오프라인 리포트 (scripts/review_risk_canary.py, 계획서 T8).

합성 원장 fixture 를 tmp_path 에 만들어 main(argv) 를 직접 호출한다 (subprocess 없음).
네트워크·운영 상태 파일 무접촉.

실행: venv/bin/python -m pytest tests/test_risk_canary_report.py -q -p no:cacheprovider
"""

import importlib.util
import json
import sys
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load():
    spec = importlib.util.spec_from_file_location("_review_risk_canary_for_test",
                                                  ROOT / "scripts" / "review_risk_canary.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


canary = _load()

COHORT = "risk-sepa-v1"
SHA = "abc1234"


# ── fixture 빌더 ────────────────────────────────────────────────────────────────

def _pos(pid, sells, *, buy_price="10000", qty=100, buy_fee="0", stop="5.0", actual_stop=None,
         entry_date="2026-09-01", status="closed", cohort=COHORT, sha=SHA, sizing_mode="risk",
         entry_risk=..., initial_risk=..., net_pnl=..., lots_ambiguous=False, budget="70000",
         planned_risk=None):
    """sells = [(date, qty, price, fee), ...]. net_pnl·initial_risk 는 손계산과 같은 식으로 생성."""
    cost = Decimal(buy_price) * qty
    fills = [{"ts": f"{entry_date}T09:05:00+09:00", "side": "buy", "price": buy_price,
              "quantity": qty, "fee": buy_fee}]
    exits = []
    proceeds, sell_fees = Decimal("0"), Decimal("0")
    for d, q, price, fee in sells:
        row = {"ts": f"{d}T14:30:00+09:00", "price": price, "quantity": q, "fee": fee}
        fills.append({**row, "side": "sell"})
        exits.append({**row, "reason": "test"})
        proceeds += Decimal(price) * q
        sell_fees += Decimal(fee)
    actual = actual_stop if actual_stop is not None else stop
    if entry_risk is ...:
        entry_risk = {
            "version": 1, "cohort_id": cohort, "sizing_mode": sizing_mode, "strategy": "sepa_trend",
            "stop_basis": "net_pnl", "stop_pct": stop, "stop_source": "strategy",
            "equity_at_decision": "10000000", "risk_budget_amount": budget,
            "planned_price": buy_price, "planned_quantity": qty,
            "planned_risk_amount": planned_risk if planned_risk is not None else str(cost * Decimal(stop) / 100),
        }
    if initial_risk is ...:
        initial_risk = str(cost * Decimal(actual) / 100)
    if net_pnl is ...:
        net_pnl = None if status == "open" else str(proceeds - cost - Decimal(buy_fee) - sell_fees)
    return {
        "position_id": pid, "symbol": "005930", "strategy": "sepa_trend", "cohort_id": cohort,
        "applied_sha": sha, "status": status, "entry_risk": entry_risk,
        "initial_risk_amount": initial_risk, "planned_vs_filled_risk_delta": None,
        "fills": fills, "exits": exits, "net_pnl": net_pnl, "actual_stop_pct": actual,
        "lots_ambiguous": lots_ambiguous,
    }


def _win(pid, date="2026-09-05"):   # +80,000 → R +1.6 (수수료 0)
    return _pos(pid, [(date, 100, "10800", "0")])


def _loss(pid, date="2026-09-05"):  # −50,000 → R −1.0
    return _pos(pid, [(date, 100, "9500", "0")])


def _write(tmp_path, positions, name="ledger.json"):
    p = tmp_path / name
    p.write_text(json.dumps({"version": 1, "generated_at": "2026-10-01T18:00:00+09:00",
                             "positions": positions}, ensure_ascii=False), encoding="utf-8")
    return p


def _run(tmp_path, ledger, *extra, cohort=COHORT):
    out = tmp_path / "report.json"
    rc = canary.main(["--input", str(ledger), "--cohort", cohort, "--output", str(out), *extra])
    report = json.loads(out.read_text(encoding="utf-8")) if out.exists() else None
    return rc, report, out


# ── (1) 파일 누락 / JSON 파싱 실패 → exit 2 + 부족 항목 ──────────────────────────

def test_missing_input_exits_2_with_reason(tmp_path, capsys):
    rc, report, out = _run(tmp_path, tmp_path / "nope.json")
    assert rc == 2 and report is None and not out.exists()
    assert "입력 원장 파일 없음" in capsys.readouterr().err


def test_broken_json_exits_2(tmp_path, capsys):
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    rc, _, _ = _run(tmp_path, bad)
    assert rc == 2
    assert "JSON 파싱 실패" in capsys.readouterr().err


# ── (2) 필수 필드 누락 → exit 2, 어떤 필드인지 출력 ──────────────────────────────

def test_missing_required_field_exits_2(tmp_path, capsys):
    positions = [_win("P1"), _win("P2"), _win("P3")]
    del positions[1]["position_id"]
    del positions[2]["fills"]
    positions[0]["status"] = "weird"
    rc, report, _ = _run(tmp_path, _write(tmp_path, positions))
    err = capsys.readouterr().err
    assert rc == 2 and report is None
    assert "positions[1].position_id 누락" in err
    assert "positions[2].fills 누락" in err
    assert "positions[0].status 값 오류" in err


def test_closed_without_net_pnl_is_structural_error(tmp_path, capsys):
    rc, _, _ = _run(tmp_path, _write(tmp_path, [_pos("P1", [("2026-09-05", 100, "10800", "0")], net_pnl=None)]))
    assert rc == 2
    assert "positions[0].net_pnl 누락" in capsys.readouterr().err


# ── (3) 완결 12건 → insufficient_sample + 제외 사유 집계 ──────────────────────────

def test_insufficient_sample_with_exclusion_counts(tmp_path):
    positions = [_win(f"W{i}", date=f"2026-09-{5 + i:02d}") for i in range(12)]
    positions += [
        _pos("O1", [], status="open"), _pos("O2", [], status="open"),
        _pos("L1", [("2026-09-05", 100, "10800", "0")], entry_risk=None),
        _pos("L2", [("2026-09-05", 100, "10800", "0")], sizing_mode="nominal"),
        _pos("A1", [("2026-09-05", 100, "10800", "0")], lots_ambiguous=True),
        _pos("M1", [("2026-09-05", 100, "10800", "0")], initial_risk=None),
        _pos("C1", [("2026-09-05", 100, "10800", "0")], cohort="other-cohort"),
        _pos("C2", [("2026-09-05", 100, "10800", "0")], sha="zzz9999"),
    ]
    rc, report, _ = _run(tmp_path, _write(tmp_path, positions), "--sha", SHA)
    assert rc == 0
    assert report["status"] == "insufficient_sample"
    assert report["technical_status"] == "passed"
    assert report["sample"] == {"closed": 12, "excluded": {
        "open": 2, "legacy_unmeasured": 2, "missing_initial_risk": 1, "lots_ambiguous": 1, "cohort_mismatch": 2}}
    assert report["metrics"]["n"] == 12
    assert Decimal(report["metrics"]["avg_r"]) == Decimal("1.6")
    assert report["metrics"]["avg_excess_return"] is None
    assert report["applied_sha"] == SHA and report["cohort"] == COHORT
    assert any("표본 부족" in n for n in report["notes"])

    # --as-of: 그 날짜 이후 완결은 미완결로 취급
    rc, report, _ = _run(tmp_path, _write(tmp_path, positions), "--sha", SHA, "--as-of", "2026-09-08")
    assert rc == 0 and report["sample"]["closed"] == 4 and report["sample"]["excluded"]["open"] == 10
    assert report["as_of"] == "2026-09-08"


def test_zero_sample_still_exit_0(tmp_path):
    rc, report, _ = _run(tmp_path, _write(tmp_path, [_pos("O1", [], status="open")]))
    assert rc == 0 and report["status"] == "insufficient_sample" and report["metrics"] == {"n": 0}


# ── (4)(5) 완결 30건 판정 ────────────────────────────────────────────────────────

def test_thirty_good_is_further_review_never_recommends_mode_switch(tmp_path):
    positions = [_win(f"W{i}") for i in range(20)] + [_loss(f"L{i}") for i in range(10)]
    rc, report, out = _run(tmp_path, _write(tmp_path, positions))
    assert rc == 0
    assert report["status"] == "further_review" and report["technical_status"] == "passed"
    m = report["metrics"]
    assert m["n"] == 30
    assert Decimal(m["profit_factor"]) == Decimal("3.2")            # 1,600,000 / 500,000
    assert Decimal(m["avg_r"]) == Decimal("0.733333")               # (20×1.6 − 10)/30
    assert Decimal(m["win_rate"]) == Decimal("0.666667")
    assert "nominal" not in out.read_text(encoding="utf-8")


def test_thirty_pf_below_one_is_hold_expansion(tmp_path):
    positions = [_win(f"W{i}") for i in range(10)] + [_loss(f"L{i}") for i in range(20)]
    rc, report, _ = _run(tmp_path, _write(tmp_path, positions))
    assert rc == 0 and report["status"] == "hold_expansion"
    assert Decimal(report["metrics"]["profit_factor"]) == Decimal("0.8")
    assert Decimal(report["metrics"]["avg_r"]) < 0


def test_thirty_positive_but_below_benchmark_is_hold_expansion(tmp_path):
    (tmp_path / "bench.csv").write_text("date,close\n2026-09-01,100\n2026-09-05,105\n", encoding="utf-8")
    positions = [_pos(f"W{i}", [("2026-09-05", 100, "10100", "0")]) for i in range(30)]  # +1% < 벤치 +5%
    rc, report, _ = _run(tmp_path, _write(tmp_path, positions), "--benchmark", str(tmp_path / "bench.csv"))
    assert rc == 0 and report["status"] == "hold_expansion"
    assert report["metrics"]["profit_factor"] is None                # 손실 0건 → PF 미정의, 통과 처리 아님
    assert Decimal(report["metrics"]["avg_excess_return"]) == Decimal("-0.04")
    assert report["metrics"]["benchmark_covered"] == 30


def test_min_sample_override(tmp_path):
    positions = [_win(f"W{i}") for i in range(4)] + [_loss("L1")]
    rc, report, _ = _run(tmp_path, _write(tmp_path, positions), "--min-sample", "5")
    assert rc == 0 and report["status"] == "further_review" and report["min_sample"] == 5


# ── (6) 벤치마크: 없음 → null, 있음 → 부분청산 가중 손계산 ──────────────────────

def test_benchmark_missing_gives_null_excess(tmp_path):
    ledger = _write(tmp_path, [_win("W1")])
    rc, report, _ = _run(tmp_path, ledger, "--benchmark", str(tmp_path / "absent.csv"))
    assert rc == 0
    assert report["metrics"]["avg_excess_return"] is None
    assert report["benchmark"]["missing_reason"].startswith("benchmark_missing")
    assert any("초과수익 미측정" in n for n in report["notes"])


def test_benchmark_weighted_by_exit_quantity(tmp_path):
    bench = tmp_path / "kodex.csv"
    bench.write_text("date,close\n2026-09-01,100\n2026-09-02,101\n2026-09-03,102\n2026-09-04,103\n2026-09-05,105\n",
                     encoding="utf-8")
    # 매수 100@10000 (수수료 140), 40주 @10500 on 09-03 (895), 60주 @11000 on 09-05 (1406)
    # net = 420,000 + 660,000 − 1,000,000 − 140 − 895 − 1406 = 77,559 → 수익률 0.077559
    # bench = 0.4×(102/100−1) + 0.6×(105/100−1) = 0.008 + 0.030 = 0.038 → 초과 0.039559
    pos = _pos("B1", [("2026-09-03", 40, "10500", "895"), ("2026-09-05", 60, "11000", "1406")], buy_fee="140")
    assert pos["net_pnl"] == "77559"
    rc, report, _ = _run(tmp_path, _write(tmp_path, [pos]), "--benchmark", str(bench))
    assert rc == 0 and report["technical_status"] == "passed"
    m = report["metrics"]
    assert Decimal(m["avg_return"]) == Decimal("0.077559")
    assert Decimal(m["avg_excess_return"]) == Decimal("0.039559")
    assert Decimal(m["total_fees"]) == Decimal("2441")
    assert report["benchmark"] == {"source": str(bench), "missing_reason": None}

    # 청산일이 벤치마크에 없으면 그 포지션은 미산출 (0 대체 없음)
    pos2 = _pos("B2", [("2026-09-20", 100, "10800", "0")])
    rc, report, _ = _run(tmp_path, _write(tmp_path, [pos, pos2]), "--benchmark", str(bench))
    assert rc == 0 and report["metrics"]["benchmark_covered"] == 1
    assert Decimal(report["metrics"]["avg_excess_return"]) == Decimal("0.039559")
    assert any("benchmark_date_missing" in n for n in report["notes"])


# ── (7) 기술 검증 — 각각 technical failed, 성과 status 와 분리 ───────────────────

def _issue_kinds(report):
    return {(i["position_id"], i["issue"]) for i in report["technical_issues"]}


def test_duplicate_position_id_fails_technical(tmp_path):
    rc, report, _ = _run(tmp_path, _write(tmp_path, [_win("P1"), _win("P1"), _win("P2")]))
    assert rc == 0 and report["technical_status"] == "failed"
    assert _issue_kinds(report) == {("P1", "duplicate_position_id")}
    assert report["status"] == "insufficient_sample"                 # 성과 판정은 별도


def test_planned_risk_over_budget_fails_technical(tmp_path):
    pos = _pos("P1", [("2026-09-05", 100, "10800", "0")], budget="40000")   # planned 50,000 > 40,000
    rc, report, _ = _run(tmp_path, _write(tmp_path, [pos]))
    assert rc == 0 and _issue_kinds(report) == {("P1", "planned_risk_exceeds_budget")}


def test_stop_interpretation_mismatch_fails_technical(tmp_path):
    pos = _pos("P1", [("2026-09-05", 100, "10800", "0")], stop="5.0", actual_stop="4.0")
    rc, report, _ = _run(tmp_path, _write(tmp_path, [pos]))
    assert rc == 0 and _issue_kinds(report) == {("P1", "stop_pct_mismatch")}   # 실제 SL 기준 재계산은 정합


def test_initial_risk_and_net_pnl_recompute_mismatch(tmp_path):
    bad_ir = _pos("P1", [("2026-09-05", 100, "10800", "0")], initial_risk="12345")
    bad_net = _pos("P2", [("2026-09-05", 100, "10800", "0")], net_pnl="1")
    no_fee = _pos("P3", [("2026-09-05", 100, "10800", "0")])
    no_fee["fills"][1]["fee"] = None                                       # 수수료 없음 → 재계산 불가
    missing = _pos("P4", [("2026-09-05", 100, "10800", "0")])
    del missing["entry_risk"]["stop_pct"]
    missing["actual_stop_pct"] = None
    rc, report, _ = _run(tmp_path, _write(tmp_path, [bad_ir, bad_net, no_fee, missing]))
    assert rc == 0 and report["technical_status"] == "failed"
    assert _issue_kinds(report) == {("P1", "initial_risk_mismatch"), ("P2", "net_pnl_mismatch"),
                                    ("P3", "net_pnl_unverifiable"), ("P4", "missing_field")}
    detail = next(i["detail"] for i in report["technical_issues"] if i["position_id"] == "P4")
    assert "stop_pct" in detail and "actual_stop_pct" in detail


def test_technical_check_ignores_other_cohort_and_legacy(tmp_path):
    other = _pos("X1", [("2026-09-05", 100, "10800", "0")], cohort="other", budget="1")
    legacy = _pos("X2", [("2026-09-05", 100, "10800", "0")], entry_risk=None, initial_risk="1")
    rc, report, _ = _run(tmp_path, _write(tmp_path, [other, legacy, _win("W1")]))
    assert rc == 0 and report["technical_status"] == "passed"


# ── (8) R·PF·상위 3건 제외·연패·보유일 손계산 ───────────────────────────────────

def test_metrics_match_hand_calculation(tmp_path):
    # 전부 매수 100@10000, 초기 위험 50,000. 청산일 순서: P1 → P2 → P4 → P3 → P5
    positions = [
        _pos("P1", [("2026-09-02", 100, "10800", "0")]),   # +80,000  R 1.6
        _pos("P2", [("2026-09-03", 100, "9500", "0")]),    # −50,000  R −1.0
        _pos("P4", [("2026-09-04", 100, "9700", "0")]),    # −30,000  R −0.6
        _pos("P3", [("2026-09-05", 100, "11000", "0")]),   # +100,000 R 2.0
        _pos("P5", [("2026-09-06", 100, "10250", "0")]),   # +25,000  R 0.5
    ]
    rc, report, _ = _run(tmp_path, _write(tmp_path, positions))
    assert rc == 0 and report["technical_status"] == "passed"
    m = {k: (Decimal(v) if isinstance(v, str) else v) for k, v in report["metrics"].items()}
    assert m["n"] == 5
    assert m["avg_r"] == Decimal("0.5")                       # (1.6−1.0−0.6+2.0+0.5)/5
    assert m["median_r"] == Decimal("0.5")
    assert m["profit_factor"] == Decimal("2.5625")            # 205,000 / 80,000
    assert m["total_net_pnl"] == Decimal("125000")
    assert m["net_pnl_excl_top3"] == Decimal("-80000")        # 125,000 − (100,000+80,000+25,000)
    assert m["win_rate"] == Decimal("0.6")
    assert m["max_consecutive_losses"] == 2                   # P2 → P4 연속
    assert m["avg_holding_days"] == Decimal("3")              # 1+2+3+4+5 / 5
    assert m["total_fees"] == Decimal("0")
    assert report["metrics"]["initial_risk_deviation"] == {"avg_delta_pct": "0.000000",
                                                           "max_abs_delta_pct": "0.000000", "covered": 5}
