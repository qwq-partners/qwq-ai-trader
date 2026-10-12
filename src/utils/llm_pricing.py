"""Explicit standard USD estimates, researched 2026-10-12 (not invoice quotes).

Sources: https://developers.openai.com/api/docs/pricing
         https://ai.google.dev/gemini-api/docs/pricing
Cached input conservatively costs full input price. No batch/flex/priority rates.
Unknown model IDs have no inferred family price. Preview Flash Lite is explicitly
an estimate from the published Flash Lite family, not a certified billing alias.
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
    'gemini-3.8-flash': (0.75, 3.75),
}


def rate_quote(model: str, input_tokens: int, output_tokens: int,
               *, on_date: Optional[date] = None) -> Optional[PriceQuote]:
    """Return a conservative estimate, or None when no valid price is known."""
    for value in (input_tokens, output_tokens):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError('Token counts must be nonnegative integers')
    rates = _RATES.get(model)
    if rates is None:
        return None
    if model == 'gemini-3.8-flash' and (on_date or date.today()) > date(2026, 12, 31):
        return None  # Introductory rate expires; require newly verified pricing.
    input_rate, output_rate = rates
    basis = 'standard_estimate_full_rate_cached_input'
    if model in {'gpt-5.6-sol', 'gpt-6.1-sol', 'gpt-6-astra'} and input_tokens > 272_000:
        input_rate *= 2
        output_rate *= 1.5
        basis += '_long_context'
    if model == 'gemini-3.1-pro-preview' and input_tokens > 200_000:
        input_rate, output_rate = 4.0, 18.0
        basis += '_long_context'
    if model == 'gemini-3.1-flash-lite-preview':
        basis += '_unverified_preview_alias_family_reference'
    if model == 'gemini-3.8-flash':
        basis += '_intro_through_2026_12_31'
    return PriceQuote((input_tokens * input_rate + output_tokens * output_rate) / 1_000_000,
                      input_rate, output_rate, basis)
