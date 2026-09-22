# P0-3 — 생산자 배선·sync 분기·게이트·체결 metadata (Plan, 2026-09-22)

> **상태: Plan 확정(심사 REVISE ×2 → 처분) → Do 착수.** 사용자 지시 "ㄱㄱ". attach 전환의 세 번째 전제 단계이자 **첫 live 파일 단계**(`kis_kr.py`·`kr_scheduler.py`·`batch_analyzer.py`). 상위: `2026-09-22-kis-judgement-decisions.md` §5, 입력: `2026-09-22-p0-2-fill-evidence-producer.md` §7.

## 0. 과정

조사 2(sync writer·체결 경로 / 배선·게이트, 요청 opus/high) → 설계 G1~G11(요청 opus/high) → 적대적 심사 2관점(요청 opus/xhigh, 설계자와 다른 실행): **① REVISE(must-fix 6·legacy 위험 5) · ② REVISE(must-fix 5·legacy 위험 6)** → coordinator 처분(§3). 읽기 전용·pytest 0·제품 0줄.

## 1. 조사가 확정한 사실(줄은 `f83e2f1`)

- **`_sync_portfolio` 의 writer 전수**(`kr_scheduler.py:1242-1474`): live 포트폴리오 6곳(`del positions`·신규 대입·수량·평단·현재가·현금), live 보호 2곳(`exit_manager.remove/register_position`), owner 소유 risk 필드 1곳(`on_buy_filled`). `_execution_runtime` 검사 8곳은 전부 레짐·시계용 — 이 함수 안에는 0건(차단 사유 17 확인).
- **충돌은 상시다.** `_owner_ready` 는 `encode_portfolio(decode(state)) == encode_portfolio(engine.portfolio)` DTO 전체 동등성으로 감지하고 `current_price` 가 포함돼 있다 — `_view_price` 는 평가/체결/수락 시세만 쓰고 sync 는 KIS `prpr` 을 그대로 넣으므로 **수동 매매도 유령도 없는 조용한 계좌에서 30초마다 게이트가 닫힌다.** `_publish` 는 `bot._portfolio_lock` 을 잡지 않고, sync 는 lock 을 쥔 채 `:1363` 에서 await 한다.
- **owner 가 외부 변화(수동 매매·배당·입금)를 배우는 경로는 없다.** 수량·현금 reducer 는 `reduce_economics(FillObservation)` 하나뿐. 설치기 대조는 기동 1회 거부용. → D1 처분 ①("거절 = 보호 실패")이 코드로 확인된다.
- **attach 의 live writer 는 sync 하나가 아니다**(심사 발견): `batch_analyzer.monitor_positions`(`:1741`, 30분, `pos.current_price`·`highest_price` 직접 대입 + `exit_manager.update_price`)와 `kr_scheduler._check_exit_signal`(`:1045`, REST 피드) 둘 다 무가드.
- **exit_type**: legacy 는 `run_fill_check` 에서 `_classify_exit_type(reason)` → `record_exit(exit_type, is_full_exit)`. attach 는 `gateway._fill_metadata` 가 SELL 에 `None` 을 돌려줘 economics 의 `exit_type` 이 항상 `""` → `daily_exit_count`/`stop_loss_today` 가 영원히 0(재진입 억제 fail-open). `Order.reason` 에 engine 이 `event.reason` 을 이미 싣는다(`engine.py:2365·2376`).
- **손실 청산 카운트 차이**: legacy 는 부분 체결마다 +1, owner 는 intent 당 1회 — owner 쪽이 옳다(결정으로 기록).
- **종료 순서는 제품에 "배선돼 있다"고 할 수 없다** — `engine._shutdown()` 의 유일한 도달 경로는 `run()` 의 finally 이고, run task 가 만들어지지 않으면 주기의 apply 가 `shield(future)` 에서 영구 대기한다.
- **설치~run 첫 반복 창**: `_reapply_inbox` 가 매 주기 같은 행에 `apply_execution_observation` 을 다시 불러 **새 ingress 행**을 만든다(QUEUE_WAIT 누적 → `_owner_ready` 가 전부 SETTLED 를 요구).
- **`reconciler_blocked_reason()` 은 대상 0 이면 None** — 수집기가 죽어 있어도 아침 첫 BUY 가 통과한다.
- **F8 ① 의 함정**(심사 발견): BUY 100/40 적용 → 보호 SELL 40(전량)이 `if full:` 에서 lot 을 `closed=True` → 남은 60 이 늦게 체결되면 닫힌 lot 위의 체결이 거부돼 `unresolved_execution_evidence` **영구 전역 정지**. 자식(취소) 가드 `unresolved_child_attempt` 도 side 를 보지 않는다.
- `entry_policy_context` 의 제품 재게시자는 factory 의 설치 1회뿐 — attach 에서 sync/추세/거시 정책 사실은 **설치 시점에 얼어붙는다**(legacy 의 "연속 3회 sync 실패 → 매수 차단"이 attach 에 없다).
- `get_execution_daily`·`_execution_queries` 는 origin/main 에 없다(engine 브랜치 전용) — "live 파일" 이지만 미설치 운영에는 도달 불가. 진짜 위험은 병합(main 의 `_api_get(tr_cont: str="")` 대 engine 의 `tr_cont: Optional[str]=None`).

