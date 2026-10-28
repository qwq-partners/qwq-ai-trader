"""역할 라우팅은 실제 전송 본문·격리 원장으로 검증한다. 외부 요청 없음."""
import asyncio
import json
import sqlite3
from dataclasses import FrozenInstanceError, replace

import pytest

from src.utils.llm import LLMConfig, LLMManager, LLMProvider
from tests.test_llm_transport_contract import Session, Reply, gemini_reply, openai_reply


def manager(tmp_path, *, spent=0, monthly=True):
    m = LLMManager(LLMConfig(
        openai_api_key='synthetic', gemini_api_key='synthetic',
        monthly_budget_usd=100 if monthly else None,
        budget_state_path=str(tmp_path / 'budget.sqlite3') if monthly else None,
        budget_policy_enabled=monthly,
    ), audit_dir=tmp_path / 'audit')
    if monthly and spent:
        month = m.monthly_budget.status()['month']
        with sqlite3.connect(m.monthly_budget.path) as db:
            db.execute("INSERT INTO reservations VALUES(?,?,?,0,'settled',?,?,?)",
                       ('seed', month, 'gpt-5-mini', int(spent * 1e9), month + '-01', month + '-01'))
    m.openai._session = Session(openai_reply())
    m.gemini._session = Session(gemini_reply())
    return m


@pytest.mark.asyncio
@pytest.mark.parametrize('role,model,cap,control', [
    ('report_one_line', 'gpt-5-mini', 512, 'minimal'),
    ('morning_brief', 'gpt-5-mini', 1600, 'minimal'),
    ('post_exit_research', 'gpt-6.1-sol', 4096, 'medium'),
])
@pytest.mark.parametrize('spent', [0, 80])
async def test_role_exact_payload_and_durable_attribution(tmp_path, role, model, cap, control, spent):
    m = manager(tmp_path, spent=spent)
    result = await m.complete_role('내용', role=role, system='범위')
    assert result.success and result.requested_model == model
    assert result.budget_policy_mode == ('normal' if spent == 0 else 'economy')
    priority = 'research' if role == 'post_exit_research' else 'standard'
    assert result.budget_priority == priority
    payload = m.openai._session.payloads[0]
    assert payload['model'] == model and payload['max_completion_tokens'] == cap
    assert payload['reasoning_effort'] == control and 'temperature' not in payload
    assert len(result.attempt_trail) == 1
    assert result.attempt_trail[0]['task'] == role
    with sqlite3.connect(m.monthly_budget.path) as db:
        row = db.execute('SELECT p.task,p.priority,r.model,r.state FROM policy_attempts p '
                         'JOIN reservations r USING(token)').fetchone()
    assert row == (role, priority, model, 'settled')
    rows = [json.loads(line) for p in (tmp_path / 'audit').glob('*.jsonl') for line in p.read_text().splitlines()]
    assert len(rows) == 1 and rows[0]['task'] == role
    # Fake sessions have no close lifecycle; remove them before draining the real manager.
    m.openai._session = m.gemini._session = None
    await m.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('role', ['report_one_line', 'morning_brief', 'post_exit_research'])
@pytest.mark.parametrize('spent,reason', [(95, 'essential_budget_reserved'), (100, 'monthly_limit_exceeded')])
async def test_role_monthly_stop_never_sends(tmp_path, role, spent, reason):
    m = manager(tmp_path, spent=spent)
    result = await m.complete_role('p', role=role)
    assert result.budget_block_reason == reason and not result.request_sent
    assert m.openai._session.payloads == []
    assert m.stats['fallback_count'] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize('content', ['', 'not JSON'])
async def test_role_never_retries_empty_or_invalid_json(tmp_path, content):
    m = manager(tmp_path)
    m.openai._session = Session(openai_reply(content))
    response = await m.complete_role('p', role='report_one_line')
    assert response.content == content
    assert len(m.openai._session.payloads) == 1 and len(response.attempt_trail) == 1


@pytest.mark.asyncio
async def test_role_known_refusal_settles_once_without_fallback(tmp_path):
    m = manager(tmp_path)
    reply = openai_reply()
    reply.data['choices'][0]['message']['refusal'] = 'synthetic refusal'
    m.openai._session = Session(reply)
    response = await m.complete_role('p', role='report_one_line')
    assert not response.success and response.usage_known
    assert len(response.attempt_trail) == 1 and m.monthly_budget.status()['settled_count'] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('override', ['model', 'max_tokens', 'budget_priority', 'reasoning_effort',
                                      'thinking_level', 'retry_on_empty', 'timeout_seconds', '_attempt_role'])
