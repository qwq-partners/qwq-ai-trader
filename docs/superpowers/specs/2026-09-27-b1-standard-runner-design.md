# B1 standard runner — independently reviewed architecture

2026-09-27 KST. Base `8e75340c3e436813136cafef3679810dc232bcc2`.
Requested author: Astra/high for critical process ownership/evidence. Actual model and effective
effort metadata are unavailable: **unverified**. No fallback claimed.

**PLAN-ONLY APPROVED / IMPLEMENTATION ACCEPTANCE OUTSTANDING.** This document and its companion plan are
authorized design artifacts only. The B1a implementation scope explicitly excluded a new
runner. This proposes a separately reviewed successor; it does not reinterpret B1a approval.
N4097 is **UNRUN**. No test, import, compile, collection, subprocess experiment, network,
installation, credential access, SSH, operation, git write, or tracked-file change was performed
for this draft. Concurrent B1b semantics work does not depend on this runner and adds no large case.

## 1. Intent, evidence, and alternatives

Provide a local standard pytest launcher with a real independent 2,097,152-byte retained prefix
for stdout and stderr, observable overflow, owned-child cleanup, exact launch constraints, and
typed evidence. Preserve the approved default controller's 8 MiB v1 behavior. This is a process
evidence component, not decoder correctness, native permission, CI provenance, or production authority.

Read completely: `runner-inventory.md`. Source basis: controller `_arguments`, `_Owner`, `_probe`,
`_observe`, `_emergency_cleanup`, `main`; bootstrap fixed arguments/guard; OS contract parser,
launch/stream validators and receipt binder; producer receipt construction. Relevant evidence:
`tests/dev/test_pytest_evidence_controller.py` real harness, flood, final-chunk, short-write,
probe, descendant, identity and publication-cutoff cases; `test_verification_os_contract.py`
8 MiB boundaries and skip/xfail rejection; existing OS design; B1 structural plan §§1,5.
Repository `CLAUDE.md`, recent `CHANGELOG.md`, and document index informed project boundaries.
This is static source analysis, not fresh runtime verification.

Three approaches:

1. **Recommended: one named profile inside the current controller.** Add a private immutable
   policy value and narrow B1 branches for retention, launch, lock, budget, and v2 evidence.
   Reuse the existing owner, raw waits, pidfd signaling, drain loop and emergency cleanup.
2. A shared private process core with two CLI adapters separates entrypoints but changes more
   ownership-sensitive code. Defer unless implementation reveals that narrow branching is unsafe.
3. A wrapper or copied controller is rejected: an outer wrapper cannot turn an internal 8 MiB
   prefix into a 2 MiB artifact; copying creates a second reaper implementation to maintain.

## 2. Compatibility and bounded interface

Keep the existing invocation unchanged when `--profile` is absent. Its fixed `LIMIT=8388608`,
retained prefix up to8388609, schema `qwq.verification-process-result/v1`, launch profile
`pytest-evidence-bootstrap/v1`, argv/env, reason/exit mappings, result key order/canonical bytes,
and existing helper default behavior remain unchanged. Source hashes necessarily change when
controller/bootstrap bytes change; compatibility means the same evidence bytes for the same
facts/identities, not pretending the old identity hash still describes changed source.

Add exactly one optional non-repeatable option:

```text
--profile b1-standard/v1
```

No numeric stream cap, lock path, env passthrough, executable, shell, command, extra pytest
options, timeout extension, unsafe override, or profile discovery mechanism. Unknown/repeated/
abbreviated profile flags are preflight failures. Existing `--timeout-seconds` remains mandatory;
for B1 its only accepted value is integer900. The v1 range1..900 is unchanged. Synthetic unit
adapters may control clocks; those runs are not actual B1 profile evidence.

The existing selector grammar is unchanged: normalized distinct literal paths beneath `tests`,
no `::`, wildcard, options, symlink, escape or explicit `tests/proofs`. The future semantic work
package will place exactly one unparameterized large node in
`tests/structural_b1/test_decoder_n4097.py`; a separate expected inventory must prove that file
collects exactly that one node. This is a proposed integration filename, not a file to create now.
Normal full selection is the single path `tests`; source discovery exclusions stay unchanged.

