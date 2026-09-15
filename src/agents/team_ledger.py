"""팀 심의 불변 원장 — T11 계약 2.4 (2026-09-15).

`team_verdicts/verdicts_YYYYMMDD.json` 은 대시보드·conviction 소비자용으로 "같은 종목은 최신 것만"
유지한다(호환). 이 모듈은 그와 별도로 **덮어쓰지 않는 append-only JSONL** 을 쌓아
같은 날 같은 종목의 여러 시점 판단, BUY/HOLD/REJECT/자료 부족/실패를 전부 보존한다.

- 경로: ~/.cache/ai_trader/team_ledger/deliberations_YYYYMMDD.jsonl (LEDGER_DIR 주입 가능)
- 멱등: `deliberation_id = sha256(symbol|date|slot|input_snapshot_hash)[:16]` — 같은 입력의
  재시도·중복 이벤트는 같은 id 를 얻고, 읽기 쪽(`load_day`)이 id 로 dedup 한다.
- 저장 실패는 warning 만 남긴다 — 기록은 부가 기능이며 돈 경로를 막지 않는다.
- 원문(프롬프트·응답)은 LLM 재현성 원장(reproducibility.py)에 있고 여기엔 참조만 둔다.
  기록 전 `_mask_secrets` 로 비밀·개인정보 패턴을 가린다.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from loguru import logger

LEDGER_DIR = Path.home() / ".cache" / "ai_trader" / "team_ledger"

EXECUTION_STATES = ("candidate", "waiting_trigger", "plan_rejected", "shadow_ready", "order_submitted", "filled")
# plan_rejected: 계획은 있으나 shadow 검증이 reject(만료·무효화·severe) — "계획 없음/판단 전"(candidate) 과 구분 (T11 통합)

# 비밀·개인정보 마스킹 (entry_risk._is_secret_key 와 같은 취지 — 키 이름·값 패턴 둘 다)
_SECRET_KEY_RE = re.compile(r"(api[_-]?key|secret|token|password|passwd|appkey|cano|acnt|account|chat_id)", re.I)
_SECRET_VALUE_RE = re.compile(r"(github_pat_[A-Za-z0-9_]{20,}|sk-[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}|\b\d{8}-\d{2}\b|\b\d{10,}\b)")


def make_deliberation_id(symbol: str, day: str, slot: str, input_snapshot_hash: str) -> str:
    raw = f"{symbol}|{day}|{slot}|{input_snapshot_hash}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _mask_secrets(obj: Any) -> Any:
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if isinstance(k, str) and _SECRET_KEY_RE.search(k):
                out[k] = "***"
            else:
                out[k] = _mask_secrets(v)
        return out
    if isinstance(obj, list):
        return [_mask_secrets(v) for v in obj]
    if isinstance(obj, str):
        return _SECRET_VALUE_RE.sub("***", obj)
    return obj


def _ledger_path(day: str, ledger_dir: Optional[Path] = None) -> Path:
    base = Path(ledger_dir) if ledger_dir is not None else LEDGER_DIR
    return base / f"deliberations_{day.replace('-', '')}.jsonl"


def append_deliberation(row: Dict[str, Any], *, ledger_dir: Optional[Path] = None) -> Optional[str]:
    """심의 1건을 원장에 덧붙인다. 성공 시 deliberation_id, 실패 시 None (예외 없음).

    row 필수 키: symbol, decided_at(ISO), slot, input_snapshot_hash. deliberation_id 가 없으면 만든다.
    """
    try:
        symbol = str(row.get("symbol") or "")
        decided_at = str(row.get("decided_at") or datetime.now().isoformat(timespec="seconds"))
        day = decided_at[:10]
        slot = str(row.get("slot") or "")
        snap = str(row.get("input_snapshot_hash") or "")
        did = str(row.get("deliberation_id") or make_deliberation_id(symbol, day, slot, snap))
        rec = dict(row)
        rec["deliberation_id"] = did
        rec["decided_at"] = decided_at
        rec.setdefault("recorded_at", datetime.now().isoformat(timespec="seconds"))
        rec.setdefault("execution_state", "candidate")
        rec = _mask_secrets(rec)
        path = _ledger_path(day, ledger_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
        return did
    except Exception as e:
        logger.warning(f"[팀원장] 기록 실패 (무시): {e}")
        return None


def load_day(day: str, *, ledger_dir: Optional[Path] = None, dedup: bool = True) -> List[Dict[str, Any]]:
    """하루치 원장 (day: YYYY-MM-DD 또는 YYYYMMDD). dedup=True 면 같은 deliberation_id 는 마지막 행만."""
    path = _ledger_path(day, ledger_dir)
    if not path.exists():
        return []
    rows: List[Dict[str, Any]] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    except OSError as e:
        logger.warning(f"[팀원장] 읽기 실패: {e}")
        return []
    if not dedup:
        return rows
    by_id: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        by_id[str(r.get("deliberation_id"))] = r
    return list(by_id.values())


def history_for_symbol(symbol: str, day: str, *, ledger_dir: Optional[Path] = None) -> List[Dict[str, Any]]:
    """같은 날 같은 종목의 모든 시점 판단 (덮어쓰지 않는다)"""
    return sorted(
        (r for r in load_day(day, ledger_dir=ledger_dir) if r.get("symbol") == symbol),
        key=lambda r: str(r.get("decided_at", "")),
    )


def count_samples(rows: Iterable[Dict[str, Any]]) -> Dict[str, int]:
    """평가 표본 집계 — 상태별 건수 (중복 id 는 load_day 에서 이미 제거됨)"""
    out: Dict[str, int] = {}
    for r in rows:
        dec = r.get("decision") or {}
        stance = str(dec.get("stance") or "")
        approved = bool(dec.get("approved"))
        if r.get("error"):
            key = "failed"
        elif (r.get("assessment") or {}).get("abstained"):
            key = "abstained"
        elif stance == "buy" and approved:
            key = "buy_approved"
        elif stance == "buy":
            key = "buy_rejected"
        else:
            key = stance or "unknown"
        out[key] = out.get(key, 0) + 1
    return out
