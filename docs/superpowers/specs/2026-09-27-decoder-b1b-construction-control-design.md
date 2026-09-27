# Decoder B1b construction/control design

2026-09-27. Base `8e75340c3e436813136cafef3679810dc232bcc2`. Requested
gpt-6-astra/high; actual model/effective effort metadata unavailable, unverified;
fallback0/fanout0. This author does not approve the design. No implementation,
import, compile, collection, test, network, Git write, or operational action belongs
to this design task. Routine human pauses are superseded by the coordinator's
explicit autonomous-development instruction; independent critical review remains.

## 1. Intent, alternatives, and exact boundary

Build the smallest coherent, ordinary test-only S experiment that moves beyond
B1a EMPTY plumbing: semantic construction, incremental unique-key lookup, ordered
ENTRY/ITEM publication, and CONT push/end/pop. A fixture must construct and close
a nested value, retaining the completed private root. Maximum eventual claim:
`B1B_CONSTRUCTION_CONTROL_OBSERVED_ONLY`.

Chosen approach: keep B1a's five files byte-for-byte frozen; create a separate
five-file B1b module, load the frozen exact shell classes, and reuse only frozen
U1–U11 for empty CONT unlink. A separate B1b subject directly implements the new
branches. Extending the B1a observer would change its accepted EMPTY-only meaning;
copying its whole implementation would create an unnecessary second authority.
A retire-first slice lacks semantic destinations; full retire/finish/dispose now
would combine unresolved ownership contracts. Both alternatives are rejected.

No replacement, WORK, retire, finish, READY, known_failure, dispose, parser,
canonical/legacy consumer, N4097, source/native lane, cold RED, allocator/free A,
owner wiring, CI/config/main/operations. Root completion is private construction,
not a validation result, consumer handoff, successful cleanup, or actual free.
The partial scope does not close full R2. `TEST_ORACLE_DESIGN_UNRESOLVED /
RED_DEFERRED` remains for the first cold/native observation, not B1a's completed
basic RED; `native_qualified=false`, `source_execution_permitted=false`, qualified
runtime0, source108 call-phase0 remain unchanged.

Inputs read: current B1a design/plan/subject/observer, compatibility decision in
`docs/superpowers/specs/2026-09-27-recovery-decoder-compatibility-decision.md`, and
the complete sibling `transition-inventory.md`. The inventory's recommendation
to reuse all A rows is refined: A4–A6 clear construction holders too soon for
semantic initialization. B1b preserves the A0–A3 allocation/append ordering under
new phase names and explicitly retains `new` through semantic publication.

## 2. Files, exact objects, scalar contract

Create only `tests/structural_b1b/{subject,observer,fixtures,mutants,test_b1b_structure}.py`.
The subject author owns subject.py; a different author owns the other four.
Only test_b1b_structure loads modules, in this fixed order: frozen
`tests/structural_b1/subject.py` as `_qwq_b1b_shell`, then B1b subject, observer,
fixtures, mutants as `_qwq_b1b_subject`, `_qwq_b1b_observer`, `_qwq_b1b_fixtures`,
`_qwq_b1b_mutants`. Resolve from test_b1b_structure's own directory with pathlib;
register each in sys.modules before exec. Load once per process, and never replace
an existing module entry with a newly executed copy; an unexpected preexisting
module identity/path is a harness error. No __init__.py or path search. Distinct
module names prevent B1a's own test monkeypatches from affecting B1b. The unique
outer test basename avoids pytest's default prepend-mode collision with frozen
`tests/structural_b1/test_structure.py`; no import-mode or package change is needed.

subject.py aliases `Cell/Arena/Holders/Operation/Supervisor` from the frozen shell,
and delegates `make_supervisor` and `_check_identity` to it. No subclass or layout
copy. Its `_new_cell()` calls the aliased Cell exactly once. Its `_step` delegates
only phases U1–U11 to `_qwq_b1b_shell._step`; never shell submit/tick/A/U0. Independent
review verifies this narrow reuse. Four shared append assignments in the new
subject are justified by the changed continuation; do not copy B1a's full engine.

Frozen slots:

