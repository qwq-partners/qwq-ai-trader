# Decoder B1 구조 실험 설계 — B1a 원시 전이 제안

2026-09-27 KST. **B1A_PARTIAL_PLAN_PROPOSED — 독립 재검토 전 Do/실험0.**
원525줄 B1 문서는 `d9d57e533582b9d2183a145c14f31c2fea313b2b`에 보존한다.
요청 gpt-6-astra/high, actual model/effective effort metadata 미노출=unverified,
fallback0·재위임0. 작성자는 승인자가 아니다. R1–R4 처분 제안이며 원 B1 인수 완료가 아니다.

## 1. 단계 분리와 권한

재검토 대상 부분 Do는 **B1a: EMPTY cell 생성·chain append·holder 인계·unlink**뿐이다.
실제 Python cell의 slot을 독립 observer로 읽는 ordinary test-only S 실험을 제안한다.
CONT/end/lookup/ENTRY·ITEM 게시/duplicate/retire/known_failure/finish/dispose는 모든 중간
전이가 고정되지 않았으므로 **full B1 미승인·UNRUN**이다. R2 full CLOSED를 주장하지 않는다.
N4097도 후속 gate다. B1a 성공의 최대 표현은 `B1A_PRIMITIVES_OBSERVED_ONLY`이며
원 `S_STRUCTURAL_OBSERVED_ONLY`가 아니다. 이 분리는 개발 순서이며 원 인수를 축소해 통과한 것이 아니다.

현재는 두 문서와 coordinator 지정 보고서만 작성한다. 같은 독립 critical reviewer의
재검토와 coordinator의 정확한 commit·파일 dispatch 전 코드 작성/수집/실험0이다.
사용자 자율 위임은 routine 확인을 대체하며 native/제품/운영 권한을 늘리지 않는다.
`TEST_ORACLE_DESIGN_UNRESOLVED / RED_DEFERRED`, `native_qualified=false`,
`source_execution_permitted=false`, qualified runtime0, source108 call-phase0 유지.
최초 allocation·부분 실패·반환 gap·actual free의 A와 첫 cold RED는 여전히 미해결이다.

근거: [decoder 결정 §9](2026-09-27-recovery-decoder-compatibility-decision.md),
[cold 설계](2026-09-27-recovery-cold-source-ownership-design.md), coordinator artifact의
`decoder-feasibility-review.md`, `decoder-feasibility-rereview.md`,
`decoder-b1-plan-independent-review.md`, `decoder-b1-plan-revision-brief.md`.
F1/F2의 준비 설계 CLOSED는 실행 증거가 아니다.

## 2. 파일·독립성·exact layout

후속 새 파일은 `tests/structural_b1/subject.py`, `observer.py`, `fixtures.py`, `mutants.py`,
`test_structure.py` 다섯 개뿐이다. 각각 실제 전이, weak census/slot oracle, literal expected,
실제 동작 변이, 인수 시험을 담당한다. subject와 observer/expected 작성자는 서로 다르다.
observer는 타입/slot 계약과 이 표만 소비하고 subject의 phase/traversal/count/owned/complete
helper를 답안으로 사용하지 않는다. 두 작성자 dispatch 전에 spec SHA와 expected를 동결한다.
기존 src/tests/helper/proofs/guard/CI/config/registry 수정0, frozen source/sequence 복사·import0.
import closure는 stdlib weakref/dataclasses/typing, sibling loader의 importlib.util/sys/pathlib,
pytest 및 다섯 파일뿐이다. test_structure만 os.environ의 고정 `QWQ_B1_RAW_MUTANT` 값을
읽을 수 있다(짝 계획의 raw 대조 선택, 파일/실행/운영 환경 접근0). 기존 conftest 격리를 유지한다.
SQL/native/외부 API0.
observer의 scalar list/dict/set은 허용하되 subject graph를 collection/frame/generator에 숨기지 않는다.

Cell은 exact private slotted 타입, subclass/`__dict__`/`__del__`/custom descriptor0이다.
slot=`tag,payload,prev,next,s0,s1,s2,c0,c1,c2,c3,__weakref__`.
B1a cell은 전 수명 `tag="EMPTY"`, payload와 모든 s/c=None이다. full B1의 tag 초기화와
semantic graph는 아직 구현하지 않은 원시 단계이며 이 결과로 full tag 계약을 입증하지 않는다.
고정 shell: Supervisor=`active`; Operation=`token,arena,holders,phase,action,fault,outcome`;
Arena=`head,tail,root`; Holders=`new,replacement,cursor,unlink,left,right,work_head,cont_head,lookup,pending_key`.
root와 사용하지 않는 holder는 항상 None. bootstrap은 head/tail과 모든 holder=None,
phase=IDLE, action/fault/outcome=None이며 Cell0이다. shell 생성 자체는 A 밖이다.
token은 fixture가 전달한 exact `(1,2,3,4)` tuple의 **동일 객체**다. driver가 bootstrap 원
operation을 별도 전달하고 submit/tick이 active is 원 operation, token is 원 token을 검사한다.
현재 active를 다시 읽어 자기 자신과 비교하거나 값 동등성으로 대체하지 않는다.
bootstrap은 길이4·각 값이 exact int 1/2/3/4인 exact tuple만 받는다.

