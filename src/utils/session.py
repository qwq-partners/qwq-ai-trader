"""
AI Trading Bot - 통합 시장 세션 유틸리티

KR(한국) 및 US(미국) 시장 세션을 통합 관리합니다.

KR 세션:
- 프리장: 08:00~08:50
- 정규장: 09:00~15:20
- 넥스트장: 15:40~20:00

US 세션:
- Pre-market: 04:00~09:30 ET
- Regular: 09:30~16:00 ET
- After-hours: 16:00~20:00 ET
"""

from datetime import date, datetime, time, timedelta
from typing import List, Optional, Set, Tuple
from zoneinfo import ZoneInfo

from loguru import logger

from src.core.types import MarketSession

# ============================================================
# 타임존 상수
# ============================================================
KST = ZoneInfo("Asia/Seoul")
ET = ZoneInfo("America/New_York")


# ============================================================
# KR 시장 휴장일 관리
# ============================================================

# KISMarketData.fetch_holidays()로 채워지는 동적 캐시
_kr_market_holidays: Set[date] = set()

# 하드코딩 공휴일 (동적 조회 실패 시 fallback) - 2026~2027년
_KR_FALLBACK_HOLIDAYS: Set[date] = {
    # 2026년
    date(2026, 1, 1),   # 신정
    date(2026, 1, 27),  # 설날 전날
    date(2026, 1, 28),  # 설날
    date(2026, 1, 29),  # 설날 다음날
    date(2026, 3, 1),   # 삼일절 (일->3/2 대체)
    date(2026, 3, 2),   # 삼일절 대체공휴일
    date(2026, 5, 5),   # 어린이날
    date(2026, 5, 24),  # 석가탄신일 (일->5/25 대체)
    date(2026, 5, 25),  # 석가탄신일 대체공휴일
    date(2026, 6, 6),   # 현충일 (토)
    date(2026, 8, 15),  # 광복절 (토)
    date(2026, 8, 17),  # 광복절 대체공휴일
    date(2026, 9, 24),  # 추석 전날
    date(2026, 9, 25),  # 추석
    date(2026, 9, 26),  # 추석 다음날 (토)
    date(2026, 10, 3),  # 개천절 (토)
    date(2026, 10, 5),  # 개천절 대체공휴일
    date(2026, 10, 9),  # 한글날
    date(2026, 12, 25), # 크리스마스
    # 2027년
    date(2027, 1, 1),   # 신정
    date(2027, 2, 8),   # 설날 전날
    date(2027, 2, 9),   # 설날
    date(2027, 2, 10),  # 설날 다음날
    date(2027, 3, 1),   # 삼일절
    date(2027, 5, 5),   # 어린이날
    date(2027, 5, 13),  # 석가탄신일
    date(2027, 6, 6),   # 현충일 (일->6/7 대체)
    date(2027, 6, 7),   # 현충일 대체공휴일
    date(2027, 8, 15),  # 광복절 (일->8/16 대체)
    date(2027, 8, 16),  # 광복절 대체공휴일
    date(2027, 10, 3),  # 개천절 (일->10/4 대체)
    date(2027, 10, 4),  # 개천절 대체공휴일
    date(2027, 10, 9),  # 한글날 (토)
    date(2027, 10, 11), # 한글날 대체공휴일
    date(2027, 10, 13), # 추석 전날
    date(2027, 10, 14), # 추석
    date(2027, 10, 15), # 추석 다음날
    date(2027, 12, 25), # 크리스마스 (토)
    date(2027, 12, 27), # 크리스마스 대체공휴일
}


def set_kr_holidays(holidays: Set[date]):
    """외부에서 조회한 KR 휴장일을 주입 (봇 시작 시 호출)"""
    global _kr_market_holidays
    _kr_market_holidays = holidays
    logger.info(f"한국 시장 휴장일 {len(holidays)}일 로드 완료")


def is_kr_market_holiday(d: date) -> bool:
    """한국 시장 휴장일 여부 (주말 + 공휴일)

    동적 데이터(API)와 Fallback(하드코딩)을 합쳐서 체크합니다.
    API는 당월/익월만 로드하므로 3개월 후 공휴일은 Fallback에서 커버합니다.
    """
    if d.weekday() >= 5:
        return True
    if _kr_market_holidays:
        return d in _kr_market_holidays or d in _KR_FALLBACK_HOLIDAYS
    # 동적 데이터가 없으면 하드코딩 공휴일 체크 (fallback)
    return d in _KR_FALLBACK_HOLIDAYS


# ============================================================
# KR 세션 관리
# ============================================================

