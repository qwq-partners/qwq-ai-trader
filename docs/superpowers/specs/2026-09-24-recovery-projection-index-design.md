# N5 실행 복구 projection·producer index 상세 설계

> 상태: **Astra/xhigh 독립 설계 재검토 APPROVE. 사용자 승인 대기이며 구현·main 병합·운영
> 배포 승인이 아니다.**
> 작성일: 2026-09-24 KST.
> 기준: `feature/engine-safety-design-20260917`의
> `0dc3c0f7fdbee727ad849c5bea69cd8ee2612e55`.
> 우선순위: 전체 엔진 운영 전환을 막는 N4 누적 상태 성능 결함의 최우선 후속.
> 사용자 지시: N5를 최우선으로 진행. 아래 상세 계약은 별도 검토·승인 대기.

## 1. 결정 요약

매 commit이나 진단 요청마다 전체 실행 원장을 다시 복사·분류하는 방식은 채택하지 않는다.
owner가 실제로 적용한 **typed edit**에서 변경분을 직접 산출하고, 다음 세 구조를 같은
revision에 맞춰 갱신한다.

1. `OwnerRecoveryProjection`: durable owner 사실만의 finding/count를 담는 고정 크기 불변 DTO.
2. `OwnerRecoveryJoinView`: RAM episode와 합성할 durable aggregate를 담는 불변 join frame.
3. `ProducerRecoveryIndex`: attempt·intent·audit·admission·pending 연결을 찾는 owner-private
   인덱스와 불변 query view.

정상 commit에서는 변경된 사실과 그 역의존성만 갱신한다. 전체 durable 사실의 재구축은 과거
checkpoint 복원과 활성화 전 명시적 migration/shadow 절차에서만 owner가 직렬로 선행 수행한다.
활성화된 일반 거래 writer가 bounded update를 증명하지 못하거나 work budget을 넘으면 저장소를
호출하기 전에 `projection_budget_exceeded`로 거부한다. 백그라운드 latest-token rebuild나 다음
read에서의 동기 full scan은 없다. 재구축 중이거나 실패한 read model은 명시적으로
`unavailable`이며, 이전 정상값이나 빈0건으로 대체하지 않는다.

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
- 동일하게 검증된 불변 논리 사실에 대한 기존 N3 순수 분류 finding code/count의 완전 동등성.
- publication·currentness·RAM 합성까지 포함한 N5 공개 완전성은 별도 전이 계약으로 더 보수적으로
  판정하고, N3의 공개 `counts_complete` 값 자체와 같다고 주장하지 않음.
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
| revision별 lazy/background 전량 rebuild | commit 경로의 추가 비용이 작음 | 지속 commit 시 starvation, 첫 보호 tick에 unbounded 작업, build 중 unavailable | 기각. 복원·명시적 migration은 직렬 선행 build |
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
    fault_epoch: int
    revision: int
```

- `incarnation`: owner 객체 수명마다 새 값. 같은 DB revision으로 owner가 교체돼도 구 결과 거부.
- `publication_epoch`: owner 안전 상태를 무효화하는 단조 증가 generation이다. 모든 candidate
  준비·검증이 끝난 뒤 실제 store submission 또는 restore publication barrier에 들어가기
  **직전**, 그리고 commit 없이 호출되는 모든 `_block()` fault에서 동기적으로 증가한다.
  정상 PREPARING 거부에는 증가하지 않으며, SQL 중 과거 version 세 개가 같아도 구 projection은
  current가 아니다.
- `fault_epoch`: `_block()`, restore barrier, owner replacement처럼 진행 중 writer lease를 모두
  폐기해야 하는 안전 경계에서만 증가한다. 정상·무관 commit은 이를 바꾸지 않는다.
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

이 DTO의 `findings`는 durable owner-only 기여분이다. 최종 공개 finding은 아래 A-B-A-B 합성기가
RAM·cross-state 기여분을 더해 만든다. 식별자·가격·자유문구는 넣지 않는다. malformed 사실은
버리지 않고 고정 finding 또는 `complete=False`로 보존한다. `None`은 N3의
`disposition_not_durable`처럼 지원하지 않거나 측정하지 않은 수치이며0이 아니다.
`complete=False`에서 부재 finding 역시0건이 아니다.

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
    def audits_for_intent(self, intent_id: str) -> tuple[AuditFact, ...]: ...
    def admissions_for_intent(self, intent_id: str) -> tuple[AdmissionFact, ...]: ...
    def pending_symbols_for_intent(self, intent_id: str) -> tuple[str, ...]: ...
    def intents_for_symbol(self, symbol: str) -> tuple[IntentFact, ...]: ...
    def active_recovery_symbols(self) -> tuple[str, ...]: ...
```

view 획득 시 owner healthy, index complete, incarnation/epoch/revision, owner/published version을 함께
검사한다. producer가 await한 뒤 다시 owner 사실을 읽어야 하면 새 view를 받아야 한다. 구 view와
새 revision을 섞지 않는다.

