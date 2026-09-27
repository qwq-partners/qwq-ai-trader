# Cold source·factory 선행 검증 소유권 설계

2026-09-27 KST. **PLAN_REVIEW_PENDING_ONLY**. 코드·시제품·시험 실행0이며 독립
Astra/xhigh 설계·계획 검토 전이다. 기준은 `59fa111c6ff56f09a7a69c91a6c9819d7ca02468`.
요청 모델 Astra/high, 실제 모델/effective effort metadata 미노출로 미검증이다.

## 1. 목적과 진행 권한

목적은 cold `_open` 검증 graph와 factory 선행 `load()` graph의 **첫 할당 전 책임자**를
결정하는 것이다. 기존 warm source 증명·consumer 목록을 재작성하거나 새 API를 구현하지 않는다.
사용자의 당일18KST까지 범위 내 순차 자율 진행 지시에 따라 원 L3 계획의 여러 선행 gate 중
**순수 설계 준비만 먼저 선택**했다. routine human 확인 대기는 대체하지만 증명이 통과했다는
가정, 제품 구현 허가, native 실행·격리·운영 권한의 확대는 없다.

P3는 실제 OS 종료·회수 증거 도구 개발 단계이고 P4는 순수 manifest 대조+빈 실제 registry
계획 한정이다. 이 문서는 둘의 완료를 검증하지 않는다. 둘이 완료되어도
`native_qualified=false`, `source_execution_permitted=false`, qualified runtime0,
source108 call-phase0은 그대로다. 과거 local139를 현재 native 자격으로 쓰지 않는다.

사용자가 개발 위험으로 수용한 관측은 frozen controlled5k **13.023871ms**와 별도
controlled100k **33.173999749ms(약33.174ms)** 두 개뿐이다. 원시 실패/100k enclosing
exit124/6UNRUN을 보존한다. 같은 두 이력으로 설계를 다시 막지 않되 timeout·수명·정합성·
새 실패를 면제하지 않는다. 이 문서는 성능 재실행을 지시하지 않는다.

## 2. 근거와 원 계획 처분

| 현행 경계(기준 SHA의 행) | 실제 공백 |
| --- | --- |
| `src/execution/safety/store.py:89–143` | `_connection` 게시 전에 connection/cursor/TEXT, `json.loads`와 `encode_state` 검증 graph/문자열을 생성한다. 오류는 원 예외 cause를 보유할 수 있다. |
| 같은 파일 `:145–179`, `:63–80` | load가 다시 decode한다. receipts는 `_open` 이후 BEGIN에서 같은 snapshot이지만 cold 검증은 그 앞이다. Future drain은 실제 자원 정리 증거가 아니다. |
| `src/execution/safety/factory.py:353–403` | preflight state와 `saved` Portfolio를 보유한 채 별도 runtime.restore가 두 번째 source를 읽는다. |
| `src/execution/safety/application.py:221–222`, `:372–398` | state getter/publish의 deepcopy와 policy receipt 대조가 별도 graph/consumer다. |
| `src/execution/safety/runtime.py:183–191`, `:514–557` | day/fence 복원 및 Portfolio identity를 보존하는 게시가 source 획득 이후다. |
| `src/execution/safety/recovery_projection.py:438–462`, `:824–876`, `:1184–1188`, `:1203–1207`, `:1261–1271`, `:1297–1310` | exact tuple/순서/중복/invalid 의미, marshal detach, tuple 생성, 예외/finally 동기 close를 wrapper로 해결할 수 없다. |

[원 N5 Task6](../plans/2026-09-24-recovery-projection-index.md)의 `store.py 무변경`과
`load_with_policy_receipts → freeze/build` 전제는 allocation-time ownership보다 먼저 큰 graph를
만든다. **그 전제를 아래 restore 전용 managed 진입 설계로 대체하자는 제안**이다.
기존 계획이나 코드는 이번에 고치지 않는다. 향후 제품 변경 allowlist와 인터페이스는 decoder/
consumer 계획에서 다시 검토한다. 기존 `_open/_load/commit`, 공개 dict·예외·동기 close 의미를
소급 변경하지 않고 현재 legacy 경로를 관리형 보증으로 보고하지 않는다.

