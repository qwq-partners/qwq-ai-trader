"""Cancellation receipts through real guard/adapter/API, with synthetic HTTP only."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from src.analytics.model_comparison_execution import ComparisonCallGuard
from src.utils.claude_transport import AnthropicAPIClient
from src.utils.llm import LLMConfig, LLMProvider, LLMResponse
from tests.test_bear_api_cap_diagnostic import setup_run
from tests.test_model_comparison_claude import reservations


class WaitingHTTP:
    """Expire a selected real asyncio timeout only after entering response JSON."""
    status = 200
    closed = False

    def __init__(self, mode, timeouts):
        self.mode, self.timeouts = mode, timeouts
        self.calls = []
        self.exited = False

    def post(self, url, **kwargs):
        self.calls.append(kwargs['json'])
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        self.exited = True

    async def json(self):
        if self.mode == 'direct':
            asyncio.get_running_loop().call_soon(asyncio.current_task().cancel)
        else:
            self.timeouts[0 if self.mode == 'outer' else 1].reschedule(
                asyncio.get_running_loop().time())
        await asyncio.Future()

    async def close(self):
        self.closed = True


def run_http(tmp_path, monkeypatch, mode, mutate=None):
    m, out, ledger, _, factories, kwargs = setup_run(tmp_path, monkeypatch)
    timeouts, guards, attachments = [], [], []
    original_timeout = asyncio.timeout_at

    def timeout_at(deadline):
        timeout = original_timeout(deadline)
        timeouts.append(timeout)
        return timeout

    monkeypatch.setattr(asyncio, 'timeout_at', timeout_at)
    session = WaitingHTTP(mode, timeouts)
    client = AnthropicAPIClient(LLMConfig(anthropic_api_key='synthetic-key',
                                         anthropic_api_key_file='', openai_api_key='', gemini_api_key=''))
    client._session = session
    original_complete = client.complete

    async def complete(*args, **kw):
        try:
            return await original_complete(*args, **kw)
        except asyncio.CancelledError as error:
            attachments.append(error._llm_response)
            if mutate is not None:
                mutate(error)
            raise

    client.complete = complete
    original_call = ComparisonCallGuard.call

    async def call(self, request, **kw):
        guards.append(self)
        return await original_call(self, request, **kw)

    monkeypatch.setattr(ComparisonCallGuard, 'call', call)

    def factory(config):
        factories.append('factory')
        return client

    kwargs['client_factory'] = factory
    if mode == 'direct':
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(m.run_diagnostic(**kwargs))
    else:
        asyncio.run(m.run_diagnostic(**kwargs))
    receipt = json.loads((out / 'receipt.json').read_text())
    assert receipt['status'] == 'stopped' and receipt['client_closed'] is True
    assert factories == ['factory'] and len(session.calls) == 1
    assert session.calls[0]['max_tokens'] == 400
    assert session.exited and session.closed and client._closed and not client._calls
    assert reservations(ledger) == [('unknown',)]
    assert ledger.active_reservations == {}
    record = guards[0].lookup('new:api:400')
    assert record['dispatch_entered'] is True
    if mode != 'inner':
        assert record['result'] is None
    assert (out / 'consumed.json').is_file()
    claims = list(tmp_path.glob('budget.db.bear-api-cap-*.consumed.json'))
    assert len(claims) == 1
    before = ((out / 'consumed.json').read_bytes(), claims[0].read_bytes())
    with pytest.raises(ValueError, match='consumed'):
        asyncio.run(m.run_diagnostic(**kwargs))
    assert before == ((out / 'consumed.json').read_bytes(), claims[0].read_bytes())
    assert len(session.calls) == 1
    assert len(receipt['attempts']) == 1
    return receipt, attachments


def assert_unknown(attempt, error):
    assert attempt['outcome'] == 'error' and attempt['verdict'] is None
    assert attempt['error'] == error and attempt['settlement_known'] is False
    assert attempt['ledger_state'] == 'blocked' and attempt['reservation_token']
    assert attempt['request_sent'] is None and attempt['http_status'] is None
    assert attempt['transport_error_kind'] is None and attempt['billing_status'] == 'unknown'
    assert attempt['input_tokens'] is None and attempt['output_tokens'] is None
    assert attempt['resolved_model'] is None and attempt['model_identity_status'] == 'unverified'


@pytest.mark.parametrize('mode', ['direct', 'outer'])
@pytest.mark.parametrize('attached', [True, False])
def test_real_cancellation_preserves_only_supplemental_status(tmp_path, monkeypatch, mode, attached):
    mutate = None if attached else lambda error: delattr(error, '_llm_response')
    receipt, attachments = run_http(tmp_path, monkeypatch, mode, mutate)
    attempt = receipt['attempts'][0]
    assert_unknown(attempt, 'CancelledError' if mode == 'direct' else 'TimeoutError')
    assert len(attachments) == 1 and attachments[0].http_status == 200
    if attached:
        assert attempt.get('timeout_observation') == {
            'request_sent': True, 'http_status': 200,
            'transport_error_kind': 'cancelled', 'refusal_detected': False}
    else:
        assert 'timeout_observation' not in attempt


def test_inner_api_timeout_retains_normal_response_semantics(tmp_path, monkeypatch):
    receipt, attachments = run_http(tmp_path, monkeypatch, 'inner')
    attempt = receipt['attempts'][0]
    assert attachments == []
    assert 'timeout_observation' not in attempt
    assert attempt['outcome'] == 'stopped' and attempt['verdict'] is None
    assert attempt['transport_error_kind'] == 'api_timeout' and attempt['http_status'] == 200
    assert attempt['request_sent'] is True and attempt['ledger_state'] == 'unknown'
    assert attempt['settlement_known'] is False and attempt['error'] is None


class Hostile:
    def __getattribute__(self, name):
        if name in ('provider', 'request_sent', 'http_status', 'transport_error_kind', 'refusal_detected'):
            raise AssertionError('untrusted attributes must not be read')
        return object.__getattribute__(self, name)

    def __eq__(self, other):
        raise AssertionError('untrusted equality must not run')


class HostileDict(dict):
    def get(self, *args):
        raise AssertionError('untrusted mapping must not run')


class ResponseSubclass(LLMResponse):
    pass


@pytest.mark.parametrize('field,value', [
    ('provider', LLMProvider.OPENAI), ('provider', 'anthropic'),
    ('requested_model', 'other'), ('model', 'other'),
    ('transport', 'claude_cli'), ('billing_channel', 'subscription'),
    ('request_sent', 1), ('request_sent', 'SENTINEL'),
    ('http_status', True), ('http_status', 99), ('http_status', 600),
    ('http_status', 'SENTINEL'), ('refusal_detected', 0),
    ('transport_error_kind', 'SENTINEL'), ('transport_error_kind', Hostile()),
    ('requested_model', Hostile()), ('request_sent', False),
])
def test_malformed_context_or_status_leaves_unknown(tmp_path, monkeypatch, field, value):
    def mutate(error):
        setattr(error._llm_response, field, value)
    receipt, _ = run_http(tmp_path, monkeypatch, 'direct', mutate)
    attempt = receipt['attempts'][0]
    assert_unknown(attempt, 'CancelledError')
    assert 'timeout_observation' not in attempt
    assert 'SENTINEL' not in json.dumps(receipt)


@pytest.mark.parametrize('replacement', [None, {}, SimpleNamespace(), Hostile(),
                                        ResponseSubclass('', 'claude-opus-5-5', LLMProvider.ANTHROPIC)])
def test_arbitrary_attachment_is_not_inspected(tmp_path, monkeypatch, replacement):
    receipt, _ = run_http(tmp_path, monkeypatch, 'direct',
                          lambda error: setattr(error, '_llm_response', replacement))
    assert_unknown(receipt['attempts'][0], 'CancelledError')
    assert 'timeout_observation' not in receipt['attempts'][0]


@pytest.mark.parametrize('target', ['exception', 'response'])
def test_hostile_attribute_mapping_cannot_mask_cancellation(tmp_path, monkeypatch, target):
    def mutate(error):
        obj = error if target == 'exception' else error._llm_response
        obj.__dict__ = HostileDict(obj.__dict__)
    receipt, _ = run_http(tmp_path, monkeypatch, 'direct', mutate)
    assert_unknown(receipt['attempts'][0], 'CancelledError')
    assert 'timeout_observation' not in receipt['attempts'][0]


@pytest.mark.parametrize('sent,status,kind,refusal', [
    (None, None, None, None), (True, None, 'cancelled', None),
    (False, None, 'api_timeout', False), (True, 200, 'refusal', True),
    (True, 100, 'cancelled', False), (True, 599, 'cancelled', False),
])
def test_nullable_status_keeps_unknown_distinct_from_false(tmp_path, monkeypatch, sent, status, kind, refusal):
    def mutate(error):
        response = error._llm_response
        response.request_sent, response.http_status = sent, status
        response.transport_error_kind, response.refusal_detected = kind, refusal
    receipt, _ = run_http(tmp_path, monkeypatch, 'direct', mutate)
    assert_unknown(receipt['attempts'][0], 'CancelledError')
    assert receipt['attempts'][0].get('timeout_observation') == {
        'request_sent': sent, 'http_status': status,
        'transport_error_kind': kind, 'refusal_detected': refusal}


def test_missing_status_is_not_inferred_from_class_defaults(tmp_path, monkeypatch):
    receipt, _ = run_http(tmp_path, monkeypatch, 'direct',
                          lambda error: delattr(error._llm_response, 'request_sent'))
    assert_unknown(receipt['attempts'][0], 'CancelledError')
    assert 'timeout_observation' not in receipt['attempts'][0]


def test_success_and_usage_on_attachment_cannot_release_unknown_or_leak(tmp_path, monkeypatch):
    def mutate(error):
        response = error._llm_response
        response.success = response.usage_known = True
        response.billing_status = 'reported_usage'
        response.input_tokens = response.output_tokens = 987654321
        response.actual_incremental_cost_usd = 987654321
        response.content = response.error = response.reasoning_tokens = 'PRIVATE_SENTINEL'
        response.resolved_model = response.model
        response.model_identity_status = 'verified'
    receipt, _ = run_http(tmp_path, monkeypatch, 'outer', mutate)
    attempt = receipt['attempts'][0]
    assert_unknown(attempt, 'TimeoutError')
    assert attempt.get('timeout_observation') == {
        'request_sent': True, 'http_status': 200,
        'transport_error_kind': 'cancelled', 'refusal_detected': False}
    assert 'PRIVATE_SENTINEL' not in json.dumps(receipt)
    assert '987654321' not in json.dumps(receipt)


@pytest.mark.parametrize('chain', ['ordinary', 'timeout_attachment', 'deep', 'cycle', 'context'])
def test_unrecognized_exception_chains_do_not_supply_observations(tmp_path, monkeypatch, chain):
    response = LLMResponse('PRIVATE_SENTINEL', 'claude-opus-5-5', LLMProvider.ANTHROPIC,
                           requested_model='claude-opus-5-5', transport='anthropic_messages',
                           billing_channel='api', transport_error_kind='cancelled', http_status=200)
    cancelled = asyncio.CancelledError('PRIVATE_SENTINEL')
    cancelled._llm_response = response
    error = RuntimeError('PRIVATE_SENTINEL') if chain == 'ordinary' else TimeoutError('PRIVATE_SENTINEL')
    if chain in ('ordinary', 'timeout_attachment'):
        error._llm_response = response
    elif chain == 'deep':
        error.__cause__ = RuntimeError('PRIVATE_SENTINEL')
        error.__cause__.__cause__ = cancelled
    elif chain == 'cycle':
        error.__cause__ = error
    else:
        error.__context__ = cancelled
    def fail(response):
        raise error
    m, out, ledger, client, _, kwargs = setup_run(tmp_path, monkeypatch, fail)
    receipt = asyncio.run(m.run_diagnostic(**kwargs))
    assert_unknown(receipt['attempts'][0], type(error).__name__)
    assert 'timeout_observation' not in receipt['attempts'][0]
    assert reservations(ledger) == [('unknown',)] and len(client.calls) == 1
    assert 'PRIVATE_SENTINEL' not in (out / 'receipt.json').read_text()
