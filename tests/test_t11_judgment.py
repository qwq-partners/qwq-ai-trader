"""T11 담당 B — judgment.py(TeamAssessment) + researchers.py(변경사유) + team.py(shadow 부착) (2026-09-15).

인수 조건(계획서 §4) #5,#6,#10,#14 를 포함한다. LLM은 FakeLLM으로 대체하고,
디스크는 전부 tmp_path 로 리다이렉트한다(운영 캐시 무접촉, [테스트 격리] 위반 0건 유지).
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional

import pytest

from src.agents import judgment, reproducibility, team as team_mod, team_ledger
from src.agents.types import (
    AnalystKind, AnalystReport, DebateResult, DebateTurn, EvidenceItem,
    PMDecision, Stance, TeamVerdict,
)
from src.utils.llm import LLMProvider, LLMResponse


# ── 디스크 격리 — team.py/team_ledger.py/reproducibility.py 의 홈 캐시 경로를 전부 tmp로 ──
@pytest.fixture(autouse=True)
def _isolated_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(team_mod, "RESULT_DIR", tmp_path / "team_verdicts")
    monkeypatch.setattr(team_ledger, "LEDGER_DIR", tmp_path / "team_ledger")
    monkeypatch.setattr(reproducibility, "LEDGER_DIR", tmp_path / "llm_ledger")
    monkeypatch.setattr(reproducibility, "_ledger", None)   # 싱글톤 강제 재생성
    yield


def _report(kind: AnalystKind, score: int, confidence: float = 0.8,
           evidence=None, positive_basis=None, risk_clear=None,
           data_status: str = "unknown") -> AnalystReport:
    return AnalystReport(
        kind=kind, symbol="005930", score=score, confidence=confidence,
        data_as_of=datetime.now(), evidence=evidence or [],
        positive_basis=positive_basis, risk_clear=risk_clear, data_status=data_status,
    )


def _ev(metric: str, value=1, kind: str = "fact", status: str = "full",
       dedup_key: Optional[str] = None) -> EvidenceItem:
    return EvidenceItem(source="test", metric=metric, value=value, kind=kind,
                        status=status, dedup_key=dedup_key)


# ══════════════════════════════════════════════════════════════════════════
# judgment.assess — 순수 함수 단위 테스트
# ══════════════════════════════════════════════════════════════════════════

def test_no_reports_no_debate_is_abstained_and_uncalibrated():
    a = judgment.assess("005930", [], None)
    assert a.abstained is True
    assert a.stance_v2 == "abstain"
    assert a.success_probability is None and a.calibration_status == "uncalibrated"
    assert a.risk_acceptable is None and a.entry_ready is None


def test_evidence_absent_falls_back_to_existing_score_but_abstains_on_unknown_status():
    """A(근거 계약) 미통합 상태 — evidence 없고 data_status=unknown → 기존 score는 보존하되 abstain."""
    r = _report(AnalystKind.NEWS, 55, confidence=0.9, data_status="unknown")
    a = judgment.assess("005930", [r], None)
    assert a.merit_score == 55          # 기존 score 재사용(기준선) — 사라지지 않는다
    assert a.merit_status == "abstain"
    assert "미통합" in a.abstain_reason or "근거" in a.abstain_reason


def test_bear_accept_alone_does_not_raise_merit_only_risk_acceptable_changes():
    """인수 조건 #5: Bear ACCEPT(REJECT→ACCEPT)만으로는 merit/stance가 오르지 않는다."""
    ev = [_ev("foreign_net_buying", True, dedup_key="dk1")]
    r = _report(AnalystKind.FUNDAMENTAL, 40, confidence=0.9, evidence=ev, positive_basis=True)
    d_reject = DebateResult(symbol="005930", bull_final=True, bear_final=False, confidence=0.5)
    d_accept = DebateResult(symbol="005930", bull_final=True, bear_final=True,
                            consensus=True, confidence=1.0)

    a_reject = judgment.assess("005930", [r], d_reject)
    a_accept = judgment.assess("005930", [r], d_accept)

    assert a_reject.merit_score == a_accept.merit_score      # 같은 근거 → merit 불변
    assert a_reject.risk_acceptable is False
    assert a_accept.risk_acceptable is True                  # risk_acceptable만 바뀐다


