# Decoder B1a Primitive Structural Probe Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> or superpowers:executing-plans to implement this plan task-by-task.
> 현재 문서 재검토만 허용한다. 아래 checkbox 모두 미착수, 코드 작성/수집/실험0.

**Goal:** EMPTY cell allocation/chain/holder/unlink만 독립 actual-slot oracle로 검증할
B1a 부분 Do를 준비한다. full B1/N4097 인수 완료가 아니다.

**Architecture:** 동결 전이표를 subject와 별도 observer 작성자가 각각 구현한다.
작은 literal fixture와 actual mutants만 guarded standard profile로 검증한다.
full semantic/CONT/retire/dispose와 N4097은 별도 승인 gate에 남긴다.

**Tech Stack:** 기존 Python/pytest·stdlib weakref/dataclasses/typing, 새 설치0.

**Spec:** `docs/superpowers/specs/2026-09-27-decoder-b1-structural-probe-design.md` 전체.

**Status:** `B1A_PARTIAL_PLAN_PROPOSED`, 같은 독립 reviewer 재검토 대기.
원525줄은 `d9d57e533582b9d2183a145c14f31c2fea313b2b`에 보존한다.
R1–R4 보완 제안이며 R2 full CLOSED/full S GO를 주장하지 않는다.

## Global Constraints

- `TEST_ORACLE_DESIGN_UNRESOLVED / RED_DEFERRED`, `native_qualified=false`,
  `source_execution_permitted=false`, qualified runtime0, source108 call-phase0 유지.
- 신규 `tests/structural_b1/{subject,observer,fixtures,mutants,test_structure}.py` 다섯 파일만.
  기존 파일/src/helper/proofs/guard/CI/config/registry 수정0, frozen 후보 복사·import0.
- SQL/native/제품 import/외부 API/SSH/deploy/restart/install/자격·운영 상태 접근0.
- exact layout/action/모든 중간 전이/fault/observer 책임은 spec §2–5로 동결한다.
- 256facts 또는5ms/raw50ms/restore1000ms/ticket1500ms와 cold/cleanup timer 불변.
  기존 두 성능 예외를 새 실패/timeout에 적용하지 않는다.
- coordinator만 통합; root+최대3workers, 고유 worktree·비중복 파일, fanout0/fallback0.
  requested 모델/effort와 actual metadata 미노출=unverified를 구분한다.
- 독립 리뷰·전체 standard UTC/KST 실제 성공 전 코드 통합0. 성공의 최대 문구는
  `B1A_PRIMITIVES_OBSERVED_ONLY`; full S/native/cold 인수0.

## Review Focus

1. 최초 생성/new 인계 동시 발생 — Task2 정상 A0/allocate_and_link 대조.
2. 순변화로 실제 쓰기 수를 과장 — Task3 double_write_restore source 감사.
3. unlink 중간 holder 유실 — Task2/3 모든 U행 전/후 fault·exact aliases.
4. wrapper/probe local·외부 alias 오판 — Task2/3 frame 반환·actual alias 대조.
5. observer/coverage 오류와 missing large를 PASS 처리 — Task2 한계 판정·§5 후속 gate.

## 0. 승인·소유권과 미래 API

- [ ] 같은 독립 Astra/xhigh reviewer가 R1–R4와 두 문서를 재검토한다.
  요청 최대 승인명=`APPROVE_B1A_PLAN_ONLY`; 현재 승인받았다는 뜻이 아니다.
- [ ] coordinator가 승인 spec/plan SHA·파일·node 목록을 고정해 dispatch한다.
  subject=Astra/high 25분, 다른 observer 작성자=Astra/high 30분,
  독립 critical reviewer=Astra/xhigh 20분, 각 fanout0/fallback0.
  소유권 전이·관측 독립성이 critical routing 근거다. author 승인0.
- [ ] 고유 feature worktree·단일 시험 workload slot을 사용한다. scaffold→observer→
  subject 완성→mutant site 보완은 순차이며 앞 단계 검토 commit이 다음 base다.
  사용자 복귀 전 자율 범위 안에서 진행하되 시간 부족은 미완으로 기록한다.

