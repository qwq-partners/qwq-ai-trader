# 토스 Phase 1 오프라인 구현·검증 원장 (2026-09-16)

## 범위와 현재 상태

이 문서는 [검토된 설계](../superpowers/plans/2026-09-15-toss-securities-fallback.md)의 **Phase 1 오프라인 부분**을 다룬다. 인증 실자료 shadow, 운영 스케줄러, 소비자 폴백 도입까지 완료했다는 의미가 아니다.

- 시작 기준: main `8849d92`, 설계 PR #65 head `2235586`. 설계 PR을 임의로 병합하지 않고 별도 `feature/toss-phase1-offline-20260916`에서 구현했다.
- 계획: [구현 계획](../superpowers/plans/2026-09-16-toss-phase1-offline.md). **오프라인 구현·최종 코드 리뷰 승인**, 검증 소스 `2bf7842`. 전체 리뷰에서 발견한 결함을 수정하고 한정 재리뷰로 닫았다. 작업별 승인·테스트 통과를 전체 승인으로 대체하지 않았다. 09-16 후속 사용자 요청으로 [설계 PR #65](https://github.com/qwq-partners/qwq-ai-trader/pull/65) → [구현 PR #67](https://github.com/qwq-partners/qwq-ai-trader/pull/67) 순서의 main 통합을 진행한다. 각 PR의 실제 상태/merge SHA가 병합 정본이다.
- 기본 OFF, 운영 호출부에 import/배선 없음. 주문·체결·잔고·청산·사이징·후보/점수·설정·의존성 파일은 변경 범위 밖이다.
- 실제 자격증명/토큰/운영 캐시를 읽지 않았고, 인증 API·SSH·systemctl·배포·재시작·주문을 실행하지 않았다. 공개 OpenAPI JSON을 인증 없이 조회한 것과 합성 transport/session 검증을 구분한다.

## 고정한 공개 계약

[OpenAPI JSON](https://openapi.tossinvest.com/openapi-docs/latest/openapi.json)을 2026-09-16 확인했다.

- 버전 `1.2.17`, 원본 바이트 SHA-256 `791082da4cb379117ed9fdc29a45bd42746f7a1aec368da1e9f4e1f3bfbff5b4`.
- 저장소의 `tests/fixtures/toss/spec_contract.json`은 원본 전체가 아니라 사용 계약의 메타데이터 요약이다. 요약 파일 자체의 해시를 원본 명세 해시와 혼동하지 않는다.
- 조회 allowlist는 `GET /api/v1/prices`, `GET /api/v1/candles`, `GET /api/v1/market-calendar/KR`뿐이다. 주문·계좌·호가·수급·랭킹·임의 URL은 포함하지 않는다.
- 일봉 `before`는 inclusive이며 `nextBefore`를 그대로 전달한다. 일봉 자정 시각은 봉 날짜이지 실시간 관측 신선도 근거가 아니다. 공급자 시장/수정주가 기준의 실자료 확인은 여전히 미완이다.

## 구현·검증 원장

| 초기 구성요소 작업 | 담당 역할 | 초기 검증/리뷰 상태 (통합 후속은 아래 별도) |
|---|---|---|
| 보안 토큰 저장·issuer/reader 상태 | Astra/high 구현, Astra/xhigh 리뷰 | Important2 전부 수정·재리뷰 승인, 전용102건 |
| 조회 경계·송신자 락·공통 예산 | Astra/high 구현, Astra/xhigh 리뷰 | Important2 전부 수정·재리뷰 승인, 전용89건 |
| 현재가/일봉 정규화·전체 구간 확보 | Terra/high 구현, Astra/high 리뷰 | Important5/Minor2 전부 수정·재리뷰 승인, 전용41건 |
| 합성 비교 manifest·CLI·통합 | Terra/high 구현, Astra/high 리뷰 | Important3/Minor3 전부 수정·재리뷰 승인, 전용41건·실제 모듈 통합 |

기준선 전체 검증: `1019 passed / 2 xfailed`, 격리 위반 0. 기존 `pykrx` 경고 1건. 초기 통합본 `298bc4c`는 테스트 **273건 증가**이며 이후 통합 리뷰 보완을 추가했다.

구성요소 1차 독립 리뷰는 Important 12건·Minor 5건이었으며 전부 수정·한정 재리뷰를 통과했다. shadow I2는 1차 수정 후 큰 정수 정밀도 경계를 추가 재현했고, `Fraction` 기반 비교로 재수정한 뒤 승인됐다. 실제 TokenManager→TossClient→가격/일봉→shadow 통합도 승인 범위에 포함된다. 별도 정규화 테스트 advisory는 정상 양성 대조군을 추가해 실제 adjusted gate를 검증하도록 보완했다(런타임 무변경); 전체 브랜치 리뷰에 함께 포함한다.

초기 정규화/CLI 테스트의 모듈 부재 수집 오류를 행동 검증 증거로 세지 않는다. 이후 독립 리뷰 지적은 기능별 실패를 직접 재현한 RED→GREEN으로 검증했고, 이미 정상인 통합/테스트 보완은 특성화 검증으로 구분했다. 하위 브랜치의 전체 통과 수를 합산하지 않고 부모 통합 트리에서 다시 검증한다.

### 부모 통합 검증 (`298bc4c` + 이 문서 변경)

```bash
env -i PATH=/usr/bin:/bin LANG=C.UTF-8 TZ=Asia/Seoul \
  PYTHONDONTWRITEBYTECODE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
  PYTEST_ADDOPTS='-p no:cacheprovider --tb=short -rx' \
  QWQ_DEPLOY_SSH_KEY=/tmp/qwq-offline-no-ssh-key \
  QWQ_VERIFY_PYTHON=/home/ubuntu/projects/qwq-ai-trader/venv/bin/python \
  bash scripts/dev/verify.sh
```

- KST: **1292 passed / 2 xfailed / 1 warning**, 36.80초, exit 0.
- 같은 명령에서 `TZ=UTC`: **1292 passed / 2 xfailed / 1 warning**, 36.77초, exit 0.
- 두 실행 모두 Python 문법·비밀정보 패턴 검사 통과, 운영 상태·외부 네트워크 접근 시도 0건. `git diff --cached --check` 통과.
- xfail 2건은 기존 live/backtest 손절 수수료·익절 touch 차이이며 이번에 숨기거나 추가하지 않았다. warning은 기존 pykrx importlib-resources deprecation 1건이다.
- 부모가 아래 합성 CLI도 직접 실행해 exit 0, 시도6/유효2/p95=1/초과비율0.5/상태 `insufficient` 및 `production_eligible=False`를 확인했다.
- 이 소스와 문서를 포함한 `14210d4`의 [원격 Verify](https://github.com/qwq-partners/qwq-ai-trader/actions/runs/35033658238)도 성공했다: **1292 passed / 2 xfailed / 33 warnings**, 25.20초, 격리 0·비밀정보 검사 통과. 원격 경고는 이번에 변경하지 않은 `scripts/backtest_strategies.py:665,1614`의 NumPy timedelta deprecation이며 Toss 경고가 아니다. 로컬의 pykrx 경고 1건과 환경 차이를 숨기지 않는다. 이 CI는 아래 추가 수정 **전** 증거다.

### 전체 리뷰의 추가 수정

`2235586..14210d4` 전체 diff를 Astra/xhigh 독립 리뷰어가 읽고 Important 2건·Minor 1건을 재현했다.

| 항목 | 원인 및 수정 | 한정 재리뷰 |
|---|---|---|
| F1 | retry 예산 소진 시 revoked 기록까지 생략됨. revoked 복구를 재전송 허가보다 먼저 수행하되 expired-token 발급은 계속 재시도 허가 뒤에 둠 | 승인, `990d34e` |
| F2 | 실제 `RequestBudget.remaining()` 예외가 일봉 수집 종료 처리를 우회함. 최초/페이지 사이 만료에서도 확보한 봉과 `missing_dates`를 부분 결과로 보존 | 승인, `f190a3d` |
| F3 | page cap 테스트가 잘못된 cursor로 먼저 중단됨. 유효한 cursor·실제 예산의 cap1/cap2 음성·양성 대조군으로 교체 | 승인, `f190a3d` |

- 부모 `f190a3d` KST 전체: **1304 passed / 2 xfailed / 1 warning**, 30.69초, 격리 0·문법·비밀정보 검사 통과.
- 독립 한정 재리뷰: 15 passed / 95 deselected, 1.08초, 격리 0. F1의 재시도 경로·F2·F3은 닫혔다.
- 이 단계에서는 전체 승인을 보류했다. `recover()`의 deadline 초과·락 대기 취소 시 폐기 관측이 사라져 재시작·만료 후 가짜 issuer가 호출되는 별도 Important를 재현했기 때문이다. 아래 추가 보완으로 닫았으며 실토큰/운영에는 연결하지 않는다.

### 취소·재시작 안전 보완과 최종 승인

- `96e2bbb`: 첫 await/deadline 검사 전 동기 폐기 관측, immutable 256슬롯·정확한 관측ID별 해결 증거. 검증한 유효·다른·최신 캐시로만 해결하며 같은 bearer는 이후에도 거부한다. 토큰 담당 관련131건/전체1150건 통과.
- `18babb2`: client도 401 revoked를 해석한 직후 제한기 대기를 포함한 첫 await 전에 관측한다. 실제 모듈의 응답 직후 deadline 소진·제한기/토큰 락 취소→재시작·만료·bootstrap mint0를 검증했다. 관측 호출만 제거한 변이는 실패하고 복원 후 통과했다.
- 이 보완의 한정 리뷰에서 **Important 1건**(허용된 긴 identity가 관측·overflow 용량을 동시에 초과)과 **Minor 1건**(관측 검사 후 성공 캐시 반환의 deadline 재검사 누락)을 추가 재현했다. `358cde0`에서 JSON-escaped identity≤256바이트·공통 generation≤2^63−1·발급 전 세대 overflow 거부, 중앙 반환 deadline 검사로 수정했다. 관련167건/전체1186건 통과, 독립 재리뷰50건 통과·신규 지적0.
- 부모 `18babb2` 병렬 전체 검증은 KST1339건 통과, UTC1337건 통과/2건 실패였다. 두 실패는 초기50ms 테스트 예산이 토큰 검사 중 소진돼 **의도한 응답 이후 경계에 도달하지 못한 테스트 불안정성**이다. 미회수 task 예외도 관찰했다. 75ms 검사 지연을 주입해 2실패를 재현한 뒤 `2bf7842`에서 공유 시계·응답/실제 락 진입 이벤트·finally cancel/gather로 고쳤다. 같은 지연에서2건 통과, UTC/KST 실제 통합12건을 각각3회 반복해 총72실행 통과. 운영 deadline 정책을 완화하지 않았다.
- 최종 독립 리뷰: Astra/xhigh, `2235586..2bf7842` **Approved**, 미해결 Critical/Important/Minor 0건. 전체 diff 읽기와 후속 변경별 한정 재리뷰를 합친 판정이다. 마지막 테스트-only UTC 검증3건 통과/70 deselected·격리0. 이는 오프라인 코드 승인이지 실자료·운영 활성화 승인이 아니다.

### 최종 부모 검증 (`2bf7842`)

위 `verify.sh` 명령으로 한국·UTC 시간대를 병렬 실행했다.

- KST: **1375 passed / 2 xfailed / 1 warning**, 59.58초, exit0.
- UTC: **1375 passed / 2 xfailed / 1 warning**, 61.68초, exit0.
- 기준선 대비 테스트 **356건 증가**. 기존 xfail2·pykrx warning1 유지, 두 실행 모두 문법·비밀정보 검사 통과 및 운영 상태/외부 네트워크 접근 시도0.
- 최종 합성 CLI도 다시 실행해 시도6/유효2/제외4/p95=1/초과비율0.5/`insufficient`/`production_eligible=False`, 기존 manifest/dataset 해시 일치를 확인했다.
- 원격 Verify: [소스 `2bf7842` 실행](https://github.com/qwq-partners/qwq-ai-trader/actions/runs/35036649156) **SUCCESS**, **1375 passed / 2 xfailed / 33 warnings**, 29.75초, 격리0·비밀정보 검사 통과. 경고는 위와 같은 미변경 백테스터 NumPy timedelta deprecation이다. 초기 `14210d4`의 성공 결과를 최신 검증으로 재사용하지 않는다. 이 결과를 기록한 최종 문서 커밋은 소스/테스트를 바꾸지 않는다.

## 합성 CLI 재현 방법

구현 브랜치의 저장소 루트에서 기존 venv Python으로 실행한다. 입력은 커밋된 합성 fixture 두 개뿐이며 stdout으로 JSON을 출력한다. 실제 자료 수집용 명령이 아니다.

```bash
env -i PATH=/usr/bin:/bin LANG=C.UTF-8 TZ=Asia/Seoul PYTHONDONTWRITEBYTECODE=1 \
  /home/ubuntu/projects/qwq-ai-trader/venv/bin/python scripts/replay_toss_shadow.py \
  --manifest tests/fixtures/toss/phase1_manifest.json \
  --input tests/fixtures/toss/phase1_pairs.json
```

합성 입력의 직접 검산: 총 6행 중 유효 2행, 중복 1·공급자 실패 1·관측 시각 차이 1·노후 1이다. 유효 차이는 약 0.1%와 1%로 nearest-rank p95는 1%, 커버리지는 2/6, 예시 0.5% **초과** 비율은 1/2이다. 이 값은 실측 결과가 아니다. manifest 예시 임계값을 바꿔 통과시키거나 실자료 기준으로 재사용하지 않는다.

`manifest_sha256`/`dataset_sha256`는 문서화된 필드로 만든 정규화 계약의 해시다. 미정의 raw 필드를 바꿔도 의미 해시는 같을 수 있으며, 입력 파일 원본 바이트의 무결성 해시라고 주장하지 않는다. `spec_sha256`만 공개 명세 원본 바이트의 해시다.

## 운영 전제와 제한

1. `TokenManager`는 주입된 issuer 인터페이스만 제공한다. 실제 OAuth 발급기·키 로딩·발급 승인 UI/절차는 이번 범위가 아니다. 독립 프로세스/외부 호스트의 수동 토큰 발급을 로컬 파일락으로 막는다고 주장하지 않는다.
2. 토큰 issuer/reader와 조회 client의 sender/reader는 서로 다른 권한이다. 토큰을 읽을 수 있어도 조회 송신 권한을 자동 취득하지 않는다. 동일 자격의 송신자는 하나의 고정 락 경로와 공유 리미터를 사용해야 한다.
3. 시세 캐시와 기존 브로커 wrapper, 5분 shadow 스케줄러는 만들지 않는다. 따라서 이번 테스트는 실제 매매 호출부의 Toss 활성화 인수가 아니다.
4. `normalize_candle_pages`/`fetch_daily_candles`는 암묵적 현재 시각을 읽지 않는다. 명시적 `now`가 없는 이 경계는 수신 시각의 aware 여부를 검사하며, 미래 수신 시각 거부는 `parse_prices` 및 shadow의 명시적 `now` 경계가 담당한다. 수신 시각만으로 봉 완성·신선도를 인정하지 않는다.
5. 오프라인 manifest의 수치와 데이터는 합성 예시다. 조건을 모두 만족해도 `production_eligible=False`이며 실자료 사전 등록 임계값이나 매매 성능 증거로 쓰지 않는다.
6. 로컬 파일 쓰기/fsync는 동기 OS 호출이다. 락의 비동기 대기·발급기 await에는 deadline이 적용되지만 커널 내부 syscall 자체를 강제 종료한다고 주장하지 않는다. 디스크가 실패 상태 재게시까지 전부 거부하는 고장은 운영자 확인 대상이다.
7. 설치된 aiohttp `3.13.5`는 GET 연결 오류를 내부적으로 재전송할 수 있다. 이번 transport는 `_retry_connection=False`를 강제하고 그 제어 훅이 없거나 다시 켜졌으면 송신 전 거부한다. 비공개 속성 의존이므로 라이브러리 변경 시 재검증이 필요하다. 이번에는 의존성 버전을 변경하거나 설치하지 않았다.
8. 폐기 관측의 추가 보완은 저장 슬롯256개·관측당1KiB·해결증거64KiB 상한을 둔다. 포화/손상은 자동 삭제·재발급으로 복구하지 않는 fail-closed 설계다. 최종 발급 허가용 슬롯 검사 **시작 전** 지속 게시된 관측은 모두 검사한다. 여러 슬롯을 읽는 과정은 원자적 snapshot이 아니므로 검사 중/후 게시된 관측을 그 검사에서 놓칠 수 있으며 새 토큰 게시/다음 호출에서 재검사한다. 이미 허가된 외부 발급을 취소하거나 모든 프로세스의 관측·발급을 원자적으로 직렬화했다고 주장하지 않는다. 실제 운영 도입 전에는 저장소 점검/수동 복구 절차와 부하도 별도 검증해야 한다.
9. client의 token provider는 동기 `observe_revocation(...) -> None` 계약을 지켜야 한다. 누락/일반 `async def` 콜백은 사전 거부하지만 런타임 검사로 모든 async callable 객체·awaitable 반환 함수를 판별한다고 주장하지 않는다. 검증된 실제 `TokenManager`를 사용해야 하며 임의 provider의 지속 기록을 타입 검사만으로 증명할 수 없다.

## 이후 단계 (이번 작업에 포함하지 않음)

- 약관/자료 저장·표시 허용 범위, 자격 발급 소유권, 공급자 시장·수정주가 기준 확인.
- 실제 관측 manifest 승인: 호출 예산·시간 제한·한도·회로·유효 표본·커버리지·차이 임계값을 자료를 보기 전에 고정.
- 승인된 발급기/키 로딩·송신자 고정 경로 배선, KIS 비교 조회 예산과 별도 저우선순위 잡 구현.
- 승인 후 실자료 3영업일 관측. 자료 부족·차이 초과 시 승격 보류. 정상 가격 근접성도 청산/주문 사용 허가는 아니다.
- Phase 2 후보/점수 변경과 Phase 3 표시 전용 wrapper는 각각 별도 소비자 원장·회귀 테스트·승인이 필요하다.

## PR 통합과 다음 단계 사전점검 (2026-09-16)

### 통합 검증과 범위

- 사용자 요청은 PR 생성·main 반영·후속 진행이다. 실토큰 발급·실자료 호출·운영 배포/재시작·설정/주문 변경까지 승인된 것으로 해석하지 않는다.
- 기능 브랜치 `080e9bc`의 새 KST 전체 검증: **1375 passed / 2 xfailed / 1 기존 warning**, 44.08초, 격리 접근0·문법/비밀정보 검사 통과.
- 통합 도중 별도 PR #66이 main `dce9941`에 반영됐다. 설계 브랜치의 CHANGELOG 충돌은 양쪽 기록을 보존했고, 관측 문서를 변경하거나 그 실측을 새로 검증했다고 주장하지 않는다. 설계 브랜치 전체 검증: **1019 passed / 2 xfailed / 1 기존 warning**, 24.34초, 격리0·비밀정보 검사 통과. 구현 브랜치에도 같은 기록을 보존했다.
- PR #67은 오프라인 코드 통합이다. 상위 설계의 Phase 1 **3영업일 실자료 관측** 체크는 닫지 않는다. GitHub Verify는 검증만 하며 배포/재시작 작업을 포함하지 않는다.

### 코드로 확인한 후속 간극

보안·승인 경계는 Astra/high, 수집·예산·원장은 Terra/high가 병렬 읽기 전용 점검했고 부모가 핵심 코드를 대조했다. 새 구현 승인이나 실자료 관측 결과가 아닌 사전점검이다.

1. `token.py:TokenManager.bootstrap(approved=True)`는 호출자 bool 확인일 뿐 승인 주체·기간·host·manifest를 검증하지 않는다. 실제 OAuth POST 발급기·키 로더도 없으며 일반 조회 transport에 POST를 추가해 해결하면 안 된다. OFF/미승인/만료 시 키·토큰 저장소·HTTP에 접근하기 전 차단하는 별도 조립 경계가 필요하다.
2. `shadow.py:ShadowManifest`는 `offline`/`synthetic`만 허용한다. 기존 fixture에 live 플래그를 붙여 운영 승인으로 재사용하지 않는다. 관측 승인과 bootstrap 1회·정상 갱신·불명확 발급 상태 수동 복구의 권한을 구분해야 한다.
3. `kis_kr.py:get_quote()`는 관측시각을 반환하지 않는다. 조회 완료 시각을 체결/시장 관측시각으로 만들어 넣지 않는다. 근거 있는 시각 계약을 확보하기 전 `observed_at=None`/자료 부족으로 남기고 유효 비교 분모에서 제외하되 전체 시도 분모에는 포함해야 한다.
4. `kis_rate_limit.py:acquire()`의 한도 상태는 프로세스 내부이고 저우선·유한 대기 승인 인터페이스가 없다. 별도 CLI 프로세스로 시세를 수집하면 기존 엔진과 합산 예산이 자동 공유되지 않는다. 추가 예산·대기 상한·부하 시 skip을 먼저 검증해야 한다.
5. 현재 replay의 관측시각 기반 중복 검사는 live 작업의 재시작 idempotency가 아니다. 예약 슬롯·종목·선정 스냅샷 기준 시도 ID, 정확히 하나의 최종 결과, append-only 기록과 중단/부분 쓰기 복구가 필요하다. 실패·노후·결측·예산 skip을 분모에서 숨기면 안 된다.

### 접근 선택과 승인 대기 항목

- **권고 후보: 엔진 내부 독립 저우선 shadow task.** 단일 issuer/sender와 기존 KIS 한도를 공유할 수 있다. 다만 동작 배선·수명주기·예산 인터페이스가 새로 생기는 아키텍처 변경이므로 별도 설계 승인 후 구현한다. 청산/동기화/REST 가격 피드에 await를 끼우지 않고 마지막 성공 보유·후보 스냅샷만 읽는다. 새 스크리닝을 실행하지 않는다.
- **별도 수동 수집 CLI:** 격리는 단순하지만 현재 프로세스별 KIS 제한기·단일 sender 계약과 맞지 않는다. 공유 예산/소유권 계약 없이는 권고하지 않는다. 기존 합성 replay CLI는 그대로 안전한 오프라인 도구다.
- **중앙 송신 서비스:** 여러 프로세스가 꼭 필요할 때 가능한 별도 설계이나 현 단계에는 운영 범위가 크다.

후속 설계에서 결정할 대상은 약관·저장 허용 근거, 단일 issuer/sender 주체·허용 host/IP·전용 경로, 초기 토큰 인계 또는 bootstrap 허가, 관측 기간·종목/군·호출/시간/페이지/회로 예산·시각/신선도·최소 표본/커버리지·오차/장애/지연 기준과 원장 보존 정책이다. 합성 예시 숫자를 승인값으로 채우지 않는다. 공급자 시장/수정주가 동등성은 계속 `unknown`이며 수치 근접성으로 확정하지 않는다.

다음 구현 후보 순서는 승인 manifest/순수 관측 계약 → 발급기·키 로더의 가짜 HTTP 인수 → 원장/재시작 → KIS 공유 저우선 승인 → 독립 task 수명주기·금지 경로 불변 검증이다. **현재는 설계 선택·승인 대기**이며 이번 PR에 해당 구현이나 실행 명령을 추가하지 않았다.
