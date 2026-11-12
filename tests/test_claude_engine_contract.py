import pytest

from src.utils.llm import LLMConfig, LLMProvider, LLMResponse, _validate_controls
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


FABLE = 'claude-fable-5-1'   # 설정 슬롯이 받는 Fable 은 5.1 뿐(5.2 는 고정 보고 역할 전용, V07)


def test_claude_config_accepts_only_opus_and_fable_identities():
    # 설정 슬롯 허용 집합 {opus-5-5, fable-5-1}. 5.2 는 고정 보고 역할 전용이라 슬롯에서는 거부(V07). 다른 세대·Sonnet·별칭도 거부.
    assert LLMConfig(claude_model=FABLE).claude_model == FABLE
    for slot in ('claude_model', 'claude_model_heavy', 'claude_model_light'):
        with pytest.raises(ValueError, match='Claude model identity'):
            LLMConfig(**{slot: 'claude-fable-5-2'})
    for model in ('claude-fable-5-2', 'claude-fable-5', 'claude-mythos-5-2', 'claude-mythos-5-1', 'claude-sonnet-5-5', 'opus', [], None):
        with pytest.raises(ValueError, match='Claude model identity'):
            LLMConfig(claude_model=model)


@pytest.mark.parametrize('model', ['claude-opus-5-5', FABLE])
def test_anthropic_role_controls_allow_low_medium_effort_only(model):
    for effort in ('low', 'medium'):
        _validate_controls(model, LLMProvider.ANTHROPIC, {'reasoning_effort': effort})
    _validate_controls(model, LLMProvider.ANTHROPIC, {})
    for bad in ({'reasoning_effort': 'high'}, {'reasoning_effort': 'minimal'}, {'thinking_level': 'low'}):
        with pytest.raises(ValueError):
            _validate_controls(model, LLMProvider.ANTHROPIC, bad)
