"""Small original synthetic journals for the H1 multi-day integration path."""
from copy import deepcopy
from datetime import date, datetime, timedelta
import hashlib
import json

from src.analytics.entry_evaluation_bundle import _digest
from src.analytics.entry_window_evaluation import _effective_policy, _aware
from test_entry_evaluation_bundle import fixture
from test_entry_observation_runtime import load
from test_order_ready_window_observation import ready_plan


def dates():
    current = date(2026, 10, 12)
    result = []
    while len(result) < 20:
        if current.weekday() < 5:
            result.append(current.isoformat())
        current += timedelta(days=1)
    return result


def _shift(value, day):
    if isinstance(value, dict):
        return {k: _shift(v, day) for k, v in value.items()}
    if isinstance(value, list):
        return [_shift(v, day) for v in value]
    return value.replace('2026-10-02', day) if isinstance(value, str) else value


def source_complete(scan):
    from src.analytics.selection_source_status import ORDER, RUN_FIELDS, snapshot_fields
    at = scan['observed_at']
    runs = []
    for source in ORDER:
        row = dict.fromkeys(RUN_FIELDS)
        row.update(source_id=source, outcome='not_called', payload_count=0,
                   source_cache_reads=0, input_cache_reads=0, failure_count=0)
        if source == 'kis_volume_surge':
            count = len(scan['candidates'])
            row.update(outcome='completed_nonempty' if count else 'completed_empty',
                       started_at=at, completed_at=at, returned_count=count, raw_count=count,
                       payload_count=1, last_payload_at=at)
        runs.append(row)
    scan.update(selection_sources_expected=True, selection_sources_status='observed',
                **snapshot_fields(dict(version='selection-source-status-v1', capture_id=hashlib.sha256(scan['scan_id'].encode()).hexdigest()[:32],
                    started_at=at, completed_at=at, fallback_used=False, runs=runs)))


def rebind(day, context=None):
    c = context if context is not None else json.loads(day['study_utf8'])
    o = day['observations']
    for seq, record in enumerate(o['records'], 1):
        record['sequence'] = seq
        if record['kind'] == 'order_ready' and record.get('capital_snapshot') is not None:
            record['capital_snapshot_ref'] = hashlib.sha256(json.dumps(
                record['capital_snapshot'], sort_keys=True, allow_nan=False).encode()).hexdigest()
    raw = (json.dumps(c, ensure_ascii=False, sort_keys=True, separators=(',', ':'))+'\n').encode()
    day['study_utf8'] = raw.decode()
    sha = hashlib.sha256(raw).hexdigest()
    o['journal']['study_sha256'] = sha
    day['session_review'] = dict(version='entry-session-review-v1', dataset_kind=c['dataset_kind'],
        binding=dict(study_sha256=sha, observation_sha256=_digest(o),
                     capture_id=o['journal']['capture_id'], evaluation_epoch=c['evaluation_epoch']), quotes=[])
    for q in o['records']:
        if q['kind'] != 'ws_quote': continue
        day['session_review']['quotes'].append(dict(quote_id=q['quote_id'], symbol=q['symbol'],
            record_sha256=_digest(q), session='KRX_REGULAR_CONTINUOUS',
            basis='external_event_history_review', source_ref='synthetic-events', source_sha256='a'*64,
            reviewer_ref='synthetic-reviewer', reviewed_at=day['date']+'T10:00:00+09:00',
            valid_from=day['date']+'T09:00:00+09:00', valid_until=day['date']+'T10:01:00+09:00'))
    return day


