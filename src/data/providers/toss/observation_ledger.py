"""Private, append-only observation ledger.

This module deliberately has no HTTP imports.  A durable row is the only
authority for an attempt; damaged storage is retained and makes future sends
impossible rather than being repaired in place.
"""
from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any, Mapping


_TERMINAL_REASONS = frozenset({"success", "excluded", "provider_failure", "budget_skip", "cancelled", "interrupted", "calendar_invalid"})
_TYPES = frozenset({"slot", "begin", "terminal", "missed"})


class LedgerError(Exception):
    """Safe ledger failure; never carries a filesystem or payload exception."""
    def __init__(self, code: str):
        self.code = code if code in {"ledger_corrupt", "ledger_incomplete", "ledger_conflict", "ledger_unavailable"} else "ledger_unavailable"
        super().__init__(self.code)


def _canonical(value: Mapping[str, Any]) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")


def _hash(row: Mapping[str, Any]) -> str:
    value = dict(row); value.pop("hash", None)
    return sha256(_canonical(value)).hexdigest()


def _safe_snapshot(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    # ``kis`` is accepted at the runner boundary but intentionally never
    # serialised: Quotes can contain source metadata that is not ledger schema.
    allowed = {"snapshot_id", "selected_at", "source_success_at", "selection_partial", "symbols", "selection_metadata", "kis"}
    if not isinstance(snapshot, Mapping) or set(snapshot) - allowed:
        raise LedgerError("ledger_conflict")
    symbols = snapshot.get("symbols")
    if not isinstance(symbols, (list, tuple)) or any(not isinstance(s, str) or not s for s in symbols):
        raise LedgerError("ledger_conflict")
    if len(set(symbols)) != len(symbols):
        raise LedgerError("ledger_conflict")
    if not isinstance(snapshot.get("snapshot_id"), str) or not snapshot["snapshot_id"]:
        raise LedgerError("ledger_conflict")
    if type(snapshot.get("selection_partial")) is not bool:
        raise LedgerError("ledger_conflict")
    result = {key: snapshot.get(key) for key in ("snapshot_id", "selected_at", "source_success_at", "selection_partial")}
    result["symbols"] = list(symbols)
    if "selection_metadata" in snapshot:
        result["selection_metadata"] = snapshot["selection_metadata"]
    try:
        _canonical(result)
    except (TypeError, ValueError):
        raise LedgerError("ledger_conflict") from None
    return result


class ObservationLedger:
    def __init__(self, path, *, plan_hash: str, max_bytes: int):
        if not isinstance(plan_hash, str) or len(plan_hash) != 64 or any(c not in "0123456789abcdef" for c in plan_hash):
            raise ValueError("invalid_plan_hash")
        if type(max_bytes) is not int or max_bytes <= 0:
            raise ValueError("invalid_max_bytes")
        self.path, self.plan_hash, self.max_bytes = Path(path), plan_hash, max_bytes
        self._opened = False; self._failed = False; self._rows: list[dict[str, Any]] = []
        self._slots: dict[str, dict[str, Any]] = {}; self._begun: set[str] = set(); self._terminals: dict[str, str] = {}
        self._duplicates = 0

    def _fail(self, code: str):
        self._failed = True
        raise LedgerError(code)

    def _check_parents(self) -> None:
        try:
            parent = self.path.parent
            parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            for item in (parent, *parent.parents):
                info = item.lstat()
                if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                    self._fail("ledger_unavailable")
                if item == parent and stat.S_IMODE(info.st_mode) & 0o077:
                    self._fail("ledger_unavailable")
                # System temp roots are intentionally shared but sticky.  The
                # private immediate parent above remains owner-only.
                if item != parent and stat.S_IMODE(info.st_mode) & 0o022 and not (info.st_mode & stat.S_ISVTX):
                    self._fail("ledger_unavailable")
        except LedgerError: raise
        except OSError: self._fail("ledger_unavailable")

    def _read_rows(self) -> list[dict[str, Any]]:
        if not self.path.exists(): return []
        try:
            info = self.path.lstat()
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size > self.max_bytes:
                self._fail("ledger_corrupt")
            with self.path.open("rb") as handle: raw = handle.read(self.max_bytes + 1)
            if len(raw) > self.max_bytes or (raw and not raw.endswith(b"\n")): self._fail("ledger_corrupt")
            rows = []
            previous = "0" * 64
            for sequence, line in enumerate(raw.splitlines(), 1):
                def reject_constant(_: str): raise ValueError
                row = json.loads(line, object_pairs_hook=lambda pairs: _unique(pairs), parse_constant=reject_constant)
                if not isinstance(row, dict) or set(row) - {"schema_version", "sequence", "previous_hash", "hash", "type", "plan_hash", "slot_id", "kind", "snapshot", "attempt_id", "reason", "observation"}:
                    self._fail("ledger_corrupt")
                if row.get("schema_version") != 1 or row.get("sequence") != sequence or row.get("previous_hash") != previous or row.get("hash") != _hash(row) or row.get("type") not in _TYPES or row.get("plan_hash") != self.plan_hash:
                    self._fail("ledger_corrupt")
                previous = row["hash"]; rows.append(row)
            return rows
        except LedgerError: raise
        except (OSError, UnicodeDecodeError, ValueError, TypeError): self._fail("ledger_corrupt")

    def open(self):
        if self._opened: return
        self._check_parents(); self._rows = self._read_rows()
        try:
            for row in self._rows: self._apply(row, loading=True)
            self._opened = True
            # An intact unfinished record is a normal crash recovery, not corruption.
            for attempt in tuple(self._all_attempt_ids()):
                if attempt not in self._terminals:
                    self._append("terminal", attempt_id=attempt, reason="interrupted")
        except LedgerError:
            self._failed = True; raise

    @classmethod
    def read_only_summary(cls, path, *, plan_hash: str, max_bytes: int) -> dict[str, Any]:
        """Inspect an existing ledger without recovery writes or network imports."""
        ledger = cls(path, plan_hash=plan_hash, max_bytes=max_bytes)
        try:
            if not ledger.path.parent.exists(): ledger._fail("ledger_unavailable")
            ledger._rows = ledger._read_rows()
            for row in ledger._rows: ledger._apply(row, loading=True)
            ledger._opened = True
        except LedgerError:
            ledger._failed = True
        return ledger.summary()

    def _all_attempt_ids(self):
        for slot in self._slots.values():
            symbols = slot["snapshot"]["symbols"]
            count = len(symbols) if slot["kind"] == "prices" else 1
            for index in range(count):
                yield f"{slot['slot_id']}:{slot['kind']}:{index:06d}"

    def _append(self, typ: str, **fields: Any) -> None:
        if not self._opened or self._failed: self._fail("ledger_incomplete")
        row = {"schema_version": 1, "sequence": len(self._rows) + 1,
               "previous_hash": self._rows[-1]["hash"] if self._rows else "0" * 64,
               "type": typ, "plan_hash": self.plan_hash, **fields}
        row["hash"] = _hash(row)
        payload = _canonical(row) + b"\n"
        try:
            current = self.path.stat().st_size if self.path.exists() else 0
            if current + len(payload) > self.max_bytes: self._fail("ledger_incomplete")
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
            try:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600: self._fail("ledger_incomplete")
                os.write(fd, payload); os.fsync(fd)
            finally: os.close(fd)
        except LedgerError: raise
        except OSError: self._fail("ledger_incomplete")
        self._rows.append(row); self._apply(row, loading=False)

    def _apply(self, row: Mapping[str, Any], *, loading: bool) -> None:
        typ = row["type"]
        if typ == "slot":
            slot_id, kind, snap = row.get("slot_id"), row.get("kind"), row.get("snapshot")
            if not isinstance(slot_id, str) or kind not in {"prices", "calendar"}:
                self._fail("ledger_corrupt" if loading else "ledger_conflict")
            snap = _safe_snapshot(snap)
            old = self._slots.get(slot_id)
            if old is not None and old != {"slot_id": slot_id, "kind": kind, "snapshot": snap}: self._fail("ledger_corrupt" if loading else "ledger_conflict")
            self._slots[slot_id] = {"slot_id": slot_id, "kind": kind, "snapshot": snap}
        elif typ == "begin":
            attempt = row.get("attempt_id")
            if not isinstance(attempt, str) or attempt not in set(self._all_attempt_ids()): self._fail("ledger_corrupt" if loading else "ledger_conflict")
            self._begun.add(attempt)
        elif typ == "terminal":
            attempt, reason = row.get("attempt_id"), row.get("reason")
            if not isinstance(attempt, str) or attempt not in set(self._all_attempt_ids()) or reason not in _TERMINAL_REASONS: self._fail("ledger_corrupt" if loading else "ledger_conflict")
            prior = self._terminals.get(attempt)
            if prior is not None and prior != reason: self._fail("ledger_corrupt" if loading else "ledger_conflict")
            self._terminals[attempt] = reason
        elif typ == "missed":
            if not isinstance(row.get("slot_id"), str): self._fail("ledger_corrupt" if loading else "ledger_conflict")

    def reserve_slot(self, slot_id: str, *, kind: str, snapshot: dict) -> tuple[str, ...] | None:
        if not self._opened or self._failed: self._fail("ledger_incomplete")
        if not isinstance(slot_id, str) or not slot_id or kind not in {"prices", "calendar"}: self._fail("ledger_conflict")
        if slot_id in self._slots:
            self._duplicates += 1; return None
        safe = _safe_snapshot(snapshot)
        self._append("slot", slot_id=slot_id, kind=kind, snapshot=safe)
        count = len(safe["symbols"]) if kind == "prices" else 1
        return tuple(f"{slot_id}:{kind}:{i:06d}" for i in range(count))

    def begin(self, attempt_id: str) -> None:
        if attempt_id not in set(self._all_attempt_ids()): self._fail("ledger_conflict")
        if attempt_id in self._terminals: return
        if attempt_id not in self._begun: self._append("begin", attempt_id=attempt_id)

    def finish(self, attempt_id: str, *, reason: str, observation: dict | None = None) -> None:
        if attempt_id not in set(self._all_attempt_ids()) or reason not in _TERMINAL_REASONS: self._fail("ledger_conflict")
        prior = self._terminals.get(attempt_id)
        if prior is not None:
            if prior != reason: self._fail("ledger_conflict")
            return
        if observation is not None:
            # No arbitrary exception or provider body may enter a durable row.
            allowed = {"valid_pairs", "excluded_pairs", "status", "holiday", "requested_date"}
            try:
                if not isinstance(observation, Mapping) or set(observation) - allowed: raise ValueError
                _canonical(observation)
            except (TypeError, ValueError): self._fail("ledger_conflict")
        fields = {"attempt_id": attempt_id, "reason": reason}
        if observation is not None: fields["observation"] = observation
        self._append("terminal", **fields)

    def record_missed(self, slot_id: str, *, kind: str) -> None:
        if not isinstance(slot_id, str) or kind not in {"prices", "calendar"}: self._fail("ledger_conflict")
        self._append("missed", slot_id=slot_id, kind=kind)

    def summary(self) -> dict[str, Any]:
        terminals = list(self._terminals.values())
        return {"expected_slots": 0, "recorded_slots": len(self._slots), "missed_slots": sum(r["type"] == "missed" for r in self._rows),
            "selected_attempts": sum(1 for _ in self._all_attempt_ids()), "terminal_attempts": len(terminals),
            "valid_pairs": sum((r.get("observation") or {}).get("valid_pairs", 0) for r in self._rows if r["type"] == "terminal"),
            "excluded_pairs": sum((r.get("observation") or {}).get("excluded_pairs", 0) for r in self._rows if r["type"] == "terminal"),
            "provider_failures": terminals.count("provider_failure"), "budget_skips": terminals.count("budget_skip"),
            "interrupted": terminals.count("interrupted"), "duplicate_submissions": self._duplicates,
            "incomplete": self._failed or len(terminals) != sum(1 for _ in self._all_attempt_ids()), "production_eligible": False}

    def close(self):
        return None


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result: raise ValueError("duplicate key")
        result[key] = value
    return result
