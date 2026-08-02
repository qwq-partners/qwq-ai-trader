"""
Kill Switch — 파일 존재만으로 주문을 즉시 차단하는 최후 안전장치.

봇 재시작·코드 수정 없이 파일 하나로 거래를 멈춘다.
엔진이 응답하지 않거나 오작동 중일 때도 동작해야 하므로,
모든 주문이 반드시 통과하는 브로커 계층(KR submit_order / US _submit_order)에서 검사한다.

플래그 파일 (~/.cache/ai_trader/):
    KILL_SWITCH       — 신규 매수 차단, 매도(청산)는 허용   ← 기본 대응
    KILL_SWITCH_ALL   — 매수·매도 전면 차단 (완전 동결)

사용:
    touch ~/.cache/ai_trader/KILL_SWITCH                     # 매수 중단
    echo "급락장 수동 개입" > ~/.cache/ai_trader/KILL_SWITCH  # 사유 기록(로그에 표시됨)
    rm ~/.cache/ai_trader/KILL_SWITCH                        # 해제

시장별 개별 차단이 필요하면 접미사를 붙인다 (KILL_SWITCH_KR / KILL_SWITCH_US).

⚠️ KILL_SWITCH_ALL은 손절·청산까지 막으므로 하락 노출이 무한정 열린다.
   포지션을 정리한 뒤 동결하거나, 청산이 필요하면 KILL_SWITCH(매수만 차단)를 쓸 것.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Tuple

from loguru import logger

CACHE_DIR = Path.home() / ".cache" / "ai_trader"

# 플래그 파일명
FLAG_HALT_BUY = "KILL_SWITCH"
FLAG_HALT_ALL = "KILL_SWITCH_ALL"

# 파일 stat 결과 캐시 (초). 매 주문마다 디스크를 때리지 않도록 최소한만 둔다.
# 긴급 상황에서 반응이 늦으면 안 되므로 짧게 유지한다.
_CACHE_TTL = 2.0

_cache: dict = {}  # {flag_name: (checked_at, exists, reason)}


def _read_flag(name: str) -> Tuple[bool, str]:
    """플래그 파일 존재 여부와 사유를 반환 (TTL 캐시 적용)."""
    now = time.monotonic()
    cached = _cache.get(name)
    if cached is not None and (now - cached[0]) < _CACHE_TTL:
        return cached[1], cached[2]

    path = CACHE_DIR / name
    exists = False
    reason = ""
    try:
        if path.exists():
            exists = True
            try:
                # 사유는 선택 사항 — 비어 있어도 무방
                reason = path.read_text(encoding="utf-8", errors="replace").strip()[:200]
            except OSError:
                reason = ""
    except OSError as e:
        # 파일시스템 오류 시에는 차단하지 않는다 (오탐으로 거래가 멈추는 것을 방지)
        logger.warning(f"[킬스위치] 플래그 확인 실패 ({name}): {e}")

    _cache[name] = (now, exists, reason)
    return exists, reason


def check(side: str, market: str = "KR") -> Tuple[bool, str]:
    """
    주문 허용 여부를 판정한다.

    Args:
        side: "buy" | "sell" (OrderSide.value 그대로 넣어도 됨)
        market: "KR" | "US"

    Returns:
        (허용 여부, 차단 사유) — 허용이면 (True, "")
    """
    market = (market or "KR").upper()
    is_buy = str(side).lower().endswith("buy")

    # 1) 전면 동결 (전역 → 시장별)
    for flag in (FLAG_HALT_ALL, f"{FLAG_HALT_ALL}_{market}"):
        exists, reason = _read_flag(flag)
        if exists:
            msg = f"킬스위치 전면 동결({flag})"
            if reason:
                msg += f": {reason}"
            return False, msg

    # 2) 신규 매수만 차단 — 청산은 살려둔다
    if is_buy:
        for flag in (FLAG_HALT_BUY, f"{FLAG_HALT_BUY}_{market}"):
            exists, reason = _read_flag(flag)
            if exists:
                msg = f"킬스위치 매수 차단({flag})"
                if reason:
                    msg += f": {reason}"
                return False, msg

    return True, ""


def is_active(market: str = "KR") -> bool:
    """킬스위치가 하나라도 켜져 있는지 (대시보드/헬스체크용)."""
    allowed_buy, _ = check("buy", market)
    allowed_sell, _ = check("sell", market)
    return not (allowed_buy and allowed_sell)


def status(market: str = "KR") -> dict:
    """현재 상태 요약 (대시보드 노출용)."""
    market = (market or "KR").upper()
    halt_all, all_reason = _read_flag(FLAG_HALT_ALL)
    if not halt_all:
        halt_all, all_reason = _read_flag(f"{FLAG_HALT_ALL}_{market}")
    halt_buy, buy_reason = _read_flag(FLAG_HALT_BUY)
    if not halt_buy:
        halt_buy, buy_reason = _read_flag(f"{FLAG_HALT_BUY}_{market}")

    return {
        "market": market,
        "halt_all": halt_all,
        "halt_buy": halt_buy or halt_all,
        "reason": all_reason or buy_reason or "",
        "active": halt_all or halt_buy,
    }


def clear_cache() -> None:
    """TTL 캐시 무효화 (테스트/즉시 반영용)."""
    _cache.clear()
