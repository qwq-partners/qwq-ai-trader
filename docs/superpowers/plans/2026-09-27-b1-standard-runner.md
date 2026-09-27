# B1 standard runner Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking. This is a plan-only approved work package; implementation acceptance is outstanding.

**Goal:** Add one immutable B1 standard profile with per-stream2097152-byte retention and typed
local process evidence, preserving the existing 8 MiB profile.

**Architecture:** Extend the existing controller/guard bootstrap narrowly; reuse its sole owner,
raw waiter, pidfd cleanup and drain loop. Independently implement a strict B1-only evidence binder
that cannot accept the old profile or claim semantic/native/CI/production qualification.

**Tech Stack:** Existing Python/pytest environment and Linux subreaper/pidfd/fcntl; no dependencies.

**Spec:** `../specs/2026-09-27-b1-standard-runner-design.md`.
Independent plan review is complete; exact implementation dispatch and every acceptance gate remain required.

Date/base:2026-09-27 KST / `8e75340c3e436813136cafef3679810dc232bcc2`.
Requested author Astra/high; actual model/effective effort **unverified**, no fallback claimed.
Author made only the two assigned ignored Markdown artifacts. No tests/import/compile/collect/
experiments/network/install/credentials/SSH/operations/git writes/tracked edits were performed.

## Global Constraints

- B1 profile=`b1-standard/v1`; absent flag retains v1 behavior and canonical shape/bytes for equal
  facts and identities. Old LIMIT8388608 and retained8388609 are unchanged.
- B1 stdout and stderr each retain at most2097152 bytes; overflow observes byte2097153 without
  retaining it; errors are monotonic and all streams continue draining during cleanup.
- Total900 includes lock, work, cleanup, publication; run_end=total_end-4; cleanup3 and finalization1
  reserved; lock-wait maximum240; startup10 after spawn; waitability probe maximum1.
- No caller limits, lock paths, argv/env extensions, new reaper, threads, permission expansion,
  guard/producer/config/CI/product changes. Source/native execution and qualification remain0.
- Exact guard hash, fixed `-x`/plugins, empty parent env, exact child env plus created nonexistent
  fake-key path, unchanged literal-path selectors, and existing common workload lock are mandatory.
- Full runs coordinator-only, serial UTC then Asia/Seoul, **all workers stopped including readers**.
- Current B1b adds no large node. N4097 remains **UNRUN**; separate semantic review and three-run GO.
- Existing two exact historical performance exceptions are unchanged; no retries/new exceptions.

## Review Focus

1. Complete-looking process result followed by failed close/unlock/crash: require independent raw
   controller rc and a linked same-host monotonic upper-bound bracket including orchestration gaps;
   test `test_b1_candidate_publication_does_not_prove_exit` in Task1/3 and ledger check in Task4.
2. Suite already owns shared lock: no reentrant bypass; synthetic lock isolated, exact real smoke
   unnested; test `test_b1_nested_lock_never_launches` in Task3.
3. Output crosses cap only in a final unread chunk or after log failure: overflow still latches;
   tests `test_b1_final_chunk_overflow` and `test_b1_write_failure_still_detects_overflow` in Task3.
4. Lock wait consumes startup/total budget: startup clock begins at spawn, overall budget never
   restarts; test `test_b1_lock_wait_preserves_total_and_spawn_startup_deadlines` in Task3.
5. Existing skip/xfail and forged matching expected profile: preserve v1 rejection; B1 can only
   bind process facts with outcomes_accepted=false; tests in Task1.

## Roles, files, and integration order

The coordinator first freezes an approved revision of both documents and an exact implementation
base; if it differs from the stated base, inspect the relevant diff before dispatch. Use `feature/*`
branches, separate worktrees from that same SHA, no overlapping writers, no fanout, maximum one
coordinator plus three workers. Record requested and observed actual model/effort separately.

| Role | Requested model/effort | Owned files |
| --- | --- | --- |
| Contract/independent test author | Astra/high: evidence trust boundary | `scripts/dev/verification_os_contract.py`; new `tests/dev/test_b1_process_contract.py`; new `tests/dev/test_b1_evidence_controller.py` |
| Controller/bootstrap author | Astra/high: process/credential ownership | `scripts/dev/pytest_evidence_controller.py`; `scripts/dev/pytest_evidence_bootstrap.py` |
| Independent final reviewer | Astra/xhigh or verified Opus/xhigh: critical, non-author | Read-only exact diff/evidence; review artifact only |
| Coordinator | Existing coordinator | Integration, fixture-helper adjustment in `tests/dev/test_pytest_evidence_controller.py`, docs, verification |

