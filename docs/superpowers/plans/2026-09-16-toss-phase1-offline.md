# Toss Phase 1 Offline Infrastructure Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 토큰/조회/시세 계약을 격리된 오프라인 테스트로 구현하고, 실자료·운영 활성화 없이 비교 원장을 재현한다.

**Architecture:** 신규 `src/data/providers/toss/` 패키지와 파일 입력 전용 CLI를 만든다. 토큰·HTTP·정규화는 주입된 의존성으로 독립 검증하며 기존 브로커/스케줄러/설정에는 연결하지 않는다. Phase 1의 **오프라인 구현 부분**만 완료 대상으로 삼고, 인증 실자료 관측·5분 운영 잡·공급자 기준 확인·3영업일 승격은 열린 항목으로 남긴다.

**Tech Stack:** Python 3.12, asyncio, dataclasses, Decimal, 기존 aiohttp, pytest. 신규 패키지 설치 없음.

**Spec:** `docs/superpowers/plans/2026-09-15-toss-securities-fallback.md` (`2235586`, 특히 §4/§5/§6/§10), `docs/reviews/toss-design-codex-2026-09-15.md`.

## Global Constraints

- 모든 기본값은 OFF. `.env`, 운영 토큰/캐시/로그/잔고를 읽거나 변경하지 않는다. 인증 API 호출·토큰 발급·SSH·systemctl·배포·주문·설정 변경 금지.
- 구현 코드는 실서버 요청 능력이 있어도 **이번 검증은 가짜 transport/session**만 사용한다. 네트워크 구현을 실행하지 않는다. 공개 스펙 GET만 부모가 별도 수행한다.
- 기존 `src/execution/`, `src/core/`, `src/schedulers/`, 설정/의존성 파일 무변경. quote_router/운영 잡/레짐/후보/주문 연결을 만들지 않는다.
- 모든 캐시/락/입출력 경로는 명시 주입, HOME 재지정 금지. 토큰/인증 raw body·예외를 로그/repr/원장에 노출하지 않는다.
- 프로젝트 TDD: 테스트→RED 관찰→최소 구현→GREEN. 외부 호출만 fake로 대체하고 상태/파일/분기 구현은 실제로 검사한다. 예상값은 직접 검산한 literal을 사용한다.
- 파일 수정은 apply_patch, 작업별 격리 feature worktree, 다른 담당 파일 수정 금지. 에이전트는 하위 에이전트를 생성하지 않는다. 부모가 통합 문서·리뷰·푸시를 담당한다.
- 기준 main `8849d92`, 설계 PR #65는 미병합. 구현은 `2235586` 위 별도 브랜치로 쌓고 main/운영 checkout은 유지한다.

## Scope / Acceptance Map

| 설계 인수 | 이번 구현 | 이후 단계 |
|---|---|---|
| A01~A07 | 임시 파일/가짜 issuer·transport로 인증/권한/한도/deadline 검증 | 실자격 소유권/약관 승인 후 연결 |
| A08~A10 | 현재가·일봉 순수 파싱, 페이지·완성 구간 계약 | 공급자 거래소/adjusted 계약 확인 |
| A11 | 패키지가 운영 캐시를 쓰지 않음, 토큰 전용 namespace와 offline 출력 격리 | 시세 캐시 구현 자체는 소비자 도입 때 |
| A12 | 기본 OFF에서 factory/토큰/transport 접근0; 기존 소비자 미배선 | live 호출부별 이벤트/Order 인수는 Phase2/3 |
| A13 | 구현 안 함(수급/종목 마스터 API 자체 allowlist 밖) | Phase2 reference/supply 별도 구현 |
| A14 | offline manifest·표본/제외/실패/중복 분모·synthetic_only 보고 | 실자료 manifest 사전 등록/관측/승격 |

## Verification Commands

작업별 테스트는 다음과 같이 기존 venv를 사용한다(경로는 작업 WT에서 실행):

```bash
env -i PATH=/usr/bin:/bin LANG=C.UTF-8 TZ=Asia/Seoul PYTHONDONTWRITEBYTECODE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 /home/ubuntu/projects/qwq-ai-trader/venv/bin/python -m pytest -p no:cacheprovider --tb=short -q tests/test_toss_token_contract.py
```

전체 검증:

```bash
env -i PATH=/usr/bin:/bin LANG=C.UTF-8 TZ=Asia/Seoul PYTHONDONTWRITEBYTECODE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PYTEST_ADDOPTS='-p no:cacheprovider --tb=short -rx' QWQ_DEPLOY_SSH_KEY=/tmp/qwq-offline-no-ssh-key QWQ_VERIFY_PYTHON=/home/ubuntu/projects/qwq-ai-trader/venv/bin/python bash scripts/dev/verify.sh
```

## Task 1: Secure token store and issuer/reader state machine

**Files:** Create `src/data/providers/toss/token.py`, `src/data/providers/toss/token_store.py`; tests `tests/test_toss_token_contract.py`, `tests/test_toss_token_storage.py`.

**Ownership/model:** Astra/high — credential integrity/concurrency. 독립 worktree. 다른 담당 파일/공통 init/문서는 편집하지 않는다.

**Interfaces:** own `TokenError(code: str)` (정해진 안전한 사유만, raw exception/token repr 없음), `TokenRecord`(access_token은 repr 제외, issued_at/expires_at UTC aware, issuer_pid/generation/client_identity/origin/schema_version), `SecureTokenStore(directory: Path, client_identity: str)`. `issuer_pid`는 bool이 아닌 양의 정수 감사 필드이며 발급 권한 근거가 아니다.

```python
# TokenManager의 공개 인터페이스. 시간은 주입 가능; deadline은 monotonic 절대시각.
TokenManager(store, *, role="reader", issuer=None, enabled=False,
             clock=time.monotonic, now=utc_now)
await manager.get_token(deadline=deadline)                 # str
await manager.recover("token-revoked", failed_token, deadline=deadline)  # str
await manager.bootstrap(approved=True, deadline=deadline) # str, 명시 승인 issuer만
# issuer는 async callable() -> {access_token, expires_in, token_type}; 실제 HTTP는 담당하지 않음.
```

- [x] Step 1: importlib로 모듈 미구현을 assertion 실패로 고정한 뒤 핵심 행동 테스트를 먼저 작성/RED 관찰한다. 같은 revoked 토큰/없음/손상에서 issuer 호출0, 다른 정상 캐시만 반환, reader는 어떤 상태에서도 mint0, enabled=False이면 디렉터리/issuer 접근0.

```python
# fake issuer/임시 store를 연결한 실제 TokenManager에 대해 외부에서 단언한다.
with pytest.raises(TokenError, match="auth_unavailable"):
    asyncio.run(manager.recover("token-revoked", old_token, deadline=deadline))
assert issued == []
assert old_token not in repr(manager)
```

- [x] Step 2: 전용 디렉터리0700, 소유권/symlink 거부, 임시/최종/락0600, 고유 임시 파일→fsync→replace→directory fsync로 안전 게시한다. 기존 상위 공유 디렉터리 chmod 금지. JSON schema/identity/origin/시간/유한 유효기간 검증. 잘못된 파일은 token을 반환하지 않는다. 파일 크기 상한도 둔다.
- [x] Step 3: 별도 고정 lock inode의 LOCK_NB+async 제한 대기로 발급을 직렬화한다. 락 안에서 캐시 재확인, 정상 갱신1회, reader/invalid-token/403 발급0. revoked 동일/없음/손상은 auth_unavailable 지속 상태, 재시작/일반 만료 우회0. 다른 유효 캐시의 최신 세대를 확인한 경우만 정상 복구한다.
- [x] Step 4: 발급 직전 안전하게 intent를 게시한다. 발급 POST의 timeout/취소/응답손상/저장실패는 issuance_unknown 지속, 자동 재발급0. bootstrap만 초기 캐시 부재를 명시 승인으로 발급한다. `expires_in`은 finite 양수·bool거부, 만료30분 전 갱신은 짧은 수명에서 매 조회 재발급이 되지 않도록 최소 갱신 간격 적용. 상태/오류가 정상 캐시의 bearer를 지우거나 출력하지 않게 한다.
- [x] Step 5: umask022·교체·symlink/잘못된소유자·replace/fsync실패·잠금 점유·취소·재시작·두 프로세스 경합을 추가 RED/GREEN으로 검증한다. 테스트의 subprocess도 임시 경로/가짜 발급만 사용하며 명시 timeout/join으로 종료한다. 미지의 발급 결과를 해제하는 임의 자동 복구 API는 만들지 않는다.
- [x] Step 6: 관련 테스트와 전체 offline verify, diff/비밀 검사 후 own files만 commit. full report에 명령·RED/GREEN 결과·보안 한계를 기록한다. 원격 push는 부모 담당.

