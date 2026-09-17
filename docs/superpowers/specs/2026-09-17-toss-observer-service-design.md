# Toss 독립 관측 서비스 — 기존 거래 봇 유지 설계

## 1. 상태와 사용자 요구

설계 기준은 main `8ff2f55f4515d049fbcd5fa1eea009b3ffc9f164`다. 사용자는 **기존 거래 봇을 유지하는 별도 관측 서비스, Toss ON, 단일 발급 주체, 추가 KIS 조회 0, 주문 영향 없음**을 선택했다. 단일 발급 소유권·외부 발급 중지·조회 및 원장 저장 허용은 이미 확인받았다. 이 사실을 다시 묻거나 실제 키를 대화에 요청하지 않는다.

**사용자가 09-17 후속 `ㄱㄱ`로 상세 설계 및 구현·검증 후 별도 서비스 ON을 승인했다.** [실행 계획](../plans/2026-09-17-toss-observer-service.md)으로 진행하며 아직 설치·활성화 완료가 아니다. 이전 배포의 실행 코드/PID는 [운영 기록](../../reviews/toss-pr-integration-2026-09-17.md)으로 구분한다. 아래 수치·권한은 이번 제한 관측 정책이지 기존 운영값이나 공급자 권장값이 아니다. 20:30 KST 착수로 첫 예정일 장이 끝나, 09/18·21·22와 09/22 18:00 만료로 옮길지 별도 확인 중이다. 답변 전 날짜를 자동 변경하지 않는다.

이 설계는 [09-16 설계](2026-09-16-toss-runtime-shadow-design.md)의 **동일 봇 프로세스 배치 선택만 대체**한다. 승인·발급·전송·원장·결측·승격 금지 계약은 보존한다. 독립 프로세스에서 KIS를 직접 조회했던 대안과 달리 이번 서비스는 KIS 클라이언트도 자격도 갖지 않는다.

## 2. 선택한 구조와 비변경 범위

| 구성 | 책임 | 허용하지 않는 것 |
|---|---|---|
| 기존 `qwq-ai-trader.service` | 현재 코드·PID·KIS 거래/조회·대시보드 유지 | 이번 작업으로 재시작, `.env`/전략/킬스위치 변경, Toss 배치 주입 |
| 새 `qwq-toss-observer.service` | 단독 Toss issuer/sender, 현재가·캘린더 관측, 전용 상태/원장 | 거래 봇 시작/import, KIS 호출, 주문/청산/신호/사이징 변경 |
| root 보호 launcher·release·승인 파일 | 실행 코드/설정/plan/grant 검증 및 서비스 권한 분리 | Git HEAD만으로 실행 증명, 서비스가 자기 승인을 수정 |
| 기존 로컬 `GET /api/positions` | 보유 코드의 best-effort 입력 | 새 스크리닝·잔고 동기화, 가격 시각/시장 기준 추정 |

기존 봇의 Toss 경로는 계속 OFF, 새 서비스에만 `TOSS_API=1`을 제공한다. 새 프로세스는 `scripts/run_trader.py`를 호출하거나 import하지 않는다. 기존 singleton/PID 파일·거래 상태 파일을 사용하지 않는다. 관측 결과는 봇으로 되돌려 주지 않는다. 기존 대시보드 `/api/health`에 새 서비스 상태가 자동 노출된다고 주장하지 않는다.

전체 거래 봇을 고정 릴리스로 옮기는 방식은 변경 범위·재시작이 필요해 선택하지 않는다. 가변 체크아웃을 그대로 import하는 얇은 wrapper도 실행 증명 요구를 만족하지 못하므로 사용하지 않는다.

같은 호스트의 CPU/메모리와 대시보드 직렬화 비용까지 0이라고 주장하지 않는다. **추가 KIS 요청 및 거래 판단/주문으로의 데이터 경로가 0**이라는 의미이며, 자원 한도와 배포 전후 점검으로 간접 부하도 제한한다.

## 3. 기존 코드 재사용과 새 책임

