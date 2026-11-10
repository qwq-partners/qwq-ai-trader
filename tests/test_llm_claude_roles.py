"""L01 Task 4: 고정 역할·heavy/light task 슬롯의 Anthropic API 라우팅 — 합성 HTTP 응답만, 외부 요청 없음."""
import asyncio
import json
import sqlite3

import pytest

from src.utils.llm import LLMConfig, LLMManager, LLMProvider, LLMTask, CLAUDE_MODELS
from src.utils.llm_roles import LLMRole, ROLE_POLICY_VERSION, ROLE_PROFILES
from tests.test_claude_transport import Session as ClaudeSession, api_body
from tests.test_llm_transport_contract import Session as OpenAISession, openai_reply

OPUS, FABLE = 'claude-opus-5-5', 'claude-fable-5-1'
JSON_HINT = 'JSON'


class EchoSession(ClaudeSession):
    """요청한 모델을 그대로 응답 model 로 돌려준다(여러 역할을 한 매니저로 검사할 때)."""
    def post(self, url, **kwargs):
        if isinstance(self.reply.body, dict) and 'model' in self.reply.body:
            self.reply.body['model'] = kwargs['json']['model']
        return super().post(url, **kwargs)


class SeqSession(ClaudeSession):
    """호출 순서대로 다른 본문을 돌려준다(시장 보고 → 웹 리포트 두 역할 E2E)."""
    def __init__(self, *bodies):
        super().__init__(bodies[0]); self.bodies = list(bodies)
    def post(self, url, **kwargs):
        from tests.test_claude_transport import Reply
        self.reply = Reply(self.bodies.pop(0)); return super().post(url, **kwargs)


def claude_body(model=FABLE, text='{"ok": true}', stop='end_turn', **extra):
    body = api_body(); body['model'] = model; body['stop_reason'] = stop
    body['content'] = [{'type': 'text', 'text': text}] if text is not None else []
    body.update(extra); return body


def manager(tmp_path, body=None, *, routes=None, spent=0):
    cfg = dict(openai_api_key='synthetic', anthropic_api_key='synthetic', monthly_budget_usd=100,
               budget_state_path=str(tmp_path / 'budget.sqlite3'), budget_policy_enabled=True,
               missing_cost_policy='log_and_continue')   # 운영값: 비용 미확인은 기록 후 계속
    if routes:
        cfg.update(task_route_heavy=routes.get('heavy', 'openai'), task_route_light=routes.get('light', 'openai'))
    m = LLMManager(LLMConfig(**cfg), audit_dir=tmp_path / 'audit')
    m._LLM_LOG_DIR = tmp_path / 'responses'
    if spent:
        month = m.monthly_budget.status()['month']
        with sqlite3.connect(m.monthly_budget.path) as db:
            db.execute("INSERT INTO reservations VALUES(?,?,?,0,'settled',?,?,?)",
                       ('seed', month, 'gpt-5-mini', int(spent * 1e9), month + '-01', month + '-01'))
    m._ensure_claude_clients()
    m.anthropic._session = ClaudeSession(body if body is not None else claude_body())
    m.openai._session = OpenAISession(openai_reply('fallback'))
    return m


def claude_request(m, index=0):
    return m.anthropic._session.calls[index][1]['json']


def test_role_table_v6_moves_report_roles_to_anthropic_api():
    assert ROLE_POLICY_VERSION == 'engine-roles-v6-claude-roles'
    expect = {LLMRole.REPORT_ONE_LINE: ('anthropic', OPUS, 512, 20.0, 'low', None),
              LLMRole.MARKET_BRIEF: ('anthropic', FABLE, 4096, 90.0, 'medium', 'json_object'),
              LLMRole.POST_EXIT_RESEARCH: ('anthropic', FABLE, 4096, 90.0, 'medium', None),
              LLMRole.WEB_REPORT: ('anthropic', FABLE, 12288, 180.0, 'medium', 'json_object'),
              LLMRole.MORNING_BRIEF: ('openai', 'gpt-5-mini', 1600, 45.0, 'minimal', None)}
    for role, (provider, model, cap, timeout, effort, fmt) in expect.items():
        p = ROLE_PROFILES[role]
        assert (p.provider, p.model, p.max_output_tokens, p.timeout_seconds, p.reasoning_effort, p.response_format) == \
               (provider, model, cap, timeout, effort, fmt), role
    assert all(ROLE_PROFILES[r].model in CLAUDE_MODELS for r in expect if expect[r][0] == 'anthropic')


