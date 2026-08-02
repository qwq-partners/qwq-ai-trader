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
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional


class Stance(str, Enum):
    """매매 방향 의견"""
    BUY = "buy"
    HOLD = "hold"
    SELL = "sell"


class AnalystKind(str, Enum):
    FUNDAMENTAL = "fundamental"
    TECHNICAL = "technical"
    NEWS = "news"


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
    data_as_of: datetime = field(default_factory=datetime.now)

    @property
    def ok(self) -> bool:
        return self.error is None

    @property
    def age_minutes(self) -> float:
        """
        근거 데이터의 나이(분).

        `data_as_of`가 tz-aware로 들어오면 naive `datetime.now()`와 빼는 순간
        TypeError가 나고, 이 프로퍼티는 종합 점수·프롬프트·저장 경로 전부에서 쓰이므로
        심의 자체가 통째로 실패한다. 외부에서 어떤 시각이 들어와도 죽지 않게 정규화한다.
        """
        try:
            as_of = self.data_as_of
            now = datetime.now(as_of.tzinfo) if as_of.tzinfo is not None else datetime.now()
            return max(0.0, (now - as_of).total_seconds() / 60.0)
        except (TypeError, AttributeError, OverflowError):
            # 시각을 신뢰할 수 없으면 "매우 오래된 것"으로 본다 —
            # 알 수 없는 데이터를 신선하다고 가정하는 쪽이 더 위험하다.
            return float("inf")

    def freshness_decayed_confidence(self, half_life_min: float = 60.0) -> float:
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
        age = self.age_minutes
        if not math.isfinite(age):
            return 0.0          # 시각 불명 → 가중치 제외
        decay = 0.5 ** (age / half_life_min)
        return round(conf * decay, 4)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["kind"] = self.kind.value
        d["data_as_of"] = self.data_as_of.isoformat(timespec="seconds")
        d["age_minutes"] = round(self.age_minutes, 1)
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
                 "stance": t.stance, "text": t.text[:200]}
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

    def to_dict(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol,
            "name": self.name,
            "decision": self.decision.to_dict() if self.decision else None,
            "reports": [r.to_dict() for r in self.reports],
            "debate": self.debate.to_dict() if self.debate else None,
            "proposal": self.proposal.to_dict() if self.proposal else None,
            "elapsed_sec": round(self.elapsed_sec, 2),
            "error": self.error,
        }
