# KR writer 이행 — Task10 Plan–Do–See

기준: Task9 요청/자원 owner의 한정 리뷰 이후 진행한다. 사용자 후속 단계3의 세분화이며 전체 완료/운영 허가는 아니다. 상세 근거는 [후속 보고서](../../reviews/engine-execution-followup-2026-09-18.md), 상위 계약은 [실행 안전성 계획](2026-09-17-engine-execution-safety.md)이다.

## 불변 조건

KIS만 거래·계좌·잔고를 담당한다. Toss는 관측 전용 그대로다. 기존 전략·위험/청산 수치·면제·주문 유형을 변경하지 않는다. 실 API/자격/운영 접속 없이 실제 클래스와 외부 경계 fake로 검증한다. `trading_ready=False`, 정정 미지원, 최초 인계·취소 체인 미입증을 유지한다. 합성 startup 허가·취소 최종성 fixture는 공식 증거가 아니다. raw writer 거부만으로 정상 흐름의 이행을 완료했다고 부르지 않는다.

## 10A1 — 동일 host 계좌 독점과 명령 종료 drain

Plan: SQLite CAS와 계좌 프로세스 독점은 별개다. 먼저 로컬 lease와 같은 runtime의 명령 수명 추적을 만든다. production 설치는 후속 A3이며 새 runtime/CLI를 지금 실행하지 않는다.

- 소유 분리: `account_lease.py`/전용 시험은 별도 구현자, `runtime.py`/`commands.py`/실제 종료 인수는 부모가 소유한다. 구현 후 다른 리뷰어가 독립 검토한다.
- lease identity는 신뢰 설치자가 제공한 `RequestAccount`의 KIS·환경·계좌번호·상품코드 canonical digest다. caller의 scope 별칭/config version/DB 경로가 같은 계좌 독점을 나누지 않는다. 계좌 원문은 파일명·내용·repr·오류에 저장하지 않는다. 이 digest는 익명성이나 caller 인증 보장이 아니다.
- `AccountLease.acquire(root, account)`는 **명시 기존 private root**의 nonblocking Linux flock을 사용한다. root는 절대 경로·실행 UID 소유·0700·비symlink 디렉터리여야 한다. 정상화한 root가 다르면 보장이 분리되므로 설치 factory가 하나의 root를 강제해야 한다. 자동 디렉터리 생성/권한 변경은 하지 않는다.
- lock은 dir-fd 상대 O_NOFOLLOW/O_CLOEXEC로 열고 regular/단일 link/UID/0600을 확인한다. ancestor symlink도 거부하며 부모 디렉터리 전체에 root0700 조건을 잘못 적용하지 않는다. FD는 수명 동안 유지한다. `assert_held()`는 PID·root/lock inode·권한/경로 일치를 재검사한다. 최초 검증 실패는 영구 invalid이며 flock 재획득으로 자동 복구하지 않는다. close는 멱등이며 파일을 unlink하지 않는다. PID/TTL takeover나 stale 파일 삭제는 없다. fork 자식은 at-fork에서 상속 FD만 닫으며 LOCK_UN으로 부모 lock을 해제해서는 안 된다. callback이 모든 과거 lease 객체를 강하게 붙잡는 누적도 피한다.
- 서로 다른 checkpoint 경로라도 동일 계좌의 두 번째 lease는 실패한다. 다른 계좌 대조와 별도 spawn 프로세스/Pipe 인수를 둔다. lease 재획득은 기존 UNKNOWN/공식 startup 장벽을 해제하지 않는다. 다중 host·악의적인 동일 UID·다른 root·직접 broker 우회까지 막는다고 주장하지 않는다.
- runtime은 첫 await 전 명령 수명을 등록한다. prepare/dispatch/publisher의 이미 진행 중인 owner 작업과 늦게 생성되는 결과 저장 task를 같은 registry에서 추적한다. 종료 시 새 admission을 닫고 명령→결과의 fixed-point drain을 끝낸 뒤에만 외부 설치자가 broker/store/lease를 닫는다.
- 호출자 취소 시 미송신 POST를 shield로 새로 보내지 않는다. 이미 시작된 결과 저장은 strong task로 추적한다. shutdown 호출자의 취소가 이미 수락한 저장 task를 취소하지 않으며 다음 shutdown으로 drain을 재개할 수 있어야 한다. 저장 실패는 unhealthy이며 정상 종료 성공으로 표시하지 않는다.
- 내부 `_result_tasks`는 호환하되 runtime 종료가 이를 모르는 현재 단절을 없앤다. registry 등록 토큰은 trusted in-process capability이지 IPC 권한이 아니다. 같은 task의 중첩 호출을 잘못 조기 해제하지 않는다.

