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

VERSION = 'model-comparison-pair-v2'
LEGACY_VERSION = 'model-comparison-pair-v1'  # requested_at+horizon 기준. 보고서가 그 의미 그대로 읽는다.
ANCHORS = ('requested_at', 'input_available_at')


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
    # 운영 SINGLE_FALLBACK_TIMEOUT 과 같은 의미: primary→fallback 체인 **전체** 시한(시도별 timeout 과 별개).
    fallback_chain_timeout_seconds: float
    # Explicit fallback prompt/system/chain required when both roles fail.
    # No inference of a fallback prompt from a different historical decision.
    fallback_prompt: str | None = None
    fallback_system: str = ''
    fallback_chain: tuple[RoleControls, ...] = ()
    empty_retries: int = 1
    horizon_seconds: int = 900
    max_input_age_seconds: float = 5.0
    # v2: 평가 기준시각. live 는 공통 입력 구성 시각(input_available_at)+horizon 이다(연구 계약).
    evaluation_anchor: str = 'requested_at'
    enqueued_at: str | None = None          # 생산자가 작업자에게 넘긴 시각(handed_off_at)
    live_provenance: str = 'caller_supplied_not_attested'
    # 작업자가 같은 프로세스 봉투를 검증한 뒤 붙이는 HMAC. 라벨만으로는 live 가 아니다. manifest 에서 제외.
    attestation: str | None = None

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
        _positive(self.fallback_chain_timeout_seconds, 'fallback_chain_timeout_seconds')
        if self.evaluation_anchor not in ANCHORS:
            raise ValueError('invalid_evaluation_anchor')
        if self.live_provenance not in {'caller_supplied_not_attested', 'in_process_attested'}:
            raise ValueError('invalid_live_provenance')
        if self.enqueued_at is not None and _time(self.enqueued_at) < _time(self.input_available_at):
            raise ValueError('enqueued_before_input_available')
        if self.pricing_basis == 'paired_live_shadow':
            if self.evaluation_anchor != 'input_available_at' or self.live_provenance != 'in_process_attested':
                raise ValueError('live_requires_input_anchor_and_in_process_attestation')
            from .model_comparison_input import verify_pair_attestation
            if self.enqueued_at is None or not verify_pair_attestation(self):
                raise ValueError('live_provenance_unattested')
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

    def snapshot(self):
        """Persisted spec: the per-process attestation is evidence of this run, not pair identity."""
        value = asdict(self)
        value.pop('attestation')
        return value


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


BLOCKED_VERSION = 'model-comparison-pair-blocked-v1'
# 쌍 입장 전 차단 — 표식 행이 남아 같은 판단이 다음날 자동 재실행되지 않는다.
PAIR_BLOCK_REASONS = ('comparison_daily_pair_cap', 'comparison_input_too_large')


def context_bytes(text) -> int:
    return len(text.encode('utf-8', 'surrogatepass'))


def failed_attempt_value(saved, *, not_sent, requested_model, error):
    """실패 시도의 기록값. 저장 응답(ComparisonAttemptResult|None)과 오류만으로 결정된다 — 복구가 같은 함수로 재계산한다.

    0원은 예약 없는 전송 전 차단(not_sent)만 증명한다. 저장 응답이 있으면 그 측정 비용을 그대로 쓴다.
    """
    value = dict(saved.value) if saved is not None else {}
    cost = None if saved is None or saved.actual_cost_usd is None else str(saved.actual_cost_usd)
    value.update(provider_success=value.get('success'), success=False, content=value.get('content', ''),
                 api_cost_usd=cost if saved is not None else '0' if not_sent else None,
                 request_sent=value.get('request_sent', False if not_sent else None),
                 timing_valid=False, requested_model=requested_model, error=error)
    return value


