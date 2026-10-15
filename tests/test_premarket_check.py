import importlib.util
import json
from datetime import datetime
from pathlib import Path

import pytest


def api():
    path = Path(__file__).parents[1] / 'scripts/ops/report_premarket_check.py'
    spec = importlib.util.spec_from_file_location('premarket_check', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def row(**kwargs):
    value = dict(ts='2026-10-16T07:01:00', task='morning_brief',
                 provider='gemini', request_sent=True, refusal_detected=False, role_policy_version='engine-roles-v1',
                 requested_model='gemini-3.1-flash-lite', resolved_model='gemini-3.1-flash-lite',
                 requested_thinking_level='minimal', outcome='success', usage_known=True,
                 input_tokens=100, output_tokens=30, elapsed_ms=200,
                 finish_reason='STOP', estimated_cost_usd=0.001)
    return value | kwargs


def test_old_or_future_audit_does_not_prove_current_call():
    a = api()
    result = a.summarize_roles([row(ts='2026-10-15T07:01:00'), row(ts='2026-10-16T08:00:00')],
                               datetime.fromisoformat('2026-10-16T07:35:00+09:00'))
    assert result['morning_brief']['status'] == 'not_observed'


def test_provider_unknown_or_error_never_passes():
    a = api()
    for patch in ({'resolved_model': None}, {'outcome': 'error'}, {'usage_known': False},
                  {'finish_reason': 'MAX_TOKENS'}, {'requested_model': 'other'},
                  {'elapsed_ms': float('nan')}, {'provider': 'openai'}, {'request_sent': False},
                  {'role_policy_version': None}, {'refusal_detected': True}):
        result = a.summarize_roles([row(**patch)], datetime.fromisoformat('2026-10-16T07:35:00+09:00'))
        assert result['morning_brief']['status'] == 'review_required'


def test_success_is_metadata_only_not_delivery_or_profit():
    a = api()
    result = a.summarize_roles([row(content='SECRET', prompt='SECRET', error='SECRET')],
                               datetime.fromisoformat('2026-10-16T07:35:00+09:00'))
    assert result['morning_brief']['status'] == 'call_metadata_verified'
    assert 'SECRET' not in json.dumps(result)
    assert result['post_exit_research']['status'] == 'not_due_friday'


def test_missing_and_malformed_are_not_zero_success(tmp_path):
    a = api()
    assert a.audit_rows(tmp_path / 'absent')[1] == 'missing'
    p = tmp_path / 'bad'; p.write_text('{broken}\n')
    assert a.audit_rows(p)[1] == 'invalid'


def test_symlink_and_oversize_rejected(tmp_path):
    a = api()
    p = tmp_path / 'data'; p.write_text('{}')
    link = tmp_path / 'link'; link.symlink_to(p)
    with pytest.raises(ValueError):
        a.read_bounded(link, 10)
    with pytest.raises(ValueError):
        a.read_bounded(p, 1)


def test_time_guard_precedes_receipt_and_duplicate_is_not_reexecuted(tmp_path, monkeypatch):
    a = api()
    manifest = {'date': '2026-10-16', 'stage': 'roles', 'not_before': '2026-10-16T07:35:00+09:00',
                'not_after': '2026-10-16T07:45:00+09:00', 'output_dir': str(tmp_path / 'out')}
    monkeypatch.setattr(a, 'collect', lambda m, now: {'status': 'review_required'})
    assert a.run(manifest, datetime.fromisoformat('2026-10-15T07:35:00+09:00')) == 2
    assert not (tmp_path / 'out').exists()
    assert a.run(manifest, datetime.fromisoformat('2026-10-16T07:35:00+09:00')) == 2
    monkeypatch.setattr(a, 'collect', lambda *_: pytest.fail('duplicate ran'))
    assert a.run(manifest, datetime.fromisoformat('2026-10-16T07:36:00+09:00')) == 3
    assert (tmp_path / 'out/result.json').stat().st_mode & 0o777 == 0o600


def test_dispatch_status_and_same_day_required(tmp_path):
    a = api()
    p = tmp_path / 'brief.json'
    p.write_text(json.dumps({'kr_date': '2026-10-16', 'dispatch': {'status': 'failed'}}))
    assert a.brief_evidence(p, '2026-10-16')['dispatch_status'] != 'sent'
    p.write_text(json.dumps({'kr_date': '2026-10-15', 'dispatch': {'status': 'sent'}}))
    assert a.brief_evidence(p, '2026-10-16')['dispatch_status'] != 'sent'


def test_dispatch_requires_matching_generated_version(tmp_path):
    a = api(); p = tmp_path / 'brief.json'
    generated = {'version': 1, 'text_sha256': 'abc', 'generated_at': '2026-10-16T07:01:00'}
    sent = {'status': 'sent', 'sent_at': '2026-10-16T07:30:00',
            'brief_ref': {'version': 2, 'text_sha256': 'different'}}
    p.write_text(json.dumps({'kr_date': '2026-10-16', 'generated': [generated], 'dispatch': [sent]}))
    assert a.brief_evidence(p, '2026-10-16')['dispatch_status'] != 'sent'
    sent['brief_ref'] = generated
    p.write_text(json.dumps({'kr_date': '2026-10-16', 'generated': [generated], 'dispatch': [sent]}))
    assert a.brief_evidence(p, '2026-10-16', now=datetime.fromisoformat('2026-10-16T07:35:00+09:00'))['dispatch_status'] == 'sent'
    assert a.brief_evidence(p, '2026-10-16', now=datetime.fromisoformat('2026-10-16T07:20:00+09:00'))['dispatch_status'] != 'sent'


def test_duplicate_audit_is_ambiguous_not_double_cost():
    a = api(); r = row()
    result = a.summarize_roles([r, dict(r)], datetime.fromisoformat('2026-10-16T07:35:00+09:00'))
    assert result['morning_brief']['status'] == 'review_required'
    assert result['morning_brief']['estimated_cost_usd'] is None


def test_health_projection_never_copies_accounts_or_symbols():
    a = api()
    result = a.health_evidence({'broker': {'connected': True, 'pending_orders': 0},
       'risk_manager': {'pending_orders': 0, 'reserved_cash': 'SECRET'},
       'execution_recovery': {'status': 'ready', 'journal_pending_count': 0, 'unattributed_symbols': ['SECRET']}})
    assert result['unattributed_symbol_count'] == 1
    assert result['healthy'] is False
    assert 'SECRET' not in json.dumps(result)
    assert a.health_evidence({})['healthy'] is False


def test_preopen_requires_pins_pid_timer_and_health(tmp_path, monkeypatch):
    import hashlib
    a = api(); pin = tmp_path / 'source'; pin.write_bytes(b'fixed')
    kill = tmp_path / 'kill'; kill.touch()
    manifest = {'stage': 'preopen', 'repository': str(tmp_path), 'expected_head': 'abc',
                'expected_pid': 123, 'file_pins': {str(pin): hashlib.sha256(b'fixed').hexdigest()},
                'kill_switch': str(kill), 'capture_receipt': str(tmp_path / 'receipt'),
                'expected_timer_time': 'Fri 2026-10-16 09:10:00 KST', 'min_free_bytes': 1}
    monkeypatch.setattr(a, 'command', lambda _: 'abc')
    monkeypatch.setattr(a, 'service', lambda name, fields: {'ActiveState': 'active', 'MainPID': '123', 'NRestarts': '0'}
                        if name == 'qwq-ai-trader' else {'ActiveState': 'active', 'UnitFileState': 'enabled',
                            'NextElapseUSecRealtime': manifest['expected_timer_time']})
    monkeypatch.setattr(a, 'local_health', lambda: {'healthy': True})
    now = datetime.fromisoformat('2026-10-16T08:50:00+09:00')
    assert a.collect(manifest, now)['status'] == 'evidence_collected'
    pin.write_bytes(b'changed')
    assert a.collect(manifest, now)['status'] == 'review_required'
    pin.write_bytes(b'fixed'); manifest['expected_pid'] = 124
    assert a.collect(manifest, now)['status'] == 'review_required'


def test_failed_collection_preserves_consumed_attempt(tmp_path, monkeypatch):
    a = api()
    manifest = {'date': '2026-10-16', 'stage': 'preopen', 'not_before': '2026-10-16T08:50:00+09:00',
                'not_after': '2026-10-16T08:59:00+09:00', 'output_dir': str(tmp_path / 'out')}
    def fail(*_):
        raise a.http.client.RemoteDisconnected()
    monkeypatch.setattr(a, 'collect', fail)
    now = datetime.fromisoformat('2026-10-16T08:50:00+09:00')
    assert a.run(manifest, now) == 2
    assert json.loads((tmp_path / 'out/result.json').read_text())['status'] == 'read_failed'
    assert a.run(manifest, now) == 3
