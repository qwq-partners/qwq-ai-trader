from src.utils.llm import LLMConfig, LLMProvider, LLMResponse
from src.utils.llm_pricing import rate_quote


def test_anthropic_is_a_distinct_provider():
    assert 'anthropic' in {p.value for p in LLMProvider}


def test_claude_config_keeps_api_secret_out_of_repr():
    assert 'anthropic_api_key' in LLMConfig.__dataclass_fields__
    config = LLMConfig(anthropic_api_key='synthetic-private')
    assert 'synthetic-private' not in repr(config)
    assert config.claude_model == 'claude-opus-5-5'
    assert config.claude_cli_enabled is True


def test_subscription_cost_is_not_implicit_zero_or_api_bill():
    assert 'billing_channel' in LLMResponse.__dataclass_fields__
    result = LLMResponse('ACCEPT', 'claude-opus-5-5', LLMProvider.ANTHROPIC,
                         transport='claude_cli', billing_channel='subscription')
    assert result.actual_incremental_cost_usd is None
    assert result.subscription_estimated_cost_usd is None


def test_claude_api_quote_covers_cache_write_ceiling():
    quote = rate_quote('claude-opus-5-5', 1000, 400)
    assert quote is not None
    assert quote.input_usd_per_million == 8.0
    assert quote.output_usd_per_million == 20.0
    assert quote.cost_usd == 0.016
    assert 'cache_write' in quote.basis
    assert rate_quote('claude-opus-unknown', 1000, 400) is None
