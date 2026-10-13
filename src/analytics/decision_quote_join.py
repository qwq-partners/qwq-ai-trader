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


VI_CHANNEL = "H0STMKO0"          # 장운영(VI·정지) 채널 — 체결 채널(H0STCNT0)은 VI 코드를 주지 않는다
# 채널별로 '근거가 있다' 고 말하려면 실제 값이 있어야 하는 필드. 파서는 빈 문자열도 raw_only 로 통과시키므로
# 여기서 결측·빈 문자열을 잡는다 — "0" 은 유효한 값으로 보존한다 (재리뷰 P1).
REQUIRED_STATE_FIELDS = {
    "H0STMKO0": ("trht_yn", "mkop_cls_code", "vi_cls_code", "iscd_stat_cls_code"),
    "H0STCNT0": ("market_cls_code", "trht_yn", "hour_cls_code"),
}


def _finite_nonnegative(value: Any, label: str) -> float:
    import math
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise ValueError(f"{label}: 유한한 0 이상 초 단위 값이 필요하다 ({value!r})")
    return float(value)


def _missing_fields(tr_id: str, state: Dict[str, Any]) -> List[str]:
    required = REQUIRED_STATE_FIELDS.get(tr_id, ())
    return [name for name in required if not isinstance(state.get(name), str) or state.get(name) == ""]
DEFAULT_MAX_STATE_AGE_SECONDS = 60.0
UNVERIFIED_LEDGER = {"verified": False, "complete": None, "incomplete_reasons": ["ledger_not_verified"]}


def _ledger_reasons(ledger_status: Optional[Dict[str, Any]]) -> List[str]:
    """원장 검증·완전성이 확인되지 않으면 어떤 행도 평가 가능으로 집계하지 않는다 (리뷰 P1)."""
    if not isinstance(ledger_status, dict):
        return ["ledger_unverified"]
    if ledger_status.get("verified") is not True:
        return ["ledger_unverified"]
    if ledger_status.get("complete") is not True:
        return ["ledger_incomplete"]
    return []


