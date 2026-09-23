# N5 Recovery Projection·Producer Index Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** 누적 실행 이력과 무관한 bounded recovery projection/index를 만들고, 정확한 owner token과
RAM observation을 결합해 producer·diagnostic의 반복 full-state 복사를 제거한다.

**Architecture:** durable owner는 typed edit에서 `OwnerRecoveryProjection`,
`OwnerRecoveryJoinView`, `ProducerRecoveryIndex`를 만들고 동일 token으로 게시한다. runtime-owned
composition coordinator는 producer episode와 transient failure를 O(1) delta로 B frame에 합성하며,
진단은 A-B-A-B, producer는 FIFO gate에서 받은 exact-token view만 사용한다. 일반 거래 경로는
증분 budget을 넘으면 commit 전에 거부하고, full build는 restore와 활성화 전 migration에만 쓴다.

**Tech Stack:** Python 3.12, asyncio, frozen dataclasses, SQLite existing store, pytest,
`perf_counter`, `tracemalloc`; 새 외부 dependency 없음.

**Spec:** `docs/superpowers/specs/2026-09-24-recovery-projection-index-design.md`

**Plan review:** non-author Astra/xhigh requested; blocking 0, advisory 0. Actual runtime model metadata was not
exposed and is recorded as unverified. Human execution-method selection remains required before product code starts.

## Global Constraints

- 기준 SHA는 `cdd42df53b80b3d7372bdaf926cd5edb44d54ad8`; 승인 spec을 조용히 변경하지 않는다.
- Plan → Do → See. coordinator 한 명만 통합하며 critical 변경 작성자는 자기 변경을 승인하지 않는다.
- 동시 작업자는 coordinator 포함 최대 4명. 병렬 writer는 같은 base SHA의 별도 worktree와 겹치지 않는
  파일만 사용한다. worker fan-out은 금지한다.
- 기본 경로에서 실 API, 자격증명, 운영 state, SSH, 주문, 배포, 재시작, 설정 변경을 사용하지 않는다.
- 제품 legacy `owner.mutate` route는 N5-D에서 0, 일반 route full-builder 호출 0,
  capture/producer `owner.state` 호출 0이어야 한다.
- 정상 증분 budget은 `RowChange <= 64`, distinct dependency edge `<= 256`; 65/257은 evidence를
  자르지 않고 store/publisher/full-builder 호출 0으로 거부한다.
- full builder는 restore와 명시적 preactivation shadow migration에서만 한 번 직렬 실행한다.
  background/latest-token rebuild와 stale full-scan fallback은 금지한다.
- public `counts_complete`는 stable/current/publication/owner/RAM/binding이 모두 맞고 최종
  `evidence_invalid`가 없을 때만 true다. incomplete에서 부재 code는 0이 아니다.
- fixed hold budgets: lookup/no-commit 1,000ms, single owner commit 1,500ms, two-commit fill ticket 2,500ms,
  restore/migration 1,500ms, producer-view critical section 5ms. 각 commit/restore 본체는 1,000ms 이하다.
- performance gate는 normal update 10ms, producer warm 10ms, producer cold 50ms, cached read 5ms,
  diagnostic+JSON 5ms, continuous stall 50ms, public JSON 16KiB를 그대로 사용한다.
- RED 명령·exit·실패 이유를 task review ledger에 기록하되 실패 tip을 원격에 남기지 않는다.
  GREEN·검토된 task commit만 push한다.
- 전체 pytest는 모든 worker가 끝난 뒤 coordinator가 UTC→KST 순서로 직렬 실행한다. 기존 xfail만
  허용하며 두 timezone 결과를 독립 test 수로 합산하지 않는다.
- 실제 model metadata가 없으면 requested model을 actual이라고 쓰지 않는다. critical final review는
  작성자가 아닌 Astra/xhigh 또는 실제 identity가 검증된 Claude Opus/xhigh가 맡는다.
- 이 저장소의 비동기 테스트는 최상위 동기 `def test_*` 안에서 `asyncio.run(scenario())`를 호출한다.
  `pytest-asyncio`나 plugin autoload에 의존하는 최상위 `async def test_*`를 추가하지 않는다.
- 각 구현 dispatch는 한 task와 명시된 파일 allowlist만 소유하고 최대 45분 뒤 coordinator에게
  중간 상태를 반환한다. Task 9–11의 돈·원장 상태 변경은 Astra/high 작성, 비작성자 Astra/xhigh 또는
  검증된 Opus/xhigh 승인으로 격상한다.

## Failure-Safe Git Protocol

- 각 RED/GREEN 명령은 git 쓰기와 분리된 단독 실행으로 수행하고 명령·exit code·실패/성공 요약을
  task ledger에 기록한다. RED가 예상 원인으로 실패하지 않거나 GREEN이 0이 아니면 즉시 중단한다.
- 독립 리뷰가 blocking 0을 반환한 뒤에만 `git status --short`, task에 적힌 allowlist에 대한
  path-scoped `git diff --check`와 **실제로 변경된 경로만** `git add --`, `git diff --cached --check`,
  `git diff --cached --name-only`를 차례로 실행한다. reviewer는 변경된 경로의 diff를 모두 읽고 승인한다.
  `actual staged paths == independently reviewed changed paths`이고
  `changed paths ⊆ task allowlist`여야 한다. 선택적 경로가 변경되지 않았다는 이유로 staged 목록에 없을 수는
  있지만, 변경된 경로를 리뷰·stage에서 누락하거나 allowlist 밖 경로를 포함하면 중단한다. 자동
  reset/restore/stash/clean은 하지 않는다.
- commit은 task에 적힌 메시지와 literal path 목록을 붙인 path-scoped `git commit`으로만 만들고
  SHA와 clean/expected-dirty 상태를
  확인한 뒤 별도 단계에서 push한다. `git add .`, `git add -A`, `git commit -a`는 금지한다.
- 아래 git code block은 모두 `set -euo pipefail`을 사용하며 앞선 GREEN과 독립 승인 결과가 기록된
  뒤에만 실행한다. 테스트와 git mutation을 같은 shell invocation에 붙이지 않는다.

## Review Focus

1. stale quote·duplicate admission·invalid encoding·builder failure가 pre-submit에서 기존 pointer,
   token, epochs, revision, health를 바꾸지 않는가 — Tasks 4–5.
2. `_block()`이 PREPARING/store wait/publisher 중 끼어도 ordinary completion이 health를 되살리지
   않는가 — Tasks 4–6.
3. bool/float/Decimal episode quantity, episode list, 잘못된 identity가 정수1로 합쳐지지 않고
   unavailable+invalid가 되는가 — Tasks 1, 12, 14.
4. prepare→claim 자체 commit은 send lease를 전진시키되, fault/day/fence/relevant change는 POST를
   막고 이미 claim한 ACK/REJECTED/NOT_SENT/UNKNOWN은 끝까지 기록하는가 — Task 8.
5. 5,000 retained events와 timeout/noisy host가 빠른 성공이나 0ms로 포장되지 않고 63셀의 실제 제품
   copy/encode/publish 비용 안에서 판정되는가 — Task 15.

---

## File Structure

### New product modules

- `src/execution/safety/recovery_projection.py` — immutable owner facts, token, full builder,
  join/index, scoped `ProducerReadView`.
- `src/execution/safety/mutation_plans.py` — typed edits, owner-applied candidate, `RowChange`, bounded
  incremental projection update.
- `src/execution/safety/fill_mutation.py` — economics/protection/initial-R/replay의 immutable
  `MutationFragment`와 fill plan assembly.
- `src/execution/safety/owner_ticket_gate.py` — cancellable ticket-FIFO owner admission and hold metrics.
- `src/execution/safety/writer_authority.py` — staged `WriterLease`, one-shot `ResultDrainToken`, receipts.
- `src/execution/safety/recovery_composition.py` — runtime B frame, O(1) episode algebra, A-B-A-B reader.

### New tests and fixtures

- `tests/test_recovery_projection.py`
- `tests/test_execution_mutation_plans.py`
- `tests/test_execution_owner_ticket_gate.py`
- `tests/test_recovery_projection_faults.py`
- `tests/test_recovery_projection_inventory.py`
- `tests/fixtures/recovery_mutation_inventory.json`
- `tests/test_recovery_projection_acceptance.py`
- `tests/test_protection_producer_recovery_index.py`
- `tests/test_execution_fill_mutation_plans.py`
- `tests/test_recovery_projection_mutation_kills.py`
- `tests/recovery_projection_scale_harness.py`
- `tests/test_recovery_projection_performance.py`
- `scripts/dev/run_recovery_projection_mutation_kills.py`
- `scripts/dev/run_recovery_projection_scale.py`

### Existing product files modified in order

- Owner spine: `application.py`, `policy_generations.py`, then `runtime.py::restore`.
- Authority/lifecycle: `commands.py`, `lifecycle.py`, `gateway.py`; `transport.py` behavior remains unchanged
  unless a RED demonstrates a defect.
- Fill writer: `application.py`, `runtime.py`, `economics.py`, `lifecycle.py`, `protection.py`,
  `initial_r.py`, `protection_recovery.py`, `journal_delivery.py`.
- Risk/regime writers: `commands.py`, `risk_sources.py`, `risk_input_seal.py`, `intraday_owner.py`,
  `risk_transition.py`, `protection_recovery.py`, `regime_owner.py`, `regime_commands.py`,
  `regime_application.py`, `regime_morning.py`, `regime_morning_commands.py`, `regime_horizon.py`.
- Consumer switch: `recovery_composition.py`, `protection_producer.py`, `core/engine.py`,
  `recovery_capture.py`, `recovery_diagnostics.py`, `factory.py`.

### Evidence and final documentation

- `docs/reviews/data/recovery-projection-scale-2026-09-24.json`
- `docs/reviews/data/recovery-projection-mutation-kills-2026-09-24.json`
- `docs/reviews/recovery-projection-scale-2026-09-24.md`
- `CHANGELOG.md`, `docs/README.md`, `CLAUDE.md`,
  `docs/operations/p1-next-steps-2026-09-23.md`

## Ordered Ownership

| Stage | Author route | Required independent gate |
|---|---|---|
| Tasks 1–2 | Terra/high; pure bounded modules | Sol/high API/parity review |
| Task 3 | Terra/high; isolated gate module | Sol/high cancellation/fairness review |
| Tasks 4–7 | Astra/high; owner critical spine | Astra/xhigh or verified Opus/xhigh |
| Task 8 | Astra/high; send/result authority | cross-provider verified Opus/xhigh preferred, else Astra/xhigh |
| Tasks 9–11 | Astra/high; economic/ledger writers, serial | Astra/xhigh or verified Opus/xhigh per task |
| Tasks 12–14 | Astra/high; runtime authority/composition | Astra/xhigh whole-path review |
| Task 15 | Terra/high harness, coordinator evidence | Sol/high raw-data review + Astra/xhigh final |

Tasks 2 and 3 may run in parallel after Task1 is integrated because product files do not overlap. Tasks 9–11 are
implemented serially in numeric order because they share the checked-in inventory and acceptance evidence; only
their read-only reconnaissance may overlap. Every other task is the ordered critical spine.

---

### Task 1: Immutable Owner Models and Full Builder

**Files:**
- Create: `src/execution/safety/recovery_projection.py`
- Create: `tests/test_recovery_projection.py`

**Interfaces:**
- Consumes: current checkpoint dict shape and the approved semantic parity scope.
- Produces: the canonical recursive immutable `FrozenJSON` value alias, `RecoveryProjectionMode`, `OwnerToken`,
  frozen fact DTOs, `OwnerRecoveryProjection`, `OwnerRecoveryJoinView`,
  `ProducerRecoveryIndex`, `ProducerReadView`, `FullBuildResult`, full builders.

- [ ] **Step 1: Write the token, detachment, semantic-count, scoped-query, 5,000-row and cancellation REDs**

```python
def test_owner_token_rejects_boolean_epochs():
    with pytest.raises(ValueError, match="invalid_owner_token"):
        OwnerToken("owner-a", True, 0, 1)


def test_full_builder_is_detached_and_matches_uncapped_owner_oracle():
    state = owner_fixture_with_all_supported_findings()
    token = OwnerToken("owner-a", 4, 1, 20)
    result = build_owner_recovery_models(state, token=token)
    state["attempts"].clear()
    assert dict(result.projection.findings) == uncapped_owner_counts(owner_fixture_with_all_supported_findings())
    assert result.index.attempts_for_symbol("005930")


def test_scoped_lookup_does_not_iterate_unrelated_history(monkeypatch):
    result = build_owner_recovery_models(history_fixture(5_000), token=OwnerToken("a", 0, 0, 1))
    monkeypatch.setattr(result.index, "all_attempts", forbidden_history_iteration, raising=False)
    assert len(result.index.attempts_for_intent("intent-4999")) == 1
```

Add exact RED names:
`test_full_builder_owner_codes_match_uncapped_n3_oracle`,
`test_malformed_orphan_duplicate_and_missing_links_are_not_dropped`,
`test_consumer_cannot_mutate_fact_or_change_sibling_view`,
`test_same_revision_different_incarnation_is_not_the_same_model`,
`test_cooperative_builder_yields_every_256_facts_or_5ms`, and
`test_cancelled_full_build_never_returns_partial_models`,
`test_freeze_thaw_preserves_finite_float_type_and_exact_value`,
`test_freeze_rejects_nan_positive_and_negative_infinity`,
`test_query_source_use_inventory_covers_every_durable_dependency_or_exclusion`, and
`test_protection_schema_and_market_have_no_product_writer`. Also add bounded-query REDs for
`protection_for_symbol`, `protection_quote_input`, `latest_quote_for_symbol`,
`pending_recovery_symbols`, `pending_admissions`, `admission(command_id)`, `audits_for_symbol`,
`active_open_buy_symbols`, and `has_degraded_protection`; no query may iterate unrelated retained history.
The tests also pin a machine-readable `PRODUCER_INDEX_DEPENDENCIES` registry derived from every durable input used
by those queries. At minimum it includes `latest_explicit_quote`, `quote_price_views`, `day_valuation_view`, and
`day_valuations` plus every mutable `protection_quote_input` field: `protection.config`, `states`, `entry_times`,
`exit_exempt`, `max_holding_days`, `current_regime`, `intraday_crash_level`, `integrity_reset_symbols`, `degraded`,
`orders`, and `pending_owners`. Quote freshness and protection inputs exercise add/update/delete, stale evidence and
conflicting source-event parity against the uncapped full builder. A source-use inventory test walks the actual
`ProducerReadView` query/builder accessors and requires every durable path to resolve to the dependency registry or
an explicit tested exclusion, so the hand-authored registry cannot self-certify. `protection.schema` and
`protection.market` are immutable-format exclusions with an AST no-writer test. `market_sources` and `entry_quotes`
remain outside the view/index only because duplicate/completion checks consume them inside the owner exact-token
plan immediately before the store effect; tests prove the producer view never uses them and the owner rejects their
relevant change.

- [ ] **Step 2: Run the RED and record the missing-module failure**

```bash
env -i PATH=/usr/bin:/bin LANG=C.UTF-8 TZ=UTC PYTHONDONTWRITEBYTECODE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
  /home/ubuntu/projects/qwq-ai-trader/venv/bin/python -m pytest \
  tests/test_recovery_projection.py -q -p no:cacheprovider --tb=short --show-capture=no
```

Expected: nonzero; import/API absence, not fixture setup failure.

- [ ] **Step 3: Implement the frozen model API and pure/cooperative builders**

```python
FrozenJSON = str | int | float | bool | None | tuple["FrozenJSON", ...] | FrozenMap


class RecoveryProjectionMode(Enum):
    SHADOW_MIGRATION = "shadow_migration"
    ACTIVATED = "activated"


@dataclass(frozen=True, slots=True)
class OwnerToken:
    incarnation: str
    publication_epoch: int
    fault_epoch: int
    revision: int

    def __post_init__(self):
        values = (self.publication_epoch, self.fault_epoch, self.revision)
        if (type(self.incarnation) is not str or not self.incarnation
                or any(type(value) is not int or value < 0 for value in values)):
            raise ValueError("invalid_owner_token")


@dataclass(frozen=True, slots=True)
class ProducerReadView:
    token: OwnerToken
    complete: bool

    def attempts_for_symbol(self, symbol: str) -> tuple[AttemptFact, ...]: ...
    def attempts_for_intent(self, intent_id: str) -> tuple[AttemptFact, ...]: ...
    def intent(self, intent_id: str) -> IntentFact | None: ...
    def intents_for_symbol(self, symbol: str) -> tuple[IntentFact, ...]: ...
    def audits_for_intent(self, intent_id: str) -> tuple[AuditFact, ...]: ...
    def audits_for_symbol(self, symbol: str) -> tuple[AuditFact, ...]: ...
    def admissions_for_intent(self, intent_id: str) -> tuple[AdmissionFact, ...]: ...
    def admissions_for_symbol(self, symbol: str) -> tuple[AdmissionFact, ...]: ...
    def pending_admissions(self) -> tuple[AdmissionFact, ...]: ...
    def admission(self, command_id: str) -> AdmissionFact | None: ...
    def next_quote_admission(self) -> AdmissionFact | None: ...
    def pending_symbols_for_intent(self, intent_id: str) -> tuple[str, ...]: ...
    def pending_owner_for_symbol(self, symbol: str) -> str | None: ...
    def protection_for_symbol(self, symbol: str) -> ProtectionFact | None: ...
    def protection_quote_input(self, symbol: str) -> FrozenJSON: ...
    def latest_quote_for_symbol(self, symbol: str) -> ExplicitQuoteFact | None: ...
    def pending_recovery_symbols(self) -> tuple[str, ...]: ...
    def active_open_buy_symbols(self) -> tuple[str, ...]: ...
    def active_recovery_symbols(self) -> tuple[str, ...]: ...
    def invalid_facts(self) -> tuple[InvalidRecoveryFact, ...]: ...
    @property
    def has_degraded_protection(self) -> bool: ...


def build_owner_recovery_models(state: Mapping[str, object], *, token: OwnerToken) -> FullBuildResult:
    frozen = freeze_checkpoint_facts(state)
    projection = classify_owner_facts(frozen, token=token)
    join_view = build_owner_join_view(frozen, token=token)
    index = build_producer_index(frozen, token=token)
    return FullBuildResult(projection, join_view, index)
```

