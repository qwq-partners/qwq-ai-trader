"""Offline transport contracts: real payload construction, usage and budget behavior."""
import json
from types import SimpleNamespace
from datetime import datetime, timedelta

import aiohttp
import pytest

from src.utils.llm import (
    GeminiClient, LLMConfig, LLMManager, LLMProvider, LLMTask, LLMUsage, OpenAIClient,
)


class Reply:
    def __init__(self, data, status=200):
        self.data, self.status = data, status
    async def __aenter__(self):
        return self
    async def __aexit__(self, *args):
        pass
    async def json(self):
        return self.data


class Session:
    closed = False
    def __init__(self, *replies):
        self.replies = list(replies)
        self.payloads = []
    def post(self, url, **kwargs):
        self.payloads.append(kwargs['json'])
        return self.replies.pop(0)


@pytest.mark.asyncio
@pytest.mark.parametrize('provider', [LLMProvider.OPENAI])
@pytest.mark.parametrize('failure', ['connect', 'socket', 'disconnect', 'timeout'])
async def test_only_proven_connection_failure_allows_light_fallback(provider, failure):
    class FailingConnection(Reply):
        async def __aenter__(self):
            if failure == 'connect':
                key = SimpleNamespace(host='example.invalid', port=443, is_ssl=True, ssl=True)
                raise aiohttp.ClientConnectorError(key, OSError('connection refused'))
            if failure == 'socket':
                raise aiohttp.ClientOSError('socket error with unknown send state')
            if failure == 'disconnect':
                raise aiohttp.ServerDisconnectedError()
            raise TimeoutError()

    manager = LLMManager(LLMConfig(openai_api_key='test', gemini_api_key='test'))
    task, primary, fallback, good_reply = LLMTask.MARKET_ANALYSIS, manager.openai, manager.openai, openai_reply()
    primary._session = Session(FailingConnection({}), good_reply)
    result = await manager.complete('prompt', task=task)
    if failure == 'connect':
        assert result.success
        assert len(fallback._session.payloads) == 2
        assert manager.daily_usage.unknown_usage_requests == 0
        assert manager._check_budget()
    else:
        assert not result.success
        assert result.request_sent
        assert result.billing_status == 'unknown'
        assert len(fallback._session.payloads) == 1
        assert manager.daily_usage.unknown_usage_requests == 1
        assert not manager._check_budget()


def openai_reply(content='ok', input_tokens=100, output_tokens=50, **extra):
    return Reply({'choices': [{'message': {'content': content}, 'finish_reason': 'stop'}],
                  'usage': {'prompt_tokens': input_tokens, 'completion_tokens': output_tokens,
                            'completion_tokens_details': {'reasoning_tokens': 30}}, **extra})


def gemini_reply(content='ok', **extra):
    return Reply({'candidates': [{'content': {'parts': [{'text': content}]}, 'finishReason': 'STOP'}],
                  'usageMetadata': {'promptTokenCount': 100, 'candidatesTokenCount': 20,
                                    'thoughtsTokenCount': 30}, **extra})


@pytest.mark.asyncio
@pytest.mark.parametrize('provider', [LLMProvider.OPENAI])
async def test_unreliable_usage_does_not_increase_known_cost_subtotal(provider, tmp_path):
    manager = LLMManager(LLMConfig(openai_api_key='test', gemini_api_key='test'), audit_dir=tmp_path)
    manager.daily_usage.add(100, 50, 'gpt-5-mini')
    known_cost = manager.daily_usage.estimated_cost_usd
    reply = openai_reply(output_tokens='malformed')
    manager.openai._session = Session(reply)
    response = await manager.complete_with('prompt', provider=provider)
    assert not response.usage_known
    assert manager.daily_usage.estimated_cost_usd == known_cost
    assert manager.daily_usage.cost_missing
    rows = [json.loads(line) for path in tmp_path.glob('llm-audit-*.jsonl') for line in path.read_text().splitlines()]
    assert rows[-1]['estimated_cost_usd'] is None


@pytest.mark.asyncio
@pytest.mark.parametrize('model,effort', [('gpt-5-mini', 'minimal'), ('gpt-5.6-sol', 'none'),
                                        ('gpt-6-astra', 'max'), ('gpt-6.1-sol', 'xhigh')])
async def test_openai_reasoning_payload_and_reported_metadata(model, effort):
    client = OpenAIClient(LLMConfig(openai_api_key='test-secret'))
    client._session = Session(openai_reply(model='server-snapshot'))
    result = await client.complete('private prompt', model=model, reasoning_effort=effort, max_tokens=200)
    body = client._session.payloads[0]
    assert body['reasoning_effort'] == effort
    assert body['max_completion_tokens'] == 200
    assert 'temperature' not in body
    assert result.model == model
    assert result.requested_model == model
    assert result.resolved_model == 'server-snapshot'
    assert result.requested_reasoning_effort == effort
    assert result.reasoning_tokens == 30
    assert result.output_tokens == 50  # OpenAI already includes reasoning.
    assert result.finish_reason == 'stop'