## Task 2: Read-only HTTP boundary, shared request budget and limiter

**Files:** Create `src/data/providers/toss/client.py`, `src/data/providers/toss/rate_limit.py`, `src/data/providers/toss/transport.py`; tests `tests/test_toss_client_boundary.py`, `tests/test_toss_rate_deadline.py`.

**Ownership/model:** Astra/high — endpoint permission/retry/cancellation. 독립 worktree. Task1에 의존하지 않는 token protocol로 먼저 구현한다.

**Interfaces:** `TossRequestError(code: str)` 안전 사유만, `HttpResponse(status: int, headers: Mapping, body: object)` body repr 제외. `RequestBudget(timeout_seconds, *, clock=time.monotonic, max_retries=1, max_pages=4)`는 생성 시 절대 `deadline` 고정, 유한 양수/정수 검증, `remaining()`/`consume_retry()`/`consume_page()` 제공. 한 논리 조회에 같은 객체를 모든 페이지/401/429/5xx가 공유한다.

```python
TossClient(*, transport, tokens, limiter, enabled=False, role="reader",
           sender_lock_path: Path, circuit_failure_threshold: int,
           circuit_open_seconds: float, clock=time.monotonic)
async with client:  # enabled=False이면 토큰/lock/transport 전혀 접근하지 않음
    body = await client.get("/api/v1/prices", params={"symbols": "005930"}, budget=budget)
# token protocol: get_token(deadline=...), recover(error_code, failed_token, deadline=...)
# 송신을 명시 승인한 사용자는 client role="sender". TokenManager issuer/reader와 별개 권한.
# get은 원본 result envelope 반환, logical get당 consume_page 1회. 재시도에는 추가 page 소비 없음.
# transport: async request(method, path, *, params, headers, timeout) -> HttpResponse
# transport: async close(); 공식 origin 이외 임의 URL 전달 경로 없음.
```

- [x] Step 1: OFF/reader/미승인 endpoint/POST 요청에서 token·transport 송신0인 테스트 먼저 RED. `get` 외 일반 request가 필요하면 method 검증도 transport 전 수행한다. `orders/accounts/cancel/conditional-orders`·절대URL·query/fragment·percent/dot/중복슬래시 변형·임의 account header는 거부한다.

```python
with pytest.raises(TossRequestError):
    asyncio.run(client.get("/api/v1/accounts", params={}, budget=budget))
assert transport.requests == []
assert tokens.calls == []
```

- [x] Step 2: allowlist는 Phase1의 **GET /api/v1/prices, GET /api/v1/candles, GET /api/v1/market-calendar/KR** 3개뿐. params key/type/길이/종목1~200·일봉count1~200·interval·adjusted·cursor를 검증한다. 세션/transport body는 HTTP200도 JSON mapping+result 계약을 검사하고 오류/미지원/인증/손상을 안전 사유로 구분한다. Token POST는 이 client에 **없다**(Task1의 injected issuer 경계만).
- [x] Step 3: `AiohttpTransport`는 고정 `https://openapi.tossinvest.com`, TLS 확인, `allow_redirects=False`, `aiohttp.ClientTimeout(total=remaining)`를 강제한다. 세션은 lazy creation, close/cancel 안전, 응답/예외·인증 헤더 로그 없음. 테스트는 injected fake session으로 인자/응답 처리를 검사하며 socket 호출 금지. raw request/body를 담은 예외 체인도 외부 표시하지 않는다.
- [x] Step 4: `GroupRateLimiter`는 같은 client identity에서 공유하도록 한 객체를 주입한다. 정상 버킷 limits는 callers가 명시 전달하며 `MARKET_DATA`, `MARKET_DATA_CHART`, `MARKET_INFO`만 허용. start에서 소유권/0600/symlink 검증된 **lifetime sender lock**을 비블로킹으로 잡아 동일 경로의 두 client/프로세스 송신을 금지한다. reader는 client송신 불가. 재시작 복구는 파일 삭제가 아니라 fd flock 해제다. holder의 합산 호출/429hold/상한하향은 모든 엔드포인트가 공유한다.
- [x] Step 5: Reset은 1토큰까지 초, Retry-After 우선, 없음/손상은 상향하지 않는다. 큰 wait는 deadline 내 남지 않으면 즉시 timeout(조기 재시도 금지). 401 recover/429/5xx/네트워크 오류가 **총 retry1** 공유. 토큰 획득·lock·gate·HTTP 전체 asyncio timeout에 걸고 CancellationError는 전파한다. 구조 손상/403/영구4xx retry0. circuit failure threshold/open seconds/half-open probe1은 생성자 필수 정책 값으로 받고 endpoint 그룹에 적용; last_success/실패는 raw자료 없이 상태로 노출.
- [x] Step 6: synthetic clock·긴429·한도하향·동시호출·두 sender lock·취소 후 close·401→429·세션 예외 비밀 미노출 RED/GREEN. focused/full offline verify 후 own files commit 및 report. bootstrap/authPOST는 Task1 계약으로 테스트하며 범용 transport를 토큰 client로 확장하지 않는다.

