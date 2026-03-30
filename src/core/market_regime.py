"""
QWQ AI Trader - 시장 체제 사전 적응

시장 체제(bull/bear/sideways)를 판단하고,
전략 파라미터를 사전에 조정합니다.

기존 스마트 사이드카(사후 방어)와 상호 보완:
- MarketRegimeAdapter: 장 시작 시 사전 조정 (공격/방어 모드)
- SmartSidecar: 장중 손실 발생 시 사후 차단 (안전망)
"""

import os
from datetime import datetime
from typing import Dict, Optional
import aiohttp
from loguru import logger


class MarketRegimeAdapter:
    """시장 체제별 전략 파라미터 동적 조정"""

    # 체제별 파라미터 기본값
    REGIME_PARAMS = {
        "bull": {
            "sepa_min_score_adj": -5,       # 기본 min_score에서 완화
            "theme_max_change_adj": 2.0,    # 기본 max_change에 추가
            "max_daily_new_buys": 5,
            "position_mult_boost": 1.1,
            "description": "강세장: 적극적 진입",
        },
        "bear": {
            "sepa_min_score_adj": +10,      # 기본 min_score에서 강화
            "theme_max_change_adj": -2.0,   # 기본 max_change에서 축소
            "max_daily_new_buys": 2,
            "position_mult_boost": 0.7,
            "description": "약세장: 보수적 진입",
        },
        "sideways": {
            "sepa_min_score_adj": +3,
            "theme_max_change_adj": 0.0,
            "max_daily_new_buys": 3,
            "position_mult_boost": 0.9,
            "description": "횡보장: 선별적 진입",
        },
        "neutral": {
            "sepa_min_score_adj": 0,
            "theme_max_change_adj": 0.0,
            "max_daily_new_buys": 4,
            "position_mult_boost": 1.0,
            "description": "중립: 기본 기준",
        },
    }

    def __init__(self):
        self._current_regime: str = "neutral"
        self._regime_data: Dict = {}
        self._last_update: Optional[datetime] = None

    def update_regime(self, kospi_data: dict, kosdaq_data: dict):
        """
        시장 체제 판단 (KOSPI/KOSDAQ OHLCV 기반)

        판단 기준:
        - bull:     평균 등락률 > +1% AND 시가대비 상승
        - bear:     평균 등락률 < -1% AND 시가대비 하락
        - sideways: 그 외
        """
        kospi_change = kospi_data.get("change_pct", 0)
        kosdaq_change = kosdaq_data.get("change_pct", 0)
        avg_change = (kospi_change + kosdaq_change) / 2

        kospi_vs_open = 0
        if kospi_data.get("open", 0) > 0 and kospi_data.get("price", 0) > 0:
            kospi_vs_open = (kospi_data["price"] - kospi_data["open"]) / kospi_data["open"] * 100
        kosdaq_vs_open = 0
        if kosdaq_data.get("open", 0) > 0 and kosdaq_data.get("price", 0) > 0:
            kosdaq_vs_open = (kosdaq_data["price"] - kosdaq_data["open"]) / kosdaq_data["open"] * 100
        avg_vs_open = (kospi_vs_open + kosdaq_vs_open) / 2

        prev_regime = self._current_regime

        if avg_change > 1.0 and avg_vs_open > 0.3:
            self._current_regime = "bull"
        elif avg_change < -1.0 and avg_vs_open < -0.3:
            self._current_regime = "bear"
        elif abs(avg_change) <= 1.0:
            self._current_regime = "sideways"
        else:
            # 혼조: 이전 상태 유지 (과도한 전환 방지)
            pass

        self._regime_data = {
            "kospi_change": kospi_change,
            "kosdaq_change": kosdaq_change,
            "avg_change": avg_change,
            "avg_vs_open": avg_vs_open,
        }
        self._last_update = datetime.now()

        if prev_regime != self._current_regime:
            params = self.REGIME_PARAMS[self._current_regime]
            logger.info(
                f"[시장체제] {prev_regime} → {self._current_regime}: "
                f"{params['description']} "
                f"(전일비 {avg_change:+.1f}%, 시가비 {avg_vs_open:+.1f}%)"
            )

    @property
    def regime(self) -> str:
        return self._current_regime

    @property
    def params(self) -> Dict:
        return self.REGIME_PARAMS.get(self._current_regime, self.REGIME_PARAMS["neutral"])

    def get_adjusted_min_score(self, base_min_score: float) -> float:
        """체제 반영 min_score"""
        adj = self.params.get("sepa_min_score_adj", 0)
        return base_min_score + adj

    def get_position_boost(self) -> float:
        """체제 반영 포지션 배율"""
        return self.params.get("position_mult_boost", 1.0)

    def get_summary(self) -> Dict:
        """현재 체제 요약"""
        return {
            "regime": self._current_regime,
            "params": self.params,
            "data": self._regime_data,
            "last_update": self._last_update.isoformat() if self._last_update else None,
            "llm_assessment": self._llm_assessment,
        }

    # ============================================================
    # LLM 장 시작 전 시장 진단 (08:50 실행)
    # ============================================================

    _llm_assessment: str = ""
    _llm_assessment_date = None

    async def llm_morning_diagnosis(
        self,
        llm_manager,
        theme_summary: str = "",
        premarket_data: Dict = None,
        news_headlines: str = "",
    ):
        """
        장 시작 전 LLM 시장 진단 — GPT-5.4 1회/일

        뉴스+매크로+넥스트장 맥락으로 체제 판단을 보강합니다.

        Args:
            llm_manager: LLMManager 인스턴스
            theme_summary: 오늘 테마 탐지 요약
            premarket_data: 넥스트장 시세 (보유 종목별 등락률)
            news_headlines: 최신 뉴스 헤드라인 요약
        """
        from datetime import date as _date
        today = _date.today()
        if self._llm_assessment_date == today:
            return  # 당일 중복 실행 방지

        from ..utils.llm import LLMTask

        regime_info = (
            f"현재 체제: {self._current_regime}\n"
            f"KOSPI 등락: {self._regime_data.get('kospi_change', 0):+.1f}%\n"
            f"KOSDAQ 등락: {self._regime_data.get('kosdaq_change', 0):+.1f}%\n"
            f"시가대비: {self._regime_data.get('avg_vs_open', 0):+.1f}%"
        )

        # 넥스트장 데이터 추가
        premarket_info = ""
        if premarket_data:
            pm_lines = []
            for sym, pm in premarket_data.items():
                if pm.get("price", 0) > 0:
                    pm_lines.append(f"  {sym}: {pm.get('change_pct', 0):+.1f}% (거래량 {pm.get('volume', 0):,})")
            if pm_lines:
                premarket_info = f"\n=== 넥스트장 보유종목 시세 ===\n" + "\n".join(pm_lines[:8])

        # Perplexity 실시간 검색으로 매크로 컨텍스트 보강 (PRISM 차용)
        perplexity_context = ""
        _pplx_key = os.getenv("PERPLEXITY_API_KEY", "")
        if _pplx_key:
            try:
                perplexity_context = await self._fetch_perplexity_context(_pplx_key)
                if perplexity_context:
                    logger.info(f"[시장체제] Perplexity 매크로 검색 완료 ({len(perplexity_context)}자)")
            except Exception as _pe:
                logger.debug(f"[시장체제] Perplexity 검색 실패 (무시): {_pe}")

        prompt = (
            f"당신은 KR 주식시장 전문 분석가입니다.\n\n"
            f"=== 현재 시장 상황 ===\n{regime_info}\n"
            + (premarket_info + "\n" if premarket_info else "")
            + (f"\n=== 오늘 테마 ===\n{theme_summary}\n" if theme_summary else "")
            + (f"\n=== 뉴스 헤드라인 ===\n{news_headlines}\n" if news_headlines else "")
            + (f"\n=== 실시간 매크로 ===\n{perplexity_context}\n" if perplexity_context else "")
            + f"\n=== 진단 요청 ===\n"
            f"오늘 장 전략 방향을 한 줄로 제시하세요.\n"
            f"형식: [공격/중립/방어] 사유\n"
            f"예: [공격] 반도체 수급 강세 + 미국 기술주 호조 + 넥스트장 강세, SEPA 확대\n"
            f"예: [방어] 관세 리스크 + 넥스트장 약세 + 원화 약세, 테마 축소 권고"
        )

        try:
            resp = await llm_manager.complete(
                prompt, task=LLMTask.MARKET_ANALYSIS, max_tokens=150,
            )
            if resp.success and resp.content:
                self._llm_assessment = resp.content.strip()
                self._llm_assessment_date = today

                # 체제 미세 조정 (LLM이 [방어]인데 체제가 bull이면 sideways로)
                content_upper = self._llm_assessment.upper()
                if "[방어]" in self._llm_assessment and self._current_regime == "bull":
                    self._current_regime = "sideways"
                    logger.info(
                        f"[시장체제] LLM 진단으로 bull → sideways 조정: "
                        f"{self._llm_assessment[:60]}"
                    )
                elif "[공격]" in self._llm_assessment and self._current_regime == "bear":
                    self._current_regime = "sideways"
                    logger.info(
                        f"[시장체제] LLM 진단으로 bear → sideways 조정: "
                        f"{self._llm_assessment[:60]}"
                    )

                logger.info(f"[시장체제] LLM 장전 진단: {self._llm_assessment[:80]}")
            else:
                logger.debug(f"[시장체제] LLM 진단 실패 (무시): {resp.error}")
        except Exception as e:
            logger.debug(f"[시장체제] LLM 진단 오류 (무시): {e}")

    async def _fetch_perplexity_context(self, api_key: str) -> str:
        """Perplexity 실시간 검색 — 오늘 KR 시장 매크로 컨텍스트 수집

        Sonar 모델 사용, 1회 ~$0.005. 실패 시 빈 문자열 반환.
        """
        try:
            timeout = aiohttp.ClientTimeout(total=15)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                payload = {
                    "model": "sonar",
                    "messages": [
                        {
                            "role": "user",
                            "content": (
                                "오늘 한국 주식시장에 영향을 줄 핵심 이슈 3가지를 "
                                "한 줄씩 간결하게 알려주세요. "
                                "글로벌 매크로, 환율, 정책, 섹터 동향 위주로."
                            ),
                        }
                    ],
                    "max_tokens": 200,
                }
                headers = {
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                }
                async with session.post(
                    "https://api.perplexity.ai/chat/completions",
                    json=payload,
                    headers=headers,
                ) as resp:
                    if resp.status != 200:
                        logger.debug(f"[Perplexity] HTTP {resp.status}")
                        return ""
                    data = await resp.json()
                    content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
                    return content.strip()[:500]  # 500자 제한
        except Exception as e:
            logger.debug(f"[Perplexity] 검색 실패: {e}")
            return ""