```python
# subject.py
Action = tuple[str, None]
def make_supervisor(token: tuple[int, int, int, int]) -> Supervisor: ...
def submit(supervisor: Supervisor, operation: Operation,
           token: tuple[int, int, int, int], action: Action) -> None: ...
def tick(supervisor: Supervisor, operation: Operation,
         token: tuple[int, int, int, int]) -> None: ...
def _step(operation: Operation) -> None: ...  # spec 표 한 행+phase 갱신
def _new_cell() -> Cell: ...                 # 유일 constructor, EMPTY/ref None

# observer.py
class ObserverAbort(BaseException): ...
def install_census(monkeypatch, subject_module) -> Census: ...
def snapshot(supervisor: object, census: Census) -> Snapshot: ...
def check_transition(before: Snapshot, after: Snapshot) -> tuple[str, ...]: ...
def terminal_live(census: Census) -> tuple[int, ...]: ...

# fixtures.py — subject helper를 사용하지 않는 literal expected
def drive(supervisor: object, operation: object, token: tuple[int, int, int, int],
          actions: tuple[Action, ...], census: object) -> tuple[str, ...]: ...

# mutants.py
def install_mutant(monkeypatch, subject_module, name: str,
                   external_holder: object) -> None: ...
```

`_step`은 private tick 내부 한 행 경계뿐이다. 정상 callback/await/재귀0.
test wrapper가 행 전/후 원 exception을 raise해 spec F를 검사한다. invalid submit은
검증 종료 전 action/phase를 바꾸지 않는다. fault 이후 mutation0.
driver는 원 operation과 매 tick 전후 snapshot을 비교하며 최대1000tick 초과=INCONCLUSIVE.
driver는 첫 구조 violation의 고정 scalar code를 반환하며 cell/traceback을 반환하지 않는다.
최초 RED 시험은 반환 뒤 actual head 존재를 먼저 `STRUCTURE_MISSING_CHAIN`으로 단언하고
그 뒤 violations==()를 단언한다. scaffold의 잘못된 phase도 missing-chain RED를 가리지 않는다.
phase는 반복 힌트이며 성공 oracle가 아니다. Census는 weakref/scalar만, Snapshot은 scalar
generation/tag/slot-target/phase/outcome만 반환한다. 정상 fixture cell handle0.
외부 mutant holder는 fixed slot value 하나다. 원 callable/slot은 monkeypatch가 복구한다.

새 __init__.py0. test_structure의 고정 sibling loader가 __file__ 옆 subject→observer→fixtures→
mutants를 importlib.util로 읽고 exec 전 sys.modules에 `_qwq_b1_subject`, `_qwq_b1_observer`,
`_qwq_b1_fixtures`, `_qwq_b1_mutants`로 등록한다. 기존 tests namespace/임의 경로 의존0.

## 1. 실제 guarded standard prefix — 후속 승인 뒤에만 사용

coordinator-validation-ledger의 실제 prefix를 아래처럼 고정한다. 지금 실행하지 않는다.
root가 candidate cwd와 각 raw log 절대 경로를 dispatch에 기록한다. env-i로
QWQ_VERIFY_SKIP_TESTS/PYTEST_ADDOPTS/운영 HOME·자격을 상속하지 않는다.
verify.sh 단독은 profile 대체물이 아니며 전체 시험도 아래 pytest 직접 호출을 사용한다.

```bash
# 호출 인자: TZ cap log mutant selector...; 정상 mutant=none
run_b1_standard() {
  local b1_tz="$1" b1_cap="$2" b1_log="$3" b1_mutant="$4"
  shift 4
  local -a b1_rc
  set -o pipefail
  flock -w 240 /home/ubuntu/projects/qwq-ai-trader/.claude/worktrees/owner-ticket-gate-20260926/.superpowers/sdd/2026-09-27-runtime-admission-contract/test-workload.lock \
    timeout --signal=TERM "${b1_cap}s" env -i PATH=/usr/bin:/bin LANG=C.UTF-8 TZ="$b1_tz" \
    PYTHONDONTWRITEBYTECODE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
    QWQ_B1_RAW_MUTANT="$b1_mutant" \
    QWQ_DEPLOY_SSH_KEY=/tmp/qwq-verification-XQNQxF/nonexistent-key \
    /home/ubuntu/projects/qwq-ai-trader/venv/bin/python -m pytest "$@" -x -q \
    -p no:cacheprovider -p pytest_asyncio.plugin -p pytest_cov.plugin \
    -p anyio.pytest_plugin --tb=short 2>&1 | tee "$b1_log"
  b1_rc=("${PIPESTATUS[@]}")
  printf 'raw_workload_rc=%s raw_tee_rc=%s\n' "${b1_rc[0]}" "${b1_rc[1]}"
  if (( b1_rc[0] != 0 )); then return "${b1_rc[0]}"; fi
  return "${b1_rc[1]}"
}
```

