# 시스템 아키텍처

> 소스 기준 최종 갱신: 2026-09-10
> 기준 진입점: [scripts/run_trader.py](../../scripts/run_trader.py), [src/core/engine.py](../../src/core/engine.py), [src/schedulers/kr_scheduler.py](../../src/schedulers/kr_scheduler.py), [src/schedulers/us_scheduler.py](../../src/schedulers/us_scheduler.py)

이 문서는 현재 실행 코드의 컴포넌트 경계와 데이터 흐름을 설명한다. 전략별 조건과 수치, 리스크 규칙, 운영 절차는 각각의 전용 문서에 두고 여기서는 시스템을 변경할 때 필요한 연결 관계와 실행 불변조건에 집중한다.

## 1. 핵심 구조

QWQ AI Trader는 하나의 프로세스와 하나의 asyncio 이벤트 루프에서 KR·US 시장, 대시보드, 실시간 피드를 함께 실행한다. 다만 두 시장의 주문 경로는 같지 않다.

- **KR은 이벤트 기반**이다. 시장 데이터와 배치 후보가 공용 UnifiedEngine 큐에 들어가 SIGNAL → ORDER → FILL 순으로 처리된다.
- **US는 스케줄러 직접 호출형**이다. USScheduler가 별도 USD 포트폴리오와 pending 주문 상태를 관리하며 브로커를 직접 호출한다.
- **MarketContext는 시장별 의존성 묶음**이다. KR과 US를 같은 방식으로 조회하게 해 주지만, 두 시장이 같은 거래 디스패치 경로를 쓴다는 뜻은 아니다.
- KRW 포트폴리오와 USD 포트폴리오는 분리된다. 양쪽 브로커는 KIS 토큰 관리자만 공유하며, REST 호출 제한은 KR 공용 리미터와 US 브로커 내부 리미터로 분리된다.

~~~text
scripts/run_trader.py
└─ main()
   ├─ AppConfig.load()
   ├─ 단일 프로세스 락
   └─ UnifiedTradingBot
      ├─ UnifiedEngine
      │  ├─ contexts["KR"] → MarketContext(KR)
      │  ├─ contexts["US"] → MarketContext(US)
      │  ├─ 우선순위 이벤트 큐 → 실질적인 KR 거래 경로
      │  └─ 대시보드 이벤트 버퍼
      │
      ├─ KR 런타임
      │  ├─ KISBroker + KRW Portfolio
      │  ├─ StrategyManager
      │  ├─ engine.py::RiskManager  → 이벤트 흐름 조정
      │  ├─ risk/manager.py::RiskManager → 거래 정책 검증
      │  ├─ ExitManager + BatchAnalyzer
      │  ├─ KRScheduler
      │  └─ KR WebSocket + REST 백업
      │
      ├─ US 런타임
      │  └─ _USEngineBundle
      │     ├─ KISUSBroker + 내부 호출 limiter + USD Portfolio
      │     ├─ 전략·스크리너·시장체제·RiskManager·ExitManager
      │     └─ USScheduler → 주문·체결 상태를 직접 관리
      │
      ├─ DashboardServer
      └─ 공유 연동
         ├─ KISTokenManager
         ├─ KR 프로세스 공용 KIS REST limiter
         ├─ LLMManager
         ├─ ExpertOrchestrator / TradingTeam
         └─ Telegram / 저장소 / HealthMonitor
~~~

## 2. 시작과 종료

### 시작 순서

1. main()이 CLI를 읽는다. 기본 시장은 both이며 kr, us, both와 dry-run을 지원한다.
2. 날짜별 로그 디렉터리와 로거를 준비한다.
3. AppConfig.load()가 환경 변수와 YAML 설정을 읽고 실효 설정을 만든다.
4. 단일 프로세스 락을 획득하고 UnifiedTradingBot을 생성한다.
5. UnifiedTradingBot이 KR 기준 UnifiedEngine을 만든다.
6. initialize()가 공용 KISTokenManager를 만든 뒤 선택된 시장을 초기화한다.
7. KR 초기화는 브로커, 실계좌 상태, 전략, 두 RiskManager, ExitManager, 스크리너·배치·모니터를 만들고 KR MarketContext를 등록한다.
8. US 초기화는 독립 USD Portfolio를 가진 _USEngineBundle을 만들고 US MarketContext를 등록한다.
9. 설정이 활성화되어 있으면 단일 aiohttp DashboardServer를 준비한다.

