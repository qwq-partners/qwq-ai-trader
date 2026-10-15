"""명시된 모델의 보수적 USD 추정치 (2026-10-15 확인, 청구서 확정액 아님).

Sources: https://developers.openai.com/api/docs/pricing
         https://developers.openai.com/api/docs/guides/prompt-caching
         https://ai.google.dev/gemini-api/docs/pricing
입력 합계만 알 때 기본 암시적 캐시의 읽기/쓰기 비율을 추정하지 않는다.
아래 OpenAI 3개 모델은 입력 전체에 캐시 쓰기 상한(일반 입력의 1.25배)을
한 번 적용하며, 일반 입력 단가를 중복 가산하지 않는다. 캐시 읽기 할인이나
Batch/Flex/Priority 요율은 가정하지 않는다. Google 도입 할인은 제외한다.
미확인 ID에는 계열 단가를 추론하지 않는다. Flash Lite preview의 기존 계열
참조 추정치는 실제 청구 alias 인증이 아니다. 과거 정산 원장은 수정하지 않는다.
"""
from dataclasses import dataclass
from datetime import date
from typing import Optional


@dataclass(frozen=True)
class PriceQuote:
    cost_usd: float
    input_usd_per_million: float
    output_usd_per_million: float
    basis: str


_RATES = {
    'gpt-5-mini': (0.25, 2.0),
    'gpt-5.6-sol': (4.0, 20.0),
    'gpt-6.1-sol': (2.0, 10.0),
    'gpt-6-astra': (10.0, 50.0),
    'gemini-3.1-pro-preview': (2.0, 12.0),
    'gemini-3.1-flash-lite': (0.25, 1.5),
    'gemini-3.1-flash-lite-preview': (0.25, 1.5),
    'gemini-3.8-flash': (1.5, 7.5),
    'gemini-4-argon': (4.0, 20.0),
}


def rate_quote(model: str, input_tokens: int, output_tokens: int,
               *, on_date: Optional[date] = None) -> Optional[PriceQuote]:
    """보수적 추정치 또는 미확인 모델의 None을 반환한다.

    on_date는 호출 계약 호환용이다. 도입 할인 종료 후 단가를 모든 날짜에
    적용하므로 종료일 이후에도 임의의 계열 추정 없이 같은 상한을 쓴다.
    """
    for value in (input_tokens, output_tokens):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError('Token counts must be nonnegative integers')
    rates = _RATES.get(model)
    if rates is None:
        return None
    input_rate, output_rate = rates
    basis = 'standard_conservative_estimate_not_invoice_no_cache_read_discount'
    if model in {'gpt-5.6-sol', 'gpt-6.1-sol', 'gpt-6-astra'}:
        input_rate *= 1.25
        basis += '_cache_write_ceiling'
        if input_tokens > 272_000:
            input_rate *= 2
            output_rate *= 1.5
            basis += '_long_context'
    if model == 'gemini-3.1-pro-preview' and input_tokens > 200_000:
        input_rate, output_rate = 4.0, 18.0
        basis += '_long_context'
    if model == 'gemini-3.1-flash-lite-preview':
        basis += '_unverified_preview_alias_family_reference'
    if model in {'gemini-3.8-flash', 'gemini-4-argon'}:
        basis += '_post_promo_ceiling'
    return PriceQuote((input_tokens * input_rate + output_tokens * output_rate) / 1_000_000,
                      input_rate, output_rate, basis)
