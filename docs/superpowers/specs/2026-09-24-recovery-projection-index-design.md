# N5 실행 복구 projection·producer index 상세 설계

> 상태: **사용자 검토 대기 중인 상세 설계. 구현·main 병합·운영 배포 승인이 아니다.**
> 작성일: 2026-09-24 KST.
> 기준: `feature/engine-safety-design-20260917`의
> `0dc3c0f7fdbee727ad849c5bea69cd8ee2612e55`.
> 우선순위: 전체 엔진 운영 전환을 막는 N4 누적 상태 성능 결함의 최우선 후속.
> 사용자 지시: N5를 최우선으로 진행. 아래 상세 계약은 별도 검토·승인 대기.

## 1. 결정 요약

매 commit이나 진단 요청마다 전체 실행 원장을 다시 복사·분류하는 방식은 채택하지 않는다.
owner가 실제로 적용한 **typed edit**에서 변경분을 직접 산출하고, 다음 두 read model을 같은
revision에 맞춰 갱신한다.

1. `OwnerRecoveryProjection`: 진단 finding/count를 담는 고정 크기 불변 DTO.
2. `ProducerRecoveryIndex`: attempt·intent·audit·admission·pending 연결을 찾는 owner-private
   인덱스와 불변 query view.

정상 commit에서는 변경된 사실과 그 역의존성만 갱신한다. 과거 checkpoint 복원, legacy
mutation, 과도한 fan-out처럼 bounded update를 증명하지 못하는 경우에만 전체 durable 사실에서
재구축한다. 재구축 중이거나 실패한 read model은 명시적으로 `unavailable`이며, 이전 정상값이나
빈0건으로 대체하지 않는다.

N5는 full checkpoint, intent, attempt, audit, inbox, outbox, replay 사실을 삭제·축약하거나
정본으로 대체하지 않는다. 파생 projection/index는 초기에는 영속 schema에 넣지 않는다.

## 2. 배경과 문제 정의

N4 합성 규모 시험은 다음을 확인했다.

- 100건에서 capture 59.37ms, producer cold/warm sweep 710.98/680.18ms로 사전50ms 게이트 실패.
- 1,000·5,000건 cold/warm sweep은 각30초 프로세스 timeout.
- 5,000건 capture는 node/text 상한으로 unavailable이며 정상0이 아니다.
- health/API/경보 연결은 이 결과 때문에 보류됐다.

근본 원인은 `ProtectionProducer`가 한 sweep 안에서 `owner.state` 전체 deepcopy와 전체 scan을
intent/episode마다 반복하기 때문이다. 반복 종목 5개인 N4 자료에서도 cold와 warm 경로가
최악 `Theta(n^2)`가 된다. 진단 capture도 owner 전체 상태를 두 번 복사한다.

현재 owner는 commit 전에 `_block()`하고 SQLite commit과 runtime publisher를 거친다. SQL 대기
중에는 과거 owner/published/engine version이 서로 같을 수 있으므로 **version equality만으로는
현재 projection을 증명할 수 없다.** publisher 실패는 durable checkpoint와 owner RAM을 앞서게
할 수도 있다. restore는 owner publication 뒤 runtime day/fence 복원을 더 수행한다.

## 3. 목표·비목표

### 3.1 목표

- 장기 누적 이력 규모와 무관한 bounded 정상 commit update와 bounded cached read.
- producer sweep의 반복 전체 deepcopy 제거와 정확한 symbol/intent scoped query.
- 기존 N3 전량 분류 결과와 finding code/count/completeness의 완전 동등성.
- commit·publish·restore·cancellation 경계에서 stale healthy projection을 절대 현재로 노출하지 않음.
- 부분체결, UNKNOWN, 예약, 취소 자식, 동일 종목 다중 episode의 기존 fail-closed 의미 보존.
- health·경보 연결 여부를 판단할 재현 가능한 offline 성능·정확성 근거 생성.

### 3.2 비목표

