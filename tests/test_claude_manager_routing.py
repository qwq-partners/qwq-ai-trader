import asyncio
import json
import sqlite3

import pytest

from src.utils.llm import LLMConfig, LLMManager, LLMProvider, LLMResponse


MODEL = 'claude-opus-5-5'


def reply(*, success=True, kind=None, refusal=False, channel='api'):
    return LLMResponse('ACCEPT' if success else '', MODEL, LLMProvider.ANTHROPIC,
                       requested_model=MODEL, resolved_model=MODEL,
                       success=success, finish_reason='end_turn', input_tokens=100,
                       output_tokens=20, transport_error_kind=kind,
                       refusal_detected=refusal, billing_channel=channel)


class Client:
    def __init__(self, result):
        self.result = result
        self.calls = []
        self.closed = False

    async def complete(self, *args, **kwargs):
        self.calls.append(kwargs)
        if callable(self.result):
            return await self.result(**kwargs)
        return self.result

    async def close(self):
        self.closed = True


def manager(tmp_path, cli, api, *, budget=100):
    m = LLMManager(LLMConfig(monthly_budget_usd=budget, daily_budget_usd=budget,
                            budget_state_path=str(tmp_path/'budget.sqlite3'),
                            missing_cost_policy='log_and_continue'), audit_dir=tmp_path/'audit')
    m.claude_cli, m.anthropic = Client(cli), Client(api)
    return m


@pytest.mark.asyncio
async def test_cli_works_without_api_budget_and_never_records_zero_bill(tmp_path):
    m = manager(tmp_path, reply(), reply(), budget=0)
    result = await m.complete_with('synthetic', LLMProvider.ANTHROPIC, max_tokens=400)
    assert result.success and not m.anthropic.calls
    assert result.billing_channel == 'subscription' and result.transport == 'claude_cli'
    assert result.actual_incremental_cost_usd is None
    assert result.budget_reservation_token is None
    assert m.daily_usage.estimated_cost_usd == 0 and not m.daily_usage.cost_missing
    assert result.attempt_trail[0]['estimated_cost_usd'] is None
    assert result.attempt_trail[0]['billing_channel'] == 'subscription'
    with sqlite3.connect(tmp_path/'budget.sqlite3') as db:
        assert db.execute('SELECT COUNT(*) FROM reservations').fetchone()[0] == 0
    rows=[json.loads(line) for f in (tmp_path/'audit').glob('*.jsonl') for line in f.read_text().splitlines()]
    assert rows[0]['estimated_cost_usd'] is None
    await m.close()


@pytest.mark.asyncio
async def test_api_fallback_is_separate_attempt_with_real_budget_reservation(tmp_path):
    m = manager(tmp_path, reply(success=False, kind='cli_timeout'), reply(channel='subscription'))
    result = await m.complete_with('synthetic', LLMProvider.ANTHROPIC, max_tokens=400)
    assert result.success and len(m.anthropic.calls) == 1
    assert result.billing_channel == 'api' and result.transport == 'anthropic_messages'
    assert result.budget_reservation_token
    assert [r['transport'] for r in result.attempt_trail] == ['claude_cli','anthropic_messages']
    assert result.attempt_trail[0]['estimated_cost_usd'] is None
    assert float(result.attempt_trail[1]['estimated_cost_usd']) > 0
    assert m.daily_usage.estimated_cost_usd > 0
    await m.close()


@pytest.mark.asyncio
async def test_failed_cli_cannot_send_paid_fallback_over_budget(tmp_path):
    m = manager(tmp_path, reply(success=False, kind='cli_rate_limited'), reply(), budget=0)
    result = await m.complete_with('synthetic', LLMProvider.ANTHROPIC, max_tokens=400)
    assert not result.success and not m.anthropic.calls
    assert len(result.attempt_trail) == 2
    assert result.attempt_trail[-1]['request_sent'] is False
    await m.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['identity_mismatch','policy_violation','refusal','invalid_request','client_closed'])
async def test_policy_and_identity_failures_do_not_trigger_fallback(tmp_path, kind):
    m = manager(tmp_path, reply(success=False, kind=kind), reply())
    result = await m.complete_with('synthetic', LLMProvider.ANTHROPIC, max_tokens=400)
    assert not result.success and not m.anthropic.calls
    await m.close()


