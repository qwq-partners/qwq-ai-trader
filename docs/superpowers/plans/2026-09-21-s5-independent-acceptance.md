# S5 — attach 경로의 독립 실큐 인수 (세부 계획)

> 상위: `docs/reviews/b2b3-stage-ledger-2026-09-20.md` 의 "S5 진입 조건" · 기준 SHA `3969eaa` · 2026-09-21
> Plan 산출 과정: 읽기 전용 조사 2관점(인수 시나리오·하네스 스텁, opus/high) → 인수 계획 설계(opus/high) → 적대적 심사 2관점(vacuity·feasibility, opus/xhigh, **둘 다 NEEDS_CHANGES — P0 6·P1 16·P2 11**) → coordinator 가 핵심 주장을 코드로 재확인하고 이 문서로 처분. pytest 0·제품 수정 0.

## 0. 한 줄 요약

- **하는 것:** S3·S4 가 만든 attach 경로를, 구현 worker 의 하네스를 물려받지 않은 **새 하네스 하나**로 처음부터 다시 구동해 인수한다. 산출물은 새 시험 파일 `tests/test_execution_signal_gateway_acceptance.py` 하나다.
- **하지 않는 것:** 제품 코드 수정(0줄), 설치, 기존 시험 수정. 인수 GREEN 은 설치 승인이 아니다 — `trading_ready` 는 제품에서 항상 False 이고 이 파일의 모든 송신 표본은 합성 startup 허가 위에 있다(파일이 스스로 단언한다).
- **결함 후보가 나오면:** 시험을 RED 나 xfail 로 파일에 남기지 않는다. 산문으로 보고하고 coordinator 가 별도 단계로 처분한다(전체 suite 직렬 실행과 통합을 막지 않기 위해).

## 1. Plan 이 확정한 사실 (코드로 재확인 — 원장·인계 문서를 고친다)

1. **"claim 이전 실패 5갈래의 예약 0" 은 틀린 문장이다.** `_dispatch` 의 claim 이전 실패는 ① `claim` 이 False(`claim_not_available`) ② `CommandValidationError`(사유 보존) ③ `ApplicationBlocked`(바깥에서 `command_admission_closed`) ④ 그 밖의 `Exception`(`dispatch_failed`) ⑤ `CancelledError`(결과 없음) 다섯이고, **예약이 0 이 되는 것은 ①②가 `abandon_candidate` True 를 받았을 때뿐이다.** ③④⑤ 는 설계상 예약을 남긴다("보낼 수 없었다"는 판정이 아니다 — `commands.py:570-586`). 다섯 갈래의 공통 계약은 **POST 0** 이다. `_unsent` 안에서 abandon 이 `ApplicationBlocked` 이외의 예외를 던지는 여섯 번째 관측 결과(사유는 정확한데 예약은 유지)는 무시험이다.
2. **attach 에서 가격 없는 SELL SIGNAL 은 거부된다.** `_get_sell_price` 가 None(호가 없음 + `event.price` 없음)이면 legacy 는 MARKET SELL 을 보내지만, attach 는 gateway `_bind` 의 `valuation=None` → `invalid_request_price` 로 끝난다(POST 0·owner 행 0). 즉 **실큐로는 MARKET SELL 이 owner 에 닿지 않는다** — MARKET SELL 의 전선 값은 S3-5 의 gateway 단위 시험에만 있다. fail-closed 이지만 보호 SELL 의 한 갈래가 닫힌 것이므로 설치 차단 사유에 올린다.
3. **UNKNOWN 은 gateway·실큐 층에서 한 건도 시험되지 않았다**(`blocked_unknown` 은 lifecycle·decision_facts 단위 시험뿐). UNKNOWN 뒤에는 그 종목이 아니라 **attach 경로 전체**가 `unresolved_execution_evidence` 로 멈춘다(`commands.py:346-348`).
4. **POST 본문을 단언하는 시험이 없다** — gateway 계열 네 파일은 POST 건수만 센다. "사이징이 전선까지 갔다"는 로그가 아닌 상태로 고정된 적이 없다. 실큐 SELL 의 매수1호가 가지(`get_best_bid`)도 0회 실행이다(어떤 fake broker 도 그 메서드가 없다).
5. **attach 에서 exit_exempt 의 정본은 owner state 다.** `publish_protection` 은 RiskManager 가 참조하는 같은 set 객체를 유지한 채 **내용을 owner DTO 로 교체**한다(`protection.py:292-294`, 의도된 계약). 따라서 런타임에 live set 에만 넣는 `exit_manager.add_exit_exempt(...)`(`kr_scheduler.py:7537·7614`)는 attach 에서 다음 게시에 지워진다 — 설치자는 주입 재현만이 아니라 **추가 경로를 owner 로 옮겨야** 한다.
6. **걷어낼 수 없는 스텁이 둘 있다.** 실제 `_sector_lookup`(PostgreSQL `kr_stock_master`)과 실제 `SignalEventStorage`(asyncpg `localhost:5432`)는 conftest 가 루프백을 허용하므로 **걷어내면 운영 DB 에 닿는다.** 둘은 끝까지 결정적 fake 다. 또 제품이 주입하는 sector lookup 은 전체가 try/except→None 이라 attach 의 섹터 fail-closed(H3)는 제품 주입값으로는 발화하지 않는다(10C 의 "예외 뭉갬"과 같은 항목). 원장의 "걷어낼 스텁 3종" 은 **2/3**(실제 `can_open_position`·팩터 버킷 게이트)로 인수한다.
7. **설정 출처는 제품 1개·하네스 4개다.** owner 가 판정에 쓰는 `EffectiveRiskPolicy` 는 하네스에서 손으로 적은 리터럴이라 RiskConfig 와 임계가 다르다. sidecar 와 owner 의 결론 비교(정책 이중 평가의 정합)는 출처를 하나로 묶는 **10A3 factory 의 인수 조건**이지 S5 의 제품 결함 탐지가 아니다.
8. 엔진 내부 RiskManager 의 `object.__new__` 는 걷어낼 수 없다(`__init__` 이 LLM 매니저·TradeMemory·TradeWiki 를 만든다). 대신 `__init__` 이 세팅하는 속성을 하네스가 **명시적으로 전부** 채운다 — 빠진 속성은 `_try_evict_weakest_position` 의 광역 `except` 에 삼켜져 "보호 가드가 동작했다"로 오독된다.

