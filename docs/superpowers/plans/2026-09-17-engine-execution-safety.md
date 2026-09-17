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

## 09-18 사용자 후속 5단계 — 실행 단위

기준 `dd3b0f7`. 단계1→2→3의 구현/리뷰는 순서대로 닫으며, 단계4의 공개 계약 조사와 단계3의 읽기 전용 호출점 조사는 병렬로 한다. 이미 검증한 DTO/경제/core receipt는 재작성하지 않는다. 단계별 Plan(계약·실패예제), Do(RED→GREEN), See(독립 리뷰·인수·잔여)를 보고서에 기록한다. 전체 완료 전 broad 리뷰를 성공한 최종 인수로 표시하지 않는다.

### Task 7: 실제 broker GET 어댑터와 legacy 수집기 통합 (사용자 단계1)

**Files:** modify `src/execution/broker/kis_kr.py`; create `tests/test_kis_execution_query_integration.py`. 다른 구현자는 이 작업이 동결될 때까지 broker 파일을 수정하지 않는다.

**Interfaces:** `KISBroker.get_execution_daily(*, account_scope, start_date, end_date, clock)` / `get_execution_cancelable(*, account_scope, clock)` → `QueryCollection`. broker.config의 계좌/상품을 사용하고 `LegacyExecutionQueries`를 단발 실행한다. timeout은 기존 broker config 값, 페이지 상한은 수집기의 기존 기본값이다. 운영 poller를 아직 교체하지 않는다.

**Plan:** 기존 `_api_get`의 GET 재시도/토큰 복구/공용 limiter 정책은 유지한다. 요청 `tr_cont`와 실제 HTTP status/응답 header를 주입형 수집기에 전달하는 선택적 내부 응답 경계를 추가한다. 성공 status를200으로 합성하지 않는다. 실패/취소 시 원장 limiter busy가 남지 않으며 실제 취득 여부를 구분한다. 기존 dict 소비자 동작은 유지한다. 미지원 dev/모의 TR은 실전 TR로 보내지 말고 호출 전에 명시 거부한다. QueryCollection은 조회 완결만 보고하고 trading_permission/finality_supported False를 유지한다.

- [ ] RED: 실제 broker `_api_get`+기존 limiter를 fake session/token clock 경계만 대체해 첫F/다음E 응답을 처리한다. 다음 요청 header N·cursor 일치/반환 complete를 검증한다. header 누락·HTTP실패·다음페이지실패·취소/timeout·실패후다음ledger획득·mock환경 거부를 고정한다.

```python
result = await broker.get_execution_daily(account_scope="test-scope", start_date="2026-09-18", end_date="2026-09-18", clock=lambda: NOW)
assert result.complete and len(result.pages) == 2
assert not result.trading_permission and not result.finality_supported
assert session.requests[1]["headers"]["tr_cont"] == "N"
```

- [ ] Do: 위 인터페이스를 기존 helper에 연결한다. 취소를 삼키거나 정상 빈 행으로 실패를 바꾸지 않는다. 계좌·토큰·HTTP원문 오류 로그를 새로 만들지 않는다. 전송/주문 API는 수정하지 않는다.
- [ ] See: `tests/test_kis_execution_queries.py`, 신규 통합시험, 기존 GET/limiter 회귀를 격리 실행한다. 독립 spec+quality 리뷰→지적 수정→한정 재리뷰. parent만 검증된 파일을 stage/commit+push한다.

### 이후 실행 순서와 인수 경계

- 사용자 단계2: 기존 Task4b116시험을 유지하며 Task5의 보호repair/일자전환/initialR·outbox 및 실제 저장소 재시작 인수를 완성한다. 외부 sink의 durable dedup 없는 전송은 pending이다.
- 사용자 단계3: Task4 전체 호출점 목록을 기준으로 단일 runtime writer·송신 guard·request 바인딩 예약을 연결한다. 정정의 추가 위험/예약을 증명하지 못하면 NOT_SENT로 유지한다. 단순 legacy 거부만으로 모든 writer 이행 완료라 하지 않는다.
- 사용자 단계4: 공식 공개 자료 고정 근거 또는 승인된 비식별 응답으로 지원 표를 갱신한다. atomic cutoff/체인 의미의 증거가 없으면 startup/finality를 열지 않는다.
- 사용자 단계5: 실제 C/F/G/R 시험명별 충족/미충족을 대조하고 독립 전체 브랜치 broad 리뷰를 한다. 미충족이 있으면 최종 완료·main/운영 전환 GO를 선언하지 않는다. main 통합·운영 전환은 이 요청에서도 별도 판단이다.

### Task 8A: 증거를 보존하는 보호 복구 (단계2의 F5b)

