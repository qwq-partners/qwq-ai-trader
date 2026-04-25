"""
매크로 이벤트 캘린더 — KR 시장 고변동성 이벤트 날짜 관리

용도: FOMC/한은 기준금리/KOSPI 옵션 만기 등 변동성 폭발 예상일에
신규 매수 한도 강제 축소 (max_daily_new_buys=1).

데이터 소스: 하드코딩된 dict (수동 갱신).
- FOMC: 연 8회 (https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm)
- 한은 금통위: 연 8회 (https://www.bok.or.kr)
- KOSPI 옵션 만기: 매월 둘째 목요일

장기적으로는 외부 캘린더 API 연동 필요.
"""

from datetime import date
from typing import Dict, List, Set
from loguru import logger


# 연도별 FOMC 일정 (수동 갱신 — 매년 11~12월에 다음 해 일정 발표됨)
_FOMC_BY_YEAR: Dict[int, Set[str]] = {
    2026: {
        "2026-01-28", "2026-03-18", "2026-04-29", "2026-06-17",
        "2026-07-29", "2026-09-16", "2026-11-04", "2026-12-16",
    },
    # 2027 일정은 연말 갱신 필요 (TODO)
}

# 연도별 한은 금통위 일정 (KR 시간)
_BOK_BY_YEAR: Dict[int, Set[str]] = {
    2026: {
        "2026-01-15", "2026-02-26", "2026-04-09", "2026-05-21",
        "2026-07-09", "2026-08-27", "2026-10-15", "2026-11-26",
    },
}


def _kospi_option_expiry(year: int, month: int) -> str:
    """매월 둘째 목요일 = KOSPI 옵션/선물 만기일"""
    # 1일이 무슨 요일인지 → 첫 번째 목요일 → 둘째 목요일
    first = date(year, month, 1)
    days_to_first_thu = (3 - first.weekday()) % 7  # weekday: Mon=0, Thu=3
    first_thu = first.replace(day=1 + days_to_first_thu)
    second_thu = first_thu.replace(day=first_thu.day + 7)
    return second_thu.isoformat()


def _kospi_option_expiry_dates(year: int) -> Set[str]:
    return {_kospi_option_expiry(year, m) for m in range(1, 13)}


_MERGED_CACHE: Dict[int, Dict[str, str]] = {}


def _build_year_events(year: int) -> Dict[str, str]:
    """연도별 매크로 이벤트 딕셔너리 생성 (지연 빌드 + 캐시)"""
    if year in _MERGED_CACHE:
        return _MERGED_CACHE[year]
    out: Dict[str, str] = {}
    for d in _FOMC_BY_YEAR.get(year, set()):
        out[d] = "FOMC"
    for d in _BOK_BY_YEAR.get(year, set()):
        out[d] = "한은 금통위"
    # 옵션만기는 매년 동적 계산 (수동 갱신 불필요)
    for d in _kospi_option_expiry_dates(year):
        out[d] = out.get(d, "옵션만기")
    _MERGED_CACHE[year] = out
    return out


def is_macro_event_day(check_date: date = None) -> tuple:
    """
    오늘(또는 지정일)이 매크로 이벤트 날짜인지 확인.

    2026-04-25 수정: 연도 무관 구조 — 연도별 빌드.
    FOMC/한은 데이터 없는 연도는 옵션만기만 자동 활성화.

    Returns:
        (event_label: str|None, is_event: bool)
    """
    if check_date is None:
        check_date = date.today()
    events = _build_year_events(check_date.year)
    iso = check_date.isoformat()
    label = events.get(iso)
    # FOMC/한은 데이터 미수록 연도 경고 (연 1회만)
    if check_date.year not in _FOMC_BY_YEAR:
        global _MISSING_DATA_WARNED
        if not _MISSING_DATA_WARNED.get(check_date.year):
            logger.warning(
                f"[매크로캘린더] {check_date.year}년 FOMC/한은 일정 미등록 — "
                f"옵션만기만 자동 활성. 연말까지 macro_calendar.py 갱신 필요."
            )
            _MISSING_DATA_WARNED[check_date.year] = True
    return (label, label is not None)


_MISSING_DATA_WARNED: Dict[int, bool] = {}


def get_event_label(check_date: date = None) -> str:
    """오늘 이벤트 라벨 (없으면 빈 문자열)"""
    label, _ = is_macro_event_day(check_date)
    return label or ""


def list_upcoming_events(days: int = 30) -> List[tuple]:
    """앞으로 N일 내 매크로 이벤트 리스트 [(date, label), ...]"""
    from datetime import timedelta
    today = date.today()
    upcoming = []
    for delta in range(days):
        d = today + timedelta(days=delta)
        events = _build_year_events(d.year)
        label = events.get(d.isoformat())
        if label:
            upcoming.append((d, label))
    return upcoming
