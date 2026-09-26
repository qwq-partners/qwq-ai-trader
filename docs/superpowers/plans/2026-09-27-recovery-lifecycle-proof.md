# N5 L3-P0 Source Lease·Managed Sequence Proof Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** 운영에 연결하지 않는 두 시험용 시제품으로 입력 소유권 인계와 bounded sequence의
취소/정리 가능성을 검증하고, 전체 L3 구현을 시작할 수 있는 범위와 남은 차단을 판정한다.

**Architecture:** 기존 SQLite store와 FIFO gate는 수정하지 않는다. 시험 전용 source transport가
이미 열린 합성 DB의 원문 TEXT와 등록 영수증을 같은 snapshot에서 읽고, 제출 전에 확보한 slot에
소유권을 기록한다. 별도의 sequence 시제품은 private node·iterator·lifetime pin을 관리해
완성됐지만 미인계한 결과의 취소와 원자적 소유권 이동을 검증한다. 두 시제품은 서로 연결하지 않는다.

**Tech Stack:** Python3.12, 기존 asyncio/SQLite executor/pytest, stdlib만 사용. 새 dependency 없음.

**Spec:** `docs/superpowers/specs/2026-09-27-recovery-build-lifecycle-design.md`
(검토본 SHA256 `c9332cf2be7feed744ad9e59a4d9b31c05215e5cedb2a31221aaac2a9f09a58f`).
기존 N5 §7.2/7.3/11.2도 함께 읽는다.

**Status:** 사용자의 서면 제안 확인 후 작성한 **P0 선행 검증 계획**이다. 이 계획의 사용자 검토는
아직 받지 않았으며 시제품/시험을 실행하지 않았다. 구현 방법은 이미 선택한 역할별 하위 에이전트
Plan→Do→See를 유지한다. 전체 L3 구현계획이나 운영 전환 계획이 아니다.

## Global Constraints

- 안전한 개발 기준은 `b1ee29076e00e1070d3c990880c4752a763740a8`의 승인 Task1+Task3 문서 branch다.
  이 계획의 docs-only 커밋까지 포함한 SHA를 실행 ledger에 확정하고 그 동일 SHA에서 두 worktree를
  만든다. Task2의 `038aa599`+미커밋 diff`721fe1c7…`를 복사/cherry-pick/merge하지 않는다.
- 현재 문서 작업트리는 `feature/owner-ticket-gate-20260926`이며 제품 구현용으로 겸용하지 않는다.
  실행 시 각각 `feature/l3-source-proof-20260927`, `feature/l3-sequence-proof-20260927`를 사용한다.
- 시제품 파일은 모두 `tests/` 아래다. `src/`, 기존 시험, store schema, 설정은 수정하지 않는다.
  운영 import/consumer0, 주문/전략/위험/보존 정책 변경0, main/배포/재시작0이다.
- 기존 `256 facts 또는5ms`, raw50ms, 성공 restore1,000ms/ticket1,500ms는 유지한다. 이 계획은
  latency 합격을 측정하지 않으며 구조 work counter를 wall-time 증거로 사용하지 않는다.
- 새 disposal 계수는 **한 번 제거한 실제 strong reference edge마다1**이다. node/registry/root/
  iterator의 참조를 모두 포함하고, native container를 한꺼번에 버릴 때 내부에서 제거될 edge도
  사전에 포함한다. public `advance_disposal(256)`의 charged edges는 최대256이다.
  직접 관리하는 owning field/container slot·pin·cursor·명시 scratch가 증명 도메인이다.
  이들을 보유하는 frame/local도 숨기지 않는다. 모든 Python interpreter 임시 ref를 계측했다거나
  native allocator를 선점한다고 주장하지 않는다. 도메인 밖의 참조가 실제 graph 해제량/마지막
  참조 수명에 영향을 주면 그 경계를 증명에 넣거나 PROOF_FAIL/미확정으로 중단한다.
- fan-out32는 sequence node에 적용한다. 모든 기록·registry·cursor stack도 bounded block으로
  유지한다. 집계값만256으로 표시하거나 거대한 container 하나를1작업으로 세지 않는다.
- receipt page는 최대64 SQL row, row는 `(command_id, version)` 두 원소다. 원래 행을 누락하거나
  상한 이후 잘라내지 않는다. prefix와 `0 < receipt_version <= checkpoint_version` 검증을 유지한다.
- managed SQL source는 provenance capability로만 받는다. 임의 raw dict/list/marshal capsule,
  사용자 clock/hook/callback·임의 Sequence는 받아들이지 않는다. 오류에 repr/str를 호출하지 않는다.
- 기존 synchronous builder/close/tuple/JSON/exception 의미를 바꾸지 않는다. warm source 증명에서
  cold `_open`/factory 선행 load/JSON decode를 검증한 것으로 보고하지 않는다.
- GC disable/threshold/freeze/수집, 추가 thread/process로 제품 해제 이관, permanent 정상 pin,
  history 삭제·입력 축소·false completion을 해법으로 쓰지 않는다. 기존 SQLite worker는 그대로다.
  기존 회귀 하네스의 사전 등록된 controlled-GC 시험은 수정하지 않으며 제품 GC 정책으로 적용하지 않는다.
- 요청/실제 모델을 구분한다. coordinator+최대3workers, fan-out0. 구현Astra/high, 독립 critical
  리뷰는 다른 Astra/xhigh; 자료 대조 Sol/high, 기계적 문서 정리 Luna/medium. 공급자 간 리뷰 아님.
- worker당45분, 한 명은 한 task/file allowlist만 담당한다. 시험은 coordinator에게 측정 슬롯을
  받아 직렬 실행한다. 같은 입력 실패를 통과할 때까지 재실행하지 않는다.
  집중 명령은 각180초 외부 timeout, 전체 suite는 각900초다. timeout은 실패/미확정이지 통과가 아니다.

## Review Focus

