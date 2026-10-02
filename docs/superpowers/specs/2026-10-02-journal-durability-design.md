# 37차 — 체결 identity와 거래 장부 commit 근거

목적은 누락·중복된 거래 기록 때문에 종목 선정/진입/청산의 순수익을 잘못 판단하지 않도록 하는 것이다.36차 읽기 전용 대사 도구가 드러낸 미래 기록 공백을 수정한다. 기준 main `0552f7648c64de63d91fe93e508ccdc3bd42ce71`. 사용자의 순차 Plan→Do→See/통합 리뷰 지시에 따라 설계·구현·검증을 수행한다.

## 선택한 접근

주문번호만 컬럼에 넣는 방법은 부분 BUY 누락과 SQL 일부 성공/중복을 해결하지 못한다. 전체 장부를 새 저장소로 교체하는 방법은 이번 목적보다 넓다. 기존 TradeJournal/TradeStorage를 유지하고 **식별된 KR 체결만 거래 단위 DB transaction과 명시 receipt**로 처리한다. 식별 정보 없는 기존 호출은 호환 경로로 남겨 내구성 증거로 승격하지 않는다.

## 계약

- Fill에 `account_scope`, `order_date`(YYYYMMDD), `kis_order_no` 문자열을 추가한다. 실제 KIS의 검증된 observation과 원장 scope에서 채우며 종목이나 현재 날짜로 추정하지 않는다. `execution_id`는 기존 원장의 증분 ID다.
- 새 `ExecutionIdentity(account_scope:str, order_date:date, kis_order_no:str, execution_id:str)`와 `ExecutionWriteReceipt`를 `src/data/storage/execution_journal.py`에서 정의한다. receipt status는 `pending|committed|unavailable|failed|unknown`, reason은 민감 원문 없는 코드다.
- `TradeJournal`/`TradeStorage.record_entry/record_exit`에 선택적 `execution_identity=None`, `execution_time=None`을 추가한다. identity는 전체 필드가 유효해야 한다. 식별된 모든 BUY 증분이 같은 trade의 수량/평단과 개별 이벤트에 반영된다. SELL은 기존 포지션 trade ID와 정확히 연결될 때만 기록하며 종목으로 옛 거래를 추정한 직접 SQL을 식별 경로에 사용하지 않는다.
- 같은 identity와 불변 체결 사실의 재호출은 JSON·메모리·DB 모두 중복 적용하지 않는다. 다른 사실은 거부한다. JSON에 실행 signature를 보존해 다시 읽은 뒤에도 같은 실행의 수량/PnL을 더하지 않는다. 자동 생성 시각은 지문에 넣지 않으며 명시 체결 관측 시각은 보존한다.
- TradeStorage는 `get_execution_receipt(account_scope, execution_id)`로 비차단 상태를, `async lookup_execution_receipt(account_scope, execution_id)`로 실제 DB만의 증거를 제공한다. 캐시 합성 이벤트는 commit 확인으로 사용하지 않는다.
- 한 execution의 부모/요약/event/필요 상태 SQL은 불변 batch 하나이며 한 PostgreSQL transaction 안에서 수행한다. `trade_events`의 scope+execution ID unique와 내용 지문을 비교한다. 이미 있는 동일 실행은 요약 UPDATE도 건너뛴다. 충돌 시 rollback한다. 이전 미확정 batch를 건너뛰어 뒤의 누적 요약만 commit하지 않는다.
- 식별 batch 실패/불명은 자동 큐 재삽입하지 않는다. 명시 재호출에서도 predecessor를 확인하며, legacy SQL의 재시도는 현재 큐 항목 안에서 순서를 유지한다. 중간 실패는 전체 rollback, COMMIT 응답 유실은 `unknown`이며 DB 원본 재조회로만 committed 판정한다. 종료는 신규 enqueue 차단 뒤 앞선 항목을 처리하고 writer를 닫는다. sentinel 뒤 재등록하지 않는다.
- 기존 데이터에는 identity를 추정 보충하지 않는다. 신규 nullable 컬럼/partial unique/check migration을 transaction으로 수행한다. DB 미연결·schema 오류·queue 실패·JSON 실패를 committed로 표시하지 않는다.

## 엔진 연결과 실패 경계

호출 사실 signature와 별도로 전체 batch(summary/event/predecessor)의 digest를 JSON·DB에 보존해 재호출/재시작/commit에서 검사한다. 후속 메타데이터/legacy 날짜파일 쓰기도 공통 atomic 저장을 거친다. 식별 SELL은 매도 직전 `avg_entry_price`가 유한 양수여야 하며 누적 매수평단으로 추정하지 않는다.

로드·동일/새 실행·파일 저장 전에 바깥 회계 누계가 최신 검증 summary와 같은지 확인하고 registry key/계좌 범위/거래/이전 실행 순서를 검증한다. context/review/갱신시각은 별도 변경 가능한 필드다. 기존 진입·청산·KR 손익 보정이 식별 거래를 덮지 않으며, DB의 식별 행을 실행 registry 없는 legacy 객체로 복원하지 않는다. registry가 없거나 최근 JSON 로드 범위 밖이면 자동 복원으로 보충하지 않고 별도 복구 근거가 필요하다. 선정 점수는 요약과 개별 이벤트에 보존한다.

현재 session의 실행 원장 projection에서 미완료 handoff 개수를 계산한다. 이 대기만 있을 때 BUY는 전역, 분할 SELL은 같은 종목을 보류하며 기존 원장 fault/prior_unclean은 기존 더 강한 보류를 유지한다. SELL snapshot에 trade ID가 없으면 해당 시점의 보유 객체에서만 보강한다. 현재 포트폴리오의 같은 종목으로 재귀속하지 않는다.

Scheduler는 청산 상태/재진입 제한/구독 등 기존 후처리를 한 번 수행한다. 저장 확인은 비차단 조회하며 pending 중 후처리를 재실행하지 않고 다른 체결의 보호 처리를 계속한다. 식별된 KR 체결은 DB committed 확인 후에만 기존 `handoff_returned`와 broker ack로 완료한다. 미확정/실패는 복구 필요로 남겨 신규 BUY/분할 SELL을 보류하되 기존 전량 보호 SELL 경로를 유지한다. 적용된 Fill을 다시 메모리에 반영하지 않는다.

v1 실행 원장 형식/과거 receipt 의미는 바꾸지 않는다. 옛 `handoff_returned`나 새 DB commit이 broker baseline 포함 여부·계좌 대사 완료를 뜻하지 않는다.35차 이전 데이터와 식별 없는 보호 체결은 여전히 별도 근거가 필요하다.

## 검증/범위

실제 임시 PostgreSQL cluster에서 Unix socket만 열고 TCP는 비활성화하며 fsync를 유지한다. 기존 서비스·DB·자격증명을 사용하지 않는다. 합성 부분 BUY/SELL, 중복/충돌, transaction rollback, COMMIT 응답 유실, 재시작 재조회, shutdown을 검증한다. DB 바이너리 없는 환경의 시험 생략을 실제 DB 통과라고 표시하지 않는다. 브로커·스케줄러는 합성 체결과 임시 원장/JSON으로 확인한다. 운영 배포/재시작/주문/API·실제 계좌 자료 접근, 설정/킬스위치/예약 변경은 범위 밖이다.
