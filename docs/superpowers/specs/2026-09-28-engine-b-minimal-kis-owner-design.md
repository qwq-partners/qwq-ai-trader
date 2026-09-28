# 엔진 본체 최소 설계 — 단일 runtime owner → KIS 주문 경계 → Toss 조회 보강 (설계 B)

> **상태 (2026-09-28, 2판):** 서면 설계만 있다. 구현·main 병합·배포·재시작·실주문·설정 변경은 없다. 승인 범위는
> 작성자가 아닌 리뷰어의 **설계 승인**까지다. §14 의 사용자 확인 항목은 리뷰 승인과 별개다.
> 기준: `feature/owner-ticket-gate-20260926` `426743f`(이하 W, engine `feature/engine-safety-design-20260917` 포함),
> main `e31c632`. 작성: Claude coordinator(세션 모델 Fable 5.1 — 라우팅 정책상 미자격이라 작성자 기록으로만 남긴다).
> 인용 표기: **확인** = coordinator 가 원문·코드를 직접 읽음, **조사** = 읽기 전용 조사 작업자(요청 opus/high) 보고.
> 1판(`673b1b2`)은 교차 공급자 리뷰에서 REQUEST_CHANGES(P1 9건)를 받았고, 이 판이 그 처분이다(§16).

## 1. 목적과 완료 조건

`docs/operations/claude-handoff-b1-runner-2026-09-28.md` §8 의 표 전부가 아니라, 두 가지를 만족하는 데 필요한 만큼만 다룬다.

1. **계측 무결성:** 계좌의 모든 KR 체결이 writer 하나를 거쳐 DB `trades`/`trade_events` 에 한 번 기록된다. 이것이
   설계 A(main `docs/superpowers/specs/2026-09-28-kodex200-excess-return-ledger-design.md`)가 읽는 원천이다.
2. **KIS 한 경로로 안전한 실거래:** 봇의 KR 주문 POST 는 모두 한 함수(킬스위치·감사·무재전송)를 지난다. 포지션·현금·
   미체결을 쓰는 주체도 하나다. 현행 위험 한도(`config/*.yml`)는 그대로 두고, **한도 검사도 약해지지 않는다.**

Toss 는 조회·관측 보강만 맡는다. 주문·잔고·계좌는 KIS 만 쓴다(사용자 09-28 재확인). US 는 영구 중단이라 제외한다.

## 2. 출발점 — 지금 무엇이 있나

### 2-1. main(운영, legacy) — 조사, 핵심 확인

| 항목 | 사실 |
| --- | --- |
| 주문 송신 | 모두 `KISBroker`(킬스위치 `kis_kr.py:461`)를 지나지만 부르는 곳은 7곳이다. 엔진 `on_signal` 가드를 지나는 곳은 `engine.py:2531` 하나다. 나머지는 `_fallback_stale_sell`(`engine.py:2318`), `_cleanup_stale_pending`(`kr_scheduler.py:918·977`), 자정 전건 취소(`:5292`), 수동 매수(`:7298`, **확인**), CLI 2개(별도 프로세스·별도 `KISBroker`)다. `modify_order` 호출자는 0건이다(**확인**) |
| 포지션·현금 writer | 엔진 체결 처리(`engine.py:544-683`)와 30초 `_sync_portfolio`(`kr_scheduler.py:1387·1412·1462·1476`)가 경쟁한다. 그 밖에 재시작 로더 2곳과 일일 리셋·복원 3곳이 있다 |
| 체결 유입 | `run_fill_check` 는 브로커 인메모리 추적이 비어 있지 않을 때만 `check_fills()` 를 부른다(`kr_scheduler.py:2823`, **확인**). 추적 밖 체결은 동기화로만 반영되고, DB 에는 저녁 17:00 `sync_from_kis` 가 당일 체결 전체를 조회해 `kis_sync` 로 쓴다(`trade_storage.py:903-904`) |
| 잔고·현금 | 잔고 `TTTC8434R` + 매수가능 `TTTC8908R`(`nrcvb_buy_amt`, 미수 없는 금액 — `kis_kr.py:1639·1657`, **확인**) |
| 주문 유형 | SELL = 매수1호가 지정가(호가가 없으면 시장가), BUY = 시장가(`engine.py:2038-2072`, **확인**). 수동 매수는 현재가 +0.6% 지정가 |
| 안전자산(KOFR) | PR #97 로 삭제됐다(`336e979`) |

### 2-2. W(attach) — 조사, 핵심 확인

- `src/execution/safety/` 는 17,933줄이다. 제품 호출자는 0건이고 main 병합도 0건이다. `trading_ready` 는 상수 `False`
  (`runtime.py:172-175`, **확인**)이고, 설치기 `install_attached_runtime` 은 "운영에서는 항상 거부"(`factory.py:225-236`, **확인**)다.
  설치기는 restore 게시 이후의 실패에서 **legacy 로 계속 가지 말라**고 명시한다(`factory.py:249-257`, **확인**).
- 주문 안전 핵심 부품: 저장소 `store.py`, 계좌 lease `account_lease.py`, 수명주기 `lifecycle.py`, 증거 `evidence.py`/`queries.py`,
  체결 적용 `application.py`/`economics.py`, 명령 `commands.py`, 한도 `risk_policy.py`/`resources.py`, guard `guards.py`,
  송신 `transport.py`(킬스위치·감사, **확인**), 보호 생산자 `protection_producer.py`, 경제 outbox `journal_delivery.py`.
