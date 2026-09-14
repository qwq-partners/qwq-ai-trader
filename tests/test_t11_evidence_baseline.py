"""T11-A 근거 계약 — 기준선 특성화 테스트 (2026-09-15).

AnalystTeam.aggregate_score/evidence_quality 의 기존 산식을 리뷰 사례로 고정한다.
이 파일의 값은 T11-A 작업 이후에도 그대로 통과해야 한다 — 단 아래 2건(C4/C5)은
근거를 명시한 버그 수정이며, 수정 전에는 실패했다(TDD로 실패를 먼저 확인한 뒤 수정).
"""

from datetime import datetime, timedelta

import pytest

from src.agents.analysts import AnalystTeam, TechnicalAnalyst
from src.agents.types import AnalystKind, AnalystReport


def _report(kind: AnalystKind, score: int, confidence: float, age_minutes: float) -> AnalystReport:
    return AnalystReport(
        kind=kind, symbol="005930", score=score, confidence=confidence,
        data_as_of=datetime.now() - timedelta(minutes=age_minutes),
    )


def test_aggregate_score_review_case_plus28():
    """계획서(docs/superpowers/plans/2026-09-15-agent-team-evidence-entryplan.md) §1 A2 사례:
    펀더 +10/0.7/15분·기술 +25/0.7/4분·뉴스 +52/0.7/30분 → 감쇠 근거량≈1.75, 종합 +28.
    aggregate_score 산식은 T11-A 에서 변경하지 않았으므로 baseline 그대로 유지된다.
    """
    reports = [
        _report(AnalystKind.FUNDAMENTAL, 10, 0.7, 15),
        _report(AnalystKind.TECHNICAL, 25, 0.7, 4),
        _report(AnalystKind.NEWS, 52, 0.7, 30),
    ]
    total_w = sum(r.freshness_decayed_confidence() for r in reports)
    assert total_w == pytest.approx(1.75, abs=0.01)
    assert AnalystTeam.aggregate_score(reports) == 28


def test_technical_analyst_volume_surge_bug_fix():
    """C5 (버그 수정, 근거): 운영 경로(kr_scheduler._cached_indicators → 스크리너)가
    실제로 쓰는 생산자는 TechnicalIndicators.calculate_all이며, 거래량 지표 키로
    'vol_ratio'를 낸다 (별도 계약 테스트:
    test_technical_indicator_producer_key_set_covers_analyst_consumer_keys).
    수정 전 analysts.py의 TechnicalAnalyst는 존재하지 않는 'volume_ratio' 키를
    읽었기 때문에 vol_ratio 값과 무관하게 거래량 급증(+15) 신호가 절대 발동하지 않았다.
    이 테스트는 그 소비 키 자체를 vol_ratio 로 직접 검증한다 — 수정 전에는
    실패했다(findings에 '거래량 급증'이 없고 score==0). 모듈 함수
    compute_indicators(technical.py:144~168)는 rsi/atr_pct 이름이 달라 이
    계약 밖이며(advisory, 2026-09-15), 운영에서 이 dict가 팀에 들어오는 경로는
    현재 없다.
    """
    import asyncio

    ta = TechnicalAnalyst()
    report = asyncio.run(ta.analyze(
        "005930", indicators={"vol_ratio": 3.0}, indicators_as_of=datetime.now(),
    ))
    assert any("거래량 급증" in f for f in report.findings)
    assert report.score >= 15


def test_evidence_quality_excludes_zero_confidence_bug_fix():
    """C4 (버그 수정, 근거): confidence=0("정보 없음")인 보고서는 error가 없어 r.ok=True다.
    aggregate_score는 가중치 0이라 이런 보고서가 섞여도 점수엔 영향이 없지만,
    evidence_quality는 '유효 소스 수'를 직접 세는 게 목적이라 정보가 전혀 없는 보고서까지
    소스로 잡으면 근거량을 부풀린다. MIN_VALID_SOURCES=2인 상황에서 confidence=0 보고서
    1건 + 진짜 유효 보고서 1건을 넣으면:
      - 수정 전: len(valid)==2 → 통과(ok=True)로 오판 — 이 테스트는 수정 전 실패했다.
      - 수정 후: len(valid)==1 → 정직하게 근거 부족(ok=False)으로 판정.
    """
    zero_conf = _report(AnalystKind.FUNDAMENTAL, 0, 0.0, 1)
    real = _report(AnalystKind.TECHNICAL, 25, 0.7, 4)
    ok, reason, _total_w = AnalystTeam.evidence_quality([zero_conf, real])
    assert ok is False
    assert "유효 근거 1개" in reason
