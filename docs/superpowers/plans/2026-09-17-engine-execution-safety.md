# KR Execution Safety Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans. Steps use checkboxes for tracking.

**Goal:** KR의 취소 불명 재주문·부분체결 이중 반영을 막고 현행 위험 조건을 최종 송신 경계에서 재검사한다.

**Architecture:** 전략은 유지하고 실행 상태를 SQLite checkpoint와 직렬 coordinator가 소유한다. KIS 관측은 누적 증거로 전달하고 경제 상태·보호 판정·dedup을 원자적으로 저장한 뒤 게시한다. 신규 모듈을 독립 검증한 후 기존 core/scheduler 연결은 한 명이 순서대로 수행하며 일부 연결본을 운영에 배포하지 않는다.

**Tech Stack:** Python asyncio/dataclasses/Decimal/sqlite3, existing pytest, real ExitManager and FeeCalculator, injected external I/O and clocks.

**Spec:** `docs/superpowers/specs/2026-09-17-engine-execution-safety-design.md` (사용자 2026-09-17 상세 설계 승인).

## 현재 진척 (완료 범위와 미완 분리)

Task1/2/3의 독립 모듈·인터페이스와 ExitManager 메모리 전용 준비를 구현했고, Important6/Minor1을 수정·재리뷰했다. Task4a 공식 legacy 조회 수집도48시험·독립 리뷰를 마쳤다. 기반 커밋의 KST/UTC 전체 verify는 각각2076 passed/기존xfail2·격리0이다. **운영 돈 경로(Task4/5)는 아직 교체하지 않았으며 전체 명세 인수 미완**이다. 아래 체크리스트는 모듈 시험만으로 실제 큐·경제/보호·복구 요구까지 통과했다고 체크하지 않는다. 중간 검증 정본은 `docs/reviews/engine-execution-safety-2026-09-17.md`다.

09-18 후속 Task4b에서 실제 경제/보호 DTO와 core 누적체결 큐/receipt를 구현했다. 경제56·보호44·통합16시험과 기존 보호43시험의 집중 검증159건을 통과했다. 독립 리뷰 Important7은 수정·한정 재리뷰를 마쳤고, 소스 동결 후 전체 KST/UTC 각각2192 passed/기존xfail2·격리0이다. 위2076은 이전 기반 커밋의 근거이며 이번 변경의 결과가 아니다. 운영 설치·모든 writer 이행/보호repair/일자전환/initialR/최초 인계/원장 배출은 남아 있다. 현재 결과는 리뷰 보고서의 Task4b 절에 별도로 기록한다.

## Global Constraints

- **KIS만 주문·취소/정정·체결·계좌·잔고를 담당한다. Toss는 별도 관측 서비스 그대로다.**
- 종목/전략 배분, 위험 사이징 한도, 손절/익절 수치, 매도 면제, 주문 종류·폴백 횟수를 바꾸지 않는다.
- 직접 주문·수동 매수 지시·킬스위치·기존 상태 파일 삭제·운영 서비스 조작은 하지 않는다.
- KR만 이행하며 US 공용 호환은 검증하지만 US 안전성 완료를 주장하지 않는다.
- 보호 degraded는 경제 체결을 숨기지 않는다. SQLite 오류·게시 불일치·시작 대사 미완료는 모든 새 KR 거래 POST를 막는다.
- UNKNOWN·취소 ACK·시간 경과·로컬 주문 부재로 예약을 해제하지 않는다. 취소 NOT_SENT/REJECTED는 원 주문을 해제하지 않는다.
- 테스트는 운영 자격/네트워크/상태 파일 없이 tmp_path와 외부 경계 fake를 사용한다. 테스트 conftest 격리 위반0이 필수다.
- feature 브랜치에서 관련 파일만 명시 stage, 검증 후 commit+push. main merge·운영 배포·재시작은 이번 승인 밖이다.

## 실행·소유권