- owner 최종 검사(`commands._evaluate`, **확인**)는 준비 상태·세션·MODIFY 거부·체결 생산자 생존(BUY)·미해결 증거(전역)·
  같은 종목 미해결(방향 무관, `:393`)·현재 시세·자원 계산(`calculate_resources`, 건당 위험)·결정 사실(`_decision_facts`)·
  현금 예약(`:420`)·SELL 수량 초과 **거부**(`:426`)·정책(`evaluate_entry_policy` — 일일 손실·손실 청산 쿨다운·동기화 건강·
  엔진 게이트, `risk_policy.py:593-658`)·최종 진입 guard 를 본다. BUY 예약은 평가금액의 1.015배다(`resources.py:152`).
- 체결 적용은 당일·owner attempt 에 대응하는 체결만 받는다(`runtime.py:728`, 전일 체결 거부 `economics.py:364`, **확인**).
  체결 metadata 허용 키에 `entry_risk` 가 없다(`commands.py:54`, **확인**). journal 은 `execution_journal` 한 테이블만
  원자적으로 쓴다(`journal_delivery.py:163`, **확인**).
- 보호 SELL 은 정규장 MARKET, 마감 LIMIT, 취소 없음이다(P1 결정, `docs/reviews/p1-producer-wiring-2026-09-23.md`, **확인**).
  모든 주문은 평가 가격이 필요하고(`requests.py:242`), 마감 MARKET 은 거부된다(`:245`). gateway 는 평가 가격으로 신호 가격을
  쓴다(`gateway.py:167`, **확인**).
- KIS 미확정 31문항은 판단으로 닫았다(D1~D10, `docs/superpowers/plans/2026-09-22-kis-judgement-decisions.md`, **확인**).
  사용자 결정 "엔진 전체를 옮기도록 하자"(같은 문서 §6)가 attach 전환의 근거다.
- 설치 차단 사유 1~31(`docs/operations/claude-migration-handoff-2026-09-20.md`, **확인**) 중 다수가 열려 있다. 그중 여럿은
  KIS 의 제약이 아니라 **attach 스스로 둔 규칙**에서 생겼다: 같은 종목 미해결의 방향 무관 잠금(12 일부), 체결 적용 창의 전역
  정지(19), 만료를 최종성으로 읽지 않음(22), 결정 사실 재봉인(13·15).
- W 끝의 125커밋은 대부분 검증 도구다(B1 runner, OS evidence, L3 proof, decoder, N4097, native qualification — 조사).

## 3. 원칙

1. **브로커가 강제하는 불변식을 뼈대로 쓴다**(09-22 사용자 지시).
   - I1 **매도가능수량을 넘는 매도는 KIS 가 거절한다.** 거절은 안전망이 아니라 "보호 실패" 신호다(D1).
   - I2 **봇이 내는 KRX 정규·마감 DAY 주문은 그 세션일 안에 끝난다.** 봇은 NXT·SOR·시간외·예약주문을 내지 않으므로(D11),
     그 세션일 20:00(NXT 종료) 이후에는 봇 주문이 살아 있지 않다. **사용자·외부 주문에는 I2 를 적용하지 않는다.**
   - 현금을 넘는 매수의 거절은 불변식으로 쓰지 않는다. 미수 허용 계좌라면 거절되지 않기 때문이다.
2. **잠기는 쪽이 조용히 틀리는 쪽보다 싸다**(판단 결정 문서 §2). 잠금 범위는 원인의 범위와 같게 둔다: 종목 원인은 그 종목
   (방향별), 현금 원인은 BUY 전체, 저장 무결성 원인은 전체.
3. **장벽을 더하지 않고 규칙을 빼서 단순하게 만든다**(09-28 사용자 지시). 봇이 취소·정정을 내지 않으면 **봇이 만든** 취소
   체인은 없다. 사용자 HTS 취소·정정이 만든 체인은 Q1~5 처분(그 주문의 수량 해석 포기 → 수동 처분)을 그대로 따른다.
4. **한도는 약하게 하지 않고, 봉인만 뺀다.** 진입 판단은 지금 운영과 같은 sidecar 가 먼저 하고, owner 는 in-flight 를 아는
   유일한 곳이므로 **현행 한도 전부**를 대기 BUY 포함으로 최종 재검사한다. 빼는 것은 결정 사실의 generation/seal 재검증뿐이다.

## 4. 결정 (D11~D17 — 기존 D1~D10 위에 더한다)

