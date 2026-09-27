# Decoder B1 Structural Probe Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> (recommended) or superpowers:executing-plans to implement this plan task-by-task.
> Steps use checkbox (`- [ ]`) syntax for tracking. 현재는 Plan만 허용하며 구현/실험은 독립 검토 뒤다.

**Goal:** 실제 private cell의 생성 반환 이후 연결·중복 교체·retire 관계를 독립 S observer로
검사하는 순수 test-only 구조 실험을 만든다.

**Architecture:** subject의 실제 slot/chain/holder와 observer의 weak census·literal expected를
분리한다. 일반 guarded pytest에서 작은 action과 N=4097을 실행하되 source/native/SQL 경로를
사용하지 않는다. S 성공을 최초 allocation/actual free 또는 cold RED 통과로 사용하지 않는다.

**Tech Stack:** 기존 Python/pytest, stdlib math/weakref/dataclasses/typing. 새 설치0.

**Spec:** `docs/superpowers/specs/2026-09-27-decoder-b1-structural-probe-design.md` 전체.

**Status:** `B1_STRUCTURAL_PLAN_PROPOSED`, 독립 검토 대기. 모든 아래 checkbox 미착수.
문서 작성 기준 `630fc90b973c2fb3fd0e76e946fe8224a57356ce`.

## Global Constraints

- `TEST_ORACLE_DESIGN_UNRESOLVED / RED_DEFERRED`, `native_qualified=false`,
  `source_execution_permitted=false`, qualified runtime0, source108 call-phase0 유지.
- 허용 신규 구현 파일은 `tests/structural_b1/subject.py`, `observer.py`, `fixtures.py`,
  `mutants.py`, `test_structure.py` 다섯 개뿐. 기존 파일 수정0. 현재는 그 파일 작성/실행0.
- `src/`, 기존 tests/helper/proofs, SQL, `json.loads`/marshal/ctypes/native layout,
  frozen source/sequence 후보 import·복사·실행, CI/config/registry 수정0.
- 새 install0, network/API/SSH/deploy/restart0, 자격·운영 상태 접근0, 환경/권한 확장0.
- source108 명시 수집/실행0. 새 구조 시험은 standard 개발 시험이며 native 자격이 아니다.
- exact layout·tag·holder·중간 전이·FAULT·observer frame 책임은 spec §3–6을 그대로 따른다.
- 256facts 또는5ms/raw50ms/restore1000ms/ticket1500ms와 cold/cleanup timer 불변.
  S는 구조 시험이며 새 성능 측정/상한 증명0. 두 기존 성능 예외를 새 실패에 적용하지 않는다.
- coordinator만 통합. root+최대3workers, worker fanout0. 작업자별 모델/effort·실제 metadata
  노출 여부·base SHA·파일 allowlist·시간 상한을 기록한다. 설치/자격이 필요하면 실행0으로 반환한다.

## Review Focus

1. WORK/CONT control link를 semantic child처럼 정리해 무한 work 생성 — Task2/3의 edge ledger와 pop 변이.
2. 중첩 duplicate의 바깥 old를 retire backlog로 오분류 — Task2/3의 outer a/inner b literal domain.
3. head/tail/유일 cell의 중간 실패에 holder 경로 유실 — Task2/3의 각 unlink 전이·fault 주입.
4. wrapper/probe local 보유를 누수 또는 실제 free로 오판 — Task2/3의 frame 경계·hidden alias 대조.
5. observer 실패·constructor 우회·수집 오류를 intended RED/PASS로 오판 — Task2/3의 INCONCLUSIVE 및 call-phase 증거.

## 0. 실행 전 gate와 역할

- [ ] 별도 Astra/xhigh critical reviewer가 spec/plan을 함께 읽고 독립 판정한다.
  `APPROVE_S_STRUCTURAL_PLAN_ONLY`라도 원 cold/native gate는 열리지 않는다.
- [ ] coordinator가 판정·지적 처분을 직접 대조한 뒤 승인 문서 commit을 고정한다.
  단순 이름 변경으로 금지 실험을 실행하는 내용이면 이 단계에서 NO-GO다.
- [ ] coordinator는 현재 base의 별도 feature worktree를 만든다. subject 작성자와 observer
  작성자는 각자의 worktree/비중복 파일을 소유한다. 공유 인터페이스는 아래대로 고정하며,
  후속 task는 앞 task의 검토된 commit을 base로 삼는다. 한 파일 동시 writer0.