## 3. 선택과 비용

| 선택 | 판단/비용 |
| --- | --- |
| restore 전용 operation이 cold acquisition부터 preflight/restore 인계까지 소유 | **권고.** ticket/barrier 아래 사전 등록하고 한 snapshot의 source capability를 재사용한다. decoder·validator·실제 consumer 소유권 증명이 필요해 당장 제품 구현할 수 없다. |
| legacy `_open/_load` 전체를 managed로 교체 | commit/lookup/close·오류·공개 dict까지 바뀌므로 범위가 크다. 여기서 채택하지 않는다. |
| 기존 load를 await wrapper로 감싸거나 반환 후 arena 등록 | 이미 생성/소멸한 검증 graph와 preflight 사본을 놓친다. 미인계 Future/traceback도 남아 기각한다. |

권고의 cold connection은 **동일한 기존 SQLite executor에서 operation 전용으로 생성하고
그 worker에서 실제로 닫는다**. `_connection`에 장기 저장하거나 정상 pin으로 남기지 않는다.
기존 warm connection을 빌리거나 닫는 확장도 이번에 하지 않는다. 새 worker/process 해제 이관0.
대가는 cold restore마다 connect·validation·close가 필요하고 기존 연결 cache를 재사용하지 못한다는
점이다. 이 비용은 timer 안에 둔다. 이후 commit의 legacy cold-open까지 해결했다는 주장은 금지하며,
전체 writer 활성화 전에 별도 인수가 필요하다. 이미 열린 store를 이 cold 전용 계약으로 인증하지 않는다.

## 4. 책임과 사건 순서

여기서 operation/source capability는 역할 이름이며 public 클래스·함수 시그니처 제안이 아니다.

1. 기존 factory의 비저장소 인자/바인딩/면제 별칭 검사와 파일 존재 요구를 먼저 유지한다.
   그 뒤 RESTORE ticket → mutation/admission barrier → publication/fault epoch 무효화 순으로
   소유한다. lock 순서는 N5의 producer → quote → owner 그대로이며 역순 획득0이다.
2. owner의 active 슬롯에 operation, exact token, source/decoder/preflight 자원 영역,
   private cleanup certificate, 단일 driver 감독 책임을 **첫 connect/SQL/JSON 할당 전** 등록한다.
   등록 실패면 제출0. 작은 shell 생성 실패도 SQL 제출0이다. 제출과 future 귀속 사이 await0;
   submit/driver 생성 실패는 같은 슬롯에서 처리한다. caller task의 finally만을 소유자로 삼지 않는다.
3. connection 생성 전 native-resource 자리와 실행 권한을 worker 영역에 등록한다. connect 내부의
   부분 할당도 그 operation의 native 증명 도메인이다. 반환 후 connection을 등록하는 것만으로
   C 내부 실패 수명을 증명했다고 하지 않는다. SQL worker는 owner RAM을 받지 않는다.
4. 아래 검증 순서로 checkpoint와 receipts를 한 read snapshot에서 취득한다. raw TEXT·row·receipt
   page와 SQL transaction은 operation의 자원이다. worker와 loop가 같은 영역을 동시에 소비/정리하지
   않는다. 단계별 worker terminal 인계 뒤 loop가 decode를 진행하고 다음 SQL 단계는 같은 executor에
   제출한다. transaction을 열어 둔 중간 await에도 다른 store 작업은 exclusive source barrier로 차단한다.
   시작 권한은 owner incarnation/publication/fault epoch 세 축이며 SQL revision은 아직 미결속이다.
   첫 checkpoint SELECT의 durable revision을 **한 번만** source에 bind한다. 새 owner의 version0에서
   durable version7을 복구하는 정상 경우를 stale로 거부하지 않는다. 이후 exact source token은
   `(시작 incarnation, publication epoch, fault epoch, bound durable revision)`이며 owner의 낡은
   RAM version을 새 SQL revision으로 덮어 맞추거나 두 번째 SELECT 값으로 재bind하지 않는다.