1. worker가 반환했지만 caller가 취소된 창에서 source가 owner 없이 버려지지 않는가 — Task1의
   `done_uncollected`와 `caller_last_reference` 시험.
2. 같은 DB의 다른 connection이 checkpoint와 receipt 사이에 commit해도 한 snapshot인가 —
   Task1의 실제 WAL connection 대조 시험. 이 대조는 다른 운영 writer를 허용하는 정책이 아니다.
3. completed-unclaimed 마지막 지점에서 취소해도 root 마지막 참조 하나로 전체가 해제되지 않는가 —
   Task2의 `sealed_before_take`와 pin 소유권 시험.
4. iterator/traceback가 큰 graph를 보유할 때 값은 없어 보여도 false cleanup을 내지 않는가 —
   Task2의 iterator/foreign error, Task1의 foreign fault negative subprocess.
5. exact tuple→arbitrary Sequence 변경이나 invalid facts 누락으로 빨라진 것으로 오인하지 않는가 —
   Task2의 hostile input 거부와 §호환성 인계표. 기존 consumer는 변경하지 않는다.

---

## 이번 계획이 닫지 않는 계약

| 원 설계 영역 | 이번 산출물 | 별도 선행/후속 gate |
| --- | --- | --- |
| source lease | warm SQL 원문/receipt 소유권 증명 | cold `_open`, factory 선행 load, allocation-time JSON decode |
| managed lifecycle | source의 제출/취소/정리 및 sequence의 후보/인계 | 전체 builder phases, policy 임시 graph, owner publish |
| 정리 증거 | source 전용 private cleanup Future | 실제 restore/runtime 전체 cleanup certificate |
| bounded graph | sequence nodes/pins/owned iterator | map/DTO/unknown nested graph 및 parser/validator scratch |
| 소비자 호환 | 현행 tuple/serialization 의존성 목록, consumer0 | 타입/정렬/identity/invalid semantics의 실제 이행 |
| 성능/운영 | 실행 시간/timeout은 시험 진행 정보만 | 현재13.023871ms FAIL,6UNRUN, 원래63셀·UTC/KST·C/F/G/R·broad gate |

원래 Task4 이후 통합은 계속 차단이다. 두 시제품이 GREEN이어도 source decoder를 뒤늦게 arena에
등록하는 구현, 큰 tuple 반환 facade, 즉시 운영 연결로 넘어가지 않는다.

## File Structure / ownership

Task1 전용:

- Create `tests/l3_source_lease_probe.py` — 실제 store의 기존 executor를 사용하는 **시험 전용**
  warm SQL reader, source slot/page 수명, private cleanup proof.
- Create `tests/test_l3_source_lease_probe.py` — 실제 임시 SQLite·취소·snapshot·fault 경계.

Task2 전용:

- Create `tests/l3_sequence_probe.py` — 시험 전용 arena/32-way sequence/candidate/transferred bundle.
- Create `tests/test_l3_sequence_probe.py` — edge/alias/iterator/transfer/fault 구조 시험.

coordinator 전용:

- Create `docs/reviews/l3-proof-results-2026-09-27.md` — 실제 RED/GREEN/실패/미실행/모델/범위 기록.
- Modify `docs/reviews/recovery-projection-progress-2026-09-27.md`, `CHANGELOG.md`, `CLAUDE.md`,
  `docs/README.md` — 결과가 실제 나온 뒤만 현재 상태 갱신.

위 이름은 **새 시제품 API**이며 제품에 이미 존재한다고 주장하지 않는다. tests 경로의 helper
import는 기존 `recovery_scale_harness` helper처럼 pytest의 tests 경로를 사용한다.

## Task 1: warm SQL source의 제출 전 소유권과 취소 후 정리

**Files:** Task1 전용 두 파일. `store.py`, `application.py`, `factory.py`, gate는 read-only.

**Interfaces:**

- `ProbeSupervisor()`는 active source slot 하나를 강하게 보유한다. 이 slot은 공개 handle이나
  driver가 사라져도 남으며 실제 정리 완료 후에만 비워진다.
- `start_source_probe(supervisor, store, gate, ticket, token, controls) -> SourceProbeHandle`은 동기다.
  warm connection·store identity·active ticket을 확인하고 slot/private proof를 등록한 뒤 SQL을
  제출한다. 제출과 Future 등록 사이 await0. 실행 전 cold store는 `ValueError("cold_source_unsupported")`,
  이미 active이면 `ValueError("source_already_active")`로 graph/SQL 제출0을 보장한다.
- `SourceProbeHandle.wait_source()`는 shield된 관측 await다. 반환값 없음. READY 또는 명시된 고정
  오류 코드만 관측한다. 외부 취소는 latch를 설정하고 SQL/driver를 cancel하지 않는다.
- `SourceProbeHandle.request_cancel()`은 동기·멱등이며 graph를 해제하지 않는다.
- `SourceProbeHandle.wait_closed()`는 cleanup proof 관측이다. 반복 취소가 proof를 cancel하지 않는다.
- `SourceProbeHandle.validate_source(store, token) -> None`은 SOURCE_READY에서만 가능하다.
  등록된 store/connection identity와 token 네 축, SQL revision을 비교한다. 불일치하면
  `ValueError("stale_source")`와 cancel latch, 정리로 전이하며 source를 반환하지 않는다.
  이 메서드는 인계/공개 API가 아니며 동일 source의 반복 검증은 허용한다. CLEANED/FAULT handle
  재사용은 `ValueError("source_unavailable")`다. 실제 owner current와의 연결은 이 시험 밖이다.
