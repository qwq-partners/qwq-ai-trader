# QWQ AI Trader - Changelog

## 2026-03-05 — US 오버나이트 + KOSPI200 야간선물 레짐 연동

### 1. screen_all에 오버나이트 레짐 직접 연동
- **`src/signals/screener/kr_screener.py`**: `screen_all()`에 `overnight_sentiment`, `overnight_volatility` 파라미터 추가
  - 7-7 단계: bearish → 수급 없는 종목 -20pt, 수급 있는 종목 -5pt
  - bullish → 기관/외국인 수급 종목 +10pt
- **`src/schedulers/kr_scheduler.py`**: 스크리닝 루프에서 `get_overnight_signal()` 호출 → screen_all에 전달

### 2. 변동성 기반 동적 포지션 사이징
- **`src/schedulers/kr_scheduler.py`**: 자동 진입 시 오버나이트 변동성에 따른 조정
  - bearish → min_score=85, 일일진입=1회
  - 변동성 2~3% → min_score +3, position_multiplier=0.7
  - 변동성 3%+ → min_score +5, position_multiplier=0.5
  - `metadata.position_multiplier`로 엔진 포지션 사이징에 반영 (기존 메커니즘 활용)

### 3. KOSPI200 야간선물(KRX) 현재가 조회
- **`src/data/providers/kis_market_data.py`**: `get_night_futures_quote()` 신규 메서드
  - KIS API TR ID: `FHMIF10000000`, 종목코드: `101W09` (KOSPI200 근월물)
  - 등락률 ±1% 기준 bullish/bearish/neutral 판정
  - 5분 캐시, price/change_pct/volume/sentiment 반환
- **`src/schedulers/kr_scheduler.py`**: US 지수보다 야간선물 sentiment 우선 적용
  - 야간선물 데이터가 있고 neutral이 아니면 US 지수 sentiment를 덮어씀

### 수정 파일
- `src/signals/screener/kr_screener.py`
- `src/schedulers/kr_scheduler.py`
- `src/data/providers/kis_market_data.py`

---

## 2026-03-05 — KR 종목 선별 고도화 3종 (대장주/재료소멸/수급)

### 1. 테마 대장주 독식 필터 (Winner Takes All)
- **`src/signals/screener/kr_screener.py`**: `screen_all()` 7-5 단계 추가
  - 같은 테마 내 여러 종목이 올라왔을 때 점수 기준 1등(대장주)에 +10pt 보너스
  - 2등 이하 종목에 -25pt 감점 + "테마[X] 2등주 감점 (대장: Y)" 사유 태깅
  - theme_detector의 stock_sentiments에서 테마 그룹핑

### 2. 재료 생애주기 필터 (Buy the rumor, Sell the news)
- **`src/signals/sentiment/kr_theme_detector.py`**: LLM 프롬프트에 `catalyst_phase` 필드 추가
  - `rumor`: 기대감/루머/검토 단계 → 스크리너에서 +8pt 보너스
  - `confirmed`: 확정/완료 단계 → 급등(+5%) 시 -30pt, 상승(+2%) 시 -15pt 감점
  - `_stock_sentiments` 저장 구조에 `catalyst_phase` 필드 추가
- **`src/signals/screener/kr_screener.py`**: 7-5b 재료 생애주기 필터 단계 추가

### 3. 개인 단독 매수 감점 필터
- **`src/signals/screener/kr_screener.py`**: 7-6 단계 추가
  - 상승(+3%) 중인데 기관/외국인 수급이 없는 종목에 -15pt 감점
  - "개인단독매수 의심" 사유 태깅

### 수정 파일
- `src/signals/screener/kr_screener.py`
- `src/signals/sentiment/kr_theme_detector.py`

---

## 2026-03-05 — 종목 필터링 재검증 (P0 1건 + P1 3건 수정)

### P0 수정 (1건)
- **`src/strategies/us/momentum.py:34`**: 최소 주가 $5 필터 누락 → `close < 5.0` 체크 추가
- **`src/strategies/us/sepa_trend.py:35`**: 동일 — $5 필터 추가
- **`src/strategies/us/earnings_drift.py:42`**: 동일 — $5 필터 추가
  - 스크리너 우회 경로(거래량급증, 동적유니버스)로 penny stock 진입 가능했음

