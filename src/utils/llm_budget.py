"""Durable shared OpenAI/Gemini reservation ledger, local POSIX filesystem only.

Money uses integer nano-USD. SQLite serializes admission/settlement; a held flock
identifies each live reservation. A lost process releases that lock, NOT its
possible charge. Orphaned/unknown usage blocks all months until explicit repair.
Reservations are conservative estimates, not provider invoice guarantees.
"""
from __future__ import annotations

import fcntl
import math
import os
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal, ROUND_CEILING
from pathlib import Path
from typing import Callable, Optional
from zoneinfo import ZoneInfo

from .llm_pricing import rate_quote

KST = ZoneInfo('Asia/Seoul')
_SCALE = 1_000_000_000
_MAX_UNITS = 2 ** 62


class BudgetBlocked(Exception):
    """Static machine-readable reason; never includes storage paths or payloads."""
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


class BudgetStateError(BudgetBlocked):
    pass


def _units(value) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise BudgetBlocked('invalid_cost')
    result = int((Decimal(str(value)) * _SCALE).to_integral_value(rounding=ROUND_CEILING))
    if result >= _MAX_UNITS:
        raise BudgetBlocked('invalid_cost')
    return result


class MonthlyBudgetLedger:
    def __init__(self, path, monthly_limit_usd: float, *, clock: Optional[Callable] = None):
        self.limit_units = _units(monthly_limit_usd)
        self.path = Path(path).expanduser()
        if not self.path.is_absolute():
            raise BudgetStateError('invalid_budget_state_path')
        self.lock_dir = self.path.with_name(self.path.name + '.locks')
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.active_reservations: dict[str, int] = {}
        self._initialize()

    def _now(self) -> datetime:
        now = self.clock()
        if not isinstance(now, datetime) or now.tzinfo is None:
            raise BudgetStateError('invalid_clock')
        return now.astimezone(KST)

    def today(self):
        return self._now().date()

    @contextmanager
    def _connect(self, *, readonly=False):
        # Explicit Python filesystem boundary: sqlite's C open must not bypass
        # the project's test isolation guard. Never recreate a missing ledger.
        self.path.stat()
        mode = 'ro' if readonly else 'rw'
        connection = sqlite3.connect(self.path.as_uri() + '?mode=' + mode, uri=True,
                                     timeout=5, isolation_level=None)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute('PRAGMA synchronous=FULL')
            yield connection
        finally:
            connection.close()

    def _initialize(self):
        init_fd = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.lock_dir.mkdir(exist_ok=True)
            init_path = self.path.with_name(self.path.name + '.init.lock')
            init_fd = os.open(init_path, os.O_CREAT | os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
            fcntl.flock(init_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            marker = self.path.with_name(self.path.name + '.activated')
            fresh = not self.path.exists()
            if fresh:
                if marker.exists() or any(self.lock_dir.iterdir()):
                    raise BudgetStateError('previous_ledger_missing')
                marker_fd = os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_CLOEXEC, 0o600)
                try:
                    os.write(marker_fd, b'llm-monthly-budget-v1\n')
                    os.fsync(marker_fd)
                finally:
                    os.close(marker_fd)
                directory_fd = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_CLOEXEC, 0o600)
                os.close(fd)
            elif marker.read_bytes() != b'llm-monthly-budget-v1\n':
                raise BudgetStateError('activation_marker_invalid')
            with self._connect() as db:
                if fresh:
                    now = self._now()
                    db.execute('BEGIN IMMEDIATE')
                    db.execute('''CREATE TABLE settings (
                        id INTEGER PRIMARY KEY CHECK(id=1), schema_version INTEGER NOT NULL CHECK(schema_version=1),
                        limit_units INTEGER NOT NULL CHECK(limit_units>=0), activated_at TEXT NOT NULL,
                        baseline_month TEXT NOT NULL, baseline_units INTEGER NOT NULL CHECK(baseline_units=0),
                        bound_breach INTEGER NOT NULL CHECK(bound_breach IN (0,1)))''')
                    db.execute('INSERT INTO settings VALUES (1,1,?,?,?,0,0)',
                               (self.limit_units, now.isoformat(), now.strftime('%Y-%m')))
                    db.execute('''CREATE TABLE reservations (
                        token TEXT PRIMARY KEY, month TEXT NOT NULL, model TEXT NOT NULL,
                        reserved_units INTEGER NOT NULL CHECK(reserved_units>=0),
                        state TEXT NOT NULL CHECK(state IN ('reserved','settled','released','unknown')),
                        actual_units INTEGER CHECK(actual_units>=0), created_at TEXT NOT NULL,
                        finished_at TEXT)''')
                    db.execute('CREATE INDEX reservation_month_state ON reservations(month,state)')
                    db.execute('CREATE INDEX reservation_state ON reservations(state)')
                    db.execute('COMMIT')
                if db.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                    raise BudgetStateError('storage_corrupt')
                self._settings(db)
                db.execute('SELECT token, state FROM reservations LIMIT 1').fetchall()
        except BudgetBlocked:
            raise
        except (OSError, sqlite3.Error, TypeError, ValueError):
            raise BudgetStateError('storage_unavailable') from None
        finally:
            if init_fd is not None:
                os.close(init_fd)

    def _settings(self, db):
        rows = db.execute('SELECT * FROM settings').fetchall()
        if len(rows) != 1 or rows[0]['schema_version'] != 1 or rows[0]['baseline_units'] != 0:
            raise BudgetStateError('storage_corrupt')
        row = rows[0]
        if row['limit_units'] != self.limit_units:
            raise BudgetStateError('budget_configuration_mismatch')
        return row

    def _orphaned(self, token: str) -> bool:
        if not isinstance(token, str) or len(token) != 32 or any(c not in '0123456789abcdef' for c in token):
            raise BudgetStateError('storage_corrupt')
        fd = None
        try:
            fd = os.open(self.lock_dir / (token + '.lock'), os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return False
            return True
        except FileNotFoundError:
            return True
        finally:
            if fd is not None:
                os.close(fd)

    def _snapshot(self, db, *, persist_orphans=False, now=None):
        settings = self._settings(db)
        month = (now or self._now()).strftime('%Y-%m')
        unresolved = db.execute("SELECT COUNT(*) FROM reservations WHERE state='unknown'").fetchone()[0]
        row = db.execute("""SELECT COALESCE(SUM(actual_units),0), COUNT(*) FROM reservations
                          WHERE month=? AND state='settled'""", (month,)).fetchone()
        spent, settled_count = row[0], row[1]
        if not isinstance(spent, int) or spent < 0:
            raise BudgetStateError('storage_corrupt')
        reserved = 0
        # Only outstanding reservations need OS lock checks; settled history is
        # aggregated using month/state indexes, not loaded on every health read.
        for row in db.execute("SELECT token,month,reserved_units FROM reservations WHERE state='reserved'").fetchall():
            if self._orphaned(row['token']):
                unresolved += 1
                if persist_orphans:
                    db.execute("UPDATE reservations SET state='unknown' WHERE token=?", (row['token'],))
            elif row['month'] == month:
                reserved += row['reserved_units']
        reason = ('reservation_bound_breach' if settings['bound_breach'] else
                  'unresolved_usage' if unresolved else
                  'monthly_limit_exceeded' if spent + reserved >= self.limit_units else None)
        return {
            'month': month, 'timezone': 'Asia/Seoul', 'monthly_limit_usd': self.limit_units / _SCALE,
            'activated_at': settings['activated_at'], 'baseline_month': settings['baseline_month'],
            'baseline_cost_usd': 0.0, 'spent_usd': spent / _SCALE, 'reserved_usd': reserved / _SCALE,
            'available_usd': None if reason in {'reservation_bound_breach', 'unresolved_usage'}
                             else max(0, (self.limit_units - spent - reserved) / _SCALE),
            'unresolved_count': unresolved, 'settled_count': settled_count,
            'bound_breach': bool(settings['bound_breach']), 'blocked': bool(reason),
            'block_reason': reason, 'cost_basis': 'published_rate_estimate_not_invoice_guarantee',
        }, spent + reserved

    def status(self) -> dict:
        """Read-only inspection; does not initialize, reset, release or call APIs."""
        try:
            with self._connect(readonly=True) as db:
                db.execute('BEGIN')
                return self._snapshot(db)[0]
        except BudgetBlocked:
            raise
        except (OSError, sqlite3.Error, TypeError, ValueError):
            raise BudgetStateError('storage_unavailable') from None

    def reserve(self, model: str, prompt: str, system: str, max_output_tokens: int) -> str:
        """Conservative admission estimate: twice UTF-8 bytes plus 256 overhead.

        Output reserve includes the requested full output limit (reasoning too).
        Month attribution is admission time, not a provider billing promise.
        """
        if (not isinstance(model, str) or not isinstance(prompt, str) or not isinstance(system, str)
                or isinstance(max_output_tokens, bool) or not isinstance(max_output_tokens, int)
                or not 0 < max_output_tokens <= 2 ** 31 - 1):
            raise BudgetBlocked('invalid_request_estimate')
        input_estimate = 2 * (len(prompt.encode('utf-8')) + len(system.encode('utf-8')) + 256)
        token = uuid.uuid4().hex
        fd = None
        admitted = staged = False
        try:
            with self._connect() as db:
                db.execute('BEGIN IMMEDIATE')
                # Capture admission once: pricing, budget and row month cannot
                # straddle midnight and charge a different month than checked.
                now = self._now()
                quote = rate_quote(model, input_estimate, max_output_tokens, on_date=now.date())
                if quote is None:
                    raise BudgetBlocked('unpriced_model')
                units = _units(quote.cost_usd)
                status, committed = self._snapshot(db, persist_orphans=True, now=now)
                if status['blocked']:
                    db.execute('COMMIT')
                    raise BudgetBlocked(status['block_reason'])
                if committed + units > self.limit_units:
                    db.execute('COMMIT')
                    raise BudgetBlocked('monthly_limit_exceeded')
                fd = os.open(self.lock_dir / (token + '.lock'),
                             os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                staged = True
                db.execute("INSERT INTO reservations VALUES (?,?,?,?,'reserved',NULL,?,NULL)",
                           (token, now.strftime('%Y-%m'), model, units, now.isoformat()))
                db.execute('COMMIT')
            self.active_reservations[token] = fd
            admitted = True
            return token
        except BudgetBlocked:
            raise
        except (OSError, sqlite3.Error, TypeError, ValueError):
            raise BudgetStateError('storage_unavailable') from None
        finally:
            if not admitted and fd is not None:
                os.close(fd)
                if not staged:
                    try:
                        (self.lock_dir / (token + '.lock')).unlink()
                    except OSError:
                        pass
                # Once staged, commit may have succeeded before an I/O error.
                # Leave its lock file and row as durable potential paid usage.

    def settle(self, token: str, *, outcome: str, actual_cost_usd=None):
        if outcome not in {'known', 'unknown', 'not_sent', 'rejected'}:
            raise BudgetBlocked('invalid_settlement')
        committed = False
        try:
            actual_units = _units(actual_cost_usd) if outcome == 'known' else None
            with self._connect() as db:
                db.execute('BEGIN IMMEDIATE')
                self._settings(db)
                row = db.execute('SELECT * FROM reservations WHERE token=?', (token,)).fetchone()
                if row is None or row['state'] != 'reserved' or token not in self.active_reservations:
                    raise BudgetStateError('invalid_reservation')
                state = {'known': 'settled', 'unknown': 'unknown', 'not_sent': 'released', 'rejected': 'released'}[outcome]
                db.execute('UPDATE reservations SET state=?,actual_units=?,finished_at=? WHERE token=?',
                           (state, actual_units, self._now().isoformat(), token))
                if actual_units is not None and actual_units > row['reserved_units']:
                    db.execute('UPDATE settings SET bound_breach=1 WHERE id=1')
                db.execute('COMMIT')
                committed = True
        except BudgetBlocked:
            raise
        except (OSError, sqlite3.Error, TypeError, ValueError):
            raise BudgetStateError('storage_unavailable') from None
        finally:
            fd = self.active_reservations.pop(token, None)
            if fd is not None:
                os.close(fd)
            if committed:
                try:
                    (self.lock_dir / (token + '.lock')).unlink()
                except OSError:
                    pass

    def close(self):
        """Lose ownership only; unfinished attempts remain uncertain, never freed."""
        for fd in self.active_reservations.values():
            os.close(fd)
        self.active_reservations.clear()