- Cell: `tag,payload,prev,next,s0,s1,s2,c0,c1,c2,c3,__weakref__`.
- Arena: `head,tail,root`; Supervisor: `active`.
- Operation: `token,arena,holders,phase,action,fault,outcome`.
- Holders: `new,replacement,cursor,unlink,left,right,work_head,cont_head,lookup,pending_key`.

All cells start EMPTY with every other slot None. No __dict__, __del__, custom
descriptor, callback, generator, recursion, or graph collection in the subject.
The action holds only an exact string name and an exact scalar payload. Local
cell references are a fixed number, never accumulated or retained between ticks.

| Tag | payload | semantic/order slots | control slots |
| --- | --- | --- | --- |
| EMPTY | None | all None | all None |
| NULL | None | all None | all None |
| BOOL | exact bool | all None | all None |
| INT | exact int, excluding bool | all None | all None |
| FLOAT | exact float, including -0.0 and nonfinite | all None | all None |
| STR | exact str, unchanged spelling | all None | all None |
| KEY | exact str, distinct from STR | all None | all None |
| OBJECT | None | s0=first ENTRY, s1=last ENTRY index, s2=None | all None |
| ARRAY | None | s0=first ITEM, s1=last ITEM index, s2=None | all None |
| ENTRY | None | s0=KEY, s1=value, s2=next ENTRY | all None |
| ITEM | None | s0=value, s1=next ITEM, s2=None | all None |
| CONT | None | all None | c0=open container, c1=completed unpublished child, c2=pending KEY for OBJECT only, c3=previous CONT |

Container s1 is an actual strong tail-index reference, not a second logical child.
The semantic ownership traversal follows container.s0, ENTRY.s0/s1/s2, ITEM.s0/s1;
it checks the tail index separately against the last ordered record. First=last
is a declared alias, not duplicate ownership. A value may temporarily be reached
from source and destination during the precisely listed handoff rows. No other
semantic/control cross-parent sharing or cycle is valid. Allocation prev/next
links and their cycles are a separate retention domain, never semantic edges.
Future full-R2 retire/known_failure/dispose tables must explicitly order removal
of the strong container.s1 tail index, enqueue each logical child only once, and
prove the final tail alias absent, including every before/after fault boundary.
Ignoring s1 can retain a removed record; treating it as another semantic child
can process that record twice. This obligation adds no B1b cleanup implementation.

Exact NULL/BOOL/INT/FLOAT/STR are accepted without size caps; fixtures are small.
Float snapshots encode exact binary64 bytes with `struct.pack('!d', value).hex()`;
this distinguishes bool/int/float and negative zero and allows NaN observation
without NaN equality. Input scalar conversion, large string/int materialization,
key comparison cost, allocator/GC cost, and actual release remain unqualified.
No finite validation occurs: NaN/±Infinity construct successfully and remain
private. Future finish must validate only the surviving tree; overwritten
nonfinite semantics and duplicate replacement are not exercised here.

Import closure: frozen shell module, the five B1b files, stdlib
dataclasses/weakref/typing/struct plus loader importlib.util/sys/pathlib, and pytest.
Only test_b1b_structure may additionally read os.environ for the fixed raw-mutant
selector. No product import, SQL/native/foreign package, or source-proof import.

## 3. Fixed API, admission, and unsupported duplicate boundary

```python
Scalar = bool | int | float | str | None
Action = tuple[str, Scalar]
def make_supervisor(token: tuple[int, int, int, int]) -> Supervisor: ...
def submit(supervisor: Supervisor, operation: Operation,
           token: tuple[int, int, int, int], action: Action) -> None: ...
def tick(supervisor: Supervisor, operation: Operation,
         token: tuple[int, int, int, int]) -> None: ...
def _step(operation: Operation) -> None: ...
def _new_cell() -> Cell: ...
```

The token is the original exact tuple `(1,2,3,4)` whose members are exact ints;
every submit/tick uses the original operation supplied by the driver and checks
`supervisor.active is operation` and `operation.token is token`. Value equality
cannot authorize work. Bootstrap allocates Cell0. Public make_supervisor/submit/
tick do not accept or return Cell handles; the private `_new_cell() -> Cell`
constructor is the required internal exception to the Cell-return restriction.