Use structurally shared immutable maps/sets; per-key updates and scoped queries must not copy a whole
5,000-row bucket. The cooperative builder consumes a detached payload and yields after 256 examined facts or 5ms,
whichever comes first; cancellation returns no partial object.
`FrozenJSON` and `RecoveryProjectionMode` are defined only in this module in Task1 and imported by later tasks; no
task creates a second alias/enum with the same semantic role.
Freeze/thaw preserves every finite `float` as `float` with the exact Python value (including protection config
values such as `10.0`, `0.10`, and `5.0`) and rejects NaN or either infinity before candidate/store work. It never
coerces a valid float to string or int, so existing `encode_state()` bytes remain parity-comparable.

- [ ] **Step 4: Run Task1 GREEN and immutability checks**

Run the Step2 command again. Formal source mutation kills run in Task14 from temporary archived trees; Task1 does
not edit and restore source ad hoc.

- [ ] **Step 5: Complete independent review, stage the literal allowlist, and commit Task1**

```bash
set -euo pipefail
git status --short
git diff --check -- src/execution/safety/recovery_projection.py tests/test_recovery_projection.py
git add -- src/execution/safety/recovery_projection.py tests/test_recovery_projection.py
git diff --cached --check
git diff --cached --name-only
git commit -m "feat(safety): add immutable recovery projection oracle" -- \
  src/execution/safety/recovery_projection.py tests/test_recovery_projection.py
```

- [ ] **Step 6: Verify the Task1 commit SHA and push separately**

```bash
set -euo pipefail
git show --stat --oneline --summary HEAD
git push -u origin HEAD
```

Independent Sol/high reviewer checks exact N3 count multiplicity, frozen detachment, and absence of historical
iteration. Integrate only after blocking findings are 0.

---

### Task 2: Typed Mutation Plans and Bounded Projection Delta

**Files:**
- Create: `src/execution/safety/mutation_plans.py`
- Create: `tests/test_execution_mutation_plans.py`
- Modify: `src/execution/safety/policy_generations.py`
- Test: `tests/test_execution_policy_generations.py`

**Interfaces:**
- Consumes: Task1 `OwnerToken`, `FullBuildResult` and immutable index roots.
- Produces: edit DTOs, `MutationPlan`, `RowChange`, `ProjectionDelta`, `PreparedMutation`,
  `PlannedCheckpointView`, `ContinuationPlanBuilder`, budget-aware `EditCollector`,
  `policy_generation_edits()`, and the single product seam
  `advance_recovery_models(current_models, delta, target_token)`.

- [ ] **Step 1: Write REDs for actual-owner edits, duplicate paths, policy finalizer, and 64/65·256/257**

```python
def test_row_65_rejects_without_inspecting_later_edits():
    plan = plan_with_counted_rows(65, tail=ExplodesIfRead())
    result = prepare_mutation(BASE_STATE, BASE_MODELS, plan, mode=RecoveryProjectionMode.ACTIVATED)
    assert result.disposition == "reject"
    assert result.reason == "projection_budget_exceeded"
    assert result.store_allowed is False


def test_repeated_dependency_edge_counts_once():
    prepared = prepare_mutation(BASE_STATE, BASE_MODELS, repeated_edge_plan(256),
                                mode=RecoveryProjectionMode.ACTIVATED)
    assert prepared.delta.distinct_edges == 1
```

Add exact tests for SetRow update/delete, SetScalar, caller inability to self-report completeness,
base-token mismatch, exact64/exact256 acceptance, edge257 rejection, ReplaceRoot rejection outside shadow,
overlay reads after prior edits, duplicate-path normalization, and an AST assertion that production callers cannot
replace or bypass `advance_recovery_models`. Add
`test_protection_config_finite_float_freeze_typed_edit_encode_roundtrip_preserves_bytes_and_types` using the real
ExitConfig-derived checkpoint, plus nonfinite SetRow/SetScalar rejection before any owner effect.

- [ ] **Step 2: Run the Task2 RED**

```bash
env -i PATH=/usr/bin:/bin LANG=C.UTF-8 TZ=UTC PYTHONDONTWRITEBYTECODE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
  /home/ubuntu/projects/qwq-ai-trader/venv/bin/python -m pytest \
  tests/test_execution_mutation_plans.py tests/test_execution_policy_generations.py \
  -q -p no:cacheprovider --tb=short --show-capture=no
```

Expected: nonzero because `MutationPlan` and `policy_generation_edits` do not exist.

- [ ] **Step 3: Implement the edit vocabulary and owner-applied candidate**

```python
@dataclass(frozen=True, slots=True)
class SetRow:
    table_path: tuple[str, ...]
    key: str
    value: FrozenJSON


@dataclass(frozen=True, slots=True)
class MutationPlan:
    base_token: OwnerToken
    operation: MutationKind
    edits: tuple[SetRow | DeleteRow | SetScalar | ReplaceRoot, ...]
    result: FrozenJSON


ContinuationPlanBuilder = Callable[[PlannedCheckpointView, OwnerToken], MutationPlan]


class EditCollector:
    def set_row(self, table_path: tuple[str, ...], key: str, value: FrozenJSON) -> None: ...
    def delete_row(self, table_path: tuple[str, ...], key: str) -> None: ...
    def set_scalar(self, path: tuple[str, ...], value: FrozenJSON) -> None: ...
    def freeze(self) -> tuple[SetRow | DeleteRow | SetScalar, ...]: ...


def advance_recovery_models(
    current_models: FullBuildResult,
    delta: ProjectionDelta,
    target_token: OwnerToken,
) -> FullBuildResult: ...


def prepare_mutation(state, models, plan, *, mode):
    require_exact_base_token(models.projection.token, plan.base_token)
    candidate, row_changes = apply_typed_edits(state, plan.edits)
    policy_edits = policy_generation_edits(state, candidate)
    candidate, policy_changes = apply_typed_edits(candidate, policy_edits)
    return build_bounded_delta(candidate, models, row_changes + policy_changes, mode=mode)
```

Stop immediately on row 65/edge 257. `ReplaceRoot` is accepted only as `migration_full_build` in explicit
preactivation shadow. No caller field can set `complete`, changed keys, or edge counts.
`prepare_mutation()` always requires an exact-current base token. It never accepts a lease or relaxes this rule.
Continuation is a later owner-gated operation: the caller provides a `ContinuationPlanBuilder`, not a caller-built
plan with an old base token. Task5 invokes that builder only after lease/dependency validation and supplies an
await-free `PlannedCheckpointView` plus the owner's exact current token.

- [ ] **Step 4: Run focused GREEN and verify the policy history bytes remain identical**

Run Step2, then:

```bash
env -i PATH=/usr/bin:/bin LANG=C.UTF-8 TZ=UTC PYTHONDONTWRITEBYTECODE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
  /home/ubuntu/projects/qwq-ai-trader/venv/bin/python -m pytest \
  tests/test_execution_policy_generations.py -q -p no:cacheprovider --tb=short --show-capture=no
```

- [ ] **Step 5: Complete review, stage the literal allowlist, and commit Task2**

```bash
set -euo pipefail
git status --short
git diff --check -- src/execution/safety/mutation_plans.py src/execution/safety/policy_generations.py \
  tests/test_execution_mutation_plans.py tests/test_execution_policy_generations.py
git add -- src/execution/safety/mutation_plans.py src/execution/safety/policy_generations.py \
  tests/test_execution_mutation_plans.py tests/test_execution_policy_generations.py
git diff --cached --check
git diff --cached --name-only
git commit -m "feat(safety): add typed owner mutation plans" -- \
  src/execution/safety/mutation_plans.py src/execution/safety/policy_generations.py \
  tests/test_execution_mutation_plans.py tests/test_execution_policy_generations.py
```

- [ ] **Step 6: Verify the Task2 commit and push separately**

```bash
set -euo pipefail
git show --stat --oneline --summary HEAD
git push -u origin HEAD
```

---

### Task 3: Explicit Ticket-FIFO Owner Gate

**Files:**
- Create: `src/execution/safety/owner_ticket_gate.py`
- Create: `tests/test_execution_owner_ticket_gate.py`

**Interfaces:**
- Consumes: an exact underlying `asyncio.Lock` retained for legacy N3 observation.
- Produces: `OwnerTicketKind`, `OwnerTicket`, `OwnerTicketGate.hold()` and immutable hold metrics.

- [ ] **Step 1: Write FIFO, queued cancellation, submitted drain, two-commit ticket, and overrun REDs**

```python
def test_fifo_orders_writer_producer_writer():
    async def scenario():
        gate = OwnerTicketGate(asyncio.Lock(), clock=clock)
        order = []
        first = asyncio.create_task(record_ticket(gate, "writer-1", order))
        producer = asyncio.create_task(record_ticket(gate, "producer", order))
        last = asyncio.create_task(record_ticket(gate, "writer-2", order))
        await asyncio.gather(first, producer, last)
        assert order == ["writer-1", "producer", "writer-2"]
    asyncio.run(scenario())
```

Also add `test_cancelled_queued_ticket_is_skipped_without_reordering_survivors`,
`test_cancelled_active_ticket_keeps_gate_until_submitted_store_drains`,
`test_hold_overrun_records_failure_without_cancelling_store`, and
`test_one_apply_ticket_covers_receive_and_fill_commits`.

- [ ] **Step 2: Run RED**

```bash
env -i PATH=/usr/bin:/bin LANG=C.UTF-8 TZ=UTC PYTHONDONTWRITEBYTECODE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
  /home/ubuntu/projects/qwq-ai-trader/venv/bin/python -m pytest \
  tests/test_execution_owner_ticket_gate.py -q -p no:cacheprovider --tb=short --show-capture=no
```

- [ ] **Step 3: Implement ticket allocation and cancellation-safe ownership**

```python
@asynccontextmanager
async def hold(self, kind: OwnerTicketKind):
    ticket = self._enqueue(kind)
    try:
        await ticket.ready
        await self._lock.acquire()
        ticket.mark_active(self._clock())
        yield ticket
    finally:
        if ticket.active:
            self._record_hold(ticket, self._clock())
            self._lock.release()
        self._release_or_skip(ticket)
```

The submitted-store task owns a drain future; cancellation does not mark the active ticket releasable until that
future settles. The gate records overrun but never cancels an already submitted store operation.

- [ ] **Step 4: Run GREEN and obtain independent cancellation/fairness review**

```bash
env -i PATH=/usr/bin:/bin LANG=C.UTF-8 TZ=UTC PYTHONDONTWRITEBYTECODE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
  /home/ubuntu/projects/qwq-ai-trader/venv/bin/python -m pytest \
  tests/test_execution_owner_ticket_gate.py -q -p no:cacheprovider --tb=short --show-capture=no
```

- [ ] **Step 5: Stage the exact Task3 allowlist and commit after blocking 0**

```bash
set -euo pipefail
git status --short
git diff --check -- src/execution/safety/owner_ticket_gate.py tests/test_execution_owner_ticket_gate.py
git add -- src/execution/safety/owner_ticket_gate.py tests/test_execution_owner_ticket_gate.py
git diff --cached --check
git diff --cached --name-only
git commit -m "feat(safety): add fair owner ticket admission" -- \
  src/execution/safety/owner_ticket_gate.py tests/test_execution_owner_ticket_gate.py
```

- [ ] **Step 6: Verify the Task3 commit and push separately**

```bash
set -euo pipefail
git show --stat --oneline --summary HEAD
git push -u origin HEAD
```

---

### Task 4: Owner Token, Phase Skeleton, and Synchronous Fault Invalidation

**Files:**
- Modify: `src/execution/safety/application.py:202-247`
- Create: `tests/test_recovery_projection_faults.py`

**Interfaces:**
- Consumes: Tasks 1 and 3.
- Produces: `OwnerPhase`, `current_token`, synchronous `_block(reason)`, unavailable A frame,
  `acquire_producer_view()` skeleton.

- [ ] **Step 1: Write idle block, repeated block, PREPARING and same-revision incarnation REDs**

```python
def test_idle_block_invalidates_frame_without_revision_change(owner):
    before = owner.current_token
    frame = owner.diagnostic_frame
    owner._block("command_result_failed")
    assert owner.version == before.revision
    assert owner.current_token.publication_epoch == before.publication_epoch + 1
    assert owner.current_token.fault_epoch == before.fault_epoch + 1
    assert owner.diagnostic_frame is not frame
    assert owner.diagnostic_frame.current_for_producer is False
```

Add REDs proving `_block` never awaits/acquires the gate, PREPARING does not change token/pointer, a concurrent
fault wins over cleanup, and a new owner at the same revision rejects the old incarnation.

- [ ] **Step 2: Run RED against current `_block()`**

```bash
env -i PATH=/usr/bin:/bin LANG=C.UTF-8 TZ=UTC PYTHONDONTWRITEBYTECODE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
  /home/ubuntu/projects/qwq-ai-trader/venv/bin/python -m pytest \
  tests/test_recovery_projection_faults.py -q -p no:cacheprovider --tb=short --show-capture=no
```

- [ ] **Step 3: Implement phases and central fault seam**

```python
class OwnerPhase(str, Enum):
    READY = "READY"
    PREPARING = "PREPARING"
    SUBMITTING = "SUBMITTING"
    PUBLISHING = "PUBLISHING"
    RECOVERY_REQUIRED = "RECOVERY_REQUIRED"
    RESTORING = "RESTORING"


def _block(self, reason: str = "owner_fault") -> None:
    self._publication_epoch += 1
    self._fault_epoch += 1
    self._phase = OwnerPhase.RECOVERY_REQUIRED
    self._healthy = False
    self._publication_recovery_required = True
    self._diagnostic_frame = unavailable_frame(self.current_token, reason)
```

Only init, `_block`, commit publication and restore transitions may assign owner health/epochs/frame. Add an AST
assertion in the test so later direct writers fail.

- [ ] **Step 4: Run Task4 and existing publication regressions**

```bash
env -i PATH=/usr/bin:/bin LANG=C.UTF-8 TZ=UTC PYTHONDONTWRITEBYTECODE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
  /home/ubuntu/projects/qwq-ai-trader/venv/bin/python -m pytest \
  tests/test_recovery_projection_faults.py tests/test_execution_fill_application.py \
  tests/test_execution_policy_generations.py -q -p no:cacheprovider --tb=short --show-capture=no
```

- [ ] **Step 5: Obtain independent critical review, stage the literal allowlist, and commit**

```bash
set -euo pipefail
git status --short
git diff --check -- src/execution/safety/application.py tests/test_recovery_projection_faults.py
git add -- src/execution/safety/application.py tests/test_recovery_projection_faults.py
git diff --cached --check
git diff --cached --name-only
git commit -m "feat(safety): add owner phases and synchronous invalidation" -- \
  src/execution/safety/application.py tests/test_recovery_projection_faults.py
```

- [ ] **Step 6: Verify the Task4 commit and push separately**

```bash
set -euo pipefail
git show --stat --oneline --summary HEAD
git push -u origin HEAD
```

---

### Task 5: PREPARING → SUBMITTING → PUBLISHING Commit Spine

**Files:**
- Modify: `src/execution/safety/application.py:365-583`
- Modify: `src/execution/safety/policy_generations.py`
- Create: `src/execution/safety/writer_authority.py` with `LeaseSpec` and `WriterLease`; Task8 extends it with
  claim/result-drain authority.
- Extend: `tests/test_recovery_projection_faults.py`
- Test: `tests/test_execution_fill_application.py`, `tests/test_execution_policy_generations.py`

**Interfaces:**
- Consumes: Tasks1–4.
- Produces: `RuntimeAdmissionAuthority`, `LeasePurpose`, `LeaseSpec`, `MutationReceipt`, exact-token
  `apply_plan()`/`continue_plan()`, private candidate installation, final await-free A publication.

- [ ] **Step 1: Write rejection identity and fault-boundary REDs**

```python
def test_stale_prevalidation_preserves_every_current_identity(owner, spies):
    async def scenario():
        before = owner.identity_snapshot()
        with pytest.raises(ApplicationBlocked, match="stale_market_quote"):
            await owner.apply_plan("stale", stale_quote_plan(before.token))
        assert owner.identity_snapshot() == before
        assert spies.calls == {"store": 0, "publisher": 0, "full_builder": 0}
    asyncio.run(scenario())


def test_fault_during_store_wait_prevents_candidate_publication(owner, paused_store):
    async def scenario():
        task = asyncio.create_task(owner.apply_plan("x", valid_plan(owner.current_token)))
        await paused_store.submitted.wait()
        owner._block("late_result_failure")
        paused_store.release_success()
        with pytest.raises(ApplicationBlocked):
            await task
        assert owner.phase is OwnerPhase.RECOVERY_REQUIRED
        assert owner.diagnostic_frame.current_for_producer is False
    asyncio.run(scenario())
```