action은 exact `(name,None)` tuple, name은 `append_empty`, `unlink_head`, `unlink_tail`,
`unlink_middle` 네 개뿐이다. Cell 입력/반환0. append는 정상 IDLE chain에서 받는다.
head/tail은 nonempty chain, middle은 **정확히 세 cell chain의 가운데**만 받는다.
middle 검사는 head.next=x, x.next=tail, head.prev=tail.next=None 및 역방향 link를 고정 길이로
대조하며 임의 lookup을 구현하지 않는다. fixture 최대 세 live cell은 제품 cap이 아니다.
malformed/unknown/busy/fault/빈 unlink/wrong middle/identity 오류는 ValueError로 사전 거부하며
constructor0·graph/phase/action/outcome 불변이다. 사전 거부는 foreign fault latch가 아니다.

## 3. 동결할 B1a 전체 전이표

phase는 **다음 tick이 수행할 행**이다. submit은 IDLE에서 action을 저장하고 append=A0,
unlink=U0로 바꾼다(graph0/constructor0). tick은 한 행 뒤 반환한다. 마지막 행에서
action=None/phase=IDLE로 돌아간다. 정상 outcome=None. IDLE tick은 사전 거부다.
표에 없는 existing graph delta0, tag/payload/s/c 불변. scalar phase 변경은 graph 쓰기와 별개다.

**A0=ALLOCATE_TO_NEW**만 constructor 도달1/반환1 + 새 EMPTY generation1 +
빈 new에 그 generation 저장1을 한 관측으로 허용한다. 다른 기존 graph delta0이다.
constructor 내부와 반환→new 저장 gap은 A 제외 영역이다. 다른 행은 constructor0·새 generation0,
정확히 지정 slot에 실제 대입1이다. None→None 행도 실제 대입1/순변화0이다.
snapshot은 **순변화**만 검증한다. 같은 slot 두 번 쓰기/변경 후 복원은 snapshot만으로
배제하지 못한다. 독립 source reviewer가 tick 분기와 실제 쓰기 위치를 SHA·행에 결속한다.
별도 tracing0, 관측되지 않은 중간 쓰기를 snapshot으로 증명했다는 주장0.

기호: n=새 generation, t=append 전 tail, x=unlink 대상, L/R=x의 원 prev/next.
alias는 앞 행에서 도입해 아직 비우지 않은 것만 허용한다. chain/anchor/holder의 동일 cell
참조는 허용하며 semantic alias는 없다. 각 행의 fault 보존은 아래 F를 정확히 적용한다.

| before phase / 선행 | 정확한 slot 대입/생성 | after | 필수 보유·허용 alias / fault |
| --- | --- | --- | --- |
| A0 / new=cursor=None | ALLOCATE_TO_NEW: new=n | A1 | n은 new; F |
| A1 / new=n,n.prev=None | n.prev=arena.tail (빈 경우 None) | A2 | new=n, t는 기존 chain; F |
| A2 / 빈 chain | arena.head=n | A3 | head/new=n, tail=None 일시 허용; F |
| A2 / nonempty,tail=t | t.next=n | A3 | t.next/new=n,n.prev=t,tail=t; F |
| A3 / A2 완료 | arena.tail=n | A4 | 정상 양방향 chain,new/tail=n; F |
| A4 / cursor=None | cursor=new | A5 | new/cursor=n,chain도 보유; F |
| A5 / new=cursor=n | new=None | A6 | cursor=n,chain도 보유; F |
| A6 / new=None,cursor=n | cursor=None | IDLE | 모든 holder None,정상 chain; F |
| U0 / 정상 chain,holders 모두 None | cursor=선택 x(head/tail/middle) | U1 | cursor/chain=x; F |
| U1 / cursor=x | unlink=cursor | U2 | cursor/unlink=x; F |
| U2 / left=None | left=x.prev | U3 | cursor/unlink=x,left=L 또는 None; F |
| U3 / right=None | right=x.next | U4 | cursor/unlink=x,left=L,right=R; F |
| U4 / L≠None | L.next=R | U5 | x는 cursor/unlink,L/R는 holders; F |
| U4 / L=None | arena.head=R | U5 | x는 cursor/unlink,right=R; F |
| U5 / R≠None | R.prev=L | U6 | 생존 chain 대칭 복구,x 옛 links만 잔존; F |
| U5 / R=None | arena.tail=L | U6 | tail/single anchor 복구,x는 cursor/unlink; F |
| U6 / x.prev=L | x.prev=None | U7 | x는 cursor/unlink,이웃은 left/right; F |
| U7 / x.next=R | x.next=None | U8 | x는 cursor/unlink,x 모든 ref=None; F |
| U8 / left=L 또는 None | left=None | U9 | x는 cursor/unlink,R은 right/chain; F |
| U9 / right=R 또는 None | right=None | U10 | x는 cursor/unlink,이웃은 chain; F |
| U10 / unlink=cursor=x | unlink=None | U11 | x는 cursor만,UNLINKING domain; F |
| U11 / cursor=x,x ref 모두 None | cursor=None | IDLE | x graph 도달0,live census 잔존이면 hidden alias; F |