### 실행 순서

UnifiedTradingBot.run()은 같은 asyncio 루프에 다음 태스크를 올린다.

- UnifiedEngine.run()
- 선택된 시장의 KRScheduler 태스크
- 선택된 시장의 USScheduler 태스크
- DashboardServer
- 실거래 모드의 KR WebSocket 피드

종료 신호는 초기화 중에도 기록된다. 실행 중 신호를 받으면 전체 태스크를 취소하고 최대 15초 동안 정리를 기다린다. 이후 shutdown()이 KR·US 브로커, 대시보드, KR WebSocket, 공용 토큰 관리자를 명시적으로 닫는다.

## 3. 공용 엔진과 이벤트 모델

### 이벤트 큐

UnifiedEngine은 기본 상한 1,000개의 공유 우선순위 힙을 가진다. 같은 우선순위에서는 발생 시각 순으로 처리하며, 중요 거래 이벤트 보존이 큐 상한보다 우선한다.

| 범주 | 이벤트 |
|---|---|
| 시장 데이터 | MARKET_DATA, QUOTE, TICK |
| 거래 | SIGNAL, ORDER, FILL, POSITION |
| 리스크 | RISK_ALERT, STOP_TRIGGERED |
| 정보 | THEME, NEWS |
| 시스템 | HEARTBEAT, SESSION, ERROR, LOG |

ORDER, FILL, RISK_ALERT, STOP_TRIGGERED, ERROR는 우선순위 1이다. 큐가 포화되면 낮은 우선순위 이벤트부터 정리하되 ORDER와 FILL은 보존하고, 가득 찬 상태에서도 두 이벤트는 강제로 넣는다.

### 동명 RiskManager의 역할

코드에는 이름이 같은 두 클래스가 있으며 역할이 다르다.

| 구현 | 역할 |
|---|---|
| src/core/engine.py의 RiskManager | SIGNAL·ORDER·FILL 이벤트 조정, 크로스 검증, pending·예약 현금, 거래 메모리와 Wiki 연결 |
| src/risk/manager.py의 RiskManager | 일일 손실, 포지션 수, 현금, 섹터, 재진입 등 거래 정책 검증 |

새 검증 규칙은 어느 계층의 책임인지 먼저 구분해야 한다. 이벤트의 수명주기나 예약 상태를 다루면 전자, 거래 허용 정책이면 후자가 기준이다.

## 4. KR 거래 경로

### 진입 경로

KR에는 실시간 경로와 배치 경로가 있으며 둘 다 최종적으로 같은 엔진 주문 흐름에 합류한다.

~~~text
실시간
KR WebSocket / REST / 장중 스크리너
  → MarketDataEvent 또는 SignalEvent
  → StrategyManager
  ┐
  │
배치
SwingScreener + BatchAnalyzer
  → pending_signals.json
  → execute_pending_signals()
  ┘
        ↓
SignalEvent
  → engine.py::RiskManager.on_signal()
     ├─ 중복·쿨다운·기보유 검사
     ├─ CrossStrategyValidator
     ├─ 조건부 LLM 보조 검증
     ├─ risk/manager.py 정책 검사
     ├─ 전략 예산·가용 현금·포지션 크기 계산
     └─ pending 및 예약 현금 등록
        ↓
OrderEvent
  → on_order()
  → KISBroker.submit_order()
        ↓
KRScheduler.run_fill_check()
  → FillEvent
  → on_fill()
  → KRW Portfolio, pending, 예약 현금 갱신
~~~

배치 분석기는 후보와 지표를 파일에 보존했다가 실행 시점에 현재가와 진입 조건을 다시 확인한다. 당일 실행 플래그도 파일로 남겨 재시작 후 중복 실행을 막는다.

