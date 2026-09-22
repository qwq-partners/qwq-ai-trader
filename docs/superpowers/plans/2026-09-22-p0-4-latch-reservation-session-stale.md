# P0-4 — 약한 지점 4곳: `_protection_failed` 래치 · 부분 체결 뒤 예약 · 세션 경계 소멸 · `_cleanup_stale_pending` (Plan, 2026-09-22 저녁)

> 결정 문서 `2026-09-22-kis-judgement-decisions.md` §4 의 약한 지점 2·4·7·9. P0-3 마감(`80571eb`) 뒤 착수. 기준 `bc1301a`(제품 `80571eb`). 운영 미설치·제품 호출자 0건 상태는 그대로다.

## 0. 과정

조사 2(opus/high, 읽기 전용) → 설계 1(opus/high) → 적대적 심사 2관점(opus/xhigh: ① 돈·상태 손실 ② 실현 가능성·인용) → **둘 다 REVISE**(① must-fix 6 · ② must-fix 9) → coordinator 처분(§3) → 확정 단계(§4) → Do·See(§5). 워크플로 `w56wd2z0u`, 관측 모델 `claude-opus-5`.

## 1. 조사가 확정한 사실(요약 — 인용은 `bc1301a` 기준)

#### 조사 A(래치·세션 경계)

- **F-B1-1** (high) 예약 수량의 실제 필드명은 `reserved_quantity`(+ `reserved_cash`/`reserved_exposure`/`reserved_planned_risk`)이고, prepare 시점에 SUBMIT 이면 요청 전량으로 설정된다(CANCEL/MODIFY 는 0).
  - src/execution/safety/lifecycle.py:306 `"reserved_quantity": quantity if kind is CommandKind.SUBMIT else 0,` / :307 `"reserved_cash": cash if kind is CommandKind.SUBMIT else "0"`. 요청 바운드 경로는 src/execution/safety/commands.py:474-484(`prepare_candidate(... reserved_cash=str(resources.cash))` 뒤 `attempt['reserved_exposure']`·`attempt['reserved_planned_risk']` 부여).
- **F-B1-2** (high) **결정 문서 §4-2 항목 4 의 기술은 코드와 어긋난다 — `reserved_quantity` 는 terminal 전에도 체결 증분만큼 줄어든다.** `reduce_economics` 가 매 체결마다 `remaining = max(0, reserved - delta.quantity)` 로 줄이고 현금·노출·계획위험도 비례 축소한다.
  - src/execution/safety/economics.py:465-467 `reserved = _integer(attempt["reserved_quantity"]) / reserved_cash = ... / remaining = max(0, reserved - delta.quantity)`, :468-469(현금 비례), :470-479(exposure/planned_risk 비례), :480 `attempt.update(applied_quantity=observation.cumulative_quantity, reserved_quantity=remaining, reserved_cash=str(next_cash))`.
- **F-B1-3** (high) 그 축소는 우연이 아니라 **application 계층이 강제하는 계약**이다 — 체결 write-set 검증기가 `reserved_quantity` 를 허용 변경 필드로 두고, 이번 체결분보다 크게 푸는 것도(과다 해제) 늘리는 것도 거부한다.
  - src/execution/safety/application.py:316-317 `allowed = {"applied_quantity", "reserved_quantity", "reserved_cash", "reserved_exposure", "reserved_planned_risk"}`, :333 `if not max(0, reserved_before - delta.quantity) <= reserved_after <= reserved_before <= ordered: raise ValueError("이번 체결보다 큰 예약 수량 해제")`, :341-344(현금 비례 상한), :360-362(자원 비례 상한).
- **F-B1-4** (high) 따라서 `held − reserved` 는 **음수가 되지 않는다.** 100주 SELL 중 40주 체결 뒤 `held = 60`(economics.py:437 `before.quantity -= delta.quantity`), `reserved = 60` → 차이는 정확히 0. 문서의 "`held − reserved < 0`" 은 틀렸고, 결론(잔여 60 에 새 보호 SELL 불가)만 맞다.
  - src/execution/safety/economics.py:447 `before.quantity -= delta.quantity`(437 은 raise 문 — 재현 R1 정정) / :467 `remaining = max(0, reserved - delta.quantity)` / src/execution/safety/commands.py:422-426 `held = next((position.quantity for position in snapshot.portfolio.positions if position.symbol == request.symbol), 0)` … `_require(held - reserved >= request.quantity, 'reserved_quantity_insufficient')`.
- **F-B1-5** (high) **거부 낱말은 `reserved_quantity_insufficient` 가 아니라 `unresolved_symbol_attempt` 다.** 같은 종목에 비terminal submit attempt 가 하나라도 있으면 새 SUBMIT 은 `held−reserved` 검사에 **도달하기 전에** 막힌다(같은 `_evaluate` 안에서 394줄이 426줄보다 앞). side 를 보지 않으므로 SELL→SELL 도, BUY→SELL 도 같은 낱말이다.
  - src/execution/safety/commands.py:393-394 `if request.command is CommandKind.SUBMIT: _require(attempt['symbol'] != request.symbol, 'unresolved_symbol_attempt')` vs :426 `_require(held - reserved >= request.quantity, 'reserved_quantity_insufficient')`. 자식(취소) attempt 는 :382-383 에서 `unresolved_child_attempt` 로 따로 막는다.
- **F-B1-6** (high) `reserved_quantity_insufficient` 는 **같은 종목 충돌에서는 사실상 도달 불가**하다 — `reserved` 합계는 `a['symbol'] == request.symbol` 인 active attempt 만 더하는데, 그런 attempt 가 있으면 394줄이 먼저 raise 한다. 실질 기능은 "보유보다 많이 팔지 마라"(reserved=0 인 경우)뿐이다.
  - src/execution/safety/commands.py:424-426 `reserved = sum(a['reserved_quantity'] for a in active if a['side'] == 'sell' and a['symbol'] == request.symbol)`; `active` 에는 :394 를 통과한(=다른 종목인) submit 만 append 된다(:395 `active.append(attempt)`).
- **F-B1-7** (high) **P0-4 의 판단 재료: 항목 4 에는 고칠 '예약 산술'이 없다.** 원 SELL 이 거래소에 살아 있는 동안 잔여 60주를 예약으로 붙들고 있는 것은 정상이며, 394줄을 열어도 426줄이 같은 결론(0 >= 60 거짓)을 낸다. 잔여 60 을 구제하려면 **취소·에스컬레이션(항목 5)** 이 필요하고 그것은 P1(D2)이다.
  - F-B1-4·F-B1-5·F-B1-6 의 인용 + docs/superpowers/plans/2026-09-22-kis-judgement-decisions.md:47(D2 "attach 는 취소를 보내지 않는다 … 목표는 어젯밤 main 이 도달한 수준과의 동등 … §5 의 전제 단계(P0) **뒤**"), :75(약한 지점 5 "미체결 SELL 에스컬레이션 없음(D2)").
- **F-B1-8** (high) **항목 4 의 지속형(진짜 영구 봉쇄)은 따로 있다 — 부분 체결된 주문이 장 마감으로 소멸해도 owner 는 그것을 배울 수 없다.** 증거 파서는 `FINAL_FILLED`(전량 체결) 아니면 `RECONCILING` 만 만들고, `reconcile` 은 `FINAL_EXPIRED` 를 명시적으로 지원하지 않는다. 그래서 attempt 는 `partial` 로 영구히 남고 예약 60 도 영구히 남는다.
  - src/execution/safety/evidence.py:265-272(`supported = ... value["filled"] == value["qty"] and value["remaining"] == 0 and value["cancelled"] == 0 and value["rejected"] == 0`), :279 `OrderState.FINAL_FILLED if supported else OrderState.RECONCILING` — 이 파서에 `FINAL_CANCELLED`/`FINAL_EXPIRED` 생산 경로가 없다. src/execution/safety/lifecycle.py:560-562 `elif evidence.state is OrderState.FINAL_EXPIRED: # 이번 단계에서는 고정된 만료 계약을 지원하지 않는다 / final = False`.
- **F-B1-9** (high) 그 영구 `partial` 은 종목 잠금에서 끝나지 않는다 — **일자 전환 자체를 BLOCKED 로 만든다**(비terminal submit 과 남은 예약이 각각 별도 사유). 즉 다음 영업일에 그 종목을 다시 팔 수도, 장부를 넘길 수도 없다.
  - src/execution/safety/runtime.py:208 `return scope_reason(state, self.account_scope) or unresolved_reason(state)` / src/execution/safety/day_recovery.py:127-128 `elif row.get("state") not in TERMINAL_STATES: return "unresolved_submit"`, :132-133 `if has_remaining_reservation(row): return "remaining_reservation"`; 이 사유가 있으면 :283 `{"status": "BLOCKED" if reason else "PREPARED", ...}`(runtime.py:281-283).
- **F-B1-10** (high) terminal 진입 시 예약 해제는 세 지점에서만 일어난다: 명령 실패(NOT_SENT/REJECTED)·미송신 포기(abandon)·증거 기반 최종성. 마지막 것은 `applied_quantity == observed_quantity` 일 때만 푼다.
  - src/execution/safety/lifecycle.py:490-491(`if status in (CommandStatus.NOT_SENT, CommandStatus.REJECTED): attempt["state"] = OrderState.FINAL_REJECTED.value; _release_resources(state, attempt_id)`), :417(abandon), :570-571 `if attempt["applied_quantity"] == qty: _release_resources(state, attempt_id)`; 본체는 :234-247.
- **F-B1-11** (high) **P0-2 의 late-fill 함정과 항목 4 의 충돌 지점은 `commands.py:394` 한 줄이다.** P0-3 은 F8 ①(같은 종목 반대 side 허용)을 P1 로 미뤘는데, 그 이유가 late-fill 이다 — 전량 청산이 lot 을 닫으면(`if full:` → `closed=True`) 부모 BUY 의 늦은 잔여 체결을 economics 가 거부해 `unresolved_execution_evidence` 로 **전 종목 영구 정지**가 된다.
  - docs/superpowers/plans/2026-09-22-p0-3-wiring-sync-gate.md:20("BUY 100/40 적용 → 보호 SELL 40(전량)이 `if full:` 에서 lot 을 `closed=True` → 남은 60 이 늦게 체결되면 … `unresolved_execution_evidence` **영구 전역 정지**"), :32(Q-1 "F8 ①은 P0-3 에서 열지 않는다 … → **P1**"). 코드: src/execution/safety/economics.py:455-457(`if full: ... for lot in lots.values(): if lot["symbol"] == symbol: lot["closed"] = True`), :384-385 `if existing_lot is not None and (existing_lot["closed"] or before is None): raise ValueError("종결된 진입의 늦은 체결은 대사가 필요합니다")`, 전역 정지 지점 src/execution/safety/commands.py:385-387 `_require(attempt['observed_quantity'] == attempt['applied_quantity'] and not attempt.get('evidence_conflict') and attempt['state'] != 'blocked_unknown', 'unresolved_execution_evidence')`(종목 무관 전역).
- **F-B2-1** (high) engine 브랜치의 `_cleanup_stale_pending` 은 **attach 를 전혀 모른다**(`_execution_runtime` 검사 0건). 타임아웃은 장전 5분·정규장 이후 3분, 종목당 60초 취소 스로틀, 취소 API 예외는 15분까지 pending 유지.
  - src/schedulers/kr_scheduler.py:916-1006(본문 전체). :924 `if not bot._exit_pending_timestamps: return`, :927 `stale_minutes = 5 if now_time.hour < 9 else 3`, :944-948(60초 스로틀), :956-957 `cancelled = await bot.broker.cancel_all_for_symbol(s)`, :967-969(15분 강제 해제). 이 함수 범위 안에 `_execution_runtime` 참조 없음(가장 가까운 것이 :1026).
- **F-B2-2** (high) 반환 0 의 해석: **`cancelled` 가 falsy 면 아무 분기도 타지 않고 그대로 해제로 진행**한다. 해제는 pending 집합·타임스탬프 제거 → `risk_manager.clear_pending(s)` → **`exit_manager.rollback_stage(s)`** 순이다. 로그만 남는다.
  - src/schedulers/kr_scheduler.py:958-959 `if cancelled: logger.info(...)` (else 분기 없음) → :971-977 `bot._exit_pending_symbols.discard(s)` / `bot._exit_pending_timestamps.pop(s, None)` / `await bot.engine.risk_manager.clear_pending(s)` / `bot.exit_manager.rollback_stage(s)`. 주석 :942 "취소 0건 = 주문 이미 소멸 → 해제 진행".
- **F-B2-3** (high) `cancel_all_for_symbol` 은 **브로커의 인메모리 `self._pending_orders` 만 돈다.** attach 는 `submit_order` 를 쓰지 않고 `GuardedKISTransport` 가 직접 POST 하므로 그 캐시가 비고, 항상 0 을 돌려준다 — 이것이 항목 9 의 오판 메커니즘이다.
  - src/execution/broker/kis_kr.py:944-961 `orders_to_cancel = [(oid, order) for oid, order in self._pending_orders.items() if order.symbol == symbol and order.is_active]` → `for order_id, order in orders_to_cancel: if await self.cancel_order(order_id): cancelled += 1` → `return cancelled`. attach 의 직접 POST 는 src/execution/safety/transport.py:188·196 계열(P0-1 이 킬스위치·감사만 추가).
- **F-B2-4** (high) **그러나 attach 에서 이 경로는 현재 도달 불가다.** `bot._exit_pending_symbols`/`_exit_pending_timestamps` 에 **추가**하는 코드는 전부 `_check_exit_signal`(1008~1211) 안에 있고, P0-3 이 그 함수를 attach 에서 조기 return 시켰다. 따라서 attach 에서 두 장부는 비어 있고 `_cleanup_stale_pending` 은 :924 에서 즉시 return 한다.
  - 추가 지점은 src/schedulers/kr_scheduler.py:1127-1128, :1170-1171 두 곳뿐(리포지토리 전역 grep: 나머지 `_exit_pending_*` 참조는 discard/pop/clear 이거나 읽기 — :1196-1197, :1284, :1375-1399, :2889-2890, :5395-5396, scripts/run_trader.py:196-197 초기화). attach 조기 return 은 :1026 `if getattr(getattr(bot, 'engine', None), '_execution_runtime', None) is not None: ... return`. 조기 return 앞에 pending 추가 없음.
- **F-B2-5** (high) 그 안전은 **설계가 아니라 '입력이 비어서'** 다. `_cleanup_stale_pending` 에는 `_check_exit_signal`(1043) 말고 **독립 주기 호출자**가 하나 더 있고, 그 호출자에는 attach 가드가 없다 — 60초마다 세션이 CLOSED 가 아니면 무조건 부른다.
  - src/schedulers/kr_scheduler.py:5137-5150 `async def run_pending_cleanup(self): ... while bot.running: session = self._get_current_session(); if session != MarketSession.CLOSED: await self._cleanup_stale_pending() ... await asyncio.sleep(60)`. 이 블록에 `_execution_runtime` 없음.