5. **검증용 별도 graph를 버리고 다시 decode하지 않는다.** 할당 시 소유하는 decoder가 동일 원문에서
   만드는 private graph가 cold JSON 검증과 factory preflight/restore의 공통 입력이다. 이 decoder는
   미구현이며 stdlib `json.loads` 뒤 등록으로 대체할 수 없다. canonical 검증 scratch/문자열도 처음부터
   operation 소유다. 지원 도메인 전체의 finite/type/JSON bytes 의미를 유지하는 별도 증명이 선행한다.
6. factory는 source graph의 읽기 capability만 빌린다. `saved` Portfolio, price-view 대사, live encoding,
   protection encoding 등 preflight 임시 graph도 **생성 전에** preflight 영역에 등록한다. preflight는
   checkpoint read/decode를 다시 요청할 수 없고 검사 결과는 같은 operation/token/snapshot에 결속한다.
7. preflight를 통과한 정확한 capability를 owner 복원에 await 없는 일회 인계로 넘긴다. destination
   owner 책임이 먼저 준비되고 capability·scratch 완료·cancel/token을 함께 검사한 뒤 소유권을 이동한다.
   이후 runtime.restore가 별도 load를 호출하는 경로는 관리형 경로에서 금지한다. stale/cancel이면
   새 snapshot으로 재시도하지 않고 같은 operation을 정리한다.
8. source 인계와 producer-current는 별개다. owner publish·runtime day/fence/admission/binding 복원,
   실제 consumer 반납 계약이 끝나야 N5 current가 된다. 기존 deepcopy/DTO/tuple consumers를 그대로
   연결하면서 안전한 인계가 됐다고 표시하지 않는다. 현재 제품 구현 gate가 여기서 계속 닫혀 있다.

## 5. 검증 순서와 DB mutation

factory의 absent checkpoint는 생성 없이 거부한다. 존재하지만 빈 파일은 기존파일로 취급하여
schema0에서 거부하며 새 schema/빈 계좌로 재생성하지 않는다. store의 일반 새 파일 생성/초기화는
legacy에 남겨 두고 이 **existing-checkpoint restore 전용** 경로의 지원 범위로 승격하지 않는다.

