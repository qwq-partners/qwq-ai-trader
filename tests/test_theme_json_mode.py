"""Offline JSON-mode contract through the KR detector and real manager/transport."""
import pytest

from src.signals.sentiment.kr_theme_detector import NewsArticle, ThemeDetector
from src.utils.llm import LLMConfig, LLMManager, LLMTask, OpenAIClient, _extract_json
from test_llm_transport_contract import Reply, Session, openai_reply


def manager_for(tmp_path, *replies):
    manager = LLMManager(LLMConfig(openai_api_key='synthetic', missing_cost_policy='log_and_continue'), audit_dir=tmp_path / 'audit')
    manager._LLM_LOG_DIR = tmp_path / 'responses'
    manager.openai._session = Session(*replies)
    return manager


async def detect(manager):
    return await ThemeDetector(llm_manager=manager)._extract_themes_from_news(
        [NewsArticle(title='합성 뉴스', content='테스트 요약')])


@pytest.mark.asyncio
async def test_detector_requests_json_mode_and_preserves_controls(tmp_path):
    manager = manager_for(tmp_path, openai_reply('{"themes": ["synthetic"], "stock_impacts": []}'))
    assert await detect(manager) == {'themes': ['synthetic'], 'stock_impacts': []}
    body = manager.openai._session.payloads[0]
    assert body['response_format'] == {'type': 'json_object'}
    assert body['model'] == 'gpt-5-mini'
    assert body['max_completion_tokens'] == 16000
    assert body['reasoning_effort'] == 'minimal'
    assert len(body['messages']) == 1 and body['messages'][0]['role'] == 'user'
    assert '합성 뉴스' in body['messages'][0]['content']
    assert '다음 JSON 형식으로만 응답하세요:' in body['messages'][0]['content']


@pytest.mark.asyncio
@pytest.mark.parametrize('json_call', [False, True])
async def test_other_calls_omit_response_format(tmp_path, json_call):
    manager = manager_for(tmp_path, openai_reply('{"ok": true}'))
    if json_call:
        assert await manager.complete_json('JSON please', LLMTask.THEME_DETECTION) == {'ok': True}
    else:
        assert (await manager.complete('plain', LLMTask.THEME_DETECTION)).success
    assert 'response_format' not in manager.openai._session.payloads[0]


@pytest.mark.asyncio
@pytest.mark.parametrize('second,expected', [('{"themes": ["repaired"], "stock_impacts": []}', ['repaired']), ('{"n": +2}', [])])
async def test_invalid_numeric_json_gets_only_one_repair_with_mode(tmp_path, second, expected):
    manager = manager_for(tmp_path, openai_reply('{"n": +1}'), openai_reply(second))
    result = await detect(manager)
    assert result == {'themes': expected, 'stock_impacts': []}
    assert len(manager.openai._session.payloads) == 2
    assert all(p['response_format'] == {'type': 'json_object'} for p in manager.openai._session.payloads)
    assert manager.openai._session.payloads[1]['messages'][0]['content'].startswith(manager.openai._session.payloads[0]['messages'][0]['content'])


@pytest.mark.asyncio
async def test_invalid_json_with_unknown_usage_does_not_retry(tmp_path):
    reply = openai_reply('{"n": +1}')
    reply.data.pop('usage')
    manager = manager_for(tmp_path, reply)
    assert await detect(manager) == {'themes': [], 'stock_impacts': []}
    assert len(manager.openai._session.payloads) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['http400', 'refusal', 'length', 'content_filter', 'empty', 'whitespace', 'missing_finish'])
async def test_failed_mode_response_never_retries_or_uses_another_provider(tmp_path, failure):
    reply = openai_reply('{"themes": ["synthetic"], "stock_impacts": []}')
    if failure == 'http400':
        reply = Reply({'error': {'message': 'unsupported format'}}, status=400)
    elif failure == 'refusal':
        reply.data['choices'][0]['message']['refusal'] = 'refused'
    elif failure in {'empty', 'whitespace'}:
        reply.data['choices'][0]['message']['content'] = '' if failure == 'empty' else '  '
    elif failure == 'missing_finish':
        reply.data['choices'][0].pop('finish_reason')
    else:
        reply.data['choices'][0]['finish_reason'] = failure
    manager = manager_for(tmp_path, reply)
    assert await detect(manager) == {'themes': [], 'stock_impacts': []}
    assert len(manager.openai._session.payloads) == 1
    assert manager.openai._session.payloads[0]['response_format'] == {'type': 'json_object'}
    if failure != 'http400':
        assert manager.daily_usage.input_tokens == 100
        assert manager.daily_usage.output_tokens == 50


@pytest.mark.asyncio
@pytest.mark.parametrize('value', [{}, {'type': 'json_schema'}, {'type': 'json_object', 'extra': 1}, [], 'json_object', False])
async def test_invalid_response_format_rejected_before_credentials_or_http(value):
    client = OpenAIClient(LLMConfig())
    client._session = Session()
    with pytest.raises(ValueError, match='response_format'):
        await client.complete('JSON', response_format=value)
    assert not client._session.payloads


def test_existing_parser_supports_wrapped_json_but_rejects_plus_numbers():
    assert _extract_json('```json\n{"n": 1}\n```') == {'n': 1}
    with pytest.raises(ValueError):
        _extract_json('{"n": +1}')


@pytest.mark.asyncio
async def test_mode_failure_does_not_trigger_configured_model_fallback(tmp_path):
    manager = manager_for(tmp_path, Reply({}, status=400), openai_reply('{"ok": true}'))
    result = await manager.complete_json('JSON', LLMTask.MARKET_ANALYSIS, response_format={'type': 'json_object'})
    assert 'error' in result
    assert len(manager.openai._session.payloads) == 1


@pytest.mark.asyncio
async def test_non_opted_transport_preserves_historical_incomplete_success():
    reply = openai_reply('{"ok": true}')
    reply.data['choices'][0]['finish_reason'] = 'length'
    client = OpenAIClient(LLMConfig(openai_api_key='synthetic'))
    client._session = Session(reply)
    result = await client.complete('ordinary')
    assert result.success and result.finish_reason == 'length'
    assert 'response_format' not in client._session.payloads[0]