Cover stale quote, duplicate admission, base mismatch, invalid domain, encoding/builder error, pre-submit
cancellation, exact 64/256 success, 65/257 store 0, SQL pause, rollback, lost response, submitted cancellation,
publisher failure, publisher-triggered block, and final fault-epoch check. Add
`test_continuation_builder_receives_exact_current_token_after_unrelated_revision`,
`test_continuation_rejects_relevant_dependency_change_before_builder`,
`test_continuation_rejects_fault_day_fence_operation_and_claim_mismatch`, and
`test_caller_cannot_submit_old_token_plan_or_reacquire_to_continue`. Also prove
`test_lease_spec_exposes_only_closed_purpose_not_selector_or_fingerprint` and
`test_owner_not_caller_computes_day_fence_and_dependency_fingerprint`, plus
`test_lease_purpose_selector_downgrade_or_operation_mismatch_rejects_before_builder_store` and
`test_runtime_admission_generation_fence_or_closed_change_rejects_before_builder_store`.

- [ ] **Step 2: Run RED and confirm failures reach old early `_block()` behavior**

```bash
env -i PATH=/usr/bin:/bin LANG=C.UTF-8 TZ=UTC PYTHONDONTWRITEBYTECODE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
  /home/ubuntu/projects/qwq-ai-trader/venv/bin/python -m pytest \
  tests/test_recovery_projection_faults.py \
  -q -p no:cacheprovider --tb=short --show-capture=no
```

- [ ] **Step 3: Implement the ordered transition exactly once**

```python
class DependencySelector(Enum):
    COMMAND_CLAIM = "command_claim"
    QUOTE_COMPLETION = "quote_completion"


class LeasePurpose(Enum):
    COMMAND_CLAIM = "command_claim"
    QUOTE_COMPLETION = "quote_completion"


@dataclass(frozen=True, slots=True)
class RuntimeAdmissionSnapshot:
    day_generation: int
    fence_id: str | None
    day_closed: bool


class RuntimeAdmissionAuthority(Protocol):
    def snapshot(self) -> RuntimeAdmissionSnapshot: ...


@dataclass(frozen=True, slots=True)
class LeaseSpec:
    operation_id: str
    purpose: LeasePurpose
    claim_id: str | None = None


@dataclass(frozen=True, slots=True)
class WriterLease:
    lease_id: str
    operation_id: str
    purpose: LeasePurpose
    dependency_selector: DependencySelector
    incarnation: str
    fault_epoch: int
    last_owner_token: OwnerToken
    day_generation: int
    fence_id: str | None
    dependency_fingerprint: str
    claim_id: str | None


@dataclass(frozen=True, slots=True)
class MutationReceipt:
    version: int
    result: FrozenJSON
    owner_token: OwnerToken
    writer_lease: WriterLease | None


async def apply_plan(
    self,
    commit_id: str,
    plan: MutationPlan,
    *,
    issue_lease: LeaseSpec | None = None,
) -> MutationReceipt: ...


async def continue_plan(
    self,
    commit_id: str,
    *,
    continuation: WriterLease,
    builder: ContinuationPlanBuilder,
    issue_lease: LeaseSpec | None = None,
) -> MutationReceipt: ...


async def _commit_prepared(self, prepared, commit_id):
    self._final_guard(prepared)
    self._publication_epoch += 1
    self._phase = OwnerPhase.SUBMITTING
    self._diagnostic_frame = unavailable_frame(self.current_token, "store_submitting")
    expected_fault = self._fault_epoch
    version = await self.store.commit(self._version, prepared.payload, commit_id)
    if self._fault_epoch != expected_fault:
        raise ApplicationBlocked("owner_fault_during_store_wait")
    self._install_private(prepared, version)
    self._phase = OwnerPhase.PUBLISHING
    self._require_sync(self.publisher(deepcopy(prepared.payload), version))
    self._final_publication_guard(expected_fault)
    self._publish_recovery_frame(prepared, version)
    return MutationReceipt(version, prepared.result, self.current_token, prepared.next_lease)
```

There is no unrelated await between final guard, invalidation and store submission, nor between publisher success
and final A pointer publication. A lost result/cancellation after submission never reopens the old pointer.
The first store effect validates `plan.base_token` exactly plus current day/fence immediately before submission.
Task5 injects a trusted synchronous `RuntimeAdmissionAuthority` into the owner. Its snapshot reads the authoritative
runtime RAM day generation, fence ID and closed flag without awaiting; these values are not inferred from the durable
checkpoint and are never caller data. Task5 ships a fail-closed production default that cannot issue continuation
leases and uses a deterministic fake only in Task5 tests. Task8 wires the real runtime port before any product path
can request a lease.

The owner enforces a closed `(MutationKind, LeasePurpose) -> DependencySelector` table. A caller requests only a
purpose and operation ID; it cannot supply the selector, day/fence values or fingerprint bytes. Lease issuance
captures the runtime admission snapshot and owner-computed fingerprint, and stores the exact purpose/selector in an
owner-private `lease_id` record. The public frozen `WriterLease` is only a proof handle and must exactly match that
record; selector downgrade, purpose/operation mismatch, forged/reused lease or missing authority rejects before the
builder and store. Task8 later adds a runtime-owned cross-facade lookup registry, but it stores these owner-issued
handles and never becomes their source of truth.
`WriterLease.last_owner_token` is audit/replay evidence only; it is never reused as a continuation plan base and is
not compared as a substitute for current dependency/runtime admission validation.

`continue_plan()` enters the same owner ticket, validates operation/incarnation/fault/day/fence/claim plus the
lease's owner-computed relevant dependency fingerprint and a fresh authoritative runtime admission snapshot
(including `day_closed is False`) against the owner-private record, and rejects before invoking the
builder if any relevant fact changed. An unrelated revision is permitted only here: the owner invokes the supplied
builder synchronously with an await-free `PlannedCheckpointView` and the exact **current** `OwnerToken`; the returned
plan must use that token and then passes ordinary `prepare_mutation()` exact-token validation. The caller never sees
an opportunity to mint/rebase a token and never supplies a prebuilt continuation plan. Only a successful owner
commit may issue the next lease.
`register_policy_generations()` is migrated here from its dedicated `_commit_publish` caller to a concrete typed
registration plan; its SQL receipt/replay checks and policy-finalizer edits remain inside the same owner ticket.

- [ ] **Step 4: Run focused GREEN plus store drain regressions**

```bash
env -i PATH=/usr/bin:/bin LANG=C.UTF-8 TZ=UTC PYTHONDONTWRITEBYTECODE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
  /home/ubuntu/projects/qwq-ai-trader/venv/bin/python -m pytest \
  tests/test_recovery_projection_faults.py tests/test_execution_fill_application.py \
  tests/test_execution_policy_generations.py tests/test_execution_state_store.py \
  -q -p no:cacheprovider --tb=short --show-capture=no
```

- [ ] **Step 5: Obtain Astra/xhigh review, stage the literal allowlist, and commit**

Reviewer must inspect actual source and pause store/publisher with tests. After blocking 0:

```bash
set -euo pipefail
git status --short
git diff --check -- src/execution/safety/application.py src/execution/safety/policy_generations.py \
  src/execution/safety/writer_authority.py \
  tests/test_recovery_projection_faults.py tests/test_execution_fill_application.py \
  tests/test_execution_policy_generations.py
git add -- src/execution/safety/application.py src/execution/safety/policy_generations.py \
  src/execution/safety/writer_authority.py \
  tests/test_recovery_projection_faults.py tests/test_execution_fill_application.py \
  tests/test_execution_policy_generations.py
git diff --cached --check
git diff --cached --name-only
git commit -m "feat(safety): stage owner commit publication" -- \
  src/execution/safety/application.py src/execution/safety/policy_generations.py \
  src/execution/safety/writer_authority.py \
  tests/test_recovery_projection_faults.py tests/test_execution_fill_application.py \
  tests/test_execution_policy_generations.py
```

- [ ] **Step 6: Verify the Task5 commit and push separately**

```bash
set -euo pipefail
git show --stat --oneline --summary HEAD
git push -u origin HEAD
```

---

### Task 6: Serialized Restore and One-Shot Full Build

**Files:**
- Modify: `src/execution/safety/application.py:385-398`
- Modify: `src/execution/safety/runtime.py:183-191`
- Extend: `tests/test_recovery_projection_faults.py`
- Test: `tests/test_execution_policy_registration_restore.py`, `tests/test_execution_runtime.py`,
  `tests/test_execution_state_store.py`

**Interfaces:**
- Consumes: Task5 commit spine and Task1 cooperative builder.
- Produces: RESTORING barrier, exact-token restore result, runtime-restorer hook, no background rebuild.

- [ ] **Step 1: Write restore barrier, receipt, cancellation, same-revision and stale completion REDs**

```python
def test_restore_not_current_until_synchronous_runtime_hook_finishes(owner):
    entered = threading.Event()
    release = threading.Event()
    observed = []

    def runtime_restorer(state, version):
        entered.set()
        assert release.wait(timeout=1.0)

    def inspect_while_hook_is_blocked():
        assert entered.wait(timeout=1.0)
        observed.append(owner.diagnostic_frame.current_for_producer)
        release.set()

    inspector = threading.Thread(target=inspect_while_hook_is_blocked)
    inspector.start()
    asyncio.run(owner.restore(runtime_restorer=runtime_restorer))
    inspector.join(timeout=1.0)
    assert inspector.is_alive() is False
    assert observed == [False]
    assert owner.diagnostic_frame.current_for_producer is True
```

Assert publication/fault epochs invalidate before load, policy receipts share one snapshot, legacy checkpoint builds
once, damaged receipt/builder error/cancellation never publish empty 0, same revision uses a new token, and there is
no background/latest-token task. Add `test_restore_rejects_awaitable_runtime_hook`: an async hook is rejected by
the sync-hook guard and the owner remains unavailable.

- [ ] **Step 2: Run RED**

```bash
env -i PATH=/usr/bin:/bin LANG=C.UTF-8 TZ=UTC PYTHONDONTWRITEBYTECODE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
  /home/ubuntu/projects/qwq-ai-trader/venv/bin/python -m pytest \
  tests/test_recovery_projection_faults.py tests/test_execution_policy_registration_restore.py \
  tests/test_execution_runtime.py -k restore -q -p no:cacheprovider --tb=short --show-capture=no
```

- [ ] **Step 3: Implement restore without changing `store.py`**

```python
async def restore(self, *, runtime_restorer=None):
    async with self._ticket_gate.hold(OwnerTicketKind.RESTORE):
        self._enter_restoring()
        version, state, receipts = await self.store.load_with_policy_receipts()
        validate_restore_receipts(state, version, receipts)
        models = await build_owner_recovery_models_cooperatively(freeze_checkpoint(state),
                                                                  token=self._restore_token(version))
        self._publish_state_privately(state, version, models)
        self._require_sync(self.publisher(deepcopy(state), version))
        if runtime_restorer is not None:
            self._require_sync(runtime_restorer(state, version))
        self._activate_restored_frame(models, version)
        return version
```

Late worker completion checks incarnation/publication/fault/revision before use. `store._run()` keeps its existing
submitted cancellation drain semantics.

- [ ] **Step 4: Run GREEN and obtain independent critical restore review**

```bash
env -i PATH=/usr/bin:/bin LANG=C.UTF-8 TZ=UTC PYTHONDONTWRITEBYTECODE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
  /home/ubuntu/projects/qwq-ai-trader/venv/bin/python -m pytest \
  tests/test_recovery_projection_faults.py tests/test_execution_policy_registration_restore.py \
  tests/test_execution_runtime.py tests/test_execution_state_store.py \
  -q -p no:cacheprovider --tb=short --show-capture=no
```

- [ ] **Step 5: Stage the exact Task6 allowlist and commit after blocking 0**

```bash
set -euo pipefail
git status --short
git diff --check -- src/execution/safety/application.py src/execution/safety/runtime.py \
  tests/test_recovery_projection_faults.py tests/test_execution_policy_registration_restore.py \
  tests/test_execution_runtime.py
git add -- src/execution/safety/application.py src/execution/safety/runtime.py \
  tests/test_recovery_projection_faults.py tests/test_execution_policy_registration_restore.py \
  tests/test_execution_runtime.py
git diff --cached --check
git diff --cached --name-only
git commit -m "feat(safety): rebuild recovery models during restore" -- \
  src/execution/safety/application.py src/execution/safety/runtime.py \
  tests/test_recovery_projection_faults.py tests/test_execution_policy_registration_restore.py \
  tests/test_execution_runtime.py
```

- [ ] **Step 6: Verify the Task6 commit and push separately**

```bash
set -euo pipefail
git show --stat --oneline --summary HEAD
git push -u origin HEAD
```

---

### Task 7: Checked-In Writer Inventory and Activation Boundary

**Files:**
- Create: `tests/fixtures/recovery_mutation_inventory.json`
- Create: `tests/test_recovery_projection_inventory.py`
- Modify: `src/execution/safety/application.py::mutate`

**Interfaces:**
- Consumes: Task5/6 owner modes.
- Produces: exact AST inventory plus canonical `SHADOW_MIGRATION` and `ACTIVATED` behavior. “Legacy” below names
  source routes counted by the inventory; it is not a third `RecoveryProjectionMode` value.

- [ ] **Step 1: Check in the exact initial inventory and failing AST assertions**

The fixture is immutable baseline evidence, not a mutable progress list. Each route records its baseline site and
declared successor. Current status is always AST-derived; Tasks8–11 must not edit or delete fixture rows.

```json
{
  "schema_version": 2,
  "baseline": {
    "sha": "cdd42df53b80b3d7372bdaf926cd5edb44d54ad8",
    "counts": {"owner_mutate": 25, "commit_publish": 6, "store_writer": 1}
  },
  "routes": [
    {"id": "lifecycle.prepare",
     "baseline": {"path": "src/execution/safety/lifecycle.py",
                  "symbol": "OrderLifecycleCoordinator.prepare",
                  "callee": "self.owner.mutate", "command_prefix": "prepare:"},
     "successor": {"path": "src/execution/safety/lifecycle.py",
                   "symbol": "OrderLifecycleCoordinator.prepare",
                   "callee": "self.owner.apply_plan"},
     "wave": 1, "migration_task": 8}
  ],
  "nested_effects": [
    {"id": "fill.economics", "baseline_path": "src/execution/safety/economics.py",
     "baseline_symbol": "reduce_economics", "required_result": "MutationFragment",
     "migration_task": 9}
  ]
}
```

The committed file contains all source-reconciled 25 product calls, six internal commit routes, the one store
writer and every nested whole-candidate effect. Tests use AST enclosing symbols rather than line numbers. Each
baseline ID must resolve exactly once to either the legacy call or its declared successor; missing, simultaneous,
or unclassified writer matches fail.

The source-reconciled migration ledger is fixed: Wave1 has six `owner.mutate` calls, Wave2 has journal ACK plus
four direct application commit routes, Wave3 has seven runtime calls, and Wave4 has eleven calls. The six internal
application routes are assigned as follows: policy registration → Task5 typed plan; generic legacy `mutate` →
Task7 shadow/activation boundary; supersede/receive/conflict/fill → Task9 typed plans.
Add exact REDs `test_baseline_inventory_retains_25_mutate_6_commit_and_1_store_routes`,
`test_current_inventory_resolves_every_baseline_route_exactly_once`,
`test_current_inventory_rejects_unclassified_nested_effect_writer`, and
`test_activation_rejects_current_source_while_legacy_routes_remain`. Also add a synthetic successor-complete
fixture test proving the immutable baseline rows can resolve to zero legacy without deleting history. Task11, after
the last real migration, replaces the current-source rejection assertion with
`test_activated_inventory_has_zero_legacy_calls_without_deleting_baseline_rows`; the JSON fixture itself remains
byte-identical.

- [ ] **Step 2: Run RED against current unclassified routes**

```bash
env -i PATH=/usr/bin:/bin LANG=C.UTF-8 TZ=UTC PYTHONDONTWRITEBYTECODE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
  /home/ubuntu/projects/qwq-ai-trader/venv/bin/python -m pytest \
  tests/test_recovery_projection_inventory.py -q -p no:cacheprovider --tb=short --show-capture=no
```

- [ ] **Step 3: Implement explicit mode behavior without activating product consumers**

`SHADOW_MIGRATION` runs exactly one serialized candidate full build before store for a manifest-listed legacy
route. `ACTIVATED` raises `legacy_mutation_unsupported` before invoking the reducer and requires zero product
legacy entries. At Task7, activation of the real source is therefore expected to be blocked because Tasks8–11 have
not migrated the listed routes yet; only the synthetic fully migrated fixture exercises successful activation.
Direct store/state/health/publisher/fault seams outside the allowlist fail the test. Later tasks change source (and
Task11 flips the current-source assertion) so AST resolution moves from `baseline` to `successor`; the JSON fixture
is never rewritten.

- [ ] **Step 4: Run GREEN and review the complete baseline inventory manually**

```bash
env -i PATH=/usr/bin:/bin LANG=C.UTF-8 TZ=UTC PYTHONDONTWRITEBYTECODE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
  /home/ubuntu/projects/qwq-ai-trader/venv/bin/python -m pytest \
  tests/test_recovery_projection_inventory.py -q -p no:cacheprovider --tb=short --show-capture=no
```

- [ ] **Step 5: Stage the exact Task7 allowlist and commit after blocking 0**

```bash
set -euo pipefail
git status --short
git diff --check -- tests/fixtures/recovery_mutation_inventory.json \
  tests/test_recovery_projection_inventory.py src/execution/safety/application.py
git add -- tests/fixtures/recovery_mutation_inventory.json tests/test_recovery_projection_inventory.py \
  src/execution/safety/application.py
git diff --cached --check
git diff --cached --name-only
git commit -m "test(safety): lock recovery mutation inventory" -- \
  tests/fixtures/recovery_mutation_inventory.json tests/test_recovery_projection_inventory.py \
  src/execution/safety/application.py
```

- [ ] **Step 6: Verify the Task7 commit and push separately**

```bash
set -euo pipefail
git show --stat --oneline --summary HEAD
git push -u origin HEAD
```

---