@pytest.mark.asyncio
@pytest.mark.parametrize('model,controls', [
    ('gpt-6-astra', {'reasoning_effort': 'minimal'}),
    ('gpt-6.1-sol', {'reasoning_effort': 'none'}),
    ('gpt-5.6-sol', {'reasoning_effort': 'minimal'}),
    ('gpt-5-mini', {'reasoning_effort': 'max'}),
    ('gpt-4o', {'reasoning_effort': 'high'}),
    ('gpt-6-astra', {'thinking_level': 'high'}),
])
async def test_openai_rejects_unsupported_controls_before_http(model, controls):
    client = OpenAIClient(LLMConfig(openai_api_key='test-secret'))
    client._session = Session(openai_reply())
    with pytest.raises(ValueError):
        await client.complete('prompt', model=model, **controls)
    assert client._session.payloads == []


@pytest.mark.asyncio
async def test_historical_gemini_visible_and_thought_tokens():
    from src.utils.llm import _gemini_usage
    data = gemini_reply().data
    usage = _gemini_usage(data, data['usageMetadata'])
    assert usage['usage_known']
    assert usage['visible_output_tokens'] == 20
    assert usage['reasoning_tokens'] == 30


@pytest.mark.asyncio
@pytest.mark.parametrize('controls', [{'reasoning_effort': 'high'}, {'thinking_level': 'nonsense'},
                                      {'thinking_level': 'high', 'thinking_budget': 100}])
async def test_gemini_rejects_ignored_controls(controls):
    client = GeminiClient(LLMConfig(gemini_api_key='test-secret'))
    client._session = Session(gemini_reply())
    result = await client.complete('prompt', **controls)
    assert result.provider_error_code == 'provider_disabled'
    assert not result.request_sent
    assert client._session.payloads == []


@pytest.mark.asyncio
async def test_absent_server_model_and_controls_remain_unknown():
    client = OpenAIClient(LLMConfig(openai_api_key='test-secret'))
    client._session = Session(openai_reply())
    response = await client.complete('prompt')
    assert response.resolved_model is None
    assert response.requested_reasoning_effort is None


@pytest.mark.parametrize('value', [True, -1, float('nan'), float('inf'), '3'])
def test_invalid_budget_rejected(value):
    with pytest.raises(ValueError):
        LLMConfig.from_config({'llm': {'daily_budget': value}})


def test_zero_budget_and_legacy_descriptive_routes():
    config = LLMConfig.from_config({'llm': {'daily_budget': 0, 'primary': 'made-up', 'fallback': 'made-up'}})
    manager = LLMManager(config)
    assert not manager._check_budget()
    assert manager._get_model(LLMProvider.OPENAI, 'heavy') == 'gpt-5.6-sol'
    assert manager.TASK_CONFIG[LLMTask.QUICK_CLASSIFY]['primary'] == (LLMProvider.OPENAI, 'light')


@pytest.mark.parametrize('model,expected', [('gpt-5-mini', 0.00225), ('gpt-5.6-sol', 0.025),
    ('gpt-6.1-sol', 0.0125), ('gpt-6-astra', 0.0625), ('gemini-3.1-pro-preview', 0.014),
    ('gemini-3.1-flash-lite', 0.00175), ('gemini-3.1-flash-lite-preview', 0.00175),
    ('gemini-3.8-flash', 0.009), ('gemini-4-argon', 0.024)])
def test_explicit_model_rates(model, expected):
    usage = LLMUsage()
    usage.add(1000, 1000, model)
    assert usage.estimated_cost_usd == pytest.approx(expected)
    assert not usage.cost_missing


@pytest.mark.parametrize('model,inputs,expected', [('gpt-6-astra', 272001, 6.875025),
    ('gemini-3.1-pro-preview', 200001, 0.818004)])
def test_long_context_prices(model, inputs, expected):
    usage = LLMUsage()
    usage.add(inputs, 1000, model)
    assert usage.estimated_cost_usd == pytest.approx(expected)


def test_unknown_price_is_flagged_and_closes_budget():
    manager = LLMManager(LLMConfig())
    manager.daily_usage.add(100, 100, 'unpriced-model')
    assert manager.daily_usage.cost_missing
    assert manager.daily_usage.unpriced_requests == 1
    assert not manager._check_budget()


@pytest.mark.asyncio
async def test_every_empty_retry_is_charged_and_audited(tmp_path):
    manager = LLMManager(LLMConfig(openai_api_key='secret-key'), audit_dir=tmp_path)
    manager.openai._session = Session(openai_reply(''), openai_reply('private response'))
    result = await manager.complete_with('private prompt', LLMProvider.OPENAI, retry_on_empty=1)
    assert result.content == 'private response'
    assert manager.daily_usage.input_tokens == 200
    assert manager.daily_usage.output_tokens == 100
    rows = [json.loads(line) for file in tmp_path.glob('*.jsonl') for line in file.read_text().splitlines()]
    assert [row['attempt_role'] for row in rows] == ['primary', 'empty_retry']
    assert [row['outcome'] for row in rows] == ['empty', 'success']
    assert all('private' not in json.dumps(row) and 'secret-key' not in json.dumps(row) for row in rows)


