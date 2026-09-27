# B1 표준 실행기 — 별도 프로필 진행 원장

2026-09-27 KST. **순수 계약·인자·시간 예산·coordination 부분 구현/검토 완료, 실제 프로필 검증 전이다.**
[설계](../superpowers/specs/2026-09-27-b1-standard-runner-design.md)와
[계획](../superpowers/plans/2026-09-27-b1-standard-runner.md)을 따른다.

## Plan

기존 실행기의 8MiB/v1 동작은 보존하고, 고정 B1 프로필에 스트림별 2MiB 보존·초과 감지,
공통 lock·격리 환경·종료/회수·900초 전체 상한을 결속한다. 기존 owner를 재사용하며
새 reaper·임의 상한/명령/환경 입력은 추가하지 않는다. `B1_PROCESS_BOUND`는 프로세스
증거 결속뿐이며 시험 결과/skip/xfail·native·CI·운영 승인으로 쓰지 않는다.

Sol/high 요청 조사와 Astra/high 요청 설계 뒤 별도 Astra/xhigh 요청 reviewer가
호출별 도구 대기시간은 전체 실행시간이 아니라는 I1을 지적했다. 같은 호스트의 독립
monotonic 전후 관측·연결된 원문을 이용한 보수적 상한으로 바꾸고 재리뷰에서
**APPROVE_RUNNER_PLAN_ONLY**, C0/I0/M0이다. 빈 시간 차감·더 좋은 수치 재실행은 없다.
실제 모델/effective effort metadata는 미노출·unverified이며 같은 공급자 독립 리뷰다.

## Do → See

독립 literal 계약 시험/RED → 순수 v2 검사 → 별도 controller/독립 시험 → critical 검토
→ worker 종료 후 실제 소규모 프로필 관측 → 기존 표준 전체 UTC/KST 순서다.
독립 tests-only `4534e44`의 첫 node는 의도된 새 API 부재의 call-phase
AttributeError로 실패했다(1 failed/0.16s, workload1/tee0/tool1·격리0).
수집/임포트 오류가 아니며 순수 계약 구현으로 진행했다. 원문과 분류는
후속 원장의 `first-contract-red-evidence.md` 및 raw log에 보존한다.
계약 GREEN·controller 변경·시계 관측·새 프로필 인수 성공을 아직 주장하지 않는다. 원문은
`.superpowers/sdd/2026-09-27-decoder-followup/`에, 후속 실행 원장은
`.superpowers/sdd/2026-09-27-b1-standard-runner/`에 보존한다.

첫 순수 계약 후보153f439에서 원래 RED node가1 passed, 신규+기존 두 모듈367 passed/
1.41s·격리0·workload0/tee0/tool0이다. 독립 critical 리뷰가 Task1 한정 승인했다.
후속 Task2a632a369는 parser/bootstrap 및 기존 controller 결합170 passed/33.53s,
격리0·workload0/tee0/tool0이고 독립 source 검토에서 결함0이다. 유효 B1 main은 아직
의도적으로125로 차단한다. Task3 제안의 fork 자식 상속 FD·nonblocking lock-open
경계 P1 두 건은 보완 뒤 `APPROVE_TASK3_SEAMS_ONLY`로 재승인됐다. 다음은 budget/
probe/cleanup의 독립 tests-first이며 실제 프로필·전체 인수 성공으로 표시하지 않는다.
원시 결과와 제안/리뷰는 위 후속 artifact 디렉터리에 보존한다.