- `src/data/providers/toss/`의 `Deployment`, `StartupAttestation`, `Preflight`, `TossWorker`, `build_app`, `ObservationRunner`, `ObservationLedger`와 토큰/HTTP 구현을 유지한다. 중복 OAuth·token manager를 만들지 않는다.
- 새 `src/observation/toss_service.py`는 별도 서비스 수명주기·슬롯·신호 종료·상태 파일을 소유한다. `src/schedulers/toss_shadow.py`의 순수 슬롯 계산 등은 재사용 가능하지만 `TossShadowSupervisor(bot, ...)`와 `attach_shadow(bot)`는 사용하지 않는다.
- 새 `src/observation/toss_positions.py`는 고정 loopback GET·bounded JSON·보유 코드 검증만 수행한다. 거래 객체/자격을 받지 않는다.
- 새 `scripts/ops/toss_observer/`에는 표준 라이브러리 기반 launcher, 고정 릴리스 패키징·검증·설치, 전용 systemd unit과 제한 원장 보존 작업을 둔다. 설치 명령은 dry-run/preflight와 실제 설치를 분리한다.
- 일반 봇 `requirements.txt`/venv는 변경하지 않는다. 관측용 의존성은 `aiohttp`와 실제 전이 의존성만 버전·배포물 해시로 고정한다. 검증된 버전에서 시작하며 묵시적 최신 업그레이드를 하지 않는다.
- 기존 source/원장 스키마를 편의상 완화하지 않는다. 추가 서비스 정책은 외부 배치 설정의 `config_hash`에 묶고 해당 설정을 엄격히 검사한다.

## 4. 실행 신뢰와 권한

### 4.1 파일과 사용자 경계

| 경로/주체 | 소유·접근 | 용도 |
|---|---|---|
| 시스템 사용자 `qwq-toss-observer` | 비-root, 로그인/추가 그룹/sudo 없음 | 관측 서비스 전용 UID/GID |
| `/opt/qwq-toss-observer/releases/<artifact-digest>/` | root 소유, 서비스 쓰기 불가 | 승인한 소스·launcher·독립 의존성 |
| `/etc/qwq-toss-observer/` | root 소유·부모 포함 서비스 쓰기 불가 | deployment, plan, registry, 승인 manifest |
| `/etc/qwq-toss-observer/credentials.env` | root 소유 0600, systemd가 읽어 전달 | Toss ID/secret만; 기존 `.env` 전체 공유 금지 |
| `/var/lib/qwq-toss-observer/` | 전용 UID 소유 0700 | 토큰/송신 락/원장/시작 영수증·진단 |

실제 UID/GID·host·정확한 OAuth client_id·artifact/config/plan 해시는 설치 전 로컬 실값으로 산출해 서로 대조한다. launcher는 실제 UID/EUID/GID/보조 그룹도 승인 identity와 대조한다. 자리표시자·별명·샘플 identity로 발급하지 않는다. 키 이전은 명시한 Toss 2개 항목만 값 출력 없이 처리하며 원본 `.env`는 보존한다. 설치 도구에 임의 쉘 실행이나 `.env` source 기능을 넣지 않는다.

자격 전달과 사용은 다르다. systemd는 root-only EnvironmentFile을 **프로세스 시작 전에** 읽으므로 자격은 시작 환경에 이미 존재한다. 여기서 승인 실패 시 자격 접근0은 **애플리케이션 credential accessor 호출0**을 뜻한다. OS의 자격 provisioning이나 프로세스 환경 자체의 비밀 부재까지 보장하지 않는다. 환경 전체 덤프·자식 프로세스 전달·core dump는 금지한다. 승인 전 환경에도 자격이 없어야 하는 설계로 확대하지 않고 기존 lazy `environment_credentials()` 계약을 그대로 사용한다.

단일 자격의 `tokens/`, `sender.lock`, 시작 영수증 경로는 **grant/release가 바뀌어도 고정**한다. 기존 토큰·bootstrap/revoked/unknown 기록을 새 경로로 피하지 않는다. cohort 원장만 plan 해시별로 구분한다. 다른 발급자가 중지됐다는 운영 확인을 로컬 파일 락이 전 세계에서 강제한다고 주장하지 않는다.

### 4.2 실제 시작 증명

