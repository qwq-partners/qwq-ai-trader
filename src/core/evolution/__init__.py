"""
QWQ AI Trader - 자가 진화 시스템

거래 복기 → LLM 분석 → 파라미터 자동 조정 → 효과 평가 → 롤백/유지
"""

from .trade_journal import TradeJournal, TradeRecord, get_trade_journal
from .trade_reviewer import TradeReviewer, ReviewResult, get_trade_reviewer
from .llm_strategist import LLMStrategist, StrategyAdvice, ParameterAdjustment, get_llm_strategist
from .strategy_evolver import StrategyEvolver, get_strategy_evolver
from .config_persistence import EvolvedConfigManager, get_evolved_config_manager

__all__ = [
    "TradeJournal", "TradeRecord", "get_trade_journal",
    "TradeReviewer", "ReviewResult", "get_trade_reviewer",
    "LLMStrategist", "StrategyAdvice", "ParameterAdjustment", "get_llm_strategist",
    "StrategyEvolver", "get_strategy_evolver",
    "EvolvedConfigManager", "get_evolved_config_manager",
]