## Task 3: Price/candle normalization and complete-window paging

**Files:** Create `src/data/providers/toss/market_data.py`, `src/data/providers/toss/market_types.py`; tests `tests/test_toss_market_contract.py`, `tests/test_toss_candle_paging.py`.

**Ownership/model:** Terra/high — 순수 값/시점/페이지 정규화. HTTP와 토큰 구현은 수정하지 않는다.

**Interfaces:** 자신의 frozen dataclasses `Quote`, `Candle`, `CandleSeries`와 안전한 `MarketDataError`. Quote는 symbol/price Decimal|None/observed_at datetime|None/fetched_at/status/missing_fields/market_basis/currency. Candle은 bar_date/OHLC Decimal/volume int/complete/adjusted/market_basis. CandleSeries는 오름차순 bars/complete/missing_dates/status. status는 `ok/partial/missing/invalid/stale`에서 선택한다. Quote의 nullable 시각은 `DataPoint`/`is_fresh`로 검사할 수 있게 변환하되 기존 utility 수정 금지.

```python
parse_prices(body, *, symbols, fetched_at, now, max_age_seconds) -> dict[str, Quote]
normalize_candle_pages(pages, *, symbol, expected_dates, fetched_at,
                       market_basis="unknown", adjusted=True) -> CandleSeries
await fetch_daily_candles(client, *, symbol, expected_dates, fetched_at,
                         budget, market_basis="unknown", adjusted=True) -> CandleSeries
compose_quote(quote, series, *, trading_date, previous_trading_date,
              required_fields) -> dict  # 성공 숫자 payload 또는 정확히 {}
```

- [x] Step 1: symbol leading0/영숫자·ISO8601 offset·null timestamp·거래량0·NaN/Infinity/bool·통화/종목 mismatch를 테스트 먼저 RED. `/prices` result는 목록, candle result는 `{candles: [...], nextBefore: ...}`다. 필드명은 lastPrice/openPrice/highPrice/lowPrice/closePrice/volume/currency/timestamp.
- [x] Step 2: decimal문자열을 Decimal로 검증(가격>0, volume 정수≥0, finite, 범위/과대 exponent 방어). timestamp naive/미래/노후는 정상 fresh로 포장하지 않는다. `fetched_at`도 aware이고 now 이후이면 거부. 요청 누락 종목은 미획득 레코드, 중복/손상 row는 성공으로 덮지 않는다.

  시각 계약 보완: 위 미래 수신 거부는 명시적 `now`를 받는 `parse_prices`에 적용한다. `normalize_candle_pages`/`fetch_daily_candles`는 aware 수신 시각을 요구하되 숨은 현재 시각을 조회하지 않는다. 호출자/shadow가 수신 시각을 검증해야 하며 수신 시각만으로 봉 완성·신선도를 추정하지 않는다.
- [x] Step 3: expected_dates는 호출자가 명시한 **요구 확정 거래일 목록**이며 날짜 계산/휴일 추측을 하지 않는다. 오늘·미래 날짜가 포함되면 확정 구간으로 성공시키지 않는다. 봉 ISO→KST date, 오름차순 정렬, OHLC 일관성, 중복 동일봉 dedup/충돌봉 거부, 당일 부분봉은 complete=False. 부분봉이 시간이 흘렀다고 확정으로 바뀌지 않게 수집시점/완성 근거를 보존한다.
- [x] Step 4: page count200·nextBefore 그대로 params 전달, 반복/미진행/페이지 cap/deadline/둘째 페이지 오류 처리. expected_dates 전체가 확보돼야 complete=True; 부족하면 missing_dates/partial로 보존하되 legacy 성공은 금지. `max_pages`/retry는 client budget 공유, collector가 자체 retry하지 않는다. expected_dates 밖 봉으로 길이를 채워 성공하지 않는다.
- [x] Step 5: compose는 가격/오늘/직전 거래일/시장·adjusted 일치가 확인된 필드만 생성한다. required_fields 하나라도 미획득이면 `{}`; metadata-only dict 금지. candle value는 미지원이며 close×volume/0으로 위조하지 않는다. 시장 기준unknown에서 prev_close/change/오늘OHLCV 합성 불가. price-only 요구도 quote 신선도 충족 필요. 미래/장전 봉을 오늘 값으로 사용하지 않는다.

