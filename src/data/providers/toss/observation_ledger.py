"""Private bounded journal. Durable records, never task returns, are authority.

The writer owns safe directory/file descriptors and a lifetime flock. Corrupt
storage is retained; neither writer nor offline reporter silently repairs it.
"""
from __future__ import annotations

import fcntl
import json
import math
import os
import re
import stat
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from fractions import Fraction
from hashlib import sha256
from pathlib import Path
from typing import Mapping

_TERMINAL_REASONS = frozenset({"success", "excluded", "provider_failure", "budget_skip", "cancelled", "interrupted", "calendar_invalid"})
_PAIR_REASONS = frozenset({"ok", "missing_quote", "status", "price", "symbol", "currency", "market_basis", "observed_at", "fetched_at", "future", "stale", "skew", "duplicate"})
_COMMON = {"schema_version", "sequence", "previous_hash", "hash", "type", "plan_hash"}
_FIELDS = {"slot": {"slot_id", "kind", "snapshot"}, "begin": {"attempt_id"},
           "terminal": {"attempt_id", "reason", "observation"}, "missed": {"slot_id", "kind"},
           "duplicate": {"slot_id", "kind"}, "plan": {"schedule"}}
_KST = timezone(timedelta(hours=9))
_COMPARISON_FIELDS = {"min_valid_pairs", "outlier_pct", "max_age_seconds", "max_skew_seconds", "expected_market_basis"}


class LedgerError(Exception):
    def __init__(self, code):
        self.code = code if code in {"ledger_corrupt", "ledger_incomplete", "ledger_conflict", "ledger_unavailable"} else "ledger_unavailable"
        super().__init__(self.code)


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")


def _hash(row):
    return sha256(_canonical({k: v for k, v in row.items() if k != "hash"})).hexdigest()


def _require(condition):
    if not condition:
        raise ValueError("schema")


def _text(value, maximum=128):
    return isinstance(value, str) and 0 < len(value) <= maximum and all(32 <= ord(c) < 127 for c in value)


def _hex(value):
    return isinstance(value, str) and re.fullmatch(r"[a-f0-9]{64}", value) is not None


def _symbols(value):
    _require(isinstance(value, (list, tuple)) and len(value) <= 10000)
    _require(all(isinstance(s, str) and re.fullmatch(r"[A-Za-z0-9._-]{1,32}", s) for s in value))
    _require(len(set(value)) == len(value))
    return list(value)


def _timestamp(value):
    if value is None:
        return None
    _require(_text(value, 40))
    parsed = datetime.fromisoformat(value)
    _require(parsed.tzinfo is not None and parsed.utcoffset() is not None)
    return parsed


def _date(value):
    _require(isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value))
    return date.fromisoformat(value)


