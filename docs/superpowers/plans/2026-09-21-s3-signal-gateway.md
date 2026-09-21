# S3 (B3a) — SIGNAL → gateway → owner prepare/dispatch (세부 계획)

> 2026-09-21 · 기준 `feature/engine-safety-design-20260917` `85a65bc` · 상위 계획 `2026-09-20-b2b3-request-bound-qualification.md` §2 의 S3 ·
> 진행·증거·체크리스트는 단계 원장 `docs/reviews/b2b3-stage-ledger-2026-09-20.md` 의 S3 절.
> 상태 표기는 **계획**이다. 이 문서의 어떤 항목도 구현·검증 완료를 뜻하지 않는다.
> 운영 경계 불변: main 병합·배포·재시작·주문·설정·Toss grant 변경 없음, 제품의 `trading_ready=False` 유지, MODIFY 미지원 유지.
> 줄 번호는 `85a65bc` 기준이며 worker 는 착수 시 다시 확인한다.

## 0. 이 단계가 끝나면 말할 수 있는 것 / 없는 것

- **있는 것:** runtime 이 붙은 엔진에서 실제 큐의 SIGNAL 이 기존 후보 판단(on_signal)을 그대로 거친 뒤 **owner 의 prepare/final 재검사/dispatch 한 길로만** 송신된다(정상 MARKET BUY · LIMIT/MARKET SELL). 송신하지 못한 요청은 예약을 남기지 않고, 왜 못 보냈는지가 사유 코드로 남는다.
- **없는 것:** 제품에 `KRExecutionRuntime` 을 만들거나 `attach()` 를 부르는 코드는 S3 뒤에도 **0건**이다 — 설치가 아니며 운영 경로에서 이 분기는 발화하지 않는다. `trading_ready` 는 항상 False 이므로 모든 GREEN 은 fake HTTP · 주입 시계 · 시험용 합성 startup 허가(`monkeypatch`) 위의 결과다. `config_version` 5축의 제품 조립과 PolicyContext 제품 publisher 는 10A3(factory), 직접 SELL 폴백·취소0건 해제·eviction 의 owner 경로 이관은 S4, 독립 실큐 인수는 S5 다.

## 1. 조사로 확정한 사실 (읽기 전용 3관점 + 설계 + 적대적 심사, coordinator 가 핵심 줄을 직접 대조)

요청 모델은 전부 claude-opus-5(조사·설계 high, 심사 xhigh), 관측 모델은 metadata 미노출이라 **미검증**이다. 정적 추적이며 런타임 관측이 아니다.

1. **폐기 분기는 한 곳이다.** `src/core/engine.py:527-533` — `self._execution_runtime is not None and event.type in (FILL, ORDER, SIGNAL)` 이면 `stats.errors_count += 1` · 로그 · return(핸들러 호출 전). 이 분기를 치는 기존 시험은 FILL 한 건뿐(`tests/test_execution_runtime.py:174-191`)이라 SIGNAL 쪽 **회귀 안전망이 없다.**
2. **엔진 루프는 직렬 단일 태스크이고, 예외 흡수는 핸들러 루프 안에만 있다.** `_process_event` 의 핸들러 루프(630-639)만 예외를 `errors_count`+ErrorEvent 로 삼킨다. 그 밖에서 새는 예외는 run 루프의 `except Exception`(459-465)으로 가는데 이 절은 `while self.running:` **밖**이라 예외 1건에 루프가 끝나고 `_shutdown()` 까지 간다(coordinator 대조).
3. **on_signal 은 emit 하지 않는다.** `return [OrderEvent.from_order(order, source='risk_manager')]`(2410)만 하고 큐 적재는 `_process_event` 의 `emit_many`(624-628)가 한다. 반환 OrderEvent 를 큐로 되돌리면 같은 527-533 분기가 ORDER 를 폐기한다.
4. **증거 조립(2308-2321) 뒤 남는 것:** 게이트 3개 — `_risk_validator.can_open_position`(2323-2350, 실패 시 eviction 시도) · `engine.can_open_position`(2356-2367) · `_pending_lock` 안 중복 주문 재검사(2376-2379). await 2개 — eviction(2337)·`_pending_lock`(2376). 네트워크 await(LLM 2113·매도호가 2264·섹터 2305)는 전부 조립 이전. Order 는 게이트보다 **먼저** 생성된다(2266-2297).
5. **legacy 장부 기록은 `_pending_lock` 블록(2381-2408)에 몰려 있다:** `_pending_orders`·`_pending_quantities`·`_pending_timestamps`·`_pending_sides`·`_reserved_by_order`(2405, `price*qty*1.015`)·`_pending_strategy`·`_pending_signal_cache`(2394) **와 `_last_signal_time`(2386)**. `_last_signal_time` 의 다른 기록 지점은 사이징 0 거부(2248)뿐이다(coordinator 대조).
6. **S4 대상 셋은 종착부 밖이다.** 90초 MARKET SELL 폴백(1899-1982, `broker.submit_order` 직접 호출 1957 — 거래시간 게이트 2018 보다 앞) · 취소0건 예약 해제(1985-2015) 는 on_signal 진입부에서 **`_pending_timestamps` 를 순회**한다(coordinator 대조). eviction 은 BUY 거부 경로(2335-2345)에서 `_try_evict_weakest_position`(1586-1696)을 불러 SELL SignalEvent 를 **큐에 emit** 한다(1676-1677), 후보 필터는 `if sym in self._pending_orders`(1612).
7. **attach 의 부수효과:** `risk_manager`·`_regime_adapter` 에도 runtime 이 서고, `update_position`(748)·`update_position_price`(896)·`on_order`(2450)·`on_fill`(2525) 등 raw writer 가 `ApplicationBlocked` 를 던진다. runtime 이 게시할 때마다 owner portfolio 가 `engine.portfolio` 에 투영된다(runtime.py:416-437) — `get_available_cash()` 의 분자는 owner 현금이지만 **owner 의 attempt 예약은 빠져 있지 않다.**
8. **예약 현금·전략 예약의 소비자는 다섯 곳이다.** `_reserved_cash`(property 1468-1471): G5_cash 조기 차단 2169 · `can_open_position(reserved_cash=)` 2352-2354 · 사이징 2716. `_pending_strategy_notional`(2426-2432, `_reserved_by_order` 를 읽음): 전략 예산 조기 차단 2193 · 사이징 2791. owner 의 `recompose_quantity` 는 같은 전략의 `snapshot.pending` 예약을 빼서 `strategy_remaining` 을 만든다(decisions.py:280-285).
9. **`trading_ready` 는 항상 False 이고 `_bound` 첫 줄이 `_owner_ready(dispatch=True)` 다**(runtime.py:61-64, commands.py:453→70-80). 따라서 gateway 를 붙이는 순간 합성 허가가 없는 모든 dispatch 는 **claim 이전에** 실패한다.
10. **미claim prepared 를 끝내는 전이가 없다.** lifecycle 의 전이는 `prepare_candidate`·`claim`·`record_result`·`reconcile` 뿐(lifecycle.py:252·326·363·445). `record_result` 는 `attempt['claim_id'] != claim_id` 면 무변경(369-373)이라 claim_id 가 None 인 attempt 를 끝낼 수 없다. `claim` 은 sibling submit 미해결 등에서 **예외 없이 False** 를 돌려준다(337-352). 두 경우 모두 `_dispatch`(commands.py:543-550)는 `NOT_SENT/claim_not_available` 을 돌려주고 attempt 는 `prepared`·예약 4항·`entry_policy_effects.pending_sectors[symbol]` 이 남는다.
11. **식별자:** `claim_id` 는 `_dispatch` 안 `uuid4().hex` 프로세스 로컬 permit(commands.py:539), broker `OrderRef` 는 ACK 뒤에야 생긴다(491-505). 송신 전에 존재하고 이벤트가 나를 수 있는 것은 `(intent_id, attempt_id)` 뿐이다.
12. **command_scope 제약:** `start_command_result` 가 `_command_scopes[token] is asyncio.current_task()` 를 요구한다(runtime.py:731-739) — dispatch 를 `create_task` 로 감쌀 수 없다. 명령 스코프 안에서 `runtime.shutdown()` 을 트리거하면 `command_shutdown_self_wait`(763-767).
13. **`EffectiveRiskPolicy` 는 15필드이고 hybrid 축이 없다**(risk_policy.py:88-108). `PolicyContext.from_dict` 가 canonical 키 집합을 재대조한다(policy_snapshot.py:61-94). `build_owned_snapshot` 은 `dataclasses.replace` 로 policy 를 옮긴다(161-163). `tests/test_execution_decision_facts.py:563` 의 `_parity_snapshot` 도 `replace` 다.
14. **`config_version` 5축 중 두 축은 gateway 가 들고 있지 않다:** sizing 의 배분 표는 `engine.py:2664` 메서드 안 리터럴, stops 는 `run_trader.py:498 _strategy_exit_params` + ExitManager 표.
15. **`qualification_sources` 는 이름으로 키잉되어 덮어써진다**(`sources[name] = dict(version, as_of, digest)`, commands.py:168-187 — coordinator 대조). 행 수는 이름 수(`regime` + 판단한 종목 + 전략×섹터)로 **유계**다. 무한히 자라는 것은 `intent_id` 로 키잉된 `entry_decision_facts` 뿐이다. 상태 저장소는 **단일 checkpoint 행**이고 이력이 없다(store.py:127-128) — state 에서 지운 facts 는 저장소 어디에도 남지 않는다.
16. **sector 조회 실패:** `engine.py:2302-2307` 의 `except Exception` 이 None 으로 뭉갠다. owner 는 `if entry.sector and ...`(risk_policy.py:632·666)라 None 에서 섹터 한도를 건너뛰고 `PENDING_SECTOR_SET` 효과(684)도 내지 않는다.
17. **시계가 넷이다:** legacy `KRSession`(`src/utils/session.py`, naive `datetime.now()`), engine 모듈의 naive `datetime.now()`(1877·2248·2383·2386 — 쿨다운·pending 타임스탬프), CV 모듈 시계(S2 의 Frozen 패턴), runtime 주입 시계(`runtime._now()`, aware KST). 실큐 시험은 넷을 같은 순간으로 맞춰야 벽시계 의존이 없다.
18. **하네스:** `tests/test_execution_runtime.py`(동결, import 만)의 `wait_queued`/`queued` 패턴(72-89)으로 실제 큐를 구동한다. `bind_execution_runtime` 은 `running`·비어 있지 않은 큐에서 RuntimeError(engine.py:294-301) — 시험은 SIGNAL emit **이전에** attach 를 끝낸다. `tests/test_execution_qualification_publishers.py` 의 `manager`/`capture`/`publish`/prepare+send helper 와 `tests/test_execution_decision_facts.py:207-247` 의 `paused_dispatch`/`apply_change` 를 재사용한다.
19. tests/ 전체에 `reason_code` 를 단언하는 줄 0건 · `claim_not_available` 참조 0건(조사 B grep) — 사유 보존이 기존 시험을 깨지 않는다.