| # | 결정 | 기존 결정과의 관계 |
| --- | --- | --- |
| **D11** | **주문 유형 고정:** 정규장 SELL = MARKET, 마감 동시호가(15:20~15:30) SELL = 현재가 LIMIT, 정규장 BUY = MARKET(legacy 와 같음), 수동 BUY = LIMIT. KRX 만 쓰고(`EXCG_ID_DVSN_CD` 미송신, legacy TR) 장전·NXT·시간외·예약주문은 내지 않는다. **CANCEL·MODIFY 를 송신하지 않는다.** SELL 의 평가 가격(자원 계산용, 주문 가격 아님)은 신선한 KIS 현재가, 없으면 owner 평단이다 — 가격이 없어 보호 SELL 이 막히는 일을 없앤다 | D2(attach 취소 없음)를 v1 에서 영구로 한다. P1 보호 SELL 규칙을 gateway 를 거치는 모든 SELL 로 넓힌다 |
| **D12** | **장 종료 절차 — 주문 종료와 회계 완료를 나눈다.** 매 거래일 20:05 에 실행 중인 owner 가: ① 그날의 계좌 전체 체결(TTTC0081R, 전 페이지)을 가져와 적용한다(D17) ② 조회가 페이지 오류 없이 끝났을 때만 그날의 봇 attempt 를 `order_closed`(I2) + `accounting_complete` 로 닫고 예약을 푼다 ③ 조회가 불완전하면 닫지 않고(해당 종목은 D13 잠금 유지) 경보 후 10분마다 재시도한다. 이 절차를 놓친 날(프로세스 정지)은 다음 차가운 시작에서 **설치 전·`trading_ready` 전·그 날짜 한정**의 전일 마감 재생으로 같은 절차를 한다(`economics.py:364` 의 교차일 거부에 이 경로만 예외). **따뜻한 시작은 어떤 attempt 도 만료하지 않는다.** `accounting_complete` 뒤에 그 주문의 체결 증가가 보이면(`lifecycle.py:533` 충돌) 경보 + 그 종목 BUY 보류 + 수동 처분(D5) | **D3 을 개정한다**: 예약은 만료가 아니라 "완전한 조회로 회계가 끝난 봇 DAY 주문"에서만 푼다. 차단 22·D6 을 구현으로 닫는다. 사용자 확인 필요(§14) |
| **D13** | **미해결의 범위 = 원인의 범위(방향별).** 종목 X 의 SELL 쪽 미해결(UNKNOWN SELL, 입양한 미체결 SELL, 미적용 SELL 관측, 장 종료 절차 미완) → X 의 새 SELL·BUY 거부. X 의 BUY 쪽 미해결 → X 의 새 BUY 만 거부하고 **확정 보유분의 보호 SELL 은 허용**한다(D4 의 원래 요구). **UNKNOWN BUY 는 추가로 전체 BUY 를 보류**한다 — 시장가 체결대금은 1.015배 예약으로 묶이지 않기 때문이다. 해제는 그 BUY 제출 뒤에 시작된 동기화의 브로커 매수가능금액(§6-3)이 들어온 때다. **저장 commit·게시 실패와 종목을 특정할 수 없는 보호 실패는 전체 정지**를 유지한다(W 의 기존 무결성 장벽, 경보 후 따뜻한 시작으로 회복) | D4 를 방향별로 구현한다. 차단 12·19 는 구현으로, 23 은 무결성 장벽 유지 + 복구 절차로 닫는다. D5(runbook)는 유지 |
| **D14** | **시작과 모드 전환:** 모드는 프로세스 시작 때만 정한다. **legacy 로 기동할 수 있는 것은 owner 저장소가 비었거나 비terminal attempt·남은 예약이 0 이고, 실패가 설치기 구간 1(게시 전)에서 났을 때뿐이다**(`factory.py:249-257` 계약). 그 밖의 시작 실패는 attach 로 남아 **새 주문을 받지 않고** 경보하며 60초마다 술어를 다시 본다(운영자 처분 D5). legacy → attach 는 차가운 시작(§6-1)에서만, attach → legacy 는 장 종료 절차 뒤 미해결·예약 0 인 때에만 설정을 바꿔 재시작한다 | D6 을 구체화한다. D7(보호 전용 모드)은 만들지 않는다 |
| **D15** | **브로커 잔고는 장중에 채택하지 않는다.** owner 의 경제 상태(수량·원가·현금)는 체결(D17 의 봇·외부 체결)로만 바뀐다. 30초 동기화의 잔고는 **점검**이다: owner 의 그 종목 최종 변경보다 뒤에 시작된 조회만 "신선"으로 보고, 신선한 값과 owner 가 다르면 경보 + 그 종목 BUY 보류(수량) / 전체 BUY 보류(현금)만 한다. 보호 SELL 이 `APBK0400`·"주문 가능한 수량"으로 거절되면 그 종목 SELL 을 다음 체결 수집 주기까지 멈추고 다시 계산한다. 채택은 차가운 시작(시장이 닫혀 값이 확정된 뒤)에서만 보정 이벤트로 한다 | 1판의 "2회 연속 불일치 → 장중 채택"을 폐기한다(지연된 잔고를 신선하다고 볼 근거가 없음 — Q19~21). D1 처분 ①은 D17 의 외부 체결 적용으로 닫는다 |
| **D16** | **한도는 그대로, 봉인만 뺀다.** owner 는 §7-2 표의 현행 한도 전부를 대기 BUY 포함(부분 체결 중복 계산 없이)으로 최종 재검사한다(`calculate_resources`·`evaluate_entry_policy` 재사용). 정책 입력(설정·레짐·추세·동기화 상태·일일 손익)은 현행 운영이 쓰는 원천에서 `PolicyContext` 로 **계속 게시**한다(차단 20 publisher). 빼는 것은 결정 사실의 generation/seal 재검증(`commands._decision_facts`·`qualification`·`risk_input_seal`·패널/trade_memory 축)과 CV 시계 대조다 | W 의 B2/B3 request-bound qualification 을 v1 설치 경로에서 뺀다(코드는 남기되 게이트로 쓰지 않는다). 차단 13·15 는 제거로 닫고, 9·11·20 은 출처 단일화·지속 게시 구현으로 닫는다. 사용자 확인 필요(§14) |
| **D17** | **계좌 전체 체결 수집 + 한 트랜잭션 projection.** owner 의 수집 task 가 오늘의 계좌 전체 체결(TTTC0081R)을 주문번호별 누적 체결 수량의 증분으로 읽는다. 주문번호가 owner attempt 와 맞으면 그 attempt 에, 맞지 않으면(사용자 HTS, 주문번호 없는 UNKNOWN 의 실제 주문, attach 이전 주문) **외부 체결**로 owner 포지션·현금에 적용한다. 설치 시점까지의 행은 커서로 건너뛴다. 각 증분은 DB 한 트랜잭션에서 `execution_journal` + `trades`/`trade_events` + projection 완료 표식을 함께 쓴다(증분 키로 멱등). 체결 metadata 에 `entry_risk` 를 더한다. attach 에서는 `sync_from_kis` 의 DB 쓰기를 끈다 | 차단 3·18 의 계측 부분을 닫는다. 설계 A 는 바뀌지 않는다 |