Do/RED: lease 신규 API 부재의 bootstrap 실패와 기존 shutdown의 의미 실패를 따로 센다. 실제 SQLite/runtime/commands/fake HTTP에서 (a) HTTP 응답 대기 중 종료, (b) shutdown 시작 뒤 생긴 결과 write, (c) caller 취소 뒤 기록, (d) DB/게시 실패, (e) 새 dispatch/publish 차단, (f) 중복 shutdown/종료 caller 취소를 고정한다. 고정 sleep 대신 Event/Pipe로 순서를 제어한다.

See: 관련 KST/UTC 회귀·독립 리뷰·전체 검증 후 한정 완료. 아직 gateway 설치/실제 SIGNAL/수동 writer 완료가 아니며 계좌 lease만으로 거래 허가가 생기지 않는다.

09-18 실행 결과: lease62·종료25시험, 독립 종료 P2 D1 수정·한정 재리뷰 승인. 전체 KST/UTC3381passed/기존xfail2·격리0. **10A1 두 조각 한정 완료**, 실제 설치/전체 writer는 다음 절의 미완 작업이다. 역할별 직접 검증·해시·경고는 후속 보고서에 기록했다.

## 10A2 — 원시각과 실제 정책 publisher

Plan: 공식 고정 revision의 KRX/NXT WS 체결 46필드 중 HHMMSS(index1)와 BSOP_DATE(index33)를 보존한다. 현재 len>=20/count 무시/원시각 폐기를 실제 parser RED로 고정한다. 다중 record 전체를 검증한 뒤 발행하며 잘못된 두 번째 행 때문에 첫 행만 승인되지 않게 한다. 원 시장 시각과 수신 시각을 구분하고 REST now로 as_of를 만들지 않는다.

Do: 동일 owner로 시세→진입 가격/보호→전략 입력을 직렬 게시한다. 유효 config·regime·macro·전략 손절·NXT 출처의 version/관측시각을 분리한다. 최신 실패가 이전 성공을 가리고 늦은 과거 응답이 덮지 못하게 한다. 새 임의 TTL·매수 가격 상한은 추가하지 않는다. 지원하지 않는 REST 시각 계약은 미지원이다.

See: 실제 WS 문자열→parser→owner, UTC/KST·역행·결측·다중행·실패 순서·최종 source mismatch를 검증한다. WS 영업일자가 잔고/체결 atomic cutoff의 증거는 아니다.

### 10A2a/b 작업 분리

- A2a(Terra/high): 순수 frozen MarketObservation·46필드 전체 frame parser·실제 feed·선택 Event provenance만 소유한다. KRX/NXT 원 TR, 원 날짜/시간과 수신 시각을 보존하고 기존 naive 이벤트 heap/US를 유지한다. 실제 callback→owner 설치는 포함하지 않는다.
- A2b(부모): 기존 보호 quote의 durable admission을 확장해 원관측을 보존하고 보호 적용과 entry quote를 **같은 commit**에 완료한다. 별도의 두 await publisher를 성공한 동일 자료라고 합성하지 않는다. 기존 손절 접촉 replay와 원본 provenance를 버리지 않는다. 접수 task·durable 미완·실패는 신규 SUBMIT prepare/claim/final의 장벽이며, 수량 노출을 늘리지 않는 CANCEL에 시장가 신선도 정책을 새로 부과하지 않는다.
- 현재 의미 RED4개: 실제 runtime.quote의 admission 전/후에서 멈추면 owner healthy임에도 prepare가 승인되고 dispatch가 fake HTTP를 송신했다. 먼저 이 장벽을 닫고 실제 DTO→완료 증거·재시작·동일시각·실패/취소 시험을 추가한다. 테스트 허가와 외부 fake는 운영 허가가 아니다.
- 기존 run_trader callback에는 scheduler의 theme/gap EOD 우선 청산도 있으므로 통째 삭제하거나 quote 뒤 그대로 재호출하지 않는다. owner 소단위·복구·실제 risk source·callback 순서로 별도 연결하며 아직 전체 A2 완료가 아니다.

### 10A2b3 — 위험 전이 계산과 시도 순서의 분리

Plan: 현재 `BatchAnalyzer.update_intraday_state`는 batch 값을 바꾸고 선제 청산을 await한 뒤 ExitManager를 바꾼다. 실제 메서드에서 A(crash)를 선제 처리 await에 멈추고 B(normal)를 완료한 다음 A를 재개하면 최종 `(batch, ExitManager)=(normal, crash)`다. 별도 ignored 원본 재현의 순차 대조는 GREEN, 겹친 호출은 의미 RED1이다. 현재 5분/정오 루프가 항상 병렬이라는 주장은 아니다.