- `SourceProbeHandle.status -> str`: `EMPTY/SUBMITTED/SOURCE_READY/DISPOSING/CLEANED/FAULT`.
- `SourceProbeHandle.advance_disposal(max_edges: int) -> int`: worker terminal 뒤만 호출 가능;
  정리 driver 단일 호출자, 실제 제거 edge 수 반환. budget은 exact int4..256만 허용한다.
  source row의 native tuple2와 제거/임시 보유 참조를 포함한 불가분 단위 때문에 최소4다.
  bool/0/1/2/3/257은 상태 변경 전 ValueError다. 지원 budget에서는 pending graph가 있는
  정상 advance마다 실제 진전해야 하며, 미리 청구하거나 작은 budget을 무한 재시도하지 않는다.
- 시험 전용 `inspect_source(handle) -> (int, str, tuple[tuple[str,int],...])`는 assertions/oracle에서만
  호출한다. 큰 tuple 사본은 **시험 도구**이며 제품/source 경로에서 사용하지 않는다. 인수 timer 없음.
  소유권/정리 시험은 이 복사본을 먼저 버리고 시행하며 복사본이 keepalive가 되지 않는지도 검사한다.
- `SourceProbeControls`: `pause_at` enum(`none/after_checkpoint/page/done_uncollected/before_driver`),
  `entered: threading.Event`, `release: threading.Event`, `fault` enum
  (`none/known_sql/foreign/submit/driver_create/driver_prestart_cancel`).
  일반 callable은 받지 않는다. 이러한 제어점은 시험 코드이며 production API 제안이 아니다.
  rollback 실패는 `rollback_fail: bool`로 별도 주입한다. loop 측 제어점은 Event를 blocking wait하지
  않고 예약된 observer로 확인한다. SQL worker에서만 threading.Event.wait의 유한 timeout을 쓴다.
  `mutation` enum(`none/skip_proof_registration/premature_proof_completion`)은 negative child만
  선택하며 일반 정상시험에서는 none이다.

### Plan / 고정할 내부 소유권

slot은 store instance + connection lifetime nonce + 시작 token의 incarnation/publication/fault
epoch를 결속한다. SQL SELECT 결과 revision을 한 번만 붙인다. 소비 시 요구 token 네 필드와
store/connection identity가 정확히 같은지 확인한다. stale이면 공개0·취소 latch·정리로 이동한다.
아직 실제 owner epoch writer를 배선한 것은 아니다.

worker가 쓰는 것은 새 source 전용 slot의 worker 영역뿐이다. 기존 owner RAM을 넘기지 않는다.
loop driver는 worker 종료 전에 그 영역을 읽거나 정리하지 않는다. 취소 latch와 제어 신호는
별도 영역이다. Future result는 `None`이며 큰 graph를 다시 담지 않는다.

SQL 순서는 **한 executor callable**에서 아래처럼 고정한다. `slot`과 page shell은 해당
allocation 전에 supervisor에 등록돼 있어야 한다. 현재 `_open/_load`를 호출하면 안 된다.

```python
conn.execute("BEGIN")
try:
    row = conn.execute("SELECT version, state FROM checkpoint WHERE id=1").fetchone()
    if row is None or type(row[0]) is not int or row[0] < 0 or type(row[1]) is not str:
        raise ValueError("invalid_source_row")
    slot.bind_encoded_row(row[0], row[1])
    cursor = conn.execute(
        "SELECT commit_id, version FROM commits WHERE substr(commit_id, 1, ?) = ?",
        (len("policy-registration:"), "policy-registration:"),
    )
    while True:
        page = slot.register_empty_page()
        rows = cursor.fetchmany(64)
        page.take_rows(rows)  # page/rows/SQL row tuple의 실제 edge를 모두 소유
        if not rows:
            break
        page.validate_receipt_versions(slot.revision)
    conn.execute("COMMIT")
finally:
    if conn.in_transaction:
        conn.execute("ROLLBACK")
```

`bind_encoded_row`, `register_empty_page`, `take_rows`, `validate_receipt_versions`는 helper 내부
메서드다. bind는 작은 두 필드 이동, register는 bounded registry에 shell1개 등록, take는 최대64
행 list의 일회 인계, validate는 prefix를 제거한 ID와 exact int version 검증이며 전 행을 보존한다.
page chain과 registry는 고정 크기 link/block으로 만들고 fetchall/거대 receipts dict를 만들지 않는다.
slot 정리에서는 row tuple2개 edge, list/page/root/registry edge를 모두 charge한다.

예상된 SQL 오류도 worker frame에 자원을 남길 수 있으므로 오류 상태와 실제 정리 증거를 분리한다.
허용된 고정 오류는 source-owned 자원만 남는지 확인하고 정리한다. 외부 graph-bearing 예외는
raw repr 없이 slot의 FAULT에 보유하며 proof pending이다. rollback 실패는 성공으로 덮지 않는다.
submit 실패도 부분 slot이 있으면 정리 후에만 proof를 성공시킨다.

`ProbeSupervisor`는 생성 시 loop를 고정하고 `observe_driver_done(slot, task)`를 task에 등록한다.
slot에는 `driver_started=False`를 먼저 기록하고 driver의 첫 opcode에서만 True로 바꾼다.
`driver_create`는 task 생성 전에 고정 오류를 내며 supervisor가 미예약 coroutine을 닫고 같은
slot에 `driver_start_failed` FAULT를 남긴다. `driver_prestart_cancel`은 시험만 private task를
첫 실행 전에 cancel하고 done observer가 `driver_never_started` FAULT로 만든다. 정상 caller
취소는 driver를 cancel하지 않는다. 미시작/생성 실패에서 worker Future는 계속 slot에 보유하며
종료도 관측하지만 새 driver·retry는 만들지 않는다. 정리 증명은 pending, gate는 잠긴 상태다.
supervisor/loop의 감독 자체가 실패하면 역시 진전 불능이며 정상 cleanup으로 보고하지 않는다.

- [ ] **Step 1 — RED: source shell/proof가 없는 현재 상태에서 새 시험을 작성한다.**

