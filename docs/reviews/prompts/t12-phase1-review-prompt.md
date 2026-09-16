# T12 Phase 1 독립 리뷰 요청 — 토스증권 Open API 2차 시세 소스 인프라 (shadow 전용)

> 이 프롬프트는 구현자와 **다른** 에이전트(Codex / 별도 Claude 세션 / 사람)가 그대로 붙여 넣어 쓰도록 작성됐다.
> 리뷰 대상 브랜치 `feature/t12-phase1-toss-infra`, 기준 `main` `dce9941`, 리뷰 대상 HEAD `52baa47`.

## 0. 당신의 역할과 제약

- 당신은 이 변경을 **머지 전 마지막으로 보는 독립 리뷰어**다. 구현자의 설명을 믿지 말고 코드로 확인하라.
- **읽기 전용.** 파일 수정·커밋·push·배포·systemctl 금지.
- **토스 API 를 실제로 호출하지 말 것.** 토스 OAuth 토큰은 클라이언트당 1개만 유효해서, 리뷰 중 토큰을 발급하면 운영 봇의 토큰이 즉시 무효화된다. 운영 `.env`·`~/.cache/ai_trader` 도 읽지 말 것.
- 전체 pytest 는 돌리지 말고(격리 가드가 있는 개별 파일만): `/home/ubuntu/projects/qwq-ai-trader/venv/bin/python -m pytest tests/test_toss_client_phase1.py tests/test_toss_parity_phase1.py -q`
- 저장소를 읽을 수 없는 환경이면 **"미검증"** 이라고 명시하라. 추측으로 승인하지 말 것.

## 1. 배경 (왜 이 변경이 있는가)

KIS(한국투자증권) OpenAPI 가 초당 한도(EGW00201/EGW00215)와 HTTP 500 으로 시세·스크리닝 경로를 반복적으로 비웠고, 백업이던 네이버 크롤링·pykrx 는 이미 사망한 상태였다. 토스증권 Open API 를 **읽기 전용 2차 시세·참조 데이터 소스**로 단계적으로 붙인다.

- 설계서(확정본, 적대적 검토 blocking 11건 반영): `docs/superpowers/plans/2026-09-15-toss-securities-fallback.md`
- 토스 정본 스펙: `https://openapi.tossinvest.com/openapi-docs/latest/openapi.json`(OpenAPI 3.1 1.2.17), 개요 `https://openapi.tossinvest.com/openapi-docs/overview.md`
- **주문·체결·잔고·계좌는 KIS 단독 유지**가 전제다. 토스 주문 API 는 제공되지만 쓰지 않는다.

## 1.1 변경 파일 (이 PR)

| 파일 | 성격 |
|---|---|
| `src/data/providers/toss/__init__.py`, `token.py`, `rate_limit.py`, `client.py`, `market_data.py` | 신규 패키지 |
| `src/analytics/toss_parity.py` | 신규 — 대조 기록·요약 |
| `src/schedulers/kr_scheduler.py` | `create_tasks` 게이트 + `run_toss_parity_scheduler` 태스크 추가 (기존 루프 무수정) |
| `src/utils/loop_heartbeat.py` | `PERIODS["kr_toss_parity"] = 300` 1줄 |
| `tests/test_toss_client_phase1.py`(27), `tests/test_toss_parity_phase1.py`(18) | 전부 스텁 |
| `docs/superpowers/plans/2026-09-15-toss-securities-fallback.md` 외 문서 | 설계 검토 반영 + Phase 1 기록 |

돈 경로 파일(`src/core/engine.py`, `src/core/batch_analyzer.py`, `src/execution/`, `src/risk/`)은 **변경이 없어야 한다.** `git diff main...HEAD --stat` 로 먼저 확인하라. 있으면 그 자체가 blocking 후보다.

## 1.2 구현자가 스스로 밝힌 절충 (검토 시 유효한지 판단하라)

1. 캔들 대조 `record_candle_parity` 는 모듈에 있지만 **스케줄러에 배선하지 않았다** — KIS 일봉을 얻을 캐시 접근점이 없어 새 KIS 호출이 불가피했고 "KIS 추가 호출 금지"를 우선했다.
2. 보유 종목의 KIS 가격은 포트폴리오 캐시(`positions[symbol].current_price`)를 재사용하고 `kis_as_of=None` 으로 기록한다(시각을 지어내지 않음). 스크리닝 후보는 KIS 캐시가 없어 `kis_price=None` 토스 단독 관측이다. `summarize()` 는 `diff_bp=None` 행을 제외한다.
3. `TossClient.get` 은 `result` 를 벗기지 않고 envelope 전체를 반환한다(`/prices` 의 result 가 list 라서).
4. 캔들 행에 KIS 계약에 없는 `timestamp` 키를 **추가**로 싣는다(분봉 구분·dedup 용). 일봉 소비자는 무시한다.
5. 검토 advisory 중 3건 미반영: 캔들 대조 배선, 호가 키 KIS 명명(`size`), 페이징 비용 측정.

## 2. Phase 1 의 범위 (이 PR 이 해야 하는 것과 하면 안 되는 것)

**해야 하는 것**
- `src/data/providers/toss/` 신규 패키지: 토큰 단일 캐시(`token.py`), 그룹별 TPS 게이트(`rate_limit.py`), HTTP 클라이언트+서킷 브레이커(`client.py`), 엔드포인트 래퍼+KIS 계약 정규화(`market_data.py`)
- `src/analytics/toss_parity.py`: KIS↔토스 **대조 기록** 러너(가격 차이 bp·세션·캔들 정렬 일치·캘린더 대조) + `summarize()`
- `src/schedulers/kr_scheduler.py`: 5분 주기 대조 태스크 1개 추가(장중만, `TOSS_API=0` 이면 미생성)
- 테스트 2파일(전부 네트워크 스텁)

