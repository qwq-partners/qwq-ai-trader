"""
주문 POST 접수 불명(UNKNOWN) 장부 — 오늘 하루치만 (2026-09-29).

KIS 주문 POST 가 서버에 닿은 뒤 응답을 잃으면 접수됐는지 알 수 없다. 재전송하지 않고 그날은 이렇게 막는다.
- BUY 불명이 하나라도 있으면 그날 신규 BUY 를 전부 보류한다.
- SELL 불명 종목은 그날 **분할** SELL 재발행만 금지한다. 전량 재발행은 KIS 매도가능수량 검사가 막는다(I1).

해제는 날짜가 바뀔 때만 일어난다. 봇의 KRX 당일 주문은 그날 소멸한다(I2). 장중 자동 해제는 없다.
파일은 같은 날 재시작용이다. 실행 중인 프로세스끼리는 상태를 공유하지 않는다.
설계: docs/superpowers/specs/2026-09-29-order-post-unknown-design.md

위치: ~/.cache/ai_trader/order_unknown.json (기록은 같은 폴더의 order_unknown.json.lock 으로 프로세스 간 직렬화)
형식: {"date": "YYYY-MM-DD", "entries": [{"side", "symbol", "qty", "reason", "at"}]}
수동 해제: 파일 삭제 후 장 마감 뒤 봇 재시작 (docs/operations/runbook.md)
"""

from __future__ import annotations

import fcntl
import json
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from loguru import logger

from ..utils.atomic_io import atomic_write_json

# 브로커가 접수 불명 주문에 돌려주는 실패 문자열의 접두어 — `(False, PREFIX + " " + 상세)`
PREFIX = "[접수불명]"

# 손상 파일(오늘 수정)은 전 방향·전 종목 불명으로 본다
_ANY = "*"


def default_path() -> Path:
    """운영 장부 경로 — 호출 시점의 Path.home() 으로 계산한다(시험의 home 패치로 격리된다)."""
    return Path.home() / ".cache" / "ai_trader" / "order_unknown.json"


class UnknownOrderBook:
    """오늘의 접수 불명 주문 장부. 생성 시 파일을 한 번 읽는다."""

    def __init__(self, path: Path, now: Optional[datetime] = None):
        self._path = Path(path)
        self._day = (now or datetime.now()).date().isoformat()
        self._entries: List[dict] = self._read(self._day)

    def _read(self, today: str) -> List[dict]:
        """파일의 오늘 항목. 없음·다른 날은 빈 목록. 손상은 mtime 이 오늘이면 전면 표식 1건, 과거면 무시."""
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            entries = data["entries"]
            if not isinstance(entries, list) or not all(isinstance(e, dict) for e in entries):
                raise ValueError(f"entries 형식 오류 ({type(entries).__name__})")
            return list(entries) if data["date"] == today else []
        except FileNotFoundError:
            return []
        except Exception as e:
            try:
                mtime = datetime.fromtimestamp(self._path.stat().st_mtime)
            except OSError:
                mtime = None
            if mtime is None or mtime.date().isoformat() != today:
                logger.warning(f"[접수불명] 장부 파일 손상(오늘 수정 아님) — 무시: {e}")
                return []
            logger.error(f"[접수불명] 장부 파일 손상(오늘 수정) — 오늘 신규 매수 보류·전 종목 분할 매도 금지: {e}")
            return [{"side": _ANY, "symbol": _ANY, "qty": 0, "reason": f"장부 파일 손상: {e}",
                     "at": mtime.isoformat(timespec="seconds")}]

    def _roll(self, now: datetime) -> str:
        today = now.date().isoformat()
        if today != self._day:
            self._day, self._entries = today, []
        return today

    def record(self, side: str, symbol: str, qty: int, reason: str, now: datetime) -> bool:
        """오늘 항목을 추가한다. 반환: 파일 저장 성공 여부(잠금·저장이 실패해도 메모리 상태는 유지).

        재읽기→병합→쓰기 전체를 `<path>.lock` 의 배타 잠금(fcntl.flock, 블로킹) 안에서 한다 — 봇과 CLI 가 같은
        파일을 동시에 써도 한쪽이 읽은 뒤 다른 쪽이 쓴 기록을 덮지 않는다.
        """
        today = self._roll(now)
        entry = {"side": side, "symbol": symbol, "qty": qty, "reason": reason,
                 "at": now.isoformat(timespec="seconds")}
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with open(self._path.with_name(self._path.name + ".lock"), "a") as lock:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX)   # 닫힐 때 풀린다
                self._entries += [e for e in self._read(today) if e not in self._entries]
                self._entries.append(entry)
                atomic_write_json(self._path, {"date": today, "entries": self._entries}, indent=1)
            return True
        except Exception as e:
            if entry not in self._entries:
                self._entries.append(entry)
            logger.error(f"[접수불명] 장부 저장 실패 — 이 프로세스 안에서만 보류된다(재시작 보호 없음): {e}")
            return False

    def buy_hold_reason(self, now: datetime) -> Optional[str]:
        """오늘 BUY 불명이 하나라도 있으면 보류 사유, 없으면 None."""
        self._roll(now)
        for e in self._entries:
            if e.get("side") in ("buy", _ANY):
                if e.get("symbol") == _ANY:   # 실제 주문이 아닌 전면 보류(조회 실패·장부 손상) — 사유만
                    return f"접수 불명 보류: {e.get('reason')} — 신규 매수 보류(날짜 변경 시 해제)"
                return (f"접수 불명 보류: 오늘 매수 {e.get('symbol')} {e.get('qty')}주 접수 불명"
                        f"({e.get('reason')}) — 신규 매수 보류(날짜 변경 시 해제)")
        return None

    def has_unknown_sell(self, symbol: str, now: datetime) -> bool:
        """오늘 이 종목의 SELL 불명이 있는가."""
        self._roll(now)
        return any(e.get("side") in ("sell", _ANY) and e.get("symbol") in (symbol, _ANY)
                   for e in self._entries)