## 2. coordinator 결정 (설계 권고 → 심사 이견 → 확정)

| # | 쟁점 | 확정 | 근거·심사 처분 |
|---|---|---|---|
| ① | 인계점 | `_process_event` 527-533 에서 **SIGNAL 만** 분리. `runtime.gateway` 가 설치돼 있으면 `rm.on_signal(event)` 를 **직접 await** → 반환 OrderEvent 와 `rm._last_qualification_evidence` 를 **다음 await 이전에 지역값**으로 받아 gateway 로. gateway 미설치면 현행 폐기 유지(fail-closed). FILL·ORDER 거부는 불변 | 사실 3. 종착부를 gateway 호출로 바꾸는 안은 on_signal 안에 owner await 를 심어 `_pending_lock` 과 command_scope 가 겹치고 S4 를 먼저 손대야 성립한다 |
| ② | **인계점의 예외 흡수(심사 must-fix)** | H1 호출부를 try/except 로 감싸 핸들러 루프와 같은 의미(`errors_count += 1` + ErrorEvent + 계속)로 흡수 | 사실 2 — 없으면 owner 예외 한 건이 엔진 루프를 내린다 |
| ③ | "ORDER 는 command ID 만"의 S3 해석 | **S3 에서는 attach 모드에서 ORDER 이벤트를 큐에 싣지 않는다.** prepare 와 dispatch 를 gateway 가 **같은 태스크에서 잇달아** 수행하고(**`command_scope` 는 각각 따로 연다** — `commands.py` 의 `prepare`·`dispatch`. S5 Codex 5차-A 정정: 초안의 "같은 command_scope" 는 구현과 달랐다), 식별자는 `(intent_id, attempt_id)` 다. 상위 계획 문구의 목적("mutable Order 를 큐로 나르지 않는다")은 큐를 아예 거치지 않음으로써 충족된다 | 사실 3·11·12. 대가: dispatch 의 network await 동안 엔진 루프(EXECUTION_FILL 포함)가 멈춘다 — legacy `on_order` 도 같은 루프에서 `broker.submit_order` 를 await 하므로 송신 대기 자체는 새 비용이 아니다. **다만 처리 순서는 legacy 와 같지 않다(S5 Codex 5차-A 정정):** legacy 에 있던 SIGNAL→ORDER 사이의 큐 경계가 사라져 엔진은 판단부터 dispatch 완료까지 한 번에 기다리고, 그 사이 도착한 체결은 먼저 적용되는 대신 후보를 `unapplied_execution_ingress` 로 거부시킨다 — 낡은 경제 상태로 송신하는 위험은 이 검사가 막지만, 체결 도착 시의 후보 거부·재평가 정책과 지연 상한은 10A3 의 인수 조건이다. **S5 최종 broad 리뷰(Codex 5차-A)의 확인 결과: 이 해석은 상위 계획의 안전 목적(단일 송신로·중복 dispatch 0·legacy writer 차단)에 부합한다. 제품의 ORDER 구독자는 `on_order` 한 곳뿐이고, eviction 의 SELL 은 큐 적재·원 BUY 는 같은 호출에서 종료라 재진입 결함이 아니다** |
| ④ | attach 모드의 legacy 장부 | **쓰지 않는다:** `_pending_orders`·`_pending_quantities`·`_pending_timestamps`·`_pending_sides`·`_reserved_by_order`·`_pending_strategy`·`_pending_signal_cache`. **유지한다(심사 must-fix):** `_last_signal_time[symbol] = now` — 지우면 성공 송신 뒤 30초 쿨다운이 영원히 무장되지 않는다 | 이중 장부 제거. 중복 주문은 owner 의 `unresolved_symbol_attempt`(commands.py:349). `_pending_timestamps` 를 안 쓰므로 S4 대상인 진입부 stale 루프 둘은 attach 에서 순회 대상이 없다(사실 6). `_pending_signal_cache` 의 소비자(`on_fill` 2547·`update_position` 768)는 attach 에서 전부 `ApplicationBlocked` 라 읽을 수 없다 — **체결 메타(entry_tags·전략) 인계와 pending 교착 감시(`dashboard/data_collector.py`·`monitoring/health_monitor.py` 가 `_pending_timestamps`/`_pending_quantities` 를 읽음)는 attach 에서 빈 값을 본다. fill projection 을 다루는 10A3/10C 로 명시 이월** |
| ⑤ | **eviction(심사 must-fix — 설계의 "S4 이관" 기각)** | attach 모드에서 eviction **호출 자체를 막는다**(2335-2345). owner 경로 이관은 S4 | ①이 SIGNAL 폐기를 없애는 순간 eviction 이 큐에 낳는 SELL SIGNAL 이 gateway 를 타고 실제 POST 가 된다. 후보 필터(`_pending_orders`)도 ④로 죽는다. 설계가 든 "순회 대상 부재" 근거는 진입부 stale 루프에만 해당한다 |
| ⑥ | 인수 조건 6 (사이징 divergence) | `_calculate_position_size` 본문은 불변. **두 지점**을 attach 분기: `_reserved_cash` 와 `_pending_strategy_notional`(심사 must-fix). 둘 다 gateway 의 동기 읽기 helper 를 부른다(⑪) | 사실 8 — 소비자 다섯 곳이 두 지점에서 한 번에 정합된다. S2-2 특성화 27건은 legacy 에서 불변. 심사 지적대로 G5_cash(2169)·전략 예산(2193)도 함께 바뀌므로 RED 에 포함 |
| ⑦ | 미claim prepared 의 종료 | 새 원자 전이 `abandon_candidate`(단일 reducer, 터미널은 기존 `FINAL_REJECTED` 재사용·구분은 `reason`). 호출 지점: claim 이전 **예외** 실패 **와 `claimed=False`(심사 must-fix)** | 사실 9·10. "claim 후 record_result" 안은 claim 과 record 사이 await 에서 죽으면 POST 한 적 없는 attempt 가 `submitting`+claim_id 로 남아 재시작 대사가 "송신됐을 수 있음"으로 읽는다. `claim_id is None` 이 "POST 시작된 적 없음"의 증거 |
| ⑧ | `_dispatch` 의 예외 분류 | **abandon 은 `request.command is CommandKind.SUBMIT` 일 때만 부른다(wave 1 독립 재현에서 확정).** `CommandValidationError` → `exc.reason` 보존 + abandon · `claimed=False` → `claim_not_available` 유지 + abandon · `ApplicationBlocked`(종료 중) → `command_admission_closed`, abandon 은 시도하지 않는다(그 자체가 다시 던진다) · 그 밖(SQL 등) → `dispatch_failed`, abandon 미시도 | 자식 명령(cancel/modify)의 행은 `prepare_candidate`(lifecycle.py:285-286)가 항상 부모의 `order_ref` 를 싣기 때문에 `abandon_candidate` 의 `order_ref is None` 가드에 걸려 **구조적으로 abandon 될 수 없다** — 그 가드를 풀면 '이미 보낸 주문'의 증거를 가진 행을 지우는 길이 되므로 풀지 않는다. S3 gateway 는 SUBMIT 만 내므로 S3 범위에서는 누수가 없고, **미claim 자식 명령의 잔류(같은 부모의 이후 취소를 `previous child command unresolved` 로 막는다)는 취소를 owner 경로로 옮기는 S4 의 설계 항목**으로 이월한다. 뒤의 두 경우 attempt 는 prepared 로 남는다 — **재시작 시 미claim prepared 의 자동 정리(sweep)는 10A3 의 startup 대사 범위**로 이월하고, S3-1 에서 "restore 뒤에도 `abandon_candidate` 가드가 성립한다"만 고정한다 |
| ⑨ | PolicyContext 제품 publisher·`config_version` 5축 조립 | **10A3 으로 미룬다.** gateway 는 `config_version` 을 생성자 주입값으로 받고, S3-3 이 owner 쪽 대조(`stale_decision_config_version`)를 처음으로 실효화하는 RED 를 세운다 | 사실 14 — S3 에서 복사본을 만들면 원본과 갈라져 config 축이 조용히 헛돈다. `run_trader.py` 는 S3 허용 파일이 아니다 |
| ⑩ | 정리 writer(인수 조건 11) 의 범위 **(설계·심사 둘 다 기각, coordinator 축소)** | `reset_daily` 가 **`entry_decision_facts` 만** 비운다. `qualification_sources` 는 **건드리지 않는다** | 사실 15 — 출처 행은 이름 수로 유계라 일별 무한 성장이 아니다. 설계의 축약 행 `{'version': n}` 은 `commands.py` 의 `previous['as_of']`·`current['digest']` 인덱싱을 KeyError 로 깨고(심사 must-fix) S3-3 과 숨은 의존을 만든다. 남겨 둔 전일 출처 행은 이미 `as_of` 당일성 검사로 재사용이 막힌다. `ponytail:` 출처 행이 수천을 넘기면 counter 를 별도 키로 분리한 뒤 행 삭제로 승격. **facts 를 지우면 저장소에 흔적이 없으므로** gateway 가 게시 시점에 facts 핵심 필드·digest 를 로그로 남긴다(⑫). 분석 원장 projection 은 기존 잔여(10C) |
| ⑪ | gateway 설치점과 engine 의 읽기 경로 | `runtime.py` 에 `gateway` 속성(None)과 `install_gateway(gateway)` 를 둔다(S3-5 단일 writer). engine 은 safety 패키지를 `self._execution_runtime.gateway` 한 길로만 만난다: `submit(...)`·`reserved_cash()`·`pending_strategy_notional(strategy)` | engine 에 owner state 해석을 복제하지 않는다(이중 정의 금지). 두 읽기 helper 는 `risk_policy.py:713-716`·`decisions.py:280-285` 와 **같은 식을 재사용**한다 |
| ⑫ | gateway 의 BUY/SELL 차이와 실패 시 상태 | **AUTOMATIC BUY:** evidence 필수(None 이면 게시 0·prepare 0) → `observe_entry_quote` → `publish_policy_context` → `publish_qualification`(출처→facts) → `prepare` → `dispatch`. **SELL:** evidence 를 요구하지 않고 `publish_qualification` 을 건너뛴다(S1 계약: facts 소비는 SUBMIT+BUY+AUTOMATIC 뿐). 게시~prepare 실패는 reducer 예외라 commit 0. "출처만 게시·facts 실패" 부분 상태는 되돌리지 않는다(version 되감기 금지) — 새 송신 허가를 만들지 않음을 시험으로 고정하고 재시도가 version n+1 로 정상 진행함을 대조로 고정 | 설계 D7 + 심사 "부분 게시 뒤 재시도 대조 누락" |
| ⑬ | 실패 사유의 노출 | 로그(운영자용)에만 남긴다. 대시보드·텔레그램 변경은 S3 에 없다. 사유 문자열에 금액·계좌가 들어 있지 않음을 시험으로 고정 | 설계 D8, 심사 동의 |
| ⑭ | sector 조회 실패(인수 조건 8) | attach 모드의 **소비 지점**(engine.py:2302-2307)에서만 예외를 거부로 바꾼다. `run_trader.py` 의 lookup(예외→None)은 10C | legacy 0줄 차이 유지. 위험의 실체는 owner 쪽 fail-open(사실 16) |
| ⑮ | `_pending_sector_map`(심사 must-fix) | attach 모드에서 H1 호출부가 gateway 결과와 **무관하게** `_pending_sector_map.pop(symbol, None)` | 성공 경로의 유일한 pop(`update_position` 763)이 attach 에서 막혀 있어, 그 종목을 매도하는 순간 다시 "pending 섹터"로 계산돼 섹터 한도를 재시작까지 좁힌다. attach 의 pending 섹터 정본은 owner 의 `entry_policy_effects.pending_sectors` |
| ⑯ | S3-6 분할 | engine.py 배선을 **S3-6a(경로: H1·H4·H5·⑮)** 와 **S3-6b(정합: H2·H3 + 게이트 순서·시계)** 두 실행으로 나눈다(같은 파일이라 순차) | 60분·시험 ≤25건 한도. 경로와 사이징 정합은 RED 도 변이도 독립이다 |
| ⑰ | hybrid 축 동결 예외 | 기존 시험 파일 수정은 **helper 2곳의 생성 인자 1줄씩**(`tests/test_execution_risk_policy.py:30`·`tests/test_execution_resources.py:36`)뿐. `_parity_snapshot` 은 `replace` 라 수정 불필요(심사). 새 필드에 **기본값 금지** | 기본값은 "설정 hybrid on 인데 신고 없음"과 "정말 off"를 구분 못 하게 해 S1 R1 이 막은 fail-open 을 정책 쪽에 재생한다 |