## 2. 하네스 (인수 파일 안에서 새로 조립)

**독립성 규칙(grep 으로 검증한다):** 인수 파일은 S3·S4 가 만든 시험 모듈 — `test_execution_signal_gateway{,_wiring,_parity,_eviction}.py`·`test_execution_dispatch_reasons.py`·`test_execution_abandon_candidate.py`·`test_execution_child_command_close.py`·`test_engine_legacy_stale_eviction_characterization.py` — 와 `test_t11_entry_plan.py`(`_order_env`)·`test_risk_sizing.py`(`_rm`) 에서 **아무것도 import 하지 않는다.** S3 이전의 조립층(`test_execution_runtime.py` 의 runtime/store 조립, `test_execution_regime_recheck.py` 의 실제 RegimeOwner+sidecar 조립)은 실제 객체를 세우는 코드라 import 해도 된다. 브로커 fake 와 commands 의 콜백은 아래 요구 때문에 새로 적는다.

- **실물:** `ExecutionStateStore`(tmp_path)·`UnifiedEngine` 과 그 큐·`_process_event`·`ExitManager`·`KRExecutionRuntime`·`RequestBoundCommands`·`KISRequestBuilder`·`GuardedKISTransport`·`SignalGateway`·`RegimeOwner`·`CrossStrategyValidator`(LLM 미주입)·`_calculate_position_size`·**`src/risk/manager.py` 의 RiskManager 를 `_risk_validator` 로**(+`set_exit_manager`)·클래스 구현 그대로의 `_check_factor_budget`/`_get_core_reserve`/`_get_core_actual_value`/`_pending_strategy_notional`(인스턴스 속성으로 가리지 않는다)·`make_entry_stop_resolver(runtime.exit_manager, …)`.
- **단일 sidecar:** `rm._risk_validator is runtime.risk_manager` 이고 RegimeOwner 가 소유한 그 객체다(별도 객체를 꽂으면 `publish_risk` 투영을 못 받아 낡은 장부로 판정한다).
- **`rm._exit_exempt_ref = exits._exit_exempt`** — 제품(`run_trader.py:854`)과 같은 별칭. plain set 대입 금지.
- **남는 fake 와 이유:** 브로커 HTTP(실 KIS 로 나간다 — **ODNO 는 호출마다 증가**) · `_SigLog.get`(운영 DB — `rm._log_sig` 가 아니라 이 좁은 경계에서 갈아끼워 제품 `_log_sig` 본문은 태운다) · `_sector_lookup`(운영 DB) · `trading_ready` property 패치(토글 리스트) · 시계.
- **시계 6축 + 1:** `src.utils.session.datetime` · `src.core.engine.datetime` · **`src.core.engine.date`**(캘린더 오버레이의 `date.today()` — 빠지면 월말·월초에 ORD_QTY 가 달라져 달력 날짜에 따라 GREEN/RED 가 뒤집힌다) · CV 모듈 · runtime 주입 clock · `src.risk.manager` 의 `datetime`·`date` · 그리고 `src.utils.macro_calendar.is_macro_event_day` 주입(`risk/manager.py:290` 은 함수 안 재임포트라 모듈 patch 가 닿지 않는다). 동결 **날짜는 owner 의 `state['risk']['day']` 와 같아야** 한다(다르면 `day_admission_closed`).
- **사이징 오버레이 3종**(calendar·volatility·team_conviction)은 제품 스위치로 끄거나 경로 상수를 tmp 로 돌린다 — 어느 쪽이든 실제 HOME 의 캐시 경로에 닿지 않아야 하고(import 시점 상수라 `Path.home` patch 가 닿지 않는다), 선택을 자기 단언으로 남긴다.
- **`synthetic_home` 은 conftest 에 없다** — 인수 파일이 autouse fixture 로 직접 정의한다. store 는 fixture 의 yield 뒤 한 곳에서 `await store.close()`.
- **진입 시세의 모양:** owner state 에 `market_sources` 행을 두지 않고 gateway 의 `_quote` 가 첫 게시자가 되는 모양으로 고정한다(제품에 market source 결합이 아직 없다 — 차단 사유 6). 독스트링에 적는다.
- **구동:** `engine.emit(...)` 뒤 `_get_next_event()`→`_process_event()`. 재진입(eviction 의 SELL)은 큐를 비울 때까지 도는 pump. 끝에 `await asyncio.sleep(0)` 로 `_log_sig` 의 fire-and-forget 태스크를 소진시킨다(안 하면 도달 증거가 비결정적이다).
- **같은 종목 재구동 금지 또는 시계 전진:** on_signal 진입부의 종목별 30초 쿨다운(`_last_signal_time`)이 동결 시계와 충돌한다 — 2회차는 다른 종목을 쓰거나 동결 시계를 31초 이상 전진시킨다.