@pytest.mark.asyncio
async def test_empty_attempt_exhausting_budget_is_not_retried():
    manager = LLMManager(LLMConfig(openai_api_key='secret', daily_budget_usd=0.00001))
    manager.openai._session = Session(openai_reply(''), openai_reply('should not happen'))
    result = await manager.complete_with('prompt', LLMProvider.OPENAI, retry_on_empty=1)
    assert result.content == ''
    assert manager.daily_usage.input_tokens == 100
    assert len(manager.openai._session.payloads) == 1


@pytest.mark.asyncio
async def test_failed_primary_reported_usage_is_charged_before_fallback():
    manager = LLMManager(LLMConfig(openai_api_key='secret', gemini_api_key='secret'))
    manager.openai._session = Session(Reply({'error': {'message': 'private diagnostic'},
        'usage': {'prompt_tokens': 100, 'completion_tokens': 50}}, status=500))
    manager.gemini._session = Session(gemini_reply())
    manager.openai._session.replies.append(openai_reply())
    result = await manager.complete('prompt', task=LLMTask.MARKET_ANALYSIS)
    assert result.success
    assert manager.daily_usage.input_tokens == 200
    assert manager.daily_usage.output_tokens == 100


@pytest.mark.asyncio
async def test_missing_usage_is_unknown_and_blocks_further_requests():
    manager = LLMManager(LLMConfig(openai_api_key='secret'))
    manager.openai._session = Session(Reply({'choices': [{'message': {'content': 'ok'}}]}))
    await manager.complete_with('prompt', LLMProvider.OPENAI)
    assert manager.daily_usage.unknown_usage_requests == 1
    assert not manager._check_budget()


@pytest.mark.asyncio
async def test_audit_retention(tmp_path):
    old = tmp_path / ('llm-audit-' + (datetime.now() - timedelta(days=8)).strftime('%Y%m%d') + '.jsonl')
    old.write_text('old')
    manager = LLMManager(LLMConfig(openai_api_key='secret'), audit_dir=tmp_path)
    manager.openai._session = Session(openai_reply())
    await manager.complete_with('prompt', LLMProvider.OPENAI)
    assert not old.exists()


@pytest.mark.asyncio
async def test_legacy_openai_payload_keeps_temperature_without_reasoning():
    client = OpenAIClient(LLMConfig(openai_api_key='secret'))
    client._session = Session(openai_reply())
    await client.complete('prompt', model='gpt-4o', temperature=0.4)
    assert client._session.payloads[0]['temperature'] == 0.4
    assert 'reasoning_effort' not in client._session.payloads[0]


@pytest.mark.asyncio
async def test_no_openai_key_is_unsent_and_cannot_use_stale_gemini_key():
    manager = LLMManager(LLMConfig(gemini_api_key='secret'))
    manager.gemini._session = Session(gemini_reply())
    result = await manager.complete('prompt', task=LLMTask.MARKET_ANALYSIS)
    assert not result.success
    assert not result.request_sent
    assert manager.daily_usage.input_tokens == 0
    assert manager.daily_usage.unknown_usage_requests == 0


@pytest.mark.asyncio
async def test_failed_primary_with_explicit_zero_usage_can_fallback():
    manager = LLMManager(LLMConfig(openai_api_key='secret', gemini_api_key='secret'))
    manager.openai._session = Session(Reply({'error': {'message': 'rejected'},
        'usage': {'prompt_tokens': 0, 'completion_tokens': 0}}, status=400))
    manager.gemini._session = Session(gemini_reply())
    manager.openai._session.replies.append(openai_reply())
    assert (await manager.complete('prompt', task=LLMTask.MARKET_ANALYSIS)).success
    assert not manager.daily_usage.cost_missing


@pytest.mark.asyncio
async def test_shadow_is_gated_and_charged(tmp_path):
    from src.utils.llm import LLMResponse
    manager = LLMManager(LLMConfig(openai_api_key='secret'), audit_dir=tmp_path / 'audit')
    manager._SHADOW_LOG_DIR = tmp_path / 'shadow'
    manager.openai._session = Session(openai_reply())
    primary = LLMResponse('ok', 'gpt-5-mini', LLMProvider.OPENAI)
    await manager._fire_shadow('prompt', '', LLMTask.QUICK_CLASSIFY, {}, primary, 'gpt-6.1-sol')
    assert manager.daily_usage.estimated_cost_usd == pytest.approx(0.00075)
    manager.config.daily_budget_usd = 0
    await manager._fire_shadow('prompt', '', LLMTask.QUICK_CLASSIFY, {}, primary, 'gpt-6.1-sol')
    assert len(manager.openai._session.payloads) == 1


