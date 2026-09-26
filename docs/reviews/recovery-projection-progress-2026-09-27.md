# N5 복구 projection·FIFO gate 진행 및 인계

기준: 2026-09-27 KST. 이 문서는 개발 증거와 미완료 항목을 기록한다. 전체 엔진 설치·main 통합·운영 배포 승인이 아니다.

## 현재 판정

- Task 1: `fec1bbc1fde436bfb09609901b71b68b1162c0dd`까지 독립 검토 승인. 이후 Task 2에서 같은 모듈을 수정한 후보까지 승인된 것은 아니다.
- Task 2: **미완료·성능 차단**. 마지막 커밋은 `038aa599f043e62d200df048152fa292167fea72`; 그 위의 세 파일 수정안은 미커밋이며 Task2 전체 승인을 받지 않았다. L1/L2 후속 후보는 **구조 범위 독립 승인**됐고, 표준 diff SHA256은 `721fe1c7bc60bf44876805a823a0d42f253b706f026eea6d8a76fcffd4c0c41c`다. 수정 후 성능은 미측정이며 진단에 사용한 이전 `8443703…` 후보와 실패 기록은 별도 보존했다.
- Task 3: **독립 부품 완료·재리뷰 승인**, 커밋 `9a01fa1`. 승인된 Task 1에서 분리한 `feature/owner-ticket-gate-20260926`에 코드·시험 두 파일만 기록했다. 초기 리뷰의 차단 4건을 보완했고 부품 12건·관련 owner/store 포함 129건이 통과했다. Task 2 수정안을 이 브랜치에 복사하지 않았다.
- Task 4–15: 미완료. 특히 실제 owner commit/restore 경로 연결, 전체 성능 행렬, UTC→KST 전체 검증과 broad review는 아직 완료하지 않았다.
- 이번 작업에서 main·운영 서비스·주문·전략·위험 설정·Toss 승인 범위를 바꾸지 않았다. 운영 상태를 새로 조회했다는 의미도 아니다.

설계 정본은 [N5 상세 설계](../superpowers/specs/2026-09-24-recovery-projection-index-design.md), 실행 항목은 [구현계획](../superpowers/plans/2026-09-24-recovery-projection-index.md)이다. 설계서의 실행 방식 선택 대기 문구는 당시 기록이며, 사용자는 이후 하위 에이전트 방식 실행을 선택했다. 승인된 성능·안전 계약은 그대로다.

## Plan → Do → See

### Plan

누적 원장을 매번 복사·분류하는 비용을 없애기 위해 정확한 owner token에 결속한 불변 projection/index와 typed mutation을 만든다. 정상 변경은 64행·256개 의존성 한도, raw update 10ms, cooperative work 5ms/256, 5,000행 raw 연속 정체 50ms를 유지한다. 전체 복원·운영 전환은 별도 인수 대상이다.

### Do

9월 26일 사용량 한도로 중단된 리뷰를 재개했다. `038aa599`의 순번 trie는 기존 순서 순회를 없앴지만, 해시가 같은 버킷에 몰리면 값과 순번 맵 각각 5,000개를 복사했다. 공개 변경 API가 이 작업을 의존성 58개로 승인하는 재현을 고정했다.

첫 bounded-leaf/AVL 후보는 전체 복사를 줄였으나, 50개 삽입 후 32개 삭제에서 leaf-pivot 이중 회전이 실제 키 16개를 잃었다. 같은 후보는 5,000행 정체 78.822ms와 100k 두 셀의 30초 초과로도 실패했다. 이 후보는 기각·별도 보존했다.

후속 후보는 leaf 회전에서 최대 34개 실제 항목을 보존하고, 초기 구성에는 독점 가변 노드, 게시 전에는 협력적 일회 불변화를 사용한다. 이후 일반 갱신은 최대 32개 leaf와 조상 경로만 복사한다. 성공 불변화 중 임시 자식 참조를 순차 해제한다.

### See

