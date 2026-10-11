"""H1 order-ready admission uses synthetic records only; no order/API execution."""
import asyncio
from copy import deepcopy
from datetime import timedelta

import pytest

from src.analytics import entry_observation as obs
from src.analytics.entry_observation_runtime import _WindowBuffer, validate_window_contract
from src.data.feeds.quote_subscription import QuoteSubscriptionCoordinator
from test_entry_observation_runtime import load
from test_signal_window_observation import window_plan, stock, event


def ready_plan(c):
    window_plan(c)
    c['capture'].update(version='runner-order-ready-window-v1', quote_admission='order_ready')
    c['capture']['selection_basis']['max_candidates'] = 5
    c['capture']['entry_gate_trace']['max_candidates'] = 5


def setup(tmp_path, monkeypatch):
    plan, context = load(tmp_path, ready_plan)
    clock = [plan.start_at]
    monkeypatch.setattr(obs, '_now', lambda: clock[0].isoformat())
    b = _WindowBuffer(plan, lambda: clock[0])
    q = QuoteSubscriptionCoordinator(b, **plan.settings['channels'])
    b.order_ready_quote_submit = q.submit_order_ready
    return plan, context, b, q, clock


def signal(b, scan, sid, strategy='gap_and_go'):
    e = event(sid)
    e.strategy.value = strategy
    obs.capture_signal(b, scan, e)


def ready(b, sid, *, quantity=1, order_id=None, strategy='gap_and_go'):
    return b.publish(dict(kind='order_ready', signal_id=sid, symbol='900001',
        order_id=order_id or 'order-' + sid, requested_quantity=quantity,
        strategy=strategy, observed_at=obs._now()))


def admissions(b):
    return [r for r in b.export()['records'] if str(r.get('reason', '')).startswith('order_ready_')]


@pytest.mark.asyncio
async def test_ready_first_five_identities_duplicates_and_rejected_signals(tmp_path, monkeypatch):
    plan, context, b, q, clock = setup(tmp_path, monkeypatch)
    scan = obs.capture_scan(b, [stock()], 'regular')
    signal(b, scan, 'rejected')
    await asyncio.sleep(0)
    assert q.snapshot()['leases'] == 0
    for i in range(7):
        signal(b, scan, f's{i}')
        assert ready(b, f's{i}')
        assert ready(b, f's{i}')
    await asyncio.gather(*q._tasks)
    assert set(q._leases) == {f's{i}' for i in range(5)}
    assert all(lease.expires_at == plan.end_at.isoformat() for lease in q._leases.values())
    rows = admissions(b)
    assert sum(r['reason'] == 'order_ready_admitted' for r in rows) == 5
    assert sum(r['reason'] == 'order_ready_duplicate' for r in rows) == 7
    assert sum(r['reason'] == 'order_ready_limit' for r in rows) == 2
    assert all(r['candidate_id'] == f'{scan}:900001' for r in rows)
    validate_window_contract(context, b.export())


@pytest.mark.asyncio
async def test_ready_scope_quantity_and_deadline_do_not_consume_leases(tmp_path, monkeypatch):
    plan, _, b, q, clock = setup(tmp_path, monkeypatch)
    scan = obs.capture_scan(b, [stock()], 'regular')
    for sid, strategy, quantity in [('zero', 'gap_and_go', 0), ('bool', 'gap_and_go', True),
                                    ('other', 'rsi2', 1)]:
        signal(b, scan, sid, strategy)
        ready(b, sid, quantity=quantity, strategy=strategy)
    clock[0] = b.admission_end
    signal(b, scan, 'late'); ready(b, 'late')
    await asyncio.sleep(0)
    assert q.snapshot()['leases'] == 0
    assert [r['reason'] for r in admissions(b)] == ['order_ready_invalid_quantity',
        'order_ready_invalid_quantity', 'order_ready_outside_scope', 'order_ready_admission_closed']
    clock[0] = plan.end_at
    assert not ready(b, 'after-end')


@pytest.mark.asyncio
async def test_ready_enrollment_never_waits_for_lock_and_cannot_restore_ended_leases(tmp_path, monkeypatch):
    plan, _, b, q, clock = setup(tmp_path, monkeypatch)
    await q._lock.acquire()
    scan = obs.capture_scan(b, [stock()], 'regular')
    signal(b, scan, 'queued'); assert ready(b, 'queued')
    tasks = list(q._tasks)
    assert len(tasks) == 1 and q.snapshot()['leases'] == 0
    clock[0] = plan.end_at
    q._lock.release()
    await asyncio.gather(*tasks)
    assert q.snapshot()['leases'] == 0


