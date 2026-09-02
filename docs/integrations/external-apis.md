# 외부 API 연동

> 최종 갱신: 2026-04-06

## 브로커 — KIS (한국투자증권)

### KR (src/execution/broker/kis_kr.py)
- 실시간 호가, 일봉/분봉 캔들
- 주문 실행 (매수/매도), 체결 확인
- 포지션/잔고 조회
- 넥스트장/프리장 시세 (FHPST02300000)
- **원장 TR 초당 1건 제한** (2026-09-03): 잔고 TTTC8434R·체결 TTTC8001R·미체결 TTTC8036R은
  KIS 원장 서버가 계좌당 초당 1건 초과 시 HTTP 500 `EGW00201`("원장에서 허용 가능한 초당
  거래건수를 초과")를 반환한다. 전역 리미터(18/s)와 별개라 `_rate_limit(tr_id)`가 원장 TR
  간 1.05초 간격을 강제한다 (주문 POST는 미적용). 포트폴리오 동기화가 잔고+포지션을 연속
  호출해 30초마다 HTTP 500이 나던 것이 원인이었음 (일 ~4,000건 재시도 경고).
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

- 위험 공시 차단 (유상증자, 소송 등)
- 호재 공시 보너스 (자사주 매입 등)
- `_apply_dart_catalyst()` in kr_screener.py
- **보유 종목 공시 경보 (2026-08-20~)**: `kr_scheduler.run_dart_alert_scheduler` —
  장중~장후(08:00~16:00) 10분 주기로 보유 종목의 당일 신규 위험 공시
  (DartChecker BLOCK/WARNING 키워드)를 감지, 텔레그램 즉시 경보.
  경보 전용(자동 매도 없음), 일중 dedup, 캐시 우회(`use_cache=False`).
  비용 최대 48콜/시 (DART 한도 20,000/일). LLM 정성 해석·자동 대응은
  경보 정확도 관측 후 승격 (리서치 #3, docs/research/ai-trading-research-2026-08.md)

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
