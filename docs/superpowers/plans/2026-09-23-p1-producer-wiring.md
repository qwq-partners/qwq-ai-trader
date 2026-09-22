# P1 S2~S5 보호 생산자·배선 실행 계획

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. 기존 P1 계획의 후속 실행이며, 사용자 "니가 판단해서 진행해"에 따라 남은 정책과 제한된 리뷰 예산을 coordinator가 결정한다. 재승인 대기로 같은 단계를 반복하지 않는다.

**Goal:** 기존 시세 이벤트를 단일 보호 생산자로 연결하고, 개발 브랜치에서 실제 engine/gateway/owner 인수까지 검증한다. 운영 설치·main 통합은 목표가 아니다.

**Architecture:** S1의 복구 반환과 S1′의 episode ID를 소비한다. 생산자 작업은 공유 lock·shield·기존 runtime command scope로 직렬화/배수한다. S3 engine → S4 factory → S5 주석을 순차 통합한다.

**Tech Stack:** Python asyncio, Decimal, 기존 SQLite 임시 store·pytest 격리 fixture.

**Spec:** `docs/superpowers/plans/2026-09-22-p1-protective-sell-parity.md` §3/§5/§6-1 이후, `docs/operations/codex-handoff-p1-2026-09-22.md`. 아래 처분이 충돌 지점에 한해 우선한다.

## 위임받은 결정·예산

- D1: 정규장 `regular` 보호 SELL은 MARKET, `closing` 15:20~15:30은 LIMIT. 취소/시장가 폴백을 새로 만들지 않는다. 시장가 체결 가격은 보장하지 않으며 슬리피지가 한 틱에 제한된다는 가정은 폐기한다. closing 미체결의 일자 차단22는 유지한다.
- D2: 보유기간은 ExitManager 영업일 정본. 달력일 중복 청산은 이식하지 않는다.
- D3: 결정 없는 틱의 owner 쓰기 간격은20초로 구현·측정하고, 손절/익절 결정은 즉시 기록한다. 고점 손실은 8종목 합성 입력으로 미스로틀 대조와 비교한다. 손실이 없다고 미리 주장하지 않으며 운영 채택은 별도다.
- D4: 외부 Claude는 도구 없는 `claude-opus-5`/xhigh, 총 최대8호출(대조/재시도 포함), 실행기 회당 CLI 예산 $5·설정상 합계 $40, 호출당 절대600초·시작120초·정체180초. 비용이 미관측이면 unknown. 실패한 동일 요청 재시도는 최대1회이며 인증/과금/실모델불일치/안전거절은 우회하지 않는다. 상한 소진 시 무단 증액하지 않고 해당 게이트를 유지한다.
- D5: native 구현 Astra/high(45분), 독립 어려운 재현 Astra/xhigh(30분), 기계적 문서 Luna/medium 또는 coordinator 통합. 실제 메타데이터가 없으면 미검증이다. 최종 범위 통합 리뷰는 독립 Astra/xhigh, live S3/S4는 실제 모델이 확인된 교차 공급자 승인도 필수다.
- D6: 실계좌 스모크·초기 인계·P2 late-fill/side 잠금·main 이행·운영 설치는 계속 범위 밖이다. 권고 정책 결정은 실주문·배포 권한으로 확대하지 않는다.

## Global Constraints

- 개발 정본 `feature/engine-safety-design-20260917`, 시작 `6394d22`. 운영 checkout `d337494` detached는 변경하지 않는다.
- main PR/merge·운영 SSH/systemctl/배포/재시작·실 API·주문·설정·킬스위치·`.env`·Toss grant 변경 금지. 거래·잔고는 KIS 정본.
- 새 durable schema·trading_ready 강제·MODIFY·CANCEL 송신·만료/chain 최종성 가정·전역 게이트 범위 축소 금지. 보호 계산에 `_owner_ready`/gateway.unresolved_symbols() 호출 금지.
- 한국어 주석·로그·문서, Decimal, 금융값 `x or default`/falsy 판단 금지. 기존 legacy 본문은 가드·주석 외 바이트 동일하게 유지한다.
- CLAUDE.md 원문에는 기존 민감행이 있어 작업자는 읽지 않는다. coordinator가 마스킹한 규칙만 전달한다. `.env`, `~/.cache/ai_trader*`, `logs/`, `journalctl`, 계좌/토큰/실응답 접근 금지.
- coordinator + native/external 작업자 총3명 이하. writer는 격리 worktree·파일별1명·재위임 금지. 전체 suite는 작업자 전원 종료 뒤 coordinator만 UTC→KST 직렬, 단위 pytest 동시 최대2개, `tests` 경로 필수.
- 작업자 full suite 지시는 위 호스트 제약으로 대체한다. 구현자 단위/관련 검증 → 독립 재현 → coordinator 전체 검증 전에는 통합 완료가 아니다.

