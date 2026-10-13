"""Real temporary budget ledger; fake provider transport only. No credentials/network."""
import asyncio
from dataclasses import replace
from datetime import datetime, timezone
import importlib.util
import json
from types import SimpleNamespace

import pytest

from src.analytics.model_comparison_execution import ComparisonCallGuard, ComparisonExecutionPolicy, ComparisonRequest
from src.utils.llm import LLMConfig, LLMProvider, LLMResponse, OpenAIClient, GeminiClient
from src.utils.llm_budget import MonthlyBudgetLedger, BudgetBlocked

NOW = datetime(2026, 10, 13, 12, tzinfo=timezone.utc)


def api():
    assert importlib.util.find_spec('src.analytics.model_comparison_runner') is not None, 'paired runner missing'
    from src.analytics import model_comparison_runner
    return model_comparison_runner


class FakeOpenAI(OpenAIClient):
    def __init__(self, *, unknown=False, empty=False):
        super().__init__(LLMConfig(openai_api_key='', gemini_api_key=''))
        self.calls = []
        self.unknown, self.empty = unknown, empty

    async def complete(self, prompt, system='', model=None, **kw):
        self.calls.append((prompt, system, model, kw))
        await asyncio.sleep(0)
        text = '' if self.empty else ('APPROVE' if kw.get('reasoning_effort') == 'low' else 'REJECT')
        return LLMResponse(text, model, LLMProvider.OPENAI, input_tokens=100, output_tokens=10,
                           requested_model=model, resolved_model='provider-reported-version',
                           usage_known=not self.unknown, billing_status='unknown' if self.unknown else 'reported_usage')


class FakeGemini(GeminiClient):
    def __init__(self):
        super().__init__(LLMConfig(openai_api_key='', gemini_api_key=''))
        self.calls = []

    async def complete(self, prompt, system='', model=None, **kw):
        self.calls.append(model)
        return LLMResponse('ACCEPT', model, LLMProvider.GEMINI, input_tokens=100, output_tokens=10,
                           requested_model=model, resolved_model='reported-gemini', usage_known=True)


def setup(tmp_path, **kwargs):
    m = api()
    ledger = MonthlyBudgetLedger(tmp_path / 'budget.db', 100, clock=lambda: NOW)
    guard = ComparisonCallGuard(ledger=ledger, policy=ComparisonExecutionPolicy(
        experiment_id='R', daily_attempt_cap=20,
        allowed_models=('gpt-5-mini', 'gemini-3.1-flash-lite-preview'), max_output_tokens=400))
    op, ge = FakeOpenAI(**kwargs), FakeGemini()
    adapter = m.SingleAttemptAdapter(openai=op, gemini=ge, clock=lambda: NOW)
    runner = m.PairedComparisonRunner(guard=guard, adapter=adapter, clock=lambda: NOW)
    spec = m.PairSpec(decision_id='decision-1', signal_id='signal-1', symbol='005490',
                      common_context='same immutable facts', input_available_at=NOW.isoformat(),
                      current=m.RoleControls('gpt-5-mini', 400, 'minimal', 0.3, 20260803),
                      candidate=m.RoleControls('gpt-5-mini', 400, 'low', 0.3, 20260803),
                      bear=m.RoleControls('gemini-3.1-flash-lite-preview', 400, None, 0.0),
                      neutral_sizing=None, pricing_basis='offline_replay',
                      fixed_at=NOW.isoformat(), strict_on_disagreement=True)
    return m, ledger, runner, spec, op, ge


def live(spec, enqueued_at=None):
    """Same-process attested live spec, as the comparison worker builds it from a verified envelope."""
    from src.analytics.model_comparison_input import attest_pair
    enqueued_at = enqueued_at or spec.input_available_at
    return replace(spec, pricing_basis='paired_live_shadow', evaluation_anchor='input_available_at',
                   live_provenance='in_process_attested', enqueued_at=enqueued_at,
                   attestation=attest_pair(decision_id=spec.decision_id, signal_id=spec.signal_id, symbol=spec.symbol,
                                           common_context=spec.common_context, input_available_at=spec.input_available_at,
                                           enqueued_at=enqueued_at, neutral_sizing=spec.neutral_sizing))