| 작업 | 파일 소유 | 모델/effort | 선행 |
|---|---|---|---|
| 1 저장/체결 | `execution/safety/store.py`, `application.py`, 전용시험 | Astra/high | 없음 |
| 2 주문/증거 | `execution/safety/lifecycle.py`, `evidence.py`, 전용시험 | Astra/high | 인터페이스 고정 |
| 3 최종 검사 | `execution/safety/guards.py`, 전용시험 | 부모 | 없음 |
| 4 통합 | `runtime.py`, `kis_kr.py`, `engine.py`, `kr_scheduler.py`, `run_trader.py`, `batch_analyzer.py`, 공용 types/ExitManager | 부모 단독 | 1/2/3 리뷰 |
| 5 복구/고장 시험 | 실제 통합시험·작업별 수정 | 담당 분리 | 4 |
| 6 See | 문서·전범위 리뷰·검증·커밋/푸시 | 독립 Astra/xhigh 요청, 불가 시 기존 Astra/high 교차 | 5 |

사용자가 요청한 병렬 작업은 파일 소유권이 겹치지 않는 1/2/3에 한정한다. 공용 타입은 새 모듈별 dataclass/명시 JSON 도메인을 사용하고 별도 공유 타입 편집 경합을 만들지 않는다. 하위 구현자는 git index/commit을 변경하지 않으며 부모가 검증한 묶음만 커밋한다. 사용 가능한 슬롯이 부족하면 기존 두 Astra/high를 재사용한 사실을 기록한다. 문서상의 모델 배치와 실제 실행을 구분한다.

## 공통 저장 인터페이스

`store.py`가 제공할 공개 경계:

```python
class ExecutionStateStore:
    def __init__(self, path: Path): ...
    async def load(self) -> tuple[int, dict]: ...
    async def commit(self, expected_version: int, state: dict,
                     commit_id: str) -> int: ...
    async def lookup_commit(self, commit_id: str) -> int | None: ...
    async def close(self) -> None: ...
```

checkpoint JSON은 `portfolio`, `protection`, `risk`, `intents`, `attempts`, `inbox`, `cursors`, `lots`, `outbox`, `startup_reconciliation`을 포함한다. 금액은 decimal 문자열, 시각은 aware ISO8601, enum은 value로 저장한다. store는 불투명 상태 원자성/버전/권한을 담당하고 도메인 검증은 coordinator가 담당한다. 새로운 store의 load는 version0과 빈 상태이며 호출자가 이를 정상 계좌로 해석하면 안 된다. async 단일 writer를 사용하고 commit ID 재시도는 같은 payload/expected-version에만 동일 결과를 반환한다. 충돌은 성공으로 위장하지 않는다.

### Task 1: Durable store and single fill application

**Files:** Create `src/execution/safety/__init__.py`, `store.py`, `application.py`; tests `tests/test_execution_state_store.py`, `tests/test_execution_fill_application.py`.

**Interfaces:** 위 store 계약을 제공한다. `FillApplicationCoordinator`는 누적관측 + 명시적 후보 상태 생성/reducer + publish 함수를 받는다. `apply`/`restore`/`mutate`는 동일 직렬 명령 잠금을 공유한다. candidate 전체에는 Task2의 intent/attempt가 포함되므로 writer를 둘로 만들지 않는다. `mutate(command_id, reducer)`가 동기 reducer에 deep copy checkpoint를 주고 결과를 원자 commit/publish한다. `state`, `version`, `published_version`, `healthy`, `publication_recovery_required`는 최종 guard가 동기 읽기 가능하다.

- [ ] RED: tmp DB에 v1 commit 후 reopen; 다른 expected_version/같은 ID 다른 payload 거부, 손상/미지원 schema 거부, DB/WAL/SHM 권한, commit 실패 원 상태 유지 시험 작성.

```python
async def store_roundtrip(tmp_path):
    store = ExecutionStateStore(tmp_path / 'state' / 'execution.sqlite3')
    assert await store.commit(0, {'cash': '100'}, 'baseline') == 1
    assert await store.commit(0, {'cash': '100'}, 'baseline') == 1
    await store.close()
    reopened = ExecutionStateStore(tmp_path / 'state' / 'execution.sqlite3')
    assert await reopened.load() == (1, {'cash': '100'})
    await reopened.close()
```