- **F-B2-6** (high) **main 의 본문은 09-21 밤에 크게 달라졌다(PR #81/#83).** 취소 뒤 `_keep_exit_pending_after_failed_cancel`(거래소 실조회 기반 소멸/생존/판단 불가 3분류 + 연속 2회 상한 + pending 당 1회 텔레그램 경보 + stage 롤백 없는 해제), `_exempt_sell_still_open`(면제 종목), 그리고 await 사이 pending 세대(`_gen`) 재검증이 들어갔다. engine 브랜치 본문에는 이 셋이 **전부 없다**.
  - main: /tmp 사본 기준 `git show origin/main:src/schedulers/kr_scheduler.py` — :836-850 `_exempt_sell_still_open`, :885 `_gen = bot._exit_pending_timestamps.get(s)`, :895-896 `if await self._keep_exit_pending_after_failed_cancel(s, now_time, _gen): continue`, :913-916(면제 유지), :917-921(세대 교체 시 해제 건너뜀), :983-1043(`_keep_exit_pending_after_failed_cancel` 본체 — :1006 `live = await rm.stale_order_still_live(s, OrderSide.SELL, confirm=waited)`, :1017-1018 `if live is False: return False`, :1019-1031 `unknown_streak >= self._MAX_EXIT_UNKNOWN` 시 stage 롤백 없이 해제). engine: src/schedulers/kr_scheduler.py:916-1006 에 해당 심볼 0건.
- **F-B2-7** (high) main 의 잔여 결함은 **전량 청산 pending 에 대해서는 여전히 '0 = 소멸'로 읽는다**는 것 — 의도된 결정이며, 근거는 "재발행돼도 KIS 가 주문가능수량 초과로 거절한다"는 D1 의 바로 그 미확인 가정이다.
  - main /tmp 사본 :987-990 docstring "분할 매도로 발행한 pending … 에만 적용한다. 전량 청산(손절·트레일링·EOD, 수량상 전량인 익절)은 재발행돼도 KIS 가 주문가능수량 초과로 거절하므로 종전대로 즉시 해제해 손절 재판단을 늦추지 않는다." / :1000 `if rm is None or pending_ts is None or self._partial_exit_marks().get(s) != pending_ts: return False`. 가정의 미확인 근거는 docs/superpowers/plans/2026-09-22-kis-judgement-decisions.md:46(D1 처분 ③)·:115.
- **F-B2-8** (high) attach 에서 '조기 return'(P0-3 의 세 writer 방식)을 택하면 **사라지는 것은 legacy stale pending 정리 자체**인데, F-B2-4 대로 attach 에는 그 장부에 들어오는 행이 애초에 없다. 즉 기능 손실 0, 얻는 것은 "입력이 비어 있다"는 우연에 기대지 않는 명시 계약이다.
  - F-B2-4(추가 지점 2곳이 모두 attach-skip 함수 안) + F-B2-5(가드 없는 독립 호출자 존재). P0-3 의 같은 방식: src/schedulers/kr_scheduler.py:1026(`_check_exit_signal`), :1919(monitor 계열), 및 docs/superpowers/plans/2026-09-22-p0-3-wiring-sync-gate.md:54 "하지 않는 것: … `_cleanup_stale_pending`(P0-4)".
- **F-B2-9** (high) owner 쪽 대응물은 **없다**. 세션 경계 소멸에 해당하는 `final_expired` 는 reconcile 이 만들지 않고(F-B1-8), 미해결 attempt 는 저절로 사라지지 않으며 일자 전환을 막는다(F-B1-9). attach 에는 미체결 주문의 타임아웃 취소 자체가 없다.
  - src/execution/safety/lifecycle.py:560-562, src/execution/safety/evidence.py:279, src/execution/safety/day_recovery.py:127-133. attach 에 취소 없음: docs/superpowers/plans/2026-09-22-kis-judgement-decisions.md:47(D2).
- **F-B3-1** (high) **owner 에 CANCEL '명령 경로'는 완성돼 있다** — 요청 빌더(`prepare_cancel`, TR `TTTC0803U`, `RVSE_CNCL_DVSN_CD='02'`·`QTY_ALL_ORD_YN='Y'`), 게이트(`_parent`), 전송(`EV_CANCEL` 감사행), 수명주기(`cancel_requested`/`RECONCILING`)가 모두 있다.
  - src/execution/safety/requests.py:270-293(`prepare_cancel` … body `'RVSE_CNCL_DVSN_CD': '02', 'ORD_QTY': '0', 'ORD_UNPR': '0', 'QTY_ALL_ORD_YN': 'Y'`, `tr_id='TTTC0803U'`), src/execution/safety/commands.py:329-360(`_parent`), :396·:412(CANCEL 분기), src/execution/safety/transport.py:188·196, src/execution/safety/lifecycle.py:362·:499.
- **F-B3-2** (high) **제품 호출자는 0건이다** — `prepare_cancel` 을 부르는 것은 `requests.py` 자신의 검증기와 시험 9개 파일뿐이다. 게이트웨이·엔진·스케줄러 어디에도 CANCEL 발행 지점이 없다.
  - repo 전역 grep `prepare_cancel`: src/execution/safety/requests.py:270(정의), :339(`validate` 안의 자기 대조), 나머지 전부 tests/(test_execution_owner_gate_authority.py:1004, test_execution_child_command_close.py:239, test_execution_dispatch_reasons.py:245·272·444, test_execution_command_owner.py:368, test_execution_decision_facts.py:140·362, test_execution_market_source.py:459, test_execution_resources.py:173, test_execution_requests.py:58).
- **F-B3-3** (high) CANCEL 을 실제로 쓰려면 owner 가 요구하는 전제가 무겁다: 부모 submit 이 비terminal·`evidence_conflict` 없음·`order_ref` 일치이고 **취소 수량이 현재 예약 잔량과 정확히 같아야** 하며(`current['reserved_quantity'] == parent.remaining_quantity == request.quantity`), `quantity − applied == remaining_quantity`, `observed == applied` 여야 한다. 부분 체결 뒤라면 취소 요청 수량은 자동으로 '줄어든 잔량'이 된다 — F-B1-2 덕분에 이 등식이 성립한다.
  - src/execution/safety/commands.py:337-343(`_require(current['version'] == parent.version and current['kind'] == 'submit' and current['state'] not in TERMINAL_STATES and not current.get('evidence_conflict') … and current['reserved_quantity'] == parent.remaining_quantity == request.quantity and current['quantity'] - current['applied_quantity'] == parent.remaining_quantity and current['observed_quantity'] == current['applied_quantity'], 'cancel_parent_changed')`), :344-358(자원 예약 단조 검사).
- **F-B3-4** (high) **따라서 P0-4 에서 `_cleanup_stale_pending` 을 owner CANCEL 로 '잇는' 것은 범위가 아니다.** 잇는 순간 필요한 것은 (a) 취소 최종성의 판정 계약 — Q7~Q12 에서 "알 수 없다"로 확정했고, (b) 취소 뒤 소멸/생존/판단 불가 3분류 — main 이 PR #81 에서 만든 것, (c) 그것을 attach 에서 재구성. 이 셋은 결정 문서가 P1(D2)로 묶어 둔 덩어리다.
  - docs/superpowers/plans/2026-09-22-kis-judgement-decisions.md:32(Q7~Q12 "취소 최종성은 알 수 없다고 확정한다"), :47(D2 "목표는 어젯밤 main 이 도달한 수준과의 동등 … 그 설계는 §5 의 전제 단계(P0) **뒤**"), :90(P1 "보호 SELL 의 main 동등(D2 의 방향 결정)"). main 의 3분류 실물: F-B2-6 인용.
- **F-B3-5** (high) P0-4 에서 `_cleanup_stale_pending` 에 대해 할 수 있는 것은 **attach 인지(조기 return) 한 줄**이고, 그것이 '정리 안 함'을 뜻해도 attach 에서는 잃을 정리가 없다(F-B2-4). 단, 같은 P0-4 에 **`run_pending_cleanup` 의 주기 호출도 같은 가드 뒤에 있어야** 한다 — 안 그러면 가드는 `_check_exit_signal` 경로에만 걸린다.
  - F-B2-4·F-B2-5·F-B2-8 인용. P0-4 범위 선언: docs/superpowers/plans/2026-09-22-kis-judgement-decisions.md:89 "P0-4 | `_protection_failed` 의 해제 경로 · 부분 체결 뒤 예약 감소 · 세션 경계 소멸 · `_cleanup_stale_pending` 의 attach 인지".
- **F-B3-6** (high) 부수 사실(Q-10 확인): `gateway.unresolved_symbols()` 는 side 를 거르지 않아, 미해결 SELL 하나가 그 종목을 교체(eviction) 후보에서도 영구 제외한다.
  - src/execution/safety/gateway.py:116-118 `return frozenset(fact.symbol for fact in self._pending())`(side 필터 없음), :144-149(`_pending` = `_snapshot(state).pending`), src/execution/safety/policy_snapshot.py:143-150(terminal 이고 예약 0 인 것만 제외). 소비자: src/core/engine.py:1677 `_owner_pending = None if _gateway is None else _gateway.unresolved_symbols()`, :1693 `_pending_ref = self._pending_orders if _owner_pending is None else _owner_pending`, :1697 `if sym in _pending_ref: continue`.
- **F-B4-1** (high) **부분 체결 뒤 예약 감소는 시험이 이미 촘촘히 고정하고 있다.** 40/60 분할 체결의 예약 잔량·현금·자원 해제 상한과 과다해제/증가 거부가 모두 RED 로 잡힌다.
  - tests/test_execution_fill_application.py::test_fill_matching_attempt_updates_and_trusted_mutate_remain_supported(:187, 내부 :193 `reserved_quantity=100 - obs.cumulative_quantity`, :203 `== 0`), ::test_attempt_write_set_boundary_preserves_checkpoint(:211, :223-225 `attempt.update(applied_quantity=40, reserved_quantity=60, reserved_cash="600")` / `reservation_quantity_overrelease` → 59 / `reservation_increase` → 101), tests/test_execution_economics.py::test_buy_and_sell_40_plus_60_cash_fees_pnl_and_scoped_count(:103, :116 `reserved_quantity == 0`), tests/test_execution_resource_release.py::test_real_queue_partial_full_duplicate_and_reopen_resource_conservation(:31), ::test_fill_write_set_rejects_excessive_or_unmeasured_resource_release(:87), ::test_remaining_resource_cannot_pass_day_quiescence_or_initial_r(:139).
- **F-B4-2** (high) **고정되지 않은 갈래: '부분 체결된 SELL 이 남은 상태에서 같은 종목에 새 보호 SELL' 이라는 시퀀스.** `unresolved_symbol_attempt` 를 고정하는 시험 3건은 전부 BUY→BUY 또는 다른 종목 우회이고, SELL→SELL 도 `reserved_quantity_insufficient` 도 표본이 없다.
  - tests/test_execution_signal_gateway_wiring.py:313-323(같은 종목 BUY 재시도 → `assert 'unresolved_symbol_attempt' in errors[0].message`), :395(합성 예외 문자열), tests/test_execution_signal_gateway_eviction.py:337, tests/test_execution_signal_gateway_acceptance.py:761(주석 — "owner 가 `unresolved_symbol_attempt` 로 먼저 막으므로 다른 종목을 쓴다"). 리포지토리 전역 grep `reserved_quantity_insufficient`: src/execution/safety/commands.py:426 한 곳뿐, tests/ 히트 0건.
- **F-B4-3** (high) **`_cleanup_stale_pending` 은 어떤 시험도 행동을 고정하지 않는다.** 언급 2건은 (a) 다른 시험의 docstring 경고, (b) 무관 시험에서의 no-op 스텁이다. S4-0 특성화 파일은 `engine.py` 의 세 루프만 고정하고 이 함수는 건드리지 않는다.
  - repo grep `cleanup_stale_pending` in tests/: tests/test_execution_signal_gateway_acceptance.py:1119(docstring "`KRScheduler._cleanup_stale_pending` 은 attach 를 모른 채 취소를 낸다(10C)"), tests/test_toss_money_path_invariance.py:65 `scheduler._cleanup_stale_pending = noop`. S4-0: tests/test_engine_legacy_stale_eviction_characterization.py:1 docstring "runtime 없는 legacy 엔진의 세 경로(90초 SELL 폴백·10분 BUY 정리·eviction) 특성화" — 시험 이름 전수(:127~:378)에 kr_scheduler 경로 없음.
- **F-B4-4** (high) 다만 S4-0 은 **같은 종류의 결함을 engine.py 쪽에서 이미 고정해 두었다** — "취소 0건과 취소 ACK 를 똑같이 최종성으로 읽는다"가 명시적 RED/특성화로 박혀 있어, 항목 9 를 고칠 때 참고할 기존 계약이 있다.
  - tests/test_engine_legacy_stale_eviction_characterization.py:273-275 `def test_a_stale_buy_is_released_whether_the_cancel_matched_anything_or_not(...)` docstring "2026-08-04 P1 의 의도된 완화 — 취소 0건과 취소 ACK 를 똑같이 최종성으로 읽는다.", :305-306 `test_without_a_usable_cancel_method_the_stale_buy_is_released_anyway` "취소를 한 번도 보내지 않아도 해제한다.", :290 `test_a_cancel_exception_keeps_the_stale_buy_pending`.

**callers**

- src/execution/safety/economics.py:342 `reduce_economics` — 유일한 예약 감소 지점. 호출자는 src/execution/safety/runtime.py:527 `_reduce` 하나(→ `apply_execution_observation`)
- src/execution/safety/application.py:297 `_validate_attempt_write_set` — 위 감소를 검증하는 유일한 관문(허용 필드 5개, 과다/증가 거부)
- src/execution/safety/lifecycle.py:234 `_release_resources` — 호출자 3곳: :417(abandon_candidate), :491(record_result 의 NOT_SENT/REJECTED), :571(reconcile 의 최종성 + applied==observed)
- src/execution/safety/commands.py:363 `_evaluate` — 게이트 본체. 호출자 2곳: :461(`_prepare` 의 reduce), :531(`_bound`, `exclude_attempt=` 로 자기 자신 제외). `held − reserved` 검사는 :426, 그보다 앞선 종목 잠금은 :394
- src/schedulers/kr_scheduler.py:916 `_cleanup_stale_pending` — 호출자 2곳: :1043(`_check_exit_signal` 안 — 이 함수는 :1026 에서 attach 조기 return), :5145(`run_pending_cleanup` 60초 주기 — **attach 가드 없음**)
- src/execution/broker/kis_kr.py:944 `cancel_all_for_symbol` — `self._pending_orders`(인메모리)만 순회. attach 는 이 딕셔너리를 채우지 않는다
- src/execution/safety/requests.py:270 `prepare_cancel` — 제품 호출자 0건(자기 `validate`:339 + tests/ 9파일)
- src/execution/safety/gateway.py:116 `unresolved_symbols` — 제품 호출자 1건: src/core/engine.py:1677(교체 후보 제외). side 필터 없음
- src/execution/safety/evidence.py:279 — `FINAL_FILLED` 또는 `RECONCILING` 만 생산. `FINAL_CANCELLED`/`FINAL_EXPIRED` 생산자 0건
- src/execution/safety/day_recovery.py:121-134 `unresolved_reason` — 호출자 src/execution/safety/runtime.py:208 `_quiescence_reason` → :281-283 일자 전환 BLOCKED

**main_vs_attach**

## 항목 4(부분 체결 뒤 예약)
- **attach(engine 브랜치):** 예약 산술은 **정상이다.** `reserved_quantity` 는 체결마다 `max(0, reserved − delta.quantity)` 로 줄고(economics.py:467,480) application 계층이 그 범위를 강제한다(application.py:333). `held − reserved` 는 음수가 되지 않는다. 결정 문서 §4-2 항목 4 의 메커니즘 서술("terminal 전까지 줄지 않는다", "held − reserved < 0")은 **코드와 어긋나므로 정정이 필요하다.**
- 실제 봉쇄 원인은 둘이고 서로 다르다:
  1. **즉시형** — `commands.py:394` 의 종목 단위 잠금(`unresolved_symbol_attempt`). side 를 보지 않아 같은 종목의 어떤 새 SUBMIT 도 막는다. 여기를 열어도 잔여 60 은 `reserved_quantity_insufficient`(:426)에서 정확히 0 >= 60 으로 다시 막힌다 — **원 주문이 거래소에 살아 있는 동안에는 그게 옳다.** 고칠 것은 예약이 아니라 취소(P1/D2).
  2. **지속형** — 부분 체결 주문이 장 마감으로 소멸해도 owner 는 배울 수 없다. 파서는 `FINAL_FILLED`/`RECONCILING` 만 만들고(evidence.py:279) reconcile 은 `FINAL_EXPIRED` 를 명시 미지원(lifecycle.py:560-562). attempt 는 `partial` 로 영구히 남아 그 종목을 잠그고, **일자 전환까지 BLOCKED** 로 만든다(day_recovery.py:127-133 → runtime.py:208,281-283). **이것이 P0-4 가 다뤄야 할 항목 4 의 진짜 내용이다.**
- **main(legacy):** 이 문제 자체가 없다 — 예약이라는 개념이 없고, 미체결 SELL 은 90초 폴백이 취소 후 시장가로 재주문한다(S4-0 특성화 :127-181 이 그 동작을 고정). attach 가 legacy 보다 약한 것이 맞다.
## 항목 9(`_cleanup_stale_pending`)
- **main:** 09-21 밤 PR #81/#83 으로 **이미 크게 고쳐졌다.** 취소 뒤 `_keep_exit_pending_after_failed_cancel`(main :983-1043)이 거래소 실조회(`rm.stale_order_still_live`)로 소멸/생존/판단 불가를 가르고, 생존이면 상한 없이 유지+텔레그램 경보, 판단 불가 연속 2회면 **stage 롤백 없이** pending 만 해제한다. `_exempt_sell_still_open`(:836-850)과 await 사이 pending 세대 재검증(:917-921)도 추가됐다. 즉 결정 문서 §6 의 "같은 결함이 운영에도 있으니 main 에서 먼저 고친다"는 **분할 청산에 한해 이미 완료**다. main 에 남은 것은 **전량 청산 pending 의 '취소 0건 = 소멸' 해석**이고, 그것은 의도된 결정이며 근거가 D1 의 미확인 가정("KIS 가 주문가능수량 초과로 거절한다", main :988-989)이다 — D10 기록기로 검증할 항목과 같은 줄에 있다.
- **attach(engine 브랜치):** 본문은 **PR #81 이전 판**이다(위 셋 전부 없음, engine :916-1006). 그리고 `_execution_runtime` 검사가 0건이라 문서가 적은 오판 시나리오(빈 캐시 → 0 → 해제 + `rollback_stage`)는 **코드상 그대로 성립한다.**
- **다만 현재는 도달 불가다.** `_exit_pending_*` 에 **추가**하는 두 지점(:1127-1128, :1170-1171)이 모두 `_check_exit_signal` 안에 있고 그 함수는 attach 에서 :1026 조기 return 한다 → 장부가 비어 :924 에서 즉시 return. **이 안전은 설계가 아니라 우연**이고, `run_pending_cleanup`(:5137-5150)이 가드 없이 60초마다 독립 호출한다.
## engine 브랜치가 main 을 들이는 날
engine 의 `kr_scheduler.py` 는 PR #81/#83 이전 본문이므로, main 병합 시 `_cleanup_stale_pending` 은 **본문이 통째로 교체된다**(main 쪽 ~150줄). P0-4 에서 여기에 attach 가드를 넣는다면, 그 가드는 병합 뒤 main 본문 위에 다시 얹혀야 한다 — 지금 engine 본문에 맞춰 세밀하게 짜 넣는 작업은 병합 때 버려진다. **가드는 함수 맨 앞 조기 return 한 줄 + `run_pending_cleanup` 쪽 한 줄로 최소화하는 편이 병합 비용상 유리하다.**

**open_questions**

- 결정 문서 §4-2 항목 4 의 메커니즘 서술이 코드와 어긋난다(F-B1-2·F-B1-4). 문서를 정정할 것인가, 아니면 항목 4 를 '세션 경계/만료 계약 부재로 부분 체결 주문이 영구 partial'(F-B1-8·F-B1-9)로 재정의할 것인가 — P0-4 착수 전에 coordinator 가 정해야 범위가 흔들리지 않는다.
- 항목 4 의 지속형을 P0-4 에서 열려면 `FINAL_EXPIRED` 계약이 필요한데, Q19~Q21 에서 '공통 cutoff·기준시각·지연 상한 없음'으로 확정했다. '장 마감 뒤 일별체결조회에서 remaining>0 이고 cancelled==0 인 행'을 만료로 읽는 것이 fail-open 인지(취소 최종성과 같은 종류의 미확인) 판단이 필요하다 — 코드로는 답이 없고 D10 기록기 영역이다.
- attach 의 `partial` 영구 잔존이 일자 전환을 BLOCKED 로 만드는데(F-B1-9), D5 의 '사람이 state 를 처분하는 운영 절차(runbook)'가 이 경우도 덮는가? D5 는 UNKNOWN 해제를 위해 쓰였고, partial 잔존은 별개 상태다. runbook 범위 확인 필요.
- 항목 9 의 attach 가드를 P0-4 에 넣을 때 `run_pending_cleanup`(:5145)도 같이 막을 것인가 — 막지 않으면 가드가 `_check_exit_signal` 경로에만 걸려 '독립 주기'라는 이름값(price event 없이도 돈다)이 그대로 구멍으로 남는다. 다만 현재는 장부가 비어 무해하므로 '명시 계약'의 가치만 있다.
- main 의 전량 청산 pending 이 여전히 '취소 0건 = 소멸'로 해제되는 것(F-B2-7)을 main 에서 추가로 고칠 것인가. 근거인 D1 가정은 저장소에 없고 §7 이 '가장 먼저 검증할 항목'으로 적었다. live 파일·배포이므로 사용자 확인 영역.
- F-B4-2 의 미고정 갈래(부분 체결 SELL 위의 새 보호 SELL)를 P0-4 의 RED 로 등록할지, P1(F8 ①·late-fill)의 RED 표본과 묶을지. P0-3 Q-1 이 이미 BUY 쪽 표본(BUY 100/40 → SELL 40 → 잔여 60)을 P1 RED 로 등록했으므로 같은 자리에 붙이는 편이 자연스럽다.

#### 조사 B(예약·stale pending)

- **F-A1-1** (높음) `_protection_failed` 는 True 로만 간다 — 프로세스 안에 False 로 되돌리는 코드가 한 줄도 없다. 초기화(False) 1곳, True 대입 2곳이 전부다.
  - src/execution/safety/runtime.py:96 (`self._protection_failed = False`, __init__ 안), runtime.py:1308, runtime.py:1343. `grep -rn "_protection_failed" --include=*.py .` 결과 src/ 안의 대입은 이 셋뿐이고 `= False` 는 96 하나다.
- **F-A1-2** (높음) 래치 setter ①(가격 보호): `quote()` 가 만든 `apply_quote` task 의 done-callback 이 `done.cancelled() or (done.exception() is not None and not admission_rejected)` 이면 래치한다. 즉 durable 접수(`owner.mutate`)나 보호 적용 commit 이 저장·게시 예외로 실패하면 전부 래치다. 면제는 `admission_rejected` 하나뿐이고 그것은 `admit` reducer 안에서 `_require_quote_freshness` 가 ApplicationBlocked 를 낸 경우(=stale quote 거부)와 진입 관측 경로의 접수 전 거부에서만 True 가 된다.
  - src/execution/safety/runtime.py:1302-1311 (task 생성·`completed` callback), runtime.py:1201 (`admission_rejected = False`), runtime.py:1204-1210 (`admit` 안의 면제 설정), runtime.py:1268-1272 (`apply_quote` 진입 경로의 `admission_rejected = True`).
- **F-A1-3** (높음) 래치 setter ②(보호 복구): `repair_protection` 의 done-callback 은 면제가 **없다** — `done.cancelled() or done.exception() is not None` 이면 무조건 래치한다. 다만 `reduce_repair` 는 통상적인 도메인 거부(protection_not_degraded·stale_execution_version 등)를 BLOCKED receipt 로 흡수하므로 실제 래치 경로는 (a) `recovery_operation_id_conflict`, (b) store/게시 실패, (c) task 취소 셋이다.
  - src/execution/safety/runtime.py:1338-1345 (면제 없는 callback), src/execution/safety/protection_recovery.py:493 (`raise ValueError("recovery_operation_id_conflict")`), protection_recovery.py:515-518 (그 밖의 ValueError/KeyError/TypeError 는 BLOCKED receipt 로 흡수).
- **F-A1-4** (높음) 보호 task 집합 `_protection_tasks` 의 구성원은 세 종류다 — `quote()` 의 `apply_quote`, `repair_protection()` 의 `apply_repair`, `finalize_initial_r()` 의 `execute`. 세 번째는 같은 집합에 들어가지만 래치하지 않고 예외만 소비한다.
  - src/execution/safety/runtime.py:1303, 1339, 1372 (세 add 지점), runtime.py:1373-1375 (`finalize_initial_r` 의 callback 은 `done.exception()` 을 읽기만 한다).
- **F-A1-5** (높음) 읽는 곳 ① — 명령 경로: `market_source_pending()` 이 `_protection_failed` 를 OR 로 물고, `_evaluate` 가 `request.command is CommandKind.SUBMIT` 일 때만 그것을 거부 조건으로 쓴다. 따라서 래치가 서면 **SUBMIT BUY 와 SUBMIT SELL(보호 매도)이 함께 영구 거부**되고 **CANCEL 은 거부되지 않는다**.
  - src/execution/safety/runtime.py:463-466 (`return (any(not task.done() ...) or bool(state.get("protection_quote_admissions")) or self._protection_failed)`), src/execution/safety/commands.py:362-363 (`if request.command is CommandKind.SUBMIT: _require(not self.runtime.market_source_pending(state), 'market_source_pending')`).
- **F-A1-6** (높음) 읽는 곳 ② — 일자 전환: `_quiescence_reason` 이 래치를 `"protection_update_failed"` 로 돌려주고, 그 값이 `prepare_day_transition`·`rollover_day`·`resume_after_rollover` 세 reducer 를 모두 BLOCKED 로 만든다. 게다가 `_day_closed = False` 로 되돌리는 유일한 줄이 `not self._quiescence_reason(...)` 조건 뒤에 있어, 래치가 서면 일자 전환이 완료될 수 없고 `_day_closed` 도 영원히 True 다.
  - src/execution/safety/runtime.py:205-206 (`if self._protection_failed: return "protection_update_failed"`), runtime.py:281 (prepare), runtime.py:313 (rollover), runtime.py:341 (resume reducer), runtime.py:349-350 (`if receipt.status == "APPLIED" and not self._quiescence_reason(self.owner.state): self._day_closed = False`).
- **F-A1-7** (중간 — 코드상 결론은 명확하지만 실제 일자 전환 구동기가 제품에 없어(F-A1-10) 런타임으로 재현한 적은 없다) 그래서 래치는 단계적으로 커진다 — 당일에는 SUBMIT(보호 SELL 포함)만 막지만, 다음 일자 경계를 넘는 순간 `day_admission_closed` 가 True 로 고정되고 `_require_day_admission` 이 **CANCEL·quote·repair 를 포함한 모든 명령**을 `day_transition_admission_closed` 로 거부한다(`risk.day != 오늘` 항만으로도 True 가 된다).
  - src/execution/safety/runtime.py:147-155 (`day_admission_closed` — `self._day_closed or ... or state.get("risk", {}).get("day") != self._now().date().isoformat()`), runtime.py:157-159 (`_require_day_admission`), commands.py:104 (`self.runtime._require_day_admission()` 이 `_owner_ready` 첫머리 — 모든 명령의 공통 경로).
- **F-A1-8** (높음) 한 종목의 실패가 전 종목을 멈추는 이유는 판정 자체가 종목을 모르기 때문이다 — `market_source_pending(state)` 는 symbol 인자를 받지 않고 프로세스 단위 bool 과 프로세스 단위 task 집합만 본다. 거부 사유 문자열도 한 낱말이라 어느 종목·어느 실패였는지가 명령 결과에 남지 않는다.
  - src/execution/safety/runtime.py:463-466 (시그니처 `def market_source_pending(self, state) -> bool`), commands.py:363 (사유 `'market_source_pending'`).
- **F-A1-9** (높음) `health()` 에는 `protection_updates_failed` 로 노출된다. 그러나 `health()` 의 제품 호출자는 0건이라 운영에서 이 래치는 **아무 화면에도 뜨지 않는다**.
  - src/execution/safety/runtime.py:1469 (`def health`), runtime.py:1476 (`"protection_updates_failed": self._protection_failed`). `grep -rn "\.health()" --include=*.py src/` 결과 0건.
- **F-A1-10** (높음) `restore()`·`attach()`·reconciler·fill 적용 경로는 래치를 건드리지 않는다. 재시작(=새 `KRExecutionRuntime` 인스턴스)만이 bool 을 지우는데, 그것도 완전한 해제가 아니다 — 접수가 durable 하게 commit 된 뒤 실패한 경우 `protection_quote_admissions` 행이 저장본에 남아 재시작 후에도 `market_source_pending` 이 그대로 True 다.
  - src/execution/safety/runtime.py:137-146 (`restore()` — `_protection_failed` 미언급), runtime.py:353-362 (`attach()`), runtime.py:465 (두 번째 OR 항 `bool(state.get("protection_quote_admissions"))`), tests/test_execution_market_source.py:388-399 (fresh runtime 복원 뒤 `assert fresh.market_source_pending(state)`).
- **F-A1-11** (높음) 오늘 이 래치는 **제품에서 도달 불가**다 — `runtime.quote`·`repair_protection` 의 제품 호출자가 0건이고, legacy 가격 writer 는 attach 에서 ApplicationBlocked 로 막혀 있다. 래치가 실제 위험이 되는 시점은 P1 이 owner quote 경로를 배선하는 그 순간이다(스케줄러 주석이 그 배선을 P1 의 일로 명시한다).
  - `grep -rn "\.quote(\|repair_protection" --include=*.py src/` 결과 runtime.py 자신과 tests 외 0건; src/core/engine.py:945-947 (`update_position_price` 가 attach 에서 `ApplicationBlocked("가격 보호 변경은 execution.quote로 직렬화해야 합니다")`), src/schedulers/kr_scheduler.py:1018-1025 ("owner 의 보호 경로(`runtime.quote`/`apply_quote`)는 존재하지만 제품에 시세를 넣는 생산자가 0건" + "닫는 것은 P1").
- **F-A2-1** (높음) 세션 이름·경계는 `_session_at(now)` 한 함수가 정한다: 08:00–08:50 `pre_market`, 08:50–09:00 `pre_close`, 09:00–15:20 `regular`, 15:20–15:30 `closing`, 15:30–15:40 `break`, 15:40–20:00 `next_market`, 그 밖 `closed`. 즉 경계는 08:00·08:50·09:00·**15:20**·**15:30**·15:40·**20:00** 일곱 개다.
  - src/execution/safety/requests.py:64-72.
- **F-A2-2** (높음) 세션은 요청 DTO 생성 시점에 한 번 고정된다 — `RequestSession.__post_init__` 이 `session == _session_at(observed_at)` 을 강제하고, gateway 가 `runtime._now()` 한 번으로 date·observed_at·session 을 같이 만든다.
  - src/execution/safety/requests.py:105-108 (`if type(self.session) is not str or self.session != _session_at(now): raise RequestValidationError('request_session_mismatch')`), src/execution/safety/gateway.py:54 (`now = self.runtime._now()`), gateway.py:161 (`session=RequestSession(now.date().isoformat(), now, _session_at(now))`).
- **F-A2-3** (높음) `request_session_changed` 는 `RequestBoundCommands._session` 에서 판정되며, 시각 출처는 `self.runtime._now()`(= 주입 clock 을 KST 로 변환)다. 이 함수는 `_request(session=True)` 로 **prepare 때** 한 번, 그리고 `_evaluate` 안에서 **prepare·dispatch 양쪽** 모두 다시 불린다.
  - src/execution/safety/commands.py:132-141 (`_session`, 133 `now = self.runtime._now()`, 136 `_require(request.session.session == _session_at(now), 'request_session_changed')`), commands.py:120-131 (`_request(..., session=True)`), commands.py:364 (`self._session(request)` — `_evaluate` 안), src/execution/safety/runtime.py:131-135 (`_now` — `clock()` → `astimezone(Asia/Seoul)`).
- **F-A2-4** (높음) dispatch 는 `_request(..., session=False)` 로 들어가지만 세션 검사를 건너뛰지 않는다 — `_bound` → `_evaluate` → `_session` 이 **dispatch 시점 시계**로 prepare 때 구운 세션 문자열을 다시 대조한다. 주석도 그렇게 설계됐다고 명시한다.
  - src/execution/safety/commands.py:610-614 (주석 "세션은 여기서 보지 않는다 — prepare 뒤에 장 경계가 닫힌 요청은 아래 `_bound`→`_evaluate` 의 재검사에서 걸려…" + `self._request(request, context, session=False)`), commands.py:503-504 (`_bound` → `_evaluate` 호출), commands.py:364.
- **F-A2-5** (높음) NOT_SENT 뒤 attempt 의 종료 상태: `CommandValidationError('request_session_changed')` → `_unsent` → `lifecycle.abandon_candidate` 가 한 transaction 으로 attempt 를 `final_rejected` / `command_status='not_sent'` / `reason_code='request_session_changed'` 로 닫고 예약(수량·현금·노출·계획위험)을 전부 푼다. 반환은 `CommandResult(NOT_SENT, attempt_id, reason_code=...)` 다.
  - src/execution/safety/commands.py:631-632 (`except CommandValidationError as exc: return await self._unsent(request, exc.reason)`), commands.py:579-600 (`_unsent`), src/execution/safety/lifecycle.py:371-416 (`abandon_candidate` — 412-415 에서 FINAL_REJECTED/NOT_SENT/reason_code 대입), tests/test_execution_dispatch_reasons.py:428-430 (같은 종료 상태를 고정한 기존 시험).
- **F-A2-6** (높음) gateway 는 그 NOT_SENT 를 그대로 반환할 뿐 재시도도 재예약도 하지 않는다(계약 6: 같은 attempt 로 재시도 금지). engine 의 `_submit_signal` 은 그것을 **로그 한 줄로만** 남긴다 — 이벤트 재발행도, ErrorEvent 도, pending 장부 기록도 없다. 이것이 "조용한 소멸"의 정확한 지점이다.
  - src/execution/safety/gateway.py:70-71 (`# 같은 attempt 로 재시도하지 않는다(계약 6).` + `return await self.commands.dispatch(...)`), src/core/engine.py:669-673 (`outcome = await ...gateway.submit(...)` → `logger.info(f"[엔진] 실행 송신 결과: {event.symbol} {detail}")`).
- **F-A2-7** (높음) 15:30–15:40(`break`)과 20:00 이후(`closed`)는 소멸의 **모양이 다르다** — 요청 객체를 만들 수조차 없다. `prepare_submit` 이 그 두 세션을 build 시점에 거부하므로 예외가 `gateway._bind` 에서 올라가 `gateway.submit` 밖으로 새고, engine 이 errors_count 를 올리며 ErrorEvent 를 낸다(CommandResult 가 아니다). 15:20–15:30(`closing`)에서는 시장가 SELL 도 같은 build 거부를 맞는다.
  - src/execution/safety/requests.py:243-246 (`if session.session in ('break','closed'): raise RequestValidationError('unsupported_submit_session')` / `if session.session in ('pre_close','closing') and order.order_type is OrderType.MARKET: raise ...`), src/execution/safety/gateway.py:159-162 (`_bind` 안에서 `prepare_submit` 호출), src/core/engine.py:675-684 (`except Exception` → errors_count + ErrorEvent).
- **F-A2-8** (높음) 세션 검사를 느슨하게 할 수 없는 구조적 이유: attach 는 **전선 본문을 prepare 시점에 굽는다**. 세션이 `ORD_DVSN`('05' 시간외 / '01' 시장가 / '00' 지정가)과 `AFHR_FLPR_YN` 을 결정하고, 그 body 와 `session` 필드가 통째로 `fingerprint` 에 들어간다(`asdict` 전체 해시). 반면 legacy 는 POST 직전에 세션을 읽어 본문을 만든다. 따라서 15:19 에 구운 `regular` 요청을 15:41 에 보내면 **본문이 그 세션에 대해 틀린 주문**이 된다.
  - src/execution/safety/requests.py:251-260 (`after_hours = session.session in ('pre_market','next_market')`, `division = '05' if after_hours else ('01' if MARKET else '00')`, `body['AFHR_FLPR_YN'] = 'Y'`), requests.py:199-202 + 231 (`fields = asdict(request)` → sha256 → fingerprint; `session` 은 DTO 필드 requests.py:161), src/execution/broker/kis_kr.py:508 (`session = self._get_current_market_session()` — POST 경로 안), kis_kr.py:556-559 (`if session in ("pre_market","next_market"): params["AFHR_FLPR_YN"]="Y"`).
- **F-A2-9** (높음) 같은 순간 legacy 의 행동: 브로커의 세션 맵이 `_session_at` 과 **경계가 똑같다**. `closing`(15:20–15:30)에서는 지정가를 접수하고 시장가만 거부하며, `break`·`closed` 에서는 거부한다. 즉 15:20 경계에서 legacy 의 보호 SELL(지정가)은 **그대로 나간다** — attach 만 죽는다.
  - src/execution/broker/kis_kr.py:652-678 (`_get_current_market_session` — 900≤t<1520 regular, 1520≤t<1530 closing, 1530≤t<1540 break, 1540≤t<2000 next_market), kis_kr.py:511-520 (`if session == "closed": return False,...` / `elif session == "break": return False,...` / `if session in ("pre_close","closing"): if order.order_type == OrderType.MARKET: return False,...`).
- **F-A2-10** (높음) legacy 는 보호 SELL 이 거절돼도 잃지 않는다 — `on_order` 가 실패 시 `clear_pending` 만 하고 **SELL 에는 일부러 쿨다운을 걸지 않으며**(주석이 "SELL 실패는 재시도가 필요하고"라고 명시), `_check_exit_signal` 이 다음 시세 틱에서 같은 청산 조건을 다시 감지해 SIGNAL 을 재발행한다. 즉 legacy 의 복구 수단은 재시도가 아니라 **재감지**다.
  - src/core/engine.py:2588-2594 (`logger.warning("[리스크] 주문 제출 실패…")` → `await self.clear_pending(event.symbol)` → `if order.side == OrderSide.BUY: self.block_symbol(...)` + 주석 2591-2592), src/schedulers/kr_scheduler.py:1156-1192 (`exit_result = bot.exit_manager.update_price(...)` → pending 등록 → `await bot.engine.emit(event)`), kr_scheduler.py:5003 (REST 피드 틱마다 `_check_exit_signal` 호출).
- **F-A2-11** (높음) legacy 의 15:20–15:30 특례도 명시적이다: 90초 미체결 SELL 의 시장가 에스컬레이션은 09:00–15:30 에만 돌고, 동시호가 구간에서는 시장가 전환을 포기하고 **지정가를 유지**한다(취소만 하고 재주문하지 않는 것이 아니라, 취소 뒤 재주문 단계에서 `continue` 한다).
  - src/core/engine.py:1996 (`is_regular_hours = 900 <= time_val < 1530`), engine.py:2001-2003 (`if is_sell and elapsed >= _SELL_TIMEOUT and is_regular_hours:`), engine.py:2029-2031 (`if 1520 <= time_val < 1530: logger.info("[리스크] 동시호가 시간대 시장가 불가: … → 지정가 유지"); continue`).
- **F-A2-12** (높음) 오늘 attach 에서 이 소멸의 피해자는 0건이다 — attach 에는 보호 SELL 생산자가 하나도 없다(차단 사유 21). `_check_exit_signal` 과 `batch_analyzer.monitor_positions` 가 attach 에서 통째로 return 하고, `prepare_cancel` 의 제품 호출자도 0건이다. 약한 지점 7 은 P1 이 보호 생산자를 배선하는 순간 실효가 된다.
  - src/schedulers/kr_scheduler.py:1020-1027 ("attach 에는 손절·트레일링·분할익절·갭EOD·보유기간 청산의 **구동기가 하나도 없다**" + 조기 return), src/core/batch_analyzer.py:1744-1757 (같은 skip), `grep -rn "prepare_cancel" --include=*.py src/` → requests.py 정의부 2줄뿐.
- **F-A2-13** (높음) `request_binding['session']` 은 단순 라벨이 아니라 **증거 provenance 키**다 — 대사기가 그것을 파서에 넘겨 조회 응답의 provenance 를 대조하고, 없으면 종결을 지어내지 않고 `missing_session` 으로 건너뛴다. 따라서 "세션만 갈아끼워 그대로 보낸다"는 완화는 본문(F-A2-8)뿐 아니라 증거 라벨까지 거짓으로 만든다.
  - src/execution/safety/runtime.py:883-896 (`session = ... binding.get("session")`, 885-890 `missing_session` skip, 891-896 `parse_order_evidence(..., session=session, query_scope=parser_scope(collection.scope, session=session))`), src/execution/safety/evidence.py:102-108 및 221 (provenance 대조에 session 포함).
- **F-A3-1** (높음) (a) 래치가 지키려는 불변식은 이미 **durable 하게 따로 존재한다**: "반쯤 게시된 시세가 있을 수 있다"의 증거는 `state['protection_quote_admissions']` 행이고, 그것은 저장본에 남아 재시작을 견디며 명령·종목 단위다. 실제로 그 행 하나만으로도 새 SUBMIT 은 막히고 더 높은 후속 가격이 덮어쓰지도 못한다. 즉 bool 래치는 같은 사실의 **더 거친 프로세스 단위 사본**이다.
  - src/execution/safety/runtime.py:465 (두 번째 OR 항), runtime.py:1211-1224 (`admit` 이 durable 행을 만든다), runtime.py:1261 (`del state["protection_quote_admissions"][command_id]` — 보호 결과와 같은 commit 에서만 제거), tests/test_execution_market_source.py:392-399 (재시작 뒤에도 행이 남아 pending, 후속 가격은 ApplicationBlocked).
- **F-A3-2** (중간 — 설계 제안이며 구현·심사 전이다) (a) 그러므로 안전한 해제 조건은 "래치를 지우자"가 아니라 "래치를 durable 증거로 환원하자"다. 충족해야 할 4가지: ① 실패 원인 task 가 실제로 끝났음(`not any(not t.done() for t in _protection_tasks)`) ② owner 가 건강하고 저장 version == 게시 version(이미 `_owner_ready`·`_quiescence_reason` 이 별도로 본다) ③ 해당 종목의 `protection_quote_admissions` 미해결 행이 없음 ④ owner 상태가 재게시됐음. ①②④ 는 이미 코드에 있는 검사라 실질적으로 남는 조건은 ③뿐이다.
  - src/execution/safety/runtime.py:196-197 (`if not self.owner.healthy or self.engine._execution_version != self.owner.version: return "publication_mismatch"`), runtime.py:204 (`if any(not task.done() for task in self._protection_tasks): return "protection_tasks_pending"`), commands.py:102-103 (`_require(self.owner.healthy and self.owner.version == self.owner.published_version, 'store_or_publication_unhealthy')`), runtime.py:465-466.
- **F-A3-3** (높음) (a) 해제를 만들 때 **반드시 같이 옮겨야 하는 것**은 일자 전환의 읽기다. `_quiescence_reason` 이 래치를 그대로 물고 있으면, 해제 API 를 만들어도 이미 래치가 선 채 일자 경계를 넘은 프로세스는 rollover 가 BLOCKED → `_day_closed` 영구 True → CANCEL 까지 포함한 전면 정지에 빠진다(F-A1-6·7). 해제가 없다면 **대신 보호하는 것은 아무것도 없다** — 오늘 안전한 이유는 setter 가 제품에서 도달 불가라는 우연뿐이다(F-A1-11).
  - src/execution/safety/runtime.py:205-206, 281, 313, 341, 349-350; F-A1-11 의 근거(제품 호출자 0건).
- **F-A3-4** (높음) (b) 세션이 바뀐 보호 SELL 을 **다음 세션용 명령으로 새로 준비하는 것은 owner 계약 안에서 가능하다** — 그리고 그것이 유일한 in-contract 경로다. NOT_SENT 로 닫힌 attempt 는 FINAL_REJECTED(=TERMINAL_STATES)라 `_evaluate` 의 `unresolved_symbol_attempt` 검사를 통과시키고, 예약도 이미 풀려 있다. gateway 는 (symbol, side, strategy) 당 intent_id 를 재사용하고 attempt_id 만 새로 발급하므로 같은 청산 의도를 유지한 채 새 attempt 를 만든다.
  - src/execution/safety/lifecycle.py:47-50 (`TERMINAL_STATES` 에 `FINAL_REJECTED` 포함), src/execution/safety/commands.py:389-395 (terminal attempt 는 `continue`, 비terminal 동종목만 `unresolved_symbol_attempt`), gateway.py:151-156 (`self._intents` 재사용 + `attempt_id='gw-a-'+uuid4().hex`), tests/test_execution_child_command_close.py:225 (`test_closing_a_child_unblocks_a_new_submit_for_the_symbol`).
- **F-A3-5** (높음) (b) 재준비가 자동으로 올바른 이유: prepare 가 세션을 본문(`ORD_DVSN`/`AFHR_FLPR_YN`)과 fingerprint 에 **굽기 때문에**, 새 세션에서 새로 prepare 하면 그 세션에 맞는 전선 본문과 새 fingerprint·새 증거 라벨이 함께 나온다. 반대로 dispatch 안에서 세션만 갈아끼우는 재시도는 본문·fingerprint·증거 라벨 셋 다 거짓이 되므로 금지되어야 한다.
  - F-A2-8·F-A2-13 의 근거(requests.py:251-260, 199-202, 231; runtime.py:883-896).
- **F-A3-6** (중간 — 설계 방향 제안이며 심사 전이다) (b) 재준비의 올바른 소유자는 gateway/dispatch 가 아니라 **생산자**다 — legacy 와 같은 모양(재시도가 아니라 재감지)이고, gateway 의 계약 6(같은 attempt 재시도 금지)과 충돌하지 않는다. 다만 `break`(15:30–15:40)·`closed`(20:00~)에서는 재준비 자체가 build 단계에서 불가능하므로(F-A2-7), 그 구간에서 안전한 것은 "버린다"가 아니라 "미충족 청산 의도를 살려 두고 다음 접수 가능 세션에 다시 준비한다"이다 — legacy 도 같은 구간에 보내지 못하지만 매 틱 재감지로 의도를 유지한다.
  - gateway.py:70 (계약 6), requests.py:243-244 (build 거부), src/core/engine.py:2588-2594 + kr_scheduler.py:5003 (legacy 의 재감지 루프), kis_kr.py:513-514 (legacy 도 break 에서는 못 보낸다).
- **F-A4-1** (높음) 고정된 것(래치): ① 래치가 서고 health 에 보이는 것 ② 실패 뒤 pending 카운터가 0 인 것 ③ stale quote 거부·접수 전 충돌은 **래치하지 않는다**는 면제 ④ pending(일시) 가지가 prepare/dispatch 를 막고 CANCEL 은 막지 않는다는 것 ⑤ durable 접수 행이 재시작을 견딘다는 것.
  - tests/test_execution_runtime.py:526 `test_failed_accepted_protection_command_is_visible_in_health`(535-536), tests/test_execution_day_recovery.py:145 `test_quote_waiting_for_admission_rechecks_newer_explicit_watermark`(assert 172), tests/test_execution_day_recovery.py:339 `test_explicit_stale_quote_is_rejected_without_price_or_protection_changes`(assert 361), tests/test_execution_market_source_review.py:69 `test_supplemental_ohlc_conflict_is_rejected_before_admission`(assert 78), tests/test_execution_market_source.py:20 `test_inflight_market_source_blocks_submit_until_protection_is_applied`, tests/test_execution_market_source.py:441 `test_market_pending_does_not_add_a_new_alpha_barrier_to_cancel`(471), tests/test_execution_market_source.py:349 `test_market_apply_failure_reopens_with_exact_pending_or_completed_evidence`(392-399).
- **F-A4-2** (높음) 고정되지 않은 갈래(래치) 5가지: ① 래치가 선 상태에서 **보호 SELL(SUBMIT/SELL)이 거부된다**는 단언이 어디에도 없다(기존 시험은 전부 pending 가지이거나 BUY 다) ② 래치가 `prepare_day_transition`/`rollover_day`/`resume_after_rollover` 를 BLOCKED 로 만들고 `_day_closed` 를 영구 True 로 남긴다는 단언이 없다 ③ `repair_protection` 쪽 setter(면제 없음)가 래치한다는 단언이 없다 ④ 프로세스 안에서 다시 False 가 되지 않는다는 단언이 없다(변이로 `= False` 한 줄을 넣어도 죽는 시험이 없다) ⑤ `finalize_initial_r` 가 래치하지 **않는다**는 대조 단언이 없다.
  - `grep -rn "protection_updates_failed\|_protection_failed\|protection_update_failed" tests/` 결과는 5줄뿐이고(test_execution_runtime.py:535, test_execution_day_recovery.py:172·361, test_execution_market_source_review.py:78, 그리고 market_source 계열의 `market_source_pending`) 어느 것도 위 다섯 갈래를 다루지 않는다. `"protection_update_failed"`(일자 전환 사유 문자열)는 tests/ 에 0건.
- **F-A4-3** (높음) 고정된 것(세션): prepare 뒤 장 경계가 닫히면 attempt 가 종료되고 예약이 전부 풀린다는 계약은 고정돼 있다. **그러나 그 시험은 주입된 `session_guard` 를 `GuardDecision(False,'market_closed')` 로 바꿔 만든 것이고, `_session_at` 경계를 실제로 넘지는 않는다.**
  - tests/test_execution_dispatch_reasons.py:413 `test_a_session_that_closes_after_prepare_ends_the_attempt`(424 `f['session'][0] = GuardDecision(False,'market_closed')`, 426-430 NOT_SENT·final_rejected·예약 0).
- **F-A4-4** (높음) 고정되지 않은 갈래(세션) 5가지: ① `'request_session_changed'` 를 단언하는 시험이 tests/ 전체에 **0건** — prepare 와 dispatch 사이에 시계를 15:19→15:21 로 넘기는 시험이 없다 ② `'request_session_mismatch'`(DTO 생성 시점 거부)도 0건 ③ `'unsupported_submit_session'`(break/closed build 거부)도 0건 — gateway 경유의 예외 모양(ErrorEvent)이 고정돼 있지 않다 ④ 세션이 바뀐 뒤 **같은 종목으로 새 SUBMIT 을 재준비할 수 있다**는 단언이 없다(F-A3-4 는 인접 시험에서 유추한 것이다) ⑤ 보호 SELL 이 NOT_SENT 로 사라질 때 로그 한 줄 말고는 어떤 관측 가능한 흔적도 남지 않는다는 사실이 시험으로 못 박혀 있지 않다.
  - `grep -rn "request_session_changed\|request_session_mismatch\|unsupported_submit_session" tests/` 결과 0건. `_session_at` 은 tests/test_execution_owner_gate_authority.py:76·664 와 tests/test_execution_signal_gateway_acceptance.py:74·637 에서만 import 되고 둘 다 `assert _session_at(NOW_KST) == 'regular'` 로 고정 시각을 확인할 뿐이다.

**callers**

- src/execution/safety/runtime.py:1302-1311 — `quote()` 의 apply_quote task + 래치 setter ①
- src/execution/safety/runtime.py:1338-1345 — `repair_protection()` 의 apply_repair task + 래치 setter ②(면제 없음)
- src/execution/safety/runtime.py:1372-1375 — `finalize_initial_r()` 의 task(같은 집합, 래치 안 함)
- src/execution/safety/runtime.py:463-466 — `market_source_pending()` (래치를 읽는 유일한 명령 경로 함수)
- src/execution/safety/commands.py:362-363 — `market_source_pending` 의 유일한 호출자, SUBMIT 에만 적용(CANCEL 면제)
- src/execution/safety/runtime.py:205-206 / 281 / 313 / 341 / 349-350 — `_quiescence_reason` 을 통한 일자 전환 4개 읽기 지점
- src/execution/safety/runtime.py:1469·1476 — `health()['protection_updates_failed']` (제품 호출자 0건)
- src/execution/safety/requests.py:64-72 — `_session_at` (세션 경계 정본)
- src/execution/safety/requests.py:105-108 — `RequestSession.__post_init__` 의 세션 일치 강제
- src/execution/safety/gateway.py:54·161 — 세션을 굽는 유일한 제품 경로(`_bind`)
- src/execution/safety/commands.py:132-141 — `_session`(`request_session_changed` 판정), 호출자: commands.py:127(prepare) 와 commands.py:364(`_evaluate` — prepare·dispatch 양쪽)
- src/execution/safety/commands.py:610-614 — `_dispatch` 의 `session=False` + 재검사 위임 주석
- src/execution/safety/commands.py:631-632 → 579-600 → src/execution/safety/lifecycle.py:371-416 — NOT_SENT 종료 경로
- src/execution/safety/gateway.py:71 → src/core/engine.py:669-673 — NOT_SENT 의 최종 소비자(로그 한 줄)
- src/execution/safety/runtime.py:883-896 — `request_binding['session']` 의 유일한 소비자(대사기 증거 provenance)
- src/core/engine.py:945-947 — attach 에서 legacy 가격 writer 를 막는 지점(= quote 생산자가 0건인 이유)
- src/schedulers/kr_scheduler.py:1020-1027 / src/core/batch_analyzer.py:1744-1757 — attach 보호 청산 구동기 skip(차단 사유 21)
- 제품 호출자 0건 확인: `runtime.quote`, `runtime.repair_protection`, `runtime.health`, `prepare_day_transition`/`rollover_day`/`resume_after_rollover`, `KISRequestBuilder.prepare_cancel`, `session_guard` 제공자, `RequestBoundCommands` 생성자

**main_vs_attach**

두 결함 모두 **attach 전용**이다. `src/execution/safety/` 는 origin/main 에 존재하지 않는다(`git ls-tree -r --name-only origin/main -- src/execution/` → broker/base.py·kis_kr.py·kis_us.py·entry_plan.py 뿐). 따라서 `_protection_failed` 래치도 `_session_at` 기반 prepare/dispatch 세션 대조도 main 에는 대응물이 없다. 약한 지점 9(`_cleanup_stale_pending`)와 달리 main 수정 대상이 아니다.
① 래치(약한 지점 2)의 main 대응물: 없다. main 에는 "보호 갱신 실패"를 기록하는 프로세스 전역 bool 자체가 없고, 가격 갱신 실패는 그 종목의 그 틱에서 끝난다(`engine.update_position_price` 는 0 이하 가격을 무시하고 return 할 뿐이다 — src/core/engine.py:952-954). main 의 전역 정지 수단은 킬스위치 파일뿐이고 그것은 사람이 만든다.
② 세션 경계(약한 지점 7)의 main 대응물: 구조적으로 발생할 수 없다. main 은 세션을 **POST 직전에** 읽어 본문(`ORD_DVSN`/`AFHR_FLPR_YN`)을 만들므로(src/execution/broker/kis_kr.py:508·533·556-559) "판단 시점 세션"과 "송신 시점 세션"이 어긋날 창 자체가 없다. 브로커가 세션 때문에 거절해도 `on_order` 가 `clear_pending` 만 하고 SELL 에는 쿨다운을 걸지 않아(src/core/engine.py:2588-2594) 다음 시세 틱의 `_check_exit_signal` 이 같은 청산 조건을 재감지해 재발행한다(src/schedulers/kr_scheduler.py:1156-1192, 5003). 즉 main 의 안전망은 "재시도"가 아니라 "재감지 루프"이고, attach 에는 그 루프가 없다(생산자 skip — kr_scheduler.py:1020-1027, batch_analyzer.py:1744-1757).
③ 현재 실효성: 두 결함 다 오늘은 제품에서 도달 불가다. 래치의 setter 두 개(`quote`·`repair_protection`)는 제품 호출자가 0건이고, 세션 소멸의 피해자가 될 보호 SELL 생산자도 attach 에서 0건이다. 둘 다 **P1(보호 SELL main 동등, owner quote 경로 배선)이 켜지는 순간 동시에 실효**가 된다 — 즉 P0-4 는 P1 보다 먼저 닫혀야 하는 전제이지 P1 과 병행할 항목이 아니다.

**open_questions**

- 래치를 durable 증거(`protection_quote_admissions`)로 환원할 때, 종목 단위로 좁혀도 되는가? 오늘 `market_source_pending` 은 symbol 을 받지 않는다(runtime.py:463). 반쯤 게시된 시세가 `quote_price_views`·`latest_explicit_quote` 를 통해 **다른 종목의 평가에도** 영향을 주는 경로가 있는지(예: 포트폴리오 equity 를 통한 사이징) 확인하지 않았다 — 확인 전에는 종목 단위 완화가 안전하다고 단정할 수 없다.
- `_quiescence_reason` 의 래치 읽기를 durable 증거로 바꾸면 일자 전환의 기존 계약(B2/B3 에서 고정한 '미해결 보호 입력이 있으면 rollover 금지')이 어디까지 유지되는가? tests/test_execution_day_recovery.py 의 rollover 시험 30여 건 중 어느 것이 bool 래치에 의존하고 어느 것이 durable 행에 의존하는지 분류하지 않았다.
- 세션 재준비를 생산자에게 맡길 때, `gateway._intents` 의 intent_id 재사용이 **재준비 횟수의 상한**을 갖지 않는다(gateway.py:152-156). legacy 는 `_pending_fallback_count` 로 2회 상한을 둔다(engine.py:2009·2038-2043). attach 에서 같은 청산 의도가 세션 경계마다 무한 재준비되는 것을 무엇이 막는지 정해지지 않았다 — P0-4 설계 입력.
- `break`(15:30–15:40)·`closed`(20:00~)에서 "미충족 청산 의도를 살려 둔다"를 어디에 저장하는가? owner state 에 새 행을 만드는 것은 checkpoint 스키마 변경이고, 메모리에만 두면 재시작에서 사라진다. 기존 `protection`/`outbox` 중 재사용 가능한 자리가 있는지 조사하지 않았다.
- `prepare_submit` 이 `closing`(15:20–15:30)에서 시장가를 build 단계에서 거부하는데(requests.py:245-246), legacy 는 같은 구간에서 시장가 에스컬레이션을 포기하고 지정가를 유지한다(engine.py:2029-2031). attach 의 보호 SELL 이 그 구간에 진입했을 때 지정가로 낮춰 재준비할지, 15:40 의 NXT 세션을 기다릴지는 결정되지 않았다(약한 지점 5·D2 와 얽힌다).
- `session_guard` 의 제품 구현이 아직 없다(호출자·제공자 0건). `_session_at` 의 시간 구간 판정과 별개로 휴장일·종목별 거래 가능 여부를 그 guard 가 보게 될 텐데, 그 guard 가 세션 경계를 어떻게 다루느냐에 따라 `request_session_changed` 이전에 다른 사유로 거부될 수도 있다 — P0-4 의 재현 시험을 쓰기 전에 guard 계약을 먼저 정해야 한다.
- 래치가 선 채 일자 경계를 넘으면 CANCEL 까지 막힌다는 F-A1-7 은 코드 추론이다. 일자 전환 구동기가 제품에 0건이라 런타임으로 재현하지 않았다 — P0-4 의 첫 RED 로 이 경로를 실제로 재현할 것을 권한다.

#### 조사 B(예약·stale pending)


## 2. 설계 초안(opus/high — **§3 의 처분으로 일부가 바뀐다. S-A 는 이 초안대로 구현하지 않는다**)

### 2-0. 이 설계가 닫는 것과 닫지 않는 것 (한 눈)

| 약한 지점 | 오늘의 실물 | P0-4 가 하는 것 | 하지 않는 것 |
|---|---|---|---|
| 2 `_protection_failed` | True 로만 가는 프로세스 래치 + **그보다 질긴 durable 봉쇄**(`protection_quote_admissions` 행에도 해제 경로가 없다 — 아래 새 사실 ①) | 래치가 서는 조건을 post-commit 정합 붕괴 **한 갈래**로 좁히고, durable 행에는 **재개(resume)** 라는 해제 경로를 준다 | 게이트의 폭(프로세스 단위·종목 무관)은 그대로 둔다 |
| 4 부분 체결 뒤 예약 | 예약 산술은 **정상**이다(결정 문서 §4-2 항목 4 의 서술이 코드와 어긋난다) | 문서 정정 + 진짜 지속형(만료 계약 부재 → 영구 `partial` → 일자 전환 영구 BLOCKED)을 **시험으로 못박고 차단 사유로 등록** | `FINAL_EXPIRED` 계약을 지어내지 않는다(Q19~Q21 미확정) |
| 7 세션 경계 소멸 | 계약은 옳고 피해자가 0 건이다(보호 SELL 생산자 0 — 차단 사유 21) | 미고정 갈래 5 개를 시험으로 고정하고 **재준비의 소유자는 생산자**임을 P1 입력으로 확정 | dispatch 안 재시도·세션 라벨 교체(= 본문·fingerprint·증거 라벨 위조) |
| 9 `_cleanup_stale_pending` | attach 에서 장부가 비어 **우연히** 무해하고, 주기 호출자에는 가드가 없다 | 함수 맨 앞 **조기 return 한 줄**(live 파일 1개, legacy 경로 바이트 동일) | main 본문(PR #81 판)의 개선 이식 — main 별도 PR 후보(§2-6) |

**조사 입력에 없던, 이번에 코드로 확인한 새 사실 3 가지 (설계의 뼈대다)**

- ① **`protection_quote_admissions` 행 자체에 해제 경로가 없다.** 접수 명령 식별자는 호출마다 새로 굽고(`runtime.py:1187` `command_id = "quote:" + uuid4().hex`) 밖에서 같은 값을 다시 줄 수 없다 → 같은 종목의 다음 `quote()` 는 `admit` 의 중복 검사에 걸리고(`runtime.py:1212-1213`), `repair_protection` 은 그 행이 있으면 replay 자체를 거부하며(`protection_recovery.py:329-331` `unresolved_quote_admission`), 재시작도 행을 지우지 않는다(`tests/test_execution_market_source.py:392-399`). 그 행 하나가 전 종목 SUBMIT(`runtime.py:466` → `commands.py:362-363`)·일자 전환(`day_recovery.py:119-120` → `runtime.py:208`)·초기 R(`initial_r.py:210`)·보호 복구를 **영구** 막는다. bool 래치보다 먼저 닫아야 할 것은 이쪽이다.
- ② **저장·게시·취소 실패는 이미 owner 건강으로 fail-closed 다.** `_commit_publish` 는 commit **전에** `_block()` 하고 실패 시 다시 `_block()` 한다(`application.py:406-411`), `mutate` 의 `lookup_commit` 실패도 같다(`application.py:450-454`). 그러면 `healthy` 가 거짓이 되어(`application.py:230-232`) `_owner_ready`(`commands.py:102-103`)와 `_quiescence_reason`(`runtime.py:196-197`)이 각각 전면 차단한다. **bool 래치가 커버한다고 믿어 온 실패의 대부분은 중복**이다.
- ③ **`_cleanup_stale_pending` 의 pending 추가 지점은 두 곳이 아니라 세 곳**(`kr_scheduler.py:1095-1096`(테마EOD)·`1127-1128`·`1170-1171`)이고 **셋 다** attach 조기 return(`kr_scheduler.py:1026`) 뒤다. 그리고 그 함수 본문(916-1006) 안에 `emit`/`Signal` 발행은 **0 건**이다 — P0-3 의 교훈("live writer 를 막으면 그 writer 가 겸하던 보호 신호 emit 까지 사라지는가?")에 대한 답은 **아니오**이고, 그것을 코드로 확인했다.

---

### 2-1. 항목 2 — `_protection_failed` 의 해제 경로

**(1) 조사가 확정한 사실**

- 래치는 초기화 1 곳(`runtime.py:96`)·True 대입 2 곳(`runtime.py:1308`, `1343`)뿐이고 프로세스 안에서 False 로 되돌리는 줄이 없다.
- 읽는 곳은 셋: SUBMIT 게이트(`runtime.py:466` → `commands.py:362-363`, CANCEL 면제), 일자 전환(`runtime.py:205-206` → `281`·`313`·`341`, 그리고 `_day_closed = False` 는 `349-350` 의 `not self._quiescence_reason(...)` 뒤라 래치가 서면 영영 닫힌 채다), `health()`(`runtime.py:1476`, 제품 호출자 0 건).
- setter ① `apply_quote` 의 콜백은 `admission_rejected` 하나만 면제하고(`runtime.py:1305-1308`), 그 플래그는 진입 경로의 접수 전 실패(`1276-1278`)와 `admit` 안의 **freshness 거부만**(`1204-1210`) 세운다.
- setter ② `repair_protection` 의 콜백은 면제가 아예 없다(`runtime.py:1338-1345`). `reduce_repair` 는 도메인 거부를 BLOCKED receipt 로 흡수하므로(`protection_recovery.py:485-518`) 실제 래치 경로는 `recovery_operation_id_conflict`(`:493`)·저장/게시 실패·취소 셋이다.
- 대조군: `finalize_initial_r` 는 같은 집합에 들어가지만 예외만 소비한다(`runtime.py:1372-1375`).
- 오늘 제품에서는 도달 불가다 — `quote`/`repair_protection` 의 제품 호출자 0 건, legacy 가격 writer 는 attach 에서 차단(`src/core/engine.py:945-947`), 생산자 배선은 P1(`kr_scheduler.py:1018-1027`).

**(2) 설계**

- **H1-1 — `admit` 의 모든 사전 거부는 래치하지 않는다(면제를 조건에서 블록으로).** `runtime.py:1203-1224` 의 `admit` 본문을 `try/except ApplicationBlocked: admission_rejected = True; raise` 로 감싼다. 근거는 구조다: reducer 는 `mutate` 안에서 commit **전에** 실행되고(`application.py:457-458`) 실패하면 candidate(deepcopy)가 버려진다 → 반쯤 게시된 것이 없다. 오늘은 freshness 거부만 면제라, **중복 접수 거부(`runtime.py:1213`)가 전 계좌를 영구 정지시킨다.** 한국어 주석으로 "reducer 는 commit 전에 돈다 — 사전 거부에는 지울 것이 없다"를 남긴다. 새 상태·새 필드 0.
- **H1-2 — quote 래치의 위치를 콜백에서 실패 지점으로 옮긴다.** `runtime.py:1305-1308` 의 조건을 없애고(`finalize_initial_r` 와 같은 "예외만 소비" 모양), 래치는 **post-commit 완료 검증 실패 한 곳**에서만 세운다: `runtime.py:1291-1297` 의 `market_source_completion_conflict` 직전에 `self._protection_failed = True`. 사유는 위 새 사실 ②다 — 나머지 실패는 (a) 사전 거부(지울 것 없음) (b) 접수만 commit 된 중간 실패(durable 행이 남아 `market_source_pending`·`unresolved_quote_admission` 이 이미 막는다) (c) 저장/게시/취소(owner unhealthy 가 이미 막는다) 로 전부 다른 장치가 덮는다. `admission_rejected` nonlocal 은 이 변경으로 **삭제**된다(순증이 아니라 상쇄).
- **H1-3 — repair 래치를 제거한다.** `runtime.py:1338-1345` 의 콜백을 `finalize_initial_r`(`1372-1375`)와 같은 모양으로 바꾼다. 남는 세 실패는 각각 사전 거부(`recovery_operation_id_conflict` 는 receipt 를 쓰기 전에 raise — `protection_recovery.py:490-493`)·owner unhealthy·취소(=owner unhealthy)라 전부 이미 fail-closed 다.
- **H1-4 — durable 미해결 접수의 해제 = 재개(resume).** `quote()` 의 `reduce` 클로저를 **저장된 request 로도 구성할 수 있는 형태**로 뽑고(`_quote_reduce(request, command_id)`; 지금 캡처하는 `price`·`now`·`market_data`·`intent_id`·`source`·`source_event_id`·`entry_observation` 은 전부 접수 행에 그대로 들어 있다 — `runtime.py:1214-1216` 이 `{**request, ...}` 로 저장한다), `apply_quote` 의 `_quote_lock` 안 **맨 앞**에서 같은 종목의 `status == "RECEIVED"` 행이 있으면 그 행의 `command_id` 로 `owner.mutate(command_id, _quote_reduce(저장된 request, command_id))` 를 먼저 돌린다. 성공하면 같은 commit 에서 행이 지워지고(`runtime.py:1263`) 옛 가격의 보호가 실제로 적용된 뒤 새 가격이 이어진다(순서 역전 없음). 그리고 그 commit 에서 `protection_quote_admissions` 가 비면 `self._protection_failed = False` 를 함께 내린다(reducer 안에서 nonlocal 로 관측 — `self.owner.state` 는 deepcopy 를 반환하므로 틱마다 부르지 않는다).
  - 재개가 도메인상 불가하면(저장 행 손상 → `runtime.py:1229-1232` 의 "보호 가격 접수 증거 불일치") **닫지 않고 그대로 올린다**: 행 보존 + 래치 없음 + 새 가격 미접수. fail-closed 유지.
  - 인메모리 캐시(`self._quotes`/`_quote_metadata`)는 재개에서 **갱신하지 않는다** — `quote_price_views`(state) 가 정본이고 캐시는 그보다 새 `source_version` 일 때만 이긴다(`runtime.py:399-402`). 재개는 옛 세대라 앞세울 이유가 없다.
- **H1-5 — 일자 전환의 래치 읽기(`runtime.py:205-206`)는 유지한다.** 삭제하면 B2/B3 가 고정한 "미해결 보호 입력이 있으면 rollover 금지"가 약해진다. H1-1~H1-4 로 래치가 **해제 가능**해졌으므로 영구 봉쇄는 사라진다. durable 행 쪽은 이미 `day_recovery.py:119-120` 이 따로 본다 — 즉 bool 을 유지해도 durable 을 유지해도 계약은 보존된다(조사 A 의 열린 질문 ②는 이 유지로 **회피**한다: 분류 작업이 필요 없다).
- **거부 낱말**: 새로 만들지 않는다. 기존 `market_source_pending` · `protection_update_failed` · `unresolved_quote_admission` · `market_source_completion_conflict` 그대로.
- **새 상태/필드**: 0. 새 공개 API: 0(재개는 `quote()` 안에서 자동, 별도 호출자를 요구하지 않는다 — "부르는 것을 잊으면 영구 정지"라는 footgun 을 만들지 않는다).

**(3) 대안과 기각 이유**

| 대안 | 기각 이유 |
|---|---|
| 래치를 `market_source_pending` 에서 지우고 durable 행만 본다 | post-commit 정합 붕괴(H1-2 의 한 갈래)를 덮을 것이 없어진다. 게이트를 여는 방향의 변경은 P0 에서 하지 않는다 |
| 래치를 종목 단위로 좁힌다 | 반쯤 게시된 시세가 `quote_price_views`/`latest_explicit_quote` 를 거쳐 다른 종목 평가(포트폴리오 equity → 사이징)에 닿는 경로를 확인하지 않았다(조사 A 열린 질문 ①). 확인 전 완화는 fail-open |
| 명시 해제 API(`clear_protection_failure(operation_id)`) + `recovery_receipts` 행 | 새 상태·새 명령·새 거부 낱말·새 운영 절차가 붙는다. 그리고 해제해야 할 **진짜** 대상은 bool 이 아니라 durable 행이라 결국 재개가 또 필요하다 |
| 해제를 D5 runbook(사람이 state 처분)으로 미룬다 | "해제 경로 없음"을 그대로 두는 것이고 P0-4 의 정의를 부정한다. 재시작도 durable 행을 지우지 못한다 |
| `admit` 의 중복 검사를 밖으로 빼서 사전 검사로 | 잠금 밖 검사는 경합을 놓친다. `admit` 안이 맞고, 고칠 것은 검사 위치가 아니라 **면제 조건**이다 |

**(4) 인수 — RED 먼저, 변이 kill 표본**

| # | RED(현행에서 실패해야 한다) | 변이 kill |
|---|---|---|
| A1 | 같은 종목에 미해결 접수가 있는 채 새 `quote()` → `ApplicationBlocked` 이지만 `runtime._protection_failed is False` 이고 `health()['protection_updates_failed'] is False` | H1-1 의 `except` 를 지우면 죽는다 |
| A2 | `repair_protection` 을 같은 `operation_id`·다른 본문으로 2 회 → `recovery_operation_id_conflict` 예외가 나도 래치하지 않는다 | H1-3 을 되돌리면 죽는다 |
| A3 | 두 번째 mutate(`command:quote:*`)만 실패시켜 접수 행을 남긴 뒤 **같은 종목의 다음 `quote()`** → 행이 닫히고 `market_source_pending(state) is False`, 이어서 SUBMIT 이 `market_source_pending` 으로 거부되지 않는다 | 재개 호출을 no-op 으로 바꾸면 죽는다. 재개를 새 가격 **뒤**로 옮겨도 죽는다(순서 단언: 옛 접수의 보호 결정이 먼저 outbox 에 기록된다) |
| A4 | 저장된 접수 행의 `payload_digest` 를 깨고 다음 `quote()` → `ApplicationBlocked("보호 가격 접수 증거 불일치")`, 행 보존, 래치 없음, 새 가격 미접수 | 재개 실패를 삼키고 진행하게 바꾸면 죽는다 |
| A5 | (대조) `store.commit` 실패 → `owner.healthy is False` 이고 SUBMIT 은 `store_or_publication_unhealthy`, 일자 전환은 `publication_mismatch` | 완화되지 않았음의 증거 |
| A6 | post-commit 불일치로 래치를 세운 상태에서 일자 경계를 넘기면 `prepare_day_rollover`/`rollover_day`/`resume_after_rollover` 가 모두 BLOCKED·`protection_update_failed` 이고, 그 뒤 **CANCEL 까지** `day_transition_admission_closed` 로 거부된다 | `runtime.py:205-206` 을 지우면 죽는다. F-A1-7(코드 추론)을 런타임으로 **처음** 재현하는 시험이다 |
| A7 | (대조) `finalize_initial_r` 의 실패는 래치하지 않는다 | `runtime.py:1373-1375` 를 setter 로 바꾸면 죽는다 |

시험 파일: A1·A3·A4 는 `tests/test_execution_market_source.py`(기존 `test_market_apply_failure_reopens_with_exact_pending_or_completed_evidence` 의 fixture 재사용), A2·A7 은 `tests/test_execution_protection_recovery.py`, A5·A6 은 `tests/test_execution_day_recovery.py`.

**(5) 단계** — S-A(부품, `src/execution/safety/runtime.py` 단독 writer). live 파일 0.

---

### 2-2. 항목 4 — 부분 체결 뒤 예약

**(1) 조사가 확정한 사실 — 결정 문서의 서술이 코드와 어긋난다**

- `reserved_quantity` 는 terminal 전에도 체결마다 줄어든다: `economics.py:467` `remaining = max(0, reserved - delta.quantity)` → `:480` `attempt.update(applied_quantity=..., reserved_quantity=remaining, ...)`, 현금·노출·계획위험도 비례 축소(`:468-479`). application 계층이 그 범위를 강제한다(`application.py:316-317`, `:333` "이번 체결보다 큰 예약 수량 해제").
- 보유도 같은 commit 에서 줄어든다(`economics.py:447`) → **`held − reserved` 는 음수가 되지 않는다**(100/40 이면 60−60=0). 결정 문서 §4-2 항목 4 의 "`reserved_quantity` 는 terminal 전까지 줄지 않는다 / `held − reserved < 0`" 은 **틀렸다**. 결론(잔여 60 재매도 불가)만 맞다.
- 즉시형 봉쇄의 실제 낱말은 `unresolved_symbol_attempt`(`commands.py:393-394`)이고 `reserved_quantity_insufficient`(`:422-426`)에는 **도달하지 않는다**(`active` 에는 394 를 통과한 = 다른 종목만 쌓인다).
- 지속형(진짜 영구 봉쇄): 증거 파서는 `FINAL_FILLED` 아니면 `RECONCILING` 만 만들고(`evidence.py:265-279`), `reconcile` 은 `FINAL_EXPIRED` 를 명시 미지원(`lifecycle.py:560-562`) → 부분 체결 주문이 장 마감으로 소멸해도 attempt 는 `partial` 로 남아 그 종목을 잠그고(`commands.py:394`) **일자 전환을 BLOCKED** 로 만든다(`day_recovery.py:127-128` `unresolved_submit`, `:132-133` `remaining_reservation` → `runtime.py:208` → `:281-283`).

**(2) 설계 — 제품 코드 0 줄**

- **H2-1 — 결정 문서 §4-2 항목 4 를 정정하고 재정의한다.** 서술을 "예약 산술은 정상이다. 봉쇄는 (a) 종목 단위 잠금(P1/F8 ①)과 (b) **만료 계약 부재로 부분 체결 주문이 영구 `partial`**(P0-4 가 못박고 D10 이 검증)" 으로 바꾼다. 근거는 위 인용 그대로.
- **H2-2 — 현 계약을 시험으로 못박는다**(아래 (4)). 고치는 것이 아니라 **고정**이다.
- **H2-3 — 설치 차단 사유 22 를 인계 표에 등록한다**: "부분 체결 주문의 장 마감 소멸을 owner 가 배울 수 없다 → 해당 종목 영구 잠금 + **일자 전환 영구 BLOCKED**. 여는 것은 만료 종결 계약이고, 그 근거는 저장소에 없다(Q19~Q21)."
- **하지 않는 것**: `FINAL_EXPIRED` 를 "일별체결조회에서 remaining>0·cancelled==0 이면 만료"로 지어내는 것. 그것은 취소 최종성과 **같은 종류의 fail-open 가정**이고 Q19~Q21 이 근거 없음을 확정했다. `commands.py:394` 의 side 인지도 열지 않는다(late-fill → `unresolved_execution_evidence` 전역 정지, P0-3 Q-1 이 P1 로 등록).

**(3) 대안과 기각 이유**

| 대안 | 기각 |
|---|---|
| 만료 계약을 이번에 넣는다 | 근거 없는 종결은 "취소 0건 = 소멸"과 같은 오판을 owner 안으로 들인다. 게이트를 여는 변경은 전제 뒤 |
| 예약 산술을 "고친다" | 고칠 것이 없다(코드가 이미 옳다). 잔여 60 의 구제는 취소·에스컬레이션(D2/P1) |
| 일자 전환에서 `partial` 을 예외 처리 | 미해결 주문을 남긴 채 일자를 넘기는 것 = 다음 날 장부가 거짓이 된다. fail-closed 유지가 맞다 |
| `_evaluate` 의 종목 잠금을 side 로 나눈다 | late-fill 이 전역 정지를 만든다(P0-3 Q-1 확정) → P1 |

**(4) 인수 — 전부 특성화/계약 고정(제품 변경 없음)**

| # | 단언 | 변이 kill |
|---|---|---|
| B1 | SELL 100 중 40 체결 → `reserved_quantity == 60`, `held == 60`, `held − reserved == 0`; 같은 종목 새 보호 SELL 은 **`unresolved_symbol_attempt`**(`reserved_quantity_insufficient` 가 **아니다**) | `economics.py:467` 을 `remaining = reserved`(=문서가 주장한 동작)로 바꾸면 죽는다 → 문서 정정이 옳다는 증명 |
| B2 | 같은 상태에서 `_evaluate` 의 두 낱말 순서(394 가 426 보다 앞) | `commands.py:393-394` 를 426 뒤로 옮기면 죽는다 |
| B3 | 그 `partial` 이 남은 채 `prepare_day_rollover` → BLOCKED·`unresolved_submit`, 예약을 0 으로 만들어도 `rollover_day` 는 여전히 BLOCKED | `day_recovery.py:127-128` 을 지우면 죽는다 |
| B4 | 파서가 `remaining>0` 행에 대해 `FINAL_FILLED` 를 만들지 않는다(`RECONCILING` + `unsupported_finality`), `reconcile` 은 `FINAL_EXPIRED` 를 최종성으로 읽지 않는다 | `lifecycle.py:561-562` 의 `final = False` 를 `final = True` 로 바꾸면 죽는다 |

시험 파일: `tests/test_execution_resource_release.py`(B1·B2) + `tests/test_execution_day_recovery.py`(B3) + `tests/test_kis_order_evidence.py(파서)·lifecycle 대상 파일`(B4). B1 의 SELL→SELL 표본은 F-B4-2 가 지적한 미고정 갈래를 처음 채운다.

**(5) 단계** — S-B(시험·문서만). 제품 0 줄.

---

### 2-3. 항목 7 — 세션 경계의 조용한 소멸

**(1) 조사가 확정한 사실**

- 세션은 `_session_at`(`requests.py:64-72`) 한 곳이 정하고 DTO 생성 시점에 고정된다(`requests.py:105-108`). gateway 가 `runtime._now()` 한 번으로 굽는다(`gateway.py:151-162`).
- dispatch 는 `session=False` 로 들어가지만 `_bound → _evaluate → _session`(`commands.py:610-614`, `:364`, `:132-141`)이 **dispatch 시점 시계**로 재검사한다 → `request_session_changed` → `_unsent`(`commands.py:631-632`, `:579-600`) → `abandon_candidate` 가 한 transaction 으로 `final_rejected`/`not_sent`/`reason_code` + 예약 전부 해제(`lifecycle.py:404-416`).
- gateway 는 재시도하지 않고(`gateway.py:70-71`, 계약 6) engine 은 로그 한 줄만 남긴다(`src/core/engine.py:669-673`).
- `break`/`closed` 는 build 단계 거부(`requests.py:243-244`)라 예외가 `gateway._bind` 밖으로 새고 engine 이 `errors_count` + ErrorEvent 를 낸다(`engine.py:675-684`) — **소멸의 모양이 다르다**. `closing` 에서는 시장가도 같은 build 거부(`requests.py:245-246`).
- 세션은 전선 본문(`ORD_DVSN`/`AFHR_FLPR_YN`, `requests.py:251-260`)과 fingerprint(`requests.py:199-202`·`231`)와 **증거 provenance**(`runtime.py:883-896`)에 통째로 구워진다.
- legacy 의 안전망은 재시도가 아니라 **재감지**다(`engine.py:2588-2594` 의 SELL 무쿨다운 + `kr_scheduler.py:1156-1192`·`5003`). attach 에는 그 루프가 없다(차단 사유 21).

**(2) 설계 — 제품 코드 0 줄, 계약 확정 + 시험**

- **H3-1 — 소멸의 흔적은 이미 durable 하다는 것을 확정한다.** NOT_SENT 로 닫힌 attempt 는 `state['attempts']` 에 `final_rejected`/`command_status='not_sent'`/`reason_code='request_session_changed'` 로 남고(`lifecycle.py:412-415`), terminal 이라 `recover_unsent()` 도 지우지 않는다. 즉 "관측 가능한 흔적이 로그뿐"이라는 서술은 **부정확**하다 — 로그가 유일한 것은 *운영 화면*이고 장부에는 남는다. 따라서 **P0-4 에서 새 이벤트·새 카운터·새 필드를 만들지 않는다**(`health()` 는 제품 호출자 0 건이라 필드를 늘려도 아무도 읽지 않는다). 시험으로 그 durable 흔적을 못박는다.
- **H3-2 — 재준비의 소유자는 생산자다(P1 계약으로 확정).** 근거: 새 세션에서 새로 `prepare` 해야 본문·fingerprint·증거 라벨이 **함께** 그 세션의 것이 된다(`requests.py:251-260`, `runtime.py:883-896`). dispatch 안에서 세션만 갈아끼우는 재시도는 셋 다 거짓으로 만드므로 **금지**로 명문화한다. NOT_SENT 는 terminal 이라 같은 종목의 새 SUBMIT 을 막지 않고(`lifecycle.py:47-50`, `commands.py:389-395`), gateway 는 intent_id 를 재사용하고 attempt_id 만 새로 발급한다(`gateway.py:151-156`) → **owner 계약 안에서 그대로 가능**하다. 이것을 유추가 아니라 시험으로 확정한다(C2).
- **H3-3 — `break`/`closed` 구간의 "살려 두는 의도"는 P1 로 넘긴다.** 저장 자리(owner state 새 행 = checkpoint 스키마 변경 vs 메모리)는 생산자 설계와 같이 정해야 한다. P0-4 에서 스키마를 건드리지 않는다.

**(3) 대안과 기각 이유**

| 대안 | 기각 |
|---|---|
| dispatch 에서 세션 재검사를 뺀다 | 15:19 에 구운 `regular` 본문이 15:41 에 나간다 = 그 세션에 대해 틀린 주문 |
| dispatch 안에서 세션 라벨만 갱신해 재전송 | 본문·fingerprint·증거 라벨 3 중 위조. 대사기가 `missing_session`/provenance 대조로 종결을 지어내지 않는 설계가 무너진다 |
| gateway 가 자동 재준비 | 계약 6(같은 attempt 재시도 금지) 위반이자, 재준비 횟수 상한이 없는 무한 루프(legacy 는 `_pending_fallback_count` 2 회 상한 — `engine.py:2009`·`2038-2043`) |
| engine 의 NOT_SENT 로그 자리에 ErrorEvent 추가 | live 파일 변경 + 소멸의 두 모양(CommandResult/예외)을 하나로 뭉갠다. 생산자 0 건인 지금 얻는 것이 없다 |

**(4) 인수 — RED 먼저**

| # | RED | 변이 kill |
|---|---|---|
| C1 | prepare 는 15:19, dispatch 는 주입 시계를 15:21 로 옮겨 호출 → `CommandResult(NOT_SENT, reason_code='request_session_changed')`, attempt 는 `final_rejected`/`not_sent`/그 `reason_code`, 예약 4 종(수량·현금·노출·계획위험) 0 | `commands.py:364` 의 `self._session(request)` 를 지우면 죽는다. `tests/` 전체에 `request_session_changed` 단언이 0 건이던 자리 |
| C2 | C1 직후 **같은 종목·같은 intent** 로 새 SUBMIT(지정가)을 prepare→dispatch 할 수 있다(`unresolved_symbol_attempt` 로 막히지 않는다) | `lifecycle.py:47-50` 에서 `FINAL_REJECTED` 를 빼면 죽는다 |
| C3 | 같은 15:21(`closing`)에서 **시장가**는 `unsupported_submit_session` 으로 build 거부, 15:31(`break`)에서는 지정가도 거부 — 둘 다 `gateway.submit` 밖으로 예외가 새고 engine 이 `errors_count` + ErrorEvent 를 낸다 | `requests.py:243-246` 을 지우면 죽는다. `tests/` 전체에 `unsupported_submit_session` 단언 0 건이던 자리 |
| C4 | `RequestSession(…, session='regular')` 을 15:31 의 시각으로 만들면 `request_session_mismatch` | `requests.py:107-108` 을 지우면 죽는다 |
| C5 | (대조) legacy 는 같은 15:21 에 지정가 보호 SELL 을 접수한다(`kis_kr.py:511-520`·`652-678`) — attach 만 죽는다는 격차를 한 파일에 나란히 고정 | 격차의 증거이자 P1 의 수용 기준 |

시험 파일: `tests/test_execution_dispatch_reasons.py`(C1·C2·C4) + `tests/test_execution_signal_gateway_wiring.py`(C3, engine 경유 모양) + 기존 legacy 특성화 옆(C5). **주의**: `session_guard` 의 제품 구현이 0 건이므로(조사 A 열린 질문 ⑥) 시험은 guard 를 `GuardDecision(True, '')` 로 주입해 **세션 경계 자체만** 격리해 본다 — guard 계약은 P1 입력이다.

**(5) 단계** — S-C(시험만). 제품 0 줄.

---

### 2-4. 항목 9 — `_cleanup_stale_pending` 의 attach 인지

**(1) 조사가 확정한 사실**

- engine 브랜치 본문(`kr_scheduler.py:916-1006`)은 PR #81 이전 판이고 `_execution_runtime` 검사가 0 건이다.
- `cancel_all_for_symbol` 은 브로커 인메모리 `self._pending_orders` 만 돈다(`kis_kr.py:944-961`). attach 는 `GuardedKISTransport` 가 직접 POST 하므로 그 캐시가 비어 항상 0 을 돌려주고, 스케줄러는 0 을 "이미 소멸"로 읽어(`:942` 주석·`:958-959` else 분기 없음) pending 해제 → `risk_manager.clear_pending` → **`exit_manager.rollback_stage`**(`:968-977`)까지 간다.
- **현재는 도달 불가**: pending 추가 지점 **세 곳**(`:1095-1096`·`:1127-1128`·`:1170-1171`)이 전부 `_check_exit_signal` 안이고 그 함수는 attach 에서 `:1026` 조기 return → 장부가 비어 `:924` 에서 즉시 return. 안전의 근거가 "설계"가 아니라 "입력이 비어서"다.
- 가드 없는 독립 주기 호출자가 있다(`:5137-5150`, 60 초).
- 본문 안에 `emit`/`Signal` 발행 0 건(916-1006 전수) — 가드로 사라지는 보호 신호 없음(P0-3 교훈 점검 통과).

**(2) 설계 — live 파일 1 개, 실행 줄 1 줄**

- **H4-1 — `_cleanup_stale_pending` 본문 맨 앞(`:922` `bot = self.bot` 직후)에 조기 return 한 줄.**
  ```python
  # attach 설치 시: 취소·pending·ExitManager 단계는 owner 소유다. 여기서 정리하면
  # 빈 브로커 캐시의 취소 0 건을 '이미 소멸'로 읽어 거래소에 살아 있는 SELL 위에서
  # 단계를 되감는다(차단 사유 19 의 같은 뿌리). attach 의 정리는 P1 의 owner CANCEL 이다.
  if getattr(getattr(bot, 'engine', None), '_execution_runtime', None) is not None:
      return
  ```
  P0-3 이 `:1026`·`:1919` 에 쓴 것과 **같은 술어**를 쓴다(새 술어를 만들지 않는다).
- **H4-2 — 호출자 쪽에는 아무것도 넣지 않는다.** 조사 B 는 `run_pending_cleanup`(`:5145`)에도 가드를 권했지만, **함수 맨 앞 가드면 두 호출자(`:1043`·`:5145`)가 모두 덮인다.** 주기 루프가 60 초마다 즉시 return 하는 함수를 부르는 비용은 0 에 가깝고, 가드를 두 곳에 두면 main 병합(`_cleanup_stale_pending` 본문이 main 판 ~150 줄로 통째 교체된다) 때 다시 얹을 자리가 둘로 늘어난다. **한 줄이 병합 비용상 유리하다.**
- **미설치 경로 바이트 동일 계약**: `_execution_runtime` 이 None 인 legacy 에서는 분기가 거짓이라 그 아래 본문이 그대로 실행된다. legacy 본문은 한 줄도 바꾸지 않는다.

**(3) 대안과 기각 이유**

| 대안 | 기각 |
|---|---|
| attach 에서 owner CANCEL 로 잇는다 | 취소 최종성 판정 계약(Q7~Q12 "알 수 없다")·취소 뒤 3 분류(main PR #81)·그 재구성이 전부 필요하다 = P1(D2). `prepare_cancel` 의 제품 호출자는 0 건이고 전제도 무겁다(`commands.py:337-343`) |
| main 본문(3 분류·세대 재검증·면제 유지)을 engine 에 먼저 이식 | engine 브랜치에서 legacy 본문을 바꾸지 않는다는 계약 위반이자, 병합 때 버려지는 작업 |
| 아무것도 하지 않는다(장부가 비니 무해) | 안전이 우연이다. 주기 호출자는 price event 없이도 돈다 — 향후 어떤 배선이 장부에 한 행이라도 넣는 순간 조용히 발화한다 |
| `cancel_all_for_symbol` 을 attach 인지형으로 고친다 | 브로커 파일(live)에 attach 지식을 심는다. 경계가 뒤집힌다 |

**(4) 인수**

| # | RED | 변이 kill |
|---|---|---|
| D1 | attach runtime 이 붙은 bot 에서 `_exit_pending_symbols`/`_exit_pending_timestamps` 에 stale 행을 심고 `_cleanup_stale_pending()` 직접 호출 → `broker.cancel_all_for_symbol` 호출 0, `risk_manager.clear_pending` 0, `exit_manager.rollback_stage` 0, 장부 보존 | 가드를 지우면 죽는다 |
| D2 | 같은 상태에서 `run_pending_cleanup` 의 한 주기를 돌려도 같다(주기 경로도 덮인다) | 가드를 함수 대신 `:1043` 호출부에만 두면 죽는다 |
| D3 | (대조·바이트 동일) `_execution_runtime` 이 없는 bot 에서는 기존 동작 그대로 — 취소 0 건이어도 pending 해제 + `rollback_stage` 수행, 취소 예외는 15 분까지 유지 | legacy 본문을 건드리면 죽는다. `tests/test_engine_legacy_stale_eviction_characterization.py` 의 자매 파일로 둔다 |
| D4 | 이 함수가 `emit`/`Signal` 을 내지 않는다는 사실(P0-3 교훈 점검) — attach 에서 가드로 사라지는 보호 신호 0 | 본문에 emit 이 생기면 죽는 구조 단언 |

**(5) 단계** — S-D(live 파일 `src/schedulers/kr_scheduler.py` 단독 writer, 실행 줄 1).

---

### 2-5. 단계 분할(확정) — 부품 먼저, live 파일은 하나씩

| 단계 | 범위 | writer | live | 끝나는 조건 |
|---|---|---|---|---|
| **S-A** | 항목 2 — H1-1~H1-5 | `src/execution/safety/runtime.py` | 없음 | RED A1~A7 → GREEN, 전체 suite UTC/KST 각 1 회 |
| **S-B** | 항목 4 — 시험 B1~B4 + 결정 문서 §4-2 정정 + 차단 사유 22 등록 | `tests/`, `docs/` | 없음 | 제품 diff 0 줄을 `git diff --stat src/` 로 증명, 전체 suite |
| **S-C** | 항목 7 — 시험 C1~C5 + P1 계약 명문화(재준비 소유자·dispatch 재시도 금지) | `tests/`, `docs/` | 없음 | 제품 diff 0 줄, 전체 suite |
| **S-D** | 항목 9 — 가드 1 줄 + 시험 D1~D4 | `src/schedulers/kr_scheduler.py` | 1 개 | RED D1~D2 → GREEN, D3 로 legacy 불변 증명, 전체 suite |

- 순서는 S-A → S-B → S-C → S-D. S-B·S-C 는 서로 독립이라 병렬 가능하지만 **파일별 단일 writer** 는 유지한다(둘 다 `tests/` 의 서로 다른 파일).
- 각 단계 뒤 전체 suite 를 UTC/KST 각 1 회, **단독 직렬**로 돌린다(호스트 제약). 지정 파일 GREEN 만으로 통합하지 않는다.
- 독립 재현 → 처분 → Codex 독립 리뷰는 P0-3 과 같은 절차. S-A 는 상태·동시성 변경이라 재현 2 명, S-B/S-C 는 시험뿐이라 1 명.
- **금지**(전 단계 공통): `trading_ready` 강제 True · MODIFY · falsy 판정(`if value and …`)·`x or default` · 영어 주석 · legacy 본문 수정 · 새 checkpoint 스키마 행 · `market_source_pending` 의 종목 단위 축소 · 게이트를 여는 완화.

---

### 2-6. main 에도 있는 결함 — **main 별도 PR 후보**(engine 브랜치에서는 건드리지 않는다)

| # | 내용 | 근거 | 상태 |
|---|---|---|---|
| M1 | main 의 `_keep_exit_pending_after_failed_cancel` 은 **전량 청산 pending 에 대해서는 여전히 "취소 0 건 = 소멸"로 해제**한다. 그 근거는 "재발행돼도 KIS 가 주문가능수량 초과로 거절한다"는 D1 의 미확인 가정이다 | main `kr_scheduler.py` 의 해당 docstring, 결정 문서 §7("저장소에 없는 가정 — D10 기록기로 가장 먼저 검증") | **사용자 확인 영역**(live·배포). P0-4 에서 고치지 않는다 |
| M2 | 항목 9 자체는 main 에서 PR #81/#83 으로 **분할 청산에 한해 이미 닫혔다**(거래소 실조회 3 분류·연속 2 회 상한·stage 롤백 없는 해제·면제 유지·pending 세대 재검증). 결정 문서 §6 의 "main 에서 먼저 고친다" 권고는 그 범위에서 완료 | 조사 B `main_vs_attach` | **완료로 기록**(추가 작업 없음) |
| M3 | 병합 부채: engine 의 `_cleanup_stale_pending` 은 main 판으로 **통째 교체**된다. S-D 의 가드 1 줄은 병합 뒤 main 본문 맨 앞에 다시 얹어야 한다 | 두 본문의 차이 | 병합 체크리스트에 등록 |
| M4 | 결정 문서 §6 이 이미 분리한 main 별도 과제 3 건(`scripts/run_trader.py:876-884` 의 symbol 없는 `get_exchange_open_orders()` 등) | 결정 문서 §6 | P0-4 범위 밖 |

항목 2·4·7 에는 main 대응물이 **없다** — `src/execution/safety/` 자체가 origin/main 에 존재하지 않는다. main 의 세션은 POST 직전에 읽어 본문을 만들고(`kis_kr.py:508`·`:556-559`) 실패해도 재감지 루프가 의도를 살린다. 즉 세 항목은 attach 전용이며 **P1(보호 SELL 생산자·owner quote 배선)이 켜지는 순간 동시에 실효**가 된다 — P0-4 는 P1 과 병행할 항목이 아니라 **P1 의 전제**다.


## 3. 적대적 심사 → coordinator 처분 = 확정

두 심사의 공통 결론: **S-A 의 H1-4(재개)는 "오늘 닫혀 있는 게이트를 여는" 방향**이고 P0-4 가 스스로 금지한 것이다. S-B·S-C·S-D 는 정정 뒤 진행 가능.

### 3-1. 항목 2(래치) — S-A 를 좁힌다: **해제 경로를 만들지 않는다**, 설정 조건만 좁힌다

| # | 지적(① 돈/상태 · ② 실현) | 처분 |
|---|---|---|
| ①M1 | 재개는 **아무도 받지 않는 보호 결정**을 커밋하고 `pending_stage` 를 세워 그 종목의 다음 quote 결정이 영구 None(손절이 조용히 사라진다 — legacy 보다 나쁨) | **H1-4 폐기.** 재개는 결정의 소비자(생산자)가 있어야 의미가 있고 그것은 P1 의 owner quote 생산자 설계다 |
| ②M1 | 재개를 `quote()` 안에 두면 `_require_day_admission` 때문에 재시작·다음 날에는 실행 자체가 안 된다. **새 사실: `prepare_day_rollover` 는 reducer 전에 `_day_closed = True`, reducer 는 BLOCKED 여도 `day_transition=PREPARED` 를 commit, 재시작 후에도 그 행으로 복원 → BLOCKED 로 끝난 rollover 시도 한 번이 quote/repair/prepare/dispatch 를 영구히 막는다**(코드 인용, 런타임 미재현) | H1-4 폐기로 회피. 새 사실은 **차단 사유 23** 본문에 넣고 S-A 의 특성화 A6 이 런타임으로 처음 재현한다 |
| ②M2 | 재개가 기존 GREEN 2건(`test_execution_market_source.py:397-399`·`test_execution_protection_recovery.py:227-229` "실패한 접수는 뒤 가격이 덮지 못한다")을 묵시적으로 뒤집는다 | H1-4 폐기 → 두 계약 **그대로 유지** |
| ②M3·①M3 | 재개가 낡은 진입 증거로 BUY 게이트를 열고, 전역 bool 을 남의 종목 행으로 내린다(교차 종목 fail-open) | H1-4 폐기. **bool 래치의 해제도 P0-4 에서 하지 않는다** |
| ①M4 | 재개는 결정적 reducer 실패(같은 입력 → 같은 실패)를 못 고치고 트리거가 같은 종목뿐 | H1-4 폐기. "해제 경로가 생겼다"고 마감에 쓰지 않는다 — **durable 접수 행의 해제 경로 부재 = 차단 사유 23** |
| ①M2 | H1-2 가 `done.cancelled()` 래치까지 없애 commit 직후~완료 검증 전 취소 창에서 SUBMIT 이 열린다 | **H1-2 폐기**(래치 위치 이동 안 함). 콜백은 지금 모양 유지 — 취소는 계속 래치 |
| ②M4·①M5 | H1-1 과 H1-2 가 서로 무효화 → A1 변이가 동치 | H1-2 폐기로 해소. **H1-1 만 남긴다**(`admit` 의 모든 사전 거부 = `ApplicationBlocked` 는 `admission_rejected` 로 — 콜백은 그 플래그를 계속 읽는다). A1 의 kill 은 "`admit` 의 try/except 를 지우면 죽는다"가 **살아 있는 변이**가 된다 |
| ②M6 | 보호-only quote 경로의 래치 | H1-2 폐기라 무관(콜백 래치가 두 경로를 계속 덮는다). 대신 인수에 "보호-only 접수 행이 남으면 래치 없이도 `market_source_pending` 이 SUBMIT 을 막는다" 대조 1건 |
| — | H1-3(repair 래치 제거) | **보류(P1)** — `repair_protection` 제품 호출자 0건, 취소 래치 논거(①M2)가 같이 적용된다. 생산자 설계와 함께 |
| — | H1-5(일자 전환의 래치 읽기 유지) | 유지 |
| ①M6 | 재구성 request 로 증거 검사가 자기 비교가 된다 | H1-4 폐기라 무관 |
| ②M5 | `prepare_day_transition` 은 없는 이름(실제 `prepare_day_rollover`), `tests/test_execution_evidence*.py` 없음 | 계획서 본문 정정(§2 에 반영됨) |

**S-A 확정 범위(제품 변경은 `runtime.py` 의 `admit` 한 곳):**
- **H1-1**: `admit` 본문(`runtime.py:1203-1224`)을 `try/except ApplicationBlocked: admission_rejected = True; raise` 로 감싼다 — 지금은 freshness 거부만 면제라 **중복 접수 거부(`:1212-1213`)가 전 계좌를 영구 정지**시킨다. 근거: reducer 는 commit 전에 돌고 실패하면 candidate 가 버려진다(`application.py:457-458`) — 사전 거부에는 지울 것이 없다. 주석 한국어.
- 새 상태·새 필드·새 API 0. 래치 해제 경로 0(의도).
- **인수(RED 먼저):** A1 같은 종목에 미해결 접수 행이 있는 채 새 `quote()` → `ApplicationBlocked` 이고 `_protection_failed is False`·`health()['protection_updates_failed'] is False`, **그리고** `market_source_pending(state) is True` 라 SUBMIT 은 여전히 `market_source_pending` 으로 거부(fail-closed 유지 증명). 변이: try/except 제거 → 죽음. · A1b 취소 창: 두 번째 mutate 커밋 직후 task 취소 → `_protection_failed is True`(①M2 의 대조군, 현행 통과 — 특성화). · A5 (대조) `store.commit` 실패 → `owner.healthy is False`·SUBMIT `store_or_publication_unhealthy`(있으면 기존 시험 인용, 없으면 추가). · A6 (특성화·②M1 재현) post-commit 불일치 또는 미해결 접수 행이 있는 채 `prepare_day_rollover` → BLOCKED, `_day_closed` True 유지, 다음 날 `CANCEL` 까지 `day_transition_admission_closed`; 재시작(새 runtime 의 `restore()`) 뒤에도 같다. · A7 (대조) `finalize_initial_r` 실패는 래치하지 않는다.
- 시험 파일: A1·A1b·A7 `tests/test_execution_market_source.py`(fixture 재사용), A5·A6 `tests/test_execution_day_recovery.py`.

### 3-2. 항목 4(예약) — S-B 그대로, 정정 3

| 지적 | 처분 |
|---|---|
| ①M9 `held − reserved ≥ 0` 은 owner attempt 에 대사된 체결에 한해 참 | 정정 문구에 한정 삽입. B1 에 "이중 매도를 막는 것은 종목 잠금(`:393-394`)과 예약 검사(`:422-426`) 두 겹" 명시 |
| ② "`reserved_quantity_insufficient` 에는 도달하지 않는다"는 절대 서술 오류 | "이 부분 체결 시나리오에서는" 으로 한정 |
| ② B4 후반은 파서가 FINAL_EXPIRED 를 안 만들므로 합성 `OrderEvidence` 시험 | 그렇게 적는다 |
| 사용자 결정 ①(지속형 partial 의 처리) | **위임된 보수적 기본값 (c)** — 영구 BLOCKED 그대로 + **차단 사유 22 등록**. (a)(b) 는 "만료"를 코드나 사람이 판정하게 해 취소 최종성에서 거부한 종류의 가정을 다시 들인다(심사 ①도 동의) |

### 3-3. 항목 7(세션 경계) — S-C 그대로, 정정 3

| 지적 | 처분 |
|---|---|
| ② C1 전제: `_bound` 가 `_owner_ready(dispatch=True)` 에서 `trading_ready is True` 를 요구 → fixture 가 기존 dispatch 시험과 같은 방식으로 충족해야 사유가 `startup_reconciliation` 이 아니라 `request_session_changed` 로 나온다; `reserved_planned_risk` 는 None 이면 None | C1 에 두 전제 명시 |
| ①M8 `next_market`(15:40~20:00)에서는 지정가·시장가가 같은 본문(`division='05'`, 시장가 `wire_price=0`) | C 시리즈에 next_market 재준비 표본 C6 추가(지정가·시장가 본문 비교). 사용자 결정 ②(closing 의 보호 SELL: 지정가 강등 vs 15:40 대기)와 ③(재준비 횟수 상한)은 **P1 입력**으로 이 사실과 함께 넘긴다 — 지금 묻지 않는다 |
| ② A3 의 두 번째 변이·A7 대조는 무의미 | S-A 재설계로 소멸 |

### 3-4. 항목 9(stale pending) — S-D 그대로, 정정 2

| 지적 | 처분 |
|---|---|
| ①M7 설치기는 `engine.risk_manager` 3장부만 보고 bot 수준 `_exit_pending_symbols/_timestamps` 는 안 본다; 그 집합은 `_sync_portfolio` 의 attach 조기 return **앞**(`:1284` `partial_missing` 계산)에서 읽힌다 → 어긋난 채 설치되면 가드가 영구 동결 | 설치기는 bot 을 못 본다 → **설치 호출자 계약**으로: 설치기 docstring 에 "호출자는 bot 수준 두 장부가 비어 있음을 확인한다" 추가 + S-D 인수 D5 "attach 에서 두 장부가 비어 있으면 `partial_missing` 계산에 영향 없음, 비어 있지 않으면 그 종목의 재시도 방어가 억제된다"(특성화) + 차단 사유 표의 설치 전제에 등록. "새 사실 ③" 문장 정정(writer 3곳은 뒤, reader 1곳은 앞) |
| ② D2 변이는 D1 과 동치, `run_pending_cleanup` 은 sleep 30/60 | D2 는 "주기 경로 커버" 단언으로(변이 아님), sleep 패치 또는 task 취소로 한 주기 |
| §2-6 M1·M2·M4 는 이 worktree 에 main 판 본문이 없어 미검증 | **미검증 표시**. M1(전량 청산 pending 의 "취소 0건=소멸") 은 사용자 확인 영역 — 마감 보고에서 사용자에게 알린다 |

### 3-5. 새 설치 차단 사유(인계 표에 등록)

- **22** 부분 체결 주문의 장 마감 소멸을 owner 가 배울 수 없다(파서 `FINAL_FILLED`/`RECONCILING` 뿐, `reconcile` 은 `FINAL_EXPIRED` 미지원) → 종목 영구 잠금 + **일자 전환 영구 BLOCKED**(`unresolved_submit`·`remaining_reservation`). 여는 것: 만료 종결 계약 — 근거는 저장소에 없다(Q19~Q21), D10 실계좌 관측이 선행.
- **23** 미해결 보호 접수 행(`protection_quote_admissions`)에 해제 경로가 없다 — 재시작도 지우지 못하고 전 종목 SUBMIT·일자 전환·초기 R·보호 복구를 영구히 막는다; 게다가 BLOCKED 로 끝난 `prepare_day_rollover` 한 번이 `_day_closed` 를 영구 래치한다(②M1). 여는 것: P1 의 owner quote 생산자 설계와 **함께**(재개는 결정의 소비자가 있어야 안전 — ①M1).

## 4. 단계(확정)

| 단계 | writer | live | 인수 |
|---|---|---|---|
| **S-A** 항목 2 — H1-1 만 | `src/execution/safety/runtime.py` + `tests/test_execution_market_source.py`·`tests/test_execution_day_recovery.py` | 없음 | A1(RED)·A1b·A5·A6·A7 |
| **S-B** 항목 4 — 시험·문서 | `tests/test_execution_resource_release.py`·`tests/test_execution_day_recovery.py`(B3 는 S-A 와 같은 파일 → **S-B 는 B3 를 `tests/test_execution_p04_reservation.py` 신규 파일에**)·`tests/test_kis_order_evidence.py` + 결정 문서 §4-2 정정 | 없음 | 제품 diff 0 |
| **S-C** 항목 7 — 시험 | `tests/test_execution_dispatch_reasons.py`·`tests/test_execution_signal_gateway_wiring.py`·legacy 특성화 자매 파일 | 없음 | C1~C6, 제품 diff 0 |
| **S-D** 항목 9 — 가드 1줄 | `src/schedulers/kr_scheduler.py` + `src/execution/safety/factory.py`(docstring 한 문단) + 신규 `tests/test_execution_p04_stale_pending.py` | 1 | D1(RED)·D2·D3·D4·D5 |

S-A ∥ S-B+S-C(한 작업자, 서로 다른 파일) ∥ S-D — 작업자 3(상한). 각 결과에 독립 재현 1(S-A 는 상태 경로라 xhigh). 통합은 coordinator, 전체 suite UTC/KST 단독 직렬(시계 창 회피), Codex 교차 리뷰.

**금지**(전 단계): `trading_ready` 강제 True · MODIFY · falsy 판정·`x or default` · 영어 주석 · legacy 본문 수정 · 새 checkpoint 스키마 행 · `market_source_pending` 종목 단위 축소 · 게이트를 여는 완화 · **래치·durable 행의 해제 경로 추가**.

## 5. Do·See (진행하며 채운다)

### 5-1. Do — 구현 3 ∥ 독립 재현 3(워크플로 `wya87gk8s`, opus/high 구현 · opus/xhigh(S-A)/high 재현, 격리 worktree, base `9c36068`)

| 단계 | 커밋 | 구현 결과 | 독립 재현 |
|---|---|---|---|
| S-A | `3fadf81` | `runtime.py` admit 본문 17/14(try 범위 확장 — H1-1 만), A1 **현행 RED**(`assert True is False` at `_protection_failed`) → GREEN, A1b(취소 창 래치)·A5(두 겹: prepare 는 `_require_ready` 의 ApplicationBlocked 로 먼저 끝나고 `store_or_publication_unhealthy` 는 `_owner_ready` 직접 호출로)·A6(`repair_protection` 충돌로 래치 + 미해결 접수 행 → `prepare_day_rollover` BLOCKED·`_day_closed` True·`day_transition=PREPARED` durable·다음 날 CANCEL `day_transition_admission_closed`·새 runtime `restore()` 뒤에도 같다 — **②M1 첫 런타임 재현**)·A7. 11파일 UTC/KST 각 477 passed. 변이 m1(try/except 제거)·m2(cancelled 래치 제거)·m3(일자 전환의 래치 읽기 제거) kill | **CHANGES_REQUIRED — P1 1:** 래치 **단독**의 SUBMIT 게이트가 corpus 어디에도 고정돼 있지 않아 `market_source_pending` 에서 `or self._protection_failed` 를 지우는 완화(X5)가 257건 전부 통과하며 살아남는다(A1 은 durable 행으로, A6 은 일자 전환 읽기로 증명). 비차단 F2: `except Exception` 으로 넓혀도 못 잡음(E1). 재확인: 콜백 취소 래치 유지, 기존 두 계약 시험 무수정 GREEN, RED-first 정직, 새로 면제되는 갈래는 중복 접수 거부뿐이고 그 순간 행이 반드시 존재해 SUBMIT 은 계속 막힘 |
| S-B+S-C | `3773da0` | 제품 0줄(`git diff --stat -- src/` 빈 출력), 신규 `tests/test_execution_p04_reservation.py`(B1~B4)·`tests/test_engine_legacy_session_characterization.py`(C5)·`dispatch_reasons`(C1·C2·C4·C6)·`gateway_wiring`(C3), 결정 문서 §4-2 항목 4 정정. 변이 8종 kill. tests/ 에 `request_session_changed`·`unsupported_submit_session`·`request_session_mismatch`·`reserved_quantity_insufficient` 단언이 0 → 13 | **CHANGES_REQUIRED — P2 3:** R1 인용 오기(`economics.py:437` 은 raise, 실제 `:447` — 계획서 §1 에서 전파), R2 결정 문서가 새로 쓴 "side 를 보지 않으며"에 고정 시험 0(변이 X5 side 인지 잠금 생존), R3 B2 주석의 반사실이 거꾸로. 생존 X1(`- reserved` 제거)은 결함이 아니라 F-B1-6 의 런타임 확인(같은 종목 active SELL 은 :394 가 먼저 raise 라 예약 합계가 항상 0 — "예약분을 뺀다"는 P1/F8 ① 뒤에야 고정 가능). B4 후반의 kill 은 `initial_r` 의 `unverified_finality_evidence` 이중 가드 경유(간접) |
| S-D | `c6c1913` | `kr_scheduler.py` 가드 5줄(주석 3+분기 2, `sed` 로 제거하면 `9c36068` 판과 diff 0 — **바이트 동일 기계 증명**), `factory.py` docstring 한 문단, 신규 `tests/test_execution_p04_stale_pending.py` D1(RED)·D2·D3×3·D4·D5×2. 변이 m1·m2·m3 kill | **APPROVE — P2 2:** P2-1 D1/D2 가 고아 정리 블록(`:986-1006`)을 안 밟는다 — fixture 의 sidecar 장부에 그 종목이 있어 `orphaned` 가 비기 때문(attach 의 실제 상태는 sidecar 빈 집합); 가드를 첫 루프에만 두는 변이 x1 이 생존, probe 로 실해악 재현(취소 POST + `rollback_stage` + 장부 삭제). P2-2 S5 인수 파일의 docstring 이 사실과 어긋남. x2(술어 진리값)는 커밋 코드가 `is not None` 이라 결함 아님 |

### 5-2. 처분(coordinator, `6eaee68` — 제품 0줄)

| 지적 | 처분 |
|---|---|
| S-A F1 | A6 의 ① 직후(래치만 서 있고 접수 행 0·task 0·healthy 인 유일한 순간)에 4줄: 접수 행 없음 · `market_source_pending(state) is True` · SUBMIT prepare 가 `market_source_pending` 으로 거부. 변이 X5(래치 항 제거) 이제 A6 에서 죽는다(직접 확인) |
| S-A F2 | 기록만 — admit 은 전부 commit 전이라 지금은 무해. 예외 종류 경계 시험은 P1 에서 H1-3 을 다룰 때 |
| S-B R1 | 두 문서(결정 문서·계획서 §1/§2-2) `:437`→`:447` |
| S-B R2 | B2 에 한 갈래: 미해결 SELL 위의 같은 종목 **BUY** 도 `unresolved_symbol_attempt`(P1/F8 ① 이 여는 날 결정으로 뒤집힌다고 주석). 변이 X5(side 인지 잠금) 이제 죽는다(직접 확인) |
| S-B R3 | 주석 정정("잠금이 없다면 이 요청은 S1 의 예약 100 때문에 `reserved_quantity_insufficient`") |
| S-D P2-1 | `scheduler(rm_pending=)` 인자 + D1b(sidecar 장부 빈 집합 = attach 실제 상태 → 고아 블록 경로도 가드 뒤). 변이 x1 이제 D1b 에서 죽는다(직접 확인) |
| S-D P2-2 | S5 인수 파일 docstring 사실 정정(단언·fixture 무수정 — S5 의 "제품 무수정 통과" 계약은 제품 코드에 대한 것) |

### 5-3. See — Codex 17차(gpt-6-astra/xhigh, 독립·읽기 전용) → APPROVE

P0/P1 0 · P2 1(C6 docstring: next_market 에서 `ORD_DVSN`·`AFHR_FLPR_YN` 은 같지만 `ORD_UNPR`·fingerprint 는 다르다 — "같은 본문" 서술 부정확 → `a4f8585` 정정). 확인 항목: (a) admit 은 복제 상태의 reducer 안에서 실행되고 반환 뒤에야 commit — 확장된 except 가 commit 뒤 실패를 면제하지 않고, 일반 예외·취소 래치 유지 (b) legacy 본문 바이트 보존·함수 안 emit 부재 (c) 제품 해제 경로 추가 없음(시험의 `owner.mutate` 예약 지우기는 제품 경로 아님) (d) 결정 문서 정정이 인용과 일치.

통합: `3fadf81`→merge · `3773da0`→merge · `c6c1913`→merge `90cfc20` → 처분 `6eaee68` → `a4f8585`. 워크플로 worktree 6개 제거·작업 브랜치 3개 삭제. 전체 suite 단독 직렬(시계 창 회피): **KST 5116 passed**/xfail 2·경고 4·격리 0(`6eaee68`), UTC 는 §5-4.

### 5-4. 마감 — 무엇이 닫혔고 무엇이 아닌가

**닫힌 것(engine 브랜치, 운영 미설치·설치기 제품 호출자 0건):**
- 항목 2(래치) — **설정 조건만**: `admit` 의 모든 사전 거부(중복 접수 포함)는 래치하지 않는다. 취소·post-commit 예외 래치 유지. **해제 경로는 만들지 않았다**(차단 사유 23).
- 항목 4(예약) — 결정 문서 §4-2 정정(예약 산술 정상·봉쇄는 종목 잠금+만료 계약 부재), 계약 고정 시험 B1~B4(+side 무관 잠금). 차단 사유 22.
- 항목 7(세션 경계) — 계약 고정 시험 C1~C6: 소멸의 durable 흔적(`final_rejected`/`not_sent`/`request_session_changed`, 예약 4종 0), 같은 intent 재준비 가능, `break`/`closed`·`closing`+MARKET 은 build 거부(예외 → engine `errors_count`+ErrorEvent), `request_session_mismatch`, legacy 는 같은 15:21 에 지정가를 접수(격차 C5), next_market 의 `ORD_DVSN='05'` 동일성. **재준비의 소유자는 생산자**(dispatch 안 재시도·세션 라벨 교체 금지) = P1 계약.
- 항목 9(stale pending) — attach 조기 return(legacy 바이트 동일), 설치 호출자 계약(bot 수준 장부 비어 있음).

**닫히지 않은 것 / P1 입력:** 래치·durable 행 해제(23, 생산자와 함께) · H1-3 repair 래치 · 만료 종결 계약(22) · closing 의 보호 SELL 처리(지정가 강등 vs 15:40 대기 — next_market 은 `ORD_UNPR` 로만 구분)·재준비 횟수 상한 · break/closed 의 "살려 두는 의도" 저장 자리 · `session_guard` 제품 구현 · C3 의 SELL 표본(생산자 배선 뒤) · `except Exception` 경계 시험 · attach 의 owner CANCEL(D2) · **main 별도 PR 후보 M1**(전량 청산 pending 의 "취소 0건=소멸" 해석이 D1 의 미확인 가정 의존 — 사용자 확인 영역, 이 worktree 에서 main 본문 미검증) · 병합 부채 M3(main 판 `_cleanup_stale_pending` 위에 가드 재적재 — D1b 가 자리를 고정).

**P1 진입 조건(다음 단계 = 보호 SELL 의 main 동등, 결정 문서 D2 방향):** 설계는 owner quote 생산자(차단 21)·취소 뒤 3분류(소멸/생존/판단 불가 180초)·F8 ①/late-fill/자식 가드 side 인지·재준비 소유자·closing 처리·래치 해제(23)를 **한 설계**로 다루고 적대적 심사 2관점을 거친다. 실계좌 스모크(P0-3 §5)는 여전히 사용자 확인 대기.
