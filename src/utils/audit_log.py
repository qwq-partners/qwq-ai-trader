"""
감사 원장 (Audit Ledger) — append-only 주문 기록.

trade_journal은 "체결된 거래"를 손익 관점에서 남긴다.
감사 원장은 그보다 앞단에서 **시도된 모든 주문**을 남긴다:
제출·성공·실패·킬스위치 차단·리스크 게이트 거부까지 전부.

"왜 이 주문이 나갔나" / "왜 안 나갔나"를 사후에 재현하기 위한 기록이므로
절대 덮어쓰지 않고 append만 한다. 월별 파일로 분리한다.

위치: ~/.cache/ai_trader/audit/audit_YYYYMM.jsonl

조회:
    python -c "import json,sys; [print(json.loads(l)) for l in open(sys.argv[1])]" \
        ~/.cache/ai_trader/audit/audit_202608.jsonl
    grep '"blocked"' ~/.cache/ai_trader/audit/audit_202608.jsonl   # 차단 이력만
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from loguru import logger

AUDIT_DIR = Path.home() / ".cache" / "ai_trader" / "audit"

# 이벤트 종류
EV_SUBMIT = "submit"        # 브로커에 주문 제출 시도
EV_ACCEPT = "accept"        # 브로커 접수 성공
EV_REJECT = "reject"        # 브로커 거부 / API 오류
EV_BLOCKED = "blocked"      # 킬스위치·리스크 게이트에 의한 사전 차단
EV_CANCEL = "cancel"        # 주문 취소


def _default(o: Any) -> str:
    """Decimal·datetime 등 JSON 비직렬화 타입 폴백."""
    return str(o)


def record(event: str, **fields: Any) -> None:
    """
    감사 원장에 한 줄 기록한다.

    실패해도 매매를 막지 않는다 (기록은 부가 기능이므로 예외를 삼킨다).

    Args:
        event: EV_* 상수 중 하나
        **fields: market, symbol, side, qty, price, reason, order_id, strategy 등
    """
    try:
        now = datetime.now()
        row = {
            "ts": now.isoformat(timespec="seconds"),
            "event": event,
        }
        # None 필드는 남기지 않아 파일 크기를 줄인다
        row.update({k: v for k, v in fields.items() if v is not None})

        AUDIT_DIR.mkdir(parents=True, exist_ok=True)
        path = AUDIT_DIR / f"audit_{now:%Y%m}.jsonl"
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False, default=_default) + "\n")
    except Exception as e:
        logger.warning(f"[감사원장] 기록 실패 ({event}): {e}")


def record_blocked(market: str, symbol: str, side: str, reason: str, **extra: Any) -> None:
    """사전 차단 기록 — 킬스위치/리스크 게이트 공용."""
    record(EV_BLOCKED, market=market, symbol=symbol, side=side, reason=reason, **extra)


def read_recent(limit: int = 100, event: str | None = None) -> list[dict]:
    """
    최근 기록을 최신순으로 반환 (대시보드/점검용).

    Args:
        limit: 최대 건수
        event: 특정 이벤트만 필터링 (None이면 전체)
    """
    now = datetime.now()
    rows: list[dict] = []
    # 이번 달 → 지난 달 순으로 훑어 limit을 채운다
    months = [f"{now:%Y%m}"]
    prev = now.replace(day=1)
    prev_month = (prev.month - 1) or 12
    prev_year = prev.year - (1 if prev.month == 1 else 0)
    months.append(f"{prev_year}{prev_month:02d}")

    for m in months:
        path = AUDIT_DIR / f"audit_{m}.jsonl"
        if not path.exists():
            continue
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError as e:
            logger.warning(f"[감사원장] 읽기 실패 ({path.name}): {e}")
            continue

        for line in reversed(lines):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event is not None and row.get("event") != event:
                continue
            rows.append(row)
            if len(rows) >= limit:
                return rows

    return rows
