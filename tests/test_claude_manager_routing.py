"""Claude Bear 전송 순서 계약 — L01 Task 2(2026-11-09): 예산 내 API 1차, 가용성 실패에서만 구독 CLI 1회."""
import asyncio
import json
import sqlite3

import pytest

from src.utils.llm import LLMConfig, LLMManager, LLMProvider, LLMResponse


MODEL = 'claude-opus-5-5'


def reply(*, success=True, kind=None, refusal=False, channel='api', http=None):
    response = LLMResponse('ACCEPT' if success else '', MODEL, LLMProvider.ANTHROPIC,
                           requested_model=MODEL, resolved_model=MODEL,
                           success=success, finish_reason='end_turn', input_tokens=100,
                           output_tokens=20, transport_error_kind=kind,
                           refusal_detected=refusal, billing_channel=channel)
    if http is not None:
        response.http_status = http
    return response


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


def transports(result):
    return [row['transport'] for row in result.attempt_trail]


@pytest.mark.asyncio
async def test_api_primary_success_reserves_budget_and_never_touches_cli(tmp_path):
    m = manager(tmp_path, reply(channel='subscription'), reply())
    result = await m.complete_with('synthetic', LLMProvider.ANTHROPIC, max_tokens=400)
    assert result.success and len(m.anthropic.calls) == 1 and not m.claude_cli.calls
    assert result.billing_channel == 'api' and result.transport == 'anthropic_messages'
    assert result.budget_reservation_token
    assert transports(result) == ['anthropic_messages']
    assert float(result.attempt_trail[0]['estimated_cost_usd']) > 0
    assert m.daily_usage.estimated_cost_usd > 0 and m.stats['fallback_count'] == 0
    await m.close()


@pytest.mark.asyncio
async def test_api_budget_block_falls_back_to_cli_without_zero_bill_or_reservation(tmp_path):
    m = manager(tmp_path, reply(channel='subscription'), reply(), budget=0)
    result = await m.complete_with('synthetic', LLMProvider.ANTHROPIC, max_tokens=400)
    assert result.success and not m.anthropic.calls and len(m.claude_cli.calls) == 1
    assert result.billing_channel == 'subscription' and result.transport == 'claude_cli'
    assert result.actual_incremental_cost_usd is None and result.budget_reservation_token is None
    assert transports(result) == ['anthropic_messages', 'claude_cli']
    blocked, cli = result.attempt_trail
    assert blocked['request_sent'] is False and blocked['billing_channel'] == 'api'
    assert cli['estimated_cost_usd'] is None and cli['billing_channel'] == 'subscription'
    assert m.daily_usage.estimated_cost_usd == 0 and not m.daily_usage.cost_missing
    with sqlite3.connect(tmp_path/'budget.sqlite3') as db:
        assert db.execute('SELECT COUNT(*) FROM reservations').fetchone()[0] == 0
    rows = [json.loads(line) for f in (tmp_path/'audit').glob('*.jsonl') for line in f.read_text().splitlines()]
    assert all(row['estimated_cost_usd'] in (None, '0') or float(row['estimated_cost_usd']) == 0 for row in rows)
    await m.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('kind,http', [('api_timeout', None), ('api_unavailable', None),
                                       ('api_auth_unavailable', None), ('api_http_error', 500),
                                       ('api_http_error', 429), ('api_http_error', 400)])
async def test_api_availability_failure_sends_one_subscription_attempt(tmp_path, kind, http):
    m = manager(tmp_path, reply(channel='subscription'), reply(success=False, kind=kind, http=http))
    result = await m.complete_with('synthetic', LLMProvider.ANTHROPIC, max_tokens=400)
    assert result.success and len(m.anthropic.calls) == 1 and len(m.claude_cli.calls) == 1
    assert result.transport == 'claude_cli' and result.billing_channel == 'subscription'
    assert transports(result) == ['anthropic_messages', 'claude_cli']
    assert m.stats['fallback_count'] == 1
    await m.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['identity_mismatch', 'api_incomplete', 'invalid_request', 'client_closed', 'cancelled'])
