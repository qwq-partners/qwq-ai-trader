"""비교 worker 조립·생산자 현행 프로필 발행 — 순수 조립 코드. import·기본 생성·조립 어디서도 자격을 읽거나
네트워크에 연결하거나 Claude CLI 를 실행하지 않는다.

운영 활성화 경로가 아니다. `model_comparison_worker.install_from_env`/`_raw_clients` 의 무조건 거부는 그대로이며,
환경변수·기존 승인·과거 실행 슬롯은 새 실행 권한이 아니다. 이 모듈은 미래의 **명시 승인 단계**가 호출할 조립 코드와
그 합성 검증을 제공한다: 호출자가 raw 전송(`ComparisonTransports`)과 승인 기록(`ComparisonActivationApproval`)을
직접 만들어 넘기고, 조립은 동결 manifest 지문·실험 ID·날짜 창·전송 집합이 전부 맞을 때만 worker 를 만든다.

소유권·수명: worker 는 자기 스레드의 이벤트 루프에서 돌고 raw 전송은 aiohttp 세션을 캐시하므로, **worker 전용으로 새로 만든
전송(`owned=True`)만** 조립한다. 다른 루프에서 쓰이는 공유 전송(`owned=False`)은 조립을 거부한다(닫지도 쓰지도 않음). 조립
실패는 소유권 이전(worker 생성) 전에 끝나므로 호출자가 만든 전송을 건드리지 않는다. Gemini 는 생성하지 않으며 Gemini 가
섞인 manifest 는 조립하지 않는다. 승인 날짜 창은 manifest 의 execution_policy 창을 포함해야 하며, 그 뒤로는 가드가 매 시도의
입장에서 policy 창을 검사하므로 승인이 끝난 날에는 새 전송·예약이 생기지 않는다.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re

from . import model_comparison_input as mci
from .model_comparison_claude import ClaudeSingleAttemptAdapter
from .model_comparison_execution import ComparisonCallGuard, ComparisonExecutionPolicy, _kst_date
from .model_comparison_runner import (BEAR_CLI_TIMEOUT_CAP_SECONDS, CURRENT_REVIEW_POLICY, PairSpec, PairedComparisonRunner,
                                      RoleControls, SingleAttemptAdapter, _positive, _time)
from .model_comparison_worker import ComparisonWorker, attach
from ..utils.claude_transport import AnthropicAPIClient, ClaudeCLIClient
from ..utils.llm import OpenAIClient

# 계약(model_comparison_contract)의 전송 이름. 승인 기록과 조립에 필요한 전송 집합은 이 이름으로 대조한다.
TRANSPORT_OPENAI, TRANSPORT_CLI, TRANSPORT_API = 'openai_chat', 'claude_cli', 'anthropic_messages'
_TRANSPORTS = (TRANSPORT_OPENAI, TRANSPORT_CLI, TRANSPORT_API)
_REQUIRED_CONTROLS = ('bull', 'bear', 'empty_retries', 'strict_on_disagreement', 'bear_empty_retries',
                      'bear_internal_timeout_seconds', 'bear_transport_order', 'fallback_chain_timeout_seconds')
_CURRENT_BEAR_TRANSPORT_ORDER = [TRANSPORT_CLI, TRANSPORT_API]


@dataclass(frozen=True)
class ComparisonTransports:
    """호출자가 만든 raw 단일 시도 전송. LLMManager 나 임의 객체는 받지 않는다(숨은 retry/fallback 차단)."""
    openai: OpenAIClient
    claude_cli: ClaudeCLIClient | None = None
    anthropic_api: AnthropicAPIClient | None = None
    owned: bool = False     # True: worker 전용 — stop 때 닫는다. False: 다른 루프의 공유 전송 — 조립 대상이 아니다(거부)

    def __post_init__(self):
        if not isinstance(self.openai, OpenAIClient):
            raise ValueError('raw_openai_client_required; LLMManager forbidden')
        if self.claude_cli is not None and not isinstance(self.claude_cli, ClaudeCLIClient):
            raise ValueError('raw_claude_cli_client_required')
        if self.anthropic_api is not None and not isinstance(self.anthropic_api, AnthropicAPIClient):
            raise ValueError('raw_anthropic_api_client_required')
        if type(self.owned) is not bool:
            raise ValueError('owned_must_be_bool')

    def names(self) -> tuple[str, ...]:
        present = [TRANSPORT_OPENAI]
        if self.claude_cli is not None:
            present.append(TRANSPORT_CLI)
        if self.anthropic_api is not None:
            present.append(TRANSPORT_API)
        return tuple(present)


@dataclass(frozen=True)
class ComparisonActivationApproval:
    """명시 승인 기록. 파일·환경변수에서 자동으로 만들지 않으며 호출자가 동결 manifest 지문을 적어 넘긴다."""
    experiment_id: str
    manifest_sha256: str
    valid_from: str      # KST YYYY-MM-DD (포함)
    valid_until: str     # KST YYYY-MM-DD (포함)
    transports: tuple[str, ...]
    approved_by: str

    def __post_init__(self):
        for key in ('experiment_id', 'approved_by'):
            value = getattr(self, key)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f'{key}_required')
        if not isinstance(self.manifest_sha256, str) or re.fullmatch(r'[0-9a-f]{64}', self.manifest_sha256) is None:
            raise ValueError('manifest_sha256_must_be_hex64')
        _kst_date(self.valid_from, 'valid_from')
        _kst_date(self.valid_until, 'valid_until')
        if self.valid_from > self.valid_until:
            raise ValueError('approval_window_inverted')
        if (not isinstance(self.transports, tuple) or not self.transports
                or any(name not in _TRANSPORTS for name in self.transports) or len(set(self.transports)) != len(self.transports)):
            raise ValueError('approval_transports_invalid')


def manifest_sha256(manifest) -> str:
    """worker 의 `config_sha256` 과 같은 지문(정규 JSON 왕복 뒤 sha256)."""
    return hashlib.sha256(mci._canon(json.loads(mci._canon(manifest))).encode()).hexdigest()


def _control(controls, key):
    if key not in controls:
        raise ValueError('operating_control_missing:' + key)
    return controls[key]


def _role_dict(controls, key):
    role = _control(controls, key)
    if not isinstance(role, dict):
        raise ValueError('operating_control_missing:' + key)
    for field in ('model', 'max_output_tokens', 'reasoning_effort', 'timeout_seconds'):
        if field not in role or (field != 'reasoning_effort' and role[field] is None):
            # 원천에서 못 읽은 값(None)은 그대로 거부한다 — 정상값으로 치환하지 않는다.
            raise ValueError(f'operating_control_missing:{key}.{field}')
    return dict(role)


def current_profile_manifest(operating_controls, *, experiment_id, fixed_at, horizon_seconds, execution_policy,
                             candidate, max_input_age_seconds, enabled=False) -> dict:
    """생산자 제어(`operating_role_controls`)에서 현행 GPT/Claude 비교 프로필 manifest 를 동결한다.

    Bull/Bear 요청 제어·공통 재시도·strict·Bear 재시도/내부 시한/전송 순서를 그대로 옮기고, CLI 시한은 운영 규칙
    `min(8, 내부/2)`, API 대체는 같은 Bear 제어에 채널 api·시한 내부 예산으로 적는다. 요청 모델만 적으며 실제 응답 모델은
    기록하지 않는다. `candidate` 는 Bull 에서 바꿀 **한 변수**(model/reasoning_effort/max_output_tokens)다.
    기본 `enabled=False` — 켜진 manifest 는 호출자가 명시해야 한다. 결과는 worker 의 `validate_profile` 과 같은 규칙으로
    검사한 정규 JSON 사본이다.
    """
    if not isinstance(operating_controls, dict):
        raise ValueError('operating_controls_required')
    if type(enabled) is not bool:
        raise ValueError('enabled_must_be_bool')
    if not isinstance(candidate, dict) or not candidate:
        raise ValueError('candidate_change_required')
    # worker 의 입장 신선도(install_from_env 와 같은 범위)는 승인 지문에 들어가야 하므로 manifest 가 소유한다.
    if (isinstance(max_input_age_seconds, bool) or not isinstance(max_input_age_seconds, (int, float))
            or not 0 < float(max_input_age_seconds) <= 60):
        raise ValueError('max_input_age_seconds must be a number in (0, 60]')
    if not isinstance(execution_policy, dict) or 'pairs_per_day' not in execution_policy:
        raise ValueError('execution_policy.pairs_per_day_required')
    for key in _REQUIRED_CONTROLS:
        if _control(operating_controls, key) is None:
            raise ValueError('operating_control_missing:' + key)
    bull, bear = _role_dict(operating_controls, 'bull'), _role_dict(operating_controls, 'bear')
    if operating_controls['bear_transport_order'] != _CURRENT_BEAR_TRANSPORT_ORDER:
        raise ValueError('unsupported_bear_transport_order')
    internal = operating_controls['bear_internal_timeout_seconds']
    if type(internal) not in (int, float) or not internal > 0:
        raise ValueError('operating_control_missing:bear_internal_timeout_seconds')
    cli_timeout = min(BEAR_CLI_TIMEOUT_CAP_SECONDS, internal / 2)
    cand = dict(bull, **candidate)
    manifest = {
        'experiment_id': experiment_id, 'enabled': enabled, 'fixed_at': fixed_at, 'horizon_seconds': horizon_seconds,
        'strict_on_disagreement': operating_controls['strict_on_disagreement'], 'empty_retries': operating_controls['empty_retries'],
        'current': bull, 'candidate': cand, 'bear': dict(bear, billing_channel='subscription'),
        'fallback_chain': [], 'fallback_chain_timeout_seconds': operating_controls['fallback_chain_timeout_seconds'],
        'review_policy': CURRENT_REVIEW_POLICY, 'bear_empty_retries': operating_controls['bear_empty_retries'],
        'bear_internal_timeout_seconds': internal, 'bear_cli_timeout_seconds': cli_timeout,
        'bear_api_fallback': dict(bear, billing_channel='api', timeout_seconds=internal),
        'execution_policy': dict(execution_policy),
        # 일별 보고(scripts/report_model_comparison.py)가 읽는 최상위 한도. pairs_per_day 는 policy 와 같은 값이다.
        'max_input_age_seconds': float(max_input_age_seconds), 'pairs_per_day': execution_policy['pairs_per_day'],
    }
    prevalidate_manifest(manifest)      # 조립 전 검사와 같은 규칙(공통 값·역할 프로필)으로 발행 시점에 거부한다
    return json.loads(mci._canon(manifest))


def _contains_gemini(value) -> bool:
    if isinstance(value, str):
        return value.strip().lower().startswith('gemini')
    if isinstance(value, dict):
        return any(_contains_gemini(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_contains_gemini(item) for item in value)
    return False


_MANIFEST_SCALARS = ('experiment_id', 'fixed_at', 'horizon_seconds', 'strict_on_disagreement', 'empty_retries',
                     'fallback_chain_timeout_seconds', 'execution_policy')


def prevalidate_manifest(manifest) -> None:
    """부작용 없는 사전 검증 — worker 가 쓰는 키와 역할 프로필을 가드(원장에 정책을 저장) 생성 **전에** 확인한다.

    잘못된 manifest 가 공유 원장에 정책 행을 남겨 뒤의 수정본을 `comparison_policy_mismatch` 로 막는 일을 피한다.
    """
    if not isinstance(manifest, dict):
        raise ValueError('manifest_required')
    for key in _MANIFEST_SCALARS + ('current', 'candidate', 'bear'):
        if key not in manifest:
            raise ValueError('manifest_key_missing:' + key)
    # 공통 값은 PairSpec.validate 와 같은 규칙으로 미리 검사한다 — 잘못된 값이 정책 저장·worker 시작까지 가지 않게.
    if not isinstance(manifest['experiment_id'], str) or not manifest['experiment_id'].strip():
        raise ValueError('manifest_invalid:experiment_id')
    try:
        _time(manifest['fixed_at'])
    except ValueError:
        raise ValueError('manifest_invalid:fixed_at') from None
    horizon = manifest['horizon_seconds']
    if type(horizon) is not int or not 0 < horizon <= 86400:
        raise ValueError('manifest_invalid:horizon_seconds')
    if type(manifest['strict_on_disagreement']) is not bool:
        raise ValueError('manifest_invalid:strict_on_disagreement')
    retries = manifest['empty_retries']
    if type(retries) is not int or not 0 <= retries <= 1:
        raise ValueError('manifest_invalid:empty_retries')
    try:
        _positive(manifest['fallback_chain_timeout_seconds'], 'fallback_chain_timeout_seconds')
    except ValueError:
        raise ValueError('manifest_invalid:fallback_chain_timeout_seconds') from None
    if not isinstance(manifest['execution_policy'], dict):
        raise ValueError('manifest_invalid:execution_policy')
    roles = {k: RoleControls(**manifest[k]) for k in ('current', 'candidate', 'bear')}
    fallback = tuple(RoleControls(**c) for c in manifest.get('fallback_chain', []))
    api = manifest.get('bear_api_fallback')
    PairSpec.validate_profile(current=roles['current'], candidate=roles['candidate'], bear=roles['bear'], fallback_chain=fallback,
                              strict_on_disagreement=manifest['strict_on_disagreement'],
                              bear_empty_retries=manifest.get('bear_empty_retries'),
                              review_policy=manifest.get('review_policy', 'legacy-single-opinion-v1'),
                              bear_internal_timeout_seconds=manifest.get('bear_internal_timeout_seconds'),
                              bear_cli_timeout_seconds=manifest.get('bear_cli_timeout_seconds'),
                              bear_api_fallback=RoleControls(**api) if api is not None else None)


def required_transports(manifest) -> tuple[str, ...]:
    """현행 manifest 가 필요로 하는 전송 집합. 레거시(Gemini Bear) 프로필은 조립 대상이 아니다."""
    if not isinstance(manifest, dict) or manifest.get('review_policy') != CURRENT_REVIEW_POLICY:
        raise ValueError('provider_disabled: only the current GPT/Claude profile can be assembled')
    if _contains_gemini(manifest):
        raise ValueError('provider_disabled: Gemini comparison manifests cannot be assembled')
    names = [TRANSPORT_OPENAI, TRANSPORT_CLI]
    if manifest.get('bear_api_fallback') is not None:
        names.append(TRANSPORT_API)
    return tuple(names)


def build_comparison_runner(guard: ComparisonCallGuard, manifest, transports: ComparisonTransports) -> PairedComparisonRunner:
    """검증된 raw 단일 시도 전송만으로 현행 흐름의 runner 를 조립한다(Gemini 인스턴스 없음, 예약·정산은 가드 소유)."""
    if not isinstance(transports, ComparisonTransports):
        raise ValueError('ComparisonTransports_required')
    required = required_transports(manifest)
    for name in required:
        if name not in transports.names():
            raise ValueError('transport_missing:' + name)
    adapter = SingleAttemptAdapter(openai=transports.openai, gemini=None)
    api = ClaudeSingleAttemptAdapter(transports.anthropic_api) if TRANSPORT_API in required else None
    return PairedComparisonRunner(guard=guard, adapter=adapter,
                                  claude_adapter=ClaudeSingleAttemptAdapter(transports.claude_cli), claude_api_adapter=api)


def assemble_comparison_worker(cross_validator, *, manifest, ledger, transports: ComparisonTransports,
                               approval: ComparisonActivationApproval, today=None, **worker_kwargs) -> ComparisonWorker:
    """명시 승인 아래 worker 를 조립·시작·attach 한다. 모든 대조는 worker/어댑터 생성 전에 끝난다(fail-closed).

    승인 기록의 실험 ID·동결 manifest 지문·전송 집합이 manifest 와 정확히 맞고, 승인 날짜 창이 manifest 의
    execution_policy 창을 포함하며, 전송 묶음이 worker 전용(owned=True)이고 필요한 집합과 같아야 한다. 입장 신선도
    `max_input_age_seconds` 는 manifest 가 소유한다(인자로 덮어쓸 수 없음). `today` 는 KST date(기본 원장의 오늘).
    운영 스케줄·승인 파일은 만들지 않는다.
    """
    if not isinstance(approval, ComparisonActivationApproval) or not isinstance(transports, ComparisonTransports):
        raise ValueError('explicit_approval_and_transports_required')
    if transports.owned is not True:
        # worker 는 자기 루프에서 돈다. 다른 루프에 묶인 aiohttp 세션을 가진 공유 전송은 쓰지도 닫지도 않는다.
        raise ValueError('shared_transports_unsupported: pass worker-dedicated transports (owned=True)')
    if 'max_input_age_seconds' in worker_kwargs:
        raise ValueError('max_input_age_from_manifest_only')
    if not isinstance(manifest, dict) or manifest.get('enabled') is not True:
        raise ValueError('manifest_disabled')
    if approval.experiment_id != manifest.get('experiment_id'):
        raise ValueError('approval_experiment_mismatch')
    if approval.manifest_sha256 != manifest_sha256(manifest):
        raise ValueError('approval_manifest_mismatch')
    day = (today if today is not None else ledger.today()).isoformat()
    if not approval.valid_from <= day <= approval.valid_until:
        raise ValueError('approval_expired')
    required = required_transports(manifest)
    if set(approval.transports) != set(required):
        raise ValueError('approval_transports_mismatch')
    for name in required:
        if name not in transports.names():
            raise ValueError('transport_missing:' + name)
    if set(transports.names()) != set(required):
        raise ValueError('transport_unexpected')
    prevalidate_manifest(manifest)                      # 가드가 원장에 정책을 저장하기 전에 역할 프로필을 거절한다
    policy = dict(manifest['execution_policy'], experiment_id=manifest['experiment_id'])
    policy['allowed_models'] = tuple(policy['allowed_models'])
    execution_policy = ComparisonExecutionPolicy(**policy)
    if not (approval.valid_from <= execution_policy.valid_from and execution_policy.valid_until <= approval.valid_until):
        # 가드는 매 시도 입장에서 policy 창을 검사한다. 승인 창이 policy 창을 포함해야 승인 만료 뒤 새 전송이 없다.
        raise ValueError('approval_window_narrower_than_policy')
    age = manifest.get('max_input_age_seconds')
    if isinstance(age, bool) or not isinstance(age, (int, float)) or not 0 < float(age) <= 60:
        raise ValueError('manifest.max_input_age_seconds must be a number in (0, 60]')
    if manifest.get('pairs_per_day') != execution_policy.pairs_per_day:
        raise ValueError('manifest_pairs_per_day_mismatch')
    guard = ComparisonCallGuard(ledger=ledger, policy=execution_policy)
    runner = build_comparison_runner(guard, manifest, transports)
    worker = ComparisonWorker(runner, manifest=manifest, max_input_age_seconds=float(age), **worker_kwargs)
    worker._owned_transports = transports.owned          # 여기서부터 소유권 이전(owned=True 일 때만)
    mci.source_sha256()
    try:
        attach(worker.start(), cross_validator)
    except BaseException:
        worker.stop(5)
        raise
    return worker
