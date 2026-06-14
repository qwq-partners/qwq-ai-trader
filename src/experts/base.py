"""ExpertAgent 추상 베이스"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from datetime import datetime, timedelta
from typing import Any, Dict, Optional

from loguru import logger

from .types import ExpertOpinion, ExpertConfig, RegimeBias


# P0-6 (2026-05-29 리뷰): yfinance 동시 호출 상한 (429 throttling 방지)
# 7명 × 평균 6 ticker = 42 req/batch → 동시 4개로 제한
YFINANCE_SEMAPHORE = asyncio.Semaphore(4)


class ExpertAgent(ABC):
    """모든 도메인 전문가의 베이스 클래스

    하위 클래스는 `_analyze()`만 구현하면 됨.
    `run()`은 캐시·예산·에러 핸들링을 일괄 처리한다.
    """

    name: str = "expert"               # 하위 클래스에서 오버라이드
    refresh_minutes: int = 60          # 기본 캐시 신선도
    cost_per_call_usd: float = 0.005   # 호출당 예상 비용 (LLM+검색 합계)

    def __init__(
        self,
        config: ExpertConfig,
        llm_manager: Any = None,        # src.utils.llm.LLMManager
        perplexity_key: str = "",
    ):
        self.config = config
        self.llm_manager = llm_manager
        self.perplexity_key = perplexity_key

        self._cached_opinion: Optional[ExpertOpinion] = None
        self._call_count_today: int = 0
        self._call_count_date: Optional[str] = None
        self._lock = asyncio.Lock()

    # ─────────────────────────────────────────
    # 공개 인터페이스
    # ─────────────────────────────────────────
    async def run(self, force: bool = False) -> ExpertOpinion:
        """전문가 의견을 반환 (캐시·예산 자동 처리)"""
        async with self._lock:
            if not self._enabled():
                return ExpertOpinion.error_opinion(self.name, "disabled")

            if not force and self._cached_opinion and self._cached_opinion.is_valid:
                return self._cached_opinion

            if not self._check_budget():
                logger.warning(f"[{self.name}] 일일 호출 예산 초과 — 캐시 또는 기본값 사용")
                # P0-7 (2026-05-29 리뷰): 만료된 캐시는 재사용 금지
                if self._cached_opinion and self._cached_opinion.is_valid:
                    return self._cached_opinion
                return ExpertOpinion.error_opinion(self.name, "budget_exceeded")

            try:
                opinion = await self._analyze()
                # P0-3 (2026-05-29 리뷰): 타입 검증
                if not isinstance(opinion, ExpertOpinion):
                    logger.warning(
                        f"[{self.name}] _analyze가 ExpertOpinion이 아닌 {type(opinion).__name__} 반환"
                    )
                    opinion = ExpertOpinion.error_opinion(self.name, "invalid_return_type")
                opinion.expert = self.name
                # P0-4 (2026-05-29 리뷰): confidence/score 안전 클램프
                opinion.confidence = max(0.0, min(1.0, float(opinion.confidence or 0.0)))
                opinion.score = max(-100, min(100, int(opinion.score or 0)))
                self._cached_opinion = opinion
                self._inc_call_count()
                return opinion
            except Exception as e:
                logger.exception(f"[{self.name}] 분석 실패: {e}")
                # P0-7: 만료된 캐시는 재사용 금지 (stale BEAR 영구 차단 방지)
                if self._cached_opinion and self._cached_opinion.is_valid:
                    logger.info(f"[{self.name}] 직전 유효 캐시 재사용")
                    return self._cached_opinion
                return ExpertOpinion.error_opinion(self.name, f"exception:{type(e).__name__}")

    @abstractmethod
    async def _analyze(self) -> ExpertOpinion:
        """실제 분석 로직 — 하위 클래스 구현"""
        ...

    # ─────────────────────────────────────────
    # 유틸
    # ─────────────────────────────────────────
    def _enabled(self) -> bool:
        if not self.config.enabled:
            return False
        return bool(self.config.agents.get(self.name, True))

    def _check_budget(self) -> bool:
        today = datetime.now().date().isoformat()
        if self._call_count_date != today:
            self._call_count_date = today
            self._call_count_today = 0
        return self._call_count_today < self.config.daily_call_budget

    def _inc_call_count(self) -> None:
        self._call_count_today += 1

    def cached(self) -> Optional[ExpertOpinion]:
        """캐시된 의견 (만료 무관)"""
        return self._cached_opinion

    def stats(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "calls_today": self._call_count_today,
            "budget": self.config.daily_call_budget,
            "has_cache": self._cached_opinion is not None,
            "cache_valid": bool(self._cached_opinion and self._cached_opinion.is_valid),
        }

    # ─────────────────────────────────────────
    # 헬퍼 — yfinance throttled fetch (P0-6)
    # ─────────────────────────────────────────
    @staticmethod
    async def _yf_history(ticker: str, period: str = "30d", interval: str = "1d"):
        """yfinance history를 semaphore로 throttle (429 방지)"""
        def _sync():
            try:
                import yfinance as yf
                return yf.Ticker(ticker).history(
                    period=period, interval=interval, auto_adjust=False
                )
            except Exception:
                return None

        async with YFINANCE_SEMAPHORE:
            return await asyncio.to_thread(_sync)

    # ─────────────────────────────────────────
    # 헬퍼 — Perplexity 검색
    # ─────────────────────────────────────────
    async def _perplexity_search(
        self,
        query: str,
        max_tokens: int = 400,
        timeout_sec: int = 20,
    ) -> str:
        """Perplexity Sonar 검색 (실패 시 빈 문자열)"""
        if not self.perplexity_key:
            return ""

        import aiohttp
        try:
            timeout = aiohttp.ClientTimeout(total=timeout_sec)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                payload = {
                    "model": "sonar",
                    "messages": [{"role": "user", "content": query}],
                    "max_tokens": max_tokens,
                }
                headers = {
                    "Authorization": f"Bearer {self.perplexity_key}",
                    "Content-Type": "application/json",
                }
                async with session.post(
                    "https://api.perplexity.ai/chat/completions",
                    json=payload,
                    headers=headers,
                ) as resp:
                    if resp.status != 200:
                        logger.debug(f"[{self.name}] Perplexity HTTP {resp.status}")
                        return ""
                    data = await resp.json()
                    return data.get("choices", [{}])[0].get("message", {}).get("content", "").strip()
        except Exception as e:
            logger.debug(f"[{self.name}] Perplexity 실패: {e}")
            return ""

    # ─────────────────────────────────────────
    # 헬퍼 — LLM JSON 응답
    # ─────────────────────────────────────────
    async def _llm_json(
        self,
        prompt: str,
        system: str = "",
        task: Any = None,
    ) -> Dict[str, Any]:
        """LLM에 JSON 응답 요청 (실패 시 빈 dict)"""
        if self.llm_manager is None:
            return {}
        try:
            from src.utils.llm import LLMTask
            t = task or LLMTask.QUICK_ANALYSIS
            return await self.llm_manager.complete_json(prompt, task=t, system=system)
        except Exception as e:
            logger.debug(f"[{self.name}] LLM JSON 실패: {e}")
            return {}

    # ─────────────────────────────────────────
    # 헬퍼 — opinion 빌더
    # ─────────────────────────────────────────
    def _build_opinion(
        self,
        score: int,
        bias: RegimeBias,
        confidence: float,
        findings: list,
        sectors: Optional[list] = None,
        symbols: Optional[list] = None,
        raw: Optional[Dict[str, Any]] = None,
        valid_hours: Optional[int] = None,
    ) -> ExpertOpinion:
        ttl = valid_hours if valid_hours is not None else self.config.cache_ttl_hours
        # P2-2: issued_at 기준 TTL 계산 (build 시점이 아닌 통일된 기준)
        now = datetime.now()
        return ExpertOpinion(
            expert=self.name,
            score=max(-100, min(100, int(score))),
            regime_bias=bias,
            confidence=max(0.0, min(1.0, float(confidence))),
            key_findings=list(findings)[:5],
            affected_sectors=list(sectors or [])[:10],
            affected_symbols=list(symbols or [])[:20],
            issued_at=now,
            valid_until=now + timedelta(hours=ttl),
            raw_evidence=raw or {},
        )