launcher는 root 보호 경로의 시스템 Python을 **`-I -S`**로 시작한다. `-I`만으로는 막을 수 없는 site 초기화·system/user `.pth`·`sitecustomize`의 검증 전 실행을 `-S`로 차단한다. 검증 전 검색 경로는 root 관리 표준 라이브러리뿐이며 repo·사용자 site-packages·`PYTHONPATH`·현재 디렉터리에서 프로젝트 모듈을 찾지 않는다. **프로젝트/제3자 import 전에** root 신뢰 manifest와 릴리스 파일 목록/내용 해시/권한/파일 종류를 검증하고, 그 뒤 승인된 소스/의존성 경로만 직접 추가한다(site 초기화/`.pth` 처리를 재호출하지 않는다). 미등재 import 가능 파일, symlink/hardlink 탈출, 쓰기 가능한 부모/의존성, 손상·누락은 거부한다. manifest 자체 해시는 별도 root 승인 deployment의 예상 digest와 맞아야 한다.

artifact digest는 canonical manifest의 해시로 정의한다. manifest는 릴리스 소스·설치된 의존성의 파일별 해시/모드와 검증된 Python 버전을 포함하며 자기 자신·운영 상태·비밀을 포함하지 않는다. OS Python/표준 라이브러리/공유 라이브러리/CA는 root 관리 플랫폼 신뢰 경계로 명시한다. 애플리케이션 artifact가 OS 전체까지 불변으로 만든다고 표현하지 않는다. 가변 봇 venv는 이 경계 밖이므로 import하지 않는다.

배치 설정 canonical hash에는 서비스 identity·고정 상태 경로·관측 입력/자원/시작 정책·plan raw/canonical 해시·artifact digest를 포함한다. `config_hash` 자신의 값, grant/registry 전체 해시, 비밀을 hash 입력에 넣어 순환 참조를 만들지 않는다. plan/grant의 실제 파일 내용은 기존 `load_authority`도 재검증한다. `StartupAttestation`은 이 검증을 통과한 실행값으로만 만든다.

서비스에는 `ProtectSystem=strict`, `ProtectHome=true`, `PrivateTmp=true`, `NoNewPrivileges=true`, `UMask=0077`, `LimitCORE=0`, 빈 capability 집합, 전용 상태 경로만 쓰기 허용을 적용한다. 첫 정책은 `CPUQuota=25%`, `MemoryMax=192M`, `TasksMax=16`, `Nice=10`, `IOWeight=10`이다. 실제 호스트의 지원 여부와 정상 시작/종료를 검증하며 미지원 설정을 조용히 생략하지 않는다. `Restart=no`, `KillMode=control-group`, 정상 정리 예산 10초/systemd 종료 상한 20초를 사용한다. 강제 종료는 정상 정리 완료가 아니며 관련 상태를 보존한다. KIS 호스트에 대한 OS 방화벽 차단까지 구현됐다고 주장하지 않는다; 코드/HTTP allowlist·자격 미제공·금지 호출 테스트를 따로 인수한다.

## 5. 단일 발급과 제한된 ON

여기서 단일 발급은 **발급 주체가 관측 서비스 하나**라는 뜻이다. 토큰 만료에 따른 정상 갱신까지 영원히 금지한다는 뜻은 아니다. 첫 grant는 issuer/query/renewal/bootstrap을 명시 허용하며 `auth_max_issues=4`(bootstrap 포함 실제 POST 최대 4회)를 제안한다. POST 자동 재송신은 계속 0, revoked 재발급도 0이다. 예상보다 수명이 짧아 예산이 부족하면 관측을 중단하고 보고하며 자동 증액하지 않는다.

기존 OAuth 카운터는 worker 수명당 한도다. **이를 재시작 누적 한도로 오해하지 않도록** 새 서비스는 grant별 한 번의 worker 수명만 허용한다. 승인/릴리스 검증 후 애플리케이션 credential accessor 호출·토큰 접근·worker 생성 전에 고정 상태 경로에 grant 시작 영수증을 배타 생성하고 파일·부모 디렉터리 fsync를 완료한다. 실패/기존 영수증/손상은 시작 거부다. 시작 이후 장애라도 영수증을 지우지 않고 같은 grant 자동 재시작을 금지한다. 이는 공급자의 평생 발급 횟수 보장이 아니라 승인된 launcher 경로의 재시작 제한이다.