### 5.4 durable·RAM 합성 산술

최종 분류 count는 다음 네 기여분의 **가산 합**이다. 같은 사고를 가리키더라도 code·entity 단위
dedupe를 하지 않는다.

```text
final_counts = owner_base_counts
             + owner_episode_cross_counts
             + runtime_producer_transient_counts
             + capture_overlays
```

- owner-only: inbox/observation/unknown BUY, reservation/cancel/attempt-link/pending SELL,
  protection quantity와 durable link, degraded의 `repair_only`.
- cross-state: audit↔RAM episode 불일치, episode 유무에 따른 `protection_unsubmitted`, intent나
  admission이 없는 pending↔same-symbol episode fallback.
- transient: unattributed failure 0/1, runtime failure map cardinality, producer
  `recovery_required` map cardinality, invariant violation 0/1.
- capture overlay: publication/owner-health/volatile/unavailable 제어 finding.

owner는 historical row DTO 대신 다음 aggregate만 `OwnerRecoveryJoinView`에 보존한다.

```text
A_i     = intent i의 episode-eligible audit 수
A_i,k   = (intent, symbol, validated decision equality key)가 같은 audit 수
U_i     = episode가 없을 때 protection_unsubmitted인 audit 수
E_i     = producer RAM의 intent i episode 수
E_i,k   = 같은 exact key의 episode 수
P       = episode fallback이 필요한 (symbol, intent) pending 수

audit_episode_mismatch = sum_i(A_i * E_i - sum_k(A_i,k * E_i,k))
protection_unsubmitted = sum_i(U_i) - sum_{i: E_i > 0}(U_i)
pending_episode_mismatch = P - same-symbol/same-intent episode match 수
```

한 audit/episode pair에서 symbol과 decision이 모두 달라도 불일치는1건이다. 여러 audit·episode는
곱집합 multiplicity를 보존한다. pending 자체는 audit를 submitted로 만들지 않는다. admission이
command와 intent에 동시에 맞아도 한 번만 처리한다. map overwrite는 현재 key cardinality를
센다. decision equality는 공동 유효 domain에서 N3의 Python tuple equality와 정확히 같다:
audit는 길이3 list, episode는 길이3 tuple이고 `(sell_all|sell_partial, exact int > 0, exact str)`이며
intent/symbol은 trim된 비어 있지 않은 exact str이다. 이 domain에서는 list audit를 tuple로 만든
canonical key가 동일 의미다. episode의 quantity가 bool/float/Decimal이거나 decision이 list,
길이·identity가 잘못된 경우 숫자 강제변환이나 `repr` key를 만들지 않는다. B를
`complete=False`로 만들고 `snapshot_unavailable`과 `evidence_invalid`를 내며 semantic parity
domain 밖의 의도적인 N5 fail-closed 차이로 기록한다. capture 안정성 fingerprint는 이와 별개로
계속 type-sensitive다. `effect_source` 결측 또는 명시적 `None`은 일반 protection audit이고,
`intraday_preemptive`는 제외하며, 그 밖의 비-null source는 `evidence_invalid` 1건을 더한다.
checked arithmetic을 사용하고 count를 포화시키지 않는다. 양의 finding이 기존 DTO 상한
100,000을 넘으면 `snapshot_unavailable=1`과 `evidence_invalid=1`이며 잘라서 공개하지 않는다.

### 5.5 runtime composition frame과 A-B-A-B

owner와 producer가 서로의 RAM을 소유하지 않는다. runtime composition coordinator가 다음 불변
pointer 두 개를 결합한다.

```python
@dataclass(frozen=True)
class OwnerDiagnosticFrame:                 # A
    token: OwnerToken
    projection: OwnerRecoveryProjection
    join_view: OwnerRecoveryJoinView
    classification_available: bool
    unavailable_reason: FixedReason | None
    current_for_producer: bool

@dataclass(frozen=True)
class RuntimeRecoveryObservation:           # B
    bound_owner_token: OwnerToken
    runtime_generation: int
    producer_epoch: int
    complete: bool
    unavailable_reason: FixedReason | None
    cross_counts: CountVector
    transient_counts: CountVector
    binding_fingerprint: BindingFingerprint
    lock_generation: int
    lock_states: tuple[bool, ...]
```

PREPARING 진입·종료는 A pointer/token을 바꾸지 않지만 B의 owner `mutation_in_flight` lock state와
`lock_generation`을 바꾼다. 따라서 진행 중 diagnostic은 old durable frame을 볼 수 있어도
`counts_complete=False`이고, 거부가 끝나면 의미상 기존 availability로 돌아간다.

episode add/delete/replace는 위 식의 O(1) delta로 B를 교체하고 `producer_epoch`을 증가시킨다.
runtime failure·day·binding·handler·reconciler·shutdown처럼 capture 의미를 바꾸는 변경은
`runtime_generation` 또는 해당 generation을 증가시킨다. owner token 교체는 B를 새 token에
재결속한다.

