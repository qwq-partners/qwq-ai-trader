"""H1 twenty-date capital-ceiling proxy from original offline observation inputs.

No account reconstruction, market access, calendar discovery, or trading promotion.
The live one-scan protocol and the existing quote/fee evaluators remain unchanged.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, time
from decimal import Decimal, ROUND_HALF_UP, localcontext
import hashlib
from typing import Mapping

from .entry_capital_limits import CapitalPolicy, limits_from_record
from .entry_evaluation_bundle import build_evaluation_bundle, load_json_bytes
from .entry_price_shadow import MIN_NET_RR, _fees, _integer, _number, _timestamp
from .entry_window_evaluation import (
    KST, _aware, _canonical, _day_input, _effective_policy, _iso_date, _sha,
    _text, _verified_empty_scan,
)

VERSION = 'h1-multiday-protocol-v1'
SCENARIOS = (0, 10, 30)


def _protocol(value):
    fields = {'version', 'protocol_ref', 'dataset_kind', 'fixed_at', 'evaluation_epoch',
              'calendar', 'study_policy_sha256', 'capital_model', 'seed_rule',
              'pending_reservation_multiplier','min_net_rr','slippage_bps_each'}
    if not isinstance(value, Mapping) or set(value) != fields or value['version'] != VERSION:
        raise ValueError('strict H1 protocol fields/version required')
    if value['dataset_kind'] not in {'synthetic', 'observed'}:
        raise ValueError('H1 dataset kind')
    for key in ('protocol_ref', 'evaluation_epoch'):
        _text(value[key], key)
    _sha(value['study_policy_sha256'], 'study_policy_sha256')
    if (value['capital_model'] != 'bounded-shadow-sleeve-v1'
            or value['seed_rule'] != 'first_admitted_nonnegative_capacity'
            or value['pending_reservation_multiplier'] != '1.015'):
        raise ValueError('H1 capital model/seed/reservation contract')
    if (value['min_net_rr'] != '1.5' or MIN_NET_RR != Decimal(value['min_net_rr'])
            or value['slippage_bps_each'] != [0,10,30]
            or any(type(n) is not int for n in value['slippage_bps_each'])):
        raise ValueError('H1 preregistered RR/slippage contract')
    calendar = value['calendar']
    if not isinstance(calendar, Mapping) or set(calendar) != {'source_ref','source_sha256','scheduled_dates'}:
        raise ValueError('strict calendar fields required')
    _text(calendar['source_ref'], 'calendar.source_ref')
    _sha(calendar['source_sha256'], 'calendar.source_sha256')
    dates = calendar['scheduled_dates']
    if not isinstance(dates, list) or len(dates) != 20:
        raise ValueError('exactly twenty scheduled dates required')
    parsed = [_iso_date(d, 'scheduled date') for d in dates]
    if dates != sorted(dates) or len(set(dates)) != 20 or any(d.weekday() >= 5 for d in parsed):
        raise ValueError('sorted unique weekday dates required')
    fixed = _aware(value['fixed_at'], 'fixed_at')
    if fixed >= datetime.combine(parsed[0], time.min, tzinfo=KST):
        raise ValueError('protocol must be fixed before first date')
    return list(dates), fixed


def _capture_quality(observations, bundle, day, report_at, context):
    journal = observations['journal']
    closed = journal.get('capture_closed_at')
    opened = journal.get('created_at')
    close_ok = closed is not None and _aware(closed,'capture_closed_at').date().isoformat() == day \
        and _aware(context['capture']['end_at'],'capture.end_at') <= _aware(closed,'capture_closed_at') <= report_at
    open_ok = opened is not None and _aware(opened,'created_at').date().isoformat() == day \
        and _aware(opened,'created_at') <= _aware(context['capture']['start_at'],'capture.start_at')
    return (journal.get('sealed') is True and journal.get('structurally_valid') is True
            and journal.get('dropped_count_exact') is True
            and journal.get('scope') == 'sealed_window' and journal.get('persistence_dropped_records') == 0
            and observations.get('complete') is True and observations.get('dropped_records') == 0
            and observations.get('incomplete_reasons') == [] and close_ok and open_ok
            and bundle['original']['capture_complete'] is True)


def _selected(records):
    return [r for r in records if r['kind'] == 'order_ready_admission'
            and r['reason'] == 'order_ready_admitted']


def _original_links(admission, records, prepared):
    cid = admission['candidate_id']
    signals = [r for r in records if r['kind'] == 'signal' and r.get('candidate_id') == cid]
    orders = [r for r in records if r['kind'] == 'order_ready' and r['signal_id'] == admission['signal_id']]
    if (len(signals) != 1 or len(orders) != 1 or signals[0]['signal_id'] != admission['signal_id']
            or orders[0]['order_id'] != admission['order_id']):
        return None, None
    inputs = [r for r in prepared['payload']['opportunities'] if r['opportunity_id'] == cid]
    return orders[0], inputs[0] if len(inputs) == 1 else None


def _limits(record, context):
    if record is None: return None
    try:
        return limits_from_record(record, CapitalPolicy.from_dict(context['capital_policy']))
    except (ValueError, TypeError, KeyError):
        return None


def _opportunity(day, admission, records, prepared, tasks, context):
    from .h1_capital_replay import ReplayOpportunity
    order, raw = _original_links(admission, records, prepared)
    cid = admission['candidate_id']
    rows = [r for r in prepared['report']['opportunities'] if r['opportunity_id'] == cid]
    row = rows[0] if len(rows) == 1 else {}
    limits = _limits(raw, context)
    ready_at = _timestamp(order['observed_at'] if order else admission['observed_at'], 'ready_at')
    order_seq = order['sequence'] if order else admission['sequence']
    task = tasks[cid]
    entry_record = (task.get('entry_quote') or {}).get('record')
    entry_at, entry_seq = ready_at, order_seq + 1
    if entry_record is not None:
        entry_at = _timestamp(entry_record['observed_at'], 'entry_at')
        entry_seq = entry_record['sequence']
    gate = row.get('gate_status', 'unknown')
    gate = gate if gate in {'allow','cash'} and limits is not None and raw is not None else 'unknown'
    capital_block_reason = None
    reasons = row.get('reason_codes', [])
    failures = {'cash_cost', 'market', 'cash_gate', 'strategy', 'position', 'risk'}
    if (gate == 'unknown' and limits is not None and raw is not None and entry_record is not None
            and len(reasons) == 1
            and reasons[0] in {'CAPITAL_CONSTRAINT_FAILED:' + name for name in failures}):
        gate = 'capital_blocked'
        capital_block_reason = reasons[0].split(':', 1)[1]
    qty = reference = reservation = None
    if raw is not None and limits is not None:
        qty = _integer(raw['quantity'], 'quantity')
        reference = _number(raw['capital_evidence']['order_reference_price'], 'reference') * qty
        reservation = reference * Decimal('1.015')
    cost = entry_price = buy_fee = proceeds = sell_fee = exit_at = exit_seq = None
    if gate in {'allow', 'cash'}:
        fees = _fees(prepared['report']['fees'])
        entry_price = _number(row['entry_price_proxy'], 'entry_price')
        buy_fee = fees.calculate_buy_fee(entry_price * qty)
        cost = entry_price * qty + buy_fee
        if row.get('a_net_pnl') is not None:
            mark = (task.get('markout_quote') or {}).get('record')
            if mark is None or row.get('markout_quote_id') != mark['quote_id']:
                raise ValueError('evaluated markout identity missing')
            exit_at = _timestamp(row['markout_observed_at'], 'exit_at')
            exit_seq = mark['sequence']
            amount = _number(row['markout_price_proxy'], 'markout_price') * qty
            sell_fee = fees.calculate_sell_fee(amount)
            proceeds = amount - sell_fee
            if (proceeds - cost).quantize(Decimal('1'),rounding=ROUND_HALF_UP) != Decimal(row['a_net_pnl']):
                raise ValueError('entry/exit fee accounting mismatch')
    return ReplayOpportunity(
        key=_canonical([day, admission['signal_id'], admission['order_id']]), date=day,
        symbol=admission['symbol'], ready_at=ready_at, entry_at=entry_at,
        entry_sequence=entry_seq, order_sequence=order_seq, gate_status=gate,
        quantity=qty, reference_notional=reference, reservation=reservation,
        entry_price=entry_price, entry_cost=cost, buy_fee=buy_fee, limits=limits,
        exit_at=exit_at, exit_sequence=exit_seq, exit_proceeds=proceeds, sell_fee=sell_fee,
        capital_block_reason=capital_block_reason,
        unknown_reason=';'.join(row.get('reason_codes',[])) or row.get('outcome_reason')
            or ('AMBIGUOUS_OR_MISSING_SELECTED_EVIDENCE' if gate == 'unknown' else None))


def build_h1_multiday_report(protocol, days, *, analysis_as_of):
    """Validate original inputs, then replay fixed first-five H1 decisions offline."""
    with localcontext() as context:
        context.prec = 34
        return _build(protocol, days, analysis_as_of=analysis_as_of)


def _build(protocol, days, *, analysis_as_of):
    scheduled, fixed = _protocol(protocol)
    report_at = _aware(analysis_as_of, 'analysis_as_of')
    if report_at < fixed: raise ValueError('analysis before preregistration')
    if isinstance(days,(str,bytes,Mapping)):
        raise ValueError('days must be a chronological iterable')
    try:
        iterator = iter(days)
    except TypeError as exc:
        raise ValueError('days must be a chronological iterable') from exc
    previous = None
    exhausted = object()
    def next_day():
        nonlocal previous
        item = next(iterator, exhausted)
        if item is exhausted:
            return None, None
        date_text, *_ = _day_input(item)
        if date_text not in scheduled or (previous is not None and date_text <= previous):
            raise ValueError('out of order, duplicate or unplanned day')
        previous = date_text
        return date_text, item
    supplied_date, supplied_day = next_day()
    output_days, failures, seeds = [], set(), []
    scenarios = {bps:[] for bps in SCENARIOS}
    counts = Counter()
    seen = {name:set() for name in ('capture','study','observations')}
    for day in scheduled:
        if day != supplied_date:
            failures.add(day)
            output_days.append(dict(date=day,status='planned_missing',scans=None,selected_admissions=None))
            continue
        _, raw, observations, review = _day_input(supplied_day)
        c = load_json_bytes(raw)
        if (not isinstance(c,Mapping) or c.get('dataset_kind') != protocol['dataset_kind']
                or c.get('evaluation_epoch') != protocol['evaluation_epoch']):
            raise ValueError('study dataset/epoch mismatch')
        cutoff = _aware(c.get('as_of'),'study.as_of')
        if cutoff.date().isoformat() != day or cutoff > report_at:
            raise ValueError('study date/cutoff mismatch')
        if c.get('capture',{}).get('version') != 'runner-order-ready-window-v1':
            raise ValueError('order-ready capture version required')
        identity = _effective_policy(c,fixed,day,cutoff)
        if hashlib.sha256(identity.encode()).hexdigest() != protocol['study_policy_sha256']:
            raise ValueError('preregistered study policy fingerprint mismatch')
        if (c['markout_policy'].get('horizon_seconds') != 900
                or _number(c['markout_policy'].get('max_delay_seconds'), 'max_delay_seconds') != 5
                or _number(c['policy'].get('max_quote_age_seconds'), 'max_quote_age_seconds') != 1
                or _number(c['policy'].get('max_decision_delay_seconds'), 'max_decision_delay_seconds') != 5
                or _number(c['received_policy'].get('max_rest_rtt_seconds'), 'max_rest_rtt_seconds') != 3
                or _number(c['received_policy'].get('max_target_age_seconds'), 'max_target_age_seconds') != 30):
            raise ValueError('H1 horizon/freshness contract')
        records = observations.get('records')
        if not isinstance(records,list): raise ValueError('original records required')
        for r in records:
            when = _aware(r.get('observed_at'),'record.observed_at')
            if not fixed < when <= report_at or when.date().isoformat() != day:
                raise ValueError('record date/time mismatch')
        journal = observations.get('journal')
        if not isinstance(journal,Mapping): raise ValueError('journal required')
        study_hash = hashlib.sha256(raw).hexdigest()
        for name, value in [('capture',_text(journal.get('capture_id'),'capture_id')),
                            ('study',study_hash),('observations',hashlib.sha256(_canonical(observations).encode()).hexdigest())]:
            if value in seen[name]: raise ValueError('reused '+name)
            seen[name].add(value)
        bundle = build_evaluation_bundle(raw,observations,study_sha256=study_hash,
                    session_review=review,analysis_as_of=report_at.isoformat())
        scans = [r for r in records if r['kind']=='scan']
        selected = _selected(records)
        all_admissions = Counter(r['reason'] for r in records if r['kind']=='order_ready_admission')
        quality = _capture_quality(observations,bundle,day,report_at,c)
        if not quality: status='incomplete_capture'
        elif not scans: status='no_scan'
        elif not all(_verified_empty_scan(s) for s in scans):
            status='source_incomplete' if selected else 'no_order_ready_unverified'
        elif not selected:
            status='verified_no_order_ready'
        else: status='complete_selected_window'
        if status not in {'verified_no_order_ready','complete_selected_window'}: failures.add(day)
        total = sum(len(s['candidates']) for s in scans)
        counts.update(all_candidates=total,scans=len(scans),selected_admissions=len(selected))
        output_days.append(dict(date=day,status=status,scans=len(scans),all_candidates=total,
            selected_admissions=len(selected),admission_reasons=dict(all_admissions),
            capture_id=journal['capture_id'],study_sha256=study_hash,
            incomplete_source_scans=sum(not _verified_empty_scan(s) for s in scans)))
        tasks = {t['opportunity_id']:t for t in bundle['review_tasks']}
        if selected:
            _, seed_raw = _original_links(selected[0],records,bundle['original'])
            seeds.append((day,selected[0],_limits(seed_raw,c)))
        if [v['slippage_bps_each'] for v in bundle['sensitivity']] != protocol['slippage_bps_each']:
            raise ValueError('derived sensitivity contract mismatch')
        for variant in bundle['sensitivity']:
            bps = variant['slippage_bps_each']
            if Decimal(variant['result']['report']['policy']['min_net_rr']) != Decimal(protocol['min_net_rr']):
                raise ValueError('derived RR contract mismatch')
            for admission in selected:
                scenarios[bps].append(_opportunity(day,admission,records,variant['result'],tasks,c))
        supplied_day = None
        del bundle, tasks, records, observations, review, raw
        supplied_date, supplied_day = next_day()
    if supplied_date is not None:
        raise ValueError('unconsumed day input')
    seed = dict(status='no_selected_orders',cash=None,strategy_capacity=None,snapshot_ref=None)
    initial_cash = initial_strategy = None
    if seeds:
        seed_day, admission, limits = seeds[0]
        seed.update(date=seed_day,signal_id=admission['signal_id'],order_id=admission['order_id'],status='unavailable')
        if limits is not None:
            initial_cash = max(Decimal(0), limits.cash)
            initial_strategy = None if limits.strategy is None else max(Decimal(0), limits.strategy)
            seed.update(cash_capacity_before=str(limits.cash), strategy_remaining_notional=(
                None if limits.strategy is None else str(limits.strategy)))
            seed.update(status='validated_internal_snapshot',cash=str(initial_cash),
                strategy_capacity=str(initial_strategy) if initial_strategy is not None else None,snapshot_ref=limits.snapshot_ref)
    final_end = datetime.combine(_iso_date(scheduled[-1],'last_date'),time(15,30),tzinfo=KST)
    quality_only = report_at < final_end
    results = {}
    for bps, opportunities in scenarios.items():
        if quality_only:
            results[str(bps)] = dict(status='quality_only',paired_decisions=None,
                delta_net_pnl=None,arms=None,daily=None,minimum_proxy_conditions_met=False)
            continue
        from .h1_capital_replay import replay_sleeve
        replay = replay_sleeve(opportunities,scheduled_dates=scheduled,unknown_dates=failures,
            initial_cash=initial_cash,initial_strategy_capacity=initial_strategy,
            pending_reservation_multiplier=Decimal(protocol['pending_reservation_multiplier']))
        b_net = replay['arms']['B']['net_pnl']; delta = replay['delta_net_pnl']
        replay['minimum_proxy_conditions_met'] = (not failures and seed['status'] != 'unavailable'
            and replay['paired_decisions'] >= 30 and b_net is not None and delta is not None
            and Decimal(b_net) > 0 and Decimal(delta) > 0)
        results[str(bps)] = replay
    unknown = not quality_only and any(s['delta_net_pnl'] is None for s in results.values())
    if quality_only or failures or seed['status']=='unavailable' or unknown: verdict='suppressed'
    elif protocol['dataset_kind']=='synthetic': verdict='synthetic_diagnostic_only'
    elif any(s['paired_decisions']<30 for s in results.values()): verdict='insufficient_data'
    elif all(s['minimum_proxy_conditions_met'] for s in results.values()): verdict='minimum_proxy_conditions_met'
    else: verdict='minimum_proxy_conditions_not_met'
    return dict(version='h1-multiday-report-v1',protocol=dict(protocol),analysis_as_of=report_at.isoformat(),
        evaluation_status='quality_only' if quality_only else 'final',days=output_days,
        counts=dict(counts),quality_failures=sorted(failures),seed=seed,scenarios=results,
        economic_verdict=verdict,production_eligible=False,profitability_pass=False,account_return=None,
        source_authenticity_verified=False,benchmarks=dict(kodex200=None,symmetric_semiconductor_excluded=None),
        limitations=[
            'Overlapping pending/cash/strategy/symbol exposure proxy; not account or actual-fill replay.',
            'Sealed-file integrity does not independently prove the original writer fsync or source authenticity.',
            'Cash uses exact Decimal proxy proceeds; per-opportunity diagnostic PnL is separately rounded to whole KRW.',
            'Original snapshot ceilings and shadow residuals are independent; live background holdings/flows are not reconstructed.',
            'First-snapshot residuals are preserved; nonnegative sleeve allocations max(0, residual) carry without top-ups.',
            'Weighted position slots, daily trade limits, sector/factor occupancy and starting symbol holdings are not replayed.',
            '900-second bid proceeds are a full-size proxy, not actual stop/target/partial exit or settlement evidence.',
            'Calendar, fees, sessions, source and regime assertions are supplied, not authenticated by hashes.',
            'Thirty pairs and positive B/delta are minimum proxy conditions, not statistical significance or profitability proof.',
            'Unallocated signals and other strategies are outside the first-five cohort; no whole-engine generalization.',
        ])
