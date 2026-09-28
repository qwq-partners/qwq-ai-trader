# 엔진 본체 최소 설계 — 단일 runtime owner → KIS 주문 경계 → Toss 조회 보강 (설계 B)

> **상태 (2026-09-28):** 서면 설계만 있다. 구현·main 병합·배포·재시작·실주문·설정 변경은 없다. 승인 범위는
> 작성자가 아닌 리뷰어의 **설계 승인**까지다. §14 의 사용자 확인 항목은 리뷰 승인과 별개다.
> 기준: `feature/owner-ticket-gate-20260926` `426743f`(이하 W, engine `feature/engine-safety-design-20260917` 포함),
> main `e31c632`. 작성: Claude coordinator(세션 모델 Fable 5.1 — 라우팅 정책상 미자격이라 작성자 기록으로만 남긴다).
> 인용 표기: **확인** = coordinator 가 원문·코드를 직접 읽음, **조사** = 읽기 전용 조사 작업자(요청 opus/high) 보고.

## 1. 목적과 완료 조건

`docs/operations/claude-handoff-b1-runner-2026-09-28.md` §8 의 표 전부가 아니라, 두 가지를 만족하는 데 필요한 만큼만 다룬다.

1. **계측 무결성:** 모든 KR 체결이 writer 하나를 거쳐 DB `trades`/`trade_events` 에 한 번 기록된다. 이것이
   설계 A(main `docs/superpowers/specs/2026-09-28-kodex200-excess-return-ledger-design.md`)가 읽는 원천이다.
2. **KIS 한 경로로 안전한 실거래:** KR 주문 POST 는 모두 한 함수(킬스위치·감사·무재전송)를 지난다. 포지션·현금·
   미체결을 쓰는 주체도 하나다. 현행 위험 한도(`config/*.yml`)는 그대로 둔다.

Toss 는 조회·관측 보강만 맡는다. 주문·잔고·계좌는 KIS 만 쓴다(사용자 09-28 재확인). US 는 영구 중단이라 제외한다.

## 2. 출발점 — 지금 무엇이 있나

### 2-1. main(운영, legacy) — 조사, 핵심 3건 확인

| 항목 | 사실 |
| --- | --- |
| 주문 송신 | 모두 `KISBroker`(킬스위치 `kis_kr.py:461`)를 지나지만 부르는 곳은 7곳이다. 엔진 `on_signal` 가드를 지나는 곳은 `engine.py:2531` 하나다. 나머지는 `_fallback_stale_sell`(`engine.py:2318`), `_cleanup_stale_pending`(`kr_scheduler.py:918·977`), 자정 전건 취소(`:5292`), 수동 매수(`:7298`, **확인**), CLI 2개(별도 프로세스·별도 `KISBroker`)다. `modify_order` 호출자는 0건이다(**확인**) |
| 포지션·현금 writer | 엔진 체결 처리(`engine.py:544-683`)와 30초 `_sync_portfolio`(`kr_scheduler.py:1387·1412·1462·1476`)가 경쟁한다. 그 밖에 재시작 로더 2곳과 일일 리셋·복원 3곳이 있다 |
| 체결 유입 | `run_fill_check` 는 브로커 인메모리 추적이 비어 있지 않을 때만 `check_fills()` 를 부른다(`kr_scheduler.py:2823`, **확인**). 재시작·CLI·취소 직전 부분 체결·`TEMP_` 주문은 동기화로만 반영된다. DB 에는 저녁 17:00 `sync_from_kis` 가 `kis_sync` 로 쓴다 |
| 미체결 장부 | 엔진 pending(종목 키)·스케줄러 청산 pending·ExitManager pending_stage·브로커 추적(주문 id)·거래소 조회, 모두 5개 |
| 주문 유형 | SELL = 매수1호가 지정가(호가가 없으면 시장가), BUY = 시장가(`engine.py:2038-2072`, **확인**). 수동 매수는 현재가 +0.6% 지정가 |
| 안전자산(KOFR) | PR #97 로 삭제됐다(`336e979`) |

### 2-2. W(attach) — 조사, 핵심 확인

