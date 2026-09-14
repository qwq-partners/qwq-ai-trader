"""T11 담당 B 기준선 특성화 — TraderAgent.propose 산식·ResearchTeam 판정표 (2026-09-15).

여기서 고정하는 것은 **기존(main) 동작**이다. B의 신규 작업(judgment.py 등)은 이 파일이
그대로 통과하는 것을 전제로 한다 — trader.py/researchers.py의 기존 산식은 읽기 전용이며
바뀌지 않는다.

합성 DebateResult/AnalystReport만 쓴다 — LLM·네트워크·운영 캐시 접촉 없음.
"""

from __future__ import annotations

from datetime import datetime

from src.agents.analysts import AnalystTeam, MIN_TOTAL_WEIGHT, MIN_VALID_SOURCES
from src.agents.trader import BUY_THRESHOLD, SELL_THRESHOLD, TraderAgent
from src.agents.types import AnalystKind, AnalystReport, DebateResult, Stance


def _report(kind: AnalystKind, score: int, confidence: float) -> AnalystReport:
    """신선(age=0) 보고서 — freshness decay가 1.0이 되게 해 산식을 단순하게 만든다."""
    return AnalystReport(kind=kind, symbol="005930", score=score, confidence=confidence,
                         data_as_of=datetime.now())


# ── A2: AnalystTeam.aggregate_score — 신뢰도 가중평균 (기존 산식 재계산 일치) ──────

def test_aggregate_score_matches_confidence_weighted_mean_when_fresh():
    reports = [
        _report(AnalystKind.FUNDAMENTAL, 10, 0.5),
        _report(AnalystKind.TECHNICAL, 25, 0.7),
        _report(AnalystKind.NEWS, 52, 0.9),
    ]
    expected = round((10 * 0.5 + 25 * 0.7 + 52 * 0.9) / (0.5 + 0.7 + 0.9))
    assert AnalystTeam.aggregate_score(reports) == expected


def test_aggregate_score_excludes_zero_confidence_and_errors():
    reports = [
        _report(AnalystKind.FUNDAMENTAL, 999, 0.0),                 # 가중치 0 → 배제
        AnalystReport.failed(AnalystKind.TECHNICAL, "005930", "x"),  # 오류 → 배제
        _report(AnalystKind.NEWS, 40, 1.0),
    ]
    assert AnalystTeam.aggregate_score(reports) == 40


def test_evidence_quality_gate_thresholds_unchanged():
    # 유효 소스 1개뿐 (MIN_VALID_SOURCES=2 미달) → 차단
    ok, reason, w = AnalystTeam.evidence_quality([_report(AnalystKind.NEWS, 50, 0.9)])
    assert ok is False and "유효 근거" in reason
    # 유효 소스 2개, 가중치 합 충분 → 통과
    ok2, _, w2 = AnalystTeam.evidence_quality([
        _report(AnalystKind.FUNDAMENTAL, 10, 0.6), _report(AnalystKind.TECHNICAL, 10, 0.6),
    ])
    assert ok2 is True and w2 >= MIN_TOTAL_WEIGHT
    assert MIN_VALID_SOURCES == 2   # 상수 자체가 바뀌면 위 두 케이스의 의미가 달라진다


# ── A1: TraderAgent.propose — 토론 보정·conviction 산식 (기존 산식 고정) ─────────

_TWO_STRONG_REPORTS = [
    _report(AnalystKind.FUNDAMENTAL, 40, 0.8),
    _report(AnalystKind.TECHNICAL, 40, 0.8),
]


def test_propose_unanimous_support_formula():
    debate = DebateResult(symbol="005930", bull_final=True, bear_final=True,
                          consensus=True, confidence=1.0, rounds_run=2)
    p = TraderAgent().propose("005930", "삼성전자", _TWO_STRONG_REPORTS, debate)
    analyst_score = AnalystTeam.aggregate_score(_TWO_STRONG_REPORTS)
    assert p.conviction == round(0.5 + 0.4 * 1.0, 2) == 0.90
    total = max(-100, min(100, analyst_score + int(20 * 1.0)))
    assert p.stance == (Stance.BUY if total >= BUY_THRESHOLD else Stance.HOLD)


def test_propose_unanimous_reject_formula():
    debate = DebateResult(symbol="005930", bull_final=False, bear_final=False,
                          consensus=False, confidence=1.0, rounds_run=2)
    p = TraderAgent().propose("005930", "삼성전자", _TWO_STRONG_REPORTS, debate)
    assert p.conviction == 0.2
    analyst_score = AnalystTeam.aggregate_score(_TWO_STRONG_REPORTS)
    total = max(-100, min(100, analyst_score + int(-40 * 1.0)))
    if total <= SELL_THRESHOLD:
        assert p.stance != Stance.BUY


def test_propose_split_formula():
    debate = DebateResult(symbol="005930", bull_final=True, bear_final=False,
                          consensus=None, confidence=0.5, rounds_run=2)
    p = TraderAgent().propose("005930", "삼성전자", _TWO_STRONG_REPORTS, debate)
    assert p.conviction == 0.35
    assert "토론 보정 -10" in p.rationale


def test_propose_debate_failed_is_fail_closed_for_new_buy():
    """신규 매수는 fail-closed — 토론 실패(LLM 장애) 시 BUY로 갈 점수여도 HOLD로 강등."""
    strong = [_report(AnalystKind.FUNDAMENTAL, 90, 0.9), _report(AnalystKind.TECHNICAL, 90, 0.9)]
    debate = DebateResult(symbol="005930", failed=True, summary="양측 판정 불가")
    p = TraderAgent().propose("005930", "삼성전자", strong, debate, holding=False)
    assert p.conviction == 0.3
    assert p.stance == Stance.HOLD
    assert "토론 검증 실패" in p.rationale


def test_propose_debate_failed_is_fail_open_for_holding():
    """보유 종목 재평가는 fail-open — 토론 실패해도 HOLD/SELL 판단은 계속한다."""
    weak = [_report(AnalystKind.FUNDAMENTAL, -80, 0.9), _report(AnalystKind.TECHNICAL, -80, 0.9)]
    debate = DebateResult(symbol="005930", failed=True, summary="양측 판정 불가")
    p = TraderAgent().propose("005930", "삼성전자", weak, debate, holding=True)
    assert p.conviction == 0.3
    assert p.stance == Stance.SELL   # 토론 실패에도 불구하고 보유 판단은 그대로 진행된다