outer errexit가 PIPESTATUS 수집을 건너뛰지 않는 shell에서 호출하고 완료 도구 실제 exit_code도
보존한다. raw/log 저장 실패도 실패다. lock 실패/timeout124/신호 종료를 pytest 성공으로 바꾸지
않는다. 가짜 SSH fixture key 경로는 실행 전 비존재만 확인하며 실제 키/SSH 호출0.
전제가 달라지면 중단한다. expected RED rc1은 root가 call-phase의 의도한 assertion을 확인해
기록한 뒤 다음 단계로 넘어가는 명시 gate다. 다른 새 실패/수집/import/observer 오류면 중단.
focused/full은 -x 첫 실패 종료, 모두 직렬·nonzero 후 자동 후속/재시도0.

| 단계 | TZ / wall cap | selector·기대 |
| --- | --- | --- |
| 최초 RED | UTC /180s | `tests/structural_b1/test_structure.py::test_append_requires_actual_chain`, 실제 assertion rc1 |
| small 집중 | UTC /180s | `tests/structural_b1/test_structure.py`, 전체 rc0 |
| standard 전체 | UTC /900s 뒤 Asia/Seoul /900s | `tests`, 각각 raw/tool exit0·격리0 |

기존 source discovery 제외 유지, 새 skip/deselect/-k0, source108 명시 수집0.
syntax는 승인 다섯 파일의 py_compile만 기존 Python으로 확인하고 bytecode는 task 전용 임시
prefix에 둔다. 시험 후 비밀 패턴·불변 diff 확인. 환경/lock/plugin/guard 변경0.
이 prefix는 **2MiB/stream cap을 제공하지 않는다**. small 표준용이며 large에는 사용할 수 없다.
root 기존 prefix 대비 추가 환경 한 개는 비밀정보 없는 고정 raw mutant 선택뿐이며 새로운
runner/controller나 guard 우회가 아니다. 일반 focused/full에서는 항상 none을 전달한다.

## Task 1: 최소 import 가능한 empty shell scaffold

**Files:** Create `tests/structural_b1/subject.py`만. **Interfaces:** §0, spec §2.

- [ ] exact layout·shell·identity/사전 거부와 EMPTY constructor를 만든다. 임시 scaffold
  `_step`은 graph 대입 없이 action=None/phase=IDLE로만 돌아온다. 다음 실제 RED용 입력이며
  완성 구현/성공 증거가 아니다.
- [ ] root가 실제 slots/constructor site/import closure를 정적으로 검토하고 syntax만 확인한다.
  시험/제품 import0. 허용 파일만 scaffold commit하고 observer의 다음 base로 전달한다.

## Task 2: 독립 oracle·literal expected·첫 구조 RED

**Files:** Create observer.py/fixtures.py/mutants.py/test_structure.py. subject 수정0.
**Interfaces:** spec §3 표 동결본·Task1 타입/slots. 별도 작성자가 expected를 작성한다.

- [ ] `test_append_requires_actual_chain`: append_empty 뒤 head=tail=g1, EMPTY·prev/next=None,
  모든 holder None·constructor 반환1을 literal로 요구한다. scaffold는 call-phase의
  `STRUCTURE_MISSING_CHAIN` assertion으로 실패해야 한다. import 오류는 RED가 아니다.
- [ ] root가 §1 최초 exact node를 실행하여 raw rc1·해당 assertion을 보존한다.
  `test_allocate_to_new_exact_transition`은 정상 A0 generation1/new1/기타 delta0를 고정한다.
- [ ] `test_append_three_literal_transitions`, `test_unlink_positions_literal_transitions`에
  append1/2/3과 single/head/tail/middle의 모든 A/U행 exact generation/slot/phase를 고정한다.
  None→None은 delta0/실제 source 쓰기1임을 구분한다.