```python
# 105 / 직전 거래일100 → 5%, 금요일90으로 계산한16.67%가 아니다.
assert result["change_pct"] == 5.0
assert compose_quote(q, series_without_today, trading_date=tuesday,
                     previous_trading_date=monday,
                     required_fields={"price", "open", "volume"}) == {}
```

- [x] Step 6: 201~250번째만 고점200인 fixture, 중복 페이지·분할 기준 mismatch·주말 경계·미래/부분봉·false freshness를 focused/full offline verify로 검증한 뒤 own files commit/report. 가격 캐시/수급·reference/랭킹/실거래 소비자는 구현하지 않는다.

## Task 4: Offline shadow replay, manifest and integration handoff

**Files:** Create `src/data/providers/toss/__init__.py`, `src/data/providers/toss/shadow.py`, `scripts/replay_toss_shadow.py`, `tests/test_toss_shadow_manifest.py`, `tests/test_toss_offline_boundary.py`, `tests/fixtures/toss/phase1_pairs.json`, `tests/fixtures/toss/phase1_manifest.json`. Parent created `tests/fixtures/toss/spec_contract.json` from the unauthenticated public spec; Task4 consumes it without changing source/hash. Parent updates CHANGELOG/CLAUDE/docsREADME/external-apis/design/report only after test/review evidence.

**Ownership/model:** Terra/high 구현; 부모가 인터페이스 확정 후 위임. 토큰/HTTP/시장 구현 파일은 수정하지 않는다.

**Interfaces:** `ShadowManifest.from_dict(data)` strict validation, `summarize_pairs(rows, manifest)` pure JSON-compatible report, `build_shadow(*, enabled=False, factory)` disabled→None without factory call. CLI arguments `--manifest PATH --input PATH` only, JSON stdout, network/live/credential options 없음.

- [x] Step 1: 기본 OFF factory0, malformed/nonfinite/negative manifest 및 timezone-naive pair 시각 거부, CLI --help가 토큰/env읽기0·라이브옵션거부 테스트 먼저 RED. manifest에는 별도 시각 필드를 요구하지 않는다.
- [x] Step 2: manifest schema_version1, mode=`offline`, dataset_kind=`synthetic`, spec_version=`1.2.17`, spec_sha256(부모 공개 조회 해시), max_age_seconds/max_pair_skew_seconds/min_valid_pairs/min_coverage/p95_limit_pct/outlier_threshold_pct/max_outlier_fraction와 고정 p95 방식 `nearest_rank`를 명시 필수로 둔다. 숫자 finite/type/bounds, bool 숫자거부. 이 값은 fixture 예시일 뿐 운영 승인값이 아님. live mode를 지원하지 않는다.

  고정 spec_sha256: `791082da4cb379117ed9fdc29a45bd42746f7a1aec368da1e9f4e1f3bfbff5b4` (공개 명세 원본 바이트, 2026-09-16). fixture metadata 자체의 해시와 혼동하지 않는다.
- [x] Step 3: 입력 행은 pair_id/symbol/now/각kis,toss의price,observed_at,fetched_at,status,latency_ms. 모든 시각aware·관측≤수신≤now, max_age/skew, finitepositive 가격, nonnegativefinite latency 검증. null/노후/실패/시각불일치 행은 차이분모에서 제외하되 총시도수/제외사유에 남긴다. symbol+관측시점 기준 duplicate를 제외(서로 다른 pair_id로 이중계상 금지). 공급자 raw error/body/credential 같은 미정의 필드는 출력하지 않는다.
- [x] Step 4: abs(toss-kis)/kis*100, nearest-rank p95(ceil(.95*n)-1), threshold 엄격초과 비율, 시도/유효/실패/중복/커버리지/시각제외 및 데이터셋 해시/manifest해시를 출력한다. 표본0 p95=None, insufficient. synthetic_only/production_eligible=False는 조건통과와 무관하게 유지한다. 숫자예시를운영승인기준으로사용금지.

