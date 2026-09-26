# N5 L3 복구 빌드 취소·자원 수명 설계 제안

2026-09-27 KST. **서면 설계 제안이며 구현 승인·성능 합격·운영 연결 승인이 아니다.**
기존 [N5 설계](2026-09-24-recovery-projection-index-design.md) §7.2/7.3/11.2를
보존한다. 아래 신규 인터페이스·입력/표현 계약은 별도 검토 대상이며, 구현계획은 아직 없다.
현재 코드와 성능 증거는 [진행 원장](../../reviews/recovery-projection-progress-2026-09-27.md)에 있다.

## 목적과 범위

복구가 취소되거나 실패해도 오래된 결과를 공개하지 않고, 큰 임시 자료가 한 번에 해제되어
거래 event loop를 장시간 점유하지 않도록 한다. **논리 취소, 물리적 정리, 결과 소유권 인계**를
서로 다른 사건으로 정의한다. 정리 전 다음 빌드를 시작하거나 미완료 정리를 배경 큐로 넘기지 않는다.

L1/L2 성공 종료 구조 승인은 이 계약의 승인이 아니다. frozen 후보는 HEAD `038aa599` 위
표준 diff SHA256 `721fe1c7bc60bf44876805a823a0d42f253b706f026eea6d8a76fcffd4c0c41c`다.
현재 `RecoveryBuildSession.close()`는 동기 generator 종료이고, `advance()` 예외와 async
wrapper의 finally에서도 호출한다. `session.result`는 외부로 빠져나갈 수 있다. 실제 full-builder
외부 호출은 `mutation_plans.py`의 명시 migration fallback이며 managed owner 연결은 없다.

금지 범위: 주문/전략/위험/보존 정책 변경, GC 비활성화·임계 상향, 별도 thread/process로 해제
이관, evidence pruning, 정상 writer에 full build 추가, 운영 서비스 조작, Task2 후보 승격.

## 대안과 권고

| 대안 | 판단 |
| --- | --- |
| 기존 동기 close 유지 | 호환성은 크지만 대형 graph의 동기 해제 문제가 남는다. legacy 전용으로 유지한다. |
| shield된 task가 취소 후 build를 끝까지 수행 | 오류 난 build는 완주할 수 없고 미인계 결과의 폐기도 남는다. 단독 해법으로 쓰지 않는다. |
| 단일 operation이 자원 목록과 협력적 정리를 소유 | **권고.** 소유권을 오류 전에 확정하고 정리 완료 증거로만 gate 반환을 허용한다. 입력·표현 설계 검증이 선행돼야 한다. |

## 1. 입력부터 시작하는 단일 소유권

새 managed 경로는 FIFO ticket 획득 전에 detached graph/session/driver를 만들지 않는다.
ticket 획득 뒤 owner의 mutation/admission barrier와 정확한 source snapshot lease를 확보한다.
lease는 source identity·owner token·receipt/checkpoint SQL snapshot의 결속과 읽기 유효 기간을
명시한다. 모든 입력 writer가 이 barrier를 따르는 실제 인수 전에는 경로를 활성화하지 않는다.

기존 `marshal.loads(marshal.dumps(...))` 결과를 새 arena에 뒤늦게 등록하는 방식은 채택하지 않는다.
큰 tuple의 마지막 참조와 초기화 실패 시 임시 graph가 이미 동기 해제될 수 있기 때문이다.
managed capture는 할당 순간부터 bounded 자료 블록을 operation에 등록하고, 부분 capture도
동일 abort/disposal 계약에 포함한다. 원본 lease는 capture 성공 또는 부분 결과 정리 완료까지 유지한다.

원본을 읽는 도중 await가 있어도 같은 snapshot이어야 하며, stale 검사만으로 섞인 복사본을
정상 결과로 만들지 않는다. source lease 해제 자체가 대형 graph의 마지막 참조를 버리지 않는지
검증한다. 그렇다면 그 원본의 실제 소유자까지 관리형 정리 계약에 포함해야 하고, 이를 단순히
caller 비용으로 빼거나 영구 pin으로 숨길 수 없다. load/decode/capture/disposal 비용은 해당
제품 연산 timer 안에 남는다.

**미해결 설계 인수:** 실제 store snapshot 공급자와 source lease의 획득/해제 경로, 부분 decode
실패에서의 소유권, 큰 scalar/sequence 표현을 구체화하고 기존 serialization/검증 의미와
대조해야 한다. 위 요구사항을 충족하는 capture 구현 가능성을 아직 증명하지 않았다.
따라서 표현 변경 방향 승인만으로 managed 입력 계약이나 전체 L3가 완료되지 않는다.

## 2. 새 managed 인터페이스와 상태

아래 이름은 제안이며 기존 클래스의 동작을 몰래 바꾸지 않는다.

- `request_cancel(reason)`: 동기·멱등 상태 변경. 즉시 결과 사용을 차단하고 abort를 기록한다.
  generator close, graph 순회, 큰 참조 삭제, 사용자 callback은 실행하지 않는다.