- 보존 이력 삭제, terminal 행 압축, cap 상향으로 성능 시험을 통과시키지 않는다.
- 주문·전략 배분·위험 한도·손절/익절·토스/KIS 책임을 변경하지 않는다.
- projection 불일치를 자동 복구 주문·예약 해제·재발행 권한으로 사용하지 않는다.
- N5 통과를 초과수익, KIS 최종성, 전체 C/F/G/R, full-engine 운영 전환 완료로 확대하지 않는다.
- 이 설계 승인만으로 main 병합·배포·재시작·주문·설정 변경을 허가하지 않는다.

## 4. 검토한 대안

| 대안 | 장점 | 결함 | 결정 |
|---|---|---|---|
| 매 publication/commit에서 전량 projection 재구축 | 구현이 단순하고 분류 동등성 확인이 쉬움 | 모든 주문·체결 완료 경로에 누적 이력 비례 CPU/할당 추가 | 기각 |
| revision별 lazy/background 전량 rebuild | commit 경로의 추가 비용이 작음 | 지속 commit 시 starvation, 첫 보호 tick에 unbounded 작업, build 중 unavailable | 복원·legacy fallback에만 사용 |
| generic reducer가 `changed_keys`를 자기신고 | 변경량이 작아 보임 | 현재 arbitrary candidate dict에서 누락을 증명할 수 없어 fail-open | 기각 |
| before/after root diff로 delta 추출 | 기존 reducer를 유지 | 관련 root 전체 scan이라 commit마다 다시 O(n) | 기각 |
| **owner 적용 typed edit + 증분 index/projection** | steady-state bounded, producer와 진단이 같은 revision 사용 | 25개 mutation 경로와 nested callback의 단계적 이행 필요 | **채택** |

## 5. 소유권과 불변 자료 계약

### 5.1 토큰

모든 파생 read model은 다음 토큰에 결속한다.

```python
@dataclass(frozen=True)
class OwnerToken:
    incarnation: str
    publication_epoch: int
    revision: int
```

- `incarnation`: owner 객체 수명마다 새 값. 같은 DB revision으로 owner가 교체돼도 구 결과 거부.
- `publication_epoch`: commit/restore를 시작하기 **전에** 증가·무효화. SQL 중 과거 version 세 개가
  같아도 구 projection은 current가 아니다.
- `revision`: durable execution checkpoint version.

runtime/producer의 day generation, fence, shutdown, handler/gateway/reconciler binding과 RAM episode는
owner revision 없이 바뀔 수 있으므로 별도 `runtime_generation`과 `producer_epoch`을 사용한다.

### 5.2 OwnerRecoveryProjection

고정 크기 frozen DTO만 외부에 노출한다.

```python
@dataclass(frozen=True)
class OwnerRecoveryProjection:
    schema_version: int
    token: OwnerToken
    complete: bool
    unavailable_reason: str | None
    findings: tuple[tuple[str, int | None], ...]
```

`findings`에는 식별자·가격·자유문구를 넣지 않는다. malformed 사실은 버리지 않고 고정
finding 또는 `complete=False`로 보존한다. `None`은 N3의 `disposition_not_durable`처럼 지원하지
않거나 측정하지 않은 수치이며0이 아니다. `complete=False`에서 부재 finding 역시0건이 아니다.

### 5.3 ProducerRecoveryIndex와 view

owner가 index를 소유한다. producer 자체 cache가 commit/publish 원자성을 추측하지 않는다.
내부 index는 최소 다음 역조회 관계를 보존한다.

- attempts by symbol / intent, child attempts by parent.
- intents by symbol과 intent가 선언한 attempt membership.
- protection audit by command / intent / symbol.
- quote admission by command / intent / symbol.
- pending symbols by intent, protected/degraded/portfolio fact by symbol.
- malformed·orphan·duplicate·unknown 사실을 나타내는 invalid bucket.

외부에는 mutable dict나 checkpoint row가 아니라 detached frozen facts와 scoped query만 제공한다.