@pytest.mark.asyncio
async def test_malformed_paid_response_keeps_reported_usage():
    manager = LLMManager(LLMConfig(openai_api_key='secret'))
    manager.openai._session = Session(Reply({'choices': [{'message': 'bad shape'}],
        'usage': {'prompt_tokens': 100, 'completion_tokens': 50}}))
    result = await manager.complete_with('prompt', LLMProvider.OPENAI)
    assert not result.success
    assert manager.daily_usage.input_tokens == 100
    assert manager.daily_usage.output_tokens == 50


@pytest.mark.asyncio
async def test_timeout_has_unknown_usage_and_does_not_expose_url_key():
    import asyncio
    class TimeoutSession:
        closed = False
        def post(self, *args, **kwargs):
            raise asyncio.TimeoutError('https://provider?key=private-key')
    manager = LLMManager(LLMConfig(openai_api_key='private-key'))
    manager.openai._session = TimeoutSession()
    result = await manager.complete_with('prompt', LLMProvider.OPENAI)
    assert not result.success
    assert 'private-key' not in result.error
    assert manager.get_usage_summary()['budget_remaining_usd'] is None
    assert manager.daily_usage.unknown_usage_requests == 1


def test_intro_expiry_uses_post_promo_ceiling_and_preview_basis_explicit():
    from datetime import date
    from src.utils.llm_pricing import rate_quote
    assert rate_quote('gemini-3.8-flash', 100, 100, on_date=date(2027, 1, 1)).cost_usd == pytest.approx(0.0009)
    assert 'unverified_preview_alias' in rate_quote('gemini-3.1-flash-lite-preview', 1, 1).basis


def test_threshold_rate_is_not_long_until_exceeded():
    from src.utils.llm_pricing import rate_quote
    assert rate_quote('gpt-6-astra', 272000, 1000).cost_usd == pytest.approx(3.45)
    assert rate_quote('gemini-3.1-pro-preview', 200000, 1000).cost_usd == pytest.approx(0.412)


@pytest.mark.asyncio
async def test_gemini_38_is_disabled_regardless_of_controls():
    client = GeminiClient(LLMConfig(gemini_api_key='secret'))
    client._session = Session(gemini_reply())
    for level in ('low', 'minimal'):
        response = await client.complete('prompt', model='gemini-3.8-flash', thinking_level=level)
        assert response.provider_error_code == 'provider_disabled'
        assert not response.request_sent
    assert client._session.payloads == []


@pytest.mark.asyncio
async def test_budget_rejection_reports_unsent_request():
    manager = LLMManager(LLMConfig(daily_budget_usd=0))
    result = await manager.complete_with('prompt', LLMProvider.OPENAI)
    assert not result.request_sent
    assert not result.usage_known


@pytest.mark.asyncio
async def test_json_retry_attempts_are_charged_with_distinct_roles(tmp_path):
    manager = LLMManager(LLMConfig(openai_api_key='secret'), audit_dir=tmp_path / 'audit')
    manager._LLM_LOG_DIR = tmp_path / 'raw'
    manager.openai._session = Session(openai_reply('not json'), openai_reply('{"ok": true}'))
    assert await manager.complete_json('prompt', task=LLMTask.MARKET_ANALYSIS) == {'ok': True}
    assert manager.daily_usage.input_tokens == 200
    rows = [json.loads(line) for path in (tmp_path / 'audit').glob('*.jsonl') for line in path.read_text().splitlines()]
    assert [row['attempt_role'] for row in rows] == ['primary', 'json_retry']


@pytest.mark.asyncio
@pytest.mark.parametrize('provider', [LLMProvider.OPENAI])
async def test_elapsed_uses_monotonic_clock_not_wall_clock(monkeypatch, provider):
    import src.utils.llm as module
    class JumpingClock:
        values = iter([datetime(2026, 10, 12, 12), datetime(2026, 10, 12, 11)])
        @classmethod
        def now(cls):
            return next(cls.values)
    monkeypatch.setattr(module, 'datetime', JumpingClock)
    ticks = iter([42.0, 42.125])
    monkeypatch.setattr('time.perf_counter', lambda: next(ticks))
    config = LLMConfig(openai_api_key='secret', gemini_api_key='secret')
    client = OpenAIClient(config) if provider == LLMProvider.OPENAI else GeminiClient(config)
    client._session = Session(openai_reply() if provider == LLMProvider.OPENAI else gemini_reply())
    result = await client.complete('prompt')
    assert result.latency_ms == 125.0


