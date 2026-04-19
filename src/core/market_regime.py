"""
QWQ AI Trader - 시장 체제 사전 적응

시장 체제(bull/bear/sideways)를 판단하고,
전략 파라미터를 사전에 조정합니다.

기존 스마트 사이드카(사후 방어)와 상호 보완:
- MarketRegimeAdapter: 장 시작 시 사전 조정 (공격/방어 모드)
- SmartSidecar: 장중 손실 발생 시 사후 차단 (안전망)
"""

import os
import json
import asyncio
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional
import aiohttp
from loguru import logger


# VIX 캐시 설정
_VIX_CACHE_PATH = Path.home() / ".cache" / "ai_trader" / "vix_cache.json"
_VIX_CACHE_TTL_SEC = 6 * 3600  # 6시간
_VIX_FEAR_THRESHOLD = 30.0
_VIX_COMPLACENCY_THRESHOLD = 15.0


class MarketRegimeAdapter:
    """시장 체제별 전략 파라미터 동적 조정"""

    # 체제별 파라미터 기본값
    REGIME_PARAMS = {
        "bull": {
            "sepa_min_score_adj": -5,       # 기본 min_score 완화 (60→55)
            "theme_max_change_adj": 2.0,
            "max_daily_new_buys": 5,        # 적극 매수
            "position_mult_boost": 1.0,     # base 30%로 이미 확대 → boost 중복 방지
            "max_positions_adj": +2,        # 최대 포지션 +2 확대 (8→10)
            "base_position_pct": 30.0,      # 기본 비중 25→30%
            "min_cash_reserve_pct": 5.0,    # 최소 현금 5% 유지 (절대 하한)
            "description": "강세장: 비중 확대 + 포지션 확장",
        },
        "bear": {
            "sepa_min_score_adj": +15,      # 기준 대폭 강화 (60→75)
            "theme_max_change_adj": -2.0,
            "max_daily_new_buys": 1,        # 극소 매수 (2→1)
            "position_mult_boost": 0.6,     # 포지션 40% 축소
            "max_positions_adj": -2,
            "base_position_pct": 18.0,      # 기본 비중 축소
            "min_cash_reserve_pct": 15.0,   # 현금 15% 확보
            "description": "약세장: 극보수적 방어",
        },
        "sideways": {
            "sepa_min_score_adj": +3,
            "theme_max_change_adj": 0.0,
            "max_daily_new_buys": 3,
            "position_mult_boost": 0.9,
            "max_positions_adj": 0,
            "base_position_pct": 25.0,
            "min_cash_reserve_pct": 5.0,
            "description": "횡보장: 선별적 진입",
        },
        "neutral": {
            "sepa_min_score_adj": 0,
            "theme_max_change_adj": 0.0,
            "max_daily_new_buys": 4,
            "position_mult_boost": 1.0,
            "max_positions_adj": 0,
            "base_position_pct": 25.0,
            "min_cash_reserve_pct": 5.0,
            "description": "중립: 기본 기준",
        },
    }

    def __init__(self):
        self._current_regime: str = "neutral"
        self._regime_data: Dict = {}
        self._last_update: Optional[datetime] = None
        # VIX 보조지표 상태
        self._vix_value: Optional[float] = None
        self._vix_state: str = "normal"  # "fear" / "complacency" / "normal"
        self._vix_last_fetch: Optional[datetime] = None

    def get_params(self) -> Dict:
        """현재 체제의 파라미터 반환"""
        return self.REGIME_PARAMS.get(self._current_regime, self.REGIME_PARAMS["neutral"])

    def update_regime(self, kospi_data: dict, kosdaq_data: dict):
        """
        시장 체제 판단 (KOSPI/KOSDAQ OHLCV 기반)

        판단 기준:
        - bull:     평균 등락률 > +1% AND 시가대비 상승
        - bear:     평균 등락률 < -1% AND 시가대비 하락
        - sideways: 그 외

        VIX 보조지표 적용 (캐시 TTL 6시간):
        - VIX >= 30 (Fear): bull → sideways 강등
        - VIX <= 15 (Complacency): bull/sideways 전환 지연 30분 → 10분 단축
        """
        # VIX 캐시 로드 (TTL 만료 시 백그라운드 refresh 예약)
        self._load_vix_cache_or_refresh()
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

        # VIX complacency 상태 시 bull/sideways 전환 확인 지연 단축 (1800초 → 600초)
        confirm_delay_sec = 600 if self._vix_state == "complacency" else 1800

        # 장초 1시간(09:00~10:00) neutral 고정 — 초기 모멘텀으로 bull/bear 오판 방지
        now_hm = datetime.now().strftime("%H:%M")
        if "09:00" <= now_hm < "10:00":
            self._current_regime = "neutral"
        elif avg_change > 1.0 and avg_vs_open > 0.3:
            # 체제 전환 지연: bull/bear 전환 시 기본 30분 (complacency 시 10분)
            if prev_regime != "bull":
                if not hasattr(self, '_pending_regime') or self._pending_regime != "bull":
                    self._pending_regime = "bull"
                    self._pending_since = datetime.now()
                    self._current_regime = prev_regime  # 유지
                elif (datetime.now() - self._pending_since).total_seconds() >= confirm_delay_sec:
                    self._current_regime = "bull"
                    self._pending_regime = None
                else:
                    self._current_regime = prev_regime  # 확인 시간 미만 → 유지
            else:
                self._current_regime = "bull"
                self._pending_regime = None
        elif avg_change < -1.0 and avg_vs_open < -0.3:
            # bear 전환은 안전 우선 — VIX complacency에도 기본 30분 유지
            if prev_regime != "bear":
                if not hasattr(self, '_pending_regime') or self._pending_regime != "bear":
                    self._pending_regime = "bear"
                    self._pending_since = datetime.now()
                    self._current_regime = prev_regime
                elif (datetime.now() - self._pending_since).total_seconds() >= 1800:
                    self._current_regime = "bear"
                    self._pending_regime = None
                else:
                    self._current_regime = prev_regime
            else:
                self._current_regime = "bear"
                self._pending_regime = None
        elif abs(avg_change) <= 1.0:
            self._current_regime = "sideways"
            self._pending_regime = None
        else:
            self._current_regime = "sideways"
            self._pending_regime = None

        # VIX 기반 조정 (Fear 시 bull 강등)
        self._apply_vix_adjustment(prev_regime)

        self._regime_data = {
            "kospi_change": kospi_change,
            "kosdaq_change": kosdaq_change,
            "avg_change": avg_change,
            "avg_vs_open": avg_vs_open,
            "vix": self._vix_value,
            "vix_state": self._vix_state,
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
            "llm_assessment": getattr(self, '_llm_assessment', ''),
        }

    # ============================================================
    # LLM 장 시작 전 시장 진단 (08:50 실행)
    # ============================================================

    def _init_llm_state(self):
        """LLM 상태 초기화 (lazy)"""
        if not hasattr(self, '_llm_assessment_inited'):
            self._llm_assessment = ""
            self._llm_assessment_date = None
            self._llm_assessment_inited = True

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
        self._init_llm_state()
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

    # ============================================================
    # VIX 보조지표 (경량: 캐시 TTL 6시간, yfinance 기반, 1일 1회)
    # ============================================================

    def _load_vix_cache_or_refresh(self):
        """VIX 캐시 파일 로드 — 만료/부재 시 백그라운드 refresh 예약"""
        try:
            if _VIX_CACHE_PATH.exists():
                with _VIX_CACHE_PATH.open("r", encoding="utf-8") as f:
                    data = json.load(f)
                ts_str = data.get("timestamp")
                value = data.get("value")
                if ts_str is not None and value is not None:
                    ts = datetime.fromisoformat(ts_str)
                    age = (datetime.now() - ts).total_seconds()
                    if age < _VIX_CACHE_TTL_SEC:
                        self._vix_value = float(value)
                        self._vix_last_fetch = ts
                        self._vix_state = self._classify_vix(self._vix_value)
                        return  # 유효 캐시 적용
        except Exception as e:
            logger.debug(f"[체제] VIX 캐시 로드 실패 (무시): {e}")

        # 캐시 만료/부재 → 백그라운드 refresh 예약 (실행 중 event loop 있을 때만)
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self._fetch_vix())
        except RuntimeError:
            # 이벤트 루프가 없으면 조용히 패스 (기존 상태 유지)
            pass

    @staticmethod
    def _classify_vix(vix: float) -> str:
        """VIX 값 → 상태 라벨"""
        if vix is not None and vix >= _VIX_FEAR_THRESHOLD:
            return "fear"
        if vix is not None and vix <= _VIX_COMPLACENCY_THRESHOLD:
            return "complacency"
        return "normal"

    async def _fetch_vix(self):
        """yfinance로 VIX 조회 (동기 → to_thread 래핑). 실패 시 조용히 fallback."""
        try:
            vix_value = await asyncio.to_thread(self._fetch_vix_sync)
            if vix_value is None:
                return
            self._vix_value = float(vix_value)
            self._vix_last_fetch = datetime.now()
            self._vix_state = self._classify_vix(self._vix_value)

            # 캐시 파일 영속화
            try:
                _VIX_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
                with _VIX_CACHE_PATH.open("w", encoding="utf-8") as f:
                    json.dump(
                        {
                            "timestamp": self._vix_last_fetch.isoformat(),
                            "value": self._vix_value,
                        },
                        f,
                    )
            except Exception as e:
                logger.debug(f"[체제] VIX 캐시 저장 실패 (무시): {e}")

            logger.info(
                f"[체제] VIX={self._vix_value:.1f} ({self._vix_state}) 갱신 완료"
            )
        except Exception as e:
            # 네트워크 실패 등 — 조용히 fallback (전체 차단 금지)
            logger.debug(f"[체제] VIX 조회 실패 (무시): {e}")

    @staticmethod
    def _fetch_vix_sync() -> Optional[float]:
        """yfinance 동기 조회 헬퍼 (asyncio.to_thread에서 호출)"""
        try:
            import yfinance as yf
            hist = yf.Ticker("^VIX").history(period="2d")
            if hist is None or hist.empty:
                return None
            close = hist["Close"].iloc[-1]
            if close is None:
                return None
            return float(close)
        except Exception:
            return None

    def _apply_vix_adjustment(self, prev_regime: str):
        """
        VIX 상태 기반 체제 조정
        - Fear (>=30): bull → sideways 강등
        - Complacency (<=15): 전환 지연 단축은 update_regime 내부에서 처리
        - Normal: 조정 없음
        """
        if self._vix_value is None:
            return

        base_regime = self._current_regime
        adjusted = base_regime

        if self._vix_state == "fear" and base_regime == "bull":
            adjusted = "sideways"
            self._current_regime = adjusted
            logger.info(
                f"[체제] VIX={self._vix_value:.1f} (fear), "
                f"기준 체제 {base_regime} → 조정 {adjusted}"
            )
        else:
            # 로그는 상태 변화 또는 complacency 확인 시에만 (스팸 방지)
            if prev_regime != base_regime:
                logger.info(
                    f"[체제] VIX={self._vix_value:.1f} ({self._vix_state}), "
                    f"기준 체제 {base_regime} → 조정 {adjusted}"
                )

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