새 grant는 기존 프로세스 종료·sender 소유권 반환을 확인한 후 별도 운영자 승인으로만 발행한다. 새 grant도 토큰/unknown/revoked/bootstrap 기록을 보존하고 기존 정상 토큰을 우선 읽는다. 불명 발급 상태의 수동 복구를 일반 재시작 절차에 넣지 않는다.

서비스 `enabled`와 조회 성공은 다르다. 시작 시 승인된 bootstrap 1회는 허용되지만, 관측 GET은 plan 슬롯에서만 보낸다. 장외에는 정상 대기 상태이며 가격 수집 성공이라고 표시하지 않는다. 만료 후 신규 작업·POST/GET을 차단하고 종료한다. 정리 완료 불확실이면 `stopping_unconfirmed`이며 후속 worker를 만들지 않는다.

## 6. 추가 KIS 조회 0인 입력

첫 cohort는 **보유 종목만** 관측한다. 후보 선정·ETF/NXT 표본 확장·일봉은 이번 서비스 인수에서 제외한다. `candidate_limit=0`, `candidates=()`, `source_success_at=None`을 사용한다. 고정 시험 종목을 가짜 보유로 넣지 않는다.

근거: 기존 `/api/positions`는 `get_positions()`의 메모리 포지션을 직렬화한다. 이름 캐시 갱신과 로컬 stock-master 파일 읽기는 있을 수 있으나 `_load_stock_master_sync(allow_network=False)`여서 추가 KIS/pykrx/FDR 요청은 없다. 따라서 이는 **mutable 봇 상태의 best-effort 읽기**이며 원자적인 브로커 잔고·최신 체결 성공 스냅샷이 아니다. `/api/screening`은 성공 provenance가 없는 여러 캐시를 합칠 수 있어 사용하지 않는다.

- 입력 URL은 정확히 `http://127.0.0.1:8080/api/positions`, 메서드 GET, query/body/인증 헤더 없음이다. 다른 route/주소·리다이렉트·환경 proxy·cookies·자동 재시도는 금지한다. 관측 슬롯당 한 번만 읽고 장외 반복 조회하지 않는다.
- 입력 전체 timeout 2초(연결 0.5초), 본문 최대 64KiB, JSON 깊이 8/노드 4096/문자열 1024자, 행 최대 64개다. 최대 20개의 중복 없는 6자리 KR 보유 코드를 코드순으로 선택한다. 입력 행/심볼 형식 또는 양수 유한 수량이 잘못되면 해당 응답은 입력 실패이며, 조용한 종목 누락으로 정상 처리하지 않는다. 20개 초과는 원장 `overflow_symbols`에 명시한다.
- 계좌·수량·가격·종목명 전체 응답은 보존하지 않는다. 보유 여부 검증 뒤 코드만 사용한다. 첫 cohort의 `kis_quotes={}`로 고정해 시간/시장 근거가 없는 `current_price`를 비교에 사용하지 않는다. snapshot에는 기존 strict 필드만 사용하고 `selection_partial=True`를 유지한다.
- 응답 완료 시각은 `input_last_transport_success_at`일 뿐 봇의 동기화 성공/시장 관측시각이 아니다. `source_success_at`을 채우거나 파일 mtime를 시장 시각으로 쓰지 않는다.
- 유효한 HTTP 200의 정상 `[]`만 `idle/empty_selection`이다. timeout·파싱·스키마 실패는 `input_unavailable`이며 Toss 가격 GET 0, 성공 시각 갱신 0이다. 기존 원장에 해당 가격 슬롯을 missed로 남기고 별도 sidecar 상태/고정 사유 로그로 입력 실패를 구분한다. `record_missed`의 idle 반환값을 정상 입력 성공으로 올리지 않는다. 원장 실패면 서비스도 실패·중단한다.
- localhost라는 이유로 인증된 데이터 소스라고 표현하지 않는다. 배포 때 포트가 기대 거래 봇에 연결됐는지 확인하고, 기존 공개 dashboard 보안 설정을 이번 작업에서 임의 변경하지 않는다. 입력은 관측 대상 선택에만 쓰며 주문/가격 동등성 판단의 신뢰원은 아니다.

## 7. 첫 관측 정책과 결과 해석

다음은 승인받을 첫 plan의 명시 정책이다. 날짜는 **관측 예정일**이며 영업일 인수 통과를 미리 뜻하지 않는다. 설치가 해당 일정에 맞지 않으면 과거 데이터를 채우거나 날짜를 자동 연장하지 않고 새 계획을 제시한다.