## 3. 시나리오 (wave 1 = 경로, wave 2 = 게이트)

모든 시나리오는 **바깥에서 관측 가능한 상태**(POST 본문·owner state·engine 장부·ErrorEvent)로 단언하고, 차단 표본은 **분기 도달 증거**(`block_gate`, spy 호출 수)를 함께 단언한다. 변이 앵커는 줄번호가 아니라 고유 문자열로 찾는다.

### H — 하네스 자기 단언 (wave 1, 나머지 전부의 근거)
- **H0:** monkeypatch 없는 `KRExecutionRuntime.trading_ready is False` · 단일 sidecar 동일성 · `_exit_exempt_ref` 별칭 · 여섯 시계가 같은 순간 · `engine.is_trading_hours() is True` 와 정규장·평일·비휴장 · 동결 날짜 == owner risk day · 주입된 `is_macro_event_day` 가 False · 오버레이 배율 1.0 · `_REPLACEMENT_COOLDOWN_SEC == 600` 등 `__init__` 속성 존재 · fake ODNO 가 증가 · 게시한 `EffectiveRiskPolicy` 의 임계가 공유 RiskConfig 에서 옮겨 적은 값과 같다 · teardown 에서 `conftest.VIOLATIONS` 증가 0.

### A — 정상 송신의 전선 값 (wave 1)
- **A1 MARKET BUY:** POST 1건, url 이 주문 경로, `tr_id`·`PDNO`·`ORD_DVSN='01'`·`ORD_QTY`(근거를 상수 주석으로 남긴 기대 수량)·`ORD_UNPR='0'`. owner 에 SUBMIT 1행·예약>0·`gateway.reserved_cash()` 와 일치. `_last_signal_time` 기록(H4 도달). 변이: 수량을 전선에 싣는 자리·tr_id 선택·H4 의 타임스탬프.
- **A2 LIMIT SELL(매수1호가):** fake broker 에 `get_best_bid` 를 붙여 실큐에서 그 가지를 처음 태운다. `ORD_DVSN='00'`·`ORD_UNPR`=호가·SELL tr_id. bid=0 표본은 `event.price` 로 떨어진다. 변이: `bid > 0` 검사 삭제.
- **A3 호가 조회 예외 → `event.price` 폴백 LIMIT.**
- **A4 가격 없는 SELL:** POST 0·owner 행 0·`commands.prepare` 호출 0(거부 지점이 `_bind` 임을 고정)·ErrorEvent 1. 사실 2 의 고정이다.
- **A5 증거 없는 BUY:** gateway 가 None 을 돌려주고 게시 0·prepare 0·POST 0(증거 조립 조건 중 하나를 비운 SIGNAL).

