"""통합 E2E(Task6): 실제 입력 훅 → 작업자 → 공유 월 예산 → 판단 기록 → 쌍 결과 → 호가/장 상태 조인
→ 진입 조건 재생 → 조건부 경제성 게이트 → 일별 보고. 가짜 공급자·임시 원장만 쓰며 네트워크 0.

W1(예산·복구)·W2(봉투·작업자)·W3(진입 조건·보고)의 단위 시험이 각 경계를 덮고, 이 파일은 그 경계들이
한 흐름으로 이어지는지와 예산 상한·재시작·비용 불명→대사 경로를 같은 자료로 확인한다.
"""
import asyncio
import hashlib
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from src.analytics import model_comparison_admission as adm
from src.analytics import model_comparison_daily_report as daily
from src.analytics.llm_decision_record import DecisionRecorder, set_decision_recorder
from src.analytics.model_comparison_execution import ComparisonCallGuard, ComparisonExecutionPolicy
from src.analytics.model_comparison_outcome import compare_policy_outcomes
from src.analytics.model_comparison_recovery import inspect_pair
from src.analytics.model_comparison_runner import PairedComparisonRunner, SingleAttemptAdapter, report_pairs
from src.utils.llm_budget import MonthlyBudgetLedger
from src.utils.llm_budget_reconciliation import reconcile_budget
from tests.test_decision_quote_join import OK_LEDGER, _quote, _state
from tests.test_model_comparison_input import _LLM, _cross
from tests.test_model_comparison_outcome import fees, live_v2
from tests.test_model_comparison_worker import FakeGemini, FakeOpenAI, manifest, wait, api as worker_api

KST = timezone(timedelta(hours=9))


def _stack(tmp_path, monkeypatch, *, ceiling="100", openai=None):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    recorder = DecisionRecorder(tmp_path / "decisions", enabled=True)
    set_decision_recorder(recorder)
    ledger = MonthlyBudgetLedger(tmp_path / "budget.db", 100, policy_enabled=True)
    man = manifest(fixed_at="2026-01-01T00:00:00+00:00")
    man["execution_policy"]["experiment_usd_ceiling"] = ceiling
    policy = dict(man["execution_policy"], experiment_id=man["experiment_id"])
    policy["allowed_models"] = tuple(policy["allowed_models"])
    guard = ComparisonCallGuard(ledger=ledger, policy=ComparisonExecutionPolicy(**policy))
    op, ge = openai or FakeOpenAI(), FakeGemini()
    runner = PairedComparisonRunner(guard=guard, adapter=SingleAttemptAdapter(openai=op, gemini=ge))
    worker = worker_api().ComparisonWorker(runner, manifest=man, max_input_age_seconds=30.0).start()
    cross = _cross(monkeypatch, _LLM())
    worker_api().attach(worker, cross)
    offered = []
    inner = cross._comparison_handoff

    def capturing(envelope):              # 훅이 넘긴 봉투를 그대로 보존한다(일별 보고의 모집단)
        offered.append(dict(envelope))
        return inner(envelope)
    cross._comparison_handoff = capturing
    return SimpleNamespace(recorder=recorder, ledger=ledger, guard=guard, runner=runner, worker=worker,
                           cross=cross, op=op, ge=ge, manifest=man, offered=offered, path=tmp_path / "budget.db")


def _decide(s, signal_id="sig-e2e"):
    approved = asyncio.run(s.cross.llm_second_check("005930", "momentum", 90, {}, "sideways", signal_id=signal_id))
    return approved, dict(s.cross.last_llm_decision)


def _pair_rows(ledger):
    with ledger._connect(readonly=True) as db:
        return [json.loads(r[0]) for r in db.execute("SELECT result FROM comparison_pairs WHERE result IS NOT NULL")]


def _decision_rows(recorder):
    assert recorder.flush(5.0)
    return [json.loads(l) for f in sorted(recorder.directory.glob("*.jsonl")) for l in f.read_text().splitlines()]


def _utc(text):
    return datetime.fromisoformat(text).astimezone(timezone.utc)


def _naive(stamp):
    return stamp.astimezone(timezone.utc).replace(tzinfo=None).isoformat()


def _teardown(s):
    s.worker.stop(5)
    s.recorder.close()
    set_decision_recorder(None)


