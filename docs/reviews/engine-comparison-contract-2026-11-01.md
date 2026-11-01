# E04 — 현행 GPT/Claude 비교 계약 점검

기준 main `1eb023a940b52820261f129402b24f3789fceb02`, 2026-11-01 KST. 이 점검은 기존 실행 원장의 E04 “현재 Claude 계약용 manifest/runner 지원부터 확인”을 수행한다. 실제 비교 실행·유료 호출·모델 변경·운영 활성화는 포함하지 않는다.

후속 구현의 사용법은 [현행 오프라인 계약 v1](#offline-contract-v1), 검증/푸시 상태는 [실행 원장](../operations/execution-roadmap.md#e04-current-contract-20261101)을 따른다. 아래 초기 감사의 미래형 표현은 착수 당시 기록이다.

## 결론

현재 운영의 독립 검토 정책을 옛 비교기로 재생할 수 없다. Claude 모델명만 허용해 해결할 문제가 아니라, 요청 제어·역할별 재시도·전체 판단 시한·검토 누락 처리·전송별 비용 계약이 다르다. 현재 비교 활성화 차단은 유지해야 한다. 이는 실행 중인 거래 엔진의 같은 결함을 확인했다는 뜻이 아니다.

## 코드 대조와 재현

| 항목 | 현행 계약 | 기존 비교 도구의 차이 |
|---|---|---|
| 공급자 | Bull GPT, Bear Anthropic | `model_comparison_runner._provider`는 GPT/Gemini만 인식하고 `PairSpec.validate`는 Gemini Bear를 요구한다. |
| Claude 제어 | entry Bear low/400, temperature·seed 없음 | `ComparisonRequest`는 temperature=None을 거부한다. |
| 재시도 | Bull 기존 계약, Bear empty retry0 | runner는 공통 `empty_retries`를 Bear에도 적용한다. |
| 시한 | entry panel12초, Bear 내부11.5초 | producer가 보존하는 Bear 내부 시한을 worker의 예상 제어 대조가 검증하지 않는다. 팀 Bear의19.5초/API800과 구별한다. |
| 독립 검토 | 한쪽 검토 누락 시 보류 | runner의 `_arm`에는 한쪽만으로 승인하거나 단일 fallback으로 승인하는 옛 규칙이 남아 있다. |
| 전송·정산 | Claude CLI 우선, API 대체; 구독 표시 추정비와 API 비용 구분 | adapter는 raw OpenAI/Gemini, 실행 정책은 `single-provider-attempt-v1`이었다(11/1 [guard 소유권](#guard-billing-channel) 뒤 v2). 다중 전송을 `LLMManager` 하나로 감싸면 기존 예약/시도 의미가 달라진다. |
| 입력 검증 | producer가 Bear retry0·내부 시한·전송 순서 보존 | worker의 `_expected_controls`가 이 세 필드를 검증하지 않는다. **추가 키 때문에 봉투를 거부하는 것이 아니라 추가 키를 검증하지 않는 것**이다. |

직접 대조한 소스:

- `src/analytics/model_comparison_input.py`: `operating_role_controls`
- `src/analytics/model_comparison_runner.py`: `_provider`, `RoleControls`, `PairSpec.validate`, `SingleAttemptAdapter`, `_arm`, `run`
- `src/analytics/model_comparison_execution.py`: `ComparisonRequest`, `ComparisonExecutionPolicy.serialized`
- `src/analytics/model_comparison_worker.py`: `_expected_controls`, `_raw_clients`, `install_from_env`
- `src/core/adversarial_validator.py`: `_ask_bear`, 독립 검토 누락 처리
- `tests/test_claude_review_consumers.py`: 현행 제어·검토 누락 계약

원본 AST에서 순수 함수/생성자만 추출한 합성 재현은 Claude 모델에 `unsupported_provider_model`, 현행 temperature=None에 입력 거절을 확인했다. GPT 대조 입력은 수락됐다. 앱 import·클라이언트 생성·자격·DB·네트워크·LLM 호출0이다. 최초 보조 실행의 AST 의존성 위치/annotation 준비 오류는 실제 재현 결과와 구분한다. 나머지 표 항목은 정적 소스 대조이며 전체 러너를 실행한 결과가 아니다.

`_raw_clients()`는 활성화를 거부하고 옛 Gemini manifest도 거부한다. 과거 reader/영수증은 역사 분석용으로 보존하며 이 점검으로 R1을 켜지 않는다.

## 새 자연 기록에서 확인한 범위

10/30 12:51:04.925887 이상~11/1 10:31:18.910364 미만 KST의 기존 판단 원장에서 새 Bull24/Bear24행을 확인했다. 각 역할은 고유 call_id24개이며 모두 success·비어 있지 않은 본문·bool판정이다. 실제 기록 모델은 GPT `gpt-5-mini`, Claude `claude-opus-5-5`다. 보고서 전용 역할의 모델과 혼합하지 않는다. 실제 새 행의 마지막 시각은10/30 14:02:08 KST이며10/30 당시 운영에서 생성됐다. 11/1 cfc9bc20 배포 뒤 자연 호출 성능으로 승격하지 않는다.

- Claude는 CLI24시도 중7완료,17시간초과 뒤 API17시도 모두완료다. 실패한 CLI를 별도 독립 판단24건에 더하지 않는다.
- 최종 응답 지연 중앙값은 Bull2,209.1ms/Bear6,209.3ms다. Bear의 기록된 시도 지연 합 중앙값은13,323.4ms다. 전자는 선행 CLI 대기를 포함하지 않으며 후자도 호출 사이 처리·정리 대기를 포함한 전체 벽시계 시간이 아니다.
- 기록된 사용량 기반 API 추정비는 GPT24시도 USD0.007964, Claude17시도 USD0.270292다. 구독 CLI 표시 추정비 USD0.148652는8시도에서만 확인됐으며16시도 사용량·24시도 구독 증분 청구액은 미확정이다. 표시 추정비를 확정 청구액에 합산하지 않는다.
- 새48프롬프트에서 종목 노트/리서치 표식0이다. 원문은 반출하지 않았으며 이 표식만으로 모든 위키 사용 부재를 입증하지 않는다.

이 결과는 경로의 완료율·지연·비용 근거다. 같은 입력의 대안 모델 결과·응답 이후 매매 가능한 가격·실제 거래 순수익 비교는 아니다. 이전 CLI 시한 질문을 다시 열거나 시간·토큰 상한을 늘릴 근거로 삼지 않는다.

## 다음 보완의 경계

먼저 현행 독립 검토를 표현하는 **버전 구분된 오프라인 계약**을 정한다. Claude 제어·역할별 retry·전체 판단 시한·검토 누락 보류·요청/실제 모델 식별·CLI/API 비용 구분을 합성 입력으로 검사하는 범위다. 과거 reader를 현재 의미로 재해석하지 않는다.

실제 전송 adapter·입장/정산·일 시도 상한·동일 입력 및 응답 이후 가격의 결합은 별도 검증 대상이다. 이 단계에서 실제 worker 연결, 기존 월 원장 생성/초기화, R1 활성화, Gemini 재활성화, 관측 기간 연장, 매수 재개는 하지 않는다. 오프라인 계약의 적합성만으로 경제 비교가 가능해졌다고 판정하지 않는다.

주 순서는 E02 선정 근거 → E01 진입 가격 → E03 청산 → E05 통합이며 E04/A01은 병행한다. 새 유효 RR/진입 입력이 생기면 그 실제 자료 판독이 비교 도구 보완보다 우선한다.

<a id="offline-contract-v1"></a>
## 현행 오프라인 계약 v1

사용자 후속 승인으로 `model-comparison-offline-contract-v1` 입력과 `model-comparison-offline-report-v1` 출력을 추가한다. 구형 pair-v1/v2 독자·worker는 변경하지 않는다. [실행/검증 상태 정본](../operations/execution-roadmap.md#e04-current-contract-20261101).

저장소 루트에서 다음 합성 예시를 실행할 수 있다. 예시의 모델 응답·시간·비용은 모두 검사값이며 실제 호출 결과가 아니다.

```bash
python scripts/check_model_comparison_contract.py \
  --input docs/examples/model-comparison-offline-synthetic.json
```

CLI는 명시한 일반 JSON 파일만 최대1MiB 읽고 결과를 표준 출력으로 보낸다. 기존 DB/로그·환경·자격·외부 API를 자동 탐색하거나 결과를 저장하지 않는다. 중복 JSON 키·비유한 수치·알 수 없는 필드/버전은 거부한다. exit0은 결과 생성이며 `contract_status`와 각 arm의 `verdict`를 함께 읽어야 한다. 정상 형식의 보류 결과도 exit0이고 잘못된 입력은 exit2다.

입력은 `profile=entry-independent-review-v1`, 합성/호출자 제공 자료 구분, 공통 입력 지문, 고정 정책과 `bull_current`/`bull_candidate`/공유 `bear`를 갖는다. 후보 Bull은 모델·effort·출력 상한 중 한 항목만 바꾸며 이것이 모델 지원/호출 승인이라는 뜻은 아니다. 각 역할의 요청 제어·전체 경과시간·결과·본문·전송 시도를 받는다. 예시 파일에 전체 필드를 명시했다. 전체 경과시간이 없으면 최종 응답 지연이나 시도 합으로 채우지 않는다. Team Bear의19.5초/API800 계약은 지원하지 않는다.

보고의 `policy_verdict`는 두 파싱 결과에 현재 불일치 허용 정책을 적용한 값이다. 기본 `verdict`는 신원·전송 순서·시한 근거까지 확인한 보수적 오프라인 판정이므로 결측 시hold다. 이를 과거 엔진이 실제로 같은 hold를 했다는 증거로 해석하지 않는다. 원문 응답은 출력하지 않고 요청 effort와 확인된 실효 effort를 구분한다.

비용은 공유 Bear를 한 번만 계수한 관측 입력 범위다. 알려진 API 추정 부분합과 미상 건수·완전 합계를 구분하고, 구독 표시 추정비는 API에 더하지 않는다. 구독 추가 청구액·전체 실제 청구액·순수익은null이다. `actual_execution=false`, `economic_evaluable=false`, `live_activation_supported=false`를 항상 보존한다. 입력 지문의 진위·완전성, 입장/정산·예산 정책, 응답 후 실행 가능한 가격은 이번 검증 대상이 아니다.

시도 기록이 없는 success/error/timeout/cancelled는 별도 `missing_trace_count`로 남기고 API 합계를null로 유지한다. 미전송 표기와 응답/토큰/신원 증거가 모순될 때도 누락 비용을0으로 확정하지 않는다. 정상 timeout/cancelled는 보류하되 위반과 구별한다. 관측 모델/effort의 비정형 값은 원문 비교 후 출력에서 치환하여 다른 시도의 비용을 보존한다.


<a id="offline-contract-v2"></a>
## 공식 snapshot 신원 판독 v2

v1은 요청/응답 모델명의 완전 일치를 계속 요구한다. v2를 명시적으로 선택하려면 입력의 `schema_version`을 `model-comparison-offline-contract-v2`로 설정한다. 기존 파일 CLI와 나머지 필드는 같으며 profile `entry-independent-review-v1`의 판단·제어·시한·비용 규칙도 유지한다. 출력 버전은 `model-comparison-offline-report-v2`다. v1 입력을 자동 승격하지 않는다.

2026-11-01 확인한 [OpenAI 공식 모델 문서](https://developers.openai.com/api/docs/models/gpt-5-mini)가 `gpt-5-mini`의 snapshot으로 `gpt-5-mini-2025-08-07`을 명시한다. v2의 고정 `identity_policy_version=documented-model-identity-v1`은 `openai_chat`에서 이 요청 alias→응답 snapshot 조합만 추가로 인정한다. 접두사/날짜 패턴이나 입력 whitelist는 사용하지 않는다. 후보 모델명만 같은 공식 snapshot으로 바꾸는 입력은 v2에서 `one_comparison_variable_required`로 거절한다(v1 의미는 유지). 새 문서 관계는 별도 검토·버전 정책을 거쳐야 하며 실행 중 네트워크 갱신은 없다.

각 시도 관측의 `identity_match`는 `exact`, `documented_snapshot`, `unknown`, `mismatch` 중 하나다. 요청과 응답 이름은 별도로 보존한다(비정형 관측은 기존 정제 규칙 유지). 마지막 응답뿐 아니라 앞선 시도에도 같은 판정을 적용하며 원래 refusal/identity_mismatch·시간/전송 위반을 정상으로 복구하지 않는다. 이 분류는 제공된 문자열 관계이며 입력 진위·실효 effort·실시간 alias 포인터·모델 가용성을 인증하지 않는다. v2 `limitations`에도 `documented_identity_not_current_attestation`을 명시한다.

예를 들어 합성 fixture의 GPT 응답 이름만 공식 snapshot으로 바꾸면 v1은 신원 불일치로hold, v2는 다른 조건이 모두 정상이면conformant다. 임의 날짜/다른 모델·반대 방향의 관계는 계속hold다. 실제 앱 모델을 바꾸거나 API를 호출하는 기능이 아니며 `actual_execution=false`, `economic_evaluable=false`, `live_activation_supported=false`, 순수익null은 유지한다. [검증·운영/NEXT](../operations/execution-roadmap.md#e04-snapshot-identity-20261101).


<a id="guard-billing-channel"></a>
## 호출 경계의 전송 채널 소유권

PR #323 병합 뒤 Claude CLI→API 대체의 시도 단위 입장·월 예약·정산 소유권을 대조했다. 운영 `LLMManager._run_complete_attempt`는 구독 CLI 시도에 월 예약을 만들지 않고 API 대체 1회만 예약·정산한다. 비교 실행기의 `ComparisonCallGuard`는 모든 입장 시도를 월 원장에 예약·정산하므로 비과금 구독 시도를 표현할 수 없었다. 구독 시도를 비용 None으로 넘기면 공유 원장 `unknown`이 운영 유료 호출까지 막고, CLI 사용량을 API 요율로 계산하면 가짜 USD가 정산된다. `SingleAttemptAdapter`의 raw 클라이언트 형식 검사는 LLMManager 감싸기(API 이중 예약)를 막지만 이 공백을 채우지 않았다.

`ComparisonRequest.billing_channel`(api|subscription)을 추가했다. 구독 요청은 일 슬롯·요청 지문을 유료와 같이 소비하고 정책 단계 normal·실험 유효기간만 읽는다. 월 `reserve`·80%·실험 상한은 대상이 아니다. 응답 정산 `subscription`은 `actual_cost_usd=None`만 허용하며 구독 표시 추정비는 value의 `subscription_estimated_cost_usd`로만 남겨 API 비용에 더하지 않는다. 요청 채널과 응답 정산이 다르면 `comparison_billing_channel_mismatch`로 거절하고 API 예약은 `unknown`, 구독 시도는 차단 ID만 남는다. `day_attempts`는 `billing_channel`과 구독 추정비를 따로 노출한다. 정책 `adapter_contract`는 `single-provider-attempt-v2`다.

독립 리뷰 반영으로 구독 요청은 어댑터의 명시 `billing_channel="subscription"` 선언 없이는 전송 전에 거절되고(유료 요청의 미선언 콜백은 기존 계약), 선언이 다르면 역시 전송 전에 거절되며(전송 전 사유 `comparison_adapter_channel_mismatch`는 전송 뒤 정산 불일치 `comparison_billing_channel_mismatch`와 구분), `SingleAttemptAdapter`는 `api`를 선언해 구독 요청을 공급자 호출 전에 거절한다. 저장 조회는 요청 채널·정산·토큰이 모두 맞을 때만 구독 `known`이며, 복구는 유료 채널에만 미전송을 추정한다. 일별 API 지출 보고는 구독 시도를 `subscription_attempts`로 따로 적고 USD 합계·불명에 넣지 않는다. 이 변경은 실제 Claude 어댑터가 아니다. runner는 여전히 raw OpenAI/Gemini만 받고 PairSpec Bear는 Gemini를 요구하며 worker/R1은 OFF다. 검증·리뷰·NEXT는 [원장](../operations/execution-roadmap.md#e04-guard-billing-20261101)을 따른다.

<a id="claude-adapter"></a>
## Claude 단일 시도 어댑터 (구현·합성 검증, 운영 비활성)

가드의 채널 소유권 위에 `src/analytics/model_comparison_claude.py`의 `ClaudeSingleAttemptAdapter`를 추가했다. raw `ClaudeCLIClient`(subscription)·`AnthropicAPIClient`(api)만 받아 `billing_channel`·`transport`를 전송 클래스에서 선언하고, 요청 채널 불일치·비Claude 모델·temperature/seed 지정·low 외 effort는 공급자 호출 전 `ValueError`다. 한 번의 dispatch는 그 전송의 `complete` 한 번이며 호출자 시한만 `deadline`으로 넘긴다. 전송 자체의 단일 시도(CLI `CLAUDE_CODE_MAX_RETRIES=0`·`--max-turns 1`·자기 세션 그룹만 정리, API 단일 POST·aiohttp 재시도 없음)와 신원/거절/불완전 출력 분류는 기존 `claude_transport`의 것이고, LLMManager는 감싸지 않는다(CLI→API 대체·쿨다운·월 예약은 그 소유). CLI→API 대체를 조정하는 상위 흐름과 worker 연결은 이번 범위 밖이다.

정산은 구독이면 `settlement=subscription`·`actual_cost_usd=None`·`cost_basis=subscription_not_api_billed`이고 CLI 표시 추정비는 value `subscription_estimated_cost_usd`로만 남긴다(API 청구액·확정 0원으로 바꾸지 않음). API는 `SingleAttemptAdapter`와 같은 규칙(미전송 0·생성 전 거절 추론 0/released·신원 `verified`+사용량 확인 시 가격표·그 밖은 None→unknown 예약)이다. value는 `success`(전송 성공과 신원 검증이 함께일 때만)·`transport_error_kind`·`refusal_detected`·`model_identity_status`·`request_sent`·`http_status`를 보존하고 `effective_reasoning_effort=None`으로 요청 effort와 구분한다. 취소는 원형 전파되어 가드가 `dispatch_entered` 상태로 남긴다.

runner는 `PairedComparisonRunner(claude_adapter=…)` 선택 주입으로 Claude 역할만 그 어댑터에 보내고, 없으면 claim·슬롯 소비 전에 `claude_adapter_required`다. `RoleControls.billing_channel`(기본 api)·`temperature=None`(Claude만)·`PairSpec.bear_empty_retries`(None=기존 의미, Claude Bear는 0 명시)를 추가했다. Bear만 Gemini(역사적 쌍) 또는 Claude(현행 entry Bear)이고 Bull 두 arm은 OpenAI, 명시 폴백 체인은 역사적 OpenAI/Gemini 그대로(Claude만 제외)다. `SingleAttemptAdapter`는 Claude 모델·temperature None을 전송 전 거절한다. 구독 시도는 `_sum_cost`에서 제외하고 arm 사유 `subscription_attempt_cost_not_api_billed`·행 `subscription_attempts`(구독 시도가 있을 때만)로 구분해 불명과 섞지 않는다. 독립 리뷰 반영으로 어댑터 value에는 러너 예외 필드 `error`를 넣지 않고(복구 대조 계약), 러너 `_failure`와 복구 `_verify_attempt`는 슬롯 blocked·토큰 없음·가드 `dispatch_entered`가 True가 아닐 때(슬롯 없는 일일 한도 차단 포함)만 0원을 추정하며 구독 실패는 `billing_channel`을 보존해 전송 불명으로 남긴다. `PairSpec.snapshot()`은 Claude용 새 필드가 기본값이면 생략해 역사적 GPT/Gemini manifest·행을 바이트 동일하게 유지한다. 기존 Gemini/GPT 역할·역사적 manifest는 기본값으로 같은 의미를 유지하며 worker `_raw_clients()`의 활성화 거부·R1 OFF는 그대로다. 실제 CLI/API 호출은 하지 않았으므로 공급자 연결·지연·모델 우위·순수익은 실증하지 않았다. 검증·리뷰·NEXT는 [원장](../operations/execution-roadmap.md#e04-claude-adapter-20261101)을 따른다.

<a id="current-flow"></a>
## 현행 독립 검토 판정·Bear CLI→API 상위 흐름 (구현·합성 검증, worker 비활성)

PR #325까지의 runner는 한쪽 의견으로 approve하고 양쪽 실패 시 레거시 단일 모델 폴백 체인을 썼으며 Bear는 어댑터 하나로 1회만 전송했다. 운영 `AdversarialValidator.validate`(`requires_independent_review`: 한쪽 의견 없음 → hold, 둘 다 있으면 `STRICT_ON_DISAGREEMENT=False`로 하나라도 긍정이면 approve)와 `LLMManager.complete_with`(Bear 내부 11.5초 안에서 CLI `min(8, 11.5/2)`, 허용 실패 종류에서만 남은 시한으로 API 1회, 거절·성공은 대체 없음)를 `PairSpec.review_policy='entry-independent-review-v1'`·`bear_internal_timeout_seconds`·`bear_cli_timeout_seconds`·`bear_api_fallback`으로 표현했다. 기본값은 스냅샷에서 생략돼 역사적 GPT/Gemini manifest·행의 바이트와 의미가 보존된다.

현행 arm은 파싱 의견만의 `policy_verdict`와 신원 인증된 성공 응답·시한까지 본 `verdict`를 분리하고 어느 한쪽이 없으면 `hold`(`independent_review_unavailable`, sizing 재생 없음)다. 신원 인증은 `_identity_certified` — 응답 모델이 요청과 정확히 같거나 계약 v2의 공식 `DOCUMENTED_SNAPSHOTS`일 때만이며, 미확인(None)·불일치는 의견으로 세지 않고 `<role>_identity_unverified_or_mismatch`를 남긴다(독립 리뷰 P1 반영; 레거시 arm의 `success` 의미는 그대로). “두 모델 모두 찬성해야 승인”으로 바꾸지 않았다. 현행 정책에서 `run()`은 운영 패널처럼 Bull/Bear 공통 절대 시한을 task 생성 전에 고정하고(Bull/Bear 역할 시한 동일 필수) Bull은 `timeout_at(min(역할, 패널))`, Bear는 역할·내부·CLI 시한을 패널 기준으로 자른다. 러너 `_bear_role`은 Bear 체인 시작에서 절대 시한을 고정해 어댑터의 task-local 상한(`model_comparison_claude.DEADLINE_AT`)으로 넘긴다 — CLI 단계는 `min(내부 시한, 시작+CLI 시한)`, API 단계는 내부 시한 — 그래서 가드 입장·월 예약 지연이 상대 시한에 더해져도 운영 `started+min(8,total/2)`·11.5초를 넘기지 않는다(독립 리뷰 P1 2건 반영). CLI 단일 시도 뒤 `_api_fallback_authorized`(가드 예외·역할 시한 없이 전송된 CLI 시도가 성공·거절이 아니고 `model_comparison_contract.FALLBACK` 종류)일 때만 `내부 예산 − CLI 완료(정리 포함)까지 경과`를 API 시한으로 전달하며 `bear_api_fallback.timeout_seconds`를 상한으로 더 줄인다. 가드가 응답 뒤 동기로 저장·정산하는 동안 역할/내부 시한을 넘긴 결과는 영수증을 보존한 채 의견으로 세지 않고 hold(`<role>_role_deadline_exceeded_after_response`, 운영 whole-call deadline과 같은 의미)다. 시한 소진이면 `bear_api_fallback_skipped_time_exhausted`, 인가되지 않은 실패면 `bear_api_fallback_not_authorized`, 보냈으면 `bear_api_fallback_dispatched`를 arm 사유에 적는다. 두 시도는 별도 attempt_id·가드 영수증(구독은 월 예약 0, API는 가드 예약 1)이고 최종 Bear 의견은 마지막 시도 1회만 계수한다. 복구는 저장 원본·spec 스냅샷에서 같은 행을 재계산한다.

worker는 새 프로필 키가 있는 manifest를 init에서 `PairSpec.validate_profile`로 검증하고(잘못된 프로필은 생성 실패, 전송 0; 역사적 manifest는 종전처럼 쌍별 `PairSpec.validate`가 claim 전에 검사), 현행 프로필이면 봉투를 생산자 `operating_role_controls()`의 계약으로 대조한다 — Bull/Bear 제어(채널 제외)·`empty_retries`·`strict_on_disagreement`·`bear_empty_retries`·`bear_internal_timeout_seconds`·`bear_transport_order`이며 레거시 단일 모델 폴백 체인은 현행 정책의 운영 조건이 아니라 대조하지 않는다(독립 리뷰 P1 반영: 실제 생산자 봉투가 수락됨을 검사). API 대체가 없는 현행 프로필도 CLI/내부 시한을 적용한다(`bear_cli_timeout_seconds` 필수, P2 반영). 합성 어댑터를 주입한 worker가 CLI→API 체인을 처리함을 검사로만 확인했고 `_raw_clients()`·`install_from_env`의 활성화 거부는 그대로라 운영 worker가 Claude를 호출하기 시작하지 않는다. 실제 호출·경제성·순수익은 실증하지 않았다. 검증·리뷰·NEXT는 [원장](../operations/execution-roadmap.md#e04-current-flow-20261101)을 따른다.

<a id="worker-wiring"></a>
## worker 클라이언트 조립 경로·생산자 현행 프로필 발행 (합성 연결 검증, 운영 비활성)

PR #326까지 worker는 `_raw_clients()`가 `provider_disabled`를 올려 실제로 조립되지 않았고 생산자는 `operating_role_controls()` 제어 봉투를 attach된 handoff가 있을 때만 발행했다. 새 `model_comparison_wiring`은 (1) 호출자가 만든 raw 전송 묶음 `ComparisonTransports`(OpenAI·Claude CLI·Anthropic API, `owned`)와 (2) 명시 승인 기록 `ComparisonActivationApproval`(실험 ID·동결 manifest sha256·KST 날짜 창·전송 집합·승인자)을 받아, (3) `current_profile_manifest`가 생산자 제어에서 동결한 현행 프로필 manifest(요청 모델만, CLI `min(8, 내부/2)`, API 대체 채널 api, 기본 `enabled=False`)와 지문이 맞을 때만 `assemble_comparison_worker`가 runner/worker를 조립·시작·attach한다. 실패(비활성 manifest·지문/실험/날짜/전송 불일치·Gemini 포함·레거시 프로필·전송 누락)는 어댑터·worker 생성 전에 끝나 호출자 전송을 건드리지 않고, `owned=False` 공유 전송은 stop 때 닫지 않는다. `SingleAttemptAdapter(gemini=None)`으로 Gemini 인스턴스 없이 조립하며 Gemini 요청은 전송 전 거절된다.

계속 차단된 경로: `install_from_env`(환경변수+manifest 파일)·`_raw_clients`는 그대로 거부한다. 환경변수·기존 승인·과거 슬롯은 새 실행 권한이 아니며 운영 코드 어디에서도 `assemble_comparison_worker`를 호출하지 않는다. 합성 E2E는 실제 `CrossValidator._offer_comparison`이 발행한 봉투가 조립된 worker에 수락되어 Bull 2·CLI 1(허용 실패)·API 1 → 가드 예약 3(구독 0) → `inspect_pair` 완료 행·같은 봉투 재실행 재전송 0까지 이어짐을 확인했다. 실제 공급자 연결·지연·비용·순수익은 실증하지 않았다. 검증·리뷰·NEXT는 [원장](../operations/execution-roadmap.md#e04-worker-wiring-20261102)을 따른다.