- [ ] Run `env -i PATH=/usr/bin:/bin LANG=C.UTF-8 TZ=Asia/Seoul PYTHONDONTWRITEBYTECODE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 /home/ubuntu/projects/qwq-ai-trader/venv/bin/python -m pytest tests/test_execution_state_store.py tests/test_execution_fill_application.py -q -p no:cacheprovider`; observe missing-module/contract RED before implementation.
- [ ] Implement store with SQLite WAL/FULL, explicit schema, monotonic version + commit-ID table in one transaction; one executor thread; permissions0700/0600; no implicit production path, no auto corruption recovery. Cancellation during writer commit must resolve outcome/reload, not assume no commit.
- [ ] Implement durable inbox and cumulative observation (scope-complete order identity, cumulative quantity/amount, immutable metadata). Applied cursor advances only in candidate commit. Equal qty/amount duplicate, lower observation old, equal qty/different amount reconciliation. Increment amount derives from cumulative difference, not repeated average. Reject impossible/nonfinite/negative/identity conflicts.
- [ ] Implement receipt APPLIED/ALREADY_APPLIED/NEEDS_RECONCILIATION/FAILED plus protection_status/version/journal_pending. commit→publish→receipt. Publish exception/cancellation sets barrier; restore checkpoint clears only after successful publication; no duplicate economics. Store failure preserves inbox/reservations.
- [ ] Tests BUY40 then60, duplicate40, stale40, restart100; SELL40 then60; failed protection produces APPLIED+degraded without double economics; precommit vs postcommit failure; asyncio.Event writer wait serializes quote/protection mutation; restore with quote view not overwritten. Domain reducer injection is tested with real economic state, not mocked success.
- [ ] GREEN focused run; self-review and report RED/GREEN commands/output. Parent task review before integration; do not commit shared index.

### Task 2: Order lifecycle and evidence

**Files:** Create `src/execution/safety/lifecycle.py`, `evidence.py`; tests `tests/test_execution_lifecycle.py`, `tests/test_kis_order_evidence.py`.

**Interfaces:** synchronous pure checkpoint transitions consumed through Task1 `mutate`. `OrderLifecycleCoordinator` owns `prepare`, `claim`, `record_result`, `reconcile`, `replacement_quantity`; accepts serialized mutation owner (not separate state). `CommandResult`, `CommandStatus`, `OrderRef`, `OrderEvidence` and intent/attempt status are defined here. No imports of old scheduler or remote API. Guards read durable attempt/claim snapshot.

- [ ] RED: target10/holding100/cancel failure must preserve reservation10 and replacement0; cancel ACK incomplete response likewise. Confirmed cumulative5+cancel5 and applied5 gives replacement5; filled10 gives0. Concurrent claims send once. Cancel NOT_SENT preserves original. Different account/day/exchange/chain ODNO never matches.

```python
def remaining_target(initial_qty, unique_applied_quantities):
    return max(0, initial_qty - sum(unique_applied_quantities))

assert remaining_target(10, [5]) == 5
assert remaining_target(10, [10]) == 0
```

- [ ] Run dedicated tests with the same isolated pytest command (replace paths); observe RED.
- [ ] Implement full nonterminal enum set and intent/attempt reservations. Persist prepare and claim before sender; only one claim is valid. NOT_SENT NEW releases only attempt-owned reservation after durable confirmation/no other active attempt; CANCEL/modify never frees original. No bool UNKNOWN coercion. Received late result verifies attempt identity/version and cannot release another attempt.
- [ ] Pin publicly available official KIS contract revision/field names and describe accepted simple order rows; parsing is strict Decimal/int/date/side/scope. Response missing status fields or incomplete pages is unknown. Finality requires explicit qty/cancel/reject/expiry evidence, not absence. Unsupported corrected chains/market/session remain unsupported_finality; no invented successful mapping.
- [ ] Deduplicate original/cancel child chain, reject conflicts. Pagination tests: next-page failure, loop, cap, missing headers, truncated row; complete fills-only response cannot imply unfilled order absent. Existing KIS limiter/poll cadence retained at later transport wiring.
- [ ] GREEN focused tests, self-review; report stable API to parent and Task1 owner. Parent/sibling reviews the diff; no shared-index commit.

### Task 3: Current-risk and final dispatch guards

**Files:** Create `src/execution/safety/guards.py`; test `tests/test_execution_guards.py`.

