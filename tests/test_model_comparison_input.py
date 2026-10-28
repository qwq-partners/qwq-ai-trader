"""Task2 — 판단 전 비교 입력 봉투·ID·출처. 가짜 공급자만, 네트워크·운영 경로 0."""
import asyncio
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.analytics import llm_decision_record as rec
from src.analytics import model_comparison_input as mci
from src.analytics.llm_decision_record import DecisionRecorder, set_decision_recorder
from src.core.adversarial_validator import AdversarialValidator, MAX_TOKENS, VERDICT_SEED
from src.core.cross_validator import CrossStrategyValidator
from src.utils.llm import LLMProvider, LLMResponse

T0 = datetime(2026, 10, 13, 10, 0, tzinfo=timezone(timedelta(hours=9)))


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)


def _controls():
    return mci.operating_role_controls(SimpleNamespace(_get_model=lambda p, w: f"{p.value}-{w}"),
                                       fallback_prompt_suffix="\nYES/NO", fallback_timeout=10.0)


def _envelope(**kw):
    base = dict(decision_id="dec-1", signal_id="sig-1", symbol="005930", strategy="momentum", score=90.0,
                market_regime="sideways", common_context="<entry_facts>{}</entry_facts>",
                role_controls=_controls(), neutral_sizing=None,
                producer=mci.producer_identity(experiment_id="R1", config_sha256="c" * 64),
                input_available_at=T0.isoformat(), handed_off_at=(T0 + timedelta(milliseconds=3)).isoformat())
    base.update(kw)
    return mci.build_envelope(**base)


def test_envelope_is_attested_and_hash_excludes_transport_times():
    env = _envelope()
    assert env["schema_version"] == "comparison-input-envelope-v1"
    assert env["attestation"]["kind"] == "in_process_nonce"
    assert mci.verify_envelope(env) is None
    moved = dict(env, received_at=(T0 + timedelta(seconds=1)).isoformat())
    assert mci.verify_envelope(moved) is None and mci.body_sha256(moved) == env["envelope_sha256"]


def test_role_controls_copy_operating_adversarial_and_fallback_conditions():
    c = _controls()
    assert c["bull"] == {"model": "openai-light", "max_output_tokens": MAX_TOKENS, "reasoning_effort": "minimal",
                         "temperature": 0.3, "seed": VERDICT_SEED, "timeout_seconds": 12.0}
    assert c["bear"]["model"] == "gemini-light" and c["bear"]["temperature"] == 0.0 and c["bear"]["seed"] is None
    assert [f["model"] for f in c["fallback_chain"]] == ["openai-heavy", "openai-light"]
    assert all(f["max_output_tokens"] == 100 and f["timeout_seconds"] == 10.0 for f in c["fallback_chain"])
    assert c["empty_retries"] == 1 and c["strict_on_disagreement"] is False and c["fallback_system"] == ""
    assert c["fallback_chain_timeout_seconds"] == 10.0     # 운영 SINGLE_FALLBACK_TIMEOUT 은 체인 전체 시한
    unknown = mci.operating_role_controls(object(), fallback_prompt_suffix="x", fallback_timeout=10.0)
    assert unknown["bull"]["model"] is None          # 원천에서 못 읽은 값은 추정하지 않는다


def test_mixed_context_or_tampered_body_or_foreign_nonce_is_rejected():
    env = _envelope()
    other = _envelope(decision_id="dec-2", common_context="<entry_facts>{\"other\":1}</entry_facts>")
    mixed = dict(env, common_context=other["common_context"])
    assert mci.verify_envelope(mixed) == "context_hash_mismatch"
    both = dict(env, common_context=other["common_context"], common_context_sha256=other["common_context_sha256"])
    assert mci.verify_envelope(both) == "envelope_hash_mismatch"
    foreign = _envelope(nonce=b"another-process-nonce")
    assert mci.verify_envelope(foreign) == "unattested"
    copied = dict(other, attestation=env["attestation"])       # 다른 봉투의 증명 복사도 무효
    assert mci.verify_envelope(copied) == "unattested"


def test_clock_reversal_is_rejected():
    env = _envelope(handed_off_at=(T0 - timedelta(milliseconds=1)).isoformat())
    assert mci.verify_envelope(env) == "clock_reversal"
    naive = _envelope(input_available_at="2026-10-13T10:00:00")
    assert mci.verify_envelope(naive) == "invalid_field:input_available_at"


