# B1 표준 실행기 — 별도 프로필 진행 원장

2026-09-28 KST 갱신. **공개 B1 경로와 복사 시험 도구의 부품 검증 완료, 실제 프로필·결합 전체 인수 전이다.** 아래 각 단계의 미완 표기는 당시 이력이며 최신 상태는 다음 절을 따른다.
[설계](../superpowers/specs/2026-09-27-b1-standard-runner-design.md)와
[계획](../superpowers/plans/2026-09-27-b1-standard-runner.md)을 따른다.

## Plan

### 2026-09-28 Claude 인계 후 결과 — 결합 후보 df9fd43 인수 (OBSERVED_ONLY)

- **결합:** W 10423c1 + C c93f567 + M 82e107f → `feature/b1-runner-combined-20260928` HEAD `df9fd436641f9c59e499d8a50841396aee0b7b18` / tree `b8716217…`(base b073b54, 경로 W16/C6/M1 중복 0, B1b 5 blob·guard 7b7b2694… 보존, R 미이식). 이후 W에 ff 통합(비문서 diff 0).
- **A 사후 리뷰:** copied 12건 실행 결과 비작성자 리뷰(요청 opus/xhigh, 실제 unverified) `APPROVE_WITH_RECORDED_LIMITS`, P0/P1/P2 0, P3 3(contention 원인 미구분·`--tb=short` 서술 누락·부가 속성 미단언). raw 는 -q 출력뿐이며 env/flock/세션 연결은 서술 — 기록만.
- **C 정적·focused:** py_compile 7 rc0, 비밀 패턴(파일명·내용·verify.sh 정본 패턴) 0, focused 8모듈 **3244 passed/126.01s**·격리0·workload/tee 0/0 (`combined-focused-df9fd43.log` 7c74b5ce…). readiness 리뷰 `APPROVE_WITH_CONDITIONS`(P2-1 초안 CWD 가 C worktree, P2-2 결합 worktree `.superpowers` 비ignore → 산출물을 ignored `logs/b1-smoke-df9fd43/` 로 고정).
- **D 원형 tiny profile 1회:** 동결 `b1-smoke-dispatch-df9fd43.md`(1ac8243a…) → controller 직접 실행(`exec env -i` 5변수, 외부 flock/tee/timeout 없음, `--profile b1-standard/v1 --timeout-seconds 900`, 1파일). raw rc 0, pre/post monotonic **17014274900ns**(같은 host/boot_id), receipt 36/36 passed·collected = 독립 inventory(a7c65f1a…), runtime e694f620…·selection 41f6e286… 독립 계산과 일치, stream 172/0 bytes·hash 일치, coordination 전부 true, pure binder **B1_PROCESS_BOUND errors []**, outcomes/native/CI/production false. 결과 `b1-smoke-evidence-df9fd43.md`, 원본 `b1-smoke-df9fd43-artifacts/`. 한계: 1 slot(UTC/standard)만 실행, `contract` 는 spec sha256 literal.
- **E 전체 UTC→KST:** legacy launcher(timeout 900, `tests`, -x), expected inventory 9379. UTC #1 1 failed(`test_recovery_projection…stays_below_5ms[normal_gc-100000]` controlled_max 12.4ms), UTC #2 **9361/16/2 744.92s**; KST #1 1 failed(`test_toss_observation…[circuit_open-provider_failure]` 189≠200, 실시간 예산), KST #2 **9361/16/2 766.73s**. 두 실패 시험·소스 base 대비 diff 0, 다른 TZ 통과, 2 vCPU·운영 봇 상주. 사용자 결정 "1안으로 재실행해…" 로 TZ당 1회 재실행, 첫 실패 raw 2건 보존, 기존 두 성능 예외 원장 불변·새 예외 0. 기록 `combined-full-evidence-df9fd43.md`.
- **final critical 리뷰:** 비작성자(요청 opus/xhigh, unverified) `APPROVE_WITH_CONDITIONS`, P0/P1 0. P2-1 `tests/dev/test_source_runtime_contract.py:489-496` 1,048,677-byte node ID — B1 full-profile receipt 의 새 차단 사유(M 82e107f 는 부분 해결). P3: ignored pycache 잔존(PYTHONDONTWRITEBYTECODE 미전달 자식 시험), v1 bootstrap 은 `--b1-*` 인자 거부가 추가돼 바이트 동일 아님(controller v1 8MiB 경로는 동일).
- coordinator 모델: 사용자 선택 claude-fable-5-1(정책상 worker 라우팅 제외, coordinator 역할만). 리뷰어 3회 모두 요청 opus/xhigh·실제 unverified·cross-provider 아님.
- **미승인·불변:** N4097 UNRUN, native/source108/full R2/CI/production 미자격, main 병합·운영 배포·재시작·주문·전략/위험/KIS/Toss 설정 변경 0. 후속 정리 후보(별도 계획): 벽시계 시험 2건 시계 주입, oversized id `pytest.param(id=)`, pycache 남기는 시험 확인.

### 최신 진행 상태 (2026-09-28 인계 시점 — 위 절이 최신)