## 3. Immutable launch facts

Profile implementation may use a frozen private value with `overflow_threshold=2097152`,
`retained_bytes=2097152`, `observed_saturation=2097153`, total900, lock-wait240,
cleanup3 and finalization-reserve1 seconds. These are implementation literals, not caller inputs.

B1 controller startup command is launched by the coordinator with an actual empty environment,
absolute existing venv interpreter, `-I -B`, and the controller path. Only PATH=/usr/bin:/bin,
LANG=C.UTF-8, TZ=the strict context's UTC or Asia/Seoul,
PYTHONDONTWRITEBYTECODE=1 and PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 are supplied to the controller.
The B1 entrypoint rejects any other inherited environment key or mismatched allowed value
before importing the bootstrap/guard/producer. This protects the parent as well as the child.
No HOME, PYTHONPATH, PYTEST_ADDOPTS, PYTEST_PLUGINS, QWQ_VERIFY_SKIP_TESTS, QWQ_B1_RAW_MUTANT,
credentials or arbitrary QWQ variables are inherited. V1's environment handling is unchanged.

The child gets precisely those five values plus the runner-generated QWQ_DEPLOY_SSH_KEY below.
Use `Popen(env=exact_dict, close_fds=True, stdin=DEVNULL, start_new_session=True)` and pass only
the existing control FD. No shell/env/flock subprocess is inserted between owner and bootstrap.
The interpreter remains the bound absolute `sys.executable`, with `-I -B`.

Bootstrap gets a private leading literal `--b1-standard-v1` marker before its existing positional
control/context/receipt/path arguments; absence preserves the v1 path. It permits no custom argv.
B1 pytest argument order is exactly:

```text
-x -q -p no:cacheprovider -p pytest_asyncio.plugin -p pytest_cov.plugin
-p anyio.pytest_plugin --tb=short PATH...
```

Guard algorithm/object checks/control frame and pinned SHA256 remain unchanged:
`7b7b26940309a2a165d9bdba7611b6730e83b90ae9ea5f9e725764ee4c0a23f7`.
No modification to `tests/conftest.py`, receipt producer/schema, installed packages, pytest
configuration, CI or product configuration. `-I`/env isolation do not establish a native sandbox.

## 4. Fake-key path and shared lock ownership

After guard setup, the B1 controller creates one private directory with a random name under
literal `/tmp` using `tempfile.mkdtemp(prefix="qwq-b1-runner-", dir="/tmp")`; do not consult
TMPDIR/HOME. Verify its actual mode0700, owner=current UID, regular directory/no symlink via
dirfd and fstat. The candidate is its fixed leaf `nonexistent-key`. `stat(..., follow_symlinks=False,
dir_fd=...)` must return ENOENT before launch and again after child cleanup. The controller never
creates, opens or deletes that leaf and never reads any supplied key. Any leaf creation, directory
replacement, unexpected contents or metadata error rejects the run. The child receives only this
absolute nonexistent path. At finalization remove the known empty private directory with rmdir;
failure is io_error. Do not recursively delete unexpected content. Record absent-before/after
facts and a path hash, not credential contents. Same-UID hostile mutation is outside attestation;
the observed checks are not a claim of an immutable filesystem.

Acquire the existing shared workload lock at the exact path:

```text
/home/ubuntu/projects/qwq-ai-trader/.claude/worktrees/owner-ticket-gate-20260926/.superpowers/sdd/2026-09-27-runtime-admission-contract/test-workload.lock
```

This host-specific `b1-standard/v1` profile intentionally binds that lock domain; it is not derived
from a worker worktree, not configurable, and not silently replaced if absent. Open the existing
regular lock file without following symlink components; verify fstat owner, nlink1, and bound
parent/leaf dev+ino. Use nonblocking `fcntl.flock(LOCK_EX|LOCK_NB)` with monotonic polling,
interruption checks and a fixed maximum240 seconds. Never truncate, unlink, recreate or chmod
the lock. Missing/unsafe lock is startup_error; exhausted contention is lock_timeout/CLI125.
No probe or pytest leader may spawn before acquisition. Imports finish before sole-reaper preflight.