- `src/execution/safety/` 는 17,933줄이다. 제품 호출자는 0건이고 main 병합도 0건이다. `trading_ready` 는 상수 `False`
  (`runtime.py:172-175`, **확인**)이고, 설치기 `install_attached_runtime` 은 "운영에서는 항상 거부"(`factory.py:225-236`, **확인**)다.
- 주문 안전 핵심 부품: 저장소 `store.py`, 계좌 lease `account_lease.py`, 수명주기 `lifecycle.py`, 증거 `evidence.py`/`queries.py`,
  체결 적용 `application.py`/`economics.py`, 명령 `commands.py`, guard `guards.py`, 송신 `transport.py`(킬스위치·감사, **확인**),
  보호 생산자 `protection_producer.py`, 경제 outbox `journal_delivery.py`.
- 보호 SELL 은 정규장 MARKET, 마감 LIMIT, 취소 없음이다(P1 결정, `docs/reviews/p1-producer-wiring-2026-09-23.md`, **확인**).
  마감 구간 MARKET 은 요청 단계에서 거부된다(`requests.py:245`, **확인**).
- KIS 미확정 31문항은 판단으로 닫았다(D1~D10, `docs/superpowers/plans/2026-09-22-kis-judgement-decisions.md`, **확인**).
  사용자 결정 "엔진 전체를 옮기도록 하자"(같은 문서 §6)가 attach 전환의 근거다.
- 설치 차단 사유 1~31(`docs/operations/claude-migration-handoff-2026-09-20.md`, **확인**) 중 다수가 열려 있다. 그중 여럿은
  KIS 의 제약이 아니라 **attach 스스로 legacy 보다 엄격하게 둔 규칙**에서 생겼다: UNKNOWN 한 건의 전역 정지(12),
  체결 적용 창의 전역 정지(19), 만료를 최종성으로 읽지 않음(22), 보호 실패 영구 래치(23), 정책 이중 평가(9·11·13·15·20).
- W 끝의 125커밋은 대부분 검증 도구다(B1 runner, OS evidence, L3 proof, decoder, N4097, native qualification — 조사).

## 3. 원칙

1. **브로커가 강제하는 불변식을 뼈대로 쓴다**(09-22 사용자 지시).
   - I1 **매도가능수량을 넘는 매도는 KIS 가 거절한다.** 거절은 안전망이 아니라 "보호 실패" 신호다(D1).
   - I2 **당일 주문은 그 세션일에 소멸한다.** 20:00(NXT 종료) 이후에는 살아 있는 주문이 없다. 예약주문 API 는 쓰지 않는다.
   - 현금을 넘는 매수의 거절은 불변식으로 쓰지 않는다. 미수 허용 계좌라면 거절되지 않기 때문이다.
2. **잠기는 쪽이 조용히 틀리는 쪽보다 싸다**(판단 결정 문서 §2). 다만 잠금 범위는 **원인이 있는 종목**으로 한정하고,
   전역 정지는 원인이 전역일 때만 둔다.
3. **장벽을 더하지 않고 규칙을 빼서 단순하게 만든다**(09-28 사용자 지시: 과도한 방어 로직 지양). 취소·정정을 보내지
   않으면 취소 최종성 문제(Q7~12)가 아예 생기지 않는다.
4. **주문 판단은 한 곳, 돈·수량 불변식도 한 곳.** 진입 판단은 지금 운영과 같은 sidecar(`risk/manager.py`·교차검증·엔진
   RiskManager)가 한다. owner 는 in-flight 를 아는 유일한 곳이므로 **용량**만 다시 본다.

## 4. 결정 (D11~D17 — 기존 D1~D10 위에 더한다)