| 단계 | 보존/명시적으로 제안한 차이와 mutation |
| --- | --- |
| 경로·permission | parent/path symlink 거부 → 기존 parent0700/DB·기존 sidecar0600 확인·적용. chmod 실패는 거부한다. 읽기 복구도 이 metadata mutation이 있다는 사실을 숨기지 않는다. |
| connect | 기존 파일 전용 open(mode=rw 의미); existence check 이후 파일 소멸 시 새 DB를 만들지 않고 거부하는 명시 강화 제안. SQLite timeout5, autocommit connection 의미는 보존한다. path identity/exclusive-writer 전제의 실제 인수는 필수다. |
| snapshot·schema | journal_mode 변경 전 read transaction을 시작하고 user_version=1, checkpoint/commits 필수 테이블, quick_check 결과 ok 순서로 검증한다. 현행 autocommit 검사들을 하나의 read snapshot 안으로 옮기는 제안이며 SQL 순서/lock 비용은 새 인수 대상이다. |
| checkpoint·JSON | checkpoint id1·version exact int≥0·존재 검사 → 동일 row 원문의 JSON/domain/canonical 검증. invalid/nonfinite/비dict/손상은 WAL 변경 전에 거부한다. TEXT/native cell lifetime과 parser partial graph는 구분한다. |
| receipts | 위 검증을 통과한 뒤 **동일 read transaction**에서 policy-registration prefix 전 행을 bounded page≤64로 읽는다. `0 < receipt_version <= checkpoint_version`, 등록 ID 양방향 일치는 보존한다. 잘라내기·fetchall facade 금지. |
| read transaction 종료 | 동일 source에 필요한 원문/graph/receipts를 operation이 소유한 뒤 종료한다. 실패 시 rollback하며 rollback 실패는 fault다. snapshot은 실제 읽은 immutable contents+revision+connection nonce+token으로 봉인되며 SQL 종료가 두 번째 load 권한을 만들지 않는다. |
| WAL·durability | transaction 밖에서 WAL 결과 wal 및 synchronous=FULL, 마지막 private-files 검사를 유지한다. WAL/sidecar 생성·permission 변경은 현행처럼 실패/거부 뒤에도 남을 수 있다. 새 경제 commit/checkpoint/receipt 쓰기0. |
| factory 검증 | version>0/required roots → scope → day → regime baseline/generations → live 대사 → non-submit prepared 거부 순서 유지. live publication0인 채 위 단계들이 끝나야 owner 인계 가능하다. |
| stale 관측·native 종료 | WAL 설정 뒤 preflight 전, 그리고 preflight 완료 뒤 인계 직전에 같은 connection의 새 짧은 read로 checkpoint version scalar만 관측한다. bound revision과 다르면 거부한다. 이후 cursor/metadata alias/statement·전용 connection을 같은 W에서 실제 종료한다. close/worker done은 사망 증거가 아니며 rollback/close 실패는 fault다. READY는 이 뒤에만 가능하다. |

JSON/receipts를 WAL보다 앞에서 검사하는 것은 기존 `_open`의 JSON 거부를 보존하면서 receipt 오류도
WAL 변경 전에 발견하는 **명시적 순서 차이**다. 현행 load에서는 receipt 오류가 WAL 후에 드러난다.
거부를 삭제하지 않고 이 차이의 오류 우선순위/sidecar effect를 별도 parity 인수로 고정한다.
다중 결함에서는 schema→table→integrity→checkpoint→JSON→receipt의 최초 오류를 유지한다.

동일 read snapshot에는 checkpoint·receipts와 preflight/restore의 입력이 모두 연결된다.
다른 WAL connection이 중간 commit한 반례에서도 V1 checkpoint+V1 receipts만 소비하고 V2를 섞지
않는다. transaction 종료→WAL 뒤 또는 preflight 도중 V2가 생기면 위 scalar 관측은
`observed_revision != bound_revision`으로 같은 operation을 stale 처리한다. 새 read는 오직
staleness 관측이며 state/receipt를 읽거나 old capability에 섞지 않는다. 실패/누락도 거부한다.
이 추가 관측과 native 종료 비용은 timer에 포함한다.

최종 관측 뒤 publish 사이에도 비참여 writer가 쓰면 검사만으로 그 창을 원자적으로 닫을 수 없다.
따라서 publication currentness는 마지막 L의 await 없는 incarnation/publication/fault 비교와
**실제 전체 writer의 동일 barrier/exclusive-writer 참여**가 함께 필요하다. 시작 owner version0과
bound durable version7의 차이는 이 비교의 오류가 아니다. 단일 writer enforcement·경로 교체 방지의
실제 인수가 없으면 활성화 불가이며 이번 설계가 이를 구현/입증하지 않는다. 마지막 token 비교는
mixed snapshot을 합법화하지 않으며 scalar 확인 성공을 hostile writer에 대한 보장으로 쓰지 않는다.

## 6. 사건별 before → after와 독립 oracle

L=event loop, W=기존 SQLite worker. 아래 after는 요구 계약이지 구현 결과가 아니다.