Reviewer preference is cross-provider where verified/available; absence of actual identity is
unverified, not cross-provider proof. Stop on a required review gate being unavailable; no silent
downgrade or permission/auth changes. Per dispatch suggest20-minute/9000-output-token bounded
tasks; split another task if necessary, without extending workload time/output caps.

Future tracked documentation allowlist: this spec/plan promoted to
`docs/superpowers/specs/2026-09-27-b1-standard-runner-design.md` and
`docs/superpowers/plans/2026-09-27-b1-standard-runner.md`, a new
`docs/reviews/b1-standard-runner-2026-09-27.md`, and concise `CHANGELOG.md`, `CLAUDE.md`,
`docs/README.md` state entries. Promotion/commits are future coordinator actions, not done here.
No MEMORY change is necessary absent a new general lesson.

## Verification command profile for future authorized execution

For focused regression development, use the actual existing standard launcher recorded at
`.superpowers/sdd/2026-09-27-runtime-admission-contract/coordinator-validation-ledger.md:7`
with the B1a first-failure `-x` addition documented in the existing B1 plan §1. Exact fixed prefix:

```text
flock -w 240 /home/ubuntu/projects/qwq-ai-trader/.claude/worktrees/owner-ticket-gate-20260926/.superpowers/sdd/2026-09-27-runtime-admission-contract/test-workload.lock
timeout --signal=TERM 300s env -i PATH=/usr/bin:/bin LANG=C.UTF-8 TZ=UTC
PYTHONDONTWRITEBYTECODE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1
QWQ_DEPLOY_SSH_KEY=/tmp/qwq-verification-XQNQxF/nonexistent-key
/home/ubuntu/projects/qwq-ai-trader/venv/bin/python -m pytest SELECTED_PATHS
-x -q -p no:cacheprovider -p pytest_asyncio.plugin -p pytest_cov.plugin
-p anyio.pytest_plugin --tb=short
```

This is one command, shown on separate lines for review. Before use verify the fixed fake path
is nonexistent; do not substitute a real key or pass HOME. If the historical fake path's safe
nonexistence cannot be established, stop and let the coordinator bind another private nonexistent
fixture path in the execution record. Preserve raw output using existing pipefail/tee procedure
and all PIPESTATUS values plus final tool exit. This legacy prefix is **not 2 MiB evidence**.
No wrapper function is created by this draft. Paths in each Task replace SELECTED_PATHS literally;
cap300 for focused and cap900 only for whole-suite verification. No `-k`, deselect or skip additions.

The later actual B1 smoke selects `tests/dev/test_verification_contract.py` (the existing small
pure-contract module, not a large decoder workload) and invokes existing absolute Python `-I -B`
and controller directly under
the five-variable env-i from the spec, exact --profile and --timeout-seconds900, safe distinct
context/receipt/process paths, and one existing small test-file path. **No outer flock/tee child
capture/timeout wrapper**: controller owns lock/drain/deadline. The execution tool supplies raw
terminal exit evidence; its per-call wall_time_seconds/wait fields do not prove invocation time.
Complete an independent same-host monotonic-nanosecond read and receive its terminal command
receipt before launch; begin the second read only after the raw controller terminal result.
Freeze this read procedure, same host/clock-domain identity, and exact integer
elapsed_ns=post_ns-pre_ns derivation in the dispatch. Preserve both raw clock receipts, launch
receipt, yielded session linkage and terminal completion under the same unique run/attempt.
The result is a conservative upper bound including every orchestration gap, not exact lifetime.
Missing/invalid/reversed/unlinked observations or a bound above900000000000ns reject. Do not
sum tool wait durations, deduct gaps, substitute controller/pytest duration, expand limits, or
retry for a smaller bound. These one-shot clock observations add no persistent helper/reaper;
none is executed during this design correction. The exact context, expected inventory, selected
file, cwd, candidate SHA, artifact paths and receipt linkage must be frozen before the smoke.

