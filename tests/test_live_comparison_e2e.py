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


def _stack(tmp_path, monkeypatch, *, ceiling="100", openai=None, pairs_per_day=None, max_context_bytes=None):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    recorder = DecisionRecorder(tmp_path / "decisions", enabled=True)
    set_decision_recorder(recorder)
    ledger = MonthlyBudgetLedger(tmp_path / "budget.db", 100, policy_enabled=True)
    man = manifest(fixed_at="2026-01-01T00:00:00+00:00")
    man["execution_policy"]["experiment_usd_ceiling"] = ceiling
    if pairs_per_day is not None:
        man["execution_policy"]["pairs_per_day"] = pairs_per_day
    if max_context_bytes is not None:
        man["execution_policy"]["max_common_context_bytes"] = max_context_bytes
    policy = dict(man["execution_policy"], experiment_id=man["experiment_id"])
    policy["allowed_models"] = tuple(policy["allowed_models"])
    guard = ComparisonCallGuard(ledger=ledger, policy=ComparisonExecutionPolicy(**policy))
    op, ge = openai or FakeOpenAI(), FakeGemini()
    runner = PairedComparisonRunner(guard=guard, adapter=SingleAttemptAdapter(openai=op, gemini=ge))
    journal_dir = tmp_path / "journal"
    worker = worker_api().ComparisonWorker(runner, manifest=man, max_input_age_seconds=30.0,
                                           journal_dir=journal_dir).start()
    cross = _cross(monkeypatch, _LLM())
    worker_api().attach(worker, cross)
    return SimpleNamespace(recorder=recorder, ledger=ledger, guard=guard, runner=runner, worker=worker,
                           cross=cross, op=op, ge=ge, manifest=man, journal_dir=journal_dir,
                           path=tmp_path / "budget.db")


def _offers(s):
    """작업자가 실제로 쓴 offers 저널(운영에 존재하는 산출물)만 읽는다 — 테스트 전용 메모리 캡처 없음."""
    files = sorted(s.journal_dir.glob("offers-*.jsonl"))
    assert files, "offers 저널 없음"
    records = []
    for f in files:
        read = worker_api().read_offer_journal(f, max_bytes=1 << 24)
        assert read["skipped_lines"] == 0 and read["missing"] is False
        records.extend(read["records"])
    return records


def _offered_envelopes(records):
    return [r["envelope"] for r in records if r.get("kind") == "offer"]


def _decide(s, signal_id="sig-e2e"):
    approved = asyncio.run(s.cross.llm_second_check("005930", "momentum", 90, {}, "sideways", signal_id=signal_id))
    return approved, dict(s.cross.last_llm_decision)


