# S4 (B3b) — attach 모드에서 되살릴 수 있는 것만 owner 경로로 (세부 계획)

> 2026-09-21 · 기준 `feature/engine-safety-design-20260917` `95029fe`(제품 HEAD `10d2ca7`) · 상위 계획 `2026-09-20-b2b3-request-bound-qualification.md` §2 의 S4 ·
> 출발점은 단계 원장 `docs/reviews/b2b3-stage-ledger-2026-09-20.md` 의 "S4 진입 조건" 7항. 진행·증거·체크리스트는 원장의 S4 절.
> 상태 표기는 **계획**이다. 운영 경계 불변: main 병합·배포·재시작·주문·설정·Toss grant 변경 없음, 제품의 `trading_ready=False`·MODIFY 미지원 유지. S3 와 마찬가지로 **S4 도 설치가 아니다**(제품 attach 호출자 0건).
> 줄 번호는 `10d2ca7` 기준이며 worker 는 착수 시 다시 확인한다.

## 0. 이 단계가 끝나면 말할 수 있는 것 / 없는 것

- **S4 는 "이관"이 아니라 부분 복원이다.** 상위 계획은 직접 SELL 폴백·취소0건 예약 해제·eviction 셋을 owner 경로로 옮긴다고 적었지만, 셋 중 둘은 **취소 최종성**에 의존하고 그 증거가 제품에 없다(§1 사실 1~3). 인계 문서의 지시("공식 startup·취소 증거 … 미완이면 차단을 유지")대로, 증거 없는 것은 만들지 않고 **attach 모드 미지원으로 명시**한다.
- **있는 것(끝나면):** legacy 세 경로의 현재 동작이 특성화로 고정된다(오늘 0건) · 미claim 자식 명령이 종료될 수 있다(S3 가 넘긴 잔여) · 만석 시 교체(eviction)가 attach 에서 owner 의 SELL 한 길로 나간다(exit_exempt·승자·코어 보호와 600초 1건 상한 포함).
- **없는 것:** attach 모드에는 계속 **미체결 SELL 의 시장가 에스컬레이션이 없고 미체결 BUY 의 타임아웃 취소도 없다.** 즉 보호 SELL 에 관해 attach 모드는 legacy 보다 **여전히 덜 안전하다** — 지정가 SELL 이 붙지 않으면 그대로 남는다. 이것을 닫는 것은 S4 가 아니라 취소·체결 최종성의 증거 계약(10A2/10C, 공식 KIS 증거)이다. 그 전에는 attach 를 운영에 설치할 수 없다.

## 1. 조사로 확정한 사실 (읽기 전용 3관점 + 설계 + 적대적 심사, coordinator 대조)

요청 모델은 전부 claude-opus-5(조사·설계 high, 심사 xhigh), 관측 모델은 metadata 미노출이라 **미검증**이다.