## 3. 계약

1. **단일 송신로.** attach 모드에서 주문 POST 는 `RequestBoundCommands.dispatch` 한 길뿐이다. gateway 는 owner 의 판정을 복제하지 않고(가격·수량·세션·경제·섹터는 `_evaluate` 가 본다) on_signal 을 복제하지 않는다.
2. **후보 판단이 먼저다.** legacy 거래시간 게이트(`KRSession`)·쿨다운(30초/300초/90·600초)·G15~G17 을 통과해 on_signal 이 OrderEvent 를 **돌려준** 요청만 gateway 로 간다. owner 세션 표가 허용하는 틈(08:50-09:00, 15:20-15:40)에 새 송신 창이 열리지 않는다. 쿨다운은 facts 로 재현하지 않는다.
3. **증거는 요청 지역값이다.** H1 은 `on_signal` 반환 직후 **다음 await 이전에** `_last_qualification_evidence` 를 지역 변수로 받는다. on_signal 이 None/빈 목록을 돌려주면 속성에 증거가 남아 있어도 gateway 를 부르지 않는다.
4. **식별자.** `intent_id` 는 gateway 가 발급하고 같은 종목·side·전략의 재시도는 같은 `intent_id` 를 재사용한다(목표 수량은 lifecycle `_replacement` 가 관리). `attempt_id` 는 송신 시도마다 새로 발급한다.
5. **송신하지 못한 요청은 예약을 남기지 않는다.** claim 이전 실패(예외·`claimed=False`)는 `abandon_candidate` 로 같은 호출 안에서 끝난다. claim 이후 실패는 기존 `record_result`→`FINAL_REJECTED` 경로 그대로다. UNKNOWN(주문번호 없는 ACK)은 예약을 **유지**한다. 취소 ACK·취소 0건은 최종성이 아니다.
6. **KIS POST 재전송 금지.** gateway 는 같은 attempt 로 dispatch 를 재시도하지 않는다.
7. **legacy(no-runtime)·US 경로는 실행 줄 0줄 차이.** engine.py 의 추가 실행 줄은 전부 `_execution_runtime is not None` 가드 안이다(§5).
8. **보고 문장.** "S3 는 설치가 아니다 · 제품 attach 호출자 0건 · 합성 startup 허가 위의 GREEN" 을 매 보고에 넣는다.

## 4. 하위 단계

wave 는 최대 2 병렬(이 호스트는 2 vCPU·3.8GB — pytest worker 동시 ≤2, 전체 suite 는 **읽기 전용 에이전트도 없이** 단독 직렬).

| wave | 단계 | 제품 파일(단일 writer) | 새 시험 파일 |
|---|---|---|---|
| 1 | S3-1 ∥ S3-2 | `lifecycle.py` ∥ `risk_policy.py` | `test_execution_abandon_candidate.py` ∥ `test_execution_policy_hybrid_axis.py` |
| 2 | S3-3 ∥ S3-4 | `commands.py` ∥ `day_recovery.py` | `test_execution_dispatch_reasons.py` ∥ `test_execution_decision_facts_gc.py` |
| 3 | S3-5 | `gateway.py`(신규)·`runtime.py`(설치점만) | `test_execution_signal_gateway.py` |
| 4 | S3-6a | `src/core/engine.py`(H1·H4·H5) | `test_execution_signal_gateway_wiring.py` |
| 5 | S3-6b | `src/core/engine.py`(H2·H3) | `test_execution_signal_gateway_parity.py` |

공통: worker 는 harness 격리 worktree 에서 `git switch -c work/<이름> <base SHA>` 로 시작하고 기준선 시험을 첫 단계로 돌린다. RED 커밋이 제품 커밋보다 앞. 새 시험 파일은 `synthetic_home` autouse 를 자체 선언하고 벽시계 의존 0, UTC·KST 양쪽 통과. 기존 시험 파일 수정 0줄(예외는 ⑰의 2줄). 부재 RED(ImportError·AttributeError·TypeError)는 표시만 하고 증거로 세지 않는다. **변이 전건 kill 이 인수 조건**이며 독립 재현자는 자체 변이를 3종 이상 더한다.

### S3-1 — `lifecycle.py`: 미claim prepared 의 원자적 종료 `abandon_candidate`

- **목표:** lifecycle 에 미claim prepared 를 원자적으로 종료하는 단일 전이를 만든다. `prepared`(claim_id None) 에서 터미널로 가는 간선이 현재 없어 — `prepare_candidate`·`claim`·`record_result`·`reconcile` 뿐(lifecycle.py:252·326·363·445) — 예약(현금·수량·exposure·planned_risk)과 `entry_policy_effects.pending_sectors[symbol]` 이 영구 잔류한다.
- **제품 파일(단일 writer):** `src/execution/safety/lifecycle.py`
- **시험 파일:** `tests/test_execution_abandon_candidate.py`
- **의존:** 없음
- **고정 인터페이스:**
  - async def abandon_candidate(self, attempt_id: str, *, reason: str) -> bool — 단일 owner.mutate reducer. 가드: attempt['state']=='prepared' and claim_id is None and command_status is None and order_ref is None and observed_quantity==applied_quantity==0 and not evidence_conflict. 통과 시 같은 reducer 안에서 state/status='final_rejected', command_status='not_sent', reason 기록, `_release_resources(state, attempt_id)`(lifecycle.py:226-236, 내부에서 clear_settled_pending_sector 호출). 하나라도 어긋나면 무변경 False.
  - 터미널 값은 기존 `FINAL_REJECTED` 를 재사용한다 — 새 OrderState 를 만들면 TERMINAL_STATES(47-51)·`_replacement`(179-186)·`_evaluate` 의 터미널 판정(commands.py:338-347)·`reconcile`(472·489·511) 네 곳에 파급된다. 미송신 포기와 '보냈는데 거부됨'의 구분은 `reason` 필드 하나로 한다.
- **RED:**
  - [행동] prepared 상태(claim_id None·reserved_cash != '0')인 attempt 하나만 있는 intent 에서 `lifecycle.replacement_quantity(intent_id)` 가 0 을 돌려주고 그 attempt 를 끝낼 수 있는 전이가 없다 — 같은 intent 의 재시도가 `previous attempt unresolved or target exceeded`(lifecycle.py:281)로 영구 차단됨을 단언. 종료 후에는 `replacement_quantity == target` 이어야 한다.
  - [행동] prepared attempt 를 `record_result(attempt_id, <임의 claim_id>, NOT_SENT)` 로 끝내려 하면 claim_id 불일치(369-373)로 무변경이고 예약이 그대로임을 단언 — 기존 경로 재사용이 불가능함을 코드로 고정.
  - [행동] 종료 후 `entry_policy_effects.pending_sectors[symbol]` 이 사라지고, 같은 종목의 다른 미해결 submit 이 있으면 사라지지 않는다(clear_settled_pending_sector:204-222 의 조건 재현).
  - [행동] claim 된 attempt(state 'submitting', claim_id 있음)에 종료를 호출하면 거부되고 예약 4항이 그대로 유지된다 — 이미 POST 가 시작됐을 수 있는 attempt 의 예약을 푸는 경로가 되지 않는다.
  - [행동] `observed_quantity > 0`(체결 관측 선도착) 또는 `evidence_conflict` 인 attempt 에 호출하면 거부 — 체결 사실을 지우지 않는다.
  - [행동] 종료가 단일 mutate 임을 고정: reducer 안에서 예외를 던지면(가드 실패) `owner.state` 의 attempt·예약·pending_sector 가 바이트 단위로 무변경(application.py:426-440 의 deepcopy 커밋 의미 재현).
  - [행동·결정 ⑧] checkpoint 를 저장하고 새 owner 로 restore 한 뒤에도 `prepared`·`claim_id None` attempt 에 `abandon_candidate` 가 성립해 예약 4항이 0 이 된다(재시작 시 자동 정리 sweep 자체는 10A3 — 여기서는 가드가 restore 뒤에도 같음만 고정).
