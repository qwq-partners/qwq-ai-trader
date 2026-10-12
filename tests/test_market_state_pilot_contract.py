"""기술 수집은 경제성 모집단을 생성하지 않으며 실제 시간/저장 한도를 지킨다."""
import hashlib
import importlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

NOW = datetime(2026, 10, 13, 0, 59, tzinfo=timezone.utc)


def context(tmp_path):
    start = NOW + timedelta(minutes=1)
    end = start + timedelta(minutes=5)
    return dict(schema_version='market-state-pilot-study-v1', evaluation_epoch='synthetic-pilot',
        as_of=end.isoformat(), capture=dict(version='runner-market-state-pilot-v1',
        study_ref='synthetic-study', start_at=start.isoformat(), end_at=end.isoformat(),
        pilot_symbol='005490', source_version_ref='synthetic-source',
        configuration_ref='synthetic-config', capacity_evidence_ref='synthetic-capacity',
        journal_path=str(tmp_path/'pilot.jsonl'), buffer_capacity=200, queue_capacity=200,
        batch_size=10, max_record_bytes=4096, max_bytes=200*6144+2048,
        open_timeout_seconds=1, close_timeout_seconds=1,
        channels=dict(registration_cap=41, external_reserved=0, operational_headroom=10,
          max_candidates=1, lease_seconds=360, evidence_ref='synthetic-single-socket',
          market_state_evidence=True, market_state_max_records=100)))


def mod():
    try:
        return importlib.import_module('src.analytics.market_state_pilot')
    except ModuleNotFoundError:
        pytest.fail('technical pilot contract is not implemented')


def plan(tmp_path, edit=None):
    from src.analytics.entry_observation_runtime import CapturePlan
    c = context(tmp_path)
    if edit: edit(c)
    path = tmp_path/'study.json'
    raw = json.dumps(c).encode(); path.write_bytes(raw)
    return CapturePlan(path, hashlib.sha256(raw).hexdigest(), raw)


def test_pilot_contract_is_technical_only(tmp_path):
    p = plan(tmp_path)
    mod().validate_pilot_context(p.context, study_path=p.study_path, now=NOW)
    assert not {'opportunities', 'capital_policy', 'fee_policy'} & p.context.keys()


@pytest.mark.parametrize('defect', ['unknown', 'economic', 'symbol', 'multiple', 'duration', 'past',
    'naive', 'weekend', 'end', 'state_off', 'multiple_candidates', 'lease', 'quota', 'memory',
    'queue', 'bytes', 'record', 'channel_budget', 'batch', 'timeout', 'epoch', 'relative', 'reused'])
def test_invalid_technical_contract_rejected(tmp_path, defect):
    c = context(tmp_path); s = c['capture']; ch = s['channels']
    if defect == 'unknown': s['selection_basis'] = {}
    elif defect == 'economic': c['opportunities'] = []
    elif defect == 'symbol': s['pilot_symbol'] = '１２３４５６'
    elif defect == 'multiple': s['pilot_symbol'] = ['005490', '005930']
    elif defect == 'duration': s['end_at'] = c['as_of'] = (NOW+timedelta(minutes=7)).isoformat()
    elif defect == 'past': s['start_at'] = NOW.isoformat()
    elif defect == 'naive': s['start_at'] = '2026-10-13T10:00:00'
    elif defect == 'weekend':
        s['start_at']='2026-10-17T10:00:00+09:00'; s['end_at']=c['as_of']='2026-10-17T10:05:00+09:00'
    elif defect == 'end': c['as_of'] = s['start_at']
    elif defect == 'state_off': ch['market_state_evidence'] = False
    elif defect == 'multiple_candidates': ch['max_candidates'] = 2
    elif defect == 'lease': ch['lease_seconds'] = 300
    elif defect == 'quota': ch['market_state_max_records'] = True
    elif defect == 'memory': s['buffer_capacity'] = 199
    elif defect == 'queue': s['queue_capacity'] = 199
    elif defect == 'bytes': s['max_bytes'] -= 1
    elif defect == 'record': s['max_record_bytes'] = 4000
    elif defect == 'channel_budget': ch['registration_cap'] = 12
    elif defect == 'batch': s['batch_size'] = 201
    elif defect == 'timeout': s['open_timeout_seconds'] = float('nan')
    elif defect == 'epoch': c['evaluation_epoch'] = ''
    elif defect == 'relative': s['journal_path'] = 'pilot.jsonl'
    elif defect == 'reused': Path(s['journal_path']).write_text('original')
    with pytest.raises((ValueError, FileExistsError)):
        mod().validate_pilot_context(c, study_path=tmp_path/'study.json', now=NOW)


def test_pilot_buffer_excludes_economic_events_other_symbol_and_outside_window(tmp_path):
    p = plan(tmp_path); clock=[NOW]; b = mod().PilotBuffer(p, lambda: clock[0])
    assert b.scan_scope == 'pilot' and b.begin_scan() is None
    assert not b.accepts_order_signal('any')
    assert b.publish(dict(kind='quote_subscription', status='desired'))
    q = dict(kind='ws_quote', symbol='005490', provenance={'received_at':p.start_at.isoformat()})
    assert not b.publish(q)
    clock[0] = p.start_at
    for kind in ('scan','signal','order_ready','rest_quote','emit_result','entry_gate_trace','selection_basis'):
        assert not b.publish(dict(kind=kind, symbol='005490'))
    assert not b.publish({**q, 'symbol':'005930'})
    assert b.publish(q)
    assert b.publish(dict(kind='market_state', symbol='005490', received_at=p.start_at.isoformat()))
    clock[0] = p.end_at
    assert not b.publish(q)
    clock[0] = p.start_at
    assert not b.publish(q)
    assert 'capture_clock_regressed' in b.capture_status()['incomplete_reasons']
    assert len(b.export()['records']) == 3

@pytest.mark.parametrize('kind',['ws_quote','market_state'])
@pytest.mark.parametrize('receipt',['before','after','missing','naive'])
def test_delayed_delivery_does_not_admit_a_receipt_outside_declared_window(tmp_path,kind,receipt):
    p=plan(tmp_path); b=mod().PilotBuffer(p,lambda:NOW)
    b._now=lambda:p.start_at+timedelta(seconds=1)
    value={'before':(p.start_at-timedelta(microseconds=1)).isoformat(),
           'after':p.end_at.isoformat(),'missing':None,'naive':'2026-10-13T10:00:00'}[receipt]
    row=dict(kind=kind,symbol='005490')
    if kind=='market_state': row['received_at']=value
    else: row['provenance']={'received_at':value}
    assert not b.publish(row)
    if receipt in ('missing','naive'):
        assert 'pilot_receipt_time_invalid' in b.capture_status()['incomplete_reasons']