All admitted submits require IDLE/action=None/fault=None/outcome=None, new/cursor/
unlink/left/right/lookup/pending_key/replacement/work_head=None. If root is set,
all actions are rejected: this probe has completed its sole private root. At an
open boundary, cont_head is a CONT whose c0 is OBJECT/ARRAY; c3 is None or the
previous CONT. These are reachable states produced by this subject, not a public
graph repair API. Reentrant calls, direct caller graph edits, and subclass values
are unsupported. The observer catches corruption; submit need not scan the arena.

| Exact action | Additional admission | First phase / action-end condition |
| --- | --- | --- |
| (`start_object`, None) | no CONT and no root, or a ready parent value slot | O0 / new CONT owns an open OBJECT |
| (`start_array`, None) | same | O0 / new CONT owns an open ARRAY |
| (`scalar`, exact Scalar) | same | V0 / parent.c1 or private root owns value |
| (`key`, exact str) | current OBJECT, c1=c2=None | L0 / unique KEY in current.c2; or explicit unsupported outcome |
| (`publish`, None) | current.c1 is a completed value; OBJECT c2=KEY or ARRAY c2=None | P0 / ENTRY/ITEM ordered, c1=c2=None |
| (`end`, None) | current CONT exists and c1=c2=None; parent destination ready | D0 / current CONT emptied/unlinked, its container in parent.c1 or root |

A ready parent value slot means parent.c1=None; OBJECT additionally has c2=KEY,
ARRAY c2=None. Starting a nested container reserves this slot logically while
the child's CONT is top; the parent cannot be addressed by any action until the
child ends. `end` checks the parent (current.c3), not the current container, for
destination readiness. An object cannot end with a pending key or uncommitted
child. An array cannot end with an uncommitted child. `publish` is explicit;
scalar/end never silently append an ENTRY/ITEM. Empty root OBJECT/ARRAY are valid.

Malformed tuple/name/payload, unsupported action, wrong type/subclass, no open
parent, missing key/value, premature end, busy/idle misuse, duplicate key while
another key is pending, second root, wrong identity, latched fault/outcome:
preflight ValueError with constant message, constructor0 and exact state0 on
both original and supplied foreign operations. Do not repr user input. Validation
finishes before action/phase assignment. submit itself mutates no graph slots.

Key uniqueness is resolved incrementally *before* allocating KEY. A hit at L1
clears lookup and sets `outcome='B1B_UNSUPPORTED_DUPLICATE'`, phase=IDLE,
action=None. Subsequent submit/tick reject without mutation. Existing semantic
tree/CONT/key slots are unchanged, key action constructor0. This is not an
unchanged-admission rejection: lookup moved during the action, and phase/action/
outcome changed at its endpoint. This is deliberately
retained, unsupported input to a duplicate-free experiment, not known_failure,
cleanup, parser rejection, or an alternative legacy duplicate policy. No private
root can be completed after that outcome. Future full B1 must implement last-wins
at the first key position; it may not preserve this probe restriction as parity.

## 4. Row convention and fault rule F

Phase names denote the next row to execute. A tick executes exactly one row and
returns. Every row has one actual cell/arena/holder slot assignment; tag/payload
are counted too. Scalar phase/action/outcome updates are separate. Only Q0 has
the B1a exception: constructor reached1/returned1, new generation1, and new=n in
one S observation; constructor internals/return-to-holder gap remain outside A.
All other rows constructor0/new generation0. Even None→None is one assignment.

Symbols: h=holders, a=arena, n=h.new, t=append's old tail, c=h.cont_head,
o=c.c0, p=c.c3, v=completed value, k=pending KEY, x=removed CONT. Symbols
describe actual slots, not new persistent hidden holders. Each row preserves
every unlisted slot; aliases inherited from earlier rows remain only until the
listed clearing row. Allocation-chain aliases always remain permitted. `IDLE`
after a final row also clears action; normal outcome stays None. Every row below
uses F, including no-op writes, lookup branches, and final action rows.

