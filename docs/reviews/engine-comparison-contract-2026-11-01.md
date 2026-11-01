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

독립 리뷰 반영으로 구독 요청은 어댑터의 명시 `billing_channel="subscription"` 선언 없이는 전송 전에 거절되고(유료 요청의 미선언 콜백은 기존 계약), 선언이 다르면 역시 전송 전에 거절되며, `SingleAttemptAdapter`는 `api`를 선언해 구독 요청을 공급자 호출 전에 거절한다. 저장 조회는 요청 채널·정산·토큰이 모두 맞을 때만 구독 `known`이며, 복구는 유료 채널에만 미전송을 추정한다. 일별 API 지출 보고는 구독 시도를 `subscription_attempts`로 따로 적고 USD 합계·불명에 넣지 않는다. 이 변경은 실제 Claude 어댑터가 아니다. runner는 여전히 raw OpenAI/Gemini만 받고 PairSpec Bear는 Gemini를 요구하며 worker/R1은 OFF다. 검증·리뷰·NEXT는 [원장](../operations/execution-roadmap.md#e04-guard-billing-20261101)을 따른다.