def test_pair_uses_one_common_bear_and_same_role_controls_then_resumes_without_spend(tmp_path):
    m, ledger, runner, spec, op, ge = setup(tmp_path)
    first = asyncio.run(runner.run(spec))
    assert first['current']['verdict'] == 'reject'
    assert first['candidate']['verdict'] == 'approve'
    assert first['actual_provider_attempts'] == 3
    assert ledger.status()['settled_count'] == 3
    assert first['economic_evaluable'] is False and first['net_delta_krw'] is None
    assert first['current']['attempts'][0]['resolved_model'] == 'provider-reported-version'
    assert first['current']['attempts'][0]['reservation_token']
    spent = ledger.status()['spent_usd']
    assert asyncio.run(runner.run(spec)) == first
    assert len(op.calls) == 2 and len(ge.calls) == 1
    assert ledger.status()['spent_usd'] == spent
    assert op.calls[0][3]['max_tokens'] == 400 and op.calls[0][3]['seed'] == 20260803
    assert sorted(c[3]['reasoning_effort'] for c in op.calls) == ['low', 'minimal']


def test_changed_pair_snapshot_or_prompt_cannot_reuse_identity_for_new_charge(tmp_path):
    m, ledger, runner, spec, op, ge = setup(tmp_path)
    asyncio.run(runner.run(spec))
    for changed in (replace(spec, common_context='different facts'),
                    replace(spec, neutral_sizing={'snapshot_id': 'different'})):
        with pytest.raises((ValueError, BudgetBlocked), match='pair.*mismatch'):
            asyncio.run(runner.run(changed))
    assert ledger.status()['settled_count'] == 3


def test_unknown_cost_is_not_zero_and_resume_cannot_retry_provider(tmp_path):
    m, ledger, runner, spec, op, ge = setup(tmp_path, unknown=True)
    row = asyncio.run(runner.run(spec))
    assert row['total_comparison_cost_usd'] is None
    assert ledger.status()['unresolved_count'] >= 1
    calls = len(op.calls) + len(ge.calls)
    assert asyncio.run(runner.run(spec)) == row
    assert len(op.calls) + len(ge.calls) == calls


def test_successful_empty_retry_has_distinct_budgeted_attempt_and_preserves_cost(tmp_path):
    m, ledger, runner, spec, op, ge = setup(tmp_path, empty=True)
    row = asyncio.run(runner.run(spec))
    assert row['current']['verdict'] == 'approve'  # Bear alone, actual aggregation rule.
    assert len(row['current']['attempts']) == 3  # Bull twice plus shared Bear.
    assert ledger.status()['settled_count'] == 5
    assert row['actual_provider_attempts'] == 5


def test_adapter_rejects_manager_or_unsupported_controls_before_any_dispatch(tmp_path):
    m, ledger, runner, spec, op, ge = setup(tmp_path)
    with pytest.raises(ValueError):
        m.SingleAttemptAdapter(openai=SimpleNamespace(complete=lambda: None), gemini=ge)
    with pytest.raises(ValueError, match='reasoning'):
        asyncio.run(runner.run(replace(spec, candidate=replace(spec.candidate, reasoning_effort='invalid'))))
    assert op.calls == [] and ge.calls == []


def test_two_changes_at_once_or_role_mismatch_are_not_model_experiments(tmp_path):
    m, ledger, runner, spec, op, ge = setup(tmp_path)
    bad = replace(spec.candidate, model='gpt-5.6-sol', max_output_tokens=1200)
    with pytest.raises(ValueError):
        asyncio.run(runner.run(replace(spec, candidate=bad)))
    with pytest.raises(ValueError):
        asyncio.run(runner.run(replace(spec, bear=spec.current)))
    assert ledger.status()['settled_count'] == 0


def test_report_preserves_denominator_and_historical_timing_is_never_live(tmp_path):
    m, ledger, runner, spec, op, ge = setup(tmp_path)
    row = asyncio.run(runner.run(spec))
    report = m.report_pairs([row], payloads=[], ledger_status=None)
    assert report['summary']['pairs'] == 1 and report['summary']['economic_evaluable'] == 0
    assert report['rows'][0]['net_delta_krw'] is None
    assert 'offline_replay_not_live' in report['rows'][0]['reasons']
    assert report['rows'][0]['current']['quote_evidence']['evaluable'] is False


def test_caller_cancellation_is_not_swallowed_and_resume_does_not_redispatch(tmp_path):
    m, ledger, runner, spec, op, ge = setup(tmp_path)
    started = asyncio.Event()
    async def blocked(*a, **kw):
        started.set()
        await asyncio.Event().wait()
    op.complete = blocked
    async def cancel():
        task = asyncio.create_task(runner.run(spec))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    asyncio.run(cancel())
    assert ledger.status()['unresolved_count'] >= 1
    with pytest.raises(BudgetBlocked, match='pair_incomplete'):
        asyncio.run(runner.run(spec))