def _minute(value):
    _require(isinstance(value, str) and re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", value))
    h, m = map(int, value.split(":"))
    return h * 60 + m


def _ratio(value):
    _require(isinstance(value, dict) and set(value) == {"numerator", "denominator"})
    a, b = value["numerator"], value["denominator"]
    _require(type(a) is int and type(b) is int and 0 <= a < 10**100 and 0 < b < 10**100)
    result = Fraction(a, b)
    _require((result.numerator, result.denominator) == (a, b))
    return result


def ratio(value):
    return {"numerator": value.numerator, "denominator": value.denominator}


def comparison_summary(comparisons, *, minimum):
    values = sorted(_ratio(c["delta_pct"]) for c in comparisons if c["valid"])
    n = len(values)
    p95 = values[(95 * n + 99) // 100 - 1] if n else None
    rate = Fraction(sum(c["outlier"] is True for c in comparisons if c["valid"]), n) if n else None
    def display(value):
        if value is None:
            return None
        number = float(value)
        return number if math.isfinite(number) and (number != 0 or value == 0) else None
    return {"status": "sufficient" if n >= minimum and n else "insufficient", "valid_pairs": n,
            "p95_pct": display(p95), "p95_pct_exact": ratio(p95) if p95 is not None else None,
            "outlier_rate": display(rate), "outlier_rate_exact": ratio(rate) if rate is not None else None}


def quote_evidence(q):
    """Retain only documented quote fields, never repr/raw source metadata."""
    if q is None:
        return None
    price = q.price
    if not isinstance(price, Decimal) or not price.is_finite() or abs(price.adjusted()) > 30 or abs(price.as_tuple().exponent) > 30:
        price = None
    return {"symbol": q.symbol, "price": str(price) if price is not None else None,
            "observed_at": q.observed_at.isoformat() if isinstance(q.observed_at, datetime) else None,
            "fetched_at": q.fetched_at.isoformat() if isinstance(q.fetched_at, datetime) else None,
            "status": q.status, "missing_fields": sorted(q.missing_fields),
            "market_basis": q.market_basis, "currency": q.currency}


def _quote(value):
    if value is None:
        return
    _require(isinstance(value, dict) and set(value) == {"symbol", "price", "observed_at", "fetched_at", "status", "missing_fields", "market_basis", "currency"})
    _symbols([value["symbol"]])
    _require(value["status"] in {"ok", "partial", "missing", "invalid", "stale"})
    _require(value["market_basis"] in {"krx", "krx_nxt", "unknown"})
    _require(value["currency"] is None or (isinstance(value["currency"], str) and re.fullmatch(r"[A-Z]{3}", value["currency"])))
    _require(isinstance(value["missing_fields"], list) and len(value["missing_fields"]) <= 16)
    _require(all(s in {"price", "observed_at", "fetched_at", "currency", "market_basis", "symbol", "status", "duplicate", "duplicate_symbol"} for s in value["missing_fields"]))
    for key in ("observed_at", "fetched_at"):
        if value[key] is not None:
            _require(_text(value[key], 40))
            datetime.fromisoformat(value[key])  # retain naive evidence; comparison excludes it
    if value["price"] is not None:
        _require(_text(value["price"], 64))
        number = Decimal(value["price"])
        _require(number.is_finite() and abs(number.adjusted()) <= 30 and abs(number.as_tuple().exponent) <= 30)


def _comparison_policy(value):
    _require(type(value["min_valid_pairs"]) is int and 1 <= value["min_valid_pairs"] <= 10**9)
    _require(type(value["max_age_seconds"]) is int and 0 <= value["max_age_seconds"] <= 10**9)
    _require(type(value["max_skew_seconds"]) is int and 0 <= value["max_skew_seconds"] <= 10**9)
    _require(_text(value["expected_market_basis"], 256))
    _require(_text(value["outlier_pct"], 64) and Decimal(value["outlier_pct"]).is_finite() and 0 <= Decimal(value["outlier_pct"]) <= 1000000)


def _safe_snapshot(snapshot):
    allowed = {"snapshot_id", "selected_at", "source_success_at", "selection_partial", "symbols", "selection_metadata", "cohort", "kis"}
    _require(isinstance(snapshot, Mapping) and not set(snapshot) - allowed)
    _require({"snapshot_id", "selected_at", "source_success_at", "selection_partial", "symbols"} <= set(snapshot))
    _require(_text(snapshot["snapshot_id"]) and type(snapshot["selection_partial"]) is bool)
    for key in ("selected_at", "source_success_at"):
        _timestamp(snapshot[key])
    result = {k: snapshot[k] for k in ("snapshot_id", "selected_at", "source_success_at", "selection_partial")}
    result["symbols"] = _symbols(snapshot["symbols"])
    if "selection_metadata" in snapshot:
        meta = snapshot["selection_metadata"]
        _require(isinstance(meta, Mapping) and set(meta) == {"rule", "holdings", "candidates", "overflow_symbols"})
        _require(meta["rule"] == "score_desc_symbol_asc_holdings_first")
        result["selection_metadata"] = {"rule": meta["rule"], **{k: _symbols(meta[k]) for k in ("holdings", "candidates", "overflow_symbols")}}
        _require(not set(meta["holdings"]) & set(meta["candidates"]))
        _require(set(meta["holdings"]) | set(meta["candidates"]) == set(result["symbols"]))
        _require(not set(meta["overflow_symbols"]) & set(result["symbols"]))
    if "cohort" in snapshot:
        c = snapshot["cohort"]
        _require(isinstance(c, Mapping) and set(c) == {"dataset_kind", "session"} | _COMPARISON_FIELDS)
        _require(c["dataset_kind"] in {"live", "synthetic", "unknown"} and c["session"] in {"regular", "pre", "after", "outside", "calendar", "unknown"})
        _comparison_policy(c)
        result["cohort"] = dict(c)
    if "kis" in snapshot:
        kis = snapshot["kis"]
        _require(isinstance(kis, Mapping) and not set(kis) - set(result["symbols"]))
        result["kis"] = {}
        for symbol, q in kis.items():
            q = dict(q) if isinstance(q, Mapping) else quote_evidence(q)
            _quote(q)
            result["kis"][symbol] = q
    return json.loads(_canonical(result))


class ObservationLedger:
    def __init__(self, path, *, plan_hash, max_bytes):
        if not _hex(plan_hash):
            raise ValueError("invalid_plan_hash")
        if type(max_bytes) is not int or max_bytes <= 0:
            raise ValueError("invalid_max_bytes")
        self.path, self.plan_hash, self.max_bytes = Path(os.path.abspath(path)), plan_hash, max_bytes
        self._opened = self._failed = False
        self._error_code = self._fd = self._schedule = None
        self._dirs, self._rows = [], []
        self._slots, self._attempts, self._terminals = {}, {}, {}
        self._missed, self._begun, self._pairs = set(), set(), set()
        self._duplicates = self._size = 0

    def _fail(self, code):
        self._failed, self._error_code = True, code
        raise LedgerError(code)

    def _open_descriptors(self, *, write):
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        fd = os.open("/", flags)
        self._dirs.append((fd, None))
        parts = self.path.parts[1:-1]
        _require(parts)
        for index, name in enumerate(parts):
            created = False
            try:
                child = os.open(name, flags, dir_fd=fd)
            except FileNotFoundError:
                if not write:
                    raise
                os.mkdir(name, mode=0o700, dir_fd=fd)
                os.fsync(fd)
                child = os.open(name, flags, dir_fd=fd)
                created = True
            self._dirs.append((child, name))
            if created:
                os.fsync(child)
            info = os.fstat(child)
            _require(info.st_uid in {0, os.getuid()})
            if index == len(parts) - 1:
                _require(info.st_uid == os.getuid() and stat.S_IMODE(info.st_mode) == 0o700)
            else:
                _require(not (info.st_mode & 0o022) or (info.st_uid == 0 and info.st_mode & stat.S_ISVTX))
            fd = child
        flags = os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
        if write:
            try:
                self._fd = os.open(self.path.name, flags | os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=fd)
            except FileExistsError:
                self._fd = os.open(self.path.name, flags | os.O_RDWR | os.O_APPEND, dir_fd=fd)
            fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        else:
            self._fd = os.open(self.path.name, flags | os.O_RDONLY, dir_fd=fd)
        self._check_identity()
        if write:
            os.fsync(self._fd)
            os.fsync(fd)

    def _check_identity(self):
        for index, (fd, name) in enumerate(self._dirs[1:], 1):
            info = os.fstat(fd)
            linked = os.stat(name, dir_fd=self._dirs[index - 1][0], follow_symlinks=False)
            _require(stat.S_ISDIR(linked.st_mode) and (info.st_dev, info.st_ino) == (linked.st_dev, linked.st_ino))
            _require(info.st_uid in {0, os.getuid()})
            if index == len(self._dirs) - 1:
                _require(info.st_uid == os.getuid() and stat.S_IMODE(info.st_mode) == 0o700)
            else:
                _require(not (info.st_mode & 0o022) or (info.st_uid == 0 and info.st_mode & stat.S_ISVTX))
        info = os.fstat(self._fd)
        linked = os.stat(self.path.name, dir_fd=self._dirs[-1][0], follow_symlinks=False)
        _require(stat.S_ISREG(info.st_mode) and stat.S_ISREG(linked.st_mode))
        _require(info.st_uid == os.getuid() and info.st_nlink == 1 and stat.S_IMODE(info.st_mode) == 0o600)
        _require((info.st_dev, info.st_ino) == (linked.st_dev, linked.st_ino) and info.st_size <= self.max_bytes)
        return info

    def _load(self):
        size = self._check_identity().st_size
        chunks, remaining = [], self.max_bytes + 1
        while remaining:
            chunk = os.read(self._fd, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        _require(len(raw) == size and len(raw) <= self.max_bytes and (not raw or raw.endswith(b"\n")))
        for line in raw.splitlines():
            row = json.loads(line, object_pairs_hook=_unique, parse_constant=lambda _: _require(False))
            self._validate(row)
            self._apply(row)
            self._rows.append(row)
        self._size = size
        _require(self._check_identity().st_size == size)

    def open(self):
        if self._failed:
            self._fail("ledger_incomplete")
        if self._opened:
            return
        try:
            self._open_descriptors(write=True)
        except BlockingIOError:
            self.close()
            self._fail("ledger_unavailable")
        except (OSError, ValueError):
            self.close()
            self._fail("ledger_corrupt" if self.path.exists() else "ledger_unavailable")
        try:
            self._load()
            self._opened = True
            for attempt in tuple(self._attempts):
                if attempt not in self._terminals:
                    self.finish(attempt, reason="interrupted")
        except LedgerError:
            self.close()
            raise
        except (OSError, ValueError, TypeError, KeyError, ArithmeticError, RecursionError):
            self.close()
            self._fail("ledger_corrupt")

    @classmethod
    def read_only_summary(cls, path, *, plan_hash, max_bytes):
        ledger = cls(path, plan_hash=plan_hash, max_bytes=max_bytes)
        try:
            ledger._open_descriptors(write=False)
            ledger._load()
            ledger._opened = True
        except FileNotFoundError:
            ledger._failed, ledger._error_code = True, "ledger_unavailable"
        except (OSError, ValueError, TypeError, KeyError, ArithmeticError, RecursionError, LedgerError):
            ledger._failed, ledger._error_code = True, "ledger_corrupt"
        finally:
            ledger.close()
        return ledger.summary()

    def _ids(self, slot_id, kind, snapshot):
        symbols = snapshot["symbols"] if kind == "prices" else ["calendar"]
        return tuple(sha256(_canonical([self.plan_hash, slot_id, kind, symbol, snapshot["snapshot_id"]])).hexdigest() for symbol in symbols)

    def _validate(self, row):
        _require(isinstance(row, dict) and row.get("type") in _FIELDS)
        typ = row["type"]
        _require(set(row) == _COMMON | _FIELDS[typ])
        _require(type(row["schema_version"]) is int and row["schema_version"] == 1)
        _require(type(row["sequence"]) is int and row["sequence"] == len(self._rows) + 1)
        _require(row["previous_hash"] == (self._rows[-1]["hash"] if self._rows else "0" * 64))
        _require(row["plan_hash"] == self.plan_hash and row["hash"] == _hash(row))
        if typ in {"slot", "missed", "duplicate"}:
            _require(_text(row["slot_id"]) and row["kind"] in {"prices", "calendar"})
            key = row["kind"], row["slot_id"]
            if typ == "slot":
                snap = _safe_snapshot(row["snapshot"])
                _require(snap == row["snapshot"] and key not in self._missed)
                _require(key not in self._slots or self._slots[key]["snapshot"] == snap)
                cohort = snap.get("cohort")
                if self._schedule is not None:
                    _require(cohort is not None and all(cohort[k] == self._schedule[k] for k in _COMPARISON_FIELDS | {"dataset_kind"}))
                if cohort is not None:
                    for old in self._slots.values():
                        prior = old["snapshot"].get("cohort")
                        if prior and (prior["dataset_kind"], prior["session"]) == (cohort["dataset_kind"], cohort["session"]):
                            _require(prior == cohort)
            elif typ == "missed":
                _require(key not in self._slots)
            else:
                _require(key in self._slots or key in self._missed)
        elif typ in {"begin", "terminal"}:
            attempt = row["attempt_id"]
            _require(isinstance(attempt, str) and attempt in self._attempts)
            if typ == "begin":
                _require(attempt not in self._terminals)
            else:
                _require(row["reason"] in _TERMINAL_REASONS)
                self._validate_observation(row)
                prior = self._terminals.get(attempt)
                _require(prior is None or (prior["reason"], prior["observation"]) == (row["reason"], row["observation"]))
        else:
            self._validate_schedule(row["schedule"])

    def _validate_schedule(self, schedule):
        _require(isinstance(schedule, dict) and set(schedule) == {"dates", "sessions", "calendar_time", "dataset_kind"} | _COMPARISON_FIELDS)
        _require(isinstance(schedule["dates"], list) and 0 < len(schedule["dates"]) <= 3660)
        _require(schedule["dates"] == sorted(set(schedule["dates"])))
        for value in schedule["dates"]:
            _date(value)
        _require(schedule["dataset_kind"] in {"live", "synthetic"})
        _comparison_policy(schedule)
        _minute(schedule["calendar_time"])
        _require(isinstance(schedule["sessions"], list) and 1 <= len(schedule["sessions"]) <= 3)
        intervals, names = [], set()
        for session in schedule["sessions"]:
            _require(isinstance(session, dict) and set(session) == {"name", "start", "end"})
            _require(session["name"] in {"pre", "regular", "after"} and session["name"] not in names)
            names.add(session["name"])
            start, end = _minute(session["start"]), _minute(session["end"])
            _require(start < end)
            intervals.append((start, end))
        intervals.sort()
        _require(all(a[1] <= b[0] for a, b in zip(intervals, intervals[1:])))
        _require(self._schedule is None or self._schedule == schedule)

    def _validate_observation(self, row):
        obs, reason = row["observation"], row["reason"]
        if obs is None:
            _require(reason not in {"success", "excluded"})
            return
        _require(isinstance(obs, dict) and reason in {"success", "excluded"})
        slot = self._slots[self._attempts[row["attempt_id"]]]
        if slot["kind"] == "calendar":
            _require(set(obs) == {"holiday", "requested_date"} and type(obs["holiday"]) is bool and reason == "success")
            _date(obs["requested_date"])
            _require(slot["snapshot"]["snapshot_id"] == "calendar:" + obs["requested_date"])
            return
        _require(set(obs) == {"valid_pairs", "excluded_pairs", "kis", "toss", "comparison", "evaluated_at"})
        evaluated_at = _timestamp(obs["evaluated_at"])
        _require(evaluated_at is not None)
        _require(type(obs["valid_pairs"]) is int and type(obs["excluded_pairs"]) is int and (obs["valid_pairs"], obs["excluded_pairs"]) in {(1, 0), (0, 1)})
        for key in ("kis", "toss"):
            _quote(obs[key])
        comp = obs["comparison"]
        _require(isinstance(comp, dict) and set(comp) == {"valid", "reason", "pair_id", "delta_pct", "outlier"})
        _require(type(comp["valid"]) is bool and comp["valid"] == bool(obs["valid_pairs"]))
        _require(comp["reason"] in _PAIR_REASONS and comp["valid"] == (comp["reason"] == "ok"))
        _require(comp["pair_id"] is None or _hex(comp["pair_id"]))
        if not comp["valid"]:
            _require(comp["delta_pct"] is None and comp["outlier"] is None)
            return
        delta = _ratio(comp["delta_pct"])
        _require(type(comp["outlier"]) is bool and _hex(comp["pair_id"]))
        _require(reason == "success" and obs["kis"] is not None and obs["toss"] is not None)
        kis, toss = obs["kis"], obs["toss"]
        symbol = slot["snapshot"]["symbols"][self._ids(slot["slot_id"], slot["kind"], slot["snapshot"]).index(row["attempt_id"])]
        _require(kis["symbol"] == toss["symbol"] == symbol)
        _require(kis == slot["snapshot"].get("kis", {}).get(symbol))
        _require(kis["symbol"] == toss["symbol"] and kis["currency"] == toss["currency"] == "KRW")
        _require(kis["market_basis"] == toss["market_basis"] and kis["market_basis"] in {"krx", "krx_nxt"})
        _require(all(q["status"] == "ok" and q["price"] is not None and Decimal(q["price"]) > 0 for q in (kis, toss)))
        _require(delta == 100 * abs(Fraction(Decimal(kis["price"])) - Fraction(Decimal(toss["price"]))) / Fraction(Decimal(kis["price"])))
        cohort = slot["snapshot"].get("cohort")
        _require(cohort is not None and comp["outlier"] == (delta > Fraction(cohort["outlier_pct"])))
        _require(kis["market_basis"] == cohort["expected_market_basis"])
        observed = []
        for q in (kis, toss):
            source, fetched = _timestamp(q["observed_at"]), _timestamp(q["fetched_at"])
            _require(source is not None and fetched is not None and source <= fetched <= evaluated_at)
            _require((evaluated_at - source).total_seconds() <= cohort["max_age_seconds"])
            observed.append(source)
        _require(abs((observed[0] - observed[1]).total_seconds()) <= cohort["max_skew_seconds"])
        pair_id = sha256(_canonical([symbol, *(t.astimezone(timezone.utc).isoformat() for t in observed)])).hexdigest()
        _require(comp["pair_id"] == pair_id)
        key = self._cohort_key(slot), comp["pair_id"]
        _require(key not in self._pairs or row["attempt_id"] in self._terminals)

    def _apply(self, row):
        typ = row["type"]
        if typ == "slot":
            key = row["kind"], row["slot_id"]
            self._slots[key] = row
            for attempt in self._ids(row["slot_id"], row["kind"], row["snapshot"]):
                self._attempts[attempt] = key
        elif typ == "begin":
            self._begun.add(row["attempt_id"])
        elif typ == "terminal":
            self._terminals[row["attempt_id"]] = row
            comp = (row["observation"] or {}).get("comparison")
            if comp and comp["valid"]:
                slot = self._slots[self._attempts[row["attempt_id"]]]
                self._pairs.add((self._cohort_key(slot), comp["pair_id"]))
        elif typ == "missed":
            self._missed.add((row["kind"], row["slot_id"]))
        elif typ == "duplicate":
            self._duplicates += 1
        else:
            self._schedule = row["schedule"]

    def _append(self, typ, **fields):
        if not self._opened or self._failed:
            self._fail("ledger_incomplete")
        row = {"schema_version": 1, "sequence": len(self._rows) + 1, "previous_hash": self._rows[-1]["hash"] if self._rows else "0" * 64,
               "type": typ, "plan_hash": self.plan_hash, **fields}
        try:
            row["hash"] = _hash(row)
            self._validate(row)
            payload = _canonical(row) + b"\n"
            row = json.loads(payload)  # caller-owned nested dicts cannot mutate committed state
        except (ValueError, TypeError, KeyError, ArithmeticError, RecursionError):
            self._fail("ledger_conflict")
        try:
            _require(self._check_identity().st_size == self._size)
            _require(self._size + len(payload) <= self.max_bytes)
            offset = interruptions = 0
            while offset < len(payload):
                try:
                    written = os.write(self._fd, payload[offset:])
                except InterruptedError:
                    interruptions += 1
                    _require(interruptions <= 16)
                    continue
                _require(type(written) is int and 0 < written <= len(payload) - offset)
                offset += written
            os.fsync(self._fd)
            _require(self._check_identity().st_size == self._size + len(payload))
        except (OSError, ValueError):
            self._fail("ledger_incomplete")
        self._apply(row)
        self._rows.append(row)
        self._size += len(payload)

    def reserve_slot(self, slot_id, *, kind, snapshot):
        if not self._opened or self._failed:
            self._fail("ledger_incomplete")
        if not _text(slot_id) or kind not in {"prices", "calendar"}:
            self._fail("ledger_conflict")
        if (kind, slot_id) in self._slots or (kind, slot_id) in self._missed:
            self._append("duplicate", slot_id=slot_id, kind=kind)
            return None
        try:
            safe = _safe_snapshot(snapshot)
        except (ValueError, TypeError, KeyError, ArithmeticError):
            self._fail("ledger_conflict")
        self._append("slot", slot_id=slot_id, kind=kind, snapshot=safe)
        return self._ids(slot_id, kind, safe)

    def begin(self, attempt_id):
        if self._failed or not self._opened:
            self._fail("ledger_incomplete")
        if attempt_id not in self._attempts:
            self._fail("ledger_conflict")
        if attempt_id not in self._begun and attempt_id not in self._terminals:
            self._append("begin", attempt_id=attempt_id)

    def finish(self, attempt_id, *, reason, observation=None):
        if self._failed or not self._opened:
            self._fail("ledger_incomplete")
        prior = self._terminals.get(attempt_id)
        if prior is not None:
            try:
                identical = _canonical([prior["reason"], prior["observation"]]) == _canonical([reason, observation])
            except (TypeError, ValueError, ArithmeticError, RecursionError):
                identical = False
            if not identical:
                self._fail("ledger_conflict")
            return
        self._append("terminal", attempt_id=attempt_id, reason=reason, observation=observation)

    def record_missed(self, slot_id, *, kind):
        typ = "duplicate" if (kind, slot_id) in self._missed or (kind, slot_id) in self._slots else "missed"
        self._append(typ, slot_id=slot_id, kind=kind)

    def configure_plan(self, policy):
        schedule = {k: policy[k] for k in ("dates", "sessions", "calendar_time", "dataset_kind")}
        schedule["dates"] = list(schedule["dates"])
        schedule["sessions"] = [dict(s) for s in schedule["sessions"]]
        schedule.update({k: policy["comparison"][k] for k in _COMPARISON_FIELDS})
        schedule["outlier_pct"] = str(schedule["outlier_pct"])
        if self._schedule != schedule:
            self._append("plan", schedule=schedule)

    def _cohort_key(self, slot):
        c = slot["snapshot"].get("cohort", {})
        return self.plan_hash, c.get("dataset_kind", "unknown"), c.get("session", "unknown")

    def pair_seen(self, *, attempt_id, pair_id):
        return (self._cohort_key(self._slots[self._attempts[attempt_id]]), pair_id) in self._pairs

    def _counts(self, kind=None):
        attempts = {a for a, key in self._attempts.items() if kind is None or key[0] == kind}
        terminals = [r for a, r in self._terminals.items() if a in attempts]
        reasons = {r: sum(t["reason"] == r for t in terminals) for r in sorted(_TERMINAL_REASONS)}
        return {"recorded_slots": sum(kind is None or key[0] == kind for key in self._slots),
                "missed_slots": sum(kind is None or key[0] == kind for key in self._missed),
                "selected_attempts": len(attempts), "terminal_attempts": len(terminals), "unfinished_attempts": len(attempts) - len(terminals),
                "valid_pairs": sum((t["observation"] or {}).get("valid_pairs", 0) for t in terminals),
                "excluded_pairs": sum((t["observation"] or {}).get("excluded_pairs", 0) for t in terminals),
                "provider_failures": reasons["provider_failure"], "budget_skips": reasons["budget_skip"],
                "interrupted": reasons["interrupted"], "cancelled": reasons["cancelled"], "calendar_invalid": reasons["calendar_invalid"],
                "terminal_reasons": reasons}

    def _coverage(self):
        if self._schedule is None:
            return {"expected_slots": None, "unaccounted_slots": None, "coverage_status": "unavailable", "coverage_scope": "full_plan"}
        price_minutes = {minute for session in self._schedule["sessions"] for minute in
            range(((_minute(session["start"]) + 4) // 5) * 5, _minute(session["end"]), 5)}
        dates = set(self._schedule["dates"])
        expected_count = (len(price_minutes) + 1) * len(dates)
        accounted = set(self._slots) | self._missed
        matched = set()
        for kind, slot_id in accounted:
            try:
                at = _timestamp(slot_id)
                if at is None or at.utcoffset() != timedelta(hours=9) or at.isoformat() != slot_id:
                    continue
                minute = at.hour * 60 + at.minute
                if at.date().isoformat() in dates and at.second == at.microsecond == 0 and (
                    (kind == "prices" and minute in price_minutes) or
                    (kind == "calendar" and minute == _minute(self._schedule["calendar_time"]))):
                    matched.add((kind, slot_id))
            except (ValueError, TypeError):
                continue
        unaccounted = expected_count - len(matched)
        return {"expected_slots": expected_count, "unaccounted_slots": unaccounted,
                "unexpected_slots": len(accounted - matched), "coverage_status": "incomplete" if unaccounted else "complete", "coverage_scope": "full_plan"}

    def summary(self):
        cohorts = {}
        for slot in self._slots.values():
            if slot["kind"] != "prices":
                continue
            key = self._cohort_key(slot)
            group = cohorts.setdefault(key, {"comparisons": [], "selected_attempts": 0, "terminal_attempts": 0, "by_date": {},
                "plan_hash": key[0], "dataset_kind": key[1], "session": key[2], "min_valid_pairs": slot["snapshot"].get("cohort", {}).get("min_valid_pairs", 1)})
            group["selected_attempts"] += len(slot["snapshot"]["symbols"])
            stamp = _timestamp(slot["snapshot"]["selected_at"])
            day = stamp.astimezone(_KST).date().isoformat() if stamp is not None else "unknown"
            day_counts = group["by_date"].setdefault(day, {"selected_attempts": 0, "terminal_attempts": 0, "valid_pairs": 0, "excluded_pairs": 0})
            day_counts["selected_attempts"] += len(slot["snapshot"]["symbols"])
        for attempt, row in self._terminals.items():
            slot = self._slots[self._attempts[attempt]]
            if slot["kind"] != "prices":
                continue
            group = cohorts[self._cohort_key(slot)]
            group["terminal_attempts"] += 1
            obs = row["observation"] or {}
            stamp = _timestamp(slot["snapshot"]["selected_at"])
            day = stamp.astimezone(_KST).date().isoformat() if stamp is not None else "unknown"
            day_counts = group["by_date"][day]
            day_counts["terminal_attempts"] += 1
            day_counts["valid_pairs"] += obs.get("valid_pairs", 0)
            day_counts["excluded_pairs"] += obs.get("excluded_pairs", 0)
            if "comparison" in obs:
                group["comparisons"].append(obs["comparison"])
        reports = []
        for group in cohorts.values():
            comparisons = group.pop("comparisons")
            group["comparison"] = comparison_summary(comparisons, minimum=group["min_valid_pairs"])
            group["excluded_pairs"] = sum(not c["valid"] for c in comparisons)
            reports.append(group)
        counts = self._counts()
        return {**counts, **self._coverage(), "duplicate_submissions": self._duplicates,
                "by_kind": {k: self._counts(k) for k in ("prices", "calendar")}, "cohorts": reports,
                "comparison": reports[0]["comparison"] if len(reports) == 1 else comparison_summary([], minimum=1),
                "incomplete": self._failed or counts["unfinished_attempts"] != 0, "error_code": self._error_code,
                "production_eligible": False}

    def close(self):
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None
        for fd, _ in reversed(self._dirs):
            os.close(fd)
        self._dirs.clear()
        self._opened = False


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result