@pytest.mark.asyncio
@pytest.mark.parametrize('status', [400, 401, 403, 404, 413, 422, 429])
async def test_structured_pre_generation_rejection_preserves_usage_and_auth_boundary(status, tmp_path):
    manager = LLMManager(LLMConfig(openai_api_key='secret', gemini_api_key='secret'), audit_dir=tmp_path)
    manager.openai._session = Session(Reply({'error': {'message': 'rejected', 'type': 'request_error'}}, status=status))
    manager.gemini._session = Session(gemini_reply())
    manager.openai._session.replies.append(openai_reply())
    result = await manager.complete('prompt', task=LLMTask.MARKET_ANALYSIS)
    if status in {401, 403}:
        assert not result.success and not manager.gemini._session.payloads
        assert manager.daily_usage.input_tokens == 0
    else:
        assert result.success
        assert manager.daily_usage.input_tokens == 100
    assert manager.daily_usage.inferred_nonbillable_rejections == 1
    assert not manager.daily_usage.cost_missing
    rows = [json.loads(line) for file in tmp_path.glob('*.jsonl') for line in file.read_text().splitlines()]
    assert rows[0]['billing_status'] == 'rejected_before_generation'
    assert rows[0]['cost_basis'] == 'pre_generation_rejection_inference_not_invoice_proof'
    assert rows[0]['input_tokens'] is None
    assert rows[0]['estimated_cost_usd'] is None
    assert not rows[0]['usage_known']


@pytest.mark.asyncio
async def test_gemini_disabled_never_falls_back_to_openai():
    manager = LLMManager(LLMConfig(openai_api_key='secret', gemini_api_key='secret'))
    manager.gemini._session = Session(Reply({'error': {'code': 429}}, status=429))
    manager.openai._session = Session(openai_reply())
    result = await manager.complete_with('prompt', LLMProvider.GEMINI)
    assert not result.success and result.provider_error_code == 'provider_disabled'
    assert manager.daily_usage.inferred_nonbillable_rejections == 0
    assert manager.daily_usage.output_tokens == 0
    assert manager.openai._session.payloads == manager.gemini._session.payloads == []


@pytest.mark.asyncio
@pytest.mark.parametrize('status,payload', [
    (500, {'error': {'message': 'unknown'}}),
    (429, {'error': {'message': 'unknown'}, 'usage': None}),
    (429, {'error': {'message': 'unknown'}, 'choices': [{'message': {'content': 'partial'}}]}),
    (429, {'error': 'malformed'}),
])
async def test_ambiguous_failure_still_blocks_fallback(status, payload):
    manager = LLMManager(LLMConfig(openai_api_key='secret', gemini_api_key='secret'))
    manager.openai._session = Session(Reply(payload, status=status))
    manager.gemini._session = Session(gemini_reply())
    manager.openai._session.replies.append(openai_reply())
    assert not (await manager.complete('prompt', task=LLMTask.MARKET_ANALYSIS)).success
    assert manager.daily_usage.inferred_nonbillable_rejections == 0
    assert manager.daily_usage.unknown_usage_requests == 1
    assert manager.gemini._session.payloads == []


@pytest.mark.asyncio
async def test_429_with_reported_usage_is_charged():
    manager = LLMManager(LLMConfig(openai_api_key='secret', gemini_api_key='secret'))
    manager.openai._session = Session(Reply({'error': {'message': 'rejected'},
        'usage': {'prompt_tokens': 100, 'completion_tokens': 50}}, status=429))
    manager.gemini._session = Session(gemini_reply())
    manager.openai._session.replies.append(openai_reply())
    assert (await manager.complete('prompt', task=LLMTask.MARKET_ANALYSIS)).success
    assert manager.daily_usage.input_tokens == 200
    assert manager.daily_usage.inferred_nonbillable_rejections == 0


@pytest.mark.asyncio
async def test_light_fallback_does_not_translate_unsupported_explicit_controls():
    manager = LLMManager(LLMConfig(openai_api_key='secret'))
    manager.openai._session = Session(Reply({'error': {'message': 'rejected'}}, status=429))
    with pytest.raises(ValueError, match='reasoning_effort'):
        await manager.complete('prompt', task=LLMTask.MARKET_ANALYSIS, reasoning_effort='none')
    assert len(manager.openai._session.payloads) == 1
    assert manager.daily_usage.inferred_nonbillable_rejections == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('provider', [LLMProvider.OPENAI])
async def test_caller_timeout_accounts_sent_cancelled_attempt_and_propagates(provider, tmp_path):
    import asyncio
    class PendingReply:
        async def __aenter__(self):
            await asyncio.Event().wait()
        async def __aexit__(self, *args):
            pass
    manager = LLMManager(LLMConfig(openai_api_key='secret', gemini_api_key='secret'), audit_dir=tmp_path)
    client = manager._get_client(provider)
    client._session = Session(PendingReply())
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(manager.complete_with('private prompt', provider), timeout=0.01)
    assert manager.daily_usage.unknown_usage_requests == 1
    assert manager.daily_usage.cost_missing
    assert len(client._session.payloads) == 1
    rows = [json.loads(line) for path in tmp_path.glob('*.jsonl') for line in path.read_text().splitlines()]
    assert len(rows) == 1
    assert rows[0]['billing_status'] == 'unknown'
    assert rows[0]['outcome'] == 'error'


