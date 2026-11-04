# E04-BEAR-COMPLETION-REVIEW — 기존 영수증으로 확인한 완료 조건

기준 소스: PR #337 병합 `7bbad4f3b967847ba3eaa2b987ce346ee48ada39`(PR HEAD `3b8c4508`과 tree 동일). 사용자 요청은 병합 뒤 기존 영수증·코드로 시한 내 의견 완성 조건을 확인하는 것이다. 추가 실호출·검증 재실행·설정 변경은 포함하지 않는다. [11/4 조사](e04-bear-completion-2026-11-04.md)를 반복 실행하지 않고, 11/5 진단 결과를 더해 결론을 갱신했다.

**결론:** 400토큰 절단은 두 기존 API 응답에서 확인됐다. 800은 완성된 응답을 얻지 못해 충분한 상한인지 판단할 수 없다. 현재 자료는 상한 증설·시한 연장·모델/effort 변경을 선택할 근거가 아니다. 다음 최소 단위는 **취소 예외에 첨부될 수 있는 허용 목록 상태가 진단 영수증까지 전달되는지 합성으로 확인하는 것**이다. 이는 완료율 개선 자체가 아니라 다음 변경의 근거를 확보하기 위한 제한된 진단 계약이며, 이번 리뷰에서는 구현·실행하지 않는다.

## 기존 관측과 한계

| 기존 실행 | 관측 | 확정할 수 없는 것 |
|---|---|---|
| 11/4 CLI400 | 5,638.770ms, cli_timeout, 모델 신원 확인 | 기동·대기·생성 중 어느 단계가 지연됐는지 |
| 11/4 API400 | 5,234.946ms, HTTP200, 입력1,143/출력400, max_tokens, known | 생각/답변 토큰 배분, 최종 의견·실패 시나리오 |
| 11/5 API400 | 5,671.093ms, HTTP200, 입력1,143/출력400, max_tokens, known | 완료에 필요한 최소 토큰·시간, 반복 성공률 |
| 11/5 API800 | 5,798.837ms, 외부 TimeoutError, 사용량·응답 신원·비용 unknown | 800토큰 생성 여부, 헤더 수신 여부, 완료에 필요한 추가 시간 |

11/4 영수증의 벽시각으로 CLI 응답→API 요청 간격은67.571ms, 첫 CLI 어댑터 요청→API 응답은10,941.287ms다. 이는 이미 저장된 요청/응답 시각의 차이이며 monotonic 시한·가드 입장·네트워크 단계별 시간은 아니다. 전체 인수 영수증 구간11,167.422ms와도 구분한다. 두400 응답에서 값이 같아도 같은 내부 생성 경로나 속도를 입증하지 않는다.

800의5,798.837ms는 예외 전파·가드 정리·조회 뒤에 측정한 시도 시간이다. 시한 뒤 결과를 의견으로 수락했다는 증거가 아니며, 마지막 client.close는 그 측정 이후다. 원응답·숨은 추론을 복구하거나 누락 필드를 추정해 채우지 않았다. unknown 예약은 그대로이며 이 리뷰에서 월 원장을 열거나 대사하지 않았다.

## 어느 단계에서 무엇을 완료로 보는가

| 단계 | 코드상 조건 | 해석 |
|---|---|---|
| API 전송 완료 | HTTP200, 정상 JSON, 거절/도구 블록 없음, 정확한 모델 신원, end_turn, 비어 있지 않은 text | max_tokens는 api_incomplete, 부분 본문은 비움 |
| Bear 의견 파싱 | 대소문자 무관 ACCEPT/REJECT 중 먼저 나타난 부분문자열 | JSON 스키마·독립 단어·실패 시나리오의 타당성을 검증하지 않음 |
| 현행 비교 흐름 | 양쪽 성공·인증된 의견, 역할/내부/공통 패널 시한 내 완료, 마지막 시도 timeout 아님 | Bear 미완료면 hold; Bear 의견 확보와 매수 승인은 별개 |
| API 단독 진단의 valid | 위 전송·파싱·시한 조건에 더해 알려진 사용량/측정 정산, HTTP200/reported_usage, 거절 없음 | known400 절단은 다음800 조건 진행 허용일 뿐 유효 의견이 아님 |

