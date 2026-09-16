# 토스 Phase 1 후속: 승인 기반 관측 런타임 설계

## 1. 상태·기준·승인 범위

- 설계 기준: main `c32de93f861694d83f94f09748ebccb12927f0cd` (#67 오프라인 기반 + #69 리뷰 도구 수정).
- 인계 대상: PR #68 `0b978e095ab1625a2f40b3669f0d06537df3f5e5`. 보류 상태를 유지한다. 병합·리베이스·전체 cherry-pick·닫기를 이번 문서로 승인하지 않는다.
- 사용자가 동의한 방향: #67을 유지하고 필요한 라이브 연결 책임만 기본 OFF로 재구성한다. 작성된 상세 설계의 확인을 거쳐 구현 계획/TDD/독립 리뷰로 진행한다.
- **2026-09-16 사용자 승인 범위의 오프라인 구현·검증/독립 소스 리뷰 완료.** 정확한 소스 SHA·인수 근거/한계는 [검증 원장](../../reviews/toss-runtime-2026-09-16.md)을 따른다. 이 설계는 실자료 수집 승인서가 아니며 인증 API·실토큰·운영 캐시·배포·재시작·주문·설정 변경 권한을 부여하지 않는다.
- 기존 상위 [T12 설계](../plans/2026-09-15-toss-securities-fallback.md) 및 [오프라인 검증 원장](../../reviews/toss-phase1-offline-2026-09-16.md)의 안전 계약을 보존한다. 충돌 시 이 문서는 후속 구현 범위만 좁히며 기존 금지 경로를 완화하지 않는다.
- 인계의 운영 SHA `8849d92`와 저장소 배포 기록 `c9923bf` 사이 변경은 문서 4개뿐이다. 현재 실행 PID/로드된 코드/운영 체크아웃은 이번에 조회하지 않았다. 새 main SHA를 실행 중 코드 SHA로 대체하지 않는다.

## 2. 선택과 제외

선택: **동일 봇 프로세스의 별도 관측 런타임**, #67 기반 위에 조립한다. 초기 버전은 KIS 캐시 스냅샷만 읽으며 추가 KIS HTTP 호출은 0이다.

검토한 대안:

1. #68 전체 병합/인증 모듈 교체: 기존 인증·보안·시간/예산·정규화 계약을 후퇴시키므로 기각.
2. 독립 수집 CLI: 기존 프로세스별 KIS 제한기와 단일 Toss 송신자 계약을 공유하지 못하므로 이번에는 기각. 기존 합성 replay CLI는 유지.
3. 중앙 시세 서비스: 별도 운영 서비스가 필요한 확장으로 현 단계에서 제외.

이번 구현 대상으로 제안하는 것은 승인 검증, 키 로더/OAuth 어댑터, 현재가·캘린더 관측, 지속 원장, 보고 및 수명주기다. **일봉 실관측 배선·새 KIS 조회/시각 수집·Phase 2 후보/점수·Phase 3 표시 폴백·호가/주문/청산/사이징은 제외**한다. 따라서 이 구현만으로 상위 Phase 1 전체 완료나 3영업일 인수를 선언하지 않는다.

## 3. 보존할 코드와 새 경계

| 책임 | 기존 코드/새 경계 | 계약 |
|---|---|---|
| 토큰 상태·저장 | 기존 `TokenManager`, `SecureTokenStore` | issuer/reader, revoked 지속 기록, unknown 잠금, 0600/0700, identity/origin 검증 유지 |
| 조회 | 기존 `TossClient`, `AiohttpTransport`, `GroupRateLimiter`, `RequestBudget` | 정확한 GET 3경로·고정 HTTPS·redirect 금지·공유 deadline/retry/page·단일 sender 유지; live 응답 크기 제한만 좁은 확장 허용 |
| 시세·일봉 | 기존 `Quote`, `CandleSeries`, `parse_prices`, `fetch_daily_candles` | nullable/finite/시각/시장 계약 유지, 일봉 런타임 배선은 미포함 |
| 합성 replay | 기존 `ShadowManifest`, `summarize_pairs` | synthetic/offline 한정과 `production_eligible=False` 유지 |
| 실행 승인 | 신규 `toss/approval.py` | 실관측 계획과 신뢰된 승인 영수증의 대조, 부작용 없는 검증 |
| 실제 발급 | 신규 `toss/oauth.py` | 별도 issuer 전용 POST, 자동 재송신 0, 가짜 HTTP로만 인수 |
| 조립·작업 소유권 | 신규 `toss/runtime.py` | 검증 후 lazy 생성, #67 API 연결, 중단/만료/cleanup |
| 관측·통계 | 신규 `toss/observation.py` | live/synthetic 라벨 분리, 현재가/캘린더 유효성 및 분모 |
| 지속 원장 | 신규 `toss/observation_ledger.py` | 슬롯/선정/시도/결과, 단일 writer, 재시작·손상 검출 |
| 스케줄러 | 신규 `src/schedulers/toss_shadow.py`, 기존 `KRScheduler.create_tasks`의 최소 훅 | 기존 거래 루프에 HTTP/디스크 await 추가 금지 |
| 보고 | 신규 오프라인 원장 리포트 CLI | 기존 파일만 읽고 인증/네트워크/키 로딩 불가 |

파일명은 구현 계획에서 확정하되 책임 경계를 합쳐 기존 대형 스케줄러에 넣지 않는다. #67의 순수 검증/산술을 공통화할 경우 별도 테스트를 먼저 고정하고 합성 public API·해시·fixture 결과를 보존한다. 실자료를 `dataset_kind=synthetic`으로 바꿔 기존 함수를 호출하지 않는다.

## 4. 실행 승인과 키 로딩 순서

### 4.1 서로 다른 세 가지 승인

- **구현 승인**: 문서·오프라인 코드/시험·PR 작업. 실자료 접근 없음.
- **실관측 승인**: 특정 관측 계획·기간·실행 주체·발급 권한으로만 수집. 자료가 0건이어도 최초 수집은 허용할 수 있다.
- **소비자 승격 승인**: 관측 결과 이후 별도 결정. 관측이 성공해도 이 설계에는 승격 기능이 없다.

`ObservationPlan`과 `LiveObservationGrant`를 별도 immutable 계약으로 둔다. 전자는 무엇을 측정할지, 후자는 누가 언제 어디서 그 계획을 실행하도록 승인했는지를 표현한다. 사용자가 관리하는 신뢰된 승인 파일의 안전한 loader만 grant를 런타임에 전달한다. 임의 입력 JSON의 `approved: true`, 환경 플래그, 파일 안의 자기 선언 승인자 이름만으로 grant를 인정하지 않는다.

신뢰원은 **서비스 UID가 수정할 수 없는 운영자 소유 승인 등록부**다. 등록부의 승인 plan 해시·기간·권한에 맞는 grant만 로딩한다. 전용 경로와 모든 부모의 소유자/쓰기 권한, 서비스의 읽기 전용 접근, symlink/하드링크 금지, 크기/스키마를 검증한다. 승인 파일 안의 owner 선언으로 실제 파일 소유권을 대신하지 않는다. 저장소/환경변수의 임의 경로를 승인 등록부로 사용하지 않는다. 실제 운영자 UID/GID·신뢰 경로가 승인된 배치 설정으로 제공되지 않으면 live loader는 거부한다. 별도 서명 체계는 이번 범위 밖이다.

운영자가 승인한 정확한 plan 원본 해시와 정규화 계약 해시를 함께 묶는다. 중복 JSON 키·미정의 필드·NaN·bool/정수 혼동을 거부한다. 이 검증은 서비스 코드의 오호출·오설정 방어이며, 이미 자격을 보유한 악성 프로세스 전체를 통제하는 공급자 read-only scope가 아니다. 실제 등록부 설치·권한 설정·grant 발행은 별도 사용자 지시가 필요하다. 저장소에는 합성 grant/plan만 둔다.

필수 승인 바인딩:

- 승인 식별자·근거 참조, 약관/저장 허용 근거, 발급 소유권 확인 근거.
- UTC aware `not_before`/`expires_at`, 관측일·세션, 비밀이 아닌 client identity.
- 승인된 host identity/운영 UID·고정 origin·전용 token/sender/ledger 경로·실행 역할.
- 승인된 코드 릴리스 식별자·설정/계획 해시·고정 공급자 스펙 버전/해시. 실행 중 코드는 시작 때 검증·고정한 릴리스 식별자를 사용하며, 나중에 바뀐 체크아웃 HEAD를 실행 코드로 보고하지 않는다.
- 조회 권한, 정상 갱신 권한, 별도 bootstrap 1회 허가를 구분. unknown/revoked 수동 복구 권한은 자동 런타임에서 지원하지 않는다.

승인 누락/만료/불일치/미지원 값은 fail-closed다. host/IP 소유권을 임의 환경변수 문자열이나 외부 IP 조회 성공만으로 증명하지 않는다. 공급자 허용 IP 및 외부 발급자 중지 확인은 승인 절차의 근거이며, 로컬 락이 이를 강제한다고 주장하지 않는다.

### 4.2 부작용 순서

1. 마스터 플래그는 기본 OFF. 명시된 정상 활성값 외에는 비활성/설정 오류로 처리한다. OFF면 승인 파일·키·토큰·원장 접근, client/session/worker 생성, 잡 등록 모두 0이다.
2. ON이면 **비밀 없는 별도 preflight I/O 작업만** 허용해 설정·승인 파일·plan·실행 identity·기간·역할·경로를 검증한다. 승인 입력 파일의 stat/open/read/hash도 거래 loop에서 동기 수행하지 않는다. 이 작업은 크기/기한/동시성1 상한을 갖고 KIS 기동/거래 작업은 이를 기다리지 않는다. 검증 중에는 Toss 상태가 `approval_pending`이며 인증/관측 task·키/store/HTTP factory 호출은 0이다. 거부 시 `unavailable`, deadline 뒤 남은 파일 I/O는 결과를 폐기하고 새 preflight를 겹쳐 만들지 않는다. 메타데이터 검증용 I/O 작업은 인증/관측 worker와 별개로 계수한다. OFF에서는 이 preflight도 0이다.
3. 검증 통과 뒤 전용 worker에서 I/O 없는 기존 store/token/transport/client 객체를 조립하고 `TossClient.start()`로 단일 sender 소유권을 확보한다. 그 뒤에만 토큰 파일을 읽거나 필요한 역할의 키를 lazy 로딩한다. 생성자 무부작용도 테스트하며 reader 관측에는 client secret이 필요 없다.
4. 최초 캐시 부재는 일반 조회에서 자동 발급하지 않는다. 명시된 bootstrap grant와 durable 1회 시도 기록을 확인한 별도 작업만 `TokenManager.bootstrap`을 호출한다. 권한 소진 후 캐시/파일을 지워 재시도하지 않는다.
5. 각 신규 GET/POST 직전, 그리고 대기 후 실제 송신 직전에 승인 유효성·역할·기한·송신 소유권을 다시 검사한다. 작업 deadline과 승인 종료 시각 중 이른 시각을 적용한다. 소급 시계 이동으로 권한 기간이 연장되지 않도록 시작 시 monotonic 상한도 고정한다.
6. 승인 만료·OFF·중단은 신규 작업/송신을 차단한다. 그러나 **이미 수신한 revoked의 지속 기록 및 발급 불명 상태·자원 정리**는 안전 후처리로 수행한다. 승인 만료를 이유로 #67의 첫 await 전 `observe_revocation`을 건너뛰지 않는다.

실제 grant/기간/host/수치 정책을 이번 구현에서 채워 활성화하지 않는다. 누락값은 실행 거부 사유이며, 구현 명세의 미정 동작이 아니다.

## 5. OAuth 연결과 실행 격리

OAuth 어댑터는 고정 HTTPS origin의 정확한 `/oauth2/token` POST만 수행한다. 일반 조회 client에 POST나 계좌/주문 경로를 추가하지 않는다. TLS 검증·redirect 금지·환경 proxy 미사용·cookie 미사용·라이브러리 내부 재송신 금지·bounded 응답 파싱을 인수한다. 요청/응답/예외 원문·인증 헤더·토큰 일부를 로그나 원장에 남기지 않는다.

실제 GET도 기존 transport의 무제한 `response.json()` 그대로 연결하지 않는다. GET/OAuth 양쪽 응답읽기 경계에 승인된 **wire/decoded byte 상한·중첩/행 수·문자열 길이·파싱 시간 예산**을 적용한다. Content-Length만 믿지 않고 스트림 누적과 압축해제 후 크기를 제한한다. 안전하게 제한할 수 없는 Content-Encoding은 거부한다. 초과/손상은 고정 오류 코드로 종료하고 부분 원문을 로그에 남기지 않는다. 필요한 변경은 `AiohttpTransport`의 응답 소비 경계에 한정한 확장으로 구현·별도 리뷰하며, 기존 URL/allowlist/retry/보안 계약과 synthetic 테스트는 유지한다. 전용 thread도 프로세스 메모리/GIL 격리는 아니므로 이 상한을 대체하지 못한다.

현재 `TokenManager`의 issuer 인터페이스는 인수 없는 async callable이다. 이를 바꾸거나 전역 mutable deadline을 두지 않고, `AuthorizedTokenProvider`가 `ContextVar`에 불변 `IssueContext(authority_hash, operation, deadline)`를 설정하고 finally 복구한다. 무인수 issuer closure가 이를 필수로 읽어 별도 OAuth 어댑터에 명시 전달한다. context 없는 직접 호출은 거부한다. 다른 task의 승인·예산을 물려받지 않는지, `wait_for` 자식 task와 취소에서도 올바르게 복구되는지 테스트로 고정한다. 발급 전 intent와 unknown 처리의 정본은 기존 `TokenManager`/`SecureTokenStore`다. 어댑터 자체는 재발급/복구/재시도를 결정하지 않는다. intent 이후 권한 만료로 POST를 하지 못해도 unknown을 자동 해제하지 않는다.

기존 `spec_contract.json`에는 OAuth 본문 형식·Content-Type·client 인증 전달 위치·성공 envelope가 없다. **구현 선행 작업으로 인증 없는 공식 공개 스펙을 확인하고 사용 부분의 버전/원본 해시/비밀 없는 fixture를 고정**한다. 추측한 form/JSON/Basic 방식으로 실제 어댑터를 만들지 않는다. 캘린더의 명시적 휴장 표현과 필수 세션 경계도 같은 방식으로 고정한다. 공급자 자료로 확정되지 않는 형태는 unsupported로 거부한다.

단순 별도 asyncio task만으로 디스크 격리를 주장하지 않는다. #67의 토큰 저장·revoked 기록은 동기 fsync를 포함하므로, **Toss 전용 단일 worker thread와 그 안의 전용 asyncio loop**가 인증·HTTP·원장 객체를 소유하도록 한다. 기존 KIS 객체·이벤트 버스·포트폴리오의 mutable 참조는 넘기지 않고 복사된 immutable 입력만 전달한다. 큐는 유한하며 한 번에 한 관측 작업만 실행한다. 이 구성은 독립 프로세스 수집이 아니고 KIS 네트워크를 호출하지 않는다.

worker를 기다리는 일은 신규 관측 감독 task만 한다. 주문·청산·동기화·REST 피드에 await를 추가하지 않는다. task-local asyncio 객체·sender 락·HTTP session은 worker에서 생성/정리하고, 기존 heartbeat 레지스트리는 메인 loop에서만 갱신한다.

중단 시 신규 제출 금지 → bootstrap을 포함한 모든 진행 작업 취소·회수 → #67 안전 후처리 → OAuth session close → 조회 client close → worker 종료 확인 순서다. 기존 `TossClient.close()`가 조회 session 정리 후 sender 락을 반환하므로, OAuth 작업/세션을 그보다 먼저 회수한다. 별도 OAuth POST를 shield하거나 백그라운드로 남기지 않는다. join을 거래 loop에서 동기로 기다리지 않는다. 커널 fsync/파일시스템 정지는 스레드 취소로 강제 종료할 수 없으므로 종료 제한을 넘으면 `stopping_unconfirmed`로 표시하고 새 worker/동일 자격 대체 발급을 금지한다. 후처리 완료 없이 '중단 완료'로 보고하지 않는다. 운영자 복구 절차를 별도로 제공한다.

## 6. 선정·시각·캘린더 계약

### 6.1 관측 입력

- 5분 슬롯은 승인된 날짜·세션과 고정 KST 경계로 결정한다. 단순 `sleep(300)` 누적으로 슬롯을 밀거나 재시작 때 과거 조회를 재송신하지 않는다.
- 보유 종목과 `bot._last_screened`의 **마지막 성공 결과**를 복사한다. 후보는 점수 내림차순·동점 종목 코드순 등 plan에 고정한 규칙으로 상위 N을 선정하고 보유와 중복 제거한다. 누적 `_watch_symbols[:N]`은 사용하지 않는다.
- 선정 시각·원본 성공 시각·규칙 버전·후보/보유 구분·최종 목록·snapshot ID를 원장에 먼저 고정한다. 후보 snapshot이 없거나 오래되면 `selection_partial`을 남기고 새 스크리닝을 실행하지 않는다. 보유 snapshot도 캡처 시점 기준임을 표시하며 브로커 동기화 성공 시각을 추정하지 않는다.
- `_last_screened_at`은 후보 선정 근거이지 가격의 시장 관측시각이 아니다. naive 시각을 조용히 KST로 확정하지 않는다. 필요한 경우 기존 성공 지점에서 shadow 전용 aware snapshot을 게시하는 최소 동기 복사 훅만 추가한다(HTTP/파일 쓰기/판단 변경 없음).
- KIS `current_price`만 있는 캐시는 가격 관측시각·수신시각·거래소 근거를 새로 만들지 않는다. `observed_at=None`과 부족 사유를 보존한다. 추가 KIS 조회/기존 quote payload 변경은 하지 않는다.
- `/prices` 요청 최대 200종목을 지킨다. 200개 초과는 동일 슬롯/전체 deadline·retry 예산으로 분할하고, 뒤 청크 HTTP·본문 파싱·예산 실패에서도 앞 청크 성공분과 미완 종목을 모두 보존한다. 초과/누락 종목을 조용히 버리지 않는다.

### 6.2 유효 가격 비교

`Quote`의 Decimal 가격·원 관측시각·수신시각·상태·통화·시장 기준을 유지한다. 두 가격의 finite/양수, 동일 종목·통화, 상태, 미래/노후, 관측시차, 명시된 비교 시장 기준을 전부 검사한다. 기준 불명·서로 다른 기준은 진단 행으로만 남긴다. fetch 완료 시각이나 파일 mtime를 관측시각으로 대체하지 않는다.

정규장과 다른 세션을 나누어 집계한다. 유효 쌍의 차이는 `100 * abs(toss-kis) / kis`(퍼센트), bp 표시는 이 값의 100배다. nearest-rank p95와 임계값 초과의 엄격한 `>` 비교를 고정한다. 정확한 Decimal/Fraction 산술을 유지하며 보고 직전 수치 표현 범위를 검증한다. 0 유효 표본은 p95/초과율 `None`, 상태 `insufficient`다.

**현 KIS 캐시 시각 부재와 공급자 시장 기준 미확정 때문에 첫 버전의 유효 비교가 0건일 수 있다.** 이를 숨기지 않는다. 공급자 관측 성공률·결측률·지연·정규화 품질은 진단할 수 있지만 가격 동등성/소비자 승격 증거로 쓰지 않는다. 미확정 기준을 관측 결과에 맞춰 완화하지 않는다.

### 6.3 캘린더

캘린더는 승인 plan에 명시한 일일 슬롯에서 별도 실행하며 기존 `KRSession.CLOSED`로 조회 자체를 막지 않는다. 현재가 루프의 장중 허용 창과 별개다. 요청일·today/previousBusinessDay/nextBusinessDay 구조, 타임존/순서, 세션 경계를 검증한다. 빈 객체·누락된 integrated·전일 응답을 정상 휴장으로 취급하지 않는다. 고정 스펙에서 정의한 명시적 휴장 응답만 휴장으로 인정하고 나머지는 unknown/partial이다.

조회 지연으로 날짜가 넘어가면 요청일 기준으로 기록하고 그 결과를 새 날짜의 성공으로 쓰지 않는다. 실패 재시도 횟수/간격은 plan에 고정하며 다음 틱마다 무제한 조회하지 않는다. 불일치는 원장/경고만 남기고 기존 세션 판정·거래 시간·주문 허용을 바꾸지 않는다. 캘린더 성공과 가격 관측 성공을 별개로 보고한다.

## 7. 원장·재시작·통계 분모

원장은 비밀 없는 고정 스키마의 append-only JSONL이며 단일 writer와 전용 안전 경로를 사용한다. 행에는 schema version·연속 sequence·이전 행 hash·현재 행 hash를 둔다. 현재 hash는 자기 hash 필드를 제외한 엄격한 canonical JSON 전체로 계산한다. 중복 JSON 키와 NaN을 거부하고 의미 해시와 원본 파일 hash를 구분한다. task 반환값이 아닌 **지속 기록 성공 확인**이 완료의 근거다.

1. 슬롯 예약 + immutable 선정 snapshot을 먼저 flush/fsync한다. 종목별 `attempt_id`는 plan hash·slot·kind·symbol·최초 snapshot ID로 결정한다. 같은 슬롯의 snapshot을 재선정해 중복 attempt를 만들지 않는다. snapshot 게시 후 개별 attempt 게시 전 중단도 복구 대상이며, snapshot의 선정 목록으로 미게시 항목을 `not_started/interrupted`로 지속 기록해 selected 분모에 남긴다. 복구 과정에서 해당 항목을 HTTP로 재조회하지 않는다.
2. 조회 전에 attempt를 지속 기록한다. 실패하면 외부 송신 0이며 `ledger_unavailable`로 관측을 중단한다.
3. terminal은 성공/제외/공급자 실패/예산 skip/취소/중단 등의 고정 사유와 관측을 포함한다. 같은 attempt에 논리적 terminal은 하나만 허용한다. 재시작/중복 제출은 조회를 반복하지 않는다.
4. 재시작 때 온전한 원장의 미완 attempt는 `interrupted` terminal로 닫고 다시 조회하지 않는다. 이미 terminal이 있으면 재집계만 한다. 물리적 쓰기의 exactly-once를 주장하지 않고 고유 ID·검증으로 논리적 중복을 통제한다.
5. 부분 JSON 행·잘못된 checksum/스키마·상충 중복·원장 write/fsync 오류는 파일을 삭제하거나 행을 버려 복구하지 않는다. `ledger_corrupt`/`ledger_incomplete`로 신규 송신과 승격 판정을 중단한다. 원본 보존 후 별도 승인 복구만 허용한다. 단순 정상 중단의 미완 attempt 복구와 디스크 손상 복구를 구분한다.
6. 종료 후 파일에 성공 결과가 늦게 쓰일 수 있는 상황에서는 완료 확인 전 성공 heartbeat를 갱신하지 않는다. 재시작의 지속 기록이 메모리 결과보다 우선한다.

분모는 `expected_slots`, `recorded_slots`, `missed_slots`, `selected_attempts`, `terminal_attempts`, `valid_pairs`, `excluded_pairs`, `provider_failures`, `budget_skips`, `interrupted`, `duplicate_submissions`로 구분한다. terminal의 배타적 주사유 합은 selected_attempts와 미완 수를 대조할 수 있어야 한다. 전원 장애 동안 선정 snapshot이 없던 슬롯은 `missed_slot`이지 상상한 종목별 attempt가 아니다. 원장 자체 기록 불가 시 전체 완결성을 주장하지 않는다.

같은 종목/양쪽 원 관측시각의 재사용은 서로 다른 attempt라도 가격 통계의 중복으로 제외한다. 분모에서는 제외 사유로 남기며 공급자 조회 자체를 없던 일로 만들지 않는다. synthetic/live 자료와 plan 버전/세션은 합쳐서 통과시키지 않는다. 모든 리포트는 `production_eligible=False`를 유지한다.

## 8. 하트비트·수명주기

기존 `loop_heartbeat`의 `record_attempt/record_success/record_failure/record_idle/set_enabled`를 최소 배선한다. 초기화 실패를 설정 OFF로 위장하지 않는다.

| 상태 | 보고 |
|---|---|
| 명시적 OFF | disabled, task/키/파일/네트워크 0 |
| 승인 누락·만료·소유권 불일치 | unavailable, 인증/관측 0, 원인 표시 |
| 예정 슬롯 밖/정상 선정 0건 | 사유 있는 idle, last_success 불변 |
| 계획한 모든 attempt의 terminal 지속 기록 확인 + 유효 Toss 관측 1개 이상 | 관측 success; KIS 쌍 부족이면 comparison=insufficient를 별도로 표시 |
| 공급자 전량 실패/전량 미유효 또는 원장 실패 | failure, last_success 불변 |
| 일부 성공/일부 실패 | success+degraded 및 성공/실패/제외 개수; 비교 통과로 표현 금지 |
| 예산·worker busy | skip 계수, last_success 불변; 연속 skip은 degraded/staleness로 드러냄 |
| 종료 확인 불가 | stopping_unconfirmed, 신규 worker/송신 금지 |

가격·캘린더의 마지막 관측 성공, 마지막 유효 비교, 마지막 원장 완료는 별도 필드다. 범용 하트비트의 success가 곧 유효 가격 비교를 뜻하지 않게 health/리포트에 상태를 함께 노출한다. 승인된 관측 주기/예정 창을 등록하는 작은 API로 기존 registry에 추가하고, 미등록/비활성 상태가 정상 성공으로 보이지 않게 한다. 기존 다른 루프들의 성공/유휴/정체 의미를 변경하지 않는다.

## 9. 실관측 전 반드시 채워야 할 값

구현용 합성 fixture에는 예시 숫자를 넣을 수 있지만 운영 기본값·실자료 승인값으로 승격하지 않는다. 실제 다음 항목이 모두 승인되기 전 실관측은 시작할 수 없다.

- 약관·자료 저장 허용 범위/보존기간, 승인 주체·기간·host/IP·자격 소유권·token 인계 또는 bootstrap 허가.
- 대상 날짜/세션·보유/후보 N·ETF/NXT/non-NXT 표본 정책·선정 snapshot 최대 나이.
- 단일 작업 deadline·종목/청크/페이지 상한·조회/발급 예산·그룹별 초기 한도·회로 기준·작업/정리 timeout·worker 큐/원장 용량 상한.
- KIS/Toss 관측시각·수신시각 근거와 필드별 freshness/시차·시장/수정주가 기준. 이번 버전의 추가 KIS HTTP 예산은 0 고정.
- 최소 유효 표본/커버리지·p95/초과율·공급자 실패율·관측 지연·허용 누락 슬롯·운영 부하 중단 기준. 최소 3영업일 등 상위 인수는 별도 유지.

미확정 항목을 결과를 본 뒤 채워 소급 통과시키지 않는다. 계획 변경은 새 버전·새 승인·새 관측 cohort다. 복구 명목의 토큰 삭제/재발급, 자동 플래그 전환, 승격/rollback 설정 변경은 이 런타임에 넣지 않는다.

## 10. 오프라인 인수와 구현 작업 경계

모든 신규 테스트는 가짜 HTTP/시계·임시 경로·환경 격리를 사용한다. 실제 자격·인증 API·운영 경로 0을 테스트 밖 호출 계수로도 단언한다.

| ID | 필수 인수 |
|---|---|
| R01 | OFF는 preflight 포함 task/파일0; ON+승인 없음/손상/만료/다른 host·release·hash는 비밀 없는 preflight만 허용하고 키/store/session/인증·관측 task0, 승인 파일 지연 중 KIS 감독 진행 |
| R02 | 승인 중간 만료·시계 역행·sender 경합·reader 비발급·bootstrap 1회·unknown 재시작 mint0 |
| R03 | POST 정확한 주소/메서드·redirect/TLS/proxy·GET/POST 큰/압축/중첩 응답 상한·timeout/취소/발급 후 저장 실패·raw secret 불출력 |
| R04 | GET 단일 예산·청크 공유 retry1·429 대기·전체 deadline·동시 복구 probe 기존 #67 회귀 유지 |
| R05 | 관측시각 없음/naive/미래/노후·부분/비정상 가격·통화/시장 불명은 p95 미포함, 0 유효는 None |
| R06 | 동일 슬롯 재시작/서로 다른 snapshot 제출/동일 관측 중복·snapshot 지속 게시 후 attempt 전 중단·조회 직전/직후 중단·terminal ACK 유실 |
| R07 | 원장 mkdir/write/fsync 실패·부분 행/상충 기록에서 송신 중단·분모 불완전 표시·성공 heartbeat 0 |
| R08 | 공급자 전량 실패/부분 성공/정상 0건/예산 skip·캘린더 독립 상태·연속 정체 |
| R09 | CLOSED일 캘린더 관측·빈/잘못된 날짜/불완전 세션·자정 경계·기존 세션 자동 변경 0 |
| R10 | 최신 성공 후보 정렬/동점/보유 중복·선정 시각≠가격 시각·실패/노후 snapshot·추가 KIS 조회 0 |
| R11 | 201개 이상 청크에서 후속 HTTP/본문 손상/예산 만료에도 성공분/미완분/시도 분모 보존 |
| R12 | worker 취소/파일 지연/반복 취소/큐 포화/종료 확인 불가 중 KIS 감독 heartbeat 계속 진행·새 sender 0 |
| R13 | 실제 금지 호출부 flag on/off × KIS 실패에서 Toss 유입/이벤트/portfolio/ExitManager/Order 지문 변화 0 |
| R14 | 기존 synthetic CLI/API·fixture 결과/해시·production_eligible=False 불변; 실자료 라벨 위조 불가 |

구현을 시작할 때 파일 소유권을 분리한다: A 인증/승인(상위 모델 high), B 원장/관측(일반 구현 high), C 부모 통합·스케줄러/문서. 승인 계약·관측 결과 인터페이스를 먼저 고정한 뒤 독립 부분만 병렬 구현한다. 각 구현자는 최종 승인자가 아니며, 보안/돈 경로 불변은 별도 최상위 모델 xhigh로 리뷰한다. 구체 모델은 당시 사용 가능한 목록과 프로젝트 규칙으로 명시한다.

검증 순서: RED → 최소 GREEN → 관련/금지경로 인수 → UTC/KST 전체 verify·비밀정보 검사 → 독립 리뷰/수정 → 명시 파일만 커밋·푸시 → PR/CI. 착수 직전과 병렬 작업 종료 직후 같은 경로의 열린 PR·main을 확인한다. 구현 PR을 만들기 전 #68 리뷰 프롬프트는 새 baseline/head/파일/검증 범위로 별도 작성하며 과거 SHA를 승인 근거로 재사용하지 않는다.

## 11. 구현 승인과 이후 경계

사용자가 **#67 보존, 기본 OFF, 실제 승인값 미발행, 추가 KIS 조회 0, Toss 전용 worker 격리, 불완전 자료는 비교 제외, 일봉/소비자 승격 별도**인 구현을 승인했다. 상세 [구현 계획](../plans/2026-09-16-toss-runtime-shadow.md)의 Plan→Do→See로 진행한다. 설계 문서 작성·테스트 기준선 통과 자체는 위 R01~R14의 구현 완료 증거가 아니며, 실관측·운영 설치/배포는 계속 별도 승인 대상이다.