### P1 수정 (3건)
- **`src/signals/screener/kr_screener.py:1748`**: `screen_all()` min_price 기본값 `0` → `1000` (호출처 미지정 시 1,000원 미만 종목 우회 방지)
- **`src/strategies/base.py:301`, `src/indicators/technical.py:361`**: vol_ratio 기본값 `0` → `1.0` (중립값) — 거래량 데이터 없을 때 0이면 의미없는 차단/통과 발생
- **`src/strategies/us/sepa_trend.py:47,83`**: `if not all([ma50, ...])` → `any(v is None or v <= 0 ...)` + `if ma5 > 0` → `if ma5 is not None` (0값 False 버그 수정)

### 수정 파일
- `src/strategies/us/momentum.py`, `src/strategies/us/sepa_trend.py`, `src/strategies/us/earnings_drift.py`
- `src/signals/screener/kr_screener.py`, `src/strategies/base.py`, `src/indicators/technical.py`

---

## 2026-03-05 — US KIS WS 체결통보 콜백 구현

### 수정: `scripts/run_trader.py`
- **`_on_kis_fill()`**: placeholder → 실제 구현
  - 체결 즉시 상세 로그 출력 (종목, 수량, 가격, 전략, 주문번호)
  - 텔레그램 즉시 알림 (REST 폴링 10초 대기 없이 WS Push 시점에 발송)
  - pending 주문 매칭하여 전략명 포함
  - 실제 포지션 처리는 기존 `order_check_loop`이 담당 (중복 처리 방지)

---

## 2026-03-05 — 전체 코드 복기 P0+P1 수정 (16건)

### P0 수정 (9건)
- **`src/core/engine.py:1296`**: 시장가 주문 `order.price=None` 포맷 크래시 → price_str 분기 처리
- **`src/core/engine.py:1084`**: `RiskConfig`에 없는 `pre_market_slippage_buffer_pct` → `engine.config`에서 getattr로 접근
- **`src/risk/manager.py:218-227`**: KR에서 `max_positions`/`min_cash_reserve` 체크 누락 → KR+US 공통 적용
- **`src/schedulers/kr_scheduler.py:345`**: 청산 pending 예외 시 `discard()` 미호출 → 교착 방지 추가
- **`src/schedulers/kr_scheduler.py:834`**: 포트폴리오 동기화 120초 → 30초 (설계 일치)
- **`src/schedulers/kr_scheduler.py:55`**: 수동매수 하드코딩 `_manual_buy_orders` 비우기 (1회 실행 완료)
- **`src/schedulers/us_scheduler.py:1014`**: 일일 통계 리셋 레이스 컨디션 → `portfolio_sync_loop` 중복 제거
- **`src/strategies/kr/momentum.py:293-309`**: `if ma5 and ma20` → `if ma5 is not None and ma20 is not None` (0값 False 방지)
- **`src/dashboard/kr_api.py:370`**: `os._exit(0)` → `sys.exit(0)` (graceful shutdown)

### P1 수정 (7건)
- **`src/strategies/kr/rsi2_reversal.py`**: `check_rr_ratio()` R/R 필터 추가 + `if close and` 패턴 수정(2건)
- **`src/strategies/kr/gap_and_go.py:98`**: `min_price` 필터 추가 (동전주 진입 차단)
- **`src/core/engine.py:522`**: `if not pos.strategy` → `if pos.strategy is None` 패턴 수정
- **`src/core/engine.py:562`**: 음수 수량 포지션 경고 + 0 보정 후 제거
- **`src/data/providers/supply_score.py:32`**: 영업일 계산에 `is_kr_market_holiday()` 적용
- **`src/strategies/exit_manager.py:401`**: 본전 이탈 판정에 매도 수수료 버퍼 0.25% 추가
- **`src/schedulers/kr_scheduler.py:1498`**: 수급캐시 루프에 공휴일 체크 추가

