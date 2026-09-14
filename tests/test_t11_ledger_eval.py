"""T11 담당 D — 원장·화면·CF·평가 도구 (2026-09-15).

- counterfactual_tracker E1: 승인 BUY 가 체결 증거 없이 조용히 제외되지 않고
  team_buy_unfilled 로 추적됨(멱등), 체결 증거가 있으면 기존처럼 건너뜀.
- kr_api.get_team_verdicts: assessment 요약·execution_state·conviction_label 노출,
  원장이 order_submitted/filled 라고 해도 실제 체결 증거 없으면 shadow_ready 로 낮춤.
- scripts/team_policy_ab.py: 정책 A/B/C 가 같은 후보 풀에서 다른 선정을 내고,
  timing 실험은 EntryPlan 정본 검증기(check_entry_plan)를 그대로 재사용하며,
  합성 스냅샷은 validation_status="synthetic_only" 로 표시된다.

전부 tmp_path 로 격리 — 운영 캐시(~/.cache/ai_trader) 무접촉.
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import date as _date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiohttp.test_utils import make_mocked_request

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import src.analytics.counterfactual_tracker as cf_mod  # noqa: E402
import src.agents.team_ledger as ledger_mod  # noqa: E402
import src.agents.team as team_mod  # noqa: E402
from src.dashboard.kr_api import KRAPIHandler  # noqa: E402
import team_policy_ab as tpab  # noqa: E402


# ── 1) CounterfactualTracker E1 ──────────────────────────────────────────

def _write_verdicts(base: Path, day8: str, rows: list) -> None:
    base.mkdir(parents=True, exist_ok=True)
    (base / f"verdicts_{day8}.json").write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")


def test_approved_buy_without_fill_evidence_is_tracked_not_dropped(tmp_path, monkeypatch):
    """기존 동작: 승인 BUY 는 조용히 제외됐다. T11 E1 은 체결 증거가 없으면
    team_buy_unfilled 로 등록해야 한다(무증거=미체결이 기본, 보수적)."""
    tv_dir = tmp_path / "team_verdicts"
    monkeypatch.setattr(cf_mod, "_TEAM_VERDICT_DIR", tv_dir)
    monkeypatch.setattr(cf_mod, "_SOURCES", {})  # rule11/12 소스는 이 테스트와 무관
    _write_verdicts(tv_dir, "20260915", [
        {"symbol": "005930", "decision": {"approved": True, "stance": "buy"}},
        {"symbol": "000660", "decision": {"approved": True, "stance": "hold"}},  # 기존 team_hold 경로
    ])
    tracker = object.__new__(cf_mod.CounterfactualTracker)
    tracker._state = {}
    tracker._fill_evidence_check = None  # 기본값 — 콜백 미주입

    added = tracker._ingest_sources()

    assert added == 2
    unfilled = [v for v in tracker._state.values() if v["source"] == "team_buy_unfilled"]
    holds = [v for v in tracker._state.values() if v["source"] == "team_hold"]
    assert len(unfilled) == 1 and unfilled[0]["symbol"] == "005930"
    assert len(holds) == 1 and holds[0]["symbol"] == "000660"
    # 재수집은 표본을 부풀리지 않는다(멱등)
    assert tracker._ingest_sources() == 0
    assert len(tracker._state) == 2


def test_approved_buy_with_fill_evidence_is_skipped_like_before(tmp_path, monkeypatch):
    """체결 증거 콜백이 True 를 돌려주면 기존과 동일하게 CF 등록 없이 건너뛴다."""
    tv_dir = tmp_path / "team_verdicts"
    monkeypatch.setattr(cf_mod, "_TEAM_VERDICT_DIR", tv_dir)
    monkeypatch.setattr(cf_mod, "_SOURCES", {})
    _write_verdicts(tv_dir, "20260915", [
        {"symbol": "005930", "decision": {"approved": True, "stance": "buy"}},
    ])
    tracker = object.__new__(cf_mod.CounterfactualTracker)
    tracker._state = {}
    tracker._fill_evidence_check = lambda sym, day: sym == "005930"

    added = tracker._ingest_sources()

    assert added == 0
    assert tracker._state == {}


def test_fill_evidence_callback_exception_is_conservative(tmp_path, monkeypatch):
    """콜백이 예외를 내도 CF 추적이 죽지 않고 '증거 없음'으로 처리한다."""
    tv_dir = tmp_path / "team_verdicts"
    monkeypatch.setattr(cf_mod, "_TEAM_VERDICT_DIR", tv_dir)
    monkeypatch.setattr(cf_mod, "_SOURCES", {})
    _write_verdicts(tv_dir, "20260915", [
        {"symbol": "005930", "decision": {"approved": True, "stance": "buy"}},
    ])
    tracker = object.__new__(cf_mod.CounterfactualTracker)
    tracker._state = {}

    def _boom(sym, day):
        raise RuntimeError("네트워크 오류")

    tracker._fill_evidence_check = _boom
    added = tracker._ingest_sources()
    assert added == 1
    assert list(tracker._state.values())[0]["source"] == "team_buy_unfilled"


# ── 2) kr_api.get_team_verdicts ──────────────────────────────────────────

class _FakeTrade:
    def __init__(self, symbol):
        self.symbol = symbol


class _FakeJournal:
    def __init__(self, filled_symbols_by_date):
        self._by_date = filled_symbols_by_date  # {date: [symbol, ...]}

    def get_trades_by_date(self, d):
        return [_FakeTrade(s) for s in self._by_date.get(d, [])]


def _base_verdict_row(symbol="005930", stance="buy", approved=True):
    return {
        "symbol": symbol, "name": "삼성전자",
        "decision": {"approved": approved, "stance": stance, "size_multiplier": 1.0, "reason": "r"},
        "proposal": {"conviction": 0.9, "analyst_scores": {"technical": 10}},
        "debate": {"summary": "s", "rounds_run": 1, "consensus": True},
        "assessment": {
            "stance_v2": "buy_candidate", "merit_status": "sufficient", "risk_acceptable": True,
            "data_sufficiency": "full", "entry_ready": True, "consensus_level": "unanimous",
            "calibration_status": "uncalibrated",
        },
        "elapsed_sec": 1.2, "saved_at": "2026-09-15T10:30:00", "error": None,
        "reports": [], "wiki_context_used": False,
    }


def test_get_team_verdicts_exposes_assessment_and_conviction_label(tmp_path, monkeypatch):
    day = "20260915"
    monkeypatch.setattr(team_mod, "RESULT_DIR", tmp_path / "team_verdicts")
    _write_verdicts(tmp_path / "team_verdicts", day, [_base_verdict_row()])
    monkeypatch.setattr(ledger_mod, "LEDGER_DIR", tmp_path / "team_ledger")

    dc = SimpleNamespace(bot=None)
    handler = KRAPIHandler(dc)
    req = make_mocked_request("GET", f"/api/team/verdicts?date={day}")

    resp = asyncio.run(handler.get_team_verdicts(req))
    body = json.loads(resp.body)

    assert body["count"] == 1
    v = body["verdicts"][0]
    assert v["assessment"]["stance_v2"] == "buy_candidate"
    assert v["assessment"]["risk_acceptable"] is True
    assert "확률 미보정" in v["conviction_label"]
    assert v["execution_state"] == "candidate"   # 원장 기록 없음 → candidate


def test_execution_state_downgrades_without_fill_evidence(tmp_path, monkeypatch):
    """원장이 filled 라고 적어도 실제 체결 증거가 없으면 shadow_ready 로 낮춘다."""
    day = "20260915"
    monkeypatch.setattr(team_mod, "RESULT_DIR", tmp_path / "team_verdicts")
    _write_verdicts(tmp_path / "team_verdicts", day, [_base_verdict_row()])
    monkeypatch.setattr(ledger_mod, "LEDGER_DIR", tmp_path / "team_ledger")
    ledger_mod.append_deliberation({
        "symbol": "005930", "decided_at": "2026-09-15T10:30:00", "slot": "10:30",
        "input_snapshot_hash": "h1", "execution_state": "filled",
    }, ledger_dir=tmp_path / "team_ledger")

    dc = SimpleNamespace(bot=None)  # trade_journal 없음 → 증거 없음
    handler = KRAPIHandler(dc)
    req = make_mocked_request("GET", f"/api/team/verdicts?date={day}")
    body = json.loads(asyncio.run(handler.get_team_verdicts(req)).body)
    assert body["verdicts"][0]["execution_state"] == "shadow_ready"

    # 실제 체결 증거가 있으면 filled 를 그대로 믿는다
    dc2 = SimpleNamespace(bot=SimpleNamespace(
        trade_journal=_FakeJournal({_date(2026, 9, 15): ["005930"]})
    ))
    handler2 = KRAPIHandler(dc2)
    req2 = make_mocked_request("GET", f"/api/team/verdicts?date={day}")
    body2 = json.loads(asyncio.run(handler2.get_team_verdicts(req2)).body)
    assert body2["verdicts"][0]["execution_state"] == "filled"


# ── 3) scripts/team_policy_ab.py ─────────────────────────────────────────

def _mk_prices(base_date: str, base_price: float, n: int = 25, drift: float = 0.012):
    prices = []
    d = _date.fromisoformat(base_date)
    px = base_price
    for i in range(n):
        d2 = d + timedelta(days=i)
        o = px
        h = px * 1.03
        lo = px * 0.97
        c = px * (1 + drift)
        prices.append({"date": d2.isoformat(), "open": round(o, 1), "high": round(h, 1),
                        "low": round(lo, 1), "close": round(c, 1)})
        px = c
    return prices


def _mk_candidate(symbol: str, *, score=80, bear_r1=True, bull_r2=True, bear_r2=True, synthetic=True):
    return {
        "date": "2026-08-01", "symbol": symbol, "strategy": "sepa_trend", "setup": "sepa_pullback",
        "plan": {"score": score, "stop_price": 9500.0, "entry_band_low": 0, "max_entry_price": 11000.0,
                 "trigger": {}, "entry_mode": "close"},
        "evidence": [
            {"kind": "technical", "score": 30, "confidence": 0.8, "positive_basis": True, "error": None},
            {"kind": "fundamental", "score": 10, "confidence": 0.6, "positive_basis": True, "error": None},
        ],
        "votes": {"r1": {"bull": True, "bear": bear_r1}, "r2": {"bull": bull_r2, "bear": bear_r2}},
        "prices": _mk_prices("2026-08-01", 10000.0),
        "synthetic": synthetic,
    }


def _write_snapshot(path: Path, rows: list) -> None:
    path.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")


def test_policy_gates_differ_on_risk_and_debate_outcome():
    ok = _mk_candidate("000001")
    risky = _mk_candidate("000002", bear_r1=False)          # R1 에서 위험 미수용
    split = _mk_candidate("000003", bull_r2=False)          # R2 에서 만장일치 실패
    rows = [tpab.Candidate(date=c["date"], symbol=c["symbol"], plan=c["plan"], evidence=c["evidence"],
                            votes=c["votes"], prices=c["prices"], synthetic=True)
            for c in (ok, risky, split)]

    assert len(tpab.select_candidates(rows, "A", max_new=5)) == 3      # 기존 규칙은 전부 통과
    b_syms = {c.symbol for c in tpab.select_candidates(rows, "B", max_new=5)}
    assert b_syms == {"000001", "000003"}                               # risky 만 R1 에서 탈락
    c_syms = {c.symbol for c in tpab.select_candidates(rows, "C", max_new=5)}
    assert c_syms == {"000001"}                                         # split 은 R2 만장일치 실패


def test_bear_accept_alone_does_not_change_merit_score():
    """계약 2.2 — Bear ACCEPT(risk_acceptable)만으로 매수 매력(merit)이 오르지 않는다."""
    evidence = [{"kind": "technical", "score": 5, "confidence": 0.7, "positive_basis": False, "error": None}]
    merit_reject, status_reject, _ = tpab.r1_assess(evidence)
    votes_bear_true = {"r1": {"bull": True, "bear": True}}
    votes_bear_false = {"r1": {"bull": True, "bear": False}}
    assert tpab.risk_acceptable_r1(votes_bear_true) is True
    assert tpab.risk_acceptable_r1(votes_bear_false) is False
    # bear 표결이 무엇이든 merit(근거 기반 점수)은 evidence 만으로 정해진다 — 동일해야 한다
    assert merit_reject == 0 and status_reject == "insufficient"


def test_selection_experiment_runs_and_marks_synthetic(tmp_path):
    snap = tmp_path / "snap.jsonl"
    _write_snapshot(snap, [_mk_candidate("000001"), _mk_candidate("000002", score=70)])
    rows = tpab.load_snapshot(snap)
    results = tpab.run_selection_experiment(rows, ["A", "B", "C"], max_new=5)
    assert set(results) == {"A", "B", "C"}
    assert results["A"]["completed_positions"] == 2
    assert results["A"]["median_r"] is not None

    out_dir = tmp_path / "out"
    manifest = tpab.build_manifest(["A"], "selection", str(snap), True, None)
    assert manifest["all_rows_synthetic"] is True
    assert "포지션당 비용 차감 R" in manifest["pre_registered"]["primary_metric"]


def test_timing_experiment_defers_to_canonical_entry_plan_checker(tmp_path):
    """EntryPlan 조건부 모드는 check_entry_plan 을 재사용한다 — placeholder 인 동안은
    전부 미체결이 나오는 게 정답이다(허위 체결 생성 금지)."""
    snap = tmp_path / "snap.jsonl"
    _write_snapshot(snap, [_mk_candidate("000001")])
    rows = tpab.load_snapshot(snap)
    results = tpab.run_timing_experiment(rows, fixed_policy="A", max_new=5)
    assert results["existing_open"]["completed_positions"] == 1
    assert results["entry_plan"]["unfilled"] == 1
    assert results["entry_plan"]["completed_positions"] == 0
    assert results["entry_plan"]["fill_rate"] == 0.0


def test_entry_plan_and_existing_open_use_the_same_first_bar_when_checker_allows(tmp_path, monkeypatch):
    """리뷰 blocking#1(2026-09-15) 재발 방지 고정 — EntryPlan 체결이 후보일 당일 봉(판단 재료)으로
    이뤄지면 existing_open(다음 거래일 시가) 보다 먼저·유리하게 체결되는 선행정보 우위가 생긴다.
    checker 를 항상 allow 로 대체해(담당 C 구현 완료 상황을 모사) 두 팔이 반드시 같은 첫
    관찰 봉에서 출발하는지 고정한다."""
    monkeypatch.setattr(tpab, "check_entry_plan",
                         lambda plan, quote, now, **kw: SimpleNamespace(
                             status="allow", expected_fill_price=None, reasons=[]))
    snap = tmp_path / "snap.jsonl"
    _write_snapshot(snap, [_mk_candidate("000001")])
    rows = tpab.load_snapshot(snap)
    c = rows[0]
    start_idx = tpab._first_tradable_idx(c)
    open_idx, _open_px = tpab._next_open_entry(c)
    plan_idx, _plan_px, _reason = tpab._entry_plan_fill(c, start_idx)
    assert start_idx == open_idx, "existing_open 조차 후보일 당일 봉을 쓰면 안 된다(기준 자체 확인)"
    assert plan_idx == open_idx, (
        "EntryPlan 이 existing_open 과 다른(더 이른) 봉에서 체결되면 선행정보 우위가 생긴다"
    )


def test_dedupe_correlated_excludes_same_week_symbol_and_overlapping_holding_period():
    """리뷰 blocking#2(2026-09-15) 재발 방지 고정 — leakage_guard 사전등록(종목-주 클러스터·
    보유기간 겹침 dedup)이 실제로 표본을 줄이는지 확인한다. 잘못된 'week': date[:8] (월 접두사)
    구현이었다면 이 제외가 전혀 일어나지 않았다."""
    positions = [
        {"symbol": "000001", "date": "2026-08-01", "week": tpab._week_key("2026-08-01"),
         "holding_days": 5, "r": 0.2},
        {"symbol": "000001", "date": "2026-08-03", "week": tpab._week_key("2026-08-03"),
         "holding_days": 5, "r": 0.5},  # 08-01 보유기간(~08-06)과 겹침 + 같은 ISO 주
        {"symbol": "000002", "date": "2026-08-01", "week": tpab._week_key("2026-08-01"),
         "holding_days": 5, "r": 0.3},  # 다른 종목 — 영향 없음
    ]
    kept, excluded = tpab._dedupe_correlated(positions)
    assert excluded == 1
    assert {(p["symbol"], p["date"]) for p in kept} == {
        ("000001", "2026-08-01"), ("000002", "2026-08-01"),
    }


def test_cli_end_to_end_writes_synthetic_only_status(tmp_path):
    snap = tmp_path / "snap.jsonl"
    _write_snapshot(snap, [_mk_candidate("000001")])
    out = tmp_path / "out"
    argv = ["team_policy_ab.py", "--snapshot", str(snap), "--policy", "all",
            "--experiment", "selection", "--out", str(out)]
    import sys as _sys
    old_argv = _sys.argv
    _sys.argv = argv
    try:
        tpab.main()
    finally:
        _sys.argv = old_argv
    results = json.loads((out / "results.json").read_text(encoding="utf-8"))
    assert results["validation_status"] == "synthetic_only"
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["all_rows_synthetic"] is True
    assert (out / "report.md").exists()
    assert "미검증" in (out / "report.md").read_text(encoding="utf-8")


def test_non_synthetic_rows_marked_unverified_not_promoted(tmp_path):
    snap = tmp_path / "snap.jsonl"
    _write_snapshot(snap, [_mk_candidate("000001", synthetic=False)])
    rows = tpab.load_snapshot(snap)
    all_synthetic = bool(rows) and all(c.synthetic for c in rows)
    assert all_synthetic is False   # 실데이터(비합성) 로 표시된 스냅샷은 synthetic_only 가 아니다


# ── 4) shadow_report.py / shadow_lab.py 라벨 (승격 기준 숫자는 불변) ─────────

def test_shadow_report_labels_process_quality_and_shows_ledger_samples(tmp_path, monkeypatch):
    import scripts.shadow_report as sr

    verdict_dir = tmp_path / "verdicts"
    monkeypatch.setattr(sr, "VERDICT_DIR", verdict_dir)
    monkeypatch.setattr(sr, "LEDGER_DIR", tmp_path / "llm_ledger")
    monkeypatch.setattr(ledger_mod, "LEDGER_DIR", tmp_path / "team_ledger")
    _write_verdicts(verdict_dir, f"{datetime.now():%Y%m%d}", [
        {"symbol": "005930", "decision": {"approved": True, "stance": "buy", "reason": "r"}},
    ])
    ledger_mod.append_deliberation({
        "symbol": "005930", "decided_at": f"{datetime.now():%Y-%m-%d}T10:30:00", "slot": "10:30",
        "input_snapshot_hash": "h1",
    }, ledger_dir=tmp_path / "team_ledger")

    report = sr.build_report(days=1)

    assert sr.MIN_SAMPLES == 200  # 승격 기준 숫자 자체(변수)가 바뀌지 않았는지 직접 확인
    assert "MIN_SAMPLES" not in report  # 리포트엔 변수명이 아니라 값만 보인다(이름 노출 안 함)
    assert "200건" in report  # 기존 승격 기준 숫자(MIN_SAMPLES) 불변
    assert "LLM 프로세스 품질 지표" in report and "P&L 미측정" in report
    assert "엣지 증명이 아님" in report
    assert "판단(중복 제거) 1건" in report  # team_ledger 표본 라인


def test_shadow_lab_promotion_report_adds_disclaimer_without_changing_thresholds(tmp_path, monkeypatch):
    import src.analytics.shadow_lab as sl

    monkeypatch.setattr(sl, "_CACHE", tmp_path)
    report = asyncio.run(sl.promotion_readiness_report())
    assert "0/20건" in report and "55%" in report  # 규칙#11 기준 숫자 불변
    assert "엣지 증명이 아님" in report
