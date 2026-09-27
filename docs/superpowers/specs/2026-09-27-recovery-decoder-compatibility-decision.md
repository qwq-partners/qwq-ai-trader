# Recovery decoder 표현·호환성 결정

2026-09-27 KST. **독립 검토 `APPROVE_DESIGN_DIRECTION_ONLY` — 방향 한정 승인.**
검토 후보는 `f7b8c0e383c239fa5d80f535b619d32145db4349`다. 기준 source는
`59fa111c6ff56f09a7a69c91a6c9819d7ca02468`다. 요청 모델/effort는
gpt-6-astra/high, actual model/effective effort metadata는 미노출로 미검증이다.
이 문서는 정적 비교와 다음 설계 범위만 정한다. API·자료구조·관측기 구현0, 시험·SQL·native·
제품 import 실행0이며 allocation-time ownership 또는 full L3를 증명하지 않는다.

## 1. 결정과 적용 범위

**다음 설계 후보로 private bounded owned representation을 선택한다.** 선택 범위는
parser 내부의 첫 노드 생성, 중복키 교체, 부분 실패, 일회 인계 전 정리까지다. 현재 legacy
반환 `dict/list/scalar`를 대체하거나 factory/runtime 소비자를 전환하는 결정이 아니다.
이 표현으로 전체 cold restore를 구현할 수 있다는 판단도 아직 **hypothesis / blocked**다.

이유는 built-in dict/list의 값 호환성은 좋지만, 큰 컨테이너의 내부 재할당·마지막 참조 해제와
중복키의 큰 이전 값 교체를 작은 소유 작업으로 나눌 실제 경계가 없기 때문이다. private 표현은
그 경계를 설계할 여지가 있는 대신 exact type 소비자에 직접 넘길 수 없다. 어느 안도 현재
큰 scalar·canonical encoding·실제 consumer·native 정리 문제를 닫지 못한다.

사용자의 당일18KST까지 범위 내 자율 진행은 routine human 확인 대기만 대체한다. 기존 P5
`APPROVE_PREPARATORY_DESIGN_ONLY / COLD_SOURCE_OWNERSHIP_DESIGN_REVIEWED_ONLY`와
`TEST_ORACLE_DESIGN_UNRESOLVED / RED_DEFERRED`는 그대로다. `native_qualified=false`,
`source_execution_permitted=false`, qualified runtime0/source108 call-phase0이다.
P3 도구 성공이나 P4 registry 대조는 이 decoder의 실제 할당·정리 증거가 아니다.

## 2. 비교 대상과 현행 근거

**A — built-in 유지:** 별도 incremental parser가 처음부터 exact dict/list/scalar를 구성하고
소유자가 생성·임시 참조·교체·해제를 책임지는 안이다. 기존 `json.loads`/hook 결과를 반환 후
등록하는 안은 A에 포함하지 않는다. A도 그런 parser와 관측 seam은 현재 없다.

A에서도 각 child를 먼저 작은 소유 ledger에 잡고 dict/list의 edge를 단계적으로 끊으면 일부
cascade를 피하는 설계를 검토할 수 있다. 그러나 ledger 자체의 생성·정리와 built-in container의
성장/내부 저장공간 해제, 큰 scalar 비용까지 닫히지는 않는다. A가 원리적으로 불가능하다는
결론은 아니며, 현재 증거로 전체 bounded lifetime을 선택할 수 없다는 판단이다.

**B — private 표현:** 고정된 최대 outgoing edge 수를 가진 작은 node/page와 분할된 순서·map
자료를 operation 자원 영역이 소유하는 안이다. 원문 span만 들고 소비 시 `loads`하는 지연 포장,
큰 tuple/list 한 개를 숨긴 facade, `marshal` 왕복은 B가 아니다. bounded는 입력 총량 제한이나
wall-time 보증이 아니라 **개별 구조 작업의 후보 제약**이다. scalar·할당기·GC 비용은 별도다.

현행 [store](../../../src/execution/safety/store.py)의 `:120`은 `_open` 검증용
`encode_state(json.loads(row[1]))`, `:150–152`는 반환용 parse+encode다.
[factory](../../../src/execution/safety/factory.py) `:353`의 load 뒤 `:403`의 restore가
[application](../../../src/execution/safety/application.py) `:388`에서 또 load한다.
따라서 wrapper/후등록은 첫 partial graph뿐 아니라 검증 graph·canonical 문자열·preflight graph를 놓친다.
이 행들은 **현행 호출 위치**이며 미구현 replacement의 실제 allocation site가 아니다.

