"""루프 하트비트 레지스트리 (2026-09-13 리뷰 "조용한 열화", 2026-09-14 리뷰 F5 후속)

`_supervised`는 **죽은** 루프만 재기동한다. 살아 있지만 일을 못 하는 루프
(HTTP 500 재시도 폭풍 14일, 수확 shadow 2일 무동작, daily_bias 7/2 정체)는
각 루프가 **성공한 반복**마다 `beat()`를 찍고, 60초 감시 루프가 나이를 비교해 잡는다.
프로세스 내 메모리 전용 — 재시작하면 기산점은 프로세스 시작 시각.

09-13 최초 버전은 "실패를 삼킨 뒤 beat" 패턴(DART 조회 전 beat, REST 전량 실패에도
beat, 진화 스케줄러 예외 삼킴 후 beat)에 무방비였다 — 40분간 5회 전부 실패해도
정체로 안 잡혔다 (F5). 이번 버전은 성공/실패/유휴를 명시적으로 구분한다:

- `record_attempt(name)`: 한 번의 반복을 시작
- `record_success(name)`: 반복이 실제로 할 일을 완수 (staleness 기준 갱신)
- `record_failure(name, reason)`: 반복이 실패 (staleness 기준 **갱신 안 함** — 나이가 계속 쌓여야 정체로 잡힘)
- `record_idle(name, reason)`: 할 일이 없어서 정상적으로 아무것도 안 함 (staleness 기준 갱신, 실패 아님)
- `set_enabled(name, enabled, reason)`: 설정으로 꺼진 기능은 정체 경보에서 제외.
  **초기화 실패는 enabled=False로 위장하지 않는다** — 그 경우 그냥 beat가 안 찍혀 정체로 드러나야 한다.

`beat(name)`은 `record_success`의 별칭으로 남아 기존 호출부와 호환된다.
"""

import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable, Dict, Optional

# 장중 루프 기대 주기(초). 정체 판정 = 주기×3 (최소 120초).
PERIODS: Dict[str, int] = {
    "kr_fill_checker": 15,       # 유휴 폴링 15초
    "kr_portfolio_sync": 30,
    "kr_screener": 300,          # screener.scan_interval_minutes 5
    "kr_rest_price_feed": 20,
    "kr_market_trend": 120,
    "kr_dart_alert": 600,
    "kr_toss_parity": 300,        # T12 Phase 1 — KIS↔토스 대조 기록, 5분 주기
}
# 일 1회 잡의 예정 시각(시, 분) — 각 스케줄러 루프 내부의 실행 시각 조건과 동일 출처로
# 취급한다(하드코딩 중복 방지 목적의 단일 정의점). 실행측(`kr_scheduler.py`)도 이 값을 참조한다.
DAILY_SCHEDULE: Dict[str, tuple] = {
    "kr_harvest_shadow": (8, 40),
    "kr_vol_targeting": (8, 30),
    "kr_evolution_scheduler": (20, 30),
}
DAILY = tuple(DAILY_SCHEDULE)  # 기존 필드 호환 (이름 목록)
DAILY_GRACE_MINUTES = 60
# 장중 루프 점검 창 — 정규장(모든 장중 루프가 활동하는 구간)
INTRADAY_WINDOW = ("09:00", "15:20")

_beats: Dict[str, float] = {}  # 마지막 "정체 아님" 확인 시각(성공 또는 사유 있는 유휴) — staleness 기준
_started: float = time.time()


@dataclass
class _LoopState:
    enabled: bool = True
    idle_reason: Optional[str] = None
    last_attempt: Optional[float] = None
    last_success: Optional[float] = None
    last_failure: Optional[float] = None
    failure_reason: Optional[str] = None
    consecutive_failures: int = 0
    note: Optional[str] = None  # 부분 실패(degraded) 등 성공이지만 덧붙일 정보


_states: Dict[str, _LoopState] = {}


def _state(name: str) -> _LoopState:
    return _states.setdefault(name, _LoopState())


def record_attempt(name: str) -> None:
    """루프가 한 번의 반복(조회/작업)을 시작할 때 호출."""
    _state(name).last_attempt = time.time()


def record_success(name: str, *, note: Optional[str] = None) -> None:
    """루프가 실제로 할 일을 완수했을 때 호출 — staleness 기준(`_beats`) 갱신."""
    now = time.time()
    _beats[name] = now
    st = _state(name)
    st.last_success = now
    st.consecutive_failures = 0
    st.idle_reason = None
    st.note = note


def beat(name: str) -> None:
    """`record_success`의 별칭 — 기존 호출부 호환용."""
    record_success(name)


def record_failure(name: str, reason: Optional[str] = None) -> None:
    """루프 반복이 실패했을 때 호출 — staleness 기준은 갱신하지 않는다(나이가 계속 쌓임)."""
    st = _state(name)
    st.last_failure = time.time()
    st.failure_reason = reason
    st.consecutive_failures += 1
    st.idle_reason = None
    st.note = None


def record_idle(name: str, reason: str) -> None:
    """할 일이 없어 정상적으로 아무것도 하지 않았을 때 호출 — 실패가 아니라 정체 기준 갱신.

    `last_success`는 갱신하지 않는다(리뷰 blocking #1) — 성공 시각은 `record_success`만
    갱신한다. 유휴와 실제 완수를 last_success로 구분해야, 유휴 이후 실제 실패가 이어질 때
    '정상 동작한 적 있음'처럼 보이지 않는다.
    """
    now = time.time()
    _beats[name] = now
    st = _state(name)
    st.consecutive_failures = 0
    st.idle_reason = reason
    st.note = None