def test_hook_to_daily_report_happy_path_then_restart_without_spend(tmp_path, monkeypatch):
    s = _stack(tmp_path, monkeypatch)
    try:
        approved, meta = _decide(s)
        assert approved is True and meta["comparison_handoff"] == "accepted"
        assert wait(lambda: s.worker.health()["finished"] == 1)
        decision_id = meta["decision_id"]

        # 1) 같은 decision_id 가 판단 기록·봉투·쌍 행에 전파되고 입력 시각이 보존된다.
        rows = _decision_rows(s.recorder)
        decision = next(r for r in rows if r["decision_id"] == decision_id)
        (pair,) = _pair_rows(s.ledger)
        assert pair["decision_id"] == decision_id == s.offered[0]["decision_id"]
        assert pair["input_available_at"] == decision["inputs"]["collected_at"] == s.offered[0]["input_available_at"]
        assert pair["schema_version"] == "model-comparison-pair-v2"
        assert pair["evaluation_anchor"] == "input_available_at" and pair["live_provenance"] == "in_process_attested"
        assert _utc(pair["evaluation_at"]) == _utc(pair["input_available_at"]) + timedelta(seconds=900)
        assert pair["current"]["queue_delay_ms"] is not None and pair["current"]["provider_latency_ms"] is not None

        # 2) 공유 월 예산: Bull 2 + 공통 Bear 1 = 3 정산, normal 단계 유지.
        status = s.ledger.status()
        assert status["settled_count"] == 3 and status["policy_mode"] == "normal" and not status["blocked"]
        assert pair["actual_provider_attempts"] == 3 and pair["total_comparison_cost_usd"] is not None
        assert len(s.op.calls) == 2 and len(s.ge.calls) == 1

        # 3) 엄격 원장 상태 + 합성 호가: 각 arm 완료 뒤 첫 호가와 공통 종료 호가(anchor+900s).
        cur_at, cand_at = (_utc(pair[a]["decision_available_at"]) for a in ("current", "candidate"))
        exit_at = _utc(pair["evaluation_at"])
        payloads = [_quote("005930", _naive(min(cur_at, cand_at) + timedelta(milliseconds=50)), ask="71000", qid="q-first"),
                    _quote("005930", _naive(max(cur_at, cand_at) + timedelta(milliseconds=50)), ask="71100", qid="q-second"),
                    _state("005930", _naive(exit_at - timedelta(seconds=1)), "H0STMKO0"),
                    _quote("005930", _naive(exit_at), ask="71500", qid="q-exit")]
        reported = report_pairs([pair], payloads=payloads, ledger_status=OK_LEDGER)["rows"][0]
        for arm in ("current", "candidate"):
            assert reported[arm]["quote_evidence"]["first_quote_after"] is not None
        assert reported["exit_quote_evidence"]["first_quote_after"]["quote_id"] == "q-exit"
        assert reported["economic_evaluable"] is False and reported["net_delta_krw"] is None
        assert "continuous_session_not_certified" in reported["reasons"]

        # 4) 진입 조건: 수량 스냅샷이 없는 판단(엔진 밖 호출)은 모든 게이트 unknown → admissible None.
        admission = adm.replay_admission({"decision_id": decision_id, "neutral_sizing": None,
                                          "input_available_at": pair["input_available_at"]},
                                         pair["candidate"]["verdict"], pair["candidate"]["sizing_replay"] or {})
        assert admission["admissible"] is None
        assert all(g["status"] == "unknown" for g in admission["gates"].values())
        # 조건부 경제성 게이트: admissible None 이면 net_delta 는 null 로 남는다.
        row = live_v2()
        row["admission"]["candidate"]["admissible"] = None
        outcome = compare_policy_outcomes(row, fees=fees())
        assert outcome["net_delta_krw"] is None and outcome["status"] == "not_evaluable"

        # 5) 일별 보고: 모집단/선정/완료 분모와 미리보기 문자열만(발송 함수 없음).
        day = _utc(pair["input_available_at"]).astimezone(KST).date().isoformat()
        env = dict(s.offered[0], received_at=s.offered[0]["handed_off_at"])
        report = daily.build_daily_report([pair], day_kst=day, offered_envelopes=[env],
                                          max_input_age_seconds=30.0, pairs_per_day=1)
        text = json.dumps(report, ensure_ascii=False)
        assert '"offered": 1' in text and '"complete": 1' in text
        assert isinstance(report["telegram_preview"], str) and report["telegram_preview"]
        assert not any(name.startswith("send") for name in dir(daily))

        # 6) 재시작: 새 작업자가 같은 봉투를 다시 받아도 공급자 호출·정산이 늘지 않는다.
        worker2 = worker_api().ComparisonWorker(s.runner, manifest=s.manifest, max_input_age_seconds=30.0).start()   # 같은 명세(다른 한도는 manifest 불일치로 재실행 거부)
        try:
            assert worker2.offer(s.offered[0]) == "accepted"
            assert wait(lambda: worker2.health()["finished"] == 1)
        finally:
            worker2.stop(5)
        assert len(s.op.calls) == 2 and len(s.ge.calls) == 1
        assert s.ledger.status()["settled_count"] == 3 and len(_pair_rows(s.ledger)) == 1
    finally:
        _teardown(s)


