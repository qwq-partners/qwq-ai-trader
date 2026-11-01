"""Claude 단일 시도 어댑터 — 실제 임시 월 원장, 가짜 Claude 전송. 자격/네트워크/CLI 실행 없음."""
import asyncio
from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace

import pytest

from src.analytics import model_comparison_runner as m
from src.analytics.model_comparison_claude import ClaudeSingleAttemptAdapter
from src.analytics.model_comparison_execution import ComparisonCallGuard, ComparisonExecutionPolicy, ComparisonRequest
from src.utils.claude_transport import AnthropicAPIClient, ClaudeCLIClient, _reject
from src.utils.llm import LLMConfig
from src.utils.llm_budget import BudgetBlocked, MonthlyBudgetLedger
from src.utils.llm_pricing import rate_quote
from tests.test_model_comparison_runner import NOW, FakeGemini, FakeOpenAI

CLAUDE = 'claude-opus-5-5'


def _ok(response):
    response.content, response.success = 'REJECT — 수요 둔화 시나리오', True
    response.request_sent, response.billing_status, response.usage_known = True, 'reported_usage', True
    response.input_tokens, response.output_tokens, response.finish_reason = 100, 10, 'end_turn'
    response.resolved_model, response.model_identity_status = response.requested_model, 'verified'
    if response.billing_channel == 'subscription':
        response.subscription_estimated_cost_usd = 0.0123
    return response


def _sent(kind, **fields):
    def script(response):
        response.request_sent, response.billing_status = True, 'unknown'
        _reject(response, kind)
        for key, value in fields.items():
            setattr(response, key, value)
        return response
    return script


def _not_sent(response):
    return _reject(response, 'cli_unavailable' if response.billing_channel == 'subscription' else 'api_auth_unavailable')


def _cancel(response):
    raise asyncio.CancelledError


SCRIPTS = {
    'ok': _ok,
    'identity_mismatch': _sent('identity_mismatch', model_identity_status='mismatch', resolved_model='claude-other'),
    'refusal': _sent('refusal'),
    'incomplete': _sent('cli_incomplete'),
    'timeout': _sent('cli_timeout'),
    'not_sent': _not_sent,
    'rejected_before_generation': _sent('api_http_error', billing_status='rejected_before_generation',
                                        http_status=429, provider_error_type='rate_limit_error'),
}


class _Recorder:
    def __init__(self, script=_ok):
        super().__init__(LLMConfig(openai_api_key='', gemini_api_key=''))
        self.calls, self.script = [], script

    async def complete(self, prompt, system='', *, model, max_tokens=400, reasoning_effort='low', deadline=None, **kw):
        self.calls.append(dict(prompt=prompt, system=system, model=model, max_tokens=max_tokens,
                               reasoning_effort=reasoning_effort, deadline=deadline, extra=kw,
                               loop_time=asyncio.get_running_loop().time()))
        await asyncio.sleep(0)
        return self.script(self._response(model, reasoning_effort))


class FakeCLI(_Recorder, ClaudeCLIClient):
    pass


class FakeAPI(_Recorder, AnthropicAPIClient):
    pass


def guard_for(tmp_path, ledger=None):
    ledger = ledger or MonthlyBudgetLedger(tmp_path / 'budget.db', 100, clock=lambda: NOW, policy_enabled=True)
    return ledger, ComparisonCallGuard(ledger=ledger, policy=ComparisonExecutionPolicy(
        experiment_id='C', daily_attempt_cap=40, allowed_models=('gpt-5-mini', CLAUDE, 'gemini-3.1-flash-lite'),
        max_output_tokens=400, experiment_usd_ceiling='100', valid_from='2026-01-01', valid_until='2027-12-31',
        pairs_per_day=20, max_common_context_bytes=65536))


def request(channel, **over):
    fields = dict(model=CLAUDE, prompt='p', system='s', max_output_tokens=400, reasoning_effort='low',
                  temperature=None, seed=None, timeout_seconds=11.5, billing_channel=channel)
    fields.update(over)
    return ComparisonRequest(**fields)


