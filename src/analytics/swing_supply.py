"""Bounded Swing supply evidence. Capture time never claims provider as-of time."""
from datetime import date, datetime, timedelta, timezone
import re

from src.analytics.selection_supply_policy import validated_quantity, _fresh_value
from src.utils.session import KST, is_kr_market_holiday

LIVE_SOURCE = 'FHPTJ04400000'
DAILY_SOURCE = 'FHKST01010900'
CACHE_VERSION = 'swing-supply-v1'
MAX_ROWS = 512


def utc_now():
    return datetime.now(timezone.utc)


def previous_session(today):
    day = today - timedelta(days=1)
    while day.weekday() >= 5 or is_kr_market_holiday(day):
        day -= timedelta(days=1)
    return day


def daily_evidence(raw, source_date, captured_at):
    value = validated_quantity(raw)
    if not isinstance(source_date, str) or re.fullmatch(r'[0-9]{8}', source_date) is None:
        return None
    try:
        day = datetime.strptime(source_date, '%Y%m%d').date()
        captured = datetime.fromisoformat(captured_at)
        if captured.tzinfo is None or day > captured.astimezone(KST).date():
            return None
    except (TypeError, ValueError):
        return None
    if value is None:
        return None
    return {'value': value, 'source': DAILY_SOURCE, 'source_date': day.isoformat(),
            'captured_at': captured_at, 'status': 'observed'}


def valid_evidence(proof, now=None):
    """Return validated proof or None, including cache boundary checks."""
    now = now if now is not None else utc_now()
    if not isinstance(proof, dict) or type(proof.get('value')) is not int:
        return None
    if validated_quantity(proof['value']) is None:
        return None
    try:
        captured = datetime.fromisoformat(proof['captured_at'])
        if captured.tzinfo is None or captured > now:
            return None
        today = now.astimezone(KST).date()
        if proof.get('source') == LIVE_SOURCE:
            if (proof.get('source_date') is not None or proof.get('status') != 'provisional'
                    or proof.get('market') not in ('0001', '0002')
                    or not 0 <= (now - captured).total_seconds() <= 600):
                return None
        elif proof.get('source') == DAILY_SOURCE:
            day = date.fromisoformat(proof['source_date'])
            if proof['source_date'] != day.isoformat():
                return None
            # Bound validation work while allowing a prior session across a long
            # KR holiday closure. Calendar age alone is not trading-session age.
            calendar_age = (today - day).days
            if not 0 <= calendar_age <= 31:
                return None
            session_age = sum(1 for offset in range(1, calendar_age + 1)
                              if (d := day + timedelta(days=offset)).weekday() < 5
                              and not is_kr_market_holiday(d))
            if (proof.get('status') != 'observed' or session_age > 7
                    or day > captured.astimezone(KST).date()
                    or day.weekday() >= 5 or is_kr_market_holiday(day)):
                return None
        else:
            return None
    except (KeyError, TypeError, ValueError, OverflowError):
        return None
    fields = ('value', 'source', 'source_date', 'captured_at', 'status')
    clean = {key: proof[key] for key in fields}
    if proof['source'] == LIVE_SOURCE:
        clean['market'] = proof['market']
    return clean


def ranking_evidence(results, now=None):
    """Merge ranked market snapshots without summing overlaps or clearing conflicts."""
    now = now if now is not None else utc_now()
    out = {}
    for market, rows in results:
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            symbol = row.get('symbol')
            if not isinstance(symbol, str) or re.fullmatch(r'[0-9]{6}', symbol) is None or symbol == '000000':
                continue
            if symbol not in out and len(out) >= MAX_ROWS:
                return {}
            value = _fresh_value(row.get('_supply_proof'))
            capture = row.get('_supply_capture') or {}
            proof = None
            if value is not None and isinstance(capture, dict) and capture.get('market') == market:
                proof = valid_evidence({'value': value, 'source': LIVE_SOURCE, 'source_date': None,
                    'captured_at': capture.get('captured_at'), 'status': 'provisional', 'market': market}, now)
            if symbol not in out:
                out[symbol] = proof
            elif out[symbol] is None or proof is None or any(
                    out[symbol].get(k) != proof.get(k) for k in ('value', 'market')):
                out[symbol] = None
            # Equal duplicates keep the first proof and never refresh its timestamp.
    return out


def sides(indicators, now=None):
    proofs = indicators.get('supply_evidence')
    proofs = proofs if isinstance(proofs, dict) else {}
    return tuple(valid_evidence(proofs.get(side), now) for side in ('foreign', 'institution'))


def pair_basis(proofs):
    foreign, inst = proofs
    if foreign is None or inst is None:
        return None
    if foreign['source'] != inst['source'] or foreign['source_date'] != inst['source_date']:
        return None
    if foreign['source'] == LIVE_SOURCE:
        if foreign['market'] != inst['market']:
            return None
        days = [datetime.fromisoformat(p['captured_at']).astimezone(KST).date() for p in proofs]
        if days[0] != days[1]:
            return None
        return (LIVE_SOURCE, days[0].isoformat(), 'capture_date_only')
    return (DAILY_SOURCE, foreign['source_date'], 'provider_date')


def supply_age(indicators, now=None):
    now = now if now is not None else utc_now()
    proofs = sides(indicators, now)
    basis = pair_basis(proofs)
    if basis is None or basis[0] != DAILY_SOURCE:
        return None
    start, today = date.fromisoformat(basis[1]), now.astimezone(KST).date()
    return sum(1 for offset in range(1, (today - start).days + 1)
               if (day := start + timedelta(days=offset)).weekday() < 5 and not is_kr_market_holiday(day))


def supply_values(indicators):
    return tuple(p['value'] if p is not None else None for p in sides(indicators))


def apply_lci(candidates):
    """Compute only within comparable, complete pairs; unknown rows never enter stats."""
    groups = {}
    now = utc_now()
    for candidate in candidates:
        ind = candidate.indicators
        ind.update(lci=None, market_sell_pressure=None, supply_accel=None)
        proofs = sides(ind, now)
        basis = pair_basis(proofs)
        if basis is not None:
            groups.setdefault(basis, []).append((ind, proofs[0]['value'], proofs[1]['value']))
    for rows in groups.values():
        if len(rows) < 2:
            continue
        values = [[row[side] for row in rows] for side in (1, 2)]
        zs = []
        for series in values:
            mean = sum(series) / len(series)
            std = (sum((v - mean) ** 2 for v in series) / len(series)) ** 0.5
            zs.append([(v - mean) / std for v in series] if std > 1e-10 else [0.0] * len(series))
        pressure = sum(v < 0 for v in values[0]) / len(rows) > 0.60
        no_variation = all(z == 0 for series in zs for z in series)
        for i, (ind, foreign, inst) in enumerate(rows):
            ind['market_sell_pressure'] = pressure
            if no_variation:
                continue
            lci = 0.5 * zs[0][i] + 0.5 * zs[1][i]
            if foreign > 0 and inst > 0:
                accel = max(0, min(0.3 + (zs[0][i] + zs[1][i]) * 0.1, 0.5))
                lci += accel
                ind['supply_accel'] = round(accel, 3)
            ind['lci'] = round(lci * (0.5 if pressure else 1), 3)