@pytest.mark.asyncio
async def test_cancellation_before_send_preserved_without_unknown_charge(monkeypatch):
    import asyncio
    manager = LLMManager(LLMConfig(openai_api_key='secret'))
    async def pending_session():
        await asyncio.Event().wait()
    monkeypatch.setattr(manager.openai, '_get_session', pending_session)
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(manager.complete_with('prompt', LLMProvider.OPENAI), timeout=0.01)
    assert not manager.daily_usage.cost_missing
    assert manager.daily_usage.unknown_usage_requests == 0


@pytest.mark.asyncio
@pytest.mark.parametrize('role', ['primary', 'fallback', 'shadow'])
async def test_cancellation_is_accounted_once_across_manager_paths(role, tmp_path):
    import asyncio
    from src.utils.llm import LLMResponse
    class PendingReply:
        async def __aenter__(self):
            await asyncio.Event().wait()
        async def __aexit__(self, *args):
            pass
    manager = LLMManager(LLMConfig(openai_api_key='secret', gemini_api_key='secret'), audit_dir=tmp_path)
    if role == 'fallback':
        manager.openai._session = Session(Reply({'error': {'message': 'rejected'}}, status=429), PendingReply())
    else:
        manager.openai._session = Session(PendingReply())
        manager.gemini._session = Session(gemini_reply())
    manager.openai._session.replies.append(openai_reply())
    if role == 'shadow':
        call = manager._fire_shadow('prompt', '', LLMTask.QUICK_CLASSIFY, {},
            LLMResponse('ok', 'gpt-5-mini', LLMProvider.OPENAI), 'gpt-6.1-sol')
    else:
        call = manager.complete('prompt', task=LLMTask.MARKET_ANALYSIS)
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(call, timeout=0.01)
    assert manager.daily_usage.unknown_usage_requests == 1
    assert manager.daily_usage.cost_missing
    rows = [json.loads(line) for path in tmp_path.glob('*.jsonl') for line in path.read_text().splitlines()]
    assert len(rows) == (2 if role == 'fallback' else 1)
    assert rows[-1]['attempt_role'] == role
    if role == 'primary':
        assert manager.gemini._session.payloads == []


def test_mini_does_not_inherit_flagship_long_context_surcharge():
    from src.utils.llm_pricing import rate_quote
    quote = rate_quote('gpt-5-mini', 300_000, 1000)
    assert quote.input_usd_per_million == 0.25
    assert quote.output_usd_per_million == 2.0
    assert quote.cost_usd == pytest.approx(0.077)


def test_preflight_warns_about_priced_model_in_wrong_provider_slot():
    manager = LLMManager(LLMConfig(openai_model_light='gemini-3.1-flash-lite'))
    assert 'openai_model_light:unsupported_provider_controls' in manager.configuration_warnings


@pytest.mark.asyncio
@pytest.mark.parametrize('payload,outputs', [
    ({'usageMetadata': {'promptTokenCount': 10, 'thoughtsTokenCount': 25, 'totalTokenCount': 35},
      'candidates': [{'content': {'parts': []}, 'finishReason': 'MAX_TOKENS'}]}, 25),
    ({'usageMetadata': {'promptTokenCount': 10, 'totalTokenCount': 10},
      'promptFeedback': {'blockReason': 'SAFETY'}}, 0),
])
async def test_gemini_proto_omitted_zero_output_counts_remain_known(payload, outputs):
    from src.utils.llm import _gemini_usage
    response = _gemini_usage(payload, payload['usageMetadata'])
    assert response['usage_known']
    assert response['visible_output_tokens'] + response['reasoning_tokens'] == outputs
    assert response['visible_output_tokens'] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize('counts', [
    {'promptTokenCount': 10, 'thoughtsTokenCount': 25, 'candidatesTokenCount': None},
    {'promptTokenCount': 10, 'thoughtsTokenCount': 25, 'totalTokenCount': 99},
    {'promptTokenCount': 10, 'candidatesTokenCount': 5, 'totalTokenCount': 16},
])
async def test_gemini_malformed_or_inconsistent_usage_stays_unknown(counts):
    from src.utils.llm import _gemini_usage
    assert not _gemini_usage({'usageMetadata': counts, 'candidates': []}, counts)['usage_known']


@pytest.mark.asyncio
async def test_bounded_provider_error_metadata_is_audited_without_arbitrary_text(tmp_path):
    manager = LLMManager(LLMConfig(openai_api_key='secret'), audit_dir=tmp_path)
    manager.openai._session = Session(Reply({'error': {'code': 'invalid_api_key',
        'type': 'private-account-id', 'message': 'secret message'}}, status=401))
    response = await manager.complete_with('prompt', LLMProvider.OPENAI)
    assert response.http_status == 401
    assert response.provider_error_code == 'invalid_api_key'
    assert response.provider_error_type == 'unknown_provider_error'
    rows = [json.loads(line) for p in tmp_path.glob('*.jsonl') for line in p.read_text().splitlines()]
    assert rows[0]['provider_error_code'] == 'invalid_api_key'
    assert 'private-account-id' not in json.dumps(rows)
    assert 'secret message' not in json.dumps(rows)