- [ ] `test_foreign_fault_preserves_each_boundary`는 모든 A/U행 전/후를 별도 작은 fixture로
  재구축하여 원 exception identity·현재 phase/graph/holder·후속 mutation0을 검사한다.
  fault는 정상 terminal을 기대하지 않으며 A0 내부 gap은 제외한다.
- [ ] `test_action_rejections_are_unchanged`는 malformed/unknown/busy/빈 unlink/wrong middle/
  identity·fault 이후 요청을 포함한다. constructor0·정확한 before=after를 비교한다.
- [ ] `test_terminal_after_frames_return`, `test_observer_failure_is_inconclusive`,
  `test_allocator_bypass_is_not_qualified`에 실제 alias/census 실패/allocator bypass를 적용한다.
  driver/wrapper/snapshot frame 종료·관측 한계 분류를 검증한다.
- [ ] spec §5 names의 `test_actual_mutants_rejected`와
  `test_net_delta_does_not_prove_write_count`를 작성한다. 후자는 double_write_restore의
  snapshot 한계를 확인하며 structural kill 수에 넣지 않는다.
- [ ] root/reviewer가 expected 독립성을 검토한 뒤 Task3로 간다. 없는 mutant site는 미결로
  남겨 실제 코드 뒤 연결하며 large node/skip을 만들지 않는다.

## Task 3: 전이 구현·actual mutants·source-write 검토

**Files:** subject 담당은 subject.py만. observer 담당은 별도 직렬 단계에서 mutants.py만.
**Interfaces:** 동일 spec/API/expected. GREEN을 위해 expected/test 완화0.

- [ ] A0–A6/U0–U11을 구현한다. A0만 복합 관측, 다른 행은 분기별 단일 실제 graph 대입이다.
  사전 거부와 원 fault/phase/graph 보존을 구현한다. full decoder action0.
- [ ] 최초 exact RED node를 같은 profile로 GREEN 확인하고 작은 module을 실행한다.
  독립 reviewer가 각 분기 쓰기 지점/constructor0·1을 SHA·행 표로 대조한다.
  double_write_restore는 source 감사로 거부하며 snapshot 증명으로 보고하지 않는다. tracing0.
- [ ] observer 담당이 실제 mutant site를 연결한다. 정상 predicate PASS와 같은 predicate의
  actual mutant 구조 실패를 한 쌍으로 남긴다. 원시 RED는 아래 고정 one-shot 절차로 보존한다.
  pytest.raises 포장 PASS만 raw RED라 하지 않는다. observer/coverage miss는 별도 분류다.
- [ ] reviewer가 모든 표 행/분기·foreign fault·frame 반환·hidden alias·actual terminal과
  source closure를 확인한다. diff/site/SHA/raw node/rc/unresolved를 root에 반환한다.

actual mutant 원시 절차: test_structure에 `test_raw_mutant_probe`를 두되 평상시에는 정상
subject의 literal predicate를 실행한다(항상 수집, skip0). 그 시험만 os.environ에서 고정
`QWQ_B1_RAW_MUTANT`를 읽으며 prefix가 정상에는 `none`, raw에는 아래 literal name을 전달한다.
허용되지 않은 값은 harness 오류이며 RED가 아니다. name으로 경로/import/callable을 고르지 않고
literal 분기에서 `install_mutant`를 호출한다. inherited 환경은 env-i로 제거한다.

| raw name | 고정 작은 action fixture / 정상 predicate |
| --- | --- |
| allocate_and_link,skip_chain,clear_new_before_cursor | append_empty1 / 모든 A행·chain exact |
| unlink_single | append1→unlink_head / 모든 U행·terminal |
| unlink_head | append3→unlink_head→unlink_head→unlink_head / 모든 U행·terminal |
| unlink_tail | append3→unlink_tail→unlink_head→unlink_head / 모든 U행·terminal |
| unlink_middle,clear_unlink_early,clear_cursor_early | append3→unlink_middle→unlink_head→unlink_head / 모든 U행·terminal |
| hidden_leaf,exception_alias,closure_alias | append1→unlink_head / frame 종료 후 terminal_live=() |

