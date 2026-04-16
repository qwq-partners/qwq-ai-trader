# 시스템 아키텍처

> 최종 갱신: 2026-04-15 (P1/P2 엔진·스케줄러 수정 반영)

## 전체 구조

```
UnifiedTradingBot (scripts/run_trader.py)
├── UnifiedEngine (src/core/engine.py) ─── KR 시장
│   ├── RiskManager (engine.py 내부) ── 신호 관리, 크로스검증, 거래메모리, Wiki
│   ├── Portfolio (KRW)
│   ├── KISBroker (KR)
│   ├── ExitManager ── 분할익절 + ATR 동적손절
│   ├── BatchAnalyzer ── 아침/점심/저녁 배치 스캔
│   ├── Strategies: SEPA, RSI2, Theme, Gap, Strategic Swing, Core
│   └── Schedulers: KRScheduler (시간 기반 태스크)
│
├── _USEngineBundle (run_trader.py) ─── US 시장
│   ├── RiskManager (risk/manager.py)
│   ├── Portfolio (USD)
│   ├── KISUSBroker
│   ├── ExitManager
│   ├── CrossStrategyValidator (market="US") + TradeWiki
│   ├── USMarketRegimeAdapter (SPY/QQQ)
│   ├── Strategies: Momentum, SEPA, EarningsDrift
│   └── Schedulers: USScheduler
│
├── DashboardServer (aiohttp, port 8080)
│   ├── KR API (/api/*)
│   ├── US API (/api/us/*)
│   ├── Engine API (/api/engine/*)
│   └── SSE Stream (/api/stream)
│
└── Shared Components
    ├── KISTokenManager (토큰 통합 관리)
    ├── LLMManager (OpenAI + Gemini)
    ├── TelegramNotifier
    └── HealthMonitor
```

## 신호 흐름 (KR)

```
08:20 아침 스캔
  SwingScreener.run_full_scan() → candidates (indicators 포함)
  ↓
  SEPA/RSI2 generate_batch_signals() → signals
  strategic_swing _generate_strategic_signals() → signals
  ↓
  LLM 랭킹 (Gemini Flash) → priority/exclude
  US 오버나이트 보정 (-7pt bearish)
  ↓
  PendingSignal 저장 (JSON)

09:30 시그널 실행
  execute_pending_signals()
  ↓ 각 시그널:
  현재가 조회 → 갭다운/갭업 체크
  Signal 재생성 (position_multiplier 재계산, indicators 캐시 주입)
  ↓
  engine.on_signal()
    크로스검증 9규칙 (50점 미만 차단)
    LLM 이중검증 (85+ 비강세장, 10회/일)
      → Wiki 교훈 + 거래메모리 컨텍스트 주입
    ↓
    포지션 사이징 (전략별 % × position_multiplier)
    리스크 체크 (일일손실, max_positions, 섹터 집중)
    ↓
    주문 생성 → KIS API 매수

체결 확인 (10초 간격)
  ExitManager 등록 (ATR 동적 손절)
  TradeJournal 기록
  ↓
  ExitManager 실시간 감시 (WS + REST 백업)
    1차 익절 5% → 20% 매도
    2차 익절 15% → 25% 매도
    트레일링 → 고점 대비 하락 시 청산
    ATR 동적 손절 (max 8%, min 3.5%)
  ↓
  매도 체결
    TradeJournal 기록
    trade_memory.record_outcome() (L1)
    Wiki.ingest() (fire-and-forget)
    risk_manager.record_exit() (재진입 제한)
```

## 신호 흐름 (US)

```
스크리닝 (15분 주기)
  StockScreener/Finviz 후보 → 전략 evaluate → signals
  ↓
  시장체제 보정 (min_score_adj, max_buys 제한)
  점수 순 정렬 → top N 처리

_process_signal()
  현재가 조회 → 가격 괴리 체크 (±3~5%)
  ↓
  크로스검증 6규칙 (indicator_cache 주입 + Wiki 교훈)
  ↓
  포지션 사이징 × ATR multiplier × 체제 배율
  Finviz Beta 보정
  장중 모멘텀 최종 확인
  ↓
  매수 주문 (지정가 +0.2%)

매도
  exit_check_loop (15초) → ExitManager
  sell_qty > 실제 보유 → 자동 클램핑
  연속 3회 실패 → 포트폴리오 동기화 강제
  ↓
  Wiki.ingest() (fire-and-forget)
```

## 비동기 아키텍처

- **단일 asyncio 이벤트 루프**: KR + US 스케줄러 동시 실행
- **KR 태스크**: 스크리닝(5분), 체결확인(10초), 포트폴리오(30초), REST피드(20초), 테마(10분), 수급(5분), 배치(08:20/12:30/19:30), evolve(20:30), 리밸런싱(토요일)
- **US 태스크**: 스크리닝(15분), exit체크(15초), 포트폴리오(30초), 주문체크(10초), EOD청산(30초), 스크리너(60분), heartbeat(5분)
- **WS**: KR 실시간가(H0STCNT0) + US 실시간가(HDFSCNT0) + 체결통보
- **fire-and-forget**: Wiki ingest, 텔레그램 알림 (매매 비차단)

## 핵심 파일 크기

| 파일 | 줄 수 | 역할 |
|------|-------|------|
| `scripts/run_trader.py` | 1,850 | 통합 진입점, 초기화, _USEngineBundle |
| `src/core/engine.py` | 1,720 | UnifiedEngine, RiskManager(내부) |
| `src/schedulers/kr_scheduler.py` | 3,800 | KR 시간 기반 태스크 전체 |
| `src/schedulers/us_scheduler.py` | 2,900 | US 시간 기반 태스크 전체 |
| `src/core/batch_analyzer.py` | 1,800 | 배치 스캔 + 시그널 실행 |
| `src/dashboard/data_collector.py` | 1,600 | 대시보드 데이터 수집 |
| `src/strategies/exit_manager.py` | 1,200 | 분할익절 + ATR 트레일링 |