## 사전 인터페이스 처분

| 경계 | 사실 | 처분/대가 |
|---|---|---|
| S1→S2 resume | `(symbol, disposition, decision)`만 반환하며 원 admission을 삭제 | 호출 전 admission을 복사하고 반환 종목과 유일 매칭해 원 command/intent/price를 보관. 모호하면 거부. 새 runtime 반환/schema 없음 |
| S1→S2 pending | 정상 보류도 intent 미등록이면 orphan처럼 보임 | 보류 결정이 있는 종목은 release에서 제외. 원 intent/결정을 보관한 뒤 다음 admission을 재개; 첫 송신 성공을 다음 재개의 전제로 삼으면 전역 source 장벽과 교착하므로 금지 |
| S2→engine | `_submit_signal`은 정상 반환해도 주문이 없을 수 있음 | await 완료를 송신 성공으로 세지 않음. 원 intent에 연결된 실제 owner attempt 증거로 인계 판정. 증거 없으면 보존, terminal/미체결은60초 재시도 규칙으로 분리 |
| S2→shutdown | runtime은 생산자 자체 task를 모름 | shield 내부 작업의 첫 await 전에 기존 `with runtime.command_scope():`를 열어 lock 대기~submit까지 추적. `_protection_tasks` 등록은 자기 장벽을 만들므로 금지. 강한 task 참조·취소 후 예외 회수·종료 거부를 시험 |
| S2→S3 | 현재 engine은 LIMIT/수량 fallback | S2는 실제 runtime·gateway와 경계 어댑터로 검증, 실제 engine의 MARKET/수량 인수는 S3에서 마감. S2 통과를 실배선 통과로 보고하지 않음 |
| S2→S4 관측 | producer.health()를 runtime health가 현재 읽지 않음 | S4에 runtime.py의 미배선 기본 None·읽기 전용 health 투영만 허용하는 최소 예외. 상태 reducer/게이트 변경0, live 파일은 factory.py 한 곳 |
| S3 기존 시험 | 독립 gateway 인수 E1은 직접 가격 갱신이 예외를 낼 것을 요구 | S3의 no-op 계약과 무수정37 GREEN은 양립 불가. 해당 단언만 owner·live 상태 무변경으로 바꾸고 다른 writer 거부 단언 유지. 실제 MARKET_DATA의 전략 도달은 신규 시험으로 증명 |
| S2/S3 병렬 | S3는 고정 SignalEvent metadata와 기존 실제 engine 하네스로 독립 구현 가능 | 같은 base2ddacf4, 별도 worktree·파일 소유로 구현 병렬 허용. S2→S3 승인 확인 뒤 두 후보를 하나의 개발 통합 후보로 묶고 UTC/KST 전체 검증을1쌍 실행. 각 후보 독립 리뷰/관련 시험은 별도 유지하고 전체 통과 전에는 둘 다 통합 완료로 보고하지 않음. S2 계약 변경 시 S3 재검증 |
| S2 재시작 cooldown | durable attempt에는 종결 시각이 없어 메모리의 거부 시각이 소실됨 | 기존 보호 intent의 현재 재시도 대상임이 증명된 확정미체결에 한해 첫 관측에서60초를 새 기산. 옛 체결 익절은 새 손절을 막지 않으며 UNKNOWN은시간으로풀지않음. 최신 episode를 증명할 수 없으면 명시 보류,새schema없음 |
| S3 거래일 | legacy 시간 가드는 KRSession의 주말·휴장일 검사도 포함하나 requests._session_at에는 없음 | 보호 전용 helper도 기존 utils.session.is_kr_market_holiday에 runtime KST날짜를 전달해 방어 유지. 새달력·조회 없음. 시간 라벨과 gateway session_guard는 그대로 두고 await뒤 거래일/세션 재확인 |
| S2 EOD 신선도 | quote 전에 EOD를 반환하면 원관측·신선도 검증도 건너뛰어 오래된 WS가 전량 청산 가능 | quote0은 durable quote/일반 보호계산0이라는 뜻. EOD 전에도 기존 원관측 검증·읽기 전용 freshness 경계를 재사용, 시장 시각 합성/새 진입 proof 게시 금지 |
| S2 복구 intent 재시작 | 원 admission에서 복구한 full SELL ID는 pp-i 접두어·pending owner가 없을 수 있음 | 재시작 재시도 식별은 UUID 접두어만으로 끝내지 않음. 기존 durable 보호 결정/원 intent 연결 근거를 대조해 같은60초 계약 적용, 근거 모호함은 명시 보류 |
| S2 재생성 미제출 pending | quote 완료·submit 보류 뒤에는 admission/intent가 없어도 outbox와 pending에 원결정 증거가 남음 | 증거가 일치하는 pending은 orphan 해제 제외·복구 필요로 관측. 원 admission 없는 outbox를 자동 소비하거나 재주문하지 않음 |
| S2 다른 부분 매도와 새 결정 | 기존10주 SELL 적용 전에 새 full100을 보관하면 잔량90 뒤 영구 수량 불일치; 일반 손절도 같은 경계 | 다른 보호 pending/미해결·예약 SELL이 있으면 충돌하는 새 결정을 만들지 않음. 일반 경로의 순수 preview는 유지하고 결정이 있을 때 durable quote/보관 전에 검사; 무결정 quote 관측은 유지. 기존 경제적용 완료 뒤 다음 실제 틱에서 새90주 계산. 보류100의 자동90 보정·시간 폐기 금지. 미체결 BUY/late-fill 문제는 P2 잔여 |
| S2 정상 대기 관측 | ACK 미체결을 매 틱 실패로 기록하면 실패 경보가 무의미해짐 | 실제 ACK·일관된 예약 대기는 pending으로 분리. UNKNOWN/충돌/불명/다른 종목의 진짜 실패는 성공으로 덮지 않음 |
| S2 지표 타입 | 기존 batch 캐시 ma5/prev_low는 float이며 WS DTO의 OHLC는 Decimal | 캐시의 명시 결측 또는 양의 유한 숫자만 정규화해 수용. bool/NaN/Inf 거부, 새 float 연산·캐시 조회 없음. 원 WS 타입 검증 보존, REST OHLC도 무조건 문자열화하지 않음 |
| S2 종료 전 미시작 task | shield 내부 scope 실행 전에 shutdown이 끝날 수 있음 | 미시작 task는 closing 거부·상태/송신0·최종 회수로 검증. current-task 소유 scope를 caller에서 빌려 넘기지 않음 |
| S3 D1 엔진 방어 | 최초 S3는 regular LIMIT도 허용해 생산자 정책에만 의존 | 보호 helper에서도 regular MARKET/closing LIMIT를 엄격히 적용. regular→closing 시험은 호가가 아니라 기존 lock await 경합으로 유지 |
| S3 손상된 보호 표식 | 명시 protection_intent_id 키가 있으나 값이 무효이면 legacy 수량 변환 경로로 흘러감 | attach SELL에 명시 키가 있으면 유효한 깨끗한 ID만 허용하고 무효는 로그·미송신. 키 없는 일반 SELL/미attach legacy는 본문 불변 |
| S3 cooldown | 기존 종목 단위30초 신호 cooldown 유지가 실제 손절 지연을 일으킬 수 있음 | 이번 단계에서 키/한도를 바꾸지 않고 거부 사유를 관측. S2 원결정 보류/다음 틱 재시도로 연계. 운영 전 지연 검토 과제로 명시 |