- `advance_build()`: 단일 driver만 호출한다. abort 뒤 재진입하지 않으며 성공해도 결과는 private
  `CANDIDATE`다. 공개 `result` 필드로 소비자에게 먼저 넘기지 않는다.
- `advance_disposal()`: 같은 operation의 private capability만 소비한다. progress와 실제 완료를
  분리하며 child ownership을 확보한 뒤 작은 단위로 참조를 해제한다.
- `take_result()`: 완전성·정확한 owner token·scratch 정리가 확인된 candidate를 정확한 owner에
  한 번만 넘긴다. 확인·소유권 이동 사이 await가 없고, 취소 이후 또는 두 번째 호출은 거부한다.
- `wait_closed()`: 정리 작업 자체가 아닌 shield된 관측용 await다. caller 취소가 내부 완료 증거를
  cancel하지 않는다.

상태는 `NEW → BUILDING → CANDIDATE → TRANSFERRED` 또는
`NEW/BUILDING/CANDIDATE → DISPOSING → DISPOSED`다. 정리 불능은 `DISPOSAL_FAULT`다.
phase와 최초 원인(cancel/error/stale)은 별도 진단값이다. `phase=cancelled` 또는 `Task.done()`은
정리 완료 증거가 아니다. `TRANSFERRED` 후 session의 취소는 소비자 graph를 변경하지 않는다.

`TRANSFERRED`/`DISPOSED`에서 `request_cancel`은 owner publication·gate·consumer를 변경하지 않는
no-op이다. `DISPOSAL_FAULT`에서는 같은 fault와 미완료 proof를 유지한다. 별도 owner fault/epoch
무효화는 기존 owner 경로의 책임이며 이미 종료된 session의 취소와 혼동하지 않는다.

owner에 결과를 넘기는 것과 current 공개는 다르다. 기존 §7.2대로 runtime day/fence/admission/
binding 복원이 끝나야 current가 된다. 인계 뒤 실패한 owner는 unavailable이며 이를 성공으로
되돌리지 않는다. 이미 원자적으로 성공한 publication을 늦은 caller 취소만으로 되돌리지 않는다.

## 3. gate 반환 증거와 고장

active owner slot이 operation, exact token, arena, driver, private cleanup certificate를 강하게
보유한다. 다음 cancellation point 전에 책임과 certificate를 등록한다. 제출/등록 실패에도
graph가 owner 없이 실행되거나 정리 책임 없이 사라지는 창이 없어야 한다.

standalone gate의 일반 Future/Task drain은 실패·취소도 settled로 취급한다. 따라서 builder task
그 자체를 정리 증거로 등록하지 않는다. **모든 private scratch가 정리되고 결과가 정리 또는
정확한 consumer로 인계된 경우에만 성공하는 private Future**를 등록한다. 외부에는 노출하지 않고
driver 실패 시 `set_exception`/`cancel`로 종료시키지 않는다. 이 adapter 불변식이 실제 시험에서
성립하지 않으면 gate 변경을 별도 설계·리뷰해야 한다. 기존 gate 부품 승인으로 대신하지 않는다.

caller 취소는 latch만 설정하고 driver를 cancel하지 않는다. 반복 취소 중에도 ticket과 certificate를
유지하고 안전 정리 뒤 `CancelledError`를 전달한다. 오류 경로의 전체 취소 응답 시간은 유한 상한을
보장하지 않는다. 정리 실패는 동일 operation 하나를 보유하는 `DISPOSAL_FAULT`, writer0,
unavailable이다. 새 builder·backlog·busy retry를 만들지 않고 certificate를 완료로 위장하지 않는다.

graceful shutdown은 loop가 살아 있을 때 drain한다. loop/process/interpreter 강제 종료 중에는
협력적 진전을 보장할 수 없으며 cleanup_completed를 true로 쓰지 않는다. 다음 시작은 기존 durable
restore 장벽을 다시 통과한다. fault 복구/운영 재시작 정책 변경은 이 제안에 포함하지 않는다.

## 4. graph·표현·오류 계약

arena는 frame unwind 뒤 graph를 수거하는 장치가 아니다. 할당 때부터 소유하며 registry 자체도
거대한 container 하나의 마지막 해제로 끝나지 않는다. 다음 자원을 모두 포함한다.

- detached/partially captured 입력, unknown nested field, alias와 중복 참조.
- transient/sealed map·tree·sequence, DTO, membership, order, seen/stack/registry.
- 정책 검증의 raw/body/canonical 임시 자료, iterator/continuation/frame/closure.
- completed-unclaimed candidate, error/context/cause/traceback의 graph 참조.

공개된 snapshot과 caller 입력에는 destructive 권한이 없다. child pin만으로 큰 immutable tuple
자체의 N개 outgoing reference 해제를 나눌 수 없으므로 bounded fan-out 표현이 필요하다.
순서·중복·값·유효/무효 판정·serialization·필요한 identity 공유 의미를 보존하며 exact tuple
소비자를 실제 inventory로 확인해야 한다. 완료 직전에 큰 tuple을 만들면 인계 전 취소가 여전히
남으므로 해법이 아니다. 이 표현 변경의 구체 자료구조와 소비자 호환성은 아직 인수하지 않았다.

