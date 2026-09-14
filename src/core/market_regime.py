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
from dataclasses import dataclass, replace
from datetime import datetime, time as dtime
from pathlib import Path
from typing import Dict, Optional
import aiohttp
from loguru import logger


# VIX 캐시 설정
_VIX_CACHE_PATH = Path.home() / ".cache" / "ai_trader" / "vix_cache.json"
_VIX_CACHE_TTL_SEC = 6 * 3600  # 6시간
_VIX_FEAR_THRESHOLD = 30.0
_VIX_COMPLACENCY_THRESHOLD = 15.0


# ============================================================
# 판단 시간 범위 분리 (2026-09-14 리뷰 후속 T9 요청 2)
#
# 09-14 사고: 08:20 스크리너 메모리(KOSPI 5일 +3.3%)로 만든 강세 판단이
# 당일 -3.34% 급락 중 12:00 에도 그대로 유효 레짐으로 쓰였다.
# 세 관점을 섞지 않고 별도 필드로 보관한다:
#   mid_trend        — 5/20일 중기 추세 (느리게 변함)
#   open_expectation — 장전 개장 예상 (09:30 이후 무효)
#   intraday_risk    — 현재 장중 위험 (급락 감지기 상태 + 당일 등락)
# ============================================================

# 장전 개장 예상의 유효 종료 시각 — 개장 30분 뒤에는 예상이 아니라 사실로 판단한다
OPEN_EXPECTATION_EXPIRY = dtime(9, 30)

# 급락 감지기 상태 (batch_analyzer._intraday_state 와 동일 어휘)
INTRADAY_RISK_LEVELS = ("normal", "caution", "crash", "severe")

# 강세를 강세로 취급하지 않는 장중 위험 수준
_BULL_BLOCKING_RISK = ("crash", "severe")

# 강등 표 — "bull 로 취급하지 않는" 최소 강등만 한다 (새 차단 추가 아님).
# bull → sideways 는 기존 VIX Fear 강등과 같은 방향,
# trending_bull → neutral 은 기존 레짐충돌가드(_KOSPI_CAP["bear"]) 와 같은 방향.
_BULL_DEMOTION = {"bull": "sideways", "trending_bull": "neutral"}


def _now() -> datetime:
    """테스트 주입용 단일 시계 진입점 (유효 레짐의 장중 위험 당일 게이트)"""
    return datetime.now()


def classify_intraday_level(change_pct: Optional[float]) -> Optional[str]:
    """KOSPI 당일 등락률 → 장중 급락 단계 (단일 출처).

    임계값은 `INTRADAY_CRASH_PARAMS` 문서와 동일 (-1.5 / -2.5 / -3.5).
    `batch_analyzer.update_intraday_state` 와 12:00/08:10 레짐 분류기가 **같은 함수**를
    써야 "감지기는 normal 인데 이번 조회는 -3%" 같은 어긋남이 생기지 않는다 (T10 F14).

    결측(None)은 0·normal 로 채우지 않고 None 을 돌려준다.
    """
    if change_pct is None:
        return None
    if change_pct <= -3.5:
        return "severe"
    if change_pct <= -2.5:
        return "crash"
    if change_pct <= -1.5:
        return "caution"
    return "normal"


def max_intraday_level(*levels: Optional[str]) -> Optional[str]:
    """여러 장중 위험 관측 중 **더 보수적인(심한) 쪽**을 고른다.

    전부 결측이면 None — 결측을 normal 로 승격시키지 않는다 (캡 근거가 없다는 뜻).
    """
    best: Optional[str] = None
    best_idx = -1
    for level in levels:
        if level not in INTRADAY_RISK_LEVELS:
            continue
        idx = INTRADAY_RISK_LEVELS.index(level)
        if idx > best_idx:
            best, best_idx = level, idx
    return best


def cap_regime_by_intraday_risk(regime: str, intraday_risk: Optional[str]) -> str:
    """장중 위험이 crash/severe 이면 강세 레짐을 강등한다.

    두 어휘(엔진 bull/bear/sideways, LLM 분류기 trending_bull/…)를 모두 받는다.
    강세가 아닌 레짐·정상 장세는 원본을 그대로 돌려준다 (새 차단 없음).
    """
    if intraday_risk not in _BULL_BLOCKING_RISK:
        return regime
    return _BULL_DEMOTION.get(regime, regime)