- 첫 독립 단위는 `risk_transition.py`의 순수 후보 계산/전용 시험이다. 현재 공통 classifier와 실제 persist=False ExitManager clone으로 기존 정상↔caution/crash/severe 전이를 대조한다. 이전 batch state·분류 시각·recovery_until·보호 DTO를 명시 입력으로 받고 후보와 선제 stale 처리 필요 여부만 반환한다. 실제 emit/파일/시간 조회/owner/송신은 하지 않는다. raw 시장 시각을 분류 시각으로 대신 만들지 않는다.
- 유한 숫자의 기존 임계값, same-level no-op 보호 정책, normal 회복5분 cooldown, core/면제·레짐 복원은 변경하지 않는다. 결측/bool/비유한 입력은 기존 보수적 보호 상태를 유지하는 `missing` 후보이며 normal로 보정하지 않는다. 이 경계 검증은 시장 관측 성공의 증거가 아니다.
- 후속 root owner 연결은 I/O 시작 전에 durable latest-begun sequence를 발급한다. 완료에서 최신 시도·일자·의존 source versions를 확인한 뒤에만 순수 후보를 적용하고 risk/protection/effect outbox를 같은 commit으로 저장한다. 이전 성공/실패가 최신 상태를 덮지 못하며 duplicate 완료는 효과를 다시 실행하지 않는다. standalone RiskSnapshotPublisher에 동일 sequence의 pending/success를 두 번 publish하지 않는다.
- 실제 선제 stale 후보/emit을 owner effect로 옮기기 전에는 pure helper만으로 위 의미 RED가 해결됐다고 보고하지 않는다. 신규 helper import 부재 bootstrap, 기존 메서드 parity 최초 GREEN, 실제 writer 의미 RED를 구분한다.

See: 순수 전이 matrix·입력 비변경·직접 실제 메서드 대조 후 독립 리뷰. 이후 owner/begin-complete/실제 5분·정오·2분 caller 검증은 별도이며 전체 A2/단계3 완료가 아니다.

09-18 순수 전이 결과: author36·독립58을 각 KST/UTC로 실행해94개씩 통과, 두 파일 한정 승인이다. 실제 writer 중첩 원본은 여전히1 RED/순차1 GREEN으로 남는다. helper의 logger 호출, 같은 단계/결측 시 보호 DTO 전체 검증 생략, mutable 독립 반환 dict를 권한으로 보지 않는 한계를 후속 보고서에 기록했다.

### 10A2b4 — 실제 위험 source/writer 연결의 다음 경계

Plan: 5분 batch 전이, 정오 max-risk 캡/LLM, 2분 기술 추세는 같은 연산이 아니다. 동일 입력 계열별 latest-begun/terminal lane과 의존 source versions를 분리한다. 전역 단일 시도 번호로 서로를 취소하거나 정오 관측으로 batch 전이·회복 cooldown을 새로 실행하지 않는다.

- begin을 해당 외부 I/O 전에 내구 기록하고, 첫 owner await 전부터 신규 자동진입의 unknown 장벽을 둔다. 응답 누락/실패/취소와 같은 시도의 본문 충돌은 이전 정상 승인으로 복귀하지 않는다. 상태/정책 후보 계산과 side effect 실행을 분리한다.
- 5분 경로는 현행 전이·회복5분·normal→crash/severe 선제 stale 조건을 보존한다. 실제 core/면제·5영업일·미실현1% 미만 선택을 명시 snapshot으로 계산해 outbox에 넣고 이후 emit/주문 의도를 별도 멱등 전달한다. 후보 계산 중 emit await·파일 쓰기·live manager 변경은 하지 않는다.
- 정오는 현재 당일 batch/이번 지수/adapter 중 보수적인 캡을 먼저 저장하고, 느린 LLM 응답은 그때 사용한 lane·일자·dependency version이 아직 일치할 때만 게시한다. cache 파일은 projection이며 owner보다 앞선 진실로 취급하지 않는다. 2분 trend와 전문가/VIX 관련 입력도 source 성공·결측과 버전을 따로 보존한다.
- REST index receipt/분류 시각은 시장 as_of가 아니다. 근거가 없는 원시각은 unknown으로 유지하며 보호 파라미터 후보가 계산됐다는 사실을 자동매수 승인으로 승격하지 않는다. 추가 KIS 조회나 새 TTL/임계값을 도입하지 않는다.
- owner DTO는 aware KST 기준이다. 기존 batch cooldown 소비자의 naive `datetime.now()` 비교를 남긴 채 aware 값을 직접 주입하지 않는다. 실제 호출점 시험은 UTC/KST 호스트에서 이를 검증한다.
- shutdown/drain·일자 fence·저장/게시 실패·재시작 pending·중복 완료를 기존 owner 명령 수명과 결합한다. 미완 효과는 startup/실행 장벽을 우회하지 못한다. 기존 SIGNAL/ORDER gateway 전체 연결 전 outbox 저장만으로 정상 청산 전송 이행을 완료했다고 세지 않는다.