@pytest.mark.parametrize('field,value', [('quote_admission','signal_created'),
    ('max_candidates', 4), ('operational_headroom',9), ('registration_cap',40)])
def test_ready_contract_binds_h1_budget(tmp_path, field, value):
    def change(c):
        ready_plan(c)
        (c['capture'] if field == 'quote_admission' else c['capture']['channels'])[field] = value
    with pytest.raises(ValueError): load(tmp_path, change)


def test_ready_submit_failure_marks_incomplete_and_keeps_order(tmp_path, monkeypatch):
    _, _, b, _, _ = setup(tmp_path, monkeypatch)
    def broken(*args): raise RuntimeError('synthetic')
    b.order_ready_quote_submit = broken
    scan = obs.capture_scan(b, [stock()], 'regular')
    signal(b, scan, 'kept'); assert ready(b, 'kept')
    assert 'order_ready_enrollment_failed' in b.capture_status()['incomplete_reasons']
    assert any(r['kind'] == 'order_ready' for r in b.export()['records'])


@pytest.mark.asyncio
@pytest.mark.parametrize('defect', ['missing_admission', 'foreign_signal', 'foreign_order',
    'foreign_candidate', 'before_ready', 'wrong_reason', 'late_lease', 'unadmitted_lease'])
async def test_ready_offline_rejects_forged_or_missing_identity(tmp_path, monkeypatch, defect):
    plan, context, b, q, _ = setup(tmp_path, monkeypatch)
    scan = obs.capture_scan(b, [stock()], 'regular')
    signal(b, scan, 's'); ready(b, 's')
    await asyncio.gather(*q._tasks)
    data = b.export()
    a = next(r for r in data['records'] if r.get('reason') == 'order_ready_admitted')
    lease = next(r for r in data['records'] if r.get('status') == 'desired')
    if defect == 'missing_admission': data['records'].remove(a)
    elif defect == 'foreign_signal': a['signal_id'] = 'foreign'
    elif defect == 'foreign_order': a['order_id'] = 'foreign'
    elif defect == 'foreign_candidate': a['candidate_id'] = 'foreign'
    elif defect == 'before_ready': a['observed_at'] = (plan.start_at-timedelta(seconds=1)).isoformat()
    elif defect == 'wrong_reason': a['reason'] = 'order_ready_limit'
    elif defect == 'late_lease': lease['expires_at'] = (plan.end_at+timedelta(seconds=1)).isoformat()
    elif defect == 'unadmitted_lease': lease['signal_id'] = 'foreign'
    with pytest.raises(ValueError): validate_window_contract(context, data)


@pytest.mark.asyncio
async def test_duplicate_signal_and_order_identity_never_add_lease(tmp_path, monkeypatch):
    _, context, b, q, clock = setup(tmp_path, monkeypatch)
    scan = obs.capture_scan(b, [stock()], 'regular')
    signal(b, scan, 'first'); ready(b, 'first', order_id='shared-order')
    clock[0] += timedelta(seconds=1)
    signal(b, scan, 'first'); ready(b, 'first', order_id='different-order')
    signal(b, scan, 'alias'); ready(b, 'alias', order_id='shared-order')
    await asyncio.gather(*q._tasks)
    assert set(q._leases) == {'first'}
    validate_window_contract(context, b.export())


@pytest.mark.asyncio
async def test_ready_channel_capacity_failure_is_incomplete(tmp_path, monkeypatch):
    _, context, b, q, _ = setup(tmp_path, monkeypatch)
    q._operational = [('H0STCNT0', str(900100+i)) for i in range(31)]
    scan = obs.capture_scan(b, [stock()], 'regular')
    signal(b, scan, 'capacity'); ready(b, 'capacity')
    await asyncio.gather(*q._tasks)
    assert not b.capture_status()['complete']
    assert 'order_ready_channel_capacity' in b.capture_status()['incomplete_reasons']
    validate_window_contract(context, b.export())