### Task 8: Lifecycle WriterLease and One-Shot Result Drain

**Files:**
- Extend: `src/execution/safety/writer_authority.py`
- Modify: `src/execution/safety/runtime.py` to own the shared authority registry.
- Modify: `src/execution/safety/lifecycle.py`
- Modify: `src/execution/safety/commands.py`
- Modify: `src/execution/safety/gateway.py`
- Read/verify only: `src/execution/safety/transport.py`; Task8 does not edit it. If a RED proves a transport defect,
  stop and amend/re-review this plan rather than silently widening the allowlist.
- Create: `tests/test_recovery_projection_acceptance.py`
- Extend: `tests/test_execution_command_owner.py`, `tests/test_execution_dispatch_reasons.py`,
  `tests/test_execution_command_shutdown.py`, `tests/test_execution_command_flow.py`,
  `tests/test_execution_transport_killswitch_audit.py`

**Interfaces:**
- Consumes: `MutationReceipt` and `OwnerToken` from Tasks1/5.
- Produces: shared `WriterAuthority`, `ResultDrainToken`, `ClaimReceipt`, `claim_with_lease()`,
  source-compatible `prepare(request, context, sector=None, fill_metadata=None, owner_token=None)`, unchanged
  public `dispatch(request, context, transport)`, and
  `record_result_with_token()`.

- [ ] **Step 1: Write REDs for self-commits, unrelated/relevant revision, faults, and all transport outcomes**

```python
def test_claim_registers_drain_before_transport(runtime, transport_probe):
    async def scenario():
        prepared_attempt = await commands.prepare(
            REQUEST, CONTEXT, owner_token=runtime.owner.current_token,
        )
        result = await commands.dispatch(REQUEST, CONTEXT, transport_probe)
        assert prepared_attempt["request_binding"]["fingerprint"] == REQUEST.fingerprint
        assert transport_probe.drain_registered_before_entry is True
        assert result.status is CommandStatus.ACKNOWLEDGED
    asyncio.run(scenario())


@pytest.mark.parametrize("outcome", ["ack", "rejected", "guard_not_sent", "kill_switch_not_sent",
                                      "pre_guard_cancel", "post_guard_cancel"])
def test_claimed_outcome_is_drained_once(outcome, command_fixture):
    async def scenario():
        receipt = await command_fixture.run(outcome)
        assert receipt.record_result_calls == 1
        assert receipt.extra_post_calls == 0
    asyncio.run(scenario())
```

Add exact tests for own prepare→claim token advancement, unrelated revision only with same dependency fingerprint,
relevant change/fault/day/fence/wrong/reused lease rejection, restore incarnation rejection, closing/day change after
claim, result-store fault, caller cancellation strong task, and drain token inability to POST. The unrelated-revision
test asserts that `claim_with_lease()` uses Task5 `continue_plan()` with an owner-side builder that receives the
current token; no caller-built old-token plan or caller reacquire/rebase is accepted. Add
`test_runtime_wires_authoritative_admission_port_before_first_lease_issue` and
`test_runtime_day_close_before_durable_transition_rejects_continuation`.

- [ ] **Step 2: Run RED**

```bash
env -i PATH=/usr/bin:/bin LANG=C.UTF-8 TZ=UTC PYTHONDONTWRITEBYTECODE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
  /home/ubuntu/projects/qwq-ai-trader/venv/bin/python -m pytest \
  tests/test_recovery_projection_acceptance.py tests/test_execution_command_owner.py \
  tests/test_execution_command_flow.py tests/test_execution_dispatch_reasons.py \
  tests/test_execution_command_shutdown.py \
  tests/test_execution_transport_killswitch_audit.py \
  -q -p no:cacheprovider --tb=short --show-capture=no
```

- [ ] **Step 3: Reuse Task5 WriterLease and add exact claim/result authority DTOs**

```python
@dataclass(frozen=True, slots=True)
class ResultDrainToken:
    token_id: str
    operation_id: str
    incarnation: str
    fault_epoch: int
    attempt_id: str
    claim_id: str
    expected_attempt_version: int
    request_fingerprint: str
    command_scope_id: str


@dataclass(frozen=True, slots=True)
class ClaimReceipt:
    claimed: bool
    attempt_version: int | None
    send_lease: WriterLease | None
    drain_token: ResultDrainToken | None
```

`prepare(request, context, *, sector=None, fill_metadata=None, owner_token: OwnerToken | None = None) -> dict`
preserves existing callers. Its
successful owner receipt registers the lease in the runtime-owned `WriterAuthority` by attempt ID and request
fingerprint. `dispatch(request, context, transport)` retrieves exactly that lease; a fresh commands facade over the
same runtime still works, and no caller mints a lease from the latest token. `claim_with_lease()` calls owner
`continue_plan()` with a `ContinuationPlanBuilder`; after the owner validates the lease and recomputes its relevant
dependency fingerprint, the owner supplies the exact current token and only then builds the claim plan. It advances
the lease and
registers a separate one-shot drain token immediately after durable claim and before transport entry. POST consumes
only `send_lease`. ACK→ACKNOWLEDGED, broker rejection→REJECTED, guard/kill-switch refusal→NOT_SENT, and
post-claim exception/cancellation→UNKNOWN all consume the drain exactly once. Day/fence/closing after claim does
not discard result evidence, while wrong/reused identity and owner/store fault remain fail-closed.
Runtime construction installs Task5's synchronous admission authority before commands/gateway binding. Its snapshot
reads `_day_generation`, `_day_fence_id`, and `_day_closed` directly and performs no await or checkpoint inference;
owner lease issue/continue remains the only consumer. The runtime registry is lookup plumbing only and cannot issue
or rewrite an owner lease.

- [ ] **Step 4: Replace the six lifecycle/command Wave1 mutate routes with typed plans**

Migrate lifecycle prepare, claim, abandon, record result, reconcile and commands bound-prepare. Preserve immutable
baseline inventory rows and require their AST-derived successor routes. Preserve lookup/replay idempotence and
compatibility `claim()->bool` only for test/setup callers.

- [ ] **Step 5: Run GREEN and the original command suites**

```bash
env -i PATH=/usr/bin:/bin LANG=C.UTF-8 TZ=UTC PYTHONDONTWRITEBYTECODE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
  /home/ubuntu/projects/qwq-ai-trader/venv/bin/python -m pytest \
  tests/test_recovery_projection_acceptance.py tests/test_recovery_projection_inventory.py \
  tests/test_execution_lifecycle.py tests/test_execution_command_owner.py tests/test_execution_command_flow.py \
  tests/test_execution_dispatch_reasons.py tests/test_execution_command_shutdown.py \
  tests/test_execution_transport_killswitch_audit.py \
  -q -p no:cacheprovider --tb=short --show-capture=no
```

- [ ] **Step 6: Independent critical authority review and exact allowlist commit**

Review WriterLease and ResultDrainToken together. After blocking 0:

```bash
set -euo pipefail
git status --short
git diff --check -- src/execution/safety/writer_authority.py src/execution/safety/runtime.py \
  src/execution/safety/lifecycle.py src/execution/safety/commands.py src/execution/safety/gateway.py \
  tests/test_recovery_projection_acceptance.py tests/test_execution_command_owner.py \
  tests/test_execution_command_flow.py tests/test_execution_dispatch_reasons.py \
  tests/test_execution_command_shutdown.py tests/test_execution_transport_killswitch_audit.py
git add -- src/execution/safety/writer_authority.py src/execution/safety/runtime.py \
  src/execution/safety/lifecycle.py src/execution/safety/commands.py src/execution/safety/gateway.py \
  tests/test_recovery_projection_acceptance.py tests/test_execution_command_owner.py \
  tests/test_execution_command_flow.py tests/test_execution_dispatch_reasons.py \
  tests/test_execution_command_shutdown.py tests/test_execution_transport_killswitch_audit.py
git diff --cached --check
git diff --cached --name-only
git commit -m "feat(safety): bind command send and result authority" -- \
  src/execution/safety/writer_authority.py src/execution/safety/runtime.py \
  src/execution/safety/lifecycle.py src/execution/safety/commands.py src/execution/safety/gateway.py \
  tests/test_recovery_projection_acceptance.py \
  tests/test_execution_command_owner.py tests/test_execution_command_flow.py \
  tests/test_execution_dispatch_reasons.py tests/test_execution_command_shutdown.py \
  tests/test_execution_transport_killswitch_audit.py
```

- [ ] **Step 7: Verify the Task8 commit and push separately**

```bash
set -euo pipefail
git show --stat --oneline --summary HEAD
git push -u origin HEAD
```

---

### Task 9: Inbox, Fill, Economics, and Journal Typed Migration

**Files:**
- Create: `src/execution/safety/fill_mutation.py`
- Modify: `src/execution/safety/application.py` (`FillReduction`, fill write-set guards, supersede, receive, apply).
- Modify: `src/execution/safety/runtime.py::_reduce`
- Modify: `src/execution/safety/economics.py`, `src/execution/safety/lifecycle.py`,
  `src/execution/safety/protection.py`, `src/execution/safety/initial_r.py`,
  `src/execution/safety/protection_recovery.py`, `src/execution/safety/journal_delivery.py`.
- Create: `tests/test_execution_fill_mutation_plans.py`
- Extend: `tests/test_recovery_projection_acceptance.py`
- Test: `tests/test_execution_fill_application.py`, `tests/test_execution_economics.py`,
  `tests/test_execution_protection.py`, `tests/test_execution_initial_r.py`,
  `tests/test_execution_initial_r_runtime.py`, `tests/test_execution_protection_recovery.py`,
  `tests/test_execution_journal_delivery.py`, `tests/test_recovery_projection_inventory.py`.

**Interfaces:**
- Consumes: owner `apply_plan()` and Task8 receipts.
- Produces: immutable `MutationFragment`, `EconomicFillResult`, `ProtectionFillResult`, `FillPlanResult`,
  `build_fill_mutation_plan()`, typed inbox receive/supersede/conflict/fill/journal ACK routes, and exact economic
  result parity.

- [ ] **Step 1: Write REDs for two-commit apply, conflict, reservations and replay**

```python
def test_fill_plan_preserves_encoded_checkpoint_and_domain_result(fill_pair):
    async def scenario():
        legacy = await fill_pair.run_control()
        typed = await fill_pair.run_indexed()
        assert typed.encoded_checkpoint == legacy.encoded_checkpoint
        assert typed.receipt == legacy.receipt
        assert typed.publisher_payloads == legacy.publisher_payloads
    asyncio.run(scenario())
```

Cover inbox RECEIVED, stale supersede, NEEDS_RECONCILIATION, partial/full BUY/SELL, unknown planned risk,
reservation release, lots/initial-R/protection/outbox/journal and duplicate commit replay. Add exact REDs:
`test_fill_builder_returns_only_frozen_typed_edits`,
`test_fill_builder_does_not_mutate_or_copy_the_checkpoint_root`,
`test_fill_initial_stop_replay_and_outbox_share_target_revision`,
`test_full_sell_exact_64_row_changes_commits`,
`test_full_sell_row_65_rejects_before_store_publish_or_full_build`,
`test_fill_exact_256_dependency_edges_commits`,
`test_fill_edge_257_rejects_before_store_publish_or_full_build`,
`test_duplicate_fill_commit_returns_stored_result_without_rebuilding_fragments`,
`test_journal_ack_plan_preserves_initial_r_prerequisites`, and
`test_apply_ticket_owns_receive_and_fill_commits_in_fifo_order`.

- [ ] **Step 2: Run the fill-plan RED before implementation**

```bash
env -i PATH=/usr/bin:/bin LANG=C.UTF-8 TZ=UTC PYTHONDONTWRITEBYTECODE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
  /home/ubuntu/projects/qwq-ai-trader/venv/bin/python -m pytest \
  tests/test_execution_fill_mutation_plans.py tests/test_recovery_projection_acceptance.py \
  tests/test_execution_fill_application.py tests/test_execution_economics.py \
  -q -p no:cacheprovider --tb=short --show-capture=no
```

Expected: nonzero because the immutable fragment API and typed fill route do not exist.

- [ ] **Step 3: Implement immutable domain fragments without a whole-candidate reducer**

```python
@dataclass(frozen=True, slots=True)
class MutationFragment:
    edits: tuple[MutationEdit, ...]
    result: FrozenJSON


@dataclass(frozen=True, slots=True)
class EconomicFillResult:
    before_position: FrozenJSON | None
    after_position: FrozenJSON | None
    fill_kind: str
    attempt_id: str
    intent_id: str
    outbox_key: str


@dataclass(frozen=True, slots=True)
class ProtectionFillResult:
    status: Literal["ready", "degraded", "exempt"]


@dataclass(frozen=True, slots=True)
class FillPlanResult:
    protection_status: Literal["ready", "degraded", "exempt"]
    journal_pending: bool


def build_fill_mutation_plan(
    state: Mapping[str, object], *, base_token: OwnerToken, observation: FillObservation,
    delta: FillDelta, now: datetime, target_revision: int,
) -> MutationPlan: ...
```

Economics, protection, initial-stop and replay helpers consume a read-only `PlannedCheckpointView` and return
fragments through the Task2 budget-aware collector. They never return/mutate a checkpoint candidate. The outbox
source version, initial-stop evidence, replay digest, inbox/cursor, attempts, pending sector, portfolio/risk, lots
and protection edits share the exact target revision. Full SELL or callback overflow rejects atomically; never
chunk, truncate, or use a normal-route full build.

- [ ] **Step 4: Run only pure builder GREEN and obtain preliminary critical economic review without committing**

```bash
env -i PATH=/usr/bin:/bin LANG=C.UTF-8 TZ=UTC PYTHONDONTWRITEBYTECODE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
  /home/ubuntu/projects/qwq-ai-trader/venv/bin/python -m pytest \
  tests/test_execution_fill_mutation_plans.py::test_fill_builder_returns_only_frozen_typed_edits \
  tests/test_execution_fill_mutation_plans.py::test_fill_builder_does_not_mutate_or_copy_the_checkpoint_root \
  -q -p no:cacheprovider --tb=short --show-capture=no
```

This is a dirty-tree design checkpoint only. The two selected tests invoke pure builders directly and do not execute
the still-old runtime/application callers. Do not run or claim GREEN for economics/protection/initial-R runtime or
typed-route integration yet, and do not commit the new fragment-returning helper contract while those callers still
expect whole-candidate objects. The reviewer checks the pure builders now; every domain/runtime/integration file runs
only at the atomic GREEN/commit boundary in Step6 after `application.py`, `runtime.py`, and journal delivery switch.

- [ ] **Step 5: Route four application commits and journal ACK through typed plans**

The owner applies explicit row/scalar edits; the domain reducer cannot return or mutate a whole candidate root.
One `apply` ticket owns at most inbox+fill commits. Duplicate lookup happens before fragment construction, so replay
cannot repeat closure effects. The Task7 immutable manifest must resolve supersede/receive/conflict/fill and journal
ACK to their declared successors without editing the fixture.

- [ ] **Step 6: Run integrated GREEN, obtain independent review, and atomically commit builders plus callers**

```bash
env -i PATH=/usr/bin:/bin LANG=C.UTF-8 TZ=UTC PYTHONDONTWRITEBYTECODE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
  /home/ubuntu/projects/qwq-ai-trader/venv/bin/python -m pytest \
  tests/test_recovery_projection_acceptance.py tests/test_recovery_projection_inventory.py \
  tests/test_execution_fill_mutation_plans.py tests/test_execution_fill_application.py \
  tests/test_execution_economics.py tests/test_execution_protection.py \
  tests/test_execution_initial_r.py tests/test_execution_initial_r_runtime.py \
  tests/test_execution_protection_recovery.py tests/test_execution_journal_delivery.py \
  -q -p no:cacheprovider --tb=short --show-capture=no
```

After blocking 0:

```bash
set -euo pipefail
git status --short
git diff --check -- src/execution/safety/fill_mutation.py src/execution/safety/application.py \
  src/execution/safety/runtime.py src/execution/safety/economics.py src/execution/safety/lifecycle.py \
  src/execution/safety/protection.py src/execution/safety/initial_r.py \
  src/execution/safety/protection_recovery.py src/execution/safety/journal_delivery.py \
  tests/test_execution_fill_mutation_plans.py tests/test_recovery_projection_acceptance.py \
  tests/test_execution_fill_application.py tests/test_execution_economics.py \
  tests/test_execution_protection.py tests/test_execution_initial_r.py \
  tests/test_execution_initial_r_runtime.py tests/test_execution_protection_recovery.py \
  tests/test_execution_journal_delivery.py
git add -- src/execution/safety/fill_mutation.py src/execution/safety/application.py \
  src/execution/safety/runtime.py src/execution/safety/economics.py src/execution/safety/lifecycle.py \
  src/execution/safety/protection.py src/execution/safety/initial_r.py \
  src/execution/safety/protection_recovery.py src/execution/safety/journal_delivery.py \
  tests/test_execution_fill_mutation_plans.py tests/test_recovery_projection_acceptance.py \
  tests/test_execution_fill_application.py tests/test_execution_economics.py \
  tests/test_execution_protection.py tests/test_execution_initial_r.py \
  tests/test_execution_initial_r_runtime.py tests/test_execution_protection_recovery.py \
  tests/test_execution_journal_delivery.py
git diff --cached --check
git diff --cached --name-only
git commit -m "refactor(safety): route fill and journal through typed plans" -- \
  src/execution/safety/fill_mutation.py src/execution/safety/application.py \
  src/execution/safety/runtime.py src/execution/safety/economics.py src/execution/safety/lifecycle.py \
  src/execution/safety/protection.py src/execution/safety/initial_r.py \
  src/execution/safety/protection_recovery.py src/execution/safety/journal_delivery.py \
  tests/test_execution_fill_mutation_plans.py tests/test_recovery_projection_acceptance.py \
  tests/test_execution_fill_application.py tests/test_execution_economics.py \
  tests/test_execution_protection.py tests/test_execution_initial_r.py \
  tests/test_execution_initial_r_runtime.py tests/test_execution_protection_recovery.py \
  tests/test_execution_journal_delivery.py
```

