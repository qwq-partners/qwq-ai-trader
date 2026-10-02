# 시스템 아키텍처

> **40·41·42차:** 합성 socket/clock→기존 토스 수신/저장→엄격 재읽기로 자원을 검증한다. KIS 기존 오류5분기→선택 진단→기존 gap 한 번→v2 원장→strict report/가격입력 검증 순서이며 거래 분기는 바꾸지 않는다. 명시 계좌 JSON→시각/금액/완결 선언→입출금/비용 순손익→경계 NAV TWR→동일 기간/자본/흐름 비교는 별도 오프라인 경로다. [통합 코드/프로세스 흐름](../research/current-engine-capture-and-account-pds-2026-10-02.md). 운영 미적용, 경제값은 제공 자료에 조건부다.

> **39차 실제 진입 관측:** 명시 v3 buffer→scan 후보/참조 고정→scheduler의 실제 조건 결과를 동기식 trace에 복사→후보별 한 행을 기존 유한 journal로 전달→strict 오프라인 보고 순서다. trace 반환값은 매매 분기를 제어하지 않고 실패/종료/상한은 불명으로 남긴다. 기존 가격 조립기는 trace를 검증만 하며 baseline 승인으로 사용하지 않는다. [흐름·검증](../research/current-engine-integrated-pds-2026-10-02.md#39차-plan--선정에서-진입까지-실제-평가-근거).

> **38차 실제 관측 흐름:** 고정5468208 첫 scan9개→signal/order_ready0; KIS 상위3개 호가14,040개와 토스10,000프레임 조기 종료를 별도로 평가한다. 오프라인 보고는 예정 종료 뒤까지 기다린 것처럼 시각을 바꾸지 않고 알려진 조기 중단을 incomplete로 출력한다. 최종 엔진 봉인을 조기 토스 snapshot에 소급 삽입하지 않는다. [실제 흐름·검증·후속 설계](../research/current-engine-integrated-pds-2026-10-02.md#38차-plan--첫-실제-관측의-품질과-진입-병목).

> **37차(10-02):** 검증된 `Fill` identity → `TradeJournal`의 실행별 JSON → `TradeStorage`의 불변 transaction batch → DB commit receipt → Scheduler의 `handoff_returned`/broker ack 순서다. 청산 후처리는 메모리 적용 뒤 한 번 수행하고 DB 대기는 비차단 조회한다. 현재 session의 미완료 receipt를 신규 위험 보류에 반영하며, SELL 거래 ID는 해당 Fill의 매도 직전 보유 객체에서만 보강한다. [통합 흐름과 한계](../research/current-engine-integrated-pds-2026-10-02.md#37차-plan--체결-identity와-장부-commit). 이전 v1 원장의 receipt를 새 DB commit 근거로 소급 해석하지 않는다. 운영 미배포.

> **36차(10-02):** 실행 중 경로와 분리된 `read_execution_ledger`가 완결된 오프라인 SQLite를 검증한다. `reconcile_execution_evidence`와 명시 파일 CLI는 증권사 원주문·거래 이벤트·양쪽 잔고의 차이만 보고하며 broker/Engine/Scheduler에 복구 결과를 적용하지 않는다. [입력·출력·복구 경계](../operations/execution-recovery-evidence.md). 기존 운영/예약에는 미적용이다.

> **35차(10-02):** KR `KISBroker`가 계좌 범위 `ExecutionHistory/ExecutionLedger`를 기본 생성하고 connect/submit/check_fills 전에 session을 기록한다. POST 전 의도와 Fill 인계 전 누적 증분을 commit하고, Scheduler는 실제 적용 뒤 `portfolio_applied`, 후처리 반환 뒤 `handoff_returned`를 기록한다. `execution_id`는 Engine의 프로세스 내 중복도 막지만 과거 자동 replay 권한이 아니다. [통합 PDS와 흐름](../research/current-engine-integrated-pds-2026-10-02.md#35차-plan--영구-실행-원장과-재시작-경계) 참조. 운영 미배포.

> **30차(10-02):** `selection-basis-v2`에서만 원천 호출의 상태를 스캔에 기록한다. ContextVar로 병렬 호출을 분리하고 원천 오류/캐시와 후보 점수 근거의 시각·순위를 검증한다. 기존 v1·반환 후보·가격 계산은 유지한다. [설계·검증](../research/current-engine-source-status-2026-10-02.md), 운영/예약 미적용.

> **29차(10-02):** 새 `runner-first-scan-v2`의 명시 `selection_basis` 계약이 있을 때 스크리너 계산 근거를 수집한다. 스캔의 전체 반환 후보 뒤에 후보별 레코드를 쓰며, 첫 관측 이후/닫힌 버퍼에는 추가 근거 계산을 요청하지 않는다. 원천/통합 캐시와 반환값 소유권을 분리했다. [선정 근거 기록](../research/current-engine-selection-basis-2026-10-02.md) 참조. 운영 미배포·10월2일 예약 대상5468208 보존. 아래 단계별 표시는 당시 이력이다.

> **15차(10-01):** 선택적 `entry_observation_runtime`이 manifest 검증·원장/버퍼 설치·독립 종료 타이머를 소유한다. Runner는 엔진 태스크 시작 전 설치하며 중단 사유 고정과 원장 close를 feed 취소 전에 수행한다. 관측 종료는 봇 중지가 아니다. 기본 비활성·운영 미설치, 상세는 선정·진입 규약15차.

> **14차(2026-10-01) 준비 판정:** 아래 관측 부품을 실제 Runner에서 생성·설치하는 경로와 고정 종료/as_of의 close owner는 아직 없다. callback/owner 접점은 있으나 기본 None이다. 실제 수집에는 WS 시작 전 명시 설치, 연구/자원 설정 검증, 정상 종료 전 원장 봉인과 중도 incomplete 처리가 필요하다. [규약14차](../research/current-engine-selection-entry-protocol-2026-09-30.md) 참조. 이번 단계는 런타임 코드 변경 없음.

> **13차(2026-10-01) 추가:** 선택적 `scan_scope="first"`는 capture_scan 복사 전에 첫 시도를 예약하고 성공한 첫 후보/Signal ID만 관측에 연결한다. 후속 scan은 None을 반환하지만 원래 emit/주문 생성은 유지한다. 전역 WS/연결 공백은 그대로 보존한다. 원장과 두 입력 조립기가 first 모집단을 명시하며 혼합/중복 범위를 거부한다. 설치·자동 시간 종료·주문 없는 정책 재생 기능은 아니다. [규약13차](../research/current-engine-selection-entry-protocol-2026-09-30.md) 참조.

> **12차(2026-10-01) 추가:** 기존 buffer에 명시적으로 `ObservationJournal.open`을 붙인 경우에만 유한 큐→단일 전용 writer→새 JSONL 파일로 보존한다. `close`는 관측 구간을 닫고 잔여 기록을 저장한다. 피드/주문/서비스 생명주기를 조작하지 않는다. CLI가 원장을 strict read하여 기존 prepare_input으로 전달한다. 기존 None 모드는 파일·스레드 부작용이 없다. [규약12차](../research/current-engine-selection-entry-protocol-2026-09-30.md) 참조.


> **11차(2026-10-01) 추가:** 정지한 `KISWebSocketFeed`에 같은 `EntryObservationBuffer`와 명시 예산을 주입할 때만 채널 조정기를 설치한다. 스크리닝 → 유한 관측 작업 큐 → 기존 WS의 KRX BOOK → 기존 Runner Quote 콜백 → 버퍼 → 오프라인 가격 비교 순서다. 관측은 운영 watch/PRICE 소유권을 추가하지 않는다. 기본 None 경로는 유지한다. [규약 11차](../research/current-engine-selection-entry-protocol-2026-09-30.md) 참조.


> 2026-10-01 작업 브랜치 10차: 오프라인 비교기에 사전 고정 보유시간의 첫 bid 평가를 추가했다. 운영 청산 엔진을 호출하거나 후보 호가를 구독하지 않는다. 기존 WS subscribe는 PRICE+BOOK을 함께 보내므로 관측용 후보 추가에는 채널 소유권·ACK·재연결 조정이 선행돼야 한다. [규약 10차](../research/current-engine-selection-entry-protocol-2026-09-30.md) 참조.

> 소스 기준 최종 갱신: 2026-09-10
> 기준 진입점: [scripts/run_trader.py](../../scripts/run_trader.py), [src/core/engine.py](../../src/core/engine.py), [src/schedulers/kr_scheduler.py](../../src/schedulers/kr_scheduler.py), [src/schedulers/us_scheduler.py](../../src/schedulers/us_scheduler.py)

이 문서는 현재 실행 코드의 컴포넌트 경계와 데이터 흐름을 설명한다. 전략별 조건과 수치, 리스크 규칙, 운영 절차는 각각의 전용 문서에 두고 여기서는 시스템을 변경할 때 필요한 연결 관계와 실행 불변조건에 집중한다.

## 1. 핵심 구조

**2026-10-01 작업 브랜치 후속 — 기본 비활성 가격 관측 접점:** KRScheduler는 전체 반환 후보, 기존 REST 응답, live_screening SignalEvent/emit 결과를 `bot._entry_price_observer`에 복사한다. Runner의 Quote 콜백과 RiskManager의 최종 OrderEvent 생성 지점도 같은 선택적 버퍼에 연결된다. 요청 수량·사이징/초기 실효 손절을 구분하며 주문 접수/체결로 해석하지 않는다. 기본값은 None이고 생성/설치·영속 저장·추가 API/구독·정책 재생은 없다. 버퍼 유실은 평가 불명으로 남긴다. 가격 조건은 운영 주문에 적용하지 않는다. 별도 오프라인 수신 기준 모드는 원순서로 고가·첫 호가를 연결한다. 계약은 [선정·진입 규약 9차](../research/current-engine-selection-entry-protocol-2026-09-30.md) 참조.

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
- 포트폴리오 동기화는 주문 접수 시간대(08:00~15:40)·NXT 세션 30초, 장외 CLOSED·휴장일 300초 주기로 브로커 상태와 로컬 상태를 다시 맞춘다. 잔고·포지션은 같은 8434R 응답(헤더 tr_cont 로 마지막 페이지 판정)을 재사용해 1틱당 1회 호출한다(2026-09-15).
  장전 장외 대기는 거래일 08:00까지 남은 시간으로 상한을 둔다(2026-09-15 후속 수정, PR #58). I/O 종료 후 수면을 계산하므로 07:59부터 300초를 그대로 자는 공백을 막는다. 배포 상태는 `docs/reviews/codex-remediation-2026-09-15.md`를 참고한다.

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
| 포트폴리오 동기화 | 08:00~15:40·NXT 30초 / 장외 CLOSED·휴장일 300초 |
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

#### 레짐 입력 흐름 (2026-09-14 갱신)

`_run_llm_regime_classifier()`는 실행 시각에 따라 서로 다른 KR 지수 자료를 쓴다.

| 실행 | KR 지수 출처 | as_of |
|---|---|---|
| 08:10 (장 시작 전) | 아침 스크리너 벤치마크 종가열(`_screener._kospi_closes`)로 5일·20일 계산 | 스크리너 벤치마크 **로드 시각**(`_kospi_loaded_at`) |
| 08:10, 종가열 없음(재시작 직후) | 없음 — c5/c20을 **0.0%로 채우지 않고 결측** 처리 | `스크리너 캐시 없음` + `missing_fields`에 `KOSPI_c5`/`KOSPI_c20` |
| 12:00 (장중, 09:00~15:35 창) | `kis_market_data.fetch_index_price("0001"/"1001")` 재조회 → 당일 등락률 + 종가열에 당일 지수를 덧붙여 5일·20일 재계산 | 조회 시각 ISO8601 |
| 12:00, 벤치마크 마지막 봉이 이미 오늘(`_kospi_last_bar_date`) | 당일 지수를 **덧붙이지 않는다**(catch-up 스캔이 장중에 로드한 경우의 이중 계상 방지) | 조회 시각 ISO8601 |
| 12:00, 조회 실패 | 아침 캐시로 폴백 | `<로드 시각> — 장중 갱신 실패` + `missing_fields`에 `KOSPI당일` |

- 미국 지수는 공급자(`us_market_data`)의 **정규화 키**(`indices_normalized`의 `SP500`/`NASDAQ`/`SOX`/`VIX`, VIX는 `price`)를 우선 읽고, 없으면 표시명 별칭(`_index_field`)으로 폴백한다. `missing: true`이거나 값이 없으면 **None → 프롬프트 "결측"**이며 0으로 채우지 않는다. `us_as_of`는 `마감 <as_of>, 조회 <fetched_at>` 형태로 **체결 시각과 조회 시각을 구분**한다.
- 장중 급락 감지기 상태(`batch_analyzer._intraday_state`)가 프롬프트에 포함되지만, **당일 갱신(`_intraday_updated_at`)이 없으면 결측**으로 둔다 — 감지기는 일일 리셋이 없어 전일 상태가 남기 때문이다(`missing_fields`에 `급락감지기(당일 갱신 없음)`). as_of는 스냅샷 시각이 아니라 감지기 갱신 시각이다.
- `crash`/`severe`면 LLM이 `trending_bull`을 반환해도 저장 전에 `neutral`로 제한된다(`cap_regime_by_intraday_risk` 단일 출처 — 스케줄러 자체 캡 표는 제거). 원본은 `llm_regime_raw`·`confidence_raw`, 제한 사실은 `regime_capped: true`로 남아 대시보드·`_market_ctx`가 알 수 있다. 같은 급락 상태를 `_apply_regime_to_exit_manager()`의 충돌 방지 장치도 쓴다.
- 5분 급락 감지 루프는 `update_intraday_state()` 직후 `bot.engine._regime_adapter.set_intraday_risk(level, change_pct, as_of)`로 같은 상태를 **유효 레짐(§10.1)** 에도 전달한다(어댑터 부재 시 조용히 스킵, 등락률 결측은 `None`).
- 모든 입력의 `as_of`·`source`·`missing_fields`·`kospi_c5`/`kospi_c20`는 `llm_regime_today.json`의 `input_meta`에 남는다.

**급락 캡 3소스 병합 (2026-09-15, T10 F14)**: 급락 캡(`cap_regime_by_intraday_risk`가 아니라 레벨 결정 자체)은 서로 다른 시점의 세 소스 중 **가장 보수적인(위험이 큰) 레벨**로 정해진다 — ① 감지기의 당일 누적 상태(`_intraday_crash_snapshot()`), ② 이번 조회에서 막 계산한 실측(`kospi_today_pct` 기반), ③ 어댑터가 5분 루프로 이미 반영해 둔 당일 관측(`_adapter_intraday_snapshot()`). 공통 순수 함수 `market_regime.classify_intraday_level`(단일 등락률 → 레벨)과 `max_intraday_level`(여러 레벨 중 보수적인 쪽 선택)이 분류기(`_run_llm_regime_classifier`)와 30분 sync(`_apply_regime_to_exit_manager`) 양쪽에서 동일하게 쓰인다 — 한쪽만 최신 값을 반영하고 다른 쪽이 옛 값을 쓰면 그 사이에 완화된 레짐이 새어나간다(통합 커밋에서 발견된 F14 잔여 결함). 완화 방향으로는 덮어쓰지 않으며(보수적인 쪽이 항상 이김), 임계값(-1.5/-2.5/-3.5)은 불변이다.

**소비자 통일 (2026-09-15, T10 F15)**: 레짐을 ExitManager에 적용하는 경로는 `kr_scheduler._apply_regime_to_exit_manager()`(30분 sync) **하나**다. `batch_analyzer.monitor_positions()`(같은 30분 주기지만 별도 루프)는 과거에 캐시 원본 레짐을 직접 재적용해 방금 sync가 낮춘 값을 아침 강세로 되살리는 레이스가 있었다 — 해당 블록 제거. G2 크로스검증(`engine.RiskManager._resolve_market_regime`)도 2분 주기 `engine._market_regime` 복사본 대신 어댑터의 유효 레짐(`effective_regime`)을 우선 읽어 같은 종류의 지연을 없앴다(어댑터 부재 시에만 복사본으로 폴백).

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

## 10.1 시장체제 판단의 시간 범위 (2026-09-14~)

`MarketRegimeAdapter`(`src/core/market_regime.py`)는 서로 다른 시간 범위의 관점을 한 값으로 섞지 않는다. `RegimeHorizons`에 세 필드를 따로 둔다.

| 필드 | 의미 | 설정 | 유효 기간 |
|---|---|---|---|
| `mid_trend` | 5/20일 중기 추세 | `update_regime()` 결과(`_current_regime`) — **단일 출처, 오버라이드 세터 없음** | 다음 `update_regime()`까지 (기준시각 `_last_update`) |
| `open_expectation` | 장전 개장 예상 | `set_open_expectation()` · LLM 장전 진단이 자동 기록 | **09:30 만료**, 다음 날 무효 |
| `intraday_risk` | 장중 급락 상태(`normal`/`caution`/`crash`/`severe`)와 당일 등락률 | `set_intraday_risk()` — 급락 감지기(`batch_analyzer._intraday_state`) 상태를 전달 | 갱신까지 |

- 게이트·사이징이 읽는 **유효 레짐**은 `adapter.regime`(= `effective_regime`)이다. `intraday_risk`가 `crash`/`severe`이면 `mid_trend`가 `bull`이어도 강세로 취급하지 않는다(`bull → sideways`). 기존 VIX Fear 강등·레짐충돌가드와 같은 방향의 강등이며 **새 차단을 추가하지 않는다**. `caution`/`normal`, 강세가 아닌 레짐은 원본 그대로다.
- `mid_trend`에 별도 설정 경로를 두지 않는 이유: 아침에 한 번 써둔 값이 2분 주기 `update_regime()`·VIX Fear 강등·전문가 BEAR 합의·LLM `[방어]` 강등을 영구히 가려, 이 절이 막으려는 "오래된 강세가 실시간 판단을 덮는" 패턴을 그대로 재현하기 때문이다(2026-09-14 리뷰). 실시간 판단이 항상 이긴다.
- `open_expectation`은 어떤 경우에도 유효 레짐을 움직이지 않는다. 개장 30분 뒤에는 예상이 아니라 실측으로 판단한다.
- LLM 분류기 어휘(`trending_bull` 등)는 `cap_regime_by_intraday_risk(regime, intraday_risk)` 순수 함수로 같은 규칙을 적용한다(`trending_bull → neutral`). `REGIME_EXIT_PARAMS`·`apply_regime_params()`의 의미는 바뀌지 않았다.
- 결측은 0·중립으로 채우지 않는다. `get_summary()["horizons"]`는 값이 없으면 `value=None` + `missing=True` + 사유와 `as_of`를 함께 낸다. `mid_trend`는 `update_regime()`이 한 번도 돌지 않았으면(`_last_update is None`) 기본 `neutral` 값을 그대로 내되 `missing=True` + 사유를 붙인다. LLM 장전 진단 프롬프트의 지수 등락도 미수집이면 `+0.0%`가 아니라 `미수집`으로 넣는다.

배경: 2026-09-14 아침 자료(KOSPI 5일 +3.3%) 기준 강세 판단이 당일 -3.34% 급락 중 12:00에도 그대로 유효 레짐으로 쓰였다.

## 10.2 모닝브리프의 주장 범위와 사후 평가 (2026-09-14~)

- `DailyReportGenerator.build_morning_brief()`는 입력 자료의 범위를 `scope`로 고정한다. 미국 마감 자료뿐이면 `us_close_only`로 제목을 "미국시장 마감 요약"으로 제한하고, 프롬프트에서 한국장 개장 방향·갭·시가 대응 전략을 금지한 뒤 **응답 후 검사**(`sanitize_brief_claims`)로 남은 단정 문장을 `국내 자료 없음 — 개장 방향 판단 불가`로 대체한다.
- 기준시각(`as_of`)이 있는 국내 자료를 넘기면 `with_kr_inputs`가 되고 "개장 관찰 포인트"가 허용되지만 반대 근거를 함께 요구한다. `as_of` 없는 국내 자료는 유효 자료로 인정하지 않는다.
- 07:30 전문가 브리핑(`kr_scheduler._send_expert_briefing_telegram`)이 모닝브리프 캐시를 결합할 때 `build_expert_conflict_note(tone|text, {"score": agg})`를 호출해 상충 문구를 브리프 끝에 붙이고, `orchestrator.data_status_summary()`의 `자료 부족 N명`·커버리지 부족(`체제 점수 무보정`)을 전문가 종합 줄에 표시한다.
- 저녁 20:30 진화 잡이 `evaluate_morning_brief(day)`를 1일 1회 호출한다(120초 상한, 실패·미구현은 로그만). 결과는 `kr_evolution_scheduler` 하트비트 note로 남는다.
- 전문가 종합점수와 브리프 톤이 상충하면 본문 끝에 `⚠️ 전문가 종합판단(+2 중립)과 상충` 표시가 붙는다. `build_expert_conflict_note(tone_or_text, expert_consensus)`는 순수 함수로, 첫 인자에 레코드의 `tone` 또는 본문 `text`(톤 값이 아니면 `brief_tone()`으로 판정)를, 둘째 인자에 `{"score": int}` 또는 `{"bias": "bull"|"bear"|"neutral"}`을 받아 문구 또는 `None`을 돌려준다. 07:30 결합부(`kr_scheduler`)가 그대로 호출한다.
- 사후 검사·주장 추출은 **KR 주어 가드**를 쓴다. 문장에 `KOSPI/코스피/코스닥/국내/한국/오늘 장…`이 있고 개장·갭·출발 단정이 있으면 미국 근거가 같은 문장에 섞여 있어도 제거·대체 대상이다("미국 반도체 랠리를 반영해 오늘 KOSPI는 갭상승 출발이 예상된다"). KR 주어가 없는 미국 마감 문장은 보존한다. 문장 분리는 마침표 뒤에 공백·문장끝이 올 때만 끊어 `+0.97%`·`S&P500 +0.8%` 같은 소수점에서 본문이 훼손되지 않는다. `extract_brief_claims()`도 같은 가드를 써 미국 마감 서술(`S&P500은 상승 마감했다`)을 KOSPI 주장으로 원장에 기록하지 않는다.
- 캐시(`~/.cache/ai_trader/llm_morning_brief.json`)는 `us_date`/`kr_date`/`text`/`generated_at`/`model`/`scope`/`inputs`/`claims`/`removed_claims` 고정 스키마로 저장된다. `kr_date`는 평가 대상 KR 거래일이다. 07:30 전문가 브리핑은 기존대로 `text`·`generated_at`만 읽는다.
- 장후 평가: `DailyReportGenerator.evaluate_morning_brief(date)` → `src/analytics/morning_brief_eval.py`가 개장 방향·종가 방향·언급 업종 상대성과·전문가 종합 방향을 판정해 `~/.cache/ai_trader/morning_brief_eval.jsonl`에 누적한다. 주장이나 실측이 없으면 `hit=None` + 사유이며, 브리프의 `kr_date`가 평가일과 다르면(07:00 생성이 실패한 날) `evaluated=False` + 사유로만 기록해 **전날 브리프를 오늘 실측과 대조하지 않는다**. **하루 결과로 규칙·임계값을 바꾸지 않는다**(누적 표본은 `summarize()`).

**발송 스냅샷·날짜별 아카이브 (2026-09-15, T10 F19·F22)**: 위 평가는 07:00 생성본이 아니라 **실제로 발송된 내용**을 기준으로 해야 한다 — 07:00 생성 직후에는 전문가 종합판단(07:30 결합 시점에만 확정)이 없고, 최신 캐시(`llm_morning_brief.json`)는 계속 덮어써져 저녁 평가 시점에는 원문이 남지 않는다. `daily_report.record_morning_brief_dispatch(kr_date, sent_text, expert_consensus, status, conflict_note=None, archive_dir=None)`를 `kr_scheduler._send_expert_briefing_telegram()`의 07:30 **morning 슬롯**(`record_dispatch=True`) 발송 직후에만 호출해 `~/.cache/ai_trader/morning_brief/<kr_date>.json`에 append한다(`generated[]`: 원문·본문·제거문장·입력·모델·주장, `dispatch[]`: 시도별 status="sent"/"failed"). `evaluate_morning_brief`는 이 날짜별 아카이브의 발송 스냅샷을 우선 읽고(`dispatched`/`brief_ref`/`dispatch_reason`), 같은 날짜를 재평가하지 않으며, 원장 쓰기는 원자적(임시 파일 후 rename)이라 손상된 아카이브가 있어도 이전 내용을 잃지 않는다.

**테마 평가 대상 사전 고정 (2026-09-15, T10 F20)**: `extract_brief_claims()`가 만드는 `claims["sectors"]`는 테마명 문자열이 아니라 `{theme, eval_targets, agg, supported, reason}` 딕셔너리 목록이다 — `BRIEF_THEME_EVAL_TARGETS`(AI/반도체→전기전자, 바이오→의약품만 등재, 근거는 모듈 주석)에 없는 테마는 `supported=False`로 **생성 시점에** 미평가 확정한다. 저녁 평가가 실측 결과를 본 뒤 대상을 고르는 경로는 없다.

**07:00 고정 문구 범위 분리 (2026-09-15, T10 F21)**: LLM 프롬프트뿐 아니라 `generate_us_market_report()`의 고정 문구(`market_msg`, 정상/차트동반/폴백 3경로 공용)도 `brief_scope(kr_inputs)`(위 LLM 스코프 판정과 동일 함수) 기준으로 분기한다. `scope=us_close_only`이면 "한국 관련 테마주 갭업 가능성" 같은 한국 방향 단정 대신 "미국 지수 평균 …% 마감 (전일 미국 세션) — 국내 자료 없음, 한국 개장 방향 판단 보류" 형태로 발송한다.

**정오 당일 봉 교체 규칙 (2026-09-15, T10 F13)**: `_today_bar_action(last_bar, today)`가 스크리너 종가열의 마지막 봉과 오늘 날짜를 비교해 판정한다 — 마지막 봉이 오늘이면 **교체**(재계산에 이중 계상 없이 반영), 직전 거래일이면 **추가**, 그 외(날짜 미상·중간 결측)는 최신으로 위장하지 않고 그대로 유지 + 사유 기록. 현재 지수 as_of(`kr_as_of`)와 5/20일 봉 기반 지표의 as_of(`kospi_bars_as_of`)는 분리해 `input_meta`에 남긴다.

## 10.3 종목 단위 팀 심의의 근거·판단·진입계획 경계 (2026-09-15~, T11)

```
스크리너 → PendingSignal(=EntryPlan 정본: 가격 범위·상한·트리거·만료·손절/청산 참조·가정)
   ├─ 09:01 변환 → Signal.metadata["entry_plan"] → engine.on_signal → [shadow] check_entry_plan → signal_events(shadow_plan_check) → 주문(시장가, 불변)
   └─ 팀 심의(10:30/11:30/13:00/14:00, shadow) → Analyst×3(evidence 계약) → Bull/Bear(R1 독립 표 보존, R2 변경사유) → Trader/PM(기준선 불변)
                                                  └─ [shadow] judgment.assess(+ 같은 check_entry_plan) → TeamAssessment → team_ledger(append-only) + verdicts_*.json(호환)
```
- 플래그 `TEAM_ASSESSMENT_V2`·`ENTRY_PLAN_SHADOW`(기본 "1"): shadow 계산·기록만. "0" 이면 미호출. 어느 쪽도 주문·청산·사이징 결과를 바꾸지 않는다(기준선 특성화 테스트).
- 결측은 None + status 로 남긴다(관측 시각 모르면 None, 확률은 미보정). 상세 `docs/agents/trading-team.md` T11 절.

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

### 23차 토스 후보 관측 부품(2026-10-01, 로컬 미연결)

`src/data/providers/toss/orderbook_stream.py`는 외부에서 주입한 전용 WS의 수신/ACK/종료만 맡고, `src/analytics/toss_candidate_observation.py`는 기존 엔진 관측 export의 scan→signal→order_ready를 토스 전용 자료와 결합한다. 파일 CLI는 품질 보고만 출력한다. 토스 자료는 KIS ws_quote/EventBus/주문 경로에 발행하지 않는다. 현재 Runner·퇴역 토스 REST 서비스·접속/인증 팩터리·영속 원장과 연결되지 않았으며 실제 수집은 시작하지 않았다. 상세는 [선정·진입 규약23차](../research/current-engine-selection-entry-protocol-2026-09-30.md)를 따른다.

### 24차 토스 후보 수집 실행 경로(2026-10-01, 로컬 구현)

기존 봉인 launcher가 검증한 plan/grant 버전으로 REST(v1)와 후보 WS(v2)를 선택한다. `toss_ws_service`는 기존 승인·토큰 발급권·송신 잠금을 재사용하고 `ws_transport`의 고정 목적지에 한 번만 연결한다. 엔진은 `entry_anchor_input.project_anchors`를 통해 loopback 경로로 제한된 후보/판단 식별자만 제공한다. 이 HTTP 경로는 거래·가격·계좌 API를 호출하지 않는다.

수집 프로세스는 동기 저장을 봇 이벤트 처리와 분리하고 시작/최종 결과만 보존한다. 호가 중간값은 메모리에 있으므로 crash 후 복구되지 않는다. 강제 종료·불완전 원천·후보 변경을 실패 자료로 남긴다. `--artifact` 보고는 해시와 후보 연결을 검사하지만 손익/체결을 판정하지 않는다. 운영 설치/활성화 전이며 상세 승인·수명·한계는 [규약24차](../research/current-engine-selection-entry-protocol-2026-09-30.md) 참조.

### 25차 첫 후보 투영/시작 수명

`entry-anchor-projection-v2`는 전체 반환≤100개와 선택 규칙을 보존하고 앞3개 판단만 전달한다. 승인 plan의 선택 규칙과 report의 전체 분모를 함께 검사한다. Runner의 `--entry-observation-once`는 비공개 영수증을 배타 생성·동기화한 뒤 기존 CapturePlan을 읽는다. 이미 소비했거나 지정 구간 밖이면 일반 기동한다. 관측 성공을 뜻하는 영수증은 아니며 원래 주문 경로는 그대로다. [설치안](../operations/entry-capture-installation-2026-10-02.md)과 [규약25차](../research/current-engine-selection-entry-protocol-2026-09-30.md) 참조.
