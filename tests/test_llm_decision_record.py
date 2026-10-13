"""64차 — LLM 진입 판단 재생 기록(llm-decision-v1): 시도 기록·판단 기록·기록기·엔진 조인 키."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from src.analytics import llm_decision_record as rec
from src.analytics.llm_decision_record import DecisionRecorder, attempt_record, decision_record, set_decision_recorder
from src.core.adversarial_validator import BEAR_SUFFIX, BULL_SUFFIX, AdversarialValidator
from src.core.cross_validator import CrossStrategyValidator
from src.utils.llm import LLMProvider, LLMResponse


@pytest.fixture
def recorder(tmp_path):
    r = DecisionRecorder(tmp_path / "decisions", enabled=True, max_file_bytes=1 << 20)
    set_decision_recorder(r)
    yield r
    r.close()
    set_decision_recorder(None)


def _resp(content="APPROVE 근거", provider=LLMProvider.OPENAI, **kw):
    base = dict(model="gpt-x", provider=provider, input_tokens=120, output_tokens=8, latency_ms=321.0,
                requested_model="gpt-x", resolved_model="gpt-x-2026-01", usage_known=True,
                budget_policy_mode="default", budget_policy_version="v1", budget_priority="essential",
                budget_reservation_token="tok-1")
    base.update(kw)
    return LLMResponse(content, **base)


# ── attempt_record 결과 분류 ──────────────────────────────────────────────

def test_attempt_record_classifies_outcomes_and_model_identity():
    ok = attempt_record("bull", LLMProvider.OPENAI, rec.now_kst(), _resp(), role_system_sha256="s", role_suffix=BULL_SUFFIX)
    assert ok["outcome"] == "success" and ok["model_identity"] == "provider_reported"
    assert ok["budget"]["reservation_token"] == "tok-1" and ok["budget"]["priority"] == "essential"
    assert ok["response_sha256"] == rec.sha256_text("APPROVE 근거") and ok["role_suffix"] == BULL_SUFFIX
    empty = attempt_record("bear", LLMProvider.GEMINI, None, _resp(content="   "), role_system_sha256=None, role_suffix="x")
    assert empty["outcome"] == "empty"
    unverified = attempt_record("bull", LLMProvider.OPENAI, None, _resp(resolved_model=None), role_system_sha256=None, role_suffix="x")
    assert unverified["model_identity"] == "unverified" and unverified["resolved_model"] is None
    blocked = attempt_record("bull", LLMProvider.OPENAI, None,
                             _resp(content="", success=False, budget_block_reason="monthly_limit", usage_known=False),
                             role_system_sha256=None, role_suffix="x")
    assert blocked["outcome"] == "budget_blocked" and blocked["estimated_cost_usd"] is None
    failed = attempt_record("bull", LLMProvider.OPENAI, None, _resp(content="", success=False, error="HTTP 500"),
                            role_system_sha256=None, role_suffix="x")
    assert failed["outcome"] == "failed" and failed["error"] == "HTTP 500"
    exc = attempt_record("bear", LLMProvider.GEMINI, None, RuntimeError("boom"), role_system_sha256=None, role_suffix="x")
    assert exc["outcome"] == "exception" and "boom" in exc["error"]
    timeout = attempt_record("single", None, None, None, timed_out=True, role_system_sha256=None, role_suffix="x")
    assert timeout["outcome"] == "timeout" and timeout["provider"] is None


def test_attempt_record_truncates_long_response_but_keeps_full_hash():
    long = "A" * (rec.MAX_TEXT_CHARS + 500)
    row = attempt_record("bull", LLMProvider.OPENAI, None, _resp(content=long), role_system_sha256=None, role_suffix="x")
    assert len(row["response_text"]) == rec.MAX_TEXT_CHARS and row["response_sha256"] == rec.sha256_text(long)


def test_decision_record_keeps_source_observed_at_null_and_hashes_context():
    row = decision_record(signal_id="sig-1", symbol="005930", strategy="momentum", score=90, market_regime="bear",
                          common_context="<entry_facts>{}</entry_facts>", stage="adversarial", attempts=[],
                          approved=True, result={"confidence": 1.0}, budget_context=None)
    assert row["schema_version"] == "llm-decision-v1" and row["signal_id"] == "sig-1"
    assert row["inputs"]["source_observed_at"] is None            # 현재 시각으로 채우지 않는다
    assert row["inputs"]["common_context_sha256"] == rec.sha256_text("<entry_facts>{}</entry_facts>")
    assert row["first_quote_after"] is None and row["decision_available_at"]


# ── 기록기 ───────────────────────────────────────────────────────────────

def test_recorder_writes_monthly_jsonl_and_reports_drops(tmp_path):
    r = DecisionRecorder(tmp_path / "d", enabled=True, max_file_bytes=1 << 20, queue_size=2)
    row = decision_record(signal_id="s", symbol="X", strategy="s", score=1, market_regime="r", common_context="c",
                          stage="adversarial", attempts=[], approved=True, result={}, budget_context=None)
    assert r.submit(row) == "queued"
    assert r.flush(5.0) and r.stats["written"] == 1
    files = list((tmp_path / "d").glob("decisions-*.jsonl"))
    assert len(files) == 1 and files[0].name == f"decisions-{row['decision_available_at'][:7]}.jsonl"
    assert json.loads(files[0].read_text().splitlines()[0])["decision_id"] == row["decision_id"]
    # 파일 상한 초과 → 기록은 버리되 통계에 남긴다
    r.max_file_bytes = files[0].stat().st_size + 1
    assert r.submit(row) == "queued" and r.flush(5.0) and r.stats["dropped_file_cap"] == 1
    assert len(files[0].read_text().splitlines()) == 1
    r.close()


def test_recorder_disabled_and_default_is_opt_in(tmp_path, monkeypatch):
    monkeypatch.delenv(rec.ENV_FLAG, raising=False)
    assert DecisionRecorder(tmp_path).enabled is False            # 모듈 기본은 명시 "1" 일 때만
    monkeypatch.setenv(rec.ENV_FLAG, "1")
    assert DecisionRecorder(tmp_path).enabled is True
    monkeypatch.setenv(rec.ENV_FLAG, "0")
    r = rec.install_production_recorder()                          # 운영 런처: "0" 이면 꺼짐
    assert r.enabled is False and r.submit({"x": 1}) == "disabled"
    set_decision_recorder(None)


# ── 적대 검증 → 시도 메타 ─────────────────────────────────────────────────

class _LLM:
    def __init__(self, bull, bear, single="YES", raise_for=None):
        self.answers = {LLMProvider.OPENAI: bull, LLMProvider.GEMINI: bear}
        self.single, self.raise_for, self.prompts = single, raise_for, []

    async def complete_with(self, prompt, *, provider, **kwargs):
        self.prompts.append(prompt)
        if self.raise_for == provider:
            raise RuntimeError("provider down")
        return _resp(self.answers[provider], provider=provider)

    async def complete(self, prompt, **kwargs):
        self.prompts.append(prompt)
        return _resp(self.single, provider=LLMProvider.OPENAI, resolved_model=None)


def test_adversarial_result_carries_two_attempts_with_role_prompt_fingerprints():
    adv = AdversarialValidator(_LLM("APPROVE 좋다", "REJECT 위험"))
    result = asyncio.run(adv.validate("005930", "momentum", 90, {}, "bear", common_context="<entry_facts>{}</entry_facts>"))
    attempts = result.meta["attempts"]
    assert [a["role"] for a in attempts] == ["bull", "bear"] and result.disagreed
    assert attempts[0]["role_suffix"] == BULL_SUFFIX and attempts[1]["role_suffix"] == BEAR_SUFFIX
    assert attempts[0]["outcome"] == attempts[1]["outcome"] == "success"
    assert attempts[0]["requested_at"] and attempts[0]["responded_at"] >= attempts[0]["requested_at"]


def test_adversarial_exception_on_one_side_is_recorded_not_hidden():
    adv = AdversarialValidator(_LLM("APPROVE", "unused", raise_for=LLMProvider.GEMINI))
    result = asyncio.run(adv.validate("005930", "momentum", 90, {}, "bear", common_context="ctx"))
    bull, bear = result.meta["attempts"]
    assert bull["outcome"] == "success" and bear["outcome"] == "exception" and "provider down" in bear["error"]
    assert result.approved is True and result.confidence == 0.5          # 기존 편측 판정 유지


# ── 크로스 검증 → 판단 기록 ──────────────────────────────────────────────

def _cross(monkeypatch, llm):
    from src.data.providers import disclosure_feed
    cross = CrossStrategyValidator(
        trade_memory=SimpleNamespace(get_context_for_signal=lambda *a: "기억"),
        trade_wiki=SimpleNamespace(query=lambda *a: "위키", query_symbol=lambda *a: "노트"))
    cross._llm_manager = llm
    cross._adversarial = AdversarialValidator(llm)
    monkeypatch.setattr(cross, "_load_panel_outlook", lambda: None)
    cross._panel_risk_factors, cross._panel_regime = [], "bear"
    monkeypatch.setattr(disclosure_feed, "get_symbol_disclosure_context", lambda *a: "")
    return cross


def _records(recorder):
    assert recorder.flush(5.0)
    rows = []
    for f in sorted(recorder.directory.glob("*.jsonl")):
        rows += [json.loads(l) for l in f.read_text().splitlines()]
    return rows


def test_adversarial_decision_is_recorded_with_signal_id_and_context(monkeypatch, recorder):
    llm = _LLM("APPROVE 좋다", "ACCEPT 감수")
    cross = _cross(monkeypatch, llm)
    assert asyncio.run(cross.llm_second_check("005930", "momentum", 90, {}, "sideways", signal_id="sig-42")) is True
    rows = _records(recorder)
    assert len(rows) == 1
    row = rows[0]
    assert row["signal_id"] == "sig-42" and row["stage"] == "adversarial" and row["result"]["approved"] is True
    assert len(row["attempts"]) == 2 and "<entry_facts>" in row["inputs"]["common_context"]
    # 기록된 공통 입력 + 역할 접미 = 실제로 보낸 프롬프트 (재생 가능)
    assert row["inputs"]["common_context"] + BULL_SUFFIX == llm.prompts[0]
    assert cross.last_llm_decision == {"decision_id": row["decision_id"], "record_status": "queued",
                                       "stage": "adversarial", "attempts": 2}


def test_fallback_decision_records_three_attempts_and_rejection(monkeypatch, recorder):
    cross = _cross(monkeypatch, _LLM("unparseable", "unparseable", single="NO 위험"))
    assert asyncio.run(cross.llm_second_check("005930", "momentum", 90, {}, "sideways", signal_id="sig-7")) is False
    row = _records(recorder)[0]
    assert row["stage"] == "single_fallback" and row["result"]["approved"] is False
    assert [a["role"] for a in row["attempts"]] == ["bull", "bear", "single"]
    assert row["attempts"][2]["model_identity"] == "unverified"
    assert row["result"]["adversarial_failed_reason"].startswith("양쪽 판정 불가")


def test_record_failure_does_not_change_verdict(monkeypatch, recorder):
    cross = _cross(monkeypatch, _LLM("APPROVE", "ACCEPT"))
    monkeypatch.setattr(rec, "decision_record", lambda **kw: (_ for _ in ()).throw(RuntimeError("disk")))
    assert asyncio.run(cross.llm_second_check("005930", "momentum", 90, {}, "sideways", signal_id="s")) is True
    assert cross.last_llm_decision["record_status"] == "error" and cross.last_llm_decision["decision_id"] is None


def test_without_signal_id_record_still_written_with_null_link(monkeypatch, recorder):
    cross = _cross(monkeypatch, _LLM("APPROVE", "ACCEPT"))
    assert asyncio.run(cross.llm_second_check("005930", "momentum", 90, {}, "sideways")) is True
    assert _records(recorder)[0]["signal_id"] is None