| 사건 | before → after: 할당/마지막 해제 스레드·선등록 소유자 | 취소·실패 보유 | 인계 권한 | 별도 oracle |
| --- | --- | --- | --- | --- |
| cold connect/검증 | W `_open` local conn/임시 JSON → W native slot 선등록, L decoder 영역 선등록; Python graph는 협력 정리, native는 W 실제 종료 | connect/validate 오류도 동일 operation; foreign 예외는 fault 보유 | raw SOURCE→등록 decoder만; legacy connection 공개0 | connect partial-failure/native cursor·metadata 수명+JSON 최초 할당 trace |
| schema/quick_check/TEXT | W 임시 cursor/row/집합 → W native slot·bounded row 영역; table metadata도 무제한 집합을 scratch로 숨기지 않음 | SQL 대기 취소는 latch; SQL/transaction terminal까지 동일 슬롯 | snapshot nonce와 version 결속 raw 권한 | 모든 SQL cursor actual-death, TEXT 마지막 ref; 단일 큰 scalar≠상수 wall time |
| preflight | L factory `state`/`saved`와 encode 임시값 → L source 읽기 lease·preflight 영역 선등록 | 대사 거부/반복 취소에서도 active operation이 보유, public live 변경0 | 같은 입력의 검사 verdict만, 새 load/임의 dict 소비권 없음 | factory 프레임 local·DTO·encoding alias/마지막 참조 관측 |
| checkpoint/receipts 경합 | preflight load V1, restore load V2 가능 → 한 V1 snapshot capability를 둘이 소비 | stale면 publish0·동일 정리, 새 builder0 | exact store/connection lifetime/token4축/snapshot provenance만 | 독립 WAL writer commit between reads; V1/V2 payload 값 직접 대조 |
| worker done·미인계 반복 취소 | Future result의 state가 미회수될 수 있음 → Future는 scalar completion만, operation이 결과 보유 | 두 번 이상 취소도 driver/cleanup certificate cancel0; gate/follower 유지 | cancel latch 뒤 take0; 성공 take는 일회 await0 | 실제 gate drain/follower queue·future result/exception/closure strong-ref 경로 |
| JSON/preflight error unwind | locals/traceback/cause가 graph 보유 → known bounded error envelope, partial graph는 선등록 영역에 잔류 | foreign graph-bearing 예외는 같은 private fault slot, certificate pending, writer0 | 원 예외/args/traceback 외부 escape0; 임의 repr/str/frame clear0 | malformed partial JSON+대사 예외+foreign cause 참조; false-cleanup 변이 |
| owner·runtime 인계 | owner deepcopy/publisher/DTO가 별도 사본 → 다음 consumer 설계가 각 할당·반납 책임 인수 | 인계 전 실패는 operation, 인계 뒤 실패는 destination owner unavailable | source ownership≠current/거래 허가 | actual consumer parity/identity/반납 oracle: **이번 미해결** |

connection/cursor의 close, cursor metadata의 최종 참조, TEXT scalar, decoder graph/scratch,
factory DTO/encode 결과, owner/runtime 소비자, exception chain, worker Future/closure는 서로 다른
수명이다. 한 weakref/counter0/Task.done()으로 이 도메인들을 합쳐 PASS하지 않는다.
관측기가 객체를 pin해서 마지막 해제를 가리면 lifetime/latency 증거로 쓰지 않는다.

## 7. 실패·정리와 금지할 우회

취소는 결과 사용을 먼저 막는 멱등 latch다. caller는 shield로 관측할 뿐 driver/SQL/private
certificate를 취소하지 않는다. 완성됐지만 미인계한 결과도 operation 소유다. known 오류 정리 후
최초 오류와 취소를 구분해 전달하며, foreign graph-bearing 오류/rollback·native close 미확인은
같은 operation의 DISPOSAL_FAULT·writer0·unavailable·certificate pending이다. 새 cleanup worker,
새 operation/backlog/무한 재시도 또는 fault를 정상 permanent pin으로 표시하지 않는다.