async def test_role_does_not_accept_callsite_overrides(tmp_path, override):
    m = manager(tmp_path)
    with pytest.raises(TypeError):
        await m.complete_role('p', role='report_one_line', **{override: 'unexpected'})
    assert m.openai._session.payloads == []


@pytest.mark.asyncio
async def test_unknown_role_rejected_before_admission(tmp_path):
    m = manager(tmp_path)
    with pytest.raises(ValueError, match='role'):
        await m.complete_role('p', role='unregistered')
    assert m.monthly_budget.status()['reserved_usd'] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize('role,bound', [('report_one_line', 32768), ('morning_brief', 65536),
                                       ('post_exit_research', 131072)])
async def test_utf8_limit_counts_system_and_prompt_before_admission(tmp_path, role, bound):
    m = manager(tmp_path)
    response = await m.complete_role('a' * (bound - 2), role=role, system='한')
    assert not response.success and not response.request_sent
    assert response.error == 'Role input byte limit exceeded'
    assert m.monthly_budget.status()['reserved_usd'] == 0
    assert m.openai._session.payloads == []
    valid = await m.complete_role('a' * (bound - 3), role=role, system='한')
    assert valid.success


@pytest.mark.asyncio
@pytest.mark.parametrize('fault', ['price', 'control'])
async def test_role_preflight_fails_closed_even_without_monthly_budget(tmp_path, monkeypatch, fault):
    import src.utils.llm as llm
    m = manager(tmp_path, monthly=False)
    if fault == 'price':
        monkeypatch.setattr(llm, 'rate_quote', lambda *a, **kw: None)
    else:
        monkeypatch.setitem(llm._OPENAI_EFFORTS, 'gpt-5-mini', {'low'})
    with pytest.raises(ValueError):
        await m.complete_role('p', role='report_one_line')
    assert m.openai._session.payloads == []


@pytest.mark.asyncio
async def test_closing_role_is_unsent(tmp_path):
    m = manager(tmp_path)
    m._closing = True
    result = await m.complete_role('p', role='report_one_line')
    assert result.budget_block_reason == 'manager_closing' and not result.request_sent
    assert m.openai._session.payloads == []


def short_role(monkeypatch, seconds=0.5):
    from src.utils import llm_roles
    profiles = dict(llm_roles.ROLE_PROFILES)
    role = llm_roles.LLMRole.REPORT_ONE_LINE
    profiles[role] = replace(profiles[role], timeout_seconds=seconds)
    monkeypatch.setattr(llm_roles, 'ROLE_PROFILES', profiles)


@pytest.mark.asyncio
async def test_role_timeout_after_send_preserves_accounting_and_blocks_later(tmp_path, monkeypatch):
    short_role(monkeypatch)
    m = manager(tmp_path)
    class SlowReply(Reply):
        async def json(self):
            await asyncio.Event().wait()
    m.openai._session = Session(SlowReply({}))
    response = await m.complete_role('p', role='report_one_line')
    assert not response.success and response.request_sent and not response.usage_known
    assert len(response.attempt_trail) == 1
    assert m.daily_usage.unknown_usage_requests == 1
    assert m.monthly_budget.status()['unresolved_count'] == 1
    blocked = await m.complete_role('p', role='report_one_line')
    assert blocked.budget_block_reason == 'daily_unknown_cost'
    assert len(m.openai._session.payloads) == 1


@pytest.mark.asyncio
async def test_role_external_cancel_preserves_trail_and_original_cancellation(tmp_path):
    m = manager(tmp_path)
    entered = asyncio.Event()
    class WaitingReply(Reply):
        async def json(self):
            entered.set()
            await asyncio.Event().wait()
    m.openai._session = Session(WaitingReply({}))
    task = asyncio.create_task(m.complete_role('p', role='report_one_line'))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError) as exc:
        await task
    assert len(exc.value._llm_trail) == 1
    assert len(exc.value._llm_response.attempt_trail) == 1
    assert m.monthly_budget.status()['unresolved_count'] == 1


@pytest.mark.asyncio
async def test_deadline_expired_during_sync_admission_never_sends(tmp_path, monkeypatch):
    import time
    short_role(monkeypatch, seconds=0.01)
    m = manager(tmp_path)
    reserve = m.monthly_budget.reserve
    def slow_reserve(*a, **kw):
        token = reserve(*a, **kw)
        time.sleep(0.02)
        return token
    monkeypatch.setattr(m.monthly_budget, 'reserve', slow_reserve)
    response = await m.complete_role('p', role='report_one_line')
    assert not response.success and not response.request_sent
    assert m.openai._session.payloads == []
    assert m.monthly_budget.status()['reserved_usd'] == 0
    assert m.monthly_budget.status()['unresolved_count'] == 0


