# 엔진 목적 정합성 — 1단계 KR 주문·체결 안전성 상세 설계

> 상태: **상세 설계 사용자 확인 대기. 구현·배포·설정 변경 없음.**
> 작성일: 2026-09-17 KST. 기준: `origin/main 465a0298059abfdeb4381d1e75933fc450e2ad58`.
> 사용자 승인: 비용 차감 후 KODEX200 초과수익 검증 + 현행 위험 한도 유지, 단계적 재설계의 첫 상세 설계 진행.
> 승인 구분: 이 문서를 만드는 승인과 구현 승인, 운영 배포·재시작 승인은 서로 다르다.

## 1. 목적·범위·변경하지 않는 것

### 1.1 목적과 완료의 의미

좋은 종목 분석이 실제 주문·보유·청산에서 다른 노출로 변하지 않도록 실행 상태의 정합성을 먼저 확보한다.
이번 단계는 수익 개선 실험이 아니다. 테스트 통과를 초과수익·투자 엣지·canary 통과로 보고하지 않는다.

첫 구현 단위는 **KR 실행 경로**다. KR·US 전체 안전성 완료로 확대 해석하지 않는다.
US의 취소/부분체결/POST 재시도 문제는 §10의 별도 필수 후속이다. 공용 ExitManager/API를 건드리면 US 호환 회귀시험은 이번 단계에서도 필수다.

### 1.2 포함

- 취소 요청 결과와 원 주문 최종 상태 분리, 미확정 주문의 예약·식별자·청산 목표 보존.
- 브로커 관측 → 체결 전달 → Portfolio/ExitManager/pending 적용 → 후속 원장의 완료 경계 통일.
- 부분체결·중복·역순 관측·잔고 동기화·재시작 간 수량/현금 이중 반영 방지.
- KR 자동 전략 진입의 최종 제출 경계에서 기존 severe·회복 cooldown·SEPA 진입 마감 재검사.
- 위 계약에 필요한 작은 실행 상태 저장소, 기존 상태의 안전한 인계, 고장 주입 시험과 관측 지표.

### 1.3 유지·제외

- **KIS만 주문·취소/정정·체결·계좌·잔고를 담당한다. Toss는 별도 관측 서비스 그대로다.**
- Toss 추가 KIS 조회 0·단일 발급·승인 일정/만료를 변경하지 않는다. 아래 KIS 대사는 거래 봇의 주문 안전성 작업이지 Toss 관측 확장이 아니다.
- 종목/전략 배분, 위험 사이징 한도, 손절/익절 수치, 매도 면제, 주문 종류·폴백 횟수를 바꾸지 않는다.
- EntryPlan 가격 상한 enforce, LLM 분석 분리, 전략 자격 통합, 주간 배분 승격 게이트, KODEX200 성과 게이트는 후속 단계다.
- 직접 주문·수동 매수 지시·킬스위치·기존 상태 파일 삭제·운영 서비스 조작은 하지 않는다.

### 1.4 구현 승인 시 함께 확인할 의도된 동작 차이

1. 취소/접수 상태가 불명확하면 시간 경과만으로 재주문하지 않는다. **중복 방지의 대가로 청산 재시도가 지연될 수 있다.** 경보와 수동 확인 절차를 제공한다.
2. 같은 최초 진입 주문의 후속 부분체결은 별도 추가매수가 아니다. 수량 증가는 반영하지만 익절 단계·본전보호를 리셋하지 않는다.
3. 자동 전략 진입의 당일 위험 입력이 결측/손상/전일 자료이면 `normal`로 간주하지 않고 신규 매수를 보류한다. 시작 직후 첫 유효 관측까지 진입이 늦어질 수 있다.
4. 실행 상태 저장/복구가 불가능하면 해당 KR 실행자의 새 거래 POST는 전송하지 않는다. 이는 매도에도 영향을 줄 수 있으므로 CRITICAL 상태와 운영자 조치가 필요하다. 저장 장애 중에도 자동 손절이 항상 가능하다고 약속하지 않는다.
5. 정책 수치의 변경은 아니지만 실행 허용 조건은 더 보수적으로 바뀐다. 위 차이를 숨긴 채 '동작 무변경 리팩터'로 배포하지 않는다.

## 2. 근거와 대안

### 2.1 코드에서 확인한 연결부

| ID | 기준 코드 | 확인된 문제 |
|---|---|---|
| S1 | `src/execution/broker/kis_kr.py::cancel_all_for_symbol`, `src/core/engine.py::RiskManager.on_signal` | 취소 False/예외가 0으로 축약되고, 90초 매도 폴백이 원 주문 생존 여부와 무관하게 재제출 가능 |
| S2 | `src/schedulers/kr_scheduler.py::run_fill_check`, `src/core/engine.py::RiskManager.on_fill` | 큐 적재 직후 옛 포지션으로 청산 등록. 포트폴리오 반영 실패도 pending 정리로 이어질 수 있음 |
| S3 | `src/execution/broker/kis_kr.py::check_fills` | 소비자가 적용하기 전에 관측 watermark 전진·완료 주문 매핑 삭제 |
| S4 | `src/schedulers/kr_scheduler.py::_sync_portfolio`, `src/strategies/exit_manager.py::register_position` | 기존 종목 청산 상태 대사 부재. 재등록만 반복하면 수량 증가를 추가매수로 오인해 stage 리셋 |
| S5 | `src/core/batch_analyzer.py::execute_pending_signals` | 시작 시 위험/시각 검사 후 quote·분산 대기 중 상태 변경을 놓침 |

직전 리뷰의 합성 재현: 보유100/목표매도10/취소실패에서 추가매도10, 매수40+60에서 Portfolio100/Exit40,
normal→severe 전환 뒤 두 번째 VCP BUY 발행. 실계좌에서 같은 사고가 발생했는지는 조사하지 않았다.

