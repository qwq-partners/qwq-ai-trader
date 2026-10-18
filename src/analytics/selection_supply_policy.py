"""Decision evidence for supply absence, independent of optional observation hooks.

Only a present, valid FHPTJ04400000 quantity establishes a state. Ranking misses
and legacy normalized cache entries remain unknown. Proof has one absolute TTL,
so passing through the provider and screener caches cannot renew its freshness.
"""
from contextvars import ContextVar
from functools import wraps
import math
import re
import time

POLICY_ID = 'kr-supply-absence-observed-v1'
MAX_AGE_SECONDS = 600
MAX_ROWS = 512
_ACTIVE = ContextVar('selection_supply_policy', default=None)


def validated_quantity(raw):
    if type(raw) is int:
        value = raw
    elif type(raw) is str:
        raw = raw.strip()
        if len(raw) > 14 or re.fullmatch(r'[+-]?[0-9]+', raw) is None:
            return None
        value = int(raw)
    else:
        return None
    return value if abs(value) <= 10**12 else None


def supply_proof(raw):
    value = validated_quantity(raw)
    return {'value': value, 'observed_monotonic': time.monotonic()} if value is not None else None


def _fresh_value(proof):
    if type(proof) is not dict:
        return None
    value, at = proof.get('value'), proof.get('observed_monotonic')
    if type(value) is not int or abs(value) > 10**12 or type(at) not in (int, float) or not math.isfinite(at):
        return None
    return value if 0 <= time.monotonic() - at <= MAX_AGE_SECONDS else None


def supply_scan(func):
    @wraps(func)
    async def wrapped(*args, **kwargs):
        token = _ACTIVE.set({})
        try:
            return await func(*args, **kwargs)
        finally:
            _ACTIVE.reset(token)
    return wrapped


def evidence_rows(rows):
    """Bounded snapshot before price/liquidity/rank filtering, with conflicts unknown."""
    result = {}
    for row in rows:
        symbol = row.get('symbol')
        if not isinstance(symbol, str) or re.fullmatch(r'[0-9]{6}', symbol) is None or symbol == '000000':
            continue
        if symbol not in result and len(result) >= MAX_ROWS:
            return {}  # a truncated snapshot cannot resolve later conflicts
        proof = row.get('_supply_proof')
        value = _fresh_value(proof)
        if symbol in result and _fresh_value(result[symbol]) != value:
            result[symbol] = None
        elif symbol not in result:
            result[symbol] = dict(proof) if value is not None else None
    return result


def record_evidence(investor, rows):
    active = _ACTIVE.get()
    if active is not None:
        active[investor] = rows


def state(investor, symbol):
    active = _ACTIVE.get()
    proof = active.get(investor, {}).get(symbol) if active is not None else None
    value = _fresh_value(proof)
    return 'unobserved' if value is None else 'positive' if value > 0 else 'observed_nonpositive'


def observed_no_buying(stock):
    # Existing positive flags remain valid positive signals; never infer absence
    # merely from their being false.
    return (not stock.has_foreign_buying and not stock.has_inst_buying
            and all(state(side, stock.symbol) == 'observed_nonpositive' for side in ('1', '2')))


def observed_buying(stock):
    return (stock.has_foreign_buying or stock.has_inst_buying
            or any(state(side, stock.symbol) == 'positive' for side in ('1', '2')))


def decision_snapshot(stock):
    """One immutable-by-ownership view used by scoring and copied by observers.

    Age is ingestion age, never a provider as-of time. Both sides use the same
    monotonic instant; an observer never refreshes or reevaluates this snapshot.
    """
    from datetime import datetime, timezone
    now = time.monotonic()
    active = _ACTIVE.get() or {}
    snapshot = {'captured_at': datetime.now(timezone.utc).isoformat(),
                'legacy_foreign': stock.has_foreign_buying,
                'legacy_institution': stock.has_inst_buying}
    for investor, name in (('1', 'foreign'), ('2', 'institution')):
        proof = active.get(investor, {}).get(stock.symbol)
        value = age = None
        origin = 'unavailable'
        if type(proof) is dict:
            raw, at = proof.get('value'), proof.get('observed_monotonic')
            if (type(raw) is int and abs(raw) <= 10**12 and type(at) in (int, float)
                    and math.isfinite(at) and 0 <= now-at <= 10**12):
                age, origin = now-at, 'policy_memory_proof'
                if age <= MAX_AGE_SECONDS:
                    value = raw
        snapshot[name] = dict(state='unobserved' if value is None else
                              'positive' if value > 0 else 'observed_nonpositive',
                              value=value, proof_age_seconds=age, origin=origin, source_as_of=None)
    flags = snapshot['legacy_foreign'] or snapshot['legacy_institution']
    snapshot['absence_observed'] = not flags and all(
        snapshot[side]['state'] == 'observed_nonpositive' for side in ('foreign', 'institution'))
    snapshot['buying_observed'] = bool(flags or any(
        snapshot[side]['state'] == 'positive' for side in ('foreign', 'institution')))
    return snapshot
