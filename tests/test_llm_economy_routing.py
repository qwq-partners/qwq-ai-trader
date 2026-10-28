import asyncio
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.utils.llm import LLMConfig, LLMManager, LLMProvider, LLMResponse, LLMTask
from src.utils.llm_budget import BudgetBlocked


def manager(tmp_path, monkeypatch, spent=0):
    m = LLMManager(LLMConfig(monthly_budget_usd=100, budget_state_path=str(tmp_path / 'cost.db'),
                             budget_policy_enabled=True))
    with sqlite3.connect(m.monthly_budget.path) as db:
        db.execute("INSERT INTO reservations VALUES('seed',?,'gpt-5-mini',0,'settled',?,'','')",
                   (m.monthly_budget.status()['month'], int(spent * 1e9)))
    for provider, client in ((LLMProvider.OPENAI, m.openai), (LLMProvider.GEMINI, m.gemini)):
        async def respond(prompt, system, *, model, _provider=provider, **kwargs):
            assert 'budget_priority' not in kwargs and 'expected_policy_mode' not in kwargs
            return LLMResponse('ok', model, _provider, input_tokens=1, output_tokens=1,
                               requested_model=model)
        monkeypatch.setattr(client, 'complete', AsyncMock(side_effect=respond))
    return m


@pytest.mark.asyncio
@pytest.mark.parametrize('spent,expected', [(0, 'gpt-5.6-sol'), (80, 'gpt-5-mini')])
async def test_summary_model_changes_and_attribution_is_durable(tmp_path, monkeypatch, spent, expected):
    m = manager(tmp_path, monkeypatch, spent)
    result = await m.complete('summary', task=LLMTask.REPORT_SUMMARY)
    assert result.success and result.model == expected
    assert result.budget_policy_mode == ('normal' if spent == 0 else 'economy')
    with sqlite3.connect(m.monthly_budget.path) as db:
        row = db.execute('SELECT p.mode,p.task,r.model FROM policy_attempts p JOIN reservations r USING(token)').fetchone()
    assert row == (result.budget_policy_mode, 'report_summary', expected)
    if spent:
        kwargs = m.openai.complete.call_args.kwargs
        assert kwargs['max_tokens'] == 4096 and kwargs['reasoning_effort'] == 'low'
    await m.close()


@pytest.mark.asyncio
async def test_intraday_budget_error_does_not_log_fabricated_yes_verdicts(tmp_path, monkeypatch):
    from src.schedulers.kr_scheduler import KRScheduler
    from src.signals.strategic import expert_panel
    from src.utils import llm
    from loguru import logger
    m = manager(tmp_path, monkeypatch, 100)
    monkeypatch.setattr(llm, 'get_llm_manager', lambda: m)
    monkeypatch.setattr(m, '_log_llm_response', lambda **kw: None)
    monkeypatch.setattr(expert_panel, 'ExpertPanel', lambda: SimpleNamespace(load_outlook=lambda: None))
    stock = SimpleNamespace(symbol='005930', name='synthetic', reasons=[], score=90)
    messages = []
    sink = logger.add(lambda msg: messages.append(str(msg)))
    try:
        result = await KRScheduler._llm_verify_intraday(SimpleNamespace(), stock, 100, 1)
    finally:
        logger.remove(sink)
    assert result is True
    assert any('예산 검증 불가' in msg for msg in messages)
    assert all('q1=Y' not in msg for msg in messages)
    await m.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('spent', [80, 95])
async def test_essential_models_provider_controls_preserved(tmp_path, monkeypatch, spent):
    m = manager(tmp_path, monkeypatch, spent)
    heavy = await m.complete('risk', task=LLMTask.STRATEGY_ANALYSIS, budget_priority='essential',
                             max_tokens=800, reasoning_effort='high')
    assert heavy.success and heavy.model == 'gpt-5.6-sol'
    assert m.openai.complete.call_args.kwargs['reasoning_effort'] == 'high'
    explicit = await m.complete_with('bear', LLMProvider.GEMINI, budget_priority='essential', temperature=0)
    assert not explicit.success and explicit.provider_error_code == 'provider_disabled'
    m.gemini.complete.assert_not_called()
    await m.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('spent,priority,reason', [
    (95, 'standard', 'essential_budget_reserved'), (95, 'research', 'essential_budget_reserved'),
    (100, 'essential', 'monthly_limit_exceeded')])
async def test_no_paid_request_or_fallback_when_blocked(tmp_path, monkeypatch, spent, priority, reason):
    m = manager(tmp_path, monkeypatch, spent)
    result = await m.complete('p', task=LLMTask.STRATEGY_ANALYSIS, budget_priority=priority)
    assert not result.success and not result.request_sent and result.budget_block_reason == reason
    m.openai.complete.assert_not_called()
    m.gemini.complete.assert_not_called()
    await m.close()


