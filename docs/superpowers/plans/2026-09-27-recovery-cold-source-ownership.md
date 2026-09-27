# Cold Source Ownership First-RED Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> (recommended) or superpowers:executing-plans to implement this plan task-by-task.
> Steps use checkbox (`- [ ]`) syntax for tracking. This plan ends at first RED; no GREEN implementation.

**Goal:** cold `_open` JSON 할당에 선등록 소유자가 없다는 현재 반례를 안전한 환경에서 한 번
관측할 첫 RED를 고정하고, factory/mixed-snapshot/취소/오류 인수의 다음 범위를 분리한다.

**Architecture:** 제품과 source helper를 수정하지 않는 test-only 관측으로 실제 cold store 경로의
최초 JSON 호출 직전 책임을 기록한다. native 자격·harness gate가 열린 뒤만 실행하며,
관측 결과를 새 managed source 구현이나 lifetime PASS로 승격하지 않는다.

**Tech Stack:** 현행 Python/SQLite/asyncio/pytest. 새 dependency0, native oracle은 별도 qualified 환경 한정.

**Spec:** `docs/superpowers/specs/2026-09-27-recovery-cold-source-ownership-design.md` 전체.

**Status:** **PLAN_REVIEW_PENDING_ONLY**. 문서2개만 작성했으며 아래 모든 실행 checkbox는 미착수다.
사용자 자율 진행 지시는 순수 설계 선행을 선택한 근거다. native/safe-harness gate를 충족하지 않았다.

## Global Constraints

- 기준 `59fa111c6ff56f09a7a69c91a6c9819d7ca02468`; 현재 feature/recovery-cold-source-design-20260927.
  실제 실행은 독립 검토된 docs commit과 실행 subject SHA를 coordinator가 새로 고정한 별도 worktree다.
- 현재 허용 쓰기는 spec/plan2개와 지정 보고서뿐. src/tests/기존 문서·CI·config 수정0, SQL/native/시험 실행0.
- P3/P4 완료와 관계없이 현재 qualified runtime0/source108 call-phase0,
  native_qualified=false/source_execution_permitted=false. host3.12.3 폴백0.
- legacy `_open/_load/commit`·dict/exception 의미 유지. 새 public/prototype API·decoder·consumer·제품 구현0.
- 원 Task6의 store 무변경 전제는 spec의 명시 재설계 제안 대상이며 기존 계획 달성을 주장하지 않는다.
- 13.023871ms/33.174ms 두 개발 예외는 timeout·수명·정합성·새 성능 실패를 면제하지 않는다.
- GC 변경/영구 정상 pin/추가 worker 해제 이관/큰 tuple facade/history pruning/검증 삭제0.
- 성공 restore1000ms/ticket1500ms/raw50ms/256facts 또는5ms 유지. 이번 RED는 latency 측정이 아니다.
- root+최대3workers, fanout0. 구현 필요 시 Astra/high, 독립 critical 검토 Astra/xhigh, 각45분.
  requested와 실제 metadata를 구분하고 실제 모델 미노출은 미검증이다.

## Review Focus

1. cold 검증 이전 warmup이 첫 미소유 할당을 가림 — Task1의 실제 새 store와 connection 없음 검증.
2. 계측기가 graph/예외를 pin해 수명을 바꿈 — Task1의 scalar-only 관측·비수명 증거 한정.
3. runtime 거부/import error를 의도한 RED로 오인 — Task1의 정확한 도달 사건/AssertionError 요구.
4. factory의 대사 graph를 source 소유라고 자동 간주 — Task2 반례 P2의 별도 saved/encode 소유자.
5. Future done을 cleanup으로 간주해 gate 반환 — Task2 P4/P5의 실제 gate drain·foreign error 증거.

## 실행 전 gate — 모두 열리기 전 Task1 파일 작성/실행 금지

- [ ] 작성자와 다른 Astra/xhigh가 spec와 본 계획을 함께 검토하고 중요 지적0/처분을 기록한다.
  범위 한정 판정은 문서 승인이지 제품 실행 허가가 아니다. coordinator만 후속 dispatch한다.
- [ ] P3/P4 actual 결과·허용 scope를 확인한다. 순수 CONTRACT_MATCH를 실행권으로 해석하지 않는다.
- [ ] 별도 capsule/bootstrap 계획 아래 exact source/build/patch/lock/loaded closure/profile·독립
  native 검토·신뢰되는 선행 등록 revision 및 이 **cold 관측 시험 subject**의 실행 자격을 확보한다.
  warm helper 자격이 생겨도 새 cold observer에 자동 상속하지 않는다. 이번은 bootstrap 실행계획이 아니다.