### B — UNKNOWN (wave 1, 무시험 칸)
- **B1:** ACK 에 주문번호가 없는 응답 → POST 1·`blocked_unknown`·예약 유지·`reserved_cash()>0`·`unresolved_symbols()` 에 포함. 그 뒤 **다른 종목** BUY 는 `unresolved_execution_evidence`(ErrorEvent)·POST 증가 0. 엔진 정지 후 `recover_unsent()` 는 그 행을 건드리지 않는다(`abandon_candidate` spy 의 호출 인자에 그 attempt 가 없다). `gateway.submit` 반환의 `status is UNKNOWN` 을 통과형 spy 로 단언.
- **B2 같은 주문번호 두 번:** 두 번째 ACK 는 기록을 거부당해 `result_not_recorded`(UNKNOWN)·owner 차단. 증가하는 ODNO 요구가 실제로 하중을 받는 유일한 자리다.

### C — claim 이전 실패, 갈래별 (wave 1)
- **C1 ready=False/True 쌍(②):** False → POST 0·`final_rejected`·`not_sent`·사유 `startup_reconciliation`·예약 4항 0, **그러면서** 이번 SIGNAL 의 게시 부산물(`entry_decision_facts` 행·출처 version 증가·owner.version 증가)은 남는다. True(다른 종목 또는 시계 전진) → POST 1.
- **C2 `claim_not_available`(①):** `lifecycle.claim` 이 False → 예약 0·POST 0. 변이: `_unsent` 호출을 결과 직접 반환으로.
- **C3 예약을 남기는 갈래(③④):** ③ 은 `command_scope` 를 통과한 뒤 claim 쪽에서 `ApplicationBlocked` 가 나게 만들고 `_bound`·claim 진입 spy 를 도달 증거로 단언한다(`_closing` 으로 만들면 claim 에 닿지 않고 같은 사유가 나온다 — 그 표본은 "명령 접수 경계"로 따로 이름 붙인다). ④ 는 claim 이 `OSError`. 둘 다 `prepared`·`claim_id None`·예약 불변·POST 0.
- **C4 CancelledError(⑤) → `recover_unsent()`:** 잔류 1행이 정확히 그 행만 쓸린다. 엔진이 도는 중의 호출은 거부.
- **C5 abandon 내부 실패:** `abandon_candidate` 가 `OSError` → 사유 보존·예약 유지·POST 0·엔진 루프 생존(다음 SIGNAL 정상 처리). 변이: `except Exception` 을 재던짐으로.
- 모든 C 표본에 `_pending_sector_map == {}`(결정 ⑮ 의 예외 경로)를 붙인다.

### E·F — 취소 0 과 legacy 불변 (wave 1)
- **E1:** A·B·C 의 모든 표본에서 `broker.cancel_all_for_symbol`·`broker.submit_order` 직접 호출 0. attach 뒤 legacy 장부에 행을 심으면 SIGNAL 이 RuntimeError 로 끝나고 취소 0(H7). `on_order` 와 `risk/manager.py` 의 `update_position` 두 legacy writer 가드를 짝으로 고정. **이 진술은 엔진 경로에 한정된다** — `KRScheduler._cleanup_stale_pending` 은 attach 를 모른 채 취소를 낸다(10C).
- **F1 legacy ORDER 구동:** runtime 없는 엔진에서 SIGNAL → 큐의 OrderEvent 를 다시 구동 → `broker.submit_order` 정확히 1회와 그 Order 의 (symbol·side·quantity·order_type·price). 대조는 **별도의 새 엔진**을 attach 해서 같은 모양의 ORDER 를 넣는다 — ErrorEvent 0·`errors_count` +1(같은 엔진에 attach 하면 H6·큐 비어있음 요구가 먼저 막는다).
- **F2 H6 실물:** 실제 inner RiskManager 를 attach **이전에** 붙이고 legacy 장부 1행 → `attach()` 거부.

