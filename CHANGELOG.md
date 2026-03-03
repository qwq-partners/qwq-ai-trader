# QWQ AI Trader - Changelog

## 2026-03-04 — 코드 리뷰 + 전략 흐름 검증

### P0 수정 (Critical)
- **KR ORDER 핸들러 누락**: `EventType.ORDER` 핸들러가 미등록 → 매수/매도 주문이 이벤트 큐에서 드롭됨. `RiskManager.on_order()` 추가하여 `broker.submit_order()` 호출
- **ExitManager 메서드명 불일치**: `check_exit()` → `update_price()` 변경. KR 손절/익절 불가 해결
- **대시보드 SSE 미실행**: `dashboard.start()` → `dashboard.run()` (브로드캐스트 루프 포함)
- **RiskManager daily loss**: `daily_pnl` → `effective_daily_pnl` (미실현 손익 반영)
- **KR MarketContext session 누락**: `KRSession()` 인스턴스 추가

### US 거래소 코드 수정
- 현재가 조회: `NASD` → `NAS`, `NYSE` → `NYS`, `AMEX` → `AMS` 변환 (`_EXCD_QUOTE_MAP`)
- FRMI 시세 조회 실패 → 해결, 60주 매도 체결 완료
- 매도 수량: `float` → `int` 변환 누락 수정
- US 잔고: `output2.frcr_dncl_amt`(예수금) + `frcr_evlu_amt`(주식평가금) 사용

### 프론트엔드 수정
- JS: `/api/us-proxy/api/us/` → `/api/us/` (프록시 제거)
- SSE: `us_status`, `us_portfolio`, `us_positions`, `us_risk` 이벤트 구독 추가
- HTML 템플릿(8개) 누락 복사
- `rm._config` → `rm.config` (AttributeError 수정, 3곳)

### 검증 결과
| 흐름 | 상태 |
|------|------|
| KR 장중 스크리닝 → 매수 | 수정 완료 (ORDER 핸들러) |
| KR 체결 확인 → 포지션 등록 | PARTIAL (2분 동기화 의존) |
| KR 분할 익절/손절 → 매도 | 수정 완료 (update_price) |
| US 스크리닝 → 매수 | PASS |
| US 청산 → 매도 | PASS |
| KR 배치 스캔 → T+1 실행 | 수정 완료 (ORDER 핸들러) |

---

## 2026-03-03 — 초기 구조 (Phase 0-6)
**커밋**: `4790280` feat: KR+US 통합 트레이딩 엔진 초기 구조

### 프로젝트 생성
- GitHub 리포: `qwq-partners/qwq-ai-trader` (private)
- 기존 `ai-trader-v2` (KR)와 `ai-trader-us` (US)를 하나로 통합
- 근본 원인: 같은 KIS appkey로 두 프로세스 → HTTP 500 토큰 충돌

### 생성된 파일 (103개, 38,538줄)
**핵심 아키텍처**:
- `src/core/engine.py` — UnifiedEngine (KR+US 단일 이벤트 루프)
- `src/core/market_context.py` — MarketContext (시장별 컴포넌트 번들)
- `src/core/types.py` — 통합 도메인 타입 (Market, Position, Portfolio, Signal + market 필드)
- `src/core/event.py` — 통합 이벤트 시스템 (15개 EventType)

**유틸리티**:
- `src/utils/token_manager.py` — KISTokenManager (단일 인스턴스, 핵심!)
- `src/utils/config.py` — 통합 YAML 로더 (kr: + us: 섹션)
- `src/utils/session.py` — KRSession + USSession + USMarketCalendar
- `src/utils/logger.py`, `telegram.py`, `llm.py`, `fee_calculator.py`

**브로커**:
- `src/execution/broker/kis_kr.py` — KR 국내주식 (1,731줄)
- `src/execution/broker/kis_us.py` — US 해외주식 (1,018줄)
- 공유 토큰 매니저 주입 패턴

**전략 (7개)**:
- KR: Momentum, Theme, Gap&Go, SEPA (src/strategies/kr/)
- US: Momentum, SEPA, EarningsDrift (src/strategies/us/)
- 통합 ExitManager + RiskManager (시장별 설정 분기)

**데이터 레이어**:
- feeds: KIS WS, Finnhub WS, KIS US WS
- providers: yfinance, finviz, earnings, sector_momentum, supply_score, kis_market_data
- storage: stock_master, trade_storage
- screeners: kr_screener, us_screener, swing_screener

**스케줄러**:
- `src/schedulers/kr_scheduler.py` — KR 18개 백그라운드 작업
- `src/schedulers/us_scheduler.py` — US 10개 백그라운드 태스크

**대시보드 (포트 8080 통합)**:
- `src/dashboard/server.py` — 통합 aiohttp 서버
- `src/dashboard/kr_api.py` — KR REST API (/api/*)
- `src/dashboard/us_api.py` — US REST API (/api/us/*)
- `src/dashboard/sse.py` — 통합 SSE (KR+US 이벤트)
- `src/dashboard/data_collector.py` — KR 데이터 수집기
- static/ — HTML/JS/CSS

**진화 시스템**:
- trade_journal, trade_reviewer, llm_strategist, strategy_evolver, config_persistence

**설정**:
- `config/default.yml` — 통합 설정 (kr: + us: 섹션)
- `config/evolved_overrides.yml` — KR 진화 오버라이드

### 남은 작업 (Phase 7)
- [ ] systemd 유닛 작성 (qwq-ai-trader.service)
- [ ] import 경로 불일치 수정 (런타임 테스트)
- [ ] --dry-run 실행 테스트
- [ ] 기존 ai-trader + ai-trader-us 서비스 교체
- [ ] 모바일 앱 API 엔드포인트 업데이트