- **변이(전건 kill 이 인수 조건):**
  - 가드에서 `claim_id is None` 제거 → claim 된 attempt 시험 실패
  - 가드에서 `observed_quantity == applied_quantity == 0` 제거 → 체결 선도착 시험 실패
  - `_release_resources` 호출 제거 → 예약 해제 시험 실패
  - `clear_settled_pending_sector` 가 부르는 조건을 무시하고 무조건 pop → 형제 submit 보존 시험 실패
  - state 를 `final_rejected` 로 바꾸지 않고 예약만 해제 → replacement_quantity 시험 실패(터미널이 아니면 `_replacement` 가 여전히 차감)
- **위험:** 새 터미널 진입점이 하나 더 생긴다 — 가드 조건 중 하나라도 빠지면 이미 송신된 attempt 의 예약을 푸는 경로가 된다. 그래서 '거부되어야 한다' RED 3건(claim 됨·체결 선도착·evidence_conflict)을 성공 RED 보다 먼저 세운다.

- **통합된 실제 인터페이스(wave 1, `86ccc7e`):** 사유는 attempt row 의 기존 키 **`reason_code`** 에 기록한다(명세의 'reason 필드'는 새 키가 아니다 — `record_result`·`reconcile` 과 같은 키). `reason` 이 str 이 아니거나 공백뿐이면 `ValueError`. `_require_admission()` 은 부르지 않는다(`record_result`·`reconcile` 과 같은 '끝내는 전이' 관용구 — day admission 이 닫힌 뒤에도 미송신 attempt 의 예약은 풀 수 있어야 한다. 종료 중의 `ApplicationBlocked` 는 `owner.mutate` 가 그대로 던진다). 가드 실패는 False·무변경, reducer 내부 예외(불완전 예약 pair)는 예외·무변경.

### S3-2 — `risk_policy.py`: `EffectiveRiskPolicy.hybrid_enabled` 필수 축

- **목표:** `EffectiveRiskPolicy` 에 hybrid 축을 실어 S1 잔여 7·Codex S1 재리뷰 조건을 닫을 준비를 한다. 현재 필드 15개에 hybrid 축이 없어(risk_policy.py:88-108 확인) `facts.hybrid_enabled` 는 게시자 자기신고이고 대조 상대가 없다.
- **제품 파일(단일 writer):** `src/execution/safety/risk_policy.py`
- **시험 파일:** `tests/test_execution_policy_hybrid_axis.py`, `tests/test_execution_risk_policy.py(helper 1줄)`, `tests/test_execution_resources.py(helper 1줄)`
- **의존:** 없음
- **고정 인터페이스:**
  - `EffectiveRiskPolicy.hybrid_enabled: bool` — **필수 필드, 기본값 없음**(기본값을 주면 '설정이 hybrid on 인데 신고가 없다'와 '정말 off'가 구분되지 않아 R1 이 막은 fail-open 이 정책 쪽에 재생된다). `__post_init__` 에서 `type(...) is bool` 강제.
  - `PolicyContext.to_dict/from_dict` 는 bool 을 그대로 나르므로 직렬화 코드 변경 0 — 단 `from_dict` 의 canonical 키 집합 재대조(policy_snapshot.py:61-94)가 키 추가를 강제하므로 왕복 시험으로 고정한다.
  - `build_owned_snapshot` 은 `replace(context.policy, regime_min_cash_reserve_pct=...)`(policy_snapshot.py:161-163)로 policy 를 옮기므로 새 필드는 자동 전파된다 — 별도 배선 없음을 시험으로 고정.
- **RED:**
  - [부재 RED — 표시만, 증거로 세지 않는다: 오늘은 필드가 없어 hybrid on 인 policy 를 만들 수조차 없다(TypeError). 이 단계의 행동 RED 는 S3-3 의 `decision_hybrid_mismatch` 다] hybrid on 인 policy 를 `PolicyContext` 로 만들어 `to_dict` → `from_dict` 왕복하면 hybrid 상태가 사라져 off 인 policy 와 구분되지 않는다(오늘 통과 = 결함). 수정 후에는 왕복 뒤에도 True 가 남고, 키를 지운 dict 는 `invalid_policy_context_fields` 로 거부.
  - [행동] `build_owned_snapshot` 이 돌려준 `snapshot.policy.hybrid_enabled` 가 게시된 `PolicyContext.policy.hybrid_enabled` 와 같다(regime reserve replace 를 거쳐도 보존).
  - [행동] `hybrid_enabled` 에 bool 아닌 값(1·'true')을 넣으면 거부 — truthy 통과 금지.
  - [대조] 기존 helper 2곳(결정 ⑰ — `_parity_snapshot` 은 `dataclasses.replace` 라 수정 불필요)이 새 필드를 넘긴 뒤 `test_execution_risk_policy`·`test_execution_resources`·`test_execution_decision_facts` 의 기존 단언이 한 줄도 바뀌지 않고 전건 통과(구현자가 numstat 으로 보고).
- **변이(전건 kill 이 인수 조건):**
  - 필드에 `= False` 기본값을 붙임 → 키 누락 거부 시험 실패
  - `type(...) is bool` 을 truthy 검사로 → 1/'true' 거부 시험 실패
  - `from_dict` 의 canonical 키 집합에서 새 키를 빼 관대하게 → 왕복 시험 실패
  - `build_owned_snapshot` 의 `replace` 를 새 dataclass 생성으로 바꿔 필드 누락 → 전파 시험 실패
- **위험:** 필수 필드 추가는 계획서 §2 의 '기존 시험 동결'에 대한 **명시 예외**다. 예외 범위는 helper 2곳(`tests/test_execution_risk_policy.py:30`·`tests/test_execution_resources.py:36`)의 생성 인자 1줄씩으로 한정하고(원장 인수 조건 1 이 요구한 '동결 예외 범위를 Plan 에서 명시'), 기존 단언·기대값 변경은 금지. coordinator 가 통합 전 `git diff --numstat -- tests/` 로 삭제 0줄을 확인한다.

### S3-3 — `commands.py`: hybrid 대조 · 사유 보존 · claim 이전 실패의 종료 · schema3 now 계약

- **제품 파일(단일 writer):** `src/execution/safety/commands.py` · **시험:** `tests/test_execution_dispatch_reasons.py` · **의존:** S3-1, S3-2
- **고정 인터페이스:**
  - `_decision_facts` 에 한 줄: `_require(facts.hybrid_enabled == snapshot.policy.hybrid_enabled, 'decision_hybrid_mismatch')` — 기존 sector `_require`(260) 옆, prepare·final 공통 관문 안.
  - `_dispatch`(543-550)의 실패 분류(결정 ⑧ — **abandon 호출은 SUBMIT 한정**, 자식 명령은 현행대로 남긴다): `CommandValidationError as exc` → `reason_code=exc.reason` + `await self.runtime.lifecycle.abandon_candidate(attempt_id, reason=exc.reason)` · `claimed is False` → `claim_not_available` 유지 + abandon(reason `'claim_not_available'`) · `ApplicationBlocked` → `command_admission_closed`, abandon 미시도 · 그 밖 → `dispatch_failed`, abandon 미시도. `CommandResult.reason_code` 는 자유 문자열이라 타입 변경 0. abandon 이 False 를 돌려주면(남의 claim 이 붙음 등) 결과는 그대로 NOT_SENT 이고 예약은 유지된다 — 삼키지 않고 로그.
  - 새 종료 경로는 **claim 이후 실패에 관여하지 않는다** — transport guard 거부는 여전히 `record_result`→`FINAL_REJECTED`(lifecycle.py:427-430).
- **RED:**
  - [행동] 합성 허가 없이(`trading_ready=False`) prepare 성공 후 dispatch → NOT_SENT 뒤 attempt 가 `prepared`·`claim_id is None`·`reserved_cash != '0'`·`pending_sectors[symbol]` 존재(오늘 통과 = 누수 증명). 수정 후 `final_rejected`·예약 4항 0·pending_sector 제거·POST 0·같은 intent 로 재 prepare 가능.
  - [행동] `_bound` 실패 원인 3종(binding 부재 / `_owner_ready` 실패 / `decision_facts_expired`)의 `result.reason_code` 가 서로 다르다 — 오늘은 전부 `claim_not_available`.
  - [행동] sibling submit 미해결로 `claimed=False` → 위 3종과 다른 코드(`claim_not_available`)이고 **예약 4항 0·pending_sector 해제·POST 0**. 대조: 이미 다른 claim 이 붙은 attempt 에는 abandon 이 거부되고 예약 유지.
  - [행동] 설정 hybrid on 인 policy 를 게시하고 `facts.hybrid_enabled=False` 로 위조한 자동 BUY prepare → `decision_hybrid_mismatch`·POST 0(오늘 통과). 대조: off+False 통과, on+True 는 S1 의 `unsupported_hybrid_sizing`.
  - [행동] `publish_policy_context` 로 `config_version` 만 바꾼 뒤 기존 facts 로 dispatch → `stale_decision_config_version`·NOT_SENT·POST 0·예약 0 — config 축이 처음으로 실효를 갖는다.
  - [행동] schema3 재유도 계약: horizon 의 `classified_at` 당일 게이트를 넘긴 시각으로 시계를 민 뒤 dispatch → `_recheck_regime` 이 `facts.decided_at` 이 아니라 **현재 now** 로 평가한다. 대조: 재유도 문자열이 같으면 POST 1.
  - [행동] `ApplicationBlocked`(종료 중) dispatch → `command_admission_closed`·POST 0, abandon 을 시도하지 않아 2차 예외가 새지 않는다.
  - [행동] 사유 문자열에 금액·계좌·종목 수량이 섞이지 않는다(결정 ⑬).
  - [대조·결정 ⑧] 미claim cancel 이 claim 이전에 실패해도 abandon 을 부르지 않고(호출 0 — spy) 행·부모 예약이 그대로다. S4 로 이월한 잔류를 '고쳐진 것'으로 오인하지 않게 현행을 고정한다.