Task3 seam clarification (independently re-reviewed): the leaf open uses
O_RDONLY|O_NOFOLLOW|O_NONBLOCK|O_CLOEXEC before its regular/uid/nlink/identity checks;
unsafe/nonregular open never reaches flock/probe/spawn. The raw-fork probe accepts
only a private exact tuple snapshot of unique nonnegative integer controller-owned
coordination FDs. Its child closes each once before exit23, without LOCK_UN;
close failure attempts remaining closes and exits125. The parent retains ownership.
The default empty tuple preserves v1; pytest receives only its control FD.

Hold the lock FD only in the controller through descendant drain and publication/finalization.
Do not pass it to the child. Recheck identity before publication; replacement rejects the run.
Release/close once in final cleanup; release/close failure is nonzero even if a candidate result
already exists. There is no early unlock on leader exit. An unexpected controller death releases
its FD but proves neither descendant death nor successful completion: stop scheduling work and
preserve evidence for the coordinator. Never sweep unrelated processes or add a crash reaper.

The coordinator must remove the old *outer* flock when invoking B1: B1 owns this same lock.
No inherited-lock exemption or reentrant mode exists. Accidentally nested launch reaches its
bounded lock failure, never launches pytest, and is a failed run.

## 5. Total budget and retained-prefix algorithm

Total900 starts at controller `main` entry and includes validation/import, lock wait, fake path,
probe, workload, cleanup, identity/receipt checks and publication. Set absolute
`total_end=started+900`, `run_end=total_end-4` (3 seconds cleanup plus1 finalization reserve).
Lock wait ends at min(lock_start+240, run_end); work/probe/startup never extends run_end.
B1 startup readiness is min(run_end, actual child-spawn-start+10), not the pre-lock timestamp;
do not accidentally spend the entire startup allowance while waiting for the lock. Probe remains
maximum1 second. Early failure/leader termination sets one immutable cleanup deadline at
min(now+3,total_end-1). TERM is allowed through its first1 second, then KILL through remaining2;
re-entry and late adoption never reset that deadline. Existing v1 timestamps/deadlines are unchanged.
This includes the existing `_probe` failure branch: its current local `finish=now+3` must be
clamped to the same B1 absolute cleanup boundary, just as `_observe` and `_emergency_cleanup`
are. Fake-clock tests must cover probe failure near that boundary without changing v1 defaults.
This is an implementation seam of the existing inclusive-budget requirement, not a new reaper
or expanded scope.

Check stopped and the same run_end immediately before probe fork, and again after
pipe/argv setup immediately before workload spawn. At/after run_end starts no child
and cannot be reclassified as ordinary lock contention. The private budget's first
cleanup/TERM endpoints are immutable across probe failure, retry and late adoption.

On each stream chunk, first saturate an independent observed counter at2097153. Write only
the still-unfilled retained prefix up to2097152 bytes. Hash/count only successfully written bytes.
Seeing byte2097153 latches overflow and output_limit even if the same chunk could not be written.
The extra detection byte is **not retained**. Never merge streams or echo unbounded child output
to controller stdout/stderr. Continue simultaneous bounded-batch drain/discard and owned cleanup
after overflow or log failure. No ring buffer, suffix preservation, resampling, second full copy,
or delayed file-size-only detection. Both EOFs plus control EOF and final raw ECHILD remain required.

Short writes latch io_error after hashing the actual reported prefix; do not retry/recount that
chunk. Partial/unreported writes remain failed evidence, never successful prefix certification.
Each successful artifact is at most2097152 bytes; overflow evidence may retain less after an I/O
failure but must still report observed2097153 and overflow=true. A final chunk after leader exit
must be read and can fail an otherwise successful run.

At each finalization boundary check remaining budget; never return0 if actual final monotonic
check exceeds total_end. Cleanup/publication failures remain nonzero. The consumer additionally
requires an independent caller-observed controller exit and a same-host monotonic bracket whose
difference is a conservative inclusive upper bound, including all orchestration gaps (§7), since
a result document cannot prove its producer's future successful close/exit. Per-call tool wait
times, their sum and the process document's own clock cannot supply that bound. A one-second finalization reserve is
budget allocation, not an OS scheduling/I/O guarantee. Blocking I/O, D-state, descheduling,
SIGKILL/host crash can exceed budget or prevent evidence. Such outcomes are inconclusive/failed,
never an extension or a justification for repeated execution.

