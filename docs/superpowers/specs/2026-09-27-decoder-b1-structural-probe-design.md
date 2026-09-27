# Decoder B1 순수 구조 실험 설계

2026-09-27 KST. **B1_STRUCTURAL_PLAN_PROPOSED — 독립 검토 대기.**
기준 `630fc90b973c2fb3fd0e76e946fe8224a57356ce`, 문서 작성만 수행했다.
요청 `gpt-6-astra/high`, actual model/effective effort metadata 미노출로 미검증,
fallback0·재위임0. 작성자는 승인자가 아니다.

## 1. 목적과 판정의 범위

선택된 B1에서 실제 작은 Python cell을 연결·교체·분리했을 때, 반환 후 관측 가능한
slot 관계를 독립 expected와 비교할 **별도 test-only S 실험**을 제안한다.
JSON parser, cold store, 기존 source helper 또는 native 수명 실험을 실행하지 않는다.
작은 action 입력으로 cell을 실제 생성하는 ordinary Python 구조 시험이며 사건 로그만
재생하는 모델 시험이 아니다. 구조 시험이 성공해도 allocation-time ownership은 미증명이다.

별도 계획을 작성할 수 있다는 설계 판단과 실행 승인은 다르다. 현재는 문서2개만 허용한다.
독립 critical reviewer가 두 문서의 범위와 알고리즘을 승인하고 coordinator가 정확한
실행 commit·파일·시험 목록을 dispatch한 뒤에만 아래 새 시험 코드를 작성/실행한다.
사용자의 18KST까지 자율 진행 지시는 routine 확인을 대체하며 native/제품 권한을 늘리지 않는다.

불변 상태는 `TEST_ORACLE_DESIGN_UNRESOLVED / RED_DEFERRED`,
`native_qualified=false`, `source_execution_permitted=false`, qualified runtime0,
source108 call-phase0이다. S 결과는 별도 `S_STRUCTURAL_OBSERVED_ONLY`까지다.
S의 실제 assertion 실패도 원 첫 cold RED와 다른 사건이다.

근거는 [decoder 결정 §9](2026-09-27-recovery-decoder-compatibility-decision.md),
[cold 설계](2026-09-27-recovery-cold-source-ownership-design.md), 그리고 coordinator의
`.superpowers/sdd/2026-09-27-runtime-admission-contract/`에 보존한
`decoder-feasibility-next-slice.md`, `decoder-feasibility-review.md`,
`decoder-feasibility-rereview.md` 전체다. F1/F2의 서면 CLOSED를 실행 증거로 사용하지 않는다.

## 2. 허용 파일과 독립성

후속 구현의 새 파일은 아래 다섯 개로 제한한다. 기존 파일은 수정하지 않는다.

| 파일 | 책임 |
| --- | --- |
| `tests/structural_b1/subject.py` | private 실제 cell/operation과 한 구조 전이씩 진행하는 subject |
| `tests/structural_b1/observer.py` | 반환된 cell의 weak census, 실제 slot 독해, scalar snapshot 비교 |
| `tests/structural_b1/fixtures.py` | 작은 literal action·expected, N을 반복하는 driver; subject 내부 helper 재사용0 |
| `tests/structural_b1/mutants.py` | 실제 link/alias/allocator 동작을 바꾸는 test-scoped 변이 |
| `tests/structural_b1/test_structure.py` | 독립 판정과 정상·반례·관측 한계 시험 |

subject와 observer는 별도 작성자가 담당한다. observer는 subject의 정확한 타입과 slot
계약만 소비하며 traversal/retire/count/owned/complete helper를 호출하지 않는다.
fixture의 기대 순서·tag·edge/domain은 subject 실행 전에 literal로 정한다.
미래 경로/API는 계획상의 이름이며 현재 실제 constructor site·SHA·행번호가 아니다.
코드가 생기면 독립 reviewer가 생성 경로 전부와 관측 경계를 실제 행/SHA에 결속한다.

