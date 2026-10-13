"""Explicit paired shadow worker; never installed in the trading event loop.

One Bull control changes, one common Bear supplies the other role. Each empty
retry and explicit fallback is separately admitted by ComparisonCallGuard.
Raw provider clients make one HTTP attempt; LLMManager is intentionally excluded.
Reports preserve sizing-only and raw market-state limitations, never certify fills.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json
import math
import sqlite3
from typing import Any

from .decision_quote_join import join_decisions
from .model_comparison_execution import ComparisonAttemptResult, ComparisonCallGuard, ComparisonRequest
from ..core.adversarial_validator import BULL_SUFFIX, BULL_SYSTEM, BEAR_SUFFIX, BEAR_SYSTEM, _parse_verdict
from ..utils.llm import OpenAIClient, GeminiClient, LLMProvider, LLMResponse, _validate_controls
from ..utils.llm_budget import BudgetBlocked, BudgetStateError
from ..utils.llm_pricing import rate_quote

VERSION = 'model-comparison-pair-v1'


def _now():
    return datetime.now(timezone.utc)


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True, allow_nan=False)


def _time(value):
    if not isinstance(value, str):
        raise ValueError('aware_time_required')
    stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if stamp.tzinfo is None or stamp.utcoffset() is None:
        raise ValueError('aware_time_required')
    return stamp


def _positive(value, label):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise ValueError(label)


def _provider(model):
    if model.startswith('gpt-'):
        return LLMProvider.OPENAI
    if model.startswith('gemini-'):
        return LLMProvider.GEMINI
    raise ValueError('unsupported_provider_model')


@dataclass(frozen=True)
class RoleControls:
    model: str
    max_output_tokens: int
    reasoning_effort: str | None = None
    temperature: float = 0.3
    seed: int | None = None
    timeout_seconds: float = 12.0

    def request(self, prompt, system):
        request = ComparisonRequest(prompt=prompt, system=system, **asdict(self))
        _validate_controls(self.model, _provider(self.model),
                           {'reasoning_effort': self.reasoning_effort, 'seed': self.seed})
        return request


@dataclass(frozen=True)
class PairSpec:
    decision_id: str
    signal_id: str
    symbol: str
    common_context: str
    input_available_at: str
    current: RoleControls
    candidate: RoleControls
    bear: RoleControls
    neutral_sizing: dict | None
    pricing_basis: str
    fixed_at: str
    strict_on_disagreement: bool
    # Explicit fallback prompt/system/chain required when both roles fail.
    # No inference of a fallback prompt from a different historical decision.
    fallback_prompt: str | None = None
    fallback_system: str = ''
    fallback_chain: tuple[RoleControls, ...] = ()
    empty_retries: int = 1
    horizon_seconds: int = 900
    max_input_age_seconds: float = 5.0

    def validate(self):
        for key in ('decision_id', 'signal_id', 'symbol', 'common_context'):
            if not isinstance(getattr(self, key), str) or not getattr(self, key).strip():
                raise ValueError(f'invalid_{key}')
        if self.pricing_basis not in {'offline_replay', 'paired_live_shadow'}:
            raise ValueError('invalid_pricing_basis')
        _time(self.input_available_at)
        _time(self.fixed_at)
        if type(self.strict_on_disagreement) is not bool or type(self.empty_retries) is not int or not 0 <= self.empty_retries <= 1:
            raise ValueError('invalid_aggregation_policy')
        if type(self.horizon_seconds) is not int or not 0 < self.horizon_seconds <= 86400:
            raise ValueError('invalid_horizon')
        _positive(self.max_input_age_seconds, 'max_input_age_seconds')
        if not all(isinstance(c, RoleControls) for c in (self.current, self.candidate, self.bear)):
            raise ValueError('explicit_role_controls_required')
        if _provider(self.current.model) != LLMProvider.OPENAI or _provider(self.candidate.model) != LLMProvider.OPENAI or _provider(self.bear.model) != LLMProvider.GEMINI:
            raise ValueError('role_provider_mismatch')
        changed = {k for k in asdict(self.current) if getattr(self.current, k) != getattr(self.candidate, k)}
        if len(changed) != 1 or not changed <= {'model', 'reasoning_effort', 'max_output_tokens'}:
            raise ValueError('one_comparison_variable_required')
        if not isinstance(self.fallback_chain, tuple) or len(self.fallback_chain) > 2:
            raise ValueError('invalid_fallback_chain')
        if self.fallback_chain and (not isinstance(self.fallback_prompt, str) or not self.fallback_prompt):
            raise ValueError('explicit_fallback_prompt_required')
        for control in (self.current, self.candidate, self.bear, *self.fallback_chain):
            control.request('validate', '')
        _json(asdict(self))  # Reject non-JSON snapshots and nonfinite numbers before admission.


class SingleAttemptAdapter:
    """Owns no budget, retries, fallback, credentials, or client lifecycle.

    The caller supplies raw clients and closes them. Price table output is a
    usage-based estimate, not an invoice or provider-reported billed USD.
    """
    def __init__(self, *, openai: OpenAIClient, gemini: GeminiClient, clock=_now):
        if not isinstance(openai, OpenAIClient) or not isinstance(gemini, GeminiClient):
            raise ValueError('raw_provider_clients_required; LLMManager forbidden')
        self.openai, self.gemini, self.clock = openai, gemini, clock

    async def __call__(self, request: ComparisonRequest):
        provider = _provider(request.model)
        _validate_controls(request.model, provider, {'reasoning_effort': request.reasoning_effort, 'seed': request.seed})
        client = self.openai if provider == LLMProvider.OPENAI else self.gemini
        start = self.clock()
        _time(start.isoformat())
        response = await asyncio.wait_for(client.complete(
            request.prompt, system=request.system, model=request.model,
            max_tokens=request.max_output_tokens, temperature=request.temperature,
            reasoning_effort=request.reasoning_effort, seed=request.seed), timeout=request.timeout_seconds)
        end = self.clock()
        if not isinstance(response, LLMResponse):
            raise ValueError('invalid_provider_response')
        cost, cost_basis = None, 'unknown'
        tokens_valid = all(type(v) is int and v >= 0 for v in (response.input_tokens, response.output_tokens))
        identity_matches = response.model == request.model and response.requested_model == request.model and response.provider == provider
        if response.request_sent is False and response.billing_status == 'not_sent':
            cost, cost_basis = Decimal('0'), 'transport_not_sent'
        elif identity_matches and response.usage_known is True and tokens_valid:
            quote = rate_quote(request.model, response.input_tokens, response.output_tokens, on_date=start.date())
            if quote is not None:
                cost = (Decimal(str(quote.input_usd_per_million)) * response.input_tokens
                        + Decimal(str(quote.output_usd_per_million)) * response.output_tokens) / 1000000
                cost_basis = quote.basis
        valid_timing = end >= start
        value = {'requested_model': request.model, 'resolved_model': response.resolved_model,
                 'model_identity': 'provider_reported_unverified' if response.resolved_model else 'unverified',
                 'requested_model_match': response.resolved_model == request.model,
                 'provider': provider.value, 'reasoning_effort': request.reasoning_effort,
                 'seed': request.seed, 'max_output_tokens': request.max_output_tokens,
                 'requested_at': start.isoformat(), 'responded_at': end.isoformat(),
                 'timing_valid': valid_timing, 'latency_ms': (end - start).total_seconds() * 1000 if valid_timing else None,
                 'content': response.content if isinstance(response.content, str) else '',
                 'success': response.success is True and identity_matches,
                 'request_sent': response.request_sent, 'billing_status': response.billing_status,
                 'usage_known': response.usage_known is True and tokens_valid,
                 'input_tokens': response.input_tokens if response.usage_known is True and tokens_valid else None,
                 'output_tokens': response.output_tokens if response.usage_known is True and tokens_valid else None,
                 'cost_basis': cost_basis, 'api_cost_usd': str(cost) if cost is not None else None,
                 'finish_reason': response.finish_reason}
        return ComparisonAttemptResult(value, cost)


def _sum_cost(attempts):
    values = [a.get('api_cost_usd') for a in attempts]
    return None if any(v is None for v in values) else str(sum((Decimal(v) for v in values), Decimal('0')))


class PairedComparisonRunner:
    """Separate worker API only. Explicit pair claim prevents concurrent/rerun spend.

    Completed pairs are recoverable. An interrupted pair remains incomplete;
    individual durable attempts are inspectable via guard.lookup, never resent.
    This conservative boundary does not repair ambiguous crash outcomes.
    """
    def __init__(self, *, guard: ComparisonCallGuard, adapter: SingleAttemptAdapter, clock=_now):
        if not isinstance(guard, ComparisonCallGuard) or not isinstance(adapter, SingleAttemptAdapter):
            raise ValueError('explicit_guard_and_single_attempt_adapter_required')
        self.guard, self.adapter, self.clock = guard, adapter, clock

    def _claim(self, spec, now):
        pair_id = hashlib.sha256(_json([self.guard.policy.experiment_id, spec.decision_id]).encode()).hexdigest()
        manifest = _json({'schema_version': VERSION, 'policy': self.guard.policy.serialized(), 'spec': asdict(spec)})

        def existing(db):
            row = db.execute('SELECT manifest,result FROM comparison_pairs WHERE pair_id=?', (pair_id,)).fetchone()
            if row is None:
                return None
            if row[0] != manifest:
                raise BudgetBlocked('comparison_pair_manifest_mismatch')
            if row[1] is None:
                raise BudgetBlocked('comparison_pair_incomplete_no_automatic_retry')
            return json.loads(row[1])

        try:
            # Short schema transaction only; never import/replay under a budget lock.
            with self.guard.ledger._connect() as db:
                db.execute('CREATE TABLE IF NOT EXISTS comparison_pairs (pair_id TEXT PRIMARY KEY, manifest TEXT NOT NULL, requested_at TEXT NOT NULL, result TEXT)')
            with self.guard.ledger._connect(readonly=True) as db:
                cached = existing(db)
            if cached is not None:
                return pair_id, cached
            if spec.neutral_sizing is not None:
                snapshot = spec.neutral_sizing
                signal = snapshot.get('signal') if isinstance(snapshot, dict) else None
                if not isinstance(signal, dict) or signal.get('signal_id') != spec.signal_id or signal.get('symbol') != spec.symbol:
                    raise ValueError('snapshot_identity_mismatch')
                if _time(snapshot.get('captured_at')) > _time(spec.input_available_at):
                    raise ValueError('snapshot_not_available_at_input')
                from .model_comparison_sizing import replay_policy_sizing
                replay_policy_sizing(snapshot, 'approve')
            if _time(spec.fixed_at) > now or _time(spec.input_available_at) > now:
                raise ValueError('policy_or_input_not_available')
            if spec.pricing_basis == 'paired_live_shadow' and (self.clock() - _time(spec.input_available_at)).total_seconds() > spec.max_input_age_seconds:
                raise ValueError('live_input_too_old')
            with self.guard.ledger._connect() as db:
                db.execute('BEGIN IMMEDIATE')
                cached = existing(db)  # Recheck after validation: another worker may have claimed.
                if cached is not None:
                    db.commit()
                    return pair_id, cached
                db.execute('INSERT INTO comparison_pairs VALUES (?,?,?,NULL)', (pair_id, manifest, now.isoformat()))
                db.commit()
                return pair_id, None
        except sqlite3.Error:
            raise BudgetStateError('comparison_pair_storage_unavailable') from None

    async def _attempt(self, pair_id, role, index, controls, prompt, system):
        attempt_id = f'{pair_id}:{role}:{index}'
        req = controls.request(prompt, system)
        try:
            result = await self.guard.call(req, dispatch=self.adapter, attempt_id=attempt_id)
            value = dict(result.value)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            record = self.guard.lookup(attempt_id)
            # Only a persisted pre-dispatch block without a reservation proves zero.
            not_sent = bool(record and record.get('state') == 'blocked' and record.get('reservation_token') is None)
            saved = record.get('result') if record else None
            value = dict(saved.value) if saved is not None else {}
            value.update(provider_success=value.get('success'), success=False,
                         content=value.get('content', ''),
                         api_cost_usd=(str(saved.actual_cost_usd) if saved is not None and saved.actual_cost_usd is not None
                                       else '0' if not_sent else None),
                         request_sent=value.get('request_sent', False if not_sent else None),
                         timing_valid=False, requested_model=req.model,
                         error=error.reason if isinstance(error, BudgetBlocked) else type(error).__name__)

        record = self.guard.lookup(attempt_id)
        value.update(attempt_id=attempt_id, role=role,
                     reservation_token=record.get('reservation_token') if record else None,
                     ledger_state=record.get('state') if record else None,
                     available_at=self.clock().isoformat())
        return value

    async def _role(self, pair_id, name, controls, prompt, system, retries):
        attempts, index = [], 0
        try:
            # The panel gives each parallel role one total deadline, including
            # its empty retry; a second request does not buy another 12 seconds.
            async with asyncio.timeout(controls.timeout_seconds):
                for index in range(retries + 1):
                    attempt = await self._attempt(pair_id, name, index, controls, prompt, system)
                    attempts.append(attempt)
                    if not attempt['success'] or attempt['content'].strip():
                        break
        except TimeoutError:
            attempt_id = f'{pair_id}:{name}:{index}'
            saved = self.guard.lookup(attempt_id)
            result = saved.get('result') if saved else None
            evidence = dict(result.value) if result is not None else {}
            evidence.update(attempt_id=attempt_id, role=name, success=False, content='',
                            requested_model=controls.model, error='TimeoutError', timing_valid=False,
                            api_cost_usd=str(result.actual_cost_usd) if result is not None and result.actual_cost_usd is not None else None,
                            reservation_token=saved.get('reservation_token') if saved else None,
                            ledger_state=saved.get('state') if saved else None,
                            available_at=self.clock().isoformat())
            attempts.append(evidence)
        return attempts

    async def _arm(self, pair_id, name, controls, spec, bear_task):
        bull = await self._role(pair_id, 'bull_' + name, controls, spec.common_context + BULL_SUFFIX, BULL_SYSTEM, spec.empty_retries)
        bear = await asyncio.shield(bear_task)
        b = _parse_verdict(bull[-1]['content'], 'APPROVE', 'REJECT') if bull[-1]['success'] else None
        r = _parse_verdict(bear[-1]['content'], 'ACCEPT', 'REJECT') if bear[-1]['success'] else None
        attempts = bull + bear
        fallback_missing = False
        approval_basis = 'adversarial_policy'
        validation_verdict = None
        # Timeouts invalidate the panel even if the other side returned, matching
        # the operational panel's whole-call timeout. Other failures allow one side.
        timed_out = any(a.get('error') == 'TimeoutError' for a in (bull[-1], bear[-1]))
        if (b is None and r is None) or timed_out:
            verdict = 'unavailable'
            fallback_missing = not spec.fallback_chain
            for index, fallback in enumerate(spec.fallback_chain):
                a = await self._attempt(pair_id, name + '_fallback', index, fallback, spec.fallback_prompt, spec.fallback_system)
                attempts.append(a)
                if a['success']:
                    # Reproduce the existing engine fallback policy, not a new
                    # parser policy. Keep its weak evidence distinct from approval.
                    verdict = 'reject' if a['content'].strip() and 'NO' in a['content'].strip().upper()[:10] else 'approve'
                    approval_basis = 'legacy_single_fallback_policy'
                    token = a['content'].strip().upper().split(maxsplit=1)
                    validation_verdict = ('approve' if token and token[0] == 'YES' else
                                          'reject' if token and token[0] == 'NO' else 'unavailable')
                    break
                if a.get('api_cost_usd') is None or a.get('error'):
                    break
        elif b is None or r is None:
            verdict = 'approve' if (r if b is None else b) else 'reject'
        else:
            verdict = 'approve' if (b and r) or (b != r and not spec.strict_on_disagreement) else 'reject'
        available = self.clock().isoformat()
        replay, reasons = None, []
        if fallback_missing:
            reasons.append('explicit_fallback_policy_missing')
        if spec.neutral_sizing is None:
            reasons.append('neutral_sizing_missing')
        else:
            try:
                from .model_comparison_sizing import replay_policy_sizing
                replay = replay_policy_sizing(spec.neutral_sizing, verdict)
                reasons.extend(replay.get('exclusions', ['downstream_admission_not_replayed']))
            except (ValueError, TypeError, KeyError):
                reasons.append('neutral_sizing_invalid')
        if approval_basis == 'legacy_single_fallback_policy' and validation_verdict == 'unavailable':
            reasons.append('fallback_policy_without_explicit_model_verdict')
        if any(a.get('requested_model_match') is not True for a in attempts if a.get('request_sent') is True):
            reasons.append('resolved_model_unverified_or_mismatch')
        if any(a.get('ledger_state') == 'pending_reconciliation' for a in attempts):
            reasons.append('budget_reconciliation_pending')
        if _sum_cost(attempts) is None:
            reasons.append('api_cost_unknown')
        if not all(a.get('timing_valid') is True for a in attempts):
            reasons.append('attempt_timing_unverified')
        return {'verdict': verdict, 'validation_verdict': validation_verdict or verdict,
                'approval_basis': approval_basis, 'unavailable_policy': 'fail_open_size_preserved',
                'api_cost_scope': 'scenario_cost_including_shared_bear_do_not_sum_arms',
                'bull_verdict': b, 'bear_verdict': r,
                'decision_available_at': available, 'attempts': attempts,
                'api_cost_usd': _sum_cost(attempts), 'sizing_replay': replay, 'reasons': reasons}

    async def run(self, spec: PairSpec):
        if not isinstance(spec, PairSpec):
            raise ValueError('PairSpec_required')
        spec.validate()
        for control in (spec.current, spec.candidate, spec.bear, *spec.fallback_chain):
            if control.model not in self.guard.policy.allowed_models:
                raise BudgetBlocked('comparison_model_not_allowed')
            if control.max_output_tokens > self.guard.policy.max_output_tokens:
                raise BudgetBlocked('comparison_output_cap')
        # Deeply freeze caller-owned snapshot before any await.
        spec = deepcopy(spec)
        now = self.clock()
        _time(now.isoformat())
        pair_id, cached = self._claim(spec, now)
        if cached is not None:
            return cached
        tasks = []
        try:
            bear = asyncio.create_task(self._role(pair_id, 'bear', spec.bear, spec.common_context + BEAR_SUFFIX, BEAR_SYSTEM, spec.empty_retries))
            tasks.append(bear)
            current = asyncio.create_task(self._arm(pair_id, 'current', spec.current, spec, bear))
            candidate = asyncio.create_task(self._arm(pair_id, 'candidate', spec.candidate, spec, bear))
            tasks += [current, candidate]
            a, b = await asyncio.gather(current, candidate)
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
        unique = {att['attempt_id']: att for arm in (a, b) for att in arm['attempts']}
        row = {'schema_version': VERSION, 'pair_id': pair_id, 'decision_id': spec.decision_id,
               'signal_id': spec.signal_id, 'symbol': spec.symbol, 'pricing_basis': spec.pricing_basis,
               'live_provenance': 'caller_supplied_not_attested',
               'latency_basis': 'worker_wall_clock_including_storage_and_scheduling',
               'requested_at': now.isoformat(),
               'evaluation_at': (now + timedelta(seconds=spec.horizon_seconds)).isoformat(),
               'policy': json.loads(_json(asdict(spec))), 'current': a, 'candidate': b,
               'actual_provider_attempts': sum(att.get('request_sent') is True for att in unique.values()),
               'unknown_transmission_attempts': sum(att.get('request_sent') is None for att in unique.values()),
               'admitted_attempts': len(unique), 'total_comparison_cost_usd': _sum_cost(list(unique.values())),
               'economic_evaluable': False, 'net_delta_krw': None, 'actual_execution': False,
               'reasons': ['downstream_admission_not_replayed', 'continuous_session_not_certified']}
        if spec.pricing_basis != 'paired_live_shadow':
            row['reasons'].append('offline_replay_not_live')
        try:
            with self.guard.ledger._connect() as db:
                db.execute('BEGIN IMMEDIATE')
                changed = db.execute('UPDATE comparison_pairs SET result=? WHERE pair_id=? AND result IS NULL', (_json(row), pair_id)).rowcount
                if changed != 1:
                    raise BudgetStateError('comparison_pair_storage_corrupt')
                db.commit()
        except (sqlite3.Error, BudgetStateError) as error:
            # Individual attempts remain durable. The caller can export this exact
            # row; never reconstruct completion times by rerunning the pair.
            raise PairResultPersistenceError(row) from error

        return row


class PairResultPersistenceError(BudgetStateError):
    def __init__(self, result):
        super().__init__('comparison_pair_result_unpersisted')
        self.result = deepcopy(result)



def report_pairs(pairs, *, payloads, ledger_status, max_delay_seconds=5.0, max_state_age_seconds=5.0):
    """Read-only source join. Raw code presence never certifies normal session.

    Existing outcome calculator remains separate until downstream admission and
    continuous-session evidence are supplied by a validated producer.
    """
    for value in (max_delay_seconds, max_state_age_seconds):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
            raise ValueError('invalid_report_time_limit')
    rows, seen = [], set()
    for raw in pairs:
        row = deepcopy(raw)
        if not isinstance(row, dict) or row.get('schema_version') != VERSION or not isinstance(row.get('pair_id'), str):
            raise ValueError('invalid_pair_record')
        if row['pair_id'] in seen:
            raise ValueError('duplicate_pair_id')
        seen.add(row['pair_id'])
        reasons = list(row.get('reasons', []))
        requested = _time(row['requested_at'])
        horizon = row['policy']['horizon_seconds']
        if type(horizon) is not int or not 0 < horizon <= 86400:
            raise ValueError('invalid_report_horizon')
        expected_evaluation = requested + timedelta(seconds=horizon)
        valid_evaluation = _time(row['evaluation_at']) == expected_evaluation
        if not valid_evaluation:
            reasons.append('evaluation_policy_mismatch')
        for name in ('current', 'candidate'):
            arm = row[name]
            try:
                valid_decision = requested <= _time(arm['decision_available_at']) < expected_evaluation
            except (ValueError, TypeError):
                valid_decision = False
            if not valid_decision:
                reasons.append(name + ':decision_time_outside_policy')
            decision = {'decision_id': row['pair_id'] + ':' + name, 'symbol': row['symbol'],
                        'decision_available_at': arm['decision_available_at'] if valid_decision else None}
            joined = join_decisions([decision], payloads, ledger_status=ledger_status,
                                    max_delay_seconds=max_delay_seconds, max_state_age_seconds=max_state_age_seconds)[0]
            arm['quote_evidence'] = joined
            reasons.extend(name + ':' + reason for reason in arm['reasons'] + joined['reasons'])
        # join_decisions uses >; subtract one microsecond to select >= evaluation.
        at = expected_evaluation - timedelta(microseconds=1)
        row['exit_quote_evidence'] = join_decisions(
            [{'decision_id': row['pair_id'] + ':exit', 'symbol': row['symbol'], 'decision_available_at': at.isoformat() if valid_evaluation else None}],
            payloads, ledger_status=ledger_status, max_delay_seconds=max_delay_seconds + 0.000001,
            max_state_age_seconds=max_state_age_seconds)[0]
        reasons.extend('exit:' + reason for reason in row['exit_quote_evidence']['reasons'])
        # Never trust a caller-edited positive economic flag in an input file.
        reasons += ['downstream_admission_not_replayed', 'continuous_session_not_certified']
        if row.get('pricing_basis') != 'paired_live_shadow':
            reasons.append('offline_replay_not_live')
        row.update(economic_evaluable=False, actual_execution=False, net_delta_krw=None,
                   reasons=sorted(set(reasons)))
        rows.append(row)
    return {'schema_version': 'model-comparison-report-v3',
            'summary': {'pairs': len(rows), 'economic_evaluable': 0,
                        'current_verdicts': {v: sum(r['current']['verdict'] == v for r in rows) for v in ('approve', 'reject', 'unavailable')},
                        'candidate_verdicts': {v: sum(r['candidate']['verdict'] == v for r in rows) for v in ('approve', 'reject', 'unavailable')}},
            'rows': rows}