| # | 결정 | 기존 결정과의 관계 |
| --- | --- | --- |
| **D11** | **주문 유형 고정:** 정규장 SELL = MARKET, 마감 동시호가(15:20~15:30) SELL = 현재가 LIMIT, 정규장 BUY = MARKET(legacy 와 같음), 수동 BUY = LIMIT. 그 밖의 세션(장전·NXT·시간외)에는 주문을 내지 않는다. attach 는 **CANCEL·MODIFY 를 송신하지 않는다** | D2(attach 취소 없음)를 v1 에서 영구로 한다. P1 의 보호 SELL 규칙을 gateway 를 거치는 모든 SELL 로 넓힌다 |
| **D12** | **당일 소멸 채택:** 차가운 시작(§6)에서 전 거래일 이전의 비terminal attempt 는 그 날짜의 일별체결조회(TTTC0081R)로 체결을 적용한 뒤 `EXPIRED` 로 닫고 예약을 푼다. 만료의 근거는 I2 다 | **D3 을 뒤집는다**(만료로 예약을 풀지 않음 → 푼다). 차단 22·D6 을 함께 닫는다. 사용자 확인 필요(§14) |
| **D13** | **미상 주문 한 규칙:** UNKNOWN ACK, 따뜻한 시작에서 본 거래소 미체결, 미적용 체결 관측, 보호 task 실패는 모두 **그 종목만 잠근다**(새 SUBMIT 거부, 예약 유지). 해제는 증거 적용 또는 D12 의 당일 소멸로 한다. 전역으로 막는 것은 ① 킬스위치 ② 체결 생산자 정지 시 BUY ③ 현금 불일치 시 BUY, 이 셋뿐이다 | D4(범위 축소)를 `blocked_unknown` 너머로 넓힌다. 차단 12·19·23 의 전역성을 없앤다. D5(runbook)는 유지 |
| **D14** | **시작 술어:** 차가운 시작(20:00~08:00)과 따뜻한 시작(장중 재시작) 두 절차(§6)로 `trading_ready` 를 연다. 술어가 실패하면 그 프로세스는 **legacy 모드로 기동하고 경보한다** — 보호 공백을 만들지 않는다 | D6 을 구체화한다. D7(보호 전용 모드)은 만들지 않는다 — D13 이 같은 효과를 낸다 |
| **D15** | **owner 가 유일 writer, sync 는 관측과 보정 제안:** 30초 동기화가 KIS 잔고와 owner 게시본의 불일치를 보면, 그 종목에 비terminal attempt·미적용 관측이 없고 **연속 2회** 같은 불일치일 때 owner 가 브로커 수량을 채택한다. 채택은 보정 이벤트로 기록한다(DB 에는 `kis_sync` 유형). 현금도 같은 규칙이되 진행 중인 BUY 가 없을 때만 채택한다 | P0-3(읽기 전용 관측·알람만)에 학습 경로를 더한다. D1 처분 ①(사용자 수동 매도를 배울 경로 없음)을 닫는다 |
| **D16** | **owner 정책 검사는 용량 축만:** 현금 예약, 최대 포지션 수(대기 BUY 포함), 일일 신규 매수 수, 섹터 한도만 본다. 출처는 로드된 `RiskConfig`(+`evolved_overrides` 머지) 하나다(factory 의 정책 생성 함수). 결정 사실의 generation/seal 재검증(`commands._decision_facts`·`qualification`·`risk_input_seal`·레짐/패널/trade_memory 축)은 v1 게이트에서 뺀다 | W 의 B2/B3 request-bound qualification 을 v1 설치 경로에서 뺀다(코드는 남기되 게이트로 쓰지 않는다). 차단 9·11·13·15·20 을 닫는다. 사용자 확인 필요(§14) |
| **D17** | **계측 projection:** owner 경제 outbox(`journal_delivery.py`)의 소비자가 legacy `record_entry`/`record_exit` 와 같은 필드(전략·`entry_risk`·`exit_type`·체결가·수량)로 `trades`/`trade_events` 를 쓰는 유일한 DB writer 다. 체결 키로 멱등이다. attach 모드에서는 `sync_from_kis` 의 DB 쓰기를 끈다 | 차단 3(fill projection)의 계측 부분을 닫는다. 설계 A 는 바뀌지 않는다 |

D11 을 따르면 정규장에 쉬고 있는(resting) 주문이 거의 생기지 않는다. 남는 경우는 거래정지·가격제한 잠김에 걸린
시장가, 마감 LIMIT, 미상 ACK 세 가지이고, 모두 D12·D13 으로 끝난다.

## 5. 단일 runtime owner — 무엇을 옮기나