@pytest.mark.asyncio
@pytest.mark.parametrize('role,model,cap,effort,json_mode,priority', [
    ('report_one_line', OPUS, 512, 'low', False, 'standard'),
    ('market_brief', FABLE, 4096, 'medium', True, 'standard'),
    ('post_exit_research', FABLE, 4096, 'medium', False, 'research'),
    ('web_report', FABLE, 12288, 'medium', True, 'standard'),
])
async def test_anthropic_role_exact_payload_and_durable_attribution(tmp_path, role, model, cap, effort, json_mode, priority):
    m = manager(tmp_path, claude_body(model))
    result = await m.complete_role('내용', role=role, system='범위')
    assert result.success and result.requested_model == model and result.resolved_model == model
    assert result.transport == 'anthropic_messages' and result.billing_channel == 'api'
    assert result.budget_priority == priority and result.budget_policy_mode == 'normal'
    body = claude_request(m)
    assert body['model'] == model and body['max_tokens'] == cap and body['output_config'] == {'effort': effort}
    assert ('thinking' in body) == (model == OPUS)          # Fable: adaptive 상시라 미지정
    assert 'response_format' not in body and 'temperature' not in body
    assert body['system'].startswith('범위') and (JSON_HINT in body['system']) == json_mode
    assert body['messages'] == [{'role': 'user', 'content': '내용'}]
    assert len(result.attempt_trail) == 1 and result.attempt_trail[0]['task'] == role
    with sqlite3.connect(m.monthly_budget.path) as db:
        row = db.execute('SELECT p.task,p.priority,r.model,r.state FROM policy_attempts p JOIN reservations r USING(token)').fetchone()
    assert row == (role, priority, model, 'settled')
    rows = [json.loads(l) for p in (tmp_path / 'audit').glob('*.jsonl') for l in p.read_text().splitlines()]
    assert len(rows) == 1 and rows[0]['task'] == role
    m.anthropic._session = m.openai._session = None
    await m.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('fault', ['refusal', 'max_tokens', 'fenced_json', 'identity', 'http500'])
async def test_anthropic_role_failures_bill_once_and_never_fall_back(tmp_path, fault):
    body = claude_body()
    if fault == 'refusal':
        body.update(stop_reason='refusal', content=[], stop_details={'type': 'refusal', 'category': None, 'explanation': None})
    elif fault == 'max_tokens': body['stop_reason'] = 'max_tokens'
    elif fault == 'fenced_json': body['content'] = [{'type': 'text', 'text': '```json\n{"ok": true}\n```'}]
    elif fault == 'identity': body['model'] = OPUS
    m = manager(tmp_path, body)
    if fault == 'http500':
        m.anthropic._session = ClaudeSession({'error': {'type': 'api_error', 'message': 'synthetic'}}, 500)
    response = await m.complete_role('p', role='market_brief')
    assert not response.success and response.request_sent
    assert len(m.anthropic._session.calls) == 1 and m.openai._session.payloads == []
    assert len(response.attempt_trail) == 1 and m.stats['fallback_count'] == 0
    status = m.monthly_budget.status()
    # 비용 미확인(HTTP 500, usage 없음)은 정산 대신 미해결로 남고 예약은 풀린다 — 어느 쪽이든 1건·재예약 없음.
    assert status['reserved_usd'] == 0 and status['settled_count'] + status['unresolved_count'] == 1
    if fault == 'refusal': assert response.refusal_detected
    if fault == 'fenced_json': assert response.error == 'Invalid JSON mode response' and response.content.startswith('```')
    if fault == 'identity': assert response.transport_error_kind == 'identity_mismatch'


