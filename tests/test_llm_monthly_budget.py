"""Offline durable monthly reservation tests; all state is under tmp_path."""
import asyncio
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import pytest

from src.utils.llm import LLMConfig, LLMManager, LLMProvider, LLMResponse, LLMTask


def ledger_type():
    from src.utils import llm_budget
    return llm_budget.MonthlyBudgetLedger


class Clock:
    def __init__(self, value):
        self.value = value
    def __call__(self):
        return self.value


def new_ledger(tmp_path, limit=100, clock=None):
    return ledger_type()(tmp_path / 'budget.sqlite3', limit, clock=clock)


def test_first_activation_starts_zero_and_reopen_never_reseeds(tmp_path):
    clock = Clock(datetime(2026, 10, 12, 12, tzinfo=timezone.utc))
    ledger = new_ledger(tmp_path, clock=clock)
    origin = ledger.status()['activated_at']
    reservation = ledger.reserve('gpt-5-mini', 'private prompt', '', 1000)
    ledger.settle(reservation, actual_cost_usd=0.001, outcome='known')
    clock.value = datetime(2026, 10, 13, tzinfo=timezone.utc)
    reopened = new_ledger(tmp_path, clock=clock)
    status = reopened.status()
    assert status['activated_at'] == origin
    assert status['baseline_cost_usd'] == 0
    assert status['spent_usd'] == 0.001
    assert 'private prompt' not in (tmp_path / 'budget.sqlite3').read_bytes().decode(errors='ignore')


def test_independent_ledgers_reserve_atomically_under_concurrency(tmp_path):
    a, b = new_ledger(tmp_path, limit=0.004), new_ledger(tmp_path, limit=0.004)
    def reserve(ledger):
        try:
            return ledger.reserve('gpt-5-mini', 'p', '', 1000)
        except Exception as exc:
            return exc
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(reserve, [a, b]))
    reservations = [r for r in results if not isinstance(r, Exception)]
    assert len(reservations) == 1
    assert a.status()['reserved_usd'] > 0.002
    for token in reservations:
        (a if token in a.active_reservations else b).settle(token, outcome='not_sent')


def test_kst_calendar_month_rollover_preserves_old_spend(tmp_path):
    clock = Clock(datetime(2026, 10, 31, 14, 59, tzinfo=timezone.utc))
    ledger = new_ledger(tmp_path, clock=clock)
    token = ledger.reserve('gpt-5-mini', 'p', '', 1000)
    ledger.settle(token, actual_cost_usd=0.001, outcome='known')
    assert ledger.status()['month'] == '2026-10'
    clock.value = datetime(2026, 10, 31, 15, 0, tzinfo=timezone.utc)
    assert ledger.status()['month'] == '2026-11'
    assert ledger.status()['spent_usd'] == 0
    clock.value = datetime(2026, 10, 31, 14, 59, tzinfo=timezone.utc)
    assert ledger.status()['spent_usd'] == 0.001


def test_unknown_persists_across_restart_and_month(tmp_path):
    clock = Clock(datetime(2026, 10, 12, tzinfo=timezone.utc))
    ledger = new_ledger(tmp_path, clock=clock)
    token = ledger.reserve('gpt-5-mini', 'p', '', 1000)
    ledger.settle(token, outcome='unknown')
    clock.value = datetime(2026, 11, 12, tzinfo=timezone.utc)
    reopened = new_ledger(tmp_path, clock=clock)
    assert reopened.status()['unresolved_count'] == 1
    assert reopened.status()['available_usd'] is None
    with pytest.raises(Exception, match='unresolved_usage'):
        reopened.reserve('gpt-5-mini', 'p', '', 1000)


def test_orphaned_reservation_blocks_but_active_reservation_does_not(tmp_path):
    ledger = new_ledger(tmp_path)
    token = ledger.reserve('gpt-5-mini', 'p', '', 1000)
    another = new_ledger(tmp_path)
    assert another.status()['unresolved_count'] == 0
    ledger.close()  # Releases OS ownership, never the uncertain reservation.
    assert another.status()['unresolved_count'] == 1
    with pytest.raises(Exception, match='unresolved_usage'):
        another.reserve('gpt-5-mini', 'p', '', 1000)