@pytest.mark.asyncio
@pytest.mark.parametrize('seed', [[], {}, True, 1.5, '42'])
async def test_invalid_seed_is_local_value_error(seed):
    client = OpenAIClient(LLMConfig(openai_api_key='secret'))
    client._session = Session(openai_reply())
    with pytest.raises(ValueError):
        await client.complete('prompt', seed=seed)
    assert client._session.payloads == []


def test_model_preflight_reports_static_warnings_without_config_content():
    manager = LLMManager(LLMConfig(openai_model_heavy='private-secret-value',
        gemini_model_light=['bad'], openai_model_light_shadow='unknown-model'))
    warnings = manager.get_usage_summary()['configuration_warnings']
    assert 'openai_model_heavy:unpriced_model' in warnings
    assert all(not warning.startswith('gemini_') for warning in warnings)
    assert 'openai_model_light_shadow:unpriced_model' in warnings
    assert 'private-secret-value' not in json.dumps(warnings)


@pytest.mark.asyncio
async def test_unknown_primary_skip_is_counted_as_error():
    manager = LLMManager(LLMConfig(openai_api_key='secret', gemini_api_key='secret'))
    manager.openai._session = Session(Reply({'error': {'code': 'server_error'}}, status=500))
    await manager.complete('prompt', task=LLMTask.MARKET_ANALYSIS)
    assert manager.stats['error_count'] == 1
    assert manager.stats['fallback_skipped_unknown_cost'] == 1


@pytest.mark.asyncio
async def test_private_attempt_helper_does_not_capture_public_task_role_kwargs():
    manager = LLMManager(LLMConfig(openai_api_key='secret'))
    manager.openai._session = Session(openai_reply())
    response = await manager.complete_with('prompt', LLMProvider.OPENAI, task='caller-task', role='caller-role')
    assert response.success


@pytest.mark.asyncio
async def test_shadow_schedule_strong_reference_and_close_accounts_cancellation(tmp_path):
    import asyncio
    from src.utils.llm import LLMResponse
    started = asyncio.Event()
    class PendingReply:
        async def __aenter__(self):
            started.set()
            await asyncio.Event().wait()
        async def __aexit__(self, *args):
            pass
    class ClosableSession(Session):
        async def close(self):
            assert manager.daily_usage.unknown_usage_requests == 1
            self.closed = True
    manager = LLMManager(LLMConfig(openai_api_key='secret', openai_model_light_shadow='gpt-6.1-sol'), audit_dir=tmp_path)
    manager.openai._session = ClosableSession(PendingReply())
    manager._maybe_fire_shadow('prompt', '', LLMTask.QUICK_CLASSIFY, {}, LLMResponse('ok', 'gpt-5-mini', LLMProvider.OPENAI))
    await asyncio.wait_for(started.wait(), timeout=1)
    assert len(manager._shadow_tasks) == 1
    await manager.close()
    assert not manager._shadow_tasks
    assert manager.daily_usage.unknown_usage_requests == 1
    assert manager.openai._session.closed


@pytest.mark.asyncio
async def test_audit_cleanup_failure_does_not_drop_current_record(tmp_path, monkeypatch):
    from pathlib import Path
    old = tmp_path / 'llm-audit-20000101.jsonl'
    unrelated = tmp_path / '20000101.jsonl'
    old.write_text('old')
    unrelated.write_text('unrelated')
    original = Path.unlink
    def fail_old(path, *args, **kwargs):
        if path == old:
            raise OSError('failed cleanup')
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'unlink', fail_old)
    manager = LLMManager(LLMConfig(openai_api_key='secret'), audit_dir=tmp_path)
    manager.openai._session = Session(openai_reply())
    await manager.complete_with('prompt', LLMProvider.OPENAI)
    assert unrelated.read_text() == 'unrelated'
    current = list(tmp_path.glob('llm-audit-20*.jsonl'))
    assert len(current) == 2
    assert any('requested_model' in path.read_text() for path in current)


@pytest.mark.asyncio
@pytest.mark.parametrize('provider', [LLMProvider.OPENAI])
@pytest.mark.parametrize('kind', ['list_json', 'html'])
async def test_non_object_or_html_response_is_unknown_and_safe(provider, kind):
    class HtmlReply(Reply):
        async def json(self):
            raise ValueError('<html>private-provider-page</html>')
    manager = LLMManager(LLMConfig(openai_api_key='secret', gemini_api_key='secret'))
    manager._get_client(provider)._session = Session(Reply([]) if kind == 'list_json' else HtmlReply({}))
    response = await manager.complete_with('prompt', provider)
    assert not response.success
    assert manager.daily_usage.unknown_usage_requests == 1
    assert 'private-provider-page' not in response.error


@pytest.mark.asyncio
async def test_openai_non_text_content_is_failed_but_reported_usage_preserved():
    client = OpenAIClient(LLMConfig(openai_api_key='secret'))
    client._session = Session(openai_reply(content=['not text']))
    response = await client.complete('prompt')
    assert not response.success
    assert response.content == ''
    assert response.usage_known
    assert response.output_tokens == 50


