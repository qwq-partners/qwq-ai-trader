"""L02(2026-11-11) 리뷰 P1: Bull/토론/비교 입력의 경량 effort 는 설정된 light 모델이 지원하는 값으로 고른다.
설정에 gpt-5-mini 가 남아 있으면 minimal, gpt-5.6-terra 면 none — 전송 전 ValueError 로 독립 검토가 빠지지 않는다."""
import pytest

from src.analytics import model_comparison_input as mci
from src.core import adversarial_validator as av
from src.utils.llm import LLMConfig, LLMManager, LLMProvider, light_default_effort
from tests.test_llm_transport_contract import Session, openai_reply


@pytest.mark.parametrize('model,effort', [('gpt-5-mini', 'minimal'), ('gpt-5.6-terra', 'none'), ('gpt-6.1-sol', 'low'),
                                          ('unknown-model', None), (None, None)])
def test_light_default_effort_follows_the_model_table(model, effort):
    assert light_default_effort(model) == effort


class _NoGetter:
    pass


def test_bull_effort_falls_back_to_default_without_a_model_getter():
    assert av.bull_reasoning_effort(_NoGetter()) == av.BULL_REASONING_EFFORT_DEFAULT == 'none'


@pytest.mark.asyncio
@pytest.mark.parametrize('light,effort', [('gpt-5-mini', 'minimal'), ('gpt-5.6-terra', 'none')])
async def test_bull_and_debate_send_the_effort_their_configured_light_model_supports(light, effort):
    manager = LLMManager(LLMConfig(openai_api_key='synthetic', openai_model_light=light))
    manager.openai._session = Session(openai_reply('APPROVE'), openai_reply('APPROVE'))
    validator = av.AdversarialValidator(llm_manager=manager)
    response = await validator._ask_bull('context')
    assert response.success
    assert manager.openai._session.payloads[0]['model'] == light
    assert manager.openai._session.payloads[0]['reasoning_effort'] == effort
    controls = mci.operating_role_controls(manager, fallback_prompt_suffix='YES/NO', fallback_timeout=10.0)
    assert (controls['bull']['model'], controls['bull']['reasoning_effort']) == (light, effort)
    assert controls['fallback_chain'][1]['reasoning_effort'] == effort