async def test_api_non_availability_failures_do_not_fallback(tmp_path, kind):
    m = manager(tmp_path, reply(channel='subscription'), reply(success=False, kind=kind))
    result = await m.complete_with('synthetic', LLMProvider.ANTHROPIC, max_tokens=400)
    assert not result.success and not m.claude_cli.calls and len(m.anthropic.calls) == 1
    assert result.transport_error_kind == kind and transports(result) == ['anthropic_messages']
    await m.close()


class _HtmlReply:
    def __init__(self, status):
        self.status = status
    async def __aenter__(self):
        return self
    async def __aexit__(self, *args):
        pass
    async def json(self):
        raise ValueError("<html>502 Bad Gateway</html>")


class _HtmlSession:
    closed = False
    def __init__(self, status):
        self.status, self.calls = status, []
    def post(self, url, **kwargs):
        self.calls.append(url)
        return _HtmlReply(self.status)
    async def close(self):
        self.closed = True


@pytest.mark.asyncio
@pytest.mark.parametrize('status,cli_calls', [(502, 1), (503, 1), (200, 0)])
async def test_real_api_client_non_json_5xx_reaches_cli_but_non_json_200_does_not(tmp_path, status, cli_calls):
    """독립 리뷰 P1 연결 회귀: 실제 AnthropicAPIClient 가 HTML 5xx 를 api_http_error 로 올려 CLI 대체가 이어진다."""
    from src.utils.claude_transport import AnthropicAPIClient
    m = manager(tmp_path, reply(channel='subscription'), reply())
    m.config.anthropic_api_key = 'synthetic'
    m.anthropic = AnthropicAPIClient(m.config)
    m.anthropic._session = _HtmlSession(status)
    result = await m.complete_with('synthetic', LLMProvider.ANTHROPIC, max_tokens=400)
    assert len(m.anthropic._session.calls) == 1 and len(m.claude_cli.calls) == cli_calls
    assert result.success is (cli_calls == 1)
    assert result.attempt_trail[0]['transport_error_kind'] == ('api_http_error' if status != 200 else 'api_incomplete')
    await m.close()


@pytest.mark.asyncio
async def test_api_refusal_does_not_fallback_even_with_retryable_error(tmp_path):
    m = manager(tmp_path, reply(channel='subscription'), reply(success=False, kind='api_http_error', refusal=True, http=500))
    result = await m.complete_with('synthetic', LLMProvider.ANTHROPIC)
    assert not result.success and not m.claude_cli.calls
    await m.close()


@pytest.mark.asyncio
async def test_truncated_or_empty_api_200_is_not_an_availability_failure(tmp_path):
    truncated = reply(success=False, kind='api_incomplete', http=200)
    truncated.finish_reason = 'max_tokens'
    m = manager(tmp_path, reply(channel='subscription'), truncated)
    result = await m.complete_with('synthetic', LLMProvider.ANTHROPIC, max_tokens=400)
    assert not result.success and not m.claude_cli.calls and result.attempt_trail[-1]['finish_reason'] == 'max_tokens'
    await m.close()


@pytest.mark.asyncio
async def test_api_gets_whole_budget_and_cli_gets_only_the_remainder(tmp_path):
    m = manager(tmp_path, reply(channel='subscription'), reply(success=False, kind='api_timeout'))
    start = asyncio.get_running_loop().time()
    result = await m.complete_with('synthetic', LLMProvider.ANTHROPIC, call_timeout_seconds=20, max_tokens=400)
    assert result.success
    api_deadline, cli_deadline = m.anthropic.calls[0]['deadline'], m.claude_cli.calls[0]['deadline']
    assert start + 19.9 <= api_deadline <= start + 20.1
    assert cli_deadline == api_deadline          # 총 시한은 늘어나지 않는다
    await m.close()