### 청산 경로

보유 종목 가격은 KR WebSocket이 우선 전달하고 REST 피드가 미구독·비연결 구간을 보완한다. 두 입력 모두 KRScheduler의 청산 검사로 연결된다.

~~~text
가격 갱신
  → ExitManager.update_price()
  → 부분 또는 전량 매도 판단
  → SELL SignalEvent
  → 기존 SIGNAL → ORDER → FILL 경로 재사용
  → 포트폴리오·ExitManager·저널·재진입 상태 갱신
~~~

자동 청산은 별도 브로커 바로가기를 만들지 않고 정상 엔진 경로를 재사용한다. 부분 매도 수량과 청산 사유는 pending 상태에 먼저 기록해 중복 매도와 체결 후 사유 유실을 줄인다.

### KR 동시성 불변조건

- pending lock은 초기 중복·쿨다운 확인과, 주문 생성 직전의 TOCTOU 재확인·pending 등록 구간을 각각 보호한다. 검증과 사이징 전체를 잠그지는 않는다.
- 미체결 주문의 예약 현금과 전략별 pending 명목금도 다음 주문의 예산 계산에 포함한다.
- 주문 접수 요청은 응답 유실 시 중복 주문이 될 수 있으므로 자동 재전송하지 않는다.
- 체결 폴링은 미체결 주문이 있으면 2초, 없으면 15초 간격으로 전환한다.
- 포트폴리오 동기화는 30초 주기로 브로커 상태와 로컬 상태를 다시 맞춘다.

전략과 청산의 세부 조건은 [KR 전략](../strategies/kr-strategies.md)과 [리스크·청산](../risk/risk-and-exit.md)을 참고한다.

## 5. US 거래 경로

US는 UnifiedEngine의 이벤트 큐에 컨텍스트를 등록하지만 실제 주문과 체결 반영은 _USEngineBundle과 USScheduler가 직접 수행한다.

~~~text
screening_loop()
  → 유니버스·Finviz·전략 평가
  → 시장체제 보정 및 점수 정렬
  → _process_signal()
     ├─ 가격·쿨다운·당일 재진입 검사
     ├─ CrossStrategyValidator
     ├─ 리스크 검사와 USD 포지션 크기 계산
     └─ KISUSBroker 매수 주문
        ↓
_pending_orders / _pending_symbols
  → order_check_loop()
  → 체결 확인
  → USD Portfolio·ExitManager·저널 갱신
  → portfolio_sync_loop()가 브로커 잔고와 재정합
~~~

청산은 가격 WebSocket 콜백과 REST 폴링이 ExitManager를 호출한 뒤 USScheduler가 브로커 매도 API를 직접 호출한다. WebSocket 체결 통보는 빠른 알림 경로이고, 주문 상태 폴링과 잔고 동기화가 상태 정합의 기준이다.

US 가격 WebSocket은 미국 장 시작 10분 전부터 연결할 수 있다. 장 종료 30분 뒤 포지션이 없으면 종료하며, KIS approval key 충돌을 피하기 위해 KR 정규장 시간에는 남은 US 연결도 강제로 닫는다. 연결 중에는 REST 청산 검사를 60초 백업 주기로 낮추고, 연결되지 않았으면 15초, 비정규장에서는 180초 주기로 검사한다.

US의 lock 범위는 제한적이다. 종목별 exit lock은 WebSocket과 REST의 청산 판단을 직렬화하고, portfolio lock은 잔고 동기화 중 KIS에 없는 로컬 포지션을 정리하는 구간을 보호한다. order_check_loop()의 체결 반영은 이 두 lock 밖에서 실행되며 pending 집합과 이후 잔고 동기화로 정합을 맞춘다.

전략 세부 사항은 [US 전략](../strategies/us-strategies.md)을 참고한다.

## 6. 설정 해석

### 일반 병합 순서

실효 설정은 한 파일만 보고 판단하면 안 된다.