1. **취소 최종성은 제품에서 도달 불가능하다.** 증거 파서 `parse_order_evidence` 는 취소 자식이 붙은 체인을 `supported_finality=False` 로 못박고 FINAL_FILLED 만 지원한다(`src/execution/safety/evidence.py:146-151`). 고정 출처 문서도 "취소·정정 체인의 최종성 승격은 별도 검증 항목"이라고 적는다(`docs/integrations/kis-execution-contract-2026-09-17.md:32,56`). `lifecycle.reconcile` 의 제품 호출자는 0건, `EXECUTION_FILL` 의 제품 생산자도 0건이다.
2. **그래서 "취소 → 같은 종목 재주문"은 owner 위에서 성립하지 않는다.** 부모가 터미널이 되지 못하면 `_replacement` 는 0 이고(`lifecycle.py:272-274`) `unresolved_symbol_attempt`(`commands.py:354-355`)가 새 SUBMIT 을 막는다. 취소를 한 번이라도 보내면 그 체인은 오늘의 파서로는 영구히 최종화할 수 없다.
3. **claim 이후 취소의 transport 실패는 자식을 영구 잔류시킨다.** `_dispatch` 는 claim 을 먼저 하고 송신한다. 송신 단계 예외는 UNKNOWN 으로 기록되고 자식(kind≠submit)은 무조건 `RECONCILING` 이 된다(`lifecycle.py:468-470`) — `claim_id` 가 있어 어떤 abandon 가드로도 끝낼 수 없고 `unresolved_child_attempt`(`commands.py:342-345`)·`unresolved_child_command`(`day_recovery.py:124-126`)를 계속 낸다.
4. **legacy 세 경로를 고정하는 시험은 0건이다.** 언급하는 6개 시험 파일은 전부 "발화하지 않게" 빈 dict 를 심거나 본문을 monkeypatch 로 대체한다.
5. **90초 SELL 폴백**(`engine.py:1974-2057`): naive 벽시계의 정규장(900≤hhmm<1530)에서 90초 넘은 `_pending_sides==SELL` 을 잡아 `cancel_all_for_symbol`(반환값을 **읽지 않는다** — 예외만 의미) → MARKET SELL `broker.submit_order` 직접 호출. 수량은 `min(_pending_quantities, pos.quantity)`(2026-08-04 P0 — 분할 익절 미체결이 전량으로 번지던 버그의 수정). 상한 `_MAX_FALLBACK=2` 를 넘으면 취소도 재주문도 없이 `clear_pending` 만 한다. **15:20~15:30 에는 취소를 이미 보낸 뒤 재주문 없이 `continue`** 한다(현행 결함). `submit_order` **예외**는 접수 여부를 모른 채 `clear_pending`. **exit_exempt 확인은 이 루프에 없다.**
6. **10분 BUY 정리**(`engine.py:2059-2090`): `cancel_all_for_symbol` 이 0건이어도 `cancel_ok=True` 가 되어 `clear_pending` 이 예약까지 전부 푼다 — 취소 0건과 취소 ACK 를 똑같이 최종성으로 읽는다(2026-08-04 P1 의 의도된 완화). 예외일 때만 pending 유지.
7. **둘 다 on_signal 본문 안에만 있다** — SIGNAL 이 한 건도 오지 않으면 90초 폴백도 10분 취소도 영원히 돌지 않는다(주기 태스크가 아니다). engine 밖의 `KRScheduler._cleanup_stale_pending`(`kr_scheduler.py:953-1044`)은 **별도 장부**(`bot._exit_pending_timestamps`)를 WS 틱마다 순회한다 — 10C 범위.
8. **eviction**(`engine.py:1653-1763`): 후보 제외 = `core_holding` · `sym in _pending_orders` · `_exit_exempt_ref` · `unrealized_pnl_pct > 0` · 같은 종목 600초 재축출 쿨다운. **최소 보유일 조건은 없다.** 정렬 `(entry_signal_score, pnl_pct)` 오름차순, 신규 점수가 희생 점수 +5 미만이면 스킵. 발행은 브로커 직접 호출이 아니라 `SignalEvent(source='replacement', metadata.quantity=전량)` 을 **자기 큐에 emit** — 세 경로 중 S3 의 H1(`_submit_signal`) 형태에 이미 맞는 것은 이것 하나뿐이다. 축출이 성립해도 원 BUY 는 그 사이클에 `G3_risk` 로 거부되고 `_last_signal_time` 이 찍히지 않는다.
9. **연쇄 축출은 닫혀 있지 않다.** 발행된 SELL 은 heap 큐에서 같은 priority 의 timestamp 순이라 이미 큐에 있던 BUY#2 **뒤에** 처리된다(`engine.py:400-406`, `event.py:61-68,165`). BUY#2 평가 시점에는 그 SELL 의 owner attempt 가 아직 없고, 첫 희생자만 600초 쿨다운에 걸리므로 **다른 희생자가 연달아 축출될 수 있다**(legacy 도 같다).
10. **`metadata['source']='replacement'`·`exit_action` 은 생산자만 있고 소비자 0건이다.** SELL 은 `evaluate_entry_policy`(`risk_policy.py:703-704`)·`FinalEntryGuard`(`guards.py:117-118`)·facts 소비를 모두 단락하므로 새 `EntryOrigin` 을 만들어도 판정은 0 만큼 바뀐다. 권한은 gateway 가 발급하는 `EntryAuthority.automatic` 하나다.
11. **`_exit_exempt_ref` 는 `scripts/run_trader.py:854` 에서만 주입된다**(제품 2·스크립트 2·시험 0). attach 설치자(10A3)가 이 주입을 재현하지 않으면 exit_exempt 가드는 빈 set 위에서 돈다.
12. SELL 의 지정가는 legacy `_get_sell_price` 가 판단 시점에 읽은 매수1호가다(`engine.py:2339,2505-2518`). owner 의 진입 시세·`source_is_current` 검사는 BUY 전용이고 `_bound` 는 binding 과의 동일성만 본다 — 판단~송신 사이에 호가가 벌어져도 통과한다.
13. 자식 행의 `order_ref` 는 prepare 시 항상 실리는 **부모의** 식별자다(`lifecycle.py:285-286,300`). 자식 자신의 송신 증거는 `claim_id`·`command_status`·`command_ref` 셋뿐이고 자식의 예약은 구조적으로 0 이다. `clear_settled_pending_sector` 는 kind≠submit 이면 즉시 반환한다.
14. S3 뒤 attach 모드의 현 상태: H4 로 legacy 장부가 비어 stale 루프 둘은 순회 대상이 없고, H7 로 잔류가 있으면 SIGNAL 자체가 거부되며, H5 로 eviction 은 호출되지 않는다.