### 수정 파일
- `src/core/engine.py`, `src/risk/manager.py`
- `src/schedulers/kr_scheduler.py`, `src/schedulers/us_scheduler.py`
- `src/strategies/kr/momentum.py`, `src/strategies/kr/rsi2_reversal.py`, `src/strategies/kr/gap_and_go.py`
- `src/strategies/exit_manager.py`
- `src/dashboard/kr_api.py`, `src/data/providers/supply_score.py`

---

## 2026-03-05 — US 테마/섹터 탐지기 구현 + 수동매수/청산예외 기능

### 신규: `src/signals/sentiment/us_theme_detector.py`
- **RSS 뉴스 수집**: MarketWatch, CNBC, Yahoo Finance RSS (무료, API 키 불필요)
- **Finnhub 뉴스**: API 키 있으면 보너스 소스로 활용
- **LLM 테마 추출**: Gemini Flash로 영문 뉴스 → 테마/종목 임팩트 JSON 추출
- **섹터 ETF 모멘텀**: SPDR 11개 섹터 ETF (XLK~XLC) 1일 수익률로 테마 점수 보정 (±15점)
- **12개 테마**: AI/Semiconductors, Cloud/SaaS, EV/Clean Energy, Biotech/Pharma, Fintech/Payments, Cybersecurity, Space/Defense, Nuclear Energy, Quantum Computing, Robotics/Automation, Streaming/Media, Cannabis
- **종목 센티멘트**: impact(-10~+10), direction, theme, reason (1시간 유효)
- **대시보드 연동**: `/api/us/themes` 엔드포인트 정상 동작

### 수정: `scripts/run_trader.py`
- finnhub_key 조건 제거 → RSS+LLM 기반이므로 항상 USThemeDetector 초기화

### 신규: 수동 매수 예약 + 청산 예외 기능
- **`src/strategies/exit_manager.py`**: `_exit_exempt` 셋 추가 — `add_exit_exempt()`, `remove_exit_exempt()`, `is_exit_exempt()` 메서드
- **`src/schedulers/kr_scheduler.py`**: `run_manual_buy_orders()` — 09:00 장 시작 시 수동 시장가 매수 + 청산 예외 등록
- **적용**: 123320 TIGER 레버리지 ETF 가용예산 풀매수, 익절/손절 비활성화

---

## 2026-03-04 — 스크리닝 시스템 8가지 개선 (KR+US 공통)

### 1. 인트라데이 전략 재활성화
- **`config/evolved_overrides.yml`**: gap_and_go, momentum_breakout, theme_chasing → `enabled: true`
- **배분 조정**: SEPA 30%, Momentum 25%, RSI2 20%, Gap&Go 15%, Theme 10%

### 2. RS Ranking 통합
- **`src/signals/screener/us_screener.py`**: SPY 벤치마크 기반 RS 보너스 (RS≥80: +15pt, RS≥70: +10pt, RS<30: -10pt)
- **`src/signals/screener/kr_screener.py`**: KOSPI 지수 대비 상대강도 `_apply_rs_ranking_bonus()` 필터 추가
- **`src/strategies/us/sepa_trend.py`, `us/momentum.py`**: RS rating 점수 반영 (최대 +10pt)
- **`src/strategies/base.py`**: USBaseStrategy에 `set_benchmark()` + `_get_indicators()`에 RS 자동 계산
- **`src/schedulers/us_scheduler.py`**: SPY 벤치마크 전략 자동 주입

### 3. R/R 비율 필터
- **`src/strategies/base.py`**: `check_rr_ratio()` 헬퍼 (KR BaseStrategy + US USBaseStrategy)
- **적용**: SEPA(KR+US), Momentum(US), EarningsDrift(US), Gap&Go(KR) — min R/R 2.0
- **`config/default.yml`**: `min_rr_ratio: 2.0` 설정 추가

### 4. 프리마켓 갭 스캔
- **`src/signals/screener/kr_screener.py`**: `screen_premarket_gap()` — 08:30~09:00 갭상승 종목 탐지
- **`src/signals/screener/us_screener.py`**: `scan_premarket_gap()` — Finviz 프리마켓 데이터 활용
- **`screen_all()`**: 08~09시 자동 프리마켓 갭 스캔 통합
- **`us_scheduler.py`**: 프리마켓 갭 종목 스크리닝 최우선 삽입