@pytest.mark.asyncio
async def test_openai_bad_optional_reasoning_detail_keeps_billable_totals():
    client = OpenAIClient(LLMConfig(openai_api_key='secret'))
    client._session = Session(Reply({'choices': [{'message': {'content': 'ok'}}],
        'usage': {'prompt_tokens': 100, 'completion_tokens': 50, 'completion_tokens_details': 'malformed'}}))
    response = await client.complete('prompt')
    assert response.usage_known
    assert response.output_tokens == 50
    assert response.reasoning_tokens is None


@pytest.mark.asyncio
async def test_actual_shadow_schedule_completion_releases_reference(tmp_path):
    import asyncio
    from src.utils.llm import LLMResponse
    manager = LLMManager(LLMConfig(openai_api_key='secret', openai_model_light_shadow='gpt-6.1-sol'), audit_dir=tmp_path / 'audit')
    manager._SHADOW_LOG_DIR = tmp_path / 'shadow'
    manager.openai._session = Session(openai_reply())
    manager._maybe_fire_shadow('prompt', '', LLMTask.QUICK_CLASSIFY, {}, LLMResponse('ok', 'gpt-5-mini', LLMProvider.OPENAI))
    assert len(manager._shadow_tasks) == 1
    await asyncio.gather(*manager._shadow_tasks)
    assert not manager._shadow_tasks
    assert manager.daily_usage.input_tokens == 100
    assert len(list((tmp_path / 'shadow').glob('*.jsonl'))) == 1


@pytest.mark.asyncio
async def test_budget_exhausted_failed_primary_has_explicit_skip_counter():
    manager = LLMManager(LLMConfig(openai_api_key='secret', daily_budget_usd=0.00001))
    manager.openai._session = Session(Reply({'error': {'code': 'server_error'},
        'usage': {'prompt_tokens': 100, 'completion_tokens': 50}}, status=500))
    await manager.complete('prompt', task=LLMTask.MARKET_ANALYSIS)
    assert manager.stats['error_count'] == 1
    assert manager.stats['fallback_skipped_budget'] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('reason', ['BLOCK_REASON_UNSPECIFIED', 'unrecognized-provider-value'])
async def test_unspecified_block_reason_does_not_prove_zero_generation(reason):
    from src.utils.llm import _gemini_usage
    payload = {'usageMetadata': {'promptTokenCount': 10}, 'promptFeedback': {'blockReason': reason}}
    assert not _gemini_usage(payload, payload['usageMetadata'])['usage_known']

@pytest.mark.parametrize('value', [
    {'x': '```ok```'},
    {'x': '```null```'},
    ['```null```', {'x': 'escaped "quote" and \\ slash ```ok```'}],
])
def test_valid_json_preserves_fences_inside_strings(value):
    from src.utils.llm import _extract_json
    assert _extract_json(json.dumps(value)) == value


@pytest.mark.parametrize('text,expected', [
    ('```json\n{"x": 1}\n```', {'x': 1}),
    ('```json\n[1, 2]\n```', [1, 2]),
    ('Here is the result: {"x": 1}', {'x': 1}),
    ('Result: [1, 2]', [1, 2]),
    ('```json\n{"x": 1}\n``` then ```json\n{"x": 2}\n```', {'x': 1}),
    ('null', None), ('42', 42), ('"value"', 'value'), ('false', False),
])
def test_json_fallback_and_scalar_contract(text, expected):
    from src.utils.llm import _extract_json
    assert _extract_json(text) == expected


@pytest.mark.parametrize('text', ['', '{"x":', '{"x": 1} trailing',
    '```json\nnot json\n``` then ```json\n{"x": 2}\n```'])
def test_invalid_json_does_not_recover_or_skip_first_block(text):
    from src.utils.llm import _extract_json
    with pytest.raises(json.JSONDecodeError):
        _extract_json(text)


@pytest.mark.asyncio
async def test_manager_valid_json_with_internal_fence_needs_one_request(tmp_path):
    manager = LLMManager(LLMConfig(openai_api_key='synthetic'), audit_dir=tmp_path / 'audit')
    manager._LLM_LOG_DIR = tmp_path / 'raw'
    manager.openai._session = Session(openai_reply('{"x":"```null```"}'))
    assert await manager.complete_json('prompt', task=LLMTask.MARKET_ANALYSIS) == {'x': '```null```'}
    assert len(manager.openai._session.payloads) == 1
    assert manager.daily_usage.input_tokens == 100
    rows = [json.loads(line) for path in (tmp_path / 'audit').glob('*.jsonl')
            for line in path.read_text().splitlines()]
    assert [row['attempt_role'] for row in rows] == ['primary']


@pytest.mark.asyncio
async def test_direct_openai_json_with_internal_fence_needs_one_request():
    client = OpenAIClient(LLMConfig(openai_api_key='synthetic'))
    client._session = Session(openai_reply('{"x":"```ok```"}'))
    assert await client.complete_json('prompt') == {'x': '```ok```'}
    assert len(client._session.payloads) == 1