- **변이:** hybrid `_require` 를 `_require(True, …)` 로 / `except CommandValidationError` 를 `except Exception` 으로 뭉갬 / abandon 의 SUBMIT 한정 제거 / 경합 코드를 예외 코드와 같게 / 예외 경로의 abandon 제거 / **`claimed=False` 경로의 abandon 제거** / abandon 을 claim 이후 실패에도 부름 / `_recheck_regime` 의 now 를 `facts.decided_at` 으로.
- **위험:** `_owner_ready` 실패 사유에 `startup_reconciliation` 같은 owner 내부 어휘가 그대로 실린다 — 로그 한정(결정 ⑬).

- **통합된 실제 인터페이스(wave 2, `d339cd7`+`9a3fe19`):**
  - hybrid `_require` 는 sector `_require` 옆이 아니라 **`recompose_quantity` 대조 뒤**에 있다(같은 prepare·final 공통 관문 안). 앞에 두면 동결 시험 `test_hybrid_sizing_is_explicitly_refused_before_any_attempt`(신고 on·정책 off)가 `unsupported_hybrid_sizing` 대신 mismatch 를 받아 깨진다. 진리표: 정책 off·신고 off → 통과 / off·on → `unsupported_hybrid_sizing` / on·on → `unsupported_hybrid_sizing` / **on·off → `decision_hybrid_mismatch`**. 통과는 off·off 하나뿐이라 순서와 무관하게 fail-closed 다.
  - claim 이전 실패는 helper `_unsent(request, reason)` 한 곳을 지난다: SUBMIT 일 때만 `abandon_candidate` 호출, False 면 loguru `[실행] 미송신 시도 예약 유지` 경고, abandon 의 `ApplicationBlocked` 는 다시 던져 바깥 `dispatch` 가 `command_admission_closed` 로 만들고, 그 밖의 abandon 예외(저장된 예약 행이 깨진 경우 — 기존 시험이 그 상태를 만든다)는 `logger.exception` 으로 남기되 NOT_SENT 결과는 보존한다.
  - **`claimed=False` 는 "sibling submit 미해결"로는 도달하지 않는다** — 그 조건들은 `_bound`→`_evaluate` 가 먼저 `unresolved_symbol_attempt` 등 예외로 막는다. 실제로 도달하는 `claimed=False` 는 "남의 claim 이 이미 붙었다/상태가 prepared 가 아니다"뿐이고 그때 abandon 은 정의상 False(예약 유지)다. 그래서 그 경로는 대조 시험(예약 유지 + abandon 호출 사실 spy)으로 고정됐다.
  - **schema3 의 "재유도는 현재 now" 는 행동으로 관측되지 않는다(동치 변이).** schema3 게이트는 `classified_at` 과 now 를 둘 다 KST 날짜로만 비교하는데, 날짜가 다른 요청은 `_session` 의 `request_day_mismatch` 가 먼저 막는다. 같은 변이는 schema1 경로의 naive `.date()` 축으로, **dispatch 시점**에 세워 kill 했다. 인수 조건 7 은 "schema3 에서는 구조적으로 성립, 시험으로는 schema1 축에서 고정"으로 닫는다.
  - **계약 귀결(기존 시험 2곳의 기대값이 바뀐 이유):** claim 이전의 **일시적** 차단(in-flight market source, regime source 교체)도 이제 그 attempt 를 끝낸다. 같은 attempt 의 재 dispatch 는 `reservation_changed` 로 NOT_SENT 이고, 재송신은 **같은 intent 의 새 prepare** 로만 열린다(계약 5·6 과 일치). `tests/test_execution_market_source.py`·`tests/test_execution_two_minute_regime_owner.py` 의 "같은 attempt 가 prepared·예약 그대로 남아 다시 보내진다"는 단언이 바로 이 단계가 닫는 누수를 현행으로 고정한 것이어서 새 계약으로 갱신됐다(독립 재현이 줄 단위로 "약화 아님" 확인, coordinator 가 사유 코드 단언을 더해 특정성 회복). S4 의 보호 SELL 설계는 이 귀결(일시 차단 뒤에는 새 attempt)을 전제로 한다.
  - 부수 확인: **미claim prepared 가 하나라도 남으면 일자 전환 자체가 `unresolved_submit` 로 막힌다**(S3-4 worker 실측) — 이 단계 이전의 누수는 예약뿐 아니라 다음 날의 rollover 도 막는 결함이었다. 자식 명령 잔류(S4 이월)가 같은 방식으로 전환을 막는지는 S4 에서 확인한다.

### S3-4 — `day_recovery.py`: 전일 판단 사실 정리 (돈 경로 아님 · 별도 커밋)

- **제품 파일(단일 writer):** `src/execution/safety/day_recovery.py` · **시험:** `tests/test_execution_decision_facts_gc.py` · **의존:** 없음(S3-3 과 병렬 — 결정 ⑩으로 `commands.py` 의존이 없다)
- **고정 인터페이스:** `reset_daily(state, prices, unrealized, to_day)`(175-186) 시그니처 무변. `state['entry_decision_facts']` 를 빈 dict 로 만든다(키가 없으면 만들지 않는다). **`qualification_sources` 는 읽지도 쓰지도 않는다.**
- **RED:**
  - [행동] 당일 facts 3건·출처 4행을 게시한 뒤 일자 전환 → facts 가 그대로 남는다(오늘 통과 = 성장). 수정 후 `entry_decision_facts == {}`, `qualification_sources` 는 **바이트 단위 무변경**(version·as_of·digest 그대로).
  - [행동] 전환 후 같은 출처 이름에 재게시하면 version 이 n+1 로 이어진다(counter 불변 — 정의된 동작이며 KeyError 가 아니다).
  - [행동] 전환 전에 prepare 된 자동 BUY 를 전환 후 dispatch → facts 부재로 정의된 사유(`decision_facts_required` 또는 `decision_facts_changed`)·NOT_SENT·POST 0. **S3-3 이 통합된 뒤에는 예약도 0** 이어야 하나 이 단계는 S3-3 과 병렬이므로 예약 단언은 넣지 않는다(S3-5 의 통합 시험에서 고정).
  - [행동] 전환 전후 `encode_state` 길이가 준다.
  - [대조] `portfolio`·`risk`·`protection`·`lots`·`outbox`·`intents`·`attempts`·`qualification_sources` 무변경.
- **변이:** facts 비우기 제거 / `qualification_sources` 까지 비움 / 키 부재 state 에서 KeyError.
- **위험:** facts 를 지우면 저장소에 흔적이 없다(사실 15) — 감사 흔적은 S3-5 의 게시 로그가 맡는다. 이 단계를 "감사 보존"으로 보고하지 않는다.

- **통합된 실제 인터페이스(wave 2, `968a097`+`9a3fe19`):** `reset_daily` 에 `if "entry_decision_facts" in state: state["entry_decision_facts"] = {}` 한 곳. 명세의 RED 두 건은 그대로 세울 수 없었다 — ① "전환 전 prepare → 전환 후 dispatch" 는 도달 불가(prepared attempt 가 있으면 전환이 `unresolved_submit` 로 시작되지 않고, 전환 뒤 전일 요청은 `request_day_mismatch` 가 먼저 막는다) → facts 부재는 **전환 후 같은 intent 재 prepare → `decision_facts_required`**(수정 전에는 `decision_facts_expired`)로 고정. ② "encode_state 길이가 준다" 는 전환 전체 구간으로는 거짓(전환이 `day_transition`·`day_valuations`·`day_valuation_view`·`recovery_receipts` 를 더한다) → 측정 구간을 `rollover_day` 직전/직후로 좁혔다. 무변경 대조는 손열거가 아니라 **"전환 직전의 전 뿌리 − 전환 소관 뿌리"** 로 유도하고 뿌리 키 집합 자체도 단언한다(이 writer 에 뿌리 삭제가 하나 더 끼어들면 자동으로 잡힌다). 거래가 있었던 날(터미널 attempt·intent 가 남은 state)의 전환도 고정.

### S3-5 — `gateway.py`(신규) + `runtime.py` 설치점

- **제품 파일(단일 writer):** `src/execution/safety/gateway.py`, `src/execution/safety/runtime.py`(`gateway` 속성·`install_gateway` 만) · **시험:** `tests/test_execution_signal_gateway.py` · **의존:** S3-3, S3-4
- **고정 인터페이스:**
  - `class SignalGateway.__init__(self, runtime, commands, *, exit_manager, config_version)` — `_require(commands.runtime is runtime)`, `_require(exit_manager is runtime.exit_manager)`(인수 조건 10). engine 을 import 하지 않는다.
  - `runtime.gateway`(초기 None) · `runtime.install_gateway(gateway)` — 이미 설치돼 있거나 `gateway.runtime is not self` 면 거부.
  - `async def submit(self, event, order, evidence) -> CommandResult | None` — 결정 ⑫의 순서. 자동 BUY 에서 `evidence is None` 이면 게시 0·prepare 0·None. SELL 은 evidence 없이 prepare→dispatch. `intent_id`/`attempt_id` 발급은 계약 4. dispatch 를 `create_task` 로 감싸지 않는다(사실 12). 같은 attempt 재 dispatch 금지(계약 6).
  - 동기 읽기 helper: `reserved_cash() -> Decimal`(owner 의 미해결 attempt 예약 현금 합 — `risk_policy.py:713-716` 의 식 재사용) · `pending_strategy_notional(strategy) -> Decimal`(side=='buy'·전략별 — `decisions.py:280-285` 의 식 재사용). 식을 새로 쓰지 말고 기존 함수를 호출하거나 공용 helper 로 뽑는다.
  - facts 게시 시 로그 1줄: intent_id·symbol·strategy·facts digest·config_version·expires_at(금액·계좌 없음).