F: wrapper around B1b `_step` raises an identical foreign Exception immediately
before or after a selected row. tick retains that exact object in fault and sets
DISPOSAL_FAULT; graph, action and phase remain exactly at that boundary. Later
submit/tick mutation0. Before-Q0 has no constructor/new generation; after-Q0 has n
in new. After-final faults retain phase=IDLE/action=None but are not success.
Exception args/context/cause/traceback/frame remain FAULT_RETAINED, not a cleanup
claim. No str/repr, frame clearing or forced GC. Fault-slot failure/OOM progress
unproved. ObserverAbort(BaseException) bypasses this catch, leaves fault/outcome
unlatched and records INCONCLUSIVE_OBSERVER, never intended RED or subject PASS.

## 5. Complete finite transition families

The Q table expands to exactly Q in `{V,O,C,K,P}` and suffixes 0–3, with no
user-supplied instruction stream. The subject uses direct branch logic against
operation state and action, never expected tables or a graph-write interpreter.
Fixtures expand these finite rows independently into literal states.

### Allocation/append: Q=V scalar, O container, C CONT, K KEY, P publication record

| Before / requirement | One assignment | After | Required holder/allowed aliases; fault |
| --- | --- | --- | --- |
| Q0; new=None, valid action context | h.new=_new_cell() | Q1 | n solely new before chain admission; cursor may hold container only for C; F |
| Q1; n EMPTY/unlinked | n.prev=a.tail | Q2 | new=n; old tail retained by chain; F |
| Q2; old tail=None | a.head=n | Q3 | head/new=n, tail=None transiently; F |
| Q2; old tail=t | t.next=n | Q3 | t.next/new=n; n.prev=t, tail still t; F |
| Q3 | a.tail=n | Q4 | chain symmetric, new/tail=n; F |

Q2 empty is reachable for O/V root construction only; C/K/P always append to a
nonempty chain. Observer accepts only the enumerated branch's exact anchor
transient, not a blanket allowance for inconsistent chains.

### Scalar V and open container O/CONT C

| Before / requirement | One assignment | After | Required holder/allowed aliases; fault |
| --- | --- | --- | --- |
| V4; n EMPTY | n.tag=exact scalar tag from action[1] | V5 | new=n; payload still None permitted only here; F |
| V5 | n.payload=action[1] | V6 | exact scalar payload; F |
| V6; c exists, c.c1=None | c.c1=n | V7 | c.c1/new=n; F |
| V6; c=None, root=None | a.root=n | V7 | root/new=n; F |
| V7 | h.new=None | IDLE | destination retains v; F |
| O4; n EMPTY | n.tag=OBJECT or ARRAY, per start action | O5 | new owns open empty container; F |
| O5; cursor=None | h.cursor=n | O6 | new/cursor=container; F |
| O6 | h.new=None | C0 | cursor=container, old cont_head unchanged; F |
| C4; new CONT-to-be EMPTY | n.tag='CONT' | C5 | new=n,cursor=container; F |
| C5; n.c0=None | n.c0=h.cursor | C6 | n.c0/cursor=container; F |
| C6; n.c3=None | n.c3=h.cont_head | C7 | new CONT owns prior CONT link (possibly None); F |
| C7 | h.cont_head=n | C8 | cont_head/new=n; c3 retains previous CONT; F |
| C8 | h.cursor=None | C9 | new/cont_head CONT owns container via c0; F |
| C9 | h.new=None | IDLE | only top CONT ownership plus chain; F |

At C0–C3 cursor cannot be cleared or overwritten: it is the construction domain
for the open container while new owns the CONT allocation. No completion value
uses cursor in this table. Open containers never enter their parent's semantic
graph and arena.root remains None until top-level end.

### Lookup and KEY: at most one existing ENTRY comparison per L1

