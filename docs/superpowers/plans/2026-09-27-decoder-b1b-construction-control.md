# Decoder B1b Construction and Control Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> or superpowers:executing-plans to implement this plan task-by-task.
> Completed within the observed-only scope below; do not repeat finished tasks.

2026-09-27 completion: candidatef3416c3 independently reviewed, actual normal1884/
raw17/combined2082 and full UTC8411/721.87s + KST8411/705.23s verified. Each full
retains16skip/2xfail/4warnings, workload0/tee0/tool0/isolation0. Exact five new
files integrated as236ccb0; all non-Markdown tracked files match the tested
candidate. Final disposition: B1B_CONSTRUCTION_CONTROL_OBSERVED_ONLY. The
[completion ledger](../../reviews/decoder-b1b-2026-09-27.md) is current; historical
draft statements at the end describe their writing-time status, not pending work.

**Goal:** Prove small duplicate-free construction, ordered publication and CONT
handoff/unlink through independent actual-slot observations.

**Architecture:** Reuse frozen B1a shell classes and U1–U11; isolate new B1b subject
from separately authored literal expectations/observer. Keep completed roots
private and retained; structural construction does not imply READY or cleanup.

**Tech Stack:** Existing Python/pytest; stdlib weakref/dataclasses/typing/struct
and the fixed importlib.util/sys/pathlib loader, no new installation.

**Spec:** `../specs/2026-09-27-decoder-b1b-construction-control-design.md`, all sections.
The coordinator freezes this documentation commit before implementation dispatch.

## Global Constraints

- Base `8e75340c3e436813136cafef3679810dc232bcc2`; coordinator alone integrates.
- New `tests/structural_b1b/{subject,observer,fixtures,mutants,test_b1b_structure}.py`
  only; frozen B1a five files byte-identical. Existing src/helper/proofs/guard/CI/
  config/registry/source/sequence remain unchanged.
- No replacement/WORK/retire/finish/READY/known_failure/dispose/parser/native/
  N4097/source lanes/consumer/owner work. No main, SSH, orders, deploy, restart,
  environment/permission expansion, runner changes, credential access or installs.
- Maximum result `B1B_CONSTRUCTION_CONTROL_OBSERVED_ONLY`; full R2/cold RED/A/free/
  full L3 remain open. `native_qualified=false`, `source_execution_permitted=false`,
  qualified runtime0 and source108 call-phase0 remain unchanged.
- Existing 256facts/5ms, raw50ms, restore1000ms/ticket1500ms and cold/cleanup timers
  unchanged; old two performance exceptions never excuse new failure/timeout.