@pytest.mark.asyncio
@pytest.mark.parametrize('spent,reason', [(95, 'essential_budget_reserved'), (100, 'monthly_limit_exceeded')])
async def test_anthropic_role_monthly_stop_never_sends(tmp_path, spent, reason):
    m = manager(tmp_path, spent=spent)
    result = await m.complete_role('p', role='web_report')
    assert result.budget_block_reason == reason and not result.request_sent
    assert m.anthropic._session.calls == [] and m.openai._session.payloads == []


@pytest.mark.asyncio
async def test_default_task_routes_stay_openai(tmp_path):
    m = manager(tmp_path)
    m.openai._session = OpenAISession(openai_reply('{"themes": []}'))
    assert await m.complete_json('p', task=LLMTask.THEME_DETECTION, response_format={'type': 'json_object'}) == {'themes': []}
    assert m.anthropic._session.calls == [] and m.openai._session.payloads[0]['model'] == 'gpt-5-mini'


@pytest.mark.asyncio
@pytest.mark.parametrize('task,model,effort,weight', [
    (LLMTask.THEME_DETECTION, OPUS, 'low', 'light'), (LLMTask.QUICK_CLASSIFY, OPUS, 'low', 'light'),
    (LLMTask.WIKI_INGEST, OPUS, 'low', 'light'), (LLMTask.NEWS_SUMMARY, OPUS, 'low', 'light'),
    (LLMTask.QUICK_ANALYSIS, OPUS, 'low', 'light'),
    (LLMTask.TRADE_REVIEW, FABLE, 'medium', 'heavy'), (LLMTask.STRATEGY_ANALYSIS, FABLE, 'medium', 'heavy'),
    (LLMTask.MARKET_ANALYSIS, FABLE, 'medium', 'heavy'),
])
async def test_configured_task_routes_use_anthropic_api_with_slot_models(tmp_path, task, model, effort, weight):
    m = manager(tmp_path, claude_body(model, '{"themes": []}'), routes={'heavy': 'anthropic', 'light': 'anthropic'})
    kwargs = {'response_format': {'type': 'json_object'}} if task is LLMTask.THEME_DETECTION else {}
    response = await m.complete('p', task=task, system='s', **kwargs)
    assert response.success and response.requested_model == model and response.transport == 'anthropic_messages'
    body = claude_request(m)
    assert body['model'] == model and body['output_config'] == {'effort': effort}
    assert body['max_tokens'] == m.config.max_output_tokens and ('thinking' in body) == (model == OPUS)
    assert 'response_format' not in body and (JSON_HINT in body['system']) == bool(kwargs)
    assert m.openai._session.payloads == []
    with sqlite3.connect(m.monthly_budget.path) as db:
        assert db.execute('SELECT model FROM reservations').fetchone() == (model,)


@pytest.mark.asyncio
async def test_light_only_route_keeps_heavy_on_openai(tmp_path):
    m = manager(tmp_path, routes={'light': 'anthropic'})
    m.openai._session = OpenAISession(openai_reply('heavy'))
    response = await m.complete('p', task=LLMTask.TRADE_REVIEW)
    assert response.success and m.openai._session.payloads[0]['model'] == 'gpt-5.6-sol'
    assert m.anthropic._session.calls == []


@pytest.mark.asyncio
async def test_heavy_anthropic_failure_falls_back_to_openai_light_but_refusal_does_not(tmp_path):
    m = manager(tmp_path, routes={'heavy': 'anthropic'})
    m.anthropic._session = ClaudeSession({'error': {'type': 'overloaded_error', 'message': 'x'}}, 529)
    response = await m.complete('p', task=LLMTask.TRADE_REVIEW)
    assert response.success and response.content == 'fallback'
    assert m.openai._session.payloads[0]['model'] == 'gpt-5-mini' and m.stats['fallback_count'] == 1
    assert [a['role'] for a in response.attempt_trail] == ['primary', 'fallback']

    m2 = manager(tmp_path / 'b', claude_body(FABLE, None, 'refusal', stop_details={'type': 'refusal'}), routes={'heavy': 'anthropic'})
    refused = await m2.complete('p', task=LLMTask.TRADE_REVIEW)
    assert not refused.success and refused.refusal_detected and m2.openai._session.payloads == []