| Before / requirement | One assignment | After | Required holder/allowed aliases; fault |
| --- | --- | --- | --- |
| L0; lookup=None, c OBJECT, c1=c2=None | h.lookup=o.s0 | L1 if head exists, otherwise K0 | lookup/semantic ENTRY alias or None; F |
| L1; lookup=e and e.s0.payload != action[1] | h.lookup=e.s2 | L1 if successor exists, otherwise K0 | exactly one-entry advance, chain/key unchanged; F |
| L1; lookup=e and e.s0.payload == action[1] | h.lookup=None | IDLE plus B1B_UNSUPPORTED_DUPLICATE | semantic/CONT unchanged, no KEY allocation; F |
| K4; lookup=None, n EMPTY | n.tag='KEY' | K5 | new=n; payload pending; F |
| K5 | n.payload=action[1] | K6 | exact str; F |
| K6; c.c2=None | c.c2=n | K7 | c.c2/new=k; F |
| K7 | h.new=None | IDLE | c.c2 owns uncommitted KEY; F |

L1 comparison is one exact-str equality; it has no graph side effects. The next
phase is selected using the new lookup slot after its one assignment. Key length
cost remains unbounded. Lookup is always None by KEY allocation and every action
boundary. `pending_key` is deliberately unused/always None: c.c2 alone is the key
holder, so there is no redundant key handoff. No dictionary/set of keys exists.

### ENTRY/ITEM publication P

Before P0, c.c1=v, and object c.c2=k or array c.c2=None. P allocation retains
those sources. The old last record is o.s1 until P8 changes it.

| Before / requirement | One assignment | After | Required holder/allowed aliases; fault |
| --- | --- | --- | --- |
| P4; n EMPTY | n.tag='ENTRY' for OBJECT, otherwise 'ITEM' | P5 | new owns unpublished record; F |
| P5; OBJECT | n.s0=c.c2 | P6 | KEY in c.c2 and new.s0; F |
| P5; ARRAY | n.s0=c.c1 | P7 | value in c.c1 and new.s0; F |
| P6; OBJECT | n.s1=c.c1 | P7 | value in c.c1 and new.s1; F |
| P7; o.s0=None | o.s0=n | P8 | head/new=n, tail=None until P8; sources still held; F |
| P7; OBJECT and old tail=e | e.s2=n | P8 | e.next/new=n; old tail index remains e; F |
| P7; ARRAY and old tail=i | i.s1=n | P8 | i.next/new=n; old tail index remains i; F |
| P8 | o.s1=n | P9 | tail index/new=n, head equals tail iff first; F |
| P9 | c.c1=None | P10 for OBJECT, P11 for ARRAY | published record now owns v; F |
| P10; OBJECT | c.c2=None | P11 | published ENTRY owns k; F |
| P11 | h.new=None | IDLE | ordered destination owns record; c1=c2=None; F |

No published record is missing a key/value at P7. Tail index catch-up P7→P8 is
the only temporary ordered-container tail mismatch. The next record pointer is
None from construction and is never bulk rewritten. ENTRY first key order and
ARRAY element order/duplicates are preserved. Lookup completes before new value
construction can begin because key action must finish and c2 must exist first.

### Child end, CONT pop, and exact scratch collision resolution

At admission c.c1=c.c2=None, c.c0=v is a completed container. Parent p, if any,
still owns its own open container and has c1=None plus the correct key state.

| Before / requirement | One assignment | After | Required holder/allowed aliases; fault |
| --- | --- | --- | --- |
| D0; p exists | p.c1=c.c0 | D1 | child.c0/parent.c1=v; child still top CONT; F |
| D0; p=None, root=None | a.root=c.c0 | D1 | child.c0/root=v; F |
| D1; cursor=None | h.cursor=h.cont_head | D2 | cursor/top=x, destination owns v; F |
| D2; cursor=x | x.c0=None | D3 | destination alone holds v outside chain; x.c1=c2=None; F |
| D3; x.c0=c1=c2=None | h.cont_head=x.c3 | D4 | previous CONT in cont_head/x.c3; x retained by cursor; F |
| D4 | x.c3=None | D5 | x all semantic/control slots None; F |
| D5; x.payload=None | x.tag='EMPTY' | U1 | cursor owns empty control cell, destination still v; F |
| U1; cursor=x | h.unlink=h.cursor | U2 | cursor/unlink=x; F |
| U2; left=None | h.left=x.prev | U3 | x retained; L in left/chain; F |
| U3; right=None | h.right=x.next | U4 | x retained; R in right/chain or None; F |
| U4; L exists | L.next=R | U5 | x cursor/unlink; old x links retained, R.prev=x transiently; F |
| U4; L=None (frozen primitive branch, unreachable in valid B1b) | a.head=R | U5 | x cursor/unlink; F |
| U5; R exists | R.prev=L | U6 | surviving chain restored; x old links retained; F |
| U5; R=None | a.tail=L | U6 | surviving tail restored; x cursor/unlink; F |
| U6 | x.prev=None | U7 | x cursor/unlink, neighbors held; F |
| U7 | x.next=None | U8 | x all reference slots None; F |
| U8 | h.left=None | U9 | x cursor/unlink, R right/chain; F |
| U9 | h.right=None | U10 | x cursor/unlink, neighbors chain; F |
| U10 | h.unlink=None | U11 | x cursor only; F |
| U11 | h.cursor=None | IDLE | x no subject graph path, completed v retained; F |