| 현재 writer/sender (main) | v1 처분 |
| --- | --- |
| 엔진 `update_position`(체결 → 포지션·현금·`daily_pnl`) | attach: owner 체결 적용(`application.py`)이 유일한 writer 다. 엔진 포트폴리오는 owner 게시본의 읽기 전용 사본이다 |
| `_sync_portfolio` 직접 쓰기 | attach: 관측과 D15 보정 제안만 한다(P0-3 의 읽기 전용 분기 재사용) |
| 재시작 로더(`run_trader.py:1765`, `batch_analyzer.py:1101`) | attach: owner `restore()` + D14 시작 술어. legacy 로더는 attach 에서 건너뛴다 |
| 일일 리셋·복원(`engine.py:864-994`) | attach: owner 일자 전환(`day_recovery.py`) + D12 |
| 저녁 `sync_from_kis`(DB `kis_sync`) | attach: DB 쓰기를 끈다(D17). 불일치는 D15 로 처리한다 |
| 수동 매수 `run_manual_buy_orders` | owner 명령(`EntryOrigin.USER`)으로 낸다. 실행 완료를 owner 상태에 남겨 재시작 뒤 재주문을 막는다 |
| CLI `liquidate_all.py`·`sell_specific.py` | `AccountLease` 가 잡혀 있으면(봇 기동 중) 거부한다. 봇이 없을 때만 지금처럼 쓴다 |
| `_cleanup_stale_pending`·자정 전건 취소·`_fallback_stale_sell` | attach 에서는 동작하지 않는다(D11 — 취소·에스컬레이션 없음). P0-4 가드를 재사용한다 |
| `exit_manager.add_exit_exempt` 런타임 추가 | 면제 집합의 정본을 owner 상태로 두고, 추가는 owner 명령으로 한다(차단 5, 펩트론 087010) |
| `modify_order` | 호출자 0건 — 삭제 |

## 6. 시작 절차 (D14)

**차가운 시작 (20:00 이후 ~ 08:00 이전):**

1. `AccountLease` 를 잡는다. 실패하면(두 번째 프로세스) legacy 로 기동하지 않고 중단한다.
2. owner `restore()`. 비terminal attempt 가 있으면 해당 날짜들의 일별체결조회를 적용한 뒤 D12 로 `EXPIRED` 처리하고 예약을 해제한다.
3. 거래소 미체결(TTTC8036R, 전 페이지, **운영과 같은 파라미터**)이 0건인지 확인한다. 0건이 아니면 설치를 거부한다.
   I2 가 틀렸거나 사용자의 예약주문이 있다는 뜻이므로, 경보 후 수동으로 처분한다(D5).
4. KIS 잔고(TTTC8434R)와 owner 포지션·현금을 대사한다. 다르면 브로커 값을 채택하고 보정 이벤트로 남긴다(D15 와 같은 기록).
5. `trading_ready = True`.

**따뜻한 시작 (장중 재시작):** 2~4 를 오늘 날짜로 한다. 거래소 미체결이 있어도 거부하지 않고, 각 행을
**미상 주문으로 입양**한다(D13: 그 종목 잠금, 방향별 예약 = 잔량). 입양한 주문은 오늘 체결 적용 또는 D12 로 끝난다.

**실패하면:** 차가운 시작의 3번, 일별체결조회 실패, 잔고 조회 실패 중 하나라도 있으면 그 프로세스는 legacy 모드로
기동하고 텔레그램으로 알린다. legacy 는 오늘 운영이 재시작마다 하는 방식(잔고 기준 보호)이다.

원장 TR 호출은 시작 1회당 3종(+페이지)이다. 장중에 추가하는 원장 호출은 없다(D15 는 기존 30초 동기화 응답을 쓴다).
EGW00215 는 09-28 결정대로 조치하지 않는다.

## 7. KIS 주문 경계 — 한 함수

모든 attach POST 는 `GuardedKISTransport.send_prepared` 하나로 나간다(P0-1 에서 킬스위치·감사를 붙였다). 계약은 다음과 같다.