## 3. 실제 consumer 계약 대조

표의 보존은 향후 인수 요구이며 PASS 표시가 아니다. `store.py` 등 축약은 모두
`src/execution/safety/` 기준이고 행은 위 base에서 직접 읽었다.

| 계약·실제 소비 지점 | A: built-in 유지 | B: private 표현과 필요한 경계 |
| --- | --- | --- |
| C1 root/nested exact dict, key exact str — `store.py:22–39` | 직접 호환 가능하지만 별도 parser parity 필요 | private node는 현재 검사에서 거부된다. Mapping facade도 불가. legacy 경계에는 exact dict가 필요 |
| C2 nested exact list, tuple 거부 — `store.py:27–37` | list 순서·중복 원소를 그대로 유지 | private sequence를 list로 간주 금지. legacy 인계 시 exact list를 만드는 별도 생성/정리 계약 필요 |
| C3 str/int/float/bool/None 구분 — `store.py:25–26`, `application.py:249–252` | bool/int Python equality만으로 parity 판정 금지 | node tag가 type 의미를 보존해야 하며 span/Decimal/int 대체값을 기존 소비자에 전달할 수 없음 |
| C4 duplicate key last-wins, 첫 삽입 위치 — `store.py:120,150`의 hook 없는 loads | 이전 key 자리 유지와 이전 value의 실제 retire 책임 필요 | 첫 key 순서를 유지하는 map entry와 교체 가능한 value 참조를 별개로 설계. 중복 거부·key 재삽입으로 순서 변경 금지 |
| C5 앞 NaN/Infinity/큰 값이 뒤 유효값에 덮임 — `store.py:41–42,120,150–151` | 최종 tree canonical 검사보다 앞 nonfinite 즉시 거부 금지 | syntax/숫자 변환은 원 위치에서 처리하되 finite/domain 검사는 최종 살아 있는 tree에 적용. 덮인 큰 값도 구문 검증·retire 책임 존재 |
| C6 noncanonical raw/완전 문서 — `store.py:120,150–151` | whitespace/key order/escape 변형 수용, trailing junk 거부 | tokenizer의 수용 집합·Unicode/escape·숫자 의미 parity 필요. canonical 원문만 허용하거나 unknown span syntax skip 금지 |
| C7 unknown root 보존/strict DTO unknown 거부 — `factory.py:354`, `economics.py:36–38`, `protection.py:108–110` | 전체 graph를 그대로 유지할 때 현 validator 적용 가능 | 알 수 없는 root도 저장·digest에 포함. DTO schema 외 필드를 삭제하여 validator를 통과시키지 않음 |
| C8 돈/가격은 exact Decimal 문자열 — `economics.py:41–55,148–164`, `protection.py:113–141` | 문자열 spelling 유지; JSON 숫자를 Decimal로 변환 금지 | 내부 분할 문자열에서 exact str 인계 비용은 미해결. canonical 숫자·문자열 type과 `1.00` 같은 spelling 보존 |
| C9 canonical UTF-8 bytes/digests — `store.py:41–42,184–201`, `protection_recovery.py:22–23` | 기존 dumps 결과 재사용 시 값 호환에 유리하나 full string/bytes·정렬 scratch 미소유 | 별도 traversal/정렬/escape/숫자 출력이 동일 bytes를 내야 함. iterencode 호출 이름만으로 bounded 판정 불가 |
| C10 factory preflight와 restore의 동일 source — `factory.py:353–403`, `application.py:388–397` | dict type 호환만으로 재load 제거·같은 snapshot 인계가 되지 않음 | read capability를 쓰는 새 managed 소비 계약이 필요. 이는 명시적 설계 선택이며 현재 API와 동등하다고 하지 않음 |
| C11 owner/publisher/state getter 복사 — `application.py:220–223,372–398` | deepcopy와 publisher 복사를 각각 선소유해야 함. source owner 하나로 포괄 불가 | private take 이후도 기존 deepcopy로 자동 연결 불가. 새 destination 책임과 모든 escape/copy 반납 지점이 선행해야 함 |
| C12 live Portfolio/면제 set identity — `runtime.py:534–569`, `protection.py:284–294` | top-level Portfolio와 `_exit_exempt` set 유지, positions 교체 의미 유지 | private source identity와 live identity는 별개. managed consumer 전환 시에도 기존 별칭은 보존 |
| C13 receipt 양방향/version 일치 — `store.py:163–174`, `application.py:391–397` | 동일 dict 값 외에 checkpoint/receipt snapshot 연결 필요 | source token·snapshot provenance를 함께 인계; receipt/graph 재bind 또는 새 snapshot 혼합 금지 |
| C14 known error와 다중 결함 우선순위 — `store.py:138–179`, `factory.py:357–403` | parser 예외 class/cause가 바뀌면 자동 parity 아님 | bounded 오류 결과는 명시적 managed 계약. P5의 receipt 우선 순서 차이만 명시하며 기타 오류 우선순위를 임의 변경하지 않음 |
| C15 projection tuple/invalid/immutable 의미 — `recovery_projection.py:438–462,1177–1244` | source list와 projection tuple은 다른 단계. marshal copy는 소유권 해법 아님 | 별도 projection consumer 인수 필요. malformed row/중복 원소 보존, sibling immutable 의미 유지. 현재 runtime 배선0이라 cold 증거로 사용 불가 |
| C16 큰 string/int/map/list/depth — `store.py:22–42,117–120,145–152` | 제품 명시 입력 크기 cap 없음. 내부 resize·변환·최종 free 비용 미해결 | segment/node만 bounded여도 exact scalar materialization·키 비교·UTF-8·최종 raw TEXT 해제 비용 미해결. 새 cap이나 budget 완화로 해결 금지 |