Every B1b CONT has a preceding container cell, so it is never chain head/single.
Empty-container end exercises tail unlink; a nonempty closed container exercises
middle unlink. B1a retains evidence for head/single; B1b does not manufacture those
states or count unreachable branches as tested.

Scratch collision proof: `new` is used for construction only, None throughout
end. `cursor` holds the open container only O5–C8, and holds the emptied CONT only
D1–U11; these are disjoint actions. The completed child moves into parent.c1/root
at D0 *before* cursor is acquired, never into cursor. `unlink/left/right` are
unused until U1/U2/U3 and released by U10/U8/U9. `lookup` exists only during key
lookup. `replacement/work_head/pending_key` remain None in all states. No completed
value is parked in a scratch holder that frozen unlink will clear. The parent
CONT remains in cont_head after D3, even while its child CONT is physically
removed from the allocation chain. Later publish uses parent.c1. A fault at any
row preserves both the destination and every still-required CONT/neighbor alias.

## 6. Independent observation and finite acceptance fixtures

Census wraps B1b `_new_cell` exactly once and records weakrefs/scalar monotonic
generations only. Snapshot reads actual exact slots and returns only scalar
identities and tagged payload scalars. It separately reconstructs allocation,
semantic/order, open-CONT, completed-child, pending-KEY, construction and unlink
domains. No subject phase/lookup/traversal/completion helper is an answer oracle.
In-flight tag/payload/edge incompleteness is allowed only in its literal row.
After frame return, the independent live census must equal the literal retained
generations: semantic tree plus open controls, never automatically the whole
allocation chain and never universally empty. Removed CONT generations must be
absent from both graph and live census after normal driver frames return. This
is reference absence in S, not actual native death.

Independent fixtures may use a renderer to apply explicit literal slot patches
to a previous *expected* state, but never derive expected values/branches/targets
from actual before state, subject helpers, or subject phase dispatch. Every case
has a fixed action list and full expected states including submit boundaries.
Each before/after comparison covers all slots, tag/type/payload, anchors, holders,
phase/action/outcome, fault identity, constructor reached/returned, generation,
ordered membership and allowed aliases. Missing/extra/dead generations fail.
Fault cases use the same expected row and change only fault/outcome as F requires.
Normal and mutant runs use the identical assertion path.

Required small literal cases (no product caps):

1. Each scalar root: None, False, True, 0, 1, -7, 1.0, -0.0, `''`, `'1.00'`,
   `'한글\\n'`; separate FLOAT NaN/+Infinity/-Infinity retained cases.
2. Empty root OBJECT and ARRAY: generation1 container retained, generation2 CONT
   removed. Root not READY; new root request rejected unchanged.
3. ARRAY `[True, True, -0.0]`: value/item generations3/4,5/6,7/8; end removes
   CONT2 and retains `(1,3,4,5,6,7,8)` in allocation order, ITEM order4→6→8.
4. OBJECT keys `b,a,c` with values True,7,`'1.00'`: KEY/value/ENTRY generations
   3/4/5,6/7/8,9/10/11. Key b lookup is empty; key a compares ENTRY5 once;
   key c compares ENTRY5 then ENTRY8 on two separate ticks. Final entry order
   5→8→11, first key order b,a,c; CONT2 removed.