def annotate(name: str, note: str) -> None:
    """방금 기록한 success/idle/failure 판정을 덮어쓰지 않고 참고용 note만 덧붙인다.

    분류(record_success/idle/failure)가 이미 끝난 뒤, 같은 반복의 후속 블록에서 발생한
    부차적 예외를 다시 record_failure로 기록하면 이미 확정한 성공/유휴 판정을 실패로
    덮어써 버린다(리뷰 advisory (e)). 그런 경우 이 함수로 note만 남긴다 — 정체 기준·
    consecutive_failures·idle_reason은 건드리지 않는다.
    """
    st = _state(name)
    st.note = f"{st.note} · {note}" if st.note else note   # 덧붙이기 — degraded note 를 지우지 않는다 (2026-09-14 재리뷰)


def set_enabled(name: str, enabled: bool, reason: Optional[str] = None) -> None:
    """설정으로 기능이 꺼져 있음을 표시 — 정체 경보에서 제외된다.

    초기화 실패(설정은 켜져 있으나 부팅/자격증명 문제 등)는 이 함수를 호출하지 않는다.
    """
    st = _state(name)
    st.enabled = enabled
    if not enabled:
        st.idle_reason = reason
        _beats[name] = time.time()


def snapshot(now: Optional[float] = None) -> Dict[str, int]:
    """등록된 모든 루프의 마지막 beat 이후 경과(초) — 대시보드 `/api/health` 노출용 (기존 필드 호환)"""
    now = time.time() if now is None else now
    return {n: int(now - _beats.get(n, _started)) for n in (*PERIODS, *DAILY)}


def stale(now: float, thresholds: Dict[str, float], floor: float = 0.0) -> Dict[str, float]:
    """`thresholds{루프: 허용 나이(초)}`를 넘긴 루프 → {루프: 나이}.

    floor: 나이 기산점 하한(장 시작 시각) — 밤새 쉰 루프가 개장 직후 일제히 정체로
    잡히는 것을 막는다. beat 기록이 없으면 프로세스 시작 시각을 기산점으로 쓴다.
    """
    out: Dict[str, float] = {}
    for name, thr in thresholds.items():
        age = now - max(_beats.get(name, _started), floor)
        if age > thr:
            out[name] = age
    return out


def check(now: Optional[datetime] = None,
          is_holiday: Optional[Callable] = None) -> Dict[str, float]:
    """운영 규칙을 적용한 정체 루프 목록 (감시 루프·대시보드 공용).

    - 휴장일: 점검 없음
    - 비활성(`set_enabled(False)`) 루프: 점검 제외
    - 거래일 정규장(09:00~15:20): 장중 루프를 주기×3(최소 120초)로, 09:00 기산
    - 거래일 당일 예정시각+60분(`DAILY_GRACE_MINUTES`) grace 이후: 일 1회 잡이
      그 예정시각 이후 성공(또는 유휴)하지 않았으면 정체 (2026-09-14 리뷰 F5 —
      기존 "직전 거래일 자정 이후" 기준은 예정시각 이전에도 정체로 오탐했다)
    """
    now = datetime.now() if now is None else now
    if is_holiday is None:
        from ..core.engine import is_kr_market_holiday as is_holiday
    if is_holiday(now.date()):
        return {}

    ts = now.timestamp()
    out: Dict[str, float] = {}
    if INTRADAY_WINDOW[0] <= now.strftime("%H:%M") <= INTRADAY_WINDOW[1]:
        open_ts = now.replace(hour=9, minute=0, second=0, microsecond=0).timestamp()
        thresholds = {
            n: max(p * 3, 120) for n, p in PERIODS.items() if _state(n).enabled
        }
        out.update(stale(ts, thresholds, floor=open_ts))

    for name, (h, m) in DAILY_SCHEDULE.items():
        if not _state(name).enabled:
            continue
        sched = now.replace(hour=h, minute=m, second=0, microsecond=0)
        deadline = sched + timedelta(minutes=DAILY_GRACE_MINUTES)
        if now < deadline:
            continue  # grace 이내 — 아직 미완료를 정체로 보지 않음
        last = _beats.get(name, _started)
        if last < sched.timestamp():
            out[name] = ts - last
    return out


def _next_due(name: str, now: datetime) -> Optional[str]:
    if name not in DAILY_SCHEDULE:
        return None
    h, m = DAILY_SCHEDULE[name]
    sched = now.replace(hour=h, minute=m, second=0, microsecond=0)
    if now >= sched:
        sched += timedelta(days=1)
    return sched.strftime("%Y-%m-%d %H:%M")


def loop_status(now: Optional[datetime] = None) -> Dict[str, dict]:
    """`/api/health`·ops_check 용 상세 상태 — 계약(계획서 T4): enabled, idle_reason,
    last_attempt, last_success, consecutive_failures, next_due. 감시 대상 9개 한정."""
    now = datetime.now() if now is None else now
    out: Dict[str, dict] = {}
    for name in (*PERIODS, *DAILY):
        st = _state(name)
        row = {
            "enabled": st.enabled,
            "idle_reason": st.idle_reason,
            "last_attempt": st.last_attempt,
            "last_success": st.last_success,
            "consecutive_failures": st.consecutive_failures,
            "next_due": _next_due(name, now),
        }
        if st.failure_reason is not None:
            row["failure_reason"] = st.failure_reason
        if st.note is not None:
            row["note"] = st.note
        out[name] = row
    return out