## 2. coordinator 결정 (설계 권고 → 심사 이견 → 확정)

| # | 쟁점 | 확정 | 근거·심사 처분 |
|---|---|---|---|
| ① | 미체결 SELL 의 90초 시장가 에스컬레이션 | **attach 모드 미지원으로 명시. 만들지 않는다** | 사실 1·2 — 취소 최종성 증거가 없어 "취소 → 재주문"이 성립하지 않는다. 인계 지시("공식 취소 증거 미완이면 차단 유지")와 같은 방향. 심사 지적대로 이것은 원장 "S4 진입 조건 1"(복원 요구)에 대한 **범위 축소**이므로 보고 문장에 "attach 모드는 보호 SELL 에 관해 legacy 보다 계속 덜 안전하다"를 그대로 넣는다(§0) |
| ② | owner 취소의 제품 배선(10분 미체결 BUY 정리 포함) | **S4 에서 하지 않는다**(설계의 S4-2·S4-4 기각) | 심사 must-fix 2·3 — 취소를 켜는 순간 부모가 영구히 최종화 불가가 되고(체인 오염), claim 이후 transport 실패는 자식을 `RECONCILING` 으로 굳혀 그 종목과 sweep 을 영구히 잠근다(사실 3). ACK/UNKNOWN 자식의 종착역은 "S4 가 드러내는 잔여"가 아니라 **취소를 켜기 전에 닫아야 하는 전제**이고, 그것은 증거 계약 뒤다. attach 모드에는 미체결 BUY 의 타임아웃 취소가 계속 없다(BUY 는 항상 MARKET 이라 정규장에서는 즉시 체결되는 경로) |
| ③ | LIMIT SELL 지정가의 dispatch 직전 재확인 | **하지 않는다**(설계의 S4-3 기각). 원장 "S4 진입 조건 4" 는 "재확인하지 않는다"로 닫는다 | 심사 must-fix 1 — "불리한 방향만 막는다"는 안전장치가 곧 손절 방향이다. 하락장에서 호가가 한 틱만 내려가도 보호 SELL 이 체계적으로 거부되고, ①로 에스컬레이션도 없으니 손절이 거래소에 닿을 길이 없어진다(S3 현재보다 나빠진다). 또 가격 판정을 gateway 에 새로 만드는 것은 S3 계약 1(owner 판정 복제 금지)과 어긋나고, prepare~dispatch 사이에 새 network await 를 넣어 wave 3 의 P1(세션 경계 누수) 창을 넓힌다. 지정가가 안 붙는 문제의 해법은 에스컬레이션이고 그것은 ①의 전제 뒤다 |
| ④ | legacy 세 경로의 특성화 | **먼저 세운다**(S4-0, 제품 0줄) | 사실 4 — S2-1 의 교훈. **현행 결함까지 그대로 고정한다**(동시호가 분기·취소 0건 해제·예외 시 `clear_pending`). 독스트링에 "legacy 불변 대조용이며 옳은 동작의 정의가 아니다"를 명시한다. exit_exempt 가드는 오늘 시험 0건이라 여기서 처음 고정된다 |
| ⑤ | 미claim 자식 명령의 종료 | `abandon_candidate` 의 가드를 **kind 인지형**으로: submit 은 현행 그대로, cancel/modify 는 `order_ref` 가드를 **부모 `order_ref` 와 일치할 때만** 면제하고 `claim_id None`·`command_status None`·`command_ref` 부재·체결 0·증거 충돌 없음을 모두 요구(S4-1). 이어서 `commands._unsent` 가 자식 명령에도 그것을 부른다(S4-1b) | 사실 13. S3 가 넘긴 잔여(결정 ⑧). 제품의 취소 호출자는 여전히 0건이지만 owner 의 취소 경로(시험이 구동한다)가 claim 이전 실패에서 잔류를 남기지 않게 된다. 가드 완화 방향이 틀리면 ACK 된 SUBMIT 이 지워지므로 면제를 **kind + 부모 order_ref 일치**로 이중 한정하고 SUBMIT 보호를 변이 kill 항목으로 둔다. ACK 된/UNKNOWN 자식은 이 단계가 풀지 않는다(②) |
| ⑥ | eviction | attach 에서 **되살린다**(S4-2): H5 의 attach 차단을 걷고, 후보 필터의 `_pending_orders` 를 attach 에서는 owner 의 미해결 종목으로 바꾼다(gateway 의 동기 helper `unresolved_symbols()`). 발행 형태는 그대로(SignalEvent → H1 → `gateway.submit`) — 새 송신 경로를 만들지 않는다 | 사실 8·10. 세 경로 중 취소에 의존하지 않는 유일한 경로다. 새 `EntryOrigin` 은 만들지 않는다(판정을 0 만큼 바꾼다 — 심사 동의). `metadata['source']` 는 권한이 아님을 시험으로 고정 |
| ⑦ | **연쇄 축출(심사 must-fix — 설계의 "자연히 닫힌다" 기각)** | attach 에서만 **전역 600초에 1건 상한**: `_REPLACEMENT_LAST_EVICT_TS` 에 600초 안의 기록이 하나라도 있으면 축출하지 않는다 | 사실 9 — 큐 순서 때문에 owner 필터로는 닫히지 않는다. S3 가 완전히 막아 둔 것을 S4 가 실제 SELL POST 로 여는 단계이므로 한 사이클에 두 포지션이 매도되는 것을 허용하지 않는다. legacy 는 불변(상한 없음 — 특성화로 고정) |
| ⑧ | exit_exempt·승자·코어 보호 | 한 줄도 바꾸지 않고 **attach 경로에서 RED + 변이 kill 로 고정** | 절대 자동매도 금지(펩트론 087010). 사실 11 은 10A3 인수 조건으로 이월: attach 설치자가 `_exit_exempt_ref` 주입을 재현해야 한다 |
| ⑨ | S3 시험의 기대값 변경(좁은 예외) | 정확히 **2건**: `test_execution_signal_gateway_wiring.py::test_eviction_is_not_reached_in_attach_mode`(S4-2 — 단언 반전) · `test_execution_dispatch_reasons.py::test_an_unclaimed_cancel_is_left_for_s4_and_never_abandoned`(S4-1b — 단언 반전). 둘 다 "S4 로 미룬다"를 현행으로 고정했던 시험이다 | `test_the_entry_stale_loops_have_nothing_to_sweep_in_attach_mode`·`test_a_risk_manager_with_legacy_orders_connected_after_attach_cannot_post_directly` 는 **변경 금지** — ②로 sweep 을 만들지 않으므로 그대로 참이어야 한다 |