- [ ] **Step 7: Verify the atomic Task9 commit and push after both review passes**

```bash
set -euo pipefail
git show --stat --oneline --summary HEAD
git push -u origin HEAD
```

---

### Task 10: Day, Quote, Protection Recovery, Repair, and Initial-R Migration

**Files:**
- Modify: `src/execution/safety/runtime.py`
- Modify: `src/execution/safety/protection_producer.py` only for the four strict token call sites
  (`quote`, `observe_market`, pending release, admission resume); Task13 performs the remaining indexed-read rewrite.
- Extend: `tests/test_recovery_projection_acceptance.py`
- Create: `tests/test_protection_producer_recovery_index.py` with the strict-token bridge REDs; Task13 extends it.
- Test: `tests/test_execution_runtime.py`, `tests/test_execution_day_recovery.py`,
  `tests/test_execution_protection_recovery.py`, `tests/test_execution_initial_r.py`,
  `tests/test_execution_p1_resume.py`, `tests/test_execution_p1_pending.py`.

**Interfaces:**
- Consumes: owner typed plans and ticket budgets.
- Produces: typed `_day_command`, exact-token two-commit `quote`, pending release, command-specific admission resume,
  repair and initial-R routes, plus immutable `ResumeProtectionResult`.

- [ ] **Step 1: Write REDs for real two-commit quote and fault windows**

```python
def test_quote_roundtrip_has_two_typed_commits_and_no_admission_after_return(runtime):
    async def scenario():
        before = runtime.owner.version
        token = (await runtime.owner.acquire_producer_view()).token
        await runtime.quote(
            "005930", Decimal("10000"), market_data=MARKET, intent_id="sell-1", owner_token=token,
        )
        assert runtime.owner.version == before + 2
        assert runtime.owner.state["protection_quote_admissions"] == {}
        assert runtime.owner.full_builder_calls == 0
    asyncio.run(scenario())
```

Cover stale/duplicate prevalidation store 0, admission completion cancellation, day fence, pending ambiguity, resume
source version, repair and initial-R expected-version checks. Add exact REDs:
`test_stale_producer_quote_token_rejects_before_store_publisher_and_epoch_change`,
`test_stale_release_token_has_zero_store_effect`, `test_stale_resume_token_has_zero_store_effect`,
`test_quote_completion_uses_owner_issued_lease_not_original_token`, and
`test_quote_self_commit_lease_rejects_fault_day_fence_and_relevant_change`, including exact
`market_sources`/`entry_quotes` changes before completion. Add producer caller REDs
`test_quote_receives_exact_supporting_view_token`,
`test_observe_market_receives_exact_supporting_view_token`,
`test_release_pending_receives_exact_supporting_view_token`, and
`test_resume_receives_exact_supporting_view_token_and_command_id`.

- [ ] **Step 2: Run the Wave3/token RED before implementation**

```bash
env -i PATH=/usr/bin:/bin LANG=C.UTF-8 TZ=UTC PYTHONDONTWRITEBYTECODE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
  /home/ubuntu/projects/qwq-ai-trader/venv/bin/python -m pytest \
  tests/test_recovery_projection_acceptance.py tests/test_execution_p1_resume.py \
  tests/test_execution_p1_pending.py tests/test_execution_runtime.py \
  tests/test_protection_producer_recovery_index.py \
  -k 'token or quote or resume or pending or day or fence' \
  -q -p no:cacheprovider --tb=short --show-capture=no
```

- [ ] **Step 3: Freeze the first-admission API and migrate all seven Wave3 routes**

```python
@dataclass(frozen=True, slots=True)
class ResumeProtectionResult:
    command_id: str
    symbol: str
    intent_id: str | None
    price: Decimal
    disposition: str
    decision: tuple | None


async def quote(
    self,
    symbol: str,
    price: Decimal,
    *,
    market_data=None,
    intent_id: str | None = None,
    market_as_of: datetime | None = None,
    source: str | None = None,
    source_event_id: str | None = None,
    entry_observation=None,
    owner_token: OwnerToken | None = None,
): ...


async def observe_market(
    self,
    event,
    *,
    intent_id=None,
    market_data=None,
    owner_token: OwnerToken | None = None,
): ...


async def release_protection_pending(
    self, symbol: str, *, owner_token: OwnerToken,
) -> bool: ...
async def resume_protection_admission(
    self, command_id: str, *, owner_token: OwnerToken,
) -> ResumeProtectionResult: ...
```

`None` is compatibility-only for a direct non-producer quote caller and captures `owner.current_token` before its
first await; an explicit token is never replaced by a newer one. The admission plan uses that exact base token and
asks the owner for a quote `WriterLease`; completion calls `continue_plan()` with that lease and an owner-side
builder, so an unrelated revision may be admitted only after relevant dependency revalidation and the resulting
plan uses the owner's exact current token. Release and
resume are single-commit exact-token plans. Resume targets one view-selected `command_id` and returns proof instead
of requiring a before/after full-map diff. Preserve one global quote gate, no per-symbol lock, and derive current
route resolution from the immutable Task7 inventory.

In the same atomic change, each existing producer call site acquires a `ProducerReadView` immediately before the
writer call, passes `view.token`, discards the view after the await, and reacquires before another owner-backed
decision. This minimal bridge preserves the existing producer read logic until Task13, but there is no intermediate
commit in which the strict runtime signature is paired with old no-argument/list-returning producer calls.

- [ ] **Step 4: Run GREEN and obtain independent authority review**

```bash
env -i PATH=/usr/bin:/bin LANG=C.UTF-8 TZ=UTC PYTHONDONTWRITEBYTECODE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
  /home/ubuntu/projects/qwq-ai-trader/venv/bin/python -m pytest \
  tests/test_recovery_projection_acceptance.py tests/test_recovery_projection_inventory.py \
  tests/test_execution_runtime.py tests/test_execution_day_recovery.py \
  tests/test_execution_protection_recovery.py tests/test_execution_initial_r.py \
  tests/test_execution_p1_resume.py tests/test_execution_p1_pending.py \
  tests/test_protection_producer_recovery_index.py \
  -q -p no:cacheprovider --tb=short --show-capture=no
```

- [ ] **Step 5: Stage the exact Task10 allowlist and commit after blocking 0**

```bash
set -euo pipefail
git status --short
git diff --check -- src/execution/safety/runtime.py src/execution/safety/protection_producer.py \
  tests/test_recovery_projection_acceptance.py tests/test_protection_producer_recovery_index.py \
  tests/test_execution_runtime.py tests/test_execution_day_recovery.py \
  tests/test_execution_protection_recovery.py tests/test_execution_initial_r.py \
  tests/test_execution_p1_resume.py tests/test_execution_p1_pending.py
git add -- src/execution/safety/runtime.py src/execution/safety/protection_producer.py \
  tests/test_recovery_projection_acceptance.py tests/test_protection_producer_recovery_index.py \
  tests/test_execution_runtime.py tests/test_execution_day_recovery.py \
  tests/test_execution_protection_recovery.py tests/test_execution_initial_r.py \
  tests/test_execution_p1_resume.py tests/test_execution_p1_pending.py
git diff --cached --check
git diff --cached --name-only
git commit -m "refactor(safety): migrate day and protection owner edits" -- \
  src/execution/safety/runtime.py src/execution/safety/protection_producer.py \
  tests/test_recovery_projection_acceptance.py tests/test_protection_producer_recovery_index.py \
  tests/test_execution_runtime.py tests/test_execution_day_recovery.py \
  tests/test_execution_protection_recovery.py tests/test_execution_initial_r.py \
  tests/test_execution_p1_resume.py tests/test_execution_p1_pending.py
```

- [ ] **Step 6: Verify the Task10 commit and push separately**

```bash
set -euo pipefail
git show --stat --oneline --summary HEAD
git push -u origin HEAD
```

---

### Task 11: Policy, Qualification, Risk, and Regime Typed Migration

**Files:**
- Risk core: modify `commands.py`, `risk_sources.py`, `risk_input_seal.py`.
- Intraday callback: modify `intraday_owner.py`, `risk_transition.py`, `protection_recovery.py`,
  `regime_horizon.py`.
- Regime callback/baselines: modify `regime_owner.py`, `regime_commands.py`, `regime_application.py`,
  `regime_morning.py`, `regime_morning_commands.py`, `regime_horizon.py`.
- Tests: `test_execution_risk_sources.py`, `test_execution_risk_input_seal.py`,
  `test_execution_intraday_owner.py`, all four `test_execution_intraday_replay*.py`,
  `test_execution_regime_completion_guard.py`, `test_execution_regime_completion_races_review.py`,
  `test_execution_regime_precommit_review.py`, both baseline-scope suites, all regime morning/noon suites,
  plus policy-generation, qualification and decision-fact suites.

**Interfaces:**
- Consumes: Task2 policy edits and owner typed plan API.
- Produces: `CompletionPlanBuilder`, immutable intraday/regime `MutationFragment`s, atomic risk completion plans,
  incremental horizon update, and remaining Wave4 product legacy count 0.

- [ ] **Step 1: Write REDs for nested callback effects and replay**

```python
def test_wave4_inventory_reaches_zero_product_legacy_routes(inventory_result):
    assert inventory_result.product_legacy_routes == ()
    assert inventory_result.normal_full_builder_routes == ()
```

Add route-specific tests that rejected callback, future/stale source, invalid risk completion, regime baseline race,
and policy history append either commit all owner-recorded edits or commit nothing. Duplicate command lookup must
return the stored result without executing closure side effects. Add exact REDs:
`test_risk_begin_builds_typed_ticket_plan`, `test_risk_complete_rejected_callback_commits_nothing`,
`test_duplicate_risk_completion_does_not_execute_callback`, `test_input_reseal_conflict_is_one_typed_plan`,
`test_intraday_completion_contains_policy_protection_outbox_horizon_and_replay_edits`,
`test_intraday_completion_fragment_is_frozen_and_does_not_mutate_input`,
`test_regime_completion_plans_trend_morning_noon_and_classifier_branches`,
`test_regime_application_protection_and_replay_commit_atomically`,
`test_regime_baseline_race_has_zero_partial_edits`,
`test_callback_exact_64_rows_and_256_edges_commits`, and
`test_callback_row_65_and_edge_257_rejects_with_zero_effects`.

- [ ] **Step 2: Run RED across direct routes and the actual nested callbacks**

```bash
env -i PATH=/usr/bin:/bin LANG=C.UTF-8 TZ=UTC PYTHONDONTWRITEBYTECODE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
  /home/ubuntu/projects/qwq-ai-trader/venv/bin/python -m pytest \
  tests/test_execution_risk_sources.py tests/test_execution_risk_input_seal.py \
  tests/test_execution_intraday_owner.py tests/test_execution_intraday_replay.py \
  tests/test_execution_intraday_replay_completeness.py tests/test_execution_intraday_replay_integration.py \
  tests/test_execution_intraday_replay_integrity.py tests/test_execution_regime_completion_guard.py \
  tests/test_execution_regime_completion_races_review.py tests/test_execution_regime_precommit_review.py \
  tests/test_recovery_projection_inventory.py \
  -q -p no:cacheprovider --tb=short --show-capture=no
```

- [ ] **Step 3: Freeze the callback contract and migrate risk core**

```python
CompletionPlanBuilder = Callable[
    [PlannedCheckpointView, RefreshTicket, FrozenJSON, int],
    MutationFragment,
]
```

Rename nested implementations to `_plan_completion` and make `_check_typed_completion` validate the exact bound
method. `RiskSourceCoordinator.complete()` combines the callback fragment and terminal risk-source row into one
plan without recursive `apply_plan()`. Duplicate lookup precedes callback construction. Migrate the four commands
publishers, risk begin/complete and risk input seal with leaf/table edits; no whole-candidate diff is permitted.

- [ ] **Step 4: Migrate intraday and replay fragments, then run a preliminary review without committing**

`IntradayRiskOwner._reduce`, `transition_intraday`, replay append and `advance_horizon(current, event)` return frozen
fragments for policy/protection/outbox/horizon/replay writes. All edits count in the same 64/256 budget.

```bash
env -i PATH=/usr/bin:/bin LANG=C.UTF-8 TZ=UTC PYTHONDONTWRITEBYTECODE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
  /home/ubuntu/projects/qwq-ai-trader/venv/bin/python -m pytest \
  tests/test_execution_risk_sources.py tests/test_execution_risk_input_seal.py \
  tests/test_execution_intraday_owner.py tests/test_execution_intraday_replay.py \
  tests/test_execution_intraday_replay_completeness.py tests/test_execution_intraday_replay_integration.py \
  tests/test_execution_intraday_replay_integrity.py tests/test_execution_policy_generations.py \
  tests/test_execution_qualification_publishers.py tests/test_execution_decision_facts.py \
  -q -p no:cacheprovider --tb=short --show-capture=no
```

This is a dirty-tree checkpoint. The exact bound-method callback contract is not committed while either intraday or
regime callers still expose the old callback shape. Critical review may start, but Steps3–5 form one atomic source
transition and one commit in Step6. Run the focused command on the same working tree after each slice and record the
result; a later slice may not use an earlier dirty-tree GREEN as evidence for a commit.

- [ ] **Step 5: Migrate regime callbacks/baselines and run the complete Wave4 GREEN matrix**

Baseline creation uses leaf/table edits even when `regime_policy` is absent. Regime completion combines source,
application, protection and replay fragments atomically and preserves exact target revision.

```bash
env -i PATH=/usr/bin:/bin LANG=C.UTF-8 TZ=UTC PYTHONDONTWRITEBYTECODE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
  /home/ubuntu/projects/qwq-ai-trader/venv/bin/python -m pytest \
  tests/test_recovery_projection_inventory.py tests/test_execution_regime_owner.py \
  tests/test_execution_regime_completion_guard.py tests/test_execution_regime_completion_races_review.py \
  tests/test_execution_regime_precommit_review.py tests/test_execution_regime_baseline_scope_guard.py \
  tests/test_execution_regime_baseline_scope_review.py tests/test_execution_regime_morning.py \
  tests/test_execution_regime_morning_acceptance.py tests/test_execution_regime_morning_dto.py \
  tests/test_execution_regime_morning_notifications.py tests/test_execution_regime_morning_read_barrier.py \
  tests/test_execution_regime_morning_review.py tests/test_execution_regime_morning_schema_review.py \
  tests/test_execution_regime_noon_fault_boundaries.py tests/test_execution_regime_noon_provenance_followup.py \
  tests/test_execution_regime_noon_replay.py tests/test_execution_regime_noon_replay_acceptance.py \
  tests/test_execution_regime_noon_review.py tests/test_execution_regime_noon_scheduler.py \
  -q -p no:cacheprovider --tb=short --show-capture=no
```

- [ ] **Step 6: Review zero-legacy evidence and atomically commit risk, intraday, and regime contracts/callers**