@pytest.mark.asyncio
async def test_light_anthropic_failure_has_no_fallback(tmp_path):
    m = manager(tmp_path, routes={'light': 'anthropic'})
    m.anthropic._session = ClaudeSession({'error': {'type': 'api_error', 'message': 'x'}}, 500)
    response = await m.complete('p', task=LLMTask.THEME_DETECTION)
    assert not response.success and m.openai._session.payloads == [] and m.stats['fallback_count'] == 0


def test_config_parses_task_routes_and_slot_models_and_rejects_unknown():
    cfg = LLMConfig.from_config({'llm': {'task_routes': {'heavy': 'anthropic', 'light': 'anthropic'},
                                         'claude_model_heavy': FABLE, 'claude_model_light': OPUS}})
    assert (cfg.task_route_heavy, cfg.task_route_light) == ('anthropic', 'anthropic')
    assert (cfg.claude_model_heavy, cfg.claude_model_light, cfg.claude_model) == (FABLE, OPUS, OPUS)
    default = LLMConfig.from_config({})
    assert (default.task_route_heavy, default.task_route_light) == ('openai', 'openai')
    for bad in ({'task_routes': {'heavy': 'gemini'}}, {'task_routes': {'light': ''}}, {'claude_model_heavy': 'claude-fable-5-2'},
                {'claude_model_light': 'claude-sonnet-5-5'},
                # 독립 리뷰 1차 P2: 명시된 falsy 값은 기본값으로 치환하지 않고 거부한다.
                {'claude_model_heavy': ''}, {'claude_model_light': 0}, {'claude_model_heavy': False},
                {'task_routes': False}, {'task_routes': {'heavy': False}}, {'task_routes': 'anthropic'}):
        with pytest.raises(ValueError):
            LLMConfig.from_config({'llm': bad})


def test_production_default_yml_routes_heavy_and_light_to_anthropic():
    import yaml
    llm = yaml.safe_load(open('config/default.yml'))['llm']
    cfg = LLMConfig.from_config(llm)
    assert (cfg.task_route_heavy, cfg.task_route_light) == ('anthropic', 'anthropic')
    assert (cfg.claude_model_heavy, cfg.claude_model_light, cfg.claude_model) == (FABLE, OPUS, OPUS)