## 6. Process ownership and failure contract

Reuse `_Owner`, `_RawPopen`, preflight, raw wait23+ECHILD probe, pidfd-first child-only authority,
and `_emergency_cleanup`. No second reaper, thread, Popen polling/wait/communicate, PID kill,
killpg, global PID search, permission expansion, namespace or cgroup introduction.
Import remains side-effect free. Final preflight requires one OS task, no existing children,
SIGCHLD reset, actual Linux subreaper/pidfd support. Probe status cannot become leader status.

| Trigger | Required facts and exit |
| --- | --- |
| Raw pytest exit1..255 | Preserve actual rc; never rewrite as pass; receipt/cleanup failures still reject |
| Leader signal | Preserve negative raw rc and shell128+signal when no earlier controller error |
| run_end or final total budget exhausted | First timeout latches; CLI124; cleanup still attempted within fixed budget |
| Lock timeout | No child/probe; lock_timeout; CLI125 |
| Overflow/log write failure | First reason retained; output_limit/io_error; CLI125; continue drain and cleanup |
| Interrupt before existing publication cutoff | interrupted/CLI125 unless an earlier reason already latched; raw rc preserved |
| Post-cutoff interrupt | Preserve documented v1-style decision cutoff; independent controller exit remains necessary |
| Detached/double-fork/setsid descendant | Existing adoption/pidfd cleanup; postleader obligation latches descendant_survived and rejects |
| Lost leader wait status, unsupported ownership, missing EOF/ECHILD | Never infer rc0/cleanup; CLI125 |
| Guard/code/fake-key/lock identity mismatch | Reject; preserve raw status and owned cleanup; CLI125 |
| Result create/write/short-write/close or lock release failure | CLI125, partial/candidate result cannot bind |
| Controller crash/SIGKILL/host crash | Missing/nonzero invocation observation rejects even with a complete-looking result; no automatic next workload |

Initial argument rejection may have no artifact. Once paths/context are safely bound, best-effort
failure publication is useful but never mandatory proof of success. Receipt is read only after
leader terminal status and complete cleanup, with current nofollow/nonblocking/regular32MiB rules.
No fallback authority exists when ownership is unsupported or cleanup is incomplete.

### 6.1 Reviewed Task3d prerequisite and mixed-failure clarification

Reviewed entry scope: the B1 hard125 matrix below starts at the validated private
body, after a valid initial timestamp and parsed invocation. Preserve the legacy
first monotonic read before argument parsing and its exception propagation. An
initial preclassification SystemExit(0) can propagate0; do not claim universal
public-entry125 normalization. This narrows the normalization promise, not admission:
missing/invalid or unlinked invocation/process/receipt/log evidence still rejects,
even with terminal0. No fabricated timestamp, foreign-attempt artifact, parser
shortcut, retry or waiver is permitted. Before separate public enablement, direct
adapters must prove exact RuntimeError/SystemExit(0) propagation for both intended
profiles, zero parsing/setup/resource/process attempts, and patch restoration;
retain independent terminal0+missing-evidence rejection. Existing in-body clock,
BaseException and finalization obligations are unchanged.

The independent Task3d re-review approved this **tests-first contract only**. It
supersedes ambiguous mixed-exit interpretations of the single-trigger table above;
it is not an implementation or enablement claim. Public B1 main remains125 until
the separate helper, shared-main and enable reviews. Accepted Task3a/3b OSError
contracts are historical scoped evidence, not proof of these stronger guarantees.

Prerequisites before main wiring:

- B1 probe child catches BaseException only around each approved FD close, attempts
  the remaining known tuple once, then terminal `_exit(125)` on any failure or23
  otherwise. No parent finish/unlock/publication/workload path is entered. The exit
  itself is outside the close/parent catches; test sentinels never enter source.
- The same private probe Owner retains parent-side exceptional cleanup, including
  an unknown non-OSError fork escape. At most one exceptional emergency attempt,
  first exception preserved, existing endpoints never reset, no replacement reaper.
  A failed fresh clock may use the last observed timestamp only to initialize an
  unset cleanup interval; no stale-time signal/sleep. Failed clock before any reap
  permits at most one nonblocking reap; a failed reap is not retried. This proves
  only a bounded attempt, never child death or cleanup success without evidence.
