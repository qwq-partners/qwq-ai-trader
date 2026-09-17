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

## 10A2 — 원시각과 실제 정책 publisher

Plan: 공식 고정 revision의 KRX/NXT WS 체결 46필드 중 HHMMSS(index1)와 BSOP_DATE(index33)를 보존한다. 현재 len>=20/count 무시/원시각 폐기를 실제 parser RED로 고정한다. 다중 record 전체를 검증한 뒤 발행하며 잘못된 두 번째 행 때문에 첫 행만 승인되지 않게 한다. 원 시장 시각과 수신 시각을 구분하고 REST now로 as_of를 만들지 않는다.

Do: 동일 owner로 시세→진입 가격/보호→전략 입력을 직렬 게시한다. 유효 config·regime·macro·전략 손절·NXT 출처의 version/관측시각을 분리한다. 최신 실패가 이전 성공을 가리고 늦은 과거 응답이 덮지 못하게 한다. 새 임의 TTL·매수 가격 상한은 추가하지 않는다. 지원하지 않는 REST 시각 계약은 미지원이다.

See: 실제 WS 문자열→parser→owner, UTC/KST·역행·결측·다중행·실패 순서·최종 source mismatch를 검증한다. WS 영업일자가 잔고/체결 atomic cutoff의 증거는 아니다.

## 10A3 — 명시 설치 factory

같은 runtime/commands/builder/authority/guard/stop/session/lease를 묶는다. 실제 run_trader 종료는 admission 닫기→producer 정지→receipt/result drain→broker/store→lease 순서로 변경한다. raw KIS 우회는 거부하지만 B/C의 정상 경로까지 연결하기 전 운영 enable하지 않는다. US tuple 계약을 무단 변경하지 않고 startupFalse 대조를 유지한다.

## 10B — 기존 후보 파이프라인 보존과 실제 큐

1. B1 Plan/Do: 현재 `_calculate_position_size`의 산술만 작은 순수 kernel로 추출한다. nominal/hybrid/risk/core·강도·손실 감액·ATR/LLM/calendar/vol/conviction·전략 재클램프·최소금액/3주·1.3 affordability·139/140 위험 경계를 기존 함수와 비교한다. characterization 최초 GREEN은 RED로 세지 않는다. 전략 pending 근거는 원금 노출이 아니라 현재 남은 cash 예약이다. factor는 기존 첫 bucket/held-only/enforce shadow/오류 fail-open을 보존한다.
2. B2 Plan/Do: crossvalidator/LLM/시간 규칙 등 기존 qualification의 명시 출처를 immutable decision facts로 저장한다. sector·등록 손절·score는 broker 체결 metadata와 별개다. 실제 출처 version이 없으면 publisher를 먼저 만든다. final에는 현재 경제/다른 예약/같은 pure kernel을 재검사하며 기존 allow bool을 재사용하거나 500줄 on_signal을 복제하지 않는다.
3. B3 Plan/Do: 실제 SIGNAL→기존 후보 판단→gateway 준비→ORDER의 command ID→prepared dispatch를 연결한다. mutable Order/metadata로 wire·origin을 바꾸지 못한다. UNKNOWN 예약 유지, 취소 ACK와 최종성 분리, 지원 종결 이후에만 기존 fallback/잔여 목표를 사용한다. eviction SELL도 동일 owner다.
4. See: 실제 emit/큐/on_signal/on_order/정책/kernel/SQLite를 사용하고 HTTP만 fake한다. MARKET BUY·LIMIT/MARKET SELL 정상 대조와 CV/LLM/budget/factor/slot/session 및 각 network await 중 변화의 HTTP0을 검증한다.

## 10C — 나머지 실제 writer와 프로세스 경계

- C1: scheduler SAFE/USER와 별도 수동 CLI를 계좌 gateway/Unix IPC로 연결한다. 임의 metadata origin은 권한이 아니며 IPC 부재 시 직접 broker fallback은 없다. 실제 승인 지시 ID+계좌/종목/수량/가격을 묶고 disconnect/UNKNOWN 중복 주문을 막는다. 기존 manual .995 수량이 공통1.015 예약에서 부족하면 명시 거부하며 조용히 sizing을 변경하지 않는다.
- C2: 실제 조회 수집기→지원 누적 증거→lifecycle→inbox→실제 core receipt로 연결한다. scheduler의 별도 portfolio/ExitManager 감산·등록·risk clear·journal 중복을 제거한다. 주문 소유 decision facts와 broker 관측을 구분한다.
- C3: sync·quote·regime/crash·면제·일일 초기화·시작 writers를 순서대로 owner 명령으로 옮긴다. snapshot 부재/빈 조회/시간 초과는 예약 해제나 최초 인계 증거가 아니다. 각 writer의 실제 성공/실패 호출점 시험과 재검색 목록을 남긴다.
- See: 실제 전체 C/F/G/R, 전체 KST/UTC·비밀검사·독립 broad 리뷰를 수행한다. 미입증 최초 인계/체인·과거일 회계·legacy 분석 원장·장기 성능은 별도 미완으로 남긴다. **main 통합·운영 전환은 그 후 별도 판단이며 이번 로컬 구현의 자동 후속이 아니다.**