D11 을 따를 때 쉬고 있는(resting) 주문은 네 가지다: 거래정지·가격제한 잠김에 걸린 시장가, 마감 LIMIT, **수동 LIMIT BUY**,
미상 ACK. 모두 D12·D13 으로 끝난다. **예약 해제는 보호 완료가 아니다** — 마감 LIMIT 이 미체결로 끝나면 포지션은 남고,
다음 정규장에서 보호 생산자가 조건을 다시 판정한다.

## 5. 단일 runtime owner — 무엇을 옮기나

| 현재 writer/sender (main) | v1 처분 |
| --- | --- |
| 엔진 `update_position`(체결 → 포지션·현금·`daily_pnl`) | attach: owner 체결 적용(`application.py`)이 유일한 writer 다(봇 체결 + D17 외부 체결). 엔진 포트폴리오는 owner 게시본의 읽기 전용 사본이다 |
| `_sync_portfolio` 직접 쓰기 | attach: 읽기 전용 점검(D15, P0-3 분기 재사용). 잔고가 owner 에 없는 종목은 D17 외부 체결이 먼저 반영하고, 그래도 다르면 경보 |
| 재시작 로더(`run_trader.py:1765`, `batch_analyzer.py:1101`) | attach: owner `restore()` + §6 시작 절차. legacy 로더는 attach 에서 건너뛴다 |
| 일일 리셋·복원(`engine.py:864-994`) | attach: owner 일자 전환(`day_recovery.py`) + D12 장 종료 절차 |
| 저녁 `sync_from_kis`(DB `kis_sync`) | attach: DB 쓰기를 끈다. 계좌 전체 체결은 D17 수집이 쓴다 |
| 수동 매수 `run_manual_buy_orders` | owner 명령(`EntryOrigin.USER`)으로 낸다. 실행 완료를 owner 상태에 남겨 재시작 뒤 재주문을 막는다 |
| CLI `liquidate_all.py`·`sell_specific.py` | `AccountLease` 가 잡혀 있으면(봇 기동 중) 거부한다. 봇이 없을 때만 지금처럼 쓴다 |
| `_cleanup_stale_pending`·자정 전건 취소·`_fallback_stale_sell` | attach 에서는 동작하지 않는다(D11). P0-4 가드를 재사용한다 |
| `exit_manager.add_exit_exempt` 런타임 추가 | 면제 집합의 정본을 owner 상태로 두고, 추가는 owner 명령으로 한다(차단 5, 펩트론 087010) |
| 외부 체결로 생긴 보유 | legacy 동기화 추가 분기와 같은 기본 청산 등록(평단 = 외부 체결가 가중)을 owner 보호 상태로 만든다. 면제 목록은 그대로 따른다 |
| `modify_order` | 호출자 0건 — 삭제 |

## 6. 시작·장 종료·점검 절차

### 6-1. 차가운 시작 (20:00 이후 ~ 08:00 이전)

1. `AccountLease` 를 잡는다. 실패하면(두 번째 프로세스) 기동하지 않는다.
2. **최초 설치**(owner 저장소가 빔): 거래소 미체결(TTTC8036R, 전 페이지, 운영과 같은 파라미터)이 0건이어야 한다. KIS 잔고
   (TTTC8434R)·매수가능(TTTC8908R)으로 checkpoint(보유·평단·현금)를 만들고, 오늘 체결(TTTC0081R) 전부를 커서로 둔다. 면제 목록을 옮긴다.
3. **재기동**(저장소 있음): `restore()`. 장 종료 절차를 놓친 날이 있으면 D12 의 전일 마감 재생을 날짜 순서로 한다.
   재생이 불완전하면 그 종목들은 잠긴 채 설치를 계속한다(D13).
4. 거래소 미체결이 0건이 아니면(= 사용자·외부 주문, I2 비적용) 설치를 거부한다. 저장소에 미해결 상태가 있으면 D14 에 따라
   legacy 로 가지 않고 주문 정지·경보한다.
5. 잔고·현금을 owner 와 대사해 다르면 브로커 값을 채택하고 보정 이벤트(DB `kis_sync` 유형)로 남긴다 — 시장이 닫혀 있어 값이 확정된 때다.
6. `trading_ready = True`.

