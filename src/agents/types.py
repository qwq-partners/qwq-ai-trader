"""
종목 단위 에이전트 팀 — 공통 데이터 타입.

기존 `src/experts/`는 시장·섹터 레벨 판단(ExpertOpinion)을 만든다.
여기 정의하는 타입은 그보다 한 단계 아래, **개별 종목**에 대한 판단을 다룬다.

파이프라인:
    AnalystReport ×3 → DebateResult → TradeProposal → PMDecision
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo


KST = ZoneInfo("Asia/Seoul")


def as_kst_aware(value: datetime) -> datetime:
    """naive 시각은 기존 로컬 계약대로 KST를 부여하고 aware offset은 보존한다."""
    return value.replace(tzinfo=KST) if value.tzinfo is None else value


class Stance(str, Enum):
    """매매 방향 의견"""
    BUY = "buy"
    HOLD = "hold"
    SELL = "sell"


class AnalystKind(str, Enum):
    FUNDAMENTAL = "fundamental"
    TECHNICAL = "technical"
    NEWS = "news"


# 근거 상태·종류 어휘 (T11 근거 계약, 2026-09-15)
EVIDENCE_STATUS = ("full", "partial", "insufficient", "error")
EVIDENCE_KIND = ("fact", "interpretation", "assumption")


@dataclass
class EvidenceItem:
    """근거 1건 — 출처·원자료 식별자·관측/수집 시각·상태·종류·유효기간을 분리해 담는다.

    원칙(T11 계약 2.1): observed_at 은 **실제 관측/공표 시각을 모르면 None** 이다 —
    수집 시각이나 now 로 채워 신선도를 세탁하지 않는다. 결측은 status 로 표시하고
    value 를 0·중립·긍정으로 바꾸지 않는다. "위험 미발견"(risk_clear)과 "긍정 근거
    확인"(positive_basis)은 보고서 수준에서 분리한다.
    """

    source: str                                  # 예: "stock_validator.supply_demand", "dart", "technical.indicators", "news_curator"
    metric: str                                  # 예: "foreign_net_buying", "rsi_14", "sentiment"
    value: Any = None
    unit: Optional[str] = None                   # 예: "%", "bool", "score(-100..100)"
    observed_at: Optional[datetime] = None       # 실제 관측/공표 시각 — 모르면 None
    collected_at: Optional[datetime] = None      # 수집 시각
    period: Optional[str] = None                 # 재무자료의 회계기간 (예: "2026Q2") — 없으면 None
    status: str = "full"                         # EVIDENCE_STATUS
    kind: str = "fact"                           # EVIDENCE_KIND
    ref_id: Optional[str] = None                 # 원자료 식별자 (기사 URL·공시 접수번호·캐시 키)
    valid_until: Optional[datetime] = None
    expiry_reason: Optional[str] = None
    dedup_key: Optional[str] = None              # 같은 기사·공시 재인용 식별용 (같으면 1회만 센다)
    note: str = ""

    @property
    def usable(self) -> bool:
        """판단 근거로 셀 수 있는가 — full/partial 이고 값이 있어야 한다"""
        return self.status in ("full", "partial") and self.value is not None

    @classmethod
    def from_datapoint(cls, dp: Any, *, metric: str, kind: str = "fact",
                       collected_at: Optional[datetime] = None, **kw) -> "EvidenceItem":
        """T9 `src.utils.data_freshness.DataPoint` 를 감싼다 (결측 사유·as_of·ttl 보존)"""
        value = getattr(dp, "value", None)
        as_of = getattr(dp, "as_of", None)
        ttl = getattr(dp, "ttl_seconds", None)
        missing_reason = getattr(dp, "missing_reason", None)
        status = "insufficient" if value is None else ("full" if as_of is not None else "partial")
        valid_until = (as_of + timedelta(seconds=ttl)) if (as_of is not None and ttl is not None) else None
        return cls(
            source=str(getattr(dp, "source", "") or ""), metric=metric, value=value,
            observed_at=as_of, collected_at=collected_at, status=status, kind=kind,
            valid_until=valid_until, expiry_reason=None,
            note=(missing_reason or ("관측 시각 미상" if as_of is None and value is not None else "")),
            **kw,
        )

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        for k in ("observed_at", "collected_at", "valid_until"):
            v = getattr(self, k)
            d[k] = v.isoformat(timespec="seconds") if isinstance(v, datetime) else None
        d["usable"] = self.usable
        return d


@dataclass
class AnalystReport:
    """개별 분석가 보고서 — 종목 하나에 대한 한 관점"""

    kind: AnalystKind
    symbol: str
    score: int                       # -100 ~ +100 (음수=부정, 양수=긍정)
    summary: str = ""                # 한 줄 요약 (토론 컨텍스트로 들어감)
    findings: List[str] = field(default_factory=list)
    metrics: Dict[str, Any] = field(default_factory=dict)   # 원자료 (지표값 등)
    confidence: float = 0.5          # 0.0 ~ 1.0
    error: Optional[str] = None
    # 이 보고서가 근거로 삼은 **데이터의 시각** (보고서 생성 시각이 아니다).
    # 캐시된 값을 썼다면 그 값이 만들어진 시점을 넣는다.
    # 장중 판단에 몇 시간 전 데이터를 쓰면서 그 사실을 모르는 것이 가장 위험하다.
    data_as_of: Optional[datetime] = field(default_factory=datetime.now)
    # ── T11 근거 계약 (2026-09-15) — 기본값으로 하위 호환. 기존 score/confidence/data_as_of
    #    의미는 기준선으로 불변이며, 신규 shadow 판단(TeamAssessment)은 아래 필드만 읽는다.
    data_status: str = "unknown"                 # full | partial | insufficient | error | unknown(미판정)
    evidence: List[EvidenceItem] = field(default_factory=list)
    positive_basis: Optional[bool] = None        # 긍정적 투자 근거가 실제로 확인됐는가
    risk_clear: Optional[bool] = None            # 검증을 실제로 수행했고 위험을 발견하지 못했는가 (≠ 긍정 근거)
    # 기존 score에 포함된 "검증 통과" 가산분. risk_clear와 독립적으로 보존해
    # shadow merit이 DART 경고 뒤에도 그 가산분만 정확히 취소할 수 있게 한다.
    # None은 T11 이전/외부 생산자이며 judgment가 legacy risk_clear 규칙으로 폴백한다.
    validation_pass_bonus: Optional[int] = None
    observed_at: Optional[datetime] = None       # 정직한 관측 시각 — 모르면 None (data_as_of 추정치와 구분)
    limitations: List[str] = field(default_factory=list)   # 예: "헤드라인 기반", "캐시 시각 미제공"

    @property
    def ok(self) -> bool:
        return self.error is None

    @property
    def age_minutes(self) -> float:
        """현재 시각 기준 근거 데이터 나이(분) — 기존 호출 하위 호환 프로퍼티."""
        return self.age_minutes_at()

    def age_minutes_at(self, now: Optional[datetime] = None) -> float:
        """
        `now` 기준 근거 데이터의 나이(분).

        `data_as_of`가 tz-aware로 들어오면 naive `datetime.now()`와 빼는 순간
        TypeError가 나고, 이 프로퍼티는 종합 점수·프롬프트·저장 경로 전부에서 쓰이므로
        심의 자체가 통째로 실패한다. 외부에서 어떤 시각이 들어와도 죽지 않게 정규화한다.
        """
        try:
            as_of = as_kst_aware(self.data_as_of)
            reference = as_kst_aware(now) if now is not None else datetime.now(KST)
            return max(0.0, (reference - as_of).total_seconds() / 60.0)
        except (TypeError, AttributeError, OverflowError):
            # 시각을 신뢰할 수 없으면 "매우 오래된 것"으로 본다 —
            # 알 수 없는 데이터를 신선하다고 가정하는 쪽이 더 위험하다.
            return float("inf")

    def freshness_decayed_confidence(self, half_life_min: float = 60.0,
                                     now: Optional[datetime] = None) -> float:
        """
        나이에 따라 감쇠시킨 신뢰도.

        half_life_min 마다 절반으로 줄인다 (지수 감쇠).
        6시간 전 수급 데이터와 방금 계산한 지표를 같은 무게로 합치면
        종합 점수가 과거를 반영하게 된다.
        """
        # NaN은 모든 비교가 거짓이라 `<= 0` 검사를 통과해버린다 → 명시적으로 배제
        conf = self.confidence
        if not self.ok or not math.isfinite(conf) or conf <= 0:
            return 0.0
        conf = min(1.0, conf)
        if half_life_min <= 0:
            return conf
        age = self.age_minutes_at(now)
        if not math.isfinite(age):
            return 0.0          # 시각 불명 → 가중치 제외
        decay = 0.5 ** (age / half_life_min)
        return round(conf * decay, 4)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["kind"] = self.kind.value
        d["data_as_of"] = (self.data_as_of.isoformat(timespec="seconds")
                            if isinstance(self.data_as_of, datetime) else None)
        d["age_minutes"] = round(self.age_minutes, 1)
        d["evidence"] = [e.to_dict() for e in self.evidence]
        d["observed_at"] = (self.observed_at.isoformat(timespec="seconds")
                            if isinstance(self.observed_at, datetime) else None)
        return d

    @classmethod
    def failed(cls, kind: AnalystKind, symbol: str, error: str) -> "AnalystReport":
        """수집 실패 시 중립 보고서 — 파이프라인을 멈추지 않는다"""
        return cls(kind=kind, symbol=symbol, score=0, confidence=0.0, error=error)


@dataclass
class DebateTurn:
    """토론 한 발언"""
    round_no: int
    side: str                        # "bull" | "bear"
    stance: Optional[bool]           # True=지지, False=반대, None=판정 불가
    text: str = ""
    # 실제로 응답한 모델 — 폴백으로 바뀔 수 있어 요청 모델과 다를 수 있다.
    # 모델 교체 전후 성과를 비교하려면 판단마다 이게 남아야 한다.
    model: str = ""
    provider: str = ""
    # T11 (2026-09-15): R2 에서 입장이 바뀌었을 때만 채운다 —
    # {"kind": "new_evidence"|"prior_error"|"unrecorded", "text": str}
    change_reason: Optional[Dict[str, Any]] = None


@dataclass
class DebateResult:
    """Bull/Bear 토론 결과"""

    symbol: str
    turns: List[DebateTurn] = field(default_factory=list)
    bull_final: Optional[bool] = None
    bear_final: Optional[bool] = None
    consensus: Optional[bool] = None   # 만장일치면 True/False, 갈리면 None
    confidence: float = 0.0            # 1.0=만장일치, 0.5=불일치, 0.0=실패
    rounds_run: int = 0
    failed: bool = False
    summary: str = ""

    @property
    def disagreed(self) -> bool:
        return (self.bull_final is not None
                and self.bear_final is not None
                and self.bull_final != self.bear_final)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol,
            "bull_final": self.bull_final,
            "bear_final": self.bear_final,
            "consensus": self.consensus,
            "confidence": self.confidence,
            "rounds_run": self.rounds_run,
            "disagreed": self.disagreed,
            "failed": self.failed,
            "summary": self.summary,
            "turns": [
                {"round": t.round_no, "side": t.side,
                 "stance": t.stance, "text": t.text[:500],
                 "model": t.model, "provider": t.provider,
                 "change_reason": t.change_reason}
                for t in self.turns
            ],
        }


@dataclass
class TradeProposal:
    """Trader 에이전트의 매매 제안"""

    symbol: str
    stance: Stance
    conviction: float = 0.5          # 0.0 ~ 1.0 — 사이징 배수의 근거
    size_multiplier: float = 1.0     # 기본 포지션 대비 배수 (0.5 ~ 1.5)
    rationale: str = ""
    analyst_scores: Dict[str, int] = field(default_factory=dict)
    debate: Optional[Dict[str, Any]] = None
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["stance"] = self.stance.value
        return d


@dataclass
class PMDecision:
    """Portfolio Manager 최종 결정"""

    symbol: str
    approved: bool
    stance: Stance
    size_multiplier: float = 1.0
    reason: str = ""
    # 리스크 게이트를 넘어선 승인인지 (감사·알림 대상)
    overrode_gate: bool = False
    overridden_gates: List[str] = field(default_factory=list)
    proposal: Optional[Dict[str, Any]] = None
    decided_at: datetime = field(default_factory=datetime.now)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["stance"] = self.stance.value
        d["decided_at"] = self.decided_at.isoformat()
        return d


ASSESSMENT_POLICY_VERSION = "v2-shadow-2026-09-15"


@dataclass
class TeamAssessment:
    """신규 shadow 판단 (T11 계약 2.2) — 기존 TradeProposal/conviction 과 병행하며 돈 경로에 쓰이지 않는다.

    네 가지 판단을 별도 필드로 표현한다: 매수 매력(merit), 위험 허용(risk_acceptable),
    자료 충분성(data_sufficiency), 진입 조건 충족(entry_ready). 합의 수준(consensus_level)·
    근거 품질(evidence_quality)·성공확률(success_probability, 외부 검증 전 None+uncalibrated)
    은 서로 다른 필드다. 판단 불능은 abstained 로 남긴다.
    """

    symbol: str
    policy_version: str = ASSESSMENT_POLICY_VERSION
    merit_score: Optional[int] = None            # -100~100, 근거 부족이면 None
    merit_status: str = "abstain"                # sufficient | weak | insufficient | abstain
    risk_acceptable: Optional[bool] = None       # Bear 최종: ACCEPT=True / REJECT=False / None=기권·실패
    data_sufficiency: str = "insufficient"       # full | partial | insufficient
    entry_ready: Optional[bool] = None           # EntryPlan shadow 검증 allow=True, wait/reject=False, None=계획 없음
    entry_check: Optional[Dict[str, Any]] = None # PlanCheck.to_dict()
    consensus_level: str = "failed"              # unanimous | split | one_sided | failed
    evidence_quality: Dict[str, Any] = field(default_factory=dict)  # unique_sources, weight, dedup_removed, expired
    success_probability: Optional[float] = None
    calibration_status: str = "uncalibrated"     # 예측 사건·기간·외부 검증이 없으면 항상 uncalibrated
    abstained: bool = False
    abstain_reason: str = ""
    independent_votes: Dict[str, Optional[bool]] = field(default_factory=dict)  # R1 {"bull":..,"bear":..}
    final_votes: Dict[str, Optional[bool]] = field(default_factory=dict)        # 최종
    change_reasons: List[Dict[str, Any]] = field(default_factory=list)          # [{"side","from","to","kind","text"}]
    stance_v2: str = "abstain"                   # buy_candidate | hold | abstain
    notes: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class TeamVerdict:
    """팀 심의 전체 결과 — 한 종목에 대한 최종 산출물"""

    symbol: str
    name: str = ""
    decision: Optional[PMDecision] = None
    reports: List[AnalystReport] = field(default_factory=list)
    debate: Optional[DebateResult] = None
    proposal: Optional[TradeProposal] = None
    elapsed_sec: float = 0.0
    error: Optional[str] = None
    wiki_context_used: bool = False  # 심의 컨텍스트에 종목 위키 노트 포함 여부 (2026-09-13 WikiSkill 계측)
    # T11 (2026-09-15): shadow 판단·원장 식별자 — 기존 소비자는 무시해도 된다
    assessment: Optional[TeamAssessment] = None
    deliberation_id: str = ""
    slot: str = ""
    entry_plan_id: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol,
            "name": self.name,
            "wiki_context_used": self.wiki_context_used,
            "assessment": self.assessment.to_dict() if self.assessment else None,
            "deliberation_id": self.deliberation_id,
            "slot": self.slot,
            "entry_plan_id": self.entry_plan_id,
            "decision": self.decision.to_dict() if self.decision else None,
            "reports": [r.to_dict() for r in self.reports],
            "debate": self.debate.to_dict() if self.debate else None,
            "proposal": self.proposal.to_dict() if self.proposal else None,
            "elapsed_sec": round(self.elapsed_sec, 2),
            "error": self.error,
        }
