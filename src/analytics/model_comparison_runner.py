"""Explicit paired shadow worker; never installed in the trading event loop.

One Bull control changes, one common Bear supplies the other role. Each empty
retry and explicit fallback is separately admitted by ComparisonCallGuard.
Raw provider clients make one HTTP attempt; LLMManager is intentionally excluded.
Reports preserve sizing-only and raw market-state limitations, never certify fills.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json
import math
import sqlite3
from typing import Any

from .decision_quote_join import join_decisions
from .model_comparison_contract import DOCUMENTED_SNAPSHOTS, FALLBACK as BEAR_API_FALLBACK_KINDS
from .model_comparison_execution import ComparisonAttemptResult, ComparisonCallGuard, ComparisonRequest
from ..core.adversarial_validator import BULL_SUFFIX, BULL_SYSTEM, BEAR_SUFFIX, BEAR_SYSTEM, _parse_verdict
from ..utils.llm import OpenAIClient, GeminiClient, LLMProvider, LLMResponse, _validate_controls
from ..utils.llm_budget import BudgetBlocked, BudgetStateError
from ..utils.llm_pricing import rate_quote

VERSION = 'model-comparison-pair-v2'
LEGACY_VERSION = 'model-comparison-pair-v1'  # requested_at+horizon 기준. 보고서가 그 의미 그대로 읽는다.
ANCHORS = ('requested_at', 'input_available_at')
# 판정 정책. legacy: 한쪽 의견으로 approve·양쪽 실패 시 명시 단일 모델 폴백(역사적 GPT/Gemini 쌍의 의미 그대로).
# current: 운영 AdversarialValidator(requires_independent_review)·오프라인 계약 POLICY 와 같이 양쪽 유효 의견이 있어야
# approve/reject, 아니면 hold. 단일 모델 폴백 없음, strict_on_disagreement False.
LEGACY_REVIEW_POLICY = 'legacy-single-opinion-v1'
CURRENT_REVIEW_POLICY = 'entry-independent-review-v1'
REVIEW_POLICIES = (LEGACY_REVIEW_POLICY, CURRENT_REVIEW_POLICY)
# LLMManager.complete_with 와 같은 Bear CLI 시한 규칙: 내부 예산의 절반, 최대 8초.
BEAR_CLI_TIMEOUT_CAP_SECONDS = 8.0


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
    if model.startswith('claude-'):
        return LLMProvider.ANTHROPIC
    raise ValueError('unsupported_provider_model')


@dataclass(frozen=True)
class RoleControls:
    model: str
    max_output_tokens: int
    reasoning_effort: str | None = None
    temperature: float | None = 0.3     # Claude 역할만 None(전송 미지원 → 요청에서 제외). GPT/Gemini 는 숫자 필수.
    seed: int | None = None
    timeout_seconds: float = 12.0
    # subscription 은 Claude CLI 전송만 선언한다. 기존 GPT/Gemini 역할·역사적 manifest 는 기본 api 그대로다.
    billing_channel: str = 'api'

    def request(self, prompt, system):
        request = ComparisonRequest(prompt=prompt, system=system, **asdict(self))
        provider = _provider(self.model)
        if provider == LLMProvider.ANTHROPIC:
            # 운영 entry Bear 와 같은 Claude 역할 제어(low effort·temperature/seed 없음). Team Bear 의 제어는 여기 없다.
            if self.reasoning_effort != 'low' or self.temperature is not None or self.seed is not None:
                raise ValueError('claude_controls_require_low_effort_without_temperature_or_seed')
            return request
        if self.billing_channel != 'api':
            raise ValueError('subscription_requires_claude_cli_role')
        if self.temperature is None:
            raise ValueError('temperature_required_for_openai_gemini_role')
        _validate_controls(self.model, provider, {'reasoning_effort': self.reasoning_effort, 'seed': self.seed})
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
    # None: 공통 Bear 도 empty_retries 를 따른다(역사적 GPT/Gemini 쌍). Claude Bear 는 운영 entry Bear 처럼 0 을 명시한다.
    bear_empty_retries: int | None = None
    review_policy: str = LEGACY_REVIEW_POLICY
    # 현행 Bear 시한 소유권: bear.timeout_seconds 는 역할(패널) 총 시한, bear_internal_timeout_seconds 는 그 안의 Bear 체인
    # 예산(운영 call_timeout_seconds 11.5), bear_cli_timeout_seconds 는 CLI 단일 시도 시한(≤ min(8, 내부/2)).
    # bear_api_fallback 은 허용된 CLI 실패 뒤 **남은 내부 예산**으로 한 번 보내는 명시 API 제어(채널 api, 같은 모델).
    bear_internal_timeout_seconds: float | None = None
    bear_cli_timeout_seconds: float | None = None
    bear_api_fallback: RoleControls | None = None
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
        if self.bear_empty_retries is not None and (type(self.bear_empty_retries) is not int
                                                    or not 0 <= self.bear_empty_retries <= 1):
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
        if not isinstance(self.fallback_chain, tuple) or len(self.fallback_chain) > 2:
            raise ValueError('invalid_fallback_chain')
        if self.fallback_chain and (not isinstance(self.fallback_prompt, str) or not self.fallback_prompt):
            raise ValueError('explicit_fallback_prompt_required')
        self.validate_profile(current=self.current, candidate=self.candidate, bear=self.bear, fallback_chain=self.fallback_chain,
                              strict_on_disagreement=self.strict_on_disagreement, bear_empty_retries=self.bear_empty_retries,
                              review_policy=self.review_policy, bear_internal_timeout_seconds=self.bear_internal_timeout_seconds,
                              bear_cli_timeout_seconds=self.bear_cli_timeout_seconds, bear_api_fallback=self.bear_api_fallback)
        _json(asdict(self))  # Reject non-JSON snapshots and nonfinite numbers before admission.

    @staticmethod
    def validate_profile(*, current, candidate, bear, fallback_chain, strict_on_disagreement, bear_empty_retries,
                         review_policy, bear_internal_timeout_seconds, bear_cli_timeout_seconds, bear_api_fallback):
        """역할·판정 정책·Bear 시한/전송 프로필 규칙. worker 도 manifest 를 받을 때 같은 검사를 쓴다."""
        extra = (bear_api_fallback,) if bear_api_fallback is not None else ()
        controls = (current, candidate, bear, *fallback_chain, *extra)
        if not all(isinstance(c, RoleControls) for c in controls):
            raise ValueError('explicit_role_controls_required')
        if review_policy not in REVIEW_POLICIES:
            raise ValueError('invalid_review_policy')
        # Bull 두 arm 은 OpenAI, Bear 는 Gemini(역사적 쌍) 또는 Claude(현행 entry Bear). 명시 폴백 체인의 공급자는
        # 역사적 의미 그대로(OpenAI/Gemini)이며 Claude 만 제외한다 — CLI→API 대체는 상위 흐름(_bear_role)의 몫이다.
        bear_provider = _provider(bear.model)
        if (_provider(current.model) != LLMProvider.OPENAI or _provider(candidate.model) != LLMProvider.OPENAI
                or bear_provider not in (LLMProvider.GEMINI, LLMProvider.ANTHROPIC)
                or any(_provider(c.model) == LLMProvider.ANTHROPIC for c in fallback_chain)):
            raise ValueError('role_provider_mismatch')
        if bear_provider == LLMProvider.ANTHROPIC and bear_empty_retries != 0:
            raise ValueError('claude_bear_empty_retries_must_be_zero')
        for value, label in ((bear_internal_timeout_seconds, 'bear_internal_timeout_seconds'),
                             (bear_cli_timeout_seconds, 'bear_cli_timeout_seconds')):
            if value is not None:
                _positive(value, label)
        if bear_internal_timeout_seconds is not None and bear_internal_timeout_seconds > bear.timeout_seconds:
            raise ValueError('bear_internal_timeout_exceeds_role_timeout')
        if bear_cli_timeout_seconds is not None:
            if bear_internal_timeout_seconds is None:
                raise ValueError('bear_cli_timeout_requires_internal_timeout')
            if bear_cli_timeout_seconds > min(BEAR_CLI_TIMEOUT_CAP_SECONDS, bear_internal_timeout_seconds / 2):
                raise ValueError('bear_cli_timeout_exceeds_contract')      # 운영 min(8, 내부/2) 보다 늘릴 수 없다
        if bear_api_fallback is not None:
            if bear_provider != LLMProvider.ANTHROPIC or bear.billing_channel != 'subscription':
                raise ValueError('bear_api_fallback_requires_cli_primary_claude_bear')
            if (_provider(bear_api_fallback.model) != LLMProvider.ANTHROPIC or bear_api_fallback.billing_channel != 'api'
                    or bear_api_fallback.model != bear.model or bear_api_fallback.reasoning_effort != 'low'
                    or bear_api_fallback.max_output_tokens != bear.max_output_tokens      # 운영 대체와 같은 출력 상한
                    or bear_api_fallback.temperature is not None or bear_api_fallback.seed is not None):
                raise ValueError('bear_api_fallback_controls_invalid')
            if bear_cli_timeout_seconds is None:
                raise ValueError('bear_cli_timeout_required_for_api_fallback')
            if bear_api_fallback.timeout_seconds > bear_internal_timeout_seconds:
                raise ValueError('bear_api_fallback_timeout_exceeds_internal')
        if review_policy == CURRENT_REVIEW_POLICY:
            if bear_provider != LLMProvider.ANTHROPIC or bear.billing_channel != 'subscription':
                raise ValueError('current_policy_requires_claude_bear_cli_primary')
            if fallback_chain:
                raise ValueError('current_policy_has_no_single_model_fallback')
            if strict_on_disagreement:
                raise ValueError('current_policy_requires_strict_on_disagreement_false')
            if bear_internal_timeout_seconds is None:
                raise ValueError('current_policy_requires_bear_internal_timeout')
            if bear_cli_timeout_seconds is None:
                raise ValueError('current_policy_requires_bear_cli_timeout')   # API 대체가 없어도 CLI 시한은 계약대로
        changed = {k for k in asdict(current) if getattr(current, k) != getattr(candidate, k)}
        if len(changed) != 1 or not changed <= {'model', 'reasoning_effort', 'max_output_tokens'}:
            raise ValueError('one_comparison_variable_required')
        for control in controls:
            control.request('validate', '')

    def snapshot(self):
        """Persisted spec: the per-process attestation is evidence of this run, not pair identity.

        Claude 역할용 새 필드는 기본값이면 생략해 역사적 GPT/Gemini 쌍의 manifest·policy 스냅샷을 바이트 동일하게 둔다
        (저장 manifest 대조·incomplete 행 복구가 완전 일치 비교라서). Claude Bear 스냅샷에만 새 키가 나타난다.
        """
        value = asdict(self)
        value.pop('attestation')
        if value['bear_empty_retries'] is None:
            value.pop('bear_empty_retries')
        if value['review_policy'] == LEGACY_REVIEW_POLICY:
            value.pop('review_policy')
        for key in ('bear_internal_timeout_seconds', 'bear_cli_timeout_seconds', 'bear_api_fallback'):
            if value[key] is None:
                value.pop(key)
        for role in (value['current'], value['candidate'], value['bear'], *value['fallback_chain']):
            if role.get('billing_channel') == 'api':
                role.pop('billing_channel')
        return value


class SingleAttemptAdapter:
    """Owns no budget, retries, fallback, credentials, or client lifecycle.

    The caller supplies raw clients and closes them. Price table output is a
    usage-based estimate, not an invoice or provider-reported billed USD.
    """
    billing_channel = 'api'   # 유료 API 전송만 한다. 가드가 전송 전에 요청 채널과 대조한다.

    def __init__(self, *, openai: OpenAIClient, gemini: GeminiClient, clock=_now):
        if not isinstance(openai, OpenAIClient) or not isinstance(gemini, GeminiClient):
            raise ValueError('raw_provider_clients_required; LLMManager forbidden')
        self.openai, self.gemini, self.clock = openai, gemini, clock

    async def __call__(self, request: ComparisonRequest):
        if request.billing_channel != self.billing_channel:
            # 구독 요청은 월 예약이 없으므로 이 어댑터가 호출하면 예약 없는 과금이 된다. 공급자 호출 전에 거절한다.
            raise ValueError('api_adapter_requires_api_billing_channel')
        provider = _provider(request.model)
        if provider not in (LLMProvider.OPENAI, LLMProvider.GEMINI):
            raise ValueError('unsupported_provider_model')      # Claude 는 ClaudeSingleAttemptAdapter 가 맡는다
        if request.temperature is None:
            raise ValueError('temperature_required_for_openai_gemini_role')
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
        cost, cost_basis, settlement = None, 'unknown', 'measured'
        tokens_valid = all(type(v) is int and v >= 0 for v in (response.input_tokens, response.output_tokens))
        identity_matches = response.model == request.model and response.requested_model == request.model and response.provider == provider
        if response.request_sent is False and response.billing_status == 'not_sent':
            cost, cost_basis = Decimal('0'), 'transport_not_sent'
        elif (response.billing_status == 'rejected_before_generation' and response.request_sent is True
              and response.usage_known is not True and response.success is not True):
            # raw 클라이언트의 생성 전 거절 추론(429/4xx + 사용량 필드 없음). LLMManager 와 같이 예약을 해제하고
            # 과금 0 으로 둔다 — unknown 으로 남기면 공유 월 원장의 운영 호출까지 막힌다(재리뷰 P1).
            cost, cost_basis, settlement = Decimal('0'), 'rejected_before_generation_inferred', 'rejected_before_generation'
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
        return ComparisonAttemptResult(value, cost, settlement)


_CONTRACT_TRANSPORTS = {'openai': 'openai_chat'}   # 계약 v2 의 전송 이름(공식 snapshot 표의 키)


def _identity_certified(attempt) -> bool:
    """현행 정책의 응답 신원: 요청 모델과 정확히 같거나 공식 문서화된 snapshot(계약 v2)만 인정한다. None 은 미확인이다."""
    if attempt.get('requested_model_match') is True:
        return True
    transport = attempt.get('transport')
    if transport is None:                       # 명시된 빈 전송값은 공급자 기본값으로 바꾸지 않는다
        transport = _CONTRACT_TRANSPORTS.get(attempt.get('provider'))
    return (transport, attempt.get('requested_model'), attempt.get('resolved_model')) in DOCUMENTED_SNAPSHOTS


def _api_fallback_authorized(attempt) -> bool:
    """LLMManager.complete_with 와 같은 API 대체 인가: 가드 예외·역할 시한 없이 전송된 CLI 시도가 성공·거절이 아니고
    허용된 실패 종류일 때만. 신원 불일치·정책 위반·취소·차단은 대체하지 않는다."""
    return (attempt.get('error') is None and attempt.get('success') is not True
            and attempt.get('refusal_detected') is not True
            and attempt.get('transport_error_kind') in BEAR_API_FALLBACK_KINDS)


def _sum_cost(attempts):
    """API 채널 시도만 합산한다. 구독 전송은 API 청구 대상이 아니라 None 도 0 도 아닌 '해당 없음' 이다(별도 사유로 표시)."""
    values = [a.get('api_cost_usd') for a in attempts if a.get('billing_channel') != 'subscription']
    return None if any(v is None for v in values) else str(sum((Decimal(v) for v in values), Decimal('0')))


BLOCKED_VERSION = 'model-comparison-pair-blocked-v1'
# 쌍 입장 전 차단 — 표식 행이 남아 같은 판단이 다음날 자동 재실행되지 않는다.
PAIR_BLOCK_REASONS = ('comparison_daily_pair_cap', 'comparison_input_too_large')


def context_bytes(text) -> int:
    return len(text.encode('utf-8', 'surrogatepass'))


def failed_attempt_value(saved, *, not_sent, requested_model, error, billing_channel=None):
    """실패 시도의 기록값. 저장 응답(ComparisonAttemptResult|None)과 오류만으로 결정된다 — 복구가 같은 함수로 재계산한다.

    0원은 예약 없는 전송 전 차단(not_sent)만 증명한다. 저장 응답이 있으면 그 측정 비용을 그대로 쓴다.
    구독 채널의 실패는 저장 응답이 없어도 채널을 남겨 API 합계·구독 집계에서 유료 시도와 섞이지 않게 한다
    (유료 채널은 역사적 행 형식 보존을 위해 키를 더하지 않는다).
    """
    value = dict(saved.value) if saved is not None else {}
    cost = None if saved is None or saved.actual_cost_usd is None else str(saved.actual_cost_usd)
    value.update(provider_success=value.get('success'), success=False, content=value.get('content', ''),
                 api_cost_usd=cost if saved is not None else '0' if not_sent else None,
                 request_sent=value.get('request_sent', False if not_sent else None),
                 timing_valid=False, requested_model=requested_model, error=error)
    if billing_channel == 'subscription':
        value['billing_channel'] = 'subscription'
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
    subscription_attempts = sum(att.get('billing_channel') == 'subscription' for att in unique.values())
    if subscription_attempts:
        # 구독 시도가 있을 때만 적는다 — 역사적 행(유료만)의 형식을 바꾸지 않아 완전 일치 복구가 유지된다.
        row['subscription_attempts'] = subscription_attempts
    if snapshot['pricing_basis'] != 'paired_live_shadow':
        row['reasons'].append('offline_replay_not_live')
    return row


class PairedComparisonRunner:
    """Separate worker API only. Explicit pair claim prevents concurrent/rerun spend.

    Completed pairs are recoverable. An interrupted pair remains incomplete;
    individual durable attempts are inspectable via guard.lookup, never resent.
    This conservative boundary does not repair ambiguous crash outcomes.
    """
    def __init__(self, *, guard: ComparisonCallGuard, adapter: SingleAttemptAdapter, clock=_now, claude_adapter=None,
                 claude_api_adapter=None):
        if not isinstance(guard, ComparisonCallGuard) or not isinstance(adapter, SingleAttemptAdapter):
            raise ValueError('explicit_guard_and_single_attempt_adapter_required')
        from .model_comparison_claude import ClaudeSingleAttemptAdapter
        # 채널별 Claude 단일 시도 전송(subscription=CLI, api=Anthropic API). 각 요청은 자기 채널의 어댑터로만 간다.
        self._claude_adapters = {}
        for candidate in (claude_adapter, claude_api_adapter):
            if candidate is None:
                continue
            if not isinstance(candidate, ClaudeSingleAttemptAdapter):
                raise ValueError('claude_single_attempt_adapter_required')
            if candidate.billing_channel in self._claude_adapters:
                raise ValueError('claude_adapter_channel_duplicate')
            self._claude_adapters[candidate.billing_channel] = candidate
        self.guard, self.adapter, self.clock = guard, adapter, clock
        self.claude_adapter, self.claude_api_adapter = claude_adapter, claude_api_adapter

    def _dispatch_for(self, request):
        if _provider(request.model) != LLMProvider.ANTHROPIC:
            return self.adapter
        adapter = self._claude_adapters.get(request.billing_channel)
        if adapter is None:
            raise ValueError('claude_adapter_required:' + request.billing_channel)
        return adapter

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
            result = await self.guard.call(req, dispatch=self._dispatch_for(req), attempt_id=attempt_id)
            value = dict(result.value)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            value = self._failure(attempt_id, req.model,
                                  error.reason if isinstance(error, BudgetBlocked) else type(error).__name__)
        return self._stamp(value, attempt_id, role)

    def _failure(self, attempt_id, requested_model, error):
        record = self.guard.lookup(attempt_id)
        # Only a persisted pre-dispatch block without a reservation proves zero: slot state blocked(dispatch_entered False)
        # 또는 슬롯조차 없는 일일 한도 차단(dispatch_entered None). 전송에 진입한 뒤 중단(True)은 구독 시도처럼 토큰이
        # 없어도 미전송이 아니다 — 복구 _verify_attempt 와 같은 규칙.
        not_sent = bool(record and record.get('state') == 'blocked' and record.get('reservation_token') is None
                        and record.get('dispatch_entered') is not True)
        return failed_attempt_value(record.get('result') if record else None, not_sent=not_sent,
                                    requested_model=requested_model, error=error,
                                    billing_channel=record.get('billing_channel') if record else None)

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

    async def _bear_role(self, pair_id, spec, prompt, system):
        """공통 Bear. 현행 Claude 프로필이면 CLI 단일 시도 뒤 허용된 전송 실패에서만 남은 내부 예산으로 API 단일 시도 1회.

        각 시도는 guard.call 1회(별도 attempt_id·영수증)이고 어댑터 안에 숨은 retry/fallback 은 없다. 가드 예외·역할 시한·
        취소·거절·신원 불일치·정책 위반은 대체하지 않는다. 시한은 늘리지 않는다 — API 에는 내부 예산에서 CLI 완료(정리 포함)
        까지의 경과를 뺀 나머지만 전달한다.
        """
        bear = spec.bear
        if spec.bear_cli_timeout_seconds is None:
            retries = spec.empty_retries if spec.bear_empty_retries is None else spec.bear_empty_retries
            return await self._role(pair_id, 'bear', bear, prompt, system, retries), False
        from .model_comparison_claude import DEADLINE_AT
        attempts, index = [], 0
        loop = asyncio.get_running_loop()
        token = None
        try:
            async with asyncio.timeout(bear.timeout_seconds):       # 역할(패널) 총 시한 — 소유자는 이 역할
                started = loop.time()                                # Bear 내부 예산의 시작점(절대 시한을 여기서 고정)
                internal_deadline = started + spec.bear_internal_timeout_seconds
                # 가드 입장·월 예약에 걸린 시간이 요청의 상대 시한에 더해지지 않도록 어댑터가 전송 직전 절대 상한을 적용한다.
                # CLI 단계 상한은 운영 `started + min(8, total/2)` 과 같이 체인 시작 기준 CLI 시한(내부 시한을 넘지 않음)이다.
                token = DEADLINE_AT.set(min(internal_deadline, started + spec.bear_cli_timeout_seconds))
                cli = replace(bear, timeout_seconds=spec.bear_cli_timeout_seconds)
                attempt = await self._attempt(pair_id, 'bear', 0, cli, prompt, system)
                attempts.append(attempt)
                remaining = internal_deadline - loop.time()
                if spec.bear_api_fallback is not None and _api_fallback_authorized(attempt) and remaining > 0:
                    index = 1
                    DEADLINE_AT.set(internal_deadline)                   # API 단계 상한은 Bear 내부 시한
                    api = replace(spec.bear_api_fallback,
                                  timeout_seconds=min(spec.bear_api_fallback.timeout_seconds, remaining))
                    attempts.append(await self._attempt(pair_id, 'bear', 1, api, prompt, system))
        except TimeoutError:
            model = (spec.bear_api_fallback if index else bear).model
            attempts.append(self._timed_out(pair_id, 'bear', index, model))
        finally:
            if token is not None:
                DEADLINE_AT.reset(token)
        # 가드는 응답 뒤 저장·정산을 동기로 처리하므로 asyncio.timeout 이 그 구간에 개입하지 못한다. 반환 뒤 monotonic 으로
        # 내부 시한을 재검사한다 — 늦은 결과는 영수증을 보존한 채 의견으로 세지 않는다(운영 whole-call deadline 과 같은 의미).
        late = loop.time() > internal_deadline if attempts and attempts[-1].get('error') != 'TimeoutError' else False
        return attempts, late

    def _attempt_reasons(self, attempts, certified=None):
        """certified: 전송된 시도의 신원 판정 함수. 레거시는 정확한 이름 일치, 현행은 _identity_certified(공식 snapshot 포함)."""
        if certified is None:
            def certified(attempt):
                return attempt.get('requested_model_match') is True
        reasons = []
        if any(not certified(a) for a in attempts if a.get('request_sent') is True):
            reasons.append('resolved_model_unverified_or_mismatch')
        if any(a.get('ledger_state') == 'pending_reconciliation' for a in attempts):
            reasons.append('budget_reconciliation_pending')
        if any(a.get('billing_channel') == 'subscription' for a in attempts):
            reasons.append('subscription_attempt_cost_not_api_billed')   # 구독 표시 추정비는 attempts 의 값으로만 남는다
        if _sum_cost(attempts) is None:
            reasons.append('api_cost_unknown')
        if not all(a.get('timing_valid') is True for a in attempts):
            reasons.append('attempt_timing_unverified')
        return reasons

    def _current_arm(self, spec, bull, bear, bull_late=False, bear_late=False):
        """현행 entry 독립 검토 정책(운영 AdversarialValidator·오프라인 계약 POLICY 와 같은 의미).

        policy_verdict 는 파싱된 의견만의 판정, verdict 는 신원 검증된 성공 응답·시한까지 고려한 판정이다. 한쪽이라도 없으면
        hold 이며 단일 모델 폴백은 없다. hold 는 진입 차단이라 sizing 재생을 하지 않는다.
        """
        def opinion(attempt, yes, no):
            content = attempt.get('content') if isinstance(attempt.get('content'), str) else ''
            return _parse_verdict(content, yes, no) if content.strip() else None
        pb, pr = opinion(bull[-1], 'APPROVE', 'REJECT'), opinion(bear[-1], 'ACCEPT', 'REJECT')

        def certified(attempt):   # 전송 성공 + 응답 모델 신원(정확 일치 또는 공식 snapshot). 미확인·불일치는 의견으로 세지 않는다.
            return attempt.get('success') is True and _identity_certified(attempt)
        b = pb if certified(bull[-1]) and not bull_late else None
        r = pr if certified(bear[-1]) and not bear_late else None
        timed_out = any(a.get('error') == 'TimeoutError' for a in (bull[-1], bear[-1]))
        policy_verdict = ('approve' if (pb or pr) else 'reject') if pb is not None and pr is not None else 'hold'
        verdict = ('approve' if (b or r) else 'reject') if b is not None and r is not None and not timed_out else 'hold'
        attempts = bull + bear
        reasons = []
        if verdict == 'hold':
            reasons.append('independent_review_unavailable')
            for role, attempt, parsed, late in (('bull', bull[-1], pb, bull_late), ('bear', bear[-1], pr, bear_late)):
                if attempt.get('success') is True and parsed is not None and not _identity_certified(attempt):
                    reasons.append(role + '_identity_unverified_or_mismatch')
                if late:
                    reasons.append(role + '_role_deadline_exceeded_after_response')
        if spec.bear_cli_timeout_seconds is not None:
            if len(bear) == 2:
                reasons.append('bear_api_fallback_dispatched')
            elif _api_fallback_authorized(bear[0]):
                reasons.append('bear_api_fallback_skipped_time_exhausted' if spec.bear_api_fallback is not None
                               else 'bear_api_fallback_not_configured')
            elif not bear[0]['success']:
                reasons.append('bear_api_fallback_not_authorized')
        replay = None
        if verdict == 'hold':
            reasons.append('independent_review_hold_no_entry')
        elif spec.neutral_sizing is None:
            reasons.append('neutral_sizing_missing')
        else:
            try:
                from .model_comparison_sizing import replay_policy_sizing
                replay = replay_policy_sizing(spec.neutral_sizing, verdict)
                reasons.extend(replay.get('exclusions', ['downstream_admission_not_replayed']))
            except (ValueError, TypeError, KeyError):
                reasons.append('neutral_sizing_invalid')
        reasons.extend(self._attempt_reasons(attempts, _identity_certified))
        return {'verdict': verdict, 'validation_verdict': verdict, 'policy_verdict': policy_verdict,
                'review_policy': spec.review_policy, 'approval_basis': 'entry_independent_review_v1',
                'unavailable_policy': 'hold_blocks_entry',
                'api_cost_scope': 'scenario_cost_including_shared_bear_do_not_sum_arms',
                'bull_verdict': b, 'bear_verdict': r,
                'independent_review_status': 'unavailable' if verdict == 'hold' else 'available',
                'decision_available_at': self.clock().isoformat(), 'attempts': attempts,
                'api_cost_usd': _sum_cost(attempts), 'sizing_replay': replay, 'reasons': reasons}

    async def _arm(self, pair_id, name, controls, spec, bear_task):
        loop = asyncio.get_running_loop()
        started = loop.time()
        bull = await self._role(pair_id, 'bull_' + name, controls, spec.common_context + BULL_SUFFIX, BULL_SYSTEM, spec.empty_retries)
        # Bull 도 가드의 동기 저장·정산 뒤 역할 시한을 재검사한다(현행 정책에서만 판정에 쓴다).
        bull_late = loop.time() - started > controls.timeout_seconds and bull[-1].get('error') != 'TimeoutError'
        bear, bear_late = await asyncio.shield(bear_task)
        if spec.review_policy == CURRENT_REVIEW_POLICY:
            return self._current_arm(spec, bull, bear, bull_late, bear_late)
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
        reasons.extend(self._attempt_reasons(attempts))
        return {'verdict': verdict, 'validation_verdict': verdict if validation_verdict is None else validation_verdict,
                'approval_basis': approval_basis, 'unavailable_policy': 'fail_open_size_preserved',
                'api_cost_scope': 'scenario_cost_including_shared_bear_do_not_sum_arms',
                'bull_verdict': b, 'bear_verdict': r,
                'decision_available_at': available, 'attempts': attempts,
                'api_cost_usd': _sum_cost(attempts), 'sizing_replay': replay, 'reasons': reasons}

    async def run(self, spec: PairSpec):
        if not isinstance(spec, PairSpec):
            raise ValueError('PairSpec_required')
        spec.validate()
        if _provider(spec.bear.model) == LLMProvider.ANTHROPIC:      # claim·슬롯 소비 전에 채널별 어댑터를 확인한다
            if spec.bear.billing_channel not in self._claude_adapters:
                raise ValueError('claude_adapter_required:' + spec.bear.billing_channel)
            if spec.bear_api_fallback is not None and 'api' not in self._claude_adapters:
                raise ValueError('claude_api_adapter_required')
        extra = (spec.bear_api_fallback,) if spec.bear_api_fallback is not None else ()
        for control in (spec.current, spec.candidate, spec.bear, *spec.fallback_chain, *extra):
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
            bear = asyncio.create_task(self._bear_role(pair_id, spec, spec.common_context + BEAR_SUFFIX, BEAR_SYSTEM))
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
                        'current_verdicts': {v: sum(r['current']['verdict'] == v for r in rows) for v in ('approve', 'reject', 'unavailable', 'hold')},
                        'candidate_verdicts': {v: sum(r['candidate']['verdict'] == v for r in rows) for v in ('approve', 'reject', 'unavailable', 'hold')}},
            'rows': rows}