`src/`, 기존 tests/helper/proofs, SQL, `json.loads`/marshal/ctypes/native layout,
기존 frozen source/sequence 후보 import·복사·실행, CI/config/registry 수정은 금지한다.
새 파일의 import closure는 Python stdlib의 `math`, `weakref`, `dataclasses`, `typing`,
시험 모듈 loader의 `importlib.util`, `sys`, `pathlib`와 pytest, 위 다섯 파일뿐이다.
기존 `tests/conftest.py` 격리는 그대로 적용한다.
observer 전용 list/dict/set은 허용하지만 subject의 graph·작업 stack을 그런 자료로 숨기지 않는다.

## 3. exact layout과 domain

모든 cell은 정확히 같은 private slotted `Cell` 타입이다. subclass·`__dict__`·`__del__`·
descriptor callback0. slot은 `tag, payload, prev, next, s0, s1, s2, c0, c1, c2, c3, __weakref__`다.
`prev/next`는 allocation chain 전용이다. semantic과 control slot은 아래처럼 분리한다.
명시되지 않은 slot은 반드시 None이다. payload는 fixture가 빌려주는 작은 exact scalar뿐이다.

| exact tag | semantic slot | control slot | payload |
| --- | --- | --- | --- |
| EMPTY | 없음 | 없음 | None; constructor 반환 직후만 |
| OBJECT | s0=첫 ENTRY | 없음 | None |
| ENTRY | s0=KEY, s1=value, s2=다음 ENTRY | 없음 | None |
| ARRAY | s0=첫 ITEM | 없음 | None |
| ITEM | s0=value, s1=다음 ITEM | 없음 | None |
| KEY | 없음 | 없음 | exact str |
| NULL/BOOL/INT/FLOAT/STR | 없음 | 없음 | 각각 None/bool/int/float/str; bool≠INT |
| WORK | 없음 | c0=retire target, c1=다음 WORK | None |
| CONT | 없음 | c0=현재 container, c1=완성 대기 value, c2=대기 KEY, c3=이전 CONT | None |

실험은 float NaN/±Infinity를 FLOAT에 임시 보유할 수 있다. `-0.0`은 FLOAT와 부호를
보존한다. KEY와 STR은 별개다. scalar를 immutable이라며 type 구분을 생략하지 않는다.
각 semantic node는 semantic 부모가 최대 하나인 tree이며 공유 child/cycle은 구조 실패다.
CONT.c0의 container alias, WORK의 target, chain/holder alias는 허용되는 별도 control domain이다.
책임 영역 하나라는 말은 강한 참조가 하나라는 뜻이 아니다.

bootstrap의 `Supervisor.active -> Operation -> Arena/Holders`는 먼저 만들어진 고정 shell이다.
`Arena`는 `head, tail, root`; `Holders`는 `new, replacement, cursor, unlink, left, right,
work_head, cont_head, lookup, pending_key`의 고정 slot만 가진다. Operation은 `token, arena,
holders, phase, action, fault, outcome`을 가진다. Supervisor는 `active`만 가진다.
token은 fixture의 exact `(1, 2, 3, 4)` tuple이며 값 일치뿐 아니라 **동일 객체**를 요구한다.
이는 제품 OwnerToken 또는 native capability가 아니다. driver가 bootstrap에서 받은 원 operation을
별도 인자로 전달하며 매 submit/tick의 `supervisor.active is operation`을 검사한다.
현재 active를 다시 읽어 자기 자신과 비교하는 검사는 wrong-operation 대조가 아니다.

phase/action/outcome은 유한 문자열/작은 action tuple이고 graph를 담지 않는다. fault는 원 예외
단일 slot이다. bootstrap 자체의 allocation/frame 수명은 S의 인수 대상 밖이며 A 공백으로 기록한다.
CONT는 중첩 construction의 책임을 유지한다. Python 재귀/생존 generator frame으로 깊이를 숨기지
않는다. lookup은 한 ENTRY씩 진행하고 한 tick에 전체 list/map을 탐색하지 않는다.

