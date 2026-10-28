"""Real transport/manager/SQLite behavior; only HTTP is replaced offline."""
import json
import sqlite3

import pytest

from src.utils.llm import LLMConfig, LLMManager, LLMProvider, LLMTask
from test_llm_transport_contract import Reply, Session, openai_reply, gemini_reply


class TimeoutReply(Reply):
    async def __aenter__(self):
        raise TimeoutError()


def manager_for(tmp_path, **overrides):
    config = dict(openai_api_key='offline', gemini_api_key='offline',
                  monthly_budget_usd=100, budget_state_path=str(tmp_path / 'budget.sqlite3'))
    config.update(overrides)
    manager = LLMManager(LLMConfig(**config), audit_dir=tmp_path / 'audit')
    manager._LLM_LOG_DIR = tmp_path / 'responses'
    return manager


@pytest.mark.asyncio
async def test_historical_timeout_hold_survives_distinct_gpt_fallback(tmp_path):
    manager = manager_for(tmp_path)
    token = manager.monthly_budget.reserve('gemini-3.1-flash-lite', 'historical', '', 100,
        timeout_hold_contract='gemini-flash-lite-standard-text-v1')
    manager.monthly_budget.settle(token, outcome='timeout')
    manager.openai._session = Session(Reply({'error': {'code': 'rate_limit_exceeded'}}, 429), openai_reply())
    result = await manager.complete('news', task=LLMTask.MARKET_ANALYSIS, max_tokens=100)
    assert result.success and result.provider == LLMProvider.OPENAI
    assert len(result.attempt_trail) == 2
    first, second = result.attempt_trail
    assert first['billing_status'] == 'rejected_before_generation'
    assert first['budget_reservation_token'] != second['budget_reservation_token']
    with sqlite3.connect(tmp_path / 'budget.sqlite3') as db:
        assert db.execute('SELECT state,actual_units FROM reservations WHERE token=?', (token,)).fetchone() == ('unknown', None)
    status = manager.monthly_budget.status()
    assert status['unresolved_count'] == 1 and not status['blocked']
    assert status['uncertain_cost_ceiling_usd'] == pytest.approx(0.262294)
    rows = [json.loads(line) for p in (tmp_path / 'audit').glob('*.jsonl') for line in p.read_text().splitlines()]
    assert len(rows) == 2 and rows[0]['estimated_cost_usd'] is None


@pytest.mark.asyncio
async def test_unknown_timeout_cannot_start_fallback_or_json_repair(tmp_path):
    manager = manager_for(tmp_path)
    manager.openai._session = Session(TimeoutReply({}), openai_reply('{}'))
    result = await manager.complete_json('news', task=LLMTask.MARKET_ANALYSIS, max_tokens=100)
    assert result['error']
    assert len(manager.openai._session.payloads) == 1
    assert manager.monthly_budget.status()['block_reason'] == 'unresolved_usage'


@pytest.mark.asyncio
async def test_json_repair_timeout_cannot_start_third_provider_attempt(tmp_path):
    manager = manager_for(tmp_path)
    manager.openai._session = Session(openai_reply('not json'), TimeoutReply({}), openai_reply('{}'))
    result = await manager.complete_json('news', task=LLMTask.MARKET_ANALYSIS, max_tokens=100)
    assert result['error']
    assert len(manager.openai._session.payloads) == 2


@pytest.mark.asyncio
async def test_timeout_while_reading_permission_error_does_not_authorize_fallback(tmp_path):
    class PermissionReply(Reply):
        async def json(self):
            raise TimeoutError()
    manager = manager_for(tmp_path)
    manager.openai._session = Session(PermissionReply({}, status=403), openai_reply())
    result = await manager.complete('news', task=LLMTask.MARKET_ANALYSIS, max_tokens=100)
    assert not result.success and len(manager.openai._session.payloads) == 1
    assert manager.monthly_budget.status()['block_reason'] == 'unresolved_usage'


