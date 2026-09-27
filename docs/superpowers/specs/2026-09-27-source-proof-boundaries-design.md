# Source 증명 시험의 수집·격리 경계

2026-09-27 KST. 기준 `711389227ce60e2d34740d4c20e45cf394fbda44`.
상위 [조건부 source 설계](2026-09-27-required-source-proof-design.md)의 §4–5를
구현하는 개발 전용 단계다. 사용자는 18시 KST 이후 복귀 전까지 순차 자율 진행을 지시했다.
통상적인 사용자 승인 대기는 이 지시로 대체하되, 문서·독립 검토·시험 경계는 유지한다.
main/운영·설정·주문·외부 API·권한 변경은 포함하지 않는다.

## 목적과 대안

현재 미커밋 source 후보를 일반 회귀와 분리하고, 명시 수집과 직접 child가 정확한
격리 guard를 사용하는지 검증한다. 개발자가 수집 성공을 native 자격으로 오해하지 않아야 한다.

- 채택: helper bytes 보존, non-default cases 경로, 독립 inventory, 무조건 fail-closed인
  native 실행 경계. qualified runtime이 없어도 수집·격리 동작을 완결할 수 있다.
- 보류: 현 host의 Python3.12.3을 qualified로 취급. native build/closure·격리 증거가 없다.
- 제외: 기본 suite 전체의 Python 버전 축소, skip/xfail, 전역 ignore, 기존 시험 삭제.

## 파일과 불변식

원본은 `l3-source-proof-20260927` worktree의 새 파일 두 개이며 읽기 전용으로 보존한다.

| 목적 | 새 파일 | 불변식 |
| --- | --- | --- |
| helper | `tests/proofs/l3_source/l3_source_lease_probe.py` | SHA256 `32725179ac8695fdb8dcb87b969159a306899c238bac355e1050c9e607e1e55e` |
| cases | `tests/proofs/l3_source/source_lease_cases.py` | 원본 SHA256 `35906c3c7b59c8addb852f97bf26c43621756ddb7f18f54ca98412bcfe8771a8`; oracle/parameter 불변 |
| expected | `scripts/dev/source_proof_inventory.py` | 함수/parameter literal로 독립 작성한 source108·related31 sorted tuple |
| 경계 시험 | `tests/dev/test_source_proof_boundaries.py` | 작은 subprocess·정적/합성 검사만; source native 실행0 |
| inventory 시험 | `tests/dev/test_source_proof_inventory.py` | 정확한 identity·정렬·중복·부분집합 거부 |

helper의 CPython 검사는 모든 호출 경로를 보호하지 않는다. cases가 직접 `_read_source`를
호출하므로 새 실행 차단이 fixture/seed/SQLite보다 앞서야 한다. 이 helper는 GC referent와
refcount를 관측하며 raw-pointer/ctypes layout probe라고 부르지 않는다.

## cases bootstrap

stdlib-only bootstrap이 pytest·제품·helper import보다 먼저 실행된다.
root는 `Path(__file__).resolve().parents[3]`, guard는 root의 `tests/conftest.py`다.
고정 guard SHA256은 `7b7b26940309a2a165d9bdba7611b6730e83b90ae9ea5f9e725764ee4c0a23f7`.
실행 전 hash를 대조하고 canonical file을 나타내는 실제 module object가 정확히 하나임을
확인한다. direct child는 정확한 파일을 명시 load하고 pytest 경로는 이미 설치된 object를
사용한다. 다른 경로의 `conftest`, 중복 object, 비정상/비어 있지 않은 실제 `VIOLATIONS`는
고정 오류로 거부한다. `__getattr__`를 호출하지 않고 module namespace를 관측한다.

`--source-guard-child`만 제품/helper import 없이 아래 record 한 줄과 exit0을 허용한다.
`{"schema":"qwq.source-guard/v1","guard":{"path":"tests/conftest.py","sha256":"<위 고정값>","module_count":1,"violations":0}}`
실제 module/list 관측으로 만든다. 실패는 짧은 고정 오류·nonzero이며 traceback에 환경을 싣지 않는다.
guard mode는 정확히 인수 하나만 받는다. fault mode는 기존
`--source-fault-child <tmp-db-path> <fault> <mutation>` 네 인수 형식을 유지하되 아래 이유로
실행을 거부한다. 잘못된/추가 인수는 거부하며 tmp-db-path를 열거나 출력하지 않는다.