- [ ] subject 작성자=Astra/high, 35분 상한; observer/fixture 작성자=다른 Astra/high,
  35분 상한; critical reviewer=작성자 아닌 Astra/xhigh, 20분 상한.
  복잡한 소유 전이와 oracle 독립성 때문에 critical routing을 택한다. 각 dispatch 재위임0,
  fallback0, 상한 도달 시 미완료 반환. root는 시험 명령을 직접 실행한다.
- [ ] root는 기존 진행 작업과 **단일 시험 workload slot**을 조율한다. 이 계획은 18KST 이후
  자동 실행 연장 또는 기존 검증 중단을 허가하지 않는다. 남은 시간 부족은 미실행으로 남긴다.

### 고정 API — 미래 test-only 계약

아래 API는 실제 파일 생성 뒤 signature·소스 행·SHA를 독립 검토한다. 지금 존재한다고 주장하지 않는다.

`subject.py`:

```python
Scalar = bool | int | float | str | None
Action = tuple[str, Scalar]  # length=2, exact tuple; graph/Cell 입력 금지

def make_supervisor(token: tuple[int, int, int, int]) -> Supervisor: ...
def submit(supervisor: Supervisor, operation: Operation,
           token: tuple[int, int, int, int], action: Action) -> None: ...
def tick(supervisor: Supervisor, operation: Operation,
         token: tuple[int, int, int, int]) -> None: ...
def _new_cell() -> Cell: ...  # 유일 constructor boundary, EMPTY·모든 ref None
```

`make_supervisor`는 고정 shell만 생성하며 Cell0이다. `submit`은 작은 action을 하나 보관하고
phase를 바꿀 뿐 Cell0/graph 대입0이다. driver는 bootstrap의 원 operation을 별도 보유해
`supervisor.active is operation`을 확인한다. busy/fault/wrong identity/token이면 graph 변경 전에
작은 ValueError를 내고 constructor0이다. `tick`은 phase에 따라 spec의 단일 구조 대입 또는
constructor 한 번을 실행하고 반환한다. 결과 count/status API는 제공하지 않는다.

허용 action은 `("object", None)`, `("array", None)`, `("key", exact_str)`,
`("null", None)`, `("bool", exact_bool)`, `("int", exact_int)`, `("float", exact_float)`,
`("str", exact_str)`, `("end", None)`, `("finish", None)`, `("dispose", None)`,
`("known_failure", "E_SYNTHETIC")`이다. open container는 CONT를 push, end는 완성 root를
부모의 pending value 또는 arena.root로 이동하고 CONT를 pop한다. object는 key/value 순서,
array는 value 순서다. 중복 key는 첫 ENTRY/KEY 유지·새 key retire다. root는 하나만 허용한다.
action 대기/완료 phase는 `IDLE`, 전체 dispose 끝은 `DISPOSED`, fault는 `DISPOSAL_FAULT`다.
구체 내부 phase 문자열은 spec 전이표 행+대입 순번으로 정의해 observer와 literal로 공유하되
subject의 phase 해석 helper는 공유하지 않는다. 구조에 맞지 않는 phase는 observer가 거부한다.

`observer.py`:

```python
class ObserverAbort(BaseException): ...
def install_census(monkeypatch, subject_module) -> Census: ...
def snapshot(supervisor: object, census: Census) -> Snapshot: ...
def check_transition(before: Snapshot, after: Snapshot) -> tuple[str, ...]: ...
def terminal_live(census: Census) -> tuple[int, ...]: ...
```

Census의 영속 값은 weakref와 monotonic generation, scalar wrapper-entry/return 수뿐이다.
Snapshot은 generation/tag/scalar/slot-target/phase/outcome만 가진다. graph/frame/exception을
반환하지 않는다. `check_transition`의 문자열은 고정 violation code이며 실제 slot 대조에서
생성한다. observer 오류는 ObserverAbort로 kernel의 일반 `except Exception`을 통과하여
driver가 `INCONCLUSIVE_OBSERVER`로 구분한다. subject fault latch로 변환하지 않는다.

`fixtures.py`:

```python
def drive(supervisor: object, token: tuple[int, int, int, int],
          actions: tuple[Action, ...], census: object) -> tuple[str, ...]: ...
def drive_large_duplicate(supervisor: object, token: tuple[int, int, int, int],
                          n: int, census: object) -> tuple[str, ...]: ...
```