@pytest.mark.asyncio
@pytest.mark.parametrize('status,error_type,falls_back', [
    (400, 'invalid_request_error', False),   # 크레딧 소진·요청 오류 → 유료 OpenAI 승격 없음(독립 리뷰 1차 P1)
    (402, 'billing_error', False), (401, 'authentication_error', False), (403, 'permission_error', False),
    (408, 'api_error', True), (429, 'rate_limit_error', True), (500, 'api_error', True),
    (503, 'api_error', True), (529, 'overloaded_error', True),
])
async def test_heavy_openai_fallback_only_on_availability_failure(tmp_path, status, error_type, falls_back):
    m = manager(tmp_path, routes={'heavy': 'anthropic'})
    m.anthropic._session = ClaudeSession({'error': {'type': error_type, 'message': 'x'}}, status)
    response = await m.complete('p', task=LLMTask.TRADE_REVIEW)
    assert response.success is falls_back
    assert len(m.openai._session.payloads) == (1 if falls_back else 0)
    assert m.stats['fallback_count'] == (1 if falls_back else 0)
    assert len(m.anthropic._session.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('fault', ['identity', 'incomplete', 'tool_use'])
async def test_heavy_non_availability_failures_never_reach_openai(tmp_path, fault):
    body = claude_body(FABLE, 'partial')
    if fault == 'identity': body['model'] = OPUS
    elif fault == 'incomplete': body['stop_reason'] = 'max_tokens'
    elif fault == 'tool_use': body['content'] = [{'type': 'tool_use'}]
    m = manager(tmp_path, body, routes={'heavy': 'anthropic'})
    response = await m.complete('p', task=LLMTask.TRADE_REVIEW)
    assert not response.success and m.openai._session.payloads == [] and m.stats['fallback_count'] == 0


@pytest.mark.asyncio
async def test_anthropic_task_uses_config_timeout_not_bear_default(tmp_path):
    # 독립 리뷰 1차 P1: 일반 task 의 시한은 OpenAI 와 같은 timeout_seconds(기본 120초). 전송 기본 12초는 Bear 경계.
    m = manager(tmp_path, claude_body(OPUS), routes={'light': 'anthropic'})
    await m.complete('p', task=LLMTask.QUICK_CLASSIFY)
    total = m.anthropic._session.calls[0][1]['timeout'].total
    assert m.config.timeout_seconds - 2 <= total <= m.config.timeout_seconds
    m.anthropic._session = ClaudeSession(claude_body(FABLE))
    await m.complete_role('p', role='web_report')
    assert 178 <= m.anthropic._session.calls[0][1]['timeout'].total <= 180      # 역할 시한은 프로필(180초) 그대로


@pytest.mark.asyncio
@pytest.mark.parametrize('kwargs', [{'thinking_level': 'high'}, {'response_format': {'type': 'text'}},
                                    {'reasoning_effort': 'xhigh'}, {'seed': 'x'},
                                    {'seed': 0}, {'seed': 42}])   # 독립 리뷰 3차 P2: 유효한 정수 seed 도 거부(조용한 무시 금지)
async def test_anthropic_task_rejects_unsupported_controls_before_send(tmp_path, kwargs):
    # 독립 리뷰 1차 P2: OpenAI 경로와 같은 공개 계약 — 잘못된 명시 제어값은 ValueError, 전송·예약 0.
    m = manager(tmp_path, routes={'heavy': 'anthropic', 'light': 'anthropic'})
    with pytest.raises(ValueError):
        await m.complete('p', task=LLMTask.TRADE_REVIEW, **kwargs)
    assert m.anthropic._session.calls == [] and m.openai._session.payloads == []
    status = m.monthly_budget.status()
    assert status['reserved_usd'] == 0 and status['settled_count'] == 0 and status['unresolved_count'] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize('status,falls_back', [(400, False), (402, False), (403, False), (429, True), (529, True)])
async def test_body_timeout_after_error_status_keeps_fallback_decision_by_status(tmp_path, status, falls_back):
    # 독립 리뷰 2차 P1: 금지 상태(400/402/403)를 받은 뒤 본문 시한 초과는 api_timeout 이어도 유료 승격 없음.
    from tests.test_claude_transport import Reply as ClaudeReply
    class BodyTimeout(ClaudeReply):
        async def json(self): raise TimeoutError()
    m = manager(tmp_path, routes={'heavy': 'anthropic'})
    m.anthropic._session = ClaudeSession(); m.anthropic._session.reply = BodyTimeout({}, status)
    response = await m.complete('p', task=LLMTask.TRADE_REVIEW)
    assert response.success is falls_back and len(m.openai._session.payloads) == (1 if falls_back else 0)
    if not falls_back:
        assert response.transport_error_kind == 'api_timeout' and response.http_status == status


@pytest.mark.asyncio
@pytest.mark.parametrize('kwargs,prompt', [({'max_tokens': None}, 'p'), ({'max_tokens': 0}, 'p'),
                                           ({'max_tokens': 400}, 'x' * 65537)])
async def test_bear_boundaries_stay_independent_of_task_defaults(tmp_path, kwargs, prompt):
    # 독립 리뷰 2차 P2: Bear(complete_with ANTHROPIC)는 출력 정수 1..400·입력 64KiB 를 예약·전송 전에 스스로 막는다.
    m = manager(tmp_path, claude_body(OPUS), routes={'heavy': 'anthropic', 'light': 'anthropic'})
    response = await m.complete_with(prompt, LLMProvider.ANTHROPIC, **kwargs)
    assert not response.success and not response.request_sent and response.transport_error_kind == 'invalid_request'
    assert m.anthropic._session.calls == [] and m.monthly_budget.status()['reserved_usd'] == 0
    ok = await m.complete_with('p', LLMProvider.ANTHROPIC, max_tokens=400)
    assert ok.success and m.anthropic._session.calls[0][1]['json']['max_tokens'] == 400