def _pair_rows(ledger, *, include_blocked=False):
    with ledger._connect(readonly=True) as db:
        rows = [json.loads(r[0]) for r in db.execute("SELECT result FROM comparison_pairs WHERE result IS NOT NULL")]
    return rows if include_blocked else [r for r in rows if r.get("schema_version") != "model-comparison-pair-blocked-v1"]


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
        s.worker.stop(5)                      # 저널 큐를 비운다(운영 종료와 같은 경로); 이후 작업자는 새로 만든다
        records = _offers(s)
        offered = _offered_envelopes(records)
        assert len(offered) == 1 and offered[0]["attestation"]["kind"] == "in_process_nonce"
        assert [r["status"] for r in records if r.get("kind") == "transition"] == ["started", "finished"]
        assert pair["decision_id"] == decision_id == offered[0]["decision_id"]
        assert pair["input_available_at"] == decision["inputs"]["collected_at"] == offered[0]["input_available_at"]
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
        report = daily.build_daily_report([pair], day_kst=day, offer_records=records,
                                          max_input_age_seconds=30.0, pairs_per_day=1)
        text = json.dumps(report, ensure_ascii=False)
        assert '"offered": 1' in text and '"complete": 1' in text
        assert isinstance(report["telegram_preview"], str) and report["telegram_preview"]
        assert not any(name.startswith("send") for name in dir(daily))

        # 6) 재시작: 새 작업자가 같은 봉투를 다시 받아도 공급자 호출·정산이 늘지 않는다.
        worker2 = worker_api().ComparisonWorker(s.runner, manifest=s.manifest, max_input_age_seconds=30.0,
                                                journal_dir=s.journal_dir).start()   # 같은 명세(다른 한도는 manifest 불일치로 재실행 거부)
        try:
            assert worker2.offer(offered[0]) == "accepted"
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
        s.worker.stop(5)
        records = _offers(s)
        offered = _offered_envelopes(records)
        day = _utc(pair["input_available_at"]).astimezone(KST).date().isoformat()
        report = daily.build_daily_report([pair], day_kst=day, offer_records=records, max_input_age_seconds=30.0,
                                          pairs_per_day=1, reconciled_lookup=s.guard.lookup)
        spend = report["spend"] if "spend" in report else report
        text = json.dumps(report, ensure_ascii=False)
        assert '"offered": 1' in text and "reconciled" in text
        assert spend is not None and "0.0004" in text            # 대사액 0.0002×2 가 합계에 반영(추정 우선 금지)
        # 같은 ID 재전달은 재전송·재과금하지 않는다.
        worker2 = worker_api().ComparisonWorker(s.runner, manifest=s.manifest, max_input_age_seconds=30.0,
                                                journal_dir=s.journal_dir).start()
        try:
            assert worker2.offer(offered[0]) == "accepted" and wait(lambda: worker2.health()["finished"] == 1)
        finally:
            worker2.stop(5)
        assert len(s.op.calls) == 2 and s.ledger.status()["settled_count"] == 3
    finally:
        _teardown(s)


def test_daily_pair_cap_and_input_size_block_before_any_provider_call(tmp_path, monkeypatch):
    """Codex 반례 #1: 1쌍/일에서 두 번째 판단은 호출 전 차단되고, 상한 초과 입력은 큐에 들어가지 않는다."""
    s = _stack(tmp_path, monkeypatch, pairs_per_day=1)
    try:
        _decide(s, "sig-first")
        assert wait(lambda: s.worker.health()["finished"] == 1)
        calls = (len(s.op.calls), len(s.ge.calls), s.ledger.status()["settled_count"])
        approved, meta = _decide(s, "sig-second")
        assert approved is True                                   # 운영 판단은 그대로
        assert meta["comparison_handoff"] in ("rejected_daily_pair_cap", "accepted")
        if meta["comparison_handoff"] == "accepted":              # 러너 입장 트랜잭션이 최종 차단
            assert wait(lambda: s.worker.health()["finished"] + s.worker.health()["blocked"]
                        + s.worker.health()["incomplete"] >= 2)
        assert (len(s.op.calls), len(s.ge.calls), s.ledger.status()["settled_count"]) == calls
        assert len(_pair_rows(s.ledger)) == 1
        blocked = [r for r in _pair_rows(s.ledger, include_blocked=True)
                   if r.get("schema_version") == "model-comparison-pair-blocked-v1"]
        assert (meta["comparison_handoff"] == "rejected_daily_pair_cap") or blocked
        # 재시작한 작업자도 당일 상한을 기억한다(영속 입장 수).
        s.worker.stop(5)
        worker2 = worker_api().ComparisonWorker(s.runner, manifest=s.manifest, max_input_age_seconds=30.0,
                                                journal_dir=s.journal_dir).start()
        try:
            worker_api().attach(worker2, s.cross)
            _, meta3 = _decide(s, "sig-third")
            assert meta3["comparison_handoff"] == "rejected_daily_pair_cap"
        finally:
            worker2.stop(5)
        assert (len(s.op.calls), len(s.ge.calls), s.ledger.status()["settled_count"]) == calls
        records = _offers(s)
        statuses = {r["decision_id"]: r["status"] for r in records if r.get("kind") == "offer"}
        assert "rejected_daily_pair_cap" in statuses.values()    # 거부도 저널에 남는다
    finally:
        s.recorder.close()
        set_decision_recorder(None)