| L1/L2 수정 전 transient/seal 후보 검사 | 실제 결과 |
| --- | --- |
| 구조·소유권 집중 시험 | 53 passed |
| Task 1 의미 + mutation/policy/source/seal/N3/store | 740 passed, 별도 성능 20셀 제외 |
| 고정된 직렬 성능 배치 | 15 passed, 1 failed, 이후 4 unrun |
| 실패한 raw 5,000행 연속 정체 | **57.748ms**, 기준 50ms; 사전 baseline 1.503ms |
| 같은 셀의 1회 진단 | **56.004ms**; index advance 55.393ms에 gen2 GC 55.368ms 겹침 |
| 독립 Astra 요청 리뷰 | **CHANGES_REQUIRED: 차단 1, 권고 2** |

작성자와 다른 리뷰어가 전체 버킷 복사, leaf 회전 데이터 손상, private construction의 반복 영속 복사가 해결됐다고 판정했다. 그러나 raw 50ms 실패는 남아 있으므로 Task 2를 승인하지 않았다. GC 겹침은 진단에서 관찰된 원인 구간이며, 원래 실패의 소급 면제나 어느 객체군이 원인인지에 대한 증명은 아니다.

성능 배치는 첫 실패에서 멈췄다. 100k normal/controlled 두 셀과 long-protection 두 셀은 **미실행**이다. 과거 100k 비-GC 11.995/13.775ms, 정책 72.507/13.574ms 실패도 이후 통과값으로 삭제하지 않는다. 805.652ms 빌드 측정은 전체 restore 1,000ms 인수 통과가 아니다.

추가 권고는 private `_BuildOrder` 재사용 시 부분 소비로 인한 길이 불일치, 동기 cancellation disposal의 남은 객체 수 비례 비용이다. 현재 실제 순번 builder 호출부는 일회용이며 취소 중 부분 결과 공개는 검출되지 않았지만, 취소 처리의 고정 시간 상한은 입증되지 않았다.

작성자가 범위를 벗어나 시작한 잔여 전체 suite는 coordinator가 중단했다. 부분 출력과 clean-env 배포 시험의 환경 오류는 보존했으며, 이를 전체 검증 통과 또는 제품 회귀 확정으로 보고하지 않는다.

별도의 메모리 진단에서는 frozen 후보와 `038aa599`를 같은 합성 입력으로 각 1회 비교했다. 동일 `dependencies` 경계의 session 보존 객체는 68,776개 대 68,611개였고, 원본 fixture의 20,008개 객체도 양쪽에 동일하게 남았다. GC 대상은 공통 frozen map·proxy·fact DTO가 주를 이뤘다. 후보의 최종 표현은 객체가 2,973개 더 많지만 관찰된 index GC 뒤에 만들어졌으므로, 그 증가를 55ms 정체의 원인으로 볼 수 없다. **원인이 특정되지 않아 이 진단은 종료**했다. GC 정책 변경·입력 축소·인수 재시험은 하지 않았으며 통과 근거도 추가되지 않았다.

### 9월 27일 후속: CPU 원인 분리 진단

기존 wall-time 겹침만으로 CPU 사용과 스케줄링 대기를 구분할 수 없어, 같은 frozen `8443703…` 후보에서 별도 진단을 한 번 수행했다. 실행 전에 판정 규칙과 계측 코드를 독립 검토했다. 계측 비용의 판정 순서, 원본 pytest 자동 격리 fixture 적용, 직접 스크립트 실행의 import 경로 문제를 고친 뒤 실행했다.

프로토콜은 clean-env 단일 프로세스·30초 상한·재시도 없음이다. 원본 5,000행 입력과 caller의 원본 상태 보유, 자동 GC·임계값, 1ms heartbeat, 50ms/5ms 기준을 유지했다. 최대 gap에 완전히 포함된 동일 스레드 gen2 브래킷을 대상으로 wall/thread/process CPU와 계측 비용을 기록했다. 객체 전수 조사나 프로파일러는 이 실행에 넣지 않았다.

| 진단 관측 | 값 |
| --- | --- |
| 사전 baseline / raw 최대 정체 | 1.270ms / **48.690ms** |
| 최대 gap 내부 gen2 브래킷 | wall 43.639ms / 동일 스레드 CPU 43.640ms |
| 최대 `advance()` / examined | raw 44.529ms / 최대 256 |
| detach / 전체 build | 23.278ms / 822.033ms |
| 판정 | **미확정: 사전 조건인 raw 50ms 초과가 재현되지 않음** |