def test_actual_over_reservation_is_recorded_and_sticky_block(tmp_path):
    ledger = new_ledger(tmp_path)
    token = ledger.reserve('gpt-5-mini', 'p', '', 10)
    ledger.settle(token, actual_cost_usd=1.0, outcome='known')
    status = new_ledger(tmp_path).status()
    assert status['spent_usd'] == 1.0
    assert status['bound_breach']
    with pytest.raises(Exception, match='reservation_bound_breach'):
        ledger.reserve('gpt-5-mini', 'p', '', 10)


@pytest.mark.parametrize('bad', [-1, True, float('nan'), float('inf'), '100'])
def test_invalid_monthly_config_rejected(bad):
    with pytest.raises(ValueError):
        LLMConfig.from_config({'llm': {'monthly_budget': bad}})


def test_monthly_config_and_explicit_path_are_parsed_without_io(tmp_path):
    config = LLMConfig.from_config({'llm': {'monthly_budget': 100, 'budget_state_path': str(tmp_path / 'new.sqlite3')}})
    assert config.monthly_budget_usd == 100
    assert not (tmp_path / 'new.sqlite3').exists()


class FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0
        self._session = None
    async def complete(self, *args, **kwargs):
        self.calls += 1
        item = self.responses.pop(0)
        if callable(item):
            return await item()
        return item


def response(content='ok', provider=LLMProvider.OPENAI, success=True, usage_known=True, sent=True):
    return LLMResponse(content, 'gpt-5-mini', provider, input_tokens=10, output_tokens=10,
                       success=success, usage_known=usage_known, request_sent=sent,
                       billing_status='reported_usage' if usage_known else 'unknown')


def manager(tmp_path, limit=100):
    return LLMManager(LLMConfig(openai_api_key='fake', gemini_api_key='fake',
        monthly_budget_usd=limit, budget_state_path=str(tmp_path / 'budget.sqlite3')))


@pytest.mark.asyncio
async def test_monthly_zero_rejects_before_provider_call(tmp_path):
    m = manager(tmp_path, limit=0)
    fake = FakeClient([response()])
    m.openai = fake
    result = await m.complete_with('p', LLMProvider.OPENAI)
    assert not result.success and not result.request_sent
    assert fake.calls == 0
    assert result.budget_block_reason == 'monthly_limit_exceeded'


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['corrupt', 'directory'])
async def test_bad_storage_blocks_without_crashing_manager(tmp_path, kind):
    path = tmp_path / 'budget.sqlite3'
    if kind == 'corrupt':
        path.write_bytes(b'not sqlite')
    else:
        path.mkdir()
    m = manager(tmp_path)
    fake = FakeClient([response()])
    m.openai = fake
    result = await m.complete_with('p', LLMProvider.OPENAI)
    assert not result.success
    assert fake.calls == 0
    assert m.get_monthly_budget_status()['blocked']
    assert m.get_monthly_budget_status()['spent_usd'] is None


@pytest.mark.asyncio
async def test_empty_retry_and_shadow_each_get_reservation(tmp_path):
    m = manager(tmp_path)
    m._SHADOW_LOG_DIR = tmp_path / 'shadow'
    m.openai = FakeClient([response(''), response(), response()])
    assert (await m.complete_with('p', LLMProvider.OPENAI, retry_on_empty=1, max_tokens=100)).success
    await m._fire_shadow('p', '', LLMTask.QUICK_CLASSIFY, {'max_tokens': 100}, response(), 'gpt-5-mini')
    status = m.get_monthly_budget_status()
    assert status['settled_count'] == 3
    assert status['spent_usd'] == pytest.approx(0.0000675)
    assert status['reserved_usd'] == 0


@pytest.mark.asyncio
async def test_unknown_attempt_blocks_after_manager_restart(tmp_path):
    m = manager(tmp_path)
    m.openai = FakeClient([response('', success=False, usage_known=False)])
    await m.complete_with('p', LLMProvider.OPENAI)
    restarted = manager(tmp_path)
    restarted.openai = FakeClient([response()])
    result = await restarted.complete_with('p', LLMProvider.OPENAI)
    assert result.budget_block_reason == 'unresolved_usage'
    assert restarted.openai.calls == 0


