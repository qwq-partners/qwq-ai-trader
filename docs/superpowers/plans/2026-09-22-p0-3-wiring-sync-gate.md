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

## 6. Do·See

### 6-1. Do 1차 — 구현 2 ∥ 독립 재현 2 (워크플로 `wd3pq2o1g`, opus/high 구현 · opus/xhigh 재현, 격리 worktree, base `198898a`)

| 브랜치 | 커밋 | 결과 |
|---|---|---|
| `work/p0-3-sa` (S-A 부품 묶음) | `904c982` | 신규 `tests/test_execution_p03_wiring.py` 12건 GREEN, 변이 6종 kill, 보호 목록 4종 무수정 GREEN. **그러나 `tests/test_execution_reconciler.py` 18건 RED**(Q-8 `engine.running` 게이트 — 그 corpus 는 engine.running 을 켜지 않는다). 설계와 다르게: `reconciler_live` 가 미배선(`_reconciler_started_at is None`)과 **대상 0** 이면 통과(Q-2 의 생존 전제에 구멍), 게이트를 attempts 순회 앞에. |
| `work/p0-3-live` (S-B·S-C·S-D) | `b5c2e20`→`5f27971`→`25dc188` | 변이 5종 kill, 21파일 565 passed, 보호 목록 4종 무수정. 설계와 다르게: 대조를 자기 try/except 로(catch-all 안), 평단가 비교 안 함, **S-D 는 두 함수의 attach 전면 조기 return**(가격 대입뿐 아니라 보호 emit 도 소멸). 시계 플레이크 발견(로컬 09:00~09:29 창, 작업 칩 `task_de5b3348`). |

**독립 재현(둘 다 CHANGES_REQUIRED):**

| 대상 | 등급 | 지적 → coordinator 처분 |
|---|---|---|
| S-A | P1 | reconciler 18건 RED → **fixture 에 `engine.running = True` 보정, 단언 불변**(기대값 변경 아님 — 그 corpus 는 주기 계약을 단언하지 engine 플래그를 단언하지 않는다; 제품에서 `engine.running=False` 는 stop/_shutdown 뒤뿐이고 곧 배수로 이어진다) |
| S-A | P1 | `reconciler_live` 신선도 절이 대상 0 이면 통과(조용한 계좌에서 죽은 주기를 못 본다) → **주기 완주 시각 필드 1개 `_reconciler_cycle_completed_at`**(일자 게이트로 끝난 주기 포함, 대상 0 에서도 찍힘)로 기준 변경. Q-2 의 "새 상태 없음" 제약은 이 한 필드에 한해 푼다(대안이 collect 결과의 유무를 상태로 쓰는 것이라 더 나쁘다). |
| S-A | P1 | BUY `name` 공백 미정규화 → prepare 검사기와 같은 strip·비어 있으면 키 없음 |
| S-A | P2 | `exit_types` 첫 줄 `reason if reason is not None else ""` 가 스케줄러 `reason or ""` 와 갈림(0/False 표본) → `type(reason) is str` |
| S-A | P2 | 구간 1 에 `execution_producer_already_wired`(`_closing`·살아 있는 `_reconciler_task`) — live 를 건드리기 전 |
| S-A | P2 | 수동 BUY 도 `reconciler_unavailable` 로 막을지 → **막는다**(생산자가 죽어 있으면 수동 매수의 체결도 관측되지 않아 owner 상태가 틀어진다 — fail-closed) |
| S-A | P2 | 실패 창 측정·collect 인자 시험이 항진 → 하중 부여 |
| live | P0 | reconciler 18건 회귀 — S-A 소관(위) |
| live | P1 | S-D 전면 skip 이 attach 의 손절·트레일링·분할익절·갭EOD·보유기간 청산 **구동기를 전부 없앤다** → **skip 유지 + 설치 차단 사유 21 등록**("attach 에는 보호 청산 구동기가 없다 — P1 보호 SELL 의 main 동등에서 owner quote 경로로 닫는다"). "emit 은 owner 게시본 읽기로 유지" 대안은 owner 의 quote 생산자가 제품에 있는지가 전제라 조사만 시킨다(§6-2). |
| live | P1 | 조용한 계좌의 `set_sync_status(True)` 무하중(변이 n2 생존) → `_sync_healthy=False`·`_sync_fail_count=1` 에서 회복 단언 |
| live | P1 | 분기 위치(빈응답/부분누락 방어 **뒤**) 미고정(n5b 생존) → 부분누락 표본(1차 누락·2차 정상 → 호출 2회·불일치 0) |
| live | P2 | 시험 이름·m4 근거·"미설치에서 속성 없음" 사실 오류(`engine.py` `__init__` 이 None 세팅)·falsy 관용구 `kis_positions if kis_positions else {}`·attach 재시도 비용(수동 매도 뒤 매 주기 partial_missing 재시도+sleep 5) → 정정·주석 |