def test_unanimous_consensus_does_not_create_success_probability():
    """인수 조건 #6/#14: 만장일치라도 success_probability는 절대 생기지 않는다."""
    ev = [_ev("m1", 1, dedup_key="a"), _ev("m2", 1, dedup_key="b")]
    reports = [
        _report(AnalystKind.FUNDAMENTAL, 50, evidence=ev, positive_basis=True, data_status="full"),
        _report(AnalystKind.TECHNICAL, 40, evidence=ev, positive_basis=True, data_status="full"),
    ]
    debate = DebateResult(symbol="005930", bull_final=True, bear_final=True,
                          consensus=True, confidence=1.0)
    a = judgment.assess("005930", reports, debate, entry_check={"status": "allow"})
    assert a.consensus_level == "unanimous"
    assert a.success_probability is None and a.calibration_status == "uncalibrated"
    # merit sufficient(>=20) + risk_acceptable True + data full + entry_ready True → buy_candidate
    assert a.merit_status == "sufficient"
    assert a.stance_v2 == "buy_candidate"


def test_risk_clear_only_does_not_add_positive_basis_bonus():
    """'검증 통과'(risk_clear)만 있고 positive_basis가 없으면 +10 가산분이 취소된다(계약 2.2)."""
    ev = [_ev("verify", True, dedup_key="v1")]
    with_clear = _report(AnalystKind.FUNDAMENTAL, 30, evidence=ev, risk_clear=True, positive_basis=False)
    without_clear = _report(AnalystKind.FUNDAMENTAL, 20, evidence=ev, risk_clear=None, positive_basis=None)
    a1 = judgment.assess("005930", [with_clear], None)
    a2 = judgment.assess("005930", [without_clear], None)
    assert a1.merit_score == a2.merit_score == 20   # 30 - 10(취소) == 20


def test_dedup_key_counted_once_across_reports():
    same_key = [_ev("x", 1, dedup_key="same")]
    r1 = _report(AnalystKind.FUNDAMENTAL, 50, evidence=same_key, positive_basis=True)
    r2 = _report(AnalystKind.TECHNICAL, 50, evidence=same_key, positive_basis=True)
    a = judgment.assess("005930", [r1, r2], None)
    assert a.evidence_quality["dedup_removed"] >= 1


def test_expired_zero_confidence_and_error_reports_excluded():
    from datetime import timedelta
    ev = [_ev("x", 1, dedup_key="k")]
    # technical의 HARD_TTL_MIN=45분 — 3시간 전이면 만료지만 감쇠 가중치는 아직 0보다 크다
    # (반감기 60분 → 0.5**3 ≈ 0.125), 그래야 "감쇠로 인한 배제"와 "TTL 만료 배제"가 구분된다.
    stale = _report(AnalystKind.TECHNICAL, 90, confidence=0.9, evidence=ev, positive_basis=True)
    stale.data_as_of = datetime.now() - timedelta(hours=3)
    zero_conf = _report(AnalystKind.NEWS, 90, confidence=0.0, evidence=ev, positive_basis=True)
    failed = AnalystReport.failed(AnalystKind.FUNDAMENTAL, "005930", "boom")
    a = judgment.assess("005930", [stale, zero_conf, failed], None)
    assert a.merit_score is None
    assert a.merit_status == "abstain"
    assert a.evidence_quality["expired"] >= 1


def test_data_sufficiency_buckets():
    full2 = [_report(AnalystKind.FUNDAMENTAL, 1, data_status="full"),
             _report(AnalystKind.TECHNICAL, 1, data_status="full")]
    assert judgment.assess("x", full2, None).data_sufficiency == "full"
    partial1 = [_report(AnalystKind.FUNDAMENTAL, 1, data_status="partial")]
    assert judgment.assess("x", partial1, None).data_sufficiency == "partial"
    assert judgment.assess("x", [], None).data_sufficiency == "insufficient"


def test_entry_ready_reflects_entry_check_status_only_when_present():
    assert judgment.assess("x", [], None, entry_check=None).entry_ready is None
    assert judgment.assess("x", [], None, entry_check={"status": "allow"}).entry_ready is True
    assert judgment.assess("x", [], None, entry_check={"status": "wait"}).entry_ready is False