- **RED:**
  - [부재 RED — 표시만] 모듈·클래스 부재.
  - [행동] runtime 과 다른 ExitManager 로 생성 → 거부. 대조: 판단~final 사이 `exit_manager._intraday_crash_level` 만 바꿔 `stop_crash_capped` 를 뒤집으면 `decision_stop_changed`·NOT_SENT·POST 0·예약 0.
  - [행동] 자동 BUY + `evidence is None` → 게시 0·attempt 0·POST 0·None. 대조: 보유 종목 LIMIT SELL·MARKET SELL 은 evidence 없이 POST 1.
  - [행동] `observe_entry_quote` 를 생략한 BUY → `current_entry_quote_required`·POST 0. facts 게시를 건너뛴 자동 BUY → `decision_facts_required`·attempt 미생성.
  - [행동] `_regime_owner` 미설치 + attach 에서 regime 을 인용한 facts → `stale_regime_decision`(legacy `_horizons` 폴백이 owner 를 통과시키지 못한다).
  - [행동] network await 경계(`connect`/`hashkey`)에서 stale 6축(현금 감소 · 다른 attempt 예약 출현 · 일손실 축소 · 소비 출처 version 변화 · config_version 변화 · 만료) → 각각 NOT_SENT·POST 0·`final_rejected`·예약 0. 대조: 무관 종목 fill·무관 취소 ACK·소비하지 않은 출처의 version 변화·같은 문자열 regime 재커밋은 POST 1.
  - [행동·쌍] 합성 허가 없이 같은 시나리오 → NOT_SENT·POST 0·**예약 0**(S3-1·S3-3 통합 효과) · 일자 전환 뒤 dispatch 도 예약 0(S3-4 와의 통합).
  - [행동] 부분 게시: `publish_decision_facts` 만 실패시키면 출처 version 은 올라 있고 facts 0건·POST 0 이며 gateway 가 성공으로 반환하지 않는다. **대조: 같은 요청을 다시 submit 하면 출처 version n+1 로 정상 진행해 POST 1.**
  - [행동] 재시도 식별자: 첫 attempt 가 NOT_SENT 로 끝난 뒤 같은 종목·side·전략의 submit 은 같은 `intent_id`·새 `attempt_id` 이고 목표 수량을 넘지 않는다.
  - [행동] `reserved_cash()`·`pending_strategy_notional()` 이 open(ACK 됨) BUY 의 예약을 포함하고 터미널 attempt·SELL 은 제외한다.
- **변이:** ExitManager 동일성 `_require` 제거 / BUY 의 `evidence is None` 가드 제거 / 게시 순서 뒤집기 / dispatch 를 `create_task` 로 감쌈 / `intent_id` 를 매번 새로 발급 / facts 게시 실패를 삼키고 성공 반환 / SELL 에도 evidence 를 요구 / `reserved_cash()` 가 터미널 attempt 를 포함.
- **위험:** 이 단계의 시험은 on_signal 결과(Order+evidence)를 S2 의 `manager`/`capture` helper 로 만든다 — 실제 큐와의 접합은 S3-6a 가 증명한다. 여기서 "실큐 인수"를 주장하지 않는다.

- **통합된 실제 인터페이스(wave 3, `737da95`+`4356d43`) — S3-6a·S3-6b 가 의존하는 계약:**
  - `SignalGateway(runtime, commands, *, exit_manager, config_version)` · `runtime.gateway`(초기 None) · `runtime.install_gateway(gateway)`(이미 설치·`gateway.runtime is not self` 면 `ApplicationBlocked`). 생성만으로는 설치가 아니다.
  - `async submit(event, order, evidence) -> CommandResult | None` — **None 은 "자동 BUY 인데 evidence 가 None" 일 때뿐**(게시 0·attempt 0·POST 0, 성공 아님). `CommandResult` 는 dispatch 까지 간 경우뿐이고 NOT_SENT 도 결과다. **게시~prepare 의 실패는 삼키지 않고 그대로 raise**(`CommandValidationError`·`QualificationRefused`·`RequestValidationError`·`ApplicationBlocked`·저장 장애) → S3-6a 의 H1 호출부가 결정 ②의 try/except 로 흡수한다.
  - **결정 ⑫의 순서 정정:** gateway 는 **정책 맥락을 게시하지 않는다**(읽기만). 1차 구현은 게시본의 config 축을 주입값으로 다시 찍어 재게시했는데, 그러면 owner 의 `stale_decision_config_version` 이 한 값을 자기 자신과 비교하게 된다(독립 재현의 변이 생존). 지금은 `entry_policy_context` 가 없으면 `entry_policy_context_required`, **BUY 는 판단 사실을 게시하기 전에** 게시본 config ≠ 주입 `config_version` 이면 `stale_decision_config_version` 으로 거부(owner.version 불변). SELL 은 사이징 설정과 무관하므로 config 로 막지 않는다. 실제 순서: (BUY) `observe_entry_quote`(종목이 `market_sources` 에 묶여 있으면 건너뜀) → `publish_qualification` → 감사 로그 → `prepare` → `dispatch`.
  - 전송 객체는 `GuardedKISTransport(runtime.engine.broker, request_builder=commands.builder)` 를 **예약이 생기기 전에** 만든다. `engine.broker` 가 None 이어도 fail-closed 다 — NOT_SENT/`preparation_failed`·`final_rejected`·예약 0(시험으로 고정). 생성자에 transport 인자가 없으므로 S3-6a 는 attach 시 `engine.broker` 를 그대로 쓴다.
  - 평가 가격은 `order.price`(LIMIT) 또는 `event.price`(MARKET). intent 키는 `(symbol, side, strategy)` — 프로세스 로컬 dict 라 재시작하면 새 intent 를 받는다(10A3 startup 대사 범위).
  - 읽기 helper `reserved_cash()`·`pending_strategy_notional(strategy)` 는 **동기 메서드**이고 `commands._snapshot(state).pending` 하나만 본다. snapshot 을 만들 수 없으면(정책 맥락 부재 → KeyError, regime 출처 미current → `CommandValidationError`) **0 을 돌려주지 않고 예외를 낸다.** 합산식 두 줄은 `risk_policy.py:716`·`decisions.py:283-284` 와 같은 식을 다시 적은 것이다(공용 함수로 뽑으려면 B1·S1 파일을 건드려야 해 수용) — 갈라짐은 S3-6b 의 parity RED 가 행동으로 잡는다.
  - `async recover_unsent() -> list[str]`(기동 전용 sweep, `aee69e3`): `kind=='submit'`·`state=='prepared'` 행마다 `abandon_candidate(reason='startup_unclaimed')`. 엔진 실행 중에는 `gateway_recover_requires_stopped_engine`. 읽기 helper 는 먼저 `commands._owner_ready(state)` 를 본다 — 저장과 게시가 어긋난 owner 의 낡은 메모리 state 를 "예약 0" 으로 읽지 않는다.
  - **entry quote 의 출처는 SIGNAL 자체다**(`source='signal'`, `event_id=event.id`) — 독립 시장 관측이 아니다. owner 의 `current_entry_quote_required` 는 "요청 가격과 같은 값이 당일 게시돼 있다"만 보므로 이 경로의 진입 가격 provenance 는 증명되지 않는다. 모든 BUY 가 실제 market source 를 갖게 하는 것은 10A2/10A3 범위.
- **wave 3 독립 재현이 찾은 P1(→ `commands.py`, RED `83e187a`·수정 `b83b6e7`):** `_dispatch` 가 `_request`(세션 재검사 포함)를 try **바깥**에서 불러, prepare 와 dispatch 사이에 세션 경계(장 마감 15:20·15:40, session_guard 거부)가 닫히면 `CommandValidationError` 가 `dispatch()` 를 탈출하고 예약 4항·pending sector 가 남았다(재현: `market_closed`·`prepared`·예약 101,500). gateway 는 prepare 로 예약을 만든 **뒤** dispatch 를 부르므로 이 창은 실재한다. 수정: `_dispatch` 는 `_request(..., session=False)` 로 신원·권한만 try 밖에서 검증(위반은 호출자 결함이라 계속 raise — 위조 요청이 남의 attempt 를 끝내는 길은 열지 않는다)하고, 세션은 `_bound`→`_evaluate` 의 기존 재검사에서 걸려 `_unsent`(abandon)로 끝난다. prepare 경로의 세션 검사는 그대로.

### S3-6a — `engine.py` 경로 배선 (H1·H4·H5·`_pending_sector_map`)

- **제품 파일(단일 writer):** `src/core/engine.py` · **시험:** `tests/test_execution_signal_gateway_wiring.py` · **의존:** S3-5
- **허용 hunk(이 밖의 변경 0 — coordinator 가 hunk 위치까지 확인):**
  - **H1** `_process_event` 527-533: 거부 튜플에서 SIGNAL 만 분리. `runtime.gateway is None` 이면 현행 폐기. 아니면 try 안에서 `result = await self.risk_manager.on_signal(event)` → **바로 다음 줄에서**(await 없이) `evidence = self.risk_manager._last_qualification_evidence` → OrderEvent 가 있으면 `await gateway.submit(event, order, evidence)` → finally 에서 `self._pending_sector_map.pop(event.symbol, None)`(결정 ⑮). except 는 결정 ②(`errors_count += 1` + ErrorEvent, 루프 계속). 반환 OrderEvent 를 `emit_many` 로 큐에 넣지 않는다. FILL·ORDER 거부와 그 `errors_count` 증가는 불변.
  - **H4** on_signal 종착부 2381-2408: attach 에서 결정 ④의 7개 dict 를 쓰지 않는다. `_last_signal_time[order.symbol] = now` 는 **유지**.
  - **H5** eviction 호출부 2335-2345: attach 에서 `_try_evict_weakest_position` 을 부르지 않는다(결정 ⑤). `_try_evict_weakest_position` 본문은 불변.