U4(single)는 head=None/tail=x, U4(head)는 R.prev=x, U4(tail)는 L.next=None/tail=x,
U4(middle)는 L.next=R/R.prev=x만 일시 허용한다. U5가 각각 이를 복구한다.
이를 일반적인 “일시 불일치 허용” 조건으로 넓히지 않는다. 기존 chain의 나머지 slot은 불변이다.

**F: foreign fault.** private `_step(op)`이 한 행과 다음 phase를 수행한다. test wrapper는
해당 행 실행 **직전 또는 직후** 동일 예외 객체를 raise한다. tick은 Exception만 잡아 원 객체를
fault에, `DISPOSAL_FAULT`를 outcome에 저장한다. phase는 실패 순간의 전/후 값을 그대로 두고
모든 anchor/holder/cell slot을 보존한다. 이후 submit/tick은 mutation0으로 거부한다.
A0 전 fault는 constructor0/new=None, A0 후는 new=n이다. A0 내부 gap 관측은 주장하지 않는다.
U11 후 graph가 비었어도 fault는 정상 cleanup 인증이 아니다. args/context/cause/traceback/frame은
FAULT_RETAINED domain이며 정상 terminal을 기대하지 않는다. str/repr/frame clear/강제GC0.
fault 저장 실패·실제 OOM의 안전성/진전은 미증명이다.

## 4. 독립 actual-slot 관측과 terminal

observer wrapper는 `_new_cell` 원 constructor를 정확히 한 번 호출하고 반환 cell에 weakref와
monotonic generation을 붙인다. Census는 weakref/scalar만 저장, cell/frame/exception 보관0.
wrapper local은 IN_OBSERVER_FRAME이며 tick 반환 뒤 끝나야 한다. snapshot은 모든 census-live
cell과 shell/holder의 exact slot을 읽어 scalar generation graph만 반환한다. 역참조 local은
snapshot 반환까지만 존재하고 그 뒤에 다음 tick을 부른다. forward/backward chain과 holder를
독립 재구성하여 count뿐 아니라 정확한 identity/slot을 비교한다. 직전/현재 snapshot만 보관한다.

각 action 종료는 IDLE·정상 chain·모든 holder None·EMPTY s/c None으로 대조한다.
모든 cell을 **명시 unlink**한 뒤 `CHAIN_EMPTY` 후보를 검사하며 full dispose 완료가 아니다.
driver/wrapper/snapshot frame 종료 뒤 별도 terminal_live가 weakref를 하나씩 읽어 generation
tuple만 반환한다. 정상 test는 shell/census만 보유하고 cell handle0이다. 순환 참조가 있으면
살아 있는 census를 거부하며 GC로 통과시키지 않는다. external hidden alias/exception/closure는
test holder가 probe 종료까지 보유한다. referent 부재는 native free의 증명이 아니다.

observer/census 실패는 `ObserverAbort(BaseException)`로 subject Exception catch를 통과하고
`INCONCLUSIVE_OBSERVER`로 중단한다. operation 보존, kernel fault/RED로 계산0.
constructor 우회 cell이 chain/holder에 보이면 census-missing을 검출한다. 관측 전 소멸/hidden
bypass만 있으면 S miss=`INCONCLUSIVE_COVERAGE`이며 전체 할당 포괄성을 주장하지 않는다.
bootstrap/builtin 임시 객체/constructor 부분 실패·반환 gap/actual deallocation은 A 미해결이다.