원본 item·fixture·소스 지문·스레드·고정 버퍼 검사는 유효했고 격리 위반은 0이었다. 그러나 `pytest 1 passed`와 exit 0은 진단의 실행 결과이지 Task2 인수 승인이나 원인 가설 지지가 아니다. 이 프로세스에서 43.640ms의 gen2 브래킷 CPU는 관찰됐지만 전체 gap 지배, 순수 GC graph 순회 비용, 특정 객체군 또는 과거 실패의 원인은 증명하지 않았다. **재측정하지 않고 종료했으며 과거 실패·미실행 4셀은 그대로 남긴다.** 원문 JSON·명령·전체 stdout은 Task2 SDD의 `gc-cpu-attribution-report-2026-09-27.md`에 보존했다.

같은 시점의 별도 소유권 감사는 두 구조 개선 대상을 특정했다. 성공 종료 때 오래된 큰 table-reference 저장소를 한꺼번에 해제하는 경로와, 유효 intent의 이미 frozen인 membership tuple을 다시 만드는 경로다. 전자는 관찰된 index GC보다 뒤에서 일어나므로 이번 정체의 원인으로 취급하지 않는다. 두 항목은 별도 L1/L2 구조 시험·구현·독립 검토 대상이며, 취소/오류 시 동기 graph 해제(L3)는 공개 수명 계약 검토가 필요한 잔여 항목이다.

### L1/L2 구조 수정 후보

- **L1 성공 종료:** detached build가 단독 소유한 큰 dict만 내부 capability에 등록한다. 마지막 policy reader가 끝난 뒤 최종 checkpoint·index·join의 참조를 협력적으로 검사해 여전히 쓰는 저장소를 보존한다. 나머지 임시 저장소는 항목별로 해제하고, 이 과정에서 추가한 방문 집합·capability·wrapper 목록도 나눠서 해제한다. 모든 capability가 공유 중인 경우에도 목록 순회를 한꺼번에 끝내지 않는다. 불필요했던 dependency 입력 root의 얕은 복사도 제거했다.
- **L2 membership 공유:** 각 항목의 exact-str 검증은 계속 협력적으로 수행한다. 모두 문자열이면 이미 frozen인 tuple을 그대로 사용한다. 중복·순서·존재하지 않는 ID의 의미는 그대로이며, 비문자열이 섞인 경우 기존 필터링과 invalid 판정을 유지한다.
- **경계:** 공개 frozen 입력·caller 원본·기존 snapshot을 소비하지 않는다. 결과 DTO가 별도로 보유한 참조와 중첩/다중 경로 참조를 시험한다. 원래 상태와 임계값·GC 정책·실행 수명·공개 API는 바꾸지 않는다. 이 구현이 추가한 최종 graph 순회 비용은 성능 인수로 검증해야 하며, 구조 시험의 keepalive 계측은 latency 증거가 아니다.

첫 RED는 11 failed / 3 passed였다. 이후 많은 최상위 항목을 가진 입력 root와 retained capability 순회의 별도 RED도 고정했다. 마지막 작성자 구조 시험은 17 passed, 13개 파일의 관련 의미 회귀는 **757 passed / 20 timing deselected / 63.53s**였다. coordinator가 같은 고정 후보에서 별도로 실행한 구조 시험도 **17 passed / 3.75s**였고 두 실행 모두 격리 위반은 0이었다. 전체 저장소 suite나 성능 행렬을 실행한 결과는 아니다.

작성·리뷰 보고서 및 검증 로그는 Task2 SDD의 `task-2-retirement-report.md`, `task-2-retirement-review.md`, `retirement-*.log`에 보존한다. L1/L2만의 변경은 `retirement-only.patch`(SHA256 `03bbde47a35069bc09642dfebcffa6c3cd1f0390d8561a0247f2e10ae0302ed5`), 전체 후보는 `retirement-candidate-721fe1c7.patch`다. 기존 `tests/test_execution_mutation_plans.py` blob `83a627be601bdf361e70d8977f2197111eba76a3`는 바꾸지 않았다.

