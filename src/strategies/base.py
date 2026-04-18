"""
QWQ AI Trader - 전략 베이스 클래스 (통합)

KR/US 모든 전략 구현의 추상 인터페이스.

KR 전략: BaseStrategy를 상속 (이벤트 기반, on_market_data / generate_signal)
US 전략: USBaseStrategy를 상속 (DataFrame 기반, evaluate / generate_signal)
"""

from abc import ABC, abstractmethod
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Dict, List, Optional, Any

import pandas as pd
from loguru import logger

from ..core.types import (
    Signal, Position, Portfolio, Price, Quote,
    OrderSide, SignalStrength, StrategyType, TimeHorizon
)
from ..core.event import MarketDataEvent, ThemeEvent
from ..indicators.technical import compute_indicators


# ============================================================
# KR 전략 설정
# ============================================================

@dataclass
class StrategyConfig:
    """전략 설정 (KR 전략용)"""
    name: str = ""
    enabled: bool = True
    strategy_type: StrategyType = StrategyType.MOMENTUM_BREAKOUT

    # 포지션 관리
    max_position_pct: float = 30.0    # 최대 포지션 비율 (%)
    stop_loss_pct: float = 2.0        # 손절 (%)
    take_profit_pct: float = 3.0      # 익절 (%)
    trailing_stop_pct: float = 1.5    # 트레일링 스탑 (%)

    # 신호 필터
    min_score: float = 60.0           # 최소 신호 점수
    min_confidence: float = 0.5       # 최소 신뢰도

    # 거래 조건
    min_volume_ratio: float = 1.5     # 최소 거래량 비율 (평균 대비)
    min_price: int = 1000             # 최소 주가

    # 추가 설정
    params: Dict[str, Any] = field(default_factory=dict)


# ============================================================
# KR 전략 베이스 클래스
# ============================================================