def reservations(ledger):
    with ledger._connect(readonly=True) as db:
        return [tuple(r) for r in db.execute('SELECT state FROM reservations')]


def bear_controls(channel='subscription', **over):
    fields = dict(model=CLAUDE, max_output_tokens=400, reasoning_effort='low', temperature=None, seed=None,
                  timeout_seconds=11.5, billing_channel=channel)
    fields.update(over)
    return m.RoleControls(**fields)


def claude_setup(tmp_path, *, bear_script=_ok, api=False):
    ledger, guard = guard_for(tmp_path)
    op, ge = FakeOpenAI(), FakeGemini()
    client = (FakeAPI if api else FakeCLI)(bear_script)
    runner = m.PairedComparisonRunner(guard=guard, adapter=m.SingleAttemptAdapter(openai=op, gemini=ge, clock=lambda: NOW),
                                      claude_adapter=ClaudeSingleAttemptAdapter(client, clock=lambda: NOW), clock=lambda: NOW)
    spec = m.PairSpec(decision_id='decision-c', signal_id='signal-1', symbol='005490',
                      common_context='same immutable facts', input_available_at=NOW.isoformat(),
                      current=m.RoleControls('gpt-5-mini', 400, 'minimal', 0.3, 20260803),
                      candidate=m.RoleControls('gpt-5-mini', 400, 'low', 0.3, 20260803),
                      bear=bear_controls('api' if api else 'subscription'), bear_empty_retries=0,
                      neutral_sizing=None, pricing_basis='offline_replay',
                      fixed_at=NOW.isoformat(), strict_on_disagreement=True, fallback_chain_timeout_seconds=10.0)
    return ledger, guard, runner, spec, op, client


# ---------------------------------------------------------------- 어댑터 선언·채널

def test_adapter_declares_channel_from_raw_transport_and_rejects_anything_else():
    cli, api = ClaudeSingleAttemptAdapter(FakeCLI()), ClaudeSingleAttemptAdapter(FakeAPI())
    assert (cli.billing_channel, cli.transport) == ('subscription', 'claude_cli')
    assert (api.billing_channel, api.transport) == ('api', 'anthropic_messages')
    for wrong in (SimpleNamespace(complete=lambda: None, billing_channel='subscription', transport='claude_cli'),
                  FakeOpenAI(), object()):
        with pytest.raises(ValueError, match='raw_claude_transport_required'):
            ClaudeSingleAttemptAdapter(wrong)


@pytest.mark.parametrize('client_cls,wrong_channel', [(FakeCLI, 'api'), (FakeAPI, 'subscription')])
def test_channel_mismatch_is_rejected_before_any_transport_call(tmp_path, client_cls, wrong_channel):
    client = client_cls()
    adapter = ClaudeSingleAttemptAdapter(client, clock=lambda: NOW)
    with pytest.raises(ValueError, match='claude_adapter_billing_channel_mismatch'):
        asyncio.run(adapter(request(wrong_channel)))
    assert client.calls == []
    ledger, guard = guard_for(tmp_path)
    with pytest.raises(BudgetBlocked, match='comparison_adapter_channel_mismatch'):
        asyncio.run(guard.call(request(wrong_channel), dispatch=adapter, attempt_id='mismatch'))
    assert client.calls == []
    record = guard.lookup('mismatch')
    assert (record['state'], record['dispatch_entered'], record['billing_channel']) == ('blocked', False, wrong_channel)
    # 유료 요청의 예약은 not_sent 로 해제되고 구독 요청은 예약이 없다 — 어느 쪽도 월 원장을 막지 않는다.
    status = ledger.status()
    assert status['unresolved_count'] == 0 and status['spent_usd'] == 0
    assert all(state == 'released' for (state,) in reservations(ledger))


@pytest.mark.parametrize('bad', [dict(model='gpt-5-mini'), dict(temperature=0.3), dict(seed=7),
                                 dict(reasoning_effort=None), dict(reasoning_effort='high')])