```bash
set -euo pipefail
git status --short
git diff --check -- src/execution/safety/commands.py src/execution/safety/risk_sources.py \
  src/execution/safety/risk_input_seal.py src/execution/safety/intraday_owner.py \
  src/execution/safety/risk_transition.py src/execution/safety/protection_recovery.py \
  src/execution/safety/regime_owner.py src/execution/safety/regime_commands.py \
  src/execution/safety/regime_application.py src/execution/safety/regime_morning.py \
  src/execution/safety/regime_morning_commands.py src/execution/safety/regime_horizon.py \
  tests/test_recovery_projection_inventory.py tests/test_execution_risk_sources.py \
  tests/test_execution_risk_input_seal.py tests/test_execution_policy_generations.py \
  tests/test_execution_qualification_publishers.py tests/test_execution_decision_facts.py \
  tests/test_execution_intraday_owner.py tests/test_execution_intraday_replay.py \
  tests/test_execution_intraday_replay_completeness.py tests/test_execution_intraday_replay_integration.py \
  tests/test_execution_intraday_replay_integrity.py \
  tests/test_execution_regime_owner.py tests/test_execution_regime_completion_guard.py \
  tests/test_execution_regime_completion_races_review.py tests/test_execution_regime_precommit_review.py \
  tests/test_execution_regime_baseline_scope_guard.py tests/test_execution_regime_baseline_scope_review.py \
  tests/test_execution_regime_morning.py tests/test_execution_regime_morning_acceptance.py \
  tests/test_execution_regime_morning_dto.py tests/test_execution_regime_morning_notifications.py \
  tests/test_execution_regime_morning_read_barrier.py tests/test_execution_regime_morning_review.py \
  tests/test_execution_regime_morning_schema_review.py tests/test_execution_regime_noon_fault_boundaries.py \
  tests/test_execution_regime_noon_provenance_followup.py tests/test_execution_regime_noon_replay.py \
  tests/test_execution_regime_noon_replay_acceptance.py tests/test_execution_regime_noon_review.py \
  tests/test_execution_regime_noon_scheduler.py
git add -- src/execution/safety/commands.py src/execution/safety/risk_sources.py \
  src/execution/safety/risk_input_seal.py src/execution/safety/intraday_owner.py \
  src/execution/safety/risk_transition.py src/execution/safety/protection_recovery.py \
  src/execution/safety/regime_owner.py src/execution/safety/regime_commands.py \
  src/execution/safety/regime_application.py src/execution/safety/regime_morning.py \
  src/execution/safety/regime_morning_commands.py src/execution/safety/regime_horizon.py \
  tests/test_recovery_projection_inventory.py tests/test_execution_risk_sources.py \
  tests/test_execution_risk_input_seal.py tests/test_execution_policy_generations.py \
  tests/test_execution_qualification_publishers.py tests/test_execution_decision_facts.py \
  tests/test_execution_intraday_owner.py tests/test_execution_intraday_replay.py \
  tests/test_execution_intraday_replay_completeness.py tests/test_execution_intraday_replay_integration.py \
  tests/test_execution_intraday_replay_integrity.py \
  tests/test_execution_regime_owner.py tests/test_execution_regime_completion_guard.py \
  tests/test_execution_regime_completion_races_review.py tests/test_execution_regime_precommit_review.py \
  tests/test_execution_regime_baseline_scope_guard.py tests/test_execution_regime_baseline_scope_review.py \
  tests/test_execution_regime_morning.py tests/test_execution_regime_morning_acceptance.py \
  tests/test_execution_regime_morning_dto.py tests/test_execution_regime_morning_notifications.py \
  tests/test_execution_regime_morning_read_barrier.py tests/test_execution_regime_morning_review.py \
  tests/test_execution_regime_morning_schema_review.py tests/test_execution_regime_noon_fault_boundaries.py \
  tests/test_execution_regime_noon_provenance_followup.py tests/test_execution_regime_noon_replay.py \
  tests/test_execution_regime_noon_replay_acceptance.py tests/test_execution_regime_noon_review.py \
  tests/test_execution_regime_noon_scheduler.py
git diff --cached --check
git diff --cached --name-only
git commit -m "refactor(safety): migrate risk and regime owner edits" -- \
  src/execution/safety/commands.py src/execution/safety/risk_sources.py \
  src/execution/safety/risk_input_seal.py src/execution/safety/intraday_owner.py \
  src/execution/safety/risk_transition.py src/execution/safety/protection_recovery.py \
  src/execution/safety/regime_owner.py src/execution/safety/regime_commands.py \
  src/execution/safety/regime_application.py src/execution/safety/regime_morning.py \
  src/execution/safety/regime_morning_commands.py src/execution/safety/regime_horizon.py \
  tests/test_recovery_projection_inventory.py tests/test_execution_risk_sources.py \
  tests/test_execution_risk_input_seal.py tests/test_execution_policy_generations.py \
  tests/test_execution_qualification_publishers.py tests/test_execution_decision_facts.py \
  tests/test_execution_intraday_owner.py tests/test_execution_intraday_replay.py \
  tests/test_execution_intraday_replay_completeness.py tests/test_execution_intraday_replay_integration.py \
  tests/test_execution_intraday_replay_integrity.py \
  tests/test_execution_regime_owner.py tests/test_execution_regime_completion_guard.py \
  tests/test_execution_regime_completion_races_review.py tests/test_execution_regime_precommit_review.py \
  tests/test_execution_regime_baseline_scope_guard.py tests/test_execution_regime_baseline_scope_review.py \
  tests/test_execution_regime_morning.py tests/test_execution_regime_morning_acceptance.py \
  tests/test_execution_regime_morning_dto.py tests/test_execution_regime_morning_notifications.py \
  tests/test_execution_regime_morning_read_barrier.py tests/test_execution_regime_morning_review.py \
  tests/test_execution_regime_morning_schema_review.py tests/test_execution_regime_noon_fault_boundaries.py \
  tests/test_execution_regime_noon_provenance_followup.py tests/test_execution_regime_noon_replay.py \
  tests/test_execution_regime_noon_replay_acceptance.py tests/test_execution_regime_noon_review.py \
  tests/test_execution_regime_noon_scheduler.py
```

- [ ] **Step 7: Verify the atomic Task11 commit and push separately**

```bash
set -euo pipefail
git show --stat --oneline --summary HEAD
git push -u origin HEAD
```

At this boundary `ACTIVATED` may be tested, but no product consumer is switched until Tasks12–14 pass.

---

### Task 12: Runtime Composition Frame and Exact RAM Algebra

**Files:**
- Create: `src/execution/safety/recovery_composition.py`
- Modify: `src/execution/safety/application.py`, `src/execution/safety/runtime.py`
- Modify: `src/execution/safety/protection_producer.py`, `src/core/engine.py`,
  `src/execution/safety/factory.py` for tracked mutation seams only; Task13 changes consumer logic.
- Modify: `src/execution/safety/intraday_owner.py`, `src/execution/safety/regime_owner.py` to replace direct
  `_intraday_writer`/`_regime_writer` assignment with runtime tracked installers.
- Extend: `tests/test_recovery_projection.py`, `tests/test_recovery_projection_faults.py`,
  `tests/test_execution_runtime.py`, `tests/test_execution_day_recovery.py`,
  `tests/test_execution_p1_install.py`, `tests/test_execution_intraday_owner.py`,
  `tests/test_execution_regime_owner.py`.

**Interfaces:**
- Consumes: Task1 join counters/delta and Task6 publication port.
- Produces: `BindingFingerprint`, `RuntimeRecoveryObservation`, `RecoveryCompositionCoordinator`, tracked producer/
  quote lock wrappers, and a complete B-frame mutation seam inventory.

- [ ] **Step 1: Write multiplicity, pending fallback, type-domain, ABA and lock-generation REDs**

```python
def test_episode_add_delete_updates_b_without_owner_revision(composition):
    revision = composition.owner_frame.token.revision
    before = composition.runtime_observation
    composition.replace_episode("005930", None, episode("intent-1", ("sell_all", 1, "r")))
    after = composition.runtime_observation
    assert after.producer_epoch == before.producer_epoch + 1
    assert composition.owner_frame.token.revision == revision
    assert after.cross_counts["protection_unsubmitted"] == 0
```

Add pair-loop multiplicity, both-wrong-one-count, same-symbol/same-intent pending, episode add-delete ABA,
bool/float/Decimal/list/length/identity invalidity, overflow, additive repair counts, map overwrite and no-history-
iteration spies. Add exact generation REDs for same-key failure overwrite, day close/open ABA, runtime task
completion, every runtime binding, reconciler start/completion/cancel/progress ABA, runtime/engine shutdown,
engine handler register/unregister ABA, factory producer installation, restart cooldown/checked retry,
recovery-required same-cardinality overwrite, invariant set/clear, and all three lock acquire/release ABAs.
Add `test_every_legacy_capture_field_is_tracked_or_explicitly_excluded`: a literal inventory maps every field read by
the pre-N5 capture to one seam-table generation, or to an explicit operational-only exclusion with rationale and a
test proving it cannot alter completeness, binding, currentness or a published finding.

- [ ] **Step 2: Run RED**

```bash
env -i PATH=/usr/bin:/bin LANG=C.UTF-8 TZ=UTC PYTHONDONTWRITEBYTECODE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
  /home/ubuntu/projects/qwq-ai-trader/venv/bin/python -m pytest \
  tests/test_recovery_projection.py tests/test_recovery_projection_faults.py \
  -q -p no:cacheprovider --tb=short --show-capture=no
```

- [ ] **Step 3: Implement the await-free coordinator and literal O(1) equations**

```python
class RecoveryCompositionCoordinator:
    def runtime_changed(self, reason: RuntimeObservationChange) -> None: ...
    def binding_changed(self, reason: BindingChange) -> None: ...
    def producer_changed(self, reason: ProducerObservationChange) -> None: ...
    def lock_changed(self, slot: RecoveryLock, locked: bool) -> None: ...
    def replace_episode(
        self, symbol: str, old: ProducerEpisodeFact | None, new: ProducerEpisodeFact | None,
    ) -> None: ...


def _add_episode(self, fact):
    intent = fact.intent_id
    key = ProtectionJoinKey(intent, fact.symbol, fact.decision)
    mismatch = self._join.audits_by_intent.get(intent) - self._join.audits_by_key.get(key)
    self._cross = self._cross.add("protection_link_inconsistent", mismatch)
    if self._episodes_by_intent.get(intent) == 0:
        self._cross = self._cross.add("protection_unsubmitted",
                                      -self._join.unsubmitted_by_intent.get(intent))
    if self._episodes_by_symbol_intent.get((fact.symbol, intent)) == 0:
        self._cross = self._cross.add("protection_link_inconsistent",
                                      -self._join.pending_fallback_by_pair.get((fact.symbol, intent)))
```

Removal is the exact inverse. Coordinator methods acquire no async lock, perform no await, and never call owner,
producer or quote gates. Existing async order stays producer → global quote → owner ticket.

- [ ] **Step 4: Wire runtime generation/binding and B-before-A publication**

Runtime constructs the coordinator before owner, passes an await-free publication port, increments generations for
failure/day/binding/shutdown fields, installs B first and `current_for_producer=True` A last. Restore calls
`activate_restored_owner()` only after day/fence/bindings are restored.

Every B input must use this fixed seam matrix; init is the only direct-assignment allowlist:

| Mutable seam | Generation/helper | File/owner |
|---|---|---|
| protection failure add/overwrite/delete and unattributed latch | `runtime_changed(PROTECTION_FAILURE)` | `runtime.py` failure helpers |
| day generation/fence close/open/restore | `runtime_changed(DAY_FENCE)` | `runtime.py` day helpers |
| protection/day/command task add/remove and result failure | `runtime_changed(RUNTIME_DRAIN)` | `runtime.py` task helpers |
| attach, gateway, writer, producer identities | `binding_changed(RUNTIME_BINDING)` | `runtime.py` tracked installers |
| reconciler start/config/task identity and completion | `binding_changed(RECONCILER_BINDING)` | `runtime.py` start/done callback |
| reconciler cycle/freshness/progress/reason/targets | `runtime_changed(RECONCILER_STATE)` | `runtime.py` reconciler helpers |
| runtime shutdown before first await | `runtime_changed(SHUTDOWN)` | `runtime.py::shutdown` |
| engine runtime bind and execution accepting | `binding_changed(ENGINE_BINDING)` | `core/engine.py::bind_execution_runtime` |
| handler prepend/append/remove | bound runtime `binding_changed(HANDLER_SET)` | `core/engine.py::register_handler/unregister_handler` |
| engine shutdown before first await | `runtime_changed(ENGINE_SHUTDOWN)` | `core/engine.py::_shutdown` |
| factory producer install and first handler | tracked installer + `register_handler(prepend=True)` | `factory.py` |
| episode add/delete/replace including rejection/reason | `_replace_episode` + O(1) algebra | `protection_producer.py` |
| recovery-required replacement | `_replace_recovery_required` | `protection_producer.py` |
| restart originals/retries/checked/cooldown | `_replace_restart_state` | `protection_producer.py` |
| invariant set/clear | `_replace_invariant` | `protection_producer.py` |
| producer, quote and owner lock acquire/release | `lock_changed(slot, held)` | producer/runtime/owner gate wrappers |

`register_handler(event_type, handler, *, prepend=False)` replaces factory's direct list insertion. Producer/quote
gate context
managers publish lock state synchronously and preserve producer → global quote → owner ticket order. Reconciler
done callbacks remove the task, retrieve cancellation/exception, and update B in one synchronous transition.
Task12 also replaces **every** direct producer mutation of episode, recovery-required, restart, cooldown/checked,
and invariant state with the listed helpers before its GREEN. Task13 may consume those helpers while migrating reads,
but must not be the first task to route any B-frame writer. Likewise, intraday/regime writer installation goes
through `runtime.install_intraday_writer()`/`install_regime_writer()` here; no direct identity assignment remains.

- [ ] **Step 5: Run GREEN and obtain independent B-frame/lock-order review**

```bash
env -i PATH=/usr/bin:/bin LANG=C.UTF-8 TZ=UTC PYTHONDONTWRITEBYTECODE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
  /home/ubuntu/projects/qwq-ai-trader/venv/bin/python -m pytest \
  tests/test_recovery_projection.py tests/test_recovery_projection_faults.py \
  tests/test_execution_runtime.py tests/test_execution_day_recovery.py tests/test_execution_p1_install.py \
  tests/test_execution_intraday_owner.py tests/test_execution_regime_owner.py \
  -q -p no:cacheprovider --tb=short --show-capture=no
```

- [ ] **Step 6: Stage the exact Task12 allowlist and commit after blocking 0**

```bash
set -euo pipefail
git status --short
git diff --check -- src/execution/safety/recovery_composition.py src/execution/safety/application.py \
  src/execution/safety/runtime.py src/execution/safety/intraday_owner.py \
  src/execution/safety/regime_owner.py \
  src/execution/safety/protection_producer.py src/core/engine.py src/execution/safety/factory.py \
  tests/test_recovery_projection.py tests/test_recovery_projection_faults.py \
  tests/test_execution_runtime.py tests/test_execution_day_recovery.py tests/test_execution_p1_install.py \
  tests/test_execution_intraday_owner.py tests/test_execution_regime_owner.py
git add -- src/execution/safety/recovery_composition.py src/execution/safety/application.py \
  src/execution/safety/runtime.py src/execution/safety/intraday_owner.py \
  src/execution/safety/regime_owner.py \
  src/execution/safety/protection_producer.py src/core/engine.py src/execution/safety/factory.py \
  tests/test_recovery_projection.py tests/test_recovery_projection_faults.py \
  tests/test_execution_runtime.py tests/test_execution_day_recovery.py tests/test_execution_p1_install.py \
  tests/test_execution_intraday_owner.py tests/test_execution_regime_owner.py
git diff --cached --check
git diff --cached --name-only
git commit -m "feat(safety): compose durable and runtime recovery evidence" -- \
  src/execution/safety/recovery_composition.py src/execution/safety/application.py \
  src/execution/safety/runtime.py src/execution/safety/intraday_owner.py \
  src/execution/safety/regime_owner.py \
  src/execution/safety/protection_producer.py src/core/engine.py src/execution/safety/factory.py \
  tests/test_recovery_projection.py tests/test_recovery_projection_faults.py \
  tests/test_execution_runtime.py tests/test_execution_day_recovery.py tests/test_execution_p1_install.py \
  tests/test_execution_intraday_owner.py tests/test_execution_regime_owner.py
```

- [ ] **Step 7: Verify the Task12 commit and push separately**

```bash
set -euo pipefail
git show --stat --oneline --summary HEAD
git push -u origin HEAD
```

---

### Task 13: Producer Indexed-View Migration

**Files:**
- Modify: `src/execution/safety/protection_producer.py`
- Modify: `src/execution/safety/runtime.py`, `src/execution/safety/gateway.py`,
  `src/execution/safety/commands.py`, `src/core/engine.py` for exact-token handoff and removal of product SELL
  pre-reads.
- Extend: `tests/test_protection_producer_recovery_index.py` created in Task10.
- Test: `tests/test_execution_p1_producer.py`, `tests/test_execution_p1_producer_recovery.py`,
  `tests/test_execution_p1_resume.py`, `tests/test_execution_p1_pending.py`,
  `tests/test_execution_command_flow.py`.

**Interfaces:**
- Consumes: `acquire_producer_view()`, runtime coordinator, Task8 initial owner token handoff.
- Produces: one-view-per-synchronous-segment sweep, view-token propagation through every first store effect and
  protection SELL prepare, zero product-path `owner.state` reads, and generation-tracked RAM helpers.

- [ ] **Step 1: Write no-owner-state, reacquisition, ambiguity and zero-writer REDs**

```python
def test_sweep_uses_one_view_and_zero_owner_state_reads(producer, owner_spy):
    async def scenario():
        await producer.sweep()
        assert owner_spy.acquire_view_calls == 1
        assert owner_spy.state_reads == 0
    asyncio.run(scenario())


def test_unavailable_view_performs_zero_writes(producer, writer_spies):
    async def scenario():
        producer.runtime.owner.fail_next_view("projection_unavailable")
        await producer.sweep()
        assert writer_spies.total_calls == 0
    asyncio.run(scenario())
```

Cover await-boundary reacquisition, retained-history independence, repeated symbol and reverse insertion parity,
multiple rejected intent ambiguity, UNKNOWN/unapplied/reservation blocks, invalid global facts and episode B update
before following await. Retain the four Task10 bridge tests as GREEN regressions:
`test_quote_receives_exact_supporting_view_token`,
`test_observe_market_receives_exact_supporting_view_token`,
`test_release_pending_receives_exact_supporting_view_token`,
`test_resume_receives_exact_supporting_view_token_and_command_id`,
and add new Task13 REDs
`test_writer_await_discards_old_view_before_next_owner_decision`,
`test_producer_sell_token_reaches_gateway_prepare_first_store_effect`, and
`test_gateway_never_rebases_explicit_stale_producer_token`.

- [ ] **Step 2: Run RED**

```bash
env -i PATH=/usr/bin:/bin LANG=C.UTF-8 TZ=UTC PYTHONDONTWRITEBYTECODE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
  /home/ubuntu/projects/qwq-ai-trader/venv/bin/python -m pytest \
  tests/test_protection_producer_recovery_index.py tests/test_execution_p1_producer.py \
  tests/test_execution_p1_producer_recovery.py tests/test_execution_p1_resume.py \
  tests/test_execution_p1_pending.py tests/test_execution_command_flow.py \
  -q -p no:cacheprovider --tb=short --show-capture=no
```

- [ ] **Step 3: Pass one explicit view through the synchronous sweep segment**

Change `_attempts`, `_new_decision_available`, `_audit_recovery`, `_episode_status`, and
`_restart_cooldowns` to accept `ProducerReadView`. Also remove all current direct reads in `_run`, `_market_input`,
`_sweep`, `_tick`, `_submit`, and `health`; `health` consumes the last exact immutable producer summary and marks
it incomplete when no current view exists. `pending_admissions()` is bounded by unresolved rows, while audits use
symbol/intent indexes only. After quote/resume/release/gateway await, discard the old view and reacquire before any
new owner decision. Use Task12's `_replace_episode`, `_replace_recovery_required`, `_replace_restart_state`, and
`_replace_invariant` helpers; every direct-writer replacement was already completed and tested there. Task13 only
routes its rewritten read/decision flow through the helpers and may not introduce a new direct assignment.