### 5. 촉매 스캔 (DART + Earnings)
- **`src/signals/screener/kr_screener.py`**: `_apply_dart_catalyst()` — DART 공시 긍정/위험/차단 자동 처리
- **`src/signals/screener/us_screener.py`**: 어닝스 촉매 보너스 (갭상승+3%: +15pt, +1%: +8pt)
- **`us_scheduler.py`**: earnings_today → screener 자동 주입

### 6. ORB (Opening Range Breakout) 확인 매수
- **`src/strategies/kr/gap_and_go.py`**: ORB 범위(고/저) 추적, 상단 돌파 시 +10pt 보너스
- **`src/strategies/us/momentum.py`**: 전일 고가 돌파 + 갭업 ORB 보너스 +5pt

### 7. 섹터 로테이션 시그널
- **`src/signals/screener/kr_screener.py`**: `_apply_sector_rotation_bonus()` — SectorMomentumProvider 활용, 강세섹터 +10pt, 약세섹터 -10pt
- **`src/signals/screener/us_screener.py`**: SPDR 섹터 ETF (XLK, XLF, XLV 등) 20일 모멘텀 계산

### 8. 동적 유니버스 확장
- **`src/schedulers/us_scheduler.py`**: screener 상위 50종목(score≥60) 자동 유니버스 편입 (최대 30개/사이클)

### 수정 파일
- `config/evolved_overrides.yml`, `config/default.yml`
- `src/strategies/base.py`, `src/strategies/kr/sepa_trend.py`, `src/strategies/kr/gap_and_go.py`
- `src/strategies/us/sepa_trend.py`, `src/strategies/us/momentum.py`, `src/strategies/us/earnings_drift.py`
- `src/signals/screener/kr_screener.py`, `src/signals/screener/us_screener.py`
- `src/schedulers/us_scheduler.py`
- `src/indicators/technical.py` (기존 `rs_rating()` 활용)

---

## 2026-03-04 — US 엔진 7가지 버그 수정 (장 오픈 대비)

### P0: initial_capital 매 동기화 덮어쓰기 → 최초 1회만 설정
- **`src/schedulers/us_scheduler.py`**: `_sync_portfolio()`에서 `initial_capital`을 `total_equity`로 30초마다 덮어쓰던 문제 수정
- **영향**: `total_pnl`(총 손익)이 항상 0에 수렴하여 수익 추적 불가 + 리스크 판단 왜곡

### P0: exit_stages 반복 복원 → 초기화 시 1회만
- **`src/schedulers/us_scheduler.py`**: `_sync_portfolio()`에서 `exit_stages` 캐시를 매 동기화마다 복원하던 문제 → `_exit_stages_restored` 플래그로 1회만 실행
- **영향**: 런타임 중 진행된 익절 단계(FIRST→SECOND)가 캐시의 이전 값으로 롤백되어 중복 분할매도 발생 가능

### P0: 전략 exit 실패 시 ExitManager 손절 누락 방지
- **`src/schedulers/us_scheduler.py`**: `_check_exits()`에서 전략별 `check_exit()` 호출 후 `_execute_exit` 실패 시에도 `break`로 ExitManager 체크를 건너뛰던 문제 → `strategy_exit_attempted` 플래그로 전략 exit 미발동 시 ExitManager 정상 실행

### P1: 부분체결(partial) 교착 상태 해소
- **`src/schedulers/us_scheduler.py`**: `_check_orders()`에서 `partial` 상태에 로그만 남기고 교착되던 문제 → 부분체결 타임아웃 추가 (매도 3분, 매수 15분), 타임아웃 시 잔여 취소 + 체결분 반영

### P1: EOD 청산 중복 실행 방지
- **`src/schedulers/us_scheduler.py`**: `eod_close_loop()`에서 마감 15분간 30초마다 `_eod_close()` 반복 호출되던 문제 → `_eod_close_done` 날짜 플래그로 당일 1회만 실행

