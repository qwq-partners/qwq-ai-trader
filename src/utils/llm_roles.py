"""명시한 보고·연구 호출만 사용하는 고정 역할표. 기존 매매 task 경로는 별도다."""
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType


ROLE_POLICY_VERSION = 'engine-roles-v2-openai'


class LLMRole(str, Enum):
    REPORT_ONE_LINE = 'report_one_line'
    MORNING_BRIEF = 'morning_brief'
    POST_EXIT_RESEARCH = 'post_exit_research'


@dataclass(frozen=True)
class RoleProfile:
    provider: str
    model: str
    max_output_tokens: int
    timeout_seconds: float
    max_input_bytes: int
    budget_priority: str
    reasoning_effort: str | None = None
    thinking_level: str | None = None

    def controls(self) -> dict:
        result = {'max_tokens': self.max_output_tokens}
        if self.reasoning_effort is not None:
            result['reasoning_effort'] = self.reasoning_effort
        if self.thinking_level is not None:
            result['thinking_level'] = self.thinking_level
        return result


# 요약은 평상시부터 저가 모델을 쓴다. 80% 절약 모드에서도 모델·제어를 바꾸지 않는다.
# 95% 필수 전용/100% 중단은 기존 공통 원장의 입장 검사로 적용한다.
ROLE_PROFILES = MappingProxyType({
    LLMRole.REPORT_ONE_LINE: RoleProfile(
        'openai', 'gpt-5-mini', 512, 20.0, 32 * 1024, 'standard',
        reasoning_effort='minimal'),
    LLMRole.MORNING_BRIEF: RoleProfile(
        'openai', 'gpt-5-mini', 1600, 45.0, 64 * 1024, 'standard',
        reasoning_effort='minimal'),
    LLMRole.POST_EXIT_RESEARCH: RoleProfile(
        'openai', 'gpt-6.1-sol', 4096, 90.0, 128 * 1024, 'research',
        reasoning_effort='medium'),
})


def get_role_profile(role: LLMRole | str) -> tuple[LLMRole, RoleProfile]:
    try:
        key = LLMRole(role)
    except (ValueError, TypeError):
        raise ValueError('Unknown LLM role') from None
    return key, ROLE_PROFILES[key]