**Files:** create `src/execution/safety/protection_recovery.py`, `tests/test_execution_protection_recovery.py`; modify `runtime.py`, `application.py`의 fill write-set만. 이 묶음은 한 구현자가 소유한다. 다른 통합 변경은 동결 후 수행한다.

**Plan:** 기존 경제/보호 DTO와 실제 큐를 재사용한다. 새 보호 실패와 같은 commit에 마지막 정상 보호 DTO, 실패한 체결의 before/after·누적관측·증분·시각·분류·intent 및 이후 수락된 fill/quote 입력을 보존한다. 기존 이력 없는 degraded를 정상 상태로 추정하지 않는다. 거래 전송/원장 성공/시작 장벽 해제는 복구의 효과가 아니다.

- [ ] RED: 실제 큐의 등록 실패 APPLIED+degraded→재전달→동일 checkpoint 증거로 repair→재시작. 현금/수량/원가/위험 카운트 재가감0, stage/high/BE/pending/R 보존. 증거 부재·다른 lifecycle·stale version·입력 누락·충돌·기존 보유의 가짜 empty anchor는 거부.
- [ ] Do: `repair_protection(operation_id, symbol, *, expected_version)`는 owner 잠금 안에서 버전·현재 종목/주문 cursor와 저장된 증거를 확인한다. operation ID와 전체 요청 digest를 영속 receipt에 묶고 같은 ID 다른 요청은 거부한다. 같은 요청 재전달은 멱등이다. 후보에서만 입력을 재생하며 과거 청산 제안의 재송신은 금지한다. 재생으로 새 주문 판단이 생겼으나 기존 intent와 대조할 증거가 없으면 차단한다.
- [ ] 입력을 모두 보존하지 못한 레짐/면제/외부 writer의 변경, 알 수 없는 pending owner, 자료가 없는 과거 degraded는 미지원으로 남긴다. 수량만 맞춰 등록하거나 현재 설정으로 최초 손절/R을 제조하지 않는다. 정상 다른 종목의 상태를 덮어쓰지 않는다.
- [ ] See: 외부 I/O만 fake, 실제 store/core queue/ExitManager 시험, DB 저장/게시 장애와 호출 취소 후 복구를 검증한다. 독립 리뷰 후 해당 범위만 완료로 기록하며 F5b와 전체 F 인수를 구분한다.

### Task 8B: durable outbox 전달 경계 (단계2의 F7)

**Files:** create `src/execution/safety/journal_delivery.py`, `tests/test_execution_journal_delivery.py`; 필요할 때 기존 `src/data/storage/trade_storage.py`에 명시 DB transaction 기반 execution journal API를 추가한다. runtime 배선은8A 동결 후 부모가 한다.

**Plan:** 경제 APPLIED와 외부 원장 ACK를 구분한다. 실행 이벤트 키·payload digest·실제 원장 저장은 sink의 같은 transaction이어야 한다. 기존 JSON 기록/비동기 enqueue는 이 계약의 ACK로 사용하지 않는다. sink I/O는 owner lock 밖, 전달 receipt 반영은 owner 안에서 한다.

- [ ] RED: sink commit 전/후 응답 유실·중복·동일 키 다른 payload·ACK commit 실패·오래된 ACK가 최신 cursor를 해제하는 경우를 고정한다. outbox 장애로 경제/보호를 되돌리지 않는다.
- [ ] Do: immutable envelope, durable receipt 조회/재전달, 현재 key/hash 검증 및 delivered 보존을 구현한다. 같은 주문의 뒤 체결이 pending이면 cursor도 pending이며 보호 제안은 체결 원장/송신으로 혼동하지 않는다. 실제 DB 전용 API의 UNIQUE key+event payload transaction을 검증하고 legacy projection/canary 배선 여부를 따로 명시한다.
- [ ] See: 실큐→checkpoint→전달/재시작 경로를 확인한다. 외부 sink 계약 미충족/미설치는 pending 유지가 결과이며 원격 운영 DB 성공을 주장하지 않는다.

Task8C 일자 전환/initial R 및 단계3의 각 writer는 위 두 단위 리뷰 후 별도 정확한 파일·실패 인수로 고정한다. 설계 참고는 SDD task-8-design-note이며, 구현되지 않은 제안을 완료 체크하지 않는다.

### Task 8C1: 확정 사실 ingress와 KST 일자 전환

**Files:** create `src/execution/safety/day_recovery.py`, `tests/test_execution_day_recovery.py`; modify `src/core/engine.py`, `application.py`, `runtime.py`, 필요한 `economics.py`의 day-change publication. 공유 파일은 단일 구현자가 소유한다. lifecycle의 직접 prepare/claim fence 검사는 그 파일 소유권 인계 후 직렬 적용한다.

