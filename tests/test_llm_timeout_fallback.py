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
async def test_timeout_fallback_preserves_unknown_cost_and_independent_reservation(tmp_path):
    manager = manager_for(tmp_path)
    manager.gemini._session = Session(TimeoutReply({}))
    manager.openai._session = Session(openai_reply())
    result = await manager.complete('news', task=LLMTask.THEME_DETECTION, max_tokens=100)
    assert result.success and result.provider == LLMProvider.OPENAI
    assert len(result.attempt_trail) == 2
    first, second = result.attempt_trail
    assert first['transport_error_kind'] == 'timeout'
    assert first['estimated_cost_usd'] is None
    assert first['uncertain_cost_ceiling_usd'] == pytest.approx(0.262294)
    assert first['budget_reservation_token'] != second['budget_reservation_token']
    with sqlite3.connect(tmp_path / 'budget.sqlite3') as db:
        assert db.execute('SELECT state,actual_units FROM reservations WHERE token=?',
                          (first['budget_reservation_token'],)).fetchone() == ('unknown', None)
    status = manager.monthly_budget.status()
    assert status['unresolved_count'] == 1 and not status['blocked']
    assert manager.daily_usage.cost_missing  # The bill is still incomplete.
    assert manager._check_budget()
    summary = manager.get_usage_summary()
    assert summary['uncertain_cost_ceiling_usd'] == pytest.approx(0.262294)
    assert summary['budget_remaining_usd'] == pytest.approx(4.7376, abs=0.0001)
    rows = [json.loads(line) for p in (tmp_path / 'audit').glob('*.jsonl')
            for line in p.read_text().splitlines()]
    assert rows[0]['estimated_cost_usd'] is None
    assert rows[0]['uncertain_cost_ceiling_usd'] == pytest.approx(0.262294)


@pytest.mark.asyncio
async def test_timeout_fallback_does_not_start_json_repair_chain(tmp_path):
    manager = manager_for(tmp_path)
    manager.gemini._session = Session(TimeoutReply({}), TimeoutReply({}))
    manager.openai._session = Session(openai_reply('not json'), openai_reply('{}'))
    result = await manager.complete_json('news', task=LLMTask.THEME_DETECTION, max_tokens=100)
    assert result['error'] == 'Invalid JSON'
    assert len(manager.gemini._session.payloads) == 1
    assert len(manager.openai._session.payloads) == 1


@pytest.mark.asyncio
async def test_json_repair_timeout_cannot_start_third_provider_attempt(tmp_path):
    manager = manager_for(tmp_path)
    manager.gemini._session = Session(gemini_reply('not json'), TimeoutReply({}))
    manager.openai._session = Session(openai_reply('{}'))
    result = await manager.complete_json('news', task=LLMTask.THEME_DETECTION, max_tokens=100)
    assert result['error']
    assert len(manager.gemini._session.payloads) == 2
    assert not manager.openai._session.payloads


@pytest.mark.asyncio
async def test_timeout_while_reading_permission_error_does_not_authorize_fallback(tmp_path):
    class PermissionReply(Reply):
        async def json(self):
            raise TimeoutError()
    manager = manager_for(tmp_path)
    manager.gemini._session = Session(PermissionReply({}, status=403))
    manager.openai._session = Session(openai_reply())
    result = await manager.complete('news', task=LLMTask.THEME_DETECTION, max_tokens=100)
    assert not result.success and not manager.openai._session.payloads
    assert manager.monthly_budget.status()['block_reason'] == 'unresolved_usage'


@pytest.mark.asyncio
async def test_timeout_hold_counts_toward_daily_gate(tmp_path):
    manager = manager_for(tmp_path, daily_budget_usd=0.2)
    manager.gemini._session = Session(TimeoutReply({}))
    manager.openai._session = Session(openai_reply())
    result = await manager.complete('news', task=LLMTask.THEME_DETECTION, max_tokens=100)
    assert not result.success
    assert not manager.openai._session.payloads
    assert manager.monthly_budget.status()['uncertain_cost_ceiling_usd'] > 0.2
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
    result = await manager.complete('news', task=LLMTask.THEME_DETECTION, max_tokens=100)
    assert result.budget_block_reason == 'unresolved_usage'
    assert not manager.gemini._session.payloads and not manager.openai._session.payloads


@pytest.mark.asyncio
async def test_preview_explicit_override_is_rejected_before_http(tmp_path):
    manager = manager_for(tmp_path, gemini_model_light='gemini-3.1-flash-lite-preview')
    manager.gemini._session = Session(TimeoutReply({}))
    manager.openai._session = Session(openai_reply())
    result = await manager.complete('news', task=LLMTask.THEME_DETECTION)
    assert result.budget_block_reason == 'retired_model'
    assert not manager.gemini._session.payloads and not manager.openai._session.payloads
