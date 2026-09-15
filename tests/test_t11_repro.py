"""T11 독립 재현 — 계획서 §4 인수 조건 #11·#12·#13·#14 (담당 D, 2026-09-15).

계약(types.py)·원장(team_ledger.py)·CF(counterfactual_tracker.py)·러너(team_policy_ab.py)로
검증 가능한 항목만 다룬다. 구현자(A/B/C)의 테스트 파일과는 별도로, 합성 입력을 직접 구성해
같은 결론에 도달하는지 확인한다 — 구현자 단정을 그대로 믿지 않는다.

#1~#10 은 통합 후 별도 D-통합 단계에서 검증한다(여기서 하지 않는다).
운영 캐시 무접촉(tmp_path 전부 주입).
"""

from __future__ import annotations

import json
import sys
from datetime import date as _date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import src.agents.team_ledger as ledger_mod  # noqa: E402
import src.analytics.counterfactual_tracker as cf_mod  # noqa: E402
from src.agents.types import TeamAssessment  # noqa: E402
import team_policy_ab as tpab  # noqa: E402


# ── #11: 같은 날 같은 종목 복수 판단 · 승인 BUY 누락 없이 추적 ──────────────

def test_11_same_symbol_same_day_multiple_deliberations_all_preserved(tmp_path):
    """장중 10:30/14:00 두 번 심의된 같은 종목 — last-write-wins 로 하나가 사라지면 안 된다."""
    ldir = tmp_path / "team_ledger"
    morning = {"symbol": "005930", "decided_at": "2026-09-15T10:30:00", "slot": "10:30",
               "input_snapshot_hash": "snap-am", "decision": {"stance": "hold", "approved": True}}
    afternoon = {"symbol": "005930", "decided_at": "2026-09-15T14:00:00", "slot": "14:00",
                 "input_snapshot_hash": "snap-pm", "decision": {"stance": "buy", "approved": True}}
    ledger_mod.append_deliberation(morning, ledger_dir=ldir)
    ledger_mod.append_deliberation(afternoon, ledger_dir=ldir)

    hist = ledger_mod.history_for_symbol("005930", "2026-09-15", ledger_dir=ldir)
    assert [h["slot"] for h in hist] == ["10:30", "14:00"], "같은 날 두 시점 판단이 모두 남아야 한다"
    assert len(ledger_mod.load_day("2026-09-15", ledger_dir=ldir)) == 2


def test_11_approved_buy_without_fill_is_never_silently_dropped(tmp_path, monkeypatch):
    """승인 BUY 후보가 체결 증거 없이 CF 추적에서 그냥 사라지면 안 된다(team_buy_unfilled)."""
    tv_dir = tmp_path / "team_verdicts"
    tv_dir.mkdir()
    (tv_dir / "verdicts_20260915.json").write_text(json.dumps([
        {"symbol": "005930", "decision": {"approved": True, "stance": "buy"}},
    ]), encoding="utf-8")
    monkeypatch.setattr(cf_mod, "_TEAM_VERDICT_DIR", tv_dir)
    monkeypatch.setattr(cf_mod, "_SOURCES", {})
    # 콜백 미주입 폴백(거래저널 디렉터리)이 운영 캐시를 건드리지 않도록 tmp 경로로 격리
    (tmp_path / "journal").mkdir(exist_ok=True)
    monkeypatch.setattr(cf_mod, "_TRADE_JOURNAL_DIR", tmp_path / "journal")  # 빈 저널 = 그날 진입 없음

    tracker = object.__new__(cf_mod.CounterfactualTracker)
    tracker._state = {}
    tracker._fill_evidence_check = None
    tracker._ingest_sources()

    assert len(tracker._state) == 1, "승인 BUY 가 어느 소스로도 등록되지 않고 사라졌다"
    assert list(tracker._state.values())[0]["source"] == "team_buy_unfilled"


# ── #12: 중복 이벤트 · 재시도가 원장 · 표본을 부풀리지 않음 ────────────────