### 6-2. 따뜻한 시작 (장중 재시작)

`restore()` → 오늘 체결 적용(D17, 커서 이어받기) → 거래소 미체결의 각 행을 **미상 주문으로 입양**(방향별 D13 잠금, 방향별 예약 = 잔량)
→ 잔고·현금은 점검만(D15, 채택 없음) → `trading_ready`. 어떤 attempt 도 만료하지 않는다. 입양한 주문은 오늘 장 종료 절차가 닫는다.
단, 입양한 주문이 사용자 주문이면 I2 가 적용되지 않으므로 장 종료 절차는 그것을 닫지 않고 다음 차가운 시작의 4번이 처리한다.

### 6-3. 점검과 호출 예산

- 신선도: 동기화 조회의 **요청 시작 시각**이 owner 의 그 종목(또는 현금) 최종 변경보다 뒤일 때만 신선하다.
- 체결 수집 주기: 봇의 미해결 attempt 가 있으면 5초, 없으면 60초(외부 체결 포착용). 원장 TR 은 기존 리미터
  (`LEDGER_TR_IDS`, 1.05초·8434R 2.1초 간격)를 그대로 거친다. 장 종료 절차·시작 절차의 원장 TR 은 4종(8434R·8908R·0081R·8036R, +페이지)이다.
- EGW00215 는 09-28 결정대로 조치하지 않는다. 체결 수집의 60초 기본 주기는 legacy(미해결 주문이 있을 때만 2~5초 조회)보다
  장중 원장 호출을 조금 늘린다 — canary 에서 거절 건수를 본다.

## 7. KIS 주문 경계

### 7-1. 한 함수

모든 attach POST 는 `GuardedKISTransport.send_prepared` 하나로 나간다(P0-1 에서 킬스위치·감사를 붙였다).

| 항목 | 규칙 |
| --- | --- |
| 송신 전 | 최종 guard → 킬스위치(SUBMIT) → 감사 기록 → POST **1회**. 재전송하지 않는다(main `retry=False` 규칙과 같음) |
| 주문 유형 | D11. `requests.py` 가 마감 MARKET 을 이미 거부하므로 같은 표에 정규장 LIMIT SELL 거부를 더한다 |
| SELL 수량 | 살아 있는 SELL 은 종목당 1건(D2). 보호 생산자가 `held − reserved` 로 수량을 정해 요청하고, 초과 요청은 owner 가 거부한다(`commands.py:426`) |
| 응답 | ACK(주문번호 있음) → `accepted`. 거절 → `rejected`. 수량 초과 거절(`APBK0400`·"주문 가능한 수량") → 경보 + D15 처리. 시한 초과·본문 없음 → UNKNOWN → D13 |
| 평가 가격 | BUY: dispatch 직전 KIS 현재가(경과 한도 초과·조회 실패면 `NOT_SENT` — 신호 가격을 처리 시각으로 다시 찍지 않는다, 차단 6). SELL: 신선한 현재가, 없으면 owner 평단(D11). 한도 값은 구현 계획에서 기존 시세 캐시 규칙에 맞춘다 |
| 호출 한도 | 기존 `broker._rate_limit`(공용 리미터)를 쓴다 |

### 7-2. 한도의 최종 검사 위치 (D16)

| 한도(현행 설정) | 지금 main 의 최종 검사 | v1 owner 최종 검사 |
| --- | --- | --- |
| 일일 최대 손실 -5%(effective_daily_pnl) | sidecar `risk/manager.py` + 엔진 게이트 | `evaluate_daily_loss`(`risk_policy.py:593`) — owner 손익 기준 |
| 당일 손실 청산 쿨다운 | sidecar | `evaluate_entry_policy` 의 `EXIT_LOSS_COOLDOWN` |
| 일일 거래 10회·신규 매수 5개 | 엔진 RiskManager | owner 정책(대기 BUY 포함) |
| 최대 포지션 8 | 엔진·sidecar | owner 정책(대기 BUY 를 슬롯 점유로) |
| 건당 위험 0.7%·포지션 상한 18%(risk 모드)/28%(nominal)·최소 20만원 | 엔진 최종 재클램프(`engine.py:2967` 부근) | `calculate_resources`(최종 수량·평가 가격 기준) |
| 최소 현금 5%·예약 현금 | 엔진 `get_available_cash` | owner 현금 − 예약(1.015배) ≥ 필요 + 여유, 그리고 신선한 브로커 매수가능금액 이하(D15) |
| 섹터·전략 배분 예산 | sidecar | owner 정책(대기 포함) |
| 동기화 건강 | sidecar | `SYNC_UNHEALTHY` 게이트(`risk_policy.py:588-592`) |
| 자동매도 금지(exit_exempt) | 엔진 `on_signal` 중앙 가드 | owner 면제 집합(§5) |

출처는 로드된 `RiskConfig`(+`evolved_overrides` 머지) 하나다(factory 의 정책 생성 함수, 차단 11). 코어 예약의 두 출처(차단 9)도 이 하나로 묶는다.
구현 계획은 이 표의 각 행에 "그 게이트만 무력화하면 RED 가 되는" 시험을 둔다(W 이월 목록의 S5 한계 — 이중 방어에 가려진 게이트).

## 8. 계측 연결 (D17)