- Root plus at most three workers, distinct feature/* worktrees, one writer per
  file, no fanout or unrecorded fallback. Requested/actual model and effort
  metadata distinguished. Author never approves own critical changes.
- All executable checks use the existing single workload lock and exact profile
  below. Full suites coordinator only, sequential UTC900/KST900, first failure
  stops. No tests during plan authoring/review.

## Review Focus

1. Completed child erased by CONT unlink scratch: Task2 nested literal trace and
   Task3 cursor_value_collision raw failure.
2. Single-entry tests miss two-entry lookup skips: Task2 third unique key plus
   first/middle/last duplicate hits, Task3 double_lookup_advance raw failure.
3. Python equality hides BOOL/INT/FLOAT/-0.0 or NaN: Task2 typed payload rows,
   Task3 bool_as_int and negative_zero_lost failures.
4. Early root/ENTRY/ITEM visibility appears complete: Task2 every publication
   row plus unfinished-key/value admission and every before/after fault boundary.
5. Oracle retains cells or tests accept unsupported as cleanup: Task2 frame-return
   census, external CONT alias, observer abort, duplicate latch and no-READY checks.

## 0. Ownership, fixed interfaces and dispatch gates

- [x] Independent Astra/xhigh critical design reviewer (not author, 20min, no
  fanout/fallback) reads design/plan, transition inventory and full B1a limits.
  Coordinator records decision/findings and freezes accepted spec/plan SHA.
- [x] Coordinator records B1a five-file fingerprints and exact node inventory,
  branch/base and permitted output paths. No active worker duplicates a task.
- [x] Subject author Astra/high, 25min per bounded task, and independent expected
  author Astra/high, 30min per bounded task. Critical ownership/observation is
  routing rationale. Final independent reviewer Astra/xhigh, 20min. Record actual
  identity only when exposed; otherwise unverified, not assumed Astra proof.
- [x] Sequential stages: scaffold review commit → independent oracle/RED →
  subject implementation → separate mutant connection → fresh review/full suites.
  Each dependent stage uses the coordinator's reviewed predecessor SHA. Parallel
  writers, if used, have separate worktrees from the same designated SHA and
  nonoverlapping files. Workers may not create extra agents.

```python
# subject.py, exact shell aliases and Scalar/Action/API as spec §3
def make_supervisor(token: tuple[int, int, int, int]) -> Supervisor: ...
def submit(supervisor: Supervisor, operation: Operation,
           token: tuple[int, int, int, int], action: Action) -> None: ...
def tick(supervisor: Supervisor, operation: Operation,
         token: tuple[int, int, int, int]) -> None: ...
def _step(operation: Operation) -> None: ...
def _new_cell() -> Cell: ...

# observer.py
class ObserverAbort(BaseException): ...
def install_census(monkeypatch, subject_module) -> Census: ...
def snapshot(supervisor: object, census: Census) -> Snapshot: ...
def check_state(actual: Snapshot, expected: ExpectedState) -> tuple[str, ...]: ...
def check_transition(before: Snapshot, after: Snapshot,
                     expected_before: ExpectedState,
                     expected_after: ExpectedState) -> tuple[str, ...]: ...
def retained_live(census: Census) -> tuple[int, ...]: ...

# fixtures.py
def expected_states(case_name: str) -> tuple[ExpectedState, ...]: ...
def case_actions(case_name: str) -> tuple[Action, ...]: ...
def drive(supervisor: object, operation: object,
          token: tuple[int, int, int, int], case_name: str,
          census: object) -> tuple[str, ...]: ...

# mutants.py
def install_mutant(monkeypatch, subject_module, name: str,
                   external_holder: object) -> None: ...
```

Observer.py defines Census and the frozen Snapshot/ExpectedState dataclasses;
fixtures.py imports these types, without observer importing fixtures at module
load. ExpectedState has no subject-dependent construction logic. Snapshot and
ExpectedState use the same frozen scalar field schema:
`slots, phase, action, outcome, fault_id, operation_id, token_id, reached,
returned, generation, live, forward, backward, semantic, continuations,
completed, pending_keys, construction, unlinking, issues`.
`slots` is sorted pairs of `(owner,slot)` to normalized value; owner is the literal
string `arena`, `holders`, or a generation int. Sort owners arena, then holders,
then numeric generations; within an owner sort slot names lexically. Tagged
payload/action atoms are `('NULL',None)`, `('BOOL',v)`,
`('INT',v)`, `('FLOAT',binary64hex)`, `('STR',v)`; tag/phase/outcome names remain
plain strings. Cell references are None/generation, uncensused references produce
an explicit issue. Domain fields are ordered tuples of generation IDs, visited
once in the traversal order pinned by the spec. `continuations` is top-to-bottom;
completed/pending_keys follow that order; construction is new then cursor when
cursor holds the opening container; unlinking is the target CONT only. `semantic`
walks private root first, then each CONT.c0/c1/c2 top-to-bottom, following ownership
edges in slot order and de-duplicating only declared aliases. The literal author
defines exact values at every row; subject code never reads them.

ID fields are bound once to fixture-created shell/token and the injected fault
object, never to an actual graph answer. No strong Cell/frame/exception in Census,
Snapshot, ExpectedState or saved trace. Drive retains only previous/current actual
snapshot, returns the first fixed violation code, and enforces1000tick cap as
INCONCLUSIVE. Original operation/token are passed every call. Normal snapshots
occur after tick/constructor-wrapper frames return.

## 1. Existing guarded standard profile — execution only after dispatch

This is the accepted B1a small standard prefix, with only the separate constant
`QWQ_B1B_RAW_MUTANT` selector replacing `QWQ_B1_RAW_MUTANT`. It is not a new runner.
Call from the exact candidate cwd recorded by coordinator. Use approved absolute
artifact log paths. The fixed fake SSH key must be nonexistent, checked without
reading any real credential. Environment credentials/HOME/PYTEST_ADDOPTS/
QWQ_VERIFY_SKIP_TESTS are not inherited.

```bash
# arguments: TZ cap log mutant selector...; normal mutant=none
run_b1b_standard() {
  local b1b_tz="$1" b1b_cap="$2" b1b_log="$3" b1b_mutant="$4"
  shift 4
  local -a b1b_rc
  set -o pipefail
  flock -w 240 /home/ubuntu/projects/qwq-ai-trader/.claude/worktrees/owner-ticket-gate-20260926/.superpowers/sdd/2026-09-27-runtime-admission-contract/test-workload.lock \
    timeout --signal=TERM "${b1b_cap}s" env -i PATH=/usr/bin:/bin LANG=C.UTF-8 TZ="$b1b_tz" \
    PYTHONDONTWRITEBYTECODE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
    QWQ_B1B_RAW_MUTANT="$b1b_mutant" \
    QWQ_DEPLOY_SSH_KEY=/tmp/qwq-verification-XQNQxF/nonexistent-key \
    /home/ubuntu/projects/qwq-ai-trader/venv/bin/python -m pytest "$@" -x -q \
    -p no:cacheprovider -p pytest_asyncio.plugin -p pytest_cov.plugin \
    -p anyio.pytest_plugin --tb=short 2>&1 | tee "$b1b_log"
  b1b_rc=("${PIPESTATUS[@]}")
  printf 'raw_workload_rc=%s raw_tee_rc=%s\n' "${b1b_rc[0]}" "${b1b_rc[1]}"
  if (( b1b_rc[0] != 0 )); then return "${b1b_rc[0]}"; fi
  return "${b1b_rc[1]}"
}
```

Outer errexit must not skip PIPESTATUS collection. Preserve tool exit_code,
workload/tee exit, full raw log and assertion identity. Lock failure,124, signals,
log failure, observer/import/collection errors stop progression. No blind retry.
Only coordinator-confirmed intended call-phase rc1 advances an expected RED gate.
Normal full/B1a tests see no B1 raw selector and default to their existing none.

| Stage | TZ/cap | Exact selector and expected evidence |
| --- | --- | --- |
| Initial RED then GREEN | UTC180 | tests/structural_b1b/test_b1b_structure.py::test_scalar_requires_private_root; assertion rc1 then rc0 |
| Small module | UTC180 | tests/structural_b1b/test_b1b_structure.py; all normal nodes rc0 |
| Each raw mutant once | UTC180 | tests/structural_b1b/test_b1b_structure.py::test_raw_mutant_probe; intended call-phase rc1 |
| B1a+B1b preservation check | UTC180 | tests/structural_b1/test_structure.py tests/structural_b1b/test_b1b_structure.py; rc0 |
| Whole suite, coordinator only | UTC900 then Asia/Seoul900 | tests; each raw/tool exit0 and isolation0 |

Preserve source discovery exclusion, existing conftest and plugin3 allowlist.
No -k/skip/deselect, source108 explicit collection, source/native call phase, large
test, new runner or guard edit. This profile does not cap each stream at2MiB and
is only the established small standard profile. N4097 gate stays UNRUN.
Syntax check, when authorized, is py_compile of only the five new files using
existing Python with task temporary bytecode prefix; no broad/product import.
Full-suite historical base was UTC6527/644.41s and KST6527/664.16s, each
16skip/2xfail/4warnings; those are context, not candidate evidence or budget grant.

## Task 1: Importable shell scaffold for genuine structural RED

**Owner/files:** subject author; create only subject.py. Exact Interfaces §0/spec.

- [x] Alias frozen shell types and bootstrap/identity validation. Implement
  scalar-only scaffold admission: exact action `('scalar', exact Scalar)` at a
  fresh root, graph0/constructor0, then phase=V0/action set. `_new_cell` exists
  but is never called by scaffold `_step`.
- [x] Scaffold `_step` only clears action and sets IDLE, with graph0. tick has
  identity/idle checks and foreign Exception retention. This deliberate absence
  of graph construction is the RED subject, not a claimed finished component.
- [x] Coordinator source-checks fixed loader dependency and exact slots, syntax
  only under authorized profile, then records reviewed scaffold SHA. No testing
  or synthetic failure flag is inserted. Root permits next task from this SHA.

## Task 2: Independent expected states, observer, and first call-phase RED

**Owner/files:** independent expected author creates observer.py, fixtures.py,
mutants.py, test_b1b_structure.py; no subject edit. Consumes exact shell API and spec
tables. Produces all §0 observer/fixture interfaces and raw-case mapping below.

- [x] Write `test_scalar_requires_private_root` using exact token, original
  operation, scalar7 and normal drive/check_state. After driver returns first
  assert actual arena.root is not None with `STRUCTURE_MISSING_PRIVATE_ROOT`;
  then assert violations==(), then compare full literal generation1 INT/payload7,
  root/head/tail1, holdersNone, IDLE/actionNone/outcomeNone, constructor1.
  This direct missing-root assertion must run even if drive reports a phase
  mismatch, so scaffold yields the intended structural failure rather than an
  import error or generic harness error. Test holds no Cell in saved state.
- [x] Coordinator runs the exact initial RED node UTC180, confirms actual rc1 and
  `STRUCTURE_MISSING_PRIVATE_ROOT` call-phase assertion, preserves raw output.
  Anything else stops; no GREEN code before this evidence.
- [x] Literalize every normal fixture/row/branch in spec §6, including submit
  transitions. Renderer may patch previous expected states using literal
  generations only; it cannot inspect actual state or import subject helpers.
- [x] Add `test_scalar_types_and_nonfinite_are_private`, `test_empty_containers`,
  `test_array_order_and_duplicates`, `test_three_unique_object_keys`, and
  `test_nested_end_then_publish` with spec's exact generations, slots, order and
  retained-live sets; no READY, cleanup or legacy materialization assertion.
- [x] Add `test_duplicate_hits_fail_closed` for first/middle/last ENTRY with
  constructor0 for key action, unchanged semantic slots and unsupported latch;
  subsequent calls unchanged. Add `test_open_parent_admission_is_unchanged` for
  key outside OBJECT, nested start without key, second pending key, publish without
  child, end with pending key/child, scalar/start after private root and idle tick.
- [x] Add `test_invalid_input_and_identity_are_constructor_zero`: malformed
  action lengths/name/payload, non-None control payload, list/dict/tuple/bytes/
  object scalars, subclasses, token equality without identity, wrong operation,
  missing/foreign active and invalid bootstrap token. Compare original and foreign
  operation snapshots; counts do not change. No user __eq__/__repr__ is invoked.
- [x] Add `test_fault_before_and_after_each_reachable_row`, keyed by literal
  case/row index/side, covering Q roles, empty/nonempty append, both destinations,
  ENTRY/ITEM first/next, L0 empty/nonempty, L1 repeat/miss/hit, D0 root/parent,
  CONT U middle/tail. Compare exact boundary plus original fault identity and
  subsequent refusal state. Fault before/after final rows and unsupported hit
  must be represented. Do not count B1b-unreachable U head/single as covered.
- [x] Add `test_observer_abort_is_inconclusive` at census-record and snapshot
  failures; ObserverAbort escapes Exception latch, outcome/fault untouched.
  Add `test_uncensused_reference_is_not_qualified`, `test_cont_live_after_frames`,
  and `test_snapshot_cannot_prove_single_physical_write`. No forced GC, no log
  frame/cell refs, no snapshot-delta claim of physical-write proof.
- [x] Author literal RAW_CASES and normal mutant contract tests now; site binding
  waits for Task3. No missing implementation skip/xfail is added. Coordinator
  freezes expected rows and receives an independent source review of oracle
  independence before dispatching subject implementation.

## Task 3: Semantic subject, then separately authored actual mutants

**Files/ownership:** subject author modifies subject.py only. After its handoff,
expected author modifies mutants.py only to bind actual sites; changing the
frozen expected/test contract requires coordinator-visible design correction,
not a quiet GREEN adjustment. Implements spec §3–5 exactly.

- [x] Implement complete admission and direct finite `_step` branches. Preserve
  new through publication, cursor ownership O5–C8, lookup-before-KEY, explicit
  publish, D0 destination-before-D2 source clear, and frozen U1–U11 delegation.
  Never build a generic action-script/expected-table interpreter.
- [x] Source-check every branch for one graph assignment, Q0 exception only.
  Preserve exact bool/int/float tags, -0.0 payload and deferred nonfinite checking.
  Duplicate hit is unsupported retained state, not known failure. Public
  make_supervisor/submit/tick do not expose Cell handles; private `_new_cell`
  necessarily returns a Cell internally. No normal callback/await/generator.
- [x] Coordinator runs the original exact RED node with unchanged profile and
  verifies GREEN rc0. Review actual diff against frozen expected and subject's
  allowed imports before handing actual sites to the separate mutant author.
- [x] Bind executable mutants to actual subject phase/slot/call sites, retaining
  wrappers/cells only in declared temporary/external test holders. All patched
  sites restore through monkeypatch. No boolean/count-only fake mutant.

| Raw name(s) | Fixed case | Normal predicate killed by mutation |
| --- | --- | --- |
| clear_container_cursor, push_cont_early | nested | all exact O/C rows and container ownership |
| clear_child_before_destination, pop_cont_with_live_child, cursor_value_collision | nested | exact D/U rows and retained completed-child generation |
| skip_lookup, double_lookup_advance, clear_lookup_early | object_three | per-entry L0/L1 position and key-allocation boundary |
| entry_missing_value, clear_value_before_publish, entry_wrong_order | object_three | exact P rows and ENTRY key/value/order |
| item_wrong_order | array_three | exact P rows and ITEM order |
| key_as_str | object_three | exact KEY tag/payload |
| bool_as_int | scalar_true | exact BOOL payload and type |
| negative_zero_lost | scalar_negative_zero | exact FLOAT binary64 `8000000000000000` |
| duplicate_admitted | duplicate_middle | zero constructor/unchanged graph and unsupported outcome |
| cont_external_alias | empty_object | final live `(1,)`, specifically absence of removed CONT2 |

The fixture names above are fixed `case_name` values in §0, in addition to scalar
cases and fault/admission setup cases. `nested`, `object_three`, `array_three`,
`empty_object`, `scalar_true`, `scalar_negative_zero`, `duplicate_middle` correspond
exactly to spec §6; normal `none` raw probe uses `nested`. `RAW_CASES` is a literal
tuple of all seventeen mutant-name/case-name pairs; no generated path/import.

- [x] `test_actual_mutants_rejected` runs each mapped normal case with the normal
  predicate first, then actual mutant with that identical predicate. `test_raw_mutant_probe`
  reads `os.environ.get('QWQ_B1B_RAW_MUTANT','none')`; none runs normal nested case,
  named mutants choose only literal RAW_CASES, unknown values are harness errors.
  Final assertion is the same exact-state/retained-live predicate, no raises wrapper
  around its raw failure. Normal default node always runs, no skip/deselect.
- [x] Coordinator executes small module UTC180 and stops on first unexpected
  failure. Then execute each of seventeen raw names once at the fixed raw node,
  UTC180, preserving intended assertion/rc1. Confirm each before proceeding to
  the next; import/observer/census-inconclusive failures do not count as kills.
  Source-review-only double-write control is not one of seventeen raw failures.
- [x] Independent reviewer binds row→SHA/source-line mapping and mutant sites,
  checks scratch disjointness and nested retained-live values, source closure,
  exact preflight rejection and exception retention. Report findings and actual
  node/log/exit evidence; do not approve from worker prose alone.

## Task 4: Independent See and coordinator-only complete verification

**Files:** no new implementation; coordinator separately allowlists status/docs
updates when ready. Author approval0. Final review uses a different critical
reviewer from subject/oracle authors. Same-provider independence is not described
as cross-provider verification.

- [x] Coordinator compares five new files against allowlist, verifies frozen B1a
  fingerprints, import closure and unchanged src/source/sequence/guard/config/CI.
  Inspect complete literal row/branch and node inventories against actual standard
  collection using §1 profile; no source explicit collection or large node.
- [x] Run focused B1a+B1b check once at final candidate, then independent critical
  review with exact raw/site evidence. Findings requiring code changes reopen
  affected checks and review; no automatic retry of unchanged failed commands.
- [x] After critical findings closed, coordinator alone runs full `tests` UTC900
  then Asia/Seoul900 sequentially under §1. Require raw/tool exit0, isolation0,
  expected existing skip/xfail behavior and source108 call-phase0. Any new failure,
  timeout or log loss blocks integration and is recorded without exception reuse.
- [x] Verify only new-file syntax, secret patterns, allowlist diff and final
  fingerprint preservation. Inspect actual tests/output; prior suite totals do
  not substitute. Coordinator alone integrates the exact verified candidate on
  feature/* and records actual counts, durations, hashes, model metadata, gaps.
- [x] Report only `B1B_CONSTRUCTION_CONTROL_OBSERVED_ONLY` if all gates succeed.
  Explicitly retain full R2/duplicate-retire/finish/failure/dispose, N4097/runner,
  large scalar/parser/canonical/legacy, A/free/native/cold/consumer/owner/full L3
  blockers. Future full-R2 cleanup must order strong tail-index removal, process
  each logical child once, and prove final tail-alias absence across fault
  boundaries; no such cleanup is implemented here. No completed-root drop is
  reported as cleanup or native death.

## Draft self-review and handoff

Spec §2–3 maps to Tasks1/2/3; every §5 row maps to Task2 literals/faults and Task3
source; §6 fixtures/independence map to Task2; §7 mutants map to Task3; deferred
scope and small/full profile map to Task4. Current output is two ignored drafts
only. No test, compile, import, collection, Git write or product action was used
to author them. Independent critical approval and exact dispatch are outstanding.

## Coordinator plan-review disposition

Independent requested Astra/xhigh review returned CHANGES_REQUIRED for the new test
basename collision; revised unique `test_b1b_structure.py` plus tail-index cleanup
handoff and private constructor wording were re-reviewed as APPROVE_B1B_PLAN_ONLY.
The original and scoped re-review are preserved under
`.superpowers/sdd/2026-09-27-decoder-followup/`. Actual model/effective effort are
unverified; same-provider independence is not cross-provider qualification.
This permits the bounded test-only Plan→Do→See sequence under the user's delegated
continuation, not a claim of implementation success or native/operational authority.