## 3. 계약

1. S3 의 계약 1~8 은 그대로다. 특히 **단일 송신로**(attach 에서 POST 는 `RequestBoundCommands.dispatch` 한 길) — eviction SELL 도 그 길로만 간다.
2. **취소 0건·취소 ACK 는 최종성이 아니다.** S4 는 attach 에서 취소를 보내지 않는다. legacy 의 "0건도 해제"는 legacy 에서만 남고(특성화), attach 로 가져오지 않는다.
3. **자식 종료는 부모를 만지지 않는다.** 부모 행의 state·version·예약 4항·pending sector 는 글자 하나 안 바뀐다.
4. **exit_exempt 종목은 어떤 경로로도 자동 매도되지 않는다.**
5. attach 의 eviction 은 600초에 전역 1건. legacy 는 불변.
6. legacy(no-runtime)·US 경로는 실행 결과 차이 0(§5).
7. 보고 문장: "S4 는 설치가 아니다 · 제품 attach 호출자 0건 · 합성 startup 허가 위의 GREEN · attach 모드에는 미체결 SELL 의 시장가 에스컬레이션과 미체결 BUY 의 타임아웃 취소가 없다."

## 4. 하위 단계

| wave | 단계 | 제품 파일(단일 writer) | 새 시험 파일 |
|---|---|---|---|
| 1 | S4-0 ∥ S4-1 | 없음 ∥ `lifecycle.py` | `test_engine_legacy_stale_eviction_characterization.py` ∥ `test_execution_child_command_close.py` |
| 2 | S4-1b ∥ S4-2 | `commands.py` ∥ `src/core/engine.py`·`gateway.py`(helper 하나) | (기존 `test_execution_dispatch_reasons.py` 의 예외 1건 + 같은 파일에 추가) ∥ `test_execution_signal_gateway_eviction.py` |