def test_consensus_level_covers_all_four_states():
    assert judgment.assess("x", [], None).consensus_level == "failed"
    assert judgment.assess("x", [], DebateResult(symbol="x", failed=True)).consensus_level == "failed"
    one_sided = DebateResult(symbol="x", bull_final=True, bear_final=None, confidence=0.3)
    assert judgment.assess("x", [], one_sided).consensus_level == "one_sided"
    split = DebateResult(symbol="x", bull_final=True, bear_final=False, confidence=0.5)
    assert judgment.assess("x", [], split).consensus_level == "split"
    unanimous = DebateResult(symbol="x", bull_final=True, bear_final=True, confidence=1.0)
    assert judgment.assess("x", [], unanimous).consensus_level == "unanimous"


def test_change_reasons_and_votes_preserved_from_debate_turns():
    """인수 조건 #6: R1/R2 표와 변경 이유가 TeamAssessment로 보존된다."""
    debate = DebateResult(symbol="005930", bull_final=True, bear_final=True,
                          consensus=True, confidence=1.0)
    debate.turns = [
        DebateTurn(1, "bull", True, "APPROVE 근거A"),
        DebateTurn(1, "bear", False, "REJECT 근거B"),
        DebateTurn(2, "bull", True, "APPROVE 유지"),
        DebateTurn(2, "bear", True, "ACCEPT 변경",
                  change_reason={"kind": "new_evidence", "text": "새 공시 확인"}),
    ]
    a = judgment.assess("005930", [], debate)
    assert a.independent_votes == {"bull": True, "bear": False}
    assert a.final_votes == {"bull": True, "bear": True}
    assert len(a.change_reasons) == 1
    cr = a.change_reasons[0]
    assert cr["side"] == "bear" and cr["from"] is False and cr["to"] is True
    assert cr["kind"] == "new_evidence" and cr["text"] == "새 공시 확인"


# ══════════════════════════════════════════════════════════════════════════
# researchers.py — R2 변경사유 파싱 (FakeLLM)
# ══════════════════════════════════════════════════════════════════════════

class _FakeLLM:
    """provider별 라운드 순서대로 응답을 내는 스텁 — 실제 LLM/네트워크 미사용."""

    def __init__(self, script: Dict[LLMProvider, list]):
        self._script = {k: list(v) for k, v in script.items()}
        self._idx: Dict[LLMProvider, int] = {}
        self.calls = []

    async def complete_with(self, prompt, *, provider, weight, system, max_tokens,
                            reasoning_effort, retry_on_empty=0, seed=None, temperature=None):
        self.calls.append((provider, prompt))
        i = self._idx.get(provider, 0)
        items = self._script[provider]
        text = items[min(i, len(items) - 1)]
        self._idx[provider] = i + 1
        return LLMResponse(content=text, model="fake-model", provider=provider, success=True)


def test_research_team_round2_flip_gets_change_reason_and_unrecorded_fallback():
    from src.agents.researchers import ResearchTeam

    fake = _FakeLLM({
        LLMProvider.OPENAI: ["APPROVE 1차 근거", "APPROVE 유지(변경 없음)"],
        LLMProvider.GEMINI: [
            "REJECT 1차 리스크",
            "ACCEPT 완화됨\n변경사유: 새근거 — 실적 서프라이즈 확인",
        ],
    })
    team = ResearchTeam(llm_manager=fake, rounds=2)
    result = asyncio.run(team.debate("005930", "삼성전자", []))

    assert result.consensus is True and result.rounds_run == 2
    bear_r2 = next(t for t in result.turns if t.side == "bear" and t.round_no == 2)
    bull_r2 = next(t for t in result.turns if t.side == "bull" and t.round_no == 2)
    assert bear_r2.change_reason == {"kind": "new_evidence", "text": "실적 서프라이즈 확인"}
    assert bull_r2.change_reason is None   # 입장 유지 → 기록 없음


def test_research_team_flip_without_change_reason_line_is_unrecorded():
    from src.agents.researchers import ResearchTeam

    fake = _FakeLLM({
        LLMProvider.OPENAI: ["APPROVE", "REJECT (사유 줄 없이 입장만 바꿈)"],
        LLMProvider.GEMINI: ["REJECT", "REJECT"],
    })
    team = ResearchTeam(llm_manager=fake, rounds=2)
    result = asyncio.run(team.debate("005930", "삼성전자", []))
    bull_r2 = next(t for t in result.turns if t.side == "bull" and t.round_no == 2)
    assert bull_r2.change_reason == {"kind": "unrecorded"}