@pytest.mark.asyncio
async def test_ready_real_hook_buy_scope_and_sealed_strict_binding(tmp_path, monkeypatch):
    from types import SimpleNamespace
    import src.analytics.entry_observation_journal as journal
    from src.analytics.entry_observation_runtime import ObservationRuntime
    from src.analytics.kis_frame_diagnostics import validate_study_binding
    from test_entry_observation_runtime import NOW, owner, read
    plan, context = load(tmp_path, ready_plan)
    clock = [NOW]
    monkeypatch.setattr(obs, '_now', lambda: clock[0].isoformat())
    monkeypatch.setattr(journal, '_timestamp', lambda: clock[0].isoformat())
    bot = owner()
    bot._strategy_exit_params = {}
    stop = SimpleNamespace(stop_pct=3, source='synthetic', crash_capped=False)
    bot.exit_manager = SimpleNamespace(resolve_stop=lambda **kwargs: stop)
    runtime = await ObservationRuntime.install(bot, plan, now=lambda: clock[0])
    b = runtime.buffer; q = bot.ws_feed._quote_subscription_owner
    try:
        clock[0] = plan.start_at
        scan = obs.capture_scan(b, [stock()], 'regular')
        e = event('hook'); e.source = 'live_screening'
        obs.capture_signal(b, scan, e)
        order = SimpleNamespace(id='hook-order', symbol='900001', strategy='gap_and_go',
            quantity=2, price=10000, side=SimpleNamespace(value='sell'),
            order_type=SimpleNamespace(value='market'))
        obs.capture_order_ready(bot, e, order)
        assert not q._tasks
        order.side.value = 'buy'; e.source = 'other'
        obs.capture_order_ready(bot, e, order)
        assert not q._tasks
        e.source = 'live_screening'
        await q._lock.acquire()
        obs.capture_order_ready(bot, e, order)
        assert len(q._tasks) == 1 and not q._leases
        q._lock.release()
        await asyncio.gather(*q._tasks)
        assert set(q._leases) == {'hook'}
    finally:
        clock[0] = plan.end_at
        await runtime.close()
    data = read(plan)
    assert data['complete'] and runtime.result['sealed']
    validate_study_binding(data, context)
    wrong = deepcopy(context); wrong['capture']['version'] = 'runner-signal-window-v1'
    wrong['capture']['quote_admission'] = 'signal_created'
    with pytest.raises(ValueError): validate_study_binding(data, wrong)


@pytest.mark.asyncio
@pytest.mark.parametrize('operational,allocated', [(26,5), (27,4)])
async def test_ready_channel_headroom_respects_actual_operational_demand(tmp_path, monkeypatch,
                                                                       operational, allocated):
    _, context, b, q, _ = setup(tmp_path, monkeypatch)
    q._operational = [('H0STCNT0', str(900100+i)) for i in range(operational)]
    symbols = [str(900001+i) for i in range(5)]
    scan = obs.capture_scan(b, [stock(s) for s in symbols], 'regular')
    for i, symbol in enumerate(symbols):
        obs.capture_signal(b, scan, event(f's{i}', symbol))
        b.publish(dict(kind='order_ready', signal_id=f's{i}', symbol=symbol,
            order_id=f'o{i}', requested_quantity=1, strategy='gap_and_go', observed_at=obs._now()))
    await asyncio.gather(*q._tasks)
    assert len(q._leases) == 5
    assert len(q._desired()) == operational + allocated
    assert b.capture_status()['complete'] is (allocated == 5)
    assert sum(r.get('reason') == 'channel_capacity' for r in b.export()['records']) == 5-allocated
    validate_window_contract(context, b.export())


@pytest.mark.parametrize('kind', ['ws_quote', 'signal', 'order_ready', 'quote_subscription'])
def test_ready_strict_reader_rejects_capture_end_timestamp(tmp_path, kind):
    plan, context = load(tmp_path, ready_plan)
    data = {'records': [dict(kind=kind, observed_at=plan.end_at.isoformat())]}
    with pytest.raises(ValueError): validate_window_contract(context, data)


@pytest.mark.parametrize('kind', ['ws_quote', 'signal', 'order_ready'])
def test_ready_strict_reader_rejects_before_start(tmp_path, kind):
    plan, context = load(tmp_path, ready_plan)
    data = {'records': [dict(kind=kind, observed_at=(plan.start_at-timedelta(seconds=1)).isoformat())]}
    with pytest.raises(ValueError): validate_window_contract(context, data)


@pytest.mark.parametrize('field,value', [('registration_cap',41.0), ('operational_headroom',10.0),
    ('max_candidates',5.0), ('external_reserved',True), ('lease_seconds',float('inf'))])
def test_ready_offline_channel_contract_rejects_invalid_types(tmp_path, field, value):
    _, context = load(tmp_path, ready_plan)
    context['capture']['channels'][field] = value
    with pytest.raises(ValueError): validate_window_contract(context)