### 2.2 대안과 선택

- **조건문 패치만:** 변경량은 작지만 bool 취소·비동기 완료·sync/체결 중복·재시작 창이 남는다.
- **선택: KR 실행 계약 통합:** 기존 전략/브로커/엔진을 유지하고 주문 생명주기, 체결 적용, 최종 제출 승인에 책임을 부여한다.
- **전면 엔진 재작성:** KR/US·분석·원장을 한 번에 교체하는 이행 위험이 커서 선택하지 않는다.

## 3. 책임과 식별자

명칭은 제안 인터페이스다. 아직 구현된 클래스/API로 읽지 않는다.

| 구성요소 | 소유 책임 | 하지 않는 일 |
|---|---|---|
| `OrderLifecycleCoordinator` | intent/attempt/예약/취소·종결 증거/재주문 가능 여부 | 분석 점수 결정, 초과수익 판정 |
| `KISOrderEvidenceAdapter` | 공식 응답 정규화·조회 완결성·원주문 연결·누적체결 관측 | 빈 목록으로 최종 취소 추정 |
| `FillApplicationCoordinator` | 체결과 보호 상태의 단일 commit·적용 receipt·sync 인계 | 텔레그램/LLM/외부 DB 완료 대기 |
| `ExecutionStateStore` | 실행 상태·적용 watermark·outbox의 원자적 영속화 | 성과 분석 DB 전체 대체 |
| `FinalEntryGuard` | 전송 직전 현재 위험/시각/분류 확인 | 신규 종목 선정·가격 상한 정책 승격 |

- `intent_id`: 같은 청산 목표/진입 주문 목적을 식별한다. 재주문해도 원래 목표수량을 늘리지 않는다.
- `attempt_id`: 실제 HTTP 주문/취소 송신 시도별 고유값. 상태 없는 자동 재전송 금지.
- `OrderRef`: 내부 계좌 범위 ID + 시장 + 주문 영업일 + 거래소 범위 + KIS 주문번호 + 기관/원주문 연결 정보.
- `position_lifecycle_id`, `entry_order_id`: 동일 종목의 재진입과 최초 진입 주문의 부분체결을 구분한다.
- 이벤트 UUID·시각·종목·가격만으로 체결을 식별하지 않는다. 계좌번호 원문·토큰·HTTP 원문을 일반 로그/문서에 남기지 않는다.
- KR의 엔진 폴백·KOFR·수동 경로를 포함한 모든 거래 호출이 생명주기 담당을 거친다. 예외는 alpha 진입 정책에만 있으며 주문 추적 예외는 아니다. KR 돈 경로에서 구조화된 UNKNOWN을 기존 `(False, 메시지)`로 축약하지 않는다. US 호환 인터페이스는 별도 보존/회귀 검사한다.

## 4. 주문·취소 최종상태 계약

### 4.1 명령 결과와 주문 상태

`CommandResult = NOT_SENT | ACKNOWLEDGED | REJECTED | UNKNOWN`.
ACK는 요청 응답이지 최종 체결/취소 증명이 아니다. 취소 REJECTED는 원 주문이 없다는 뜻이 아니다.

주문 생명주기는 `PREPARED`, `SUBMITTING`, `OPEN`, `PARTIAL`, `CANCEL_REQUESTED`,
`RECONCILING`, `BLOCKED_UNKNOWN`, `FINAL_FILLED`, `FINAL_CANCELLED`, `FINAL_REJECTED`, `FINAL_EXPIRED`를 구분한다.
비종결 상태는 모두 예약/중복 검사에 포함한다. 기존 `Order.is_active`의 enum 열거에서 누락되지 않게 한다.

- intent/attempt를 **송신 전** 저장한다. 응답 저장 전에 종료되면 다음 시작은 UNKNOWN이며 같은 POST를 다시 보내지 않는다.
- attempt별 송신 소유자는 하나다. 폴백·스케줄러·복구가 동시에 같은 목표를 처리해도 coordinator의 version/claim 검사에서 한 시도만 송신권을 얻는다. 단일 sender가 종료됐다고 다른 태스크가 불명 POST를 이어 보내지 않는다.
- 주문번호 없는 성공 응답의 `TEMP_*`는 로컬 추적키일 뿐 거래소 주문번호가 아니다. 자동 매칭/종결 추정 금지.
- 취소 ACK 뒤에도 매핑을 보존한다. 기존 주문의 늦은 체결과 응답은 attempt/version을 확인해 적용한다.
- KR 주문 접수·정정·취소의 불확실한 송신은 자동 재전송하지 않는다. 인증 오류도 같은 함수 내부에서 거래 POST를 반복하지 않는다. GET·hashkey 조회 정책과 구분한다.
- 90초·3분·15분 등 기존 시간은 조사/경보/재시도 요청의 계기일 뿐 pending 해제 증거가 아니다.

### 4.2 주문별 최종 증거

`OrderEvidence`는 OrderRef, 원래 수량, 누적 체결수량/대금, 잔량, 취소확인수량(지원 시),
거부/만료 증거, 조회 범위/시각, schema validity, page completeness, source를 보존한다.

다음은 **최종상태 증거가 아니다**: 로컬 `get_order_status`, 로컬 미체결 목록의 부재, 취소 개수0,
잔고 감소, 일부 페이지만 성공한 조회, 같은 빈 목록의 반복, 체결 내역 전용 조회에서 미체결 주문 부재.

