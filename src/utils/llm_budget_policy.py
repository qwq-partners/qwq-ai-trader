"""Transactional policy/outbox extension; never sends messages or calls providers.

The financial ledger owns every transaction. This optional extension retains
monthly high-water marks and attribution without rewriting financial history.
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import timedelta

POLICY_VERSION = 'budget-economy-v1'


class BudgetPolicyMixin:
    def _initialize_policy(self, db):
        from .llm_budget import BudgetStateError

        db.execute('BEGIN IMMEDIATE')
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        required = {'policy_meta', 'policy_months', 'policy_cursor', 'policy_notices', 'policy_attempts'}
        if tables & required:
            if not required <= tables:
                raise BudgetStateError('policy_storage_incomplete')
            rows = db.execute('SELECT version FROM policy_meta').fetchall()
            if len(rows) != 1 or rows[0][0] != POLICY_VERSION:
                raise BudgetStateError('policy_version_mismatch')
            # Reopening an activated policy must not recreate missing history.
            for table, columns in (
                ('policy_months', 'month,level'), ('policy_cursor', 'id,month,mode,reason'),
                ('policy_notices', 'sequence,event_id,payload,created_at,claim_token,lease_until,attempts,next_try,delivered_at'),
                ('policy_attempts', 'token,version,mode,priority,task,role'),
            ):
                db.execute(f'SELECT {columns} FROM {table} LIMIT 0')
            db.execute('COMMIT')
            return
        db.execute('CREATE TABLE policy_meta (id INTEGER PRIMARY KEY CHECK(id=1), version TEXT NOT NULL)')
        db.execute('INSERT INTO policy_meta VALUES (1,?)', (POLICY_VERSION,))
        for statement in (
            '''CREATE TABLE IF NOT EXISTS policy_months (
                month TEXT PRIMARY KEY, level INTEGER NOT NULL CHECK(level BETWEEN 0 AND 2))''',
            '''CREATE TABLE IF NOT EXISTS policy_cursor (
                id INTEGER PRIMARY KEY CHECK(id=1), month TEXT NOT NULL,
                mode TEXT NOT NULL, reason TEXT)''',
            '''CREATE TABLE IF NOT EXISTS policy_notices (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT NOT NULL UNIQUE,
                payload TEXT NOT NULL, created_at REAL NOT NULL,
                claim_token TEXT, lease_until REAL NOT NULL DEFAULT 0,
                attempts INTEGER NOT NULL DEFAULT 0, next_try REAL NOT NULL DEFAULT 0,
                delivered_at REAL)''',
            '''CREATE TABLE IF NOT EXISTS policy_attempts (
                token TEXT PRIMARY KEY, version TEXT NOT NULL, mode TEXT NOT NULL,
                priority TEXT NOT NULL, task TEXT NOT NULL, role TEXT NOT NULL)''',
        ):
            db.execute(statement)
        db.execute('COMMIT')

    def _policy_view(self, db, status, committed):
        row = db.execute('SELECT level FROM policy_months WHERE month=?', (status['month'],)).fetchone()
        level = max(row['level'] if row else 0,
                    2 if committed * 100 >= self.limit_units * 95 else
                    1 if committed * 100 >= self.limit_units * 80 else 0)
        mode = ('paused' if status['block_reason'] == 'monthly_limit_exceeded' else
                'blocked' if status['blocked'] else ('normal', 'economy', 'essential')[level])
        reason = status['block_reason'] or ('threshold_95' if level == 2 else 'threshold_80' if level else None)
        return {**status, 'policy_mode': mode, 'policy_reason': reason, 'policy_version': POLICY_VERSION}, level

    def _observe_policy(self, db, status, committed, now):
        from .llm_budget import BudgetStateError

        month = status['month']
        previous = db.execute('SELECT * FROM policy_cursor WHERE id=1').fetchone()
        if previous is not None and previous['month'] > month:
            raise BudgetStateError('policy_clock_regression')
        status, level = self._policy_view(db, status, committed)
        db.execute('INSERT INTO policy_months VALUES (?,?) ON CONFLICT(month) DO UPDATE SET level=excluded.level',
                   (month, level))
        mode, reason = status['policy_mode'], status['policy_reason']
        reset = previous is not None and previous['month'] != month
        changed = (previous is None and mode != 'normal') or (previous is not None and (
            reset or previous['mode'] != mode or previous['reason'] != reason))
        if changed:
            payload = {**status, 'event_at': now.isoformat(), 'kind': 'month_reset' if reset else 'transition'}
            if reset:
                previous_date = now.replace(day=1) - timedelta(days=1)
                prior = self._snapshot(db, now=previous_date)[0]
                payload['previous_month'] = {key: prior[key] for key in (
                    'month', 'spent_usd', 'reserved_usd', 'unresolved_count',
                    'uncertain_cost_ceiling_usd', 'bounded_unresolved_count',
                    'unbounded_unresolved_count', 'missing_cost_policy',
                    'skipped_unknown_count', 'cost_accounting_complete') if key in prior}
            event_id = uuid.uuid4().hex
            sequence = db.execute('INSERT INTO policy_notices(event_id,payload,created_at) VALUES (?,?,?)',
                                  (event_id, json.dumps(payload, ensure_ascii=False), now.timestamp())).lastrowid
            payload['sequence'] = sequence
            db.execute('UPDATE policy_notices SET payload=? WHERE event_id=?',
                       (json.dumps(payload, ensure_ascii=False), event_id))
        db.execute('INSERT INTO policy_cursor VALUES (1,?,?,?) ON CONFLICT(id) DO UPDATE SET '
                   'month=excluded.month,mode=excluded.mode,reason=excluded.reason', (month, mode, reason))
        return status

    def policy_status(self):
        from .llm_budget import BudgetBlocked, BudgetStateError

        if not self.policy_enabled:
            return self.status()
        try:
            with self._connect() as db:
                db.execute('BEGIN IMMEDIATE')
                now = self._now()
                status, committed = self._snapshot(db, persist_orphans=True, now=now)
                status = self._observe_policy(db, status, committed, now)
                db.execute('COMMIT')
                return status
        except BudgetBlocked:
            raise
        except (OSError, sqlite3.Error, TypeError, ValueError):
            raise BudgetStateError('storage_unavailable') from None

    def claim_policy_notice(self, *, lease_seconds=120):
        from .llm_budget import BudgetBlocked, BudgetStateError

        if not self.policy_enabled:
            return None
        if isinstance(lease_seconds, bool) or not isinstance(lease_seconds, int) or not 1 <= lease_seconds <= 3600:
            raise BudgetStateError('invalid_notice_lease')
        try:
            with self._connect() as db:
                db.execute('BEGIN IMMEDIATE')
                now = self._now().timestamp()
                # Preserve event order: a leased/backed-off head holds later notices.
                row = db.execute('SELECT * FROM policy_notices WHERE delivered_at IS NULL '
                                 'ORDER BY sequence LIMIT 1').fetchone()
                if row is None or row['lease_until'] > now or row['next_try'] > now:
                    db.execute('COMMIT')
                    return None
                claim = uuid.uuid4().hex
                db.execute('UPDATE policy_notices SET claim_token=?,lease_until=?,attempts=attempts+1 '
                           'WHERE event_id=?', (claim, now + lease_seconds, row['event_id']))
                result = {'event_id': row['event_id'], 'sequence': row['sequence'],
                          'claim_token': claim, 'payload': json.loads(row['payload'])}
                db.execute('COMMIT')
                return result
        except BudgetBlocked:
            raise
        except (OSError, sqlite3.Error, TypeError, ValueError):
            raise BudgetStateError('storage_unavailable') from None

    def finish_policy_notice(self, event_id, claim_token, *, delivered):
        from .llm_budget import BudgetBlocked, BudgetStateError

        try:
            with self._connect() as db:
                db.execute('BEGIN IMMEDIATE')
                row = db.execute('SELECT * FROM policy_notices WHERE event_id=? AND claim_token=? '
                                 'AND delivered_at IS NULL', (event_id, claim_token)).fetchone()
                if row is not None:
                    now = self._now().timestamp()
                    delay = min(3600, 60 * 2 ** min(6, max(0, row['attempts'] - 1)))
                    db.execute('UPDATE policy_notices SET delivered_at=?,claim_token=NULL,lease_until=0,next_try=? '
                               'WHERE event_id=? AND claim_token=?',
                               (now if delivered is True else None, now + delay, event_id, claim_token))
                db.execute('COMMIT')
        except BudgetBlocked:
            raise
        except (OSError, sqlite3.Error, TypeError, ValueError):
            raise BudgetStateError('storage_unavailable') from None
