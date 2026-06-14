"""
QWQ AI Trader — 전문가 에이전트 시스템

7명의 도메인 전문가가 시장·뉴스·거시·실적을 분석하여 엔진 의사결정에 기여한다.

구성:
- types.py        : ExpertOpinion, RegimeBias, ExpertConfig
- base.py         : ExpertAgent 추상 베이스 (Perplexity 헬퍼 포함)
- orchestrator.py : 7명 호출 조율 + 예산/캐시
- opinion_store.py: 의견 파일 영속화 (jsonl)
- wiki.py         : 도메인별 마크다운 누적 + lint
- 7개 *_expert.py / news_curator.py : 도메인 전문가 구현
"""

from .types import ExpertOpinion, RegimeBias, ExpertConfig
from .base import ExpertAgent
from .orchestrator import ExpertOrchestrator

__all__ = [
    "ExpertOpinion",
    "RegimeBias",
    "ExpertConfig",
    "ExpertAgent",
    "ExpertOrchestrator",
]
