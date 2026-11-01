"""Claude 단일 시도 어댑터 — raw 전송 하나를 ComparisonCallGuard 의 dispatch 로 노출한다.

구독 CLI(`ClaudeCLIClient`, billing_channel=subscription)와 Anthropic API(`AnthropicAPIClient`,
billing_channel=api)를 명시적으로 구분하며, 어댑터 하나는 그 전송 한 번만 수행한다. 예산 예약·정산·
재시도·CLI→API 대체·자격 수명·클라이언트 종료는 소유하지 않는다(가드와 호출자 소유). LLMManager 를
감싸지 않으므로 숨은 재시도·대체·이중 월 예약이 생기지 않는다. CLI→API 대체를 조정하는 상위 흐름과
worker 연결은 이 모듈 밖이다. import·생성만으로는 자격을 읽거나 외부에 연결하지 않는다.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from decimal import Decimal

from .model_comparison_execution import ComparisonAttemptResult, ComparisonRequest
from ..utils.claude_transport import AnthropicAPIClient, ClaudeCLIClient
from ..utils.llm import LLMProvider, LLMResponse
from ..utils.llm_pricing import rate_quote


def _now():
    return datetime.now(timezone.utc)


class ClaudeSingleAttemptAdapter:
    """전송 클래스가 채널을 선언한다. 요청 채널이 다르면 공급자 호출 전에 거절한다."""

    def __init__(self, client, clock=_now):
        if not isinstance(client, (ClaudeCLIClient, AnthropicAPIClient)):
            raise ValueError('raw_claude_transport_required; LLMManager forbidden')
        self.client, self.clock = client, clock
        self.transport, self.billing_channel = client.transport, client.billing_channel

    async def __call__(self, request: ComparisonRequest) -> ComparisonAttemptResult:
        if not isinstance(request, ComparisonRequest):
            raise ValueError('ComparisonRequest_required')
        if request.billing_channel != self.billing_channel:
            # 구독 요청을 API 전송으로 보내면 예약 없는 과금, 유료 요청을 CLI 로 보내면 예약만 남는 미전송이다.
            raise ValueError('claude_adapter_billing_channel_mismatch')
        if not request.model.startswith('claude-'):
            raise ValueError('claude_adapter_requires_claude_model')
        if request.temperature is not None or request.seed is not None or request.reasoning_effort != 'low':
            # Claude 전송은 temperature/seed 를 지원하지 않고 엔진 역할은 low effort 다. 임의 변환 없이 거절한다.
            raise ValueError('unsupported_claude_request_controls')
        loop = asyncio.get_running_loop()
        start = self.clock()
        # 호출자 시한만 전달한다. 전송은 자기 자식 프로세스/요청만 정리하며 시한·토큰 상한을 늘리지 않는다.
        response = await self.client.complete(
            request.prompt, system=request.system, model=request.model,
            max_tokens=request.max_output_tokens, reasoning_effort=request.reasoning_effort,
            deadline=loop.time() + request.timeout_seconds)
        end = self.clock()
        if not isinstance(response, LLMResponse):
            raise ValueError('invalid_provider_response')
        subscription = self.billing_channel == 'subscription'
        tokens_valid = all(type(v) is int and v >= 0 for v in (response.input_tokens, response.output_tokens))
        usage_known = response.usage_known is True and tokens_valid
        identity_verified = (response.model_identity_status == 'verified'
                             and response.resolved_model == request.model
                             and response.requested_model == request.model
                             and response.provider == LLMProvider.ANTHROPIC)
        cost, cost_basis, settlement = None, 'unknown', 'measured'
        if subscription:
            # 월 API 원장 정산 없음. CLI 표시 추정비는 value 에만 남기고 API 청구액이나 확정 0원으로 바꾸지 않는다.
            cost_basis, settlement = 'subscription_not_api_billed', 'subscription'
        elif response.request_sent is False and response.billing_status == 'not_sent':
            cost, cost_basis = Decimal('0'), 'transport_not_sent'
        elif (response.billing_status == 'rejected_before_generation' and response.request_sent is True
              and response.usage_known is not True and response.success is not True):
            # SingleAttemptAdapter 와 같은 생성 전 거절 추론 — 예약 해제·과금 0, unknown 으로 공유 원장을 막지 않는다.
            cost, cost_basis, settlement = Decimal('0'), 'rejected_before_generation_inferred', 'rejected_before_generation'
        elif identity_verified and usage_known:
            quote = rate_quote(request.model, response.input_tokens, response.output_tokens, on_date=start.date())
            if quote is not None:
                cost = (Decimal(str(quote.input_usd_per_million)) * response.input_tokens
                        + Decimal(str(quote.output_usd_per_million)) * response.output_tokens) / 1000000
                cost_basis = quote.basis
        valid_timing = end >= start
        value = {'requested_model': request.model, 'resolved_model': response.resolved_model,
                 'model_identity': 'provider_reported_unverified' if response.resolved_model else 'unverified',
                 'model_identity_status': response.model_identity_status,
                 'requested_model_match': response.resolved_model == request.model,
                 'provider': LLMProvider.ANTHROPIC.value, 'transport': self.transport,
                 'billing_channel': self.billing_channel,
                 # 요청 effort 와 확인된 실효 effort 는 다르다. 전송은 실효 effort 를 노출하지 않는다.
                 'reasoning_effort': request.reasoning_effort, 'requested_reasoning_effort': request.reasoning_effort,
                 'effective_reasoning_effort': None,
                 'seed': None, 'max_output_tokens': request.max_output_tokens,
                 'requested_at': start.isoformat(), 'responded_at': end.isoformat(),
                 'timing_valid': valid_timing, 'latency_ms': (end - start).total_seconds() * 1000 if valid_timing else None,
                 'content': response.content if isinstance(response.content, str) else '',
                 'success': response.success is True and identity_verified,
                 # 'error' 키는 러너가 가드 예외(차단·중단·시한)에만 붙인다. 전송 실패 사유는 transport_error_kind 다.
                 'transport_error_kind': response.transport_error_kind,
                 'refusal_detected': response.refusal_detected is True,
                 'request_sent': response.request_sent, 'billing_status': response.billing_status,
                 'http_status': response.http_status, 'provider_error_type': response.provider_error_type,
                 'usage_known': usage_known,
                 'input_tokens': response.input_tokens if usage_known else None,
                 'output_tokens': response.output_tokens if usage_known else None,
                 'cost_basis': cost_basis, 'api_cost_usd': str(cost) if cost is not None else None,
                 'subscription_estimated_cost_usd': response.subscription_estimated_cost_usd if subscription else None,
                 'finish_reason': response.finish_reason}
        return ComparisonAttemptResult(value, cost, settlement)