**하면 안 되는 것 — 발견하면 blocking**
- 기존 소비자(청산·사이징·주문·스크리닝·대시보드)가 토스 값을 **한 건이라도** 받는 것
- `token-revoked` 수신 시 **재발급**하는 코드 경로 (설계 §4.2: 캐시 재읽기 → 최신 토큰 재시도 → 같을 때만 락 안 double-check 후 1회 발급)
- 캐시 파일 삭제·무효화
- 캔들을 토스 원본 순서(최신순)로 소비자에게 넘기는 것 (KIS 계약은 오래된 순, `date="YYYYMMDD"`)
- `timestamp=null` 을 현재 시각으로 채우는 것 (as_of 는 None 으로 보존)
- 거래대금 `value` 를 0 이나 `close×volume` 으로 지어내는 것 (None 이어야 함)
- 토스 리미터가 `src/utils/kis_rate_limit` 의 예산을 소모하는 것
- 새 태스크가 KIS 원장/시세 호출을 **추가로** 발생시키는 것 (EGW00215 재발)
- 테스트가 `~/.cache/ai_trader` 나 실제 네트워크를 건드리는 것 (`tests/conftest.py` 격리 가드)
- 토큰·시크릿이 로그·예외 메시지·원장에 남는 것

## 3. 검토 항목 (각 항목에 대해 파일·라인·근거를 남겨라)

### 3.1 토큰 (`token.py`) — 가장 먼저, 가장 깊게
1. `401 token-revoked` 경로를 코드로 추적하라. 재발급으로 가는 분기가 하나라도 있으면 blocking.
2. `expired-token` / `invalid-token` 과 `token-revoked` 를 **응답 본문 에러 코드**로 구분하는가. HTTP 401 만으로 분기하면 blocking.
3. 락 안 double-check: 두 코루틴/프로세스가 동시에 만료를 감지했을 때 발급이 **정확히 1회**인지 코드 경로로 증명하라. 테스트가 이를 실제로 잡는지(스텁을 되돌리면 실패하는지) 확인.
4. 캐시·락 경로가 생성자 인자로 주입되는가. 모듈 상수를 직접 쓰는 곳이 있으면 지적.
5. `expires_at`/`issued_at` 파싱에서 `float(None)` 같은 타입 오류 가능성.

### 3.2 리미터 (`rate_limit.py`)
1. 그룹별 초당 한도가 설계 §1.1 표와 일치하는가.
2. 동시 호출에서 버킷이 한도를 초과할 수 있는 경합이 있는가.
3. 429 시 `Retry-After` 우선, 재시도 상한 2회인가.
4. `X-RateLimit-Limit` 헤더로 상한을 **낮추기만** 하는가(올리면 지적).

### 3.3 클라이언트 (`client.py`)
1. 서킷 브레이커 개폐 조건과 복구가 설계 §6.6 대로인가. 열린 동안 즉시 실패를 반환하는가.
2. `aiohttp.ClientTimeout(total=...)` 을 쓰는가(숫자 리터럴 금지).
3. 에러 envelope(`{"error":{"code":...}}`) 파싱이 비-JSON 응답에서 예외를 내지 않는가.
4. `TOSS_API=0` 에서 팩토리가 None 을 반환하고 아무 것도 초기화하지 않는가.

### 3.4 계약 정규화 (`market_data.py`)
1. 캔들: 오래된 순 재정렬 / `date` KST `YYYYMMDD` / OHLCV decimal 문자열→float / `value=None` / `before` 페이징 종료 조건(무한 루프 가능성).
2. 현재가: 200개 분할 경계, `timestamp` null → `as_of=None`.
3. 지수 현재가: 실측상 `timestamp` 가 null 로 온다 — None 보존 확인.
4. 반환 dict 에 `None` 이 들어가는 키가 **기존 KIS 계약 키**(price/open/high/low/volume/change_pct)인지 확인 — 설계 §4.4 는 기존 계약 키에 None 금지(신규 키 `as_of`·`value` 만 허용).

### 3.5 대조 러너 (`toss_parity.py`) + 스케줄러 배선
1. 러너가 반환값을 어디에도 소비시키지 않는가(기록·요약만).
2. 세션 태깅이 `src/utils/session.py` KRSession 을 쓰는가(별도 하드코딩이면 지적).
3. 캔들 대조가 **정렬 방향**을 실제로 검사하는가(가격만 비교하면 정렬 역전이 안 드러난다).
4. 캘린더 대조가 세션 판정을 **바꾸지 않는가**(경고만).
5. 새 태스크: 예외가 다른 루프로 전파되는가, 동기 I/O 로 이벤트 루프를 막는가, KIS 호출을 추가하는가, 장외에 유휴인가.
6. 원장 append 가 원자적인가, 경로 주입이 가능한가.

### 3.6 테스트 실효성
- 각 테스트에 대해 "수정을 되돌리면 실패하는가"를 판단하라. 통과만 하는 테스트는 advisory 로 지적.
- 특히 캔들 정렬 역전 테스트가 **역순 입력**을 실제로 넣는지 확인.

## 4. 출력 형식

```
## 결론: 머지 가능 / 조건부 / 불가

## Blocking (있으면)
- [파일:라인] 문제 — 근거(코드 인용) — 최소 수정안

## Advisory
- [파일:라인] 문제 — 근거 — 제안

## 확인한 것 (문제 없음)
- 항목별로 어떻게 확인했는지 한 줄

## 미검증
- 환경·시간 제약으로 못 본 것
```

- 스타일·네이밍 지적은 하지 말 것.
- "~일 수 있다"는 추측은 근거 없으면 쓰지 말 것. 코드 경로로 확인된 것만.
- 한국어로 답하라.