```python
import asyncio
import pytest
from src.execution.safety.store import ExecutionStateStore
from src.execution.safety.owner_ticket_gate import OwnerTicketGate, OwnerTicketKind
from src.execution.safety.recovery_projection import OwnerToken
from l3_source_lease_probe import (
    ProbeSupervisor, SourceProbeControls, start_source_probe, inspect_source,
)

def test_source_receipts_and_cancel_cleanup(tmp_path):
    async def scenario():
        store = ExecutionStateStore(tmp_path / "source.sqlite3")
        await store.commit(0, {"unknown": [1, True, "한글"]}, "policy-registration:r1")
        gate = OwnerTicketGate(asyncio.Lock())
        supervisor = ProbeSupervisor()
        try:
            async with gate.hold(OwnerTicketKind.RESTORE) as ticket:
                handle = start_source_probe(
                    supervisor, store, gate, ticket, OwnerToken("probe", 1, 1, 1),
                    SourceProbeControls(),
                )
                await handle.wait_source()
                version, text, receipts = inspect_source(handle)
                assert version == 1 and receipts == (("r1", 1),)
                assert '"unknown"' in text
                del text, receipts
                handle.request_cancel()
                await handle.wait_closed()
                assert handle.status == "CLEANED"
            assert supervisor.active is None
        finally:
            await store.close()
    asyncio.run(scenario())
```

같은 파일에 parameterized phase matrix를 추가한다. 각 phase는 control Event로 정확히 멈추며
`sleep(0)`은 순서를 보증하는 수단이 아니다. cancellation은 두 번 요청하고 그 사이 다음 producer가
실제 gate에 queued됐다는 Event를 확인한다. release 전 producer 진입0, SQL Future.cancelled false,
supervisor.active identity 동일; release 후 모든 source edge 제거 뒤 producer가 진입해야 한다.

| 추가 입력/사건 | 고정 단언 |
| --- | --- |
| cold store / wrong store / reused handle / stale token 각각4축 | SQL 제출0 또는 해당 slot 미공개; 기존 state/identity 불변 |
| submit / before_driver / after_checkpoint / page / done_uncollected / caller_last_reference | source 인계 유실0, driver 미시작 finally에만 의존0, 정리 전 gate 반환0 |
| receipt0/1/64/65/4097개, 마지막 빈 page; budget4/5/31/32/255/256 | 모든 영수증·version 보존, page≤64, 각 disposal≤budget, 실제 진전·최종 live owned edge0 |
| 다른 WAL connection이 중간에 checkpoint+receipt 교체 | 최초 SELECT의 old version/text/receipt pair 유지; 다음 별도 read만 new pair |
| known SQL / rollback failure / foreign exception | 최초 원인 보존, false CLEANED0; foreign/미인증 fault는 proof pending |

foreign/driver fault는 fail-closed라 정상 gate 종료를 기다리면 영원히 대기할 수 있다.
억지 proof 완료 대신 아래 **source fault child 프로토콜**을 사용한다.

1. 부모 pytest가 clean-env의 child1개를 시작하고 child는 실제 gate의 owner ticket을 획득한다.
2. follower가 queue에 실제 삽입됐음을 시험 전용 gate 관측기로 확인한다. 메서드 호출 직전
   Event만으로 대신하지 않는다. observer는 기존 gate를 수정하지 않고 private queue를 읽는다.
3. fault/worker terminal을 확인한 owner가 context exit를 시작한다. gate의 실제 drain 진입은
   `register_submitted_drain`에 등록된 private proof가 pending인 채 `hold().__aexit__`의 drain
   대기에 들어갔는지 관측한다. source helper가 임의로 보고한 bool만 사용하지 않는다.
4. drain 진입 뒤 sentinel follower와 종료 observer를 loop에 예약해 각각 한 번 실행 기회를
   준다. observer는 lock held, 같은 slot, proof pending, follower not entered를 읽는다.
   owner가 먼저 끝났거나 drain에 들어가지 않았으면 즉시 실패한다. sleep 시간으로 순서를 추측하지 않는다.
5. child는 다음 고정 JSON 한 줄을 flush한다. 부모는 정확한 필드와 값, child 생존을 확인한
   뒤 terminate하고 유한 wait한다. 정상 cleanup/latency PASS로 보고하지 않는다.

```json
{"case":"source_fault","fault":true,"worker_settled":true,"follower_queued":true,"exit_drain_observed":true,"post_fault_turn":true,"same_slot":true,"lock_held":true,"cleanup_completed":false,"follower_entered":false}
```

부모의 시작→관측→종료 전체는 monotonic10초 deadline1개다. JSON 누락/중복/timeout/종료 실패는
시험 실패, 재시도0. `skip_proof_registration`·`premature_proof_completion` 두 시험 변이는 각각
이 프로토콜에서 반드시 실패해야 한다. 변이 선택은 test helper 내부 fixed enum이며 사용자 API가 아니다.

- [ ] **Step 2 — RED 명령 실행; helper import 부재 또는 실제 소유권 단언 실패를 기록한다.**

```bash
timeout --signal=TERM 180s env -i PATH=/usr/bin:/bin LANG=C.UTF-8 TZ=UTC PYTHONDONTWRITEBYTECODE=1 \
  PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 /home/ubuntu/projects/qwq-ai-trader/venv/bin/python \
  -m pytest tests/test_l3_source_lease_probe.py -q -p no:cacheprovider --tb=short
```

- [ ] **Step 3 — helper 구현.** 위 SQL seam과 slot 메서드를 작성하고, `run_in_executor` 직전에
  등록된 private proof를 gate에 넘긴다. `supervisor.active = slot`은 driver 생성보다 먼저다.
  단일 정리 driver는 SQL terminal을 shield해 기다리고 cancel latch를 관측한 경우 아래 순서로만 정리한다.

