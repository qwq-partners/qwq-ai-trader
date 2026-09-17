# Toss 관측 런타임 운영 경계

상태: **09-17 21:47:37 KST 별도 `qwq-toss-observer.service` ON(PID3335469, release877768e)**. 초기 토큰 발급 성공·장외idle이며 가격/캘린더 관측은 아직0건이다. 기존 거래 봇은 재시작하지 않았고 Toss도 계속 OFF다. 사용자가 확정한 관측일은09/18·21·22, 승인 만료는09/22 18:00 KST다. 실제 설치·검증 증거와 미완 인수는 [관측 원장](../reviews/toss-observer-service-2026-09-17.md)에서 구분한다.

## 독립 서비스의 추가 경계

- 거래 봇 재시작·checkout 갱신·키/전략/킬스위치 변경 없이 별도 sealed release를 실행한다. 기존 `local_deploy.sh`는 거래 봇을 재시작하므로 사용하지 않는다.
- root-only Toss 전용 EnvironmentFile과 root 보호 launcher/registry/plan/deployment를 사용한다. `/usr/bin/python3 -I -S`가 프로젝트/의존성 import 전에 manifest·실제 UID/GID/host·plan/config hash를 검사한다. OS Python/표준 라이브러리/CA는 별도 플랫폼 신뢰 경계다.
- grant당 durable 시작 영수증을 자격 accessor/worker 전에 소비한다. `Restart=no`이며 같은 grant 재시작·영수증 삭제·토큰/unknown/revoked 초기화는 금지한다. 새 grant는 기존 소유권 반환 확인과 별도 운영 승인 대상이다.
- 기존 `/api/positions`를 슬롯당 최대1회/2초/64KiB로 읽되 전체 응답은 보존하지 않는다. 정상 `[]`만 빈 보유이며 입력 장애는 missed+input_unavailable, 성공 시각 갱신0이다. 후보0·KIS 비교0·주문 경로0이다.
- 전용 `status.json`은 입력 수신/관측 성공/원장 완료 시각을 구분한다. 실제 원장과 계수 불일치·상태 저장 실패면 중단하며 성능 인수는 incomplete/None이다. 기존 `/api/health`에 새 서비스가 자동 연결되지는 않는다.
- 가격/캘린더 coverage를 합산하지 않는다. 가격 ended 분모는5분/세션 종료, 캘린더는 예정시각+작업20초 기준이다(같은 날 늦은 조회는 허용). full-plan·경과 분모/선정 attempt·budget skip을 별도 표시한다. 0분모는None이며 장외 ON은 관측 성공이 아니다.
- 보존 timer는 승인 cohort 종료+30일, 서비스 inactive 및 sender 락 반환 뒤 정확한 원장만 삭제한다. token/starts/unknown/revoked/grant/거래 상태는 삭제 대상이 아니다. 실제 삭제 시 복구 보장 없음과 삭제 영수증을 별도 보고한다.
- observer는 start1회만 수행하며 boot 자동 시작은 enable하지 않는다. retention timer만 enabled다. **같은 grant로 restart/start 재시도 금지**: 실패/재부팅 후 영수증·토큰을 지우지 말고 종료/소유권 및 새 승인 필요 여부를 확인한다. 이상 시 새 관측 서비스만 중단하고 기존 거래 봇은 유지한다.

## 설치된 관측의 확인 절차

- `systemctl show qwq-toss-observer.service -p MainPID -p ActiveState -p NRestarts`와 전용 state의 승인 cohort `status.json`을 읽는다. 실제 경로는 root 보호 `/etc/qwq-toss-observer/deployment.json`의 `status_path`/`ledger_path`다. 전체 deployment·프로세스 환경·토큰·자격 파일을 로그/대화에 덤프하지 않는다.
- 상태에서 `state`, `updated_at`, `expires_at`, `by_kind`, `last_observation_success_at`, `last_ledger_complete_at`, `accounting_consistent`를 확인한다. 장외idle/0표본을 실패 또는 가격 수집 성공으로 바꾸지 않는다. 첫 캘린더08:55/가격09:00 이후 원장·상태 계수 대조가 필요하다.
- 09/22 18:00 만료 후 서비스 종료·sender 락 반환·추가 송신 중단을 별도 확인한다. 이번 활성화가 미래 종료를 이미 검증했다는 뜻은 아니다. 이후30일 보존 기한(10/22 18:00)을 넘긴19:00 timer부터 원장 삭제 대상이며 auth 안전 기록은 보존한다.