- Private exact-bool `b1=False` modes on `_close_descriptors` and
  `_emergency_cleanup` retain default None/exception behavior even for budget owners.
  Explicit True exhausts remaining FD batches before propagating the first
  non-OSError; expected close OSError returns False, clean batch True. Emergency
  requires an existing budget, initialized budget cleanup/TERM endpoints, and a
  non-None owner.finish_end equal to budget.cleanup_end before any effects. Missing
  or unequal Owner ends reject without clock/reap/signal/sleep/pipe/state effects;
  the callee neither initializes nor synchronizes them. The caller may synchronize
  through existing owner._begin_cleanup without extending already-fixed ends.
  A new Owner already holding valid equal ends is also valid. Emergency always
  closes transferred pipes and preserves a process exception over a batch exception.
  Bool is close status only. Invalid mode/preconditions reject before effects.
- `_BoundPaths(paths, *, b1=False)` preserves default temporary-close/constructor
  exception identity and priority. Only explicit True initializes partial ownership
  before acquisition and exhausts retained closes despite BaseException; strict
  close returns sticky False. B1 constructor failure exhausts its owned batch then
  propagates the constructor's received exception, including any temporary-close
  replacement. Every such constructor escape is a hard main veto.
- B1 coordination temporary close records/rethrows any close exception after
  consuming its FD. Final close protects every directory close, LOCK_UN and lock
  close separately, attempts all once, lock last, and retains sticky False.
  Main similarly removes each owned stream before its individual close. No stale
  snapshot retry, FD sweep, unrequested path deletion or broader v1 fix is permitted.

For B1 the final precedence is **hard125 > total-overrun124 > first-timeout124 >
other rejection125 > valid actual raw status**. A hard flag is sticky and separate
from the first reason; neither reason nor raw status is rewritten. Exact total-end
equality is allowed, strictly later is overrun. After every coordination acquire,
prepare, finish, check and close, including escapes, consume its error and independently
its close_failed flag. Check returned bounds.close_failed likewise. Explicit B1
descriptor/emergency callers must consume exact bool status immediately; False or
invalid/missing bool is hard, and a later empty-batch True cannot clear it.

| B1 stage/outcome | Classification / publication constraint |
| --- | --- |
| Ordinary bootstrap/guard/context/initial identity failure | Soft startup/identity reason; incomplete or unsafe inputs prohibit publication. |
| Any B1 bounds constructor escape | Hard; no usable bounds, no publication. |
| Acquire False / prepare None / normal probe False | Soft reason; close_failed independently hard; no downstream startup. Probe False is not cleanup evidence. |
| Any escaping probe exception after its owned attempt | Hard; no workload or workload receipt; only safe honest failure facts may be published. |
| Observer-internal handled IO / incomplete cleanup / ordinary observer escape followed by bounded retry | Soft first reason; persistent failure snapshot, not success. Do not guess internal exception origin. |
| Emergency False/escape; main-owned stream or explicit batch close failure | Hard, complete=False for emergency; continue remaining cleanup. |
| Expected fake finish False, including caught rmdir OSError | Soft recorded reason unless close_failed; no retry/deletion. |
| Ordinary exception escaping acquire/prepare/finish/lock-check | Hard plus recorded coordination reason/close flag; no retry. |
| Code/guard mismatch or ordinary check exception | Soft identity/startup reason; unknown guard remains unknown; publication only through still-safe binding. |
| Receipt missing | Soft failed gate; no new reason required; no0 inferred. |
| Receipt invalid / ordinary receipt exception | Soft io_error, invalid fact; bounds.close_failed independently hard. |
| Expected lock recheck False | Soft recorded reason unless close_failed; never reacquire/repair. |
| Snapshot/facts/serialization/size failure | Hard; no invented fallback document. |
| Result create/write/count/close failure | Hard, publication incomplete; preserve partial/candidate file, never republish. |
| Strict bounds close False/escape; coordination close False/escape | Hard; candidate already final, no new publication/path work. |
| Any validated-body parent BaseException-only escape | Hard; normalize even SystemExit(0) to125 after remaining cleanup. |
| Validated-body boundary/final clock exception | Hard unproved clock; skip discretionary publication, finish owned cleanup. Probe/emergency use their own rows; initial preclassification entry has the explicit limitation above. |
| Total overrun with no hard cause | 124, preserve earlier reason/raw status; no new discretionary publication. |
| Post-cutoff stopped flag alone | Preserve cutoff decision; later actual hard/time failures still apply. |