~~~text
config/default.yml
  → config/evolved_overrides.yml deep merge
  → AppConfig.raw
  → KR TradingConfig + 시장별 컴포넌트 설정
~~~

evolved_overrides.yml의 특수 매핑은 다음과 같다.

| override 최상위 키 | 반영 위치 |
|---|---|
| risk_config | risk와 kr.risk |
| exit_manager | exit_manager와 kr.exit_manager |
| batch | 전역 batch |
| 전략 컴포넌트 | strategies와, 같은 컴포넌트가 존재할 경우 kr.strategies |

### 배치 설정의 추가 병합

KRScheduler.run_batch_scheduler()는 AppConfig 병합이 끝난 뒤 다음 순서로 한 번 더 합친다.

~~~text
실효 전역 batch
  → kr.batch가 같은 키를 덮어씀
~~~

따라서 현재 저장소 기본값에서 아침 스캔은 08:20, 첫 실행은 09:30이다. 전역 override에만 있는 점심 스캔은 13:30이다. 코드 주석이나 함수의 fallback 값보다 이 병합 결과가 우선한다.

### 현재 저장소 기본 프로필

실제 실행 여부는 CLI 시장 선택과 환경 설정에 따라 달라지며, 아래는 추적된 YAML을 병합한 기본 프로필이다.

- KR 자동 진입의 주요 활성 경로: gap_and_go, sepa_trend, VCP, core_holding
- KR 비활성 전략: rsi2_reversal, theme_chasing, momentum_breakout
- KR 통합·폐지 경로: strategic_swing 독립 배분은 0이며 조건 일부가 SEPA conviction 보조 신호로 흡수됨
- KR 관측 전용: value-growth core shadow, asymmetric harvest shadow 및 기타 shadow 게이트
- US 활성 전략: momentum, SEPA
- US 비활성 전략: earnings_drift, earnings_reversal
- US 스크리닝: 15분, 회당 최대 100종목·3개 신호, 종목 쿨다운 300초

현재 수치가 필요한 코드나 문서를 바꿀 때는 반드시 default.yml과 evolved_overrides.yml을 함께 검토한다.

## 7. 스케줄러

모든 시간은 별도 표기가 없으면 KST다. 미국 장 관련 시각은 USSession이 ET 기준으로 판단한다.

### KR 상시·주기 태스크

KRScheduler.create_tasks()가 기능 존재 여부와 설정에 따라 태스크를 만든다. 현재 주요 주기는 다음과 같다.

| 태스크 | 현재 동작 |
|---|---|
| 실시간 스크리닝 | 장중 5분 |
| 체결 확인 | 미체결 있음 2초 / 없음 15초 |
| 포트폴리오 동기화 | 30초 |
| 보유 종목 REST 가격 백업 | 20초 |
| 시장 추세·체제 | 2분 |
| pending 정리 | 60초 |
| 수급 캐시 | 장중 30분 |
| 테마 탐지 | 설정 기본 10분 |
| 포지션 모니터 | 09:30~15:20, 설정 기본 10분 |
| 보유 종목 공시 경보 | 08:00~16:00, 10분 |

핵심 장기 루프 일부는 _supervised()로 감싼다. 예상치 못하게 종료되면 60초부터 최대 30분까지 지수 백오프로 재시작한다. 포트폴리오 동기화, pending 정리, 수급 캐시, core·shadow 작업, 수동 주문, 전문가 브리핑 등은 이 공용 래퍼 밖에서 실행되므로 “모든 KR 태스크가 supervisor로 복구된다”고 가정하면 안 된다.

### KR 일일·주간 시계