| 항목 | 규칙 |
| --- | --- |
| 송신 전 | 최종 guard → 킬스위치(SUBMIT) → 감사 기록 → POST **1회**. 재전송하지 않는다(main `retry=False` 규칙과 같음) |
| 주문 유형 | D11. `requests.py` 가 마감 MARKET 을 이미 거부하므로 같은 표에 정규장 LIMIT SELL 거부를 더한다 |
| 종목당 SELL | 살아 있는 SELL 은 종목당 1건이다(D2). 수량은 `held − reserved` 로 클램프한다(D1, `commands.py` 의 `reserved_quantity_insufficient`) |
| 응답 | ACK(주문번호 있음) → attempt `accepted`. 거절 → `rejected`. 거절 사유가 `APBK0400`·"주문 가능한 수량"이면 그 종목을 잠그고 경보한다(D1 처분 ①; 다음 D15 보정이 풀어 준다). 시한 초과·본문 없음 → UNKNOWN → D13 |
| 진입 가격 | 정규장 BUY 는 MARKET 이라 지정가가 없다. 수량 산정에 쓰는 평가 가격은 dispatch 직전 KIS 현재가다. 경과가 한도를 넘거나 조회에 실패하면 `NOT_SENT` 다(차단 6 — SIGNAL 가격을 처리 시각으로 다시 찍지 않는다). 한도 값은 구현 계획에서 기존 시세 캐시 규칙에 맞춘다 |
| 한도 | 기존 `broker._rate_limit`(공용 리미터)를 쓴다. 원장 TR 직렬화(`LEDGER_TR_IDS`)도 그대로다 |

## 8. 계측 연결 (D17)

- 체결 → owner 적용 → 경제 outbox → projection 소비자 → `trades`/`trade_events`. 기록 필드는 legacy `run_fill_check`
  (`kr_scheduler.py:2860~`)가 넘기던 것과 같다: 전략, `entry_risk` 스냅샷(`market_context`), `exit_type`, 체결가·수량·시각.
- D14·D15 보정은 `exit_type`/`event_type` 에 `kis_sync` 유형을 쓴다. 설계 A 는 이것을 `sync_estimated` 로 따로 센다.
- 인수 조건: 합성 owner 체결에서 설계 A 의 회계 대조(Σ행 `net_pnl` = Σ`trades.pnl`)가 성립하고, 같은 체결을 두 번
  배달해도 DB 행이 한 번만 생긴다.
- legacy 모드에서는 지금 경로를 그대로 쓴다. 모드가 바뀌어도 설계 A 의 입력 계약은 같다.
- 설계 A §10 의 "놓친 체결" 한계(17:00 `sync_from_kis` 는 당일 체결만 보고, 브로커 추적 밖 체결은 동기화로만 들어온다)는
  attach 에서 owner 의 주기 일별체결조회(P0-2)와 D15 보정으로 대체된다. v1 은 NXT 주문을 내지 않으므로 17시 이후 봇 체결도 없다.

## 9. Toss 조회 보강 (v1 이후, 관측 전용)

| 순서 | 무엇 | 쓰임 | 전제 |
| --- | --- | --- | --- |
| T1 | KR 시장 캘린더(`/api/v1/market-calendar/KR`)와 KIS 휴장일 합집합 대조 | D12·D14 가 거래일 판정에 기대므로 09-25 추석 누락 같은 오류를 드러낸다. 불일치는 경보만 한다 | 새 grant(기존 grant 09-22 만료) — 사용자 조치 |
| T2 | 069500 일봉 종가와 KIS 일봉 대조 | 설계 A 벤치마크의 2차 확인. 차이가 크면 `bench_suspect` 후보로 표시만 한다 | 같음. 수정주가 기준 미확정 |

체결가·잔고·주문에는 쓰지 않는다. Toss 시세는 시장 기준이 `unknown` 이고 5분 격자다(조사). v1 설치 조건도 아니다.

## 10. 설치 차단 사유 처분 (1~31)