def test_unsupported_claude_request_controls_never_reach_transport(bad):
    client = FakeCLI()
    with pytest.raises(ValueError):
        asyncio.run(ClaudeSingleAttemptAdapter(client, clock=lambda: NOW)(request('subscription', **bad)))
    assert client.calls == []


# ---------------------------------------------------------------- 과금 소유권

def test_cli_attempt_consumes_day_slot_but_never_touches_monthly_ledger(tmp_path):
    ledger, guard = guard_for(tmp_path)
    client = FakeCLI()
    result = asyncio.run(guard.call(request('subscription'), dispatch=ClaudeSingleAttemptAdapter(client, clock=lambda: NOW),
                                    attempt_id='cli-1'))
    assert len(client.calls) == 1
    assert result.settlement == 'subscription' and result.actual_cost_usd is None
    assert reservations(ledger) == []
    assert ledger.status()['spent_usd'] == 0 and ledger.status()['unresolved_count'] == 0
    value = result.value
    assert value['billing_channel'] == 'subscription' and value['transport'] == 'claude_cli'
    assert value['api_cost_usd'] is None and value['cost_basis'] == 'subscription_not_api_billed'
    assert value['subscription_estimated_cost_usd'] == 0.0123 and value['success'] is True
    (row,) = guard.day_attempts(ledger.today().isoformat())
    assert (row['state'], row['cost_usd'], row['reservation_token'], row['dispatch_entered']) == ('known', None, None, True)
    assert row['billing_channel'] == 'subscription' and row['subscription_estimated_cost_usd'] == 0.0123
    # 같은 ID 복구는 재전송 없이 같은 결과를 돌려준다.
    again = asyncio.run(guard.call(request('subscription'), dispatch=ClaudeSingleAttemptAdapter(client, clock=lambda: NOW),
                                   attempt_id='cli-1'))
    assert again.value == value and len(client.calls) == 1


def test_api_attempt_is_reserved_once_by_guard_and_never_by_adapter(tmp_path):
    ledger, guard = guard_for(tmp_path)
    client = FakeAPI()
    adapter = ClaudeSingleAttemptAdapter(client, clock=lambda: NOW)
    direct = asyncio.run(adapter(request('api')))
    assert reservations(ledger) == [] and len(client.calls) == 1        # 어댑터 단독 호출은 예약하지 않는다
    quote = rate_quote(CLAUDE, 100, 10, on_date=NOW.date())
    expected = (Decimal(str(quote.input_usd_per_million)) * 100 + Decimal(str(quote.output_usd_per_million)) * 10) / 1000000
    assert direct.settlement == 'measured' and direct.actual_cost_usd == expected
    assert direct.value['api_cost_usd'] == str(expected) and direct.value['cost_basis'] == quote.basis
    assert direct.value['subscription_estimated_cost_usd'] is None
    result = asyncio.run(guard.call(request('api'), dispatch=adapter, attempt_id='api-1'))
    assert len(client.calls) == 2 and reservations(ledger) == [('settled',)]
    assert Decimal(str(ledger.status()['spent_usd'])) == expected
    assert guard.lookup('api-1')['state'] == 'known' and result.actual_cost_usd == expected


def test_transport_receives_only_supported_controls_and_caller_deadline():
    client = FakeCLI()
    req = request('subscription', prompt='질문', system='규칙', max_output_tokens=123, timeout_seconds=4.5)
    asyncio.run(ClaudeSingleAttemptAdapter(client, clock=lambda: NOW)(req))
    (call,) = client.calls
    assert (call['prompt'], call['system'], call['model'], call['max_tokens'], call['reasoning_effort']) == (
        '질문', '규칙', CLAUDE, 123, 'low')
    assert call['extra'] == {}                                           # temperature/seed 는 전송하지 않는다
    assert 0 < call['deadline'] - call['loop_time'] <= 4.5                # 호출자 시한을 늘리지 않는다