C5는 최종값에 관한 조건이다. 앞 값이 구문 오류거나 현 parser의 숫자 변환 자체가 실패하는 경우까지
뒤 duplicate가 구제한다는 뜻이 아니다. 비유한 값이 최종 tree에 남으면 종전처럼 거부해야 한다.
C9에는 int/float 구분, 음의 zero·Unicode/escape 및 허용되지 않는 encoding의 최초 오류도 포함한다.

기존 assertion은 `tests/test_execution_state_store.py:96–106,123–134`(사본 분리·nonfinite·10,000자),
`tests/test_execution_policy_registration_boundaries.py:108–154`(한 receipt snapshot·취소),
`tests/test_execution_protection_checkpoint.py:340–350`(set alias),
`tests/test_recovery_projection.py:95–118,181–189,786–812`(invalid·immutable·token·DTO 거부)에서
읽었다. 이번에 실행하지 않았으며 이 assertion들은 decoder allocation-time 증명이 아니다.

## 4. 가장 작은 다음 설계 범위

다음 산출물은 **B의 construction/duplicate-retire/error 경계와 독립 관측의 실현 가능성 설계** 하나다.
factory/runtime 전환이나 구현 계획을 함께 넣지 않는다. 기존 legacy API와 자료형은 유지하며,
private 표현을 legacy 소비자에 전달하는 길은 아직 닫혀 있다. 단일 전체 materialization을 넣어
호환을 맞추면 C1–C3은 만족할 수 있어도 큰 graph 생성/해제 문제를 그대로 되살린다.

검토할 구조 가설은 다음과 같다. 명명은 역할 설명이며 새 클래스·함수 시그니처가 아니다.

1. active operation 아래 parser resource 영역과 작은 construction holder가 먼저 존재한다.
   첫 holder는 고정 크기의 감독 shell 안에 두고, shell 생성 실패는 SQL/parse 제출0이다.
   node/page와 소유권 bookkeeping은 모두 유한 fanout의 작은 단위로 만든다. 등록부 자체를
   무제한 list/dict/tuple 한 개로 만들거나 깊이를 Python 재귀 프레임에 숨기지 않는다.
2. 새 단위의 destination slot을 먼저 준비한 뒤 생성한다. allocator 반환부터 slot 연결까지의
   일시 참조는 owner가 감독하는 construction frame의 책임이다. 이 창을 zero라고 가정하지 않는다.
   프레임의 최대 live 참조 수·실패 unwind·실제 slot 연결을 증명하지 못하면 선등록 요건 미충족이다.
3. object map은 최초 key 순서와 마지막 value를 분리하고, array는 원 순서/중복을 유지한다.
   새 duplicate value를 끝까지 parse한 후 이전 subtree를 operation의 retire holder로 옮긴다.
   새 값 게시와 이전 값 보유가 await 없는 전이로 결속되어야 한다. root 대입 하나로 이전 graph가
   cascade 해제되는 경로는 허용하지 않는다. 다음 형제 value를 시작하기 전에 해당 retire 작업을
   협력적으로 끝내는 방향을 검토해, 덮인 값들의 미처리 backlog가 duplicate 수만큼 쌓이지 않게 한다.
   큰 이전 값의 retire가 오래 걸리는 것은 숨기지 않으며 기존 timer를 계속 소비한다.