### 6-2. Do 2차 — 처분 구현(워크플로 `wz2yvnqv6`, opus/high ×2, 격리 worktree, 관측 모델 `claude-opus-5[1m]`)

| 브랜치 | 커밋 | 결과 |
|---|---|---|
| S-A 처분 8건 | `b794893`(`work/p0-3-sa-f3f` — 원 브랜치가 다른 worktree 에 체크아웃돼 같은 SHA 에서 분기) | 9파일 UTC/KST 각 299 passed·경고 0, 변이 4종 kill(m1 완주 시각을 대상 있을 때만 / m2 name strip 제거 / m3 구간 1 already_wired 제거 / m4 수동 BUY 예외). **설계와 다르게 한 것(전부 수용):** ① 처분 2 의 전제("대상 0 이어도 완주 시각이 갱신된다")가 제품에서 거짓이었다 — `_reconcile_loop` 는 대상이 0 이면 `Event.wait()` 로 **무기한** 잤다. 예외 절만 지우면 조용한 아침에 설치 15초 뒤부터 그날의 모든 BUY 가 `reconciler_unavailable` 로 영구히 막힌다. → 루프의 잠을 항상 `wait_for(…, interval)` 로 바꿔 대상 0 이어도 3초마다 한 바퀴(조회는 대상 0 이면 안 나가므로 원장 TR 예산 0, 비용은 상태 사전 순회). ② 완주 시각은 `reconcile_once` 의 `else:` 한 곳(일자 게이트로 끝난 주기 포함·`cycle_timeout`·예외 제외). ③ 조회 신선도 절(`_reconciler_complete_at`)은 지우지 않고 절 4 로 — `_stalled_targets` 일 때만 본다("진전은 신선하고 조회만 죽은" 표본이 그것으로만 죽는다). ④ `health()['reconciler']['last_cycle_completed_at']` 추가. ⑤ 수동 출처는 `EntryOrigin.USER`(코드에 MANUAL 없음) — 게이트는 이미 출처를 보지 않으므로 제품 분기 0, 시험+주석으로 고정. ⑥ 처분 8 은 `seed_checkpoint(mutate=…)` 로 ACK 된 미해결 SUBMIT 을 심어 실제 조회 1건의 인자 `{'start_date','end_date','exchange_scope':'ALL','max_pages':3}` 를 통째로 단언. |
| live 처분 7건 | `66a7d70`(detached — 원 브랜치가 다른 worktree 에 체크아웃돼 `switch --detach 25dc188` 로 작업) | 14파일 UTC/KST 각 202 passed, 변이 3종 kill(m1 조용한 계좌의 `set_sync_status(True)`+하트비트 제거 / m2 분기를 재시도 방어 앞으로 / m3 monitor 가드 제거 — 단언으로 죽음). 제품 실질 변경은 falsy 관용구 1줄, 나머지는 주석. **owner 보호 경로 생산자 조사 결과: 없음** — `runtime.quote`/`observe_market` 제품 호출자 0건, engine attach 분기의 owner 인계는 `apply_observation`(체결) 뿐, `engine.update_price` 는 attach 에서 `ApplicationBlocked` 를 올리고 그 자신도 호출자 0, kr_scheduler 의 runtime 검사 8곳은 전부 레짐/장중위험/배치시계용. → **설치 차단 사유 21 확정**(두 함수의 attach 분기 주석·시험 docstring 3곳·인계 표). 부수 발견: 같은 tmp_path 의 두 ExitManager 가 같은 일자 stage 파일을 공유해 앞 절의 first pending 이 뒤에 복원된다(이중 매도 방지 정상 동작) — 표본 종목을 나눴다. 남긴 P1 후보: attach 재시도 비용(수동 매도 뒤 매 주기 `partial_missing` → 30초마다 원장 TR 1회+sleep 5). |