driver는 op.phase를 반복 구동의 힌트로만 읽으며 성공 판정에 사용하지 않는다. 매 tick 전후
독립 snapshot과 check_transition을 호출한다. 최대 tick 수는 작은 사례100,000,
N=4097 사례2,000,000으로 harness 무한루프 방지만 한다. 이 상한은 제품 budget/입력 cap이
아니며 초과는 INCONCLUSIVE로 보존한다. 정상 bounded 작업 주장에는 실제 slot delta를 쓴다.
driver의 cell handle 보유0, 원 operation 참조는 고정 shell 참조로 허용한다. 반환은 scalar
violations뿐이며 terminal_live는 driver 반환 뒤 호출한다. supervisor/원 operation은 테스트가
보유하되 dispose 후 모든 graph slot이 비었는지 observer가 대조한다.

`mutants.py`의 고정 진입은 `install_mutant(monkeypatch, subject_module, name: str,
external_holder: object) -> None`이다. 외부 holder는 test 소유 fixed slot `value` 하나다.
실제 변경 위치는 소스 구현 뒤 지정하고 unknown name은 거부한다. 변경 전후 원 callable/slot을
monkeypatch가 복구하며 원 subject 파일을 덮어쓰지 않는다.

## Task 1: 실제 empty layout과 실행 경계의 최소 subject

**작성자:** subject 담당 Astra/high, 10분. **Files:** Create `tests/structural_b1/subject.py`만.
**Interfaces:** 위 subject API와 spec의 exact slots를 제공한다. 완성 decoder를 제공하지 않는다.

- [ ] exact Cell/Supervisor/Operation/Arena/Holders와 `_new_cell`을 만든다.
  아직 tick의 구조 행동은 no-op이며 `IDLE`로 돌아오게 한다. 미구현 status를 성공으로 쓰지 않는다.
  이 최소 scaffold는 다음 task의 실제 slot assertion RED를 위한 입력이다.
- [ ] `make_supervisor`는 Cell을 만들지 않고 원 token identity를 유지한다. submit은
  action type/shape·active/token·busy/fault를 검증한다. no-op tick은 graph를 만들지 않는다.
- [ ] 정적 import/slot 검토와 syntax compile만 수행한다. 시험 없음·S 증거 없음으로 기록한다.
  새 package `__init__.py`는 만들지 않는다. Task2 test module의 loader가 자신의 `__file__`
  sibling 네 파일을 `importlib.util.spec_from_file_location`으로 각각
  `_qwq_b1_subject`, `_qwq_b1_observer`, `_qwq_b1_fixtures`, `_qwq_b1_mutants`로 읽고
  실행 전에 sys.modules에 등록한다. loader 순서는 이 순서이며 외부 파일/임의 path 입력0이다.
  fixture는 고정 `_qwq_b1_subject`/`_qwq_b1_observer`만 참조한다. 기존 tests namespace나
  설치된 동명 tests package에 의존하지 않는다.
- [ ] root가 파일 diff·실제 slots·금지 import0을 확인한 뒤 최소 scaffold commit을
  observer 담당 worktree의 다음 base로 전달한다. 통합 제품 기능 또는 성공 인수로 기록하지 않는다.

## Task 2: 독립 oracle·literal fixture와 실제 구조 RED

**작성자:** subject와 다른 Astra/high, 35분. **Files:** Create `observer.py`, `fixtures.py`,
`mutants.py`, `test_structure.py`. subject 수정0.
**Interfaces:** Task1의 실제 Cell 타입/slots와 위 observer/driver API. kernel helper 재사용0.

- [ ] `test_empty_constructor_census_and_frame_boundary`를 작성한다. 실제 `_new_cell` 반환
  cell을 wrapper에서 census에 등록하고 반환 전/후 holder 관계를 구분한다. wrapper 종료 후
  cell의 weak census를 독립 조회하되 native-free 주장은 없음을 단언한다.
- [ ] `test_literal_build_requires_actual_root`를 먼저 작성한다. `object,key a,int7,end,finish`
  action 뒤 실제 arena.root가 OBJECT이고 ENTRY/KEY/INT slot graph가 literal과 같아야 한다.
  no-op tick은 call-phase에서 `STRUCTURE_MISSING_ROOT` assertion으로 실패해야 한다.
  import error/없는 getattr/observer bool 실패는 RED가 아니다.
- [ ] 아래 exact node를 root의 단일 slot에서 실행하여 원시 rc1·도달·그 assertion을 보존한다.
  Task1은 실제 import 가능한 Cell subject여야 한다. 수집 오류면 INCONCLUSIVE로 수정 보고한다.