@pytest.mark.asyncio
async def test_cli_fallback_is_skipped_when_remaining_budget_is_below_startup_floor(tmp_path):
    async def slow_api(**kwargs):
        await asyncio.sleep(.05)
        return reply(success=False, kind='api_timeout')
    m = manager(tmp_path, reply(channel='subscription'), slow_api)
    result = await m.complete_with('synthetic', LLMProvider.ANTHROPIC, call_timeout_seconds=4.03, max_tokens=400)
    assert not result.success and result.transport_error_kind == 'api_timeout'
    assert len(m.anthropic.calls) == 1 and not m.claude_cli.calls and m.stats['fallback_count'] == 0
    await m.close()


@pytest.mark.asyncio
async def test_cli_disabled_returns_api_failure_without_a_cli_row(tmp_path):
    m = manager(tmp_path, reply(channel='subscription'), reply(success=False, kind='api_timeout'))
    m.config.claude_cli_enabled = False
    result = await m.complete_with('synthetic', LLMProvider.ANTHROPIC, max_tokens=400)
    assert not result.success and not m.claude_cli.calls and transports(result) == ['anthropic_messages']
    await m.close()


@pytest.mark.asyncio
async def test_caller_cancellation_preserves_api_attempt_without_cli(tmp_path):
    entered = asyncio.Event()
    async def waiting(**kwargs):
        entered.set()
        await asyncio.sleep(30)
    m = manager(tmp_path, reply(channel='subscription'), waiting)
    task = asyncio.create_task(m.complete_with('synthetic', LLMProvider.ANTHROPIC))
    await asyncio.wait_for(entered.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError) as caught:
        await task
    assert not m.claude_cli.calls
    trail = caught.value._llm_trail
    assert len(trail) == 1 and trail[0]['billing_channel'] == 'api'
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
async def test_api_payment_cooldown_routes_to_cli_without_sending_api(tmp_path):
    m = manager(tmp_path, reply(channel='subscription'), reply(success=False, kind='api_http_error', http=402))
    first = await m.complete_with('synthetic', LLMProvider.ANTHROPIC)
    assert first.success and first.transport == 'claude_cli'
    assert first.attempt_trail[0]['transport_error_kind'] == 'api_http_error' and len(m.anthropic.calls) == 1
    second = await m.complete_with('synthetic', LLMProvider.ANTHROPIC)
    assert second.success and len(m.anthropic.calls) == 1 and len(m.claude_cli.calls) == 2
    assert second.attempt_trail[0]['transport_error_kind'] == 'transport_cooldown'
    assert second.attempt_trail[0]['request_sent'] is False
    m._claude_transport_cooldowns.clear()
    await m.complete_with('synthetic', LLMProvider.ANTHROPIC)
    assert len(m.anthropic.calls) == 2
    await m.close()


@pytest.mark.asyncio
async def test_cli_quota_cooldown_is_returned_after_api_failure_without_second_cli_spawn(tmp_path):
    m = manager(tmp_path, reply(success=False, kind='cli_rate_limited', channel='subscription'),
                reply(success=False, kind='api_timeout'))
    first = await m.complete_with('synthetic', LLMProvider.ANTHROPIC)
    assert not first.success and first.transport_error_kind == 'cli_rate_limited' and len(m.claude_cli.calls) == 1
    second = await m.complete_with('synthetic', LLMProvider.ANTHROPIC)
    assert len(m.anthropic.calls) == 2 and len(m.claude_cli.calls) == 1
    assert second.transport_error_kind == 'cli_rate_limited' and second.request_sent is False
    assert transports(second) == ['anthropic_messages', 'claude_cli']
    await m.close()


