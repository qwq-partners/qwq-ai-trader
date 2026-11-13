"""장중 스캔 후보 전수 원장 — V02 (2026-11-13, 측정 전용).

스크리너가 반환한 후보 **전체**를 현금·레짐·점수 게이트보다 앞에서 하루 한 파일(JSONL)에 덧붙인다.
현금이 없거나 매수 중지여도 "그 시각 엔진이 고른 후보와 점수 구성"이 남아, 일봉 사후 수익을 붙여
선정 능력을 잴 수 있다(판독은 오프라인 — scripts/research/candidate_forward_excess.py 계열).

- 경로: ~/.cache/ai_trader/candidate_ledger/candidates_YYYYMMDD.jsonl (0600)
- 행: {"kind":"scan", scan_id, at, session, n, candidates:[…]} 와 {"kind":"gate", scan_id, at, …}
- 실패는 warning 한 줄뿐이다. 반환값·예외로 선정·주문 흐름을 바꾸지 않는다.
- 끄기: CANDIDATE_LEDGER=0 (재시작 필요)
"""

from __future__ import annotations

import json
import math
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Optional
from uuid import uuid4

from loguru import logger

LEDGER_DIR = Path.home() / ".cache" / "ai_trader" / "candidate_ledger"
SCHEMA = "candidate-ledger-v1"
MAX_CANDIDATES = 300      # 스캔 1회 반환은 보통 100개 이하 — 넘으면 앞에서 자르고 n 에 원래 개수를 남긴다
MAX_REASONS = 8
_NUM_FIELDS = ("score", "price", "change_pct", "volume", "volume_ratio", "rsi", "atr_pct", "per", "pbr")
_BASIS_FIELDS = (("merge_score_sum", "merge"), ("post_merge_adjustment", "adj"), ("occurrence_count", "occ"),
                 ("occurrence_bonus", "bonus"), ("pre_normalization_score", "pre"))


def enabled() -> bool:
    return os.environ.get("CANDIDATE_LEDGER", "1") != "0"


def _num(value: Any) -> Optional[float]:
    """유한한 수만 남긴다. bool·NaN·문자열은 None(미관측) — 0 으로 바꾸지 않는다."""
    if type(value) not in (int, float) or not math.isfinite(value):
        return None
    return value


def _candidate(stock: Any) -> dict:
    row = {"symbol": str(stock.symbol), **{k: _num(getattr(stock, k, None)) for k in _NUM_FIELDS}}
    for attr, key in (("has_foreign_buying", "foreign"), ("has_inst_buying", "inst")):
        v = getattr(stock, attr, None)
        row[key] = v if isinstance(v, bool) else None
    reasons = getattr(stock, "reasons", None)
    row["reasons"] = [str(r)[:80] for r in reasons[:MAX_REASONS]] if isinstance(reasons, list) else []
    basis = getattr(stock, "selection_basis", None)
    if isinstance(basis, dict):   # 원천별 가중 점수와 병합 뒤 보정 — 점수 구성요소 판독용
        terms = basis.get("source_terms")
        row["basis"] = {
            "terms": [[t.get("source_id"), _num(t.get("weighted_score"))] for t in terms if isinstance(t, dict)]
            if isinstance(terms, list) else [],
            **{key: _num(basis.get(name)) for name, key in _BASIS_FIELDS},
            "cache": basis.get("cache_fallback") is True,
        }
    return row


def _append(record: dict, now: datetime, ledger_dir: Optional[Path]) -> None:
    base = Path(ledger_dir) if ledger_dir is not None else LEDGER_DIR
    base.mkdir(parents=True, exist_ok=True)
    line = (json.dumps(record, ensure_ascii=False, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")
    fd = os.open(base / f"candidates_{now:%Y%m%d}.jsonl", os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.write(fd, line)   # 한 줄을 한 번에 쓴다(O_APPEND) — 줄 중간이 섞이지 않는다
    finally:
        os.close(fd)


def record_scan(stocks: Any, session: str, *, now: Optional[datetime] = None,
                ledger_dir: Optional[Path] = None) -> Optional[str]:
    """스캔 1회의 반환 후보 전체를 덧붙이고 scan_id 를 돌려준다. 꺼져 있거나 실패하면 None(예외 없음)."""
    if not enabled():
        return None
    try:
        at = now if now is not None else datetime.now()
        items = list(stocks)
        scan_id = uuid4().hex[:16]
        _append({"kind": "scan", "schema": SCHEMA, "scan_id": scan_id, "at": at.isoformat(timespec="seconds"),
                 "session": str(session), "n": len(items),
                 "candidates": [_candidate(s) for s in items[:MAX_CANDIDATES]]}, at, ledger_dir)
        return scan_id
    except Exception as e:   # 기록은 부가 기능 — 선정·주문 흐름을 막지 않는다
        logger.warning(f"[후보원장] 스캔 기록 실패(선정 흐름 유지): {type(e).__name__}")
        return None


def record_gate(scan_id: Optional[str], *, now: Optional[datetime] = None, ledger_dir: Optional[Path] = None,
                **fields: Any) -> bool:
    """그 스캔에 적용된 스캔 단위 게이트 결과(레짐·현금 등)를 덧붙인다. scan_id 가 없으면 아무것도 하지 않는다."""
    if scan_id is None or not enabled():
        return False
    try:
        at = now if now is not None else datetime.now()
        clean = {k: (v if isinstance(v, (bool, str)) or v is None else _num(v)) for k, v in fields.items()}
        _append({"kind": "gate", "scan_id": scan_id, "at": at.isoformat(timespec="seconds"), **clean}, at, ledger_dir)
        return True
    except Exception as e:
        logger.warning(f"[후보원장] 게이트 기록 실패(선정 흐름 유지): {type(e).__name__}")
        return False