- 별도 후보748f0ad에서 임시 공개125 차단 세 줄만 제거했다. 독립 위임 시험의 실제 RED 뒤 17건 및 새·기존 controller **765 passed/53.97s**·격리0·workload/tee/tool0을 확인했고, 비작성자 리뷰는 `PUBLIC_ENABLE_COMPONENT_ONLY`·미해결 지적0이다.
- 복사 시험 도구는 기본 v1 바이트를 보존하며 닫힌 B1 옵션만 추가했다. 독립 리뷰가 찾은 repo/scripts 심볼릭 링크 우회를 실제 실패 시험으로 재현하고 새 전용 디렉터리 생성으로 막았다. acf18ce의 관련 **15 passed/0.57s**·격리0·세 exit0, 비작성자 `APPROVE_COPIED_HELPER_COMPONENT_ONLY`다.
- 실제 프로세스 후보c93f567은 자손 준비 신호·잘못된 guard 전송 체크포인트 보강 뒤 비작성자 oracle 한정 승인·지적0이다. 독립 실패 dummy **1 passed/1.71s**를 먼저 확인한 뒤 복사본 12사례가 **12 passed/19.30s**·격리0·세 exit0으로 종료됐다. 잠금 경합의 실제 관측 범위와 adapter의 zero-birth 증명을 구분하며 기존 하네스/8초 자식 backstop·정리 한도는 불변이다. 실행 결과의 독립 재검토는 다음 단계다.
- runner 코드의 최종 feature 통합, 원형 B1 소규모 프로필, 결합 전체 UTC/KST·최종 독립 리뷰는 아직이다. 운영 배포·main·주문·설정 변경은 없다. 원문은 `task-3d-public-enable-evidence.md`, `task-3d-copy-helper-evidence.md`와 관련 리뷰에 보존한다.
- 사용자의 Claude 인계 지시로 새 구현·시험·통합을 멈췄다. 마지막 실행 원문은 `task-3d-copied-runtime-evidence.md`, 후속 지시는 [상세 인계 프롬프트](../operations/claude-handoff-b1-runner-2026-09-28.md)를 따른다.

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

공통 실행 경로의 첫 독립 시험e4f774b는 `_run_validated` 부재의 정상 call-phase
RED로 실패했다(1 failed/1.16s·격리0·workload1/tee0/tool1). 이후 동결한 시험
1c7c033은 총1353줄 추가이며 기존3093줄·첫269줄을 보존한다. 독립 리뷰에서
발견한 실패한 결과 생성 재시도 관측 공백과 별도 확인한 실패한 stream-close
재시도 공백은 복원 후 외부 시도 횟수를 검사하도록 보완했다. 후속 정적 리뷰는
`READY_FOR_MAIN_SOURCE_AUTHORING_ORACLE_ONLY`, 미해결 P0/P1/P2 각각0이다.
문법 검사는 통과했지만 새 행렬의 실행 성공을 주장하지 않는다. 구현 후 실제
후속 empty-batch 경로의 존재 여부·sticky 실패 보존을 별도로 확인해야 한다.
source-only 공통 경로 구현을 시작하며 공개125 차단은 유지한다. 원문은
`task-3d-main-first-red-evidence.md`, `task-3d-main-matrix-review.md`에 보존한다.

이후 source95bdb1f(+437/-39)로 단일 owner/Popen 경로를 공유하고 기존 v1의
catch/finally·argv/env·결과 형식을 보존했다. 실제 후속 빈 close batch를 확인한
tests-only dfcf5e6(+2)까지 결합해 원래 node와 실패 상태3건 **4 passed/1.47s**,
새·기존 controller 전체 **757 passed/52.43s**·격리0·세 exit0이다. 독립 검토는
`READY_TASK3D_SHARED_MAIN_COMPONENT_ONLY`, P0/P1/P2 각각0이며 정리 소유권·
복합 종료·결과 발행·v1 보존과 조건부 oracle를 인수했다. 공개 B1 차단 해제,
copied 실제 프로세스·원형 profile·결합 UTC/KST 전체 검증은 다음 별도 단계다.
원문은 `task-3d-main-focused-evidence.md`, `task-3d-main-source-independent-review.md`다.

후속 경계 검토에서 최초 시계 읽기가 모드 판별보다 앞이라 내부 함수의 hard125
보장에 포함될 수 없음을 확인했다. 독립 정책 리뷰 후 기존 진입 순서·예외 전파는
보존하고 hard125 약속은 검증된 내부 실행 진입 이후로 명시했다. 최초 SystemExit(0)
가능성을 숨기지 않으며, 반환0만으로는 승인하지 않는 전체 증거 결속 조건은 그대로다.
공개 활성화 전 두 프로필의 초기 예외 동일성·부작용0·누락 증거 거부 시험이 필요하다.
정책 검토 기록은 `task-3d-entry-clock-disposition-review.md`이며 구현 승인과 구분한다.

후속 실제 copied-harness 시험의 닫힌 배치도 정책 한정 승인받았다. 일반 복사본은900초,
시간 의존 사례만 복사본 내부 total6을 사용하며 실제 정책·기존 외부 안전 한도는
바꾸지 않는다. 타이머 시작점이 달라6<8<12를 절대 시한 보장으로 주장하지 않으며,
실제 작업 checkpoint·회수 증거가 없으면 중단한다. 아직 helper 수정/실행하지 않았고
공개 활성화 우회나 실제 B1 qualification 승인이 아니다. 상세 조건은 설계 §8과
`task-3d-copied-harness-policy-review.md`에 보존한다.

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