@pytest.mark.asyncio
async def test_profile_race_reselects_before_any_send_and_is_bounded(tmp_path, monkeypatch):
    m = manager(tmp_path, monkeypatch)
    reserve = m.monthly_budget.reserve
    n = 0
    def racing(*a, **kw):
        nonlocal n
        n += 1
        if n == 1:
            with sqlite3.connect(m.monthly_budget.path) as db:
                db.execute("UPDATE reservations SET actual_units=80000000000 WHERE token='seed'")
        return reserve(*a, **kw)
    monkeypatch.setattr(m.monthly_budget, 'reserve', racing)
    result = await m.complete('p', task=LLMTask.REPORT_SUMMARY)
    assert result.success and result.model == 'gpt-5-mini' and n == 2
    assert m.openai.complete.call_count == 1
    def never_stable(*a, **kw):
        nonlocal n
        n += 1
        raise BudgetBlocked('budget_profile_changed')
    monkeypatch.setattr(m.monthly_budget, 'reserve', never_stable)
    result = await m.complete('p', task=LLMTask.REPORT_SUMMARY)
    assert not result.success and n == 5 and m.openai.complete.call_count == 1
    assert m.monthly_budget.status()['reserved_usd'] == 0
    await m.close()


@pytest.mark.asyncio
async def test_json_retry_keeps_essential_priority_and_daily_block_reason(tmp_path, monkeypatch):
    m = manager(tmp_path, monkeypatch, 95)
    monkeypatch.setattr(m, '_log_llm_response', lambda **kw: None)
    replies = [LLMResponse('invalid', 'gpt-5-mini', LLMProvider.OPENAI),
               LLMResponse('{"approved":true}', 'gpt-5-mini', LLMProvider.OPENAI)]
    m.openai.complete.side_effect = replies
    result = await m.complete_json('p', task=LLMTask.STRATEGY_ANALYSIS, budget_priority='essential')
    assert result == {'approved': True} and m.openai.complete.call_count == 2
    m.config.daily_budget_usd = 0
    result = await m.complete_json('p', budget_priority='essential')
    assert result['budget_block_reason'] == 'daily_limit_exceeded'
    await m.close()


@pytest.mark.asyncio
async def test_advisory_budget_failure_is_not_approval_evidence(tmp_path, monkeypatch):
    from src.core.adversarial_validator import AdversarialValidator
    from src.core.cross_validator import CrossStrategyValidator
    m = manager(tmp_path, monkeypatch, 100)
    # Budget-only availability is a separate contract from explicit retirement.
    # Use provider-neutral unsent replies; never enable a retired transport.
    async def quota_only(prompt, *, provider=LLMProvider.OPENAI, **kwargs):
        return LLMResponse("", "", provider, success=False, request_sent=False,
                           usage_known=False, billing_status="not_sent",
                           budget_block_reason="monthly_limit_exceeded")
    budget_only = SimpleNamespace(complete_with=AsyncMock(side_effect=quota_only),
                                 complete=AsyncMock(side_effect=quota_only),
                                 get_monthly_budget_status=m.get_monthly_budget_status)
    adv = AdversarialValidator(budget_only)
    result = await adv.validate('005930', 'momentum', 90, {}, 'bear')
    assert result.approved and result.failed and result.budget_block_reason == 'monthly_limit_exceeded'
    cross = CrossStrategyValidator(llm_manager=budget_only)
    monkeypatch.setattr(cross, "_load_panel_outlook", lambda: None)
    cross._adversarial = adv
    assert await cross.llm_second_check('005930', 'momentum', 90, {}, 'bear') is True
    assert cross.last_llm_budget_status['status'] == 'unavailable'
    assert cross.last_llm_budget_status['reason'] == 'monthly_limit_exceeded'
    # Concurrent callers must not overwrite each other's provenance.
    arrived = asyncio.Event()
    async def blocked():
        await cross.llm_second_check('005930', 'momentum', 90, {}, 'bear')
        arrived.set()
        await asyncio.sleep(0.01)
        return cross.last_llm_budget_status
    async def bypass():
        await arrived.wait()
        await cross.llm_second_check('000660', 'momentum', 99, {}, 'bull')
        return cross.last_llm_budget_status
    failed, skipped = await asyncio.gather(blocked(), bypass())
    assert failed['status'] == 'unavailable' and skipped == {}
    await m.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('spent,fault,reason', [
    (80, None, 'economy_shadow_paused'), (95, None, 'economy_shadow_paused'),
    (100, None, 'monthly_limit_exceeded'), (0, 'unknown', 'unresolved_usage'),
    (0, 'breach', 'reservation_bound_breach')])
async def test_shadow_reports_authoritative_block_reason(tmp_path, monkeypatch, spent, fault, reason):
    m = manager(tmp_path, monkeypatch, spent)
    if fault:
        with sqlite3.connect(m.monthly_budget.path) as db:
            if fault == 'unknown':
                db.execute("UPDATE reservations SET state='unknown',actual_units=NULL WHERE token='seed'")
            else:
                db.execute('UPDATE settings SET bound_breach=1')
    result = await m._complete_attempt(m.openai, 'p', '', 'gpt-5-mini', 'quick_classify', 'shadow')
    assert result.budget_block_reason == reason and not result.request_sent
    m.openai.complete.assert_not_called()
    await m.close()


