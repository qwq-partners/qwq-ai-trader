# E04 — 현행 GPT/Claude 비교 계약 점검

기준 main `1eb023a940b52820261f129402b24f3789fceb02`, 2026-11-01 KST. 이 점검은 기존 실행 원장의 E04 “현재 Claude 계약용 manifest/runner 지원부터 확인”을 수행한다. 실제 비교 실행·유료 호출·모델 변경·운영 활성화는 포함하지 않는다.

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
| 전송·정산 | Claude CLI 우선, API 대체; 구독 표시 추정비와 API 비용 구분 | adapter는 raw OpenAI/Gemini, 실행 정책은 `single-provider-attempt-v1`이다. 다중 전송을 `LLMManager` 하나로 감싸면 기존 예약/시도 의미가 달라진다. |
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