- 계좌 전체 체결 → owner 적용(봇/외부) → 한 트랜잭션(`execution_journal` + `trades`/`trade_events` + 완료 표식). 증분 키 =
  (계좌, 주문번호, 누적 체결 수량). 트랜잭션 도중 종료되면 전부 없고, 재배달은 표식으로 한 번만 된다.
- trade 연결: owner 포지션이 0 → 양수가 될 때 새 `trades` 행(lifecycle id), 부분 체결은 `trade_events` 로 붙인다. 봇 체결은
  전략·`entry_risk`(`market_context`)·`exit_type`·체결가·수량·시각을 legacy `run_fill_check`(`kr_scheduler.py:2860~`)와 같은
  필드로 쓴다. 외부 체결은 거래 id `KIS_SYNC_` 접두와 `entry_reason="external_fill"` 로 쓴다 — 설계 A 가 `sync_entry` 로 제외한다.
  차가운 시작 보정은 `kis_sync` 유형 이벤트다.
- 인수 조건: 합성 체결에서 설계 A 의 회계 대조(Σ행 `net_pnl` = Σ`trades.pnl`)가 성립하고, 같은 증분을 두 번 배달해도 행이
  한 번만 생기며, HTS 왕복(같은 날 매수·매도)이 외부 체결 2건으로 남는다.
- legacy 모드에서는 지금 경로를 그대로 쓴다. 모드가 바뀌어도 설계 A 의 입력 계약은 같다. 설계 A §10 의 "놓친 체결" 한계는
  attach 에서 D17 수집으로 대체된다.

## 9. Toss 조회 보강 (v1 이후, 관측 전용)

| 순서 | 무엇 | 쓰임 | 전제 |
| --- | --- | --- | --- |
| T1 | KR 시장 캘린더(`/api/v1/market-calendar/KR`)와 KIS 휴장일 합집합 대조 | D12 장 종료 절차·§6 시작 창이 거래일 판정에 기대므로 09-25 추석 누락 같은 오류를 드러낸다. 불일치는 경보만 한다 | 새 grant(기존 grant 09-22 만료) — 사용자 조치 |
| T2 | 069500 일봉 종가와 KIS 일봉 대조 | 설계 A 벤치마크의 2차 확인. 차이가 크면 표시만 한다 | 같음. 수정주가 기준 미확정 |

체결가·잔고·주문에는 쓰지 않는다. Toss 시세는 시장 기준이 `unknown` 이고 5분 격자다(조사). v1 설치 조건도 아니다.

## 10. 설치 차단 사유 처분 (1~31)

| 처분 | 번호 | 내용 |
| --- | --- | --- |
| **제거로 닫힘** | 1(봇 취소·에스컬레이션), 13, 15 | 1→D11(봇은 취소·에스컬레이션을 하지 않음. HTS 체인은 Q1~5 처분 그대로), 13·15→D16(재봉인·CV 시계 대조 제거) |
| **v1 에서 구현해야 닫힘** | 2, 3, 4, 5, 6, 7(수동·CLI), 8, 9, 10, 11, 12, 14, 19, 20, 22, 23, 29, 30 | 2→§6 시작 절차, 3·18 계측→D17, 4→설치기 배선, 5→면제 owner 정본, 6→§7-1 BUY 평가 가격, 7→§5, 8→대시보드 attach 인지, 9·11→D16 출처 단일화, 10→D11 SELL 평가 가격 대체, 12·19→D13 방향별 범위, 14→축출 쿨다운 시각 owner 저장, 20→`PolicyContext` 지속 게시, 22→D12 장 종료 절차, 23→무결성 장벽 유지 + 복구 절차(따뜻한 시작), 29→D5 runbook, 30→경보가 텔레그램에 닿음 |
| **이미 닫힘(개발 부품)** | 16, 17, 18(생산자), 21, 24, 25, 28(일반 기아) | P0-1·P0-3·P0-2·P1. v1 결합 시험에서 다시 확인한다 |
| **KOFR 삭제로 소멸** | 7 의 KOFR 부분 | main PR #97. 재병합 때 `EntryOrigin.SAFE_ASSET`(`guards.py:62·88`)도 지운다 |
| **v1 미지원(한계로 명시)** | 26 | 시간외·장전 보호 SELL 없음. 밤사이 보유는 다음 정규장부터 보호한다 |
| **v1 결정 필요** | 27 | legacy SELL 발행처(`batch_analyzer._preemptive_stale_exit_on_bear` 등)는 gateway 로 그대로 들어온다. W 의 `intraday_owner` 결정 outbox 소비자는 legacy 에 없는 새 기능이다 → v1 은 소비하지 않고 끈다(구현 계획에서 legacy 동등을 코드로 확인) |
| **관측 항목** | 28(실시간 지연 상한), 31 | canary 기간에 측정한다. 설치 조건이 아니다 |

## 11. 인계 §8 표 처분

| §8 행 | v1 처분 |
| --- | --- |
| 단일 runtime owner | §5·§6 범위만 |
| 실제 consumer/outbox | D17 projection 과 P1 보호 intent 만 |
| 주문경계 | §7. MODIFY 없음, 봇의 취소 체인 없음, 최초 인계는 §6-1 |
| health/경보/장기성능 | 경보 전달(30)만. 63-cell·장기 이력은 관측 |
| N4097, native/source, cold 소유권, 전체 C/F/G/R | **v1 설치 조건에서 뺀다**(코드·문서 보존). 대신 §13 의 정상 + **장애·복구 인수 시나리오**가 설치 조건이다. 원자성·복구 검증은 빼지 않는다 |
| main/운영 | 별도 release. 그 시점의 명시 권한·배포·롤백·당일 관측 계획을 따른다 |