통합: `77e8c5e`(S-A, no-ff) → `86022da`(live, no-ff). 워크플로 worktree 6개 제거·작업 브랜치 3개 삭제. 전체 suite KST **5084 passed**/xfail 2·경고 4·격리 0(`86022da`). UTC 1차 실행은 10:09~10:15 UTC 에 `test_execution_signal_gateway_eviction` 2건 RED — cross_validator 의 **09:30~10:30 -8 페널티**가 프로세스 지역시각(UTC)에 걸린 것(로그 `장초반 변동성 -8 (09:30~10:30)`), 같은 파일 KST 대조 GREEN. **시계 창은 09:00~09:29(하드 차단)·09:30~10:30(-8)·12:30~13:00(+5) 의 프로세스 지역시각이다** — 전체 suite 는 UTC 실행이면 18:00~19:30 KST·21:30~22:00 KST 를, KST 실행이면 09:00~10:30·12:30~13:00 KST 를 피한다(작업 칩 갱신).

### 6-3. See — Codex 15차(gpt-6-astra/xhigh, 독립·읽기 전용) → 처분 → 16차 APPROVE

| # | 지적 | 처분 |
|---|---|---|
| P1 | `reconciler_live` 절 4 가 `fallback=_reconciler_started_at` — 미해결 주문이 있고 첫 collect 가 바로 죽어도 기동 15초 안에는 다른 종목 BUY 가 통과 | **수정(`80571eb`)** `fallback=None`: 오늘 성공한 조회가 없으면 대상이 있는 한 거짓. `reconciler_blocked_reason()` 의 기동 시각 폴백은 "굶었다"의 유예이고 이 절은 "조회가 있었다"의 사실. 시험 `test_a_fresh_start_with_a_pending_order_but_no_query_yet_is_not_live` 수정 전 RED |
| P1 | 미배선 생산자(`_reconciler_started_at is None`)를 통과시켜 Q-2 의 "미기동 거부" 계약 불충족 | **기각·유지(결정)** — 제품에서 attach 에 이르는 유일한 경로는 `install_attached_runtime`(`attach()` 호출 1곳, `collect` 필수, attach 직후 `await` 없이 `start_reconciler`) 이고 시험이 주기 task 1건을 고정한다. 술어에 걸면 수집기를 세우지 않는 기존 owner 인수 176건이 `reconciler_unavailable` 로 회귀하고 그 시험들은 내부 시각 필드를 위조해야 산다. 손 attach 는 설치기의 16종 거부 전부를 우회하는 경로라 술어 하나의 대상이 아니다. 16차가 "설치기를 거치지 않는 제품 경로 없음"을 확인(`factory.py:407` 한 곳). 술어 docstring 의 "Q-2 와 다른 한 곳" 문단이 정본 |
| P2 | 설치 성공 뒤 재호출이 `execution_runtime_already_restored` 대신 `execution_producer_already_wired` — "구간 1 거부니 legacy 로"의 거짓 조치 유발 | **수정(`80571eb`)** 생산자 거부를 `already_restored` 뒤로. 시험 `test_a_second_install_names_the_restore_before_the_running_producer` 수정 전 RED |
| 비지적 | `classify_exit_type` 은 옛 스케줄러와 입력 `1` 에서 다르다(`TypeError` → `manual`) — §6-1 의 형 검사 처분이라 지적으로 세지 않음 | 기록 |