열린 container는 해당 CONT.c0만이 construction root로 보유하며, `end` 전에는 부모의
semantic graph에 붙이지 않는다. 부모 CONT.c1도 이 열린 자식을 중복 보유하지 않는다.
`end`에서 완성 root를 부모 CONT.c1로 옮긴 뒤 ENTRY/ITEM에 게시한다. 최상위만 arena.root로
옮긴다. action 완료 IDLE에서는 c1=None이고, c0는 각기 분리된 partial root다.
KEY action 뒤 c2는 다음 value를 위한 대기 KEY이며 아직 ENTRY에 게시되지 않았다.
이 규칙 덕분에 partial dispose가 공유 graph의 임의 dedup 알고리즘을 필요로 하지 않는다.

## 4. 정상 전이와 중간 정지점

`tick`은 재귀/await/callback 없이 최대 한 cell constructor 호출 또는 최대 한 graph 참조 slot
대입을 수행한다. phase/scalar 상태 변경은 별도다. 이 선택은 **관측용 세분화**이며 실제 제품의
256facts 또는5ms 구현이 아니다. constructor 내부 변경/allocator 작업은 이 수치에 포함됐다고
주장하지 않는다. observer는 매 tick 반환 뒤 실제 slot을 읽는다. constructor wrapper 내부는
별도 IN_OBSERVER_FRAME 정지점이고 NORMAL 전이 완료/TERMINAL과 섞지 않는다.

| 단계 | 선행/변경 순서 | 중간 상태의 책임 |
| --- | --- | --- |
| 생성 | active/token 확인 → 빈 new 예약 phase → `_new_cell()` 결과를 new에 저장 → chain append → tag/payload 설정 → semantic/control 게시 → new=None | constructor 반환 gap은 A 공백. 반환 후는 wrapper local 또는 new가 보유 |
| 빈 chain append | new.prev=None/new.next=None → head=new → tail=new | head만 설정된 동안 new와 head가 보유, APPEND_SINGLE phase |
| 일반 append | new.prev=tail → tail.next=new → tail=new | 기존 tail과 new가 양쪽 책임 유지. 정상 chain 대칭은 완료 뒤 검사 |
| duplicate 게시 | replacement=entry.s1 → entry.s1=new root → CONT.c1=None | old root는 replacement와 chain, new root는 chain/entry. 최초 ENTRY/KEY 위치 유지 |
| retire 진입 | old root용 WORK 완성·work_head에 연결 → replacement=None | 한 배치에 old root와 중복 입력 KEY가 속한다. 둘의 seed WORK만 별도 seed로 표시 |
| child 분리 | cursor의 s0/s1/s2 순서로 child WORK를 먼저 생성·연결 → 해당 semantic slot=None | WORK.c0와 chain이 child를 보유한 뒤 원 edge를 끊음 |
| work pop | cursor=WORK.c0 → work_head=WORK.c1 → WORK.c0=None → WORK.c1=None → 빈 WORK 직접 unlink | pop 중 consumed WORK는 unlink holder가 보유. cursor와 work_head 인계가 먼저 |
| CONT pop | c0/c1/c2가 이행/정리됐는지 확인 → cont_head=CONT.c3 → c3=None → 빈 CONT 직접 unlink | CONT를 정리할 WORK를 만들지 않는다. 아직 live인 container는 semantic/상위 CONT 경로가 유지 |

work pop 전에 `unlink=현재 WORK`를 잡고, cursor가 비었을 때만 pop한다. cursor의 semantic
child를 모두 work로 옮긴 뒤 그 cell을 unlink하고 cursor를 비워 다음 work를 pop한다.
WORK.c1은 semantic edge가 아니며 work 처리에 새 WORK를 생성하지 않는다.
유한 원 semantic edge `(generation, s-slot)`는 정확히 한 번 끊고 child WORK도 한 번만 만든다.
root/중복 KEY seed는 원 semantic edge 방문 수와 분리한다. 정상 pop의 constructor 도달 수는0이다.
따라서 유한 graph에서 새 work를 만드는 사건은 유한 semantic edge와 seed뿐이다.
할당 실패 없이 한 tick씩 계속 진행하면 retire가 끝난다. OOM 진전 보증은 없다.