| 처분 | 번호 | 내용 |
| --- | --- | --- |
| **v1 에서 구현해야 닫힘** | 2, 3(계측·health pending 검사), 4, 5, 6, 7(수동·CLI), 8, 14, 29, 30 | 2→D14, 3→D17 + health 가 owner attempt 를 읽게, 4→설치기 배선, 5→면제 owner 정본, 6→§7 진입 가격, 7→§5, 8→대시보드 attach 인지, 14→축출 쿨다운 시각 owner 저장(1필드), 29→D5 runbook, 30→D13·D15·거절 경보가 텔레그램에 닿음 |
| **결정으로 닫힘(규칙을 빼서)** | 1(1a·1b), 9, 10, 11, 12, 13, 15, 19, 20, 22, 23 | 1→D11(에스컬레이션 대상 없음), 10→D11(정규장 SELL 은 가격 불필요), 9·11·13·15·20→D16, 12·19·23→D13, 22→D12 |
| **이미 닫힘(개발 부품)** | 16, 17, 18, 21, 24, 25, 28(일반 기아) | P0-1·P0-3·P0-2·P1. v1 결합 시험에서 다시 확인한다 |
| **KOFR 삭제로 소멸** | 7 의 KOFR 부분 | main PR #97. 재병합 때 `EntryOrigin.SAFE_ASSET`(`guards.py:62·88`)도 지운다 |
| **v1 미지원(한계로 명시)** | 26 | 시간외·장전 보호 SELL 없음. 밤사이 보유는 다음 정규장부터 보호한다 |
| **v1 결정 필요** | 27 | legacy SELL 발행처(`batch_analyzer._preemptive_stale_exit_on_bear` 등)는 gateway 로 그대로 들어온다. W 의 `intraday_owner` 결정 outbox 소비자는 legacy 에 없는 새 기능이다 → v1 은 소비하지 않고 끈다(구현 계획에서 legacy 동등을 코드로 확인) |
| **관측 항목** | 28(실시간 지연 상한), 31 | canary 기간에 측정한다. 설치 조건이 아니다 |

## 11. 인계 §8 표 처분

| §8 행 | v1 처분 |
| --- | --- |
| 단일 runtime owner | §5·§6 범위만 |
| 실제 consumer/outbox | 경제 outbox → DB projection(D17)과 P1 보호 intent 만 |
| 주문경계 | §7. MODIFY 없음, 취소 체인 최종성은 D11 로 불필요, 최초 인계는 D14 |
| health/경보/장기성능 | 경보 전달(30)만. 63-cell·장기 이력은 관측 |
| N4097, native/source, cold 소유권, 전체 C/F/G/R | **v1 설치 조건에서 뺀다.** 검증 도구 연구로 남긴다(코드·문서 보존). 대신 §13 의 v1 인수 시나리오가 설치 조건이다 |
| main/운영 | 별도 release. 그 시점의 명시 권한·배포·롤백·당일 관측 계획을 따른다 |

## 12. legacy 대비 달라지는 동작 (숨기지 않는다)

1. 정규장 SELL 이 매수1호가 지정가에서 **시장가**로 바뀐다. 호가가 얇으면 한 호가보다 아래에서 체결될 수 있다.
   대신 90초 미체결 → 취소 → 시장가 폴백 경로가 없어진다.
2. 수동 매수 지정가가 체결되지 않으면 **취소하지 않고** 당일 소멸까지 둔다. 그동안 그 종목은 잠기고 현금은 예약된다.
3. 장전·NXT·시간외 주문이 없다.
4. 따뜻한 시작에서 입양한 미상 주문의 종목은 당일 끝까지 잠길 수 있다.
5. 축출 쿨다운이 재시작을 넘어 유지된다(더 엄격해진다).
6. 진입의 레짐·패널 결정 사실을 dispatch 직전에 다시 봉인하지 않는다. **지금 운영과 같고**, W 와 비교할 때만 약해진다.

## 13. Plan → Do → See

| 단계 | 내용 | writer / reviewer(요청) | 끝 조건 |
| --- | --- | --- | --- |
| B0 | main 28커밋을 W 에 재병합(KOFR 삭제·휴장일 합집합 포함), `SAFE_ASSET` 제거 | Terra high / Sol high | 충돌 처리 diff 검토, 전체 UTC·KST |
| B1 owner | D12·D13·D14·D15, §5 표(면제·수동·CLI·대시보드·축출 쿨다운) | Opus high 또는 Astra high / 비작성자 Astra xhigh(교차 공급자) | §14 사용자 확인 후 착수. 시나리오 1~6 |
| B2 경계 | D11·D16·§7(주문 유형·거절 분류·진입 가격·용량 검사) | 같음 | 시나리오 7~9 |
| B3 계측 | D17 projection, `sync_from_kis` 끄기, 설계 A 대조 | Terra high / Opus high | 시나리오 10 |
| B4 설치 | 설치기 배선(`run_trader`), 모드 스위치(기본 legacy), 경보·runbook | Opus high / Astra xhigh | 결합 전체 회귀 + final critical. **운영 설치는 별도 사용자 지시** |
| B5 Toss | T1·T2 | Terra medium / Sol high | 새 grant 이후. 관측만 |