composition coordinator는 현재 `E_i/E_i,k` episode aggregate를 소유한다. owner candidate는
`OwnerJoinDelta`를 만들며 이 역시64/256 budget에 포함한다. coordinator update는 같은 event loop의
await 없는 pointer-swap critical section이며 async lock이 아니다. publisher 성공 직전 owner gate
안에서 현재 episode aggregate에 join delta를 적용해 새 B와 owner A를 함께 준비한다. B를 먼저
설치하고 `current_for_producer=True`인 A를 마지막 단일 pointer swap으로
설치하는 지점이 recovery read-model publication의 linearization point다. 중간 capture는 A-B-A-B에
실패하며 producer는 마지막 A 전에는 권한 view를 얻지 못한다. store 대기 중 episode가 변했어도
historical scan 없이 최신 RAM에 대해 정확하다. coordinator는 owner/producer/quote lock을 얻거나
await하지 않으며 pointer swap을 마친 뒤에만 caller가 await할 수 있다. restore에서만 전체 A/B를
한 번 구성한다.

진단은 `A1 → B1 → A2 → B2`를 읽고 pointer identity, owner token 전 필드, B의 bound token,
runtime/producer generation, binding fingerprint, lock generation/state가 모두 같을 때만 합성한다.
episode add→delete ABA는 값이 되돌아와도 epoch로 탐지한다. 불일치하면 과거 count 없이
`snapshot_volatile=1`만 반환한다. 같은 owner revision에서도 episode 변화 직후 B와 최종 count는
즉시 달라진다. producer 권한 view는 `current_for_producer=True`인 exact-token frame에서만 얻는다.

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
    disposition="incremental" | "migration_full_build" | "reject",
    reason=None | FixedReason,
)
```

호출자는 `complete=True`나 changed key를 자기신고하지 않는다. owner가 detached candidate에 typed
edit를 적용할 때 old/new projection fact를 기록한다. policy generation finalizer가 추가한 쓰기도
owner가 기록한다.

### 6.2 legacy 경로

기존 `mutate(command_id, reducer)`는 활성화 전 shadow migration 동안만 호환한다. 그 기간의
legacy mutation은 owner admission barrier 아래에서 candidate와 전체 projection/index를 **한 번
직렬 선행 build**한 뒤에만 commit할 수 있다. 비동기 rebuild 요청이나 latest-token coalescing은
없다. N5-D 활성화 gate는 product legacy route가0임을 요구하며, 그 뒤 legacy 호출은 commit 전
`legacy_mutation_unsupported`로 거부한다. 구 projection을 다음 read에서 full scan하지 않는다.

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
최대256개다. 동일 edge 중복은 한 번만 센다. 65번째 row 또는257번째 edge가 필요하다는 사실을
확인한 시점에 증거를 자르거나 나머지를 순회하지 않고 준비를 중단한다. shadow migration에서
명시적으로 full-build eligible인 route만 선행 build로 전환한다. N5-D 이후 일반 거래 route와
`ReplaceRoot`는 저장소·publisher 호출0으로 `projection_budget_exceeded` 또는
`replace_root_unsupported`를 반환하며 현재 pointer/token/revision/healthy 상태를 그대로 보존한다.
64/65와256/257 경계를 RED로 고정하며, 결과를 본 뒤 숫자를 완화하려면 이 문서를 덮어쓰지 않고
새 사전 등록을 만든다.

UUID/dict 순서로 같은 종목의 최신 episode를 추측하지 않는다. 동일 종목 rejected intent가 둘
이상이고 pending 증거가 하나를 특정하지 못하면 계속 `restart_episode_order_ambiguous`다.

### 6.4 prevalidation 경계

candidate 준비는 현재 publication을 먼저 무효화하지 않는다. owner의 공정한 직렬화 lock 안에서
base token, command/plan, domain, policy-finalizer 쓰기, full checkpoint encoding, 64/256 budget,
projection/index candidate를 모두 검증한다. 다음 사건은 **store submission 전 거부**다.

- stale quote, duplicate admission, invalid command/plan, base-token mismatch, domain validation 실패.
- encoding/builder 실패 또는 준비 중 cancellation.
- 활성화 후 legacy/ReplaceRoot/normal-budget 초과.

이 경우 기존 projection/index pointer identity, token, revision, publication epoch, owner healthy를
정확히 보존하고 store/publisher/rebuild 호출은0이다. store lookup 자체가 필요한 오류는 기존처럼
fail-closed이며 이 목록의 무변경 거부로 분류하지 않는다. 준비된 candidate를 버린 뒤 producer가
보는 기존 current 결과가 달라져서는 안 된다. 이 동일성 보장은 준비 중 별도 health fault가 없다는
전제다. 동시 `_block()`은 더 높은 우선순위의 안전 전이이며 candidate 종료가 epoch나 blocked 상태를
되돌릴 수 없다.

## 7. commit·publish·restore 상태 전이

### 7.1 상태 기계와 정상 commit

owner 상태는 `READY → PREPARING → SUBMITTING → PUBLISHING → READY`다. 실패 후에는
`RECOVERY_REQUIRED`, restore 중에는 `RESTORING`이다.

`_block()`은 별도 async lock을 얻지 않는 중앙 fault seam이다. owner event-loop callback에서
동기적으로 health/recovery latch를 세우고 `publication_epoch`과 `fault_epoch`을 증가시키며, A를
`classification_available=False`, `current_for_producer=False`인 새 frame으로 교체한다. B는 즉시
새 token과 불일치하므로 capture가 거부하며 후속 coordinator 정리 전에도 안전하다. routine
`SUBMITTING/PUBLISHING` 전이는 `_block()`을 상태 표시용으로 재사용하지 않고 아래 전용 transition을
쓴다. direct health flag writer나 이 seam을 우회하는 fault route는 inventory상0이어야 한다.

| 상태 | 기존 current pointer | producer 신규 view | 공개 completeness |
|---|---|---|---|
| `READY` | exact token이면 current | 공정 lock으로 획득 | 모든 조건 충족 시 true |
| `PREPARING` | 그대로 보존 | lock 뒤에서 대기 | capture가 lock/in-flight를 보면 false |
| `SUBMITTING` | epoch 증가와 동시에 무효 | 불가 | false |
| `PUBLISHING` | 새 candidate도 아직 비공개 | 불가 | false |
| `RECOVERY_REQUIRED`/`RESTORING` | 구·신 모두 current 아님 | 불가 | false |

정상 commit 순서는 다음과 같다.

1. 공정한 owner lock 아래 `PREPARING`으로 전이한다. 기존 pointer/token/revision/healthy는 바꾸지
   않는다.
2. detached candidate에 typed plan과 policy-finalizer를 적용하고 full checkpoint encoding,
   domain·budget 검증, candidate projection/index 구성을 완료한다. 준비 중 await가 있더라도 구
   pointer는 훼손하지 않는다. producer 신규 view는 lock 뒤에서 기다린다.
3. 준비 실패·지원 거부면 candidate만 버리고 `READY`로 돌아간다. 6.4의 동일성·호출0 계약을
   만족한다. 단, 준비 중 별도 `_block()`이 발생했으면 epoch/health가 바뀌므로 구 pointer를
   복원하지 않고 `RECOVERY_REQUIRED`를 유지한다.
4. 모든 검증이 끝난 뒤 day/fence/base token/freshness 최종 guard를 다시 확인한다.
5. 실제 `store.commit` 제출 **직전**, 서로 사이에 무관한 await 없이 `publication_epoch`을
   증가시키고 current pointer를 무효화하여 `SUBMITTING`으로 전이한다.
6. 기존 full checkpoint와 commit ID/digest를 SQLite transaction에 commit한다. 알려진 rollback,
   결과 불명, lost acknowledgement, submission 뒤 caller cancellation이면 candidate를 공개하지
   않고 `RECOVERY_REQUIRED`다. old-current fallback도 없다.
7. durable 성공 뒤에도 준비 시 캡처한 fault generation/epoch가 예상값인지 다시 확인한다. store
   대기 중 `_block()`이 끼었으면 성공 candidate가 그 fault를 지우지 못하며, state/version은 복구
   대상으로 남기고 read model은 비공개인 `RECOVERY_REQUIRED`다. 일치할 때만 state/version과
   candidate read model을 private로 설치하고 `PUBLISHING`으로 전이한다.
8. publisher 성공 뒤에도 expected publication/fault epoch와 health를 마지막으로 동기 검사한다.
   일치할 때만 published version, owner token, projection/index pointer, runtime B binding을 하나의
   await 없는 publication transition으로 공개하고 `READY`가 된다.
9. publisher가 전·중·후 실패하면 durable candidate를 old/new 어느 pointer로도 공개하지 않고
   `RECOVERY_REQUIRED`다.

projection/index 불가를 빈 결과로 보지 않는다. 해당 상태에서 recovery producer의
release/reissue/submit writer 수는0이다.

### 7.2 restore·명시적 full build

- restore 진입은 먼저 publication/fault epoch를 모두 증가시켜 무효화하고 mutation/admission
  barrier를 잡은 채 종료까지 유지한다.
- policy registration receipt와 checkpoint를 기존 동일 SQL read snapshot에서 검증한다.
- projection이 없는 legacy checkpoint도 전체 durable 사실에서 **단 한 번** 재구축하며 빈0으로
  간주하지 않는다. mutable owner RAM을 worker thread에 넘기지 않는다.
- restore와 활성화 전 migration build만 full builder를 사용할 수 있다. 동시에 하나만 실행하고,
  later commit을 queue한다. latest-desired token 반복/coalescing/background backlog는 없다.
- builder 오류, stale completion, damaged receipt, cancellation은 unavailable이다. cooperative
  builder는 256 facts 또는5ms마다 yield하고 제품 연산의 연속 loop stall은50ms 이하다.
- owner publication 뒤 runtime day/fence/admission/binding generation 복원까지 끝나야 B를 결속하고
  current를 공개한다. incarnation/epoch/revision이 하나라도 다르면 결과를 폐기한다.
- successful restore의 전체 unavailable 구간은5,000건에서도1,000ms 예산 안이다. 실패·외부
  I/O fault에는 유한 진행 시간을 주장하지 않지만 stale 결과 공개는 계속 금지한다.

### 7.3 공정성과 producer 진행 보장

전체 async lock 순서는 기존 `producer gate → runtime-global quote gate → owner ticket gate`다. owner
안에서 호출하는 composition update는5.5의 await 없는 pointer swap뿐이다. composition update가
producer/quote/owner를 역으로 얻거나, 어떤 async lock을 잡은 채 owner에서 다시 producer/quote로
진입하는 경로는 금지한다.

`owner.acquire_producer_view()`는 commit과 같은 명시적 ticket-FIFO async gate를 사용한다. 취소된
ticket은 원자적으로 건너뛰고 남은 ticket의 상대 순서를 보존한다. PREPARING이나
restore 중 도착한 producer는 이미 앞선 작업 뒤에 줄서며, 그 뒤 도착한 commit writer보다 먼저
exact immutable view를 얻는다. view를 얻은 즉시 lock을 놓고 sweep한다. writer나 외부 await 뒤
owner 사실이 다시 필요하면 재획득한다.

각 ticket type은 gate를 실제 보유하는 전체 구간의 사전 등록 최대 `H_i`를 둔다. 한 apply ticket이
여러 commit을 수행하면 각 commit 예산이 아니라 그 ticket의 전체 hold time을 잰다. Q개 선행
ticket이 있으면 producer 획득 상한은 `sum(H_i for preceding tickets) + acquire/scheduler overhead`다.
queue에서 취소된 미시작 ticket은 원자 제거하지만 store에 제출된 active ticket의 취소는 기존
drain이 끝날 때까지 gate 소유권을 유지한다. fault 상태에는 유한 상한을 약속하지 않고
writer0/unavailable을 약속한다. 지속 commit 부하만으로 producer가 무한히 굶는 구현은 불합격이다.

성공 경로의 사전 등록 `H_i`는 no-commit/lookup ticket 1,000ms, single-commit owner ticket 1,500ms,
owner gate 안에서 최대 두 commit을 수행하는 fill `apply` ticket 2,500ms, restore/migration ticket 1,500ms,
producer-view critical section 5ms다. 각 commit과 restore 자체의11.2 1,000ms 예산은 별도로 더
엄격하게 유지한다. hold overrun은 gate 실패로 기록하되 이미 제출된 store 작업을 강제 취소해
결과 불명을 만들지는 않는다. 명시한 최대 commit 수를 넘는 ticket route는 활성화 불가다.

### 7.4 transient producer observation

producer RAM episode, failure, restart cooldown, lock, shutdown은 producer lock 안에서 epoch를 증가시키고
불변 observation pointer를 교체한다. recovery capture는 owner projection과 runtime observation을
5.5의 A-B-A-B로 읽어 identity/token/binding/lock이 앞뒤 모두 같을 때만 안정 표본으로 인정한다.

## 8. producer 의미 보존

`_sweep`은 시작 시 정확히 하나의 `ProducerReadView`를 얻고 `_attempts`, `_episode_status`,
`_audit_recovery`, `_restart_cooldowns`에 명시적으로 전달한다. owner 전체 상태를 다시 읽지 않는다.
unscoped historical-audit query는 제공하지 않으며 시작 집합은 현재 active recovery symbol index다.

반드시 보존할 fail-closed 의미:

- UNKNOWN, `blocked_unknown`, evidence conflict, observed != applied, terminal+reservation을 모두 유지.
- attempt index와 intent membership의 extra/orphan/duplicate/missing link는 inconsistent/block.
- audit symbol/intent/quantity, RAM episode, pending owner, admission이 한 종목으로 수렴하지 않으면
  `protection_decision_evidence_mismatch`.
- malformed/unknown/귀속 불가 audit symbol은 global block이며 조용히 제외하지 않음.
- late fill/reconcile/NOT_SENT/REJECTED는 새 revision에 즉시 반영하고 예전 status cache 재사용 금지.
- admission add/remove, pending add/release, audit 생성/변형은 모두 index update 대상.
- release/resume/submit 같은 writer 뒤 await 경계에서 새 revision view를 다시 획득·검증.
- producer가 만든 writer request는 근거 view의 exact `OwnerToken`을 첫 owner admission에 운반한다.
  첫 store effect 전에 이 token/day/fence가 current가 아니면 거부한다. 성공한 prepare/claim처럼
  그 operation 자체의 commit이 revision/publication epoch를 올리면 원 token은 소비되고 owner만
  다음 staged `WriterLease`를 발급한다.

```python
@dataclass(frozen=True)
class WriterLease:
    operation_id: str
    incarnation: str
    fault_epoch: int
    last_owner_token: OwnerToken
    day_generation: int
    fence_id: str
    dependency_fingerprint: str
    claim_id: str | None
