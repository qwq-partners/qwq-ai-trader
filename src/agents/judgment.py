"""신규 판단 정책 — TeamAssessment 산출 (T11 계약 2.2, 2026-09-15).

기존 `TraderAgent.propose`의 산식(만장일치 +20·conviction 0.90, 분열 -10/0.35,
반대 -40/0.2)은 **비교 기준(기준선)** 으로 그대로 둔다 — 이 모듈은 그 산식을 쓰지
않고 독립된 순수 함수로 새로 계산하며, 돈 경로(TradeProposal/PMDecision/사이징)에는
전혀 관여하지 않는다(shadow).

네 가지 판단을 분리한다:
    merit(매수 매력)      — evidence 기반. Bear ACCEPT(위험 허용)는 섞지 않는다.
    risk_acceptable(위험 허용) — Bear 최종 판정만.
    data_sufficiency(자료 충분성) — 보고서 data_status 집계.
    entry_ready(진입 가능) — EntryPlan shadow 검증 결과(있을 때만).

합의 수준(consensus_level)은 "몇 명이 동의했나"이지 확률이 아니다.
success_probability는 외부 검증(캘리브레이션) 전까지 항상 None이다 — 자료가 없는데
성공확률을 만들어 내지 않는다.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from .trader import BUY_THRESHOLD
from .types import AnalystReport, DebateResult, TeamAssessment


def _expired_at(valid_until: datetime, now: Optional[datetime]) -> bool:
    """유효시각을 평가시각과 비교한다.

    과거 replay의 decision_time은 KST-aware일 수 있지만, 기존 직렬화 자료의
    valid_until은 KST naive일 수 있다. naive 값은 기존 로컬(KST) wall-clock
    계약으로 해석해 비교하고, 두 시각의 시간대 정보는 보존한다.
    """
    if now is None:
        return False
    if valid_until.tzinfo is None and now.tzinfo is not None:
        reference = now.replace(tzinfo=None)
    elif valid_until.tzinfo is not None and now.tzinfo is None:
        reference = now.replace(tzinfo=valid_until.tzinfo)
    else:
        reference = now
    return valid_until < reference


def _valid_reports(reports: List[AnalystReport], now: Optional[datetime] = None) -> List[AnalystReport]:
    """merit/data_sufficiency 판단에 공통으로 쓰는 '유효 보고서' 필터.

    ok·신뢰도(감쇠후)>0·TTL 미초과 — `_evidence_merit`의 배제 규칙과 반드시 같게
    유지한다. 여기서 걸러진 보고서(만료·confidence=0·error)는 data_sufficiency
    집계에서도 빠져야 한다 — 결측·만료가 "자료 충분"으로 둔갑하면 안 된다.
    """
    from .analysts import AnalystTeam  # 지연 임포트 — 읽기 전용 의존성

    out = []
    for r in reports:
        if not r.ok:
            continue
        if r.freshness_decayed_confidence(now=now) <= 0:
            continue
        if AnalystTeam.is_expired(r, now=now):
            continue
        out.append(r)
    return out


def _evidence_merit(reports: List[AnalystReport], now: Optional[datetime]) -> tuple:
    """evidence 기반 매수 매력 점수.

    기존 분석가 score를 그대로 재사용하되(새 임계값을 만들지 않는다),
    "검증 통과"만 있고 긍정 근거가 없는 보고서는 그 +10 가산분만 취소한다
    (계약 2.2 — risk_clear는 매력에 미가산, positive_basis만 가산).

    dedup은 evidence 항목이 아니라 reports 순서를 기준으로 한다 — 같은 dedup_key가
    여러 보고서에 걸쳐 있으면 reports 리스트에서 먼저 나온 보고서가 그 근거를 갖는다
    (결정론적이지만 호출측 순서에 의존한다).

    Returns:
        (merit_score: Optional[int], unique_sources, weight, dedup_removed, expired, any_evidence)
    """
    from .analysts import AnalystTeam  # 지연 임포트 — 읽기 전용 의존성

    seen_dedup: set = set()
    weighted = 0.0
    total_w = 0.0
    unique_sources = 0
    dedup_removed = 0
    expired_n = 0
    any_evidence = False

    for r in reports:
        if r.ok and r.evidence:
            any_evidence = True

        w = r.freshness_decayed_confidence(now=now)
        if w <= 0:
            continue                          # confidence=0·error·시각불명 → freshness_decayed_confidence가 이미 0
        if AnalystTeam.is_expired(r, now=now):
            expired_n += 1
            continue

        usable_facts = []
        for ev in r.evidence:
            if ev.kind != "fact" or not ev.usable:
                continue
            if ev.valid_until is not None and _expired_at(ev.valid_until, now):
                continue                      # 근거 자체의 유효기간 만료
            key = ev.dedup_key
            if key is not None:
                if key in seen_dedup:
                    dedup_removed += 1
                    continue
                seen_dedup.add(key)
            usable_facts.append(ev)
        if not usable_facts:
            continue

        unique_sources += 1
        score = r.score
        if r.validation_pass_bonus is not None:
            score -= r.validation_pass_bonus
        elif r.risk_clear is True:
            # 계약 §2.2 문언대로 "검증 통과·위험 미발견" 은 매수 매력에 가산하지 않는다 —
            # 긍정 근거(positive_basis)가 함께 있어도 그 근거는 보고서 score 의 다른 항목
            # (동반 순매수 등)으로 이미 반영돼 있으므로 '통과 +10' 자체는 항상 취소한다
            # (통합 담당 확정, FINAL-1 blocking 2, 2026-09-15).
            score -= 10
        weighted += score * w
        total_w += w

    if total_w <= 0:
        return None, unique_sources, 0.0, dedup_removed, expired_n, any_evidence
    return (int(round(weighted / total_w)), unique_sources,
            round(total_w, 3), dedup_removed, expired_n, any_evidence)


def _merit_status(score: Optional[int], *, abstain_unknown: bool) -> tuple:
    """merit_score → merit_status. 기존 BUY_THRESHOLD(trader.py)만 재사용, 새 값 도입 없음."""
    if score is None:
        return "abstain", "근거 부족 — 유효 evidence 없음"
    if abstain_unknown:
        return "abstain", ("근거 계약 미통합(A 미배선) — 전 보고서 data_status=unknown, "
                            "점수만으로는 매력 판정 불가")
    if score >= BUY_THRESHOLD:
        return "sufficient", ""
    if score >= 0:
        return "weak", f"종합 {score:+d} — 매수 임계값({BUY_THRESHOLD}) 미달"
    return "insufficient", f"종합 {score:+d} — 부정적"


def _data_sufficiency(reports: List[AnalystReport], now: Optional[datetime] = None) -> str:
    """자료 충분성 — 만료·confidence=0·error 보고서는 유효 소스로 세지 않는다(B1 수정).

    data_status 라벨만 보면 만료된 'full' 보고서가 자료 완비로 둔갑한다 —
    `_valid_reports`로 `_evidence_merit`과 같은 배제 규칙을 적용한 뒤 집계한다.
    """
    valid = _valid_reports(reports, now=now)
    full = sum(1 for r in valid if r.data_status == "full")
    usable = sum(1 for r in valid if r.data_status in ("full", "partial"))
    if full >= 2:
        return "full"
    if usable >= 1:
        return "partial"
    return "insufficient"


def _consensus_level(debate: Optional[DebateResult]) -> str:
    if debate is None or debate.failed:
        return "failed"
    if debate.bull_final is None and debate.bear_final is None:
        return "failed"           # 양측 무응답 — 단독(one_sided)이 아니라 합의 미성립
    if debate.bull_final is None or debate.bear_final is None:
        return "one_sided"
    return "unanimous" if debate.bull_final == debate.bear_final else "split"


def _risk_acceptable(debate: Optional[DebateResult]) -> Optional[bool]:
    """Bear 최종 판정만 본다 — Bull 지지·합의 수준과는 무관(계약 2.2 규칙)."""
    if debate is None or debate.bear_final is None:
        return None
    return bool(debate.bear_final)


def _r1_votes(debate: Optional[DebateResult]) -> Dict[str, Optional[bool]]:
    bull = bear = None
    for t in (debate.turns if debate else []):
        if t.round_no == 1:
            if t.side == "bull":
                bull = t.stance
            elif t.side == "bear":
                bear = t.stance
    return {"bull": bull, "bear": bear}


def _final_votes(debate: Optional[DebateResult]) -> Dict[str, Optional[bool]]:
    if debate is None:
        return {"bull": None, "bear": None}
    return {"bull": debate.bull_final, "bear": debate.bear_final}


def _change_reasons(debate: Optional[DebateResult]) -> List[Dict[str, Any]]:
    """R1→R2 입장 변경 이유 보존(계약 §2.2). 토론 turns의 change_reason을 그대로 옮긴다."""
    out: List[Dict[str, Any]] = []
    if debate is None:
        return out
    last: Dict[str, Optional[bool]] = {}
    for t in sorted(debate.turns, key=lambda x: (x.side, x.round_no)):
        prev = last.get(t.side)
        if t.change_reason is not None:
            out.append({
                "side": t.side, "round": t.round_no,
                "from": prev, "to": t.stance,
                "kind": t.change_reason.get("kind"),
                "text": t.change_reason.get("text", ""),
            })
        last[t.side] = t.stance
    return out


def _entry_ready(entry_check: Optional[Dict[str, Any]]) -> Optional[bool]:
    if not entry_check:
        return None
    return entry_check.get("status") == "allow"


def assess(
    symbol: str,
    reports: List[AnalystReport],
    debate: Optional[DebateResult],
    *,
    entry_check: Optional[Dict[str, Any]] = None,
    now: Optional[datetime] = None,
) -> TeamAssessment:
    """보고서·토론·(선택)EntryPlan 검증 결과로 TeamAssessment를 만드는 순수 함수.

    LLM은 해석·반증만 담당했고(토론 단계), 여기서는 수치 계산만 한다 — 결정론적.
    """
    reports = list(reports or [])
    valid_reports = _valid_reports(reports, now=now)   # ok·신뢰도>0·TTL 이내 — B1: fallback도 이 기준을 따른다

    merit_score, unique_sources, weight, dedup_removed, expired_n, any_evidence = \
        _evidence_merit(reports, now)

    # 레거시 aggregate_score 폴백은 **A(근거 계약)가 아직 배선되지 않은 상태**(유효 보고서 전부
    # data_status=unknown)에서만 허용한다. 보고서가 data_status 를 판정했는데 usable evidence 가
    # 하나도 없으면 그것은 "근거 없음" 이지 기준선 점수로 대체할 상황이 아니다 — evidence 0건으로
    # merit_status=sufficient 가 되는 우회로를 막는다 (R-B r3 advisory, 통합 담당 반영).
    all_unknown = (not reports) or all(
        r.data_status == "unknown" for r in reports if r.ok
    )
    used_fallback = merit_score is None and not any_evidence and all_unknown
    if used_fallback:
        # A(근거 계약)가 아직 evidence를 채우지 않은 상태 — 기존 score를 그대로 쓴다(기준선)
        from .analysts import AnalystTeam
        try:
            merit_score = AnalystTeam.aggregate_score(reports, now=now)
        except Exception:
            merit_score = None

    # B1: 보고서는 있는데 전부 만료·오류·신뢰도 0 이면(valid_reports 비어 있음),
    # fallback의 aggregate_score()가 돌려주는 0을 "중립(weak)"으로 포장하지 않는다 —
    # 정보가 전혀 없는 것과 "약한 매력(0점)"은 다르다.
    no_valid_reports = bool(reports) and not valid_reports
    if no_valid_reports:
        merit_score = None

    merit_status, merit_reason = _merit_status(
        merit_score, abstain_unknown=(used_fallback and all_unknown)
    )
    if no_valid_reports:
        merit_status = "abstain"
        merit_reason = "유효 근거 0 — 전 보고서 만료·오류·신뢰도 0"

    risk_acceptable = _risk_acceptable(debate)
    data_suff = _data_sufficiency(reports, now=now)
    consensus = _consensus_level(debate)
    entry_ready = _entry_ready(entry_check)

    abstained = False
    reasons: List[str] = []
    if merit_status == "abstain":
        abstained = True
        reasons.append(merit_reason or "근거 부족")
    if consensus == "failed":
        abstained = True
        reasons.append("토론 실패 — 판단 불능")

    stance_v2 = "abstain" if abstained else (
        "buy_candidate" if (
            merit_status == "sufficient"
            and risk_acceptable is True
            and data_suff in ("full", "partial")
            and entry_ready is True
        ) else "hold"
    )

    return TeamAssessment(
        symbol=symbol,
        merit_score=merit_score,
        merit_status=merit_status,
        risk_acceptable=risk_acceptable,
        data_sufficiency=data_suff,
        entry_ready=entry_ready,
        entry_check=dict(entry_check) if entry_check else None,
        consensus_level=consensus,
        evidence_quality={
            "unique_sources": unique_sources, "weight": weight,
            "dedup_removed": dedup_removed, "expired": expired_n,
        },
        success_probability=None,
        calibration_status="uncalibrated",
        abstained=abstained,
        abstain_reason=" | ".join(reasons),
        independent_votes=_r1_votes(debate),
        final_votes=_final_votes(debate),
        change_reasons=_change_reasons(debate),
        stance_v2=stance_v2,
        notes=[],
    )


def demo() -> None:
    """최소 자체 점검 — 프레임워크 없이 assert만."""
    from .types import AnalystKind

    # 1) 자료·토론 전부 없음 → abstain, success_probability None
    a = assess("005930", [], None)
    assert a.abstained and a.stance_v2 == "abstain" and a.success_probability is None

    # 2) evidence 없는 보고서(A 미통합) — data_status=unknown → abstain (기존 score는 보존)
    r = AnalystReport(kind=AnalystKind.NEWS, symbol="005930", score=50, confidence=0.8)
    a2 = assess("005930", [r], None)
    assert a2.merit_status == "abstain" and a2.merit_score == 50

    # 3) Bear REJECT→ACCEPT 로 바뀌어도 merit은 그대로, risk_acceptable만 변화
    d_reject = DebateResult(symbol="005930", bull_final=True, bear_final=False,
                            consensus=None, confidence=0.5)
    d_accept = DebateResult(symbol="005930", bull_final=True, bear_final=True,
                            consensus=True, confidence=1.0)
    a3 = assess("005930", [r], d_reject)
    a4 = assess("005930", [r], d_accept)
    assert a3.merit_score == a4.merit_score == a2.merit_score
    assert a3.risk_acceptable is False and a4.risk_acceptable is True
    assert a4.success_probability is None   # 만장일치도 확률을 만들지 않는다

    print("judgment.demo OK")


if __name__ == "__main__":
    demo()