## Review Focus

### 호출7 이후 순서 보완 (2026-09-23)

외부 fix2의 조건부 승인 뒤 독립 실제 재현에서 두 후속 결함을 확인했다. phantom/공백
감사 종목이 전역 보류를 우회했고, stale/abandoned 배수 뒤 생산자 heartbeat가 성공으로
표시됐다(기존 runtime 실패 래치·degraded는 남음). S2 fix3에서 이 범위만 수정한다.
외부 상한8호출은 늘리지 않는다. 마지막8호출은 **S2 추가 diff와 S4 설치 배선을 함께**
검토하며, 둘 중 하나라도 승인되지 않으면 해당 후보의 정본 feature 통합은 차단한다.

따라서 S2 fix3 native 승인·S3 양측 승인 뒤에는 임시 격리 통합 후보에서 전체 UTC/KST를
먼저 실행하고, 이 후보 위에서 S4를 개발한다. S2/S3를 정본 feature에 먼저 병합하던
순서만 최종 교차 승인 뒤로 미룬다. S4 독립 변이/리뷰·교차 승인·전체 UTC/KST도 그대로
필수다. 임시 후보 commit은 검증 대상 고정을 위한 것이며 완료/통합/운영 승인이 아니다.
현재 첨부 계약에 없어서 생긴 반복 질의를 줄이기 위해 마지막 입력에는 `_submit`·순수
preview·owner deepcopy·감사 종류/수명 및 실제 설치/종료 경계까지 함께 제공한다.

