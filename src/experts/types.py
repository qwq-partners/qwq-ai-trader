"""전문가 시스템 데이터 타입"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Dict, List, Optional


class RegimeBias(str, Enum):
    BULL = "bull"
    NEUTRAL = "neutral"
    BEAR = "bear"


@dataclass
class ExpertOpinion:
    """전문가 의견 — 모든 전문가의 공통 출력 스키마"""

    expert: str                       # 에이전트 이름 (e.g. "macro-economist")
    score: int                        # -100 ~ +100 (음수=하방, 양수=상방)
    regime_bias: RegimeBias           # bull/neutral/bear
    confidence: float                 # 0.0 ~ 1.0
    key_findings: List[str] = field(default_factory=list)        # 핵심 발견 3~5개
    affected_sectors: List[str] = field(default_factory=list)    # 영향 섹터
    affected_symbols: List[str] = field(default_factory=list)    # 영향 종목
    issued_at: datetime = field(default_factory=datetime.now)
    valid_until: datetime = field(
        default_factory=lambda: datetime.now() + timedelta(hours=6)
    )
    raw_evidence: Dict[str, Any] = field(default_factory=dict)   # 원본 데이터
    error: Optional[str] = None

    @property
    def is_valid(self) -> bool:
        return self.error is None and datetime.now() < self.valid_until

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["regime_bias"] = self.regime_bias.value
        d["issued_at"] = self.issued_at.isoformat()
        d["valid_until"] = self.valid_until.isoformat()
        return d

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "ExpertOpinion":
        # P2-6: 잘못된 값에 대한 안전 파싱
        try:
            bias = RegimeBias(d.get("regime_bias", "neutral"))
        except ValueError:
            bias = RegimeBias.NEUTRAL
        try:
            issued = datetime.fromisoformat(d["issued_at"]) if "issued_at" in d else datetime.now()
        except (ValueError, TypeError):
            issued = datetime.now()
        try:
            valid_until = datetime.fromisoformat(d["valid_until"]) if "valid_until" in d else datetime.now()
        except (ValueError, TypeError):
            valid_until = datetime.now()
        return cls(
            expert=str(d.get("expert", "unknown")),
            score=int(d.get("score", 0)),
            regime_bias=bias,
            confidence=max(0.0, min(1.0, float(d.get("confidence", 0.5) or 0.0))),
            key_findings=list(d.get("key_findings", [])),
            affected_sectors=list(d.get("affected_sectors", [])),
            affected_symbols=list(d.get("affected_symbols", [])),
            issued_at=issued,
            valid_until=valid_until,
            raw_evidence=dict(d.get("raw_evidence", {})),
            error=d.get("error"),
        )

    @classmethod
    def error_opinion(cls, expert: str, error: str) -> "ExpertOpinion":
        """오류 발생 시 안전한 기본값"""
        return cls(
            expert=expert,
            score=0,
            regime_bias=RegimeBias.NEUTRAL,
            confidence=0.0,
            error=error,
        )


@dataclass
class ExpertConfig:
    """전문가 시스템 설정 (config/default.yml의 experts: 섹션과 매핑)"""

    enabled: bool = True
    fail_open: bool = True            # 오류 시 매매 차단 안 함
    daily_call_budget: int = 50       # 에이전트당 일일 LLM 호출 상한
    cache_ttl_hours: int = 6          # 의견 캐시 유효 시간
    # Shadow mode: 규칙 #11 BUY 차단을 비활성화하고 로그만 기록.
    # 1주일 운영 후 hit rate 확인하고 false로 전환.
    shadow_mode: bool = True

    # 개별 에이전트 on/off
    agents: Dict[str, bool] = field(default_factory=lambda: {
        "news_curator": True,
        "macro_economist": True,
        "kr_market_expert": True,
        "us_market_expert": True,
        "kr_economy_expert": True,
        "global_micro_expert": True,
        "earnings_expert": True,
        "weekend_signal_expert": True,   # 2026-06-07 추가
        "sector_council": True,          # 2026-08-07 추가 — 종목 단위 판단 전용
    })

    # 가중치 (품질 검증으로 자동 조정)
    weights: Dict[str, float] = field(default_factory=lambda: {
        "news_curator": 1.0,
        "macro_economist": 1.0,
        "kr_market_expert": 1.0,
        "us_market_expert": 1.0,
        "kr_economy_expert": 0.8,
        "global_micro_expert": 0.8,
        "earnings_expert": 1.0,
        "weekend_signal_expert": 1.2,    # 2026-06-07 추가, 갭 risk 캐치
    })

    @classmethod
    def from_yaml(cls, cfg: Dict[str, Any]) -> "ExpertConfig":
        """YAML config["experts"] 섹션에서 로드"""
        section = cfg.get("experts", {}) if isinstance(cfg, dict) else {}
        result = cls()
        if isinstance(section, dict):
            result.enabled = bool(section.get("enabled", result.enabled))
            result.fail_open = bool(section.get("fail_open", result.fail_open))
            result.daily_call_budget = int(section.get("daily_call_budget", result.daily_call_budget))
            result.cache_ttl_hours = int(section.get("cache_ttl_hours", result.cache_ttl_hours))
            result.shadow_mode = bool(section.get("shadow_mode", result.shadow_mode))
            if "agents" in section and isinstance(section["agents"], dict):
                result.agents.update({k: bool(v) for k, v in section["agents"].items()})
            if "weights" in section and isinstance(section["weights"], dict):
                result.weights.update({k: float(v) for k, v in section["weights"].items()})
        return result