동시 작업자는 합산 3명 이하로 두고, 같은 base SHA·별도 worktree·파일당 writer 1명·coordinator 단독 통합을 지킨다.
전체 회귀는 작업자를 모두 멈춘 뒤 조용한 시간대에 한다(09-28 사용자 결정 — 벽시계 시험 무변경).

**인수 시나리오(가짜 KIS HTTP·주입 시계, 실 API 0):**

1. 차가운 시작: 전일 부분 체결 BUY → 전일 체결 적용 → 잔량 `EXPIRED` → 예약 해제 → `trading_ready`.
2. 차가운 시작에서 거래소 미체결이 1건 있으면 설치를 거부하고 legacy 로 기동하며 경보를 낸다.
3. 따뜻한 시작에서 거래소 미체결 2건 → 두 종목만 잠기고 다른 종목의 보호 SELL 은 나간다.
4. UNKNOWN ACK 한 건 → 그 종목만 잠기고 다른 종목의 BUY·SELL 은 진행한다. 당일 소멸 뒤 다음 차가운 시작에서 풀린다.
5. 사용자 HTS 매도 → 30초 sync 2회 연속 불일치 → owner 가 브로커 수량을 채택하고 `kis_sync` 보정이 DB 에 남는다.
   그 사이 보호 SELL 이 `APBK0400` 으로 거절되면 종목 잠금·경보가 나고 보정 뒤 풀린다.
6. 면제 종목(087010)은 어떤 발행처에서도 SELL POST 0건이다. 런타임 추가도 다음 게시에서 유지된다.
7. 킬스위치(`KILL_SWITCH`)는 BUY 만, `KILL_SWITCH_ALL` 은 SELL 도 막고, 감사 원장에 기록된다.
8. 정규장 SELL 은 MARKET, 마감 SELL 은 LIMIT 이다. attach 경로의 CANCEL·MODIFY POST 는 0건이다.
9. 대기 BUY 2건이 있을 때 최대 포지션·일일 신규 매수 한도가 대기 포함으로 막힌다(D16).
10. 같은 체결을 두 번 배달해도 `trades`/`trade_events` 가 한 번만 쓰이고, 설계 A 회계 대조가 성립한다.

## 14. 사용자 확인이 필요한 결정

리뷰 승인과 별개로, 아래는 이전 결정을 바꾸므로 구현(B1) 착수 전에 사용자가 정한다.

1. **D12** — D3("만료로 예약을 풀지 않음")을 뒤집어 당일 소멸을 최종성으로 쓴다(09-22 지시의 "당일 주문 소멸"을 뼈대로).
2. **D16** — W 의 결정 사실 재봉인(B2/B3 qualification·seal)을 v1 설치 경로에서 뺀다. owner 는 용량만 본다.
3. **§11** — N4097·native·cold 소유권·전체 C/F/G/R 을 설치 조건에서 빼고 §13 의 v1 인수 시나리오로 바꾼다.
4. **D14 실패 폴백** — attach 시작 술어가 실패하면 legacy 로 기동한다(보호 공백 대신 현행 방식).
5. **§12-1** — 정규장 SELL 을 매수1호가 지정가에서 시장가로 바꾼다(주문 동작 변경).

## 15. 하지 않는 것

`trading_ready` 강제 True, MODIFY, 취소 송신, KR 체결통보(D9), 매도가능수량 조회 TR(D1), 보호 전용 모드(D7),
Toss 를 주문·잔고에 쓰기, 장중 새 원장 TR 호출, EGW00215 조치, 위험 한도·전략 설정 변경, main 병합·배포·재시작.

## 16. 리뷰 기록

| 회차 | 리뷰어(요청 모델/effort, 실제 모델) | 결과 | 처리 |
| --- | --- | --- | --- |
| 1 | (리뷰 후 기록) | | |