완결성은 요청 계좌·시장/거래소·영업일·원주문 체인을 모두 포함한다. 페이지 실패/상한 도달/순환 cursor/파싱 누락이면 incomplete다.
KIS 조회 제한은 기존 공용 limiter/원장 TR 직렬화를 사용한다. 조회는 기존 체결 확인·취소 폴백·복구 시점에 묶고 별도 고빈도 polling을 추가하지 않는다.

공식 KIS 예제에는 주문체결조회 전체/체결/미체결 구분, 주문번호·거래소 선택과 연속조회가 있다.
취소확인수량·잔여수량·원주문번호도 응답 필드로 열거된다. 다만 **필드 존재만으로 정정·부분취소 체인의 최종성을 확정하지 않는다**.
지원할 조합을 공식 계약과 고정 fixture로 입증한 것만 자동 재주문 대상으로 삼는다.
근거가 없는 시장/세션/주문유형은 `unsupported_finality`로 보류한다. 예제의 TR ID를 기존 운영 코드에 무조건 치환하지 않는다.

참고한 1차 자료(2026-09-17 조회, `main`은 변동 가능):

- [KIS 공식 일별주문체결 조회 예제](https://github.com/koreainvestment/open-trading-api/blob/main/examples_llm/domestic_stock/inquire_daily_ccld/inquire_daily_ccld.py)
- [KIS 공식 응답 필드 목록](https://github.com/koreainvestment/open-trading-api/blob/main/examples_llm/domestic_stock/inquire_daily_ccld/chk_inquire_daily_ccld.py)
- [KIS 공식 정정취소가능주문 조회 예제](https://github.com/koreainvestment/open-trading-api/blob/main/examples_llm/domestic_stock/inquire_psbl_rvsecncl/inquire_psbl_rvsecncl.py)

구현 전 해당 공식 버전/응답 계약을 고정한다. 공개 문서 조회만 했으며 실계좌 API 검증은 하지 않았다.

### 4.3 재주문 수량

`남은 목표 = max(0, 최초 intent 목표수량 − 연결된 각 주문의 확정 누적체결량 합)`.

원주문/취소 자식행을 각각 독립 체결로 더하지 않는다. 어댑터가 동일 체결을 한 번만 집계한다.
관련 이전 attempt 전부의 최종상태와 마지막 체결 적용이 확인되어야 재주문한다.
목표10에서 3체결+7취소 확정이면 새 주문7, 전량10체결이면0이다. 보유100이라는 이유로 목표를100으로 확대하지 않는다.
계좌의 확인된 매도가능수량은 추가 상한이다. 같은 종목의 다른 미확정 주문이 있으면 해당 수량을 자유 잔량으로 쓰지 않는다.
같은 청산 intent의 부분체결 누적이 목표에 도달할 때만 stage를 승격한다. 취소 ACK로 stage rollback/완료 처리하지 않는다.

## 5. 체결 적용과 저장

### 5.1 관측·적용 완료 분리

브로커는 `FillObservation`을 전달한다. KIS 누적 방식에서는 OrderRef와 누적체결수량/대금을 보존한다.
기존 `Fill`의 증분 수량·가격은 적용 담당이 **마지막 적용 누적값**에서 계산한다.
누적 평균가를 매번 단순 증분 가격으로 사용하지 않으며, Decimal과 기존 FeeCalculator의 비용 규칙을 유지한다.

관측 watermark와 적용 watermark를 구분한다. 관측을 durable inbox에 인계하기 전 브로커가 유일한 매핑/정보를 버리지 않는다.
큐 적재는 성공 receipt가 아니다. receipt는 `APPLIED | ALREADY_APPLIED | NEEDS_RECONCILIATION | FAILED`다.
같은 누적 수량/대금은 중복, 오래된 낮은 누적값은 이전 상태를 후퇴시키지 않는다. 같은 수량에 다른 대금 등 정정/충돌은 별도 대사 대상으로 보존한다.

### 5.2 단일 commit의 범위

APPLIED는 **경제적 체결 적용과 보호 상태 판정**이 같은 execution version으로 저장·게시됐다는 뜻이다.
receipt는 `protection_status=ready|degraded|exempt`, `execution_version`, `journal_pending`을 별도로 포함한다.
APPLIED만 보고 보호 정상/저널 완료로 표시하지 않는다.

- Portfolio 수량·평단·현금·실현손익과 주문 단위 거래 횟수.
- 비면제 보유의 ExitManager 수량·보호 설정·고점·stage/pending 목표 또는 명시적인 보호 degraded 상태.
- intent/attempt의 적용 누적량·잔여수량·예약 현금/예산.
- 최초 진입 lot 및 초기 위험의 확정/미확정 상태.
- 체결에 연동된 손절 당일 기록·누적 청산/손실 카운터·재진입 제한·V자 재진입 1회권 소모. 체결 키로 멱등 처리하며 동일 transaction에 포함한다.
- 후속 저널 작업을 재실행할 outbox ID.

`RiskManager.on_fill`의 오류 삼킴 후 pending 정리, 스케줄러의 SELL 직접 감산,
BUY 포지션 존재 대기·중복 재등록을 단일 적용 담당으로 이동한다. 기존 유효 포지션을 삭제해 불변조건을 맞추지 않는다.
보호 상태 생성에 실패해도 경제 상태·적용 watermark·확인된 수량을 담은 `protection_degraded` placeholder를 같은 transaction으로 반영한다.
receipt는 APPLIED+degraded이며 실제 체결을 숨기거나 다음 전달 때 다시 가감하지 않는다. 보호 복구는 별도 멱등 명령이고 새로운 체결 적용이 아니다.
비면제 보유의 degraded가 남아 있으면 KR 자동 전략 신규 매수는 보류하고, 확인된 보호 상태/수량에 대한 기존 청산은 §6의 충돌 검사를 유지한다.
경제 상태 transaction 자체가 실패하면 FAILED/NEEDS_RECONCILIATION이며 적용 watermark를 전진시키지 않는다.

### 5.3 영속 저장의 선택

**표준 라이브러리 SQLite의 작은 KR execution store를 선택한다.** 신규 패키지/외부 DB는 필요하지 않다.
독립 JSON 여러 개를 atomic replace하는 방법은 상태와 dedup watermark의 동시 commit을 보장하지 못하므로 선택하지 않는다.

- 로컬 실행 상태 DB는 향후 `~/.cache/ai_trader/execution_kr.sqlite3`에 두고 테스트는 명시적 `tmp_path`를 주입한다. 이번 문서 작업에서 파일을 만들지 않는다.
- 단일 writer, 트랜잭션, WAL + `synchronous=FULL`, schema version과 monotonic commit sequence를 사용한다.
- 디렉터리는0700, DB/WAL/SHM·백업은0600을 기본으로 제한한다. 로컬 파일시스템만 지원하며 운영 파일 경로는 명시 주입/검증한다. 실행 상태는 Git에 포함하지 않고, 정상 종료/backup API를 사용하지 않은 DB 단독 복사를 안전한 백업으로 취급하지 않는다.
- 논리 자료: intents/attempts, 관측 inbox, 적용 누적값, 실행·보호 checkpoint, 후속 outbox. 비밀정보는 저장하지 않는다.
- 새 execution checkpoint는 **실행 상태와 적용 watermark의 정본**이다. 기존 stage JSON은 호환 projection이고 성과 저널/분석 DB는 멱등 후속 작업을 받으며 기존 수익평가 정본 역할을 유지한다. 새 checkpoint와 구 실행 상태 중 '더 최근 같은 것'을 임의로 합치지 않는다.
- core state와 applied marker는 한 transaction으로 저장한다. transaction 후 메모리에 같은 version을 게시하고 receipt를 완료한다.
- commit 전 실패는 미적용 재전달, commit 후 메모리 게시/ACK 전 종료는 checkpoint 재로딩이다. 동일 체결을 다시 가감하지 않는다.
- commit 성공 후 게시 예외/태스크 취소로 프로세스만 살아 있으면 `publication_recovery_required` 장벽을 세운다. 다음 core 명령·거래 POST를 중단하고 durable checkpoint를 재설치해 메모리/DB version 일치 후 재개한다. ALREADY_APPLIED도 게시 version이 일치할 때만 반환한다.
- commit 응답 유실 등 결과 자체가 불명확하면 transaction/commit ID를 조회해 성공 여부를 확정한다. 확인 전 새 증분을 계산하거나 원 관측을 버리지 않는다.
- 새 store를 열거나 쓰지 못하면 성공 receipt·watermark 전진·예약 해제를 하지 않는다. 상태를 보존하고 실행자 불건전 상태를 노출한다.
- 손상/미지원 schema를 빈 DB로 재생성하거나 조용히 legacy 모드로 돌아가지 않는다. 임의 정리·자동 삭제·자동 downgrade는 금지한다.

### 5.4 잠금과 비동기 작업

핵심 실행 변경은 단일 coordinator의 명령 순서로 직렬화한다. 계산은 외부 부작용 없는 후보 상태에서 수행한다.
SQLite 작업은 전용 직렬 writer에서 처리해 이벤트 루프를 막지 않는다. writer 완료 대기 중 별도 명령이 core state를 직접 변경하지 못하게 한다.
메모리 게시에는 짧은 trading lock을 쓰고 pending lock이 남으면 **trading → pending** 순서만 허용한다.
큐 lock을 잡고 trading lock을 기다리지 않는다. 네트워크·LLM·원격 DB·SQLite 완료 대기는 이 메모리 잠금 밖이다.

현재가/관측시각 같은 비영속 quote view와 고점·본전보호·stage·pending 목표 같은 보호 실행 상태를 구분한다.
fill checkpoint 게시가 최신 quote view를 덮어쓰지 않는다. 보호 상태 변경은 가격 콜백도 coordinator 명령으로 제출해 직렬화하며 SQLite 대기 중 직접 갱신하지 않는다.
청산 판단은 일관된 실행 version을 읽고 intent 예약도 coordinator를 거친다. quote 수신 병합이 필요해도 손절 접촉·고점·보호 상태 전이를 버리는 단순 마지막 가격 덮어쓰기는 금지한다.
기존 외부 쓰기 경로를 그대로 두고 '단일 writer'라고 부르지 않는다. `update_position`, sync, SELL stage 변경, pending 해제의 모든 KR 호출점을 인수 목록에 포함한다.

### 5.5 stage·초기 R·저널

- `INITIAL_ENTRY / SAME_ENTRY_FILL / DISTINCT_ADD_ON / RECONCILIATION`을 명시적으로 구분한다.
- 같은 최초 주문40+60: 수량·평단 갱신, 기존 stage/본전보호/고점/pending 목표 보존. 무조건 `register_position` 재호출 금지.
- 실제 별도 추가매수: 기존 추가매수 리셋 정책을 식별된 별도 주문에 한 번만 적용한다. 반복 체결·sync가 재리셋하지 않는다.
- 확정 initial R은 부분매도·sync·재시작으로 덮어쓰지 않는다. 최초 진입 주문이 명시적으로 종결되고 체결 적용이 끝났을 때만 확정한다. 별도 추가 주문은 별도 lot이며 모호하면 canary 제외다.
- 저널/DB/성과 원장·WS 구독·알림은 핵심 적용 이후 outbox 작업이다. 지연이 보호 적용을 취소하지 않으며 저장 실패를 완료로 표시하지 않는다.
- 외부 저널은 실행 키의 멱등 처리/unique 제약 또는 기존 기록 확인 후 재시도를 지원해야 한다. outbox만 두고 원격 쓰기의 exactly-once를 주장하지 않는다.
- 저널이 미완이면 `journal_pending`을 남기며 canary/export에서 불완전 표본을 통과시키지 않는다. `APPLIED`와 `journal_synced`를 구분한다.

## 6. 잔고 동기화·시작·복구

### 6.1 동기화와 체결의 중복 반영 방지

잠금만으로 '잔고100 설치 후 체결60 추가하여160'을 막을 수 없다. 조회 시작 version과 진행 주문 집합을 기록하고 응답 적용 전 다시 확인한다.
조회 사이 상태가 바뀌면 옛 응답을 덮어쓰지 않는다. 미해결 주문/미적용 inbox가 있는 종목은 잔고 수량을 증분 체결과 동시에 별도 정본으로 적용하지 않는다.
현금도 전체 주문과 연결되므로, unsettled 상태에서 KIS 현금을 새 기준으로 설치한 뒤 같은 체결 비용을 다시 차감하지 않는다.

KIS snapshot에 체결 cursor가 없으면 무체결·무미해결 상태를 확인한 뒤 새 전체 snapshot으로 reconciliation 기준선을 만든다.
그 기준선에 포함된 체결과 이후 체결을 구분할 증거가 없으면 `NEEDS_RECONCILIATION`을 유지한다.
로컬 version 일치나 동일 잔고 두 번만으로 거래소의 원자적 snapshot을 증명했다고 주장하지 않는다.
외부 수동 거래/입출금은 별도 adjustment로 기록하며 특정 자동 주문의 체결로 꾸미지 않는다.

신규·기존 종목 모두 `ensure_protection` 대상을 검사한다. 매도 면제는 등록/수량 확인과 별개로 항상 유지한다.
보호 누락은 신규 매수 보류·복구 대기·경보로 처리한다. 같은 종목의 주문 불명 상태에서 중복 위험을 무시하고 보호 매도를 새로 보내지 않는다.
정합한 다른 종목의 보호 청산은 이 종목의 불명 상태만으로 전면 차단하지 않는다. 단 store 자체의 장애는 §1.4대로 별도다.

### 6.2 최초 전환과 재시작

1. 기존 파일을 지우지 않고 읽기 전용으로 보존한다. 최초 store가 없다는 이유로 빈 계좌/빈 pending을 가정하지 않는다.
2. `startup_reconciliation` 송신 장벽으로 KR의 신규 주문·정정·취소 POST를 기본 차단한다. BUY뿐 아니라 복구 전 SELL도 차단한다. 기존 intent·보호 상태·면제·진입 위험과 KIS의 완전한 주문/체결/잔고 증거를 읽기 전용으로 대조한다.
3. 명확한 기존 포지션은 lifecycle을 부여하되 미측정 initial R을 현재 설정으로 채우지 않는다. 모르는 원가/청산 stage도 성공한 복원으로 위장하지 않는다.
4. 로컬 원장에 없는 거래소 주문은 격리 추적한다. 수동 주문을 자동 취소하거나 비슷한 종목/수량의 intent에 자동 연결하지 않는다.
5. 실행 checkpoint·포함 체결 기준선·면제/보호 상태를 저장·게시한 후 정상 모드로 전환한다. 증거가 불완전하면 전환하지 않는다. 1차 구현은 종목별 조기 개방 없이 계좌/시장 복구 완료 후 장벽을 해제한다. 정상 모드 이후의 종목별 충돌 격리와 구분한다.
6. 이후 시작은 execution checkpoint를 우선 복원한다. 미완 inbox/outbox는 이어 처리하고, SUBMITTING/CANCEL_REQUESTED는 조회로 대사한다. POST 재실행으로 복구하지 않는다.

롤백도 DB 삭제/구 JSON 복사로 수행하지 않는다. 미종결 주문·미적용 inbox가 없고 완전한 KIS 대사와 구버전 호환 checkpoint가 확인될 때만 별도 승인된 롤백을 수행한다.
호환성을 확인하지 못하면 승인된 운영 중단/복구 절차를 택한다. 이번 단계에서 운영 롤백을 실제 실행하지 않는다.

## 7. 최종 진입 승인

### 7.1 자동 전략과 기존 예외

| 진입 분류 | 이번 최종 guard | 유지하는 경계 |
|---|---|---|
| KR 자동 전략 신규 매수 | severe 차단, 기존 회복 cooldown, SEPA 14:30+ 마감, 위험 입력 unknown 보류 | 기존 사이징/위험 한도와 kill switch 모두 추가로 충족 |
| KOFR 방어 `safe_asset` | 기존 방어 매수 조건을 유지; alpha 진입 severe 차단을 일괄 적용하지 않음 | 기존 리스크 검사·kill switch·주문 생명주기는 유지 |
| 기존 사용자 수동 경로 | 이번에 새 주문 권한/자동 실행을 만들지 않음; 기존 별도 경로 유지 | 면제/사용자 설정·kill switch·생명주기 추적 보존 |
| SELL·취소 | alpha 신규 매수 guard 대상 아님 | 면제·미확정 중복 방지·수량 정합·kill switch는 그대로 |

분류는 신뢰된 호출 경로에서 전달하는 typed context다. `strategy="manual"` 같은 문자열이나 LLM metadata만으로 예외를 선택할 수 없다.
분류 없는 자동 경로는 보류한다. 발견된 모든 KR `submit_order` 호출점을 검사해 누락·불명 예외를 남기지 않는다.

### 7.2 마지막 await 뒤 검사

- 공통 `FinalDispatchGuard`는 **모든 KR 거래 POST**에 적용한다. 마지막 대기 뒤 durable attempt/송신 소유권·store 건강성·메모리/DB 게시 일치·startup 장벽·동일 intent 충돌을 검사한다. SELL/취소도 예외가 아니다. 실패 시 HTTP0이며, 이미 송신한 것은 NOT_SENT로 바꾸지 않고 UNKNOWN으로 대사한다.
- 전략용 `FinalEntryGuard`는 공통 송신 조건 위에 자동 전략 BUY 조건을 추가한다. 정상 다른 종목의 SELL 허용과 전역 store/startup 장애를 혼동하지 않는다.
- 입력: 단조 version, 실제 관측 `as_of`, **최신 관측 시도의 순서·성공/결측/실패 상태**, 거래일, level, recovery_until, 검증 시각, 진입 분류.
- 기존 급락 감지기와 당일 어댑터 입력을 재사용한다. 임계값을 다시 만들지 않는다. 동일 입력 계열의 최신 시도를 기준으로 하며, 더 최신인 결측/실패가 있으면 unknown을 게시한다. 이전 정상값은 참고값으로만 남기고 신규 매수 승인에 사용하지 않는다. 동일 시각 충돌은 더 보수적인 상태/unknown으로 보류한다.
- 당일 관측이 없는 기본 normal·전일 상태·미래시각·파싱 실패는 unknown이다. 이번에 새로운 분 단위 TTL 숫자는 추가하지 않는다.
- SEPA 마감은 매번 현재 KST를 읽는다. 최초 함수의 `now`를 재사용하지 않는다. cooldown은 기존 5분 규칙이다.
- 배치의 종목별 검사는 조기 스킵용이다. 실제 승인 검사는 KIS 연결/토큰/hashkey/limiter 대기가 끝난 **거래 POST 시작 직전**에도 시행한다.
- guard는 네트워크 없는 동기 검사다. 앞선 분석 metadata가 아니라 현재 snapshot을 읽으며 거부 시 HTTP 호출0이다. `NOT_SENT`는 **이번 명령 attempt만** 미송신됐다는 뜻이다.
- 취소·정정 attempt의 NOT_SENT/REJECTED는 원 주문의 예약·pending·청산 목표를 해제하지 않는다. 최초 신규 주문도 같은 intent의 다른 미종결 attempt가 없고 저장소가 정상이며 미송신 처리가 durable하게 확정된 경우에만 **그 attempt에 귀속된 예약**을 coordinator transaction으로 해제한다. 저장 장애 때는 해제를 보류한다. 이미 송신된 다른 attempt를 NOT_SENT로 덮어쓰지 않는다.
- 이 보장은 로컬 송신 승인 시점 기준이다. 이미 네트워크에 넘긴 주문을 시장 상태 변경으로 소급 취소할 수 있다고 주장하지 않는다. HTTP 라이브러리 내부 대기·서버 접수 시각은 별도 한계다.
- pending intent 저장을 기다린 사이 위험 version이 바뀌어도 마지막 검사로 잡아야 한다. 종목 조회·분산 대기·LLM·hashkey·limiter 각각에 사건 장벽을 넣어 검증한다.

EntryPlan 전체 shadow→enforce, 지정가 도입, 실제 체결 가격 상한 보장은 이번에 포함하지 않는다.

## 8. 관측과 안전 운용 계약

기존 health/하트비트에 다음 상태를 별도로 보고한다. 프로세스 생존이나 HTTP 성공을 대사 완료로 표시하지 않는다.

- 미종결/UNKNOWN intent 수와 최초 미해결 시각, last successful reconciliation.
- observed/applied watermark 및 unapplied inbox 수, commit sequence, store 상태.
- 보호 누락/수량 불일치 종목 수, journal/outbox pending 수.
- 최종 guard 차단 사유별 수(위험/마감/결측/분류), 조회 incomplete/unsupported 수.

기존 알림 전송기를 사용해 새 UNKNOWN/보호 누락/store 장애에 즉시 경보하고 동일 원인의 반복은 중복 억제한다.
경보 억제와 상태의 시간 기반 자동 해제는 다르다. alert/health 실패가 실행 안전 상태를 해제하지 않는다.
운영자 안내는 주문 참조·확인할 상태와 근거를 제공하되 계좌번호/자격/원문 payload를 노출하지 않는다.

## 9. 인수 시험 — 실패를 먼저 재현하고 같은 시험으로 확인

아래는 **구현 후 만족해야 하는 조건**이며 이번 문서 작업의 통과 결과가 아니다.
네트워크/운영 파일은 `tests/conftest.py`로 차단하고 fake broker·tmp_path·주입 시계·asyncio.Event 장벽을 사용한다.
sleep을 늘려 우연히 통과시키지 않는다. 실제 엔진 큐/핸들러/ExitManager/저장소를 연결하고 외부 경계만 대체한다.

| ID | 입력/실패 주입 | 기대 결과 |
|---|---|---|
| C1 | 보유100, 목표SELL10, 취소False/예외/timeout | 추가 주문0, 원 주문·예약·청산 목표 보존 |
| C2 | 취소ACK 후 최종조회 실패/빈 행/부분페이지 | 종결 확정0, stage 해제0, 원인 표시 |
| C3 | 목표10, 취소 전후 합계5체결+5취소 확정 | 체결5 한 번 적용, 다음 주문5만; 전량체결이면 다음0 |
| C4 | 서로 다른 계좌/일자/거래소의 동일 주문번호, 늦은 이전 응답 | 교차 반영0, 새 attempt 해제0 |
| C5 | POST 직전/직후/ACK 저장 전 프로세스 종료 | 재시작 POST 재전송0, 상태 UNKNOWN 대사 |
| C6 | 조회 다음 페이지 실패/상한/순환/지원 안 되는 취소 체인 | incomplete/unsupported, 자동 재주문0 |
| C7 | 엔진 폴백·스케줄러가 동시에 같은 intent 재처리 | 송신 소유자1, 같은 목표의 중복 attempt 전송0 |
| C8 | OPEN SELL10의 취소 attempt가 최종 guard에서 미송신/거부 | 취소 HTTP0, 원 SELL10 예약/pending/목표 보존, 추가 SELL0 |
| F1 | 실제 큐로 BUY40+60 | Portfolio100/Exit100, 현금·수수료·카운트 정확히 한 번 |
| F2 | F1 뒤 SELL40, 이후 SELL60 | 첫 매도 뒤 Portfolio60/Exit60, 전량 이후에만 상태 제거 |
| F3 | 누적40→40→100→과거40→재시작100 | 적용40,0,60,0,0; 대금 충돌은 별도 대사 |
| F4 | sync→fill, fill→sync, 조회 중 fill, restart→replay | 수량·현금 이중 반영0, 불명 cutoff는 정상 선언 금지 |
| F5 | core 계산/보호 등록/DB commit/메모리 게시/ACK 각 실패점 | 성공 receipt 위장0, watermark 유실0, 재처리 가능 |
| F5a | commit 성공 후 게시만 실패, 프로세스 생존·재전달·다음 주문 | checkpoint 복구 전 POST0, DB/메모리 수량100, 중복 가감0 |
| F5b | BUY 체결의 보호 생성 실패→재전달→보호 복구 | APPLIED+degraded 명시, 경제 상태 한 번 적용, 복구가 재가감하지 않음 |
| F6 | FIRST/SECOND/TRAILING 중 동일 주문 후속 체결·sync | stage/BE/고점/pending 목표/확정R 보존 |
| F6a | fill DB 대기 중 더 높은 quote·BE 활성화 조건 수신 | 처리·재시작 후 고점/BE 후퇴0, 체결수량 정확 |
| F7 | 별도 추가매수·부분체결 후 잔여 취소·저널 실패 | 추가매수 정책1회, R 확정 조건 준수, outbox 재시도 중 보호 유지 |
| F8 | 기존 비면제 포지션의 Exit 누락, 면제 종목 | 비면제 복구 또는 degraded 명시, 면제 자동매도0 |
| F9 | 손절/V자 재진입 체결 commit 직후 종료·재전달 | 재진입 제한·1회권 소모 복원, 손실/청산 카운터 중복 증가0 |
| G1 | quote/분산대기/LLM/hashkey/limiter 중 normal→severe | 자동 전략 BUY HTTP0; 기존 SELL 경로는 신규매수 guard로 막지 않음 |
| G2 | 대기 중 cooldown 시작, SEPA14:29:59→14:30 | BUY HTTP0, 함수 시작 시각 재사용0 |
| G3 | 전일/결측/미래 위험 입력, UTC/KST 호스트 | unknown 보류, KST 경계 동일 |
| G3a | 당일 normal 성공→다음 관측 실패→후속 성공 | 실패 뒤 BUY0, 다음 유효 관측 후 기존 조건으로 재판정 |
| G4 | KOFR/수동/SELL, metadata로 manual 위장 | 지정 예외의 기존 정책 유지, 임의 우회0 |
| G5 | BUY/SELL/취소 attempt 저장→hashkey/limiter 대기 중 store 장애 | 모든 새 거래 POST0, 이미 송신된 요청은 UNKNOWN 보존 |
| R1 | 최초전환·손상store·schema 불일치·legacy 상태 충돌 | 빈 초기화/임의삭제/자동 downgrade0, 차단·복구 근거 표시 |
| R1a | 시작 시 주문조회 정체 중 급락·SELL 이벤트 발생 | 복구 장벽 해제 전 POST0, 복구 후 확인된 기존 정책으로만 실행 |
| R2 | 공통 ExitManager/브로커 계약 변경 | US 기존 정상 경로·면제·stage 회귀 없음; US 안전 완료로 보고하지 않음 |

기존 시험은 `test_entry_risk_wiring.py`, `test_entry_risk_lifecycle.py`, `test_sync_portfolio_characterization.py`,
`test_exit_manager_characterization.py`, `test_regime_llm_inputs.py`, `test_t10_regime_path.py`, `test_codex_sync_boundary.py`를 재사용한다.
특히 wiring의 fake emit/ExitManager만으로 F1/F4를 대체할 수 없다. 브로커 전송·coordinator·crash-recovery 통합시험을 추가한다.

전체 verify를 UTC/KST에서 순차 실행하고 문법·비밀정보 검사·격리 위반0을 확인한다.
기존 live/backtest 손절·익절 parity xfail2를 이 단계 성과로 없앴다고 주장하지 않는다.

## 10. Plan–Do–See와 단계 경계

### Plan — 이번 산출물

- 기준 SHA·결함·변경 의미·식별/저장/동기화/최종승인 규칙과 §9 인수 조건을 확정한다.
- 이 설계 승인 후에만 세부 구현 계획을 작성한다. 이번 커밋에는 거래 코드·설정·실행 상태를 포함하지 않는다.

### Do — 승인 후의 구현 분할 원칙

1. 증거/상태 계약과 저장·복구 시험을 먼저 만든다. 기존 실패를 실제 메서드 조합으로 RED 고정한다.
2. 주문 취소·체결 적용은 별도 새 모듈/시험에서 병렬 구현할 수 있지만 같은 core/scheduler 파일은 동시에 편집하지 않는다.
3. 부모 통합 담당이 단일 적용·sync·최종 guard를 순서대로 연결한다. 기능 일부만 연결된 상태를 운영에 배포하지 않는다.
4. 구현자와 다른 리뷰어가 돈 경로 및 restart/실패 주입을 검토한다. 문서상의 희망이 아니라 코드와 실행 결과로 판정한다.

역할별 권장 배치: 일반 시험·문서 Terra/high, 주문·체결 구현 Astra/high, 독립 돈 경로/통합 리뷰 Astra/xhigh.
실제 사용할 수 없는 모델/슬롯은 대체와 미실행을 기록한다. 이 배치는 거래 봇 내부 LLM 설정 변경이 아니다.

### See — 단계 종료와 운영은 구분

- 오프라인 계약/경합/복구 시험과 독립 리뷰 통과 시 **KR 1단계 구현 검증 완료**로만 보고한다.
- KIS 최종성 지원 범위는 명시적으로 열거한다. 미지원이면 자동 재주문 보류가 정상 결과이며 미지원 기능을 완료로 세지 않는다.
- 실제 운영 설치·재시작·상태 인계는 별도 사용자 승인 후 장외 절차로 수행한다. 승인 전에 운영 DB/상태를 읽거나 만들지 않는다.
- 시험을 위해 매수를 만들거나 Toss 관측을 변경하지 않는다. 수익성·canary 승격은 별도다.

### US와 다음 단계

US는 직접 스케줄러 경로이며 현재 취소 미확정 해제, 부분체결 후행 반영, POST 재시도 문제가 있다.
별도 후속 설계/구현에서 같은 생명주기·체결 적용 계약을 US 어댑터로 검증해야 한다.
KR 변경만 끝내고 KR+US 전체 안전성 개선 완료로 보고하거나 US 활성화 근거로 사용하지 않는다.

후속 목적 정합성 작업: 느린 LLM을 청산 소비 경로에서 분리 → 후보/전략 자격·가격/시점 계약 통합 →
주간 배분 포함 공통 승격 + 비용 차감 KODEX200·노출 대조군·실제 기간 분리 검증.
현재 큐의 LLM 지연 문제는 1단계 후에도 남으며, 이번 receipt가 저지연 청산까지 보장하는 것은 아니다.

## 11. 문서·호환성 정리 대상

구현 시 다음 기존 규칙은 새 계약과 함께 개정한다. 이 설계 문서만으로 현행 동작을 바꿨다고 기록하지 않는다.

- `CLAUDE.md`: '예외 시 반드시 clear_pending' → 확정된 미송신/거부/종결만 해제, UNKNOWN은 보존.
- KR `_api_post`/외부 API 문서: 실패 후 무조건 pending 해제, 거래 POST 재시도 예외를 명시적 command 상태로 대체.
- KR stale/orphan pending 및 ExitManager pending verifier: 시간·미체결 부재만으로 stage/예약 해제 금지.
- 위험 원장 문서: 로컬 open 목록 부재를 주문 완료로 보는 규칙을 최종 증거+체결 적용 완료로 교체.
- 기존 sync 건강성 타임아웃과 execution UNKNOWN을 분리한다. 전자의 시간 기반 복구가 후자의 차단을 해제하지 않는다.
- 아키텍처/리스크/외부API/운영 runbook/monitoring/CHANGELOG/문서 인덱스에 실제 구현·미지원·운영 상태를 구분한다.

## 12. 이번 문서 작업의 증거

- 최신 원격 main 조회: `465a029`; 별도 `feature/engine-safety-design-20260917` 작업공간. 운영 checkout은 수정하지 않았다.
- 변경 전 기준선 KST verify: **1810 passed / 2 known xfailed / 1 기존 pykrx warning, 85.02초**, 문법·비밀정보 패턴 검사 통과, 운영 상태·외부 네트워크 접근 시도0.
- 위 숫자는 기존 코드 기준선이다. §9의 신규 계약 시험을 구현/통과했다는 의미가 아니다.
- 주문/취소와 체결/동기화를 Astra/high 두 에이전트가 병렬 읽기 전용 조사했다. 추가 Terra 및 xhigh 생성은 도구 슬롯 제한으로 실행하지 못했다. 시험 목록은 부모가 확인한다.
- 문서 작성 후 UTC verify: **1810 passed / 2 known xfailed / 1 기존 pykrx warning, 86.42초**, 문법·비밀정보 패턴 검사 통과, 격리0. 거래 소스/테스트는 기준선과 같으며 이후 리뷰 수정도 문서에만 한정된다.
- 두 Astra/high의 1차 문서 교차 리뷰: Important7·Minor1(고점 소유권 지적은 중복). 게시 복구 장벽, 경제 적용/보호 degraded 분리, 시작 시 SELL 장벽, 보호 상태 단일 소유, 체결 연동 재진입 제한, 공통 최종 송신 guard, 최신 결측 우선 규칙을 인수 조건과 함께 반영했다.
- 재리뷰에서 위 항목 해소를 확인했고, 두 리뷰어가 같은 추가 Important1건(취소 미송신을 원 주문 예약 해제로 오해할 문구)을 지적했다. §7.2의 attempt별 예약 소유권·저장 정상 조건과 C8로 보완했다. 이 문서 리뷰는 구현 인수·xhigh 리뷰를 대체하지 않는다.
- 마지막 §7.2/C8 한정 재확인에서 체결 담당 리뷰어가 남은 문서 차단 사항 없음을 확인했다. 상세 설계의 사용자 확인은 별도로 남아 있다.
- self-review로 모든 KR 호출의 typed 결과 보존·단일 송신 소유자·로컬 저장 권한/backup 경계도 보완했다. 운영 SSH/systemctl/배포/재시작·자격/계좌 API·주문·설정·킬스위치 변경0.