Pre-execution input correction: collection-only evidence found one65647-byte node ID in the
original OS-contract smoke file, exceeding the unchanged producer2048-byte limit. Independent
critical review approved only substituting the existing base-contract file (36 nodes, max149
bytes; frozen SHA2565e2c1fd77236ef2dcc00d12d04b6ac78e91aba7e5a582b2280ca539e483ef53b).
Recheck its candidate identity and bind the independently collected inventory before smoke.
The OS file remains required in focused/full regression; no test or producer is modified.
Current-package full runs still use the legacy launcher. Future receipt-producing B1 full-suite
runs remain structurally blocked by that oversized ID until a separately reviewed compatibility
resolution; a successful tiny smoke cannot grant N4097/full-profile GO. Raw evidence and review
are in `.superpowers/sdd/2026-09-27-b1-standard-runner/smoke-file-revision-*.md`.

## Task 1: Independent strict B1 contract and literal expected tests

**Files:** Modify `scripts/dev/verification_os_contract.py`; create
`tests/dev/test_b1_process_contract.py`. V1 tests are read-only.

**Interfaces produced:** `parse_b1_process_result(raw: bytes)->dict`,
`validate_b1_process_receipt(receipt_raw: bytes,process_raw: bytes,expected: dict,invocation: dict)
->tuple[str,...]`, `evaluate_b1_process_slot(...same arguments...)->dict`.
Exact v2 keys/constants and B1 decision shape are the spec §7; invocation exact two fields are
controller_returncode and elapsed_ns, the latter the linked monotonic bracket's conservative
inclusive upper bound. Raw receipt provenance is a mandatory coordinator acceptance check before
constructing this value, not a property the two-field pure API can verify. No optional bypass argument.

- [ ] Write literal fixture tests independently of controller/profile constants. Assert B1 success
  requires all coordination booleans, rc0, supplied inclusive bound<=900000000000ns, bounded stream facts,
  expected run/slot/inventory/runtime/producer/guard/selection identity, and always false semantic/
  native/CI/production flags. No expected builder derives values from the result under test.
- [ ] Add `test_b1_schema_profile_confusion_rejected`: v1-as-B1, B1-as-v1, mixed profile/schema,
  cap2097151/2097153/8388608, bool integers, unknown fields, repeated JSON keys, forged equal
  expected/result nonliteral constants, selection/guard/env/lock/budget profile mismatches all reject.
- [ ] Add `test_b1_prefix_observation_contract`: both streams bytes2097151/2097152 are admissible
  with matching observed; observed2097153 requires overflow; stored2097153 rejects; write-failed
  shorter prefix with overflow parses as failure but never binds; hash mismatch handled by raw
  artifact check in Task4. Preserve failure documents, never turn overflow into parsing-only noise.
- [ ] Add `test_b1_candidate_publication_does_not_prove_exit`: missing/wrongly typed invocation,
  rc125/signal, negative/too-large time and elapsed900000000001 all reject even with success JSON.
  Use literal synthetic evidence to check the acceptance procedure's yielded-session gap and
  after-publication-delay examples: a bracket over900 rejects even when summed per-call waits
  are under900. Missing/reversed/unlinked/nonterminal/host-or-clock-mismatched receipts must
  prevent invocation construction. This documents/checks receipt acceptance without adding a
  persistent timing helper or executing a timing probe during planning.
- [ ] Add `test_b1_outcomes_never_auto_accepted`: receipt skip/xfail may have structurally valid
  process binding, but outcomes_accepted=false; existing v1 evaluate still rejects unchanged.
  Session collection_errors/deselected/nonzero rc never bind even if expected agrees.
- [ ] Run the focused existing profile with new contract file; preserve real missing-API RED.
  Implement narrow new APIs/shared syntax reuse; do not weaken any v1 validator.
- [ ] Run new contract file plus `tests/dev/test_verification_os_contract.py` and
  `tests/dev/test_verification_contract.py`; expect actual rc0 and guard0. All three existing
  regression file paths were checked statically during drafting.
- [ ] Independent review of literal contract and v1 compatibility precedes controller integration.

## Task 2: Narrow immutable profile and parent/child launch

**Files:** Controller/bootstrap only. Consumes Task1 exact schema, not its fixture implementation.
**Interfaces:** Existing `main(argv: list[str]|None=None)->int` unchanged. `_observe` may gain
keyword-only private `profile` and `total_end` arguments with legacy defaults; existing positional
callers/tests must behave identically. `_Owner` remains the sole recorder/reaper. Internal profile
contains the spec §3 literal values; it is never serialized from caller configuration.

- [ ] Independent test author starts `test_b1_evidence_controller.py` with argument/argv/env tests:
  absent profile preserves default; only b1-standard/v1 permitted once; B1 timeout only900;
  path-only grammar unchanged; fixed -x/plugin order and guard hash; arbitrary inherited parent
  key or child passthrough rejects; unknown marker cannot become a selector.