raw 실행은 §1 함수의 mutant 인자만 해당 name으로 고정하고 selector는 언제나
`tests/structural_b1/test_structure.py::test_raw_mutant_probe`, UTC/180초다.
예: `run_b1_standard UTC 180 /TASK_ARTIFACT/allocate-and-link.log allocate_and_link tests/structural_b1/test_structure.py::test_raw_mutant_probe`.
실행 전 `/TASK_ARTIFACT`를 승인된 실제 절대 artifact 경로로 치환한다. 각 name1회 expected rc1과
의도한 slot/terminal assertion을 확인한 뒤 다음 case로 간다. 모든 raw name의 정상 대조도 같은
fixture와 predicate를 `test_actual_mutants_rejected`에서 검사한다. identity 사전 거부는
positive 방어 대조, double_write_restore는 source 한계 대조이며 이 rc1 목록에 넣지 않는다.
독립 리뷰어가 고정 입력값/명령과 raw 증거를 확인하기 전 변이 인수 완료0이다.

## Task 4: B1a 한정 See·전체 standard 검증

**Files:** 새 코드0; root의 별도 원장 allowlist만 갱신. author 승인0.

- [ ] root가 다섯 파일/금지 import0/전이 source/actual mutant를 대조하고 새 parameterized node
  literal inventory와 standard collection을 §1 profile로 비교한다. missing large는 미구현이며
  skip/xfail/deselect로 전체를 통과시키지 않았는지 확인한다.
- [ ] final 작은 module1회 → 독립 critical 승인 → UTC 전체1회 → KST 전체1회 직렬 검증한다.
  raw/tool exit0·격리0·source108 call-phase0·syntax/비밀 패턴/불변 diff를 모두 요구한다.
  새 실패/timeout이면 통합0. 이전 full 기록은 새 후보 PASS를 대신하지 않는다.
- [ ] 실제 성공한 경우에만 `B1A_PRIMITIVES_OBSERVED_ONLY`로 허용 파일을 개발 통합한다.
  full B1/R2/large/S/첫 cold RED/native는 미완으로 명시한다.

## 5. full B1/N4097 후속 gate — 현재 미승인·UNRUN

- [ ] CONT/end/lookup/publish/retire/finish/known_failure/dispose의 모든 중간 전이를 spec §3
  형식으로 동결하고 원 B1 중첩 a/b/key/tag/nonfinite/partial disposal/WORK 변이 인수를 유지한
  별도 계획을 독립 승인받는다. B1a 표는 full 계약의 대체물이 아니다.
- [ ] stdout/stderr 각각2MiB 강제·보존을 제공하는 **기존 승인 standard runner**의 정확한
  경로/기능/node 선택/guard profile를 확보한다. 현재 없음=UNRUN. OS controller8MiB,
  exec token 절단/tee는 대체 불가. 새 runner 개발/설치·한도 변경은 이번 범위 밖이다.
- [ ] N4097 전수 oracle의 O(N²) 비용과 dedicated1+UTC full1+KST full1=총3회를 별도 고정한다.
  추가 focused-module large 반복0. 승인된 후속에서 정상 default node로 추가하며 skip/deselect0.
- [ ] dedicated 전수 관측을 cap900 아래 단회 실행할 별도 GO를 얻는다.900초 이내만으로 전체를
  시작하지 않는다. 실제 신규 small/기존 suite 비용·dedicated 비용·양 시간대 여유의 양립을
  reviewer가 검토한 뒤 전체 진입한다. 그 뒤에도 전체 각각 actual cap900 내 성공이 필요하다.
  부족하면 UNRUN/INCONCLUSIVE, 재시도/표본화/N축소/oracle완화/기존 예외 확대0.

참고 root 제공18a2196 UTC6329/641.35s·KST6329/643.67s, 각각16skip/2xfail/4warnings·격리0·
실제 exit0. 이 개정에서 재실행하지 않았다. 산술 잔여258.65/256.33초는 새 후보 예산 보증이 아니다.

## 문서 자체 종료 조건

두 문서 diff·전이/API/spec coverage·공백·비밀 패턴·allowlist를 확인해 local commit만 한다.
현재 pytest/수집/import/SQL/native/실험0·push0. 같은 독립 재승인 전 Task1 코드 작성0.
R1/B1a R2/R3 profile·UNRUN/R4 분리를 재검토에 넘기며 full R2는 미완으로 유지한다.