S4의 마지막 외부 입력을 완결하기 위해 설치 인수 독립 리뷰와 S2/S3 중심의 전체 범위
독립 리뷰를 먼저 병렬 수행한다. 구체적 지적을 처분한 후보를 호출8에 전달하고, 모든
작업자가 종료한 뒤 최종 UTC/KST 전체 시험을 직렬 수행한다. 리뷰 게이트를 줄이거나
전체 검증을 승인 전 정본 통합으로 대체하는 변경은 아니다.

감사 outbox와 intents는 현재 append-only이고 `effect_source`는 완료 표식이 아니라
intraday 종류 구분이다. 장기 누적 규모별 처리 지연·안전한 보존/압축 정책은 운영 차단
항목으로 남기며 이번에 임의 삭제하거나 새 schema를 만들지 않는다.

1. 보류된 복구 결정이 다음 sweep에서 해제되거나 다른 종목 복구 실패에 잃어버리는 창 → Task2 실제 owner 인수.
2. caller 취소/종료가 quote commit~prepare 사이에서 발생해 이중 매도·고아 task를 만드는 창 → Task2 lock/shield/scope 인수.
3. full SELL/EOD는 pending_stage가 없을 수 있어 틱마다 새 주문을 내는 위험 → Task2 실제 attempt/예약 대조와60초 재준비.
4. regular→closing 사이 가격 조회 await로 세션이 바뀌거나 지정가 호가가 결측인 경우 → Task3 wire body/미송신 인수.
5. factory 검증 실패가 live를 먼저 바꾸거나 부트 sweep 종료가 기존 종료 루트를 놓치는 경우 → Task4 assert_untouched/기동·배수 인수.

## Task 2: 보호 생산자 부품

**Files:** Create `src/execution/safety/protection_producer.py`, `tests/test_execution_p1_producer.py`(크면 전용 recovery/flow 시험 파일로 분리 가능, 기존 fixture 파일 무수정).

**Interfaces:** `ProtectionProducer(runtime, *, clock, indicator_source)`, `async on_market_data(event)`, `async sweep()`, `health()->dict`. runtime.engine을 사용하며 새 네트워크·주기 loop·durable schema 없음. `indicator_source(symbol)`은 기존 캐시의 ma5/prev_low만 읽는다.