def finalize_row(pair_id, snapshot, requested_at, current, candidate):
    """저장 spec 스냅샷·입장 시각·두 arm 으로 쌍 행을 만든다(순수). arm 의 지연 필드를 채운다."""
    now = _time(requested_at)
    unique = {att['attempt_id']: att for arm in (current, candidate) for att in arm['attempts']}
    times = {'input_available_at': snapshot['input_available_at'], 'enqueued_at': snapshot['enqueued_at'],
             'requested_at': requested_at}
    queue_delay = (None if snapshot['enqueued_at'] is None else
                   (now - _time(snapshot['enqueued_at'])).total_seconds() * 1000)
    for arm in (current, candidate):
        latencies = [att.get('latency_ms') for att in arm['attempts']]
        arm['queue_delay_ms'] = queue_delay
        arm['provider_latency_ms'] = (None if not latencies or any(v is None for v in latencies)
                                      else sum(latencies))
    anchor = snapshot['evaluation_anchor']
    row = {'schema_version': VERSION, 'pair_id': pair_id, 'decision_id': snapshot['decision_id'],
           'signal_id': snapshot['signal_id'], 'symbol': snapshot['symbol'], 'pricing_basis': snapshot['pricing_basis'],
           'live_provenance': snapshot['live_provenance'],
           'latency_basis': 'worker_wall_clock_including_storage_and_scheduling',
           **times, 'evaluation_anchor': anchor,
           'evaluation_at': (_time(times[anchor]) + timedelta(seconds=snapshot['horizon_seconds'])).isoformat(),
           'policy': json.loads(_json(snapshot)), 'current': current, 'candidate': candidate,
           'actual_provider_attempts': sum(att.get('request_sent') is True for att in unique.values()),
           'unknown_transmission_attempts': sum(att.get('request_sent') is None for att in unique.values()),
           'admitted_attempts': len(unique), 'total_comparison_cost_usd': _sum_cost(list(unique.values())),
           'economic_evaluable': False, 'net_delta_krw': None, 'actual_execution': False,
           'reasons': ['downstream_admission_not_replayed', 'continuous_session_not_certified']}
    if snapshot['pricing_basis'] != 'paired_live_shadow':
        row['reasons'].append('offline_replay_not_live')
    return row


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

    def pair_id(self, decision_id) -> str:
        return hashlib.sha256(_json([self.guard.policy.experiment_id, decision_id]).encode()).hexdigest()

    def _schema(self):
        """Short schema transaction only. admission_day 는 입장한(지출 가능) 쌍의 KST 날짜, 차단 표식은 NULL."""
        with self.guard.ledger._connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS comparison_pairs (pair_id TEXT PRIMARY KEY, manifest TEXT NOT NULL, '
                       'requested_at TEXT NOT NULL, result TEXT, admission_day TEXT)')
            if 'admission_day' not in {row[1] for row in db.execute('PRAGMA table_info(comparison_pairs)')}:
                db.execute('ALTER TABLE comparison_pairs ADD COLUMN admission_day TEXT')

    def admitted_pair_ids(self, day: str) -> set:
        """그날 입장한 쌍 ID(작업자 재시작 때 일 상한 복원용). 차단 표식은 입장이 아니라 빠진다."""
        try:
            self._schema()
            with self.guard.ledger._connect(readonly=True) as db:
                return {row[0] for row in db.execute('SELECT pair_id FROM comparison_pairs WHERE admission_day=?', (day,))}
        except sqlite3.Error:
            raise BudgetStateError('comparison_pair_storage_unavailable') from None

    def _claim(self, spec, now):
        pair_id = self.pair_id(spec.decision_id)
        manifest = _json({'schema_version': VERSION, 'policy': self.guard.policy.serialized(), 'spec': spec.snapshot()})

        def existing(db):
            row = db.execute('SELECT manifest,result FROM comparison_pairs WHERE pair_id=?', (pair_id,)).fetchone()
            if row is None:
                return None
            if row[0] != manifest:
                raise BudgetBlocked('comparison_pair_manifest_mismatch')
            if row[1] is None:
                raise BudgetBlocked('comparison_pair_incomplete_no_automatic_retry')
            result = json.loads(row[1])
            if isinstance(result, dict) and result.get('schema_version') == BLOCKED_VERSION:
                raise BudgetBlocked(result.get('reason') or 'comparison_pair_blocked')   # 캐시 결과가 아니다
            return result

        def block(db, reason):
            # 같은 판단이 다음날 자동 재실행되지 않게 하는 표식. admission_day 는 NULL(입장 아님).
            marker = _json({'schema_version': BLOCKED_VERSION, 'reason': reason, 'blocked_at': now.isoformat()})
            db.execute('INSERT INTO comparison_pairs (pair_id,manifest,requested_at,result,admission_day) '
                       'VALUES (?,?,?,?,NULL)', (pair_id, manifest, now.isoformat(), marker))
            db.commit()
            raise BudgetBlocked(reason)

        try:
            # Never import/replay under a budget lock.
            self._schema()
            with self.guard.ledger._connect(readonly=True) as db:
                cached = existing(db)
            if cached is not None:
                return pair_id, cached
            if context_bytes(spec.common_context) > self.guard.policy.max_common_context_bytes:
                with self.guard.ledger._connect() as db:
                    db.execute('BEGIN IMMEDIATE')
                    cached = existing(db)
                    if cached is not None:
                        db.commit()
                        return pair_id, cached
                    block(db, 'comparison_input_too_large')
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
                # 하루 쌍 상한은 같은 쓰기 잠금 안에서 센다 — 공급자 호출·예약 전에 막는다.
                day = self.guard.ledger.today().isoformat()
                used = db.execute('SELECT COUNT(*) FROM comparison_pairs WHERE admission_day=?', (day,)).fetchone()[0]
                if used >= self.guard.policy.pairs_per_day:
                    block(db, 'comparison_daily_pair_cap')
                db.execute('INSERT INTO comparison_pairs (pair_id,manifest,requested_at,result,admission_day) '
                           'VALUES (?,?,?,NULL,?)', (pair_id, manifest, now.isoformat(), day))
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
            value = self._failure(attempt_id, req.model,
                                  error.reason if isinstance(error, BudgetBlocked) else type(error).__name__)
        return self._stamp(value, attempt_id, role)

    def _failure(self, attempt_id, requested_model, error):
        record = self.guard.lookup(attempt_id)
        # Only a persisted pre-dispatch block without a reservation proves zero.
        not_sent = bool(record and record.get('state') == 'blocked' and record.get('reservation_token') is None)
        return failed_attempt_value(record.get('result') if record else None, not_sent=not_sent,
                                    requested_model=requested_model, error=error)

    def _stamp(self, value, attempt_id, role):
        record = self.guard.lookup(attempt_id)
        value.update(attempt_id=attempt_id, role=role,
                     reservation_token=record.get('reservation_token') if record else None,
                     ledger_state=record.get('state') if record else None,
                     available_at=self.clock().isoformat())
        return value

    def _timed_out(self, pair_id, role, index, requested_model):
        """시한에 끊긴 시도. 이미 전송됐을 수 있어 재전송하지 않고 원장 상태만 기록한다."""
        attempt_id = f'{pair_id}:{role}:{index}'
        return self._stamp(self._failure(attempt_id, requested_model, 'TimeoutError'), attempt_id, role)

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
            attempts.append(self._timed_out(pair_id, name, index, controls.model))
        return attempts

    async def _arm(self, pair_id, name, controls, spec, bear_task):
        bull = await self._role(pair_id, 'bull_' + name, controls, spec.common_context + BULL_SUFFIX, BULL_SYSTEM, spec.empty_retries)
        bear = await asyncio.shield(bear_task)
        b = _parse_verdict(bull[-1]['content'], 'APPROVE', 'REJECT') if bull[-1]['success'] else None
        r = _parse_verdict(bear[-1]['content'], 'ACCEPT', 'REJECT') if bear[-1]['success'] else None
        attempts = bull + bear
        fallback_missing = fallback_timed_out = False
        approval_basis = 'adversarial_policy'
        validation_verdict = None
        # Timeouts invalidate the panel even if the other side returned, matching
        # the operational panel's whole-call timeout. Other failures allow one side.
        timed_out = any(a.get('error') == 'TimeoutError' for a in (bull[-1], bear[-1]))
        if (b is None and r is None) or timed_out:
            verdict = 'unavailable'
            fallback_missing = not spec.fallback_chain
            index = 0
            try:
                # 운영 SINGLE_FALLBACK_TIMEOUT 처럼 체인 전체 시한. 넘기면 남은 폴백은 보내지 않고 unavailable(수량 보존).
                async with asyncio.timeout(spec.fallback_chain_timeout_seconds):
                    for index, fallback in enumerate(spec.fallback_chain):
                        a = await self._attempt(pair_id, name + '_fallback', index, fallback,
                                                spec.fallback_prompt, spec.fallback_system)
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
            except TimeoutError:
                attempts.append(self._timed_out(pair_id, name + '_fallback', index, spec.fallback_chain[index].model))
                verdict, approval_basis, validation_verdict, fallback_timed_out = (
                    'unavailable', 'adversarial_policy', None, True)
        elif b is None or r is None:
            verdict = 'approve' if (r if b is None else b) else 'reject'
        else:
            verdict = 'approve' if (b and r) or (b != r and not spec.strict_on_disagreement) else 'reject'
        available = self.clock().isoformat()
        replay, reasons = None, []
        if fallback_missing:
            reasons.append('explicit_fallback_policy_missing')
        if fallback_timed_out:
            reasons.append('fallback_chain_timeout')
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
        row = finalize_row(pair_id, spec.snapshot(), now.isoformat(), a, b)
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
        if (not isinstance(row, dict) or row.get('schema_version') not in (VERSION, LEGACY_VERSION)
                or not isinstance(row.get('pair_id'), str)):
            raise ValueError('invalid_pair_record')
        if row['pair_id'] in seen:
            raise ValueError('duplicate_pair_id')
        seen.add(row['pair_id'])
        reasons = list(row.get('reasons', []))
        if row['schema_version'] == LEGACY_VERSION:
            # v1 rows keep their original meaning; never reinterpret them as input-anchored.
            row['evaluation_anchor'] = 'requested_at'
            reasons.append('legacy_v1_requested_at_anchor')
        elif row.get('evaluation_anchor') not in ANCHORS:
            raise ValueError('invalid_pair_record')
        requested = _time(row['requested_at'])
        anchor = _time(row[row['evaluation_anchor']])
        horizon = row['policy']['horizon_seconds']
        if type(horizon) is not int or not 0 < horizon <= 86400:
            raise ValueError('invalid_report_horizon')
        expected_evaluation = anchor + timedelta(seconds=horizon)
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
        elif row.get('live_provenance') != 'in_process_attested' or row['evaluation_anchor'] != 'input_available_at':
            reasons.append('live_provenance_not_attested')
        row.update(economic_evaluable=False, actual_execution=False, net_delta_krw=None,
                   reasons=sorted(set(reasons)))
        rows.append(row)
    return {'schema_version': 'model-comparison-report-v3',
            'summary': {'pairs': len(rows), 'economic_evaluable': 0,
                        'current_verdicts': {v: sum(r['current']['verdict'] == v for r in rows) for v in ('approve', 'reject', 'unavailable')},
                        'candidate_verdicts': {v: sum(r['candidate']['verdict'] == v for r in rows) for v in ('approve', 'reject', 'unavailable')}},
            'rows': rows}