아래는 기존 **봇 내부 supervisor의 참조 계약**이다. 독립 서비스의 설치/활성화·상태는 위 설계/원장을 우선한다.

아래 수명주기/health 설명은 구현된 **봇 내부 관측 supervisor** 계약이다. 후속 독립 서비스는 기존 거래 봇을 재시작하지 않고 별도 상태를 제공하며 기존 `/api/health` 자동 연결을 주장하지 않는다. 이 문서 자체를 실제 grant로 사용하지 않는다.

## 무엇이 달라지는가

기본 `TOSS_API=0`은 승인 파일·토큰·원장·worker·잡 생성 0이다. 정확히 `1`인 경우에도 운영자가 승인한 배치 객체, 읽기 전용 승인 등록부, 계획, 시작 시점 실행 증명이 모두 일치해야 관측을 시작할 수 있다. 다른 truthy 문자열은 설정 오류다. 환경 플래그는 승인서가 아니다.

현재가와 KR 캘린더만 별도 worker thread에서 읽는다. 기존 KIS 브로커·청산·주문·포트폴리오에는 Toss 자료를 주입하지 않는다. 추가 KIS 조회는 0이며 일봉 live/후보 점수/표시 fallback은 미포함이다. 기존 `replay_toss_shadow.py`의 synthetic 계약도 그대로다.

관측 성공은 **유효 Toss 응답 + 모든 예정 attempt의 terminal 지속 기록**을 뜻한다. KIS와의 가격 비교 유효는 별도다. 현재 KIS 캐시의 원 관측시각/수신시각과 양쪽 시장 기준이 불명인 경우 비교에서 제외하므로, 관측이 성공해도 유효 비교 0·p95=None·insufficient일 수 있다. 모든 보고서는 `production_eligible=False`다.

## 실관측 전에 별도로 승인·설치해야 하는 것

1. 약관·자료 저장/보존 허가, 자격 소유권과 외부 발급자 중지 확인, 허용 host/IP, 승인 기간/근거. 로컬 lock은 다른 호스트의 발급을 차단하지 못한다.
2. 서비스 UID가 수정할 수 없는 운영자 소유 registry. leaf/부모 owner·쓰기권한·symlink/hardlink·파일 크기를 검증한다. 임의 환경변수 경로를 trust anchor로 사용하지 않는다. registry는 비밀이 아니며 서비스에 읽기만 허용한다.
3. strict `ObservationPlan`과 원본/canonical SHA256에 결합된 grant. 날짜/세션/선정/예산/비교/인수·보존 정책은 모두 명시값이며 샘플 숫자가 운영 기본값이 되지 않는다. 스키마의 정본은 `approval.py`와 구현 계획이다.
4. `Deployment`의 explicit `RegistryTrust`/`ExecutionIdentity`, grant ID·plan 경로·preflight 시간 상한. 전용 token directory/sender lock/ledger 경로를 기존 운영 상태와 분리해 프로비저닝한다. 임의 디렉터리 권한을 런타임이 완화하지 않는다.
5. **코드 로딩/프로세스 시작 시 신뢰된 launcher가 고정한 `StartupAttestation`**: release ID·config hash·artifact SHA256·host·실제 서비스 UID. 승인된 artifact digest 및 실행 identity와 대조한다. 실행 도중 읽은 Git HEAD는 실행 코드 증거가 아니다. 이 저장소 변경은 launcher/배치 anchor를 설치하지 않으며, 증명이 없으면 live 거부를 유지한다. 실제 immutable release 설치는 별도 승인 작업이다.
6. 토큰 reader와 issuer, query/renewal/bootstrap 권한을 분리한다. reader에는 secret loader가 필요 없다. issuer의 환경 키는 승인된 실제 발급 필요 시에만 읽으며 `.env`를 이 모듈에서 로딩하지 않는다. bootstrap은 별도 durable 1회 권한이며 실패 후 재시도/파일 삭제로 초기화하지 않는다.

이 런타임의 `client_identity`는 OAuth의 비밀이 아닌 **정확한 `client_id`**다. 별명/추정 매핑을 지원하지 않는다. lazy 자격 로더가 실제 `client_id`와 grant를 대조하여 다르면 POST 전에 거부한다. intent 이후의 자격 불일치 실패는 기존 보수적 unknown 처리로 남으며 자동 재발급/복구하지 않는다.

