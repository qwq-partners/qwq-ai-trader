"""Explicit, evidence-bound operator repair of unresolved charges; never calls APIs.

Evidence hashes bind a reviewed artifact, not its truth. Default is read-only.
Known charges, original month/activation and configured limit cannot be reduced.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from .llm_budget import MonthlyBudgetLedger, _units, _SCALE


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                    allow_nan=False).encode()).hexdigest()


def reconcile_budget(path, limit, review, evidence_path, *, apply=False, expected_state_sha256=None):
    path = Path(path).expanduser()
    if not path.is_absolute() or not path.is_file():
        raise ValueError('existing absolute ledger path required')
    required = {'version','activated_at','reviewer_ref','evidence_sha256','adjustments','clear_bound_breach'}
    if (type(review) is not dict or set(review) != required
            or review['version'] != 'llm-budget-reconciliation-v1'
            or type(review['clear_bound_breach']) is not bool
            or not isinstance(review['reviewer_ref'],str)
            or re.fullmatch(r'[A-Za-z0-9._:/-]{1,120}',review['reviewer_ref']) is None
            or not isinstance(review['evidence_sha256'],str)
            or re.fullmatch(r'[0-9a-f]{64}',review['evidence_sha256']) is None):
        raise ValueError('invalid reconciliation review')
    evidence_path = Path(evidence_path)
    if evidence_path.stat().st_size > 16 * 1024 * 1024:
        raise ValueError('evidence exceeds 16 MiB')
    evidence = evidence_path.read_bytes()
    if len(evidence) > 16 * 1024 * 1024 or hashlib.sha256(evidence).hexdigest() != review['evidence_sha256']:
        raise ValueError('evidence hash mismatch')
    changes = review['adjustments']
    if type(changes) is not list or len(changes)>1000 or (not changes and not review['clear_bound_breach']):
        raise ValueError('explicit nonempty review required')
    costs = {}
    for row in changes:
        if (type(row) is not dict or set(row) != {'token','actual_cost_usd'}
                or not isinstance(row['token'],str) or re.fullmatch(r'[0-9a-f]{32}',row['token']) is None
                or row['token'] in costs):
            raise ValueError('invalid or duplicate adjustment')
        costs[row['token']] = _units(row['actual_cost_usd'])
    # Existing file only. Initialization verifies marker/schema without reseeding.
    ledger = MonthlyBudgetLedger(path, limit)
    try:
        with ledger._connect(readonly=not apply) as db:
            db.execute('BEGIN IMMEDIATE' if apply else 'BEGIN')
            settings = dict(ledger._settings(db))
            if review['activated_at'] != settings['activated_at']:
                raise ValueError('activation identity mismatch')
            rows = [dict(row) for row in db.execute('SELECT * FROM reservations ORDER BY token')]
            identity = _digest({'settings':settings,'reservations':rows})
            if apply and expected_state_sha256 != identity:
                raise ValueError('dry-run state changed or missing')
            by_token = {row['token']:row for row in rows}
            unresolved = set()
            for row in rows:
                if row['state']=='unknown':
                    unresolved.add(row['token'])
                elif row['state']=='reserved':
                    if not ledger._orphaned(row['token']):
                        raise ValueError('active reservation: wait for completion before reconciliation')
                    unresolved.add(row['token'])
            if set(costs)-unresolved:
                raise ValueError('only unresolved reservations may be reconciled')
            new_breaches = {token for token, units in costs.items()
                            if units > by_token[token]['reserved_units']}
            if review['clear_bound_breach'] and new_breaches:
                raise ValueError('new reservation breach requires a separate acknowledgment review')
            if review['clear_bound_breach'] and (not settings['bound_breach'] or unresolved-set(costs)):
                raise ValueError('breach acknowledgment requires existing breach and all costs resolved')
            result = {'applied':bool(apply),'state_sha256':identity,'review_sha256':_digest(review),
                      'adjustment_count':len(costs),'new_cost_usd':sum(costs.values())/_SCALE,
                      'remaining_unresolved_count':len(unresolved-set(costs)),
                      'new_bound_breach_count':len(new_breaches),
                      'clear_bound_breach':review['clear_bound_breach']}
            if apply:
                now = ledger._now().isoformat()
                db.execute('''CREATE TABLE IF NOT EXISTS reconciliations (
                    id INTEGER PRIMARY KEY, applied_at TEXT NOT NULL, reviewer_ref TEXT NOT NULL,
                    evidence_sha256 TEXT NOT NULL, review_sha256 TEXT NOT NULL,
                    previous_state_sha256 TEXT NOT NULL, adjustments_json TEXT NOT NULL,
                    cleared_bound_breach INTEGER NOT NULL)''')
                for token, units in costs.items():
                    db.execute("UPDATE reservations SET state='settled',actual_units=?,finished_at=? WHERE token=?",
                               (units,now,token))
                if new_breaches:
                    db.execute('UPDATE settings SET bound_breach=1 WHERE id=1')
                if review['clear_bound_breach']:
                    db.execute('UPDATE settings SET bound_breach=0 WHERE id=1')
                db.execute('INSERT INTO reconciliations VALUES (NULL,?,?,?,?,?,?,?)',
                           (now, review['reviewer_ref'], review['evidence_sha256'],result['review_sha256'],identity,
                            json.dumps(changes,sort_keys=True),int(review['clear_bound_breach'])))
                db.execute('COMMIT')
            else:
                db.execute('ROLLBACK')
            return result
    finally:
        ledger.close()