@pytest.mark.asyncio
async def test_provider_refusal_does_not_fallback_even_with_retryable_error(tmp_path):
    m = manager(tmp_path, reply(success=False, kind='cli_incomplete', refusal=True), reply())
    result = await m.complete_with('synthetic', LLMProvider.ANTHROPIC)
    assert not result.success and not m.anthropic.calls
    await m.close()


@pytest.mark.asyncio
async def test_deadline_budget_is_shared_and_total_not_extended(tmp_path):
    async def slow_cli(**kwargs):
        await asyncio.sleep(.03)
        return reply(success=False, kind='cli_timeout')
    m=manager(tmp_path, slow_cli, reply())
    start=asyncio.get_running_loop().time()
    result=await m.complete_with('synthetic', LLMProvider.ANTHROPIC,
                                 call_timeout_seconds=.2,max_tokens=400)
    assert result.success
    assert start < m.claude_cli.calls[0]['deadline'] <= start+.11
    assert start+.19 <= m.anthropic.calls[0]['deadline'] <= start+.21
    await m.close()


@pytest.mark.asyncio
async def test_caller_cancellation_preserves_subscription_attempt_without_api(tmp_path):
    entered=asyncio.Event()
    async def waiting(**kwargs):
        entered.set()
        await asyncio.sleep(30)
    m=manager(tmp_path, waiting, reply())
    task=asyncio.create_task(m.complete_with('synthetic', LLMProvider.ANTHROPIC))
    await asyncio.wait_for(entered.wait(),1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError) as caught:
        await task
    assert not m.anthropic.calls
    trail=caught.value._llm_trail
    assert len(trail)==1 and trail[0]['billing_channel']=='subscription'
    assert trail[0]['estimated_cost_usd'] is None
    await m.close()
    assert m.claude_cli.closed and m.anthropic.closed


def test_config_loads_secret_reference_without_reading_file(monkeypatch):
    monkeypatch.setenv('ANTHROPIC_API_KEY_FILE','/not-opened/credential')
    assert LLMConfig.from_env().anthropic_api_key_file=='/not-opened/credential'
    assert LLMConfig.from_config({}).anthropic_api_key_file=='/not-opened/credential'


def test_claude_controls_are_strict():
    with pytest.raises(ValueError):
        LLMConfig(claude_cli_enabled='false')
    with pytest.raises(ValueError):
        LLMConfig(claude_model='opus')


def test_subscription_decision_cost_remains_unknown():
    from src.analytics.llm_decision_record import attempt_record
    result=reply(channel='subscription')
    row=attempt_record('bear', LLMProvider.ANTHROPIC,None,result,
                       role_system_sha256=None,role_suffix=None)
    assert row['estimated_cost_usd'] is None
    assert row['billing_channel']=='subscription'


@pytest.mark.asyncio
async def test_api_payment_cooldown_never_suppresses_subscription(tmp_path):
    api_failure = reply(success=False, kind='http_error')
    api_failure.http_status = 402
    m = manager(tmp_path, reply(success=False, kind='cli_timeout'), api_failure)
    first = await m.complete_with('synthetic', LLMProvider.ANTHROPIC)
    assert first.http_status == 402
    m.claude_cli.result = reply()
    second = await m.complete_with('synthetic', LLMProvider.ANTHROPIC)
    assert second.success and len(m.claude_cli.calls) == 2
    m.claude_cli.result = reply(success=False, kind='cli_timeout')
    third = await m.complete_with('synthetic', LLMProvider.ANTHROPIC)
    assert not third.success and len(m.anthropic.calls) == 1
    assert third.transport_error_kind == 'transport_cooldown'
    assert third.request_sent is False
    await m.close()


@pytest.mark.asyncio
async def test_subscription_quota_cooldown_preserves_api_fallback(tmp_path):
    m = manager(tmp_path, reply(success=False, kind='cli_rate_limited'), reply())
    await m.complete_with('synthetic', LLMProvider.ANTHROPIC)
    result = await m.complete_with('synthetic', LLMProvider.ANTHROPIC)
    assert result.success and len(m.claude_cli.calls) == 1 and len(m.anthropic.calls) == 2
    assert result.attempt_trail[0]['request_sent'] is False
    assert result.attempt_trail[0]['billing_channel'] == 'subscription'
    m._claude_transport_cooldowns.clear()
    await m.complete_with('synthetic', LLMProvider.ANTHROPIC)
    assert len(m.claude_cli.calls) == 2
    await m.close()