공통은 S3 와 같다: harness 격리 worktree 에서 `git switch -c work/<이름> <base SHA>`, 기준선 시험이 첫 단계, RED 커밋이 제품 커밋보다 앞, 새 시험 파일은 `synthetic_home` autouse·벽시계 의존 0·UTC·KST 양쪽, 실제 store/runtime 은 `finally` 에서 닫고 새 시험 파일을 `test_execution_account_lease.py` **앞에 같은 프로세스로** 돌려 fd 위생 확인, 기존 시험 수정 0줄(예외는 결정 ⑨의 2건), **변이 전건 kill 이 인수 조건**, 독립 재현자는 자체 변이 ≥3. **live 파일(`engine.py`)을 만진 단계는 지정 파일 GREEN 만으로 통합하지 않는다 — coordinator 가 전체 suite 를 단독 직렬로 돌린다.**

### S4-0 — legacy 세 경로의 특성화 (제품 0줄)

- **시험:** `tests/test_engine_legacy_stale_eviction_characterization.py` · **의존:** 없음
- **구동:** runtime 없는 `UnifiedEngine` + 실제 inner `RiskManager` + engine 모듈 naive 시계 동결(`tests/test_execution_signal_gateway_wiring.py` 의 `engine_clock`·`risk_manager` 재사용). `cancel_all_for_symbol` 의 0건/N건/예외와 `submit_order` 의 성공/False/예외는 monkeypatch 로 심는다. 시계 하나로 90초·10분·정규장을 모두 민다.
- **고정할 것(현행 그대로 — 결함 포함):**
  - [폴백 발화] 90초 넘은 미체결 LIMIT SELL + 다음 SIGNAL → `cancel_all_for_symbol` → `submit_order`(MARKET) 가 이 순서로 1회씩, `_pending_timestamps`·`_pending_sides`·`_pending_fallback_count` 갱신
  - [폴백 수량] 보유 100주 중 30주 미체결의 폴백은 30주. `_pending_quantities` 를 비우면 100주(대조)
  - [폴백 상한] 폴백 2회 뒤 3회째는 취소도 재주문도 없이 `clear_pending` 만
  - [동시호가] 15:20~15:30 에는 취소를 이미 보낸 뒤 재주문 없이 pending 유지
  - [폴백 예외] `submit_order` 예외 → `clear_pending` / 반환 False → 카운트 +1 후 유지
  - [신호 없으면 폴백 없음] SIGNAL 이 오지 않으면 90초가 지나도 아무 일도 없다
  - [10분 BUY·취소 0건] 0 을 돌려줘도 `clear_pending` 이 예약·`_reserved_by_order`·`_pending_signal_cache` 까지 푼다 / [10분 BUY·예외] pending 유지 / 브로커·메서드 부재면 해제
  - [eviction] 후보 제외 순서(core·`_pending_orders`·**exit_exempt**·pnl>0·600초)와 정렬 · +5점 미만 스킵 · 발행은 `engine.emit(SignalEvent)` 이고 `metadata['quantity']` 는 전량 · 축출 뒤 원 BUY 는 `G3_risk` 거부이며 `_last_signal_time` 미기록 · **같은 배치의 고득점 BUY 두 건이 서로 다른 희생자를 연달아 축출한다**(사실 9 — legacy 는 닫혀 있지 않다)
- **변이(제품에 일시 적용해 시험이 죽는지 — 끝나면 원복):** 폴백 수량의 `min(...)` 을 `pos.quantity` 로 / exit_exempt `continue` 삭제 / `fallback_cnt >= _MAX_FALLBACK` 을 False 로 / 동시호가 분기 삭제 / +5 를 +0 으로 / **`cancel_ok = True` 를 `cancel_ok = cancelled` 로**(심사 추가 — "0건도 해제한다"가 실제로 고정됐는지) / pnl>0 제외 삭제 / core_holding 제외 삭제
- **위험:** 낮음. 특성화가 현행 결함을 "계약"으로 굳히는 것처럼 읽히지 않게 독스트링에 명시한다.

### S4-1 — `lifecycle.py`: 미claim 자식 명령의 종료 간선