def join_decisions(decisions: List[Dict[str, Any]], payloads: List[Dict[str, Any]], *,
                   max_delay_seconds: float = DEFAULT_MAX_DELAY_SECONDS,
                   max_state_age_seconds: float = DEFAULT_MAX_STATE_AGE_SECONDS,
                   ledger_status: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    max_delay_seconds = _finite_nonnegative(max_delay_seconds, "max_delay_seconds")
    max_state_age_seconds = _finite_nonnegative(max_state_age_seconds, "max_state_age_seconds")
    ledger_reasons = _ledger_reasons(ledger_status)
    quotes: Dict[str, List[tuple]] = {}
    states: Dict[str, List[tuple]] = {}
    # 시각이 손상된 레코드는 조용히 버리지 않는다 — 그 종목의 판단은 '첫 호가/최신 상태' 를 믿을 수 없다 (재리뷰 P2)
    time_errors: Dict[str, int] = {}
    for p in payloads:
        symbol = p.get("symbol")
        if not isinstance(symbol, str):
            symbol = "<missing_symbol>"
        try:
            at = parse_time(p.get("observed_at"), f"{p.get('kind')} observed_at")
        except ValueError:
            time_errors[symbol] = time_errors.get(symbol, 0) + 1
            continue
        if symbol == "<missing_symbol>":
            continue
        (quotes if p.get("kind") == "ws_quote" else states).setdefault(symbol, []).append((at, p))
    for bucket in (quotes, states):
        for rows in bucket.values():
            rows.sort(key=lambda item: item[0])

    out = []
    for d in decisions:
        symbol = d.get("symbol")
        result = d.get("result")
        if not isinstance(result, dict):
            result = {}
        reasons: List[str] = list(ledger_reasons)
        if time_errors.get(symbol) or time_errors.get("<missing_symbol>"):
            reasons.append("ledger_record_time_invalid")
        row: Dict[str, Any] = {
            "decision_id": d.get("decision_id"), "signal_id": d.get("signal_id"), "symbol": symbol,
            "stage": d.get("stage"), "approved": result.get("approved"),
            "decision_available_at": d.get("decision_available_at"),
            "first_quote_after": None, "market_state_at_quote": None,
        }
        try:
            decided = parse_time(d.get("decision_available_at"), "decision_available_at")
        except ValueError:
            reasons.append("decision_time_missing")
            row.update(evaluable=False, reasons=reasons)
            out.append(row)
            continue
        candidates = [q for q in quotes.get(symbol, []) if q[0] > decided]
        if not candidates:
            reasons.append("no_quote_after_decision")
        else:
            at, q = candidates[0]
            delay = (at - decided).total_seconds()
            ask, ask_size = _positive_decimal(q.get("ask")), _positive_int(q.get("ask_size"))
            provenance = q.get("provenance")
            if not isinstance(provenance, dict):
                provenance = {}
            connection_id, generation = provenance.get("connection_id"), provenance.get("generation")
            row["first_quote_after"] = {
                "quote_id": q.get("quote_id"), "observed_at": q.get("observed_at"), "delay_seconds": delay,
                "ask": str(ask) if ask is not None else None, "ask_size": ask_size,
                "bid": q.get("bid"), "bid_size": q.get("bid_size"),
                "exchange_time": provenance.get("exchange_time"),
                "connection_id": connection_id, "generation": generation,
            }
            if ask is None:
                reasons.append("invalid_ask")
            if ask_size is None:
                reasons.append("invalid_ask_size")
            if delay > max_delay_seconds:
                reasons.append("stale_quote")
            if connection_id is None or generation is None:
                reasons.append("quote_provenance_missing")
            # 장 상태는 호가와 같은 연결·세대의 기록만 쓴다(재연결 전 상태를 새 호가에 붙이지 않음, 리뷰 P2).
            # 채널(tr_id)별 최신 1건을 각각 보존한다 — 체결/장운영 행의 필드를 하나로 합치지 않는다.
            latest_by_channel: Dict[str, tuple] = {}
            for s_at, s in states.get(symbol, []):
                if s_at > at:
                    break
                if connection_id is None or s.get("connection_id") != connection_id or s.get("generation") != generation:
                    continue
                tr_id = s.get("tr_id")
                if isinstance(tr_id, str):
                    latest_by_channel[tr_id] = (s_at, s)
            if not latest_by_channel:
                reasons.append("market_state_unknown")
            else:
                channels = {}
                for tr_id, (s_at, s) in sorted(latest_by_channel.items()):
                    age = (at - s_at).total_seconds()
                    channels[tr_id] = {
                        "observed_at": s.get("observed_at"), "age_seconds": age,
                        "market_state_status": s.get("market_state_status"), "state_verdict": s.get("state_verdict"),
                        "market_cls_code": s.get("market_cls_code"), "trht_yn": s.get("trht_yn"),
                        "vi_cls_code": s.get("vi_cls_code"), "hour_cls_code": s.get("hour_cls_code"),
                        "mkop_cls_code": s.get("mkop_cls_code"), "iscd_stat_cls_code": s.get("iscd_stat_cls_code"),
                    }
                    if s.get("market_state_status") != "raw_only":
                        reasons.append(f"market_state_not_raw:{tr_id}")
                    missing = _missing_fields(tr_id, s)
                    if missing:
                        reasons.append(f"market_state_fields_missing:{tr_id}:{','.join(missing)}")
                    if age > max_state_age_seconds:
                        reasons.append(f"market_state_stale:{tr_id}")
                row["market_state_at_quote"] = channels
                if VI_CHANNEL not in channels:
                    reasons.append("market_state_vi_channel_missing")   # VI·정지 근거 없이 '진입 가능' 을 말하지 않는다
        row.update(evaluable=not reasons, reasons=reasons)
        out.append(row)
    if out:
        out[0].setdefault("_ledger_time_errors", dict(time_errors))   # 요약용 — 첫 행에 집계를 싣는다
    return out


def summarize(rows: List[Dict[str, Any]], *, ledger_status: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    reasons: Dict[str, int] = {}
    for r in rows:
        row_reasons = r.get("reasons")
        if not isinstance(row_reasons, list):
            row_reasons = []
        for reason in row_reasons:
            reasons[reason] = reasons.get(reason, 0) + 1
    time_errors = rows[0].get("_ledger_time_errors") if rows and isinstance(rows[0].get("_ledger_time_errors"), dict) else {}
    return {
        "schema_version": SCHEMA_VERSION,
        "ledger": dict(ledger_status) if isinstance(ledger_status, dict) else dict(UNVERIFIED_LEDGER),
        "ledger_record_time_errors_by_symbol": time_errors,
        "decisions": len(rows),
        "evaluable": sum(1 for r in rows if r.get("evaluable") is True),
        "approved_evaluable": sum(1 for r in rows if r.get("evaluable") is True and r.get("approved") is True),
        "reasons": reasons,
        # 관측 호가는 체결 보장이 아니다 — 이 표는 '응답 뒤 호가 존재·신선도·같은 연결의 장 상태 근거' 까지만 말한다
        "note": "evaluable 은 검증된 완전한 원장에서 호가·장 상태 근거가 충족됐다는 뜻이며 체결 가능성·순수익 판정이 아니다",
    }
