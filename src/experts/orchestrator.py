"""ExpertOrchestrator — 7명 전문가 호출 조율"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime
from typing import Any, Dict, List, Optional

from loguru import logger

from .base import ExpertAgent
from .opinion_store import save_opinion
from .types import ExpertConfig, ExpertOpinion, RegimeBias
from . import wiki as wiki_mod


class ExpertOrchestrator:
    """7명 전문가의 라이프사이클·호출·집계를 관리"""

    def __init__(
        self,
        config: ExpertConfig,
        llm_manager: Any = None,
    ):
        self.config = config
        self.llm_manager = llm_manager
        self.perplexity_key = os.getenv("PERPLEXITY_API_KEY", "")
        self.agents: Dict[str, ExpertAgent] = {}
        self._last_run: Dict[str, datetime] = {}
        # 종목 위키 리서치 노트용 TradeWiki (run_trader에서 엔진 인스턴스 배선, 2026-08-07)
        self.trade_wiki = None

    def register(self, agent: ExpertAgent) -> None:
        self.agents[agent.name] = agent
        logger.info(f"[Orchestrator] 등록: {agent.name}")

    def register_all(self) -> None:
        """기본 9명 전문가를 모두 등록

        (2026-06-07 weekend_signal_expert, 2026-08-07 sector_council 추가)
        sector_council은 종목 단위 판단 전용 — 시장 체제 집계 3종에서 제외된다.
        """
        from .news_curator import NewsCurator
        from .macro_economist import MacroEconomist
        from .kr_market_expert import KRMarketExpert
        from .us_market_expert import USMarketExpert
        from .kr_economy_expert import KREconomyExpert
        from .global_micro_expert import GlobalMicroExpert
        from .earnings_expert import EarningsExpert
        from .weekend_signal_expert import WeekendSignalExpert
        from .sector_council import SectorCouncilExpert

        for cls in (
            NewsCurator,
            MacroEconomist,
            KRMarketExpert,
            USMarketExpert,
            KREconomyExpert,
            GlobalMicroExpert,
            EarningsExpert,
            WeekendSignalExpert,
            SectorCouncilExpert,
        ):
            agent = cls(
                config=self.config,
                llm_manager=self.llm_manager,
                perplexity_key=self.perplexity_key,
            )
            self.register(agent)

    @staticmethod
    def _log_task_exception(task) -> None:
        """백그라운드 태스크 예외 회수 (2026-08-08 P2)"""
        if not task.cancelled() and task.exception() is not None:
            logger.warning(f"[Orchestrator] 백그라운드 태스크 예외: {task.exception()}")

    async def _note_symbols(self, op: ExpertOpinion) -> None:
        """전문가가 지목한 종목을 종목 위키 리서치 노트에 기록 (fire-and-forget)

        전문가당 최대 5종목 — note_research가 일일 중복·페이지 상한을 알아서 거른다.
        """
        try:
            text = (op.key_findings[0] if op.key_findings else "").strip()
            if not text:
                text = f"{op.expert} 지목"
            for sym in op.affected_symbols[:5]:
                await self.trade_wiki.note_research(
                    symbol=str(sym),
                    source=op.expert,
                    text=f"{op.regime_bias.value}({op.score:+d}) {text}",
                )
        except Exception as e:
            logger.debug(f"[Orchestrator] 종목 노트 기록 실패 (무시): {e}")

    # ─────────────────────────────────────────
    # 단일 호출
    # ─────────────────────────────────────────
    async def run_one(self, name: str, force: bool = False) -> Optional[ExpertOpinion]:
        agent = self.agents.get(name)
        if agent is None:
            logger.warning(f"[Orchestrator] 전문가 없음: {name}")
            return None
        op = await agent.run(force=force)
        self._last_run[name] = datetime.now()
        if op.is_valid:
            save_opinion(op)
            asyncio.create_task(wiki_mod.ingest_opinion(op))
        return op

    # ─────────────────────────────────────────
    # 일괄 호출 (병렬)
    # ─────────────────────────────────────────
    async def run_all(self, force: bool = False) -> Dict[str, ExpertOpinion]:
        """모든 활성 전문가를 병렬 실행"""
        if not self.config.enabled:
            logger.info("[Orchestrator] 전문가 시스템 비활성화")
            return {}

        targets = [
            (name, agent)
            for name, agent in self.agents.items()
            if self.config.agents.get(name, True)
        ]
        if not targets:
            return {}

        logger.info(f"[Orchestrator] {len(targets)}명 병렬 분석 시작")
        results = await asyncio.gather(
            *[a.run(force=force) for _, a in targets],
            return_exceptions=True,
        )

        opinions: Dict[str, ExpertOpinion] = {}
        for (name, _), result in zip(targets, results):
            if isinstance(result, Exception):
                logger.error(f"[Orchestrator] {name} 예외: {result}")
                opinions[name] = ExpertOpinion.error_opinion(name, str(result))
            elif isinstance(result, ExpertOpinion):
                opinions[name] = result
                if result.is_valid:
                    save_opinion(result)
                    # 2026-08-08 P2: fire-and-forget 예외 관찰 — 미회수 예외가
                    # 'Task exception was never retrieved'로만 남던 문제
                    _t1 = asyncio.create_task(wiki_mod.ingest_opinion(result))
                    _t1.add_done_callback(self._log_task_exception)
                    # 지목 종목을 종목 위키 리서치 노트에 기록 (2026-08-07)
                    if self.trade_wiki is not None and result.affected_symbols:
                        _t2 = asyncio.create_task(self._note_symbols(result))
                        _t2.add_done_callback(self._log_task_exception)
            else:
                # defensive: 예기치 못한 반환 타입
                logger.warning(
                    f"[Orchestrator] {name} 비정상 반환 타입: {type(result).__name__}"
                )
                opinions[name] = ExpertOpinion.error_opinion(name, "invalid_type")
            self._last_run[name] = datetime.now()

        valid = sum(1 for op in opinions.values() if op.is_valid)
        logger.info(f"[Orchestrator] 완료: {valid}/{len(opinions)} 유효")
        return opinions

    # ─────────────────────────────────────────
    # 집계 — 시장 체제 보정값
    # ─────────────────────────────────────────
    def aggregate_regime_score(
        self,
        opinions: Optional[Dict[str, ExpertOpinion]] = None,
    ) -> int:
        """시장체제 보정 점수 (-30 ~ +30)

        가중 평균 점수에 시장 관련 전문가만 반영.
        market_regime.py가 호출하여 base regime 점수를 보정.
        """
        if opinions is None:
            opinions = self.snapshot()

        market_experts = (
            "macro_economist",
            "kr_market_expert",
            "us_market_expert",
            "kr_economy_expert",
            "global_micro_expert",
            "weekend_signal_expert",   # 2026-06-07 추가 — 갭 risk 종합 반영
        )

        weighted_sum = 0.0
        weight_total = 0.0
        for name in market_experts:
            op = opinions.get(name)
            if op is None or not op.is_valid:
                continue
            # P0-4 (2026-05-29 리뷰): 음수 가중치/confidence 방어
            cfg_w = max(0.0, float(self.config.weights.get(name, 1.0)))
            conf = max(0.0, min(1.0, float(op.confidence)))
            w = cfg_w * conf
            if w <= 0:
                continue
            weighted_sum += op.score * w
            weight_total += w

        if weight_total <= 0:
            return 0

        avg = weighted_sum / weight_total
        # ±100 → ±30으로 스케일링
        return int(max(-30, min(30, avg * 0.3)))

    # 시장 체제 집계에서 제외되는 전문가 (종목 단위 판단 전용)
    NON_REGIME_EXPERTS = frozenset({"sector_council"})

    def aggregate_bias(
        self,
        opinions: Optional[Dict[str, ExpertOpinion]] = None,
    ) -> RegimeBias:
        """다수결 bias (sector_council 제외 — 섹터 의견이 시장 판단 희석 방지)"""
        if opinions is None:
            opinions = self.snapshot()
        counts = {RegimeBias.BULL: 0.0, RegimeBias.NEUTRAL: 0.0, RegimeBias.BEAR: 0.0}
        for op in opinions.values():
            if not op.is_valid or op.expert in self.NON_REGIME_EXPERTS:
                continue
            w = self.config.weights.get(op.expert, 1.0) * op.confidence
            counts[op.regime_bias] += w
        return max(counts.items(), key=lambda x: x[1])[0]

    # ─────────────────────────────────────────
    # cross_validator 게이트
    # ─────────────────────────────────────────
    def bear_consensus(
        self,
        threshold_confidence: float = 0.7,
        min_count: int = 2,
        opinions: Optional[Dict[str, ExpertOpinion]] = None,
    ) -> bool:
        """confidence ≥ threshold인 BEAR 의견이 min_count 이상이면 True"""
        if opinions is None:
            opinions = self.snapshot()
        bear_strong = [
            op for op in opinions.values()
            if op.is_valid
            and op.expert not in self.NON_REGIME_EXPERTS
            and op.regime_bias == RegimeBias.BEAR
            and op.confidence >= threshold_confidence
        ]
        return len(bear_strong) >= min_count

    # ─────────────────────────────────────────
    # 스냅샷
    # ─────────────────────────────────────────
    def snapshot(self) -> Dict[str, ExpertOpinion]:
        """현재 캐시된 의견들"""
        result: Dict[str, ExpertOpinion] = {}
        for name, agent in self.agents.items():
            op = agent.cached()
            if op is not None:
                result[name] = op
        return result

    def stats(self) -> Dict[str, Any]:
        return {
            "enabled": self.config.enabled,
            "agents": [a.stats() for a in self.agents.values()],
            "last_run": {k: v.isoformat() for k, v in self._last_run.items()},
        }

    # ─────────────────────────────────────────
    # 종료 처리 (P1-1, 2026-05-29 리뷰)
    # ─────────────────────────────────────────
    async def close_all(self) -> None:
        """모든 에이전트의 aiohttp 세션 정리 (봇 셧다운 시 호출)"""
        for name, agent in self.agents.items():
            fn = getattr(agent, "close", None)
            if fn is None:
                continue
            try:
                if asyncio.iscoroutinefunction(fn):
                    await fn()
                else:
                    fn()
            except Exception as e:
                logger.debug(f"[Orchestrator] {name} close 실패: {e}")

    # ─────────────────────────────────────────
    # 종목별 뉴스 sentiment (cross_validator·engine.on_signal용)
    # ─────────────────────────────────────────
    async def get_news_sentiment(self, symbol: str) -> Optional[Dict[str, Any]]:
        """news_curator에 종목별 sentiment 조회"""
        curator = self.agents.get("news_curator")
        if curator is None:
            return None
        try:
            fn = getattr(curator, "get_symbol_sentiment", None)
            if fn is None:
                return None
            return await fn(symbol)
        except Exception as e:
            logger.debug(f"[Orchestrator] news sentiment 조회 실패 ({symbol}): {e}")
            return None
