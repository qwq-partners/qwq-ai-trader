"""
AI Trading Bot v2 - 배치 분석 엔진

스윙 모멘텀 전체 배치 분석/실행/모니터링의 중심 모듈.

흐름:
  [15:40] run_daily_scan()
    -> SwingScreener.run_full_scan() -> 기술적 지표 -> 전략별 시그널 -> JSON 저장

  [09:01] execute_pending_signals()
    -> JSON 로드 -> 현재가 확인 -> 진입 범위 내면 주문

  [매 30분] monitor_positions()
    -> REST API 현재가 -> ExitManager 체크 -> 청산 주문
"""

import asyncio
import json
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, time, date
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional

from loguru import logger

from .event import SignalEvent
from .types import (
    Signal, OrderSide, SignalStrength, StrategyType
)
from ..data.storage.signal_event_storage import SignalEventStorage as _SigLog
from ..utils.sizing import atr_position_multiplier


@dataclass
class PendingSignal:
    """대기 시그널 (JSON 직렬화 가능)"""
    symbol: str
    name: str
    strategy: str  # "rsi2_reversal" | "sepa_trend"
    side: str  # "buy"
    entry_price: float
    max_entry_price: float  # entry_price x 1.03
    stop_price: float
    target_price: float
    score: float
    reason: str
    created_at: str  # ISO format
    expires_at: str  # ISO format
    atr_pct: float = 0.0  # ATR % (ExitManager 전달용)
    # 넥스트장(시간외 단일가) 보정 데이터 (19:30 스캔 시 채워짐)
    ovtm_price_chg_pct: float = 0.0   # 시간외 가격 변동% (종가 대비)
    ovtm_vol_ratio: float = 0.0       # 시간외 거래량 / 정규장 거래량
    evening_score_adj: float = 0.0    # 19:30 스캔에서 적용된 스코어 보정치

    def is_expired(self) -> bool:
        return datetime.now() > datetime.fromisoformat(self.expires_at)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "PendingSignal":
        # 이전 JSON 호환: 없는 필드는 기본값으로
        data = d.copy()
        data.setdefault("atr_pct", 0.0)
        data.setdefault("ovtm_price_chg_pct", 0.0)
        data.setdefault("ovtm_vol_ratio", 0.0)
        data.setdefault("evening_score_adj", 0.0)
        return cls(**data)