### unlink의 head/tail/단일 cell 계약

semantic/control slot이 모두 빈 cell만 unlink할 수 있다. `unlink=x`, `left=x.prev`,
`right=x.next` 순서로 기존 holder에 잡은 뒤 아래 대입을 한 tick씩 수행한다.

| 경우 | 이웃/anchor 변경 순서 | 마감 순서 |
| --- | --- | --- |
| 가운데 L←x→R | L.next=R → R.prev=L | x.prev=None → x.next=None → left=None → right=None → unlink=None |
| head x→R | arena.head=R → R.prev=None | 동일 |
| tail L←x | L.next=None → arena.tail=L | 동일 |
| 유일 cell x | arena.head=None → arena.tail=None | 동일 |

cursor가 x를 가리키면 unlink 마감 뒤 cursor=None도 별도 tick이다. chain에 없고
unlink/cursor에만 남는 cell은 UNLINKING domain이며 terminal live orphan과 구별한다.
각 중간 phase에 observer는 위 순서에서 허용한 비대칭만 받아들인다. 이웃을 먼저 해제하거나
빈 head인데 낡은 tail이 정상 완료까지 남는 변이는 실패한다. 정상 마지막 cell 후 head/tail과
모든 holder가 None이어야 한다. cycle을 자동 GC가 언젠가 치워 준다는 가정으로 통과하지 않는다.

## 5. 중첩·실패·결과

한 번에 활성 retire 배치는 하나다. old root 및 duplicate KEY seed가 같은 배치에 속하며
이 둘이 모두 끝나기 전 다음 action의 construction을 받지 않는다. 안쪽 `b`를 retire하는
동안 바깥 `a`의 교체 전 old array는 live semantic graph에 남는다. 새 `a` object와 그 안의
old/new `b`도 함께 생존 가능하다. 조상 old를 retire backlog로 세지 않으며 전체 live 수나
깊이가 두 subtree로 제한된다고 주장하지 않는다.

action `known_failure`는 작은 code를 outcome에 보존하고 같은 arena의 모든 partial graph,
CONT/KEY/work를 정상 순서로 정리한다. 이는 syntax/legacy exception parity가 아니다.
예상 밖 예외는 원 예외를 `op.fault`에 붙이고 DISPOSAL_FAULT로 멈춘다. fault 시 새 action/
tick mutation을 거부하고 아직 연결된 chain과 holder를 그대로 유지한다. 부분 unlink에서도
left/right/unlink/cursor를 강제로 비우지 않는다. 이는 정상 pin·cleanup 성공이 아니다.
예외 args/context/cause/traceback·호출 frame의 참조는 FAULT_RETAINED domain으로 별도 관측한다.
임의 str/repr/frame clear/강제 GC0. fault 저장 자체 실패·진짜 OOM의 안전성은 미증명이다.

subject에는 writer/gate/Future/certificate가 없다. DISPOSAL_FAULT를 보고 제품 writer0가
시험으로 입증됐다고 기록하지 않는다. 정상 `finish`는 최종 살아 있는 tree의 nonfinite를
검사하고 READY 또는 KNOWN_ERROR를 작은 outcome으로 남긴다. READY의 private arena/root는
계속 operation이 보유한다. `dispose` 완료가 S의 terminal 후보이며 consumer take는 없다.
죽은 weakref는 Python referent 부재의 보조 신호일 뿐 native free가 아니다.

전체 dispose/known_failure는 IDLE에서만 받는다. arena.root와 각 CONT.c0의 분리된 partial
root, CONT.c2의 미게시 KEY를 같은 단일 retire 배치의 seed로 먼저 보유한 뒤 원 slot을 비운다.
IDLE에서 c1가 남았으면 구조 결함이다. seed들을 모두 drain한 뒤 빈 CONT를 pop한다.
lookup/pending_key는 action 완료에서 None이어야 하므로 숨은 seed를 만들지 않는다.
정상 완료를 chain의 남은 cell 무차별 drop으로 구현하지 않는다. 중간 foreign fault는 이
정상 dispose로 진입하지 않고 당시의 모든 holder를 보존한다.