def test_live_label_requires_fresh_input_and_no_replay_clock_substitution(tmp_path):
    m, ledger, runner, spec, op, ge = setup(tmp_path)
    old = live(replace(spec, input_available_at='2025-01-01T00:00:00+00:00'))
    with pytest.raises(ValueError, match='live_input'):
        asyncio.run(runner.run(old))
    with pytest.raises(ValueError, match='live_requires'):
        asyncio.run(runner.run(replace(spec, pricing_basis='paired_live_shadow')))   # caller label only
    assert ledger.status()['settled_count'] == 0


def test_report_selects_each_responses_later_quote_and_same_exit(tmp_path):
    from tests.test_decision_quote_join import _quote, _state, OK_LEDGER
    m, ledger, runner, spec, op, ge = setup(tmp_path)
    row = asyncio.run(runner.run(live(spec)))
    row['current']['decision_available_at'] = '2026-10-13T12:00:01+00:00'
    row['candidate']['decision_available_at'] = '2026-10-13T12:00:03+00:00'
    payloads = [_state('005490', '2026-10-13T12:00:00', 'H0STMKO0'),
                _quote('005490', '2026-10-13T12:00:02', ask='313000', qid='early'),
                _quote('005490', '2026-10-13T12:00:04', ask='314000', qid='late'),
                _state('005490', '2026-10-13T12:14:59', 'H0STMKO0'),
                _quote('005490', '2026-10-13T12:15:00', qid='exit')]
    out = m.report_pairs([row], payloads=payloads, ledger_status=OK_LEDGER)['rows'][0]
    assert out['current']['quote_evidence']['first_quote_after']['ask'] == '313000'
    assert out['candidate']['quote_evidence']['first_quote_after']['ask'] == '314000'
    assert out['exit_quote_evidence']['first_quote_after']['quote_id'] == 'exit'
    assert out['current']['quote_evidence']['evaluable'] is True
    assert out['net_delta_krw'] is None  # raw codes and sizing are not fill/admission proof.


def test_bad_report_limits_are_rejected_even_for_empty_input():
    with pytest.raises(ValueError):
        api().report_pairs([], payloads=[], ledger_status=None, max_delay_seconds=float('nan'))