- **RED (네 시계를 같은 순간으로 맞춘 실큐 — 사실 17·18):**
  - [행동·핵심] attach + gateway 설치 상태에서 **실제 큐**에 BUY SIGNAL 1건 → `_get_next_event()`→`_process_event()` → POST 1·ACKNOWLEDGED·attempt 생성(양성 단언)·`stats.errors_count` 불변. 오늘은 POST 0·errors_count +1.
  - [행동] 보유 종목 LIMIT SELL·MARKET SELL SIGNAL 도 POST 1.
  - [대조·회귀] 같은 상태에서 FILL·ORDER 이벤트는 여전히 거부·`errors_count` +1·legacy handler 미호출. gateway 미설치 attach 에서는 SIGNAL 도 현행대로 폐기.
  - [행동·②] gateway 가 예외를 던지는 SIGNAL 1건 뒤에도 `engine.running` 이 True 이고 다음 이벤트가 처리된다(`errors_count` +1, `_shutdown` 미호출).
  - [행동·④] 첫 BUY SIGNAL 이 POST 1 → 10초 뒤 같은 종목 SIGNAL → 게시 0·prepare 0·POST 0(쿨다운). 그리고 첫 송신 뒤 7개 legacy dict 가 전부 비어 있다(이중 장부 0).
  - [행동·⑤] 만석 + 고득점 BUY → 원 BUY 거부·예약 0 이고 eviction SELL 의 큐 emit 0·게시 0·POST 0.
  - [행동·⑮] 송신 성공(ACK) 뒤 `_pending_sector_map` 이 비어 있고, 그 종목의 포지션이 빠진 뒤 같은 섹터 신규 BUY 가 legacy 섹터 집계에 막히지 않는다. gateway 거부 뒤에도 비어 있다.
  - [행동·계약 3] on_signal 이 `_risk_validator`/`can_open_position` 거부로 None 을 돌려준 요청은 속성에 증거가 남아 있어도 게시 0·prepare 0·POST 0.
  - [행동] 인계점 이후 SignalEvent 불변(`event.score`·`metadata['position_multiplier']`).
  - [행동·S4 경계] attach 에서 90초가 지난 미체결 SELL·10분이 지난 미체결 BUY 가 있어도 진입부 stale 루프가 `broker.submit_order`/`cancel_all_for_symbol`/`clear_pending` 을 부르지 않는다(POST 0·취소 0). UNKNOWN 은 예약 유지·재 dispatch 없음.
  - [대조·ready=False] 합성 허가 없이 같은 SIGNAL → NOT_SENT·POST 0·예약 0·`_pending_sector_map` 잔류 0.
  - [대조·legacy 불변] runtime 미attach 엔진에 같은 SIGNAL → 기존과 동일한 Order(수량·가격·전략), `_pending_orders`·`_reserved_by_order`·`_pending_strategy`·`_pending_signal_cache` 가 종전대로 채워지고 ORDER 이벤트가 큐에 실리며 gateway/publish/prepare/dispatch 호출 0(spy)·`errors_count` 불변.
- **변이:** H1 되돌리기(다시 폐기) / 반환 OrderEvent 를 `emit_many` 로 큐에 넣음 / 거부 튜플에서 FILL·ORDER 까지 뺌 / **H1 의 try/except 제거** / 증거를 지역값이 아니라 submit 시점의 속성 재독으로 / on_signal 이 None 인데 gateway 호출 / H4 의 attach 가드 제거(legacy dict 기록) / **H4 에서 `_last_signal_time` 기록까지 제거** / **H5 제거** / **성공 경로의 `_pending_sector_map.pop` 제거** / H1·H4·H5 의 attach 가드를 "항상 참"으로(legacy 대조 시험이 죽어야 한다).
- **위험:** 527-533 의 SIGNAL 분기에는 기존 회귀 안전망이 없다(사실 1) — 양성 단언과 대조를 같은 파일에 쌍으로 둔다.

- **통합된 실제 인터페이스(wave 4, `0b5ac18`+coordinator 시험 보강):** H1 은 두 조각이다 — `_process_event` 의 거부 분기 **직전**에 `if runtime is not None and event.type == SIGNAL and runtime.gateway is not None: await self._submit_signal(event); return`(6줄 가산), 그리고 새 메서드 `UnifiedEngine._submit_signal(event)`: `on_signal` await → **바로 다음 줄에서** `_last_qualification_evidence` 를 지역값으로 → `result[0].order` 가 있을 때만 `gateway.submit` → 결과는 `status/reason_code` 만 로그. `CancelledError` 는 재던짐, 그 밖의 예외는 핸들러 루프와 같은 형태(`errors_count += 1`·`logger.exception`·`ErrorEvent(source='on_signal', recoverable=True)`)로 흡수, `finally` 에서 `_pending_sector_map.pop`. H4 는 `_pending_lock` 블록 안 중복 재검사 직후의 **조기 반환**(`_last_signal_time` 기록 + 같은 `[OrderEvent…]` 반환 — 그 뒤에 건너뛰는 다른 동작 없음을 coordinator 가 끝까지 읽어 확인). H5 는 기존 `if` 에 조건 한 줄(`… and getattr(self.engine, '_execution_runtime', None) is None`) — **engine.py 에서 기존 줄이 바뀐 유일한 곳**이다(numstat 52 추가 / 1 삭제). 실제 사이징 수량은 47주(CV 메모리 보정 −3 뒤)라 시험 상수도 47 이다.
  - 이 단계의 실큐 시험은 재사용한 `_order_env` 의 스텁(legacy 세션·`engine.can_open_position`·`_risk_validator`·`_sector_lookup`) 위에 있다 — **실제 게이트 통과 경로와 실제 `KRSession` 표는 S3-6b 가 처음 태운다.** UNKNOWN 의 예약 유지·재 dispatch 없음은 S3-5 의 계약으로만 고정돼 있고 실큐 접합 쪽 대조는 없다.

### S3-6b — `engine.py` 정합 배선 (H2·H3·H6) + 게이트 순서

- **제품 파일(단일 writer):** `src/core/engine.py` · **시험:** `tests/test_execution_signal_gateway_parity.py` · **의존:** S3-6a
- **허용 hunk:**
  - **H2** `_reserved_cash` property(1468-1471)와 `_pending_strategy_notional`(2426-2432): attach + gateway 설치 시 각각 `gateway.reserved_cash()`·`gateway.pending_strategy_notional(strategy_name)` 를 돌려준다. **`_reserved_cash` 는 `@property` 로 남긴다**(소비 지점 다섯 곳이 괄호 없이 산술식에 쓴다 — gateway 쪽은 메서드이므로 property 안에서 호출). gateway helper 는 fail-closed 로 **예외를 낸다** — 지금까지 예외를 내지 않던 property 가 예외원이 되므로, on_signal 안에서 난 예외가 H1 의 try/except 로 올라가 그 SIGNAL 이 명시 거부(`errors_count` +1)로 끝남을 RED 로 세우고, on_signal **밖**에서 `_reserved_cash`/`_pending_strategy_notional` 을 읽는 곳(`grep`)이 attach 모드에서 어떻게 되는지 보고한다. **core_reserve 기준:** owner 의 `evaluate_entry_policy` 는 non-core 에서 `core_reserve(snapshot)` 를 예약 합에 더하는데(risk_policy.py:717-718) gateway 의 `reserved_cash()` 는 더하지 않는다 — engine 은 `_get_core_reserve()` 를 따로 뺀다(engine.py:2713 부근 주석). 이중 차감·누락이 없는지 parity RED 로 확인한다. `_calculate_position_size` 본문·소비 지점 5곳은 불변.
  - **H6(wave 4 독립 재현이 찾은 전제 — 추가)** `bind_execution_runtime`(293-301): attach 의 폐쇄성은 "legacy 장부가 비어 있다"는 전제 위에 있다. 같은 프로세스가 legacy 로 신호를 처리해 `_pending_timestamps` 에 행이 남은 채 runtime 을 bind 하면, 다음 on_signal 진입부의 90초 stale SELL 루프가 owner 를 거치지 않고 `broker.submit_order` 를 직접 부른다(계약 1 위반). 그래서 bind 시 `risk_manager` 의 legacy 장부(`_pending_orders`·`_pending_timestamps`·`_reserved_by_order`)가 **비어 있지 않으면 RuntimeError 로 거부**한다(기존의 running·큐 검사와 같은 자리·같은 방식, `risk_manager` 가 아직 없으면 통과). RED: 장부에 행을 심고 attach → RuntimeError·`_execution_runtime` 은 None 그대로. 변이: 그 검사 제거.
  - **H3** sector 조회 2302-2307: attach 에서 조회가 **예외**로 끝나면 BUY 를 거부한다(`_log_sig` 의 block_gate 는 기존 어휘 규칙을 따른다). 정상적으로 None 을 돌려준 경우와 legacy 경로의 None 뭉갬은 불변.
- **RED:**
  - [행동·⑥] 같은 전략·다른 종목 BUY 두 건(A 는 ACK 후 open — 루프가 직렬이라 "동시"는 이 형태로만 재현된다) → 오늘은 B 의 legacy 수량이 owner `strategy_remaining` 을 넘어 `decision_quantity_unjustified`·POST 0. 수정 후 B 의 수량이 owner 재유도값 이하로 산출돼 POST 1.
  - [행동·⑥] owner pending 예약이 가용 현금을 넘으면 G5_cash(2169)에서 차단 — 게시 0·prepare 0·POST 0(`block_gate='G5_cash'`). 전략 예산 조기 차단(2193)도 owner 예약 기준으로 발화.
  - [행동·⑭] `_sector_lookup` 이 예외를 던진 BUY → 오늘은 sector None 으로 섹터 한도가 꺼진 채 통과. 수정 후 거부·POST 0. 대조: lookup 이 정상 None → 기존대로.
  - [행동·계약 2] `is_trading_hours` 류 스텁을 **걷어내고** `src.utils.session.datetime` 과 engine 모듈 `datetime` 을 함께 얼려 실제 `KRSession` 표를 태운다 — legacy CLOSED·owner 허용 구간(08:50-09:00, 15:20-15:40)의 LIMIT SIGNAL 이 owner 로 넘어가기 전에 막힌다(게시 0·POST 0).
  - [대조·legacy 불변] runtime 미attach 에서 `_reserved_cash`·`_pending_strategy_notional` 이 종전 값을 돌려주고 S2-2 사이징 특성화 표본의 수량이 같다. sector 예외도 종전대로 None.
  - [대조·ready=False] 합성 허가 없이 위 시나리오 → POST 0·예약 0.
- **변이:** H6 의 legacy 장부 검사 제거 / `_reserved_cash` 의 attach 분기 제거 / **`_pending_strategy_notional` 의 attach 분기 제거** / 두 분기의 attach 가드를 "항상 참"으로 / H3 의 예외 구분 제거 / H3 가 정상 None 까지 거부.
- **위험:** property 변경은 다섯 소비 지점을 함께 움직인다(사실 8) — 다섯 곳 각각에 도달하는 RED 또는 대조가 있어야 한다.

