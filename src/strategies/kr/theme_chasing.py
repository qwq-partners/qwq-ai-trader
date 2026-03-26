"""
QWQ AI Trader - KR 테마 추종 전략

핫 테마 관련 종목을 추적하고 적시에 진입합니다.
원본: ai-trader-v2/src/strategies/theme_chasing.py

주의: theme_detector는 KR 전용 모듈이며, 통합 프로젝트에서는
      src/signals/sentiment/theme_detector.py에 위치합니다.
"""

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Dict, List, Optional, Set, Any
from loguru import logger

from ..base import BaseStrategy, StrategyConfig
from ...core.types import (
    Signal, Position, Theme,
    OrderSide, SignalStrength, StrategyType
)
from ...utils.sizing import atr_position_multiplier
from ...core.event import MarketDataEvent, ThemeEvent

# ThemeDetector는 lazy import (모듈 미존재 시 graceful degradation)
try:
    from ...signals.sentiment.kr_theme_detector import ThemeDetector, ThemeInfo, get_theme_detector
except ImportError:
    ThemeDetector = None
    ThemeInfo = None
    def get_theme_detector():
        return None


@dataclass
class ThemeChasingConfig(StrategyConfig):
    """테마 추종 전략 설정"""
    name: str = "ThemeChasing"
    strategy_type: StrategyType = StrategyType.THEME_CHASING

    # 테마 조건
    min_theme_score: float = 70.0     # 최소 테마 점수
    max_theme_age_minutes: int = 30   # 테마 신선도 (분)

    # 종목 조건
    min_change_pct: float = 2.0       # 최소 등락률 (%)
    max_change_pct: float = 8.0       # 최대 등락률 (%)
    min_volume_ratio: float = 1.8     # 최소 거래량 비율

    # 진입 조건
    entry_window_minutes: int = 30    # 테마 발생 후 진입 가능 시간
    max_entries_per_theme: int = 2    # 테마당 최대 진입 수

    # 청산 조건
    stop_loss_pct: float = 1.5        # 손절 (테마는 빠른 손절)
    take_profit_pct: float = 3.0      # 익절
    trailing_stop_pct: float = 1.0    # 트레일링 스탑

    # 거래대금 필터 (진입 품질)
    min_trading_value: float = 500_000_000   # 최소 거래대금 5억원 (당일 누적)

    # 테마 확산도 (진입 품질)
    min_theme_breadth: int = 3               # 테마 내 동반 상승 최소 종목 수
    theme_breadth_change_pct: float = 1.0    # 동반 상승 판정 최소 등락률 (%)

    # 장중 고점 유지 (진입 품질)
    max_high_retreat_pct: float = 3.0        # 장중 고점 대비 최대 후퇴 허용 (%)

    # ATR 진입 필터 (초고변동 종목 차단)
    max_atr_pct: float = 8.0                 # ATR 상한 (%)

    # 대형주 제외 (시가총액 상위 대형주는 테마 모멘텀 약함)
    exclude_large_cap_symbols: bool = True    # 대형주 테마 편입 차단

    # 장초반 과열 방지 (시간대별 등락률 상한 차등)
    max_change_pct_morning: float = 4.0      # 09:05~10:00 (장초반 추격 방지)

    # 시간대 제한
    trading_start_time: str = "09:05" # 시작 시간
    trading_end_time: str = "15:00"   # 종료 시간