# ---------------------------------------------------------------- 응답 상태

@pytest.mark.parametrize('case', ['ok', 'identity_mismatch', 'refusal', 'incomplete', 'timeout', 'not_sent'])
@pytest.mark.parametrize('api', [False, True])
def test_response_states_are_preserved_not_repaired(tmp_path, case, api):
    ledger, guard = guard_for(tmp_path)
    client = (FakeAPI if api else FakeCLI)(SCRIPTS[case])
    channel = 'api' if api else 'subscription'
    result = asyncio.run(guard.call(request(channel), dispatch=ClaudeSingleAttemptAdapter(client, clock=lambda: NOW),
                                    attempt_id=case))
    value = result.value
    assert len(client.calls) == 1
    assert value['success'] is (case == 'ok')
    assert value['requested_model_match'] is (case == 'ok')
    assert value['effective_reasoning_effort'] is None and value['requested_reasoning_effort'] == 'low'
    if case == 'ok':
        assert value['model_identity_status'] == 'verified' and value['error'] is None
    else:
        assert value['error'] == value['transport_error_kind'] and value['transport_error_kind']
    assert value['refusal_detected'] is (case == 'refusal')
    assert value['request_sent'] is (case != 'not_sent')
    assert value['usage_known'] is (case == 'ok')
    record = guard.lookup(case)
    if not api:
        assert result.settlement == 'subscription' and result.actual_cost_usd is None and value['api_cost_usd'] is None
        assert record['state'] == 'known' and record['reservation_token'] is None
        assert ledger.status()['unresolved_count'] == 0
    elif case == 'not_sent':
        assert (value['api_cost_usd'], value['cost_basis']) == ('0', 'transport_not_sent')
        assert record['state'] == 'known' and ledger.status()['unresolved_count'] == 0
    elif case == 'ok':
        assert value['api_cost_usd'] is not None and record['state'] == 'known'
    else:
        # 전송 뒤 사용량 불명의 유료 시도는 기존 계약대로 unknown 예약으로 남는다(임의 0원 금지).
        assert value['api_cost_usd'] is None and record['state'] == 'unknown'
        assert ledger.status()['unresolved_count'] == 1


def test_api_rejected_before_generation_releases_reservation_without_charge(tmp_path):
    ledger, guard = guard_for(tmp_path)
    client = FakeAPI(SCRIPTS['rejected_before_generation'])
    result = asyncio.run(guard.call(request('api'), dispatch=ClaudeSingleAttemptAdapter(client, clock=lambda: NOW),
                                    attempt_id='rej'))
    assert result.settlement == 'rejected_before_generation' and result.actual_cost_usd == Decimal('0')
    assert result.value['cost_basis'] == 'rejected_before_generation_inferred'
    assert result.value['http_status'] == 429 and result.value['provider_error_type'] == 'rate_limit_error'
    assert reservations(ledger) == [('released',)] and ledger.status()['unresolved_count'] == 0


@pytest.mark.parametrize('api', [False, True])
def test_cancellation_propagates_and_leaves_dispatch_entered_without_resend(tmp_path, api):
    ledger, guard = guard_for(tmp_path)
    client = (FakeAPI if api else FakeCLI)(_cancel)
    adapter = ClaudeSingleAttemptAdapter(client, clock=lambda: NOW)
    channel = 'api' if api else 'subscription'
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(guard.call(request(channel), dispatch=adapter, attempt_id='cancel'))
    record = guard.lookup('cancel')
    assert record['dispatch_entered'] is True and record['block_reason'] == 'comparison_attempt_interrupted'
    assert record['billing_channel'] == channel
    assert (record['reservation_token'] is None) is (not api)
    with pytest.raises(BudgetBlocked):
        asyncio.run(guard.call(request(channel), dispatch=adapter, attempt_id='cancel'))
    assert len(client.calls) == 1