def test_role_table_is_versioned_and_immutable():
    from src.utils.llm_roles import LLMRole, ROLE_POLICY_VERSION, ROLE_PROFILES
    assert ROLE_POLICY_VERSION == 'engine-roles-v2-openai'
    with pytest.raises(TypeError):
        ROLE_PROFILES[LLMRole.REPORT_ONE_LINE] = None
    with pytest.raises(FrozenInstanceError):
        ROLE_PROFILES[LLMRole.REPORT_ONE_LINE].max_output_tokens = 100000


@pytest.mark.asyncio
@pytest.mark.parametrize('expert,args', [
    ('macro', ({'us10y': 4.0}, '범위')),
    ('kr', ('범위', {'krw_usd': 1300})),
    ('global', ('범위', {'semiconductors': 1.2})),
])
async def test_narrative_experts_use_one_line_role(tmp_path, expert, args):
    from src.experts.macro_economist import MacroEconomist
    from src.experts.kr_economy_expert import KREconomyExpert
    from src.experts.global_micro_expert import GlobalMicroExpert
    cls = {'macro': MacroEconomist, 'kr': KREconomyExpert, 'global': GlobalMicroExpert}[expert]
    obj = object.__new__(cls)
    obj.llm_manager = manager(tmp_path)
    assert await obj._llm_synthesize(*args) == 'ok'
    payload = obj.llm_manager.openai._session.payloads[0]
    assert payload['model'] == 'gpt-5-mini' and payload['max_completion_tokens'] == 512
    assert payload['reasoning_effort'] == 'minimal'


@pytest.mark.asyncio
async def test_post_exit_role_reaches_actual_transport_with_research_priority(tmp_path):
    from src.analytics.post_exit_review import PostExitReviewer
    m = manager(tmp_path)
    reviewer = PostExitReviewer(llm_manager=m)
    result = await reviewer._generate_llm_insights(
        [], {'missed': [], 'avoided': [], 'neutral': []}, {'by_strategy': {}, 'by_exit_type': {}})
    assert result == 'ok'
    payload = m.openai._session.payloads[0]
    assert payload['model'] == 'gpt-6.1-sol' and payload['max_completion_tokens'] == 4096
    with sqlite3.connect(m.monthly_budget.path) as db:
        assert db.execute('SELECT task,priority FROM policy_attempts').fetchone() == ('post_exit_research', 'research')


@pytest.mark.asyncio
async def test_morning_brief_uses_no_llm_role_or_transport(tmp_path, monkeypatch):
    from src.analytics.daily_report import DailyReportGenerator
    from tests.test_morning_brief import US_QUOTES, SECTOR_SIGNALS
    monkeypatch.setattr(
        'src.utils.llm.get_llm_manager',
        lambda: (_ for _ in ()).throw(AssertionError('morning summary must not load LLM manager')),
    )
    record = await object.__new__(DailyReportGenerator).build_morning_brief(
        quotes=US_QUOTES, sector_signals=SECTOR_SIGNALS, avg_pct=1.4,
        us_date_str='2026-09-12', mood='강세')
    assert record['scope'] == 'us_close_only'
    assert record['model'] == 'deterministic-source-summary'
    assert record['raw_text'] == '' and record['body_full'] == record['text']
    assert record['claims']['open_direction'] is None


@pytest.mark.asyncio
async def test_legacy_argon_qualification_cannot_reactivate_retired_provider(tmp_path):
    from src.utils.llm_roles import ROLE_PROFILES
    assert all(p.model != 'gemini-4-argon' for p in ROLE_PROFILES.values())
    m = manager(tmp_path, monthly=False)
    m.config.gemini_model_light = 'gemini-4-argon'
    m.gemini._session = Session(gemini_reply(modelVersion='gemini-4-argon'))
    result = await m.complete_with('p', provider=LLMProvider.GEMINI, max_tokens=2048, thinking_level='low')
    assert m.gemini._session.payloads == []
    assert result.success is False and result.request_sent is False
    assert result.provider_error_code == 'provider_disabled'
    assert result.requested_model == 'gemini-4-argon'
    assert result.resolved_model is None and result.usage_known is False