```python
class ProducerReadView:
    token: OwnerToken
    complete: bool

    def attempts_for_symbol(self, symbol: str) -> tuple[AttemptFact, ...]: ...
    def attempts_for_intent(self, intent_id: str) -> tuple[AttemptFact, ...]: ...
    def intent(self, intent_id: str) -> IntentFact | None: ...
    def protection_audits(self) -> tuple[AuditFact, ...]: ...
    def audits_for_intent(self, intent_id: str) -> tuple[AuditFact, ...]: ...
    def admissions_for_intent(self, intent_id: str) -> tuple[AdmissionFact, ...]: ...
    def pending_symbols_for_intent(self, intent_id: str) -> tuple[str, ...]: ...
    def intents_for_symbol(self, symbol: str) -> tuple[IntentFact, ...]: ...
```

view 획득 시 owner healthy, index complete, incarnation/epoch/revision, owner/published version을 함께
검사한다. producer가 await한 뒤 다시 owner 사실을 읽어야 하면 새 view를 받아야 한다. 구 view와
새 revision을 섞지 않는다.

## 6. 신뢰 가능한 mutation 계약

### 6.1 arbitrary reducer를 delta로 승격하지 않는다

현재 제품에는 `_commit_publish` 여섯 경로와 `owner.mutate` 25개 호출점이 있다. generic reducer는
임의 root를 바꿀 수 있고, risk-source completion·regime application·fill economics 같은 callback은
호출자가 직접 보지 못하는 nested effect도 만든다.

새 patch-only API는 owner가 실제 적용하는 edit로부터 delta를 직접 만든다.

```python
MutationPlan(
    base_token=OwnerToken(...),
    operation=MutationKind(...),
    edits=tuple[SetRow | DeleteRow | SetScalar | ReplaceRoot, ...],
    result=immutable_domain_result,
)

ProjectionDelta(
    base_token=...,
    target_revision=...,
    changes=tuple[RowChange, ...],
    disposition="incremental" | "rebuild_required",
    reason=None | FixedReason,
)
```

호출자는 `complete=True`나 changed key를 자기신고하지 않는다. owner가 detached candidate에 typed
edit를 적용할 때 old/new projection fact를 기록한다. policy generation finalizer가 추가한 쓰기도
owner가 기록한다.

### 6.2 legacy 경로

기존 `mutate(command_id, reducer)`는 단계적 이행 동안만 호환한다. 성공한 legacy mutation은
항상 projection/index를 즉시 unavailable로 만들고 bounded rebuild를 요청한다. 구 projection을
유지하거나 다음 read에서 동기 전량 scan하지 않는다.

정적 inventory 시험은 다음을 고정한다.

- 모든 `_commit_publish` 호출이 owner-generated delta 또는 명시적 rebuild disposition을 전달.
- direct store commit/publication/state replacement는 승인된 coordinator seam에만 존재.
- 남은 product legacy `mutate` 호출 목록이 checked-in migration inventory와 정확히 일치.
- 새 callback은 typed plan 또는 invalidation 중 하나를 반드시 선택.

이 검사는 Python reflection에 대한 보안 증명은 아니지만 새 미분류 writer의 조용한 추가를 막는다.

### 6.3 high fan-out

parent attempt, intent membership, audit, admission, pending-owner, portfolio/protection 수량은 한 변경이
많은 관계에 영향을 줄 수 있다. 의미를 보존하는 aggregate dependency bucket을 우선 사용한다.
정상 증분 commit의 고정 work budget은 `RowChange` 최대64개, 서로 다른 dependency edge touch
최대256개다. 동일 edge 중복은 한 번만 세며, `ReplaceRoot`는 크기와 무관하게 항상
`rebuild_required`다. 65번째 row 또는257번째 edge가 필요하다는 사실을 확인한 시점에 증거를
자르거나 나머지를 순회하지 않고 `rebuild_required`로 전환한다. 64/65와256/257 경계를 RED로
고정하며, 결과를 본 뒤 숫자를 완화하려면 이 문서를 덮어쓰지 않고 새 사전 등록을 만든다.

UUID/dict 순서로 같은 종목의 최신 episode를 추측하지 않는다. 동일 종목 rejected intent가 둘
이상이고 pending 증거가 하나를 특정하지 못하면 계속 `restart_episode_order_ambiguous`다.