| 항목 | 값 |
|---|---|
| plan/dataset | `toss-observer-holdings-20260917-v1` / `live` |
| 예정 날짜 | `2026-09-17`, `2026-09-18`, `2026-09-21` |
| 가격 창·주기 | KST `regular` 09:00 이상 15:20 미만, 5분 정각 격자(일 76슬롯) |
| 캘린더 | 각 예정일 08:55, 현재가와 별개; 지연해도 해당일만 실행 |
| grant 기간 | 설치·최초 시작 직전 확정한 UTC not_before부터 2026-09-21 18:00 KST 미만, 최대 7일 |
| 선정 | 후보 0 / 보유 우선·코드순 / 최대 20 / snapshot-age 300초(후보 없는 이번엔 미사용) |
| 작업·정리·preflight | 20초 / 10초 / 5초; import 전 artifact 검증은 별도 30초 상한 |
| GET 예산 | 작업당 max_pages 2 / 전체 retry 1 / PRICES·CANDLES·MARKET_INFO 각 1req/s; live CANDLES 호출 0 |
| 회로 | 연속 실패 3회 / 300초 open |
| Toss 응답 | 최대 256KiB / 깊이 8 / 노드 10000 / 문자열 16384 / 파싱 0.2초 |
| 원장 | cohort 16MiB 상한, 넘으면 중단; 보존 30일 |
| 비교 정책 | age 60초 / skew 5초 / outlier 0.5% / min_valid_pairs 100 / expected_market_basis=`unknown` |
| 관측 인수 | 아래 정의의 coverage 각각 ≥0.95 / provider 실패율 ≤0.05 / 전체 missed slots ≤11 / latency ≤20초 / 확인된 영업일 ≥3 |
| 비교 인수 | p95 ≤0.5% / outlier rate ≤0.05; 이번 cohort는 비교 근거가 없어 **판정 불가** |

공급자 스펙은 기존 `1.2.17`과 pinned SHA256을 그대로 사용한다. 신형 응답을 만나면 unsupported/실패로 기록하며 실측에 맞춰 스펙/임계값을 자동 변경하지 않는다. 날짜별 영업일 확인 근거가 부족하거나 캘린더가 partial/휴장이면 3영업일 충족으로 세지 않는다. 기존 봇 세션·휴일 설정은 바꾸지 않는다.

가격 228개+캘린더 3개가 전체 계획 슬롯이다. 시작 이전 지나간 슬롯은 missed이며 재송신하지 않는다. 중간 보고의 미래 예정 슬롯을 장애로 오인하지 않게 full-plan 분모와 경과 슬롯을 구분한다. 가격/캘린더 성공·보유 선정 부족·입력 실패·공급자 실패·예산 skip을 각각 보고한다.

비율은 가격/캘린더를 합치지 않고 각각 계산한다. **슬롯 coverage**는 유효 Toss 관측 ≥1이고 모든 선정 attempt의 terminal ACK가 끝난 슬롯 수 / 종료된 예정 슬롯 수다. 정상 빈 보유는 idle로 따로 세되 분자에는 넣지 않는다. **attempt coverage**는 유효 Toss observation 수 / selected_attempts다. provider 실패율은 provider_failures / selected_attempts이며 budget skip·interrupted·미완을 분모에서 빼지 않는다. 두 coverage 모두 0.95 이상이어야 한다. 분모0은 None/insufficient이고 전체 selected_attempts의 terminal 미완 또는 원장 오류가 있으면 통과 불가다. comparison 제외만으로 유효 Toss observation을 실패로 바꾸지는 않는다. 최소 영업일은 동일 예정일의 유효 개장 캘린더와 가격 관측 ≥1이 모두 있는 날짜로 센다. 전체 종료 전 결과는 잠정값이지 최종 인수가 아니다.