작성자는 Astra/high 요청, 독립 리뷰어는 다른 Astra/xhigh 요청이다. 독립 검토는 detached 소유권 이전·wrapper 별칭·최종 DTO 참조·협력적 해제와 tuple 의미를 대조했고 **Spec compliance / Code quality 모두 `APPROVE_THIS_SLICE`**, 구체적 P0/P1/P2 결함 없음으로 판정했다. 이는 L1/L2 구조 계약만의 승인이다. GC/할당을 포함한 실제 wall-time 상한, malformed tuple materialization, 취소/오류 해제와 Task2 전체를 승인하지 않았다. 추가 성능 실행 없이 이번 수정안을 고정·보존했으며 전체 suite도 미실행이다.

## Task 3 격리 결정과 계약

구현계획의 Ordered Ownership은 Task 1 이후 Tasks 2/3의 독립 진행을 명시한다. 별도 설계 검토에서도 Task 3가 `asyncio.Lock`과 주입 시계만 사용하고, Task 2의 builder·mutation에 의존하지 않음을 확인했다. coordinator는 기존 직렬 실행 결정을 좁게 변경해 Task 3 부품만 별도 작업트리에서 진행한다. Task 2 미완료 상태와 Task 4 이후 통합 차단은 유지한다.

Task 3 부품은 전달된 lock의 동일성을 유지하고, 명시적 FIFO 순서를 보존하며, 이미 제출된 작업이 끝날 때까지 취소된 호출자의 lock을 해제하지 않아야 한다. 전체 ticket 예산은 LOOKUP 1초, COMMIT 1.5초, FILL_APPLY 2.5초, RESTORE 1.5초, PRODUCER_VIEW 5ms다. 예산 초과는 기록하며 제출된 작업을 강제 취소하지 않는다. 지표는 ticket 이력을 무한 보존하지 않는 불변 집계다.

초기 기준선: owner/store 관련 117 passed, 격리 위반 0. 구현자는 Terra/high 요청, 독립 리뷰어는 Sol/high 요청이다. 부품 시험은 fake clock과 event를 사용하며 실제 owner 지연이나 운영 준비를 인증하지 않는다.

공개 인터페이스는 `OwnerTicketGate(lock, *, clock=...)`, `hold(OwnerTicketKind)`, `register_submitted_drain(ticket, future)`, `metrics_snapshot()`이다. 실제 owner 배선은 아직 없다. 향후 호출자는 작업 제출 직후 다음 취소 지점 전에 등록해야 한다. gate 밖에서 직접 lock을 쓰는 경로, 등록하지 않은 작업, 영원히 끝나지 않는 drain의 진행을 이 부품이 보장하지는 않는다.

첫 독립 리뷰의 차단 4건은 다른 event loop의 future·자기 task 수락, caller가 바꿀 수 있는 ticket 상태, 시계 예외에 의한 lock 미해제, 실제 작업을 제출하지 않는 two-commit 시험이었다. 보완본은 불변 ticket과 gate 내부 상태를 분리하고, 호환되지 않는 drain을 등록 전에 거부하며, 진입/종료 시계가 실패해도 잠금과 대기열을 정리한다. 두 순차 commit은 이제 각각 실제 task를 제출·등록해 한 ticket으로 측정한다.

보완 검증: RED는 예상 결함으로 9 passed / 3 failed, GREEN은 12 passed였다. coordinator의 새 UTC 관련 회귀는 아래 명령으로 **129 passed / 10.09s, 격리 위반 0**이었다. 두 파일 문법 검사와 변경 여섯 경로의 비밀키 패턴 검사도 문제를 발견하지 않았다. 이는 전체 저장소 UTC/KST 검증, 실제 store 연결 또는 성능 행렬 완료를 뜻하지 않는다.

독립 재리뷰는 B1–B4 각각 해결, 새 차단 없음으로 spec·code quality 모두 승인했다. 승인 blob은 source `e6456fdb304626a4f53b9f64604270262a0f8b66`, test `8b4cf98fd25be0af4b54f2a488fb078942727fe0`이며 커밋 `9a01fa1`과 일치한다. 실제 staged 두 경로의 whitespace 검사도 통과했다. 리뷰 원문은 `task-3-review.md`, `task-3-rereview-1.md`에 보존했다.