def test_research_team_ledger_records_prompt_version_and_real_reasoning_effort():
    from src.agents.researchers import DEBATE_SEED, DEBATE_TEMPERATURE, PROMPT_VERSION, REASONING_EFFORT, ResearchTeam

    fake = _FakeLLM({LLMProvider.OPENAI: ["APPROVE"], LLMProvider.GEMINI: ["ACCEPT"]})
    team = ResearchTeam(llm_manager=fake, rounds=1)
    asyncio.run(team.debate("005930", "삼성전자", []))
    rows = reproducibility.LLMLedger.load()
    assert rows, "원장에 최소 1건은 기록돼야 한다"
    row = rows[0]
    assert row["prompt_version"] == PROMPT_VERSION == "debate-v2-2026-09-15"
    assert row["params"]["reasoning_effort"] == REASONING_EFFORT == "minimal"   # "low" 하드코딩 버그 수정
    assert row["params"]["seed"] == DEBATE_SEED
    assert row["params"]["temperature"] == DEBATE_TEMPERATURE


# ══════════════════════════════════════════════════════════════════════════
# team.py — TEAM_ASSESSMENT_V2 플래그·shadow 격리 (인수 조건 #9, #10)
# ══════════════════════════════════════════════════════════════════════════

def _make_team() -> team_mod.TradingTeam:
    # llm_manager=None → research.debate는 즉시 실패 반환(네트워크 없음), analysts도
    # 전부 소스 미연결 상태라 디스크/네트워크를 건드리지 않는다.
    return team_mod.TradingTeam(llm_manager=None, allow_pm_override=True)


def _decision_fingerprint(v: TeamVerdict) -> tuple:
    d = v.decision.to_dict()
    return (d["approved"], d["stance"], d["size_multiplier"], d["overrode_gate"],
           v.proposal.to_dict() if v.proposal else None, v.error)


def test_flag_off_never_computes_assessment(monkeypatch):
    monkeypatch.setenv("TEAM_ASSESSMENT_V2", "0")
    team = _make_team()
    v = asyncio.run(team.deliberate_candidate("005930", "삼성전자"))
    assert v.assessment is None


def test_flag_on_computes_assessment_and_decision_matches_flag_off(monkeypatch):
    monkeypatch.setenv("TEAM_ASSESSMENT_V2", "0")
    team_off = _make_team()
    v_off = asyncio.run(team_off.deliberate_candidate("005930", "삼성전자"))

    monkeypatch.setenv("TEAM_ASSESSMENT_V2", "1")
    team_on = _make_team()
    v_on = asyncio.run(team_on.deliberate_candidate("005930", "삼성전자"))

    assert v_on.assessment is not None
    assert _decision_fingerprint(v_on) == _decision_fingerprint(v_off)


def test_judgment_exception_is_isolated_from_money_path(monkeypatch):
    """인수 조건 #10: judgment.assess가 터져도 decision/proposal은 flag-off와 동일."""
    monkeypatch.setenv("TEAM_ASSESSMENT_V2", "0")
    v_off = asyncio.run(_make_team().deliberate_candidate("005930", "삼성전자"))

    monkeypatch.setenv("TEAM_ASSESSMENT_V2", "1")
    team = _make_team()

    def _boom(*a, **kw):
        raise RuntimeError("judgment 고장 주입")
    monkeypatch.setattr(team_mod, "_assess_team", _boom)

    v = asyncio.run(team.deliberate_candidate("005930", "삼성전자"))
    assert v.assessment is None
    assert _decision_fingerprint(v) == _decision_fingerprint(v_off)


def test_entry_plan_check_exception_is_isolated_and_assessment_still_computed(monkeypatch):
    """check_entry_plan이 예외를 내도 judgment.assess는 계속되고(entry_check=None) decision은 불변."""
    monkeypatch.setenv("TEAM_ASSESSMENT_V2", "0")
    v_off = asyncio.run(_make_team().deliberate_candidate("005930", "삼성전자"))

    monkeypatch.setenv("TEAM_ASSESSMENT_V2", "1")
    team = _make_team()

    def _boom(*a, **kw):
        raise RuntimeError("check_entry_plan 고장 주입")
    monkeypatch.setattr("src.execution.entry_plan.check_entry_plan", _boom)

    v = asyncio.run(team.deliberate_candidate(
        "005930", "삼성전자", entry_plan={"plan_id": "p1", "setup": "sepa_pullback"},
        current_price=70000.0,
    ))
    assert _decision_fingerprint(v) == _decision_fingerprint(v_off)
    assert v.assessment is not None                 # judgment는 계속 실행됨
    assert v.assessment.entry_ready is None          # entry_check 없이 판단
    assert any("entry_check 실패" in n for n in v.assessment.notes)


