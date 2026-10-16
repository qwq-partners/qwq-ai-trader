"""Task3 — 비교 작업자 생명주기. 임시 원장·가짜 공급자만, 네트워크·운영 경로 0."""
import asyncio
from contextlib import contextmanager
from dataclasses import asdict
from datetime import timedelta
import json
from pathlib import Path
import sqlite3
import threading
import time
from types import SimpleNamespace

import pytest

from src.analytics import model_comparison_input as mci
from src.analytics.model_comparison_execution import ComparisonCallGuard, ComparisonExecutionPolicy
from src.utils.llm import LLMProvider
from src.utils.llm_budget import BudgetBlocked, MonthlyBudgetLedger
from tests.test_model_comparison_runner import NOW, FakeGemini, FakeOpenAI

MODELS = {(LLMProvider.OPENAI, "light"): "gpt-5-mini", (LLMProvider.GEMINI, "light"): "gemini-3.1-flash-lite",
          (LLMProvider.OPENAI, "heavy"): "gpt-5.6-sol", (LLMProvider.GEMINI, "heavy"): "gemini-3.1-pro-preview"}
FAKE_LLM = SimpleNamespace(_get_model=lambda p, w: MODELS[(p, w)])
SUFFIX = "\n이 매수 시그널을 승인하시겠습니까? YES 또는 NO로 답하고, 한 줄 사유를 적어주세요."


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)


def api():
    from src.analytics import model_comparison_worker
    return model_comparison_worker


def manifest(**kw):
    controls = mci.operating_role_controls(FAKE_LLM, fallback_prompt_suffix=SUFFIX, fallback_timeout=10.0)
    value = {"experiment_id": "R", "enabled": True, "fixed_at": (NOW - timedelta(hours=1)).isoformat(),
             "horizon_seconds": 900, "strict_on_disagreement": controls["strict_on_disagreement"],
             "empty_retries": controls["empty_retries"], "current": controls["bull"],
             "candidate": dict(controls["bull"], reasoning_effort="low"), "bear": controls["bear"],
             "fallback_chain": controls["fallback_chain"],
             "fallback_chain_timeout_seconds": controls["fallback_chain_timeout_seconds"],
             "execution_policy": {"daily_attempt_cap": 40, "max_output_tokens": 400,
                                  "allowed_models": sorted(set(MODELS.values())),
                                  "experiment_usd_ceiling": "100", "valid_from": "2026-01-01",
                                  "valid_until": "2027-12-31", "pairs_per_day": 20,
                                  "max_common_context_bytes": 65536, "required_policy_mode": "normal"}}
    value.update(kw)
    return value


class Clock:
    def __init__(self, at=NOW):
        self.at = at

    def __call__(self):
        return self.at


def build(tmp_path, *, ledger=None, op=None, ge=None, clock=None, man=None, runner_clock=lambda: NOW, **kw):
    from src.analytics.model_comparison_runner import PairedComparisonRunner, SingleAttemptAdapter
    m = api()
    man = man or manifest()
    ledger = ledger or MonthlyBudgetLedger(tmp_path / "budget.db", 100, clock=lambda: NOW, policy_enabled=True)
    policy = dict(man["execution_policy"], experiment_id=man["experiment_id"])
    policy["allowed_models"] = tuple(policy["allowed_models"])
    guard = ComparisonCallGuard(ledger=ledger, policy=ComparisonExecutionPolicy(**policy))
    op, ge = op or FakeOpenAI(), ge or FakeGemini()
    runner = PairedComparisonRunner(guard=guard, adapter=SingleAttemptAdapter(openai=op, gemini=ge, clock=lambda: NOW),
                                    clock=runner_clock)
    worker = m.ComparisonWorker(runner, manifest=man, clock=clock or Clock(), **kw)
    return worker, ledger, op, ge


def envelope(worker, decision_id="dec-1", *, at=NOW, nonce=None, **kw):
    fields = dict(decision_id=decision_id, signal_id="sig-" + decision_id, symbol="005930", strategy="momentum",
                  score=90.0, market_regime="sideways", common_context="<entry_facts>{}</entry_facts>",
                  role_controls=mci.operating_role_controls(FAKE_LLM, fallback_prompt_suffix=SUFFIX, fallback_timeout=10.0),
                  neutral_sizing=None, producer=mci.producer_identity(**worker.producer_config()),
                  input_available_at=at.isoformat(), handed_off_at=at.isoformat(), nonce=nonce)
    fields.update(kw)
    return mci.build_envelope(**fields)