- **제품 파일:** `src/execution/safety/lifecycle.py` · **시험:** `tests/test_execution_child_command_close.py` · **의존:** 없음(S4-0 과 병렬)
- **고정 인터페이스:** `abandon_candidate(attempt_id, *, reason) -> bool` 의 시그니처·반환·터미널(`FINAL_REJECTED` + `command_status='not_sent'` + `reason_code`)은 그대로(`gateway.recover_unsent` 가 이미 쓴다). 가드만 kind 인지형으로(결정 ⑤). 자식 abandon 은 부모 행을 만지지 않는다. `commands._unsent` 의 "SUBMIT 일 때만"은 이 단계에서 바꾸지 않는다(S4-1b).
- **RED:**
  - [행동] prepared·claim None·command_status None 인 취소 자식을 끝낼 수 있다(오늘 False)
  - [부수효과 0] 자식 abandon 뒤 부모 행의 state·version·예약 4항·`pending_sectors` 가 글자 하나 안 바뀐다
  - [재취소 해금] abandon 뒤 같은 부모에 두 번째 취소 prepare 가 통과한다(전에는 `previous child command unresolved`)
  - [종목 해금] abandon 뒤 같은 종목의 새 SUBMIT 이 `unresolved_child_attempt` 를 넘는다
  - [일자 전환] **부모를 터미널로 만든 표본에서** abandon 뒤 `unresolved_reason` 이 `unresolved_child_command` 를 더 이상 돌려주지 않는다(심사: 부모가 비터미널이면 `unresolved_submit` 이 먼저 나와 단언이 무의미하다)
  - [섹터] 부모가 이미 터미널이고 자식만 남아 있던 표본에서 자식 abandon 이 pending sector 를 어떻게 남기는지 **현행을 관측해 고정**한다(`clear_settled_pending_sector` 는 kind≠submit 이면 즉시 반환 — 풀리지 않는다면 그 사실을 단언하고 unverified 에 적는다)
  - [SUBMIT 보호 유지] ACK 되어 `order_ref` 가 채워진 SUBMIT 은 여전히 abandon 되지 않는다
  - [claim 된 자식 거부]·[ACK 된 자식 거부(`RECONCILING`·`command_ref` 있음)]·[order_ref 위조 — 부모 것과 다르면 거부]
  - [restore 뒤 성립] 실제 SQLite store 를 닫았다 다시 열어 restore 한 뒤에도 같은 결과(`finally` 에서 store 를 닫는다)
- **변이:** kind 조건을 지우고 `order_ref` 가드를 통째로 제거 / `claim_id is None` 제거 / `command_status is None` 제거 / 부모 order_ref 대조를 True 로 / `command_ref` 부재 요구 제거
- **위험:** 중간 — 이 가드는 "이미 보낸 주문의 증거를 지우지 마라"는 방어선이다.

### S4-1b — `commands.py`: `_unsent` 가 자식 명령도 끝낸다

- **제품 파일:** `src/execution/safety/commands.py` · **시험:** `tests/test_execution_dispatch_reasons.py`(결정 ⑨의 예외 1건 개정 + 같은 파일에 추가) · **의존:** S4-1
- **고정 인터페이스:** `_unsent(request, reason)` 의 "SUBMIT 일 때만" 조건을 걷는다 — 어떤 명령이든 claim 이전에 실패하면 `abandon_candidate` 를 부른다(누가 끝낼 수 있는지는 lifecycle 의 가드가 정한다). 독스트링의 "자식 명령은 S4" 문장을 갱신한다. 그 밖의 분류(`ApplicationBlocked` 재던짐·`dispatch_failed`·False 시 로그)는 그대로.
- **RED:** [행동] claim 이전에 실패한 cancel dispatch 뒤 자식 행이 `final_rejected`·`reason_code` 보존이고 같은 부모에 다시 취소를 prepare 할 수 있다(오늘은 자식이 prepared 로 남아 두 번째 prepare 가 `previous child command unresolved`) · [대조] claim **이후** transport 실패의 자식은 `state=='reconciling'`·`command_status=='unknown'` 이고 abandon 이 거부되며 부모 예약 4항이 그대로다(사실 3 — 이 잔류는 S4 가 풀지 않는다는 것을 상태값까지 고정) · [대조] SUBMIT 경로의 기존 시험 불변
- **변이:** 조건을 다시 SUBMIT 한정으로 / claim 이후 실패에도 abandon 을 부름
- **위험:** 낮음(제품 호출자 0건). 기존 시험 1건의 단언 반전은 줄 단위로 보고한다.

### S4-2 — `engine.py`·`gateway.py`: eviction 을 owner 경로로