**Interfaces:** immutable `RiskSnapshot(version, attempt_sequence, observation_status, as_of, level, recovery_until)` and trusted `EntryContext` produced by route factories, not signal metadata. `FinalEntryGuard.evaluate(context)` reads a callable current snapshot and injected aware clock. `FinalDispatchGuard.evaluate(attempt_id, claim_id)` reads current Task1/Task2 publication/state and composes the entry check for automatic BUY only. Return structured allowed/reason, no I/O.

- [ ] RED: normal→failed overrides previous success; severe; missing/previous-day/future/naive timestamp; invalid level; same-sequence conflict conservative; cooldown; SEPA14:29:59→14:30; current store failure blocks SELL/cancel; BUY unclassified/manual metadata spoof blocked; explicit safe_asset/manual context only exempts alpha guard.

```python
from datetime import datetime
from zoneinfo import ZoneInfo

kst = ZoneInfo('Asia/Seoul')
before = datetime(2026, 9, 17, 14, 29, 59, tzinfo=kst)
at_cutoff = datetime(2026, 9, 17, 14, 30, tzinfo=kst)
assert before.time() < at_cutoff.time()
```

- [ ] Run isolated `tests/test_execution_guards.py`, confirm intended RED.
- [ ] Implement KST conversion with no host-time assumption and no new freshness TTL/threshold. Latest observation attempt failed/missing means unknown; absence of snapshot isn't normal. Current-day and <=now checks required; preserve existing5min recovery/14:30 SEPA cutoff (do not create different policy numbers).
- [ ] Implement common dispatch barrier: store healthy, startup complete, published version matches, attempt sender ownership and still admissible. Return NOT_SENT only at pre-HTTP boundary; sent attempts remain UNKNOWN pending evidence. Entry guard does not block SELL due to severe alone.
- [ ] GREEN tests; sibling reviews guard and exclusion mapping before integration.

### Task 4: Integrate all KR writers and HTTP boundary

#### Task4b — 실제 경제/보호 후보와 core 큐 receipt (2026-09-18 착수)

Task4a 다음 독립 검증 단위. `economics.py`(Astra/high 저장 담당), `protection.py`(Astra/high 생명주기 담당), `runtime.py` 및 core event/engine 연결(부모)을 파일별 병렬 소유한다. 구현 종료 후 서로 다른 담당자가 리뷰한다. 슬롯 제한 때문에 기존 두 에이전트를 재사용한다. 기존 scheduler/broker/run_trader는 이 조각에서 활성화하지 않으며, Task4 전체·운영 안전성 완료로 세지 않는다.

- 명시 Portfolio/Risk/ExitManager DTO, Decimal 비용, 같은 주문의 부분체결/별도 추가매수 구분, intent별 손실 청산·V재진입 소모, 보호 실패 시 degraded를 실제 컴포넌트로 시험한다.
- 비용은 기존 FeeCalculator 요율/원 단위 반올림을 사용한 **주문 누적 추정 비용의 차분**이다. 실제 징수 비용으로 표시하지 않는다. 분할 관측마다 반올림하던 legacy와 원 단위 차이가 날 수 있으며 비용 귀속만 명시적으로 바꾼다. 잔여 매수비용을 보존하여 분할 매도에 배분한다.
- 별도 추가매수는 주문 시작 직전 수량을 기준으로 누적 증가율10%에 도달할 때 기존 리셋을 한 번만 적용한다. 동일 최초 진입의 추가 체결은 리셋하지 않는다. pending 청산 stage는 같은 intent 체결만 진행시킨다.
- 위험 일자 불일치·전일 주문 지연 체결·미측정 기존 원가/비용·초기 baseline 인계는 자동 추정하지 않는다. Task5 대사/rollover 연결 전 fail-closed로 남긴다. 기존 미호출 KR 승패 통계를 새로 활성화하지 않는다.
- 실제 UnifiedEngine 큐에 별도 누적체결 이벤트를 넣고 APPLIED receipt까지 기다린다. 큐 적재/일반 핸들러 예외 삼킴은 성공으로 처리하지 않는다. 연결 후 legacy 증분 FILL/update_position 경로는 거부한다. 이 opt-in 연결은 운영 설정 토글이 아니며 run_trader에서는 아직 호출하지 않는다.
- quote 현재가는 별도 view로 보존하고 고점/BE/pending 변경은 같은 coordinator 명령으로 직렬화한다. 종료/대기 취소/DB 대기/게시 실패·복구를 외부 경계만 fake한 통합시험으로 검증한다.
- 초기 R은 명시 종결+누적 적용 및 측정 자료가 없으면 pending이며, outbox는 미전송 상태로 둔다. 원격 원장 exactly-once나 자동 startup 해제를 주장하지 않는다.