```bash
PYTHONDONTWRITEBYTECODE=1 TZ=UTC /home/ubuntu/projects/qwq-ai-trader/venv/bin/python -m pytest \
  tests/structural_b1/test_structure.py::test_literal_build_requires_actual_root -q -p no:cacheprovider --tb=short
```

- [ ] small fixture를 literal로 작성한다. `a=array(3),b=True,a=7`의 최초 key a,b,
  exact tag/value, 중복 key의 별도 generation, old semantic edge 방문1/seed 분리,
  work-pop constructor0을 실제 snapshots와 대조한다.
- [ ] `test_unlink_positions_and_fault_holders`, `test_nested_ancestor_stays_live`,
  `test_scalar_tags_and_overwritten_nonfinite`, `test_known_failure_disposes_partial_graph`를
  작성한다. spec §7의 각 표 행을 parametrization으로 빠짐없이 포함한다.
- [ ] `test_actual_mutants_rejected`의 cases를 literal로 고정한다:
  `owner_missing`, `wrong_operation`, `wrong_token`, `skip_chain`, `late_replacement_holder`, `hidden_tuple`,
  `early_sibling`, `work_next_as_semantic`, `work_pop_allocates`, `unlink_head`,
  `unlink_tail`, `unlink_single`, `hidden_leaf`, `exception_alias`, `closure_alias`.
  첫 세 경우는 실제 constructor0, 나머지는 실제 slot/census/constructor 관계 위반을 요구한다.
  변이의 정확한 메서드 위치는 Task3의 구현 소스가 생긴 뒤 root가 작성자에게 전달한다.
- [ ] `test_observer_failure_is_inconclusive`, `test_allocator_bypass_is_not_qualified`를
  추가한다. 실제 census 기록 중 ObserverAbort와 constructor 우회 동작을 발생시킨다.
  miss는 INCONCLUSIVE_COVERAGE이며 임의 실패 bool을 kernel 결함으로 세지 않는다.
- [ ] N=4097은 `test_large_duplicate_structure` 하나로 고정한다. 모든 tick의 actual slots/census
  oracle를 동일하게 쓰고 정상 terminal까지 검사한다. 큰 action list 사전 생성0.
- [ ] observer·fixture 작성자는 expected 원장과 실제 RED를 root에 전달한다. 독립 reviewer가
  oracle가 subject self-report를 답안으로 읽지 않는지 확인하기 전 Task3의 GREEN 수정을 시작하지 않는다.

## Task 3: subject의 구조 전이 구현과 실제 반례 검출

**작성자:** 원 subject 담당 Astra/high, 35분. **Files:** Modify `subject.py`만.
mutant site 반영은 observer 담당이 **별도 직렬 단계**에서 `mutants.py`만 수정한다.
**Interfaces:** Task2의 고정 API와 literal expected. 기대값 완화/수집 제외로 GREEN 만들기0.

- [ ] 생성/append·CONT push/pop·단계별 lookup을 구현하고 Task2의 첫 RED를 GREEN으로 만든다.
  actual root/entry/key/value 관계가 맞아야 하며 phase==IDLE만으로 통과하지 않는다.
- [ ] duplicate의 old holder→new 게시→child WORK→원 edge clear와 work pop을 spec대로 구현한다.
  head/tail/단일/가운데 unlink는 left/right/unlink holder를 준비하고 한 대입씩 진행한다.
  원 semantic edge당 WORK1, work pop의 추가 allocation0을 observer의 독립 원장으로 확인한다.
- [ ] nested a/b·nonfinite·known failure·foreign failure를 구현한다. fault는 원 예외를 보유하고
  동작을 멈추며 graph 강제 clear/정상 cleanup 인증0. 최종 nonfinite 검사는 전체 live tree를
  한 edge씩 순회하고 덮인 nonfinite를 다시 검사하지 않는다.
- [ ] observer 담당이 실제 method/slot site에 mutant를 연결한다. counter만 바꾸는 변이0.
  각 mutant를 켠 동일 predicate가 call-phase의 의도한 구조 assertion으로 실패하는 원시 증거와
  정상 subject의 통과를 한 쌍으로 남긴다. pytest 내부 `raises` 포장 성공만 RED 원문으로 쓰지 않는다.
- [ ] root가 small subset을 실행한다. 실제 site·SHA·predicate·call-phase rc를 대조한다.
  observer failure/coverage miss/timeout은 intended RED에서 제외한다.