@pytest.mark.asyncio
async def test_ready_invalid_symbol_gap_seals_and_passes_strict_binding(tmp_path, monkeypatch):
    import src.analytics.entry_observation_journal as journal
    from src.analytics.entry_observation_runtime import ObservationRuntime
    from src.analytics.kis_frame_diagnostics import validate_study_binding
    from test_entry_observation_runtime import NOW, owner, read
    plan, context = load(tmp_path, ready_plan); clock = [NOW]
    monkeypatch.setattr(obs, '_now', lambda: clock[0].isoformat())
    monkeypatch.setattr(journal, '_timestamp', lambda: clock[0].isoformat())
    bot = owner(); runtime = await ObservationRuntime.install(bot, plan, now=lambda: clock[0])
    b = runtime.buffer; q = bot.ws_feed._quote_subscription_owner
    try:
        clock[0] = plan.start_at
        scan = obs.capture_scan(b, [stock('BADSYM')], 'regular')
        obs.capture_signal(b, scan, event('invalid', 'BADSYM'))
        b.publish(dict(kind='order_ready', signal_id='invalid', symbol='BADSYM',
            order_id='invalid-order', requested_quantity=1, strategy='gap_and_go', observed_at=obs._now()))
        await asyncio.gather(*q._tasks)
        assert not q._leases
    finally:
        clock[0] = plan.end_at
        await runtime.close()
    data = read(plan)
    assert runtime.result['sealed'] and not data['complete']
    assert 'order_ready_enrollment_failed' in data['incomplete_reasons']
    validate_study_binding(data, context)


@pytest.mark.asyncio
async def test_ready_pending_enrollment_marks_incomplete_before_close(tmp_path, monkeypatch):
    import src.analytics.entry_observation_journal as journal
    from src.analytics.entry_observation_runtime import ObservationRuntime
    from src.analytics.kis_frame_diagnostics import validate_study_binding
    from test_entry_observation_runtime import NOW, owner, read
    plan, context = load(tmp_path, ready_plan); clock = [NOW]
    monkeypatch.setattr(obs, '_now', lambda: clock[0].isoformat())
    monkeypatch.setattr(journal, '_timestamp', lambda: clock[0].isoformat())
    bot = owner(); runtime = await ObservationRuntime.install(bot, plan, now=lambda: clock[0])
    b = runtime.buffer; q = bot.ws_feed._quote_subscription_owner
    q._operational = [('H0STCNT0', '900002')]
    await q._lock.acquire()
    try:
        clock[0] = plan.start_at
        scan = obs.capture_scan(b, [stock()], 'regular')
        signal(b, scan, 'pending'); ready(b, 'pending')
        tasks = list(q._tasks)
        clock[0] = plan.end_at
        await runtime.close()  # Must seal incomplete without waiting for the enrollment lock.
        assert not b.capture_status()['complete']
        assert 'order_ready_enrollment_unresolved' in b.capture_status()['incomplete_reasons']
        data = read(plan)
        assert not data['complete'] and runtime.result['sealed']
        validate_study_binding(data, context)
    finally:
        q._lock.release()
        await asyncio.gather(*q._tasks)
    assert not q._leases
    assert q._operational == [('H0STCNT0', '900002')]
    assert not q._ready_unresolved


def test_ready_direct_malformed_timestamp_is_observation_failure_only(tmp_path, monkeypatch):
    _, _, b, q, _ = setup(tmp_path, monkeypatch)
    scan = obs.capture_scan(b, [stock()], 'regular')
    signal(b, scan, 'malformed')
    assert b.publish(dict(kind='order_ready', signal_id='malformed', symbol='900001',
        order_id='bad-clock-order', requested_quantity=1, strategy='gap_and_go', observed_at='10:00'))
    assert not q._tasks and not q._leases
    assert 'order_ready_admission_failed' in b.capture_status()['incomplete_reasons']


def test_ready_real_hook_contains_malformed_timestamp(tmp_path, monkeypatch):
    from types import SimpleNamespace
    _, _, b, q, _ = setup(tmp_path, monkeypatch)
    scan = obs.capture_scan(b, [stock()], 'regular')
    e = event('malformed-hook'); e.source = 'live_screening'
    obs.capture_signal(b, scan, e)
    stop = SimpleNamespace(stop_pct=3, source='synthetic', crash_capped=False)
    bot = SimpleNamespace(_entry_price_observer=b, _strategy_exit_params={},
                          exit_manager=SimpleNamespace(resolve_stop=lambda **kwargs: stop))
    order = SimpleNamespace(id='bad-clock-hook', symbol='900001', strategy='gap_and_go',
        quantity=2, price=10000, side=SimpleNamespace(value='buy'), order_type=SimpleNamespace(value='market'))
    monkeypatch.setattr(obs, '_now', lambda: '10:00')
    obs.capture_order_ready(bot, e, order)
    assert not b.capture_status()['complete']
    assert not q._tasks and order.quantity == 2 and order.side.value == 'buy'