def test_real_transports_without_credentials_send_nothing(tmp_path):
    ledger, guard = guard_for(tmp_path)
    cli = ClaudeCLIClient(LLMConfig(openai_api_key='', gemini_api_key='', claude_cli_enabled=False))
    api = AnthropicAPIClient(LLMConfig(openai_api_key='', gemini_api_key='',
                                       anthropic_api_key_file=str(tmp_path / 'missing-key')))
    sub = asyncio.run(guard.call(request('subscription'), dispatch=ClaudeSingleAttemptAdapter(cli, clock=lambda: NOW),
                                 attempt_id='real-cli'))
    paid = asyncio.run(guard.call(request('api'), dispatch=ClaudeSingleAttemptAdapter(api, clock=lambda: NOW),
                                  attempt_id='real-api'))
    assert sub.value['request_sent'] is False and sub.value['transport_error_kind'] == 'cli_unavailable'
    assert sub.actual_cost_usd is None and sub.settlement == 'subscription'
    assert paid.value['request_sent'] is False and paid.value['transport_error_kind'] == 'api_auth_unavailable'
    assert (paid.value['api_cost_usd'], paid.value['cost_basis']) == ('0', 'transport_not_sent')
    assert reservations(ledger) == [('settled',)]                      # 미전송 유료 시도는 측정 0원으로 정산된다
    assert ledger.status()['spent_usd'] == 0 and ledger.status()['unresolved_count'] == 0
    asyncio.run(api.close())


# ---------------------------------------------------------------- PairSpec·runner 통합

def test_pair_with_claude_bear_dispatches_bear_once_and_separates_subscription_cost(tmp_path):
    ledger, guard, runner, spec, op, client = claude_setup(tmp_path)
    row = asyncio.run(runner.run(spec))
    assert len(client.calls) == 1 and len(op.calls) == 2
    assert len(reservations(ledger)) == 2 and all(s == 'settled' for (s,) in reservations(ledger))
    assert row['subscription_attempts'] == 1 and row['admitted_attempts'] == 3
    assert row['total_comparison_cost_usd'] is not None            # GPT 두 시도만 API 합계
    for arm in ('current', 'candidate'):
        bear = [a for a in row[arm]['attempts'] if a['role'] == 'bear']
        assert len(bear) == 1 and bear[0]['billing_channel'] == 'subscription' and bear[0]['ledger_state'] == 'known'
        assert bear[0]['api_cost_usd'] is None and bear[0]['subscription_estimated_cost_usd'] == 0.0123
        assert 'subscription_attempt_cost_not_api_billed' in row[arm]['reasons']
        assert 'api_cost_unknown' not in row[arm]['reasons']
        assert row[arm]['bear_verdict'] is False and row[arm]['api_cost_usd'] is not None
    assert asyncio.run(runner.run(spec)) == row and len(client.calls) == 1


def test_claude_bear_empty_response_is_not_retried_and_failed_bear_keeps_slot_state(tmp_path):
    def empty(response):
        response = _ok(response)
        response.content = ''
        return response
    ledger, guard, runner, spec, op, client = claude_setup(tmp_path, bear_script=empty)
    row = asyncio.run(runner.run(spec))
    assert len(client.calls) == 1                                   # bear_empty_retries=0
    assert row['current']['bear_verdict'] is None and row['subscription_attempts'] == 1
    ledger2, guard2, runner2, spec2, op2, client2 = claude_setup(tmp_path / 'b', bear_script=SCRIPTS['timeout'])
    row2 = asyncio.run(runner2.run(spec2))
    bear = [a for a in row2['current']['attempts'] if a['role'] == 'bear'][0]
    assert bear['success'] is False and bear['request_sent'] is True and bear['ledger_state'] == 'known'
    assert bear['api_cost_usd'] is None and reservations(ledger2).count(('settled',)) == len(reservations(ledger2))
    assert ledger2.status()['unresolved_count'] == 0                 # 구독 실패는 월 원장을 막지 않는다