def test_12_retry_with_identical_input_does_not_inflate_ledger_or_cf(tmp_path, monkeypatch):
    ldir = tmp_path / "team_ledger"
    row = {"symbol": "005930", "decided_at": "2026-09-15T10:30:00", "slot": "10:30",
           "input_snapshot_hash": "same-hash", "decision": {"stance": "buy", "approved": True}}
    for _ in range(5):  # 스케줄러 재시작·재시도를 흉내 — 같은 입력 5회
        ledger_mod.append_deliberation(dict(row), ledger_dir=ldir)
    rows = ledger_mod.load_day("2026-09-15", ledger_dir=ldir)
    assert len(rows) == 1, "동일 입력 재시도가 원장 표본을 부풀렸다"

    tv_dir = tmp_path / "team_verdicts"
    tv_dir.mkdir()
    # 같은 verdicts 파일을 두 번 읽는 상황(예: 파일 mtime 변경 없이 재실행)을 흉내
    (tv_dir / "verdicts_20260915.json").write_text(json.dumps([
        {"symbol": "005930", "decision": {"approved": True, "stance": "buy"}},
    ]), encoding="utf-8")
    monkeypatch.setattr(cf_mod, "_TEAM_VERDICT_DIR", tv_dir)
    monkeypatch.setattr(cf_mod, "_SOURCES", {})
    # 콜백 미주입 폴백(거래저널 디렉터리)이 운영 캐시를 건드리지 않도록 tmp 경로로 격리
    (tmp_path / "journal").mkdir(exist_ok=True)
    monkeypatch.setattr(cf_mod, "_TRADE_JOURNAL_DIR", tmp_path / "journal")  # 빈 저널 = 그날 진입 없음
    tracker = object.__new__(cf_mod.CounterfactualTracker)
    tracker._state = {}
    tracker._fill_evidence_check = None
    first = tracker._ingest_sources()
    second = tracker._ingest_sources()
    third = tracker._ingest_sources()
    assert (first, second, third) == (1, 0, 0), "CF 재수집이 같은 항목을 중복 등록했다"
    assert len(tracker._state) == 1


# ── #13: A/B/C 동일 후보 · 시점 · 위험 · 청산 · 비용 비교 ──────────────────

def _mk_candidate(symbol: str, day: str = "2026-08-01"):
    votes = {"r1": {"bull": True, "bear": True}, "r2": {"bull": True, "bear": True}}
    prices = []
    px = 10000.0
    d0 = _date.fromisoformat(day)
    for i in range(25):
        d = d0 + timedelta(days=i)
        prices.append({"date": d.isoformat(), "open": px, "high": px * 1.03, "low": px * 0.97,
                        "close": px * 1.01})
        px *= 1.01
    return tpab.Candidate(
        date=day, symbol=symbol, strategy="sepa_trend", setup="sepa_pullback",
        plan={"score": 80, "stop_price": 9500.0, "entry_band_low": 0, "max_entry_price": 11000.0,
              "trigger": {}, "entry_mode": "close"},
        # data_status/evidence(kind=fact) 채움 — judgment.assess 가 "A 근거계약 미배선" abstain
        # 폴백 대신 실제 근거 기반 merit 을 계산하게 한다(D 3차, 정책 B/C 재배선 §2.5).
        evidence=[{"kind": "technical", "score": 20, "confidence": 0.7, "positive_basis": True, "error": None,
                   "data_status": "full",
                   "evidence": [{"source": "t", "metric": "score", "value": 20, "status": "full", "kind": "fact"}]}],
        votes=votes, prices=prices, synthetic=True,
    )