- [ ] 선행 warm source/sequence 인수의 미완료 조건을 coordinator가 근거별로 처분한다.
  두 성능 예외만으로 native/전체 회귀/수명 인수를 완료로 표시하지 않는다.
- [ ] 새 safe harness 계획에서 network-off, source/runtime read-only, private tmp, credentials/home
  미노출, 제품 import 전 exact guard, clean env, 실제 위반0, 제한된 child 종료/회수를 확인한다.
  현 host 직접 SQLite/native introspection, ctypes import, guard 우회, source setup 차단 제거 금지.
- [ ] coordinator가 exact 실행 SHA·새 파일 SHA·expected node 하나·guard/controller/capsule 지문,
  stdout/stderr≤2MiB 각각·receipt≤64KiB, wall180초·TERM/KILL 각1초·재시도0을 사전 등록한다.
  이런 제어를 지원하는 실행기를 먼저 검토해야 하며 P3의 standard 전용 실행 권한을 재사용하지 않는다.
- [ ] coordinator의 단일 workload slot 확보. 첫 node 외 다른 시험/성능/전체 UTC/KST 실행 권한0.

gate가 하나라도 없으면 **BLOCKED_BEFORE_EXECUTION**이며 source 시험 수집/SQL/프로세스 실행을
시작하지 않는다. 실행권은 이 계획의 Python 명령문이 아니라 위 별도 검토·증거에서 나온다.

## Task 1: 첫 cold 검증 소유권 RED 한 건

**향후 파일 allowlist 제안:** 새 `tests/proofs/l3_cold_source/cold_source_cases.py` 하나만.
기본 `test_*.py` 이름을 쓰지 않고 기존 proof/guard/제품/수집 설정은 무변경.
실제 허용은 위 gate 이후 별도 dispatch에 고정한다. 지금 이 파일을 만들지 않는다.

**Interfaces:** 기존 `ExecutionStateStore(Path)`, `await store.load()`, `await store.close()`를 소비한다.
새 source API/helper를 만들지 않는다. 결과는 bounded scalar 관측(recorded code location,
native thread id, operation 존재/등록 여부, checkpoint revision, reached flag)과 실제 pytest failure뿐이다.
실제 graph/row/exception/traceback/frame 객체를 관측 로그나 저장 배열에 보관하지 않는다.

- [ ] **준비:** 임시 디렉터리의 synthetic schema1 DB에 version1과 유한 dict checkpoint를 별도
  fixture 준비 단계에서 기록하고 그 준비용 store/connection을 종료한다. unknown nested list와
  정책 등록 사실을 포함하되 실제 계좌 자료0. 새 `ExecutionStateStore`의 `_connection is None`을
  확인한다. fixture 준비와 대상 cold 경로를 구분하며 대상 store의 `_open/load/commit` warmup0.
- [ ] **관측:** test-scoped observer가 실제 `store._open`의 `json.loads` 호출 직전에 scalar 사건을
  기록한다. 현 함수/global/local 값은 읽기만 하고 native refcount/layout 관측을 수행하지 않는다.
  observer는 결과에 operation을 사후 붙이거나 graph를 포장하지 않는다. 시험 자체의 보관/관측
  책임은 제품의 allocation-time owner가 아니다. 기존코드에서 operation 부재를 None/false로 기록한다.
- [ ] **단언:** literal node 이름은
  `test_cold_validation_allocations_require_preregistered_operation`.
  실제 cold JSON 호출 도달≥1·W 스레드·준비 store와 다른 target instance를 먼저 확인한 후,
  첫 호출 시 `operation_registered_before_allocation is True`를 요구한다.
  현 기준 코드의 예상 RED는 **이 단언의 False 대 True AssertionError 한 건**이다.
  `_open` 호출 횟수나 테스트가 만든 counter만으로 판정하지 않는다.
- [ ] **정리:** observer 복구와 store.close를 finally에서 수행한다. close/finalizer/guard/controller
  문제가 있으면 의도된 RED와 별개 실패로 보존한다. 현행 비관리 graph의 실행은 이 단계에서만
  위 safe harness가 책임지며 정상 bounded lifetime을 증명한 것으로 보고하지 않는다.
- [ ] **실행:** 아래 exact payload argv를 qualified capsule 내부 작업 root에서 UTC 한 번만 실행한다.
  `/capsule/bin/python`은 별도 gate가 실제 검증 artifact의 interpreter로 결속할 mount 경로다.
  지금 존재/실행 가능하다고 주장하지 않으며 host venv로 대체하지 않는다. wrapper는 위 예산/증거와
  이 argv 전체를 결속해야 한다. 변수에 executable을 넣거나 사용자 임의 command를 받지 않는다.