4. map lookup/sort, parser stack, 중복키의 버려지는 key, 문자열 segment, canonical 출력·오류 위치
   자료도 각각 소유 domain이다. 크기·깊이가 입력에 비례할 수 있어도 각 변경/제거 단위의 참조 수를
   제한해야 한다. 현재 구체 layout·수치·scalar 알고리즘이 없으므로 이 가설은 구현-ready가 아니다.
5. syntax known error는 작은 코드·위치 결과로 private driver에 반환하는 방향을 권고한다.
   graph/raw string을 예외 args·message·cause·Future result에 넣지 않는다. 예상 밖 graph-bearing
   예외는 같은 private fault slot에 보존하고 DISPOSAL_FAULT·writer0·certificate pending을 유지한다.
   이를 정상 cleanup이나 영구 pin 성공으로 바꾸지 않는다.

이 선택은 parser 내부 구조 문제를 좁히는 데만 유효하다. exact str/int와 Decimal consumer의 변환,
큰 SQLite TEXT의 생성/해제, canonical output의 전체 bytes·정렬, legacy graph materialization에
대해 새 wall-time 상한을 만들지 않는다. **이 미해결 경계를 통과하는 제품 구현은 blocked**다.
향후 새 managed consumer API를 선택하면 C1–C16 각각의 기존 의미·명시 차이·반납 지점을 별도로
설계해야 하며, private representation이 그 작업을 자동 승인하지 않는다.

## 5. 실제 위치에서 출발하는 독립 oracle 요구

아래는 관측기 구현 지시나 실행 가능한 시험 명세가 아니다. 기존 실제 위치와 필요한 관계를
구분해 기록한다. `store.py:120/150`에서 wrapper로 호출 전 bool만 기록하는 것은 충분하지 않다.
**replacement의 실제 node allocator, 선행 holder, 독립 관측기가 모두 없으므로 첫 RED는 deferred**다.

| 경계와 현재 실제 위치 | 먼저 존재해야 하는 책임·실제 관측 관계 | 결속을 끊는 음성 대조와 합격 조건 | 현재 증거 공백 |
| --- | --- | --- | --- |
| 첫 parser 할당: `store.py:120,150`의 대체 대상 | owner active slot→정확한 operation/token→parser 영역→construction holder→새 node slot. allocator 직전 선행 관계와 반환 후 실제 참조 연결을 별도 관측 | active-slot link 생략, wrong operation/token, 생성 뒤 등록으로 이동, allocator 직후 연결 실패를 각각 주입. 선행 link 누락이면 node allocation 도달0; 연결 실패면 감독 holder가 책임을 유지하고 cleanup 인증0 | 현행은 loads 내부 할당. 새 allocation 함수/holder 실제 소스 위치 없음. 예시 사건을 실제 trace라고 기록할 수 없음 |
| duplicate 교체: 현 loads 호출과 최종 `store.py:151` | key entry의 old value, new construction value, retire holder의 실제 참조. old edge 제거 전 retire 책임이 존재 | retire link 삭제·큰 tuple에 old graph 숨김·다음 duplicate로 retire backlog 누적. 외부 관측에서 orphan/cascade 또는 미종결 domain을 검출해야 함 | private map/retire layout·실제 free seam 없음; 후행 counter로 대체 불가 |
| factory borrow: `factory.py:353,384–388,398–400` | 동일 source capability를 읽는 preflight lease와 별도 saved Portfolio/encode 영역의 생성 전 책임. lease는 복사본 책임을 대신하지 않음 | preflight 영역 link 누락, validation 후 재decode, V2 source 바꿔치기. 값과 capability provenance 양쪽 불일치를 검출 | 관리형 preflight 없음. 기존 DTO 생성은 unbounded이며 source 등록만 검사하면 거짓 통과 |
| 일회 take: `factory.py:403`→`runtime.py:185`→`application.py:388,397`의 대체 대상 | destination 책임을 먼저 준비; 같은 source/token/snapshot, borrow 종료·scratch 종결, cancel latch를 확인하고 await 없이 capability 소유를 이동 | 두 번째 take·cancel 뒤 take·destination 준비 전 source release·restore 재load. 두 번째는 새 graph/consumer 할당 전에 거부; 실패 전후 책임자는 source 또는 destination 정확히 하나 | 실제 managed handoff/API 없음. revision 정수 같음이나 payload equality만으로 take 증명 불가 |
| disposal: `store.py:138–154`, `application.py:372–380`, `runtime.py:535–557` | raw/partial node/retired subtree/preflight/canonical/owner/publisher/live 후보/예외/Future 각각의 terminal disposition. 인계된 결과는 destination 책임 증거와 결속 | 원문을 exception context/Future closure에 숨김, terminal event 누락, consumer alias 잔존, close만으로 성공 발행. 독립 actual-release 관측과 source 감사가 미회수 domain을 검출해야 함 | Python 실제 마지막 참조와 native actual-death 관측기 미자격; 완성 Future·등록 개수0·refcount만으로 판정 금지 |