```python
while not slot.source_cleaned:
    removed = slot.advance_disposal(256)
    if not 0 <= removed <= 256:
        slot.mark_fault("invalid_disposal_accounting")
        return
    await asyncio.sleep(0)
if (slot.worker_settled and slot.transaction_settled
        and slot.owned_edges == 0 and slot.disposal_fault is None):
    slot.cleanup_proof.set_result(None)
    supervisor.clear_if_same(slot)
```

`owned_edges`는 source graph를 실제로 등록/제거한 내부 counter와 독립 test observer로 대조한다.
counter0만 보고 proof를 내지 않는다. `first_error`는 known SQL/submit 등 최초 원인을 고정 코드로
보존하고 `disposal_fault`와 구분한다. 알려진 source 실패는 transaction 종료와 실제 자원 정리가
증명되면 proof 성공이 가능하다. foreign 예외·rollback 실패·driver 진전 불능은 disposal_fault이며
proof를 완료하지 않는다. CLEANED 뒤 cancel은 no-op이다.
`source_cleaned`는 encoded cell/page registry/cursor/worker
handoff 참조까지 비었음을 의미한다. FAULT에서는 이 loop에 재진입하거나 새 driver를 만들지 않는다.

- [ ] **Step 4 — 같은 집중 명령으로 GREEN, 다음 기존 회귀와 함께 독립 review를 받는다.**

```bash
timeout --signal=TERM 180s env -i PATH=/usr/bin:/bin LANG=C.UTF-8 TZ=UTC PYTHONDONTWRITEBYTECODE=1 \
  PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 /home/ubuntu/projects/qwq-ai-trader/venv/bin/python \
  -m pytest tests/test_l3_source_lease_probe.py tests/test_execution_state_store.py \
  tests/test_execution_owner_ticket_gate.py tests/test_execution_policy_registration_boundaries.py \
  -q -p no:cacheprovider --tb=short --show-capture=no
```

- [ ] **Step 5 — 명시 실패는 원인+범위만 보고하고 중단.** cold-open/JSON decode를 추가 구현하거나
  `store._open` 검증을 생략해 warm-only를 full restore로 바꾸지 않는다. GREEN이면 한정 리뷰와
  아래 통합 검증을 통과한 뒤 두 literal path만 커밋한다.

## Task 2: 관리형 sequence 후보·정리·인계 시제품

**Files:** Task2 전용 두 파일. FrozenJSON/DTO/validator/serializer와 기존 시험은 read-only.

**Interfaces:**

- `SeqArena()`는 private sequence 하나와 registry/pins/owned iterators를 보유한다.
- 선택적 `SeqArena(controls=SeqProbeControls())`를 허용한다. controls는 `stage` enum
  (`none/before_append/mid_leaf/grow/seal_each_level/sealed_before_take/take_preallocation/disposal`),
  `fault` enum(`none/known/foreign`), `hit: int`(1 이상)의 고정 데이터만 받는다. 지정 stage의
  hit번째 방문에서만 오류를 주입한다. public callback/clock은 받지 않는다.
- `append(value)`는 exact `str/int/float/bool/None` 또는 같은 arena가 발급한 `OwnedSeqRef`만
  받는다. nonfinite float, subclass, arbitrary Sequence, 다른 arena 참조는 callback 없이 거부한다.
  그 입력 거부는 시제품 도메인일 뿐 실제 checkpoint/invalid history를 제거하는 정책이 아니다.
- `new_child(values: tuple) -> OwnedSeqRef`는 exact tuple 최대32개를 받아 등록된 child를 만든다.
  각 값은 append와 같은 exact scalar 또는 같은 arena의 기존 OwnedSeqRef다. empty도 허용한다.
  한 child 제한은 node fan-out이며 전체 sequence 크기 제한이 아니다. 소유권은 arena에 남고
  반환 ref는 weak capability여서 graph를 별도로 pin하지 않는다. 재사용하면 같은 child를 공유한다.
  public API는 cycle을 만들지 않는다. cycle 인수는 내부 fixture에서만 별도로 구성한다.
- `begin_seal()` + `advance_seal(max_edges: int) -> int`: 큰 root를 한 호출에 봉인하지 않는다.
  node≤32, budget exact int1..256; 완료 후 `arena.candidate`만 접근 가능하다.
- `candidate[index: int]`, `len(candidate)`, `candidate.open_cursor()`를 지원한다. slice/hash/equality/
  tuple/list/JSON 변환은 제공하지 않는다. `__iter__`는 항상 TypeError("use_owned_cursor")라서
  builtin tuple/list의 __getitem__ fallback도 차단한다. exact int만 index로 받고 negative index를 정규화한다.
- `SeqCursor.next_item() -> object` / `close()`: generator 대신 명시 cursor. 끝은 StopIteration.
  registry에 등록되고 bounded path를 소유한다. abort는 owned cursor를 cooperatively 무효화한다.
- `request_cancel()`, `advance_disposal(max_edges: int) -> int`, `status`를 제공한다. 상태는
  `BUILDING/SEALING/CANDIDATE/DISPOSING/DISPOSED/TRANSFERRED/FAULT`다.
- `take() -> TransferredSeq`: await 없이 candidate root + **필수 lifetime pin registry 전체의
  소유권**을 이동한다. 큰 registry를 복사/clear하지 않고 작은 owner shell의 일회 pointer 이동이다.
- `TransferredSeq.open_reader() -> SeqReadLease`는 bundle에 명시 등록한 lease를 반환한다.
  lease의 `sequence`는 읽기 전용 weak capability이고 source arena의 취소가 값을 바꾸지 않는다.
  `SeqReadLease.close()`는 멱등이며 해당 lease의 capability/cursor를 무효화한다. close 뒤
  저장해 둔 capability alias도 읽을 수 없다. 임의 Python alias의 생존 여부는 검출하지 않는다.
  bundle에 직접 sequence property는 없다. `TransferredSeq.request_disposal()`은 **등록된**
  lease/owned cursor가 남으면 ValueError("reader_active"), 전부 반납되면 DISPOSING이다.
  `advance_disposal(max_edges: int) -> int`는 arena와 같은 budget1..256/실제 계수 규칙이다.
  이는 전용 시험 protocol이며 일반 Python last-ref destruction이나 실제 consumer 수명의
  안전성을 보장하지 않는다. 그 제품 계약은 다음 단계 필수 미해결이다.
