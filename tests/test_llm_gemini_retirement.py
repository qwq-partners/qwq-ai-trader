"""Gemini retirement stays enforced with stale configuration and direct clients."""
import json
from unittest.mock import Mock

import pytest
from src.utils.llm import GeminiClient, LLMConfig, LLMManager, LLMProvider, LLMTask
from tests.test_llm_transport_contract import Session, Reply, openai_reply


@pytest.mark.asyncio
@pytest.mark.parametrize('task', list(LLMTask))
async def test_general_task_uses_only_openai(task):
    manager = LLMManager(LLMConfig(openai_api_key='synthetic', gemini_api_key='stale'))
    manager.openai._session = Session(openai_reply())
    manager.gemini._session = Session()
    response = await manager.complete('public synthetic input', task=task)
    assert response.success and response.provider == LLMProvider.OPENAI
    assert len(manager.openai._session.payloads) == 1
    assert manager.gemini._session.payloads == []


@pytest.mark.asyncio
@pytest.mark.parametrize('heavy,same_model,expected', [(False, False, 1), (True, False, 2), (True, True, 1)])
async def test_failure_has_at_most_one_distinct_light_fallback(heavy, same_model, expected):
    config = LLMConfig(openai_api_key='synthetic')
    if same_model:
        config.openai_model_heavy = config.openai_model_light
    manager = LLMManager(config)
    manager.openai._session = Session(Reply({'error': {'code': 'rate_limit_exceeded'}}, 429), openai_reply())
    response = await manager.complete('input', task=LLMTask.MARKET_ANALYSIS if heavy else LLMTask.QUICK_CLASSIFY)
    payloads = manager.openai._session.payloads
    assert len(payloads) == expected
    assert response.success == (expected == 2)
    if expected == 2:
        assert [p['model'] for p in payloads] == [config.openai_model_heavy, config.openai_model_light]
        assert [r['role'] for r in response.attempt_trail] == ['primary', 'fallback']


@pytest.mark.asyncio
async def test_raw_gemini_never_constructs_session_even_with_stale_key(monkeypatch):
    constructor = Mock(side_effect=AssertionError('session creation forbidden'))
    monkeypatch.setattr('src.utils.llm.aiohttp.ClientSession', constructor)
    client = GeminiClient(LLMConfig(gemini_api_key='stale'))
    response = await client.complete('input', model='gemini-stale', reasoning_effort='ignored')
    assert not response.success and response.provider_error_code == 'provider_disabled'
    assert not response.request_sent and response.billing_status == 'not_sent'
    constructor.assert_not_called()
    assert await client.complete_json('input') == {'error': 'Provider disabled by user policy'}


@pytest.mark.asyncio
async def test_manager_blocks_gemini_before_budget_or_client(tmp_path, monkeypatch):
    manager = LLMManager(LLMConfig(gemini_api_key='stale', monthly_budget_usd=100,
        budget_state_path=str(tmp_path / 'budget.sqlite3')))
    monkeypatch.setattr(manager.monthly_budget, 'reserve', Mock(side_effect=AssertionError('reservation forbidden')))
    monkeypatch.setattr(manager.gemini, 'complete', Mock(side_effect=AssertionError('client forbidden')))
    response = await manager.complete_with('input', LLMProvider.GEMINI, retry_on_empty=3)
    assert response.provider_error_code == 'provider_disabled'
    assert not response.request_sent and response.billing_status == 'not_sent'
    assert response.budget_reservation_token is None
    assert len(response.attempt_trail) == 1


def test_env_and_yaml_cannot_reactivate_gemini(monkeypatch):
    monkeypatch.setenv('GEMINI_API_KEY', 'stale')
    for config in [LLMConfig.from_env(), LLMConfig.from_config({'gemini_model_light': 'stale'})]:
        assert not config.gemini_api_key
        assert config.gemini_model_light != 'stale'


def test_enabled_gemini_manifest_rejected_before_manager_and_clients(tmp_path, monkeypatch):
    from src.analytics import model_comparison_worker as worker
    manifest = tmp_path / 'manifest.json'
    manifest.write_text(json.dumps({'enabled': True, 'execution_policy': {'allowed_models': ['gpt-5-mini', 'gemini-3.1-flash-lite']}}))
    clients = Mock(side_effect=AssertionError('clients forbidden'))
    manager = Mock(side_effect=AssertionError('ledger access forbidden'))
    monkeypatch.setattr(worker, '_raw_clients', clients)
    monkeypatch.setattr('src.utils.llm.get_llm_manager', manager)
    assert worker.install_from_env(object(), env={'COMPARISON_WORKER': '1', 'COMPARISON_EXPERIMENT_MANIFEST': str(manifest)}) is None
    clients.assert_not_called()
    manager.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize('explicit', [None, 'high'])
async def test_common_light_uses_minimal_unless_caller_supplies_effort(explicit):
    manager = LLMManager(LLMConfig(openai_api_key='synthetic'))
    manager.openai._session = Session(openai_reply())
    kwargs = {} if explicit is None else {'reasoning_effort': explicit}
    response = await manager.complete('input', task=LLMTask.QUICK_CLASSIFY, **kwargs)
    assert manager.openai._session.payloads[0]['reasoning_effort'] == (explicit or 'minimal')
    assert response.requested_reasoning_effort == (explicit or 'minimal')


@pytest.mark.asyncio
async def test_heavy_fallback_uses_light_minimal_effort_only_on_second_attempt():
    manager = LLMManager(LLMConfig(openai_api_key='synthetic'))
    manager.openai._session = Session(Reply({'error': {'code': 'rate_limit_exceeded'}}, 429), openai_reply())
    response = await manager.complete('input', task=LLMTask.MARKET_ANALYSIS)
    assert response.success
    first, second = manager.openai._session.payloads
    assert 'reasoning_effort' not in first
    assert second['reasoning_effort'] == 'minimal'


@pytest.mark.asyncio
async def test_configured_light_without_minimal_uses_supported_low():
    manager = LLMManager(LLMConfig(openai_api_key='synthetic', openai_model_light='gpt-6.1-sol'))
    manager.openai._session = Session(openai_reply())
    response = await manager.complete('input')
    assert response.success and response.requested_reasoning_effort == 'low'
    assert manager.openai._session.payloads[0]['reasoning_effort'] == 'low'


def test_comparison_raw_clients_require_a_replacement_independent_provider(monkeypatch):
    from src.analytics import model_comparison_worker as worker
    constructor = Mock(side_effect=AssertionError('client construction forbidden'))
    monkeypatch.setattr('src.utils.llm.GeminiClient', constructor)
    monkeypatch.setattr('src.utils.llm.OpenAIClient', constructor)
    with pytest.raises(ValueError, match='provider_disabled'):
        worker._raw_clients()
    constructor.assert_not_called()


@pytest.mark.asyncio
async def test_resolved_primary_identity_cannot_be_retried_as_light_alias():
    manager = LLMManager(LLMConfig(openai_api_key='synthetic'))
    manager.openai._session = Session(Reply({'error': {'code': 'rate_limit_exceeded'},
        'model': 'gpt-5-mini'}, 429), openai_reply())
    response = await manager.complete('input', task=LLMTask.MARKET_ANALYSIS)
    assert not response.success
    assert len(manager.openai._session.payloads) == 1
    assert response.resolved_model == 'gpt-5-mini'