### P1: 매수 체결 시 기존 포지션 수량/평균가 갱신
- **`src/schedulers/us_scheduler.py`**: `_on_order_filled()` 매수 체결 시 sync에서 이미 생성된 포지션의 수량/평균가를 체결 정보로 갱신하지 않던 문제 수정

### P1: API 빈 응답 방어 강화
- **`src/schedulers/us_scheduler.py`**: `_sync_portfolio()`에서 account_info는 있지만 positions만 빈 배열로 반환된 경우 로컬 포지션 급감 방어 로직 추가

### P1: 스크리너 캐시 주말 무효화 방지
- **`src/signals/screener/us_screener.py`**: 캐시 유효기간 1일→3일 (금요일 스캔 → 월요일 사용 가능)

## 2026-03-04 — stock_master 안정화 + WS 장시간 제어

### P0: pykrx stock_master 로딩 실패 해결
- **`src/dashboard/data_collector.py`**: pykrx 실패 시 StockMaster DB(`kr_stock_master` 테이블)에서 종목명 폴백 로드 (3708개 종목)
- **`src/dashboard/data_collector.py`**: pykrx 재시도 횟수 제한 (최대 3회) — 무한 반복 WARNING 방지
- **`src/dashboard/data_collector.py`**: 캐시 파일 단일화 (`stock_master.json`), TTL 72시간으로 확장
- **원인**: pykrx `get_market_ticker_list()`가 장 마감 후 KRX 서버에서 빈 응답 반환 → `index -1` 에러

### P1: KR WebSocket 장 마감 후 불필요한 연결 방지
- **`src/data/feeds/kis_websocket.py`**: `_is_market_active()` 메서드 추가 — `KRSession`으로 장외 시간 판별
- **`src/data/feeds/kis_websocket.py`**: `run()` 루프에서 장 마감(CLOSED) 시 WS 연결 해제 + 대기, 장 시작 시 자동 재연결
- **효과**: 장 마감 후 2분마다 끊기던 WS 재연결 사이클 완전 제거

## 2026-03-04 — CLAUDE.md 대폭 업데이트

### 문서: CLAUDE.md 상세화 (ai-trader-v2 참고)
- **`CLAUDE.md`**: 79줄 → 300줄+ 대폭 확장
  - 세션 시작 필수 읽기 설명 보강 (중복 작업 방지, 맥락 파악)
  - Git & GitHub 섹션 신규 추가
  - 코드 리뷰 프로토콜 추가 (P0/P1/P2 분류)
  - 매매 전략 상세 (KR 5개 + US 3개 나열, ExitManager 파라미터)
  - 리스크 관리 테이블 (KR/US 분리, 상세 파라미터)
  - 수수료 정보 (KR 왕복 0.227%, US Zero-commission)
  - 실행 흐름 상세 (KR 스케줄러 7태스크 + US 스케줄러 9태스크)
  - WebSocket 피드 정보 (KR H0STCNT0, US HDFSCNT0)
  - 대시보드 개발 패턴, 운영 모니터링 계층
  - 코딩 규칙 금지 패턴 코드 예시
  - 설정 주의사항 (evolved_overrides 머지)
  - 의존성, LLM 모델 선택, 진화 시스템 상세
  - 트러블슈팅 가이드 (5개 시나리오)
  - 실행 방법 (--market kr|us|both)

## 2026-03-04 — WS 실시간 포지션 모니터링 + 스캔 품질 개선 + 전략 다변화

### P0: WS 실시간 보유 포지션 모니터링 구현
- **`scripts/run_trader.py`**: `_load_existing_positions()` 후 `ws_feed.set_priority_symbols()` + `subscribe()` 호출하여 보유 종목 WS 자동 구독
- **`scripts/run_trader.py`**: `_on_market_data()` 콜백에서 보유 종목 수신 시 `kr_scheduler._check_exit_signal()` 즉시 호출 (WS 실시간 청산 체크)
- **`scripts/run_trader.py`**: `kr_scheduler` 인스턴스를 봇 속성으로 저장 (WS 콜백 접근용)
- **`src/schedulers/kr_scheduler.py`**: `run_fill_check()` BUY 체결 시 WS priority symbols 갱신 + 신규 심볼 구독
- **`src/schedulers/kr_scheduler.py`**: `run_rest_price_feed()` 폴링 간격 45초 → 20초 (WS 백업 역할 강화)