```

lease는 최신 token을 다시 읽어 권한을 새로 만드는 수단이 아니다. **POST 이전** 다음
prepare/claim store stage와 broker guard는 같은 incarnation·fault epoch·day/fence,
operation/request binding, 현재 relevant attempt/admission/claim version과 fingerprint를 재검증한다.
자기 성공 commit은 owner 반환값으로 lease를 전진시키고, 무관 revision은 relevant dependency가
그대로일 때만 허용한다. relevant 변경, `_block()`, restore, day/fence 변경은 모두 새 송신 전에
거부한다. broker guard는 claim lease를 단 한 번 소비하며 durable prepare/admission과 claim 없이
POST할 수 없다. immutable old view만으로 송신 권한을 얻지 못한다.

durable claim 성공 직후, transport에 들어가기 **전에** owner는 send lease와 별개인 필수 결과-drain
authority를 등록한다. attempt/claim/expected-attempt-version/request binding과 이미 열린 command
scope에 묶인 one-shot `ResultDrainToken`으로 ACK/REJECTED/NOT_SENT/UNKNOWN 결과 저장을 강한
task에서 끝낸다. transport가 guard를 호출하지 않거나 거부한 경우, pre-guard cancellation,
kill-switch 거부, broker rejection도 이 token으로 기존 lifecycle evidence 규칙에 따라 기록한다.
POST guard는 send lease만 단 한 번 소비하며 drain token의 존재·소비와 독립이다.

결과 저장은 이미 발생했을 수 있는 외부 효과 또는 claimed attempt의 종결 증거이므로 POST 뒤
day/fence/closing 변화나 send lease 소비만으로 거부하지 않는다. drain token은 broker 호출·새
주문·lease 갱신 권한이 없고, wrong/reused claim/version은 거부한다. owner/store fault에서는 결과
저장을 성공으로 꾸미지 않고 기존처럼 owner를 block한 채 unknown/recovery-required로 남긴다.
caller cancellation도 submitted store drain과 result task의 강한 참조를 끊지 않는다.

목표 복잡도는 symbol/intent 조회 `O(관련 행 수)`, 전체 sweep `O(현재 관련 index 사실 수)`다.
retained history가 늘어도 같은 intent query가 전체 owner deepcopy를 수행하지 않아야 한다.

## 9. 단계적 이행과 활성화 경계

1. **N5-A — oracle·무효화:** pure full builder, token/invalidation, mutation-route inventory와 RED.
   제품 consumer는 아직 기존 경로를 사용한다.
2. **N5-B — typed mutation:** 단순 inbox/lifecycle부터 compound fill, quote/recovery, day,
   risk/regime callback까지 25개 writer와 여섯 commit route를 patch-only 계약으로 이행한다.
3. **N5-C — shadow parity·RAM 합성:** test/shadow에서 projection/index와 runtime B frame을 만들되
   capture/producer 의사결정에는 사용하지 않고 기존 N3 순수 classifier와 differential 비교한다.
   같은 owner revision에서 episode add/delete로 cross count가 바뀌는 계약도 여기서 고정한다.
4. **N5-D — producer/capture 전환:** exact-token view로 producer hot path와 N3 capture를 교체한다.
   product legacy route0, 정상 route full-builder 호출0, budget overflow store 호출0,
   unavailable writer0, stale full-scan fallback0이어야 한다.
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

- stale quote·duplicate admission·invalid plan·base mismatch·domain/encoding/builder 준비 실패가 기존
  pointer identity, token, epoch, revision, healthy를 보존하고 store/publisher/build 호출0.
- idle cached frame에서 command-result callback `_block()`이 revision 없이 epoch를 올리고 즉시
  noncurrent로 만듦. PREPARING 중 fault는 store 호출0/blocked 유지, store wait 중 fault는 성공
  candidate도 공개0이며 ordinary completion이 뒤늦게 health를 되살리지 못함.
- PREPARING을 멈춘 동안 기존 projection은 훼손되지 않지만 신규 producer view는 공정 lock에서 대기.
- SQL을 멈춘 동안 과거 owner/published/engine version이 같아도 구 projection current 금지.
- commit rollback, durable commit 뒤 응답 유실, caller cancellation, publisher 전/후 실패.
- 동일 revision restore, 같은 revision의 owner 교체, 늦게 완료된 구 rebuild 결과 거부.
- projection 없는 legacy checkpoint rebuild, 손상 policy receipt 계속 거부.
- 64/65 rows와256/257 edges: 활성 일반 route의 65/257은 store/full-builder 호출0, shadow의
  명시적 eligible route만 선행 full build 정확히1회.
- rebuild 중 producer를 enqueue하고 그 뒤 writer 여러 개를 enqueue해도 producer가 후속 writer보다
  먼저 exact view를 얻음. background/latest-token rebuild 수0.
- queued-before-start cancellation은 다음 ticket을 깨우고, submitted active cancellation은 store
  drain 뒤에만 ticket을 놓음. producer→quote→owner에서 시작한 commit publication도 교착0.
- producer submit의 prepare→claim→POST 자기 commit 두 번은 owner-issued lease로 정상 전진한다.
  무관 revision은 relevant fingerprint가 같을 때만 허용하고 relevant 변경, fault epoch, day/fence,
  wrong/reused claim lease는 POST 이전 claim/store 단계와 POST guard에서 거부한다.
- durable claim 직후 transport 전 result drain token이 발급된다. 정상 ACK, broker REJECTED,
  guard 거부·kill-switch의 NOT_SENT, pre/post-guard caller cancellation의 UNKNOWN이 send lease 소비
  여부와 무관하게 claim/version-bound drain으로 저장된다. closing/day-admission 변화도 이미 claim한
  결과 저장을 막지 않는다. wrong/reused drain token은 거부되고 drain token으로 추가 POST는 불가하다.
- BUY/SELL 부분·완전체결, UNKNOWN, unapplied observation, 잔여 cash/exposure/risk reservation.
- cancel child와 parent finality, rejected order, uncertain cancel, day rollover.
- 같은 symbol 다중 intent/episode, insertion order 역전, audit/admission/pending/RAM mismatch.
- 같은 durable revision에서 orphan audit의 matching episode add/delete가
  `protection_unsubmitted`을 `+1→0→+1`로 바꾸며 owner A는 동일하고 B epoch만 변함.
- audit quantity int1과 episode int1은 match. episode 1.0/True/Decimal('1'), episode decision list,
  잘못된 길이·intent·symbol은 강제변환 없이 unavailable+evidence_invalid이고 counts_complete false.
- 같은 intent의 wrong symbol/decision은 audit/episode pair당 link mismatch1건, 여러 audit·episode의
  multiplicity는 기존 pair-loop와 정확히 같음.
- pending fallback은 same-symbol/same-intent episode만 해결하며 같은 intent 다른 symbol과 같은
  symbol 다른 intent는 해결하지 않음.
- degraded/runtime-failure/recovery-required/invariant가 같은 사고를 가리켜도 repair count는 모두
  가산되고, 같은 runtime key overwrite는 cardinality를 늘리지 않음.
- episode add→delete ABA 또는 binding/runtime generation 변화가 A-B-A-B 사이에 끼면
  `snapshot_volatile`만 반환하고 quiet retry에서 새 정확 count를 반환.
- malformed/orphan/duplicate/missing link를 제거하지 않고 incomplete/block로 보존.
- consumer가 반환된 DTO를 변형해도 owner/index 불변.
- full checkpoint/reopen/journal dedup/economics 결과가 변경 전과 동일.
- owner commit이 없는 producer episode/failure/binding/handler/reconciler 변화도 stale 표본 거부.
- 5,000 event에서 N3 capture cap 실패는 그대로 확인하되 test-only uncapped pure oracle과 N5 full
  builder의 finding code/count를 비교하고, episode 합성·capture에서 owner.state/history iteration0.

### 10.1 parity와 공개 완전성의 분리

**순수 semantic parity**는 동일하게 검증된 불변 논리 사실을 입력해 기존 `_classify`와 다음 finding의
code/count를 정확히 비교한다: inbox/observation/unknown BUY, reservation/cancel/attempt-link,
모든 protection finding, `repair_only`, pending SELL, row-level `evidence_invalid`. 고정
`disposition_not_durable=None`도 보존한다. availability/control finding인
`snapshot_unavailable`, `snapshot_volatile`, `publication_inconsistent`,
`owner_health_unconfirmed`, mode/token/currentness/stability와 공개 `counts_complete`는 이 비교에서
제외한다.

N5 공개 `counts_complete`는 다음을 모두 만족할 때만 true다.

```text
sample_stable
and not mutation_in_flight
and read_model_current
and publication_consistent
and owner_projection.complete
and runtime_observation.complete
and runtime_generation_matches
and producer_epoch_and_bindings_match
and no evidence_invalid in final_counts
```

따라서 N3가 publisher failure 뒤 `counts_complete=True`였던 경우 N5는 의도적으로 false이고,
N3 wrapper가5,000건 cap으로 unavailable인 경우 N5 restore가 완전하면 의도적으로 true일 수 있다.
owner/RAM 어느 쪽이 incomplete이거나 최종 합성 finding에 분류된 `evidence_invalid`가 하나라도 있으면
항상 false다. complete가 false이면 부재 code는 unknown이지0이 아니다.

| 사건 | current/readable | publication | `counts_complete` |
|---|---|---|---:|
| healthy steady | exact token만 yes | consistent | 위 전체 predicate 충족 시 true |
| prevalidation reject | 기존 pointer 그대로 | unchanged | 기존 값 그대로 |
| PREPARING/lock observed | diagnostic old frame만 가능, producer 대기 | unchanged | false |
| SUBMITTING/PUBLISHING | no | blocked/unconfirmed | false |
| rollback/unknown/cancel/publisher failure | no old/new fallback | inconsistent | false |
| restore 및 owner-published/runtime-incomplete | no | 일부만 완료 | false |
| restore와 runtime generation 모두 완료 | 새 token만 yes | consistent | 위 전체 predicate 충족 시 true |
| A-B-A-B epoch/binding 변화 | no combined sample | owner는 consistent일 수 있음 | false |

단순 line coverage 대신 다음 mutation kill이 각각 한 RED를 실패시켜야 한다: root별 index update 제거,
revision ±1, prevalidation 전에 epoch 증가, publish 실패 때 old cache 유지, missing projection→empty,
producer epoch/binding 검사 제거, RAM cross-count update 제거, dict 순서로 최신 추정,
UNKNOWN/예약/unknown risk→0.

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

`projection-update`와 `commit-control/indexed` 셀의 실제 edit workload는 다음 세 subtrial로
사전 고정한다. 각 scale cohort 위에 constant-size `benchmark_seed`를 setup에서 얹고 raw 결과에
seed 행 수를 별도로 써서 역사 규모와 섞지 않는다.

1. `lifecycle_ack`: 기존 claimed SELL submit 한 건에 정상 ACK/order ref를 기록하는 실제 lifecycle API.
2. `protection_quote_roundtrip`: 기존 live SELL intent 한 건에 실제 `runtime.quote()`를 호출해
   admission commit과 protection/outbox 계산·admission 제거 completion commit까지 모두 수행한다.
   두 commit 사이의 제품 연산도 operation wall timer에 포함한다.
3. `partial_buy_fill`: setup에 이미 RECEIVED인 BUY observation의 누적수량을1 증가시켜 attempt,
   reservation, portfolio/lot/risk/economics를 바꾸는 실제 apply API. timed 구간에는 inbox 선행 commit이
   들어가지 않고 fill commit 한 번만 들어간다.

control과 indexed는 같은 base checkpoint clone, 고정 clock/ID/input, 같은 public API를 사용한다.
indexed flag 외에는 차이가 없어야 하고, 종료 후 version을 포함한 encoded checkpoint digest,
publisher 호출 수·payload, domain result가 정확히 같아야 해당 pair를 latency 비교에 쓸 수 있다.
세 workload 각각 ABBA20쌍을 기록하며, 쉬운 scalar/no-op edit로 대체할 수 없다.

### 11.2 예산

| 구간 | 합격 예산 |
|---|---:|
| 정상 동기 projection/index update | 모든 규모 trial 최대10ms |
| end-to-end commit wall | 모든 규모 최대1,000ms |
| 두 commit quote roundtrip / fill apply operation wall | 최대2,500ms |
| 성공 commit의 SUBMITTING→READY unavailable 구간 | 최대1,000ms |
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

각 셀은 별도 `env -i` pytest 프로세스, 외부 timeout30초, 동시 실행0으로 수행한다. **timer 밖인
것은 synthetic fixture/template 생성, test reset용 copy, N3 test-only oracle 계산·비교,
assert/report와 별도 tracemalloc run뿐이다.** 제품 코드가 수행하는 candidate/full-state copy,
detached payload copy, typed edit/delta, domain·policy 검증, projection/index 할당, checkpoint encoding,
store wait, publisher, restore load/decode/build/publication, runtime generation 복원, producer view 획득,
진단 copy/build/JSON은 모두 해당 제품 구간 timer 안이다. “deepcopy 밖”이라는 표현으로 제품 copy를
제외할 수 없다. control/indexed를 ABBA 순서로20쌍 측정한다. normal latency와 tracemalloc run을
분리한다.

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
2. 모든 cohort에서10.1의 N3 순수 semantic oracle parity와 mutation kill 통과.
3. product legacy mutation0, 일반 거래 route full-builder 호출0, budget 초과 store/publisher 호출0.
4. commit/restore/publish/cancellation 모든 경계 fail-closed, stale-current0, producer starvation0.
5. serial 성능 matrix 전 셀 완료, 모든 예산 충족, unresolved timeout/noisy0.
6. focused tests 후 전체 suite를 UTC→KST 직렬 실행, 기존 xfail만 허용.
7. 비밀정보·격리 검사 통과.
8. 작성자가 아닌 Astra/xhigh 또는 검증된 Claude Opus의 source/diff/raw 측정 독립 리뷰.

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
- revised architecture critical review: 요청 `gpt-6-astra/xhigh`; 네 차례 REVISE에서 발견한
  prevalidation/fault invalidation, rebuild starvation, durable+RAM 합성, parity/completeness,
  staged send lease/result drain 경계를 모두 반영한 뒤 최종 `APPROVE`(blocking0, advisory0).
  실제 model/effort metadata는 미노출이라 미검증.

모든 분석·검토는 같은 base 계열에서 read-only로 수행했고 구현·자가 승인·운영 조작을 하지 않았다.
향후 구현은 파일 소유권을 나눈 격리 worktree와 Plan→Do→See를 사용하며, critical 변경 작성자는
자기 변경을 최종 승인하지 않는다.