- **제품 파일:** `src/core/engine.py`(허용 hunk 3곳), `src/execution/safety/gateway.py`(동기 helper `unresolved_symbols()` 하나) · **시험:** `tests/test_execution_signal_gateway_eviction.py` + 결정 ⑨의 S3 시험 1건 개정 · **의존:** S4-0(legacy eviction 이 특성화로 고정된 뒤)
- **허용 hunk(이 밖의 변경 0, 추가 실행 줄은 attach 가드 안):**
  - **H5 복원:** eviction 호출부의 `and getattr(self.engine, "_execution_runtime", None) is None` 조건 한 줄 삭제(S3-6a 가 engine.py 에서 기존 줄을 바꾼 유일한 곳을 원래대로)
  - **H9 후보 필터:** `if sym in self._pending_orders` 가 attach 에서는 owner 의 미해결 종목을 본다 — `_attached_gateway(getattr(self, "engine", None))` 이 있으면 `gateway.unresolved_symbols()`, 없으면 `self._pending_orders` **그 객체 그대로**(legacy 결과 동일). 예외는 흡수하지 않는다(예외가 `_submit_signal` 로 올라가 그 SIGNAL 이 명시 거부로 끝나는 것을 RED 로 고정)
  - **H10 상한(결정 ⑦):** attach 에서만, `_REPLACEMENT_LAST_EVICT_TS` 에 600초 안의 기록이 하나라도 있으면 축출하지 않는다
  - `gateway.unresolved_symbols() -> frozenset[str]`: 다른 두 helper 와 같이 `_owner_ready` → `commands._snapshot(state).pending` 에서 종목만 모은다(새 식을 만들지 않는다). **미해결 SELL attempt 의 종목이 `pending` 에 들어 있는지 worker 가 먼저 확인한다** — 들어 있지 않으면 구현하지 말고 deviations 에 보고하고 멈춘다(coordinator 가 출처를 다시 정한다)
  - exit_exempt·core_holding·pnl>0·600초 종목 쿨다운·+5점은 한 줄도 바꾸지 않는다
- **RED (`_order_env` 는 `_risk_validator` 를 None 으로 스텁한다 — eviction 호출부에 닿으려면 "최대 포지션 수 도달"을 돌려주는 `_risk_validator`·실제 portfolio 포지션·`entry_signal_score` 를 시험이 직접 심는다):**
  - [행동·핵심] attach 에서 만석 + 점수 ≥ `_REPLACEMENT_MIN_SCORE` 인 BUY → SELL SignalEvent 가 큐에 들어가고, 그것을 구동하면 owner 경로로 POST **정확히 1건**(`broker.submit_order` 직접 호출 0). 오늘은 H5 가 막아 0
  - [exit_exempt] 유일한 후보가 `_exit_exempt_ref` 에 있으면 SELL SIGNAL 0·POST 0 · [승자·코어 보호] pnl>0·core_holding 은 attach 에서도 축출되지 않는다 · [+5점] 우위 5 미만이면 POST 0
  - [중복] owner 에 미해결 SELL attempt 가 있는 종목은 후보에서 빠진다(legacy `_pending_orders` 로 두면 attach 에서 그 집합은 비어 있다)
  - [상한] 같은 배치의 고득점 BUY 두 건 → 축출 SELL 은 **1건뿐**(둘째는 서로 다른 희생자가 있어도 축출하지 않는다). 대조: legacy 는 S4-0 특성화대로 2건
  - [owner 의 최종 방어선] 같은 종목에 미해결 SELL attempt 가 있을 때 두 번째 eviction SELL 을 강제로 구동하면 owner 가 `unresolved_symbol_attempt`(또는 보유−예약 부족)로 POST 0 — 사유 코드까지 단언
  - [원 BUY 거부] 축출이 성립해도 원 BUY 는 같은 사이클에 `G3_risk` 거부, POST 는 SELL 1건뿐
  - [권한] `metadata['source']` 를 지우거나 바꿔도 송신 가능/불가가 바뀌지 않는다
  - [helper 예외] `unresolved_symbols()` 가 예외를 내면(owner 복구 필요) 그 BUY SIGNAL 은 `errors_count` +1 로 끝나고 축출 0
  - [ready=False 대조] 합성 허가 없이 같은 시나리오 → SELL SIGNAL 은 발행되지만 POST 0·예약 0
  - [legacy 불변] runtime 없는 엔진에서 H9 가 `self._pending_orders` **같은 객체**를 돌려주고 H10 상한이 걸리지 않는다(S4-0 의 연쇄 특성화가 그대로 통과) · `engine` 속성 없는 부분 생성 인스턴스 축(S3 wave 5 의 회귀)
- **변이:** H5 를 다시 attach 차단으로 / H9 를 attach 에서도 `_pending_orders` 로 / H9 의 legacy 분기를 owner 로(가드 항상 참) / **H10 상한 제거** / H10 을 legacy 에도 적용(가드 항상 참) / exit_exempt `continue` 삭제 / pnl>0 제외 삭제 / core_holding 제외 삭제 / emit 대신 `broker.submit_order` 직접 호출
- **위험:** 높음 — attach 에서 실제 SELL POST 를 새로 여는 유일한 단계다.

## 5. legacy·US 불변 증명