latency는 worker 제출 직전부터 모든 terminal ACK를 확인한 결과 수신까지의 monotonic 초이며, 완료 작업별 최댓값으로 판정한다(입력 GET의 2초 예산은 별도로 보고). 신규 서비스의 private `status.json`은 schema·plan/grant/release·PID·시작/갱신 시각·상태·가격/캘린더별 누적 슬롯/attempt/observation 계수·latency 최대/표본수·입력 수신 성공/관측 성공/원장 완료의 분리 시각을 저장한다. 최대 16KiB·0600·단일 writer·원자 교체와 fsync를 사용하며, 원장 ACK 이후 갱신한다. 상태 쓰기 실패는 관측 중단이다. 원장 ACK 후 상태 쓰기 전 중단 등으로 계수가 대조되지 않으면 원장을 수정하거나 지연을 추정하지 않고 해당 인수는 incomplete/None이다. 기존 `ObservationLedger.summary()`의 missed 포함 회계 완결성과 위 인수 비율을 혼동하지 않으며, 현 CLI가 이 신규 판정을 이미 구현했다고 보고하지 않는다.

첫 단계의 성공은 **제한된 Toss 실관측·정규화·지속 기록·안전한 종료**다. KIS 유효 비교 0건, p95/outlier=None, comparison=insufficient, `production_eligible=False`가 정상적으로 남을 수 있다. 보유 한 종목의 성공으로 후보 전체 품질·최적 매수·가격 fallback/소비자 승격을 주장하지 않는다. 결과를 보고 사전 정책을 소급 완화하지 않는다.

보존기간은 cohort 종료 후 30일이다. 설치하는 전용 로컬 정리 timer는 승인된 cohort 원장 경로만 대상으로, 기한·실행 서비스 종료·symlink/파일 동일성을 재검사해 만료 원장을 제거하고 비밀/가격/보유 없는 삭제 영수증을 남긴다. 원본 관측자료는 삭제 후 복구 보장을 하지 않으며 보고한다. **토큰·revoked/unknown/bootstrap·grant 시작 영수증·sender lock·거래 상태·공유 디렉터리 재귀 삭제는 금지**한다. 정리 실패는 경고/미완료이며 보존 정책이 지켜졌다고 보고하지 않는다. 새 network-facing 상태 서버나 텔레그램 자격은 추가하지 않는다.

## 8. 운영·검증과 Plan → Do → See

### Plan — 이 문서 확인 후 구체 구현 계획

상세 설계를 승인받아 파일/API 소유권과 인수 테스트를 분리한 [실행 계획](../plans/2026-09-17-toss-observer-service.md)을 작성했다. 같은 경로의 열린 PR/main을 착수 전과 병렬 작업 종료 후 확인한다. 원래 설계 단계의 문서-only 경계는 아래 구현·검증 후 새 서비스만 활성화하는 승인 범위로 전환됐다.

### Do — 역할별 병렬 구현, 접점은 부모 통합

- A 보안·launcher/승인·시작 영수증: **Astra/high**. 토큰/HTTP 정본을 보존하며 설치 권한 경계를 담당한다.
- B 입력 adapter·관측 서비스·상태·테스트: 계획은 **Terra/high**, 실제 해당 슬롯을 확보하지 못해 **부모**가 격리 worktree에서 구현했다. 인증 파일을 수정하지 않고 확정된 Deployment/입력 계약을 소비한다. 별도 sealed-runtime 통합 인수는 **Terra/high**가 담당한다.
- C 의존성 packaging/설치·보존: **Astra/high** 격리 구현. 부모는 계획·문서·통합·실제 운영 실행을 담당한다. 같은 파일 동시 편집을 금지한다.
- 최종 독립 리뷰: 구현자와 다른 **Astra/xhigh**. 구현/검증 후 승인 전에 운영 ON하지 않는다.

### See — 반드시 확인할 인수