공식 legacy 추가 조사 후 Task4a(독립 선행 조각)를 분리했다: `execution/safety/queries.py`와 `test_kis_execution_queries.py`에서 현행 TTTC8001R/8036R의 읽기 전용 요청·페이지 수집을 오프라인 구현한다. 외부 fetch/clock 주입, 전체 범위·F/M→N·상한/실패·provenance를 검사하며 최종성/시작 장벽 해제 권한은 없다. 신규 두 파일은 저장 담당 Astra/high가 맡고 기존 live 파일은 여전히 부모 소유다. Task4a 완료가 Task4 전체 완료는 아니다.

**Files:** Create `src/execution/safety/runtime.py`; modify `src/execution/broker/kis_kr.py`, `src/core/engine.py`, `src/core/event.py`, `src/schedulers/kr_scheduler.py`, `src/core/batch_analyzer.py`, `src/strategies/exit_manager.py`, `scripts/run_trader.py`; focused `tests/test_execution_runtime.py`, `tests/test_kr_final_dispatch.py` plus existing characterization tests.

**Interfaces:** runtime owns Task1+2+3 and exposes submit/cancel/reconcile/apply-observation/quote/protection-repair/sync-start+apply/startup-reconcile/health. Broker is external transport+query; engine consumes real fill observations via queue and waits for receipt rather than interpreting emit as completion. No new runtime switch that silently falls back to unsafe legacy mode after store failure.

- [ ] RED real queue BUY40+60 must yield portfolio100/exit100/cash-fee exact/order-count1; ordinary SELL40+60 must decrement both once. Reproduce old cancel-failure90sec duplicate and quote-wait normal→severe HTTP boundary using fake I/O, not mocked coordinator.
- [ ] Economic reducer copies existing Decimal/FeeCalculator math, refuses sell beyond known quantity (reconcile, not silently clamp cash), serializes actual position/exit/risk fields. No synchronous external journal or state persistence inside candidate. Use existing real ExitManager via explicit snapshot/restore and no-persist candidate methods. Same-entry fill updates quantity/avg while preserving stage/BE/high/pending; distinct add-on policy once; exempt stays exempt. All fill-linked risk counters including V-token and loss exits are keyed by order/intent, not arrival count. initialR only final+applied, ambiguous external holdings never fabricated.
- [ ] Broker durable request wrapper saves attempts before connection/hashkey/limiter and runs final synchronous callback after the last application await just before `session.post`. All cash/revise/cancel trade POSTs use no retry including401/token-invalid; GET/hashkey behavior retained. Structured ACK/REJECTED/UNKNOWN/NOT_SENT reaches caller intact; no TEMP ID as exchange proof.
- [ ] Existing full evidence poll uses bounded pagination with `tr_cont:N`, full order scope and existing limiter. Durable inbox receives observations before mappings can disappear. Cancellation ACK preserves broker refs. Scheduler fill branch awaits core receipt; remove its duplicate exit decrement/reregistration and success-on-emit. Outbox contains original side-effect snapshots and idempotency keys; external journal completion separately tracked.
- [ ] Route every KR submit including fallback, KOFR and manual through runtime/trusted context. Keep risk/exempt/kill policy checks and repeat appropriate kill check at final dispatch. No exception via arbitrary strategy string. Old pending clear/timeout/verifier paths ask coordinator terminality; no orphan absence unlock. Scheduler+engine fallback share sender claim.
- [ ] Quote view updates independently, protection mutations serialized via coordinator; no SQLite await under trading/pending locks. Sync captures version/inflight set outside network, never installs cash/position baseline while pending/unapplied or cutoff unknown; ensure existing protection too. Refuse raw `BatchAnalyzer` portfolio hydration overwriting an active coordinator.
- [ ] BatchAnalyzer publishes latest attempt success/failure atomically before awaits; recovery status and current level visible together. Final guard reads this current view, not entry metadata. Strategy early gates remain skip optimizations.
- [ ] GREEN integration + existing entry_risk/sync/ExitManager/US compatibility tests; enumerate all actual mutation/submit call sites in report. A dormant module alone is not accepted as completed wiring.

