"""가짜 API·임시 월 원장만 사용하는 Bear API 상한 진단 수락 검사."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
import importlib
import json
from types import SimpleNamespace

import pytest

from src.analytics import model_comparison_acceptance as acc
from src.analytics.model_comparison_execution import ComparisonCallGuard, ComparisonExecutionPolicy
from src.utils.llm_budget import MonthlyBudgetLedger
from tests.test_model_comparison_acceptance import spec
from tests.test_model_comparison_claude import FakeAPI, _ok, reservations
from tests.test_model_comparison_wiring import controls

NOW = datetime(2026, 11, 4, 18, 0, tzinfo=acc.KST)
SHA = 'a' * 40


def module():
    # 구현이 없는 첫 실행도 의도한 기능 부재로 명확히 실패한다.
    import importlib.util
    assert importlib.util.find_spec('src.analytics.bear_api_cap_diagnostic'), 'diagnostic module not implemented'
    return importlib.import_module('src.analytics.bear_api_cap_diagnostic')


def prepared(tmp_path, monkeypatch):
    m = module()
    monkeypatch.setattr(m, '_source_commit', lambda: SHA)
    source = tmp_path / 'old'
    acc.prepare_acceptance(controls(), experiment_id='old', day='2026-11-03', candidate={'reasoning_effort': 'low'},
                           input_spec=spec(), out_dir=source, now=NOW - timedelta(days=1))
    out = tmp_path / 'new'
    summary = m.prepare_diagnostic(source_dir=source, out_dir=out, experiment_id='new', source_commit=SHA,
                                   start=NOW.isoformat(), end=(NOW + timedelta(minutes=30)).isoformat(), now=NOW)
    return m, source, out, summary


def setup_run(tmp_path, monkeypatch, script=None):
    m, source, out, summary = prepared(tmp_path, monkeypatch)
    led = MonthlyBudgetLedger(tmp_path / 'budget.db', 100, clock=lambda: NOW, policy_enabled=True)
    def ok(response):
        response = _ok(response)
        response.http_status = 200
        response.content = 'ACCEPT RESPONSE_SENTINEL'
        return response
    client = FakeAPI(script or ok)
    calls = []
    def factory(config):
        calls.append('factory')
        return client
    kwargs = dict(out_dir=out, approved_by='reviewer', approved_plan_sha256=summary['plan_sha256'],
                  clock=lambda: NOW, config_loader=lambda: object(), controls_reader=lambda config: controls(),
                  ledger_factory=lambda config: led, client_factory=factory)
    return m, out, led, client, calls, kwargs


def test_prepare_is_pure_fresh_and_retains_provenance(tmp_path, monkeypatch):
    m, source, out, summary = prepared(tmp_path, monkeypatch)
    plan = json.loads((out / 'plan.json').read_text())
    assert plan['provenance']['experiment_id'] == 'old'
    assert plan['provenance']['prepared_at'].startswith('2026-11-03')
    assert plan['contract']['caps'] == [400, 800]
    assert plan['contract']['timeout_seconds'] == 5.75
    assert plan['contract']['continue_after_known_max_tokens'] is True
    assert set(p.name for p in out.iterdir()) == {'plan.json', 'input.json'}
    assert 'SENTINELCTX' not in (out / 'plan.json').read_text()
    before = {p.name: p.read_bytes() for p in source.iterdir()}
    with pytest.raises(ValueError):
        m.prepare_diagnostic(source_dir=source, out_dir=out, experiment_id='new', source_commit=SHA,
                             start=NOW.isoformat(), end=(NOW + timedelta(minutes=30)).isoformat(), now=NOW)
    assert before == {p.name: p.read_bytes() for p in source.iterdir()}


@pytest.mark.parametrize('bad', ['approval', 'digest', 'source', 'controls', 'window', 'input', 'contract'])
def test_preflight_rejects_before_factory_and_reservation(tmp_path, monkeypatch, bad):
    m, out, led, client, calls, kw = setup_run(tmp_path, monkeypatch)
    if bad == 'approval': kw['approved_by'] = ''
    if bad == 'digest': kw['approved_plan_sha256'] = 'b' * 64
    if bad == 'source': monkeypatch.setattr(m, '_source_commit', lambda: 'b' * 40)
    if bad == 'controls': kw['controls_reader'] = lambda config: {}
    if bad == 'window': kw['clock'] = lambda: NOW + timedelta(hours=1)
    if bad in ('input', 'contract'):
        path = out / ('input.json' if bad == 'input' else 'plan.json')
        value = json.loads(path.read_text())
        if bad == 'input': value['input_spec']['sector'] = 'changed'
        else: value['contract']['caps'] = [800, 800]
        path.write_text(json.dumps(value))
    with pytest.raises(ValueError): asyncio.run(m.run_diagnostic(**kw))
    assert calls == [] and client.calls == [] and reservations(led) == []
    assert not (out / 'consumed.json').exists()


def test_success_runs_caps_only_once_closes_and_scrubs_receipt(tmp_path, monkeypatch):
    m, out, led, client, calls, kw = setup_run(tmp_path, monkeypatch)
    result = asyncio.run(m.run_diagnostic(**kw))
    assert result['status'] == 'completed'
    assert [r['max_tokens'] for r in client.calls] == [400, 800]
    assert client.calls[0]['prompt'] == client.calls[1]['prompt']
    assert client.calls[0]['system'] == client.calls[1]['system']
    assert reservations(led) == [('settled',), ('settled',)]
    assert all(a['billing_channel'] == 'api' and a['verdict'] == 'accept' for a in result['attempts'])
    assert result['client_closed'] is True
    assert 'RESPONSE_SENTINEL' not in (out / 'receipt.json').read_text()
    assert 'SENTINELCTX' not in (out / 'receipt.json').read_text()
    with pytest.raises(ValueError, match='consumed'): asyncio.run(m.run_diagnostic(**kw))
    assert len(client.calls) == 2 and len(calls) == 1


def test_only_known_max_tokens_continues_without_partial_verdict(tmp_path, monkeypatch):
    def script(response):
        _ok(response)
        response.http_status = 200
        response.success = False
        response.transport_error_kind = 'api_incomplete'
        response.finish_reason = 'max_tokens'
        return response
    m, out, led, client, calls, kw = setup_run(tmp_path, monkeypatch, script)
    result = asyncio.run(m.run_diagnostic(**kw))
    assert len(client.calls) == 2
    assert [a['outcome'] for a in result['attempts']] == ['expected_max_tokens', 'stopped']
    assert all(a['verdict'] is None for a in result['attempts'])
    assert result['status'] == 'stopped'


@pytest.mark.parametrize('case', ['timeout', 'refusal', 'unknown', 'identity', 'invalid_verdict', 'http', 'error'])
def test_other_failure_stops_after_one(tmp_path, monkeypatch, case):
    def script(response):
        _ok(response)
        response.http_status = 200
        if case == 'timeout': response.transport_error_kind, response.success = 'api_timeout', False
        if case == 'refusal': response.refusal_detected = True
        if case == 'unknown': response.usage_known = False
        if case == 'identity': response.resolved_model = 'wrong'
        if case == 'invalid_verdict': response.content = 'uncertain'
        if case == 'http': response.http_status = 500
        if case == 'error': raise RuntimeError('RESPONSE_SENTINEL')
        return response
    m, out, led, client, calls, kw = setup_run(tmp_path, monkeypatch, script)
    result = asyncio.run(m.run_diagnostic(**kw))
    assert len(client.calls) == 1 and result['status'] == 'stopped'
    assert result['client_closed'] is True
    assert 'RESPONSE_SENTINEL' not in (out / 'receipt.json').read_text()


def test_consumption_is_exclusive_and_survives_factory_failure(tmp_path, monkeypatch):
    m, out, led, client, calls, kw = setup_run(tmp_path, monkeypatch)
    def fail(config):
        calls.append('fail')
        raise RuntimeError('sensitive')
    kw['client_factory'] = fail
    def run():
        try: return asyncio.run(m.run_diagnostic(**kw))['status']
        except ValueError: return 'consumed'
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda _: run(), range(2)))
    assert sorted(results) == ['consumed', 'stopped']
    assert calls == ['fail'] and reservations(led) == []


def test_existing_guard_policy_conflict_never_creates_client(tmp_path, monkeypatch):
    m, out, led, client, calls, kw = setup_run(tmp_path, monkeypatch)
    policy = ComparisonExecutionPolicy('old-policy', 4, ('claude-opus-5-5',), 400, '0.50',
                                        '2026-11-04', '2026-11-04', 1, 65536)
    ComparisonCallGuard(ledger=led, policy=policy)
    result = asyncio.run(m.run_diagnostic(**kw))
    assert result['status'] == 'stopped' and calls == [] and reservations(led) == []
    with led._connect(readonly=True) as db:
        assert json.loads(db.execute('SELECT policy FROM comparison_policy').fetchone()[0])['experiment_id'] == 'old-policy'


def test_guard_delay_cannot_extend_deadline_or_accept_late_result(tmp_path, monkeypatch):
    m, out, led, client, calls, kw = setup_run(tmp_path, monkeypatch)
    original = ComparisonCallGuard.call
    recorded = []
    async def delayed(self, request, **args):
        fixed = m.DEADLINE_AT.get()
        recorded.append(fixed)
        result = await original(self, request, **args)
        monkeypatch.setattr(m, '_monotonic', lambda: fixed + 1)
        return result
    monkeypatch.setattr(ComparisonCallGuard, 'call', delayed)
    result = asyncio.run(m.run_diagnostic(**kw))
    assert len(client.calls) == 1
    assert client.calls[0]['deadline'] <= recorded[0]
    assert result['attempts'][0]['outcome'] == 'late'
    assert result['attempts'][0]['verdict'] is None


def test_readonly_policy_check_preserves_existing_policy_requests_budget(tmp_path, monkeypatch):
    m, out, led, client, calls, kw = setup_run(tmp_path, monkeypatch)
    asyncio.run(m.run_diagnostic(**kw))
    plan = json.loads((out / 'plan.json').read_text())
    def snapshot():
        with led._connect(readonly=True) as db:
            return {table: [tuple(r) for r in db.execute('SELECT * FROM ' + table)]
                    for table in ('comparison_policy', 'comparison_requests', 'reservations', 'comparison_attempts')}
    before = snapshot()
    other = dict(plan, experiment_id='different')
    assert m.check_ledger_policy(led.path, other) == {'ready': False, 'reason': 'existing_policy_conflict'}
    assert m.check_ledger_policy(led.path, plan) == {'ready': True, 'reason': 'matching_policy'}
    assert before == snapshot()
    missing = tmp_path / 'absent.db'
    assert m.check_ledger_policy(missing, plan) == {'ready': False, 'reason': 'existing_ledger_required'}
    assert not missing.exists()


def test_cli_ledger_check_reports_conflict_without_factories(tmp_path, monkeypatch, capsys):
    m, out, led, client, calls, kw = setup_run(tmp_path, monkeypatch)
    old = ComparisonExecutionPolicy('old', 4, ('claude-opus-5-5',), 400, '0.50', '2026-11-04', '2026-11-04', 1, 65536)
    ComparisonCallGuard(ledger=led, policy=old)
    assert m.main(['check-ledger', '--dir', str(out), '--ledger-path', str(led.path)]) == 1
    assert json.loads(capsys.readouterr().out) == {'ready': False, 'reason': 'existing_policy_conflict'}
    assert calls == [] and reservations(led) == []


def test_nonempty_and_symlink_output_rejected(tmp_path, monkeypatch):
    m, source, out, summary = prepared(tmp_path, monkeypatch)
    other = tmp_path / 'other'
    other.mkdir(mode=0o700)
    (other / 'unrelated').write_text('preserve')
    link = tmp_path / 'link'
    link.symlink_to(other, target_is_directory=True)
    for target in (other, link):
        with pytest.raises(ValueError):
            m.prepare_diagnostic(source_dir=source, out_dir=target, experiment_id='fresh', source_commit=SHA,
                                 start=NOW.isoformat(), end=(NOW + timedelta(minutes=30)).isoformat(), now=NOW)
    assert (other / 'unrelated').read_text() == 'preserve'


def test_cancelled_execution_stays_consumed_and_closes(tmp_path, monkeypatch):
    def cancel(response):
        raise asyncio.CancelledError
    m, out, led, client, calls, kw = setup_run(tmp_path, monkeypatch, cancel)
    with pytest.raises(asyncio.CancelledError): asyncio.run(m.run_diagnostic(**kw))
    assert client._closed and len(client.calls) == 1
    assert json.loads((out / 'receipt.json').read_text())['status'] == 'stopped'
    with pytest.raises(ValueError, match='consumed'): asyncio.run(m.run_diagnostic(**kw))


def test_underlying_session_must_really_close(tmp_path, monkeypatch):
    m, out, led, client, calls, kw = setup_run(tmp_path, monkeypatch)
    client._session = SimpleNamespace(closed=False)
    async def incomplete_close(): client._closed = True
    client.close = incomplete_close
    result = asyncio.run(m.run_diagnostic(**kw))
    assert result['status'] == 'stopped' and result['client_closed'] is False


def test_expired_admission_deadline_never_reaches_raw_http(tmp_path, monkeypatch):
    # 실제 raw API는 deadline 검사 뒤에만 키/HTTP에 접근한다. guard 입장 지연을 재현한다.
    from src.utils.claude_transport import AnthropicAPIClient
    from src.utils.llm import LLMConfig
    m, out, led, client, calls, kw = setup_run(tmp_path, monkeypatch)
    raw = AnthropicAPIClient(LLMConfig(openai_api_key='', gemini_api_key=''))
    touched = []
    def forbidden_key():
        touched.append('credentials')
        raise AssertionError('must not read key')
    monkeypatch.setattr(raw, '_key', forbidden_key)
    kw['client_factory'] = lambda config: raw
    original = ComparisonCallGuard.call
    async def delayed(self, request, **args):
        # 전송 어댑터가 보는 절대 상한만 과거로 두어 실시간 5.75초 대기 없이 재현한다.
        token = m.DEADLINE_AT.set(asyncio.get_running_loop().time() - 1)
        try: return await original(self, request, **args)
        finally: m.DEADLINE_AT.reset(token)
    monkeypatch.setattr(ComparisonCallGuard, 'call', delayed)
    result = asyncio.run(m.run_diagnostic(**kw))
    assert touched == [] and len(result['attempts']) == 1
    assert result['attempts'][0]['verdict'] is None and result['status'] == 'stopped'
    assert reservations(led) == [('settled',)]


def test_window_too_short_does_not_shorten_fixed_attempt_or_reserve(tmp_path, monkeypatch):
    m, out, led, client, calls, kw = setup_run(tmp_path, monkeypatch)
    kw['clock'] = lambda: NOW + timedelta(minutes=30, seconds=-2)
    result = asyncio.run(m.run_diagnostic(**kw))
    assert result['status'] == 'stopped' and calls == [] and reservations(led) == []


def test_receipt_has_safe_identity_usage_timing_and_ledger_evidence(tmp_path, monkeypatch):
    m, out, led, client, calls, kw = setup_run(tmp_path, monkeypatch)
    result = asyncio.run(m.run_diagnostic(**kw))
    attempt = result['attempts'][0]
    assert attempt['requested_model'] == attempt['resolved_model'] == 'claude-opus-5-5'
    assert attempt['input_tokens'] == 100 and attempt['output_tokens'] == 10
    assert attempt['attempt_id'] == 'new:api:400'
    assert attempt['reservation_token'] and attempt['ledger_state'] == 'known'
    assert attempt['elapsed_ms'] >= 0


def test_transport_exception_receipt_keeps_unknown_attempt_evidence(tmp_path, monkeypatch):
    def fail(response): raise RuntimeError('sensitive')
    m, out, led, client, calls, kw = setup_run(tmp_path, monkeypatch, fail)
    result = asyncio.run(m.run_diagnostic(**kw))
    assert len(result['attempts']) == 1
    attempt = result['attempts'][0]
    assert attempt['attempt_id'] == 'new:api:400' and attempt['reservation_token']
    assert attempt['ledger_state'] == 'blocked' and attempt['settlement_known'] is False
    assert attempt['dispatch_entered'] is True and attempt['billing_status'] == 'unknown'
    assert reservations(led) == [('unknown',)]
    assert attempt['outcome'] == 'error' and attempt['error'] == 'RuntimeError'


def test_close_exception_is_failure_even_when_closed_flags_true(tmp_path, monkeypatch):
    m, out, led, client, calls, kw = setup_run(tmp_path, monkeypatch)
    async def close():
        client._closed = True
        raise RuntimeError('sensitive')
    client.close = close
    result = asyncio.run(m.run_diagnostic(**kw))
    assert result['status'] == 'stopped' and result['error'] == 'client_close_failed'


def test_reused_experiment_in_different_plan_cannot_recover_prior_attempts(tmp_path, monkeypatch):
    m, out, led, client, calls, kw = setup_run(tmp_path, monkeypatch)
    asyncio.run(m.run_diagnostic(**kw))
    other = tmp_path / 'other'
    other.mkdir(mode=0o700)
    (other / 'input.json').write_bytes((out / 'input.json').read_bytes())
    plan = json.loads((out / 'plan.json').read_text())
    plan['output_directory'] = str(other)
    (other / 'plan.json').write_text(json.dumps(plan))
    kw.update(out_dir=other, approved_plan_sha256=m._sha(m._canon(plan)))
    result = asyncio.run(m.run_diagnostic(**kw))
    assert result['status'] == 'stopped' and len(calls) == 1 and len(client.calls) == 2