| ID | 필수 증거 |
|---|---|
| S01 | OFF: 애플리케이션 관측 설정 접근·credential accessor·토큰·원장·HTTP·worker0. ON+승인/릴리스 오류: credential accessor/OAuth/GET0. systemd의 시작 전 자격 전달은 별개 |
| S02 | root anchor/manifest·소스·의존성 변조, writable parent, symlink/hardlink, user-site/PYTHONPATH 주입 거부; system-site `.pth`/`sitecustomize` 선실행0 |
| S03 | 동시 시작/같은 grant 재시작/시작 영수증 fsync 실패에서 두 번째 worker/발급0; 고정 sender/token 경로 유지 |
| S04 | grant 만료·시계 역행·발급 예산 소진·revoked/unknown·자격 불일치·POST 응답 유실의 재송신0 |
| S05 | loopback exactGET, redirect/proxy/큰 응답/손상 JSON/행 초과/잘못된 코드·수량; 실패≠정상 빈 보유 |
| S06 | 후보0·보유 중복/overflow·과거 슬롯·가격 시각 None·valid_pairs0·p95None·production_eligibleFalse |
| S07 | 입력 실패 missed와 failure 상태, 전량 공급자 실패, 원장 write/fsync 실패에서 last_success 불변; 빈 보유/skip/미래 슬롯/분모0·상태와 원장 불일치가 인수 통과로 바뀌지 않음 |
| S08 | SIGTERM/timeout/실제 미정리·worker busy·systemd 강제 종료 후 안전 기록 보존, 자동 재시작/복구0 |
| S09 | KIS/broker/주문/스캔/동기화 호출 감시 모두0, 거래 봇 entrypoint import0; 기존 돈 경로 회귀 불변 |
| S10 | 최소 의존성 릴리스의 오프라인 시작·중지·install dry-run, UTC/KST 전체 verify·비밀정보 검사·독립 리뷰 |
| S11 | 보존 timer는 승인 cohort만 처리, 기한 전/실행 중/경로 탈출/다른 UID/손상 receipt는 삭제0; auth 안전 기록 영구 보존 |
| S12 | 운영 전후 기존 서비스 PID/시작 시각·설정/킬스위치 지문 동일, KIS 연결/오류/미체결/하트비트 점검 |

실제 설치는 검증된 commit·artifact를 사용하고 **새 관측 서비스만** 시작한다. 기존 `local_deploy.sh`는 거래 봇 재시작을 포함하므로 이 배치에 사용하지 않는다. 첫 오류 시 관측 서비스만 중단하고 토큰/원장을 보존한다. main 병합과 운영 체크아웃 갱신도 실행 봇의 lazy import에 영향을 줄 수 있으므로 이번 관측 배포를 위해 기존 운영 checkout을 바꾸지 않는다. 원격 main/고정 릴리스 SHA와 기존 운영 checkout/실행 SHA를 따로 보고한다.

운영 완료 보고는 서비스 ON/PID/릴리스·승인 만료, 단일 worker/sender, 실제 OAuth/GET/원장 결과, 기존 봇 무재시작 증거를 구분한다. 장외 대기만 확인했으면 그 사실만 보고하며 3영업일 인수/소비자 승격은 완료로 표시하지 않는다.

## 9. 이번 설계 준비의 검증 기록

- 2026-09-17 main `8ff2f55` 기준 새로운 격리 작업공간에서 전체 verify: **1684 passed / 기존 xfailed 2 / 기존 pykrx warning 1**, 83.71초, 외부 네트워크·운영 상태 접근 시도0, 문법·비밀정보 검사 통과.
- Astra/high는 실행 증명/issuer/의존성 경계를, Terra/high는 dashboard 캐시/숨은 호출/시각/선정 계약을 읽기 전용 조사했다. 이는 새 서비스 구현의 S01~S12 인수 결과가 아니다.
- 별도 Astra/xhigh의 문서/소스 대조에서 Important 2건(Python site 선실행, OS 자격 전달과 accessor 경계 혼동)·Minor 1건(인수 산식 모호)을 지적받아 수정했다. 로컬 표준 Python은 `-I`에서 site_loaded=True, `-I -S`에서 False임을 직접 확인했다. 신규 launcher의 실행 인수로 계산하지 않는다.
- 같은 독립 리뷰어가 변경 문서 7개를 재검토해 기존 3건 해소·신규 Critical/Important 0·사용자 상세 설계 확인에 제출 가능으로 판정했다. 구현 승인/인수/ON 완료 판정은 아니다.
- 05:51:58 KST 읽기 전용 운영 조회: 기존 거래 서비스 active/PID3274983/00:47:47 시작 유지, 새 관측 서비스 LoadState=not-found. 이번 턴에 운영 재시작/변경은 하지 않았다.
- 신규 소스·서비스 사용자·systemd unit·grant·자격 파일·운영 플래그는 아직 만들거나 변경하지 않았다. 문서 확인 이후의 구현·설치·실관측 결과는 별도 원장에 기록한다.
