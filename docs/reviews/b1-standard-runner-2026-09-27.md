# B1 표준 실행기 — 별도 프로필 진행 원장

2026-09-27 KST. **순수 계약·인자·시간 예산 adapter 부분 구현/검토 완료, 실제 프로필 검증 전이다.**
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
세지 않는다. 원문을 보존하고 trap을 test-body context로 제한하는 진단 기반 수정
후 정상 RED를 확인하기 전까지 해당 구현 진행을 멈춘다. 성능 예외로 처리하지 않는다.

실제 소규모 프로필의 원래 대상 파일에서 수집 node ID65647 bytes가 producer2048
제한을 넘는 문제를 확인했다. 별도 critical 리뷰 후 기존 base-contract 모듈36 nodes/
최대149 bytes로 미실행 대상을 교체한다. 기존 OS 회귀·전체 시험·producer 제한은
그대로다. 따라서 향후 B1 receipt 방식의 전체 시험은 별도 호환성 해결 전 차단이며,
현재 계획의 legacy 전체 검증과 소규모 smoke를 그 해결로 주장하지 않는다.

이후 별도 한정 계획의 독립 승인으로 해당65537-byte 입력에 짧은 parameter ID만
부여한 별도 후보82e107f를 만들었다. fresh collection109(OS73/base36), 이전108개
이름 불변·지정한 한 이름만 변경·최대178bytes, 두 모듈109 passed/0.52s·격리0이다.
producer/2048 상한/입력/assertion은 불변이다. 독립 focused 리뷰와 최종 combined
후보 전체 검증 전에는 최종 인수하지 않으며, 모든 suite ID 또는 B1 full-profile
적합성이 증명됐다고 확대하지 않는다.

N4097은 별도 의미 전이·전수 oracle·세 실행 예산의 승인과 결과 전까지 UNRUN이다.
현재 B1b 구성 시험과 파일 소유권을 분리한다. 기존 성능 예외·guard·제품·CI·main·운영·
주문·전략·위험/KIS/Toss 설정은 변경하지 않는다.