- `inspect_owned_edges(arena_or_bundle)`는 시험에서만 독립 graph observer를 만든다. 이 observer와
  keepalive를 붙인 실행은 latency 측정이 아니다. production caller에 노출하지 않는다.

### Plan / 자료 표현

private node는 최대32개 child/value 슬롯을 갖고 leaf는 순서와 중복을 그대로 보존한다.
봉인은 **외부 읽기 권한을 고정**하는 것이며 내부를 native immutable tuple로 바꾸지 않는다.
각 slot을 하나씩 제거할 수 있는 private mutable block을 사용하고 registry가 생성 때부터 pin한다. grow/seal의
continuation stack도 bounded block이며 원래 node와 새 node는 인계가 끝날 때까지 모두 등록된다.
오류 후 generator frame을 찾아서 pin을 복구하는 방식은 금지한다.

completed-unclaimed까지 pins를 유지한다. 먼저 pins를 모두 버리고 root만 남긴 뒤 take하면
그 사이 취소가 전체 tree destructor를 유발할 수 있다. take의 destination shell을 먼저 준비하고
검증을 끝낸 뒤 pointer ownership을 이동하며, 실패할 수 있는 allocation은 이동 전에 수행한다.
terminal cancel(`TRANSFERRED/DISPOSED`)은 no-op; FAULT에서는 proof/state를 유지한다.

- [ ] **Step 1 — RED: 순서/중복/후보 취소/edge 계수를 실제 graph로 검사한다.**

```python
import pytest
from l3_sequence_probe import SeqArena, inspect_owned_edges

@pytest.mark.parametrize("size", [0, 1, 31, 32, 33, 1024, 4097, 100000])
def test_candidate_cancel_releases_real_edges_in_quanta(size):
    arena = SeqArena()
    for index in range(size):
        arena.append(str(index % 7))
    arena.begin_seal()
    while arena.status == "SEALING":
        assert 0 <= arena.advance_seal(256) <= 256
    candidate = arena.candidate
    assert len(candidate) == size
    for index in range(size):
        assert candidate[index] == str(index % 7)
    if size:
        assert candidate[-1] == str((size - 1) % 7)
    observer = inspect_owned_edges(arena)
    arena.request_cancel()
    while arena.status != "DISPOSED":
        observer.begin_step()
        reported = arena.advance_disposal(256)
        actual = observer.end_step(reported)
        assert actual == reported
        assert 0 < reported <= 256
    assert observer.live_owned_edges == 0
    assert observer.max_unaccounted_release == 0
    with pytest.raises(ValueError, match="candidate_unavailable"):
        len(candidate)
```

`inspect_owned_edges`는 시험 전용 독립 `EdgeObserver`를 반환한다. `begin_step()`은 실제 private
field/list/registry의 edge ID multiset을 만들고 임시 순회 참조를 모두 버린다. 실행 중 graph를
강하게 보유하지 않으며 ID·weakref·고정 진단값만 보존한다. weakref callback은 scalar ID만 기록한다.
`end_step(reported: int) -> int`는 아래 실제 primitive 감사 결과를 누적/대조하고 실제 제거량을 반환한다.

시험의 primitive 감사는 **실제 slot 교체/pop/delete와 bounded block 최종 해제 경계**에서 이전
참조·변경된 field를 읽어 대조한다. disposer의 reported counter나 별도 remove 통지만 신뢰하지 않는다.
도중 생겼다 사라진 scratch/pin도 각각 기록한다. strong reference를 지역변수에 보유하는 관측기는
해제 전에 그 변수를 버린 뒤 자연스러운 마지막 ref 종료를 weakref 수명 기록과 비교한다.
before/after snapshot은 교차 대조일 뿐 실제 내부 해제량을 대신하지 않는다. graph의 제거 외에
registry/cursor/명시 scratch/마지막 shell과 frame 인계 참조도 감사 대상이다.

`live_owned_edges`는 독립 사후 순회의 남은 edge 수, `max_unaccounted_release`는 한 step 안의
primitive 실측 제거량에서 reported를 뺀 양수 차이 최댓값이다. 관측 도메인을 확정할 수 없거나
observer가 소유권 수명을 가렸다면 PROOF_FAIL/미확정이다. interpreter 임시 ref까지 측정했다는
주장이나 weakref/payload sentinel만으로 통과시키는 것은 금지한다. 이 관측은 latency 증거가 아니다.

독립 관측기가 문제를 잡는지도 RED로 고정한다. test-only `mutate_for_observer_test(arena, mode)`의
fixed mode는 `uncharged_scratch_drop`, `underreport_one`, `early_pin_root_drop` 세 개다. 각각 내부
scratch 생성/제거 누락, charge1 과소 보고, root/pin 동시 해제 cascade를 주입한다. 세 변이는 모두
observer 또는 구조/수명 단언을 반드시 실패시켜야 한다. 별도의 observer **없는** 실행에서도
weakref 수명·노드별 슬롯 상한·완전 정리를 확인해 계측이 없으면 cascade가 생기는 구현을 거부한다.
이 독립성 증명이 실패하면 helper 구현의 counter가 맞아도 PROOF_PASS가 아니다.

작은 budget과 builtin 변환은 다음 독립 RED도 고정한다. 이 예시는 native tuple node의 무한
defer를 검출하는 작은 fixture다. 4096은 이 size33 시험의 유한 진전 상한이지 제품 history 제한이 아니다.