Do/See 예정: 기존 실제 writer RED1을 보존하고 새 실제 caller 시험(시도 시작 뒤 오래된 정상, 역순 성공/실패, 느린 LLM 동안 새 급락/일자 전환, 선제 emit 대기 중 정상 회복, 중복/복원)을 먼저 실패시킨다. 실제 클래스·임시 SQLite·fake 조회/LLM/HTTP만 사용한다. 전체 source hash 동결 검증이 끝난 뒤 공유 runtime/caller 수정을 시작한다.

552fb25 이후 구현 소유 분리: Astra/high는 새 `risk_sources.py`/전용시험의 durable 시도 수명, Terra/high는 새 `stale_exit_candidate.py`/전용시험의 실제 선제 stale 선택 parity, 부모는 실제 `fetch_index_price`의 원응답 자료 상태·공유 runtime/caller 연결을 담당한다. 같은 파일을 동시에 수정하지 않는다. 각각 독립 리뷰 후 조합한다.

KIS 지수 경계: 기존 numeric 반환/반올림·10초 캐시·GET/limiter 횟수는 유지하고 JSON-safe `_observation` 자료를 추가한다. 각 원 숫자 필드의 missing/invalid/valid 상태와 기존 정규화 값, 원 응답 ID·수신 aware 시각을 분리한다. 시장 `as_of`는 미입증 None이며 raw 누락/bool/nonfinite를 정상0으로 승인하지 않는다. cache 재조회는 원 ID/수신시각을 유지하고 caller의 반환 dict 변경이 보존된 원관측을 바꾸지 않게 한다. 이 metadata만으로 기존 scheduler가 자동으로 고쳐진다고 세지 않으며 실제 owned caller가 소비하는 시험을 뒤에 추가한다.

## 10A3 — 명시 설치 factory

선행 보호 재생 경계: 5분 writer가 실제 보호 DTO를 바꾸면 degraded 포지션의 기존 fill/quote 재생도 그 정책 입력을 알아야 한다. source ID·완료 version/digest·전후 정책·실제 보호 scope를 같은 commit에 기록하고, 실제 persist=False 전이로 재생한다. 누락/다른 source/잘못된 정책·과거 청산 결정은 계속 BLOCKED이며 회복 과정에서 경제/예약/R/outbox를 다시 적용하지 않는다. 현재 정책을 과거 체결 전체에 소급하는 복구는 금지다. 실제 큐 등록 실패→5분 정책→quote/추가 fill→repair/새 runtime 복원을 RED부터 확인한다. 5분 조각의 한정 승인은 이 재생 인수까지 완료했다는 뜻이 아니다.

같은 runtime/commands/builder/authority/guard/stop/session/lease를 묶는다. 실제 run_trader 종료는 admission 닫기→producer 정지→receipt/result drain→broker/store→lease 순서로 변경한다. raw KIS 우회는 거부하지만 B/C의 정상 경로까지 연결하기 전 운영 enable하지 않는다. US tuple 계약을 무단 변경하지 않고 startupFalse 대조를 유지한다.

## 10B — 기존 후보 파이프라인 보존과 실제 큐

