# Health 반환값·일자 관측 정합성 — 개발 인수

2026-09-27 KST. **부품 독립 승인·feature 통합·집중 및 전체 UTC/KST 검증 완료.** 운영 health/경보 연결이나
전체 엔진 승격 보고가 아니다. 공통 구현 base는 `af098769d7095c5630bbaa7c98301794ad213fd5`다.

## Plan

기존 [P1 후속](../operations/p1-next-steps-2026-09-23.md)이 남긴 health의 두 관측 경계를
실제 코드에서 확인했다. 각각 별도 Terra/high 작성·격리 worktree, 작성자와 다른
Astra/xhigh 검토, coordinator 통합으로 진행한다. 두 소스 파일은 겹치지 않는다.

1. `ProtectionProducer.health()`가 반환한 `restart_retries[*].intent_ids`는 내부 RAM과
   같은 list였다. 반환값을 수정하면 보호 재시도 상태를 바꿀 수 있다. 알려진 mutable leaf만
   새 list로 복사하며 dict/list schema·ID 순서/중복·reason·ISO/null은 유지한다.
2. `KRExecutionRuntime.health()`가 동일 일자 차단 property를 두 번 평가한다. KST 자정
   경계에서 한 응답의 상단과 reconciler 표시가 상충할 수 있다. 첫 기존 필드 자리에서
   한 번 얻은 값을 두 표시에 사용하며 첫 관측의 순서와 다음 요청의 fresh 판단은 유지한다.

제품의 실제 admission property·시계·쿨다운·주문·전략·위험 설정은 변경하지 않는다.
반환 목록 수정 방어는 미래 모든 mutable field의 자동 방어가 아니며 일자 표시의 단일
관측은 health 전체의 원자적 snapshot·bounded latency 보증이 아니다.

## Do

각 작성자는 실제 합성 runtime 경로에서 RED를 먼저 보존하고 최소 제품 변경과 새 시험을
작성한다. 모델 metadata가 노출되지 않으면 요청 모델/effort와 실제 실행 identity를 구분한다.
시험은 공통 runtime-contract workload lock 아래 직렬이며 전체 suite는 작업자 종료 후
coordinator만 UTC→KST로 실행한다. 기존 실패를 숨기는 skip/xfail/임계값 변경은 하지 않는다.

retry 저자 후보 `c5bd69a`, day 저자 후보 `40ab862`는 독립 Astra/xhigh의 Spec/Quality
승인을 받았다(C0/I0, retry 자문M1). M1은 최종 retry RAM을 현재값 양쪽으로 대조하던
시험 한 곳이다. 원저자 재활성화의 thread limit 때문에 coordinator가 `25cbf9f`로
관측 전 snapshot의 독립 복사와 의도한 한 원소 변경을 기대값으로 사용하도록 보완했다.
동일 독립 리뷰어가 M1 CLOSED·새 지적0으로 재승인했다. 제품 추가 변경은 없다.

통합은 `46c0a1d`·`d17c956`·`059c7c4`이며 각 코드/시험 bytes는 최종 승인 후보와 같다.

| 검증 | 실제 결과 |
| --- | --- |
| 작성자 retry RED | alias identity 실패1, exit1; 당시 tool 출력의 사후 보존 |
| 작성자 day RED | `(False, True, 2)`와 `(False, False, 1)` 불일치, 1 failed/2 passed; transcript 요약 |
| root retry `c5bd69a` | 117 passed/30.35s, 실제 도구 exit0/격리0 |
| root day `40ab862` | 146 passed/31.11s, 실제 도구 exit0/격리0 |
| root M1 보완 | 1 passed/2.07s, 실제 도구 exit0/격리0 |
| root 결합 `059c7c4` | 201 passed/39.49s, 실제 도구 exit0/격리0 |
| runtime/기존 도구 포함 결합 | 610 passed/164.68s, 실제 도구 exit0/격리0 |
| 전체 UTC `18a2196` 코드 | 6329 passed/641.35s, 기존16 skipped/2 xfailed/4 warnings·실제 도구 exit0/격리0 |
| 전체 KST 동일 코드 | 6329 passed/643.67s, 기존16 skipped/2 xfailed/4 warnings·실제 도구 exit0/격리0 |

최초 저자 원문은 실행 시점 파일이 없었던 절차 한계를 남긴다. 특히 retry의 과거 원명령에는
요청한 timeout이 없었다. root의 새 실행은 모두 실제300초 cap·공통 lock·env-i·pipefail+tee로
동시에 원문을 저장했으며 새 GREEN으로 과거 RED 보존 공백을 덮지 않는다. 정확한 명령/cwd/
후보·도구 exit는 root 실행 기록과 함께 읽는다. 전체는 worker 종료 뒤 coordinator 단독으로
같은 환경/plugin/lock 아래 각각900초 cap으로 직렬 실행했다. 새 Astra/xhigh broad와 문서 보완
재검토도 C0/I0/M0으로 승인했다. compile-only545·비밀 패턴·diff·보존 지문 검사도 완료했다.
검증 중 계획 진행 체크 문서만 바뀌었고 source/test bytes는 동일하다. 전체 원문/명령은
옆 runtime-admission 원장의 `full-*-18a2196.log` 및 `coordinator-validation-ledger.md`를 참조한다.

## See와 다음 경계

새 회귀는 두 방향 list alias, null/ISO/순서/중복, 자정 전후·요청 간 fresh 관측,
short-circuit의 clock0, naive 시계의 기존 오류 및 producer→clock 순서를 고정한다.
owner/store/broker writer와 경제·보호 상태가 관측으로 변경되지 않는지도 대조한다.

이 수정으로 full owner snapshot/이력 전량 순회가 없어지는 것은 아니다. N5 index/owner
publication·실제 bounded capture와 N6 sampler/최소5거래일 shadow/경보 소비 인수는 미완료다.
`trading_ready=False`, KIS 거래·잔고/Toss 관측 구분을 유지한다. main·운영·외부 API 실행0.

원문은 coordinator `.superpowers/sdd/2026-09-27-health-retry-snapshot/`과
`.superpowers/sdd/2026-09-27-health-day-observation/`에 보존한다.
