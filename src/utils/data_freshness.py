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
from datetime import datetime
from typing import Any, Optional

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
    print("data_freshness demo OK")


if __name__ == "__main__":
    demo()