`auth_max_issues`는 **worker 수명당 POST 시도 상한**이며 재시작 시 새 카운터다. grant 전체의 누적 발급 한도라고 보고하지 않는다. 승인 기간/renewal 권한 검사와 durable bootstrap 1회는 별개로 유지한다. 재시작을 포괄하는 발급 예산이 필요하면 추가 설계·검증 없이는 활성화하지 않는다.

운영자의 승인 없이 여기서 플래그를 바꾸거나 grant/키를 발행하지 않는다. 현재 배치 객체·launcher 연결 제공자가 없다는 것은 의도적인 실행 거부 조건이며, 이 코드의 오프라인 통과를 실관측 완료라고 보고해서는 안 된다.

## 수명주기와 중단

ON → 비밀 없는 단일 preflight(thread, 크기·기한 제한) → 승인 확인 → Toss worker/thread 자체 loop 생성 → sender lock → 원장/토큰 → 관측 순이다. 기존 거래 loop는 승인 파일이나 fsync를 기다리지 않는다. worker에는 불변 데이터만 전달하고 broker/engine 참조를 전달하지 않는다.

가격은 KST 5분 정각 격자와 승인 세션에 맞춘다. 세션 시작이 09:02이면 첫 슬롯은 09:05다. 과거 슬롯은 missed로 기록하며 과거 종목을 상상해 조회하지 않는다. 캘린더는 별도 일일 슬롯이고 기존 CLOSED 판정으로 차단하지 않는다. 캘린더 관측은 세션/휴장 정본을 변경하지 않는다.

중단은 신규 제출/송신 차단 → 진행 작업(bootstrap 포함) 취소·회수 → OAuth close → query client close/sender 반환 → 실제 thread 종료 확인 순이다. 이미 받은 revoked 기록이나 issuance_unknown은 권한 만료·중단 후에도 안전 후처리로 보존한다.

`stopping_unconfirmed`면 완료로 간주하지 않는다. kernel 파일 I/O는 thread 취소로 강제 종료할 수 없다. 같은 프로세스의 대체 worker를 생성하지 않으며 기존 sender/worker 소유권을 임의 해제하지 않는다. 운영자는 파일시스템·프로세스의 실제 종료 상태를 별도 승인된 점검으로 확인한 뒤 복구 방향을 결정한다. 원장·토큰·lock 파일 삭제, unknown→ready 덮어쓰기, 자동 재발급/재시작으로 우회하지 않는다.

## 원장과 리포트

원장은 합성 replay와 분리된 private JSONL이다. 슬롯 선정 snapshot → attempt → terminal 순서의 fsync ACK가 송신/완료의 근거다. 재시작은 정상 미완 기록을 interrupted로 닫으며 재조회하지 않는다. partial tail/checksum/스키마/상충 기록은 원본 보존·fail-closed이며 자동 truncate/repair를 하지 않는다.

오프라인 CLI는 명시된 기존 원장만 읽는다:

```text
venv/bin/python scripts/review_toss_observation.py \
  --ledger <approved-ledger-copy> --plan-hash <canonical-plan-sha256> \
  --max-bytes <approved-limit>
```

이는 자리표시자 설명이며 실행할 운영 명령이 아니다. 보고 모드는 인증/토큰/HTTP를 생성하지 않고 입력 파일을 수정하지 않는다. 입력 부재/권한/손상은 정상 0표본과 구분한다. 전체 plan의 예정 슬롯과 recorded/missed/unaccounted, 선택/terminal, provider 실패/예산 skip/interrupted, 유효/제외/중복을 분리해서 읽는다. 미래 예정 슬롯이 남은 full-plan coverage와 물리 원장 완결성은 서로 다른 개념이다.

## 상태 확인 의미

- `kr_toss_prices`, `kr_toss_calendar`는 별도 승인 일정/성공 시각을 가진다. `/api/health` 기존 loop 상태에 observation 진단이 추가된다.
- success: durable complete + 유효 Toss 관측. 비교 통과/승격을 의미하지 않는다.
- degraded/failure: 부분/전량 실패, 전량 무효, 예산 부족, 원장 오류. last_success를 갱신하지 않는다(부분 성공은 degraded note와 함께 관측 성공만 기록).
- idle: 정상 선정 0건 등 실제 할 일 없음. success 시각은 갱신하지 않는다.
- comparison/last_valid_pair, last_ledger_complete, skip count를 관측 last_success와 별도로 본다.

오프라인 회귀·독립 리뷰·최종 검증 증거는 후속 구현 보고서에 SHA와 함께 기록한다. 실관측 최소 영업일 인수·시장 기준 확정·소비자 승격은 아직 수행되지 않았다.