### D·G — eviction 과 실제 게이트 (wave 2)
- **D1 eviction:** 실제 sidecar 가 "최대 포지션 수 도달"을 내게 만석을 owner 에 심는다(점수·손익이 **서로 다른** 비코어 손실 포지션 ≥3). pump 구동 → POST 1건이 최약 후보의 SELL 본문, 직접 broker 호출 0, 원 BUY 는 같은 사이클에 `G3_risk`. 최약 후보에 미해결 SELL 을 심은 표본은 다른 후보가 나간다. 변이: 정렬 키 역순·owner 미해결 제외.
- **D2 보호 가드 대 조용한 실패:** ① 전역 쿨다운 — 기록을 **후보 목록에 없는 제3 종목**으로 심는다(희생자 종목이면 종목 쿨다운에 가려 변이가 산다) + **양성 대조**(그 기록만 지우면 같은 fixture 가 실제로 축출한다). ② +5 우위 경계.
- **D3 exit_exempt:** (a) owner 의 `protection.exit_exempt` 에 든 종목은 축출되지 않는다. (b) **별칭 불변식** — 게시 뒤에도 `rm._exit_exempt_ref is exits._exit_exempt` 이고 내용이 owner DTO 와 정확히 같다(live 에만 추가한 종목은 사라진다 — 사실 5 를 GREEN 으로 고정).
- **D4 owner 읽기 실패의 fail-closed:** 현금 게이트·전략 예산 게이트·eviction 세 소비 지점이 각각 예외로 끝난다. 현금 게이트 표본은 전략 cap 을 0 으로 두어 뒤 게이트에 가려지지 않게 한다.
- **G1 실제 `can_open_position` 의 거부:** 최소 현금 미달·최소 포지션 금액 미달이 `G3_risk` 로 끝나고 POST 0·owner 행 0. (sidecar↔owner 결론 비교는 하지 않는다 — 사실 7.)
- **G2 죽은 legacy 보정의 짝:** attach 에서 engine 쪽 TOCTOU 보정은 0 을 더한다 — 미해결 BUY 가 있는 상태에서 `daily_max_trades`·섹터 한도 경계를 넘기는 BUY 는 **owner 가** 막는다(ErrorEvent 의 message 로 사유 단언 — prepare 의 reducer 가 던지면 owner 에는 기록이 없다).
- **G3 팩터 버킷:** `rm.config.factor_budgets` 를 주입(값 출처·확인 시점을 주석으로 — 미러 부채)하고 보유를 **같은 버킷의 형제 전략**에 심는다(신호 전략 자신에게 심으면 전략 예산 게이트가 먼저 막는다). `enforce=false` 는 통과·`enforce=true` 는 차단. fail-open 표본의 예외 주입은 `_check_factor_budget` 구간에만 건다.

## 4. 분업·검증

| wave | 범위 | 구현 | 독립 재현 |
|---|---|---|---|
| 1 | 하네스 + H0 + A·B·C·E·F | opus/high, 격리 worktree, `work/s5-accept-1` ← `<이 계획 커밋>` | opus/xhigh, 다른 실행, detached |
| 2 | D·G (같은 파일에 추가) | opus/high, `work/s5-accept-2` ← wave 1 통합 SHA | opus/xhigh |

- 파일 하나·writer 한 명이라 wave 는 직렬이다. 재현자의 일은 리뷰가 아니라 **변이 kill 실측**(자기 worktree 의 `src/` 에 넣고 → 해당 시험 RED 확인 → 원복, 마지막에 `git diff --stat -- src/ scripts/` 빈 출력), 독립성 grep, H0 을 깨뜨렸을 때 다른 시나리오가 vacuous GREEN 이 되지 않는지, 그리고 자체 변이 ≥3 이다. 구현자가 "동치 변이"라고 처분한 것은 의심한다.
- 인수 파일을 `volatility_targeting`/`team_conviction` 을 먼저 import 하는 파일 뒤에 놓고 1회 실행(단독 GREEN ≠ 전체 GREEN).
- 통합 뒤: 전체 suite UTC→KST 단독 직렬(`nice` 금지·다른 세션 pytest 확인·load 기록) → Codex Astra/xhigh 최종 broad 리뷰(포그라운드, 범위를 나눠 여러 번 — 원장 S5 조건 2 의 네 결정) → 원장·인계 문서 마감.

## 5. S5 가 하지 않는 것

- 제품 수정 전부. 결함 후보는 보고만 한다.
- sidecar↔owner 정책 이중 평가의 정합(10A3) · 실제 `_sector_lookup`·`SignalEventStorage`(운영 DB) · 실제 2분/5분 레짐 강등 경로로 만드는 stale · `KRScheduler._cleanup_stale_pending`(10C) · MODIFY·`prepare_cancel` · 성능.
- 인수 통과를 설치 허가로 읽히게 쓰지 않는다. 설치 차단 사유는 `docs/operations/claude-migration-handoff-2026-09-20.md` 의 목록이 정본이다.