**Plan:** 큐 길이0을 정지 증명으로 쓰지 않는다. 첫 await 전 ingress ticket·strong task를 등록하여 queue-lock 대기부터 RECEIVED·경제 게시/실패까지 추적한다. 거래 admission과 확정 사실 접수를 분리하고, fence 이후 사실도 원래 주문일 그대로 durable RECEIVED로 남긴다. 미수신 거래소 체결이 없다는 증거를 만들지는 않는다.

- [ ] RED: queue lock 대기 중 caller 취소, dequeue 뒤 owner/DB 대기, fence 후 사실 도착, 최종 rollover DB await 중 전일 사실 도착을 실제 core 큐로 재현한다.
- [ ] Do: `owner.receive`는 기존 inbox 접수 경계를 분리 재사용하며 원 관측/identity 불변·접수 성공과 APPLIED를 구분한다. runtime의 prepare/rollover/resume는 요청 전체 digest·영속 receipt·잠금 안 expected_version을 사용한다. fence·세대·미완 ticket·inbox·child·관측/적용 차이·예약·보호 admission·DB/게시 상태를 점검한다. 중단/재시작 후 자동 resume는 금지한다.
- [ ] 최초 fence는 거래·새 보호 변경의 수락을 닫되 이미 수락한 작업을 유실시키지 않는다. 경제 적용이 보류된 관측은 명시 RECEIVED/parked로 보존한다. shutdown을 rollover pause로 사용하지 않고 별도 두 번째 큐 소비자를 만들지 않는다.
- [ ] 일자 전환은 현재 KST day·소유 포트폴리오와 일치하는 명시 valuation 증거가 있을 때만 일일 PnL/거래·손실 제한 및 미실현 기준선을 같은 commit으로 갱신한다. 단순 `_quotes` 값에 현재시각을 붙여 근거를 만들지 않는다. 현금/보유/원가/수수료 잔액·dedup·pending·stage/high/BE·R·과거 증거는 유지한다. 새 임의 TTL/위험 숫자/전일 종가 정책은 추가하지 않는다.
- [ ] See: 미완·결측·미지원은 BLOCKED, 다음날 첫 체결/중복은1회, pre/postcommit·publication·caller 취소·reopen·UTC/KST를 검증한다. 로컬 resume는 startup/trading_ready의 허가가 아니며, 전일 late fact의 과거 회계 처리는 미지원으로 남긴다. 실제 일일 scheduler writer 이행은 단계3 별도 인수다.

### Task 8C2: 최초 손절·최종성 증거와 초기 R 원장

**Files:** create `src/execution/safety/initial_r.py`, `tests/test_execution_initial_r.py`; modify `protection.py`, `lifecycle.py`, `application.py`, `runtime.py`, `journal_delivery.py` 및 관련 시험. C1 공유 파일 동결 후 배선한다.

**Plan:** 최초 보호 등록 성공과 같은 후보에서 기존 uncapped initial-R `StopDecision` 입력/결과·설정 provenance를 보존한다. 기존 `apply_crash_cap=False` 및 신규 등록 dynamic stop 정책을 바꾸지 않는다. 최종성은 terminal 문자열이 아니라 이미 검증된 전체 원본 근거를 append-only로 보관한다.

- [ ] RED: 부분체결 중 설정/레짐 변화, terminal 문자열만 존재, ACK/미지원 취소, 관측>적용, 원본 범위 불일치, 등록 실패→repair, 부분매도/완전청산→같은 종목 재진입, 나누어떨어지지 않는 누적대금을 고정한다.
- [ ] Do: `finalize_initial_r`는 owner 안 CAS·멱등 receipt를 사용하고 동일 최초 BUY lifecycle의 실제 최종 누적대금×저장된 초기SL/100만 확정한다. 현재 설정/남은 수량/계획 위험을 사용하지 않는다. 기존 값 충돌·모호한 add-on·지원 근거 없는 종결은 pending/BLOCKED다. 과거 lot의 R 확정이 새 lifecycle 보호를 수정하지 않는다.
- [ ] R kind는 fill 원장과 같은 typed envelope/UNIQUE transaction을 사용하되 선행 fill ACK를 요구하고 별도 `initial_r_journal_pending`으로 관리한다. R ACK가 기존 fill cursor를 풀지 않는다. 경제·위험·예약을 재가감하지 않는다. legacy 분석 원장 projection은 별도 인수다.
- [ ] See: 실제 큐/SQLite와 sink ACK 실패·재시작·payload 충돌 시험, 별도 리뷰, KST/UTC 전체 검증. 보호 repair 성공과 R 미측정은 별개이며 최초 손절 근거 없는 과거 lot은 미측정으로 남긴다.