class ThemeChasingStrategy(BaseStrategy):
    """
    테마 추종 전략

    핫 테마 감지 시 관련 종목에 빠르게 진입하여
    테마 모멘텀을 따라가는 전략입니다.
    """

    def __init__(self, config: Optional[ThemeChasingConfig] = None, kis_market_data=None):
        config = config or ThemeChasingConfig()
        super().__init__(config)
        self.theme_config = config

        # 테마 탐지기
        self._theme_detector = None

        # KIS 시장 데이터 (외국인/기관 수급)
        self._kis_market_data = kis_market_data
        self._foreign_cache: Dict[str, Dict] = {}
        self._institution_cache: Dict[str, Dict] = {}

        # 테마 추적
        self._active_themes: Dict[str, Any] = {}  # ThemeInfo 또는 유사 객체
        self._theme_entries: Dict[str, int] = {}
        self._entries_date: Optional[date] = None

        # 포지션별 테마 매핑
        self._position_themes: Dict[str, str] = {}

    def set_theme_detector(self, detector):
        """테마 탐지기 설정"""
        self._theme_detector = detector

    async def on_theme(self, event: ThemeEvent) -> Optional[Signal]:
        """테마 이벤트 처리"""
        if not self.enabled:
            return None

        theme_name = event.name
        theme_score = event.score

        if theme_score < self.theme_config.min_theme_score:
            return None

        if theme_name not in self._active_themes:
            if ThemeInfo is not None:
                self._active_themes[theme_name] = ThemeInfo(
                    name=theme_name,
                    keywords=event.keywords,
                    related_stocks=event.symbols,
                    score=theme_score,
                )
            else:
                # ThemeInfo 미사용 시 dict 폴백
                self._active_themes[theme_name] = {
                    "name": theme_name,
                    "keywords": event.keywords,
                    "related_stocks": event.symbols,
                    "score": theme_score,
                    "last_updated": datetime.now(),
                }
            self._theme_entries[theme_name] = 0
            logger.info(f"[테마 추종] 새 핫 테마 감지: {theme_name} (점수: {theme_score:.0f})")
        else:
            theme = self._active_themes[theme_name]
            if hasattr(theme, 'score'):
                theme.score = theme_score
                theme.last_updated = datetime.now()
            else:
                theme["score"] = theme_score
                theme["last_updated"] = datetime.now()

        return None

    async def generate_signal(
        self,
        symbol: str,
        current_price: Decimal,
        position: Optional[Position] = None
    ) -> Optional[Signal]:
        """매매 신호 생성"""
        today = date.today()
        if self._entries_date != today:
            self._theme_entries.clear()
            self._active_themes.clear()
            self._entries_date = today

        indicators = self.get_indicators(symbol)

        if not indicators:
            return None

        if position and position.quantity > 0:
            return await self._check_exit_signal(symbol, current_price, position, indicators)

        return await self._check_entry_signal(symbol, current_price, indicators)

    async def _check_entry_signal(
        self,
        symbol: str,
        current_price: Decimal,
        indicators: Dict[str, float]
    ) -> Optional[Signal]:
        """진입 신호 체크"""
        if not self._is_trading_time():
            return None

        if not self._theme_detector:
            self._theme_detector = get_theme_detector()

        if not self._theme_detector:
            return None

        stock_themes = self._theme_detector.get_stock_themes(symbol)
        if not stock_themes:
            return None

        hot_theme = None
        hot_theme_score = 0.0

        for theme_name in stock_themes:
            if theme_name in self._active_themes:
                theme = self._active_themes[theme_name]

                last_updated = getattr(theme, 'last_updated', None) or (theme.get("last_updated", datetime.now()) if isinstance(theme, dict) else datetime.now())
                age_minutes = (datetime.now() - last_updated).total_seconds() / 60
                if age_minutes > self.theme_config.max_theme_age_minutes:
                    continue

                if self._theme_entries.get(theme_name, 0) >= self.theme_config.max_entries_per_theme:
                    continue

                t_score = getattr(theme, 'score', None) or (theme.get("score", 0) if isinstance(theme, dict) else 0)
                if t_score > hot_theme_score:
                    hot_theme = theme
                    hot_theme_score = t_score

        if not hot_theme:
            return None

        price = float(current_price)
        change_pct = indicators.get("change_1d", 0)
        vol_ratio = indicators.get("vol_ratio", 0)

        if change_pct < self.theme_config.min_change_pct:
            return None

        # 시간대별 등락률 상한 차등 (장초반 추격 방지)
        now_time = datetime.now().strftime("%H:%M")
        if now_time < "10:00":
            _max_change = self.theme_config.max_change_pct_morning  # 4%
        else:
            _max_change = self.theme_config.max_change_pct  # 8%
        if change_pct > _max_change:
            logger.debug(f"[테마 추종] {symbol} 과열 (등락률 {change_pct:.1f}% > {_max_change:.0f}%)")
            return None

        # 대형주 테마 편입 차단 (시총 상위 대형주는 테마 모멘텀 약함)
        if self.theme_config.exclude_large_cap_symbols:
            _large_caps = {
                '005930', '000660', '373220', '207940', '005380',
                '000270', '051910', '006400', '035420', '035720',
                '068270', '028260', '105560', '055550', '086790',
                '316140', '003670', '034730', '012330', '066570',
            }
            if symbol in _large_caps:
                logger.debug(f"[테마 추종] {symbol} 대형주 제외")
                return None

        # RSI 과매수 차단 — 이미 과열된 종목은 초반 확산 구간이 아님
        rsi_14 = indicators.get("rsi_14")
        if rsi_14 is not None and rsi_14 > 75:
            logger.debug(f"[테마 추종] {symbol} RSI 과매수 차단: {rsi_14:.0f} > 75")
            return None

        # MA20 대비 과확장 차단 — 극단적 급등 종목만 제외
        # 테마는 본질적으로 단기 급등 → 등락률 필터(8%)와 RSI(75)가 1차 과열 방어
        # MA20 필터는 며칠간 지속 급등한 극단 케이스만 차단 (25%)
        ma20 = indicators.get("ma20")
        if ma20 is not None and ma20 > 0 and price > 0:
            ma20_dist = (price - ma20) / ma20 * 100
            if ma20_dist > 25:
                logger.debug(f"[테마 추종] {symbol} MA20 과확장 차단: +{ma20_dist:.1f}% > 25%")
                return None

        if vol_ratio < self.theme_config.min_volume_ratio:
            return None

        # 거래대금 필터 (유동성 확보)
        trading_value = float(current_price) * indicators.get("volume", 0)
        if trading_value < self.theme_config.min_trading_value:
            logger.debug(
                f"[테마 추종] {symbol} 거래대금 부족: "
                f"{trading_value / 1e8:.1f}억 < {self.theme_config.min_trading_value / 1e8:.1f}억"
            )
            return None

        # ATR 진입 필터 (초고변동 종목 차단)
        atr_pct = indicators.get("atr_14")
        if atr_pct is not None and atr_pct > self.theme_config.max_atr_pct:
            logger.info(
                f"[테마 추종] {symbol} ATR 과다: "
                f"{atr_pct:.1f}% > {self.theme_config.max_atr_pct:.1f}%"
            )
            return None

        # 장중 고점 대비 후퇴율 체크
        day_high = indicators.get("high", 0)
        retreat_pct = 0.0
        if day_high > 0 and price > 0:
            retreat_pct = (day_high - price) / day_high * 100
            if retreat_pct > self.theme_config.max_high_retreat_pct:
                logger.debug(
                    f"[테마 추종] {symbol} 장중 고점 후퇴: "
                    f"{retreat_pct:.1f}% > {self.theme_config.max_high_retreat_pct:.1f}%"
                )
                return None

        # 테마 확산도: 같은 테마의 다른 종목들도 동반 상승 중인지 확인
        theme_stocks = (
            getattr(hot_theme, 'related_stocks', None)
            or (hot_theme.get("related_stocks", []) if isinstance(hot_theme, dict) else [])
        )
        breadth_count = 0
        _cached_count = 0  # 지표 캐시가 있는 종목 수
        for ts in theme_stocks[:10]:  # 상위 10개만 체크 (성능)
            if ts == symbol:
                continue
            ts_ind = self.get_indicators(ts)
            if ts_ind:
                _cached_count += 1
                ts_change = ts_ind.get("change_1d", 0)
                if ts_change >= self.theme_config.theme_breadth_change_pct:
                    breadth_count += 1

        # 캐시된 종목이 충분할 때만 확산도 체크 (장 초반 캐시 미스 방어)
        # 캐시 2개 미만이면 확산도 판단 불가 → 스킵 (다른 필터로 품질 보장)
        if _cached_count >= 2 and breadth_count < self.theme_config.min_theme_breadth:
            logger.debug(
                f"[테마 추종] {symbol} 테마 확산도 부족: "
                f"동반상승 {breadth_count}/{_cached_count}종목 < 최소 {self.theme_config.min_theme_breadth}종목"
            )
            return None

        # 뉴스 센티멘트 필터/보너스
        news_bonus = 0.0
        news_info = ""
        if self._theme_detector:
            sentiment = self._theme_detector.get_stock_sentiment(symbol)
            if sentiment:
                direction = sentiment.get("direction", "")
                impact = sentiment.get("impact", 0)
                reason_text = sentiment.get("reason", "")

                if direction == "bearish":
                    logger.info(
                        f"[테마 추종] {symbol} 악재 차단: "
                        f"impact={impact}, {reason_text}"
                    )
                    return None

                if direction == "bullish":
                    news_bonus = min(impact * 1.5, 15.0)
                    news_info = f", 뉴스호재={impact}"

        # 외국인/기관 수급 체크
        supply_bonus = 0.0
        supply_info = ""
        await self._refresh_supply_demand()
        if self._foreign_cache or self._institution_cache:
            supply_bonus, supply_info, _ = self._get_supply_demand_bonus(symbol)
            if supply_info:
                news_info += f", {supply_info}"

        # 신호 강도 결정
        if hot_theme_score >= 90:
            strength = SignalStrength.VERY_STRONG
        elif hot_theme_score >= 80:
            strength = SignalStrength.STRONG
        else:
            strength = SignalStrength.NORMAL

        score = self._calculate_entry_score(
            hot_theme_score, change_pct, vol_ratio,
            breadth_count=breadth_count, retreat_pct=retreat_pct,
        )
        score = max(0.0, min(score + news_bonus + supply_bonus, 100.0))

        if score < self.config.min_score:
            return None

        target_price = Decimal(str(price * (1 + self.theme_config.take_profit_pct / 100)))
        stop_price = Decimal(str(price * (1 - self.theme_config.stop_loss_pct / 100)))

        hot_theme_name = getattr(hot_theme, 'name', None) or hot_theme.get("name", "unknown")
        self._theme_entries[hot_theme_name] = self._theme_entries.get(hot_theme_name, 0) + 1
        self._position_themes[symbol] = hot_theme_name

        reason = (
            f"테마[{hot_theme_name}] 점수={hot_theme_score:.0f}, "
            f"등락률={change_pct:+.1f}%, 거래량={vol_ratio:.1f}x{news_info}"
        )

        logger.info(f"[테마 추종] 진입 신호: {symbol} - {reason}")

        # ATR 기반 포지션 사이징 (고변동 → 비중 축소)
        _atr_val = atr_pct if atr_pct is not None else 0
        _pos_mult = atr_position_multiplier(_atr_val)

        return Signal(
            symbol=symbol,
            side=OrderSide.BUY,
            strength=strength,
            strategy=self.config.strategy_type,
            price=current_price,
            target_price=target_price,
            stop_price=stop_price,
            score=score,
            confidence=score / 100.0,
            reason=reason,
            metadata={
                "strategy_name": self.name,
                "indicators": dict(self._indicators.get(symbol, {})),
                "atr_pct": _atr_val,
                "position_multiplier": _pos_mult,
                "theme_name": hot_theme_name,
            },
        )

    async def _check_exit_signal(
        self,
        symbol: str,
        current_price: Decimal,
        position: Position,
        indicators: Dict[str, float]
    ) -> Optional[Signal]:
        """청산 신호 체크 (테마 쿨다운만 자체 처리)"""
        theme_name = self._position_themes.get(symbol)
        if theme_name and theme_name in self._active_themes:
            theme = self._active_themes[theme_name]
            t_score = getattr(theme, 'score', None) or theme.get("score", 0)

            if t_score < self.theme_config.min_theme_score * 0.7:
                self._cleanup_position_theme(symbol)
                return self.create_signal(
                    symbol=symbol,
                    side=OrderSide.SELL,
                    strength=SignalStrength.NORMAL,
                    price=current_price,
                    score=70.0,
                    reason=f"테마 쿨다운: {theme_name} 점수 {t_score:.0f}",
                )

        return None

    def _calculate_entry_score(
        self,
        theme_score: float,
        change_pct: float,
        vol_ratio: float,
        breadth_count: int = 0,
        retreat_pct: float = 0.0,
    ) -> float:
        """진입 점수 계산 (100점 만점, 5개 항목)"""
        score = 0.0

        # 테마 점수 (40점)
        score += min(theme_score * 0.4, 40)

        # 등락률 (20점) — 초기 확산 구간(2~4%)에 집중
        if 2 <= change_pct <= 4:
            score += 20   # 초기 확산: 최고 점수
        elif 4 < change_pct <= 6:
            score += 14   # 초기 가속
        elif 6 < change_pct <= 8:
            score += 8    # 과열 진입
        else:
            score += 4

        # 거래량비율 (15점)
        score += min(vol_ratio * 3, 15)

        # 테마 확산도 (15점)
        if breadth_count >= 5:
            score += 15
        elif breadth_count >= 3:
            score += 10
        elif breadth_count >= 1:
            score += 5

        # 장중 고점 유지도 (10점) — 후퇴가 적을수록 고점수
        if retreat_pct <= 0.5:
            score += 10
        elif retreat_pct <= 1.5:
            score += 7
        elif retreat_pct <= 3.0:
            score += 4

        return min(score, 100.0)

    def _is_trading_time(self) -> bool:
        """거래 가능 시간 체크"""
        now = datetime.now()
        current_time = now.strftime("%H:%M")
        return self.theme_config.trading_start_time <= current_time <= self.theme_config.trading_end_time

    def calculate_score(self, symbol: str) -> float:
        """신호 점수 계산"""
        if self._theme_detector:
            theme_score = self._theme_detector.get_theme_score(symbol)
        else:
            theme_score = 0.0

        indicators = self.get_indicators(symbol)
        if not indicators:
            return theme_score

        change_pct = indicators.get("change_1d", 0)
        vol_ratio = indicators.get("vol_ratio", 0)

        return self._calculate_entry_score(theme_score, change_pct, vol_ratio)

    def update_themes(self, themes: List):
        """테마 정보 업데이트"""
        for theme in themes:
            t_score = getattr(theme, 'score', 0)
            t_name = getattr(theme, 'name', str(theme))
            if t_score >= self.theme_config.min_theme_score:
                self._active_themes[t_name] = theme
            elif t_name in self._active_themes:
                del self._active_themes[t_name]

    def get_active_themes(self) -> List[str]:
        """활성 테마 목록"""
        return list(self._active_themes.keys())

    def get_theme_stocks(self) -> Dict[str, List[str]]:
        """테마별 관련 종목"""
        result = {}
        for theme_name, theme in self._active_themes.items():
            stocks = getattr(theme, 'related_stocks', None) or theme.get("related_stocks", [])
            result[theme_name] = stocks
        return result

    async def _refresh_supply_demand(self):
        """외국인/기관 수급 데이터 캐시 갱신 (10분 주기)"""
        if not self._kis_market_data:
            return

        now = datetime.now()
        if self._foreign_cache:
            first = next(iter(self._foreign_cache.values()), {})
            updated = first.get("updated")
            if updated and (now - updated).total_seconds() < 600:
                return

        try:
            foreign_kospi = await self._kis_market_data.fetch_foreign_institution(market="0001", investor="1") or []
            foreign_kosdaq = await self._kis_market_data.fetch_foreign_institution(market="0002", investor="1") or []
            self._foreign_cache.clear()
            for item in foreign_kospi + foreign_kosdaq:
                sym = item.get("symbol", "")
                net_buy = item.get("net_buy_qty", 0)
                if sym:
                    self._foreign_cache[sym] = {"net_buy": net_buy, "updated": now}

            inst_kospi = await self._kis_market_data.fetch_foreign_institution(market="0001", investor="2") or []
            inst_kosdaq = await self._kis_market_data.fetch_foreign_institution(market="0002", investor="2") or []
            self._institution_cache.clear()
            for item in inst_kospi + inst_kosdaq:
                sym = item.get("symbol", "")
                net_buy = item.get("net_buy_qty", 0)
                if sym:
                    self._institution_cache[sym] = {"net_buy": net_buy, "updated": now}

            logger.debug(
                f"[테마 추종] 수급 캐시 갱신: 외국인 {len(self._foreign_cache)}종목, "
                f"기관 {len(self._institution_cache)}종목"
            )
        except Exception as e:
            logger.warning(f"[테마 추종] 수급 데이터 조회 실패 (무시): {e}")

    def _get_supply_demand_bonus(self, symbol: str) -> tuple:
        """외국인/기관 수급 기반 신뢰도 보너스/페널티"""
        foreign_data = self._foreign_cache.get(symbol)
        inst_data = self._institution_cache.get(symbol)

        foreign_buy = foreign_data.get("net_buy", 0) if foreign_data else 0
        inst_buy = inst_data.get("net_buy", 0) if inst_data else 0

        bonus = 0.0
        info_parts = []
        should_block = False

        if foreign_buy < 0 and inst_buy < 0:
            bonus = -10.0
            info_parts.append(f"외국인+기관 동시 순매도 주의")
        elif foreign_buy > 0 and inst_buy > 0:
            bonus = 10.0
            info_parts.append(f"외국인+기관 순매수")
        elif foreign_buy > 0:
            bonus = 5.0
            info_parts.append(f"외국인 순매수")
        elif inst_buy > 0:
            bonus = 5.0
            info_parts.append(f"기관 순매수")

        info = ", ".join(info_parts) if info_parts else ""
        return bonus, info, should_block

    def _cleanup_position_theme(self, symbol: str):
        """포지션 청산 시 테마 매핑 정리"""
        if symbol in self._position_themes:
            theme_name = self._position_themes.pop(symbol)
            logger.debug(f"[테마 추종] 포지션-테마 매핑 해제: {symbol} <- {theme_name}")

    def on_position_closed(self, symbol: str):
        """포지션 청산 콜백 (외부에서 호출)"""
        self._cleanup_position_theme(symbol)