```python
@pytest.mark.parametrize("budget", [1, 2, 31, 32, 33, 255, 256])
def test_small_budget_makes_progress_and_blocks_builtin_iteration(budget):
    arena = SeqArena()
    for value in range(33):
        arena.append(value)
    arena.begin_seal()
    for _ in range(4096):
        if arena.status == "CANDIDATE":
            break
        arena.advance_seal(budget)
    assert arena.status == "CANDIDATE"
    with pytest.raises(TypeError, match="use_owned_cursor"):
        tuple(arena.candidate)
    with pytest.raises(TypeError, match="use_owned_cursor"):
        list(arena.candidate)
    arena.request_cancel()
    for _ in range(4096):
        if arena.status == "DISPOSED":
            break
        assert 0 < arena.advance_disposal(budget) <= budget
    assert arena.status == "DISPOSED"
```

다음 matrix도 parameterize한다. injection은 고정 stage enum이며 일반 사용자 callable을 받지 않는다.

| 사건 | 필수 단언 |
| --- | --- |
| before_append/mid_leaf/grow/seal_each_level/sealed_before_take | 순서·duplicate·type 보존; 조기 root cascade0, disposal≤256 |
| budget1/2/31/32/33/255/256; 같은 OwnedSeqRef100k회/두 경로 alias/내부 cycle fixture | 실제 진전·edge별 release, double-clear0; cycle은 동일 slot FAULT 또는 정당한 graph 정리로 처리하며 false DISPOSED0 |
| active/partly-read/abandoned/closed cursor | owner registry가 cursor 강참조; abort 뒤 재독0; cursor path가 정리 전 숨겨져 남지 않음 |
| allocation/seal/disposal known error, foreign graph error | 최초 오류와 cleanup 오류 구분, 외부 repr/str/iterator 호출0, foreign은 미인증 FAULT |
| take/double_take/preallocation_failure/post_transfer_cancel | 전체 소유권은 한쪽에만, early pin retirement0, exported 값 불변 |
| open_reader/복수 lease/부분 cursor/close 두 번/반납 뒤 capability alias | 등록 reader 생존 중 해제 거부, 전부 반납 뒤 정리, 종료 lease의 alias 읽기 거부 |
| hostile str/list/Sequence subclass, bool index, float NaN/Inf | TypeError/ValueError, 임의 callback0, 이전 owned graph/순서 불변 |
| tuple(candidate)/list(candidate)/observer 세 변이 | builtin iteration trap; 세 변이 각각 실패, 실제 primitive 관측 없으면 통과 금지 |

sequence foreign fault는 gate가 없는 별도 negative child다. fault 뒤 loop observer가 실행됐고
같은 arena가 FAULT인 것을 확인한 뒤 append/begin_seal/take/advance_disposal 재호출이 모두
ValueError("arena_faulted")로 거부됐는지 검사한다. 이후 다음 JSON을 flush한다.

```json
{"case":"sequence_fault","fault":true,"same_arena":true,"post_fault_turn":true,"cleanup_completed":false,"build_reentry_rejected":true,"transfer_rejected":true,"disposal_reentry_rejected":true}
```

부모는 clean-env child 시작부터 monotonic10초 deadline 하나로 정확한 출력·생존을 확인하고
terminate/wait한다. 누락/timeout/종료 실패는 실패, 재시도0이며 정상정리로 보고하지 않는다.
`fault_marked_disposed`·`fault_allows_take` 변이는 각각 위 관측을 실패시켜야 한다.
두 mode도 `mutate_for_observer_test(arena, mode)`에서만 선택한다. 함수의 전체 mode 집합은
앞의 관측기 세 변이와 이 두 fault 변이, 총5개이며 그 밖의 값은 상태 변경 전 ValueError다.
transferred bundle의 실제 소비자 자동 해제는 구현하지 않고 명시 관리형 해제
시험만 수행한다. 이 제한을 숨기기 위해 reference leak을 정상 상태로 두지 않는다.

- [ ] **Step 2 — RED 실행.** import 부재 또는 실제 미인계 graph 수명 단언 실패인지 기록한다.

```bash
timeout --signal=TERM 180s env -i PATH=/usr/bin:/bin LANG=C.UTF-8 TZ=UTC PYTHONDONTWRITEBYTECODE=1 \
  PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 /home/ubuntu/projects/qwq-ai-trader/venv/bin/python \
  -m pytest tests/test_l3_sequence_probe.py -q -p no:cacheprovider --tb=short
```

- [ ] **Step 3 — 최소 시제품 구현.** 부품은 ordinal 경로로 찾아가며 한 node fan-out32만 처리한다.
  disposal은 child의 별도 pin이 존재하는 동안 parent edge를 하나씩 제거한다. 핵심 제어는 다음과 같다.

```python
removed = 0
while removed < max_edges and cursor.has_edge():
    owner, field = cursor.next_edge()
    arena.require_private_owner(owner)
    cursor.assert_child_already_pinned(owner, field)
    arena.remove_owned_edge(owner, field)
    removed += 1
arena.finish_only_if_no_owned_edges()
return removed
```

`cursor`는 별도 registry traversal state이며 자신의 path/registry 참조 제거도 work에 포함한다.
`remove_owned_edge`는 private slot 하나의 제거이며 container 전체 clear가 아니다. sequence는
native tuple node를 사용하지 않으므로 budget1에서도 다음 edge를 실제로 제거할 수 있어야 한다.
child pin은 parent edge 제거 전에 이미 registry에 존재하며 무계수 임시 pin 복사를 만들지 않는다.
`assert_child_already_pinned`는 기존 pin identity를 읽기만 하며 새 pin을 만들지 않는다.
exported owner capability에는 source arena 정리를 적용하지 않는다. 마지막 shell을 버릴 때도
남은 owning field를 먼저 한 개씩 없애고 graph 해제가 없는 status 갱신만 남긴다.

- [ ] **Step 4 — 같은 집중 GREEN 및 기존 frozen-value 회귀 실행, 다른 Astra/xhigh가 검토한다.**