def wait(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return False


class GatedOpenAI(FakeOpenAI):
    """Blocks inside the provider until released — the request may already be at the provider."""
    def __init__(self):
        super().__init__()
        self.entered = threading.Event()
        self.gate = threading.Event()

    async def complete(self, prompt, system="", model=None, **kw):
        self.entered.set()  # Pair claim and this request's budget reservation precede dispatch.
        while not self.gate.is_set():
            await asyncio.sleep(0.005)
        return await super().complete(prompt, system, model, **kw)


def test_offer_runs_pair_off_loop_and_restart_does_not_spend_again(tmp_path):
    worker, ledger, op, ge = build(tmp_path)
    worker.start()
    env = envelope(worker)
    assert worker.offer(env) == "accepted"
    assert worker.offer(env) == "rejected_duplicate"
    assert wait(lambda: worker.health()["finished"] == 1)
    health = worker.stop(5)
    assert health["drained"] is True and health["incomplete"] == 0 and health["alive"] is False
    assert health["offered"] == 2 and health["accepted"] == 1 and health["rejected_duplicate"] == 1
    settled, calls = ledger.status()["settled_count"], (len(op.calls), len(ge.calls))
    assert settled == 3 and calls == (2, 1)
    with ledger._connect(readonly=True) as db:
        (row,), = db.execute("SELECT result FROM comparison_pairs").fetchall()
    row = json.loads(row)
    assert row["decision_id"] == "dec-1" and row["pricing_basis"] == "paired_live_shadow"
    assert row["live_provenance"] == "in_process_attested" and row["evaluation_anchor"] == "input_available_at"
    # Restart: new worker, same ledger, same decision delivered again → cached pair, no provider/charge.
    again, _, op2, ge2 = build(tmp_path, ledger=ledger, op=op, ge=ge)
    again.start()
    assert again.offer(env) == "accepted"
    assert wait(lambda: again.health()["finished"] == 1)
    again.stop(5)
    assert ledger.status()["settled_count"] == settled and (len(op.calls), len(ge.calls)) == calls


def test_disabled_unstarted_or_stopped_worker_rejects_without_queueing(tmp_path):
    worker, *_ = build(tmp_path)
    assert worker.offer(envelope(worker)) == "rejected_disabled"
    worker.start()
    worker.stop(5)
    assert worker.offer(envelope(worker, "dec-2")) == "rejected_disabled"
    assert worker.health()["rejected_disabled"] == 2 and worker.health()["accepted"] == 0


def test_unattested_tampered_invalid_stale_and_mismatched_controls_are_rejected(tmp_path):
    clock = Clock()
    worker, ledger, op, ge = build(tmp_path, clock=clock)
    worker.start()
    try:
        assert worker.offer(envelope(worker, nonce=b"other-process")) == "rejected_unattested"
        tampered = dict(envelope(worker), symbol="000660")
        assert worker.offer(tampered) == "rejected_unattested"
        reversed_ = envelope(worker, handed_off_at=(NOW - timedelta(seconds=1)).isoformat())
        assert worker.offer(reversed_) == "rejected_invalid"
        mismatch = envelope(worker, role_controls=dict(envelope(worker)["role_controls"],
                                                      bull=dict(envelope(worker)["role_controls"]["bull"], model=None)))
        assert worker.offer(mismatch) == "rejected_control_mismatch"
        foreign_cfg = envelope(worker, producer=mci.producer_identity(experiment_id="R", config_sha256="0" * 64))
        assert worker.offer(foreign_cfg) == "rejected_control_mismatch"
        clock.at = NOW + timedelta(seconds=6)
        assert worker.offer(envelope(worker)) == "rejected_stale"
    finally:
        health = worker.stop(5)
    assert (health["rejected_unattested"], health["rejected_invalid"], health["rejected_control_mismatch"],
            health["rejected_stale"], health["accepted"]) == (2, 1, 2, 1, 0)
    assert op.calls == [] and ge.calls == [] and ledger.status()["settled_count"] == 0


def test_full_queue_and_expired_dispatch_are_counted_not_waited_on(tmp_path):
    clock, op = Clock(), GatedOpenAI()
    worker, ledger, *_ = build(tmp_path, clock=clock, op=op, max_queue=1)
    worker.start()
    try:
        assert worker.offer(envelope(worker, "dec-1")) == "accepted"
        assert wait(lambda: worker.health()["started"] == 1)      # in flight, blocked at the provider
        assert worker.offer(envelope(worker, "dec-2")) == "accepted"
        started = time.monotonic()
        assert worker.offer(envelope(worker, "dec-3")) == "rejected_full"
        assert time.monotonic() - started < 0.05                   # offer never waits on the worker
        clock.at = NOW + timedelta(seconds=6)                       # dec-2 ages in the queue
        op.gate.set()
        assert wait(lambda: worker.health()["expired"] == 1 and worker.health()["finished"] == 1)
    finally:
        health = worker.stop(5)
    assert health["rejected_full"] == 1 and health["expired"] == 1 and health["started"] == 1


def test_stop_deadline_cancels_in_flight_and_queued_as_incomplete_without_resend(tmp_path):
    op = GatedOpenAI()
    worker, ledger, _, ge = build(tmp_path, op=op)
    worker.start()
    assert worker.offer(envelope(worker, "dec-1")) == "accepted"
    assert wait(lambda: worker.health()["started"] == 1)
    assert worker.offer(envelope(worker, "dec-2")) == "accepted"
    health = worker.stop(0.2)
    assert health["drained"] is False and health["alive"] is False
    assert health["incomplete"] == 2 and health["finished"] == 0
    assert ledger.status()["unresolved_count"] >= 1               # possibly-received requests stay unresolved
    from src.analytics.model_comparison_runner import PairedComparisonRunner
    retry, *_ = build(tmp_path, ledger=ledger, op=op, ge=ge)
    calls = len(op.calls) + len(ge.calls)
    retry.start()
    assert retry.offer(envelope(worker, "dec-1")) == "accepted"
    assert wait(lambda: retry.health()["incomplete"] == 1)
    retry.stop(5)
    assert len(op.calls) + len(ge.calls) == calls                   # never re-sent automatically
    assert any("pair_incomplete" in reason for _, reason in retry.health()["last_errors"])


def test_unpersisted_pair_result_is_kept_as_incomplete_evidence(tmp_path, monkeypatch):
    worker, ledger, op, ge = build(tmp_path)
    original = ledger._connect

    class Connection:
        def __init__(self, db): self.db = db
        def execute(self, sql, *args):
            if sql.startswith("UPDATE comparison_pairs SET result="):
                raise sqlite3.OperationalError("injected pair storage failure")
            return self.db.execute(sql, *args)
        def __getattr__(self, name): return getattr(self.db, name)

    @contextmanager
    def connect(**kwargs):
        with original(**kwargs) as db:
            yield Connection(db)
    monkeypatch.setattr(ledger, "_connect", connect)
    worker.start()
    assert worker.offer(envelope(worker)) == "accepted"
    assert wait(lambda: worker.health()["incomplete"] == 1)
    worker.stop(5)
    (kept,) = worker.incomplete_results
    assert kept["decision_id"] == "dec-1" and kept["actual_provider_attempts"] == 3
    assert worker.health()["finished"] == 0


def test_manifest_must_be_enabled_and_match_guard_experiment(tmp_path):
    with pytest.raises(ValueError, match="manifest_disabled"):
        build(tmp_path, man=manifest(enabled=False))
    worker, ledger, *_ = build(tmp_path)
    from src.analytics.model_comparison_worker import ComparisonWorker
    with pytest.raises(ValueError, match="experiment_mismatch"):
        ComparisonWorker(worker.runner, manifest=manifest(experiment_id="other"))
    with pytest.raises(ValueError, match="max_concurrent_pairs"):
        ComparisonWorker(worker.runner, manifest=manifest(), max_concurrent_pairs=2)


def test_launcher_wiring_is_default_off_and_disabled_manifest_creates_no_clients(tmp_path, monkeypatch):
    m = api()
    created = []
    monkeypatch.setattr(m, "_raw_clients", lambda: created.append(1) or (None, None))
    cross = SimpleNamespace(_comparison_handoff=None, _comparison_producer_config=None)
    assert m.install_from_env(cross, env={}) is None
    assert m.install_from_env(cross, env={"COMPARISON_WORKER": "1"}) is None
    path = tmp_path / "R.json"
    path.write_text(json.dumps(manifest(enabled=False)))
    assert m.install_from_env(cross, env={"COMPARISON_EXPERIMENT_MANIFEST": str(path)}) is None
    assert m.install_from_env(cross, env={"COMPARISON_WORKER": "1", "COMPARISON_EXPERIMENT_MANIFEST": str(path)}) is None
    assert m.install_from_env(cross, env={"COMPARISON_WORKER": "1", "COMPARISON_EXPERIMENT_MANIFEST": str(tmp_path / "x")}) is None
    assert created == [] and cross._comparison_handoff is None and cross._comparison_producer_config is None


def test_cross_validator_to_worker_end_to_end_keeps_verdict_and_ids(tmp_path, monkeypatch):
    """Real cross_validator hook → worker → pair. Slow/failed worker never changes the live verdict."""
    from src.analytics.llm_decision_record import DecisionRecorder, set_decision_recorder
    from tests.test_model_comparison_input import _LLM, _cross
    from src.analytics.model_comparison_runner import PairedComparisonRunner, SingleAttemptAdapter
    m = api()
    recorder = DecisionRecorder(tmp_path / "decisions", enabled=True)
    set_decision_recorder(recorder)
    ledger = MonthlyBudgetLedger(tmp_path / "budget.db", 100, policy_enabled=True)
    guard = ComparisonCallGuard(ledger=ledger, policy=ComparisonExecutionPolicy(
        experiment_id="R", daily_attempt_cap=40, allowed_models=tuple(sorted(set(MODELS.values()))), max_output_tokens=400,
        experiment_usd_ceiling='100', valid_from='2026-01-01', valid_until='2027-12-31',
        pairs_per_day=20, max_common_context_bytes=65536))
    op, ge = GatedOpenAI(), FakeGemini()
    runner = PairedComparisonRunner(guard=guard, adapter=SingleAttemptAdapter(openai=op, gemini=ge))
    worker = m.ComparisonWorker(runner, manifest=manifest(fixed_at="2026-01-01T00:00:00+00:00"), max_input_age_seconds=30.0)
    try:
        cross = _cross(monkeypatch, _LLM())
        baseline = asyncio.run(cross.llm_second_check("005930", "momentum", 90, {}, "sideways", signal_id="sig-e2e"))
        base_meta = {k: v for k, v in cross.last_llm_decision.items() if k != "decision_id"}
        worker.start()
        m.attach(worker, cross)
        assert asyncio.run(cross.llm_second_check("005930", "momentum", 90, {}, "sideways", signal_id="sig-e2e")) is baseline
        meta = dict(cross.last_llm_decision)
        assert meta.pop("comparison_handoff") == "accepted"
        assert {k: v for k, v in meta.items() if k != "decision_id"} == base_meta
        assert wait(lambda: worker.health()["started"] == 1)      # the worker is blocked; the decision already returned
        op.gate.set()
        assert wait(lambda: worker.health()["finished"] == 1)
    finally:
        worker.stop(5)
        recorder.close()
        set_decision_recorder(None)
    rows = [json.loads(l) for f in (tmp_path / "decisions").glob("*.jsonl") for l in f.read_text().splitlines()]
    with ledger._connect(readonly=True) as db:
        (pair,), = db.execute("SELECT result FROM comparison_pairs").fetchall()
    pair = json.loads(pair)
    assert pair["decision_id"] == rows[-1]["decision_id"] == meta["decision_id"]
    assert pair["input_available_at"] == rows[-1]["inputs"]["collected_at"]
    assert pair["current"]["bull_verdict"] is False and pair["candidate"]["bull_verdict"] is True
    assert pair["current"]["queue_delay_ms"] is not None and pair["current"]["queue_delay_ms"] >= 0


def test_dryrun_entrypoint_runs_offline_under_isolation_guard(capsys):
    import importlib.util
    path = Path(__file__).resolve().parents[1] / "scripts" / "dev" / "comparison_worker_dryrun.py"
    spec = importlib.util.spec_from_file_location("comparison_worker_dryrun", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.main() == 0
    out = json.loads(capsys.readouterr().out)
    assert out["offer"] == "accepted" and out["health"]["finished"] == 1 and out["ledger"]["settled_count"] == 3


def capped(pairs_per_day=1, max_bytes=65536, **kw):
    man = manifest(**kw)
    man["execution_policy"] = dict(man["execution_policy"], pairs_per_day=pairs_per_day,
                                   max_common_context_bytes=max_bytes)
    return man


def journal(worker, kind="offers"):
    from src.analytics.model_comparison_worker import read_incomplete_journal, read_offer_journal
    reader = read_offer_journal if kind == "offers" else read_incomplete_journal
    return [r for p in sorted(worker.journal_dir.glob(f"{kind}-*.jsonl"))
            for r in reader(p, max_bytes=1 << 22)["records"]]


# --- P1 #1: 하루 쌍 상한·입력 크기는 offer 에서(큐 전) 거부되고 재시작 뒤에도 유지된다 --------------

def test_offer_rejects_daily_cap_and_large_input_before_queue_and_cap_survives_restart(tmp_path):
    man = capped(pairs_per_day=1, max_bytes=16384)
    worker, ledger, op, ge = build(tmp_path, man=man)
    worker.start()
    try:
        assert worker.offer(envelope(worker, "dec-1")) == "accepted"
        assert wait(lambda: worker.health()["finished"] == 1)
        calls = len(op.calls) + len(ge.calls)
        assert worker.offer(envelope(worker, "dec-2")) == "rejected_daily_pair_cap"
        big = envelope(worker, "dec-big", common_context="x" * 20000)
        assert worker.offer(big) == "rejected_input_too_large"
        assert worker.health()["queue_depth"] == 0
    finally:
        worker.stop(5)
    assert len(op.calls) + len(ge.calls) == calls and ledger.status()["settled_count"] == 3
    # 재시작: 당일 입장 수를 원장에서 복원 → 새 판단은 거부, 같은 판단 재전달은 저장 결과(지출 0).
    again, *_ = build(tmp_path, ledger=ledger, op=op, ge=ge, man=man)
    again.start()
    try:
        assert again.offer(envelope(again, "dec-3")) == "rejected_daily_pair_cap"
        assert again.offer(envelope(again, "dec-1")) == "accepted"
        assert wait(lambda: again.health()["finished"] == 1)
    finally:
        health = again.stop(5)
    assert health["rejected_daily_pair_cap"] == 1 and len(op.calls) + len(ge.calls) == calls
    assert ledger.status()["settled_count"] == 3
    with ledger._connect(readonly=True) as db:
        assert db.execute("SELECT COUNT(*) FROM comparison_pairs").fetchone()[0] == 1   # 거부분은 큐·원장에 없다


def test_expired_accepted_offer_does_not_consume_the_daily_pair(tmp_path):
    clock, op = Clock(), GatedOpenAI()
    worker, ledger, *_ = build(tmp_path, clock=clock, op=op, man=capped(pairs_per_day=2), runner_clock=clock)
    worker.start()
    try:
        assert worker.offer(envelope(worker, "dec-0")) == "accepted"
        assert wait(lambda: worker.health()["started"] == 1)
        # started 는 claim 전이다. 공급자 진입 신호로 claim 완료를 기다린 뒤 시계를 옮긴다.
        # 실행 중 외부 DB 조회는 worker의 쓰기와 경쟁하므로 영속 결과는 stop 뒤에 확인한다.
        assert op.entered.wait(5)
        assert worker.offer(envelope(worker, "dec-1")) == "accepted"
        clock.at = NOW + timedelta(seconds=6)
        op.gate.set()
        assert wait(lambda: worker.health()["expired"] == 1)
        assert worker.offer(envelope(worker, "dec-2", at=clock.at)) == "accepted"   # 만료분은 쌍 수에서 빠졌다
        assert worker.offer(envelope(worker, "dec-3", at=clock.at)) == "rejected_daily_pair_cap"
    finally:
        health = worker.stop(5)
    assert health["drained"] is True
    assert worker.runner.admitted_pair_ids(ledger.today().isoformat()) == {
        worker.runner.pair_id("dec-0"), worker.runner.pair_id("dec-2")}


def test_pair_failing_before_claim_frees_the_day_slot_after_ledger_resync(tmp_path):
    clock = Clock()
    worker, ledger, op, ge = build(tmp_path, clock=clock, man=capped(pairs_per_day=1))   # 러너 시계는 NOW 고정
    worker.start()
    try:
        clock.at = NOW + timedelta(seconds=3)
        future = envelope(worker, "dec-future", at=clock.at)        # 러너 시각보다 미래 입력 → claim 전 ValueError
        assert worker.offer(future) == "accepted"
        assert wait(lambda: worker.health()["incomplete"] == 1)
        clock.at = NOW
        assert worker.offer(envelope(worker, "dec-ok")) == "accepted"   # 원장 입장 없음 → 그날 수에서 빠졌다
        assert wait(lambda: worker.health()["finished"] == 1)
        assert worker.offer(envelope(worker, "dec-late")) == "rejected_daily_pair_cap"
    finally:
        worker.stop(5)
    assert ledger.status()["settled_count"] == 3


# --- P1 #3: manifest 의 체인 시한과 봉투 운영 값이 다르면 비교가 아니다 --------------------------------

def test_fallback_chain_timeout_mismatch_is_control_mismatch_and_reaches_pair_spec(tmp_path):
    worker, ledger, op, ge = build(tmp_path)
    worker.start()
    try:
        env = envelope(worker)
        other = envelope(worker, "dec-2", role_controls=dict(env["role_controls"], fallback_chain_timeout_seconds=12.0))
        assert worker.offer(other) == "rejected_control_mismatch"
        assert worker._spec(dict(env, received_at=NOW.isoformat())).fallback_chain_timeout_seconds == 10.0
    finally:
        worker.stop(5)
    man = manifest()
    man.pop("fallback_chain_timeout_seconds")
    with pytest.raises(KeyError):
        build(tmp_path, ledger=ledger, man=man)


# --- P1 #4: offer·전이·incomplete 행은 영속 저널에 남는다 ---------------------------------------------

def test_offers_transitions_and_rejections_are_journaled_off_loop(tmp_path, monkeypatch):
    import builtins
    worker, ledger, op, ge = build(tmp_path)
    opened = []
    real_open = builtins.open

    def spy(*args, **kwargs):
        opened.append(threading.current_thread().name)
        return real_open(*args, **kwargs)
    monkeypatch.setattr(builtins, "open", spy)
    worker.start()
    env = envelope(worker)
    caller = threading.current_thread().name
    before = len([t for t in opened if t == caller])
    assert worker.offer(env) == "accepted" and worker.offer(env) == "rejected_duplicate"
    assert worker.offer(envelope(worker, "dec-x", nonce=b"other")) == "rejected_unattested"
    assert len([t for t in opened if t == caller]) == before         # 운영 루프(호출 스레드)는 파일을 열지 않는다
    assert wait(lambda: worker.health()["finished"] == 1)
    health = worker.stop(5)
    monkeypatch.setattr(builtins, "open", real_open)
    assert health["journal_drained"] is True and health["journal_dropped"] == health["journal_failed"] == 0
    records = journal(worker)
    offers = [r for r in records if r["kind"] == "offer"]
    assert [r["status"] for r in offers] == ["accepted", "rejected_duplicate", "rejected_unattested"]
    assert offers[0]["envelope"] == env and offers[0]["envelope"]["attestation"]["kind"] == "in_process_nonce"
    assert offers[0]["received_at"] == NOW.isoformat() and offers[2]["reason"] == "unattested"
    transitions = [(r["status"], r["pair_id"]) for r in records if r["kind"] == "transition"]
    pair_id = worker.runner.pair_id("dec-1")
    assert transitions == [("started", pair_id), ("finished", pair_id)]
    assert [p.name for p in worker.journal_dir.glob("*.jsonl")] == ["offers-2026-10.jsonl"]   # KST 월


def test_unpersisted_pair_row_is_journaled_and_restorable_without_provider_calls(tmp_path, monkeypatch):
    from src.analytics import model_comparison_recovery as recovery
    worker, ledger, op, ge = build(tmp_path)
    original = ledger._connect

    class Connection:
        def __init__(self, db): self.db = db
        def execute(self, sql, *args):
            if sql.startswith("UPDATE comparison_pairs SET result="):
                raise sqlite3.OperationalError("injected pair storage failure")
            return self.db.execute(sql, *args)
        def __getattr__(self, name): return getattr(self.db, name)

    @contextmanager
    def connect(**kwargs):
        with original(**kwargs) as db:
            yield Connection(db)
    monkeypatch.setattr(ledger, "_connect", connect)
    worker.start()
    assert worker.offer(envelope(worker)) == "accepted"
    assert wait(lambda: worker.health()["incomplete"] == 1)
    worker.stop(5)
    monkeypatch.setattr(ledger, "_connect", original)
    (record,) = journal(worker, "incomplete")
    (kept,) = worker.incomplete_results
    assert record["row"] == kept and record["pair_id"] == kept["pair_id"]
    statuses = [(r["status"], r["reason"]) for r in journal(worker) if r["kind"] == "transition"]
    assert statuses[-1] == ("incomplete", "pair_result_unpersisted")
    with ledger._connect(readonly=True) as db:
        manifest_json = db.execute("SELECT manifest FROM comparison_pairs").fetchone()[0]
    evidence = next(worker.journal_dir.glob("incomplete-*.jsonl"))
    assert recovery.restore_pair_result(worker.runner.guard, kept["pair_id"], record["row"],
                                        manifest_json=manifest_json, evidence_path=evidence)["restored"] is True
    calls = len(op.calls) + len(ge.calls)
    again, *_ = build(tmp_path, ledger=ledger, op=op, ge=ge)
    again.start()
    assert again.offer(envelope(again)) == "accepted"
    assert wait(lambda: again.health()["finished"] == 1)
    again.stop(5)
    assert len(op.calls) + len(ge.calls) == calls


def test_journal_full_or_unwritable_never_changes_offer_result(tmp_path):
    blocked_dir = tmp_path / "not-a-dir"
    blocked_dir.write_text("file in the way")
    worker, *_ = build(tmp_path, journal_dir=blocked_dir / "journal")
    worker.start()
    try:
        assert worker.offer(envelope(worker)) == "accepted"
        assert wait(lambda: worker.health()["finished"] == 1)
    finally:
        health = worker.stop(5)
    assert health["journal_failed"] >= 1 and health["finished"] == 1
    (tmp_path / "second").mkdir()
    full, *_ = build(tmp_path / "second", journal_queue=1)
    gate = threading.Event()
    full._journal_run = gate.wait                  # 저널 소비를 멈춰 큐 포화를 결정적으로 만든다
    full.start()
    statuses = [full.offer(envelope(full, f"dec-{i}")) for i in range(3)]
    assert statuses == ["accepted"] * 3 and full.health()["journal_dropped"] == 2
    full._journal.get_nowait()
    gate.set()
    full.stop(5)


def test_journal_readers_skip_corrupt_lines_and_refuse_partial_reads(tmp_path):
    from src.analytics.model_comparison_worker import read_incomplete_journal, read_offer_journal
    path = tmp_path / "offers-2026-10.jsonl"
    good = {"schema_version": "comparison-offer-v1", "kind": "offer", "decision_id": "d", "status": "accepted"}
    path.write_text(json.dumps(good) + "\n{broken\n" + json.dumps({"kind": "offer"}) + "\n")
    out = read_offer_journal(path, max_bytes=1 << 20)
    assert out["records"] == [good] and out["skipped_lines"] == 2 and out["missing"] is False
    with pytest.raises(ValueError, match="journal_too_large"):
        read_offer_journal(path, max_bytes=10)
    assert read_offer_journal(tmp_path / "none.jsonl", max_bytes=10)["missing"] is True
    bad_sha = tmp_path / "incomplete-2026-10.jsonl"
    bad_sha.write_text(json.dumps({"schema_version": "comparison-offer-v1", "kind": "incomplete_result",
                                   "pair_id": "p", "row_sha256": "0" * 64, "row": {"pair_id": "p"}}) + "\n")
    out = read_incomplete_journal(bad_sha, max_bytes=1 << 20)
    assert out["records"] == [] and out["skipped_lines"] == 1


def test_bot_shutdown_stops_worker_before_llm_manager_closes_shared_ledger(monkeypatch):
    from scripts.run_trader import UnifiedTradingBot
    from src.analytics import llm_decision_record as rec
    from src.utils import llm
    order = []

    async def close_llm():
        order.append("llm_closed")
    monkeypatch.setattr(llm, "close_llm_manager", close_llm)
    monkeypatch.setattr(rec, "_recorder", None)
    bot = object.__new__(UnifiedTradingBot)
    bot.broker = bot._us_engine = bot.dashboard = bot.ws_feed = bot._token_manager = None
    bot._entry_observation_runtime = None
    bot._comparison_worker = SimpleNamespace(stop=lambda deadline: order.append(("worker_stop", deadline)) or
                                             {"drained": True, "finished": 0, "incomplete": 0, "expired": 0})
    asyncio.run(bot.shutdown())
    assert order == [("worker_stop", 20), "llm_closed"]