@dataclass(frozen=True)
class RegimeHorizons:
    """시간 범위별 레짐 관점 — 값이 없으면 None (0·중립으로 채우지 않는다)

    mid_trend 는 여기 담지 않는다. 중기 추세의 단일 출처는 `update_regime()` 결과
    (`_current_regime`)이며, 별도 오버라이드를 두면 2분 주기 실시간 판단·VIX 강등·
    전문가 BEAR 합의·LLM [방어] 결과를 오래된 값이 영구히 가린다 (2026-09-14 리뷰).
    """

    open_expectation: Optional[str] = None
    open_expectation_as_of: Optional[datetime] = None
    intraday_risk: Optional[str] = None
    intraday_risk_as_of: Optional[datetime] = None
    intraday_change_pct: Optional[float] = None


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
        # 시간 범위별 관점 (2026-09-14 T9 요청 2)
        self._horizons = RegimeHorizons()

    def get_params(self) -> Dict:
        """현재 체제의 파라미터 반환"""
        return self.params

    # ============================================================
    # 시간 범위별 관점 (2026-09-14 리뷰 후속 T9 요청 2)
    # ============================================================

    @property
    def horizons(self) -> RegimeHorizons:
        """원본 관점 스냅샷 (만료 필터 미적용)"""
        return self._horizons

    def set_open_expectation(self, expectation: str, as_of: Optional[datetime] = None) -> None:
        """장전 개장 예상을 기록한다 — 09:30 이후에는 읽을 때 무효 처리된다."""
        self._horizons = replace(
            self._horizons, open_expectation=expectation,
            open_expectation_as_of=as_of if as_of is not None else datetime.now(),
        )

    def set_intraday_risk(
        self,
        level: str,
        change_pct: Optional[float] = None,
        as_of: Optional[datetime] = None,
    ) -> None:
        """장중 위험(급락 감지기 상태)과 당일 등락률을 기록한다.

        **역순 도착 거부** (2026-09-15 T10 F14): 5분 감지기·12:00 분류기·재시도가
        같은 어댑터에 쓰므로, 늦게 도착한 **과거** 관측(as_of 가 기존보다 이르거나
        아예 없는 값)이 최신 crash 를 normal 로 덮으면 안 된다. as_of 없는 값은
        "지금 관측"으로 취급하지 않는다 — 기존 관측이 있으면 무시하고, 없으면
        as_of=None(결측)으로 저장해 `effective_regime` 의 당일 게이트에서 걸러진다.
        """
        if level not in INTRADAY_RISK_LEVELS:
            raise ValueError(
                f"intraday_risk 는 {INTRADAY_RISK_LEVELS} 중 하나여야 합니다: {level!r}"
            )
        prev_as_of = self._horizons.intraday_risk_as_of
        if prev_as_of is not None and (as_of is None or as_of < prev_as_of):
            logger.debug(
                f"[레짐] 역순 장중 위험 무시: {level}@{as_of} ≤ 기존 "
                f"{self._horizons.intraday_risk}@{prev_as_of}"
            )
            return
        self._horizons = replace(
            self._horizons, intraday_risk=level, intraday_change_pct=change_pct,
            intraday_risk_as_of=as_of,
        )

    @property
    def mid_trend(self) -> str:
        """중기 추세 관점 — update_regime() 결과 단일 출처 (기준시각 = _last_update)"""
        return self._current_regime

    def open_expectation(self, now: Optional[datetime] = None) -> Optional[str]:
        """장전 개장 예상 — 09:30 이후·다음 날에는 None (만료)"""
        h = self._horizons
        if h.open_expectation is None or h.open_expectation_as_of is None:
            return None
        now = now if now is not None else datetime.now()
        if now.date() != h.open_expectation_as_of.date():
            return None
        if now.time() >= OPEN_EXPECTATION_EXPIRY:
            return None
        return h.open_expectation

    @property
    def effective_regime(self) -> str:
        """게이트·사이징이 읽는 유효 레짐 — 장중 위험이 오래된 강세 전망을 이긴다.
        장중 위험은 **당일** 관측만 인정 — 전일 15:3x crash 가 다음 날 장전(PRE_MARKET 2분 루프)까지
        강등을 거는 잔존 경로 차단 (2026-09-14 재리뷰). as_of 없음/다른 날짜 = 결측(None)."""
        risk = self._horizons.intraday_risk
        as_of = self._horizons.intraday_risk_as_of
        if risk is not None and (as_of is None or as_of.date() != _now().date()):
            risk = None
        return cap_regime_by_intraday_risk(self.mid_trend, risk)

    def _horizons_summary(self, now: Optional[datetime] = None) -> Dict:
        """관점별 값·기준시각·결측 사유 — 결측을 0·중립으로 채우지 않는다"""
        h = self._horizons
        effective = self.effective_regime

        def _iso(dt: Optional[datetime]) -> Optional[str]:
            return dt.isoformat() if dt is not None else None

        expectation = self.open_expectation(now=now)
        if h.open_expectation is None:
            expectation_reason = "장전 진단 미수집"
        elif expectation is None:
            expectation_reason = "09:30 만료 (개장 후에는 예상이 아니라 실측으로 판단)"
        else:
            expectation_reason = ""

        return {
            "mid_trend": {
                # 값은 실제 게이트가 쓰는 _current_regime 그대로 내되, 미갱신이면 missing 으로 표시
                "value": self.mid_trend,
                "as_of": _iso(self._last_update),
                "missing": self._last_update is None,
                "reason": "update_regime 미실행 (지수 시세 미수집)"
                          if self._last_update is None else "",
                "source": "update_regime",
            },
            "open_expectation": {
                "value": expectation,
                "as_of": _iso(h.open_expectation_as_of),
                "missing": expectation is None,
                "reason": expectation_reason,
            },
            "intraday_risk": {
                "value": h.intraday_risk,
                "as_of": _iso(h.intraday_risk_as_of),
                "change_pct": h.intraday_change_pct,
                "missing": h.intraday_risk is None,
                "reason": "급락 감지 상태 미전달" if h.intraday_risk is None else "",
            },
            "effective_regime": effective,
            "capped_by_intraday_risk": effective != self.mid_trend,
        }

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
        """유효 레짐 — 장중 crash/severe 중에는 강세로 취급하지 않는다"""
        return self.effective_regime

    @property
    def params(self) -> Dict:
        return self.REGIME_PARAMS.get(self.effective_regime, self.REGIME_PARAMS["neutral"])

    def get_adjusted_min_score(self, base_min_score: float) -> float:
        """체제 반영 min_score"""
        adj = self.params.get("sepa_min_score_adj", 0)
        return base_min_score + adj

    def get_position_boost(self) -> float:
        """체제 반영 포지션 배율"""
        return self.params.get("position_mult_boost", 1.0)

    # ============================================================
    # 전문가 시스템 보정 (2026-05-29 추가)
    # ============================================================
    def apply_expert_adjustment(self, expert_orchestrator) -> None:
        """ExpertOrchestrator의 집계 점수로 체제를 보정한다.

        - aggregate_regime_score: -30 ~ +30
        - bear_consensus: confidence≥0.7인 BEAR 2명 이상

        효과:
        - bear 합의 → bull/sideways → bear 강등 (안전 우선)
        - 강한 bull 점수 (+20 이상) → sideways → bull 격상 (확인 지연 단축)
        """
        if expert_orchestrator is None:
            return
        try:
            score = expert_orchestrator.aggregate_regime_score()
            bear_consensus = expert_orchestrator.bear_consensus(
                threshold_confidence=0.7, min_count=2
            )
        except Exception as e:
            logger.debug(f"[시장체제] 전문가 보정 실패: {e}")
            return

        prev = self._current_regime

        # P1-6 (2026-05-29 리뷰): pending_regime 메커니즘과 통합
        # 즉시 변경 대신 _expert_pending에 등록하고 확인 시간(기본 10분) 후 적용.
        # 페이크 BEAR 변동에 의한 잦은 체제 변경 방지.
        proposed: Optional[str] = None
        reason: str = ""
        if bear_consensus and self._current_regime in ("bull", "sideways", "neutral"):
            proposed = "bear"
            reason = f"전문가 BEAR 합의 (score={score})"
        elif score >= 20 and self._current_regime in ("sideways", "neutral"):
            proposed = "bull"
            reason = f"전문가 BULL 강한 합의 (score={score})"
        elif score <= -20 and self._current_regime in ("bull", "sideways"):
            proposed = "sideways"
            reason = f"전문가 BEAR (score={score})"
        elif score >= 10 and not bear_consensus and self._current_regime == "bear":
            proposed = "sideways"
            reason = f"전문가 BEAR 합의 해소 (score={score})"

        EXPERT_CONFIRM_SEC = 600  # 10분 — VIX의 confirm_delay와 통일
        if proposed is None:
            # 변경 없음 → pending 클리어
            if hasattr(self, "_expert_pending"):
                self._expert_pending = None
        elif (
            not hasattr(self, "_expert_pending")
            or self._expert_pending != proposed
        ):
            # 새 제안 → pending 등록만, 즉시 변경 안 함
            self._expert_pending = proposed
            self._expert_pending_since = datetime.now()
            logger.info(
                f"[시장체제] 전문가 제안: {prev} → {proposed} ({reason}, 확인 대기 10분)"
            )
        elif (
            datetime.now() - self._expert_pending_since
        ).total_seconds() >= EXPERT_CONFIRM_SEC:
            self._current_regime = proposed
            logger.info(f"[시장체제] 전문가 보정 확정: {prev} → {proposed} ({reason})")
            self._expert_pending = None

        # 디버그 흔적
        self._regime_data["expert_score"] = score
        self._regime_data["expert_bear_consensus"] = bear_consensus

    def get_summary(self, now: Optional[datetime] = None) -> Dict:
        """현재 체제 요약 (유효 레짐 + 시간 범위별 관점)"""
        return {
            "regime": self.effective_regime,
            "base_regime": self._current_regime,
            "params": self.params,
            "data": self._regime_data,
            "horizons": self._horizons_summary(now=now),
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

        def _pct(key: str) -> str:
            """결측을 +0.0% 로 포장하지 않는다 — 값이 없으면 '미수집'"""
            value = self._regime_data.get(key)
            return f"{value:+.1f}%" if value is not None else "미수집"

        regime_info = (
            f"현재 체제: {self._current_regime}\n"
            f"KOSPI 등락: {_pct('kospi_change')}\n"
            f"KOSDAQ 등락: {_pct('kosdaq_change')}\n"
            f"시가대비: {_pct('avg_vs_open')}"
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
                # 장전 진단은 "오늘 개장 예상" — 09:30 이후에는 만료된다 (T9 요청 2)
                self.set_open_expectation(self._llm_assessment)

                # 체제 미세 조정 (LLM이 [방어]인데 체제가 bull이면 sideways로)
                if "[방어]" in self._llm_assessment and self._current_regime == "bull":
                    self._current_regime = "sideways"
                    logger.info(
                        f"[시장체제] LLM 진단으로 bull → sideways 조정: "
                        f"{self._llm_assessment[:60]}"
                    )
                elif "[공격]" in self._llm_assessment and self._current_regime == "bear":
                    # 장중 급락 중에는 장전 낙관 진단으로 상향하지 않는다 (T9 요청 2)
                    if self._horizons.intraday_risk in _BULL_BLOCKING_RISK:
                        logger.warning(
                            f"[시장체제] LLM [공격] 진단이지만 장중 위험="
                            f"{self._horizons.intraday_risk} → bear 유지"
                        )
                    else:
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
        # race 방지: 이미 실행 중인 fetch 태스크가 있으면 재예약 금지
        if getattr(self, "_vix_fetch_task", None) is not None and not self._vix_fetch_task.done():
            return
        try:
            loop = asyncio.get_running_loop()
            self._vix_fetch_task = loop.create_task(self._fetch_vix())
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

            # 캐시 파일 영속화 (원자적 쓰기: tmp → rename)
            try:
                _VIX_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
                tmp_path = _VIX_CACHE_PATH.with_suffix(".tmp")
                with tmp_path.open("w", encoding="utf-8") as f:
                    json.dump(
                        {
                            "timestamp": self._vix_last_fetch.isoformat(),
                            "value": self._vix_value,
                        },
                        f,
                    )
                os.replace(tmp_path, _VIX_CACHE_PATH)
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