Thus timeout+expected rmdir/receipt-invalid/lock-False remains124 without a close
failure; timeout+close_failed/unexpected coordination escape/result-close failure
is125. An observer-internal handled OSError after timeout is soft124, whereas the
same errno exposed by a main-owned B1 batch as False is hard125. No publication
can certify a later close, unlock or terminal exit; independent invocation evidence
remains required. Raw child0 with skipped or incomplete publication still returns125
via the missing-publication gate when there is no earlier timeout or hard cause;
valid raw status is considered only after all required gates pass. Source tests must
distinguish these event channels explicitly.

First independent prerequisite RED injects SystemExit(0) into a direct probe child
close; a distinct fake-exit sentinel is captured inside a short patch context, then
patch identities are restored before assertions. Complete literal helper/constructor
matrices and non-author source review precede shared-main tests/implementation.
Catchable-operation guarantees exclude arbitrary destruction/kernel hangs. Actual
profile/full/native/N4097/operations qualification is not conferred by this section.

## 7. Separate typed B1 evidence and consumer

Leave v1 public parser/binder/evaluator behavior unchanged, including rejecting skip/xfail.
Add B1-only APIs to `verification_os_contract.py`; shared strict syntax helpers may be reused
without changing v1 semantics. Old parser rejects new schema; B1 parser rejects v1.

B1 schema is `qwq.verification-process-result/v2`, scope=`local_b1_process_only`.
Same strict JSON limits64KiB/depth8/bounded integers/duplicate and unknown-key rejection.
Exact top-level keys are existing v1 keys plus `coordination`. `run`, `slot`, `identity`,
`receipt` retain their exact shapes and guard/producer hash relationships.

`launch` exact keys/literals:

```text
profile="b1-standard/v1"
process_scope="linux-subreaper/v1"
timeout_seconds=900
budget_profile="inclusive-lock-cleanup-publication/v1"
retained_stream_bytes=2097152
overflow_observed_bytes=2097153
pytest_profile="b1-x-fixed-plugins/v1"
environment_profile="b1-env-i-fake-key/v1"
selection_profile="tests-path-only/v1"
lock_profile="owner-ticket-workload/v1"
selection_sha256=<canonical normalized path list digest>
```

None of these redundant constants authorizes variation: exact types and literal equality are
required before comparing with an independently authored expected launch. bool-as-int rejects.
`process` keeps v1 fields and adds only `lock_timeout` to the B1 reason set.
Each stream exact keys: `bytes,sha256,overflow,observed_bytes`; bytes0..2097152,
observed0..2097153, bytes<=min(observed,2097152), overflow iff observed==2097153.
For a successful process, bytes==observed and overflow=false. Hash binds retained raw bytes.

`coordination` exact keys:

- `lock_acquired` exact bool; `lock_identity_stable` exact bool;
- `fake_key_absent_before` exact bool; `fake_key_absent_after` exact bool;
- `fake_directory_removed` exact bool; `fake_key_path_sha256` hex64 or null when never created.

Success requires all booleans true and a path hash. Failure documents may contain false/null.
Release completion and deadline-at-exit are deliberately not self-certified inside a document
written before those events: require independent invocation facts.

Proposed public signatures:

```python
parse_b1_process_result(raw: bytes) -> dict
validate_b1_process_receipt(receipt_raw: bytes, process_raw: bytes,
                            expected: dict, invocation: dict) -> tuple[str, ...]
evaluate_b1_process_slot(receipt_raw: bytes, process_raw: bytes,
                         expected: dict, invocation: dict) -> dict
```