## 12. legacy 대비 달라지는 동작 (숨기지 않는다)

1. 정규장 SELL 이 매수1호가 지정가에서 **시장가**로 바뀐다. 호가가 얇으면 한 호가보다 아래에서 체결될 수 있다.
   대신 90초 미체결 → 취소 → 시장가 폴백 경로가 없어진다. 시장가 잔량(가격제한 잠김)은 당일 소멸 뒤 다음 정규장에서 다시 판정한다.
2. 수동 매수 지정가가 체결되지 않으면 **취소하지 않고** 당일 소멸까지 둔다. 그동안 그 종목 BUY 는 잠기고 현금은 예약된다
   (그 종목의 보호 SELL 은 된다).
3. 장전·NXT·시간외 주문이 없다.
4. 따뜻한 시작에서 입양한 미상 주문의 종목은 당일 끝까지(사용자 주문이면 다음 차가운 시작까지) 방향별로 잠길 수 있다.
5. UNKNOWN BUY 뒤에는 신선한 매수가능금액이 올 때까지(최대 약 30초) 모든 BUY 가 보류된다.
6. 시작 실패 + 미해결 상태면 legacy 로 가지 않고 주문을 멈춘다(운영자 처분).
7. 축출 쿨다운이 재시작을 넘어 유지된다(더 엄격해진다).
8. W P1 결정을 그대로 받는다: 보유기간 판정이 달력일에서 **영업일**(ExitManager 정본)로 바뀌고, 결정이 없는 틱은 20초마다
   기록해 **순간 고점을 놓칠 수 있다**(트레일링 기준 고점이 legacy 보다 낮게 잡힐 수 있음).
9. 진입의 레짐·패널 결정 사실을 dispatch 직전에 다시 봉인하지 않는다. **지금 운영과 같고**, W 와 비교할 때만 약해진다.

## 13. Plan → Do → See

| 단계 | 내용 | writer / reviewer(요청) | 끝 조건 |
| --- | --- | --- | --- |
| B0 | main 28커밋을 W 에 재병합(KOFR 삭제·휴장일 합집합 포함), `SAFE_ASSET` 제거 | Terra high / Sol high | 충돌 처리 diff 검토, 전체 UTC·KST |
| B1 owner | D12·D13·D14·D15·§6, §5 표 | Claude Opus high / **Codex Astra xhigh**(교차 공급자) — 또는 Astra 작성 / Opus 리뷰 | §14 사용자 확인 후 착수. 시나리오 1~6·11~16 |
| B2 경계 | D11·D16·§7(주문 유형·평가 가격·한도 표·출처 단일화·`PolicyContext` 게시) | 같은 방식으로 작성자와 다른 공급자가 리뷰 | 시나리오 7~9·§7-2 게이트별 RED |
| B3 계측 | D17 수집·projection·metadata·`sync_from_kis` 끄기·설계 A 대조 | Terra high / Opus high | 시나리오 10·17·18 |
| B4 설치 | 설치기 배선(`run_trader`), 모드 스위치(기본 legacy), 경보·runbook | Opus high / Astra xhigh | 결합 전체 회귀 + final critical. **운영 설치는 별도 사용자 지시** |
| B5 Toss | T1·T2 | Terra medium / Sol high | 새 grant 이후. 관측만 |

동시 작업자는 합산 3명 이하로 두고, 같은 base SHA·별도 worktree·파일당 writer 1명·coordinator 단독 통합을 지킨다.
전체 회귀는 작업자를 모두 멈춘 뒤 조용한 시간대에 한다(09-28 사용자 결정 — 벽시계 시험 무변경).

**인수 시나리오(가짜 KIS HTTP·주입 시계·장애 주입, 실 API 0):**

정상 경로
1. 최초 설치: 빈 저장소 + 미체결 0 → 잔고·매수가능으로 checkpoint, 오늘 체결은 커서로 건너뜀, 면제 목록 이전.
2. 장 종료 절차: 부분 체결 BUY → 그날 체결 적용 → 조회 완전 → `order_closed`·`accounting_complete` → 예약 해제.
3. 따뜻한 시작에서 거래소 미체결 2건(봇 SELL 1, 사용자 BUY 1) → SELL 종목은 SELL·BUY 잠금, BUY 종목은 BUY 만 잠금·보호 SELL 허용, 다른 종목 정상.
4. UNKNOWN BUY → 전체 BUY 보류 → 그 뒤 시작된 동기화의 매수가능금액 도착 후 해제. 그 사이 보호 SELL 은 나간다.
5. 사용자 HTS 매도 → 외부 체결로 owner 수량 감소 → 보호 SELL 수량이 줄어 거절 없음. 수집 전에 거절되면 그 종목 SELL 이 다음 주기까지 멈춘 뒤 재계산.
6. 면제 종목(087010)은 어떤 발행처에서도 SELL POST 0건이다. 런타임 추가도 다음 게시에서 유지된다.
7. 킬스위치(`KILL_SWITCH`)는 BUY 만, `KILL_SWITCH_ALL` 은 SELL 도 막고, 감사 원장에 기록된다.
8. 정규장 SELL 은 MARKET, 마감 SELL 은 LIMIT 이다. attach 경로의 CANCEL·MODIFY POST 는 0건이다. 시세 없음에도 보호 SELL 은 평단 평가로 나간다.
9. 대기 BUY 2건이 있을 때 최대 포지션·일일 신규 매수·일일 손실·현금 여유가 대기 포함으로 막힌다(§7-2 각 행을 단독으로 무력화하면 RED).
10. 같은 증분을 두 번 배달해도 `trades`/`trade_events` 가 한 번만 쓰이고, 설계 A 회계 대조가 성립한다.