def test_experiment_ceiling_blocks_inside_reservation_before_any_provider_call(tmp_path, monkeypatch):
    s = _stack(tmp_path, monkeypatch, ceiling="0.000000001")
    try:
        approved, meta = _decide(s, "sig-ceiling")
        assert approved is True and meta["comparison_handoff"] == "accepted"      # 운영 판단은 그대로
        assert wait(lambda: s.worker.health()["finished"] == 1)
        (pair,) = _pair_rows(s.ledger)
        assert s.op.calls == [] and s.ge.calls == []                             # 전송 전 차단
        assert s.ledger.status()["settled_count"] == 0 and not s.ledger.status()["blocked"]
        for arm in ("current", "candidate"):
            assert pair[arm]["verdict"] == "unavailable"
            assert {a["error"] for a in pair[arm]["attempts"]} == {"comparison_experiment_ceiling"}
            assert all(a["api_cost_usd"] == "0" for a in pair[arm]["attempts"])    # 미전송 증명만 0원
        assert pair["actual_provider_attempts"] == 0
        assert inspect_pair(s.guard, pair["pair_id"])["complete"] is True
    finally:
        _teardown(s)


def test_unknown_cost_blocks_ledger_until_audited_reconciliation_then_report_shows_both_costs(tmp_path, monkeypatch):
    s = _stack(tmp_path, monkeypatch, openai=FakeOpenAI(unknown=True))
    try:
        approved, meta = _decide(s, "sig-unknown")
        assert approved is True and wait(lambda: s.worker.health()["finished"] == 1)
        (pair,) = _pair_rows(s.ledger)
        unknown = [a for arm in ("current", "candidate") for a in pair[arm]["attempts"] if a["ledger_state"] == "unknown"]
        assert len(unknown) == 2 and all(a["api_cost_usd"] is None for a in unknown)
        assert s.ledger.status()["blocked"] and s.ledger.status()["block_reason"] == "unresolved_usage"
        with sqlite3.connect(s.path) as db:
            activated_before = db.execute("SELECT activated_at FROM settings").fetchone()[0]

        evidence = tmp_path / "usage-export.txt"
        evidence.write_text("synthetic provider usage export")
        review = {"version": "llm-budget-reconciliation-v1", "activated_at": s.ledger.status()["activated_at"],
                  "reviewer_ref": "synthetic-operator",
                  "evidence_sha256": hashlib.sha256(evidence.read_bytes()).hexdigest(),
                  "adjustments": [{"token": a["reservation_token"], "actual_cost_usd": 0.0002} for a in unknown],
                  "clear_bound_breach": False}
        preview = reconcile_budget(s.path, 100, review, evidence)
        assert s.guard.lookup(unknown[0]["attempt_id"])["state"] == "unknown"       # 미리보기는 읽기 전용
        applied = reconcile_budget(s.path, 100, review, evidence, apply=True,
                                   expected_state_sha256=preview["state_sha256"])
        assert applied["applied"] is True and applied["adjustment_count"] == 2
        for a in unknown:
            record = s.guard.lookup(a["attempt_id"])
            assert record["state"] == "known_reconciled" and record["reconciled"]["reviewer_ref"] == "synthetic-operator"
            assert record["original_estimated_cost_usd"] is None                    # 원래 응답은 불변
            assert record["result"].value["success"] is True
        assert not s.ledger.status()["blocked"]
        with sqlite3.connect(s.path) as db:
            assert db.execute("SELECT activated_at FROM settings").fetchone()[0] == activated_before

        assert inspect_pair(s.guard, pair["pair_id"])["complete"] is True
        day = _utc(pair["input_available_at"]).astimezone(KST).date().isoformat()
        env = dict(s.offered[0], received_at=s.offered[0]["handed_off_at"])
        report = daily.build_daily_report([pair], day_kst=day, offered_envelopes=[env], max_input_age_seconds=30.0,
                                          pairs_per_day=1, reconciled_lookup=s.guard.lookup)
        text = json.dumps(report, ensure_ascii=False)
        assert "reconcil" in text and '"offered": 1' in text
        # 같은 ID 재전달은 재전송·재과금하지 않는다.
        worker2 = worker_api().ComparisonWorker(s.runner, manifest=s.manifest, max_input_age_seconds=30.0).start()   # 같은 명세(다른 한도는 manifest 불일치로 재실행 거부)
        try:
            assert worker2.offer(s.offered[0]) == "accepted" and wait(lambda: worker2.health()["finished"] == 1)
        finally:
            worker2.stop(5)
        assert len(s.op.calls) == 2 and s.ledger.status()["settled_count"] == 3
    finally:
        _teardown(s)