- [ ] Run focused new controller tests for RED before author implementation; use fake subprocess/
  clocks at this stage, no unowned real processes.
- [ ] Implement profile selection, internal bootstrap marker and exact argv/env branches.
  Apply parent env rejection before dependency/guard loading. Preserve v1 paths exactly.
- [ ] Run focused launch tests and existing `tests/dev/test_pytest_evidence_controller.py`
  regression with the unchanged independent harness. Expect rc0/guard0; no new failure exemption.
- [ ] Reviewer/coordinator inspect delta for arbitrary command/env/cap surfaces, default serialization
  order and old8388609 boundary. Author cannot approve this critical change.

## Task 3: Output, coordination, deadline and failure evidence

**Files:** Same controller/bootstrap author; independent test author owns new controller test file.
Coordinator alone adjusts existing harness helpers if needed; preserve original assertions.
**Interfaces:** `_observe` returns existing `(complete,guard,streams)` shape; B1 streams gain only
observed_bytes. Process v2 exact spec; fake-dir/lock helpers stay private and fixed-policy.

- [ ] Write `test_b1_stream_boundaries` for each stream at2097151/2097152/2097153 with exact raw
  prefix/hash/count/overflow; parameterize split chunks and EOF after leader status.
  Write final-chunk, simultaneous-flood, short-write0..N, failed-write/overflow and observer-reentry
  tests. Preserve v1 LIMIT+1 assertion without changing it to a parameter-derived expectation.
- [ ] Write `test_b1_lock_wait_preserves_total_and_spawn_startup_deadlines`, nested-lock,
  no-probe-before-lock, unsafe/missing/replaced lock, lock-held-after-leader and release-failure
  tests. Fake monotonic clock proves900 includes wait/cleanup/publication; no sleep900 experiment.
  Explicitly cover `_probe` failure near the existing B1 absolute cleanup deadline: its local
  now+3 cleanup must be clamped just as observer/emergency cleanup is; preserve v1 behavior.
  Freeze `_probe(deadline, stopped, *, budget=None, controller_fds=())`: adapter tests
  validate the exact unique-int tuple before fork, child close-once/no-unlock before23,
  close-failure125 with remaining close attempts, parent ownership and v1 defaults.
  Assert O_NONBLOCK/O_NOFOLLOW/O_CLOEXEC leaf open and regular/uid/nlink/identity checks
  before flock; FIFO/unsafe/replace paths never probe/spawn. Fake-clock cases prohibit
  fork at/after run_end and prohibit spawn after pipe/argv setup exhausts run_end.
- [ ] Write fake-key private-dir/leaf-nonexistence/replacement/created-leaf/rmdir-failure tests;
  assert no key read/create/unlink, no recursive cleanup, no inherited credentials, exact child env.
- [ ] Extend independent harness fixture entrypoints only as necessary. Isolated copied fixtures
  rewrite the fixed lock literal to a private synthetic lock; record changed source identity and
  never claim actual standard profile qualification from such results. Keep sole test-harness
  owner independent from the candidate. Verify its failed-dummy cleanup before unsafe fixtures.
- [ ] Implement cap accounting and coordination/deadline branches using the existing drain/owner
  code. Keep latched errors and absolute finish deadline across retries/emergency paths; cleanup
  excludes unrelated processes and early lock release. Fix no unrelated reaper behavior.
- [ ] Add real bounded fixture cases for exit0/1/9/signal/hang, setsid/double-fork/adoption, overflow,
  guard failure and observer exception. Add adapters for missing status/EOF/ECHILD, pidfd denial,
  unresponsive kernel/cleanup deadline and result short-write/close/unlock/crash after candidate
  publication. Assert nonzero/owned cleanup/raw statuses, not just exception text.
- [ ] Run new controller tests plus unchanged old controller and controlled-evidence integration
  file `tests/dev/test_controlled_verification_evidence.py` under the fixed focused profile.
  First unexpected nonzero/missing harness evidence stops the batch; preserve raw evidence.
- [ ] Independently review all ownership-sensitive changes, output cap at exact byte boundaries,
  env key order/values, guard bytes and no duplicate reaper. No semantic B1 tests are modified.