## 2. 설계 G1~G11(요약)

G1 sync 는 읽기 전용 관측 + 불일치 알람(후보2), 분기점은 `_portfolio_lock` 직전 · G2 설치기에 `collect` 필수 kwarg, 거부 2종은 구간 1, `start_reconciler` 는 `install_gateway` 다음 줄 · G3 `kis_kr.py` 에 가법 kwarg 2개(`max_pages`·`exchange_scope`) · G4 기동 순서는 시험으로만 · G5 F8 두 줄 · G6 SELL 의 `exit_type` 을 `_classify_exit_type(order.reason)` 으로 · G7 BUY 의 `name` 은 `event.metadata` 에서만 · G8 부분 청산 카운트는 owner 현행(0줄) · G9 창 측정 2키 · G10 스모크는 사용자 확인 대기 · G11 KRX 하드코딩은 열린 항목.

## 3. 심사 → coordinator 처분 = 확정

| # | 처분 |
|---|---|
| Q-1 | **F8 ①(미체결 BUY 뒤 같은 종목 보호 SELL 허용)은 P0-3 에서 열지 않는다.** late-fill(전량 청산 뒤 부모 BUY 잔여 체결)을 economics 가 받는 설계와 자식(취소) 가드의 side 인지가 함께 필요하다 → **P1(보호 SELL 의 main 동등)** 으로. 결정 문서 §5 P1 에 "부분 체결·late-fill·자식 가드" 를 명시. 인수 표본(BUY 100/40 → SELL 40 → 잔여 60 도착 → 정지 없음)은 P1 의 RED 로 등록. |
| Q-2 | **F8 ②(BUY 한정 `reconciler_unavailable`)는 유지하되 생존 전제를 더한다:** `reconciler_blocked_reason()` 이 None 이어도, 자동 BUY 는 `runtime` 이 주기를 **시작했고**(`_reconciler_started_at`) 오늘 영업일에 **마지막 완료 주기가 k×interval 안**(`_reconciler_complete_at`)일 때만 통과 — 없으면 `reconciler_unavailable`. 새 상태 없음. |
| Q-3 | **attach 의 live writer 3곳을 전부 가드한다:** `_sync_portfolio`(읽기 전용 관측 + 불일치 알람 + `set_sync_status`), `batch_analyzer.monitor_positions`(가격·highest·`update_price` 대입을 attach 에서 건너뛰고 읽기 전용 — 시세는 owner 의 quote 경로가 정본), `kr_scheduler._check_exit_signal`(attach 에서 `exit_manager` 직접 갱신 금지). 각각 `_execution_runtime is not None` 조기 분기. **live 파일 3개 — 단계를 나누고 각 단계 뒤 전체 suite.** |
| Q-4 | G1 의 알람: 불일치 0 이면 legacy 꼬리와 같이 `set_sync_status(True)` + 하트비트 성공, 불일치면 **`set_sync_status(False)` + 하트비트 실패 + `logger.error`**(연속 임계 뒤 sidecar 매수 차단이 legacy 와 같이 걸린다 — 10분 강제 해제 때문에 best-effort 임을 기록). 계산은 전부 삼키는 `try:` **밖**에서 별도 사유로. 불일치 판정은 종목 대칭차·수량·현금(1,000원 임계). |
| Q-5 | **`entry_policy_context` 재게시자 0건 → 설치 차단 사유 20 등록**(attach 에서 sync/추세/거시 정책 사실이 설치 시점에 얼어붙는다 — 10C 의 지속 publisher). |
| Q-6 | G6/G7 의 event 는 duck-typing: `getattr(event, 'reason', '')`·`getattr(event, 'metadata', None) or {}`(이 `or` 는 dict 결측 폴백이라 규칙의 falsy 판정이 아님을 주석). `name` 추출은 점수 검증 **뒤**. `tests/test_execution_fill_producer_parts.py` 의 스텁 event 표본 4단언은 **기대값 변경(결정)** 으로 기록. `_classify_exit_type` 은 순수 함수라 **`src/execution/safety/` 가 스케줄러를 import 하지 않도록** 함수를 `src/utils/exit_types.py`(신규 모듈 — 이 한 곳은 ponytail 예외, 순환 import 회피)로 옮기고 스케줄러 3곳은 위임. 빈 문자열은 `'manual'` 폴백을 명시. |
| Q-7 | **종료 계약**: 제품 호출자는 "설치 → `engine.run()` task 생성" 을 **하나의 try/finally** 로 묶어 그 사이 실패에서 `engine._shutdown()` 이 돈다 — 지금은 호출자가 없으므로 계약을 설치기 docstring 과 S7 인수(설치 성공 + run 미생성 표본)로 고정. |
| Q-8 | **설치~run 창**: `_reapply` 는 `engine.running` 이 아니면 apply 를 부르지 않고 `engine_not_running` 으로 skip(새 ingress 행 0). run 시작 뒤 다음 주기(3초)가 처리한다. G4 의 "run 첫 반복에서 기동" 대안은 기각(설치기의 "attach 는 마지막 줄" 계약 유지). |
| Q-9 | 설치기 시험 fixture 의 `collect` 는 결정적(즉시 complete=True·빈 pages)이고 성공 표본은 전부 `runtime.shutdown()` 까지 배수 — 경고 수(기존 4) 불변을 인수에. |
| Q-10 | `_cleanup_stale_pending` 이 attach 에서 `cancel_all_for_symbol` 을 직접 부르는 것(차단 사유 7 의 owner 우회)은 **P0-4** 로(이미 목록에 있음). `unresolved_symbols()` 가 side 를 거르지 않아 교체 로직이 그 종목을 계속 제외하는 것은 F8 ① 과 함께 P1. |
| Q-11 | 나머지 인용 오류(줄 번호 ±1~4)는 구현 시 재확인. |

