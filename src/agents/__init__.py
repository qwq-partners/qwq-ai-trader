"""
종목 단위 에이전트 팀 (2026-08-02~).

`src/experts/`가 시장·섹터 레벨을 판단한다면, 이 패키지는 **개별 종목**을 판단한다.
TradingAgents(TauricResearch) 구조를 참고하되, 기존 자산을 최대한 재사용한다.

    Analyst 3인(팬아웃) → Bull/Bear 토론(2R) → Trader → Risk게이트 → PM

문서: docs/agents/trading-team.md
"""

from .types import (
    AnalystKind, AnalystReport, DebateResult, DebateTurn,
    PMDecision, Stance, TeamVerdict, TradeProposal,
)
from .analysts import AnalystTeam, FundamentalAnalyst, NewsAnalyst, TechnicalAnalyst
from .researchers import ResearchTeam
from .trader import TraderAgent
from .portfolio_manager import PortfolioManager
from .team import TradingTeam
from .allocator import AllocationPlan, AllocationResult, PortfolioAllocator, get_allocator
from .reproducibility import LLMCallRecord, LLMLedger, get_ledger, snapshot_reports

__all__ = [
    "AllocationPlan", "AllocationResult", "PortfolioAllocator", "get_allocator",
    "LLMCallRecord", "LLMLedger", "get_ledger", "snapshot_reports",
    "AnalystKind", "AnalystReport", "DebateResult", "DebateTurn",
    "PMDecision", "Stance", "TeamVerdict", "TradeProposal",
    "AnalystTeam", "FundamentalAnalyst", "NewsAnalyst", "TechnicalAnalyst",
    "ResearchTeam", "TraderAgent", "PortfolioManager", "TradingTeam",
]