@pytest.mark.asyncio
async def test_team_api_cap_reaches_reservation_and_attempt_trail(tmp_path, monkeypatch):
    failed = reply(success=False, kind='api_incomplete', http=200)
    failed.finish_reason = 'max_tokens'
    m = manager(tmp_path, reply(channel='subscription'), failed)
    reserved = []
    reserve = m.monthly_budget.reserve
    def capture(*args, **kwargs):
        reserved.append(args[3])
        return reserve(*args, **kwargs)
    monkeypatch.setattr(m.monthly_budget, 'reserve', capture)
    result = await m.complete_with('synthetic', LLMProvider.ANTHROPIC,
                                   max_tokens=400, api_max_tokens=800)
    assert m.anthropic.calls[0]['max_tokens'] == 800 and not m.claude_cli.calls
    assert reserved == [800]
    assert [row['requested_max_tokens'] for row in result.attempt_trail] == [800]
    assert result.attempt_trail[-1]['finish_reason'] == 'max_tokens'
    await m.close()


@pytest.mark.asyncio
async def test_cli_fallback_uses_base_cap_not_api_cap(tmp_path):
    m = manager(tmp_path, reply(channel='subscription'), reply(success=False, kind='api_http_error', http=503))
    result = await m.complete_with('synthetic', LLMProvider.ANTHROPIC, max_tokens=400, api_max_tokens=800)
    assert result.success and m.anthropic.calls[0]['max_tokens'] == 800 and m.claude_cli.calls[0]['max_tokens'] == 400
    assert [row['requested_max_tokens'] for row in result.attempt_trail] == [800, 400]
    await m.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('cap', [True, False, 0, 801, 1.5, '800', None])
async def test_invalid_api_cap_rejected_before_any_attempt(tmp_path, cap):
    m = manager(tmp_path, reply(), reply())
    with pytest.raises(ValueError):
        await m.complete_with('synthetic', LLMProvider.ANTHROPIC, api_max_tokens=cap)
    assert not m.claude_cli.calls and not m.anthropic.calls
    await m.close()


@pytest.mark.asyncio
async def test_attempt_finish_reason_is_bounded(tmp_path):
    failure = reply(success=False, kind='api_incomplete')
    failure.finish_reason = 'untrusted free text'
    m = manager(tmp_path, reply(channel='subscription'), failure)
    result = await m.complete_with('synthetic', LLMProvider.ANTHROPIC, api_max_tokens=800)
    assert result.attempt_trail[-1]['finish_reason'] is None
    await m.close()


@pytest.mark.asyncio
async def test_api_primary_preserves_requested_cap(tmp_path):
    m = manager(tmp_path, reply(channel='subscription'), reply())
    await m.complete_with('synthetic', LLMProvider.ANTHROPIC, max_tokens=123)
    assert m.anthropic.calls[0]['max_tokens'] == 123
    await m.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('primary', ['plain', 'disabled', 'cooldown'])
async def test_non_opt_in_cap_above_400_never_reaches_any_transport(tmp_path, primary):
    m = manager(tmp_path, reply(), reply())
    if primary == 'disabled':
        m.config.claude_cli_enabled = False
    elif primary == 'cooldown':
        m._claude_transport_cooldowns['anthropic_messages'] = (
            asyncio.get_running_loop().time() + 300, 'transport_cooldown')
    result = await m.complete_with('synthetic', LLMProvider.ANTHROPIC, max_tokens=800)
    assert not m.claude_cli.calls and not m.anthropic.calls
    assert not result.success and not result.request_sent
    assert result.transport_error_kind == 'invalid_request'
    assert result.attempt_trail[-1]['requested_max_tokens'] == 800
    await m.close()


@pytest.mark.asyncio
async def test_explicit_api_cap_800_is_sent_to_api_first(tmp_path):
    m = manager(tmp_path, reply(channel='subscription'), reply())
    result = await m.complete_with('synthetic', LLMProvider.ANTHROPIC, max_tokens=400, api_max_tokens=800)
    assert result.success and not m.claude_cli.calls
    assert m.anthropic.calls[0]['max_tokens'] == 800
    await m.close()