## 7. commit·publish·restore 상태 전이

### 7.1 정상 commit

1. 기존 owner lock 안에서 `publication_epoch`을 증가시키고 current projection/index를 무효화한다.
2. typed plan을 candidate에 적용하며 owner-generated delta와 candidate index update를 준비한다.
3. bounded work를 넘거나 미지원 edit면 candidate domain 변경은 계속 검증하되 read model
   disposition은 `rebuild_required`다.
4. 기존 full checkpoint와 commit ID/digest를 그대로 SQLite transaction에 commit한다.
5. commit 실패·결과 불명·caller cancellation이면 candidate read model을 공개하지 않는다.
6. durable 성공 후 state/version과 candidate read model을 owner-private로 설치하되 아직 query 불가다.
7. 기존 runtime publisher가 성공한 뒤에만 published version과 projection/index current pointer를
   같은 token으로 공개한다.
8. publisher가 중간 실패하면 owner는 기존처럼 recovery-required이며 새·구 index 모두 query 불가다.

projection 실패만으로 durable owner 사실을 지우거나 commit을 되돌리지 않는다. 그러나 index를
사용하는 recovery producer는 unavailable을 빈 결과로 보지 않고 어떤 release/reissue/submit도
수행하지 않는다.

### 7.2 restore

- policy registration receipt와 checkpoint를 기존 동일 SQL read snapshot에서 검증한다.
- projection이 없는 legacy checkpoint도 전체 durable 사실에서 재구축하며 빈0으로 간주하지 않는다.
- 전체 rebuild는 startup/admission recovery barrier 아래서 수행한다. 이벤트 루프 stall을 제한하도록
  detached immutable payload를 worker가 소비하거나 cooperative chunk를 사용하며 mutable owner RAM을
  worker thread에 넘기지 않는다.
- policy receipt 손상, builder 오류, stale completion, cancellation은 unavailable이다.
- owner publication뿐 아니라 runtime day/fence/admission 복원 완료 generation까지 맞아야 복구 capture가
  current다.
- rebuild 결과는 incarnation/epoch/revision이 모두 일치할 때만 수락한다. 한 active build와 최신 desired
  token 하나만 유지하며 revision별 backlog를 만들지 않는다.

### 7.3 transient producer observation

producer RAM episode, failure, restart cooldown, lock, shutdown은 producer lock 안에서 epoch를 증가시키고
불변 observation pointer를 교체한다. recovery capture는 owner projection과 producer observation을
A-B-A-B로 읽어 identity/token/binding/lock이 앞뒤 모두 같을 때만 안정 표본으로 인정한다.

## 8. producer 의미 보존

`_sweep`은 시작 시 정확히 하나의 `ProducerReadView`를 얻고 `_attempts`, `_episode_status`,
`_audit_recovery`, `_restart_cooldowns`에 명시적으로 전달한다. owner 전체 상태를 다시 읽지 않는다.

반드시 보존할 fail-closed 의미:

- UNKNOWN, `blocked_unknown`, evidence conflict, observed != applied, terminal+reservation을 모두 유지.
- attempt index와 intent membership의 extra/orphan/duplicate/missing link는 inconsistent/block.
- audit symbol/intent/quantity, RAM episode, pending owner, admission이 한 종목으로 수렴하지 않으면
  `protection_decision_evidence_mismatch`.
- malformed/unknown/귀속 불가 audit symbol은 global block이며 조용히 제외하지 않음.
- late fill/reconcile/NOT_SENT/REJECTED는 새 revision에 즉시 반영하고 예전 status cache 재사용 금지.
- admission add/remove, pending add/release, audit 생성/변형은 모두 index update 대상.
- release/resume/submit 같은 writer 뒤 await 경계에서 새 revision view를 다시 획득·검증.

목표 복잡도는 symbol/intent 조회 `O(관련 행 수)`, 전체 sweep `O(현재 관련 index 사실 수)`다.
retained history가 늘어도 같은 intent query가 전체 owner deepcopy를 수행하지 않아야 한다.

## 9. 단계적 이행과 활성화 경계