`--source-fault-child`는 이번 단계에서 제품 import 전에
`source_proof_native_subject_unqualified`로 거부한다. pytest 수집은 허용하되 `setup_module()`이
동일 이유로 모든 call-phase를 hard-fail한다. 버전 문자열/환경변수/사용자 인수로 열 수 없다.
실제 sentinel에서 원본 `native_connection_provenance` 및 function-scope fixture·seed·connect·source
호출0을 관측한다. pytest 자체의 session-scope autouse fixture0을 주장하지 않는다.
sentinel은 호출을 기록한 즉시 raise하며 원 seed/SQLite/native 함수를 호출하지 않는다. xunit 순서가 이를
보장하지 못하면 구현을 중단하고 별도 nested hook 설계 검토를 받는다. global guard는 고치지 않는다.
helper import도 canonical destination path와 단일 object를 검증한다.

## fault 기록과 기존 oracle

기존 `FAULT_RECORD`, `parse_fault_record`, 변이 단언은 그대로 보존한다.
향후 qualified 실행이 열릴 때를 위해 출력만 strict envelope로 감싼다:
`{"schema":"qwq.source-fault/v1","guard":<위 실제 guard>,"record":<기존 fault dict>}`.
새 `parse_fault_envelope(raw: str | bytes) -> dict`는 UTF8 최대4096bytes만 받고
중복 key·추가/누락 field·bool/int 혼동·잘못된
guard identity·위반 수를 거부하고 기존 parser에 record를 전달해 기존 dict를 반환한다.
기존 parent는 새 outer parser를 사용한다. mutation tests의 기존 inner parser는 바꾸지 않는다.
native fault child는 미실행이므로 이 envelope의 시험은 합성 검사일 뿐 실제 child 증거가 아니다.
기존 단일10초 deadline, 한 줄, 관측 후 child 생존, 의도된 terminate/reap 의미를 보존한다.
child PYTHONPATH는 root/tests와 root 둘만; HOME/운영 환경을 전달하지 않는다.

## 수집 인수와 검증 범위

expected는 실제 collection 결과에서 생성하거나 자동 갱신하지 않는다. source23함수의
parameter literal을 정적으로 확장한108node와 관련3파일의10+12+9node를 별도 고정한다.
관련 파일: `test_execution_state_store.py`, `test_execution_owner_ticket_gate.py`,
`test_execution_policy_registration_boundaries.py`. 기존 경로/일반 회귀 포함을 유지한다.
inventory module은 stdlib-only이며 tuple·canonical JSON SHA256·정확한 actual 비교 함수만 제공한다.

승인 후 base의 실제 기본수집 집합을 기록하고 통합본의 수집과 비교한다. 기존 집합 삭제0,
source prefix0, 추가는 이번 새 경계/inventory 시험뿐이어야 한다. source 명시 집합108과
related 명시 집합31은 정확히 각각 대조한다. 실제 pytest collection 오류/중복/deselection은 실패다.
collection만 수행하는 pytest는 guard 자동 로딩/실제 위반0을 별도 확인하며 test PASS라고 부르지 않는다.

각 task의 RED→GREEN과 독립 리뷰 뒤 전체 UTC→KST를 직렬 실행한다. 기존16skip/2xfail은
이번 변경이 만든 통과가 아니며 기존 identity/조건을 보존한다. source native108 실행,
runtime qualification, OS outcome, CI 필수 gate·원격 보호는 미실행/후속으로 명시한다.
기존 성능 예외는 유지하되 새 오류/timeout/정합성 실패로 확대하지 않는다.

## 순서와 완료 판정

두 독립 구현(source bootstrap/경계 시험, inventory/시험)은 같은 base의 별도 worktree로 나눈다.
최종 reviewer는 구현자가 아닌 Astra/xhigh. 성공은 SOURCE_BOUNDARIES_VALIDATED일 뿐
source proof 전체 완료나 엔진 승격이 아니다. 다음 독립 단계는 실제 OS 종료·회수 관측기다.