@pytest.mark.asyncio
async def test_deadline_during_settlement_keeps_completed_paid_attempt(tmp_path, monkeypatch):
    import time
    short_role(monkeypatch, seconds=0.5)
    m = manager(tmp_path)
    account = m._account_attempt
    def slow_account(*args, **kwargs):
        account(*args, **kwargs)
        time.sleep(0.6)
    monkeypatch.setattr(m, '_account_attempt', slow_account)
    response = await m.complete_role('p', role='report_one_line')
    assert response.request_sent and response.usage_known
    assert response.input_tokens == 100 and response.output_tokens == 50
    assert len(response.attempt_trail) == 1
    assert m.daily_usage.unknown_usage_requests == 0
    assert m.monthly_budget.status()['settled_count'] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('finish,refusal,content', [
    ('length', False, '잘린 설명'),
    ('content_filter', False, '거절'),
    (None, False, '종료 불명'),
    ('stop', True, '거절'),
    ('stop', False, ''),
])
async def test_role_incomplete_or_refused_output_is_not_usable_but_billed_once(
        tmp_path, finish, refusal, content):
    m = manager(tmp_path)
    reply = openai_reply(content)
    reply.data['choices'][0]['finish_reason'] = finish
    if refusal:
        reply.data['choices'][0]['message']['refusal'] = 'synthetic refusal'
    m.openai._session = Session(reply)
    role = 'report_one_line'
    result = await m.complete_role('p', role=role)
    assert not result.success and result.request_sent and result.usage_known
    assert result.finish_reason == finish
    assert result.content == content
    assert result.output_tokens == 50 and len(result.attempt_trail) == 1
    assert not result.attempt_trail[0]['success']
    assert m.monthly_budget.status()['settled_count'] == 1
    assert m.monthly_budget.status()['unresolved_count'] == 0
    assert len(m.openai._session.payloads) == 1
    rows = [json.loads(line) for p in (tmp_path / 'audit').glob('*.jsonl') for line in p.read_text().splitlines()]
    assert len(rows) == 1 and rows[0]['outcome'] == 'error'


@pytest.mark.asyncio
async def test_legacy_complete_with_keeps_truncation_contract(tmp_path):
    m = manager(tmp_path)
    reply = openai_reply('기존 부분본문')
    reply.data['choices'][0]['finish_reason'] = 'length'
    m.openai._session = Session(reply)
    result = await m.complete_with('p', provider=LLMProvider.OPENAI)
    assert result.success and result.content == '기존 부분본문'


@pytest.mark.asyncio
async def test_production_role_audit_is_private_metadata_only_without_enabling_legacy(tmp_path):
    import stat
    m = manager(tmp_path)
    m.audit_dir = None
    reply = openai_reply('private-response', model='provider-reported-version')
    reply.data['choices'][0]['finish_reason'] = 'length'
    m.openai._session = Session(reply)
    response = await m.complete_role('private-prompt', role='report_one_line')
    assert not response.success
    audit_dir = tmp_path / 'role-audit'
    files = list(audit_dir.glob('llm-audit-*.jsonl'))
    assert len(files) == 1
    row = json.loads(files[0].read_text())
    assert row['task'] == 'report_one_line' and row['role_policy_version'] == 'engine-roles-v2-openai'
    assert row['requested_reasoning_effort'] == 'minimal'
    assert row['resolved_model'] == 'provider-reported-version'
    assert row['outcome'] == 'error' and row['elapsed_ms'] >= 0
    assert 'private-prompt' not in files[0].read_text() and 'private-response' not in files[0].read_text()
    assert stat.S_IMODE(audit_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(files[0].stat().st_mode) == 0o600
    await m.complete_with('legacy', provider=LLMProvider.OPENAI)
    assert len(files[0].read_text().splitlines()) == 1


@pytest.mark.asyncio
async def test_no_monthly_path_does_not_enable_implicit_audit(tmp_path):
    m = manager(tmp_path, monthly=False)
    m.audit_dir = None
    response = await m.complete_role('p', role='report_one_line')
    assert response.success and response.resolved_model is None
    assert not (tmp_path / 'role-audit').exists()


@pytest.mark.asyncio
async def test_role_audit_write_failure_does_not_lose_budget_settlement(tmp_path):
    m = manager(tmp_path)
    m.audit_dir = None
    (tmp_path / 'role-audit').write_text('synthetic path collision')
    response = await m.complete_role('p', role='report_one_line')
    assert response.success and len(response.attempt_trail) == 1
    assert m.monthly_budget.status()['settled_count'] == 1


@pytest.mark.asyncio
async def test_implicit_role_audit_never_follows_directory_symlink_for_retention(tmp_path):
    m = manager(tmp_path)
    m.audit_dir = None
    other = tmp_path / 'unrelated'
    other.mkdir()
    historical = other / 'llm-audit-20000101.jsonl'
    historical.write_text('must remain')
    (tmp_path / 'role-audit').symlink_to(other, target_is_directory=True)
    response = await m.complete_role('p', role='report_one_line')
    assert response.success and historical.read_text() == 'must remain'
    assert list(other.iterdir()) == [historical]
    assert m.monthly_budget.status()['settled_count'] == 1