def test_completed_monthly_record_cannot_become_live_pair(tmp_path):
    """RED(1): 판단이 끝난 뒤 JSONL 에서 읽어 만든 PairSpec 은 attestation 이 없어 live 가 될 수 없다."""
    from src.analytics.model_comparison_runner import PairSpec, RoleControls
    row = rec.decision_record(signal_id="sig-1", symbol="005930", strategy="m", score=90, market_regime="sideways",
                              common_context="ctx", stage="adversarial", attempts=[], approved=True, result={},
                              budget_context=None, collected_at=T0)
    loaded = json.loads(json.dumps(row))
    spec = PairSpec(decision_id=loaded["decision_id"], signal_id=loaded["signal_id"], symbol=loaded["symbol"],
                    common_context=loaded["inputs"]["common_context"], input_available_at=loaded["inputs"]["collected_at"],
                    current=RoleControls("gpt-5-mini", 400, "minimal", 0.3, 20260803),
                    candidate=RoleControls("gpt-5-mini", 400, "low", 0.3, 20260803),
                    bear=RoleControls("gemini-3.1-flash-lite", 400, None, 0.0),
                    neutral_sizing=None, pricing_basis="paired_live_shadow", fixed_at=T0.isoformat(),
                    strict_on_disagreement=False, fallback_chain_timeout_seconds=10.0)
    with pytest.raises(ValueError, match="live"):
        spec.validate()
    labelled = replace(spec, evaluation_anchor="input_available_at", live_provenance="in_process_attested",
                       enqueued_at=T0.isoformat())
    with pytest.raises(ValueError, match="live_provenance_unattested"):
        labelled.validate()                         # 라벨만 붙인 것은 증명이 아니다
    signed = replace(labelled, attestation=mci.attest_pair(
        decision_id=spec.decision_id, signal_id=spec.signal_id, symbol=spec.symbol,
        common_context=spec.common_context, input_available_at=spec.input_available_at,
        enqueued_at=T0.isoformat(), neutral_sizing=None))
    signed.validate()
    with pytest.raises(ValueError, match="live_provenance_unattested"):
        replace(signed, common_context="swapped").validate()


# ── cross_validator 훅 ───────────────────────────────────────────────────

def _resp(content, provider):
    return LLMResponse(content, model="m", provider=provider, input_tokens=1, output_tokens=1,
                       requested_model="m", usage_known=True)


class _LLM:
    def __init__(self, delay=0.0):
        self.delay, self.prompts = delay, []

    def _get_model(self, provider, weight):
        return {(LLMProvider.OPENAI, "light"): "gpt-5-mini", (LLMProvider.GEMINI, "light"): "gemini-3.1-flash-lite",
                (LLMProvider.OPENAI, "heavy"): "gpt-5.6-sol", (LLMProvider.GEMINI, "heavy"): "gemini-3.1-pro-preview"}[(provider, weight)]

    async def complete_with(self, prompt, *, provider, **kwargs):
        self.prompts.append(prompt)
        await asyncio.sleep(self.delay)
        return _resp("APPROVE" if provider == LLMProvider.OPENAI else "ACCEPT", provider)

    async def complete(self, prompt, **kwargs):
        return _resp("YES", LLMProvider.OPENAI)


def _cross(monkeypatch, llm):
    from src.data.providers import disclosure_feed
    cross = CrossStrategyValidator(trade_memory=SimpleNamespace(get_context_for_signal=lambda *a: ""),
                                   trade_wiki=SimpleNamespace(query=lambda *a: "", query_symbol=lambda *a: ""))
    cross._llm_manager = llm
    cross._adversarial = AdversarialValidator(llm)
    monkeypatch.setattr(cross, "_load_panel_outlook", lambda: None)
    cross._panel_risk_factors, cross._panel_regime = [], "bear"
    monkeypatch.setattr(disclosure_feed, "get_symbol_disclosure_context", lambda *a: "")
    return cross


@pytest.fixture
def recorder(tmp_path):
    r = DecisionRecorder(tmp_path / "decisions", enabled=True, max_file_bytes=1 << 20)
    set_decision_recorder(r)
    yield r
    r.close()
    set_decision_recorder(None)


def _rows(recorder):
    assert recorder.flush(5.0)
    return [json.loads(l) for f in sorted(recorder.directory.glob("*.jsonl")) for l in f.read_text().splitlines()]


def test_handoff_happens_before_provider_await_and_id_propagates(monkeypatch, recorder):
    llm = _LLM()
    cross = _cross(monkeypatch, llm)
    seen = []

    def handoff(envelope):
        seen.append((envelope, len(llm.prompts)))
        return "accepted"
    cross._comparison_handoff = handoff
    assert asyncio.run(cross.llm_second_check("005930", "momentum", 90, {}, "sideways", signal_id="sig-1")) is True
    (env, prompts_before), = seen
    assert prompts_before == 0                                    # 공급자 호출 전 전달
    assert mci.verify_envelope(env) is None
    row = _rows(recorder)[0]
    assert env["decision_id"] == row["decision_id"] == cross.last_llm_decision["decision_id"]
    assert env["common_context"] == row["inputs"]["common_context"] and env["input_available_at"] == row["inputs"]["collected_at"]
    assert cross.last_llm_decision["comparison_handoff"] == "accepted"
    assert env["role_controls"]["bull"]["model"] == "gpt-5-mini"
    from src.core.cross_validator import SINGLE_FALLBACK_TIMEOUT
    assert env["role_controls"]["fallback_chain_timeout_seconds"] == SINGLE_FALLBACK_TIMEOUT   # 운영 체인 시한 복사


