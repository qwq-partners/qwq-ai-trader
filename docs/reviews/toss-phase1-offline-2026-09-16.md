# 토스 Phase 1 오프라인 구현·검증 원장 (2026-09-16)

## 범위와 현재 상태

이 문서는 [검토된 설계](../superpowers/plans/2026-09-15-toss-securities-fallback.md)의 **Phase 1 오프라인 부분**을 다룬다. 인증 실자료 shadow, 운영 스케줄러, 소비자 폴백 도입까지 완료했다는 의미가 아니다.

- 시작 기준: main `8849d92`, 설계 PR #65 head `2235586`. 설계 PR을 임의로 병합하지 않고 별도 `feature/toss-phase1-offline-20260916`에서 구현했다.
- 계획: [구현 계획](../superpowers/plans/2026-09-16-toss-phase1-offline.md). 네 작업의 구현·작업별 독립 재리뷰는 완료했다. 통합 소스 `298bc4c`에 대해 부모 UTC/KST 검증과 전체 브랜치 최종 리뷰를 진행한다.
- 기본 OFF, 운영 호출부에 import/배선 없음. 주문·체결·잔고·청산·사이징·후보/점수·설정·의존성 파일은 변경 범위 밖이다.
- 실제 자격증명/토큰/운영 캐시를 읽지 않았고, 인증 API·SSH·systemctl·배포·재시작·주문을 실행하지 않았다. 공개 OpenAPI JSON을 인증 없이 조회한 것과 합성 transport/session 검증을 구분한다.

## 고정한 공개 계약

[OpenAPI JSON](https://openapi.tossinvest.com/openapi-docs/latest/openapi.json)을 2026-09-16 확인했다.

- 버전 `1.2.17`, 원본 바이트 SHA-256 `791082da4cb379117ed9fdc29a45bd42746f7a1aec368da1e9f4e1f3bfbff5b4`.
- 저장소의 `tests/fixtures/toss/spec_contract.json`은 원본 전체가 아니라 사용 계약의 메타데이터 요약이다. 요약 파일 자체의 해시를 원본 명세 해시와 혼동하지 않는다.
- 조회 allowlist는 `GET /api/v1/prices`, `GET /api/v1/candles`, `GET /api/v1/market-calendar/KR`뿐이다. 주문·계좌·호가·수급·랭킹·임의 URL은 포함하지 않는다.
- 일봉 `before`는 inclusive이며 `nextBefore`를 그대로 전달한다. 일봉 자정 시각은 봉 날짜이지 실시간 관측 신선도 근거가 아니다. 공급자 시장/수정주가 기준의 실자료 확인은 여전히 미완이다.

## 구현·검증 원장

| 작업 | 담당 역할 | 검증/리뷰 상태 |
|---|---|---|
| 보안 토큰 저장·issuer/reader 상태 | Astra/high 구현, Astra/xhigh 리뷰 | Important2 전부 수정·재리뷰 승인, 전용102건 |
| 조회 경계·송신자 락·공통 예산 | Astra/high 구현, Astra/xhigh 리뷰 | Important2 전부 수정·재리뷰 승인, 전용89건 |
| 현재가/일봉 정규화·전체 구간 확보 | Terra/high 구현, Astra/high 리뷰 | Important5/Minor2 전부 수정·재리뷰 승인, 전용41건 |
| 합성 비교 manifest·CLI·통합 | Terra/high 구현, Astra/high 리뷰 | Important3/Minor3 전부 수정·재리뷰 승인, 전용41건·실제 모듈 통합 |

기준선 전체 검증: `1019 passed / 2 xfailed`, 격리 위반 0. 기존 `pykrx` 경고 1건. 통합본은 테스트 **273건 증가**다.

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
- 전체 브랜치 최종 독립 리뷰·원격 CI는 아직 진행 전/중이며 작업별 승인을 최종 승인으로 대체하지 않는다.

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

## 이후 단계 (이번 작업에 포함하지 않음)

- 약관/자료 저장·표시 허용 범위, 자격 발급 소유권, 공급자 시장·수정주가 기준 확인.
- 실제 관측 manifest 승인: 호출 예산·시간 제한·한도·회로·유효 표본·커버리지·차이 임계값을 자료를 보기 전에 고정.
- 승인된 발급기/키 로딩·송신자 고정 경로 배선, KIS 비교 조회 예산과 별도 저우선순위 잡 구현.
- 승인 후 실자료 3영업일 관측. 자료 부족·차이 초과 시 승격 보류. 정상 가격 근접성도 청산/주문 사용 허가는 아니다.
- Phase 2 후보/점수 변경과 Phase 3 표시 전용 wrapper는 각각 별도 소비자 원장·회귀 테스트·승인이 필요하다.