def test_api_claude_bear_pair_reserves_each_attempt_exactly_once(tmp_path):
    ledger, guard, runner, spec, op, client = claude_setup(tmp_path, api=True)
    row = asyncio.run(runner.run(spec))
    assert len(client.calls) == 1 and len(reservations(ledger)) == 3
    assert row['subscription_attempts'] == 0 and row['total_comparison_cost_usd'] is not None
    bear = [a for a in row['current']['attempts'] if a['role'] == 'bear'][0]
    assert bear['billing_channel'] == 'api' and bear['api_cost_usd'] is not None


@pytest.mark.parametrize('mutate,match', [
    (lambda s: replace(s, bear=bear_controls(temperature=0.3)), 'claude_controls'),
    (lambda s: replace(s, bear=bear_controls(seed=1)), 'claude_controls'),
    (lambda s: replace(s, bear=bear_controls(reasoning_effort='high')), 'claude_controls'),
    (lambda s: replace(s, bear=bear_controls(reasoning_effort=None)), 'claude_controls'),
    (lambda s: replace(s, bear_empty_retries=None), 'claude_bear_empty_retries'),
    (lambda s: replace(s, bear_empty_retries=1), 'claude_bear_empty_retries'),
    (lambda s: replace(s, current=bear_controls('api'), candidate=bear_controls('api', max_output_tokens=300)), 'role_provider_mismatch'),
    (lambda s: replace(s, fallback_prompt='p', fallback_chain=(bear_controls('api'),)), 'role_provider_mismatch'),
    (lambda s: replace(s, bear=m.RoleControls('gemini-3.1-flash-lite', 400, None, 0.0, billing_channel='subscription')),
     'subscription_requires_claude'),
    (lambda s: replace(s, bear=m.RoleControls('gemini-3.1-flash-lite', 400, None, None)), 'temperature_required'),
])
def test_claude_bear_profile_rules_and_historical_roles_are_separate(tmp_path, mutate, match):
    ledger, guard, runner, spec, op, client = claude_setup(tmp_path)
    spec.validate()
    with pytest.raises(ValueError, match=match):
        mutate(spec).validate()
    assert client.calls == [] and op.calls == []


def test_historical_gemini_bear_spec_still_validates_and_snapshot_keeps_api_channel(tmp_path):
    from tests.test_model_comparison_runner import setup
    _, _, _, spec, _, _ = setup(tmp_path)
    spec.validate()
    snapshot = spec.snapshot()
    assert snapshot['bear']['billing_channel'] == 'api' and snapshot['bear_empty_retries'] is None
    assert spec.bear.temperature == 0.0


def test_runner_without_claude_adapter_rejects_claude_bear_before_claim(tmp_path):
    ledger, guard, runner, spec, op, client = claude_setup(tmp_path)
    plain = m.PairedComparisonRunner(guard=guard, adapter=runner.adapter, clock=lambda: NOW)
    with pytest.raises(ValueError, match='claude_adapter_required'):
        asyncio.run(plain.run(spec))
    assert plain.admitted_pair_ids(ledger.today().isoformat()) == set()
    with pytest.raises(ValueError, match='claude_single_attempt_adapter_required'):
        m.PairedComparisonRunner(guard=guard, adapter=runner.adapter, claude_adapter=runner.adapter, clock=lambda: NOW)


def test_api_adapter_rejects_claude_models_and_missing_temperature_before_dispatch():
    op, ge = FakeOpenAI(), FakeGemini()
    adapter = m.SingleAttemptAdapter(openai=op, gemini=ge, clock=lambda: NOW)
    with pytest.raises(ValueError, match='unsupported_provider_model'):
        asyncio.run(adapter(request('api')))
    with pytest.raises(ValueError, match='temperature_required'):
        asyncio.run(adapter(ComparisonRequest(model='gpt-5-mini', prompt='p', system='s', max_output_tokens=10,
                                              reasoning_effort='low', temperature=None)))
    assert op.calls == [] and ge.calls == []


def test_worker_activation_remains_refused():
    from src.analytics import model_comparison_worker as w
    with pytest.raises(ValueError, match='provider_disabled'):
        w._raw_clients()