@pytest.mark.asyncio
async def test_research_is_attributed_and_essential_control_is_not_assumed(tmp_path, monkeypatch):
    m = manager(tmp_path, monkeypatch, 80)
    result = await m.complete('hypothesis', task=LLMTask.STRATEGY_ANALYSIS, budget_priority='research')
    assert result.success and result.model == 'gpt-5.6-sol' and result.budget_priority == 'research'
    with sqlite3.connect(m.monthly_budget.path) as db:
        assert db.execute('SELECT priority FROM policy_attempts').fetchone()[0] == 'research'
    await m.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('status,event_type', [('partial', 'llm_partial'), ('unavailable', 'llm_unavailable')])
async def test_budget_validation_provenance_survives_signal_storage_whitelist(monkeypatch, status, event_type):
    from src.core.engine import RiskManager
    from src.core.types import OrderSide
    captured = []
    sink = SimpleNamespace(log=AsyncMock(side_effect=lambda **kwargs: captured.append(kwargs)))
    monkeypatch.setattr('src.core.engine._SigLog.get', staticmethod(lambda: sink))
    rm = object.__new__(RiskManager)
    rm.engine = SimpleNamespace(_market_regime='bear')
    budget = {'status': status, 'reason': 'monthly_limit_exceeded'}
    event = SimpleNamespace(id='synthetic', side=OrderSide.BUY, symbol='005930', strategy=None,
                            score=90, metadata={'llm_budget_validation': budget})
    rm._log_sig(event, event_type=event_type)
    await asyncio.sleep(0)
    assert captured[0]['metadata']['llm_budget_validation'] == budget
    assert captured[0]['block_gate'] is None
    assert len(captured[0]['event_type']) <= 20


@pytest.mark.asyncio
async def test_manager_retains_settlement_when_post_response_policy_write_fails(tmp_path, monkeypatch):
    m = manager(tmp_path, monkeypatch)
    original_response = m.openai.complete.side_effect
    observe = m.monthly_budget._observe_policy
    def fail_policy(*a, **kw):
        raise sqlite3.OperationalError('synthetic policy table error')
    async def respond(*a, **kw):
        reply = await original_response(*a, **kw)
        monkeypatch.setattr(m.monthly_budget, '_observe_policy', fail_policy)
        return reply
    m.openai.complete.side_effect = respond
    response = await m.complete('p', task=LLMTask.REPORT_SUMMARY)
    assert response.success and m._monthly_budget_error is None
    status = m.monthly_budget.status()
    assert status['settled_count'] == 2 and status['spent_usd'] > 0 and status['unresolved_count'] == 0
    blocked = await m.complete('p', task=LLMTask.REPORT_SUMMARY)
    assert blocked.budget_block_reason == 'storage_unavailable' and not blocked.request_sent
    assert m.openai.complete.call_count == 1
    monkeypatch.setattr(m.monthly_budget, '_observe_policy', observe)
    await m.close()


@pytest.mark.asyncio
async def test_unsupported_economy_controls_are_local_block_not_provider_exception(tmp_path, monkeypatch):
    m = manager(tmp_path, monkeypatch, 80)
    m.config.openai_model_light = 'synthetic-unqualified-model'
    response = await m.complete('p', task=LLMTask.REPORT_SUMMARY)
    assert response.budget_block_reason == 'unsupported_economy_controls'
    m.openai.complete.assert_not_called()
    m.gemini.complete.assert_not_called()
    await m.close()


@pytest.mark.asyncio
async def test_provider_retirement_precedes_monthly_and_validation_quotas(tmp_path, monkeypatch):
    from src.core.cross_validator import CrossStrategyValidator
    m = manager(tmp_path, monkeypatch, 100)
    cross = CrossStrategyValidator(llm_manager=m)
    from src.core.adversarial_validator import AdversarialValidator
    cross._adversarial = AdversarialValidator(m)
    cross._daily_llm_max = 0
    records = []
    monkeypatch.setattr('src.analytics.llm_decision_record.get_decision_recorder',
                        lambda: SimpleNamespace(submit=lambda row: records.append(row) or 'accepted'))
    assert await cross.llm_second_check('005930', 'momentum', 90, {}, 'bear') is False
    assert cross.last_llm_decision['stage'] == 'provider_policy'
    assert records[0]['result']['independent_review_status'] == 'provider_disabled'
    assert cross.last_llm_decision['attempts'] == 0
    m.openai.complete.assert_not_called()
    m.gemini.complete.assert_not_called()
    with sqlite3.connect(m.monthly_budget.path) as db:
        assert db.execute('SELECT COUNT(*) FROM reservations').fetchone()[0] == 1  # seed only
    await m.close()