certificate는 native closure와 모든 private scratch가 실제 정리되고 결과가 정리 또는 정확한
destination에 인계됐을 때만 성공한다. 일반 Future의 error/cancel/settled를 certificate로 등록하지
않는다. 결과 소비자 계약이 미완이면 certificate 성공도 미인증이다. shutdown/강제 종료를 정상
cleanup으로 표시하지 않는다. 원 N5의 256facts 또는5ms, raw50ms, 성공 restore1000ms/
ticket1500ms와 timer 포함 경계는 유지하며 cold 파일/SQL/decode/preflight/cleanup도 timer 안이다.
edge≤256 구조 인수는 wall-time·allocator·GC 선점 보증과 다르다.

후등록 arena, await wrapper, 큰 tuple/list facade, 정상 영구 pin, 추가 thread/process 해제 이관,
GC 정책 변경, 이력 pruning, schema/JSON/permission/WAL/빈파일 검증 삭제로 통과하지 않는다.

## 8. 검토 인수 질문과 남은 결정

독립 Astra/xhigh는 아래를 판정하고 저자는 자기 critical 변경을 승인하지 않는다.

1. operation 전용 cold connection의 실제 W 종료 선택이 캐시 재사용 손실을 감수할 만큼 범위가
   명확한가? connect 실패의 native 부분 할당, cursor metadata, store executor queue closure를 빠뜨리지 않았는가?
2. read transaction 안으로 schema/JSON 검증을 옮기고 receipts를 WAL 앞으로 당기는 순서의 lock/
   오류 우선순위/DB side effect가 명확한가? existing-only 강화가 legacy 생성 의미와 분리되는가?
3. 등록 없이 만들어지는 decoder/parser/validator/preflight graph를 막을 수 있는 allocation-time
   구현 증명이 아직 없다는 사실과 그 후속 gate가 명확한가? public dict consumers로의 임시 우회가 없는가?
4. 같은 capability가 preflight→restore를 연결하며 다른 snapshot의 revision/receipt 조합을 거부하는가?
   source 일관성과 publication currentness를 별개로 인수하는가?
5. 취소·error unwind·미인계 Future 마지막 참조에서 소유자가 하나로 유지되고 false cleanup이 막히는가?
6. 첫 RED가 실제 현행 경계의 소유권 단언 실패이며 import 부재나 runtime 차단을 RED로 오인하지 않는가?

선택한 책임/순서는 위와 같다. 미결정은 **구현 승인에 필요한** exact native/capsule·safe harness,
allocation-time decoder 표현/오류 도메인, 실제 consumer 반납·serialization/identity 호환,
all-writer barrier 배선과 현재성 증명, cold 성능 인수다. 이들을 임의 prototype API로 채우지 않는다.
짝 [첫 RED 계획](../plans/2026-09-27-recovery-cold-source-ownership.md)은 실행 전 gate와 한 번의
반례 실행까지만 고정한다. PLAN_REVIEW_PENDING_ONLY는 문서 작성 종료 상태이며 L3/source/제품 완료가 아니다.

## 근거 문서

[L3 설계](2026-09-27-recovery-build-lifecycle-design.md),
[L3-P0 계획](../plans/2026-09-27-recovery-lifecycle-proof.md),
[결과](../../reviews/l3-proof-results-2026-09-27.md),
[N5 원장](../../reviews/recovery-projection-progress-2026-09-27.md),
[필수 source/성능 예외 계약](2026-09-27-required-source-proof-design.md),
[N5 §7.2/7.3/11.2](2026-09-24-recovery-projection-index-design.md).
coordinator의 `.superpowers/sdd/2026-09-27-runtime-admission-contract/engine-next-slice-analysis.md`
전체와 같은 작업트리의 P4 spec/plan도 대조했다. 그 분석·계획을 새 native 증거로 사용하지 않았다.