S3 §5 의 네 겹(구조 diff 검사 · 기준선 불변 · 행동 대조 · "attach 가드를 항상 참으로" 변이 kill)에 두 축을 더한다: **S4-0 의 특성화가 S4-2 뒤에도 한 글자 안 고치고 통과한다** · **`engine` 속성 없는 부분 생성 `RiskManager` 인스턴스**에서 새 helper 가 종전 값으로 떨어진다. engine.py 를 만진 단계는 coordinator 가 전체 suite(UTC→KST, 단독 직렬)를 돌린 뒤에만 통합을 확정한다. `src/`·`scripts/` 의 `KRExecutionRuntime(`·`.attach(`·`install_gateway(`·`recover_unsent(` 제품 호출자 0건을 단계마다 grep 으로 재확인한다.

## 6. S4 에서 하지 않는 것 (그리고 그것을 여는 전제)

- **미체결 SELL 의 시장가 에스컬레이션 · 미체결 BUY 의 타임아웃 취소 · owner 취소의 제품 배선** — 전제: 취소·체결 최종성의 증거 계약(공식 KIS 증거, `parse_order_evidence` 의 체인 최종성, `lifecycle.reconcile`·`EXECUTION_FILL` 의 제품 생산자 = 10A2/10C). 그 뒤에야 ACK/UNKNOWN 자식의 종착역과 "취소 확정 뒤 재주문"이 성립한다.
- **SELL 지정가의 재확인** — 하지 않는다(결정 ③). 해법은 위 에스컬레이션이다.
- **S5:** 독립 실큐 인수·최종 broad 리뷰·전체 직렬. S3 결정 ③(ORDER 이벤트를 큐에 싣지 않음)과 S3 의 Codex 3차 처분(`3ff04c8..10d2ca7`)·S4 의 범위 축소(결정 ①②③)를 확인받는다.
- **10A3:** attach 설치자가 `_exit_exempt_ref` 주입을 재현할 것(사실 11) · `recover_unsent()` 호출 · factory·`config_version` 5축 · attach 인지형 대시보드.
- **10C:** `KRScheduler._cleanup_stale_pending`(별도 장부 `_exit_pending_timestamps`, 취소 0건 해제 포함)·KOFR·수동 매수·CLI.
- legacy 의 현행 결함(동시호가에 취소만 보내고 재주문 없음, 폴백 상한 뒤 원 지정가 방치, `submit_order` 예외 시 접수 여부를 모른 채 `clear_pending`, 폴백 루프에 exit_exempt 확인 없음)은 **S4 가 고치지 않는다** — 특성화로 드러내 두고 운영 경로의 별도 수정 과제로 남긴다.

## 7. 위험·미확인

- S4 뒤에도 attach 모드는 보호 SELL 에 관해 legacy 보다 덜 안전하다(§0). 이 한 문장이 attach 설치의 차단 사유다.
- eviction SELL 은 지정가(매수1호가)다 — 붙지 않으면 그대로 남고(에스컬레이션 없음) 포지션이 비지 않아 원 BUY 의 재진입도 계속 막힌다(fail-closed 방향).
- 조사·설계·심사는 같은 provider(요청 Opus)이고 관측 모델은 미검증이다. 교차 provider 확인은 wave 2 뒤 Codex 포그라운드 리뷰로 받는다 — 범위에 S3 의 3차 처분 diff(`3ff04c8..10d2ca7 -- src/core/engine.py`)를 포함한다.
- `_risk_validator`(실제 `risk/manager.py` 게이트)와 팩터 버킷 게이트는 attach 경로 시험에서 여전히 스텁이다(S5).

## 8. 역할·모델·한도 (정책 `ai-routing-v1-2026-09-20`)

| 역할 | 요청 모델/effort | 한도 |
|---|---|---|
| coordinator | 이 세션 — Plan·diff 확인·변이 재적용·통합·문서·전체 suite | — |
| S4-0~S4-2 구현 | claude-opus-5 / high | 단계당 60~75분 · 새 시험 ≤25건 · 허용 파일만 |
| 단계별 독립 재현 | claude-opus-5 / xhigh, 구현 worker 와 다른 실행 — 지정 변이 전건 + 자체 변이 ≥3 | 단계당 1회 |
| 교차 provider 리뷰 | Codex gpt-6-astra / xhigh, companion 포그라운드(`--background` 금지)·read-only·10분 상한 | wave 2 뒤 1회 + 같은 요청 재시도 1회 |

- active worker ≤3(전 provider 합산)이나 pytest 를 도는 worker 는 동시 ≤2. 전체 suite 는 어떤 에이전트도 없이 단독 직렬, 시작·종료 시 `uptime` load 기록.
- worker 금지: 운영 `.env`·토큰·`~/.cache/ai_trader*`·네트워크·systemctl·git push·engine 브랜치 직접 수정·허용 목록 밖 파일·다른 에이전트 호출.