```bash
PYTHONDONTWRITEBYTECODE=1 TZ=UTC /home/ubuntu/projects/qwq-ai-trader/venv/bin/python -m pytest \
  tests/structural_b1/test_structure.py -q -p no:cacheprovider -k 'not large_duplicate_structure' --tb=short
```

- [ ] small subset 승인 뒤 large node를 단회 실행한다. root는 기존 standard 실행 도구에서
  wall900초 상한, stdout/stderr 각각2MiB 상한을 적용한다. 그 상한을 지원하는 승인된 도구가
  없으면 실행 전 멈춘다. 새 controller/native sandbox를 이 작업에 만들지 않는다.
  타임아웃 뒤 같은 값을 얻기 위한 재시도0, UNRUN/INCONCLUSIVE 보존.

```bash
PYTHONDONTWRITEBYTECODE=1 TZ=UTC /home/ubuntu/projects/qwq-ai-trader/venv/bin/python -m pytest \
  tests/structural_b1/test_structure.py::test_large_duplicate_structure -q -p no:cacheprovider --tb=short
```

- [ ] 본문과 hidden alias test는 각 driver/wrapper/snapshot frame 종료 뒤 terminal probe를
  호출했는지 독립 reviewer가 소스로 확인한다. 실제 frame 객체 수집·강제 GC0.
- [ ] 모든 focused 결과와 변이 분류를 source commit/SHA에 묶어 root에 반환한다. S 한정
  조건 충족 여부만 보고하며 source/native/제품 성공은 모두 false로 유지한다.

## Task 4: coordinator See·전체 standard 검증·개발 통합

**담당:** coordinator와 새로운 Astra/xhigh reviewer(20분), 구현자 승인0.
**Files:** 코드 추가0. 진행 원장 갱신은 root의 기존 문서 allowlist 아래 별도 수행한다.

- [ ] root는 candidate diff/import closure를 읽고 다섯 새 파일 외 변경0, source/helper/proofs
  재사용0, shell/SQL/native/외부 호출0, 실제 slots 및 모든 constructor site를 확인한다.
- [ ] 실제 수집을 standard profile로 확인한다. 새 module의 exact parametrized node list를
  사전 literal inventory와 대조한다. 기존 source108 수집 명령은 실행하지 않는다.
- [ ] focused 전체 module을 root가 재실행하고 독립 reviewer가 oracle/actual-mutant/한계
  분류를 본다. 초기 실패/observer 오류/timeout도 원문을 보존한다. 승인 없으면 통합0.
- [ ] root는 한 workload slot에서 **UTC standard 전체 → KST standard 전체 → 비밀 패턴/
  불변 diff → 최종 commit 검토** 순서로 수행한다. 일반 전체 suite는 기존 source discovery
  제외를 그대로 유지한다. source lane/기존 freeze 성능 재실행을 명령에 추가하지 않는다.

```bash
TZ=UTC QWQ_VERIFY_PYTHON=/home/ubuntu/projects/qwq-ai-trader/venv/bin/python bash scripts/dev/verify.sh
TZ=Asia/Seoul QWQ_VERIFY_PYTHON=/home/ubuntu/projects/qwq-ai-trader/venv/bin/python bash scripts/dev/verify.sh
```

명령은 root가 통합 후보 worktree를 cwd로 사용한 뒤 **각각 직렬** 실행한다. verify.sh는
syntax→일반 전체 tests→secret 순서다. 새 secret 검사는 plans 제외 설정에 기대지 않고 새 다섯
파일과 두 문서도 별도 검사한다. 전체에서 source call-phase가 생기거나 격리 위반이 있으면
새 실패이며 승인하지 않는다. 두 과거 성능 예외로 지우지 않는다.

- [ ] 결과가 통과하면 `S_STRUCTURAL_OBSERVED_ONLY`와 실제 SHA/node/rc/한계를 기록한다.
  현재 계획 문서에는 그 결과가 없으며 qualified runtime0/source108 call-phase0 그대로다.
  전체 검증 실패/시간 부족이면 미완료로 남기고 코드 통합을 성공으로 보고하지 않는다.

## 계획 자체의 검증과 현재 종료점

이번 작업은 두 문서의 spec coverage/API 일치/참조/공백/비밀 패턴/allowlist를 확인한 뒤
local commit만 한다. pytest 수집·실험·제품 import·SQL/native 실행0, push0이다.
독립 reviewer의 승인 없이는 이 계획을 실행하지 않는다. 승인 가능한 범위는 ordinary Python
S 구조 실험뿐이며 A의 실제 allocation/free oracle와 원 cold 첫 RED는 여전히 미해결이다.