@pytest.mark.asyncio
async def test_ready_monotonic_expiry_before_wall_end_is_incomplete(tmp_path, monkeypatch):
    plan, context, b, q, clock = setup(tmp_path, monkeypatch)
    monotonic = [0]
    q.clock = lambda: monotonic[0]
    scan = obs.capture_scan(b, [stock()], 'regular')
    signal(b, scan, 'early-expiry'); ready(b, 'early-expiry')
    await asyncio.gather(*q._tasks)
    monotonic[0] = q._leases['early-expiry'].deadline
    assert clock[0] < plan.end_at
    await q.maintain()
    assert not q._leases
    assert 'order_ready_lease_expired_early' in b.capture_status()['incomplete_reasons']
    validate_window_contract(context, b.export())


@pytest.mark.parametrize('acknowledged', [True, False])
def test_ready_economic_bundle_admission_is_not_channel_evidence(tmp_path, acknowledged):
    import hashlib
    import json
    from datetime import datetime
    from src.analytics import entry_gate_trace as gates
    from test_entry_evaluation_bundle import fixture, review, build
    c, o, _ = fixture()
    _, declared = load(tmp_path, ready_plan)
    c['capture'] = declared['capture']
    c['capture'].update(start_at='2026-10-02T09:13:00+09:00',
        admission_end_at='2026-10-02T09:20:00+09:00', end_at=c['as_of'],
        source_version_ref=c['capital_policy']['source_version_ref'],
        configuration_ref=c['capital_policy']['configuration_ref'])
    c['capture']['channels']['lease_seconds'] = 3600
    c['capture']['entry_gate_trace'].update(source_version_ref=c['capture']['source_version_ref'],
                                           configuration_ref=c['capture']['configuration_ref'])
    scan = o['records'][0]
    scan.update(population_scope='window_returned_scan_candidates',
        scan_admission_ref=c['capture']['scan_admission_ref'], selection_basis_expected=True,
        selection_basis_max_candidates=5, selection_basis_max_terms=16,
        selection_sources_expected=True, selection_sources_status='unavailable',
        **gates.scan_fields(c['capture']['entry_gate_trace']))
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
                candidate_id=signals[r['signal_id']]['candidate_id'], tr_id='H0STASP0',
                expires_at=c['as_of']))
            for status in (['requested', 'acknowledged'] if acknowledged else ['requested']):
                o['records'].append(dict(kind='quote_subscription', status=status, generation=1,
                    connection_id='synthetic-connection', observed_at=r['observed_at'],
                    symbol=r['symbol'], tr_id='H0STASP0'))
        if r['kind'] == 'ws_quote':
            r['provenance'].update(generation=1, connection_id='synthetic-connection')
    for sequence, r in enumerate(o['records'], 1): r['sequence'] = sequence
    o['frame_diagnostics'] = c['capture']['frame_diagnostics']
    o['journal'].update(format='entry-observation-journal-v2', frame_diagnostics=o['frame_diagnostics'])
    sha = hashlib.sha256((json.dumps(c, ensure_ascii=False, sort_keys=True,
                                    separators=(',', ':')) + '\n').encode()).hexdigest()
    o['journal']['study_sha256'] = sha
    out = build(c, o, sha, review(c, o, sha))
    assert out['original']['capture_complete'] is acknowledged
    assert out['original']['report']['counts']['paired_outcomes'] == (3 if acknowledged else 0)


@pytest.mark.parametrize('defect', ['missing_epoch', 'short_lease'])
def test_ready_offline_contract_rejects_missing_epoch_and_short_lease(tmp_path, defect):
    _, context = load(tmp_path, ready_plan)
    if defect == 'missing_epoch': context.pop('evaluation_epoch')
    else: context['capture']['channels']['lease_seconds'] = 1
    with pytest.raises(ValueError): validate_window_contract(context)


@pytest.mark.parametrize('candidate_id', [None, 1, False])
def test_ready_reader_rejects_non_text_candidate_identity(tmp_path, monkeypatch, candidate_id):
    _, context, b, _, _ = setup(tmp_path, monkeypatch)
    b.order_ready_quote_submit = lambda admission: None
    scan = obs.capture_scan(b, [stock()], 'regular')
    signal(b, scan, 'bad-identity'); ready(b, 'bad-identity')
    data = b.export()
    for record in data['records']:
        if record['kind'] == 'scan':
            record['candidates'][0]['candidate_id'] = candidate_id
        elif record['kind'] in ('signal', 'order_ready_admission'):
            record['candidate_id'] = candidate_id
    with pytest.raises(ValueError):
        validate_window_contract(context, data)
