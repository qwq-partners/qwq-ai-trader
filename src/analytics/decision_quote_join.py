"""판단 기록(llm-decision-v1) ↔ 관측 원장(ws_quote·market_state) 오프라인 조인 — 65차.

각 판단의 `decision_available_at` 이후 같은 종목의 **첫 호가**와 그 시점 이전의 마지막 장 상태를 찾아
"응답 뒤 진입 가능 가격" 표의 입력을 만든다. 관측 호가는 체결 보장이 아니며, ask·수량·신선도·장 상태가
부족하면 `evaluable=False` 와 사유를 남긴다. 장 상태 어휘(state_verdict)는 해석하지 않고 그대로 보고한다 —
정규장 코드만으로 연속매매/VI 부재를 확정하지 않는다. 네트워크·운영 상태 접근 없음.
"""
from __future__ import annotations

import json
import os
import stat
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Iterable, List, Optional

SCHEMA_VERSION = "decision-quote-join-v1"
DEFAULT_MAX_BYTES = 256 * 1024 * 1024
DEFAULT_MAX_DELAY_SECONDS = 60.0


def parse_time(value: Any, label: str) -> datetime:
    """ISO 시각. tz 없는 값은 관측 원장 계약(UTC naive)으로 해석한다 — 현재 시각으로 대체하지 않는다."""
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label}: 시각 누락")
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def _read_lines(path, *, max_bytes: int) -> Iterable[bytes]:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > max_bytes:
            raise ValueError("정규 파일/읽기 크기 한도 위반")
        for line in handle:
            if line.strip():
                yield line


def load_decisions(path, *, max_bytes: int = DEFAULT_MAX_BYTES) -> List[Dict[str, Any]]:
    rows = []
    for line in _read_lines(path, max_bytes=max_bytes):
        row = json.loads(line)
        if not isinstance(row, dict) or row.get("schema_version") != "llm-decision-v1":
            raise ValueError("llm-decision-v1 행이 아니다")
        rows.append(row)
    return rows


def load_ledger_payloads(path, *, max_bytes: int = DEFAULT_MAX_BYTES,
                         kinds=("ws_quote", "market_state")) -> List[Dict[str, Any]]:
    """관측 원장 envelope(row/previous_hash/kind/payload/hash)의 record payload 만 모은다(관대한 읽기).
    해시 체인 검증은 `read_observation_journal`(strict) 로 따로 한다."""
    payloads = []
    for line in _read_lines(path, max_bytes=max_bytes):
        row = json.loads(line)
        if not isinstance(row, dict) or row.get("kind") != "record":
            continue
        payload = row.get("payload")
        if isinstance(payload, dict) and payload.get("kind") in kinds:
            payloads.append(payload)
    return payloads


def _positive_decimal(value: Any) -> Optional[Decimal]:
    if isinstance(value, bool) or value is None:
        return None
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return amount if amount.is_finite() and amount > 0 else None


def _positive_int(value: Any) -> Optional[int]:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def join_decisions(decisions: List[Dict[str, Any]], payloads: List[Dict[str, Any]], *,
                   max_delay_seconds: float = DEFAULT_MAX_DELAY_SECONDS) -> List[Dict[str, Any]]:
    quotes: Dict[str, List[tuple]] = {}
    states: Dict[str, List[tuple]] = {}
    for p in payloads:
        symbol = p.get("symbol")
        if not isinstance(symbol, str):
            continue
        try:
            at = parse_time(p.get("observed_at"), f"{p.get('kind')} observed_at")
        except ValueError:
            continue
        (quotes if p.get("kind") == "ws_quote" else states).setdefault(symbol, []).append((at, p))
    for bucket in (quotes, states):
        for rows in bucket.values():
            rows.sort(key=lambda item: item[0])

    out = []
    for d in decisions:
        symbol = d.get("symbol")
        reasons: List[str] = []
        row: Dict[str, Any] = {
            "decision_id": d.get("decision_id"), "signal_id": d.get("signal_id"), "symbol": symbol,
            "stage": d.get("stage"), "approved": (d.get("result") or {}).get("approved"),
            "decision_available_at": d.get("decision_available_at"),
            "first_quote_after": None, "market_state_at_quote": None,
        }
        try:
            decided = parse_time(d.get("decision_available_at"), "decision_available_at")
        except ValueError:
            row.update(evaluable=False, reasons=["decision_time_missing"])
            out.append(row)
            continue
        candidates = [q for q in quotes.get(symbol, []) if q[0] > decided]
        if not candidates:
            reasons.append("no_quote_after_decision")
        else:
            at, q = candidates[0]
            delay = (at - decided).total_seconds()
            ask, ask_size = _positive_decimal(q.get("ask")), _positive_int(q.get("ask_size"))
            row["first_quote_after"] = {
                "quote_id": q.get("quote_id"), "observed_at": q.get("observed_at"), "delay_seconds": delay,
                "ask": str(ask) if ask is not None else None, "ask_size": ask_size,
                "bid": q.get("bid"), "bid_size": q.get("bid_size"),
                "exchange_time": (q.get("provenance") or {}).get("exchange_time") if isinstance(q.get("provenance"), dict) else None,
            }
            if ask is None:
                reasons.append("invalid_ask")
            if ask_size is None:
                reasons.append("invalid_ask_size")
            if delay > max_delay_seconds:
                reasons.append("stale_quote")
            prior_states = [s for s in states.get(symbol, []) if s[0] <= at]
            if prior_states:
                s_at, s = prior_states[-1]
                row["market_state_at_quote"] = {
                    "observed_at": s.get("observed_at"), "age_seconds": (at - s_at).total_seconds(),
                    "state_verdict": s.get("state_verdict"), "market_state_status": s.get("market_state_status"),
                    "market_cls_code": s.get("market_cls_code"), "trht_yn": s.get("trht_yn"),
                    "vi_cls_code": s.get("vi_cls_code"), "hour_cls_code": s.get("hour_cls_code"),
                }
                if s.get("market_state_status") != "raw_only":
                    reasons.append("market_state_not_raw")
            else:
                reasons.append("market_state_unknown")
        row.update(evaluable=not reasons, reasons=reasons)
        out.append(row)
    return out


def summarize(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    reasons: Dict[str, int] = {}
    for r in rows:
        for reason in r.get("reasons") or []:
            reasons[reason] = reasons.get(reason, 0) + 1
    return {
        "schema_version": SCHEMA_VERSION,
        "decisions": len(rows),
        "evaluable": sum(1 for r in rows if r.get("evaluable")),
        "approved_evaluable": sum(1 for r in rows if r.get("evaluable") and r.get("approved") is True),
        "reasons": reasons,
        # 관측 호가는 체결 보장이 아니다 — 이 표는 '응답 뒤 호가 존재·신선도·장 상태' 까지만 말한다
        "note": "evaluable 은 호가·장 상태 자료 충족이며 체결 가능성·순수익 판정이 아니다",
    }