class BaseStrategy(ABC):
    """
    전략 추상 베이스 클래스 (KR 전략용)

    모든 KR 전략은 이 클래스를 상속받아 구현합니다.
    이벤트 기반으로 on_market_data()에서 시그널을 생성합니다.
    """

    def __init__(self, config: StrategyConfig):
        self.config = config
        self.name = config.name or self.__class__.__name__
        self.enabled = config.enabled

        # 상태
        self._signals_generated: int = 0
        self._last_signal_at: Optional[datetime] = None

        # 데이터 캐시 (LRU: 최대 500 종목, 오래 미접근 종목 자동 제거)
        self._price_history: OrderedDict[str, List[Price]] = OrderedDict()
        self._indicators: OrderedDict[str, Dict[str, float]] = OrderedDict()
        self._max_cache_symbols = 500

        # 지표 재계산 방지용: 종목별 마지막 캔들 타임스탬프 / 마지막 계산 시각
        self._last_candle_ts: Dict[str, datetime] = {}
        self._last_calc_time: Dict[str, datetime] = {}

        logger.info(f"전략 초기화: {self.name} (type={config.strategy_type.value})")

    # ============================================================
    # 추상 메서드 (필수 구현)
    # ============================================================

    @abstractmethod
    async def generate_signal(
        self,
        symbol: str,
        current_price: Decimal,
        position: Optional[Position] = None
    ) -> Optional[Signal]:
        """
        매매 신호 생성

        Args:
            symbol: 종목코드
            current_price: 현재가
            position: 현재 포지션 (있는 경우)

        Returns:
            매매 신호 또는 None
        """
        pass

    @abstractmethod
    def calculate_score(self, symbol: str) -> float:
        """
        신호 점수 계산 (0~100)

        Args:
            symbol: 종목코드

        Returns:
            신호 점수
        """
        pass

    # ============================================================
    # 이벤트 핸들러 (오버라이드 가능)
    # ============================================================

    async def on_market_data(self, event: MarketDataEvent, position: Optional[Position] = None) -> Optional[Signal]:
        """시장 데이터 수신 시 호출"""
        if not self.enabled:
            return None

        symbol = event.symbol
        current_price = event.close

        # 가격 히스토리 업데이트
        self._update_price_history(event)

        # 지표 계산 (캔들 변경 또는 10초 경과 시에만 재계산)
        # 10초 쿨다운: 실시간 틱 수신 빈도와 CPU 사용률 균형
        now = datetime.now()
        candle_ts = event.timestamp if hasattr(event, 'timestamp') else now
        prev_candle_ts = self._last_candle_ts.get(symbol)
        prev_calc_time = self._last_calc_time.get(symbol)

        need_recalc = (
            prev_candle_ts is None
            or candle_ts != prev_candle_ts
            or prev_calc_time is None
            or (now - prev_calc_time) >= timedelta(seconds=10)
        )

        if need_recalc:
            self._calculate_indicators(symbol)
            self._last_candle_ts[symbol] = candle_ts
            self._last_calc_time[symbol] = now

        # 신호 생성
        signal = await self.generate_signal(symbol, current_price, position=position)

        if signal:
            # 최소 점수 필터
            if signal.score < self.config.min_score:
                return None

            self._signals_generated += 1
            self._last_signal_at = datetime.now()
            logger.info(
                f"[{self.name}] 신호 생성: {symbol} {signal.side.value} "
                f"점수={signal.score:.1f} 이유={signal.reason}"
            )

        return signal

    async def on_theme(self, event: ThemeEvent) -> Optional[Signal]:
        """테마 감지 시 호출 (필요시 오버라이드)"""
        return None

    async def generate_batch_signals(self, candidates: List) -> List[Signal]:
        """배치 분석용 시그널 생성 (스윙 전략 오버라이드)"""
        return []

    # ============================================================
    # 유틸리티 메서드
    # ============================================================

    def preload_history(self, symbol: str, prices: List[Price]):
        """
        과거 가격 데이터 사전 로드 (일봉 기반)

        봇 시작 시 KIS API에서 가져온 일봉 데이터를 전략에 주입합니다.
        이후 실시간 틱은 마지막 일봉 이후 데이터만 추가됩니다.
        """
        if not prices:
            return

        symbol = symbol.zfill(6)
        self._price_history[symbol] = list(prices)
        self._price_history.move_to_end(symbol)  # LRU 갱신

        # 지표 사전 계산 (내부에서 LRU 갱신 및 evict 처리)
        self._calculate_indicators(symbol)
        logger.debug(f"[{self.name}] {symbol} 히스토리 {len(prices)}일 로드, 지표 계산 완료")

    def _update_price_history(self, event: MarketDataEvent):
        """가격 히스토리 업데이트 (실시간 틱)"""
        symbol = event.symbol

        if symbol not in self._price_history:
            self._price_history[symbol] = []

        # 기존 히스토리가 있으면 마지막 엔트리를 업데이트 (당일 캔들 갱신)
        # 없으면 새로 추가
        history = self._price_history[symbol]
        price = event.to_price()

        if history:
            last = history[-1]
            # 같은 날짜면 당일 캔들 업데이트 (고가/저가/종가/거래량)
            if last.timestamp.date() == price.timestamp.date():
                last.close = price.close
                if price.high > last.high:
                    last.high = price.high
                if price.low < last.low:
                    last.low = price.low
                last.volume = price.volume
                # LRU 갱신: 접근한 종목을 최신으로 이동
                self._price_history.move_to_end(symbol)
                return
            else:
                # 새로운 날짜면 새 캔들 추가
                history.append(price)
        else:
            history.append(price)

        # 최대 200개 유지
        if len(history) > 200:
            self._price_history[symbol] = history[-200:]

        # LRU 갱신
        self._price_history.move_to_end(symbol)

    def _get_elapsed_trading_fraction(self) -> float:
        """
        정규장 경과 비율 (0.2 ~ 1.0)

        정규장 09:00~15:30 기준, 현재까지 경과 비율 반환.
        장 시작 직후 과도한 거래량 추정 방지를 위해 최소 0.2 반환
        (20% 미만 경과 시 거래량 추정 정확도가 너무 낮음).
        """
        now = datetime.now()
        current_minutes = now.hour * 60 + now.minute

        market_open = 540   # 09:00
        market_close = 930  # 15:30
        total_minutes = market_close - market_open  # 390분

        if current_minutes < market_open:
            return 0.2
        elif current_minutes >= market_close:
            return 1.0
        else:
            elapsed = (current_minutes - market_open) / total_minutes
            return max(elapsed, 0.2)

    def _calculate_indicators(self, symbol: str):
        """기술적 지표 계산"""
        history = self._price_history.get(symbol, [])
        if len(history) < 5:
            return

        closes = [float(p.close) for p in history]
        volumes = [p.volume for p in history]

        # 기본 지표
        indicators = {}

        # 이동평균
        if len(closes) >= 5:
            indicators["ma5"] = sum(closes[-5:]) / 5
        if len(closes) >= 20:
            indicators["ma20"] = sum(closes[-20:]) / 20
        if len(closes) >= 60:
            indicators["ma60"] = sum(closes[-60:]) / 60

        # 거래량 평균 (장중 경과시간 보정)
        if len(volumes) >= 20:
            indicators["vol_ma20"] = sum(volumes[-20:]) / 20

            today_volume = volumes[-1]

            # 당일 캔들이면 경과시간으로 보정하여 하루 전체 거래량 추정
            last_price = history[-1]
            if last_price.timestamp.date() == datetime.now().date():
                elapsed = self._get_elapsed_trading_fraction()
                if elapsed > 0:
                    today_volume = today_volume / elapsed

            indicators["vol_ratio"] = today_volume / indicators["vol_ma20"] if indicators["vol_ma20"] > 0 else 1.0
            indicators["vol_ratio_raw"] = volumes[-1] / indicators["vol_ma20"] if indicators["vol_ma20"] > 0 else 1.0

        # 전일 종가 / 당일 시가 (갭 전략용)
        if len(history) >= 2:
            indicators["prev_close"] = float(history[-2].close)
            indicators["open"] = float(history[-1].open)
        elif len(history) == 1:
            indicators["prev_close"] = 0  # 전일 데이터 없음
            indicators["open"] = float(history[-1].open)

        # 모멘텀
        if len(closes) >= 2:
            indicators["change_1d"] = (closes[-1] - closes[-2]) / closes[-2] * 100 if closes[-2] > 0 else 0
        if len(closes) >= 4:
            indicators["change_3d"] = (closes[-1] - closes[-4]) / closes[-4] * 100 if closes[-4] > 0 else 0
        if len(closes) >= 6:
            indicators["change_5d"] = (closes[-1] - closes[-6]) / closes[-6] * 100 if closes[-6] > 0 else 0
        if len(closes) >= 21:
            indicators["change_20d"] = (closes[-1] - closes[-21]) / closes[-21] * 100 if closes[-21] > 0 else 0

        # RSI (14일) - Wilder's Smoothing 적용
        if len(closes) >= 15:
            rsi_value = self._calculate_rsi(closes, 14)
            indicators["rsi"] = rsi_value
            indicators["rsi_14"] = rsi_value  # 별칭

        # VWAP (당일 기준 - 최근 데이터 사용)
        if len(history) >= 1:
            vwap_period = min(len(history), 20)
            recent = history[-vwap_period:]
            total_pv = sum(
                (float(p.high) + float(p.low) + float(p.close)) / 3 * p.volume
                for p in recent
            )
            total_vol = sum(p.volume for p in recent)
            indicators["vwap"] = total_pv / total_vol if total_vol > 0 else closes[-1]

        # 변동성 (표준편차)
        if len(closes) >= 20:
            mean = sum(closes[-20:]) / 20
            variance = sum((x - mean) ** 2 for x in closes[-20:]) / 20
            indicators["volatility"] = (variance ** 0.5) / mean * 100 if mean > 0 else 0

        # 고가/저가 대비 (당일 제외: 돌파 감지를 위해 전일까지만 사용)
        if len(history) >= 21:
            prev_history = history[-21:-1]  # 전일까지 20일
            high_20d = max(float(p.high) for p in prev_history)
            low_20d = min(float(p.low) for p in prev_history)
            indicators["high_20d"] = high_20d
            indicators["low_20d"] = low_20d
            indicators["high_proximity"] = closes[-1] / high_20d if high_20d > 0 else 0
            indicators["low_proximity"] = closes[-1] / low_20d if low_20d > 0 else 1
        elif len(history) >= 20:
            prev_history = history[-20:-1]
            high_20d = max(float(p.high) for p in prev_history)
            low_20d = min(float(p.low) for p in prev_history)
            indicators["high_20d"] = high_20d
            indicators["low_20d"] = low_20d
            indicators["high_proximity"] = closes[-1] / high_20d if high_20d > 0 else 0
            indicators["low_proximity"] = closes[-1] / low_20d if low_20d > 0 else 1

        # 52주 최고가 — 명시적 250영업일 슬라이스
        # history가 250 미만이면 사용 가능한 전체 사용 (warm-up 구간 fallback)
        # 이전: history 전체 max → history 길이가 200이면 사실상 "200일 신고가"라 SEPA stage 판정 오차
        _w52 = history[-250:] if len(history) >= 250 else history
        _w52_highs = [float(p.high) for p in _w52]
        indicators["high_52w"] = max(_w52_highs) if _w52_highs else closes[-1]
        indicators["high_52w_window"] = len(_w52)  # 디버깅: 실제 사용된 캔들 수

        # 지표 저장 및 LRU 갱신
        self._indicators[symbol] = indicators
        self._indicators.move_to_end(symbol)

        # 캐시 크기 제한 (LRU: 가장 오래 미접근 종목부터 제거)
        self._evict_lru_if_needed()

    def _evict_lru_if_needed(self):
        """LRU 캐시 크기 제한 적용 (오래 미접근 종목부터 제거)"""
        while len(self._indicators) > self._max_cache_symbols:
            oldest_symbol = next(iter(self._indicators))
            del self._indicators[oldest_symbol]
            self._price_history.pop(oldest_symbol, None)
            self._last_candle_ts.pop(oldest_symbol, None)
            self._last_calc_time.pop(oldest_symbol, None)

    def _calculate_rsi(self, prices: List[float], period: int = 14) -> float:
        """
        RSI 계산 (Wilder's Smoothing 적용)
        """
        if len(prices) < period + 1:
            return 50.0

        changes = [prices[i] - prices[i-1] for i in range(1, len(prices))]

        gains = [max(c, 0) for c in changes[:period]]
        losses = [max(-c, 0) for c in changes[:period]]

        avg_gain = sum(gains) / period
        avg_loss = sum(losses) / period

        for c in changes[period:]:
            gain = max(c, 0)
            loss = max(-c, 0)
            avg_gain = (avg_gain * (period - 1) + gain) / period
            avg_loss = (avg_loss * (period - 1) + loss) / period

        if avg_gain == 0 and avg_loss == 0:
            return 50.0

        if avg_loss == 0:
            return 100.0

        rs = avg_gain / avg_loss
        return 100 - (100 / (1 + rs))

    def get_indicators(self, symbol: str) -> Dict[str, float]:
        """지표 조회 (LRU 갱신)"""
        indicators = self._indicators.get(symbol, {})
        if symbol in self._indicators:
            self._indicators.move_to_end(symbol)
            if symbol in self._price_history:
                self._price_history.move_to_end(symbol)
        return indicators

    def get_stats(self) -> Dict[str, Any]:
        """전략 통계"""
        return {
            "name": self.name,
            "enabled": self.enabled,
            "signals_generated": self._signals_generated,
            "last_signal_time": self._last_signal_at.isoformat() if self._last_signal_at else None,
            "tracked_symbols": len(self._price_history),
        }

    # ============================================================
    # R/R 비율 필터 (리스크 대비 리워드)
    # ============================================================

    def check_rr_ratio(
        self,
        entry_price: Decimal,
        target_price: Optional[Decimal],
        stop_price: Optional[Decimal],
        min_rr: float = 2.0,
    ) -> bool:
        """
        R/R 비율 체크 — min_rr 미만이면 False (진입 차단)

        Args:
            entry_price: 진입가
            target_price: 목표가
            stop_price: 손절가
            min_rr: 최소 R/R 비율 (기본 2.0)
        """
        if target_price is None or stop_price is None:
            return True  # 목표/손절 미설정 시 통과
        if entry_price <= 0:
            return True

        reward = float(target_price - entry_price)
        risk = float(entry_price - stop_price)

        if risk <= 0:
            return False  # 손절가 >= 진입가: 잘못된 설정 → 진입 차단

        rr_ratio = reward / risk
        if rr_ratio < min_rr:
            logger.debug(
                f"[R/R] {self.name} R/R={rr_ratio:.2f} < {min_rr} — 진입 차단"
            )
            return False
        return True

    # ============================================================
    # 신호 생성 헬퍼
    # ============================================================

    def create_signal(
        self,
        symbol: str,
        side: OrderSide,
        strength: SignalStrength,
        price: Decimal,
        score: float,
        reason: str,
        target_price: Optional[Decimal] = None,
        stop_price: Optional[Decimal] = None,
    ) -> Signal:
        """신호 객체 생성"""
        return Signal(
            symbol=symbol,
            side=side,
            strength=strength,
            strategy=self.config.strategy_type,
            price=price,
            target_price=target_price,
            stop_price=stop_price,
            score=score,
            confidence=score / 100.0,
            reason=reason,
            metadata={
                "strategy_name": self.name,
                "indicators": dict(self._indicators.get(symbol, {})),
            },
        )


