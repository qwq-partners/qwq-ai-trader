# 외부 API 연동

> 최종 갱신: 2026-04-06

## 브로커 — KIS (한국투자증권)

### KR (src/execution/broker/kis_kr.py)
- 실시간 호가, 일봉/분봉 캔들
- 주문 실행 (매수/매도), 체결 확인
- 포지션/잔고 조회
- 넥스트장/프리장 시세 (FHPST02300000)

### US (src/execution/broker/kis_us.py)
- 해외주식 주문/체결
- 미체결 조회 (TTTS3018R)
- 당일 체결 (TTTS3035R)
- 잔고 조회 (TTTS3012R)

### WebSocket
- KR: H0STCNT0(실시간 체결가), H0STASP0(호가)
- US: HDFSCNT0(해외 실시간 체결), H0GSCNI0(체결통보)

## 데이터 — pykrx

- KR 종목 마스터 (stock_list)
- 일봉 OHLCV
- `await asyncio.to_thread()` 필수 (동기 블로킹)
- **간헐적 실패** → DB 캐시 폴백

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

## 알림 — Telegram Bot

- 체결 알림 (매수/매도)
- 일일 리포트 (16:00)
- LLM 장전 진단 (08:50)
- 주간 원칙 리포트 (토요일)
- 주간 리밸런싱 결과
- 환경변수: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

## 수수료

| 시장 | 매수 | 매도 | 왕복 |
|------|------|------|------|
| KR (한투 BanKIS) | 0.014% | 0.213% (세금 포함) | ~0.227% |
| US (KIS 해외주식) | 0% | 0% | 0% |
