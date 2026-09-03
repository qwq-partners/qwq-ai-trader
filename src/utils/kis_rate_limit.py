"""KIS REST 프로세스 공용 초당 호출 리미터 (2026-09-03)

브로커(kis_kr)·시세(kis_market_data)·스크리너(kr_screener)가 같은 appkey로 각자 aiohttp
세션을 쓰므로 KIS 게이트웨이 한도(실전 20/s)는 **합산**으로 걸린다. 모듈별 세마포어/리미터만
으로는 배치·스크리닝 버스트가 겹칠 때 HTTP 500 EGW00201("초당 거래건수를 초과")이 나고,
kis_market_data 쪽은 재시도가 없어 업종지수·외국인 동향 조회가 그대로 실패했다
(배포 후 실측: 07:49~08:47 23건, 전부 시세 TR).

- 슬라이딩 윈도우 15/s — check-and-append 사이에 await가 없어 asyncio 단일 루프에서 Lock 없이
  원자적이다. 18/s로도 재시작 스크리닝 버스트에서 EGW00201 7건/3분이 남았다(2026-09-03 15:42
  실측): 윈도우는 전송 시각 기준이라 도착 지터(±수백 ms)로 서버측 1초 버킷에서 20을 넘는다.
  15/s로 낮춰도 8건, 67ms 페이싱을 더해도 8건이 **1~2초 간격으로 고르게** 재현(15:52 스크리닝
  ~250건 중 3% 거절) → 총량·집중도가 아니라 정속 15/s 자체가 게이트웨이 실효 한도(도착 지터
  포함)를 넘는다. 10/s(100ms 간격)로 하향 — 스크리닝 ~19s → ~28s.
  ponytail: 고정 10/s — 더 줄여야 하면 EGW00201 수신 시 전역 hold(적응형)로 격상.
- 원장(계좌) TR은 계좌당 초당 1건 추가 제한(EGW00215) → 원장 TR 간 1.05초 간격.
  응답 수신 시각으로 재스탬프하려면 `stamp_ledger()`.
"""

from __future__ import annotations

import asyncio
import collections
import time

MAX_RPS = 10
MIN_GAP = 1.0 / MAX_RPS   # 연속 호출 최소 간격 — 버스트를 초당 한도 안에서 고르게 분산
# 원장 조회 TR: 잔고 TTTC8434R / 매수가능 TTTC8908R / 체결 TTTC8001R / 미체결 TTTC8036R / 해외잔고
LEDGER_TR_IDS = frozenset({"TTTC8434R", "TTTC8908R", "TTTC8001R", "TTTC8036R", "TTTS3012R", "VTTS3012R"})
LEDGER_MIN_INTERVAL = 1.05

_calls: collections.deque = collections.deque(maxlen=MAX_RPS)
_state = {"ledger_last": 0.0, "last_send": 0.0}


def is_ledger(tr_id: str) -> bool:
    return tr_id in LEDGER_TR_IDS


def stamp_ledger() -> None:
    """원장 TR 응답 수신 시각 기록 (전송 시각만 쓰면 서버 도착 간격이 1초 미만이 될 수 있음)"""
    _state["ledger_last"] = time.monotonic()


async def acquire(tr_id: str = "") -> None:
    """호출 직전 대기. tr_id가 원장 TR이면 원장 간격까지 보장."""
    ledger = tr_id in LEDGER_TR_IDS
    while True:
        now = time.monotonic()
        while _calls and now - _calls[0] > 1.0:
            _calls.popleft()
        wait = 0.0
        if len(_calls) >= MAX_RPS:
            wait = 1.0 - (now - _calls[0])
        elif ledger and now - _state["ledger_last"] < LEDGER_MIN_INTERVAL:
            wait = LEDGER_MIN_INTERVAL - (now - _state["ledger_last"])
        elif now - _state["last_send"] < MIN_GAP:
            wait = MIN_GAP - (now - _state["last_send"])
        if wait <= 0:
            _calls.append(now)
            _state["last_send"] = now
            if ledger:
                _state["ledger_last"] = now
            return
        await asyncio.sleep(wait)


def reset() -> None:
    """테스트 전용 — 윈도우·원장 시각 초기화"""
    _calls.clear()
    _state["ledger_last"] = 0.0
    _state["last_send"] = 0.0
