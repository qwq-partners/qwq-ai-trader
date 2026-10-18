"""대시보드 전용 KR 시세 캐시. 거래 경로에는 전달하지 않는다."""
import asyncio
from copy import deepcopy
from datetime import datetime
import time

from loguru import logger

from src.core.types import MarketSession
from src.utils.session import KRSession, KST, is_kr_market_holiday


def _now_kst():
    return datetime.now(KST)


def session_key():
    """호스트 시간대와 무관한 한국 거래일·세션. 공통 휴장일 판정을 쓴다."""
    now = _now_kst()
    return now.date(), KRSession().get_session(now)


class DashboardMarketCache:
    """서버별 공유 캐시. HTTP/SSE 동시 요청을 종목별 한 번으로 합친다."""

    def __init__(self):
        self._entries = {}
        self._locks = {}

    async def get(self, key, fetch):
        # 호출자는 전광판의 고정 지수2개/종목3개뿐이다.
        async with self._locks.setdefault(key, asyncio.Lock()):
            current_session = session_key()
            now = _now_kst()
            # CLOSED에는 08:50/15:20 동시호가도 포함되므로 그 시간은 늦추지 않는다.
            intraday = not is_kr_market_holiday(now.date()) and 8 <= now.hour < 20
            ttl = 300 if current_session[1] == MarketSession.CLOSED and not intraday else 10
            entry = self._entries.get(key)
            if (entry is not None and entry["session"] == current_session
                    and 0 <= time.monotonic() - entry["attempted"] < ttl):
                return deepcopy(entry["data"])

            previous = entry["data"] if entry is not None else None
            data = None
            try:
                value = await fetch()
                if isinstance(value, dict) and value.get("price", 0) > 0:
                    data = deepcopy(value)
                    data.update(fetched_at=_now_kst().isoformat(), stale=False)
            except Exception as exc:
                logger.debug(f"[대시보드 시세] 조회 실패: {type(exc).__name__}")
            # 취소는 전파하고 시도/캐시를 갱신하지 않는다.
            if data is None and previous is not None:
                data = deepcopy(previous)
                data["stale"] = True
            self._entries[key] = {
                "session": current_session, "attempted": time.monotonic(), "data": data,
            }
            return deepcopy(data)