1. **N5-A — oracle·무효화:** pure full builder, token/invalidation, mutation-route inventory와 RED.
   제품 consumer는 아직 기존 경로를 사용한다.
2. **N5-B — typed mutation:** 단순 inbox/lifecycle부터 compound fill, quote/recovery, day,
   risk/regime callback까지 25개 writer와 여섯 commit route를 patch-only 계약으로 이행한다.
3. **N5-C — shadow parity:** projection/index를 매 revision 구축하지만 capture/producer 의사결정에는
   사용하지 않고 기존 N3 oracle과 differential 비교한다. unexpected legacy route가0이어야 한다.
4. **N5-D — producer/capture 전환:** 정확 revision view로 producer hot path와 N3 capture를 교체한다.
   unavailable에서는 writer0, stale full-scan fallback0.
5. **N5-E — 성능·broad gate:** 아래 전체 matrix와 UTC/KST 회귀, 격리·비밀정보 검사,
   독립 critical review를 통과한다.
6. **후속 N6:** 통과 결과가 있을 때만 health/API/alert 공용 sampler를 별도 설계·RED로 연결한다.

각 중간 commit은 동작 의미를 명시한다. N5-C 이전 미완 migration을 live consumer에 켜지 않는다.

## 10. RED 정확성·고장 인수

최소 시험 파일:

- `tests/test_recovery_projection.py`: projection/index builder와 typed edit add/update/delete parity.
- `tests/test_recovery_projection_faults.py`: off-by-one token, commit/publish/cancellation/restore 고장.
- `tests/test_recovery_projection_acceptance.py`: 실제 runtime fill/partial/UNKNOWN/cancel/day/fence/account.
- `tests/test_protection_producer_recovery_index.py`: 반복 종목·모호 episode·audit/admission/pending.
- `tests/recovery_projection_scale_harness.py`: 합성 cohort와 serial phase 측정.
- `tests/test_recovery_projection_performance.py`: opt-in 예산·timeout/censoring 계약.

필수 RED 사건:

- SQL을 멈춘 동안 과거 owner/published/engine version이 같아도 구 projection current 금지.
- commit rollback, durable commit 뒤 응답 유실, caller cancellation, publisher 전/후 실패.
- 동일 revision restore, 같은 revision의 owner 교체, 늦게 완료된 구 rebuild 결과 거부.
- projection 없는 legacy checkpoint rebuild, 손상 policy receipt 계속 거부.
- BUY/SELL 부분·완전체결, UNKNOWN, unapplied observation, 잔여 cash/exposure/risk reservation.
- cancel child와 parent finality, rejected order, uncertain cancel, day rollover.
- 같은 symbol 다중 intent/episode, insertion order 역전, audit/admission/pending/RAM mismatch.
- malformed/orphan/duplicate/missing link를 제거하지 않고 incomplete/block로 보존.
- consumer가 반환된 DTO를 변형해도 owner/index 불변.
- full checkpoint/reopen/journal dedup/economics 결과가 변경 전과 동일.
- owner commit이 없는 producer episode/failure/binding/handler/reconciler 변화도 stale 표본 거부.

projection 결과는 timing 밖에서 기존 N3 full classifier와 finding code/count, completeness, token을
정확히 비교한다. 단순 line coverage 대신 다음 mutation kill이 각각 한 RED를 실패시켜야 한다:
root별 index update 제거, revision ±1, publish 실패 때 old cache 유지, missing projection→empty,
producer epoch/binding 검사 제거, dict 순서로 최신 추정, UNKNOWN/예약/unknown risk→0.

## 11. 사전 등록 성능 gate

동일 호스트·직렬·offline synthetic 연구 gate다. 운영 SLO로 확대하지 않는다.

### 11.1 cohort

- `n4_rejected`: `0/100/1000/5000`, intent/attempt/outbox 각각 size, live `min(size,2)`,
  나머지 실제 fixture `final_rejected`, 반복 symbol 5개.
- `mixed_recovery`: `100/1000/5000`, terminal rejected 60%, full BUY 10%, full SELL 10%,
  partial BUY 5%, partial SELL 5%, UNKNOWN submit 5%, ACK nonterminal SELL 3%, ACK cancel child 2%.