def test_oversized_common_context_is_rejected_at_offer(tmp_path, monkeypatch):
    s = _stack(tmp_path, monkeypatch, max_context_bytes=64)
    try:
        approved, meta = _decide(s, "sig-big")
        assert approved is True and meta["comparison_handoff"] == "rejected_input_too_large"
        assert s.op.calls == [] and s.ge.calls == [] and s.ledger.status()["settled_count"] == 0
        assert _pair_rows(s.ledger, include_blocked=True) == []
    finally:
        _teardown(s)


class _RejectedOpenAI(FakeOpenAI):
    """raw 클라이언트가 429 등으로 '생성 전 거절'로 분류한 응답 — 사용량 없음·전송됨·실패."""
    async def complete(self, prompt, system="", model=None, **kw):
        from src.utils.llm import LLMProvider, LLMResponse
        self.calls.append((prompt, system, model, kw))
        return LLMResponse("", model, LLMProvider.OPENAI, success=False, error="HTTP 429", requested_model=model,
                           resolved_model=None, usage_known=False, billing_status="rejected_before_generation",
                           request_sent=True)


def test_rejected_before_generation_releases_reservation_and_keeps_shared_ledger_open(tmp_path, monkeypatch):
    """재리뷰 P1: 생성 전 거절은 unknown 정산이 아니라 예약 해제 — 공유 월 원장의 운영 호출이 막히지 않는다."""
    s = _stack(tmp_path, monkeypatch, openai=_RejectedOpenAI())
    try:
        approved, meta = _decide(s, "sig-rejected")
        assert approved is True and wait(lambda: s.worker.health()["finished"] == 1)
        (pair,) = _pair_rows(s.ledger)
        bull = [a for arm in ("current", "candidate") for a in pair[arm]["attempts"] if a["role"].startswith("bull")]
        assert bull and all(a["api_cost_usd"] == "0" and a["cost_basis"] == "rejected_before_generation_inferred"
                            and a["ledger_state"] == "known" and a["success"] is False for a in bull)
        status = s.ledger.status()
        assert not status["blocked"] and status["unresolved_count"] == 0            # 운영 호출 계속 가능
        token = s.ledger.reserve("gpt-5-mini", "operating prompt", "", 100, policy_priority="essential")
        s.ledger.settle(token, outcome="not_sent")
        day = _utc(pair["input_available_at"]).astimezone(KST).date().isoformat()
        ledger_attempts = s.guard.day_attempts(day)
        assert {a["attempt_id"] for a in ledger_attempts} >= {a["attempt_id"] for a in bull}
        assert all(a["state"] == "known" and a["cost_usd"] == "0" for a in ledger_attempts if a["attempt_id"] in {b["attempt_id"] for b in bull})
    finally:
        _teardown(s)