Task3a tests-only6004865에서 `_B1Budget` 부재의 정상 call-phase RED를 확인했다
(1 failed/0.20s, workload1/tee0/tool1·격리0). 독립 행렬5579f14 후 source-only
eadcd7d에서 원래 node1 passed/0.26s, 새+기존 controller216 passed/34.76s,
workload0/tee0/tool0·격리0이다. 별도 critical 리뷰는 P0/P1/P2 각각0이며
`READY_FOR_TASK3B_SEPARATE_UNIT_WORK`다. 이어 test-only fa80b3a에서 fork OSError와
기존 cleanup 상태의 직접 시험3건을 보강해219 passed/34.64s·격리0·세 exit0이며,
작은 delta도 독립 재검토 지적0으로 마쳤다. source eadcd7d는 그대로다.
원문·정확한 후보·리뷰는 `task-3a-first-red-evidence.md`,
`task-3a-focused-evidence.md`, `task-3a-independent-review.md`에 보존한다.
Task3b의 예외 우선순위·정리 lifecycle 설계는 지적 보완 뒤 독립 tests-first 한정
승인받았다. coordination 구현·전체 Task3·실제 profile 인수는 아직 아니다.

Task3b 최초 constructor 시험2b6cae0는 전역 syscall trap이 pytest의 종료 처리까지
막는 시험장치 결함으로 exit1이었다. 의도한 missing-class RED 또는 격리0으로
세지 않는다. 원문을 보존하고 trap을 test-body context로 제한한9d6f0f9에서 정상
missing-class RED(1 failed/0.33s·격리0)를 확인했다. 성능 예외로 처리하지 않는다.

독립 상태/예외 행렬873fc189(+918줄) 뒤 source-only2f5c9c5에서 원래 constructor
1 passed/0.50s, 전체 controller focused **333 passed/38.18s**·격리0·세 exit0을
확인했다. 비작성자 critical 리뷰는 `READY_FOR_TASK3B_COMPONENT_ACCEPTANCE`,
P0/P1/P2 지적0이다. prepare/finish의 temporary-close 두 조건부 항목은 해당 소스
경로에 임시 FD close가 없음을 독립 확인해 N/A로 닫았다(PASS/skip/면제가 아님).
expected OSError 처리·기존 예외 우선순위·lock-last close에 한정하며 BaseException
전체 정리나 main 실행을 승인하지 않는다. 원문은 `task-3b-focused-evidence.md`와
`task-3b-independent-review.md`에 보존한다.

Task3c 출력 전용 시험55bc822의 첫 node는 미구현 profile 인자의 정상 call-phase
TypeError로 RED(1 failed/0.62s·격리0·workload1/tee0/tool1)다. 후속44fb2a4까지
총531줄의 독립 시험 뒤 source-only c632e57에서 동일 node1 passed/0.72s,
새+기존 controller **415 passed/42.12s**·격리0·세 exit0이다. 시험과 source를
분리해 검토한 비작성자 리뷰가 `READY_FOR_TASK3C_COMPONENT_ACCEPTANCE`,
P0/P1/P2 지적0으로 마쳤다. `_observe` 바깥 및 기존 v1 회계 블록은 불변이며,
main 실행·실제 프로필·전체 suite 인수는 별도다. 원문은 `task-3c-focused-evidence.md`,
`task-3c-oracle-review.md`, `task-3c-independent-review.md`에 보존한다.

