"""데이터 신선도 유틸 (T9 요청 4, 2026-09-14)

"조회 성공"과 "현재 판단에 유효"는 다른 개념이다. 08:20에 조회한 KOSPI 5일/20일
수치를 12:00 레짐 재분류에 그대로 재사용하면 "조회는 성공"했지만 "그 시점 판단에는
더 이상 유효하지 않다". 이 모듈은 값 하나마다 기준시각(as_of)·출처·세션·유효기간(ttl)을
붙여 두 개념을 코드 레벨에서 구분하는 단일 출처다.

원칙: 모르는 것을 0·중립·높은 확신으로 포장하지 않는다. 결측은 `missing()`으로
명시하고, 신선도는 `is_fresh()`로만 판정한다(추측·임계값 하드코딩 금지).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Optional, Tuple

# 전문가 confidence 상한 — 자료 상태별 (2026-09-14 T9 요청 4)
# 근거: insufficient(핵심 입력 전부 결측)는 사실상 추측이므로 0.2 이상 확신 금지.
# partial(일부 결측)은 판단은 가능하되 과신 방지 상한을 둔다. 상한값은 임의가 아니라
# cross_validator의 bear_consensus 임계(orchestrator.bear_consensus 기본
# threshold_confidence=0.7)와 동일하게 맞춘다 — 부분 결측 상태에서도 "위험 방향으로
# 안전하게 강등"하는 방어 합의는 여전히 도달 가능해야 하고(과소 방어 금지), 반면
# 완전한 자료 없이 그 이상(0.7 초과)의 확신은 금지한다. 값 자체는 이 정책 문서(T9)에
# 근거 — 표본 축적 후 재검토 가능(2026-09-14 리뷰 advisory 반영, 0.5→0.7).
CONFIDENCE_CAP_INSUFFICIENT = 0.2
CONFIDENCE_CAP_PARTIAL = 0.7

# 수동 오버라이드 등 valid_until이 없는 항목의 기본 유효기간
DEFAULT_OVERRIDE_TTL_DAYS = 14


@dataclass(frozen=True)
class DataPoint:
    """단일 자료값 + 신선도 메타데이터.

    value가 None이면 결측(직접 만들지 말고 missing()을 쓸 것).
    as_of가 None이면 신선도를 판정할 기준시각이 없다는 뜻으로 항상 not fresh.
    """

    value: Any
    as_of: Optional[datetime]
    source: str
    session: Optional[str] = None       # 예: "night"/"day"/"us_close" 등 자료가 속한 세션
    ttl_seconds: Optional[int] = None   # None이면 명시적 TTL 없음(값+as_of만 있으면 fresh)
    missing_reason: Optional[str] = None

    @property
    def is_missing(self) -> bool:
        return self.value is None


def missing(source: str, reason: str, session: Optional[str] = None) -> DataPoint:
    """결측 DataPoint 생성 — 0/중립/기본값 대신 이걸 쓴다."""
    return DataPoint(value=None, as_of=None, source=source, session=session, missing_reason=reason)


def is_fresh(dp: DataPoint, now: Optional[datetime] = None) -> bool:
    """dp가 now 시점 판단에 쓸 만큼 신선한지.

    결측이거나 as_of가 없으면 항상 False(판단 근거로 쓸 수 없음).
    ttl_seconds가 None이면 값+as_of만 있으면 fresh(만료 개념 없는 자료).
    """
    if dp.is_missing or dp.as_of is None:
        return False
    now = now or datetime.now()
    # 2026-09-15 (T10 B 리뷰 반영·2차 advisory): 미래 시각 as_of는 ttl 유무와
    # 무관하게 거부한다 — F17은 "미래 시각 자료는 정상 자료로 세지 않는다"를
    # 무조건 요구하는데, ttl_seconds가 없는 자료(예: 수동 오버라이드)는 이 검사
    # 없이는 age 계산 자체를 건너뛰어 미래 as_of도 fresh로 통과했었다.
    if dp.as_of > now:
        return False
    if dp.ttl_seconds is None:
        return True
    age = (now - dp.as_of).total_seconds()
    return 0 <= age <= dp.ttl_seconds


def freshness_label(dp: DataPoint, now: Optional[datetime] = None) -> str:
    """사람이 읽는 신선도 라벨.

    예: "as_of 08:20 · 3h 전", "as_of 08:20 · 3h 전 · 만료", "결측 (사유: ...)"
    """
    if dp.is_missing:
        reason = dp.missing_reason or "사유 미기록"
        return f"결측 ({reason})"
    now = now or datetime.now()
    if dp.as_of is None:
        return f"기준시각 없음 (source={dp.source})"
    age_label = _format_age((now - dp.as_of).total_seconds())
    label = f"as_of {dp.as_of.strftime('%H:%M')} · {age_label} 전"
    if not is_fresh(dp, now):
        label += " · 만료"
    return label


def _format_age(seconds: float) -> str:
    seconds = max(0.0, seconds)
    if seconds < 60:
        return f"{int(seconds)}초"
    minutes = seconds / 60
    if minutes < 60:
        return f"{int(minutes)}분"
    hours = minutes / 60
    if hours < 24:
        return f"{hours:.1f}h"
    return f"{hours / 24:.1f}일"


# ─────────────────────────────────────────────────────────────────
# KRX 야간선물(CM) 세션 as_of 도출 (T10 F17 — 정책값, 2026-09-15 사용자 승인)
#
# 근거: KIS 야간선물 조회 API(inquire-price)는 체결시각 필드를 주지 않는다. 대신
# 어느 시장구분(CM=야간/F=주간)이 응답했는지는 알 수 있다. 세션 개장 중 조회는
# 실시간 호가이므로 as_of=조회 시각. 세션 종료 후 조회는 직전 완료된 세션의
# 마지막 체결가이므로 as_of=그 세션 종료 시각. 유효기간(ttl)은 as_of부터 다음
# 세션 개장(18:00)까지.
#
# 세션 존재 규칙 (KRX 파생상품시장 야간거래 안내·FAQ 2025-04-28, 2026-09-15 확인):
#   - 거래시간 18:00 ~ 익일 06:00 KST (호가 접수 17:50~). 초안의 05:00 은 구 기준이라
#     06:00 으로 정정(사실 정정).
#   - 휴장 여부는 **야간거래 개시일(저녁 18:00 이 속한 날)** 기준으로 판정한다:
#     개시일이 KRX 거래일이면 익일이 공휴일이어도 개장(예: 연휴 전날 저녁),
#     개시일이 주말·공휴일이면 그날 저녁 세션은 없다(금요일 세션은 토요일 06:00 까지).
#   - 공휴일 캘린더는 `src.utils.session.is_kr_market_holiday`(동적 조회 우선, 실패 시
#     내장 폴백)를 쓴다 — 2026-09-15 사용자 승인으로 연동.
# ─────────────────────────────────────────────────────────────────
KR_NIGHT_SESSION_START_HOUR = 18   # KST, 세션 개장
KR_NIGHT_SESSION_END_HOUR = 6      # KST, 익일 세션 종료 (KRX 현행 06:00)


def _night_session_opens_on(d) -> bool:
    """d(날짜) 저녁에 야간 세션이 개시되는가 — KRX 거래일(주말·공휴일 제외)이면 True."""
    try:
        from .session import is_kr_market_holiday   # 지연 import (core.types 의존 회피)
    except ImportError:                              # 스크립트로 직접 실행(demo)한 경우
        from src.utils.session import is_kr_market_holiday
    return not is_kr_market_holiday(d)


def kr_night_futures_as_of(
    now: datetime, session: Optional[str]
) -> Tuple[Optional[datetime], Optional[str], Optional[int]]:
    """KRX 야간선물 세션 규칙으로 (as_of, as_of_note, ttl_seconds)를 도출한다.

    session이 "night"가 아니면(F/주간 폴백·미상) 시장 시각을 알 수 없으므로
    (None, 사유, None)을 반환한다 — 조회 시각을 시장 시각처럼 포장하지 않는다.
    세션 존재 여부는 개시일 기준 KRX 거래일 판정(주말+공휴일 캘린더)을 따른다.
    """
    if session != "night":
        return None, "주간(F)/미상 세션 — 야간 시장 시각 아님", None

    today = now.date()
    if now.hour >= KR_NIGHT_SESSION_START_HOUR:
        in_session = _night_session_opens_on(today)
    elif now.hour < KR_NIGHT_SESSION_END_HOUR:
        in_session = _night_session_opens_on(today - timedelta(days=1))   # 전날 저녁 세션의 연장
    else:
        in_session = False

    if in_session:
        as_of = now
    else:
        # 직전 완료된 세션의 개시일을 역산 — 주말·공휴일은 세션이 없으므로 건너뛴다.
        # 낮(06:00~18:00)이면 전날 저녁 세션이 후보, 새벽(<06:00, 세션 없음)이면 이틀 전.
        d = today - timedelta(days=1) if now.hour >= KR_NIGHT_SESSION_END_HOUR else today - timedelta(days=2)
        for _ in range(30):
            if _night_session_opens_on(d):
                break
            d -= timedelta(days=1)
        as_of = datetime.combine(d, datetime.min.time()) + timedelta(
            days=1, hours=KR_NIGHT_SESSION_END_HOUR
        )

    # 다음 세션 개장: as_of 이후 첫 거래일 18:00
    next_day = as_of.date() if as_of.hour < KR_NIGHT_SESSION_START_HOUR else as_of.date() + timedelta(days=1)
    for _ in range(30):
        if _night_session_opens_on(next_day):
            break
        next_day += timedelta(days=1)
    next_open = datetime.combine(next_day, datetime.min.time()) + timedelta(hours=KR_NIGHT_SESSION_START_HOUR)
    ttl_seconds = int((next_open - as_of).total_seconds())
    return as_of, None, ttl_seconds


def demo() -> None:
    """비-테스트 환경에서 수동 확인용 self-check (pytest 없이 python -m 실행 가능)."""
    now = datetime(2026, 9, 14, 12, 0, 0)
    fresh = DataPoint(value=1.0, as_of=datetime(2026, 9, 14, 11, 30), source="x", ttl_seconds=3600)
    stale = DataPoint(value=1.0, as_of=datetime(2026, 9, 14, 8, 20), source="x", ttl_seconds=3600)
    gone = missing("y", "조회 실패")

    assert is_fresh(fresh, now) is True
    assert is_fresh(stale, now) is False
    assert is_fresh(gone, now) is False
    assert "만료" not in freshness_label(fresh, now)
    assert "만료" in freshness_label(stale, now)
    assert "결측" in freshness_label(gone, now)

    # kr_night_futures_as_of — 세션 규칙 self-check
    assert kr_night_futures_as_of(now, "day") == (None, "주간(F)/미상 세션 — 야간 시장 시각 아님", None)
    # 세션 개장 중(20:00) 조회 — 실시간 호가, as_of=now
    live = kr_night_futures_as_of(datetime(2026, 9, 14, 20, 0), "night")
    assert live[0] == datetime(2026, 9, 14, 20, 0) and live[2] > 0
    # 월요일(2026-09-14는 월) 07:30 조회 — 금요일(09-11) 야간 세션이 토요일 06:00에 종료,
    # 주말엔 세션이 없으므로 그 값이 월요일 아침까지 그대로 유효해야 한다.
    monday_morning = kr_night_futures_as_of(datetime(2026, 9, 14, 7, 30), "night")
    assert monday_morning[0] == datetime(2026, 9, 12, 6, 0)  # 09-12(토) 06:00 = 금요일 세션 종료
    assert monday_morning[2] > 0  # 아직 만료 전(ttl>0)
    print("data_freshness demo OK")


if __name__ == "__main__":
    import sys
    from pathlib import Path as _P
    sys.path.insert(0, str(_P(__file__).resolve().parents[2]))   # 저장소 루트 (src 패키지 import)
    demo()