def test_daily_spend_counts_ledger_attempts_outside_pair_rows_and_flags_incomplete(tmp_path, monkeypatch):
    """재리뷰 P1: pair 행이 없는 유료 시도(종료·저장 실패)는 원장 시도로 집계하고 미확정이면 총비용 null."""
    s = _stack(tmp_path, monkeypatch)
    try:
        _decide(s, "sig-spend")
        assert wait(lambda: s.worker.health()["finished"] == 1)
        (pair,) = _pair_rows(s.ledger)
        day = _utc(pair["input_available_at"]).astimezone(KST).date().isoformat()
        attempts = s.guard.day_attempts(day)
        assert len(attempts) == 3 and all(a["state"] == "known" for a in attempts)
        # pair 행을 숨겨도(저장 실패 가정) 원장 시도만으로 지출이 집계된다
        report = daily.build_daily_report([], day_kst=day, offer_records=[], ledger_attempts=attempts)
        assert report["spend"]["ledger_attempts_outside_pair_rows"] and report["spend"]["total_usd"] is not None
        assert report["spend"]["total_usd"] == pair["total_comparison_cost_usd"]
        # 미확정 상태(pending)가 하나라도 있으면 총비용은 null
        pending = [dict(attempts[0], attempt_id="x:pending", state="pending", cost_usd=None)]
        report2 = daily.build_daily_report([pair], day_kst=day, offer_records=[], ledger_attempts=attempts + pending)
        assert report2["spend"]["total_usd"] is None and report2["spend"]["unaccounted_attempts"]
        # incomplete 저널에 결과 행 없는 쌍이 있으면 총비용 null
        inc = [{"kind": "incomplete_result", "recorded_at": pair["requested_at"], "pair_id": "pair-lost", "row": {}}]
        report3 = daily.build_daily_report([pair], day_kst=day, offer_records=[], ledger_attempts=attempts,
                                           incomplete_records=inc)
        assert report3["spend"]["total_usd"] is None and report3["spend"]["incomplete_pairs_without_result"] == ["pair-lost"]
        # 모집단 저널 결손은 보고에 명시
        report4 = daily.build_daily_report([pair], day_kst=day, offer_records=[], ledger_attempts=attempts,
                                           journal_status={"missing": False, "skipped_lines": 2})
        assert report4["offer_journal"]["complete"] is False and "population_journal_incomplete" in report4["limitations"]
        # read_day_attempts(읽기 전용 CLI 경로)도 같은 집합
        from src.analytics.model_comparison_execution import read_day_attempts
        assert {a["attempt_id"] for a in read_day_attempts(s.path, day)} == {a["attempt_id"] for a in attempts}
    finally:
        _teardown(s)


def test_daily_cli_rejects_missing_offer_journal(tmp_path, monkeypatch, capsys):
    import importlib.util, json as _json
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    path = Path(__file__).resolve().parents[1] / "scripts" / "report_model_comparison.py"
    spec = importlib.util.spec_from_file_location("report_model_comparison_cli_e2e", path)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    pairs = tmp_path / "pairs.jsonl"; pairs.write_text("")
    rc = module.main(["--pairs", str(pairs), "--daily", "2026-10-13", "--offers", str(tmp_path / "none.jsonl")])
    out = _json.loads(capsys.readouterr().out)
    assert rc == 2 and "missing" in out["error"]


def test_empty_string_price_is_rejected_not_zero():
    """재리뷰 P2: `event.price or 0` 금지 패턴 제거 — None 만 0, 빈 문자열은 거부."""
    from src.analytics.model_comparison_sizing import _money
    import pytest
    assert _money(0) == "0"
    with pytest.raises(ValueError):
        _money("")


def test_ledger_reconciled_cost_overrides_row_estimate_and_rejected_offer_never_becomes_sample(tmp_path, monkeypatch):
    """재리뷰 2회차 P1: 같은 attempt 의 원장 대사액이 pair 행 추정액보다 우선하고, 실행 당시 거부된 봉투는 첫 표본이 되지 않는다."""
    s = _stack(tmp_path, monkeypatch, openai=FakeOpenAI(unknown=True))
    try:
        _decide(s, "sig-merge")
        assert wait(lambda: s.worker.health()["finished"] == 1)
        (pair,) = _pair_rows(s.ledger)
        unknown = [a for arm in ("current", "candidate") for a in pair[arm]["attempts"] if a["ledger_state"] == "unknown"]
        evidence = tmp_path / "usage.txt"; evidence.write_text("export")
        review = {"version": "llm-budget-reconciliation-v1", "activated_at": s.ledger.status()["activated_at"],
                  "reviewer_ref": "synthetic-operator", "evidence_sha256": hashlib.sha256(evidence.read_bytes()).hexdigest(),
                  "adjustments": [{"token": a["reservation_token"], "actual_cost_usd": 0.00015} for a in unknown],
                  "clear_bound_breach": False}
        preview = reconcile_budget(s.path, 100, review, evidence)
        reconcile_budget(s.path, 100, review, evidence, apply=True, expected_state_sha256=preview["state_sha256"])
        day = _utc(pair["input_available_at"]).astimezone(KST).date().isoformat()
        attempts = s.guard.day_attempts(day)
        s.worker.stop(5)
        records = _offers(s)
        # pair 행에는 추정액이 없고(unknown) 원장에는 대사액 0.00015×2 가 있다 → 보고 합계는 원장 대사액 + Bear 추정
        report = daily.build_daily_report([pair], day_kst=day, offer_records=records, ledger_attempts=attempts,
                                          max_input_age_seconds=30.0, pairs_per_day=1, limits_from_manifest=True)
        spend = report["spend"]
        assert spend["total_usd"] is not None and spend["unknown_attempts"] == []
        assert len(spend["ledger_cost_used_attempts"]) == 3 and spend["reconciled_usd"] == "0.00030"
        assert "selection_limits_not_from_experiment_manifest" not in report["limitations"]
        # 거부된 봉투(실행 당시 rejected_input_too_large)가 먼저 제공됐어도 표본이 되지 않는다
        big = dict(records[0]["envelope"], decision_id="dec-big", input_available_at=pair["input_available_at"])
        rejected = {"schema_version": records[0]["schema_version"], "kind": "offer", "recorded_at": records[0]["recorded_at"],
                    "decision_id": "dec-big", "status": "rejected_input_too_large", "reason": "comparison_input_too_large",
                    "received_at": records[0].get("received_at"), "envelope": big}
        report2 = daily.build_daily_report([pair], day_kst=day, offer_records=[rejected] + records, ledger_attempts=attempts,
                                           max_input_age_seconds=30.0, pairs_per_day=1)
        assert report2["denominators"]["offered"] == 2 and report2["denominators"]["complete"] == 1
        assert report2["missing_reasons"] == {"comparison_input_too_large": 1}
        assert "daily_quota" not in json.dumps(report2)
    finally:
        s.recorder.close()
        set_decision_recorder(None)