def test_13_policies_see_identical_candidate_universe_and_selection_never_reads_outcome(monkeypatch):
    """정책 A/B/C 는 같은 날짜·후보 풀에서 출발해야 하고, 선정 함수는 prices(결과)를
    전혀 들여다보지 않아야 한다 — 사후 선별을 원천적으로 배제한다."""
    rows = [_mk_candidate("000001"), _mk_candidate("000002"), _mk_candidate("000003")]
    # select_candidates 소스에 "prices"/"net_pct"/"r " 같은 결과 지표 참조가 없어야 한다(정적 확인)
    import inspect
    src = inspect.getsource(tpab.select_candidates) + inspect.getsource(tpab.gate_a) \
        + inspect.getsource(tpab.gate_b) + inspect.getsource(tpab.gate_c)
    for forbidden in (".prices", "net_pct", "exit_reason"):
        assert forbidden not in src, f"선정 로직이 결과({forbidden})를 참조하면 사후 선별이 된다"

    # 세 정책의 "입력 후보 풀"(게이트 적용 전) 자체가 실제로 동일한지 계측 — select_candidates 가
    # 받는 rows 는 정책과 무관하게 항상 같은 객체다. 게이트를 전부 통과시켜 세 정책의 최종 선정
    # 결과까지 동일해지는지도 함께 확인한다(구현이 정책별로 다른 입력을 몰래 넣으면 여기서 깨진다).
    monkeypatch.setattr(tpab, "GATES", {"A": lambda c: True, "B": lambda c: True, "C": lambda c: True})
    selections = {p: [c.symbol for c in tpab.select_candidates(rows, p, max_new=5)] for p in ("A", "B", "C")}
    assert selections["A"] == selections["B"] == selections["C"] == ["000001", "000002", "000003"]


def test_13_exit_and_cost_assumptions_identical_across_policies(tmp_path):
    """정책이 달라도 청산 사다리·비용 가정은 하나의 시뮬레이터(simulate_exit/_net_pct)로 고정된다."""
    snap = tmp_path / "snap.jsonl"
    rows = [_mk_candidate("000001"), _mk_candidate("000002")]
    snap.write_text("\n".join(json.dumps({
        "date": c.date, "symbol": c.symbol, "plan": c.plan, "evidence": c.evidence,
        "votes": c.votes, "prices": c.prices, "synthetic": True,
    }) for c in rows), encoding="utf-8")
    loaded = tpab.load_snapshot(snap)
    results = tpab.run_selection_experiment(loaded, ["A", "B", "C"], max_new=5)
    for policy in ("A", "B", "C"):
        assert results[policy]["completed_positions"] == 2
        # 같은 입력이면 median_r 도 같아야 한다 — 정책별로 다른 청산·비용 가정을 쓰지 않는다는 증거
        assert results[policy]["median_r"] == results["A"]["median_r"]


# ── #14: 검증 자료 없을 때 성공확률 · 개선 수치 생성 금지 ──────────────────

def test_14_team_assessment_never_fabricates_success_probability_by_default():
    a = TeamAssessment(symbol="005930")
    assert a.success_probability is None
    assert a.calibration_status == "uncalibrated"
    d = a.to_dict()
    assert d["success_probability"] is None and d["calibration_status"] == "uncalibrated"


def test_14_r1_assess_abstains_without_fabricating_a_score_when_no_usable_evidence():
    """근거가 전혀 없거나 전부 confidence=0/오류면 merit_score=None(abstain) — 0점도 만들면 안 된다."""
    merit, status, data_suff = tpab.r1_assess([])
    assert merit is None and status == "abstain"
    all_zero_conf = [{"kind": "technical", "score": 50, "confidence": 0.0, "positive_basis": True, "error": None}]
    merit2, status2, _ = tpab.r1_assess(all_zero_conf)
    assert merit2 is None and status2 == "abstain", "confidence=0 근거로 점수를 만들어내면 안 된다"


def test_14_offline_runner_reports_no_improvement_numbers_without_completed_positions(tmp_path):
    """완결 포지션이 하나도 없으면 median_r/mean_r 은 None 이어야 한다 — '개선됐다'는 수치를 지어내면 안 된다."""
    snap = tmp_path / "snap.jsonl"
    empty_candidate = tpab.Candidate(date="2026-08-01", symbol="000001",
                                      plan={"score": 80, "stop_price": 9500.0}, evidence=[], votes={},
                                      prices=[], synthetic=True)  # prices 없음 → 체결 불가
    snap.write_text(json.dumps({
        "date": empty_candidate.date, "symbol": empty_candidate.symbol, "plan": empty_candidate.plan,
        "evidence": [], "votes": {}, "prices": [], "synthetic": True,
    }), encoding="utf-8")
    rows = tpab.load_snapshot(snap)
    results = tpab.run_selection_experiment(rows, ["A"], max_new=5)
    assert results["A"]["completed_positions"] == 0
    assert results["A"]["median_r"] is None and results["A"]["mean_r"] is None
    assert results["A"]["sample_requirement_met"] is False