## 6. 관측 S의 실제 책임과 한계

observer가 실제 `_new_cell`을 test-scoped wrapper로 감싸 원 constructor를 정확히 한 번
호출한다. 반환된 실제 cell에 weakref와 관측 monotonic generation을 연결한다. registry는
weakref+scalar만 보유하고 cell/frame/traceback을 저장하지 않는다. wrapper의 local은 살아
있는 동안 IN_OBSERVER_FRAME 책임이며 `tick`이 반환한 뒤 wrapper frame은 끝나야 한다.
wrapper 반환 전 `new` slot이 비었다는 사실을 orphan으로 판정하지 않는다.

`snapshot`은 실제 모든 census-live cell의 exact slots와 supervisor/operation/anchor/holder를
읽어 scalar generation graph로 복사한다. kernel helper를 쓰지 않고 forward/backward chain,
semantic tree, CONT/WORK, phase별 transient 경로를 재구성한다. 원 semantic edge ledger도
교체 직전 snapshot에서 독립 생성한다. cell당 count만 일치해도 다른 identity/slot이면 실패다.
해당 함수의 역참조 local은 함수 반환까지 관측 domain이다. scalar snapshot이 돌아온 뒤에만
다음 subject tick을 실행한다. snapshot/expected는 구조·사건 검증용이며 실제 free/latency 증거0.
전체 snapshot은 직전/현재 두 개만 보유하고 edge별 scalar 방문 ledger와 고정 실패 문맥만
남긴다. 모든 tick snapshot을 누적 보관하는 trace는 만들지 않는다.

terminal probe는 driver·wrapper·snapshot 호출 frame이 모두 반환한 뒤 별도 함수에서 수행한다.
테스트 본문은 Supervisor/원 Operation shell과 weak census만 보유하고 cell handle을 보관하지 않는다.
probe는 weakref를 하나씩 역참조하고 scalar generation만 반환한다. probe의 마지막 local도
반환 뒤 사라진다. 순환 참조가 남으면 살아 있는 census가 terminal을 거부하며 GC 호출로 통과시키지
않는다. 의도한 hidden alias/exception/closure 변이는 외부 test holder를 probe 종료까지 유지한다.
fault의 operation은 유지하므로 terminal-disposed를 기대하지 않는다.

observer/census의 MemoryError·기록 누락·snapshot 실패는 `INCONCLUSIVE_OBSERVER`다.
subject fault로 바꾸거나 intended RED로 계산하지 않는다. observer는 `ObserverAbort(BaseException)`로
중단을 전달하고 subject의 foreign fault catch는 `Exception`만 받는다. wrapper 오류는 test driver가
별도 표시해 관측을 중단하고 보유 operation을 유지한다. 예외 traceback은 pytest 실패 출력에 graph가
들지 않게 실제 객체 repr 없이 고정 code로 보고하되 fault 강제 clear는 하지 않는다.

constructor 우회 cell이 chain/holder에 나타나면 census-missing을 검출한다. 반환 전 생성됐다
사라진 cell이나 chain 밖 hidden bypass cell은 S가 놓칠 수 있다. 그런 대조는
`INCONCLUSIVE_COVERAGE`가 정답이며 S의 전체 할당 포괄성을 주장하면 oracle 불합격이다.
이 한계는 소스 감사만으로 native A 통과가 되지 않는다. bootstrap·builtin 임시 객체·constructor
부분 실패·반환 gap·실제 deallocation은 계속 A의 미해결 영역이다.

## 7. 독립 fixture와 반례 인수

작은 literal sequence와 N=4097 사례는 구조 인수다. N은 array 항목 수이며 array/item/scalar/
work의 실제 cell 수를 N이라고 뭉뚱그리지 않는다. N개의 action/value list를 미리 만들지 않고
fixture driver가 같은 작은 scalar action을 반복한다. 입력 cap·성능 benchmark가 아니다.