```bash
env -i PATH=/usr/bin:/bin LANG=C.UTF-8 TZ=UTC PYTHONDONTWRITEBYTECODE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
  /home/ubuntu/projects/qwq-ai-trader/venv/bin/python -m pytest \
  tests/test_execution_owner_ticket_gate.py tests/test_execution_command_owner.py tests/test_execution_state_store.py \
  -q -p no:cacheprovider --tb=short --show-capture=no
```

## 다음 작업과 금지할 단축

1. 메모리 비교·1회 CPU 진단·L1/L2 구현 및 독립 구조 검토는 완료했다. 새 세션은 `task-2-retirement-review.md`, `gc-cpu-attribution-report-2026-09-27.md`와 후보 `721fe1c7…`를 먼저 대조한다. 완료한 진단/구조 수정은 반복하지 않으며 이전 `8443703…`용 고정 지문 probe를 새 후보에 그대로 실행하지 않는다.
2. 다음 설계 대상은 L3의 논리 취소와 물리적 graph 정리 수명이다. 취소 즉시 결과는 unavailable로 하되 정리가 끝날 때까지 같은 단일 builder/gate가 소유해야 한다. gate 선반환·무제한 background queue로 비용을 숨기지 않는다. 현재 동기 `close()`와 예외/cancellation 호출자 계약에 영향을 주므로, 설계·호출부 인수와 독립 검토 전에 코드를 확장하지 않는다.
3. 성능 후속은 새 후보의 전체 순회 비용과 미실행 4셀·과거 비-GC/policy tail을 함께 다루는 유한 검증 계획부터 확정한다. 정확한 source/fixture·관측량·판정·중단 조건을 고정하고 실제 성능 인수 전에는 Task4 이후 owner 통합을 진행하지 않는다. 추가 동결/수집·제품 GC 비활성화·임계 상향·입력 축소로 기준을 우회하지 않으며, 원인이 특정되지 않은 실패는 미해결로 보존한다. 이후 Task3 부품의 단독 시험을 실제 통합 인수로 대체하지 않는다.
4. 실제 owner commit/restore·writer/producer 연결, 63셀 성능 행렬·UTC→KST 전체 검증·독립 broad review는 원래 Task 4–15 순서로 진행한다. 전체 C/F/G/R, health/경보 연결과 전체 엔진 운영 전환은 별도 게이트이며 이번 완료 범위가 아니다.

## 재개 위치와 증거 보존

- Task 2 작업트리: `/home/ubuntu/projects/qwq-ai-trader/.claude/worktrees/recovery-projection-20260924`.
- Task 3 작업트리: `/home/ubuntu/projects/qwq-ai-trader/.claude/worktrees/owner-ticket-gate-20260926`.
- 각 작업트리의 `.superpowers/sdd/2026-09-24-recovery-projection-index/`에 ledger·brief·raw log·작성/독립 리뷰가 있다. Task2의 현재 `retirement-candidate-721fe1c7.patch`, 이전 `transient-seal-candidate-8443703.patch`, 기각된 `map-candidate-038aa599-working.patch`는 서로 별개다. `.superpowers`는 Git 제외이므로 정리 전에 반드시 보존 상태를 확인한다. 이 문서 브랜치에는 Task2 코드나 미승인 선행 커밋을 가져오지 않았다.
- 이 문서는 핵심 증거를 추적 가능한 문서로 남긴다. 새 세션은 `git status`, branch/base와 위 diff 식별자를 먼저 대조하고, 미커밋 후보를 자동 reset/clean하거나 승인된 코드로 간주하지 않는다.
- Astra/xhigh·Astra/high·Sol/high·Terra/high·Luna/medium은 역할별 **요청값**이다. 런타임의 actual model/effective effort가 노출되지 않아 미검증으로 기록한다. 같은 제공자의 독립 리뷰를 cross-provider 검증이라고 부르지 않는다.
