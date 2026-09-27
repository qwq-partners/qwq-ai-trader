# OS Process Evidence Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Steps use checkbox syntax for tracking.

**Goal:** pytest receipt를 실제 자식 종료·회수 결과와 결속하는 로컬 한 슬롯 도구를 만든다.

**Architecture:** 기존 receipt v1은 유지한다. 전용 CLI controller와 고정 bootstrap이 실제 관측을
생산하고 순수 parser/validator가 independently supplied expectation과 대조한다.

**Tech Stack:** 기존 Python/pytest와 Linux waitid/waitpid/subreaper/pidfd, stdlib만 추가 사용.

**Spec:** `docs/superpowers/specs/2026-09-27-os-process-evidence-design.md`.

## Global Constraints

- 독립 계획 재리뷰 APPROVE_PLAN_ONLY. 앞 단계 검증·공통 base 확정 전 구현/새 process-control 실행0.
- Source-boundary 단계 검증·개발 통합 뒤 coordinator가 실제 공통 base SHA를 원장에 고정한다.
- 기존 producer/receipt/contract/guard/src/config/CI 변경0. qualified runtime0, source108 실행0.
- feature-only, 외부 API/SSH/설치/권한/운영 변경0, HOME/운영 환경 전달0, worker fanout0.
- root+최대3workers. 동일 base 별도 worktree, 소유 파일 겹침0. root만 통합한다.
- 모든 시험은 공통 `.superpowers/sdd/2026-09-27-os-process-evidence/test-workload.lock`으로 직렬.
- focused300초/전체900초 cap, raw 최초 실패와 tool 반환 exit code를 별도 보존한다.
- env -i PATH=/usr/bin:/bin LANG=C.UTF-8 TZ=UTC 또는 Asia/Seoul,
  PYTHONDONTWRITEBYTECODE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1. 기존 venv Python만 사용.
- controller는 standard/local만 지원하며 UI/CI launcher/qualification/skip 허용은 추가하지 않는다.

## Review Focus

1. 출력 후 죽는 child/atexit hang → file 존재가 아니라 실제 OS status로 거부(Task2).
2. reused PID·누락 proc entry·서로 다른 reaper → pidfd+양성 소유권, final ECHILD(Task2).
3. child 종료 전 읽은 receipt/원문 유실 → cleanup 뒤 bounded read/hash·전용 raw 로그(Task2).
4. exact JSON에서 bool/null/중복 key·부분 파일 → strict schema와 고정 오류(Task1).
5. 자체 기대값·서로 다른 run/slot/launch → 독립 expected 대조, CI/자격 주장false(Task1/3).

## Task 1: 순수 process-result 계약

**Owner:** Terra/high, bounded pure schema/validator. Budget35분.
**Files:** Create `scripts/dev/verification_os_contract.py`, `tests/dev/test_verification_os_contract.py`.
**Interfaces:** `ProcessEvidenceError(ValueError)`, `parse_process_result(raw: bytes) -> dict`,
`validate_controlled_receipt(receipt_raw: bytes, process_raw: bytes, expected: dict) -> tuple[str,...]`,
`evaluate_controlled_slot(receipt_raw: bytes, process_raw: bytes, expected: dict) -> dict`.
Spec exact fields/values are binding; source/capsule/Git/OS calls are forbidden in this module.

- [x] 독립 literal fixture로 정상 one-slot evidence와 expected를 작성한다. 기존 v1 expectation은
  네 슬롯 형식이지만 새 consumer는 그 중 actual 한 슬롯만 대조하며 나머지 실행을 주장하지 않는다.
  spec의 exact expected wrapper/ordered canonical selector digest/고정 code를 따른다.
- [x] 새 정의 부재 RED, 이후 malformed duplicate/unknown/nonfinite/depth8·bytes65536 경계,
  int/bool/null returncode/guard/count, receipt hash/run/slot/identity/launch mismatch를 고정한다.
- [x] 모든 reason·cleanup/guard/ownership-probe false·stream overflow·누락 receipt를 거부한다.
  source-proof/비local 실행을 승인하지 않으며 skipped/xfail/xpass는 기존 validator로 거부한다.
- [x] 최소 pure 구현 후 focused GREEN, raw/command/exit/guard 기록. unknown exceptions를
  raw 입력 문자열로 내보내지 않는다. 성공도 native/CI/productionfalse를 유지한다.