class KRSession:
    """한국 시장 세션 관리 유틸리티"""

    # 세션 시간대 (시:분 튜플)
    PRE_MARKET_START = (8, 0)
    PRE_MARKET_END = (8, 50)
    REGULAR_START = (9, 0)
    REGULAR_END = (15, 20)  # 동시호가 전까지
    NEXT_START = (15, 40)   # 10분 휴장 후
    NEXT_END = (20, 0)

    def now_kst(self) -> datetime:
        """현재 시각 (KST)"""
        return datetime.now(KST)

    def get_session(self, dt: Optional[datetime] = None) -> MarketSession:
        """현재 시장 세션 반환

        Args:
            dt: 기준 시각 (None이면 현재 시각)

        Returns:
            MarketSession: 현재 세션 (PRE_MARKET, REGULAR, NEXT, CLOSED)
        """
        if dt is None:
            now = datetime.now()
        else:
            now = dt

        # 주말 + 공휴일
        if is_kr_market_holiday(now.date()):
            return MarketSession.CLOSED

        hour = now.hour
        minute = now.minute
        time_int = hour * 100 + minute  # HHMM 형식

        # 프리장: 08:00 ~ 08:50
        if 800 <= time_int < 850:
            return MarketSession.PRE_MARKET

        # 정규장: 09:00 ~ 15:20 (장마감 동시호가 전까지)
        if 900 <= time_int < 1520:
            return MarketSession.REGULAR

        # 15:20~15:40: CLOSED (장마감 동시호가 + 휴장)

        # 넥스트장: 15:40 ~ 20:00
        if 1540 <= time_int < 2000:
            return MarketSession.NEXT

        return MarketSession.CLOSED

    # get_current_session 하위 호환
    get_current_session = get_session

    def is_trading_hours(self, enable_pre_market: bool = True, enable_next_market: bool = True) -> bool:
        """거래 가능 시간 여부

        Args:
            enable_pre_market: 프리장 거래 활성화 여부
            enable_next_market: 넥스트장 거래 활성화 여부

        Returns:
            bool: 거래 가능 여부
        """
        session = self.get_session()

        if session == MarketSession.CLOSED:
            return False

        if session == MarketSession.PRE_MARKET and not enable_pre_market:
            return False

        if session == MarketSession.NEXT and not enable_next_market:
            return False

        return True

    def get_session_time_range(self, session: MarketSession) -> Tuple[time, time]:
        """세션의 시작/종료 시각 반환

        Args:
            session: 세션 타입

        Returns:
            (시작시각, 종료시각) 튜플
        """
        if session == MarketSession.PRE_MARKET:
            return (
                time(*self.PRE_MARKET_START),
                time(*self.PRE_MARKET_END),
            )
        elif session == MarketSession.REGULAR:
            return (
                time(*self.REGULAR_START),
                time(*self.REGULAR_END),
            )
        elif session == MarketSession.NEXT:
            return (
                time(*self.NEXT_START),
                time(*self.NEXT_END),
            )
        else:
            # CLOSED는 범위가 없음
            return (time(0, 0), time(0, 0))

    def time_to_session_end(self, session: MarketSession) -> int:
        """현재 세션 종료까지 남은 시간(초)

        Args:
            session: 현재 세션

        Returns:
            int: 남은 시간(초), CLOSED면 0
        """
        if session == MarketSession.CLOSED:
            return 0

        now = datetime.now()
        _, end_time = self.get_session_time_range(session)

        end_datetime = datetime.combine(now.date(), end_time)
        if end_datetime < now:
            return 0

        return int((end_datetime - now).total_seconds())

    def is_trading_day(self, d: Optional[date] = None) -> bool:
        """거래일 여부"""
        if d is None:
            d = datetime.now().date()
        return not is_kr_market_holiday(d)

    def next_trading_day(self, d: Optional[date] = None) -> date:
        """다음 거래일"""
        if d is None:
            d = datetime.now().date()
        candidate = d + timedelta(days=1)
        while is_kr_market_holiday(candidate):
            candidate += timedelta(days=1)
        return candidate

    def prev_trading_day(self, d: Optional[date] = None) -> date:
        """이전 거래일"""
        if d is None:
            d = datetime.now().date()
        candidate = d - timedelta(days=1)
        while is_kr_market_holiday(candidate):
            candidate -= timedelta(days=1)
        return candidate

    @staticmethod
    def format_session(session: MarketSession) -> str:
        """세션을 한글 문자열로 변환

        Args:
            session: 세션 타입

        Returns:
            str: 세션 이름 (예: "정규장", "프리장")
        """
        session_names = {
            MarketSession.PRE_MARKET: "프리장",
            MarketSession.REGULAR: "정규장",
            MarketSession.NEXT: "넥스트장",
            MarketSession.CLOSED: "휴장",
        }
        return session_names.get(session, "알 수 없음")


# ============================================================
# US 마켓 캘린더
# ============================================================