Task3 private seams were independently re-reviewed after P1 findings for inherited
fork-probe lock FDs and potentially blocking lock leaf open. The coordinator's
`task-3-seam-revision.md` and `task-3-seam-revision-review.md` in the runner artifact
directory preserve the exact close/deadline/finalization contract and disposition
`APPROVE_TASK3_SEAMS_ONLY`. This permits independent tests-first, not runtime acceptance.
Implement bounded budget/probe/cleanup adapters first, then coordination/main and
retained-output/publication. Keep valid B1 main fail-closed until all parts are ready.

## Task 4: Coordinator acceptance, real profile smoke, and future handoff

**Files:** Coordinator docs/ledger only after integrating reviewed code/test deltas; no source-native
execution, large-node creation, main/CI/product settings or operational changes.

- [ ] Inspect each worker's actual diff, requested/actual model record, raw commands/statuses and
  independently authored tests. Integrate only nonoverlapping reviewed deltas into candidate.
- [ ] Run combined new/old controller, consumer and producer regression using the existing standard
  launcher and recorded exact selectors. Run required syntax and canonical secret scan using
  existing repository procedures; no new exclusions, credential reads or guard modification.
- [ ] Have an independent critical reviewer inspect candidate and raw failure/success evidence.
  Require actual new-profile tests as well as all old8MiB regressions; approval is limited to
  readiness for the tiny actual-profile smoke and full verification, not N4097 or operations.
- [ ] Stop all workers. Freeze small actual-profile smoke dispatch with the fixed pure-contract file above,
  independent expected inventory, literal context, candidate/runtime identities and artifact paths.
  Freeze the same-host monotonic bracket and raw receipt linkage defined above, with a pre-read
  completed before launch and post-read begun after the raw terminal controller result. Execute
  the controller once without outer flock; independently check linked raw rc0 and nonnegative
  exact-integer post_ns-pre_ns<=900000000000, preserving orchestration gaps and any delay after
  candidate publication. Missing/reversed/invalid/unlinked observations reject before constructing
  invocation; no per-call wait sums, gap deductions, retries or limit expansion. Check
  both log byte limits/hashes, parsed receipt/process, B1 profile/constants and coordination facts.
  CLI0 alone or B1_PROCESS_BOUND alone is insufficient. Any new failure stops; no automatic retry.
- [ ] Keep all workers stopped. Coordinator runs existing approved standard full suite UTC then
  Asia/Seoul at each900, preserving established skip/xfail exact ledger, first-failure `-x`, guard0,
  raw workload/tee/tool exits. This verifies the implementation without pretending legacy full
  output is 2MiB-controlled or exercising not-yet-created N4097. First new failure stops next run.
- [ ] Independent final review after full runs checks exact tested tree, v1 byte/behavior equivalence,
  typed limitations, raw artifacts and no hidden scope expansion. Coordinator publishes development
  ledger/allowed commits only after evidence passes; no push/main/operations inferred here.
- [ ] Mark only runner development/profile acceptance actually proved. Keep outcomes policy,
  native/CI/production flags false. Record no large node, no three-run budget approval, N4097 UNRUN.
  A separate semantic owner supplies future one-node file/inventory and oracle; future execution
  plan fixes dedicated1+UTCfull1+KSTfull1 with new profile, each inclusive900 and cap2MiB/stream.

## Self-review and unexecuted exit condition

Spec §§2/3 map Task2, §§4/5/6 Task3, §7 Task1, §§8/9 Task4. Every Review Focus risk has a named
test. Interfaces/names/constants agree; v1 APIs remain unchanged. No task introduces a N4097
semantic dependency. The exact-profile smoke is distinguished from synthetic rewritten fixtures.
Draft text checks are not test evidence. Design/plan review and explicit implementation work-package
authorization remain future gates; the author does not approve this plan or claim any tests passed.

## Coordinator plan-review disposition

The independent requested Astra/xhigh reviewer required correction I1: per-call tool wait
fields do not measure total invocation lifetime. The author froze the conservative same-host
monotonic prelaunch/postcompletion bracket and linked receipts; scoped re-review returned
APPROVE_RUNNER_PLAN_ONLY with C0/I0/M0. The original drafts/reviews/hashes remain under
`.superpowers/sdd/2026-09-27-decoder-followup/`. Actual model/effective effort are unverified,
not cross-provider proof. Historical draft-author statements above describe the writing task.
Under the user's autonomous consecutive-development instruction, the coordinator may dispatch
this separately bounded local development plan, not native qualification/large-case execution,
installation, main, or operations. No implementation/test success is asserted here.