def test_handoff_failure_never_changes_verdict_or_metadata(monkeypatch, recorder):
    cross = _cross(monkeypatch, _LLM())
    baseline = asyncio.run(cross.llm_second_check("005930", "momentum", 90, {}, "sideways", signal_id="s"))
    base_decision = dict(cross.last_llm_decision)

    def boom(envelope):
        raise RuntimeError("worker gone")
    cross._comparison_handoff = boom
    assert asyncio.run(cross.llm_second_check("005930", "momentum", 90, {}, "sideways", signal_id="s")) is baseline
    assert cross.last_llm_decision["comparison_handoff"] == "handoff_error"
    assert {k: v for k, v in cross.last_llm_decision.items() if k not in ("decision_id", "comparison_handoff")} == \
        {k: v for k, v in base_decision.items() if k != "decision_id"}
    rows = _rows(recorder)
    assert len(rows) == 2 and rows[0]["result"] == rows[1]["result"]


def test_default_off_has_no_handoff_key_and_unavailable_stage_makes_no_envelope(monkeypatch, recorder):
    cross = _cross(monkeypatch, _LLM())
    asyncio.run(cross.llm_second_check("005930", "momentum", 90, {}, "sideways", signal_id="s"))
    assert "comparison_handoff" not in cross.last_llm_decision
    seen = []
    cross._comparison_handoff = lambda e: seen.append(e) or "accepted"
    cross._daily_llm_count, cross._daily_llm_count_date = cross._daily_llm_max, datetime.now().date()
    sizing = {"signal": {"signal_id": "s", "symbol": "005930"}}
    assert asyncio.run(cross.llm_second_check("005930", "momentum", 90, {}, "sideways", signal_id="s",
                                              neutral_sizing=sizing)) is True
    assert seen == []                                             # 실제 호출 전 종료 → 봉투 없음
    row = _rows(recorder)[-1]
    assert row["stage"] == "unavailable" and row["decision_id"].startswith("dec-")


def test_recorder_disabled_still_assigns_one_id_and_sends_none_sizing(monkeypatch, tmp_path):
    set_decision_recorder(DecisionRecorder(tmp_path / "off", enabled=False))
    try:
        cross = _cross(monkeypatch, _LLM())
        seen = []
        cross._comparison_handoff = lambda e: seen.append(e) or "accepted"
        asyncio.run(cross.llm_second_check("005930", "momentum", 90, {}, "sideways", signal_id="s"))
        assert len(seen) == 1 and seen[0]["neutral_sizing"] is None
        assert cross.last_llm_decision["decision_id"] == seen[0]["decision_id"]
        assert cross.last_llm_decision["record_status"] == "disabled"
    finally:
        set_decision_recorder(None)


def test_concurrent_invocations_keep_distinct_ids_and_envelopes(monkeypatch, recorder):
    llm = _LLM(delay=0.01)
    cross = _cross(monkeypatch, llm)
    seen = []
    cross._comparison_handoff = lambda e: seen.append(e) or "accepted"

    async def one(symbol, signal):
        await cross.llm_second_check(symbol, "momentum", 90, {}, "sideways", signal_id=signal)
        return dict(cross.last_llm_decision)

    async def both():
        return await asyncio.gather(asyncio.create_task(one("005930", "a")), asyncio.create_task(one("000660", "b")))
    asyncio.run(both())
    rows = {r["signal_id"]: r for r in _rows(recorder)}
    envs = {e["signal_id"]: e for e in seen}
    assert set(rows) == set(envs) == {"a", "b"}
    assert envs["a"]["decision_id"] != envs["b"]["decision_id"]
    for key in ("a", "b"):
        assert rows[key]["decision_id"] == envs[key]["decision_id"]
        assert rows[key]["inputs"]["common_context"] == envs[key]["common_context"]
        assert rows[key]["inputs"]["collected_at"] == envs[key]["input_available_at"]
        assert mci.verify_envelope(envs[key]) is None
    assert '"005930"' in envs["a"]["common_context"] and '"000660"' in envs["b"]["common_context"]


def test_decision_record_accepts_preassigned_id_and_stays_compatible():
    a = rec.decision_record(signal_id="s", symbol="X", strategy="s", score=1, market_regime="r", common_context="c",
                            stage="adversarial", attempts=[], approved=True, result={}, budget_context=None,
                            decision_id="dec-fixed")
    b = rec.decision_record(signal_id="s", symbol="X", strategy="s", score=1, market_regime="r", common_context="c",
                            stage="adversarial", attempts=[], approved=True, result={}, budget_context=None)
    assert a["decision_id"] == "dec-fixed" and b["decision_id"].startswith("dec-") and b["decision_id"] != "dec-fixed"


def test_light_fallback_snapshot_records_effective_reasoning_control():
    from src.utils.llm import LLMConfig, LLMManager
    controls = mci.operating_role_controls(LLMManager(LLMConfig()),
        fallback_prompt_suffix="YES/NO", fallback_timeout=10.0)
    primary, fallback = controls["fallback_chain"]
    assert primary["reasoning_effort"] is None
    assert fallback["model"] == "gpt-5-mini"
    assert fallback["reasoning_effort"] == "minimal"