@pytest.mark.asyncio
async def test_cancelled_unknown_request_persists_and_propagates(tmp_path):
    m = manager(tmp_path)
    async def pending():
        await asyncio.Event().wait()
    m.openai = FakeClient([pending])
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(m.complete_with('p', LLMProvider.OPENAI), timeout=0.01)
    assert manager(tmp_path).get_monthly_budget_status()['unresolved_count'] == 1


@pytest.mark.asyncio
async def test_local_validation_releases_reservation(tmp_path):
    m = manager(tmp_path)
    with pytest.raises(ValueError):
        await m.complete_with('p', LLMProvider.OPENAI, reasoning_effort='unsupported')
    assert m.get_monthly_budget_status()['reserved_usd'] == 0
    assert not m.get_monthly_budget_status()['blocked']


@pytest.mark.asyncio
async def test_monthly_cap_refuses_manus_bypass(tmp_path, monkeypatch):
    m = manager(tmp_path)
    m.config.manus_enabled = True
    async def forbidden(*args, **kwargs):
        pytest.fail('Manus must not bypass monthly cap')
    monkeypatch.setattr(m, '_try_manus', forbidden)
    result = await m.complete('p', task=LLMTask.TRADE_REVIEW)
    assert not result.success
    assert result.budget_block_reason == 'unsupported_billing_provider'


@pytest.mark.asyncio
async def test_close_singleton_helper_does_not_initialize(monkeypatch):
    import src.utils.llm as module
    monkeypatch.setattr(module, '_llm_manager', None)
    await module.close_llm_manager()
    assert module._llm_manager is None


def test_deleted_existing_ledger_does_not_restart_at_zero(tmp_path):
    ledger = new_ledger(tmp_path)
    ledger.path.unlink()
    with pytest.raises(Exception, match='previous_ledger_missing'):
        new_ledger(tmp_path)
    assert not ledger.path.exists()


def test_changed_limit_is_not_silently_applied(tmp_path):
    new_ledger(tmp_path, limit=100)
    with pytest.raises(Exception, match='budget_configuration_mismatch'):
        new_ledger(tmp_path, limit=200)


def test_actual_process_crash_leaves_unresolved_reservation(tmp_path):
    import subprocess
    import sys
    path = tmp_path / 'budget.sqlite3'
    code = "from src.utils.llm_budget import MonthlyBudgetLedger; import os,sys; ledger=MonthlyBudgetLedger(sys.argv[1],100); ledger.reserve('gpt-5-mini','p','',100); os._exit(0)"
    subprocess.run([sys.executable, '-c', code, str(path)], check=True, timeout=5)
    ledger = new_ledger(tmp_path)
    assert ledger.status()['unresolved_count'] == 1
    with pytest.raises(Exception, match='unresolved_usage'):
        ledger.reserve('gpt-5-mini', 'p', '', 100)


def test_reservation_and_budget_check_use_one_kst_month(tmp_path):
    clock = Clock(datetime(2026, 10, 31, 15, tzinfo=timezone.utc))
    ledger = new_ledger(tmp_path, limit=0.001, clock=clock)
    token = ledger.reserve('gpt-5-mini', 'p', '', 100)
    ledger.settle(token, outcome='known', actual_cost_usd=0.0003)
    # Jump precisely between ledger clock reads; admission must use one timestamp.
    moments = iter([datetime(2026, 10, 31, 14, 59, tzinfo=timezone.utc),
                    datetime(2026, 10, 31, 14, 59, tzinfo=timezone.utc),
                    datetime(2026, 10, 31, 15, tzinfo=timezone.utc)])
    ledger.clock = lambda: next(moments)
    token2 = ledger.reserve('gpt-5-mini', 'p', '', 400)
    with sqlite3.connect(ledger.path) as db:
        assert db.execute('SELECT month FROM reservations WHERE token=?', (token2,)).fetchone()[0] == '2026-10'
    ledger.close()


@pytest.mark.asyncio
async def test_storage_write_failure_blocks_provider(tmp_path, monkeypatch):
    import src.utils.llm_budget as module
    m = manager(tmp_path)
    m.openai = FakeClient([response()])
    original = module.os.open
    def deny_lock(path, *args, **kwargs):
        if str(path).endswith('.lock'):
            raise PermissionError('denied')
        return original(path, *args, **kwargs)
    monkeypatch.setattr(module.os, 'open', deny_lock)
    result = await m.complete_with('p', LLMProvider.OPENAI)
    assert result.budget_block_reason == 'storage_unavailable'
    assert m.openai.calls == 0