- [x] RED: 실제 runtime/store fixture로 소비자 없이 남은 손절 결정, WS/REST provenance, EOD/면제, 보류 복구/취소 창을 먼저 실패시킨다. 신규 모듈 부재만 collection error로 보고 끝내지 말고 행동 실패를 확인한다.
- [x] GREEN: shared lock이 shield 내부 작업에서 sweep→quote→동기 `_submit_signal`까지 유지된다. 기존 command_scope는 첫 await 전에 열며 task 예외를 health와 callback에서 회수하되 프로그래밍 오류를 성공으로 삼키지 않는다.
- [x] sweep: 보류 결정 종목 제외 후 H7; H8는 호출 전 원 admission snapshot과 한 행씩 유일 대조, 결정 보관 후 다음 행. 앞 결과를 뒤 오류로 잃지 않음. admission 닫힌 창에는 복구만 하고 새 제출은 보류, 보류 intent는 다음 틱에도 불변.
- [x] session: 주입 aware clock을 KST로 정규화한 뒤 requests._session_at 단일 출처. regular MARKET, closing LIMIT 메타데이터, 나머지(pre_market/pre_close/break/next_market/closed) quote·발행0 + health. 휴장 지원을 이 함수가 증명한다고 하지 않는다.
- [x] tick: 보유/면제 확인 → tick 진입 때 새 pp-i- ID → EOD 우선 → degraded quote 제외 → 순수 quote_protection 사전 계산 → 결정이면 즉시 durable 호출, 없으면 종목별20초 throttle. 결정 없는 ID는 버린다.
- [x] WS는 observe_market 원 observation + ma5/prev_low; REST는 quote의 market_as_of/source/source_event_id 셋 다 None + high/low. receipt 시각을 시장 시각으로 합성하지 않음. 종목 출처 혼합 처리와 재접속을 명시하며 기존 source gate를 열지 않음.
- [x] EOD: 15:10부터 gap_and_go 손익<0, theme_chasing 손익<1%면 원 position.avg_price와 event.close로 전량, reason은 기존 한국어 갭EOD/테마EOD 규약. EOD면 quote호출0. 모든 SELL에 protection_intent_id/quantity/exit_action/source/order_type.
- [x] 제출 직전 게시본 면제 재검사, 결정 수량은 정확한 양의int이고 보유량 이하; 초과/불일치는 발행0·health. 미해결 BUY는 owner attempts 순수 순회로 발견하고 blocked_by_open_entry. 미해결 SELL도 중복발행 금지. `_submit_signal` 반환None만으로 성공/실패를 지어내지 말고 실제 intent/attempt를 읽는다.
- [x] 기대 ApplicationBlocked는 health 보류(핸들러로 새지 않음), 프로그래밍 오류는 전파. NOT_SENT/REJECTED 확정 미체결만60초 후 새episode, UNKNOWN/nonterminal·관측/적용 체결이 있으면 재발행하지 않는다. 취소/만료를 추측하지 않는다.
- [x] health: last_tick_at, last_quote_at, throttled, blocked_by_open_entry, pending_released, resume_dispositions, reemissions, 보류이유/결정, 종료상태. 하트비트 kr_protection_producer는 attempt/success/failure/idle을 구분(0보유 idle). 새 BUY gate 없음.
- [x] 시험: R2~R4/R14~R17, pending 실제10익절→잔량90 보호, full/EOD반복, degraded증거불변, 보류원intent,2행재개뒤실패, 정상 quote~prepare 경합, caller취소/종료drain, ApplicationBlocked vs TypeError, 수량불일치, 다른종목 inbox미적용시 순수결정 계산.
- [x] 측정: 8종목 합성 ticks의 owner-write 지연과 순간고점 누락을 미스로틀 대조로 기록. 합성 성능을 운영 성능/허용 슬리피지 근거로 과장하지 않음.
- [x] See: 작성자 변이 최소3종 + 독립 추가3종, 관련 corpus GREEN. 외부 Opus 도구없는 소스 리뷰(상한 내). 제품등록0 유지. coordinator 전체 UTC/KST 후 feature통합/문서/푸시.

예시 불변식(실제 fixture/필드에 맞춰 시험하며 아래 기대값은 독립 계산):

```python
assert retained.intent_id == original_intent
assert runtime.owner.state['protection']['pending_owners'][symbol] == original_intent
assert sent.metadata['quantity'] == 90
assert sent.metadata['order_type'] == 'market'
```

## Task 3: engine attach 보호 경로