def test_read_only_cli_reports_pair_without_provider_or_implicit_ledger(tmp_path, capsys):
    from scripts.report_model_comparison import main
    m, ledger, runner, spec, op, ge = setup(tmp_path)
    row = asyncio.run(runner.run(spec))
    path = tmp_path / 'pairs.jsonl'
    path.write_text(json.dumps(row) + '\n')
    assert main(['--pairs', str(path)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out['summary']['pairs'] == 1 and out['summary']['economic_evaluable'] == 0
    assert len(op.calls) == 2
    assert main(['--pairs', str(path), '--ledger', 'missing']) == 2
    assert 'must be supplied together' in capsys.readouterr().out


def test_total_role_deadline_includes_empty_retry_and_keeps_spend_unknown(tmp_path):
    m, ledger, runner, spec, op, ge = setup(tmp_path)
    original = op.complete
    async def slow(*args, **kwargs):
        await asyncio.sleep(0.025)
        return await original(*args, **kwargs)
    op.complete = slow
    spec = replace(spec, current=replace(spec.current, timeout_seconds=0.01),
                   candidate=replace(spec.candidate, timeout_seconds=0.01))
    row = asyncio.run(runner.run(spec))
    assert row['current']['verdict'] == row['candidate']['verdict'] == 'unavailable'
    assert row['total_comparison_cost_usd'] is None
    assert ledger.status()['unresolved_count'] == 2
    assert 'explicit_fallback_policy_missing' in row['current']['reasons']


@pytest.mark.parametrize('bull,bear,strict,expected', [
    ('APPROVE', 'ACCEPT', True, 'approve'),
    ('APPROVE', 'REJECT', True, 'reject'),
    ('APPROVE', 'REJECT', False, 'approve'),
    ('REJECT', 'ACCEPT', True, 'reject'),
    ('REJECT', 'REJECT', False, 'reject'),
    ('unknown', 'REJECT', False, 'reject'),
    ('APPROVE', 'unknown', True, 'approve'),
    ('unknown', 'unknown', True, 'unavailable'),
])
def test_fixed_panel_aggregation_keeps_role_verdict_distinct_from_final_policy(tmp_path, bull, bear, strict, expected):
    m, ledger, runner, spec, op, ge = setup(tmp_path)
    async def bull_reply(*args, model=None, **kw):
        return LLMResponse(bull, model, LLMProvider.OPENAI, requested_model=model, usage_known=True)
    async def bear_reply(*args, model=None, **kw):
        return LLMResponse(bear, model, LLMProvider.GEMINI, requested_model=model, usage_known=True)
    op.complete, ge.complete = bull_reply, bear_reply
    row = asyncio.run(runner.run(replace(spec, strict_on_disagreement=strict)))
    assert row['current']['verdict'] == row['candidate']['verdict'] == expected


@pytest.mark.parametrize('fallback_text', ['NO unsafe', '             NO unsafe'])
def test_explicit_fallback_is_separately_budgeted_and_applies_single_no_policy(tmp_path, fallback_text):
    m, ledger, runner, spec, op, ge = setup(tmp_path)
    async def reply(prompt, system='', model=None, **kw):
        provider = LLMProvider.GEMINI if model.startswith('gemini') else LLMProvider.OPENAI
        return LLMResponse(fallback_text if prompt == 'fallback same facts' else 'unknown', model, provider,
                           requested_model=model, input_tokens=10, output_tokens=2)
    op.complete = ge.complete = reply
    spec = replace(spec, fallback_prompt='fallback same facts',
                   fallback_chain=(m.RoleControls('gpt-5-mini', 100),))
    row = asyncio.run(runner.run(spec))
    assert row['current']['verdict'] == row['candidate']['verdict'] == 'reject'
    assert row['actual_provider_attempts'] == ledger.status()['settled_count'] == 5
    assert row['current']['attempts'][-1]['role'] == 'current_fallback'
    assert row['candidate']['attempts'][-1]['role'] == 'candidate_fallback'


from tests.test_model_comparison_sizing import case as sizing_case


@pytest.fixture
def frozen_capture(monkeypatch):
    """Snapshot capture uses wall time; pin it 1s before NOW so input/claim checks don't depend on today's clock."""
    from datetime import timedelta
    from src.analytics import model_comparison_sizing as sizing

    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return (NOW - timedelta(seconds=1)).astimezone(tz)
    monkeypatch.setattr(sizing, 'datetime', Frozen)


def test_real_neutral_sizing_runs_each_final_verdict_and_retains_caps(tmp_path, sizing_case, frozen_capture):
    from src.analytics.model_comparison_sizing import capture_policy_sizing
    m, ledger, runner, spec, op, ge = setup(tmp_path)
    rm, event = sizing_case
    snapshot = capture_policy_sizing(rm, event)
    spec = replace(spec, neutral_sizing=snapshot, signal_id=event.id, symbol=event.symbol)
    row = asyncio.run(runner.run(spec))
    assert row['current']['sizing_replay']['quantity'] == 100
    assert row['candidate']['sizing_replay']['quantity'] == 200
    assert row['current']['sizing_replay']['snapshot_id'] == row['candidate']['sizing_replay']['snapshot_id']
    assert row['net_delta_krw'] is None


def test_unrelated_snapshot_is_rejected_before_paid_comparison(tmp_path, sizing_case):
    from src.analytics.model_comparison_sizing import capture_policy_sizing
    m, ledger, runner, spec, op, ge = setup(tmp_path)
    snapshot = capture_policy_sizing(*sizing_case)
    with pytest.raises(ValueError, match='snapshot.*identity'):
        asyncio.run(runner.run(replace(spec, neutral_sizing=snapshot)))
    assert ledger.status()['settled_count'] == 0


def test_proven_not_sent_raw_clients_preserve_zero_spend_without_unknown_block(tmp_path):
    m, ledger, runner, spec, op, ge = setup(tmp_path)
    config = LLMConfig(openai_api_key='', gemini_api_key='')
    runner.adapter = m.SingleAttemptAdapter(openai=OpenAIClient(config), gemini=GeminiClient(config), clock=lambda: NOW)
    row = asyncio.run(runner.run(spec))
    assert row['actual_provider_attempts'] == 0
    assert row['unknown_transmission_attempts'] == 0
    assert row['total_comparison_cost_usd'] == '0'
    assert ledger.status()['unresolved_count'] == 0
    assert ledger.status()['spent_usd'] == 0
    assert row['current']['attempts'][0]['billing_status'] == 'not_sent'


def test_snapshot_replay_does_not_hold_shared_budget_write_lock(tmp_path, sizing_case, monkeypatch, frozen_capture):
    import sqlite3
    from src.analytics import model_comparison_sizing as sizing
    m, ledger, runner, spec, op, ge = setup(tmp_path)
    snapshot = sizing.capture_policy_sizing(*sizing_case)
    original = sizing.replay_policy_sizing
    def replay(*args):
        with sqlite3.connect(ledger.path, timeout=0) as db:
            db.execute('BEGIN IMMEDIATE')
            db.rollback()
        return original(*args)
    monkeypatch.setattr(sizing, 'replay_policy_sizing', replay)
    event = sizing_case[1]
    row = asyncio.run(runner.run(replace(spec, neutral_sizing=snapshot, signal_id=event.id, symbol=event.symbol)))
    assert row['candidate']['sizing_replay']['quantity'] == 200


def test_settlement_failure_keeps_saved_response_and_measured_cost_in_report(tmp_path, monkeypatch):
    m, ledger, runner, spec, op, ge = setup(tmp_path)
    original = ledger.settle
    def fail(token, **kwargs):
        ledger.abandon(token)
        raise RuntimeError('settlement unavailable')
    monkeypatch.setattr(ledger, 'settle', fail)
    row = asyncio.run(runner.run(spec))
    all_attempts = [a for arm in ('current', 'candidate') for a in row[arm]['attempts']]
    saved = [a for a in all_attempts if a.get('ledger_state') == 'pending_reconciliation']
    assert saved and saved[0]['api_cost_usd'] is not None
    assert saved[0]['content'] in ('APPROVE', 'REJECT', 'ACCEPT')
    assert saved[0]['provider_success'] is True and saved[0]['success'] is False
    assert ledger.status()['unresolved_count'] >= 1


def test_model_substitution_is_visible_and_not_certified_as_requested_model(tmp_path):
    m, ledger, runner, spec, op, ge = setup(tmp_path)
    row = asyncio.run(runner.run(spec))
    att = row['current']['attempts'][0]
    assert att['resolved_model'] == 'provider-reported-version'
    assert att['requested_model_match'] is False
    assert 'resolved_model_unverified_or_mismatch' in row['current']['reasons']


def test_report_rejects_mutated_horizon_and_out_of_window_decision_times(tmp_path):
    m, ledger, runner, spec, op, ge = setup(tmp_path)
    row = asyncio.run(runner.run(spec))
    row['evaluation_at'] = '2026-10-13T14:00:00+00:00'
    row['current']['decision_available_at'] = '2025-01-01T00:00:00+00:00'
    out = m.report_pairs([row], payloads=[], ledger_status=None)['rows'][0]
    assert 'evaluation_policy_mismatch' in out['reasons']
    assert 'current:decision_time_outside_policy' in out['reasons']
    assert out['exit_quote_evidence']['first_quote_after'] is None


def test_pair_storage_failure_exposes_original_result_without_another_charge(tmp_path, monkeypatch):
    from contextlib import contextmanager
    import sqlite3
    m, ledger, runner, spec, op, ge = setup(tmp_path)
    original = ledger._connect
    class Connection:
        def __init__(self, db): self.db = db
        def execute(self, sql, *args):
            if sql.startswith('UPDATE comparison_pairs SET result='):
                raise sqlite3.OperationalError('injected pair storage failure')
            return self.db.execute(sql, *args)
        def __getattr__(self, name): return getattr(self.db, name)
    @contextmanager
    def connect(**kwargs):
        with original(**kwargs) as db:
            yield Connection(db)
    monkeypatch.setattr(ledger, '_connect', connect)
    with pytest.raises(m.PairResultPersistenceError) as error:
        asyncio.run(runner.run(spec))
    assert error.value.result['current']['verdict'] == 'reject'
    assert error.value.result['actual_provider_attempts'] == 3
    assert ledger.status()['settled_count'] == 3
    with pytest.raises(BudgetBlocked, match='pair_incomplete'):
        asyncio.run(runner.run(spec))
    assert len(op.calls) == 2 and len(ge.calls) == 1


def test_concurrent_same_pair_claim_dispatches_only_one_pair(tmp_path):
    m, ledger, runner, spec, op, ge = setup(tmp_path)
    async def both():
        return await asyncio.gather(runner.run(spec), runner.run(spec), return_exceptions=True)
    results = asyncio.run(both())
    assert sum(isinstance(r, dict) for r in results) == 1
    assert sum(isinstance(r, BudgetBlocked) for r in results) == 1
    assert ledger.status()['settled_count'] == 3


def test_empty_fallback_keeps_engine_policy_and_marks_absent_model_approval(tmp_path):
    m, ledger, runner, spec, op, ge = setup(tmp_path)
    async def reply(prompt, system='', model=None, **kw):
        provider = LLMProvider.GEMINI if model.startswith('gemini') else LLMProvider.OPENAI
        return LLMResponse('' if prompt == 'fallback facts' else 'unknown', model, provider,
                           requested_model=model, input_tokens=10, output_tokens=2)
    op.complete = ge.complete = reply
    spec = replace(spec, fallback_prompt='fallback facts', fallback_chain=(m.RoleControls('gpt-5-mini', 100),))
    row = asyncio.run(runner.run(spec))
    assert row['current']['verdict'] == 'approve'  # Existing CrossStrategyValidator policy, not a new parser.
    assert row['current']['validation_verdict'] == 'unavailable'
    assert 'fallback_policy_without_explicit_model_verdict' in row['current']['reasons']


def test_v2_row_separates_input_queue_dispatch_times_and_anchors_live_on_input(tmp_path):
    from datetime import timedelta
    m, ledger, runner, spec, op, ge = setup(tmp_path)
    t_input = NOW - timedelta(seconds=2)
    t_enqueue = NOW - timedelta(seconds=1.5)
    spec = live(replace(spec, input_available_at=t_input.isoformat()), enqueued_at=t_enqueue.isoformat())
    row = asyncio.run(runner.run(spec))
    assert row['schema_version'] == 'model-comparison-pair-v2'
    assert (row['input_available_at'], row['enqueued_at'], row['requested_at']) == (
        t_input.isoformat(), t_enqueue.isoformat(), NOW.isoformat())
    assert row['evaluation_anchor'] == 'input_available_at'
    assert row['evaluation_at'] == (t_input + timedelta(seconds=900)).isoformat()
    assert row['live_provenance'] == 'in_process_attested'
    assert row['current']['queue_delay_ms'] == row['candidate']['queue_delay_ms'] == 1500
    assert row['current']['provider_latency_ms'] == 0     # fake clock: every attempt 0 ms
    assert 'attestation' not in row['policy']
    out = m.report_pairs([row], payloads=[], ledger_status=None)['rows'][0]
    assert 'evaluation_policy_mismatch' not in out['reasons'] and 'live_provenance_not_attested' not in out['reasons']
    offline = asyncio.run(runner.run(replace(spec, decision_id='offline-1', pricing_basis='offline_replay',
                                             evaluation_anchor='requested_at', live_provenance='caller_supplied_not_attested',
                                             attestation=None, enqueued_at=None)))
    assert offline['evaluation_at'] == (NOW + timedelta(seconds=900)).isoformat()
    assert offline['current']['queue_delay_ms'] is None


def test_report_reads_v1_rows_with_original_requested_at_meaning(tmp_path):
    from datetime import timedelta
    m, ledger, runner, spec, op, ge = setup(tmp_path)
    row = asyncio.run(runner.run(spec))
    legacy = json.loads(json.dumps(row))
    legacy['schema_version'] = 'model-comparison-pair-v1'
    for key in ('input_available_at', 'enqueued_at', 'evaluation_anchor'):
        legacy.pop(key)
    legacy['pricing_basis'] = 'paired_live_shadow'      # historical caller label, never upgraded
    legacy['live_provenance'] = 'caller_supplied_not_attested'
    out = m.report_pairs([legacy], payloads=[], ledger_status=None)['rows'][0]
    assert out['evaluation_anchor'] == 'requested_at'
    assert 'legacy_v1_requested_at_anchor' in out['reasons'] and 'live_provenance_not_attested' in out['reasons']
    assert 'evaluation_policy_mismatch' not in out['reasons']   # requested_at + horizon still checks out
    bad = dict(json.loads(json.dumps(row)), evaluation_anchor='responded_at')
    with pytest.raises(ValueError, match='invalid_pair_record'):
        m.report_pairs([bad], payloads=[], ledger_status=None)