| 시각 | 작업 |
|---|---|
| 07:30 | 평일 전문가 장전 브리핑 |
| 08:10 | LLM 시장체제 분류 |
| 08:15 | 배치 전략 사전분석 |
| 08:20 | 아침 배치 스캔 |
| 08:25 | 아침 리포트 |
| 08:30 | 변동성 타게팅 캐시 |
| 08:40 | asymmetric harvest shadow |
| 09:30 | pending 신호 실행 시작 |
| 10:30 / 11:30 / 13:00 / 14:00 | 종목 심의팀 |
| 12:00 | 장중 시장체제 재분류 |
| 13:00 | 평일 전문가 장중 브리핑 |
| 13:30 | 점심 재스캔 및 즉시 실행 |
| 13:50 | 자본 활용률 검사 |
| 15:00 | 보유 포지션 LLM 종가 점검 |
| 15:40 / 20:40 | 일봉 캐시 갱신 |
| 16:00 | 저녁 리포트 |
| 16:30 | 평일 전문가 장후 브리핑 |
| 18:00 | 종목 마스터 갱신 |
| 20:30 | 일일 진화 리뷰 |

morning_scan_enabled가 false인 대체 모드에서는 15:35 사전분석 → 15:40 일일 스캔 → 19:30 저녁 보정 스캔을 사용하고, 다음 거래일 실행 시간은 설정값을 따른다.

주간 작업에는 토요일 00:00 예산 리밸런싱, 토요일 09:30~09:44 매도 후 복기, 일요일 21:00 전문가 패널이 있다. KR 전문가 정기 브리핑에는 일요일 22:00과 월요일 06:00 슬롯도 있다. core 리밸런싱과 value-growth shadow는 각각의 주기·중복 방지 상태를 별도로 관리한다.

### US 태스크

USScheduler.create_tasks()는 기본 6개 루프에 데이터 소스와 기능 설정에 따른 태스크를 추가한다.

| 태스크 | 현재 동작 |
|---|---|
| screening_loop | 정규장, 설정 기본 15분 |
| exit_check_loop | WS 없음 15초 / WS 연결 60초 백업 / 비정규장 180초 |
| portfolio_sync_loop | 정규장 30초 / 비정규장 5분 |
| order_check_loop | 10초 |
| eod_close_loop | 30초 확인, DAY 포지션은 마감 15분 전 청산 |
| heartbeat_loop | 5분, SPY·QQQ 시장체제도 갱신 |
| screener_loop | 60분 |
| watchlist_loop | 5분 |
| ws_market_loop | 30초마다 연결 수명주기 확인 |
| volume_surge_loop | 15분 |
| theme_detection_loop | 30분 |
| us_expert_loop | 21:30 / 01:30 / 06:00 KST 고정 슬롯, 전문가 시스템 활성 시 |

US 장후 일일 리포트는 16:10 ET 이후, KIS 체결 동기화는 16:20 ET 이후 하루 한 번 실행한다. US에는 KR의 _supervised()와 같은 공용 재시작 래퍼가 없고 각 루프가 자체 예외 처리를 수행한다.

## 8. 외부 연동과 상태

### KIS

- KR·US 브로커는 하나의 KISTokenManager를 공유한다.
- KR 브로커, KR 시장 데이터, KR 스크리너가 같은 앱 키를 사용하므로 이들의 REST 호출은 프로세스 공용 리미터를 거친다.
- KR 공용 페이스는 초당 최대 10건이며 호출 간 최소 간격을 둔다.
- 계좌 원장 TR은 응답 완료 뒤 직렬화한다. 일반 원장 간격은 1.05초이고 TTTC8434R 잔고 조회는 2.1초다.
- US 브로커는 별도의 인스턴스 내부 리미터를 사용하며 현재 상한은 초당 18건이다.
- KR의 접수·정정 같은 비멱등 주문 POST는 자동 재전송하지 않는다.

자세한 API별 책임은 [외부 API](../integrations/external-apis.md)를 참고한다.

### 저장 위치

| 위치 | 성격 |
|---|---|
| config/default.yml | 추적되는 기본 설정 |
| config/evolved_overrides.yml | 추적되는 진화·수동 override |
| logs/YYYYMMDD/ | 실행 로그 |
| ~/.cache/ai_trader/ | KR pending, 저널, Wiki, 진화, 전문가, 감사·중복 방지 상태 |
| ~/.cache/ai_trader_us/ | US 잔고·스크리너·청산 및 재진입 상태 |
| PostgreSQL | DATABASE_URL이 있을 때 거래·분석 데이터 저장 |

