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
                name=sig.metadata.get("candidate_name", sig.symbol),
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
                atr_pct=float(sig.metadata.get("atr_pct", 0)),
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

    async def execute_pending_signals(self):
        """[09:01] 대기 시그널 실행"""
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

        executed = 0
        skipped = 0

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
                        "market_regime": _regime,
                        "sector": _sector,          # P0-5: 섹터 제한 체크에 활용
                        "gap_pct": round(gap_pct, 2),
                    },
                )

                # 종목명 캐시에 저장 (매수 시그널/주문 이벤트에 종목명 표시)
                name_cache = getattr(self._engine, '_stock_name_cache', None)
                if name_cache is not None and sig.name and sig.name != sig.symbol:
                    name_cache[sig.symbol] = sig.name

                event = SignalEvent.from_signal(signal, source="batch_analyzer")
                await self._engine.emit(event)
                executed += 1

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

    async def monitor_positions(self):
        """[매 30분] 보유 포지션 시세 갱신 + 청산 체크"""
        if not self._engine.portfolio.positions:
            return

        logger.debug(f"[포지션모니터] {len(self._engine.portfolio.positions)}개 포지션 체크")

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
                            await self._engine.emit(event)
                            await asyncio.sleep(0.2)
                            continue
                    except Exception as _rsi2e:
                        logger.debug(f"[포지션모니터] {symbol} RSI2 체크 실패 (무시): {_rsi2e}")

                # ExitManager 청산 체크
                if self._exit_manager:
                    exit_result = self._exit_manager.update_price(symbol, current_price)
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
                        await self._engine.emit(event)
                        await asyncio.sleep(0.2)  # rate limit
                        continue  # 청산 시그널 발행 시 보유기간 체크 스킵

                # 보유기간 초과 강제 청산
                if pos.entry_time:
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
                        await self._engine.emit(event)

                await asyncio.sleep(0.2)  # rate limit

            except Exception as e:
                logger.warning(f"[포지션모니터] {symbol} 체크 오류: {e}")

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

    async def execute_core_rebalance(self) -> bool:
        """코어홀딩 리밸런싱 실행

        1. 현재 코어 포지션 확인
        2. 스캔 실행 → 후보 생성
        3. 교체 대상 판단 (재스코어 < 55 또는 신규 +15점)
        4. 교체 매도 → 신규 매수 시그널 발행

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

            # 현재 코어 포지션 확인
            portfolio = self._engine.portfolio
            current_core = {}
            for sym, pos in portfolio.positions.items():
                if pos.strategy == "core_holding":
                    current_core[sym] = pos

            logger.info(f"[코어홀딩] 현재 코어 포지션: {len(current_core)}개")
            for sym, pos in current_core.items():
                pnl_pct = pos.unrealized_pnl_pct
                logger.info(f"  - {sym} {pos.name}: {pnl_pct:+.2f}%")

            # 이전 리밸런싱에서 미완료 매수가 있는지 확인 (2단계 리밸런싱)
            core_state = self.get_core_state()
            pending_buys = core_state.get("pending_core_buys")
            if pending_buys:
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
                logger.warning("[코어홀딩] 리밸런싱 후보 없음 → 기존 포지션 유지 (스캔 실패일 수 있음)")
                return False

            # 후보 스코어 맵
            candidate_scores = {c.symbol: c for c in candidates}

            # 신규 매수 후보 (현재 미보유 + 점수 상위, 교체 판단에도 사용)
            buy_candidates = [
                c for c in candidates
                if c.symbol not in current_core
                and c.score >= min_score
            ]

            # 교체 대상 판단
            sell_targets = []
            for sym, pos in current_core.items():
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
                if pos.unrealized_pnl_pct <= -10.0:
                    sell_targets.append((sym, f"리밸런싱 손절 {pos.unrealized_pnl_pct:.1f}%"))
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
            if replace_threshold > 0 and buy_candidates:
                # 아직 sell_targets에 안 들어간 기존 포지션만 대상
                already_selling = {s for s, _ in sell_targets}
                replaceable = [
                    (sym, candidate_scores[sym].score)
                    for sym in current_core
                    if sym not in already_selling and sym in candidate_scores
                ]
                replaceable.sort(key=lambda x: x[1])  # 점수 낮은 순

                for (old_sym, old_score), new_cand in zip(replaceable, buy_candidates):
                    if new_cand.score - old_score >= replace_threshold:
                        sell_targets.append((
                            old_sym,
                            f"교체 (현재 {old_score:.0f} vs 신규 {new_cand.symbol} {new_cand.score:.0f}, 차이 +{new_cand.score - old_score:.0f})"
                        ))

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