@pytest.mark.asyncio
async def test_definitive_rejection_releases_then_fallback_is_settled(tmp_path):
    m = manager(tmp_path)
    rejection = response('', success=False, usage_known=False)
    rejection.billing_status = 'rejected_before_generation'
    m.openai = FakeClient([rejection])
    m.gemini = FakeClient([response(provider=LLMProvider.GEMINI)])
    assert (await m.complete('p', task=LLMTask.MARKET_ANALYSIS)).success
    status = m.get_monthly_budget_status()
    assert status['settled_count'] == 1
    assert status['unresolved_count'] == 0
    assert status['reserved_usd'] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize('sent', [False, True])
async def test_real_transport_cancel_releases_only_unsent_attempt(tmp_path, monkeypatch, sent):
    m = manager(tmp_path)
    class PendingReply:
        async def __aenter__(self):
            await asyncio.Event().wait()
        async def __aexit__(self, *args):
            pass
    class PendingSession:
        closed = False
        def post(self, *args, **kwargs):
            return PendingReply()
    if sent:
        m.openai._session = PendingSession()
    else:
        async def pending_session():
            await asyncio.Event().wait()
        monkeypatch.setattr(m.openai, '_get_session', pending_session)
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(m.complete_with('p', LLMProvider.OPENAI), timeout=0.01)
    status = manager(tmp_path).get_monthly_budget_status()
    assert status['unresolved_count'] == int(sent)
    assert status['reserved_usd'] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize('model,max_tokens', [('unpriced', 100), ('gpt-5-mini', True), ('gpt-5-mini', -1)])
async def test_unknown_price_and_bad_output_limits_reject_before_http(tmp_path, model, max_tokens):
    m = manager(tmp_path)
    m.config.openai_model_light = model
    m.openai = FakeClient([response()])
    result = await m.complete_with('p', LLMProvider.OPENAI, max_tokens=max_tokens)
    assert not result.success
    assert m.openai.calls == 0
    assert m.get_monthly_budget_status()['reserved_usd'] == 0


def test_pending_policy_blocks_unmetered_without_initialization(monkeypatch):
    import src.utils.llm as module
    monkeypatch.setattr(module, '_llm_manager', None)
    monkeypatch.setattr(module, '_pending_config', LLMConfig(monthly_budget_usd=100))
    assert not module.unmetered_paid_calls_allowed()
    assert module._llm_manager is None


def test_enabled_relative_state_path_is_rejected_without_io():
    with pytest.raises(ValueError, match='budget_state_path'):
        LLMConfig.from_config({'llm': {'monthly_budget': 100, 'budget_state_path': 'relative.sqlite3'}})


@pytest.mark.asyncio
async def test_huge_output_limit_is_rejected_without_provider_or_arithmetic_error(tmp_path):
    m = manager(tmp_path)
    m.openai = FakeClient([response()])
    result = await m.complete_with('p', LLMProvider.OPENAI, max_tokens=10 ** 1000)
    assert not result.success
    assert result.budget_block_reason == 'invalid_request_estimate'
    assert m.openai.calls == 0


@pytest.mark.asyncio
async def test_failed_settlement_leaves_durable_uncertainty(tmp_path, monkeypatch):
    from src.utils.llm_budget import BudgetStateError
    m = manager(tmp_path)
    m.openai = FakeClient([response()])
    def fail_settlement(*args, **kwargs):
        raise BudgetStateError('storage_unavailable')
    monkeypatch.setattr(m.monthly_budget, 'settle', fail_settlement)
    assert (await m.complete_with('p', LLMProvider.OPENAI)).success
    assert m.get_monthly_budget_status()['spent_usd'] is None
    assert manager(tmp_path).get_monthly_budget_status()['unresolved_count'] == 1


@pytest.mark.asyncio
async def test_close_singleton_closes_existing_without_constructing_another(monkeypatch):
    import src.utils.llm as module
    class Existing:
        closed = False
        async def close(self):
            self.closed = True
    existing = Existing()
    monkeypatch.setattr(module, '_llm_manager', existing)
    await module.close_llm_manager()
    assert existing.closed
    assert module._llm_manager is None