@pytest.mark.asyncio
@pytest.mark.parametrize('reported_usage', [False, True])
async def test_provider_safety_refusal_survives_http_cleanup_timeout(tmp_path, reported_usage):
    class CleanupTimeoutReply(Reply):
        async def __aexit__(self, *args):
            raise TimeoutError()
    manager = manager_for(tmp_path)
    data = {'choices': [{'message': {'content': '', 'refusal': 'safety refusal'}}]}
    if reported_usage:
        data['usage'] = {'prompt_tokens': 10, 'completion_tokens': 0}
    manager.openai._session = Session(CleanupTimeoutReply(data), openai_reply())
    result = await manager.complete('news', task=LLMTask.MARKET_ANALYSIS, max_tokens=100)
    assert not result.success and result.refusal_detected
    assert result.uncertain_cost_ceiling_usd is None
    assert len(manager.openai._session.payloads) == 1


@pytest.mark.asyncio
async def test_fallback_refusal_is_not_reported_or_audited_as_success(tmp_path):
    manager = manager_for(tmp_path)
    refusal = openai_reply('')
    refusal.data['choices'][0]['message']['refusal'] = 'Provider safety refusal'
    manager.openai._session = Session(Reply({'error': {'code': 'rate_limit_exceeded'}}, 429), refusal)
    result = await manager.complete('news', task=LLMTask.MARKET_ANALYSIS, max_tokens=100)
    assert not result.success and result.refusal_detected
    assert result.attempt_trail[-1]['success'] is False
    rows = [json.loads(line) for p in (tmp_path / 'audit').glob('*.jsonl')
            for line in p.read_text().splitlines()]
    assert rows[-1]['outcome'] == 'error'


@pytest.mark.asyncio
async def test_historical_timeout_hold_counts_toward_daily_gate(tmp_path):
    manager = manager_for(tmp_path, daily_budget_usd=0.2)
    manager.daily_usage.add(0, 0, 'gemini-3.1-flash-lite', usage_known=False,
        uncertain_cost_ceiling_usd=0.262294)
    manager.openai._session = Session(openai_reply())
    result = await manager.complete('news', task=LLMTask.MARKET_ANALYSIS, max_tokens=100)
    assert not result.success
    assert not manager.openai._session.payloads
    assert manager.daily_usage.uncertain_cost_ceiling_usd > 0.2
    assert not manager._check_budget()


@pytest.mark.asyncio
async def test_fixed_provider_keeps_identity_after_bounded_timeout(tmp_path):
    manager = manager_for(tmp_path)
    manager.gemini._session = Session(TimeoutReply({}))
    manager.openai._session = Session(openai_reply())
    result = await manager.complete_with('news', provider=LLMProvider.GEMINI, max_tokens=100)
    assert not result.success and result.provider == LLMProvider.GEMINI
    assert not manager.openai._session.payloads


@pytest.mark.asyncio
async def test_old_unknown_still_blocks_without_sending_to_either_provider(tmp_path):
    manager = manager_for(tmp_path)
    token = manager.monthly_budget.reserve('gpt-5-mini', 'old', '', 100)
    manager.monthly_budget.settle(token, outcome='unknown')
    manager.gemini._session = Session(TimeoutReply({}))
    manager.openai._session = Session(openai_reply())
    result = await manager.complete('news', task=LLMTask.MARKET_ANALYSIS, max_tokens=100)
    assert result.budget_block_reason == 'unresolved_usage'
    assert not manager.gemini._session.payloads and not manager.openai._session.payloads


@pytest.mark.asyncio
async def test_preview_explicit_override_is_disabled_before_http(tmp_path):
    manager = manager_for(tmp_path, gemini_model_light='gemini-3.1-flash-lite-preview')
    manager.gemini._session = Session(TimeoutReply({}))
    manager.openai._session = Session(openai_reply())
    result = await manager.complete_with('news', LLMProvider.GEMINI)
    assert result.provider_error_code == 'provider_disabled'
    assert result.budget_reservation_token is None
    assert not manager.gemini._session.payloads and not manager.openai._session.payloads