# ============================================================
# US 전략 베이스 클래스
# ============================================================

class USBaseStrategy(ABC):
    """
    Base class for all US trading strategies.

    US 전략은 DataFrame 기반으로 evaluate()에서 시그널을 생성합니다.
    """

    # Override in subclass
    name: str = "base"
    strategy_type: StrategyType = StrategyType.MOMENTUM_BREAKOUT
    time_horizon: TimeHorizon = TimeHorizon.DAY

    def __init__(self, config: dict = None):
        self.config = config or {}
        self.enabled = self.config.get('enabled', True)
        self.min_score = self.config.get('min_score', 65)

        # Indicator cache: {symbol: {indicator_name: value}}
        self._indicator_cache: OrderedDict[str, Dict[str, Any]] = OrderedDict()
        self._max_cache_symbols = 500

        # Sentiment scorer (optional, set externally)
        self._sentiment_scorer = None

        # RS Ranking용 벤치마크 (외부에서 set_benchmark() 호출)
        self._benchmark_close: Optional[pd.Series] = None

    def evaluate(self, symbol: str, history: pd.DataFrame,
                 portfolio: Portfolio) -> Optional[Signal]:
        """
        Main entry point: evaluate a symbol and return a Signal or None.

        Args:
            symbol: Ticker symbol (e.g., 'AAPL')
            history: OHLCV DataFrame up to current bar (inclusive)
            portfolio: Current portfolio state
        """
        if not self.enabled:
            return None

        if history is None or len(history) < 20:
            return None

        # Compute indicators
        indicators = self._get_indicators(symbol, history)
        if not indicators:
            return None

        # Check entry conditions (implemented by subclass)
        signal = self.generate_signal(symbol, indicators, history, portfolio)

        # Apply news sentiment adjustment if available
        if signal and self._sentiment_scorer:
            try:
                adj = self._sentiment_scorer.get_adjustment(symbol)
                if adj.bonus != 0:
                    signal.score = max(0, min(100, signal.score + adj.bonus))
                    signal.reason = f"{signal.reason} | {adj.reason}"
            except Exception:
                pass  # Don't fail on sentiment errors

        if signal and signal.score >= self.min_score:
            return signal

        return None

    @abstractmethod
    def generate_signal(
        self,
        symbol: str,
        indicators: Dict[str, Any],
        history: pd.DataFrame,
        portfolio: Portfolio,
    ) -> Optional[Signal]:
        """
        Generate trading signal. Implement in subclass.

        Args:
            symbol: Ticker
            indicators: Pre-computed technical indicators
            history: Full price history up to current bar
            portfolio: Current portfolio

        Returns:
            Signal object or None
        """
        pass

    def check_exit(self, symbol: str, history: pd.DataFrame,
                   position: Position) -> Optional[str]:
        """
        Check custom exit conditions. Override in subclass.

        Returns:
            Exit reason string, or None to keep holding.
        """
        return None

    def set_benchmark(self, benchmark_close: pd.Series):
        """RS Ranking용 벤치마크 시세 설정 (예: SPY)"""
        self._benchmark_close = benchmark_close

    def _get_indicators(self, symbol: str, history: pd.DataFrame) -> Dict[str, Any]:
        """Compute and cache indicators"""
        try:
            indicators = compute_indicators(history)

            # RS Ranking 계산 (벤치마크 설정 시)
            if self._benchmark_close is not None and len(history) >= 252:
                try:
                    from src.indicators.technical import rs_rating
                    bench = self._benchmark_close.reindex(history.index, method='ffill')
                    rs = rs_rating(history['close'], bench)
                    rs_val = float(rs.iloc[-1]) if not pd.isna(rs.iloc[-1]) else None
                    if rs_val is not None:
                        indicators['rs_rating'] = rs_val
                except Exception:
                    pass

            # Cache management (LRU)
            if symbol in self._indicator_cache:
                self._indicator_cache.move_to_end(symbol)
            self._indicator_cache[symbol] = indicators

            while len(self._indicator_cache) > self._max_cache_symbols:
                self._indicator_cache.popitem(last=False)

            return indicators
        except Exception as e:
            logger.debug(f"Indicator computation failed for {symbol}: {e}")
            return {}

    def check_rr_ratio(
        self,
        entry_price: float,
        target_price: Optional[float],
        stop_price: Optional[float],
        min_rr: float = 2.0,
    ) -> bool:
        """R/R 비율 체크 — min_rr 미만이면 False"""
        if target_price is None or stop_price is None:
            return True
        if entry_price <= 0:
            return True

        reward = target_price - entry_price
        risk = entry_price - stop_price

        if risk <= 0:
            return True

        rr_ratio = reward / risk
        if rr_ratio < min_rr:
            logger.debug(
                f"[R/R] {self.name} R/R={rr_ratio:.2f} < {min_rr} — 진입 차단"
            )
            return False
        return True

    def _create_signal(
        self,
        symbol: str,
        score: float,
        reason: str,
        price: float = None,
        stop_price: float = None,
        target_price: float = None,
        strength: SignalStrength = None,
        metadata: dict = None,
    ) -> Signal:
        """Helper to create a Signal object"""
        if strength is None:
            if score >= 85:
                strength = SignalStrength.VERY_STRONG
            elif score >= 75:
                strength = SignalStrength.STRONG
            elif score >= 65:
                strength = SignalStrength.NORMAL
            else:
                strength = SignalStrength.WEAK

        meta = metadata or {}
        meta['time_horizon'] = self.time_horizon

        return Signal(
            symbol=symbol,
            side=OrderSide.BUY,
            strength=strength,
            strategy=self.strategy_type,
            price=Decimal(str(price)) if price is not None else None,
            target_price=Decimal(str(target_price)) if target_price is not None else None,
            stop_price=Decimal(str(stop_price)) if stop_price is not None else None,
            score=score,
            confidence=score / 100,
            reason=reason,
            metadata=meta,
        )