def test_ledger_append_failure_does_not_raise_or_affect_decision(monkeypatch):
    """인수 조건 #10: 원장 저장 실패는 warning만 — 심의 자체는 정상 완료된다."""
    monkeypatch.setenv("TEAM_ASSESSMENT_V2", "1")
    team = _make_team()

    def _boom(*a, **kw):
        raise OSError("디스크 고장 주입")
    monkeypatch.setattr(team_ledger, "append_deliberation", _boom)

    v = asyncio.run(team.deliberate_candidate("005930", "삼성전자"))
    assert v.decision is not None
    assert v.error is None


def test_flag_off_never_writes_ledger_or_assigns_deliberation_id(monkeypatch):
    """리뷰 blocking #1: 플래그 off면 원장 기록도 deliberation_id 부여도 없어야 한다."""
    monkeypatch.setenv("TEAM_ASSESSMENT_V2", "0")
    team = _make_team()
    v = asyncio.run(team.deliberate_candidate("005930", "삼성전자", slot="10:30"))
    assert v.deliberation_id == ""

    rows = team_ledger.load_day(f"{datetime.now():%Y-%m-%d}", ledger_dir=team_ledger.LEDGER_DIR)
    assert rows == []


def test_now_is_injected_so_expired_evidence_is_excluded_via_team(monkeypatch):
    """리뷰 blocking #2: team.py가 now를 넘기지 않으면 만료된 evidence가 merit에 가산된다.

    team._attach_assessment가 now를 judgment.assess에 실제로 전달하는지를
    entry_plan 경로가 아니라 verdict.assessment 결과로 직접 검증한다.
    """
    from datetime import timedelta

    from src.agents.types import EvidenceItem

    monkeypatch.setenv("TEAM_ASSESSMENT_V2", "1")
    team = _make_team()

    expired_ev = EvidenceItem(
        source="test", metric="m1", value=1, kind="fact", status="full",
        dedup_key="k1", valid_until=datetime.now() - timedelta(hours=1),
    )
    report = AnalystReport(
        kind=AnalystKind.FUNDAMENTAL, symbol="005930", score=60, confidence=0.9,
        data_as_of=datetime.now(), evidence=[expired_ev], positive_basis=True,
        data_status="full",
    )

    async def _run():
        v = await team.deliberate_candidate("005930", "삼성전자")
        v.reports = [report]           # 실제 analysts.run() 결과를 대체(합성 근거 주입)
        v.assessment = None
        team._attach_assessment(v)     # now가 실제로 전달되는지 직접 검증
        return v

    v = asyncio.run(_run())
    assert v.assessment is not None
    # now가 전달되지 않으면(패치 전 버그) 만료된 근거도 유효로 잡혀 merit_score=60이 된다.
    # now가 실제로 전달돼야 usable_facts가 비어 total_w=0 → merit_score=None.
    assert v.assessment.merit_score is None
    assert v.assessment.evidence_quality["unique_sources"] == 0


def test_deliberation_id_is_stable_for_same_input_and_ledger_row_has_execution_state(monkeypatch):
    monkeypatch.setenv("TEAM_ASSESSMENT_V2", "1")
    team = _make_team()
    v = asyncio.run(team.deliberate_candidate("005930", "삼성전자", slot="10:30"))
    assert v.deliberation_id
    assert v.slot == "10:30"

    rows = team_ledger.load_day(f"{datetime.now():%Y-%m-%d}", ledger_dir=team_ledger.LEDGER_DIR)
    assert len(rows) == 1
    row = rows[0]
    assert row["deliberation_id"] == v.deliberation_id
    assert row["execution_state"] in team_ledger.EXECUTION_STATES
    assert row["prompt_version"] == "debate-v2-2026-09-15"
    assert "model_calls" in row and isinstance(row["model_calls"], list)
