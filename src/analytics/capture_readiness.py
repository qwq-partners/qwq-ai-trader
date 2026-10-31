"""Offline, fail-closed LLM capture readiness report.

This module deliberately reads only the three explicit inputs.  It neither
contacts providers nor infers fills, profit, or an account return.
"""
from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path

from .decision_quote_join import join_decisions, summarize, _aware_time
from .entry_evaluation_bundle import load_json_bytes
from .entry_observation import analysis_time
from .entry_observation_journal import read_observation_journal
from .entry_observation_runtime import LLM_VERSION, validate_window_contract
from .entry_price_shadow import PriceGatePolicy, build_report

STUDY_MAX_BYTES = 4 * 1024 * 1024
HARD_MAX_BYTES = 256 * 1024 * 1024
DEFAULT_MAX_BYTES = HARD_MAX_BYTES
DEFAULT_MAX_STATE_AGE_SECONDS = 5.0


def _limit(value):
    if type(value) is not int or not 0 < value <= HARD_MAX_BYTES:
        raise ValueError("max_bytes must be a positive integer no greater than 268435456")
    return value


def _snapshot(path, maximum):
    """Return bounded bytes after two same-fd reads and metadata checks agree.

    Detect observed changes; this is not an atomic snapshot or protection
    against changes that are reverted between observations (ABA).
    """
    fd = os.open(os.fspath(path), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_size > maximum:
            raise ValueError("regular file/size limit violation")
        chunks, left = [], before.st_size
        while left:
            part = os.read(fd, min(left, 1024 * 1024))
            if not part:
                raise ValueError("input changed during read")
            chunks.append(part)
            left -= len(part)
        if os.read(fd, 1):
            raise ValueError("input changed during read")
        after = os.fstat(fd)
        identity = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
        if identity != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise ValueError("input changed during read")
        # 같은 길이의 빠른 쓰기는 mtime/ctime이 같을 수 있다. 경로를 다시
        # 열지 않고 같은 fd의 내용을 한 번만 대조한다. 두 번째 전체 버퍼는 없다.
        os.lseek(fd, 0, os.SEEK_SET)
        for expected in chunks:
            offset = 0
            while offset < len(expected):
                part = os.read(fd, len(expected) - offset)
                if not part or part != expected[offset:offset + len(part)]:
                    raise ValueError("input changed during read")
                offset += len(part)
        if os.read(fd, 1):
            raise ValueError("input changed during read")
        checked = os.fstat(fd)
        if identity != (checked.st_dev, checked.st_ino, checked.st_size, checked.st_mtime_ns, checked.st_ctime_ns):
            raise ValueError("input changed during read")
    finally:
        os.close(fd)
    raw = b"".join(chunks)
    return raw, {"bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest(), "identity": identity}


def _stable_journal(path, study_sha, maximum):
    before, meta = _snapshot(path, maximum)
    verified = read_observation_journal(path, max_bytes=maximum, expected_study_sha256=study_sha)
    after, later = _snapshot(path, maximum)
    if before != after or meta["identity"] != later["identity"]:
        raise ValueError("ledger changed around strict validation")
    return verified, {"bytes": meta["bytes"], "sha256": meta["sha256"]}


def _loads_line(line):
    return json.loads(line, object_pairs_hook=lambda pairs: _unique(pairs), parse_constant=_no_constant)


def _unique(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise ValueError("duplicate JSON key")
        out[key] = value
    return out


def _no_constant(_value):
    raise ValueError("nonfinite JSON constant")


def _decisions(raw):
    if raw and not raw.endswith(b"\n"):
        raise ValueError("partial decision JSONL")
    rows = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        item = _loads_line(line)
        required = {"schema_version", "decision_id", "signal_id", "symbol", "strategy", "stage",
                    "decision_available_at", "result"}
        if type(item) is not dict or item.get("schema_version") != "llm-decision-v1" or not required <= set(item):
            raise ValueError("invalid llm-decision-v1 row")
        if (any(not isinstance(item[k], str) or not item[k] for k in
                ("decision_id", "symbol", "strategy", "stage"))
                or (item["signal_id"] is not None and (not isinstance(item["signal_id"], str) or not item["signal_id"]))
                or not isinstance(item["result"], dict)):
            raise ValueError("invalid decision identity")
        _aware_time(item["decision_available_at"], "decision_available_at")
        rows.append(item)
    return rows


def _cohort(records):
    starts, admissions = {}, {}
    for record in records:
        if record.get("kind") == "llm_check_started":
            sid = record["signal_id"]
            if sid in starts:
                raise ValueError("duplicate llm_check_started signal_id")
            starts[sid] = record
        elif record.get("kind") == "llm_check_admission":
            admissions[record["signal_id"]] = record
    return starts, admissions


def _select(rows, starts, end, audit_at):
    by_id, by_signal = {}, {}
    for row in rows:
        by_id.setdefault(row["decision_id"], []).append(row)
        if row["signal_id"] is not None:
            by_signal.setdefault(row["signal_id"], []).append(row)
    duplicate_ids = sorted(key for key, value in by_id.items() if len(value) > 1)
    ambiguous_signals = sorted(key for key, value in by_signal.items() if len(value) > 1)
    matched, excluded, unlinked, invalid = [], 0, 0, {"decision_before_started": 0, "decision_identity_mismatch": 0,
                                         "late_decision": 0, "after_analysis_as_of": 0}
    invalid_ids = set(duplicate_ids) | set(ambiguous_signals)
    late_ids = []
    for row in rows:
        if row["signal_id"] is None:
            excluded += 1
            unlinked += 1
            continue
        signal = starts.get(row["signal_id"])
        if signal is None:
            excluded += 1
            continue
        if row["decision_id"] in invalid_ids or row["signal_id"] in invalid_ids:
            continue
        if row["symbol"] != signal["symbol"] or row["strategy"] != signal["strategy"]:
            invalid["decision_identity_mismatch"] += 1
            continue
        decided = _aware_time(row["decision_available_at"], "decision_available_at")
        if decided > audit_at:
            invalid["after_analysis_as_of"] += 1
            continue
        if decided < _aware_time(signal["observed_at"], "llm_check_started observed_at"):
            invalid["decision_before_started"] += 1
            continue
        if decided >= end:
            invalid["late_decision"] += 1
            late_ids.append(row["decision_id"])
            continue
        matched.append(row)
    return matched, {"matched": len(matched), "excluded": excluded, "unlinked_monthly_decisions": unlinked,
                     "missing_signal_ids": sorted(set(starts) - {r["signal_id"] for r in matched}),
                     "late_decision_ids": sorted(late_ids), "ambiguous_decision_ids": duplicate_ids,
                     "ambiguous_signal_ids": ambiguous_signals, "invalid": invalid}


def build_capture_readiness(study_path, ledger_path, *, decisions_path=None, analysis_as_of=None,
                            max_bytes=DEFAULT_MAX_BYTES, max_state_age_seconds=DEFAULT_MAX_STATE_AGE_SECONDS):
    """Build a deterministic report from explicit local paths only."""
    maximum = _limit(max_bytes)
    study_bytes, study_meta = _snapshot(study_path, STUDY_MAX_BYTES)
    study_sha = study_meta["sha256"]
    context = load_json_bytes(study_bytes)
    if context.get("capture", {}).get("version") != LLM_VERSION:
        raise ValueError("requires runner-llm-window-v1 study")
    verified, ledger_meta = _stable_journal(ledger_path, study_sha, maximum)
    build_report({**context, "opportunities": []})
    if (verified.get("evaluation_epoch") != context.get("evaluation_epoch")
            or verified["journal"].get("study_ref") != context["capture"].get("study_ref")):
        raise ValueError("journal/study lifecycle binding mismatch")
    validate_window_contract(context, verified)
    # The generic evaluator does not accept LLM admission records; this validator
    # is its dedicated strict study/window/cohort path.
    closed_at = verified["journal"].get("capture_closed_at")
    audit_at = analysis_time(context, analysis_as_of)
    if closed_at is not None and audit_at < _aware_time(closed_at, "capture_closed_at"):
        raise ValueError("analysis_as_of precedes sealed capture")
    if closed_at is None and analysis_as_of is not None:
        raise ValueError("analysis_as_of requires sealed journal")
    starts, admissions = _cohort(verified["records"])
    end = _aware_time(context["capture"]["end_at"], "capture.end_at")
    decision_status, decision_meta, decision_rows = "decision_input_missing", None, []
    if decisions_path is not None and Path(decisions_path).exists():
        raw, decision_meta = _snapshot(decisions_path, maximum)
        decision_rows = _decisions(raw)
        decision_status = "decision_input_empty" if not decision_rows else "decision_input_loaded"
    elif decisions_path is not None:
        decision_status = "decision_input_missing"
    selected, decisions = _select(decision_rows, starts, end, audit_at)
    policy_delay = PriceGatePolicy.from_dict(context.get("policy")).max_decision_delay_seconds
    if not policy_delay.is_finite() or policy_delay < 0:
        raise ValueError("study max_decision_delay_seconds required")
    ledger_status = {"verified": True, "complete": verified["complete"],
                     "incomplete_reasons": verified["incomplete_reasons"]}
    payloads = [r for r in verified["records"] if r.get("kind") in ("ws_quote", "market_state", "rest_quote")]
    rows = join_decisions(selected, payloads, max_delay_seconds=float(policy_delay),
                          max_state_age_seconds=max_state_age_seconds,
                          ledger_status=ledger_status)
    summary = summarize(rows, ledger_status=ledger_status)
    status = "no_llm_checks" if not starts and verified["complete"] else "capture_diagnostic"
    return {"version": "capture-readiness-v1", "scope": LLM_VERSION, "status": status,
            "inputs": {"study": study_meta, "ledger": ledger_meta, "decisions": decision_meta},
            "journal": {"verified": True, "complete": verified["complete"],
                        "incomplete_reasons": verified["incomplete_reasons"],
                        "dropped_records": verified["dropped_records"]},
            "analysis_as_of": audit_at.isoformat(),
            "technical": {"technical_ready": verified["complete"] is True, "integrity_verified": True,
                          "journal_complete": verified["complete"] is True, "full_coverage": False},
            "llm_started_count": len(starts),
            "started": [{"signal_id": sid, "symbol": row["symbol"], "strategy": row["strategy"],
                         "observed_at": row["observed_at"], "admission_reason": admissions.get(sid, {}).get("reason")}
                        for sid, row in starts.items()],
            "decision_input_status": decision_status, "decision_recording_finality": "unverified",
            "decision_snapshot_unsettled": bool(decisions["missing_signal_ids"]),
            "decisions": decisions,
            "quote_join": {"summary": summary, "rows": rows,
                           "note": "evaluable is quote/state evidence only; it is not a fill or profitability result"},
            "economics": {"profitability_pass": False, "account_return": None, "production_eligible": False}}