## 5. 작은 fixture·실제 반례

literal fixture: empty shell; append1/2/3; single/head/tail/middle unlink; 남은 cell의 명시 head
unlink로 CHAIN_EMPTY; 모든 A/U행 전/후 foreign fault. 매 경계 exact graph/alias/phase·constructor
도달/반환을 비교한다. 최대1000tick은 harness 중단 상한이며 초과=INCONCLUSIVE, 제품 cap이 아니다.

| actual mutant | 독립 판정 |
| --- | --- |
| owner_missing/wrong_operation/wrong_token | constructor 도달0,사전 거부·graph 불변 |
| allocate_and_link | A0 새 cell/new 외 head 또는 tail delta 검출 |
| skip_chain | 실제 chain membership/전이 누락 검출 |
| clear_new_before_cursor | A4/A5 exact 순서와 alias 위반 |
| unlink_head/unlink_tail/unlink_single/unlink_middle | 실제 anchor/이웃 대입 누락·오연결 검출 |
| clear_unlink_early/clear_cursor_early | U10/U11 전 holder 해제 검출 |
| hidden_leaf/exception_alias/closure_alias | external 실제 cell alias로 terminal live generation 잔존 |
| double_write_restore | snapshot 순변화로 구별 못 하는 한계 대조; source-write 감사에서 거부 |

실제 callable/slot/constructor/alias를 monkeypatch하며 bool/count만 바꾸지 않는다.
double_write_restore는 snapshot kill 수에 넣지 않는다. 실제 mutant site는 코드가 생긴 뒤
SHA·행에 결속한다. observer 오류/hidden bypass는 intended structural RED가 아니다.
정상 A0와 allocate_and_link 대조를 반드시 한 쌍으로 둔다.

## 6. profile·예산·남은 full B1 gate

small은 [짝 계획 §1](../plans/2026-09-27-decoder-b1-structural-probe.md)의 실제 guarded prefix만
사용한다. env-i/plugin3/cache off/공통 lock/wall cap/raw exit/첫 실패 중단·기존 conftest 유지.
source/native lane·OS controller·새 runner/install·외부 API/SSH/deploy/restart0.
승인된 stdout/stderr 각각2MiB standard runner는 **현재 미확보**다. OS controller8MiB와
node selector 제한·exec token 절단·tee는 대체 불가다. N4097은 계획 단계부터 **UNRUN**.
B1a에는 아직 없는 large node를 만들거나 skip/deselect로 넣지 않는다. 원 full 인수는 그대로 남는다.

full B1은 CONT 생성/push·container→CONT 인계·end child→parent alias·ENTRY/ITEM 최초 게시·
lookup·duplicate 게시·WORK seed/pop·retire·finish READY/KNOWN_ERROR·known_failure/dispose의
모든 중간 상태를 §3 형식으로 동결해야 한다. 아직 실행 가능한 완성 계약이 아니다.
선행 F1/F2 유지: semantic edge당 WORK≤1, WORK pop 추가 allocation0, 빈 WORK/CONT 직접 unlink,
활성 retire 배치≤1, 미교체 조상 old는 live, 전체 live 수/깊이 두 subtree 상한 주장0.
최초 key 순서·bool/int/float/-0.0/문자열·덮인/최종 nonfinite·partial failure·inner b 중 outer old a
생존 및 실제 full mutants는 후속 미완 인수다.

N4097은 array 항목 수다. ITEM/value만 약2N이며 매 tick 전수 snapshot은 적어도 O(N²) 작업이다.
두 snapshot 보관은 총 비용을 줄이지 않는다. 전수 oracle/원 N 유지, 표본화/완화0.
후속 반복 계획은 **dedicated large1 + UTC 전체1 + KST 전체1 = 3회**, 별도 focused large 반복0이다.
runner 확보·full 전이표 승인·dedicated 비용과 전체 cap900의 양립을 먼저 검토한다.
root의 최신18a2196 UTC641.35s/KST643.67s는 새 후보 보장이 아니며 산술 잔여258.65/256.33초가
모두 large 예산은 아니다. 여유·새 small 비용·전체 actual exit0이 모두 필요하다.
부족하면 UNRUN/INCONCLUSIVE, cap 확대·재시도·기존 두 성능 예외로 새 timeout 면제0.
256facts 또는5ms/raw50ms/restore1000ms/ticket1500ms와 cold/cleanup timer 불변.
B1a/full S로 A·parser/encoding/scalar 비용·consumer·cold SQL/owner·full L3를 인증하지 않는다.