16차(같은 모델·effort): (a) 두 수정이 지적을 닫고 새 회귀 없음 (b) 기각 근거를 반박할 제품 경로 없음 → **APPROVE**. 최종 전체 suite `80571eb` UTC/KST 각 **5086 passed**/xfail 2·경고 4·격리 0.

## 7. 마감 — 무엇이 닫혔고 무엇이 아닌가

**닫힌 것(engine 브랜치, 부품·제품 호출자 0건·운영 미설치):**
- 설치 차단 사유 **17**(`_sync_portfolio` attach 분기 없음) → **닫힘**: attach 에서 live 를 쓰지 않고 읽기 전용 대조(종목 대칭차·수량·현금 1,000원 임계) + 불일치면 `set_sync_status(False)`·하트비트 실패·error, 0 이면 legacy 꼬리와 같이 `set_sync_status(True)`·하트비트 성공. 분기는 빈응답·부분누락 재시도 방어 **뒤**. 미설치 경로 바이트 동일.
- 설치 차단 사유 **18**(증거 생산자 배선 0) → **설치기 기준 닫힘**: `install_attached_runtime(…, collect)` 가 attach 직후 `start_reconciler(collect)`. 구간 1 거부 3종 추가(`invalid_execution_collector`·`execution_query_environment_required`·`execution_producer_already_wired`). 설치기 자체의 제품 호출자는 여전히 0건.
- F8 ②: 자동·수동 BUY 는 생산자 생존 전제(task 생존·굶지 않음·오늘 주기 완주 ≤15초·대상 있으면 오늘 조회 완료 ≤15초) 없이는 `reconciler_unavailable`, SELL·CANCEL 은 통과.
- 체결 metadata: SELL `exit_type`(`src/utils/exit_types.py::classify_exit_type` — 스케줄러 3곳 위임, 순환 import 회피), BUY `name`(strip·공백뿐이면 키 없음)·`entry_signal_score`.
- 설치~run 창: `_reapply` 가 `engine.running` 아니면 `engine_not_running` skip(ingress 행 0), run 뒤 다음 주기가 처리. 창 측정 `apply_seconds_last/max`. 종료 계약은 설치기 docstring(설치 → run 생성을 하나의 try/finally 로).
- `kis_kr.get_execution_daily(exchange_scope=, max_pages=)` 가법 kwarg(기본값 동일).

**닫히지 않은 것 / 새로 등록:**
- **차단 사유 20**(`entry_policy_context` 재게시자 0 — 정책 사실이 설치 시점에 얼어붙음) → 10C.
- **차단 사유 21**(attach 에 보호 청산 구동기 없음 — `monitor_positions`·`_check_exit_signal` 전면 skip, owner quote 생산자 0) → **P1**. 차단 사유 1b 보다 앞선 문제.
- 차단 사유 19(체결마다 전 종목 정지 창)의 범위 축소 → P2.
- F8 ①·late-fill·자식 가드 side 인지 → P1. `_cleanup_stale_pending` → P0-4. attach 재시도 비용(P1 후보). 실계좌 스모크 §5 → 사용자 확인 대기.

**P0-4 입력:** ① `_protection_failed` 해제 경로 ② 부분 체결의 예약 감소 ③ 세션 경계(장 마감) 미해결 attempt 소멸 ④ `_cleanup_stale_pending` 의 owner 경로(차단 사유 7) ⑤ 차단 사유 21 을 P1 에서 닫을 owner quote 생산자의 입력 계약(시세 출처·시각·stale 규칙).