`expected` exact shape remains `{verification,process_identity,launch}` but requires B1 launch.
Verification expectation is parsed by the existing strict expectation parser, independently
authored before the run; receipt-derived expected data is prohibited. Bind the matching slot's
run/timezone/lane/runtime/producer/inventory/collected identities and exact guard. Require receipt
session finished=true, exit_code=0, collection_errors=0,deselected=0, plus child OSrc0, no signal
flags/error/overflow, ownership/cleanup true and descendant_survived=false. Do **not** call a v1
outcome validator and discard its errors: implement explicitly limited process binding with
separate tests demonstrating that it makes no phase-outcome acceptance claim.

`invocation` exact caller-supplied shape is `{controller_returncode:int,elapsed_ns:int}`;
require rc0 and 0<=elapsed_ns<=900000000000, rejecting bool/negative/unbounded values.
`elapsed_ns` is a **conservative upper bound**, not an exact controller lifetime. The coordinator
completes an independent same-host monotonic-nanosecond timestamp read and receives its raw
terminal command receipt **before** launching the controller. Only **after** receiving the raw
terminal controller tool result does it begin the second same-host, same-clock timestamp read.
Derive elapsed_ns=post_ns-pre_ns as exact integer arithmetic; retain both clock command receipts
and link them in order to the unique run/attempt, launch tool receipt, every yielded session ID
and the terminal completion receipt. Confirm both clock reads use the same host and monotonic
clock domain/epoch. Reads are one-shot observations; no persistent helper, supervisor or reaper.

The bracket includes launch/poll scheduling, every orchestration gap, and any delay after
candidate publication through close/unlock/raw terminal observation. Never use
exec_command.wall_time_seconds, write_stdin wait fields, their sum, pytest duration, wall-clock
time or controller self-reported duration to replace or shorten it. A bracket above900 seconds
is failed/inconclusive evidence even if the actual workload was faster: no gap deductions,
retry for a shorter bracket, or cap expansion.

The coordinator must reject missing/invalid/reversed/unlinked clock or completion evidence,
host/clock-domain mismatch and nonterminal results **before constructing an invocation value**;
a standalone two-field invocation dict does not prove this provenance. The pure API checks its
strict supplied values; artifact acceptance additionally requires the preserved linked receipts.
This is local trusted-caller evidence, not external attestation. Do not infer controller rc from
the document or pytest rc. Caller also independently compares actual log sizes/hashes with stream
facts before accepting the artifact set. No clock command is authorized or executed by this draft.

Decision exact fields: schema=`qwq.b1-process-decision/v1`, status=`B1_PROCESS_BOUND` or `REJECTED`,
sorted unique errors, scope=`local_b1_process_only`, `outcomes_accepted=false`,
`native_qualified=false`, `ci_provenance_verified=false`, `production_eligible=false`.
Errors use existing fixed process mismatch classes plus `INVALID_INVOCATION`,
`B1_CONTROLLER_EXIT_REJECTED`, `B1_TOTAL_BUDGET_EXCEEDED`, `B1_COORDINATION_UNPROVEN`.
Never emit raw paths/env/exception text as error codes.

The current entire suite has16skip/2xfail and v1 `validate_receipt` rejects those. B1_PROCESS_BOUND
therefore proves process/receipt binding only, not semantic success or acceptance of skips/xfails.
Coordinator standard verification must retain its established exact outcome ledger separately;
this package adds no automatic skip/xfail exemption. N4097's dedicated semantic gate requires all
phases passed, exact one-node inventory and separate oracle review. Historical performance
exceptions remain exact, unchanged, and cannot excuse new timeout/overflow/failure.

## 8. Test and approval gates

Independent contract/test author owns literal expected profiles, v1 compatibility fixtures,
B1 adversarial inputs and synthetic acceptance tests. Controller/bootstrap author cannot approve
their own ownership changes. Both work from the same coordinator-frozen SHA in separate worktrees
and nonoverlapping files; one coordinator integrates. No fanout. Critical review is a separate
Astra/xhigh or verified Opus/high-or-xhigh reviewer; actual metadata/fallback must be recorded.