**Files:** 제품은 `src/core/engine.py`만, Create `tests/test_execution_p1_engine.py`. 기존 `tests/test_execution_signal_gateway_acceptance.py`의 E1 가격 writer 예외 단언만 위 처분대로 갱신 허용.
**Interfaces:** S2 SignalEvent metadata→RiskManager.on_signal→gateway. runtime.clock의 KST시각·requests._session_at를 사용한다.

- [x] RED: 실제 MARKET_DATA 처리의 errors_count/전략 도달, regular 보호 SELL wire MARKET, closing LIMIT,15:35/15:45미송신, 정확한 지정수량/초과수량거부.
- [x] GREEN: update_position_price의 attach raise만 무동작 return. 보호 intent가 명시된 SELL attach 분기에서 명시 수량을 정확히 사용하며 불일치 전량 보정 금지. regular + market metadata면 호가조회0/MARKET priceNone. closing은 LIMIT만; 호가 결측으로 MARKET fallback하지 않음. 보호 attach는 legacy 거래시간 가드(15:25 CLOSED)를 재사용하지 않고 runtime 시계→KST→requests._session_at를 사용. legacy와 일반 nonprotective 경로는 기존 계약 대조.
- [x] See: legacy 본문을 신규 attach guard만 제거해 base와 바이트 비교, 기존S4-0 26 무수정·독립gateway인수37(E1 계약 단언만 갱신) GREEN, 작성자 변이3종 및 독립 추가3종과 실제모델 Opus승인, 전체 UTC/KST 후 feature통합/문서/푸시.

```python
assert body['ORD_DVSN'] == '01'
assert body['ORD_UNPR'] == '0'
assert body['ORD_QTY'] == '90'
assert engine.stats.errors_count == 0
```

## Task 4: factory 설치기 배선·관측

**Files:** Modify `src/execution/safety/factory.py`, runtime.py는 위 표의 관측 전용 예외만. Create `tests/test_execution_p1_install.py`. 기존 `test_execution_install_factory.py`의 target kwargs에 신규필수인자를 넣고 live_snapshot에 producer참조·MARKET_DATA 핸들러 identity/순서를 추가해 거부 시 무변경 계약을 강화한다. 다른 기존설치시험은 무수정.
**Interfaces:** `install_attached_runtime(..., indicator_source)` 필수 callable. 구간1 검증, 구간2 마지막(attach/start_reconciler뒤)에 producer 생성·runtime참조·MARKET_DATA첫핸들러·기동sweep1회. shutdown은 S2 command_scope 경로.

- [x] RED: invalid indicator_source에서 live무변경, 실제 보호 핸들러 선행, 기동 복구·결정 보류, 종료drain·중복설치거부, runtime.health의 보호 관측.
- [x] GREEN: 위 순서를 지키고 구간2 오류는 legacy로 복귀하지 않음. bot 캐시 closure 호출자계약·실제품 설치caller0 유지.
- [x] See: 실제engine→producer→owner→fakeHTTP→실큐fill 인수, installer거부 corpus, 독립 변이+Opus 실제모델 승인, 전체 UTC/KST 후 feature통합/문서/푸시. 차단21/24/25는 검증한 범위에서만 설치기 기준 닫힘, 실제 설치아님.

## Task 5: 주석·문서·범위 통합리뷰

**Files:** kr_scheduler.py·batch_analyzer.py의 차단21 주석만, CHANGELOG/CLAUDE top/README/위험/아키텍처/인계/원장/최종보고. 원본문 불변을 AST/주석제외 대조로 증명한다.

- [x] 미설치/운영 금지 경계와 남은 차단23/26/27·미체결BUY/late-fill·초기 인계·정책게시를 문서화.
- [x] 독립 Astra/xhigh 최종 범위 리뷰는 이번 시작6394d22 이후 제품과 S1경계만, 기존 전체234+커밋 재승인 아님. 경미한 보류 지적도 재검토.
- [ ] 명령/exit코드·UTC/KST/변이·교차리뷰 actual model/사용량·SHA·설계처분을 보고하고 feature commit/push. 이번 전용clean worktree만 통합 이력 보존 후 정리.
