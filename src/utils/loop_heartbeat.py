"""루프 하트비트 레지스트리 (2026-09-13 리뷰 "조용한 열화")

`_supervised`는 **죽은** 루프만 재기동한다. 살아 있지만 일을 못 하는 루프
(HTTP 500 재시도 폭풍 14일, 수확 shadow 2일 무동작, daily_bias 7/2 정체)는
각 루프가 **성공한 반복**마다 `beat()`를 찍고, 60초 감시 루프가 나이를 비교해 잡는다.
프로세스 내 메모리 전용 — 재시작하면 기산점은 프로세스 시작 시각.
"""

import time
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
}
# 일 1회 잡 — 직전 거래일 자정 이후 beat가 없으면 정체 (26h 상당, 주말·공휴일 오탐 없음)
DAILY = ("kr_harvest_shadow", "kr_vol_targeting", "kr_evolution_scheduler")
# 장중 루프 점검 창 — 정규장(모든 장중 루프가 활동하는 구간)
INTRADAY_WINDOW = ("09:00", "15:20")

_beats: Dict[str, float] = {}
_started: float = time.time()


def beat(name: str) -> None:
    """루프가 한 번의 반복을 성공적으로 마쳤을 때 호출"""
    _beats[name] = time.time()


def snapshot(now: Optional[float] = None) -> Dict[str, int]:
    """등록된 모든 루프의 마지막 beat 이후 경과(초) — 대시보드 `/api/health` 노출용"""
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
    - 거래일 정규장(09:00~15:20): 장중 루프를 주기×3(최소 120초)로, 09:00 기산
    - 거래일 종일: 일 1회 잡은 직전 거래일 자정 이후 beat 없으면 정체
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
        out.update(stale(ts, {n: max(p * 3, 120) for n, p in PERIODS.items()}, floor=open_ts))

    prev = now.date() - timedelta(days=1)
    while is_holiday(prev):
        prev -= timedelta(days=1)
    prev_midnight = datetime.combine(prev, datetime.min.time()).timestamp()
    out.update(stale(ts, {n: ts - prev_midnight for n in DAILY}))
    return out