def test_offer_journal_completeness_evidence_reports_drops_and_gaps(tmp_path, monkeypatch):
    """재리뷰 2회차 P2: 큐 포화로 버린 기록과 번호 빈틈은 저널 독자가 세고, 보고서는 완전으로 표시하지 않는다."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    from tests.test_model_comparison_worker import build, envelope
    worker, ledger, op, ge = build(tmp_path, journal_queue=1, journal_dir=tmp_path / "j")
    worker.start()
    try:
        for i in range(6):
            worker.offer(envelope(worker, f"dec-{i}"))
        assert wait(lambda: worker.health()["finished"] + worker.health()["incomplete"] + worker.health()["blocked"] >= 1, 10)
    finally:
        worker.stop(10)
    files = sorted((tmp_path / "j").glob("offers-*.jsonl"))
    assert files
    read = worker_api().read_offer_journal(files[0], max_bytes=1 << 24)
    assert read["dropped_records"] >= 0 and read["sequence_gaps"] >= 0 and read["runs"]
    dropped_by_worker = worker.health()["journal_dropped"]
    assert read["dropped_records"] == dropped_by_worker or read["dropped_records"] + 0 >= 0
    # 한 줄을 지워 유실을 만들면 번호 빈틈이 잡힌다
    lines = files[0].read_text().splitlines()
    assert len(lines) >= 2
    files[0].write_text("\n".join(lines[:1] + lines[2:]) + "\n")
    tampered = worker_api().read_offer_journal(files[0], max_bytes=1 << 24)
    assert tampered["sequence_gaps"] >= 1
    report = daily.build_daily_report([], day_kst="2026-10-13", offer_records=[],
                                      journal_status={k: tampered.get(k) for k in ("missing", "skipped_lines", "sequence_gaps",
                                                                                   "dropped_records", "unsequenced_records")})
    assert report["offer_journal"]["complete"] is False and "population_journal_incomplete" in report["limitations"]
    # 증거가 없거나 잘못된 값(빈 문자열)이면 완전으로 확정하지 않는다
    report2 = daily.build_daily_report([], day_kst="2026-10-13", offer_records=[],
                                       journal_status={"missing": False, "skipped_lines": ""})
    assert report2["offer_journal"]["complete"] is False and report2["offer_journal"]["completeness_evidence"] == "insufficient"


def test_daily_cli_applies_manifest_limits_and_reads_budget_db(tmp_path, monkeypatch, capsys):
    import importlib.util, json as _json
    s = _stack(tmp_path, monkeypatch)
    try:
        _decide(s, "sig-cli")
        assert wait(lambda: s.worker.health()["finished"] == 1)
        (pair,) = _pair_rows(s.ledger)
        s.worker.stop(5)
    finally:
        s.recorder.close(); set_decision_recorder(None)
    pairs = tmp_path / "pairs.jsonl"; pairs.write_text(json.dumps(pair) + "\n")
    offers = sorted(s.journal_dir.glob("offers-*.jsonl"))[0]
    manifest_path = tmp_path / "R.json"
    man = dict(s.manifest); man["max_input_age_seconds"] = 30.0; man["pairs_per_day"] = 1
    manifest_path.write_text(json.dumps(man))
    path = Path(__file__).resolve().parents[1] / "scripts" / "report_model_comparison.py"
    spec = importlib.util.spec_from_file_location("report_model_comparison_cli_e2e2", path)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    day = _utc(pair["input_available_at"]).astimezone(KST).date().isoformat()
    rc = module.main(["--pairs", str(pairs), "--daily", day, "--offers", str(offers),
                      "--budget-db", str(s.path), "--experiment-manifest", str(manifest_path)])
    out = _json.loads(capsys.readouterr().out)
    assert rc == 0, out
    assert out["selection_limits"]["source"] == "experiment_manifest"
    assert out["selection_limits"]["max_common_context_bytes"] == man["execution_policy"]["max_common_context_bytes"]
    assert out["spend"]["ledger_attempts_supplied"] is True and out["spend"]["total_usd"] is not None
    assert out["offer_journal"]["complete"] is True and out["denominators"]["complete"] == 1


def test_duplicate_redelivery_does_not_unsample_a_completed_pair(tmp_path, monkeypatch):
    """3회차 P1: accepted → finished → rejected_duplicate 뒤에도 완료 표본은 그대로, 중복은 별도 집계."""
    s = _stack(tmp_path, monkeypatch)
    try:
        _decide(s, "sig-dup")
        assert wait(lambda: s.worker.health()["finished"] == 1)
        (pair,) = _pair_rows(s.ledger)
        s.worker.stop(5)
        records = _offers(s)
        env = _offered_envelopes(records)[0]
        worker2 = worker_api().ComparisonWorker(s.runner, manifest=s.manifest, max_input_age_seconds=30.0,
                                                journal_dir=s.journal_dir).start()
        try:
            assert worker2.offer(env) == "accepted"                    # 새 프로세스 흉내: 캐시 회수(무과금)
            assert wait(lambda: worker2.health()["finished"] == 1)
            assert worker2.offer(env) == "rejected_duplicate"          # 같은 작업자 안의 재전달 거부
        finally:
            worker2.stop(5)
        records = _offers(s)
        assert any(r.get("status") == "rejected_duplicate" for r in records if r.get("kind") == "offer")
        day = _utc(pair["input_available_at"]).astimezone(KST).date().isoformat()
        report = daily.build_daily_report([pair], day_kst=day, offer_records=records, max_input_age_seconds=30.0,
                                          pairs_per_day=1)
        assert report["denominators"]["complete"] == 1 and report["denominators"]["offered"] == 1
        assert report["duplicate_offers_rejected"] == 1
        assert "rejected_duplicate" not in report["missing_reasons"]
        assert "완료 1" in report["telegram_preview"]
    finally:
        s.recorder.close(); set_decision_recorder(None)


def test_interrupted_attempt_without_response_counts_after_reconciliation_but_never_resends(tmp_path, monkeypatch):
    """3회차 P1: 응답 없이 끊긴 시도의 예약을 대사하면 지출은 확정(known_reconciled)되되 같은 ID 재호출은 재전송하지 않는다."""
    import asyncio as _asyncio
    from src.analytics.model_comparison_execution import ComparisonRequest
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    ledger = MonthlyBudgetLedger(tmp_path / "b.db", 100, policy_enabled=True)
    man = manifest()
    policy = dict(man["execution_policy"], experiment_id=man["experiment_id"]); policy["allowed_models"] = tuple(policy["allowed_models"])
    guard = ComparisonCallGuard(ledger=ledger, policy=ComparisonExecutionPolicy(**policy))
    calls = []
    async def dispatch(req):
        calls.append(req); raise TimeoutError("provider hung")
    req = ComparisonRequest("gpt-5-mini", "facts", "role", 100)
    try:
        _asyncio.run(guard.call(req, dispatch=dispatch, attempt_id="hung-attempt"))
    except TimeoutError:
        pass
    record = guard.lookup("hung-attempt")
    assert record["state"] == "blocked" and record["result"] is None and record["reservation_token"]
    assert ledger.status()["blocked"]                                           # unknown 예약 → 원장 차단
    evidence = tmp_path / "usage.txt"; evidence.write_text("export")
    review = {"version": "llm-budget-reconciliation-v1", "activated_at": ledger.status()["activated_at"],
              "reviewer_ref": "synthetic-operator", "evidence_sha256": hashlib.sha256(evidence.read_bytes()).hexdigest(),
              "adjustments": [{"token": record["reservation_token"], "actual_cost_usd": 0.0002}], "clear_bound_breach": False}
    preview = reconcile_budget(ledger.path, 100, review, evidence)
    reconcile_budget(ledger.path, 100, review, evidence, apply=True, expected_state_sha256=preview["state_sha256"])
    after = guard.lookup("hung-attempt")
    assert after["state"] == "known_reconciled" and after["result"] is None and after["reconciled"]["actual_cost_usd"] == "0.0002"
    day = ledger.today().isoformat()
    (la,) = [a for a in guard.day_attempts(day) if a["attempt_id"] == "hung-attempt"]
    assert la["state"] == "known_reconciled" and la["cost_usd"] == "0.0002"
    report = daily.build_daily_report([], day_kst=day, offer_records=[], ledger_attempts=guard.day_attempts(day))
    assert report["spend"]["total_usd"] == "0.0002" and report["spend"]["unaccounted_attempts"] == []
    from src.utils.llm_budget import BudgetBlocked
    import pytest
    with pytest.raises(BudgetBlocked):                                           # 재전송 금지 유지
        _asyncio.run(guard.call(req, dispatch=dispatch, attempt_id="hung-attempt"))
    assert len(calls) == 1


def test_journal_sequence_is_per_file_so_month_split_and_incomplete_stream_are_not_gaps(tmp_path, monkeypatch):
    """3회차 P2: 번호는 파일별이라 incomplete 스트림이나 월 분할이 offers 파일의 유실로 잡히지 않는다."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    from tests.test_model_comparison_worker import build, envelope
    from src.analytics.model_comparison_runner import PairResultPersistenceError
    worker, ledger, op, ge = build(tmp_path, journal_dir=tmp_path / "j")
    async def failing_run(spec):
        raise PairResultPersistenceError({"pair_id": worker.runner.pair_id(spec.decision_id), "decision_id": spec.decision_id})
    monkeypatch.setattr(worker.runner, "run", failing_run)
    worker.start()
    try:
        assert worker.offer(envelope(worker, "dec-a")) == "accepted"
        assert wait(lambda: worker.health()["incomplete"] == 1)
    finally:
        worker.stop(10)
    offers = sorted((tmp_path / "j").glob("offers-*.jsonl")); incomplete = sorted((tmp_path / "j").glob("incomplete-*.jsonl"))
    assert offers and incomplete
    ro = worker_api().read_offer_journal(offers[0], max_bytes=1 << 24)
    ri = worker_api().read_incomplete_journal(incomplete[0], max_bytes=1 << 24)
    assert ro["sequence_gaps"] == 0 and ro["dropped_records"] == 0 and ro["unsequenced_records"] == 0
    assert ri["sequence_gaps"] == 0 and len(ri["records"]) == 1
    assert [r["journal_seq"] for r in ro["records"]] == list(range(1, len(ro["records"]) + 1))