```bash
env -i PATH=/usr/bin:/bin LANG=C.UTF-8 TZ=UTC PYTHONDONTWRITEBYTECODE=1 \
  PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 /capsule/bin/python -m pytest \
  tests/proofs/l3_cold_source/cold_source_cases.py::test_cold_validation_allocations_require_preregistered_operation \
  -q -p no:cacheprovider --tb=short --show-capture=no
```

- [ ] **See:** pytest rc1 + 위 정확한 call-phase assertion/reached 사건 + teardown/실제 격리0 +
  controller 정상 종료·회수 증거를 함께 기록한다. collection/import/runtime 거부·timeout·crash·
  다른 assertion·정리 실패는 intended RED가 아니라 INCONCLUSIVE/FAILED_HARNESS다.
  예상과 달리 PASS면 현행/observer 전제 오류로 중단한다. 통과값 선택 재시도·GREEN 수정0.
- [ ] 정확한 artifact/source/test SHA와 원시 rc를 독립 reviewer가 대조한 뒤 종료한다.
  `FIRST_COLD_RED_OBSERVED_ONLY`도 native/lifetime/managed 구현 PASS가 아니다.

## Task 2: 다음 반례의 인수 계약만 보존 — 이번 실행0

아래 이름은 후속 검토용 literal node 범위다. Task1 결과 뒤 새 계획 없이 작성·실행하지 않는다.
이 표는 새 prototype API를 명세하지 않으며 지금 제품 코드에 없는 관리형 기능을 import하지 않는다.

| ID·후속 node | 실제 입력/사건과 실패해야 할 현행 계약 | 독립 관측/미완료 gate |
| --- | --- | --- |
| P2 `test_factory_preflight_graph_has_registered_owner_before_restore` | 유효 factory fixture에서 restore 직전 event로 멈춤. factory `state`, `saved`, encode scratch의 생성 전 등록을 요구하면 현행은 없음. 단순 source slot 존재로 PASS 금지. | frame/DTO/alias의 소유권 관측 계획·factory clock/side effects 격리; observer 보유를 lifetime 증거로 쓰지 않음 |
| P3 `test_factory_preflight_and_restore_consume_one_snapshot` | preflight가 V1을 읽은 뒤 별도 합성 WAL writer가 V2 checkpoint+receipts commit. 실제 owner restore가 V2를 재읽으면 동일 capability/revision 요구 실패. 추가 인수는 transaction 종료→WAL 뒤/preflight 도중 V2이면 scalar stale 관측으로 거부·source 재bind0·publish0. | V1/V2 distinguishable values·receipts 양방향 대조, 실제 둘 다 유효 fixture; 시작 owner0/durable7 정상 bind와 epoch stale를 구분. 최종 관측 뒤 비참여 writer 창은 전체 writer enforcement 별도 gate |
| P4 `test_completed_unclaimed_cold_source_retains_ticket_on_repeated_cancel` | SQL 완료/호출자 미수령 지점에서 두 번 취소하고 실제 follower queue/drain 관측. private source/cleanup 책임 없이 Future settled로 반환하면 실패. | 신규 ownership instrumentation·qualified native observer와 controller child protocol 필요; max10초 negative child/의도된 회수≠cleanup |
| P5 `test_cold_validation_error_unwind_cannot_certify_cleanup` | 앞부분 큰 unknown list 이후 malformed JSON, 이어 별도 known validation/foreign graph-bearing 오류 사례. 원 traceback·cause·worker Future 보유가 미인증인데 cleanup을 발행하면 실패. | parser native partial-allocation/exception oracle 별도; foreign fault same slot·writer0·pending proof를 실제 관측 |

P3는 혼합 snapshot을 실제로 만드는 반례이며 끝의 token 검사만으로 정상화하지 못한다.
P4/P5는 현재 제품에 없는 gate/certificate를 시험 mock 성공값으로 채우지 않는다. 선행 source
계약의 실제 배선/관측 seam 설계가 없으면 후속 RED도 미실행이다. 새 child 시작/종료 명령은
별도 승인된 safe-harness 계획에서만 구체화한다.

## 설계 이후에도 남는 단계

allocation-time decoder → preflight/실제 consumer 반납과 tuple·JSON·digest·identity 호환 →
full managed lifecycle → 원 N5 Task4–15 연결·63셀/전체 회귀·독립 broad 인수 순서를 유지한다.
이 계획에는 GREEN 구현·기존 시험 수정·성능 재측정·운영 배선 단계가 없다.
현재 문서 self-review는 §1–8 책임/순서/반례 대응만 확인한다. **PLAN_REVIEW_PENDING_ONLY**로
coordinator에게 넘기며 최종 critical 승인은 작성자와 다른 Astra/xhigh가 수행해야 한다.