TradeStorage는 PostgreSQL 쓰기를 비동기 큐로 처리하면서 JSON 저널을 백업으로 유지한다. DATABASE_URL이 없으면 JSON 전용 모드로 동작한다. 캐시와 실행 상태는 재시작 정합성과 중복 방지에 사용되므로 단순 임시 파일로 취급하면 안 된다.

## 9. 대시보드와 관측

DashboardServer는 단일 aiohttp 애플리케이션이다.

| 경로 | 역할 |
|---|---|
| /api/* | KR 상태·거래·분석 API |
| /api/us/* | US 상태 API |
| /api/engine/* | 엔진·에이전트 상태 |
| /api/system/* | 시스템 리소스 |
| /api/office/* | 가상 오피스 상태 브리지 |
| /api/stream | KR·US 통합 SSE |
| /, /trades, /performance, /themes, /settings, /evolution, /engine, /office, /principles | HTML 화면 |
| /equity, /settlement | 각각 /performance, /trades로 리다이렉트 |

DashboardDataCollector가 KR 런타임을 API 표현으로 바꾸고 SSEManager가 주기적으로 클라이언트에 상태를 보낸다. HealthMonitor의 활성 검사는 장중 critical 15초와 important 60초 주기이며 실패 시 알림과 SSE 이벤트를 만든다. 5분 periodic 훅은 현재 빈 결과를 반환한다.

## 10. 전문가·학습·shadow 경계

- ExpertOrchestrator는 현재 등록된 9개 전문가를 병렬 실행하고 결과를 파일에 저장한다. sector council은 종목 판단용이며 시장체제 합산에서는 제외된다.
- TradingTeam은 결정론적 분석 → LLM 찬반 토론 → Trader 제안 → 리스크 게이트 → PM 결정 순서로 종목을 심의한다.
- 심의 결과는 주문을 직접 내지 않는다. 승인된 고확신 BUY만 기존 엔진 사이징의 보조 입력으로 사용된다.
- TradeMemory와 Trade Wiki는 체결·청산 결과를 다음 검증과 리뷰의 문맥으로 되돌린다.
- 진화 후보는 백테스트 게이트를 통과해야 설정 반영 대상이 된다.
- Counterfactual과 Shadow Lab, value-growth core, asymmetric harvest는 관측·리포트 경로이며 주문 경로와 분리된다. 승격 표시만으로 자동 활성화되지 않는다.

세부 학습 흐름과 승격 기준은 [진화 시스템](../evolution/evolution-system.md)을 참고한다.

## 11. 변경 시 확인할 경계

| 변경 종류 | 함께 확인할 파일 |
|---|---|
| 시작·시장 초기화·공유 의존성 | scripts/run_trader.py, src/core/market_context.py |
| KR 이벤트 수명주기 | src/core/event.py, src/core/engine.py |
| KR 시간·반복 작업 | src/schedulers/kr_scheduler.py |
| US 주문·체결·시간 작업 | src/schedulers/us_scheduler.py |
| 실효 설정 | config/default.yml, config/evolved_overrides.yml, src/utils/config.py |
| 배치 시각·pending 처리 | src/core/batch_analyzer.py, KRScheduler.run_batch_scheduler() |
| 청산 상태 | src/strategies/exit_manager.py와 각 시장 스케줄러 |
| KIS 공유 제약 | src/utils/token_manager.py, src/utils/kis_rate_limit.py, KR·US 브로커 |
| 대시보드 계약 | src/dashboard/server.py와 kr_api.py, us_api.py, sse.py |
| 영속 상태 | src/data/storage/, src/core/evolution/, src/analytics/ |

아키텍처를 변경했다면 이 문서와 함께 [문서 인덱스](../README.md), [운영 런북](../operations/runbook.md), 관련 전략·리스크 문서를 검토한다. 스케줄 시각은 코드 주석이 아니라 실효 설정과 실제 조건문을 기준으로 갱신한다.