```bash
timeout --signal=TERM 180s env -i PATH=/usr/bin:/bin LANG=C.UTF-8 TZ=UTC PYTHONDONTWRITEBYTECODE=1 \
  PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 /home/ubuntu/projects/qwq-ai-trader/venv/bin/python \
  -m pytest tests/test_l3_sequence_probe.py tests/test_recovery_projection.py \
  -q -p no:cacheprovider --tb=short --show-capture=no
```

이것은 clean Task1 baseline 회귀다. frozen Task2 후보의13.023871ms 실패를 재시험/대체하는
명령이 아니다. large fixture는 구조 인수이지 new performance PASS가 아니다.

- [ ] **Step 5 — 구조 증명이 실패하면 정당한 표현 대안까지 보고하고 중단한다.** 공개
  FrozenJSON 타입을 수정하거나 tuple 생성 facade로 통과시키지 않는다. GREEN이면 한정 리뷰와
  아래 통합 검증 뒤 두 literal path만 커밋한다.

## coordinator See / 통합 및 문서화

- [ ] 두 독립 부품의 base/allowlist/RED→GREEN/리뷰 지적 처분을 확인한다. helper만 존재하고
  제품 import0인 것을 `rg -n 'l3_source_lease_probe|l3_sequence_probe' src scripts`로 확인한다.
- [ ] 허용 파일 문법 검사 후 집중4파일 및 기존 store/gate/registration regression을 직렬 실행한다.
- [ ] 전체 suite는 모든 worker가 idle인 뒤 UTC→KST 순서, 각 외부900초 상한으로 실행한다.
  clean-env fixture 문제/timeout/새 실패도 통과가 아니며, 실패 시 원인을 기록하고 커밋·push를
  보류한다. `.env`나 운영 자격증명으로 fixture를 복구하지 않는다. 기존 xfailed만 허용한다.

```bash
timeout --signal=TERM 900s env -i PATH=/usr/bin:/bin LANG=C.UTF-8 TZ=UTC \
  PYTHONDONTWRITEBYTECODE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
  /home/ubuntu/projects/qwq-ai-trader/venv/bin/python -m pytest -q -p no:cacheprovider --tb=short --show-capture=no
```

UTC가 실제 통과했을 때만 같은 명령의 `TZ=Asia/Seoul` 버전을 실행한다. 서로 다른 timezone의
건수를 합산하지 않는다. 자동 latency probe가 포함됐다면 정확히 해당 baseline 결과로만 보존한다.

- [ ] 독립 broad review는 부품 scope만 판정한다. source/capture/representation 전체와 운영
  readiness를 승인하지 않는다. 결과 문서에 cold-open/decoder/consumer retirement/Task2 실패를
  별도 미완 항목으로 남긴다.
- [ ] 리뷰 승인 뒤만 literal 파일 allowlist를 stage한다. changed⊆allowlist, staged=reviewed,
  `git diff --cached --check` 및 비밀정보 패턴 검사를 확인한다. 명령/exit/로그/hash를 기록한다.
  RED 또는 source 차단 후보가 원격에 나가지 않도록 한다. coordinator만 path-scoped commit/push한다.

```bash
set -euo pipefail
git add -- tests/l3_source_lease_probe.py tests/test_l3_source_lease_probe.py
git diff --cached --check
git diff --cached --name-only
git commit -m "test(safety): prove warm source handoff ownership" -- tests/l3_source_lease_probe.py tests/test_l3_source_lease_probe.py
```

Task2의 경로 한정 커밋은 다음과 같다.

```bash
set -euo pipefail
git add -- tests/l3_sequence_probe.py tests/test_l3_sequence_probe.py
git diff --cached --check
git diff --cached --name-only
git commit -m "test(safety): prove managed sequence candidate lifetime" -- tests/l3_sequence_probe.py tests/test_l3_sequence_probe.py
```

push는 commit SHA/상태를
확인한 별도 명령으로 **그 feature branch만** 수행한다. docs 변경은 coordinator의 별도 reviewed
docs-only 커밋이며 main merge는 하지 않는다.

## 호환성 인계표 — 다음 계획에서 반드시 소유자를 정할 경로

| 소비자 | 바뀌면 안 되는 의미 | 이 계획의 처리 |
| --- | --- | --- |
| IntentFact.attempt_ids / membership index | 순서·중복·invalid count·valid frozen identity | 미변경, 관리형 후보 타입으로 직접 전달 금지 |
| ordered admissions / invalid facts / symbol outputs | 정렬·중복·random index·정확한 invalid 의미 | 미변경, tuple 반환 facade 미구현 |
| policy history/selectors / protection exit history | exact type·version 순서·ABA·digest·identity | 미변경; Task2 정책 수정은 clean base에 가져오지 않음 |
| thaw/canonical/encode_state/digest | exact dict/list·finite scalar·동일 JSON bytes | 미변경; 시제품 oracle의 materialization만 허용 |
| startup factory/_open/_load/_publish/runtime.restore | 같은 SQL snapshot·불변 scope·day/fence·기존 원장 | warm 시험에 포함됐다고 주장하지 않음 |
| exported result의 마지막 소비자 | live snapshot 불변·마지막 ref 해제 비용 | 실제 consumer lifecycle은 별도 설계/RED |

## 실행 후 종료 판정

결과는 Task1/Task2 각각 `PROOF_PASS`, 구체적 `PROOF_FAIL`, 또는 측정/환경 미확정으로 기록한다.
이름만 구현하거나 counter만 맞추면 PASS가 아니다. 통과한 부품은 시험 전용으로 보존할 수 있지만
product code로 복사하는 순간 별도 명세·검토가 필요하다. full managed decoder를 기존 json.loads에
그대로 감싸는 것으로 계획을 확대하지 않는다. 전체 L3/Task2/엔진 배포 완료는 계속 별도다.