- `mixed_recovery`의 size는 **intent 수**다. 모든 size가100의 배수이므로 위 비율은 반올림 없이
  정확한 정수다. terminal rejected 60%는 BUY30%/SELL30%로 고정하고 UNKNOWN5%는 SELL로
  고정한다. intent마다 submit attempt 하나를 두며 cancel-child2%만 부모 SELL에 cancel attempt
  하나를 추가하므로 총 attempt 수는 `size * 1.02`다. 모든 SELL intent에는 연결된
  protection-decision outbox 하나를 두므로 outbox 수는 `size * 0.55`다. symbol은 index modulo5,
  insertion order는 정방향/역방향 두 fixture로 고정한다. 결과 행에는 이 예상/실제 수를 모두 쓴다.
- correctness 사건은 account/day/exchange/order-number 충돌, admissions, pending, audit, RAM episode를
  별도 고정한다.

### 11.2 예산

| 구간 | 합격 예산 |
|---|---:|
| 정상 동기 projection/index update | 모든 규모 trial 최대10ms |
| end-to-end commit wall | 모든 규모 최대1,000ms |
| indexed commit의 ABBA paired 추가 비용 | 20쌍 median 상대 증가 ≤10% 및 최대 delta ≤25ms |
| 5,000건 cold restore load+validate+rebuild+publish | 최대1,000ms |
| producer cold index/restart preparation | 모든 규모 최대50ms |
| producer warm sweep | 모든 규모 최대10ms |
| immutable cached read | 최대5ms |
| diagnostic builder+finite JSON | 최대5ms |
| 제품 연산 중 연속 event-loop stall | 최대50ms |
| 공개 JSON | 최대16KiB |

commit wall과 event-loop stall은 별개로 측정한다. timeout, unavailable, incomplete reconstruction,
두 번의 noisy host 결과는 PASS나0ms가 아니라 connection-ineligible/inconclusive다.

N4의 원래 gate에는 capture, `owner.state`, producer cold/warm이 모든 규모에서 각각50ms 이하여야
한다는 조건이 있었다. N5는 `owner.state` 자체를 빠르게 만든다고 주장하지 않는다. 그 N4 항목은
5000건에서 계속 실패한 역사적 결과로 보존한다. 대신 recovery capture와 producer의 제품
call-site가 `owner.state`를 호출하지 않고 exact-revision projection/scoped query만 쓴다는 정적
inventory와 runtime spy를 필수 인수한다. 이를 만족하지 못하면 아래 N5 gate가 빨라도 연결
부적격이다. 따라서 N5는 N4 결과를 PASS로 다시 쓰는 것이 아니라, 실패한 소비 구조를 제거하는
별도 사전 등록 gate다.

각 셀은 별도 `env -i` pytest 프로세스, 외부 timeout30초, 동시 실행0으로 수행한다. fixture 생성,
deepcopy, oracle 비교는 timer 밖이다. control/indexed를 ABBA 순서로20쌍 측정한다. normal latency와
tracemalloc run을 분리한다.

직렬 matrix는 `n4_rejected` 4 sizes와 `mixed_recovery` 3 sizes 각각에 대해 다음9종을 모두 실행한다:
`projection-update`, `commit-control`, `commit-indexed`, `restore`, `producer-cold`, `producer-warm`,
`cache-read`, `json`, `continuous-stall`. 즉 operation 측정 셀은 `(4 + 3) * 9 = 63`개이며,
ABBA commit 하위 trial과 정방향/역방향 correctness fixture는 별도로 기록한다.

연속 loop probe는 1ms `loop.call_at` deadline을 독립 task가 전·중·후에 기록한다. N4의 첫 양보
한 번을 최대 stall로 부르지 않는다. 시작 전250ms baseline max가25ms를 넘으면 한 번만 새 프로세스로
재실행하고, 재차 noisy면 기준을 낮추지 않고 보류한다.

phase marker는 최소 다음을 flush한다:

`setup → warmup → control → projection_update → store_encode → store_commit_wait → publish →
restore_load → restore_rebuild → producer_cold|producer_warm → cache_read → json → validation → cleanup`

각 raw 결과는 `active_phase`, `phase_started_monotonic`, `last_completed_phase`, `exit_code`,
`measurement` 또는 명시적 `null`, `cleanup_completed`를 보존한다. timeout은 censored이며0으로
채우지 않는다. active phase 시작시각이 실제 남았을 때만 해당 phase의 관측 하한을 기록하고,
그 수치를 전체 제품 연산의30초 초과로 일반화하지 않는다.

### 11.3 후속 sampler freshness 계약

N6가 시작되더라도 이 경계는 결과를 본 뒤 바꾸지 않는다.

- sampler 주기는15초, 표본 age가 `0 <= age <= 30초`일 때만 current다.
- age가30초를 **초과**하면 stale다. 음수 age, 미래 captured time, naive/invalid clock은
  `unavailable(reason=clock_invalid)`이며 current/stale 어느 쪽의 finding count도 제공하지 않는다.
- refresh 실패 시 stale-while-error를 금지한다. cache를 unavailable marker로 원자 교체하고 이전
  green 표본의 `last_success_at`과 revision만 참고 metadata로 남긴다.
- stale 표본은 마지막 성공 시각/revision을 표시할 수 있지만 과거 finding count를 current count로
  내보내지 않는다.
- age `0`, 정확히`30초`, `30초 + 최소 clock tick`, 음수, wall clock 역행을 각각 RED로 고정한다.
- health/API/alert 두 소비자는 같은 `sample_id`, token, freshness 판정을 공유하며 private capture를
  따로 호출하지 않는다.

## 12. 최종 gate와 증명 한계

N5-D/E 통과 조건:

1. 새 RED가 기존 결함 때문에 실제로 실패한 증거.
2. 모든 cohort에서 N3 oracle parity와 mutation kill 통과.
3. commit/restore/publish/cancellation 모든 경계 fail-closed, stale-current0.
4. serial 성능 matrix 전 셀 완료, 모든 예산 충족, unresolved timeout/noisy0.
5. focused tests 후 전체 suite를 UTC→KST 직렬 실행, 기존 xfail만 허용.
6. 비밀정보·격리 검사 통과.
7. 작성자가 아닌 Astra/xhigh 또는 검증된 Claude Opus의 source/diff/raw 측정 독립 리뷰.

그 뒤에도 증명되는 것은 projection/classifier 동등성, revision fail-close, restore rebuild,
합성 규모 성능과 순수 읽기 계약이다. 실제 장중 분포·호스트 tail latency·KIS 최종성·전체 C/F/G/R은
별도다. health 연결 전 최소5개 완전 거래일 read-only shadow에서 다음을 요구한다:

- revision mismatch0, stale-as-current0.
- sampler가 만든 writer/API/order0.
- sampler attributable 50ms 초과 stall0.
- open/regular/closing/closed/no-holdings/stopped 상태별 기대 분류 관측.

N5 통과는 health 설계 착수 자격이다. health와 alert는 같은 `sample_id/revision`을 읽고 recovery
writer를 호출하지 않아야 한다. full-engine main 통합·운영 전환은 그 후 전체 C/F/G/R과 broad
review에서 별도 판단한다.

## 13. 역할·검토 기록

- coordinator: 통합 설계와 최종 증거 소유.
- owner/publication critical analysis: 요청 `gpt-6-astra/high`; 실제 model/effort 메타데이터는
  미노출이라 미검증.
- producer complexity/index analysis: 요청 `gpt-5.6-sol/high`; 실제 metadata 미검증.
- acceptance/performance analysis: 요청 `gpt-5.6-sol/high`; 실제 metadata 미검증.

세 분석은 같은 base SHA에서 read-only로 수행했고 구현·자가 승인·운영 조작을 하지 않았다.
향후 구현은 파일 소유권을 나눈 격리 worktree와 Plan→Do→See를 사용하며, critical 변경 작성자는
자기 변경을 최종 승인하지 않는다.