장애·복구 경로
11. 장 종료 절차 중 체결 조회 2페이지 실패 → 닫지 않음·잠금 유지·경보·10분 재시도 후 완료.
12. 20:05 절차를 놓치고 다음 날 차가운 시작 → 설치 전 전일 마감 재생으로 적용 후 닫힘. 재생이 불완전하면 그 종목만 잠긴 채 설치.
13. 시작 술어 실패 + 미해결 attempt 존재 → legacy 로 가지 않고 주문 정지·경보, 60초 재검사로 회복.
14. projection 트랜잭션 도중 프로세스 종료 → 재기동 후 정확히 한 번 기록.
15. store commit 실패 → 전체 정지·경보 → 따뜻한 시작으로 회복.
16. `accounting_complete` 뒤 같은 주문의 체결 증가 → 경보·그 종목 BUY 보류·자동 처분 없음.
17. HTS 왕복(같은 날 매수·매도) → 외부 체결 2건이 DB 에 남고 설계 A 에서 `sync_entry` 로 제외된다.
18. 설치 전 체결(커서 이전)은 다시 적용되지 않는다.

## 14. 사용자 확인이 필요한 결정

리뷰 승인과 별개로, 아래는 이전 결정이나 운영 동작을 바꾸므로 구현(B1) 착수 전에 사용자가 정한다.

1. **D12** — D3("만료로 예약을 풀지 않음")을 개정해, 완전한 조회로 회계가 끝난 봇 DAY 주문에서만 예약을 푼다.
2. **D16** — W 의 결정 사실 재봉인(B2/B3 qualification·seal)을 v1 설치 경로에서 뺀다. 한도 검사는 그대로 둔다.
3. **§11** — N4097·native·cold 소유권·전체 C/F/G/R 을 설치 조건에서 빼고 §13 의 정상·장애 인수 시나리오로 바꾼다.
4. **D14** — 시작 실패 + 미해결 상태면 legacy 로 가지 않고 주문을 멈춘다(운영자 처분). legacy 폴백은 저장소가 깨끗할 때만.
5. **§12-1** — 정규장 SELL 을 매수1호가 지정가에서 시장가로 바꾼다.
6. **§12-8** — W P1 의 보유기간 영업일 기준과 20초 고점 표본을 받아들인다.

## 15. 하지 않는 것

`trading_ready` 강제 True, MODIFY, 봇의 취소 송신, KR 체결통보(D9), 매도가능수량 조회 TR(D1), 보호 전용 모드(D7), 장중 잔고 채택,
Toss 를 주문·잔고에 쓰기, EGW00215 조치, 위험 한도·전략 설정 변경, main 병합·배포·재시작.

## 16. 리뷰 기록

| 회차 | 리뷰어(요청 모델/effort, 실제 모델) | 결과 | 처리 |
| --- | --- | --- | --- |
| 1 | Codex 교차 공급자 리뷰(요청 gpt-6-astra/xhigh, read-only 샌드박스; rollout 메타데이터 model `gpt-6-astra`·effort `xhigh`, 작성자 아님) — `673b1b2` | **REQUEST_CHANGES** — P0 0, P1 9, P2 2, 틀린 인용 6 | coordinator 가 근거 코드를 직접 확인(`factory.py:249-257`, `economics.py:364`, `runtime.py:728`, `resources.py:152`, `commands.py:54·393·426`, `requests.py:242`, `gateway.py:167`, `risk_policy.py:588-658`, `journal_delivery.py:163`, main `kis_kr.py:1639·1657`) — 전부 사실. 처분: ①legacy 폴백 제한(D14) ②I2 를 봇 KRX DAY 주문으로 한정·따뜻한 시작 만료 금지(§3·D12) ③주문 종료와 회계 완료 분리·장 종료 절차·전일 마감 재생·최초 설치 checkpoint(D12·§6-1) ④UNKNOWN BUY 전체 BUY 보류·무결성 실패 전체 정지 유지(D13) ⑤방향별 잠금(D13) ⑥장중 잔고 채택 폐기·신선도 정의(D15) ⑦한도 전부 유지·재봉인만 제거·한도표(D16·§7-2) ⑧계좌 전체 체결 수집·외부 체결(D17) ⑨한 트랜잭션 projection·`entry_risk` metadata·trade 연결(D17·§8). P2: 차단 9·11·20·23 을 구현 필요로 이동, §12-8 추가. 인용: 클램프→거부, 차단 10 은 구현 필요, resting 에 수동 LIMIT BUY 추가, 시작 TR 4종, HTS 취소 체인 문장, 교차 공급자 짝 |