Task3d main/finalization 제안의 독립 리뷰는 P1 두 건, P2 한 건으로 변경 요청이다.
probe 예외 시 정리 소유권·fork 자식의 부모 정리 진입 차단, helper가 FD 목록을
비운 뒤 BaseException이 나도 남은 close를 시도하는 보장, 종료 실패가 겹칠 때
124/125 분류가 보완 대상이다. 기존 Task3a/3b의 기대 OSError 범위를 소급 실패로
바꾸지 않고, main의 더 강한 보장에 필요한 별도 tests-first 선행 작업으로 처리한다.
수정안은 독립 재리뷰에서 `APPROVE_TASK3D_TESTS_ONLY`를 받았다. 승인된 종료
우선순위와 선행 정리 계약을 [설계 §6.1](../superpowers/specs/2026-09-27-b1-standard-runner-design.md#61-reviewed-task3d-prerequisite-and-mixed-failure-clarification)에 기록한다.
별도 probe-child RED·helper 행렬 → 구현 → critical 검토 뒤 shared main 단계로
진행하며, B1 main125 차단은 유지한다. 설계 지적 해소를 구현 완료로 세지 않는다.
첫 prerequisite 시험9386774에서 close의 SystemExit(0)이 자식 terminal125 경계까지
이어지지 않는 결함을 실제 RED로 확인했다(1 failed/0.92s·격리0·세 exit1/0/1).
실제 fork 없이 독립 adapter를 사용했고 시험 종료 전 patch 복원을 확인했다.
후속 독립 행렬c4212c4(+730줄)와 source-only2b19be8(+129/-33줄)를 동결했다.
그러나 새 BaseException 처리 아래에서는 시험 내부 assertion 자체가 흡수될 수 있다는
독립 oracle P1을 확인해 GREEN 판정을 보류했다. 최초2341줄과 원래 RED는 보존하고,
추가 행렬의 close/금지 호출 시도·detach 시점 상태를 바깥 원장으로 검증하도록 보강한다.
이는 신규 시험의 관측 공백이며 현재 source 결함 판정이나 이전 OSError 한정 승인
철회가 아니다. 보강 시험과 source의 별도 독립 검토·실행 뒤에만 main 배선으로 진행한다.

보강e1d250f의 독립 oracle 승인 뒤 원래 결함 node는1 passed/0.92s였다. 이어진
첫 focused는 새 감도 시험이 pytest의 가공된 assertion 메시지를 원래 tuple로
가정해467 passed/1 failed로 중단됐다. 이 실패를 보존하고 외부 판정 위치·조건을
유지하는 명시적 AssertionError(+2/-1)만 적용했다. 감도5건·작은 독립 재검토 뒤
결합5575186에서 **568 passed/42.79s**·격리0·세 exit0을 새로 확인했다.
source651e7305…는 그대로이고 비작성자 source/evidence 리뷰가
`READY_FOR_TASK3D_PREREQUISITE_COMPONENT_ACCEPTANCE`, P0/P1/P2 각각0으로
마쳤다. 원문은 `task-3d-prerequisites-focused-evidence.md`와 관련 raw·리뷰에
보존한다. 다음은 공통 `_run_validated` 경로의 독립 첫 RED·main 행렬이며 공개
B1 main125 차단·runner 최종 feature 통합 전 상태는 유지한다.

실제 소규모 프로필의 원래 대상 파일에서 수집 node ID65647 bytes가 producer2048
제한을 넘는 문제를 확인했다. 별도 critical 리뷰 후 기존 base-contract 모듈36 nodes/
최대149 bytes로 미실행 대상을 교체한다. 기존 OS 회귀·전체 시험·producer 제한은
그대로다. 따라서 향후 B1 receipt 방식의 전체 시험은 별도 호환성 해결 전 차단이며,
현재 계획의 legacy 전체 검증과 소규모 smoke를 그 해결로 주장하지 않는다.

이후 별도 한정 계획의 독립 승인으로 해당65537-byte 입력에 짧은 parameter ID만
부여한 별도 후보82e107f를 만들었다. fresh collection109(OS73/base36), 이전108개
이름 불변·지정한 한 이름만 변경·최대178bytes, 두 모듈109 passed/0.52s·격리0이다.
producer/2048 상한/입력/assertion은 불변이다. [독립 focused 리뷰](b1-nodeid-compatibility-2026-09-27.md)는
지적0으로 마쳤으나 최종 combined 후보 전체 검증 전에는 최종 인수하지 않는다.
모든 suite ID 또는 B1 full-profile 적합성이 증명됐다고 확대하지 않는다.

N4097은 별도 의미 전이·전수 oracle·세 실행 예산의 승인과 결과 전까지 UNRUN이다.
현재 B1b 구성 시험과 파일 소유권을 분리한다. 기존 성능 예외·guard·제품·CI·main·운영·
주문·전략·위험/KIS/Toss 설정은 변경하지 않는다.