graph를 보유한 원 exception/traceback를 외부로 넘기지 않는 managed failure envelope를 제안한다.
최초 원인과 cleanup 실패를 고정 개수의 진단 항목으로 구분한다. 임의 repr/str/exception args를
안전한 scalar로 취급하지 않으며 사용자 frame을 임의로 지우지 않는다. 취소가 latch된 경우 정리
뒤 CancelledError를 전달하되 발생한 build 오류의 진단을 없애지 않는다.

주입 build clock/hook이 실패하면 재호출하지 않고 정리 scheduler와 고정 work quantum을 사용한다.
trusted scheduler/clock까지 실패해 진전을 보장하지 못하면 fault이며 측정 불능을 PASS로 쓰지 않는다.

managed entry는 arbitrary 사용자 clock/yield_hook을 받지 않는다. legacy callback API와 구분하고
제품 소유 scheduler/clock 및 고정된 오류 도메인만 사용한다. 시험의 fault injection도 이 도메인에
한정한다. 외부 callback이 가진 큰 exception args/traceback는 arena 소유가 아니므로 단순
sanitization만으로 bounded 해제를 보증할 수 없다. 예상 밖 graph-bearing 예외는 재호출·임의
frame clear·증거 삭제 없이 같은 operation의 private fault 슬롯에 보유하고 `DISPOSAL_FAULT`로
미인증 유지한다. 외부 오류 graph의 정리 정책을 해결하기 전에는 cleanup certificate를 발급하지
않는다. 이 제한과 sanitized managed 오류 전달은 새로운 API 계약이며 legacy에 소급하지 않는다.

## 5. 호환성과 예산의 정확한 구분

기존 sync builder, blocking close, lazy async API는 legacy로 명시해 호출/오류 의미를 보존한다.
managed 객체와 혼용하지 않는다. sync migration fallback은 협력적 live-loop 보증 밖이며,
실제 owner 연결 전에 managed 경로로 옮기거나 미지원 상태로 남겨야 한다. legacy 경로가 남은
상태를 전체 L3 해결로 보고하지 않는다. 원본/이미 exported 결과의 값을 바꾸지 않는다.

기존 수치인 256 facts 또는5ms yield, raw50ms stall, 성공 restore1,000ms/ticket1,500ms는 유지한다.
**disposal의 outgoing-edge 작업량까지 ≤256으로 계수하는 것은 기존 facts 계약의 단순 재서술이
아닌 새 구조적 인수 제안**이다. 별도 승인·work 정의가 필요하며 fact 하나에 무제한 해제를
숨기지 않기 위한 것이다. 이 구조 증명은 CPython GC/allocator를 선점하거나 임의 크기 입력에
wall time을 보장한다는 뜻이 아니다. 기존 empirical raw/controlled 성능시험도 별도로 통과해야 한다.

현재 후보는 자동 GC 없는5k freeze 최대13.023871ms로5ms 시험을 실패했다. L3 설계로 이 실패,
이전 raw57.747982ms, 과거 policy/100k tail, 미실행 셀을 소급 해결하지 않는다.

## 6. 구현 전 고정할 RED 인수

1. capture 전/도중, NEW, 모든 build phase, finalize, candidate 인계 직전 취소와 반복 취소.
2. source snapshot/lease, 부분 decode 오류, 인계 이전 참조 수명, mixed-snapshot 금지.
3. 실제 graph/edge 수명 계수: 큰 단일 membership/history/unknown sequence, frame unwind,
   registry 마지막 해제, shared alias/cycle/잘못된 입력. 계측 keepalive를 latency 증거로 쓰지 않는다.
4. build/clock/hook 오류와 cleanup 오류 구분, graph-bearing traceback 외부 escape 금지.
5. generic task 실패/취소와 cleanup proof 분리; fault 때 다음 writer/builder 진입0, active graph1,
   backlog0, false completion0, survivor FIFO 유지.
6. 정확한 token 네 축의 stale, double-take/dispose, exported 결과 불변, 인계 뒤 publication 실패.
7. 미시작 coroutine/driver와 생성 실패, legacy sync/close/migration parity.
8. 구조 인수 후 별도의 취소·오류 정리 raw 성능 및 전체 restore/ticket/63셀·UTC/KST·broad review.

## 결정 및 진행 경계

권고는 관리형 lifecycle + 안전 인계 + 정리 증거를 채택하되, **source lease/capture와 bounded
표현의 구체 타당성**을 먼저 닫는 것이다. 순수 async-close 수정으로 시작하지 않는다.
원래 N5 통합계획의 Task4 이후 차단은 유지한다. 이 문서 승인 뒤에도 실행계획과 인수 방법을
별도로 확정해야 하며, 사용자 승인되지 않은 API/예산 변경이나 운영 활성화를 추론하지 않는다.

초안 분석 Astra/high, 독립 설계 검토 Astra/xhigh, 성능 절차/원인 분석 Sol/high 요청.
actual model/effective effort metadata는 미노출로 미검증이며 cross-provider 검토가 아니다.