5. Nested root OBJECT with key `a`, child ARRAY containing empty OBJECT and
   scalar7. Exact actions: start_object, key(a), start_array, start_object,
   end, publish, scalar(7), publish, end, publish, end. Generations:
   root1/CONT2/KEY3/ARRAY4/CONT5/OBJECT6/CONT7/ITEM8/INT9/ITEM10/ENTRY11.
   CONT7 tail unlink first, CONT5 middle unlink next, CONT2 middle unlink last;
   final retained `(1,3,4,6,8,9,10,11)`. This pins two nontrivial child-to-parent
   handoffs and publication after unlink without hidden scratch retention.
6. Duplicate attempt after `b,a,c`: key(b), key(a), key(c) in separate fresh
   open-object fixtures, covering hit at first/middle/last ENTRY. Constructor0
   for rejected key action; semantic state unchanged; outcome unsupported;
   subsequent scalar/end/submit/tick mutation0.
7. Every reachable row/branch above, foreign fault immediately before and after;
   reconstruct fresh small fixture to each boundary. No fault instance is reused
   across cases. No normal terminal-live assertion applies to FAULT_RETAINED.

Harness maximum1000 ticks per case; exceeding it is INCONCLUSIVE, not a product
input cap or successful test. First structural violation returns one fixed code
after driver frames return. Observer failures abort distinctly; a witnessed
uncensused allocation is INCONCLUSIVE_COVERAGE, not a subject RED. An extra
external alias to the removed CONT is a valid structural negative control because
the normal predicate expects that specific CONT generation absent, while its
completed child remains present. Do not delete the root or invoke GC to pass.

## 7. Actual negative controls and remaining gates

Required executable mutant names: `clear_container_cursor`, `push_cont_early`,
`clear_child_before_destination`, `pop_cont_with_live_child`, `cursor_value_collision`,
`skip_lookup`, `double_lookup_advance`, `clear_lookup_early`, `entry_missing_value`,
`clear_value_before_publish`, `entry_wrong_order`, `item_wrong_order`, `key_as_str`,
`bool_as_int`, `negative_zero_lost`, `duplicate_admitted`, `cont_external_alias`.
Each mutates an actual assignment/phase/call/alias, not counts or expected data.
They must fail the normal full-state predicate at a call-phase assertion; exact
fixture mapping is in the plan. Identity rejection is a positive guard check,
not a raw mutant. Double-write-and-restore remains a snapshot limitation shown
by a separate source-audit control, not a fabricated snapshot kill.

Source review binds each row/branch to actual source lines and SHA, including
delegated U rows, to verify exactly one physical assignment. Snapshot deltas
alone do not prove it. Every normal action and every reachable fault boundary
must have explicit coverage before B1b may be accepted.

Full R2 must still freeze replacement, WORK seed/pop, retire, finish, known_failure,
dispose and their faults; preserve last-wins/first-key order, overwritten vs
surviving nonfinite, nested outer-old a/inner b, partial cleanup, and single retire
batch. Large N4097 remains unimplemented/UNRUN with unchanged full oracle and
dedicated1+UTCfull1+KSTfull1 sequence. Approved independent 2MiB/stream capped
runner remains unavailable; ordinary small profile cannot substitute. No runner
development, native install, source collection, large test/skip/deselect, or cap
increase. Existing 256facts/5ms, raw50ms, restore1000ms/ticket1500ms and both exact
historical performance exceptions retain their existing meanings.

## Coordinator plan-review disposition

Independent requested Astra/xhigh review returned CHANGES_REQUIRED for the new test
basename collision; revised unique `test_b1b_structure.py` plus tail-index cleanup
handoff and private constructor wording were re-reviewed as APPROVE_B1B_PLAN_ONLY.
The original and scoped re-review are preserved under
`.superpowers/sdd/2026-09-27-decoder-followup/`. Actual model/effective effort are
unverified; same-provider independence is not cross-provider qualification.
This permits the bounded test-only Plan→Do→See sequence under the user's delegated
continuation, not a claim of implementation success or native/operational authority.