관측 독립성은 제품이 만든 `owned=True`나 observer의 자체 counter를 다른 파일에서 읽는 것으로
성립하지 않는다. reviewer가 검토한 **실제 생성/연결/이동/해제 지점**과 별도의 expected lifecycle을
결속해야 한다. 작은 논리 allocation identity는 실제 지점에서의 관측을 연결하는 용도일 뿐 증거가
아니다. 재사용 가능한 `id`만 저장하거나 native free event 없는 refcount 감소를 종료로 판정하지 않는다.

관측기는 graph·frame·traceback을 strong reference로 로그/배열에 보관하지 않는다. 일시적인 실제
관계 관측이 수명을 바꾸면 그 구간은 latency/actual-death 증거에서 제외하고 별도 검증해야 한다.
registry 안에 보이는 노드만 검사해서는 registry 밖 orphan을 발견할 수 없으므로 실제 allocator의
독립 포괄성이 필수다. 완전한 포괄성을 제공할 seam이 없으면 판정은 INCONCLUSIVE이며 mock
속성/`getattr(..., False)`/등록 counter/완료 Future를 조합해서 PASS로 만들지 않는다.

## 6. 대표 사건별 요구와 아직 없는 증거

| 사건 | 요구되는 결과·책임·반례 관측 | 아직 없는 증거 |
| --- | --- | --- |
| malformed: 큰 unknown list 뒤 잘못된 token/trailing junk | partial 결과 공개0/live publication0. 첫 할당부터 partial graph·raw를 동일 operation이 보유. known 오류 위치 보존, 실제 정리 후에만 실패 결과 전달/슬롯 반환. 빠른 bool 실패는 수명 통과가 아님 | parser partial allocation·error unwind·actual disposal observer |
| duplicate replacement: 앞 NaN/Infinity/큰 subtree, 뒤 finite/small | 구문/변환에 성공한 앞 값은 덮여 최종 finite canonical을 방해하지 않음. 뒤 값은 그대로, 최초 key 순서 유지. old subtree와 duplicate key는 retire 후 실제 회수. surviving nonfinite는 거부 | 값/순서/bytes 차등 대조와 actual retire negative control. 정적 의미 분석만 존재 |
| cancel-before-take: worker 완료 후 미수령, 반복 취소 | take0; driver/SQL/private certificate cancel0. 같은 operation/ticket·barrier가 결과/정리를 끝까지 보유. follower writer는 실제 정리 전 진입0; fault이면 certificate pending·writer0 유지 | 실제 gate/follower 연결과 unclaimed Future/closure의 마지막 참조 관측 |
| consumer failure: preflight 거부 또는 take 뒤 publisher 실패 | take 전 live publication0/source 책임 유지. take 뒤 destination unavailable·legacy 복귀0; owner/publisher 사본과 live 후보 책임 유지. take 완료는 게시/복구 성공이 아님 | managed consumer 계약·DTO/copy allocation seam·인계 전후 오류 관측 |
| native partial failure: connect/cursor/rollback/close 실패 | 기존 단일 SQL worker에서 operation 전용 cold connection 처리. 부분 native 할당도 동일 책임. 실제 종료 미확인이면 DISPOSAL_FAULT·writer0·certificate pending; 새 worker/process 이관0 | exact runtime/capsule와 connect 내부·cursor metadata/statement·connection actual-death qualification |