## 4. 단계(확정) — 부품 먼저, live 파일은 하나씩

| 단계 | 파일 | 인수(요지) |
|---|---|---|
| S-A 부품 묶음 | `factory.py`(구간 1 거부 2종 `invalid_execution_collector`·`execution_query_environment_required`, `install_gateway` 다음 줄 `start_reconciler(collect)`, docstring 에 종료 계약) · `commands.py`(F8 ② + 생존 전제) · `gateway.py`(SELL `exit_type`·BUY `name`, duck-typing) · `runtime.py`(`_reapply` 의 `engine.running` 게이트, 창 측정 `apply_seconds_last/max`) · 신규 `src/utils/exit_types.py`(`classify_exit_type` 순수 함수) · 시험 | 설치 성공 뒤 `_reconciler_tasks` 1·shutdown 배수·경고 4 불변 / env≠prod·collect 아님 거부는 live 무변경 / BUY 는 수집기 미기동·stale 이면 `reconciler_unavailable`, SELL 은 통과 / SELL binding 에 `exit_type`, BUY 에 `name`(결측이면 키 없음) / run 전 apply 호출 0·run 뒤 처리 / 창 2키 |
| S-B live 1 | `kis_kr.py`(`_execution_queries(max_pages=)`·`get_execution_daily(exchange_scope=, max_pages=)` 가법 kwarg) · `kr_scheduler.py`(`_classify_exit_type` 3곳 위임) | 기본값 동일(기존 시험 불변) · **전체 suite** |
| S-C live 2 | `kr_scheduler.py::_sync_portfolio` attach 분기(Q-4) | 조용한 계좌 sync 10회 뒤 `_owner_ready` 통과·`_sync_healthy` True / 수동 매도 뒤 불일치 → `set_sync_status(False)`·하트비트 실패·error 로그, live 무변경 / 미설치 legacy 는 바이트 동일 · **전체 suite** |
| S-D live 3 | `batch_analyzer.monitor_positions`·`kr_scheduler._check_exit_signal` attach 가드 | attach 에서 live `current_price`/`highest_price`/`exit_manager` 무변경, 미설치 동일 · **전체 suite** |
| S-E 인수 | S7: 기동·종료 순서 시험(설치 → run / 설치 → run 미생성 → shutdown), 제품 호출자 0건 유지 | |

**하지 않는 것:** F8 ①·자식 가드 side 인지(P1) · `_owner_ready` 범위 축소(P2) · 실계좌 스모크(사용자 확인 대기 — §5) · run_trader 의 설치 호출자 · `_cleanup_stale_pending`(P0-4).

## 5. 실계좌 스모크 명세(사용자 확인 대기 — 실행하지 않는다)

장 마감 후, main 의 일별체결조회 **수신 지점**에 비식별 기록기만(신규 TR 호출 0, `KIS_TR_SET=legacy` 유지). 확인 4항: ① `TTTC0081R`(또는 현행 `TTTC8001R`) 응답 행의 15필드 실제 철자 ② `EXCG_ID_DVSN_CD="ALL"` 수용과 `excg_id_dvsn_cd` 값 분포(Q27) ③ `cncl_yn` 실제 값·취소확인수량 철자(Q24·Q25) ④ 페이지 수와 `tr_cont` 전이. 증거가 없는 동안 설치기는 `execution_query_environment_required`(env) 로, 주기는 파서의 명명된 skip 으로, BUY 는 `reconciler_unavailable` 로 막힌다 — **설치는 이 4항이 닫히기 전에는 하지 않는다.**

## 6. Do·See (진행하며 채운다)