### P1: FDR 조회 타임아웃 개선
- **`src/signals/screener/swing_screener.py`**: `_calculate_all_indicators()` 타임아웃 10초 → 15초, 실패 시 1회 재시도
- **`src/signals/screener/swing_screener.py`**: `_load_benchmark_index()` FDR 실패 시 KIS API (`broker.get_daily_prices("0001")`) 폴백

### P2: SEPA 전략 점수 완화 + 전략 다변화
- **`src/core/batch_analyzer.py`**: `execute_pending_signals()` 전략별 최대 포지션 수 제한 추가 (`rsi2_reversal` 최대 3개, `sepa_trend` 최대 3개, 기타 2개)
- `config/default.yml` sepa_trend min_score는 이미 55 (변경 불필요)

### P3: 포지션 모니터링 간격 단축
- **`config/default.yml`**: `position_update_interval` 30 → 10분
- **`src/schedulers/kr_scheduler.py`**: 기본값 30 → 10분

### P4: US 포트폴리오 초기화 None 비교 버그
- **`scripts/run_trader.py`**: `_initialize_us()` 잔고 조회 시 `balance.get('total_equity') or 0` + `is not None` 가드 추가

### P5: stock_master 로컬 캐시 폴백
- **`src/dashboard/data_collector.py`**: `_load_stock_master_sync()` pykrx 성공 시 `~/.cache/ai_trader/stock_master_kospi.json` 캐시 저장, 실패 시 캐시 로드 (TTL 48시간)

## 2026-03-04 — 버그 상세 리뷰 + 수정 (2차)

### 수정 완료
- **재시작 중복 매수 방지** (`kr_scheduler.py`): `last_execute_date` 플래그 파일 영속화 (`~/.cache/ai_trader/executed_YYYY-MM-DD.flag`). 풀백/catch-up/정규 실행 3곳 모두 적용. 오래된 플래그 자동 정리
- **config 경로 전수 수정** (`kr_scheduler.py`): `bot.config.get("scheduler")` → `bot.config.get("kr", "scheduler")` 등 6곳. `intraday_buy`, `momentum_breakout` 포함. 기존엔 항상 기본값 폴백되던 문제 해결
- **portfolio guard 추가** (`batch_analyzer.py`): `execute_pending_signals()` 시작 시 포지션 비어있으면 `broker.get_positions()` 호출하여 복구
- **VCP timedelta import** (`vcp_detector.py`): `_cleanup_old_cache`에서 `timedelta` 미정의 → import 추가

### 확인 완료 (문제없음)
- **get_positions 타입**: KR scheduler, data_collector 모두 KR broker(`Dict[str, Position]`) 사용. US broker와 혼용 없음
- **pykrx 동기 호출**: `supply_score.py`, `sector_momentum.py`, `swing_screener.py` 모두 `asyncio.to_thread()` 정상 래핑

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

### 2차 심층 리뷰 (P0×3 + P1×3 추가 수정)
- **P0**: on_fill에서 `update_position(fill)` 호출 추가 (체결 즉시 포트폴리오 갱신)
- **P0**: ExitManager에 float 대신 Decimal 전달 (TypeError 해결)
- **P0**: on_order에서 `event.order` 직접 사용 (order_type/strategy 보존)
- **P1**: 매수 체결 시 ExitManager 즉시 등록 (2분 지연 → 즉시)
- **P1**: 매도 체결 시 `_exit_pending_symbols` 즉시 해제 (3분 지연 → 즉시)

### 최종 검증 결과
| 흐름 | 상태 |
|------|------|
| KR 장중 스크리닝 → 매수 | PASS (ORDER 핸들러 + event.order) |
| KR 체결 확인 → 포지션 등록 | PASS (on_fill에서 update_position + ExitManager 등록) |
| KR 분할 익절/손절 → 매도 | PASS (update_price + Decimal) |
| US 스크리닝 → 매수 | PASS |
| US 청산 → 매도 | PASS |
| KR 배치 스캔 → T+1 실행 | PASS (ORDER 핸들러) |

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