- **통합된 실제 인터페이스(wave 5, `6e5c621`+coordinator 시험 보강) — `engine.py` 37줄 가산 / 0 삭제:** 모듈 함수 `_attached_gateway(engine)`(runtime 있고 gateway 설치됨일 때만 gateway, 아니면 None)이 H2a·H2b·H3 의 **단일 가드**다. `_reserved_cash`(property 유지)와 `_pending_strategy_notional` 은 첫머리에서 gateway helper 를 그대로 돌려주고(예외 무흡수), H3 는 sector 조회 `except` 블록 안에서 attach 일 때만 `block_gate='G3_sector'` 로 거부, H6 은 `bind_execution_runtime` 의 running·큐 검사 직후에 legacy 장부 3종을 본다.
  - **core_reserve 기준 — 이중 차감·누락 없음:** owner 는 `pending 합 + core_reserve(snapshot)`(non-core), `gateway.reserved_cash()` 는 pending 합만, engine 은 소비 지점에서 `_get_core_reserve()` 를 따로 더한다 → 합계 기준이 같다(시험: `can_open_position` 이 받은 `reserved_cash` == 477,050 + 600,000). **단 두 core 식의 입력 출처가 다르다** — owner 는 `snapshot.policy.core_allocation_pct`, engine 은 `config.strategy_allocation['core_holding']`. 설정 축이 갈라지면 두 값이 달라질 수 있고, 그것을 하나로 묶는 것은 `config_version` 5축 조립(10A3)이다.
  - 표본: 자본 2,000,000·가용 1,900,000·sepa_trend 배분 40%(800,000) → A 47주 예약 477,050 → 전략 잔여 322,950 → **B 30주**로 줄어 POST 1(수정 전에는 B 가 `decision_quantity_unjustified`·POST 0).
  - **on_signal 밖의 소비자 1건:** `src/dashboard/data_collector.py:1572` 의 `float(getattr(rm, '_reserved_cash', 0))` — attach+gateway 에서 property 가 내는 `CommandValidationError`/`KeyError` 는 `getattr` 기본값이 덮지 못해 그 디버그 통계 응답이 실패한다(돈 경로 아님·허용 파일 밖 → **10A3/10C 이월**).
  - 이 단계가 걷어낸 스텁은 legacy 세션·`engine.can_open_position`·`_sector_lookup`·`_pending_strategy_notional` 넷이다. **`_risk_validator`(None)·`_check_factor_budget`(항상 None)은 여전히 스텁** — 실제 `risk/manager.py` 게이트와 팩터 버킷 게이트는 attach 경로 시험에서 아직 한 줄도 돌지 않는다(S5 인수의 범위). 세션 차단 표본(08:55·15:30)은 legacy 세션·engine 시계 두 축만 옮긴다.

- **Codex 3차 처분(`10d2ca7`) — H7·정리 구문:** H6 은 attach **시점**만 본다(그 뒤 연결된 RiskManager 의 잔류는 못 본다) → **H7:** on_signal 의 stale 루프 직전에서 attach 이고 legacy 장부 3종 중 하나라도 비어 있지 않으면 RuntimeError(그 SIGNAL 만 거부, 브로커 직접 호출 0). `_submit_signal` 의 `finally` 는 `getattr(event, "symbol", None)` 로 symbol 없는 SIGNAL 이벤트도 받는다(정리가 다시 던지면 흡수한 예외를 덮고 루프 밖으로 샌다). engine.py 누적 **98 추가 / 1 삭제**.

## 5. legacy·US 불변 증명 (네 겹)

1. **구조(diff 검사):** engine.py 의 허용 hunk(H1~H5) 각각에서 추가된 실행 줄이 전부 `_execution_runtime is not None`(및 `gateway is not None`) 가드 안이거나 순수 가산임을 coordinator 가 줄 단위로 확인한다(S2-5 가 `engine.py` +39/−0 을 같은 방식으로 통과).
2. **기준선 불변:** `git diff --numstat <base> <head> -- tests/` 에서 기존 시험 파일 삭제 0줄. 동결: `test_t11_money_path_baseline`·`test_t11_evidence_baseline`·`test_t11_judgment_baseline`·`test_execution_sizing_characterization`·`test_cross_validator_characterization`·`test_risk_sizing`·`test_exit_manager_characterization`·`test_sync_portfolio_characterization`·`test_toss_money_path_invariance`·공용 하네스 `test_execution_runtime`(import 만)·on_signal 소비 5파일(`test_t11_acceptance`·`test_t11_entry_plan`·`test_entry_risk_lifecycle`·`test_t10_repro_a`·`test_t10_regime_path`). 예외는 결정 ⑰의 helper 2줄뿐.
3. **행동 대조:** S3-6a·S3-6b 의 "legacy 불변" 쌍 시험.
4. **변이 kill:** 각 hunk 의 attach 가드를 "항상 참"으로 바꾸는 변이에서 3의 대조 시험이 ≥1건 실패해야 한다.

**US:** `_USEngineBundle`(run_trader.py:1495)은 KR `UnifiedEngine`·KR `RiskManager` 와 별도 인스턴스다. `src/`·`scripts/` 에서 `KRExecutionRuntime` 생성·`.attach()`·`install_gateway` 호출이 0건임을 단계마다 grep 으로 재확인한다. US 시험 파일 수정 0줄·전건 통과.

## 6. S3 에서 하지 않는 것

- **S4:** 미claim **자식 명령(cancel/modify)** 의 종료 간선(결정 ⑧ — 부모 `order_ref` 를 싣는 행은 `abandon_candidate` 로 끝낼 수 없다). 90초 MARKET SELL 폴백(1899-1982)·취소0건 예약 해제(1985-2015)·eviction(1586-1696·2335-2345)의 owner 경로 이관. S3 는 attach 에서 셋이 **발화하지 않음**만 고정한다. eviction 권한 표식을 `metadata['source']='replacement'` 문자열에서 `EntryAuthority` 발급 context 로 대체. SELL 지정가(`broker.get_best_bid` await, 2264)를 final 에서 다시 볼지 facts 로 굳힐지.
- **S5:** 독립 실큐 인수·최종 broad 리뷰·전체 직렬. 결정 ③(ORDER 이벤트를 큐에 싣지 않음)을 상위 계획과의 차이로 확인받는다.
- **10A3(factory):** `config_version` 5축 조립·제품 PolicyContext publisher · `KRExecutionRuntime` 생성·`attach()`·`install_gateway()` 와 설치 순서(regime owner 가 command owner 보다 앞) · `trading_ready` 를 True 로 만드는 실제 startup 대사 · **재시작 시 미claim prepared 의 자동 정리 — 부품은 S3 에 있다: factory 가 `runtime.restore()` 뒤·엔진 루프 시작 전에 `await runtime.gateway.recover_unsent()` 를 불러야 한다**(Codex 2차 P1 의 처분. 취소·종료·crash 가 prepare 와 claim 사이에 남긴 행은 같은 호출 안에서 정리할 수 없고, 남으면 예약과 일자 전환을 막는다. 엔진이 도는 중에는 거부된다) · attach 모드의 체결 메타(entry_tags·전략) 인계와 pending 교착 감시(결정 ④) · `dashboard/data_collector.py:1572` 의 디버그 통계가 attach 에서 예외로 끝나는 것(attach 인지형으로) · engine 의 `_get_core_reserve()` 와 owner 의 `core_reserve(snapshot)` 가 서로 다른 설정 출처를 읽는 것.
- **10C:** `run_trader.py:1846-1848` 의 sector lookup 예외 뭉갬 · KOFR(`kr_scheduler.py:6754·6818`) · 수동 매수(7597) · CLI 2개 · `kr_scheduler.py:978-1004` · 판단 사실의 분석 원장 projection.
- **성능:** `ExecutionStateStore.commit` 이 매 commit 마다 state 전체를 직렬화한다(store.py:212-217). S3-4 가 facts 누적을 끊지만 commit 당 비용의 측정·개선은 미착수. `ponytail:` 출처 행이 수천을 넘기면 counter 분리 후 행 삭제.

## 7. 위험·미확인

- 조사·설계·심사는 전부 같은 provider(요청 Opus)이고 관측 모델은 미검증이다. 교차 provider 확인은 wave 경계의 Codex 포그라운드 리뷰(좁힌 프롬프트·10분 상한)로 받는다 — S2 마감 수정 diff(`fcc2a27..85a65bc`)도 첫 Codex 리뷰 범위에 넣는다.
- 결정 ③은 상위 계획 문구와 다르다. 목적은 같다고 판단했으나 S5 에서 확인받기 전까지 "계획과 다른 해석"으로 표기한다.
- 결정 ④로 attach 모드에서는 legacy 중복 주문 재검사(2376-2379)가 항상 비어 통과한다 — owner 의 `unresolved_symbol_attempt` 가 실제로 같은 상황을 막는지는 S3-6a 의 쿨다운 밖(30초 뒤) 재신호 시험으로 확인한다(RED 목록에 worker 가 1건 추가).
- gateway dispatch 의 network await 동안 엔진 루프가 멈춘다(EXECUTION_FILL 처리 포함). legacy 와 같은 성질이지만 owner 경로는 connect·hashkey·POST 세 번의 await 가 있다 — 지연 상한은 측정하지 않았다.
- `_owner_ready` 가 합성 허가 없이 항상 실패하므로, 제품이 설치되기 전까지 "정상 송신" 표본은 시험 안에만 있다. 현금 고갈(펩트론 99.6%)로 정상 BUY 실경로 표본도 0건이다.

## 8. 역할·모델·한도 (정책 `ai-routing-v1-2026-09-20`)

| 역할 | 요청 모델/effort | 한도 |
|---|---|---|
| coordinator | 이 세션 — Plan·diff 확인·변이 재적용·통합·문서·전체 suite | — |
| S3-1~S3-6b 구현 | claude-opus-5 / high (돈 경로·상태 무결성) | 단계당 60분 · 새 시험 ≤25건 · 허용 파일만 |
| wave 별 독립 재현 | claude-opus-5 / xhigh, 구현 worker 와 다른 실행 — 지정 변이 전건 + 자체 변이 ≥3 | wave 당 1회 |
| 교차 provider 리뷰 | Codex gpt-6-astra / xhigh, companion 포그라운드(`--background` 금지)·read-only·10분 상한 | wave 2·3·5 뒤 각 1회 + 같은 요청 재시도 1회 |

- active worker ≤3(전 provider 합산)이나 pytest 를 도는 worker 는 동시 ≤2. 전체 suite 는 어떤 에이전트도 없이 단독 직렬(UTC→KST).
- worker 금지: 운영 `.env`·토큰·`~/.cache/ai_trader*`·네트워크·systemctl·git push·engine 브랜치 직접 수정·허용 목록 밖 파일·다른 에이전트 호출.
- 실제 모델 metadata 가 노출되지 않으면 "미검증"으로 기록한다.