1. B1 Plan/Do: 현재 `_calculate_position_size`의 산술만 작은 순수 kernel로 추출한다. nominal/hybrid/risk/core·강도·손실 감액·ATR/LLM/calendar/vol/conviction·전략 재클램프·최소금액/3주·1.3 affordability·139/140 위험 경계를 기존 함수와 비교한다. characterization 최초 GREEN은 RED로 세지 않는다. 전략 pending 근거는 원금 노출이 아니라 현재 남은 cash 예약이다. factor는 기존 첫 bucket/held-only/enforce shadow/오류 fail-open을 보존한다.
2. B2 Plan/Do: crossvalidator/LLM/시간 규칙 등 기존 qualification의 명시 출처를 immutable decision facts로 저장한다. sector·등록 손절·score는 broker 체결 metadata와 별개다. 실제 출처 version이 없으면 publisher를 먼저 만든다. final에는 현재 경제/다른 예약/같은 pure kernel을 재검사하며 기존 allow bool을 재사용하거나 500줄 on_signal을 복제하지 않는다.
3. B3 Plan/Do: 실제 SIGNAL→기존 후보 판단→gateway 준비→ORDER의 command ID→prepared dispatch를 연결한다. mutable Order/metadata로 wire·origin을 바꾸지 못한다. UNKNOWN 예약 유지, 취소 ACK와 최종성 분리, 지원 종결 이후에만 기존 fallback/잔여 목표를 사용한다. eviction SELL도 동일 owner다.
4. See: 실제 emit/큐/on_signal/on_order/정책/kernel/SQLite를 사용하고 HTTP만 fake한다. MARKET BUY·LIMIT/MARKET SELL 정상 대조와 CV/LLM/budget/factor/slot/session 및 각 network await 중 변화의 HTTP0을 검증한다.

### 10B1 추출 경계 (A2 독립 리뷰 중 선행 분석)

기존 `RiskManager._calculate_position_size`의 외부 호출과 순수 수치 계산을 분리한다. 외부 상태를 한 번에 선조회해 조기 거부 뒤 원래 없던 resolver/캘린더/변동성/conviction 호출이나 metadata 변경을 만들지 않는다. 먼저 기존 메서드에 actual Portfolio·RiskManager와 fake 외부 팩터/시계만 붙여 nominal/risk/core/hybrid 및 조기 반환의 고정 기대값·호출 순서·metadata 대조를 만든다. 최초 GREEN 특성화와 새 kernel 부재 RED를 구별한다.

- base/pool/core-reserve 산출에 쓰는 config·포지션 평가·현금 예약은 명시 불변 입력이다. float 곱셈 뒤 `Decimal(str(...))`로 옮기는 기존 순서를 바꾸지 않는다.
- 필요하면 초기 금액/전략 캡/일일손실 축소 후보와 overlay 이후 최종 수량을 별도 순수 함수로 나눈다. 실제 wrapper가 기존 위치에서 손절 resolver와 팩터를 읽고, risk tag와 최종 entry_risk 기록 시점도 유지한다. kernel은 파일·시계·callback·live metadata를 읽거나 수정하지 않는다.
- core risk 예외·하이브리드 pool·risk ATR 배율 제거 조건·최소금액 바닥·3주 보정·시장가1.3 affordability·수수료 포함 risk final cap을 동일 입력으로 대조한다. 새로운 가격상한/주문유형 금지는 추가하지 않는다. 기존 nominal/risk 승격 상태도 바꾸지 않는다.
- 실제 wrapper가 추출한 함수를 사용하기 전 순수 helper만 완료로 세지 않는다. 이후 B2/B3에서 final 재검사에 쓸 팩터/정책 source version과 현재 보유/예약을 연결한다. kernel 결과 자체는 승인 capability가 아니다.

## 10C — 나머지 실제 writer와 프로세스 경계

- C1: scheduler SAFE/USER와 별도 수동 CLI를 계좌 gateway/Unix IPC로 연결한다. 임의 metadata origin은 권한이 아니며 IPC 부재 시 직접 broker fallback은 없다. 실제 승인 지시 ID+계좌/종목/수량/가격을 묶고 disconnect/UNKNOWN 중복 주문을 막는다. 기존 manual .995 수량이 공통1.015 예약에서 부족하면 명시 거부하며 조용히 sizing을 변경하지 않는다.
- C2: 실제 조회 수집기→지원 누적 증거→lifecycle→inbox→실제 core receipt로 연결한다. scheduler의 별도 portfolio/ExitManager 감산·등록·risk clear·journal 중복을 제거한다. 주문 소유 decision facts와 broker 관측을 구분한다.
- C3: sync·quote·regime/crash·면제·일일 초기화·시작 writers를 순서대로 owner 명령으로 옮긴다. snapshot 부재/빈 조회/시간 초과는 예약 해제나 최초 인계 증거가 아니다. 각 writer의 실제 성공/실패 호출점 시험과 재검색 목록을 남긴다.
- See: 실제 전체 C/F/G/R, 전체 KST/UTC·비밀검사·독립 broad 리뷰를 수행한다. 미입증 최초 인계/체인·과거일 회계·legacy 분석 원장·장기 성능은 별도 미완으로 남긴다. **main 통합·운영 전환은 그 후 별도 판단이며 이번 로컬 구현의 자동 후속이 아니다.**