근거: [전송](../../src/utils/claude_transport.py#L456), [의견 파서·프롬프트](../../src/core/adversarial_validator.py#L97), [현행 arm](../../src/analytics/model_comparison_runner.py#L675), [진단 판정](../../src/analytics/bear_api_cap_diagnostic.py#L222). 현행 비교 흐름의 의견 판정과 진단의 비용 인수 조건을 같은 것으로 표현하지 않는다. `completed` 같은 전체 상태만으로 두 조건 모두 의견을 완성했다고 해석하지 않는다.

요청은 low·adaptive thinking·max_tokens400/800이며 별도 thinking budget이나 JSON 출력 스키마를 보내지 않는다. API는 스트리밍 요청을 하지 않고 reply.json 전체를 기다린다. text 블록만 의견 대상으로 추출하고 영수증의 output_tokens는 공급자가 보고한 합계다. 400이라는 수치만으로 최종 답변400토큰 또는 추론400토큰이라고 결론내릴 수 없고, 실효 effort도 노출되지 않는다. 이전 공식 문서 조사 내용은11/4 보고서의 역사적 근거로 유지하며 이번에 외부 조회하지 않았다.

## 시한은 새로 생기지 않는다

- 운영 entry: 패널12초, Bear 내부11.5초, CLI 상한5.75초. API 대체는 허용된 CLI 실패 이후 **내부 시한에서 이미 경과한 시간을 뺀 나머지**를 쓴다. CLI 정리·가드 입장도 시간을 소비한다. CLI를 늘리면 같은 내부 시한에서 API 시간이 줄어든다.
- 비교 runner: 공통 패널 시한은 task 생성 전에 고정하며 Bear 내부 시한은 이를 넘을 수 없다. 어댑터는 상대 요청 시한과 전달된 절대 상한 중 빠른 쪽을 사용한다. 응답이 정상이어도 동기 저장·정산 뒤 늦으면 의견에서 제외한다.
- 이번 API 단독 진단: 각 조건에 독립5.75초를 주되 가드 입장 전부터 센다. 반환 후 lookup 뒤에도 늦음을 검사한다. 운영 fallback의 실제 잔여 시간을 측정한 결과가 아니다.

근거: [운영 매니저](../../src/utils/llm.py#L1253), [Bear 흐름](../../src/analytics/model_comparison_runner.py#L611), [어댑터 절대 상한](../../src/analytics/model_comparison_claude.py#L54), [진단 시한](../../src/analytics/bear_api_cap_diagnostic.py#L341). 특정 토큰 상한이면5.75초 내 완료한다는 보장은 이 코드나 기존 표본에 없다.

## NEXT: E04-BEAR-TIMEOUT-EVIDENCE

정적 코드에서 **정보 전달 공백 가능성**을 확인했다. 실제800 예외에 그 정보가 있었음을 입증한 것은 아니다.

1. [API 취소 처리](../../src/utils/claude_transport.py#L511)는 현재 LLMResponse를 CancelledError의 `_llm_response`에 붙여 다시 올린다. 가드도 정산·중단 기록 후 예외를 다시 올린다.
2. [진단 예외 처리](../../src/analytics/bear_api_cap_diagnostic.py#L361)는 예외 유형만 `_evidence(result=None)`에 넘긴다. 결과가 없으면 응답/사용량 필드는 기본 unknown이 된다.
3. 기존 [매니저 처리](../../src/utils/llm.py#L914)에는 `TimeoutError.__cause__._llm_response`를 읽는 선례가 있다. 이를 그대로 복사하지 말고 진단의 정산·거절 경계에 맞게 최소 합성 계약부터 검토한다.

후속 오프라인 범위는 첨부 정보 있음/없음, 내부 전송 timeout/외부 timeout, 직접 취소, 위조·불일치 객체, 예외 원인 범위 제한을 합성으로 구분하는 것이다. 허용 목록의 안전한 상태만 검토하고 원응답·프롬프트·예외 전문·임의 속성·숨은 추론은 저장하지 않는다. 첨부가 없으면 unknown을 유지하며 `request_sent=False`를 만들어 내지 않는다. 기존 `request_sent=True`도 session.post 진입 전에 세운 클라이언트 플래그이므로 공급자 수신 증거는 아니다.

원장/가드의 정산이 우선이다. 추가 상태를 얻어도 outcome=error·verdict=None·중단·재시도0·unknown 예약을 유지하고, 이를 성공·비용 확정·예약 해제·800 재호출 근거로 쓰지 않는다. 광범위한 새 단계 계측보다 기존 상태 재사용 가능성 확인을 먼저 한다. 이번 리뷰에는 구현·새 합성 검사·실호출·관측기간 연장·배포가 없다.

## See 및 보존

기존 공개 검사를 읽어 계약을 대조했으며 재실행하지 않았다: `test_api_request_and_cache_usage`, `test_api_requires_complete_identity_text`, `test_api_fallback_receives_only_remaining_internal_time`, `test_admission_latency_cannot_extend_the_bear_internal_deadline`, `test_result_arriving_inside_deadline_but_settled_after_it_is_not_counted`, `test_transport_exception_receipt_keeps_unknown_attempt_evidence`.

별도 Astra/high가 완료 조건과 예외 정보 경계를 독립적으로 정적 검토했다(요청 모델/effort, 실제 모델·실효 effort metadata 미노출). coordinator는 기존 두 영수증의 상태·시각·본문 미저장 여부와 소스를 대조했다. 최종 문서 리뷰 P0/P1 0·P2 표현 보정2건은 위 결론의 조건부 표현과 보호 파일/미커밋 변경 구분에 반영했다. 기존 WSL/PR 검증을 반복하지 않았으며 병합으로 자동 시작된 main CI run41859848787만 결과를 확인했다: 10,484 passed / 2 skipped / 2 xfailed,527.28초·격리0·비밀검사 PASS. 판독 문서는 feature/e04-bear-completion-review-20261105에 커밋/푸시하고 후속 소스 작업에 함께 통합한다(별도 상태 PR/전체 검증 없음).

운영 `a399c297`, 기존 evolved 미커밋 변경과 default/evolved/KILL 지문, 월USD100/log_and_continue·R1/worker 비활성·Gemini 차단을 유지한다. 기존 영수증은 해시로 보존을 대조한다. 비공개 근거: `~/.local/state/qwq-task-audit/20261105/e04-bear-completion-review/`. E02/E01·O02는 이번 리뷰의 실행 대상이 아니다.