| 사례 | 독립 기대 관계 |
| --- | --- |
| 빈 object/array, 단일 scalar, 세 cell unlink | head/tail/유일/가운데 전이표, 완료 뒤 모든 slot 해제 |
| root object에 a=array(N), b=True, a=7 | 최초 key 순서 a,b; a exact INT7/b BOOLTrue; old array+중복 KEY만 retire |
| a=NaN 또는 +Infinity/-Infinity, 뒤 a=7 | 임시 FLOAT 허용, 최종 a INT7, 덮인 nonfinite 최종 검사 영향0 |
| 최종 a=NaN/+Infinity/-Infinity | finish KNOWN_ERROR, dispose 후 구조 terminal; parser parity 주장0 |
| true, 1, 1.0, -0.0, null, "1.00" | BOOL/INT/FLOAT/FLOAT/NULL/STR 및 음의 zero 부호·문자열 spelling |
| 바깥 a=array(3) 뒤 새 a object 내부 b=array(3), b=7 | inner retire 중 바깥 old a는 live; inner 배치≤1; outer 교체/retire는 뒤에 |
| partial object+empty child 뒤 known_failure | 원 code 보존, 부분 CONT/KEY/semantic/work 영역 모두 정리 |
| 생성 뒤 append 중간·unlink 이웃 변경 뒤 foreign failure | same operation/fault/holder 보유, DISPOSAL_FAULT, terminal-disposed 불허 |

실제 변이는 owner link/token 불일치, 반환 cell chain 등록 누락, old replacement holder 생략,
old tuple 은닉, 다음 sibling 조기 시작, work-next를 semantic처럼 재처리, work-pop에서 새 WORK
생성, head/tail/단일 unlink 누락, 마지막 leaf hidden alias, exception/closure cell 보유다.
각 반례는 실제 slot·alias·constructor 동작을 바꾼다. 제품 count나 observer bool만 바꾸는
변이는 인수에서 제외한다. owner/token 대조는 constructor 도달0까지 독립 wrapper로 확인한다.
constructor 우회와 observer 오류 대조는 구조 RED가 아닌 한계 판정을 검증한다.

매 tick의 전체 scalar snapshot을 직전 snapshot과 비교해 graph slot 변경≤1 또는 새 cell1의
서로 배타 조건을 검사한다. 최초 bootstrap/constructor 내부는 이 판정 밖임을 표시한다.
N=4097에서도 동일 oracle를 줄이거나 표본 추출하지 않는다. 비용이 예산을 넘으면 UNRUN/
INCONCLUSIVE로 보존하고 S 인수를 완료로 표시하지 않는다. 관측 비용은 성능 증거로 쓰지 않는다.

## 8. 실행 profile와 뒤에 남는 gate

후속 S만 기존 guarded pytest의 standard 개발 profile로 실행한다. 별도 source/native lane,
capsule 자격, OS 종료 controller, SQLite 또는 외부 실행기는 사용하지 않는다. 새 install0,
network/API/SSH/deploy/restart0, 자격·운영 상태 접근0, 환경/권한 확장0이다.
순수 Python object/weakref 사용을 C 메모리 layout 관측으로 확장하지 않는다.

coordinator가 구현 뒤 exact import closure와 수집 목록을 검사해 일반 개발 시험임을 확정한다.
source marker를 이름만 바꾸거나 frozen helper를 복사했다면 **실행 전 거부**한다.
기존 conftest가 차단하면 우회 없이 중단한다. 기존 source108의 명시 수집·call-phase는 이 계획에 없다.
통합 전체 UTC/KST는 coordinator의 기존 standard 검증만 사용하고 source lane 활성화0이다.

256facts 또는5ms/raw50ms/restore1000ms/ticket1500ms와 cold/cleanup timer는 불변이다.
S 성공으로 이 수치를 충족했다고 주장하지 않는다. native A·parser/encoding/scalar 비용·실제
consumer·cold snapshot/SQL/owner 배선·full L3는 계속 별도 gate다. 두 과거 성능 예외는 새 실패/
timeout/수명 결함을 면제하지 않는다. 정확한 Do/See 순서는 [실행계획](../plans/2026-09-27-decoder-b1-structural-probe.md)에 둔다.