class USMarketCalendar:
    """US market calendar using exchange-calendars"""

    def __init__(self):
        self._cal = None
        self._init_calendar()

    def _init_calendar(self):
        try:
            import exchange_calendars as xcals
            self._cal = xcals.get_calendar("XNYS")  # NYSE calendar
            logger.debug("Exchange calendar loaded (XNYS)")
        except ImportError:
            logger.warning("exchange-calendars not installed, using basic weekend check")

    def is_trading_day(self, d: Optional[date] = None) -> bool:
        """Check if date is a trading day"""
        if d is None:
            d = date.today()

        # Weekend check (always)
        if d.weekday() >= 5:
            return False

        # Exchange calendar (if available)
        if self._cal is not None:
            try:
                import pandas as pd
                ts = pd.Timestamp(d)
                return self._cal.is_session(ts)
            except Exception:
                pass

        return True  # Assume trading day if no calendar

    def next_trading_day(self, d: Optional[date] = None) -> date:
        """Get next trading day"""
        if d is None:
            d = date.today()

        candidate = d + timedelta(days=1)
        while not self.is_trading_day(candidate):
            candidate += timedelta(days=1)
        return candidate

    def prev_trading_day(self, d: Optional[date] = None) -> date:
        """Get previous trading day"""
        if d is None:
            d = date.today()

        candidate = d - timedelta(days=1)
        while not self.is_trading_day(candidate):
            candidate -= timedelta(days=1)
        return candidate

    def trading_days_between(self, start: date, end: date) -> List[date]:
        """Get list of trading days in range"""
        days = []
        current = start
        while current <= end:
            if self.is_trading_day(current):
                days.append(current)
            current += timedelta(days=1)
        return days


# ============================================================
# US 세션 관리
# ============================================================

# US Regular session times
US_REGULAR_OPEN = time(9, 30)
US_REGULAR_CLOSE = time(16, 0)
US_PRE_MARKET_OPEN = time(4, 0)
US_AFTER_HOURS_CLOSE = time(20, 0)


class USSession:
    """US market session manager"""

    def __init__(self):
        self._calendar = USMarketCalendar()

    def now_et(self) -> datetime:
        """Current time in ET"""
        return datetime.now(ET)

    def now_kst(self) -> datetime:
        """Current time in KST"""
        return datetime.now(KST)

    def get_session(self, dt: Optional[datetime] = None) -> MarketSession:
        """Get current market session"""
        if dt is None:
            dt = self.now_et()
        elif dt.tzinfo is None:
            dt = dt.replace(tzinfo=ET)

        t = dt.time()
        d = dt.date()

        if not self._calendar.is_trading_day(d):
            return MarketSession.CLOSED

        if US_REGULAR_OPEN <= t < US_REGULAR_CLOSE:
            return MarketSession.REGULAR
        elif US_PRE_MARKET_OPEN <= t < US_REGULAR_OPEN:
            return MarketSession.PRE_MARKET
        elif US_REGULAR_CLOSE <= t < US_AFTER_HOURS_CLOSE:
            return MarketSession.AFTER_HOURS
        else:
            return MarketSession.CLOSED

    def is_market_open(self, dt: Optional[datetime] = None) -> bool:
        """Is regular session open?"""
        return self.get_session(dt) == MarketSession.REGULAR

    def minutes_to_close(self, dt: Optional[datetime] = None) -> float:
        """Minutes until regular session close"""
        if dt is None:
            dt = self.now_et()
        close_dt = dt.replace(hour=16, minute=0, second=0, microsecond=0)
        diff = (close_dt - dt).total_seconds() / 60
        return max(0.0, diff)

    def minutes_to_open(self, dt: Optional[datetime] = None) -> Optional[float]:
        """Minutes until next regular session open (None if market is open or closed for the day)"""
        if dt is None:
            dt = self.now_et()
        if not self.is_trading_day(dt.date()):
            return None
        open_dt = dt.replace(hour=9, minute=30, second=0, microsecond=0)
        diff = (open_dt - dt).total_seconds() / 60
        if diff < 0:
            return None  # 이미 지나감
        return diff

    def minutes_since_close(self, dt: Optional[datetime] = None) -> Optional[float]:
        """Minutes elapsed since regular session close (None if market is still open or pre-market)"""
        if dt is None:
            dt = self.now_et()
        close_dt = dt.replace(hour=16, minute=0, second=0, microsecond=0)
        diff = (dt - close_dt).total_seconds() / 60
        if diff < 0:
            return None  # 아직 장 중 or 프리마켓
        return diff

    def is_trading_day(self, d: Optional[date] = None) -> bool:
        """Is today a trading day? (NYSE 기준)"""
        if d is None:
            d = self.now_et().date()
        return self._calendar.is_trading_day(d)

    def next_trading_day(self, d: Optional[date] = None) -> date:
        """Next NYSE trading day"""
        if d is None:
            d = self.now_et().date()
        return self._calendar.next_trading_day(d)

    def prev_trading_day(self, d: Optional[date] = None) -> date:
        """Previous NYSE trading day"""
        if d is None:
            d = self.now_et().date()
        return self._calendar.prev_trading_day(d)