@pytest.mark.asyncio
async def test_failed_settlement_does_not_abandon_other_inflight_request(tmp_path, monkeypatch):
    from src.utils.llm_budget import BudgetStateError
    m = manager(tmp_path)
    started, finish = asyncio.Event(), asyncio.Event()
    async def pending_success():
        started.set()
        await finish.wait()
        return response()
    m.openai = FakeClient([pending_success, response()])
    original = m.monthly_budget.settle
    failed_once = False
    def fail_first(token, **kwargs):
        nonlocal failed_once
        if not failed_once:
            failed_once = True
            raise BudgetStateError('storage_unavailable')
        return original(token, **kwargs)
    monkeypatch.setattr(m.monthly_budget, 'settle', fail_first)
    a = asyncio.create_task(m.complete_with('A', LLMProvider.OPENAI))
    await started.wait()
    await m.complete_with('B', LLMProvider.OPENAI)
    finish.set()
    assert (await a).success
    status = manager(tmp_path).get_monthly_budget_status()
    assert status['unresolved_count'] == 1
    assert status['settled_count'] == 1
    assert status['spent_usd'] == pytest.approx(0.0000225)


@pytest.mark.asyncio
async def test_close_drains_nonshadow_known_completion_and_blocks_new_attempts(tmp_path):
    m = manager(tmp_path)
    started = asyncio.Event()
    async def pending_success():
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            return response()  # Provider already has a completed known response.
    m.openai = FakeClient([pending_success, response()])
    task = asyncio.create_task(m.complete_with('p', LLMProvider.OPENAI))
    await started.wait()
    await asyncio.wait_for(m.close(), timeout=1)
    assert task.done()
    assert (await task).success
    assert manager(tmp_path).get_monthly_budget_status()['settled_count'] == 1
    assert manager(tmp_path).get_monthly_budget_status()['unresolved_count'] == 0
    assert not (await m.complete_with('new', LLMProvider.OPENAI)).success
    assert m.openai.calls == 1


@pytest.mark.asyncio
async def test_close_from_active_attempt_does_not_deadlock_or_lose_known_charge(tmp_path):
    m = manager(tmp_path)
    async def closes_itself():
        await m.close()
        return response()
    m.openai = FakeClient([closes_itself])
    assert (await asyncio.wait_for(m.complete_with('p', LLMProvider.OPENAI), timeout=1)).success
    status = manager(tmp_path).get_monthly_budget_status()
    assert status['settled_count'] == 1
    assert status['unresolved_count'] == 0


@pytest.mark.parametrize('config', [None, LLMConfig()])
def test_global_get_requires_explicit_monthly_policy(monkeypatch, config):
    import src.utils.llm as module
    monkeypatch.setattr(module, '_llm_manager', None)
    monkeypatch.setattr(module, '_pending_config', config)
    with pytest.raises(RuntimeError, match='monthly budget'):
        module.get_llm_manager()
    assert module._llm_manager is None


def test_unmetered_no_config_is_closed(monkeypatch):
    import src.utils.llm as module
    monkeypatch.setattr(module, '_llm_manager', None)
    monkeypatch.setattr(module, '_pending_config', None)
    assert not module.unmetered_paid_calls_allowed()


@pytest.mark.asyncio
async def test_manus_enabled_does_not_block_other_tasks(tmp_path):
    m = manager(tmp_path)
    m.config.manus_enabled = True
    m.gemini = FakeClient([response(provider=LLMProvider.GEMINI)])
    assert (await m.complete('p', task=LLMTask.QUICK_CLASSIFY)).success
    assert m.gemini.calls == 1


@pytest.mark.asyncio
async def test_status_read_failure_does_not_permanently_disable_requests(tmp_path, monkeypatch):
    from src.utils.llm_budget import BudgetStateError
    m = manager(tmp_path)
    original = m.monthly_budget.status
    def fail_read():
        raise BudgetStateError('storage_unavailable')
    monkeypatch.setattr(m.monthly_budget, 'status', fail_read)
    assert m.get_monthly_budget_status()['blocked']
    monkeypatch.setattr(m.monthly_budget, 'status', original)
    m.openai = FakeClient([response()])
    assert (await m.complete_with('p', LLMProvider.OPENAI)).success