def template(tmp_path):
    from src.analytics import entry_gate_trace as gates
    c, o, _ = fixture()
    _, declared = load(tmp_path, ready_plan)
    c['capture'] = declared['capture']
    c['capture'].update(start_at='2026-10-02T09:12:00+09:00',
        admission_end_at='2026-10-02T09:20:00+09:00', end_at=c['as_of'],
        source_version_ref=c['capital_policy']['source_version_ref'],
        configuration_ref=c['capital_policy']['configuration_ref'])
    c['capture']['channels']['lease_seconds'] = 3600
    c['capture']['entry_gate_trace'].update(source_version_ref=c['capture']['source_version_ref'],
                                           configuration_ref=c['capture']['configuration_ref'])
    for name in ('capital_policy', 'markout_policy'):
        c[name]['fixed_at'] = '2026-10-01T08:00:00+09:00'
    scan = o['records'][0]
    scan.update(population_scope='window_returned_scan_candidates',
        scan_admission_ref=c['capture']['scan_admission_ref'], selection_basis_expected=True,
        selection_basis_max_candidates=5, selection_basis_max_terms=16,
        **gates.scan_fields(c['capture']['entry_gate_trace']))
    source_complete(scan)
    records = sorted(o['records'], key=lambda r: datetime.fromisoformat(r['observed_at']))
    o['records'] = [dict(kind='quote_subscription', status='connected', generation=1,
        connection_id='synthetic-connection', observed_at='2026-10-02T09:13:00+09:00')]
    signals = {}
    for r in records:
        o['records'].append(r)
        if r['kind'] == 'signal': signals[r['signal_id']] = r
        if r['kind'] == 'order_ready':
            o['records'].append(dict(kind='order_ready_admission', status='admitted',
                reason='order_ready_admitted', observed_at=r['observed_at'],
                signal_id=r['signal_id'], order_id=r['order_id'], symbol=r['symbol'],
                candidate_id=signals[r['signal_id']]['candidate_id'], tr_id='H0STASP0', expires_at=c['as_of']))
            for status in ('requested', 'acknowledged'):
                o['records'].append(dict(kind='quote_subscription', status=status, generation=1,
                    connection_id='synthetic-connection', observed_at=r['observed_at'],
                    symbol=r['symbol'], tr_id='H0STASP0'))
        if r['kind'] == 'ws_quote': r['provenance'].update(generation=1, connection_id='synthetic-connection')
    o.update(complete=True, dropped_records=0, incomplete_reasons=[])
    o['frame_diagnostics'] = c['capture']['frame_diagnostics']
    o['journal'].update(format='entry-observation-journal-v2', frame_diagnostics=o['frame_diagnostics'],
                        sealed=True, structurally_valid=True, dropped_count_exact=True,
                        scope='sealed_window', persistence_dropped_records=0,
                        created_at='2026-10-02T09:11:59+09:00',
                        capture_closed_at='2026-10-02T09:45:00+09:00', durability_confirmed=True)
    return c, o


def make_day(base, day_text):
    c, o = _shift(deepcopy(base[0]),day_text), _shift(deepcopy(base[1]),day_text)
    o['journal']['capture_id'] = 'synthetic-capture-'+day_text
    # Per-date IDs must not alias another original capture.
    for record in o['records']:
        for key in ('quote_id',):
            if key in record: record[key] += '-'+day_text
    return rebind(dict(date=day_text, observations=o), c)


def protocol(day):
    c = json.loads(day['study_utf8'])
    fixed = '2026-10-11T12:00:00+09:00'
    identity = _effective_policy(c, _aware(fixed,'fixed'), day['date'], _aware(c['as_of'],'as_of'))
    return dict(version='h1-multiday-protocol-v1', protocol_ref='synthetic-only-h1',
        dataset_kind='synthetic', fixed_at=fixed, evaluation_epoch=c['evaluation_epoch'],
        calendar=dict(source_ref='synthetic-weekday-list-not-market-proof', source_sha256='c'*64,
                      scheduled_dates=dates()), study_policy_sha256=hashlib.sha256(identity.encode()).hexdigest(),
        capital_model='bounded-shadow-sleeve-v1', seed_rule='first_admitted_nonnegative_capacity',
        pending_reservation_multiplier='1.015',min_net_rr='1.5',slippage_bps_each=[0,10,30])


def payload(tmp_path, *, all_days=True):
    base = template(tmp_path)
    items = [make_day(base, d) for d in dates()[:20 if all_days else 1]]
    return dict(protocol=protocol(items[0]), days=items, as_of=dates()[-1]+'T16:00:00+09:00')