Pass `view.token` to `runtime.quote/observe_market/release_protection_pending/resume_protection_admission` and
through the `owner_token` keyword of `engine._submit_signal`, `gateway.submit`, and `commands.prepare`. The owner
validates the token at the first store effect. Gateway removes
the protection SELL `owner.state` policy pre-read; the typed prepare/candidate guard validates the policy inside
the owner. Task8 dispatch uses receipts/leases and cannot read or rebase from `owner.state`.

- [ ] **Step 4: Run GREEN and static/runtime zero-read spies**

```bash
env -i PATH=/usr/bin:/bin LANG=C.UTF-8 TZ=UTC PYTHONDONTWRITEBYTECODE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
  /home/ubuntu/projects/qwq-ai-trader/venv/bin/python -m pytest \
  tests/test_protection_producer_recovery_index.py tests/test_execution_p1_producer.py \
  tests/test_execution_p1_producer_recovery.py tests/test_execution_p1_resume.py \
  tests/test_execution_p1_pending.py tests/test_execution_command_flow.py \
  tests/test_execution_intraday_replay_completeness.py \
  -q -p no:cacheprovider --tb=short --show-capture=no
```

Static inventory must name every former direct read in `_run`, `_market_input`, `_new_decision_available`,
`_attempts`, `_episode_status`, `_restart_cooldowns`, `_sweep` (five sites), `_tick` (two), `_submit`, and `health`.
It also rejects product SELL reads in `gateway._policy_context` and command dispatch/guard.
Startup-only gateway reads, BUY-only quote lookup, and lifecycle query helpers remain named in the checked inventory
as intentional non-producer exclusions with their scopes; they are not silently ignored or counted as product SELL
compliance.

- [ ] **Step 5: Obtain independent review, stage the exact Task13 allowlist, and commit**

```bash
set -euo pipefail
git status --short
git diff --check -- src/execution/safety/protection_producer.py src/execution/safety/runtime.py \
  src/execution/safety/gateway.py src/execution/safety/commands.py src/core/engine.py \
  tests/test_protection_producer_recovery_index.py tests/test_execution_p1_producer.py \
  tests/test_execution_p1_producer_recovery.py tests/test_execution_p1_resume.py \
  tests/test_execution_p1_pending.py tests/test_execution_command_flow.py
git add -- src/execution/safety/protection_producer.py src/execution/safety/runtime.py \
  src/execution/safety/gateway.py src/execution/safety/commands.py src/core/engine.py \
  tests/test_protection_producer_recovery_index.py tests/test_execution_p1_producer.py \
  tests/test_execution_p1_producer_recovery.py tests/test_execution_p1_resume.py \
  tests/test_execution_p1_pending.py tests/test_execution_command_flow.py
git diff --cached --check
git diff --cached --name-only
git commit -m "refactor(safety): use indexed recovery producer views" -- \
  src/execution/safety/protection_producer.py src/execution/safety/runtime.py \
  src/execution/safety/gateway.py src/execution/safety/commands.py src/core/engine.py \
  tests/test_protection_producer_recovery_index.py tests/test_execution_p1_producer.py \
  tests/test_execution_p1_producer_recovery.py tests/test_execution_p1_resume.py \
  tests/test_execution_p1_pending.py tests/test_execution_command_flow.py
```

- [ ] **Step 6: Verify the Task13 commit and push separately**

```bash
set -euo pipefail
git show --stat --oneline --summary HEAD
git push -u origin HEAD
```

Review gate requires product producer/SEND path `owner.state` 0 and no unscoped historical audit query.

---

### Task 14: A-B-A-B Diagnostic Switch and N5-D Activation

**Files:**
- Modify: `src/execution/safety/recovery_capture.py`
- Modify: `src/execution/safety/recovery_diagnostics.py`
- Modify: `src/execution/safety/factory.py`
- Extend: `tests/test_recovery_projection_faults.py`, `tests/test_recovery_projection_acceptance.py`
- Extend: `tests/test_execution_mutation_plans.py`, `tests/test_protection_producer_recovery_index.py`
- Test: `tests/test_recovery_diagnostics.py`, `tests/test_recovery_diagnostics_acceptance.py`,
  `tests/test_recovery_diagnostics_boundaries.py`, `tests/test_execution_p1_install.py`,
  `tests/test_execution_install_factory.py`
- Create: `scripts/dev/run_recovery_projection_mutation_kills.py`,
  `tests/test_recovery_projection_mutation_kills.py`.
- Create after execution: `docs/reviews/data/recovery-projection-mutation-kills-2026-09-24.json`.

**Interfaces:**
- Consumes: owner A, runtime B, zero-legacy inventory and indexed producer.
- Produces: exact A-B-A-B snapshot, stricter public completeness, product activation after complete binding, and
  auditable §10.1 mutation-kill evidence from disposable committed-source trees.

- [ ] **Step 1: Write volatile/publication/restore/cap-divergence REDs**

```python
def test_episode_aba_returns_only_snapshot_volatile(capture_fixture):
    capture_fixture.change_episode_between_b_reads_then_restore_value()
    report = build_recovery_diagnostic(capture_fixture.capture())
    assert report["counts_complete"] is False
    assert [row["code"] for row in report["findings"]] == ["disposition_not_durable", "snapshot_volatile"]
```

Cover binding/runtime generation changes, PREPARING, SUBMITTING same versions, publisher failure, restore
runtime-incomplete, final evidence_invalid, immutable return, owner/history read 0, and 5,000 old-N3-cap versus new
complete full oracle.
Add `test_factory_activation_follows_complete_tracked_bindings_and_precedes_first_sweep` and
`test_activation_failure_starts_no_producer_sweep`.

- [ ] **Step 2: Run RED against N3 full-copy capture**

```bash
env -i PATH=/usr/bin:/bin LANG=C.UTF-8 TZ=UTC PYTHONDONTWRITEBYTECODE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
  /home/ubuntu/projects/qwq-ai-trader/venv/bin/python -m pytest \
  tests/test_recovery_projection_faults.py tests/test_recovery_projection_acceptance.py \
  tests/test_recovery_diagnostics.py tests/test_recovery_diagnostics_acceptance.py \
  tests/test_recovery_diagnostics_boundaries.py tests/test_execution_p1_install.py \
  tests/test_execution_install_factory.py \
  -q -p no:cacheprovider --tb=short --show-capture=no
```

- [ ] **Step 3: Replace full-copy capture with exact A-B-A-B composition**

Read A1/B1/A2/B2 once. Require pointer identities, all token fields, bound token, generations, binding fingerprint,
lock state/generation, publication and owner health. Any mismatch returns only volatile/unavailable control evidence.
`RecoverySnapshot` carries the already-decided `counts_complete`; builder no longer recomputes the weaker N3 rule.
Put each B-field comparison behind a small pure predicate that accepts the captured scalar/identity values. Unit tests
hold every other field equal and fault-inject exactly one producer epoch or binding fingerprint difference; this
makes each guard independently assertion-killable even though product updates normally replace the immutable B
pointer too. The pointer-identity and full ABA integration checks remain additional guards, not substitutes.

- [ ] **Step 4: Activate only the installed product path**

Direct test runtimes remain `SHADOW_MIGRATION` so synthetic `owner.mutate` setup stays possible. In
`install_attached_runtime`, after restore, gateway, producer binding and coordinator B are complete, call the owner
activation seam; product runtime checks current exact A/B, complete bindings and owner phase before the first indexed
sweep. It does **not** import tests or claim to inspect Python source at runtime. Zero product legacy routes are a
release/integration precondition enforced by Task7's source-reconciled AST inventory test, the Task11 zero-legacy
GREEN, and Task14 review evidence; failure of that gate forbids installing/committing the activation change. Active
mode has no legacy/full-scan fallback.

- [ ] **Step 5: Run N5-D static and behavior gates**

```bash
env -i PATH=/usr/bin:/bin LANG=C.UTF-8 TZ=UTC PYTHONDONTWRITEBYTECODE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
  /home/ubuntu/projects/qwq-ai-trader/venv/bin/python -m pytest \
  tests/test_recovery_projection_inventory.py tests/test_recovery_projection.py \
  tests/test_recovery_projection_faults.py tests/test_recovery_projection_acceptance.py \
  tests/test_protection_producer_recovery_index.py tests/test_recovery_diagnostics.py \
  tests/test_recovery_diagnostics_acceptance.py tests/test_recovery_diagnostics_boundaries.py \
  tests/test_execution_p1_install.py tests/test_execution_install_factory.py \
  -q -p no:cacheprovider --tb=short --show-capture=no
```

Assert: product legacy 0, normal full-builder 0, capture/producer owner.state 0, overflow store 0,
unavailable writer 0, stale full-scan fallback 0.

- [ ] **Step 6: Write the mutation-registry/runner RED and execute it before runner implementation**

`tests/test_recovery_projection_mutation_kills.py` first imports the absent runner, checks the exact registry and
rejects collection/setup failures as kills.

```bash
env -i PATH=/usr/bin:/bin LANG=C.UTF-8 TZ=UTC PYTHONDONTWRITEBYTECODE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
  /home/ubuntu/projects/qwq-ai-trader/venv/bin/python -m pytest \
  tests/test_recovery_projection_mutation_kills.py \
  -q -p no:cacheprovider --tb=short --show-capture=no
```

Expected: nonzero with `ModuleNotFoundError` for the not-yet-created runner helper; record command, rc and failure.

- [ ] **Step 7: Implement the disposable-tree runner and fixed mutation registry**

The runner archives committed candidate `HEAD` into a fresh `TemporaryDirectory` for baseline-before, every one-
mutant process, and baseline-after. Each replacement must match exactly once. A mutant is killed only when a
declared killing test assertion fails; import/collection/setup failure is invalid. Never edit the integration
worktree. Capture status/hash before and after all temp runs, compare them before atomically writing output, and
require equality.

| Mutation | Declared killing test |
|---|---|
| `omit_root_handler:inbox` | `test_incremental_inbox_matches_full_builder` |
| `omit_root_handler:attempts` | `test_incremental_attempt_matches_full_builder` |
| `omit_root_handler:intents` | `test_incremental_intent_matches_full_builder` |
| `omit_root_handler:outbox` | `test_incremental_audit_outbox_matches_full_builder` |
| `omit_root_handler:protection_quote_admissions` | `test_incremental_admission_matches_full_builder` |
| `omit_root_handler:protection.states` | `test_incremental_protected_position_matches_full_builder` |
| `omit_root_handler:protection.config` | `test_incremental_protection_config_matches_full_builder` |
| `omit_root_handler:protection.entry_times` | `test_incremental_protection_entry_times_matches_full_builder` |
| `omit_root_handler:protection.exit_exempt` | `test_incremental_protection_exit_exempt_matches_full_builder` |
| `omit_root_handler:protection.max_holding_days` | `test_incremental_protection_max_holding_matches_full_builder` |
| `omit_root_handler:protection.current_regime` | `test_incremental_protection_regime_matches_full_builder` |
| `omit_root_handler:protection.intraday_crash_level` | `test_incremental_protection_crash_level_matches_full_builder` |
| `omit_root_handler:protection.integrity_reset_symbols` | `test_incremental_protection_integrity_reset_matches_full_builder` |
| `omit_root_handler:protection.orders` | `test_incremental_protection_orders_matches_full_builder` |
| `omit_root_handler:protection.pending_owners` | `test_incremental_pending_owner_matches_full_builder` |
| `omit_root_handler:protection.degraded` | `test_incremental_degraded_matches_full_builder` |
| `omit_root_handler:portfolio.positions` | `test_incremental_portfolio_position_matches_full_builder` |
| `omit_root_handler:latest_explicit_quote` | `test_explicit_quote_add_update_delete_and_conflict_match_full_builder` |
| `omit_root_handler:quote_price_views` | `test_quote_price_view_add_update_delete_matches_full_builder` |
| `omit_root_handler:day_valuation_view` | `test_day_valuation_view_switch_and_stale_quote_match_full_builder` |
| `omit_root_handler:day_valuations` | `test_day_valuation_evidence_add_update_delete_matches_full_builder` |
| `target_revision_minus_one`, `target_revision_plus_one` | `test_off_by_one_projection_revision_is_never_current` |
| `increment_publication_epoch_before_prevalidation` | `test_prevalidation_rejection_preserves_pointer_token_epochs_revision_and_health` |
| `publisher_failure_keeps_old_current_frame` | `test_publisher_failure_leaves_no_old_or_candidate_current_frame` |
| `missing_projection_becomes_empty_counts` | `test_missing_projection_is_unavailable_not_empty` |
| `skip_producer_epoch_comparison` | `test_producer_epoch_guard_predicate_rejects_when_only_epoch_differs` |
| `skip_binding_fingerprint_comparison` | `test_binding_guard_predicate_rejects_when_only_fingerprint_differs` |
| `skip_ram_cross_count_delta` | `test_episode_add_delete_changes_cross_count_without_owner_revision` |
| `choose_latest_episode_by_dict_order` | `test_reverse_insertion_order_preserves_restart_episode_order_ambiguous` |
| `unknown_buy_to_zero` | `test_unknown_buy_count_is_not_zero` |
| `remaining_reservation_to_zero` | `test_remaining_reservation_count_is_not_zero` |
| `unknown_planned_risk_to_zero` | `test_unmeasured_planned_risk_is_invalid_not_zero` |

The exact initial root set is derived from Task1 `PRODUCER_INDEX_DEPENDENCIES` and currently equals `inbox`,
`attempts`, `intents`, `outbox`, `protection_quote_admissions`, `protection.states`,
`protection.config`, `protection.entry_times`, `protection.exit_exempt`, `protection.max_holding_days`,
`protection.current_regime`, `protection.intraday_crash_level`, `protection.integrity_reset_symbols`,
`protection.orders`, `protection.pending_owners`, `protection.degraded`, `portfolio.positions`,
`latest_explicit_quote`, `quote_price_views`, `day_valuation_view`, and `day_valuations`. Each mutable root test
exercises add/update/delete or scalar replacement as its domain permits. Quote/valuation roots additionally cover
stale market time, day-boundary rollover, and same source-event conflicting payload. `protection.schema` and
`protection.market` are checked immutable/no-writer exclusions, not omitted mutable handlers. `market_sources` and
`entry_quotes` are checked owner-validation-only exclusions and their relevant-change lease REDs must fail before
store. The source-use inventory test must fail if an actual query accessor reads an unclassified path. The registry
assertion requires
`set(PRODUCER_INDEX_DEPENDENCIES) == set(incremental_root_handlers) == set(omit_root_handler_mutants)`; a query
dependency or later handler cannot escape coverage.

- [ ] **Step 8: Run the runner contract GREEN, obtain preliminary review, and commit the candidate HEAD**

```bash
env -i PATH=/usr/bin:/bin LANG=C.UTF-8 TZ=UTC PYTHONDONTWRITEBYTECODE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
  /home/ubuntu/projects/qwq-ai-trader/venv/bin/python -m pytest \
  tests/test_recovery_projection_mutation_kills.py \
  -q -p no:cacheprovider --tb=short --show-capture=no
```

After preliminary critical review blocking 0:

```bash
set -euo pipefail
git status --short
git diff --check -- src/execution/safety/recovery_capture.py src/execution/safety/recovery_diagnostics.py \
  src/execution/safety/factory.py scripts/dev/run_recovery_projection_mutation_kills.py \
  tests/test_recovery_projection_mutation_kills.py tests/test_recovery_projection_faults.py \
  tests/test_recovery_projection_acceptance.py tests/test_recovery_diagnostics.py \
  tests/test_recovery_diagnostics_acceptance.py tests/test_recovery_diagnostics_boundaries.py \
  tests/test_execution_mutation_plans.py tests/test_protection_producer_recovery_index.py \
  tests/test_execution_p1_install.py tests/test_execution_install_factory.py
git add -- src/execution/safety/recovery_capture.py src/execution/safety/recovery_diagnostics.py \
  src/execution/safety/factory.py scripts/dev/run_recovery_projection_mutation_kills.py \
  tests/test_recovery_projection_mutation_kills.py tests/test_recovery_projection_faults.py \
  tests/test_recovery_projection_acceptance.py tests/test_recovery_diagnostics.py \
  tests/test_recovery_diagnostics_acceptance.py tests/test_recovery_diagnostics_boundaries.py \
  tests/test_execution_mutation_plans.py tests/test_protection_producer_recovery_index.py \
  tests/test_execution_p1_install.py tests/test_execution_install_factory.py
git diff --cached --check
git diff --cached --name-only
git commit -m "feat(safety): activate exact recovery read models" -- \
  src/execution/safety/recovery_capture.py src/execution/safety/recovery_diagnostics.py \
  src/execution/safety/factory.py scripts/dev/run_recovery_projection_mutation_kills.py \
  tests/test_recovery_projection_mutation_kills.py tests/test_recovery_projection_faults.py \
  tests/test_recovery_projection_acceptance.py tests/test_recovery_diagnostics.py \
  tests/test_recovery_diagnostics_acceptance.py tests/test_recovery_diagnostics_boundaries.py \
  tests/test_execution_mutation_plans.py tests/test_protection_producer_recovery_index.py \
  tests/test_execution_p1_install.py tests/test_execution_install_factory.py
```

- [ ] **Step 9: Execute every mutation from committed HEAD and verify raw evidence**

