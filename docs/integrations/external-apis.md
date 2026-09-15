# 외부 API 연동

> 최종 갱신: 2026-09-16 (토스 Phase 1 오프라인 모듈, 실자료·운영 미배선)

## 브로커 — KIS (한국투자증권)

### KR (src/execution/broker/kis_kr.py)
- 실시간 호가, 일봉/분봉 캔들
- 주문 실행 (매수/매도), 체결 확인
- 포지션/잔고 조회
- 넥스트장/프리장 시세 (FHPST02300000)
- **원장 TR 초당 1건 제한** (2026-09-03): 잔고 TTTC8434R·체결 TTTC8001R·미체결 TTTC8036R은
  KIS 원장 서버가 계좌당 초당 1건 초과 시 HTTP 500 `EGW00201`("원장에서 허용 가능한 초당
  거래건수를 초과", 코드 `EGW00215`)를 반환한다. 전역 리미터(18/s)와 별개라 원장 TR 간
  1.05초 간격을 강제한다 (주문 POST는 미적용). 포트폴리오 동기화가 잔고+포지션을 연속
  호출해 30초마다 HTTP 500이 나던 것이 원인이었음 (일 ~4,000건 재시도 경고).
- **잔고 응답 스냅샷** (2026-09-11): `get_account_balance`의 inquire-balance 응답 `output1`을 5초 보관해
  바로 이어지는 `get_positions`가 재사용(1회용, 다음 페이지 있으면 미보관) — 동기화 30초 사이클의 8434R
  2회→1회. 장중 원장 초과(EGW00215)의 절반이 이 두 번째 호출이었다.
- **프로세스 공용 리미터** (`src/utils/kis_rate_limit.py`, 2026-09-03): 브로커·`kis_market_data`·
  `kr_screener`가 같은 appkey로 별도 세션을 쓰므로 게이트웨이 한도(20/s, `EGW00201`)는 합산으로
  걸린다 → 세 모듈이 하나의 슬라이딩 윈도우 + 호출 간 100ms 페이싱(10/s)을 공유 — 18/s·15/s는
  정속에서도 ~3% EGW00201 거절이 실측됐다(2026-09-03).
  KIS를 직접 호출하는 코드를 새로
  쓰면 `await kis_rate_limit.acquire(tr_id)`를 요청 직전에 넣을 것.
- 재시도 경고 형식: `[API] HTTP 500 TTTC8434R EGW00201 …, 1회 재시도` — `tr_id`로 호출 주체 식별
- **주문 POST 재전송 금지** (2026-09-03 P0): 접수(order-cash)·정정은 `_api_post(retry=False)` —
  타임아웃/연결 끊김/5xx 시 이미 접수됐을 수 있어 같은 본문을 다시 보내지 않는다 (hashkey는
  본문 무결성 검사이지 멱등키가 아님). 실패 반환 → 호출자 pending 해제 → 30초 동기화가
  실제 체결분을 sync_detected로 정합. 취소(order-rvsecncl 취소)는 멱등이라 재시도 유지.
- **토큰 회전 채택** (2026-09-03 P1): 공유 `KISTokenManager`의 토큰이 다른 컴포넌트
  (kis_market_data/kr_screener)에 의해 회전되면 `_get_headers`가 즉시 채택하고, 토큰 오류
  응답 시 `_recover_token()`이 회전 토큰이 있으면 `invalidate()`를 생략한다 — 무조건 무효화가
  새 토큰까지 지워 재발급 1분 제한(EGW00133) 락아웃을 부르던 문제 (8월 6회).

### US (src/execution/broker/kis_us.py)
- 해외주식 주문/체결
- 미체결 조회 (TTTS3018R)
- 당일 체결 (TTTS3035R)
- 잔고 조회 (TTTS3012R)

### WebSocket
- KR: H0STCNT0(실시간 체결가), H0STASP0(호가)
- US: HDFSCNT0(해외 실시간 체결), H0GSCNI0(체결통보)

### KOSPI200 선물 시세 (kis_market_data.py, FHMIF10000000)
- 종목코드 신형식: `A01 + 연도끝1자리 + 월2자리` (예: 2026년 9월물 `A01609`) —
  구형 `101T9000` 체계 폐지됨 (2026-08-05 확인, 마스터 `fo_idx_code_mts.mst` 기준)
- 시장구분: `CM`=야간(18:00~05:00, 기준가=주간 종가 → prdy_ctrt=밤사이 변동률), `F`=주간
- 아침 스크리닝 선행지표로 사용 (US 지수보다 우선, kr_scheduler)

## 데이터 — 토스증권 Open API (Phase 1 오프라인 모듈, **운영 미배선**)

> [설계서](../superpowers/plans/2026-09-15-toss-securities-fallback.md) · [구현·검증 원장과 CLI](../reviews/toss-phase1-offline-2026-09-16.md) · 상태: 오프라인 모듈 통합 검증 중, 인증 실자료·운영 활성화 미승인

- 구현 위치: `src/data/providers/toss/`의 보안 token store/manager, 조회 client/transport/limiter, 시장 자료 정규화, 합성 shadow 비교. `scripts/replay_toss_shadow.py`는 명시한 합성 JSON 파일만 읽어 stdout 보고서를 만든다.
- **현재 배선 없음**: 실제 OAuth 발급기는 주입 인터페이스뿐이며 키 로딩·시세 캐시·5분 잡·broker wrapper를 만들지 않았다. 기존 실행 경로·설정/의존성 파일은 무변경. `enabled=False`가 기본이며 `TOSS_API=1` 환경 문자열만으로 활성화되는 코드도 없다.
- 공개 명세 `1.2.17`/2026-09-16 원본 SHA는 `tests/fixtures/toss/spec_contract.json`에 고정했다. 오프라인 fixture의 한도·시각·비교 임계값은 합성 예시이지 승인된 운영값이 아니다. `production_eligible=False`를 유지한다.

- 용도(예정): **읽기 전용 2차 시세·참조 데이터**. 청산·사이징·포트폴리오 평가/최고가·주문·체결·잔고·계좌·호가는 **전 세션 KIS 단독**. 브로커 전역 폴백 훅 금지; 표시용 wrapper opt-in과 후보/점수 변경 승격을 분리
- Base `https://openapi.tossinvest.com` · WS `wss://openapi-ws.tossinvest.com/ws/v1` · OpenAPI 3.1 스펙 `/openapi-docs/latest/openapi.json`
- 인증: OAuth2 client_credentials, 기존 관측 `expires_in=86399`(약24h, 상수 아님). **클라이언트당 유효 토큰 1개**. 단일 issuer/reader·별도 고정 락·최초 생성부터0600인 전용 보안 캐시 필요(일반 atomic writer 그대로 사용 금지)
- 허용 IP 사전 등록 필수(미등록 403). 시세·종목·수급·랭킹·지수·캘린더는 토큰만으로 조회(계좌 헤더 불필요)
- Rate limit: 그룹별 TPS(`MARKET_DATA` 15 / `MARKET_DATA_CHART` 20 / `RANKING` 5 / `STOCK` 5 / `STOCK_ALL` 1 / `STOCK_TRADING_TREND` 10 / `MARKET_INFO` 3), 응답 헤더 `X-RateLimit-*`·429 `Retry-After`. **KIS 리미터와 분리된 독립 게이트**를 쓸 것
- 주요 필드 제약: `/prices` 는 `lastPrice`·`timestamp` 만(등락률·거래량·전일종가 없음), `timestamp` 는 조건부 nullable, 종목 정보에 **업종 필드 없음**(WICS 대체 불가), `sharesOutstanding` 은 `/stocks` 에만(`/stocks/all` 에는 없음)
- **일봉 정렬이 KIS 와 반대다** — 토스 `/candles` 는 최신순 내림차순, KIS `get_daily_prices` 는 오래된 순. 소비자가 `[-1]`·`[-200:]` 로 "끝이 최신"을 가정하므로 어댑터에서 반드시 재정렬(같은 유형의 역전이 2026-08-07 실사고). 값·수량은 decimal **문자열**
- **`401 token-revoked`는 재발급 신호가 아니다** — 다른 유효 공유 캐시로만1회 재시도. 동일/없음/손상이면 mint0·auth unavailable 지속, reader는 항상 mint0. 발급 응답 유실/저장 실패도 자동 재발급 금지
- **공개 OAuth scopes가 비어 있음** — 시세 전용 권한을 주장하지 않는다. 메서드+조회 endpoint allowlist·고정 HTTPS origin·redirect 금지·계좌 호출 금지; POST는 issuer의 token 발급만 예외. 허용 IP만으로 서버 내부 오호출을 막을 수 없음
- **가격 기준**: 국내 시세는 KRX+NXT 통합이라 20:00 까지 갱신 — KIS 정규장 종가와 다르다(실측 0.9% 차이). 청산·사이징에 그대로 쓰지 말 것
- 환경변수: `TOSS_CLIENT_ID`, `TOSS_CLIENT_SECRET` (`.env`, 커밋 금지)
- 모든 배포 기본 `TOSS_API=0`(토큰/네트워크/캐시 소비/잡 등록0). 실자료 shadow 전 약관·발급 소유권·manifest 승인 필요. 토스 shadow 캐시는 운영 수급/섹터/포트폴리오와 분리
- 일봉2개를 오늘/전일로 가정하지 않는다. 거래일·원 관측/수신 시각·확정/부분·시장/adjusted 기준을 검증하고 필수 결측은 legacy 숫자 dict를 만들지 않음(N/A 또는 스킵). 200은 페이지 크기이며 52주/252기간 요청은 전체 구간 확보 필요
- `/stocks/all`은 토스 거래 가능 목록이지 KRX 전체 정본이 아님. 종목 수급의 주/등록외국인/통합시장/잠정치와 시장 수급 금액 기준을 혼용하지 않음
- 합산 예산은 단일 송신자에서 공유(동시 reader 조회 금지). Reset은 1토큰 보충까지 초; 락·한도·HTTP·페이지·retry에 단일 deadline, 논리 조회 총 retry1·발급 retry0. 원문 인증/body/예외 로그 금지
- 위 TPS/IP/0.9%는 기존 조사 보고이며 이번에 인증 재호출하지 않았다. [공개 스펙](https://openapi.tossinvest.com/openapi-docs/latest/openapi.json) `1.2.17`/현재 main 소비자만 대조했으며 구현 인수는 설계 §10 참고

## 데이터 — pykrx

- KR 종목 마스터 (stock_list)
- 일봉 OHLCV
- `await asyncio.to_thread()` 필수 (동기 블로킹)
- **간헐적 실패** → DB 캐시 폴백
- 종목 마스터: pykrx 실패 시 FDR `StockListing("KRX")` 폴백
  (`storage/stock_master.py` 2026-04-21, `dashboard/data_collector.py` 2026-09-03)
- `get_market_sector_classifications`(WICS 업종): KRX 인증 없이는 항상 JSON 오류 →
  `sector_momentum`이 실패 시 **6시간 백오프** 후 키워드/파일 캐시 매핑 사용 (2026-09-03)

## 데이터 — yfinance

- US 역사 데이터, 시가총액
- SPY/QQQ 벤치마크 (시장 체제 판단)
- S&P 500/400 유니버스
- `asyncio.to_thread()` 래핑

## 데이터 — Finnhub

- US 뉴스 피드
- 어닝 캘린더
- 재무 메트릭 (EPS, Revenue)

## 데이터 — Finviz

- US 종목 스크리닝
- Beta 리스크 보정
- 장중 모멘텀 확인
- Short Interest

## 데이터 — Yahoo Finance (v8 API)

- 시장 지수 (KOSPI, KOSDAQ, S&P500, NASDAQ, DOW)
- KOSPI 벤치마크 히스토리 (/api/benchmark)
- SPY/QQQ 등락률 (US 시장 체제)
- 환율 (USDKRW)
- **비공식 API** — 인증 불요, rate limit 주의

## 데이터 — DART

- 조회 상태 계약(2026-09-15 교차 리뷰 후속): HTTP 200 / status `000`의 **해석 가능한 공시 목록**은 위험·호재 키워드가 없는 중립 공시도 `DartCheckResult.fetched=True`. 목록·행 모양과 비어 있지 않은 문자열 `report_nm`을 검증한다. 제목 누락/공백/비문자·잘못된 목록은 `fetched=False`이며 캐시하지 않는다. 유효 목록의 0건·status `013`은 정상 조회다. 유효·손상 행이 섞이면 이미 확인된 위험 사실은 보존하되 획득 성공으로 포장하지 않는다. HTTP/API/파싱 실패와 정상 무위험 결과를 구분한다.
- 불완전 목록은 `positive_disclosures`를 비워 실제 StockValidator의 +0.10 및 스크리너의 +15 호재 가산을 만들지 않는다. 정상 완전 호재의 가산과 확인된 block/warning의 차단·감점은 유지한다. 이는 shadow 출력만의 변경이 아니라 **불완전 자료의 실제 긍정 가산을 억제하는 의미 변경**이며, 주문·점수 임계값·설정값을 바꾼 것은 아니다.
- 위험 공시 차단 (유상증자, 소송 등)
- 호재 공시 보너스 (자사주 매입 등)
- `_apply_dart_catalyst()` in kr_screener.py
- **보유 종목 공시 경보 (2026-08-20~)**: `kr_scheduler.run_dart_alert_scheduler` —
  장중~장후(08:00~16:00) 10분 주기로 보유 종목의 당일 신규 위험 공시
  (DartChecker BLOCK/WARNING 키워드)를 감지, 텔레그램 즉시 경보.
  경보 전용(자동 매도 없음), 일중 dedup, 캐시 우회(`use_cache=False`).
  비용 최대 48콜/시 (DART 한도 20,000/일). LLM 정성 해석·자동 대응은
  경보 정확도 관측 후 승격 (리서치 #3, docs/research/ai-trading-research-2026-08.md)

### MCP 런타임 제거와 종목 검증 범위 (2026-09-15)

엔진의 MCP 클라이언트·부팅 연결·수급/검색 버즈 조회는 제거했다. SDK나 `pykrx-mcp` 서버를 설치하지 않으며, 부팅 중 `npx`로 Naver MCP 서버를 내려받거나 실행하지 않는다. 이 변경은 일반 `pykrx` 라이브러리와 별개다.

- `StockValidator`는 네이버 뉴스·DART 직접 검증을 유지한다. 수급·공매도·검색 버즈의 공개 결과 필드는 호환성을 위해 남기되 미획득·무가산으로 취급한다. 전체 검증을 정상 완료했다고 승격하지 않으며, 기존 `approved` 기본값·확인된 DART 위험 차단·뉴스/공시 조정값은 유지한다. `approved=True`는 자료 충분성이나 위험 해소의 증명이 아니다.
- `SupplyTrendDetector`는 기존 KIS 투자자 일별 조회를 유지한다. KIS 공급자 미주입 시 기존 일일 수급 폴백을 사용한다. MCP 응답/거래대금 단위가 맞지 않던 폴백은 삭제하고, KIS를 검증기의 신규 가산 경로로 연결하지 않는다.
- 전략 수집기의 미소비 업종별 MCP 조회를 없앤다. 기존 결과 키의 호환성과 나머지 직접 공급자 경로를 보존한다.
- 당시 MCP 미연결 운영을 기준으로 한 정리다. 과거 MCP를 실제 연결한 별도 환경에서는 해당 보조 가산이 제거되므로 모든 환경의 신호가 동일하다고 주장하지 않는다. 후속 공급자 도입은 관측 시각·단위·응답 계약·점수 효과를 별도 검증해야 한다.

검증·배포 기록: [MCP 정리 보고서](../reviews/mcp-retirement-2026-09-15.md).

## 데이터 — AIK Stock Data (공시 요약, 2026-08-11~)

- `https://aikstockdata.com/data/public/disclosures.json` — DART 공공데이터
  재가공 공시 피드 (중요도 점수·유형 라벨·장구분 태깅), 무키·무인증
- 소비: `src/data/providers/disclosure_feed.py`
  ① 아침 브리핑(07:30 슬롯) — "최근 공시 중요도 상위 5건" 섹션
  ② 크로스검증 `llm_second_check` — 종목별 "최근 공시 (보조 참고)" 컨텍스트
  ③ 배치 LLM 랭킹(Gemini) — 후보 라인 공시 태그
  (②③은 배치 스캔 시 갱신되는 메모리 캐시(TTL 6h) 동기 조회 — 캐시 미적재 시 생략)
- ⚠️ **개인 운영 무료 서비스 — 지속성·정확성 무보증. fail-open 필수**
  (실패 시 빈 문자열, 브리핑에서 섹션 생략). 매매 판단 경로에 연결 금지
  (T+1 데이터). 출처 표기 조건부 라이선스 — 브리핑에 출처 명시함

## LLM — OpenAI (GPT-5.4)

### 용도 (heavy 작업)
| 태스크 | 용도 |
|--------|------|
| STRATEGY_ANALYSIS | 매수 전 LLM 이중검증 (크로스검증) |
| TRADE_REVIEW | 일일 거래 복기 (20:30) |
| MARKET_ANALYSIS | 장전 시장 진단 (08:50) |

### 한도
- 이중검증: 10회/일
- 일일 예산: $5

## LLM — Gemini Flash

### 용도 (light 작업)
| 태스크 | 용도 |
|--------|------|
| THEME_DETECTION | 테마 탐지, 뉴스 요약 |
| QUICK_CLASSIFY | 빠른 분류 |
| WIKI_INGEST | Wiki 교훈 추출 (~$0.0001/회) |
| QUICK_ANALYSIS | 빠른 실시간 분석 |

## LLM — Perplexity (Sonar)

- 장전 시장 진단 시 실시간 매크로 검색
- `_fetch_perplexity_context()` in market_regime.py
- 타임아웃 15초, API 키: PERPLEXITY_API_KEY

## LLM — Manus API (2026-08-05 도입 → 2026-08-19 비활성)

> ⚠️ **2026-08-19 구독 해지로 비활성** (`llm.manus.enabled: false`).
> 배치 작업(trade_review/strategy_analysis)은 기존 API 경로로 회귀 —
> OpenAI gpt-5.6-sol primary, Gemini 3.1 Pro 폴백. 클라이언트 코드는 보존,
> 재구독 시 enabled만 되돌리면 복원. 아래는 도입 당시 스펙.

- 배치성 작업(거래 복기·전략 진화·주간 분석)을 OpenAI API 대신 Manus 에이전트로 처리
- `src/utils/manus_client.py` — 태스크 기반 비동기 API:
  `POST /v2/task.create` → `GET /v2/task.listMessages` 5초 폴링 →
  `agent_status=stopped` 시 assistant_message / structured_output_result 추출
- 인증: `x-manus-api-key` 헤더, API 키: MANUS_API_KEY
- agent_profile: manus-1.6 (기본) | manus-1.6-lite | manus-1.6-max — `llm.manus.agent_profile`
- 라우팅: `llm.py`의 `MANUS_ALLOWED_TASKS` allowlist(trade_review/strategy_analysis/market_analysis)
  ∩ config `llm.manus.tasks`. 실패 시 OpenAI/Gemini API 자동 폴백
- **응답 수십 초~수 분 — 실시간 매매 경로 사용 금지** (배치 전용)
- waiting(추가 입력 요구)·타임아웃(기본 600s) 시 `task.stop` 호출로 크레딧 낭비 방지

## 알림 — Telegram Bot

- 체결 알림 (매수/매도)
- 일일 리포트 (16:00)
- LLM 장전 진단 (08:50)
- 주간 원칙 리포트 (토요일)
- 주간 리밸런싱 결과
- 환경변수: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
- **HTML 파싱 실패 폴백 (2026-08-05~)**: `parse_mode=HTML` 발송이 400
  `can't parse entities`로 거부되면 `parse_mode` 제거 후 plain text로 자동 재발송
  (`send_message`/`send_alert` 공통). 근본 대책은 발신부에서 동적 문자열
  `html.escape()` — 미이스케이프 `<` 포함 메시지(예: `0 < 200,000`)가 3회 재시도
  전량 실패하던 문제의 안전망.

## 수수료

| 시장 | 매수 | 매도 | 왕복 |
|------|------|------|------|
| KR (한투 BanKIS) | 0.014% | 0.213% (세금 포함) | ~0.227% |
| US (KIS 해외주식) | 0% | 0% | 0% |