### Task 5: First migration, crash recovery, outbox, health

**Files:** runtime/store amendments under sole owner; tests `tests/test_execution_recovery.py`, `tests/test_execution_runtime.py`; `src/api` actual health provider identified with `rg '/api/health' src` before editing; entry-risk/export consumers for journal_pending.

- [ ] RED: missing DB + legacy nonempty state starts blocked; corrupt/schema mismatch blocked without deletion; query stalled and simultaneous SELL sends0; unknown external order is quarantined, not auto-cancelled. No invented initialR from current settings.
- [ ] Implement startup before all KR trade tasks: read-only complete evidence/positions comparison, unresolved remains global barrier. Quiescent snapshot cutoff must have explicit full-query proof; matching local versions alone is insufficient. Existing-state read is runtime-only, not developer command. Persist reconciled checkpoint then publish and unlock. Restore existing checkpoint, durable inbox/outbox, recover SUBMITTING as UNKNOWN without retransmission.
- [ ] Inject crashes precommit/postcommit/prepublish/preACK and live publication failure; journal failure after core commit must retain protection and retry idempotently. External sinks require real unique execution key or safe readback; absent support keeps journal_pending (canary excluded), not false exactly-once. One-use V re-entry and loss counters survive replay.
- [ ] Add health unknown/age/reconcile/inbox/applied/store/seq/degraded/outbox/guard/incomplete counters, sanitized identifiers. Existing notifier on new meaningful faults deduplicated, alert failure never unlocks. runbook evidence requirements, manual resolution and conservative unsupported scope documented; no operator action executed.
- [ ] GREEN all spec C1–C8/F1–F9/G1–G5/R1–R2 mapped to concrete test names; report any unmet as incomplete, not implicit pass.

### Task 6: Independent review, whole verification and documentation

**Files:** `CHANGELOG.md`, `CLAUDE.md`, `docs/README.md`, `docs/architecture/system-overview.md`, `docs/risk/risk-and-exit.md`, `docs/integrations/external-apis.md`, `docs/operations/runbook.md`, `docs/operations/monitoring-checkpoints.md`, `docs/reviews/engine-execution-safety-2026-09-17.md`.

- [ ] Review per-task diff + spec/test evidence with implementer/reviewer separated. Capture blocking fixes and scoped rereview; do not claim unavailable xhigh executed. Final broad review sees all files plus parked concerns.
- [ ] Update unconditional clear_pending rules to definite evidence only; sync-timeout release does not override execution barrier; receipt vs protection/journal status, rollback compatibility, unsupported KIS scope and US followup explicit. Status distinguishes branch source, main, running service.
- [ ] Run full verification sequential UTC and Asia/Seoul:

```bash
execution_verify_cache=$(mktemp -d)
env -i PATH=/usr/bin:/bin LANG=C.UTF-8 TZ=Asia/Seoul \
 PYTHONPYCACHEPREFIX="$execution_verify_cache" PYTHONDONTWRITEBYTECODE=1 \
 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PYTEST_ADDOPTS='-p no:cacheprovider --tb=short -rx' \
 QWQ_DEPLOY_SSH_KEY=/tmp/qwq-offline-no-ssh-key \
 QWQ_VERIFY_PYTHON=/home/ubuntu/projects/qwq-ai-trader/venv/bin/python \
 bash scripts/dev/verify.sh
```

- [ ] Repeat with TZ=UTC; require syntax/secret checks and isolation0. Existing2 parity xfails are not repaired by this work. Compare config/Toss files to base unchanged.
- [ ] Explicit path staging, commit+push feature branch, confirm remote SHA and clean worktree. Do not merge/deploy/restart. Final report gives actual completed acceptance IDs, remaining constraints and review/test evidence, no profitability claim.