@pytest.mark.asyncio
@pytest.mark.parametrize('budgeted', [False, True])
async def test_shadow_generic_exception_is_accounted_once(tmp_path, budgeted):
    m = manager(tmp_path) if budgeted else LLMManager(LLMConfig(openai_api_key='fake'))
    m.audit_dir = tmp_path / 'audit'
    m._SHADOW_LOG_DIR = tmp_path / 'shadow'
    async def broken():
        raise RuntimeError('private failure')
    m.openai = FakeClient([broken])
    await m._fire_shadow('p', '', LLMTask.QUICK_CLASSIFY, {}, response(), 'gpt-5-mini')
    assert m.daily_usage.unknown_usage_requests == 1
    rows = [line for path in m.audit_dir.glob('*.jsonl') for line in path.read_text().splitlines()]
    assert len(rows) == 1
    assert 'private failure' not in rows[0]


@pytest.mark.asyncio
async def test_unbudgeted_generic_exception_is_accounted_and_reraised(tmp_path):
    m = LLMManager(LLMConfig(openai_api_key='fake'), audit_dir=tmp_path)
    async def broken():
        raise RuntimeError('private failure')
    m.openai = FakeClient([broken])
    with pytest.raises(RuntimeError):
        await m.complete_with('p', LLMProvider.OPENAI)
    assert m.daily_usage.unknown_usage_requests == 1


@pytest.mark.asyncio
async def test_sqlite_contention_is_bounded_before_http(tmp_path):
    import time
    m = manager(tmp_path)
    m.openai = FakeClient([response()])
    with sqlite3.connect(tmp_path / 'budget.sqlite3') as blocker:
        blocker.execute('BEGIN IMMEDIATE')
        started = time.monotonic()
        result = await m.complete_with('p', LLMProvider.OPENAI)
        elapsed = time.monotonic() - started
    assert result.budget_block_reason == 'storage_unavailable'
    assert m.openai.calls == 0
    assert elapsed < 1.0


def test_gemini_reservation_has_explicit_thought_headroom(tmp_path):
    ledger = new_ledger(tmp_path)
    token = ledger.reserve('gemini-3.1-pro-preview', 'p', '', 1000)
    assert ledger.status()['reserved_usd'] > 0.024
    ledger.settle(token, outcome='known', actual_cost_usd=0.024)
    assert not ledger.status()['bound_breach']


@pytest.mark.asyncio
async def test_unbudgeted_cancel_without_transport_metadata_is_accounted_once(tmp_path):
    m = LLMManager(LLMConfig(openai_api_key='fake'), audit_dir=tmp_path)
    async def pending():
        await asyncio.Event().wait()
    m.openai = FakeClient([pending])
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(m.complete_with('p', LLMProvider.OPENAI), timeout=0.01)
    assert m.daily_usage.unknown_usage_requests == 1
    assert len([line for path in tmp_path.glob('*.jsonl') for line in path.read_text().splitlines()]) == 1


@pytest.mark.asyncio
async def test_shadow_local_validation_has_one_unsent_audit_row(tmp_path):
    m = manager(tmp_path)
    m.audit_dir = tmp_path / 'audit'
    m._SHADOW_LOG_DIR = tmp_path / 'shadow'
    await m._fire_shadow('p', '', LLMTask.QUICK_CLASSIFY, {'reasoning_effort': 'unsupported'}, response(), 'gpt-5-mini')
    rows = [json.loads(line) for path in m.audit_dir.glob('*.jsonl') for line in path.read_text().splitlines()]
    assert len(rows) == 1
    assert not rows[0]['request_sent']
    assert m.daily_usage.unknown_usage_requests == 0
    assert m.get_monthly_budget_status()['reserved_usd'] == 0


@pytest.mark.asyncio
async def test_closed_manager_cannot_enter_legacy_manus_path(monkeypatch):
    m = LLMManager(LLMConfig(manus_enabled=True))
    await m.close()
    async def forbidden(*args, **kwargs):
        pytest.fail('Closing must reject every paid path')
    monkeypatch.setattr(m, '_try_manus', forbidden)
    assert not (await m.complete('p', task=LLMTask.TRADE_REVIEW)).success