```bash
set -euo pipefail
env -i PATH=/usr/bin:/bin LANG=C.UTF-8 TZ=UTC PYTHONDONTWRITEBYTECODE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
  QWQ_DEPLOY_SSH_KEY=/tmp/qwq-offline-no-ssh-key \
  /home/ubuntu/projects/qwq-ai-trader/venv/bin/python \
  scripts/dev/run_recovery_projection_mutation_kills.py \
  --output docs/reviews/data/recovery-projection-mutation-kills-2026-09-24.json

env -i PATH=/usr/bin:/bin LANG=C.UTF-8 TZ=UTC PYTHONDONTWRITEBYTECODE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
  /home/ubuntu/projects/qwq-ai-trader/venv/bin/python -m pytest \
  tests/test_recovery_projection_mutation_kills.py \
  -q -p no:cacheprovider --tb=short --show-capture=no
```

Raw JSON records source SHA, before/after baseline rc, status/hash before/after, exact registry completeness, and
per mutant target/symbol/replacement hash/command/rc/failure class/declared and observed tests/killed/duration/
stdout+stderr hashes/temp cleanup. Both baselines must pass, every required mutant must be assertion-killed, no
survivor or collection/setup kill is allowed, and integration status/hash must match.

- [ ] **Step 10: Final whole-path review, commit evidence, and push only after blocking 0**

```bash
set -euo pipefail
git status --short
git diff --check -- docs/reviews/data/recovery-projection-mutation-kills-2026-09-24.json
git add -- docs/reviews/data/recovery-projection-mutation-kills-2026-09-24.json
git diff --cached --check
git diff --cached --name-only
git commit -m "test(safety): record recovery projection mutation kills" -- \
  docs/reviews/data/recovery-projection-mutation-kills-2026-09-24.json
git log -2 --oneline
git push -u origin HEAD
```

---

### Task 15: 63-Cell Performance Gate, Broad Verification, and Evidence

**Files:**
- Create: `tests/recovery_projection_scale_harness.py`
- Create: `tests/test_recovery_projection_performance.py`
- Create: `scripts/dev/run_recovery_projection_scale.py`
- Create after measurement: `docs/reviews/data/recovery-projection-scale-2026-09-24.json`
- Create after the separate memory run:
  `docs/reviews/data/recovery-projection-tracemalloc-2026-09-24.json`
- Create after measurement: `docs/reviews/recovery-projection-scale-2026-09-24.md`
- Modify after evidence: `CHANGELOG.md`, `docs/README.md`, `CLAUDE.md`,
  `docs/operations/p1-next-steps-2026-09-23.md`

**Interfaces:**
- Consumes: completed N5-D branch.
- Produces: always-on harness contract, exactly 63 serial logical cells, same-typed-path ABBA control, immutable
  raw evidence, every §11.2 budget assertion, and N5-E verdict.

- [ ] **Step 1: Write harness-schema, timing boundary, phase, censor and noisy-host REDs**

```python
def test_matrix_has_exactly_63_unique_cells():
    cells = matrix_cells()
    assert len(cells) == len(set(cells)) == 63


def test_timeout_is_censored_not_zero():
    row = classify_process(timeout_result(active_phase="restore_rebuild"))
    assert row["status"] == "censored"
    assert row["measurement"] is None
```

Add exact always-on tests:
`test_orchestrator_owns_exact_abba20_schedule`,
`test_control_and_indexed_use_same_typed_public_path`,
`test_only_scale_harness_can_replace_advance_recovery_models`,
`test_abba_pair_rejected_when_checkpoint_digest_differs`,
`test_abba_pair_rejected_when_version_differs`,
`test_abba_pair_rejected_when_publisher_payload_or_count_differs`,
`test_abba_pair_rejected_when_domain_result_differs`,
`test_section_11_2_budget_constants_are_exact`,
`test_each_section_11_2_overrun_is_ineligible`, and
`test_synthetic_evaluator_accepts_only_fixture_meeting_every_section_11_2_budget`.
Also pin phase order, production copy/encode/update inside timer, fixture/oracle only outside timer, exactly one retry
for baseline stall >25ms, second noisy result inconclusive, writer/full-scan spies and finite nonidentifying JSON.
Always-on pytest uses checked-in synthetic pass/fail rows only; it never requires a dated raw evidence file that has
not yet been measured. Actual evidence acceptance is the explicit post-measurement CLI gate in Step6.

- [ ] **Step 2: Run the harness RED before creating harness or orchestrator**

```bash
env -i PATH=/usr/bin:/bin LANG=C.UTF-8 TZ=UTC PYTHONDONTWRITEBYTECODE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
  /home/ubuntu/projects/qwq-ai-trader/venv/bin/python -m pytest \
  tests/test_recovery_projection_performance.py \
  -q -p no:cacheprovider --tb=short --show-capture=no
```

Expected: nonzero at import with `ModuleNotFoundError: recovery_projection_scale_harness`; record command, rc and
failure text. Do not create implementation files until this RED is captured.

- [ ] **Step 3: Implement fixed cohorts, one-cell harness, orchestrator, control seam and raw schemas**

Cells are `n4_rejected` sizes0/100/1000/5000 and `mixed_recovery` sizes100/1000/5000 times nine operations:
projection-update, commit-control, commit-indexed, restore, producer-cold, producer-warm, cache-read, json,
continuous-stall.

The first three use fixed subtrials:

1. real lifecycle ACK on one claimed SELL;
2. real two-commit `runtime.quote()` roundtrip;
3. one-unit partial BUY fill with inbox setup outside timer and fill commit inside.

`tests/recovery_projection_scale_harness.py` owns deterministic fixtures and executes exactly one **logical cell**
selected by `QWQ_RECOVERY_SCALE_CELL=<cohort>:<size>:<operation>`. The orchestrator constructs the fixed ABBA20
schedule and passes it as immutable JSON input; the cell process validates that exact schedule, executes all its
subtrials, and emits one cell result. `scripts/dev/run_recovery_projection_scale.py` owns the 63-cell serial schedule,
one process per primary cell, the one prescribed noisy-host replacement attempt, aggregation and atomic output.

Both modes call the same public typed API. Product/factory always calls Task2's
`advance_recovery_models(current_models, delta, target_token)`. Only a control trial monkeypatches that single symbol
to return `unavailable_owner_models(target_token, reason="benchmark_control_no_index")`; no legacy mutation path is
used. Production candidate copy, domain/policy validation, encoding, store and publisher remain identical and timed.
The inventory rejects any production caller that supplies/replaces this seam.

For each `projection-update`, `commit-control`, and `commit-indexed` logical cell, that single cell process runs all
three fixed workloads and, for each workload, 20 `control,indexed,indexed,control` schedules internally. Every slot
uses a fresh deterministic clone. There are no per-trial mode/pair/slot subprocesses or environment variables.
A pair is valid only with exact encoded checkpoint digest including version, publisher payload/count and domain
result. `commit-control` emits the control operation aggregate, `commit-indexed` the indexed operation aggregate,
and `projection-update` the indexed projection-update phase; each cell still runs both modes internally for parity.

Use these exact checked-in constants:

```python
SECTION_11_2_BUDGETS = {
    "projection_update_max_ms": 10.0,
    "commit_wall_max_ms": 1000.0,
    "quote_roundtrip_or_fill_apply_max_ms": 2500.0,
    "successful_commit_unavailable_max_ms": 1000.0,
    "abba_median_relative_increase_pct": 10.0,
    "abba_max_delta_ms": 25.0,
    "restore_5000_max_ms": 1000.0,
    "producer_cold_max_ms": 50.0,
    "producer_warm_max_ms": 10.0,
    "cache_read_max_ms": 5.0,
    "diagnostic_json_max_ms": 5.0,
    "continuous_stall_max_ms": 50.0,
    "public_json_max_bytes": 16 * 1024,
}
```

Apply every budget to every applicable cell/subtrial. Timeout, null measurement, parity-invalid pair, missing
metric or second noisy run is ineligible, never 0/PASS. An independent `loop.call_at` probe uses 1ms deadlines;
the 250ms pre-run baseline permits exactly one process retry when max stall exceeds 25ms.
The evaluator applies projection max to every scale trial; 1,000ms commit wall to lifecycle ACK and each single
commit; 2,500ms operation wall to quote roundtrip and fill apply; unavailable max to every successful commit;
ABBA median-relative and max-delta limits to every cohort/size/workload; restore max to 5,000-event cold restore;
and producer/cache/JSON/stall/byte limits to every applicable logical cell.

- [ ] **Step 4: Run always-on harness GREEN and inspect timing-boundary spies**

```bash
env -i PATH=/usr/bin:/bin LANG=C.UTF-8 TZ=UTC PYTHONDONTWRITEBYTECODE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
  /home/ubuntu/projects/qwq-ai-trader/venv/bin/python -m pytest \
  tests/test_recovery_projection_performance.py -q -p no:cacheprovider --tb=short --show-capture=no
```

- [ ] **Step 5: Obtain harness review, stage the exact code allowlist, and commit before measurement**

```bash
set -euo pipefail
git status --short
git diff --check -- tests/recovery_projection_scale_harness.py \
  tests/test_recovery_projection_performance.py scripts/dev/run_recovery_projection_scale.py
git add -- tests/recovery_projection_scale_harness.py \
  tests/test_recovery_projection_performance.py scripts/dev/run_recovery_projection_scale.py
git diff --cached --check
git diff --cached --name-only
git commit -m "test(safety): add recovery projection scale gate" -- \
  tests/recovery_projection_scale_harness.py tests/test_recovery_projection_performance.py \
  scripts/dev/run_recovery_projection_scale.py
```

- [ ] **Step 6: Run latency and tracemalloc as separate checked-in serial protocols, then evaluate evidence**

```bash
env -i PATH=/usr/bin:/bin LANG=C.UTF-8 TZ=UTC PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
  PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 QWQ_DEPLOY_SSH_KEY=/tmp/qwq-offline-no-ssh-key \
  /home/ubuntu/projects/qwq-ai-trader/venv/bin/python \
  scripts/dev/run_recovery_projection_scale.py \
  --mode latency \
  --output docs/reviews/data/recovery-projection-scale-2026-09-24.json

env -i PATH=/usr/bin:/bin LANG=C.UTF-8 TZ=UTC PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
  PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 QWQ_DEPLOY_SSH_KEY=/tmp/qwq-offline-no-ssh-key \
  /home/ubuntu/projects/qwq-ai-trader/venv/bin/python \
  scripts/dev/run_recovery_projection_scale.py \
  --mode tracemalloc \
  --output docs/reviews/data/recovery-projection-tracemalloc-2026-09-24.json

env -i PATH=/usr/bin:/bin LANG=C.UTF-8 TZ=UTC PYTHONDONTWRITEBYTECODE=1 \
  PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 /home/ubuntu/projects/qwq-ai-trader/venv/bin/python \
  scripts/dev/run_recovery_projection_scale.py verify \
  --latency docs/reviews/data/recovery-projection-scale-2026-09-24.json \
  --tracemalloc docs/reviews/data/recovery-projection-tracemalloc-2026-09-24.json
```

Latency and tracemalloc are different invocations and different raw schemas; tracemalloc never wraps or contaminates
normal latency timers. Each invocation has the same 63 logical cell coordinates. The latency orchestrator launches
exactly one primary `env -i` pytest process per logical cell and gives the **whole cell**, including all internal
ABBA subtrials, `timeout --signal=TERM --kill-after=5s 30s`. The only extra process permitted is the spec-prescribed
single replacement for a cell whose 250ms pre-run baseline stall exceeds 25ms; both attempts remain in evidence and
the second noisy result is inconclusive. No shell glob or manual cell substitution exists. Persist
`active_phase`, monotonic phase start, last completed phase, exit code, measurement/null, cleanup status and raw
subtrials. Required phases are setup, warmup, control, projection_update, store_encode, store_commit_wait, publish,
restore_load, restore_rebuild, producer-cold/producer-warm, cache-read, json, validation and cleanup. Never change a
preregistered budget after observing results.

The latency schema contains exactly 63 final cell records plus any linked discarded noisy attempt, ABBA slot rows,
parity digests and every §11.2 metric. The tracemalloc schema separately contains 63 cell records, peak/current bytes,
allocation phase and no latency verdict. Tracemalloc also launches exactly one primary `env -i` pytest process per
cell with the same external 30-second whole-cell TERM/5-second-KILL boundary, flushed phase markers, cleanup status,
and censored/null result on timeout. No noisy-host latency retry is applied to tracing; a missing/censored tracing
cell remains explicit and finite and makes evidence incomplete rather than launching an unbounded replacement.
`verify` always parses both files, but N5-E PASS is decided only by the preregistered latency/correctness budgets and
complete finite tracing evidence; it exits nonzero for censored, inconclusive, parity-invalid, missing or over-budget
latency evidence and for missing/censored tracing evidence. A nonzero result does not delete either raw file and must
be reported as N5-E failed.

- [ ] **Step 7: Run complete focused gate and UTC→KST broad suite after all workers stop**

First require `pgrep -af '[p]ytest'` to return no unrelated test process. Then:

```bash
set -euo pipefail
env -i PATH=/usr/bin:/bin LANG=C.UTF-8 TZ=UTC PYTHONDONTWRITEBYTECODE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
  QWQ_DEPLOY_SSH_KEY=/tmp/qwq-offline-no-ssh-key QWQ_VERIFY_ROOT="$PWD" \
  QWQ_VERIFY_PYTHON=/home/ubuntu/projects/qwq-ai-trader/venv/bin/python \
  bash scripts/dev/verify.sh

env -i PATH=/usr/bin:/bin LANG=C.UTF-8 TZ=Asia/Seoul PYTHONDONTWRITEBYTECODE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
  QWQ_DEPLOY_SSH_KEY=/tmp/qwq-offline-no-ssh-key QWQ_VERIFY_ROOT="$PWD" \
  QWQ_VERIFY_PYTHON=/home/ubuntu/projects/qwq-ai-trader/venv/bin/python \
  bash scripts/dev/verify.sh
```

- [ ] **Step 8: Write evidence without rewriting N4 history**

Raw JSON top level includes schema/spec/base/candidate SHA, times, timezone, offline execution, sanitized host,
concurrency 0, timeout 30, ABBA 20, gates, retry policy, verdict, exactly 63 cells and correctness fixtures. Each cell
contains expected/actual/seed counts, phase events, status, measurement/null, process time, isolation, row/edge and
store/publisher/builder counts. Link the prior N4 12-complete/4-timeout result and the Task14 mutation JSON; do not
overwrite either history. A failed/censored/inconclusive run remains evidence and must report N5-E failed.
The separate tracemalloc JSON is linked by candidate SHA and cell IDs and is never substituted for latency evidence.

- [ ] **Step 9: Independent raw-data and critical final review**

Sol/high recomputes cell count, ABBA aggregates, budgets and censor decisions from raw JSON. A non-author
Astra/xhigh or verified Opus/xhigh reviews source, diff, mutation kills, UTC/KST results and raw evidence. Any
blocking finding returns to the owning task; no threshold relaxation is allowed.

- [ ] **Step 10: Update status docs and commit only the proven verdict**

```bash
set -euo pipefail
git status --short
git diff --check -- docs/reviews/data/recovery-projection-scale-2026-09-24.json \
  docs/reviews/data/recovery-projection-tracemalloc-2026-09-24.json \
  docs/reviews/recovery-projection-scale-2026-09-24.md CHANGELOG.md docs/README.md CLAUDE.md \
  docs/operations/p1-next-steps-2026-09-23.md
git add -- docs/reviews/data/recovery-projection-scale-2026-09-24.json \
  docs/reviews/data/recovery-projection-tracemalloc-2026-09-24.json \
  docs/reviews/recovery-projection-scale-2026-09-24.md CHANGELOG.md docs/README.md CLAUDE.md \
  docs/operations/p1-next-steps-2026-09-23.md
git diff --cached --check
git diff --cached --name-only
git commit -m "test(safety): record recovery projection acceptance" -- \
  docs/reviews/data/recovery-projection-scale-2026-09-24.json \
  docs/reviews/data/recovery-projection-tracemalloc-2026-09-24.json \
  docs/reviews/recovery-projection-scale-2026-09-24.md CHANGELOG.md docs/README.md CLAUDE.md \
  docs/operations/p1-next-steps-2026-09-23.md
```

- [ ] **Step 11: Verify implementation/evidence commits and push separately**

```bash
set -euo pipefail
git log -2 --oneline
git push -u origin HEAD
```

The report must state whether N5 passed. Even on PASS, health/API/alert wiring is only N6 eligibility; it is not
deployment, restart, order, setting change, KIS finality proof or full-engine promotion.

---

## Final Plan Self-Review Checklist

- [ ] Every approved spec section maps to at least one Task1–15 deliverable.
- [ ] No task introduces a second state owner, background rebuild, unscoped historical query or active stale fallback.
- [ ] Type and method names are identical across producer, composition, owner, authority and capture tasks.
- [ ] Review Focus items each have a named RED in the owning task.
- [ ] The inventory starts at 25 product mutate calls and six internal commit routes and reaches product legacy 0.
- [ ] Baseline inventory rows stay immutable while current routes are derived from AST successors.
- [ ] Fill and risk/regime nested callbacks emit immutable fragments; no whole-candidate writer is unowned.
- [ ] WriterLease and ResultDrainToken are reviewed together; result drain is issued before transport.
- [ ] Every producer-originated first store effect consumes the exact supporting view token, and self-commits advance
  only through owner-issued receipts.
- [ ] Every owner/runtime/producer/handler/reconciler/lock B-frame input has one tracked generation seam and an ABA RED.
- [ ] Repository async tests use synchronous `asyncio.run()` wrappers; RED and git mutation never share a shell block.
- [ ] The fixed §10.1 mutation registry is fully killed from disposable committed trees with unchanged integration state.
- [ ] Timer exclusions apply only to test fixture/oracle work; all product copies/builds/encodes are timed.
- [ ] All §11.2 budgets, 63 logical cells and ABBA20 parity/ineligibility rules are executable assertions.
- [ ] No implementation task author performs its own final critical approval.