오류 envelope는 작은 값을 전달하는 방향이지만 legacy exception parity를 자동 주장하지 않는다.
`raise ... from None`은 원 `__context__`/traceback의 참조 해제를 보장한 것으로 취급하지 않는다.
known error의 예외 처리 프레임이 끝나도 driver/Future/closure가 보유할 수 있으므로 실제 escape
경로를 검사해야 한다. 임의 외부 예외의 `str/repr`, traceback/frame 강제 clear, 강제 GC로
원문·graph를 지우는 방식을 쓰지 않는다. 외부 공개 오류가 private graph를 참조하지 않는다는
독립 증거가 없으면 known cleanup 완료도 인증하지 않는다.

## 7. 변하지 않는 cold-source/전체 gate

P5의 existing-checkpoint-only, schema/table/integrity/JSON/permission 검증, WAL/FULL 의미와
명시된 순서 차이를 유지한다. checkpoint와 receipts는 한 read snapshot; revision은 최초 SELECT에서
한 번만 bind한다. 시작 owner0/durable7을 stale로 오판하지 않고 token의 incarnation/publication/
fault와 bound revision을 구분한다. factory preflight와 restore는 같은 source를 사용해야 한다.
WAL 뒤/preflight 뒤 scalar staleness 관측은 graph 재load/rebind 권한이 아니다.

전용 cold connection은 기존 단일 SQL executor에서 실제 종료하고 정상 cache/pin으로 남기지 않는다.
P5대로 native/SQL은 W, parser/preflight는 L의 선등록 영역에서 처리하며 SQL worker에 owner RAM을
넘기지 않는다. worker terminal 인계 전 양쪽이 같은 자원을 동시에 소비/정리하지 않는다.
최종 관측→publish 창은 모든 writer의 동일 ticket/barrier 참여와 경로 교체 방어가 별도 필수다.
native 종료, private scratch 종결, 결과 disposal 또는 정확한 destination 인계 증거가 모두 있어야
cleanup certificate가 성공할 수 있다. consumer 계약 미완인 지금은 그 성공도 인증할 수 없다.

기존 256facts 또는5ms, raw50ms, 성공 restore1000ms/ticket1500ms와 cold/cleanup 포함 timer를 유지한다.
큰 scalar의 새 wall-time 보증·입력 cap·GC 조정·이력 pruning·정상 permanent pin·추가 thread/process
offload는 도입하지 않는다. 사용자가 수용한 13.023871ms/33.174ms 두 기존 관측은 원시 실패·exit124·
6UNRUN을 보존하며 새 실패·timeout·정합성·수명을 면제하지 않는다.

다음 설계에서 실제 표현과 observer 가능성을 독립 검토받더라도 별도 실행계획, native/capsule/
safe harness/정확한 cold subject qualification, 실제 consumer parity, all-writer enforcement,
cold latency·전체 회귀/full L3 인수는 각각 남는다. 첫 RED 파일 작성·실행은 현 gate 아래 금지다.
이번 문서로 legacy consumer 이행, projection 배선, 제품/CI/main/운영 전환을 시작하지 않는다.

## 8. 입력 문서와 근거 범위

- coordinator ROOT: `/home/ubuntu/projects/qwq-ai-trader/.claude/worktrees/owner-ticket-gate-20260926`.
  P5 `docs/superpowers/specs/2026-09-27-recovery-cold-source-ownership-design.md` 및
  `docs/superpowers/plans/2026-09-27-recovery-cold-source-ownership.md` 전체를 읽었다.
  이 두 문서는 본 작업의 base 이후 문서이므로 통합 시 coordinator의 해당 문맥이 필요하다.
- ROOT `.superpowers/sdd/2026-09-27-runtime-admission-contract/decoder-consumer-inventory.md` 전체:
  SHA-256 `82af53eb9dd3d970bcb903d57310bbed69e03b026a1e0222c9c054ec084dc031`.
- 같은 디렉터리 `decoder-design-grounding.md` 전체:
  SHA-256 `c379d652d3927c8c9b0f539075f8b0b23d7ae01f05e25b4fd48e17cdaff92d01`.
  공식 CPython decoder/hook 관찰은 그 문서에 수록된 정적 조사 범위로만 사용한다.
- 본문 인용 제품/기존 assertion은 source를 직접 읽었으며 실제 L1/L2 frozen worktree를 열지 않았다.
  source 행은 현재 코드 근거만 가리키고 미래 함수·관측기의 허구 행번호는 부여하지 않았다.

문서 자체 검토·diff/링크/비밀 패턴 확인은 작성자 보고서에 기록한다. critical 최종 승인과 통합은
coordinator가 배정한 독립 reviewer의 책임이며 작성자는 자신을 승인자로 기록하지 않는다.