- [x] 두 허용 파일만 local commit하고 독립 spec+quality review를 받는다.

Task1 최종 저자 `b2cca2f`는 중요 지적 R1~R4 보완 후 `APPROVE_SCOPE`다. coordinator의
새73+기존36 집중109 passed/0.52s/exit0/격리0을 확인하고 feature `e08019b`까지 통합했다.
이 부품 승인은 Task2/3·전체 UTC/KST 완료를 뜻하지 않는다.

## Task 2: 고정 bootstrap과 CLI-only OS owner

**Owner:** Astra/high, 중요 프로세스 수명/격리 경계. Budget65분,15분마다 구체 진행 보고.
**Files:** Create `scripts/dev/pytest_evidence_bootstrap.py`, `scripts/dev/pytest_evidence_controller.py`,
`tests/dev/test_pytest_evidence_controller.py`만. Task1 module을 수정하거나 작성하지 않는다.
**Interfaces:** 두 script의 `main(argv: list[str] | None=None) -> int`와 spec의 고정 CLI.
controller 모듈 import는 signal/subreaper/spawn 효과0이다. 공용 guard 설치/관측은 bootstrap의
private 함수 하나를 parent/child가 함께 사용하고 stdlib import 외 side effect를 만들지 않는다.
controller는 Task1 consumer를 호출하지 않고 관측 문서를 생산한다(독립 생산/검증 경계).

- [x] 작은 합성 저장소에 기존 producer와 **정확한 guard bytes**를 넣는 test harness를 작성한다.
  fixture pytest case는 하드코딩된 이름/노드이며 외부API/원래source/native oracle은 호출하지 않는다.
- [x] 실제 RED 전에 독립 `pytest→harness→dummy controller`에서 harness 자식0/subreaper/
  pidfd와 finally 회수를 검증한다. case12초+cleanup3초, outer20초/16초 TERM 후4초 보장,
  모든 종료경로 최종 raw __WALL ECHILD, 누락/사망/초과 시 batch 중단을 spec대로 고정한다.
  pytest parent를 subreaper로 만들거나 outer timeout이 harness를 먼저 KILL하지 않는다.
- [x] missing controller RED 및 정상0/실패1, 출력 후 exit9/signal/atexit hang, guard-ready 누락·
  중복·잘못된 hash, overwrite/symlink/outside-root, selection/환경 옵션 거부를 고정한다.
- [x] Linux preflight(single OS task, non-consuming initial ECHILD, SIGCHLD reset,
  subreaper set/get, self pidfd signal0) 및 fork→exit23 waitability probe를 구현한다.
  모든 guard/dependency import를 최종 single-task/empty-child 검사 전에 완료한다.
  probe 실패면 bootstrap0, source/runtime 자격으로 해석하지 않는다.
- [x] pidfd-open→child-only wait→zero일 때만 fd signal, descriptor finally-close와 raw status
  단일 recorder를 구현한다. Popen implicit reaping/signal 및 PID/PGID fallback은 금지한다.
- [x] 실제 tiny fork/setsid/double-fork와 후행 zombie 의무를 각각 검사한다. fixture descendant는
  모두 자신의 최대8초 종료장치를 설정하며 별도 전용 harness가 subreaper로 회수한다.
  pytest parent 자체를 subreaper로 바꾸거나 모든 host PID에 signal하지 않는다.
- [x] fake OS adapter의 고정 순서로 proc omission/불완전 cleanup/PID재사용/EINTR/permission
  failure를 검사한다. 실제 host PID 고갈·무한생성·살아남는 자식 생성은 금지한다.
- [x] fair loop(한 turn 각 FD64KiB, 최대64 wait/candidate, proc read64KiB)에서 N초 deadline,
  startup10초, TERM1초/KILL2초 budget, stdout/stderr 각8MiB+1, control4096 상한을 구현한다.
- [x] cleanup 뒤 receipt regular bounded32MiB/hash, parent/child guard/코드 지문 재대조,
  process-result exclusive publication을 구현한다. 원문두로그 최대치는 유지한다.