class BatchAnalyzer:
    """스윙 모멘텀 배치 분석 엔진"""

    def __init__(self, engine, broker, kis_market_data, stock_master=None,
                 exit_manager=None, config: Optional[Dict] = None):
        from ..signals.screener.swing_screener import SwingScreener
        from ..strategies.kr.rsi2_reversal import RSI2ReversalStrategy
        from ..strategies.kr.sepa_trend import SEPATrendStrategy
        from ..strategies.base import StrategyConfig
        from ..data.providers.sector_momentum import SectorMomentumProvider

        self._engine = engine
        self._broker = broker
        self._kis_market_data = kis_market_data
        self._exit_manager = exit_manager
        self._config = config or {}

        # 섹터 모멘텀 프로바이더 (Phase 3)
        self._sector_momentum = SectorMomentumProvider(broker=broker)

        # 스크리너
        self._screener = SwingScreener(broker, kis_market_data, stock_master)

        # 전략 인스턴스
        rsi2_cfg = StrategyConfig(
            name="RSI2Reversal",
            strategy_type=StrategyType.RSI2_REVERSAL,
            min_score=self._config.get("rsi2_reversal", {}).get("min_score", 65.0),
            params=self._config.get("rsi2_reversal", {}),
        )
        sepa_cfg = StrategyConfig(
            name="SEPATrend",
            strategy_type=StrategyType.SEPA_TREND,
            min_score=self._config.get("sepa_trend", {}).get("min_score", 55.0),
            stop_loss_pct=self._config.get("sepa_trend", {}).get("stop_loss_pct", 5.0),  # 3곳 불일치 통일
            params=self._config.get("sepa_trend", {}),
        )
        self._rsi2 = RSI2ReversalStrategy(rsi2_cfg)
        self._sepa = SEPATrendStrategy(sepa_cfg)

        # 코어홀딩 전략 초기화
        self._core_strategy = None
        self._core_screener = None
        core_cfg_raw = self._config.get("core_holding", {})
        if core_cfg_raw.get("enabled", False):
            try:
                from ..strategies.kr.core_holding import CoreHoldingStrategy
                from ..signals.screener.core_screener import CoreScreener
                core_strat_cfg = StrategyConfig(
                    name="CoreHolding",
                    strategy_type=StrategyType.CORE_HOLDING,
                    min_score=core_cfg_raw.get("min_score", 70.0),
                    stop_loss_pct=core_cfg_raw.get("stop_loss_pct", 15.0),
                    params=core_cfg_raw,
                )
                self._core_strategy = CoreHoldingStrategy(core_strat_cfg)
                self._core_screener = CoreScreener(
                    broker=broker,
                    kis_market_data=kis_market_data,
                    stock_master=stock_master,
                    config=core_cfg_raw,
                )
                logger.info("[배치분석기] 코어홀딩 전략/스크리너 초기화 완료")
            except Exception as e:
                logger.warning(f"[배치분석기] 코어홀딩 초기화 실패 (무시): {e}")

        # 코어홀딩 상태 영속화 파일
        self._core_state_path = Path.home() / ".cache" / "ai_trader" / "core_holding_state.json"

        # strategic_swing 최소 점수 (2계층 이상 복합 시그널만)
        self._strategic_min_score = self._config.get(
            "strategic_swing", {}
        ).get("min_score", 70.0)

        # 대기 시그널
        self._pending: List[PendingSignal] = []
        self._signals_path = Path.home() / ".cache" / "ai_trader" / "pending_signals.json"
        self._signals_path.parent.mkdir(parents=True, exist_ok=True)

        # 시장 레짐 캐시 (스캔 후 업데이트)
        self._market_regime: str = "neutral"  # "bull"|"neutral"|"caution"|"bear"

        # 장중 급락 감지 (5분 주기, kr_scheduler에서 갱신)
        # "normal" | "caution" | "crash" | "severe"
        self._intraday_state: str = "normal"
        self._intraday_kospi_pct: float = 0.0

        # 복합 트레일링용 캐시 (MA5, 전일저가)
        self._ma5_cache: Dict[str, float] = {}
        self._prev_low_cache: Dict[str, float] = {}
        self._composite_cache_date: Optional[date] = None

        # 설정
        self._max_entry_slippage_pct = self._config.get("batch", {}).get(
            "max_entry_slippage_pct", 3.0
        )
        self._max_holding_days = self._config.get("batch", {}).get(
            "max_holding_days", 10
        )

    @staticmethod
    def _safe_strategy_type(strategy_str: Optional[str]) -> StrategyType:
        """문자열을 StrategyType으로 안전하게 변환 (ValueError 방지)"""
        if not strategy_str:
            return StrategyType.SEPA_TREND  # 기본 전략 (momentum_breakout 비활성)
        try:
            return StrategyType(strategy_str)
        except (ValueError, KeyError):
            return StrategyType.SEPA_TREND

    async def _scan_and_build(self, expire_today: bool = False):
        """공통 스캔 로직: 스크리닝 -> 시그널 생성 -> PendingSignal 리스트 반환

        Args:
            expire_today: True -> 오늘 15:30 만료 (아침 스캔)
                          False -> 익영업일 15:30 만료 (전일 마감 후 스캔)
        Returns:
            PendingSignal 리스트 또는 None (오류/후보 없음)
        """
        from .engine import is_kr_market_holiday

        # 스크리너 실행
        candidates = await self._screener.run_full_scan()
        if not candidates:
            logger.info("[배치분석] 후보 종목 없음")
            self._pending = []
            self._save_json()
            return None

        # 전략별 시그널 생성
        rsi2_candidates = [c for c in candidates if c.strategy == "rsi2_reversal"]
        sepa_candidates = [c for c in candidates if c.strategy == "sepa_trend"]

        # -- Phase 3: SEPA 후보에 섹터 모멘텀 점수 주입 --
        # KIS API로 섹터 ETF 20일 수익률 계산 -> candidate.indicators["sector_momentum_score"]
        # 실패 시 무시 (sepa_trend.py에서 change_20d 폴백 사용)
        if sepa_candidates and self._sector_momentum:
            try:
                sm_tasks = [
                    self._sector_momentum.get_sepa_score(c.symbol, getattr(c, "name", ""))
                    for c in sepa_candidates
                ]
                sm_results = await asyncio.gather(*sm_tasks, return_exceptions=True)
                injected = 0
                for candidate, result in zip(sepa_candidates, sm_results):
                    if isinstance(result, Exception) or result is None:
                        continue
                    candidate.indicators["sector_momentum_score"] = float(result)
                    injected += 1
                if injected:
                    logger.info(f"[배치분석] 섹터 모멘텀 점수 주입: {injected}/{len(sepa_candidates)}개")
            except Exception as _sm_e:
                logger.debug(f"[배치분석] 섹터 모멘텀 주입 실패 (폴백 사용): {_sm_e}")

        rsi2_signals = await self._rsi2.generate_batch_signals(rsi2_candidates)
        sepa_signals = await self._sepa.generate_batch_signals(sepa_candidates)
        strategic_signals = self._generate_strategic_signals(candidates)

        # ── 시장 레짐 감지 및 적용 ───────────────────────────────────────────
        regime = "neutral"
        kospi_info = {}
        if hasattr(self._screener, "get_market_regime"):
            regime = self._screener.get_market_regime()
            kospi_info = self._screener.get_kospi_change()
        self._market_regime = regime

        if regime == "bear":
            # 하락장: SEPA(추세추종) 전면 차단, STRATEGIC_SWING 차단
            # RSI2(역추세)는 허용하되 최소 점수 상향
            bear_sepa_blocked = len(sepa_signals) + len(strategic_signals)
            sepa_signals = []
            strategic_signals = []
            rsi2_signals = [s for s in rsi2_signals if s.score >= 70]  # RSI2 기준 강화
            logger.warning(
                f"[배치분석] 🔴 하락장 감지 "
                f"(KOSPI 5일={kospi_info.get('c5', 0):+.1f}%, "
                f"20일={kospi_info.get('c20', 0):+.1f}%) "
                f"→ SEPA/전략스윙 {bear_sepa_blocked}개 차단, "
                f"RSI2만 허용(score≥70): {len(rsi2_signals)}개"
            )
        elif regime == "caution":
            # 주의장: SEPA 기준 상향 (+10점), STRATEGIC_SWING은 유지
            sepa_min_caution = self._sepa.config.min_score + 10
            sepa_signals = [s for s in sepa_signals if s.score >= sepa_min_caution]
            logger.info(
                f"[배치분석] 🟡 주의장 감지 "
                f"(KOSPI 5일={kospi_info.get('c5', 0):+.1f}%, "
                f"20일={kospi_info.get('c20', 0):+.1f}%) "
                f"→ SEPA 기준 {self._sepa.config.min_score:.0f}→{sepa_min_caution:.0f}pt, "
                f"유지: {len(sepa_signals)}개"
            )
        elif regime == "bull":
            logger.info(
                f"[배치분석] 🟢 상승장 확인 "
                f"(KOSPI 5일={kospi_info.get('c5', 0):+.1f}%, "
                f"20일={kospi_info.get('c20', 0):+.1f}%) → 정상 운영"
            )
        # ────────────────────────────────────────────────────────────────────

        all_signals = rsi2_signals + sepa_signals + strategic_signals

        # 동일 종목 중복 제거 (score 높은 것 우선)
        seen: dict = {}
        for sig in all_signals:
            if sig.symbol not in seen or sig.score > seen[sig.symbol].score:
                seen[sig.symbol] = sig
        all_signals = list(seen.values())

        # LLM 컨텍스트 필터 적용 (daily_bias + regime + LLM 우선순위)
        _llm_ops = self._config.get("llm_ops") or {}
        if _llm_ops.get("batch_llm_filter_enabled", True):
            try:
                all_signals = await self._llm_rank_candidates(all_signals)
            except Exception as _llm_rank_e:
                logger.debug(f"[배치분석] LLM 랭킹 실패 (무시): {_llm_rank_e}")

        # 만료일 계산
        now = datetime.now()
        if expire_today:
            # 오늘 15:30 만료
            expires = datetime.combine(now.date(), time(15, 30, 0))
        else:
            # 익영업일 15:30 만료 (주말/공휴일 건너뜀)
            expires_date = now.date() + timedelta(days=1)
            while is_kr_market_holiday(expires_date) or expires_date.weekday() >= 5:
                expires_date += timedelta(days=1)
            expires = datetime.combine(expires_date, time(15, 30, 0))

        # PendingSignal 변환
        result: List[PendingSignal] = []
        for sig in all_signals:
            entry_price = float(sig.price) if sig.price else 0
            if entry_price <= 0:
                continue

            max_entry = entry_price * (1 + self._max_entry_slippage_pct / 100)

            pending = PendingSignal(
                symbol=sig.symbol,
                name=(sig.metadata or {}).get("candidate_name", sig.symbol),
                strategy=sig.strategy.value,
                side=sig.side.value,
                entry_price=entry_price,
                max_entry_price=max_entry,
                stop_price=float(sig.stop_price) if sig.stop_price else entry_price * 0.95,
                target_price=float(sig.target_price) if sig.target_price else entry_price * 1.10,
                score=sig.score,
                reason=sig.reason,
                created_at=now.isoformat(),
                expires_at=expires.isoformat(),
                atr_pct=float((sig.metadata or {}).get("atr_pct", 0)),
            )
            result.append(pending)

        logger.info(
            f"[배치분석] 스캔 완료: "
            f"RSI2={len(rsi2_signals)}개, SEPA={len(sepa_signals)}개, "
            f"전략스윙={len(strategic_signals)}개 -> "
            f"시그널 {len(result)}개"
        )
        return result, rsi2_signals, sepa_signals, strategic_signals

    async def run_daily_scan(self):
        """[15:40] 전일 마감 후 일일 배치 스캔 (morning_scan_enabled=false 시 사용)"""
        logger.info("[배치분석] ===== 일일 스캔 시작 (15:40) =====")
        try:
            result = await self._scan_and_build(expire_today=False)
            if result is None:
                return
            pending_list, rsi2_signals, sepa_signals, strategic_signals = result

            self._pending = pending_list
            self._save_json()
            logger.info(f"[배치분석] 저장 완료: 대기 시그널 {len(self._pending)}개")
            await self._send_telegram_report()

        except Exception as e:
            logger.error(f"[배치분석] 일일 스캔 오류: {e}")
            import traceback
            logger.error(traceback.format_exc())

    async def run_morning_scan(self):
        """[08:20] 아침 배치 스캔 (전일 종가 + 미국 오버나이트 반영)

        run_daily_scan 대체 버전:
        - 전일 종가 기반 스크리닝 (결과 동일, pykrx/KIS 전일 데이터 사용)
        - 미국 오버나이트 데이터 기반 점수 보정
            - 평균 지수 -2% 이하 -> 전 종목 -15점 (한국 시장 하방 압력)
            - 평균 지수 -1% 이하 -> 전 종목 -7점
            - 평균 지수 +1% 이상 -> 전 종목 +3점 (상승 탄력)
        - 프리장 현재가 조회 후 max_entry_price 초과 종목 사전 제거
        - expires_at = 오늘 15:30 (당일 소비)
        """
        logger.info("[배치분석] ===== 아침 스캔 시작 (08:20) =====")
        try:
            # 1. 공통 스캔 로직 실행
            result = await self._scan_and_build(expire_today=True)
            if result is None:
                return
            pending_list, rsi2_signals, sepa_signals, strategic_signals = result

            # 2. 미국 오버나이트 점수 보정
            us_adj = 0.0
            us_summary = "데이터 없음"
            try:
                from ..data.providers.us_market_data import get_us_market_data
                umd = get_us_market_data()
                overnight = await umd.get_overnight_signal()
                sentiment = overnight.get("sentiment", "neutral")
                indices = overnight.get("indices", {})
                idx_pcts = [v.get("change_pct", 0) for v in indices.values() if isinstance(v, dict)]
                avg_idx = sum(idx_pcts) / len(idx_pcts) if idx_pcts else 0.0

                if avg_idx <= -2.0:
                    us_adj = -15.0
                elif avg_idx <= -1.0:
                    us_adj = -7.0
                elif avg_idx >= 1.0:
                    us_adj = +3.0

                us_summary = (
                    f"{sentiment} (평균 {avg_idx:+.1f}%"
                    + (f", 보정 {us_adj:+.0f}pt" if us_adj != 0 else "")
                    + ")"
                )
                logger.info(f"[아침스캔] 미국 오버나이트: {us_summary}")
            except Exception as e:
                logger.warning(f"[아침스캔] US 오버나이트 조회 실패: {e}")

            # 3. 점수 보정 + 최소 점수 필터링
            min_score = self._config.get("batch", {}).get("min_score", 60.0)
            adjusted: List[PendingSignal] = []
            removed_us: List[str] = []

            from dataclasses import replace
            for sig in pending_list:
                if us_adj != 0:
                    new_score = sig.score + us_adj
                    if new_score < min_score:
                        logger.info(
                            f"[아침스캔] {sig.symbol} US 보정 후 제거: "
                            f"{sig.score:.1f}{us_adj:+.0f}={new_score:.1f} < {min_score}"
                        )
                        removed_us.append(f"{sig.symbol}({new_score:.0f}점)")
                        continue
                    sig = replace(sig, score=new_score)
                adjusted.append(sig)

            # 4. 프리장 현재가 체크 (이미 갭업된 종목 사전 제거)
            removed_gap: List[str] = []
            if self._broker:
                final: List[PendingSignal] = []
                for sig in adjusted:
                    try:
                        quote = await self._broker.get_quote(sig.symbol)
                        cur_price = float(quote.get("price", 0)) if quote else 0.0
                        # 현재가가 진입가보다 명확히 높을 때만 필터 (전일 종가 = 진입가인 경우 차이 없음)
                        if cur_price > 0 and cur_price > sig.max_entry_price:
                            logger.info(
                                f"[아침스캔] {sig.symbol} 갭업 제거: "
                                f"현재가 {cur_price:,.0f} > 최대진입가 {sig.max_entry_price:,.0f}"
                            )
                            removed_gap.append(
                                f"{sig.symbol}({cur_price:,.0f}>{sig.max_entry_price:,.0f})"
                            )
                            continue
                    except Exception as e:
                        logger.debug(f"[아침스캔] {sig.symbol} 현재가 조회 실패: {e}")
                    final.append(sig)
            else:
                final = adjusted

            # 5. 저장
            self._pending = final
            self._save_json()

            # 6. 텔레그램 알림
            # 레짐 이모지
            _regime_emoji = {
                "bull": "🟢", "neutral": "⚪", "caution": "🟡", "bear": "🔴"
            }.get(self._market_regime, "⚪")
            lines = [f"\U0001f305 <b>아침 스캔 완료</b>"]
            lines.append(f"{_regime_emoji} 시장 레짐: <b>{self._market_regime.upper()}</b>")
            if self._market_regime == "bear":
                lines.append("⛔ <b>하락장 — SEPA 시그널 차단, RSI2만 허용</b>")
            elif self._market_regime == "caution":
                lines.append("⚠️ 주의장 — SEPA 기준 상향 적용됨")
            lines.append(f"\U0001f1fa\U0001f1f8 US 오버나이트: {us_summary}")
            lines.append(f"\u2705 최종 시그널: <b>{len(self._pending)}개</b>")
            if removed_us:
                lines.append(f"\U0001f53b US 보정 제거: {', '.join(removed_us)}")
            if removed_gap:
                lines.append(f"\U0001f680 갭업 제거: {', '.join(removed_gap)}")
            lines.append(f"\u23f0 09:01 시그널 실행 예정 (만료: 오늘 15:30)")

            for sig in self._pending:
                lines.append(
                    f"  \u2022 {sig.name}({sig.symbol}) "
                    f"점수={sig.score:.0f} 진입={sig.entry_price:,.0f}원"
                )

            try:
                from ..utils.telegram import send_alert
                await send_alert("\n".join(lines))
            except Exception:
                pass

            logger.info(
                f"[아침스캔] 완료: {len(self._pending)}개 시그널 "
                f"(US보정제거={len(removed_us)}, 갭업제거={len(removed_gap)})"
            )

        except Exception as e:
            logger.error(f"[배치분석] 아침 스캔 오류: {e}")
            import traceback
            logger.error(traceback.format_exc())

    async def run_evening_scan(self):
        """[19:30] 넥스트장 반영 2차 스캔 (시간외 단일가 보정)

        15:40 1차 스캔 결과를 넥스트장 데이터로 보정합니다.
        - 거래량 우선, 가격 방향성 보조 (Gemini 권고)
        - ovtm_vol_ratio >= 1% (정규장 대비) + 가격 상승 -> 스코어 +10
        - 가격 -3% 이하 하락 -> 스코어 -15
        - +8% 이상 갭업 -> 제거 (다음날 고점 위험)
        - 넥스트장 신규 등장 종목 추가
        """
        logger.info("[저녁스캔] ===== 넥스트장 2차 스캔 시작 =====")
        try:
            # 기존 시그널 로드
            pending = self._load_json()
            if not pending:
                logger.info("[저녁스캔] 기존 시그널 없음, 1차 스캔을 먼저 실행하세요")
                return

            # 설정값 (config["batch"]["evening_scan"] 경로)
            batch_cfg = self._config.get("batch", {})
            evening_cfg = batch_cfg.get("evening_scan", {})
            ovtm_vol_threshold = evening_cfg.get("ovtm_vol_threshold", 0.01)   # 정규장 대비 1%
            price_bonus_pct = evening_cfg.get("price_bonus_pct", 2.0)          # +2% 이상 -> 보너스
            price_penalty_pct = evening_cfg.get("price_penalty_pct", -3.0)     # -3% 이하 -> 패널티
            gap_remove_pct = evening_cfg.get("gap_remove_pct", 8.0)            # +8% 이상 -> 제거
            score_bonus = evening_cfg.get("score_bonus", 10.0)
            score_penalty = -abs(evening_cfg.get("score_penalty", 15.0))
            min_score_after = evening_cfg.get("min_score_after", 60.0)

            updated = []
            removed_symbols = []
            bonus_symbols = []
            penalty_symbols = []

            for sig in pending:
                try:
                    quote = await self._broker.get_quote(sig.symbol)
                    if not quote:
                        updated.append(sig)
                        continue

                    close_price = quote.get("price", 0)
                    ovtm_price = quote.get("ovtm_price", 0)
                    ovtm_vol = quote.get("ovtm_vol", 0)
                    reg_vol = quote.get("volume", 1) or 1  # 정규장 거래량

                    # 시간외 데이터 없으면 그대로 유지
                    if ovtm_price <= 0 or ovtm_vol == 0:
                        updated.append(sig)
                        continue

                    # 지표 계산
                    ovtm_chg_pct = ((ovtm_price - close_price) / close_price * 100) if close_price > 0 else 0
                    ovtm_vol_ratio = ovtm_vol / reg_vol

                    # 1) 과도한 갭업 -> 제거 (Gemini: 다음날 고점 위험)
                    if ovtm_chg_pct >= gap_remove_pct:
                        logger.info(
                            f"[저녁스캔] {sig.symbol} 제거: 넥스트장 갭업 {ovtm_chg_pct:+.1f}% "
                            f"(>={gap_remove_pct}% 기준)"
                        )
                        removed_symbols.append(f"{sig.symbol}({ovtm_chg_pct:+.1f}%)")
                        continue

                    # 2) 스코어 보정
                    adj = 0.0
                    has_vol = ovtm_vol_ratio >= ovtm_vol_threshold

                    if has_vol and ovtm_chg_pct >= price_bonus_pct:
                        # 거래량 있고 가격도 상승 -> 보너스 (Gemini: 거래량이 먼저)
                        adj = score_bonus
                        bonus_symbols.append(f"{sig.symbol}(+{adj:.0f}pt, {ovtm_chg_pct:+.1f}%)")
                    elif ovtm_chg_pct <= price_penalty_pct:
                        # 가격 하락 -> 패널티
                        adj = score_penalty
                        penalty_symbols.append(f"{sig.symbol}({adj:+.0f}pt, {ovtm_chg_pct:+.1f}%)")

                    new_score = sig.score + adj

                    # 최소 점수 미달 -> 제거
                    if new_score < min_score_after:
                        logger.info(
                            f"[저녁스캔] {sig.symbol} 제거: 보정 후 점수 {new_score:.1f} "
                            f"< 최소 {min_score_after}"
                        )
                        removed_symbols.append(f"{sig.symbol}(score {new_score:.0f})")
                        continue

                    # 업데이트된 시그널 저장
                    from dataclasses import replace
                    updated_sig = replace(
                        sig,
                        score=new_score,
                        ovtm_price_chg_pct=round(ovtm_chg_pct, 2),
                        ovtm_vol_ratio=round(ovtm_vol_ratio, 4),
                        evening_score_adj=adj,
                    )
                    updated.append(updated_sig)

                except Exception as e:
                    logger.warning(f"[저녁스캔] {sig.symbol} 보정 오류: {e}")
                    updated.append(sig)

            # 결과 저장
            self._pending = updated
            self._save_json()

            # 텔레그램 요약 알림
            lines = ["\U0001f4ca <b>저녁 스캔 완료 (넥스트장 반영)</b>"]
            lines.append(f"\u2705 최종 시그널: <b>{len(updated)}개</b>")
            if bonus_symbols:
                lines.append(f"\U0001f4c8 보너스 (+{score_bonus:.0f}pt): {', '.join(bonus_symbols)}")
            if penalty_symbols:
                lines.append(f"\U0001f4c9 패널티: {', '.join(penalty_symbols)}")
            if removed_symbols:
                lines.append(f"\U0001f6ab 제거: {', '.join(removed_symbols)}")
            lines.append("-> 내일 09:01 시그널 실행 예정")

            try:
                from ..utils.telegram import send_alert
                await send_alert("\n".join(lines))
            except Exception:
                pass

            logger.info(
                f"[저녁스캔] 완료: {len(updated)}개 유지, "
                f"보너스 {len(bonus_symbols)}개, "
                f"패널티 {len(penalty_symbols)}개, "
                f"제거 {len(removed_symbols)}개"
            )

        except Exception as e:
            logger.error(f"[저녁스캔] 오류: {e}")
            import traceback
            logger.error(traceback.format_exc())

    async def _premarket_revalidate(
        self, signals: list, nxt_symbols: list
    ) -> list:
        """프리장 가격 기반 시그널 재검증 (NXT 대상 종목만)

        08:20 아침 스캔 이후 프리장(08:00~08:50)에서 가격이 크게 변한 종목의
        시그널을 전략별 기준으로 필터링합니다.
        """
        if not nxt_symbols:
            return signals

        nxt_set = {s.zfill(6) for s in nxt_symbols}
        pre_cfg = self._config.get("batch", {}).get("premarket_revalidation", {})
        rsi2_bounce_pct = pre_cfg.get("rsi2_bounce_cancel_pct", 3.0)
        gap_down_cancel_pct = pre_cfg.get("gap_down_cancel_pct", -5.0)
        rr_min = pre_cfg.get("min_rr_ratio", 1.3)

        validated = []
        for sig in signals:
            sym6 = sig.symbol.zfill(6)
            if sym6 not in nxt_set:
                validated.append(sig)
                continue

            try:
                quote = await self._broker.get_quote(sig.symbol)
                if not quote:
                    validated.append(sig)
                    continue

                # 프리장 시간외 가격 → 없으면 정규장 현재가 사용
                pre_price = float(quote.get("ovtm_price", 0) or 0)
                if pre_price <= 0:
                    pre_price = float(quote.get("price", 0) or 0)
                if pre_price <= 0 or sig.entry_price <= 0:
                    validated.append(sig)
                    continue

                chg_pct = (pre_price - sig.entry_price) / sig.entry_price * 100

                # 1) 공통: 프리장 급락 → 악재 의심, 시그널 취소
                if chg_pct <= gap_down_cancel_pct:
                    logger.info(
                        f"[프리장검증] {sig.symbol} {sig.name} 취소: "
                        f"프리장 {chg_pct:+.1f}% 급락 (기준 {gap_down_cancel_pct}%)"
                    )
                    continue

                # 2) RSI-2 역추세: 프리장에서 이미 반등 → 역추세 진입 의미 상실
                if sig.strategy == "rsi2_reversal" and chg_pct >= rsi2_bounce_pct:
                    logger.info(
                        f"[프리장검증] {sig.symbol} {sig.name} RSI-2 취소: "
                        f"프리장 이미 +{chg_pct:.1f}% 반등 (기준 +{rsi2_bounce_pct}%)"
                    )
                    continue

                # 3) SEPA 등 추세전략: 프리장 가격 기준 R/R 재계산
                if sig.strategy in ("sepa_trend", "core_holding") and chg_pct > 0:
                    remaining_upside = (
                        (sig.target_price - pre_price) / pre_price * 100
                        if pre_price > 0 else 0
                    )
                    downside = abs(
                        (pre_price - sig.stop_price) / pre_price * 100
                    ) if pre_price > 0 else 1
                    rr = remaining_upside / downside if downside > 0 else 99
                    if rr < rr_min:
                        logger.info(
                            f"[프리장검증] {sig.symbol} {sig.name} R/R 미달 취소: "
                            f"프리장 {chg_pct:+.1f}% → R/R={rr:.2f} (기준 {rr_min})"
                        )
                        continue

                validated.append(sig)
            except Exception as e:
                logger.warning(f"[프리장검증] {sig.symbol} 조회 실패 (통과): {e}")
                validated.append(sig)

        cancelled = len(signals) - len(validated)
        if cancelled > 0:
            logger.info(
                f"[프리장검증] NXT 프리장 재검증 완료: "
                f"{len(signals)}개 중 {cancelled}개 취소, {len(validated)}개 유효"
            )
        return validated

    async def execute_pending_signals(self):
        """[09:01] 대기 시그널 실행 (분산 실행: signal_interval_sec 간격)"""
        logger.info("[배치분석] ===== 대기 시그널 실행 =====")

        # 포트폴리오 가드: 재시작 직후 포지션 미로드 대비
        if not self._engine.portfolio.positions and self._broker:
            try:
                loaded_positions = await self._broker.get_positions()
                if loaded_positions:
                    for sym, pos in loaded_positions.items():
                        self._engine.portfolio.positions[sym] = pos
                    logger.info(
                        f"[배치분석] 포트폴리오 가드: {len(loaded_positions)}개 포지션 복구"
                    )
            except Exception as e:
                logger.warning(f"[배치분석] 포트폴리오 가드 포지션 조회 실패: {e}")

        signals = self._load_json()
        if not signals:
            logger.info("[배치분석] 대기 시그널 없음")
            return

        # ── 프리장 가격 기반 시그널 재검증 (NXT 대상 종목만) ──
        # 08:20 아침 스캔 후 40분간 프리장 가격 변동 반영
        try:
            nxt_symbols = await self._broker.get_nxt_symbols()
            signals = await self._premarket_revalidate(signals, nxt_symbols)
            if not signals:
                logger.info("[배치분석] 프리장 재검증 후 유효 시그널 없음")
                return
        except Exception as e:
            logger.warning(f"[배치분석] 프리장 재검증 실패 (원본 유지): {e}")

        # ── 장중 급락 게이트 ───────────────────────────────────────────────
        # severe: 신규 진입 전면 차단
        if self._intraday_state == "severe":
            logger.warning(
                f"[장중급락] 🆘 severe 상태 (KOSPI {self._intraday_kospi_pct:+.2f}%) "
                f"→ 대기 시그널 {len(signals)}개 전면 차단"
            )
            _reason = f"장중 폭락 severe (KOSPI {self._intraday_kospi_pct:+.2f}%)"
            for _s in signals:
                asyncio.create_task(_SigLog.get().log(
                    symbol=_s.symbol, name=getattr(_s, "name", ""),
                    strategy=getattr(_s, "strategy", ""),
                    score=float(getattr(_s, "score", 0)),
                    side="buy", event_type="blocked",
                    block_gate="G_intraday", block_reason=_reason,
                    market_regime=self._market_regime,
                    metadata={"intraday_state": "severe",
                              "kospi_pct": round(self._intraday_kospi_pct, 2)},
                ))
            return
        # caution/crash: 최소 점수 상향 (종목 루프에서 개별 적용)
        _intraday_score_boost = {"normal": 0, "caution": 5, "crash": 10}.get(
            self._intraday_state, 0
        )
        if _intraday_score_boost > 0:
            logger.warning(
                f"[장중급락] {'🟡' if self._intraday_state=='caution' else '🔴'} "
                f"{self._intraday_state} 상태 (KOSPI {self._intraday_kospi_pct:+.2f}%) "
                f"→ 진입 최소점수 +{_intraday_score_boost}pt 상향, "
                f"{'SEPA 차단' if self._intraday_state=='crash' else 'SEPA 점수 강화'}"
            )
        # ─────────────────────────────────────────────────────────────────

        # 슬라이딩 윈도우 설정: 시그널 간 간격으로 슬리피지 위험 분산
        batch_cfg = self._config.get("batch", {})
        signal_interval_sec = batch_cfg.get("signal_interval_sec", 30)

        executed = 0
        skipped = 0
        valid_idx = 0  # 유효 시그널 실행 순번 (간격 적용용)

        for sig in signals:
            try:
                if sig.is_expired():
                    logger.debug(f"[배치분석] {sig.symbol} 만료됨")
                    skipped += 1
                    continue

                # 현재가 조회
                quote = await self._broker.get_quote(sig.symbol)
                if not quote:
                    logger.warning(f"[배치분석] {sig.symbol} 현재가 조회 실패")
                    skipped += 1
                    continue

                current_price = float(quote.get("price", 0))
                if current_price <= 0:
                    skipped += 1
                    continue

                # 진입 범위 체크 (상단: 갭업 슬리피지)
                if current_price > sig.max_entry_price:
                    logger.info(
                        f"[배치분석] {sig.symbol} 진입 스킵: "
                        f"현재가 {current_price:,.0f} > 최대진입가 {sig.max_entry_price:,.0f}"
                    )
                    skipped += 1
                    continue

                # 갭다운/갭업 체크
                gap_pct = 0.0
                if sig.entry_price > 0:
                    gap_pct = (current_price - sig.entry_price) / sig.entry_price * 100

                    # 갭다운 스킵 (개장 급락 시 당일 추가 하락 위험)
                    gap_down_threshold = self._config.get("batch", {}).get(
                        "gap_down_skip_pct", -2.0
                    )
                    if gap_pct < gap_down_threshold:
                        logger.info(
                            f"[배치분석] {sig.symbol} 갭다운 스킵: "
                            f"{gap_pct:+.1f}% (기준 {gap_down_threshold:+.1f}%) "
                            f"전일종가={sig.entry_price:,.0f} 현재={current_price:,.0f}"
                        )
                        skipped += 1
                        continue

                    # ── P0-6: 갭업 시 점수 재검증 ──
                    # 갭업이 클수록 목표까지 남은 upside가 줄어들어 실질 R/R 저하.
                    # 갭업 정도에 비례해 더 높은 품질 점수를 요구.
                    if gap_pct > 1.0:
                        _batch_min = self._config.get(sig.strategy, {}).get("min_score", 55.0)
                        if gap_pct > 2.5:
                            _gap_extra = 10.0   # 2.5%+ 갭업 → +10pt 추가 요구
                        else:
                            _gap_extra = 5.0    # 1.0%+ 갭업 → +5pt 추가 요구
                        if sig.score < _batch_min + _gap_extra:
                            logger.info(
                                f"[배치분석] {sig.symbol} 갭업 점수 미달 스킵: "
                                f"갭={gap_pct:+.1f}% 점수={sig.score:.0f} "
                                f"(필요={_batch_min + _gap_extra:.0f})"
                            )
                            skipped += 1
                            continue

                # ── 장중 급락 개별 종목 게이트 ────────────────────────────
                if _intraday_score_boost > 0:
                    _i_reason = None
                    # crash 상태: SEPA 전략 차단 (역추세 RSI2만 허용)
                    if self._intraday_state == "crash" and sig.strategy == "sepa_trend":
                        _i_reason = (
                            f"장중 급락 crash (KOSPI {self._intraday_kospi_pct:+.2f}%) "
                            "— SEPA 전략 차단"
                        )
                        logger.info(
                            f"[장중급락] {sig.symbol} SEPA 차단 (crash 상태): "
                            f"score={sig.score:.0f}"
                        )
                    else:
                        # 최소 점수 검증 (+boost)
                        _intraday_min = self._config.get(sig.strategy, {}).get("min_score", 55.0)
                        _required = _intraday_min + _intraday_score_boost
                        if sig.score < _required:
                            _i_reason = (
                                f"장중 급락 {self._intraday_state} "
                                f"(KOSPI {self._intraday_kospi_pct:+.2f}%) "
                                f"— 점수 미달 {sig.score:.0f}/{_required:.0f}"
                            )
                            logger.info(
                                f"[장중급락] {sig.symbol} 점수 미달 스킵: "
                                f"{sig.score:.0f} < {_required:.0f} "
                                f"(base={_intraday_min:.0f} + boost={_intraday_score_boost})"
                            )
                    if _i_reason:
                        asyncio.create_task(_SigLog.get().log(
                            symbol=sig.symbol, name=getattr(sig, "name", ""),
                            strategy=getattr(sig, "strategy", ""),
                            score=float(getattr(sig, "score", 0)),
                            side="buy", event_type="blocked",
                            block_gate="G_intraday", block_reason=_i_reason,
                            market_regime=self._market_regime,
                            metadata={"intraday_state": self._intraday_state,
                                      "kospi_pct": round(self._intraday_kospi_pct, 2)},
                        ))
                        skipped += 1
                        continue
                # ──────────────────────────────────────────────────────

                # 이미 보유 중인 종목 스킵
                if sig.symbol in self._engine.portfolio.positions:
                    logger.info(f"[배치분석] {sig.symbol} 이미 보유 중, 스킵")
                    skipped += 1
                    continue

                # 전략별 최대 동시 포지션 수 제한 (config 기반 — 하드코딩 제거)
                _batch_cfg = self._config.get("batch", {})
                _cfg_limits = _batch_cfg.get("strategy_limits", {})
                _default_strategy_limits = {
                    "sepa_trend": 5,      # 23M 자본 기준 현금이 실질 한도
                    "rsi2_reversal": 3,   # 과매도 반전 — 동시 보유 상한
                }
                _default_strategy_limits.update(_cfg_limits)   # config 우선
                default_limit = _batch_cfg.get("default_strategy_limit", 3)
                strategy_count = sum(
                    1 for p in self._engine.portfolio.positions.values()
                    if p.strategy == sig.strategy
                )
                max_for_strategy = _default_strategy_limits.get(sig.strategy, default_limit)
                if strategy_count >= max_for_strategy:
                    logger.info(
                        f"[배치분석] {sig.symbol} 전략 한도 초과: "
                        f"{sig.strategy} {strategy_count}/{max_for_strategy}개, 스킵"
                    )
                    skipped += 1
                    continue

                # ── P0-5: 섹터 조회 → can_open_position 섹터 제한에 반영 ──
                # 기존: sector 파라미터 미전달 → engine.can_open_position 섹터 제한 완전 무효
                # 수정: SectorMomentumProvider로 섹터 조회 → 시그널 메타데이터에 포함
                _sector = None
                if self._sector_momentum:
                    try:
                        _sector = await self._sector_momentum.get_sector(
                            sig.symbol, getattr(sig, "name", "") or ""
                        )
                    except Exception:
                        pass  # 섹터 조회 실패 시 섹터 제한 없이 진행

                # 기존 이벤트 시스템으로 Signal 발행
                try:
                    strategy_type = StrategyType(sig.strategy)
                except (ValueError, KeyError):
                    strategy_type = StrategyType.SEPA_TREND  # momentum_breakout 비활성

                # 레짐별 시그널 강도/손절 조정
                _regime = self._market_regime
                _strength = SignalStrength.STRONG
                _stop = Decimal(str(sig.stop_price))

                if _regime == "bear":
                    # 하락장: 포지션 축소 (STRONG→NORMAL), 손절 타이트
                    _strength = SignalStrength.NORMAL
                    _tight_stop = float(current_price) * 0.965   # -3.5%
                    _stop = Decimal(str(max(float(_stop), _tight_stop)))  # 더 타이트한 쪽
                    logger.info(f"[배치분석] {sig.symbol} 하락장 조정: 강도=NORMAL, 손절 타이트")
                elif _regime == "caution":
                    # 주의장: STRONG 유지, 손절 소폭 타이트
                    _tight_stop = float(current_price) * 0.975   # -2.5%
                    _stop = Decimal(str(max(float(_stop), _tight_stop)))

                # 장중 급락 시 손절 추가 강화 (레짐 조정과 누적 적용)
                if self._intraday_state == "caution":
                    _tight_stop = float(current_price) * 0.970   # -3.0%
                    _stop = Decimal(str(max(float(_stop), _tight_stop)))
                    _strength = SignalStrength.NORMAL
                elif self._intraday_state == "crash":
                    _tight_stop = float(current_price) * 0.975   # -2.5%
                    _stop = Decimal(str(max(float(_stop), _tight_stop)))
                    _strength = SignalStrength.NORMAL

                signal = Signal(
                    symbol=sig.symbol,
                    side=OrderSide.BUY,
                    strength=_strength,
                    strategy=strategy_type,
                    price=Decimal(str(current_price)),
                    target_price=Decimal(str(sig.target_price)),
                    stop_price=_stop,
                    score=sig.score,
                    confidence=sig.score / 100.0,
                    reason=sig.reason,
                    metadata={
                        "batch_signal": True,
                        "name": sig.name,
                        "atr_pct": sig.atr_pct,
                        "position_multiplier": atr_position_multiplier(sig.atr_pct) if sig.atr_pct > 0 else 1.0,
                        "market_regime": _regime,
                        "intraday_state": self._intraday_state,
                        "intraday_kospi_pct": round(self._intraday_kospi_pct, 2),
                        "sector": _sector,
                        "gap_pct": round(gap_pct, 2),
                    },
                )

                # 종목명 캐시에 저장 (매수 시그널/주문 이벤트에 종목명 표시)
                name_cache = getattr(self._engine, '_stock_name_cache', None)
                if name_cache is not None and sig.name and sig.name != sig.symbol:
                    name_cache[sig.symbol] = sig.name

                # 슬라이딩 윈도우: 첫 시그널은 즉시, 이후 signal_interval_sec 간격
                if valid_idx > 0 and signal_interval_sec > 0:
                    logger.info(
                        f"[배치분석] 시그널 분산: {signal_interval_sec}초 대기 "
                        f"({valid_idx + 1}/{len(signals)})"
                    )
                    await asyncio.sleep(signal_interval_sec)

                event = SignalEvent.from_signal(signal, source="batch_analyzer")
                await self._engine.emit(event)
                executed += 1
                valid_idx += 1

                logger.info(
                    f"[배치분석] {sig.symbol} {sig.name} 시그널 발행: "
                    f"현재가={current_price:,.0f} 전략={sig.strategy} 점수={sig.score:.0f}"
                )

                # Rate limit
                await asyncio.sleep(0.5)

            except Exception as e:
                logger.error(f"[배치분석] {sig.symbol} 실행 오류: {e}")
                skipped += 1

        logger.info(f"[배치분석] 실행 완료: 발행={executed}개, 스킵={skipped}개")

        # 실행 완료 후 시그널 파일 비우기 (재시작 시 중복 방지)
        self._pending = []
        self._save_json()

    async def _refresh_composite_cache(self):
        """복합 트레일링용 MA5/전일저가 캐시 갱신 (일 1회)"""
        today = date.today()
        if self._composite_cache_date == today:
            return

        # 날짜 변경 시 이전 캐시 정리 (메모리 누수 방지)
        self._ma5_cache.clear()
        self._prev_low_cache.clear()

        symbols = list(self._engine.portfolio.positions.keys())
        if not symbols:
            return

        try:
            from pykrx import stock as pykrx_stock

            end_date = today.strftime("%Y%m%d")
            start_date = (today - timedelta(days=20)).strftime("%Y%m%d")

            for symbol in symbols:
                try:
                    padded = symbol.zfill(6)
                    df = await asyncio.get_running_loop().run_in_executor(
                        None,
                        pykrx_stock.get_market_ohlcv_by_date,
                        start_date, end_date, padded,
                    )
                    if df is None or df.empty or len(df) < 2:
                        continue

                    prev_low = float(df["저가"].iloc[-2])
                    if prev_low > 0:
                        self._prev_low_cache[symbol] = prev_low

                    if len(df) >= 5:
                        ma5 = float(df["종가"].iloc[-5:].mean())
                        if ma5 > 0:
                            self._ma5_cache[symbol] = ma5
                except Exception as e:
                    logger.debug(f"[포지션모니터] {symbol} 복합캐시 실패: {e}")

                await asyncio.sleep(0.1)

            self._composite_cache_date = today
            logger.info(
                f"[포지션모니터] 복합캐시 갱신: MA5 {len(self._ma5_cache)}종목, "
                f"전일저가 {len(self._prev_low_cache)}종목"
            )
        except Exception as e:
            logger.warning(f"[포지션모니터] 복합캐시 갱신 실패 (무시): {e}")

    async def _fill_composite_single(self, symbol: str):
        """단일 종목 MA5/전일저가 즉시 갱신 (장중 신규 매수 종목용)"""
        try:
            from pykrx import stock as pykrx_stock
            today = date.today()
            end_date = today.strftime("%Y%m%d")
            start_date = (today - timedelta(days=20)).strftime("%Y%m%d")
            padded = symbol.zfill(6)
            df = await asyncio.get_running_loop().run_in_executor(
                None, pykrx_stock.get_market_ohlcv_by_date,
                start_date, end_date, padded,
            )
            if df is None or df.empty or len(df) < 2:
                self._ma5_cache.setdefault(symbol, None)
                logger.debug(f"[포지션모니터] {symbol} 복합캐시 데이터 없음 (재시도 방지)")
                return
            prev_low = float(df["저가"].iloc[-2])
            if prev_low > 0:
                self._prev_low_cache[symbol] = prev_low
            if len(df) >= 5:
                ma5 = float(df["종가"].iloc[-5:].mean())
                if ma5 > 0:
                    self._ma5_cache[symbol] = ma5
            if symbol not in self._ma5_cache:
                self._ma5_cache[symbol] = None
            logger.info(f"[포지션모니터] {symbol} 복합캐시 즉시 갱신 완료")
        except Exception as e:
            self._ma5_cache.setdefault(symbol, None)
            logger.debug(f"[포지션모니터] {symbol} 복합캐시 즉시 갱신 실패 (재시도 방지): {e}")

    async def update_intraday_state(self, kospi_pct: float) -> str:
        """KOSPI 당일 등락률 기반 장중 급락 상태 업데이트.

        kr_scheduler가 5분 주기로 호출.
        상태 변화 시 ExitManager에 SL/TS 즉시 반영.

        Args:
            kospi_pct: KOSPI 당일 등락률 (%) — fetch_index_price()의 change_pct

        Returns:
            새 상태 문자열
        """
        # 상태 결정
        if kospi_pct <= -3.5:
            new_state = "severe"
        elif kospi_pct <= -2.5:
            new_state = "crash"
        elif kospi_pct <= -1.5:
            new_state = "caution"
        else:
            new_state = "normal"

        prev_state = self._intraday_state
        self._intraday_state = new_state
        self._intraday_kospi_pct = kospi_pct

        if new_state != prev_state:
            if new_state == "normal":
                # 급락 해제 → 레짐 파라미터 복원
                if self._exit_manager:
                    self._exit_manager.recover_from_intraday_crash()
                logger.info(
                    f"[장중급락] 해제: KOSPI {kospi_pct:+.2f}% "
                    f"({prev_state} → normal) — 레짐 파라미터 복원"
                )
            else:
                # 급락 심화 또는 신규 진입
                if self._exit_manager:
                    self._exit_manager.apply_intraday_crash_params(new_state)
                emoji = {"caution": "🟡", "crash": "🔴", "severe": "🆘"}.get(new_state, "⚠️")
                level_desc = {
                    "caution": f"주의장 (KOSPI {kospi_pct:+.2f}%)",
                    "crash":   f"급락 (KOSPI {kospi_pct:+.2f}%)",
                    "severe":  f"폭락 (KOSPI {kospi_pct:+.2f}%) — 신규 진입 전면 차단",
                }.get(new_state, "")
                logger.warning(
                    f"[장중급락] {emoji} {prev_state} → {new_state}: {level_desc}"
                )
        return new_state

    async def monitor_positions(self):
        """[매 30분] 보유 포지션 시세 갱신 + 청산 체크"""
        if not self._engine.portfolio.positions:
            return

        logger.debug(f"[포지션모니터] {len(self._engine.portfolio.positions)}개 포지션 체크")

        # 복합 트레일링 캐시 갱신 (일 1회)
        await self._refresh_composite_cache()

        # 레짐 기반 ExitManager 파라미터 동기화
        # → 구체적인 조정은 kr_scheduler._apply_regime_to_exit_manager() + REGIME_EXIT_PARAMS 에서 처리.
        # 여기서는 LLM 레짐 캐시를 읽어 ExitManager에 위임 (30분 주기 monitor와 동기화).
        if self._exit_manager:
            try:
                import json
                from pathlib import Path
                from datetime import date as _date
                from ..strategies.exit_manager import REGIME_EXIT_PARAMS
                _regime_path = Path.home() / ".cache" / "ai_trader" / "llm_regime_today.json"
                if _regime_path.exists():
                    _rd = json.loads(_regime_path.read_text(encoding="utf-8"))
                    if _rd.get("date") == _date.today().isoformat():
                        _llm_regime = _rd.get("regime", "neutral")
                        if _llm_regime in REGIME_EXIT_PARAMS:
                            self._exit_manager.apply_regime_params(_llm_regime)
            except Exception as _e:
                logger.debug(f"[포지션모니터] 레짐 동기화 오류 (무시): {_e}")

        _exited_symbols: set = set()  # 이번 루프에서 청산 신호 발행된 종목 (중복 방지)
        for symbol, pos in list(self._engine.portfolio.positions.items()):
            try:
                # REST API 현재가 조회
                quote = await self._broker.get_quote(symbol)
                if not quote:
                    continue

                current_price = Decimal(str(quote.get("price", 0)))
                if current_price <= 0:
                    continue

                # 포지션 가격 갱신
                pos.current_price = current_price
                if pos.highest_price is None or current_price > pos.highest_price:
                    pos.highest_price = current_price

                # RSI2 전략 전용: RSI(2) > 70 청산 (FDR 일봉 기반, 30분마다 체크)
                # ScreenedStock(장중 캐시)에는 RSI(2) 없으므로 여기서 정확하게 계산
                if pos.strategy == "rsi2_reversal":
                    try:
                        rsi2_val = await asyncio.get_running_loop().run_in_executor(
                            None, self._calc_rsi2_from_fdr, symbol
                        )
                        if rsi2_val is not None and rsi2_val > 70:
                            _r2_reason = f"RSI2 청산: RSI(2)={rsi2_val:.1f} > 70 (반등 목표 도달)"
                            logger.info(f"[포지션모니터] {symbol} {_r2_reason}")
                            signal = Signal(
                                symbol=symbol,
                                side=OrderSide.SELL,
                                strength=SignalStrength.STRONG,
                                strategy=StrategyType.RSI2_REVERSAL,
                                price=current_price,
                                score=100,
                                confidence=1.0,
                                reason=_r2_reason,
                            )
                            event = SignalEvent.from_signal(signal, source="rsi2_monitor")
                            _rm = getattr(self._engine, 'risk_manager', None)
                            if _rm and hasattr(_rm, '_pending_exit_reasons'):
                                _rm._pending_exit_reasons[symbol] = _r2_reason
                            await self._engine.emit(event)
                            await asyncio.sleep(0.2)
                            continue
                    except Exception as _rsi2e:
                        logger.debug(f"[포지션모니터] {symbol} RSI2 체크 실패 (무시): {_rsi2e}")

                # ExitManager 청산 체크 (복합 트레일링 데이터 포함)
                if self._exit_manager:
                    # 캐시 미스 종목 즉시 갱신 (장중 신규 매수)
                    if symbol not in self._ma5_cache and self._composite_cache_date is not None:
                        await self._fill_composite_single(symbol)
                    _md = None
                    if self._ma5_cache or self._prev_low_cache:
                        _md = {
                            "ma5": self._ma5_cache.get(symbol),
                            "prev_low": self._prev_low_cache.get(symbol),
                            "high": float(quote.get("high", 0)) if quote.get("high") else None,
                            "low": float(quote.get("low", 0)) if quote.get("low") else None,
                        }
                    exit_result = self._exit_manager.update_price(symbol, current_price, market_data=_md)
                    if exit_result:
                        action, qty, reason = exit_result
                        logger.info(f"[포지션모니터] {symbol} 청산 시그널: {reason} ({qty}주)")

                        # 매도 시그널 -> 이벤트 시스템
                        signal = Signal(
                            symbol=symbol,
                            side=OrderSide.SELL,
                            strength=SignalStrength.STRONG,
                            strategy=self._safe_strategy_type(pos.strategy),
                            price=current_price,
                            score=100,
                            confidence=1.0,
                            reason=reason,
                        )
                        event = SignalEvent.from_signal(signal, source="position_monitor")
                        _rm = getattr(self._engine, 'risk_manager', None)
                        if _rm and hasattr(_rm, '_pending_exit_reasons'):
                            _rm._pending_exit_reasons[symbol] = reason
                        await self._engine.emit(event)
                        _exited_symbols.add(symbol)
                        await asyncio.sleep(0.2)  # rate limit
                        continue  # 청산 시그널 발행 시 보유기간 체크 스킵

                # 보유기간 초과 강제 청산 (코어홀딩은 월 리밸런싱으로 관리)
                if pos.strategy == "core_holding":
                    pass  # 코어홀딩: max_holding_days=0 (무제한), ExitManager가 트레일링/손절 관리
                elif pos.entry_time:
                    holding_days = (datetime.now() - pos.entry_time).days
                    if holding_days > self._max_holding_days:
                        logger.info(
                            f"[포지션모니터] {symbol} 보유기간 초과: {holding_days}일 "
                            f"(최대 {self._max_holding_days}일)"
                        )
                        signal = Signal(
                            symbol=symbol,
                            side=OrderSide.SELL,
                            strength=SignalStrength.NORMAL,
                            strategy=self._safe_strategy_type(pos.strategy),
                            price=current_price,
                            score=80,
                            confidence=0.8,
                            reason=f"보유기간 초과: {holding_days}일>{self._max_holding_days}일",
                        )
                        event = SignalEvent.from_signal(signal, source="position_monitor")
                        _rm = getattr(self._engine, 'risk_manager', None)
                        if _rm and hasattr(_rm, '_pending_exit_reasons'):
                            _rm._pending_exit_reasons[symbol] = f"보유기간 초과: {holding_days}일>{self._max_holding_days}일"
                        await self._engine.emit(event)

                await asyncio.sleep(0.2)  # rate limit

            except Exception as e:
                logger.warning(f"[포지션모니터] {symbol} 체크 오류: {e}")

        # 코어홀딩 이벤트 기반 조기 경보 (이미 청산 신호 발행된 종목 제외)
        await self._monitor_core_positions(exclude_symbols=_exited_symbols)

    async def _monitor_core_positions(self, exclude_symbols: set = None):
        """코어홀딩 이벤트 기반 조기 청산 체크

        월 1회 리밸런싱 사이에 급격히 악화된 코어 포지션을 조기에 감지.
        트리거 조건:
          1) 수익률 <= early_loss_alert_pct (-12%)
          2) MA200 이탈 연속 일수 >= early_ma200_alert_days (3일)

        Args:
            exclude_symbols: 이미 청산 신호가 발행된 종목 (중복 방지)
        """
        core_cfg = self._config.get("core_holding", {})
        if not core_cfg.get("enabled", False):
            return

        early_loss_pct = core_cfg.get("early_loss_alert_pct", -12.0)
        early_ma200_days = core_cfg.get("early_ma200_alert_days", 3)

        portfolio = self._engine.portfolio
        pending_sells = getattr(self._engine, '_pending_sells', set())

        _exclude = exclude_symbols or set()
        core_positions = [
            (sym, p) for sym, p in portfolio.positions.items()
            if p.strategy == "core_holding"
            and sym not in pending_sells
            and sym not in _exclude  # ExitManager/보유기간에서 이미 청산 신호 발행된 종목 제외
        ]
        if not core_positions:
            return

        # MA200 이탈 카운터 캐시
        ma200_cache_path = Path.home() / ".cache" / "ai_trader" / "core_ma200_breaks.json"
        ma200_breaks = {}
        try:
            if ma200_cache_path.exists():
                ma200_breaks = json.loads(ma200_cache_path.read_text(encoding="utf-8"))
                # 날짜 롤오버: 오래된 데이터 정리 (7일 초과)
                today_str = date.today().isoformat()
                ma200_breaks = {
                    k: v for k, v in ma200_breaks.items()
                    if isinstance(v, dict) and v.get("updated", "") >= (date.today() - timedelta(days=7)).isoformat()
                }
        except Exception:
            ma200_breaks = {}

        ma200_updated = False

        for symbol, pos in core_positions:
            try:
                triggers = []

                # 1. 수익률 조기 경보
                pnl_pct = float(getattr(pos, "unrealized_pnl_net_pct", 0) or 0)
                if pnl_pct <= early_loss_pct:
                    triggers.append(f"손실경보 {pnl_pct:.1f}% (임계 {early_loss_pct:.1f}%)")

                # 2. MA200 이탈 체크 (일봉 기반)
                try:
                    daily_prices = await self._broker.get_daily_prices(symbol, days=250)
                    if daily_prices is not None and len(daily_prices) >= 200:
                        # MA200 계산: 최근 200일 종가 평균
                        closes = [float(d.get("close", d.get("stck_clpr", 0))) for d in daily_prices[:200]]
                        if all(c > 0 for c in closes):
                            ma200 = sum(closes) / len(closes)
                            current = float(pos.current_price) if pos.current_price is not None else 0
                            if current > 0 and current < ma200:
                                # MA200 이탈 카운터 증가
                                prev = ma200_breaks.get(symbol, {})
                                prev_count = prev.get("count", 0)
                                prev_date = prev.get("updated", "")
                                today_str = date.today().isoformat()
                                if prev_date == today_str:
                                    # 같은 날 이미 업데이트됨 → 유지
                                    break_days = prev_count
                                else:
                                    break_days = prev_count + 1
                                    ma200_breaks[symbol] = {"count": break_days, "updated": today_str}
                                    ma200_updated = True

                                if break_days >= early_ma200_days:
                                    triggers.append(
                                        f"MA200 이탈 {break_days}일 (임계 {early_ma200_days}일, "
                                        f"현재가={current:,.0f} < MA200={ma200:,.0f})"
                                    )
                            else:
                                # MA200 위에 있으면 카운터 리셋
                                if symbol in ma200_breaks:
                                    ma200_breaks[symbol] = {"count": 0, "updated": date.today().isoformat()}
                                    ma200_updated = True
                except Exception as _ma_e:
                    logger.debug(f"[코어조기경보] {symbol} MA200 체크 실패 (무시): {_ma_e}")

                if triggers:
                    trigger_str = " / ".join(triggers)
                    logger.warning(
                        f"[코어조기경보] {symbol} 이벤트 트리거: {trigger_str} → 즉시 매도 시그널"
                    )

                    # 텔레그램 알림
                    try:
                        from ..utils.telegram import send_alert
                        await send_alert(
                            f"🚨 코어홀딩 조기경보\n"
                            f"{getattr(pos, 'name', symbol)}({symbol})\n"
                            f"{trigger_str}"
                        )
                    except Exception as _tg_e:
                        logger.warning(f"[코어조기경보] {symbol} 텔레그램 알림 실패: {_tg_e}")

                    # 매도 시그널 발행
                    current_price = pos.current_price if pos.current_price is not None else Decimal("0")
                    signal = Signal(
                        symbol=symbol,
                        side=OrderSide.SELL,
                        strength=SignalStrength.STRONG,
                        strategy=StrategyType.CORE_HOLDING,
                        price=current_price,
                        score=100,
                        confidence=1.0,
                        reason=f"코어홀딩 조기경보: {trigger_str}",
                        metadata={"is_core": True, "early_alert": True},
                    )
                    event = SignalEvent.from_signal(signal, source="core_early_alert")
                    _rm = getattr(self._engine, 'risk_manager', None)
                    if _rm and hasattr(_rm, '_pending_exit_reasons'):
                        _rm._pending_exit_reasons[symbol] = f"코어홀딩 조기경보: {trigger_str}"
                    await self._engine.emit(event)

            except Exception as e:
                logger.error(f"[코어조기경보] {symbol} 체크 오류: {e}")

        # MA200 카운터 캐시 저장
        if ma200_updated:
            try:
                ma200_cache_path.parent.mkdir(parents=True, exist_ok=True)
                ma200_cache_path.write_text(json.dumps(ma200_breaks, ensure_ascii=False, indent=2), encoding="utf-8")
            except Exception:
                pass

    def _generate_strategic_signals(self, candidates) -> List[Signal]:
        """strategic_swing 시그널 생성: 2계층 이상 복합신호 종목"""
        signals = []
        for c in candidates:
            # 2계층 이상 복합신호 확인 (구조화된 메타데이터 기반)
            layers = c.indicators.get("strategic_layers", 0)
            if layers < 2:
                continue
            if c.score < self._strategic_min_score:
                continue

            entry_price = float(c.entry_price) if c.entry_price else 0
            if entry_price <= 0:
                continue

            signal = Signal(
                symbol=c.symbol,
                side=OrderSide.BUY,
                strength=SignalStrength.STRONG,
                strategy=StrategyType.STRATEGIC_SWING,
                price=c.entry_price,
                target_price=c.target_price,
                stop_price=c.stop_price,
                score=c.score,
                confidence=min(c.score / 100.0, 1.0),
                reason=f"전략적 스윙: {', '.join(c.reasons[:3])}",
                metadata={
                    "candidate_name": c.name,
                    "atr_pct": c.indicators.get("atr_pct", 0),
                    "strategic_layers": sum(
                        1 for r in c.reasons
                        if any(kw in r for kw in ["전문가패널", "수급추세", "VCP"])
                    ),
                },
            )
            signals.append(signal)

        logger.info(f"[배치분석] 전략스윙 시그널 {len(signals)}개 생성")
        return signals

    async def _llm_rank_candidates(self, all_signals: list) -> list:
        """배치 후보에 대해 LLM 컨텍스트 필터 적용

        1. llm_regime_today.json → 전략 우선순위/entry_start_time 적용
        2. daily_bias.json → score boost 적용
        3. 후보 5개 이상 → Gemini Flash 우선순위 재조정
        4. fail-safe: 오류 시 원본 그대로 반환
        """
        if not all_signals:
            return all_signals

        try:
            import json
            from pathlib import Path

            cache_dir = Path.home() / ".cache" / "ai_trader"

            # 1. llm_regime_today.json 로드
            regime_data = {}
            regime_path = cache_dir / "llm_regime_today.json"
            if regime_path.exists():
                try:
                    regime_data = json.loads(regime_path.read_text(encoding="utf-8"))
                    logger.info(f"[배치LLM] 레짐 로드: {regime_data.get('regime', 'unknown')}")
                except Exception:
                    pass

            lead_strategy = regime_data.get("lead_strategy", "balanced")
            entry_start_time = regime_data.get("entry_start_time", "09:01")

            # 2. daily_bias.json 로드
            bias_data = {}
            bias_path = cache_dir / "daily_bias.json"
            if bias_path.exists():
                try:
                    bias_data = json.loads(bias_path.read_text(encoding="utf-8"))
                    logger.info(
                        f"[배치LLM] 바이어스 로드: sepa={bias_data.get('sepa_score_boost', 0):+d}, "
                        f"rsi2={bias_data.get('rsi2_score_boost', 0):+d}"
                    )
                except Exception:
                    pass

            sepa_boost = bias_data.get("sepa_score_boost", 0)
            rsi2_boost = bias_data.get("rsi2_score_boost", 0)
            avoid_before = bias_data.get("avoid_entry_before")

            # 전략별 score 조정 적용
            from dataclasses import replace
            adjusted = []
            for sig in all_signals:
                adj = 0.0
                strategy = sig.strategy.value if hasattr(sig.strategy, 'value') else str(sig.strategy)

                # lead_strategy 기반 조정
                if lead_strategy == "rsi2" and "sepa" in strategy:
                    adj -= 5
                elif lead_strategy == "sepa" and "rsi2" in strategy:
                    adj -= 3

                # daily_bias score boost
                if "sepa" in strategy:
                    adj += sepa_boost
                elif "rsi2" in strategy:
                    adj += rsi2_boost

                new_score = sig.score + adj
                meta = dict(sig.metadata) if sig.metadata else {}

                # entry_start_time 메타데이터 추가
                if entry_start_time != "09:01":
                    meta["entry_delay"] = entry_start_time
                if avoid_before:
                    meta["avoid_entry_before"] = avoid_before

                if adj != 0 or meta != sig.metadata:
                    sig = Signal(
                        symbol=sig.symbol,
                        side=sig.side,
                        strength=sig.strength,
                        strategy=sig.strategy,
                        price=sig.price,
                        target_price=sig.target_price,
                        stop_price=sig.stop_price,
                        score=new_score,
                        confidence=sig.confidence,
                        reason=sig.reason,
                        metadata=meta,
                    )
                adjusted.append(sig)

            all_signals = adjusted

            # 3. 후보 5개 이상일 때만 Gemini Flash LLM 호출
            if len(all_signals) >= 5:
                try:
                    from ..utils.llm import get_llm_manager, LLMTask

                    llm = get_llm_manager()
                    regime_str = regime_data.get("regime", "neutral")
                    reasoning = regime_data.get("reasoning", "")

                    cand_lines = []
                    for i, sig in enumerate(sorted(all_signals, key=lambda s: -s.score)[:20], 1):
                        name = sig.metadata.get("candidate_name", sig.symbol) if sig.metadata else sig.symbol
                        strategy = sig.strategy.value if hasattr(sig.strategy, 'value') else str(sig.strategy)
                        cand_lines.append(
                            f"{i}. {name}({sig.symbol}) {strategy} score={sig.score:.0f}"
                        )
                    candidates_text = "\n".join(cand_lines)

                    prompt = f"""오늘 시장 레짐: {regime_str} ({reasoning})
배치 후보 {len(all_signals)}개:
{candidates_text}

오늘 시장에서 진입 우선순위 top 3과 제외 권장을 JSON으로:
{{"priority_symbols": ["005930", "000660"], "exclude_symbols": ["123456"], "comment": "한 줄 요약"}}"""

                    result = await asyncio.wait_for(
                        llm.complete_json(
                            prompt=prompt,
                            system="한국 주식 배치 후보 우선순위 필터. JSON만 응답.",
                            task=LLMTask.QUICK_ANALYSIS,
                            max_tokens=200,
                        ),
                        timeout=10.0,
                    )

                    if result and isinstance(result, dict):
                        priority = set(result.get("priority_symbols", []))
                        exclude = set(result.get("exclude_symbols", []))
                        comment = result.get("comment", "")

                        # list.index() 대신 리스트 재구성으로 안전하게 수정
                        new_signals = []
                        for sig in all_signals:
                            if sig.symbol in priority:
                                sig = Signal(
                                    symbol=sig.symbol, side=sig.side,
                                    strength=sig.strength, strategy=sig.strategy,
                                    price=sig.price, target_price=sig.target_price,
                                    stop_price=sig.stop_price,
                                    score=sig.score + 3,
                                    confidence=sig.confidence, reason=sig.reason,
                                    metadata=dict(sig.metadata or {}),
                                )
                            elif sig.symbol in exclude:
                                sig = Signal(
                                    symbol=sig.symbol, side=sig.side,
                                    strength=sig.strength, strategy=sig.strategy,
                                    price=sig.price, target_price=sig.target_price,
                                    stop_price=sig.stop_price,
                                    score=sig.score - 8,
                                    confidence=sig.confidence, reason=sig.reason,
                                    metadata=dict(sig.metadata or {}),
                                )
                            new_signals.append(sig)
                        all_signals = new_signals

                        logger.info(
                            f"[배치LLM] LLM 필터 적용: priority={priority}, "
                            f"exclude={exclude}, comment={comment}"
                        )

                except asyncio.TimeoutError:
                    logger.debug("[배치LLM] LLM 타임아웃 → 스킵")
                except Exception as e:
                    logger.debug(f"[배치LLM] LLM 호출 실패 → 스킵: {e}")

            # 점수순 재정렬
            all_signals.sort(key=lambda s: -s.score)
            return all_signals

        except Exception as e:
            logger.warning(f"[배치LLM] 전체 오류 → 원본 반환: {e}")
            return all_signals

    def _save_json(self):
        """대기 시그널 JSON 저장"""
        try:
            data = [p.to_dict() for p in self._pending]
            with open(self._signals_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            logger.debug(f"[배치분석] {len(self._pending)}개 시그널 저장: {self._signals_path}")
        except Exception as e:
            logger.error(f"[배치분석] JSON 저장 실패: {e}")

    def _load_json(self) -> List[PendingSignal]:
        """대기 시그널 JSON 로드"""
        try:
            if not self._signals_path.exists():
                return []
            with open(self._signals_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return [PendingSignal.from_dict(d) for d in data]
        except Exception as e:
            logger.error(f"[배치분석] JSON 로드 실패: {e}")
            return []

    @staticmethod
    def _calc_rsi2_from_fdr(symbol: str) -> Optional[float]:
        """FDR 일봉으로 RSI(2) 계산 (동기 함수 — run_in_executor 전용)

        RSI2 포지션의 반등 목표 도달 여부 체크용.
        Returns None if data unavailable.
        """
        try:
            import FinanceDataReader as fdr
            from datetime import timedelta
            start = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
            df = fdr.DataReader(symbol, start)
            if df is None or len(df) < 5:
                return None
            closes = df["Close"].tolist()
            if len(closes) < 4:
                return None
            # Wilder's RSI(2) — 단순 버전 (충분히 정확)
            period = 2
            gains, losses = [], []
            for i in range(1, len(closes)):
                diff = closes[i] - closes[i - 1]
                gains.append(max(diff, 0))
                losses.append(max(-diff, 0))
            if len(gains) < period:
                return None
            avg_gain = sum(gains[-period:]) / period
            avg_loss = sum(losses[-period:]) / period
            if avg_loss == 0:
                return 100.0
            rs = avg_gain / avg_loss
            rsi = 100 - (100 / (1 + rs))
            return round(rsi, 1)
        except Exception:
            return None

    async def _send_telegram_report(self):
        """스캔 결과 텔레그램 알림"""
        try:
            from ..utils.telegram import send_alert

            if not self._pending:
                await send_alert(
                    "\U0001f50d <b>일일 스윙 스캔 완료</b>\n\n"
                    "후보 종목: <b>0개</b>"
                )
                return

            # 전략별 분류
            strat_names = {
                "sepa_trend": "SEPA",
                "rsi2_reversal": "RSI2",
                "strategic_swing": "전략스윙",
                "momentum_breakout": "모멘텀",
            }
            strat_counts = {}
            for p in self._pending:
                sn = strat_names.get(p.strategy, p.strategy)
                strat_counts[sn] = strat_counts.get(sn, 0) + 1

            strat_summary = " / ".join(f"{k} {v}개" for k, v in strat_counts.items())

            lines = [
                f"\U0001f50d <b>일일 스윙 스캔 완료</b>",
                f"",
                f"후보: <b>{len(self._pending)}개</b> ({strat_summary})",
                f"",
            ]

            strat_emoji = {
                "sepa_trend": "\U0001f7e2",
                "rsi2_reversal": "\U0001f535",
                "strategic_swing": "\U0001f7e3",
                "momentum_breakout": "\U0001f7e0",
            }

            for i, p in enumerate(self._pending[:10], 1):
                emoji = strat_emoji.get(p.strategy, "\u26aa")
                sn = strat_names.get(p.strategy, p.strategy)
                pnl_target = (p.target_price / p.entry_price - 1) * 100 if p.entry_price > 0 else 0
                pnl_stop = (p.stop_price / p.entry_price - 1) * 100 if p.entry_price > 0 else 0
                lines.append(
                    f"{emoji} <b>{p.name}</b> <code>{p.symbol}</code> "
                    f"| {sn} {p.score:.0f}점"
                )
                lines.append(
                    f"    진입 {p.entry_price:,.0f} -> "
                    f"목표 {p.target_price:,.0f}(<b>+{pnl_target:.1f}%</b>) / "
                    f"손절 {p.stop_price:,.0f}({pnl_stop:.1f}%)"
                )
                if p.reason:
                    # reason이 너무 길면 축약
                    reason_display = p.reason if len(p.reason) <= 60 else p.reason[:57] + "..."
                    lines.append(f"    \U0001f4a1 {reason_display}")
                lines.append("")

            if len(self._pending) > 10:
                lines.append(f"<i>... 외 {len(self._pending) - 10}개 종목</i>")

            await send_alert("\n".join(lines))

        except Exception as e:
            logger.warning(f"[배치분석] 텔레그램 알림 실패: {e}")

    # ================================================================
    # 코어홀딩 스캔/리밸런싱
    # ================================================================

    async def execute_core_rebalance(self, allow_replace: bool = True) -> bool:
        """코어홀딩 리밸런싱 실행

        1. 현재 코어 포지션 확인
        2. 스캔 실행 → 후보 생성
        3. 교체 대상 판단 (재스코어 < 55 또는 신규 +15점) — allow_replace=True 시만
        4. 교체 매도 → 신규 매수 시그널 발행

        Args:
            allow_replace: True=교체 허용 (월초 리밸런싱), False=빈슬롯 매수만 (교체 없음)

        Returns:
            True=성공, False=실패 (재시도 필요)
        """
        if self._core_screener is None or self._core_strategy is None:
            return False

        logger.info("[코어홀딩] 리밸런싱 시작...")

        try:
            core_cfg = self._config.get("core_holding", {})
            max_positions = core_cfg.get("max_positions", 3)
            min_score = core_cfg.get("min_score", 70)
            replace_threshold = core_cfg.get("replace_threshold", 15)
            ma200_break_days = core_cfg.get("ma200_break_days", 5)
            rebalance_exclude = set(str(s) for s in core_cfg.get("rebalance_exclude", []))

            # 현재 코어 포지션 확인
            portfolio = self._engine.portfolio
            current_core = {}
            for sym, pos in portfolio.positions.items():
                if pos.strategy == "core_holding":
                    current_core[sym] = pos

            logger.info(f"[코어홀딩] 현재 코어 포지션: {len(current_core)}개")
            for sym, pos in current_core.items():
                pnl_pct = pos.unrealized_pnl_net_pct  # 수수료 포함 순손익률
                logger.info(f"  - {sym} {pos.name}: {pnl_pct:+.2f}%")

            # 이전 리밸런싱에서 미완료 매수가 있는지 확인 (2단계 리밸런싱)
            core_state = self.get_core_state()
            pending_buys = core_state.get("pending_core_buys")
            if pending_buys:
                # pending 유효기간 체크: 2일 초과 시 폐기 (가격 괴리 위험)
                last_rb = core_state.get("last_rebalance")
                if last_rb:
                    try:
                        rb_dt = datetime.fromisoformat(last_rb)
                        age_days = (datetime.now() - rb_dt).days
                        if age_days > 2:
                            logger.warning(f"[코어홀딩] pending 매수 {age_days}일 경과 → 폐기 (가격 괴리 위험)")
                            self._save_core_state({"pending_core_buys": None})
                            pending_buys = None
                    except Exception:
                        pass
            if pending_buys:
                # 매도 체결 확인: 이전 리밸런싱에서 매도한 종목이 아직 보유 중이면 매수 보류
                sold_symbols = core_state.get("sold", [])
                for sold_sym in sold_symbols:
                    if sold_sym in portfolio.positions:
                        logger.warning(f"[코어홀딩] {sold_sym} 매도 미체결 → 매수 보류 (다음 윈도우 재시도)")
                        return False
                buy_count = 0
                for pb in pending_buys:
                    sym = pb["symbol"]
                    if sym in portfolio.positions:
                        logger.info(f"[코어홀딩] {sym} 이미 보유 중 → 스킵")
                        continue
                    entry_price = Decimal(str(pb["entry_price"]))
                    signal = Signal(
                        symbol=sym,
                        side=OrderSide.BUY,
                        strength=SignalStrength.NORMAL,
                        strategy=StrategyType.CORE_HOLDING,
                        price=entry_price,
                        stop_price=entry_price * Decimal(str(1 - core_cfg.get("stop_loss_pct", 15.0) / 100)),
                        score=pb.get("score", 70),
                        confidence=min(pb.get("score", 70) / 100.0, 1.0),
                        reason=f"코어홀딩 리밸런싱 매수(재시도): {pb.get('name', sym)}",
                        metadata={"is_core": True, "candidate_name": pb.get("name", sym), "batch_signal": True, "rebalance": True},
                    )
                    event = SignalEvent.from_signal(signal, source="core_rebalance")
                    await self._engine.emit(event)
                    buy_count += 1
                    logger.info(f"[코어홀딩] 재시도 매수: {sym} {pb.get('name', '')}")
                # pending 해제
                self._save_core_state({"pending_core_buys": None, "last_rebalance": datetime.now().isoformat()})
                if buy_count > 0:
                    logger.info(f"[코어홀딩] 재시도 매수 {buy_count}개 발행 완료")
                    return True
                logger.info("[코어홀딩] 재시도 매수 대상 없음 (이미 보유)")
                return True

            # 스캔 실행
            candidates = await self._core_screener.run_full_scan()
            if not candidates:
                if not current_core:
                    logger.warning("[코어홀딩] 후보 0건 + 보유 0개 → 스킵")
                    return False
                # 후보 없어도 기존 보유 포지션의 교체 매도 판단은 수행
                logger.warning("[코어홀딩] 스캔 후보 없음 → 기존 포지션 손실/MA200 이탈 체크만 수행")
                sell_targets_fallback = []
                for sym, pos in current_core.items():
                    if sym in rebalance_exclude:
                        logger.info(f"[코어홀딩] {sym} 리밸런싱 제외 목록 → 폴백 스킵")
                        continue
                    if pos.unrealized_pnl_net_pct <= -10.0:
                        sell_targets_fallback.append((sym, f"리밸런싱 손절 {pos.unrealized_pnl_net_pct:.1f}%"))
                if sell_targets_fallback:
                    for sym, reason in sell_targets_fallback:
                        pos = portfolio.positions[sym]
                        event = SignalEvent.from_signal(
                            Signal(
                                symbol=sym, side=OrderSide.SELL, strength=SignalStrength.NORMAL,
                                strategy=StrategyType.CORE_HOLDING, price=pos.current_price,
                                score=0, reason=f"코어홀딩 리밸런싱: {reason}",
                                metadata={"is_core": True, "rebalance": True},
                            ), source="core_rebalance",
                        )
                        await self._engine.emit(event)
                        logger.info(f"[코어홀딩] 후보 없음에서도 매도: {sym} ({reason})")
                    self._save_core_state({"last_rebalance": datetime.now().isoformat()})
                    return True
                logger.info("[코어홀딩] 기존 포지션 이상 없음 → 유지")
                self._save_core_state({"last_rebalance": datetime.now().isoformat()})
                return True

            # 후보 스코어 맵
            candidate_scores = {c.symbol: c for c in candidates}

            # 신규 매수 후보 (전체 미보유 + 점수 상위, 교체 판단에도 사용)
            # 스윙+코어 이중 보유 방지: 코어뿐 아니라 전체 포트폴리오 체크
            buy_candidates = [
                c for c in candidates
                if c.symbol not in portfolio.positions
                and c.score >= min_score
            ]

            # 교체 대상 판단
            sell_targets = []
            for sym, pos in current_core.items():
                # rebalance_exclude 설정 심볼은 교체/손절 대상 제외
                if sym in rebalance_exclude:
                    logger.info(f"[코어홀딩] {sym} 리밸런싱 제외 목록 → 스킵")
                    continue
                rescore = candidate_scores.get(sym)
                # 스캔에 포함되지 않은 종목은 유지 (스캔 누락 ≠ 필터 미달)
                if rescore is None:
                    logger.info(f"[코어홀딩] {sym} 스캔 결과 없음 → 유지")
                    continue

                # 1) 재스코어 < 55 → 교체
                if rescore.score < 55:
                    sell_targets.append((sym, f"재스코어 {rescore.score:.0f} < 55"))
                    continue

                # 2) 수익률 -10% → 교체
                if pos.unrealized_pnl_net_pct <= -10.0:
                    sell_targets.append((sym, f"리밸런싱 손절 {pos.unrealized_pnl_net_pct:.1f}%"))
                    continue

                # 3) MA200 이탈 연속 N일 → 교체 (스크리너 지표 활용)
                if ma200_break_days > 0 and rescore.indicators:
                    below_days = rescore.indicators.get("ma200_below_days", 0)
                    if below_days >= ma200_break_days:
                        close = rescore.indicators.get("close", 0)
                        ma200 = rescore.indicators.get("ma200", 0)
                        sell_targets.append((sym, f"MA200 이탈 {below_days}일 연속 (종가 {close:,.0f} < MA200 {ma200:,.0f})"))
                        continue

            # 4) replace_threshold: 기존 포지션을 점수 낮은 순으로 1:1 매칭
            # allow_replace=False(빈슬롯 즉시 매수)일 때는 교체 로직 스킵 — 빈슬롯 채우기만
            if allow_replace and replace_threshold > 0 and buy_candidates:
                # 아직 sell_targets에 안 들어간 기존 포지션만 대상 (제외 목록 심볼도 스킵)
                already_selling = {s for s, _ in sell_targets}
                replaceable = [
                    (sym, candidate_scores[sym].score)
                    for sym in current_core
                    if sym not in already_selling and sym in candidate_scores
                    and sym not in rebalance_exclude
                ]
                replaceable.sort(key=lambda x: x[1])  # 점수 낮은 순

                for (old_sym, old_score), new_cand in zip(replaceable, buy_candidates):
                    if new_cand.score - old_score >= replace_threshold:
                        sell_targets.append((
                            old_sym,
                            f"교체 (현재 {old_score:.0f} vs 신규 {new_cand.symbol} {new_cand.score:.0f}, 차이 +{new_cand.score - old_score:.0f})"
                        ))
            elif not allow_replace:
                logger.debug("[코어홀딩] allow_replace=False → replace_threshold 교체 스킵 (빈슬롯 매수 전용)")

            for sym, reason in sell_targets:
                logger.info(f"[코어홀딩] 교체 대상: {sym} ({reason})")

            # 빈 슬롯 계산 (매도 예정 슬롯 반영, 음수 방어)
            remaining_slots = max(0, max_positions - (len(current_core) - len(sell_targets)))

            # 매도 시그널 발행 (교체 대상)
            for sym, reason in sell_targets:
                if sym in portfolio.positions:
                    pos = portfolio.positions[sym]
                    event = SignalEvent.from_signal(
                        Signal(
                            symbol=sym,
                            side=OrderSide.SELL,
                            strength=SignalStrength.NORMAL,
                            strategy=StrategyType.CORE_HOLDING,
                            price=pos.current_price,
                            score=0,
                            reason=f"코어홀딩 리밸런싱: {reason}",
                            metadata={"is_core": True, "rebalance": True},
                        ),
                        source="core_rebalance",
                    )
                    await self._engine.emit(event)
                    logger.info(f"[코어홀딩] 리밸런싱 매도: {sym} ({reason})")

            # 매수 시그널 발행 (상위 후보, 빈 슬롯만큼)
            actual_buys = buy_candidates[:remaining_slots]

            # 매도가 있으면 매수를 pending으로 저장 (매도 체결 후 다음 윈도우에서 실행)
            if sell_targets and actual_buys:
                pending_buy_data = [{
                    "symbol": c.symbol,
                    "name": c.name,
                    "score": c.score,
                    "entry_price": str(c.entry_price),
                    "reasons": c.reasons[:3],
                } for c in actual_buys]
                self._save_core_state({
                    "sold": [s for s, _ in sell_targets],
                    "pending_core_buys": pending_buy_data,
                    "last_rebalance": datetime.now().isoformat(),
                })
                logger.info(
                    f"[코어홀딩] 매도 {len(sell_targets)}개 발행. "
                    f"매수 {len(actual_buys)}개는 pending (매도 체결 후 재시도)"
                )
                return False  # 매수 미완료 → 다음 윈도우에서 재시도

            # 매도 없이 매수만 있는 경우 즉시 발행
            buy_count = 0
            for candidate in actual_buys:
                # 코어홀딩: NORMAL 강도 사용 → STRONG(1.5x) 시 2종목으로 30% 도달, 3종목 불가
                signal = Signal(
                    symbol=candidate.symbol,
                    side=OrderSide.BUY,
                    strength=SignalStrength.NORMAL,
                    strategy=StrategyType.CORE_HOLDING,
                    price=candidate.entry_price,
                    stop_price=candidate.entry_price * Decimal(str(1 - core_cfg.get("stop_loss_pct", 15.0) / 100)),
                    score=candidate.score,
                    confidence=min(candidate.score / 100.0, 1.0),
                    reason=f"코어홀딩 리밸런싱 진입: {', '.join(candidate.reasons[:3])}",
                    metadata={
                        "is_core": True,
                        "candidate_name": candidate.name,
                        "batch_signal": True,
                        "rebalance": True,
                    },
                )
                event = SignalEvent.from_signal(signal, source="core_rebalance")
                await self._engine.emit(event)
                buy_count += 1
                logger.info(
                    f"[코어홀딩] 리밸런싱 매수: {candidate.symbol} {candidate.name} "
                    f"점수={candidate.score:.0f}"
                )

            # 상태 저장
            self._save_core_state({
                "last_rebalance": datetime.now().isoformat(),
                "sold": [s for s, _ in sell_targets],
                "bought": [c.symbol for c in actual_buys[:buy_count]],
                "pending_core_buys": None,
                "current_core_count": len(current_core) - len(sell_targets) + buy_count,
            })

            logger.info(
                f"[코어홀딩] 리밸런싱 완료: "
                f"매도={len(sell_targets)}개, 매수={buy_count}개"
            )
            return True

        except Exception as e:
            logger.error(f"[코어홀딩] 리밸런싱 오류: {e}", exc_info=True)
            return False

    def _save_core_state(self, data: Dict) -> None:
        """코어홀딩 상태 파일 저장"""
        try:
            self._core_state_path.parent.mkdir(parents=True, exist_ok=True)
            existing = {}
            if self._core_state_path.exists():
                existing = json.loads(self._core_state_path.read_text())
            existing.update(data)
            self._core_state_path.write_text(json.dumps(existing, ensure_ascii=False, indent=2))
        except Exception as e:
            logger.warning(f"[코어홀딩] 상태 저장 실패: {e}")

    def get_core_state(self) -> Dict:
        """코어홀딩 상태 파일 로드"""
        try:
            if self._core_state_path.exists():
                return json.loads(self._core_state_path.read_text())
        except Exception as e:
            logger.debug(f"[코어홀딩] 상태 로드 실패: {e}")
        return {}