Use the existing independent test harness as the only safety owner around destructive synthetic
cases. Preserve its12-second case,1+2 cleanup,20-second outer observation and8-second fixture
backstops; lost harness/ECHILD evidence stops the batch. Never use the controller's cleanup code
to approve its own cleanup. Adapter tests cover hangs/unsupported ownership that cannot safely be
bounded in reality. Existing harness helper reuse may be extended, not duplicated.

Tests inside the normal suite must not wait on the global lock already owned by the suite launcher.
They use an isolated synthetic repository with a fixture-only rewritten fixed lock literal and
short clocks where needed; changed controller identity prevents these from qualifying the real
profile. This is test arrangement, never a public lock override or production bypass. A separate
coordinator-owned tiny smoke through the **unmodified actual profile** proves real common-lock,
env/guard/argv/receipt behavior after focused tests. No surrounding flock is used for that smoke.
Lock-contention tests assert no launch rather than weakening the profile. Final full verification
stays under the existing approved standard launcher until this new profile is independently
accepted; it is not self-approved by its own synthetic tests.

Reviewed copied-harness arrangement (policy only): coordinator may extend the existing
helper with exact-bool/default-false isolated and short-budget choices; short requires
isolated, invalid combinations reject before effects. Normal finite/flood/descendant
copies retain900. Isolated copies replace exactly one complete `_LOCK` declaration
with a new same-UID regular private fixture leaf, rejecting pre-existing/symlink paths.
Only duration-dependent timeout/contention copies may additionally replace exactly
`        self.total_end = started + 900` with `        self.total_end = started + 6`.
Check old/new counts1/0→0/1 and preserve run-minus4, cleanup3/total-minus1, TERM1,
lock240/startup10/probe1 and serialized/CLI900. Record transformed identity; no public
gate-removal replacement, arbitrary path/duration/source hook or new reaper is allowed.

Synthetic total6 gives relative run2/cleanup≤5/final6 durations. The alarm/harness
clocks have different origins:6<8<12 is not an absolute-deadline nesting or cold-start
proof. Require the intended workload/receipt checkpoint and actual raw cleanup/harness
evidence; prelaunch timeout or a backstop kill is not a passing workload-timeout case.
Missing/late proof or unexpected failure stops the batch, without retry-based tuning,
new exceptions or enlarged backstops. Short contention does not prove the real240
ceiling or necessarily lock_timeout. Dummy-cleanup proof and separate public enable
must precede real cases. Observer-fault/post-candidate-crash real fixtures still need
separately reviewed frozen-source fault anchors; adapter evidence is not real coverage.

Required adversarial coverage: both streams at2097151/2097152/2097153; chunk-spanning/last chunk;
simultaneous floods; short/failed log writes; old8388609 behavior; timeout inclusive of lock and
cleanup; startup after lock; nested lock/unsafe lock/no release on leader exit; parent/child env;
fake leaf race/replacement; exact argv/guard/path selector; raw rc/signal; child hang; double fork/
setsid/postleader zombie/adoption; missing EOF/ECHILD/lost status; observer retry/emergency path;
identity changes; receipt unavailable; result short-write/close failure; crash-after-publication;
strict schema/profile mismatch and unchanged skip/xfail policy.

The full suite is coordinator-only UTC then Asia/Seoul, with **all workers, including read-only
reviewers, stopped**. Tests/compile/secret scan are future authorized implementation checks,
not part of this draft. No new performance exception, retries after failed workloads, or cap rise.

## 9. Deferred N4097 execution

Runner approval alone does not authorize N4097. Required next gates: independent runner review
and real standard-profile evidence; separately approved B1 semantics/oracle/mutant package;
dedicated-file exact inventory; explicit total budget for dedicated1+UTC-full1+KST-full1, each900;
review of whether observed dedicated cost plus current full-suite cost actually fits both totals.
No additional focused-module large run, skip/deselect, N reduction, sampling, oracle relaxation,
timeout extension or retry. Any new failure stops subsequent runs. No N4097 file is created by
this runner package or by current B1b; until these gates are completed N4097 remains **UNRUN**.

Self-review: profile/cap names are fixed; old/new evidence cannot substitute for one another;
publication cannot certify future exit; global-lock nesting and full-suite outcome rejection are
explicit; no decoder semantic dependency or operational authority is introduced. Independent
review may revise this draft; the author makes no approval claim.

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