- [x] parent write-end 즉시 close, leader status+세 pipe drain/EOF+최종 ECHILD를
  cleanup_complete 필수 조건으로 구현한다. 후행 generic zero/nonleader status는
  descendant_survived를 latch하며 cleanup 성공으로 reject를 지우지 않는다.
  pending pipe/ECHILD, 마지막 overflow·추가 frame, proc omission+generic zero를 adapter로 고정한다.
- [x] caller SIGTERM·양 pipe flood·늦은 adoption·누락 status를 검증하고 raw rc와 거부 reason이
  동시에 보존되는지 확인한다. terminal latch는 이후 cleanup 성공으로 지우지 않는다.
- [x] focused GREEN/격리0, local3files commit·보고. 전체/native/API/운영 실행은 하지 않는다.

Task2 최종 `995d59e`는 R1·R2 수정 후 독립 한정 재승인됐다. coordinator 최종64 passed/
24.90s/exit0/격리0 후 `96c4610`/`b99d051`로 통합했다. 실제 zombie의 특정 post-leader
관측 순서를 강제하지 않은 한계와 별도 recorder 대조는 진행 원장에 구분한다.

## Task 3: coordinator 결합 인수·독립 broad review

**Owner:** coordinator integration, 별도 Sol/high writer가 결합 시험 한 파일만 작성할 수 있다.
**Files:** `tests/dev/test_controlled_verification_evidence.py` 새 파일, 관련 문서만.
**Interfaces:** 실제 Task2 CLI가 쓴 raw receipt/process-result를 Task1 evaluate에 넣는다.

- [x] Task1/2의 독립 scoped review를 완료하고 문제는 원 writer에게 수정·재리뷰시킨다.
- [x] 같은 feature에 후보를 통합한 뒤 결합 시험을 RED부터 작성한다: 실제 tiny pytest
  고정 node1·독립 literal run/slot/selector·사전 파일 hash 기대값으로 OS_RESULT_BOUND를 확인한다.
  actual.collected를 expected에 복사하지 않는다. exit 뒤 fault는 valid receipt가 있어도 거부한다.
  이미 두 API가 구현되어 최초 실행이 GREEN이면 특성화라고 기록하며 RED를 조작하지 않는다.
- [x] focused 새3시험+기존 evidence3시험, guard0·exit0·raw logs를 확인한다.
- [x] 전체 변경의 fresh Astra/xhigh broad review와 발견사항 보완·한정 재리뷰를 완료한다.
- [x] root단독 전체 UTC→KST를900초 cap으로 실행한다. 일반 suite의16skip/2xfail은 기존 조건을
  유지하고 새 OS consumer가 이를 승인했다고 보고하지 않는다. source108 call-phase0.
- [x] compile-only·비밀 패턴·diff check, 기존파일/guard/sourcehelperhash 불변 확인 후
  CHANGELOG/CLAUDE/docs index/report에 actual 범위/미지원/원문증거를 기록한다.
- [ ] feature commit/push+remote SHA 확인 뒤 runtime qualification의 순수 admission 경계 또는
  다음 미해결 개발 단계를 실제 최신 원장 기준으로 선택한다. main/운영 전환은 별도 권한이다.

## Self-review

Task3 최종 결합 저자 `a77227f`는 scoped 승인을 받았다. fresh broad의 추가 후행 중단
지적은 `ffcc384`의36사례/최소 latch와 cutoff 명세로 수정하고 같은 broad reviewer의
한정 재리뷰에서 R1/R2 모두 ADDRESSED·새 지적0을 받았다. coordinator 후보 집중349
passed/124.84s/exit0/격리0 후 `26dd5ae`로 통합했다. 전체 UTC/KST·마감은 위 체크에 따른다.

spec 필드는 Task1, 관측은 Task2, 양방향 실제 결속은 Task3가 소유한다. 구체 프로세스 신호는
소유한 test child만 대상으로 하며 이를 운영 승인으로 확장하지 않는다. OS 관측의 이벤트 루프
예산과 전체 syscall 선점 보증을 구분한다. native runtime·원격 trust·후속 skip 정책은 미해결이다.
Task2 테스트 harness의 독립 bounded cleanup까지 중요 검토 범위다. 독립 계획 재리뷰는 승인됐으며
현재 부품별 결과와 전체 단계 완료는 위 체크리스트 및 OS 진행 원장으로 구분한다.