```python
report = summarize_pairs([], manifest)
assert report["p95_difference_pct"] is None
assert report["production_eligible"] is False
called = []
assert build_shadow(factory=lambda: called.append(1)) is None
assert called == []
```

- [x] Step 5: 실제 CLI subprocess를 synthetic fixture로 실행해 JSON/exitcode/분모를 검산하고 과거결과 덮어쓰기/캐시읽기/인증설정 접근이 없음을 검증한다. 함수 OFF는 env를 암묵 조회하지 않는다. TOSS_API=1 문자열만으로 on으로 전환되는 경로도 만들지 않는다(현재 런타임 미배선).
- [x] Step 6: 세 모듈 통합 fixture 테스트(가짜 issuer/token→client→가격/페이지→shadow)와 전체 offline verify를 실행한다. subprocess는 명시 timeout, output path 미지정(stdout만), .env로드·운영results사용 금지. 자체 선행 커밋/리뷰 report를 부모에게 넘긴다.
- [ ] Step 7: 부모가 독립 작업별/전체 리뷰·수정·UTC/KST 전체 verify·비밀 검사 후 feature push/PR 생성한다. 설계 PR #65는 별도 유지하며 main 병합/배포는 실행하지 않는다. README에 reproducible offline CLI 명령과 Phase1 **실자료 부분 미완**을 명시한다.

## Final-review follow-up: cancellation-safe revoked observation

실제 Store/Manager/Client 조합의 전체 리뷰에서 재시도 예산으로 폐기 기록을 건너뛰는 경로를 고쳤다. 이후 deadline 선행 검사·락 대기 취소에도 같은 영속화 누락이 재현됐다. 이는 A01/Task1 Step3의 내부 안전 보완이며 실자료/운영 권한을 확장하지 않는다.

**Chosen contract:** `TokenManager.observe_revocation(failed_token)`는 동기 메서드다. OFF이면 저장소 I/O 0. ON이면 첫 await/deadline 검사 전에 해당 bearer의 digest·known generation·identity/origin/schema를 안전하게 게시한다. `recover()`도 idempotent 호출하며, client는 유효한 401 revoked 응답을 받은 뒤 `limiter.observe()`를 포함한 첫 비동기 대기 전에 호출한다. 재전송 예산·HTTP deadline을 늘리지 않고 background task/shield 발급을 만들지 않는다.

- [ ] Token owner (Astra/high): 기존 auth_state는 계속 발급 락 안에서만 변경. 별도 immutable 관측을 최대256개 고정 slot(각≤1KiB)에 no-clobber 게시·fsync하며, 충돌은 결정적 탐색·중복은 idempotent 처리. 포화는 고정 overflow latch로 지속 차단, 자동 삭제/TTL GC 없음. 해결증거 JSON≤64KiB는 발급 락 안에서만 갱신하고 정확한 관측ID에 대해 실제 확인한 유효·다른·더 높은 캐시만 증거로 인정. 같은 bearer는 해결 후에도 재사용 불가. 해결된 최신 캐시의 이후 정상 만료 갱신은 유지.
- [ ] Token owner: deadline 초과/락 취소→재시작·만료 mint0, 늦은 T1 응답과 정상 T2, 복수 관측·idempotency·동시 게시·포화·손상·symlink/권한·쓰기 실패·issuance_unknown·bootstrap 우회를 RED/GREEN으로 검증. 이미 시작한 외부 발급/다른 호스트를 취소·통제한다는 보장은 하지 않고 발급 허가 검사 시점을 명시.
- [ ] Client owner (Astra/high): sync API를 필수 token protocol에 추가하고 모호한 result/error·잘못된 401 형식은 계속 거부. 응답 직후 deadline 만료, limiter 대기 취소, token lock 대기 취소를 실제 token 모듈/임시 저장소/가짜 HTTP로 검증. expired-token은 재시도 허가 없는 발급0 유지. Token owner 파일은 편집하지 않음.
- [ ] Parent: own 커밋만 통합하고 Astra/xhigh 독립 한정 재리뷰로 위 경계 및 도입 회귀 확인. UTC/KST 전체 검증·비밀 검사·최신 SHA CI와 최종 문서를 갱신한 뒤 브랜치를 푸시. 기본 OFF·main/운영 미변경 유지.
